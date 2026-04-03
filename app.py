"""
OTHSL web app — MVP homepage showing Irish Village's current season.

Run:
  python app.py

Then open http://localhost:5000
"""

import csv
import os
from flask import Flask, render_template

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TEAM = "Irish Village"
AGE_GROUP = "Over 55"
CURRENT_SEASON = "Fall 2025"  # update each season


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def get_team_results():
    """All games Irish Village played in the current season, in date order."""
    rows = load_csv(os.path.join(DATA_DIR, "all_results.csv"))
    rows += load_csv(os.path.join(DATA_DIR, "current_results.csv"))

    games = []
    for r in rows:
        if r["season"] != CURRENT_SEASON or r["age_group"] != AGE_GROUP:
            continue
        is_home = r["home_team"] == TEAM
        is_away = r["away_team"] == TEAM
        if not is_home and not is_away:
            continue

        hg = r["home_goals"]
        ag = r["away_goals"]

        def is_forfeit(g):
            return isinstance(g, str) and g.lower().startswith("forfeit")

        if r["notes"] == "double forfeit":
            result = "L"
        elif is_forfeit(hg) or is_forfeit(ag):
            # Single forfeit — determine who forfeited
            if (is_home and is_forfeit(hg)) or (is_away and is_forfeit(ag)):
                result = "L"
            else:
                result = "W"
        elif r["date"] == "TBD":
            result = "F"
        else:
            hg_i, ag_i = int(hg), int(ag)
            if is_home:
                gf, ga = hg_i, ag_i
            else:
                gf, ga = ag_i, hg_i
            if gf > ga:
                result = "W"
            elif gf < ga:
                result = "L"
            else:
                result = "T"

        opponent = r["away_team"] if is_home else r["home_team"]
        venue = "H" if is_home else "A"

        if not is_forfeit(hg) and not is_forfeit(ag):
            score = f"{hg}-{ag}" if is_home and r["date"] != "TBD" else (f"{ag}-{hg}" if r["date"] != "TBD" else "F")
        else:
            score = "F"

        games.append({
            "date": r["date"],
            "opponent": opponent,
            "venue": venue,
            "score": score,
            "result": result,
            "notes": r.get("notes", ""),
        })

    games.sort(key=lambda g: ("1" if g["date"] == "TBD" else "0") + g["date"])
    return games


def get_standings():
    """All teams in Irish Village's current flight, sorted by PPG."""
    rows = load_csv(os.path.join(DATA_DIR, "all_results.csv"))
    rows += load_csv(os.path.join(DATA_DIR, "current_results.csv"))

    # Gather results for this division
    division = None
    geography = None
    for r in rows:
        if r["season"] == CURRENT_SEASON and r["age_group"] == AGE_GROUP:
            if r["home_team"] == TEAM or r["away_team"] == TEAM:
                division = r["division"]
                geography = r["geography"]
                break

    if division is None:
        return [], None, None

    stats = {}
    for r in rows:
        if (r["season"] != CURRENT_SEASON or r["age_group"] != AGE_GROUP
                or r["division"] != division or r["geography"] != geography):
            continue

        ht, at = r["home_team"], r["away_team"]
        hg, ag = r["home_goals"], r["away_goals"]

        def is_forfeit(g):
            return isinstance(g, str) and g.lower().startswith("forfeit")

        # Skip phantom team names produced by forfeit parsing artifacts
        if "lost by forfeit" in ht.lower() or "lost by forfeit" in at.lower():
            continue

        for team in (ht, at):
            if team not in stats:
                stats[team] = {"gp": 0, "w": 0, "l": 0, "t": 0, "pts": 0, "gf": 0, "ga": 0}

        if is_forfeit(hg) or is_forfeit(ag):
            if not is_forfeit(hg) and is_forfeit(ag):
                # Home wins by forfeit
                stats[ht]["w"] += 1; stats[ht]["pts"] += 3; stats[ht]["gp"] += 1
                stats[at]["l"] += 1; stats[at]["gp"] += 1
            elif is_forfeit(hg) and not is_forfeit(ag):
                stats[at]["w"] += 1; stats[at]["pts"] += 3; stats[at]["gp"] += 1
                stats[ht]["l"] += 1; stats[ht]["gp"] += 1
            else:
                # double forfeit — both teams take a loss
                stats[ht]["l"] += 1; stats[ht]["gp"] += 1
                stats[at]["l"] += 1; stats[at]["gp"] += 1
        else:
            hg_i, ag_i = int(hg), int(ag)
            stats[ht]["gf"] += hg_i; stats[ht]["ga"] += ag_i; stats[ht]["gp"] += 1
            stats[at]["gf"] += ag_i; stats[at]["ga"] += hg_i; stats[at]["gp"] += 1
            if hg_i > ag_i:
                stats[ht]["w"] += 1; stats[ht]["pts"] += 3
                stats[at]["l"] += 1
            elif hg_i < ag_i:
                stats[at]["w"] += 1; stats[at]["pts"] += 3
                stats[ht]["l"] += 1
            else:
                stats[ht]["t"] += 1; stats[ht]["pts"] += 1
                stats[at]["t"] += 1; stats[at]["pts"] += 1

    table = []
    for team, s in stats.items():
        ppg = s["pts"] / s["gp"] if s["gp"] else 0
        table.append({
            "team": team,
            "gp": s["gp"], "w": s["w"], "l": s["l"], "t": s["t"],
            "pts": s["pts"], "gf": s["gf"], "ga": s["ga"],
            "gd": s["gf"] - s["ga"], "ppg": round(ppg, 2),
            "is_iv": team == TEAM,
        })

    table.sort(key=lambda x: (-x["ppg"], -x["gd"], -x["gf"]))
    return table, division, geography


def get_elo_history():
    """ELO time series for Irish Village Over 55 this season."""
    rows = load_csv(os.path.join(DATA_DIR, "elo_history.csv"))
    points = []
    for r in rows:
        if r["season"] != CURRENT_SEASON or r["age_group"] != AGE_GROUP:
            continue
        if r["home_team"] == TEAM:
            points.append({"date": r["date"], "elo": float(r["elo_home_after"])})
        elif r["away_team"] == TEAM:
            points.append({"date": r["date"], "elo": float(r["elo_away_after"])})
    return sorted(points, key=lambda p: p["date"])


@app.route("/")
def index():
    games = get_team_results()
    standings, division, geography = get_standings()
    elo_history = get_elo_history()

    # Summary record
    w = sum(1 for g in games if g["result"] == "W")
    l = sum(1 for g in games if g["result"] in ("L", "F"))
    t = sum(1 for g in games if g["result"] == "T")

    current_elo = elo_history[-1]["elo"] if elo_history else None

    return render_template(
        "index.html",
        team=TEAM,
        season=CURRENT_SEASON,
        age_group=AGE_GROUP,
        division=division,
        geography=geography,
        games=games,
        standings=standings,
        elo_history=elo_history,
        record={"w": w, "l": l, "t": t},
        current_elo=current_elo,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
