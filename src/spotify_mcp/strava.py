"""Strava: OAuth token, activity lookups, and the push-subscription webhook.

When an activity is saved to Strava, Strava POSTs an event to
``/strava/webhook``. The server answers 200 at once and, after the response,
fetches the activity from Strava, then logs the Spotify plays inside its time
window with ``log_workout_listening``.

Strava does not sign events, so nothing in the payload is trusted beyond
"look at activity N": the subscription and owner IDs must match ours, and the
start time, duration and name come from Strava's API, not the POST body.

With activity:write, ``set_strava_description`` edits an activity's
description, and STRAVA_WRITE_DESCRIPTIONS=true writes a music block (summary
line, numbered track list, playlist link) after each webhook-logged activity.
The block is replaced in place on later writes; text around it is kept.

Strava's API reference gives no length limit for ``description`` (checked
2026-11-25), so the block is capped at BLOCK_MAX_CHARS by shortening the list.

Docs (checked 2026-11-25):
https://developers.strava.com/docs/webhooks/
https://developers.strava.com/docs/authentication/
https://developers.strava.com/docs/reference/#api-Activities-updateActivityById
"""

import asyncio
import hmac
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from spotify_mcp import tools
from spotify_mcp.history import ListeningLog, to_utc

AUTH_URL = 'https://www.strava.com/oauth/authorize'
TOKEN_URL = 'https://www.strava.com/oauth/token'
API = 'https://www.strava.com/api/v3'
DEFAULT_REDIRECT_URI = 'http://localhost/exchange_token'

# activity:read_all also covers "Only You" activities; with only
# activity:read, Strava sends a delete event when an activity turns private.
# activity:write is what PUT /activities/{id} needs to change a description.
SCOPES = ('activity:read_all', 'activity:write')
SCOPE = ','.join(SCOPES)

# Starts the automatic music block. The block runs from the 🎧 line through
# the track list and the Playlist line; mode="block" replaces only that.
SUMMARY_MARK = '🎧'
PLAYLIST_URL = 'https://open.spotify.com/playlist/'
# Strava documents no description limit; this keeps our part of it modest.
BLOCK_MAX_CHARS = 1500
_TRACK_LINE = re.compile(r'^\d+\. \S')
_MORE_LINE = re.compile(r'^…and \d+ more\.?$')
_PLAYLIST_LINE = re.compile(r'^Playlist: ' + re.escape(PLAYLIST_URL) + r'\S+$')
METERS_PER_MILE = 1609.344
# Laps shorter than this are button-press stubs, not intervals.
_MIN_LAP_METERS = 200

_EXPIRY_SKEW_SECONDS = 60

# Sports where Strava's average_speed means something as a running pace.
_FOOT_SPORTS = frozenset({'Run', 'TrailRun', 'VirtualRun', 'Walk', 'Hike'})


class StravaError(RuntimeError):
    pass


@dataclass(frozen=True)
class StravaSettings:
    client_id: str
    client_secret: str
    verify_token: str
    token_path: Path
    # Optional: when unset, the server asks Strava for its subscription ID.
    subscription_id: int | None = None
    # Append a music summary to each webhook-logged activity's description.
    write_descriptions: bool = False


def load_strava_settings(data_dir: Path) -> StravaSettings | None:
    """None unless the client ID, client secret and verify token are all set."""
    client_id = os.getenv('STRAVA_CLIENT_ID', '').strip()
    secret = os.getenv('STRAVA_CLIENT_SECRET', '').strip()
    verify = os.getenv('STRAVA_WEBHOOK_VERIFY_TOKEN', '').strip()
    if not (client_id and secret and verify):
        return None
    sub = os.getenv('STRAVA_SUBSCRIPTION_ID', '').strip()
    return StravaSettings(
        client_id=client_id,
        client_secret=secret,
        verify_token=verify,
        token_path=data_dir / 'strava_token.json',
        subscription_id=int(sub) if sub else None,
        write_descriptions=os.getenv('STRAVA_WRITE_DESCRIPTIONS', '').strip().lower()
        in ('1', 'true', 'yes'),
    )


# --- OAuth ---------------------------------------------------------------

