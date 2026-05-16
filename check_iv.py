import os
os.chdir(r"c:/Users/Owner/Desktop/othsl")
from app import get_current_season_rows, clean_team_name

rows = get_current_season_rows()
target = "Irish Village"
ag_target = "Over 55"

games = []
for r in rows:
    h = clean_team_name(r.get("home_team",""))
    a = clean_team_name(r.get("away_team",""))
    if target in (h, a) and r.get("age_group") == ag_target and r.get("division") == "2" and r.get("geography") == "South":
        hs = r.get("home_score","")
        asp = r.get("away_score","")
        played = bool(hs and asp)
        if played:
            hs_i, as_i = int(hs), int(asp)
            if h == target:
                result = "W" if hs_i > as_i else ("L" if hs_i < as_i else "T")
            else:
                result = "W" if as_i > hs_i else ("L" if as_i < hs_i else "T")
        else:
            result = "TBD"
        games.append((r.get("date",""), h, a, hs, asp, result))

games.sort(key=lambda x: x[0] or "")
print(f"Total games: {len(games)}")
for g in games:
    print(g)
