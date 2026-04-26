# DisasterFusion — Technical Documentation

*Geospatial Video Intelligence Hackathon · Track 03 — Multimodal Geospatial Workloads*
*St. Louis · April 25–26, 2026*

---

## At a glance

DisasterFusion is a multi-source intelligence pipeline that fuses aerial disaster footage with official damage reports and produces a single, georeferenced, confidence-scored finding stream. It takes one video and one or more text reports — PDF, DOCX, JSON, plain text, or a news URL — and emits the four-class output that single-source analysis cannot produce on its own.

Three things distinguish the approach:

* **Bidirectional fusion produces emergent classifications.** Findings are labelled `corroborated`, `discrepancy`, `unreported`, or `unverified`. The two interesting classes — unreported damage that no report covers, and unverified claims with no visual support — only exist when both sources are read together. A video-only system cannot tell you what's missing from the official record; a report-only system cannot tell you what the cameras captured that the field teams haven't logged.
* **Visual *and* audio embeddings are first-class signals.** Marengo 3.0 produces independent per-segment embeddings for the visual track and the audio track. Disaster footage from drones, helicopters, and TV cuts routinely carries operator narration, news voice-over, and environmental cues (sirens, generators, running water) that disambiguate scenes the visual stream alone cannot. Fusion scores both modalities and reports the winner per match.
* **Disaster-agnostic by configuration, not by code.** The same pipeline serves tornado, hurricane, flood, wildfire, and earthquake events; switching between them is a single edit to `config/disaster_types.yaml`. The canonical demo run is the EF-1 tornado that struck Grafton, Illinois on March 11, 2026 — 33 MB of aerial footage paired with six independent text reports (NWS survey, county EMA, four news articles).

Outputs: a master findings JSON document, an interactive Folium map, an Overture-aligned GeoJSON FeatureCollection, an SNS alert stream gated on operationally meaningful signals, and a natural-language query API that returns answers with cited finding IDs.

---

## 1. System Architecture

The pipeline is a four-stage flow — **Ingest → Extract → Fuse → Output** — backed by a DynamoDB persistence layer and orchestrated by a FastAPI service that runs each job in a background worker. Each stage has a defined input/output contract so stages can be developed and tested independently.

```
┌──────────────────────────┐  ┌──────────────────────────┐
│  Aerial video (MP4)      │  │ Damage reports           │
│  drone · heli · TV cuts  │  │ PDF · DOCX · JSON · URL  │
└──────────┬───────────────┘  └──────────┬───────────────┘
           │                             │
           └──────────────┬──────────────┘
                          ▼
        Stage 1 · Ingest                  src/video_pipeline/ingest.py
          - S3 upload (presigned URL)     src/report_parser/parser.py
          - Pegasus 1.2  (sync)
          - Marengo 3.0  (async, V+A)
          - Claude Haiku 4.5 (sync)
                          │
                          ▼
        Stage 2 · Extract                 src/video_pipeline/validation.py
          - Validate (flag-don't-drop)    src/video_pipeline/geo_simulator.py
          - Geo-simulate GPS-less video   src/report_parser/geocoder.py
          - Geocode claims (Overture)
                          │
                          ▼
        Stage 3 · Fuse                    src/fusion/pass_a.py
          - Pass A: cosine pre-filter     src/fusion/pass_b.py
          - Pass B: tiered scorer
          - Classify + score
                          │
                          ▼
        Stage 4 · Persist & Output        api/main.py
          - DynamoDB: jobs / claims /     src/output/frontend_schema.py
            fused_findings                src/output/alerts.py
          - Master findings JSON          api/nl_query.py
          - GeoJSON · map · SNS · NL query
```

### Pipeline contract

The pipeline runs once per submitted job and writes durable state to three DynamoDB tables (`jobs`, `report_claims`, `fused_findings`) plus per-stage JSON intermediates to `data/processed/`. The API serves results from DynamoDB; the dashboard reads master findings JSON. There is no stateful long-running service between Bedrock calls — every stage can be re-run from on-disk inputs without re-invoking upstream models, so iteration on fusion thresholds completes in seconds.

This decoupling is deliberate. Re-tuning the Pass B confidence weights does not require re-paying for Pegasus or Marengo. Replaying a finished job for an audit does not require a live Bedrock account. Job state in DynamoDB means the API itself is stateless and horizontally scalable.

---

## 2. Multi-Source Fusion Approach

