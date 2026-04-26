"""
M3 step 3.4 — geocode ReportClaims via Overture Maps.

Reads `data/processed/report_claims.json` (output of m3_parse_reports.py),
fills in lat/lon/county_parish/state/geo_confidence/geo_source on each
row using Overture Maps via DuckDB, and writes the file back in place.

Run after the parser. Idempotent — re-running just re-geocodes.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report_parser.geocoder import geocode_claims


CLAIMS_PATH = Path("data/processed/report_claims.json")


def main() -> int:
    if not CLAIMS_PATH.is_file():
        print(f"Missing {CLAIMS_PATH} — run m3_parse_reports.py first.")
        return 1

    claims = json.loads(CLAIMS_PATH.read_text())
    print(f"Loaded {len(claims)} claims from {CLAIMS_PATH}")

    geocode_claims(claims)

    CLAIMS_PATH.write_text(json.dumps(claims, indent=2))
    size_kb = CLAIMS_PATH.stat().st_size / 1024
    print(f"Wrote {CLAIMS_PATH} ({size_kb:.0f} KB).")

    # Per-claim summary so the run is self-documenting.
    print()
    print("Geocoding outcomes:")
    by_conf: Counter[str] = Counter(c.get("geo_confidence") or "?" for c in claims)
    for k, v in by_conf.most_common():
        print(f"  {k:11s}  {v}")
    print()
    print("Per-claim detail:")
    for c in claims:
        loc = c.get("location_name", "")[:55]
        lat = c.get("lat"); lon = c.get("lon")
        ll = f"{lat:.4f},{lon:.4f}" if lat is not None and lon is not None else "—"
        print(
            f"  [{c['claim_id']}] "
            f"{c.get('geo_confidence','?'):11s} "
            f"{c.get('geo_source','?'):20s} "
            f"{ll:20s} "
            f"{loc}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
