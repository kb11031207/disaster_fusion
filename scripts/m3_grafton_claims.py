"""
M3 (Grafton) — load hand-authored Grafton claim JSON, normalize to our
ReportClaim shape, geocode via Overture, and write report_claims_grafton.json.

The Grafton reports are NWS / EMA / news, not FEMA DOCX. Rather than build
five separate parsers for one demo event, the claims were authored directly
from the source articles and saved as `data/raw/reports/grafton_reports.json`.
This script just normalizes + geocodes them.

Usage
=====
    python scripts/m3_grafton_claims.py
    python scripts/m3_grafton_claims.py --in data/raw/reports/grafton_reports.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.shared.models import ReportClaim  # noqa: E402


DEFAULT_INPUT = (
    _PROJECT_ROOT / "data" / "raw" / "reports" / "grafton_reports.json"
)
DEFAULT_OUTPUT = (
    _PROJECT_ROOT / "data" / "processed" / "report_claims_grafton.json"
)


_STATE_ABBREV = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def _expand_state(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    return _STATE_ABBREV.get(s.upper(), s)


def _normalize(raw: dict, sources_by_id: dict) -> ReportClaim:
    """Map a hand-authored claim dict onto our ReportClaim dataclass."""
    src = sources_by_id.get(raw.get("source_id") or "", {})
    source_document = src.get("source_name") or raw.get("source_id") or ""

    impacts = raw.get("infrastructure_impacts") or []
    if not isinstance(impacts, list):
        impacts = []

    # Compose location_name in the shape the Overture geocoder expects:
    #   "<landmark/asset>, <City>, <County> County, <State full name>"
    # The geocoder reads state from the last segment (full name only) and
    # finds counties via "<Name> County" anywhere in the string. The hand-
    # authored JSON uses 2-letter state codes ("IL") and stores county
    # separately, so we rewrite into the canonical shape.
    loc = (raw.get("location_name") or "").strip()
    county = (raw.get("location_county") or "").strip()
    state_full = _expand_state(raw.get("location_state"))

    # Drop a trailing 2-letter state code from loc so we don't end up with
    # "..., IL, Jersey County, Illinois".
    if state_full:
        loc = loc.rstrip()
        for tail in (f", {raw.get('location_state','')}", f" {raw.get('location_state','')}"):
            if tail.strip(", ") and loc.endswith(tail):
                loc = loc[: -len(tail)].rstrip(", ")
                break

    parts = [loc] if loc else []
    if county and county.lower() not in loc.lower():
        parts.append(county)
    if state_full and state_full.lower() not in loc.lower():
        parts.append(state_full)
    full_location = ", ".join(parts) if parts else loc

    return ReportClaim(
        claim_id=raw.get("claim_id") or "",
        source_document=source_document,
        source_type=raw.get("source_type") or src.get("source_type") or "news_report",
        location_name=full_location,
        damage_description=(raw.get("damage_description") or "").strip(),
        severity=raw.get("severity") or "moderate",
        damage_type=raw.get("damage_type") or "other",
        cost_estimate=raw.get("cost_estimate"),
        ef_rating=raw.get("ef_rating"),
        event_type=raw.get("event_type") or "tornado",
        event_name=raw.get("event_name"),
        event_date=raw.get("event_date"),
        report_date=raw.get("report_date"),
        building_type=raw.get("building_type"),
        building_name=raw.get("building_name"),
        structures_affected=raw.get("structures_affected"),
        infrastructure_impacts=[str(x) for x in impacts if x],
        is_valid=bool(raw.get("is_valid", True)),
        validation_errors=list(raw.get("validation_errors") or []),
    )


# Known Grafton IL coords for each claim. Aeries is ~1.5 mi NE up the bluffs.
_GRAFTON_COORDS: dict[str, tuple[float, float]] = {
    "rc-001": (38.9690, -90.4338),   # Drifters Eats and Drinks, Main St
    "rc-002": (38.9812, -90.4198),   # Aeries Resort and Winery
    "rc-003": (38.9691, -90.4340),   # Dees Riverside Retreat, next to Drifters
    "rc-004": (38.9680, -90.4320),   # Hotel/Motel, Grafton waterfront
    "rc-005": (38.9670, -90.4310),   # Residential, Grafton general
    "rc-006": (38.9676, -90.4318),   # Grafton general path centroid
}


def _apply_grafton_coords(claims: list[dict]) -> None:
    for c in claims:
        coords = _GRAFTON_COORDS.get(c.get("claim_id") or "")
        if coords:
            c["lat"], c["lon"] = coords
            c["geo_confidence"] = "high"
            c["geo_source"] = "manual_grafton"
        else:
            c["lat"] = c["lon"] = None
            c["geo_confidence"] = "unresolved"
            c["geo_source"] = "unresolved"


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(
        description="Normalize + geocode the hand-authored Grafton claims."
    )
    ap.add_argument("--in", dest="input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    if not args.input.is_file():
        print(f"Missing input: {args.input}", file=sys.stderr)
        return 1

    doc = json.loads(args.input.read_text(encoding="utf-8"))
    sources_by_id = {s["source_id"]: s for s in (doc.get("sources") or [])}
    raw_claims = doc.get("claims") or []

    print(f"Loaded {len(raw_claims)} hand-authored claim(s) "
          f"from {args.input.relative_to(_PROJECT_ROOT)}")

    claims = [_normalize(rc, sources_by_id) for rc in raw_claims]
    claim_dicts = [c.to_dict() for c in claims]

    # Grafton IL coords are known — skip Overture S3 round-trips.
    _apply_grafton_coords(claim_dicts)

    by_conf: dict[str, int] = {}
    for c in claim_dicts:
        by_conf[c.get("geo_confidence") or "unresolved"] = (
            by_conf.get(c.get("geo_confidence") or "unresolved", 0) + 1
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(claim_dicts, indent=2))
    print(f"\nWrote {len(claim_dicts)} claim(s) -> {args.out.relative_to(_PROJECT_ROOT)}")
    print("Geocode confidence:", dict(sorted(by_conf.items())))

    print("\nPreview")
    print("=======")
    for i, c in enumerate(claim_dicts, 1):
        latlon = (
            f"({c['lat']:.4f}, {c['lon']:.4f})"
            if c.get("lat") is not None else "(unresolved)"
        )
        print(
            f"  {i:>2}. {c['claim_id']:<8} "
            f"{c['damage_type']:<22} {c['severity']:<10} "
            f"{latlon:<22} [{c.get('geo_source')}]  "
            f"{(c.get('building_name') or c['location_name'])[:60]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
