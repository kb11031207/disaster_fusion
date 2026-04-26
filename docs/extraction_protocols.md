# Extraction Protocols — Tornado

These are the standardized fields that BOTH report parsing AND video analysis must produce. When both sides output the same field names, enums, and severity scales, the fusion engine can actually match them.

---

## Why This Matters

The fusion engine compares video findings against report claims. If Pegasus calls something `"roof_damage"` but the report parser calls it `"roof torn off"`, cosine similarity drops and you get false negatives. Both sides need to output the SAME taxonomy.

---

## Shared Damage Taxonomy (Tornado)

### Damage Types (enum — used by BOTH video and report extraction)

| damage_type | What It Means | Video Example | Report Example |
|---|---|---|---|
| `structural_collapse` | Walls, frame, or entire structure failed | Building with walls removed, only foundation remaining | "Multiple townhouses flattened" |
| `roof_damage` | Roof partially or fully removed/punctured | Missing shingles, exposed rafters, hole in roof | "Roof completely removed" |
| `debris_field` | Scattered wreckage, building materials, furniture | Debris across road, insulation in trees | "Debris scattered across 200 yards" |
| `vegetation_damage` | Trees uprooted, snapped, debarked | Fallen trees on power lines, stripped branches | "Large trees uprooted" |
| `infrastructure_damage` | Roads, power lines, utility poles, bridges | Leaning utility poles, blocked roads | "Power lines down, road impassable" |
| `vehicle_damage` | Cars/trucks displaced, flipped, crushed | Overturned vehicles, cars pushed off road | "Vehicles displaced from parking lot" |
| `window_door_damage` | Windows blown out, doors removed | Shattered glass, missing patio doors | "Patio doors and screens blown out" |
| `flooding` | Water accumulation from storm | Standing water, submerged areas | "Flash flooding reported" |
| `other` | Anything not in above categories | — | — |

### Severity Scale (enum — used by BOTH)

| severity | Definition | EF-Scale Equivalent | Visual Indicators | Report Indicators |
|---|---|---|---|---|
| `minor` | Cosmetic damage, still functional | EF0 (65-85 mph) | Broken branches, missing shingles, minor debris | "Minor damage", "some damage" |
| `moderate` | Significant damage, partially usable | EF1 (86-110 mph) | Roof sections missing, large trees down, windows blown | "Moderate damage", "substantial damage" |
| `severe` | Major structural damage, unusable | EF2 (111-135 mph) | Roof fully removed, walls collapsed, heavy debris | "Severe damage", "destroyed" |
| `destroyed` | Total loss, structure gone | EF3+ (136+ mph) | Only foundation/slab remaining, swept clean | "Leveled", "total destruction" |

### Building Types (enum — used by BOTH)

| building_type | Examples |
|---|---|
| `residential` | Houses, apartments, mobile homes, townhouses |
| `commercial` | Restaurants, shops, resorts, hotels/motels |
| `industrial` | Warehouses, factories, grain bins |
| `public` | Schools, churches, government buildings |
| `infrastructure` | Power lines, roads, bridges, water systems |
| `agricultural` | Barns, outbuildings, silos, farm equipment |
| `unknown` | Can't determine from available info |

---

## Protocol A: Report Claim Extraction

### What Gets Extracted From Every Report

These are the fields the report parser (Person 2 or LLM) must extract from EVERY damage report, regardless of format:

```json
{
  "claim_id": "rc-001",

  "source_document": "filename.pdf",
  "source_type": "nws_survey | fema_pda | fema_pw | county_ema | news_report | insurance",
  "report_date": "2026-03-11",

  "event_type": "tornado",
  "event_date": "2026-03-11",
  "event_name": "Grafton EF1 Tornado",

  "location_name": "Drifters Eats and Drinks, Main Street, Grafton IL",
  "location_county": "Jersey County",
  "location_state": "IL",
  "lat": null,
  "lon": null,

  "damage_type": "roof_damage",
  "severity": "severe",
  "damage_description": "Back roof completely removed. Second roof section on opposite side also destroyed. Covered deck structure dislodged.",

  "building_type": "commercial",
  "building_name": "Drifters Eats and Drinks",
  "structures_affected": 1,

  "infrastructure_impacts": ["debris blocking street"],

  "ef_rating": "EF1",
  "max_winds_mph": 98,
  "tornado_path_length_mi": null,
  "tornado_path_width_yd": null,

  "injuries": 0,
  "fatalities": 0,
  "cost_estimate": null,

  "operational_status": "closed",
  "is_valid": true,
  "validation_errors": []
}
```

### Extraction Rules for Reports

1. **One claim per damaged location/structure.** If a report mentions 3 damaged buildings, extract 3 separate claims.
2. **Map report language to our enums.** "Blown out" → `window_door_damage`. "Roof off" → `roof_damage`. "Flattened" → `structural_collapse` + `destroyed`.
3. **If a structure has multiple damage types, pick the MOST SEVERE as primary** and list others in the description.
4. **EF rating → severity mapping:**
   - EF0 → `minor`
   - EF1 → `moderate` (default) or `severe` (if report says "significant/extensive")
   - EF2 → `severe`
   - EF3+ → `destroyed`
5. **If no lat/lon in report, leave null.** The geolocation module will handle it.
6. **Extract building names explicitly.** "Drifters Eats and Drinks" is a building_name. This helps with geocoding AND matching to video (Pegasus may identify the same name).

---

## Protocol B: Pegasus Video Extraction

### What Gets Extracted From Every Video

These are the fields Pegasus must return for every damage observation in the footage:

