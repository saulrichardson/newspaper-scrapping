# Architecture

This repo follows the same high-level ports/adapters split as the older
`newspaper-scrapping` repo, but the browser strategy is different.

## Core design choices

- **Real Chrome, not Playwright Chromium**
  The site tolerated a live Google Chrome session attached over CDP far better
  than automation-flavored browsers.

- **Headful-only**
  Headless mode is intentionally unsupported.

- **Browser for discovery, HTTP for light APIs, browser-derived tokens for images**
  We use the live `/papers/` page to discover paper families, the browse API to
  confirm issues and enumerate page inventories, and a signed image URL
  extracted from the live `/image/<id>/` DOM to fetch full JPEGs.

- **Checkpoint everything**
  Long runs append CSV rows incrementally and keep JSON summaries current so
  they can be resumed safely after a stop.

- **Session-level parallelism only**
  Any parallelism should come from a very small number of independent Chrome
  sessions with separate profile dirs, ports, and shard CSVs. Do not parallelize
  image requests inside one session.

## Layout

- `src/newspaper_scrapper/domain/`
  - pure models and deterministic helpers
- `src/newspaper_scrapper/adapters/chrome/`
  - real Chrome launching, AppleScript fallback, CDP utilities
- `src/newspaper_scrapper/adapters/newspapers/`
  - Newspapers.com-specific DOM expressions, browse API logic, signed image URL
    extraction
- `src/newspaper_scrapper/application/`
  - use-case orchestration for auth, discovery, cataloging, sharding, torch
    checks, downloads, and parser-ready source artifact manifests
- `src/newspaper_scrapper/cli/`
  - Click CLI surface

## Scaling guidance

- Search is lighter than image download.
  - Start with one search session.
  - Only increase beyond that after checking for Cloudflare or rate-limit drift.
- Image download is the tightest constraint.
  - Start with one downloader at `60-90s` base sleep.
  - If a second downloader is needed, give it a separate profile, port, and
    shard, plus nonzero jitter.
- Keep output state per worker.
  - one output dir per shard
  - one summary/results file per worker
  - resume workers independently

## Cross-repo manifest contract

Acquisition exports parser inputs with:

```bash
newspaper-scrapper build-source-artifact-manifest \
  --input-csv output/<run>/results.csv \
  --output-jsonl output/<run>/source_artifacts.jsonl \
  --include-status downloaded \
  --require-files
```

Each JSONL row is intentionally compatible with the parsing repo's parse input
manifest: `page_id`, `image_path`, `issue_id`, `page_number`,
`checksum_sha256`, `source`, and `metadata`. The acquisition repo owns source
identity and checksums; the parser repo owns model outputs, fusion, transcripts,
review packets, and performance reports.

Torch smoke for this contract:

```bash
bash scripts/torch/submit_source_manifest_smoke.sh
```

## Torch / HPC

Torch does not expose the same ready-made public Chrome overlay paths referenced
in the Greene mail thread, but it does expose a shared Ubuntu base image with
`Xvfb`. The current HPC mode is:

1. Pull a browser-capable Apptainer image into `/scratch/$USER`.
2. Run a headful Chromium session under Xvfb inside that image.
3. Keep a dedicated remote profile dir per session.
4. Bootstrap auth with imported cookies from a live local Chrome session.
5. Run the same scraper commands against shard CSVs from inside the remote
   session environment.

See [torch.md](torch.md) and `scripts/torch/` for the runtime bundle.
