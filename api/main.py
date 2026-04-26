"""
DisasterFusion API

Endpoints
---------
POST /analyze                   — submit video + report, returns { job_id }
GET  /jobs/{job_id}             — poll status + progress
GET  /jobs/{job_id}/results     — get master_findings shape when done
GET  /jobs/{job_id}/overture    — get overture_reference GeoJSON when done
GET  /health                    — liveness check

Usage
-----
    pip install fastapi uvicorn python-multipart
    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.job_store       import store
from api.nl_query        import query_findings
from api.pipeline_runner import run_pipeline


class QueryRequest(BaseModel):
    question: str

app = FastAPI(title="DisasterFusion API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(
    video:         UploadFile = File(..., description="Tornado video file (mp4, mov, avi)"),
    report:        UploadFile = File(..., description="Damage report (txt, pdf, docx, csv)"),
    location_hint: str        = Form(default="", description="Optional location hint e.g. 'Grafton, Illinois'"),
):
    """
    Submit a video + damage report for fusion analysis.
    Returns a job_id to poll for progress and results.
    """
    video_bytes  = await video.read()
    report_bytes = await report.read()

    if not video_bytes:
        raise HTTPException(status_code=400, detail="Video file is empty.")
    if not report_bytes:
        raise HTTPException(status_code=400, detail="Report file is empty.")

    job = store.create()

    thread = threading.Thread(
        target=run_pipeline,
        args=(job, video.filename, video_bytes, report.filename, report_bytes, location_hint),
        daemon=True,
    )
    thread.start()

    return {
        "job_id":  job.job_id,
        "status":  job.status,
        "message": "Pipeline started. Poll /jobs/{job_id} for progress.",
    }


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    """Poll this endpoint for job progress."""
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job.to_dict()


@app.get("/jobs/{job_id}/results")
def job_results(job_id: str):
    """
    Returns master_findings shape: { center, zoom, findings }.
    Only available when status == 'done'.
    """
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status == "failed":
        raise HTTPException(status_code=500, detail=job.error or "Pipeline failed.")
    if job.status != "done":
        raise HTTPException(status_code=202, detail=f"Job is {job.status}: {job.progress}")
    return job.results


@app.get("/jobs/{job_id}/overture")
def job_overture(job_id: str):
    """
    Returns overture_reference GeoJSON for this event's bounding box.
    Only available when status == 'done'.
    """
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(status_code=202, detail=f"Job is {job.status}: {job.progress}")
    return job.overture or {"type": "FeatureCollection", "features": []}


@app.post("/jobs/{job_id}/query")
def job_query(job_id: str, req: QueryRequest):
    """
    Natural language query over a job's fused findings.

    Body: { "question": "..." }
    Returns: { "answer": "...", "referenced_ids": [...], "query_type": "..." }
    """
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status != "done":
        raise HTTPException(status_code=202, detail=f"Job is {job.status}: {job.progress}")
    if not job.results or not job.results.get("findings"):
        raise HTTPException(status_code=404, detail="No findings available to query.")

    return query_findings(req.question, job.results["findings"])


@app.get("/jobs")
def list_jobs():
    """List all jobs (for debugging)."""
    return store.all()


@app.post("/test/parse-url")
def test_parse_url(req: QueryRequest):
    """
    Test endpoint: extract damage claims from a news article URL.

    Body: { "question": "https://example.com/article" }
    Returns: { "claims": [...], "count": N, "source": "url" }
    """
    url = req.question.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Must provide a valid HTTP(S) URL.")

    from src.report_parser.parser import fetch_and_parse_url

    try:
        claims = fetch_and_parse_url(url, source_type="news_report")
        return {
            "claims": [c.to_dict() for c in claims],
            "count": len(claims),
            "source": url,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parse failed: {str(e)}")
