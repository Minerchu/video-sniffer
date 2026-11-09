from app.sniffer import (
    _apply_download_mode,
    _extract_from_html,
    _kind_from_url,
    _make_item,
    _merge_items,
)


def test_extract_html_sources_and_escaped_urls():
    html = """
    <html>
      <head>
        <meta property="og:title" content="Demo page">
        <meta property="og:image" content="/cover.jpg">
        <script type="application/ld+json">
          {"@type":"VideoObject","contentUrl":"https://cdn.example.com/ld.mp4"}
        </script>
      </head>
      <body>
        <video poster="/poster.jpg">
          <source src="/media/clip.mp4" type="video/mp4">
        </video>
        <audio src="https://cdn.example.com/song.mp3"></audio>
        <a href="/live/index.m3u8">live</a>
        <script>
          const hls = "https:\\/\\/cdn.example.com\\/escaped\\/playlist.m3u8";
          const image = "https://cdn.example.com/poster.jpg";
        </script>
      </body>
    </html>
    """
    items, meta = _extract_from_html(html, "https://example.com/page")
    urls = {item["url"] for item in items}
    assert "https://example.com/media/clip.mp4" in urls
    assert "https://cdn.example.com/song.mp3" in urls
    assert "https://example.com/live/index.m3u8" in urls
    assert "https://cdn.example.com/ld.mp4" in urls
    assert "https://cdn.example.com/escaped/playlist.m3u8" in urls
    assert "https://cdn.example.com/poster.jpg" not in urls
    assert meta["title"] == "Demo page"
    assert meta["thumbnail"] == "/cover.jpg"


def test_kind_detection():
    assert _kind_from_url("https://example.com/a.m3u8") == "hls"
    assert _kind_from_url("https://example.com/a.mp4") == "video"
    assert _kind_from_url("https://example.com/a.mp3") == "audio"
    assert _kind_from_url("https://example.com/a.jpg") is None
    assert _kind_from_url("https://example.com/play", "application/vnd.apple.mpegurl") == "hls"


def test_make_item_and_merge():
    a = _make_item(
        kind="video",
        url="https://cdn.example.com/a.mp4",
        title="Clip",
        mode="proxy",
        source="网页源码",
        size=100,
    )
    b = _make_item(
        kind="video",
        url="https://cdn.example.com/a.mp4",
        title="",
        mode="proxy",
        source="浏览器嗅探",
        thumbnail="https://cdn.example.com/a.jpg",
    )
    merged = _merge_items([a, b])
    assert len(merged) == 1
    assert merged[0]["size"] == 100
    assert merged[0]["thumbnail"] == "https://cdn.example.com/a.jpg"
    assert "网页源码" in merged[0]["source"]
    assert "浏览器嗅探" in merged[0]["source"]


def test_download_mode_prefers_job_for_hls_and_site_items():
    hls = _make_item(
        kind="hls",
        url="https://cdn.example.com/a.m3u8",
        title="HLS",
        mode="proxy",
        source="网页源码",
    )
    site = _make_item(
        kind="video",
        url="https://example.com/video/1",
        title="Site",
        mode="proxy",
        source="站点解析",
    )
    direct = _make_item(
        kind="video",
        url="https://cdn.example.com/a.mp4",
        title="Direct",
        mode="proxy",
        source="网页源码",
    )
    items = [hls, site, direct]
    _apply_download_mode(items, None)
    assert hls["mode"] == "job"
    assert site["mode"] == "job"
    assert direct["mode"] == "proxy"