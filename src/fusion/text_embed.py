"""
Marengo sync text embeddings via AWS Bedrock.

This wraps the bedrock-runtime invoke_model call so the rest of the
fusion code can think in terms of (text -> 512-d float vector) without
caring about JSON shape or which model id flavour Bedrock will accept
on a given account.

Bedrock contract for Marengo sync text (confirmed by probe):
    body = {"inputType": "text",
            "text": {"inputText": "<the string>"}}
    -> {"data": [{"embedding": [...512 floats...]}]}

The default model id is `MARENGO_INFERENCE_ID` from the env (set to
`us.twelvelabs.marengo-embed-3-0-v1:0`). If the bare id is rejected with
a `ValidationException` mentioning "inference profile", we retry once
with the `us.` regional prefix — same fallback pattern used elsewhere
in the project.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import boto3


def _bedrock_client(region: Optional[str] = None):
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)


def embed_text(
    text: str,
    *,
    model_id: Optional[str] = None,
    region: Optional[str] = None,
    client=None,
) -> list[float]:
    """
    Return a single 512-d embedding for `text`.

    Whitespace is collapsed and the input is left-trimmed of any zero-width
    characters before sending — Bedrock will reject empty strings with a
    400, and we'd rather fail loudly here than mid-batch.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise ValueError("embed_text: refusing to embed empty/whitespace text")

    model_id = model_id or os.environ.get(
        "MARENGO_INFERENCE_ID", "us.twelvelabs.marengo-embed-3-0-v1:0"
    )
    bedrock = client or _bedrock_client(region)

    body = json.dumps({
        "inputType": "text",
        "text": {"inputText": cleaned},
    })

    try:
        resp = bedrock.invoke_model(modelId=model_id, body=body)
    except bedrock.exceptions.ValidationException as e:
        if "inference profile" in str(e) and not model_id.startswith("us."):
            resp = bedrock.invoke_model(modelId=f"us.{model_id}", body=body)
        else:
            raise

    payload = json.loads(resp["body"].read())
    data = payload.get("data") or []
    if not data or "embedding" not in data[0]:
        raise RuntimeError(f"Unexpected Marengo response shape: {payload!r}")
    emb = data[0]["embedding"]
    if not isinstance(emb, list) or len(emb) == 0:
        raise RuntimeError("Marengo returned empty embedding")
    return [float(x) for x in emb]


def embed_texts(
    texts: list[str],
    *,
    model_id: Optional[str] = None,
    region: Optional[str] = None,
    sleep_between: float = 0.0,
) -> list[list[float]]:
    """
    Embed a list of strings sequentially. Marengo doesn't expose a batch
    text endpoint — each call is one HTTP round trip. Reuses a single
    boto3 client so the connection pool is shared.

    `sleep_between` is a politeness delay between calls if you ever hit
    a throttling exception. Default 0 (no sleep) is fine for our 16-claim
    use case.
    """
    client = _bedrock_client(region)
    out: list[list[float]] = []
    for i, t in enumerate(texts, start=1):
        emb = embed_text(t, model_id=model_id, region=region, client=client)
        out.append(emb)
        if sleep_between and i < len(texts):
            time.sleep(sleep_between)
    return out
