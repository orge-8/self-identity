# -*- coding: utf-8 -*-
"""人设图库纯模块：目录扫描、内容哈希 ID、缩略图惰性生成、选图解析、分页。

纯模块纪律：不 import maibot_sdk、不出现 ctx。Pillow 为可选依赖：
缺失时缩略图功能降级（只出文本清单），其余功能不受影响。
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

if __package__:  # 包式加载（Runner 真机 / 测试包式加载）
    from .imageio_si import is_supported_suffix
else:  # 平铺兜底（脚本直跑）
    from imageio_si import is_supported_suffix

try:  # 可选依赖：Pillow 缺失时缩略图降级，模块仍可导入
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - 真机未装 Pillow 时走降级
    Image = None  # type: ignore[assignment]
    PIL_AVAILABLE = False

#: 图片 ID = 内容 sha1 前 12 位（与路径解耦：改名/挪目录不换 ID，改内容才换）
IMAGE_ID_HEX_LEN = 12

#: 文件名标签分隔符与标签内分隔符：`小镜#默认,制服.jpg` → ('默认', '制服')
TAG_MARK = "#"
TAG_SEPARATORS = r"[,，、;；/\s]+"

#: 标记「基准参考图」的标签名（可在配置里改）
DEFAULT_TAG = "默认"

LogFn = Callable[[str, str], None]


def parse_tags(file_name: str) -> Tuple[Tuple[str, ...], str]:
    """从文件名解析标签与基础名。

    ``小镜#默认,制服.jpg`` → ``(("默认", "制服"), "小镜")``；
    没有 ``#`` 时返回空标签与去扩展名的原名。
    """
    stem = Path(file_name).stem
    if TAG_MARK not in stem:
        return (), stem
    base, _sep, tag_text = stem.partition(TAG_MARK)
    tags = [part.strip() for part in re.split(TAG_SEPARATORS, tag_text) if part.strip()]
    return tuple(dict.fromkeys(tags)), (base.strip() or stem)


@dataclass
class SelfImageRecord:
    """一张人设图的记录。"""

    index: int = 0
    image_id: str = ""
    name: str = ""
    path: Path = None  # type: ignore[assignment]
    thumbnail_path: Optional[Path] = None
    size_bytes: int = 0
    mtime: float = 0.0
    thumbnail_ok: bool = False
    tags: Tuple[str, ...] = ()
    base_name: str = ""
    is_default: bool = False

    @property
    def tag_text(self) -> str:
        """标签的展示文本（无标签时为空串）。"""
        return "、".join(self.tags)

    def to_dict(self) -> Dict[str, Any]:
        """序列化（路径转字符串）。"""
        return {
            "index": self.index,
            "id": self.image_id,
            "name": self.name,
            "tags": list(self.tags),
            "is_default": self.is_default,
            "size_bytes": self.size_bytes,
            "thumbnail_ready": self.thumbnail_ok,
        }


def content_hash_id(image_path: Path) -> str:
    """按文件内容算 sha1 前 12 位作为图片 ID（内容为空/读不了时退回文件名哈希）。"""
    try:
        digest = hashlib.sha1(image_path.read_bytes()).hexdigest()
    except OSError:
        digest = hashlib.sha1(image_path.name.encode("utf-8", errors="ignore")).hexdigest()
    return digest[:IMAGE_ID_HEX_LEN]


def _thumbnail_name(image_path: Path, image_id: str) -> str:
    safe_stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", image_path.stem).strip("._") or "self_image"
    return f"{safe_stem}_{image_id}.png"


def _generate_thumbnail(image_path: Path, thumbnail_path: Path, max_px: int) -> None:
    """生成单张缩略图（最长边不超过 max_px，等比，LANCZOS，PNG 落盘）。"""
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow 未安装，无法生成缩略图")
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as image:
        image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        image.save(thumbnail_path, format="PNG", optimize=True)


def _ensure_thumbnail(record_src: Path, thumb_dst: Path, image_id: str, max_px: int) -> Tuple[Optional[Path], bool]:
    """惰性缩略图：不存在或比原图旧才生成；失败返回 (None, False)。"""
    if not PIL_AVAILABLE:
        return None, False
    try:
        source_mtime = record_src.stat().st_mtime
        if not (thumb_dst.exists() and thumb_dst.stat().st_mtime >= source_mtime):
            _generate_thumbnail(record_src, thumb_dst, max_px)
        return thumb_dst, True
    except Exception:
        return None, False


def scan_gallery(
    image_dir: Path,
    thumbnail_dir: Path,
    max_px: int = 512,
    ensure_dirs: bool = True,
    log: Optional[LogFn] = None,
    default_tag: str = DEFAULT_TAG,
) -> Tuple[List[SelfImageRecord], Dict[str, Any]]:
    """扫描人设图库并惰性生成缩略图。

    文件名里的 ``#`` 之后是标签（``小镜#默认,制服.jpg``）：标签用于按服饰/风格选参考图，
    含 ``默认`` 标签的那张是「基准参考图」（多图时无参调用默认用它）。

    Returns:
        (按文件名排序的记录列表, 统计 dict：total/generated/thumb_failed/pil_available/tagged/default_tag)
    """
    stats: Dict[str, Any] = {
        "total": 0,
        "generated": 0,
        "thumb_failed": 0,
        "pil_available": PIL_AVAILABLE,
        "tagged": 0,
        "has_default": False,
        "default_tag": default_tag,
    }
    if ensure_dirs:
        try:
            image_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if log is not None:
                log("warning", f"人设图目录创建失败：{image_dir}（{exc}）")
    if not image_dir.is_dir():
        return [], stats

    records: List[SelfImageRecord] = []
    files = sorted(
        (p for p in image_dir.iterdir() if p.is_file() and is_supported_suffix(p.name)),
        key=lambda p: p.name.lower(),
    )
    for index, path in enumerate(files, start=1):
        image_id = content_hash_id(path)
        thumb_path = thumbnail_dir / _thumbnail_name(path, image_id)
        thumb_ready = False
        if PIL_AVAILABLE:
            before = thumb_path.exists()
            thumb_path, thumb_ready = _ensure_thumbnail(path, thumb_path, image_id, max_px)
            if thumb_ready and not before:
                stats["generated"] += 1
            elif not thumb_ready:
                stats["thumb_failed"] += 1
        try:
            stat = path.stat()
            size_bytes = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            size_bytes, mtime = 0, 0.0
        tags, base_name = parse_tags(path.name)
        is_default = bool(default_tag) and default_tag in tags
        if tags:
            stats["tagged"] += 1
        if is_default:
            stats["has_default"] = True
        records.append(
            SelfImageRecord(
                index=index,
                image_id=image_id,
                name=path.name,
                path=path,
                thumbnail_path=thumb_path if thumb_ready else None,
                size_bytes=size_bytes,
                mtime=mtime,
                thumbnail_ok=thumb_ready,
                tags=tags,
                base_name=base_name,
                is_default=is_default,
            )
        )
    stats["total"] = len(records)
    return records, stats


def available_tags(records: List[SelfImageRecord]) -> List[str]:
    """图库里出现过的全部标签（去重保序）。"""
    tags: List[str] = []
    for record in records:
        for tag in record.tags:
            if tag not in tags:
                tags.append(tag)
    return tags


def find_default(records: List[SelfImageRecord], default_tag: str = DEFAULT_TAG) -> Optional[SelfImageRecord]:
    """找「基准参考图」：带默认标签的优先，其次唯一的那张。"""
    for record in records:
        if default_tag and default_tag in record.tags:
            return record
    if len(records) == 1:
        return records[0]
    return None


def match_by_tags(
    records: List[SelfImageRecord],
    tag_text: str,
    default_tag: str = DEFAULT_TAG,
) -> Tuple[Optional[SelfImageRecord], str]:
    """按标签选图（多个标签是 **AND** 语义）。

    命中多张时优先返回带默认标签的那张，否则要求调用方给出更精确的标签。
    """
    wanted = [part.strip() for part in re.split(TAG_SEPARATORS, str(tag_text or "")) if part.strip()]
    if not wanted:
        return None, "标签为空"
    hits = [record for record in records if all(tag in record.tags for tag in wanted)]
    if not hits:
        existing = available_tags(records)
        hint = "、".join(existing) if existing else "（当前没有任何图片带标签）"
        return None, f"没有标签为「{'、'.join(wanted)}」的人设图；现有标签：{hint}"
    if len(hits) == 1:
        return hits[0], ""
    default_hits = [record for record in hits if default_tag and default_tag in record.tags]
    if len(default_hits) == 1:
        return default_hits[0], ""
    names = "、".join(record.name for record in hits[:5])
    return None, f"标签「{'、'.join(wanted)}」匹配到多张（{names}），请再加一个标签或改用序号。"


def page_slice(
    records: List[SelfImageRecord],
    page: int,
    page_size: int,
) -> Tuple[List[SelfImageRecord], int, int, bool]:
    """分页切片：页码越界夹取。

    Returns:
        (当前页记录, 总页数, 实际页码, 是否被夹取)
    """
    total = len(records)
    total_pages = max(1, (total + max(1, page_size) - 1) // max(1, page_size))
    try:
        requested = int(page if page else 1)
    except (TypeError, ValueError):
        requested = 1
    actual = min(max(1, requested), total_pages)
    start = (actual - 1) * page_size
    return records[start : start + page_size], total_pages, actual, actual != requested


RANDOM_KEYWORDS = frozenset({"random", "随机", "随便", "任选"})


def resolve_image(
    records: List[SelfImageRecord],
    image_index: int = 0,
    image_name: str = "",
    tag: str = "",
    rng: Optional[random.Random] = None,
    default_tag: str = DEFAULT_TAG,
) -> Tuple[Optional[SelfImageRecord], str]:
    """按 标签 / 序号 / 精确名 / ID / 模糊名 / random 解析一条图库记录。

    匹配优先级：random 关键字 > 标签 > 精确文件名 > 精确 ID > 文件名(去扩展名)
    > 模糊名 > 序号；都没给时优先「默认」标记的那张，其次唯一的那张。

    Returns:
        (记录, "") 或 (None, 中文错误说明)。
    """
    if not records:
        return None, "人设图库为空，请先把图片放入插件目录的 self_image/ 文件夹。"

    name_query = str(image_name or "").strip()
    if name_query and name_query.lower() in RANDOM_KEYWORDS:
        pool = records
        return (rng or random).choice(pool), ""

    tag_query = str(tag or "").strip()
    if tag_query:
        record, error = match_by_tags(records, tag_query, default_tag=default_tag)
        if record is not None:
            return record, ""
        if name_query or image_index:
            # 标签没命中但调用方还给了别的选择条件 → 继续走后面的分支
            pass
        else:
            return None, error

    if name_query:
        lowered = name_query.lower()
        for record in records:
            if record.name.lower() == lowered:  # 精确名（大小写不敏感）
                return record, ""
        for record in records:
            if record.image_id == lowered:
                return record, ""
        for record in records:
            if record.path.stem.lower() == lowered or record.base_name.lower() == lowered:
                return record, ""
        # 模糊名：包含关系优先，其次相似度
        contains = [r for r in records if lowered in r.name.lower() or r.path.stem.lower() in lowered]
        if len(contains) == 1:
            return contains[0], ""
        if len(contains) > 1:
            names = "、".join(r.name for r in contains[:5])
            return None, f"图片名“{name_query}”匹配到多张（{names}），请给出更完整的文件名或序号。"
        best, best_score = None, 0.0
        for record in records:
            score = SequenceMatcher(None, lowered, record.path.stem.lower()).ratio()
            if score > best_score:
                best, best_score = record, score
        if best is not None and best_score >= 0.6:
            return best, ""
        return None, f"没有找到名为“{name_query}”的人设图，可先调用 view_all_image 查看图库清单。"

    try:
        normalized_index = int(image_index or 0)
    except (TypeError, ValueError):
        normalized_index = 0
    if normalized_index > 0:
        if normalized_index <= len(records):
            return records[normalized_index - 1], ""
        return None, f"人设图序号 {normalized_index} 超出范围，当前共有 {len(records)} 张。"

    default_record = find_default(records, default_tag=default_tag)
    if default_record is not None:
        return default_record, ""

    tags_hint = "、".join(available_tags(records)) or "（没有图片带标签）"
    return None, (
        "存在多张人设图，请提供 tag（服饰/风格标签）、image_index 或 image_name，"
        f"或给基准图加上「{default_tag}」标签；现有标签：{tags_hint}。"
        "也可以传 image_name=\"random\" 随机抽一张。"
    )


def format_size(size_bytes: int) -> str:
    """人类可读的文件大小。"""
    if size_bytes <= 0:
        return "0 KB"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def render_gallery_text(records: List[SelfImageRecord], thumbnail_ready_label: str = "缩略图就绪") -> str:
    """渲染给人看的文本图库清单（/人设图库 用）。"""
    if not records:
        return "人设图库为空（self_image/ 目录里还没有图片）。"
    lines = [f"人设图库共 {len(records)} 张："]
    for record in records:
        state = thumbnail_ready_label if record.thumbnail_ok else "缩略图不可用"
        marks: List[str] = []
        if record.is_default:
            marks.append("基准参考图")
        tag_part = f"｜标签：{record.tag_text}" if record.tags else "｜未打标签"
        lines.append(
            f"{record.index}. {record.name}（id: {record.image_id}，"
            f"{format_size(record.size_bytes)}，{state}{tag_part}"
            + (f"｜{'/'.join(marks)}" if marks else "")
            + "）"
        )
    tags = available_tags(records)
    if tags:
        lines.append(
            "可用标签：" + "、".join(tags)
            + "（按标签取图：get_self_image 的 tag 参数，多个标签是 AND 语义）"
        )
    else:
        lines.append(
            "提示：在文件名里加 # 标签即可按服饰/风格取图，例如「小镜#默认,制服.jpg」；"
            "带「默认」标签的那张是基准参考图。"
        )
    return "\n".join(lines)


def refresh_timestamp() -> float:
    """取当前时间戳（独立出来方便测试注入）。"""
    return time.time()
