# -*- coding: utf-8 -*-
"""法律禁止目录的**语境判定层**(中立模块)。

[V6 单点化 · Codex 定稿 2026-08-10]

## 为什么要这个模块

改动前仓里有**两份**广告法目录,而且**词项内容本身就不同**:

| | 词项 | 匹配方式 | 后果 |
|---|---|---|---|
| `config/legal_prohibited_pack.json`(Owner 签发) | 含**裸「第一」** | `guards._find_words` 纯子串 | 「第一步」「第一季度」**必然误伤** |
| `writing/evidence_first_policy.py` | 只有复合式(第一名/排名第一/行业第一) | 带语境判定 | 不误伤,但**是第二份未签发目录** |

两个毛病要一起治,且不能互相拆台:
- 只把词项统一到 JSON、匹配还用子串 → 误伤面反而扩大(裸「第一」进来了);
- 只删第二份、把语境算法一起删掉 → 「第一步」开始被拦。

所以本模块:**词项与版本单点来自 Owner 签发的 JSON;匹配一律经语境判定**。
`guards.py` 与 `writing/evidence_first_policy.py` 共同调用本模块,谁都不再自己维护词表。

## 为什么放在 `services/marketing/` 而不是 `writing/`

`guards.py` 不应反向依赖整个 `writing` 层(会形成循环依赖)。本模块只依赖标准库
与同目录的包加载器,两侧都能安全引用。

## 零阻断

本模块**只做判定,不做拦截**。包缺失/损坏时按 `guards` 既有口径 fail-open
(未经签发的内嵌集合无权硬拦)。判定结果如何使用由调用方决定。
"""
from __future__ import annotations

import re
from typing import Final, Iterable, NamedTuple

__all__ = [
    "LegalHit",
    "absolute_terms",
    "catalog_version",
    "find_absolute_violations",
    "is_safe_context",
]


class LegalHit(NamedTuple):
    """一次命中。`term` 是签发目录里的词,`start`/`end` 是正文位置。"""

    term: str
    start: int
    end: int
    excerpt: str


# ---------------------------------------------------------------------------
# 词项来源:唯一 SSOT = Owner 签发的 JSON 包
# ---------------------------------------------------------------------------
def absolute_terms() -> tuple[str, ...]:
    """《广告法》第九条绝对化用语词项。**唯一来源是签发包,本文件不硬编码词表。**"""
    from services.marketing.guards import legal_pack

    return tuple(str(w).strip() for w in (legal_pack().get("ad_law") or []) if str(w).strip())


def catalog_version() -> str:
    """目录版本。透出用,与签发包同源(旧的 `ad-law-art9-absolute-v9` 常量已废)。"""
    from services.marketing.guards import legal_pack_version

    return legal_pack_version()


# ---------------------------------------------------------------------------
# 语境判定(自 evidence_first_policy 抽出,行为保持)
# ---------------------------------------------------------------------------
# 🔴 [返工 R2 2026-08-11] 原「品牌宣称语境」兜底常量已删 —— 它支撑的
# "不在品牌语境就放行"分支超出封闭放行清单授权(签发词裸文本必须硬拦)。

#: 🔴 裸「第一」的安全后缀 —— 序数/次序用法,**不是**绝对化宣称。
#: 这是 JSON 包里裸「第一」与子串匹配相乘产生的误伤面,必须在这里挡住。
_ORDINAL_AFTER: Final = re.compile(
    r"^(?:步|次|季度|阶段|轮|批|章|节|条|款|项|部分|时间|现场|作者|人称|时|天|周|月|年|"
    r"位面试者|个月)"
)

#: 技术参数语境:「最大功率」「最大扭矩」—— 参数名,不是宣称。
_TECH_PARAM_AFTER: Final = re.compile(
    r"^(?:功率|扭矩|载重|承重|转速|流量|压力|电流|电压|容量|带宽|射程|量程|"
    r"行程|直径|长度|宽度|高度|深度|温度|湿度|速度|风量|扬程|噪音|误差)"
)

