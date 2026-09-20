# DraftKings Optimizer

Python-based NFL DraftKings lineup optimizer that:

- Reads the DraftKings player pool from Google Sheets
- Pulls weekly ESPN projections
- Matches DK players to ESPN projections by name and team
- Excludes inactive players based on DK status values like `OUT` and `IR`
- Solves for the optimal 9-player lineup under the $50,000 salary cap

## Lineup Rules

- 1 QB
- 2 RB
- 3 WR
- 1 TE
- 1 FLEX (RB/WR/TE)
- 1 DST

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```bash
pip install gspread google-auth pandas requests pulp
```

3. Copy `local_settings.example.py` to `local_settings.py`.
4. Set these values in `local_settings.py`:
   - `CREDENTIALS_FILE`
   - `SHEET_URL`
   - `WORKSHEET_NAME`

## Usage

Run using ESPN weekly projections:

```bash
python main.py
```

Run using DraftKings `AvgPointsPerGame` instead:

```bash
python main.py --use-avg
```

Download ESPN projections only:

```bash
python espn_projections.py
```

## Debugging

VS Code launch configurations are included in `.vscode/launch.json` for:

- Running the optimizer with ESPN projections
- Running the optimizer with `AvgPointsPerGame`
- Downloading ESPN projections only

## Notes

- `local_settings.py` is git-ignored and should not be committed.
- Generated files like `espn_projections.csv` are git-ignored.
- The optimizer uses integer linear programming via PuLP.