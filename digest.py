"""
OTHSL AI Digest — weekly email powered by Claude + Resend.

Usage:
  pip install anthropic resend
  set ANTHROPIC_API_KEY=sk-ant-...
  set RESEND_API_KEY=re_...
  python digest.py

Schedule with Windows Task Scheduler to run automatically.
"""

import csv
import os
import sys
from collections import defaultdict
from datetime import date


# ── config ──────────────────────────────────────────────────────────────────
TO_EMAIL = "jacobkhaykin09@gmail.com"
FROM_EMAIL = "OTHSL Digest <digest@othsl.khaykin.com>"  # must be a verified Resend sender domain
SUBJECT = f"OTHSL Weekly Digest — {date.today().strftime('%b %d, %Y')}"

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CURRENT_SEASON = "Spring 2026"


# ── data helpers ─────────────────────────────────────────────────────────────
def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def has_played_score(row):
    return row["date"] != "TBD" and row["home_goals"] != "" and row["away_goals"] != ""


def is_forfeit(value):
    return isinstance(value, str) and value.lower().startswith("forfeit")


def get_season_rows():
    rows = load_csv(os.path.join(DATA_DIR, "current_results.csv"))
    return [r for r in rows if r["season"] == CURRENT_SEASON]


def build_standings(rows, age_group, division, geography):
    stats = {}
    for r in rows:
        if r["age_group"] != age_group or r["division"] != division or r["geography"] != geography:
            continue
        ht, at = r["home_team"], r["away_team"]
        hg, ag = r["home_goals"], r["away_goals"]
        if "lost by forfeit" in ht.lower() or "lost by forfeit" in at.lower():
            continue
        if ht.strip().upper() == "TBD" or at.strip().upper() == "TBD":
            continue
        for t in (ht, at):
            if t not in stats:
                stats[t] = dict(gp=0, w=0, l=0, t=0, pts=0, gf=0, ga=0)
        if is_forfeit(hg) or is_forfeit(ag):
            if not is_forfeit(hg) and is_forfeit(ag):
                stats[ht]["w"] += 1; stats[ht]["pts"] += 3; stats[ht]["gp"] += 1
                stats[at]["l"] += 1; stats[at]["gp"] += 1
            elif is_forfeit(hg) and not is_forfeit(ag):
                stats[at]["w"] += 1; stats[at]["pts"] += 3; stats[at]["gp"] += 1
                stats[ht]["l"] += 1; stats[ht]["gp"] += 1
            else:
                stats[ht]["l"] += 1; stats[ht]["gp"] += 1
                stats[at]["l"] += 1; stats[at]["gp"] += 1
        elif has_played_score(r):
            hg_i, ag_i = int(hg), int(ag)
            stats[ht]["gf"] += hg_i; stats[ht]["ga"] += ag_i; stats[ht]["gp"] += 1
            stats[at]["gf"] += ag_i; stats[at]["ga"] += hg_i; stats[at]["gp"] += 1
            if hg_i > ag_i:
                stats[ht]["w"] += 1; stats[ht]["pts"] += 3; stats[at]["l"] += 1
            elif hg_i < ag_i:
                stats[at]["w"] += 1; stats[at]["pts"] += 3; stats[ht]["l"] += 1
            else:
                stats[ht]["t"] += 1; stats[ht]["pts"] += 1
                stats[at]["t"] += 1; stats[at]["pts"] += 1

    table = []
    for team, s in stats.items():
        ppg = s["pts"] / s["gp"] if s["gp"] else 0.0
        table.append({**s, "team": team, "gd": s["gf"] - s["ga"], "ppg": round(ppg, 2)})
    table.sort(key=lambda x: (-x["ppg"], -x["gd"], -x["gf"], x["team"]))
    return table


def gather_digest_data():
    rows = get_season_rows()

    # Flights
    flight_keys = sorted({(r["age_group"], r["division"], r["geography"]) for r in rows},
                         key=lambda k: (k[0], int(k[1]), k[2]))

    # Find the latest played date
    played_dates = sorted({r["date"] for r in rows if has_played_score(r) and r["date"] != "TBD"})
    latest_date = played_dates[-1] if played_dates else None
    next_dates = sorted({r["date"] for r in rows if not has_played_score(r) and not is_forfeit(r["home_goals"]) and not is_forfeit(r["away_goals"]) and r["date"] != "TBD"})
    next_date = next_dates[0] if next_dates else None

    # Recent results (latest matchday)
    recent_results = []
    if latest_date:
        for r in rows:
            if r["date"] != latest_date:
                continue
            if has_played_score(r):
                recent_results.append(
                    f"{r['home_team']} {r['home_goals']}-{r['away_goals']} {r['away_team']}"
                    f" ({r['age_group']} Div {r['division']} {r['geography']})"
                )
            elif is_forfeit(r["home_goals"]) or is_forfeit(r["away_goals"]):
                recent_results.append(
                    f"{r['home_team']} vs {r['away_team']} — FORFEIT"
                    f" ({r['age_group']} Div {r['division']} {r['geography']})"
                )

    # Upcoming fixtures (next matchday)
    upcoming = []
    if next_date:
        for r in rows:
            if r["date"] == next_date and not has_played_score(r):
                upcoming.append(
                    f"{r['home_team']} vs {r['away_team']}"
                    f" ({r['age_group']} Div {r['division']} {r['geography']})"
                )

    # Standings summary per flight (top 3)
    standings_summaries = []
    for key in flight_keys:
        ag, div, geo = key
        table = build_standings(rows, ag, div, geo)
        if not table:
            continue
        label = f"{ag} Division {div} {geo}"
        top3 = table[:3]
        lines = [f"  {i+1}. {t['team']} — {t['w']}W/{t['l']}L/{t['t']}T ({t['pts']} pts)" for i, t in enumerate(top3)]
        standings_summaries.append(f"{label}:\n" + "\n".join(lines))

    return {
        "season": CURRENT_SEASON,
        "latest_date": latest_date,
        "next_date": next_date,
        "recent_results": recent_results,
        "upcoming": upcoming,
        "standings_summaries": standings_summaries,
        "total_flights": len(flight_keys),
    }


