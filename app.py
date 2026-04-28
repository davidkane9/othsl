"""
OTHSL web app for Spring 2026 league browsing and team pages.

Run:
  python app.py

Then open http://localhost:5000
"""

import csv
import os
import re
import random
from collections import defaultdict
from flask import Flask, abort, render_template, request

app = Flask(__name__)

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
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400))


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
        row["team"]: {"pts": row["pts"], "gd": row["gd"], "gf": row["gf"]}
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
        row["team"]: {"pts": row["pts"], "gd": row["gd"], "gf": row["gf"]}
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


def get_calibration_data():
    """Compute ELO calibration stats from full elo_history.csv."""
    from elo import expected as elo_expected
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
        exp = elo_expected(eh, ea)
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
    return render_template("team.html", season=CURRENT_SEASON, **context)


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
                return render_template("flight.html", season=season,
                                       is_historical=(season != CURRENT_SEASON), **context)
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


@app.route("/calibration/")
def calibration_page():
    return render_template(
        "calibration.html",
        cal=get_calibration_data(),
        home_path="../",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
