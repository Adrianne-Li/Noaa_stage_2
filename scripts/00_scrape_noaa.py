#!/usr/bin/env python3
"""
NOAA Storm Events scraper.

Downloads NOAA Storm Events bulk CSVs from NCEI, joins events to county
shapefiles (sourced locally by scripts/setup_data.py), and produces four CSV
outputs:

  - noaa_storm_events_<span>_raw.csv          : raw event records, all NOAA columns
  - noaa_storm_events_<span>_county_panel.csv : county-year aggregates
  - noaa_storm_events_<span>_event_level.csv  : every event with county_fips joined
  - noaa_storm_events_<span>_year_stats.csv   : per-year summary statistics

Each output includes a `data_coverage` column flagging the completeness of the
underlying NOAA record:
  - tornado_only     (1950-1954): only tornado events recorded
  - limited_3types   (1955-1995): tornado, thunderstorm wind, hail only
  - comprehensive    (1996-present): all 48 event categories

USAGE
-----
  # full historical backfill from 1984 onward (slow, ~1-2 hours)
  python scripts/00_scrape_noaa.py --start-year 1984 --end-year 2025

  # current calendar year only (used by the monthly workflow)
  python scripts/00_scrape_noaa.py --current-year-only

  # specific year
  python scripts/00_scrape_noaa.py --year 2024

  # incremental: refresh current year and merge into an existing raw file
  python scripts/00_scrape_noaa.py \\
      --incremental \\
      --existing-raw outputs/noaa_storm_events_1984_2025_raw.csv

ENVIRONMENT
-----------
  OUTPUT_DIR (default: ./outputs)   where CSVs are written
  DATA_DIR   (default: ./data)      where shapefiles live (set up by setup_data.py)
  CACHE_DIR  (default: ./.cache)    legacy; kept for compatibility
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import geopandas as gpd
import pandas as pd
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOAA_BULK_URL = "https://www.ncei.noaa.gov/pub/data/swdi/stormevents/csvfiles/"

DEFAULT_OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./outputs"))
DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DEFAULT_CACHE_DIR = Path(os.environ.get("CACHE_DIR", "./.cache"))

# Boundary year where we switch from Newberry (historical) to TIGER (modern).
# 2010 is when TIGER/Line started publishing yearly snapshots.
TIGER_CUTOVER_YEAR = 2010


def data_coverage_for_year(year: int) -> str:
    """Return the NOAA coverage tier for a given year."""
    if year < 1955:
        return "tornado_only"
    if year < 1996:
        return "limited_3types"
    return "comprehensive"


# ---------------------------------------------------------------------------
# Shapefile loading (from local files set up by setup_data.py)
# ---------------------------------------------------------------------------


def _find_shp(directory: Path) -> Path:
    """Find the first .shp file in `directory` (recursive)."""
    shps = list(directory.rglob("*.shp"))
    if not shps:
        raise FileNotFoundError(
            f"No .shp file found under {directory}. "
            f"Have you run `python scripts/setup_data.py`?"
        )
    return shps[0]


def load_counties_for_year(year: int, data_dir: Path) -> gpd.GeoDataFrame:
    """Load counties for `year` from local shapefiles.

    For year >= 2010: uses TIGER 2010 county snapshot.
    For year <  2010: uses Newberry historical county boundaries, filtered
      to that year's snapshot.

    Both paths produce a GeoDataFrame with standardized columns
    (county_fips, county_name, state_fips).
    """
    if year >= TIGER_CUTOVER_YEAR:
        tiger_dir = data_dir / "tiger_2010_county"
        if not tiger_dir.exists():
            raise FileNotFoundError(
                f"TIGER 2010 county shapefile not found at {tiger_dir}. "
                f"Run `python scripts/setup_data.py` first."
            )
        gdf = gpd.read_file(_find_shp(tiger_dir))
    else:
        newberry_dir = data_dir / "newberry_historical"
        if not newberry_dir.exists():
            raise FileNotFoundError(
                f"Newberry shapefile not found at {newberry_dir}. "
                f"Run `python scripts/setup_data.py` first "
                f"(or use --skip-newberry if you only need years >= 2010)."
            )
        gdf = gpd.read_file(_find_shp(newberry_dir))
        gdf = _filter_newberry_to_year(gdf, year)

    return _standardize_county_columns(gdf)


def _filter_newberry_to_year(gdf: gpd.GeoDataFrame, year: int) -> gpd.GeoDataFrame:
    """Newberry has per-day boundary records. Slice to the snapshot active on Jan 1 of `year`."""
    snapshot_date = pd.Timestamp(f"{year}-01-01")
    # Newberry's start/end date columns are typically START_DATE and END_DATE.
    # Be defensive about column casing.
    cols = {c.upper(): c for c in gdf.columns}
    start_col = cols.get("START_DATE")
    end_col = cols.get("END_DATE")
    if not start_col or not end_col:
        # If the schema differs, return everything; better than crashing.
        return gdf

    gdf = gdf.copy()
    gdf[start_col] = pd.to_datetime(gdf[start_col], errors="coerce")
    gdf[end_col] = pd.to_datetime(gdf[end_col], errors="coerce")
    mask = (gdf[start_col] <= snapshot_date) & (
        gdf[end_col].isna() | (gdf[end_col] >= snapshot_date)
    )
    return gdf.loc[mask].reset_index(drop=True)


def _standardize_county_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Normalize varied shapefile column names to county_fips / county_name / state_fips."""
    gdf = gdf.copy()
    mappings = {
        "GEOID": "county_fips",
        "GEOID20": "county_fips",
        "GEOID10": "county_fips",
        "FIPS": "county_fips",
        "FIPS_CODE": "county_fips",
        "COUNTYFP": "county_fp",
        "COUNTYFP10": "county_fp",
        "NAME": "county_name",
        "NAMELSAD": "county_name",
        "NAME10": "county_name",
        "STATEFP": "state_fips",
        "STATEFP20": "state_fips",
        "STATEFP10": "state_fips",
        "STATE_FIPS": "state_fips",
    }
    for old, new in mappings.items():
        if old in gdf.columns and new not in gdf.columns:
            gdf = gdf.rename(columns={old: new})

    if (
        "county_fips" not in gdf.columns
        and "state_fips" in gdf.columns
        and "county_fp" in gdf.columns
    ):
        gdf["county_fips"] = (
            gdf["state_fips"].astype(str)
            + gdf["county_fp"].astype(str).str.zfill(3)
        )

    if "state_fips" not in gdf.columns and "county_fips" in gdf.columns:
        gdf["state_fips"] = gdf["county_fips"].astype(str).str[:2]

    # Newberry sometimes uses "NAME" for county name and stores FIPS in
    # different columns; if we still don't have county_fips, bail loudly so
    # the user knows.
    if "county_fips" not in gdf.columns:
        raise ValueError(
            f"Could not derive county_fips from shapefile columns: {list(gdf.columns)}"
        )
    if "county_name" not in gdf.columns:
        gdf["county_name"] = None

    gdf["county_fips"] = gdf["county_fips"].astype(str)
    return gdf


