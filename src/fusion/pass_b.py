"""
M5 — Fusion Pass B: pair Pegasus video findings with report claims.

Design note (2026-04-26): the original architecture had Pass B call Claude
per (finding, claim) pair. We replaced that with a no-LLM tiered scorer
because Pegasus has *already* extracted structured findings — there's no
need to re-interpret raw segments.

Tiered matching (per docs/extraction_protocols.md "Matching Priority"):

  TIER 1 — STRONG SIGNALS (short-circuit to high confidence)
    1a. building_name / named_entity match
        Pegasus reads "Drifters" off a sign; the report says
        "Drifters Eats and Drinks". Strongest possible signal.
    1b. spatial proximity (real telemetry only — simulated coords are
        within the disaster zone by construction and would falsely
        match every claim).

  TIER 2 — MEDIUM SIGNALS (weighted blend, used always)
    - category (damage_type)         weight 0.35
    - severity                       weight 0.15
    - building_type                  weight 0.10
    - text similarity (Marengo)      weight 0.40

A pair's final score = max(strong-signal score, medium blend). Strong
signals don't replace the medium blend — they raise the floor, so a
name-matched pair always lands above the corroborated threshold even
if the categorical signals happen to disagree.

Classification thresholds:
  >= 0.50  -> corroborated
  >= 0.30  -> discrepancy
  <  0.30  -> unverified

Findings no claim picks up emit as `unreported`. Pass A's top-K Marengo
timestamps become the evidence chain on the FusedFinding.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Optional

import numpy as np


# -- Scoring weights --------------------------------------------------------

WEIGHTS = {
    "category":      0.35,
    "severity":      0.15,
    "building_type": 0.10,
    "text":          0.40,
}

CORROBORATED_THRESHOLD = 0.50
DISCREPANCY_THRESHOLD  = 0.30

# Strong-signal floors. A pair that hits one of these can't fall below
# this score regardless of categorical disagreement — the assumption is
# that name-matching or close-spatial agreement is itself decisive.
NAME_MATCH_FLOOR    = 0.85
SPATIAL_MATCH_FLOOR = 0.80

# Spatial: full credit within 200 m, linear decay to zero by 2 km.
SPATIAL_FULL_KM = 0.2
SPATIAL_ZERO_KM = 2.0

# Geo methods we DO trust for spatial matching. Simulated coords scatter
# every finding inside the disaster zone, so they'd false-positive every
# claim — exclude them.
_TRUSTED_GEO_METHODS = {"telemetry", "exif", "gps"}


# -- Categorical scoring ---------------------------------------------------

# Exact-match damage type pairs are the strong signal. A small set of
# related-but-not-equal pairs gets partial credit because they often
# describe the same physical event from different perspectives — e.g.
# a collapsed bridge IS infrastructure damage AND structural collapse.
_RELATED_TYPES: dict[tuple[str, str], float] = {
    ("structural_collapse", "infrastructure_damage"): 0.5,
    ("infrastructure_damage", "structural_collapse"): 0.5,
    ("flooding", "infrastructure_damage"):            0.4,
    ("infrastructure_damage", "flooding"):            0.4,
    ("debris_field", "structural_collapse"):          0.4,
    ("structural_collapse", "debris_field"):          0.4,
    ("debris_field", "infrastructure_damage"):        0.3,
    ("infrastructure_damage", "debris_field"):        0.3,
}


def _category_score(finding_type: Optional[str],
                    claim_category: Optional[str]) -> float:
    """0.0–1.0. Exact match = 1.0, related = partial, otherwise 0."""
    if not finding_type or not claim_category:
        return 0.0
    if finding_type == claim_category:
        return 1.0
    return _RELATED_TYPES.get((finding_type, claim_category), 0.0)


# -- Severity scoring ------------------------------------------------------

# Ordinal scale; gap is computed in tiers.
_SEV_RANK = {
    "destroyed": 4, "severe": 3, "major": 3,
    "moderate": 2, "minor": 1, "affected": 1,
}


def _severity_score(s_finding: Optional[str],
                    s_claim: Optional[str]) -> float:
    """1.0 if same tier, 0.5 if 1-tier gap, 0.0 if 2+."""
    if not s_finding or not s_claim:
        return 0.0
    a = _SEV_RANK.get(s_finding.lower())
    b = _SEV_RANK.get(s_claim.lower())
    if a is None or b is None:
        return 0.0
    gap = abs(a - b)
    if gap == 0:
        return 1.0
    if gap == 1:
        return 0.5
    return 0.0


# -- Building-type scoring -------------------------------------------------

def _building_type_score(b_finding: Optional[str],
                         b_claim: Optional[str]) -> float:
    """1.0 same, 0.0 different. Unknown on either side -> 0.0 (no info)."""
    if not b_finding or not b_claim:
        return 0.0
    if b_finding == "unknown" or b_claim == "unknown":
        return 0.0
    return 1.0 if b_finding == b_claim else 0.0


# -- Name matching (strong signal) -----------------------------------------

def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _name_match_score(finding: dict[str, Any],
                      claim: dict[str, Any]) -> float:
    """
    Strong-signal name match. Tries multiple shapes:
      1.0  finding.building_name overlaps claim.building_name
      0.95 finding.building_name appears inside claim.location_name
      0.90 any finding.named_entities[i] overlaps claim.building_name
      0.80 any finding.named_entities[i] appears inside claim.location_name
      0.0  otherwise
    Overlap = case-insensitive substring either direction, min 3 chars.
    """
    fb = _norm(finding.get("building_name"))
    cb = _norm(claim.get("building_name"))
    cl = _norm(claim.get("location_name"))
    fnes = [_norm(x) for x in (finding.get("named_entities") or []) if x]
    fnes = [n for n in fnes if len(n) >= 3]

    def overlap(a: str, b: str) -> bool:
        return bool(a) and bool(b) and len(a) >= 3 and len(b) >= 3 and (a in b or b in a)

    if fb and cb and overlap(fb, cb):
        return 1.0
    if fb and cl and len(fb) >= 3 and fb in cl:
        return 0.95
    if cb and any(overlap(n, cb) for n in fnes):
        return 0.90
    if cl and any(n in cl for n in fnes):
        return 0.80
    return 0.0


# -- Spatial proximity (strong signal, real geo only) ----------------------

def _haversine_km(lat1: float, lon1: float,
                  lat2: float, lon2: float) -> float:
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _spatial_score(finding: dict[str, Any],
                   claim: dict[str, Any]) -> float:
    """
    1.0 within SPATIAL_FULL_KM, linearly decaying to 0 at SPATIAL_ZERO_KM.

    Skipped entirely for simulated video coords — they're scattered inside
    the disaster zone and would false-positive every claim.
    """
    geo = finding.get("geo")
    if not geo or len(geo) != 2:
        return 0.0
    if finding.get("geo_method") not in _TRUSTED_GEO_METHODS:
        return 0.0
    lat = claim.get("lat")
    lon = claim.get("lon")
    if lat is None or lon is None:
        return 0.0
    d_km = _haversine_km(float(geo[0]), float(geo[1]), float(lat), float(lon))
    if d_km <= SPATIAL_FULL_KM:
        return 1.0
    if d_km >= SPATIAL_ZERO_KM:
        return 0.0
    return 1.0 - (d_km - SPATIAL_FULL_KM) / (SPATIAL_ZERO_KM - SPATIAL_FULL_KM)


# -- Text similarity -------------------------------------------------------

def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


def text_similarity_matrix(
    finding_embeds: list[list[float]],
    claim_embeds: list[list[float]],
) -> np.ndarray:
    """Returns (F, C) matrix of cosines between Pegasus + FEMA descriptions."""
    F = _l2_normalize(np.asarray(finding_embeds, dtype=np.float32))
    C = _l2_normalize(np.asarray(claim_embeds,   dtype=np.float32))
    return F @ C.T


# -- The fusion routine ----------------------------------------------------

def _pair_score(
    finding: dict[str, Any],
    claim: dict[str, Any],
    text_sim: float,
) -> tuple[float, dict[str, float]]:
    """Return (final_score, breakdown) for one (finding, claim) pair."""
    # Medium-signal weighted blend (always computed).
    cat = _category_score(finding.get("damage_type"),  claim.get("damage_type"))
    sev = _severity_score(finding.get("severity"),     claim.get("severity"))
    bt  = _building_type_score(finding.get("building_type"),
                               claim.get("building_type"))
    txt = max(0.0, min(1.0, float(text_sim)))
    medium = (
        WEIGHTS["category"]      * cat
        + WEIGHTS["severity"]    * sev
        + WEIGHTS["building_type"] * bt
        + WEIGHTS["text"]        * txt
    )

    # Strong-signal short circuits — raise the floor only.
    name_score    = _name_match_score(finding, claim)
    spatial_score = _spatial_score(finding, claim)

    floor = 0.0
    if name_score >= 0.80:
        # Anything from 0.80 -> 1.0 maps onto NAME_MATCH_FLOOR -> 1.0.
        scaled = NAME_MATCH_FLOOR + (name_score - 0.80) / 0.20 * (1.0 - NAME_MATCH_FLOOR)
        floor = max(floor, scaled)
    if spatial_score >= 0.5:
        scaled = SPATIAL_MATCH_FLOOR + (spatial_score - 0.5) / 0.5 * (1.0 - SPATIAL_MATCH_FLOOR)
        floor = max(floor, scaled)

    score = max(medium, floor)

    breakdown = {
        "category":        round(WEIGHTS["category"]      * cat, 4),
        "severity":        round(WEIGHTS["severity"]      * sev, 4),
        "building_type":   round(WEIGHTS["building_type"] * bt, 4),
        "text_similarity": round(WEIGHTS["text"]          * txt, 4),
        "name_match":      round(name_score, 4),
        "spatial":         round(spatial_score, 4),
    }
    return float(score), breakdown


def _ff_id(*parts: str) -> str:
    raw = "|".join(p or "" for p in parts).encode()
    return "ff-" + hashlib.sha1(raw).hexdigest()[:8]


def _classify(score: float) -> str:
    if score >= CORROBORATED_THRESHOLD:
        return "corroborated"
    if score >= DISCREPANCY_THRESHOLD:
        return "discrepancy"
    return "unverified"


def _discrepancy_kind(
    finding: dict[str, Any], claim: dict[str, Any],
    breakdown: dict[str, float],
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort label for *why* something is a discrepancy."""
    cat_match = (finding.get("damage_type") == claim.get("damage_type"))
    sev_match = (finding.get("severity") == claim.get("severity"))
    if not cat_match:
        return ("category_mismatch",
                f"video says {finding.get('damage_type')!r} "
                f"vs report {claim.get('damage_type')!r}")
    if not sev_match:
        return ("severity_mismatch",
                f"video says {finding.get('severity')!r} "
                f"vs report {claim.get('severity')!r}")
    return ("description_divergence",
            "category and severity agree but descriptions don't strongly match")


