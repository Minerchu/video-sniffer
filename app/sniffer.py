from __future__ import annotations

import asyncio
import hashlib
import html as html_module
import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from . import config
from .models import MediaItem, ParseResponse
from .platforms import (
    extract_input_url,
    is_junk_media_url,
    sniff_platform,
)
from .security import (
    _is_obviously_private_host,
    ensure_safe_url,
    sanitize_header_value,
)


VIDEO_EXTS = {"mp4", "flv", "webm", "mov", "mkv", "m4v", "avi", "mpd"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "ogg", "wav", "opus", "flac"}
MEDIA_URL_RE = re.compile(
    r"\.(" + "|".join(sorted(VIDEO_EXTS | AUDIO_EXTS | {"m3u8", "ts"})) + r")(?:[?#]|$)",
    re.IGNORECASE,
)
ANY_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
RELATIVE_MEDIA_RE = re.compile(
    r"[\"']([^\"']+?\.(?:m3u8|mp4|flv|webm|mov|mkv|m4v|mpd|mp3|m4a|aac|ogg|wav|opus|flac)(?:\?[^\"']*)?)[\"']",
    re.IGNORECASE,
)
_BROWSER_SEMAPHORE = asyncio.Semaphore(2)
_MAX_HTML_BYTES = 4 * 1024 * 1024


