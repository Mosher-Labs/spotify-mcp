"""Strava: OAuth token, activity lookups, and the push-subscription webhook.

When an activity is saved to Strava, Strava POSTs an event to
``/strava/webhook``. The server answers 200 at once and, after the response,
fetches the activity from Strava, then logs the Spotify plays inside its time
window with ``log_workout_listening``.

Strava does not sign events, so nothing in the payload is trusted beyond
"look at activity N": the subscription and owner IDs must match ours, and the
start time, duration and name come from Strava's API, not the POST body.

Docs (checked 2026-11-25):
https://developers.strava.com/docs/webhooks/
https://developers.strava.com/docs/authentication/
"""

import asyncio
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from spotify_mcp import tools
from spotify_mcp.history import to_utc

AUTH_URL = 'https://www.strava.com/oauth/authorize'
TOKEN_URL = 'https://www.strava.com/oauth/token'
API = 'https://www.strava.com/api/v3'
DEFAULT_REDIRECT_URI = 'http://localhost/exchange_token'

# Read-only. activity:read_all also covers "Only You" activities; with only
# activity:read, Strava sends a delete event when an activity turns private.
SCOPE = 'activity:read_all'

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
    scope is checked too.
    """
    query = parse_qs(urlparse(redirected_url.strip()).query)
    if 'error' in query:
        raise StravaError(f"Strava returned an error: {query['error'][0]}")
    if query.get('state', [''])[0] != expected_state:
        raise StravaError('State mismatch: paste the URL from this login attempt.')
    granted = set(query.get('scope', [''])[0].split(','))
    if SCOPE not in granted:
        raise StravaError(
            f"Strava granted {sorted(granted)}, not {SCOPE}. Log in again and leave "
            '"View data about your private activities" ticked.'
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

    async def activity(self, activity_id: int) -> dict | None:
        """GET /activities/{id}; None when Strava says it does not exist."""
        for attempt in (0, 1):
            token = await self.tokens.access_token(self._http, force=attempt == 1)
            response = await self._http.get(
                f"{API}/activities/{activity_id}",
                headers={'Authorization': f"Bearer {token}"},
            )
            if response.status_code == 401 and attempt == 0:
                continue
            break
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise StravaError(f"Strava GET activity failed ({response.status_code}): {response.text[:300]}")
        return response.json()

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
        self.s.log.set_strava_event(key, result['status'], result)
        return result

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
