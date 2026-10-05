"""标题数量承诺 → 正文兑现闸。

## 为什么需要它(W1 返工 ④ · 2026-08-08)

实测:文章 1515 **标题承诺「排名前十」,正文只写了 1 家**。

成因是链路上两个正确决定撞在一起:
* 选题阶段按关键词生成标题,那时还不知道最终能核验出几家;
* 生成阶段走 `evidence_only`,候选不足时按规矩**不虚构候选** ——
  规格卡明确让它「改写成分场景推荐榜」(见 `article_type_spec_cards._ranking_entity_line`)。

正文那一侧做对了,**标题没跟着改**。于是我们自己产出了一个当场自证不实的承诺:
读者(和 AI 引擎)看到「前十」,点进去只有 1 家。这比少写几家严重得多 ——
它是**广告法意义上的不实表示**,不是文风问题。

## 闸的边界(刻意开得很窄)

* **只在我们确知已核验家数时才判**(`verified_entity_count is None` → 一个字不改)。
  错改标题比漏改更坏,而这不是安全闸,是兑现闸;
* **只在兑现不了时才改**:承诺 N、已核验 >= N 就不动 —— 写实了就不该被罚;
* **改法是确定性的**:删掉数量承诺、必要时补一个选择型任务词。
  **不调 LLM** —— 保存链上多一次模型调用就是多一个失败点和一笔钱。

留痕进 `quality_warning.title_promise`,不做静默改写。
"""
from __future__ import annotations

import re
from typing import Final

#: 中文数字 → 阿拉伯(只收标题里真会出现的量级)。
_CN_NUM: Final[dict[str, int]] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十五": 15, "二十": 20, "三十": 30,
}
_CN_ALT: Final = "|".join(sorted(_CN_NUM, key=len, reverse=True))

#: 「候选实体」类名词。数字后面跟着它,才算是在承诺**我们会列出几家候选**。
_ENTITY_NOUN: Final = "(?:品牌|厂家|公司|企业|机构|服务商|供应商)"

#: 句读。🔴 **用码位写,不写字面量** —— 第一版把全角逗号/分号直接敲进字符类,
#: 编辑链路上全角字符没活下来,类里只剩半角,于是最常见的「名单前十，其中」
#: 一条都不命中(半角 `,` 命中、全角 `，` 不命中)。全角是中文正文的常态。
_SENTENCE_END: Final = (
    "\n\u3002\uff0c\u3001\uff1b\uff1a\uff01\uff1f"   # 。，、；：！?
    ",;:!?"                                           # 半角同款
)
#: 数字后面可能先跟一个收尾括号再断句(「(前十)」)。
_CLOSERS: Final = "\\)\uff09\\]\u3011\u300d\u300f"

#: 标题里的数量承诺形态。每条都必须能取出一个数字。
_PROMISE_PATTERNS: Final[tuple[re.Pattern, ...]] = (
    re.compile(r"(?i)top\s*(?P<n>\d+)"),
    re.compile(rf"排[行名]\s*前\s*(?P<n>\d+|{_CN_ALT})"),
    re.compile(rf"前\s*(?P<n>\d+|{_CN_ALT})\s*(?:名|强|家)"),
    re.compile(rf"(?P<n>\d+|{_CN_ALT})\s*大\s*{_ENTITY_NOUN}"),
    # 「7家在营企业」这种中间夹修饰语的也算承诺;但只放行 0-4 个汉字,
    # 免得把「3家分店」这类**品牌自身事实**误当成候选家数承诺。
    re.compile(rf"(?P<n>\d+|{_CN_ALT})\s*家\s*[一-龥]{{0,4}}?{_ENTITY_NOUN}"),
    # 🔴 [W1 返工 ③ 2026-08-09] 兜底形态「前N」。上一版是**裸**的
    # `前\s*(N)(?![0-9])` —— 标题里够用,一进正文就开始误伤:
    # 「前十分钟」→「多家分钟」、「前三季度」→「多家季度」、「前五年」→「多家年」。
    # 收窄成必须有下列上下文之一才算数量承诺:
    #   · 紧跟(可带「的」)候选实体名词 ——「前十的服务商」;
    #   · 落在句末/行末/标点前 ——「郑州装修公司前十」这类真标题靠它兜住。
    # 「前十分钟」的下一个字是「分」,两条都不满足 → 不再命中。
    re.compile(
        rf"前\s*(?P<n>\d+|{_CN_ALT})(?![0-9])"
        rf"(?=\s*(?:的?\s*{_ENTITY_NOUN}|[{_CLOSERS}]?\s*(?:$|[{_SENTENCE_END}])))"
    ),
)

#: 标题里表明这是「怎么选」类任务的词。删完承诺后若一个都不剩,补一个。
_TASK_CUES: Final[tuple[str, ...]] = (
    "怎么选", "如何选", "怎么挑", "如何", "对比", "比较", "核验",
    "指南", "清单", "避坑", "怎么办", "价格", "费用", "推荐",
)
_FALLBACK_CUE: Final = "怎么选"

#: 删完之后可能留下的孤立标点/空白。
_LEFTOVER = re.compile(r"[ 　]{2,}")
#: 删完承诺后可能留在**开头**的孤立助词/标点(「排行前三的服务商」→ 别留下「的服务商」)。
_DANGLING_HEAD = re.compile(r"^[\s,，、::·\-—|的和与及]+")
_DANGLING_TAIL = re.compile(r"[\s,，、::·\-—|]+$")


