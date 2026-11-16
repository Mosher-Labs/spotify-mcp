"""Thin Spotify Web API client (post February 2026 Development Mode shape)."""

import re

import httpx

from spotify_mcp.spotify_auth import TokenStore

API = 'https://api.spotify.com/v1'
_TRACK_ID = re.compile(r'^[A-Za-z0-9]{22}$')
_TRACK_URI = re.compile(r'^spotify:track:([A-Za-z0-9]{22})$')


class SpotifyError(RuntimeError):
    pass


def track_uri(value: str) -> str:
    """Accept a track ID or URI and return the URI; reject anything else."""
    value = value.strip()
    if _TRACK_ID.match(value):
        return f"spotify:track:{value}"
    if _TRACK_URI.match(value):
        return value
    raise ValueError(f"Not a Spotify track ID or URI: {value!r}")


def track_id(value: str) -> str:
    return track_uri(value).rsplit(':', 1)[1]


def slim_track(track: dict) -> dict:
    return {
        'id': track['id'],
        'uri': track['uri'],
        'name': track['name'],
        'artists': [a['name'] for a in track.get('artists', [])],
        'album': (track.get('album') or {}).get('name'),
        'duration_ms': track.get('duration_ms'),
        'explicit': track.get('explicit'),
    }


class SpotifyClient:
    def __init__(self, tokens: TokenStore, http: httpx.AsyncClient):
        self._tokens = tokens
        self._http = http

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        for attempt in (0, 1):
            token = await self._tokens.access_token(self._http, force=attempt == 1)
            response = await self._http.request(
                method,
                API + path,
                params=params,
                json=json,
                headers={'Authorization': f"Bearer {token}"},
            )
            if response.status_code == 401 and attempt == 0:
                continue  # token revoked or clock skew: refresh once and retry
            break
        if response.status_code == 429:
            wait = response.headers.get('Retry-After', 'unknown')
            raise SpotifyError(f"Spotify rate limit hit; retry after {wait} seconds.")
        if response.status_code == 403:
            raise SpotifyError(
                'Spotify returned 403. In Development Mode that means the account is '
                "not on the app's allowlist, or the app owner's Premium lapsed."
            )
        if response.status_code >= 400:
            raise SpotifyError(f"Spotify {method} {path} failed ({response.status_code}): {response.text[:300]}")
        if not response.content:
            return {}
        return response.json()

    async def paged(self, path: str, *, params: dict, total: int, page: int) -> list[dict]:
        """Collect up to `total` items from an offset-paged endpoint."""
        items: list[dict] = []
        offset = 0
        while len(items) < total:
            body = await self.request(
                'GET', path, params={**params, 'limit': min(page, total - len(items)), 'offset': offset}
            )
            batch = body.get('items', [])
            items.extend(batch)
            offset += len(batch)
            if not batch or not body.get('next'):
                break
        return items
