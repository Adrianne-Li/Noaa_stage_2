#!/usr/bin/env python3
"""
Upload NOAA scraper outputs to an OSF project.

Uses the osfclient Python library, which wraps the OSF v2 API. Authenticates
via an OSF Personal Access Token (PAT) supplied as the OSF_TOKEN environment
variable. The target project is identified by its OSF GUID (the 5-character
ID in the URL, e.g. for https://osf.io/abc12/ the GUID is `abc12`).

USAGE
-----
  # upload the four CSVs from ./outputs/ to OSF project abc12, under path noaa/
  export OSF_TOKEN=your_token_here
  python scripts/03_publish_to_osf.py --project abc12 --remote-path noaa/

ENVIRONMENT
-----------
  OSF_TOKEN     OSF personal access token (required)
  OSF_PROJECT   OSF project GUID (alternative to --project)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List

try:
    from osfclient import OSF
except ImportError:
    print(
        "osfclient is not installed. Install with: pip install osfclient",
        file=sys.stderr,
    )
    raise


OUTPUT_FILE_SUFFIXES = (
    "_raw.csv",
    "_county_panel.csv",
    "_event_level.csv",
    "_year_stats.csv",
)


def discover_outputs(output_dir: Path) -> List[Path]:
    """Find the four NOAA CSV outputs in `output_dir`."""
    files: List[Path] = []
    for suffix in OUTPUT_FILE_SUFFIXES:
        matches = sorted(output_dir.glob(f"*{suffix}"))
        if matches:
            files.append(max(matches, key=lambda p: p.stat().st_mtime))
    return files


def upload_to_osf(
    files: List[Path],
    project_guid: str,
    remote_path: str,
    token: str,
) -> None:
    """Upload `files` to s3://osf.io/<project_guid>/<remote_path>/<filename>."""
    osf = OSF(token=token)
    project = osf.project(project_guid)
    storage = project.storage("osfstorage")

    remote_prefix = remote_path.strip("/")

    for path in files:
        remote_name = f"{remote_prefix}/{path.name}" if remote_prefix else path.name
        size_mb = path.stat().st_size / (1024 * 1024)
        print(
            f"  Uploading {path.name} ({size_mb:.1f} MB) "
            f"-> osf://{project_guid}/{remote_name}"
        )
        with open(path, "rb") as fh:
            # osfclient's upload silently no-ops if the file exists with the
            # same content; pass force=True to always overwrite.
            storage.create_file(remote_name, fh, force=True, update=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Upload NOAA outputs to OSF.")
    p.add_argument(
        "--project",
        default=os.environ.get("OSF_PROJECT"),
        help="OSF project GUID (or set OSF_PROJECT env var).",
    )
    p.add_argument(
        "--remote-path",
        default="noaa-storm-events",
        help="Remote path/folder inside the project (default: noaa-storm-events).",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("OUTPUT_DIR", "./outputs")),
        help="Local directory containing the CSVs to upload.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("OSF_TOKEN")
    if not token:
        print("OSF_TOKEN environment variable is not set.", file=sys.stderr)
        return 2
    if not args.project:
        print("--project is required (or set OSF_PROJECT env var).", file=sys.stderr)
        return 2

    files = discover_outputs(args.output_dir)
    if not files:
        print(
            f"No NOAA output CSVs found in {args.output_dir}. Did the scraper run?",
            file=sys.stderr,
        )
        return 1

    print(f"Found {len(files)} file(s) to upload to OSF project {args.project}:")
    for f in files:
        print(f"  - {f.name}")

    upload_to_osf(files, args.project, args.remote_path, token)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