#: 「最佳实践」「最佳实例」—— 行业术语,不是对品牌的宣称。
_BEST_PRACTICE_AFTER: Final = re.compile(r"^(?:实践|实例|案例|做法|路径|时机|时间点)")

#: 否定语境 —— 「不得使用最佳」「禁止宣称第一」是元语言禁令,不是宣称。
_NEGATION_BEFORE: Final = re.compile(
    r"(?:不是|并非|不能说|不敢说|禁止|不得|严禁|避免|杜绝|不要|别|拒绝|不存在|没有)"
    r"[^。！？!?\n]{0,8}$"
)

# 🔴 [返工 R1 2026-08-11 · 广告法红线] 旧 `_QUOTED_BEFORE = [“"『「【]\s*$` 把
# **任何孤立引号**当引用语境整体豁免 —— 而 `claim_evidence_qa` 扫的是
# `json.dumps(payload)`,词项紧跟 JSON 结构引号 `"` → 任何位于 JSON 字符串值
# 开头的广告法词被静默放行(复审双向实证:裸文本命中,JSON 包裹后零命中)。
# 新口径:引用豁免只认**真引用** —— 三个条件缺一不可:
#   ⓐ 命中词落在**成对闭合**的引号内(同句内有开有闭);
#   ⓑ 开引号之前(≤12 字)有**言说/引用标记**(据/称/说/报道/写道/引用/宣称/
#      所谓/原话/指出/表示/回应);
#   ⓒ 引号只保护引用内容 —— 引用之外的自我断言照拦
#      (「"最佳选择"就是本公司」:引号成对但无言说标记 → 命中)。
# JSON 的 `":` 前没有言说标记 → 恒不豁免,红线闭合。
_CITE_MARKER_BEFORE_QUOTE: Final = re.compile(
    r"(?:据|称|说|报道|写道|引用|宣称|所谓|原话|指出|表示|回应|发文)"
    r"[^。！？!?\n]{0,6}$"
)
_QUOTE_PAIRS: Final[dict[str, str]] = {"“": "”", "「": "」", "『": "』", '"': '"'}
_CLOSERS: Final = ("”", "」", "』")
_SENT_BREAK: Final = "。！？!?\n"


def _in_cited_quote(text: str, start: int, end: int) -> bool:
    """命中词是否在**带言说标记的成对引号**内(= 真引用,可豁免)。"""
    opener_pos = -1
    opener_ch = ""
    i = start - 1
    while i >= 0 and text[i] not in _SENT_BREAK:
        ch = text[i]
        if ch in _CLOSERS:
            return False        # 先遇到闭引号 → 命中词不在引号里
        if ch in _QUOTE_PAIRS:
            opener_pos, opener_ch = i, ch
            break
        i -= 1
    if opener_pos < 0:
        return False
    closer = _QUOTE_PAIRS[opener_ch]
    j = end
    while j < len(text) and text[j] not in _SENT_BREAK:
        if text[j] == closer:
            # ⓑ 开引号前必须有言说/引用标记
            return bool(_CITE_MARKER_BEFORE_QUOTE.search(
                text[max(0, opener_pos - 12):opener_pos]
            ))
        j += 1
    return False                # 同句内没闭合 → 不是成对引用


#: 疑问语境(封闭清单成员):**读者问句**豁免 ——「哪家是最好的选择?」是提问
#: 不是宣称。🔴 同句含我方自指(我们/本公司/我司…)不豁免:
#: 「我们是第一品牌吗?」是修辞式自我宣传,照拦(宁严勿宽)。
_SELF_REFERENCE: Final = re.compile(r"我们|本公司|我司|本店|本品牌|旗下")
_INTERROGATIVE: Final = re.compile(r"哪家|哪个|哪种|谁|什么|如何|怎么|怎样|吗|呢")


