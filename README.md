# riot-crawler

Collects every discoverable **Ranked Solo/Duo** match from one League of Legends
patch using Riot's official API. It then exports a **draft-only** table for a
Blue-side win-probability model (gradient boosted trees, label `blue_win`).

Default target: **patch 26.20**. The API reports it as `gameVersion` `16.20.x`.
It went live Oct 7 2026; 26.21 is due Oct 21.

## Setup

```bash
pip install -r requirements.txt
export RIOT_API_KEY=RGAPI-...        # PowerShell: $env:RIOT_API_KEY="RGAPI-..."
```

## Usage

```bash
# Crawl all platforms (resumable: Ctrl+C anytime, rerun the same command)
python -m riot_crawler crawl

# Limit scope / keep running until the patch window closes
python -m riot_crawler crawl --platforms na1 euw1 kr --follow

# Progress
python -m riot_crawler stats

# Build the training table (.csv, or .parquet with pyarrow installed)
python -m riot_crawler export --out data/draft_16_20.csv --require-roles
```

## How discovery works

1. **Seed** players from every ladder page, Challenger down to Iron
   (`league-v4`, platform hosts).
2. **List** each player's match IDs with `queue=420`, limited to the patch
   window (`match-v5 /by-puuid/{puuid}/ids`, regional hosts, paginated).
3. **Fetch** each new match. It's kept only if `queueId == 420` and
   `gameVersion` starts with `16.20.`. The game's 10 participants are then added
   to the player list (snowball).
4. Players found inside an in-patch match are known to be active, so they're
   crawled before ladder-only players.
5. **Live patch:** while the window is still open, players are re-listed every
   `--recrawl-hours` (default 24). Only the time not yet covered is requested,
   with a 2 h overlap. Run again after the window closes (or use `--follow`) to
   pick up the final days.

The date window (`--start/--end`, padded around the patch dates) only limits
which IDs get listed. **`gameVersion` decides what is kept**, so regional
rollout timing can't leak 16.19/16.21 games into the data.

Rate limits follow Riot's response headers (`X-App-Rate-Limit`,
`X-Method-Rate-Limit`). Each routing host has its own sliding-window limiter.
A 429 response honors `Retry-After`. 5xx and network errors retry with
exponential backoff. 401/403 stops the crawl, because dev keys expire every
24 hours.

## Throughput reality check

| Key type | Limit per region | ≈ requests/day/region |
|---|---|---|
| Development / personal | 20/1 s, 100/2 min | ~72 k |
| Production | e.g. 500/10 s | ~4.3 M |

Each kept match costs about 1 match request plus its share of
match-list requests. A dev key collects tens of thousands of matches per region
per patch. Getting "every match" from a whole region needs a **production key**,
which you apply for on the Riot developer portal. Ladder seeding uses platform
hosts, so it doesn't compete with match fetching. Your use must comply with
Riot's API policies.

## Exported columns

| Column | Meaning |
|---|---|
| `{blue,red}_{top,jungle,mid,bot,support}_champ` | championId by `teamPosition` |
| `{blue,red}_ban_{1..5}` | banned championId in pick-turn order, `-1` = no ban |
| `{blue,red}_{role}_spells` | summoner spell pair, order-independent, e.g. `4-14` |
| `roles_from_api` | 0 if `teamPosition` wasn't one of each role (fell back to participant order) |
| `blue_win` | **label**: 1 = Blue (team 100) won, 0 = Red won |
| `match_id, platform, game_version, game_creation_ms, game_duration_s` | metadata only; don't use as features |

Excluded automatically: remakes (`gameEndedInEarlySurrender` or < 5 min),
non-420 queues, non-Summoner's-Rift games, wrong patch, malformed games.

### Modeling notes
- Treat champion IDs as **categorical** (e.g., LightGBM/CatBoost categorical
  features, or role-agnostic per-champion one-hot/multi-hot counts per side).
  Their numeric order is meaningless.
- Draft-only features yield modest AUC (roughly 0.52–0.56 is typical). Blue
  wins slightly more than half of games, so compare against a constant-rate
  baseline, and use log loss / Brier score and a calibration plot, since the
  output is a probability.
- Split by `game_creation_ms` (time-based) or at least by `match_id`. Each match
  is one row, so random splits don't duplicate games.
- Raw match JSON is kept (zlib-compressed) in the `match_raw` table, so new
  features can be added without re-crawling.

## Layout

```
riot_crawler/
  client.py     async Riot API client, retries, 429 handling
  ratelimit.py  multi-window sliding limiter
  crawler.py    seeding + snowball crawl + live-patch re-crawl
  store.py      SQLite state (resumable)
  features.py   match JSON -> draft row + exclusion rules
  cli.py        crawl / stats / export
tests/          offline tests (fake API + local HTTP server)
```

Run tests: `python -m pytest -q`
