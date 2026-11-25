import json
import stat

import httpx
import pytest

from spotify_mcp import spotify_auth
from spotify_mcp.spotify_auth import AuthError, TokenStore


def test_pkce_challenge_matches_rfc_example():
    # RFC 7636 appendix B
    assert (
        spotify_auth.challenge_for('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk')
        == 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'
    )


def test_authorize_url_has_pkce_and_scopes_without_playback():
    url = spotify_auth.build_authorize_url('cid', 'http://127.0.0.1:8888/callback', 'ch', 'st')
    assert 'code_challenge_method=S256' in url and 'state=st' in url
    assert 'playlist-modify-private' in url
    assert 'streaming' not in url and 'user-modify-playback-state' not in url


def test_code_from_redirect_checks_state():
    assert spotify_auth.code_from_redirect('http://127.0.0.1:8888/callback?code=abc&state=s', 's') == 'abc'
    with pytest.raises(AuthError, match='State mismatch'):
        spotify_auth.code_from_redirect('http://127.0.0.1:8888/callback?code=abc&state=x', 's')
    with pytest.raises(AuthError, match='access_denied'):
        spotify_auth.code_from_redirect('http://127.0.0.1:8888/callback?error=access_denied&state=s', 's')


def test_token_file_is_private_and_keeps_refresh_token(tmp_path):
    store = TokenStore(tmp_path / 'token.json', 'cid')
    store.save_response({'access_token': 'a', 'refresh_token': 'r', 'expires_in': 10})
    assert stat.S_IMODE((tmp_path / 'token.json').stat().st_mode) == 0o600
    # A refresh response without refresh_token keeps the old one.
    store.save_response({'access_token': 'b', 'expires_in': 10})
    assert json.loads((tmp_path / 'token.json').read_text())['refresh_token'] == 'r'


def test_loosened_token_file_is_made_private_again(tmp_path):
    path = tmp_path / 'token.json'
    TokenStore(path, 'cid').save_response({'access_token': 'a', 'refresh_token': 'r'})
    path.chmod(0o660)  # what kubelet's fsGroup pass leaves at pod start
    store = TokenStore(path, 'cid')
    store._load()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_expired_token_is_refreshed(tmp_path, recorder):
    store = TokenStore(tmp_path / 'token.json', 'cid')
    store.save_response({'access_token': 'old', 'refresh_token': 'r', 'expires_in': 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        assert await store.access_token(http) == 'new-access'
    assert recorder.calls[0][1] == '/api/token'


async def test_missing_token_file_says_how_to_log_in(tmp_path):
    store = TokenStore(tmp_path / 'none.json', 'cid')
    async with httpx.AsyncClient() as http:
        with pytest.raises(AuthError, match='spotify-mcp-auth'):
            await store.access_token(http)
