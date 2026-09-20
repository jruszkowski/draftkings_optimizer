"""
Downloads this week's NFL player projections from ESPN Fantasy Football.
Uses ESPN's internal Fantasy Football v3 API (the same API the projections page calls).
Saves results to espn_projections.csv.
"""

import json
import math
import requests
import pandas as pd
from datetime import date

SEASON = 2026
PAGE_SIZE = 300  # players per API page

# ESPN position ID -> label
POSITION_MAP = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "D/ST",
}

# ESPN pro team ID -> abbreviation
PRO_TEAM_MAP = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE",
    6: "DAL", 7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND",
    12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE",
    18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT",
    24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}


def get_current_nfl_week() -> int:
    """
    Estimate the current NFL scoring period (week) based on today's date.
    Adjust WEEK1_START if the season start date changes.
    """
    week1_start = date(2026, 9, 4)  # Thursday of Week 1
    today = date.today()
    delta_days = (today - week1_start).days
    if delta_days < 0:
        return 1
    week = math.floor(delta_days / 7) + 1
    return max(1, min(week, 18))


def _build_filter(scoring_period: int, limit: int, offset: int) -> str:
    """Build the x-fantasy-filter header value."""
    # The sortAppliedStatTotal value key encodes season+week for sorting
    sort_key = f"10{SEASON}{scoring_period:02d}"
    filt = {
        "players": {
            "filterSlotIds": {
                "value": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                           13, 14, 15, 16, 17, 18, 19, 23, 24]
            },
            "sortPercOwned": {"sortPriority": 3, "sortAsc": False},
            "limit": limit,
            "offset": offset,
            "sortAppliedStatTotal": {
                "sortAsc": False,
                "sortPriority": 2,
                "value": sort_key,
            },
            "filterStatsForTopScorersByScoringPeriodIds": {"value": [scoring_period]},
            "filterRanksForScoringPeriodIds": {"value": [scoring_period]},
            "filterStatsForScoringPeriodIds": {"value": [scoring_period]},
        }
    }
    return json.dumps(filt, separators=(",", ":"))


def _parse_players(raw_entries: list, scoring_period: int) -> list[dict]:
    """Parse raw API player entries into clean projection dicts."""
    # Stat block ID that identifies current-season PPR projections:
    # format is  11{SEASON}{scoring_period}  (no zero-padding on period)
    target_block_id = str(f"11{SEASON}{scoring_period}")

    rows = []
    for entry in raw_entries:
        # Data lives under entry["player"] (not playerPoolEntry)
        player = entry.get("player", {})
        if not player:
            player = entry.get("playerPoolEntry", {}).get("player", {})

        name = player.get("fullName", "Unknown")
        pos_id = player.get("defaultPositionId", 0)
        position = POSITION_MAP.get(pos_id, f"POS{pos_id}")
        pro_team_id = player.get("proTeamId", 0)
        team = PRO_TEAM_MAP.get(pro_team_id, str(pro_team_id))

        # Find the projection stat block for the current season/week
        projected_points = None
        raw_stats: dict = {}
        stats_list = player.get("stats", []) or entry.get("playerPoolEntry", {}).get("stats", [])
        for block in stats_list:
            block_id = str(block.get("id", ""))
            if block_id == target_block_id:
                projected_points = block.get("appliedTotal")
                raw_stats = block.get("stats", {})
                break

        # Fallback: any projection block (statSourceId=1) for this period
        if projected_points is None:
            for block in stats_list:
                if (
                    block.get("statSourceId") == 1
                    and block.get("scoringPeriodId") == scoring_period
                ):
                    projected_points = block.get("appliedTotal")
                    raw_stats = block.get("stats", {})
                    break

        if projected_points is None or projected_points == 0:
            continue

        row = {
            "name": name,
            "position": position,
            "team": team,
            "projected_points": round(projected_points, 2),
        }

        # Shared receiving stats (RB / WR / TE)
        if pos_id in (2, 3, 4):
            row["receptions"] = round(raw_stats.get("41", 0), 1)
            row["rec_yards"] = round(raw_stats.get("42", 0), 1)
            row["rec_tds"] = round(raw_stats.get("43", 0), 2)

        # Rushing stats (QB / RB)
        if pos_id in (1, 2):
            row["rush_attempts"] = round(raw_stats.get("24", 0), 1)
            row["rush_yards"] = round(raw_stats.get("25", 0), 1)
            row["rush_tds"] = round(raw_stats.get("26", 0), 2)

        # Passing stats (QB)
        if pos_id == 1:
            row["pass_completions"] = round(raw_stats.get("6", 0), 1)
            row["pass_attempts"] = round(raw_stats.get("7", 0), 1)
            row["pass_yards"] = round(raw_stats.get("3", 0), 1)
            row["pass_tds"] = round(raw_stats.get("4", 0), 2)
            row["interceptions"] = round(raw_stats.get("5", 0), 2)

        rows.append(row)

    return rows


def fetch_espn_projections(scoring_period: int) -> list[dict]:
    """
    Fetch all player projections from ESPN's Fantasy Football API,
    handling pagination automatically.
    """
    url = (
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
        f"/seasons/{SEASON}/segments/0/leaguedefaults/3"
    )
    base_headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    params = {
        "view": "kona_player_info",
        "scoringPeriodId": scoring_period,
    }

    all_rows: list[dict] = []
    offset = 0

    while True:
        headers = {**base_headers, "x-fantasy-filter": _build_filter(scoring_period, PAGE_SIZE, offset)}
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        raw_entries = data.get("players", [])
        if not raw_entries:
            break

        page_rows = _parse_players(raw_entries, scoring_period)
        all_rows.extend(page_rows)

        if len(raw_entries) < PAGE_SIZE:
            break  # last page
        offset += PAGE_SIZE

    all_rows.sort(key=lambda r: r["projected_points"], reverse=True)
    return all_rows


def download_projections(scoring_period: int | None = None) -> pd.DataFrame:
    """
    Download ESPN projections for the given week (defaults to current week).
    Saves to espn_projections.csv and returns a DataFrame.
    """
    if scoring_period is None:
        scoring_period = get_current_nfl_week()

    print(f"Fetching ESPN projections — Season {SEASON}, Week {scoring_period} ...")
    rows = fetch_espn_projections(scoring_period)

    if not rows:
        print("No projection data returned. The season may not have started yet.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    out_file = "espn_projections.csv"
    df.to_csv(out_file, index=False)
    print(f"Saved {len(df)} player projections to {out_file}")
    print(df[["name", "position", "team", "projected_points"]].head(20).to_string(index=False))
    return df


if __name__ == "__main__":
    download_projections()