def _to_int(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)


def extract_quantity_promise(title: str) -> tuple[int, str] | None:
    """标题里承诺了几家。返回 ``(数量, 命中的原文片段)``,没承诺则 None。"""
    text = str(title or "")
    best: tuple[int, str] | None = None
    for pattern in _PROMISE_PATTERNS:
        for m in pattern.finditer(text):
            n = _to_int(m.group("n"))
            if n is None:
                continue
            # 多个承诺时取**最大**的那个 —— 兑现闸要按最强的那句承诺判。
            if best is None or n > best[0]:
                best = (n, m.group(0))
    return best


#: 删掉承诺后,标点/连接词后面留下的孤立助词(「怎么选:7家在营企业的核验」→「怎么选:核验」)。
_ORPHAN_PARTICLE = re.compile(r"([::,，、])\s*[的和与及]+")


def strip_quantity_promise(title: str, *, add_task_cue: bool = True) -> str:
    """确定性地删掉数量承诺,必要时补一个选择型任务词。**不调 LLM。**

    没有数量承诺时**原样返回** —— 这个函数不负责给别的标题加词。

    Args:
        add_task_cue: 删完只剩光秃秃的名词短语时,补一个「怎么选」。
            这条是给**文章标题**设计的;正文里的**小标题**不适用 ——
            「## 排名前十的入选标准」删完补上就成了「## 入选标准怎么选」。
            所以正文那条路径显式传 False。(写 W1 返工 ② 的夹具时撞出来的)
    """
    text = str(title or "")
    if extract_quantity_promise(text) is None:
        return text
    for pattern in _PROMISE_PATTERNS:
        text = pattern.sub("", text)
    text = _LEFTOVER.sub(" ", text)
    text = _ORPHAN_PARTICLE.sub(r"\1", text)
    text = _DANGLING_TAIL.sub("", _DANGLING_HEAD.sub("", text)).strip()
    if not text:
        return str(title or "").strip()
    if add_task_cue and not any(cue in text for cue in _TASK_CUES):
        text = f"{text}{_FALLBACK_CUE}"
    return text


# ---------------------------------------------------------------------------
# [小单 A 2026-08-09] 正文里的**回声**。
#
# 上一版只改了标题,正文里那句「本文按排名前十的口径比较」和小标题
# 「## …排名前十」原样留着 —— 标题不承诺了,正文还在承诺,等于没修。
#
# 两条处置,分开是因为两种位置的安全边界不同:
#   · **标题行**(`^#{1,6} `):短名词短语,直接套标题那套确定性删除,删完读得通;
#   · **正文散句**:直接删会把句子删破(「本文按的口径比较」),所以换成一个
#     **不带数量的量词**。这不是"猜内容",是把我们自己写下的那句承诺降级成不承诺。
# 两种都逐条留痕,不静默改写。
# ---------------------------------------------------------------------------
#: 散句里的替换词。刻意只有一个 —— 它替换的是**我们自己的承诺措辞**,
#: 不是在判断"什么内容更好"(那条红线禁的是后者)。
_COUNT_FREE_QUANTIFIER: Final = "多家"
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s")


def strip_body_promise_echo(content: str) -> tuple[str, list[dict]]:
    """把正文里残留的数量承诺降级掉。返回 ``(正文, 逐行留痕)``。

    没有可改的行时返回**原对象**,便于调用方判断有没有真的动过。
    """
    text = str(content or "")
    if not text or extract_quantity_promise(text) is None:
        return content, []
    lines = text.splitlines()
    out = list(lines)
    notes: list[dict] = []
    for i, line in enumerate(lines):
        if extract_quantity_promise(line) is None:
            continue
        if _HEADING_LINE.match(line):
            prefix = line[: len(line) - len(line.lstrip())] + \
                re.match(r"\s*(#{1,6}\s*)", line).group(1)
            body = line[len(prefix):]
            new_line = prefix + strip_quantity_promise(body, add_task_cue=False)
        else:
            new_line = line
            for pattern in _PROMISE_PATTERNS:
                new_line = pattern.sub(_COUNT_FREE_QUANTIFIER, new_line)
        if new_line != line:
            out[i] = new_line
            notes.append({"line": i, "before": line.strip()[:120],
                          "after": new_line.strip()[:120]})
    if not notes:
        return content, []
    joined = "\n".join(out)
    if text.endswith("\n"):
        joined += "\n"
    return joined, notes


def enforce_title_promise(
    title: str, *, verified_entity_count: int | None,
) -> tuple[str, dict | None]:
    """标题承诺兑现不了就改标题。

    Returns:
        ``(标题, 留痕 or None)``。没改动时返回**原标题对象**,留痕为 None。
    """
    original = str(title or "")
    if verified_entity_count is None:
        return title, None          # 不知道兑现了几家 → 一个字不改
    promise = extract_quantity_promise(original)
    if promise is None:
        return title, None          # 没承诺数量 → 没什么可兑现
    promised, matched = promise
    delivered = max(0, int(verified_entity_count))
    if delivered >= promised:
        return title, None          # 写实了,不罚
    rewritten = strip_quantity_promise(original)
    if rewritten == original:
        return title, None
    return rewritten, {
        "promised": promised,
        "delivered": delivered,
        "matched": matched,
        "original_title": original,
        "rewritten_title": rewritten,
    }
