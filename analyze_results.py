import re
from dataclasses import dataclass
from pathlib import Path

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

from local_settings import CREDENTIALS_FILE, SHEET_URL

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

POSITION_TOKENS = ["DST", "FLEX", "QB", "RB", "TE", "WR"]
RESULTS_DIR = Path("results")


@dataclass
class ResultSummary:
    field_count: int
    top_count: int
    mean_points_field: float
    mean_points_top: float
    mean_ownership_field: float
    mean_ownership_top: float
    mean_salary_field: float
    mean_salary_top: float


def authenticate_gspread() -> gspread.Client:
    credentials = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(credentials)


def load_results_sheet() -> pd.DataFrame:
    client = authenticate_gspread()
    sheet = client.open_by_url(SHEET_URL)
    ws = sheet.worksheet("results")
    values = ws.get_all_values()
    if not values:
        raise ValueError("results worksheet is empty")

    df = pd.DataFrame(values[1:], columns=values[0])
    keep_cols = ["Rank", "EntryId", "EntryName", "TimeRemaining", "Points", "Lineup", "Player", "Roster Position", "%Drafted", "FPTS"]
    return df[keep_cols].copy()


def load_draftkings_context() -> pd.DataFrame:
    client = authenticate_gspread()
    sheet = client.open_by_url(SHEET_URL)
    ws = sheet.worksheet("draftkings")
    values = ws.get_all_values()
    if not values:
        return pd.DataFrame(columns=["player", "team", "salary", "game_info", "position"])

    dk = pd.DataFrame(values[1:], columns=values[0])
    required = ["Name", "TeamAbbrev", "Salary", "Game Info", "Position"]
    for col in required:
        if col not in dk.columns:
            return pd.DataFrame(columns=["player", "team", "salary", "game_info", "position"])

    dk = dk[required].copy()
    dk = dk.rename(
        columns={
            "Name": "player",
            "TeamAbbrev": "team",
            "Salary": "salary",
            "Game Info": "game_info",
            "Position": "position",
        }
    )
    dk["salary"] = pd.to_numeric(dk["salary"], errors="coerce")
    dk["team"] = dk["team"].astype(str).str.strip().str.upper()
    dk = dk.drop_duplicates("player")
    return dk


def parse_lineup_slots(lineup: str) -> list[tuple[str, str]]:
    tokens = lineup.split()
    slots: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token not in POSITION_TOKENS:
            i += 1
            continue

        j = i + 1
        player_parts = []
        while j < len(tokens) and tokens[j] not in POSITION_TOKENS:
            player_parts.append(tokens[j])
            j += 1

        player_name = " ".join(player_parts).strip()
        if player_name:
            slots.append((token, player_name))
        i = j

    return slots


