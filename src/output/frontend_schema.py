"""
Output transformer — maps our internal fused_findings shape to the
frontend contract defined in docs/BACKEND_CONTRACT.md.

Single entry point:
    transform(fused_doc) -> dict

The returned dict has shape:
    {
      "center": [lat, lon],   # centroid of all findings — frontend uses
      "zoom":   int,          #   this to position the map on load
      "findings": [ ...Finding objects per contract... ]
    }

This is the ONLY place that knows about the frontend field names. If the
contract changes, update here and nowhere else.
"""

from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enum maps
# ---------------------------------------------------------------------------

_CLASSIFICATION_TO_STATUS = {
    "corroborated": "confirmed",
    "discrepancy":  "conflicting_severity",
    "unverified":   "uncertain",
    "unreported":   "unreported_damage",
}

_BUILDING_TYPE_TO_FACILITY = {
    "commercial":   "commercial_plaza",
    "residential":  "residential_multifamily",
    "public":       "community_center",
    "infrastructure": "infrastructure_bridge",
    # industrial / agricultural / unknown pass through — UI falls back gracefully
}

_SEVERITY_RECOMMENDATIONS = {
    ("confirmed",            "destroyed"): "Coordinate immediate debris removal and safety inspection. Structure is a total loss.",
    ("confirmed",            "severe"):    "Building is unusable. Arrange structural engineering assessment before re-entry.",
    ("confirmed",            "moderate"):  "Significant damage present. Restrict access until repairs are assessed.",
    ("confirmed",            "minor"):     "Cosmetic damage. Monitor for secondary issues; normal operations likely resumable.",
    ("conflicting_severity", "destroyed"): "CONFLICT: Report and video severity disagree. Treat as destroyed until reconciled.",
    ("conflicting_severity", "severe"):    "CONFLICT: Report and video severity disagree. Manual verification required.",
    ("conflicting_severity", "moderate"):  "CONFLICT: Severity assessments differ. Field inspection recommended.",
    ("conflicting_severity", "minor"):     "CONFLICT: Video suggests more damage than the report states. Verify on-site.",
    ("unreported_damage",    "destroyed"): "UNREPORTED: Video shows total loss not captured in official reports. File supplemental claim immediately.",
    ("unreported_damage",    "severe"):    "UNREPORTED: Significant damage visible on video with no corresponding report entry.",
    ("unreported_damage",    "moderate"):  "UNREPORTED: Moderate damage observed on video. Consider adding to official assessment.",
    ("unreported_damage",    "minor"):     "UNREPORTED: Minor damage visible on video not yet reported.",
    ("uncertain",            None):        "Insufficient evidence to assess. Manual review recommended.",
}


def _recommendation(fusion_status: str, severity: Optional[str]) -> str:
    key = (fusion_status, severity)
    if key in _SEVERITY_RECOMMENDATIONS:
        return _SEVERITY_RECOMMENDATIONS[key]
    # Fallback for any combo not explicitly listed
    if fusion_status == "unreported_damage":
        return "UNREPORTED: Damage visible on video with no matching report entry."
    if fusion_status == "conflicting_severity":
        return "CONFLICT: Video and report assessments differ. Manual reconciliation required."
    if fusion_status == "uncertain":
        return "Insufficient evidence. Manual review recommended."
    return "Review finding and verify current operational status."


def _secs_to_hhmmss(secs: float) -> str:
    s = int(secs)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _entity_name(rc: Optional[dict], vf: Optional[dict]) -> str:
    """Best human-readable name for the finding."""
    if rc:
        name = rc.get("building_name") or ""
        if name:
            return name
        loc = rc.get("location_name") or ""
        return loc.split(",")[0].strip() or loc
    if vf:
        name = vf.get("building_name") or ""
        if name:
            return name
        desc = vf.get("damage_description") or ""
        return desc[:60].rstrip() + ("…" if len(desc) > 60 else "")
    return "Unknown"


def _video_block(vf: Optional[dict], pass_a_segments: list) -> Optional[dict]:
    if not vf:
        return None

    # Use the best Pass A segment timestamp if available, else fallback to finding's own.
    ts_start = vf.get("timestamp_start", 0.0) or 0.0
    ts_end   = vf.get("timestamp_end",   0.0) or 0.0
    if pass_a_segments:
        best = pass_a_segments[0]
        ts_start = best.get("start_sec", ts_start)
        ts_end   = best.get("end_sec",   ts_end)

    dt = vf.get("damage_type", "damage").replace("_", " ")
    desc = vf.get("damage_description", "")

    return {
        "source":              vf.get("source_video", ""),
        "timestamp_start":     _secs_to_hhmmss(ts_start),
        "timestamp_end":       _secs_to_hhmmss(ts_end),
        "summary":             desc[:200] if desc else "No description available.",
        "clip_url":            None,
        "marengo_query":       f"{dt} {desc[:80]}".strip(),
        "pegasus_description": desc,
    }


