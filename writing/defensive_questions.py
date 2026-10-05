"""[#185] 防御型(公司词)题面 —— 品牌名 + 企业八问。

Owner 口径:「防御型 GEO 主要是问公司怎么样,相当于是公司的词,
这个非常好打上去,因为没多少人会和你抢公司的名字」。

🔴 本模块**不调 LLM**:题面是确定的。八问是 WO_185 §6 逐字定的,
   顺序也是它定的 —— 前端徽章与缺事实提示按问名显示,两边必须是同一份。
   LLM 可以在别处润色标题,但**品牌名必入、每问恰好一题**由这里保证,
   润色结果违反就落回模板题(润色是加分项,不是把关项)。
"""
from __future__ import annotations

from typing import List

#: 企业八问 —— 逐字取自 WO_185 §6,**顺序即出题顺序**。
#: 🔴 第六问是「适合谁·不适合谁」,不是「适合谁」:
#:    转述里丢过后半句,而前端徽章按问名匹配,差半句就对不上。
DEFENSIVE_QUESTIONS: tuple[str, ...] = (
    "怎么样",
    "靠谱吗·口碑",
    "投诉·售后",
    "与同类的差异(不点名不排名)",
    "资质·案例",
    "适合谁·不适合谁",
    "价格·收费",
    "团队·流程",
)

#: 题面模板:问名 → 标题后半句。品牌名在前,保证「必含品牌名」是结构性的。
_TITLE_SUFFIX = {
    "怎么样": "怎么样?",
    "靠谱吗·口碑": "靠谱吗?真实口碑与评价",
    "投诉·售后": "有投诉吗?售后怎么处理",
    "与同类的差异(不点名不排名)": "和同类服务有什么不同",
    "资质·案例": "有哪些资质与实际案例",
    "适合谁·不适合谁": "适合哪些客户,不适合谁",
    "价格·收费": "怎么收费?价格贵不贵",
    "团队·流程": "的团队与服务流程是怎样的",
}

#: 超过 8 篇时的企业问法**变体**。
#: 🔴 「不凑数」:变体用完就停,**不循环回第一问凑满 N 篇**。
#:    凑出来的重复题会让客户看到两条几乎一样的标题,而那比少两篇更难解释。
_VARIANTS: tuple[tuple[str, str], ...] = (
    ("怎么样", "值得选吗?一次说清"),
    ("资质·案例", "做过哪些同行业的项目"),
    ("价格·收费", "报价包含哪些内容"),
    ("投诉·售后", "出了问题找谁、多久响应"),
    ("适合谁·不适合谁", "什么情况下不建议找我们"),
    ("团队·流程", "接单到交付要多久"),
)


def defensive_capacity() -> int:
    """这个方向**最多**能出多少篇不重复的题 = 八问 + 变体。

    🔴 单一来源:前端的「防御型最多 N 篇」与配比封顶都读它,
       两边各自写死一个 14,加一个变体就会对不上,而对不上的那一天
       只表现为"配比里能选 15 篇、实际出来 14 篇",没人会红。
    """
    return len(DEFENSIVE_QUESTIONS) + len(_VARIANTS)


def defensive_titles(brand_name: str, count: int, exclude_titles=()) -> List[dict]:
    """出 `count` 条防御题。返回 `[{"question": 问名, "title": 标题}, ...]`。

    · 前 8 条按八问顺序各一;
    · 超过 8 条走变体,**变体用完就停**(不凑数,宁可少给);
    · 每条标题**必含品牌名**——品牌名在最前,是结构性的不是校验出来的。
    · `exclude_titles`:同一单别的词**已有**的防御题标题(Review 09-28 · WO_317 第三笔)。
      子集批次只看得见自己这批,不排除就会把别的词已经出过的那一问再出一条。
      按标题原文认(库里不存问名,而问名在变体里会重复,标题 = 品牌名 + 固定后缀是唯一的)。
    """
    brand = str(brand_name or "").strip()
    if not brand:
        # 没有品牌名就没有"公司词"可言。返空而不是出一条没有主语的题。
        return []
    n = max(0, int(count or 0))
    skip = {str(t) for t in (exclude_titles or ())}
    candidates = [(q, "%s%s" % (brand, _TITLE_SUFFIX[q])) for q in DEFENSIVE_QUESTIONS]
    candidates += [(q, "%s%s" % (brand, suffix)) for q, suffix in _VARIANTS]
    out: List[dict] = []
    for question, title in candidates:
        if len(out) >= n:
            break
        if title in skip:
            continue
        out.append({"question": question, "title": title})
    # 🔴 只出得了 `defensive_capacity()` 条。**不循环回第一问凑满 n** ——
    #    也不由本函数决定剩下那几篇怎么办:那是调用方的事,
    #    而调用方必须**点名**它们(保持原方向),不许静默换成普通题。
    #    静默换掉的话,「全部设为防御型」就是一句假话,而用户看不出来。
    return out


