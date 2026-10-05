"""标题问句化策略（工单 `WORKORDER_TITLE_QUESTION_AND_LENGTH_2026-07-29` · T1）。

数据底座 = `CONTENT_MIX_SSOT_2026-07-29.md` §1 规则一（L2 · Review 独立正则复跑）：

====== ====== ====== ==========
榜单词 问句词 篇数   采纳率
====== ====== ====== ==========
无     无     6,741  11.24%
无     **有** 2,540  **18.90%**
有     无     3,466  12.03%
有     **有** 1,142  **16.99%**
====== ====== ====== ==========

两条结论落到本模块：

1. **榜单词本身几乎无效应**（11.24% → 12.03%，+0.8pt 噪音级），**问句式表达才是
   效应源**（+5~10pt，短/中/长三档全部成立）→ 因此问句化是 **全家族** 规则，
   榜单家族尤其要改，而不是"多做问答家族"；
2. 🔴 这是 **观察性相关不是因果**（域名/行业/渠道混杂，见 SSOT §4）。本模块的
   目标是"提高问句式标题占比"，**不是**把问句当成效果承诺。

## 为什么是 70% 而不是 100%

数据说"问句一律更好"，但全部问句会同质化、AI 味重（工单 §1.2）。70% 是
**业务判断，不是数据推导**，因此它必须是 **可配置默认值**：

优先级 ``显式入参 > 环境变量 > settings.json > 模块默认 70``。

## 用户 > 默认

工单 §1.2：用户/运营显式指定标题形态时，**用户 > 默认，不走比例抽签**。
``enforce_question_ratio(..., user_specified=True)`` 直接放行并留痕，
一个字都不改写。
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Final, Iterable, Sequence

from .title_formula_library import has_question_form, has_ranking_form


TITLE_QUESTION_POLICY_VERSION: Final = "geo-title-question-v1.0"

#: 工单 §1.2 默认值。**运营参数**，改后新批次生效、存量不追溯。
DEFAULT_QUESTION_RATIO_PERCENT: Final = 70
#: 判别锁容差（工单 §1.3 锁 1：10 个标题命中 6-8）。70% ±10pt。
QUESTION_RATIO_TOLERANCE_PERCENT: Final = 10
#: settings.json 字段名 / 环境变量名（两条都可覆盖默认值）。
QUESTION_RATIO_SETTING_FIELD: Final = "title_question_ratio_percent"
QUESTION_RATIO_ENV: Final = "WRITING_TITLE_QUESTION_RATIO_PERCENT"

#: 工单 §1.1：榜单家族尤其要改（纯榜单 12.03% → 榜单+问句 16.99%）。
#: 配额分配时它是**优先族**：全局配额不够分给所有家族时，先保证它拿到 ≥1。
PRIORITY_FAMILY_CODES: Final[tuple[str, ...]] = ("multi_brand_comparison",)

#: 用户显式指定标题形态的合法取值。``None`` / "auto" = 未指定 → 走比例。
TITLE_FORM_CHOICES: Final[frozenset[str]] = frozenset({"question", "open", "auto"})


def is_question_title(title: str | None) -> bool:
    """问句式判定 —— 直接复用标题公式库的唯一正则，不写第二份。"""
    return has_question_form(title)


def is_ranking_title(title: str | None) -> bool:
    return has_ranking_form(title)


# ---------------------------------------------------------------------------
# 比例读取（可配置默认值 · 工单 §1.2「不是硬编码」）
# ---------------------------------------------------------------------------
def _clamp_percent(value: Any) -> int | None:
    try:
        percent = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if percent < 0 or percent > 100:
        return None
    return percent


def _ratio_from_settings(industry: str | None) -> int | None:
    """settings.json（含行业 override）里的运营配置。取不到返回 None。"""
    try:
        from config.settings_manager import get_current_settings

        settings = get_current_settings()
    except Exception:
        return None
    overrides = getattr(settings, "industry_overrides", None) or {}
    key = str(industry or "").strip()
    if key and isinstance(overrides.get(key), dict):
        scoped = _clamp_percent(overrides[key].get(QUESTION_RATIO_SETTING_FIELD))
        if scoped is not None:
            return scoped
    return _clamp_percent(getattr(settings, QUESTION_RATIO_SETTING_FIELD, None))


def get_question_ratio_percent(
    industry: str | None = None,
    *,
    override: Any = None,
) -> int:
    """本批标题的问句式目标占比（0-100）。

    ``显式入参 > 环境变量 > settings.json > 模块默认``。任何一层给出越界值
    （非数字 / <0 / >100）都当作"没配"继续往下退，绝不让一个脏配置把整批
    标题的形态策略打翻。
    """
    explicit = _clamp_percent(override)
    if explicit is not None:
        return explicit
    env_value = _clamp_percent(os.environ.get(QUESTION_RATIO_ENV))
    if env_value is not None:
        return env_value
    configured = _ratio_from_settings(industry)
    if configured is not None:
        return configured
    return DEFAULT_QUESTION_RATIO_PERCENT


def question_quota_for_count(count: int, ratio_percent: int) -> int:
    """一批 ``count`` 条标题里应有多少条问句式。"""
    total = max(0, int(count or 0))
    percent = max(0, min(100, int(ratio_percent or 0)))
    if total <= 0 or percent <= 0:
        return 0
    return max(0, min(total, int(round(total * percent / 100.0))))


def ratio_within_tolerance(question_count: int, total: int, ratio_percent: int) -> bool:
    """判别锁 1 的口径：命中数是否落在 ``ratio ± 容差`` 内（按条数向外取整）。"""
    total = max(0, int(total or 0))
    if total <= 0:
        return True
    percent = max(0, min(100, int(ratio_percent or 0)))
    lo = math.floor(total * max(0, percent - QUESTION_RATIO_TOLERANCE_PERCENT) / 100.0)
    hi = math.ceil(total * min(100, percent + QUESTION_RATIO_TOLERANCE_PERCENT) / 100.0)
    return lo <= int(question_count or 0) <= hi


def allocate_question_quota(
    family_counts: dict[str, int],
    ratio_percent: int,
    *,
    priority_families: Sequence[str] = PRIORITY_FAMILY_CODES,
) -> dict[str, int]:
    """把全局配额按家族摊开，并保证 **每个在场家族至少 1 条问句**。

    工单 §1.3 锁 2 要的是"榜单家族问句命中率 > 0"。纯按比例摊分在小批次里会
    把某个家族摊成 0（例如 10 条里榜单只占 2 条，70% 配额可能全落到别的族），
    所以这里用**最大余额法分配 + 保底补 1**：

    * 先按各家族条数做最大余额分配，总数**恰好等于**全局配额（不多不少，
      所以锁 1 的 6-8 容差不会被保底逻辑撑破）；
    * 再把拿到 0 的家族补到 1，从当前分得最多（>1）的家族身上扣，总数不变；
    * 配额本身小于在场家族数时补不满，此时 ``priority_families`` 先拿。
    """
    counts = {
        str(code): max(0, int(value or 0))
        for code, value in (family_counts or {}).items()
        if int(value or 0) > 0
    }
    total = sum(counts.values())
    quota = question_quota_for_count(total, ratio_percent)
    if not counts or quota <= 0:
        return {code: 0 for code in counts}

    # 最大余额法：先取整数部分，余数按小数部分从大到小补。
    exact = {code: n * quota / total for code, n in counts.items()}
    allocation = {code: int(math.floor(value)) for code, value in exact.items()}
    # 单族不得超过自己的条数。
    for code in allocation:
        allocation[code] = min(allocation[code], counts[code])
    remainder = quota - sum(allocation.values())
    if remainder > 0:
        ranked = sorted(
            counts,
            key=lambda code: (-(exact[code] - math.floor(exact[code])), -counts[code], code),
        )
        for code in ranked:
            if remainder <= 0:
                break
            if allocation[code] < counts[code]:
                allocation[code] += 1
                remainder -= 1

    # 保底：在场却拿到 0 的家族补到 1，从分得最多的家族身上扣（总数不变）。
    priority = [code for code in priority_families if code in counts]
    zero_families = sorted(
        (code for code in counts if allocation[code] == 0),
        key=lambda code: (code not in priority, code),
    )
    for code in zero_families:
        donor = max(
            (c for c in counts if allocation[c] > 1),
            key=lambda c: (allocation[c], c),
            default=None,
        )
        if donor is None:
            break
        allocation[donor] -= 1
        allocation[code] = 1
    return allocation


# ---------------------------------------------------------------------------
# 形态改写（确定性 · 不调 LLM）
# ---------------------------------------------------------------------------
#: 榜单/营销前缀词。改写成问句时把它们从**标题开头**摘掉，挪进副标题。
_LEADING_MARKETING_RE: Final = re.compile(
    r"^\s*(?:20\d{2}\s*年?\s*)?(?:最新\s*)?"
    r"(?:TOP\s*\d+|十大|[一二三四五六七八九十\d]+\s*大)\s*"
)
#: 标题里的年份（工单外的既有口径：年份不作开头）。
_YEAR_PREFIX_RE: Final = re.compile(r"^\s*20\d{2}\s*年?\s*")
#: 副标题分隔符——改写时在这里切开主副标题。
_SUBTITLE_SPLIT_RE: Final = re.compile(r"[｜|：:—－\-]{1,2}")
#: 问句化后缀（按家族给不同问法，避免整批同一句式）。
_FAMILY_QUESTION_SUFFIX: Final[dict[str, tuple[str, ...]]] = {
    "multi_brand_comparison": ("哪家靠谱？", "怎么选？", "哪家好？"),
    "evidence_qa": ("怎么判断？", "该怎么看？", "有什么区别？"),
    "implementation_guide": ("怎么做？", "分几步？", "怎么落地？"),
    "trend_policy_risk": ("有哪些变化？", "风险在哪？", "该怎么应对？"),
    "case_data_roi": ("怎么测算？", "值得吗？", "投入产出怎么算？"),
    "company_facts": ("适合哪些场景？", "怎么核验？", "能做什么？"),
}
_DEFAULT_QUESTION_SUFFIX: Final[tuple[str, ...]] = ("怎么选？", "哪家靠谱？", "怎么看？")
#: 陈述化时用来接住原问句语义的尾巴（保持信息量，不做无脑截断）。
_STATEMENT_TAIL: Final = "对比与选择建议"


def _split_title(title: str) -> tuple[str, str]:
    """切出主标题与副标题（没有副标题时副标题为空串）。"""
    text = str(title or "").strip()
    match = _SUBTITLE_SPLIT_RE.search(text)
    if not match or match.start() == 0:
        return text, ""
    return text[: match.start()].strip(), text[match.end():].strip()


def _question_suffix(family_code: str | None, seed: int) -> str:
    pool = _FAMILY_QUESTION_SUFFIX.get(str(family_code or ""), _DEFAULT_QUESTION_SUFFIX)
    return pool[max(0, int(seed or 0)) % len(pool)]


def _anchor_of(keyword: str | None, fallback: str) -> str:
    """标题必须保留的业务锚点。复用既有的关键词锚点 SSOT，取不到就用回退值。"""
    text = str(keyword or "").strip()
    if not text:
        return fallback
    try:
        from writing.title_keyword_alignment import title_anchor_from_purchased_keyword

        anchor = str(title_anchor_from_purchased_keyword(text) or "").strip()
    except Exception:
        anchor = ""
    return anchor or text


def to_question_title(
    title: str,
    *,
    keyword: str | None = None,
    family_code: str | None = None,
    seed: int = 0,
) -> str:
    """把非问句标题确定性地改写成问句式。改不动就返回原标题（fail-open）。

    工单 §1.1 的样例形态：
    ``2026年十大GEO服务商`` → ``GEO服务商哪家靠谱？10家横向评测与场景推荐``
    —— 榜单**信息**（"10 家横向评测"）不丢，只是不再由榜单词占标题开头。
    """
    original = str(title or "").strip()
    if not original:
        return original
    if is_question_title(original):
        return original

    head, tail = _split_title(original)
    ranking_prefix = _LEADING_MARKETING_RE.match(head)
    ranking_hint = ""
    if ranking_prefix:
        token = ranking_prefix.group(0).strip()
        digits = re.search(r"\d+", token)
        if "十大" in token:
            ranking_hint = "10 家横向评测与场景推荐"
        elif digits:
            ranking_hint = f"{digits.group(0)} 家横向评测与场景推荐"
        else:
            ranking_hint = "多家横向评测与场景推荐"
        head = head[ranking_prefix.end():].strip()
    head = _YEAR_PREFIX_RE.sub("", head).strip()

    anchor = _anchor_of(keyword, head)
    if not head:
        head = anchor
    # 锚点必须在场：改写不能把客户买的那个词改没了。
    if anchor and anchor not in head:
        head = f"{head}{anchor}" if head else anchor
    if not head:
        return original

    question = f"{head}{_question_suffix(family_code, seed)}"
    subtitle = tail or ranking_hint
    rewritten = f"{question}{subtitle}" if subtitle else question
    # 锚点丢失 = 改写失败，宁可保留原标题也不交一个跑题标题。
    if anchor and anchor not in rewritten:
        return original
    return rewritten


#: 疑问标记。陈述化时从**第一个**疑问标记处把主标题切开：标记左边是主体，
#: 标记及其右边整段是"提问"的那部分。
#:
#: 用通用标记而不是枚举问法：枚举永远追不上模型的说法（2026-07-29 真跑实测，
#: 枚举版对"全屋定制**有哪些**常见陷阱""全屋定制验收**查什么**"两条都认不出来，
#: 3 次陈述化只成功 1 次，整批停在 9/10 问句、超出 6-8 容差）。
_QUESTION_MARKER_RE: Final = re.compile(
    r"(?:哪家|哪个|哪些|哪里|哪|怎么|怎样|如何|什么|多少|几步|几家|几种|"
    r"是否|要不要|值得|靠谱吗|好不好|有没有)"
)
#: 疑问标记左边常常粘着一个动词（"验收**查**什么" / "全屋定制**有**哪些"），
#: 切完要一并摘掉，否则会留下"全屋定制验收查"这种半截主体。
#: ⚠️ 多字动词必须排在单字之前：正则交替是最左匹配，把 `查` 放在 `检查` 前面会
#: 只吃掉 `查`、留下 `全屋定制验收要检`（2026-07-29 真跑实测到的残句）。
_TRAILING_VERB_RE: Final = re.compile(
    r"(?:检查|查看|查验|核对|核验|测算|判断|应对|注意|选购|考察|考虑|了解|知道|关注|"
    r"包含|包括|可以|需要|该|要|应|能|看|选|挑|做|算|测|查|检|问|有|是|用|买|找)+\s*$"
)
#: 主体切完至少要留这么多字，否则视为切过头（宁可不改）。
_MIN_STATEMENT_STEM_CHARS: Final = 2


def to_open_title(title: str, *, keyword: str | None = None) -> str:
    """把问句标题改回非问句（陈述）形态，用于压住"整批全问句"的同质化。

    做法是**在问号处切开**，而不是在整串里做正则替换：

    * 问号**前**是提问句 —— 摘掉句尾的问法词，剩下的就是主体（"全屋定制验收"）；
    * 问号**后**本来就是陈述式副标题 —— 直接拿来当正文标题的后半段。

    ``全屋定制验收该看哪些地方？分区域验收清单与常见问题``
      → ``全屋定制验收：分区域验收清单与常见问题``

    没有问号、或切完拿不到干净主体/副标题时，**返回原标题**。比例是运营偏好，
    不值得为了凑它交一个残句 —— 少一条陈述式只是略偏离 70%，容差吃得下；
    交一个"…对比对比与选择建议"是实打实的质量事故。
    """
    original = str(title or "").strip()
    if not original or not is_question_title(original):
        return original

    parts = re.split(r"[？?]", original, maxsplit=1)
    if len(parts) != 2:
        return original
    stem_raw, subtitle_raw = parts[0].strip(), parts[1].strip()
    marker = _QUESTION_MARKER_RE.search(stem_raw)
    if not marker:
        return original
    stem = _TRAILING_VERB_RE.sub("", stem_raw[: marker.start()]).strip(" 、,，:：|｜-—")
    subtitle = subtitle_raw.strip(" 、,，:：|｜-—")
    if len(stem) < _MIN_STATEMENT_STEM_CHARS or not subtitle:
        return original

    rewritten = f"{stem}：{subtitle}"
    anchor = _anchor_of(keyword, "")
    if anchor and anchor not in rewritten:
        return original
    if is_question_title(rewritten):
        return original
    return rewritten


# ---------------------------------------------------------------------------
# 批级执行（topics 落库前的唯一入口）
# ---------------------------------------------------------------------------
def _family_of(topic: dict, family_key: str | None) -> str:
    """从 topic 上解析六族 family code（兼容 family code / 中文文体名 / style code）。"""
    from writing.article_style_contract import STYLE_FAMILIES, family_for_style

    candidates: list[str] = []
    if family_key:
        candidates.append(str(topic.get(family_key) or ""))
    candidates.extend(
        str(topic.get(key) or "")
        for key in ("user_choice", "family_code", "style_family", "article_style", "style_code", "style", "type")
    )
    name_to_code = {family.name: code for code, family in STYLE_FAMILIES.items()}
    for raw in candidates:
        value = raw.strip()
        if not value:
            continue
        if value in STYLE_FAMILIES:
            return value
        if value in name_to_code:
            return name_to_code[value]
        resolved = family_for_style(value)
        if resolved:
            return resolved
    return "evidence_qa"


def enforce_question_ratio(
    topics: list[dict],
    *,
    title_key: str = "optimized_title",
    family_key: str | None = None,
    keyword_key: str = "original_keyword",
    industry: str | None = None,
    ratio_percent: Any = None,
    user_specified: bool = False,
) -> dict[str, Any]:
    """把一批 topic 的标题形态收敛到目标比例。**原地改写 topics**，返回执行报告。

    工单 §1.2「用户 > 默认」：``user_specified=True`` 时**不做任何改写**，
    只记录一份 ``skipped_user_specified`` 的报告 —— 比例抽签不介入。
    """
    items = [t for t in (topics or []) if isinstance(t, dict)]
    percent = get_question_ratio_percent(industry, override=ratio_percent)
    report: dict[str, Any] = {
        "version": TITLE_QUESTION_POLICY_VERSION,
        "ratio_percent": percent,
        "total": len(items),
        "user_specified": bool(user_specified),
        "applied": False,
        "question_before": sum(1 for t in items if is_question_title(t.get(title_key))),
        "question_after": 0,
        "promoted": 0,
        "demoted": 0,
        "unchanged_failed_rewrite": 0,
        "by_family": {},
    }
    if not items:
        report["question_after"] = 0
        return report
    if user_specified:
        report["question_after"] = report["question_before"]
        report["skipped_reason"] = "user_specified_title_form"
        return report

    groups: dict[str, list[int]] = {}
    for index, topic in enumerate(items):
        groups.setdefault(_family_of(topic, family_key), []).append(index)
    allocation = allocate_question_quota(
        {code: len(idx) for code, idx in groups.items()}, percent,
    )

    for family_code, indexes in groups.items():
        want = int(allocation.get(family_code, 0))
        # 已经是问句的优先占用配额（不改一个好标题），不足再改写补齐。
        already = [i for i in indexes if is_question_title(items[i].get(title_key))]
        others = [i for i in indexes if i not in set(already)]
        keep_question = already[:want]
        promote = others[: max(0, want - len(keep_question))]
        demote = already[want:]

        for seed, index in enumerate(promote):
            topic = items[index]
            before = str(topic.get(title_key) or "")
            after = to_question_title(
                before,
                keyword=topic.get(keyword_key),
                family_code=family_code,
                seed=seed,
            )
            if after != before and is_question_title(after):
                topic[title_key] = after
                topic["title_form"] = "question"
                topic["title_form_source"] = "ratio_policy_promoted"
                report["promoted"] += 1
            else:
                report["unchanged_failed_rewrite"] += 1

        for index in demote:
            topic = items[index]
            before = str(topic.get(title_key) or "")
            after = to_open_title(before, keyword=topic.get(keyword_key))
            if after != before and not is_question_title(after):
                topic[title_key] = after
                topic["title_form"] = "open"
                topic["title_form_source"] = "ratio_policy_demoted"
                report["demoted"] += 1
            else:
                report["unchanged_failed_rewrite"] += 1

        for index in indexes:
            topic = items[index].setdefault("title_form", None) or None
            if items[index].get("title_form") is None:
                items[index]["title_form"] = (
                    "question" if is_question_title(items[index].get(title_key)) else "open"
                )
                items[index]["title_form_source"] = "as_generated"

        report["by_family"][family_code] = {
            "total": len(indexes),
            "quota": want,
            "question": sum(1 for i in indexes if is_question_title(items[i].get(title_key))),
        }

    report["applied"] = True
    report["question_after"] = sum(1 for t in items if is_question_title(t.get(title_key)))
    report["within_tolerance"] = ratio_within_tolerance(
        report["question_after"], report["total"], percent,
    )
    for topic in items:
        topic["title_question_policy_version"] = TITLE_QUESTION_POLICY_VERSION
    return report


def build_question_ratio_prompt(ratio_percent: int, *, families: Iterable[str] = ()) -> str:
    """把比例要求渲染成提示词块（生成端与执行端同源，不写第二份口径）。"""
    percent = max(0, min(100, int(ratio_percent or 0)))
    lines = [
        "【标题形态配比（数据驱动 · 全家族适用）】",
        f"- 本批标题里 **约 {percent}% 必须是问句式**"
        "（“怎么选/哪家靠谱/有什么区别/值得吗/怎么样”或带问号），"
        f"其余 {100 - percent}% 保留陈述等其他形态，避免整批同质化；",
        "- 🔴 **榜单/推荐/对比类标题同样要问句化**：带对照组的样本里，纯榜单标题"
        "（12.03%）与不带任何形态词的标题（11.24%）没有实质差别 —— **榜单词买不来采纳**；"
        "把榜单信息放进副标题即可，例："
        "`2026年十大GEO服务商` → `GEO服务商哪家靠谱？10 家横向评测与场景推荐`；",
        "- ❌ 禁止把“十大 / TOP10 / 年度榜单”式纯营销标题当作整批唯一形态；",
        "- ⚠️ 上述数字是**观察性相关不是因果**（域名/行业/渠道混杂），"
        "它决定形态偏好，不构成任何采纳率承诺。",
    ]
    wanted = {str(f).strip() for f in families if str(f or "").strip()}
    if wanted:
        lines.append(f"- 本批涉及家族：{'、'.join(sorted(wanted))} —— 每个家族都要有问句式标题。")
    return "\n".join(lines)


def validate_question_policy() -> list[str]:
    """自检（release gate 可直接调）。返回错误码列表，空 = 通过。"""
    errors: list[str] = []
    if not (0 <= DEFAULT_QUESTION_RATIO_PERCENT <= 100):
        errors.append("default_ratio_out_of_range")
    if QUESTION_RATIO_TOLERANCE_PERCENT <= 0:
        errors.append("tolerance_must_be_positive")
    # 配额分配的总数必须恰好等于全局配额（保底补 1 不得撑破锁 1 的容差）。
    for counts in ({"a": 4, "b": 3, "c": 3}, {"a": 8, "b": 1, "c": 1}, {"a": 10}):
        allocated = allocate_question_quota(counts, DEFAULT_QUESTION_RATIO_PERCENT)
        expected = question_quota_for_count(sum(counts.values()), DEFAULT_QUESTION_RATIO_PERCENT)
        if sum(allocated.values()) != expected:
            errors.append(f"quota_allocation_drift:{counts}:{allocated}")
        for code, value in allocated.items():
            if value > counts[code]:
                errors.append(f"quota_exceeds_family_size:{code}")
    return errors
