import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import random
import pulp
import argparse
from espn_projections import download_projections

try:
    from local_settings import CREDENTIALS_FILE, SHEET_URL, WORKSHEET_NAME
except ImportError as exc:
    raise RuntimeError(
        "Missing local_settings.py. Copy local_settings.example.py to local_settings.py "
        "and set your local credentials and sheet values."
    ) from exc

# Define the scope for Google Sheets API
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]


def authenticate_gspread(credentials_file):
    """
    Authenticate and return gspread client

    Args:
        credentials_file (str): Path to your Google Service Account JSON file

    Returns:
        gspread.Client: Authenticated client
    """
    credentials = Credentials.from_service_account_file(
        credentials_file,
        scopes=SCOPES
    )
    client = gspread.authorize(credentials)
    return client


def read_sheet_data(client, sheet_url_or_key, worksheet_name=None):
    """
    Read data from Google Sheet

    Args:
        client: gspread client
        sheet_url_or_key (str): Google Sheet URL or key
        worksheet_name (str): Name of specific worksheet (optional)

    Returns:
        list: All values from the sheet
    """
    try:
        # Open the spreadsheet
        sheet = client.open_by_url(sheet_url_or_key)

        # Select worksheet (first sheet by default)
        if worksheet_name:
            worksheet = sheet.worksheet(worksheet_name)
        else:
            worksheet = sheet.sheet1  # First worksheet

        # Get all values
        data = worksheet.get_all_values()

        return data

    except gspread.SpreadsheetNotFound:
        print("Spreadsheet not found. Check the URL or share permissions.")
        return None
    except gspread.WorksheetNotFound:
        print(f"Worksheet '{worksheet_name}' not found.")
        return None


def data_to_dataframe(data):
    """
    Convert sheet data to pandas DataFrame

    Args:
        data (list): Sheet data as list of lists

    Returns:
        pd.DataFrame: Data as DataFrame
    """
    if data and len(data) > 1:
        # First row as headers, rest as data
        headers = data[0]
        rows = data[1:]
        df = pd.DataFrame(rows, columns=headers)
        return df
    return pd.DataFrame()


def get_dk_data():
    try:
        # Authenticate
        client = authenticate_gspread(CREDENTIALS_FILE)
        print("Authentication successful!")

        # Read data
        data = read_sheet_data(client, SHEET_URL, WORKSHEET_NAME)

        if data:
            print(f"Successfully read {len(data)} rows of data")

            # Convert to DataFrame for easier manipulation
            df = data_to_dataframe(data)

            # Display first few rows
            print("\nFirst 5 rows:")
            print(df.head())

            # Basic info about the data
            print(f"\nDataFrame shape: {df.shape}")
            print(f"Columns: {list(df.columns)}")

            return df

    except Exception as e:
        print(f"An error occurred: {e}")
        return None


def random_integers_basic(count, min_val, max_val):
    """Generate list of random integers using basic random module"""
    return [random.randint(min_val, max_val) for _ in range(count)]


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, and remove common suffixes for matching."""
    import re
    name = name.lower()
    # Remove generational suffixes
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\.?\s*$", "", name).strip()
    name = re.sub(r"[^a-z0-9 ]", "", name).strip()
    return name


def match_projections(dk_df: pd.DataFrame, proj_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge ESPN projections onto the DK player pool by (name, team).
    For D/ST rows, matches by team abbreviation alone since name formats differ
    (DK: "Chargers", ESPN: "Chargers D/ST").
    Falls back to name-only match for remaining unmatched players.
    Returns dk_df with a new 'projected_points' column.
    """
    proj = proj_df[["name", "position", "team", "projected_points"]].copy()
    proj["_name_key"] = proj["name"].apply(normalize_name)
    proj["_team_key"] = proj["team"].str.upper()

    dk = dk_df.copy()
    dk["_name_key"] = dk["Name"].apply(normalize_name)
    dk["_team_key"] = dk["TeamAbbrev"].str.upper()

    # Primary merge: exact name + team (handles all skill positions)
    merged = dk.merge(
        proj[["_name_key", "_team_key", "projected_points"]],
        on=["_name_key", "_team_key"],
        how="left",
    )

    # D/ST merge: match by team abbreviation, then by name substring
    # (DK: "Chargers" / "Commanders", ESPN: "Chargers D/ST" / "Commanders D/ST")
    unmatched = merged["projected_points"].isna()
    if unmatched.any():
        dst_proj = proj[proj["position"] == "D/ST"][["_name_key", "_team_key", "projected_points"]]

        # Pass 1: match by team abbreviation
        dst_by_team = dst_proj[["_team_key", "projected_points"]].rename(
            columns={"projected_points": "_dst_proj"}
        )
        dst_fill = dk.loc[unmatched, ["_team_key"]].merge(dst_by_team, on="_team_key", how="left")
        merged.loc[unmatched, "projected_points"] = dst_fill["_dst_proj"].values

        # Pass 2: for still-unmatched, check if DK name is a word in the ESPN D/ST name
        unmatched = merged["projected_points"].isna()
        if unmatched.any():
            dst_name_map = {
                row["_name_key"].replace(" dst", "").strip(): row["projected_points"]
                for _, row in dst_proj.iterrows()
            }
            def _dst_name_lookup(dk_name_key):
                return dst_name_map.get(dk_name_key)
            merged.loc[unmatched, "projected_points"] = (
                dk.loc[unmatched, "_name_key"].map(_dst_name_lookup).values
            )

    # Final fallback: name-only match for remaining unmatched
    unmatched = merged["projected_points"].isna()
    if unmatched.any():
        name_only = proj.drop_duplicates("_name_key")[["_name_key", "projected_points"]]
        fallback = dk.loc[unmatched, ["_name_key"]].merge(
            name_only, on="_name_key", how="left"
        )
        merged.loc[unmatched, "projected_points"] = fallback["projected_points"].values

    merged = merged.drop(columns=["_name_key", "_team_key"])

    matched = merged["projected_points"].notna().sum()
    total = len(merged)
    print(f"Matched {matched}/{total} players to ESPN projections.")
    if matched < total:
        unmatched_names = merged.loc[merged["projected_points"].isna(), "Name"].tolist()
        print(f"Unmatched players ({len(unmatched_names)}): {unmatched_names[:20]}")

    return merged