def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_URL + '?' + urlencode(
        {
            'client_id': client_id,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'approval_prompt': 'force',
            'scope': SCOPE,
            'state': state,
        }
    )


def code_from_redirect(redirected_url: str, expected_state: str) -> str:
    """The authorization code from the URL Strava redirected to.

    Strava lets the athlete untick scopes on the consent page, so the granted
    scopes are checked too.
    """
    query = parse_qs(urlparse(redirected_url.strip()).query)
    if 'error' in query:
        raise StravaError(f"Strava returned an error: {query['error'][0]}")
    if query.get('state', [''])[0] != expected_state:
        raise StravaError('State mismatch: paste the URL from this login attempt.')
    granted = set(query.get('scope', [''])[0].replace(' ', ',').split(','))
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise StravaError(
            f"Strava granted {sorted(granted)}, missing {missing}. Log in again and leave "
            '"View data about your private activities" and "Upload your activities" ticked.'
        )
    code = query.get('code', [''])[0]
    if not code:
        raise StravaError('No code in that URL.')
    return code


def _write_private(path: Path, data: dict) -> None:
    """Write JSON readable only by this user, replacing the file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class StravaTokenStore:
    """Access and refresh token plus the athlete ID, JSON with mode 0600."""

    def __init__(self, settings: StravaSettings):
        self._s = settings
        self._data: dict | None = None
        self._lock = asyncio.Lock()

    def exists(self) -> bool:
        return self._s.token_path.is_file()

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self._s.token_path.read_text())
            except FileNotFoundError:
                raise StravaError(
                    'Not logged in to Strava. Run spotify-mcp-strava-auth (see RUNBOOK.md).'
                ) from None
        return self._data

    def athlete_id(self) -> int | None:
        try:
            return int(self._load()['athlete_id'])
        except (StravaError, KeyError, TypeError, ValueError):
            return None

    def save_response(self, payload: dict) -> None:
        previous: dict = {}
        try:
            previous = json.loads(self._s.token_path.read_text())
        except (FileNotFoundError, ValueError):
            pass
        athlete = (payload.get('athlete') or {}).get('id') or previous.get('athlete_id')
        if not athlete:
            raise StravaError('Strava returned no athlete ID.')
        self._data = {
            'access_token': payload['access_token'],
            # Strava may rotate the refresh token on any refresh and invalidates
            # the old one at once, so always keep the newest.
            'refresh_token': payload.get('refresh_token') or previous['refresh_token'],
            'expires_at': int(payload['expires_at']),
            'athlete_id': int(athlete),
        }
        _write_private(self._s.token_path, self._data)

    async def _post_token(self, http: httpx.AsyncClient, data: dict) -> dict:
        response = await http.post(
            TOKEN_URL,
            data={'client_id': self._s.client_id, 'client_secret': self._s.client_secret, **data},
        )
        if response.status_code != 200:
            raise StravaError(f"Strava token request failed ({response.status_code}): {response.text[:300]}")
        return response.json()

    async def exchange_code(self, http: httpx.AsyncClient, code: str) -> dict:
        payload = await self._post_token(http, {'code': code, 'grant_type': 'authorization_code'})
        self.save_response(payload)
        return payload

    async def access_token(self, http: httpx.AsyncClient, force: bool = False) -> str:
        async with self._lock:
            data = self._load()
            if not force and data['expires_at'] - _EXPIRY_SKEW_SECONDS > time.time():
                return data['access_token']
            # Another process (the backfill CLI, a new login) may have
            # refreshed and rotated the refresh token, so read the file again.
            self._data = None
            data = self._load()
            if not force and data['expires_at'] - _EXPIRY_SKEW_SECONDS > time.time():
                return data['access_token']
            payload = await self._post_token(
                http, {'grant_type': 'refresh_token', 'refresh_token': data['refresh_token']}
            )
            self.save_response(payload)
            return self._data['access_token']  # type: ignore[index]


class StravaClient:
    def __init__(self, settings: StravaSettings, tokens: StravaTokenStore, http: httpx.AsyncClient):
        self._s = settings
        self.tokens = tokens
        self._http = http
        self._subscription_id = settings.subscription_id

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Call the API with the athlete's token, refreshing it once on a 401."""
        for attempt in (0, 1):
            token = await self.tokens.access_token(self._http, force=attempt == 1)
            response = await self._http.request(
                method, f"{API}{path}", headers={'Authorization': f"Bearer {token}"}, **kwargs
            )
            if response.status_code == 401 and attempt == 0:
                continue
            return response
        return response

    async def activity(self, activity_id: int) -> dict | None:
        """GET /activities/{id}; None when Strava says it does not exist."""
        response = await self._request('GET', f"/activities/{activity_id}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise StravaError(f"Strava GET activity failed ({response.status_code}): {response.text[:300]}")
        return response.json()

    async def set_description(self, activity_id: int, description: str) -> dict:
        """PUT /activities/{id} with a new description. Needs activity:write."""
        response = await self._request(
            'PUT', f"/activities/{activity_id}", json={'description': description}
        )
        if response.status_code >= 400:
            hint = ''
            if response.status_code in (401, 403):
                hint = ' The token may lack activity:write; run spotify-mcp-strava-auth again.'
            raise StravaError(
                f"Strava PUT activity failed ({response.status_code}): {response.text[:300]}.{hint}"
            )
        return response.json()

    async def laps(self, activity_id: int) -> list[dict]:
        response = await self._request('GET', f"/activities/{activity_id}/laps")
        if response.status_code >= 400:
            raise StravaError(f"Strava GET laps failed ({response.status_code})")
        return response.json() or []

    async def streams(self, activity_id: int, keys: list[str]) -> dict:
        """Streams keyed by type, e.g. {'time': {'data': [...]}, 'heartrate': {...}}."""
        response = await self._request(
            'GET', f"/activities/{activity_id}/streams",
            params={'keys': ','.join(keys), 'key_by_type': 'true'},
        )
        if response.status_code == 404:
            return {}
        if response.status_code >= 400:
            raise StravaError(f"Strava GET streams failed ({response.status_code})")
        body = response.json()
        return body if isinstance(body, dict) else {}

    @property
    def known_subscription_id(self) -> int | None:
        """The subscription ID if already known, without asking Strava."""
        return self._subscription_id

    async def subscription_id(self) -> int | None:
        """Our push subscription's ID: from settings, else asked of Strava once."""
        if self._subscription_id is None:
            response = await self._http.get(
                f"{API}/push_subscriptions",
                params={'client_id': self._s.client_id, 'client_secret': self._s.client_secret},
            )
            if response.status_code != 200:
                raise StravaError(f"Strava subscription lookup failed ({response.status_code})")
            subs = response.json()
            # One subscription per app, per the webhook docs.
            self._subscription_id = int(subs[0]['id']) if subs else None
        return self._subscription_id


# --- Descriptions ----------------------------------------------------------

def find_block(lines: list[str]) -> tuple[int, int] | None:
    """[start, end) of the music block in description lines, or None.

    The block is the first line starting with 🎧, then any blank, numbered
    track ("1. ..."), "…and N more" lines, ending at our Playlist line if one
    follows. Anything else ends it, so text after the block is not touched.
    """
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith(SUMMARY_MARK)), None)
    if start is None:
        return None
    last = start
    for j in range(start + 1, len(lines)):
        line = lines[j].strip()
        if not line:
            continue
        if _TRACK_LINE.match(line) or _MORE_LINE.match(line):
            last = j
            continue
        if _PLAYLIST_LINE.match(line):
            last = j
        break
    return start, last + 1


