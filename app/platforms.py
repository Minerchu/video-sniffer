from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import config
from .security import ensure_safe_url, sanitize_header_value


DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
TIKTOK_HOSTS = ("tiktok.com",)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_RE = re.compile(
    r"(?:(?:www|m|v|vm|vt)\.)?(?:douyin|tiktok)\.com/[^\s<>\"']+",
    re.IGNORECASE,
)
_DOUYIN_ID_RE = re.compile(r"/(?:video|note|share/video)/(\d{10,25})")
_TIKTOK_ID_RE = re.compile(r"/(?:video|photo)/(\d{10,25})")
_DOUYIN_SHARE_ID_RE = re.compile(r"(?:modal_id=|aweme_id=)(\d{10,25})")
_ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*", re.IGNORECASE)
_DOUYIN_PLAY_URI_RE = re.compile(r'"play_addr"\s*:\s*\{[^{}]*?"uri"\s*:\s*"([^"]+)"', re.DOTALL)
_DOUYIN_DESC_RE = re.compile(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"')


@dataclass
class PlatformEntry:
    url: str
    kind: str = "video"
    mode: str = "proxy"
    ext: str | None = None
    quality: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    size: int | None = None
    thumbnail: str | None = None
    selector: str | None = None
    referer: str | None = None
    note: str | None = None
    title: str | None = None


@dataclass
class PlatformResult:
    platform: str
    status: str
    entries: list[PlatformEntry]
    title: str = ""
    thumbnail: str = ""
    final_url: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "抖音" if self.platform == "douyin" else "TikTok"


_DOUYIN_BROWSER_SEMAPHORE = asyncio.Semaphore(1)


def _clean_input(text: str | None) -> str:
    value = str(text or "").strip()
    match = _URL_RE.search(value)
    if match:
        url = match.group(0)
    else:
        match = _BARE_RE.search(value)
        if not match:
            return value
        url = "https://" + match.group(0)
    return url.rstrip(".,;:!?)]}\"'，。；：！？、】）")


def extract_input_url(text: str | None) -> str:
    return _clean_input(text)


def detect_platform(url: str | None) -> str | None:
    value = _clean_input(url)
    try:
        host = (urlsplit(value).hostname or "").lower()
    except ValueError:
        return None
    if any(host == domain or host.endswith("." + domain) for domain in DOUYIN_HOSTS):
        return "douyin"
    if any(host == domain or host.endswith("." + domain) for domain in TIKTOK_HOSTS):
        return "tiktok"
    return None


def _douyin_id(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    for pattern in (_DOUYIN_ID_RE, _DOUYIN_SHARE_ID_RE):
        match = pattern.search(parsed.path + "?" + parsed.query)
        if match:
            return match.group(1)
    return None


def _tiktok_id(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    match = _TIKTOK_ID_RE.search(path)
    return match.group(1) if match else None


async def normalize_platform_url(url: str) -> tuple[str, str | None, str | None]:
    value = _clean_input(url)
    platform = detect_platform(value)
    if not platform:
        return value, None, None
    safe_url = await ensure_safe_url(value)
    if platform == "douyin":
        video_id = _douyin_id(safe_url)
        if not video_id:
            safe_url, video_id = await _resolve_douyin_short_url(safe_url)
        canonical = f"https://www.douyin.com/video/{video_id}" if video_id else safe_url
        return canonical, video_id, safe_url
    video_id = _tiktok_id(safe_url)
    if not video_id and any(
        (urlsplit(safe_url).hostname or "").lower().startswith(("vm.", "vt."))
        or "/t/" in urlsplit(safe_url).path
        for _ in (0,)
    ):
        resolved = await _resolve_tiktok_short_url(safe_url)
        if resolved:
            safe_url = resolved
            video_id = _tiktok_id(resolved)
    return safe_url, video_id, safe_url


async def _resolve_douyin_short_url(url: str) -> tuple[str, str | None]:
    headers = {"User-Agent": config.MOBILE_USER_AGENT, "Accept": "text/html,*/*"}
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=False,
            headers=headers,
        ) as client:
            for _ in range(6):
                video_id = _douyin_id(current)
                if video_id:
                    return current, video_id
                current = await ensure_safe_url(current)
                response = await client.get(current)
                location = response.headers.get("location")
                if location:
                    current = urljoin(current, location)
                    continue
                video_id = _douyin_id(str(response.url)) or _douyin_id(current)
                if video_id:
                    return current, video_id
                text = response.text[:200000]
                video_id = _douyin_id(text)
                if video_id:
                    return current, video_id
                break
    except Exception:
        pass
    return current, _douyin_id(current)


async def _resolve_tiktok_short_url(url: str) -> str | None:
    headers = {"User-Agent": config.DESKTOP_USER_AGENT, "Accept": "text/html,*/*"}
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=False,
            headers=headers,
        ) as client:
            for _ in range(6):
                current = await ensure_safe_url(current)
                response = await client.get(current)
                if _tiktok_id(current):
                    return current
                location = response.headers.get("location")
                if location:
                    current = urljoin(current, location)
                    continue
                if _tiktok_id(str(response.url)):
                    return str(response.url)
                break
    except Exception:
        pass
    return None


def _find_douyin_item(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        item_list = value.get("item_list")
        if isinstance(item_list, list):
            for item in item_list:
                if isinstance(item, dict) and item.get("video"):
                    return item
        for child in value.values():
            found = _find_douyin_item(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_douyin_item(child)
            if found:
                return found
    return None


def _first_url(value: Any) -> str | None:
    if isinstance(value, dict):
        return _first_url(value.get("url_list"))
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
    return None


def parse_douyin_page(html_text: str) -> dict[str, Any] | None:
    data: dict[str, Any] | None = None
    match = _ROUTER_DATA_RE.search(html_text or "")
    if match:
        decoder = json.JSONDecoder()
        try:
            data, _ = decoder.raw_decode((html_text[match.end():]).lstrip())
        except (ValueError, TypeError):
            data = None

    item = _find_douyin_item(data) if data else None
    video = item.get("video") if item else None
    if not isinstance(video, dict):
        video = None

    play_uri = None
    if video:
        play_uri = (video.get("play_addr") or {}).get("uri")
    if not play_uri:
        uri_match = _DOUYIN_PLAY_URI_RE.search(html_text or "")
        if uri_match:
            play_uri = uri_match.group(1)

    title = ""
    author = ""
    cover = None
    duration = None
    width = None
    height = None
    aweme_id = None
    is_images = False
    music_title = None

    if item:
        title = str(item.get("desc") or "").strip()
        author_info = item.get("author") or {}
        author = str(author_info.get("nickname") or "").strip()
        aweme_id = str(item.get("aweme_id") or "").strip() or None
        is_images = bool(item.get("images"))
        music = item.get("music") or {}
        music_title = str(music.get("title") or "").strip() or None
        if video:
            cover = _first_url(video.get("cover")) or _first_url(video.get("origin_cover"))
            width = video.get("width") if isinstance(video.get("width"), int) else None
            height = video.get("height") if isinstance(video.get("height"), int) else None
            duration_ms = video.get("duration") or item.get("duration")
            if isinstance(duration_ms, (int, float)) and duration_ms > 0:
                duration = float(duration_ms) / 1000.0 if duration_ms > 1000 else float(duration_ms)

    if not title:
        desc_match = _DOUYIN_DESC_RE.search(html_text or "")
        if desc_match:
            try:
                title = json.loads(f'"{desc_match.group(1)}"').strip()
            except (ValueError, TypeError):
                title = desc_match.group(1).strip()

    if not play_uri:
        return None
    return {
        "video_id": play_uri,
        "aweme_id": aweme_id,
        "title": title,
        "author": author,
        "cover": cover,
        "duration": duration,
        "width": width,
        "height": height,
        "music_title": music_title,
        "is_images": is_images,
    }


def douyin_play_url(video_id: str, ratio: str = "1080p") -> str:
    return (
        "https://aweme.snssdk.com/aweme/v1/play/"
        f"?video_id={quote(str(video_id), safe='')}&ratio={ratio}&line=0"
    )


def is_junk_media_url(url: str) -> bool:
    lowered = (url or "").lower()
    junk_markers = (
        "apk_new",
        "/obj/douyin-pc-web/",
        "/douyin-pc-web/",
    )
    return any(marker in lowered for marker in junk_markers)


def _meta_from_html(html_text: str) -> dict[str, str]:
    result = {"title": "", "image": ""}
    for key in ("og:title", "twitter:title"):
        match = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)',
            html_text or "",
            re.IGNORECASE,
        )
        if match:
            result["title"] = match.group(1).strip()
            break
    for key in ("og:image", "twitter:image"):
        match = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']*)',
            html_text or "",
            re.IGNORECASE,
        )
        if match:
            result["image"] = match.group(1).strip()
            break
    return result


