import pytest

from app.security import (
    UnsafeURLError,
    ensure_safe_url,
    sanitize_filename,
    sanitize_format_selector,
)


@pytest.mark.asyncio
async def test_rejects_loopback_and_private_ranges():
    for url in (
        "http://127.0.0.1/video.mp4",
        "http://localhost/video.mp4",
        "http://10.1.2.3/video.mp4",
        "http://192.168.1.2/video.mp4",
        "http://169.254.169.254/latest/meta-data",
        "http://198.18.0.1/video.mp4",
    ):
        with pytest.raises(UnsafeURLError):
            await ensure_safe_url(url)


@pytest.mark.asyncio
async def test_rejects_non_http_schemes():
    with pytest.raises(UnsafeURLError):
        await ensure_safe_url("file:///C:/secret.txt")
    with pytest.raises(UnsafeURLError):
        await ensure_safe_url("ftp://example.com/video.mp4")


def test_filename_is_sanitized():
    assert sanitize_filename('../../bad:name?.mp4') == "_.._bad_name_.mp4"
    assert sanitize_filename("   ") == "media"
    assert sanitize_filename("video.MP4") == "video.mp4"


def test_format_selector_validation():
    assert sanitize_format_selector("bestvideo[height<=1080]+bestaudio/best") == (
        "bestvideo[height<=1080]+bestaudio/best"
    )
    with pytest.raises(ValueError):
        sanitize_format_selector("best; rm -rf /")