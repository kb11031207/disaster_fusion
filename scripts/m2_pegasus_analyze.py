"""
M2 step 2.2 — run Pegasus on the uploaded Katrina video.

Saves the raw response to data/processed/pegasus_raw.json so we can
re-inspect without re-burning a Bedrock call. Validation happens in 2.3.

Run from the project root:
    python scripts/m2_pegasus_analyze.py
"""

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.video_pipeline.pegasus_analysis import analyze_video  # noqa: E402


def main() -> int:
    load_dotenv()

    bucket = os.environ["S3_BUCKET"]
    videos_prefix = os.environ.get("S3_VIDEOS_PATH", "videos").strip("/")
    video_filename = "katrina_tv_coverage_2005.mp4"
    s3_uri = f"s3://{bucket}/{videos_prefix}/{video_filename}"
    disaster_type = "hurricane"

    out_dir = _PROJECT_ROOT / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_out = out_dir / "pegasus_raw.json"

    t0 = time.time()
    result = analyze_video(s3_uri, disaster_type)
    elapsed = time.time() - t0

    findings = result.get("findings", [])
    raw_out.write_text(json.dumps(result, indent=2))

    print()
    print(f"Pegasus returned {len(findings)} finding(s) in {elapsed:.1f}s.")
    print(f"Raw response saved to: {raw_out.relative_to(_PROJECT_ROOT)}")
    print()

    # Brief preview so we can eyeball quality without opening the file.
    for i, f in enumerate(findings[:5], 1):
        print(f"--- Finding {i} ---")
        print(f"  damage_type:  {f.get('damage_type')}")
        print(f"  severity:     {f.get('severity')}")
        print(f"  description:  {f.get('description', '')[:240]}")
        print(f"  structures:   {f.get('structures_affected')}")
        print(f"  building:     {f.get('building_type')}")
        if f.get("infrastructure_impacts"):
            print(f"  infra:        {f.get('infrastructure_impacts')}")
        if f.get("location_indicators"):
            print(f"  loc indicators:{f.get('location_indicators')}")
        print()

    if len(findings) > 5:
        print(f"... {len(findings) - 5} more finding(s) in {raw_out.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
