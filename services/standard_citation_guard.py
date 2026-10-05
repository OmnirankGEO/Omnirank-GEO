"""标题级标准证据的"内容性引用"判别锁。

工单 ``WORKORDER_STANDARD_EVIDENCE_LANE_2026-07-29`` §4.3 —— 与标准类证据 lane
**同批交付,不拆**(Owner §5-5 已定)。

为什么必须同批:标题级标准名进了证据池之后,写作模型会凭预训练知识补出
「该标准规定甲醛释放量≤0.03mg/m³」这类**看似极可信、实则纯编造**的句子,
读者与客户几乎不会去核。没有这把锁的 lane 等于给编造开一条权威通道 ——
**那种状态比不做这条 lane 更糟。**

判别条件(三条设计理由,实现时不许简化):

1. **不能用"文章含数字就红"** —— 文章本来就该有数据,会大面积误伤。
   要红的是「把数字挂在标题级标准名下」。
2. **不能限定"同一句"** —— 编造通常跨句:
   > 依据 GB 50210-2018《…验收标准》。**该标准要求**甲醛释放量不超过 0.03mg/m³。
3. **回指词是关键** —— 把"标准名出现后、后文恰好有别的无关数据"这种误伤排掉。
"""
from __future__ import annotations

import json
import re
from typing import Any, Final

from writing.evidence_pack import STANDARD_CITATION_TIER


#: 标准名出现后的取窗规则:**同一段落内 或 3 句以内**(§4.3,两者取并集)。
#: 🔴 这个 3 不许缩回"同句" —— 编造的典型形态就是跨句(设计理由 2)。
SENTENCE_WINDOW: Final = 3

#: 句子切分。段落先按换行切,段内再按中英文句末标点切。
_SENTENCE_SPLIT: Final = re.compile(r"(?<=[。！？；!?;])")

#: 回指词 —— 该句主语指向那份标准(设计理由 3)。
_ANAPHORS: Final[tuple[str, ...]] = (
    "该标准", "本标准", "此标准", "上述标准", "该项标准", "该技术标准",
    "该规范", "本规范", "此规范", "上述规范", "该规程", "本规程",
    "该文件", "该办法", "该通知",
    "其中规定", "其中要求", "按此标准", "按该标准", "依据该标准", "根据该标准",
    "标准要求", "标准规定", "标准明确", "规范要求", "规范规定", "规范明确",
)

#: 数值 + 单位。单位表刻意收窄到"条款里会出现的量",避免把普通行文里的数字全吃进来。
#: 🔴 不含"年/日":`该标准于 2022 年发布` 是**存在性元信息**(检索结果自带),
#:    不是条款内容,拿它转红属误伤。时长类的"5 年质保"由下方 1-3 位数那条单独收。
_UNITS: Final = (
    r"mg/m³|mg/m3|μg/m³|ug/m3|mg/kg|kg/m³|kg/m3|"
    r"m³|m3|m²|m2|km|cm|mm|kg|g|MPa|kPa|Pa|dB|kWh|W/\(m·K\)|"
    r"℃|°C|小时|分钟|天|次|倍|级|项|类|条|款|分项|个百分点"
)
_NUMERIC_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # 数值 + 单位:0.03mg/m³ / 50 mm / 3 级
    re.compile(rf"\d+(?:\.\d+)?\s*(?:{_UNITS})"),
    # 时长"N 年":限 1-3 位,避免把 2022 这种**年份**吃成数量(标准编号里全是 4 位年)。
    re.compile(r"(?<!\d)\d{1,3}\s*年(?!份|代)"),
    # 百分比 / 千分比
    re.compile(r"\d+(?:\.\d+)?\s*[%‰]"),
    # 比较式阈值:≤0.03 / 应不小于 5 / 不得超过 30
    re.compile(r"[≤≥<>＜＞]\s*\d+(?:\.\d+)?"),
    re.compile(r"(?:不得超过|不得低于|不得少于|不得大于|不得小于|不应超过|不应低于|"
               r"应不小于|应不大于|应不低于|应不超过|应大于|应小于|应达到|"
               r"最高不超过|最低不低于)\s*[≤≥<>＜＞]?\s*\d"),
    # 「分为 N 个 / N 类 / N 级 / N 个分项」
    re.compile(r"(?:分为|划分为|共分|包含|包括)\s*[0-9一二三四五六七八九十两]+\s*"
               r"(?:个|类|级|项|条|款|部分|分项|等级)"),
)

