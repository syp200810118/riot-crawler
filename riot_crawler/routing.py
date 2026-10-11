"""Platform / regional routing values for the Riot API.

league-v4 is served from *platform* hosts (na1, euw1, ...), match-v5 from
*regional* hosts (americas, europe, asia, sea). Rate limits are enforced per
routing value, so each host gets its own limiter.
"""

PLATFORM_TO_REGION = {
    "na1": "americas",
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "euw1": "europe",
    "eun1": "europe",
    "tr1": "europe",
    "ru": "europe",
    "me1": "europe",
    "kr": "asia",
    "jp1": "asia",
    "oc1": "sea",
    "sg2": "sea",
    "tw2": "sea",
    "vn2": "sea",
}

ALL_PLATFORMS = list(PLATFORM_TO_REGION)

APEX_TIERS = ["CHALLENGER", "GRANDMASTER", "MASTER"]
DIVISION_TIERS = ["DIAMOND", "EMERALD", "PLATINUM", "GOLD", "SILVER", "BRONZE", "IRON"]
ALL_TIERS = APEX_TIERS + DIVISION_TIERS
DIVISIONS = ["I", "II", "III", "IV"]

RANKED_SOLO_QUEUE = "RANKED_SOLO_5x5"
RANKED_SOLO_QUEUE_ID = 420


def host(routing_value: str) -> str:
    return f"https://{routing_value}.api.riotgames.com"
