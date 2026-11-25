"""MCP tools. Each takes services explicitly so tests can fake the network."""

from dataclasses import dataclass
from datetime import timedelta

import httpx

from spotify_mcp.history import ListeningLog, to_utc
from spotify_mcp.poller import poll_once

from spotify_mcp.spotify_api import SpotifyClient, SpotifyError, slim_track, track_id, track_uri
from spotify_mcp.tempo import TempoCache, matching_bpm

TIME_RANGES = ('short_term', 'medium_term', 'long_term')
# Spotify accepts at most 100 URIs per playlist write.
WRITE_CHUNK = 100


@dataclass
class Services:
    spotify: SpotifyClient
    tempo: TempoCache
    http: httpx.AsyncClient
    log: ListeningLog


def _uris(values: list[str]) -> list[str]:
    return [track_uri(v) for v in values]


async def search_tracks(s: Services, query: str, limit: int = 10, offset: int = 0) -> list[dict]:
    """Search the catalog. Spotify caps search at 10 results per request."""
    body = await s.spotify.request(
        'GET',
        '/search',
        params={'q': query, 'type': 'track', 'limit': min(max(limit, 1), 10), 'offset': offset},
    )
    return [slim_track(t) for t in body.get('tracks', {}).get('items', [])]


async def top_tracks(s: Services, time_range: str = 'medium_term', limit: int = 50) -> list[dict]:
    if time_range not in TIME_RANGES:
        raise ValueError(f"time_range must be one of {TIME_RANGES}")
    items = await s.spotify.paged(
        '/me/top/tracks', params={'time_range': time_range}, total=min(limit, 50), page=50
    )
    return [slim_track(t) for t in items]


async def saved_tracks(s: Services, limit: int = 50, offset: int = 0) -> list[dict]:
    body = await s.spotify.request(
        'GET', '/me/tracks', params={'limit': min(max(limit, 1), 50), 'offset': offset}
    )
    return [slim_track(i['track']) for i in body.get('items', []) if i.get('track')]


async def _track_name(s: Services, tid: str) -> tuple[str, str] | None:
    """(first artist, title) for one track. Single-track lookups still work in Dev Mode."""
    try:
        track = await s.spotify.request('GET', f"/tracks/{tid}")
    except SpotifyError:
        return None  # the track stays pending; a later call tries again
    artists = track.get('artists') or []
    if not artists or not track.get('name'):
        return None
    return artists[0]['name'], track['name']


def _names(tracks: list[dict]) -> dict[str, tuple[str, str]]:
    return {t['id']: (t['artists'][0], t['name']) for t in tracks if t.get('artists')}


async def track_features(s: Services, track_ids: list[str]) -> dict:
    """Tempo (BPM), energy, danceability, valence and loudness per track ID.

    Each feature carries `source`: "reccobeats", "tempo-service" (BPM measured
    from the iTunes preview; no energy) or "manual". tempo-service results carry
    `trust`: "high", or "check" when its two estimators disagree, often at half
    or double time, so confirm those by ear. A few tempo-service lookups run per
    call; IDs in `pending` have not been tried yet, so call again for them.
    """
    ids = [track_id(t) for t in track_ids]
    features = await s.tempo.get_many(s.http, ids, name_lookup=lambda tid: _track_name(s, tid))
    return {'features': features, 'pending': s.tempo.pending(ids)}


async def set_track_tempo(
    s: Services, track_id_: str, bpm: float, energy: float | None = None
) -> dict:
    """Record a BPM (and optionally energy 0-1) by hand for a track the lookup lacks."""
    if not 40 <= bpm <= 240:
        raise ValueError('bpm must be between 40 and 240')
    if energy is not None and not 0 <= energy <= 1:
        raise ValueError('energy must be between 0 and 1')
    tid = track_id(track_id_)
    s.tempo.set_manual(tid, bpm, energy)
    return {'track_id': tid, 'tempo': bpm, 'energy': energy}


