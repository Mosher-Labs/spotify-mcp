"""Background poller: copies Spotify's last 50 plays into the listening log."""

import asyncio
import sys
import threading
import time

import httpx

from spotify_mcp.history import ListeningLog
from spotify_mcp.spotify_api import SpotifyClient
from spotify_mcp.spotify_auth import TokenStore


async def poll_once(spotify: SpotifyClient, log: ListeningLog) -> int:
    """Fetch the latest 50 plays and store the new ones. Records success or error."""
    try:
        body = await spotify.request('GET', '/me/player/recently-played', params={'limit': 50})
        new = log.add_plays(body.get('items', []))
        log.set_meta('last_poll_ok', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        log.set_meta('last_poll_error', '')
        # Settle which reading of played_at fits the data once there is enough of it.
        better = log.timestamp_fit()['better_fit']
        if better:
            log.set_meta('played_at_semantics', better)
        return new
    except Exception as exc:  # keep polling; the status tool shows the last error
        log.set_meta('last_poll_error', f"{type(exc).__name__}: {str(exc)[:200]}")
        raise


async def _loop(settings, interval: int) -> None:
    log = ListeningLog(settings.log_db_path)
    tokens = TokenStore(settings.token_path, settings.client_id)
    async with httpx.AsyncClient(timeout=30) as http:
        spotify = SpotifyClient(tokens, http)
        while True:
            if tokens.exists():
                try:
                    new = await poll_once(spotify, log)
                    print(f"poller: {new} new plays", file=sys.stderr)
                except Exception as exc:
                    print(f"poller: {type(exc).__name__}: {exc}", file=sys.stderr)
            await asyncio.sleep(interval)


def start_poller(settings, interval: int) -> threading.Thread | None:
    """Run the poll loop on its own thread and event loop. interval 0 disables it."""
    if interval <= 0:
        return None
    thread = threading.Thread(
        target=lambda: asyncio.run(_loop(settings, interval)), name='play-poller', daemon=True
    )
    thread.start()
    return thread
