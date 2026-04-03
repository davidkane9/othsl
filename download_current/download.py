"""
Download game results for the current season (Spring 2026).

This script:
  1. Fetches one page of the current season to discover all active divisions.
  2. Downloads each division's standings and results.
  3. Saves per-division CSVs to data/raw/ and rebuilds data/current_results.csv.

Usage:
  python download.py                 # full download
  python download.py --lnd v2s       # one specific division (e.g. Irish Village)
  python download.py --resume        # skip already-downloaded files

Output:
  data/raw/{season_code}_{lnd}.csv   one file per division
  data/current_results.csv           all current-season games combined

Run this script periodically (e.g. nightly) to keep results up to date.
It is safe to run multiple times — use --resume to avoid re-fetching unchanged data.
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scraper

BASE_URL = "https://www.othsl.org/cgi-bin/socman.pl"

# Import season metadata from download_history
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "download_history"))
from seasons import CURRENT_SEASON, ALL_DIVISIONS, IRISH_VILLAGE_LND

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
CURRENT_FILE = os.path.join(DATA_DIR, "current_results.csv")

RESULT_FIELDS = [
    "season", "age_group", "division", "geography",
    "date", "home_team", "home_goals", "away_goals", "away_team", "notes",
]

INTER_REQUEST_DELAY = 8


def raw_path(lnd):
    return os.path.join(RAW_DIR, f"{CURRENT_SEASON}_{lnd}.csv")


def save_division(lnd, rows):
    os.makedirs(RAW_DIR, exist_ok=True)
    path = raw_path(lnd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def discover_active_divisions(driver):
    """
    Fetch one page and parse all division links for the current season.
    Falls back to ALL_DIVISIONS if discovery fails.
    """
    url = f"{BASE_URL}?DATADIR={CURRENT_SEASON}&LDN={IRISH_VILLAGE_LND}"
    print(f"Discovering divisions from {url}")
    html = scraper.fetch_page(driver, url)
    links = scraper.parse_division_links(html, season_code=CURRENT_SEASON)
    if links:
        found = [lnd for _, lnd in links]
        print(f"Found {len(found)} divisions in navigation.")
        return found
    print("Could not discover divisions from nav; using known division list.")
    return ALL_DIVISIONS


def combine_current(divisions):
    """Combine per-division raw CSVs into data/current_results.csv."""
    all_rows = []
    for lnd in divisions:
        path = raw_path(lnd)
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                all_rows.extend(csv.DictReader(f))

    with open(CURRENT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    sn = scraper.season_name(CURRENT_SEASON)
    print(f"\nSaved {len(all_rows)} games for {sn} -> {CURRENT_FILE}")
    return all_rows


def main():
    parser = argparse.ArgumentParser(description="Download current OTHSL season data")
    parser.add_argument("--lnd", help="Download only this division code (e.g. v2s)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip divisions already saved in data/raw/")
    args = parser.parse_args()

    sn = scraper.season_name(CURRENT_SEASON)
    print(f"Downloading {sn} ({CURRENT_SEASON})")
    print("Starting browser...")
    driver = scraper.make_driver()

    try:
        if args.lnd:
            divisions = [args.lnd]
        else:
            divisions = discover_active_divisions(driver)

        total_games = 0

        for lnd in divisions:
            age_group, division, geography = scraper.parse_lnd(lnd)
            print(f"\n{age_group} Div {division} {geography} ({lnd})")

            if args.resume and os.path.exists(raw_path(lnd)):
                print("  SKIP (already downloaded)")
                continue

            url = f"{BASE_URL}?DATADIR={CURRENT_SEASON}&LDN={lnd}"
            print(f"  Fetching {url}")
            html = scraper.fetch_page(driver, url)
            rows = scraper.parse_division_page(html, CURRENT_SEASON, lnd)

            if rows is None:
                print("  Cloudflare block — waiting 30s and retrying...")
                time.sleep(30)
                html = scraper.fetch_page(driver, url)
                rows = scraper.parse_division_page(html, CURRENT_SEASON, lnd)

            if rows is None:
                print("  Still blocked, skipping.")
                continue

            if rows:
                path = save_division(lnd, rows)
                print(f"  Saved {len(rows)} games -> {os.path.basename(path)}")
                total_games += len(rows)
            else:
                print("  No games found.")

            if lnd != divisions[-1]:
                time.sleep(INTER_REQUEST_DELAY)

    finally:
        driver.quit()

    print(f"\nTotal: {total_games} games downloaded.")
    all_rows = combine_current(divisions)

    # Print Irish Village summary
    iv_rows = [r for r in all_rows if "Irish Village" in (r.get("home_team", "") + r.get("away_team", ""))]
    if iv_rows:
        print(f"\nIrish Village games this season: {len(iv_rows)}")
        for r in iv_rows:
            print(f"  {r['date']}  {r['home_team']} {r['home_goals']} - {r['away_goals']} {r['away_team']}")


if __name__ == "__main__":
    main()
