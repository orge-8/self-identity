# -*- coding: utf-8 -*-
"""身份档案纯模块：档案条目模型、profile 概览/身份卡渲染、检索打分、声明校验。

纯模块纪律：不 import maibot_sdk、不出现 ctx——可脱机单测。

检索规则（v1.0.0 定案）：
- 空条目守卫：title/keywords/full_information 全空的条目直接跳过；
- 多关键词 AND：title / keyword 参数按分隔符拆分，每个子词都必须对条目
  产生正向命中，否则整条排除；
- 打分阈值：总分须 ≥ match_threshold 才算命中（过滤 SequenceMatcher 噪声）。

v1.1.0 追加：
- ``build_identity_card``：~300 字紧凑身份卡（v1.3.0 起改为**给用户复制的产物**，
  由用户粘进宿主配置，不再由插件注入模型请求）；
- ``verify_claim``：把一句话判成 符合 / 冲突 / 档案无记录，附依据条目（防人设漂移）；
- ``neutralize``：把不可信文本里的分隔符硬中和，防提示注入（runtime-gotchas §27）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, List, Mapping, Tuple

#: 多关键词分隔符（中英文逗号/顿号/分号/斜杠/全角竖线/空白）
KEYWORD_SEPARATORS = r"[，,、；;/｜\s]+"

#: profile 节的字段（与 plugin.py 的 ProfileSectionConfig 一一对应）
PROFILE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("name", "名字"),
    ("role", "身份定位"),
    ("appearance", "外貌"),
    ("personality", "性格"),
    ("speech_style", "说话风格"),
    ("taboos", "忌讳"),
    ("background", "背景"),
)


@dataclass(frozen=True)
class IdentityInfo:
    """单条自我信息。"""

    title: str = ""
    keywords: Tuple[str, ...] = field(default=())
    full_information: str = ""

    @property
    def is_blank(self) -> bool:
        """空条目守卫：三个字段全空即视为无效条目。"""
        return not self.title.strip() and not any(k.strip() for k in self.keywords) and not self.full_information.strip()

    def normalized_keywords(self) -> Tuple[str, ...]:
        """小写化、去空后的关键词元组。"""
        return tuple(k.strip().lower() for k in self.keywords if k.strip())


def collect_infos(payload: Iterable[Any], log=None) -> List[IdentityInfo]:
    """把配置里的 infos（dict 或 pydantic 对象）归一为 IdentityInfo 列表。

    空条目在此处即被过滤；解析失败的字段按空串处理并记日志。
    """
    infos: List[IdentityInfo] = []
    for raw_index, raw in enumerate(payload or (), start=1):
        title = str(_pick(raw, "title") or "").strip()
        raw_keywords = _pick(raw, "keywords") or []
        keywords: List[str] = []
        if isinstance(raw_keywords, str):
            keywords = split_keywords(raw_keywords)
        else:
            for entry in raw_keywords:
                text = str(entry or "").strip()
                if text:
                    keywords.extend(split_keywords(text))
        info = IdentityInfo(
            title=title,
            keywords=tuple(dict.fromkeys(keywords)),
            full_information=str(_pick(raw, "full_information") or "").strip(),
        )
        if info.is_blank:
            if log is not None:
                # v1.3.2：报**原始序号**（此前用已保留条数+1，连续空条目会打出重复的「第 1 条」）
                log("info", f"第 {raw_index} 条自我信息为空，已跳过")
            continue
        infos.append(info)
    return infos


def split_keywords(text: str) -> List[str]:
    """按分隔符拆多关键词，去空去重（保序）。"""
    parts = [p.strip() for p in re.split(KEYWORD_SEPARATORS, str(text or "")) if p.strip()]
    return list(dict.fromkeys(parts))


def profile_summary(profile: Any) -> str:
    """profile 填写概览：'身份档案已填 3/7（名字、外貌、背景）' 的紧凑文本。"""
    if profile is None:
        return f"身份档案未填写（0/{len(PROFILE_FIELDS)}）"
    filled = [label for key, label in PROFILE_FIELDS if str(_pick(profile, key) or "").strip()]
    if not filled:
        return f"身份档案未填写（0/{len(PROFILE_FIELDS)}）"
    if len(filled) == len(PROFILE_FIELDS):
        return f"身份档案已完整填写（{len(filled)}/{len(PROFILE_FIELDS)}）"
    return "身份档案已填 {n}/{total}（{names}）".format(
        n=len(filled), total=len(PROFILE_FIELDS), names="、".join(filled)
    )


def _pick(raw: Any, key: str) -> Any:
    """兼容 dict 与 pydantic 对象两种形态取字段。"""
    if isinstance(raw, Mapping):
        return raw.get(key)
    return getattr(raw, key, None)


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _title_score(item_title: str, title_part: str) -> float:
    """单个 title 子词的命中分。"""
    if not title_part:
        return 0.0
    if item_title == title_part:
        return 120.0
    if title_part in item_title:
        return 90.0
    return _ratio(title_part, item_title) * 70.0


def _keyword_score(item_keywords: Tuple[str, ...], keyword_part: str) -> float:
    """单个 keyword 子词的命中分（取该子词与全部关键词的最高分）。"""
    if not keyword_part:
        return 0.0
    best = 0.0
    for item_keyword in item_keywords:
        if item_keyword == keyword_part:
            score = 85.0
        elif keyword_part in item_keyword or item_keyword in keyword_part:
            score = 60.0
        else:
            score = _ratio(keyword_part, item_keyword) * 35.0
        best = max(best, score)
    return best


def _query_score(info: IdentityInfo, item_title: str, item_keywords: Tuple[str, ...], query: str) -> float:
    """通用搜索词的命中分。"""
    if not query:
        return 0.0
    score = 0.0
    if query in item_title:
        score += 65.0
    if any(query in item_keyword for item_keyword in item_keywords):
        score += 55.0
    if query in info.full_information.lower():
        score += 35.0
    score += _ratio(query, item_title) * 25.0
    return score


def score_info_item(
    info: IdentityInfo,
    query: str = "",
    title: str = "",
    keyword: str = "",
) -> float:
    """计算单条信息与搜索条件的总分。

    AND 语义：title / keyword 拆出的每个子词都必须产生正向命中；
    任一子词 0 分即整条 0 分（排除）。query 保持单整体词模糊匹配。
    """
    item_title = _norm(info.title)
    item_keywords = info.normalized_keywords()

    total = 0.0
    if query.strip():
        total += _query_score(info, item_title, item_keywords, _norm(query))

    title_parts = split_keywords(title) if title.strip() else []
    if title_parts:
        part_scores = [_title_score(item_title, _norm(part)) for part in title_parts]
        if any(score <= 0.0 for score in part_scores):
            return 0.0
        total += sum(part_scores)

    keyword_parts = split_keywords(keyword) if keyword.strip() else []
    if keyword_parts:
        part_scores = [_keyword_score(item_keywords, _norm(part)) for part in keyword_parts]
        if any(score <= 0.0 for score in part_scores):
            return 0.0
        total += sum(part_scores)

    return total


def search_infos(
    infos: List[IdentityInfo],
    query: str = "",
    title: str = "",
    keyword: str = "",
    limit: int = 5,
    threshold: float = 15.0,
) -> List[Tuple[float, IdentityInfo]]:
    """检索档案：打分 → 阈值过滤 → 降序 → 截断。

    调用方保证 query/title/keyword 至少一个非空（空条件在 plugin 层拒绝）。
    """
    normalized_limit = max(1, int(limit or 1))
    scored: List[Tuple[float, IdentityInfo]] = []
    for info in infos:
        if info.is_blank:  # 双保险：collect_infos 已滤过一次
            continue
        score = score_info_item(info, query=query, title=title, keyword=keyword)
        if score > 0.0 and score >= threshold:
            scored.append((score, info))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    return scored[:normalized_limit]


def format_search_results(matches: List[Tuple[float, IdentityInfo]]) -> str:
    """把命中结果渲染为给模型/用户看的文本。"""
    lines: List[str] = [f"共找到 {len(matches)} 条与 Bot 自我信息相关的结果："]
    for index, (score, info) in enumerate(matches, start=1):
        keywords_text = "、".join(info.keywords) if info.keywords else "无"
        lines.extend(
            [
                "",
                f"{index}. 标题：{info.title or '未命名'}",
                f"关键词：{keywords_text}",
                f"全量信息：{info.full_information or '无'}",
            ]
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════ v1.1.0


#: 提示注入硬中和：连续 2 个以上的尖括号一律打散（runtime-gotchas §27）
_RE_ANGLE_RUN = re.compile(r"<{2,}|>{2,}")


def neutralize(text: str) -> str:
    """把不可信文本里的分隔符硬中和，避免闭合段落伪造指令。"""
    return _RE_ANGLE_RUN.sub(lambda m: "‹" * len(m.group(0)) if m.group(0)[0] == "<" else "›" * len(m.group(0)), str(text or ""))


def build_identity_card(
    profile: Any,
    *,
    infos: Iterable[IdentityInfo] = (),
    max_chars: int = 400,
) -> str:
    """渲染身份档案卡（**给用户复制的文本产物**，不是注入载荷）。

    v1.3.0 起本插件不改写任何模型请求。原因（宿主源码核实）：
    宿主的 replyer 每轮都用 ``[personality] personality`` 填模板里的 ``{identity}``、
    planner 每轮用 ``[personality] behavior_style`` 填 ``{behavior_style}``，
    插件再注入一份身份描述就是**同一诉求两份措辞**（风格约束还可能互相打架）；
    而 replyer 那条链路上没有工具定义，卡片里写「需要时调用 search_self_information」
    是一句无法执行的提示。

    因此改由「插件生成、宿主承载」：``/人设卡片`` 输出本函数的文本，
    用户粘进宿主配置即可每轮生效，且结构上不存在重复。
    """
    lines: List[str] = []
    name = str(_pick(profile, "name") or "").strip()
    if name:
        lines.append(f"我是{name}。")
    for key, label in PROFILE_FIELDS:
        if key == "name":
            continue
        value = str(_pick(profile, key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")

    info_list = [item for item in infos if not item.is_blank]
    if info_list:
        titles = "、".join(item.title or "未命名" for item in info_list[:6])
        lines.append(
            f"另有 {len(info_list)} 条自我信息（{titles}），细节需要时调用 search_self_information 查询。"
        )

    if not lines:
        return ""
    card = "\n".join(lines).strip()
    if max_chars and len(card) > max_chars:
        card = card[: max(1, max_chars - 1)].rstrip() + "…"
    return card


#: 状态常量（与 verify_self_claim 工具返回保持一致）
CLAIM_MATCH = "符合"
CLAIM_CONFLICT = "冲突"
CLAIM_NO_RECORD = "档案无记录"

#: 「我叫 X」这类**显式**自我命名（刻意不含「我是 X」：它会把「我是浅蓝长发的」
#: 误判成自称名字 ⇒ 假冲突，而假冲突比漏报更糟）
_NAME_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"我(?:的)?名字(?:是|叫)\s*(?P<name>[^\s，。,.！!？?、；;：:]{1,12})"),
    re.compile(r"我(?:叫|叫做|名为)\s*(?P<name>[^\s，。,.！!？?、；;：:]{1,12})"),
)

#: 明显不是名字的开头/结尾/包含物（防误判成冲突）
_NON_NAME_PREFIXES = ("一个", "一只", "一位", "一名", "个", "种", "谁", "不", "你", "他", "她", "它", "那种", "这样")
_NON_NAME_TAILS = ("的", "了", "吗", "呢", "吧", "啊", "哦", "嘛", "着", "过")
_NON_NAME_CONTAINS = ("的", "是", "很", "在", "有", "会", "能", "就", "都", "和", "与")


def _looks_like_name(value: str) -> bool:
    """名字形态校验：太短太长、带助词/连接词的都不算。"""
    text = str(value or "").strip()
    if not text or len(text) > 12:
        return False
    if any(text.startswith(prefix) for prefix in _NON_NAME_PREFIXES):
        return False
    if text.endswith(_NON_NAME_TAILS):
        return False
    return not any(token in text for token in _NON_NAME_CONTAINS)


@dataclass(frozen=True)
class ClaimVerdict:
    """一句话与档案的校验结果。"""

    state: str = CLAIM_NO_RECORD
    score: float = 0.0
    evidence: Tuple[Tuple[float, IdentityInfo], ...] = ()
    conflicts: Tuple[str, ...] = ()
    claim: str = ""

    @property
    def is_conflict(self) -> bool:
        return self.state == CLAIM_CONFLICT

    def summary(self) -> str:
        """给模型看的一句话结论 + 依据。"""
        lines = [f"判定：{self.state}"]
        if self.evidence:
            lines.append("依据条目：")
            for index, (_score, info) in enumerate(self.evidence, start=1):
                detail = info.full_information or info.title or "（无正文）"
                lines.append(f"  {index}. {info.title or '未命名'}：{detail}")
        if self.conflicts:
            lines.append("冲突点：")
            lines.extend(f"  - {item}" for item in self.conflicts)
        if self.state == CLAIM_NO_RECORD and not self.conflicts:
            lines.append("档案里没有相关内容；如需补充，请在 WebUI 的 infos / profile 里登记。")
        return "\n".join(lines)


def extract_claim_name(claim: str) -> str:
    """从声明里抽「我叫 X」的 X；抽不到或不像名字时返回空串。"""
    text = str(claim or "").strip()
    if not text:
        return ""
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        value = match.group("name").strip().strip("，。,.！!？?、；;：:")
        if _looks_like_name(value):
            return value
    return ""


def _content_phrases(text: str, limit: int = 20) -> List[str]:
    """把正文切成短语（声明校验用：正文里的一段出现在声明里即算命中）。"""
    parts = re.split(r"[，。,.、；;：:！!？?\s（）()\[\]【】]+", str(text or ""))
    return [part.strip().lower() for part in parts if len(part.strip()) >= 2][:limit]


def score_claim(info: IdentityInfo, claim: str, parts: List[str]) -> float:
    """声明校验用打分：以「声明里有没有档案内容」为主（OR 语义，不要求全命中）。"""
    claim_lower = _norm(claim)
    item_title = _norm(info.title)
    item_keywords = info.normalized_keywords()
    score = 0.0

    # ① 反向包含：档案的标题 / 关键词 / 正文短语出现在声明里
    if item_title and item_title in claim_lower:
        score += 90.0
    for keyword in item_keywords:
        if keyword in claim_lower:
            score += 70.0
    for phrase in _content_phrases(info.full_information):
        if phrase in claim_lower:
            score += 60.0

    # ② 正向模糊：声明拆出的子词与标题 / 关键词的相似度
    for part in parts:
        normalized = _norm(part)
        if not normalized:
            continue
        score += _title_score(item_title, normalized) * 0.5
        score += _keyword_score(item_keywords, normalized)
    return score


def verify_claim(
    infos: List[IdentityInfo],
    claim: str,
    *,
    profile: Any = None,
    threshold: float = 15.0,
    limit: int = 3,
) -> ClaimVerdict:
    """校验一句关于自己的声明：符合 / 冲突 / 档案无记录（防人设漂移）。

    冲突判定是**确定性规则**（不依赖模型）：
    1. 声明里出现档案登记的忌讳词；
    2. 声明自称的名字与 profile.name 不符（双向包含视为一致）。
    """
    raw_claim = str(claim or "").strip()
    if not raw_claim:
        return ClaimVerdict(state=CLAIM_NO_RECORD, claim="")

    safe_claim = neutralize(raw_claim)
    lowered = safe_claim.lower()
    conflicts: List[str] = []

    taboos = split_keywords(str(_pick(profile, "taboos") or "")) if profile is not None else []
    for taboo in taboos:
        if taboo.lower() in lowered:
            conflicts.append(f"这句话触及档案登记的忌讳「{taboo}」")

    configured_name = str(_pick(profile, "name") or "").strip() if profile is not None else ""
    claimed_name = extract_claim_name(safe_claim)
    if configured_name and claimed_name:
        left, right = configured_name.lower(), claimed_name.lower()
        if left not in right and right not in left:
            conflicts.append(f"档案里名字是「{configured_name}」，与这句话自称的「{claimed_name}」不符")

    parts = split_keywords(safe_claim)
    candidates: List[Tuple[float, IdentityInfo]] = []
    for info in infos:
        if info.is_blank:
            continue
        score = score_claim(info, safe_claim, parts)
        if score > 0.0:
            candidates.append((score, info))
    candidates.sort(key=lambda entry: entry[0], reverse=True)
    best_score = candidates[0][0] if candidates else 0.0
    strong = [entry for entry in candidates if entry[0] >= threshold][: max(1, limit)]

    if conflicts:
        return ClaimVerdict(
            state=CLAIM_CONFLICT,
            score=best_score,
            evidence=tuple(strong),
            conflicts=tuple(conflicts),
            claim=safe_claim,
        )
    if strong:
        return ClaimVerdict(
            state=CLAIM_MATCH,
            score=best_score,
            evidence=tuple(strong),
            claim=safe_claim,
        )
    return ClaimVerdict(state=CLAIM_NO_RECORD, score=best_score, claim=safe_claim)