```json
{
  "finding_id": "vf-001",

  "source_video": "grafton_tornado_ef1.mp4",
  "timestamp_start": 0.0,
  "timestamp_end": 0.0,
  "capture_date": "2026-03-11",
  "capture_date_source": "user_supplied",

  "damage_type": "roof_damage",
  "severity": "severe",
  "damage_description": "Restaurant with entire rear roof section removed. Exposed interior visible. Deck structure missing, debris scattered across adjacent street.",

  "building_type": "commercial",
  "building_name": null,
  "structures_affected": 1,

  "location_indicators": ["Main Street sign visible", "Mississippi River in background"],
  "named_entities": ["Drifters"],

  "infrastructure_impacts": ["road partially blocked by roof debris"],

  "geo": null,
  "geo_method": null,
  "geo_confidence": "unresolved",

  "visual_evidence_quality": "clear",

  "is_valid": true,
  "validation_errors": []
}
```

### Pegasus Prompt for Tornado Analysis

```
Analyze this tornado damage footage. For each visually distinct
damaged area or structure, identify:

- damage_type: one of [structural_collapse, roof_damage, debris_field,
  vegetation_damage, infrastructure_damage, vehicle_damage,
  window_door_damage, flooding, other]
- severity: one of [minor, moderate, severe, destroyed]
- damage_description: detailed description of visible damage
- building_type: one of [residential, commercial, industrial, public,
  infrastructure, agricultural, unknown]
- building_name: any visible business name, sign, or identifier (null if not visible)
- structures_affected: estimated count of damaged structures visible
- location_indicators: list of any visible text, signs, street names,
  landmarks, or geographic features that could help identify the location
- named_entities: list of any business names, organization names, or
  place names visible or mentioned in audio
- infrastructure_impacts: list of infrastructure issues visible
  (e.g., "road blocked by debris", "power lines down")
- visual_evidence_quality: one of [clear, partial, poor]
  (clear = damage clearly visible, partial = partially obscured,
   poor = hard to assess from footage)

Focus on: structural collapse patterns, roof removal, debris scatter
direction, fallen trees, EF-scale-consistent damage signatures,
utility pole and power line damage.

Return a JSON array of findings. Each finding covers one visually
distinct damage area. Do not combine unrelated damage into one finding.
```

### Key Additions vs Our Original Protocol

| Field | Why Added |
|---|---|
| `building_name` | Pegasus can read signs. Reports have business names. Matching on name is the strongest signal. |
| `named_entities` | Separate from location_indicators — these are proper nouns that can be geocoded or matched to report claims. |
| `visual_evidence_quality` | Tells the fusion engine how much to trust this finding. A "poor" quality finding gets lower confidence. |
| `operational_status` (reports only) | "Closed", "partially open", "operational" — useful context for report claims. |
| `event_name` (reports only) | Groups claims under one event. Helps when system handles multiple disasters. |

---

## How Fusion Matches Them

The fusion engine now has multiple matching signals:

### Strong Signals (high confidence when matched)
1. **building_name match** — Report says "Drifters Eats and Drinks", Pegasus reads "Drifters" on a sign → very high confidence
2. **named_entity match** — Same proper noun appears in both sources
3. **Spatial proximity** — Both have coordinates within 200m

### Medium Signals
4. **damage_type match** — Both say `roof_damage`
5. **severity agreement** — Both say `severe`
6. **building_type match** — Both say `commercial`
7. **Semantic similarity** — Descriptions describe similar damage (via Marengo cosine or Claude rating)

### Weak Signals (supporting evidence only)
8. **infrastructure_impacts overlap** — Both mention "power lines down"
9. **structures_affected count** — Similar numbers

### Matching Priority
```
1. Try building_name / named_entity exact match first (fastest, highest confidence)
2. Then try spatial proximity (if both have coordinates)
3. Then try Marengo embedding cosine similarity
4. Finally, LLM semantic similarity (slowest, last resort)
```

---

## Severity Cross-Reference Table (Tornado)

For when the fusion engine needs to compare video-observed severity against report-stated severity:

| EF Rating | NWS Description | Our Severity | Expected Visual Damage |
|---|---|---|---|
| EF0 | Light | `minor` | Broken branches, damaged signs, surface roof damage |
| EF1 | Moderate | `moderate` | Roof sections gone, mobile homes overturned, large trees snapped |
| EF2 | Significant | `severe` | Roofs torn off, mobile homes destroyed, large trees uprooted |
| EF3 | Severe | `destroyed` | Entire stories destroyed, heavy cars thrown, most trees debarked |
| EF4 | Devastating | `destroyed` | Well-built homes leveled, structures blown off foundations |
| EF5 | Incredible | `destroyed` | Strong buildings swept away, steel-reinforced structures damaged |

---

## Standard Report Types We Accept

| Type | What It Contains | How to Get It |
|---|---|---|
| NWS Damage Survey | EF rating, path length/width, wind estimates, damage descriptions per location | NWS website, news articles summarizing survey results |
| County EMA Report | Damage summaries, injury/fatality counts, affected areas, emergency response status | County emergency management press releases |
| FEMA PDA | Residence counts by severity tier, cost estimates, county demographics | FEMA.gov (only for major declarations) |
| FEMA Project Worksheet | Site-specific damage, addresses, itemized repair costs | FEMA.gov (only for major declarations) |
| Insurance/Adjuster Report | Per-property damage assessment, cost estimates, photos | Typically private, but can mock based on real damage |
| News Report (structured) | Interviews with owners, damage descriptions, timeline | News articles — extract structured claims |

For the Grafton tornado, we have NWS survey data + County EMA statements + news reports. No FEMA docs (too small for a declaration). That's fine — the system handles whatever report types it gets.
