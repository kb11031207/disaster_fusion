"""
M2 step 2.4 — fire off Marengo async embedding for the uploaded video.

This script returns immediately. Marengo runs server-side; we save the
invocation ARN to data/processed/marengo_job.json so a follow-up
script can poll for completion and fetch the embedding output.

Run from the project root:
    python scripts/m2_marengo_embed.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.video_pipeline.ingest import start_video_embedding  # noqa: E402


def main() -> int:
    load_dotenv()

    bucket = os.environ["S3_BUCKET"]
    videos_prefix = os.environ.get("S3_VIDEOS_PATH", "videos").strip("/")
    source_video = "katrina_tv_coverage_2005.mp4"
    s3_uri = f"s3://{bucket}/{videos_prefix}/{source_video}"

    job = start_video_embedding(
        s3_uri=s3_uri,
        source_video=source_video,
        embedding_options=["visual", "audio"],
    )
    job["started_at"] = datetime.now(timezone.utc).isoformat()

    out_dir = _PROJECT_ROOT / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "marengo_job.json"
    out_path.write_text(json.dumps(job, indent=2))

    print()
    print(f"Job record saved to: {out_path.relative_to(_PROJECT_ROOT)}")
    print()
    print("Marengo is running asynchronously on Bedrock. Poll later with")
    print("a status check (next step). Typical wait: a few minutes for a")
    print("video this size.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
