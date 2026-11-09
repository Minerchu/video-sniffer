from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

from . import config


class UnsafeURLError(ValueError):
    pass


_PRIVATE_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".lan",
    ".localhost",
    ".home",
)
_PRIVATE_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "metadata",
    "metadata.google.internal",
}
_PROXY_EGRESS_RANGE = ipaddress.ip_network("198.18.0.0/15")
_FILENAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SELECTOR_BAD = re.compile(r"[^a-zA-Z0-9_+\-*./<>=?&:,\[\] ]")


def _is_public_ip(value: str, *, allow_proxy_egress: bool = False) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if allow_proxy_egress and ip.version == 4 and ip in _PROXY_EGRESS_RANGE:
        return True
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _is_obviously_private_host(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in _PRIVATE_HOSTS:
        return True
    if any(host.endswith(suffix) for suffix in _PRIVATE_HOST_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return not _is_public_ip(host)


async def ensure_safe_url(raw_url: str) -> str:
    if not raw_url or len(raw_url) > 8192:
        raise UnsafeURLError("URL 为空或过长")
    raw_url = raw_url.strip()
    parts = urlsplit(raw_url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError("只支持 http/https 链接")
    if not parts.hostname:
        raise UnsafeURLError("链接缺少有效域名")
    if parts.username or parts.password:
        raise UnsafeURLError("链接不能包含用户名或密码")

    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("链接端口无效") from exc

    host = parts.hostname.strip().lower()
    if not config.ALLOW_PRIVATE_NETWORK and _is_obviously_private_host(host):
        raise UnsafeURLError("出于安全考虑，不能访问内网或本机地址")

    if not config.ALLOW_PRIVATE_NETWORK:
        try:
            literal = ipaddress.ip_address(host)
            if not _is_public_ip(str(literal)):
                raise UnsafeURLError("出于安全考虑，不能访问内网或保留地址")
        except ValueError:
            resolved_port = port or (443 if parts.scheme.lower() == "https" else 80)
            try:
                infos = await asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    resolved_port,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                )
            except OSError as exc:
                raise UnsafeURLError(f"域名解析失败：{host}") from exc
            addresses = {info[4][0] for info in infos}
            if not addresses or not all(
                _is_public_ip(addr, allow_proxy_egress=True) for addr in addresses
            ):
                raise UnsafeURLError("域名解析到了内网或保留地址")

    return urlunsplit(
        (parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, "")
    )


def sanitize_header_value(value: str | None, *, max_length: int = 65536) -> str | None:
    if not value:
        return None
    if "\r" in value or "\n" in value:
        raise ValueError("请求头中不能包含换行符")
    value = value.strip()
    if not value:
        return None
    return value[:max_length]


def sanitize_filename(value: str | None, fallback: str = "media") -> str:
    value = (value or "").strip()
    value = _FILENAME_BAD.sub("_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    if not value:
        value = fallback
    stem, dot, suffix = value.rpartition(".")
    if dot and len(suffix) <= 8:
        stem = stem[:100].rstrip(" .")
        value = f"{stem or fallback}.{suffix.lower()}"
    else:
        value = value[:120].rstrip(" .") or fallback
    return value


def sanitize_format_selector(value: str | None) -> str:
    value = (value or "best").strip()
    if not value or len(value) > 160 or _SELECTOR_BAD.search(value):
        raise ValueError("无效的下载格式")
    return value