def existing_summary(description: str) -> str | None:
    """The 🎧 line already in a description, or None."""
    lines = description.splitlines()
    found = find_block(lines)
    return lines[found[0]].strip() if found else None


def merge_description(current: str, text: str, mode: str) -> str:
    """The description after writing text with mode append, replace or block."""
    if mode not in ('append', 'replace', 'block'):
        raise ValueError('mode must be "append", "replace" or "block"')
    text = text.strip()
    if mode == 'replace':
        return text
    if not text:
        raise ValueError('text is empty')
    if mode == 'block':
        if not text.startswith(SUMMARY_MARK):
            raise ValueError(f'a block must start with {SUMMARY_MARK}')
        lines = current.splitlines()
        found = find_block(lines)
        if found:
            start, end = found
            return '\n'.join(lines[:start] + text.split('\n') + lines[end:])
    elif text in current:
        return current
    return f"{current.rstrip()}\n\n{text}" if current.strip() else text


async def set_strava_description(
    client: StravaClient, activity_id: int, text: str, mode: str = 'append'
) -> dict:
    """Write text into a Strava activity's description and return the result.

    mode "append" (default) adds text below the existing description after a
    blank line, and does nothing if that exact text is already there, so
    calling it twice is safe. mode "replace" overwrites the description.
    mode "block" takes a music block starting with 🎧 and swaps it for the
    one already there (the 🎧 line, its numbered track list and its
    "Playlist: https://open.spotify.com/playlist/..." line), keeping any
    other text; with no block there yet, it appends.
    """
    if mode not in ('append', 'replace', 'block'):
        raise ValueError('mode must be "append", "replace" or "block"')
    activity = await client.activity(int(activity_id))
    if activity is None:
        raise StravaError(f"Strava activity {activity_id} not found.")
    current = activity.get('description') or ''
    new = merge_description(current, text, mode)
    if new == current:
        return {'activity_id': int(activity_id), 'changed': False, 'description': current}
    updated = await client.set_description(int(activity_id), new)
    return {
        'activity_id': int(activity_id),
        'changed': True,
        'description': updated.get('description', new) if isinstance(updated, dict) else new,
    }


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def _quoted(spans: list[dict]) -> str:
    return ' → '.join(f'"{s["name"]}"' for s in spans)


