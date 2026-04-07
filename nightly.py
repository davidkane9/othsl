"""
Nightly job: refresh data, then push updated CSVs to GitHub so the
GitHub Actions workflow rebuilds and redeploys the public site.

Schedule via Windows Task Scheduler (run once as admin):
  schtasks /create /tn "OTHSL Nightly" /tr "python C:\\Users\\Owner\\Desktop\\othsl\\nightly.py" /sc daily /st 00:00 /ru SYSTEM /f
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

BASE = os.path.dirname(os.path.abspath(__file__))


def run(cmd, **kwargs):
    logging.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
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
    logging.info("=== Nightly job started ===")

    # 1. Refresh data (scrape + ELO)
    ok = run([sys.executable, os.path.join(BASE, "refresh.py")])
    if not ok:
        logging.error("Refresh failed — skipping git push.")
        return

    # 2. Stage updated data files
    ok = run(["git", "-C", BASE, "add", "data/current_results.csv", "data/elo_history.csv"])
    if not ok:
        return

    # 3. Check if anything actually changed
    result = subprocess.run(
        ["git", "-C", BASE, "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if result.returncode == 0:
        logging.info("No data changes — nothing to push.")
        logging.info("=== Nightly job complete (no changes) ===\n")
        return

    # 4. Commit
    date_str = datetime.now().strftime("%Y-%m-%d")
    ok = run(["git", "-C", BASE, "commit", "-m", f"nightly: refresh data {date_str}"])
    if not ok:
        return

    # 5. Push → triggers GitHub Actions to rebuild and redeploy the site
    ok = run(["git", "-C", BASE, "push", "origin", "main"])
    if not ok:
        return

    logging.info("=== Nightly job complete — site will redeploy shortly ===\n")


if __name__ == "__main__":
    main()
