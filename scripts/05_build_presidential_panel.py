#!/usr/bin/env python3
"""
Build the presidential election weather panel (county-level).

Joins NOAA county-level weather events directly to county-year presidential
election results, and aggregates weather into four time windows per election:

  - full_term : the 4 years preceding the election (full presidential term)
  - pre2y     : the 2 years preceding the election (mid-term to election)
  - pre1y     : the 1 year preceding the election
  - pre6m     : the 6 months preceding the election

Output: one row per (county_fips, election_year, time_window) with aggregated
weather statistics.

STATUS
------
The time-window logic is implemented and tested. The election-data loading
and the final aggregation columns are stubbed — they need the actual Harvard
Dataverse election file's column names to be wired up. Search for "TODO"
comments below.

USAGE
-----
  python scripts/05_build_presidential_panel.py \\
      --noaa-events outputs/noaa_storm_events_1984_2025_event_level.csv \\
      --elections inputs/harvard_president_elections.csv \\
      --output outputs/presidential_weather_panel.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

import pandas as pd


PRESIDENTIAL_WINDOWS = [
    ("full_term", 48),   # 4-year term
    ("pre2y", 24),
    ("pre1y", 12),
    ("pre6m", 6),
]


def window_bounds(election_date: pd.Timestamp, months_before: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    end = election_date
    start = election_date - pd.DateOffset(months=months_before)
    return start, end


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_noaa_events(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
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


def load_elections(path: Path) -> pd.DataFrame:
    """Load Harvard Dataverse presidential elections file.

    TODO: Fill in once we have the actual file. Expected output columns:
      - county_fips       (5-digit FIPS, zero-padded)
      - election_year     (int)
      - election_date     (datetime)

    The MIT Election Lab "countypres" file has columns roughly:
      year, state, state_po, county_name, county_fips, office, candidate,
      party, candidatevotes, totalvotes, ...
    """
    df = pd.read_csv(path, low_memory=False)

    # TODO: confirm against actual schema
    if "year" in df.columns and "election_year" not in df.columns:
        df = df.rename(columns={"year": "election_year"})

    if "county_fips" in df.columns:
        df["county_fips"] = df["county_fips"].astype(str).str.zfill(5)

    if "election_date" not in df.columns:
        df["election_date"] = df["election_year"].apply(_first_tuesday_after_first_monday_nov)
    df["election_date"] = pd.to_datetime(df["election_date"])

    return df


def _first_tuesday_after_first_monday_nov(year: int) -> pd.Timestamp:
    nov_1 = pd.Timestamp(year=int(year), month=11, day=1)
    days_to_monday = (7 - nov_1.weekday()) % 7
    first_monday = nov_1 + pd.Timedelta(days=days_to_monday)
    return first_monday + pd.Timedelta(days=1)


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------


def aggregate_window(
    events: pd.DataFrame,
    counties: pd.Series,
    election_date: pd.Timestamp,
    months_before: int,
) -> pd.DataFrame:
    """Aggregate events into a county-level slice for one window."""
    start, end = window_bounds(election_date, months_before)
    window_events = events[(events["event_date"] >= start) & (events["event_date"] < end)]

    if window_events.empty:
        return pd.DataFrame({"county_fips": counties.unique(), "n_events": 0})

    agg = window_events.groupby("county_fips").size().reset_index(name="n_events")

    # TODO: Add aggregations for casualties (DEATHS_DIRECT, INJURIES_DIRECT)
    # and by EVENT_TYPE once the panel column requirements are confirmed
    # with Nich.

    # Ensure every county in `counties` appears, even with zero events
    all_counties = pd.DataFrame({"county_fips": counties.unique()})
    return all_counties.merge(agg, on="county_fips", how="left").fillna({"n_events": 0})


def build_panel(noaa_events: pd.DataFrame, elections: pd.DataFrame) -> pd.DataFrame:
    out_frames: List[pd.DataFrame] = []

    for election_year, year_elections in elections.groupby("election_year"):
        election_date = year_elections["election_date"].iloc[0]
        counties = year_elections["county_fips"]

        for window_name, months_before in PRESIDENTIAL_WINDOWS:
            agg = aggregate_window(noaa_events, counties, election_date, months_before)
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
    p = argparse.ArgumentParser(description="Build presidential county-level weather panel.")
    p.add_argument("--noaa-events", type=Path, required=True)
    p.add_argument("--elections", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print(f"Loading NOAA events from {args.noaa_events}...")
    events = load_noaa_events(args.noaa_events)
    print(f"  {len(events):,} events")

    print(f"Loading elections from {args.elections}...")
    elections = load_elections(args.elections)
    print(f"  {len(elections):,} county-election rows")

    print("\nBuilding panel...")
    panel = build_panel(events, elections)
    print(f"  {len(panel):,} rows")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    panel.to_csv(args.output, index=False)
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