def build_lineup_player_frame(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in df.iterrows():
        slots = parse_lineup_slots(str(r["Lineup"]))
        for slot, player in slots:
            rows.append(
                {
                    "EntryName": r["EntryName"],
                    "Rank": r["Rank"],
                    "Points": r["Points"],
                    "slot": slot,
                    "player": player,
                }
            )

    lineup_players = pd.DataFrame(rows)
    if lineup_players.empty:
        return lineup_players

    lineup_players["Rank"] = pd.to_numeric(lineup_players["Rank"], errors="coerce")
    lineup_players["Points"] = pd.to_numeric(lineup_players["Points"], errors="coerce")
    return lineup_players


def clean_player_stats(df: pd.DataFrame) -> pd.DataFrame:
    stats = df[["Player", "Roster Position", "%Drafted", "FPTS"]].copy()
    stats = stats.rename(columns={"Player": "player", "Roster Position": "roster_position", "%Drafted": "pct_drafted", "FPTS": "fpts"})
    stats["pct_drafted"] = stats["pct_drafted"].astype(str).str.replace("%", "", regex=False)
    stats["pct_drafted"] = pd.to_numeric(stats["pct_drafted"], errors="coerce")
    stats["fpts"] = pd.to_numeric(stats["fpts"], errors="coerce")

    # Multiple rows can contain the same player with different roster-position labels.
    # Keep one record per player so lineup joins stay 1-to-1 per parsed lineup slot.
    stats = stats.groupby(["player"], as_index=False).agg({"pct_drafted": "max", "fpts": "max"})
    return stats


def summarize_lineups(lineup_players: pd.DataFrame, player_stats: pd.DataFrame) -> pd.DataFrame:
    joined = lineup_players.merge(player_stats, on="player", how="left")

    lineup_summary = joined.groupby(["EntryName", "Rank", "Points"], as_index=False).agg(
        lineup_ownership_mean=("pct_drafted", "mean"),
        lineup_ownership_sum=("pct_drafted", "sum"),
        lineup_player_fpts_sum=("fpts", "sum"),
        player_count=("player", "count"),
    )

    return lineup_summary, joined


def _extract_opponent(game_info: str, team: str) -> str | None:
    if not isinstance(game_info, str) or "@" not in game_info:
        return None
    m = re.search(r"([A-Z]{2,3})@([A-Z]{2,3})", game_info)
    if not m:
        return None
    away, home = m.group(1), m.group(2)
    if team == away:
        return home
    if team == home:
        return away
    return None


def summarize_top_vs_field(lineup_summary: pd.DataFrame, top_n: int) -> ResultSummary:
    lineup_summary = lineup_summary.sort_values(["Rank", "Points"], ascending=[True, False])
    field = lineup_summary.copy()
    top = lineup_summary.head(top_n).copy()

    return ResultSummary(
        field_count=len(field),
        top_count=len(top),
        mean_points_field=field["Points"].mean(),
        mean_points_top=top["Points"].mean(),
        mean_ownership_field=field["lineup_ownership_mean"].mean(),
        mean_ownership_top=top["lineup_ownership_mean"].mean(),
        mean_salary_field=float("nan"),
        mean_salary_top=float("nan"),
    )


def top_player_leverage(joined: pd.DataFrame, top_n: int) -> pd.DataFrame:
    all_entries = joined["EntryName"].nunique()

    top_entries = (
        joined[["EntryName", "Rank"]]
        .drop_duplicates()
        .sort_values("Rank")
        .head(top_n)["EntryName"]
        .tolist()
    )

    top_joined = joined[joined["EntryName"].isin(top_entries)].copy()
    top_entry_count = len(set(top_entries))

    all_rate = (
        joined.groupby("player")["EntryName"]
        .nunique()
        .div(all_entries)
        .rename("field_lineup_rate")
    )
    top_rate = (
        top_joined.groupby("player")["EntryName"]
        .nunique()
        .div(max(top_entry_count, 1))
        .rename("top_lineup_rate")
    )

    leverage = pd.concat([all_rate, top_rate], axis=1).fillna(0.0)
    leverage["leverage"] = leverage["top_lineup_rate"] - leverage["field_lineup_rate"]

    # Attach player-level ownership/FPTS for context.
    meta = joined[["player", "pct_drafted", "fpts"]].drop_duplicates("player")
    leverage = leverage.reset_index().merge(meta, on="player", how="left")
    leverage = leverage.sort_values("leverage", ascending=False)
    return leverage


def slot_scoring_insights(joined: pd.DataFrame, top_n: int) -> pd.DataFrame:
    top_entries = (
        joined[["EntryName", "Rank"]]
        .drop_duplicates()
        .sort_values("Rank")
        .head(top_n)["EntryName"]
        .tolist()
    )

    joined = joined.copy()
    joined["group"] = joined["EntryName"].isin(top_entries).map({True: "top", False: "field"})

    slot_stats = (
        joined.groupby(["group", "slot"], as_index=False)
        .agg(
            avg_fpts=("fpts", "mean"),
            avg_pct_drafted=("pct_drafted", "mean"),
        )
        .sort_values(["slot", "group"])
    )
    return slot_stats


def stack_insights(joined: pd.DataFrame, top_n: int) -> pd.DataFrame:
    base = joined.copy()
    top_entries = (
        base[["EntryName", "Rank"]]
        .drop_duplicates()
        .sort_values("Rank")
        .head(top_n)["EntryName"]
        .tolist()
    )

    rows = []
    for entry_name, group in base.groupby("EntryName"):
        qb_rows = group[group["slot"] == "QB"]
        if qb_rows.empty:
            continue
        qb = qb_rows.iloc[0]
        qb_team = qb.get("team")
        opponent = _extract_opponent(qb.get("game_info", ""), qb_team)

        same_team_pass_catchers = group[
            (group["slot"].isin(["WR", "TE", "FLEX"]))
            & (group["team"] == qb_team)
            & (group["player"] != qb.get("player"))
        ]
        bringbacks = group[
            (group["team"] == opponent)
            & (group["slot"].isin(["WR", "RB", "TE", "FLEX"]))
        ]

        rows.append(
            {
                "EntryName": entry_name,
                "group": "top" if entry_name in top_entries else "field",
                "qb_player": qb.get("player"),
                "qb_team": qb_team,
                "stack_size": int(len(same_team_pass_catchers)),
                "bringback_count": int(len(bringbacks)),
            }
        )

    if not rows:
        return pd.DataFrame()

    stack_df = pd.DataFrame(rows)
    summary = stack_df.groupby("group", as_index=False).agg(
        avg_stack_size=("stack_size", "mean"),
        pct_with_1plus_stack=("stack_size", lambda s: (s >= 1).mean()),
        pct_with_2plus_stack=("stack_size", lambda s: (s >= 2).mean()),
        avg_bringback_count=("bringback_count", "mean"),
        pct_with_bringback=("bringback_count", lambda s: (s >= 1).mean()),
    )
    return summary


def salary_allocation_by_slot(joined: pd.DataFrame, top_n: int) -> pd.DataFrame:
    top_entries = (
        joined[["EntryName", "Rank"]]
        .drop_duplicates()
        .sort_values("Rank")
        .head(top_n)["EntryName"]
        .tolist()
    )

    work = joined.copy()
    work["group"] = work["EntryName"].isin(top_entries).map({True: "top", False: "field"})
    work["salary"] = pd.to_numeric(work["salary"], errors="coerce")

    slot_salary = (
        work.groupby(["group", "slot"], as_index=False)
        .agg(avg_salary=("salary", "mean"), median_salary=("salary", "median"))
        .sort_values(["slot", "group"])
    )
    return slot_salary


def build_exposure_targets(leverage: pd.DataFrame) -> pd.DataFrame:
    work = leverage.copy()
    work = work[work["field_lineup_rate"] >= 0.02].copy()

    work["target_exposure"] = (work["field_lineup_rate"] + 0.75 * work["leverage"]).clip(0.0, 1.0)

    def _bucket(v: float) -> str:
        if v >= 0.15:
            return "core_overweight"
        if v >= 0.05:
            return "overweight"
        if v <= -0.10:
            return "hard_underweight"
        if v <= -0.05:
            return "underweight"
        return "neutral"

    work["bucket"] = work["leverage"].apply(_bucket)
    work = work.sort_values("leverage", ascending=False)
    return work[["player", "field_lineup_rate", "top_lineup_rate", "leverage", "bucket", "target_exposure", "pct_drafted", "fpts"]]


def main() -> None:
    df = load_results_sheet()
    dk_context = load_draftkings_context()
    lineup_players = build_lineup_player_frame(df)
    if lineup_players.empty:
        print("No parsed lineups found in results sheet.")
        return

    player_stats = clean_player_stats(df)
    lineup_summary, joined = summarize_lineups(lineup_players, player_stats)

    joined = joined.merge(dk_context, on="player", how="left")

    lineup_summary = lineup_summary[lineup_summary["player_count"] == 9].copy()
    lineup_summary = lineup_summary.sort_values(["Rank", "Points"], ascending=[True, False])

    top_n = max(25, int(round(len(lineup_summary) * 0.05)))
    top_n = min(top_n, len(lineup_summary))

    summary = summarize_top_vs_field(lineup_summary, top_n)
    leverage = top_player_leverage(joined, top_n)
    slot_stats = slot_scoring_insights(joined, top_n)
    stack_stats = stack_insights(joined, top_n)
    slot_salary = salary_allocation_by_slot(joined, top_n)
    exposure_targets = build_exposure_targets(leverage)

    print("=" * 72)
    print("DRAFTKINGS RESULTS INSIGHTS")
    print("=" * 72)
    print(f"Entries analyzed: {summary.field_count}")
    print(f"Top cohort size: {summary.top_count} (top 5% or min 25)")
    print(f"Avg points (field): {summary.mean_points_field:.2f}")
    print(f"Avg points (top):   {summary.mean_points_top:.2f}")
    print(f"Avg ownership per player slot (field): {summary.mean_ownership_field:.2f}%")
    print(f"Avg ownership per player slot (top):   {summary.mean_ownership_top:.2f}%")

    print("\nMost Overrepresented Players In Top Cohort")
    cols = ["player", "field_lineup_rate", "top_lineup_rate", "leverage", "pct_drafted", "fpts"]
    print(leverage[cols].head(20).to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\nMost Underrepresented Players In Top Cohort")
    print(leverage[cols].tail(20).sort_values("leverage").to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\nSlot-Level Top vs Field")
    print(slot_stats.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\nQB Stack / Bring-Back Patterns (Top vs Field)")
    if stack_stats.empty:
        print("No stack data available.")
    else:
        print(stack_stats.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    print("\nSalary Allocation By Slot (Top vs Field)")
    print(slot_salary.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    print("\nRecommended Exposure Targets (Next Week)")
    print(
        exposure_targets.head(25).to_string(
            index=False,
            float_format=lambda x: f"{x:.3f}",
        )
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    leverage.to_csv(RESULTS_DIR / "results_player_leverage.csv", index=False)
    slot_stats.to_csv(RESULTS_DIR / "results_slot_insights.csv", index=False)
    lineup_summary.to_csv(RESULTS_DIR / "results_lineup_summary.csv", index=False)
    stack_stats.to_csv(RESULTS_DIR / "results_stack_insights.csv", index=False)
    slot_salary.to_csv(RESULTS_DIR / "results_slot_salary.csv", index=False)
    exposure_targets.to_csv(RESULTS_DIR / "results_exposure_targets.csv", index=False)
    print(
        "\nSaved to results/: results_player_leverage.csv, "
        "results_slot_insights.csv, results_lineup_summary.csv, "
        "results_stack_insights.csv, results_slot_salary.csv, "
        "results_exposure_targets.csv"
    )


if __name__ == "__main__":
    main()
