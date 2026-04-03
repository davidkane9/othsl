"""
Download game results for all historical seasons and all divisions.

Usage:
  python scrape_all.py                     # scrape all seasons
  python scrape_all.py --season 25f        # scrape one season
  python scrape_all.py --resume            # skip already-downloaded files

Output:
  data/raw/{season_code}_{lnd}.csv         one file per season+division
  data/all_results.csv                     combined file (all seasons)

The script keeps one browser session open across all requests to avoid
repeated Cloudflare challenges. It sleeps between requests to be polite.
"""

import argparse
import csv
import os
import sys
import time

# Allow importing scraper.py from the parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper
from seasons import ALL_SEASONS, ALL_DIVISIONS, CURRENT_SEASON

BASE_URL = "https://www.othsl.org/cgi-bin/socman.pl"
RAW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw")
ALL_RESULTS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "all_results.csv")

RESULT_FIELDS = [
    "season", "age_group", "division", "geography",
    "date", "home_team", "home_goals", "away_goals", "away_team", "notes",
]

# Seconds to wait between division page requests (be polite to the server)
INTER_REQUEST_DELAY = 2


def raw_path(season_code, lnd):
    return os.path.join(RAW_DIR, f"{season_code}_{lnd}.csv")


def already_downloaded(season_code, lnd):
    return os.path.exists(raw_path(season_code, lnd))


def save_division(season_code, lnd, rows):
    """Save rows for one division to its raw CSV file."""
    os.makedirs(RAW_DIR, exist_ok=True)
    path = raw_path(season_code, lnd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def scrape_division(driver, season_code, lnd, resume=False):
    """
    Fetch and parse one division page. Returns list of game rows.
    Returns [] on empty page, None on Cloudflare block.
    """
    if resume and already_downloaded(season_code, lnd):
        print(f"  SKIP (already downloaded)")
        return "skipped"

    url = f"{BASE_URL}?DATADIR={season_code}&LDN={lnd}"
    print(f"  Fetching {url}")
    html = scraper.fetch_page(driver, url)
    rows = scraper.parse_division_page(html, season_code, lnd)
    return rows


def combine_all():
    """Merge all raw CSVs into data/all_results.csv."""
    all_rows = []
    for fname in sorted(os.listdir(RAW_DIR)):
        if not fname.endswith(".csv"):
            continue
        with open(os.path.join(RAW_DIR, fname), newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            all_rows.extend(reader)

    with open(ALL_RESULTS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nCombined {len(all_rows)} rows -> {ALL_RESULTS_FILE}")
    return len(all_rows)


def main():
    parser = argparse.ArgumentParser(description="Scrape OTHSL historical data")
    parser.add_argument("--season", help="Scrape only this season code (e.g. 25f)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip divisions already saved in data/raw/")
    parser.add_argument("--combine-only", action="store_true",
                        help="Skip scraping, just rebuild data/all_results.csv")
    args = parser.parse_args()

    if args.combine_only:
        combine_all()
        return

    seasons = [args.season] if args.season else ALL_SEASONS
    # Exclude current season from historical scrape (handled by download_current/)
    seasons = [s for s in seasons if s != CURRENT_SEASON]

    total_games = 0
    total_divs = 0
    skipped = 0

    print(f"Scraping {len(seasons)} season(s) x {len(ALL_DIVISIONS)} division(s)")
    print("Starting browser...")
    driver = scraper.make_driver()

    # Seed URL: any page for a known division that existed in early seasons
    SEED_LND = "o1n"

    try:
        for season_code in seasons:
            sn = scraper.season_name(season_code)
            print(f"\n=== {sn} ({season_code}) ===")

            # Fetch one page to discover which divisions the nav lists for this season.
            # This avoids checking ~30 division codes when only ~10-15 actually exist.
            seed_url = f"{BASE_URL}?DATADIR={season_code}&LDN={SEED_LND}"
            print(f"  Discovering divisions from nav...")
            seed_html = scraper.fetch_page(driver, seed_url)
            nav_links = scraper.parse_division_links(seed_html, season_code=season_code)
            if nav_links:
                divisions_this_season = [lnd for _, lnd in nav_links
                                         if scraper.parse_lnd(lnd) is not None]
                print(f"  {len(divisions_this_season)} divisions found in nav.")
            else:
                divisions_this_season = ALL_DIVISIONS
                print(f"  Nav parse failed; trying all {len(ALL_DIVISIONS)} divisions.")
            time.sleep(INTER_REQUEST_DELAY)

            for lnd in divisions_this_season:
                age_group, division, geography = scraper.parse_lnd(lnd)
                print(f"  {age_group} Div {division} {geography} ({lnd})")

                rows = scrape_division(driver, season_code, lnd, resume=args.resume)

                if rows == "skipped":
                    skipped += 1
                    continue
                if rows is None:
                    print("  Cloudflare block — waiting 30s before retry...")
                    time.sleep(30)
                    rows = scrape_division(driver, season_code, lnd)
                    if rows is None:
                        print("  Still blocked, skipping this division.")
                        continue

                if rows:
                    path = save_division(season_code, lnd, rows)
                    print(f"  Saved {len(rows)} games -> {os.path.basename(path)}")
                    total_games += len(rows)
                    total_divs += 1
                else:
                    print("  No games found (division may not exist this season).")

                time.sleep(INTER_REQUEST_DELAY)

    finally:
        driver.quit()

    print(f"\n{'='*60}")
    print(f"Done. {total_divs} divisions, {total_games} games, {skipped} skipped.")
    print("Building combined all_results.csv...")
    combine_all()


if __name__ == "__main__":
    main()
