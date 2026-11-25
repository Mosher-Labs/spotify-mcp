import json

import httpx
import pytest

from spotify_mcp import tools
from spotify_mcp.spotify_api import SpotifyClient
from spotify_mcp.spotify_auth import TokenStore
from spotify_mcp.history import ListeningLog
from spotify_mcp.tempo import TempoCache

IDS = [f"{i:022d}" for i in range(1, 8)]


class Recorder:
    """Fake Spotify and ReccoBeats; records every request."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.tempos: dict[str, float] = {}
        self.top: list[str] = []
        self.status_overrides: list[int] = []
        self.recent: list[tuple[str, str, int]] = []  # (track_id, played_at, duration_ms)
        self.playlists = 0
        self.service_bpm: dict[str, float | None] = {}  # title -> bpm; None means 404
        self.service_status: int | None = None
        self.reccobeats_status: int | None = None
        self.context: str | None = None  # context URI on every recently-played item

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = (
            json.loads(request.content)
            if request.content and request.headers.get('content-type', '').startswith('application/json')
            else None
        )
        self.calls.append((request.method, request.url.path, body))
        host = request.url.host
        if host == 'api.reccobeats.com':
            if self.reccobeats_status:
                return httpx.Response(self.reccobeats_status, text='x')
            ids = request.url.params['ids'].split(',')
            content = [
                {
                    'href': f"https://open.spotify.com/track/{i}",
                    'tempo': self.tempos[i],
                    'energy': 0.5 + self.tempos[i] / 1000,
                    'danceability': 0.5,
                    'valence': 0.5,
                    'loudness': -6,
                }
                for i in ids
                if i in self.tempos
            ]
            return httpx.Response(200, json={'content': content})
        if host == 'tempo.test':
            if self.service_status:
                return httpx.Response(self.service_status, text='x')
            bpm = self.service_bpm.get(request.url.params['title'])
            if bpm is None:
                return httpx.Response(404, json={'detail': 'no iTunes preview found for that track'})
            return httpx.Response(200, json={'bpm': bpm, 'trust': 'high' if bpm > 100 else 'check'})
        if host == 'accounts.spotify.com':
            return httpx.Response(
                200, json={'access_token': 'new-access', 'expires_in': 3600}
            )
        if self.status_overrides:
            return httpx.Response(self.status_overrides.pop(0), text='x')
        path = request.url.path
        if path == '/v1/me/top/tracks':
            return httpx.Response(200, json={'items': [track(i) for i in self.top], 'next': None})
        if path.startswith('/v1/tracks/'):
            return httpx.Response(200, json=track(path.rsplit('/', 1)[1]))
        if path == '/v1/me/player/recently-played':
            items = [
                {'played_at': at, 'track': {**track(tid), 'duration_ms': dur}, 'context': {'uri': self.context} if self.context else None}
                for tid, at, dur in self.recent
            ]
            return httpx.Response(200, json={'items': items})
        if path == '/v1/me/playlists' and request.method == 'POST':
            self.playlists += 1
            pid = f"PL{self.playlists}"
            return httpx.Response(
                201, json={'id': pid, 'name': body['name'], 'external_urls': {'spotify': f"https://open.spotify.com/playlist/{pid}"}}
            )
        return httpx.Response(200, json={'snapshot_id': 'snap'})


def track(tid: str) -> dict:
    return {
        'id': tid,
        'uri': f"spotify:track:{tid}",
        'name': f"Song {tid[-1]}",
        'artists': [{'name': 'Artist'}],
        'album': {'name': 'Album'},
        'duration_ms': 200000,
        'explicit': False,
    }


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
async def services(tmp_path, recorder):
    store = TokenStore(tmp_path / 'token.json', 'cid')
    store.save_response({'access_token': 'tok', 'refresh_token': 'ref', 'expires_in': 3600})
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    yield tools.Services(
        spotify=SpotifyClient(store, http), tempo=TempoCache(tmp_path / 't.sqlite3'), http=http,
        log=ListeningLog(tmp_path / 'l.sqlite3'),
    )
    await http.aclose()


@pytest.fixture
async def fallback_services(services, tmp_path):
    """services with the tempo-service fallback on, two lookups per call."""
    services.tempo = TempoCache(tmp_path / 'f.sqlite3', 'http://tempo.test', fallback_limit=2)
    return services
