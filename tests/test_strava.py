import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone

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
            aid = int(path.rsplit('/', 1)[1])
            if aid not in self.activities:
                return httpx.Response(404, json={'message': 'Record Not Found'})
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

def test_redirect_requires_read_all_scope():
    with pytest.raises(strava.StravaError, match='activity:read_all'):
        strava.code_from_redirect('http://localhost/exchange_token?state=s&code=c&scope=read,activity:read', 's')
    with pytest.raises(strava.StravaError, match='State'):
        strava.code_from_redirect('http://localhost/exchange_token?state=x&code=c&scope=read,activity:read_all', 's')
    assert strava.code_from_redirect(
        'http://localhost/exchange_token?state=s&code=c&scope=read,activity:read_all', 's') == 'c'


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
