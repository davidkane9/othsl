# OTHSL Match Intelligence

A Khaykin and Kane production.

Live site: https://davidkane9.github.io/othsl/

Standings, team pages, ELO ratings, matchweek history, and interactive season projections for the Over-The-Hill Soccer League. Current season: **Fall 2026** (Mass Soccer season 179, league 7).

## Run locally

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open http://localhost:5000.

## Refresh data

```powershell
python refresh.py
```

This fetches the public JSON feed used by [Mass Soccer Victory](https://victory.mass-soccer.org/schedules?season_id=179&league_id=7), recalculates ELO chronologically, regenerates analysis caches, and builds the static site in `docs/`. No Chrome or login is needed for current data. Failed downloads return a nonzero exit status; responses are validated before replacing existing files.

For just fixtures and official standings, run `python download_current/download.py`. Run `python elo.py` afterwards to update ratings. Use `python refresh.py --skip-build` to refresh data and analysis without building the site.

The current table uses official points, deductions, and ordering. Regular league fixtures feed the projections; byes, cross-flight exhibitions, and playoff rounds are excluded. Historical tables are reconstructed from archived results and may lack administrative deductions. The summary, finishing-position chart, and interactive table share one reproducible 10,000-run baseline per flight. Reset restores those exact results. What-if scenarios use 10,000 runs; conditional match-impact commentary uses 2,000 runs per outcome. The baseline summary is hidden while viewing a what-if scenario or a historical week. Promotion/relegation cutoffs and simulated tiebreakers are model assumptions, not guarantees from the league.

Season settings live in `season_config.py`. At rollover, change the ID, name, and short code together. The importer checks the source name and archives the previous current-season snapshot. Legacy historical download scripts remain available separately and use the old OTHSL site.

## Verify and build

```powershell
python -m unittest discover -s tests -v
node tests/test_browser_consistency.mjs
python freeze.py
```

GitHub Actions refreshes data and publishes to GitHub Pages on pushes to `main`, manual runs, and daily at 09:00 UTC (4 or 5 a.m. Eastern). It builds full historical pages as well. Local builds default to current pages; set `OTHSL_FULL_FREEZE=1` for the full archive. Scheduled refreshes publish refreshed data in the built site; they do not commit the downloaded CSVs back to `main`.

AI commentary is optional and uses `OPENAI_API_KEY` when configured. Calculated fallbacks work without it.