Fusion is two passes: a cheap, broad cosine pre-filter (Pass A) and a detailed, explainable per-pair scorer (Pass B). Splitting the work this way keeps the combinatorial cost manageable and keeps the explanation surface human-readable.

### 2.1 Pass A — claim-to-segment cosine match

For each report claim, the parser builds a short visual-language description ("damaged commercial roof, debris in parking lot") and embeds it via Marengo's synchronous text endpoint. The result is a 512-dimensional vector that lives in the same space as the video segment embeddings produced asynchronously during ingest. Cosine similarity is computed between the claim vector and every segment embedding, **separately for the visual and audio modalities**.

Thresholds are calibrated for Marengo's cross-modal cosine distribution rather than text-to-text cosine, where typical thresholds are an order of magnitude higher: visual `0.05`, audio `0.03`. Both values are externalised in `config/thresholds.yaml`. Pass A's output is a `{claim_id → ranked segment list, per modality}` map plus per-claim stats (`visual_max`, `audio_max`, `best_modality`, `count_above_threshold`). It does not classify anything; it produces the candidate set Pass B will score.

### 2.2 Pass B — tiered (video finding × report claim) matcher

Pass B is the explainability layer. Each (video_finding, report_claim) pair is scored from two tiers of signals.

**Tier 1 — strong signals that raise the confidence floor.** A normalised building-name match imposes a floor of `0.85`. Spatial proximity within 200 m imposes a floor of `0.80`, decaying linearly to `0` at 2 km. These floors are deliberately high because the signals themselves are high-precision: two sources independently naming "Drifters Eats and Drinks", or co-locating an observation within 200 metres, is rarely coincidence.

**Tier 2 — medium signals, weighted blend.** Damage category match (weight `0.40`), text similarity from Pass A (weight `0.40`), severity agreement (`0.15`), building-type agreement (`0.10`). Severity scoring is graded: same → `1.0`, off-by-one → `0.5`, off-by-two → `0.2`, off-by-three → `0.0`.

`final_score = max(tier1_floor, tier2_blend)`. Using `max` rather than `sum` means a single strong signal can carry a pair, while the tier 2 blend keeps scoring graceful when no strong signal fires.

### 2.3 Classification thresholds

| Score range | Classification | Meaning |
|---|---|---|
| ≥ 0.50 | `corroborated` | Report and video describe the same damage |
| 0.30 – 0.49 | `discrepancy`  | Same area, but disagreement on severity, type, or precise location |
| < 0.30 with a claim | `unverified` | Report claim has no plausible video support |
| < 0.30 with no claim | `unreported` | Video finding has no report claim within reach — flagged for analyst attention |

When the system finds a corroboration but the two sources disagree, **it does not pick a winner** — it emits a `discrepancy` with both sides preserved. The `discrepancy_type` field records what kind of mismatch was detected; the `discrepancy_detail` field records the human-readable summary. The analyst decides; the system supplies the comparison.

---

## 3. TwelveLabs + Bedrock Integration

DisasterFusion uses both TwelveLabs foundation models in distinct roles via AWS Bedrock Runtime in `us-east-1`, plus Anthropic Claude Haiku 4.5 on the same Bedrock principal for text-side work.

### 3.1 Pegasus 1.2 — structured damage extraction

* **Role:** read each video as a single multimodal asset and produce a structured array of damage findings. Output is constrained to enums (`damage_type`, `severity`, `building_type`) that match the report-side taxonomy exactly, so categorical agreement at fusion time requires no synonym tables.
* **Invocation:** synchronous. `bedrock_runtime.invoke_model()` returns the JSON array in a single call.
* **Model ID:** `us.twelvelabs.pegasus-1-2-v1:0` (cross-region inference profile).
* **Prompt approach:** disaster-type-aware. The focus paragraph is loaded from `config/disaster_types.yaml` so the same code path handles tornado / hurricane / flood / wildfire / earthquake; only the prompt and severity vocabulary change.

### 3.2 Marengo 3.0 — multimodal embeddings

* **Role:** produce one 512-dimensional embedding per ~5–10 second segment of video, computed independently for the visual and audio modalities. Both modalities are requested in a single async invocation.
* **Invocation:** asynchronous via `start_async_invoke`. The pipeline polls `get_async_invoke()` until status is `Completed` and reads the embedding output from S3.
* **Model ID:** `twelvelabs.marengo-embed-3-0-v1:0` (async, video) and `us.twelvelabs.marengo-embed-3-0-v1:0` (sync, text — used at fusion time to embed report-claim summaries).

