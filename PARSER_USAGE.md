# Report Parser Usage

The `src/report_parser/parser.py` module extracts structured damage claims from multiple input formats using Claude on Bedrock.

## Supported Input Formats

### 1. **DOCX Files** (File path)
```python
from src.report_parser.parser import parse_report

claims = parse_report("data/raw/reports/FEMA_PDA.docx")
# Extracts text from DOCX, runs Claude extraction, returns ReportClaim[]
```

### 2. **PDF Files** (File path)
```python
claims = parse_report("data/raw/reports/NWS_Survey.pdf")
# Passes PDF bytes directly to Claude as document block (native parsing)
```

### 3. **JSON Reports** (File path, pre-structured)
```python
claims = parse_report("data/raw/reports/grafton_nws_survey.json")
# Loads "claims" array directly, skips Claude (no extraction needed)
```

### 4. **Plain Text** (No file required)
```python
from src.report_parser.parser import parse_text

article_text = """EF1 tornado struck Grafton, Illinois on March 11, 2026...
Drifters restaurant roof completely removed..."""

claims = parse_text(
    article_text,
    source_name="Manual article excerpt",
    source_type="news_report"
)
```

### 5. **URLs** (Fetch and extract)
```python
from src.report_parser.parser import fetch_and_parse_url

claims = fetch_and_parse_url(
    url="https://www.firstalert4.com/2026/03/11/grafton-businesses-damaged-after-ef-1-tornado-touched-down/",
    source_type="news_report"
)
# Fetches URL, Claude extracts article text from HTML, parses claims
```

## Field Extraction

All methods produce the same output structure (`ReportClaim` dataclass):

```python
claim.id                      # UUID
claim.location_name           # "Drifters Eats and Drinks, Main Street, Grafton, IL"
claim.damage_type             # "roof_damage", "structural_collapse", etc.
claim.severity                # "minor", "moderate", "severe", "destroyed"
claim.damage_description      # Full description text
claim.building_type           # "commercial", "residential", etc.
claim.building_name           # Specific business/facility name
claim.structures_affected     # Count of affected buildings
claim.infrastructure_impacts  # ["roof debris on street", ...]
claim.ef_rating               # "EF1", etc. (if tornado)
claim.event_date              # "2026-03-11"
claim.source_document         # Original filename or URL
claim.source_type             # "news_report", "nws_survey", "county_ema", "fema_pda", "json"
```

## Example: Ingest Multiple Sources

```python
from src.report_parser.parser import parse_report, parse_text, fetch_and_parse_url

event_name = "Grafton EF1 Tornado"
event_date = "2026-03-11"

# Source 1: NWS damage survey (JSON)
nws_claims = parse_report("data/raw/reports/grafton_nws_survey.json")

# Source 2: News article (URL)
news_claims = fetch_and_parse_url(
    "https://www.firstalert4.com/2026/03/11/grafton-businesses-damaged-after-ef-1-tornado-touched-down/",
    source_type="news_report"
)

# Source 3: Manual text (copy-pasted from another source)
manual_text = """Dees Riverside Retreat suffered structural damage..."""
manual_claims = parse_text(manual_text, source_name="Local owner statement", source_type="news_report")

# Combine all claims
all_claims = nws_claims + news_claims + manual_claims
print(f"Extracted {len(all_claims)} total claims from {len(all_claims)} sources")
```

## Dependencies

- `boto3` — AWS Bedrock client (already in requirements)
- `httpx>=0.24.0` — HTTP client for URL fetching (added)

## Error Handling

All functions raise `FileNotFoundError`, `ValueError`, or `RuntimeError` with descriptive messages if:
- Report file doesn't exist
- File format is unsupported (e.g., `.xls`)
- Claude JSON extraction fails (malformed response)
- URL fetch fails (network error, timeout)
- AWS credentials not found

## Workflow Integration

In the pipeline (`api/pipeline_runner.py`), the report is already being parsed as a file. To add URL ingestion:

```python
# In the API endpoint:
@app.post("/analyze")
async def analyze(
    video: UploadFile,
    report_url: str = Form(default=""),  # Add URL field
):
    if report_url:
        claims = fetch_and_parse_url(report_url)
    else:
        report_bytes = await report.read()
        # ... existing file-based flow
```

## Notes

- `parse_text()` and `fetch_and_parse_url()` reuse the same Claude extraction prompt as file-based parsing.
- Extraction handles "noisy" sources (news articles, web content) — Claude filters to damage-only claims and maps journalist language to the standard severity enum.
- Pre-structured JSON (`.json` files) skip Claude entirely for speed.
- All functions accept `source_type` to tag the origin of each claim for downstream filtering/analysis.
