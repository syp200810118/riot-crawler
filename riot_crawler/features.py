"""Turn raw match-v5 JSON into one draft-only training row per match.

Label: blue_win = 1 if team 100 (Blue) won, else 0 (Red won).

Only information known when champion select ends is emitted (picks by role,
bans, summoner spells). Nothing from in-game stats, which would leak the outcome.
"""

from __future__ import annotations

POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
POS_COL = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "bot", "UTILITY": "support"}
SIDES = {100: "blue", 200: "red"}
NO_BAN = -1
REMAKE_MAX_DURATION_S = 300

COLUMNS = (
    ["match_id", "platform", "game_version", "game_creation_ms", "game_duration_s"]
    + [f"{side}_{POS_COL[p]}_champ" for side in SIDES.values() for p in POSITIONS]
    + [f"{side}_ban_{i}" for side in SIDES.values() for i in range(1, 6)]
    + [f"{side}_{POS_COL[p]}_spells" for side in SIDES.values() for p in POSITIONS]
    + ["roles_from_api", "blue_win"]
)


def exclusion_reason(match: dict, patch: str | None = None) -> str | None:
    """Why this match shouldn't be a training example, or None if it's fine."""
    info = match.get("info") or {}
    if info.get("queueId") != 420:
        return "not_ranked_solo"
    if info.get("mapId") != 11:
        return "not_summoners_rift"
    if patch and info.get("gameVersion", "").split(".")[:2] != patch.split(".")[:2]:
        return "wrong_patch"
    parts = info.get("participants") or []
    if len(parts) != 10 or sorted(p.get("teamId") for p in parts) != [100] * 5 + [200] * 5:
        return "bad_participants"
    if any(p.get("gameEndedInEarlySurrender") for p in parts) or info.get("gameDuration", 0) < REMAKE_MAX_DURATION_S:
        return "remake"
    teams = {t.get("teamId"): t for t in info.get("teams") or []}
    if set(teams) != {100, 200} or teams[100].get("win") == teams[200].get("win"):
        return "no_single_winner"
    return None


def _assign_roles(team_parts: list[dict]) -> tuple[dict[str, dict], bool]:
    """Map position -> participant. Falls back to participant order (which
    follows draft order, not role) when teamPosition isn't a clean 1-of-each."""
    by_pos = {p.get("teamPosition"): p for p in team_parts}
    if set(by_pos) == set(POSITIONS):
        return by_pos, True
    ordered = sorted(team_parts, key=lambda p: p.get("participantId", 0))
    return dict(zip(POSITIONS, ordered)), False


def match_to_row(match: dict) -> dict:
    info = match["info"]
    row: dict = {
        "match_id": match["metadata"]["matchId"],
        "platform": info.get("platformId"),
        "game_version": info.get("gameVersion"),
        "game_creation_ms": info.get("gameCreation"),
        "game_duration_s": info.get("gameDuration"),
    }
    roles_ok = True
    teams = {t["teamId"]: t for t in info["teams"]}
    for team_id, side in SIDES.items():
        parts = [p for p in info["participants"] if p["teamId"] == team_id]
        roles, ok = _assign_roles(parts)
        roles_ok &= ok
        for pos in POSITIONS:
            p = roles[pos]
            row[f"{side}_{POS_COL[pos]}_champ"] = p["championId"]
            # Order-independent spell pair, e.g. "4-14" (Flash + Ignite).
            s1, s2 = sorted((p.get("summoner1Id", 0), p.get("summoner2Id", 0)))
            row[f"{side}_{POS_COL[pos]}_spells"] = f"{s1}-{s2}"
        bans = sorted(teams[team_id].get("bans") or [], key=lambda b: b.get("pickTurn", 0))
        ban_ids = [b.get("championId", NO_BAN) for b in bans][:5]
        ban_ids += [NO_BAN] * (5 - len(ban_ids))
        for i, champ in enumerate(ban_ids, 1):
            row[f"{side}_ban_{i}"] = champ
    row["roles_from_api"] = int(roles_ok)
    row["blue_win"] = int(bool(teams[100]["win"]))
    return {c: row[c] for c in COLUMNS}
