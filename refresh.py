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

    logging.info("=== Refresh complete ===\n")


if __name__ == "__main__":
    main()
