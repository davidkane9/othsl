"""
ELO rating system for OTHSL teams.

Key design decisions:
- Each (team, age_group) pair gets its own ELO track, since "Irish Village"
  appears in multiple age groups and divisions over the years.
- Default starting ELO: 1500
- K-factor: 32 (standard; higher = faster adaptation)
- Season carryover: at the start of each season a team's ELO regresses
  40% toward 1500 to account for player turnover.
- Forfeits count as a 3-0 win/loss for ELO purposes.
- Ties split the expected points evenly (standard ELO draw handling).
"""

import csv
from collections import defaultdict

DEFAULT_ELO = 1500
K = 32
SEASON_REGRESSION = 0.4   # fraction to regress toward 1500 between seasons


def expected(rating_a, rating_b):
    """Expected score for team A against team B (0-1 scale)."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400))


def update_elo(elo_home, elo_away, home_goals, away_goals):
    """
    Compute new ELO ratings after one game.

    Args:
        elo_home: current ELO of home team
        elo_away: current ELO of away team
        home_goals: goals scored by home team (int, or 'forfeit'/'forfeitNP')
        away_goals: goals scored by away team (same)

    Returns:
        (new_elo_home, new_elo_away)
    """
    # Convert forfeit strings to numeric outcomes
    def is_forfeit(g):
        return isinstance(g, str) and g.lower().startswith("forfeit")

    h_forfeit = is_forfeit(home_goals)
    a_forfeit = is_forfeit(away_goals)

    if h_forfeit and a_forfeit:
        # Double forfeit: treat as draw
        actual_home = 0.5
    elif a_forfeit:
        # Away team forfeited: home wins
        actual_home = 1.0
    elif h_forfeit:
        # Home team forfeited: away wins
        actual_home = 0.0
    else:
        hg = int(home_goals)
        ag = int(away_goals)
        if hg > ag:
            actual_home = 1.0
        elif hg < ag:
            actual_home = 0.0
        else:
            actual_home = 0.5

    exp_home = expected(elo_home, elo_away)
    exp_away = 1.0 - exp_home

    new_home = elo_home + K * (actual_home - exp_home)
    new_away = elo_away + K * ((1.0 - actual_home) - exp_away)
    return new_home, new_away


def regress(elo):
    """Apply season-to-season regression toward 1500."""
    return elo + SEASON_REGRESSION * (DEFAULT_ELO - elo)


def rolling_elo(rows):
    """
    Calculate ELO for every team at every point in time.

    Args:
        rows: list of game dicts with keys:
              season, age_group, date, home_team, home_goals, away_goals, away_team

    Returns:
        List of dicts, one per game played, with:
          season, age_group, date, home_team, away_team,
          elo_home_before, elo_away_before,
          elo_home_after, elo_away_after,
          home_goals, away_goals

        Also returns final_elos: dict of {(team, age_group): elo}
    """
    # Sort chronologically
    def sort_key(r):
        d = r.get("date", "")
        return (r.get("season", ""), "0" if d == "TBD" else d)

    rows = sorted(rows, key=sort_key)

    # ELO state: keyed by (team, age_group)
    elos = defaultdict(lambda: DEFAULT_ELO)
    # Track which season each team last played in
    last_season = {}

    output = []
    for row in rows:
        season = row["season"]
        age_group = row["age_group"]
        date = row["date"]
        ht = row["home_team"]
        at = row["away_team"]
        hg = row["home_goals"]
        ag = row["away_goals"]

        if date == "TBD":
            continue

        hkey = (ht, age_group)
        akey = (at, age_group)

        # Apply season regression for teams returning after a season gap
        for key in (hkey, akey):
            if key in last_season and last_season[key] != season:
                elos[key] = regress(elos[key])

        elo_h_before = elos[hkey]
        elo_a_before = elos[akey]

        elo_h_after, elo_a_after = update_elo(elo_h_before, elo_a_before, hg, ag)

        elos[hkey] = elo_h_after
        elos[akey] = elo_a_after
        last_season[hkey] = season
        last_season[akey] = season

        output.append({
            "season": season,
            "age_group": age_group,
            "date": date,
            "home_team": ht,
            "away_team": at,
            "home_goals": hg,
            "away_goals": ag,
            "elo_home_before": round(elo_h_before, 1),
            "elo_away_before": round(elo_a_before, 1),
            "elo_home_after": round(elo_h_after, 1),
            "elo_away_after": round(elo_a_after, 1),
        })

    return output, dict(elos)


def load_results(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_elo_history(output_rows, path):
    if not output_rows:
        return
    fields = list(output_rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)


if __name__ == "__main__":
    import os
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    rows = load_results(os.path.join(data_dir, "all_results.csv"))
    # Also include current season
    current = os.path.join(data_dir, "current_results.csv")
    if os.path.exists(current):
        rows += load_results(current)

    print(f"Loaded {len(rows)} games, calculating ELO...")
    history, final_elos = rolling_elo(rows)
    print(f"Calculated {len(history)} ELO updates.")

    out_path = os.path.join(data_dir, "elo_history.csv")
    save_elo_history(history, out_path)
    print(f"Saved -> {out_path}")

    # Print current Irish Village ELO
    iv_elos = {k: v for k, v in final_elos.items() if "Irish Village" in k[0]}
    print("\nIrish Village current ELO ratings:")
    for (team, ag), elo in sorted(iv_elos.items(), key=lambda x: -x[1]):
        print(f"  {team} ({ag}): {elo:.1f}")
