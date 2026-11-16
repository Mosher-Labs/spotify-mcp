import httpx
import pytest

from spotify_mcp import http_auth, server
from spotify_mcp.spotify_auth import TokenStore


async def test_tools_registered_without_services_parameter(services, tmp_path):
    mcp = server.build_server(services, TokenStore(tmp_path / 't.json', 'c'), '127.0.0.1', 8000, True)
    listed = {t.name: t for t in await mcp.list_tools()}
    assert set(listed) == set(server.TOOL_NAMES)
    for tool in listed.values():
        assert 's' not in tool.inputSchema.get('properties', {}), tool.name
        assert 'services' not in tool.inputSchema.get('properties', {})
    assert 'playlist_id' in listed['add_tracks'].inputSchema['properties']


async def test_gate_accepts_x_api_key_and_probes_are_open(services, tmp_path):
    tokens = TokenStore(tmp_path / 'missing.json', 'c')
    mcp = server.build_server(services, tokens, '127.0.0.1', 8000, True)
    http_auth.install_http_bearer_auth(mcp, 's3cret')
    app = mcp.streamable_http_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
        assert (await c.get('/healthz')).status_code == 200
        assert (await c.get('/readyz')).status_code == 503  # no token file yet
        assert (await c.post('/mcp', json={})).status_code == 401
        assert (await c.post('/mcp', json={}, headers={'X-API-Key': 'wrong'})).status_code == 401
        ok = await c.post('/mcp', json={}, headers={'X-API-Key': 's3cret'})
        # Past the gate; the MCP session manager isn't running in this test, so 500.
        assert ok.status_code == 500


def test_refuses_to_serve_beyond_loopback_without_token(monkeypatch, tmp_path):
    monkeypatch.setenv('SPOTIFY_CLIENT_ID', 'cid')
    monkeypatch.setenv('SPOTIFY_MCP_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('SPOTIFY_MCP_HOST', '0.0.0.0')
    monkeypatch.delenv('SPOTIFY_MCP_HTTP_TOKEN', raising=False)
    with pytest.raises(SystemExit, match='SPOTIFY_MCP_HTTP_TOKEN'):
        server.main()
