from __future__ import annotations

from pathlib import Path
import argparse
import math

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POINTS = ROOT / "data" / "output" / "jis5_representative_points.csv"
DEFAULT_OUTPUT = ROOT / "data" / "output" / "secondary_medical_zone_dispersion.csv"
DEFAULT_UNMATCHED = ROOT / "data" / "output" / "secondary_medical_zone_dispersion.unmatched_targets.csv"
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    lat1 = np.radians(lat1)
    lon1 = np.radians(lon1)
    lat2 = np.radians(lat2)
    lon2 = np.radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def weighted_spherical_centroid(lat_deg: pd.Series, lon_deg: pd.Series, w: pd.Series):
    lat = np.radians(lat_deg.to_numpy(float))
    lon = np.radians(lon_deg.to_numpy(float))
    weights = w.to_numpy(float)

    x = np.sum(weights * np.cos(lat) * np.cos(lon))
    y = np.sum(weights * np.cos(lat) * np.sin(lon))
    z = np.sum(weights * np.sin(lat))
    total = np.sum(weights)
    x /= total
    y /= total
    z /= total

    lon_c = math.atan2(y, x)
    hyp = math.hypot(x, y)
    lat_c = math.atan2(z, hyp)
    return math.degrees(lat_c), math.degrees(lon_c)


def calculate(targets: Path, jis5_col: str, points: Path, output: Path, unmatched: Path) -> None:
    t = pd.read_csv(targets, dtype={jis5_col: "string"})
    if jis5_col not in t.columns:
        raise ValueError(f"target file does not contain JIS5 column: {jis5_col}")
    t = t.copy()
    t["muni_code"] = t[jis5_col].astype("string").str.extract(r"(\d{5})", expand=False)

    p = pd.read_csv(points, dtype={"muni_code": "string", "medical_zone_code": "string"})
    p["muni_code"] = p["muni_code"].astype("string").str.zfill(5)
    p = p.drop_duplicates(subset=["muni_code"])

    merged = t.merge(
        p[["muni_code", "latitude", "longitude", "medical_zone_code", "medical_zone_name", "prefecture"]],
        on="muni_code",
        how="left",
        validate="m:1",
    )

    bad = merged[
        merged["medical_zone_code"].isna() | merged["latitude"].isna() | merged["longitude"].isna()
    ].copy()
    unmatched.parent.mkdir(parents=True, exist_ok=True)
    bad.to_csv(unmatched, index=False, encoding="utf-8-sig")

    good = merged.drop(bad.index).copy()
    if good.empty:
        raise ValueError("No target rows could be mapped to JIS5 representative coordinates.")

    # Collapse to municipality counts first. This makes the weighting explicit and efficient.
    muni_counts = (
        good.groupby(
            ["medical_zone_code", "medical_zone_name", "prefecture", "muni_code", "latitude", "longitude"],
            dropna=False,
        )
        .size()
        .rename("target_n")
        .reset_index()
    )

    rows = []
    for (zone_code, zone_name, pref), g in muni_counts.groupby(
        ["medical_zone_code", "medical_zone_name", "prefecture"], dropna=False
    ):
        center_lat, center_lon = weighted_spherical_centroid(g["latitude"], g["longitude"], g["target_n"])
        d = haversine_km(g["latitude"].to_numpy(float), g["longitude"].to_numpy(float), center_lat, center_lon)
        w = g["target_n"].to_numpy(float)
        target_n = int(w.sum())
        rms = float(np.sqrt(np.average(d ** 2, weights=w)))
        mean = float(np.average(d, weights=w))
        max_d = float(np.max(d))
        rows.append(
            {
                "prefecture": pref,
                "medical_zone_code": zone_code,
                "medical_zone_name": zone_name,
                "target_n": target_n,
                "target_municipality_n": int(len(g)),
                "target_weighted_center_lat": center_lat,
                "target_weighted_center_lon": center_lon,
                "rms_dispersion_km": rms,
                "mean_distance_to_center_km": mean,
                "max_municipality_distance_km": max_d,
            }
        )

    out = pd.DataFrame(rows).sort_values(["prefecture", "medical_zone_code"]).reset_index(drop=True)
    # Diagnostic relative index only; final staffing coefficient should be calibrated separately.
    positive = out.loc[out["rms_dispersion_km"] > 0, "rms_dispersion_km"]
    benchmark = float(positive.median()) if len(positive) else np.nan
    out["rms_benchmark_median_km"] = benchmark
    out["relative_dispersion"] = out["rms_dispersion_km"] / benchmark if benchmark and not np.isnan(benchmark) else np.nan

    output.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output, index=False, encoding="utf-8-sig")

    mapped = len(good)
    coverage = mapped / len(merged) if len(merged) else 0.0
    print(f"targets={len(merged):,} mapped={mapped:,} unmatched={len(bad):,} coverage={coverage:.2%}")
    print(f"zones={len(out):,} benchmark_median_rms_km={benchmark:.3f}")
    print(f"output={output}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Calculate target-weighted spatial dispersion by secondary medical zone from a target facility CSV."
    )
    ap.add_argument("targets", type=Path, help="Target facility CSV")
    ap.add_argument("--jis5-col", default="jis5", help="JIS5 column name in target CSV (default: jis5)")
    ap.add_argument("--points", type=Path, default=DEFAULT_POINTS)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--unmatched-output", type=Path, default=DEFAULT_UNMATCHED)
    args = ap.parse_args()
    calculate(args.targets, args.jis5_col, args.points, args.output, args.unmatched_output)


if __name__ == "__main__":
    main()
