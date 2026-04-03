"""
Shared scraping utilities for OTHSL data collection.

URL structure:
  https://www.othsl.org/cgi-bin/socman.pl?DATADIR={season_code}&LDN={division_code}

Season codes: '97s', '97f', '98s', ..., '25f', '26s'
Division codes: {prefix}{number}{geo}
  prefix: o=Over30, m=Over40, s=Over48, v=Over55, z=Over62, a=Over68
  number: 1-6
  geo: n=North, s=South, c=Central
"""

import re
import time

import undetected_chromedriver as uc
from bs4 import BeautifulSoup

BASE_URL = "https://www.othsl.org/cgi-bin/socman.pl"

AGE_GROUP_MAP = {
    "o": "Over 30",
    "m": "Over 40",
    "s": "Over 48",
    "v": "Over 55",
    "z": "Over 62",
    "a": "Over 68",
}

GEO_MAP = {
    "n": "North",
    "s": "South",
    "c": "Central",
}


def season_name(code):
    """'25f' -> 'Fall 2025', '26s' -> 'Spring 2026'"""
    year_num = int(code[:2])
    year = 2000 + year_num if year_num < 50 else 1900 + year_num
    half = "Fall" if code[2] == "f" else "Spring"
    return f"{half} {year}"


def season_year(code):
    """'25f' -> 2025"""
    year_num = int(code[:2])
    return 2000 + year_num if year_num < 50 else 1900 + year_num


def parse_lnd(lnd):
    """'v2s' -> ('Over 55', 2, 'South'). Returns None if code is unrecognized."""
    try:
        prefix = lnd[0]
        number = int(lnd[1:-1])
        geo = lnd[-1]
        if prefix not in AGE_GROUP_MAP or geo not in GEO_MAP:
            return None
        return AGE_GROUP_MAP[prefix], number, GEO_MAP[geo]
    except (ValueError, IndexError):
        return None


def normalize_date(date_str, season_code):
    """
    Convert '9/7' to '2025-09-07' given season code '25f'.
    Strips playoff labels like 'semi', 'final'. Returns 'TBD' unchanged.
    """
    date_str = date_str.strip()
    if date_str.upper() == "TBD":
        return "TBD"
    # Take just the first token (handles '11/16 semi', '11/23 final')
    date_part = date_str.split()[0]
    try:
        parts = date_part.split("/")
        month = int(parts[0])
        day = int(parts[1])
        year = season_year(season_code)
        return f"{year}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return date_str


def make_driver():
    """Create a reusable undetected Chrome driver."""
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1280,800")
    return uc.Chrome(options=options, headless=False, version_main=146)


def fetch_page(driver, url, initial_wait=2, max_attempts=20):
    """
    Navigate to URL, wait for Cloudflare to clear, return HTML.
    Reuses an existing driver session so subsequent pages load faster.
    initial_wait is short because Cloudflare is already cleared after the first page.
    """
    driver.get(url)
    for i in range(max_attempts):
        time.sleep(initial_wait if i == 0 else 2)
        title = driver.title
        if "just a moment" not in title.lower():
            break
    return driver.page_source


