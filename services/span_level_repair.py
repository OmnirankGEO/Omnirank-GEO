"""[工单 span 级 AI 免费修复 2026-07-30] 四条底线的 **span 级** AI 免费修复引擎。

Owner:「用 AI 修不硬编码 · 免费修 · 尽量不重新生成 · 仅改那几个硬东西,不要把结构改了」。

本模块与既有 `services.writing_span_repair`(**整段**级)的关键差别 —— 也是本单存在的
理由 —— 在于**约束靠输入输出形状,不靠提示词**(工单 §1):

    旧:整段丢给 LLM → 让它"只改违规处"     → 它一定顺手改别的(LLM 天性)
    新:只给它违规**那一句** + 只读上下文    → 它物理上吐不出别的东西
       输出只有替换后的片段,系统按字节区间贴回原位

提示词一定会有失效的时候,所以贴回正文前有**不依赖模型自觉**的四项机械校验(§4):

    1. 长度带(见 _LENGTH_* 常量,附绝对增量上限)
    2. 结构指纹 diff 为空(标题/列表/表格/管道符/段落/引用/方括号记号数量全等)
    3. 除该 span 字节区间外,正文其余部分**逐字节相同**  ← 最硬的一条
    4. 模型返回 CANNOT_FIX_WITHOUT_FABRICATION → 零改动转人工(不算失败,是正确行为)

外加两条本项目特有的:
    5. 违规命中串必须消失(有来源的数字补出处那一路除外 —— 数字本身合法)
    6. 数字个数不许变多,且新出现的数字必须在证据包里找得到(§7 锁 8)

修完**重跑同一判定函数**才算放行(§5):不许用"模型说改好了"当依据。

🔴 医疗/法律/金融高风险类**不做 AI 修复**(§2.1):它缺的是**人工签发**,不是措辞。
让 AI 把结论性判断软化成"可能有帮助"会让判定转绿而风险一点没减,还留下
"系统认为它合规"的记录 —— 那是绕过审核,不是修复。
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Final, Iterable, Optional

from writing.geo_article_expert import is_high_risk_text

#: 模型的诚实出口。不给这个出口,它会为了完成任务补一个新编造 —— 比不修更糟。
CANNOT_FIX_MARKER: Final = "CANNOT_FIX_WITHOUT_FABRICATION"

# ── 四条底线 · 修法类别(不许一套提示词打天下,§2) ──────────────────────────
CATEGORY_FABRICATED_NUMBER: Final = "fabricated_number"
CATEGORY_SELF_INVENTED_SCORE: Final = "self_invented_score"
CATEGORY_FAKE_AUTHORITY: Final = "fake_authority"
#: [执行方订正 2026-07-30] 广告法绝对化用语。工单 §2 表只列了四类,但 legal_hard
#: (`article_review_gate.py:97-117`,本单锚定的那个按钮所在分支)**真正的 blocked
#: 成因**就是 absolute_first_claim / absolute_superlative_claim / 自创评分三者。
#: 不接绝对化 = 工单指名的那个出口按钮仍然走旧整段重写,§9 验收覆盖不到硬门本身。
#: 它也是最 span 形状的一类(改掉一个"最好")。理由见 EXIT「我改了工单哪几处」。
CATEGORY_ABSOLUTE_CLAIM: Final = "absolute_claim"

#: finding code → 修法类别。**只有这里列出的 code 走 span 级引擎**;
#: 其余 code(含 claim_missing_inline_evidence 这类定位锚是整块的)保持既有
#: 整段级路径不变 —— 存量零回归。
BOTTOMLINE_CATEGORIES: Final[dict[str, str]] = {
    "unsourced_outcome_number": CATEGORY_FABRICATED_NUMBER,
    "self_invented_scoring_system": CATEGORY_SELF_INVENTED_SCORE,
    "manufactured_score": CATEGORY_SELF_INVENTED_SCORE,
    "anonymous_authority": CATEGORY_FAKE_AUTHORITY,
    "absolute_first_claim": CATEGORY_ABSOLUTE_CLAIM,
    "absolute_superlative_claim": CATEGORY_ABSOLUTE_CLAIM,
}

# ── span 形状约束 ────────────────────────────────────────────────────────
#: 定位锚上限:超过这个长度的 matched_text 不是"一处表述"而是一整块,
#: 走 span 级会把整块交给模型 —— 与本单目的相反,直接诚实转人工。
MAX_ANCHOR_CHARS: Final = 120
#: span(= 命中串所在**那一句**)上限。整段/长表格行超限即转人工。
MAX_SPAN_CHARS: Final = 300
#: 只读上下文各取多少字(仅供模型理解语境,标注为只读)。
CONTEXT_CHARS: Final = 120

#: 句界。`\n` 是**硬边界** —— 让 span 永远不跨行,表格行/列表项/标题的
#: Markdown 骨架物理上不可能被跨行吃掉。
_SENTENCE_BOUNDARY: Final = frozenset("。！？!?；;\n")
#: 行首 Markdown 记号:span 起点跳过它,模型拿不到也就删不掉。
_LINE_MARKER_RE: Final = re.compile(r"^[ \t]*(?:[-*+][ \t]+|#{1,6}[ \t]+|\d+[.、)][ \t]+|>[ \t]*|\|[ \t]*)")

# ── 长度带([执行方订正 2026-07-30],原文 ±20%) ─────────────────────────
#: 🔴 工单 §4 写的 ±20% 与工单 §3.4 **自己的示例**互相矛盾:
#:   「综合评分 92 分，位列第一梯队。」→「在预算充足且需要深度定制的场景下更为匹配。」
#: 短 span 换成条件化表述必然超 20%;而删除型修复(§3.2 去掉数字)天然短得多。
#: 真正要防的是"把一句话扩写成一段",所以:比例带放宽 + **绝对增量硬上限**,
#: 并把"不许扩写"的主防线交给结构指纹 + 数字个数 + 重判(它们才拦得住实质变化)。
_LENGTH_RATIO_MIN: Final = 0.5
_LENGTH_RATIO_MAX: Final = 1.6
_LENGTH_ABS_GROWTH: Final = 40

_NUMBER_RE: Final = re.compile(r"\d+(?:[.,]\d+)*")
_BRACKET_RE: Final = re.compile(r"\[[^\]\n]{1,40}\]")


# ══════════════════════════════════════════════════════════════════════════
# span 定位
# ══════════════════════════════════════════════════════════════════════════
def locate_span(content: str, matched_text: str) -> Optional[dict[str, Any]]:
    """定位命中串所在的**那一句**,返回 span 字节区间与只读上下文。

    返回 {start, end, span, context_before, context_after} 或 None(定位不到)。
    命中串出现多次时取第一处(修完可再触发下一处,逐处修复语义清晰)。
    """
    text = str(content or "")
    needle = str(matched_text or "").strip()
    if not text or not needle:
        return None
    if len(needle) > MAX_ANCHOR_CHARS:
        return None
    hit = text.find(needle)
    if hit < 0:
        return None
    start = 0
    for i in range(hit - 1, -1, -1):
        if text[i] in _SENTENCE_BOUNDARY:
            start = i + 1
            break
    marker = _LINE_MARKER_RE.match(text[start:hit])
    if marker:
        start += marker.end()
    while start < hit and text[start] in " \t":
        start += 1
    end = len(text)
    for i in range(hit + len(needle), len(text)):
        if text[i] in _SENTENCE_BOUNDARY:
            # 句末标点含在 span 内(模型要还一句完整的话);换行永不含。
            end = i if text[i] == "\n" else i + 1
            break
    if end - start > MAX_SPAN_CHARS:
        return None
    return {
        "start": start,
        "end": end,
        "span": text[start:end],
        "context_before": _context_before(text, start),
        "context_after": _context_after(text, end),
    }


def _paragraph_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """span 所在段的边界。只读上下文**不许跨段** —— 把隔壁整段喂给模型,
    正是"顺手改别的"的邀请函(工单 §1 要求上下文只有 1-2 句)。"""
    left = text.rfind("\n\n", 0, start)
    left = 0 if left < 0 else left + 2
    right = text.find("\n\n", end)
    return left, (len(text) if right < 0 else right)


def _last_sentences(segment: str, count: int = 2) -> str:
    parts = re.findall(r"[^。！？!?；;\n]*[。！？!?；;\n]|[^。！？!?；;\n]+$", segment)
    parts = [part for part in parts if part.strip()]
    return "".join(parts[-count:]) if parts else segment


def _first_sentences(segment: str, count: int = 2) -> str:
    parts = re.findall(r"[^。！？!?；;\n]*[。！？!?；;\n]|[^。！？!?；;\n]+$", segment)
    parts = [part for part in parts if part.strip()]
    return "".join(parts[:count]) if parts else segment


def _context_before(text: str, start: int) -> str:
    left, _ = _paragraph_bounds(text, start, start)
    return _last_sentences(text[left:start])[-CONTEXT_CHARS:]


def _context_after(text: str, end: int) -> str:
    _, right = _paragraph_bounds(text, end, end)
    return _first_sentences(text[end:right])[:CONTEXT_CHARS]


def splice_span(content: str, start: int, end: int, new_span: str) -> str:
    """把 [start,end) 换成 new_span,其余逐字节不动。"""
    text = str(content or "")
    return text[:start] + str(new_span or "") + text[end:]


# ══════════════════════════════════════════════════════════════════════════
# 机械校验(§4)——这一层才是真防线
# ══════════════════════════════════════════════════════════════════════════
def structure_fingerprint(text: str) -> dict[str, int]:
    """整篇 Markdown 骨架指纹。修复前后必须完全一致(§4.2 / 锁 2)。"""
    body = str(text or "")
    lines = body.splitlines()
    return {
        "headings": sum(1 for line in lines if re.match(r"[ \t]*#{1,6}[ \t]", line)),
        "list_items": sum(1 for line in lines if re.match(r"[ \t]*(?:[-*+][ \t]|\d+[.、)][ \t])", line)),
        "table_rows": sum(1 for line in lines if line.strip().startswith("|")),
        "pipes": body.count("|"),
        "blockquotes": sum(1 for line in lines if line.strip().startswith(">")),
        "paragraphs": len([block for block in re.split(r"\n[ \t]*\n", body) if block.strip()]),
        "lines": len(lines),
        "bold": body.count("**"),
        # 方括号记号:Evidence ID / [1][2] 编号引用 / [CLIENT_IMAGE] 占位符
        # 一个都不许多、不许少(§3.3 禁编号引用 · 占位符不许被吃掉)。
        "brackets": len(_BRACKET_RE.findall(body)),
    }


def verify_span_isolation(
    original: str, repaired: str, start: int, end: int, new_span: str,
) -> bool:
    """除该 span 的字节区间外,正文其余部分**逐字节相同**(§4.3 / 锁 1)。

    🔴 这是最硬的一条:它让"AI 顺手改了别处"在物理上过不去。刻意用 utf-8
    字节比对(工单原文是"逐字节"),而不是信任 splice 的构造正确性 —— 校验对象
    是**将要落库的那个 candidate**,不是构造过程。
    """
    left_old = str(original or "")[:start].encode("utf-8")
    right_old = str(original or "")[end:].encode("utf-8")
    candidate = str(repaired or "").encode("utf-8")
    middle = str(new_span or "").encode("utf-8")
    if len(candidate) != len(left_old) + len(middle) + len(right_old):
        return False
    if candidate[:len(left_old)] != left_old:
        return False
    if candidate[len(left_old):len(left_old) + len(middle)] != middle:
        return False
    return candidate[len(left_old) + len(middle):] == right_old


def check_length(old_span: str, new_span: str) -> bool:
    """长度带(§4.1 / 锁 3,带绝对增量上限,见 _LENGTH_* 注释)。"""
    old_len = len(str(old_span or ""))
    new_len = len(str(new_span or ""))
    if old_len == 0 or new_len == 0:
        return False
    if new_len - old_len > _LENGTH_ABS_GROWTH:
        return False
    ratio = new_len / old_len
    return _LENGTH_RATIO_MIN <= ratio <= _LENGTH_RATIO_MAX


def pack_entity_whitelist(evidence_pack: Any,
                          extra_names: tuple[str, ...] = ()) -> tuple[str, ...]:
    """[R5.1/R5.2-1] 本文实体白名单 = **完整 roster**,不再只从 items 派生。

    🔴 R5.2-1:items[*].entity 会漏「零结果 lane」—— 某竞品逐家检索一条都
    没搜回来时,它压根不在 items 里,但主题素材照样可能点名它。改用:
      ① pack["queries"][*].entity(实际发出的逐家 query,含零结果 lane);
      ② pack["evidence_supply"]["entities_covered"](供给统计 roster);
      ③ extra_names(调用方现成的客户+竞品名单);
      ④ items[*].entity 只作补充。
    并做**确定性别名展开**:每个名字附 brand_core_term 简称(剥行政区划/
    组织后缀),全称/简称都进白名单 —— 不做开放式内容检测。
    """
    from writing.evidence_pack import raw_pack_items
    from writing.evidence_research import brand_core_term

    pack = evidence_pack if isinstance(evidence_pack, dict) else {}
    names: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        name = str(raw or "").strip()
        if not name:
            return
        for form in (name, brand_core_term(name)):
            form = str(form or "").strip()
            if form and form not in seen:
                seen.add(form)
                names.append(form)

    for q in pack.get("queries") or []:
        if isinstance(q, dict):
            _add(q.get("entity"))
    supply = pack.get("evidence_supply")
    if isinstance(supply, dict):
        for name in supply.get("entities_covered") or []:
            _add(name)
    for name in extra_names or ():
        _add(name)
    for item in raw_pack_items(pack):
        _add(item.get("entity"))
    return tuple(names)


def _same_subject(a: str, b: str) -> bool:
    """全称/简称同企对齐(确定性:核心词相等即同企)。"""
    from writing.evidence_research import brand_core_term

    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b:
        return False
    return a == b or brand_core_term(a) == brand_core_term(b)


def repair_subject_admits(item: dict[str, Any], *, target_entity: str,
                          entity_whitelist: tuple[str, ...]) -> bool:
    """[R5.1/R5.2-1 · 修复链主体核对] 修目标品牌 X 的 span 时条目是否可用。

    - 实体条目(已过统一门 = confirmed):只认 **confirmed 于 X**(全称/简称
      按核心词对齐,竞品条目 = 把 A 的事实写成 B 的事实,三硬禁之三);
    - 无实体条目:标题/claim **不含白名单里 X 以外主体的名字**(白名单已含
      全称+简称两种形态;X 自己的别名不算命中 —— 防过杀);
      单品牌 pack 的无主体素材照常可用 —— 零降级;
    - target_entity 为空(未接线的旧调用方)→ 维持现行为,不静默变语义。
    """
    target = str(target_entity or "").strip()
    if not target:
        return True
    entity = str((item or {}).get("entity") or "").strip()
    if entity:
        return _same_subject(entity, target)
    text = f"{(item or {}).get('title') or ''} {(item or {}).get('claim') or ''}"
    return not any(
        name and not _same_subject(name, target) and name in text
        for name in entity_whitelist
    )


def evidence_number_pool(evidence_pack: Any, *, target_entity: str = "") -> frozenset[str]:
    """证据包里出现过的数字集合(锁 8:新增数字必须在 evidence 里找得到)。

    🔴 [R4/R5.1] 数字保留资格同样是特权:错实体/缺 binding 条目里的数字
    不得进入"允许出现"池(否则修复链可用错实体的数字反写正文)——
    统一门(唯一消费 API)+ 主体核对(repair_subject_admits)双层。
    """
    from writing.evidence_pack import iter_entity_admissible_items

    _wl = pack_entity_whitelist(evidence_pack)
    text_parts: list[str] = []
    pack = evidence_pack if isinstance(evidence_pack, dict) else {}
    for item in iter_entity_admissible_items(pack):
        if not repair_subject_admits(item, target_entity=target_entity,
                                     entity_whitelist=_wl):
            continue
        for key in ("claim", "excerpt", "title", "published_at", "publisher", "quote"):
            value = item.get(key)
            if value:
                text_parts.append(str(value))
    return frozenset(_NUMBER_RE.findall(" ".join(text_parts)))


def check_numbers(
    old_span: str,
    new_span: str,
    evidence_numbers: Iterable[str],
    *,
    allow_more: bool = False,
) -> bool:
    """新出现的数字必须在证据包里找得到;数字个数默认不许变多(§7 锁 8)。

    `allow_more=True` 只给"有来源、补出处"那一路(§3.3):把发布时间/统计口径写进
    句子**必然**多出数字(如"2026 年报告"),那正是要求的修法。此时防编造靠的是
    "每个新数字都得在 evidence 里对得上"这一条,而不是计数 —— 计数在这一路会把
    正确修法一律判死(本条由锁 8 的两个方向共同钉住)。
    """
    old_numbers = _NUMBER_RE.findall(str(old_span or ""))
    new_numbers = _NUMBER_RE.findall(str(new_span or ""))
    if not allow_more and len(new_numbers) > len(old_numbers):
        return False
    allowed = set(old_numbers) | set(evidence_numbers or ())
    return all(number in allowed for number in new_numbers)


def verify_repair(
    *,
    original: str,
    repaired: str,
    start: int,
    end: int,
    old_span: str,
    new_span: str,
    category: str,
    matched_text: str,
    evidence_numbers: Iterable[str] = (),
    preserve_terms: Iterable[str] = (),
    allow_kept_anchor: bool = False,
) -> Optional[str]:
    """跑完全部机械校验。通过返回 None,不通过返回**机器可读的失败原因**。

    任一不过 → 调用方拒绝该次修复、回退原文、转人工(绝不"修了一半就落库")。
    """
    if "\n" in str(new_span or ""):
        # 换行 = 往一句里塞了新的行/段/列表项。结构指纹也会拦,这里提前给准原因。
        return "repair_span_multiline"
    if not check_length(old_span, new_span):
        return "repair_span_length_out_of_band"
    if structure_fingerprint(original) != structure_fingerprint(repaired):
        return "repair_structure_changed"
    if not verify_span_isolation(original, repaired, start, end, new_span):
        return "repair_touched_outside_span"
    if not check_numbers(old_span, new_span, evidence_numbers, allow_more=allow_kept_anchor):
        return "repair_introduced_number"
    for term in preserve_terms:
        term = str(term or "").strip()
        if term and term in str(old_span or "") and term not in str(new_span or ""):
            # 专有名词/品牌全称不许被顺手删掉(§3.1 硬性要求 3)。
            return "repair_dropped_protected_term"
    anchor = str(matched_text or "").strip()
    if anchor and anchor in str(new_span or "") and not allow_kept_anchor:
        # 违规命中串还在 = 没修。
        # 唯一例外:数字类**有可用来源**那一路(§3.3)——数字本身合法,
        # 修的是"没交代出处",数字必须留着。是否走这一路由调用方按
        # build_source_hint 的真实结果决定,不靠猜。
        return "repair_violation_text_remains"
    return None


# ══════════════════════════════════════════════════════════════════════════
# 高风险排除(§2.1 / 锁 6)
# ══════════════════════════════════════════════════════════════════════════
def article_ai_repair_blocked(industry: str = "", title: str = "") -> bool:
    """整篇级:医疗/法律/金融高风险文章不给 AI 修复出口(只人工签发 / 手动改)。"""
    return is_high_risk_text(industry, title)


def span_ai_repair_blocked(span_text: str) -> bool:
    """span 级:这一处本身是结论性诊疗/法律/收益判断 → 同样不给 AI 修。"""
    return is_high_risk_text(span_text)


def repair_route(
    code: str,
    *,
    span_text: str = "",
    industry: str = "",
    title: str = "",
) -> dict[str, Any]:
    """一处 finding 的修复路由(前端渲染与端点拒绝走**同一个**函数)。

    {category, span_level, ai_repairable, block_reason}
    - span_level=False → 不是底线类,走既有整段级路径(存量行为不变);
    - ai_repairable=False → 高风险,`ai_fix_this_span` 按钮不渲染、端点也拒。
    """
    category = BOTTOMLINE_CATEGORIES.get(str(code or "").strip())
    if article_ai_repair_blocked(industry, title):
        return {
            "category": category, "span_level": bool(category),
            "ai_repairable": False, "block_reason": "high_risk_article",
        }
    if span_text and span_ai_repair_blocked(span_text):
        return {
            "category": category, "span_level": bool(category),
            "ai_repairable": False, "block_reason": "high_risk_span",
        }
    return {
        "category": category, "span_level": bool(category),
        "ai_repairable": True, "block_reason": "",
    }


# ══════════════════════════════════════════════════════════════════════════
# 免费额度(§6 / 锁 7)
# ══════════════════════════════════════════════════════════════════════════
#: 每条 finding 的免费 span 级修复次数(首修 + 1 次重试)。
#: 🔴 额度按 **finding 条数** 给,不按点击次数 —— 没有 finding 就没有按钮,
#: 点不出来,所以"没事就点重新生成"钻不到空子(那条路本来就是收费的整篇重写)。
FREE_REPAIRS_PER_FINDING: Final = 2
_QUOTA_KEY: Final = "span_repair_quota"


def finding_fingerprint(code: str, matched_text: str) -> str:
    """同一条 finding 的稳定指纹(code + 命中串)。刻意不含字符偏移:
    偏移会随每次修复漂移,那样额度会被"改一处就重置"绕开。"""
    import hashlib

    anchor = hashlib.sha1(str(matched_text or "").encode("utf-8")).hexdigest()[:16]
    return f"{str(code or '').strip()}:{anchor}"


def free_repairs_used(quality_warning: Any, fingerprint: str) -> int:
    warning = quality_warning if isinstance(quality_warning, dict) else {}
    quota = warning.get(_QUOTA_KEY) if isinstance(warning.get(_QUOTA_KEY), dict) else {}
    entry = quota.get(fingerprint) if isinstance(quota.get(fingerprint), dict) else {}
    try:
        return max(0, int(entry.get("used") or 0))
    except (TypeError, ValueError):
        return 0


def free_repairs_left(quality_warning: Any, fingerprint: str) -> int:
    return max(0, FREE_REPAIRS_PER_FINDING - free_repairs_used(quality_warning, fingerprint))


def reserve_free_repair(
    quality_warning: Any, fingerprint: str, *, code: str = "", at: str = "",
) -> Optional[dict[str, Any]]:
    """占一次额度,返回**完整的** span_repair_quota 子对象(quality_warning 是
    浅合并,子对象必须整体写回);额度已用尽返回 None(调用方拒绝且**零计费**)。

    刻意在**调用模型之前**占位:一次尝试就是一次平台成本,失败也算用掉
    (§5"再修 1 次"就是这个语义),不给"失败不计次"的无限重试口子。
    """
    if free_repairs_left(quality_warning, fingerprint) <= 0:
        return None
    warning = quality_warning if isinstance(quality_warning, dict) else {}
    quota = dict(warning.get(_QUOTA_KEY) or {}) if isinstance(warning.get(_QUOTA_KEY), dict) else {}
    entry = dict(quota.get(fingerprint) or {}) if isinstance(quota.get(fingerprint), dict) else {}
    entry["code"] = str(code or entry.get("code") or "")
    entry["used"] = free_repairs_used(quality_warning, fingerprint) + 1
    entry["limit"] = FREE_REPAIRS_PER_FINDING
    if at:
        entry["last_at"] = at
    quota[fingerprint] = entry
    return quota


def release_free_repair(quality_warning: Any, fingerprint: str) -> Optional[dict[str, Any]]:
    """退还一次已占的额度;没有可退的返回 None。

    🔴 占位时机**不动**:仍然在调用模型之前占(见 `reserve_free_repair` 注释)——
    那是防"连点两下各读到 used=1"的串行闸。这里是事后**显式退还**,不是把占位
    挪到调用之后(挪了就重新打开连点绕过的洞)。

    只在两种情形退:平台自身故障(基础设施错误),或**根本没调过模型**
    (没产生平台成本就不该扣用户额度 —— `repair_bottomline_span` 的 `llm_invoked`
    早就是为这件事回的,只是此前没有任何调用方用它)。
    """
    used = free_repairs_used(quality_warning, fingerprint)
    if used <= 0:
        return None
    warning = quality_warning if isinstance(quality_warning, dict) else {}
    quota = dict(warning.get(_QUOTA_KEY) or {}) if isinstance(warning.get(_QUOTA_KEY), dict) else {}
    entry = dict(quota.get(fingerprint) or {}) if isinstance(quota.get(fingerprint), dict) else {}
    entry["used"] = used - 1
    entry["limit"] = FREE_REPAIRS_PER_FINDING
    quota[fingerprint] = entry
    return quota


# ══════════════════════════════════════════════════════════════════════════
# 修复出口三态(误报治理工单 2026-07-30 · T2)
# ══════════════════════════════════════════════════════════════════════════
# 旧代码把三种**性质完全不同**的结果都渲染成红色「失败」,信息失真最贵的一处是:
# `CANNOT_FIX_WITHOUT_FABRICATION` 明明是**按设计的正确行为**(模块 docstring
# 第 4 条自陈"不算失败"),却被显示成失败并配「重试这一处」—— 用户点一次必然
# 再失败,两次点完免费额度归零,那一处就彻底没路了。
OUTCOME_REPAIRED: Final = "repaired"
#: 需人工:AI 诚实拒绝编造 / 高风险类不做 AI 修复。**重试无意义 → 不给重试**。
OUTCOME_NEEDS_HUMAN: Final = "needs_human"
#: 真失败:模型改了但没改对(机械校验拦下)。重试可能有用 → 给重试,且**计次**
#: (给了重试就必须有界,否则就是无限免费重试)。
OUTCOME_FAILED: Final = "failed"
#: 我们的故障:超时 / 5xx / 连接失败。不是用户的问题 → 不计额度。
OUTCOME_SERVICE_UNAVAILABLE: Final = "service_unavailable"

#: 基础设施类失败(平台自身故障)。`repair_llm_failed` 正是 `llm_fn` 抛异常那一路。
INFRA_FAILURE_REASONS: Final[frozenset[str]] = frozenset({"repair_llm_failed"})
#: 转人工类(不给重试出口)。
NEEDS_HUMAN_REASONS: Final[frozenset[str]] = frozenset({
    "cannot_fix_without_fabrication", "high_risk_article", "high_risk_span",
})


def repair_outcome(reason: str) -> str:
    """把内部 reason 归成三态之一(成功另算)。"""
    key = str(reason or "").strip()
    if key == "repaired":
        return OUTCOME_REPAIRED
    if key in NEEDS_HUMAN_REASONS:
        return OUTCOME_NEEDS_HUMAN
    if key in INFRA_FAILURE_REASONS:
        return OUTCOME_SERVICE_UNAVAILABLE
    return OUTCOME_FAILED


def quota_should_refund(reason: str, llm_invoked: bool) -> bool:
    """这次失败该不该退还额度。

    退:①平台自身故障(基础设施错误) ②压根没调过模型(零平台成本)。
    不退:机械校验失败(给了重试就必须有界)、CANNOT_FIX(真花了一次模型调用,
         且它本来就不给重试,扣 1 次无害)。
    """
    if not llm_invoked:
        return True
    return str(reason or "").strip() in INFRA_FAILURE_REASONS


# ══════════════════════════════════════════════════════════════════════════
# 提示词(§3)
# ══════════════════════════════════════════════════════════════════════════
_SHARED_HEAD: Final = f"""你在做【单点文字替换】,不是改写文章。

