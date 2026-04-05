# Newspapers.com Scraper

Headful, real-Google-Chrome scraping pipeline for Newspapers.com.

This repo is built around the operational constraints we already learned the
hard way:

- use a real Chrome session, not Playwright Chromium
- stay headful at all times
- rely on the live `/papers/` UI only for family discovery
- use the lighter browse API for exact-issue confirmation and issue page
  inventory
- use the signed image URL exposed by the live `/image/<id>/` page to download
  original JPEG page scans
- stop on Cloudflare or rate limiting rather than trying to power through

## Operational model

This repository is the intended source of truth for scraper logic and AWS
worker orchestration.

- edit code locally in this repo
- commit locally
- push to GitHub
- then deploy that committed code to AWS workers

EC2 hosts should be treated as runtime environments with persistent browser
profiles and run state, not as the primary place where source code changes are
made. For the current AWS operating notes, see
[`docs/aws.md`](docs/aws.md).

## Documentation map

Start here if someone new needs to take over operations:

- [`docs/aws.md`](docs/aws.md)
  - AWS overview and document map
- `docs/private/aws_account_reference.local.md`
  - local-only exact live-account values, current fleet inventory, and other takeover notes that are intentionally gitignored
- [`docs/aws_account_reference.md`](docs/aws_account_reference.md)
  - the safe tracked version: what values exist, how to discover them, and what belongs only in the local private reference
- [`docs/aws_launch_runbook.md`](docs/aws_launch_runbook.md)
  - end-to-end launch procedure for a new EC2 worker using values sourced from the local private reference
- [`docs/aws_operations_runbook.md`](docs/aws_operations_runbook.md)
  - day-2 operations, access, health checks, recovery, watchers, and safe updates
- [`docs/aws_storage_model.md`](docs/aws_storage_model.md)
  - canonical PNG archive, inventory, and worker retirement model
- [`scripts/aws/README.md`](scripts/aws/README.md)
  - script catalog and common operational recipes

For screenshot artifacts, the intended long-term model is:

- worker-local `results/...` paths are runtime storage
- the canonical viewer PNG archive lives in S3 under
  `archive/viewer_png/by_image_id/...`
- authoritative inventory/provenance manifests live under
  `archive/inventory/...` and `archive/snapshots/...`

The helper that backfills that canonical archive is:

```bash
python3 scripts/aws/archive_viewer_pngs.py \
  --bucket "$FLEET_BUCKET" \
  --source-prefix results/ \
  --output-dir output/aws_viewer_png_archive_<date>
```

## Public release workflow

If this private operations repo needs to produce a public-safe GitHub repo,
export the current clean `HEAD` into a fresh sibling repository instead of
making this repo public in place.

The exporter only includes tracked files from the current commit, so ignored
runtime state and local private notes stay out automatically. If the local-only
file `docs/private/public_export_forbidden_patterns.local.txt` exists, the
exporter also scans the generated tree for forbidden account-specific patterns
before it initializes the public repo.

```bash
python3 scripts/release_public_repo.py \
  --output-dir ../newspaper-scrapping \
  --force
```

The exported sibling repo can then be pushed to a new public GitHub repository
without exposing the private operational history of this repo.

## Workflow

1. Launch a dedicated real Chrome session with a persistent profile.
2. Sign into Newspapers.com once and reuse that profile.
3. Discover paper families through `/papers/`.
4. Confirm exact issue dates through the browse API.
5. Enumerate issue page inventories.
6. Download original page JPEGs through signed `img.newspapers.com` URLs at a
   conservative rate.

## Quick start

1. Install:

   ```bash
   poetry install
   ```

2. Review config:

   ```bash
   cp .env.example .env
   ```

   This repo also reads `.env.local`. Keep credentials in a local gitignored
   env file rather than in tracked config.

3. Launch real Chrome:

   ```bash
   poetry run newspaper-scrapper chrome-launch
   ```

4. Open the login page and prefill credentials:

   ```bash
   poetry run newspaper-scrapper auth-login --fill --wait-seconds 180
   ```

   If Cloudflare or Turnstile requires manual interaction, complete it in the
   browser. The command polls until the session looks signed in or times out.

5. Confirm status:

   ```bash
   poetry run newspaper-scrapper auth-status
   ```

