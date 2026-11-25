"""The listening log: what played, and what it meant for each workout.

Spotify keeps only the last 50 plays, so a poller appends them to SQLite on
the data volume. ``log_workout_listening`` then ties the plays inside a
workout's time window to that workout. The "learning" is plain tables and
aggregate queries: no model, every number can be traced to rows.

Skips are inferred, not observed. Spotify reports no play duration, only
``played_at``, so the time between consecutive plays stands in for how long a
track was listened to. See the RUNBOOK for the caveats.
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

# A play shorter than this share of the track counts as skipped.
SKIP_BELOW = 0.75

_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'


def to_utc(value: str) -> str:
    """Normalize an ISO timestamp to a sortable UTC string. Naive times are rejected."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        raise ValueError(f"{value!r} has no timezone offset; include one, e.g. -07:00 or Z")
    return parsed.astimezone(timezone.utc).strftime(_FMT)


def _dt(value: str) -> datetime:
    return datetime.strptime(value, _FMT).replace(tzinfo=timezone.utc)


def annotate(plays: list[dict], semantics: str) -> list[dict]:
    """Add listened_ms and completed (None when unknowable) to time-ordered plays.

    semantics "end": played_at marks when the play finished, so the gap from the
    previous play is how long this one ran. "start": played_at marks when it
    began, so the gap to the next play is. A gap longer than the track means it
    ran to the end (or was paused), so listened is capped at the duration.
    """
    out = []
    for i, play in enumerate(plays):
        neighbor = None
        if semantics == 'end' and i > 0:
            neighbor = plays[i - 1]
        elif semantics == 'start' and i + 1 < len(plays):
            neighbor = plays[i + 1]
        listened = completed = None
        if neighbor is not None and play['duration_ms']:
            gap = abs((_dt(play['played_at']) - _dt(neighbor['played_at'])).total_seconds()) * 1000
            listened = min(int(gap), play['duration_ms'])
            completed = int(listened / play['duration_ms'] >= SKIP_BELOW)
        out.append({**play, 'listened_ms': listened, 'completed': completed})
    return out