def _clean_title(title: str) -> str:
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    return re.sub(r"\s*[-_|]\s*抖音\s*$", "", value)


async def _probe_media(url: str, referer: str) -> dict[str, Any] | None:
    headers = {
        "User-Agent": config.MOBILE_USER_AGENT,
        "Referer": referer,
        "Accept": "*/*",
        "Range": "bytes=0-1",
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=6.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(url)
            if response.status_code not in {200, 206}:
                return None
            content_type = (response.headers.get("content-type") or "").lower()
            if content_type and not content_type.startswith(
                ("video/", "audio/", "application/octet-stream")
            ):
                return None
            size = None
            content_range = response.headers.get("content-range") or ""
            range_match = re.search(r"/(\d+)\s*$", content_range)
            if range_match:
                size = int(range_match.group(1))
            elif (response.headers.get("content-length") or "").isdigit():
                size = int(response.headers["content-length"])
            return {"size": size, "content_type": content_type, "final_url": str(response.url)}
    except Exception:
        return None


async def _sniff_douyin(
    url: str,
    cookie: str | None,
    referer: str | None,
) -> PlatformResult:
    canonical, video_id, resolved = await normalize_platform_url(url)
    result = PlatformResult(platform="douyin", status="error", entries=[], final_url=canonical)
    if not config.ENABLE_PLAYWRIGHT:
        result.warnings.append("抖音解析需要浏览器组件；请启用 Playwright 或运行 playwright install chromium")
        return result

    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except Exception as exc:
        result.warnings.append(f"抖音解析不可用：{exc}")
        return result

    target = canonical if canonical.startswith("https://www.douyin.com/video/") else resolved
    captured: list[str] = []
    router: dict[str, Any] | None = None
    page_title = ""
    html_text = ""

    try:
        async with _DOUYIN_BROWSER_SEMAPHORE:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                try:
                    context = await browser.new_context(
                        user_agent=config.MOBILE_USER_AGENT,
                        viewport={"width": 390, "height": 844},
                        device_scale_factor=2,
                        is_mobile=True,
                        has_touch=True,
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        ignore_https_errors=True,
                        service_workers="block",
                        extra_http_headers=({"Cookie": cookie} if cookie else {}),
                    )
                    page = await context.new_page()

                    def on_response(response: Any) -> None:
                        media_url = response.url
                        if "douyinvod.com" not in media_url and "/aweme/v1/play" not in media_url:
                            return
                        if is_junk_media_url(media_url):
                            return
                        if media_url not in captured:
                            captured.append(media_url)

                    page.on("response", on_response)

                    try:
                        await page.goto(target, wait_until="domcontentloaded", timeout=25000)
                    except PlaywrightError as exc:
                        result.warnings.append(f"抖音页面加载提示：{str(exc)[:160]}")
                    await page.wait_for_timeout(2200)

                    html_text = await page.content()
                    router = parse_douyin_page(html_text)
                    if not router:
                        try:
                            await page.evaluate(
                                """() => document.querySelectorAll('video').forEach((node) => {
                                    try {
                                        node.muted = true;
                                        const result = node.play();
                                        if (result && result.catch) result.catch(() => {});
                                    } catch (error) {}
                                })"""
                            )
                        except Exception:
                            pass
                        await page.wait_for_timeout(3000)
                        try:
                            html_text = await page.content()
                            router = parse_douyin_page(html_text)
                        except Exception:
                            pass

                    if not router:
                        video_match = re.search(r"video_id=([A-Za-z0-9]+)", html_text or "")
                        if video_match:
                            router = {"video_id": video_match.group(1)}

                    try:
                        page_title = await page.title()
                    except Exception:
                        page_title = ""
                    result.final_url = page.url or canonical
                finally:
                    await browser.close()
    except PlaywrightError as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message.lower():
            message = "Chromium 未安装，请运行 python -m playwright install chromium"
        result.warnings.append(f"抖音解析失败：{message[:200]}")
        return result
    except Exception as exc:
        result.warnings.append(f"抖音解析失败：{str(exc)[:200]}")
        return result

    meta = _meta_from_html(html_text)
    if router:
        result.title = _clean_title(router.get("title") or meta.get("title") or page_title)
        result.thumbnail = router.get("cover") or meta.get("image") or ""
        if router.get("is_images") and not router.get("video_id"):
            result.warnings.append("该抖音链接是图文作品，没有可下载的视频")

    if router and router.get("video_id"):
        uri = router["video_id"]
        best_url = douyin_play_url(uri, "1080p")
        sd_url = douyin_play_url(uri, "720p")
        best_info, sd_info = await asyncio.gather(
            _probe_media(best_url, canonical),
            _probe_media(sd_url, canonical),
        )
        if best_info:
            result.entries.append(
                PlatformEntry(
                    url=best_url,
                    kind="video",
                    mode="proxy",
                    ext="mp4",
                    quality="去水印 · 最佳画质",
                    width=router.get("width"),
                    height=router.get("height"),
                    duration=router.get("duration"),
                    size=best_info.get("size"),
                    thumbnail=result.thumbnail,
                    referer=canonical,
                    title=result.title,
                    note="抖音去水印直链，有效期有限，请尽快下载；失效后重新解析即可。",
                )
            )
        if sd_info and (not best_info or sd_info.get("size") != best_info.get("size")):
            result.entries.append(
                PlatformEntry(
                    url=sd_url,
                    kind="video",
                    mode="proxy",
                    ext="mp4",
                    quality="去水印 · 720P",
                    duration=router.get("duration"),
                    size=sd_info.get("size"),
                    thumbnail=result.thumbnail,
                    referer=canonical,
                    title=result.title,
                    note="较小体积版本，适合手机流量。",
                )
            )
        if not result.entries:
            result.entries.append(
                PlatformEntry(
                    url=best_url,
                    kind="video",
                    mode="proxy",
                    ext="mp4",
                    quality="去水印 · 最佳画质",
                    width=router.get("width"),
                    height=router.get("height"),
                    duration=router.get("duration"),
                    thumbnail=result.thumbnail,
                    referer=canonical,
                    title=result.title,
                    note="直链校验超时，通常仍可下载；若失败请重新解析。",
                )
            )
        result.entries.append(
            PlatformEntry(
                url=douyin_play_url(uri, "540p"),
                kind="audio",
                mode="job",
                ext="mp3",
                quality="仅音频 · 192kbps",
                duration=router.get("duration"),
                thumbnail=result.thumbnail,
                selector="best",
                referer=canonical,
                title=result.title,
                note="下载后自动提取为 MP3。",
            )
        )
    elif captured:
        best = next(
            (media_url for media_url in captured if "douyinvod.com" in media_url),
            captured[0],
        )
        captured_video_id = re.search(r"video_id=([A-Za-z0-9]+)", best)
        if captured_video_id:
            best = douyin_play_url(captured_video_id.group(1), "1080p")
        result.entries.append(
            PlatformEntry(
                url=best,
                kind="video",
                mode="proxy",
                ext="mp4",
                quality="页面捕获 · 最佳画质",
                thumbnail=result.thumbnail or meta.get("image"),
                referer=result.final_url or canonical,
                title=result.title or _clean_title(meta.get("title") or page_title),
                note="直链有效期有限，请尽快下载。",
            )
        )

    if not result.entries:
        result.warnings.append(
            "抖音页面没有返回可下载地址，可能作品已删除、需要登录，或触发了风控。可尝试在高级设置中填写 Cookie。"
        )
    else:
        result.status = "ok"
        if not result.title:
            result.title = _clean_title(meta.get("title") or page_title) or "抖音视频"
        if not result.thumbnail:
            result.thumbnail = meta.get("image") or ""
        if result.warnings:
            result.warnings = [w for w in result.warnings if "页面加载提示" not in w]
    return result


async def _sniff_tiktok(
    url: str,
    cookie: str | None,
    referer: str | None,
) -> PlatformResult:
    canonical, video_id, _ = await normalize_platform_url(url)
    result = PlatformResult(platform="tiktok", status="error", entries=[], final_url=canonical)

    from .sniffer import _ytdlp_extract

    items, meta, warnings, status = await asyncio.to_thread(
        _ytdlp_extract, canonical, cookie, referer
    )
    if status != "ok" or not items:
        await asyncio.sleep(1.2)
        items, meta, warnings, status = await asyncio.to_thread(
            _ytdlp_extract, canonical, cookie, referer
        )
    result.title = str(meta.get("title") or "")
    result.thumbnail = str(meta.get("thumbnail") or "")
    result.final_url = str(meta.get("final_url") or canonical)
    if status != "ok" or not items:
        result.warnings.extend(
            w for w in warnings if w and "impersonat" not in w.lower()
        )
        result.warnings.append("TikTok 解析未命中，请确认链接可公开访问或填写 Cookie。")
        return result

    for item in items:
        selector = item.get("format_selector") or "bestvideo*+bestaudio/best"
        note = "TikTok 无水印版本，下载时自动合并音视频。" if item.get("kind") == "video" else "下载后自动转为 MP3。"
        result.entries.append(
            PlatformEntry(
                url=item["url"],
                kind=item["kind"],
                mode="job",
                ext=item.get("ext"),
                quality=item.get("quality"),
                width=item.get("width"),
                height=item.get("height"),
                duration=item.get("duration"),
                size=item.get("size"),
                thumbnail=item.get("thumbnail") or result.thumbnail,
                selector=selector,
                referer=item.get("referer") or result.final_url,
                note=note,
                title=item.get("title") or result.title,
            )
        )
    result.status = "ok"
    result.warnings.extend(w for w in warnings if w and "impersonat" not in w.lower())
    return result


async def sniff_platform(
    url: str,
    cookie: str | None = None,
    referer: str | None = None,
) -> PlatformResult | None:
    platform = detect_platform(url)
    if not platform:
        return None
    cookie = sanitize_header_value(cookie)
    referer = sanitize_header_value(referer)
    try:
        if platform == "douyin":
            return await _sniff_douyin(url, cookie, referer)
        return await _sniff_tiktok(url, cookie, referer)
    except Exception as exc:
        label = "抖音" if platform == "douyin" else "TikTok"
        return PlatformResult(
            platform=platform,
            status="error",
            entries=[],
            warnings=[f"{label}解析失败：{str(exc)[:200]}"],
        )
