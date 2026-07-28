import pytest
from app.instagram_content.service import InstagramContentUrlError, normalize_instagram_url


@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/p/ABC123",
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/p/ABC123/?igsh=anything#fragment",
    ],
)
def test_normalizes_equivalent_post_urls(url):
    result = normalize_instagram_url(url)

    assert result.normalized_url == "https://www.instagram.com/p/ABC123/"
    assert result.shortcode == "ABC123"
    assert result.content_type == "post"


def test_normalizes_reel_url_and_removes_query_string():
    result = normalize_instagram_url("https://instagram.com/reel/Reel_123/?utm_source=test")

    assert result.normalized_url == "https://www.instagram.com/reel/Reel_123/"
    assert result.shortcode == "Reel_123"
    assert result.content_type == "reel"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.instagram.com/p/ABC123/",
        "https://instagram.example/p/ABC123/",
        "https://user:password@instagram.com/p/ABC123/",
        "https://www.instagram.com/example_profile/",
        "https://www.instagram.com/stories/example/123/",
        "https://www.instagram.com/p/ABC123/extra",
    ],
)
def test_rejects_invalid_domains_authority_and_paths(url):
    with pytest.raises(InstagramContentUrlError):
        normalize_instagram_url(url)
