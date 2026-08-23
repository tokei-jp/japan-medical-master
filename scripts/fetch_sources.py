"""Download the two e-Stat source files used by build_geo_coefficient.py.

Usage:
    python scripts/fetch_sources.py

Downloads into data/raw/:
    - habitable_area.xlsx      (統計でみる市区町村のすがた 2026, 表B 自然環境 - 可住地面積)
    - medical_zone_mapping.xlsx (厚労省 病院報告 R5, 二次医療圏-市区町村対応表)

If a URL below returns HTML instead of an Excel file (e.g. e-Stat served an
interstitial/redirect page), open it in a browser once, find the real
"file-download" link for the current data version, and update SOURCES below
-- e-Stat occasionally reshuffles statInfId values between releases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

SOURCES = {
    "habitable_area.xlsx": (
        "https://www.e-stat.go.jp/stat-search/file-download"
        "?statInfId=000040463585&fileKind=0"
    ),
    "medical_zone_mapping.xlsx": (
        "https://www.e-stat.go.jp/stat-search/file-download"
        "?statInfId=000040224319&fileKind=4"
    ),
    # Fallback candidate if the above mapping file is stale/wrong shape:
    # 患者調査 二次医療圏-市区町村対応表 (statInfId=000031348710 前後).
    # Check manually on e-Stat and swap in the exact file-download URL.
}


def download(name: str, url: str) -> None:
    dest = RAW_DIR / name
    print(f"Fetching {name} ...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "html" in content_type.lower():
        print(
            f"  WARNING: {name} looks like an HTML page, not an Excel file "
            f"(Content-Type: {content_type}). e-Stat may require a session/"
            f"cookie or the statInfId has changed. Inspect manually.",
            file=sys.stderr,
        )
    dest.write_bytes(resp.content)
    print(f"  saved -> {dest} ({len(resp.content):,} bytes)")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        download(name, url)


if __name__ == "__main__":
    main()
