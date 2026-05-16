"""
OTHSL web app for Spring 2026 league browsing and team pages.

Run:
  python app.py

Then open http://localhost:5000
"""

import csv
import json
import os
import re
import random
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from flask import Flask, abort, render_template, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# Pre-generated AI texts populated by freeze.py before freezing.
# Keys are flight_slug / team_slug strings.
_ai_flight_texts: dict = {}
_ai_team_texts:  dict  = {}
_flight_importance_cache: dict = {}

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CURRENT_SEASON = "Spring 2026"
DEFAULT_ELO = 1500
SIMULATION_RUNS = 400
IMPORTANCE_SIM_RUNS = 220
REGRESSION_MAX  = 0.80   # blend win_expectation 80% toward 0.5 at season start
REGRESSION_GP_FULL = 6   # regression reaches 0 once avg games played reaches this
DRAW_PROBABILITY = 0.22


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def slugify(value):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "team"


def clean_team_name(team):
    team = (team or "").strip()
    team = re.sub(r"\s*#\s*review referee\s*$", "", team, flags=re.IGNORECASE).strip()
    team = re.sub(r"\s*(?:#\s*)?crossover\s*$", "", team, flags=re.IGNORECASE).strip()
    return team


def is_real_team_name(team):
    team = clean_team_name(team)
    if not team:
        return False
    if team.upper() == "TBD":
        return False
    if team.upper() == "FC":
        return False
    if re.search(r"lost by forfeit", team, re.IGNORECASE):
        return False
    return True


def season_to_slug(season):
    return season.lower().replace(" ", "-")


def slug_to_season(slug):
    parts = slug.split("-")
    return parts[0].capitalize() + " " + parts[1] if len(parts) == 2 else slug


def get_all_seasons():
    rows = load_csv(os.path.join(DATA_DIR, "all_results.csv"))
    seasons = sorted({r["season"] for r in rows if r["season"]}, key=season_sort_key)
    if CURRENT_SEASON not in seasons:
        seasons.append(CURRENT_SEASON)
        seasons.sort(key=season_sort_key)
    return seasons


def get_rows_for_season(season):
    if season == CURRENT_SEASON:
        return get_current_season_rows()
    rows = load_csv(os.path.join(DATA_DIR, "all_results.csv"))
    return [r for r in rows if r["season"] == season]


def get_current_season_rows():
    rows = load_csv(os.path.join(DATA_DIR, "current_results.csv"))
    return [r for r in rows if r["season"] == CURRENT_SEASON]


def get_elo_rows():
    return load_csv(os.path.join(DATA_DIR, "elo_history.csv"))


# Cache the ELO map once at startup — reading 65k rows per page is too slow.
_elo_map_cache = None

def get_latest_elo_map():
    global _elo_map_cache
    if _elo_map_cache is not None:
        return _elo_map_cache
    latest = {}
    for row in get_elo_rows():
        age_group = row["age_group"]
        if is_real_team_name(row["home_team"]):
            latest[(clean_team_name(row["home_team"]), age_group)] = float(row["elo_home_after"])
        if is_real_team_name(row["away_team"]):
            latest[(clean_team_name(row["away_team"]), age_group)] = float(row["elo_away_after"])
    _elo_map_cache = latest
    return latest


def get_current_regression(current_stats):
    avg_gp = sum(stats.get("gp", 0) for stats in current_stats.values()) / max(1, len(current_stats))
    return max(0.0, REGRESSION_MAX * (1.0 - avg_gp / REGRESSION_GP_FULL))


def adjusted_expected_result(home_elo, away_elo, regression=0.0):
    base = expected_result(home_elo, away_elo)
    return base * (1.0 - regression) + 0.5 * regression


def match_outcome_probabilities(home_elo, away_elo, regression=0.0, draw_prob=DRAW_PROBABILITY):
    win_expectation = adjusted_expected_result(home_elo, away_elo, regression=regression)
    home_win_prob = max(0.05, min(0.9, win_expectation - draw_prob / 2))
    away_win_prob = max(0.05, 1.0 - draw_prob - home_win_prob)
    return {
        "home_win_prob": round(home_win_prob, 3),
        "draw_prob": round(draw_prob, 3),
        "away_win_prob": round(away_win_prob, 3),
    }


def format_game_date(date_str):
    if not date_str or date_str == "TBD":
        return "TBD"
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%A, %B %d")
    except ValueError:
        return date_str


def simulate_flight_outlook(sim_data, total_runs=SIMULATION_RUNS, overrides=None):
    teams = sim_data.get("teams", [])
    if not teams:
        return {"team_stats": {}, "counts": {}, "total_runs": total_runs, "regression": 0.0}

    current_stats = sim_data.get("current_stats", {})
    current_elos = sim_data.get("current_elos", {})
    remaining_games = sim_data.get("remaining_games", [])
    promotion_cut = sim_data.get("promotion_cut", 2)
    relegation_cut = sim_data.get("relegation_cut", 0)
    regression = sim_data.get("current_regression")
    if regression is None:
        regression = get_current_regression(current_stats)
    overrides = overrides or {}

    counts = {team: [0] * (len(teams) + 1) for team in teams}

    if not remaining_games:
        ordered = sorted(
            teams,
            key=lambda name: (
                -current_stats.get(name, {}).get("pts", 0),
                -current_stats.get(name, {}).get("gd", 0),
                -current_stats.get(name, {}).get("gf", 0),
                name,
            ),
        )
        for place, team in enumerate(ordered, start=1):
            counts[team][place] = total_runs
    else:
        for _ in range(total_runs):
            sim_stats = {
                team: {
                    "pts": current_stats.get(team, {}).get("pts", 0),
                    "gd": current_stats.get(team, {}).get("gd", 0),
                    "gf": current_stats.get(team, {}).get("gf", 0),
                }
                for team in teams
            }
            sim_elos = {team: current_elos.get(team, DEFAULT_ELO) for team in teams}

            for game in remaining_games:
                home = game["home"]
                away = game["away"]
                if home not in sim_stats or away not in sim_stats:
                    continue

                forced = overrides.get(game["id"])
                if forced is not None:
                    act = 1.0 if forced == "home" else (0.5 if forced == "draw" else 0.0)
                else:
                    probs = match_outcome_probabilities(
                        sim_elos.get(home, DEFAULT_ELO),
                        sim_elos.get(away, DEFAULT_ELO),
                        regression=regression,
                    )
                    roll = random.random()
                    if roll < probs["home_win_prob"]:
                        act = 1.0
                    elif roll < probs["home_win_prob"] + probs["draw_prob"]:
                        act = 0.5
                    else:
                        act = 0.0

                if act == 1.0:
                    sim_stats[home]["pts"] += 3
                    sim_stats[home]["gd"] += 1
                    sim_stats[home]["gf"] += 2
                    sim_stats[away]["gd"] -= 1
                    sim_stats[away]["gf"] += 1
                elif act == 0.5:
                    sim_stats[home]["pts"] += 1
                    sim_stats[away]["pts"] += 1
                    sim_stats[home]["gf"] += 1
                    sim_stats[away]["gf"] += 1
                else:
                    sim_stats[away]["pts"] += 3
                    sim_stats[away]["gd"] += 1
                    sim_stats[away]["gf"] += 2
                    sim_stats[home]["gd"] -= 1
                    sim_stats[home]["gf"] += 1

                e_home = sim_elos.get(home, DEFAULT_ELO)
                e_away = sim_elos.get(away, DEFAULT_ELO)
                expected_home = expected_result(e_home, e_away)
                sim_elos[home] = e_home + 32 * (act - expected_home)
                sim_elos[away] = e_away + 32 * ((1 - act) - (1 - expected_home))

            ordered = sorted(
                teams,
                key=lambda name: (
                    -sim_stats[name]["pts"],
                    -sim_stats[name]["gd"],
                    -sim_stats[name]["gf"],
                    name,
                ),
            )
            for place, team in enumerate(ordered, start=1):
                counts[team][place] += 1

    team_stats = {}
    n_teams = len(teams)
    for team in teams:
        arr = counts[team]
        promo_count = sum(arr[p] for p in range(1, min(promotion_cut, n_teams) + 1))
        relg_start = max(1, n_teams - relegation_cut + 1)
        relg_count = sum(arr[p] for p in range(relg_start, n_teams + 1)) if relegation_cut else 0
        modal_place = max(range(1, n_teams + 1), key=lambda place: arr[place])
        expected_place = sum(place * arr[place] for place in range(1, n_teams + 1)) / max(1, total_runs)
        promo_prob = round(100 * promo_count / max(1, total_runs), 1)
        relg_prob = round(100 * relg_count / max(1, total_runs), 1)
        team_stats[team] = {
            "promotion_probability": promo_prob,
            "relegation_probability": relg_prob,
            "stay_probability": round(100.0 - promo_prob - relg_prob, 1),
            "modal_place": modal_place,
            "expected_place": round(expected_place, 2),
            "place_counts": arr,
        }

    return {
        "team_stats": team_stats,
        "counts": counts,
        "total_runs": total_runs,
        "regression": regression,
    }


def is_forfeit(value):
    return isinstance(value, str) and value.lower().startswith("forfeit")


def has_played_score(row):
    return (
        row["date"] != "TBD"
        and row["home_goals"] != ""
        and row["away_goals"] != ""
    )


def build_team_slug(team, age_group, division, geography):
    return slugify(f"{team}-{age_group}-div-{division}-{geography}")


def flight_slug(age_group, division, geography):
    return slugify(f"{age_group}-div-{division}-{geography}")


def team_path(team_slug):
    return f"team/{team_slug}/"


def season_sort_key(season_name):
    if not season_name:
        return (0, 0)
    parts = season_name.split()
    if len(parts) != 2:
        return (0, 0)
    term, year = parts
    term_order = 0 if term == "Spring" else 1
    return (int(year), term_order)


def expected_result(elo_a, elo_b):
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 200))


def identify_playoff_visitors(rows, age_group, division, geography):
    """Return set of cleaned team names that appear to be playoff visitors in this flight.

    A playoff visitor is a team with very few games (≤ 3) in a flight where regular-season
    teams play 8+ games. OTHSL runs cross-geography playoff rounds at season end, and
    those visiting teams contaminate the regular-season standings.
    """
    flight_rows = [
        r for r in rows
        if r["age_group"] == age_group
        and r["division"] == division
        and r["geography"] == geography
        and is_real_team_name(r["home_team"])
        and is_real_team_name(r["away_team"])
    ]
    counts = defaultdict(int)
    for r in flight_rows:
        counts[clean_team_name(r["home_team"])] += 1
        counts[clean_team_name(r["away_team"])] += 1

    if not counts:
        return set()
    max_count = max(counts.values())
    if max_count < 8:
        return set()
    return {team for team, count in counts.items() if count <= 2}


