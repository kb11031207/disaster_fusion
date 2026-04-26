"""
Fusion engine evaluation — computes precision, recall, and F1 against
a manually annotated ground-truth file.

Usage
=====
    python -m src.shared.evaluate
    python -m src.shared.evaluate --gt data/ground_truth/grafton_pairs.json \
                                   --fused data/processed/fused_findings_grafton.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate(gt_path: Path, fused_path: Path) -> dict:
    """
    Compare fused findings against ground-truth pairs.

    Returns a metrics dict with per-class and macro-averaged scores.
    """
    gt_doc     = _load(gt_path)
    fused_doc  = _load(fused_path)

    # Index fused findings by video_finding_id
    fused_index: dict[str, str] = {}
    for finding in fused_doc.get("findings", []):
        vf = finding.get("video_finding")
        if vf:
            fused_index[finding["finding_id"]] = finding["classification"]

    classes = ["corroborated", "unreported", "discrepancy", "unverified"]

    # Per-class TP/FP/FN tallies
    tp: dict[str, int] = {c: 0 for c in classes}
    fp: dict[str, int] = {c: 0 for c in classes}
    fn: dict[str, int] = {c: 0 for c in classes}

    pair_results = []
    for pair in gt_doc.get("pairs", []):
        vid_id        = pair["video_finding_id"]
        true_cls      = pair["true_classification"]
        predicted_cls = fused_index.get(vid_id)

        result = {
            "video_finding_id":   vid_id,
            "report_claim_id":    pair.get("report_claim_id"),
            "true_classification":      true_cls,
            "predicted_classification": predicted_cls,
            "correct": predicted_cls == true_cls,
            "notes":   pair.get("notes", ""),
        }
        pair_results.append(result)

        if predicted_cls is None:
            # Finding not present in fused output at all → FN for true class
            fn[true_cls] = fn.get(true_cls, 0) + 1
            continue

        if predicted_cls == true_cls:
            tp[true_cls] += 1
        else:
            fp[predicted_cls] = fp.get(predicted_cls, 0) + 1
            fn[true_cls]      = fn.get(true_cls, 0) + 1

    per_class: dict[str, dict] = {}
    for c in classes:
        p = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) > 0 else 0.0
        r = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) > 0 else 0.0
        per_class[c] = {
            "tp": tp[c], "fp": fp[c], "fn": fn[c],
            "precision": round(p, 4),
            "recall":    round(r, 4),
            "f1":        round(_f1(p, r), 4),
        }

    active_classes = [c for c in classes if (tp[c] + fp[c] + fn[c]) > 0]
    macro_precision = sum(per_class[c]["precision"] for c in active_classes) / len(active_classes) if active_classes else 0.0
    macro_recall    = sum(per_class[c]["recall"]    for c in active_classes) / len(active_classes) if active_classes else 0.0

    total_pairs   = len(gt_doc.get("pairs", []))
    correct_pairs = sum(1 for r in pair_results if r["correct"])

    return {
        "summary": {
            "total_pairs":       total_pairs,
            "correct":           correct_pairs,
            "accuracy":          round(correct_pairs / total_pairs, 4) if total_pairs else 0.0,
            "macro_precision":   round(macro_precision, 4),
            "macro_recall":      round(macro_recall, 4),
            "macro_f1":          round(_f1(macro_precision, macro_recall), 4),
        },
        "per_class": per_class,
        "pair_results": pair_results,
    }


def _print_report(metrics: dict) -> None:
    s = metrics["summary"]
    print("\n=== Fusion Engine Evaluation ===")
    print(f"Pairs evaluated : {s['total_pairs']}")
    print(f"Correct         : {s['correct']}")
    print(f"Accuracy        : {s['accuracy']:.1%}")
    print(f"Macro Precision : {s['macro_precision']:.1%}")
    print(f"Macro Recall    : {s['macro_recall']:.1%}")
    print(f"Macro F1        : {s['macro_f1']:.1%}")
    print()
    print(f"{'Class':<22} {'P':>6} {'R':>6} {'F1':>6}  TP  FP  FN")
    print("-" * 56)
    for cls, m in metrics["per_class"].items():
        if m["tp"] + m["fp"] + m["fn"] == 0:
            continue
        print(f"{cls:<22} {m['precision']:>6.1%} {m['recall']:>6.1%} {m['f1']:>6.1%}  {m['tp']:>2}  {m['fp']:>2}  {m['fn']:>2}")
    print()
    print("Per-pair results:")
    for r in metrics["pair_results"]:
        mark = "OK" if r["correct"] else "XX"
        print(f"  {mark} {r['video_finding_id']}  true={r['true_classification']:<14}  pred={r['predicted_classification'] or 'MISSING'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt",    type=Path, default=_PROJECT_ROOT / "data" / "ground_truth" / "grafton_pairs.json")
    ap.add_argument("--fused", type=Path, default=_PROJECT_ROOT / "data" / "processed"    / "fused_findings_grafton.json")
    ap.add_argument("--out",   type=Path, default=None, help="Optional JSON output path")
    args = ap.parse_args()

    metrics = evaluate(args.gt, args.fused)
    _print_report(metrics)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(metrics, indent=2))
        print(f"\nMetrics written to {args.out}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