6. Export the authenticated cookie bundle if you want to bootstrap another
   persistent Chrome session, including a torch session:

   ```bash
   poetry run newspaper-scrapper auth-export-cookies \
     --output-path output/auth/newscom_cookies.json
   ```

7. Discover issues from a seed CSV:

   ```bash
   poetry run newspaper-scrapper discover-issues \
     --base-csv /path/to/base_issue_seed.csv \
     --confirmed-csv /path/to/already_confirmed.csv \
     --output-dir output/discovery_batch1
   ```

8. Catalog issue pages for confirmed exact issues:

   ```bash
   poetry run newspaper-scrapper catalog-issue-pages \
     --confirmed-csv /path/to/confirmed_exact_issues.csv \
     --output-dir output/catalog_batch1
   ```

9. Download page images from a preferred image manifest:

   ```bash
   poetry run newspaper-scrapper download-pages \
     --manifest-csv /path/to/target_page_image_manifest_preferred_only.csv \
     --output-dir output/download_batch1 \
     --sleep-between-pages 60
   ```

10. Search page contents by keyword and build a downloader-ready page manifest:

   ```bash
   poetry run newspaper-scrapper search-content \
     --keyword zoning \
     --output-dir output/search_zoning \
     --max-pages 50 \
     --count-per-request 100 \
     --sleep-between-requests 2
   ```

   This writes:
   - `results.csv`: one row per search API record
   - `page_manifest.csv`: deduped page-level manifest that can be fed directly to
     `download-pages`
   - `summary.json`: checkpoint state including the last `nextStart` cursor for
     resume

11. Plan a large keyword seed crawl across multiple workers by sharding the
    query into independent date ranges:

   ```bash
   poetry run newspaper-scrapper plan-search-workers \
     --keyword zoning \
     --output-dir output/zoning_search_workers \
     --workers 10 \
     --start-year 1800 \
     --end-year 2026 \
     --max-pages 10000 \
     --count-per-request 100 \
     --sleep-between-requests 1
   ```

   This writes:
   - `worker_plan.csv`: one date-range shard per worker
   - `launch_workers.sh`: runnable shell launcher with isolated Chrome ports and profiles
   - `summary.json`: top-level plan metadata

   For very large seed crawls, the healthy production pattern is:
   - use explicit `--date-start` / `--date-end` slices
   - keep search workers unauthenticated
   - always use `--navigate-search-results` before paging the search API
   - prefer yearly slices through `1914`
   - prefer monthly slices from `1915` onward

12. Download the unique page hits from that keyword search:

   ```bash
   poetry run newspaper-scrapper download-pages \
     --manifest-csv output/search_zoning/page_manifest.csv \
     --output-dir output/search_zoning_downloads \
     --sleep-between-pages 60
   ```

13. Merge completed worker seed outputs into deduped page and issue manifests:

   ```bash
   poetry run newspaper-scrapper merge-search-workers \
     --workers-root output/zoning_search_workers/workers \
     --output-dir output/zoning_search_workers/merged
   ```

   This writes:
   - `results_merged.csv`
   - `page_manifest_merged.csv`
   - `issue_manifest_merged.csv`

14. Split a large manifest into conservative worker shards:

   ```bash
   poetry run newspaper-scrapper shard-manifest \
     --manifest-csv output/search_zoning/page_manifest.csv \
     --output-dir output/search_zoning_shards \
     --num-shards 2 \
     --strategy by_issue
   ```

   This keeps each issue together by default, which is safer for low-rate
   multi-session downloading.

15. Capture a browser-rendered full-page PNG as a first-class artifact when
   the live `/image/<id>/` page is viewable and you want a browser-native
   page image:

   ```bash
   poetry run newspaper-scrapper capture-viewer-screenshot \
     --image-page-url https://www.newspapers.com/image/22175081/ \
     --output-dir output/viewer_screenshot_probe
   ```

16. Run browser-rendered screenshot capture over a manifest the same way you
   would run `download-pages`. For large batches, prefer the isolated
   synthetic tile-canvas strategy:

   ```bash
   poetry run newspaper-scrapper screenshot-pages \
     --manifest-csv output/search_zoning/page_manifest.csv \
     --output-dir output/search_zoning_screenshots \
     --sleep-between-pages 0 \
     --strategy synthetic_tiles \
     --continue-on-error
   ```

