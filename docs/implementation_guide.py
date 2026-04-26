"""
DisasterFusion — Implementation Guide
Based on TwelveLabs Bedrock Workshop patterns.
This is your reference for actual API calls.
"""

import boto3
import json
import time
import uuid
import base64
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from sklearn.metrics.pairwise import cosine_similarity

# ============================================================
# SECTION 1: SETUP
# ============================================================

session = boto3.Session()
AWS_REGION = session.region_name  # Should be us-east-1
bedrock_client = session.client('bedrock-runtime')
s3_client = session.client('s3')
aws_account_id = session.client('sts').get_caller_identity()["Account"]

# Your S3 bucket (replace with actual)
S3_BUCKET = "your-disasterfusion-bucket"
S3_VIDEOS_PATH = "videos"
S3_EMBEDDINGS_PATH = "embeddings"

# Model IDs (from workshop — these are confirmed working)
MARENGO_MODEL_ID = 'twelvelabs.marengo-embed-3-0-v1:0'  # For async video
MARENGO_INFERENCE_ID = 'us.twelvelabs.marengo-embed-3-0-v1:0'  # For sync text/image
PEGASUS_MODEL_ID = 'us.twelvelabs.pegasus-1-2-v1:0'

# Disaster config (no hardcoded region — system is disaster-agnostic).
# `DISASTER_TYPE` is supplied by the user at runtime and drives the
# Pegasus prompt focus + severity mapping in disaster_types.yaml.
DISASTER_TYPE = "hurricane"


# ============================================================
# SECTION 2: VIDEO PIPELINE — Pegasus Analysis
# ============================================================

def analyze_video_with_pegasus(
    video_s3_key: str,
    disaster_type: str = "hurricane",
    streaming: bool = False
) -> dict:
    """
    Send video to Pegasus for damage analysis.
    Uses structured JSON output via responseFormat.

    Args:
        video_s3_key: S3 key of the video (e.g., "videos/katrina_01.mp4")
        disaster_type: Type of disaster for context-aware prompting
        streaming: Whether to use streaming response

    Returns:
        Parsed JSON response with damage findings
    """
    # Disaster-specific focus areas for the prompt
    focus = {
        "hurricane": (
            "wind damage vs storm surge vs flooding differentiation, "
            "waterline marks on structures, roof and siding damage, "
            "coastal erosion, submerged structures, debris fields, "
            "road accessibility, power line damage"
        ),
        "tornado": (
            "structural collapse patterns, roof removal, debris scatter "
            "direction, path width indicators, fallen trees"
        ),
        "flood": (
            "water depth indicators, submerged structures, silt/mud lines, "
            "structural displacement, road accessibility"
        ),
    }.get(disaster_type, "all visible damage to structures and infrastructure")

    prompt = f"""Analyze this {disaster_type} footage for damage assessment.
For each visually distinct damaged area or scene, identify:
- damage_type: the primary type of damage visible
- severity: how severe the damage is
- description: detailed description of what you observe
- structures_affected: estimated count of damaged structures
- building_type: type of structures affected
- infrastructure_impacts: list of infrastructure issues visible
- location_indicators: any text, signs, landmarks, building names visible

Focus on: {focus}

Return a JSON array of findings. Each finding should cover one
visually distinct damage area."""

    # JSON schema for structured output — THIS IS THE KEY FEATURE
    json_schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "damage_type": {
                            "type": "string",
                            "enum": [
                                "structural_collapse", "roof_damage",
                                "flooding", "debris_field",
                                "infrastructure_damage", "vegetation_damage",
                                "vehicle_damage", "fire_damage",
                                "erosion", "other"
                            ]
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["minor", "moderate", "severe", "destroyed"]
                        },
                        "description": {"type": "string"},
                        "structures_affected": {"type": "integer"},
                        "building_type": {
                            "type": "string",
                            "enum": [
                                "residential", "commercial", "industrial",
                                "public", "infrastructure", "unknown"
                            ]
                        },
                        "infrastructure_impacts": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "location_indicators": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": [
                        "damage_type", "severity", "description"
                    ]
                }
            }
        },
        "required": ["findings"]
    }

    request_body = {
        "inputPrompt": prompt,
        "mediaSource": {
            "s3Location": {
                "uri": f"s3://{S3_BUCKET}/{video_s3_key}",
                "bucketOwner": aws_account_id
            }
        },
        "temperature": 0,
        "responseFormat": {
            "jsonSchema": json_schema
        }
    }

    if streaming:
        response = bedrock_client.invoke_model_with_response_stream(
            modelId=PEGASUS_MODEL_ID,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json"
        )
        message = ""
        for event in response["body"]:
            chunk = json.loads(event["chunk"]["bytes"])
            message += chunk["message"]
            print(chunk["message"], end="")
        print()
        return json.loads(message)
    else:
        response = bedrock_client.invoke_model(
            modelId=PEGASUS_MODEL_ID,
            body=json.dumps(request_body),
            contentType="application/json",
            accept="application/json"
        )
        body = json.loads(response.get("body").read())
        return json.loads(body["message"])


