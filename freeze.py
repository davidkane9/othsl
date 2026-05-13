"""
Generate a static snapshot of the OTHSL site using Frozen-Flask.

Output goes to docs/ (served by GitHub Pages).

Usage:
  pip install frozen-flask
  python freeze.py
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen

import app as app_module
from app import (
    CURRENT_SEASON,
    app,
    flight_slug,
    get_all_seasons,
    get_current_season_rows,
    get_flight_catalog,
    get_flight_page_context,
    get_rows_for_season,
    get_standings_for_flight,
    get_team_catalog,
    get_team_page_context,
    identify_playoff_visitors,
    season_to_slug,
)
from flask_frozen import Freezer

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


# ── AI text generation ────────────────────────────────────────────────────────

_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()


def _openai_call(prompt, max_tokens=200):
    if not _OPENAI_KEY:
        return ""
    try:
        body = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }).encode()
        req = Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_OPENAI_KEY}",
            },
        )
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"    AI call failed: {e}")
        return ""


def _flight_prompt(fslug, season):
    rows = get_current_season_rows()
    flights = {
        (r["age_group"], r["division"], r["geography"])
        for r in rows
        if r["age_group"] and r["division"] and r["geography"]
    }
    for ag, div, geo in flights:
        if flight_slug(ag, div, geo) != fslug:
            continue
        ctx = get_flight_page_context(ag, div, geo, rows=rows)
        if not ctx:
            return None
        sd = ctx["sim_data"]
        label = ctx["label"]
        teams = sd.get("teams", [])
        stats = sd.get("current_stats", {})
        pc = sd.get("promotion_cut", 2)
        rc = sd.get("relegation_cut", 2)
        n = len(teams)
        srt = sorted(teams, key=lambda t: (-stats.get(t, {}).get("pts", 0), -stats.get(t, {}).get("gd", 0)))
        standings_txt = "; ".join(
            f"{i+1}. {t}: {stats.get(t,{}).get('gp',0)}GP "
            f"{stats.get(t,{}).get('w',0)}W-{stats.get(t,{}).get('l',0)}L-{stats.get(t,{}).get('t',0)}T "
            f"{stats.get(t,{}).get('pts',0)}pts GD{stats.get(t,{}).get('gd',0)}"
            for i, t in enumerate(srt)
        )
        promo_zone = " & ".join(srt[:pc])
        relg_zone  = " & ".join(srt[n - rc:]) if n > rc else ""
        promo_gap  = (stats.get(srt[pc-1], {}).get("pts", 0) - stats.get(srt[pc], {}).get("pts", 0)) if len(srt) > pc else 0
        relg_gap   = (stats.get(srt[n-rc-1], {}).get("pts", 0) - stats.get(srt[n-rc], {}).get("pts", 0)) if n > rc else 0
        remaining  = sd.get("remaining_games", [])
        weeks_left = len({g["date"] for g in remaining if g.get("date") and g["date"] != "TBD"})
        # Find the flight's highest-leverage game: biggest point-differential matchup near a cutoff
        def _game_effect(g):
            if not g.get("date") or g["date"] == "TBD":
                return 0
            h, a = g["home"], g["away"]
            hp, ap = stats.get(h, {}).get("pts", 0), stats.get(a, {}).get("pts", 0)
            h_pos = srt.index(h) + 1 if h in srt else n
            a_pos = srt.index(a) + 1 if a in srt else n
            # Proximity to promo / relg border
            promo_prox = min(abs(h_pos - pc), abs(a_pos - pc), abs(h_pos - pc-1), abs(a_pos - pc-1))
            relg_prox  = min(abs(h_pos - (n-rc)), abs(a_pos - (n-rc))) if n > rc else 99
            zone_score = max(0, 4 - min(promo_prox, relg_prox))
            pts_close  = max(0, 7 - abs(hp - ap))
            hwp        = g.get("home_win_prob", 0.5)
            uncertainty = 1 - abs(hwp - 0.5) * 2
            return zone_score * 3 + pts_close + uncertainty * 2
        key_game = max((g for g in remaining if g.get("date") and g["date"] != "TBD"),
                       key=_game_effect, default=None)
        key_game_txt = (f"{key_game['home']} vs {key_game['away']} on {key_game['date']} "
                        f"({round(key_game.get('home_win_prob',0.5)*100)}% home win probability)"
                        if key_game else "none scheduled")
        upcoming = "; ".join(
            f"{g['home']} vs {g['away']} ({g['date']}, {round(g.get('home_win_prob',0.5)*100)}% HW)"
            for g in remaining[:5] if g.get("date") and g["date"] != "TBD"
        )
        return (
            f"You are a sharp soccer analyst for an adult recreational league (OTHSL). "
            f"Write 3 sentences analyzing the {label} flight this {season} season. "
            f"Cover: (1) promotion race — who leads and how tight the gap is, "
            f"(2) relegation danger — exact point gap from safety and who's most at risk, "
            f"(3) the single most decisive upcoming game and why it could swing the table. "
            f"Full standings ({weeks_left} week{'s' if weeks_left != 1 else ''} remaining): {standings_txt}. "
            f"Promotion zone top {pc}: {promo_zone} (gap to 3rd: {promo_gap}pts). "
            f"Relegation zone bottom {rc}: {relg_zone} (gap from safety: {relg_gap}pts). "
            f"Key game: {key_game_txt}. "
            f"All upcoming games: {upcoming or 'none scheduled'}. "
            f"Use exact team names, points, and win percentages. Casual, confident tone. No fluff."
        )
    return None


def _key_game_for_team(selected, my_pts, promo_pts, relg_pts, stats, remaining, n, rc):
    """Return a text description of the highest-leverage upcoming game for this team."""
    my_games = [g for g in remaining if g.get("involves_team") and g.get("date") and g["date"] != "TBD"]
    if not my_games:
        return None
    def _score(g):
        opp = g["away"] if g["home"] == selected else g["home"]
        opp_pts   = stats.get(opp, {}).get("pts", 0)
        is_home   = g["home"] == selected
        hwp       = g.get("home_win_prob", 0.5)
        team_wp   = hwp if is_home else max(0, 1 - hwp - g.get("draw_prob", 0.22))
        uncertainty   = 1 - abs(team_wp - 0.5) * 2
        pts_closeness = max(0, 9 - abs(my_pts - opp_pts))
        promo_rel = max(0, 7 - abs(my_pts - promo_pts)) + max(0, 7 - abs(opp_pts - promo_pts))
        relg_rel  = (max(0, 7 - abs(my_pts - relg_pts)) + max(0, 7 - abs(opp_pts - relg_pts))) if n > rc else 0
        return uncertainty * 2 + pts_closeness + promo_rel + relg_rel
    best    = max(my_games, key=_score)
    is_home = best["home"] == selected
    hwp     = best.get("home_win_prob", 0.5)
    team_wp = hwp if is_home else max(0, 1 - hwp - best.get("draw_prob", 0.22))
    opp     = best["away"] if is_home else best["home"]
    opp_pts = stats.get(opp, {}).get("pts", 0)
    return (f"{'vs' if is_home else '@'} {opp} on {best['date']} "
            f"({round(team_wp*100)}% win probability, {opp} has {opp_pts}pts)")


def _team_prompt(team_slug, season):
    ctx = get_team_page_context(team_slug)
    if not ctx:
        return None
    sd       = ctx["sim_data"]
    ti       = ctx["team_info"]
    sim      = ctx["simulation"]
    selected = ti["team"]
    label    = f"{ti['age_group']} Div {ti['division']} {ti['geography']}"
    teams    = sd.get("teams", [])
    stats    = sd.get("current_stats", {})
    pc = sd.get("promotion_cut", 2)
    rc = sd.get("relegation_cut", 2)
    n  = sd.get("total_teams", len(teams))
    srt = sorted(teams, key=lambda t: (-stats.get(t, {}).get("pts", 0), -stats.get(t, {}).get("gd", 0)))
    pos     = srt.index(selected) + 1 if selected in srt else 0
    my      = stats.get(selected, {})
    my_pts  = my.get("pts", 0)
    my_gp   = my.get("gp", 0)
    my_gd   = my.get("gd", 0)
    promo_pts = stats.get(srt[pc-1], {}).get("pts", my_pts) if len(srt) >= pc else my_pts
    relg_pts  = stats.get(srt[n-rc], {}).get("pts", my_pts) if n > rc and len(srt) > n-rc else my_pts
    promo_gap = promo_pts - my_pts
    relg_gap  = my_pts - relg_pts
    promo_prob = sim.get("promotion_probability", 0)
    relg_prob  = sim.get("relegation_probability", 0)
    standings_txt = "; ".join(
        f"{i+1}. {t}: {stats.get(t,{}).get('gp',0)}GP "
        f"{stats.get(t,{}).get('w',0)}W-{stats.get(t,{}).get('l',0)}L-{stats.get(t,{}).get('t',0)}T "
        f"{stats.get(t,{}).get('pts',0)}pts GD{stats.get(t,{}).get('gd',0)}"
        for i, t in enumerate(srt)
    )
    remaining  = sd.get("remaining_games", [])
    weeks_left = len({g["date"] for g in remaining if g.get("date") and g["date"] != "TBD"})
    my_games_txt = "; ".join(
        f"{'vs' if g['home']==selected else '@'} {g['away'] if g['home']==selected else g['home']} "
        f"({g['date']}, {round((g.get('home_win_prob',0.5) if g['home']==selected else max(0,1-g.get('home_win_prob',0.5)-g.get('draw_prob',0.22)))*100)}%win)"
        for g in remaining if g.get("involves_team") and g.get("date") and g["date"] != "TBD"
    )[:300]
    key_game = _key_game_for_team(selected, my_pts, promo_pts, relg_pts, stats, remaining, n, rc)
    pos_desc  = f"in promotion zone ({pos}/{n})" if promo_gap <= 0 else f"{pos}/{n}, {promo_gap}pts off promotion"
    relg_desc = f"IN relegation zone ({abs(relg_gap)}pts from safety)" if relg_gap < 0 else (
                f"on the relegation line" if relg_gap == 0 else f"{relg_gap}pts above relegation")
    return (
        f"You are a sharp soccer analyst for an adult recreational league (OTHSL). "
        f"Write 3 sentences analyzing {selected}'s season in the {label} flight ({season}). "
        f"Simulation (400 runs): {promo_prob}% promotion, {relg_prob}% relegation. "
        f"Currently {pos_desc}, {relg_desc}, after {my_gp} games (GD{my_gd:+d}). "
        f"Cover: (1) form and standing, "
        f"(2) exact point gap for promotion or relegation safety, "
        f"(3) the single most decisive upcoming game and why — use the key-game data. "
        f"Key game: {key_game or 'none scheduled'}. "
        f"Full standings: {standings_txt}. "
        f"Their remaining games ({weeks_left} wks left): {my_games_txt or 'none'}. "
        f"Use exact names, figures, percentages. Casual, confident. No fluff."
    )


def generate_all_ai_texts():
    if not _OPENAI_KEY:
        print("No OPENAI_API_KEY — skipping AI text generation.")
        return

    flight_catalog = get_flight_catalog()
    team_catalog   = get_team_catalog()
    total = len(flight_catalog) + len(team_catalog)
    print(f"Generating AI texts for {len(flight_catalog)} flights + {len(team_catalog)} teams ({total} total)…")

    flight_texts: dict = {}
    team_texts:   dict = {}
    done = [0]

    def _flight_job(card):
        fslug = card["slug"]
        prompt = _flight_prompt(fslug, CURRENT_SEASON)
        text = _openai_call(prompt) if prompt else ""
        done[0] += 1
        if done[0] % 10 == 0:
            print(f"  {done[0]}/{total} done…")
        return fslug, text

    def _team_job(item):
        tslug = item["slug"]
        prompt = _team_prompt(tslug, CURRENT_SEASON)
        text = _openai_call(prompt, max_tokens=200) if prompt else ""
        done[0] += 1
        if done[0] % 10 == 0:
            print(f"  {done[0]}/{total} done…")
        return tslug, text

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(_flight_job, c): "flight" for c in flight_catalog}
        futs.update({ex.submit(_team_job, t): "team" for t in team_catalog})
        for fut in as_completed(futs):
            kind = futs[fut]
            try:
                slug, text = fut.result()
                if kind == "flight":
                    flight_texts[slug] = text
                else:
                    team_texts[slug] = text
            except Exception as e:
                print(f"  job failed: {e}")

    app_module._ai_flight_texts = flight_texts
    app_module._ai_team_texts   = team_texts
    print(f"AI generation done: {sum(1 for v in flight_texts.values() if v)} flights, "
          f"{sum(1 for v in team_texts.values() if v)} teams.")


if __name__ == "__main__":
    generate_all_ai_texts()

    print(f"Freezing site to {DOCS_DIR} …")
    freezer.freeze()

    nojekyll = os.path.join(DOCS_DIR, ".nojekyll")
    open(nojekyll, "w").close()

    print("Done. Static site written to docs/")