def _short(value: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _ext_from_url(url: str) -> str:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return ""
    if "." not in path:
        return ""
    return path.rsplit(".", 1)[-1][:8]


def _kind_from_url(url: str, content_type: str = "") -> str | None:
    content_type = (content_type or "").lower()
    lowered = (url or "").lower()
    try:
        parts = urlsplit(lowered)
        path = parts.path
        query = parts.query
    except ValueError:
        return None
    ext = _ext_from_url(lowered)
    if "mpegurl" in content_type or ext == "m3u8" or ".m3u8" in path or "m3u8" in query:
        return "hls"
    if ext == "mpd":
        return "video"
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("video/"):
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def _normalize_candidate(raw: Any, base_url: str) -> str | None:
    if raw is None:
        return None
    value = html_module.unescape(str(raw)).strip()
    value = (
        value.replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )
    value = value.strip(" \t\r\n\"'")
    if not value or value.lower().startswith(("blob:", "data:", "javascript:")):
        return None
    if value.startswith("//"):
        value = "https:" + value
    if not value.lower().startswith(("http://", "https://")):
        value = urljoin(base_url, value)
    if not value.lower().startswith(("http://", "https://")):
        return None
    value = value.rstrip(").,;]}\"'")
    return value or None


def _item_id(kind: str, url: str, selector: str | None = None) -> str:
    raw = f"{kind}|{url}|{selector or ''}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()[:12]


def _make_item(
    *,
    kind: str,
    url: str,
    title: str,
    mode: str,
    source: str,
    ext: str | None = None,
    quality: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration: float | None = None,
    size: int | None = None,
    thumbnail: str | None = None,
    selector: str | None = None,
    referer: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    resolved_ext = (ext or _ext_from_url(url) or {
        "hls": "m3u8",
        "audio": "mp3",
        "live": "mp4",
    }.get(kind, "mp4")).lower()
    format_label = "HLS" if kind == "hls" or resolved_ext == "m3u8" else resolved_ext.upper()
    thumb = _normalize_candidate(thumbnail, url) if thumbnail else None
    return {
        "id": _item_id(kind, url, selector),
        "kind": kind,
        "title": _short(title, 300) or "未命名媒体",
        "url": url,
        "mode": mode,
        "format": format_label,
        "quality": _short(quality, 80) if quality else None,
        "ext": resolved_ext,
        "width": width if isinstance(width, int) and width > 0 else None,
        "height": height if isinstance(height, int) and height > 0 else None,
        "duration": float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        "size": size if isinstance(size, int) and size > 0 else None,
        "thumbnail": thumb,
        "source": source,
        "format_selector": selector,
        "referer": referer,
        "note": _short(note, 240) if note else None,
    }
def _extract_from_html(html_text: str, base_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    title = ""
    thumbnail = ""
    candidates: list[dict[str, Any]] = []

    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content")
        if not content:
            continue
        if prop in {"og:title", "twitter:title"} and not title:
            title = content
        elif prop in {"og:image", "twitter:image", "twitter:image:src"} and not thumbnail:
            thumbnail = content

    def add(raw: Any, poster: str | None = None) -> None:
        url = _normalize_candidate(raw, base_url)
        if not url or is_junk_media_url(url):
            return
        kind = _kind_from_url(url)
        if not kind:
            return
        candidates.append({"url": url, "kind": kind, "thumbnail": poster})

    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content")
        if not content:
            continue
        if (
            prop.startswith("og:video")
            or prop.startswith("og:audio")
            or prop in {"twitter:player:stream"}
        ):
            add(content)

    for tag in soup.find_all(["video", "audio", "source"]):
        poster = tag.get("poster") if tag.name == "video" else None
        if not poster and tag.parent is not None and tag.parent.name == "video":
            poster = tag.parent.get("poster")
        for attr in (
            "src",
            "data-src",
            "data-url",
            "data-video",
            "data-mp4",
            "data-hls",
            "data-original",
        ):
            value = tag.get(attr)
            if value:
                add(value, poster)

    for tag in soup.find_all(["link", "a"]):
        rel = " ".join(tag.get("rel") or []).lower() if tag.get("rel") else ""
        href = tag.get("href")
        if href and ("preload" in rel or "video" in rel or "audio" in rel):
            add(href)

    def walk_json(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"contenturl", "embedurl"} and isinstance(child, str):
                    add(child)
                else:
                    walk_json(child)
        elif isinstance(value, list):
            for child in value:
                walk_json(child)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            walk_json(json.loads(script.string or script.get_text() or "null"))
        except (ValueError, TypeError):
            continue

    decoded = html_module.unescape(html_text or "")
    decoded = (
        decoded.replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\/", "/")
    )
    for match in ANY_URL_RE.findall(decoded):
        add(match)
    for match in RELATIVE_MEDIA_RE.findall(decoded):
        add(match)

    deduped: dict[str, dict[str, Any]] = {}
    for item in candidates:
        current = deduped.get(item["url"])
        if current is None:
            deduped[item["url"]] = item
        elif not current.get("thumbnail") and item.get("thumbnail"):
            current["thumbnail"] = item["thumbnail"]

    if not title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(" ", strip=True)
        elif soup.title and soup.title.string:
            title = soup.title.string
    if not thumbnail:
        image = soup.find("img")
        if image and image.get("src"):
            thumbnail = image.get("src")

    return list(deduped.values())[:200], {"title": title.strip(), "thumbnail": thumbnail.strip()}

class _YdlLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        return None

    def warning(self, msg: str) -> None:
        if msg:
            self.messages.append(_short(msg, 220))

    def error(self, msg: str) -> None:
        if msg:
            self.messages.append(_short(msg, 220))


def _ytdlp_extract(
    url: str,
    cookie: str | None,
    referer: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], str]:
    warnings: list[str] = []
    meta: dict[str, Any] = {}
    try:
        import yt_dlp
    except Exception as exc:
        return [], meta, [f"yt-dlp 不可用：{_short(exc)}"], "error"

    logger = _YdlLogger()
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "retries": 2,
        "logger": logger,
        "no_color": True,
    }
    if headers:
        opts["http_headers"] = headers

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        warnings.extend(logger.messages[-2:])
        warnings.append(f"站点解析未命中：{_short(exc)}")
        return [], meta, warnings, "error"

    if not info:
        return [], meta, ["站点解析没有返回数据"], "error"

    if info.get("_type") == "playlist" or info.get("entries"):
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if entries:
            info = entries[0]
            warnings.append("检测到列表页面，仅解析第一个条目")

    webpage_url = info.get("webpage_url") or url
    title = info.get("title") or info.get("fulltitle") or ""
    thumbnail = info.get("thumbnail")
    if isinstance(thumbnail, list):
        thumbnail = next((item for item in thumbnail if isinstance(item, str)), None)
    duration = info.get("duration")
    extractor = info.get("extractor_key") or info.get("extractor") or "站点解析"
    meta = {
        "title": title or "",
        "thumbnail": thumbnail or "",
        "final_url": webpage_url,
        "site": extractor,
    }

    formats = info.get("formats") or []
    heights: set[int] = set()
    has_audio = False
    for fmt in formats:
        height = fmt.get("height")
        if isinstance(height, int) and 240 <= height <= 4320:
            heights.add(height)
        if fmt.get("acodec") not in (None, "none"):
            has_audio = True

    items: list[dict[str, Any]] = []
    is_live = bool(info.get("is_live"))
    items.append(
        _make_item(
            kind="video",
            url=webpage_url,
            title=title,
            mode="job",
            source="站点解析",
            ext="mp4",
            quality="直播流" if is_live else "最佳画质",
            duration=duration,
            thumbnail=thumbnail,
            selector="bestvideo*+bestaudio/best",
            referer=webpage_url,
            note="直播流可能无法完整下载" if is_live else None,
        )
    )

    chosen_heights = sorted(
        {height for height in heights if height in {360, 480, 720, 1080, 1440, 2160}},
        reverse=True,
    )[:4]
    for height in chosen_heights:
        items.append(
            _make_item(
                kind="video",
                url=webpage_url,
                title=title,
                mode="job",
                source="站点解析",
                ext="mp4",
                quality=f"{height}P",
                height=height,
                duration=duration,
                thumbnail=thumbnail,
                selector=f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                referer=webpage_url,
            )
        )

    if has_audio or not formats:
        items.append(
            _make_item(
                kind="audio",
                url=webpage_url,
                title=title,
                mode="job",
                source="站点解析",
                ext="mp3",
                quality="仅音频 · 192kbps",
                duration=duration,
                thumbnail=thumbnail,
                selector="bestaudio/best",
                referer=webpage_url,
                note="下载后自动转为 MP3",
            )
        )

    warnings.extend(logger.messages[-3:])
    return items, meta, warnings, "ok"

async def _fetch_text(
    url: str,
    cookie: str | None,
    referer: str | None,
) -> tuple[dict[str, Any], list[str]]:
    headers = {
        "User-Agent": config.MOBILE_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer

    warnings: list[str] = []
    current = url
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers=headers,
    ) as client:
        for _ in range(5):
            current = await ensure_safe_url(current)
            async with client.stream("GET", current, headers=headers) as resp:
                content_type = (resp.headers.get("content-type") or "").lower()
                location = resp.headers.get("location")
                if resp.status_code in {301, 302, 303, 307, 308} and location:
                    current = urljoin(current, location)
                    continue

                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    remaining = _MAX_HTML_BYTES - total
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        total += remaining
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)

                body = b"".join(chunks)
                text = ""
                if not content_type.startswith(("video/", "audio/")) and "mpegurl" not in content_type:
                    encoding = resp.encoding or "utf-8"
                    try:
                        text = body.decode(encoding, errors="replace")
                    except LookupError:
                        text = body.decode("utf-8", errors="replace")
                if truncated:
                    warnings.append("页面内容较大，已截断后解析")
                if resp.status_code >= 400:
                    warnings.append(f"网页返回 HTTP {resp.status_code}")
                return {
                    "url": current,
                    "status": resp.status_code,
                    "content_type": content_type,
                    "text": text,
                }, warnings

    raise RuntimeError("重定向次数过多")


