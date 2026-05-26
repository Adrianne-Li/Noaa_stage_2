#!/usr/bin/env python3
"""
Build the congressional district weather panel.

Joins NOAA county-level weather events to congressional districts via the
CD-County crosswalk (from the cd-county-matcher repo), and aggregates weather
into three time windows per election:

  - full_term : the 2 years preceding the election (full congressional term)
  - pre1y     : the 1 year preceding the election
  - pre6m     : the 6 months preceding the election

Output: one row per (state, cd_number, election_year, time_window) with the
aggregated weather statistics for that window, weighted by the fraction of
the CD that lies in each county.

STATUS
------
The time-window logic is implemented and tested. The election-data loading
and the final weighted aggregation are stubbed — they need the actual
Harvard Dataverse election file's column names to be wired up. Search for
"TODO" comments below.

USAGE
-----
  python scripts/04_build_congressional_panel.py \\
      --noaa-events outputs/noaa_storm_events_1984_2025_event_level.csv \\
      --crosswalk inputs/cd_county_crosswalk.csv \\
      --elections inputs/harvard_house_elections.csv \\
      --output outputs/congressional_weather_panel.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd


# ---------------------------------------------------------------------------
# Time-window definitions
# ---------------------------------------------------------------------------

# Each tuple: (window_name, months_before_election)
CONGRESSIONAL_WINDOWS = [
    ("full_term", 24),   # 2-year congressional term
    ("pre1y", 12),       # 1 year preceding election
    ("pre6m", 6),        # 6 months preceding election
]


def window_bounds(election_date: pd.Timestamp, months_before: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return (start, end) timestamps for the window of `months_before` months ending at the election."""
    end = election_date
    start = election_date - pd.DateOffset(months=months_before)
    return start, end


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_noaa_events(path: Path) -> pd.DataFrame:
    """Load the NOAA event_level CSV. Must have BEGIN_DATE_TIME and county_fips."""
    df = pd.read_csv(path, low_memory=False)

    # Parse the event begin date. NOAA's format is "DD-MMM-YY HH:MM:SS".
    date_col = None
    for c in ("BEGIN_DATE_TIME", "begin_date_time", "BEGIN_DATE"):
        if c in df.columns:
            date_col = c
            break
    if date_col is None:
        raise ValueError(
            f"NOAA events CSV missing a recognizable date column. "
            f"Got: {list(df.columns)[:10]}..."
        )
    df["event_date"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=["event_date"])
    df["county_fips"] = df["county_fips"].astype(str).str.zfill(5)
    return df


