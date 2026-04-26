# DisasterFusion — Master Document

**Project single source of truth. If it's not here or linked from here, it doesn't exist.**

Last updated: Saturday April 25, 2026

---

## Quick Links

| Document | Location | Status |
|---|---|---|
| Project Overview | `docs/project_overview.md` | ✅ Done |
| Technical Documentation | `docs/technical_documentation.md` | ✅ Done |
| Architecture Doc | `docs/architecture.md` | ✅ Done |
| Task Breakdown | `docs/task_breakdown.md` | ✅ Done |
| Implementation Guide (ref code) | `docs/implementation_guide.py` | ✅ Done |
| Milestones | `docs/milestones.md` | ✅ Done |
| Frontend Integration Guide | `docs/FRONTEND_INTEGRATION.md` | ✅ Done |
| Agent Context | `claude.md` | ✅ Done |
| Repo README | `README.md` | ⬜ Stub |
| GitHub Repo | `https://github.com/YOUR_TEAM/disasterfusion` | ⬜ Set Up |

---

## What We're Building (One Paragraph)

A tornado damage intelligence system that fuses aerial video with official damage reports. Input: one video (MP4) + one report (PDF/DOCX/TXT/CSV) + optional location hint. Output: fused damage findings classified as confirmed, conflicting_severity, unreported_damage, or uncertain — served via REST API in a frontend-ready JSON shape with confidence scores, video timestamps, report excerpts, Overture map reference data, and natural-language query support. Demo uses the Grafton IL EF-1 tornado (April 2, 2025): 6 corroborated findings + 22 unreported damage sites surfaced from video.

---

## Team (2 people)

| Role | Owner | Delivers |
|---|---|---|
| **Backend (all)** | **Me** | `video_findings.json`, `video_segments.json`, `report_claims.json`, `fused_findings.json`, GeoJSON/CSV exports, summary JSON, Folium map HTML |
| **Frontend + Demo** | Teammate | Streamlit dashboard polish, 4–6 min demo video |

The frontend consumes `fused_findings.json` via a frozen schema (`FusedFinding` dataclass in `docs/technical_documentation.md`). The teammate can build against a mock `fused_findings.json` on Saturday morning while I'm building the pipeline.

---

## Backend Task List (Me)

### P0 — Must work Saturday

| # | Task | Est. | Notes |
|---|---|---|---|
| 1 | AWS Bedrock access + S3 bucket + `.env` | 30 min | Confirm TwelveLabs models enabled in us-east-1 |
| 2 | Upload 1 Katrina video to S3 | 15 min | Helicopter footage or 2025 Fox TV coverage |
| 3 | Pegasus analysis → `video_findings.json` | 1–2 hr | Use structured JSON output via `responseFormat.jsonSchema` |
| 4 | Marengo async video embeddings → `video_segments.json` | 1 hr | `StartAsyncInvoke`, poll, read `output.json` from S3 |
| 5 | Report parser (DOCX → `report_claims.json`) | 2 hr | Claude Haiku 4.5 on Bedrock + stdlib `zipfile` for DOCX text. Parses six FEMA docs (3 PWs + 3 PDAs) |
| 6 | Geocode report claims | 30 min | Overture Maps via DuckDB (sponsor-credited, no rate limit, no API key) |
| 7 | Fusion Pass A (text embed + cosine) | 1–2 hr | Marengo sync text embed, numpy cosine. Threshold 0.3 to start |
| 8 | Confidence scoring + classification | 30 min | Weighted sum; enum classification |
| 9 | Folium map + GeoJSON export | 1–2 hr | Color-coded markers, click-to-expand popups |

### P1 — Saturday evening / Sunday morning

