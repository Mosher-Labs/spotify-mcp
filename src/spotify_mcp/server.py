"""FastMCP server: tool registration, HTTP transport, probes."""

import os
import sys
from functools import wraps

import httpx
from mcp.server.fastmcp import FastMCP

from spotify_mcp import http_auth, tools
from spotify_mcp.config import load_settings
from spotify_mcp.history import ListeningLog
from spotify_mcp.poller import start_poller
from spotify_mcp.spotify_api import SpotifyClient
from spotify_mcp.spotify_auth import TokenStore
from spotify_mcp.strava import (
    StravaClient,
    StravaTokenStore,
    WebhookProcessor,
    load_strava_settings,
    set_strava_description,
)
from spotify_mcp.tempo import TempoCache


def _bound(fn, services):
    """Expose tools.fn(services, ...) as fn(...) to MCP, keeping its signature."""

    @wraps(fn)
    async def call(*args, **kwargs):
        return await fn(services, *args, **kwargs)

    # FastMCP builds the schema from the signature; drop the services parameter.
    import inspect

    sig = inspect.signature(fn)
    call.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])  # type: ignore[attr-defined]
    return call


TOOL_NAMES = (
    'search_tracks',
    'top_tracks',
    'saved_tracks',
    'track_features',
    'set_track_tempo',
    'candidate_tracks',
    'my_playlists',
    'playlist_items',
    'create_playlist',
    'rename_playlist',
    'add_tracks',
    'replace_playlist_tracks',
    'reorder_tracks',
    'remove_tracks',
    'log_workout_listening',
    'listening_status',
    'get_track_history',
    'get_workout_music_stats',
)
# Registered only when the Strava variables are set.
STRAVA_TOOL_NAMES = ('set_strava_description',)


def build_server(
    services,
    tokens: TokenStore,
    host: str,
    port: int,
    stateless: bool,
    strava: WebhookProcessor | None = None,
) -> FastMCP:
    mcp = FastMCP('Spotify', host=host, port=port, stateless_http=stateless)
    for name in TOOL_NAMES:
        fn = getattr(tools, name)
        mcp.tool(name=name, description=(fn.__doc__ or '').strip() or None)(_bound(fn, services))
    if strava is not None:
        fn = set_strava_description
        mcp.tool(name=fn.__name__, description=(fn.__doc__ or '').strip())(_bound(fn, strava.client))

    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse, PlainTextResponse

    @mcp.custom_route('/healthz', methods=['GET'])
    async def healthz(_request):
        return PlainTextResponse('ok')

    @mcp.custom_route('/readyz', methods=['GET'])
    async def readyz(_request):
        if tokens.exists():
            return JSONResponse({'status': 'ready'})
        return JSONResponse(
            {'status': 'not logged in', 'detail': 'run spotify-mcp-auth'}, status_code=503
        )

    # Strava push subscription. http_auth leaves this path open because
    # Strava cannot send our token; see strava.py for how events are checked.
    @mcp.custom_route(http_auth.STRAVA_WEBHOOK_PATH, methods=['GET'])
    async def strava_validate(request):
        if strava is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        params = request.query_params
        if params.get('hub.mode') != 'subscribe' or not strava.verify_token_ok(
            params.get('hub.verify_token', '')
        ):
            return JSONResponse({'error': 'forbidden'}, status_code=403)
        return JSONResponse({'hub.challenge': params.get('hub.challenge', '')})

    @mcp.custom_route(http_auth.STRAVA_WEBHOOK_PATH, methods=['POST'])
    async def strava_event(request):
        if strava is None:
            return JSONResponse({'error': 'not found'}, status_code=404)
        try:
            event = await request.json()
        except ValueError:
            return JSONResponse({'error': 'bad json'}, status_code=400)
        if not isinstance(event, dict):
            return JSONResponse({'error': 'bad json'}, status_code=400)
        # Strava wants a 200 within two seconds and retries otherwise, so the
        # Strava and Spotify calls run after the response is sent. Ignored
        # events still get a 200 so Strava does not retry them.
        reason = strava.quick_check(event)
        if reason:
            return JSONResponse({'status': 'ignored'})
        return JSONResponse({'status': 'accepted'}, background=BackgroundTask(strava.handle, event))

    return mcp


def main() -> None:
    settings = load_settings()
    host = os.getenv('SPOTIFY_MCP_HOST', '127.0.0.1')
    port = int(os.getenv('SPOTIFY_MCP_PORT', '8000'))
    stateless = os.getenv('SPOTIFY_MCP_STATELESS_HTTP', 'true').lower() in ('1', 'true', 'yes')

    token = http_auth.resolve_http_token()
    if token is None and not http_auth.is_loopback(host):
        # Unlike garmin-mcp this refuses rather than warns: the server can write
        # to the Spotify account.
        raise SystemExit(http_auth.exposure_warning(host, port))

    http = httpx.AsyncClient(timeout=30)
    tokens = TokenStore(settings.token_path, settings.client_id)
    services = tools.Services(
        spotify=SpotifyClient(tokens, http),
        tempo=TempoCache(
            settings.tempo_db_path,
            # In-cluster tempo-service: BPM from iTunes previews when ReccoBeats misses.
            fallback_url=os.getenv('TEMPO_SERVICE_URL') or None,
            fallback_limit=int(os.getenv('TEMPO_SERVICE_MAX_PER_CALL', '5')),
        ),
        http=http,
        log=ListeningLog(settings.log_db_path),
    )
    # Plays are fetched when a workout is logged (tool call or Strava
    # webhook). The background poll is an optional safety net against
    # Spotify's 50-play limit; 0 (the default) leaves it off.
    interval = int(os.getenv('SPOTIFY_MCP_POLL_SECONDS', '0'))
    start_poller(settings, interval)
    strava = None
    strava_settings = load_strava_settings(settings.data_dir)
    if strava_settings:
        client = StravaClient(strava_settings, StravaTokenStore(strava_settings), http)
        strava = WebhookProcessor(strava_settings, client, services)
    mcp = build_server(services, tokens, host, port, stateless, strava)
    if token:
        http_auth.install_http_bearer_auth(mcp, token)
    print(
        f"Serving Spotify MCP on {host}:{port} (stateless={stateless}, "
        f"strava_webhook={'on' if strava else 'off'}, poll_seconds={interval})",
        file=sys.stderr,
    )
    mcp.run(transport='streamable-http')


if __name__ == '__main__':
    main()