async def candidate_tracks(
    s: Services,
    source: str = 'top',
    min_bpm: float = 0,
    max_bpm: float = 300,
    min_energy: float = 0,
    max_energy: float = 1,
    half_double_time: bool = False,
    scan: int = 150,
    limit: int = 50,
    exclude_skipped: bool = True,
) -> dict:
    """Tracks from the user's own listening that fit a BPM and energy window.

    source: "top" (top tracks across all three time ranges) or "saved" (liked
    songs, newest first, up to `scan`). With half_double_time, a 85 BPM track
    also fits a 170 BPM window. Tracks with no known tempo are counted, not listed.
    Tempos ReccoBeats lacks come from tempo-service a few per call, with no
    energy; those tracks match only when no energy filter is set, and
    `tempo_lookups_pending` says how many are still to try.
    With exclude_skipped, tracks skipped in at least half of 2 or more logged
    workout plays are dropped (see get_track_history). Each match carries its
    workout skip history.
    """
    if source == 'top':
        seen: dict[str, dict] = {}
        for rng in TIME_RANGES:
            for t in await top_tracks(s, rng, 50):
                seen.setdefault(t['id'], t)
        pool = list(seen.values())
    elif source == 'saved':
        pool = []
        offset = 0
        while len(pool) < scan:
            page = await saved_tracks(s, min(50, scan - len(pool)), offset)
            if not page:
                break
            pool.extend(page)
            offset += len(page)
    else:
        raise ValueError('source must be "top" or "saved"')

    features = await s.tempo.get_many(s.http, [t['id'] for t in pool], names=_names(pool))
    skips = s.log.skip_stats()
    matches, unknown, dropped = [], 0, 0
    for t in pool:
        f = features.get(t['id'])
        if f is None:
            unknown += 1
            continue
        bpm = matching_bpm(f['tempo'], min_bpm, max_bpm, half_double_time)
        energy = f['energy']
        if bpm is None:
            continue
        # tempo-service gives no energy: such tracks pass only an unfiltered window.
        if energy is None:
            if min_energy > 0 or max_energy < 1:
                continue
        elif not min_energy <= energy <= max_energy:
            continue
        judged, skipped = skips.get(t['id'], (0, 0))
        if exclude_skipped and judged >= 2 and skipped / judged >= 0.5:
            dropped += 1
            continue
        matches.append(
            {
                **t,
                'tempo': round(f['tempo'], 1),
                'fits_at_bpm': bpm,
                'energy': energy,
                'tempo_source': f['source'],
                'tempo_trust': f['trust'],
                'workout_plays': judged,
                'workout_skips': skipped,
            }
        )
    matches.sort(key=lambda m: (m['energy'] is None, m['energy'] or 0))
    pending = s.tempo.pending([t['id'] for t in pool])
    return {
        'scanned': len(pool),
        'no_tempo_data': unknown,
        # Unknown tracks tempo-service has not tried yet; calling again fills more.
        'tempo_lookups_pending': len(pending),
        'dropped_for_skips': dropped,
        'matches': matches[:limit],
    }


async def my_playlists(s: Services, limit: int = 50) -> list[dict]:
    items = await s.spotify.paged('/me/playlists', params={}, total=min(limit, 50), page=50)
    return [{'id': p['id'], 'name': p['name'], 'public': p.get('public')} for p in items]


async def playlist_items(s: Services, playlist_id: str, limit: int = 100) -> list[dict]:
    """Items of a playlist you own. Spotify returns nothing for others' playlists."""
    items = await s.spotify.paged(
        f"/playlists/{playlist_id}/items", params={}, total=limit, page=50
    )
    out = []
    for i in items:
        item = i.get('item') or i.get('track')
        if item and item.get('type', 'track') == 'track':
            out.append(slim_track(item))
    return out


async def create_playlist(
    s: Services,
    name: str,
    description: str = '',
    public: bool = False,
    track_ids: list[str] | None = None,
) -> dict:
    """Create a playlist (private by default), optionally with tracks.

    Name run playlists "Run · YYYY-MM-DD · <cadence>": the date of the run and
    the target cadence in steps per minute, e.g. "Run · 2026-11-16 · 150". They
    then sort together, oldest first. Spotify's API cannot file playlists into
    folders, so tell the user to drag new ones into their running folder.
    """
    uris = _uris(track_ids or [])
    created = await s.spotify.request(
        'POST', '/me/playlists', json={'name': name, 'description': description, 'public': public}
    )
    await _add(s, created['id'], uris, None)
    return {
        'id': created['id'],
        'name': created['name'],
        'url': created.get('external_urls', {}).get('spotify'),
        'tracks_added': len(uris),
    }


async def rename_playlist(
    s: Services, playlist_id: str, name: str, description: str | None = None
) -> dict:
    """Rename a playlist you own, and optionally replace its description.

    Run playlists follow the naming in create_playlist.
    """
    body = {'name': name}
    if description is not None:
        body['description'] = description
    await s.spotify.request('PUT', f"/playlists/{playlist_id}", json=body)
    return {'id': playlist_id, **body}


async def _add(s: Services, playlist_id: str, uris: list[str], position: int | None) -> None:
    for start in range(0, len(uris), WRITE_CHUNK):
        payload: dict = {'uris': uris[start : start + WRITE_CHUNK]}
        if position is not None:
            payload['position'] = position + start
        await s.spotify.request('POST', f"/playlists/{playlist_id}/items", json=payload)


