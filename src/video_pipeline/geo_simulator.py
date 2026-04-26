"""
M5.5 — Simulate plausible coordinates for video findings.

Why this exists
===============
Pegasus reads MP4 pixels, not telemetry. News and YouTube clips strip
GPS metadata, so `VideoFinding.geo` is None after validation. The
production path is per-frame drone telemetry; this module is the demo
path for footage that has none. The fusion engine and renderer treat
real and simulated coordinates identically — same code either way.

Pipeline (fully parameterized — no hardcoded locations)
=======================================================
  1. EXTRACT  Send all finding descriptions to Claude Haiku 4.5 on
              Bedrock. Claude returns primary_location, area_type,
              estimated_center, and any landmarks it spots (with
              coords). One Bedrock call per video.
  2. SCATTER  Gaussian-distribute coords around the centre, sigma
              derived from area_type. Findings whose descriptions
              mention a known landmark snap to that landmark with
              small jitter. Reproducible via `seed`.

Every simulated coord is stamped:
    geo_method     = "simulated_within_disaster_zone"
    geo_confidence = "low"

Honest by construction — the JSON makes it visible that these aren't
real GPS, so judges can see the architecture without us pretending.

Optional `hint` lets an operator override the LLM (e.g.
`--hint "Grafton, Illinois"`) for cases where descriptions don't
name a place — silent aerial drone footage with no narration is the
typical reason this is needed.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import boto3
import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Zone half-widths in degrees. We use sigma = spread/2 inside the gaussian,
# so the 2-sigma cone is roughly `spread` wide. Tuned against real disaster
# scales — a tornado track is a few km, an urban event tighter.
ZONE_SPREADS: dict[str, dict[str, float]] = {
    "rural":    {"lat": 0.020, "lon": 0.030},   # ~2-3 km cone
    "suburban": {"lat": 0.015, "lon": 0.020},   # ~1.5-2 km
    "urban":    {"lat": 0.008, "lon": 0.012},   # tighter, blocks
    "coastal":  {"lat": 0.015, "lon": 0.035},   # elongated along coast
}
DEFAULT_AREA_TYPE = "suburban"

GEO_METHOD     = "simulated_within_disaster_zone"
GEO_CONFIDENCE = "low"

# Jitter applied when a finding snaps to a landmark — ~110 m at the equator,
# so multiple findings at the same landmark don't render as one marker.
_LANDMARK_JITTER_DEG = 0.001


# ---------------------------------------------------------------------------
# Step 1 — LLM extraction
# ---------------------------------------------------------------------------

_LOCATION_PROMPT = """You are a geospatial analyst. You will be given damage descriptions
that an aerial-video AI extracted from drone or news footage of a SINGLE
disaster event. Identify where it happened and any specific landmarks
the descriptions name.

Rules
=====
- primary_location: ONE place for the whole set. Format
  "<City>, <State/Region>, <Country>". If only the country is known,
  return just "<Country>".
- landmarks: each named business, park, bridge, resort, school, etc.
  the descriptions mention. Provide your best lat/lon for each. Skip
  generic phrases ("a house", "downtown"). Empty list is fine.
- area_type: one of rural, suburban, urban, coastal — best fit.
- confidence: high if a place name is explicit in the descriptions,
  medium if you inferred it from context, low if you are guessing.
- estimated_center: your best lat/lon for the primary_location. Used
  as the disaster-zone centre.
{hint_block}

Damage descriptions
===================
{descriptions_block}

