"""标题拼接卫生 · 共用件(WO-ACCEPTANCE-3FIX-2026-08-05 项2)。

## 为什么是共用件而不是"再补一个 if"

「已含地名不再叠加」这条规则在本仓被**独立实现过至少四次**,每次只覆盖一支:

| # | 实现处 | 覆盖范围 | 结果 |
|---|---|---|---|
| 1 | ``services/diagnosis_question_quality.has_geo_qualifier`` | 只保护**矫正闸补前缀**那一支 | 诊断 531「深圳深圳龙岗医美哪家靠谱？」 |
| 2 | ``tools/keyword_cluster._extract_stem`` | 只保护关键词变体扩展 | 未见事故 |
| 3 | ``tools/batch_pricing`` 直辖市修正(``if city not in kw``) | 只保护报价词补市名 | 未见事故 |
| 4 | ``services/geo_douyin/title_engine`` | **一处都没有** | post 14「深圳深圳全屋定制哪家好哪家好？…」 |

同型第三次出事(诊断 531 → post 14)说明问题不在某一条链,而在于
**这条规则被实现了 N 次、每次只覆盖一支**。所以本模块是唯一实现,
所有「城市 + 关键词 + 后缀」形态的**标题**拼接必须过它。

## 两条规则(判据严格限定在这两类,不做通用去重)

1. **地名不叠加** —— 关键词里已经带了该城市的任一级地名 token 时,**不再补城市前缀**。
   🔴 只"不加",**从不删**:关键词自己写的「深圳到广州搬家」原样保留,
   所以"标题里城市只能出现一次"这种一刀切不会发生(工单 §2.5 反向对照)。
2. **问句后缀不叠加** —— 关键词以问句/选型后缀结尾,而模板紧跟着又是一个同类后缀时,
   去掉关键词那一份。判据限定在 ``QUESTION_TAILS`` 白名单**且只在拼接边界**生效,
   正文里的正常重复(如"越用越好用")碰不到。

## 谁必须走这里

锁在 ``tests/test_acc3fix_title_hygiene_2026_08_05.py::test_no_bypassing_title_concat_site``:
全仓扫「地名槽紧挨着品类/关键词槽」的拼接形态,命中文件要么 import 本模块,
要么在那份用例的 ``KNOWN_NON_TITLE_SITES`` 里带理由登记。新增调用点绕过 → 用例转红。

## 不并进来的那一处

``services/diagnosis_question_quality`` 那套(R1/R4/R5)**没有**并进来,理由:
它同时承担"判题面合不合格"与"补前缀",且带 ``brands.cities`` 合并池等诊断链独有语义;
工单 §2.4 明令不动它,除非给出行为等价证明。本模块复用它的 ``geo_tokens``
(地名 token 拆分的 SSOT),不复制第五份地名解析。
"""
from __future__ import annotations

import re
from typing import Optional

# 地名 token 拆分复用诊断链的 SSOT ——「贵州省遵义市仁怀市（茅台镇）」这种整串
# 要拆成 ('贵州','遵义','仁怀','茅台镇') 才能判"关键词里带没带地名"。
# 🔴 该函数属另一个在途包(qgate-r567)的文件,本包**不改它**,只读用;
#    用例 test_geo_tokens_contract_pinned 把它的契约钉死,对方改了会让本包转红
#    而不是让本模块静默退化成"永远判没带地名"。
from services.diagnosis_question_quality import geo_tokens

__all__ = [
    "QUESTION_TAILS",
    "effective_city_prefix",
    "trim_duplicate_tail",
    "compose_title",
    "join_city_keyword",
]

# 问句/选型后缀白名单。**长的排前面**,否则"哪家好"会被"哪家"先吃掉半截。
# 这份表故意只收「查询意图后缀」,不收任何品类词/形容词 —— 工单 §2.4:
# 判据要限定在城市名与问句后缀两类,不许做通用去重。
QUESTION_TAILS: tuple[str, ...] = (
    "哪家比较好", "哪家最靠谱", "哪家价格便宜", "哪家价格实在",
    "哪家靠谱", "哪家好", "哪家强", "哪个好", "哪个靠谱",
    "怎么选", "如何选", "怎么挑", "如何挑", "怎么样",
    "推荐", "排名", "排行榜", "十大", "前十名",
)

_WS_RE = re.compile(r"[\s　]+")
# compose_title 用的占位哨兵:先把模板渲染出来,再看关键词槽**后面**紧跟着什么。
# 直接 split("{kw}") 做不到 —— "{city}{kw}{n}强名单整理" 后面跟的是另一个占位符,
# 得等 {n} 也填完才知道那里到底是不是一个问句后缀。
_KW_SENTINEL = "\x00__KW__\x00"


