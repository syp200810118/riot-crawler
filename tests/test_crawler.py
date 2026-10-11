"""End-to-end crawl against an in-memory fake of the Riot API."""

import asyncio

from conftest import make_match

from riot_crawler.crawler import CrawlConfig, Crawler
from riot_crawler.store import IN_PATCH, NEVER, OTHER, Store

START, END = 1_791_300_000, 1_792_600_000


class FakeClient:
    """Ladder has one Challenger player; everything else is reachable only by
    snowballing through match participants."""

    def __init__(self):
        self.request_counts = {}
        self.matches = {}
        self.history = {}  # puuid -> [match ids], newest first
        self.id_calls = []

        def add(mid, puuids, **kw):
            self.matches[mid] = make_match(mid, puuids, **kw)
            for p in puuids:
                self.history.setdefault(p, []).insert(0, mid)

        team_a = [f"a{i}" for i in range(10)]           # a0 is on the ladder
        team_b = ["a9"] + [f"b{i}" for i in range(9)]   # reachable only via a9
        add("NA1_1", team_a)
        add("NA1_2", team_b, blue_wins=False)
        add("NA1_3", [f"c{i}" for i in range(9)] + ["b8"], version="16.19.1.1")  # previous patch
        add("NA1_4", [f"d{i}" for i in range(9)] + ["c0"])  # only reachable via an out-of-patch match
        # Enough history for one player to need pagination.
        for k in range(150):
            add(f"NA1_{1000 + k}", ["b0"] + [f"x{k}_{j}" for j in range(9)], queue_id=420)

    async def apex_league(self, platform, tier):
        return {"entries": [{"puuid": "a0"}] if tier == "CHALLENGER" else []}

    async def league_entries(self, platform, tier, division, page):
        return []

    async def match_ids(self, region, puuid, start_time, end_time, start=0, count=100):
        self.id_calls.append((puuid, start_time, end_time))
        return self.history.get(puuid, [])[start:start + count]

    async def match(self, region, match_id):
        return self.matches.get(match_id)


def run(client, store, now, **kw):
    cfg = CrawlConfig(patch="16.20", start_time=START, end_time=END, platforms=["na1"],
                      seed_tiers=["CHALLENGER", "GRANDMASTER", "DIAMOND"], workers_per_region=4, **kw)
    crawler = Crawler(client, store, cfg, clock=lambda: now)
    asyncio.run(crawler.run(report_interval=3600))
    return crawler


def test_snowball_crawl(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    client = FakeClient()
    run(client, store, now=END + 10)

    status = dict(store.db.execute("SELECT match_id, status FROM matches").fetchall())
    assert status["NA1_1"] == IN_PATCH
    assert status["NA1_2"] == IN_PATCH          # found by snowballing through a9
    assert status["NA1_3"] == OTHER             # wrong patch: stored as seen, not kept
    assert "NA1_4" not in status                # out-of-patch matches don't expand the frontier
    assert sum(1 for k in status if k.startswith("NA1_1") and len(k) == 8) == 150  # pagination
    assert store.count_in_patch() == 152
    raw_ids = {mid for mid, _ in store.iter_raw_matches()}
    assert "NA1_3" not in raw_ids and "NA1_2" in raw_ids
    # Window closed: every crawled player is finished.
    assert store.db.execute("SELECT COUNT(*) FROM players WHERE next_due < ?", (NEVER,)).fetchone()[0] == 0


def test_live_patch_recrawl_is_incremental(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    client = FakeClient()
    mid = START + 500_000
    run(client, store, now=mid, recrawl_hours=1)
    first = {p: (s, e) for p, s, e in client.id_calls}
    assert first["a0"] == (START, mid)

    client.id_calls.clear()
    run(client, store, now=mid + 7200, recrawl_hours=1)
    again = {p: (s, e) for p, s, e in client.id_calls}
    assert again["a0"] == (mid - 2 * 3600, mid + 7200)   # only the uncovered part (+ overlap)


def test_max_matches_stops_early(tmp_path):
    store = Store(tmp_path / "c.sqlite")
    run(FakeClient(), store, now=END + 10, max_matches=5)
    assert 5 <= store.count_in_patch() < 20
