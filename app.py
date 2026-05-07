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

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CURRENT_SEASON = "Spring 2026"
DEFAULT_ELO = 1500
SIMULATION_RUNS = 400


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
                stats[ht]["w"] += 1
                stats[ht]["pts"] += 3
                stats[ht]["gp"] += 1
                stats[at]["l"] += 1
                stats[at]["gp"] += 1
            elif is_forfeit(hg) and not is_forfeit(ag):
                stats[at]["w"] += 1
                stats[at]["pts"] += 3
                stats[at]["gp"] += 1
                stats[ht]["l"] += 1
                stats[ht]["gp"] += 1
            else:
                stats[ht]["l"] += 1
                stats[ht]["gp"] += 1
                stats[at]["l"] += 1
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


def get_team_catalog():
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
    flight_rows = [
        r for r in rows
        if (
            r["age_group"] == team_info["age_group"]
            and r["division"] == team_info["division"]
            and r["geography"] == team_info["geography"]
        )
    ]
    future_games = [
        r for r in flight_rows
        if not has_played_score(r) and not is_forfeit(r["home_goals"]) and not is_forfeit(r["away_goals"])
        and is_real_team_name(r["home_team"]) and is_real_team_name(r["away_team"])
    ]

    teams = [row["team"] for row in standings]
    current_stats = {
        row["team"]: {"pts": row["pts"], "gd": row["gd"], "gf": row["gf"], "gp": row["gp"], "w": row["w"], "l": row["l"], "t": row["t"]}
        for row in standings
    }
    position_counts = {place: 0 for place in range(1, len(teams) + 1)}
    latest_elos = get_latest_elo_map()
    promotion_cut = 2
    n_teams = len(teams)
    relegation_cut = 2 if n_teams >= 6 else (1 if n_teams >= 4 else 0)

    if not future_games:
        current_place = next((i + 1 for i, row in enumerate(standings) if row["is_selected"]), None)
        if current_place:
            position_counts[current_place] = SIMULATION_RUNS
        return {
            "future_game_count": 0,
            "place_probabilities": [
                {"place": place, "probability": round(100 * count / SIMULATION_RUNS, 1)}
                for place, count in position_counts.items()
                if count
            ],
            "promotion_probability": 100.0 if current_place and current_place <= promotion_cut else 0.0,
            "relegation_probability": 100.0 if current_place and current_place > len(teams) - relegation_cut else 0.0,
            "stay_probability": 100.0 if current_place and promotion_cut < current_place <= len(teams) - relegation_cut else 0.0,
            "summary": "No remaining scheduled games are in the dataset, so the current table is treated as final.",
        }

    for _ in range(SIMULATION_RUNS):
        sim_stats = {team: dict(stats) for team, stats in current_stats.items()}
        for game in future_games:
            home = clean_team_name(game["home_team"])
            away = clean_team_name(game["away_team"])
            if home not in sim_stats or away not in sim_stats:
                continue
            home_elo = latest_elos.get((home, team_info["age_group"]), DEFAULT_ELO)
            away_elo = latest_elos.get((away, team_info["age_group"]), DEFAULT_ELO)
            win_expectation = expected_result(home_elo, away_elo)
            draw_prob = 0.22
            home_win_prob = max(0.05, min(0.9, win_expectation - draw_prob / 2))
            away_win_prob = max(0.05, 1.0 - draw_prob - home_win_prob)
            roll = random.random()

            if roll < home_win_prob:
                sim_stats[home]["pts"] += 3
                sim_stats[home]["gd"] += 1
                sim_stats[home]["gf"] += 2
                sim_stats[away]["gd"] -= 1
                sim_stats[away]["gf"] += 1
            elif roll < home_win_prob + draw_prob:
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

        ordered = sorted(
            teams,
            key=lambda name: (
                -sim_stats[name]["pts"],
                -sim_stats[name]["gd"],
                -sim_stats[name]["gf"],
                name,
            ),
        )
        if team_info["team"] not in ordered:
            continue
        final_place = ordered.index(team_info["team"]) + 1
        position_counts[final_place] += 1

    place_probabilities = [
        {"place": place, "probability": round(100 * position_counts[place] / SIMULATION_RUNS, 1)}
        for place in sorted(position_counts)
        if position_counts[place]
    ]
    promotion_probability = round(
        100 * sum(position_counts[p] for p in range(1, promotion_cut + 1)) / SIMULATION_RUNS, 1
    )
    relegation_probability = round(
        100 * sum(position_counts[p] for p in range(n_teams - relegation_cut + 1, n_teams + 1)) / SIMULATION_RUNS,
        1,
    ) if relegation_cut else 0.0
    stay_probability = round(
        100.0 - promotion_probability - relegation_probability,
        1,
    )

    return {
        "future_game_count": len(future_games),
        "place_probabilities": place_probabilities,
        "promotion_probability": promotion_probability,
        "relegation_probability": relegation_probability,
        "stay_probability": stay_probability,
        "summary": f"{SIMULATION_RUNS} simulations using current ELO ratings and the remaining scheduled games in this flight.",
    }


