"""
Generate a static snapshot of the OTHSL site using Frozen-Flask.

Output goes to docs/ (served by GitHub Pages).

Usage:
  pip install frozen-flask
  python freeze.py
"""

import os
import shutil
from flask_frozen import Freezer
from app import app

# Output directory — GitHub Pages serves from docs/ on main branch
DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")

app.config["FREEZER_DESTINATION"] = DOCS_DIR
app.config["FREEZER_RELATIVE_URLS"] = True
app.config["FREEZER_REMOVE_EXTRA_FILES"] = True

freezer = Freezer(app)

if __name__ == "__main__":
    print(f"Freezing site to {DOCS_DIR} ...")
    freezer.freeze()

    # GitHub Pages needs this file to disable Jekyll processing
    nojekyll = os.path.join(DOCS_DIR, ".nojekyll")
    open(nojekyll, "w").close()

    print(f"Done. Static site written to docs/")