#: 从条目标题里抠"可被正文复述的标准名"。
_STD_CODE_IN_TITLE: Final = re.compile(
    r"(?:GB/T|GBT|GB|JGJ/T|JGJ|CJJ|DB[0-9]{2}(?:/T)?|T/[A-Z]{2,6})\s*[-—]?\s*[0-9]{4,5}"
    r"(?:\s*[-—]\s*[0-9]{4})?"
)
#: 书名号包起来的标准名。
_BOOK_TITLE: Final = re.compile(r"《([^《》]{4,60})》")
#: 名字太短会在正文里到处误命中(比如"标准""规范"两个字)。
_MIN_NAME_LEN: Final = 4

#: 「这条证据带正文」的门槛。与 ``writing/evidence_research.py`` 的 verified 门槛
#: (``len(body) >= 300``)**同一口径** —— 🔴 那边改了这里必须同步改,别各造一个数。
BODY_EVIDENCE_MIN_CHARS: Final = 300

#: 「只有标题」的那一个状态。其余状态(``body_retrieved_claim_unverified`` /
#: ``claim_span_verified`` / ``official_record`` / ``human_verified``)都意味着
#: 这条证据背后有正文或更强的来源 —— 与本文件下方逐条目判定用的是**同一个口径**。
#:
#: 🔴 为什么不直接量 ``len(item["body"])``(工单原话是"body/content 长度 >= 300"):
#:    **pack 条目里根本没有 body 字段** —— evidence_research.py:602-624 构造的 item
#:    只落 ``excerpt``,body 留在 ``verification_candidates`` 里;而且
#:    ``normalize_evidence_pack`` 是**字段白名单**,body/content 活不过 normalize。
#:    照字面量 body 长度会做出一条**在任何真实 pack 上永不触发的豁免**,
#:    正是本单 §3.3 警告的「看着在跑、实际空转」。
#:    ``verification_status`` 才是 evidence_research.py:588 那道 ``len(body) >= 300``
#:    门槛的可读产物 —— 用它就是在复用工单要求对齐的那个口径,不是另造数字。
#:    下面仍保留显式 body/content 长度判定,是给将来自带正文的来源(C 臂豆包)
#:    留的前向兼容,两者取并集。
_TITLE_ONLY_STATUS: Final = "search_result_only"


def _standard_key(text: str) -> str:
    """标准标识的比对键:编号去掉 ``-YYYY`` 后缀 + 去空白 + 大写;非编号则原样去空白。

    这样 ``GB 50210`` / ``GB 50210-2018`` / ``GB50210`` 会归到同一个键 ——
    否则换个写法豁免就对不上,等于豁免没做。
    """
    value = str(text or "").strip()
    if _STD_CODE_IN_TITLE.fullmatch(value):
        value = re.sub(r"\s*[-—]\s*[0-9]{4}$", "", value)
        return re.sub(r"\s+", "", value).upper()
    return re.sub(r"\s+", "", value)


def _item_has_body(item: dict[str, Any]) -> bool:
    """这条证据是不是「有正文」(≥ BODY_EVIDENCE_MIN_CHARS)?"""
    status = str(item.get("verification_status") or "")
    if status and status != _TITLE_ONLY_STATUS:
        return True
    for field in ("body", "content", "full_text"):
        if len(str(item.get(field) or "")) >= BODY_EVIDENCE_MIN_CHARS:
            return True
    return False


