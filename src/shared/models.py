"""
Shared dataclasses for DisasterFusion.

These three classes are the data contract between every pipeline stage:
    Video Pipeline   -> VideoFinding[]   -> Fusion Engine
    Report Parser    -> ReportClaim[]    -> Fusion Engine
    Fusion Engine    -> FusedFinding[]   -> Output Layer

Each class has `to_dict()` for JSON serialization and `from_dict()` for
loading back. The round-trip preserves every field, including the `geo`
tuple in VideoFinding (JSON has no tuple type, so we convert list -> tuple
on load) and nested VideoFinding / ReportClaim inside FusedFinding.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional


def _known_fields_only(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys so an extra field in JSON doesn't crash construction."""
    allowed = {f.name for f in fields(cls)}
    return {k: v for k, v in raw.items() if k in allowed}


@dataclass
class VideoFinding:
    """A single damage observation extracted from video by Pegasus."""

    finding_id: str
    source_video: str
    timestamp_start: float
    timestamp_end: float

    capture_date: Optional[str] = None
    capture_date_source: Optional[str] = None

    damage_type: str = "other"
    severity: str = "moderate"
    damage_description: str = ""
    structures_affected: Optional[int] = None
    building_type: Optional[str] = None
    building_name: Optional[str] = None

    location_indicators: list[str] = field(default_factory=list)
    named_entities: list[str] = field(default_factory=list)
    geo: Optional[tuple[float, float]] = None
    geo_method: Optional[str] = None
    geo_confidence: str = "unresolved"

    infrastructure_impacts: list[str] = field(default_factory=list)
    visual_evidence_quality: str = "unknown"

    is_valid: bool = True
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VideoFinding":
        data = _known_fields_only(cls, raw)
        # JSON has no tuple type — list comes back from json.loads, convert.
        if data.get("geo") is not None:
            data["geo"] = tuple(data["geo"])
        return cls(**data)


@dataclass
class ReportClaim:
    """A single damage claim extracted from an official report."""

    claim_id: str
    source_document: str
    source_type: str
    location_name: str

    lat: Optional[float] = None
    lon: Optional[float] = None
    county_parish: Optional[str] = None
    state: Optional[str] = None

    # Set by the geocoder (src/report_parser/geocoder.py).
    # geo_confidence  in {"high","medium","low","unresolved"} or None pre-geocode
    # geo_source      e.g. "overture_places", "overture_county",
    #                 "overture_region", "unresolved"
    geo_confidence: Optional[str] = None
    geo_source: Optional[str] = None

    damage_description: str = ""
    severity: str = "moderate"
    damage_type: Optional[str] = None
    cost_estimate: Optional[float] = None
    ef_rating: Optional[str] = None

    event_type: Optional[str] = None
    event_name: Optional[str] = None
    event_date: Optional[str] = None
    report_date: Optional[str] = None

    building_type: Optional[str] = None
    building_name: Optional[str] = None
    structures_affected: Optional[int] = None
    infrastructure_impacts: list[str] = field(default_factory=list)

    is_valid: bool = True
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReportClaim":
        return cls(**_known_fields_only(cls, raw))


@dataclass
class FusedFinding:
    """A finding after fusion — links video evidence to report claims."""

    finding_id: str
    classification: str  # "corroborated" | "discrepancy" | "unreported" | "unverified"

    video_finding: Optional[VideoFinding] = None
    report_claim: Optional[ReportClaim] = None

    confidence_score: float = 0.0
    confidence_breakdown: dict[str, float] = field(default_factory=dict)

    lat: Optional[float] = None
    lon: Optional[float] = None
    location_source: Optional[str] = None  # "report" | "video" | "both"
    event_date: Optional[str] = None

    discrepancy_type: Optional[str] = None
    discrepancy_detail: Optional[str] = None

    evidence_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FusedFinding":
        data = _known_fields_only(cls, raw)
        vf = data.get("video_finding")
        if isinstance(vf, dict):
            data["video_finding"] = VideoFinding.from_dict(vf)
        rc = data.get("report_claim")
        if isinstance(rc, dict):
            data["report_claim"] = ReportClaim.from_dict(rc)
        return cls(**data)
