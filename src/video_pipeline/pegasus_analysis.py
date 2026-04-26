"""
Pegasus analysis — send a video at an S3 URI to TwelveLabs Pegasus 1.2
on Bedrock and get a structured list of damage findings back.

This module returns RAW Pegasus output (already JSON-parsed). Validation
(schema checks, enum checks, length checks) lives in `validation.py`
and runs in step 2.3.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import boto3

from src.shared.config import load_disaster_config


# Per-finding JSON schema. Pegasus cannot return values outside these enums.
_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "damage_type": {
            "type": "string",
            "enum": [
                "structural_collapse", "roof_damage", "debris_field",
                "vegetation_damage", "infrastructure_damage", "vehicle_damage",
                "window_door_damage", "flooding", "other",
            ],
        },
        "severity": {
            "type": "string",
            "enum": ["minor", "moderate", "severe", "destroyed"],
        },
        "damage_description": {"type": "string"},
        "structures_affected": {"type": "integer"},
        "building_type": {
            "type": "string",
            "enum": [
                "residential", "commercial", "industrial", "public",
                "infrastructure", "agricultural", "unknown",
            ],
        },
        "building_name": {"type": ["string", "null"]},
        "named_entities": {
            "type": "array",
            "items": {"type": "string"},
        },
        "infrastructure_impacts": {
            "type": "array",
            "items": {"type": "string"},
        },
        "location_indicators": {
            "type": "array",
            "items": {"type": "string"},
        },
        "visual_evidence_quality": {
            "type": "string",
            "enum": ["clear", "partial", "poor"],
        },
    },
    "required": ["damage_type", "severity", "damage_description"],
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": _FINDING_SCHEMA},
    },
    "required": ["findings"],
}


def _build_prompt(disaster_type: str) -> str:
    """Compose the disaster-aware Pegasus prompt from disaster_types.yaml."""
    cfg = load_disaster_config(disaster_type)
    focus = cfg["pegasus_focus"].strip()

    return (
        f"Analyze this {disaster_type} damage footage. For each visually "
        "distinct damaged area or structure, identify:\n"
        "- damage_type: one of [structural_collapse, roof_damage, debris_field,\n"
        "  vegetation_damage, infrastructure_damage, vehicle_damage,\n"
        "  window_door_damage, flooding, other]\n"
        "- severity: one of [minor, moderate, severe, destroyed]\n"
        "- damage_description: detailed description of visible damage. Use\n"
        '  damage-assessment vocabulary ("destroyed", "uninhabitable",\n'
        '  "total loss", "structural damage", "major damage", "minor damage")\n'
        "  so descriptions are comparable to official reports.\n"
        "- building_type: one of [residential, commercial, industrial, public,\n"
        "  infrastructure, agricultural, unknown]\n"
        "- building_name: any visible business name, sign, or identifier\n"
        "  (null if not visible). Read signs literally — a sign that says\n"
        '  "DRIFTERS" yields building_name "Drifters".\n'
        "- structures_affected: estimated count of damaged structures visible\n"
        "- location_indicators: visible text, street signs, landmarks, or\n"
        "  geographic features that could help identify the location\n"
        "- named_entities: proper nouns visible or mentioned in audio —\n"
        "  business names, organization names, place names. Separate from\n"
        "  location_indicators (those are geographic clues; these are names).\n"
        "- infrastructure_impacts: list of infrastructure issues visible\n"
        '  (e.g. "road blocked by debris", "power lines down")\n'
        "- visual_evidence_quality: one of [clear, partial, poor]\n"
        "  (clear = damage clearly visible, partial = partially obscured,\n"
        "  poor = hard to assess from footage)\n"
        "\n"
        f"Focus on: {focus}\n"
        "\n"
        "Return a JSON object with a 'findings' array. Each finding covers "
        "ONE visually distinct damage area — do not combine unrelated damage "
        "into one finding."
    )


def analyze_video(
    s3_uri: str,
    disaster_type: str,
    account_id: Optional[str] = None,
    region: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run Pegasus 1.2 on a video stored in S3.

    Returns the raw parsed Pegasus response, e.g.
        {"findings": [ {damage_type, severity, description, ...}, ... ]}

    `account_id` is needed for the S3 `bucketOwner` field. If not given,
    we look it up via STS get-caller-identity.
    """
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    model_id = os.environ.get(
        "PEGASUS_MODEL_ID", "us.twelvelabs.pegasus-1-2-v1:0"
    )

    if account_id is None:
        account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

    bedrock = boto3.client("bedrock-runtime", region_name=region)

    request_body = {
        "inputPrompt": _build_prompt(disaster_type),
        "mediaSource": {
            "s3Location": {
                "uri": s3_uri,
                "bucketOwner": account_id,
            }
        },
        "temperature": 0,
        "responseFormat": {"jsonSchema": _RESPONSE_SCHEMA},
    }

    print(f"Sending {s3_uri} to {model_id} ...")
    response = bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(request_body),
        contentType="application/json",
        accept="application/json",
    )

    body = json.loads(response["body"].read().decode("utf-8"))
    # Pegasus puts the model's text/JSON output in body["message"].
    return json.loads(body["message"])