def _standards_covered_by_a_bodied_source(items: Any) -> set[str]:
    """pack 里**另有带正文来源**的标准 → 这些标准不受本锁约束。

    为什么必须有这条豁免:本锁拦的是「只拿到标题就往下写条款数值」。一旦同一份标准
    另有带正文的来源(§6 已把豆包定位成唯一能支撑**内容性引用**的源),那些条款就有
    来源可依,再拦就是**精确拦掉这条 lane 被设计来允许的那个用例**,而且
    ``overridable=False`` 没有逃生口 —— 现象会是"明明有正文来源却被拦、还签不了字"。
    """
    covered: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict) or not _item_has_body(item):
            continue
        # 只扫 title + 正文派生文本。excerpt 在有正文时就是正文截断(evidence_research:601),
        # 无正文时才退化成 snippet —— 而无正文的条目上面已经被 _item_has_body 挡掉了,
        # 所以这里不会出现"拿搜索摘要换豁免"。
        haystack = "\n".join(str(item.get(f) or "") for f in ("title", "excerpt", "body", "content", "full_text"))
        for match in _STD_CODE_IN_TITLE.finditer(haystack):
            covered.add(_standard_key(match.group(0)))
        for name in _BOOK_TITLE.findall(haystack):
            if len(name.strip()) >= _MIN_NAME_LEN:
                covered.add(_standard_key(name))
    return covered


def collect_title_only_standard_names(evidence_pack: Any) -> list[str]:
    """从 pack 里读出**标题级**标准条目的可复述名字。

    🔴 这是 §5.6 的接线点:`source_tier == 'standard_citation'` 这个标记必须能在
    发布门这一侧被真读到。写入侧在 `writing/evidence_research.py` 的 document lane,
    活过 `normalize_evidence_pack`,落 `articles.evidence_pack` (JSONB),
    再由本函数读回 —— 不接线的话锁拿不到字段,会永远不触发而全绿。
    """
    pack = evidence_pack
    if isinstance(pack, (str, bytes)):
        # JSONB 一般由驱动解成 dict,但历史行/别的读法可能给回原始串。
        try:
            pack = json.loads(pack)
        except (TypeError, ValueError):
            return []
    names: list[str] = []
    seen: set[str] = set()
    from writing.evidence_pack import raw_pack_items

    # [R5] 覆盖性扫描 = 判别(要看全量含候选)→ 结构访问器。
    items = raw_pack_items(pack) if isinstance(pack, dict) else None
    # 先扫一遍全 pack:同一标准若另有带正文来源,本锁对它整体不触发。
    covered = _standards_covered_by_a_bodied_source(items)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_tier") or "") != STANDARD_CITATION_TIER:
            continue
        # 只有"没有正文"的条目才受这条锁约束 —— 将来同一 tier 若拿到正文核验,
        # 它的条款就有来源可依,不该再被拦。
        if str(item.get("verification_status") or "") != "search_result_only":
            continue
        title = str(item.get("title") or "").strip()
        candidates: list[str] = list(_BOOK_TITLE.findall(title))
        for match in _STD_CODE_IN_TITLE.finditer(title):
            full = match.group(0).strip()
            candidates.append(full)
            # 正文可能只写 `GB 50210` 而标题是 `GB 50210-2018` —— 两种写法都要能对上,
            # 否则改个写法就绕过整把锁。
            candidates.append(re.sub(r"\s*[-—]\s*[0-9]{4}$", "", full).strip())
        for candidate in candidates:
            name = str(candidate).strip()
            if _standard_key(name) in covered:
                continue          # 该标准另有带正文来源 → 不进 title_only 集合
            if len(name) >= _MIN_NAME_LEN and name not in seen:
                seen.add(name)
                names.append(name)
        # 标题本身没有编号也没有书名号时(实测大量政府站条目就是这样),用整条标题。
        if not _STD_CODE_IN_TITLE.search(title) and not _BOOK_TITLE.search(title):
            if _standard_key(title) in covered:
                continue
            if len(title) >= _MIN_NAME_LEN and title not in seen:
                seen.add(title)
                names.append(title)
    return names


