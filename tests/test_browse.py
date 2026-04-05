from newspaper_scrapper.adapters.newspapers import browse


def test_exact_issue_url_to_api_url() -> None:
    assert (
        browse.exact_issue_url_to_api_url(
            "https://www.newspapers.com/browse/united-states/new-york/canandaigua/the-daily-messenger_805/1973/01/02/"
        )
        == "https://www.newspapers.com/api/browse/1/united-states/new-york/canandaigua/the-daily-messenger_805/1973/01/02"
    )


def test_build_target_join_rows_prefers_main_edition() -> None:
    target_rows = [{"issue_id": "x", "page_num": "3"}]
    page_rows = [
        {
            "issue_id": "x",
            "page_num": "3",
            "image_id": "999",
            "image_page_url": "https://www.newspapers.com/image/999/",
            "browse_api_url": "root",
            "edition_path": "late-edition",
            "edition_display_name": "Late Edition",
            "page_position": "1",
        },
        {
            "issue_id": "x",
            "page_num": "3",
            "image_id": "123",
            "image_page_url": "https://www.newspapers.com/image/123/",
            "browse_api_url": "main",
            "edition_path": "main-edition",
            "edition_display_name": "Main Edition",
            "page_position": "1",
        },
    ]
    joined, summary = browse.build_target_join_rows(target_rows, page_rows)
    assert summary["matched_target_page_rows"] == 1
    assert joined[0]["preferred_image_id"] == "123"
