from newspaper_scrapper.adapters.newspapers import image


def test_build_full_image_url() -> None:
    probe = {
        "tile": {
            "href": "https://img.newspapers.com/img/img?id=123&user=456&iat=789&brightness=1&contrast=2&invert=0"
        },
        "thumbnail": {"width": "3858", "height": "5221"},
    }
    assert (
        image.build_full_image_url(probe)
        == "https://img.newspapers.com/img/img?id=123&user=456&iat=789&brightness=1&contrast=2&invert=0&width=3858&height=5221"
    )


def test_extract_page_metadata_from_script_texts() -> None:
    script_texts = [
        r'self.__next_f.push([1,"11:[{\"page\":{\"image\":{\"imageId\":290573965,\"width\":5063,\"height\":7615,\"canView\":true,\"reasonCanView\":\"extra\"},\"articles\":null},\"iat\":\"token-123\",\"rights\":{\"Download\":{\"allowed\":true,\"fcfToken\":\"fcf-456\"}}}]"])'
    ]
    metadata = image.extract_page_metadata_from_script_texts(
        script_texts,
        user="17976350",
    )
    assert metadata == {
        "imageId": "290573965",
        "width": "5063",
        "height": "7615",
        "publicationId": "",
        "canView": True,
        "reasonCanView": "extra",
        "iat": "token-123",
        "downloadFcfToken": "fcf-456",
        "user": "17976350",
    }


def test_build_full_image_url_prefers_page_metadata() -> None:
    probe = {
        "pageMeta": {
            "imageId": "290573965",
            "width": "5063",
            "height": "7615",
            "iat": "token-123",
            "user": "17976350",
        },
        "tile": {
            "href": "https://img.newspapers.com/img/img?id=123&user=456&iat=789&brightness=1&contrast=2&invert=0"
        },
        "thumbnail": {"width": "3858", "height": "5221"},
    }
    assert (
        image.build_full_image_url(probe)
        == "https://img.newspapers.com/img/img?id=290573965&user=17976350&iat=token-123&brightness=0&contrast=0&invert=0&width=5063&height=7615"
    )
