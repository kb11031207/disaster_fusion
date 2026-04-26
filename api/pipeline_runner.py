"""
Background pipeline runner. Called in a thread per job.

Steps:
  1. Write video bytes to a temp file, upload to S3
  2. Pegasus analysis
  3. Validate + geolocate findings
  4. Parse report text with Claude (handles plain text, PDF, DOCX)
  5. Marengo async embed → Pass A
  6. Pass B fusion
  7. Frontend schema transform
  8. Overture GeoJSON for the findings bbox
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import boto3
import numpy as np

from api.job_store import Job


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_POLL_INTERVAL = 30
_TOP_K = 5
_MIN_SCORE = {"visual": 0.05, "audio": 0.03}


def _set(job: Job, progress: str, status: str = "running") -> None:
    job.status   = status
    job.progress = progress
    print(f"[{job.job_id}] {progress}")


def _finding_text(f: dict) -> str:
    parts = []
    if f.get("damage_type"):
        parts.append(f["damage_type"].replace("_", " "))
    if f.get("damage_description"):
        parts.append(f["damage_description"])
    return ". ".join(parts).strip() or f.get("finding_id", "")


def _claim_text(c: dict) -> str:
    parts = []
    if c.get("damage_type"):
        parts.append(c["damage_type"].replace("_", " "))
    if c.get("damage_description"):
        parts.append(c["damage_description"])
    return ". ".join(parts).strip() or c.get("claim_id", "")


def _extract_text_from_bytes(filename: str, content: bytes) -> str:
    """Extract plain text from uploaded file regardless of format."""
    name = filename.lower()

    if name.endswith(".pdf"):
        # Claude on Bedrock handles PDF natively — pass raw bytes via document block.
        # We return a sentinel so the parser knows to use the bytes directly.
        return content.decode("latin-1", errors="replace")

    if name.endswith(".docx"):
        import zipfile, re
        TAG_RE  = re.compile(r"<[^>]+>")
        PARA_RE = re.compile(r"</w:p>")
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            with zipfile.ZipFile(tmp_path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            xml  = PARA_RE.sub("\n", xml)
            text = TAG_RE.sub("", xml)
            return text.strip()
        finally:
            os.unlink(tmp_path)

    # CSV or plain text — decode as UTF-8
    return content.decode("utf-8", errors="replace")


def _bbox_from_findings(findings: list[dict]) -> tuple[float, float, float, float]:
    lats = [f["lat"] for f in findings if f.get("lat") is not None]
    lons = [f["lon"] for f in findings if f.get("lon") is not None]
    pad  = 0.01
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)


def run_pipeline(
    job:             Job,
    video_filename:  str,
    video_bytes:     bytes,
    report_filename: str,
    report_bytes:    bytes,
    location_hint:   str,
) -> None:
    try:
        _run(job, video_filename, video_bytes, report_filename, report_bytes, location_hint)
    except Exception as exc:
        import traceback
        job.status  = "failed"
        job.error   = str(exc)
        job.progress = f"Failed: {exc}"
        print(f"[{job.job_id}] FAILED: {traceback.format_exc()}")


def _run(
    job:             Job,
    video_filename:  str,
    video_bytes:     bytes,
    report_filename: str,
    report_bytes:    bytes,
    location_hint:   str,
) -> None:
    from src.video_pipeline.ingest        import upload_video, start_video_embedding, fetch_video_embeddings
    from src.video_pipeline.pegasus_analysis import analyze_video
    from src.video_pipeline.validation    import validate_findings
    from src.video_pipeline.geo_simulator import geolocate_findings
    from src.report_parser.parser         import parse_report
    from src.fusion.pass_a                import run_pass_a
    from src.fusion.pass_b                import fuse, text_similarity_matrix
    from src.fusion.text_embed            import embed_texts
    from src.output.frontend_schema       import transform

    bucket        = os.environ["S3_BUCKET"]
    videos_prefix = os.environ.get("S3_VIDEOS_PATH", "videos").strip("/")
    region        = os.environ.get("AWS_REGION", "us-east-1")

    # ---- 1. Upload video to S3 ------------------------------------------
    _set(job, "Uploading video to S3...")
    with tempfile.NamedTemporaryFile(suffix=Path(video_filename).suffix, delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = Path(tmp.name)
    try:
        upload_video(tmp_path, capture_date=time.strftime("%Y-%m-%d"))
    finally:
        tmp_path.unlink(missing_ok=True)

    s3_uri = f"s3://{bucket}/{videos_prefix}/{video_filename}"

    # ---- 2. Pegasus analysis --------------------------------------------
    _set(job, "Running Pegasus video analysis (this takes a few minutes)...")
    raw = analyze_video(s3_uri, disaster_type="tornado")

    # ---- 3. Validate + geolocate ----------------------------------------
    _set(job, "Validating findings and geolocating...")
    findings_objs = validate_findings(raw, video_filename, time.strftime("%Y-%m-%d"))
    finding_dicts = [f.to_dict() for f in findings_objs]
    finding_dicts, zone = geolocate_findings(finding_dicts, hint=location_hint or None)

    # ---- 4. Parse report ------------------------------------------------
    _set(job, "Parsing damage report with Claude...")
    report_text = _extract_text_from_bytes(report_filename, report_bytes)
    claims = parse_report(report_text)
    claim_dicts = [c.to_dict() for c in claims]

    if not claim_dicts:
        job.progress = "Warning: no claims extracted from report — continuing with video only."

    # ---- 5. Marengo async embed -----------------------------------------
    _set(job, "Starting Marengo video embedding...")
    embed_job = start_video_embedding(
        s3_uri=s3_uri,
        source_video=video_filename,
        embedding_options=["visual", "audio"],
    )

    _set(job, "Waiting for Marengo embedding to complete...")
    bedrock_rt = boto3.client("bedrock-runtime", region_name=region)
    while True:
        resp   = bedrock_rt.get_async_invoke(invocationArn=embed_job["invocation_arn"])
        status = resp["status"]
        if status == "Completed":
            break
        if status in ("Failed", "Cancelled"):
            raise RuntimeError(f"Marengo job {status}")
        time.sleep(_POLL_INTERVAL)

    _set(job, "Fetching Marengo segment embeddings...")
    seg_doc = fetch_video_embeddings(embed_job)

    # ---- 6. Pass A ------------------------------------------------------
    _set(job, "Running Pass A (claim-to-video alignment)...")
    claim_texts = [_claim_text(c) for c in claim_dicts]
    claim_vecs  = embed_texts(claim_texts) if claim_texts else []
    pass_a_doc  = run_pass_a(claim_vecs, claim_dicts, seg_doc,
                              min_score=_MIN_SCORE, top_k=_TOP_K) if claim_vecs else {"matches": []}

    # ---- 7. Pass B fusion -----------------------------------------------
    _set(job, "Running Pass B fusion...")
    f_texts = [_finding_text(f) for f in finding_dicts]
    c_texts = [_claim_text(c)   for c in claim_dicts]
    f_vecs  = embed_texts(f_texts)
    c_vecs  = embed_texts(c_texts) if c_texts else []

    if c_vecs:
        sim_matrix = text_similarity_matrix(f_vecs, c_vecs)
    else:
        sim_matrix = np.zeros((len(f_vecs), 0))

    fused_doc = fuse(finding_dicts, claim_dicts, sim_matrix, pass_a_doc=pass_a_doc)

    # ---- 8. Frontend transform ------------------------------------------
    _set(job, "Building frontend response...")
    result = transform(fused_doc)
    job.results = result

    # ---- 9. Overture GeoJSON for this event's bbox ----------------------
    if result["findings"]:
        _set(job, "Fetching Overture map reference data...")
        try:
            from scripts.m6_overture_geojson import query_places, query_buildings, query_roads, _connect
            min_lat, min_lon, max_lat, max_lon = _bbox_from_findings(result["findings"])
            con      = _connect()
            features = (
                query_places(con, min_lat, min_lon, max_lat, max_lon)
                + query_buildings(con, min_lat, min_lon, max_lat, max_lon)
                + query_roads(con, min_lat, min_lon, max_lat, max_lon)
            )
            job.overture = {"type": "FeatureCollection", "features": features}
        except Exception as e:
            print(f"[{job.job_id}] Overture query failed (non-fatal): {e}")
            job.overture = {"type": "FeatureCollection", "features": []}

    _set(job, f"Done — {len(result['findings'])} findings", status="done")