你会收到:
  [只读上文]  —— 仅供理解语境,绝对不要输出它、不要修改它
  [待替换片段] —— 你只需要输出这一段的替换版本
  [只读下文]  —— 同上文
  [违规说明]  —— 这段为什么不能发

硬性要求:
1. 只输出替换后的片段本身。不要解释、不要加引号、不要加 Markdown 包装、
   不要输出上下文。
2. 🔴 保持原片段的【长度量级】、【句子数】、【语气】。不要顺手扩写、
   不要顺手润色其他部分。输出必须是**单行**,不许出现换行。
3. 🔴 不得改动片段内的:专有名词、品牌全称、地域、有出处的数字、
   引用来源、Markdown 记号(**、#、-、| 等)。
4. 替换后必须与上下文自然衔接,读者读不出这里被改过。
5. 🔴 如果你无法在不编造新信息的前提下修好它,
   输出恰好这一行:{CANNOT_FIX_MARKER}
   —— 这不是失败,是正确行为。宁可交回人工,也不许补一个新的编造。
"""

_TEMPLATE_NUMBER_NO_SOURCE: Final = """[违规说明] 这段里的数字/比例/频率在证据包与客户资料里都找不到支撑。

修法:把无出处的具体数值【去掉】,改写成不含具体数字的定性表述,
      保留原句想传达的方向与结论强度。
🔴 不许换成另一个数字、不许换成"约""大约""据估算"再带数字、
   不许改成"业内普遍认为"(那是假身份)。

示例(仅示意结构,不要照抄措辞):
  改前:2025年抽检显示,8.7%的品牌板材甲醛超标。
  改后:板材甲醛超标仍是行业内被反复提及的质量风险点。
"""

_TEMPLATE_NUMBER_WITH_SOURCE: Final = """[违规说明] 数字本身可能成立,但正文没有交代出处与口径。
[可用来源] {sources}

修法:把出处与口径【自然地写进句子】,用"据《XX》""XX 发布的数据显示"
      这类正常行文,并补上时间或统计口径。
🔴 不许写成编号引用([1][2])、不许出现 Evidence ID、
   不许把数字改成来源里没有的值。
"""

_TEMPLATE_SELF_INVENTED_SCORE: Final = """[违规说明] 这段出现了平台自造的评分/星级/等级(如综合分、百分制、S-A-B 级),
           没有可信来源,属不可发布内容。

修法:删掉评分/等级本身,改成【条件化表述】——说明在什么条件下更适合谁。
🔴 不许改成"约 90 分""接近满分""第一梯队"(换皮的自创评价)。

示例:
  改前:综合评分 92 分,位列第一梯队。
  改后:在预算充足且需要深度定制的场景下更为匹配。
"""

_TEMPLATE_FAKE_AUTHORITY: Final = """[违规说明] 这段引用了无法核实的主体(虚构分析师/机构/媒体/第三方核验)。
[可用来源] {sources}

修法:
  有可用来源 → 换成该真实主体,按其原本表述引用;
  无可用来源 → 【整个删掉这层背书】,把主张改成不依赖背书的表述,
              或降低断言强度到无需背书即可成立。
🔴 不许改成"业内人士""相关专家""有观点认为"——那仍是匿名权威。
"""

#: [执行方订正 2026-07-30] 第五类模板:广告法绝对化用语(legal_hard 真成因)。
_TEMPLATE_ABSOLUTE_CLAIM: Final = """[违规说明] 这段把品牌/产品写成第一、唯一、最好、国家级等绝对化用语
           (《广告法》第九条禁止清单),对外发布会被硬门拦下。

修法:把绝对化用语换成【披露依据的相对表述】,只保留能被证据支撑的强度。
🔴 不许换成"数一数二""领先品牌""公认第一"这类换皮绝对化;
   不许新增销量、排名、份额等没有出处的数字来支撑。

示例:
  改前:是行业第一的板材品牌。
  改后:在多家门店的可比报价中处于中上区间。
"""


def build_span_prompt(
    *,
    category: str,
    span: str,
    context_before: str,
    context_after: str,
    message: str = "",
    source_hint: str = "",
) -> str:
    """按 violation_code 的类别选模板(§3),拼出只含 span 的最小提示词。"""
    sources = source_hint.strip() or "无"
    body = {
        CATEGORY_SELF_INVENTED_SCORE: _TEMPLATE_SELF_INVENTED_SCORE,
        CATEGORY_FAKE_AUTHORITY: _TEMPLATE_FAKE_AUTHORITY.format(sources=sources),
        CATEGORY_ABSOLUTE_CLAIM: _TEMPLATE_ABSOLUTE_CLAIM,
    }.get(category)
    if body is None:
        body = (
            _TEMPLATE_NUMBER_WITH_SOURCE.format(sources=sources)
            if source_hint.strip() else _TEMPLATE_NUMBER_NO_SOURCE
        )
    extra = f"[判定原文] {message.strip()}\n" if message.strip() else ""
    return (
        f"{_SHARED_HEAD}\n{body}\n{extra}"
        f"[只读上文]\n{context_before}\n\n"
        f"[待替换片段]\n{span}\n\n"
        f"[只读下文]\n{context_after}\n\n"
        "现在只输出替换后的片段(单行):"
    )


def normalize_model_span(raw: str) -> str:
    """剥掉模型顺手加的包装(代码围栏 / 引号 / "改后:" 前缀),不做任何语义改写。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    text = re.sub(r"^(?:改后|修改后|替换后|输出)\s*[:：]\s*", "", text).strip()
    if len(text) >= 2 and text[0] in "「“\"'‘" and text[-1] in "」”\"'’":
        text = text[1:-1].strip()
    return text


def build_source_hint(evidence_pack: Any, span: str, category: str,
                      *, target_entity: str = "") -> str:
    """给模型的[可用来源]。**只在真的对得上时才给** —— 否则它会张冠李戴。

    数字类:证据条目里必须出现该 span 里的**同一个数字**才算可用来源
    (§3.3 与 §3.2 是两条完全不同的修法,选错比不修更糟);
    假背书类:任何已核验条目都可作为替换主体的候选。
    """
    pack = evidence_pack if isinstance(evidence_pack, dict) else {}
    from writing.evidence_pack import (
        VERIFIED_STATES,
        _verified_provenance_complete,
        iter_entity_admissible_items,
    )

    span_numbers = set(_NUMBER_RE.findall(str(span or "")))
    hints: list[str] = []
    _wl = pack_entity_whitelist(pack)
    # 🔴 [R4/R5.1 · 第 3 出口] 本函数产出直接进修复 LLM prompt,修复结果
    # UPDATE 回正文 —— 统一走唯一消费 API + 主体核对双层
    # (Codex 复现:核验态+溯源完整的错实体/缺 binding 条目曾漏进提示词;
    # 深档多品牌 pack 装着竞品素材,修 X 的 span 不得抓别家)。
    for item in iter_entity_admissible_items(pack):
        if not repair_subject_admits(item, target_entity=target_entity,
                                     entity_whitelist=_wl):
            continue
        if item.get("verification_status") not in VERIFIED_STATES:
            continue
        if not _verified_provenance_complete(item):
            continue
        blob = " ".join(
            str(item.get(key) or "")
            for key in ("claim", "excerpt", "title", "publisher", "published_at")
        )
        if category == CATEGORY_FABRICATED_NUMBER:
            if not span_numbers or not span_numbers & set(_NUMBER_RE.findall(blob)):
                continue
        publisher = str(item.get("publisher") or "").strip()
        title = str(item.get("title") or item.get("claim") or "").strip()[:60]
        when = str(item.get("published_at") or "").strip()[:10]
        hints.append(" / ".join(part for part in (title, publisher, when) if part))
        if len(hints) >= 3:
            break
    return "; ".join(hints)


# ══════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════
async def repair_bottomline_span(
    content: str,
    finding: dict[str, Any],
    llm_fn: Callable[[str], Awaitable[str]],
    *,
    industry: str = "",
    title: str = "",
    preserve_terms: Iterable[str] = (),
    target_entity: str = "",
) -> dict[str, Any]:
    """对一条底线类 finding 做 span 级修复。

    [R5.1] target_entity = 本文品牌 X:来源提示与数字保留资格按主体核对过滤
    (修 X 的 span 不得用竞品/别家条目;未接线的旧调用传空 = 现行为)。

    返回 {ok, content, reason, span_before, span_after, category, llm_invoked}。
    ok=False 时 content **恒为原文**(一字不动);llm_invoked 供调用方决定
    是否消耗该 finding 的免费额度(没真调模型就不该扣额度)。
    """
    original = str(content or "")
    code = str(finding.get("code") or "").strip()
    matched_text = str(finding.get("matched_text") or "")
    category = BOTTOMLINE_CATEGORIES.get(code)
    fail = {"ok": False, "content": original, "category": category, "llm_invoked": False}
    if not category:
        return {**fail, "reason": "not_bottomline_finding"}
    located = locate_span(original, matched_text)
    if not located:
        return {**fail, "reason": "span_not_located"}
    route = repair_route(code, span_text=located["span"], industry=industry, title=title)
    if not route["ai_repairable"]:
        # §2.1:高风险类缺的是人工签发,不是措辞 —— 这里**永不调模型**。
        return {**fail, "reason": route["block_reason"]}

    evidence_pack = finding.get("evidence_pack") if isinstance(finding.get("evidence_pack"), dict) else {}
    source_hint = build_source_hint(evidence_pack, located["span"], category,
                                    target_entity=target_entity)
    prompt = build_span_prompt(
        category=category,
        span=located["span"],
        context_before=located["context_before"],
        context_after=located["context_after"],
        message=str(finding.get("message") or ""),
        source_hint=source_hint,
    )
    try:
        raw = await llm_fn(prompt)
    except Exception:
        return {**fail, "reason": "repair_llm_failed", "llm_invoked": True}
    fail = {**fail, "llm_invoked": True}
    new_span = normalize_model_span(raw)
    if not new_span:
        return {**fail, "reason": "repair_empty"}
    if CANNOT_FIX_MARKER in new_span:
        # 🔴 诚实出口:零改动转人工,不算失败(§4.4 / 锁 5)。
        return {**fail, "reason": "cannot_fix_without_fabrication"}

    candidate = splice_span(original, located["start"], located["end"], new_span)
    problem = verify_repair(
        original=original,
        repaired=candidate,
        start=located["start"],
        end=located["end"],
        old_span=located["span"],
        new_span=new_span,
        category=category,
        matched_text=matched_text,
        evidence_numbers=evidence_number_pool(evidence_pack,
                                              target_entity=target_entity),
        preserve_terms=preserve_terms,
        # §3.3 有来源 → 数字留着补出处;§3.2 无来源 → 数字必须消失。
        allow_kept_anchor=bool(source_hint) and category == CATEGORY_FABRICATED_NUMBER,
    )
    if problem:
        return {**fail, "reason": problem}

    # §5 修完必须重跑**同一判定函数**才算放行(不许用"模型说改好了"当依据)。
    # 判定对象是修复后的**整段**:原 finding 就是在带 ±180 邻近半径的正文扫描里
    # 产出的,只判裸句会让"补出处"那一路必然假红。
    from services.writing_span_repair import locate_paragraph, still_violates

    paragraph = locate_paragraph(candidate, new_span)
    paragraph_text = paragraph["paragraph"] if paragraph else new_span
    if still_violates(
        paragraph_text, code,
        evidence_pack or None,
        finding.get("brand_fact_snapshot"),
    ):
        return {**fail, "reason": "still_violating"}
    return {
        "ok": True,
        "content": candidate,
        "reason": "repaired",
        "category": category,
        "llm_invoked": True,
        "span_before": located["span"],
        "span_after": new_span,
        # 兼容既有端点/前端字段名(整段级链的 paragraph_* 契约)。
        "paragraph_before": located["span"],
        "paragraph_after": new_span,
    }