# ============================================================
# SECTION 3: VIDEO PIPELINE — Marengo Video Embeddings
# ============================================================

def wait_for_async_embeddings(
    s3_bucket: str, s3_prefix: str, invocation_arn: str
) -> list:
    """Wait for async Marengo embedding task and retrieve results."""
    status = None
    while status not in ["Completed", "Failed", "Expired"]:
        response = bedrock_client.get_async_invoke(
            invocationArn=invocation_arn
        )
        status = response['status']
        print(f"  Embedding status: {status}")
        time.sleep(5)

    if status != "Completed":
        raise Exception(f"Embedding task failed: {status}")

    # Retrieve output from S3
    response = s3_client.list_objects_v2(
        Bucket=s3_bucket, Prefix=s3_prefix
    )
    for obj in response.get('Contents', []):
        if obj['Key'].endswith('output.json'):
            output = s3_client.get_object(
                Bucket=s3_bucket, Key=obj['Key']
            )
            content = output['Body'].read().decode('utf-8')
            return json.loads(content).get("data", [])

    raise Exception("No output.json found")


def create_video_embeddings(video_s3_key: str) -> list:
    """
    Create Marengo embeddings for a video.
    ASYNC — uploads to S3, waits for completion.

    Returns list of segments:
    [
        {
            "embedding": [float x 512],
            "startSec": 0.0,
            "endSec": 6.5,
            "embeddingOption": "visual",
            "embeddingScope": "clip"
        },
        ...
    ]
    """
    uid = uuid.uuid4()
    output_prefix = f'{S3_EMBEDDINGS_PATH}/{S3_VIDEOS_PATH}/{uid}'

    video_uri = f"s3://{S3_BUCKET}/{video_s3_key}"
    print(f"Creating video embeddings for: {video_uri}")

    response = bedrock_client.start_async_invoke(
        modelId=MARENGO_MODEL_ID,
        modelInput={
            "inputType": "video",
            "video": {
                "mediaSource": {
                    "s3Location": {
                        "uri": video_uri,
                        "bucketOwner": aws_account_id
                    }
                },
                "embeddingOption": ["visual"],
                "embeddingScope": ["clip"]
            }
        },
        outputDataConfig={
            "s3OutputDataConfig": {
                "s3Uri": f's3://{S3_BUCKET}/{output_prefix}'
            }
        }
    )

    arn = response["invocationArn"]
    print(f"  Task started: {arn}")

    segments = wait_for_async_embeddings(S3_BUCKET, output_prefix, arn)
    print(f"  ✅ Got {len(segments)} segments")
    return segments


# ============================================================
# SECTION 4: MARENGO TEXT EMBEDDINGS (for report claims)
# ============================================================

def create_text_embedding(text: str) -> list:
    """
    Create a 512-dim text embedding using Marengo.
    SYNC — returns immediately.
    """
    response = bedrock_client.invoke_model(
        modelId=MARENGO_INFERENCE_ID,
        body=json.dumps({
            "inputType": "text",
            "text": {"inputText": text}
        })
    )
    data = json.loads(response['body'].read().decode('utf-8'))['data']
    return data[0]["embedding"]  # 512-dim float list


# ============================================================
# SECTION 5: FUSION ENGINE — Report → Video (Pass A)
# ============================================================

