"""Command line entry point: `python -m riot_crawler {crawl,stats,export}`."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from .features import COLUMNS, exclusion_reason, match_to_row
from .routing import ALL_PLATFORMS, ALL_TIERS
from .store import Store

# Default crawl windows (UTC). Patch 26.20 (client gameVersion 16.20.x) went
# live Oct 7, 2026; 26.21 is scheduled for Oct 21. Regions deploy at different
# times, so the window is padded on both sides. The gameVersion check is the
# real filter; the window only limits which match IDs get listed.
KNOWN_WINDOWS = {
    "16.20": ("2026-10-06T12:00:00Z", "2026-10-22T12:00:00Z"),
}


def _parse_time(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def cmd_crawl(args) -> int:
    import aiohttp

    from .client import AuthError, RiotClient
    from .crawler import CrawlConfig, Crawler

    api_key = os.environ.get("RIOT_API_KEY")
    if not api_key:
        print("Set the RIOT_API_KEY environment variable (https://developer.riotgames.com).", file=sys.stderr)
        return 2

    default_start, default_end = KNOWN_WINDOWS.get(args.patch, (None, None))
    start, end = args.start or default_start, args.end or default_end
    if not start or not end:
        print(f"No default window for patch {args.patch}; pass --start and --end.", file=sys.stderr)
        return 2

    platforms = [p.lower() for p in args.platforms]
    unknown = [p for p in platforms if p not in ALL_PLATFORMS]
    if unknown:
        print(f"Unknown platforms {unknown}; choose from {ALL_PLATFORMS}", file=sys.stderr)
        return 2

    cfg = CrawlConfig(
        patch=args.patch,
        start_time=_parse_time(start),
        end_time=_parse_time(end),
        platforms=platforms,
        seed_tiers=[t.upper() for t in args.seed_tiers],
        workers_per_region=args.workers,
        recrawl_hours=args.recrawl_hours,
        follow=args.follow,
        max_matches=args.max_matches,
    )

    store = Store(args.db)
    stored_patch = store.get_meta("patch")
    if stored_patch and stored_patch != cfg.patch:
        print(f"{args.db} holds patch {stored_patch}; use a different --db for {cfg.patch}.", file=sys.stderr)
        return 2
    store.set_meta("patch", cfg.patch)
    store.set_meta("start_time", str(cfg.start_time))
    if store.get_meta("end_time") != str(cfg.end_time):
        # Window end changed: players marked "finished" may now have uncovered time.
        store.reopen_players(cfg.end_time)
        store.set_meta("end_time", str(cfg.end_time))
    if args.reseed:
        store.reset_seeds()

    logging.info(
        "patch %s window %s -> %s, platforms=%s regions=%s",
        cfg.patch, start, end, ",".join(cfg.platforms), ",".join(cfg.regions),
    )

    async def main() -> int:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            client = RiotClient(api_key, session)
            try:
                # Fail fast on a bad/expired key before spawning workers.
                await client.apex_league(cfg.platforms[0], "CHALLENGER")
            except AuthError as exc:
                logging.error("API key rejected: %s", exc)
                return 1
            crawler = Crawler(client, store, cfg)
            try:
                await crawler.run()
            except ExceptionGroup as eg:
                auth, rest = eg.split(AuthError)
                if rest or not auth:
                    raise
                logging.error("API key rejected mid-crawl (dev keys expire after 24h): %s", auth.exceptions[0])
                return 1
        return 0

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("interrupted; progress is saved, rerun the same command to resume")
        return 130
    finally:
        print(json.dumps(store.summary(int(time.time())), indent=2))
        store.close()


def cmd_stats(args) -> int:
    store = Store(args.db)
    print(json.dumps({"patch": store.get_meta("patch"), **store.summary(int(time.time()))}, indent=2))
    store.close()
    return 0


def cmd_export(args) -> int:
    store = Store(args.db)
    patch = store.get_meta("patch")
    reasons: Counter[str] = Counter()

    def rows():
        for _, match in store.iter_raw_matches():
            reason = exclusion_reason(match, patch)
            if reason:
                reasons[reason] += 1
                continue
            row = match_to_row(match)
            if args.require_roles and not row["roles_from_api"]:
                reasons["roles_incomplete"] += 1
                continue
            reasons["kept"] += 1
            yield row

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if args.out.endswith(".parquet"):
        import pandas as pd

        pd.DataFrame(list(rows()), columns=COLUMNS).to_parquet(args.out, index=False)
    else:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows())
    store.close()
    print(json.dumps(dict(reasons), indent=2))
    print(f"wrote {reasons['kept']} rows to {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="riot_crawler", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="crawl Ranked Solo/Duo matches for one patch (resumable)")
    c.add_argument("--db", default="data/crawl_16_20.sqlite")
    c.add_argument("--patch", default="16.20", help="gameVersion major.minor (16.20 == patch 26.20)")
    c.add_argument("--start", help="window start, ISO-8601 (UTC if no offset)")
    c.add_argument("--end", help="window end, ISO-8601 (UTC if no offset)")
    c.add_argument("--platforms", nargs="+", default=ALL_PLATFORMS, help="default: all platforms")
    c.add_argument("--seed-tiers", nargs="+", default=ALL_TIERS, help="ladder tiers to seed from")
    c.add_argument("--workers", type=int, default=10, help="concurrent workers per region")
    c.add_argument("--recrawl-hours", type=float, default=24.0,
                   help="re-list each player's matches this often while the window is open")
    c.add_argument("--follow", action="store_true", help="keep running until the window closes")
    c.add_argument("--max-matches", type=int, help="stop after this many in-patch matches")
    c.add_argument("--reseed", action="store_true", help="re-walk ladder pages (catch newly ranked players)")
    c.set_defaults(func=cmd_crawl)

    s = sub.add_parser("stats", help="show crawl progress")
    s.add_argument("--db", default="data/crawl_16_20.sqlite")
    s.set_defaults(func=cmd_stats)

    e = sub.add_parser("export", help="write the draft-features training table")
    e.add_argument("--db", default="data/crawl_16_20.sqlite")
    e.add_argument("--out", default="data/draft_16_20.csv", help=".csv or .parquet (needs pyarrow)")
    e.add_argument("--require-roles", action="store_true",
                   help="drop matches where teamPosition isn't one-of-each per team")
    e.set_defaults(func=cmd_export)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return args.func(args)
