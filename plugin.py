# -*- coding: utf-8 -*-
"""self-identity：自我身份档案插件入口。

本文件是唯一允许出现 ``self.ctx`` 的地方（check_plugin.py 只扫本文件推导
能力声明）。plugin.py 只做装配：生命周期、Tool/Command 注册、依赖注入；
纯逻辑全在 ``*_si`` 模块里，经回调注入、可脱机单测。

⚠ 入口文件禁止 ``from __future__ import annotations``（pydantic 配置模型
解析需要运行时可解析的注解）。
"""

import asyncio
import copy
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from pydantic import field_validator

if __package__:  # 包式加载（Runner 真机）
    from . import compare_si, fetch_si, gallery_si, identity_si, imageio_si
else:  # 平铺兜底（脚本直跑/本地测试）
    import compare_si
    import fetch_si
    import gallery_si
    import identity_si
    import imageio_si

#: 插件 ID（与 _manifest.json 一致）
PLUGIN_ID = "org.mai-mai.self-identity"

#: 配置版本（1.2.3+ 硬性要求；与 PluginSectionConfig.config_version 同步）
SUPPORTED_CONFIG_VERSION = "1.3.0"

#: 人设图目录默认值（插件目录相对路径）
DEFAULT_IMAGE_DIR = "self_image"
DEFAULT_THUMB_DIR = "image_thumbup"

#: 头像缓存：24 小时磁盘缓存（runtime 目录，可重建）
AVATAR_CACHE_TTL_SECONDS = 24 * 3600.0
AVATAR_CACHE_DIR_NAME = "avatar_cache"

#: 视觉调用的 RPC 超时（Host 默认 30s；必须大于内层 wait_for，否则真因被盖掉）
LLM_RPC_TIMEOUT_MS = 150_000

#: 取最近消息的条数（§72：返回顺序无约定，取回后自己按时间排序）
RECENT_MESSAGE_SCAN_LIMIT = 20

#: 工具返回条数上限（防 LLM 传超大 limit）
MAX_SEARCH_LIMIT = 20

logger = logging.getLogger(f"plugin.{PLUGIN_ID}")


def _tool_param(name: str, param_type: ToolParamType, description: str, required: bool) -> ToolParameterInfo:
    """构造工具参数声明。"""
    return ToolParameterInfo(name=name, param_type=param_type, description=description, required=required)


def _unavailable_result(reason: str, **extra: Any) -> Dict[str, Any]:
    """工具不可用/失败时的兜底返回（给模型可读的原因，不抛堆栈）。"""
    result: Dict[str, Any] = {"success": False, "content": str(reason or "功能暂时不可用。")}
    result.update(extra)
    return result


# ══════════════════════════════════════════════════════════════ 配置模型


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "user-round"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用自我身份档案插件")
    config_version: str = Field(default=SUPPORTED_CONFIG_VERSION, description="配置版本")
    debug: bool = Field(default=False, description="输出详细诊断日志")


class IdentityImageConfig(PluginConfigBase):
    """人设图库配置。"""

    __ui_label__ = "人设图库"
    __ui_icon__ = "image"
    __ui_order__ = 1

    image_dir: str = Field(
        default=DEFAULT_IMAGE_DIR,
        description="人设原图目录：插件目录相对路径或绝对路径（把人设图放进去即可）",
    )
    thumbnail_dir: str = Field(
        default=DEFAULT_THUMB_DIR,
        description="缩略图缓存目录：插件目录相对路径或绝对路径（可整体删除，会按需重建）",
    )
    thumbnail_max_px: int = Field(
        default=512, ge=128, le=1024, description="缩略图最长边像素（等比缩放）"
    )
    page_size: int = Field(
        default=10, ge=1, le=50, description="view_all_image 每页显示几张"
    )
    default_reference_tag: str = Field(
        default="默认",
        description=(
            "标记「基准参考图」的标签名：多张人设图时，带该标签的那张作为默认参考图"
            "（文件名里写 #默认 即可，如「小镜#默认,制服.jpg」）"
        ),
    )


class SearchConfig(PluginConfigBase):
    """检索配置。"""

    __ui_label__ = "检索"
    __ui_icon__ = "search"
    __ui_order__ = 2

    default_limit: int = Field(default=5, ge=1, le=MAX_SEARCH_LIMIT, description="默认返回条数")
    match_threshold: float = Field(
        default=15.0, ge=0.0, le=200.0,
        description="命中阈值：条目总分须不低于该值才算匹配（调低更宽松，调高更严格）",
    )


class ProfileSectionConfig(PluginConfigBase):
    """身份档案（结构化 profile，v1.1.0 起用于身份卡渲染）。"""

    __ui_label__ = "身份档案"
    __ui_icon__ = "id-card"
    __ui_order__ = 3

    name: str = Field(default="", description="名字")
    role: str = Field(default="", description="身份定位")
    appearance: str = Field(default="", description="外貌")
    personality: str = Field(default="", description="性格")
    speech_style: str = Field(default="", description="说话风格")
    taboos: str = Field(default="", description="忌讳")
    background: str = Field(default="", description="背景")
    max_card_chars: int = Field(
        default=400, ge=120, le=1200,
        description=(
            "身份档案卡字符上限（/人设卡片 输出的长度，约 300 字最省 token；"
            "profile 写得多时会被截断）"
        ),
    )


class VisionSectionConfig(PluginConfigBase):
    """视觉比对配置（v1.1.0：compare_with_self_image）。"""

    __ui_label__ = "视觉比对"
    __ui_icon__ = "eye"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否启用 compare_with_self_image 视觉比对")
    task_name: str = Field(default="vlm", description="Host 模型任务名（不是具体模型名）")
    model_name: str = Field(default="", description="具体模型名，留空则用任务默认")
    max_tokens: int = Field(
        default=compare_si.VISION_MAX_TOKENS_FLOOR, ge=64, le=4096,
        description=(
            "视觉请求最大输出 token。低于下限会被运行期抬到 1600："
            "推理模型的思考 token 也算在这个上限里，配小了 JSON 会被截断，"
            "表现为「比对没结果」"
        ),
    )
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="视觉请求温度")
    ignore_outfit_differences: bool = Field(
        default=True,
        description=(
            "不同服饰/画风不算冲突：同一个角色可能有多套装扮，比对只按发色／发型／瞳色／五官／气质判定，"
            "服装差异写进 differences；关掉则服装也作为判定依据"
        ),
    )
    image_timeout_seconds: float = Field(
        default=90.0, ge=3.0, le=170.0,
        description="单张图片比对的插件侧预算（秒）；会被外层预算（消息×80% / RPC−10s）夹取，生效值见 /人设状态",
    )
    message_timeout_seconds: float = Field(
        default=110.0, ge=5.0, le=300.0,
        description="整条消息的比对总预算（秒）；必须大于单图预算，否则用户只会看到「超预算」",
    )


class IdentityInfoItem(PluginConfigBase):
    """单条自我信息。"""

    title: str = Field(default="", description="信息标题，如「喜欢的食物」")
    keywords: List[str] = Field(default_factory=list, description="关键词列表，如 [\"食物\",\"喜好\"]")
    full_information: str = Field(default="", description="全量信息")

    @field_validator("keywords", mode="before")
    @classmethod
    def _split_keyword_string(cls, value: Any) -> Any:
        """容忍关键词写成字符串：按分隔符拆成列表（"a,b" → ["a","b"]）。"""
        if isinstance(value, str):
            return identity_si.split_keywords(value)
        return value


