# -*- coding: utf-8 -*-
"""图片读写纯模块：格式嗅探 / Base64 编解码 / MIME 推断 / 本地文件读取。

纯模块纪律：不 import maibot_sdk、不出现 ctx——全部能力经参数注入，
可脱机单测。允许使用标准库与 Pillow（可选依赖，缺失时功能降级）。
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Optional, Tuple

#: 支持的人设图扩展名（小写、含点）
SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

#: 下载图片的体积上限（15 MB，与参考实现一致）
MAX_IMAGE_BYTES = 15 * 1024 * 1024

#: data URL 解析（image/*;base64,xxxx）
_DATA_URL_RE = re.compile(
    r"^data:image/(?P<format>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
    re.DOTALL,
)

#: 各格式的文件头魔数
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGIC = b"GIF8"
_BMP_MAGIC = b"BM"


def sniff_format(image_bytes: bytes, default: str = "png") -> str:
    """按文件头嗅探图片格式，返回 jpeg/png/gif/webp/bmp 之一。"""
    if image_bytes.startswith(_JPEG_MAGIC):
        return "jpeg"
    if image_bytes.startswith(_PNG_MAGIC):
        return "png"
    if image_bytes.startswith(_GIF_MAGIC):
        return "gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    if image_bytes.startswith(_BMP_MAGIC):
        return "bmp"
    return default


def sniff_strict(image_bytes: bytes) -> Optional[str]:
    """严格魔数嗅探：只认已知图片头，认不出返回 None（**不**用扩展名/默认值兜底）。

    v1.3.2 新增：本地路径引用（``file``/``path``）用本函数把关——
    非图片字节一律拒绝，防止把任意本地文件当「图片」读进模型请求。
    """
    if image_bytes.startswith(_JPEG_MAGIC):
        return "jpeg"
    if image_bytes.startswith(_PNG_MAGIC):
        return "png"
    if image_bytes.startswith(_GIF_MAGIC):
        return "gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "webp"
    if image_bytes.startswith(_BMP_MAGIC):
        return "bmp"
    return None


def format_from_name(file_name: str, default: str = "png") -> str:
    """按扩展名猜图片格式（jpg 归一为 jpeg）。"""
    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "bmp"}:
        return "jpeg" if suffix == "jpg" else suffix
    return default


def is_supported_suffix(file_name: str) -> bool:
    """判断文件名是否是支持的人设图扩展名。"""
    return Path(file_name).suffix.lower() in SUPPORTED_SUFFIXES


def mime_type(image_format: str) -> str:
    """把内部格式名转成 MIME 类型（jpg 归一为 jpeg）。"""
    normalized = (image_format or "png").strip().lower()
    if normalized == "jpg":
        normalized = "jpeg"
    return f"image/{normalized}"


def suffix_for_format(image_format: str) -> str:
    """把内部格式名转回常规扩展名（jpeg → jpg）。"""
    normalized = (image_format or "png").strip().lower()
    return "jpg" if normalized == "jpeg" else normalized


def to_base64(image_bytes: bytes) -> str:
    """图片二进制 → Base64 文本。"""
    return base64.b64encode(image_bytes).decode("utf-8")


def decode_base64_payload(raw: str) -> Optional[Tuple[str, str]]:
    """解析裸 Base64 或 ``data:image/...;base64,`` 文本。

    Returns:
        (格式, Base64) 或 None（空串/解码失败）。
    """
    decoded = decode_image_bytes(raw)
    if decoded is None:
        return None
    image_format, image_bytes = decoded
    return image_format, to_base64(image_bytes)


def decode_image_bytes(raw: str) -> Optional[Tuple[str, bytes]]:
    """解析裸 Base64 / data URL，返回 (格式, 原始字节)；失败返回 None。"""
    normalized = str(raw or "").strip()
    if not normalized:
        return None

    fallback_format = "png"
    data_url_match = _DATA_URL_RE.match(normalized)
    if data_url_match is not None:
        fallback_format = data_url_match.group("format").lower()
        normalized = data_url_match.group("data").strip()

    try:
        image_bytes = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not image_bytes:
        return None
    return sniff_format(image_bytes, fallback_format), image_bytes


def read_image_file(image_path: Path) -> Optional[Tuple[str, str]]:
    """读本地图片文件，返回 (格式, Base64)；不存在/读不了返回 None。"""
    try:
        if not image_path.is_file():
            return None
        image_bytes = image_path.read_bytes()
    except OSError:
        return None
    if not image_bytes:
        return None
    image_format = sniff_format(image_bytes, format_from_name(image_path.name))
    return image_format, to_base64(image_bytes)


# ══════════════════════════════════════════════════════════════ 消息取图（v1.1.0）


#: 图片组件里可能承载图片引用的键（顺序即优先级）
IMAGE_REFERENCE_KEYS = ("binary_data_base64", "base64", "data_url", "url", "file", "file_path", "path")


def _message_timestamp(message: Any) -> Optional[float]:
    """取消息时间戳；取不到返回 None（**不要**用 0 顶替，否则排序会猜错）。"""
    if not isinstance(message, dict):
        return None
    for container in (message, message.get("message_info"), message.get("additional_config")):
        if not isinstance(container, dict):
            continue
        for key in ("timestamp", "time", "ts", "created_at"):
            value = container.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                return float(value)
            try:
                return float(str(value))
            except (TypeError, ValueError):
                continue
    return None


def sort_messages_chronologically(messages: Any) -> list:
    """归一成「旧 → 新」；任一条缺时间戳就原样返回（不猜顺序，§72.3 ①）。"""
    if not isinstance(messages, list):
        return []
    stamps = [_message_timestamp(item) for item in messages]
    if any(stamp is None for stamp in stamps):
        return list(messages)
    return [item for _stamp, item in sorted(zip(stamps, messages), key=lambda pair: pair[0])]


def iter_image_components(message: Any) -> list:
    """列出消息里的图片组件（兼容 raw_message / segments / message 段列表）。"""
    if isinstance(message, dict):
        for key in ("raw_message", "segments", "message"):
            candidate = message.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)
                        and str(item.get("type") or "").strip().lower() == "image"]
        return []
    if isinstance(message, list):
        return [item for item in message if isinstance(item, dict)
                and str(item.get("type") or "").strip().lower() == "image"]
    return []


def find_image_reference(message: Any) -> Tuple[Optional[str], str, str]:
    """从消息里找第一张可用的图片引用。

    Returns:
        (引用种类, 引用值, 失败原因)；成功时失败原因为空串。
        种类取 ``base64`` / ``data_url`` / ``url`` / ``file`` / ``path``。
    """
    components = iter_image_components(message)
    if not components:
        return None, "", "消息里没有图片组件"
    for component in components:
        containers = [component]
        data = component.get("data")
        if isinstance(data, dict):
            containers.append(data)
        for container in containers:
            for key in IMAGE_REFERENCE_KEYS:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    kind = {
                        "binary_data_base64": "base64",
                        "base64": "base64",
                        "data_url": "data_url",
                        "url": "url",
                        "file": "file",
                        "file_path": "path",
                        "path": "path",
                    }[key]
                    return kind, value.strip(), ""
    return None, "", "图片组件里没有可用的图片内容（既没有 Base64 也没有链接）"


def message_text(message: Any) -> str:
    """取消息正文（processed_plain_text 优先，其次纯文本段拼接）。"""
    if isinstance(message, dict):
        direct = message.get("processed_plain_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
    parts: list = []
    if isinstance(message, dict):
        for key in ("raw_message", "segments", "message"):
            candidate = message.get(key)
            if isinstance(candidate, list):
                for item in candidate:
                    if isinstance(item, dict) and str(item.get("type") or "") == "text":
                        text = item.get("data") or item.get("text") or ""
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                if parts:
                    break
    return " ".join(parts).strip()
