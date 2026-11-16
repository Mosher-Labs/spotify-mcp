import pytest

from spotify_mcp import tools
from spotify_mcp.spotify_api import SpotifyError, track_uri
from spotify_mcp.tempo import matching_bpm

from conftest import IDS


def test_track_uri_accepts_ids_and_uris_and_rejects_junk():
    assert track_uri(IDS[0]) == f"spotify:track:{IDS[0]}"
    assert track_uri(f"spotify:track:{IDS[0]}") == f"spotify:track:{IDS[0]}"
    for bad in ('spotify:album:' + IDS[0], 'short', 'x' * 23, 'spotify:track:../etc'):
        with pytest.raises(ValueError):
            track_uri(bad)


def test_matching_bpm_half_and_double_time():
    assert matching_bpm(172, 165, 180, False) == 172
    assert matching_bpm(86, 165, 180, False) is None
    assert matching_bpm(86, 165, 180, True) == 172.0
    assert matching_bpm(340, 165, 180, True) == 170.0


async def test_search_limit_is_capped_at_ten(services, recorder):
    await tools.search_tracks(services, 'run', limit=50)
    assert recorder.calls[-1][1] == '/v1/search'


async def test_create_playlist_uses_me_endpoint_and_items_path(services, recorder):
    result = await tools.create_playlist(services, 'Tempo', track_ids=IDS[:2])
    assert result['id'] == 'PL1' and result['tracks_added'] == 2
    paths = [(m, p) for m, p, _ in recorder.calls]
    assert ('POST', '/v1/me/playlists') in paths
    assert ('POST', '/v1/playlists/PL1/items') in paths
    assert not any(p.endswith('/tracks') or '/users/' in p for _, p in paths)


async def test_private_by_default(services, recorder):
    await tools.create_playlist(services, 'Tempo')
    body = next(b for m, p, b in recorder.calls if p == '/v1/me/playlists')
    assert body['public'] is False


async def test_add_tracks_chunks_of_100(services, recorder):
    ids = [f"{i:022d}" for i in range(250)]
    await tools.add_tracks(services, 'PL1', ids)
    posts = [b for m, p, b in recorder.calls if p == '/v1/playlists/PL1/items']
    assert [len(b['uris']) for b in posts] == [100, 100, 50]


async def test_replace_puts_first_chunk_then_appends(services, recorder):
    ids = [f"{i:022d}" for i in range(120)]
    await tools.replace_playlist_tracks(services, 'PL1', ids)
    methods = [(m, len(b['uris'])) for m, p, b in recorder.calls if p == '/v1/playlists/PL1/items']
    assert methods == [('PUT', 100), ('POST', 20)]


async def test_reorder_sends_range(services, recorder):
    await tools.reorder_tracks(services, 'PL1', 3, 0, 2)
    assert recorder.calls[-1] == ('PUT', '/v1/playlists/PL1/items', {'range_start': 3, 'insert_before': 0, 'range_length': 2})


async def test_bad_uri_never_reaches_spotify(services, recorder):
    with pytest.raises(ValueError):
        await tools.add_tracks(services, 'PL1', ['nope'])
    assert recorder.calls == []


async def test_candidate_tracks_filters_by_bpm_and_energy(services, recorder):
    recorder.top = IDS
    recorder.tempos = {IDS[0]: 172, IDS[1]: 86, IDS[2]: 120, IDS[3]: 175}
    result = await tools.candidate_tracks(services, min_bpm=165, max_bpm=180, half_double_time=True)
    got = {m['id']: m['fits_at_bpm'] for m in result['matches']}
    assert got == {IDS[0]: 172, IDS[1]: 172.0, IDS[3]: 175}
    assert result['no_tempo_data'] == 3  # IDS[4:7] unknown
    strict = await tools.candidate_tracks(services, min_bpm=165, max_bpm=180)
    assert {m['id'] for m in strict['matches']} == {IDS[0], IDS[3]}


async def test_tempo_is_cached_and_misses_not_refetched(services, recorder):
    recorder.tempos = {IDS[0]: 150}
    await tools.track_features(services, [IDS[0], IDS[1]])
    first = len([c for c in recorder.calls if c[1].endswith('audio-features')])
    again = await tools.track_features(services, [IDS[0], IDS[1]])
    assert len([c for c in recorder.calls if c[1].endswith('audio-features')]) == first
    assert again['features'][IDS[0]]['tempo'] == 150 and again['features'][IDS[1]] is None
    assert again['pending'] == []


async def test_manual_tempo_beats_missing_and_survives(services, recorder):
    await tools.track_features(services, [IDS[1]])  # recorded as a miss
    await tools.set_track_tempo(services, IDS[1], 168, 0.9)
    got = (await tools.track_features(services, [IDS[1]]))['features']
    assert got[IDS[1]]['tempo'] == 168 and got[IDS[1]]['manual'] is True
    assert got[IDS[1]]['source'] == 'manual'
    with pytest.raises(ValueError):
        await tools.set_track_tempo(services, IDS[1], 10)


async def test_403_and_429_have_actionable_messages(services, recorder):
    recorder.status_overrides = [403]
    with pytest.raises(SpotifyError, match='allowlist'):
        await tools.saved_tracks(services)
    recorder.status_overrides = [429]
    with pytest.raises(SpotifyError, match='rate limit'):
        await tools.saved_tracks(services)


async def test_401_triggers_one_refresh_then_retry(services, recorder):
    recorder.status_overrides = [401]
    await tools.saved_tracks(services)
    hosts = [p for _, p, _ in recorder.calls]
    assert '/api/token' in hosts and hosts[-1] == '/v1/me/tracks'