def _in_reader_question(text: str, start: int, end: int) -> bool:
    lo = start
    while lo > 0 and text[lo - 1] not in _SENT_BREAK:
        lo -= 1
    hi = end
    while hi < len(text) and text[hi] not in _SENT_BREAK:
        hi += 1
    if hi >= len(text) or text[hi] not in "？?":
        return False
    sentence = text[lo:hi]
    return bool(_INTERROGATIVE.search(sentence)) and not _SELF_REFERENCE.search(sentence)


_WINDOW_BEFORE: Final = 24
_WINDOW_AFTER: Final = 12


def is_safe_context(text: str, term: str, start: int, end: int) -> bool:
    """这次命中是否落在**安全语境**(= 不构成绝对化宣称)。

    🔴 [返工 R2 2026-08-11] 放行清单是**封闭的**(工单 §7 + Review 裁定):
      ① 序数/次序:第一步、第一季度、第一次……(仅裸「第一」)
      ② 技术参数:最大功率、最大载重……
      ③ 行业术语:最佳实践、最佳实例/案例/做法……
      ④ 否定语境:「不得使用最佳」……
      ⑤ 真引用:成对引号 + 言说标记(R1 修复后的口径,JSON 结构引号不豁免)
      ⑥ 读者问句:「哪家是最好的选择?」(自指问句不豁免)
    **不匹配任何一条的签发词裸文本 = 硬拦**。旧版"不在品牌宣称语境就放行"
    的兜底分支已删除 —— 它超出授权(Owner 热改包加词的预期是加了就拦),
    且把「最好先核验资质」类副词用法一并放掉。副词用法如需豁免,
    列词+理由报 Owner 裁定,默认不放宽(已列入交付单)。
    """
    before = text[max(0, start - _WINDOW_BEFORE):start]
    after = text[end:end + _WINDOW_AFTER]

    # ④ 否定(元语言禁令)
    if _NEGATION_BEFORE.search(before):
        return True
    # ⑤ 真引用(R1:成对引号 + 言说标记;孤立引号/JSON 结构引号不算)
    if _in_cited_quote(text, start, end):
        return True
    # ⑥ 读者问句
    if _in_reader_question(text, start, end):
        return True
    # ① 裸「第一」的序数用法
    if term == "第一" and _ORDINAL_AFTER.match(after):
        return True
    # ② 技术参数
    if _TECH_PARAM_AFTER.match(after):
        return True
    # ③ 最佳实践类
    if term in ("最佳", "最好") and _BEST_PRACTICE_AFTER.match(after):
        return True

    return False


def find_absolute_violations(
    text: str, *, terms: Iterable[str] | None = None,
) -> list[LegalHit]:
    """扫出**真正构成绝对化宣称**的命中。安全语境不计入。

    🔴 与被替代的 `guards._find_words` 的差别:那个是纯子串 `if w in text`,
    「第一步」「第一季度」「最大功率」「最佳实践」全部误伤。本函数逐命中判语境。
    """
    body = str(text or "")
    if not body:
        return []
    pool = tuple(terms) if terms is not None else absolute_terms()
    hits: list[LegalHit] = []
    seen: set[tuple[int, int]] = set()
    # 长词优先,避免「第一品牌」被裸「第一」抢先并被序数规则误放行
    for term in sorted({t for t in pool if t}, key=len, reverse=True):
        for match in re.finditer(re.escape(term), body):
            span = (match.start(), match.end())
            if any(s <= span[0] and span[1] <= e for s, e in seen):
                continue        # 已被更长的词覆盖
            if is_safe_context(body, term, span[0], span[1]):
                continue
            seen.add(span)
            lo = max(0, span[0] - 12)
            hits.append(LegalHit(term, span[0], span[1], body[lo:span[1] + 12]))
    return sorted(hits, key=lambda h: h.start)
