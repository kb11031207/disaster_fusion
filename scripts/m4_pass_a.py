"""
M4 — Fusion Pass A runner.

Pipeline:
  1. Load 16 ReportClaims from `data/processed/report_claims.json`.
  2. For each claim, build the embedding text by concatenating
     location_name + damage_description (location often carries the
     uniquely-identifying words like "Charity Hospital" that audio
     narration might also pick up).
  3. Embed all claim texts via Marengo sync text -> 512-d vectors.
  4. Load `data/processed/video_segments.json` (618 rows).
  5. Run cosine fusion (`run_pass_a`) with threshold=0.3, top_k=5.
  6. Write `data/processed/pass_a_matches.json` and print a short
     per-claim summary so the run is self-documenting.

This script is read-only on its inputs — it never modifies report_claims
or video_segments.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.fusion.pass_a import run_pass_a
from src.fusion.text_embed import embed_texts


CLAIMS_PATH   = Path("data/processed/report_claims.json")
SEGMENTS_PATH = Path("data/processed/video_segments.json")
OUT_PATH      = Path("data/processed/pass_a_matches.json")

# Marengo cross-modal cosines run low (visual ~0.15 max, audio ~0.08 max).
# We use these as a noise floor; top_k is the real selectivity knob, and
# Pass B re-ranks survivors. Tune per the M5 evaluation, not by gut feel.
MIN_SCORE = {"visual": 0.05, "audio": 0.03}
TOP_K     = 5


def _claim_text_for_embedding(claim: dict) -> str:
    """
    Build the string we hand Marengo for each claim.

    Concatenating location + description gives the embedding both:
      - identifying nouns that audio narration tends to repeat
        ("Charity Hospital", "Pumping Stations", "Highway 90")
      - damage vocabulary that visual frames support
        ("submerged", "collapsed", "storm surge")
    """
    parts = []
    loc = (claim.get("location_name") or "").strip()
    if loc:
        parts.append(loc)
    desc = (claim.get("damage_description") or "").strip()
    if desc:
        parts.append(desc)
    return ". ".join(parts) or claim.get("claim_id", "")


def main() -> int:
    load_dotenv()

    if not CLAIMS_PATH.is_file():
        print(f"Missing {CLAIMS_PATH} — run m3_parse_reports.py first.")
        return 1
    if not SEGMENTS_PATH.is_file():
        print(f"Missing {SEGMENTS_PATH} — run m2_marengo_fetch.py first.")
        return 1

    claims = json.loads(CLAIMS_PATH.read_text())
    seg_doc = json.loads(SEGMENTS_PATH.read_text())
    print(
        f"Loaded {len(claims)} claims and "
        f"{len(seg_doc['segments'])} segments "
        f"({seg_doc.get('segment_count')} clips x "
        f"{len(seg_doc.get('modalities', []))} modalities)."
    )

    texts = [_claim_text_for_embedding(c) for c in claims]
    print(f"Embedding {len(texts)} claim strings via Marengo sync...")
    t0 = time.time()
    claim_vecs = embed_texts(texts)
    dur = time.time() - t0
    print(
        f"Got {len(claim_vecs)} embeddings "
        f"(dim={len(claim_vecs[0]) if claim_vecs else 0}) in {dur:.1f}s "
        f"-> {dur / max(len(texts), 1) * 1000:.0f} ms/claim avg."
    )

    result = run_pass_a(
        claim_vecs, claims, seg_doc,
        min_score=MIN_SCORE, top_k=TOP_K,
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2))
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUT_PATH} ({size_kb:.0f} KB).")

    # Per-claim summary so we can spot-check thresholds without opening the file.
    print()
    print(f"Pass A summary (min_score={MIN_SCORE}, top_k={TOP_K}):")
    print(
        f"  {'claim_id':14s} {'sev':10s} "
        f"{'vis_max':>8s} {'aud_max':>8s} "
        f"{'#vis':>5s} {'#aud':>5s}  best  loc"
    )
    n_with_match = 0
    for row in result["matches"]:
        s = row["stats"]
        has_any = (s.get("visual_above_thr", 0) + s.get("audio_above_thr", 0)) > 0
        n_with_match += int(has_any)
        print(
            f"  {row['claim_id']:14s} "
            f"{(row['claim_severity'] or '?'):10s} "
            f"{s.get('visual_max', 0):>8.3f} "
            f"{s.get('audio_max', 0):>8.3f} "
            f"{s.get('visual_above_thr', 0):>5d} "
            f"{s.get('audio_above_thr', 0):>5d}  "
            f"{(s.get('best_modality') or '-'):6s}"
            f"{row['claim_summary'][:55]}"
        )
    print()
    print(
        f"{n_with_match}/{len(result['matches'])} claims have at least "
        f"one match above threshold."
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