def _song_at(spans: list[dict], moment: datetime) -> dict | None:
    return next((s for s in spans if s['start'] <= moment <= s['end']), None)


def _songs_during(spans: list[dict], start: datetime, end: datetime, limit: int = 3) -> list[dict]:
    """Songs that played for at least 30 s of [start, end], the longest ones, in play order."""
    overlaps = []
    for i, s in enumerate(spans):
        seconds = (min(s['end'], end) - max(s['start'], start)).total_seconds()
        if seconds >= 30:
            overlaps.append((seconds, i, s))
    longest = sorted(overlaps, key=lambda o: -o[0])[:limit]
    return [s for _, _, s in sorted(longest, key=lambda o: o[1])]


def _pace_per_mile(speed: float) -> str:
    seconds = round(METERS_PER_MILE / speed)
    return f"{seconds // 60}:{seconds % 60:02d}/mi"


def tracks_phrase(spans: list[dict]) -> str:
    """'Wiz Khalifa for 14 of 15 tracks.' or '11 tracks, Snoop Dogg to Lil Jon.'"""
    total = len(spans)
    if total == 1:
        return f'1 track, "{spans[0]["name"]}".'
    artists = [(s['artists'] or ['?'])[0] for s in spans]
    top = max(set(artists), key=artists.count)
    count = artists.count(top)
    if count == total:
        return f"{total} tracks, all {top}."
    if count * 2 > total:
        return f"{top} for {count} of {total} tracks."
    if artists[0] == artists[-1]:
        return f"{total} tracks."
    return f"{total} tracks, {artists[0]} to {artists[-1]}."


def fastest_lap_phrase(activity: dict, laps: list[dict], spans: list[dict]) -> str | None:
    """'Fastest lap (9:43/mi) on "A" → "B"', from Strava's own laps.

    Needs two or more laps: one lap is the whole activity, not an interval.
    """
    if activity.get('sport_type') not in _FOOT_SPORTS:
        return None
    real = [lap for lap in laps if (lap.get('distance') or 0) >= _MIN_LAP_METERS
            and (lap.get('average_speed') or 0) > 0 and lap.get('start_date')]
    if len(real) < 2:
        return None
    lap = max(real, key=lambda lap: lap['average_speed'])
    start = _parse_time(lap['start_date'])
    songs = _songs_during(spans, start, start + timedelta(seconds=int(lap.get('elapsed_time') or 0)))
    phrase = f"Fastest lap ({_pace_per_mile(lap['average_speed'])})"
    return f"{phrase} on {_quoted(songs)}" if songs else phrase


