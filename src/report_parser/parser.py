"""
M3 — FEMA report parser.

Reads a FEMA Project Worksheet (PW) or Preliminary Damage Assessment (PDA)
DOCX, extracts plain text, hands it to Claude Haiku 4.5 on Bedrock with a
strict JSON-output prompt, and returns a list of ReportClaim dataclasses.

Geocoding (location_name -> lat/lon) lives in geocoder.py and runs after.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Optional

import boto3

from src.shared.models import ReportClaim
from src.shared.utils import generate_id


# ---------------------------------------------------------------------------
# DOCX text extraction
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_PARA_RE = re.compile(r"</w:p>")


def extract_docx_text(path: Path) -> str:
    """
    Pull readable text out of a .docx by reading word/document.xml.
    Avoids a python-docx dependency — we just unzip and strip XML tags,
    converting paragraph closes to newlines so structure survives.
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    xml = _PARA_RE.sub("\n", xml)
    text = _TAG_RE.sub("", xml)
    text = (
        text.replace("&apos;", "'")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def detect_source_type(doc_text: str) -> str:
    """Return 'PW' if the doc looks like a Project Worksheet, else 'PDA'."""
    head = doc_text[:1500].upper()
    if "PROJECT WORKSHEET" in head and "DISASTER NUMBER" in head:
        return "PW"
    if "PRELIMINARY DAMAGE ASSESSMENT" in head:
        return "PDA"
    return "PDA"  # fallback — most FEMA narrative docs are PDA-style


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """You are a FEMA damage report analyst. You will be given the raw text of an
official damage assessment document. It will be ONE of two types:

(A) PROJECT WORKSHEET (PW) — FEMA Form 009-0-91 covering ONE damaged asset.
    Recognizable by headers: "PROJECT WORKSHEET", "DISASTER NUMBER",
    "LOCATION / SITE OF DAMAGE", "DESCRIPTION OF DAMAGE", "SCOPE OF WORK",
    "PROJECT COST ESTIMATE".

(B) PRELIMINARY DAMAGE ASSESSMENT (PDA) — state-level rollup. Has counts of
    residences (Destroyed / Major Damage / Minor Damage / Affected),
    per-county per-capita impact dollars, and a narrative paragraph that
    may name specific damaged assets.

EXTRACTION RULES
================

If the document is a PROJECT WORKSHEET:
- Return exactly ONE row.
- location_name: the address/asset description from LOCATION / SITE OF DAMAGE.
- damage_description: extract from the DESCRIPTION OF DAMAGE section verbatim
  or near-verbatim. Preserve technical wording: "storm surge", "displaced",
  "inundated", "rendered inoperable", "complete failure". Do NOT paraphrase
  these — Marengo will text-embed this string and match it against video.
- cost_estimate: the TOTAL PROJECT COST as an integer (no $, no commas).
- report_date: from "DATE PREPARED".

If the document is a PRELIMINARY DAMAGE ASSESSMENT:
- Return ONE row per county that appears in the "Countywide per capita impact"
  line.
- ALSO return ONE row per specific asset named in the narrative paragraph
  (e.g. "Mobile State Docks", "Bay St. Louis Bridge"). Skip generic phrases
  like "homes along the coast".
- For county rows: location_name = "<County> County, <State>";
  damage_description synthesizes the narrative + residence counts
  ("Storm surge damage; 18,940 residences destroyed, 24,600 with major damage,
  per FEMA PDA"); cost_estimate = null unless an exact dollar figure is
  attributed to THAT county.
- For named-asset rows: location_name = asset + city/state; damage_description
  = the sentence(s) in the narrative describing it.
- report_date: from "Declared <date>".

SEVERITY ENUM (FEMA's own definitions):
- "Destroyed" / "total loss" / "catastrophic" / "complete failure" -> destroyed
- "Major Damage" / "substantial failure" / "severe"                -> severe
- "Affected" / "moderate" / "significant damage"                   -> moderate
- "Minor Damage"                                                   -> minor

For aggregate rows mixing severities, use the highest severity that has
>10% of the total count.

DAMAGE_CATEGORY ENUM:
structural_collapse | roof_damage | flooding | debris_field |
infrastructure_damage | vegetation_damage | vehicle_damage |
fire_damage | erosion | other

Pick the dominant category. A flooded hospital with destroyed electrical
systems -> flooding (the primary cause). A bridge with displaced spans ->
infrastructure_damage. A coastal area with washed-away buildings -> flooding.

OUTPUT (return ONLY this JSON shape, no markdown fences, no prose):
{{
  "claims": [
    {{
      "location_name": "<specific>",
      "damage_description": "<doc's wording>",
      "severity": "<enum>",
      "damage_category": "<enum>",
      "cost_estimate": <integer or null>,
      "event_date": "<YYYY-MM-DD or null>",
      "report_date": "<YYYY-MM-DD or null>"
    }}
  ]
}}

CONVENTIONS:
- For Hurricane Katrina docs, event_date = 2005-08-29 unless the doc says
  otherwise (Katrina's main landfall date).
- cost_estimate: integers only. $47.4M -> 47400000.
- If a field is missing from the doc, use null. Do not invent values.

DOCUMENT TEXT:
<<<
{document_text}
>>>
"""


# ---------------------------------------------------------------------------
# Bedrock Claude call
# ---------------------------------------------------------------------------

def _call_claude_bedrock(
    prompt: str,
    region: Optional[str] = None,
    model_id: Optional[str] = None,
) -> str:
    """
    Send a prompt to Claude on Bedrock and return the raw text response.
    Falls back to the regional inference profile prefix (us.) if the bare
    model ID 400s — Bedrock requires the prefix for some models.
    """
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    model_id = model_id or os.environ.get(
        "CLAUDE_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0"
    )

    bedrock = boto3.client("bedrock-runtime", region_name=region)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        resp = bedrock.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
    except bedrock.exceptions.ValidationException as e:
        # Bedrock often requires the regional inference-profile prefix.
        msg = str(e)
        if not model_id.startswith("us.") and "inference profile" in msg.lower():
            print(f"  retrying with us. prefix...")
            return _call_claude_bedrock(prompt, region, "us." + model_id)
        raise

    payload = json.loads(resp["body"].read())
    return payload["content"][0]["text"]


# ---------------------------------------------------------------------------
# Response cleanup
# ---------------------------------------------------------------------------

def _strip_json_fences(text: str) -> str:
    """If Claude wrapped output in ```json ... ``` despite the prompt, strip it."""
    text = text.strip()
    if text.startswith("```"):
        # Drop first line (```json or ```).
        if "\n" in text:
            text = text.split("\n", 1)[1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_report(report_path: str | Path) -> list[ReportClaim]:
    """
    Extract structured ReportClaim rows from a FEMA DOCX.
    No geocoding here — that's a separate step; lat/lon stay None.
    """
    path = Path(report_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Report not found: {path}")

    print(f"Parsing {path.name} ...")
    doc_text = extract_docx_text(path)
    source_type = detect_source_type(doc_text)
    print(f"  text length: {len(doc_text)} chars")
    print(f"  source_type: {source_type}")

    prompt = _PROMPT_TEMPLATE.format(document_text=doc_text)
    raw_response = _call_claude_bedrock(prompt)
    json_text = _strip_json_fences(raw_response)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed: {e}")
        print(f"  raw response head: {raw_response[:500]}")
        raise

    raw_claims: list[dict[str, Any]] = parsed.get("claims") or []
    claims: list[ReportClaim] = []
    for raw in raw_claims:
        claim = ReportClaim(
            claim_id=generate_id("rc"),
            source_document=path.name,
            source_type=source_type,
            location_name=(raw.get("location_name") or "").strip(),
            damage_description=(raw.get("damage_description") or "").strip(),
            severity=raw.get("severity") or "moderate",
            damage_category=raw.get("damage_category") or "other",
            cost_estimate=raw.get("cost_estimate"),
            event_date=raw.get("event_date"),
            report_date=raw.get("report_date"),
        )
        claims.append(claim)

    print(f"  Claude returned {len(claims)} claim(s)")
    return claims