def fuse_reports_to_video(
    claims: list[dict],
    video_segments: list[dict],
    video_source: str,
    similarity_threshold: float = 0.3
) -> list[dict]:
    """
    Pass A: For each report claim, find the best-matching video segment
    using Marengo text↔video cosine similarity.

    Args:
        claims: List of ReportClaim dicts (must have 'damage_description')
        video_segments: List from create_video_embeddings()
                        (must have 'embedding', 'startSec', 'endSec')
        video_source: Filename of the source video
        similarity_threshold: Minimum cosine similarity to consider a match

    Returns:
        List of FusedFinding dicts
    """
    results = []

    # Pre-extract video embedding matrix for batch similarity
    video_matrix = np.array([s["embedding"] for s in video_segments])

    for claim in claims:
        desc = claim.get("damage_description", "")
        if not desc:
            continue

        # Embed the claim description as text
        print(f"  Embedding claim: {claim.get('claim_id', '?')}")
        claim_embedding = create_text_embedding(desc[:500])  # Truncate
        claim_vec = np.array(claim_embedding).reshape(1, -1)

        # Cosine similarity against all video segments
        similarities = cosine_similarity(claim_vec, video_matrix)[0]
        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])

        segment = video_segments[best_idx]

        if best_score >= similarity_threshold:
            classification = "corroborated"  # Could refine with severity check
            confidence = compute_confidence(
                spatial=0.5,  # Default if no geo comparison possible
                semantic=best_score,
                severity_match=0.7,  # Default — refine later
                source_reliability=get_source_reliability(
                    claim.get("source_type", "other")
                )
            )
        else:
            classification = "unverified"
            confidence = 0.2

        results.append({
            "finding_id": f"ff-{uuid.uuid4().hex[:8]}",
            "classification": classification,
            "report_claim": claim,
            "video_match": {
                "source_video": video_source,
                "start_sec": segment["startSec"],
                "end_sec": segment["endSec"],
                "similarity_score": round(best_score, 4)
            } if best_score >= similarity_threshold else None,
            "confidence_score": round(confidence, 3),
            "lat": claim.get("lat"),
            "lon": claim.get("lon"),
            "location_source": "report"
        })

    return results


# ============================================================
# SECTION 6: FUSION ENGINE — Video → Report (Pass B)
# ============================================================

def fuse_video_to_reports(
    video_findings: list[dict],
    claims: list[dict],
    matched_claim_ids: set
) -> list[dict]:
    """
    Pass B: For each Pegasus video finding, check if any report
    claim covers the same damage. Uses LLM semantic similarity.

    Unmatched video findings → UNREPORTED
    """
    results = []

    for vf in video_findings:
        desc_a = vf.get("description", "")
        if not desc_a:
            continue

        best_claim = None
        best_score = 0.0

        for claim in claims:
            if claim.get("claim_id") in matched_claim_ids:
                continue

            desc_b = claim.get("damage_description", "")
            score = compute_semantic_similarity_llm(desc_a, desc_b)

            if score > best_score:
                best_score = score
                best_claim = claim

        if best_claim and best_score >= 0.5:
            results.append({
                "finding_id": f"ff-{uuid.uuid4().hex[:8]}",
                "classification": "corroborated",
                "video_finding": vf,
                "report_claim": best_claim,
                "confidence_score": round(best_score, 3),
                "lat": best_claim.get("lat"),
                "lon": best_claim.get("lon"),
                "location_source": "both"
            })
            matched_claim_ids.add(best_claim.get("claim_id"))
        else:
            results.append({
                "finding_id": f"ff-{uuid.uuid4().hex[:8]}",
                "classification": "unreported",
                "video_finding": vf,
                "report_claim": None,
                "confidence_score": 0.3,
                "lat": None,
                "lon": None,
                "location_source": None
            })

    return results


# ============================================================
# SECTION 7: HELPER FUNCTIONS
# ============================================================

def compute_confidence(
    spatial: float, semantic: float,
    severity_match: float, source_reliability: float
) -> float:
    """Weighted confidence score."""
    return min(max(
        0.30 * spatial +
        0.35 * semantic +
        0.20 * severity_match +
        0.15 * source_reliability
    , 0.0), 1.0)


