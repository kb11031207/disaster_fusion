"""
M4 — Fusion Pass A: text-to-video cosine similarity.

Given a list of ReportClaim text embeddings and the VideoSegment vectors
from `data/processed/video_segments.json`, score every (claim, segment)
pair with cosine similarity and emit the top-K matches per claim per
modality (visual + audio).

This is the cheap, no-LLM stage. Pass B (M5) will re-rank surviving
pairs with spatial proximity + a Claude semantic check.

Schema of the per-claim match row:
    {
      "claim_id":      "rc-aa4c9c56",
      "claim_summary": "Charity Hospital, 1532 Tulane Avenue ...",
      "claim_severity":"destroyed",
      "visual": [{"segment_id":"vs-...", "start_sec":..., "end_sec":...,
                  "score":0.42}, ...],
      "audio":  [...],
      "stats":  {"visual_max":..., "audio_max":...,
                 "visual_above_thr":N, "audio_above_thr":N,
                 "best_modality":"visual"|"audio"|null}
    }
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalize. Zero rows stay zero (no NaN)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return matrix / norms


def _split_segments_by_modality(
    segments: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group raw segment rows by modality, preserving order."""
    by_mod: dict[str, list[dict[str, Any]]] = {}
    for s in segments:
        by_mod.setdefault(s["modality"], []).append(s)
    return by_mod


_DEFAULT_FLOORS = {"visual": 0.05, "audio": 0.03}


def run_pass_a(
    claim_embeddings: list[list[float]],
    claims: list[dict[str, Any]],
    video_segments_doc: dict[str, Any],
    *,
    min_score: dict[str, float] | float | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """
    Compute Pass A matches.

    Args:
        claim_embeddings:    one 512-d vector per claim, same order as claims
        claims:              the ReportClaim dicts (we only read claim_id,
                             damage_description, severity)
        video_segments_doc:  the dict loaded from video_segments.json
                             (top-level: source_video, segments, ...)
        min_score:           soft noise floor — drop matches below this.
                             Either a single float (applies to all
                             modalities) or a dict keyed by modality.
                             Defaults to {visual:0.05, audio:0.03},
                             calibrated to where Marengo cross-modal
                             cosines stop being meaningful in practice.
                             (Marengo packs cross-modal pairs into a
                             narrow band — visual peaks ~0.15, audio
                             ~0.08 — so 0.3-style thresholds wipe out
                             real matches.)
        top_k:               keep at most this many matches per modality
                             after thresholding.

    Returns the full Pass A result dict, ready to be JSON-dumped.
    """
    if min_score is None:
        floors = dict(_DEFAULT_FLOORS)
    elif isinstance(min_score, (int, float)):
        floors = {"visual": float(min_score), "audio": float(min_score)}
    else:
        floors = dict(_DEFAULT_FLOORS)
        floors.update({k: float(v) for k, v in min_score.items()})
    if len(claim_embeddings) != len(claims):
        raise ValueError(
            f"claim_embeddings ({len(claim_embeddings)}) and claims "
            f"({len(claims)}) length mismatch"
        )

    segments = video_segments_doc["segments"]
    if not segments:
        raise ValueError("video_segments_doc.segments is empty")

    by_mod = _split_segments_by_modality(segments)

    C = _l2_normalize(np.asarray(claim_embeddings, dtype=np.float32))

    # Pre-build, per modality, both the matrix M (n_segments x dim) and
    # the parallel list of segment metadata so we can map row index back
    # to a segment dict cheaply.
    per_mod_matrix: dict[str, np.ndarray] = {}
    per_mod_segments: dict[str, list[dict[str, Any]]] = {}
    for mod, rows in by_mod.items():
        per_mod_matrix[mod] = _l2_normalize(
            np.asarray([r["embedding"] for r in rows], dtype=np.float32)
        )
        per_mod_segments[mod] = rows

    matches_out: list[dict[str, Any]] = []

    for ci, claim in enumerate(claims):
        c_vec = C[ci:ci + 1]  # shape (1, dim)
        per_mod_results: dict[str, list[dict[str, Any]]] = {}
        per_mod_max: dict[str, float] = {}
        per_mod_count: dict[str, int] = {}

        for mod, M in per_mod_matrix.items():
            floor = floors.get(mod, 0.0)
            # cosine similarity row vector (n_segments,)
            sims = (c_vec @ M.T).reshape(-1)
            order = np.argsort(-sims)  # descending
            kept: list[dict[str, Any]] = []
            for idx in order:
                score = float(sims[idx])
                if score < floor:
                    break
                if len(kept) >= top_k:
                    break
                seg = per_mod_segments[mod][int(idx)]
                kept.append({
                    "segment_id": seg["segment_id"],
                    "start_sec":  seg["start_sec"],
                    "end_sec":    seg["end_sec"],
                    "score":      round(score, 4),
                })
            per_mod_results[mod] = kept
            per_mod_max[mod] = float(sims.max()) if sims.size else 0.0
            per_mod_count[mod] = int((sims >= floor).sum())

        # Decide best modality (the one with the higher top score),
        # but only if at least one match cleared threshold.
        best_mod = None
        best_score = -1.0
        for mod, kept in per_mod_results.items():
            if kept and kept[0]["score"] > best_score:
                best_score = kept[0]["score"]
                best_mod = mod

        row: dict[str, Any] = {
            "claim_id":       claim["claim_id"],
            "claim_summary":  (claim.get("location_name") or "")[:120],
            "claim_severity": claim.get("severity"),
            "stats": {
                **{f"{m}_max": round(per_mod_max[m], 4) for m in per_mod_max},
                **{f"{m}_above_thr": per_mod_count[m] for m in per_mod_count},
                "best_modality": best_mod,
            },
        }
        for mod, kept in per_mod_results.items():
            row[mod] = kept

        matches_out.append(row)

    return {
        "min_score":           floors,
        "top_k_per_modality":  top_k,
        "source_video":        video_segments_doc.get("source_video"),
        "claim_count":         len(claims),
        "segment_count":       video_segments_doc.get("segment_count"),
        "modalities":          sorted(by_mod.keys()),
        "matches":             matches_out,
    }
