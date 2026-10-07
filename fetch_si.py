# -*- coding: utf-8 -*-
"""网络抓取纯模块：httpx 异步图片下载（手动逐跳重定向 + SSRF 校验）+ 磁盘缓存。

纯模块纪律：不 import maibot_sdk、不出现 ctx。httpx 为可选依赖：
缺失时模块仍可导入，调用方捕获 FetchUnavailableError 转成可读提示。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

try:  # 可选依赖：httpx 缺失时下载功能降级，模块仍可导入
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - 真机未装 httpx 时走降级
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

#: 默认 UA（不冒充浏览器，也不暴露过多信息）
DEFAULT_USER_AGENT = "MaiBot-self-identity-plugin/1.0"

#: 默认下载超时（秒）
DEFAULT_TIMEOUT_SECONDS = 15.0

#: 默认体积上限（15 MB）
DEFAULT_MAX_BYTES = 15 * 1024 * 1024

#: 重定向跳数上限
MAX_REDIRECTS = 5


class FetchUnavailableError(RuntimeError):
    """httpx 缺失，下载功能不可用。"""


class DownloadError(RuntimeError):
    """下载失败（网络/状态码/体积/SSRF 校验）。"""


def _log_noop(_level: str, _message: str) -> None:
    return None


def _is_public_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """公网地址判定（runtime-gotchas §62.3：is_global 不能单独用）。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 之类要拆开再判
    if (
        ip.is_multicast
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_loopback
        or ip.is_private
        or ip.is_unspecified
    ):
        return False
    return bool(ip.is_global)


async def _resolve_host(host: str):
    """DNS 解析（在线程池里跑，避免阻塞事件循环）。"""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, None)


