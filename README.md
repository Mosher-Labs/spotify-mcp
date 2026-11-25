# spotify-mcp

![GitHub branch status](https://img.shields.io/github/checks-status/mosher-labs/spotify-mcp/main)
![GitHub last commit](https://img.shields.io/github/last-commit/mosher-labs/spotify-mcp)
![GitHub License](https://img.shields.io/github/license/mosher-labs/spotify-mcp)

An MCP server that lets Claude build Spotify playlists for a workout. It picks
songs from your own top tracks and liked songs, filters them by energy and
tempo (BPM), and creates or edits playlists in your account.

Built for one user on a homelab, behind a shared-secret header, with the
Spotify refresh token stored on a persistent volume. It is not a general
multi-user service.

## What it does and does not use from Spotify

Spotify's audio-features endpoint (BPM, energy) is closed to apps created after
November 2024, and its February 2026 Development Mode changes cut the search
limit to 10, removed batch track lookups, and moved playlist writes to
`/playlists/{id}/items`. This server follows those rules.

- **From Spotify:** search, your top tracks, your liked songs, your playlists,
  and playlist create and edit.
- **From ReccoBeats** (`api.reccobeats.com`, no key): tempo and energy per
  Spotify track ID. Results are cached in SQLite on the data volume. A track
  ReccoBeats does not know is retried after a week, and `set_track_tempo`
  records a BPM by hand.
- **From tempo-service** (optional, `TEMPO_SERVICE_URL`): when ReccoBeats has
  no tempo, [tempo-service](https://github.com/Mosher-Labs/tempo-service)
  measures the BPM from the track's iTunes preview, looked up by first artist
  and title. It gives tempo only, no energy, and a `trust` of `high` or
  `check`. Lookups take 4 to 7 seconds, so each tool call makes at most
  `TEMPO_SERVICE_MAX_PER_CALL` of them, within 60 seconds, and reports the
  rest as pending. The artist and title go to tempo-service and on to Apple's
  iTunes Search API.
- **When a source errors:** the tool call still returns. Tracks ReccoBeats or
  tempo-service did not answer are listed as pending, and the next call
  retries them.

## Tools

| Tool | Purpose |
|------|---------|
| `search_tracks` | Catalog search (max 10 per call). |
| `top_tracks`, `saved_tracks` | Your listening history. |
| `track_features` | Tempo, energy, danceability, valence, loudness for track IDs, with each tempo's source and the IDs still pending. |
| `set_track_tempo` | Record a BPM by hand. |
| `candidate_tracks` | Your tracks inside a BPM and energy window, with optional half or double time. |
| `my_playlists`, `playlist_items` | Read your own playlists. |
| `log_workout_listening` | After a workout: match the plays in its time window, tie them to it, add the completed ones to the "Workouts" playlist. |
| `get_track_history`, `get_workout_music_stats`, `listening_status` | What the log says: skips, effort, cadence, tempo bands, keepers. |
| `create_playlist`, `rename_playlist`, `add_tracks`, `replace_playlist_tracks`, `reorder_tracks`, `remove_tracks` | Edit playlists. Run playlists are named `Run · YYYY-MM-DD · <cadence>`. |
| `set_strava_description` | Write a Strava activity's description. `mode="append"` (default) adds the text below what is there, once; `mode="replace"` overwrites it. Only listed when the Strava variables are set. |

## Listening log

Spotify's recently-played endpoint holds only the last 50 plays. They are
copied into `listening.sqlite3` on the data volume whenever a workout is
logged. `log_workout_listening` ties the plays in a workout's window to that
workout. The log never leaves the volume, and nothing in it is sent anywhere
except to Claude when a tool is called. Delete `listening.sqlite3` to erase it.

### Strava trigger

With the Strava variables set, a Strava push subscription logs every new
activity on its own. Strava POSTs to `/strava/webhook`; the server answers 200
at once and then:

1. checks `subscription_id` and `owner_id` against its own subscription and
   the logged-in athlete (Strava does not sign events),
1. fetches the activity from Strava's API for its start time, elapsed time,
   name and sport (nothing in the POST body is used beyond the IDs),
1. fetches Spotify's 50 most recent plays and runs `log_workout_listening`
   for that window, the same as calling the tool.

An activity already logged is skipped, so Strava's retries do nothing. An
`update` renames a logged workout, or logs an activity that was missed while
the server was down (editing the title in Strava retries it). A `delete` drops
the workout only if Strava confirms the activity is gone; plays and the
playlist are kept. `listening_status` shows the last event in
`strava_last_event`.

**Music summary.** With `STRAVA_WRITE_DESCRIPTIONS=true`, each newly logged
activity also gets one line appended to its Strava description, for example:

```text
🎧 Wiz Khalifa for 4 of 5 tracks. Fastest lap (9:43/mi) on "Financial Advice" → "The Calm"; HR peaked at 156 on "Lost on the Road".
```

Every number is measured: the plays in the log, the fastest of Strava's laps
(two or more laps of at least 200 m, foot sports only), and the peak of the
heart-rate stream, each matched to the song playing at that time. If Strava
has no laps or no heart rate, that part is left out. A description that
already holds a 🎧 line is left alone, so retries never add a second line. A
failed write shows in the event result as `description: error: ...` and does
not undo the logging.

**The 50-play limit.** At about 3.5 minutes a track, 50 plays is about three
hours of continuous listening. Plays roll off when the workout plus the time
until the webhook fires holds more than 50 plays: a one-hour run has about two
hours of margin, a 2.5-hour long run under 30 minutes. If the fetch was full
and does not reach back to the start of the workout, the event result carries
a `possible_gap` note. Set `SPOTIFY_MCP_POLL_SECONDS=3600` to turn on the
background poll as a safety net; hourly is well inside the three-hour window.

Skips are inferred from the time between consecutive plays, because Spotify
reports no play length. The learning is plain tables and aggregate queries,
and shows what coincided with a track, not what it caused.

## Configuration

| Variable | Meaning |
|----------|---------|
| `SPOTIFY_CLIENT_ID` | From your Spotify developer app. No client secret is used (PKCE). |
| `SPOTIFY_REDIRECT_URI` | Default `http://127.0.0.1:8888/callback`. Must match the app exactly. |
| `SPOTIFY_MCP_DATA_DIR` | Where `token.json` and `tempo.sqlite3` live. |
| `SPOTIFY_MCP_HTTP_TOKEN` | Required unless bound to loopback. Sent as `X-API-Key` or `Authorization: Bearer`. |
| `SPOTIFY_MCP_HOST`, `SPOTIFY_MCP_PORT` | Default `127.0.0.1`, `8000`. |
| `SPOTIFY_MCP_STATELESS_HTTP` | Default `true`. |
| `SPOTIFY_MCP_POLL_SECONDS` | Background play-log poll interval. Default `0` (off). |
| `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET` | From your Strava API app. All three Strava values set turns the webhook on. |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Any random string; the same value goes in the subscription create call. |
| `STRAVA_SUBSCRIPTION_ID` | Optional. Unset, the server looks it up from Strava once. |
| `STRAVA_WRITE_DESCRIPTIONS` | `true` appends the music summary to each webhook-logged activity. Default off. |
| `STRAVA_REDIRECT_URI` | For `spotify-mcp-strava-auth`. Default `http://localhost/exchange_token`. |
| `TEMPO_SERVICE_URL` | tempo-service base URL. Unset turns the fallback off. |
| `TEMPO_SERVICE_MAX_PER_CALL` | Fallback lookups per tool call. Default `5`. |

## Login

```bash
spotify-mcp-auth
```

It prints a URL. Approve in a browser, then paste the address the browser was
sent to (the page itself fails to load, which is expected).

For the Strava trigger, run `spotify-mcp-strava-auth` the same way. It asks for
`activity:read_all` (includes "Only You" activities) and `activity:write`
(needed to change a description), and saves `strava_token.json` with
mode 0600. A token from before `activity:write` was added can still read but
not write; run the login again to replace it.

## Spotify's terms

Spotify's developer terms forbid using Spotify content to train machine
learning models. This server only passes track names and IDs to the model at
request time. Read the current terms before widening that use.

## Tests

```bash
uv sync && uv run pytest
```

## Deployment

`homelab-gitops` runs it at `https://spotify.benniemosher.dev/mcp` (see
`apps/spotify-mcp/RUNBOOK.md` there). Every push to `main` builds
`ghcr.io/mosher-labs/spotify-mcp:<short sha>`, and the cluster pins the
digest.

## Contributing

Install the pre-commit hooks once after cloning, and run them before each
commit:

```bash
pre-commit install
pre-commit run --all-files
```

Commits follow Conventional Commits, which drives the release workflow.