def _split_sentences(content: str) -> list[tuple[int, str]]:
    """→ ``[(段落序号, 句子), ...]``,顺序即正文顺序。"""
    out: list[tuple[int, str]] = []
    for para_index, paragraph in enumerate(str(content or "").split("\n")):
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            if sentence.strip():
                out.append((para_index, sentence))
    return out


def _window_indices(sentences: list[tuple[int, str]], start: int) -> list[int]:
    """§4.3 取窗:标准名出现后的【同一段落内 **或** 3 句以内】。

    🔴 两条是并集不是交集,且不许缩回"同句"(设计理由 2:编造通常跨句)。
    """
    para = sentences[start][0]
    return [
        index for index in range(start, len(sentences))
        if sentences[index][0] == para or (index - start) <= SENTENCE_WINDOW
    ]


def _has_numeric_claim(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _NUMERIC_PATTERNS)


def _points_at_standard(sentence: str, name: str) -> bool:
    """该句主语是否指向那份标准:回指词,或该句自己又把标准名念了一遍。

    后者对应铁律里 ❌「GB 50210-2018 规定甲醛释放量≤0.03mg/m³」那种同句直挂。
    """
    return any(word in sentence for word in _ANAPHORS) or name in sentence


def scan_standard_citation_misuse(content: str, evidence_pack: Any) -> list[dict[str, Any]]:
    """标题级标准名被做成"内容性引用" → 返回命中列表(非空即应拦)。"""
    names = collect_title_only_standard_names(evidence_pack)
    if not names:
        return []
    sentences = _split_sentences(content)
    if not sentences:
        return []
    findings: list[dict[str, Any]] = []
    reported: set[tuple[str, int]] = set()
    for name in names:
        for start, (_para, sentence) in enumerate(sentences):
            if name not in sentence:
                continue
            for index in _window_indices(sentences, start):
                candidate = sentences[index][1]
                if not _has_numeric_claim(candidate):
                    continue
                if not _points_at_standard(candidate, name):
                    continue
                key = (name, index)
                if key in reported:
                    continue
                reported.add(key)
                findings.append({
                    "standard_name": name,
                    "sentence": candidate.strip()[:200],
                    "sentence_index": index,
                    "distance": index - start,
                })
    return findings


def evaluate_standard_citation_discipline(
    content: str, evidence_pack: Any
) -> dict[str, Any] | None:
    """给发布门用的成品判定:通过返回 ``None``,不通过返回可直接并进 gate payload 的 dict。"""
    findings = scan_standard_citation_misuse(content, evidence_pack)
    if not findings:
        return None
    sample = findings[0]
    return {
        "eligible": False,
        "reason": "standard_citation_content_claim",
        "reason_class": "evidence_fabrication_hard",
        "overridable": False,
        "message": (
            f"正文把只拿到标题的标准《{sample['standard_name']}》当成有正文的来源引用了具体条款/数值："
            f"「{sample['sentence']}」。"
            "标题级证据只能做存在性引用（“依据 GB xxx《…》”），"
            "具体条款、数值、比例必须另有正文来源才能写 —— 否则就是编造。"
        ),
        "repair_hint": "删掉挂在该标准名下的具体数值/条款表述，或补一条带正文的来源后重新审核。",
        "standard_citation_findings": findings[:10],
        "actions": [
            {"id": "ai_fix_this_span", "label": "AI 修复该处表述", "type": "retry"},
            {"id": "view_article", "label": "查看文章", "type": "nav"},
        ],
    }


__all__ = [
    "SENTENCE_WINDOW",
    "BODY_EVIDENCE_MIN_CHARS",
    "collect_title_only_standard_names",
    "scan_standard_citation_misuse",
    "evaluate_standard_citation_discipline",
]
