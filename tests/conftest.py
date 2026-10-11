import itertools

POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


def make_match(match_id, puuids, version="16.20.712.3456", queue_id=420, blue_wins=True,
               duration=1800, remake=False, positions=None, creation=1_791_400_000_000):
    """Minimal match-v5 payload with the fields the crawler and features use."""
    positions = positions or POSITIONS * 2
    champ = itertools.count(1)
    participants = []
    for i, puuid in enumerate(puuids):
        participants.append({
            "participantId": i + 1,
            "puuid": puuid,
            "teamId": 100 if i < 5 else 200,
            "teamPosition": positions[i],
            "championId": next(champ),
            "summoner1Id": 14 if i % 2 else 4,
            "summoner2Id": 4 if i % 2 else 11,
            "gameEndedInEarlySurrender": remake,
        })
    return {
        "metadata": {"matchId": match_id, "participants": list(puuids)},
        "info": {
            "gameVersion": version,
            "queueId": queue_id,
            "mapId": 11,
            "platformId": match_id.split("_")[0],
            "gameCreation": creation,
            "gameDuration": duration,
            "participants": participants,
            "teams": [
                {"teamId": 100, "win": blue_wins,
                 "bans": [{"championId": 100 + k, "pickTurn": k} for k in range(1, 6)]},
                {"teamId": 200, "win": not blue_wins,
                 "bans": [{"championId": 200 + k, "pickTurn": k + 5} for k in range(1, 5)]
                         + [{"championId": -1, "pickTurn": 10}]},
            ],
        },
    }