### 3.3 Claude Haiku 4.5 — text-side parsing and reasoning

Claude does three jobs on the text side: it extracts structured `ReportClaim` arrays from PDF / DOCX / JSON / TXT / URL inputs (one schema, five formats); it powers the geo simulator that places GPS-less video findings within a plausible disaster zone; and it serves the natural-language query API at `/jobs/{job_id}/query`. **Model ID:** `us.anthropic.claude-haiku-4-5-20251001-v1:0`.

### 3.4 Why all three, not any one alone

Pegasus alone gives findings with no entity grounding. Marengo alone gives timestamps with similarity scores but no condition assessment. Claude alone gives parsed claims but no visual evidence. The three-model architecture uses each where it dominates: structured visual generation (Pegasus), retrieval over the visual + audio space (Marengo), and structured text generation plus reasoning (Claude). The output of one becomes the input to the next.

---

## 4. Persistence Layer — DynamoDB Design

Pipeline state, parsed claims, and fused findings persist to three DynamoDB tables. DynamoDB was chosen for sub-10 ms reads (status polling stays cheap), pay-per-request billing (no idle cost), and IAM/region alignment with the rest of the AWS-native stack.

| Table | Partition Key | Sort Key | Global Secondary Indexes |
|---|---|---|---|
| `jobs` | `job_id` (S) | — | — |
| `report_claims` | `source_document` (S) | `claim_id` (S) | GSI1: `event_name` / `event_date` · GSI2: `county_state` / `event_date` |
| `fused_findings` | `job_id` (S) | `finding_id` (S) | GSI1: `event_name` / `classification` · GSI2: `geohash5` / `event_date` |

**`jobs`** replaces the in-memory job dict in `api/main.py`. Job status, step progress (a map of `step_name → {status, started_at, duration_ms}`), error tracebacks, and a pointer to the archived master findings document all live here. With state in DynamoDB the FastAPI worker is stateless — horizontal scale is a load-balancer config change.

**`report_claims`** is keyed on `source_document` so re-runs do not re-invoke Claude on the same file. Cache hits return in single-digit milliseconds. GSI1 enables "all claims for the Grafton EF1 event" without scanning. GSI2 enables "all claims in Jersey County, IL" — useful for cross-event analysis when the same area is hit again.

**`fused_findings`** preserves every fused finding with its full evidence chain. GSI1 supports "all unreported findings from Grafton" queries. GSI2 supports geographic-neighbourhood queries over geohash buckets — "all severe findings within ~5 km of Grafton on March 11" is a single `Query` call. The natural-language query API reads from this table, which means questions can be issued days or weeks after the originating job's working files are gone.

---

## 5. Entity Resolution

Three complementary mechanisms identify the same real-world asset across video and reports, and all three propagate into the evidence chain so humans can audit any decision.

**Name-based matching.** Both `VideoFinding` and `ReportClaim` carry a `building_name` field. Pegasus extracts business names, building names, and street signs visible in footage; Claude extracts the same from text. Strings are normalised (lowercase, punctuation-stripped, diacritics-folded) and tested for substring overlap. A positive match imposes the Tier 1 floor of `0.85`.

**Spatial matching.** Coordinates come from three sources: Overture-geocoded report claims (high confidence), GPS metadata in the video file (rare on public footage but exact when present), and the geo simulator (described below) for GPS-less video. Pass B uses haversine distance and treats matches under 200 m as a Tier 1 signal — a deliberately tight radius designed to avoid false positives in dense urban damage clusters.

**Overture place IDs as a global key.** When a claim is geocoded via the Overture `places` table (Tier 1, requiring an exact landmark match), the resolved place's Overture ID is attached to the claim and propagated through to the `fused_findings` DynamoDB record. This makes DisasterFusion outputs joinable to any other Overture-aligned dataset (building footprints, ownership records, tax roll, prior inspection history) and provides cross-event entity tracking out of the box.

**Geo simulator for GPS-less footage.** Most public aerial footage carries no GPS telemetry. The simulator runs in two steps: (1) Claude is given the concatenated descriptions from all video findings plus an optional region hint and asked to identify the disaster's primary location, named landmarks, area type, an estimated centre, and a confidence score; (2) findings are scattered around that centre via a Gaussian (σ = spread/2), and findings whose descriptions mention a named landmark are snapped to that landmark with small jitter. Every simulated coordinate is tagged `geo_method = "simulated_within_disaster_zone"` and `geo_confidence = "low"`. The simulator accepts a numpy seed (default `42`) so identical inputs produce identical outputs.

