"""
M5 — Fusion Pass B runner.

Pipeline:
  1. Load 10 Pegasus video findings (`video_findings.json`).
  2. Load 16 FEMA report claims (`report_claims.json`, geocoded).
  3. Load Pass A matches (`pass_a_matches.json`) — used for evidence chains
     (the top Marengo timestamps per claim get attached to each fused row).
  4. Embed all finding descriptions and claim damage descriptions via
     Marengo sync text — text-vs-text cosines, much better calibrated
     than the cross-modal cosines Pass A used.
  5. Build the (F x C) similarity matrix.
  6. Call `pass_b.fuse(...)` -> classify each (claim, finding) pair as
     corroborated / discrepancy / unverified, plus emit `unreported`
     rows for findings no claim picked up.
  7. Write `data/processed/fused_findings.json` and print a summary
     so the run is self-documenting.

This script is read-only on its inputs.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.fusion.pass_b import fuse, text_similarity_matrix
from src.fusion.text_embed import embed_texts


FINDINGS_PATH = Path("data/processed/video_findings.json")
CLAIMS_PATH   = Path("data/processed/report_claims.json")
PASS_A_PATH   = Path("data/processed/pass_a_matches.json")
OUT_PATH      = Path("data/processed/fused_findings.json")


def _finding_text(f: dict) -> str:
    """Pegasus already gives us a clean prose description — use it as-is.
    Prepending the damage type makes the embedding lean a bit toward that
    semantic axis, which helps separate flooding-vs-structural cases.
    """
    parts: list[str] = []
    if f.get("damage_type"):
        parts.append(f["damage_type"].replace("_", " "))
    if f.get("description"):
        parts.append(f["description"])
    return ". ".join(parts).strip() or f.get("finding_id", "")


def _claim_text(c: dict) -> str:
    """Mirror the finding text shape: lead with category, then the FEMA
    free-text description. We deliberately do NOT include location_name
    here — Pass B is about *what happened*, not *where*. Location is
    handled separately by spatial proximity downstream.
    """
    parts: list[str] = []
    cat = c.get("damage_type") or c.get("damage_category")
    if cat:
        parts.append(cat.replace("_", " "))
    if c.get("damage_description"):
        parts.append(c["damage_description"])
    return ". ".join(parts).strip() or c.get("claim_id", "")


def main() -> int:
    load_dotenv()

    for p in (FINDINGS_PATH, CLAIMS_PATH):
        if not p.is_file():
            print(f"Missing {p} — earlier milestone hasn't been run.")
            return 1

    findings = json.loads(FINDINGS_PATH.read_text())
    claims   = json.loads(CLAIMS_PATH.read_text())
    pass_a   = json.loads(PASS_A_PATH.read_text()) if PASS_A_PATH.is_file() else None

    print(
        f"Loaded {len(findings)} Pegasus findings and "
        f"{len(claims)} FEMA claims."
    )
    if pass_a:
        print(
            f"Pass A doc: {pass_a.get('claim_count')} claims, "
            f"{pass_a.get('segment_count')} segments, "
            f"min_score={pass_a.get('min_score')}, "
            f"top_k={pass_a.get('top_k_per_modality')}."
        )
    else:
        print(f"No {PASS_A_PATH} — evidence chains will be empty.")

    f_texts = [_finding_text(f) for f in findings]
    c_texts = [_claim_text(c)   for c in claims]

    total_calls = len(f_texts) + len(c_texts)
    print(f"Embedding {total_calls} strings via Marengo sync text...")
    t0 = time.time()
    f_vecs = embed_texts(f_texts)
    c_vecs = embed_texts(c_texts)
    dur = time.time() - t0
    print(
        f"Got {len(f_vecs)}+{len(c_vecs)} embeddings "
        f"(dim={len(f_vecs[0]) if f_vecs else 0}) in {dur:.1f}s "
        f"-> {dur / max(total_calls, 1) * 1000:.0f} ms/string avg."
    )

    sims = text_similarity_matrix(f_vecs, c_vecs)
    print(
        f"Text-text similarity matrix shape={sims.shape}, "
        f"min={sims.min():.3f}, mean={sims.mean():.3f}, max={sims.max():.3f}."
    )

    result = fuse(findings, claims, sims, pass_a_doc=pass_a)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2))
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUT_PATH} ({size_kb:.0f} KB).")

    s = result["stats"]
    print()
    print(
        f"Pass B summary "
        f"(corroborated>={result['corroborated_threshold']}, "
        f"discrepancy>={result['discrepancy_threshold']}):"
    )
    print(
        f"  corroborated: {s['corroborated']:>2d}  "
        f"discrepancy: {s['discrepancy']:>2d}  "
        f"unverified: {s['unverified']:>2d}  "
        f"unreported: {s['unreported']:>2d}  "
        f"(total fused rows: {s['total_fused']})"
    )
    print()
    print(
        f"  {'class':14s} {'score':>6s}  "
        f"{'cat':>5s} {'sev':>5s} {'txt':>5s}  "
        f"{'src':6s} loc"
    )
    for row in result["findings"]:
        b = row.get("confidence_breakdown") or {}
        claim = row.get("report_claim") or {}
        loc = (claim.get("location_name") or "")[:50]
        if not claim and row.get("video_finding"):
            f = row["video_finding"]
            loc = f"[unreported] {f.get('damage_type')} / {f.get('severity')}"
        print(
            f"  {row['classification']:14s} "
            f"{row['confidence_score']:>6.3f}  "
            f"{b.get('category', 0):>5.2f} "
            f"{b.get('severity', 0):>5.2f} "
            f"{b.get('text_similarity', 0):>5.2f}  "
            f"{(row.get('location_source') or '-'):6s}{loc}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