async def _extract_http(
    url: str,
    cookie: str | None,
    referer: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], str]:
    try:
        fetched, warnings = await _fetch_text(url, cookie, referer)
    except Exception as exc:
        return [], {}, [f"网页源码解析失败：{_short(exc)}"], "error"

    base_url = fetched["url"]
    content_type = fetched["content_type"]
    kind = _kind_from_url(base_url, content_type)
    if kind:
        item = _make_item(
            kind=kind,
            url=base_url,
            title=urlsplit(base_url).path.rsplit("/", 1)[-1] or "媒体文件",
            mode="job" if kind == "hls" else "proxy",
            source="直接链接",
            referer=referer or base_url,
            note="HLS 流需要合并为 MP4" if kind == "hls" else None,
        )
        return [item], {"title": "", "thumbnail": "", "final_url": base_url}, warnings, "ok"

    if fetched["status"] >= 400:
        return [], {"final_url": base_url}, warnings, "error"

    items, meta = _extract_from_html(fetched["text"], base_url)
    page_thumbnail = (
        _normalize_candidate(meta.get("thumbnail"), base_url)
        if meta.get("thumbnail")
        else None
    )
    built: list[dict[str, Any]] = []
    for item in items:
        kind = item["kind"]
        item_thumbnail = _normalize_candidate(item.get("thumbnail"), base_url) if item.get("thumbnail") else None
        built.append(
            _make_item(
                kind=kind,
                url=item["url"],
                title=meta.get("title") or "",
                mode="job" if kind == "hls" else "proxy",
                source="网页源码",
                thumbnail=item_thumbnail or page_thumbnail,
                referer=base_url,
                note="HLS 流需要合并为 MP4" if kind == "hls" else None,
            )
        )
    meta["thumbnail"] = page_thumbnail or ""
    meta["final_url"] = base_url
    return built, meta, warnings, "ok"

