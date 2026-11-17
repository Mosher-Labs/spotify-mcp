import time

from spotify_mcp import tools
from spotify_mcp.tempo import TempoCache

from conftest import IDS


def service_calls(recorder):
    return [c for c in recorder.calls if c[1] == '/bpm']


async def test_reccobeats_miss_falls_back_by_name(fallback_services, recorder):
    recorder.tempos = {IDS[0]: 150}
    recorder.service_bpm = {'Song 2': 172.3}
    got = await tools.track_features(fallback_services, [IDS[0], IDS[1]])
    assert got['features'][IDS[0]]['source'] == 'reccobeats'
    f = got['features'][IDS[1]]
    assert (f['tempo'], f['source'], f['trust'], f['energy']) == (172.3, 'tempo-service', 'high', None)
    # The name came from a single-track lookup, then the result is cached.
    assert ('GET', f"/v1/tracks/{IDS[1]}", None) in recorder.calls
    await tools.track_features(fallback_services, [IDS[1]])
    assert len(service_calls(recorder)) == 1


async def test_fallback_is_capped_per_call_and_resumes(fallback_services, recorder):
    recorder.service_bpm = {'Song 1': 150, 'Song 2': 160, 'Song 3': 170}
    first = await tools.track_features(fallback_services, IDS[:3])
    assert first['pending'] == [IDS[2]]
    second = await tools.track_features(fallback_services, IDS[:3])
    assert second['pending'] == [] and second['features'][IDS[2]]['tempo'] == 170


async def test_service_404_is_a_miss_and_not_asked_again(fallback_services, recorder):
    await tools.track_features(fallback_services, [IDS[0]])
    again = await tools.track_features(fallback_services, [IDS[0]])
    assert again == {'features': {IDS[0]: None}, 'pending': []}
    assert len(service_calls(recorder)) == 1


async def test_service_down_leaves_tracks_pending(fallback_services, recorder):
    recorder.service_status = 503
    got = await tools.track_features(fallback_services, IDS[:2])
    assert got['pending'] == IDS[:2]
    assert len(service_calls(recorder)) == 1  # stops after the first failure
    recorder.service_status = None
    recorder.service_bpm = {'Song 1': 150, 'Song 2': 160}
    got = await tools.track_features(fallback_services, IDS[:2])
    assert got['pending'] == []


async def test_reccobeats_error_leaves_tracks_pending(fallback_services, recorder):
    recorder.reccobeats_status = 502
    got = await tools.track_features(fallback_services, IDS[:2])
    assert got == {'features': {IDS[0]: None, IDS[1]: None}, 'pending': IDS[:2]}
    assert service_calls(recorder) == []  # unknown to ReccoBeats is not the same as unanswered
    recorder.reccobeats_status = None
    recorder.tempos = {IDS[0]: 150, IDS[1]: 160}
    got = await tools.track_features(fallback_services, IDS[:2])
    assert got['pending'] == [] and got['features'][IDS[1]]['tempo'] == 160


async def test_reccobeats_error_without_fallback_is_not_a_miss(services, recorder):
    recorder.reccobeats_status = 503
    got = await tools.track_features(services, [IDS[0]])
    assert got['pending'] == [IDS[0]]


async def test_fallback_stops_when_out_of_time(tmp_path, services, recorder):
    services.tempo = TempoCache(tmp_path / 'b.sqlite3', 'http://tempo.test', fallback_budget=0)
    recorder.service_bpm = {'Song 1': 150}
    got = await tools.track_features(services, [IDS[0]])
    assert got['pending'] == [IDS[0]] and service_calls(recorder) == []


async def test_misses_from_before_the_fallback_get_one_try(tmp_path, services, recorder):
    path = tmp_path / 'old.sqlite3'
    old = TempoCache(path)  # no fallback: a ReccoBeats miss is final for a week
    await old.get_many(services.http, [IDS[0]])
    assert old.pending([IDS[0]]) == []
    recorder.service_bpm = {'Song 1': 88}
    services.tempo = TempoCache(path, 'http://tempo.test')
    got = await tools.track_features(services, [IDS[0]])
    assert got['features'][IDS[0]]['tempo'] == 88


async def test_candidates_use_pool_names_and_skip_energy_filter(fallback_services, recorder):
    recorder.top = IDS[:2]
    recorder.tempos = {IDS[0]: 172}
    recorder.service_bpm = {'Song 2': 86}
    result = await tools.candidate_tracks(fallback_services, min_bpm=165, max_bpm=180, half_double_time=True)
    got = {m['id']: (m['fits_at_bpm'], m['tempo_source'], m['tempo_trust']) for m in result['matches']}
    assert got == {IDS[0]: (172, 'reccobeats', None), IDS[1]: (172.0, 'tempo-service', 'check')}
    assert result['tempo_lookups_pending'] == 0
    # No name lookups needed: the pool already had artist and title.
    assert not any(p.startswith('/v1/tracks/') for _, p, _ in recorder.calls)
    filtered = await tools.candidate_tracks(
        fallback_services, min_bpm=165, max_bpm=180, half_double_time=True, min_energy=0.2
    )
    assert {m['id'] for m in filtered['matches']} == {IDS[0]}


def test_old_schema_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / 'legacy.sqlite3'
    db = sqlite3.connect(path)
    db.execute(
        'CREATE TABLE features (track_id TEXT PRIMARY KEY, tempo REAL, energy REAL, danceability REAL,'
        'valence REAL, loudness REAL, manual INTEGER NOT NULL DEFAULT 0, fetched_at REAL NOT NULL)'
    )
    db.execute('INSERT INTO features VALUES (?,?,?,?,?,?,0,?)', (IDS[0], 120, 0.7, 0.5, 0.5, -6, time.time()))
    db.commit()
    db.close()
    cache = TempoCache(path, 'http://tempo.test')
    assert cache.peek(IDS[0])['source'] == 'reccobeats'
    cache.set_manual(IDS[0], 125)
    assert cache.peek(IDS[0])['energy'] == 0.7 and cache.peek(IDS[0])['source'] == 'manual'
