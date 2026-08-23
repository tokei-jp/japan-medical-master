"""Build the simple municipality-level table for all of Japan (nationwide).

Same shape as build_muni_table.py (prefecture, pref_code, municipality,
muni_code, total_area_km2, habitable_area_km2, medical_zone_name,
medical_zone_code) but not filtered to one prefecture.

Designated-city (政令指定都市) wards need special handling: several share a
bare ward name with another designated city in the *same* prefecture (e.g.
Kanagawa has both Yokohama-shi and Sagamihara-shi wards named 緑区/南区;
Osaka has both Osaka-shi and Sakai-shi wards named 西区/北区), and the
medical-zone mapping file only ever prints the bare ward name. To resolve
these, ward rows in the habitable-area file are composited with their
parent city name (e.g. "横浜市緑区") using the JIS code's shared 3-digit
city prefix, and ambiguous mapping-file ward names are matched to the
composite whose sibling wards best overlap the rest of that zone's ward
list (see resolve_ward_candidate()).

Usage:
    python scripts/build_muni_table_national.py

Inputs expected at:
    data/raw/habitable_area.xlsx
    data/raw/medical_zone_mapping.xlsx
(see scripts/fetch_sources.py to download them)
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import pandas as pd

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "output"

HABITABLE_AREA_FILE = RAW_DIR / "habitable_area.xlsx"
MEDICAL_ZONE_FILE = RAW_DIR / "medical_zone_mapping.xlsx"
OUTPUT_CSV = OUT_DIR / "national_municipality_table.csv"
UNMATCHED_CSV = OUT_DIR / "national_municipality_table.unmatched.csv"

MUNI_CODE_KEYWORDS = ["市区町村コード", "団体コード", "地方公共団体コード"]
HABITABLE_AREA_KEYWORDS = ["可住地面積"]
TOTAL_AREA_KEYWORDS = ["総面積"]


def normalize_text(value) -> str:
    if pd.isna(value):
        return ""
    return unicodedata.normalize("NFKC", str(value)).replace("\n", "").strip()


def find_header_row(raw: pd.DataFrame, keyword_sets: list[list[str]], max_scan_rows: int = 15) -> int:
    for i in range(min(max_scan_rows, len(raw))):
        row_text = " ".join(normalize_text(v) for v in raw.iloc[i].tolist())
        for keywords in keyword_sets:
            if all(kw in row_text for kw in keywords):
                return i
    raise LookupError(f"Could not locate a header row matching any of {keyword_sets}.")


def find_col(df: pd.DataFrame, keywords: list[str]) -> str:
    for col in df.columns:
        if any(kw in normalize_text(col) for kw in keywords):
            return col
    raise LookupError(f"No column header contains any of {keywords}. Available columns: {list(df.columns)}")


def load_habitable_area_national() -> pd.DataFrame:
    """Return one row per municipality (all 47 prefectures), with:
    pref_code, prefecture, municipality (bare), match_name (composite
    city+ward for designated-city wards, else same as municipality),
    total_area_km2, habitable_area_km2.
    """
    raw = pd.read_excel(HABITABLE_AREA_FILE, header=None, sheet_name=0)
    header_row = find_header_row(raw, [MUNI_CODE_KEYWORDS, HABITABLE_AREA_KEYWORDS])
    df = pd.read_excel(HABITABLE_AREA_FILE, header=header_row, sheet_name=0)

    code_col = find_col(df, MUNI_CODE_KEYWORDS)
    habitable_col = find_col(df, HABITABLE_AREA_KEYWORDS)
    total_area_col = find_col(df, TOTAL_AREA_KEYWORDS)
    name_col = next(c for c in df.columns if normalize_text(c) == "市区町村")

    out = df[[code_col, name_col, total_area_col, habitable_col]].copy()
    out.columns = ["code_raw", "muni_name_raw", "total_area_km2", "habitable_area_km2"]
    out["digits"] = out["code_raw"].astype(str).str.strip().str.split(".").str[0]
    out["digits"] = out["digits"].str.replace(r"\D", "", regex=True)
    out["municipality"] = out["muni_name_raw"].map(normalize_text)
    out["total_area_km2"] = pd.to_numeric(out["total_area_km2"], errors="coerce")
    out["habitable_area_km2"] = pd.to_numeric(out["habitable_area_km2"], errors="coerce")

    # Prefecture-total rows have a 1-2 digit code; drop them (not municipalities).
    out = out[out["digits"].str.len() >= 4].copy()
    out["digits"] = out["digits"].str.zfill(5)
    out = out.dropna(subset=["habitable_area_km2"])
    out["pref_code"] = out["digits"].str[:2]

    # Prefecture name lookup, from the prefecture-total rows (1-2 digit codes).
    pref_raw = df[[code_col, name_col]].copy()
    pref_raw.columns = ["code_raw", "name_raw"]
    pref_raw["digits"] = pref_raw["code_raw"].astype(str).str.strip().str.split(".").str[0]
    pref_raw["digits"] = pref_raw["digits"].str.replace(r"\D", "", regex=True)
    pref_raw = pref_raw[(pref_raw["digits"].str.len() >= 1) & (pref_raw["digits"].str.len() <= 2)]
    pref_names = dict(
        zip(pref_raw["digits"].str.zfill(2), pref_raw["name_raw"].map(normalize_text))
    )
    out["prefecture"] = out["pref_code"].map(pref_names)

    # Designated-city (政令指定都市) ward rows: identified by name ending in
    # "区" (regular municipality names never end in a bare "区" in this
    # dataset; "地区" is excluded as a precaution though it doesn't occur
    # here). The source file lists each designated city's aggregate row
    # immediately followed by its own ward rows (in ascending JIS-code
    # order), so a sequential scan -- tracking the most recently seen
    # non-ward row within the same prefecture -- reliably assigns each
    # ward to its parent city without relying on JIS code arithmetic
    # (which is not uniform: e.g. Yokohama's wards are 14101-14118 but
    # Kawasaki's are 14131-14137, not a fixed-width block per city).
    out = out.reset_index(drop=True)
    is_ward = out["municipality"].str.endswith("区") & ~out["municipality"].str.endswith("地区")
    out["is_ward"] = is_ward

    parent_city = []
    current_pref = None
    current_city = None
    for pref_code, muni, ward in zip(out["pref_code"], out["municipality"], out["is_ward"]):
        if pref_code != current_pref:
            current_pref, current_city = pref_code, None
        if ward:
            parent_city.append(current_city)
        else:
            current_city = muni
            parent_city.append(None)
    out["parent_city"] = parent_city
    out["city_group"] = out["parent_city"].fillna(out["municipality"])
    out["match_name"] = out.apply(
        lambda r: (r["parent_city"] + r["municipality"]) if r["is_ward"] and pd.notna(r["parent_city"]) else r["municipality"],
        axis=1,
    )

    out["muni_code"] = out["digits"]
    return out[
        ["pref_code", "prefecture", "municipality", "muni_code", "match_name", "is_ward", "city_group",
         "total_area_km2", "habitable_area_km2"]
    ]


def load_medical_zone_mapping_national() -> pd.DataFrame:
    """Parse every prefecture block of the print-grid 二次医療圏－市区町村対応表.

    Returns rows of pref_code, medical_zone_code, medical_zone_name,
    municipality (bare ward/municipality name as printed).
    """
    raw = pd.read_excel(MEDICAL_ZONE_FILE, header=None, sheet_name=0)

    pref_header_rows = []  # (row_idx, pref_code, pref_name)
    for i in range(len(raw)):
        c0 = normalize_text(raw.iloc[i, 0])
        c1 = normalize_text(raw.iloc[i, 1])
        if c0.isdigit() and len(c0) <= 2 and c1 and pd.isna(raw.iloc[i, 2]):
            pref_header_rows.append((i, c0.zfill(2), c1))

    records: list[dict] = []
    for block_i, (start_idx, pref_code, pref_name) in enumerate(pref_header_rows):
        end_idx = (
            pref_header_rows[block_i + 1][0]
            if block_i + 1 < len(pref_header_rows)
            else len(raw)
        )
        current_zone_code: str | None = None
        current_zone_name: str | None = None
        i = start_idx + 1
        while i < end_idx:
            row = raw.iloc[i]
            col0 = normalize_text(row[0])
            col1 = normalize_text(row[1])
            if "医療圏名" in col0:
                i += 1
                continue
            if row.isna().all():
                i += 1
                continue
            if col0.isdigit() and len(col0) == 4:
                current_zone_code = col0
                current_zone_name = col1
            if current_zone_code is not None:
                for value in row.iloc[2:]:
                    muni_name = normalize_text(value)
                    if muni_name:
                        records.append(
                            {
                                "pref_code": pref_code,
                                "pref_name": pref_name,
                                "medical_zone_code": current_zone_code,
                                "medical_zone_name": current_zone_name,
                                "municipality": muni_name,
                            }
                        )
            i += 1

    out = pd.DataFrame.from_records(records)
    if len(pref_header_rows) != 47:
        raise ValueError(f"Expected 47 prefecture blocks, found {len(pref_header_rows)}.")
    return out


def resolve_ward_candidates(zone_df: pd.DataFrame, area_df: pd.DataFrame) -> pd.DataFrame:
    """Attach match_name to zone_df, resolving cases where a bare ward name
    is ambiguous within a prefecture (matches more than one composite name
    in area_df) by picking the candidate whose sibling wards best overlap
    the rest of that zone's municipality list."""
    # bare name -> list of (pref_code, match_name, city_prefix3) candidates
    ward_candidates = (
        area_df[area_df["is_ward"]]
        .groupby(["pref_code", "municipality"])["match_name"]
        .apply(lambda s: sorted(set(s)))
        .to_dict()
    )
    ward_siblings = (
        area_df[area_df["is_ward"]].groupby("city_group")["municipality"].apply(set).to_dict()
    )
    group_by_match_name = (
        area_df[area_df["is_ward"]].drop_duplicates("match_name").set_index("match_name")["city_group"].to_dict()
    )

    zone_muni_sets = zone_df.groupby(["pref_code", "medical_zone_code"])["municipality"].apply(set).to_dict()

    def resolve(row) -> str:
        key = (row["pref_code"], row["municipality"])
        candidates = ward_candidates.get(key)
        if not candidates:
            return row["municipality"]  # not a ward name at all, or no area match (handled later)
        if len(candidates) == 1:
            return candidates[0]
        zone_key = (row["pref_code"], row["medical_zone_code"])
        zone_munis = zone_muni_sets.get(zone_key, set())
        best_match, best_overlap = candidates[0], -1
        for cand in candidates:
            group = group_by_match_name[cand]
            siblings = ward_siblings.get(group, set())
            overlap = len(siblings & zone_munis)
            if overlap > best_overlap:
                best_match, best_overlap = cand, overlap
        return best_match

    zone_df = zone_df.copy()
    zone_df["match_name"] = zone_df.apply(resolve, axis=1)
    return zone_df