def peak_hr_phrase(activity: dict, streams: dict, spans: list[dict]) -> str | None:
    """'HR peaked at 156 on "Song"', from the heart-rate stream."""
    hr = (streams.get('heartrate') or {}).get('data') or []
    times = (streams.get('time') or {}).get('data') or []
    if not hr or len(hr) != len(times):
        return None
    peak = max(hr)
    if not 30 <= peak <= 230:
        return None
    # The time stream counts seconds from the activity's start.
    moment = _parse_time(activity['start_date']) + timedelta(seconds=times[hr.index(peak)])
    song = _song_at(spans, moment)
    return f"HR peaked at {peak} on {_quoted([song])}" if song else f"HR peaked at {peak}"


def build_summary(activity: dict, spans: list[dict], laps: list[dict], streams: dict) -> str | None:
    """The one-line music summary, from measured data only. None with no plays."""
    if not spans:
        return None
    parts = [p for p in (fastest_lap_phrase(activity, laps, spans),
                         peak_hr_phrase(activity, streams, spans)) if p]
    line = f"{SUMMARY_MARK} {tracks_phrase(spans)}"
    if parts:
        line += ' ' + '; '.join(parts) + '.'
    return line


def playlist_for(spans: list[dict], workouts_playlist_id: str | None) -> str | None:
    """The playlist ID to link: the one most plays came from, else Workouts.

    A playlist counts only if it was the context of more than half the plays.
    Plays logged before context was stored, or started from an album, an
    artist or Liked Songs, have no playlist and fall back to Workouts.
    """
    ids = [uri.rsplit(':', 1)[1] for uri in (s.get('context_uri') or '' for s in spans)
           if ':playlist:' in uri]
    for pid in dict.fromkeys(ids):
        if ids.count(pid) * 2 > len(spans):
            return pid
    return workouts_playlist_id


def build_block(summary: str, spans: list[dict], playlist_id: str | None,
                max_chars: int = BLOCK_MAX_CHARS) -> str:
    """Summary line, numbered tracks in play order, and the playlist link.

    If the block would pass max_chars, the list ends early with "…and N more".
    """
    tracks = [f"{i}. {(s['artists'] or ['?'])[0]} – {s['name'] or '?'}"
              for i, s in enumerate(spans, 1)]
    tail = ['', f"Playlist: {PLAYLIST_URL}{playlist_id}"] if playlist_id else []

    def render(n: int) -> str:
        more = [f"…and {len(tracks) - n} more"] if n < len(tracks) else []
        return '\n'.join([summary, '', *tracks[:n], *more, *tail])

    n = len(tracks)
    while n > 0 and len(render(n)) > max_chars:
        n -= 1
    return render(n)


async def description_block(client: StravaClient, log: ListeningLog, activity: dict,
                            spans: list[dict], summary: str | None = None) -> str | None:
    """The music block for an activity, or None with no plays.

    With summary given, it is used as the 🎧 line as is. Otherwise the line is
    built from laps and the HR stream; if Strava fails on either, that part is
    left out.
    """
    if not spans:
        return None
    if summary is None:
        aid = int(activity['id'])
        try:
            laps = await client.laps(aid)
        except (StravaError, httpx.HTTPError):
            laps = []
        try:
            streams = await client.streams(aid, ['time', 'heartrate'])
        except (StravaError, httpx.HTTPError):
            streams = {}
        summary = build_summary(activity, spans, laps, streams)
    if not summary:
        return None
    return build_block(summary, spans, playlist_for(spans, log.meta('workouts_playlist_id')))


