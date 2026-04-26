"""
M6 — Generate overture_reference.geojson for the Grafton IL bounding box.

Queries three Overture layers via DuckDB → public S3 parquet:
  - buildings   (Polygon / MultiPolygon)
  - places      (Point)
  - transportation.segments (LineString)

Output: exports/overture_reference.geojson  (GeoJSON FeatureCollection)

The frontend splits features by properties.layer into three Leaflet overlays.

Usage
=====
    python scripts/m6_overture_geojson.py
    python scripts/m6_overture_geojson.py --bbox 38.955 -90.450 38.990 -90.410
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

OVERTURE_RELEASE = "2026-04-15.0"
OVERTURE_BASE    = f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}"

# Grafton IL waterfront bounding box — covers downtown + Aeries (~1.5 mi NE)
DEFAULT_BBOX = (38.955, -90.455, 38.990, -90.410)  # (min_lat, min_lon, max_lat, max_lon)

DEFAULT_OUTPUT = _PROJECT_ROOT / "exports" / "overture_reference.geojson"


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs;  LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    return con


def query_places(con, min_lat, min_lon, max_lat, max_lon) -> list[dict]:
    url = f"{OVERTURE_BASE}/theme=places/type=place/*.parquet"
    sql = f"""
        SELECT
            names.primary                                AS name,
            categories.primary                           AS category,
            id                                           AS ot_id,
            ST_AsGeoJSON(geometry)       AS geom_json
        FROM read_parquet('{url}')
        WHERE bbox.xmin >= {min_lon}
          AND bbox.xmax <= {max_lon}
          AND bbox.ymin >= {min_lat}
          AND bbox.ymax <= {max_lat}
        LIMIT 300
    """
    rows = con.execute(sql).fetchall()
    features = []
    for name, category, ot_id, geom_json in rows:
        if not geom_json:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "layer":    "places",
                "name":     name or "Place",
                "category": category or "",
                "ot_id":    ot_id or "",
            },
            "geometry": json.loads(geom_json),
        })
    return features


def query_buildings(con, min_lat, min_lon, max_lat, max_lon) -> list[dict]:
    url = f"{OVERTURE_BASE}/theme=buildings/type=building/*.parquet"
    sql = f"""
        SELECT
            names.primary                                AS name,
            class                                        AS category,
            id                                           AS ot_id,
            height                                       AS height_m,
            ST_AsGeoJSON(geometry)       AS geom_json
        FROM read_parquet('{url}')
        WHERE bbox.xmin >= {min_lon}
          AND bbox.xmax <= {max_lon}
          AND bbox.ymin >= {min_lat}
          AND bbox.ymax <= {max_lat}
        LIMIT 500
    """
    rows = con.execute(sql).fetchall()
    features = []
    for name, category, ot_id, height_m, geom_json in rows:
        if not geom_json:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "layer":    "buildings",
                "name":     name or "Building",
                "category": category or "",
                "ot_id":    ot_id or "",
                "height_m": height_m,
            },
            "geometry": json.loads(geom_json),
        })
    return features


def query_roads(con, min_lat, min_lon, max_lat, max_lon) -> list[dict]:
    url = f"{OVERTURE_BASE}/theme=transportation/type=segment/*.parquet"
    sql = f"""
        SELECT
            names.primary                                AS name,
            class                                        AS category,
            id                                           AS ot_id,
            ST_AsGeoJSON(geometry)       AS geom_json
        FROM read_parquet('{url}')
        WHERE bbox.xmin >= {min_lon}
          AND bbox.xmax <= {max_lon}
          AND bbox.ymin >= {min_lat}
          AND bbox.ymax <= {max_lat}
          AND class IN (
              'primary', 'secondary', 'tertiary',
              'residential', 'trunk', 'motorway'
          )
        LIMIT 300
    """
    rows = con.execute(sql).fetchall()
    features = []
    for name, category, ot_id, geom_json in rows:
        if not geom_json:
            continue
        # Map Overture road class → frontend category names
        cat_map = {
            "trunk":     "highway.trunk",
            "motorway":  "highway.trunk",
            "primary":   "highway.primary",
            "secondary": "highway.secondary",
            "tertiary":  "highway.secondary",
            "residential": "highway.residential",
        }
        features.append({
            "type": "Feature",
            "properties": {
                "layer":    "transportation.segments",
                "name":     name or "Road",
                "category": cat_map.get(category or "", category or ""),
                "ot_id":    ot_id or "",
            },
            "geometry": json.loads(geom_json),
        })
    return features


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float,
                    metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"),
                    default=list(DEFAULT_BBOX),
                    help="Bounding box for the query.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    min_lat, min_lon, max_lat, max_lon = args.bbox
    print(f"Bbox: lat [{min_lat}, {max_lat}]  lon [{min_lon}, {max_lon}]")

    con = _connect()
    all_features: list[dict] = []

    for label, fn in [
        ("places",    query_places),
        ("buildings", query_buildings),
        ("roads",     query_roads),
    ]:
        print(f"Querying {label}...", end=" ", flush=True)
        feats = fn(con, min_lat, min_lon, max_lat, max_lon)
        print(f"{len(feats)} features")
        all_features.extend(feats)

    fc = {
        "type":     "FeatureCollection",
        "features": all_features,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fc))
    size_kb = args.out.stat().st_size / 1024
    print(f"\nWrote {len(all_features)} features -> {args.out.relative_to(_PROJECT_ROOT)} ({size_kb:.0f} KB)")
    layer_counts = {}
    for f in all_features:
        l = f["properties"]["layer"]
        layer_counts[l] = layer_counts.get(l, 0) + 1
    print("By layer:", layer_counts)

    return 0


if __name__ == "__main__":
    sys.exit(main())