class GeneralSectionConfig(PluginConfigBase):
    """自我信息（配置页的「自我信息」节；config.toml 里是 ``[general]``）。

    ⚠ 为什么必须是一节、而不是根级字段（v1.3.0 真机缺陷的根因，宿主 1.3.2 源码核实）：

    - 宿主的插件配置页按「节名 = 点号路径」读写配置
      （dashboard `routes/plugin-config/utils.ts` 的 `getNestedRecord` / `setNestedField`）；
    - 而 SDK 会把**根级字段**塞进一个合成的 ``general`` 节
      （`maibot_sdk/config.py::generate_plugin_config_schema`）。

    于是根级 ``infos`` 在配置页里读的是 ``config.general.infos``（永远 undefined ⇒ 已有条目看不见），
    保存时写成 ``{"general": {"infos": [...]}}``，再被根模型的 ``extra="ignore"`` 丢掉 ——
    用户在配置页加满条目，插件始终读到 0 条（复现见 `tests/test_self_identity.py`）。
    把 ``infos`` 声明在名为 ``general`` 的真实节里，读写两侧就对上了。
    """

    __ui_label__ = "自我信息"
    __ui_icon__ = "list"
    __ui_order__ = 4

    infos: List[IdentityInfoItem] = Field(
        default_factory=list,
        description="Bot 的自我信息列表（检索与声明自检的素材来源）",
        json_schema_extra={
            "label": "自我信息条目",
            # webui 的可视化模式只渲染 label/hint/placeholder，不渲染 description（webui 契约）
            "hint": "点「添加项目」逐条添加：标题 + 关键词（逗号分隔可写多个）+ 全量信息。此节即 config.toml 的 [general]。",
        },
    )


