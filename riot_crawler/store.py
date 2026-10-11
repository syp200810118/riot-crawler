"""SQLite-backed crawl state: player frontier, match queue, raw match JSON.

Everything is committed as it happens, so a crawl can be killed at any point
and resumed by rerunning the same command.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from typing import Iterable, Iterator

# matches.status
PENDING, IN_PATCH, OTHER, MISSING, FAILED = 0, 1, 2, 3, 4
STATUS_NAMES = {PENDING: "pending", IN_PATCH: "in_patch", OTHER: "other_patch_or_queue", MISSING: "not_found", FAILED: "failed"}

# players.priority: players seen in an in-window match are known to be active,
# so they're crawled before players who only came from a ladder page.
PRIO_FROM_MATCH, PRIO_FROM_LADDER = 0, 1

NEVER = 2**62
MAX_ERRORS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS players(
    puuid         TEXT PRIMARY KEY,
    region        TEXT NOT NULL,
    priority      INTEGER NOT NULL,
    discovered_at INTEGER NOT NULL,
    crawled_until INTEGER,             -- epoch s; match list covered up to here
    next_due      INTEGER NOT NULL DEFAULT 0,
    n_errors      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS players_due ON players(region, priority, next_due);

CREATE TABLE IF NOT EXISTS matches(
    match_id      TEXT PRIMARY KEY,
    region        TEXT NOT NULL,
    status        INTEGER NOT NULL DEFAULT 0,
    game_version  TEXT,
    queue_id      INTEGER,
    game_creation INTEGER,             -- epoch ms
    fetched_at    INTEGER,
    n_errors      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS matches_status ON matches(region, status);

CREATE TABLE IF NOT EXISTS match_raw(
    match_id TEXT PRIMARY KEY,
    data     BLOB NOT NULL             -- zlib-compressed match-v5 JSON
);

CREATE TABLE IF NOT EXISTS seed_state(
    platform  TEXT NOT NULL,
    tier      TEXT NOT NULL,
    division  TEXT NOT NULL,
    next_page INTEGER NOT NULL DEFAULT 1,
    finished  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(platform, tier, division)
);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # --- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, value))

    # --- players ----------------------------------------------------------

    def add_players(self, puuids: Iterable[str], region: str, priority: int, now: int) -> None:
        with self.db:
            self.db.executemany(
                """INSERT INTO players(puuid, region, priority, discovered_at) VALUES(?, ?, ?, ?)
                   ON CONFLICT(puuid) DO UPDATE SET priority=MIN(priority, excluded.priority)""",
                [(p, region, priority, now) for p in puuids],
            )

    def due_players(self, region: str, now: int, limit: int) -> list[tuple[str, int | None]]:
        out: list[tuple[str, int | None]] = []
        for prio in (PRIO_FROM_MATCH, PRIO_FROM_LADDER):
            out += self.db.execute(
                """SELECT puuid, crawled_until FROM players
                   WHERE region=? AND priority=? AND next_due<=?
                   ORDER BY next_due LIMIT ?""",
                (region, prio, now, limit - len(out)),
            ).fetchall()
            if len(out) >= limit:
                break
        return out

    def mark_player_crawled(self, puuid: str, crawled_until: int, next_due: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE players SET crawled_until=?, next_due=?, n_errors=0 WHERE puuid=?",
                (crawled_until, next_due, puuid),
            )

    def reopen_players(self, end_time: int) -> None:
        """Make players whose coverage stops short of `end_time` due again."""
        with self.db:
            self.db.execute(
                "UPDATE players SET next_due=0 WHERE crawled_until < ? AND n_errors < ?",
                (end_time, MAX_ERRORS),
            )

    def mark_player_error(self, puuid: str, retry_at: int) -> None:
        with self.db:
            self.db.execute(
                """UPDATE players SET n_errors=n_errors+1,
                   next_due=CASE WHEN n_errors+1>=? THEN ? ELSE ? END WHERE puuid=?""",
                (MAX_ERRORS, NEVER, retry_at, puuid),
            )

    # --- matches ----------------------------------------------------------

    def add_match_ids(self, match_ids: Iterable[str], region: str) -> int:
        with self.db:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO matches(match_id, region) VALUES(?, ?)",
                [(m, region) for m in match_ids],
            )
            return self.db.total_changes - before

    def pending_matches(self, region: str, limit: int) -> list[str]:
        rows = self.db.execute(
            "SELECT match_id FROM matches WHERE region=? AND status=? LIMIT ?", (region, PENDING, limit)
        ).fetchall()
        return [r[0] for r in rows]

    def save_match(self, match_id: str, data: dict, version: str, queue_id: int, creation: int, now: int) -> None:
        blob = zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 6)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO match_raw VALUES(?, ?)", (match_id, blob))
            self.db.execute(
                """UPDATE matches SET status=?, game_version=?, queue_id=?, game_creation=?, fetched_at=?
                   WHERE match_id=?""",
                (IN_PATCH, version, queue_id, creation, now, match_id),
            )

    def mark_match(self, match_id: str, status: int, now: int, version=None, queue_id=None, creation=None) -> None:
        with self.db:
            self.db.execute(
                """UPDATE matches SET status=?, game_version=?, queue_id=?, game_creation=?, fetched_at=?
                   WHERE match_id=?""",
                (status, version, queue_id, creation, now, match_id),
            )

    def mark_match_error(self, match_id: str) -> None:
        with self.db:
            self.db.execute(
                """UPDATE matches SET n_errors=n_errors+1,
                   status=CASE WHEN n_errors+1>=? THEN ? ELSE status END WHERE match_id=?""",
                (MAX_ERRORS, FAILED, match_id),
            )

    def iter_raw_matches(self) -> Iterator[tuple[str, dict]]:
        for match_id, blob in self.db.execute("SELECT match_id, data FROM match_raw ORDER BY match_id"):
            yield match_id, json.loads(zlib.decompress(blob))

    def count_in_patch(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM matches WHERE status=?", (IN_PATCH,)).fetchone()[0]

    # --- ladder seeding ---------------------------------------------------

    def seed_state(self, platform: str, tier: str, division: str) -> tuple[int, bool]:
        row = self.db.execute(
            "SELECT next_page, finished FROM seed_state WHERE platform=? AND tier=? AND division=?",
            (platform, tier, division),
        ).fetchone()
        return (row[0], bool(row[1])) if row else (1, False)

    def set_seed_state(self, platform: str, tier: str, division: str, next_page: int, finished: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO seed_state VALUES(?, ?, ?, ?, ?)",
                (platform, tier, division, next_page, int(finished)),
            )

    def reset_seeds(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM seed_state")

    # --- reporting --------------------------------------------------------

    def summary(self, now: int) -> dict:
        q = lambda sql, *a: self.db.execute(sql, a).fetchall()
        by_patch: dict[str, int] = {}
        for version, n in q("SELECT game_version, COUNT(*) FROM matches WHERE game_version IS NOT NULL GROUP BY 1"):
            patch = ".".join(version.split(".")[:2])
            by_patch[patch] = by_patch.get(patch, 0) + n
        return {
            "matches_by_status": {STATUS_NAMES[s]: n for s, n in q("SELECT status, COUNT(*) FROM matches GROUP BY status")},
            "matches_by_region": dict(q("SELECT region, COUNT(*) FROM matches WHERE status=? GROUP BY region", IN_PATCH)),
            "fetched_by_patch": dict(sorted(by_patch.items(), key=lambda kv: -kv[1])),
            "in_patch_creation_range_ms": q("SELECT MIN(game_creation), MAX(game_creation) FROM matches WHERE status=?", IN_PATCH)[0],
            "players_total": q("SELECT COUNT(*) FROM players")[0][0],
            "players_crawled": q("SELECT COUNT(*) FROM players WHERE crawled_until IS NOT NULL")[0][0],
            "players_due_now": q("SELECT COUNT(*) FROM players WHERE next_due<=?", now)[0][0],
            "players_failed": q("SELECT COUNT(*) FROM players WHERE n_errors>=?", MAX_ERRORS)[0][0],
            "seed_pages_finished": q("SELECT COUNT(*) FROM seed_state WHERE finished=1")[0][0],
        }
