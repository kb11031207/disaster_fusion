"""
M2 step 2.3 — wrap raw Pegasus output into VideoFinding dataclasses.

Philosophy: flag don't drop. We never throw away a finding because it
looks bad — we set is_valid=False, append the reason to validation_errors,
and let downstream code filter if it wants clean data only.

Pegasus is JSON-schema-constrained on the way out, so most enum failures
shouldn't be possible. The checks below are defense in depth.
"""

from __future__ import annotations

from typing import Any

from src.shared.models import VideoFinding
from src.shared.utils import generate_id


_DAMAGE_TYPES = {
    "structural_collapse", "roof_damage", "flooding",
    "debris_field", "infrastructure_damage", "vegetation_damage",
    "vehicle_damage", "fire_damage", "erosion", "other",
}
_SEVERITIES = {"minor", "moderate", "severe", "destroyed"}
_BUILDING_TYPES = {
    "residential", "commercial", "industrial",
    "public", "infrastructure", "unknown",
}

_DESCRIPTION_MIN_CHARS = 10
_DESCRIPTION_MAX_CHARS = 2000


def _validate_one(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a sanitised dict + a list of validation_errors (may be empty)."""
    errors: list[str] = []
    out: dict[str, Any] = {}

    # damage_type — required
    dt = raw.get("damage_type")
    if not dt:
        errors.append("missing damage_type")
        out["damage_type"] = "other"
    elif dt not in _DAMAGE_TYPES:
        errors.append(f"unknown damage_type {dt!r}")
        out["damage_type"] = "other"
    else:
        out["damage_type"] = dt

    # severity — required
    sev = raw.get("severity")
    if not sev:
        errors.append("missing severity")
        out["severity"] = "moderate"
    elif sev not in _SEVERITIES:
        errors.append(f"unknown severity {sev!r}")
        out["severity"] = "moderate"
    else:
        out["severity"] = sev

    # description — required, length-checked
    desc = (raw.get("description") or "").strip()
    if len(desc) < _DESCRIPTION_MIN_CHARS:
        errors.append(f"description too short ({len(desc)} chars)")
    elif len(desc) > _DESCRIPTION_MAX_CHARS:
        errors.append(f"description too long ({len(desc)} chars), truncated")
        desc = desc[:_DESCRIPTION_MAX_CHARS]
    out["description"] = desc

    # structures_affected — optional int
    sa = raw.get("structures_affected")
    if sa is not None:
        if isinstance(sa, bool) or not isinstance(sa, int):
            errors.append(f"structures_affected not int: {sa!r}")
            out["structures_affected"] = None
        elif sa < 0:
            errors.append(f"structures_affected negative: {sa}")
            out["structures_affected"] = None
        else:
            out["structures_affected"] = sa
    else:
        out["structures_affected"] = None

    # building_type — optional, enum if present
    bt = raw.get("building_type")
    if bt is None:
        out["building_type"] = None
    elif bt not in _BUILDING_TYPES:
        errors.append(f"unknown building_type {bt!r}")
        out["building_type"] = "unknown"
    else:
        out["building_type"] = bt

    # list-typed optional fields
    for list_field in ("infrastructure_impacts", "location_indicators"):
        val = raw.get(list_field)
        if val is None:
            out[list_field] = []
        elif isinstance(val, list) and all(isinstance(x, str) for x in val):
            out[list_field] = val
        else:
            errors.append(f"{list_field} not list[str]: {val!r}")
            out[list_field] = []

    return out, errors


def validate_findings(
    raw_response: dict[str, Any],
    source_video: str,
    capture_date: str,
) -> list[VideoFinding]:
    """
    Convert raw Pegasus response into a list of VideoFinding dataclasses.

    Pegasus does not return per-finding timestamps in our current prompt,
    so timestamp_start/_end are placeholder zeros. Real per-segment time
    ranges come from Marengo embeddings in step 2.4.
    """
    raw_findings = raw_response.get("findings") or []
    findings: list[VideoFinding] = []

    for raw in raw_findings:
        clean, errors = _validate_one(raw)
        finding = VideoFinding(
            finding_id=generate_id("vf"),
            source_video=source_video,
            timestamp_start=0.0,
            timestamp_end=0.0,
            capture_date=capture_date,
            capture_date_source="user_supplied",
            damage_type=clean["damage_type"],
            severity=clean["severity"],
            description=clean["description"],
            structures_affected=clean["structures_affected"],
            building_type=clean["building_type"],
            location_indicators=clean["location_indicators"],
            infrastructure_impacts=clean["infrastructure_impacts"],
            is_valid=(len(errors) == 0),
            validation_errors=errors,
        )
        findings.append(finding)

    return findings
