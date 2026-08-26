#!/usr/bin/env python3
"""
Turn the Canadian Highlander sheet into flat, geocoded data for the map.

The sheet uses its first column two ways: as a country banner on its own row
("USA", "Canada", ...) and as a "City, Region, Country" label on store rows.
Blank rows separate the sections. This flattens all of that, geocodes any
address it hasn't seen before, and writes a plain CSV the front end reads
without knowing any of it.

Geocoding notes
---------------
Nominatim is a text search over OpenStreetMap, not an address parser. It does
not ignore tokens it cannot match — an unmatched postcode or a venue name in
front of the house number will return zero results rather than a looser match.
So geocode() walks a cascade from most to least specific and stops at the first
hit, recording how precise that hit was in a `precision` column:

    manual    a hand-set coordinate from data/overrides.csv — always wins
    address   matched to a street address
    poi       matched the business by name
    postcode  postcode centroid, so within a few hundred metres
    city      city centroid from the sheet's own Location column

Anything short of `address` still puts the store on the map, which beats
dropping it. The front end renders the loose ones differently.

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
OVERRIDES = "data/overrides.csv"

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
    "day", "time", "organizer", "description", "last_verified",
    "lat", "lng", "precision",
]

STREET_WORDS = r"St|Rd|Ave|Blvd|Dr|Ln|Ct|Hwy|Pkwy|Pl|Ter|Trail|Way|Cir|Sq"


# --------------------------------------------------------------------------- io

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def load_overrides():
    """address -> (lat, lng), hand-set and never overwritten by the geocoder."""
    out = {}
    try:
        with open(OVERRIDES, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                addr = (row.get("address") or "").strip()
                lat, lng = (row.get("lat") or "").strip(), (row.get("lng") or "").strip()
                if addr and lat and lng:
                    out[norm(addr)] = (float(lat), float(lng))
    except (OSError, ValueError, TypeError):
        pass
    return out


# ---------------------------------------------------------------------- parsing

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def col(row, headers, *names):
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


def city_from_label(label):
    """'Vancouver, BC, Canada' -> 'Vancouver, BC, Canada' (already usable)."""
    return (label or "").strip()


def tidy(address):
    """
    Repair the separators Nominatim depends on. Commas are the only real
    delimiter it has, and a period standing in for one loses the boundary.
    """
    s = re.sub(r"\s+", " ", address or "").strip()
    # "2301 Spenard Rd. Anchorage" -> "2301 Spenard Rd, Anchorage"
    s = re.sub(r"\b(%s)\.\s+(?=[A-Z])" % STREET_WORDS, r"\1, ", s)
    # "York, PA. 17402" -> "York, PA 17402"
    s = re.sub(r"\b([A-Z]{2})\.\s+(?=\d)", r"\1 ", s)
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"(,\s*)+,", ", ", s)
    return s.strip(" ,")


def strip_unit(s):
    s = re.sub(
        r"[,\s]+(?:suite|ste|unit|apt|bldg|fl|floor|rm|room)\.?\s*[\w-]+",
        "", s, flags=re.I,
    )
    s = re.sub(r"[,\s]+#\s*[\w-]+", "", s)
    return tidy(s)


def strip_leading_venue(s):
    """
    'Semiahmoo Mall, 1711 152 St #134, Surrey' -> '1711 152 St #134, Surrey'.
    Only when the first segment has no digits and something after it does, so a
    real house number is never thrown away.
    """
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 3 and not re.search(r"\d", parts[0]) \
       and re.search(r"\d", " ".join(parts[1:])):
        return ", ".join(parts[1:])
    return s


def strip_postcode(s, code):
    """Postcodes are thinly mapped in OSM; an unmatched one zeroes the query."""
    if code == "ca":
        s = re.sub(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b", "", s, flags=re.I)
    elif code == "gb":
        s = re.sub(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", "", s, flags=re.I)
    elif code in ("us",):
        s = re.sub(r"\b\d{5}(-\d{4})?\b", "", s)
    else:
        s = re.sub(r"\b\d{4,5}\b(?=\s*[A-Za-zÀ-ÿ])", "", s)
    return tidy(s)


def find_postcode(s, code):
    pats = {
        "ca": r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b",
        "gb": r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b",
        "us": r"\b\d{5}(?:-\d{4})?\b",
    }
    m = re.search(pats.get(code, r"\b\d{4,5}\b"), s, flags=re.I)
    return m.group(0) if m else ""


def parse_sheet(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SystemExit("error: sheet is empty")

    raw_headers = [h.strip() for h in rows[0]]
    headers = {h.lower(): i for i, h in enumerate(raw_headers) if h}
    if "address" not in headers:
        raise SystemExit("error: no 'Address' column. Got headers: %r" % raw_headers)

    out = []
    current_country = ""

    for row in rows[1:]:
        if not any((c or "").strip() for c in row):
            continue  # spacer row

        label = (row[0] or "").strip() if row else ""
        address = col(row, headers, "address")

        if label and not address:      # country banner row
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
            "lat": "", "lng": "", "precision": "",
        })

    return out


# -------------------------------------------------------------------- geocoding

def query(params, code):
    params = dict(params)
    params.update({"format": "jsonv2", "limit": "1"})
    if code:
        params["countrycodes"] = code
    url = NOMINATIM + "?" + urllib.parse.urlencode(params)
    try:
        hits = json.loads(fetch(url))
    except Exception as e:
        print("    ! request failed: %s" % e, file=sys.stderr)
        return None
    finally:
        time.sleep(DELAY)
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def geocode(rec):
    """Most specific first; stop at the first hit and report how loose it was."""
    code = rec["country_code"]
    country = rec["country"]
    addr = tidy(rec["address"])

    def with_country(s):
        return s if country.lower() in s.lower() else "%s, %s" % (s, country)

    attempts = []

    # 1. the address as written
    attempts.append(("address", {"q": with_country(addr)}))

    # 2. without unit / suite fragments
    nounit = strip_unit(addr)
    if nounit != addr:
        attempts.append(("address", {"q": with_country(nounit)}))

    # 3. without a leading venue name
    novenue = strip_leading_venue(nounit)
    if novenue != nounit:
        attempts.append(("address", {"q": with_country(novenue)}))

    # 4. without the postcode — sparse OSM postcode data zeroes otherwise
    #    perfectly good queries
    nopost = strip_postcode(novenue, code)
    if nopost != novenue:
        attempts.append(("address", {"q": with_country(nopost)}))

    # 5. the business by name — often mapped as a POI where addresses are not
    if rec["store"]:
        city = city_from_label(rec["location"]) or country
        attempts.append(("poi", {"q": "%s, %s" % (rec["store"], city)}))

    # 6. postcode centroid
    pc = find_postcode(addr, code)
    if pc:
        attempts.append(("postcode", {"postalcode": pc, "country": country}))

    # 7. city centroid from the sheet's own label, which is always well formed
    if rec["location"]:
        attempts.append(("city", {"q": rec["location"]}))

    for precision, params in attempts:
        hit = query(params, code if "postalcode" not in params else "")
        if hit:
            return {"lat": hit[0], "lng": hit[1], "precision": precision}
    return None


def main():
    if not RAW_URL:
        raise SystemExit("error: SHEET_CSV_URL is not set")

    records = parse_sheet(fetch(RAW_URL))
    print("Parsed %d stores." % len(records))

    cache = load_json(CACHE, {})
    overrides = load_overrides()
    if overrides:
        print("Loaded %d manual override(s)." % len(overrides))

    fresh, loose, failed = 0, [], []

    for rec in records:
        k = norm(rec["address"])

        if k in overrides:
            lat, lng = overrides[k]
            rec.update(lat="%.6f" % lat, lng="%.6f" % lng, precision="manual")
            continue

        hit = cache.get(k)
        if hit is None:
            print("Geocoding: %s" % rec["address"])
            hit = geocode(rec)
            fresh += 1
            cache[k] = hit if hit else {}
            if hit:
                print("    -> %s (%.5f, %.5f)" % (hit["precision"], hit["lat"], hit["lng"]))

        if hit:
            rec.update(
                lat="%.6f" % hit["lat"],
                lng="%.6f" % hit["lng"],
                precision=hit.get("precision", "address"),
            )
            if rec["precision"] not in ("address", "manual"):
                loose.append((rec["precision"], rec["store"] or rec["location"], rec["address"]))
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

    if loose:
        print("\n%d matched below street precision:" % len(loose))
        for p, name, a in loose:
            print("  [%-8s] %s — %s" % (p, name, a))
        print("  Pin one by hand in %s if any of these sit wrong." % OVERRIDES)

    if failed:
        print("\nCould not locate %d address(es):" % len(failed))
        for a in failed:
            print("  - %s" % a)
        print("  Fix in the sheet, or add coordinates to %s." % OVERRIDES)
        print("  Then delete the address from %s to force a retry." % CACHE)

    if located == 0:
        raise SystemExit("error: nothing was located — refusing to publish")


if __name__ == "__main__":
    main()
