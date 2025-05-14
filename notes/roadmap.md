### Road-map to a 200 M-page NewspaperArchive Harvester

*(one account ➜ many, one laptop ➜ Slurm cluster, k pages ➜ hundreds M)*

| Phase                     | Corpus size / pressure        | Queue & DB                                           | Binary storage                             | Worker model            | **You do**                                                                                                                   | **When to move on**                                          |
| ------------------------- | ----------------------------- | ---------------------------------------------------- | ------------------------------------------ | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| **0 — Notebook POC**      | ≤ 50 k pages • single run     | no DB (or tiny SQLite)                               | local disk                                 | single Python           | • dump `cookies.json` & `ua.txt` with **login\_once.py**  <br>• run `python -m na_scraper.pipeline` over `cfg/seed_urls.txt` | want parallel or progress tracking                           |
| **1 — Small HPC**         | ≤ 1 M pages • dozens of tasks | **SQLite WAL** (`run/meta.sqlite`)                   | shared Lustre / GPFS (`run/pages/`)        | Slurm array 10–200      | • generate **shards/NNN.txt** once  <br>• `sbatch --array` (each task reads its shard)                                       | *database-is-locked* errors **or** shard admin pain          |
| **2 — Medium (1-10 M)**   | many writers • retries        | **PostgreSQL** (single VM, monthly partitions)       | same FS                                    | Slurm array 100–1 000   | • `pgloader meta.sqlite → pg`  <br>• replace `db.py` connection string                                                       | backlog needs dynamic retries **or** > 50 simultaneous tasks |
| **3 — Medium+ (10-40 M)** | shard files annoying          | **Redis Streams** for queue  <br>Postgres still meta | same FS                                    | Slurm array or tiny K8s | • deploy `redis-server`  <br>• swap `index.py` to `XREADGROUP / XACK`                                                        | queue ≫ RAM **or** restart risk                              |
| **4 — Large (40-100 M)**  | memory limit, long backlog    | **Kafka** (or AWS SQS/Kinesis)  <br>Postgres meta    | local FS **or** MinIO/S3                   | Slurm/K8s 1-5 k pods    | • create topic `pages`  <br>• `index.py` → Kafka consumer                                                                    | inode warnings; directory listing slow                       |
| **5 — Huge (≥100 M)**     | fs inode / backup pain        | Kafka + Postgres/ClickHouse                          | **Object storage** bucket (S3, GCS, Azure) | K8s HPA / Slurm         | • `save_image()` uploads to bucket  <br>• optional ClickHouse for analytics                                                  | throughput limited only by NA / egress                       |

---

## File / module layout that survives every phase

```
cfg/                  # only knobs you hand-edit
    scraper.toml      # run_dir, cookie paths, queue URI
    seed_urls.txt     # used only in phase 0–1

run/ (git-ignored)    # mutable data – never moved
    cookies.json
    ua.txt
    pages/
    meta.sqlite  (later: still valid for Postgres dump)
    logs/

src/na_scraper/
    config.py         # reads scraper.toml, picks cookie file
    session.py        # make_session()
    decide.py         # classic | proxy | stitch
    download/         # engines (code never changes)
    index.py          # **only file you swap** (txt → Redis → Kafka)
    db.py             # minimal CRUD, swap SQLite → Postgres
    pipeline.py       # glue (stays identical through all phases)

scripts/
    login_once.py     # interactive Selenium → dump cookie/UA
    shard_urls.py     # create shard files (phase 1)
    sbatch_array.sh   # Slurm wrapper (no flags)
```

---

## Everyday commands

| Action                                | One-liner                                      |
| ------------------------------------- | ---------------------------------------------- |
| **Refresh cookie** (local login node) | `python scripts/login_once.py`                 |
| **Quick local scrape test**           | `python -m na_scraper.pipeline`                |
| **Launch array (phase 1+)**           | `sbatch --array=0-199 scripts/sbatch_array.sh` |
| **Create shard files** (phase 1)      | `python scripts/shard_urls.py`                 |
| **Promote SQLite → Postgres**         | `pgloader run/meta.sqlite  postgresql:///na`   |

---

## Decision triggers (memorise these)

* SQLite “database is locked” ▶ **move to Postgres**
* Managing dozens of shard files ▶ **introduce Redis Streams**
* Redis RAM ≥ 100 GB or restarts risky ▶ **switch queue to Kafka**
* File tree millions of files / inode alerts ▶ **upload images to S3/MinIO**
* NA 429 throttling ▶ **add second cookie slot** (no infra change)

Each trigger alters **one module** (queue, DB, or storage helper);
engines, session logic, and Slurm scripts stay untouched.

---

### Keep this roadmap handy

Follow the row that matches your current pain point, upgrade *exactly one component*, and you’ll scale from a single-cookie notebook to a 200-million-page cluster without ever rewriting the download engines you finished today.

