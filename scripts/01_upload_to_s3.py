#!/usr/bin/env python3
"""
Upload NOAA scraper outputs to S3.

Uses boto3 with credentials from the standard AWS environment chain
(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION, or an IAM
role on EC2/Actions). The bucket must already exist; this script does NOT
auto-create it.

USAGE
-----
  python scripts/01_upload_to_s3.py --bucket my-bucket --output-dir outputs
  python scripts/01_upload_to_s3.py --bucket my-bucket --download-to ./downloaded

ENVIRONMENT
-----------
  S3_BUCKET (default: read from --bucket)
  S3_PREFIX (default: 'noaa-storm-events/')
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
except ImportError:
    print("boto3 is not installed. Install with: pip install boto3", file=sys.stderr)
    raise


DEFAULT_PREFIX = "noaa-storm-events/"
OUTPUT_FILE_SUFFIXES = ("_raw.csv", "_county_panel.csv", "_event_level.csv", "_year_stats.csv")


def discover_outputs(output_dir: Path) -> List[Path]:
    files: List[Path] = []
    for suffix in OUTPUT_FILE_SUFFIXES:
        matches = sorted(output_dir.glob(f"*{suffix}"))
        if matches:
            files.append(max(matches, key=lambda p: p.stat().st_mtime))
    return files


def upload_files(files: List[Path], bucket: str, prefix: str, s3_client) -> List[str]:
    uploaded: List[str] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for path in files:
        key = f"{prefix.rstrip('/')}/{path.name}"
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  Uploading {path.name} ({size_mb:.1f} MB) -> s3://{bucket}/{key}")
        try:
            s3_client.upload_file(
                Filename=str(path),
                Bucket=bucket,
                Key=key,
                ExtraArgs={
                    "ContentType": "text/csv",
                    "Metadata": {
                        "uploaded-at": timestamp,
                        "source": "noaa-storm-events-pipeline",
                    },
                },
            )
        except (BotoCoreError, ClientError) as exc:
            print(f"    ERROR uploading {path.name}: {exc}", file=sys.stderr)
            raise
        uploaded.append(f"s3://{bucket}/{key}")

    return uploaded


def download_latest(bucket: str, prefix: str, dest_dir: Path, s3_client) -> List[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    downloaded: List[Path] = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".csv"):
                continue
            local = dest_dir / Path(key).name
            print(f"  Downloading s3://{bucket}/{key} -> {local}")
            s3_client.download_file(Bucket=bucket, Key=key, Filename=str(local))
            downloaded.append(local)
    return downloaded


def get_existing_raw_key(bucket: str, prefix: str, s3_client) -> Optional[str]:
    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    candidates: List[dict] = []
    for page in pages:
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("_raw.csv"):
                candidates.append(obj)
    if not candidates:
        return None
    return max(candidates, key=lambda o: o["LastModified"])["Key"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Upload NOAA outputs to S3.")
    p.add_argument("--bucket", default=os.environ.get("S3_BUCKET"))
    p.add_argument("--prefix", default=os.environ.get("S3_PREFIX", DEFAULT_PREFIX))
    p.add_argument(
        "--output-dir", type=Path,
        default=Path(os.environ.get("OUTPUT_DIR", "./outputs")),
    )
    p.add_argument("--download-to", type=Path)
    p.add_argument("--print-existing-raw-key", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.bucket:
        print("--bucket is required (or set S3_BUCKET env var).", file=sys.stderr)
        return 2

    try:
        s3 = boto3.client("s3")
    except NoCredentialsError:
        print(
            "AWS credentials not found. Configure with `aws configure` or "
            "set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars.",
            file=sys.stderr,
        )
        return 1

    if args.print_existing_raw_key:
        key = get_existing_raw_key(args.bucket, args.prefix, s3)
        if key:
            print(key)
        return 0

    if args.download_to:
        print(f"Downloading from s3://{args.bucket}/{args.prefix}")
        files = download_latest(args.bucket, args.prefix, args.download_to, s3)
        print(f"\nDownloaded {len(files)} file(s) to {args.download_to}")
        return 0

    files = discover_outputs(args.output_dir)
    if not files:
        print(f"No NOAA output CSVs found in {args.output_dir}.", file=sys.stderr)
        return 1

    print(f"Found {len(files)} file(s) to upload:")
    for f in files:
        print(f"  - {f.name}")

    print(f"\nUploading to s3://{args.bucket}/{args.prefix}")
    uris = upload_files(files, args.bucket, args.prefix, s3)
    print(f"\nUploaded {len(uris)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
