# DisasterFusion

**Multi-Source Disaster Damage Intelligence System**

---

## What Is This?

DisasterFusion takes two things that exist after every major disaster — aerial video footage and official damage reports — and fuses them into a single, comprehensive damage intelligence product that neither source can produce alone.

It answers three questions no single data source can:

1. **What did reports miss?** Video reveals damage in areas officials haven't surveyed yet.
2. **Are reports accurate?** Video evidence corroborates or contradicts official severity ratings.
3. **Where are the gaps?** Which report claims lack visual evidence? Which video findings lack official documentation?

## The Problem

After a major disaster, damage assessment involves dozens of analysts manually cross-referencing aerial footage against FEMA preliminary damage assessments, NWS surveys, and local government reports. This process takes **days to weeks** — and by the time the picture is complete, the response window has narrowed.

The data exists. The correlation doesn't.

- FEMA PDA reports quantify damage by county but lack visual verification
- FEMA Project Worksheets detail site-specific damage but cover only applicant-reported sites
- NWS damage surveys provide GPS-tagged severity ratings but miss structures between survey points
- Aerial footage captures everything the camera sees but lacks structured metadata

Each source tells part of the story. DisasterFusion tells the whole story — automatically.

## How It Works

```
VIDEO (aerial/drone MP4)          REPORTS (FEMA PDAs, Project
    │                              Worksheets, NWS surveys)
    │                                        │
    ▼                                        ▼
┌──────────────┐                  ┌────────────────────┐
│ TwelveLabs   │                  │ LLM-Based Parser   │
│ Pegasus:     │                  │                    │
│ "What damage │                  │ Extract structured │
│ do you see?" │                  │ claims: location,  │
│              │                  │ severity, type,    │
│ Marengo:     │                  │ cost estimates     │
│ (512-d per-  │                  │                    │
│  segment     │                  │ Geocode addresses  │
│  embeddings) │                  │                    │
└──────┬───────┘                  └─────────┬──────────┘
       │                                    │
       ▼                                    ▼
  List[VideoFinding]                 List[ReportClaim]
       │                                    │
       └─────────────┬─────────────────────┘
                     ▼
          ┌─────────────────────┐
          │   FUSION ENGINE     │
          │                     │
          │ Pass A: For each    │
          │ report claim →      │
          │ search video for    │
          │ matching evidence   │
          │                     │
          │ Pass B: For each    │
          │ video finding →     │
          │ check if any report │
          │ covers it           │
          │                     │
          │ Confidence scoring  │
          │ per finding         │
          └─────────┬───────────┘
                    ▼
          ┌─────────────────────┐
          │ INTELLIGENCE OUTPUT │
          │                     │
          │ ✅ Corroborated     │
          │ ⚠️ Discrepancy      │
          │ 🔴 Unreported       │
          │ ⚪ Unverified       │
          │                     │
          │ Map + evidence      │
          │ chains + exports    │
          └─────────────────────┘
```

### Bidirectional Fusion — Why It Matters

Most multi-source systems do one-way matching. DisasterFusion runs fusion in **both directions**:

**Report → Video (Marengo embeddings + cosine similarity):** A FEMA Project Worksheet says Pumping Station 1 at 300 Howard Avenue was submerged under 8 feet of water. The system embeds that claim description as a 512-dim text vector and computes cosine similarity against every pre-computed video segment embedding. The highest-scoring segment above threshold is the match. If found: corroborated with video evidence. If the video shows a different severity: discrepancy flagged. If no segment crosses the threshold: unverified — the claim stands but without visual backup.

**Video → Report (spatial + semantic matching):** Pegasus identifies a collapsed residential block in the footage. The system checks whether any report claim covers that location and damage type. If no match: this is **unreported damage** — a gap in the official record that could affect resource allocation.

The gap analysis — what video found that reports missed, and what reports claim that video can't verify — is the intelligence product that single-source analysis simply cannot produce.

## Disaster-Agnostic Design

The system adapts to any disaster type through a configuration layer:

| Parameter | Tornado | Hurricane | Flood | Wildfire | Earthquake |
|---|---|---|---|---|---|
| Video analysis focus | Structural collapse, debris path, roof removal | Wind vs. surge vs. flood differentiation | Water depth, road access, displacement | Burn perimeter, structure survivability | Cracking, tilt, ground displacement |
| Severity mapping | EF-scale → minor/moderate/severe/destroyed | Category → severity | Depth-based | Burn extent | Structural integrity |
| Typical damage types | Collapse, roof, debris, vegetation | Flooding, roof, erosion, surge | Flooding, displacement, infrastructure | Fire damage, vegetation loss | Collapse, cracking, liquefaction |

The user selects disaster type at input. The system adjusts Pegasus prompts, damage taxonomies, and severity normalization accordingly.

