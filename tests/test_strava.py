import dataclasses
import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from spotify_mcp import http_auth, server, strava
from spotify_mcp.spotify_auth import TokenStore

from conftest import IDS

T0 = datetime(2026, 11, 14, 15, 0, 0, tzinfo=timezone.utc)
ATHLETE = 134815
SUB = 120475
AID = 1360128428


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


class FakeStrava:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.activities: dict[int, dict] = {}
        self.subs = [{'id': SUB}]
        self.laps: dict[int, list[dict]] = {}
        self.streams: dict[int, dict] = {}
        self.puts: list[tuple[int, dict]] = []
        self.put_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == '/oauth/token':
            return httpx.Response(200, json={
                'access_token': 'fresh', 'refresh_token': 'rotated',
                'expires_at': int(time.time()) + 21600, 'athlete': {'id': ATHLETE}})
        if path == '/api/v3/push_subscriptions':
            return httpx.Response(200, json=self.subs)
        if path.startswith('/api/v3/activities/'):
            parts = path.split('/')
            aid = int(parts[4])
            if aid not in self.activities:
                return httpx.Response(404, json={'message': 'Record Not Found'})
            if path.endswith('/laps'):
                return httpx.Response(200, json=self.laps.get(aid, []))
            if path.endswith('/streams'):
                assert request.url.params['key_by_type'] == 'true'
                return httpx.Response(200, json=self.streams.get(aid, {}))
            if request.method == 'PUT':
                body = json.loads(request.content)
                self.puts.append((aid, body))
                if self.put_status != 200:
                    return httpx.Response(self.put_status, json={'message': 'Authorization Error'})
                self.activities[aid] = {**self.activities[aid], **body}
            return httpx.Response(200, json=self.activities[aid])
        return httpx.Response(500)


def activity(aid=AID, start=T0, minutes=30, name='Morning Run', sport='Run'):
    return {'id': aid, 'name': name, 'sport_type': sport, 'type': sport,
            'start_date': start.isoformat().replace('+00:00', 'Z'),
            'elapsed_time': minutes * 60, 'average_speed': 3.0, 'average_heartrate': 150.0}


def event(aspect='create', aid=AID, owner=ATHLETE, sub=SUB, **extra):
    return {'aspect_type': aspect, 'event_time': 1, 'object_id': aid, 'object_type': 'activity',
            'owner_id': owner, 'subscription_id': sub, 'updates': {}, **extra}


@pytest.fixture
def fake():
    return FakeStrava()


@pytest.fixture
async def processor(services, tmp_path, fake):
    settings = strava.StravaSettings('cid', 'secret', 'verify-me', tmp_path / 'strava_token.json')
    tokens = strava.StravaTokenStore(settings)
    tokens.save_response({'access_token': 'a', 'refresh_token': 'r',
                          'expires_at': int(time.time()) + 3600, 'athlete': {'id': ATHLETE}})
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    yield strava.WebhookProcessor(settings, strava.StravaClient(settings, tokens, http), services)
    await http.aclose()


def plays_during(recorder, start_min, count, step=4):
    recorder.recent = [(IDS[i % len(IDS)], at(start_min + step * (i + 1)), 200_000)
                       for i in range(count)]


# --- webhook HTTP surface ---------------------------------------------------