def get_flight_team_cards(team_info, standings, rows, playoff_visitors=None):
    """For each team in the flight return their slug + last 3 played games."""
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

    cards = {}
    for row in standings:
        team = row["team"]
        recent = []
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
            recent.append({"date": r["date"], "opponent": opp,
                           "venue": "H" if is_home else "A",
                           "score": score, "result": res})
            if len(recent) == 3:
                break

        cards[team] = {"slug": slug_map.get(team, ""), "recent": recent}

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

    remaining = []
    for r in flight_rows:
        if not is_real_team_name(r["home_team"]) or not is_real_team_name(r["away_team"]):
            continue
        if not has_played_score(r) and not is_forfeit(r["home_goals"]) and not is_forfeit(r["away_goals"]):
            sel = team_info.get("team")
            home = clean_team_name(r["home_team"])
            away = clean_team_name(r["away_team"])
            remaining.append({
                "id": f"{r['date']}|{home}|{away}",
                "home": home,
                "away": away,
                "date": r["date"],
                "involves_team": bool(sel and sel in (home, away)),
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
                # Assume double round-robin (2 meetings); add unplayed fixtures
                for _ in range(max(0, 2 - times_played)):
                    sel = team_info.get("team")
                    remaining.append({
                        "id": f"inferred|{home}|{away}",
                        "home": home,
                        "away": away,
                        "date": "TBD",
                        "involves_team": bool(sel and sel in (home, away)),
                    })
        if remaining:
            schedule_inferred = True

    latest_elos = get_latest_elo_map()
    current_elos = {
        team: latest_elos.get((team, age_group), DEFAULT_ELO)
        for team in current_stats
    }

    teams = [row["team"] for row in standings]
    n = len(teams)
    return {
        "selected_team": team_info.get("team", ""),
        "teams": teams,
        "current_stats": current_stats,
        "current_elos": current_elos,
        "remaining_games": sorted(remaining, key=lambda g: g["date"]),
        "promotion_cut": 2,
        "relegation_cut": 2 if n >= 6 else (1 if n >= 4 else 0),
        "total_teams": n,
        "schedule_inferred": schedule_inferred,
    }


# --- HISTORICAL TEAM LOOKUP (commented out) ---
# def build_team_name_slug(team_name): ...
# --- END ---


def get_team_page_context(team_slug):
    rows = get_current_season_rows()

    team_catalog = get_team_catalog()
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
    current_elo = elo_history[-1]["elo"] if elo_history else None
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
            {"value": "current", "label": "This season"},
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
    """Position distribution for Irish Village Over 30 (homepage plinko teaser)."""
    if rows is None:
        rows = get_current_season_rows()


    flight_rows = defaultdict(list)
    for r in rows:
        if r["age_group"] and r["division"] and r["geography"]:
            flight_rows[(r["age_group"], r["division"], r["geography"])].append(r)

    def _build(target_team, target_ag):
        for (ag, div, geo) in flight_rows:
            if ag != target_ag:
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
            }
        return None

    # Fixed featured team — consistent across refreshes
    return _build("Milton FC", "Over 40")


# ── AI-generated insight paragraphs ───────────────────────────────────────────

_AI_FLIGHT_CACHE = os.path.join(DATA_DIR, "ai_flight_outlooks.json")
_AI_TEAM_CACHE   = os.path.join(DATA_DIR, "ai_team_insights.json")


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
            max_tokens=120,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
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


def build_ai_caches(rows=None):
    """Generate and save all AI insight paragraphs for the current season."""
    if rows is None:
        rows = get_current_season_rows()

    flight_cache = {}
    team_cache   = {}

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

        # Pick any team for sim_data (just need remaining games + team list)
        team_info = {"team": standings[0]["team"], "age_group": ag, "division": div, "geography": geo}
        flight_sim = get_flight_sim_data(team_info, standings, rows)

        print(f"  Flight {sl}…", end=" ", flush=True)
        outlook = generate_ai_flight_outlook(ag, div, geo, standings, flight_sim)
        flight_cache[sl] = outlook
        print("done")

        for row in standings:
            t_info = {"team": row["team"], "age_group": ag, "division": div, "geography": geo}
            t_sim  = get_flight_sim_data(t_info, standings, rows)
            t_sim_py = simulate_team_outlook(t_info, standings, rows)
            t_sim["promotion_probability"] = t_sim_py.get("promotion_probability", 0)
            t_sim["relegation_probability"] = t_sim_py.get("relegation_probability", 0)
            slug = build_team_slug(row["team"], ag, div, geo)
            print(f"    Team {row['team']}…", end=" ", flush=True)
            insight = generate_ai_team_insight(row["team"], ag, standings, t_sim)
            team_cache[slug] = insight
            print("done")

    with open(_AI_FLIGHT_CACHE, "w") as f:
        json.dump(flight_cache, f)
    with open(_AI_TEAM_CACHE, "w") as f:
        json.dump(team_cache, f)
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
        for home, away in remaining:
            if home not in st or away not in st:
                continue
            we = expected_result(el.get(home, DEFAULT_ELO), el.get(away, DEFAULT_ELO))
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
        featured_plinko=get_featured_plinko(rows) if is_current else None,
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
    context = get_team_page_context(team_slug)
    if not context:
        abort(404)
    return render_template("team.html", season=CURRENT_SEASON,
                           ai_text=_ai_team_texts.get(team_slug, ""), **context)


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


def _resolve_flight_page(flight_slug_val, rows=None, season=None):
    if rows is None:
        rows = get_current_season_rows()
    if season is None:
        season = CURRENT_SEASON
    flights = {
        (r["age_group"], r["division"], r["geography"])
        for r in rows
        if r["age_group"] and r["division"] and r["geography"]
    }
    for age_group, division, geography in flights:
        if flight_slug(age_group, division, geography) == flight_slug_val:
            context = get_flight_page_context(age_group, division, geography, rows=rows)
            if context:
                home_path = "../../" if season == CURRENT_SEASON else "../../../../"
                ai_text = _ai_flight_texts.get(flight_slug_val, "") if season == CURRENT_SEASON else ""
                return render_template("flight.html", season=season,
                                       is_historical=(season != CURRENT_SEASON),
                                       home_path=home_path, ai_text=ai_text, **context)
    return None


@app.route("/flight/<flight_slug_val>/")
def flight_page(flight_slug_val):
    result = _resolve_flight_page(flight_slug_val)
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


@app.route("/calibration/")
def calibration_page():
    return render_template(
        "calibration.html",
        cal=get_calibration_data(),
        season_cal=load_season_outlook_calibration(),
        home_path="../",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