---

## 6. Operating Envelope

DisasterFusion is scoped honestly. The system is designed for and validated on:

* **Video quality:** 720p+ aerial / drone / TV-cut footage. Marengo handles bright-sun and harsh shadow conditions implicitly. Footage below 720p, heavy compression artifacts, and night flights are not characterised.
* **Disasters:** five natural-disaster types — tornado, hurricane, flood, wildfire, earthquake — selected at run time via a single CLI argument. The canonical demo run is a tornado (Grafton EF-1, 2026-03-11). Man-made disasters and ongoing events (active wildfire perimeters mid-burn) are out of scope.
* **Report formats:** PDF, DOCX, JSON, CSV, TXT, MD, and remote URLs. PDF and DOCX go through Claude as multimodal/text inputs — no OCR, no per-format parser stack. CSV is treated as text by header signature.
* **Localisation accuracy:** approximate (~50–500 m typical) when GPS is unavailable, suitable for prioritising field-team dispatch. Sub-decametre accuracy for regulatory documentation requires real telemetry on the source video.
* **Languages:** English. Claude can parse other languages but the disaster-type prompt set is English-only.

This scope was set at the start of the build. The Track 03 brief calls out **three modalities in 24 hours** as a common pitfall; we addressed it by picking video + documents + Overture geospatial reference up front and being explicit about what we don't handle.

---

## 7. Confidence Handling

Operational readiness requires that every finding carry a transparent confidence signal a reviewer can trust. DisasterFusion emits three independent layers per finding.

**Layer 1 — numerical confidence with breakdown.** Every fused finding has a `confidence_score` in `[0.0, 1.0]` plus a `confidence_breakdown` that decomposes the score into named components: `category`, `severity`, `building_type`, `text_similarity`, `name_match`, `spatial`. The breakdown is the difference between an opaque ML output and an actionable analytical product — a reviewer who suspects a particular signal is unreliable can sort or filter on the underlying component.

**Layer 2 — categorical confidence labels.** Every coordinate carries a `geo_confidence` label (`high` / `medium` / `low` / `unresolved`) tied to its source: GPS metadata, Overture exact-landmark match, county centroid, simulator placement, or no signal. Every video finding carries a `visual_evidence_quality` label (clarity, lighting, framing). Every source carries an implicit reliability weight (FEMA / NWS `0.9`, county `0.7`, news `0.5`).

