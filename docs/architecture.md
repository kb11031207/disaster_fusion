# DisasterFusion — System Architecture

## System Context

DisasterFusion operates as a pipeline with four stages: ingest, extract, fuse, and output. Each stage is independently testable and communicates through defined JSON data contracts.

```
┌─────────────────────────────────────────────────────────────────────┐
│                          DISASTERFUSION                             │
│                                                                     │
│   ┌──────────┐    ┌───────────┐    ┌──────────┐    ┌───────────┐  │
│   │  INGEST  │───▶│  EXTRACT  │───▶│   FUSE   │───▶│  OUTPUT   │  │
│   └──────────┘    └───────────┘    └──────────┘    └───────────┘  │
│                                                                     │
│   User uploads     Pegasus +        Bidirectional   Map, reports,  │
│   video + reports  LLM parser       matching +      exports        │
│   + disaster type  produce          confidence                     │
│                    structured        scoring                        │
│                    findings                                         │
└─────────────────────────────────────────────────────────────────────┘
        │                  │                │                │
        ▼                  ▼                ▼                ▼
   Raw MP4/PDF      VideoFinding[]    FusedFinding[]    GeoJSON/CSV
                    ReportClaim[]                       PDF/Dashboard
```

---

## Detailed Data Flow

### Stage 1: Ingest

User provides three inputs. The disaster type parameter configures the entire downstream pipeline.

```
USER INPUT
    │
    ├── Video files (MP4) + capture_date per video
    │     │
    │     ▼
    │   ┌─────────────────────────────┐
    │   │ S3 Upload                   │
    │   │                             │
    │   │ • Upload each MP4 to S3     │
    │   │ • Tag with capture_date     │
    │   │ • Return S3 URIs            │
    │   └─────────────┬───────────────┘
    │                 │
    │                 ▼
    │           S3 URIs (consumed by Pegasus + Marengo async)
    │
    ├── Damage reports (PDF / CSV / DOCX)
    │     │
    │     ▼
    │   ┌─────────────────────────────┐
    │   │ File Reader                 │
    │   │                             │
    │   │ • PDF: text extraction      │
    │   │ • CSV: pandas load          │
    │   │ • DOCX: stdlib zipfile+regex│
    │   └─────────────┬───────────────┘
    │                 │
    │                 ▼
    │           Raw text / structured data (passed to Extract stage)
    │
    └── Disaster type
          │
          ▼
        ┌─────────────────────────────┐
        │ Config Loader               │
        │                             │
        │ • Load disaster_types.yaml  │
        │ • Set Pegasus focus prompt  │
        │ • Set severity mapping      │
        └─────────────┬───────────────┘
                      │
                      ▼
                DisasterConfig (shared by all components)

Note: there is NO region or bounding box in the config. The system is
disaster-agnostic and accepts input for any geography.
```

### Stage 2: Extract

Two parallel extraction paths. They run independently and produce structurally compatible outputs.

```
┌─────────────────────────────────┐     ┌─────────────────────────────────┐
│       VIDEO PIPELINE            │     │        REPORT PARSER            │
│                                 │     │                                 │
│  S3 URI + capture_date          │     │  Raw report text (DOCX/PDF)     │
│     │                           │     │     │                           │
│     ├─────────┐                 │     │     ▼                           │
│     ▼         ▼                 │     │  ┌───────────────────────┐      │
│  ┌──────┐ ┌──────────┐          │     │  │ LLM Extraction        │      │
│  │Pegasus│ │ Marengo  │         │     │  │ (Claude API)          │      │
│  │       │ │ (async   │         │     │  │                       │      │
│  │JSON   │ │  video   │         │     │  │ Returns: JSON array   │      │
│  │schema │ │  embed)  │         │     │  │ of damage claims      │      │
│  │output │ │          │         │     │  └───────────┬───────────┘      │
│  └───┬──┘ └────┬─────┘          │     │              │                  │
│      │        │                 │     │              ▼                  │
│      ▼        ▼                 │     │  ┌───────────────────────┐      │
│  ┌──────┐ ┌──────────┐          │     │  │ Geocoding             │      │
│  │ Val. │ │ 512-d    │          │     │  │                       │      │
│  │      │ │ segment  │          │     │  │ • Address → lat/lon   │      │
│  │      │ │ vectors  │          │     │  │ • Overture / DuckDB   │      │
│  └───┬──┘ └────┬─────┘          │     │  └───────────┬───────────┘      │
│      │        │                 │     │              │                  │
│      ▼        ▼                 │     │              ▼                  │
│ video_      video_              │     │  ┌───────────────────────┐      │
│ findings    segments            │     │  │ Validation            │      │
│ .json       .json               │     │  │                       │      │
│                                 │     │  │ • Severity enum       │      │
└─────────────────────────────────┘     │  │ • Lat/lon sane range  │      │
                                        │  │ • Cost sanity         │      │
                                        │  │ • Required fields     │      │
                                        │  └───────────┬───────────┘      │
                                        │              ▼                  │
                                        │     List[ReportClaim]           │
                                        │     → report_claims.json        │
                                        └─────────────────────────────────┘
```