async def _extract_browser(
    url: str,
    cookie: str | None,
    referer: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str], str]:
    if not config.ENABLE_PLAYWRIGHT:
        return [], {}, [], "disabled"

    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except Exception as exc:
        return [], {}, [f"浏览器嗅探不可用：{_short(exc)}"], "error"

    warnings: list[str] = []
    meta: dict[str, Any] = {}
    found: dict[str, dict[str, Any]] = {}
    stop_media = {"value": False}

    def add_found(media_url: str, content_type: str = "") -> None:
        normalized = _normalize_candidate(media_url, url)
        if not normalized or is_junk_media_url(normalized):
            return
        kind = _kind_from_url(normalized, content_type)
        if not kind:
            return
        found.setdefault(normalized, {"url": normalized, "kind": kind})

    try:
        async with _BROWSER_SEMAPHORE:
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

                    def on_request(request: Any) -> None:
                        if request.resource_type == "media" or _kind_from_url(request.url):
                            add_found(request.url)

                    def on_response(response: Any) -> None:
                        content_type = (response.headers or {}).get("content-type", "")
                        if content_type.startswith(("video/", "audio/")) or "mpegurl" in content_type:
                            add_found(response.url, content_type)

                    page.on("request", on_request)
                    page.on("response", on_response)

                    async def route_handler(route: Any) -> None:
                        try:
                            request = route.request
                            request_url = request.url
                            if request_url.startswith(("blob:", "data:")):
                                await route.continue_()
                                return
                            if request.resource_type in {"image", "font"}:
                                await route.abort()
                                return
                            host = urlsplit(request_url).hostname or ""
                            if (
                                not config.ALLOW_PRIVATE_NETWORK
                                and host
                                and _is_obviously_private_host(host)
                            ):
                                await route.abort()
                                return
                            if request.resource_type == "media" or _kind_from_url(request_url):
                                add_found(request_url)
                                if stop_media["value"]:
                                    await route.abort()
                                    return
                            await route.continue_()
                        except Exception:
                            try:
                                await route.continue_()
                            except Exception:
                                pass

                    await page.route("**/*", route_handler)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    except PlaywrightError as exc:
                        warnings.append(f"页面加载提示：{_short(exc)}")
                    except Exception as exc:
                        warnings.append(f"页面加载失败：{_short(exc)}")

                    await page.wait_for_timeout(2500)
                    try:
                        await page.evaluate("window.scrollBy(0, 900)")
                    except Exception:
                        pass
                    await page.wait_for_timeout(1500)
                    try:
                        await page.evaluate(
                            """() => {
                                document.querySelectorAll('video,audio').forEach((node) => {
                                    try {
                                        node.muted = true;
                                        const result = node.play();
                                        if (result && result.catch) result.catch(() => {});
                                    } catch (error) {}
                                });
                            }"""
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(2500)
                    stop_media["value"] = True
                    try:
                        await page.evaluate(
                            """() => document.querySelectorAll('video,audio').forEach((node) => {
                                try { node.pause(); } catch (error) {}
                            })"""
                        )
                    except Exception:
                        pass

                    try:
                        meta["title"] = await page.title()
                    except Exception:
                        meta["title"] = ""
                    meta["final_url"] = page.url

                    try:
                        dom_sources = await page.evaluate(
                            """() => Array.from(document.querySelectorAll('video,audio,source')).map((node) => ({
                                src: node.currentSrc || node.src || node.getAttribute('data-src') || '',
                                poster: node.poster || (node.parentElement && node.parentElement.poster) || ''
                            }))"""
                        )
                        for entry in dom_sources or []:
                            src = entry.get("src") if isinstance(entry, dict) else None
                            if not src:
                                continue
                            add_found(src)
                            if src in found and entry.get("poster"):
                                found[src]["thumbnail"] = entry["poster"]
                    except Exception:
                        pass

                    try:
                        content = await page.content()
                        html_items, html_meta = _extract_from_html(content, page.url)
                        for item in html_items:
                            add_found(item["url"], "")
                            if item["url"] in found and item.get("thumbnail"):
                                found[item["url"]]["thumbnail"] = item["thumbnail"]
                        if html_meta.get("title") and not meta.get("title"):
                            meta["title"] = html_meta["title"]
                        if html_meta.get("thumbnail"):
                            meta["thumbnail"] = (
                                _normalize_candidate(html_meta["thumbnail"], page.url)
                                or html_meta["thumbnail"]
                            )
                    except Exception as exc:
                        warnings.append(f"浏览器页面解析提示：{_short(exc)}")

                    await context.close()
                finally:
                    await browser.close()
    except PlaywrightError as exc:
        message = _short(exc)
        if "Executable doesn't exist" in message or "playwright install" in message.lower():
            message = "Chromium 未安装，请运行 python -m playwright install chromium"
        return [], meta, [f"浏览器嗅探失败：{message}"], "error"
    except Exception as exc:
        return [], meta, [f"浏览器嗅探失败：{_short(exc)}"], "error"

    items = [
        _make_item(
            kind=entry["kind"],
            url=entry["url"],
            title=meta.get("title") or "",
            mode="job" if entry["kind"] == "hls" else "proxy",
            source="浏览器嗅探",
            thumbnail=entry.get("thumbnail") or meta.get("thumbnail"),
            referer=meta.get("final_url") or url,
            note="HLS 流需要合并为 MP4" if entry["kind"] == "hls" else None,
        )
        for entry in found.values()
    ]
    return items, meta, warnings, "ok"

def _merge_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        key = (item["url"], item["kind"], item.get("format_selector") or "")
        current = merged.get(key)
        if current is None:
            merged[key] = dict(item)
            continue
        for field in (
            "title",
            "quality",
            "width",
            "height",
            "duration",
            "size",
            "thumbnail",
            "format_selector",
            "referer",
            "note",
        ):
            if not current.get(field) and item.get(field):
                current[field] = item[field]
        sources = {part for part in str(current.get("source", "")).split("+") if part}
        sources.update(part for part in str(item.get("source", "")).split("+") if part)
        current["source"] = "+".join(sorted(sources))
    return list(merged.values())


async def _enrich_direct_items(
    items: list[dict[str, Any]],
    cookie: str | None,
    referer: str | None,
) -> None:
    targets = [
        item
        for item in items
        if item.get("mode") == "proxy"
        and not str(item.get("source", "")).startswith(("抖音解析", "TikTok解析"))
    ]
    if not targets:
        return

    headers = {"User-Agent": config.MOBILE_USER_AGENT}
    if cookie:
        headers["Cookie"] = cookie

    async def inspect(item: dict[str, Any]) -> None:
        try:
            target = await ensure_safe_url(item["url"])
            item_headers = dict(headers)
            if item.get("referer") or referer:
                item_headers["Referer"] = item.get("referer") or referer
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(6.0, connect=4.0),
                follow_redirects=False,
                headers=item_headers,
            ) as client:
                response = await client.head(target, headers=item_headers)
                if response.status_code >= 400:
                    if response.status_code in {404, 410}:
                        item["_dead"] = True
                    return
                content_type = (response.headers.get("content-type") or "").lower()
                length = response.headers.get("content-length")
                if length and length.isdigit():
                    item["size"] = int(length)
                detected = _kind_from_url(target, content_type)
                if detected in {"video", "audio", "hls"}:
                    item["kind"] = detected
        except Exception:
            return

    try:
        await asyncio.wait_for(
            asyncio.gather(*(inspect(item) for item in targets), return_exceptions=True),
            timeout=9,
        )
    except asyncio.TimeoutError:
        return