| # | Task | Est. | Notes |
|---|---|---|---|
| 10 | Fusion Pass B (spatial + LLM semantic) | 1–2 hr | Haversine filter, Claude semantic score |
| 11 | Process any remaining Katrina videos | 30 min | Rerun Pegasus + Marengo per video |
| 12 | Summary stats JSON + CSV export | 30 min | Counts by classification, corroboration rate |
| 13 | Threshold tuning | 30 min | Based on ground-truth spot checks |
| 14 | Ground truth: 5–8 verified pairs | 1 hr | Manual watch-and-read; template in tech doc |

### P2 — Sunday nice-to-haves

| # | Task | Est. | Notes |
|---|---|---|---|
| 15 | Entity-to-location resolution (video → geocode) | 1 hr | For findings where Pegasus picked up landmarks |
| 16 | PDF damage assessment report | 1 hr | `reportlab` — skip if crunched |
| 17 | Precision/recall report | 30 min | Against the 5–8 ground truth items |

---

## Timeline

### Saturday April 25

| Time | Milestone |
|---|---|
| 9:00–10:00 AM | Registration, confirm Bedrock access works |
| 10:00–11:00 AM | Opening keynote |
| 11:00 AM–12:00 PM | Tasks 1–3: env setup, upload video, get Pegasus working end-to-end on one video |
| 12:00–1:00 PM | Lunch. Sanity check: Pegasus output useful? |
| 1:00–3:00 PM | Tasks 4–6: Marengo video embeddings, report parser, geocoding |
| 3:00–5:00 PM | Tasks 7–9: Pass A cosine fusion, Folium map, GeoJSON export. Hand teammate a working `fused_findings.json` |
| 5:00–6:00 PM | Integration: teammate loads real data into Streamlit |
| 6:00 PM | **Venue closes.** Push to GitHub. |
| Evening (remote) | Tasks 10–12: Pass B, remaining videos, CSV/summary |

### Sunday April 26

| Time | Milestone |
|---|---|
| 9:00–10:00 AM | Fix whatever broke overnight |
| 10:00 AM–12:00 PM | Tasks 13–14 polish + ground truth validation. Teammate finalizes dashboard + records demo video |
| 12:00–1:00 PM | Lunch + submission prep: mission impact brief, README, push everything |
| **1:00 PM** | **HARD DEADLINE: Submissions due** |
| 2:00 PM | Presentations (10 min each) |
| 5:30 PM | Awards ceremony |

---

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Demo disaster | Grafton IL EF-1 tornado (April 2, 2025) | Real NWS survey + EMA report + YouTube aerial footage available |
| Input contract | Video file + report file + optional location hint via multipart POST | System is not autonomous — it fuses what it's given |
| Marengo usage | Embedding model (async video, sync text), cosine similarity locally | Bedrock does not expose a search API; embeddings are the primitive |
| Pegasus usage | Structured JSON output via `responseFormat.jsonSchema` | Enum-constrained outputs are easier to validate than free text |
| Fusion direction | Bidirectional (Report→Video AND Video→Report) | Catches both unreported damage and unverified claims |
| Confidence scoring | Tiered scorer: name-match floor (0.85), GPS spatial floor (0.80), medium blend (category 0.35 + severity 0.15 + building_type 0.10 + text 0.40) | Strong signals short-circuit weak ones |
| Classification | 4-way internal: corroborated / discrepancy / unreported / unverified → frontend: confirmed / conflicting_severity / unreported_damage / uncertain | Transformer maps at output boundary only |
| Frontend schema | Single transformer `src/output/frontend_schema.py` maps all field names + enums | One place to update if contract changes |
| Error handling | Flag, don't drop | Invalid findings stay in output with `is_valid=False` |
| API model | Job-based async (POST /analyze → job_id, poll /jobs/{id}) | Pegasus takes 2-5 min — synchronous call would timeout |
| NL query | Claude Haiku reads condensed findings JSON, returns answer + referenced IDs | No vector search needed at 28-finding scale; swap in at 1000+ |
| Geo simulation | `centre_override` param bypasses LLM geocoding for known locations | Claude mis-identified Grafton IL (small town); hardcoded centroid is more accurate |
| Model versions | Pegasus 1.2 (`us.twelvelabs.pegasus-1-2-v1:0`), Marengo 3.0 (`twelvelabs.marengo-embed-3-0-v1:0`), Claude Haiku 4.5 (`anthropic.claude-haiku-4-5-20251001-v1:0`) | Confirmed working on Bedrock us-east-1 |

