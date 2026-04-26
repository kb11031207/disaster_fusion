"""
M4+M5 (Grafton) — Full fusion pipeline for the Grafton EF-1 tornado.

Steps:
  1. Start Marengo async embedding for the Grafton video.
  2. Poll until the job is complete (polls every 30 s).
  3. Fetch + normalise the segment embeddings.
  4. Pass A: embed Grafton report claims -> cosine vs video segments.
  5. Pass B: embed finding descriptions -> text-text cosine matrix,
     run tiered fusion, write fused_findings_grafton.json.

Usage
=====
    python scripts/m4_m5_grafton_fusion.py

Set environment variables in .env:
    AWS_REGION, S3_BUCKET, S3_VIDEOS_PATH, MARENGO_MODEL_ID (optional)
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

from src.video_pipeline.ingest import (  # noqa: E402
    fetch_video_embeddings,
    start_video_embedding,
)
from src.fusion.pass_a import run_pass_a  # noqa: E402
from src.fusion.pass_b import fuse, text_similarity_matrix  # noqa: E402
from src.fusion.text_embed import embed_texts  # noqa: E402


VIDEO_FILENAME  = "grafton_tornado_ef1.mp4"
FINDINGS_PATH   = Path("data/processed/video_findings_grafton_geo.json")
CLAIMS_PATH     = Path("data/processed/report_claims_grafton.json")
JOB_PATH        = Path("data/processed/marengo_job_grafton.json")
SEGMENTS_PATH   = Path("data/processed/video_segments_grafton.json")
PASS_A_OUT      = Path("data/processed/pass_a_matches_grafton.json")
FUSED_OUT       = Path("data/processed/fused_findings_grafton.json")

POLL_INTERVAL_S = 30
MIN_SCORE = {"visual": 0.05, "audio": 0.03}
TOP_K     = 5


# ---------------------------------------------------------------------------
# Text builders
# ---------------------------------------------------------------------------

def _finding_text(f: dict) -> str:
    parts: list[str] = []
    if f.get("damage_type"):
        parts.append(f["damage_type"].replace("_", " "))
    if f.get("damage_description"):
        parts.append(f["damage_description"])
    return ". ".join(parts).strip() or f.get("finding_id", "")


def _claim_text(c: dict) -> str:
    parts: list[str] = []
    if c.get("damage_type"):
        parts.append(c["damage_type"].replace("_", " "))
    if c.get("damage_description"):
        parts.append(c["damage_description"])
    return ". ".join(parts).strip() or c.get("claim_id", "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv()

    for p in (FINDINGS_PATH, CLAIMS_PATH):
        if not p.is_file():
            print(f"Missing {p}", file=sys.stderr)
            return 1

    findings = json.loads(FINDINGS_PATH.read_text())
    claims   = json.loads(CLAIMS_PATH.read_text())
    print(f"Findings: {len(findings)}   Claims: {len(claims)}")

    # ---- Step 1+2+3: Marengo async embed --------------------------------
    bucket        = os.environ["S3_BUCKET"]
    videos_prefix = os.environ.get("S3_VIDEOS_PATH", "videos").strip("/")
    s3_uri        = f"s3://{bucket}/{videos_prefix}/{VIDEO_FILENAME}"

    if JOB_PATH.is_file():
        print(f"\nFound existing Marengo job at {JOB_PATH}. Skipping submit.")
        job = json.loads(JOB_PATH.read_text())
    else:
        print(f"\nStep 1 — Start Marengo async embedding for {VIDEO_FILENAME}")
        job = start_video_embedding(
            s3_uri=s3_uri,
            source_video=VIDEO_FILENAME,
            embedding_options=["visual", "audio"],
        )
        JOB_PATH.parent.mkdir(parents=True, exist_ok=True)
        JOB_PATH.write_text(json.dumps(job, indent=2))
        print(f"Job ARN: {job['invocation_arn']}")

    if SEGMENTS_PATH.is_file():
        print(f"Found cached segments at {SEGMENTS_PATH}. Skipping fetch.")
        seg_doc = json.loads(SEGMENTS_PATH.read_text())
    else:
        print("\nStep 2 — Polling for Marengo completion...")
        import boto3
        bedrock_rt = boto3.client("bedrock-runtime",
                                  region_name=os.environ.get("AWS_REGION", "us-east-1"))
        while True:
            resp   = bedrock_rt.get_async_invoke(
                invocationArn=job["invocation_arn"]
            )
            status = resp["status"]
            print(f"  status: {status}")
            if status == "Completed":
                break
            if status in ("Failed", "Cancelled"):
                print(f"Job {status}.", file=sys.stderr)
                return 1
            time.sleep(POLL_INTERVAL_S)

        print("\nStep 3 — Fetch + normalise Marengo output...")
        seg_doc = fetch_video_embeddings(job)
        SEGMENTS_PATH.write_text(json.dumps(seg_doc, indent=2))
        n = seg_doc["segment_count"]
        dim = seg_doc["embedding_dim"]
        print(f"  {n} segments, dim={dim}, span=0-{max(s['end_sec'] for s in seg_doc['segments']):.0f}s")

    # ---- Step 4: Pass A -------------------------------------------------
    print("\nStep 4 — Pass A (claim text vs Marengo video segments)")
    texts_a = [_claim_text(c) for c in claims]
    print(f"  Embedding {len(texts_a)} claim strings...")
    t0 = time.time()
    claim_vecs = embed_texts(texts_a)
    print(f"  Got {len(claim_vecs)} vecs in {time.time()-t0:.1f}s")

    pass_a_result = run_pass_a(
        claim_vecs, claims, seg_doc, min_score=MIN_SCORE, top_k=TOP_K
    )
    PASS_A_OUT.write_text(json.dumps(pass_a_result, indent=2))
    print(f"  Written -> {PASS_A_OUT}")

    # ---- Step 5: Pass B -------------------------------------------------
    print("\nStep 5 — Pass B (tiered fusion)")
    f_texts = [_finding_text(f) for f in findings]
    c_texts = [_claim_text(c)   for c in claims]
    print(f"  Embedding {len(f_texts)} findings + {len(c_texts)} claims...")
    t0 = time.time()
    f_vecs = embed_texts(f_texts)
    c_vecs = embed_texts(c_texts)
    print(f"  Done in {time.time()-t0:.1f}s")

    import numpy as np
    sim_matrix = text_similarity_matrix(f_vecs, c_vecs)
    fused_doc  = fuse(findings, claims, sim_matrix, pass_a_doc=pass_a_result)
    FUSED_OUT.write_text(json.dumps(fused_doc, indent=2))

    s = fused_doc["stats"]
    print(f"\n{'='*50}")
    print(f"  corroborated : {s['corroborated']}")
    print(f"  discrepancy  : {s['discrepancy']}")
    print(f"  unverified   : {s['unverified']}")
    print(f"  unreported   : {s['unreported']}")
    print(f"{'='*50}")
    print(f"\nWritten -> {FUSED_OUT}")

    # Quick preview of corroborated + discrepancy
    print("\nCorroborated / Discrepancy rows:")
    for row in fused_doc["findings"]:
        if row["classification"] not in ("corroborated", "discrepancy"):
            continue
        rc = row.get("report_claim") or {}
        vf = row.get("video_finding") or {}
        nm = (
            rc.get("building_name")
            or vf.get("building_name")
            or rc.get("location_name", "")[:40]
        )
        print(
            f"  [{row['classification'][:4]}] "
            f"score={row['confidence_score']:.2f}  "
            f"name_match={row['confidence_breakdown'].get('name_match', 0):.2f}  "
            f"{nm}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
