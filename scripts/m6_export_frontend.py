"""
M6 — Export master_findings.json for the frontend.

Reads data/processed/fused_findings_grafton.json, runs it through the
frontend schema transformer, writes exports/master_findings.json.

Usage
=====
    python scripts/m6_export_frontend.py
    python scripts/m6_export_frontend.py --fused data/processed/fused_findings_grafton.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.output.frontend_schema import transform  # noqa: E402


DEFAULT_INPUT  = _PROJECT_ROOT / "data" / "processed" / "fused_findings_grafton.json"
DEFAULT_OUTPUT = _PROJECT_ROOT / "exports" / "master_findings.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fused", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out",   type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    if not args.fused.is_file():
        print(f"Missing {args.fused}", file=sys.stderr)
        return 1

    fused_doc = json.loads(args.fused.read_text(encoding="utf-8"))
    result    = transform(fused_doc)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))

    n      = len(result["findings"])
    center = result["center"]
    counts = {}
    for f in result["findings"]:
        s = f["fusion_status"]
        counts[s] = counts.get(s, 0) + 1

    print(f"Wrote {n} findings -> {args.out.relative_to(_PROJECT_ROOT)}")
    print(f"Center: {center}  zoom: {result['zoom']}")
    print("Status breakdown:", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
