import csv

from newspaper_scrapper.application import sharding


def test_shard_manifest_by_issue(tmp_path) -> None:
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "\n".join(
            [
                "issue_id,issue_date,page_num,preferred_image_id,preferred_image_page_url",
                "issue-a,1970-01-01,1,11,https://www.newspapers.com/image/11/",
                "issue-a,1970-01-01,2,12,https://www.newspapers.com/image/12/",
                "issue-b,1970-01-02,1,21,https://www.newspapers.com/image/21/",
                "issue-c,1970-01-03,1,31,https://www.newspapers.com/image/31/",
            ]
        )
        + "\n"
    )
    output_dir = tmp_path / "shards"
    summary = sharding.shard_manifest(
        manifest_csv=manifest,
        output_dir=output_dir,
        num_shards=2,
        strategy="by_issue",
    )
    assert summary["total_rows"] == 4
    assert len(summary["shards"]) == 2

    shard_rows = []
    for index in (1, 2):
        path = output_dir / f"shard_{index:03d}.csv"
        with path.open(newline="") as handle:
            shard_rows.append(list(csv.DictReader(handle)))

    issue_sets = [{row["issue_id"] for row in rows} for rows in shard_rows]
    assert any("issue-a" in issue_set for issue_set in issue_sets)
    assert all(
        not ({"issue-a", "issue-b"} <= issue_set) for issue_set in issue_sets
    )