def _apply_download_mode(items: list[dict[str, Any]], cookie: str | None) -> None:
    for item in items:
        source = str(item.get("source") or "")
        if source.startswith(("抖音解析", "TikTok解析")):
            continue
        if item.get("source") == "站点解析" or "站点解析" in source:
            item["mode"] = "job"
            item["format_selector"] = item.get("format_selector") or "bestvideo*+bestaudio/best"
        elif item["kind"] == "hls":
            item["mode"] = "job"
            item["format_selector"] = item.get("format_selector") or "best"
        elif cookie:
            item["mode"] = "job"
            item["format_selector"] = item.get("format_selector") or "best"
        else:
            item["mode"] = "proxy"


def _sort_key(item: dict[str, Any]) -> tuple[int, int, int, int, str]:
    order = {"video": 0, "hls": 1, "audio": 2, "live": 3}
    mode_order = 0 if item.get("mode") == "job" else 1
    height = item.get("height") or 0
    provider_priority = (
        0
        if str(item.get("source", "")).startswith(("抖音解析", "TikTok解析"))
        else 1
    )
    return (
        provider_priority,
        order.get(item["kind"], 9),
        mode_order,
        -height,
        item["url"],
    )


def _provider_items(result: Any) -> list[dict[str, Any]]:
    source = f"{result.label}解析"
    items: list[dict[str, Any]] = []
    for entry in result.entries:
        items.append(
            _make_item(
                kind=entry.kind,
                url=entry.url,
                title=entry.title or result.title,
                mode=entry.mode,
                source=source,
                ext=entry.ext,
                quality=entry.quality,
                width=entry.width,
                height=entry.height,
                duration=entry.duration,
                size=entry.size,
                thumbnail=entry.thumbnail or result.thumbnail,
                selector=entry.selector,
                referer=entry.referer or result.final_url,
                note=entry.note,
            )
        )
    return items