def get_playoff_games_for_flight(rows, age_group, division, geography, playoff_visitors):
    """Return played games involving at least one playoff visitor, sorted by date."""
    if not playoff_visitors:
        return []
    games = []
    for r in rows:
        if (
            r["age_group"] != age_group
            or r["division"] != division
            or r["geography"] != geography
        ):
            continue
        if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
            continue
        ht = clean_team_name(r["home_team"])
        at = clean_team_name(r["away_team"])
        if ht not in playoff_visitors and at not in playoff_visitors:
            continue
        if not has_played_score(r) and not is_forfeit(r["home_goals"]) and not is_forfeit(r["away_goals"]):
            continue
        hg, ag_val = r["home_goals"], r["away_goals"]
        if is_forfeit(hg) or is_forfeit(ag_val):
            score = "F"
        else:
            score = f"{hg}–{ag_val}"
        games.append({
            "date": r["date"],
            "home": ht,
            "away": at,
            "score": score,
        })
    games.sort(key=lambda g: g["date"])
    return games


def get_standings_for_flight(rows, age_group, division, geography, selected_team=None, playoff_visitors=None):
    stats = {}

    for r in rows:
        if (
            r["age_group"] != age_group
            or r["division"] != division
            or r["geography"] != geography
        ):
            continue

        if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
            continue

        ht, at = clean_team_name(r["home_team"]), clean_team_name(r["away_team"])

        if playoff_visitors and (ht in playoff_visitors or at in playoff_visitors):
            continue

        hg, ag = r["home_goals"], r["away_goals"]

        for team in (ht, at):
            if team not in stats:
                stats[team] = {"gp": 0, "w": 0, "l": 0, "t": 0, "pts": 0, "gf": 0, "ga": 0}

        if is_forfeit(hg) or is_forfeit(ag):
            if not is_forfeit(hg) and is_forfeit(ag):
                stats[ht]["gf"] += 2
                stats[at]["ga"] += 2
                stats[ht]["w"] += 1
                stats[ht]["pts"] += 3
                stats[ht]["gp"] += 1
                stats[at]["l"] += 1
                stats[at]["pts"] -= 2
                stats[at]["gp"] += 1
            elif is_forfeit(hg) and not is_forfeit(ag):
                stats[at]["gf"] += 2
                stats[ht]["ga"] += 2
                stats[at]["w"] += 1
                stats[at]["pts"] += 3
                stats[at]["gp"] += 1
                stats[ht]["l"] += 1
                stats[ht]["pts"] -= 2
                stats[ht]["gp"] += 1
            else:
                stats[ht]["l"] += 1
                stats[ht]["pts"] -= 2
                stats[ht]["gp"] += 1
                stats[at]["l"] += 1
                stats[at]["pts"] -= 2
                stats[at]["gp"] += 1
        elif has_played_score(r):
            hg_i, ag_i = int(hg), int(ag)
            stats[ht]["gf"] += hg_i
            stats[ht]["ga"] += ag_i
            stats[ht]["gp"] += 1
            stats[at]["gf"] += ag_i
            stats[at]["ga"] += hg_i
            stats[at]["gp"] += 1

            if hg_i > ag_i:
                stats[ht]["w"] += 1
                stats[ht]["pts"] += 3
                stats[at]["l"] += 1
            elif hg_i < ag_i:
                stats[at]["w"] += 1
                stats[at]["pts"] += 3
                stats[ht]["l"] += 1
            else:
                stats[ht]["t"] += 1
                stats[ht]["pts"] += 1
                stats[at]["t"] += 1
                stats[at]["pts"] += 1

    table = []
    for team, s in stats.items():
        ppg = s["pts"] / s["gp"] if s["gp"] else 0
        table.append(
            {
                "team": team,
                "gp": s["gp"],
                "w": s["w"],
                "l": s["l"],
                "t": s["t"],
                "pts": s["pts"],
                "gf": s["gf"],
                "ga": s["ga"],
                "gd": s["gf"] - s["ga"],
                "ppg": round(ppg, 2),
                "is_selected": team == selected_team,
            }
        )

    table.sort(key=lambda x: (-x["ppg"], -x["gd"], -x["gf"], x["team"]))
    return table


def get_team_results(rows, team_info):
    games = []
    for r in rows:
        if (
            r["age_group"] != team_info["age_group"]
            or r["division"] != team_info["division"]
            or r["geography"] != team_info["geography"]
        ):
            continue

        if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
            continue

        home_team = clean_team_name(r["home_team"])
        away_team = clean_team_name(r["away_team"])
        is_home = home_team == team_info["team"]
        is_away = away_team == team_info["team"]
        if not is_home and not is_away:
            continue

        hg = r["home_goals"]
        ag = r["away_goals"]

        if r["notes"] == "double forfeit":
            result = "L"
        elif is_forfeit(hg) or is_forfeit(ag):
            if (is_home and is_forfeit(hg)) or (is_away and is_forfeit(ag)):
                result = "L"
            else:
                result = "W"
        elif not has_played_score(r):
            result = "F"
        else:
            hg_i, ag_i = int(hg), int(ag)
            gf, ga = (hg_i, ag_i) if is_home else (ag_i, hg_i)
            if gf > ga:
                result = "W"
            elif gf < ga:
                result = "L"
            else:
                result = "T"

        opponent = away_team if is_home else home_team
        venue = "H" if is_home else "A"

        if is_forfeit(hg) or is_forfeit(ag):
            score = "F"
        elif not has_played_score(r):
            score = "Scheduled"
        else:
            score = f"{hg}-{ag}" if is_home else f"{ag}-{hg}"

        games.append(
            {
                "date": r["date"],
                "opponent": opponent,
                "venue": venue,
                "score": score,
                "result": result,
                "notes": r.get("notes", ""),
            }
        )

    games.sort(key=lambda g: ("1" if g["date"] == "TBD" else "0") + g["date"])
    return games


def get_team_elo_history(team_info):
    history = []
    for r in get_elo_rows():
        if r["age_group"] != team_info["age_group"]:
            continue
        elo_after = None
        if clean_team_name(r["home_team"]) == team_info["team"]:
            elo_after = float(r["elo_home_after"])
        elif clean_team_name(r["away_team"]) == team_info["team"]:
            elo_after = float(r["elo_away_after"])

        if elo_after is None:
            continue

        history.append(
            {
                "season": r["season"],
                "age_group": r["age_group"],
                "date": r["date"],
                "elo": elo_after,
                "label": f"{r['season']} · {r['date']}",
            }
        )

    history.sort(key=lambda point: (season_sort_key(point["season"]), point["date"]))
    return history


def get_team_catalog(rows=None):
    if rows is None:
        rows = get_current_season_rows()
    catalog = {}

    for r in rows:
        flight = (r["age_group"], r["division"], r["geography"])
        for raw_team in (r["home_team"], r["away_team"]):
            if not is_real_team_name(raw_team):
                continue
            team = clean_team_name(raw_team)
            key = (*flight, team)
            if key in catalog:
                continue
            age_group, division, geography = flight
            slug = build_team_slug(team, age_group, division, geography)
            catalog[key] = {
                "team": team,
                "age_group": age_group,
                "division": division,
                "geography": geography,
                "slug": slug,
                "path": team_path(slug),
            }

    return sorted(
        catalog.values(),
        key=lambda x: (x["age_group"], int(x["division"]), x["geography"], x["team"]),
    )


