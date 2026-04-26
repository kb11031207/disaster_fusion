"""
Natural language query over fused findings.

Given a question + a list of Finding dicts (frontend-schema shape), calls
Claude Haiku on Bedrock and returns a structured answer plus the IDs of
findings the answer cites — so the frontend can highlight markers on the map.

No retrieval / vector search yet — Claude reads the full condensed finding
list. That's fine for hackathon-scale (~30 findings); swap in vector
pre-filtering when N gets large.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3


_BEDROCK = None


def _client():
    global _BEDROCK
    if _BEDROCK is None:
        _BEDROCK = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    return _BEDROCK


def _model_id() -> str:
    return os.environ.get("CLAUDE_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0")


def _condense(f: dict) -> dict:
    """Strip a Finding down to the fields useful for answering questions."""
    entry = {
        "id":             f.get("id"),
        "entity_name":    f.get("entity_name", "Unknown"),
        "facility_type":  f.get("facility_type"),
        "fusion_status":  f.get("fusion_status"),
        "final_severity": f.get("final_severity"),
        "confidence":     round(f.get("confidence") or 0, 3),
        "lat":            f.get("lat"),
        "lon":            f.get("lon"),
        "event_date":     f.get("event_date"),
    }
    if f.get("video"):
        entry["video_summary"] = (f["video"].get("summary") or "")[:240]
    if f.get("pdf"):
        entry["report_excerpt"]  = (f["pdf"].get("excerpt") or "")[:240]
        entry["report_severity"] = f["pdf"].get("claimed_severity")
    if f.get("fusion"):
        entry["fusion_reasoning"] = (f["fusion"].get("reasoning") or "")[:160]
    return entry


_SYSTEM_PROMPT = """You are an intelligence analyst assistant for DisasterFusion, a disaster damage assessment system that fuses aerial video analysis with official damage reports.

Each finding has a fusion_status:
- "confirmed"           — both video and report agree on damage
- "unreported_damage"   — video found damage no report mentions (gap in official record)
- "conflicting_severity"— both sources present but disagree on severity
- "uncertain"           — insufficient evidence from either source

Your job: answer the analyst's question by searching across ALL findings.
Always cite specific finding IDs (e.g., "ff-7ea73a1e") so the analyst can click them on the map.
When the answer involves both video and report data, explicitly note what each source shows.
Be concise — analysts want actionable answers, not essays.

Respond ONLY with raw JSON, no markdown fences:
{
  "answer": "<your answer, citing finding IDs inline>",
  "referenced_ids": ["<id1>", "<id2>"],
  "query_type": "filter" | "summary" | "comparison" | "detail"
}

query_type:
- "filter"     — show specific findings (e.g., "show me severe damage")
- "summary"    — aggregate / overview (e.g., "how much damage total")
- "comparison" — video vs report (e.g., "where do sources disagree")
- "detail"    — single entity / location (e.g., "what happened to Drifters")
"""


def query_findings(question: str, findings: list[dict]) -> dict:
    """
    Answer a natural-language question against the fused findings.

    Args:
        question: analyst's plain-text question
        findings: list of Finding dicts (frontend schema shape)

    Returns:
        {"answer": str, "referenced_ids": [str], "query_type": str}
    """
    if not question or not question.strip():
        return {"answer": "Please enter a question.", "referenced_ids": [], "query_type": "summary"}
    if not findings:
        return {"answer": "No findings available to query.", "referenced_ids": [], "query_type": "summary"}

    condensed = [_condense(f) for f in findings]

    user_message = (
        "FINDINGS DATA:\n"
        f"{json.dumps(condensed, indent=2)}\n\n"
        f"ANALYST QUESTION: {question}\n\n"
        "Respond with JSON only."
    )

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens":  1024,
        "temperature": 0,
        "system":      _SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": user_message}],
    })

    model_id = _model_id()
    bedrock  = _client()

    try:
        try:
            resp = bedrock.invoke_model(modelId=model_id, body=body)
        except Exception:
            # Some Bedrock regions require the inference profile prefix.
            resp = bedrock.invoke_model(modelId=f"us.{model_id}", body=body)

        payload = json.loads(resp["body"].read())
        text    = payload["content"][0]["text"].strip()

        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        result = json.loads(text)

        # Sanity-check shape
        result.setdefault("answer", "")
        result.setdefault("referenced_ids", [])
        result.setdefault("query_type", "summary")
        return result

    except json.JSONDecodeError:
        return {
            "answer":         text if "text" in dir() else "Claude returned an unparseable response.",
            "referenced_ids": [],
            "query_type":     "summary",
        }
    except Exception as e:
        return {
            "answer":         f"Query failed: {e}",
            "referenced_ids": [],
            "query_type":     "summary",
        }
