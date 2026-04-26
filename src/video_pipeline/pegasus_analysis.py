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
                "structural_collapse", "roof_damage", "flooding",
                "debris_field", "infrastructure_damage", "vegetation_damage",
                "vehicle_damage", "fire_damage", "erosion", "other",
            ],
        },
        "severity": {
            "type": "string",
            "enum": ["minor", "moderate", "severe", "destroyed"],
        },
        "description": {"type": "string"},
        "structures_affected": {"type": "integer"},
        "building_type": {
            "type": "string",
            "enum": [
                "residential", "commercial", "industrial",
                "public", "infrastructure", "unknown",
            ],
        },
        "infrastructure_impacts": {
            "type": "array",
            "items": {"type": "string"},
        },
        "location_indicators": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["damage_type", "severity", "description"],
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
        f"Analyze this {disaster_type} footage for damage assessment.\n"
        "For each visually distinct damaged area or scene, identify:\n"
        "- damage_type: the primary type of damage visible\n"
        "- severity: how severe the damage is\n"
        "- description: detailed description of what you observe.\n"
        "  Where applicable, use damage-assessment vocabulary such as\n"
        '  "destroyed", "uninhabitable", "total loss", "structural damage",\n'
        '  "major damage", "minor damage" so the description is comparable\n'
        "  to official damage reports.\n"
        "- structures_affected: estimated count of damaged structures\n"
        "- building_type: type of structures affected\n"
        "- infrastructure_impacts: list of infrastructure issues visible\n"
        "- location_indicators: any text, signs, landmarks, building names visible\n"
        "\n"
        f"Focus on: {focus}\n"
        "\n"
        "Return a JSON object with a 'findings' array. Each finding should "
        "cover one visually distinct damage area."
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