def get_flight_catalog(rows=None):
    if rows is None:
        rows = get_current_season_rows()
    flight_rows = defaultdict(list)
    for r in rows:
        flight_rows[(r["age_group"], r["division"], r["geography"])].append(r)

    cards = []
    for (age_group, division, geography), flight_data in sorted(
        flight_rows.items(),
        key=lambda x: (x[0][0], int(x[0][1]), x[0][2]),
    ):
        standings = get_standings_for_flight(rows, age_group, division, geography)
        teams = sorted({
            clean_team_name(team)
            for r in flight_data
            for team in (r["home_team"], r["away_team"])
            if is_real_team_name(team)
        })
        played_games = sum(
            1
            for r in flight_data
            if is_real_team_name(r["home_team"])
            and is_real_team_name(r["away_team"])
            and (has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"]))
        )
        leader = standings[0]["team"] if standings else None
        cards.append(
            {
                "age_group": age_group,
                "division": division,
                "geography": geography,
                "label": f"{age_group} Division {division} {geography}",
                "slug": flight_slug(age_group, division, geography),
                "leader": leader,
                "team_count": len(teams),
                "game_count": played_games,
            }
        )

    return cards


def get_flight_catalog_grouped(rows=None):
    """Return flight catalog grouped by age_group for the compact directory grid."""
    cards = get_flight_catalog(rows)
    geo_abbr = {"North": "n", "South": "s", "Central": "c", "East": "e", "West": "w"}

    ag_map = defaultdict(list)
    for card in cards:
        ag_map[card["age_group"]].append(card)

    result = []
    for age_group in sorted(ag_map.keys()):
        flights = ag_map[age_group]
        div_map = defaultdict(list)
        for f in flights:
            div_map[f["division"]].append(f)

        divisions = []
        for div_num in sorted(div_map.keys(), key=lambda x: int(x)):
            div_flights = sorted(div_map[div_num], key=lambda x: x["geography"])
            divisions.append({
                "div_num": div_num,
                "flights": [
                    {
                        "geo": f["geography"],
                        "geo_abbr": geo_abbr.get(f["geography"], f["geography"][0].lower()),
                        "slug": f["slug"],
                    }
                    for f in div_flights
                ],
            })

        result.append({"age_group": age_group, "divisions": divisions})

    return result


def get_league_overview(rows=None):
    if rows is None:
        rows = get_current_season_rows()
    teams = sorted({
        clean_team_name(team)
        for r in rows
        for team in (r["home_team"], r["away_team"])
        if is_real_team_name(team)
    })
    flights = sorted({(r["age_group"], r["division"], r["geography"]) for r in rows})
    completed = sum(
        1
        for r in rows
        if is_real_team_name(r["home_team"])
        and is_real_team_name(r["away_team"])
        if has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"])
    )
    latest_dates = sorted({r["date"] for r in rows if r["date"] != "TBD"})

    return {
        "team_count": len(teams),
        "flight_count": len(flights),
        "game_count": len(rows),
        "completed_count": completed,
        "latest_date": latest_dates[-1] if latest_dates else None,
        "age_groups": sorted({r["age_group"] for r in rows}),
    }


def get_key_games():
    rows = get_current_season_rows()
    all_candidates = []
    standings_cache = {}
    latest_played_date = None

    for row in rows:
        if has_played_score(row) or is_forfeit(row["home_goals"]) or is_forfeit(row["away_goals"]):
            if row["date"] != "TBD":
                latest_played_date = max(latest_played_date, row["date"]) if latest_played_date else row["date"]

    for row in rows:
        if not is_real_team_name(row["home_team"]) or not is_real_team_name(row["away_team"]):
            continue

        home_team = clean_team_name(row["home_team"])
        away_team = clean_team_name(row["away_team"])
        flight_key = (row["age_group"], row["division"], row["geography"])
        if flight_key not in standings_cache:
            standings_cache[flight_key] = get_standings_for_flight(rows, *flight_key)

        standings = standings_cache[flight_key]
        position_lookup = {team_row["team"]: i + 1 for i, team_row in enumerate(standings)}
        row_is_future = not has_played_score(row) and not is_forfeit(row["home_goals"]) and not is_forfeit(row["away_goals"])
        row_is_recent = row["date"] == latest_played_date

        if not row_is_future and not row_is_recent:
            continue

        home_pos = position_lookup.get(home_team, len(standings))
        away_pos = position_lookup.get(away_team, len(standings))
        importance = (len(standings) - home_pos + 1) + (len(standings) - away_pos + 1)
        mode = "upcoming" if row_is_future else "recent"
        score = "vs"
        if has_played_score(row):
            score = f"{row['home_goals']}-{row['away_goals']}"
        elif is_forfeit(row["home_goals"]) or is_forfeit(row["away_goals"]):
            score = "Forfeit"

        all_candidates.append(
            {
                "mode": mode,
                "date": row["date"],
                "flight": f"{row['age_group']} Division {row['division']} {row['geography']}",
                "matchup": f"{home_team} vs {away_team}",
                "score": score,
                "context": f"{home_team} ({home_pos}) vs {away_team} ({away_pos})",
                "importance": importance,
            }
        )

    preferred_mode = "upcoming" if any(item["mode"] == "upcoming" for item in all_candidates) else "recent"
    filtered = [item for item in all_candidates if item["mode"] == preferred_mode]
    filtered.sort(key=lambda item: (-item["importance"], item["date"], item["matchup"]))
    return preferred_mode, filtered[:8]


def get_selector_data():
    team_catalog = get_team_catalog()
    age_groups = sorted({t["age_group"] for t in team_catalog})
    flight_map = defaultdict(set)
    team_map = defaultdict(list)

    for item in team_catalog:
        flight_key = (item["age_group"], item["division"], item["geography"])
        flight_map[item["age_group"]].add((item["division"], item["geography"]))
        team_map[flight_key].append(
            {
                "name": item["team"],
                "slug": item["slug"],
                "path": item["path"],
            }
        )

    flights_by_age = {
        age: [
            {"division": division, "geography": geography, "label": f"Division {division} {geography}"}
            for division, geography in sorted(options, key=lambda x: (int(x[0]), x[1]))
        ]
        for age, options in flight_map.items()
    }

    teams_by_flight = {
        f"{age}|{division}|{geography}": sorted(items, key=lambda x: x["name"])
        for (age, division, geography), items in team_map.items()
    }

    return {
        "age_groups": age_groups,
        "flights_by_age": flights_by_age,
        "teams_by_flight": teams_by_flight,
    }


def simulate_team_outlook(team_info, standings, rows):
    sim_data = get_flight_sim_data(team_info, standings, rows)
    outlook = simulate_flight_outlook(sim_data, total_runs=SIMULATION_RUNS)
    team_stats = outlook["team_stats"].get(team_info["team"])
    if not team_stats:
        return {
            "future_game_count": len(sim_data.get("remaining_games", [])),
            "place_probabilities": [],
            "promotion_probability": 0.0,
            "relegation_probability": 0.0,
            "stay_probability": 0.0,
            "summary": f"{SIMULATION_RUNS} simulations using current ELO ratings and the remaining scheduled games in this flight.",
        }

    place_probabilities = [
        {"place": place, "probability": round(100 * count / SIMULATION_RUNS, 1)}
        for place, count in enumerate(team_stats["place_counts"])
        if place and count
    ]
    summary = (
        "No remaining scheduled games are in the dataset, so the current table is treated as final."
        if not sim_data.get("remaining_games")
        else f"{SIMULATION_RUNS} simulations using current ELO ratings and the remaining scheduled games in this flight."
    )
    return {
        "future_game_count": len(sim_data.get("remaining_games", [])),
        "place_probabilities": place_probabilities,
        "promotion_probability": team_stats["promotion_probability"],
        "relegation_probability": team_stats["relegation_probability"],
        "stay_probability": team_stats["stay_probability"],
        "summary": summary,
        "expected_place": team_stats["expected_place"],
        "modal_place": team_stats["modal_place"],
    }


def get_flight_team_cards(team_info, standings, rows, playoff_visitors=None):
    """For each team in the flight return their slug, full played history, and next game."""
    age_group = team_info["age_group"]
    division  = team_info["division"]
    geography = team_info["geography"]

    flight_rows = [
        r for r in rows
        if r["age_group"] == age_group
        and r["division"] == division
        and r["geography"] == geography
    ]

    team_catalog = get_team_catalog()
    slug_map = {
        item["team"]: item["slug"]
        for item in team_catalog
        if item["age_group"] == age_group
        and item["division"] == division
        and item["geography"] == geography
    }

    played = sorted(
        [
            r for r in flight_rows
            if (has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"]))
            and (
                not playoff_visitors
                or (
                    clean_team_name(r["home_team"]) not in playoff_visitors
                    and clean_team_name(r["away_team"]) not in playoff_visitors
                )
            )
        ],
        key=lambda r: r["date"],
        reverse=True,
    )

    upcoming = sorted(
        [
            r for r in flight_rows
            if is_real_team_name(r["home_team"])
            and is_real_team_name(r["away_team"])
            and not has_played_score(r)
            and not is_forfeit(r["home_goals"])
            and not is_forfeit(r["away_goals"])
            and (
                not playoff_visitors
                or (
                    clean_team_name(r["home_team"]) not in playoff_visitors
                    and clean_team_name(r["away_team"]) not in playoff_visitors
                )
            )
        ],
        key=lambda r: (r["date"] == "TBD", r["date"]),
    )

    cards = {}
    for row in standings:
        team = row["team"]
        played_history = []
        for r in played:
            if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
                continue
            home_team = clean_team_name(r["home_team"])
            away_team = clean_team_name(r["away_team"])
            if home_team != team and away_team != team:
                continue
            is_home = home_team == team
            opp = away_team if is_home else home_team
            hg, ag = r["home_goals"], r["away_goals"]
            if is_forfeit(hg) or is_forfeit(ag):
                res   = "W" if (is_home and is_forfeit(ag)) or (not is_home and is_forfeit(hg)) else "L"
                score = "F"
            else:
                hg_i, ag_i = int(hg), int(ag)
                gf, ga = (hg_i, ag_i) if is_home else (ag_i, hg_i)
                res   = "W" if gf > ga else ("L" if gf < ga else "T")
                score = f"{gf}–{ga}"
            played_history.append({
                "date": r["date"],
                "display_date": format_game_date(r["date"]),
                "opponent": opp,
                "venue": "H" if is_home else "A",
                "score": score,
                "result": res,
            })

        next_game = None
        for r in upcoming:
            home_team = clean_team_name(r["home_team"])
            away_team = clean_team_name(r["away_team"])
            if team not in (home_team, away_team):
                continue
            is_home = home_team == team
            next_game = {
                "date": r["date"],
                "display_date": format_game_date(r["date"]),
                "opponent": away_team if is_home else home_team,
                "venue": "vs" if is_home else "@",
            }
            break

        cards[team] = {
            "slug": slug_map.get(team, ""),
            "recent": played_history[:3],
            "played": played_history,
            "next_game": next_game,
        }

    return cards


def get_flight_sim_data(team_info, standings, rows):
    """Return JSON-serializable data for the client-side JS simulation engine."""
    age_group = team_info["age_group"]
    division = team_info["division"]
    geography = team_info["geography"]

    flight_rows = [
        r for r in rows
        if r["age_group"] == age_group
        and r["division"] == division
        and r["geography"] == geography
    ]

    current_stats = {
        row["team"]: {"pts": row["pts"], "gd": row["gd"], "gf": row["gf"], "gp": row["gp"], "w": row["w"], "l": row["l"], "t": row["t"]}
        for row in standings
    }

    # Build ELO map before remaining-games loop so win probs can be embedded
    latest_elos = get_latest_elo_map()
    current_elos = {
        team: latest_elos.get((team, age_group), DEFAULT_ELO)
        for team in current_stats
    }
    current_regression = get_current_regression(current_stats)

    remaining = []
    for r in flight_rows:
        if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
            continue
        if not has_played_score(r) and not is_forfeit(r["home_goals"]) and not is_forfeit(r["away_goals"]):
            sel = team_info.get("team")
            home = clean_team_name(r["home_team"])
            away = clean_team_name(r["away_team"])
            probs = match_outcome_probabilities(
                current_elos.get(home, DEFAULT_ELO),
                current_elos.get(away, DEFAULT_ELO),
                regression=current_regression,
            )
            remaining.append({
                "id": f"{r['date']}|{home}|{away}",
                "home": home,
                "away": away,
                "date": r["date"],
                "involves_team": bool(sel and sel in (home, away)),
                "home_win_prob": probs["home_win_prob"],
                "draw_prob": probs["draw_prob"],
                "away_win_prob": probs["away_win_prob"],
            })

    # Fallback: if no scheduled games were scraped, infer remaining round-robin matchups
    schedule_inferred = False
    if not remaining:
        played_pairs: dict = {}
        for r in flight_rows:
            if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
                continue
            if has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"]):
                key = tuple(sorted([clean_team_name(r["home_team"]), clean_team_name(r["away_team"])]))
                played_pairs[key] = played_pairs.get(key, 0) + 1

        all_teams = list(current_stats.keys())
        for i, home in enumerate(all_teams):
            for j, away in enumerate(all_teams):
                if i >= j:
                    continue
                key = tuple(sorted([home, away]))
                times_played = played_pairs.get(key, 0)
                for _ in range(max(0, 2 - times_played)):
                    sel = team_info.get("team")
                    probs = match_outcome_probabilities(
                        current_elos.get(home, DEFAULT_ELO),
                        current_elos.get(away, DEFAULT_ELO),
                        regression=current_regression,
                    )
                    remaining.append({
                        "id": f"inferred|{home}|{away}",
                        "home": home,
                        "away": away,
                        "date": "TBD",
                        "involves_team": bool(sel and sel in (home, away)),
                        "home_win_prob": probs["home_win_prob"],
                        "draw_prob": probs["draw_prob"],
                        "away_win_prob": probs["away_win_prob"],
                    })
        if remaining:
            schedule_inferred = True

    teams = [row["team"] for row in standings]
    n = len(teams)
    return {
        "selected_team": team_info.get("team", ""),
        "teams": teams,
        "current_stats": current_stats,
        "current_elos": current_elos,
        "current_regression": current_regression,
        "regression_gp_full": REGRESSION_GP_FULL,
        "remaining_games": sorted(remaining, key=lambda g: g["date"]),
        "promotion_cut": 2,
        "relegation_cut": 2 if n >= 6 else (1 if n >= 4 else 0),
        "total_teams": n,
        "schedule_inferred": schedule_inferred,
    }


# --- HISTORICAL TEAM LOOKUP (commented out) ---
# def build_team_name_slug(team_name): ...
# --- END ---


def get_team_page_context(team_slug, season=None):
    if season is None:
        season = CURRENT_SEASON
    rows = get_rows_for_season(season)

    team_catalog = get_team_catalog(rows)
    team_info = None
    team_lookup = {item["slug"]: item for item in team_catalog}
    team_info = team_lookup.get(team_slug)

    if not team_info:
        return None
    games = get_team_results(rows, team_info)
    standings = get_standings_for_flight(
        rows,
        team_info["age_group"],
        team_info["division"],
        team_info["geography"],
        selected_team=team_info["team"],
    )
    elo_history = get_team_elo_history(team_info)
    simulation = simulate_team_outlook(team_info, standings, rows)
    sim_data = get_flight_sim_data(team_info, standings, rows)
    flight_team_cards = get_flight_team_cards(team_info, standings, rows)

    w = sum(1 for g in games if g["result"] == "W")
    l = sum(1 for g in games if g["result"] == "L")
    t = sum(1 for g in games if g["result"] == "T")
    season_elo_history = [point for point in elo_history if point["season"] == season]
    current_elo = season_elo_history[-1]["elo"] if season_elo_history else None
    standing = next((i + 1 for i, row in enumerate(standings) if row["is_selected"]), None)
    seasons_seen = []
    for point in elo_history:
        if point["season"] not in seasons_seen:
            seasons_seen.append(point["season"])

    team_info = dict(team_info)
    team_info["standing"] = standing

    # Determine top/bottom flight within this age group
    ag, div, geo = team_info["age_group"], team_info["division"], team_info["geography"]
    age_divs = {int(r["division"]) for r in rows if r["age_group"] == ag and r["division"].isdigit()}
    max_div = max(age_divs) if age_divs else int(div)
    is_top_flight = int(div) == 1
    is_bottom_flight = int(div) == max_div

    # Collect all played results for the flight (for matchweek history timeline)
    flight_results = []
    for r in rows:
        if r["age_group"] != ag or r["division"] != div or r["geography"] != geo:
            continue
        if not (has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"])):
            continue
        if r["date"] == "TBD":
            continue
        if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
            continue
        hg = r["home_goals"]
        ag_val = r["away_goals"]
        flight_results.append({
            "date": r["date"],
            "home": clean_team_name(r["home_team"]),
            "away": clean_team_name(r["away_team"]),
            "hg": int(hg) if hg.isdigit() else None,
            "ag": int(ag_val) if ag_val.isdigit() else None,
            "forfeit": is_forfeit(hg) or is_forfeit(ag_val),
            "home_forfeit": is_forfeit(hg),
        })

    return {
        "team_info": team_info,
        "games": games,
        "standings": standings,
        "elo_history": elo_history,
        "elo_range_options": [
            {"value": "current", "label": "This season" if season == CURRENT_SEASON else season},
            {"value": "5", "label": "Past 5 seasons"},
            {"value": "all", "label": "All seasons"},
        ],
        "elo_seasons": seasons_seen,
        "simulation": simulation,
        "sim_data": sim_data,
        "flight_team_cards": flight_team_cards,
        "flight_results": flight_results,
        "is_top_flight": is_top_flight,
        "is_bottom_flight": is_bottom_flight,
        "record": {"w": w, "l": l, "t": t},
        "current_elo": current_elo,
    }


def get_top_teams(rows=None):
    """Top 15 teams by current ELO rating, with current-season record overlay."""
    if rows is None:
        rows = get_current_season_rows()

    # Build current-season records per (team, age_group)
    season_records = defaultdict(lambda: {"gp": 0, "w": 0, "l": 0, "t": 0, "pts": 0})
    for r in rows:
        if not (has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"])):
            continue
        if not (is_real_team_name(r["home_team"]) and is_real_team_name(r["away_team"])):
            continue
        ht = clean_team_name(r["home_team"])
        at = clean_team_name(r["away_team"])
        ag = r["age_group"]
        hg, ag_g = r["home_goals"], r["away_goals"]
        if is_forfeit(hg) or is_forfeit(ag_g):
            hwin = is_forfeit(ag_g)
            awin = not hwin
        else:
            hg, ag_g = int(hg), int(ag_g)
            hwin = hg > ag_g; awin = ag_g > hg
        for team, win, loss in [(ht, hwin, awin), (at, awin, hwin)]:
            k = (team, r["age_group"])
            season_records[k]["gp"] += 1
            if win: season_records[k]["w"] += 1; season_records[k]["pts"] += 3
            elif loss: season_records[k]["l"] += 1
            else: season_records[k]["t"] += 1; season_records[k]["pts"] += 1

    # Get most recent ELO per (team, age_group) from elo_history
    elo_history = load_csv(os.path.join(DATA_DIR, "elo_history.csv"))
    latest_elo = {}  # (team, age_group) → elo
    for row in elo_history:
        ht = clean_team_name(row.get("home_team", ""))
        at = clean_team_name(row.get("away_team", ""))
        ag = row.get("age_group", "")
        try:
            latest_elo[(ht, ag)] = float(row["elo_home_after"])
            latest_elo[(at, ag)] = float(row["elo_away_after"])
        except (ValueError, KeyError):
            pass

    # Build flight lookup: (team, ag) → (flight_label, flight_slug)
    flight_lookup = {}
    flight_rows = defaultdict(list)
    for r in rows:
        flight_rows[(r["age_group"], r["division"], r["geography"])].append(r)
    for (ag, div, geo) in flight_rows:
        sl = flight_slug(ag, div, geo)
        label = f"{ag} Div {div} {geo}"
        pv = identify_playoff_visitors(rows, ag, div, geo)
        standings = get_standings_for_flight(rows, ag, div, geo, playoff_visitors=pv)
        for row in standings:
            flight_lookup[(row["team"], ag)] = (label, sl)

    # Only include teams in the current season's flights
    results = []
    for (team, ag), (flabel, fslug) in flight_lookup.items():
        elo = latest_elo.get((team, ag))
        if elo is None:
            continue
        rec = season_records.get((team, ag), {})
        results.append({
            "team": team,
            "elo": round(elo),
            "gp": rec.get("gp", 0),
            "w": rec.get("w", 0),
            "l": rec.get("l", 0),
            "t": rec.get("t", 0),
            "pts": rec.get("pts", 0),
            "age_group": ag,
            "flight_label": flabel,
            "flight_slug": fslug,
        })

    results.sort(key=lambda x: -x["elo"])
    return results[:15]


def get_featured_plinko(rows=None):
    """Homepage plinko teaser for Irish Village Over 55 Division 2 South."""
    if rows is None:
        rows = get_current_season_rows()


    flight_rows = defaultdict(list)
    for r in rows:
        if r["age_group"] and r["division"] and r["geography"]:
            flight_rows[(r["age_group"], r["division"], r["geography"])].append(r)

    def _build(target_team, target_ag, target_division=None, target_geography=None):
        for (ag, div, geo) in flight_rows:
            if ag != target_ag:
                continue
            if target_division and div != target_division:
                continue
            if target_geography and geo != target_geography:
                continue
            pv        = identify_playoff_visitors(rows, ag, div, geo)
            standings = get_standings_for_flight(rows, ag, div, geo, playoff_visitors=pv)
            if not standings:
                continue
            if not any(row["team"] == target_team for row in standings):
                continue
            n = len(standings)
            team_info = {"team": target_team, "age_group": ag, "division": div, "geography": geo}
            sim = simulate_team_outlook(team_info, standings, rows)
            if not sim.get("place_probabilities"):
                continue
            sl = flight_slug(ag, div, geo)
            sd = get_flight_sim_data(team_info, standings, rows)
            current_pos = next(
                (i + 1 for i, row in enumerate(standings) if row["team"] == target_team),
                (n + 1) // 2,
            )
            team_games = [g for g in sd.get("remaining_games", []) if g.get("involves_team")]
            remaining_lookup = {game["id"]: game for game in team_games}
            season_steps = []
            for r in sorted(flight_rows[(ag, div, geo)], key=lambda item: item["date"]):
                if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
                    continue
                home = clean_team_name(r["home_team"])
                away = clean_team_name(r["away_team"])
                if target_team not in (home, away):
                    continue

                is_home = home == target_team
                opponent = away if is_home else home
                step = {
                    "id": f"{r['date']}|{home}|{away}",
                    "date": r["date"],
                    "opponent": opponent,
                    "venue": "vs" if is_home else "@",
                    "fixed": False,
                }

                hg = r["home_goals"]
                ag_goals = r["away_goals"]
                if has_played_score(r) or is_forfeit(hg) or is_forfeit(ag_goals):
                    if is_forfeit(hg) or is_forfeit(ag_goals):
                        won = (is_home and is_forfeit(ag_goals)) or ((not is_home) and is_forfeit(hg))
                        step["result"] = "W" if won else "L"
                        step["score"] = "F"
                    else:
                        hg_i = int(hg)
                        ag_i = int(ag_goals)
                        gf, ga = (hg_i, ag_i) if is_home else (ag_i, hg_i)
                        step["result"] = "W" if gf > ga else ("L" if gf < ga else "T")
                        step["score"] = f"{gf}-{ga}"
                    step["fixed"] = True
                else:
                    game = remaining_lookup.get(step["id"])
                    if game:
                        step["home_win_prob"] = game["home_win_prob"]
                        step["draw_prob"] = game["draw_prob"]

                season_steps.append(step)

            return {
                "team": target_team,
                "flight_slug": sl,
                "flight_label": f"{ag} Div {div} {geo}",
                "n_teams": n,
                "promo_cut": 2,
                "relg_cut": 2 if n >= 6 else (1 if n >= 4 else 0),
                "place_probs": sim.get("place_probabilities", []),
                "promo_prob": sim.get("promotion_probability", 0),
                "relg_prob": sim.get("relegation_probability", 0),
                "current_position": current_pos,
                "team_games": team_games,
                "season_steps": season_steps,
                "games_played": sum(1 for step in season_steps if step["fixed"]),
                "games_remaining": len(team_games),
                "current_stats": sd.get("current_stats", {}),
                "remaining_games": sd.get("remaining_games", []),
                "teams": sd.get("teams", []),
            }
        return None

    # Fixed featured team — consistent across refreshes
    return _build("Irish Village", "Over 55", "2", "South")


# ── AI-generated insight paragraphs ───────────────────────────────────────────

_AI_FLIGHT_CACHE = os.path.join(DATA_DIR, "ai_flight_outlooks.json")
_AI_TEAM_CACHE   = os.path.join(DATA_DIR, "ai_team_insights.json")
_IMPORTANCE_CACHE = os.path.join(DATA_DIR, "flight_importance.json")


def calculate_flight_importance(sim_data, baseline_outlook=None, impact_runs=220):
    remaining_games = sim_data.get("remaining_games", [])
    teams = sim_data.get("teams", [])
    if not remaining_games or not teams:
        return {
            "formula_name": "Three-result swing for the two teams in the match",
            "formula": "Match score = home-win change + draw change + away-win change, where each change = promotion change for both teams + relegation change for both teams.",
            "formula_words": "For each remaining game, force a home win, a draw, and an away win. For each of those three results, add up how much the two teams in that match would change in promotion chance and relegation chance. Then add those three result totals together.",
            "impact_runs": impact_runs,
            "games": [],
            "top_games_by_team": {},
        }

    baseline_outlook = baseline_outlook or simulate_flight_outlook(sim_data, total_runs=impact_runs)
    baseline_stats = baseline_outlook.get("team_stats", {})
    game_reports = []
    top_games_by_team = {}

    for game in remaining_games:
        variants = {
            outcome: simulate_flight_outlook(sim_data, total_runs=impact_runs, overrides={game["id"]: outcome})
            for outcome in ("home", "draw", "away")
        }

        match_teams = (game["home"], game["away"])
        match_total = 0.0
        team_effects = {team: 0.0 for team in match_teams}
        outcome_effects = {}
        team_effects_by_outcome = {team: {} for team in match_teams}
        biggest_team = None
        biggest_team_effect = -1.0

        for outcome, variant_outlook in variants.items():
            outcome_total = 0.0
            variant_stats = variant_outlook.get("team_stats", {})
            for team in match_teams:
                base = baseline_stats.get(team, {})
                variant = variant_stats.get(team, {})
                delta = (
                    abs(variant.get("promotion_probability", 0.0) - base.get("promotion_probability", 0.0))
                    + abs(variant.get("relegation_probability", 0.0) - base.get("relegation_probability", 0.0))
                )
                team_effects_by_outcome[team][outcome] = round(delta, 2)
                outcome_total += delta
            outcome_effects[outcome] = round(outcome_total, 2)
            match_total += outcome_total

        for team in match_teams:
            team_total_effect = sum(team_effects_by_outcome[team].get(outcome, 0.0) for outcome in ("home", "draw", "away"))
            team_effects[team] = round(team_total_effect, 2)
            if team_total_effect > biggest_team_effect:
                biggest_team_effect = team_total_effect
                biggest_team = team

        outcome_labels = {
            "home": f"{game['home']} win",
            "draw": "Draw",
            "away": f"{game['away']} win",
        }
        biggest_outcome = max(outcome_effects, key=lambda outcome: outcome_effects[outcome]) if outcome_effects else None

        game_report = {
            "id": game["id"],
            "home": game["home"],
            "away": game["away"],
            "date": game["date"],
            "display_date": format_game_date(game["date"]),
            "home_win_prob": game.get("home_win_prob", 0.5),
            "draw_prob": game.get("draw_prob", DRAW_PROBABILITY),
            "away_win_prob": game.get("away_win_prob", 0.0),
            "total_effect": round(match_total, 2),
            "home_win_swing": outcome_effects.get("home", 0.0),
            "draw_swing": outcome_effects.get("draw", 0.0),
            "away_win_swing": outcome_effects.get("away", 0.0),
            "biggest_outcome": outcome_labels.get(biggest_outcome, ""),
            "biggest_outcome_swing": outcome_effects.get(biggest_outcome, 0.0) if biggest_outcome else 0.0,
            "most_affected_team": biggest_team,
            "most_affected_team_effect": round(biggest_team_effect, 2) if biggest_team else 0.0,
            "team_effects": team_effects,
        }
        game_reports.append(game_report)

        for team in (game["home"], game["away"]):
            best = top_games_by_team.get(team)
            if best is None or team_effects.get(team, 0.0) > best["team_effect"]:
                team_wp = game["home_win_prob"] if team == game["home"] else game["away_win_prob"]
                top_games_by_team[team] = {
                    "id": game["id"],
                    "opponent": game["away"] if team == game["home"] else game["home"],
                    "date": game["date"],
                    "display_date": format_game_date(game["date"]),
                    "venue": "vs" if team == game["home"] else "@",
                    "win_probability": round(100 * team_wp),
                    "draw_probability": round(100 * game.get("draw_prob", DRAW_PROBABILITY)),
                    "team_effect": round(team_effects.get(team, 0.0), 2),
                }

    game_reports.sort(key=lambda report: (-report["total_effect"], report["date"], report["home"], report["away"]))
    return {
        "formula_name": "Three-result swing for the two teams in the match",
        "formula": "Match score = home-win change + draw change + away-win change, where each change = promotion change for both teams + relegation change for both teams.",
        "formula_words": "For each remaining game, force a home win, a draw, and an away win. For each of those three results, add up how much the two teams in that match would change in promotion chance and relegation chance. Then add those three result totals together.",
        "impact_runs": impact_runs,
        "games": game_reports,
        "top_games_by_team": top_games_by_team,
    }


def build_flight_importance_fallback(label, standings, sim_data, importance):
    top_game = next((g for g in importance.get("games", []) if g.get("date") != "TBD"), None)
    if not top_game:
        return f"{label} has no remaining scheduled games, so the current table is effectively the final picture for promotion and relegation."

    promo_cut = sim_data.get("promotion_cut", 2)
    relg_cut = sim_data.get("relegation_cut", 0)
    leader_names = ", ".join(row["team"] for row in standings[:promo_cut])
    danger_names = ", ".join(row["team"] for row in standings[len(standings) - relg_cut:]) if relg_cut else "none"
    return (
        f"{label} is being driven by a tight race around the promotion and relegation lines, with {leader_names} currently in the promotion spots and {danger_names} under the most pressure. "
        f"The biggest remaining game is {top_game['home']} vs {top_game['away']} on {top_game['display_date']}, because this matchup changes the promotion and relegation outlook for those two teams more than any other game left. "
        f"Its match score is {top_game['total_effect']:.2f}, built from {top_game['home_win_swing']:.2f} if {top_game['home']} win, {top_game['draw_swing']:.2f} if it ends in a draw, and {top_game['away_win_swing']:.2f} if {top_game['away']} win."
    )


def build_team_insight_fallback(team, standings, simulation, top_game, recent_games):
    pos = next((i + 1 for i, row in enumerate(standings) if row["team"] == team), None)
    row = next((item for item in standings if item["team"] == team), {})
    promo_prob = simulation.get("promotion_probability", 0.0)
    relg_prob = simulation.get("relegation_probability", 0.0)
    stay_prob = simulation.get("stay_probability", 0.0)
    modal_place = simulation.get("modal_place", pos or 0)
    expected_place = simulation.get("expected_place", pos or 0)
    recent = recent_games[:3]
    recent_txt = ", ".join(f"{g['score']} {g['result']} vs {g['opponent']}" for g in recent) if recent else "no recent results"
    if top_game:
        game_txt = (
            f"The biggest swing game for them is {top_game['venue']} {top_game['opponent']} on {top_game['display_date']}, "
            f"where the three possible results create a total match swing of {top_game['team_effect']:.2f} for their own promotion and relegation outlook."
        )
    else:
        game_txt = "They do not have a scheduled remaining game that materially changes their outlook right now."
    return (
        f"{team} are {pos}th with {row.get('pts', 0)} points and GD {row.get('gd', 0):+d}, and the model currently gives them "
        f"{promo_prob:.1f}% promotion, {stay_prob:.1f}% stay, and {relg_prob:.1f}% relegation odds, with a most likely finish around {modal_place}th "
        f"(expected place {expected_place:.2f}). Recent form: {recent_txt}. {game_txt}"
    )


def get_flight_preview_map(rows):
    preview_map = {}
    flights = {
        (r["age_group"], r["division"], r["geography"])
        for r in rows
        if r["age_group"] and r["division"] and r["geography"]
    }
    for age_group, division, geography in flights:
        playoff_visitors = identify_playoff_visitors(rows, age_group, division, geography)
        standings = get_standings_for_flight(rows, age_group, division, geography, playoff_visitors=playoff_visitors)
        if not standings:
            continue
        preview_map[flight_slug(age_group, division, geography)] = {
            "label": f"{age_group} Division {division} {geography}",
            "standings": [
                {"team": row["team"], "pts": row["pts"], "gd": row["gd"], "gp": row["gp"]}
                for row in standings
            ],
        }
    return preview_map


def _openai_complete(prompt):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=220,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        try:
            body = json.dumps({
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 220,
                "temperature": 0.7,
            }).encode("utf-8")
            req = Request(
                "https://api.openai.com/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            return None


def generate_ai_flight_outlook(age_group, division, geography, standings, sim_data):
    """One paragraph about what drives promotion/relegation in this flight."""
    n = len(standings)
    promo_cut = 2
    relg_cut  = 2 if n >= 6 else (1 if n >= 4 else 0)

    rows_txt = "\n".join(
        f"  {i+1}. {row['team']} — {row['pts']}pts, GD {row['gd']:+d}, "
        f"{row.get('w',0)}W {row.get('l',0)}L {row.get('d',0)}D"
        for i, row in enumerate(standings)
    )
    promo_names = ", ".join(row["team"] for row in standings[:promo_cut])
    relg_names  = ", ".join(row["team"] for row in standings[n - relg_cut:])

    remaining = sim_data.get("remaining_games", [])
    n_remaining = len(remaining)

    prompt = (
        f"You are a soccer analyst writing for an adult recreational league (OTHSL) website. "
        f"Write exactly 2 sentences (no more) about what will most likely decide promotion and relegation "
        f"in the {age_group} Division {division} {geography} flight this season. "
        f"Current standings ({n_remaining} games remaining):\n{rows_txt}\n"
        f"Top {promo_cut} promote: currently {promo_names}. "
        f"Bottom {relg_cut} relegate: currently {relg_names}. "
        f"Be specific — mention team names and point gaps. Casual, confident tone. No fluff."
    )
    return _openai_complete(prompt)


def generate_ai_team_insight(team, age_group, standings, sim_data):
    """One sentence about the team's most important remaining game."""
    remaining = [g for g in sim_data.get("remaining_games", []) if g.get("involves_team")]
    if not remaining:
        return None

    pos = next((i + 1 for i, r in enumerate(standings) if r["team"] == team), None)
    n   = len(standings)
    pts = next((r["pts"] for r in standings if r["team"] == team), 0)
    promo_pts = standings[1]["pts"] if len(standings) > 1 else pts
    relg_pts  = standings[n - 2]["pts"] if n >= 2 else pts
    promo_prob = sim_data.get("promotion_probability", 0)
    relg_prob  = sim_data.get("relegation_probability", 0)

    games_txt = ", ".join(
        f"{'vs' if g['home'] == team else '@'} {g['away'] if g['home'] == team else g['home']} ({g['date']})"
        for g in remaining[:4]
    )

    prompt = (
        f"You are a soccer analyst for an adult recreational league (OTHSL). "
        f"{team} sits {pos}/{n} with {pts} pts in the {age_group} flight. "
        f"Promotion probability: {promo_prob}%. Relegation probability: {relg_prob}%. "
        f"Points from top 2: {promo_pts - pts:+d}. Points from relegation zone: {pts - relg_pts:+d}. "
        f"Remaining games involving {team}: {games_txt}. "
        f"Write exactly 1 sentence identifying their single most important remaining game and why. "
        f"Mention the opponent and date. Casual, confident tone. No fluff."
    )
    return _openai_complete(prompt)


def generate_ai_flight_outlook_v2(age_group, division, geography, standings, sim_data, flight_outlook, importance):
    promo_cut = sim_data.get("promotion_cut", 2)
    relg_cut = sim_data.get("relegation_cut", 0)
    team_stats = flight_outlook.get("team_stats", {})
    top_games = importance.get("games", [])[:3]

    rows_txt = "\n".join(
        f"  {i+1}. {row['team']} - {row['pts']}pts, GD {row['gd']:+d}, "
        f"{team_stats.get(row['team'], {}).get('promotion_probability', 0):.1f}% promo, "
        f"{team_stats.get(row['team'], {}).get('relegation_probability', 0):.1f}% relegation"
        for i, row in enumerate(standings)
    )
    key_games_txt = "; ".join(
        f"{g['home']} vs {g['away']} on {g['display_date']} "
        f"(match score {g['total_effect']}, "
        f"{g['biggest_outcome']} changes the two teams by {g['biggest_outcome_swing']}, "
        f"{round(g['home_win_prob'] * 100)}% home win, {round(g['draw_prob'] * 100)}% draw)"
        for g in top_games
    ) or "No major remaining games."

    prompt = (
        f"You are writing a single paragraph for the OTHSL website in clean, natural American English. "
        f"Write 3 to 4 sentences on the {age_group} Division {division} {geography} race in {CURRENT_SEASON}. "
        f"Use exact team names, point gaps, and promotion/relegation percentages from the simulation. "
        f"Mention the biggest race at the top, the key danger at the bottom, and the single most important upcoming game. "
        f"The importance model is: {importance.get('formula_words', '')}. "
        f"Avoid hype, filler, and bullet formatting.\n\n"
        f"Standings and simulation snapshot:\n{rows_txt}\n\n"
        f"Promotion spots: top {promo_cut}. Relegation spots: bottom {relg_cut}.\n"
        f"Highest-impact upcoming games from the what-if simulation: {key_games_txt}"
    )
    return _openai_complete(prompt)


def generate_ai_team_insight_v2(team, age_group, standings, sim_data, team_outlook, top_game):
    pos = next((i + 1 for i, r in enumerate(standings) if r["team"] == team), None)
    n = len(standings)
    pts = next((r["pts"] for r in standings if r["team"] == team), 0)
    gd = next((r["gd"] for r in standings if r["team"] == team), 0)
    promo_prob = team_outlook.get("promotion_probability", 0)
    relg_prob = team_outlook.get("relegation_probability", 0)
    expected_place = team_outlook.get("expected_place", 0)
    modal_place = team_outlook.get("modal_place", pos or 0)
    promotion_cut = sim_data.get("promotion_cut", 2)
    relg_cut = sim_data.get("relegation_cut", 0)
    promo_line_idx = min(max(0, promotion_cut - 1), max(0, n - 1))
    promo_line_pts = standings[promo_line_idx]["pts"] if standings else pts
    safe_idx = n - relg_cut - 1
    safe_pts = standings[safe_idx]["pts"] if relg_cut and safe_idx >= 0 else pts
    remaining = [g for g in sim_data.get("remaining_games", []) if team in (g.get("home"), g.get("away"))]
    top_game_txt = "No meaningful remaining game identified."
    if top_game:
        top_game_txt = (
            f"{top_game['venue']} {top_game['opponent']} on {top_game['display_date']} "
            f"({top_game['win_probability']}% win, {top_game['draw_probability']}% draw, effect {top_game['team_effect']})"
        )
    remaining_txt = "; ".join(
        f"{'vs' if g['home'] == team else '@'} {g['away'] if g['home'] == team else g['home']} on {format_game_date(g['date'])}"
        for g in remaining[:5]
    ) or "No remaining games."

    prompt = (
        f"You are writing a single paragraph for the OTHSL website in clean, natural American English. "
        f"Write 2 to 3 sentences about {team}'s current outlook in {CURRENT_SEASON} {age_group}. "
        f"Use exact standings facts, their simulated promotion/relegation chances, and identify their most important remaining match. "
        f"Keep it concise, specific, and readable.\n\n"
        f"{team} are {pos}th of {n} with {pts} points and GD {gd:+d}. "
        f"Simulation: {promo_prob:.1f}% promotion, {relg_prob:.1f}% relegation, expected finish {expected_place}, modal finish {modal_place}. "
        f"Gap to the promotion line: {promo_line_pts - pts:+d} points. Gap to safety: {pts - safe_pts:+d} points. "
        f"Most important remaining game: {top_game_txt}. "
        f"Remaining schedule sample: {remaining_txt}"
    )
    return _openai_complete(prompt)


def build_ai_caches(rows=None):
    """Generate and save all AI insight paragraphs for the current season."""
    if rows is None:
        rows = get_current_season_rows()

    flight_cache = {}
    team_cache   = {}
    importance_cache = {}

    flight_rows = defaultdict(list)
    for r in rows:
        if r["age_group"] and r["division"] and r["geography"]:
            flight_rows[(r["age_group"], r["division"], r["geography"])].append(r)

    for (ag, div, geo) in sorted(flight_rows):
        pv        = identify_playoff_visitors(rows, ag, div, geo)
        standings = get_standings_for_flight(rows, ag, div, geo, playoff_visitors=pv)
        if not standings:
            continue
        sl = flight_slug(ag, div, geo)

        team_info = {"team": standings[0]["team"], "age_group": ag, "division": div, "geography": geo}
        flight_sim = get_flight_sim_data(team_info, standings, rows)
        flight_outlook = simulate_flight_outlook(flight_sim, total_runs=SIMULATION_RUNS)
        importance = calculate_flight_importance(flight_sim, baseline_outlook=flight_outlook, impact_runs=IMPORTANCE_SIM_RUNS)
        importance_cache[sl] = importance

        print(f"  Flight {sl}…", end=" ", flush=True)
        outlook = generate_ai_flight_outlook_v2(ag, div, geo, standings, flight_sim, flight_outlook, importance)
        if not outlook:
            outlook = build_flight_importance_fallback(f"{ag} Division {div} {geo}", standings, flight_sim, importance)
        flight_cache[sl] = outlook
        print("done")

        for row in standings:
            slug = build_team_slug(row["team"], ag, div, geo)
            print(f"    Team {row['team']}…", end=" ", flush=True)
            insight = generate_ai_team_insight_v2(
                row["team"],
                ag,
                standings,
                flight_sim,
                flight_outlook["team_stats"].get(row["team"], {}),
                importance["top_games_by_team"].get(row["team"]),
            )
            team_cache[slug] = insight
            print("done")

    with open(_AI_FLIGHT_CACHE, "w") as f:
        json.dump(flight_cache, f)
    with open(_AI_TEAM_CACHE, "w") as f:
        json.dump(team_cache, f)
    with open(_IMPORTANCE_CACHE, "w") as f:
        json.dump(importance_cache, f)
    global _ai_flight_texts, _ai_team_texts, _flight_importance_cache
    _ai_flight_texts = flight_cache
    _ai_team_texts = team_cache
    _flight_importance_cache = importance_cache
    print("AI caches saved.")


def load_ai_flight_outlook(flight_slug_val):
    if not os.path.exists(_AI_FLIGHT_CACHE):
        return None
    with open(_AI_FLIGHT_CACHE) as f:
        return json.load(f).get(flight_slug_val)


def load_ai_team_insight(team_slug_val):
    if not os.path.exists(_AI_TEAM_CACHE):
        return None
    with open(_AI_TEAM_CACHE) as f:
        return json.load(f).get(team_slug_val)


def load_flight_importance(flight_slug_val):
    if not os.path.exists(_IMPORTANCE_CACHE):
        return None
    with open(_IMPORTANCE_CACHE) as f:
        return json.load(f).get(flight_slug_val)


def get_flight_importance_report(flight_slug_val, sim_data, baseline_outlook=None):
    cached = _flight_importance_cache.get(flight_slug_val)
    if cached:
        return cached
    report = calculate_flight_importance(sim_data, baseline_outlook=baseline_outlook, impact_runs=IMPORTANCE_SIM_RUNS)
    _flight_importance_cache[flight_slug_val] = report
    return report


def load_ai_caches_into_memory():
    global _ai_flight_texts, _ai_team_texts, _flight_importance_cache
    _ai_flight_texts = {}
    _ai_team_texts = {}
    _flight_importance_cache = {}
    if os.path.exists(_AI_FLIGHT_CACHE):
        with open(_AI_FLIGHT_CACHE) as f:
            _ai_flight_texts = json.load(f)
    if os.path.exists(_AI_TEAM_CACHE):
        with open(_AI_TEAM_CACHE) as f:
            _ai_team_texts = json.load(f)
    if os.path.exists(_IMPORTANCE_CACHE):
        with open(_IMPORTANCE_CACHE) as f:
            _flight_importance_cache = json.load(f)


def get_season_outlook_calibration():
    """
    For each completed historical season/flight, run a simulation at each
    game-week checkpoint and compare predicted promo/relg probabilities to
    actual outcomes. Returns bucketed calibration curves indexed by week 1-10.
    """
    SIM_RUNS  = 100
    MAX_WEEKS = 10
    N_BUCKETS = 10
    DP = 0.22

    # Build ELO timeline: (team, ag) -> sorted [(date, elo_after)]
    elo_hist = load_csv(os.path.join(DATA_DIR, "elo_history.csv"))
    elo_tl = defaultdict(list)
    for row in elo_hist:
        try:
            d  = row["date"]
            ag = row["age_group"]
            elo_tl[(clean_team_name(row["home_team"]), ag)].append((d, float(row["elo_home_after"])))
            elo_tl[(clean_team_name(row["away_team"]), ag)].append((d, float(row["elo_away_after"])))
        except (ValueError, KeyError):
            pass
    for k in elo_tl:
        elo_tl[k].sort()

    def get_elo(team, ag, cutoff):
        elo = DEFAULT_ELO
        for d, e in elo_tl.get((team, ag), []):
            if d <= cutoff:
                elo = e
            else:
                break
        return elo

    def sim_once(teams, base, elos, remaining):
        st = {t: dict(s) for t, s in base.items()}
        el = dict(elos)
        avg_gp = sum(s.get("gp", 0) for s in base.values()) / max(1, len(base))
        reg = max(0.0, REGRESSION_MAX * (1.0 - avg_gp / REGRESSION_GP_FULL))
        for home, away in remaining:
            if home not in st or away not in st:
                continue
            we = expected_result(el.get(home, DEFAULT_ELO), el.get(away, DEFAULT_ELO))
            we = we * (1.0 - reg) + 0.5 * reg
            hp = max(0.05, min(0.90, we - DP / 2))
            r  = random.random()
            if r < hp:
                st[home]["pts"] += 3; st[home]["gd"] += 1; st[home]["gf"] += 2
                st[away]["gd"]  -= 1; st[away]["gf"] += 1
            elif r < hp + DP:
                st[home]["pts"] += 1; st[away]["pts"] += 1
                st[home]["gf"]  += 1; st[away]["gf"]  += 1
            else:
                st[away]["pts"] += 3; st[away]["gd"] += 1; st[away]["gf"] += 2
                st[home]["gd"]  -= 1; st[home]["gf"] += 1
            act = 1.0 if r < hp else (0.5 if r < hp + DP else 0.0)
            ex  = expected_result(el.get(home, DEFAULT_ELO), el.get(away, DEFAULT_ELO))
            el[home] = el.get(home, DEFAULT_ELO) + 32 * (act - ex)
            el[away] = el.get(away, DEFAULT_ELO) + 32 * ((1 - act) - (1 - ex))
        return sorted(teams, key=lambda t: (-st[t]["pts"], -st[t]["gd"], -st[t]["gf"], t))

    # week_pts[week]["promo"|"relg"] = list of (pred, actual)
    week_pts = {w: {"promo": [], "relg": []} for w in range(MAX_WEEKS + 1)}

    for season in get_all_seasons():
        if season == CURRENT_SEASON:
            continue
        rows = get_rows_for_season(season)
        flight_rows = defaultdict(list)
        for r in rows:
            if r["age_group"] and r["division"] and r["geography"]:
                flight_rows[(r["age_group"], r["division"], r["geography"])].append(r)

        for (ag, div, geo), frows in flight_rows.items():
            pv = identify_playoff_visitors(rows, ag, div, geo)

            def real(r):
                ht = clean_team_name(r["home_team"])
                at = clean_team_name(r["away_team"])
                if not (is_real_team_name(r["home_team"]) and is_real_team_name(r["away_team"])):
                    return False
                if pv and (ht in pv or at in pv):
                    return False
                return True

            played_rows = [r for r in frows if real(r) and
                           (has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"]))]
            all_game_rows = [r for r in frows if real(r)]

            teams = sorted({
                name
                for r in all_game_rows
                for name in (clean_team_name(r["home_team"]), clean_team_name(r["away_team"]))
            })
            n = len(teams)
            if n < 4:
                continue

            promo_cut = 2
            relg_cut  = 2 if n >= 6 else (1 if n >= 4 else 0)

            final = get_standings_for_flight(rows, ag, div, geo, playoff_visitors=pv)
            if not final:
                continue
            actual_promo = {row["team"] for i, row in enumerate(final) if i < promo_cut}
            actual_relg  = {row["team"] for i, row in enumerate(final) if i >= n - relg_cut}

            dates = sorted({r["date"] for r in played_rows if r["date"] != "TBD"})
            if not dates:
                continue

            # Week 0 = before any games (pure ELO)
            pre_season = (datetime.strptime(dates[0][:10], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            checkpoints = [(0, None, pre_season)] + [(w, dates[w - 1], dates[w - 1]) for w in range(1, min(MAX_WEEKS, len(dates)) + 1)]

            for week, cutoff, elo_cutoff in checkpoints:
                base = {t: {"pts": 0, "gd": 0, "gf": 0} for t in teams}
                if cutoff:
                    for r in played_rows:
                        if r["date"] > cutoff:
                            continue
                        ht = clean_team_name(r["home_team"])
                        at = clean_team_name(r["away_team"])
                        if ht not in base or at not in base:
                            continue
                        hg_s, ag_s = r["home_goals"], r["away_goals"]
                        if is_forfeit(hg_s) or is_forfeit(ag_s):
                            winner, loser = (at, ht) if is_forfeit(hg_s) else (ht, at)
                            base[winner]["pts"] += 3; base[winner]["gd"] += 1; base[winner]["gf"] += 2
                            base[loser]["gd"]   -= 1; base[loser]["gf"]  += 1
                        elif hg_s.isdigit() and ag_s.isdigit():
                            hg, ag_g = int(hg_s), int(ag_s)
                            base[ht]["gf"] += hg; base[ht]["gd"] += hg - ag_g
                            base[at]["gf"] += ag_g; base[at]["gd"] += ag_g - hg
                            if hg > ag_g:   base[ht]["pts"] += 3
                            elif ag_g > hg: base[at]["pts"] += 3
                            else:           base[ht]["pts"] += 1; base[at]["pts"] += 1

                elos = {t: get_elo(t, ag, elo_cutoff) for t in teams}
                remaining = [(clean_team_name(r["home_team"]), clean_team_name(r["away_team"]))
                             for r in all_game_rows
                             if not cutoff or r["date"] == "TBD" or r["date"] > cutoff]

                promo_ct = defaultdict(int)
                relg_ct  = defaultdict(int)
                for _ in range(SIM_RUNS):
                    ranked = sim_once(teams, base, elos, remaining)
                    for i, t in enumerate(ranked):
                        if i < promo_cut:        promo_ct[t] += 1
                        if i >= n - relg_cut:    relg_ct[t]  += 1

                for t in teams:
                    pp = promo_ct[t] / SIM_RUNS
                    rp = relg_ct[t]  / SIM_RUNS
                    week_pts[week]["promo"].append((pp, 1 if t in actual_promo else 0))
                    week_pts[week]["relg"].append( (rp, 1 if t in actual_relg  else 0))

    def bucket(pairs):
        bp, ba, bn = defaultdict(float), defaultdict(float), defaultdict(int)
        for pred, actual in pairs:
            idx = min(int(pred * N_BUCKETS), N_BUCKETS - 1)
            bp[idx] += pred; ba[idx] += actual; bn[idx] += 1
        return [{"pred": round(bp[i]/bn[i], 3), "actual": round(ba[i]/bn[i], 3), "n": bn[i]}
                for i in range(N_BUCKETS) if bn[i] >= 5]

    return {
        str(w): {"promo": bucket(week_pts[w]["promo"]), "relg": bucket(week_pts[w]["relg"])}
        for w in range(MAX_WEEKS + 1)
    }


def get_calibration_data():
    """Compute ELO calibration stats from full elo_history.csv."""
    history = load_csv(os.path.join(DATA_DIR, "elo_history.csv"))
    N = 20  # 5%-wide buckets
    bucket_pred  = defaultdict(float)
    bucket_act   = defaultdict(float)
    bucket_n     = defaultdict(int)
    brier = 0.0
    total = 0
    correct = 0
    for row in history:
        hg = row.get("home_goals", "")
        ag = row.get("away_goals", "")
        if not (hg.isdigit() and ag.isdigit()):
            continue
        try:
            eh = float(row["elo_home_before"])
            ea = float(row["elo_away_before"])
            hg, ag = int(hg), int(ag)
        except (ValueError, KeyError):
            continue
        exp = 1.0 / (1.0 + 10 ** ((ea - eh) / 200))
        actual = 1.0 if hg > ag else 0.0 if hg < ag else 0.5
        idx = min(int(exp * N), N - 1)
        bucket_pred[idx] += exp
        bucket_act[idx]  += actual
        bucket_n[idx]    += 1
        brier += (exp - actual) ** 2
        total += 1
        if eh != ea:
            fav_won = (exp > 0.5 and actual == 1.0) or (exp < 0.5 and actual == 0.0)
            if fav_won:
                correct += 1
    points = []
    for i in range(N):
        n = bucket_n[i]
        if n >= 20:
            points.append({
                "pred": round(bucket_pred[i] / n, 3),
                "actual": round(bucket_act[i] / n, 3),
                "n": n,
            })
    # Also count draws for accuracy denominator (exclude equal ELOs)
    non_equal = sum(bucket_n[i] for i in range(N))
    return {
        "points": points,
        "brier": round(brier / total, 4) if total else 0,
        "total": total,
        "favorite_win_pct": round(100 * correct / non_equal, 1) if non_equal else 0,
    }


def _render_index(season, home_path, season_nav_prefix):
    all_seasons = get_all_seasons()
    season_slug = season_to_slug(season)
    is_current = (season == CURRENT_SEASON)
    rows = get_rows_for_season(season)
    key_games_mode, key_games = (get_key_games() if is_current else (None, []))
    seasons_for_select = [
        {"name": s, "slug": season_to_slug(s)}
        for s in reversed(all_seasons)
    ]
    return render_template(
        "index.html",
        season=season,
        season_slug=season_slug,
        is_current_season=is_current,
        all_seasons=seasons_for_select,
        current_season_slug=season_to_slug(CURRENT_SEASON),
        league_overview=get_league_overview(rows),
        flight_groups=get_flight_catalog_grouped(rows),
        flight_url_prefix="flight/" if is_current else "flight/",
        home_path=home_path,
        season_nav_prefix=season_nav_prefix,
        calibration_path=home_path + "calibration/",
        top_teams=get_top_teams(rows) if is_current else [],
        featured_plinko=get_featured_plinko(rows),
        key_games=key_games,
        key_games_mode=key_games_mode,
    )


@app.route("/")
def index():
    # Support ?season= for local dev; static site uses /season/<slug>/ pages.
    season_slug_param = request.args.get("season")
    all_seasons = get_all_seasons()
    if season_slug_param:
        season = slug_to_season(season_slug_param)
        if season not in all_seasons:
            season = CURRENT_SEASON
    else:
        season = CURRENT_SEASON
    return _render_index(season, home_path="./", season_nav_prefix="season/")


@app.route("/season/<season_slug>/")
def index_historical(season_slug):
    all_seasons = get_all_seasons()
    season = slug_to_season(season_slug)
    if season not in all_seasons or season == CURRENT_SEASON:
        abort(404)
    return _render_index(season, home_path="../../", season_nav_prefix="../")


@app.route("/team/<team_slug>/")
def team_page(team_slug):
    context = get_team_page_context(team_slug, season=CURRENT_SEASON)
    if not context:
        abort(404)
    ti = context["team_info"]
    team_fslug = flight_slug(ti["age_group"], ti["division"], ti["geography"])
    team_ai_text = _ai_team_texts.get(team_slug, "")
    if not team_ai_text:
        importance = get_flight_importance_report(team_fslug, context["sim_data"])
        team_ai_text = build_team_insight_fallback(
            ti["team"],
            context["standings"],
            context["simulation"],
            importance.get("top_games_by_team", {}).get(ti["team"]),
            context["games"],
        )
    return render_template("team.html", season=CURRENT_SEASON,
                           ai_text=team_ai_text,
                           team_flight_slug=team_fslug, **context)


@app.route("/season/<season_slug>/team/<team_slug>/")
def team_page_historical(season_slug, team_slug):
    season = slug_to_season(season_slug)
    all_seasons = get_all_seasons()
    if season not in all_seasons:
        abort(404)
    context = get_team_page_context(team_slug, season=season)
    if not context:
        abort(404)
    ti = context["team_info"]
    team_fslug = flight_slug(ti["age_group"], ti["division"], ti["geography"])
    return render_template("team.html", season=season,
                           ai_text="",
                           team_flight_slug=team_fslug, **context)


def get_flight_page_context(age_group, division, geography, rows=None):
    if rows is None:
        rows = get_current_season_rows()

    playoff_visitors = identify_playoff_visitors(rows, age_group, division, geography)
    standings = get_standings_for_flight(
        rows, age_group, division, geography, playoff_visitors=playoff_visitors
    )
    if not standings:
        return None
    # Attach team slugs so the template can link to team pages
    for row in standings:
        row["slug"] = build_team_slug(row["team"], age_group, division, geography)
    team_info = {"age_group": age_group, "division": division, "geography": geography}
    sim_data = get_flight_sim_data(team_info, standings, rows)
    flight_team_cards = get_flight_team_cards(team_info, standings, rows, playoff_visitors=playoff_visitors)
    playoff_games = get_playoff_games_for_flight(rows, age_group, division, geography, playoff_visitors)
    age_divs = {int(r["division"]) for r in rows if r["age_group"] == age_group and r["division"].isdigit()}
    max_div = max(age_divs) if age_divs else int(division)
    is_top_flight = int(division) == 1
    is_bottom_flight = int(division) == max_div

    # Collect played results for the matchweek history timeline (exclude playoff games)
    flight_results = []
    for r in rows:
        if r["age_group"] != age_group or r["division"] != division or r["geography"] != geography:
            continue
        if not (has_played_score(r) or is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"])):
            continue
        if r["date"] == "TBD":
            continue
        if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
            continue
        ht = clean_team_name(r["home_team"])
        at = clean_team_name(r["away_team"])
        if playoff_visitors and (ht in playoff_visitors or at in playoff_visitors):
            continue
        hg = r["home_goals"]
        ag = r["away_goals"]
        flight_results.append({
            "date": r["date"],
            "home": ht,
            "away": at,
            "hg": int(hg) if hg.isdigit() else None,
            "ag": int(ag) if ag.isdigit() else None,
            "forfeit": is_forfeit(hg) or is_forfeit(ag),
            "home_forfeit": is_forfeit(hg),
        })

    return {
        "age_group": age_group,
        "division": division,
        "geography": geography,
        "label": f"{age_group} Division {division} {geography}",
        "standings": standings,
        "sim_data": sim_data,
        "flight_results": flight_results,
        "flight_team_cards": flight_team_cards,
        "playoff_games": playoff_games,
        "is_top_flight": is_top_flight,
        "is_bottom_flight": is_bottom_flight,
    }


def _find_flight_context(flight_slug_val, rows):
    flights = {
        (r["age_group"], r["division"], r["geography"])
        for r in rows
        if r["age_group"] and r["division"] and r["geography"]
    }
    for age_group, division, geography in flights:
        if flight_slug(age_group, division, geography) != flight_slug_val:
            continue
        context = get_flight_page_context(age_group, division, geography, rows=rows)
        if context:
            return age_group, division, geography, context
    return None


def _resolve_flight_page(flight_slug_val, rows=None, season=None):
    if rows is None:
        rows = get_current_season_rows()
    if season is None:
        season = CURRENT_SEASON
    found = _find_flight_context(flight_slug_val, rows)
    if found:
        _age_group, _division, _geography, context = found
        home_path = "../../" if season == CURRENT_SEASON else "../../../../"
        ai_text = _ai_flight_texts.get(flight_slug_val, "") if season == CURRENT_SEASON else ""
        ai_team_summaries = []
        if season == CURRENT_SEASON:
            needs_fallback_context = (not ai_text) or any(not _ai_team_texts.get(row["slug"], "") for row in context["standings"])
            flight_outlook = None
            importance = None
            if needs_fallback_context:
                flight_outlook = simulate_flight_outlook(context["sim_data"], total_runs=SIMULATION_RUNS)
                importance = get_flight_importance_report(flight_slug_val, context["sim_data"], baseline_outlook=flight_outlook)
            if not ai_text:
                ai_text = build_flight_importance_fallback(context["label"], context["standings"], context["sim_data"], importance)
            for row in context["standings"]:
                text = _ai_team_texts.get(row["slug"], "")
                if not text:
                    if flight_outlook is None:
                        flight_outlook = simulate_flight_outlook(context["sim_data"], total_runs=SIMULATION_RUNS)
                    if importance is None:
                        importance = get_flight_importance_report(flight_slug_val, context["sim_data"], baseline_outlook=flight_outlook)
                    text = build_team_insight_fallback(
                        row["team"],
                        context["standings"],
                        flight_outlook["team_stats"].get(row["team"], {}),
                        importance.get("top_games_by_team", {}).get(row["team"]),
                        context["flight_team_cards"].get(row["team"], {}).get("played", []),
                    )
                ai_team_summaries.append({
                    "team": row["team"],
                    "slug": row["slug"],
                    "text": text,
                })
        return render_template("flight.html", season=season,
                                is_historical=(season != CURRENT_SEASON),
                               home_path=home_path, ai_text=ai_text,
                               ai_team_summaries=ai_team_summaries, **context)
    return None


def _render_flight_importance_page(flight_slug_val, rows=None, season=None):
    if rows is None:
        rows = get_current_season_rows()
    if season is None:
        season = CURRENT_SEASON
    found = _find_flight_context(flight_slug_val, rows)
    if not found:
        return None
    age_group, division, geography, context = found
    sim_data = context["sim_data"]
    baseline_outlook = simulate_flight_outlook(sim_data, total_runs=SIMULATION_RUNS)
    importance = get_flight_importance_report(flight_slug_val, sim_data, baseline_outlook=baseline_outlook)
    return render_template(
        "flight_importance.html",
        season=season,
        label=context["label"],
        flight_slug=flight_slug_val,
        age_group=age_group,
        division=division,
        geography=geography,
        standings=context["standings"],
        home_path="../../../",
        importance=importance,
        top_game=importance.get("games", [None])[0] if importance.get("games") else None,
        is_historical=(season != CURRENT_SEASON),
    )


@app.route("/flight/<flight_slug_val>/")
def flight_page(flight_slug_val):
    result = _resolve_flight_page(flight_slug_val)
    if result:
        return result
    abort(404)


@app.route("/flight/<flight_slug_val>/importance/")
def flight_importance_page(flight_slug_val):
    result = _render_flight_importance_page(flight_slug_val, season=CURRENT_SEASON)
    if result:
        return result
    abort(404)


@app.route("/season/<season_slug>/flight/<flight_slug_val>/")
def flight_page_historical(season_slug, flight_slug_val):
    season = slug_to_season(season_slug)
    all_seasons = get_all_seasons()
    if season not in all_seasons:
        abort(404)
    rows = get_rows_for_season(season)
    if not rows:
        abort(404)
    result = _resolve_flight_page(flight_slug_val, rows=rows, season=season)
    if result:
        return result
    abort(404)


_SEASON_CAL_CACHE = os.path.join(DATA_DIR, "season_outlook_cal.json")

def load_season_outlook_calibration():
    if os.path.exists(_SEASON_CAL_CACHE):
        with open(_SEASON_CAL_CACHE) as f:
            return json.load(f)
    result = get_season_outlook_calibration()
    with open(_SEASON_CAL_CACHE, "w") as f:
        json.dump(result, f)
    return result


load_ai_caches_into_memory()


@app.route("/calibration/")
def calibration_page():
    return render_template(
        "calibration.html",
        cal=get_calibration_data(),
        season_cal=load_season_outlook_calibration(),
        home_path="../",
        regression_max=int(REGRESSION_MAX * 100),
        regression_gp_full=REGRESSION_GP_FULL,
        simulation_runs=SIMULATION_RUNS,
    )


def _render_teams(season, home_path, season_nav_prefix, flight_url_prefix):
    all_seasons = get_all_seasons()
    is_current = (season == CURRENT_SEASON)
    rows = get_rows_for_season(season)
    flight_groups = get_flight_catalog_grouped(rows)
    flight_previews = get_flight_preview_map(rows)

    seen = {}
    for r in rows:
        for raw in (r["home_team"], r["away_team"]):
            if not is_real_team_name(raw):
                continue
            team = clean_team_name(raw)
            key = (r["age_group"], r["division"], r["geography"], team)
            if key in seen:
                continue
            seen[key] = {
                "name": team,
                "age_group": r["age_group"],
                "division": r["division"],
                "geography": r["geography"],
                "flight_label": f"{r['age_group']} · Div {r['division']} · {r['geography']}",
                "flight_slug": flight_slug(r["age_group"], r["division"], r["geography"]),
            }
    search_data = sorted(seen.values(), key=lambda x: (x["age_group"], x["division"], x["geography"], x["name"]))

    seasons_for_select = [{"name": s, "slug": season_to_slug(s)} for s in reversed(all_seasons)]

    return render_template(
        "teams.html",
        flight_groups=flight_groups,
        search_data=search_data,
        season=season,
        season_slug=season_to_slug(season),
        current_season_slug=season_to_slug(CURRENT_SEASON),
        all_seasons=seasons_for_select,
        home_path=home_path,
        calibration_path=home_path + "calibration/",
        flight_url_prefix=flight_url_prefix,
        flight_previews=flight_previews,
        season_nav_prefix=season_nav_prefix,
        is_current_season=is_current,
    )


@app.route("/teams/")
def teams_page():
    return _render_teams(
        season=CURRENT_SEASON,
        home_path="../",
        season_nav_prefix="../season/",
        flight_url_prefix="../flight/",
    )


@app.route("/season/<season_slug>/teams/")
def teams_page_historical(season_slug):
    all_seasons = get_all_seasons()
    season = slug_to_season(season_slug)
    if season not in all_seasons:
        season = CURRENT_SEASON
    return _render_teams(
        season=season,
        home_path="../../",
        season_nav_prefix="../../season/",
        flight_url_prefix="../flight/",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
