# -*- coding: utf-8 -*-
"""VLM 比对纯模块：待判断图片 ↔ Bot 人设参考图，产出 verdict + 依据。

纯模块纪律：不 import maibot_sdk、不出现 ctx——``generate`` 回调由 plugin.py 注入。

沿用的真机教训（runtime-gotchas §69/§70）：
- 推理模型的**思考 token 也计入 max_tokens**（真机 220/700 都触顶）⇒ 运行期抬到下限，
  只按实际输出计费；
- 同一插件里两个超时**短的必须先触发** ⇒ 运行期夹取 + 生效值外显；
- 解析失败要分「空内容 / 跑偏（非 JSON）/ JSON 中途截断 / 结构不合法」四类，
  并把原始输出留在异常里（``VisionOutputError.raw``）；
- 跨进程 RPC 超时**不能靠 isinstance 认** ⇒ 三取一判据 ``is_timeout_error``；
- 判定词归一化**否定变体必须先于肯定词匹配**（v1.3.2：否则「不符合」被判成「符合」），
  且结构化 ``same_person`` 布尔字段优先于判定词，矛盾时留痕不静默。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Tuple

#: 输出 token 下限：低于它会被运行期抬到这个值（思考 token 也占额度）
VISION_MAX_TOKENS_FLOOR = 1600

#: 插件侧内层预算相对外层消息预算的比例（保证内层先触发）
INNER_TIMEOUT_RATIO = 0.8

#: verdict 取值
VERDICT_SAME = "符合"
VERDICT_CONFLICT = "冲突"
VERDICT_UNKNOWN = "无法判断"

GenerateFn = Callable[..., Awaitable[Any]]

_SNIPPET_RE = re.compile(r"\s+")


class VisionError(Exception):
    """视觉调用失败（依赖缺失/接口错误/超时）。"""


class VisionOutputError(VisionError):
    """模型输出不可解析。``raw`` 留住原始输出，供 /人设状态 展开排查。"""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = str(raw or "")


def snippet(text: str, limit: int = 60) -> str:
    """单行短摘要（日志/工具返回里用，避免刷屏）。"""
    value = _SNIPPET_RE.sub(" ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def is_timeout_error(exc: BaseException) -> bool:
    """超时判定三取一（§69.2：跨进程 RPCError 不是同一个类，不能只靠 isinstance）。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if any(hint in type(exc).__name__.lower() for hint in ("timeout", "e_timeout")):
        return True
    text = str(exc).lower()
    return any(hint in text for hint in ("timeout", "timed out", "e_timeout", "超时"))


def effective_max_tokens(configured: Any) -> int:
    """生效的 max_tokens：不低于下限（运行期抬升，改配置默认值对存量实例无效）。"""
    try:
        value = int(configured or 0)
    except (TypeError, ValueError):
        value = 0
    return max(value, VISION_MAX_TOKENS_FLOOR)


def effective_timeout(configured: Any, message_budget: Any) -> Tuple[float, bool]:
    """生效的单图超时 = min(配置值, 消息预算 × 0.8)。

    Returns:
        (生效秒数, 是否被外层预算夹取)
    """
    try:
        inner = float(configured or 0.0)
    except (TypeError, ValueError):
        inner = 0.0
    try:
        outer = float(message_budget or 0.0)
    except (TypeError, ValueError):
        outer = 0.0
    if inner <= 0:
        inner = 90.0
    if outer > 0:
        capped = outer * INNER_TIMEOUT_RATIO
        if capped < inner:
            return max(1.0, capped), True
    return inner, False


