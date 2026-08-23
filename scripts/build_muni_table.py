"""Build a simple Nagano municipality-level table.

No aggregation, no geo_coefficient — just the raw join requested:
    prefecture, municipality, total_area_km2, habitable_area_km2, medical_zone_name

Usage:
    python scripts/build_muni_table.py

Inputs expected at:
    data/raw/habitable_area.xlsx
    data/raw/medical_zone_mapping.xlsx
(see scripts/fetch_sources.py to download them)
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pandas as pd

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "output"

HABITABLE_AREA_FILE = RAW_DIR / "habitable_area.xlsx"
MEDICAL_ZONE_FILE = RAW_DIR / "medical_zone_mapping.xlsx"
OUTPUT_CSV = OUT_DIR / "nagano_municipality_table.csv"

NAGANO_PREF_CODE = "20"
PREFECTURE_NAME = "長野県"
NAGANO_ZONE_COUNT = 10  # sanity check: Nagano has 10 secondary medical zones

MUNI_CODE_KEYWORDS = ["市区町村コード", "団体コード", "地方公共団体コード"]
HABITABLE_AREA_KEYWORDS = ["可住地面積"]
TOTAL_AREA_KEYWORDS = ["総面積"]


def normalize_text(value) -> str:
    """NFKC-normalize (half-width -> full-width etc.) and strip whitespace,
    including embedded newlines from wrapped e-Stat headers."""
    if pd.isna(value):
        return ""
    return unicodedata.normalize("NFKC", str(value)).replace("\n", "").strip()


def normalize_muni_code(value) -> str | None:
    """Normalize a JIS municipality code to a zero-padded 5-digit string."""
    if pd.isna(value):
        return None
    s = str(value).strip().split(".")[0]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if len(digits) >= 6:
        digits = digits[:5]  # drop the JIS check digit
    return digits.zfill(5)


def find_header_row(raw: pd.DataFrame, keyword_sets: list[list[str]], max_scan_rows: int = 15) -> int:
    for i in range(min(max_scan_rows, len(raw))):
        row_text = " ".join(normalize_text(v) for v in raw.iloc[i].tolist())
        for keywords in keyword_sets:
            if all(kw in row_text for kw in keywords):
                return i
    raise LookupError(
        f"Could not locate a header row matching any of {keyword_sets} in the "
        f"first {max_scan_rows} rows."
    )


def find_col(df: pd.DataFrame, keywords: list[str]) -> str:
    for col in df.columns:
        if any(kw in normalize_text(col) for kw in keywords):
            return col
    raise LookupError(f"No column header contains any of {keywords}. Available columns: {list(df.columns)}")


def load_habitable_area() -> pd.DataFrame:
    raw = pd.read_excel(HABITABLE_AREA_FILE, header=None, sheet_name=0)
    header_row = find_header_row(raw, [MUNI_CODE_KEYWORDS, HABITABLE_AREA_KEYWORDS])
    df = pd.read_excel(HABITABLE_AREA_FILE, header=header_row, sheet_name=0)

    code_col = find_col(df, MUNI_CODE_KEYWORDS)
    habitable_col = find_col(df, HABITABLE_AREA_KEYWORDS)
    total_area_col = find_col(df, TOTAL_AREA_KEYWORDS)
    # The Japanese municipality-name column header is the bare "市区町村"
    # (exact match), distinct from "市区町村コード"/"Municipalities".
    name_col = next(c for c in df.columns if normalize_text(c) == "市区町村")

    out = df[[code_col, name_col, total_area_col, habitable_col]].copy()
    out.columns = ["muni_code_raw", "muni_name_raw", "total_area_km2", "habitable_area_km2"]
    out["jis_code"] = out["muni_code_raw"].map(normalize_muni_code)
    out["municipality"] = out["muni_name_raw"].map(normalize_text)
    out["total_area_km2"] = pd.to_numeric(out["total_area_km2"], errors="coerce")
    out["habitable_area_km2"] = pd.to_numeric(out["habitable_area_km2"], errors="coerce")
    out = out.dropna(subset=["jis_code", "habitable_area_km2"])
    out = out[out["jis_code"].str.startswith(NAGANO_PREF_CODE)]
    return out[["municipality", "total_area_km2", "habitable_area_km2"]]


def load_medical_zone_mapping() -> pd.DataFrame:
    """Parse the 二次医療圏－市区町村対応表 print-grid layout (see
    docs/ASSUMPTIONS.md item 0 for why this needs a dedicated parser
    instead of a normal header/column lookup)."""
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
    current_zone_name: str | None = None
    i = pref_row_idx + 1
    while i < len(raw):
        row = raw.iloc[i]
        col0 = normalize_text(row[0])
        col1 = normalize_text(row[1])

        if col0.isdigit() and len(col0) <= 2 and pd.isna(row[2]):
            break  # next prefecture's header row
        if "医療圏名" in col0:
            i += 1
            continue
        if row.isna().all():
            i += 1
            continue

        if col0.isdigit() and len(col0) == 4:
            current_zone_name = col1

        if current_zone_name is not None:
            for value in row.iloc[2:]:
                muni_name = normalize_text(value)
                if muni_name:
                    records.append({"medical_zone_name": current_zone_name, "municipality": muni_name})
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

    merged = zone_df.merge(area_df, on="municipality", how="left", validate="one_to_one")
    missing = merged[merged["habitable_area_km2"].isna()]
    if not missing.empty:
        raise ValueError(
            "Some Nagano municipalities in the medical zone mapping did not "
            f"match a habitable-area row (check name normalization):\n{missing}"
        )

    merged["prefecture"] = PREFECTURE_NAME
    return merged[
        ["prefecture", "municipality", "total_area_km2", "habitable_area_km2", "medical_zone_name"]
    ].sort_values(["medical_zone_name", "municipality"])


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
