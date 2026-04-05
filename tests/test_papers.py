from newspaper_scrapper.adapters.newspapers import papers


def test_parse_year_range() -> None:
    assert papers.parse_year_range("The Daily Messenger 1950-1980") == (1950, 1980)


def test_choose_browse_base() -> None:
    links = [
        {"href": "https://www.newspapers.com/browse/x/y_805/", "text": "browse"},
        {"href": "https://www.newspapers.com/browse/x/y_999/", "text": "other"},
    ]
    assert (
        papers.choose_browse_base(
            links, "https://www.newspapers.com/paper/the-daily-messenger/805/"
        )
        == "https://www.newspapers.com/browse/x/y_805/"
    )
