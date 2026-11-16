"""One-time Spotify login: ``spotify-mcp-auth``.

Run it inside the pod (RUNBOOK.md). It prints a URL to open in any browser.
After you approve, the browser is sent to the redirect URI, which nothing
serves, so the page fails to load. Copy that address from the browser's
address bar and paste it here.
"""

import asyncio
import secrets

import httpx

from spotify_mcp import spotify_auth
from spotify_mcp.config import load_settings


async def _login() -> None:
    settings = load_settings()
    verifier = spotify_auth.new_verifier()
    state = secrets.token_urlsafe(16)
    url = spotify_auth.build_authorize_url(
        settings.client_id,
        settings.redirect_uri,
        spotify_auth.challenge_for(verifier),
        state,
    )
    print('Open this URL in a browser and approve access:\n')
    print(url)
    print(
        f"\nThe browser will then fail to load {settings.redirect_uri}. "
        'That is expected. Paste the full address from the address bar:\n'
    )
    code = spotify_auth.code_from_redirect(input('> '), state)
    async with httpx.AsyncClient(timeout=30) as http:
        payload = await spotify_auth.exchange_code(
            http, settings.client_id, settings.redirect_uri, code, verifier
        )
        store = spotify_auth.TokenStore(settings.token_path, settings.client_id)
        store.save_response(payload)
        me = await http.get(
            'https://api.spotify.com/v1/me',
            headers={'Authorization': f"Bearer {payload['access_token']}"},
        )
    name = me.json().get('display_name', '?') if me.status_code == 200 else '?'
    print(f"Logged in as {name}. Token saved to {settings.token_path}.")
    print('Restart the deployment so the server picks it up.')


def main() -> None:
    asyncio.run(_login())


if __name__ == '__main__':
    main()
