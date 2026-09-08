# Paper Canadian Highlander — map

Static map of the store list, hosted on GitHub Pages, right here: https://theadonalsium.github.io/CanlanderMap/ 
A scheduled action pulls the Google Sheet, geocodes any new address, and redeploys.

## Files

```
index.html                 the map
data/locations.csv         flattened, geocoded store list — the only thing index.html reads
data/geocache.json         address -> coordinates, so nothing is geocoded twice
scripts/build_data.py      sheet -> locations.csv (stdlib only, no pip install)
.github/workflows/sync-and-deploy.yml
```

## Sheet format it expects

The sheet as it stands. Column A doubles as a country banner ("USA" on its own
row) and a "City, Region, Country" label on store rows; blank rows separate
sections. Named columns: Store, Address, Day/frequency, Time, Organizer,
Description, Last Verified. Only Address is required — everything else is
optional per row.

Add a country and its ISO code to `COUNTRY_CODES` in `scripts/build_data.py`
if the list ever expands beyond USA / Canada / UK / Germany / Denmark /
Switzerland.

## Fixing a store that won't map

The build prints anything it can't locate. Correct the address in the sheet,
then delete that address's entry from `data/geocache.json` and re-run the
workflow — a cached failure is remembered until you clear it.

## Running it locally

```bash
SHEET_CSV_URL='<published csv url>' python3 scripts/build_data.py
python3 -m http.server 8000     # then open http://localhost:8000
```

Opening `index.html` as a `file://` URL will not work — the browser blocks the
fetch of `data/locations.csv`. Use the local server.