def parse_division_links(html, season_code=None):
    """
    Extract all (datadir, lnd) division links from a page's navigation.
    If season_code is given, only returns links for that season.
    Returns list of (season_code, lnd) tuples, deduplicated.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"DATADIR=(\w+)&(?:amp;)?LDN=([a-z]\d+[a-z])", a["href"])
        if m:
            dc, lnd = m.group(1), m.group(2)
            if season_code is None or dc == season_code:
                seen[(dc, lnd)] = True
    return list(seen.keys())


def parse_game_cell(cell_text):
    """
    Parse one game result cell, e.g.:
      'Irish Village 4  --  0 F.C. Westwood'
      'Ashland 2  -- forfeitNP lost by forfeit ... F.C. Westwood'
      'Medway MOB 0 (3)  --  0 (4) Weston'

    Returns dict with home_team, home_goals, away_goals, away_team, notes
    or None if unparseable.
    """
    text = re.sub(r"\s+", " ", cell_text).strip().rstrip(" #")

    # Double forfeit: team -- forfeitNP ... -- forfeitNP ... team
    double = re.search(
        r"^(.+?)\s+forfeit\S*\s+lost by forfeit.*--\s*forfeit\S*\s+lost by forfeit.*\s+(.+)$",
        text,
        re.IGNORECASE,
    )
    if double:
        return {
            "home_team": double.group(1).strip(),
            "home_goals": "forfeit",
            "away_goals": "forfeit",
            "away_team": double.group(2).strip(),
            "notes": "double forfeit",
        }

    # Forfeit: "Team 2 -- forfeit[NP] lost by forfeit Other Team"
    forfeit = re.search(
        r"^(.+?)\s+(\d+)\s+--\s+(forfeit\S*)\s+lost by forfeit.*?\s{2,}(.+)$",
        text,
        re.IGNORECASE,
    )
    if not forfeit:
        # Simpler forfeit: "Team 2 -- forfeit Other Team"
        forfeit = re.search(
            r"^(.+?)\s+(\d+)\s+--\s+(forfeit\S*)\s+(.+)$",
            text,
            re.IGNORECASE,
        )
    if forfeit:
        return {
            "home_team": forfeit.group(1).strip(),
            "home_goals": forfeit.group(2),
            "away_goals": forfeit.group(3),
            "away_team": forfeit.group(4).strip(),
            "notes": "forfeit",
        }

    # Shootout: "Team 0 (3) -- 0 (4) Other Team"
    shootout = re.search(
        r"^(.+?)\s+(\d+)\s*\(\d+\)\s+--\s+(\d+)\s*\(\d+\)\s+(.+)$", text
    )
    if shootout:
        return {
            "home_team": shootout.group(1).strip(),
            "home_goals": shootout.group(2),
            "away_goals": shootout.group(3),
            "away_team": shootout.group(4).strip(),
            "notes": "shootout",
        }

    # Normal: "Team 3 -- 0 Other Team"
    normal = re.search(r"^(.+?)\s+(\d+)\s+--\s+(\d+)\s+(.+)$", text)
    if normal:
        return {
            "home_team": normal.group(1).strip(),
            "home_goals": normal.group(2),
            "away_goals": normal.group(3),
            "away_team": normal.group(4).strip(),
            "notes": "",
        }

    return None


def parse_division_page(html, season_code, lnd):
    """
    Parse a division page, return list of game result dicts.

    Each dict has:
      season, age_group, division, geography, date,
      home_team, home_goals, away_goals, away_team, notes

    Returns None if still on Cloudflare page.
    Returns [] if page has no schedule data.
    """
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text() if soup.title else ""
    if "just a moment" in title.lower():
        print("  ERROR: Still on Cloudflare page.")
        return None

    parsed = parse_lnd(lnd)
    if parsed is None:
        print(f"  Skipping unrecognized division code: {lnd}")
        return []
    age_group, division, geography = parsed
    sn = season_name(season_code)

    tables = soup.find_all("table")
    if len(tables) < 5:
        print(f"  Only {len(tables)} tables found, skipping.")
        return []

    schedule_table = tables[4]
    schedule_rows = schedule_table.find_all("tr")

    results = []
    for tr in schedule_rows[1:]:
        cells = tr.find_all("td")
        if not cells:
            continue
        date_text = cells[0].get_text(strip=True)
        for cell in cells[1:]:
            raw = cell.get_text(separator=" ", strip=True)
            if "indicates that this result" in raw:
                continue
            parsed = parse_game_cell(raw)
            if parsed:
                results.append(
                    {
                        "season": sn,
                        "age_group": age_group,
                        "division": division,
                        "geography": geography,
                        "date": normalize_date(date_text, season_code),
                        "home_team": parsed["home_team"],
                        "home_goals": parsed["home_goals"],
                        "away_goals": parsed["away_goals"],
                        "away_team": parsed["away_team"],
                        "notes": parsed["notes"],
                    }
                )

    return results
