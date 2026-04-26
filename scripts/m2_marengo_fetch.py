"""
M2 step 2.4 (continued) — fetch Marengo async output from S3.

Reads the job record written by `m2_marengo_embed.py`, downloads the
embedding output.json, normalizes each row into a VideoSegment dict, and
writes `data/processed/video_segments.json`.

This is the consumer side of `start_video_embedding` — assumes the job
has already completed (status = `Completed`). It will raise loudly if the
output object is not yet in S3.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.video_pipeline.ingest import fetch_video_embeddings


JOB_PATH = Path("data/processed/marengo_job.json")
OUT_PATH = Path("data/processed/video_segments.json")


def main() -> int:
    load_dotenv()

    if not JOB_PATH.is_file():
        print(f"Missing {JOB_PATH} — run m2_marengo_embed.py first.")
        return 1

    job = json.loads(JOB_PATH.read_text())
    print(f"Job ARN: {job['invocation_arn']}")

    result = fetch_video_embeddings(job)

    # Quick stats so the run is self-documenting in the terminal.
    n_segs = result["segment_count"]
    dim = result["embedding_dim"]
    modes = result["modalities"]
    rows = result["segments"]
    span = max(s["end_sec"] for s in rows) if rows else 0.0
    print(
        f"Parsed {len(rows)} rows = {n_segs} segments x {len(modes)} modalities "
        f"({modes}). Embedding dim = {dim}. Span = 0 -> {span:.1f}s."
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2))
    size_mb = OUT_PATH.stat().st_size / 1_000_000
    print(f"Wrote {OUT_PATH} ({size_mb:.1f} MB).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