async def sniff(
    url: str,
    cookie: str | None = None,
    referer: str | None = None,
) -> ParseResponse:
    started = time.perf_counter()
    normalized_input = extract_input_url(url)
    safe_url = await ensure_safe_url(normalized_input or url)
    cookie = sanitize_header_value(cookie)
    referer = sanitize_header_value(referer)
    if referer:
        referer = await ensure_safe_url(referer)

    all_items: list[dict[str, Any]] = []
    warnings: list[str] = []
    engines: dict[str, str] = {}
    titles: list[str] = []
    thumbnails: list[str] = []
    final_urls: list[str] = []

    platform_result = None
    provider_error = ""
    try:
        platform_result = await asyncio.wait_for(
            sniff_platform(safe_url, cookie, referer),
            timeout=max(10.0, config.SNIFF_TIMEOUT_SECONDS - 5),
        )
    except asyncio.TimeoutError:
        provider_error = "平台解析超时，已切换通用嗅探"
    except Exception as exc:
        provider_error = f"平台解析失败：{_short(exc)}"

    if platform_result is not None and platform_result.status == "ok" and platform_result.entries:
        label = f"{platform_result.label}解析"
        engines[label] = "ok"
        all_items.extend(_provider_items(platform_result))
        warnings.extend(platform_result.warnings or [])
        if platform_result.title:
            titles.append(platform_result.title)
        if platform_result.thumbnail:
            thumbnails.append(platform_result.thumbnail)
        if platform_result.final_url:
            final_urls.append(platform_result.final_url)
    else:
        if platform_result is not None:
            engines[f"{platform_result.label}解析"] = platform_result.status
            warnings.extend(platform_result.warnings or [])
        if provider_error:
            warnings.append(provider_error)

        tasks = [
            asyncio.to_thread(_ytdlp_extract, safe_url, cookie, referer),
            _extract_http(safe_url, cookie, referer),
            _extract_browser(safe_url, cookie, referer),
        ]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=config.SNIFF_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            results = [asyncio.TimeoutError()] * len(tasks)

        names = ["yt-dlp", "HTML", "Playwright"]
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                engines[name] = "error"
                warnings.append(f"{name} 执行失败：{_short(result)}")
                continue
            items, meta, engine_warnings, status = result
            engines[name] = status
            warnings.extend(engine_warnings or [])
            all_items.extend(items or [])
            if meta.get("title"):
                titles.append(str(meta["title"]))
            if meta.get("thumbnail"):
                thumbnails.append(str(meta["thumbnail"]))
            if meta.get("final_url"):
                final_urls.append(str(meta["final_url"]))

    items = _merge_items(all_items)
    await _enrich_direct_items(items, cookie, referer)
    items = [item for item in items if not item.get("_dead")]
    _apply_download_mode(items, cookie)
    proxy_keys = {(item["url"], item["kind"]) for item in items if item.get("mode") == "proxy"}
    if proxy_keys:
        items = [
            item
            for item in items
            if not (item.get("mode") == "job" and (item["url"], item["kind"]) in proxy_keys)
        ]
    items.sort(key=_sort_key)
    items = items[: config.MAX_MEDIA_ITEMS]

    if not items:
        warnings.append(
            "没有发现可直接访问的媒体。可能是页面需要登录、存在 DRM 加密，或媒体地址由纯 JavaScript 动态生成。"
        )

    deduped_warnings: list[str] = []
    seen_warnings: set[str] = set()
    for warning in warnings:
        clean = _short(warning)
        if clean and clean not in seen_warnings:
            seen_warnings.add(clean)
            deduped_warnings.append(clean)

    media_items = [
        MediaItem(**{key: value for key, value in item.items() if key in MediaItem.model_fields})
        for item in items
    ]
    return ParseResponse(
        ok=bool(media_items),
        url=url,
        final_url=final_urls[0] if final_urls else safe_url,
        title=titles[0] if titles else None,
        thumbnail=thumbnails[0] if thumbnails else None,
        items=media_items,
        warnings=deduped_warnings[:12],
        engines=engines,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
