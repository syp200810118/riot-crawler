"""Crawl orchestration: ladder seeding + snowball crawl over match participants.

Discovery strategy ("every discoverable match"):
  1. Seed the player frontier from every Ranked Solo/Duo ladder page
     (Challenger -> Iron) on each platform (league-v4, platform hosts).
  2. For each player, list their ranked solo match IDs inside the patch window
     (match-v5 by-puuid, regional hosts).
  3. Fetch each new match; keep it only if gameVersion is the target patch and
     queueId == 420, then push its 10 participants into the frontier.
  4. Repeat until nothing is left. Because the patch may still be live,
     players are re-listed every `recrawl_hours` until the window closes, and
     only the not-yet-covered part of the window is requested.

Platform hosts (seeding) and regional hosts (matches) have independent rate
limits, so seeding runs concurrently with match crawling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .client import AuthError, RiotAPIError, TransientError
from .routing import APEX_TIERS, DIVISIONS, PLATFORM_TO_REGION, RANKED_SOLO_QUEUE_ID
from .store import IN_PATCH, MISSING, NEVER, OTHER, PRIO_FROM_LADDER, PRIO_FROM_MATCH, Store

log = logging.getLogger(__name__)

# Re-listing a player overlaps the previous window by this much, so games that
# were still in progress at the last listing are picked up.
RECRAWL_OVERLAP_S = 2 * 3600
PLAYER_ERROR_RETRY_S = 600
MATCH_BATCH = 200
PLAYER_BATCH = 50
PAGE_SIZE = 100


@dataclass
class CrawlConfig:
    patch: str                       # e.g. "16.20" (matches info.gameVersion "16.20.x")
    start_time: int                  # epoch seconds, inclusive
    end_time: int                    # epoch seconds
    platforms: list[str]
    seed_tiers: list[str]
    workers_per_region: int = 10
    recrawl_hours: float = 24.0
    follow: bool = False             # keep running until the window closes
    max_matches: int | None = None
    regions: list[str] = field(init=False)

    def __post_init__(self):
        self.regions = sorted({PLATFORM_TO_REGION[p] for p in self.platforms})


def version_matches(game_version: str, patch: str) -> bool:
    return game_version.split(".")[:2] == patch.split(".")[:2]


class _RegionQueue:
    """Hands out work for one region; matches first, then due players."""

    def __init__(self, store: Store, region: str):
        self.store = store
        self.region = region
        self.buffer: deque[tuple] = deque()
        self.inflight: set[str] = set()

    def next_task(self, now: int) -> tuple | None:
        if not self.buffer:
            self._refill(now)
        if not self.buffer:
            return None
        task = self.buffer.popleft()
        self.inflight.add(task[1])
        return task

    def _refill(self, now: int) -> None:
        skip = len(self.inflight)
        ids = self.store.pending_matches(self.region, MATCH_BATCH + skip)
        tasks = [("match", m) for m in ids if m not in self.inflight][:MATCH_BATCH]
        if not tasks:
            players = self.store.due_players(self.region, now, PLAYER_BATCH + skip)
            tasks = [("player", p, cu) for p, cu in players if p not in self.inflight][:PLAYER_BATCH]
        self.buffer.extend(tasks)


class Crawler:
    def __init__(self, client, store: Store, cfg: CrawlConfig, clock=time.time):
        self.client = client
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.queues = {r: _RegionQueue(store, r) for r in cfg.regions}
        self.seeding: set[str] = set(cfg.platforms)
        self.stop = asyncio.Event()
        self.stats = {"players_listed": 0, "match_ids_new": 0, "matches_in_patch": 0, "matches_other": 0}

    def now(self) -> int:
        return int(self.clock())

    # --- seeding ----------------------------------------------------------

    async def seed_platform(self, platform: str) -> None:
        region = PLATFORM_TO_REGION[platform]
        try:
            for tier in self.cfg.seed_tiers:
                if tier in APEX_TIERS:
                    await self._seed_apex(platform, region, tier)
                else:
                    for division in DIVISIONS:
                        await self._seed_division(platform, region, tier, division)
                if self.stop.is_set():
                    return
            log.info("seeding finished for %s", platform)
        except AuthError as exc:
            # 403 on one platform after the key was validated usually means the
            # platform isn't served; don't take the whole crawl down for it.
            log.error("seeding %s aborted: %s", platform, exc)
        except (TransientError, RiotAPIError) as exc:
            log.error("seeding %s aborted (rerun to resume): %s", platform, exc)
        finally:
            self.seeding.discard(platform)

    async def _seed_apex(self, platform: str, region: str, tier: str) -> None:
        _, finished = self.store.seed_state(platform, tier, "I")
        if finished:
            return
        league = await self.client.apex_league(platform, tier) or {}
        puuids = [e["puuid"] for e in league.get("entries", []) if e.get("puuid")]
        self.store.add_players(puuids, region, PRIO_FROM_LADDER, self.now())
        self.store.set_seed_state(platform, tier, "I", 2, True)
        log.info("seeded %d players from %s %s", len(puuids), platform, tier)

    async def _seed_division(self, platform: str, region: str, tier: str, division: str) -> None:
        page, finished = self.store.seed_state(platform, tier, division)
        total = 0
        while not finished and not self.stop.is_set():
            entries = await self.client.league_entries(platform, tier, division, page)
            if not entries:
                finished = True
            else:
                puuids = [e["puuid"] for e in entries if e.get("puuid")]
                self.store.add_players(puuids, region, PRIO_FROM_LADDER, self.now())
                total += len(puuids)
                page += 1
            self.store.set_seed_state(platform, tier, division, page, finished)
        if total:
            log.info("seeded %d players from %s %s %s", total, platform, tier, division)

    # --- workers ----------------------------------------------------------

    def _region_seeding(self, region: str) -> bool:
        return any(PLATFORM_TO_REGION[p] == region for p in self.seeding)

    async def worker(self, region: str) -> None:
        queue = self.queues[region]
        while not self.stop.is_set():
            task = queue.next_task(self.now())
            if task is None:
                if queue.inflight or self._region_seeding(region):
                    await asyncio.sleep(2)
                    continue
                if self.cfg.follow and self.now() < self.cfg.end_time:
                    await asyncio.sleep(60)
                    continue
                return
            try:
                if task[0] == "match":
                    await self._crawl_match(region, task[1])
                else:
                    await self._crawl_player(region, task[1], task[2])
            except AuthError:
                raise
            except (TransientError, RiotAPIError) as exc:
                log.warning("%s %s failed: %s", task[0], task[1], exc)
                if task[0] == "match":
                    self.store.mark_match_error(task[1])
                else:
                    self.store.mark_player_error(task[1], self.now() + PLAYER_ERROR_RETRY_S)
            finally:
                queue.inflight.discard(task[1])

    async def _crawl_player(self, region: str, puuid: str, crawled_until: int | None) -> None:
        now = self.now()
        end = min(self.cfg.end_time, now)
        start = self.cfg.start_time
        if crawled_until is not None:
            start = max(start, crawled_until - RECRAWL_OVERLAP_S)
        ids: list[str] = []
        offset = 0
        while start < end:
            page = await self.client.match_ids(region, puuid, start, end, offset, PAGE_SIZE)
            if not page:
                break
            ids += page
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        new = self.store.add_match_ids(ids, region) if ids else 0
        if end >= self.cfg.end_time:
            next_due = NEVER
        else:
            next_due = now + int(self.cfg.recrawl_hours * 3600)
        self.store.mark_player_crawled(puuid, end, next_due)
        self.stats["players_listed"] += 1
        self.stats["match_ids_new"] += new

    async def _crawl_match(self, region: str, match_id: str) -> None:
        data = await self.client.match(region, match_id)
        now = self.now()
        if data is None:
            self.store.mark_match(match_id, MISSING, now)
            return
        info = data.get("info", {})
        version = info.get("gameVersion", "")
        queue_id = info.get("queueId")
        creation = info.get("gameCreation")
        if queue_id == RANKED_SOLO_QUEUE_ID and version_matches(version, self.cfg.patch):
            self.store.save_match(match_id, data, version, queue_id, creation, now)
            participants = data.get("metadata", {}).get("participants", [])
            self.store.add_players([p for p in participants if p and p != "BOT"], region, PRIO_FROM_MATCH, now)
            self.stats["matches_in_patch"] += 1
            if self.cfg.max_matches and self.store.count_in_patch() >= self.cfg.max_matches:
                log.info("reached max_matches=%d, stopping", self.cfg.max_matches)
                self.stop.set()
        else:
            self.store.mark_match(match_id, OTHER, now, version, queue_id, creation)
            self.stats["matches_other"] += 1

    # --- driver -----------------------------------------------------------

    async def _report(self, interval: float) -> None:
        last = dict(self.client.request_counts)
        while True:
            await asyncio.sleep(interval)
            counts = dict(self.client.request_counts)
            rates = {h: (counts[h] - last.get(h, 0)) / interval for h in counts}
            last = counts
            log.info(
                "in_patch=%d other=%d players_listed=%d new_ids=%d seeding=%s req/s=%s",
                self.stats["matches_in_patch"], self.stats["matches_other"],
                self.stats["players_listed"], self.stats["match_ids_new"],
                sorted(self.seeding) or "done",
                {h: round(r, 1) for h, r in sorted(rates.items()) if r},
            )

    async def run(self, report_interval: float = 30.0) -> None:
        reporter = asyncio.create_task(self._report(report_interval))
        try:
            async with asyncio.TaskGroup() as tg:
                for platform in self.cfg.platforms:
                    tg.create_task(self.seed_platform(platform))
                for region in self.cfg.regions:
                    for _ in range(self.cfg.workers_per_region):
                        tg.create_task(self.worker(region))
        finally:
            reporter.cancel()