#: 每一问**靠哪些事实**才写得实。
#:
#: 🔴 列名逐列核对过 `client_profiles`(`.deploy_toolkit/prod_schema_2026-09-05.sql`),
#:    不是照工单转述 —— 工单只写了「来源 client_profile / 知识库 / brands 字段」,
#:    没点名到列;照转述写会出现一个**库里根本没有**的字段名,
#:    而那样的提示永远为「缺」,用户补不了也看不懂。
#:
#: 🔴 「价格·收费」与「团队·流程」**故意是空元组**:`client_profiles` 里确实
#:    没有价格类/团队类的结构化列(逐列核过)。空元组不是"忘了填",
#:    它走的是另一句提示(让用户上传材料到品牌知识库),见 `facts_hint_for`。
QUESTION_FACT_FIELDS: dict = {
    "怎么样": ("company_intro", "business", "core_value"),
    "靠谱吗·口碑": ("testimonials", "successful_patterns"),
    "投诉·售后": ("negative_feedback", "brand_constraints"),
    "与同类的差异(不点名不排名)": ("differentiation", "competitors"),
    "资质·案例": ("success_cases", "products"),
    "适合谁·不适合谁": ("target_users", "pain_points"),
    "价格·收费": (),
    "团队·流程": (),
}

#: 列名 -> 人话。元指令第 11 条:工程词不出现在用户面前。
#: 提示里要说「去补哪一栏」,说的必须是客户资料页上**看得见的那个名字**。
_FIELD_LABELS: dict = {
    "company_intro": "公司简介",
    "business": "主营业务",
    "core_value": "核心价值",
    "testimonials": "客户评价",
    "successful_patterns": "成功经验",
    "negative_feedback": "常见问题与反馈",
    "brand_constraints": "品牌注意事项",
    "differentiation": "差异化优势",
    "competitors": "同类对比资料",
    "success_cases": "成功案例",
    "products": "产品与资质",
    "target_users": "目标客户",
    "pain_points": "客户痛点",
}

_NO_COLUMN_HINT = ("客户资料里没有这一问对应的栏位,可以把报价说明、服务流程这类"
                   "材料传到品牌知识库,写稿时会用上")


def facts_hint_for(question: str, facts) -> dict:
    """这一问**有没有事实可写**,没有的话缺哪几栏。

    只提示,不阻断(零瞎编红线另有守卫:缺事实就不写,不是不让出题)。
    返回 ``{"question", "has_facts", "missing", "hint"}``,`missing` 已是人话。
    """
    fields = QUESTION_FACT_FIELDS.get(question, ())
    src = facts if isinstance(facts, dict) else {}
    if not fields:
        return {"question": question, "has_facts": False,
                "missing": [], "hint": _NO_COLUMN_HINT}
    present = [f for f in fields if str(src.get(f) or "").strip()]
    if present:
        return {"question": question, "has_facts": True, "missing": [], "hint": ""}
    missing = [_FIELD_LABELS.get(f, f) for f in fields]
    return {"question": question, "has_facts": False, "missing": missing,
            "hint": "这一问缺事实,建议先补「%s」" % "」「".join(missing)}


#: 一篇**没被转成防御型**的原因。前端要按原因说不同的话,所以是枚举不是布尔。
UNCONVERTED_REASONS: tuple[str, ...] = (
    "capacity_exceeded",   # 超出八问 + 变体的上限 —— 再出就是重复题
    "fixed_slot",          # 系统固定槽(company_profile),不归用户配比管
    "not_retitlable",      # 连品牌名都没有 —— 没有主语就没有「公司词」
)


