# DisasterFusion

*Multi-source geospatial intelligence for disaster damage assessment.*
*Geospatial Video Intelligence Hackathon · Track 03 · St. Louis · April 25–26, 2026*

---

DisasterFusion fuses aerial disaster footage with official damage reports and produces a single, georeferenced, confidence-scored finding stream. It takes one video and one or more text reports — PDF, DOCX, JSON, plain text, or a news URL — and emits the four-class output that single-source analysis cannot produce on its own: corroborated, discrepancy, unreported, unverified.

Three things distinguish the approach:

* **Bidirectional fusion produces emergent classifications.** A video-only system cannot tell you what's missing from the official record; a report-only system cannot tell you what the cameras captured that the field teams haven't logged. DisasterFusion does both.
* **Visual *and* audio embeddings are first-class signals.** Marengo 3.0 produces independent per-segment embeddings for the visual track and the audio track. Operator narration, news voice-over, and environmental cues disambiguate scenes the visual stream alone cannot.
* **Disaster-agnostic by configuration.** The same pipeline serves tornado, hurricane, flood, wildfire, and earthquake events. Switching is a single edit to `config/disaster_types.yaml`. Canonical demo: the EF-1 tornado that struck Grafton, Illinois on March 11, 2026.

---

## Quickstart

```bash
git clone https://github.com/your-org/disasterfusion
cd disasterfusion
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # AWS credentials + DynamoDB table names
```

Provision DynamoDB (Terraform under `scripts/infra/`):

```bash
cd scripts/infra && terraform init && terraform apply
```

Smoke-test Bedrock + DynamoDB access:

```bash
python scripts/m2_smoke.py
```

Run the API and submit the bundled Grafton dataset:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
# then in another shell:
curl -X POST localhost:8000/analyze \
     -F video=@data/raw/videos/grafton.mp4 \
     -F reports=@data/raw/reports/grafton_reports.json \
     -F disaster_type=tornado \
     -F region_hint="Grafton, Illinois"
```

Poll `/jobs/{job_id}` until status is `completed`, then fetch results from `/jobs/{job_id}/results` (master findings JSON), `/jobs/{job_id}/overture` (GeoJSON), or `/jobs/{job_id}/query` (natural-language).

---

## Inputs and outputs

**Inputs per run.** One video (MP4), one or more text reports (PDF, DOCX, JSON, TXT, CSV, MD, or remote URL), a disaster type (`tornado` · `hurricane` · `flood` · `wildfire` · `earthquake`), and an optional region hint (place name or bounding box).

**Outputs.** A master findings JSON document with full evidence chains, an interactive Folium map, an Overture-aligned GeoJSON FeatureCollection ready for ArcGIS / QGIS / Mapbox, an SNS alert stream gated on operationally meaningful signals (unreported severe / infrastructure damage, severity discrepancies), and a natural-language query API that returns answers with cited finding IDs.

Every fused finding carries its evidence chain: source video clip with deep-link timestamp, report excerpt verbatim, Overture place ID where applicable, per-component confidence breakdown, and pipeline + model versions. Persisted to DynamoDB so any finding remains auditable after the originating job's working files are gone.

---

## Architecture

Four stages backed by DynamoDB persistence and orchestrated by a FastAPI service:

```
Ingest      → Pegasus 1.2 + Marengo 3.0 (visual + audio) + Claude Haiku 4.5
Extract     → Validation, geo simulator, Overture geocoder (DuckDB)
Fuse        → Pass A (cosine pre-filter) + Pass B (tiered scorer)
Persist     → DynamoDB: jobs · report_claims · fused_findings
Output      → Master findings JSON · GeoJSON · Folium map · SNS · NL query
```

Full architecture, model IDs, fusion math, DynamoDB schema, validation, and benchmarks are in [`docs/TECH.md`](docs/TECH.md). One-page architecture diagram: [`docs/architecture_diagram.png`](docs/architecture_diagram.png).

---

## Repository layout

```
disasterfusion/
├── README.md                  This file
├── PARSER_USAGE.md            Report-parser usage notes
├── requirements.txt
├── .env.example
├── api/                       FastAPI service (main, pipeline_runner, nl_query)
├── src/
│   ├── video_pipeline/        S3 upload, Pegasus, Marengo async, geo simulator
│   ├── report_parser/         Claude-based parser, Overture geocoder
│   ├── fusion/                Pass A (cosine) + Pass B (tiered) + text embedder
│   ├── output/                Frontend transformer, alerts, exporters
│   └── shared/                Dataclasses, config loaders, evaluation utilities
├── config/
│   ├── disaster_types.yaml    Per-disaster Pegasus prompt + severity map
│   └── thresholds.yaml        Pass A/B thresholds, confidence weights
├── data/
│   ├── raw/                   Input videos + reports
│   ├── processed/             Per-stage JSON intermediates
│   └── ground_truth/          Manual cross-source validation set
├── scripts/                   Step-by-step replay scripts (m2…m6) + Terraform infra
├── docs/                      TECH.md, VALIDATION.md, PRODUCTS.md, diagrams
├── exports/                   Generated GeoJSON / CSV / map HTML / master_findings.json
└── demo/                      Demo script + recorded walkthrough
```

---

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/TECH.md`](docs/TECH.md) | Technical documentation — architecture, fusion methodology, TwelveLabs integration, DynamoDB schema, entity resolution, operating envelope, performance benchmarks |
| [`docs/VALIDATION.md`](docs/VALIDATION.md) | Validation report — ground-truth correlations, precision and recall, error analysis |
| [`docs/PRODUCTS.md`](docs/PRODUCTS.md) | Intelligence product examples — five products with evidence chains, single-source comparison |
| [`PARSER_USAGE.md`](PARSER_USAGE.md) | Report-parser usage — PDF, DOCX, JSON, URL paths |
| [`docs/DisasterFusion_Submission.docx`](docs/DisasterFusion_Submission.docx) | Consolidated submission document |

---

## Environment

* Python 3.10+
* AWS Bedrock in `us-east-1` with TwelveLabs Pegasus 1.2, Marengo 3.0, and Anthropic Claude Haiku 4.5 enabled
* AWS DynamoDB (on-demand billing recommended; three tables: `jobs`, `report_claims`, `fused_findings`)
* AWS S3 bucket for video uploads and Marengo embedding output
* Optional: AWS SNS topic for the alert stream

Required environment variables, full table schemas, and the canonical model IDs are listed in [`docs/TECH.md`](docs/TECH.md) §3 and §4.

---

## License

Hackathon project — no license specified.

---

*DisasterFusion · Geospatial Video Intelligence Hackathon, Track 03 · April 25–26, 2026*
