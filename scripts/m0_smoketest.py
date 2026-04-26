"""
M0 smoke test: confirm we can reach AWS Bedrock and call Marengo's
sync text-embedding endpoint. If this works, the rest of the pipeline
is plumbing.

Usage:
    python scripts/m0_smoketest.py
"""

import json
import os
import sys

import boto3
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()  # reads .env from current working directory

    region = os.getenv("AWS_REGION", "us-east-1")
    model_id = os.getenv(
        "MARENGO_INFERENCE_ID", "us.twelvelabs.marengo-embed-3-0-v1:0"
    )

    print(f"Region:   {region}")
    print(f"Model ID: {model_id}")

    bedrock = boto3.client("bedrock-runtime", region_name=region)

    body = json.dumps({"inputType": "text", "text": {"inputText": "test"}})

    try:
        response = bedrock.invoke_model(modelId=model_id, body=body)
    except Exception as exc:
        print(f"FAIL: Bedrock invoke_model raised: {type(exc).__name__}: {exc}")
        return 1

    payload = json.loads(response["body"].read().decode("utf-8"))
    embedding = payload["data"][0]["embedding"]

    if not isinstance(embedding, list) or len(embedding) != 512:
        print(f"FAIL: expected 512-dim list, got {type(embedding).__name__} "
              f"of length {len(embedding) if hasattr(embedding, '__len__') else '?'}")
        return 1

    print(f"OK: got {len(embedding)} floats (first 4: {embedding[:4]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