Return ONLY this JSON shape, no prose, no markdown fences:
{{
  "primary_location": "<City, State, Country>",
  "landmarks": [
    {{"name": "<name>", "lat": <float>, "lon": <float>}}
  ],
  "area_type": "rural" | "suburban" | "urban" | "coastal",
  "confidence": "high" | "medium" | "low",
  "estimated_center": [<lat>, <lon>]
}}
"""


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        if "\n" in text:
            text = text.split("\n", 1)[1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _call_claude(
    prompt: str,
    region: Optional[str] = None,
    model_id: Optional[str] = None,
) -> str:
    """Mirror of src/report_parser/parser.py::_call_claude_bedrock — same
    fallback to the regional inference-profile prefix."""
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    model_id = model_id or os.environ.get(
        "CLAUDE_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    bedrock = boto3.client("bedrock-runtime", region_name=region)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
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
        if not model_id.startswith("us.") and "inference profile" in str(e).lower():
            return _call_claude(prompt, region, "us." + model_id)
        raise
    payload = json.loads(resp["body"].read())
    return payload["content"][0]["text"]


def extract_location(
    descriptions: list[str],
    *,
    hint: Optional[str] = None,
) -> dict[str, Any]:
    """
    Ask Claude to identify the disaster location and any landmarks.

    Returns a dict with keys:
        primary_location:  str
        landmarks:         list[{"name", "lat", "lon"}]
        area_type:         one of ZONE_SPREADS keys
        confidence:        "high" | "medium" | "low"
        estimated_center:  [lat, lon]
    """
    descriptions = [d for d in descriptions if (d or "").strip()]
    if not descriptions:
        raise ValueError("extract_location: no non-empty descriptions provided")

    desc_block = "\n".join(f"- {d}" for d in descriptions)
    if hint:
        hint_block = (
            f"\nOPERATOR HINT: this footage was captured at or near "
            f"{hint!r}. Trust this hint over what the descriptions imply."
        )
    else:
        hint_block = ""

    prompt = _LOCATION_PROMPT.format(
        hint_block=hint_block,
        descriptions_block=desc_block,
    )
    raw = _call_claude(prompt)
    data = json.loads(_strip_json_fences(raw))

    if not data.get("primary_location"):
        raise RuntimeError(f"extract_location: missing primary_location in {data!r}")
    if data.get("area_type") not in ZONE_SPREADS:
        data["area_type"] = DEFAULT_AREA_TYPE
    centre = data.get("estimated_center")
    if not (isinstance(centre, list) and len(centre) == 2):
        raise RuntimeError(f"extract_location: bad estimated_center in {data!r}")

    # Normalize landmark shape — accept only well-formed entries.
    raw_lms = data.get("landmarks") or []
    landmarks: list[dict[str, Any]] = []
    for lm in raw_lms:
        if not isinstance(lm, dict):
            continue
        if not all(k in lm for k in ("name", "lat", "lon")):
            continue
        try:
            landmarks.append({
                "name": str(lm["name"]),
                "lat":  float(lm["lat"]),
                "lon":  float(lm["lon"]),
            })
        except (TypeError, ValueError):
            continue
    data["landmarks"] = landmarks
    return data


# ---------------------------------------------------------------------------
# Step 2 — Scatter
# ---------------------------------------------------------------------------


def _match_landmark(
    description: str,
    landmarks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not description:
        return None
    desc_lc = description.lower()
    for lm in landmarks:
        if lm["name"].lower() in desc_lc:
            return lm
    return None


def assign_coordinates(
    findings: list[dict[str, Any]],
    *,
    centre: tuple[float, float],
    spread_lat: float,
    spread_lon: float,
    landmarks: list[dict[str, Any]],
    seed: int = 42,
) -> list[dict[str, Any]]:
    """
    Mutate `findings` in place: stamp `geo`, `geo_method`, `geo_confidence`.
    Returns the same list for chaining.

    Findings whose `geo_method` is already set to something *other* than
    GEO_METHOD are left alone — that's the production-telemetry escape
    hatch. Findings already simulated are re-simulated (idempotent re-run).
    """
    rng = np.random.default_rng(seed)
    sigma_lat = spread_lat / 2.0
    sigma_lon = spread_lon / 2.0

    for finding in findings:
        existing_method = finding.get("geo_method")
        if (
            finding.get("geo")
            and existing_method
            and existing_method != GEO_METHOD
        ):
            continue  # real telemetry — don't overwrite

        match = _match_landmark(finding.get("damage_description", ""), landmarks)
        if match is not None:
            lat = match["lat"] + rng.normal(0, _LANDMARK_JITTER_DEG)
            lon = match["lon"] + rng.normal(0, _LANDMARK_JITTER_DEG)
        else:
            lat = centre[0] + rng.normal(0, sigma_lat)
            lon = centre[1] + rng.normal(0, sigma_lon)

        finding["geo"]            = [round(lat, 6), round(lon, 6)]
        finding["geo_method"]     = GEO_METHOD
        finding["geo_confidence"] = GEO_CONFIDENCE

    return findings


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def geolocate_findings(
    findings: list[dict[str, Any]],
    *,
    hint: Optional[str] = None,
    centre_override: Optional[tuple[float, float]] = None,
    seed: int = 42,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Run the full simulate-coords pipeline on a list of VideoFinding dicts.

    Returns (findings_with_geo, zone_info). `findings` is mutated in place.

    `centre_override` skips the Claude LLM call and uses the supplied
    (lat, lon) as the disaster-zone centre. Useful when the real location
    is known (e.g. from the report claims) and Claude's geocoding is
    unreliable because the video descriptions don't name the place.

    `zone_info` keys:
        primary_location, centre [lat,lon], spread_lat, spread_lon,
        area_type, landmarks, extraction_confidence, method, hint
    """
    descriptions = [f.get("damage_description", "") for f in findings]

    if centre_override is not None:
        info = {
            "primary_location": hint or "operator-supplied",
            "landmarks": [],
            "area_type": DEFAULT_AREA_TYPE,
            "confidence": "high",
            "estimated_center": list(centre_override),
        }
    else:
        info = extract_location(descriptions, hint=hint)

    centre  = (float(info["estimated_center"][0]),
               float(info["estimated_center"][1]))
    area    = info["area_type"]
    spreads = ZONE_SPREADS[area]

    assign_coordinates(
        findings,
        centre=centre,
        spread_lat=spreads["lat"],
        spread_lon=spreads["lon"],
        landmarks=info["landmarks"],
        seed=seed,
    )

    zone_info = {
        "primary_location":      info["primary_location"],
        "centre":                [centre[0], centre[1]],
        "spread_lat":            spreads["lat"],
        "spread_lon":            spreads["lon"],
        "area_type":              area,
        "landmarks":             info["landmarks"],
        "extraction_confidence": info.get("confidence"),
        "method":                GEO_METHOD,
        "hint":                  hint,
    }
    return findings, zone_info
