"""Spotify authorization code flow with PKCE, and the token file.

PKCE needs no client secret, so the only credential the cluster holds for
Spotify is the refresh token on the data volume.
"""

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URL = 'https://accounts.spotify.com/authorize'
TOKEN_URL = 'https://accounts.spotify.com/api/token'

# Read top tracks and saved songs; create and edit playlists. Nothing that
# controls playback or reads account details.
SCOPES = (
    'user-top-read',
    'user-library-read',
    'user-read-recently-played',
    'playlist-read-private',
    'playlist-modify-private',
    'playlist-modify-public',
)

# Refresh a little early so a token never expires mid-request.
_EXPIRY_SKEW_SECONDS = 60


class AuthError(RuntimeError):
    pass


def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')


def build_authorize_url(
    client_id: str, redirect_uri: str, challenge: str, state: str
) -> str:
    return AUTH_URL + '?' + urlencode(
        {
            'client_id': client_id,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'scope': ' '.join(SCOPES),
            'code_challenge_method': 'S256',
            'code_challenge': challenge,
            'state': state,
        }
    )


def code_from_redirect(redirected_url: str, expected_state: str) -> str:
    """Pull the authorization code out of the URL the browser was sent to."""
    query = parse_qs(urlparse(redirected_url.strip()).query)
    if 'error' in query:
        raise AuthError(f"Spotify returned an error: {query['error'][0]}")
    if query.get('state', [''])[0] != expected_state:
        raise AuthError('State mismatch: paste the URL from this login attempt.')
    code = query.get('code', [''])[0]
    if not code:
        raise AuthError('No code in that URL.')
    return code


def _now() -> float:
    return time.time()


async def _post_token(client: httpx.AsyncClient, data: dict[str, str]) -> dict:
    response = await client.post(TOKEN_URL, data=data)
    if response.status_code != 200:
        # The body is Spotify's error JSON; it never echoes our credentials.
        raise AuthError(f"Token request failed ({response.status_code}): {response.text[:300]}")
    return response.json()


async def exchange_code(
    client: httpx.AsyncClient,
    client_id: str,
    redirect_uri: str,
    code: str,
    verifier: str,
) -> dict:
    return await _post_token(
        client,
        {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
            'client_id': client_id,
            'code_verifier': verifier,
        },
    )


class TokenStore:
    """The access and refresh token, persisted as JSON with mode 0600."""

    def __init__(self, path: Path, client_id: str):
        self._path = path
        self._client_id = client_id
        self._data: dict | None = None

    def exists(self) -> bool:
        return self._path.is_file()

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self._path.read_text())
            except FileNotFoundError:
                raise AuthError(
                    'Not logged in to Spotify. Run spotify-mcp-auth (see RUNBOOK.md).'
                ) from None
            self._restrict_mode()
        return self._data

    def _restrict_mode(self) -> None:
        # save_response writes 0600, but something outside the app can loosen
        # it later: kubelet's fsGroup pass adds group read/write to every file
        # on the volume when the pod starts. Put it back on each read.
        if os.stat(self._path).st_mode & 0o077:
            os.chmod(self._path, 0o600)

    def save_response(self, payload: dict) -> None:
        """Store a token response. Spotify may omit refresh_token on refresh."""
        previous = {}
        try:
            previous = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError):
            pass
        refresh = payload.get('refresh_token') or previous.get('refresh_token')
        if not refresh:
            raise AuthError('Spotify returned no refresh token.')
        self._data = {
            'access_token': payload['access_token'],
            'refresh_token': refresh,
            'expires_at': _now() + int(payload.get('expires_in', 3600)),
            'scope': payload.get('scope', previous.get('scope', '')),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix='.token-')
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(self._data, handle)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            os.unlink(tmp)
            raise

    async def access_token(self, client: httpx.AsyncClient, force: bool = False) -> str:
        data = self._load()
        if not force and data['expires_at'] - _EXPIRY_SKEW_SECONDS > _now():
            return data['access_token']
        # The poller thread has its own store on the same file; pick up its
        # refresh before doing another.
        self._data = None
        data = self._load()
        if not force and data['expires_at'] - _EXPIRY_SKEW_SECONDS > _now():
            return data['access_token']
        payload = await _post_token(
            client,
            {
                'grant_type': 'refresh_token',
                'refresh_token': data['refresh_token'],
                'client_id': self._client_id,
            },
        )
        self.save_response(payload)
        return self._data['access_token']  # type: ignore[index]
