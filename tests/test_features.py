from conftest import make_match

from riot_crawler.features import COLUMNS, exclusion_reason, match_to_row

PUUIDS = [f"p{i}" for i in range(10)]


def test_row_layout_and_label():
    m = make_match("NA1_1", PUUIDS, blue_wins=False)
    assert exclusion_reason(m, "16.20") is None
    row = match_to_row(m)
    assert list(row) == COLUMNS
    assert row["blue_win"] == 0
    assert row["blue_top_champ"] == 1 and row["red_support_champ"] == 10
    assert [row[f"blue_ban_{i}"] for i in range(1, 6)] == [101, 102, 103, 104, 105]
    assert row["red_ban_5"] == -1
    assert row["blue_top_spells"] == "4-11"
    assert row["roles_from_api"] == 1


def test_roles_assigned_by_position_not_order():
    pos = ["UTILITY", "TOP", "JUNGLE", "MIDDLE", "BOTTOM"] * 2
    row = match_to_row(make_match("NA1_2", PUUIDS, positions=pos))
    assert row["blue_support_champ"] == 1
    assert row["blue_top_champ"] == 2


def test_role_fallback_flagged():
    pos = ["TOP", "TOP", "JUNGLE", "MIDDLE", "BOTTOM"] + ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    row = match_to_row(make_match("NA1_3", PUUIDS, positions=pos))
    assert row["roles_from_api"] == 0


def test_exclusions():
    assert exclusion_reason(make_match("NA1_4", PUUIDS, remake=True)) == "remake"
    assert exclusion_reason(make_match("NA1_5", PUUIDS, duration=200)) == "remake"
    assert exclusion_reason(make_match("NA1_6", PUUIDS, queue_id=440)) == "not_ranked_solo"
    assert exclusion_reason(make_match("NA1_7", PUUIDS, version="16.19.1.1"), "16.20") == "wrong_patch"
