"""
M3 step 3.4 — geocode ReportClaims via Overture Maps over DuckDB.

Three-tier strategy (highest confidence wins):

  Tier 1 (high)   Overture `places`     -> named landmark match
                  e.g. "Charity Hospital" in Louisiana
  Tier 2 (medium) Overture `divisions`  -> county / parish centroid
                  subtype='county', filtered by US state region
  Tier 3 (low)    Overture `divisions`  -> state region centroid
                  subtype='region'      fallback

If none of the tiers match, geo_confidence = "unresolved" and lat/lon
remain None. The downstream fusion engine treats unresolved claims as
text-only (cosine similarity still works; spatial filtering does not).

DuckDB reads Overture parquet directly from the public sponsor bucket
`s3://overturemaps-us-west-2/release/<YYYY-MM-DD.X>/`. Anonymous access
works — no AWS credentials required for this module — but parquet
metadata fetching is per-query. We therefore *batch*: one query for all
landmarks, one for all counties, one for all states. Three round trips
total instead of 3 x N.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import duckdb


# Latest Overture release at time of writing. Bump when a new one drops.
OVERTURE_RELEASE = "2026-04-15.0"
OVERTURE_BUCKET = "s3://overturemaps-us-west-2/release"

PLACES_URL = (
    f"{OVERTURE_BUCKET}/{OVERTURE_RELEASE}/"
    "theme=places/type=place/*.parquet"
)
DIVISIONS_URL = (
    f"{OVERTURE_BUCKET}/{OVERTURE_RELEASE}/"
    "theme=divisions/type=division/*.parquet"
)


# US state name -> ISO 3166-2 region code used by Overture.
# Limited to disaster-relevant states; extend as needed.
_STATE_TO_REGION = {
    "alabama":      "US-AL",
    "alaska":       "US-AK",
    "arizona":      "US-AZ",
    "arkansas":     "US-AR",
    "california":   "US-CA",
    "colorado":     "US-CO",
    "connecticut":  "US-CT",
    "delaware":     "US-DE",
    "florida":      "US-FL",
    "georgia":      "US-GA",
    "hawaii":       "US-HI",
    "idaho":        "US-ID",
    "illinois":     "US-IL",
    "indiana":      "US-IN",
    "iowa":         "US-IA",
    "kansas":       "US-KS",
    "kentucky":     "US-KY",
    "louisiana":    "US-LA",
    "maine":        "US-ME",
    "maryland":     "US-MD",
    "massachusetts":"US-MA",
    "michigan":     "US-MI",
    "minnesota":    "US-MN",
    "mississippi":  "US-MS",
    "missouri":     "US-MO",
    "montana":      "US-MT",
    "nebraska":     "US-NE",
    "nevada":       "US-NV",
    "new hampshire":"US-NH",
    "new jersey":   "US-NJ",
    "new mexico":   "US-NM",
    "new york":     "US-NY",
    "north carolina":"US-NC",
    "north dakota": "US-ND",
    "ohio":         "US-OH",
    "oklahoma":     "US-OK",
    "oregon":       "US-OR",
    "pennsylvania": "US-PA",
    "rhode island": "US-RI",
    "south carolina":"US-SC",
    "south dakota": "US-SD",
    "tennessee":    "US-TN",
    "texas":        "US-TX",
    "utah":         "US-UT",
    "vermont":      "US-VT",
    "virginia":     "US-VA",
    "washington":   "US-WA",
    "west virginia":"US-WV",
    "wisconsin":    "US-WI",
    "wyoming":      "US-WY",
}


# ---------------------------------------------------------------------------
# Location-string parsing
# ---------------------------------------------------------------------------

_COUNTY_RE = re.compile(
    r"\b([A-Z][A-Za-z.\s\-]+?)\s+(County|Parish)\b", re.IGNORECASE
)


@dataclass
class LocationCandidates:
    """What we managed to pull out of the free-text location_name."""
    landmark: Optional[str]              # e.g. "Charity Hospital"
    counties: list[tuple[str, str]]      # [(name, "County"|"Parish"), ...]
    state_name: Optional[str]            # e.g. "Louisiana"
    state_region: Optional[str]          # e.g. "US-LA"


def parse_location(location_name: str) -> LocationCandidates:
    """
    Best-effort parse of a FEMA location string into searchable parts.

    Handles all 16 known shapes in our Katrina corpus:
      "Mobile County, Alabama"
        -> counties=[("Mobile","County")] state="Alabama"
      "Mobile State Docks, Mobile, Alabama"
        -> landmark="Mobile State Docks" state="Alabama"
      "Charity Hospital, 1532 Tulane Avenue, New Orleans, Louisiana 70112"
        -> landmark="Charity Hospital" state="Louisiana"
      "U.S. Highway 90 ... Hancock County and Harrison County, Mississippi"
        -> landmark="U.S. Highway 90 Bay St. Louis Bridge"
           counties=[("Hancock","County"),("Harrison","County")]
           state="Mississippi"
    """
    text = (location_name or "").strip()
    if not text:
        return LocationCandidates(None, [], None, None)

    # State: assume the last comma-segment with a known state name.
    # Strip any trailing ZIP code so "Louisiana 70112" still matches.
    state_name = None
    state_region = None
    for seg in reversed([s.strip() for s in text.split(",")]):
        candidate = re.sub(r"\s+\d{5}(-\d{4})?$", "", seg).strip().lower()
        if candidate in _STATE_TO_REGION:
            state_name = seg.strip()
            state_region = _STATE_TO_REGION[candidate]
            break

    # All county/parish mentions, anywhere in the string.
    counties: list[tuple[str, str]] = []
    for m in _COUNTY_RE.finditer(text):
        name = m.group(1).strip()
        kind = m.group(2).capitalize()  # "County" or "Parish"
        # Avoid duplicates from "X County and Y County".
        if (name, kind) not in counties:
            counties.append((name, kind))

    # Landmark candidate: the first comma-segment, IF it doesn't itself
    # look like a county/parish/state. Trim trailing parenthetical noise.
    first_seg = text.split(",", 1)[0].strip()
    first_seg_clean = re.sub(r"\s*\(.*?\)\s*$", "", first_seg).strip()
    looks_admin = (
        _COUNTY_RE.search(first_seg_clean)
        or first_seg_clean.lower() in _STATE_TO_REGION
    )
    landmark = None if looks_admin else (first_seg_clean or None)

    return LocationCandidates(
        landmark=landmark,
        counties=counties,
        state_name=state_name,
        state_region=state_region,
    )


# ---------------------------------------------------------------------------
# DuckDB connection + batched queries
# ---------------------------------------------------------------------------


def _connect() -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection ready to read Overture from public S3."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs;  LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    return con


def _query_places(
    con: duckdb.DuckDBPyConnection,
    landmark_keys: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[float, float]]:
    """
    Look up landmarks in Overture `places`.

    `landmark_keys` is a list of (lower_name, region_code) pairs.
    Returns a dict: (lower_name, region_code) -> (lat, lon).

    We use a LIKE prefix match — Overture place names sometimes have
    qualifiers ("Charity Hospital New Orleans") so an exact equality is
    too brittle. The query filters tightly by region first to keep the
    scan small.
    """
    if not landmark_keys:
        return {}

    # Overture `places.addresses` is a LIST of address structs, and the
    # `region` field there is a bare 2-letter state code ("LA"), not the
    # ISO "US-LA". So we strip the "US-" prefix when matching.
    #
    # Build a UNION ALL of per-landmark-region predicates. Each member
    # is parenthesized so its LIMIT applies locally, not to the union.
    union_sql = []
    params: list[Any] = []
    for name_lc, region in landmark_keys:
        short_region = region.removeprefix("US-")
        union_sql.append(
            "(SELECT ? AS key_name, ? AS key_region, "
            "names.primary AS overture_name, "
            "addresses[1].region AS overture_region, "
            "ST_X(ST_Centroid(geometry)) AS lon, "
            "ST_Y(ST_Centroid(geometry)) AS lat "
            f"FROM read_parquet('{PLACES_URL}') "
            "WHERE addresses IS NOT NULL "
            "  AND len(addresses) > 0 "
            "  AND addresses[1].country = 'US' "
            "  AND addresses[1].region = ? "
            "  AND lower(names.primary) LIKE ? "
            "LIMIT 1)"
        )
        params.extend([name_lc, region, short_region, f"{name_lc}%"])

    sql = "\n UNION ALL \n".join(union_sql)
    rows = con.execute(sql, params).fetchall()
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for key_name, key_region, _ovn, _ovr, lon, lat in rows:
        out[(key_name, key_region)] = (float(lat), float(lon))
    return out


def _query_counties(
    con: duckdb.DuckDBPyConnection,
    county_keys: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], tuple[float, float]]:
    """
    Look up counties / parishes in Overture `divisions`.

    `county_keys` is a list of (lower_name, kind, region_code) where
    kind is "County" or "Parish" (kept only so we can rebuild the same
    key on the Python side).

    Overture stores them all under subtype='county' regardless of
    "Parish" naming in Louisiana — but the *primary name* itself
    contains "Parish" for LA, so an exact name match still works.
    """
    if not county_keys:
        return {}

    # Build "name + ' ' + kind" -> 'mobile county', 'orleans parish' ...
    full_names = [
        (f"{n} {k}".lower(), region) for (n, k, region) in county_keys
    ]
    # Distinct (name, region) pairs to query.
    distinct: list[tuple[str, str]] = sorted(set(full_names))

    placeholders = ", ".join(["(?, ?)"] * len(distinct))
    flat_params: list[Any] = []
    for n, r in distinct:
        flat_params.extend([n, r])

    sql = f"""
        SELECT lower(names.primary)               AS name_lc,
               region                              AS region,
               ST_X(ST_Centroid(geometry))         AS lon,
               ST_Y(ST_Centroid(geometry))         AS lat
        FROM read_parquet('{DIVISIONS_URL}')
        WHERE country = 'US'
          AND subtype = 'county'
          AND (lower(names.primary), region) IN (VALUES {placeholders})
    """
    rows = con.execute(sql, flat_params).fetchall()
    found: dict[tuple[str, str], tuple[float, float]] = {}
    for name_lc, region, lon, lat in rows:
        found[(name_lc, region)] = (float(lat), float(lon))

    out: dict[tuple[str, str, str], tuple[float, float]] = {}
    for n, k, region in county_keys:
        full = f"{n} {k}".lower()
        if (full, region) in found:
            out[(n.lower(), k, region)] = found[(full, region)]
    return out


def _query_states(
    con: duckdb.DuckDBPyConnection,
    regions: list[str],
) -> dict[str, tuple[float, float]]:
    """Return region_code -> (lat, lon) for Overture state centroids."""
    if not regions:
        return {}
    placeholders = ", ".join(["?"] * len(regions))
    sql = f"""
        SELECT region,
               ST_X(ST_Centroid(geometry)) AS lon,
               ST_Y(ST_Centroid(geometry)) AS lat
        FROM read_parquet('{DIVISIONS_URL}')
        WHERE country = 'US'
          AND subtype = 'region'
          AND region IN ({placeholders})
    """
    rows = con.execute(sql, regions).fetchall()
    return {r[0]: (float(r[2]), float(r[1])) for r in rows}


# ---------------------------------------------------------------------------
# Public batch entry point
# ---------------------------------------------------------------------------


def geocode_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Geocode a list of ReportClaim dicts in place (returns the same list,
    mutated). Adds/updates lat, lon, county_parish, state, geo_confidence,
    geo_source.

    Three batched DuckDB queries: one places, one counties, one states.
    """
    con = _connect()

    # --- Pass 1: parse all locations ---
    parsed: list[LocationCandidates] = [
        parse_location(c.get("location_name", "")) for c in claims
    ]

    # Dedup keys for the batched queries.
    landmark_keys: set[tuple[str, str]] = set()
    county_keys: set[tuple[str, str, str]] = set()
    state_regions: set[str] = set()

    for cand in parsed:
        if cand.state_region:
            state_regions.add(cand.state_region)
            if cand.landmark:
                landmark_keys.add((cand.landmark.lower(), cand.state_region))
            for cname, ckind in cand.counties:
                county_keys.add((cname.lower(), ckind, cand.state_region))

    print(
        f"Geocoding plan: "
        f"{len(claims)} claims, "
        f"{len(landmark_keys)} unique landmarks, "
        f"{len(county_keys)} unique counties/parishes, "
        f"{len(state_regions)} states."
    )

    place_hits  = _query_places(con,  sorted(landmark_keys))
    county_hits = _query_counties(con, sorted(county_keys))
    state_hits  = _query_states(con,   sorted(state_regions))

    print(
        f"Hits: places={len(place_hits)} "
        f"counties={len(county_hits)} states={len(state_hits)}"
    )

    # --- Pass 2: assign best tier per claim ---
    for claim, cand in zip(claims, parsed):
        claim["state"] = cand.state_name
        # county_parish gets the first county we matched, if any.
        first_county = cand.counties[0][0] + " " + cand.counties[0][1] \
            if cand.counties else None
        claim["county_parish"] = first_county

        lat = lon = None
        confidence = "unresolved"
        source = "unresolved"

        # Tier 1: landmark in places.
        if cand.landmark and cand.state_region:
            key = (cand.landmark.lower(), cand.state_region)
            if key in place_hits:
                lat, lon = place_hits[key]
                confidence, source = "high", "overture_places"

        # Tier 2: first county / parish.
        if lat is None and cand.counties and cand.state_region:
            for cname, ckind in cand.counties:
                k = (cname.lower(), ckind, cand.state_region)
                if k in county_hits:
                    lat, lon = county_hits[k]
                    confidence, source = "medium", "overture_county"
                    break

        # Tier 3: state centroid.
        if lat is None and cand.state_region:
            if cand.state_region in state_hits:
                lat, lon = state_hits[cand.state_region]
                confidence, source = "low", "overture_region"

        claim["lat"] = lat
        claim["lon"] = lon
        claim["geo_confidence"] = confidence
        claim["geo_source"] = source

    return claims
