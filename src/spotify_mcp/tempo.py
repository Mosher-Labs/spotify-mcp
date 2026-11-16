"""Tempo and energy lookups, cached in SQLite on the data volume.

Spotify closed its audio-features endpoint to new apps (November 2024), so
tempo and energy come from ReccoBeats, which takes Spotify track IDs and
needs no key. When ReccoBeats does not know a track and TEMPO_SERVICE_URL is
set, tempo-service (Mosher-Labs/tempo-service, in-cluster) measures the BPM
from the iTunes preview by artist and title. It gives tempo only, no energy.
Manual overrides win over anything fetched, and a track no source knows is
remembered as a miss for a week.
"""

import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

RECCOBEATS = 'https://api.reccobeats.com/v1/audio-features'
BATCH = 25
MISS_TTL_SECONDS = 7 * 24 * 3600
# tempo-service waits ~3.5 s between iTunes calls, so a lookup takes 4-7 s
# and longer when iTunes backs off.
FALLBACK_TIMEOUT_SECONDS = 90
_SPOTIFY_ID = re.compile(r'/track/([A-Za-z0-9]{22})')
FIELDS = ('tempo', 'energy', 'danceability', 'valence', 'loudness')

# Returns (artist, title) for a track ID, or None.
NameLookup = Callable[[str], Awaitable[tuple[str, str] | None]]


