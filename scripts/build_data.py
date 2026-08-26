#!/usr/bin/env python3
"""
Turn the Canadian Highlander sheet into flat, geocoded data for the map.

The sheet uses its first column two ways: as a country banner on its own row
("USA", "Canada", ...) and as a "City, Region, Country" label on store rows.
Blank rows separate the sections. This flattens all of that, geocodes any
address it hasn't seen before, and writes a plain CSV the front end can read
without knowing any of it.

Geocoding results persist in data/geocache.json, keyed by address, so a normal
run makes zero network calls for stores that were already on the map.

Stdlib only — nothing to install in CI.
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

RAW_URL = os.environ.get("SHEET_CSV_URL", "")
OUT_CSV = "data/locations.csv"
CACHE = "data/geocache.json"

USER_AGENT = "canadian-highlander-map/1.0 (+github actions; static site build)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
DELAY = 1.1  # Nominatim asks for no more than one request per second

COUNTRY_CODES = {
    "usa": "us", "united states": "us", "us": "us",
    "canada": "ca",
    "uk": "gb", "united kingdom": "gb", "great britain": "gb",
    "germany": "de", "deutschland": "de",
    "denmark": "dk", "danmark": "dk",
    "switzerland": "ch", "suisse": "ch", "schweiz": "ch",
    "netherlands": "nl", "france": "fr", "spain": "es", "italy": "it",
    "sweden": "se", "norway": "no", "finland": "fi", "ireland": "ie",
    "australia": "au", "new zealand": "nz", "japan": "jp", "brazil": "br",
    "mexico": "mx", "poland": "pl", "austria": "at", "belgium": "be",
}

FIELDS = [
    "location", "store", "address", "country", "country_code",
    "day", "time", "organizer", "description", "last_verified", "lat", "lng",
]


# --------------------------------------------------------------------------- io

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------- parsing

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def col(row, headers, *names):
    """Fetch a cell by any of several possible header names."""
    for n in names:
        if n in headers:
            i = headers[n]
            if i < len(row):
                return (row[i] or "").strip()
    return ""


def country_from_label(label):
    """'Lyngby, Hovedstaden, Denmark' -> 'Denmark'"""
    parts = [p.strip() for p in (label or "").split(",") if p.strip()]
    return parts[-1] if parts else ""


def parse_sheet(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SystemExit("error: sheet is empty")

    raw_headers = [h.strip() for h in rows[0]]
    headers = {h.lower(): i for i, h in enumerate(raw_headers) if h}

    # The label column has no header in the sheet, so fall back to position 0.
    if "address" not in headers:
        raise SystemExit(
            "error: no 'Address' column. Got headers: %r" % raw_headers
        )

    out = []
    current_country = ""

    for row in rows[1:]:
        if not any((c or "").strip() for c in row):
            continue  # spacer row

        label = (row[0] or "").strip() if row else ""
        address = col(row, headers, "address")

        # A row with a label but no address is a country banner.
        if label and not address:
            current_country = label
            continue
        if not address:
            continue

        country = country_from_label(label) or current_country
        out.append({
            "location": label,
            "store": col(row, headers, "store"),
            "address": address,
            "country": country,
            "country_code": COUNTRY_CODES.get(country.lower(), ""),
            "day": col(row, headers, "day/frequency", "day", "frequency"),
            "time": col(row, headers, "time"),
            "organizer": col(row, headers, "organizer"),
            "description": col(row, headers, "description"),
            "last_verified": col(row, headers, "last verified", "last_verified"),
            "lat": "",
            "lng": "",
        })

    return out


# -------------------------------------------------------------------- geocoding

def strip_unit(s):
    s = re.sub(
        r"[,\s]+(?:suite|ste|unit|apt|bldg|fl|floor|rm|room)\.?\s*[\w-]+",
        "", s, flags=re.I,
    )
    s = re.sub(r"[,\s]+#\s*[\w-]+", "", s)
    return re.sub(r"\s{2,}", " ", s).replace(", ,", ",").strip()


def geocode(address, code):
    """Try the address as given, then again without unit/suite noise."""
    attempts = [address]
    stripped = strip_unit(address)
    if stripped != address:
        attempts.append(stripped)

    for q in attempts:
        params = {"format": "jsonv2", "limit": "1", "q": q}
        if code:
            params["countrycodes"] = code
        url = NOMINATIM + "?" + urllib.parse.urlencode(params)
        try:
            hits = json.loads(fetch(url))
        except Exception as e:
            print("  ! request failed: %s" % e, file=sys.stderr)
            time.sleep(DELAY)
            continue
        if hits:
            h = hits[0]
            return {
                "lat": float(h["lat"]),
                "lng": float(h["lon"]),
                "matched": h.get("display_name", ""),
            }
        time.sleep(DELAY)
    return None


def main():
    if not RAW_URL:
        raise SystemExit("error: SHEET_CSV_URL is not set")

    records = parse_sheet(fetch(RAW_URL))
    print("Parsed %d stores." % len(records))

    cache = load_cache()
    fresh = 0
    failed = []

    for rec in records:
        key = norm(rec["address"])
        hit = cache.get(key)

        if hit is None:
            country = rec["country"] or ""
            query = rec["address"]
            if country and country.lower() not in query.lower():
                query = "%s, %s" % (query, country)

            print("Geocoding: %s" % rec["address"])
            hit = geocode(query, rec["country_code"])
            time.sleep(DELAY)
            fresh += 1
            cache[key] = hit if hit else {}  # {} marks a known failure

        if hit:
            rec["lat"] = "%.6f" % hit["lat"]
            rec["lng"] = "%.6f" % hit["lng"]
        else:
            failed.append(rec["address"])

    save_cache(cache)

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)

    located = sum(1 for r in records if r["lat"])
    print("\n%d stores, %d located, %d newly geocoded." % (len(records), located, fresh))

    if failed:
        print("\nCould not locate %d address(es):" % len(failed))
        for a in failed:
            print("  - %s" % a)
        print("\nFix these in the sheet, then delete their entries from "
              "%s to force a retry." % CACHE)

    if located == 0:
        raise SystemExit("error: nothing was located — refusing to publish")


if __name__ == "__main__":
    main()
