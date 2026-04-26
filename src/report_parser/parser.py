"""
M3 — FEMA report parser.

Reads a FEMA Project Worksheet (PW) or Preliminary Damage Assessment (PDA)
DOCX, extracts plain text, hands it to Claude Haiku 4.5 on Bedrock with a
strict JSON-output prompt, and returns a list of ReportClaim dataclasses.

Geocoding (location_name -> lat/lon) lives in geocoder.py and runs after.
"""

from __future__ import annotations

import base64
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

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

_PROMPT_TEMPLATE = """You are a damage report analyst. You will be given the raw text of an
official damage report. It may be any of:

- NWS Damage Survey (EF rating, path length/width, wind estimates, per-location
  damage descriptions)
- County EMA report or press release (damage summaries, affected areas, response)
- FEMA Project Worksheet (PW) — single damaged asset, has DISASTER NUMBER,
  LOCATION / SITE OF DAMAGE, DESCRIPTION OF DAMAGE, PROJECT COST ESTIMATE
- FEMA Preliminary Damage Assessment (PDA) — state/county rollups with
  residence counts (Destroyed / Major / Minor / Affected) and per-county
  per-capita dollars
- News report (interviews, damage descriptions, named businesses)
- Insurance / adjuster summary

EXTRACTION RULES
================

ONE CLAIM PER DAMAGED LOCATION OR STRUCTURE. If a report mentions three
damaged buildings, return three separate claim rows. Do not merge them.

For PROJECT WORKSHEETs:
- Return exactly ONE row.
- location_name from LOCATION / SITE OF DAMAGE; damage_description from
  DESCRIPTION OF DAMAGE (preserve wording verbatim — Marengo will embed
  it). cost_estimate = TOTAL PROJECT COST as integer.

For PDAs:
- Return ONE row per county in "Countywide per capita impact".
- ALSO return ONE row per specific asset named in the narrative
  (e.g. "Mobile State Docks"). Skip generic phrases ("homes along the coast").
- County rows: location_name = "<County> County, <State>".

For NWS surveys / news / EMA reports:
- Return ONE row per identifiable damaged location or business.
- Pull building_name aggressively — "Drifters Eats and Drinks", "St. Mary's
  Hospital". Matching on name is the strongest fusion signal.

DAMAGE_TYPE ENUM (pick the MOST SEVERE if multiple apply; mention the others
in damage_description):
structural_collapse | roof_damage | debris_field | vegetation_damage |
infrastructure_damage | vehicle_damage | window_door_damage | flooding | other

Mappings:
- "blown out", "windows out", "patio doors gone" -> window_door_damage
- "roof off", "roof torn", "rafters exposed" -> roof_damage
- "flattened", "leveled", "walls down" -> structural_collapse
- "trees uprooted", "branches down" -> vegetation_damage
- "power lines down", "poles snapped", "road impassable" -> infrastructure_damage

SEVERITY ENUM:
- minor    : cosmetic, still functional ("minor damage", EF0)
- moderate : significant, partially usable ("moderate", EF1)
- severe   : major structural, unusable ("severe", "destroyed", EF2)
- destroyed: total loss, structure gone ("leveled", "total destruction", EF3+)

EF -> severity (default if narrative is silent):
EF0 -> minor; EF1 -> moderate (or severe if narrative says "significant"/"extensive");
EF2 -> severe; EF3+ -> destroyed.

BUILDING_TYPE ENUM:
residential | commercial | industrial | public | infrastructure |
agricultural | unknown

OUTPUT (return ONLY this JSON shape, no markdown fences, no prose):
{{
  "claims": [
    {{
      "event_type": "<tornado | hurricane | flood | wildfire | earthquake>",
      "event_name": "<e.g. Grafton EF1 Tornado, or null>",
      "event_date": "<YYYY-MM-DD or null>",
      "report_date": "<YYYY-MM-DD or null>",

      "location_name": "<specific address / asset name + city, state>",

      "damage_type": "<enum>",
      "severity": "<enum>",
      "damage_description": "<doc's wording, preserve technical terms>",

      "building_type": "<enum>",
      "building_name": "<specific business/structure name or null>",
      "structures_affected": <integer or null>,

      "infrastructure_impacts": ["<short phrase>", ...],

      "ef_rating": "<EF0|EF1|EF2|EF3|EF4|EF5 or null>",
      "cost_estimate": <integer or null>
    }}
  ]
}}

CONVENTIONS:
- For Hurricane Katrina docs, event_date = 2005-08-29 if not stated.
- cost_estimate: integers only. $47.4M -> 47400000.
- If a field is missing, use null. Do not invent values.
- infrastructure_impacts: empty list [] if none mentioned.

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
    pdf_bytes: Optional[bytes] = None,
) -> str:
    """
    Send a prompt to Claude on Bedrock and return the raw text response.
    If pdf_bytes is provided, attach it as a base64 document block so Claude
    parses the PDF natively (no pdfplumber dep). Falls back to the regional
    inference profile prefix (us.) if the bare model ID 400s.
    """
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    model_id = model_id or os.environ.get(
        "CLAUDE_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0"
    )

    if pdf_bytes is not None:
        import base64
        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
                },
            },
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    bedrock = boto3.client("bedrock-runtime", region_name=region)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "temperature": 0,
        "messages": [{"role": "user", "content": content}],
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
            return _call_claude_bedrock(prompt, region, "us." + model_id, pdf_bytes)
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

def _build_claims(
    raw_claims: list[dict[str, Any]],
    source_document: str,
    source_type: str,
) -> list[ReportClaim]:
    claims: list[ReportClaim] = []
    for raw in raw_claims:
        impacts = raw.get("infrastructure_impacts") or []
        if not isinstance(impacts, list):
            impacts = []
        claim = ReportClaim(
            claim_id=generate_id("rc"),
            source_document=source_document,
            source_type=source_type,
            location_name=(raw.get("location_name") or "").strip(),
            damage_description=(raw.get("damage_description") or "").strip(),
            severity=raw.get("severity") or "moderate",
            damage_type=raw.get("damage_type") or raw.get("damage_category") or "other",
            cost_estimate=raw.get("cost_estimate"),
            ef_rating=raw.get("ef_rating"),
            event_type=raw.get("event_type"),
            event_name=raw.get("event_name"),
            event_date=raw.get("event_date"),
            report_date=raw.get("report_date"),
            building_type=raw.get("building_type"),
            building_name=raw.get("building_name"),
            structures_affected=raw.get("structures_affected"),
            infrastructure_impacts=[str(x) for x in impacts if x],
        )
        claims.append(claim)
    return claims


def parse_report(report_path: str | Path) -> list[ReportClaim]:
    """
    Extract structured ReportClaim rows from a damage report.

    Supported inputs:
      - .docx  → extract text and run Claude on Bedrock
      - .pdf   → pass raw bytes to Claude as a document block (native parsing)
      - .json  → already-structured claims; load directly, skip Claude
      - .txt / .csv / .md → read as plain text and run Claude
    """
    path = Path(report_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Report not found: {path}")

    suffix = path.suffix.lower()
    print(f"Parsing {path.name} ...")

    if suffix == ".json":
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        raw_claims: list[dict[str, Any]] = parsed.get("claims") or []
        sources = parsed.get("sources") or []
        source_type = (sources[0].get("source_type") if sources else None) or "json"
        claims = _build_claims(raw_claims, path.name, source_type)
        print(f"  loaded {len(claims)} claim(s) from JSON (Claude skipped)")
        return claims

    pdf_bytes: Optional[bytes] = None
    if suffix == ".pdf":
        pdf_bytes = path.read_bytes()
        # Claude parses the PDF itself; we can't sniff PW vs PDA up front.
        source_type = "PDA"
        print(f"  pdf bytes: {len(pdf_bytes)} (Claude will parse natively)")
        prompt = _PROMPT_TEMPLATE.format(document_text="(see attached PDF)")
    else:
        if suffix == ".docx":
            doc_text = extract_docx_text(path)
        elif suffix in (".txt", ".csv", ".md"):
            doc_text = path.read_text(encoding="utf-8", errors="replace")
        else:
            raise ValueError(f"Unsupported report format: {suffix}")

        source_type = detect_source_type(doc_text)
        print(f"  text length: {len(doc_text)} chars")
        print(f"  source_type: {source_type}")
        prompt = _PROMPT_TEMPLATE.format(document_text=doc_text)

    raw_response = _call_claude_bedrock(prompt, pdf_bytes=pdf_bytes)
    json_text = _strip_json_fences(raw_response)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed: {e}")
        print(f"  raw response head: {raw_response[:500]}")
        raise

    raw_claims = parsed.get("claims") or []
    claims = _build_claims(raw_claims, path.name, source_type)
    print(f"  Claude returned {len(claims)} claim(s)")
    return claims


def parse_text(
    raw_text: str,
    source_name: str = "raw_text",
    source_type: str = "news_report",
) -> list[ReportClaim]:
    """
    Extract structured ReportClaim rows from plain text.
    No file needed — pass article/report text directly.

    Args:
        raw_text: plain-text content (article, news report, etc.)
        source_name: human-readable source (e.g., "First Alert 4 — Grafton tornado")
        source_type: "news_report" | "nws_survey" | "county_ema" | "fema_pda"

    Returns:
        list[ReportClaim]
    """
    print(f"Parsing text from {source_name!r} ({source_type})...")
    print(f"  text length: {len(raw_text)} chars")

    source_type_sniff = detect_source_type(raw_text)
    prompt = _PROMPT_TEMPLATE.format(document_text=raw_text)
    raw_response = _call_claude_bedrock(prompt)
    json_text = _strip_json_fences(raw_response)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed: {e}")
        print(f"  raw response head: {raw_response[:500]}")
        raise

    raw_claims: list[dict[str, Any]] = parsed.get("claims") or []
    claims = _build_claims(raw_claims, source_name, source_type)
    print(f"  Claude returned {len(claims)} claim(s)")
    return claims


def fetch_and_parse_url(
    url: str,
    source_type: str = "news_report",
) -> list[ReportClaim]:
    """
    Fetch a URL, extract article text, parse into ReportClaims.

    Uses httpx to fetch; Claude extracts article text from HTML before
    claim extraction. Avoids beautifulsoup dependency.

    Args:
        url: article or report URL
        source_type: "news_report" | "nws_survey" | "county_ema" | "fema_pda"

    Returns:
        list[ReportClaim]
    """
    try:
        import httpx
    except ImportError:
        raise ImportError(
            "httpx required for URL fetching. Install with: pip install httpx"
        )

    print(f"Fetching {url}...")
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        html = response.text
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {url}: {e}")

    extraction_prompt = f"""Extract the main article or report text from this HTML.
Ignore navigation, ads, scripts, styles, metadata, and page structure.
Return only clean, readable text with paragraph breaks preserved.

<html>
{html[:50000]}
</html>

Return only the article text:"""

    print(f"  extracting article text...")
    raw_response = _call_claude_bedrock(extraction_prompt)
    article_text = raw_response.strip()

    print(f"  extracted {len(article_text)} chars")
    source_name = urlparse(url).netloc or url
    return parse_text(article_text, source_name=source_name, source_type=source_type)
