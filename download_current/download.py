"""Download Victory fixtures and official standings: python download_current/download.py."""
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from season_config import CURRENT_SEASON, CURRENT_SEASON_ID, LEAGUE_ID, SCHEDULE_URL
from victory import RESULT_FIELDS, fetch_collection, normalize_games
DATA_DIR = BASE / "data"


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def csv_text(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def main():
    print(f"Downloading {CURRENT_SEASON} from {SCHEDULE_URL}", flush=True)
    params = {"season_id": CURRENT_SEASON_ID, "league_id": LEAGUE_ID}
    standings = fetch_collection("standings", **params)
    games = fetch_collection("games", fa=1, **params)
    rows = normalize_games(games, standings)
    current = DATA_DIR / "current_results.csv"
    history = DATA_DIR / "all_results.csv"
    if current.exists():
        with current.open(newline="", encoding="utf-8") as file:
            previous = list(csv.DictReader(file))
        older = [r for r in previous if r["season"] != CURRENT_SEASON]
        if older:
            with history.open(newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                fields = reader.fieldnames
                archived = list(reader)
            replacing = {r["season"] for r in older}
            archived = [r for r in archived if r["season"] not in replacing] + older
            atomic_write(history, csv_text(archived, fields))
    snapshot = {"season": CURRENT_SEASON, "season_id": CURRENT_SEASON_ID,
                "source": SCHEDULE_URL, "row_count": len(rows),
                "game_ids": sorted(r["game_id"] for r in rows),
                "updated_at": datetime.now(timezone.utc).isoformat(), "standings": standings}
    atomic_write(current, csv_text(rows, RESULT_FIELDS))
    atomic_write(DATA_DIR / "current_standings.json", json.dumps(snapshot, indent=2) + "\n")
    print(f"Saved {len(rows)} fixtures and {len(standings)} official standings entries.")


if __name__ == "__main__":
    main()