@pytest.fixture
async def client(services, tmp_path, processor):
    mcp = server.build_server(services, TokenStore(tmp_path / 't.json', 'c'),
                              '127.0.0.1', 8000, True, processor)
    http_auth.install_http_bearer_auth(mcp, 's3cret')
    transport = httpx.ASGITransport(app=mcp.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
        yield c


async def test_validation_echoes_challenge_only_with_our_verify_token(client):
    path = http_auth.STRAVA_WEBHOOK_PATH
    ok = await client.get(path, params={'hub.mode': 'subscribe', 'hub.challenge': 'abc',
                                        'hub.verify_token': 'verify-me'})
    assert ok.status_code == 200 and ok.json() == {'hub.challenge': 'abc'}
    bad = await client.get(path, params={'hub.mode': 'subscribe', 'hub.challenge': 'abc',
                                         'hub.verify_token': 'nope'})
    assert bad.status_code == 403


async def test_webhook_path_is_open_but_mcp_still_gated(client):
    assert (await client.post('/mcp', json={})).status_code == 401
    r = await client.post(http_auth.STRAVA_WEBHOOK_PATH, json=event(owner=999))
    assert r.status_code == 200 and r.json() == {'status': 'ignored'}
    assert (await client.post(http_auth.STRAVA_WEBHOOK_PATH, content=b'nope')).status_code == 400


async def test_create_event_logs_workout_after_responding(client, fake, recorder, services):
    fake.activities[AID] = activity()
    plays_during(recorder, 0, 6)
    r = await client.post(http_auth.STRAVA_WEBHOOK_PATH, json=event())
    assert r.status_code == 200 and r.json() == {'status': 'accepted'}
    # ASGITransport waits for the background task, so the work is done now.
    row = services.log.strava_event(str(AID))
    assert row['status'] == 'logged'
    assert json.loads(row['detail'])['plays_matched'] == 6
    summary = services.log.workout_summaries(None, None)[0]
    assert summary['workout_id'] == str(AID) and summary['name'] == 'Morning Run'
    assert summary['workout_type'] == 'Run' and summary['avg_hr'] == 150.0
    assert round(summary['avg_pace_sec_per_km']) == 333


async def test_routes_404_when_strava_not_configured(services, tmp_path):
    mcp = server.build_server(services, TokenStore(tmp_path / 't.json', 'c'), '127.0.0.1', 8000, True)
    transport = httpx.ASGITransport(app=mcp.streamable_http_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
        assert (await c.post(http_auth.STRAVA_WEBHOOK_PATH, json=event())).status_code == 404


# --- verification and idempotency ------------------------------------------

async def test_wrong_owner_is_ignored_before_any_call(processor, fake):
    assert processor.quick_check(event(owner=1)) is not None
    assert processor.quick_check({'object_type': 'activity'}) is not None
    assert processor.quick_check(event()) is None
    assert fake.calls == []


async def test_wrong_subscription_is_ignored(processor, fake, recorder):
    fake.activities[AID] = activity()
    result = await processor.handle(event(sub=1))
    assert result['status'] == 'ignored'
    assert not any(p.startswith('/api/v3/activities') for _, p in fake.calls)
    assert not any(p == '/v1/me/player/recently-played' for _, p, _ in recorder.calls)


async def test_payload_times_are_ignored_activity_comes_from_strava(processor, fake, recorder, services):
    fake.activities[AID] = activity(minutes=10)
    plays_during(recorder, 0, 6)  # plays at 4..24 min; only 4 and 8 are inside 10 min (+5 grace)
    await processor.handle(event(start_date='2020-01-01T00:00:00Z', elapsed_time=999999))
    assert services.log.workout_summaries(None, None)[0]['plays'] == 3


async def test_retries_and_duplicates_do_not_relog(processor, fake, recorder):
    fake.activities[AID] = activity()
    plays_during(recorder, 0, 3)
    assert (await processor.handle(event()))['status'] == 'logged'
    spotify_calls = len(recorder.calls)
    assert (await processor.handle(event()))['status'] == 'duplicate'
    assert len(recorder.calls) == spotify_calls


async def test_failed_create_is_retried_by_next_event(processor, fake, recorder, services):
    fake.activities[AID] = activity()
    plays_during(recorder, 0, 3)
    recorder.status_overrides = [500, 500, 500]
    assert (await processor.handle(event()))['status'] == 'error'
    recorder.status_overrides = []
    assert (await processor.handle(event('update')))['status'] == 'logged'


async def test_update_renames_logged_workout_without_touching_spotify(processor, fake, recorder, services):
    fake.activities[AID] = activity()
    plays_during(recorder, 0, 3)
    await processor.handle(event())
    fake.activities[AID]['name'] = 'Tempo Tuesday'
    spotify_calls = len(recorder.calls)
    assert (await processor.handle(event('update', updates={'title': 'x'})))['status'] == 'renamed'
    assert services.log.workout_summaries(None, None)[0]['name'] == 'Tempo Tuesday'
    assert len(recorder.calls) == spotify_calls


async def test_delete_only_when_strava_confirms(processor, fake, recorder, services):
    fake.activities[AID] = activity()
    plays_during(recorder, 0, 3)
    await processor.handle(event())
    # Still exists (e.g. turned private): keep it.
    assert (await processor.handle(event('delete')))['status'] == 'ignored'
    assert services.log.workout_summaries(None, None)
    del fake.activities[AID]
    assert (await processor.handle(event('delete')))['status'] == 'deleted'
    assert services.log.workout_summaries(None, None) == []
    assert services.log.status()['plays_logged'] == 3  # plays themselves are kept


async def test_deauthorize_is_recorded(processor, services):
    ev = {**event(), 'object_type': 'athlete', 'object_id': ATHLETE, 'aspect_type': 'update',
          'updates': {'authorized': 'false'}}
    assert (await processor.handle(ev))['status'] == 'deauthorized'
    assert services.log.meta('strava_deauthorized_at') == '1'


async def test_subscription_id_from_settings_skips_lookup(processor, fake):
    processor.client._subscription_id = SUB
    fake.activities[AID] = activity()
    await processor.handle(event())
    assert ('GET', '/api/v3/push_subscriptions') not in fake.calls
    # Once known, a wrong subscription is turned away before any work.
    assert processor.quick_check(event(sub=1)) is not None


# --- the 50-play limit ------------------------------------------------------

async def test_possible_gap_flagged_when_50_plays_do_not_reach_the_start(processor, fake, recorder):
    fake.activities[AID] = activity(minutes=120)
    # 50 plays starting 40 minutes into the workout: the first 40 minutes rolled off.
    plays_during(recorder, 36, 50, step=3)
    result = await processor.handle(event())
    assert 'possible_gap' in result


async def test_no_gap_when_earlier_poll_stored_the_start(processor, fake, recorder, services):
    fake.activities[AID] = activity(minutes=120)
    plays_during(recorder, 0, 5)
    services.log.add_plays([{'played_at': at(4), 'track': {'id': IDS[0], 'duration_ms': 200_000}}])
    plays_during(recorder, 36, 50, step=3)
    assert 'possible_gap' not in await processor.handle(event())


async def test_no_gap_when_fewer_than_50(processor, fake, recorder):
    fake.activities[AID] = activity()
    plays_during(recorder, 0, 5)
    assert 'possible_gap' not in await processor.handle(event())


# --- OAuth and token file ---------------------------------------------------

def test_redirect_requires_read_all_and_write_scopes():
    url = 'http://localhost/exchange_token?state={}&code=c&scope={}'
    with pytest.raises(strava.StravaError, match='activity:read_all'):
        strava.code_from_redirect(url.format('s', 'read,activity:read,activity:write'), 's')
    with pytest.raises(strava.StravaError, match='activity:write'):
        strava.code_from_redirect(url.format('s', 'read,activity:read_all'), 's')
    with pytest.raises(strava.StravaError, match='State'):
        strava.code_from_redirect(url.format('x', 'read,activity:read_all,activity:write'), 's')
    assert strava.code_from_redirect(url.format('s', 'read,activity:write,activity:read_all'), 's') == 'c'


def test_authorize_url_asks_for_read_all_and_write():
    query = parse_qs(urlparse(strava.build_authorize_url('1', 'http://localhost/x', 's')).query)
    assert set(query['scope'][0].split(',')) == {'activity:read_all', 'activity:write'}


async def test_token_file_is_0600_and_refresh_keeps_rotated_token(processor, fake, tmp_path):
    path = tmp_path / 'strava_token.json'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    token = await processor.client.tokens.access_token(processor.client._http, force=True)
    assert token == 'fresh'
    saved = json.loads(path.read_text())
    assert saved['refresh_token'] == 'rotated' and saved['athlete_id'] == ATHLETE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_settings_need_all_three_values(monkeypatch, tmp_path):
    for k in ('STRAVA_CLIENT_ID', 'STRAVA_CLIENT_SECRET', 'STRAVA_WEBHOOK_VERIFY_TOKEN', 'STRAVA_SUBSCRIPTION_ID'):
        monkeypatch.delenv(k, raising=False)
    assert strava.load_strava_settings(tmp_path) is None
    monkeypatch.setenv('STRAVA_CLIENT_ID', '1')
    monkeypatch.setenv('STRAVA_CLIENT_SECRET', 's')
    monkeypatch.setenv('STRAVA_WEBHOOK_VERIFY_TOKEN', 'v')
    monkeypatch.setenv('STRAVA_SUBSCRIPTION_ID', '42')
    got = strava.load_strava_settings(tmp_path)
    assert got.subscription_id == 42 and got.token_path == tmp_path / 'strava_token.json'


# --- writing descriptions -----------------------------------------------------

async def test_append_adds_below_existing_text_once(processor, fake):
    fake.activities[AID] = {**activity(), 'description': 'Easy day.'}
    tool = strava.set_strava_description
    first = await tool(processor.client, AID, 'Songs: X')
    assert first == {'activity_id': AID, 'changed': True, 'description': 'Easy day.\n\nSongs: X'}
    again = await tool(processor.client, AID, 'Songs: X')
    assert again['changed'] is False and again['description'] == 'Easy day.\n\nSongs: X'
    assert len(fake.puts) == 1


async def test_append_to_empty_and_replace(processor, fake):
    fake.activities[AID] = {**activity(), 'description': None}
    got = await strava.set_strava_description(processor.client, AID, '  hello  ')
    assert got['description'] == 'hello'
    got = await strava.set_strava_description(processor.client, AID, 'new', mode='replace')
    assert got['description'] == 'new' and fake.puts[-1] == (AID, {'description': 'new'})
    with pytest.raises(ValueError):
        await strava.set_strava_description(processor.client, AID, 'x', mode='prepend')


async def test_write_failure_names_the_scope_and_not_the_token(processor, fake):
    processor.client.tokens.save_response({'access_token': 'tok-SECRET-123', 'refresh_token': 'r',
                                           'expires_at': int(time.time()) + 3600})
    fake.activities[AID] = activity()
    fake.put_status = 403
    with pytest.raises(strava.StravaError, match='activity:write') as err:
        await strava.set_strava_description(processor.client, AID, 'x')
    assert 'SECRET' not in str(err.value)


async def test_tool_registered_only_with_strava(services, tmp_path, processor):
    tokens = TokenStore(tmp_path / 't.json', 'c')
    with_strava = server.build_server(services, tokens, '127.0.0.1', 8000, True, processor)
    listed = {t.name: t for t in await with_strava.list_tools()}
    props = listed['set_strava_description'].inputSchema['properties']
    assert set(props) == {'activity_id', 'text', 'mode'}
    assert '"block"' in listed['set_strava_description'].description
    without = server.build_server(services, tokens, '127.0.0.1', 8000, True)
    assert 'set_strava_description' not in {t.name for t in await without.list_tools()}


def spans(*names, artists=None, step=4):
    """Back-to-back plays of `step` minutes each, from T0."""
    return [{'track_id': str(i), 'name': n, 'artists': [(artists or {}).get(n, 'Wiz Khalifa')],
             'start': T0 + timedelta(minutes=step * i), 'end': T0 + timedelta(minutes=step * (i + 1))}
            for i, n in enumerate(names)]


def lap(start_min, minutes, meters):
    return {'start_date': at(start_min), 'elapsed_time': minutes * 60, 'distance': meters,
            'average_speed': meters / (minutes * 60)}


def test_summary_uses_laps_and_hr_stream():
    played = spans('A', 'Financial Advice', 'The Calm', 'Lost on the Road', 'E',
                   artists={'E': 'Taylor Gang'})
    laps = [lap(0, 6, 800), lap(6, 4, 663), lap(10, 10, 1000)]  # lap 2 is 9:43/mi, minutes 6-10
    streams = {'time': {'data': [0, 600, 840]}, 'heartrate': {'data': [120, 150, 156]}}
    line = strava.build_summary(activity(), played, laps, streams)
    assert line == ('🎧 Wiz Khalifa for 4 of 5 tracks. Fastest lap (9:43/mi) on '
                    '"Financial Advice" → "The Calm"; HR peaked at 156 on "Lost on the Road".')


def test_summary_without_laps_or_hr_and_with_mixed_artists():
    played = spans('A', 'B', 'C', artists={'A': 'Snoop Dogg', 'B': 'Dr. Dre', 'C': 'Lil Jon'})
    assert strava.build_summary(activity(), played, [lap(0, 30, 5000)], {}) == \
        '🎧 3 tracks, Snoop Dogg to Lil Jon.'
    assert strava.build_summary(activity(sport='Ride'), played, [lap(0, 4, 2000), lap(4, 4, 1000)], {}) == \
        '🎧 3 tracks, Snoop Dogg to Lil Jon.'
    assert strava.build_summary(activity(), [], [], {}) is None


async def test_webhook_writes_summary_once_when_enabled(processor, fake, recorder, services):
    processor.settings = dataclasses.replace(processor.settings, write_descriptions=True)
    fake.activities[AID] = {**activity(), 'description': 'Felt good.'}
    plays_during(recorder, 0, 6)
    fake.streams[AID] = {'time': {'data': [0, 900]}, 'heartrate': {'data': [130, 171]}}
    result = await processor.handle(event())
    assert result['status'] == 'logged' and result['description'] == 'written'
    desc = fake.activities[AID]['description']
    assert desc.startswith('Felt good.\n\n🎧 6 tracks, all Artist. HR peaked at 171 on "Song ')
    # No playlist context on these plays, so the Workouts playlist is linked.
    assert desc.endswith('\n\n1. Artist – Song 1\n2. Artist – Song 2\n3. Artist – Song 3\n'
                         '4. Artist – Song 4\n5. Artist – Song 5\n6. Artist – Song 6\n\n'
                         'Playlist: https://open.spotify.com/playlist/PL1')
    # A missed create retried by an update rewrites the same block: no second one.
    services.log.set_strava_event(str(AID), 'error', {})
    result = await processor.handle(event('update'))
    assert result['description'] == 'unchanged'
    assert len(fake.puts) == 1 and fake.activities[AID]['description'] == desc


async def test_webhook_leaves_description_alone_by_default(processor, fake, recorder):
    fake.activities[AID] = activity()
    plays_during(recorder, 0, 3)
    result = await processor.handle(event())
    assert result['status'] == 'logged' and 'description' not in result
    assert fake.puts == [] and not any(p.endswith(('/laps', '/streams')) for _, p in fake.calls)


async def test_summary_write_failure_does_not_fail_logging(processor, fake, recorder):
    processor.settings = dataclasses.replace(processor.settings, write_descriptions=True)
    fake.activities[AID] = activity()
    fake.put_status = 401
    plays_during(recorder, 0, 3)
    result = await processor.handle(event())
    assert result['status'] == 'logged' and result['description'].startswith('error:')


def test_write_descriptions_env(monkeypatch, tmp_path):
    for k, v in (('STRAVA_CLIENT_ID', '1'), ('STRAVA_CLIENT_SECRET', 's'),
                 ('STRAVA_WEBHOOK_VERIFY_TOKEN', 'v')):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv('STRAVA_WRITE_DESCRIPTIONS', raising=False)
    assert strava.load_strava_settings(tmp_path).write_descriptions is False
    monkeypatch.setenv('STRAVA_WRITE_DESCRIPTIONS', 'true')
    assert strava.load_strava_settings(tmp_path).write_descriptions is True


# --- the music block --------------------------------------------------------

BLOCK = '🎧 2 tracks.\n\n1. A – x\n2. B – y\n\nPlaylist: https://open.spotify.com/playlist/P1'


def test_block_lists_tracks_in_play_order_and_links_playlist():
    played = spans('Roll Up and Smile', 'The Calm', artists={'The Calm': 'Drake'})
    assert strava.build_block('🎧 2 tracks.', played, 'P1') == (
        '🎧 2 tracks.\n\n1. Wiz Khalifa – Roll Up and Smile\n2. Drake – The Calm\n\n'
        'Playlist: https://open.spotify.com/playlist/P1')
    assert strava.build_block('🎧 2 tracks.', played, None) == (
        '🎧 2 tracks.\n\n1. Wiz Khalifa – Roll Up and Smile\n2. Drake – The Calm')


def test_block_shortens_a_long_list():
    played = spans(*[f"Song number {i}" for i in range(80)], step=1)
    block = strava.build_block('🎧 80 tracks.', played, 'P1', max_chars=400)
    assert len(block) <= 400
    lines = block.split('\n')
    assert lines[-3].startswith('…and ') and lines[-3].endswith(' more')
    shown = sum(1 for line in lines if line[:1].isdigit())
    assert lines[-3] == f"…and {80 - shown} more" and lines[2] == '1. Wiz Khalifa – Song number 0'
    assert lines[-1] == 'Playlist: https://open.spotify.com/playlist/P1'


def test_playlist_is_the_context_of_most_plays_else_workouts():
    played = spans('a', 'b', 'c')
    assert strava.playlist_for(played, 'W') == 'W'
    for s, uri in zip(played, ['spotify:playlist:P1', 'spotify:playlist:P1', 'spotify:album:X']):
        s['context_uri'] = uri
    assert strava.playlist_for(played, 'W') == 'P1'
    played[1]['context_uri'] = None  # one of three is not "most"
    assert strava.playlist_for(played, 'W') == 'W'
    assert strava.playlist_for(played, None) is None


@pytest.mark.parametrize('current, expected', [
    ('', BLOCK),
    ('Felt good.', 'Felt good.\n\n' + BLOCK),
    ('🎧 old line by hand.', BLOCK),
    ('Before.\n\n🎧 old.\n\n1. Z – z\n…and 3 more\n\nPlaylist: https://open.spotify.com/playlist/OLD'
     '\n\nAfter.', 'Before.\n\n' + BLOCK + '\n\nAfter.'),
    ('Spotify Playlist: https://open.spotify.com/playlist/6LP\n\n🎧 11 tracks, NF to Justin Bieber.',
     'Spotify Playlist: https://open.spotify.com/playlist/6LP\n\n' + BLOCK),
    ('🎧 old.\nMy own note.', BLOCK + '\nMy own note.'),
])
def test_block_mode_replaces_only_our_block(current, expected):
    assert strava.merge_description(current, BLOCK, 'block') == expected
    # Idempotent: writing the same block again changes nothing.
    assert strava.merge_description(expected, BLOCK, 'block') == expected


def test_block_mode_needs_the_mark():
    with pytest.raises(ValueError):
        strava.merge_description('x', 'no mark', 'block')


async def test_block_mode_writes_once(processor, fake):
    fake.activities[AID] = {**activity(), 'description': 'Note.\n\n🎧 by hand.'}
    first = await strava.set_strava_description(processor.client, AID, BLOCK, mode='block')
    assert first['changed'] and fake.activities[AID]['description'] == 'Note.\n\n' + BLOCK
    again = await strava.set_strava_description(processor.client, AID, BLOCK, mode='block')
    assert again['changed'] is False and len(fake.puts) == 1


async def test_webhook_links_the_playlist_the_plays_came_from(processor, fake, recorder):
    processor.settings = dataclasses.replace(processor.settings, write_descriptions=True)
    fake.activities[AID] = activity()
    recorder.context = 'spotify:playlist:RUN1'
    plays_during(recorder, 0, 3)
    await processor.handle(event())
    assert fake.activities[AID]['description'].endswith(
        '\n\nPlaylist: https://open.spotify.com/playlist/RUN1')


async def test_backfill_keeps_hand_summary_and_text_and_writes_only_when_asked(
        processor, fake, services):
    hand = '🎧 Wiz Khalifa for 2 of 3 tracks. Fastest interval (9:43/mi) on "Song 2".'
    fake.activities[AID] = {**activity(), 'description': 'Spotify Playlist: mine\n\n' + hand}
    # Plays stored by an earlier poll; this activity was never logged as a workout.
    services.log.add_plays([
        {'played_at': at(m), 'track': {'id': IDS[i], 'name': f"Song {i + 1}", 'duration_ms': 200_000,
                                       'artists': [{'name': 'Wiz Khalifa'}]},
         'context': {'uri': 'spotify:playlist:RUN1'}}
        for i, m in enumerate((4, 8, 12))])
    services.log.add_plays([{'played_at': at(90), 'track': {'id': IDS[4], 'duration_ms': 1}}])
    expected = ('Spotify Playlist: mine\n\n' + hand + '\n\n1. Wiz Khalifa – Song 1\n'
                '2. Wiz Khalifa – Song 2\n3. Wiz Khalifa – Song 3\n\n'
                'Playlist: https://open.spotify.com/playlist/RUN1')
    preview = await strava.rebuild_description(processor.client, services.log, AID)
    assert preview['plays_from'] == 'activity window' and preview['plays'] == 3
    assert preview['summary'] == 'kept' and preview['description'] == expected
    assert fake.puts == []
    done = await strava.rebuild_description(processor.client, services.log, AID, write=True)
    assert done['written'] and fake.activities[AID]['description'] == expected
    again = await strava.rebuild_description(processor.client, services.log, AID, write=True)
    assert again['changed'] is False and len(fake.puts) == 1
    regen = await strava.rebuild_description(processor.client, services.log, AID, keep_summary=False)
    assert regen['summary'] == 'generated'
    assert regen['description'].startswith('Spotify Playlist: mine\n\n🎧 3 tracks, all Wiz Khalifa.\n\n1.')


async def test_backfill_with_no_plays_changes_nothing(processor, fake, services):
    fake.activities[AID] = {**activity(), 'description': '🎧 by hand.'}
    result = await strava.rebuild_description(processor.client, services.log, AID, write=True)
    assert result['changed'] is False and result['plays'] == 0 and fake.puts == []


async def test_token_reread_from_disk_before_refresh(processor, fake, tmp_path):
    tokens = processor.client.tokens
    await tokens.access_token(processor.client._http)  # cache the file in memory
    # Another process refreshed meanwhile; our cached copy is expired.
    tokens._data['expires_at'] = 0
    strava._write_private(tmp_path / 'strava_token.json', {
        'access_token': 'from-cli', 'refresh_token': 'cli-rotated',
        'expires_at': int(time.time()) + 3600, 'athlete_id': ATHLETE})
    assert await tokens.access_token(processor.client._http) == 'from-cli'
    assert ('POST', '/oauth/token') not in fake.calls