def get_source_reliability(source_type: str) -> float:
    return {
        "fema_pda": 0.9, "fema_pw": 0.9,
        "nws_survey": 0.9, "city_report": 0.7,
        "news_media": 0.5, "other": 0.5
    }.get(source_type, 0.5)


def compute_semantic_similarity_llm(desc_a: str, desc_b: str) -> float:
    """
    Use Claude Haiku 4.5 (via AWS Bedrock) to rate similarity between two
    damage descriptions. Returns 0.0–1.0.
    """
    import os, json, boto3
    bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    model_id = os.environ.get("CLAUDE_MODEL_ID", "anthropic.claude-haiku-4-5-20251001-v1:0")

    prompt = f"""Rate the semantic similarity of these two damage descriptions
on a scale from 0.0 to 1.0:
- 1.0 = clearly the same damage at the same location
- 0.5 = related but could be different locations
- 0.0 = completely unrelated

Description A: "{desc_a[:300]}"
Description B: "{desc_b[:300]}"

Return ONLY a number between 0.0 and 1.0."""

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 10,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    })

    try:
        try:
            resp = bedrock.invoke_model(modelId=model_id, body=body)
        except bedrock.exceptions.ValidationException:
            # Fall back to regional inference profile prefix
            resp = bedrock.invoke_model(modelId=f"us.{model_id}", body=body)
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"].strip()
        return float(text)
    except Exception:
        return 0.0