# ---------------------------------------------------------------------------
# NOAA file discovery + download
# ---------------------------------------------------------------------------


def discover_noaa_files(year: int) -> Dict[str, str]:
    """Parse the NOAA directory listing to find the latest filenames for `year`."""
    resp = requests.get(NOAA_BULK_URL, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    files: Dict[str, str] = {}
    for link in soup.find_all("a"):
        href = link.get("href", "")
        if f"d{year}_" not in href or not href.endswith(".csv.gz"):
            continue
        if "details" in href:
            files["details"] = href
        elif "fatalities" in href:
            files["fatalities"] = href
        elif "locations" in href:
            files["locations"] = href
    return files


def fetch_storm_events(year: int, output_dir: Path) -> pd.DataFrame:
    """Download the 'details' bulk CSV for `year` and return as a DataFrame."""
    files = discover_noaa_files(year)
    if "details" not in files:
        print(f"  No 'details' file found for {year}")
        return pd.DataFrame()

    url = NOAA_BULK_URL + files["details"]
    print(f"  Downloading {files['details']}...", end=" ", flush=True)

    resp = requests.get(url, timeout=300, stream=True)
    resp.raise_for_status()

    tmp = output_dir / f".tmp_details_{year}.csv.gz"
    with open(tmp, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                fh.write(chunk)

    df = pd.read_csv(tmp, compression="gzip", encoding="latin1", low_memory=False)
    tmp.unlink()
    df["year"] = year
    df["data_coverage"] = data_coverage_for_year(year)
    print(f"{len(df):,} records ({data_coverage_for_year(year)})")
    return df


# ---------------------------------------------------------------------------
# Event <-> county join
# ---------------------------------------------------------------------------


def match_events_to_counties(
    events_df: pd.DataFrame, county_gdf: gpd.GeoDataFrame, year: int
) -> Dict[str, pd.DataFrame]:
    """Build the three matched outputs for a single year."""
    coverage = data_coverage_for_year(year)

    county_panel = county_gdf[["county_fips", "county_name", "state_fips"]].copy()
    county_panel["year"] = year
    county_panel["data_coverage"] = coverage

    if events_df.empty:
        county_panel["event_count"] = 0
        return {
            "county_panel": county_panel,
            "event_level": pd.DataFrame(),
            "year_stats": pd.DataFrame(
                [
                    {
                        "year": year,
                        "data_coverage": coverage,
                        "n_events": 0,
                        "n_counties_affected": 0,
                    }
                ]
            ),
        }

    events_df = events_df.copy()
    if "STATE_FIPS" in events_df.columns and "CZ_FIPS" in events_df.columns:
        state = (
            events_df["STATE_FIPS"]
            .astype(str)
            .str.split(".")
            .str[0]
            .str.zfill(2)
        )
        cz = (
            events_df["CZ_FIPS"]
            .astype(str)
            .str.split(".")
            .str[0]
            .str.zfill(3)
        )
        events_df["county_fips"] = state + cz
    else:
        events_df["county_fips"] = pd.NA

    events_df["county_fips"] = events_df["county_fips"].astype(str)
    county_panel["county_fips"] = county_panel["county_fips"].astype(str)

    event_counts = (
        events_df.groupby("county_fips").size().reset_index(name="event_count")
    )

    event_type_counts = (
        events_df.groupby(["county_fips", "EVENT_TYPE"])
        .size()
        .reset_index(name="count")
    )
    event_type_pivot = (
        event_type_counts.pivot(
            index="county_fips", columns="EVENT_TYPE", values="count"
        )
        .fillna(0)
        .reset_index()
    )
    event_type_pivot.columns = [
        c if c == "county_fips"
        else f"events_{str(c).lower().replace(' ', '_').replace('/', '_')}"
        for c in event_type_pivot.columns
    ]

    casualty_cols = [
        "INJURIES_DIRECT",
        "INJURIES_INDIRECT",
        "DEATHS_DIRECT",
        "DEATHS_INDIRECT",
    ]
    for c in casualty_cols:
        if c not in events_df.columns:
            events_df[c] = 0
    damage_stats = (
        events_df.groupby("county_fips")[casualty_cols].sum().reset_index()
    )
    damage_stats.columns = [
        "county_fips",
        "total_injuries_direct",
        "total_injuries_indirect",
        "total_deaths_direct",
        "total_deaths_indirect",
    ]

    county_events = event_counts.merge(event_type_pivot, on="county_fips", how="left")
    county_events = county_events.merge(damage_stats, on="county_fips", how="left")

    panel = county_panel.merge(county_events, on="county_fips", how="left")
    panel["event_count"] = panel["event_count"].fillna(0).astype(int)
    for c in panel.columns:
        if c.startswith("events_") or c.startswith("total_"):
            panel[c] = panel[c].fillna(0).astype(int)

    county_lookup = county_gdf[["county_fips", "county_name"]].drop_duplicates()
    event_level = events_df.merge(
        county_lookup, on="county_fips", how="left", suffixes=("", "_county")
    )

    year_stats = pd.DataFrame(
        [
            {
                "year": year,
                "data_coverage": coverage,
                "n_events": len(events_df),
                "n_counties_total": len(county_panel),
                "n_counties_affected": int(panel["event_count"].gt(0).sum()),
                "pct_counties_affected": round(
                    100 * panel["event_count"].gt(0).sum() / len(county_panel), 2
                )
                if len(county_panel)
                else 0.0,
                "total_deaths": int(
                    panel["total_deaths_direct"].sum()
                    + panel["total_deaths_indirect"].sum()
                ),
                "total_injuries": int(
                    panel["total_injuries_direct"].sum()
                    + panel["total_injuries_indirect"].sum()
                ),
            }
        ]
    )

    return {
        "county_panel": panel,
        "event_level": event_level,
        "year_stats": year_stats,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    years: List[int],
    output_dir: Path,
    data_dir: Path,
    existing_raw: Optional[Path] = None,
) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Scraping NOAA Storm Events for years: {years[0]}-{years[-1]} ({len(years)} years)")
    print(f"Output dir: {output_dir.resolve()}")
    print(f"Data dir:   {data_dir.resolve()}")
    print("=" * 70)

    raw_frames: List[pd.DataFrame] = []
    panel_frames: List[pd.DataFrame] = []
    event_frames: List[pd.DataFrame] = []
    year_stat_frames: List[pd.DataFrame] = []

    for year in years:
        print(f"\nProcessing {year}...")
        events = fetch_storm_events(year, output_dir)
        if events.empty:
            print(f"  No events for {year}; skipping.")
            continue

        try:
            counties = load_counties_for_year(year, data_dir)
        except (FileNotFoundError, ValueError) as exc:
            print(f"  Failed to load counties for {year}: {exc}")
            continue
        print(f"  Loaded {len(counties):,} counties")

        matched = match_events_to_counties(events, counties, year)
        raw_frames.append(events)
        panel_frames.append(matched["county_panel"])
        if not matched["event_level"].empty:
            event_frames.append(matched["event_level"])
        year_stat_frames.append(matched["year_stats"])

        time.sleep(1)  # be polite to NOAA

    if not raw_frames:
        print("No data scraped. Exiting.")
        return {}

    new_raw = pd.concat(raw_frames, ignore_index=True)

    if existing_raw is not None and existing_raw.exists():
        print(f"\nMerging with existing raw file: {existing_raw}")
        old = pd.read_csv(existing_raw, low_memory=False)
        if "year" in old.columns:
            old = old[~old["year"].isin(years)]
        # Ensure data_coverage exists on the old data too
        if "data_coverage" not in old.columns and "year" in old.columns:
            old["data_coverage"] = old["year"].astype(int).map(data_coverage_for_year)
        combined_raw = pd.concat([old, new_raw], ignore_index=True)
    else:
        combined_raw = new_raw

    span = f"{int(combined_raw['year'].min())}_{int(combined_raw['year'].max())}"
    paths = {
        "raw": output_dir / f"noaa_storm_events_{span}_raw.csv",
        "county_panel": output_dir / f"noaa_storm_events_{span}_county_panel.csv",
        "event_level": output_dir / f"noaa_storm_events_{span}_event_level.csv",
        "year_stats": output_dir / f"noaa_storm_events_{span}_year_stats.csv",
    }

    print("\n" + "=" * 70)
    print("Saving outputs...")
    combined_raw.to_csv(paths["raw"], index=False)
    print(f"  raw:          {paths['raw']} ({len(combined_raw):,} rows)")

    pd.concat(panel_frames, ignore_index=True).to_csv(
        paths["county_panel"], index=False
    )
    print(f"  county panel: {paths['county_panel']}")

    if event_frames:
        pd.concat(event_frames, ignore_index=True).to_csv(
            paths["event_level"], index=False
        )
        print(f"  event-level:  {paths['event_level']}")

    pd.concat(year_stat_frames, ignore_index=True).to_csv(
        paths["year_stats"], index=False
    )
    print(f"  year stats:   {paths['year_stats']}")

    print("\nDone.")
    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape NOAA Storm Events.")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--year", type=int, help="Single year to scrape.")
    grp.add_argument(
        "--current-year-only",
        action="store_true",
        help="Scrape only the current calendar year.",
    )
    grp.add_argument(
        "--incremental",
        action="store_true",
        help="Refresh current year and merge with an existing raw CSV.",
    )

    p.add_argument("--start-year", type=int, default=1984)
    p.add_argument("--end-year", type=int, default=datetime.now().year)
    p.add_argument(
        "--existing-raw",
        type=Path,
        help="Path to existing raw CSV to merge with (incremental mode).",
    )
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    current_year = datetime.now().year

    if args.year:
        years = [args.year]
    elif args.current_year_only or args.incremental:
        years = [current_year]
    else:
        years = list(range(args.start_year, args.end_year + 1))

    existing_raw = args.existing_raw if args.incremental else None
    if args.incremental and not existing_raw:
        print("--incremental requires --existing-raw", file=sys.stderr)
        return 2

    run(years, args.output_dir, args.data_dir, existing_raw=existing_raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
