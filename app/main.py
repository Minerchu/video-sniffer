from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, downloader
from .models import JobRequest, JobStatus, ParseRequest, ParseResponse
from .security import (
    UnsafeURLError,
    ensure_safe_url,
    sanitize_filename,
    sanitize_format_selector,
    sanitize_header_value,
)
from .sniffer import sniff


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(600)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(downloader.cleanup_old_jobs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_directories()
    with contextlib.suppress(Exception):
        await asyncio.to_thread(downloader.cleanup_old_jobs)
    task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title=f"{config.APP_NAME} · 视频嗅探下载",
    version=config.APP_VERSION,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https:; media-src 'self' https:; "
        "connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/quick", include_in_schema=False)
async def quick() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "quick.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict:
    try:
        import yt_dlp

        ytdlp_version = yt_dlp.version.__version__
    except Exception:
        ytdlp_version = None
    return {
        "ok": True,
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "ffmpeg": bool(config.FFMPEG_PATH),
        "ffmpeg_path": config.FFMPEG_PATH,
        "playwright": config.ENABLE_PLAYWRIGHT,
        "yt_dlp": ytdlp_version,
        "max_download_mb": config.MAX_DOWNLOAD_MB,
    }


@app.post("/api/parse", response_model=ParseResponse)
async def parse_media(payload: ParseRequest) -> ParseResponse:
    try:
        return await sniff(payload.url, payload.cookie, payload.referer)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"解析失败：{exc}") from exc


@app.post("/api/jobs", response_model=JobStatus)
async def create_download_job(payload: JobRequest) -> JobStatus:
    try:
        safe_url = await ensure_safe_url(payload.url)
        referer = sanitize_header_value(payload.referer)
        if referer:
            referer = await ensure_safe_url(referer)
        selector = sanitize_format_selector(payload.format_selector)
        cookie = sanitize_header_value(payload.cookie)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = downloader.create_job(
        url=safe_url,
        title=payload.title,
        format_selector=selector,
        kind=payload.kind,
        referer=referer,
        cookie=cookie,
    )
    return JobStatus(**job)


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
async def job_status(job_id: str) -> JobStatus:
    job = downloader.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="下载任务不存在或已过期")
    return JobStatus(**job)


@app.get("/api/jobs/{job_id}/file")
async def job_file(job_id: str) -> FileResponse:
    result = downloader.get_job_file(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="文件尚未生成或任务已过期")
    path, filename = result
    return FileResponse(
        path,
        filename=filename,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str) -> dict:
    if not downloader.delete_job(job_id):
        raise HTTPException(status_code=404, detail="下载任务不存在")
    return {"ok": True}


async def _open_safe_response(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    current = url
    for _ in range(6):
        current = await ensure_safe_url(current)
        request = client.build_request("GET", current, headers=headers)
        response = await client.send(request, stream=True)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            await response.aclose()
            if location:
                current = urljoin(current, location)
                continue
        return response
    raise HTTPException(status_code=400, detail="重定向次数过多")


@app.get("/api/download")
async def download_proxy(
    request: Request,
    url: str = Query(min_length=4, max_length=8192),
    filename: str | None = Query(default=None, max_length=300),
    referer: str | None = Query(default=None, max_length=8192),
) -> StreamingResponse:
    try:
        safe_url = await ensure_safe_url(url)
        safe_referer = sanitize_header_value(referer)
        if safe_referer:
            safe_referer = await ensure_safe_url(safe_referer)
    except UnsafeURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    headers = {
        "User-Agent": config.MOBILE_USER_AGENT,
        "Accept": "*/*",
    }
    if safe_referer:
        headers["Referer"] = safe_referer
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=False,
    )
    try:
        response = await _open_safe_response(client, safe_url, headers)
    except Exception:
        await client.aclose()
        raise

    if response.status_code >= 400:
        status = response.status_code
        await response.aclose()
        await client.aclose()
        raise HTTPException(status_code=status, detail=f"源站返回 HTTP {status}")

    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > config.MAX_DOWNLOAD_BYTES:
        await response.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {config.MAX_DOWNLOAD_MB} MB 下载上限",
        )

    if not filename:
        filename = urlsplit(safe_url).path.rsplit("/", 1)[-1] or "media"
    filename = sanitize_filename(filename, fallback="media")
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "media"
    disposition = (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )

    out_headers = {
        "Content-Disposition": disposition,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    for header in ("content-type", "content-length", "accept-ranges", "content-range"):
        value = response.headers.get(header)
        if value:
            out_headers[header.title()] = value

    async def body():
        downloaded = 0
        try:
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > config.MAX_DOWNLOAD_BYTES:
                    break
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=response.status_code,
        headers=out_headers,
        media_type=response.headers.get("content-type") or "application/octet-stream",
    )


if config.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")