"""题面来源(origin)与**品牌定向豁免**的唯一判定处。

## 为什么要有这个模块

#149 之后题单里会混两种题:AI 出的候选(直接进题单)与客户自己写的。
在此之前 verbatim 模式默认「题单里全是客户自己写的题」,于是有一条豁免:

    aggregate_dimension_stats(..., brand_name=("" if mode_verbatim else brand_name))

—— 传空品牌名 = 关掉「题面含品牌名就归品牌认知层」那条纠正。
对**客户自己写的**题这是对的(她想问什么就照问,不替她改归层);
对 **AI 出的**题就不对了:Owner 截图里第一道候选就是
「全域上榜(深圳)科技有限公司是做什么的?」——
这种品牌定向题必然命中自己,沿用豁免会让它进竞争格局分母,把提及率顶高
(WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2 / 报告 551 那类病)。

## 🔴 这个谓词只准写这一处

`is_custom_mode or diagnosis_mode == "verbatim"` 这一行今天散在四个消费点。
四份「应该等价」的实现漂开那天不会有任何东西变红,而表现是
**同一次诊断,报告说的分层和实际算分的分层不一样**。

所以四个消费点一律调本模块,**不准再复制那一行**。

## 数据在哪

`question_origins` 与 `is_custom_mode` 一样存在 `ai_visibility_data` 里 ——
消费点拿到的 `ai_data` 就是它,**够不到** `results["input_params"]`
(那里也存了一份 `question_meta`,那份是给**复测继承**用的)。
两处存的是同一件事的两种用途,不是重复:一份给下游算分,一份给下次复测。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 客户自己写的题。**缺元数据时的默认** —— 老前端不送 `question_meta`,
#: 按 customer 处理才能与改前逐字节同行为。
ORIGIN_CUSTOMER = "customer"
#: 页面加载时 AI 出的候选,用户没删就留在题单里。
ORIGIN_AI = "ai_suggested"
#: 系统默认题(防守/全面线的冻结题单里有)。
ORIGIN_SYSTEM = "system"


def origins_from_meta(question_meta: Any) -> dict:
    """`question_meta` 列表 → `{题面: origin}`。

    容忍脏数据:非 dict 的条目跳过,缺 text 的跳过 —— 它来自请求体,
    而请求体是外部输入。
    """
    out: dict = {}
    for m in question_meta or []:
        if not isinstance(m, (dict, Mapping)):
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        out[text] = str(m.get("origin") or ORIGIN_CUSTOMER)
    return out


def is_verbatim(ai_data: Any) -> bool:
    """题单模式(照题单原文跑,不由系统另出题)。

    🔴 今天散在四处的 `bool(is_custom_mode) or diagnosis_mode == "verbatim"`
       只写这一处。
    """
    if not isinstance(ai_data, (dict, Mapping)):
        return False
    return (bool(ai_data.get("is_custom_mode"))
            or ai_data.get("diagnosis_mode") == "verbatim")


def brand_filter_exempt_questions(ai_data: Any,
                                  questions: Iterable[str] = ()) -> tuple:
    """🔴 **哪些题享品牌定向豁免** —— 本仓只准这一处回答。

    · 非 verbatim:一道都不豁免(与改前一致 —— 那时豁免只在 verbatim 里给)。
    · verbatim:只豁免 `origin == customer` 的题。
      缺元数据的题按 customer ⇒ 老前端提交的纯客户题单,
      豁免集 = 全部题 = 改前的「整体豁免」,**逐字节同行为**。
      全 AI 的题单豁免集为空 ⇒ 品牌定向题正确归品牌认知层(本单的目的)。
    """
    if not is_verbatim(ai_data):
        return ()
    origins = ai_data.get("question_origins") if isinstance(ai_data, (dict, Mapping)) else None
    origins = origins if isinstance(origins, (dict, Mapping)) else {}
    return tuple(
        q for q in (questions or ())
        if str(origins.get(q) or ORIGIN_CUSTOMER) == ORIGIN_CUSTOMER
    )


def origin_counts(ai_data: Any, questions: Iterable[str] = ()) -> dict:
    """`{ai_suggested: a, customer: b, system: c}` —— 报告文案按它改写。

    「本次按您填写的 N 个原题跑」在全 AI 题单上是**假话**,
    而假话在屏幕上和真话长得一模一样。
    """
    origins = ai_data.get("question_origins") if isinstance(ai_data, (dict, Mapping)) else None
    origins = origins if isinstance(origins, (dict, Mapping)) else {}
    counts = {ORIGIN_AI: 0, ORIGIN_CUSTOMER: 0, ORIGIN_SYSTEM: 0}
    for q in (questions or ()):
        key = str(origins.get(q) or ORIGIN_CUSTOMER)
        counts[key] = counts.get(key, 0) + 1
    return counts


def has_customer_questions(ai_data: Any, questions: Iterable[str] = ()) -> bool:
    """题单里**含**客户自己写的题。

    这是 `is_custom_mode` 在报告文案里的新语义(#149 §2.3.3):
    「您填写的问题」这个说法只有在她真的填了题时才成立。
    """
    return origin_counts(ai_data, questions).get(ORIGIN_CUSTOMER, 0) > 0


def split_questions_needing_layers(question_meta: Any,
                                   questions: Iterable[str] = ()) -> tuple:
    """`(已知层的 {题: 层}, 还需要 LLM 归层的题列表)`。

    AI 出的候选**已经带层**(生成器算过一次),只有客户手写的题才要再花一次 LLM。

    🔴 返回的是「分工」而不是「跳过」:分类器调用点保留 ——
       全 customer 无 layer 时 `need` = 全部题,它照常被调用。
       把调用点整个砍掉,会让「客户手写题不归层」那个老缺陷
       (report 325 总分被低估的根因)悄悄回来。
    """
    known = {}
    for m in (question_meta or []):
        if not isinstance(m, (dict, Mapping)):
            continue
        text = str(m.get("text") or "").strip()
        layer = str(m.get("layer") or "").strip()
        if text and layer:
            known[text] = layer
    need = [q for q in (questions or ()) if q not in known]
    return known, need


async def resolve_verbatim_question_types(question_meta, questions, *, classify):
    """verbatim 分支的**归层分工** —— 增长线唯一的归层入口。

    🔴 [#149 返修] 本函数住在 services 而不是工作流里,是因为本仓约定
       **判据不 import `workflows/diagnosis_workflow`**(它 import 就连库;
       r567 那个包的 docstring 写明了这条)。实测同跑还会让 pytest 拆 capture 时
       炸 `I/O operation on closed file` —— 行为臂够不着的东西等于没锁,
       所以把逻辑放到够得着的地方,再用结构臂钉住调用点。

    `classify(need)` 由调用方注入(工作流传它自己的 LLM 分类器)。
    `need` 为空时**根本不调用** —— 全带层的题单省一次 LLM;
    全 customer 无 layer 时 `need` = 全部题,分类器照常被调用。
    """
    known, need = split_questions_needing_layers(question_meta, questions)
    types = dict(known)
    if need:
        types.update(await classify(need))
    return types