# ── Claude digest generation ─────────────────────────────────────────────────
def generate_digest_html(data: dict) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    recent_block = "\n".join(data["recent_results"][:30]) if data["recent_results"] else "No results yet."
    upcoming_block = "\n".join(data["upcoming"][:20]) if data["upcoming"] else "No upcoming fixtures scheduled."
    standings_block = "\n\n".join(data["standings_summaries"][:10]) if data["standings_summaries"] else "No standings available."

    prompt = f"""You are writing the weekly email digest for OTHSL (Old Timers Hockey/Soccer League), a recreational adult soccer league.

Season: {data['season']}
Latest matchday: {data['latest_date'] or 'N/A'}
Next matchday: {data['next_date'] or 'N/A'}

Recent results ({data['latest_date']}):
{recent_block}

Upcoming fixtures ({data['next_date']}):
{upcoming_block}

Current standings (top 3 per flight):
{standings_block}

Write a warm, engaging, slightly humorous weekly digest email.
- Open with a short intro paragraph covering the week's headline story or a general observation.
- Include a "Results" section briefly narrating the most interesting recent results (pick the best 3-5, don't list them all mechanically).
- Include a "Table Talk" section highlighting any interesting standings battles (top of table, tight races, relegation danger).
- Close with a "Weekend Preview" teasing the upcoming fixtures worth watching.
- Keep it conversational and fun — this is a recreational league of adults who play for love of the game.
- Total length: around 300-400 words.
- Return ONLY the email body as clean HTML (no <html>/<body> wrapper). Use <h2> for section headings, <p> for paragraphs. No inline styles."""

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=1500,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        body = stream.get_final_message().content

    # Extract the text block
    text = ""
    for block in body:
        if hasattr(block, "text"):
            text = block.text
            break

    return text


# ── Resend delivery ──────────────────────────────────────────────────────────
def send_email(html_body: str):
    import resend

    resend.api_key = os.environ["RESEND_API_KEY"]

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{SUBJECT}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; color: #1f2a1f; background: #f4efe4; margin: 0; padding: 0; }}
  .wrap {{ max-width: 600px; margin: 32px auto; background: #fffaf1; border-radius: 16px; padding: 32px 36px; border: 1px solid rgba(38,61,41,0.14); }}
  h1 {{ color: #23442b; font-size: 1.4rem; margin-top: 0; }}
  h2 {{ color: #23442b; font-size: 1.1rem; border-bottom: 1px solid rgba(38,61,41,0.14); padding-bottom: 4px; }}
  p {{ line-height: 1.65; margin: 0 0 14px; }}
  .footer {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid rgba(38,61,41,0.14); font-size: 0.82rem; color: #5f695f; text-align: center; }}
  a {{ color: #c68a2b; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>OTHSL Digest &mdash; {date.today().strftime('%B %d, %Y')}</h1>
    {html_body}
    <div class="footer">
      <a href="https://jacobkhaykin.github.io/othsl/">othsl.khaykin.com</a> &middot;
      Data sourced from <a href="https://www.othsl.org">othsl.org</a>
    </div>
  </div>
</body>
</html>"""

    params = {
        "from": FROM_EMAIL,
        "to": [TO_EMAIL],
        "subject": SUBJECT,
        "html": full_html,
    }

    result = resend.Emails.send(params)
    return result


# ── main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Gathering league data...")
    data = gather_digest_data()
    print(f"  Latest matchday: {data['latest_date']}")
    print(f"  Recent results:  {len(data['recent_results'])}")
    print(f"  Upcoming:        {len(data['upcoming'])}")
    print(f"  Flights:         {data['total_flights']}")

    print("\nGenerating digest with Claude...")
    html_body = generate_digest_html(data)
    print("  Done.")

    # Preview mode: just print the HTML
    if "--preview" in sys.argv:
        print("\n--- DIGEST PREVIEW ---")
        print(html_body)
        sys.exit(0)

    print(f"\nSending email to {TO_EMAIL} via Resend...")
    result = send_email(html_body)
    print(f"  Sent! ID: {result.get('id', result)}")
