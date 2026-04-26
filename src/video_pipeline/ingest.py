"""
Video pipeline — ingest stage.

Two responsibilities live here:
  1. `upload_video` — push a local MP4 to S3 (M2 step 2.1).
  2. `start_video_embedding` — kick off a Marengo async embedding job
     against an S3 video URI (M2 step 2.4). Returns the invocation ARN
     immediately; the caller polls / fetches output later.
"""

from __future__ import annotations

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
