from __future__ import annotations

import os
import shutil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMP_ROOT = Path(os.getenv("TEMP_DIR", str(BASE_DIR / ".tmp"))).resolve()
JOBS_DIR = TEMP_ROOT / "jobs"

APP_NAME = "拾影"
APP_VERSION = "0.1.0"

MAX_MEDIA_ITEMS = int(os.getenv("MAX_MEDIA_ITEMS", "40"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "2048"))
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
SNIFF_TIMEOUT_SECONDS = float(os.getenv("SNIFF_TIMEOUT_SECONDS", "45"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "7200"))

ENABLE_PLAYWRIGHT = os.getenv("ENABLE_PLAYWRIGHT", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
ALLOW_PRIVATE_NETWORK = os.getenv("ALLOW_PRIVATE_NETWORK", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 "
    "Mobile/15E148 Safari/604.1"
)


def find_ffmpeg() -> str | None:
    configured = os.getenv("FFMPEG_PATH")
    if configured and Path(configured).exists():
        return str(Path(configured).resolve())

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(bundled).exists():
            return bundled
    except Exception:
        pass

    candidates: list[Path] = []
    for root in (
        Path(os.getenv("LOCALAPPDATA", "")),
        Path(os.getenv("PROGRAMFILES", "")),
        Path(os.getenv("PROGRAMFILES(X86)", "")),
    ):
        if not root or not root.exists():
            continue
        candidates.extend(root.glob("JianyingPro/Apps/*/ffmpeg.exe"))
        candidates.extend(root.glob("**/ffmpeg.exe"))

    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    return None


FFMPEG_PATH = find_ffmpeg()


def ensure_directories() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)