SALARY_CAP = 50_000
TOTAL_PLAYERS = 9
EXCLUDED_STATUSES = {"out", "ir"}

# Players confirmed out — excluded from lineup optimization
EXCLUDED_PLAYERS = [
]


def optimize_lineup(
    dk_df: pd.DataFrame,
    score_col: str = "projected_points",
    min_qb_stack: int = 0,
) -> pd.DataFrame | None:
    """
    Find the optimal DraftKings NFL lineup using integer linear programming.

    Roster: QB, 2 RB, 3 WR, TE, FLEX (RB/WR/TE), DST — 9 players total.
    Salary cap: $50,000.
    score_col: column used as the optimisation objective
               ("projected_points" or "AvgPointsPerGame").
    min_qb_stack: minimum number of same-team WR/TE stacked with the QB.
    Players listed in EXCLUDED_PLAYERS are removed before solving.

    Returns a 9-row DataFrame of the selected lineup, or None if infeasible.
    """
    if score_col not in dk_df.columns:
        raise ValueError(f"Score column '{score_col}' not found in DataFrame.")
    print(f"Optimizing using: {score_col}")
    if min_qb_stack > 0:
        print(f"Applying QB stack constraint: at least {min_qb_stack} same-team WR/TE")

    pool = dk_df.copy()
    pool["Salary"] = pd.to_numeric(pool["Salary"], errors="coerce")
    pool[score_col] = pd.to_numeric(pool[score_col], errors="coerce")
    pool = pool.dropna(subset=["Salary", score_col])
    pool = pool[pool[score_col] > 0]

    if "Status" in pool.columns:
        pool["Status"] = pool["Status"].fillna("").astype(str).str.strip()
        status_excluded = pool["Status"].str.lower().isin(EXCLUDED_STATUSES)
        if status_excluded.any():
            excluded_status_rows = pool.loc[status_excluded, ["Name", "Status"]]
            print(
                "Excluding status-based inactives: "
                f"{excluded_status_rows.to_dict(orient='records')}"
            )
        pool = pool[~status_excluded]

    if EXCLUDED_PLAYERS:
        excluded = pool["Name"].isin(EXCLUDED_PLAYERS)
        if excluded.any():
            print(f"Excluding injured/out players: {pool.loc[excluded, 'Name'].tolist()}")
        pool = pool[~excluded]

    pool = pool.reset_index(drop=True)
    n = len(pool)
    players = list(range(n))

    prob = pulp.LpProblem("DraftKings_Optimizer", pulp.LpMaximize)
    x = [pulp.LpVariable(f"x_{i}", cat="Binary") for i in players]

    # Objective: maximise score
    prob += pulp.lpSum(pool.loc[i, score_col] * x[i] for i in players)

    # Salary cap
    prob += pulp.lpSum(pool.loc[i, "Salary"] * x[i] for i in players) <= SALARY_CAP

    # Total roster size = 9
    prob += pulp.lpSum(x) == TOTAL_PLAYERS

    # Position counts
    # QB: exactly 1
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "QB") == 1
    # RB: 2 starters + optionally 1 FLEX => 2–3
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "RB") >= 2
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "RB") <= 3
    # WR: 3 starters + optionally 1 FLEX => 3–4
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "WR") >= 3
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "WR") <= 4
    # TE: 1 starter + optionally 1 FLEX => 1–2
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "TE") >= 1
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "TE") <= 2
    # DST: exactly 1
    prob += pulp.lpSum(x[i] for i in players if pool.loc[i, "Position"] == "DST") == 1

    # Optional QB stack rule: require at least N same-team WR/TE with selected QB.
    if min_qb_stack > 0:
        teams = sorted(pool["TeamAbbrev"].dropna().astype(str).str.upper().unique().tolist())
        team_qb_vars = {t: pulp.LpVariable(f"team_qb_{t}", cat="Binary") for t in teams}

        for team in teams:
            qb_on_team = [i for i in players if pool.loc[i, "Position"] == "QB" and str(pool.loc[i, "TeamAbbrev"]).upper() == team]
            if qb_on_team:
                prob += team_qb_vars[team] == pulp.lpSum(x[i] for i in qb_on_team)
            else:
                prob += team_qb_vars[team] == 0

            pass_catchers_on_team = [
                i for i in players
                if str(pool.loc[i, "TeamAbbrev"]).upper() == team
                and pool.loc[i, "Position"] in ("WR", "TE")
            ]
            prob += (
                pulp.lpSum(x[i] for i in pass_catchers_on_team)
                >= min_qb_stack * team_qb_vars[team]
            )

    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if pulp.LpStatus[prob.status] != "Optimal":
        print(f"Optimization failed: {pulp.LpStatus[prob.status]}")
        return None

    selected_idx = [i for i in players if pulp.value(x[i]) == 1]
    lineup = pool.loc[selected_idx].copy()

    # Assign slot labels (QB, RB, RB, WR, WR, WR, TE, FLEX, DST)
    slot_order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "FLEX": 4, "DST": 5}
    starter_limits = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
    pos_counts = {p: 0 for p in starter_limits}
    flex_assigned = False
    slots = []
    for _, row in lineup.sort_values(["Position", score_col], ascending=[True, False]).iterrows():
        pos = row["Position"]
        if pos in starter_limits and pos_counts[pos] < starter_limits[pos]:
            slots.append((row.name, pos))
            pos_counts[pos] += 1
        elif pos in ("RB", "WR", "TE") and not flex_assigned:
            slots.append((row.name, "FLEX"))
            flex_assigned = True
        else:
            slots.append((row.name, pos))

    slot_map = {idx: slot for idx, slot in slots}
    lineup["Slot"] = lineup.index.map(slot_map)
    lineup = lineup.sort_values("Slot", key=lambda s: s.map(slot_order))

    total_salary = lineup["Salary"].sum()
    total_score  = lineup[score_col].sum()

    print("\n" + "=" * 74)
    print(f"OPTIMAL DRAFTKINGS LINEUP  [{score_col}]")
    print("=" * 74)
    lineup["AvgPointsPerGame"] = pd.to_numeric(lineup["AvgPointsPerGame"], errors="coerce")
    if "projected_points" in lineup.columns:
        lineup["projected_points"] = pd.to_numeric(lineup["projected_points"], errors="coerce")
    display_cols = ["Slot", "Name", "TeamAbbrev", "Salary", "AvgPointsPerGame"]
    if "projected_points" in lineup.columns:
        display_cols.append("projected_points")
    print(lineup[display_cols].to_string(index=False))
    print("-" * 74)
    print(f"Total Salary:  ${total_salary:,.0f} / ${SALARY_CAP:,}")
    print(f"{score_col}: {total_score:.2f}")
    print("=" * 74)

    return lineup


