"""
Video pipeline — ingest stage.

Three responsibilities live here:
  1. `upload_video` — push a local MP4 to S3 (M2 step 2.1).
  2. `start_video_embedding` — kick off a Marengo async embedding job
     against an S3 video URI (M2 step 2.4). Returns the invocation ARN
     immediately; the caller polls / fetches output later.
  3. `fetch_video_embeddings` — given a job record from (2), download the
     Marengo `output.json` from S3 and reshape it into the project's
     VideoSegment rows for downstream fusion.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

import boto3


def upload_video(local_path: str | Path, capture_date: str) -> dict[str, Any]:
    """
    Upload a local MP4 to S3 under the configured videos prefix.

    The function is idempotent in the sense that uploading the same file
    twice just overwrites the object — there is no "skip if exists" check
    because we want to be able to re-run after re-encoding the source.

    Returns a dict the rest of the pipeline can pass around:
        {
            "s3_uri":       "s3://bucket/videos/foo.mp4",
            "capture_date": "2005-09-01",
            "source_video": "foo.mp4",
            "size_bytes":   59_842_115,
        }
    """
    local = Path(local_path).resolve()
    if not local.is_file():
        raise FileNotFoundError(f"Video not found: {local}")

    bucket = os.environ["S3_BUCKET"]
    videos_prefix = os.environ.get("S3_VIDEOS_PATH", "videos").strip("/")
    region = os.environ.get("AWS_REGION", "us-east-1")

    key = f"{videos_prefix}/{local.name}"
    s3 = boto3.client("s3", region_name=region)

    local_size = local.stat().st_size
    print(
        f"Uploading {local.name} "
        f"({local_size / 1_000_000:.1f} MB) "
        f"-> s3://{bucket}/{key}"
    )
    s3.upload_file(str(local), bucket, key)

    # Confirm the object actually landed and the byte count matches.
    head = s3.head_object(Bucket=bucket, Key=key)
    remote_size = head["ContentLength"]
    if remote_size != local_size:
        raise RuntimeError(
            f"Size mismatch after upload: local={local_size} s3={remote_size}"
        )

    return {
        "s3_uri": f"s3://{bucket}/{key}",
        "capture_date": capture_date,
        "source_video": local.name,
        "size_bytes": remote_size,
    }


def generate_presigned_url(s3_uri: str, expiry_seconds: int = 3600) -> str:
    """
    Generate a presigned URL for an S3 object so the frontend can stream
    the video directly without needing AWS credentials.

    Args:
        s3_uri:         e.g. "s3://bucket/videos/foo.mp4"
        expiry_seconds: how long the URL stays valid (default 1 hour)

    Returns:
        HTTPS presigned URL string
    """
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Expected s3:// URI, got: {s3_uri}")

    without_prefix = s3_uri[len("s3://"):]
    bucket, key    = without_prefix.split("/", 1)
    region         = os.environ.get("AWS_REGION", "us-east-1")

    s3  = boto3.client("s3", region_name=region)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry_seconds,
    )
    return url


def start_video_embedding(
    s3_uri: str,
    source_video: str,
    embedding_options: Optional[list[str]] = None,
    account_id: Optional[str] = None,
    region: Optional[str] = None,
) -> dict[str, Any]:
    """
    Kick off a Marengo async embedding job for a video already in S3.

    Marengo writes per-segment 512-dim vectors to S3 as JSON. Each
    requested modality in `embedding_options` produces its own set of
    segment vectors — visual+audio means roughly 2x the segments in
    the output file vs visual alone.

    Returns immediately with the invocation_arn and where output will
    land. Polling and fetching live in a separate function/script.

    `embedding_options` defaults to ["visual", "audio"]. Use ["visual"]
    only if you want to skip audio.
    """
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    model_id = os.environ.get(
        "MARENGO_MODEL_ID", "twelvelabs.marengo-embed-3-0-v1:0"
    )
    bucket = os.environ["S3_BUCKET"]
    embeddings_prefix = os.environ.get(
        "S3_EMBEDDINGS_PATH", "embeddings"
    ).strip("/")

    if embedding_options is None:
        embedding_options = ["visual", "audio"]

    # Stem the filename so each video gets its own output sub-prefix.
    stem = Path(source_video).stem
    output_prefix = f"{embeddings_prefix}/{stem}"
    output_s3_uri = f"s3://{bucket}/{output_prefix}/"

    if account_id is None:
        account_id = boto3.client(
            "sts", region_name=region
        ).get_caller_identity()["Account"]

    bedrock = boto3.client("bedrock-runtime", region_name=region)

    print(f"Starting Marengo async embed:")
    print(f"  input:    {s3_uri}")
    print(f"  output:   {output_s3_uri}")
    print(f"  options:  {embedding_options}")

    response = bedrock.start_async_invoke(
        modelId=model_id,
        modelInput={
            "inputType": "video",
            "video": {
                "mediaSource": {
                    "s3Location": {
                        "uri": s3_uri,
                        "bucketOwner": account_id,
                    }
                },
                "embeddingOption": embedding_options,
                "embeddingScope": ["clip"],
            },
        },
        outputDataConfig={
            "s3OutputDataConfig": {"s3Uri": output_s3_uri}
        },
    )

    invocation_arn = response["invocationArn"]
    print(f"  arn:      {invocation_arn}")

    return {
        "invocation_arn": invocation_arn,
        "input_s3_uri": s3_uri,
        "output_s3_uri": output_s3_uri,
        "source_video": source_video,
        "embedding_options": embedding_options,
        "model_id": model_id,
    }


def _segment_id(source_video: str, modality: str, start_sec: float) -> str:
    """
    Stable 8-hex segment id derived from the (video, modality, startSec)
    triple. Stable across re-runs as long as Marengo returns the same
    segmentation, which lets us re-link fused findings to the same row.
    """
    raw = f"{source_video}|{modality}|{round(start_sec, 3)}".encode()
    return "vs-" + hashlib.sha1(raw).hexdigest()[:8]


def fetch_video_embeddings(
    job: dict[str, Any],
    region: Optional[str] = None,
) -> dict[str, Any]:
    """
    Download the Marengo async output for a started job and reshape it
    into the project's VideoSegment row format.

    Marengo's raw output looks like:
        {"data": [{"embedding": [...512 floats...],
                   "embeddingOption": "visual" | "audio",
                   "embeddingScope": "clip",
                   "startSec": 0.0,
                   "endSec":   4.75}, ...]}

    We rewrite each row to:
        {"segment_id":   "vs-<8 hex>",
         "source_video": "<file>.mp4",
         "modality":     "visual" | "audio",
         "start_sec":    0.0,
         "end_sec":      4.75,
         "embedding":    [...512 floats...]}

    The job record is the dict returned by `start_video_embedding`. The
    invocation ARN's tail is used as the S3 sub-prefix (Bedrock writes
    output to <prefix>/<arn-tail>/output.json).

    Returns:
        {"source_video":   "...",
         "model_id":       "...",
         "segment_count":  309,           # distinct (start_sec) values
         "embedding_dim":  512,
         "modalities":     ["visual","audio"],
         "segments":       [ ...VideoSegment rows... ]}
    """
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)

    output_uri = job["output_s3_uri"]
    if not output_uri.startswith("s3://"):
        raise ValueError(f"Bad output_s3_uri: {output_uri!r}")
    bucket, _, prefix = output_uri[len("s3://"):].partition("/")
    if not prefix.endswith("/"):
        prefix += "/"

    # Bedrock async writes to <prefix>/<job-id>/output.json. The job-id is
    # the tail of the invocation ARN. Listing is more robust than building
    # the path ourselves in case Bedrock's layout drifts.
    listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    keys = [obj["Key"] for obj in listing.get("Contents", [])
            if obj["Key"].endswith("output.json")]
    if not keys:
        raise FileNotFoundError(
            f"No output.json under s3://{bucket}/{prefix} — "
            f"is the job actually Completed?"
        )
    if len(keys) > 1:
        # Pick the one matching this ARN's tail; otherwise warn.
        tail = job["invocation_arn"].rsplit("/", 1)[-1]
        match = [k for k in keys if f"/{tail}/" in k]
        if match:
            keys = match
        else:
            print(f"WARNING: multiple output.json keys, using first: {keys}")

    output_key = keys[0]
    print(f"Fetching s3://{bucket}/{output_key}")
    obj = s3.get_object(Bucket=bucket, Key=output_key)
    raw = json.loads(obj["Body"].read())
    rows = raw.get("data", [])
    if not rows:
        raise RuntimeError("Marengo output had empty 'data' array")

    source_video = job["source_video"]
    segments: list[dict[str, Any]] = []
    embedding_dim: Optional[int] = None
    modalities: set[str] = set()
    starts: set[float] = set()

    for r in rows:
        modality = r["embeddingOption"]
        start = float(r["startSec"])
        end = float(r["endSec"])
        emb = r["embedding"]
        if embedding_dim is None:
            embedding_dim = len(emb)
        elif len(emb) != embedding_dim:
            raise ValueError(
                f"Inconsistent embedding dim: expected {embedding_dim}, "
                f"got {len(emb)} at startSec={start}"
            )
        modalities.add(modality)
        starts.add(round(start, 3))
        segments.append({
            "segment_id":   _segment_id(source_video, modality, start),
            "source_video": source_video,
            "modality":     modality,
            "start_sec":    start,
            "end_sec":      end,
            "embedding":    emb,
        })

    # Sort: time-ordered, with modalities grouped per timestamp so a fusion
    # writer can iterate visual+audio for the same clip side-by-side.
    segments.sort(key=lambda s: (s["start_sec"], s["modality"]))

    return {
        "source_video":  source_video,
        "model_id":      job.get("model_id"),
        "segment_count": len(starts),
        "embedding_dim": embedding_dim,
        "modalities":    sorted(modalities),
        "segments":      segments,
    }