def _truthy(value: Any) -> bool:
    """显式枚举真值（§72.4：模型很爱写字符串 "false"）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("true", "1", "yes", "y", "是", "一致", "相同")


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠 JSON 对象（容忍 ```json 围栏与前后杂讯）。"""
    value = str(text or "").strip()
    if not value:
        return None
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.lower().startswith("json"):
            value = value[4:].strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(value[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def diagnose_unparsable(content: str, *, max_tokens: int = 0) -> str:
    """把「解析不出来」拆成四类，让用户知道该调哪个旋钮（§70.1 第 3 条）。"""
    text = str(content or "").strip()
    limit = f"（当前 max_tokens={max_tokens}）" if max_tokens else ""
    if not text:
        return "模型返回了空内容（可能是 Provider 异常或任务模型缺失）"
    head = text.find("{")
    if head < 0:
        return f"模型没有按 JSON 回答（开头是：{snippet(text, 40)}）"
    if "}" not in text[head:]:
        return f"输出在 JSON 中途被截断，多半是达到了 max_tokens 上限{limit}，请调大 vision.max_tokens"
    return f"模型给的 JSON 结构不合法（开头是：{snippet(text, 40)}）"


_VERDICT_ALIASES = {
    "符合": VERDICT_SAME, "一致": VERDICT_SAME, "相同": VERDICT_SAME, "相似": VERDICT_SAME,
    "same": VERDICT_SAME, "match": VERDICT_SAME, "yes": VERDICT_SAME, "true": VERDICT_SAME,
    "冲突": VERDICT_CONFLICT, "不一致": VERDICT_CONFLICT, "不同": VERDICT_CONFLICT,
    "different": VERDICT_CONFLICT, "mismatch": VERDICT_CONFLICT, "no": VERDICT_CONFLICT,
    "false": VERDICT_CONFLICT,
    "无法判断": VERDICT_UNKNOWN, "不确定": VERDICT_UNKNOWN, "unknown": VERDICT_UNKNOWN,
    "unsure": VERDICT_UNKNOWN,
}

#: 判定词的**否定变体**：必须先于肯定词做子串匹配（v1.3.2 修复的静默反转缺陷）。
#:
#: 「不符合」包含「符合」、「not same」包含「same」——旧的字典序子串兜底会先命中
#: 肯定词，把冲突判成符合（实测：不符合/不相同/并不一致/not same/no match 全部误判）。
#: 刻意**不收**「不像」「并非完全一致」这类修饰性表述：它们常出现在 differences 的
#: 特征描述句里，进了判定词字段也不该单独定案。
_NEGATIVE_VERDICTS: Tuple[str, ...] = (
    # 中文否定变体
    "不符合", "不符", "不相同", "不一致", "不相似", "不一样",
    "并非同一", "并不是同一", "不是同一", "并非相同", "并不相同", "不同一",
    # 英文否定变体（mismatch 本身含 match，也必须先判）
    "not same", "not the same", "no match", "not a match", "not matching",
    "mismatch", "doesn't match", "does not match", "do not match", "don't match",
    "not similar", "not identical",
)


def normalize_verdict(value: Any, *, same_person: Optional[bool] = None) -> str:
    """规范化 verdict；缺省时用布尔字段兜底，都没有则「无法判断」。

    匹配顺序：全等别名 → 否定变体子串 → 肯定词子串 → 布尔兜底。
    否定变体必须排在肯定词之前（见 ``_NEGATIVE_VERDICTS`` 的说明）。
    """
    text = str(value or "").strip().lower()
    if text in _VERDICT_ALIASES:
        return _VERDICT_ALIASES[text]
    for negative in _NEGATIVE_VERDICTS:
        if negative in text:
            return VERDICT_CONFLICT
    for alias, verdict in _VERDICT_ALIASES.items():
        if alias and alias in text:
            return verdict
    if same_person is True:
        return VERDICT_SAME
    if same_person is False:
        return VERDICT_CONFLICT
    return VERDICT_UNKNOWN


def _text_list(value: Any, limit: int = 6) -> Tuple[str, ...]:
    """把模型给的列表字段规范化为短字符串元组。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    items = []
    for entry in value[:limit]:
        text = snippet(str(entry or ""), 120)
        if text:
            items.append(text)
    return tuple(items)


def _image_part(image_bytes: bytes, mime: str = "image/png") -> Dict[str, Any]:
    """构造视觉请求的图片段（与真机验证过的形态一致）。"""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/png'};base64,{encoded}"}}


@dataclass(frozen=True)
class CompareResult:
    """一次人设图比对的结果。"""

    verdict: str = VERDICT_UNKNOWN
    same_person: bool = False
    confidence: float = 0.0
    similarities: Tuple[str, ...] = ()
    differences: Tuple[str, ...] = ()
    raw: str = ""

    @property
    def summary(self) -> str:
        """给模型/用户看的一句话结论。"""
        parts = [f"判定：{self.verdict}"]
        if self.confidence > 0:
            parts.append(f"置信度 {self.confidence:.2f}")
        if self.similarities:
            parts.append("相符点：" + "；".join(self.similarities))
        if self.differences:
            parts.append("差异点：" + "；".join(self.differences))
        if self.verdict == VERDICT_UNKNOWN and not self.similarities and not self.differences:
            parts.append("模型没有给出可用的比对依据")
        return "。".join(parts)


COMPARE_SYSTEM_INSTRUCTION = (
    "你是图像比对助手。第一张图是【待判断图片】，第二张图是【Bot 人设参考图】。"
    "请判断两张图是否为同一个角色形象，判定依据是**发色／发型（含呆毛、发饰）／瞳色／五官与整体气质**。"
    "只依据可见证据，不要脑补。"
    "必须只输出一个 JSON 对象，不要输出解释文字或代码块，字段如下："
    '{"same_person": true|false, "verdict": "符合"|"冲突"|"无法判断", '
    '"confidence": 0.0~1.0, "similarities": ["..."], "differences": ["..."]}'
)

#: 多套装扮场景下的追加纪律（同一角色的不同服饰不应判冲突）
OUTFIT_TOLERANT_RULE = (
    "注意：同一个角色可能有多套服饰／不同画风的作品。"
    "如果两张图的服装、配饰、场景或画风不同，但发色／发型／瞳色／五官与气质一致，"
    "应判 \"符合\"，并把差异写进 differences（例如「服装不同：参考图是制服，待判断图是夏日装」）。"
    "只有**角色本身特征**（发色／瞳色／五官／气质）不一致时才判 \"冲突\"。"
)

#: 需要按服装否定时的相反纪律
OUTFIT_STRICT_RULE = (
    "注意：参考图是该角色的特定形象，服装／配饰也是判定依据之一；"
    "服装明显不符时应写进 differences，必要时可判 \"冲突\"。"
)


def build_compare_prompt(
    self_image_name: str = "",
    extra_hint: str = "",
    reference_tags: Sequence[str] = (),
    ignore_outfit: bool = True,
) -> str:
    """构造比对提示词（参考图标签、补充提示、装扮宽松度都可选）。

    ``reference_tags`` 用来告诉模型参考图是哪套装扮（如「制服」「夏日」），
    否则模型容易把「服装不同」直接判成不同角色。
    """
    lines = [COMPARE_SYSTEM_INSTRUCTION]
    lines.append(OUTFIT_TOLERANT_RULE if ignore_outfit else OUTFIT_STRICT_RULE)
    if str(self_image_name or "").strip():
        lines.append(f"参考图文件名：{snippet(str(self_image_name), 80)}")
    tags = [str(tag).strip() for tag in (reference_tags or ()) if str(tag).strip()]
    if tags:
        lines.append(f"参考图的装扮／风格标签：{'、'.join(tags[:6])}（待判断图不必同样是这套装扮）")
    hint = str(extra_hint or "").strip()
    if hint:
        lines.append(f"补充信息（来自 Bot 档案，仅供参考）：{snippet(hint, 400)}")
    return "\n".join(lines)


async def compare_images(
    generate: GenerateFn,
    *,
    message_image: bytes,
    self_image: bytes,
    message_mime: str = "image/png",
    self_mime: str = "image/png",
    self_image_name: str = "",
    reference_tags: Sequence[str] = (),
    ignore_outfit: bool = True,
    extra_hint: str = "",
    task_name: str = "",
    model_name: str = "",
    timeout_seconds: float = 90.0,
    max_tokens: int = VISION_MAX_TOKENS_FLOOR,
    temperature: float = 0.0,
) -> CompareResult:
    """调视觉模型比对两张图。

    ``generate`` 是 plugin.py 注入的 ``llm.generate`` 适配回调（内部已带 RPC 超时）；
    这里的 ``timeout_seconds`` 是插件侧内层预算，必须小于 RPC 层才不会把真因盖掉。
    """
    if not message_image:
        raise VisionError("待判断图片内容为空")
    if not self_image:
        raise VisionError("人设参考图内容为空")

    effective_tokens = effective_max_tokens(max_tokens)
    kwargs: Dict[str, Any] = {
        "prompt": build_compare_prompt(
            self_image_name, extra_hint, reference_tags=reference_tags, ignore_outfit=ignore_outfit
        ),
        "images": [_image_part(message_image, message_mime), _image_part(self_image, self_mime)],
        "max_tokens": effective_tokens,
    }
    if task_name:
        kwargs["task_name"] = task_name
    if model_name:
        kwargs["model_name"] = model_name
    if temperature is not None:
        kwargs["temperature"] = temperature

    try:
        result = await asyncio.wait_for(generate(**kwargs), timeout=timeout_seconds + 10.0)
    except Exception as exc:  # noqa: BLE001 - 统一归类为 VisionError，超时单独标注
        if is_timeout_error(exc):
            raise VisionError(f"视觉请求超时（>{timeout_seconds:.0f}s）") from exc
        raise VisionError(f"视觉调用失败：{type(exc).__name__}: {exc}") from exc

    if not isinstance(result, dict) or not result.get("success"):
        reason = ""
        if isinstance(result, dict):
            reason = str(result.get("error") or result.get("reason") or "")
        raise VisionError(f"视觉任务未成功返回：{reason or '未知原因'}")

    raw = str(result.get("response") or result.get("content") or "")
    parsed = extract_json_object(raw)
    if parsed is None:
        raise VisionOutputError(diagnose_unparsable(raw, max_tokens=effective_tokens), raw=raw)

    # v1.3.2：结构化布尔字段优先于判定词。same_person 是 prompt schema 里的第一字段，
    # 判定词可能被模型写成否定变体或干脆写反——布尔在场时以它为准，
    # 与判定词矛盾时对齐 verdict 并把矛盾写进 differences 留痕（不静默篡改）。
    same_person_field = parsed.get("same_person")
    has_explicit_bool = same_person_field is not None and not (
        isinstance(same_person_field, str) and not same_person_field.strip()
    )
    same_person_bool = _truthy(same_person_field)
    verdict = normalize_verdict(parsed.get("verdict"), same_person=same_person_bool)
    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    similarities = _text_list(parsed.get("similarities"))
    differences = list(_text_list(parsed.get("differences")))
    if has_explicit_bool:
        if verdict in (VERDICT_SAME, VERDICT_CONFLICT) and (verdict == VERDICT_SAME) != same_person_bool:
            differences.append(
                f"模型输出矛盾：判定词「{verdict}」与 same_person={same_person_field} 不一致，已按布尔字段为准"
            )
            verdict = VERDICT_SAME if same_person_bool else VERDICT_CONFLICT
        final_same_person = same_person_bool
    else:
        final_same_person = verdict == VERDICT_SAME
    if not similarities and not differences and verdict == VERDICT_UNKNOWN:
        raise VisionOutputError("模型输出解析成功但没有任何比对依据", raw=raw)
    return CompareResult(
        verdict=verdict,
        same_person=final_same_person,
        confidence=confidence,
        similarities=similarities,
        differences=tuple(differences),
        raw=raw,
    )
