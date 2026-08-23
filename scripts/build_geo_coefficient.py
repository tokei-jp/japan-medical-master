"""Build the Nagano secondary-medical-zone geo_coefficient table.

Pipeline (see docs/ASSUMPTIONS.md for the full rationale):

1. Load habitable area by municipality (統計でみる市区町村のすがた, 表B 自然環境).
2. Load the municipality -> secondary medical zone mapping (病院報告 R5).
3. Filter both to Nagano prefecture (JIS pref code 20).
4. Join on 5-digit JIS municipality code, aggregate habitable area per zone.
5. Attach placeholder target_facility_count / simple_required_fte.
6. Compute geo_coefficient per the fixed formula and adjusted_required_fte.
7. Write data/output/nagano_secondary_medical_zone_geo_coefficient.csv.

Usage:
    python scripts/build_geo_coefficient.py

Inputs expected at:
    data/raw/habitable_area.xlsx
    data/raw/medical_zone_mapping.xlsx
(see scripts/fetch_sources.py to download them)

IMPORTANT: The header-row / column-keyword detection below is written
defensively because the exact e-Stat sheet layout was not available to
inspect when this script was authored (see docs/ASSUMPTIONS.md, item 0).
After downloading the real files, run this script; if it raises
LookupError, open the offending sheet, find the real header text, and add
it to the keyword lists in HABITABLE_AREA_HEADER_KEYWORDS /
MEDICAL_ZONE_HEADER_KEYWORDS below.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "output"

HABITABLE_AREA_FILE = RAW_DIR / "habitable_area.xlsx"
MEDICAL_ZONE_FILE = RAW_DIR / "medical_zone_mapping.xlsx"
OUTPUT_CSV = OUT_DIR / "nagano_secondary_medical_zone_geo_coefficient.csv"

NAGANO_PREF_CODE = "20"
PREFECTURE_NAME = "長野県"

# ---------------------------------------------------------------------------
# Placeholder constants (要検証事項 -- replace with real data per the task).
# ---------------------------------------------------------------------------
DEFAULT_TARGET_FACILITY_COUNT = 10
DEFAULT_SIMPLE_REQUIRED_FTE = 3.0

# geo_coefficient formula constants (initial values, subject to calibration).
LAMBDA = 0.3
COEF_MIN = 0.85
COEF_MAX = 1.30

NOTE_TEXT = (
    "target_facility_count と simple_required_fte は仮の定数であり、"
    "実データに差し替えが必要。geo_coefficient の benchmark は長野県内"
    "二次医療圏の spacing 中央値（初期値）。lambda=0.3, min=0.85, max=1.30 は"
    "初期値で実績Call数等によるCalibration予定。"
)

# Column-header keyword hints used for defensive auto-detection.
MUNI_CODE_KEYWORDS = ["市区町村コード", "団体コード", "地方公共団体コード"]
HABITABLE_AREA_KEYWORDS = ["可住地面積"]
MEDICAL_ZONE_NAME_KEYWORDS = ["二次医療圏"]


def normalize_muni_code(value) -> str | None:
    """Normalize a JIS municipality code to a zero-padded 5-digit string."""
    if pd.isna(value):
        return None
    s = str(value).strip()
    s = s.split(".")[0]  # strip stray float formatting like '20201.0'
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if len(digits) >= 6:
        digits = digits[:5]  # drop the JIS check digit
    return digits.zfill(5)


def find_header_row(raw: pd.DataFrame, keyword_sets: list[list[str]], max_scan_rows: int = 15) -> int:
    """Scan the first rows of a headerless read for the row containing all
    of the keywords in any one of the given keyword sets."""
    for i in range(min(max_scan_rows, len(raw))):
        row_text = " ".join(str(v) for v in raw.iloc[i].tolist())
        for keywords in keyword_sets:
            if all(kw in row_text for kw in keywords):
                return i
    raise LookupError(
        f"Could not locate a header row matching any of {keyword_sets} in the "
        f"first {max_scan_rows} rows. Inspect the file manually and update "
        "the keyword lists at the top of this script."
    )


def find_col(df: pd.DataFrame, keywords: list[str]) -> str:
    for col in df.columns:
        col_text = str(col)
        if any(kw in col_text for kw in keywords):
            return col
    raise LookupError(
        f"No column header contains any of {keywords}. Available columns: "
        f"{list(df.columns)}"
    )


def load_habitable_area() -> pd.DataFrame:
    raw = pd.read_excel(HABITABLE_AREA_FILE, header=None, sheet_name=0)
    header_row = find_header_row(raw, [MUNI_CODE_KEYWORDS, HABITABLE_AREA_KEYWORDS])
    df = pd.read_excel(HABITABLE_AREA_FILE, header=header_row, sheet_name=0)

    code_col = find_col(df, MUNI_CODE_KEYWORDS)
    area_col = find_col(df, HABITABLE_AREA_KEYWORDS)

    out = df[[code_col, area_col]].copy()
    out.columns = ["muni_code_raw", "habitable_area_km2"]
    out["jis_code"] = out["muni_code_raw"].map(normalize_muni_code)
    out["habitable_area_km2"] = pd.to_numeric(out["habitable_area_km2"], errors="coerce")
    out = out.dropna(subset=["jis_code", "habitable_area_km2"])
    out = out[out["jis_code"].str.startswith(NAGANO_PREF_CODE)]
    return out[["jis_code", "habitable_area_km2"]]


def load_medical_zone_mapping() -> pd.DataFrame:
    raw = pd.read_excel(MEDICAL_ZONE_FILE, header=None, sheet_name=0)
    header_row = find_header_row(raw, [MUNI_CODE_KEYWORDS, MEDICAL_ZONE_NAME_KEYWORDS])
    df = pd.read_excel(MEDICAL_ZONE_FILE, header=header_row, sheet_name=0)

    code_col = find_col(df, MUNI_CODE_KEYWORDS)
    zone_col = find_col(df, MEDICAL_ZONE_NAME_KEYWORDS)

    # Municipality display name column, if present, for municipality_list.
    try:
        name_col = find_col(df, ["市区町村名", "市町村名"])
    except LookupError:
        name_col = None

    cols = [code_col, zone_col] + ([name_col] if name_col else [])
    out = df[cols].copy()
    out.columns = ["muni_code_raw", "medical_zone_name"] + (["muni_name"] if name_col else [])
    out["jis_code"] = out["muni_code_raw"].map(normalize_muni_code)
    out = out.dropna(subset=["jis_code", "medical_zone_name"])
    out = out[out["jis_code"].str.startswith(NAGANO_PREF_CODE)]
    return out


def build() -> pd.DataFrame:
    area_df = load_habitable_area()
    zone_df = load_medical_zone_mapping()

    merged = zone_df.merge(area_df, on="jis_code", how="left", validate="one_to_one")
    missing = merged[merged["habitable_area_km2"].isna()]
    if not missing.empty:
        raise ValueError(
            "Some Nagano municipalities in the medical zone mapping did not "
            f"match a habitable-area row (check code normalization):\n{missing}"
        )

    has_names = "muni_name" in merged.columns
    agg = merged.groupby("medical_zone_name", as_index=False).agg(
        habitable_area_km2=("habitable_area_km2", "sum"),
        municipality_list=("muni_name" if has_names else "jis_code", lambda s: "、".join(sorted(set(s.astype(str))))),
    )

    agg["prefecture"] = PREFECTURE_NAME
    agg["target_facility_count"] = DEFAULT_TARGET_FACILITY_COUNT
    agg["simple_required_fte"] = DEFAULT_SIMPLE_REQUIRED_FTE

    agg["spacing"] = agg["habitable_area_km2"] / agg["target_facility_count"]
    benchmark = agg["spacing"].median()
    agg["benchmark_used"] = benchmark
    relative_distance = np.sqrt(agg["spacing"] / benchmark)
    agg["geo_coefficient"] = (1 + LAMBDA * (relative_distance - 1)).clip(lower=COEF_MIN, upper=COEF_MAX)
    agg["adjusted_required_fte"] = agg["simple_required_fte"] * agg["geo_coefficient"]
    agg["note"] = NOTE_TEXT

    return agg[
        [
            "prefecture",
            "medical_zone_name",
            "municipality_list",
            "habitable_area_km2",
            "target_facility_count",
            "geo_coefficient",
            "simple_required_fte",
            "adjusted_required_fte",
            "benchmark_used",
            "note",
        ]
    ].sort_values("medical_zone_name")


def main() -> None:
    if not HABITABLE_AREA_FILE.exists() or not MEDICAL_ZONE_FILE.exists():
        raise SystemExit(
            "Source files not found. Run scripts/fetch_sources.py first, or "
            f"place them manually at:\n  {HABITABLE_AREA_FILE}\n  {MEDICAL_ZONE_FILE}"
        )
    result = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(result)} rows -> {OUTPUT_CSV}")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
