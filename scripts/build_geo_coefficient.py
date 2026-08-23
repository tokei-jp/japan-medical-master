"""Build the Nagano secondary-medical-zone geo_coefficient table.

Pipeline (see docs/ASSUMPTIONS.md for the full rationale):

1. Load habitable area by municipality (統計でみる市区町村のすがた, 表B 自然環境).
2. Load the municipality -> secondary medical zone mapping (病院報告 R5).
   This source is a print-oriented grid (prefecture blocks, zone code +
   zone name in the first two columns, municipality names spread across
   the remaining columns with continuation rows) rather than a normal
   one-row-per-municipality table, so it is parsed with a dedicated
   block scanner instead of the generic header/column lookup used for
   the habitable-area file.
3. Filter both to Nagano prefecture (JIS pref code 20).
4. Join on normalized municipality name (the mapping file has no
   municipality code column), aggregate habitable area per zone.
5. Attach placeholder target_facility_count / simple_required_fte.
6. Compute geo_coefficient per the fixed formula and adjusted_required_fte.
7. Write data/output/nagano_secondary_medical_zone_geo_coefficient.csv.

Usage:
    python scripts/build_geo_coefficient.py

Inputs expected at:
    data/raw/habitable_area.xlsx
    data/raw/medical_zone_mapping.xlsx
(see scripts/fetch_sources.py to download them)
"""

from __future__ import annotations

import unicodedata
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
# e-Stat headers wrap over multiple lines within a single cell (e.g.
# "市区\n町村\nｺｰﾄﾞ") and mix half-width/full-width forms, so header/column
# matching normalizes text with NFKC and strips newlines before comparing.
MUNI_CODE_KEYWORDS = ["市区町村コード", "団体コード", "地方公共団体コード"]
HABITABLE_AREA_KEYWORDS = ["可住地面積"]
MEDICAL_ZONE_NAME_KEYWORDS = ["二次医療圏"]

NAGANO_ZONE_COUNT = 10  # sanity check: Nagano has 10 secondary medical zones


def normalize_text(value) -> str:
    """NFKC-normalize (half-width -> full-width etc.) and strip whitespace,
    including embedded newlines from wrapped e-Stat headers."""
    if pd.isna(value):
        return ""
    return unicodedata.normalize("NFKC", str(value)).replace("\n", "").strip()


def normalize_muni_name(value) -> str:
    """Strip a municipality name down to its bare form for matching across
    sources (e-Stat pads names with trailing full-width spaces)."""
    return normalize_text(value)


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
        row_text = " ".join(normalize_text(v) for v in raw.iloc[i].tolist())
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
        col_text = normalize_text(col)
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
    # The Japanese municipality-name column header is the bare "市区町村"
    # (exact match), distinct from "市区町村コード"/"Municipalities".
    name_col = next(c for c in df.columns if normalize_text(c) == "市区町村")

    out = df[[code_col, area_col, name_col]].copy()
    out.columns = ["muni_code_raw", "habitable_area_km2", "muni_name_raw"]
    out["jis_code"] = out["muni_code_raw"].map(normalize_muni_code)
    out["muni_name"] = out["muni_name_raw"].map(normalize_muni_name)
    out["habitable_area_km2"] = pd.to_numeric(out["habitable_area_km2"], errors="coerce")
    out = out.dropna(subset=["jis_code", "habitable_area_km2"])
    out = out[out["jis_code"].str.startswith(NAGANO_PREF_CODE)]
    return out[["jis_code", "muni_name", "habitable_area_km2"]]


def load_medical_zone_mapping() -> pd.DataFrame:
    """Parse the 二次医療圏－市区町村対応表 print-grid layout.

    The sheet is organized as one block per prefecture: a "<pref code>
    <pref name>" row, a label row, then rows of "<zone code> <zone name>
    <municipality names...>" with continuation rows (blank zone
    code/name) carrying extra municipality names for the same zone. There
    is no municipality-code column, so municipality names are the join
    key (normalized to match load_habitable_area's names).
    """
    raw = pd.read_excel(MEDICAL_ZONE_FILE, header=None, sheet_name=0)

    pref_row_idx = None
    for i in range(len(raw)):
        code_text = normalize_text(raw.iloc[i, 0])
        name_text = normalize_text(raw.iloc[i, 1])
        if code_text.isdigit() and len(code_text) <= 2 and name_text == PREFECTURE_NAME:
            pref_row_idx = i
            break
    if pref_row_idx is None:
        raise LookupError(f"Could not find a prefecture block for {PREFECTURE_NAME!r}.")

    records: list[dict] = []
    current_zone: dict | None = None
    i = pref_row_idx + 1
    while i < len(raw):
        row = raw.iloc[i]
        col0 = normalize_text(row[0])
        col1 = normalize_text(row[1])

        # Next prefecture's header row: "<=2-digit code>" with no zone data.
        if col0.isdigit() and len(col0) <= 2 and pd.isna(row[2]):
            break
        # Column-label row ("二次医療圏名 / 市区町村名").
        if "医療圏名" in col0:
            i += 1
            continue
        if row.isna().all():
            i += 1
            continue

        if col0.isdigit() and len(col0) == 4:
            current_zone = {"medical_zone_code": col0, "medical_zone_name": col1}

        if current_zone is not None:
            for value in row.iloc[2:]:
                muni_name = normalize_text(value)
                if muni_name:
                    records.append(
                        {
                            "medical_zone_code": current_zone["medical_zone_code"],
                            "medical_zone_name": current_zone["medical_zone_name"],
                            "muni_name": muni_name,
                        }
                    )
        i += 1

    out = pd.DataFrame.from_records(records)
    zone_count = out["medical_zone_name"].nunique()
    if zone_count != NAGANO_ZONE_COUNT:
        raise ValueError(
            f"Expected {NAGANO_ZONE_COUNT} secondary medical zones for "
            f"{PREFECTURE_NAME}, parsed {zone_count}. Inspect the source "
            "file's Nagano block for a layout change."
        )
    return out


def build() -> pd.DataFrame:
    area_df = load_habitable_area()
    zone_df = load_medical_zone_mapping()

    merged = zone_df.merge(area_df, on="muni_name", how="left", validate="one_to_one")
    missing = merged[merged["habitable_area_km2"].isna()]
    if not missing.empty:
        raise ValueError(
            "Some Nagano municipalities in the medical zone mapping did not "
            f"match a habitable-area row (check name normalization):\n{missing}"
        )

    agg = merged.groupby("medical_zone_name", as_index=False).agg(
        habitable_area_km2=("habitable_area_km2", "sum"),
        municipality_list=("muni_name", lambda s: "、".join(s)),
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