async def rebuild_description(client: StravaClient, log: ListeningLog, activity_id: int,
                              write: bool = False, keep_summary: bool = True,
                              grace_minutes: int = 5) -> dict:
    """Rebuild one activity's music block from the listening log (backfill).

    Plays come from the workout logged under this activity ID, or, if there
    is none, from the stored plays inside the activity's time window. With
    keep_summary, a 🎧 line already in the description is kept word for word
    and only the track list and link are rebuilt. Nothing is written unless
    write is true; the result shows the description either way.
    """
    aid = int(activity_id)
    activity = await client.activity(aid)
    if activity is None:
        raise StravaError(f"Strava activity {aid} not found.")
    spans, source = log.workout_play_spans(str(aid)), 'logged workout'
    if not spans:
        start, end = window(activity)
        spans = log.window_play_spans(to_utc(start), to_utc(end), timedelta(minutes=grace_minutes))
        source = 'activity window'
    current = activity.get('description') or ''
    result: dict = {'activity_id': aid, 'plays': len(spans), 'plays_from': source}
    kept = existing_summary(current) if keep_summary else None
    block = await description_block(client, log, activity, spans, kept)
    if not block:
        return {**result, 'changed': False, 'written': False, 'description': current,
                'note': 'no plays stored for this activity'}
    new = merge_description(current, block, 'block')
    result.update(summary='kept' if kept else 'generated', changed=new != current,
                  written=False, description=new)
    if write and new != current:
        await client.set_description(aid, new)
        result['written'] = True
    return result


# --- Webhook events --------------------------------------------------------

def _pace(activity: dict) -> float | None:
    speed = activity.get('average_speed') or 0
    if activity.get('sport_type') not in _FOOT_SPORTS or speed <= 0:
        return None
    pace = 1000 / speed
    return round(pace, 1) if 90 <= pace <= 1800 else None


def _hr(activity: dict) -> float | None:
    hr = activity.get('average_heartrate')
    return float(hr) if hr and 30 <= hr <= 230 else None


def window(activity: dict) -> tuple[str, str]:
    """(start, end) in UTC from start_date and elapsed_time (pauses included)."""
    start = datetime.fromisoformat(activity['start_date'].replace('Z', '+00:00'))
    end = start + timedelta(seconds=int(activity.get('elapsed_time') or 0))
    fmt = lambda d: d.astimezone(timezone.utc).isoformat()  # noqa: E731
    return fmt(start), fmt(end)