class SelfIdentityConfig(PluginConfigBase):
    """插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    identity_image: IdentityImageConfig = Field(default_factory=IdentityImageConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    profile: ProfileSectionConfig = Field(default_factory=ProfileSectionConfig)
    general: GeneralSectionConfig = Field(default_factory=GeneralSectionConfig)
    vision: VisionSectionConfig = Field(default_factory=VisionSectionConfig)



# ══════════════════════════════════════════════════════════════ 插件主体


class SelfIdentityPlugin(MaiBotPlugin):
    """自我身份档案插件：档案检索 + 人设图库 + 头像获取 + 声明自检/视觉比对。"""

    config_model = SelfIdentityConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # 基类 __init__ 不接受参数：收下但不转发（runtime-gotchas §61.4）
        super().__init__()
        del args, kwargs
        self._infos: List[identity_si.IdentityInfo] = []
        self._gallery: List[gallery_si.SelfImageRecord] = []
        self._gallery_stats: Dict[str, Any] = {}
        self._last_refresh_ts: float = 0.0
        self._avatar_cache: Optional[fetch_si.DiskCache] = None
        self._fallback_config_instance: Optional[SelfIdentityConfig] = None
        #: 一次性提示标记（max_tokens 下限抬升只提示一次）
        self._token_floor_hinted = False

    # ---------------------------------------------------------- 配置注入防御

    @staticmethod
    def _sanitize_config(config: Any) -> Any:
        """交给 SDK 之前补齐 [plugin].config_version——版本检查排在合并默认值之前。"""
        if not isinstance(config, Mapping):
            return config  # 非 Mapping 原样透传，交给 SDK 的默认值分支
        data = dict(config)
        raw = data.get("plugin")
        section = dict(raw) if isinstance(raw, Mapping) else {}
        if not str(section.get("config_version") or "").strip():
            section["config_version"] = SUPPORTED_CONFIG_VERSION
        data["plugin"] = section
        return data

    def normalize_plugin_config(self, config_data: Any) -> Tuple[Dict[str, Any], bool]:
        """归一化配置：把**旧版根级** ``infos`` 迁进 ``[general]`` 节，再交给 SDK。

        宿主的插件配置页把 SDK 合成出来的 ``general`` 节当普通节读写，所以
        v1.3.0 里那个根级 ``infos`` 会被配置页静默丢弃（根因见 ``GeneralSectionConfig``
        的文档字符串）。v1.3.1 起 ``infos`` 落在真实的 ``[general]`` 节里。

        这里只做一件事：老 ``config.toml`` 的根级 ``[[infos]]`` 仍然可用 ——
        只要 ``[general] infos`` 还是空的，就把根级那批条目搬进去
        （两处都有值时以 ``[general]`` 为准并告警，绝不静默覆盖配置页里的内容）。
        """
        raw = config_data if isinstance(config_data, Mapping) else {}
        data = dict(raw)
        legacy = data.pop("infos", None)
        section = data.get("general")
        section = dict(section) if isinstance(section, Mapping) else {}
        if isinstance(legacy, list) and legacy:
            if section.get("infos"):
                self._log(
                    "warning",
                    "根级 infos（%d 条）与 [general] infos（%d 条）同时存在，按 [general] 为准；"
                    "根级那批请手工合并后删除",
                    len(legacy),
                    len(section.get("infos") or []),
                )
            else:
                section["infos"] = legacy
                self._log(
                    "info",
                    "检测到旧版根级 infos（%d 条），已迁入 [general] 节（下次保存后落盘为该节）",
                    len(legacy),
                )
        elif legacy:
            # 根级 infos 存在但不是列表：不能静默丢掉（审计第 12 项）
            self._log(
                "warning",
                "根级 infos 不是列表（实际为 %s），已忽略；请把条目写进 [general] 节",
                type(legacy).__name__,
            )
        if section:
            data["general"] = section
        return super().normalize_plugin_config(self._sanitize_config(data))

    def set_plugin_config(self, config: Dict[str, Any]) -> None:
        """宿主注入配置的唯一入口：先走 SDK 主路，失败再「默认配置 + 用户已有值」逐字段修复。

        真机配置异常的报错会汇总成「插件初始化失败」且插件代码零执行，
        这里是唯一能拦住它并留下真因日志的位置（runtime-gotchas §7.1）。
        """
        sanitized = self._sanitize_config(config)
        try:
            super().set_plugin_config(sanitized)
            return
        except Exception as exc:  # noqa: BLE001 - 必须兜住，否则整个插件注册失败
            self._log(
                "error",
                "插件配置注入失败，已回退「默认配置 + 用户已有值」。真因：%s: %s",
                type(exc).__name__,
                exc,
            )
        defaults = type(self).build_default_config()
        merged = self._merge_with_defaults(
            defaults, sanitized if isinstance(sanitized, Mapping) else {}
        )
        instance = self._repair_config(type(self).get_config_model(), merged, defaults)
        if instance is None:
            self._log("error", "配置兜底修复失败，self.config 将回退默认配置")
            instance = self._build_default_instance()
        if instance is not None:
            self._plugin_config_instance = instance
            try:
                self._plugin_config_data = instance.model_dump(mode="python")
            except Exception:
                pass

    @property
    def config(self) -> SelfIdentityConfig:
        """下游保险：配置实例缺失时回退默认配置（不抛 RuntimeError 拖垮生命周期）。

        ⚠ SDK 的 ``collect_components`` 会 ``getattr`` 遍历实例的全部属性
        （``maibot_sdk/components.py`` 的 ``dir(instance)`` 循环），所以这里必须
        **安静且不改动 SDK 状态**：真因由 ``set_plugin_config`` 记 error 日志，
        本兜底只在 debug 级别留痕，且不写 ``_plugin_config_instance``。
        """
        try:
            return super().config
        except RuntimeError:
            instance = self._fallback_config_instance
            if instance is None:
                instance = self._build_default_instance()
                self._fallback_config_instance = instance
                self._log("debug", "配置实例尚未注入，config 属性回退默认配置（组件枚举阶段属正常）")
            if instance is None:
                raise
            return instance

    # ---------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        """插件加载：重建档案/图库/缓存。单点失败不阻断启动。"""
        try:
            self._reload_infos()
            count, _stats = await self._refresh_gallery()
            self._log(
                "info",
                "自我身份档案插件已加载：档案条目 %d，人设图 %d 张，缩略图引擎=%s（被动模式：不改写模型请求）",
                len(self._infos),
                count,
                "Pillow" if gallery_si.PIL_AVAILABLE else "缺失（图库降级为纯文本）",
            )
        except Exception as exc:  # noqa: BLE001 - on_load 抛异常即插件加载失败
            self._log("error", "on_load 执行异常（插件继续运行）：%s: %s", type(exc).__name__, exc)

    async def on_unload(self) -> None:
        """插件卸载：清空内存状态（磁盘缓存按 TTL 自然过期，不清理）。"""
        self._infos = []
        self._gallery = []
        self._avatar_cache = None
        self._log("info", "自我身份档案插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热重载：真重载 infos 与图库目录（参考插件的空函数已修正）。

        WebUI 保存配置后 Runner 会先注入新配置再回调本方法，
        此时 ``self.config`` 已是最新值，直接重建派生状态即可。
        """
        del config_data, version
        try:
            infos = self._reload_infos()
            count, stats = await self._refresh_gallery()
            self._avatar_cache = None  # 运行时目录可能变化，下次用时惰性重建
            self._log(
                "info",
                "配置已更新（scope=%s）：档案条目 %d，人设图 %d 张（缩略图生成 %d 张）",
                scope,
                len(infos),
                count,
                stats.get("generated", 0),
            )
        except Exception as exc:  # noqa: BLE001 - 配置回调失败不能拖垮 Runner
            self._log("error", "on_config_update 执行异常：%s: %s", type(exc).__name__, exc)

    # ---------------------------------------------------------- 基础设施

    def _log(self, level: str, message: str, *args: Any) -> None:
        """统一日志入口：无 ctx 时回退模块级 logger，日志失败不拖垮业务。"""
        try:
            text = message % args if args else message
            log = self._get_logger()
            method = getattr(log, level if level in {"debug", "info", "warning", "error"} else "info")
            method(text)
        except Exception:  # noqa: BLE001
            pass

    def _plugin_dir(self) -> Path:
        """插件目录。"""
        return Path(__file__).resolve().parent

    def _resolve_configured_dir(self, configured_path: str, default_name: str) -> Path:
        """解析配置目录：空值回默认；相对路径以插件目录为基准。"""
        normalized = str(configured_path or "").strip() or default_name
        path = Path(normalized)
        if not path.is_absolute():
            path = self._plugin_dir() / normalized
        return path

    def _runtime_dir(self) -> Optional[Path]:
        """运行时可重建目录（ctx.paths.runtime_dir，拿不到时返回 None）。"""
        try:
            runtime_dir = Path(self.ctx.paths.runtime_dir)
            runtime_dir.mkdir(parents=True, exist_ok=True)
            return runtime_dir
        except Exception:  # noqa: BLE001 - FakeHost/旧 SDK 可能没有 paths
            return None

    def _get_avatar_cache(self) -> Optional[fetch_si.DiskCache]:
        """头像磁盘缓存（惰性创建，目录不可用时返回 None）。"""
        if self._avatar_cache is None:
            base_dir = self._runtime_dir()
            if base_dir is None:
                base_dir = self._plugin_dir() / "cache"
            self._avatar_cache = fetch_si.DiskCache(
                base_dir / AVATAR_CACHE_DIR_NAME,
                AVATAR_CACHE_TTL_SECONDS,
                log=self._log,
            )
        return self._avatar_cache

    def _reload_infos(self) -> List[identity_si.IdentityInfo]:
        """从当前配置重建档案条目（``[general] infos``；空条目已过滤）。"""
        section = getattr(self.config, "general", None)
        raw_infos = getattr(section, "infos", None) if section is not None else None
        self._infos = identity_si.collect_infos(raw_infos or (), log=self._log)
        return self._infos

    async def _refresh_gallery(self) -> Tuple[int, Dict[str, Any]]:
        """按当前配置重扫图库并惰性生成缩略图。

        扫描要读整张原图算内容哈希、还要跑 PIL 缩放（CPU + 磁盘），
        一律 `asyncio.to_thread` —— 同步跑会阻塞事件循环（审计第 13 项）。
        """
        image_cfg = self.config.identity_image
        image_dir = self._resolve_configured_dir(image_cfg.image_dir, DEFAULT_IMAGE_DIR)
        thumb_dir = self._resolve_configured_dir(image_cfg.thumbnail_dir, DEFAULT_THUMB_DIR)
        records, stats = await asyncio.to_thread(
            gallery_si.scan_gallery,
            image_dir,
            thumb_dir,
            max_px=image_cfg.thumbnail_max_px,
            ensure_dirs=True,
            log=self._log,
            default_tag=str(image_cfg.default_reference_tag or gallery_si.DEFAULT_TAG),
        )
        self._gallery = records
        self._gallery_stats = stats
        self._last_refresh_ts = time.time()
        return len(records), stats

    async def _scan_gallery_now(self) -> List[gallery_si.SelfImageRecord]:
        """实时扫描（工具/命令每次调用都扫，保证新放入的图片立即可见）。"""
        await self._refresh_gallery()
        return self._gallery

    def _stream_id(self, kwargs: Dict[str, Any]) -> str:
        """从命令载荷稳健提取会话 ID（字段名随版本浮动，多路径兜底）。"""
        for key in ("stream_id", "chat_id", "session_id", "stream"):
            value = kwargs.get(key)
            if value:
                return str(value)
        message = kwargs.get("message")
        if isinstance(message, dict):
            return str(message.get("session_id") or message.get("stream_id") or "")
        return ""

    async def _reply(self, kwargs: Dict[str, Any], text: str) -> Tuple[bool, str, int]:
        """命令回复：发得出去就拦截（2），发不出去放行让 bot 补话（0）。"""
        stream_id = self._stream_id(kwargs)
        sent = False
        if stream_id:
            try:
                sent = bool(await self.ctx.send.text(text, stream_id))
            except Exception as exc:  # noqa: BLE001
                self._log("error", "命令回复发送失败：%s: %s", type(exc).__name__, exc)
        else:
            self._log("error", "命令载荷里没有 stream_id，无法回复（字段=%s）", sorted(kwargs))
        return True, text, 2 if sent else 0

    @staticmethod
    def _format_ts(ts: float) -> str:
        """时间戳 → 紧凑可读文本。"""
        if ts <= 0:
            return "尚未刷新"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    # ---------------------------------------------------------- 身份档案卡（被动）

    def _vision_cfg(self) -> VisionSectionConfig:
        """视觉配置（旧 config.toml 缺该节时回退默认值）。"""
        section = getattr(self.config, "vision", None)
        return section if section is not None else VisionSectionConfig()

    def _card_text(self) -> str:
        """渲染身份档案卡文本（给用户复制的产物，不注入任何模型请求）。"""
        infos = self._infos or self._reload_infos()
        return identity_si.build_identity_card(
            self.config.profile,
            infos=infos,
            max_chars=int(getattr(self.config.profile, "max_card_chars", 400) or 400),
        )

    def _profile_hint(self, extra: str = "") -> str:
        """视觉比对的参考要点：profile 已填字段 + 调用方补充描述（中和后截断）。"""
        profile = self.config.profile
        bits = [
            f"{label}：{str(getattr(profile, key, '') or '').strip()}"
            for key, label in identity_si.PROFILE_FIELDS
            if str(getattr(profile, key, "") or "").strip()
        ]
        hint = "；".join(bits)
        extra_text = identity_si.neutralize(str(extra or "").strip())
        if extra_text:
            hint = f"{hint}；图片补充描述：{extra_text}" if hint else f"图片补充描述：{extra_text}"
        return hint[:400]

    def _effective_max_tokens(self) -> int:
        """生效的 max_tokens：低于下限时运行期抬升 + 一次性提示（§70.1）。"""
        configured = int(self._vision_cfg().max_tokens or 0)
        effective = compare_si.effective_max_tokens(configured)
        if effective > configured and not self._token_floor_hinted:
            self._token_floor_hinted = True
            self._log(
                "warning",
                "视觉 max_tokens 配的是 %d，低于下限 %d，已按 %d 使用："
                "推理模型的思考 token 也计入该上限，配小了 JSON 会被截断、"
                "表现为「比对没结果」。抬高上限只按实际输出计费，不会多花钱。",
                configured,
                compare_si.VISION_MAX_TOKENS_FLOOR,
                effective,
            )
        return effective

    def _effective_image_timeout(self) -> Tuple[float, bool]:
        """生效的单图超时（返回是否被外层预算夹取；短者必须先触发，§70.2）。

        v1.3.2：夹取源有两个——消息预算 ×80% 与 RPC 层预算（``LLM_RPC_TIMEOUT_MS``
        减 10s 解析余量）。配置允许的单图上限 170s 理论上可以越过 RPC 的 150s，
        不兜底则 RPC 先超时、真因只能靠文本启发式猜。
        """
        cfg = self._vision_cfg()
        seconds, clamped = compare_si.effective_timeout(cfg.image_timeout_seconds, cfg.message_timeout_seconds)
        rpc_budget = max(1.0, LLM_RPC_TIMEOUT_MS / 1000.0 - 10.0)
        if seconds > rpc_budget:
            return rpc_budget, True
        return seconds, clamped

    async def _generate_vlm(self, **kwargs: Any) -> Any:
        """给 compare_si 注入的 generate 适配器：显式带 RPC 超时（Host 默认 30s）。"""
        return await self.ctx.llm.generate(timeout_ms=LLM_RPC_TIMEOUT_MS, **kwargs)

    async def _resolve_self_image_bytes(
        self, image_index: int = 0, image_name: str = "", tag: str = ""
    ) -> Tuple[Optional[gallery_si.SelfImageRecord], Optional[bytes], str, str]:
        """取人设参考图（读盘 + 编解码走线程池）。

        Returns:
            (记录, 字节, MIME, 失败原因)；成功时失败原因为空串。
        """
        records = await self._scan_gallery_now()
        record, resolve_error = gallery_si.resolve_image(
            records,
            image_index=image_index,
            image_name=str(image_name or ""),
            tag=str(tag or ""),
            default_tag=str(self.config.identity_image.default_reference_tag or gallery_si.DEFAULT_TAG),
        )
        if record is None:
            return None, None, "", resolve_error
        # v1.3.3（插件中心审核建议①）：原图读取加体积上限——先 stat 预检（不把巨型文件
        # 整个读进内存），读取后再兜底校验一次；超限给可读压缩提示，而不是把巨图
        # base64 进模型上下文。缩略图侧早有 max_px 兜底，原图侧此前没有。
        try:
            size_bytes = record.path.stat().st_size
        except OSError as exc:
            return record, None, "", f"人设图读取失败：{record.name}（{type(exc).__name__}）"
        if size_bytes > imageio_si.MAX_IMAGE_BYTES:
            return record, None, "", (
                f"人设图超过体积上限（{imageio_si.MAX_IMAGE_BYTES // (1024 * 1024)} MB）：{record.name}"
                f"（实际 {size_bytes / (1024 * 1024):.1f} MB）。原图会整份进模型上下文，请压缩后再放入图库。"
            )
        try:
            data = await asyncio.to_thread(record.path.read_bytes)
        except OSError as exc:
            return record, None, "", f"人设图读取失败：{record.name}（{type(exc).__name__}）"
        if len(data) > imageio_si.MAX_IMAGE_BYTES:  # 兜底：stat 与读取之间文件被替换
            return record, None, "", (
                f"人设图超过体积上限（{imageio_si.MAX_IMAGE_BYTES // (1024 * 1024)} MB）：{record.name}。"
                "原图会整份进模型上下文，请压缩后再放入图库。"
            )
        if not data:
            return record, None, "", f"人设图为空文件：{record.name}"
        mime = imageio_si.mime_type(imageio_si.sniff_format(data, imageio_si.format_from_name(record.name)))
        return record, data, mime, ""

    async def _recent_image_from_stream(self, stream_id: str) -> Tuple[Optional[bytes], str, str]:
        """从最近消息里取一张图片（§72：顺序无约定 → 自己按时间排序取最新）。"""
        target = str(stream_id or "").strip()
        if not target:
            return None, "", "没有会话上下文，无法读取最近图片"
        try:
            raw = await self.ctx.message.get_recent(target, RECENT_MESSAGE_SCAN_LIMIT)
        except Exception as exc:  # noqa: BLE001
            return None, "", f"读取最近消息失败：{type(exc).__name__}: {exc}"
        messages: Any = raw
        if isinstance(raw, dict):
            messages = raw.get("messages")
        if not isinstance(messages, list) or not messages:
            return None, "", "最近消息里没有可用内容"
        ordered = imageio_si.sort_messages_chronologically(messages)
        for message in reversed(ordered):  # 新 → 旧，优先最新的图片
            kind, reference, reason = imageio_si.find_image_reference(message)
            if not kind:
                continue
            data, mime, error = await self._load_image_reference(kind, reference)
            if data:
                return data, mime, ""
            self._log("warning", "最近消息里的图片读取失败：kind=%s reason=%s", kind, error)
        return None, "", "最近消息里没有可用的图片"

    async def _load_image_reference(self, kind: str, reference: str) -> Tuple[Optional[bytes], str, str]:
        """把 Base64 / data URL / URL / 本地路径统一读成 (字节, MIME, 失败原因)。"""
        if kind in {"base64", "data_url"}:
            payload = reference
            if kind == "base64" and not payload.startswith("data:"):
                payload = "data:image/png;base64," + payload
            decoded = await asyncio.to_thread(imageio_si.decode_image_bytes, payload)
            if decoded is None:
                return None, "", "Base64 图片解码失败"
            image_format, data = decoded
            return data, imageio_si.mime_type(image_format), ""
        if kind == "url":
            try:
                data, content_type = await fetch_si.download_image_bytes(
                    reference,
                    timeout_seconds=fetch_si.DEFAULT_TIMEOUT_SECONDS,
                    log=self._log,
                )
            except fetch_si.FetchUnavailableError as exc:
                return None, "", str(exc)
            except fetch_si.DownloadError as exc:
                return None, "", f"图片下载失败：{exc}"
            image_format = imageio_si.sniff_format(
                data, fetch_si.guess_format_from_content_type(content_type)
            )
            return data, imageio_si.mime_type(image_format), ""
        if kind in {"file", "path"}:
            path_text = str(reference or "").strip()
            if path_text.startswith("file://"):
                path_text = path_text[len("file://") :]
            try:
                path = Path(path_text)
                if not path.is_absolute():
                    return None, "", "本地图片路径必须是绝对路径"
                data = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                return None, "", f"本地图片读取失败：{type(exc).__name__}"
            if not data:
                return None, "", "本地图片为空文件"
            # v1.3.2 加固（审计低危项）：本地路径引用来自入站消息组件的 file/path 字段，
            # 必须严格校验图片魔数并限制体积——非图片文件（任意绝对路径可读到的）一律拒绝，
            # 防止把本地文件内容当「图片」读进模型请求；大文件同步读也会阻塞事件循环。
            if len(data) > imageio_si.MAX_IMAGE_BYTES:
                return None, "", f"本地图片超过体积上限（{imageio_si.MAX_IMAGE_BYTES // (1024 * 1024)} MB）"
            image_format = imageio_si.sniff_strict(data)
            if image_format is None:
                return None, "", f"本地文件不是有效图片，已拒绝读取：{path.name}"
            return data, imageio_si.mime_type(image_format), ""
        return None, "", f"不支持的图片引用类型：{kind}"

    async def _message_image_from_kwargs(self, kwargs: Dict[str, Any]) -> Tuple[Optional[bytes], str, str]:
        """入站原文兜底：工具载荷里 Host 注入的 message 里可能就有图片（§72.2 ②）。"""
        message = kwargs.get("message")
        if not isinstance(message, (dict, list)):
            return None, "", "载荷里没有入站消息"
        kind, reference, reason = imageio_si.find_image_reference(message)
        if not kind:
            return None, "", reason
        return await self._load_image_reference(kind, reference)

    # ---------------------------------------------------------- Tool：档案检索

    @Tool(
        "search_self_information",
        description=(
            "查询 Bot 自己的人设信息（名字、年龄、外貌、性格、喜好、背景、人际关系等设定）。"
            "当有人问“你是谁 / 你多大了 / 你喜欢什么 / 你从哪来”，"
            "或你需要在回复前确认自己的设定细节时调用。"
            "title 按条目标题匹配；keyword 按关键词匹配（可用逗号写多个，需全部命中）；"
            "query 为通用搜索词。"
        ),
        parameters=[
            _tool_param("query", ToolParamType.STRING, "通用搜索词，如“喜欢的食物”，可为空", False),
            _tool_param("title", ToolParamType.STRING, "按条目标题匹配，可为空", False),
            _tool_param("keyword", ToolParamType.STRING, "按关键词匹配，多个用逗号分隔（AND），可为空", False),
            _tool_param("limit", ToolParamType.INTEGER, "最多返回几条，默认 5", False),
        ],
    )
    async def search_self_information(
        self,
        query: str = "",
        title: str = "",
        keyword: str = "",
        limit: int = 0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """检索自我档案：空条目守卫 + 多关键词 AND + 打分阈值。"""
        del kwargs
        try:
            infos = self._infos or self._reload_infos()
            if not infos:
                return _unavailable_result(
                    "当前还没有配置任何自我信息。请在 WebUI 插件配置的 infos 列表里添加条目，"
                    "或参考 README 的配置示例。"
                )

            if not str(query or "").strip() and not str(title or "").strip() and not str(keyword or "").strip():
                return _unavailable_result(
                    "请至少提供 query、title、keyword 其中一个搜索条件。"
                    "例如 keyword=“外貌,发型” 或 title=“背景故事”。"
                )

            search_cfg = self.config.search
            try:
                normalized_limit = int(limit) if limit else 0
            except (TypeError, ValueError):
                normalized_limit = 0
            normalized_limit = min(max(1, normalized_limit or search_cfg.default_limit), MAX_SEARCH_LIMIT)

            matches = identity_si.search_infos(
                infos,
                query=str(query or ""),
                title=str(title or ""),
                keyword=str(keyword or ""),
                limit=normalized_limit,
                threshold=search_cfg.match_threshold,
            )
            if not matches:
                return _unavailable_result(
                    "没有匹配的自我信息。可放宽关键词（改用 query 通用词），"
                    "或检查 infos 配置里的标题/关键词拼写。"
                )

            serialized = [
                {
                    "title": info.title,
                    "keywords": list(info.keywords),
                    "full_information": info.full_information,
                }
                for _score, info in matches
            ]
            return {
                "success": True,
                "content": identity_si.format_search_results(matches),
                "matches": serialized,
                "total_count": len(infos),
            }
        except Exception as exc:  # noqa: BLE001 - 工具异常必须转可读错误
            self._log("error", "search_self_information 异常：%s: %s", type(exc).__name__, exc)
            return _unavailable_result(f"自我信息检索暂时不可用：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Tool：浏览图库

    @Tool(
        "view_all_image",
        description=(
            "浏览 Bot 的人设图库（缩略图分页）。当你想回忆或展示自己的形象、"
            "或需要为后续选图（get_self_image）挑一张时调用。"
            "每页最多 10 张，可用 page 翻页；返回清单里的 index/name/id 以及装扮标签（tags）"
            "可用于 get_self_image 或 compare_with_self_image 指定参考图。"
        ),
        parameters=[
            _tool_param("page", ToolParamType.INTEGER, "要浏览的页码，从 1 开始", False),
        ],
    )
    async def view_all_image(self, page: int = 1, **kwargs: Any) -> Dict[str, Any]:
        """分页返回人设图缩略图。"""
        del kwargs
        try:
            page_cfg = self.config.identity_image
            records = await self._scan_gallery_now()
            if not records:
                image_dir = self._resolve_configured_dir(page_cfg.image_dir, DEFAULT_IMAGE_DIR)
                return _unavailable_result(
                    "人设图库为空。请把人设图片放入插件目录的 self_image/ 文件夹后重试"
                    "（支持 jpg/png/webp/gif/bmp）。",
                    images=[],
                )

            page_records, total_pages, actual_page, clamped = gallery_si.page_slice(
                records, page, page_cfg.page_size
            )
            content_items: List[Dict[str, Any]] = []
            serialized: List[Dict[str, Any]] = []
            image_lines: List[str] = []
            for record in page_records:
                serialized.append(record.to_dict())
                tag_part = f"｜标签：{record.tag_text}" if record.tags else ""
                default_part = "｜基准参考图" if record.is_default else ""
                image_lines.append(f"{record.index}. {record.name}（id: {record.image_id}{tag_part}{default_part}）")
                if record.thumbnail_ok and record.thumbnail_path is not None:
                    thumb = await asyncio.to_thread(imageio_si.read_image_file, record.thumbnail_path)
                    if thumb is not None:
                        image_format, image_base64 = thumb
                        content_items.append(
                            {
                                "type": "image",
                                "data": image_base64,
                                "mime_type": imageio_si.mime_type(image_format),
                                "name": f"thumb_{record.index}_{record.name}.png",
                                "metadata": {
                                    "source": PLUGIN_ID,
                                    "usage": "self_identity_thumbnail",
                                    "image_index": record.index,
                                    "image_id": record.image_id,
                                    "image_name": record.name,
                                    "image_tags": list(record.tags),
                                },
                            }
                        )

            clamp_note = f"（请求的页码超出范围，已夹取到第 {actual_page} 页）" if clamped else ""
            tags_all = gallery_si.available_tags(records)
            tag_hint = (
                "可按标签取图（get_self_image / compare_with_self_image 的 tag 参数，多个标签是 AND）："
                + "、".join(tags_all)
                if tags_all
                else "在文件名里加 # 标签即可按服饰/风格取图，例如「小镜#默认,制服.jpg」"
            )
            header = (
                f"人设图库第 {actual_page}/{total_pages} 页，共 {len(records)} 张{clamp_note}。"
                "可用 get_self_image 的 tag / image_index / image_name（或 id）获取原图，"
                "image_name 传 “random” 可随机抽一张。"
                f"{tag_hint}。"
            )
            content = header + "\n" + "\n".join(image_lines)
            return {
                "success": True,
                "content": content,
                "page": actual_page,
                "page_size": page_cfg.page_size,
                "total_pages": total_pages,
                "total_count": len(records),
                "images": serialized,
                "content_items": content_items,
            }
        except Exception as exc:  # noqa: BLE001
            self._log("error", "view_all_image 异常：%s: %s", type(exc).__name__, exc)
            return _unavailable_result(f"浏览人设图库失败：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Tool：取原图

    @Tool(
        "get_self_image",
        description=(
            "获取某张 Bot 人设图的原始大小版本。"
            "可按服饰/风格标签挑图（tag，如「制服」「夏日」「女仆」，多标签为 AND），"
            "也可用 image_index / image_name（可从 view_all_image 的结果里取，image_name 传 “random” 随机抽一张）。"
            "都不传时优先给「默认」标记的基准形象。拿到后请把这张图作为自己的形象参考。"
        ),
        parameters=[
            _tool_param("image_index", ToolParamType.INTEGER, "图片序号，从 1 开始", False),
            _tool_param("image_name", ToolParamType.STRING, "图片文件名或 id，或 “random”", False),
            _tool_param("tag", ToolParamType.STRING, "按服饰/风格标签挑图，如“制服”或“夏日,海边”，可为空", False),
        ],
    )
    async def get_self_image(
        self,
        image_index: int = 0,
        image_name: str = "",
        tag: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """返回指定人设图原图。"""
        del kwargs
        try:
            record, data, mime, error = await self._resolve_self_image_bytes(
                image_index=image_index, image_name=image_name, tag=tag
            )
            if data is None or record is None:
                return _unavailable_result(error)
            image_format = imageio_si.sniff_format(data, imageio_si.format_from_name(record.name))
            image_base64 = imageio_si.to_base64(data)
            tag_note = f"（装扮标签：{record.tag_text}）" if record.tags else ""
            return {
                "success": True,
                "content": (
                    f"已返回第 {record.index} 张 Bot 人设原图：{record.name}{tag_note}。"
                    "请把这张图作为自我形象参考：同一个角色可能有多套装扮，服装不同不代表不是同一个人，"
                    "判定以发色／发型／瞳色／五官／气质为准。"
                ),
                "image_index": record.index,
                "image_id": record.image_id,
                "image_name": record.name,
                "image_tags": list(record.tags),
                "is_default_reference": record.is_default,
                "image_format": image_format,
                "image_base64": image_base64,
                "mime_type": mime or imageio_si.mime_type(image_format),
                "content_items": [
                    {
                        "type": "image",
                        "data": image_base64,
                        "mime_type": mime or imageio_si.mime_type(image_format),
                        "name": record.name,
                        "metadata": {
                            "source": PLUGIN_ID,
                            "usage": "self_identity_reference",
                            "image_index": record.index,
                            "image_id": record.image_id,
                            "image_name": record.name,
                            "image_tags": list(record.tags),
                        },
                    }
                ],
            }
        except Exception as exc:  # noqa: BLE001
            self._log("error", "get_self_image 异常：%s: %s", type(exc).__name__, exc)
            return _unavailable_result(f"人设图片工具暂时不可用：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Tool：QQ 头像

    @Tool(
        "get_self_avatar",
        description=(
            "获取 Bot 自己的 QQ 头像（根据 bot.qq_account，24 小时缓存）。"
            "当需要查看、展示或引用自己的头像时调用。"
        ),
        parameters=[],
    )
    async def get_self_avatar(self, **kwargs: Any) -> Dict[str, Any]:
        """读取 bot.qq_account → qlogo.cn 高清头像（带磁盘缓存）。"""
        del kwargs
        try:
            try:
                config_value = await self.ctx.config.get("bot.qq_account", "")
            except Exception as exc:  # noqa: BLE001
                return _unavailable_result(f"读取 bot.qq_account 失败：{type(exc).__name__}: {exc}")

            qq_account = str(config_value or "").strip()
            if qq_account in {"", "0"}:
                return _unavailable_result("当前未配置 bot.qq_account，无法获取自己的 QQ 头像。")
            if not qq_account.isdigit():
                return _unavailable_result(f"bot.qq_account 不是有效 QQ 号：{qq_account}")

            avatar_url = fetch_si.build_qq_avatar_url(qq_account)
            cache = self._get_avatar_cache()
            # v1.3.2：缓存读写是磁盘 IO，移入线程池（审计第 13 项的口径统一）
            entry = await asyncio.to_thread(cache.load, qq_account) if cache is not None else None
            cache_state = "缓存命中"
            if entry is not None:
                image_format = entry.image_format
                image_bytes = entry.data
            else:
                image_bytes, content_type = await fetch_si.download_image_bytes(
                    avatar_url,
                    timeout_seconds=fetch_si.DEFAULT_TIMEOUT_SECONDS,
                    log=self._log,
                )
                image_format = imageio_si.sniff_format(
                    image_bytes, fetch_si.guess_format_from_content_type(content_type)
                )
                if cache is not None:
                    await asyncio.to_thread(cache.store, qq_account, image_bytes, image_format)
                cache_state = "已下载并缓存"

            image_base64 = await asyncio.to_thread(imageio_si.to_base64, image_bytes)
            mime = imageio_si.mime_type(image_format)
            suffix = imageio_si.suffix_for_format(image_format)
            self._log(
                "info",
                "get_self_avatar：qq=%s，%s，format=%s，%d 字节",
                qq_account,
                cache_state,
                image_format,
                len(image_bytes),
            )
            return {
                "success": True,
                "content": f"已获取 Bot 自己的 QQ 头像（QQ：{qq_account}，{cache_state}）。",
                "qq_account": qq_account,
                "avatar_url": avatar_url,
                "image_format": image_format,
                "image_base64": image_base64,
                "mime_type": mime,
                "content_items": [
                    {
                        "type": "image",
                        "data": image_base64,
                        "mime_type": mime,
                        "name": f"self_avatar_{qq_account}.{suffix}",
                        "metadata": {
                            "source": PLUGIN_ID,
                            "usage": "self_avatar",
                            "qq_account": qq_account,
                            "avatar_url": avatar_url,
                        },
                    }
                ],
            }
        except fetch_si.FetchUnavailableError as exc:
            return _unavailable_result(str(exc))
        except fetch_si.DownloadError as exc:
            self._log("warning", "get_self_avatar 下载失败：%s", exc)
            return _unavailable_result(f"QQ 头像下载失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self._log("error", "get_self_avatar 异常：%s: %s", type(exc).__name__, exc)
            return _unavailable_result(f"获取自己的头像失败：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Tool：声明校验

    @Tool(
        "verify_self_claim",
        description=(
            "校验一句关于 Bot 自己的说法与档案是否一致，返回「符合 / 冲突 / 档案无记录」以及依据条目。"
            "当有人质疑你的设定（“你不是短发吗”）、或你不确定某个说法是否符合自己的人设时调用，"
            "用来防止人设漂移——先查档案再回答，不要凭印象。"
        ),
        parameters=[
            _tool_param(
                "claim",
                ToolParamType.STRING,
                "要校验的说法，例如「我是小镜，浅蓝长发」",
                True,
            ),
        ],
    )
    async def verify_self_claim(self, claim: str = "", **kwargs: Any) -> Dict[str, Any]:
        """把一句声明判成 符合 / 冲突 / 档案无记录，并附依据条目。"""
        del kwargs
        try:
            infos = self._infos or self._reload_infos()
            verdict = identity_si.verify_claim(
                infos,
                str(claim or ""),
                profile=self.config.profile,
                threshold=self.config.search.match_threshold,
            )
            evidence = [
                {"title": info.title, "full_information": info.full_information, "score": round(score, 1)}
                for score, info in verdict.evidence
            ]
            return {
                "success": True,
                "content": verdict.summary(),
                "state": verdict.state,
                "conflicts": list(verdict.conflicts),
                "evidence": evidence,
                "claims_checked": len(infos),
            }
        except Exception as exc:  # noqa: BLE001
            self._log("error", "verify_self_claim 异常：%s: %s", type(exc).__name__, exc)
            return _unavailable_result(f"声明校验暂时不可用：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Tool：视觉比对

    @Tool(
        "compare_with_self_image",
        description=(
            "调用视觉模型，把一张图片与 Bot 的人设参考图比对，判断是否为同一个角色形象，"
            "给出相符点与差异点。判定依据是发色／发型／瞳色／五官／气质；"
            "同一个角色的不同服饰/画风不算冲突（服装差异会写进 differences）。"
            "参考图默认用「基准」那张，也可用 reference_tag（如“制服”“夏日”“女仆”）指定装扮。"
            "图片可以传 image_url / image_base64；都不传时会自动取当前会话里最近的图片。"
            "当有人发图问「这是你吗」「画得像不像你」「这张对不对」时调用。"
        ),
        parameters=[
            _tool_param("image_url", ToolParamType.STRING, "待判断图片的 http(s) 链接，可为空", False),
            _tool_param("image_base64", ToolParamType.STRING, "待判断图片的 Base64 或 data URL，可为空", False),
            _tool_param("image_index", ToolParamType.INTEGER, "用人设图库第几张做参考（留空则用基准参考图）", False),
            _tool_param("image_name", ToolParamType.STRING, "用人设图库哪张做参考（文件名 / id / random），可为空", False),
            _tool_param("reference_tag", ToolParamType.STRING, "按装扮标签选参考图，如“制服”或“夏日,海边”，可为空", False),
            _tool_param("target_description", ToolParamType.STRING, "对这张图的补充描述，可为空", False),
        ],
    )
    async def compare_with_self_image(
        self,
        image_url: str = "",
        image_base64: str = "",
        image_index: int = 0,
        image_name: str = "",
        reference_tag: str = "",
        target_description: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """VLM 比对：待判断图片 ↔ 人设参考图。"""
        try:
            vision_cfg = self._vision_cfg()
            if not vision_cfg.enabled:
                return _unavailable_result("视觉比对功能已关闭（WebUI 的「视觉比对」节里可开启）。")

            # ① 待判断图片：显式 URL/Base64 → 入站原文兜底 → 最近消息（§72）
            target_bytes: Optional[bytes] = None
            target_mime = "image/png"
            target_source = ""
            if str(image_url or "").strip():
                target_bytes, target_mime, error = await self._load_image_reference("url", str(image_url).strip())
                target_source = "参数 image_url"
                if target_bytes is None:
                    return _unavailable_result(f"待判断图片读取失败：{error}")
            elif str(image_base64 or "").strip():
                target_bytes, target_mime, error = await self._load_image_reference(
                    "base64", str(image_base64).strip()
                )
                target_source = "参数 image_base64"
                if target_bytes is None:
                    return _unavailable_result(f"待判断图片读取失败：{error}")
            else:
                target_bytes, target_mime, error = await self._message_image_from_kwargs(kwargs)
                if target_bytes is not None:
                    target_source = "本条消息里的图片"
                else:
                    target_bytes, target_mime, recent_error = await self._recent_image_from_stream(
                        self._stream_id(kwargs)
                    )
                    if target_bytes is not None:
                        target_source = "最近消息里的图片"
                    else:
                        return _unavailable_result(
                            f"没有拿到可比对的图片：{error}；{recent_error}。"
                            "可以先用 view_all_image 确认图库，或直接传 image_url / image_base64。"
                        )

            # ② 人设参考图（默认用「基准」那张；可用参考标签指定装扮）
            self_record, self_bytes, self_mime, self_error = await self._resolve_self_image_bytes(
                image_index=image_index, image_name=image_name, tag=reference_tag
            )
            if self_bytes is None or self_record is None:
                return _unavailable_result(f"没有人设参考图可用于比对：{self_error}")

            # ③ 两个超时里短的必须先触发（运行期夹取 + 生效值外显）
            timeout_seconds, clamped = self._effective_image_timeout()
            max_tokens = self._effective_max_tokens()
            extra_hint = self._profile_hint(target_description)
            result = await compare_si.compare_images(
                self._generate_vlm,
                message_image=target_bytes,
                self_image=self_bytes,
                message_mime=target_mime,
                self_mime=self_mime,
                self_image_name=self_record.name,
                reference_tags=self_record.tags,
                ignore_outfit=bool(vision_cfg.ignore_outfit_differences),
                extra_hint=extra_hint,
                task_name=str(vision_cfg.task_name or ""),
                model_name=str(vision_cfg.model_name or ""),
                timeout_seconds=timeout_seconds,
                max_tokens=max_tokens,
                temperature=float(vision_cfg.temperature or 0.0),
            )
            clamp_note = f"（单图超时已被外层预算夹取为 {timeout_seconds:.0f}s）" if clamped else ""
            reference_note = self_record.name + (
                f"（装扮：{self_record.tag_text}）" if self_record.tags else ""
            )
            return {
                "success": True,
                "content": (
                    f"{result.summary}。图片来源：{target_source}；参考图：{reference_note}{clamp_note}"
                ),
                "verdict": result.verdict,
                "same_person": result.same_person,
                "confidence": round(result.confidence, 3),
                "similarities": list(result.similarities),
                "differences": list(result.differences),
                "image_source": target_source,
                "reference_image": self_record.name,
                "reference_tags": list(self_record.tags),
                "outfit_differences_ignored": bool(vision_cfg.ignore_outfit_differences),
                "effective_timeout_seconds": timeout_seconds,
                "timeout_clamped": clamped,
            }
        except compare_si.VisionOutputError as exc:
            self._log("warning", "compare_with_self_image 输出不可解析：%s", exc)
            return _unavailable_result(
                f"视觉模型输出无法解析：{exc}。原始输出开头：{compare_si.snippet(exc.raw, 120)}",
                raw_excerpt=compare_si.snippet(exc.raw, 400),
            )
        except compare_si.VisionError as exc:
            self._log("warning", "compare_with_self_image 调用失败：%s", exc)
            return _unavailable_result(
                f"视觉比对失败：{exc}。可在 WebUI 的「视觉比对」节里换 task_name / 具体 model_name 后重试。"
            )
        except Exception as exc:  # noqa: BLE001
            self._log("error", "compare_with_self_image 异常：%s: %s", type(exc).__name__, exc)
            return _unavailable_result(f"视觉比对暂时不可用：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Command：状态

    @Command(
        "persona_status",
        description="查看自我身份档案状态",
        pattern=r"^\s*[/／]\s*(?:人设状态|自我状态)\s*$",
    )
    async def cmd_persona_status(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """汇报条目数 / 图库数 / 配置版本 / 最近刷新时间。"""
        try:
            infos = self._infos or self._reload_infos()
            if not self._gallery and not self._last_refresh_ts:
                await self._refresh_gallery()
            image_cfg = self.config.identity_image
            lines = [
                "🪞 自我身份档案状态",
                f"配置版本：{self.config.plugin.config_version}",
                f"档案条目：{len(infos)} 条（空条目已过滤）",
                identity_si.profile_summary(self.config.profile),
                f"人设图库：{len(self._gallery)} 张"
                + self._gallery_stats_note(),
                f"图库目录：{image_cfg.image_dir}（缩略图：{image_cfg.thumbnail_dir}）",
                f"缩略图引擎：{'就绪' if gallery_si.PIL_AVAILABLE else 'Pillow 缺失，图库降级为纯文本'}",
                f"最近刷新：{self._format_ts(self._last_refresh_ts)}",
            ]
            lines.extend(self._gallery_status_lines())
            lines.extend(self._card_status_lines())
            lines.extend(self._vision_status_lines())
            return await self._reply(kwargs, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self._log("error", "cmd_persona_status 异常：%s: %s", type(exc).__name__, exc)
            return await self._reply(kwargs, f"查询状态失败：{type(exc).__name__}: {exc}")

    def _gallery_status_lines(self) -> List[str]:
        """图库标签与基准参考图状态行（多套装扮时的可发现性）。"""
        records = self._gallery
        if not records:
            return []
        default_tag = str(self.config.identity_image.default_reference_tag or gallery_si.DEFAULT_TAG)
        tags = gallery_si.available_tags(records)
        default = gallery_si.find_default(records, default_tag=default_tag)
        lines = [
            "装扮标签：" + ("、".join(tags) if tags else f"无（在文件名里加 # 标签即可，如「小镜#{default_tag},制服.jpg」）")
        ]
        lines.append(
            f"基准参考图：{default.name}"
            if default is not None
            else f"基准参考图：未标记（多张图时需显式给 tag/index/name，或给一张加「{default_tag}」标签）"
        )
        return lines

    def _card_status_lines(self) -> List[str]:
        """身份档案卡状态行（v1.3.0：卡片是给用户复制的产物，不再注入模型请求）。"""
        card = self._card_text()
        if not card:
            card_line = "身份档案卡：空（请在 WebUI 的「身份档案」节填 profile，或加 infos 条目）"
        else:
            card_line = f"身份档案卡：{len(card)} 字（用 /人设卡片 复制到宿主 personality 或 behavior_style）"
        return [card_line, "运行模式：被动（不改写任何模型请求）"]

    def _vision_status_lines(self) -> List[str]:
        """视觉比对状态行：生效值必须外显，且标出与配置不一致（§70.2）。"""
        cfg = self._vision_cfg()
        if not cfg.enabled:
            return ["视觉比对：已关闭"]
        timeout_seconds, clamped = self._effective_image_timeout()
        max_tokens = self._effective_max_tokens()
        timeout_text = f"单图超时 {timeout_seconds:.0f}s"
        if clamped:
            timeout_text += f"（配置 {cfg.image_timeout_seconds:.0f}s 已被外层预算夹取："
            timeout_text += f"消息预算 {cfg.message_timeout_seconds:.0f}s×80% 与 RPC {LLM_RPC_TIMEOUT_MS // 1000}s−10s 取小）"
        token_text = f"max_tokens {max_tokens}"
        if max_tokens > int(cfg.max_tokens or 0):
            token_text += f"（配置 {int(cfg.max_tokens or 0)} 低于下限，已抬升）"
        return [
            f"视觉比对：开启（task_name={cfg.task_name or 'vlm'}"
            + (f"，model_name={cfg.model_name}" if cfg.model_name else "")
            + "）",
            f"视觉参数：{timeout_text}；{token_text}",
        ]

    def _gallery_stats_note(self) -> str:
        """图库统计附注。"""
        stats = self._gallery_stats or {}
        notes: List[str] = []
        if stats.get("generated"):
            notes.append(f"本次生成缩略图 {stats['generated']} 张")
        if stats.get("thumb_failed"):
            notes.append(f"缩略图失败 {stats['thumb_failed']} 张")
        if not stats.get("pil_available", True):
            notes.append("Pillow 缺失")
        return f"（{'；'.join(notes)}）" if notes else ""

    # ---------------------------------------------------------- Command：刷新

    @Command(
        "persona_refresh",
        description="重扫人设图库并重载档案配置（热生效）",
        pattern=r"^\s*[/／]\s*(?:人设刷新|刷新人设)\s*$",
    )
    async def cmd_persona_refresh(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """重扫图库 + 重载 infos，热生效（无需重启）。"""
        try:
            infos = self._reload_infos()
            count, stats = await self._refresh_gallery()
            lines = [
                "🔄 自我身份档案已刷新",
                f"档案条目：{len(infos)} 条",
                f"人设图库：{count} 张（生成缩略图 {stats.get('generated', 0)} 张"
                f"，失败 {stats.get('thumb_failed', 0)} 张）",
                f"刷新时间：{self._format_ts(self._last_refresh_ts)}",
            ]
            return await self._reply(kwargs, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self._log("error", "cmd_persona_refresh 异常：%s: %s", type(exc).__name__, exc)
            return await self._reply(kwargs, f"刷新失败：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Command：图库清单

    @Command(
        "persona_gallery",
        description="以文本形式列出人设图库（给人看）",
        pattern=r"^\s*[/／]\s*(?:人设图库|图库清单)\s*$",
    )
    async def cmd_persona_gallery(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """文本图库清单。"""
        try:
            records = await self._scan_gallery_now()
            return await self._reply(kwargs, gallery_si.render_gallery_text(records))
        except Exception as exc:  # noqa: BLE001
            self._log("error", "cmd_persona_gallery 异常：%s: %s", type(exc).__name__, exc)
            return await self._reply(kwargs, f"读取图库失败：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- Command：身份档案卡

    @Command(
        "persona_card",
        description="输出可复制到宿主配置的身份档案卡",
        pattern=r"^\s*[/／]\s*(?:人设卡片|身份卡片|人设卡)\s*$",
    )
    async def cmd_persona_card(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """输出身份档案卡（v1.3.0：插件生成、宿主承载，不再注入模型请求）。"""
        try:
            card = self._card_text()
            if not card:
                return await self._reply(
                    kwargs,
                    "身份档案卡是空的。请在 WebUI 的「身份档案」节填 profile（名字/身份定位/外貌…）"
                    "或加 infos 条目后重试。",
                )
            text = "\n".join([
                "🪞 身份档案卡（复制下面分隔线之间的内容）",
                "用途：粘进宿主 bot_config.toml 的 [personality]，每轮都会生效：",
                "· 想让它影响「怎么说话」→ 填 personality（replyer 的 {identity}）",
                "· 想让它影响「怎么决策」→ 填 behavior_style（planner 的 {behavior_style}，"
                "planner 能调 search_self_information 等工具）",
                "本插件不再主动注入，避免与宿主 personality 出现同一诉求的两套措辞。",
                "──────────",
                card,
                "──────────",
            ])
            return await self._reply(kwargs, text)
        except Exception as exc:  # noqa: BLE001
            self._log("error", "cmd_persona_card 异常：%s: %s", type(exc).__name__, exc)
            return await self._reply(kwargs, f"生成身份档案卡失败：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------- 配置兜底工具

    @staticmethod
    def _merge_with_defaults(defaults: Dict[str, Any], user: Mapping[str, Any]) -> Dict[str, Any]:
        """浅层递归合并：默认值为骨架，用户已有值覆盖。"""
        merged = copy.deepcopy(defaults)
        for key, value in dict(user or {}).items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
                merged[key] = SelfIdentityPlugin._merge_with_defaults(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _reset_path(candidate: Dict[str, Any], defaults: Dict[str, Any], loc: Tuple[Any, ...]) -> bool:
        """把校验错误 loc 指向的字段重置为默认值；返回是否成功。"""
        if not loc:
            return False
        node: Any = candidate
        source: Any = defaults
        for part in loc[:-1]:
            if isinstance(part, int) and isinstance(node, list):
                if isinstance(source, list) and 0 <= part < len(node) and part < len(source):
                    node = node[part]
                    source = source[part]
                    continue
                return False
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
            source = source.get(part) if isinstance(source, dict) else None
        last = loc[-1]
        if isinstance(last, int) and isinstance(node, list):
            if 0 <= last < len(node):
                fallback = source[last] if isinstance(source, list) and last < len(source) else None
                node[last] = copy.deepcopy(fallback)
                return True
            return False
        if isinstance(node, dict) and isinstance(last, str) and last in node:
            fallback = source.get(last) if isinstance(source, dict) else None
            node[last] = copy.deepcopy(fallback)
            return True
        return False

    @staticmethod
    def _repair_config(config_class: Any, merged: Dict[str, Any], defaults: Dict[str, Any]) -> Optional[Any]:
        """逐字段修复配置：定点还原非法字段后重试，直到通过或修不动。"""
        if config_class is None:
            return None
        candidate = copy.deepcopy(merged)
        for _round in range(24):  # 字段数量级上限，兼作死循环保险
            try:
                return config_class.model_validate(candidate)
            except Exception as exc:  # noqa: BLE001
                errors_fn = getattr(exc, "errors", None)
                if not callable(errors_fn):
                    return None
                try:
                    error_list = list(errors_fn())
                except Exception:  # noqa: BLE001
                    return None
                if not error_list:
                    return None
                repaired = False
                for err in error_list:
                    if SelfIdentityPlugin._reset_path(candidate, defaults, tuple(err.get("loc") or ())):
                        repaired = True
                if not repaired:
                    return None
        return None

    def _build_default_instance(self) -> Optional[SelfIdentityConfig]:
        """构造默认配置实例（兜底用）。"""
        try:
            config_class = type(self).get_config_model()
            if config_class is None:
                return None
            return config_class.model_validate(type(self).build_default_config())
        except Exception:  # noqa: BLE001
            return None


def create_plugin() -> SelfIdentityPlugin:
    """创建插件实例。"""
    return SelfIdentityPlugin()