async def add_tracks(
    s: Services, playlist_id: str, track_ids: list[str], position: int | None = None
) -> dict:
    uris = _uris(track_ids)
    await _add(s, playlist_id, uris, position)
    return {'added': len(uris)}


async def replace_playlist_tracks(s: Services, playlist_id: str, track_ids: list[str]) -> dict:
    """Replace the whole playlist contents with this ordered list."""
    uris = _uris(track_ids)
    first, rest = uris[:WRITE_CHUNK], uris[WRITE_CHUNK:]
    await s.spotify.request('PUT', f"/playlists/{playlist_id}/items", json={'uris': first})
    await _add(s, playlist_id, rest, None)
    return {'tracks': len(uris)}


async def reorder_tracks(
    s: Services, playlist_id: str, range_start: int, insert_before: int, range_length: int = 1
) -> dict:
    """Move range_length tracks starting at range_start to just before insert_before."""
    body = await s.spotify.request(
        'PUT',
        f"/playlists/{playlist_id}/items",
        json={'range_start': range_start, 'insert_before': insert_before, 'range_length': range_length},
    )
    return {'snapshot_id': body.get('snapshot_id')}


async def remove_tracks(s: Services, playlist_id: str, track_ids: list[str]) -> dict:
    uris = _uris(track_ids)
    for start in range(0, len(uris), WRITE_CHUNK):
        chunk = uris[start : start + WRITE_CHUNK]
        await s.spotify.request(
            'DELETE',
            f"/playlists/{playlist_id}/items",
            json={'items': [{'uri': u} for u in chunk]},
        )
    return {'removed': len(uris)}


WORKOUTS_PLAYLIST_NAME = 'Workouts'


def _number(value, name: str, low: float, high: float) -> float | None:
    if value is None:
        return None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def _segments(raw: list[dict] | None) -> list[dict]:
    out = []
    for seg in raw or []:
        out.append(
            {
                'start_at': to_utc(seg['start']),
                'end_at': to_utc(seg['end']),
                'label': seg.get('label'),
                'avg_pace_sec_per_km': _number(seg.get('avg_pace_sec_per_km'), 'segment pace', 90, 1800),
                'avg_hr': _number(seg.get('avg_hr'), 'segment hr', 30, 230),
                'avg_cadence': _number(seg.get('avg_cadence'), 'segment cadence', 30, 260),
            }
        )
    return out


async def log_workout_listening(
    s: Services,
    workout_id: str,
    start: str,
    end: str,
    name: str = '',
    workout_type: str = '',
    planned_intensity: str | None = None,
    avg_pace_sec_per_km: float | None = None,
    avg_hr: float | None = None,
    avg_cadence: float | None = None,
    effort_1_to_10: int | None = None,
    segments: list[dict] | None = None,
    grace_minutes: int = 5,
    add_to_playlist: bool = True,
    per_workout_playlist: bool = False,
) -> dict:
    """Record what played during a workout and tie it to the workout.

    start and end are ISO timestamps with a timezone offset (from Garmin or
    Strava); workout_id is the activity ID, so logging the same workout twice
    replaces the first record. segments is an optional list of
    {start, end, label, avg_pace_sec_per_km, avg_hr, avg_cadence}; each play is
    tagged with the segment it falls in. Completed plays (not skipped) are added
    to the single "Workouts" playlist, skipping tracks already there. Set
    per_workout_playlist for an extra playlist of just this workout.
    """
    start_at, end_at = to_utc(start), to_utc(end)
    if end_at <= start_at:
        raise ValueError('end must be after start')
    effort = None if effort_1_to_10 is None else int(_number(effort_1_to_10, 'effort', 1, 10))
    poll_note = None
    try:
        await poll_once(s.spotify, s.log)
    except Exception as exc:  # fall back to what the poller already stored
        poll_note = f"Could not refresh plays just now ({type(exc).__name__}); used the stored log."
    plays = s.log.window_plays(start_at, end_at, timedelta(minutes=grace_minutes))
    s.log.record_workout(
        {
            'workout_id': workout_id,
            'name': name,
            'workout_type': workout_type,
            'start_at': start_at,
            'end_at': end_at,
            'planned_intensity': planned_intensity,
            'avg_pace_sec_per_km': _number(avg_pace_sec_per_km, 'pace', 90, 1800),
            'avg_hr': _number(avg_hr, 'hr', 30, 230),
            'avg_cadence': _number(avg_cadence, 'cadence', 30, 260),
            'effort': effort,
        },
        plays,
        _segments(segments),
    )
    result: dict = {
        'workout_id': workout_id,
        'plays_matched': len(plays),
        'completed': sum(1 for p in plays if p['completed'] == 1),
        'skipped': sum(1 for p in plays if p['completed'] == 0),
        'unknown': sum(1 for p in plays if p['completed'] is None),
        'tracks': [
            {'track_id': p['track_id'], 'name': p['name'], 'played_at': p['played_at'],
             'completed': p['completed']}
            for p in plays
        ],
    }
    if poll_note:
        result['note'] = poll_note
    if not plays:
        result['note'] = (result.get('note', '') + ' No plays in that window. Spotify keeps only the '
                          'last 50 plays, so older ones may have rolled off.').strip()
        return result

    keep = list(dict.fromkeys(p['track_id'] for p in plays if p['completed'] != 0))
    if add_to_playlist:
        playlist_id = s.log.meta('workouts_playlist_id')
        if not playlist_id:
            created = await create_playlist(
                s, WORKOUTS_PLAYLIST_NAME, 'Songs that carried workouts. Built by spotify-mcp.'
            )
            playlist_id = created['id']
            s.log.set_meta('workouts_playlist_id', playlist_id)
            result['workouts_playlist_url'] = created['url']
        new = [t for t in keep if t not in s.log.playlist_has(playlist_id)]
        await _add(s, playlist_id, [track_uri(t) for t in new], None)
        s.log.note_playlist_tracks(playlist_id, new)
        result['added_to_workouts_playlist'] = len(new)
    if per_workout_playlist and keep:
        label = name or workout_type or workout_id
        created = await create_playlist(
            s, f"{start_at[:10]} {label}", 'Played during this workout.', track_ids=keep
        )
        result['workout_playlist_url'] = created['url']
    return result


