import json

from app.platforms import (
    detect_platform,
    douyin_play_url,
    extract_input_url,
    is_junk_media_url,
    normalize_platform_url,
    parse_douyin_page,
)
from app.sniffer import _make_item, _sort_key


def test_extract_share_text_and_detect_platform():
    text = (
        "7.43 复制打开抖音，看看【某作者的作品】 https://v.douyin.com/iRNBho6u/ "
        "复制此链接"
    )
    assert extract_input_url(text) == "https://v.douyin.com/iRNBho6u/"
    assert detect_platform(text) == "douyin"
    assert detect_platform("https://vm.tiktok.com/ZMabc123/") == "tiktok"
    assert detect_platform("https://example.com/video/1") is None


def test_parse_douyin_router_data():
    data = {
        "loaderData": {
            "video_(id)/page": {
                "videoInfoRes": {
                    "item_list": [
                        {
                            "aweme_id": "7157519152863890719",
                            "desc": "测试视频标题",
                            "author": {"nickname": "测试作者"},
                            "video": {
                                "duration": 12500,
                                "width": 1080,
                                "height": 1920,
                                "play_addr": {
                                    "uri": "v0200fg10000cda9robc77u9qo4i4hmg",
                                    "url_list": [
                                        "https://aweme.snssdk.com/aweme/v1/playwm/"
                                        "?video_id=v0200fg10000cda9robc77u9qo4i4hmg&ratio=720p"
                                    ],
                                },
                                "cover": {"url_list": ["https://p9.douyinpic.com/cover.webp"]},
                            },
                        }
                    ]
                }
            }
        }
    }
    html = (
        "<html><script>window._ROUTER_DATA = "
        + json.dumps(data, ensure_ascii=False)
        + ";</script></html>"
    )
    parsed = parse_douyin_page(html)
    assert parsed is not None
    assert parsed["video_id"] == "v0200fg10000cda9robc77u9qo4i4hmg"
    assert parsed["title"] == "测试视频标题"
    assert parsed["author"] == "测试作者"
    assert parsed["duration"] == 12.5
    assert parsed["width"] == 1080
    assert parsed["height"] == 1920
    assert parsed["cover"] == "https://p9.douyinpic.com/cover.webp"


def test_parse_douyin_page_regex_fallback():
    html = (
        '<script>{"play_addr":{"uri":"v0200fg10000abc","url_list":[]}}</script>'
        '<meta property="og:title" content="备用标题">'
    )
    parsed = parse_douyin_page(html)
    assert parsed is not None
    assert parsed["video_id"] == "v0200fg10000abc"


def test_douyin_play_url_is_no_watermark_endpoint():
    url = douyin_play_url("v0200abc", "1080p")
    assert "/aweme/v1/play/" in url
    assert "playwm" not in url
    assert "ratio=1080p" in url


def test_junk_media_filter():
    assert is_junk_media_url("https://v16.tiktokcdn.com/apk_new_123.mp4")
    assert is_junk_media_url("https://lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4")
    assert not is_junk_media_url("https://v95-zjjx2tc-cold.douyinvod.com/video/tos/cn/a.mp4")


def test_provider_items_sort_first():
    provider = _make_item(
        kind="video",
        url="https://aweme.snssdk.com/aweme/v1/play/?video_id=x&ratio=720p",
        title="抖音",
        mode="proxy",
        source="抖音解析",
        quality="去水印 · 720P",
    )
    generic = _make_item(
        kind="video",
        url="https://cdn.example.com/a.mp4",
        title="普通",
        mode="proxy",
        source="浏览器嗅探",
        height=1080,
    )
    assert sorted([generic, provider], key=_sort_key)[0]["source"] == "抖音解析"


def test_normalize_platform_url_keeps_standard_douyin_url():
    import asyncio

    canonical, video_id, _ = asyncio.run(
        normalize_platform_url("https://www.douyin.com/video/7157519152863890719")
    )
    assert video_id == "7157519152863890719"
    assert canonical == "https://www.douyin.com/video/7157519152863890719"