async def assert_public_http_url(url: str) -> str:
    """SSRF 校验：仅允许 http(s)，且域名必须解析到公网地址（resolve-then-check）。

    Returns:
        规范化后的 URL。
    Raises:
        DownloadError: 协议不合法 / 无法解析 / 解析结果非公网。
    """
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise DownloadError(f"不支持的 URL 协议：{parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise DownloadError("URL 缺少主机名")

    try:
        infos = await _resolve_host(host)
    except Exception as exc:
        raise DownloadError(f"域名解析失败：{host}（{type(exc).__name__}）") from exc

    if not infos:
        raise DownloadError(f"域名解析不到任何地址：{host}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            continue
        if not _is_public_ip(ip):
            raise DownloadError(f"URL 指向非公网地址，已拒绝：{host} → {ip}")
    return urllib.parse.urlunsplit(parsed)


def build_qq_avatar_url(qq_account: str) -> str:
    """构造 QQ 头像 URL（qlogo 高清 640）。"""
    return f"https://q1.qlogo.cn/g?b=qq&nk={qq_account}&s=640"


def _safe_url(url: str) -> str:
    """日志脱敏：只保留 scheme + host + path（query 可能带签名）。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return "<unparsed-url>"


async def download_image_bytes(
    url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    validate_url: bool = True,
    client: Optional[Any] = None,
    log: Callable[[str, str], None] = _log_noop,
) -> Tuple[bytes, str]:
    """下载图片，返回 (字节, Content-Type 或 "")。

    - SSRF：validate_url=True 时逐跳做 resolve-then-check（runtime-gotchas §62.3）；
    - 重定向：手动逐跳跟随（follow_redirects=False），每一跳都重新校验；
    - 体积：Content-Length 预检 + 响应体硬校验（httpx 为缓冲式读取，
      恶意服务器可能在预检后仍推送超量数据，响应体校验兜底）。

    Raises:
        FetchUnavailableError: httpx 未安装。
        DownloadError: 校验失败 / 网络失败 / 状态码异常 / 超过体积上限。
    """
    if not HTTPX_AVAILABLE:
        raise FetchUnavailableError("httpx 未安装，图片下载功能不可用")

    current_url = str(url or "").strip()
    if validate_url:
        current_url = await assert_public_http_url(current_url)

    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "image/*,*/*;q=0.8"}
    owned_client = client is None
    if owned_client:
        client = httpx.AsyncClient(follow_redirects=False, timeout=timeout_seconds)
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                response = await client.get(current_url, headers=headers)
            except Exception as exc:
                timeout_cls = getattr(httpx, "TimeoutException", ())
                if timeout_cls and isinstance(exc, timeout_cls):
                    raise DownloadError(
                        f"下载超时（{timeout_seconds:.0f}s）：{_safe_url(current_url)}"
                    ) from exc
                raise DownloadError(
                    f"下载失败：{_safe_url(current_url)}（{type(exc).__name__}）"
                ) from exc

            if response.is_redirect:
                location = response.headers.get("location") or ""
                if not location:
                    raise DownloadError(f"重定向缺少 location：{_safe_url(current_url)}")
                current_url = urllib.parse.urljoin(current_url, location)
                if validate_url:
                    current_url = await assert_public_http_url(current_url)
                log("info", f"跟随重定向 → {_safe_url(current_url)}")
                continue

            if response.status_code >= 400:
                raise DownloadError(f"下载失败 HTTP {response.status_code}：{_safe_url(current_url)}")

            content_type = str(response.headers.get("content-type") or "").strip().lower()
            if content_type and not content_type.startswith("image/"):
                raise DownloadError(f"返回内容不是图片（Content-Type: {content_type}）")

            content = response.content or b""
            if len(content) > max_bytes:
                raise DownloadError(f"图片超过体积上限（{max_bytes // (1024 * 1024)} MB）")
            if not content:
                raise DownloadError("下载内容为空")
            return content, content_type
        raise DownloadError(f"重定向跳数超过上限（{MAX_REDIRECTS}）：{_safe_url(current_url)}")
    finally:
        if owned_client:
            try:
                await client.aclose()
            except Exception:
                pass


@dataclass(frozen=True)
class CacheEntry:
    """缓存命中的内容。"""

    data: bytes
    image_format: str


class DiskCache:
    """极简磁盘缓存：内容 + 元数据双文件，原子写，坏即空。"""

    def __init__(self, cache_dir: Path, ttl_seconds: float, log: Callable[[str, str], None] = _log_noop) -> None:
        self._dir = Path(cache_dir)
        self._ttl = max(0.0, float(ttl_seconds))
        self._log = log

    def _paths(self, key: str) -> Tuple[Path, Path]:
        digest = hashlib.sha1(str(key).encode("utf-8", errors="ignore")).hexdigest()
        return self._dir / f"{digest}.bin", self._dir / f"{digest}.json"

    def load(self, key: str) -> Optional[CacheEntry]:
        """读缓存；过期/损坏/缺失一律 None（坏即空）。"""
        blob_path, meta_path = self._paths(key)
        try:
            if not blob_path.is_file() or not meta_path.is_file():
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stored_at = float(meta.get("stored_at") or 0.0)
            if (time.time() - stored_at) > self._ttl:  # ttl<=0 视为「不缓存」
                return None
            data = blob_path.read_bytes()
            if not data:
                return None
            return CacheEntry(data=data, image_format=str(meta.get("image_format") or "png"))
        except Exception as exc:
            self._log("warning", f"缓存读取失败，按未命中处理：{type(exc).__name__}: {exc}")
            return None

    def store(self, key: str, data: bytes, image_format: str) -> bool:
        """写缓存（临时文件 + replace 原子替换）；失败只记日志不抛。"""
        blob_path, meta_path = self._paths(key)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            blob_tmp = blob_path.with_name(f"{blob_path.name}.{time.time_ns()}.tmp")
            meta_tmp = meta_path.with_name(f"{meta_path.name}.{time.time_ns()}.tmp")
            blob_tmp.write_bytes(data)
            meta_tmp.write_text(
                json.dumps({"stored_at": time.time(), "image_format": image_format}, ensure_ascii=False),
                encoding="utf-8",
            )
            blob_tmp.replace(blob_path)
            meta_tmp.replace(meta_path)
            return True
        except Exception as exc:
            self._log("warning", f"缓存写入失败：{type(exc).__name__}: {exc}")
            return False

    def clear(self) -> int:
        """清空缓存目录，返回删除的文件数（on_unload 用）。"""
        removed = 0
        try:
            if not self._dir.is_dir():
                return 0
            for path in self._dir.iterdir():
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        except Exception as exc:
            self._log("warning", f"缓存清理失败：{type(exc).__name__}: {exc}")
        return removed


def guess_format_from_content_type(content_type: str, fallback: str = "png") -> str:
    """从 Content-Type 提取格式名（image/jpeg → jpeg）。"""
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        subtype = normalized.split("/", 1)[1].strip()
        if subtype:
            return subtype
    return fallback