---

## Data Sources

### Video

| Source | Type | Status | Notes |
|---|---|---|---|
| Katrina aerial/helicopter footage (2005) | YouTube / news archives | ⬜ Finding | Need at least one clip. Manual download. |
| Katrina 2025 Fox TV coverage retrospective | YouTube | ✅ Available | Published Aug 30, 2025 |

**Action:** Download at least one clip with `capture_date` set at upload time. Ingest script tags each uploaded video with its `capture_date`.

### Reports

| Source | Type | Format | Status |
|---|---|---|---|
| FEMA PDA — Florida DR-1602 | Preliminary Damage Assessment | DOCX | ✅ Have it |
| FEMA PW — Louisiana DR-1603 (Pumping Stations) | Project Worksheet | DOCX | ✅ Have it |
| Additional FEMA PWs for Katrina | Project Worksheets | TBD | ⬜ Optional — only if time allows |

Two report docs is enough for a credible demo. Don't spend Saturday hunting for more.

---

## Open Questions

| Question | Status |
|---|---|
| Are TwelveLabs models enabled on the hackathon AWS account? | ⬜ Confirm at registration |
| Will Pegasus extract useful findings from 2005-era helicopter footage? | ⬜ Test Saturday morning — biggest technical risk |
| Can Overture Maps geocode all FEMA-asset names + county centroids via DuckDB? | ⬜ Test during M3 geocoding sub-step |
| How does the teammate want `fused_findings.json` handed over? | ⬜ Decide file path + schema freeze time |

---

## Submission Checklist (Due Sunday 1 PM)

| Deliverable | Status |
|---|---|
| Working multi-source system (video + report → fused findings) | ⬜ |
| Interface showing correlated findings across modalities | ⬜ |
| Evidence chains (video clip + report excerpt + confidence) | ⬜ |
| Exports: GeoJSON, CSV, summary JSON | ⬜ |
| Demo video (4–6 min, teammate) | ⬜ |
| Architecture diagram (already in `docs/architecture.md`) | ✅ |
| Video processing approach documented (TwelveLabs via Bedrock) | ⬜ |
| Report parsing approach documented | ⬜ |
| GitHub repo with setup README | ⬜ |
| 3–5 example analytical products (saved JSON/GeoJSON) | ⬜ |
| Single-source vs multi-source comparison | ⬜ |
| Ground truth: 5–8 verified cross-source correlations | ⬜ |
| Precision/recall numbers | ⬜ |
| Error analysis writeup | ⬜ |
| Mission impact brief (1 page) | ⬜ |

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Pegasus returns poor results on 2005 helicopter footage | High | High | Test first thing Saturday. If bad, supplement with 2025 TV coverage footage |
| Solo backend = slower than 4-person parallel work | Certain | Medium | Frontend teammate can start with mock `fused_findings.json`; no hard blockers |
| TwelveLabs Bedrock access issues | Medium | Critical | Test at registration. Have direct TwelveLabs API key as backup |
| Geolocation fails for most video findings | High | Medium | Acceptable — show `geo_confidence=unresolved` transparently. Report claims still have lat/lon from Overture Maps |
| Run out of time before Pass B | Medium | Medium | Ship with Pass A only + Pegasus unreported findings. One-direction fusion still demonstrates the system |
| Report parser mis-extracts from DOCX | Medium | Medium | Fall back to hardcoded mock claims (in `docs/task_breakdown.md`) if parsing breaks |
