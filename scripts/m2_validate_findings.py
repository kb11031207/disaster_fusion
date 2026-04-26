"""
M2 step 2.3 — wrap raw Pegasus output into VideoFinding dataclasses
and write data/processed/video_findings.json.

This script does NOT call Bedrock. It just reads the raw response we
already saved in 2.2 and converts it to our pipeline's contract shape.

Run from the project root:
    python scripts/m2_validate_findings.py
"""

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.video_pipeline.validation import validate_findings  # noqa: E402


def main() -> int:
    processed_dir = _PROJECT_ROOT / "data" / "processed"
    raw_path = processed_dir / "pegasus_raw.json"
    out_path = processed_dir / "video_findings.json"

    if not raw_path.is_file():
        print(f"Missing {raw_path}. Run m2_pegasus_analyze.py first.")
        return 2

    raw = json.loads(raw_path.read_text())

    # These two pieces of metadata aren't in pegasus_raw.json, so we
    # hardcode them here. They mirror what m2_upload_video.py used.
    source_video = "katrina_tv_coverage_2005.mp4"
    capture_date = "2005-08-29"

    findings = validate_findings(raw, source_video, capture_date)

    valid_count = sum(1 for f in findings if f.is_valid)
    flagged_count = len(findings) - valid_count

    out_path.write_text(
        json.dumps([f.to_dict() for f in findings], indent=2)
    )

    print()
    print(f"Validated {len(findings)} finding(s):")
    print(f"  valid:   {valid_count}")
    print(f"  flagged: {flagged_count}")
    print(f"Written to: {out_path.relative_to(_PROJECT_ROOT)}")
    print()

    if flagged_count:
        print("Flagged findings:")
        for f in findings:
            if not f.is_valid:
                print(f"  {f.finding_id}: {f.validation_errors}")
        print()

    # Quick preview of the first finding so we can eyeball the schema.
    if findings:
        print("First finding (as dict):")
        print(json.dumps(findings[0].to_dict(), indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
