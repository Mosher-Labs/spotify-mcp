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
    'add_tracks',
    'replace_playlist_tracks',
    'reorder_tracks',
    'remove_tracks',
    'log_workout_listening',
    'listening_status',
    'get_track_history',
    'get_workout_music_stats',
)


def build_server(services, tokens: TokenStore, host: str, port: int, stateless: bool) -> FastMCP:
    mcp = FastMCP('Spotify', host=host, port=port, stateless_http=stateless)
    for name in TOOL_NAMES:
        fn = getattr(tools, name)
        mcp.tool(name=name, description=(fn.__doc__ or '').strip() or None)(_bound(fn, services))

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
        tempo=TempoCache(settings.tempo_db_path),
        http=http,
        log=ListeningLog(settings.log_db_path),
    )
    # Spotify keeps only the last 50 plays; copy them out every few minutes.
    interval = int(os.getenv('SPOTIFY_MCP_POLL_SECONDS', '600'))
    start_poller(settings, interval)
    mcp = build_server(services, tokens, host, port, stateless)
    if token:
        http_auth.install_http_bearer_auth(mcp, token)
    print(f"Serving Spotify MCP on {host}:{port} (stateless={stateless})", file=sys.stderr)
    mcp.run(transport='streamable-http')


if __name__ == '__main__':
    main()
