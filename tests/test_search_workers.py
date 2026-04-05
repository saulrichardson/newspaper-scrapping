import csv
import json
from pathlib import Path

from newspaper_scrapper.application import search_workers


def test_build_year_shards_balanced() -> None:
    shards = search_workers.build_year_shards(
        start_year=1900,
        end_year=1909,
        worker_count=3,
    )
    assert [(s.start_year, s.end_year) for s in shards] == [
        (1900, 1903),
        (1904, 1906),
        (1907, 1909),
    ]
    assert [s.years for s in shards] == [
        [1900, 1901, 1902, 1903],
        [1904, 1905, 1906],
        [1907, 1908, 1909],
    ]


def test_build_density_aware_date_slices_switches_to_monthly_after_1914() -> None:
    slices = search_workers.build_density_aware_date_slices(
        start_year=1914,
        end_year=1915,
    )
    assert slices[0].label == "1914"
    assert slices[0].date_start == "1914-01-01"
    assert slices[0].date_end == "1914-12-31"
    assert slices[1].label == "1915-01"
    assert slices[1].date_start == "1915-01-01"
    assert slices[1].date_end == "1915-01-31"
    assert slices[-1].label == "1915-12"
    assert len(slices) == 13


def test_plan_search_workers_density_aware_writes_date_ranges_and_sleep(tmp_path: Path) -> None:
    summary = search_workers.plan_search_workers(
        keyword="zoning",
        output_dir=tmp_path / "plan",
        worker_count=2,
        start_year=1914,
        end_year=1915,
        max_pages=1000,
        count_per_request=100,
        sleep_between_requests=1.0,
        max_api_retries=4,
        api_backoff_seconds=60.0,
        entity_types="page",
        location=None,
        base_debug_port=9500,
        profile_root=None,
        cookies_json=None,
        shard_preset="density-aware",
        sleep_scale=1.0,
    )
    assert summary["worker_count_planned"] == 13
    with (tmp_path / "plan" / "worker_plan.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["slice_label"] == "1914"
    assert rows[0]["date_start"] == "1914-01-01"
    assert rows[0]["date_end"] == "1914-12-31"
    assert rows[0]["sleep_between_requests"] == "10.0"
    assert rows[1]["slice_label"] == "1915-01"
    assert rows[1]["date_start"] == "1915-01-01"
    assert rows[1]["date_end"] == "1915-01-31"
    assert rows[1]["sleep_between_requests"] == "12.0"


def test_merge_search_workers_builds_page_and_issue_seeds(tmp_path: Path) -> None:
    workers_root = tmp_path / "workers"
    worker1 = workers_root / "worker_01"
    worker2 = workers_root / "worker_02"
    worker1.mkdir(parents=True)
    worker2.mkdir(parents=True)

    page_rows_1 = [
        {
            "issue_id": "paper-a__1970-01-02__pub1",
            "issue_date": "1970-01-02",
            "page_num": "3",
            "preferred_image_id": "100",
            "preferred_image_page_url": "https://www.newspapers.com/image/100/",
            "query_keyword": "zoning",
            "query_date": "1970-01-01_1979-12-31",
            "query_location": "",
            "entity_types": "page",
            "publication_id": "1",
            "publication_canonical_id": "1",
            "newspaper_display_name": "Paper A",
            "publication_location": "Town A",
            "first_api_page_index": "1",
            "first_search_record_index": "1",
            "viewer_url": "https://www.newspapers.com/image/100?terms=zoning",
        }
    ]
    page_rows_2 = [
        {
            "issue_id": "paper-a__1970-01-02__pub1",
            "issue_date": "1970-01-02",
            "page_num": "5",
            "preferred_image_id": "101",
            "preferred_image_page_url": "https://www.newspapers.com/image/101/",
            "query_keyword": "zoning",
            "query_date": "1970-01-01_1979-12-31",
            "query_location": "",
            "entity_types": "page",
            "publication_id": "1",
            "publication_canonical_id": "1",
            "newspaper_display_name": "Paper A",
            "publication_location": "Town A",
            "first_api_page_index": "1",
            "first_search_record_index": "2",
            "viewer_url": "https://www.newspapers.com/image/101?terms=zoning",
        },
        {
            "issue_id": "paper-b__1980-03-04__pub2",
            "issue_date": "1980-03-04",
            "page_num": "7",
            "preferred_image_id": "200",
            "preferred_image_page_url": "https://www.newspapers.com/image/200/",
            "query_keyword": "zoning",
            "query_date": "1980-01-01_1989-12-31",
            "query_location": "",
            "entity_types": "page",
            "publication_id": "2",
            "publication_canonical_id": "2",
            "newspaper_display_name": "Paper B",
            "publication_location": "Town B",
            "first_api_page_index": "1",
            "first_search_record_index": "1",
            "viewer_url": "https://www.newspapers.com/image/200?terms=zoning",
        },
    ]
    results_rows = [
        {
            "query_keyword": "zoning",
            "query_date": "1970-01-01_1979-12-31",
            "query_location": "",
            "entity_types": "page",
            "search_api_url": "https://example.test/query",
            "api_page_index": "1",
            "api_page_size": "100",
            "search_record_index": "1",
            "record_type": "page",
            "publication_id": "1",
            "publication_canonical_id": "1",
            "newspaper_display_name": "Paper A",
            "publication_location": "Town A",
            "issue_id": "paper-a__1970-01-02__pub1",
            "issue_date": "1970-01-02",
            "page_num": "3",
            "image_id": "100",
            "image_page_url": "https://www.newspapers.com/image/100/",
            "viewer_url": "https://www.newspapers.com/image/100?terms=zoning",
        }
    ]

    for worker, page_rows in [(worker1, page_rows_1), (worker2, page_rows_2)]:
        with (worker / "page_manifest.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=search_workers.PAGE_MANIFEST_FIELDNAMES)
            writer.writeheader()
            writer.writerows(page_rows)
        with (worker / "results.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=search_workers.RESULT_FIELDNAMES)
            writer.writeheader()
            writer.writerows(results_rows)

    output_dir = tmp_path / "merged"
    summary = search_workers.merge_search_workers(
        workers_root=workers_root,
        output_dir=output_dir,
    )

    assert summary["unique_page_hits"] == 3
    assert summary["unique_issue_hits"] == 2

    with (output_dir / "issue_manifest_merged.csv").open(newline="") as handle:
        issue_rows = list(csv.DictReader(handle))
    assert len(issue_rows) == 2
    first = next(row for row in issue_rows if row["issue_id"] == "paper-a__1970-01-02__pub1")
    assert first["hit_page_count"] == "2"

    loaded_summary = json.loads((output_dir / "summary.json").read_text())
    assert loaded_summary["worker_count_detected"] == 2