## Demo: Grafton IL EF-1 Tornado

For the hackathon demonstration, we use the Grafton Illinois EF-1 tornado (April 2, 2025):

**Why Grafton:**
- Real NWS damage survey + Jersey County EMA situation report available
- YouTube aerial + ground-level footage (6 min, available as local MP4)
- Small geographic scope — tight bbox, easy to verify findings on a map
- Proximate to St. Louis venue — judges may recognize the location

**Report sources:**
- NWS damage survey — building-level damage with EF-scale ratings
- Jersey County EMA situation report — business damage summaries
- Local news (First Alert 4 / KSDK / Spectrum News) — named business damage reports

**Video sources:**
- `Grafton businesses damaged after EF-1 tornado touched down` (YouTube, 6 min)

**Results:**
- 27 Pegasus video findings → 6 corroborated with reports → **22 unreported damage sites** surfaced
- All 28 findings exported with confidence scores, timestamps, and Overture map reference data

## REST API

The full pipeline is exposed as a FastAPI server (`api/`):

```
POST /analyze                  — upload video + report → { job_id }
GET  /jobs/{id}                — poll { status, progress }
GET  /jobs/{id}/results        — { center, zoom, findings } (frontend contract)
GET  /jobs/{id}/overture       — Overture GeoJSON for event bbox
POST /jobs/{id}/query          — natural language query → { answer, referenced_ids }
GET  /health
```

Run: `python -m uvicorn api.main:app --host 0.0.0.0 --port 8000`

## Natural Language Query

Analysts can query findings in plain English via `POST /jobs/{id}/query`:

- *"What severe damage did video find that reports missed?"* → lists 21 unreported findings with descriptions
- *"Tell me about Drifters restaurant"* → synthesizes video + report evidence into one answer
- *"Where do video and reports agree?"* → lists all 6 confirmed findings

Claude Haiku reads a condensed version of the findings JSON and returns a structured answer with `referenced_ids` the frontend uses to highlight map markers.

## What Judges See

**Map Dashboard:** Every finding plotted on a map, color-coded by fusion_status. Click any marker to see the full evidence chain — video timestamp, report excerpt, confidence score breakdown, recommendation.

**Damage Summary:** 28 total findings. 6 confirmed (video + report agree). 22 unreported damage sites visible only in video. Center automatically computed from data — no hardcoded location.

**Gap Analysis:** 22 sites where video found damage that official reports didn't document — the core intelligence product no single source can produce.

**Natural Language Query:** Type a question, get a multi-source synthesized answer with clickable finding IDs.

**Overture Reference Layer:** 803 Overture features (places, buildings, roads) for the Grafton bounding box as a reference layer.

## Mission Impact

> **"Surfaces 22 unreported tornado damage sites in under 10 minutes — damage that would take field teams days to discover and document manually."**

For a Katrina-scale disaster: FEMA deployed hundreds of damage assessment teams over weeks. DisasterFusion processes the same video and report data in minutes, produces a fused intelligence product with confidence scores, and identifies gaps that manual cross-referencing would take days to surface.

**Applications beyond disaster response:**
- NGA geospatial intelligence workflows (multi-source correlation)
- Infrastructure monitoring (inspection video + maintenance records)
- Emergency management (FEMA damage assessment acceleration)
- Insurance (claims verification against aerial evidence)

## Team

| Role | Responsibility |
|---|---|
| Backend (all) | Video pipeline (Pegasus + Marengo), report parser, fusion engine, output layer, exports |
| Frontend + Demo | Streamlit dashboard polish, demo video |

## Tech Stack

| Component | Technology |
|---|---|
| Video analysis | TwelveLabs Pegasus 1.2 (`us.twelvelabs.pegasus-1-2-v1:0`) via AWS Bedrock |
| Video + text embeddings | TwelveLabs Marengo 3.0 (`twelvelabs.marengo-embed-3-0-v1:0` async video / `us.twelvelabs.marengo-embed-3-0-v1:0` sync text) |
| Report parsing + semantic similarity | Claude Haiku 4.5 via AWS Bedrock (`anthropic.claude-haiku-4-5-20251001-v1:0`) |
| Orchestration | Python 3.10+, boto3, numpy, sklearn, pandas, duckdb |
| Geolocation | Overture Maps (queried via DuckDB against public S3 parquet) |
| Frontend | Streamlit + Folium |
| Exports | GeoJSON, CSV, summary JSON |

## Hackathon Context

- **Event:** Geospatial Video Intelligence Hackathon — St. Louis, April 25–26, 2026
- **Track:** Track 3 — Multimodal Geospatial Workloads (Intelligence Fusion Across Data Types)
- **Judging weights:** 30% Multi-Source Integration · 25% Intelligence Value · 20% Video Understanding · 15% System Design · 10% Technical Execution