class WebhookProcessor:
    """Turns verified Strava events into listening-log entries.

    Work runs one event at a time (a bulk upload should not fan out into
    parallel Spotify calls), and an activity already logged is skipped, so
    Strava's retries and duplicate deliveries do nothing.
    """

    def __init__(self, settings: StravaSettings, client: StravaClient, services: tools.Services):
        self.settings = settings
        self.client = client
        self.s = services
        self._lock = asyncio.Lock()

    def verify_token_ok(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self.settings.verify_token.encode())

    def quick_check(self, event: dict) -> str | None:
        """Cheap checks before answering. Returns why the event is ignored, or None."""
        if event.get('object_type') not in ('activity', 'athlete'):
            return 'unknown object_type'
        if event.get('aspect_type') not in ('create', 'update', 'delete'):
            return 'unknown aspect_type'
        try:
            int(event['object_id']), int(event['owner_id']), int(event['subscription_id'])
        except (KeyError, TypeError, ValueError):
            return 'missing ids'
        athlete = self.client.tokens.athlete_id()
        if athlete is None:
            return 'not logged in to Strava'
        if int(event['owner_id']) != athlete:
            return 'owner_id is not the logged-in athlete'
        known = self.client.known_subscription_id
        if known is not None and int(event['subscription_id']) != known:
            return 'subscription_id does not match'
        return None

    async def handle(self, event: dict) -> dict:
        """Process one event that passed quick_check. Never raises."""
        async with self._lock:
            try:
                result = await self._handle(event)
            except Exception as exc:  # keep serving; the status tool shows it
                result = {'status': 'error', 'error': f"{type(exc).__name__}: {str(exc)[:200]}"}
                if event.get('object_type') == 'activity':
                    self.s.log.set_strava_event(str(event['object_id']), 'error', result)
            self.s.log.set_meta(
                'strava_last_event',
                json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                            'object_id': event.get('object_id'), 'aspect_type': event.get('aspect_type'),
                            **result})[:1000],
            )
            print(f"strava: {event.get('aspect_type')} {event.get('object_id')}: {result}", file=sys.stderr)
            return result

    async def _handle(self, event: dict) -> dict:
        sub = await self.client.subscription_id()
        if sub is None or int(event['subscription_id']) != sub:
            return {'status': 'ignored', 'reason': 'subscription_id does not match'}
        if event['object_type'] == 'athlete':
            if (event.get('updates') or {}).get('authorized') == 'false':
                self.s.log.set_meta('strava_deauthorized_at', str(event.get('event_time')))
                return {'status': 'deauthorized'}
            return {'status': 'ignored', 'reason': 'athlete update'}

        aid = int(event['object_id'])
        key = str(aid)
        prior = self.s.log.strava_event(key)
        aspect = event['aspect_type']

        if aspect == 'delete':
            # A delete can also mean "turned private" for apps without
            # read_all, so confirm with Strava before dropping anything.
            if await self.client.activity(aid) is not None:
                return {'status': 'ignored', 'reason': 'activity still exists'}
            removed = self.s.log.delete_workout(key)
            self.s.log.set_strava_event(key, 'deleted', {'workout_removed': removed})
            return {'status': 'deleted', 'workout_removed': removed}

        if aspect == 'update' and prior and prior['status'] == 'logged':
            activity = await self.client.activity(aid)
            if activity is None:
                return {'status': 'ignored', 'reason': 'activity not found'}
            self.s.log.rename_workout(key, activity.get('name') or '', activity.get('sport_type') or '')
            return {'status': 'renamed'}

        if prior and prior['status'] == 'logged':
            return {'status': 'duplicate'}

        # create, or an update to an activity not logged yet (a create that was
        # missed while the pod was down; editing the title retries it).
        activity = await self.client.activity(aid)
        if activity is None:
            return {'status': 'ignored', 'reason': 'activity not found'}
        start, end = window(activity)
        if end <= start:
            return {'status': 'ignored', 'reason': 'activity has no duration'}
        logged = await tools.log_workout_listening(
            self.s,
            workout_id=key,
            start=start,
            end=end,
            name=activity.get('name') or '',
            workout_type=activity.get('sport_type') or activity.get('type') or '',
            avg_pace_sec_per_km=_pace(activity),
            avg_hr=_hr(activity),
        )
        result = {
            'status': 'logged',
            'plays_matched': logged['plays_matched'],
            'added_to_workouts_playlist': logged.get('added_to_workouts_playlist', 0),
        }
        gap = self.possible_gap(to_utc(start))
        if gap:
            result['possible_gap'] = gap
        if logged.get('note'):
            result['note'] = logged['note']
        if self.s.log.meta('last_poll_error'):
            # Spotify could not be read, so this used only stored plays. Leave
            # it retryable: a Strava retry or a title edit runs it again.
            result['status'] = 'error'
            result['error'] = self.s.log.meta('last_poll_error')
        elif self.settings.write_descriptions and logged['plays_matched']:
            result['description'] = await self.write_summary(activity, key)
        self.s.log.set_strava_event(key, result['status'], result)
        return result

    async def write_summary(self, activity: dict, key: str) -> str:
        """Write or replace the music block. Returns what happened.

        A failure to write never fails the logging.
        """
        aid = int(activity['id'])
        spans = self.s.log.workout_play_spans(key)
        block = await description_block(self.client, self.s.log, activity, spans)
        if not block:
            return 'skipped: no plays'
        try:
            written = await set_strava_description(self.client, aid, block, mode='block')
        except (StravaError, httpx.HTTPError) as exc:
            return f"error: {str(exc)[:200]}"
        return 'written' if written['changed'] else 'unchanged'

    def possible_gap(self, start_at: str) -> str | None:
        """Explain if plays from the start of the workout may have rolled off.

        Spotify returns only the newest 50 plays. If the fetch was full and
        its oldest play is after the workout started, and nothing stored from
        an earlier fetch fills the stretch in between, plays may be missing.
        """
        count = int(self.s.log.meta('last_poll_count') or 0)
        oldest = self.s.log.meta('last_poll_oldest')
        if count < 50 or not oldest or oldest <= start_at:
            return None
        if self.s.log.plays_between(start_at, oldest):
            return None
        return (f"Spotify's 50 most recent plays only reach back to {oldest}, after the "
                f"workout started at {start_at}. Plays before then may be missing.")