async def listening_status(s: Services) -> dict:
    """Health of the listening log: counts, last poll, last Strava event, and how played_at is read."""
    return s.log.status()


async def get_track_history(
    s: Services, track_ids: list[str] | None = None, limit: int = 30
) -> list[dict]:
    """Per track: total plays, plays inside logged workouts, completed vs skipped,
    skip_rate, and the average effort, cadence, HR and pace of the workouts they
    played in. With no track_ids, the most-played-in-workouts tracks.

    Averages show what coincided with a track, not what it caused.
    """
    ids = [track_id(t) for t in track_ids] if track_ids else None
    return s.log.track_history(ids, min(max(limit, 1), 100))


async def get_workout_music_stats(
    s: Services,
    workout_type: str | None = None,
    since: str | None = None,
    min_workout_plays: int = 2,
) -> dict:
    """Aggregates across logged workouts, optionally for one workout_type or since a date.

    Returns each workout with its play and skip counts, tempo bands (10 BPM wide)
    with skip rate and average cadence and effort, and two lists to bias the next
    playlist: keepers (played in min_workout_plays or more workouts, never skipped,
    best average effort first) and often_skipped (skipped in at least half of their plays).
    """
    workouts = s.log.workout_summaries(workout_type, since)
    rows = s.log.workout_play_rows(workout_type, since)
    bands: dict[int, dict] = {}
    for r in rows:
        f = s.tempo.peek(r['track_id'])
        if not f or not f.get('tempo'):
            continue
        b = bands.setdefault(int(f['tempo'] // 10 * 10), {'plays': 0, 'judged': 0, 'skipped': 0, 'cad': [], 'eff': []})
        b['plays'] += 1
        if r['completed'] is not None:
            b['judged'] += 1
            b['skipped'] += int(r['completed'] == 0)
        if r['seg_cadence'] is not None:
            b['cad'].append(r['seg_cadence'])
        if r['effort'] is not None:
            b['eff'].append(r['effort'])
    tempo_bands = [
        {
            'bpm_from': k,
            'bpm_to': k + 10,
            'plays': b['plays'],
            'skip_rate': round(b['skipped'] / b['judged'], 2) if b['judged'] else None,
            'avg_cadence': round(sum(b['cad']) / len(b['cad']), 1) if b['cad'] else None,
            'avg_effort': round(sum(b['eff']) / len(b['eff']), 1) if b['eff'] else None,
        }
        for k, b in sorted(bands.items())
    ]
    history = s.log.track_history(None, 100)
    keepers = [
        h for h in history
        if h['workouts'] >= min_workout_plays and (h['skipped'] or 0) == 0
    ]
    keepers.sort(key=lambda h: (h['avg_effort'] is None, -(h['avg_effort'] or 0)))
    often_skipped = [
        h for h in history
        if h['skip_rate'] is not None and h['skip_rate'] >= 0.5 and (h['skipped'] or 0) >= 2
    ]
    return {
        'workouts': workouts,
        'tempo_bands': tempo_bands,
        'keepers': keepers[:25],
        'often_skipped': often_skipped[:25],
    }
