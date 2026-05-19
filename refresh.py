"""
Nightly refresh: download current season data, recalculate ELO.

Run manually or via Windows Task Scheduler:
  python refresh.py

To schedule nightly at midnight via Task Scheduler, run once as admin:
  schtasks /create /tn "OTHSL Refresh" /tr "python C:\\Users\\Owner\\Desktop\\othsl\\refresh.py" /sc daily /st 00:00 /ru SYSTEM
"""

import os
import subprocess
import sys
import logging
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(__file__), "data", "refresh.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def run(script, *args, cwd=None):
    cmd = [sys.executable, script] + list(args)
    logging.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or os.path.dirname(__file__))
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logging.info(f"  {line}")
    if result.returncode != 0:
        logging.error(f"  FAILED (exit {result.returncode})")
        if result.stderr:
            logging.error(result.stderr[-500:])
        return False
    return True


def main():
    logging.info(f"=== Refresh started ===")

    base = os.path.dirname(__file__)
    season_cal_cache = os.path.join(base, "data", "season_outlook_cal.json")

    # 1. Download current season
    ok = run(os.path.join(base, "download_current", "download.py"))
    if not ok:
        logging.error("Download failed — aborting refresh.")
        return

    # 2. Recalculate ELO
    ok = run(os.path.join(base, "elo.py"))
    if not ok:
        logging.error("ELO calculation failed.")
        return

    # 3. Regenerate AI caches with updated data
    logging.info("Regenerating AI caches...")
    try:
        sys.path.insert(0, base)
        from app import build_ai_caches
        build_ai_caches()
        logging.info("AI caches regenerated successfully.")
    except Exception as e:
        logging.error(f"Failed to regenerate AI caches: {e}")

    # 4. Refresh calibration cache when schema or historical aggregation changes
    if os.path.exists(season_cal_cache):
        try:
            os.remove(season_cal_cache)
            logging.info("Removed cached season outlook calibration data.")
        except OSError as e:
            logging.warning(f"Could not remove season outlook calibration cache: {e}")

    # 5. Rebuild frozen site so docs/ reflects the latest data and copy
    ok = run(os.path.join(base, "freeze.py"))
    if not ok:
        logging.error("Static site build failed.")
        return

    logging.info("=== Refresh complete ===\n")


if __name__ == "__main__":
    main()