### Stage 3: Fuse (Core — 55% of judging weight)

The fusion engine runs two passes, then merges results.

```
                    List[VideoFinding]        List[ReportClaim]
                         │                         │
            ┌────────────┘                         └──────────┐
            │                                                  │
            ▼                                                  ▼
┌───────────────────────────────────┐    ┌───────────────────────────────────┐
│  PASS A: Report → Video           │    │  PASS B: Video → Report           │
│  (Cosine Similarity)              │    │  (Spatial + Semantic Match)       │
│                                   │    │                                   │
│  For each ReportClaim:            │    │  For each VideoFinding            │
│                                   │    │  (not already matched in Pass A): │
│  1. Embed claim description as    │    │                                   │
│     512-d text vector (Marengo    │    │  1. Spatial filter: any claims    │
│     sync InvokeModel)             │    │     within radius? (haversine)    │
│                                   │    │     → cheap, eliminates 90%+      │
│  2. Cosine similarity against all │    │                                   │
│     pre-computed video segment    │    │  2. Semantic filter: LLM rates    │
│     embeddings                    │    │     description similarity 0–1    │
│                                   │    │     → expensive, only on spatial  │
│  3. If best score ≥ 0.3:          │    │       survivors                   │
│     • Compare severity            │    │                                   │
│     • If agrees → CORROBORATED    │    │  3. Combined score ≥ 0.5:         │
│     • If differs → DISCREPANCY    │    │     → match found (corroborated)  │
│                                   │    │                                   │
│  4. If no match → UNVERIFIED      │    │  4. No match → UNREPORTED         │
│                                   │    │     (gap in official record)      │
│  Output: FusedFindings with       │    │                                   │
│  classification + confidence      │    │                                   │
└──────────────┬────────────────────┘    └──────────────┬────────────────────┘
               │                                        │
               └──────────────┬─────────────────────────┘
                              │
                              ▼
                 ┌────────────────────────┐
                 │  MERGE + DEDUPLICATE   │
                 │                        │
                 │  • Combine Pass A + B  │
                 │  • Deduplicate (same   │
                 │    claim matched from  │
                 │    both directions)    │
                 │  • Final confidence    │
                 │    scores             │
                 └────────────┬───────────┘
                              │
                              ▼
                     List[FusedFinding]
                     → fused_findings.json
```

#### Confidence Scoring Detail

Each fused finding receives a confidence score from 0.0 to 1.0 composed of four weighted signals:

```
confidence = (0.30 × spatial_proximity)
           + (0.35 × semantic_similarity)
           + (0.20 × severity_agreement)
           + (0.15 × source_reliability)

spatial_proximity:
  1.0 — same location (< 100m)
  0.5 — nearby (< 1km)
  0.0 — beyond max radius (> 5km) or location unknown

semantic_similarity:
  From Marengo score (Pass A) or LLM rating (Pass B)
  1.0 — clearly same damage
  0.0 — unrelated descriptions

severity_agreement:
  1.0 — exact match (both say "severe")
  0.5 — off by one level (severe vs. destroyed)
  0.2 — off by two levels
  0.0 — opposite ends (minor vs. destroyed)

source_reliability:
  0.9 — FEMA / NWS official documents
  0.7 — City / county government reports
  0.5 — News media or unverified sources
```

#### Staged Filtering (Computational Efficiency)

