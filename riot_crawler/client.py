"""Async Riot API client with per-host app limits and per-method limits."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from typing import Any

import aiohttp

from .ratelimit import DEV_KEY_APP_LIMITS, SlidingWindowLimiter, parse_limit_header
from .routing import RANKED_SOLO_QUEUE, RANKED_SOLO_QUEUE_ID, host

log = logging.getLogger(__name__)


class RiotAPIError(Exception):
    pass


class AuthError(RiotAPIError):
    """401/403: missing, invalid, or expired key (dev keys expire every 24 h)."""


class TransientError(RiotAPIError):
    """Still failing after all retries (5xx, timeouts, connection errors)."""


class RiotClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession, max_retries: int = 6):
        self._key = api_key
        self._session = session
        self._max_retries = max_retries
        self._app_limiters: dict[str, SlidingWindowLimiter] = {}
        self._method_limiters: dict[tuple[str, str], SlidingWindowLimiter] = {}
        self.request_counts: Counter[str] = Counter()

    def _app_limiter(self, routing: str) -> SlidingWindowLimiter:
        if routing not in self._app_limiters:
            self._app_limiters[routing] = SlidingWindowLimiter(DEV_KEY_APP_LIMITS)
        return self._app_limiters[routing]

    def _method_limiter(self, routing: str, method: str) -> SlidingWindowLimiter:
        key = (routing, method)
        if key not in self._method_limiters:
            # Unknown until the first response tells us; app limit governs meanwhile.
            self._method_limiters[key] = SlidingWindowLimiter()
        return self._method_limiters[key]

    async def get(self, routing: str, path: str, method: str, params: dict | None = None) -> Any:
        """GET a Riot endpoint. Returns parsed JSON, or None on 404."""
        app = self._app_limiter(routing)
        meth = self._method_limiter(routing, method)
        url = host(routing) + path
        backoff = 1.0
        failures = 0
        while True:
            await meth.acquire()
            await app.acquire()
            self.request_counts[routing] += 1
            try:
                async with self._session.get(
                    url, params=params, headers={"X-Riot-Token": self._key}
                ) as resp:
                    app.set_limits(parse_limit_header(resp.headers.get("X-App-Rate-Limit")))
                    meth.set_limits(parse_limit_header(resp.headers.get("X-Method-Rate-Limit")))

                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 404:
                        return None
                    if resp.status in (401, 403):
                        raise AuthError(f"{resp.status} for {url}: {await resp.text()}")
                    if resp.status == 400:
                        raise RiotAPIError(f"400 for {url} {params}: {await resp.text()}")
                    if resp.status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        limit_type = resp.headers.get("X-Rate-Limit-Type", "service")
                        if retry_after is not None:
                            wait = float(retry_after)
                            if limit_type == "application":
                                app.block_for(wait)
                            elif limit_type == "method":
                                meth.block_for(wait)
                            else:
                                await asyncio.sleep(wait)
                            log.warning("429 (%s) on %s %s, waiting %.1fs", limit_type, routing, method, wait)
                            continue
                        # Service-level 429 without Retry-After: plain backoff.
                    elif resp.status < 500:
                        raise RiotAPIError(f"{resp.status} for {url}: {await resp.text()}")
                    log.debug("HTTP %s on %s", resp.status, url)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.debug("network error on %s: %r", url, exc)
            # 429s with Retry-After `continue` above and don't count as failures.
            failures += 1
            if failures > self._max_retries:
                raise TransientError(f"giving up on {url} after {failures} failed attempts")
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    # --- league-v4 (platform routing) -------------------------------------

    async def apex_league(self, platform: str, tier: str) -> dict | None:
        name = {"CHALLENGER": "challengerleagues", "GRANDMASTER": "grandmasterleagues", "MASTER": "masterleagues"}[tier]
        return await self.get(
            platform, f"/lol/league/v4/{name}/by-queue/{RANKED_SOLO_QUEUE}", f"league.{name}"
        )

    async def league_entries(self, platform: str, tier: str, division: str, page: int) -> list | None:
        return await self.get(
            platform,
            f"/lol/league/v4/entries/{RANKED_SOLO_QUEUE}/{tier}/{division}",
            "league.entries",
            {"page": page},
        )

    # --- match-v5 (regional routing) --------------------------------------

    async def match_ids(
        self, region: str, puuid: str, start_time: int, end_time: int, start: int = 0, count: int = 100
    ) -> list[str] | None:
        return await self.get(
            region,
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
            "match.ids",
            {
                "queue": RANKED_SOLO_QUEUE_ID,
                "type": "ranked",
                "startTime": start_time,
                "endTime": end_time,
                "start": start,
                "count": count,
            },
        )

    async def match(self, region: str, match_id: str) -> dict | None:
        return await self.get(region, f"/lol/match/v5/matches/{match_id}", "match.get")