def _norm(text) -> str:
    """去空白(与 title_engine 原有 ``_norm`` 同义,合并到共用件避免第二份)。"""
    return _WS_RE.sub("", str(text or "").strip())


def effective_city_prefix(city, keyword) -> str:
    """真正该拼进标题的城市前缀。关键词里已带该地名 → 返回空串。

    >>> effective_city_prefix("深圳", "深圳全屋定制哪家好")
    ''
    >>> effective_city_prefix("深圳", "全屋定制")
    '深圳'
    >>> effective_city_prefix("广东省深圳市", "深圳全屋定制")
    ''

    🔴 判"带没带"用 token 命中(整串 + 逐级地名),判"加什么"仍用调用方给的原值 ——
       只改"加不加",不改"加什么",这样不含地名的老行为逐位不变。
    """
    c = _norm(city)
    if not c:
        return ""
    kw = _norm(keyword)
    if not kw:
        return c
    if c in kw:
        return ""
    for token in geo_tokens(c):
        if token and token in kw:
            return ""
    return c


def trim_duplicate_tail(keyword, following: str = "") -> str:
    """关键词尾部的问句后缀与模板紧跟的后缀撞车时,去掉关键词那一份。

    >>> trim_duplicate_tail("深圳全屋定制哪家好", "哪家好？5家实测对比")
    '深圳全屋定制'
    >>> trim_duplicate_tail("深圳全屋定制哪家好", "top5分享")   # 后面不是问句后缀 → 不动
    '深圳全屋定制哪家好'
    >>> trim_duplicate_tail("全屋定制", "哪家好？")             # 关键词没带后缀 → 不动
    '全屋定制'

    两侧都必须是白名单后缀才动手:``following`` 是模板的固定文案,
    只有它本身就是一个问句后缀时,关键词再带一个才叫"叠了两遍"。
    """
    kw = _norm(keyword)
    if not kw:
        return kw
    nxt = str(following or "")
    if not any(nxt.startswith(t) for t in QUESTION_TAILS):
        return kw
    for tail in QUESTION_TAILS:
        if kw.endswith(tail):
            trimmed = kw[: -len(tail)]
            # 整条关键词就是一个后缀(如 kw='推荐')→ 剪完是空,那就别剪。
            return trimmed or kw
    return kw


def join_city_keyword(city, keyword, suffix: str = "") -> str:
    """非模板的裸拼接(hashtag / prompt 主题词那种)走这里。

    >>> join_city_keyword("深圳", "深圳全屋定制哪家好", "哪家好")
    '深圳全屋定制哪家好'
    >>> join_city_keyword("深圳", "全屋定制", "推荐")
    '深圳全屋定制推荐'
    """
    kw = _norm(keyword)
    if not kw:
        return _norm(city)
    prefix = effective_city_prefix(city, kw)
    body = trim_duplicate_tail(kw, suffix)
    return f"{prefix}{body}{_norm(suffix)}"


def compose_title(pattern: str, *, city=None, keyword: str = "", **fields) -> str:
    """按模板拼标题。模板用 ``{city}`` / ``{kw}`` 两个槽,其余槽走 ``fields``。

    >>> compose_title("{city}{kw}哪家好？{n}家实测对比", city="深圳",
    ...               keyword="深圳全屋定制哪家好", n=5)
    '深圳全屋定制哪家好？5家实测对比'
    >>> compose_title("{city}{kw}哪家好？{n}家实测对比", city="深圳",
    ...               keyword="全屋定制", n=5)
    '深圳全屋定制哪家好？5家实测对比'

    两个例子拼出同一条标题,正是本件要的效果:关键词自带地名/后缀与否,
    结果都只出现一次。
    """
    kw = _norm(keyword)
    prefix = effective_city_prefix(city, kw)
    rendered = pattern.format(city=prefix, kw=_KW_SENTINEL, **fields)
    idx = rendered.find(_KW_SENTINEL)
    if idx < 0:
        # 模板没有 {kw} 槽:没有"被拼的那段",两条规则都无从谈起,原样返回。
        return rendered
    following = rendered[idx + len(_KW_SENTINEL):]
    return rendered.replace(_KW_SENTINEL, trim_duplicate_tail(kw, following))


def normalize_whitespace(text) -> str:
    """给调用方复用的去空白(title_engine 的 ``_norm`` 现在指向这里)。"""
    return _norm(text)


def _tail_of(keyword) -> Optional[str]:
    """关键词尾部命中的白名单后缀(给用例/排障用,不参与拼接)。"""
    kw = _norm(keyword)
    for tail in QUESTION_TAILS:
        if kw.endswith(tail):
            return tail
    return None
