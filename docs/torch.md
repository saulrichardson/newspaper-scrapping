# Torch Runtime Notes

This repo now supports a conservative HPC path. `torch` is not a drop-in match
for the old Greene setup from the mail thread, but it is now far enough along
to be viable with a scratch-backed runtime.

## Current observed state on torch

- SSH works.
- `apptainer` is available on the host.
- home is over quota for large image staging in this environment.
- `/scratch/$USER` is writable and should be used for runtime state.
- the Greene-style shared Chrome image paths from the PDF are not present on
  `torch`; `/scratch/work/public/singularity` is missing.
- there are shared base images under `/share/apps/images/`, including
  `/share/apps/images/ubuntu-24.04.3.sif`.
- that shared Ubuntu image already contains `Xvfb` and `xvfb-run`.
- no obvious shared Chrome or Chromium overlay was found on `torch`.
- a pulled Playwright image in scratch works and can launch headful Chromium
  under Xvfb with CDP on `127.0.0.1:9222`.
- a local authenticated Newspapers.com cookie bundle can be imported into the
  remote torch Chrome session to bootstrap auth.

The built-in probe command is:

```bash
poetry run newspaper-scrapper torch-check
```

## Intended remote model

The remote model stays aligned with the local one:

- headful browser semantics only
- persistent per-session profile dirs
- conservative one-session-per-worker execution
- resumable shard CSVs and output dirs

On torch, "headful" means Chrome running under Xvfb inside an Apptainer image,
not Chrome headless mode.

## Included helper scripts

- `scripts/torch/pull_playwright_image.sh`
  - pulls a Playwright-based browser image into a stable local SIF path
- `scripts/torch/launch_headful_chrome.sh`
  - launches Chrome under Xvfb inside that image with:
    - remote debugging port
    - dedicated profile dir
    - conservative Chrome flags for HPC/container use

## Recommended first remote workflow

1. Use scratch, not home:

   ```bash
   export NEWSCOM_TORCH_BASE=/scratch/$USER/newscom-runtime
   mkdir -p "$NEWSCOM_TORCH_BASE"
   ```

2. Pull the browser image once:

   ```bash
   IMAGE_PATH=$NEWSCOM_TORCH_BASE/images/playwright-python-v1.55.0-jammy.sif \
   bash scripts/torch/pull_playwright_image.sh
   ```

3. Launch one remote browser session:

   ```bash
   IMAGE_PATH=$NEWSCOM_TORCH_BASE/images/playwright-python-v1.55.0-jammy.sif \
   PROFILE_DIR=$NEWSCOM_TORCH_BASE/session-a/profile \
   SESSION_ROOT=$NEWSCOM_TORCH_BASE/session-a \
   DEBUG_PORT=9222 \
   START_URL=https://www.newspapers.com/ \
   bash scripts/torch/launch_headful_chrome.sh
   ```

4. Export cookies from a local authenticated Chrome session:

   ```bash
   poetry run newspaper-scrapper auth-export-cookies \
     --output-path output/auth/newscom_cookies.json
   ```

5. Copy that cookie bundle to torch and import it into the remote browser:

   ```bash
   poetry run newspaper-scrapper auth-import-cookies \
     --cookies-json /path/to/newscom_cookies.json
   ```

   The import command must be run against the remote browser session, so in
   practice this means running the repo on torch or using an equivalent remote
   CDP helper.

6. Run scraper commands conservatively against shard CSVs.

## Parallelism guidance

- Start with one remote search session and one remote download session at most.
- Keep a separate:
  - profile dir
  - debug port
  - shard CSV
  - output dir
  for each worker.
- Use `shard-manifest --strategy by_issue`.
- For download workers, use nonzero jitter so sessions do not synchronize on the
  image endpoint.

## What is still missing

The missing work is no longer basic runtime validation. We now know the remote
Chromium session can start and reach Newspapers.com. The remaining work is to
turn the scratch-based bootstrap into a polished remote workflow:

- stage the repo or a minimal runtime bundle on torch
- formalize cookie import into the remote browser session
- run discovery/download commands directly on torch against shard CSVs
- decide whether remote screenshot fallback should be supported alongside the
  signed original-JPEG path