15. For a production-style single-worker screenshot run, use the multi-pass
   orchestrator. It writes pass-specific outputs, a merged final results CSV,
   and a remaining-failures manifest:

   ```bash
   poetry run newspaper-scrapper screenshot-pages-production \
     --manifest-csv output/search_zoning/page_manifest.csv \
     --output-dir output/search_zoning_screenshots_prod \
     --strategy synthetic_tiles \
     --max-passes 3 \
     --stop-on-stall
   ```

16. Check the current torch environment before attempting an HPC run:

   ```bash
   poetry run newspaper-scrapper torch-check
   ```

   See also [docs/torch.md](docs/torch.md) and the helper scripts under
   `scripts/torch/`.

## CLI surface

- `chrome-launch`
- `auth-store`
- `auth-login`
- `auth-status`
- `auth-export-cookies`
- `auth-import-cookies`
- `papers-search`
- `search-content`
- `plan-search-workers`
- `merge-search-workers`
- `discover-issues`
- `catalog-issue-pages`
- `probe-image`
- `capture-viewer-screenshot`
- `screenshot-pages`
- `screenshot-pages-production`
- `download-pages`
- `download-issue`
- `shard-manifest`
- `torch-check`

## Notes

- The scraper is intentionally conservative. When it sees Cloudflare, auth
  loss, or a `429`, it stops and checkpoints.
- This pipeline is designed to scale through resumable manifests and low-rate
  real-browser execution, not through parallel headless fetches.
- `search-content` uses the `/api/search/query` backend rather than scraping
  rendered result cards, so it can page deterministically with `nextStart`
  cursors and resume from checkpoints.
- For large seed-harvest runs, the healthiest mode is currently
  unauthenticated browser search with a real search-results navigation before
  API pagination, especially on modern dense date slices.
- Browser-rendered screenshots are a first-class capture mode alongside direct
  signed-JPEG downloads, not just a debugging tool.
- `screenshot-pages` and `capture-viewer-screenshot` support explicit strategy
  selection: `auto`, `synthetic_full_image`, `synthetic_tiles`, and
  `viewer_upgraded`.
- `capture-viewer-screenshot` now isolates each capture in a fresh Chrome debug
  tab, extracts authoritative page metadata from the live image page, and can
  render synthetic newspaper pages without depending on the native
  Newspapers.com viewer state.
- The screenshot validator now rejects blank, washed-out, vertically
  inconsistent, and laterally inconsistent renders before accepting a page.
- In live validation on known pages, the synthetic full-image screenshot path
  matched the direct signed JPEG on representative pages, but the full-image
  endpoint can still return `429` under load.
- The current single-worker production recommendation is therefore
  `synthetic_tiles`, which rasterizes signed tiles onto a synthetic canvas and
  avoids the large full-image request path.
- In a fresh-browser 10-page keyword-hit validation run,
  `screenshot-pages-production` completed `10/10` captures in one pass at about
  `7.3` pages per minute:
  [summary.json](output/production_validation_10_20260328/run/summary.json)
- Screenshot batches now support `--continue-on-error`, fresh per-page reruns,
  and longer second-pass waits so transient render failures can be recovered
  without aborting the whole run.
- `screenshot-pages-production` is the recommended single-worker operating mode.
  It runs the manifest in passes, reruns failure-only subsets with escalated
  waits, restarts the dedicated Chrome worker browser between passes, writes a
  merged `final_results.csv`, and emits a
  `remaining_failures_manifest.csv` for any pages that still need manual
  attention.
- The signed original-JPEG downloader is still the preferred path for fidelity
  and throughput, but the screenshot method is now a peer capture mode rather
  than a best-effort viewer grab.
- Cookie export/import is intended for persistent-session bootstrapping between
  real Chrome sessions, including remote torch sessions. The cookie JSON should
  be treated as sensitive session material.
- For download parallelism, prefer a very small number of independent Chrome
  sessions with separate profile dirs, debug ports, shard CSVs, and a nonzero
  `--sleep-jitter-seconds` so workers do not synchronize on the image service.
- For screenshot capture, the current production recommendation is one worker.
  Multi-worker screenshot runs remain experimental and still show more
  almost-blank render failures than the single-worker path.