def load_crosswalk(path: Path) -> pd.DataFrame:
    """Load the CD-County crosswalk.

    Expected columns (from cd-county-matcher repo):
      year, state_name, cd_number, county_fips, pct_cd_in_county, pct_county_in_cd
    """
    df = pd.read_csv(path, low_memory=False)
    df["county_fips"] = df["county_fips"].astype(str).str.zfill(5)
    required = {"year", "cd_number", "county_fips", "pct_cd_in_county"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Crosswalk missing columns: {missing}")
    return df


def load_elections(path: Path) -> pd.DataFrame:
    """Load the Harvard Dataverse House elections file.

    TODO: Fill in once we have the actual file. Expected to produce a
    DataFrame with at minimum these columns:
      - state             (state name or abbreviation)
      - state_fips        (2-digit FIPS)
      - cd_number         (district number)
      - election_year     (int)
      - election_date     (datetime; first Tuesday after first Monday of November)

    For now, this function infers the election date as November of the
    election year if not provided. Adjust once the schema is known.
    """
    df = pd.read_csv(path, low_memory=False)

    # TODO: Rename Harvard's columns to the standard names above. The MIT
    # Election Lab "1976-2022 House" file uses, roughly:
    #   year, state, state_po, state_fen, state_cen, state_ic, office,
    #   district, stage, special, candidate, party, ...
    # The mapping below is a guess; verify against the actual file.
    rename_map = {
        "year": "election_year",
        "district": "cd_number",
        # leave state / state_po as-is
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    if "election_date" not in df.columns:
        # US federal elections: first Tuesday after the first Monday of November.
        # For aggregation purposes this is precise enough.
        df["election_date"] = df["election_year"].apply(_first_tuesday_after_first_monday_nov)

    df["election_date"] = pd.to_datetime(df["election_date"])
    return df


def _first_tuesday_after_first_monday_nov(year: int) -> pd.Timestamp:
    """Return the federal Election Day for `year`."""
    nov_1 = pd.Timestamp(year=int(year), month=11, day=1)
    # weekday: Monday=0 ... Sunday=6
    days_to_monday = (7 - nov_1.weekday()) % 7
    first_monday = nov_1 + pd.Timedelta(days=days_to_monday)
    return first_monday + pd.Timedelta(days=1)


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------


def aggregate_window(
    events: pd.DataFrame,
    crosswalk_year: pd.DataFrame,
    election_date: pd.Timestamp,
    months_before: int,
) -> pd.DataFrame:
    """Aggregate weather events into a single (cd, window) panel slice.

    For each CD in `crosswalk_year`:
      1. Find counties that overlap the CD
      2. Filter events to those counties + within the window
      3. Sum events, weighted by pct_cd_in_county
    Returns a DataFrame with one row per CD.
    """
    start, end = window_bounds(election_date, months_before)

    # Filter events to the time window
    window_events = events[(events["event_date"] >= start) & (events["event_date"] < end)].copy()

    if window_events.empty:
        # Still return a row per CD with zeros so the panel is balanced.
        result = crosswalk_year[["cd_number"]].drop_duplicates().copy()
        result["n_events"] = 0
        result["n_events_weighted"] = 0.0
        return result

    # Count events per county
    events_by_county = (
        window_events.groupby("county_fips").size().reset_index(name="n_events_county")
    )

    # Join to crosswalk and weight by CD-share of the county
    joined = crosswalk_year.merge(events_by_county, on="county_fips", how="left")
    joined["n_events_county"] = joined["n_events_county"].fillna(0)
    # The weight is pct_cd_in_county / 100 — the fraction of the CD's area
    # contained in that county. Summing weighted county totals gives an
    # area-share-weighted event count for the CD.
    joined["n_events_weighted_contribution"] = (
        joined["n_events_county"] * joined["pct_cd_in_county"] / 100.0
    )

    cd_agg = (
        joined.groupby("cd_number")
        .agg(
            n_events=("n_events_county", "sum"),  # unweighted total
            n_events_weighted=("n_events_weighted_contribution", "sum"),
        )
        .reset_index()
    )

    # TODO: Add similar aggregations for casualties (DEATHS_DIRECT,
    # INJURIES_DIRECT) and by EVENT_TYPE. The pattern is identical:
    # groupby county_fips, sum, merge with crosswalk, weight, sum by cd.

    return cd_agg


def build_panel(
    noaa_events: pd.DataFrame,
    crosswalk: pd.DataFrame,
    elections: pd.DataFrame,
) -> pd.DataFrame:
    """Build the full panel: one row per (election_year, cd, window)."""
    out_frames: List[pd.DataFrame] = []

    for election_year, year_elections in elections.groupby("election_year"):
        cw = crosswalk[crosswalk["year"] == election_year]
        if cw.empty:
            # Crosswalk doesn't have this year; fall back to the closest year.
            closest = crosswalk["year"].iloc[(crosswalk["year"] - election_year).abs().argsort()[:1]].iloc[0]
            print(f"  Warning: no crosswalk for {election_year}, using {closest}")
            cw = crosswalk[crosswalk["year"] == closest]

        election_date = year_elections["election_date"].iloc[0]

        for window_name, months_before in CONGRESSIONAL_WINDOWS:
            agg = aggregate_window(noaa_events, cw, election_date, months_before)
            agg["election_year"] = election_year
            agg["election_date"] = election_date
            agg["time_window"] = window_name
            out_frames.append(agg)

    if not out_frames:
        return pd.DataFrame()
    return pd.concat(out_frames, ignore_index=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build congressional district weather panel.")
    p.add_argument("--noaa-events", type=Path, required=True,
                   help="Path to noaa_storm_events_*_event_level.csv")
    p.add_argument("--crosswalk", type=Path, required=True,
                   help="Path to CD-County crosswalk CSV (from cd-county-matcher)")
    p.add_argument("--elections", type=Path, required=True,
                   help="Path to Harvard Dataverse House elections CSV")
    p.add_argument("--output", type=Path, required=True,
                   help="Path for the output panel CSV")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print(f"Loading NOAA events from {args.noaa_events}...")
    events = load_noaa_events(args.noaa_events)
    print(f"  {len(events):,} events")

    print(f"Loading crosswalk from {args.crosswalk}...")
    crosswalk = load_crosswalk(args.crosswalk)
    print(f"  {len(crosswalk):,} (CD, county, year) rows")

    print(f"Loading elections from {args.elections}...")
    elections = load_elections(args.elections)
    print(f"  {len(elections):,} elections")

    print("\nBuilding panel...")
    panel = build_panel(events, crosswalk, elections)
    print(f"  {len(panel):,} rows")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(args.output, index=False)
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
