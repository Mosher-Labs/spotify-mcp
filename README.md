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

## Tools

| Tool | Purpose |
|------|---------|
| `search_tracks` | Catalog search (max 10 per call). |
| `top_tracks`, `saved_tracks` | Your listening history. |
| `track_features` | Tempo, energy, danceability, valence, loudness for track IDs. |
| `set_track_tempo` | Record a BPM by hand. |
| `candidate_tracks` | Your tracks inside a BPM and energy window, with optional half or double time. |
| `my_playlists`, `playlist_items` | Read your own playlists. |
| `log_workout_listening` | After a workout: match the plays in its time window, tie them to it, add the completed ones to the "Workouts" playlist. |
| `get_track_history`, `get_workout_music_stats`, `listening_status` | What the log says: skips, effort, cadence, tempo bands, keepers. |
| `create_playlist`, `add_tracks`, `replace_playlist_tracks`, `reorder_tracks`, `remove_tracks` | Edit playlists. |

## Listening log

Spotify's recently-played endpoint holds only the last 50 plays. A poller
thread copies them into `listening.sqlite3` on the data volume every 10
minutes (`SPOTIFY_MCP_POLL_SECONDS`, 0 turns it off). `log_workout_listening`
ties the plays in a workout's window to that workout. The log never leaves the
volume, and nothing in it is sent anywhere except to Claude when a tool is
called. Delete `listening.sqlite3` to erase it.

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
| `SPOTIFY_MCP_POLL_SECONDS` | Play-log poll interval. Default `600`; `0` disables. |

## Login

```bash
spotify-mcp-auth
```

It prints a URL. Approve in a browser, then paste the address the browser was
sent to (the page itself fails to load, which is expected).

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
