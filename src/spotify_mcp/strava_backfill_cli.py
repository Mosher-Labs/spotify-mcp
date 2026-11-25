"""Rebuild the music block on past Strava activities: ``spotify-mcp-strava-backfill``.

Run it inside the pod, where the listening log and Strava token live:

    spotify-mcp-strava-backfill 20987536751 21013702441
    spotify-mcp-strava-backfill --write 20987536751 21013702441

Without --write it only prints the description each activity would get. A 🎧
line already in a description is kept word for word unless
--regenerate-summary is given; the track list and playlist link are always
rebuilt. Text outside the block is left alone.
"""

import argparse
import asyncio
import json

import httpx

from spotify_mcp import strava
from spotify_mcp.config import load_settings
from spotify_mcp.history import ListeningLog


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    strava_settings = strava.load_strava_settings(settings.data_dir)
    if strava_settings is None:
        raise SystemExit(
            'Set STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET and STRAVA_WEBHOOK_VERIFY_TOKEN first.'
        )
    log = ListeningLog(settings.log_db_path)
    failed = 0
    async with httpx.AsyncClient(timeout=30) as http:
        client = strava.StravaClient(strava_settings, strava.StravaTokenStore(strava_settings), http)
        for aid in args.activity_ids:
            try:
                result = await strava.rebuild_description(
                    client, log, aid, write=args.write, keep_summary=not args.regenerate_summary
                )
            except (strava.StravaError, httpx.HTTPError) as exc:
                failed += 1
                print(json.dumps({'activity_id': aid, 'error': str(exc)[:300]}))
                continue
            description = result.pop('description')
            print(json.dumps(result, ensure_ascii=False))
            print(description, end='\n\n')
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog='spotify-mcp-strava-backfill',
        description='Rebuild the music block on past Strava activities.',
    )
    parser.add_argument('activity_ids', nargs='+', type=int, metavar='ACTIVITY_ID')
    parser.add_argument('--write', action='store_true', help='write to Strava (default: print only)')
    parser.add_argument('--regenerate-summary', action='store_true',
                        help='replace an existing 🎧 line with a generated one')
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == '__main__':
    main()
