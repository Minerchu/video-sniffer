from __future__ import annotations

import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config
from .security import sanitize_filename


_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shipin-download")


def _short(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _update(job_id: str, **changes: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(changes)


def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "status": job["status"],
        "progress": round(float(job.get("progress") or 0), 4),
        "downloaded_bytes": int(job["downloaded_bytes"]) if job.get("downloaded_bytes") else None,
        "total_bytes": int(job["total_bytes"]) if job.get("total_bytes") else None,
        "filename": job.get("filename"),
        "title": job.get("title"),
        "error": job.get("error"),
        "created_at": job["created_at"],
    }


def create_job(
    *,
    url: str,
    title: str | None,
    format_selector: str,
    kind: str,
    referer: str | None,
    cookie: str | None,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job_dir = config.JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job: dict[str, Any] = {
        "id": job_id,
        "status": "queued",
        "progress": 0.0,
        "downloaded_bytes": None,
        "total_bytes": None,
        "filename": None,
        "title": title,
        "error": None,
        "created_at": time.time(),
        "dir": job_dir,
        "url": url,
        "format_selector": format_selector,
        "kind": kind,
        "referer": referer,
        "cookie": cookie,
    }
    with _lock:
        _jobs[job_id] = job
    _executor.submit(_run_job, job_id)
    return _snapshot(job)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job else None


def get_job_file(job_id: str) -> tuple[Path, str] | None:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job.get("status") != "done":
            return None
        path = job.get("file")
        filename = job.get("filename")
    if not path or not filename:
        return None
    file_path = Path(path)
    if file_path.is_file():
        return file_path, filename
    return None


def delete_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.pop(job_id, None)
    if not job:
        return False
    job_dir = job.get("dir")
    if job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)
    return True


def _run_job(job_id: str) -> None:
    with _lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return

    _update(job_id, status="running", progress=0.0)

    def progress_hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        downloaded = int(data.get("downloaded_bytes") or 0)
        total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
        if downloaded and downloaded > config.MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"文件超过 {config.MAX_DOWNLOAD_MB} MB 下载上限")
        if status == "downloading":
            progress = 0.0
            if total:
                progress = min(0.98, max(0.0, downloaded / float(total)))
            _update(
                job_id,
                progress=progress,
                downloaded_bytes=downloaded or None,
                total_bytes=total or None,
            )
        elif status == "finished":
            _update(job_id, progress=0.99, downloaded_bytes=downloaded or None, total_bytes=total or None)

    headers: dict[str, str] = {}
    if job.get("referer"):
        headers["Referer"] = job["referer"]
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]

    options: dict[str, Any] = {
        "outtmpl": str(Path(job["dir"]) / "media.%(ext)s"),
        "format": job["format_selector"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "max_filesize": config.MAX_DOWNLOAD_BYTES,
        "windowsfilenames": True,
        "progress_hooks": [progress_hook],
        "prefer_ffmpeg": True,
    }
    if headers:
        options["http_headers"] = headers
    if config.FFMPEG_PATH:
        options["ffmpeg_location"] = config.FFMPEG_PATH

    if job["kind"] == "audio":
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        options["postprocessors"] = [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        ]

    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([job["url"]])

        files = [
            item
            for item in Path(job["dir"]).iterdir()
            if item.is_file() and item.suffix.lower() not in {".part", ".ytdl", ".json"}
        ]
        if not files:
            raise RuntimeError("下载完成但没有找到输出文件")
        output = max(files, key=lambda item: item.stat().st_size)
        title = sanitize_filename(
            str(job.get("title") or output.stem), fallback="media"
        )
        stem = Path(title).stem[:90].rstrip(" .") or "media"
        target = output.with_name(f"{stem}{output.suffix.lower()}")
        if target != output:
            try:
                if target.exists():
                    target.unlink()
                output.replace(target)
            except OSError:
                target = output
            output = target
        filename = sanitize_filename(output.name, fallback=f"media{output.suffix}")
        size = output.stat().st_size
        if size > config.MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"文件超过 {config.MAX_DOWNLOAD_MB} MB 下载上限")
        _update(
            job_id,
            status="done",
            progress=1.0,
            filename=filename,
            file=str(output.resolve()),
            downloaded_bytes=size,
            total_bytes=size,
        )
    except Exception as exc:
        _update(job_id, status="error", error=_short(exc), progress=0.0)


def cleanup_old_jobs() -> int:
    cutoff = time.time() - config.JOB_TTL_SECONDS
    removed = 0
    with _lock:
        stale = [
            job_id
            for job_id, job in _jobs.items()
            if job.get("created_at", 0) < cutoff and job.get("status") in {"done", "error", "cancelled"}
        ]
    for job_id in stale:
        if delete_job(job_id):
            removed += 1
    return removed
