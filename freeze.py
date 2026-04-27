"""
Generate a static snapshot of the OTHSL site using Frozen-Flask.

Output goes to docs/ (served by GitHub Pages).

Usage:
  pip install frozen-flask
  python freeze.py
"""

import os
from flask_frozen import Freezer
from app import (
    app, get_team_catalog, get_flight_catalog,
    get_all_seasons, get_rows_for_season, flight_slug,
    season_to_slug, CURRENT_SEASON,
)

# Output directory — GitHub Pages serves from docs/ on main branch
DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")

app.config["FREEZER_DESTINATION"] = DOCS_DIR
app.config["FREEZER_RELATIVE_URLS"] = True
app.config["FREEZER_REMOVE_EXTRA_FILES"] = True

freezer = Freezer(app)


@freezer.register_generator
def team_page():
    for item in get_team_catalog():
        yield {"team_slug": item["slug"]}

@freezer.register_generator
def flight_page():
    for card in get_flight_catalog():
        yield {"flight_slug_val": card["slug"]}

@freezer.register_generator
def calibration_page():
    yield {}


@freezer.register_generator
def index_historical():
    for season in get_all_seasons():
        if season == CURRENT_SEASON:
            continue
        yield {"season_slug": season_to_slug(season)}


@freezer.register_generator
def flight_page_historical():
    from app import get_standings_for_flight, identify_playoff_visitors
    for season in get_all_seasons():
        if season == CURRENT_SEASON:
            continue
        slug = season_to_slug(season)
        rows = get_rows_for_season(season)
        flights = {
            (r["age_group"], r["division"], r["geography"])
            for r in rows
            if r["age_group"] and r["division"] and r["geography"]
        }
        for age_group, division, geography in flights:
            pv = identify_playoff_visitors(rows, age_group, division, geography)
            standings = get_standings_for_flight(rows, age_group, division, geography, playoff_visitors=pv)
            if not standings:
                continue
            yield {
                "season_slug": slug,
                "flight_slug_val": flight_slug(age_group, division, geography),
            }

if __name__ == "__main__":
    print(f"Freezing site to {DOCS_DIR} ...")
    freezer.freeze()

    # GitHub Pages needs this file to disable Jekyll processing
    nojekyll = os.path.join(DOCS_DIR, ".nojekyll")
    open(nojekyll, "w").close()

    print(f"Done. Static site written to docs/")
