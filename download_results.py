"""
Download OTHSL league standings and game results, save as CSV.
"""
import csv
import re
import time
import undetected_chromedriver as uc
from bs4 import BeautifulSoup

URL = "https://www.othsl.org/cgi-bin/socman.pl?DATADIR=25f&LDN=v2s"
STANDINGS_FILE = "standings.csv"
RESULTS_FILE = "results.csv"


def fetch_html(url):
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1280,800")
    driver = uc.Chrome(options=options, headless=False, version_main=146)
    try:
        print("Opening browser, waiting for Cloudflare to clear...")
        driver.get(url)
        for i in range(15):
            time.sleep(3)
            title = driver.title
            print(f"  [{(i+1)*3}s] title: {title}")
            if "just a moment" not in title.lower():
                break
        html = driver.page_source
    finally:
        driver.quit()
    return html


def clean_header(raw):
    """'GP|Games Played' or 'GPGames Played' → 'GP (Games Played)'"""
    raw = raw.replace("|", " ")
    # Insert space before a capital letter that follows a lowercase letter (CamelCase join)
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    return raw.strip()


def parse_game_cell(cell_text):
    """
    Parse a game result cell such as:
      'Medway MOB 3  --  0 Ashland'
      'Ashland 2  -- forfeit F.C. Westwood'
    Returns dict with home_team, home_score, away_score, away_team, notes.
    """
    text = re.sub(r"\s+", " ", cell_text).strip().rstrip(" #")

    # Forfeit pattern: "Team 2 -- forfeit Other Team"
    forfeit_match = re.search(
        r"^(.+?)\s+(\d+)\s+--\s+(forfeit(?:NP)?)\s+(.+)$", text, re.IGNORECASE
    )
    if forfeit_match:
        return {
            "home_team": forfeit_match.group(1).strip(),
            "home_score": forfeit_match.group(2),
            "away_score": forfeit_match.group(3),
            "away_team": forfeit_match.group(4).strip(),
            "notes": "forfeit",
        }

    # Reverse forfeit: "Team -- forfeit ... Other Team" (team being forfeited to)
    rev_forfeit = re.search(
        r"^(.+?)\s+(forfeit(?:NP)?)\s+lost by forfeit.*--\s*(forfeit(?:NP)?)\s+lost by forfeit.*\s+(.+)$",
        text, re.IGNORECASE,
    )
    if rev_forfeit:
        return {
            "home_team": rev_forfeit.group(1).strip(),
            "home_score": "forfeit",
            "away_score": "forfeit",
            "away_team": rev_forfeit.group(4).strip(),
            "notes": "double forfeit",
        }

    # Score with shootout: "Team 0 (3) -- 0 (4) Other Team"
    shootout = re.search(
        r"^(.+?)\s+(\d+)\s*\(\d+\)\s+--\s+(\d+)\s*\(\d+\)\s+(.+)$", text
    )
    if shootout:
        return {
            "home_team": shootout.group(1).strip(),
            "home_score": shootout.group(2),
            "away_score": shootout.group(3),
            "away_team": shootout.group(4).strip(),
            "notes": "shootout",
        }

    # Normal score: "Team 3 -- 0 Other Team"
    score_match = re.search(r"^(.+?)\s+(\d+)\s+--\s+(\d+)\s+(.+)$", text)
    if score_match:
        return {
            "home_team": score_match.group(1).strip(),
            "home_score": score_match.group(2),
            "away_score": score_match.group(3),
            "away_team": score_match.group(4).strip(),
            "notes": "",
        }

    return None


def parse_and_save(html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text() if soup.title else ""
    if "just a moment" in title.lower():
        print("ERROR: Still on Cloudflare challenge page.")
        return

    tables = soup.find_all("table")

    # ── Standings (Table 3) ──────────────────────────────────────────────────
    standings_table = tables[3]
    standings_rows = standings_table.find_all("tr")
    headers = [clean_header(th.get_text(separator=" ", strip=True))
               for th in standings_rows[0].find_all(["th", "td"])]

    standings_data = []
    for tr in standings_rows[1:]:
        row = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
        if row:
            standings_data.append(row)

    with open(STANDINGS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(standings_data)
    print(f"Standings: {len(standings_data)} teams saved to '{STANDINGS_FILE}'")
    print(f"  Headers: {headers}")

    # ── Schedule / Results (Table 4) ─────────────────────────────────────────
    schedule_table = tables[4]
    schedule_rows = schedule_table.find_all("tr")

    result_rows = []
    for tr in schedule_rows[1:]:  # skip header row
        cells = tr.find_all("td")
        if not cells:
            continue
        date_text = cells[0].get_text(strip=True)
        game_cells = cells[1:]
        for cell in game_cells:
            raw = cell.get_text(separator=" ", strip=True)
            # skip footnote rows
            if "indicates that this result" in raw:
                continue
            parsed = parse_game_cell(raw)
            if parsed:
                result_rows.append({
                    "date": date_text,
                    **parsed,
                })

    result_headers = ["date", "home_team", "home_score", "away_score", "away_team", "notes"]
    with open(RESULTS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=result_headers)
        writer.writeheader()
        writer.writerows(result_rows)
    print(f"Results:  {len(result_rows)} games saved to '{RESULTS_FILE}'")
    if result_rows:
        print(f"  Sample: {result_rows[0]}")


def main():
    html = fetch_html(URL)
    parse_and_save(html)


main()