def plan_defensive_titles(brand_name: str, topics: List[dict], exclude_titles=()) -> tuple[list, list]:
    """把一批选题分成「能转成防御题的」与「保持原方向的」。

    `topics` 按**篇序**给(调用方决定顺序,通常是 topic id 升序)。
    每项至少要有 ``id``;``is_fixed`` 缺省当 False。

    返回 ``(applied, unconverted)``:
      · applied     = ``[{"topic_id", "question", "title"}, ...]``
      · unconverted = ``[{"topic_id", "reason"}, ...]``,reason ∈ `UNCONVERTED_REASONS`

    🔴 **不凑数、也不静默**。超出上限的篇**保持原方向不动**,并且必须被**点名**
       回给前端 —— 静默换成普通题的话,「全部设为防御型」就是一句假话,
       而用户从列表上看不出来(标题本来就各不相同)。
    🔴 固定槽不动:`company_profile` 是系统每项目固定 1 篇,不归用户配比管
       (Review 09-13 裁定「不动固定槽联合判定」)。
    """
    rows = list(topics or [])
    brand = str(brand_name or "").strip()

    unconverted: list = []
    eligible: list = []
    for t in rows:
        if t.get("is_fixed"):
            unconverted.append({"topic_id": t.get("id"), "reason": "fixed_slot"})
        else:
            eligible.append(t)

    if not brand:
        # 没有品牌名 ⇒ 一篇都转不了。返空而不是出一堆没有主语的题。
        unconverted.extend({"topic_id": t.get("id"), "reason": "not_retitlable"}
                           for t in eligible)
        return [], unconverted

    titles = defensive_titles(brand, len(eligible), exclude_titles=exclude_titles)
    applied = [{"topic_id": t.get("id"), **titles[i]}
               for i, t in enumerate(eligible[:len(titles)])]
    unconverted.extend({"topic_id": t.get("id"), "reason": "capacity_exceeded"}
                       for t in eligible[len(titles):])
    return applied, unconverted


def title_carries_brand(title: str, brand_name: str) -> bool:
    """标题里有没有品牌名 —— 润色路径的**把关项**。

    润色可以改写法,但改掉主语就不是公司词了;这一条不过就落回模板题。
    """
    return bool(str(brand_name or "").strip()) and \
        str(brand_name).strip() in str(title or "")


# ═══════════════════════════════════════════════════════════════════════
# #185 c3 · 把关器:LLM 出的防御题过不过
#
# Owner 2026-09-13 定的形态是「标题走 LLM,这八问降为**把关与兜底**」。
# 所以本段不再决定题面,只回答一句:**这条 LLM 题能不能用**;
# 不能用就落回 `plan_defensive_titles` 给的那条模板题,并**标出来**。
#
# 🔴 `source` 必须回给前端(`llm` / `template_fallback`)。
#    静默兜底是最坏的一种:用户以为 AI 写了,实际是模板,而两者长得一样。
# ═══════════════════════════════════════════════════════════════════════

#: 兜底原因。**逐条可读**,不要一个笼统的 "invalid"——
#: 「为什么落回模板」是下一个人调 prompt 的唯一线索。
FALLBACK_REASONS: tuple[str, ...] = (
    "missing_brand",        # 标题里没有品牌名 ⇒ 主语丢了,就不是公司词
    "duplicate_title",      # 与本批另一条重复
    "empty",                # LLM 没给这一槽
    "too_long",             # 超长(前端一行放不下,且长题在引擎里表现差)
)

#: 标题长度上限。与 c2 模板题的量级一致;超了就不是"润色"是"另写一篇"。
_MAX_TITLE_CHARS: int = 48


def vet_llm_titles(brand_name: str, planned: List[dict],
                   llm_titles: dict) -> List[dict]:
    """逐槽把关。返回 `[{topic_id, question, title, source, fallback_reason?}, ...]`。

    `planned`    = `plan_defensive_titles()` 的 `applied`,**槽与问名的唯一出处**;
    `llm_titles` = `{topic_id: LLM 出的标题}`。

    🔴 问名**不由 LLM 决定**:它由 `planned` 定死。LLM 只负责把那一问写得好看 ——
       让 LLM 自己挑问名的话,「八问覆盖」这件事就没有分母了
       (它会挑好写的那几问,而用户看到的是"防御型"三个字)。
    🔴 去重跨整批,不只跨同一问:两条不同问名却写成同一句话,对用户就是重复。
    """
    brand = str(brand_name or "").strip()
    out: List[dict] = []
    seen: set = set()
    for row in (planned or []):
        tid = row.get("topic_id")
        question = row.get("question")
        template_title = row.get("title")
        candidate = str((llm_titles or {}).get(tid) or "").strip()

        reason = None
        if not candidate:
            reason = "empty"
        elif not title_carries_brand(candidate, brand):
            reason = "missing_brand"
        elif len(candidate) > _MAX_TITLE_CHARS:
            reason = "too_long"
        elif candidate in seen:
            reason = "duplicate_title"

        if reason:
            title, source = template_title, "template_fallback"
        else:
            title, source = candidate, "llm"

        # 🔴 模板题也要进 `seen`:兜底之后仍可能与后面某条 LLM 题撞上,
        #    而那一条撞上的才是"重复",不是这一条。
        if title in seen:
            # 连模板题都撞了(同一问被排了两次 —— 上游本不该发生),
            # 仍然点名而不是静默丢:形状固定优于"少给一条"。
            reason = reason or "duplicate_title"
        seen.add(title)

        item = {"topic_id": tid, "question": question, "title": title,
                "source": source}
        if source == "template_fallback":
            item["fallback_reason"] = reason
        out.append(item)
    return out
