# DisasterFusion — Frontend Integration Guide

## Base URL

```
http://<EC2_IP>:8000
```

During local dev the backend runs at `http://localhost:8000`.

---

## Flow

```
1. User uploads video + report
        ↓
2. POST /analyze  →  { job_id: "b6aa37d1" }
        ↓
3. Poll GET /jobs/{job_id} every 5s
        ↓
4. When status == "done":
   GET /jobs/{job_id}/results   →  { center, zoom, findings }
   GET /jobs/{job_id}/overture  →  GeoJSON FeatureCollection

5. (Optional) Analyst asks natural-language questions:
   POST /jobs/{job_id}/query    →  { answer, referenced_ids, query_type }
```

---

## Endpoints

### `POST /analyze`

Submit a video and damage report for fusion analysis.

**Request** — `multipart/form-data`

| Field | Type | Required | Description |
|---|---|---|---|
| `video` | File | Yes | Video file (`.mp4`, `.mov`, `.avi`) |
| `report` | File(s) | Yes | One or more damage reports — append **multiple** `report` parts for several PDFs / docs (`.txt`, `.pdf`, `.docx`, `.csv`, `.json`). Backend should accept `List[UploadFile]` or equivalent for the `report` field name. |
| `location_hint` | String | No | Plain-English location e.g. `"Grafton, Illinois"` |

**Example (fetch)** — single report:
```js
const form = new FormData();
form.append("video",         videoFile);
form.append("report",        reportFile);
form.append("location_hint", "Grafton, Illinois");
```

**Multiple reports** — repeat the `report` key for each file:
```js
const form = new FormData();
form.append("video", videoFile);
for (const f of reportFiles) {
  form.append("report", f);
}
form.append("location_hint", "Grafton, Illinois");

const res  = await fetch("http://localhost:8000/analyze", { method: "POST", body: form });
const data = await res.json();
// data = { job_id: "b6aa37d1", status: "queued", message: "..." }
```

---

### `GET /jobs/{job_id}`

Poll for job progress.

**Response**

```json
{
  "job_id":   "b6aa37d1",
  "status":   "running",
  "progress": "Running Pegasus video analysis (this takes a few minutes)...",
  "error":    null
}
```

| `status` value | Meaning |
|---|---|
| `queued` | Job accepted, not started yet |
| `running` | Pipeline is running — check `progress` for current step |
| `done` | Results ready |
| `failed` | Something went wrong — check `error` field |

**Progress messages you'll see (in order):**
1. `"Uploading video to S3..."`
2. `"Running Pegasus video analysis (this takes a few minutes)..."`
3. `"Validating findings and geolocating..."`
4. `"Parsing damage report with Claude..."`
5. `"Starting Marengo video embedding..."`
6. `"Waiting for Marengo embedding to complete..."`
7. `"Running Pass A (claim-to-video alignment)..."`
8. `"Running Pass B fusion..."`
9. `"Building frontend response..."`
10. `"Fetching Overture map reference data..."`
11. `"Done — 28 findings"`