def collapse_stale_ward_boundaries(
    matched: pd.DataFrame, unmatched: pd.DataFrame, area_df: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Recover cases where a designated city's wards in the mapping file are
    partly stale (renamed/merged since the mapping file's reference date,
    e.g. Hamamatsu's 2024 reorg from 7 wards to 3 -- the medical-zone file
    is dated 令和5年12月31日, just before that took effect, so some of its
    old ward names, like 天竜区, are unchanged and match fine while others,
    like 中区/東区/西区/南区/北区/浜北区, don't exist anymore).

    Fires only when a zone has both unmatched leftover names AND at least
    one already-matched ward row belonging to a single designated city
    that appears in no other zone -- i.e. the whole city clearly lives in
    this one zone, so its remaining (stale-named) wards must too. In that
    case the zone's per-ward rows for that city (matched and unmatched)
    are collapsed into one city-level row, using the city's aggregate
    area/habitable-area figures. Ordinary multi-city ambiguity (e.g.
    Yokohama/Sagamihara sharing 緑区) is unaffected: that's already
    resolved by resolve_ward_candidates() using live ward names, so it
    never reaches this function as unmatched.

    Returns (rows_to_drop_from_matched, extra_rows, still_unmatched).
    """
    empty_extra = pd.DataFrame(
        columns=[
            "prefecture", "pref_code", "municipality", "muni_code",
            "total_area_km2", "habitable_area_km2", "medical_zone_name", "medical_zone_code",
        ]
    )
    if unmatched.empty:
        return matched.iloc[0:0], empty_extra, unmatched

    match_to_city_group = (
        area_df[area_df["is_ward"]].drop_duplicates("match_name").set_index("match_name")["city_group"].to_dict()
    )
    matched = matched.copy()
    matched["city_group"] = matched["match_name"].map(match_to_city_group)

    # Zones (pref_code, city_group) a designated city's wards are matched in.
    city_zone_counts = (
        matched.dropna(subset=["city_group"])
        .groupby(["pref_code", "city_group"])["medical_zone_code"]
        .nunique()
    )

    city_area_lookup = area_df[~area_df["is_ward"]].set_index(["pref_code", "municipality"])[
        ["prefecture", "muni_code", "total_area_km2", "habitable_area_km2"]
    ]

    new_rows = []
    drop_matched_idx = []
    drop_unmatched_idx = []
    for (pref_code, zone_code), grp in unmatched.groupby(["pref_code", "medical_zone_code"]):
        same_zone_matched = matched[
            (matched["pref_code"] == pref_code) & (matched["medical_zone_code"] == zone_code)
        ]
        candidate_groups = [
            g
            for g in same_zone_matched["city_group"].dropna().unique()
            if city_zone_counts.get((pref_code, g)) == 1  # this city lives in exactly this one zone
        ]
        if len(candidate_groups) != 1:
            continue  # ambiguous or no candidate -- leave unmatched rather than guess
        city_name = candidate_groups[0]
        key = (pref_code, city_name)
        if key not in city_area_lookup.index:
            continue
        area_row = city_area_lookup.loc[key]
        new_rows.append(
            {
                "prefecture": area_row["prefecture"],
                "pref_code": pref_code,
                "municipality": city_name,
                "muni_code": area_row["muni_code"],
                "total_area_km2": area_row["total_area_km2"],
                "habitable_area_km2": area_row["habitable_area_km2"],
                "medical_zone_name": grp["medical_zone_name"].iloc[0],
                "medical_zone_code": zone_code,
            }
        )
        drop_unmatched_idx.extend(grp.index.tolist())
        drop_matched_idx.extend(same_zone_matched[same_zone_matched["city_group"] == city_name].index.tolist())

    extra_rows = pd.DataFrame(new_rows) if new_rows else empty_extra
    rows_to_drop_from_matched = matched.loc[drop_matched_idx]
    still_unmatched = unmatched.drop(index=drop_unmatched_idx)
    return rows_to_drop_from_matched, extra_rows, still_unmatched


def build() -> tuple[pd.DataFrame, pd.DataFrame]:
    area_df = load_habitable_area_national()
    zone_df = load_medical_zone_mapping_national()
    zone_df = resolve_ward_candidates(zone_df, area_df)

    area_lookup = area_df.set_index(["pref_code", "match_name"])[
        ["prefecture", "muni_code", "total_area_km2", "habitable_area_km2"]
    ]
    # Guard against remaining ambiguity (duplicate (pref_code, match_name) in area_df).
    dup_area_keys = area_lookup.index[area_lookup.index.duplicated(keep=False)]
    if len(dup_area_keys) > 0:
        raise ValueError(f"Ambiguous composite names in habitable-area data: {set(dup_area_keys)}")

    merged = zone_df.join(area_lookup, on=["pref_code", "match_name"], how="left")
    matched = merged[merged["habitable_area_km2"].notna()].copy()
    unmatched = merged[merged["habitable_area_km2"].isna()].copy()

    rows_to_drop, extra_rows, unmatched = collapse_stale_ward_boundaries(matched, unmatched, area_df)
    matched = matched.drop(index=rows_to_drop.index)

    matched["prefecture"] = matched["prefecture"].fillna(matched["pref_name"])
    result = pd.concat(
        [
            matched[
                [
                    "prefecture", "pref_code", "municipality", "muni_code",
                    "total_area_km2", "habitable_area_km2", "medical_zone_name", "medical_zone_code",
                ]
            ],
            extra_rows,
        ],
        ignore_index=True,
    ).sort_values(["prefecture", "medical_zone_name", "municipality"])

    return result, unmatched


def main() -> None:
    if not HABITABLE_AREA_FILE.exists() or not MEDICAL_ZONE_FILE.exists():
        raise SystemExit(
            "Source files not found. Run scripts/fetch_sources.py first, or "
            f"place them manually at:\n  {HABITABLE_AREA_FILE}\n  {MEDICAL_ZONE_FILE}"
        )
    result, unmatched = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(result)} rows -> {OUTPUT_CSV}")

    if not unmatched.empty:
        unmatched[["pref_name", "medical_zone_name", "municipality"]].to_csv(
            UNMATCHED_CSV, index=False, encoding="utf-8-sig"
        )
        print(
            f"WARNING: {len(unmatched)} rows in the medical-zone mapping did not match a "
            f"habitable-area row -> {UNMATCHED_CSV}",
            file=sys.stderr,
        )
    else:
        UNMATCHED_CSV.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