class ListeningLog:
    """One instance per thread: SQLite connections are not shared across threads."""

    def __init__(self, path):
        self._db = sqlite3.connect(path, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS plays (
                played_at TEXT NOT NULL, track_id TEXT NOT NULL, name TEXT, artists TEXT,
                duration_ms INTEGER, context_uri TEXT,
                PRIMARY KEY (played_at, track_id));
            CREATE TABLE IF NOT EXISTS workouts (
                workout_id TEXT PRIMARY KEY, name TEXT, workout_type TEXT,
                start_at TEXT NOT NULL, end_at TEXT NOT NULL, planned_intensity TEXT,
                avg_pace_sec_per_km REAL, avg_hr REAL, avg_cadence REAL, effort INTEGER,
                logged_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS workout_plays (
                workout_id TEXT NOT NULL, played_at TEXT NOT NULL, track_id TEXT NOT NULL,
                listened_ms INTEGER, completed INTEGER,
                seg_label TEXT, seg_pace REAL, seg_hr REAL, seg_cadence REAL,
                PRIMARY KEY (workout_id, played_at, track_id));
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                playlist_id TEXT NOT NULL, track_id TEXT NOT NULL,
                PRIMARY KEY (playlist_id, track_id));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS strava_events (
                activity_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT,
                updated_at REAL NOT NULL);
            """
        )
        self._db.commit()

    # --- meta -----------------------------------------------------------
    def meta(self, key: str) -> str | None:
        row = self._db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))
        self._db.commit()

    # --- plays ----------------------------------------------------------
    def add_plays(self, items: list[dict]) -> int:
        """Store Spotify recently-played items; returns how many were new."""
        before = self._db.total_changes
        for item in items:
            track = item.get('track') or {}
            if not track.get('id'):
                continue
            self._db.execute(
                'INSERT OR IGNORE INTO plays VALUES (?,?,?,?,?,?)',
                (
                    to_utc(item['played_at']),
                    track['id'],
                    track.get('name'),
                    json.dumps([a['name'] for a in track.get('artists', [])]),
                    track.get('duration_ms'),
                    (item.get('context') or {}).get('uri'),
                ),
            )
        self._db.commit()
        return self._db.total_changes - before

    def semantics(self) -> str:
        return self.meta('played_at_semantics') or 'end'

    def timestamp_fit(self) -> dict:
        """How well each reading of played_at explains the gaps between plays.

        If played_at is the end of a play, the gap before a play equals that
        play's length; if it is the start, the gap after equals it. Whichever
        error is smaller is the better reading. Only gaps shorter than 1.5
        track lengths count, so pauses and idle time do not dominate.
        """
        rows = self._db.execute(
            'SELECT played_at, duration_ms FROM plays ORDER BY played_at'
        ).fetchall()
        start_err = end_err = 0.0
        pairs = 0
        for a, b in zip(rows, rows[1:]):
            gap = (_dt(b['played_at']) - _dt(a['played_at'])).total_seconds() * 1000
            if not a['duration_ms'] or not b['duration_ms']:
                continue
            if gap > 1.5 * max(a['duration_ms'], b['duration_ms']):
                continue
            start_err += abs(gap - a['duration_ms'])
            end_err += abs(gap - b['duration_ms'])
            pairs += 1
        return {
            'pairs': pairs,
            'error_if_start_s': round(start_err / 1000, 1),
            'error_if_end_s': round(end_err / 1000, 1),
            'better_fit': None if pairs < 20 else ('start' if start_err < end_err else 'end'),
        }

    def status(self) -> dict:
        row = self._db.execute(
            'SELECT COUNT(*) n, MIN(played_at) first, MAX(played_at) last FROM plays'
        ).fetchone()
        return {
            'plays_logged': row['n'],
            'first_play': row['first'],
            'last_play': row['last'],
            'last_poll_ok': self.meta('last_poll_ok'),
            'last_poll_error': self.meta('last_poll_error'),
            'strava_last_event': json.loads(self.meta('strava_last_event') or 'null'),
            'played_at_reading_in_use': self.semantics(),
            'timestamp_fit': self.timestamp_fit(),
        }

    def plays_between(self, start: str, end: str) -> int:
        """How many stored plays have start <= played_at < end."""
        return self._db.execute(
            'SELECT COUNT(*) FROM plays WHERE played_at >= ? AND played_at < ?', (start, end)
        ).fetchone()[0]

    def window_plays(self, start: str, end: str, grace: timedelta) -> list[dict]:
        """Plays in [start, end + grace], annotated using the neighbor outside the window."""
        sem = self.semantics()
        lo, hi = start, (_dt(end) + grace).strftime(_FMT)
        rows = [dict(r) for r in self._db.execute(
            'SELECT * FROM plays WHERE played_at >= ? AND played_at <= ? ORDER BY played_at', (lo, hi))]
        if not rows:
            return []
        if sem == 'end':
            prev = self._db.execute(
                'SELECT * FROM plays WHERE played_at < ? ORDER BY played_at DESC LIMIT 1', (lo,)).fetchone()
            padded = ([dict(prev)] if prev else []) + rows
            skip = 1 if prev else 0
        else:
            nxt = self._db.execute(
                'SELECT * FROM plays WHERE played_at > ? ORDER BY played_at LIMIT 1', (hi,)).fetchone()
            padded = rows + ([dict(nxt)] if nxt else [])
            skip = 0
        annotated = annotate(padded, sem)
        return annotated[skip : skip + len(rows)]

    # --- workouts -------------------------------------------------------
    def record_workout(self, workout: dict, plays: list[dict], segments: list[dict]) -> None:
        """Replace everything stored for this workout_id (re-logging is safe)."""
        w = workout
        self._db.execute('DELETE FROM workout_plays WHERE workout_id=?', (w['workout_id'],))
        self._db.execute(
            'INSERT OR REPLACE INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (w['workout_id'], w['name'], w['workout_type'], w['start_at'], w['end_at'],
             w['planned_intensity'], w['avg_pace_sec_per_km'], w['avg_hr'], w['avg_cadence'],
             w['effort'], time.time()),
        )
        for play in plays:
            seg = next((s for s in segments if s['start_at'] <= play['played_at'] <= s['end_at']), {})
            self._db.execute(
                'INSERT OR IGNORE INTO workout_plays VALUES (?,?,?,?,?,?,?,?,?)',
                (w['workout_id'], play['played_at'], play['track_id'], play['listened_ms'],
                 play['completed'], seg.get('label'), seg.get('avg_pace_sec_per_km'),
                 seg.get('avg_hr'), seg.get('avg_cadence')),
            )
        self._db.commit()

    def workout_play_spans(self, workout_id: str) -> list[dict]:
        """A logged workout's plays in order, each with the UTC span it played.

        The span runs for the play's listened time (or the track length when
        that is unknown), from played_at back or forward depending on which
        reading of played_at is in use.
        """
        rows = self._db.execute(
            """SELECT wp.played_at, wp.track_id, wp.listened_ms, p.name, p.artists, p.duration_ms,
                      p.context_uri
               FROM workout_plays wp
               JOIN plays p ON p.played_at = wp.played_at AND p.track_id = wp.track_id
               WHERE wp.workout_id=? ORDER BY wp.played_at""",
            (workout_id,),
        ).fetchall()
        return self._spans([dict(r) for r in rows])

    def window_play_spans(self, start: str, end: str, grace: timedelta) -> list[dict]:
        """Like workout_play_spans, for plays in a window not logged as a workout."""
        return self._spans(self.window_plays(start, end, grace))

    def _spans(self, rows: list[dict]) -> list[dict]:
        sem = self.semantics()
        out = []
        for r in rows:
            length = timedelta(milliseconds=r['listened_ms'] or r['duration_ms'] or 0)
            at = _dt(r['played_at'])
            start, end = (at - length, at) if sem == 'end' else (at, at + length)
            out.append({
                'track_id': r['track_id'],
                'name': r['name'] or '',
                'artists': json.loads(r['artists'] or '[]'),
                # Where the play started from, e.g. spotify:playlist:<id>; None if unknown.
                'context_uri': r.get('context_uri'),
                'start': start,
                'end': end,
            })
        return out

    def rename_workout(self, workout_id: str, name: str, workout_type: str) -> None:
        self._db.execute(
            'UPDATE workouts SET name=?, workout_type=? WHERE workout_id=?',
            (name, workout_type, workout_id))
        self._db.commit()

    def delete_workout(self, workout_id: str) -> bool:
        """Drop a workout and its links to plays. Plays and playlists are kept."""
        self._db.execute('DELETE FROM workout_plays WHERE workout_id=?', (workout_id,))
        removed = self._db.execute('DELETE FROM workouts WHERE workout_id=?', (workout_id,)).rowcount
        self._db.commit()
        return bool(removed)

    # --- strava events --------------------------------------------------
    def strava_event(self, activity_id: str) -> dict | None:
        row = self._db.execute(
            'SELECT * FROM strava_events WHERE activity_id=?', (activity_id,)).fetchone()
        return dict(row) if row else None

    def set_strava_event(self, activity_id: str, status: str, detail: dict) -> None:
        self._db.execute(
            'INSERT OR REPLACE INTO strava_events VALUES (?,?,?,?)',
            (activity_id, status, json.dumps(detail), time.time()))
        self._db.commit()

    def playlist_has(self, playlist_id: str) -> set[str]:
        return {r['track_id'] for r in self._db.execute(
            'SELECT track_id FROM playlist_tracks WHERE playlist_id=?', (playlist_id,))}

    def note_playlist_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        self._db.executemany(
            'INSERT OR IGNORE INTO playlist_tracks VALUES (?,?)', [(playlist_id, t) for t in track_ids])
        self._db.commit()

    # --- learning queries -----------------------------------------------
    def track_history(self, track_ids: list[str] | None, limit: int) -> list[dict]:
        """Per track: plays overall and inside workouts, with skip and effort aggregates."""
        where, args = '', []
        if track_ids:
            where = f"WHERE p.track_id IN ({','.join('?' * len(track_ids))})"
            args = list(track_ids)
        rows = self._db.execute(
            f"""
            SELECT p.track_id, MAX(p.name) name, MAX(p.artists) artists,
                   COUNT(*) plays,
                   COUNT(wp.workout_id) workout_plays,
                   COUNT(DISTINCT wp.workout_id) workouts,
                   SUM(wp.completed) completed,
                   SUM(CASE WHEN wp.completed = 0 THEN 1 ELSE 0 END) skipped,
                   AVG(w.effort) avg_effort, AVG(wp.seg_cadence) avg_cadence,
                   AVG(wp.seg_hr) avg_hr, AVG(wp.seg_pace) avg_pace_sec_per_km,
                   MAX(p.played_at) last_played
            FROM plays p
            LEFT JOIN workout_plays wp ON wp.track_id = p.track_id AND wp.played_at = p.played_at
            LEFT JOIN workouts w ON w.workout_id = wp.workout_id
            {where}
            GROUP BY p.track_id
            ORDER BY workout_plays DESC, plays DESC
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            judged = (d['completed'] or 0) + (d['skipped'] or 0)
            d['skip_rate'] = round(d['skipped'] / judged, 2) if judged else None
            d['artists'] = json.loads(d['artists']) if d['artists'] else []
            out.append({k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()})
        return out

    def workout_summaries(self, workout_type: str | None, since: str | None) -> list[dict]:
        clauses, args = [], []
        if workout_type:
            clauses.append('w.workout_type = ?'); args.append(workout_type)
        if since:
            clauses.append('w.start_at >= ?'); args.append(to_utc(since))
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        rows = self._db.execute(
            f"""
            SELECT w.*, COUNT(wp.track_id) plays, SUM(wp.completed) completed,
                   SUM(CASE WHEN wp.completed = 0 THEN 1 ELSE 0 END) skipped
            FROM workouts w LEFT JOIN workout_plays wp ON wp.workout_id = w.workout_id
            {where} GROUP BY w.workout_id ORDER BY w.start_at DESC
            """,
            args,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop('logged_at', None)
            judged = (d['completed'] or 0) + (d['skipped'] or 0)
            d['skip_rate'] = round(d['skipped'] / judged, 2) if judged else None
            out.append(d)
        return out

    def workout_play_rows(self, workout_type: str | None, since: str | None) -> list[dict]:
        """Every logged play with its workout, for band aggregation in Python."""
        clauses, args = [], []
        if workout_type:
            clauses.append('w.workout_type = ?'); args.append(workout_type)
        if since:
            clauses.append('w.start_at >= ?'); args.append(to_utc(since))
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        return [dict(r) for r in self._db.execute(
            f"""SELECT wp.track_id, wp.completed, wp.seg_cadence, wp.seg_hr, wp.seg_pace, w.effort
                FROM workout_plays wp JOIN workouts w ON w.workout_id = wp.workout_id {where}""", args)]

    def skip_stats(self) -> dict[str, tuple[int, int]]:
        """track_id -> (judged plays, skipped plays) across the whole log, workout or not."""
        rows = self._db.execute(
            'SELECT track_id, COUNT(completed) judged, SUM(CASE WHEN completed=0 THEN 1 ELSE 0 END) skipped '
            'FROM workout_plays GROUP BY track_id').fetchall()
        return {r['track_id']: (r['judged'], r['skipped'] or 0) for r in rows}
