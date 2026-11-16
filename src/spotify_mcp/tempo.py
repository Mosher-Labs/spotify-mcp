"""Tempo and energy lookups from ReccoBeats, cached in SQLite on the data volume.

Spotify closed its audio-features endpoint to new apps (November 2024), so
tempo and energy come from ReccoBeats, which takes Spotify track IDs and
needs no key. Manual overrides win over anything fetched, and a track
ReccoBeats does not know is remembered as a miss for a week.
"""

import re
import sqlite3
import time
from pathlib import Path

import httpx

RECCOBEATS = 'https://api.reccobeats.com/v1/audio-features'
BATCH = 25
MISS_TTL_SECONDS = 7 * 24 * 3600
_SPOTIFY_ID = re.compile(r'/track/([A-Za-z0-9]{22})')
FIELDS = ('tempo', 'energy', 'danceability', 'valence', 'loudness')


class TempoCache:
    def __init__(self, path: Path):
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
        self._db.commit()

    def _cached(self, track_id: str) -> dict | None:
        row = self._db.execute(
            'SELECT tempo, energy, danceability, valence, loudness, manual FROM features WHERE track_id=?',
            (track_id,),
        ).fetchone()
        if row is None:
            return None
        return {**dict(zip(FIELDS, row[:5])), 'manual': bool(row[5])}

    def peek(self, track_id: str) -> dict | None:
        """Cached features only; never touches the network."""
        return self._cached(track_id)

    def _recent_miss(self, track_id: str) -> bool:
        row = self._db.execute('SELECT checked_at FROM misses WHERE track_id=?', (track_id,)).fetchone()
        return row is not None and time.time() - row[0] < MISS_TTL_SECONDS

    def set_manual(self, track_id: str, bpm: float, energy: float | None = None) -> None:
        existing = self._cached(track_id) or {}
        self._db.execute(
            'INSERT OR REPLACE INTO features VALUES (?,?,?,?,?,?,1,?)',
            (
                track_id,
                bpm,
                energy if energy is not None else existing.get('energy'),
                existing.get('danceability'),
                existing.get('valence'),
                existing.get('loudness'),
                time.time(),
            ),
        )
        self._db.execute('DELETE FROM misses WHERE track_id=?', (track_id,))
        self._db.commit()

    def _store(self, track_id: str, feature: dict) -> None:
        self._db.execute(
            'INSERT OR IGNORE INTO features VALUES (?,?,?,?,?,?,0,?)',
            (track_id, *(feature.get(f) for f in FIELDS), time.time()),
        )
        self._db.commit()

    async def get_many(self, http: httpx.AsyncClient, track_ids: list[str]) -> dict[str, dict | None]:
        """Features per ID; None for tracks no source knows."""
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
                    self._store(tid, found[tid])
                    result[tid] = self._cached(tid)
                else:
                    self._db.execute(
                        'INSERT OR REPLACE INTO misses VALUES (?,?)', (tid, time.time())
                    )
                    result[tid] = None
            self._db.commit()
        return result


def matching_bpm(tempo: float, low: float, high: float, half_double: bool) -> float | None:
    """The BPM at which a track fits [low, high]; half or double time counts if allowed."""
    candidates = [tempo, tempo * 2, tempo / 2] if half_double else [tempo]
    for bpm in candidates:
        if low <= bpm <= high:
            return round(bpm, 1)
    return None
