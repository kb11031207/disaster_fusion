"""
M2 step 2.1 — upload the Katrina NOAA GOES-12 video to S3.

Run from the project root:
    python scripts/m2_upload_video.py
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

# Make `src` importable when the script is launched directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.video_pipeline.ingest import upload_video  # noqa: E402


def main() -> int:
    load_dotenv()

    video_path = (
        _PROJECT_ROOT
        / "data"
        / "raw"
        / "videos"
        / "katrina_tv_coverage_2005.mp4"
    )
    # TV news coverage of Hurricane Katrina landfall/aftermath, late Aug 2005.
    capture_date = "2005-08-29"

    result = upload_video(video_path, capture_date)

    print()
    print("Upload complete.")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
