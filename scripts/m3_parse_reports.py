"""
M3 — parse every FEMA DOCX in data/raw/reports/ via Claude Haiku 4.5
on Bedrock and write the combined ReportClaim list to
data/processed/report_claims.json.

Smoke-test first: we run on ONE doc (the LA Charity Hospital PW) so we
can validate the prompt before burning tokens on all six.

Run from the project root:
    python scripts/m3_parse_reports.py            # all docs
    python scripts/m3_parse_reports.py --one      # just Charity Hospital
"""

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.report_parser.parser import parse_report  # noqa: E402


SMOKE_TEST_DOC = "PW_1603_LA_CharityHospital_PW19731.docx"


def main() -> int:
    load_dotenv()

    one_only = "--one" in sys.argv

    reports_dir = _PROJECT_ROOT / "data" / "raw" / "reports"
    out_dir = _PROJECT_ROOT / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "report_claims.json"

    if one_only:
        docs = [reports_dir / SMOKE_TEST_DOC]
    else:
        docs = sorted(reports_dir.glob("*.docx"))

    if not docs:
        print(f"No .docx files found in {reports_dir}")
        return 2

    all_claims: list[dict] = []
    for doc_path in docs:
        if not doc_path.is_file():
            print(f"Skipping (missing): {doc_path.name}")
            continue
        t0 = time.time()
        try:
            claims = parse_report(doc_path)
        except Exception as e:
            print(f"  FAILED on {doc_path.name}: {e}")
            continue
        elapsed = time.time() - t0
        print(f"  parsed in {elapsed:.1f}s ({len(claims)} claim(s))")
        print()
        all_claims.extend(c.to_dict() for c in claims)

    out_path.write_text(json.dumps(all_claims, indent=2))

    print("=" * 60)
    print(f"Total claims: {len(all_claims)}")
    print(f"Written to: {out_path.relative_to(_PROJECT_ROOT)}")
    print()

    # Quick preview
    for i, c in enumerate(all_claims[:6], 1):
        print(f"--- Claim {i} ---")
        print(f"  source:   {c['source_document']} ({c['source_type']})")
        print(f"  location: {c['location_name']}")
        print(f"  severity: {c['severity']}  category: {c.get('damage_type') or c.get('damage_category')}")
        print(f"  cost:     {c['cost_estimate']}")
        print(f"  desc:     {c['damage_description'][:200]}")
        print()
    if len(all_claims) > 6:
        print(f"... {len(all_claims) - 6} more in {out_path.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
