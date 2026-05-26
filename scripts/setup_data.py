#!/usr/bin/env python3
"""
Fetch county shapefiles used by the NOAA scraper.

Downloads two sources:

  1. TIGER/Line 2010 county shapefile (~75 MB) for 2010+ event matching.
  2. Newberry Atlas of Historical County Boundaries (~500 MB tar.gz, ~1.5 GB
     extracted) for 1984-2009 event matching. Newberry has per-day county
     boundaries; we slice it to one snapshot per year.

Idempotent: skips downloads if files already exist locally. Re-run to refresh.

USAGE
-----
  python scripts/setup_data.py
  python scripts/setup_data.py --data-dir ./data
  python scripts/setup_data.py --skip-newberry    # if you only need 2010+
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import List

import requests


TIGER_2010_URL = (
    "https://www2.census.gov/geo/tiger/TIGER2010/COUNTY/2010/tl_2010_us_county10.zip"
)

# Newberry Atlas of Historical County Boundaries.
# The canonical source is at publications.newberry.org. URLs there have
# historically moved; if this 404s, see the troubleshooting note in the README.
NEWBERRY_URL = (
    "https://digital.newberry.org/ahcb/downloads/gis/US_HistCounties_Gen001.zip"
)

DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))


def download_with_progress(url: str, dest: Path, label: str) -> None:
    """Stream a download to disk with simple progress output."""
    print(f"  Downloading {label} from {url}")
    resp = requests.get(url, timeout=600, stream=True)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            fh.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = 100 * downloaded / total
                mb = downloaded / (1024 * 1024)
                print(f"    {mb:.0f} MB ({pct:.0f}%)", end="\r", flush=True)
    print(f"    -> {dest} ({downloaded / (1024 * 1024):.0f} MB)         ")


def fetch_tiger_2010(data_dir: Path) -> Path:
    """Download and extract TIGER/Line 2010 county shapefile."""
    extracted = data_dir / "tiger_2010_county"
    if (extracted / "tl_2010_us_county10.shp").exists():
        print(f"  TIGER 2010 already present at {extracted}; skipping.")
        return extracted

    zip_path = data_dir / "tl_2010_us_county10.zip"
    download_with_progress(TIGER_2010_URL, zip_path, "TIGER 2010 counties")

    print(f"  Extracting to {extracted}")
    extracted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extracted)
    zip_path.unlink()
    return extracted


def fetch_newberry(data_dir: Path) -> Path:
    """Download and extract Newberry historical county boundaries."""
    extracted = data_dir / "newberry_historical"
    # The archive contains a top-level directory; we check for any .shp inside.
    if extracted.exists() and any(extracted.rglob("*.shp")):
        print(f"  Newberry already present at {extracted}; skipping.")
        return extracted

    archive_path = data_dir / "newberry.zip"
    try:
        download_with_progress(NEWBERRY_URL, archive_path, "Newberry historical counties")
    except requests.HTTPError as exc:
        print(f"  ERROR: {exc}")
        print(
            "\n  The Newberry URL may have moved. To work around this:\n"
            "    1. Visit https://digital.newberry.org/ahcb/pages/United_States.html\n"
            "    2. Download the 'Generalized Shapefile' (US_HistCounties_Gen001.zip).\n"
            f"    3. Place it at {archive_path}\n"
            "    4. Re-run this script.\n"
        )
        raise

    print(f"  Extracting to {extracted}")
    extracted.mkdir(parents=True, exist_ok=True)
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extracted)
    else:
        with tarfile.open(archive_path) as tf:
            tf.extractall(extracted)
    archive_path.unlink()
    return extracted


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch county shapefiles.")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument(
        "--skip-newberry",
        action="store_true",
        help="Skip the large Newberry download (use only if you don't need pre-2010).",
    )
    p.add_argument(
        "--skip-tiger",
        action="store_true",
        help="Skip the TIGER 2010 download.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    print(f"Data directory: {args.data_dir.resolve()}")

    if not args.skip_tiger:
        print("\n[1/2] TIGER 2010 county shapefile")
        fetch_tiger_2010(args.data_dir)

    if not args.skip_newberry:
        print("\n[2/2] Newberry historical county boundaries")
        try:
            fetch_newberry(args.data_dir)
        except requests.HTTPError:
            return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
