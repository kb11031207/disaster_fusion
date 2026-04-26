"""
M5.5 — Run the video-finding geo simulator on a video_findings JSON.

Pegasus has no GPS to work from on stripped-metadata footage, so this
script asks Claude (Bedrock) to identify the disaster location and
scatters plausible coords inside the disaster zone. Output is written
to a NEW file alongside the input — the raw Pegasus artifact stays
intact for debugging.

Usage
=====
    python scripts/m5_5_simulate_geo.py
    python scripts/m5_5_simulate_geo.py --findings data/processed/video_findings.json
    python scripts/m5_5_simulate_geo.py --hint "Grafton, Illinois"

Defaults
========
- input:     data/processed/video_findings_grafton.json
- output:    <input_stem>_geo.json next to the input
- zone-out:  disaster_zone_<input_stem>.json next to the input
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.video_pipeline.geo_simulator import geolocate_findings  # noqa: E402


DEFAULT_INPUT = (
    _PROJECT_ROOT / "data" / "processed" / "video_findings_grafton.json"
)


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser(
        description="Simulate coordinates for Pegasus video findings."
    )
    ap.add_argument(
        "--findings", type=Path, default=DEFAULT_INPUT,
        help="Path to a video_findings JSON (default: Grafton).",
    )
    ap.add_argument(
        "--out", type=Path, default=None,
        help="Output path (default: <stem>_geo.json next to input).",
    )
    ap.add_argument(
        "--zone-out", type=Path, default=None,
        help="Zone summary path (default: disaster_zone_<stem>.json).",
    )
    ap.add_argument(
        "--hint", type=str, default=None,
        help='Optional location override, e.g. "Grafton, Illinois".',
    )
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if not args.findings.is_file():
        print(f"Missing input: {args.findings}", file=sys.stderr)
        return 1

    out_dir   = args.findings.parent
    stem      = args.findings.stem
    out_path  = args.out      or out_dir / f"{stem}_geo.json"
    zone_path = args.zone_out or out_dir / f"disaster_zone_{stem}.json"

    findings = json.loads(args.findings.read_text())
    print(
        f"Loaded {len(findings)} finding(s) from "
        f"{args.findings.relative_to(_PROJECT_ROOT)}"
    )
    if args.hint:
        print(f"Operator hint: {args.hint!r}")

    findings, zone = geolocate_findings(
        findings, hint=args.hint, seed=args.seed
    )

    out_path.write_text(json.dumps(findings, indent=2))
    zone_path.write_text(json.dumps(zone, indent=2))

    print()
    print("Disaster zone")
    print("=============")
    print(f"  primary_location:      {zone['primary_location']}")
    print(f"  centre:                {zone['centre']}")
    print(
        f"  area_type:             {zone['area_type']}  "
        f"(spread {zone['spread_lat']} x {zone['spread_lon']} deg)"
    )
    lm_names = [lm["name"] for lm in zone["landmarks"]]
    print(f"  landmarks ({len(lm_names)}):       {lm_names}")
    print(
        f"  extraction_confidence: {zone['extraction_confidence']}"
        f"{'  (operator hint applied)' if zone['hint'] else ''}"
    )
    print()
    print(f"Wrote {len(findings)} finding(s) -> {out_path.relative_to(_PROJECT_ROOT)}")
    print(f"Wrote zone summary           -> {zone_path.relative_to(_PROJECT_ROOT)}")

    print()
    print("Findings preview")
    print("================")
    for i, f in enumerate(findings, 1):
        fid = f.get("finding_id", "?")
        print(
            f"  {i:>2}. {fid:<14} geo={f.get('geo')}  "
            f"{(f.get('description') or '')[:80]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