class TempoCache:
    def __init__(self, path: Path, fallback_url: str | None = None, fallback_limit: int = 5):
        self._fallback_url = fallback_url.rstrip('/') if fallback_url else None
        self._fallback_limit = fallback_limit
        self._db = sqlite3.connect(path)
        self._db.execute(
            'CREATE TABLE IF NOT EXISTS features ('
            'track_id TEXT PRIMARY KEY, tempo REAL, energy REAL, danceability REAL,'
            'valence REAL, loudness REAL, manual INTEGER NOT NULL DEFAULT 0,'
            'fetched_at REAL NOT NULL)'
        )
        self._db.execute(
            'CREATE TABLE IF NOT EXISTS misses (track_id TEXT PRIMARY KEY, checked_at REAL NOT NULL)'
        )
        # Added with the tempo-service fallback. Rows from before default to a
        # ReccoBeats result, and to a miss tempo-service has not been asked about.
        self._add_column('features', 'source', "TEXT NOT NULL DEFAULT 'reccobeats'")
        self._add_column('features', 'trust', 'TEXT')
        self._add_column('misses', 'fallback_tried', 'INTEGER NOT NULL DEFAULT 0')
        self._db.commit()

    def _add_column(self, table: str, column: str, decl: str) -> None:
        existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _cached(self, track_id: str) -> dict | None:
        row = self._db.execute(
            'SELECT tempo, energy, danceability, valence, loudness, manual, source, trust '
            'FROM features WHERE track_id=?',
            (track_id,),
        ).fetchone()
        if row is None:
            return None
        return {**dict(zip(FIELDS, row[:5])), 'manual': bool(row[5]), 'source': row[6], 'trust': row[7]}

    def peek(self, track_id: str) -> dict | None:
        """Cached features only; never touches the network."""
        return self._cached(track_id)

    def _recent_miss(self, track_id: str) -> bool:
        row = self._db.execute(
            'SELECT checked_at, fallback_tried FROM misses WHERE track_id=?', (track_id,)
        ).fetchone()
        if row is None or time.time() - row[0] >= MISS_TTL_SECONDS:
            return False
        # A miss from before the fallback existed still gets one fallback try.
        return self._fallback_url is None or bool(row[1])

    def _write(self, track_id: str, values: dict, *, manual: bool, source: str,
               trust: str | None, replace: bool) -> None:
        verb = 'INSERT OR REPLACE' if replace else 'INSERT OR IGNORE'
        self._db.execute(
            f"{verb} INTO features (track_id, tempo, energy, danceability, valence, loudness,"
            ' manual, fetched_at, source, trust) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (track_id, *(values.get(f) for f in FIELDS), int(manual), time.time(), source, trust),
        )
        self._db.execute('DELETE FROM misses WHERE track_id=?', (track_id,))

    def set_manual(self, track_id: str, bpm: float, energy: float | None = None) -> None:
        existing = self._cached(track_id) or {}
        values = {**existing, 'tempo': bpm}
        if energy is not None:
            values['energy'] = energy
        self._write(track_id, values, manual=True, source='manual', trust=None, replace=True)
        self._db.commit()

    def _miss(self, track_id: str, fallback_tried: bool) -> None:
        self._db.execute(
            'INSERT OR REPLACE INTO misses (track_id, checked_at, fallback_tried) VALUES (?,?,?)',
            (track_id, time.time(), int(fallback_tried)),
        )

    def pending(self, track_ids: list[str]) -> list[str]:
        """IDs with no cached tempo and no recent miss: a later call may still fill them."""
        return [t for t in dict.fromkeys(track_ids) if self._cached(t) is None and not self._recent_miss(t)]

    async def _ask_tempo_service(self, http: httpx.AsyncClient, artist: str, title: str) -> dict | None:
        """BPM from tempo-service; None if it has no preview for the track. Raises if it is unreachable."""
        response = await http.get(
            f"{self._fallback_url}/bpm",
            params={'artist': artist, 'title': title},
            timeout=FALLBACK_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get_many(
        self,
        http: httpx.AsyncClient,
        track_ids: list[str],
        names: dict[str, tuple[str, str]] | None = None,
        name_lookup: NameLookup | None = None,
    ) -> dict[str, dict | None]:
        """Features per ID; None for tracks no source knows yet.

        names maps track ID to (artist, title) for the tempo-service fallback;
        name_lookup fetches it for IDs not in names. Each call makes at most
        fallback_limit fallback lookups. The rest stay unrecorded, so pending()
        lists them and a later call picks them up.
        """
        names = names or {}
        result: dict[str, dict | None] = {}
        missing: list[str] = []
        for tid in dict.fromkeys(track_ids):
            cached = self._cached(tid)
            if cached is not None:
                result[tid] = cached
            elif self._recent_miss(tid):
                result[tid] = None
            else:
                missing.append(tid)

        unknown: list[str] = []
        for start in range(0, len(missing), BATCH):
            batch = missing[start : start + BATCH]
            response = await http.get(RECCOBEATS, params={'ids': ','.join(batch)})
            response.raise_for_status()
            found: dict[str, dict] = {}
            for item in response.json().get('content', []):
                match = _SPOTIFY_ID.search(item.get('href', ''))
                if match and item.get('tempo'):
                    found[match.group(1)] = item
            for tid in batch:
                if tid in found:
                    self._write(tid, found[tid], manual=False, source='reccobeats', trust=None, replace=False)
                    result[tid] = self._cached(tid)
                else:
                    unknown.append(tid)
                    result[tid] = None
            self._db.commit()

        if not self._fallback_url:
            for tid in unknown:
                self._miss(tid, fallback_tried=False)
            self._db.commit()
            return result

        for tid in unknown[: self._fallback_limit]:
            name = names.get(tid)
            if name is None and name_lookup is not None:
                name = await name_lookup(tid)
            if name is None:
                continue
            try:
                found = await self._ask_tempo_service(http, *name)
            except httpx.HTTPError:
                break  # down or rate-limited: leave the rest pending for a later call
            if found is None:
                self._miss(tid, fallback_tried=True)
            else:
                self._write(tid, {'tempo': found['bpm']}, manual=False, source='tempo-service',
                            trust=found.get('trust'), replace=False)
                result[tid] = self._cached(tid)
            self._db.commit()
        return result


def matching_bpm(tempo: float, low: float, high: float, half_double: bool) -> float | None:
    """The BPM at which a track fits [low, high]; half or double time counts if allowed."""
    candidates = [tempo, tempo * 2, tempo / 2] if half_double else [tempo]
    for bpm in candidates:
        if low <= bpm <= high:
            return round(bpm, 1)
    return None
