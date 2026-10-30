"""Public JSON feed used by victory.mass-soccer.org; no browser or login needed."""
import json
import re
import time
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from season_config import CURRENT_SEASON, CURRENT_SEASON_ID, LEAGUE_ID

API_ROOT = "https://reg.mass-soccer.org/api/"
RESULT_FIELDS = ["season", "age_group", "division", "geography", "date", "home_team",
                 "home_goals", "away_goals", "away_team", "notes", "game_id", "game_type"]


def fetch_json(url):
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "OTHSL-Match-Intelligence/2.0", "Accept": "application/json"})
            with urlopen(req, timeout=60) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def fetch_collection(endpoint, **params):
    url = API_ROOT + endpoint + "/?" + urlencode(params)
    rows, visited = [], set()
    while url:
        if url in visited or urlparse(url).netloc != urlparse(API_ROOT).netloc:
            raise ValueError("Invalid pagination link from schedule API")
        visited.add(url)
        payload = fetch_json(url)
        if isinstance(payload, list):
            rows.extend(payload)
            break
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError(f"Unexpected {endpoint} response")
        rows.extend(payload["results"])
        url = urljoin(url, payload["next"]) if payload.get("next") else None
    if not rows:
        raise ValueError(f"Empty {endpoint} response; keeping existing data")
    return rows


def flight_info(team):
    match = re.fullmatch(r"Division (\d+)", team["division_name"])
    if not match or not re.fullmatch(r"Over \d+", team["bracket_name"]):
        raise ValueError(f"Unknown flight: {team}")
    geography = team.get("flight")
    if geography not in ("North", "South", "Central"):
        raise ValueError(f"Unknown geography for {team['team_name']}: {geography}")
    return team["bracket_name"], match[1], geography


def normalize_games(games, standings):
    teams = {}
    for team in standings:
        if team["season_id"] != CURRENT_SEASON_ID or team["league_id"] != LEAGUE_ID:
            raise ValueError("Standings contains a different season or league")
        if (team.get("team_name") or "").upper() in ("TBD", "BYE", "TBD/BYE"):
            continue
        flight_info(team)
        teams[team["team_id"]] = team
    result, seen = [], set()
    for game in games:
        if game["season_id"] != CURRENT_SEASON_ID or game["league_id"] != LEAGUE_ID:
            raise ValueError("Schedule contains a different season or league")
        if game["season_name"] != CURRENT_SEASON:
            raise ValueError("Update season_config.py: source season name changed")
        # Cross-flight exhibitions, playoffs and byes do not enter league tables.
        if game["game_type"] != "R" or game["id"] in seen:
            continue
        seen.add(game["id"])
        home = teams.get(game["home_seasonlineup_id"])
        away = teams.get(game["away_seasonlineup_id"])
        if not home or not away:
            raise ValueError(f"Unmapped team in league fixture {game['id']}")
        hg, ag = game["home_team_score"], game["away_team_score"]
        notes = []
        if game.get("postponed") or game.get("abandoned"):
            hg = ag = None
            notes.append("postponed" if game.get("postponed") else "abandoned")
        if (hg is None) != (ag is None):
            raise ValueError(f"Partial score in fixture {game['id']}")
        if hg is not None and (type(hg) is not int or type(ag) is not int or min(hg, ag) < 0):
            raise ValueError(f"Invalid score in fixture {game['id']}")
        if game.get("forfeit"):
            # Preserve the awarded score; standings supply additional penalties.
            notes.append("forfeit")
        if hg is None:
            notes.append("scheduled")
        game_date = game.get("game_date") or "TBD"
        if game_date != "TBD":
            date.fromisoformat(game_date)
        age, division, geography = flight_info(home)
        result.append(dict(zip(RESULT_FIELDS, [CURRENT_SEASON, age, division, geography,
            game_date, home["team_name"].strip(), "" if hg is None else hg,
            "" if ag is None else ag, away["team_name"].strip(), "; ".join(notes),
            str(game["id"]), game["game_type"]])))
    if not result:
        raise ValueError("No league fixtures found; keeping existing data")
    return sorted(result, key=lambda r: (r["date"], r["age_group"], int(r["division"]), r["geography"], r["game_id"]))
