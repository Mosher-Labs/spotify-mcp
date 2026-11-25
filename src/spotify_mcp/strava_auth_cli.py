"""One-time Strava login: ``spotify-mcp-strava-auth``.

Run it inside the pod (RUNBOOK.md). It prints a URL to open in any browser.
After you approve, Strava sends the browser to http://localhost/..., which
nothing serves, so the page fails to load. Copy that address from the address
bar and paste it here.
"""

import asyncio
import os
import secrets

import httpx

from spotify_mcp import strava
from spotify_mcp.config import load_settings


async def _login() -> None:
    settings = strava.load_strava_settings(load_settings().data_dir)
    if settings is None:
        raise SystemExit(
            'Set STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET and STRAVA_WEBHOOK_VERIFY_TOKEN first.'
        )
    redirect_uri = os.getenv('STRAVA_REDIRECT_URI', strava.DEFAULT_REDIRECT_URI).strip()
    state = secrets.token_urlsafe(16)
    print('Open this URL in a browser and approve access:\n')
    print(strava.build_authorize_url(settings.client_id, redirect_uri, state))
    print(
        f"\nThe browser will then fail to load {redirect_uri}. "
        'That is expected. Paste the full address from the address bar:\n'
    )
    code = strava.code_from_redirect(input('> '), state)
    store = strava.StravaTokenStore(settings)
    async with httpx.AsyncClient(timeout=30) as http:
        payload = await store.exchange_code(http, code)
    athlete = payload.get('athlete') or {}
    print(
        f"Logged in to Strava as {athlete.get('firstname', '?')} (athlete {athlete.get('id')}). "
        f"Token saved to {settings.token_path}."
    )


def main() -> None:
    asyncio.run(_login())


if __name__ == '__main__':
    main()
