"""
M2 — Pegasus pipeline for the Grafton EF-1 tornado video.

End-to-end so we can eyeball Pegasus's behaviour on a different disaster
type (tornado vs the Katrina hurricane footage):

  1. Upload `data/raw/videos/grafton_tornado_ef1.mp4` to S3.
  2. Run Pegasus 1.2 with disaster_type="tornado".
  3. Save raw response to data/processed/pegasus_raw_grafton.json.
  4. Validate -> data/processed/video_findings_grafton.json.

Outputs are suffixed `_grafton` so the existing Katrina artifacts stay in
place — until we decide whether the new video replaces or supplements
the demo set.

`CAPTURE_DATE` below is a placeholder. It only feeds the VideoFinding
metadata; the Pegasus call itself doesn't use it. Re-run step 4 with
the right date once it's confirmed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.video_pipeline.ingest import upload_video  # noqa: E402
from src.video_pipeline.pegasus_analysis import analyze_video  # noqa: E402
from src.video_pipeline.validation import validate_findings  # noqa: E402
from src.video_pipeline.geo_simulator import geolocate_findings  # noqa: E402


VIDEO_FILENAME = "grafton_tornado_ef1.mp4"
DISASTER_TYPE  = "tornado"
CAPTURE_DATE   = "2026-04-26"  # placeholder — confirm and re-run validation


def main() -> int:
    load_dotenv()

    video_path = _PROJECT_ROOT / "data" / "raw" / "videos" / VIDEO_FILENAME
    if not video_path.is_file():
        print(f"Missing {video_path}")
        return 1

    out_dir  = _PROJECT_ROOT / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_out      = out_dir / "pegasus_raw_grafton.json"
    findings_out = out_dir / "video_findings_grafton.json"

    # ---- 1. upload ------------------------------------------------------
    print("=" * 60)
    print("STEP 1 — Upload to S3")
    print("=" * 60)
    upload_info = upload_video(video_path, CAPTURE_DATE)
    print(json.dumps(upload_info, indent=2))

    # ---- 2. Pegasus analyze --------------------------------------------
    print()
    print("=" * 60)
    print("STEP 2 — Pegasus 1.2")
    print("=" * 60)
    bucket = os.environ["S3_BUCKET"]
    videos_prefix = os.environ.get("S3_VIDEOS_PATH", "videos").strip("/")
    s3_uri = f"s3://{bucket}/{videos_prefix}/{VIDEO_FILENAME}"

    t0 = time.time()
    raw = analyze_video(s3_uri, DISASTER_TYPE)
    elapsed = time.time() - t0

    findings_raw = raw.get("findings", [])
    raw_out.write_text(json.dumps(raw, indent=2))
    print(f"Pegasus returned {len(findings_raw)} finding(s) in {elapsed:.1f}s.")
    print(f"Raw saved -> {raw_out.relative_to(_PROJECT_ROOT)}")

    # ---- 3. validate ----------------------------------------------------
    print()
    print("=" * 60)
    print("STEP 3 — Validate")
    print("=" * 60)
    findings = validate_findings(raw, VIDEO_FILENAME, CAPTURE_DATE)
    valid_count = sum(1 for f in findings if f.is_valid)
    flagged = [f for f in findings if not f.is_valid]
    findings_out.write_text(json.dumps([f.to_dict() for f in findings], indent=2))
    print(f"Valid: {valid_count}/{len(findings)}  Flagged: {len(flagged)}")
    print(f"Saved -> {findings_out.relative_to(_PROJECT_ROOT)}")
    for f in flagged:
        print(f"  - {f.finding_id}: {f.validation_errors}")

    # ---- 4. geo simulation ----------------------------------------------
    print()
    print("=" * 60)
    print("STEP 4 — Geo simulation (YouTube clip has no GPS)")
    print("=" * 60)
    geo_out = out_dir / "video_findings_grafton_geo.json"
    zone_out = out_dir / "disaster_zone_grafton.json"
    finding_dicts = [f.to_dict() for f in findings]
    # Grafton IL centroid hardcoded — Claude's geocoder misidentifies this
    # small town because the video descriptions don't mention the place name.
    GRAFTON_CENTRE = (38.9676, -90.4318)
    finding_dicts, zone = geolocate_findings(
        finding_dicts,
        hint="Grafton, Illinois",
        centre_override=GRAFTON_CENTRE,
        seed=42,
    )
    geo_out.write_text(json.dumps(finding_dicts, indent=2))
    zone_out.write_text(json.dumps(zone, indent=2))
    print(f"Zone: {zone['primary_location']}  centre={zone['centre']}")
    print(f"Saved geo findings -> {geo_out.relative_to(_PROJECT_ROOT)}")

    # ---- 5. preview -----------------------------------------------------
    print()
    print("=" * 60)
    print("STEP 5 — Preview findings")
    print("=" * 60)
    for i, d in enumerate(finding_dicts, 1):
        print(f"--- Finding {i} ({d.get('finding_id')}) ---")
        print(f"  damage_type:   {d.get('damage_type')}")
        print(f"  severity:      {d.get('severity')}")
        print(f"  building_type: {d.get('building_type')}")
        if d.get("building_name"):
            print(f"  building_name: {d.get('building_name')}")
        if d.get("named_entities"):
            print(f"  named_entities:{d.get('named_entities')}")
        print(f"  evidence_qual: {d.get('visual_evidence_quality')}")
        print(f"  description:   {(d.get('damage_description') or '')[:280]}")
        print(f"  geo:           {d.get('geo')}")
        if d.get("infrastructure_impacts"):
            print(f"  infra_impacts: {d.get('infrastructure_impacts')}")
        if d.get("location_indicators"):
            print(f"  loc_indicators:{d.get('location_indicators')}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
