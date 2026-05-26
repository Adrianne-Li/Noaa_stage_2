# NOAA Storm Events — Automated Scraper

Scrapes the [NOAA Storm Events Database](https://www.ncei.noaa.gov/stormevents/)
from 1984 onward, joins each event to its U.S. county via local shapefiles
(TIGER/Line 2010 for 2010+, Newberry historical for 1984–2009), and produces
four CSV outputs published to both **S3** and **OSF**. A single-year sample
lives in [`data_sample/`](./data_sample) for browsing on GitHub.

Two downstream panel builders join the weather data to elections:
- `04_build_congressional_panel.py` — CD-level panel for House races (2-year terms, three pre-election windows)
- `05_build_presidential_panel.py` — county-level panel for presidential races (4-year terms, four pre-election windows)

## Output files

For a year span (e.g. `1984_2025`):

| File | Description | Typical size |
|------|-------------|--------------|
| `noaa_storm_events_<span>_raw.csv` | Raw NOAA records, all columns + `data_coverage`. | ~600 MB |
| `noaa_storm_events_<span>_county_panel.csv` | One row per county-year, with event-type counts and casualty totals. | ~80 MB |
| `noaa_storm_events_<span>_event_level.csv` | Every event with `county_fips` joined. | ~700 MB |
| `noaa_storm_events_<span>_year_stats.csv` | One row per year, summary statistics. | ~10 KB |

### NOAA data coverage caveat

NOAA Storm Events was not comprehensive before 1996. Each output carries a
`data_coverage` column flagging this:

| Tier | Years | What's recorded |
|------|-------|-----------------|
| `tornado_only` | 1950–1954 | Only tornado events |
| `limited_3types` | 1955–1995 | Tornado, thunderstorm wind, hail only |
| `comprehensive` | 1996–present | All 48 event categories |

For robust comparisons across decades, filter on `data_coverage == 'comprehensive'`.

## Repository layout

```
.
├── .github/workflows/
│   └── scrape_noaa.yml             # monthly cron + manual trigger
├── scripts/
│   ├── setup_data.py               # fetches county shapefiles (run once)
│   ├── 00_scrape_noaa.py           # NOAA download + county join
│   ├── 01_upload_to_s3.py          # publish to S3
│   ├── 02_build_sample.py          # carve out one-year sample for the public repo
│   ├── 03_publish_to_osf.py        # publish to OSF
│   ├── 04_build_congressional_panel.py   # CD-level weather panel (stub)
│   └── 05_build_presidential_panel.py    # presidential county panel (stub)
├── data_sample/                    # one full recent year of all four outputs
├── data/                           # gitignored; populated by setup_data.py
├── outputs/                        # gitignored; populated by the scraper
├── requirements.txt
├── README.md
└── .gitignore
```

## Quickstart (local)

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Download county shapefiles (~1.5 GB extracted, runs once)
python scripts/setup_data.py

# 3. Scrape NOAA — full backfill from 1984
python scripts/00_scrape_noaa.py --start-year 1984 --end-year 2025

# 4. (Optional) Push to S3 and/or OSF
export S3_BUCKET=your-bucket
python scripts/01_upload_to_s3.py --bucket "$S3_BUCKET"

export OSF_TOKEN=your_osf_pat
export OSF_PROJECT=your_osf_guid
python scripts/03_publish_to_osf.py --project "$OSF_PROJECT"
```

## Automation

Runs at **06:00 UTC on the 2nd of every month** via GitHub Actions, plus
manual triggers from the *Actions* tab.

### Scrape modes

| Mode | What it does |
|---|---|
| `incremental` (default) | Pulls existing raw CSV from S3, re-scrapes current year, merges. |
| `current-year-only` | Re-scrapes current year, no merge. |
| `full-rebuild` | Re-scrapes every year from `start_year` to `end_year`. |

### Publishing destinations

After each scrape, the workflow uploads to:

1. **S3** — primary, always runs. Requires `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` secrets.
2. **OSF** — secondary, runs only when `OSF_TOKEN` and `OSF_PROJECT` secrets are set. Failures here do not fail the workflow.
3. **Workflow artifacts** — 90-day retention, always runs.

OSF is the long-term canonical home. S3 is the operational store the
incremental updates read from each month. As OSF integration stabilizes, S3
will be deprecated.

## One-time setup

### 1. AWS

Create an S3 bucket and an IAM user with `s3:ListBucket`, `s3:GetObject`,
`s3:PutObject` on it. Generate an access key. Add four GitHub repo secrets:
`S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`.

### 2. OSF

1. Create an OSF project at <https://osf.io>. Note its 5-character GUID (the
   last segment of the URL: e.g. for `https://osf.io/abc12/` the GUID is `abc12`).
2. Generate a Personal Access Token at <https://osf.io/settings/tokens/>.
   Grant it `osf.full_write` scope.
3. Add two GitHub repo secrets: `OSF_TOKEN` (the PAT) and `OSF_PROJECT` (the GUID).

### 3. GitHub repo settings

Settings → Actions → General → Workflow permissions → **Read and write
permissions** (so the workflow can commit `data_sample/` back).

### 4. First run

Actions → *Monthly NOAA Storm Events scrape* → Run workflow →
mode = `full-rebuild`, start_year = `1984`. Takes ~1–2 hours. After it
finishes, subsequent monthly cron runs default to `incremental`.

## Downstream panel builders

These join NOAA county-level weather to election outcomes from the Harvard
Dataverse. They currently exist as stubs with working time-window logic —
the election-data loading needs to be wired up to your specific Dataverse
files (see `TODO` comments in each script).

```bash
# Congressional panel (2-year terms; full_term, pre1y, pre6m windows)
python scripts/04_build_congressional_panel.py \
    --noaa-events outputs/noaa_storm_events_1984_2025_event_level.csv \
    --crosswalk inputs/cd_county_crosswalk.csv \
    --elections inputs/harvard_house_elections.csv \
    --output outputs/congressional_weather_panel.csv

# Presidential panel (4-year terms; full_term, pre2y, pre1y, pre6m windows)
python scripts/05_build_presidential_panel.py \
    --noaa-events outputs/noaa_storm_events_1984_2025_event_level.csv \
    --elections inputs/harvard_president_elections.csv \
    --output outputs/presidential_weather_panel.csv
```

The CD-County crosswalk is produced by the companion repo
[`cd-county-matcher`](https://github.com/Adrianne-Li/Climate-Project).

## Troubleshooting

**Newberry URL returns 404.** Shapefile sources at `digital.newberry.org`
have moved before. If `setup_data.py` fails, follow the fallback note it
prints — usually you download the file manually and drop it in
`data/newberry.zip`, then re-run.

**GeoPandas import errors.** GeoPandas needs native GDAL/GEOS/PROJ libraries.
On Ubuntu: `sudo apt-get install gdal-bin libgdal-dev`. On macOS:
`brew install gdal`. The GitHub Actions workflow installs these automatically.

**Out of memory on full rebuild.** The full 1984–present scrape uses ~4 GB
peak RAM during the final concat. GitHub Actions runners have 7 GB so this
is fine, but on a small laptop you may need to split the run into chunks.