def haversine_km(lat1, lon1, lat2, lon2):
    """Distance in km between two lat/lon points."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


# ============================================================
# SECTION 8: OUTPUT — Map Generation
# ============================================================

def build_map(fused_findings: list[dict], center=(29.95, -90.07)):
    """Build a Folium map with color-coded markers."""
    import folium

    colors = {
        "corroborated": "green",
        "discrepancy": "orange",
        "unreported": "red",
        "unverified": "gray"
    }
    icons = {
        "corroborated": "ok-sign",
        "discrepancy": "warning-sign",
        "unreported": "remove-sign",
        "unverified": "question-sign"
    }

    m = folium.Map(location=center, zoom_start=12)

    for f in fused_findings:
        lat, lon = f.get("lat"), f.get("lon")
        if not lat or not lon:
            continue

        cls = f.get("classification", "unverified")
        conf = f.get("confidence_score", 0)

        # Build popup HTML
        popup_parts = [f"<b>{cls.upper()}</b> (confidence: {conf:.2f})"]

        if f.get("report_claim"):
            rc = f["report_claim"]
            popup_parts.append(
                f"<br><b>Report:</b> {rc.get('damage_description', '')[:200]}"
            )

        if f.get("video_match"):
            vm = f["video_match"]
            popup_parts.append(
                f"<br><b>Video:</b> {vm['source_video']} "
                f"@ {vm['start_sec']:.1f}s–{vm['end_sec']:.1f}s "
                f"(similarity: {vm['similarity_score']:.3f})"
            )

        if f.get("video_finding"):
            vf = f["video_finding"]
            popup_parts.append(
                f"<br><b>Video finding:</b> {vf.get('description', '')[:200]}"
            )

        folium.Marker(
            location=[lat, lon],
            popup=folium.Popup("".join(popup_parts), max_width=400),
            icon=folium.Icon(
                color=colors.get(cls, "gray"),
                icon=icons.get(cls, "info-sign")
            )
        ).add_to(m)

    return m


def export_geojson(fused_findings: list[dict], filepath: str):
    """Export fused findings as GeoJSON."""
    features = []
    for f in fused_findings:
        lat, lon = f.get("lat"), f.get("lon")
        if not lat or not lon:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "finding_id": f.get("finding_id"),
                "classification": f.get("classification"),
                "confidence": f.get("confidence_score"),
                "location_source": f.get("location_source")
            }
        })

    geojson = {"type": "FeatureCollection", "features": features}
    with open(filepath, 'w') as fp:
        json.dump(geojson, fp, indent=2)
    print(f"Exported {len(features)} features to {filepath}")


# ============================================================
# SECTION 9: MAIN PIPELINE — Run Everything
# ============================================================

def run_pipeline(
    video_s3_keys: list[str],
    report_claims: list[dict],
    disaster_type: str = "hurricane"
):
    """
    Full DisasterFusion pipeline.
    """
    all_video_findings = []
    all_video_segments = {}  # key: video_s3_key, value: segments

    # --- Step 1: Pegasus analysis on each video ---
    print("=" * 60)
    print("STEP 1: Pegasus Video Analysis")
    print("=" * 60)
    for vkey in video_s3_keys:
        print(f"\nAnalyzing: {vkey}")
        result = analyze_video_with_pegasus(vkey, disaster_type)
        findings = result.get("findings", [])
        for f in findings:
            f["source_video"] = vkey
        all_video_findings.extend(findings)
        print(f"  → {len(findings)} findings extracted")

    # --- Step 2: Marengo embeddings for each video ---
    print("\n" + "=" * 60)
    print("STEP 2: Marengo Video Embeddings")
    print("=" * 60)
    all_segments = []
    for vkey in video_s3_keys:
        print(f"\nEmbedding: {vkey}")
        segments = create_video_embeddings(vkey)
        for s in segments:
            s["source_video"] = vkey
        all_segments.extend(segments)

    # --- Step 3: Fusion Pass A — Report → Video ---
    print("\n" + "=" * 60)
    print("STEP 3: Fusion Pass A (Report → Video)")
    print("=" * 60)
    pass_a_results = fuse_reports_to_video(
        report_claims, all_segments, "all_videos"
    )
    print(f"\nPass A: {len(pass_a_results)} fused findings")

    # Track which claims were matched
    matched_ids = set()
    for r in pass_a_results:
        if r["classification"] == "corroborated" and r.get("report_claim"):
            matched_ids.add(r["report_claim"].get("claim_id"))

    # --- Step 4: Fusion Pass B — Video → Report ---
    print("\n" + "=" * 60)
    print("STEP 4: Fusion Pass B (Video → Report)")
    print("=" * 60)
    pass_b_results = fuse_video_to_reports(
        all_video_findings, report_claims, matched_ids
    )
    print(f"\nPass B: {len(pass_b_results)} fused findings")

    # --- Step 5: Merge & Output ---
    all_fused = pass_a_results + pass_b_results

    # Summary stats
    from collections import Counter
    cls_counts = Counter(f["classification"] for f in all_fused)
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"Total fused findings: {len(all_fused)}")
    for cls, count in cls_counts.items():
        print(f"  {cls}: {count}")

    corroborated = cls_counts.get("corroborated", 0)
    total_claims = len(report_claims)
    if total_claims > 0:
        print(f"Corroboration rate: {corroborated}/{total_claims} "
              f"({corroborated/total_claims*100:.0f}%)")

    # Build map
    m = build_map(all_fused)
    m.save("exports/damage_map.html")
    print("\nMap saved to exports/damage_map.html")

    # Export GeoJSON
    export_geojson(all_fused, "exports/damage_assessment.geojson")

    # Save full results as JSON
    with open("data/processed/fused_findings.json", 'w') as fp:
        json.dump(all_fused, fp, indent=2, default=str)
    print("Full results saved to data/processed/fused_findings.json")

    return all_fused


# ============================================================
# USAGE EXAMPLE
# ============================================================

if __name__ == "__main__":
    # Mock claims (replace with the report parser's actual output)
    mock_claims = [
        {
            "claim_id": "rc-001",
            "source_type": "fema_pw",
            "location_name": "300 Howard Avenue, New Orleans",
            "lat": 29.9430, "lon": -90.0715,
            "damage_description": (
                "Pumping Station PS-1 submerged under 8 feet of water. "
                "Turbine pump engines, electrical switchgear destroyed."
            ),
            "severity": "destroyed",
            "damage_category": "utilities",
        },
        {
            "claim_id": "rc-002",
            "source_type": "fema_pw",
            "location_name": "Tulane & Broad, New Orleans",
            "lat": 29.9688, "lon": -90.0853,
            "damage_description": (
                "Pumping Station PS-3 severe flood damage. "
                "Emergency electrical bypass required."
            ),
            "severity": "destroyed",
            "damage_category": "utilities",
        },
    ]

    # Run the full pipeline
    results = run_pipeline(
        video_s3_keys=["videos/katrina_aerial_01.mp4"],
        report_claims=mock_claims,
        disaster_type="hurricane"
    )