def _pdf_block(rc: Optional[dict]) -> Optional[dict]:
    if not rc:
        return None
    return {
        "source":           rc.get("source_document", ""),
        "page":             1,          # we parse full-doc text, not per-page
        "excerpt":          rc.get("damage_description", ""),
        "claimed_severity": rc.get("severity", "moderate"),
    }


def _overture_block(rc: Optional[dict], vf: Optional[dict]) -> Optional[dict]:
    """
    Build an OvertureMatch when we have high-confidence geocoding.
    Returns null for simulated video coords (they aren't real Overture hits).
    """
    if rc and rc.get("geo_source") not in (None, "unresolved", "simulated_within_disaster_zone"):
        name = rc.get("building_name") or rc.get("location_name", "")
        bt   = rc.get("building_type") or "unknown"
        conf_map = {"high": 0.90, "medium": 0.65, "low": 0.40}
        conf = conf_map.get(rc.get("geo_confidence") or "low", 0.40)
        return {
            "id":               f"grafton-{rc.get('claim_id', 'unknown')}",
            "name":             name,
            "category":         bt,
            "geometry_type":    "place",
            "match_method":     rc.get("geo_source", "manual"),
            "match_confidence": conf,
        }
    return None


def _fusion_block(row: dict) -> dict:
    bd = row.get("confidence_breakdown") or {}
    return {
        "spatial_score":  bd.get("spatial") or None,
        "semantic_score": bd.get("text_similarity"),
        "temporal_score": None,          # not computed — renders as "n/a"
        "severity_score": bd.get("severity"),
        "final_score":    row.get("confidence_score", 0.0),
        "reasoning":      row.get("evidence_summary", ""),
    }


def _lat_lon(row: dict) -> tuple[Optional[float], Optional[float]]:
    """
    Pick best coordinates. Report claim coords first (real geocoding);
    fall back to video finding geo (may be simulated — still displayable).
    """
    rc = row.get("report_claim")
    vf = row.get("video_finding")
    if rc and rc.get("lat") is not None and rc.get("lon") is not None:
        return rc["lat"], rc["lon"]
    if vf:
        geo = vf.get("geo")
        if geo and len(geo) == 2:
            return geo[0], geo[1]
    return None, None


def _transform_one(row: dict) -> Optional[dict]:
    """Transform one internal fused row to the frontend Finding shape."""
    rc = row.get("report_claim")
    vf = row.get("video_finding")

    lat, lon = _lat_lon(row)
    if lat is None or lon is None:
        return None   # can't render a marker — skip

    classification = row.get("classification", "unverified")
    fusion_status  = _CLASSIFICATION_TO_STATUS.get(classification, "uncertain")

    # Severity — prefer report claim; fall back to video finding.
    severity = (
        (rc.get("severity") if rc else None)
        or (vf.get("severity") if vf else None)
        or "moderate"
    )

    # building_type → facility_type
    bt = (
        (rc.get("building_type") if rc else None)
        or (vf.get("building_type") if vf else None)
        or "unknown"
    )
    facility_type = _BUILDING_TYPE_TO_FACILITY.get(bt, bt)

    evidence_segs = row.get("evidence_segments") or []

    return {
        "id":             row.get("finding_id", ""),
        "entity_name":    _entity_name(rc, vf),
        "aliases":        list(vf.get("named_entities") or []) if vf else [],
        "facility_type":  facility_type,
        "address":        (rc.get("location_name") if rc else None) or "",
        "lat":            lat,
        "lon":            lon,
        "final_severity": severity,
        "fusion_status":  fusion_status,
        "confidence":     row.get("confidence_score", 0.0),
        "video":          _video_block(vf, evidence_segs),
        "pdf":            _pdf_block(rc),
        "overture":       _overture_block(rc, vf),
        "fusion":         _fusion_block(row),
        "recommendation": _recommendation(fusion_status, severity),
    }


def transform(fused_doc: dict) -> dict:
    """
    Transform the full fused_findings document to the frontend contract shape.

    Returns:
        {
          "center": [lat, lon],   # centroid of all renderable findings
          "zoom":   15,
          "findings": [ ...Finding objects... ]
        }

    The `center` field lets the frontend position the map dynamically
    instead of relying on a hardcoded location.
    """
    raw_findings = fused_doc.get("findings") or []
    findings: list[dict] = []
    lats: list[float] = []
    lons: list[float] = []

    for row in raw_findings:
        out = _transform_one(row)
        if out is None:
            continue
        findings.append(out)
        lats.append(out["lat"])
        lons.append(out["lon"])

    center = (
        [round(sum(lats) / len(lats), 6), round(sum(lons) / len(lons), 6)]
        if lats else [0.0, 0.0]
    )

    return {
        "center":   center,
        "zoom":     15,
        "findings": findings,
    }