def main():
    parser = argparse.ArgumentParser(description="DraftKings NFL lineup optimizer")
    parser.add_argument(
        "--use-avg",
        action="store_true",
        help="Optimize using AvgPointsPerGame instead of ESPN projected_points",
    )
    parser.add_argument(
        "--min-qb-stack",
        type=int,
        default=0,
        help="Minimum same-team WR/TE required with selected QB (e.g. 2 for 2+ stacks)",
    )
    args = parser.parse_args()
    score_col = "AvgPointsPerGame" if args.use_avg else "projected_points"

    dk_df = get_dk_data()
    if dk_df is None:
        return None

    if score_col == "projected_points":
        proj_df = download_projections()
        if not proj_df.empty:
            dk_df = match_projections(dk_df, proj_df)
        else:
            dk_df['projected_points'] = random_integers_basic(len(dk_df), 0, 25)

    lineup = optimize_lineup(dk_df, score_col=score_col, min_qb_stack=max(args.min_qb_stack, 0))
    return lineup


if __name__ == "__main__":
    # Run the main function
    dataframe = main()

    # Example of additional operations you can perform
    if dataframe is not None:
        print("\n" + "=" * 50)
        print("ADDITIONAL OPERATIONS EXAMPLES:")
        print("=" * 50)

        # Save to CSV
        # dataframe.to_csv('output.csv', index=False)
        # print("Data saved to output.csv")

        # Basic statistics (for numeric columns)
        numeric_cols = dataframe.select_dtypes(include=['number']).columns
        if len(numeric_cols) > 0:
            print(f"\nNumeric columns statistics:")
            print(dataframe[numeric_cols].describe())

        # Filter data example (uncomment and modify as needed)
        # filtered_data = dataframe[dataframe['ColumnName'] == 'SomeValue']
        # print(f"Filtered data shape: {filtered_data.shape}")