Naive pairwise comparison is O(V × R) where V = video findings and R = report claims. For large datasets this creates combinatorial explosion.

```
All VideoFindings × All ReportClaims
         │
         ▼
┌─────────────────────────┐
│ Stage 1: Temporal Filter │  Cost: O(1) per pair
│ Both within disaster     │  Eliminates: ~0% (all same event)
│ timeframe?               │  (More useful for multi-event datasets)
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 2: Spatial Filter  │  Cost: O(1) per pair (haversine)
│ Within 5km radius?       │  Eliminates: 80–95% of pairs
│ (requires both have geo) │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 3: Type Filter     │  Cost: O(1) per pair
│ Compatible damage types? │  Eliminates: 50% of remaining
│ (flooding ≠ fire_damage) │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 4: Semantic Match  │  Cost: 1 LLM API call per pair
│ LLM rates description    │  Only runs on ~2–5% of original pairs
│ similarity 0.0–1.0       │
└────────────┬────────────┘
             ▼
        Matched pairs with confidence scores
```

### Stage 4: Output

```
List[FusedFinding]
         │
         ├──────────────────────────────────────────┐
         │                                          │
         ▼                                          ▼
┌─────────────────────┐                  ┌─────────────────────┐
│ Map Builder         │                  │ Report Generator    │
│ (Folium)            │                  │                     │
│                     │                  │ • Summary stats     │
│ • Plot each finding │                  │ • By classification │
│   as color-coded    │                  │ • By severity       │
│   marker            │                  │ • Corroboration     │
│ • Popup: evidence   │                  │   rate              │
│   chain             │                  │ • Gap analysis      │
│ • Layer toggles per │                  │                     │
│   classification    │                  │ Formats: JSON, PDF  │
└─────────┬───────────┘                  └──────────┬──────────┘
          │                                         │
          ▼                                         ▼
┌─────────────────────┐                  ┌─────────────────────┐
│ Streamlit Dashboard │                  │ Exporters           │
│                     │                  │                     │
│ • Map view          │                  │ • GeoJSON (GIS)     │
│ • Findings table    │                  │ • CSV (spreadsheet) │
│ • Filter controls   │                  │ • PDF (formal       │
│ • Summary stats     │                  │   assessment)       │
│ • Gap analysis tab  │                  │                     │
└─────────────────────┘                  └─────────────────────┘
```

---

## Component Interaction Diagram

Shows which components call which external services.

```
┌──────────────────────────────────────────────────────────────┐
│                      DISASTERFUSION                          │
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐      │
│  │   Video     │    │   Report    │    │   Fusion    │      │
│  │   Pipeline  │    │   Parser    │    │   Engine    │      │
│  └──────┬──────┘    └──────┬──────┘    └──┬───┬──────┘      │
│         │                  │              │   │              │
│         │                  │              │   │              │
└─────────┼──────────────────┼──────────────┼───┼──────────────┘
          │                  │              │   │
          ▼                  ▼              │   ▼
  ┌──────────────┐   ┌──────────────┐      │  ┌──────────────┐
  │ TwelveLabs   │   │ Claude API   │      │  │ Claude API   │
  │ (Bedrock)    │   │ (Anthropic)  │      │  │ (Anthropic)  │
  │              │   │              │      │  │              │
  │ • Pegasus    │   │ • Report     │      │  │ • Semantic   │
  │   (analyze)  │   │   extraction │      │  │   similarity │
  │ • Marengo ◀──┼───┼──────────────┼──────┘  │   scoring    │
  │   embed      │   │              │         │              │
  │   (async vid │   │              │         │              │
  │    + sync    │   │              │         │              │
  │    text)     │   │              │         │              │
  └──────────────┘   └──────────────┘         └──────────────┘
                             │
                             ▼
                     ┌──────────────┐
                     │ Geocoding    │
                     │ (Overture    │
                     │  via DuckDB) │
                     └──────────────┘
```

Note: Marengo is used twice. (1) The Video Pipeline invokes it **async on video** to pre-compute a 512-d embedding per ~5-second segment, persisted to `video_segments.json`. (2) The Fusion Engine invokes it **sync on text** at fusion time to embed each report claim description, then computes cosine similarity against the video segment matrix in-process (numpy). There is no search index.

---

