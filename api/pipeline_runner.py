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
import re
import tempfile
import time
from pathlib import Path


def _safe_filename(name: str) -> str:
    """Strip characters that Bedrock's async-invoke S3 fetch chokes on."""
    stem = Path(name).stem
    suffix = Path(name).suffix
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "video"
    return f"{stem}{suffix}"

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


def _bbox_from_coords(*sources: list[dict]) -> tuple[float, float, float, float] | None:
    """Build a padded bbox from the first source that has any lat/lon."""
    for items in sources:
        lats = [x["lat"] for x in items if x.get("lat") is not None]
        lons = [x["lon"] for x in items if x.get("lon") is not None]
        if lats and lons:
            pad = 0.01
            return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
    return None


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
    from src.video_pipeline.ingest        import upload_video, start_video_embedding, fetch_video_embeddings, generate_presigned_url
    from src.video_pipeline.pegasus_analysis import analyze_video
    from src.video_pipeline.validation    import validate_findings
    from src.video_pipeline.geo_simulator import geolocate_findings
    from src.report_parser.parser         import parse_report
    from src.report_parser.geocoder       import geocode_claims
    from src.fusion.pass_a                import run_pass_a
    from src.fusion.pass_b                import fuse, text_similarity_matrix
    from src.fusion.text_embed            import embed_texts
    from src.output.frontend_schema       import transform
    from src.output.alerts                import check_and_alert

    bucket        = os.environ["S3_BUCKET"]
    videos_prefix = os.environ.get("S3_VIDEOS_PATH", "videos").strip("/")
    region        = os.environ.get("AWS_REGION", "us-east-1")

    # ---- 1. Upload video to S3 ------------------------------------------
    _set(job, "Uploading video to S3...")
    safe_name = _safe_filename(video_filename)
    if safe_name != video_filename:
        print(f"[{job.job_id}] sanitized filename: {video_filename!r} -> {safe_name!r}")
    video_filename = safe_name
    tmp_dir  = Path(tempfile.mkdtemp())
    tmp_path = tmp_dir / video_filename
    try:
        tmp_path.write_bytes(video_bytes)
        upload_info = upload_video(tmp_path, capture_date=time.strftime("%Y-%m-%d"))
    finally:
        tmp_path.unlink(missing_ok=True)
        tmp_dir.rmdir()

    s3_uri = upload_info["s3_uri"]

    # ---- 2. Pegasus analysis --------------------------------------------
    _set(job, "Running Pegasus video analysis (this takes a few minutes)...")
    raw = analyze_video(s3_uri, disaster_type="tornado")

    # ---- 3. Validate + geolocate ----------------------------------------
    _set(job, "Validating findings and geolocating...")
    findings_objs = validate_findings(raw, video_filename, time.strftime("%Y-%m-%d"))
    finding_dicts = [f.to_dict() for f in findings_objs]

    # Try to geocode the operator hint via Overture so we never depend on
    # Claude's flaky guess for the centre when an authoritative place name
    # is already on hand.
    centre_override = None
    if location_hint:
        try:
            hint_hit = geocode_claims([{"location_name": location_hint}])
            if hint_hit and hint_hit[0].get("lat") is not None:
                centre_override = (hint_hit[0]["lat"], hint_hit[0]["lon"])
                print(f"[{job.job_id}] hint geocoded via Overture: {centre_override}")
        except Exception as e:
            print(f"[{job.job_id}] hint geocode failed (non-fatal): {e}")

    try:
        finding_dicts, zone = geolocate_findings(
            finding_dicts,
            hint=location_hint or None,
            centre_override=centre_override,
        )
        centre = zone.get("centre")
    except Exception as e:
        print(f"[{job.job_id}] geolocate_findings failed (non-fatal): {e}")
        centre = None
        zone = {"primary_location": location_hint, "centre": None, "method": "failed"}

    # Pin all findings to the centre (no random scatter). If centre is missing,
    # findings stay coord-less and will get coords from geocoded report claims
    # during fusion.
    if centre and len(centre) == 2 and centre[0] is not None and centre[1] is not None:
        for f in finding_dicts:
            f["lat"] = centre[0]
            f["lon"] = centre[1]
        print(f"[{job.job_id}] pinned video findings to centre {centre}")
    else:
        print(f"[{job.job_id}] no usable centre — video findings left without coords (will inherit from report claims)")

    # ---- 4. Parse report ------------------------------------------------
    _set(job, "Parsing damage report with Claude...")
    report_tmp_dir = Path(tempfile.mkdtemp())
    report_tmp_path = report_tmp_dir / report_filename
    try:
        report_tmp_path.write_bytes(report_bytes)
        claims = parse_report(report_tmp_path)
    finally:
        report_tmp_path.unlink(missing_ok=True)
        report_tmp_dir.rmdir()
    claim_dicts = [c.to_dict() for c in claims]

    if not claim_dicts:
        job.progress = "Warning: no claims extracted from report — continuing with video only."
    else:
        _set(job, "Geocoding report claim addresses via Overture...")
        try:
            claim_dicts = geocode_claims(claim_dicts)
            geocoded = sum(1 for c in claim_dicts if c.get("lat") is not None)
            print(f"[{job.job_id}] geocoded {geocoded}/{len(claim_dicts)} claim addresses")
        except Exception as e:
            print(f"[{job.job_id}] geocoding failed (non-fatal): {e}")

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

    # Populate clip_url on every finding that has a video block.
    # Presigned URL is valid for 2 hours — enough for any demo session.
    clip_url = None
    clip_url_error = None
    try:
        clip_url = generate_presigned_url(s3_uri, expiry_seconds=7200)
        print(f"[{job.job_id}] presigned clip_url generated: {clip_url[:80]}...")
    except Exception as e:
        clip_url_error = f"presign_failed: {type(e).__name__}: {e}"
        print(f"[{job.job_id}] CLIP URL FAILED: {clip_url_error}")

    for finding in result["findings"]:
        if finding.get("video"):
            finding["video"]["clip_url"] = clip_url
            if clip_url_error:
                finding["video"]["clip_url_error"] = clip_url_error

    job.results = result

    # ---- 8b. SNS alerts -------------------------------------------------
    _set(job, "Checking for critical findings...")
    check_and_alert(
        result["findings"],
        event_name=location_hint or "Tornado Assessment",
        location=location_hint,
    )

    # ---- 9. Overture GeoJSON for this event's bbox ----------------------
    if result["findings"]:
        _set(job, "Fetching Overture map reference data...")
        try:
            from scripts.m6_overture_geojson import query_places, query_buildings, query_roads, _connect
            # Prefer geocoded claim coords (real Overture lookups) over fused
            # findings (which may carry an LLM-guessed centre).
            bbox = _bbox_from_coords(claim_dicts, result["findings"])
            if bbox is None:
                raise RuntimeError("no lat/lon available for Overture bbox")
            min_lat, min_lon, max_lat, max_lon = bbox
            print(f"[{job.job_id}] Overture bbox: {bbox}")
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
