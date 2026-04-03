"""
All OTHSL seasons and known division codes.

Season codes: '{2-digit year}{s|f}'
  s = Spring, f = Fall
  Years 97-99 map to 1997-1999; 00-26 map to 2000-2026.

Division codes: '{prefix}{number}{geo}'
  Prefixes (age groups):
    o = Over 30
    m = Over 40
    s = Over 48
    v = Over 55
    z = Over 62
    a = Over 68
  Geo: n = North, s = South, c = Central

Note: Not every division exists in every season. The scrapers try each
combination and skip pages that return no schedule data.
"""

# All seasons from Spring 1997 to Spring 2026, in chronological order.
ALL_SEASONS = [
    "97s", "97f",
    "98s", "98f",
    "99s", "99f",
    "00s", "00f",
    "01s", "01f",
    "02s", "02f",
    "03s", "03f",
    "04s", "04f",
    "05s", "05f",
    "06s", "06f",
    "07s", "07f",
    "08s", "08f",
    "09s", "09f",
    "10s", "10f",
    "11s", "11f",
    "12s", "12f",
    "13s", "13f",
    "14s", "14f",
    "15s", "15f",
    "16s", "16f",
    "17s", "17f",
    "18s", "18f",
    "19s", "19f",
    "20s", "20f",
    "21s", "21f",
    "22s", "22f",
    "23s", "23f",
    "24s", "24f",
    "25s", "25f",
    "26s",
]

CURRENT_SEASON = "26s"

# All known division codes by age group, as visible in Fall 2025 navigation.
# Older seasons may have fewer divisions; the scraper skips empty pages.
DIVISIONS_BY_AGE_GROUP = {
    "Over 30":  ["o1n", "o1s", "o2n", "o2s", "o3n", "o3s"],
    "Over 40":  ["m1n", "m1s", "m2n", "m2s", "m3n", "m3s",
                 "m4n", "m4s", "m5n", "m5s", "m6c"],
    "Over 48":  ["s1n", "s1s", "s2n", "s2s", "s3n", "s3s",
                 "s4n", "s4s", "s5n", "s5s"],
    "Over 55":  ["v1n", "v1s", "v2n", "v2s", "v3n", "v3s"],
    "Over 62":  ["z1c", "z2c"],
    "Over 68":  ["a1c", "a2c"],
}

# Flat list of all division codes
ALL_DIVISIONS = [lnd for divs in DIVISIONS_BY_AGE_GROUP.values() for lnd in divs]

# Irish Village plays in Over 55 Division 2 South
IRISH_VILLAGE_LND = "v2s"