## Error Handling Architecture

```
Any Component Output
         │
         ▼
┌─────────────────────────┐
│ Schema Validation       │
│                         │
│ Required fields present?│───No──▶ is_valid = False
│ Enums in valid set?     │         validation_errors += reason
│ Types correct?          │         KEEP finding in output
└────────────┬────────────┘
             │ Yes
             ▼
┌─────────────────────────┐
│ Sanity Checks           │
│                         │
│ Lat/lon in [-90,90] /   │───No──▶ is_valid = False
│   [-180,180]?           │         validation_errors += reason
│ Timestamps in range?    │         KEEP finding in output
│ Description meaningful? │
└────────────┬────────────┘
             │ Yes
             ▼
┌─────────────────────────┐
│ Confidence Assessment   │
│                         │
│ Multi-source agreement? │───Low──▶ confidence_score < 0.4
│ Strong spatial match?   │          Flag as low-confidence
│ Semantic alignment?     │          KEEP finding in output
└────────────┬────────────┘
             │ High
             ▼
        Valid, high-confidence finding

PRINCIPLE: Nothing is silently dropped.
Every finding appears in output with its validation status visible.
```

---

## Technology Dependencies

```
Python 3.10+
├── boto3                    # AWS Bedrock API calls (Pegasus, Marengo, Claude Haiku) + S3
├── numpy                    # Cosine similarity + vector math
├── scikit-learn             # Cosine similarity utilities
├── pandas                   # Tabular data manipulation + CSV export
├── duckdb                   # Overture Maps geocoding (S3 parquet queries)
├── folium                   # Interactive map generation
├── streamlit                # Dashboard UI
├── geojson                  # GeoJSON export
├── pyyaml                   # Config file loading
└── python-dotenv            # Environment variable management

# Stdlib only — no extra deps:
# • DOCX text extraction: zipfile + regex
# • Claude (report parsing + semantic similarity): boto3 Bedrock invoke_model
```

---

## Deployment Architecture (Hackathon)

For the hackathon demo, all components run locally on a single machine:

```
┌─────────────────────────────────────┐
│         Local Machine               │
│                                     │
│  ┌───────────┐   ┌───────────────┐  │
│  │ Streamlit │   │ Python        │  │
│  │ Server    │   │ Pipeline      │  │
│  │ (port     │   │ Scripts       │  │
│  │  8501)    │   │               │  │
│  └─────┬─────┘   └───────┬───────┘  │
│        │                 │          │
│        └────────┬────────┘          │
│                 │                   │
│        ┌────────┴────────┐          │
│        │ data/processed/ │          │
│        │ (JSON files)    │          │
│        └─────────────────┘          │
└──────────────────┬──────────────────┘
                   │
          Network calls to:
          ├── AWS Bedrock (Pegasus, Marengo, Claude Haiku 4.5)
          ├── S3 (video upload, Marengo embedding output)
          └── Overture Maps S3 parquet (geocoding via DuckDB)
```

### Production Architecture (Future State)

For the mission impact brief — how this would deploy at scale:

```
┌──────────┐     ┌──────────────┐     ┌───────────────┐
│ Ingest   │     │ Processing   │     │ Serving       │
│ Layer    │     │ Layer        │     │ Layer         │
│          │     │              │     │               │
│ S3 +     │────▶│ AWS Lambda / │────▶│ API Gateway + │
│ SQS      │     │ Step Fns     │     │ RDS/DynamoDB  │
│ (upload  │     │ (pipeline    │     │ (query +      │
│  queue)  │     │  orchestr.)  │     │  serve)       │
└──────────┘     └──────────────┘     └───────────────┘
```

---

## Data Contract Summary

All inter-component communication uses JSON. These contracts are defined in `src/shared/models.py`.

```
Video Pipeline ──── VideoFinding[]    ────▶ Fusion Engine
                    (video_findings.json)
               ──── MarengoSegment[]  ────▶ Fusion Engine (Pass A)
                    (video_segments.json)

Report Parser ───── ReportClaim[]     ────▶ Fusion Engine
                    (report_claims.json)

Fusion Engine ───── FusedFinding[]    ────▶ Output Layer
                    (fused_findings.json)
```

Field definitions, types, and validation rules: see `docs/technical_documentation.md`.