def _evidence_segments_for_claim(
    claim_id: str,
    pass_a_doc: Optional[dict[str, Any]],
    max_per_modality: int = 3,
) -> list[dict[str, Any]]:
    """Pull Pass A timestamps for a given claim_id."""
    if not pass_a_doc:
        return []
    row = next(
        (r for r in pass_a_doc.get("matches", []) if r["claim_id"] == claim_id),
        None,
    )
    if not row:
        return []
    out: list[dict[str, Any]] = []
    for mod in ("visual", "audio"):
        for m in (row.get(mod) or [])[:max_per_modality]:
            out.append({
                "segment_id": m["segment_id"],
                "start_sec":  m["start_sec"],
                "end_sec":    m["end_sec"],
                "score":      m["score"],
                "modality":   mod,
            })
    out.sort(key=lambda x: -x["score"])
    return out


def _location_source(
    claim: Optional[dict[str, Any]],
    finding: Optional[dict[str, Any]],
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Pick the best available coords; report which side they came from."""
    claim_has = (
        claim is not None
        and claim.get("lat") is not None
        and claim.get("lon") is not None
    )
    video_has = (
        finding is not None
        and finding.get("geo") is not None
    )
    if claim_has and video_has:
        return claim["lat"], claim["lon"], "both"
    if claim_has:
        return claim["lat"], claim["lon"], "report"
    if video_has:
        return finding["geo"][0], finding["geo"][1], "video"
    return None, None, None


def _evidence_summary(
    classification: str,
    finding: Optional[dict[str, Any]],
    claim: Optional[dict[str, Any]],
    score: float,
    evidence_segments: list[dict[str, Any]],
) -> str:
    """Short one-paragraph human-readable explanation."""
    parts: list[str] = []
    if claim:
        loc = (claim.get("location_name") or "")[:80]
        parts.append(
            f"FEMA ({claim.get('source_document')}): "
            f"{claim.get('damage_type') or '?'} / "
            f"{claim.get('severity') or '?'} at {loc}"
        )
    if finding:
        parts.append(
            f"Pegasus: {finding.get('damage_type') or '?'} / "
            f"{finding.get('severity') or '?'} — "
            f"\"{(finding.get('damage_description') or '')[:120]}\""
        )
    if evidence_segments:
        ts = ", ".join(
            f"{s['start_sec']/60:.0f}:{int(s['start_sec']%60):02d}"
            for s in evidence_segments[:3]
        )
        parts.append(f"Top supporting clips: {ts}")
    parts.append(f"classification={classification} score={score:.2f}")
    return " | ".join(parts)


def fuse(
    findings: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    text_sims: np.ndarray,
    pass_a_doc: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Run Pass B fusion. Returns the document ready to dump as
    `fused_findings.json`.

    `text_sims` shape (len(findings), len(claims)).
    """
    if text_sims.shape != (len(findings), len(claims)):
        raise ValueError(
            f"text_sims shape {text_sims.shape} doesn't match "
            f"findings/claims sizes {(len(findings), len(claims))}"
        )

    # For each claim, find best-matching finding.
    claim_best: list[tuple[int, float, dict[str, float]]] = []
    for ci, claim in enumerate(claims):
        best_idx = -1
        best_score = -1.0
        best_break: dict[str, float] = {}
        for fi, finding in enumerate(findings):
            score, breakdown = _pair_score(finding, claim, float(text_sims[fi, ci]))
            if score > best_score:
                best_score = score
                best_idx = fi
                best_break = breakdown
        claim_best.append((best_idx, best_score, best_break))

    # Track which findings ever got picked above the discrepancy floor.
    finding_used: set[int] = set()

    fused: list[dict[str, Any]] = []

    # Pass 1 — emit one row per claim.
    for ci, claim in enumerate(claims):
        fi, score, breakdown = claim_best[ci]
        finding = findings[fi] if fi >= 0 else None
        classification = _classify(score)

        # Below discrepancy floor -> we don't really have a video match.
        if classification == "unverified":
            finding = None
            breakdown = {k: 0.0 for k in breakdown} or {
                "category": 0.0, "severity": 0.0, "text_similarity": 0.0
            }
        else:
            finding_used.add(fi)

        evidence = _evidence_segments_for_claim(claim["claim_id"], pass_a_doc)
        lat, lon, loc_src = _location_source(claim, finding)

        disc_type = disc_detail = None
        if classification == "discrepancy":
            disc_type, disc_detail = _discrepancy_kind(finding, claim, breakdown)

        fused.append({
            "finding_id": _ff_id(claim["claim_id"],
                                 finding["finding_id"] if finding else "none"),
            "classification": classification,
            "confidence_score": round(score, 4),
            "confidence_breakdown": breakdown,
            "report_claim": claim,
            "video_finding": finding,
            "lat": lat,
            "lon": lon,
            "location_source": loc_src,
            "event_date": (claim.get("event_date")
                           or (finding.get("capture_date") if finding else None)),
            "discrepancy_type": disc_type,
            "discrepancy_detail": disc_detail,
            "evidence_summary": _evidence_summary(
                classification, finding, claim, score, evidence
            ),
            "evidence_segments": evidence,
        })

    # Pass 2 — emit "unreported" rows for any Pegasus finding never claimed.
    for fi, finding in enumerate(findings):
        if fi in finding_used:
            continue
        fused.append({
            "finding_id": _ff_id("none", finding["finding_id"]),
            "classification": "unreported",
            "confidence_score": 0.0,
            "confidence_breakdown": {
                "category": 0.0, "severity": 0.0, "text_similarity": 0.0
            },
            "report_claim": None,
            "video_finding": finding,
            "lat": (finding.get("geo") or [None, None])[0],
            "lon": (finding.get("geo") or [None, None])[1],
            "location_source": "video" if finding.get("geo") else None,
            "event_date": finding.get("capture_date"),
            "discrepancy_type": None,
            "discrepancy_detail": None,
            "evidence_summary": _evidence_summary(
                "unreported", finding, None, 0.0, []
            ),
            "evidence_segments": [],
        })

    counts = {
        "corroborated": sum(1 for r in fused if r["classification"] == "corroborated"),
        "discrepancy":  sum(1 for r in fused if r["classification"] == "discrepancy"),
        "unverified":   sum(1 for r in fused if r["classification"] == "unverified"),
        "unreported":   sum(1 for r in fused if r["classification"] == "unreported"),
    }

    return {
        "weights":                  WEIGHTS,
        "corroborated_threshold":   CORROBORATED_THRESHOLD,
        "discrepancy_threshold":    DISCREPANCY_THRESHOLD,
        "stats": {
            **counts,
            "total_claims":   len(claims),
            "total_findings": len(findings),
            "total_fused":    len(fused),
        },
        "findings": fused,
    }