**Layer 3 — validation flags (flag-don't-drop).** Every `VideoFinding` and `ReportClaim` carries an `is_valid` boolean and a list of `validation_errors`. Findings that fail schema, enum, or sanity checks remain in the output stream so the analyst can see them — but they are visually downgraded in the UI and excluded from alert triggers. The principle is that judges (and analysts) should be able to see which observations the system distrusts, not have them silently disappear.

A finding's classification and its confidence are independent dimensions. A `corroborated` classification with confidence `0.74` is *flagged for verification*, not auto-routed to any work queue; a `discrepancy` with confidence `0.85` is escalated immediately for source reconciliation. The system is explicit that high-stakes findings with weak confidence require expert eyes before action.

---

## 8. Performance Benchmarks

Measured on the canonical pipeline run against the Grafton tornado dataset: 33.2 MB MP4 (~3 min 02 s, 1080p), 6 text reports (~38 KB cumulative), `disaster_type=tornado`, `region_hint="Grafton, Illinois"`.

| Stage | Wall time | Notes |
|---|---|---|
| S3 upload (33 MB) | ~5 s | us-east-1, presigned URL minted |
| Pegasus 1.2 (sync) | ~28 s | 27 findings, single Bedrock call |
| Marengo 3.0 (async, full video) | ~6–8 min | 16 visual + 16 audio segments at 512-d |
| Claude report parsing | ~4 s | 6 documents, cache-miss path; cached returns <50 ms |
| Overture geocoding (DuckDB) | ~5 s | Three batched queries against S3 Parquet |
| Geo simulator | ~3 s | 27 findings placed within disaster zone |
| Pass A (numpy cosine) | <1 s | 162 dot products |
| Pass B (tiered scoring) | <1 s | 162 scored pairs |
| DynamoDB writes | <1 s total | ~35 single-item writes (1 job + 6 claims + 28 findings) |
| Frontend transform + GeoJSON | <1 s | In-memory JSON serialise |
| **End-to-end total** | **~9–11 min** | Marengo async dominates |

Throughput on the worked example: 27 Pegasus findings from ~3 min of footage (≈ 9 findings / minute of source); 32 Marengo segments at 512-d (≈ 64 KB of embeddings); 28 fused findings emitted; ~210 KB of master findings JSON; ~30 s time-to-first-result-row after job submission.

Cold-cache scaling is approximately linear in video duration: a 15-minute video yields ~25–35 minutes of wall time, a 45-minute video yields ~50–70 minutes. Multiple-job parallelism is free (each job is a self-contained Bedrock + DynamoDB workload).

---

## 9. Validation

Validated on a 13-item ground-truth set drawn from the Grafton run (six system-classified `corroborated` findings, five system-classified `unreported` findings, plus two synthetic probes for the discrepancy and unverified pathways). On the 11 live items:

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| `corroborated` | 1.00 | 0.86 | 0.92 | 7 |
| `unreported` | 0.80 | 1.00 | 0.89 | 4 |
| **macro average** | **0.90** | **0.93** | **0.91** | **11** |

The single false negative (an east-face view of a building Pegasus described as "commercial unit" rather than naming the business) is analysed in `docs/VALIDATION.md` §13.1 along with the threshold tweaks that would recover it. Discrepancy and unverified support is `0` because the input report set happens to agree with the video; the two probes document how the pathways are exercised and are not scored.

Full validation report — confusion matrix, error analysis, performance benchmarks — lives in `docs/VALIDATION.md`.

---

## 10. Innovation and Operational Extensions

The persistence-and-evidence-chain architecture intentionally enables several extensions that go beyond the brief's required scope. None ship in this build, but each is reachable from the current data model with limited additional engineering.

**Cross-event analytics.** GSI1 on `fused_findings` keys by `event_name`, so "every disaster at this Overture place ID across the last five years" is a single `Query`. Damage history per asset becomes a free byproduct of running the pipeline on every disaster.

**Real-time correlation.** The pipeline is currently per-job. Swapping the FastAPI background worker for an SQS-driven Lambda fan-out turns DisasterFusion into a streaming system that consumes new video clips and reports as they are produced. The Bedrock calls and DynamoDB writes are already designed to be idempotent.

**Conflict resolution beyond severity.** Discrepancy currently fires only on severity / damage_type / location offsets. Adding date-window discrepancy (the report's `event_date` predates the video's `capture_date` by more than the temporal radius) is a new `discrepancy_type` and one rule in Pass B.

**Knowledge graph layer.** Overture place IDs as the entity primary key, plus `event_name` from claims, plus extracted operator narration entities, are the three node types of a damage knowledge graph. Materialising one is a Neptune or AWS Knowledge Graph job over the existing DynamoDB tables.

---

## 11. Repository and Reproduction

GitHub: <https://github.com/your-org/disasterfusion>

Setup:

```bash
git clone https://github.com/your-org/disasterfusion
cd disasterfusion
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # fill in AWS credentials + table names
```

Provision DynamoDB tables (Terraform module under `scripts/infra/`):

```bash
cd scripts/infra && terraform init && terraform apply
```

Verify Bedrock + DynamoDB access with the smoke test:

```bash
python scripts/m2_smoke.py
```

Run a full pipeline against the bundled Grafton dataset:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
# then in another shell:
curl -X POST localhost:8000/analyze -F video=@data/raw/videos/grafton.mp4 \
                                     -F reports=@data/raw/reports/grafton_reports.json \
                                     -F disaster_type=tornado \
                                     -F region_hint="Grafton, Illinois"
```

Re-running the pipeline requires AWS Bedrock access with Pegasus 1.2, Marengo 3.0, and Claude Haiku 4.5 enabled in `us-east-1`, plus IAM permissions for Bedrock + S3 + DynamoDB (+ SNS for alerts).

---

## 12. Document map

| Topic | Document |
|---|---|
| This document | `docs/TECH.md` |
| Architecture diagram (PNG) | `docs/architecture_diagram.png` |
| Validation report — ground truth, precision/recall, error analysis | `docs/VALIDATION.md` |
| Intelligence product examples — five products with evidence chains | `docs/PRODUCTS.md` |
| Report-parser usage notes (PDF/DOCX/JSON/URL paths) | `PARSER_USAGE.md` |
| Submission DOCX (consolidated) | `docs/DisasterFusion_Submission.docx` |

---

*DisasterFusion · Geospatial Video Intelligence Hackathon, Track 03 · April 25–26, 2026*
