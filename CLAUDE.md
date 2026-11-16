# spotify-mcp - Project Memory

## Project Overview

A single-user MCP server that lets Claude build Spotify playlists for a
workout and log what was played during it. It runs on the Mosher Labs k3s
homelab (`homelab-gitops/apps/spotify-mcp`) behind a Cloudflare tunnel, and
claude.ai reaches it with an `X-API-Key` header.

**Key Details:**

- **Language:** Python 3.12, FastMCP, managed with `uv`
- **Image:** `ghcr.io/mosher-labs/spotify-mcp:<short sha>`, built by
  `.github/workflows/image.yml` on push to `main`
- **Spotify auth:** PKCE, no client secret. `spotify-mcp-auth` stores the
  refresh token in `token.json` on the data volume, mode 0600
- **Tempo and energy:** ReccoBeats, because Spotify's audio-features endpoint
  is closed to apps created after November 2024. Cached in SQLite
- **Listening log:** a poller stores recently played tracks in
  `listening.sqlite3` on the data volume

## Repository Structure

```text
spotify-mcp/
├── src/spotify_mcp/
│   ├── server.py        # FastMCP app, HTTP routes, poller start
│   ├── tools.py         # MCP tools
│   ├── spotify_api.py   # Spotify Web API client
│   ├── spotify_auth.py  # PKCE and token refresh
│   ├── auth_cli.py      # spotify-mcp-auth
│   ├── tempo.py         # ReccoBeats lookups and cache
│   ├── http_auth.py     # X-API-Key / Bearer gate
│   └── config.py
├── tests/
├── Dockerfile
└── .github/workflows/   # image, PR tests, pre-commit, release, stale
```

## Working on This Repo

1. Branch from `main`: `git checkout -b feat/description`
1. Run the tests: `uv sync && uv run pytest`
1. Run pre-commit before committing: `pre-commit run --all-files`
1. Commit with Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`)
1. Open a PR. Merging to `main` builds a new image; update the digest in
   `homelab-gitops/apps/spotify-mcp/application.yaml`

## Constraints

- Spotify Development Mode: search returns at most 10 results, no batch track
  lookups, playlist writes go to `/playlists/{id}/items`, and only playlists
  the user owns are readable.
- Spotify's developer terms forbid training models on Spotify content. Only
  pass track names and IDs to the model at request time.
- YAML lines stay under 80 characters (yamllint), outside
  `.github/workflows`.

## References

- @README.md
- Shared workflows: <https://github.com/Mosher-Labs/.github>
