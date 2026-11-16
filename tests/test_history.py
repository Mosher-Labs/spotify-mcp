from datetime import datetime, timedelta, timezone

import pytest

from spotify_mcp import tools
from spotify_mcp.history import annotate, to_utc
from spotify_mcp.poller import poll_once

from conftest import IDS

T0 = datetime(2026, 11, 14, 15, 0, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


def test_naive_timestamps_are_rejected_and_offsets_normalized():
    with pytest.raises(ValueError, match='timezone'):
        to_utc('2026-11-14T08:00:00')
    assert to_utc('2026-11-14T08:00:00-07:00') == to_utc('2026-11-14T15:00:00Z')


def test_annotate_end_semantics_uses_gap_before():
    plays = [
        {'played_at': to_utc(at(0)), 'duration_ms': 200_000},
        {'played_at': to_utc(at(1)), 'duration_ms': 200_000},   # only 60s after: skipped
        {'played_at': to_utc(at(5)), 'duration_ms': 200_000},   # 240s gap > 200s: completed
    ]
    got = annotate(plays, 'end')
    assert [p['completed'] for p in got] == [None, 0, 1]
    assert got[1]['listened_ms'] == 60_000 and got[2]['listened_ms'] == 200_000


def test_annotate_start_semantics_uses_gap_after():
    plays = [
        {'played_at': to_utc(at(0)), 'duration_ms': 200_000},
        {'played_at': to_utc(at(1)), 'duration_ms': 200_000},
        {'played_at': to_utc(at(5)), 'duration_ms': 200_000},
    ]
    assert [p['completed'] for p in annotate(plays, 'start')] == [0, 1, None]


async def test_poll_stores_new_plays_once(services, recorder):
    recorder.recent = [(IDS[0], at(0), 200_000), (IDS[1], at(4), 200_000)]
    assert await poll_once(services.spotify, services.log) == 2
    assert await poll_once(services.spotify, services.log) == 0  # same 50 again
    status = services.log.status()
    assert status['plays_logged'] == 2 and status['last_poll_ok'] and not status['last_poll_error']


async def test_poll_failure_is_recorded(services, recorder):
    recorder.status_overrides = [403]
    with pytest.raises(Exception):
        await poll_once(services.spotify, services.log)
    assert 'allowlist' in services.log.status()['last_poll_error']


def three_song_run(recorder):
    # Workout 15:10-15:40. Play ends: 15:03 (before), 15:14, 15:15 (skipped, 1 min), 15:19, 15:50 (after).
    recorder.recent = [
        (IDS[0], at(3), 200_000),
        (IDS[1], at(14), 200_000),
        (IDS[2], at(15), 200_000),
        (IDS[3], at(19), 200_000),
        (IDS[4], at(50), 200_000),
    ]


async def test_log_workout_matches_window_and_builds_workouts_playlist(services, recorder):
    three_song_run(recorder)
    result = await tools.log_workout_listening(
        services, 'garmin-1', at(10), at(40), name='Tempo', workout_type='run',
        avg_cadence=172, avg_hr=155, effort_1_to_10=7,
        segments=[{'start': at(10), 'end': at(25), 'label': 'tempo', 'avg_cadence': 176, 'avg_hr': 160}],
    )
    assert [t['track_id'] for t in result['tracks']] == [IDS[1], IDS[2], IDS[3]]
    assert (result['completed'], result['skipped'], result['unknown']) == (2, 1, 0)
    # IDS[2] was skipped, so only the other two go in the playlist.
    adds = [b['uris'] for m, p, b in recorder.calls if p == '/v1/playlists/PL1/items']
    assert adds == [[f"spotify:track:{IDS[1]}", f"spotify:track:{IDS[3]}"]]
    assert result['added_to_workouts_playlist'] == 2


async def test_relogging_is_idempotent_and_playlist_deduped(services, recorder):
    three_song_run(recorder)
    await tools.log_workout_listening(services, 'garmin-1', at(10), at(40), workout_type='run')
    again = await tools.log_workout_listening(services, 'garmin-1', at(10), at(40), workout_type='run')
    assert again['added_to_workouts_playlist'] == 0
    assert recorder.playlists == 1  # the Workouts playlist is created once
    stats = await tools.get_workout_music_stats(services)
    assert len(stats['workouts']) == 1 and stats['workouts'][0]['plays'] == 3


async def test_second_workout_adds_only_new_tracks(services, recorder):
    three_song_run(recorder)
    await tools.log_workout_listening(services, 'w1', at(10), at(40))
    recorder.recent += [(IDS[5], at(95), 200_000), (IDS[1], at(99), 200_000)]
    r = await tools.log_workout_listening(services, 'w2', at(90), at(120))
    assert r['added_to_workouts_playlist'] == 1  # IDS[1] already there


async def test_per_workout_playlist_is_optional(services, recorder):
    three_song_run(recorder)
    r = await tools.log_workout_listening(services, 'w1', at(10), at(40), name='Tempo', per_workout_playlist=True)
    assert recorder.playlists == 2 and 'workout_playlist_url' in r


async def test_empty_window_explains_itself(services, recorder):
    r = await tools.log_workout_listening(services, 'w0', at(10), at(40))
    assert r['plays_matched'] == 0 and 'poller' in r['note']


async def test_track_history_and_stats_learn_from_workouts(services, recorder):
    recorder.tempos = {IDS[1]: 172, IDS[3]: 90}
    await tools.track_features(services, [IDS[1], IDS[3]])  # fill the tempo cache
    for wid, base, effort in (('w1', 0, 8), ('w2', 100, 6)):
        recorder.recent = [(IDS[1], at(base + 14), 200_000), (IDS[2], at(base + 15), 200_000),
                           (IDS[3], at(base + 19), 200_000)]
        await tools.log_workout_listening(
            services, wid, at(base + 10), at(base + 40), workout_type='run', effort_1_to_10=effort,
            segments=[{'start': at(base + 10), 'end': at(base + 40), 'avg_cadence': 174}],
        )
    hist = {h['track_id']: h for h in await tools.get_track_history(services)}
    assert hist[IDS[2]]['skip_rate'] == 1.0 and hist[IDS[2]]['workouts'] == 2
    assert hist[IDS[1]]['skip_rate'] == 0.0 or hist[IDS[1]]['skip_rate'] is None
    assert hist[IDS[3]]['avg_effort'] == 7.0 and hist[IDS[3]]['avg_cadence'] == 174.0
    stats = await tools.get_workout_music_stats(services, workout_type='run')
    assert {k['track_id'] for k in stats['keepers']} == {IDS[1], IDS[3]}  # never skipped, 2 workouts each
    assert [k['track_id'] for k in stats['often_skipped']] == [IDS[2]]
    bands = {b['bpm_from']: b for b in stats['tempo_bands']}
    assert bands[170]['avg_cadence'] == 174.0 and bands[90]['plays'] == 2
    assert (await tools.get_workout_music_stats(services, workout_type='ride'))['workouts'] == []


async def test_candidates_drop_tracks_skipped_in_workouts(services, recorder):
    recorder.tempos = {IDS[1]: 172, IDS[2]: 172}
    for wid, base in (('w1', 0), ('w2', 100)):
        recorder.recent = [(IDS[1], at(base + 14), 200_000), (IDS[2], at(base + 15), 200_000)]
        await tools.log_workout_listening(services, wid, at(base + 10), at(base + 40))
    recorder.top = [IDS[1], IDS[2]]
    kept = await tools.candidate_tracks(services, min_bpm=165, max_bpm=180)
    assert [m['id'] for m in kept['matches']] == [IDS[1]] and kept['dropped_for_skips'] == 1
    allowed = await tools.candidate_tracks(services, min_bpm=165, max_bpm=180, exclude_skipped=False)
    assert {m['id'] for m in allowed['matches']} == {IDS[1], IDS[2]}


async def test_timestamp_fit_picks_the_reading_the_data_supports(services, recorder):
    # 25 back-to-back full plays of 200s, played_at 200s apart.
    recorder.recent = [(IDS[i % 7], at(i * 200 / 60), 200_000) for i in range(25)]
    await poll_once(services.spotify, services.log)
    fit = services.log.status()['timestamp_fit']
    assert fit['pairs'] == 24 and fit['error_if_end_s'] == fit['error_if_start_s'] == 0