**Example polling loop**
```js
async function pollJob(jobId) {
  while (true) {
    const res  = await fetch(`http://localhost:8000/jobs/${jobId}`);
    const data = await res.json();

    updateProgressUI(data.progress);

    if (data.status === "done")    return fetchResults(jobId);
    if (data.status === "failed")  throw new Error(data.error);

    await new Promise(r => setTimeout(r, 5000)); // poll every 5s
  }
}
```

---

### `GET /jobs/{job_id}/results`

Returns the full findings payload. Only available when `status == "done"`.

**Response shape**

```json
{
  "center":   [38.9695, -90.4315],
  "zoom":     15,
  "findings": [ ...Finding objects... ]
}
```

Use `center` and `zoom` to initialize the map — do not hardcode a location.

**Finding object**

```json
{
  "id":             "ff-7ea73a1e",
  "entity_name":    "Drifters Eats and Drinks",
  "aliases":        [],
  "facility_type":  "commercial_plaza",
  "address":        "Drifters Eats and Drinks, Main Street, Grafton, Jersey County, Illinois",
  "lat":            38.969,
  "lon":            -90.4338,
  "event_date":     "2026-03-11",
  "final_severity": "severe",
  "fusion_status":  "confirmed",
  "confidence":     0.9037,
  "recommendation": "Building is unusable. Arrange structural engineering assessment before re-entry.",
  "video": {
    "source":              "grafton_tornado_ef1.mp4",
    "timestamp_start":     "00:01:13",
    "timestamp_end":       "00:01:18",
    "summary":             "The back and other side of the roof are completely off, leaving the interior exposed to the elements.",
    "clip_url":            null,
    "marengo_query":       "roof damage The back and other side of the roof are completely off...",
    "pegasus_description": "The back and other side of the roof are completely off, leaving the interior exposed to the elements."
  },
  "pdf": {
    "source":           "First Alert 4 / KSDK News — Grafton Business Damage Reports",
    "page":             1,
    "excerpt":          "Back roof completely removed. Second roof section on opposite side also completely gone...",
    "claimed_severity": "severe"
  },
  "overture": {
    "id":               "grafton-rc-001",
    "name":             "Drifters Eats and Drinks",
    "category":         "commercial",
    "geometry_type":    "place",
    "match_method":     "manual_grafton",
    "match_confidence": 0.9
  },
  "fusion": {
    "spatial_score":  null,
    "semantic_score": 0.3037,
    "temporal_score": null,
    "severity_score": 0.15,
    "final_score":    0.9037,
    "reasoning":      "roof_damage / severe at Drifters Eats and Drinks | Pegasus: roof_damage / severe | classification=corroborated score=0.90"
  }
}
```

**`fusion_status` values**

| Value | Meaning |
|---|---|
| `confirmed` | Video and report agree |
| `conflicting_severity` | Same location, severity disagrees |
| `unreported_damage` | Visible in video, not in any report |
| `uncertain` | Insufficient evidence |

**`final_severity` values:** `destroyed` · `severe` · `moderate` · `minor`

---

### `GET /jobs/{job_id}/overture`

Returns a GeoJSON `FeatureCollection` of places, buildings, and roads for the event bounding box. Use this as a reference layer on the map.

Each feature has a `properties.layer` field: `"places"` · `"buildings"` · `"transportation.segments"`

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "layer":    "places",
        "name":     "Drifters Eats and Drinks",
        "category": "restaurant",
        "ot_id":    "..."
      },
      "geometry": { "type": "Point", "coordinates": [-90.4318, 38.9676] }
    }
  ]
}
```

---

### `POST /jobs/{job_id}/query`

Natural language query over a completed job's findings. Powered by Claude Haiku.

**Request body**

```json
{ "question": "What severe damage did video find that reports missed?" }
```

**Response**

```json
{
  "answer":         "Video analysis identified 19 severe damage findings that official reports did not mention. Key unreported severe damage includes structural collapse at ff-1bad4ecd, ff-289fd92d, ff-80f6d687...",
  "referenced_ids": ["ff-1bad4ecd", "ff-289fd92d", "ff-80f6d687", "ff-ce5aa2fe"],
  "query_type":     "filter"
}
```

**`query_type` values**

| Value | Meaning |
|---|---|
| `filter` | Show specific findings — highlight `referenced_ids` on map |
| `summary` | Aggregate / overview question — show answer text only |
| `comparison` | Video vs report comparison — highlight refs |
| `detail` | Single-entity question — auto-zoom to that marker |

**Example queries that work well**
- *"What severe damage did video find that reports missed?"* → `filter`
- *"Where do video and reports agree?"* → `comparison`
- *"Tell me about Drifters restaurant"* → `detail`
- *"Summarize all infrastructure damage"* → `summary`

**Example (fetch)**
```js
async function askQuestion(jobId, question) {
  const res  = await fetch(`http://localhost:8000/jobs/${jobId}/query`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ question }),
  });
  const data = await res.json();

  showAnswer(data.answer);

  if (data.referenced_ids.length) {
    highlightMarkers(data.referenced_ids);
    if (data.query_type === "detail") {
      zoomToMarker(data.referenced_ids[0]);
    }
  }
}
```

**Errors**

| Code | Reason |
|---|---|
| `404` | Job not found, or no findings to query |
| `202` | Job is still running — wait for `status == "done"` |

---

### `GET /health`

Returns `{"status": "ok"}`. Use for liveness checks.

---

## HTTP Status Codes

| Code | Meaning |
|---|---|
| `200` | Success |
| `202` | Job exists but not done yet (poll again) |
| `400` | Bad request (missing/empty file) |
| `404` | Job ID not found |
| `500` | Pipeline failed — check `error` field in `/jobs/{id}` |
