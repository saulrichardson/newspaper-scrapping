from newspaper_scrapper.adapters.newspapers import search


def test_build_search_results_url() -> None:
    assert (
        search.build_search_results_url(keyword="zoning", date="1970-01-01_1979-12-31", page=2)
        == "https://www.newspapers.com/search/results/?keyword=zoning&date=1970-01-01_1979-12-31&page=2"
    )


def test_build_search_results_url_with_date_range() -> None:
    assert (
        search.build_search_results_url(
            keyword="zoning",
            date_start="1990-01-01",
            date_end="1990-01-31",
            page=2,
        )
        == "https://www.newspapers.com/search/results/?keyword=zoning&date-start=1990-01-01&date-end=1990-01-31&page=2"
    )


def test_build_search_api_url() -> None:
    assert (
        search.build_search_api_url(
            keyword="zoning",
            date="1970-01-01_1979-12-31",
            location="new-york",
            entity_types="page",
            count=100,
            start="*",
        )
        == "https://www.newspapers.com/api/search/query?keyword=zoning&entity-types=page&sort=score-desc&start=%2A&count=100&date=1970-01-01_1979-12-31&location=new-york"
    )


def test_build_search_api_url_with_date_range() -> None:
    assert (
        search.build_search_api_url(
            keyword="zoning",
            date_start="1990-01-01",
            date_end="1990-01-31",
            entity_types="page",
            count=100,
            start="*",
        )
        == "https://www.newspapers.com/api/search/query?keyword=zoning&entity-types=page&sort=score-desc&start=%2A&count=100&date-start=1990-01-01&date-end=1990-01-31"
    )


def test_build_search_issue_id() -> None:
    assert (
        search.build_search_issue_id(
            publication_name="The Daily Messenger",
            issue_date="1973-01-02",
            publication_canonical_id=805,
        )
        == "the-daily-messenger__1973-01-02__pub805"
    )


def test_canonical_image_page_url() -> None:
    assert (
        search.canonical_image_page_url(22175081)
        == "https://www.newspapers.com/image/22175081/"
    )
