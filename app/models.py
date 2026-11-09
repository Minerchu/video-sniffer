from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MediaKind = Literal["video", "audio", "hls", "live"]
DownloadMode = Literal["proxy", "job"]


class ParseRequest(BaseModel):
    url: str = Field(min_length=4, max_length=8192)
    cookie: str | None = Field(default=None, max_length=65536)
    referer: str | None = Field(default=None, max_length=8192)


class MediaItem(BaseModel):
    id: str
    kind: MediaKind
    title: str
    url: str
    mode: DownloadMode
    format: str
    quality: str | None = None
    ext: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    size: int | None = None
    thumbnail: str | None = None
    source: str
    format_selector: str | None = None
    referer: str | None = None
    note: str | None = None


class ParseResponse(BaseModel):
    ok: bool
    url: str
    final_url: str | None = None
    title: str | None = None
    thumbnail: str | None = None
    items: list[MediaItem]
    warnings: list[str]
    engines: dict[str, str]
    elapsed_ms: int


class JobRequest(BaseModel):
    url: str = Field(min_length=4, max_length=8192)
    title: str | None = Field(default=None, max_length=300)
    format_selector: str = Field(default="best", max_length=160)
    kind: Literal["video", "audio"] = "video"
    referer: str | None = Field(default=None, max_length=8192)
    cookie: str | None = Field(default=None, max_length=65536)


class JobStatus(BaseModel):
    id: str
    status: Literal["queued", "running", "done", "error", "cancelled"]
    progress: float = 0
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    filename: str | None = None
    title: str | None = None
    error: str | None = None
    created_at: float