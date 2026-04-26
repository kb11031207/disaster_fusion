# GeoArbiter — Frontend

Header branding uses `public/geoarbiter-branding.png` (GeoArbiter logo + motto banner; replace this file to update branding).

This is the analyst review interface for the GeoArbiter fusion pipeline.
Each map point is one fused intelligence object derived from:

- **Video evidence** (Marengo retrieval + Pegasus description)
- **PDF damage reports** (extracted excerpts and claimed severities)
- **Overture geospatial reference data** (buildings, places, roads)

The goal is not "a pretty map" — it is a defensible **evidence chain** for
each finding so an analyst can answer *what was damaged, where, by which
source, and how confident the system is*.

## Scenario

The default demo scenario is **Hurricane Katrina — New Orleans Damage
Assessment**, with 12 sample fused findings around the Lower Ninth Ward,
Lakeview, downtown New Orleans, and St. Bernard Parish.

## Tech

- Vite + React 19
- react-leaflet (CARTO Voyager / Positron / OSM / Dark Matter base layers)
- `leaflet.heat` for the severity-weighted density underlay
- Tailwind CSS v3 — warm white + amber Anthropic-style palette
- Pure static data — no backend required for the demo

## Run

```bash
npm install
npm run dev
```

Open http://localhost:5173.

```bash
npm run build && npm run preview
```

## Project structure

```
public/data/
  raw_candidates.json         # Simulated upstream pipeline output (video+PDF)
  overture_reference.geojson  # Real Overture subset (New Orleans bbox)
  master_findings.json        # Validated findings — what the UI renders

scripts/
  extract_overture.mjs        # DuckDB → Overture S3 → overture_reference.geojson
  validate_findings.mjs       # raw_candidates × Overture → master_findings

src/
  App.jsx                     # Layout + state (filters, selection, report modal)
  lib/
    markers.js                # Severity colours, fusion-status accents, labels
    format.js                 # Helpers (percent, latlon, JSON download)
    overture.js               # Validator (works in Node and the browser)
  components/
    Header.jsx                # Brand, scenario, export/report buttons
    SummaryCards.jsx          # Top metrics row (computeStats lives here)
    Filters.jsx               # Status / severity / facility / confidence / search / comparison + heat toggle
    FindingsList.jsx          # Sidebar list of currently-visible findings
    DamageMap.jsx             # Leaflet map + Overture overlays + heat layer + DivIcon markers
    ContextTab.jsx            # Object-level evidence chain (THE key view) — includes Overture validation panel
    ReportPreview.jsx         # FEMA-style report modal with print-to-PDF
    Legend.jsx                # Severity + fusion-status legend
```

## Overture pipeline

This frontend consumes the output of a small, reproducible
Overture-validation pipeline:

```
video + PDF analyzers
        │
        ▼
public/data/raw_candidates.json
        │  (entity_name, approx lat/lon, hints, fusion engine output)
        ▼
[ scripts/validate_findings.mjs ] + public/data/overture_reference.geojson
        │  (real Overture extract, see scripts/extract_overture.mjs)
        ▼
public/data/master_findings.json  ← what the UI renders
```

### Run it

```bash
# 1. Pull a fresh Overture subset for the New Orleans bbox.
#    Uses DuckDB + httpfs to query the public Overture S3 release.
npm run data:overture

# 2. Validate the simulated pipeline output against that subset.
npm run data:validate

# 3. Or both in one go:
npm run data:pipeline
```

The validator (`src/lib/overture.js`) snaps each candidate to the nearest
Overture feature using a weighted blend of spatial proximity, name
similarity, and category affinity. It produces the canonical lat/lon,
GERS-style id, address, category, match method, and a confidence score,
which is exactly what the **Overture validation** panel in the Context
Tab displays.

The same validator runs in the browser when you press **"Re-validate
live"** in the Context Tab — useful for showing the matching step in
real time during demos.

## Demo flow

1. Open dashboard — Voyager basemap, Overture overlays, summary cards load.
2. See 12 colour-coded points across New Orleans plus a soft severity heat layer.
3. Click any point — context tab opens with full evidence chain:
   side-by-side source comparison, video/PDF/Overture blocks, fusion
   reasoning with sub-scores, and a recommended action.
4. Use **Comparison mode** in filters to show only findings where the
   sources disagree (the most interesting cases for judges).
5. Toggle **Heat layer** to switch the density underlay on/off.
6. Click **Generate Report** to open the FEMA-style report preview;
   from there, **Download JSON** or **Print / Save as PDF**.

## Marker colour logic

Lives in `src/lib/markers.js`. Markers are coloured by **severity** so the
analyst's primary visual question — *how bad is it?* — is answered at a
glance. **Fusion status** (whether the sources agree) is communicated via
a coloured **outline ring** on the marker plus a status badge in the
findings list and context tab.

| Marker fill | Severity |
| --- | --- |
| `#D95D39` | Destroyed |
| `#F28C28` | Severe |
| `#e8943d` | Moderate |
| `#f4b97a` | Minor |
| `#9CA3AF` | Unknown / no severity |

| Outline / badge | Fusion status |
| --- | --- |
| Emerald | Confirmed (sources agree) |
| Violet  | Unreported damage (video only) |
| Blue    | Unsupported PDF claim |
| Pink    | Sources disagree on severity |
| Gray    | Uncertain / insufficient evidence |

## Visual palette

The interface uses a warm-white + orange palette designed to feel calm and
authoritative rather than dashboard-noisy:

- background: `#FFFDF8`
- cards: `#FFFFFF`
- primary accent: `#e8640c`
- secondary accent (hover / darker): `#cf5a0a`
- text: `#2F2A24`
- muted borders: `#F1E6CF`

## Heat layer

`src/components/DamageMap.jsx` wires `leaflet.heat` as a soft underlay
weighted by severity (destroyed = 1.0, severe = 0.8, moderate = 0.55,
minor = 0.3). The gradient mirrors the marker palette so the heat field
reads as the same visual story at a different scale.

The heat layer is **atmospheric only** — interaction is always through
the markers, because the product is built around object-level evidence
chains.

## Data contract

`master_findings.json` matches the schema documented in
`BACKEND_CONTRACT.md`. To plug in real backend output, replace the file
(or change the `fetch()` calls in `App.jsx`).

## What this app deliberately does NOT do

No login, no user accounts, no live Overture queries from the browser,
no upload pipeline, no real-time streaming, no polygon editing. It is a
**stable, convincing analyst review surface**.
