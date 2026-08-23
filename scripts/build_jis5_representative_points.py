from __future__ import annotations

from pathlib import Path
import argparse
import io

import pandas as pd
import requests

SOURCE_URL = "https://raw.githubusercontent.com/code4fukui/localgovjp/master/localgovjp-utf8.csv"
SOURCE_NAME = "code4fukui/localgovjp"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MUNI_TABLE = ROOT / "data" / "output" / "national_municipality_table.csv"
DEFAULT_OUTPUT = ROOT / "data" / "output" / "jis5_representative_points.csv"
DEFAULT_ENRICHED = ROOT / "data" / "output" / "national_municipality_table_with_coords.csv"
DEFAULT_UNMATCHED = ROOT / "data" / "output" / "jis5_representative_points.unmatched.csv"


def load_source(url: str = SOURCE_URL) -> pd.DataFrame:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    src = pd.read_csv(io.BytesIO(r.content), dtype={"lgcode": "string"})
    required = {"pref", "city", "lat", "lng", "lgcode"}
    missing = required - set(src.columns)
    if missing:
        raise ValueError(f"source missing columns: {sorted(missing)}")

    src = src.copy()
    src["lgcode"] = src["lgcode"].astype("string").str.zfill(6)
    src["muni_code"] = src["lgcode"].str[:5]
    src["latitude"] = pd.to_numeric(src["lat"], errors="coerce")
    src["longitude"] = pd.to_numeric(src["lng"], errors="coerce")
    src = src.dropna(subset=["muni_code", "latitude", "longitude"])
    src = src.drop_duplicates(subset=["muni_code"], keep="first")
    return src[["muni_code", "pref", "city", "latitude", "longitude", "lgcode"]]


def build_master(muni_table: Path, output: Path, enriched: Path, unmatched: Path) -> None:
    muni = pd.read_csv(muni_table, dtype={"muni_code": "string", "pref_code": "string", "medical_zone_code": "string"})
    muni["muni_code"] = muni["muni_code"].astype("string").str.zfill(5)

    src = load_source()
    merged = muni.merge(src, on="muni_code", how="left", validate="m:1")

    point_cols = [
        "muni_code",
        "prefecture",
        "municipality",
        "latitude",
        "longitude",
        "medical_zone_code",
        "medical_zone_name",
    ]
    points = merged[point_cols].copy()
    points["coordinate_source"] = SOURCE_NAME

    output.parent.mkdir(parents=True, exist_ok=True)
    points.to_csv(output, index=False, encoding="utf-8-sig")
    merged.to_csv(enriched, index=False, encoding="utf-8-sig")

    bad = merged[merged["latitude"].isna() | merged["longitude"].isna()].copy()
    bad.to_csv(unmatched, index=False, encoding="utf-8-sig")

    matched = len(merged) - len(bad)
    coverage = matched / len(merged) if len(merged) else 0.0
    print(f"rows={len(merged):,} matched={matched:,} unmatched={len(bad):,} coverage={coverage:.2%}")
    if len(bad):
        print(f"unmatched written to: {unmatched}")


def main() -> None:
    p = argparse.ArgumentParser(description="Build JIS5 -> representative latitude/longitude master by code join.")
    p.add_argument("--municipality-table", type=Path, default=DEFAULT_MUNI_TABLE)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--enriched-output", type=Path, default=DEFAULT_ENRICHED)
    p.add_argument("--unmatched-output", type=Path, default=DEFAULT_UNMATCHED)
    args = p.parse_args()
    build_master(args.municipality_table, args.output, args.enriched_output, args.unmatched_output)


if __name__ == "__main__":
    main()
