"""缺口作战计划 · 统一决策源(P4 · A1/A4/A6)

合同 §8:页面头部、任务卡、小榜必须消费**同一个**版本化快照,禁止三个入口各自重算。
本模块是那个唯一的生成器。

设计上的三条硬边界(每条都对应一次真实事故风险):

1. 🔴 **不建第二套媒体推荐**(工单 R4)。目标网站一律来自现役
   `services.placement_service.recommend_for_publish_v2` +`get_industry_engine_scores`
   (底层 geo_engine_stats,2026-08-08 生产实测 35469 行)。本模块只做「挑一个 + 说人话」,
   不做评分、不做分组、不做去重排序 —— 那些都在那台机器里。

2. 🔴 **不建第四套域名分级**(工单 R5)。权威度现读 `domain_authority_cache.tier`
   的现役五档;可进入性现读 `media_outlets.entry_assessment`(迁移 029 新列)。
   两个轴各有自己的现役来源,本模块一个都不复制。

3. 🔴 **容量守卫是服务端唯一出口**。`_actions_for_item()` 是全模块**唯一**能签发
   `open_writing_task` 的地方。
   [WO_225-c1 §8.5 · Owner 2026-09-15 ④] 它问的**不再是** `capacity.executable`,
   而是 `capacity.payable`:已付款项目内写作不设容量闸(想写多少写多少,按篇正常扣费),
   **未付款**才剔除写类动作。报价 372 是已付款的,所以它现在**应当**出「去写这篇」,
   并带 `over_capacity: true` 供显示。
   前端仍只是显示层 —— 删掉 `and not capacity.payable`,未付款单 998 立刻出现「去写这篇」,
   `test_capacity_zero_never_emits_write_action` 必须转红;把条件改回
   `not capacity.executable`,`test_paid_over_capacity_still_emits_write_action` 转红。

不做的事(做了就是越界):
  · 不调 LLM(解释层在小榜,且模型不可用时本模块照常工作)
  · 不扣费、不发布、不改报价、不动容量(报价写链归 P1)
  · 不写 geo_article_* 那台全黑的 closed-loop 机器
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from db import gap_plan_db
from services import article_capacity_contract as capacity_contract
from services.gap_operation_labels import (
    anonymize_platforms,
    assert_no_internal_leak,
    labels_version,
    translate_action,
    translate_status,
)

logger = logging.getLogger("GEO-GapPlan")

SNAPSHOT_VERSION = "gap-plan-v1"
RULE_VERSION = "gap-plan-rules-v1"

# 🔴🔴 容量口径 SSOT = P1 的 services/article_capacity_contract,**本模块不再自算**。
#
#    并轨之前我认的是 `quotes.total_articles`,那是错的 —— P1 实证:
#      · 137 张有词的单里 **52 张 total_articles 与词表对不上,且全是 0**(脏数据不是容量)
#      · 真正被交付侧消费的是 SUM(confirmed_keywords.required_articles)
#      · 容量占用单位是 **topics(交付槽)**,不是 articles 行数
#    报价 372 三个数打架的真相因此是:authorized 15 / topics 15 / articles 136(重写稿,
#    不占容量)→ 可用 = 0。设计包说的「报价 372 容量 0」**只有按 topics 口径才成立**;
#    按我原来那个 total_articles 口径恒 0 是**碰巧对**,换一张单就错。
#
#    落库的 capacity_source 随之从表名改成合同版本号。
#
# 🔴🔴 但**版本串必须从真实返回值里取,不能写成模块常量**(Review 2026-08-08 指出):
#    常量不随执行路径变 —— 谁把 compute_capacity 改回自算,`capacity_source` 照样
#    印着"用了 P1 合同",落库的那行审计就是一句假话,而且**永远不会被发现**。
#    现在它从 `view["contract_version"]` 派生:数据里没有版本串 = 这份容量不是合同给的
#    → Capacity 构造直接 fail-closed 抛错(见 __init__)。
#    这条与 M10 变异配对:M10 把 compute_capacity 改成自算(返回的 dict 没有
#    contract_version)→ 构造即炸 → 锁转红,而不是悄悄印一个假来源。
CAPACITY_SOURCE_PREFIX = "services.article_capacity_contract@"

MAX_ITEMS = 6

# ════════════════════════════════════════════════════════════════
# 受众(WO_GAPPLAN_RELOCATION_A 2026-08-10 · 阶段 2)
#
# 同一份快照,两个受众:
#   · agent    —— 服务商执行面(原行为,一字不改)
#   · customer —— 客户售前预览(「我们会做什么」)
#
# 🔴 为什么加参数而不是新写一个平行的 present_snapshot_for_customer():
#    `_actions_for_item` 是**全模块唯一**签发写动作的出口(见其 docstring)。
#    复制一份出参装配 = 复制一条通往那个出口的路径,而容量守卫、术语泄漏闸、
#    白名单转录三层都挂在这一条路径上。平行函数迟早只跟其中两层同步。
#
# 🔴 customer 分支**根本不调用** `_actions_for_item`,而不是"调完再把 actions 清空"。
#    清空是后置擦除:哪天有人在中间加一行 `out["actions"] = ...` 就漏了。
#    不调用 = 客户侧与那个出口之间**没有代码路径**。
AUDIENCE_AGENT = "agent"
AUDIENCE_CUSTOMER = "customer"
AUDIENCES = frozenset({AUDIENCE_AGENT, AUDIENCE_CUSTOMER})

# 客户售前版屏蔽的 access 态。🔴 这份清单是判据的一部分:
#    锁 test_customer_preview_has_no_access_wording 会拿这五条的 label/explanation
#    全文去客户出参里搜,一条命中即红。加新 access 态时必须同步加进来,
#    否则新态会绕过这条锁(这正是"白名单要跟着实现走"的同一个坑)。
ACCESS_STATUS_CODES = (
    "self_service_publishable",
    "mediated_publish_required",
    "hold_until_domain_access_confirmed",
    "domain_unreachable",
    "access_unverified",
)

# 缺口态 → P1 不足额原因词表(SHORTFALL_REASONS)。
# 🔴 合同原文:「P4 的缺口计划负责给 reasons;没给 → capacity_shortfall_unexplained,
#    不许静默当成已完成」。这张表就是那份供给。
_SHORTFALL_REASON_BY_ALLOCATION = {
    "retained_capacity_no_gap": capacity_contract.SHORTFALL_REASON_NO_GAP,
    "hold_until_domain_access_confirmed": capacity_contract.SHORTFALL_REASON_DOMAIN_BLOCKED,
    "rejected_duplicate_coverage": capacity_contract.SHORTFALL_REASON_DUPLICATE_COVERAGE,
    "switch_to_adjacent_query": capacity_contract.SHORTFALL_REASON_SWITCH_QUERY,
}


# ════════════════════════════════════════════════════════════════
# 容量
# ════════════════════════════════════════════════════════════════

class Capacity:
    """容量态。**本类不做任何算术** —— 数字全部来自 P1 合同的 `build_capacity_view`。

    它只剩两件事:
      1. `executable` —— 全模块唯一的"能不能写"开关(容量守卫本体)
      2. `as_dict()`  —— 把合同块 + 人话字典拼成给浏览器的形状

    🔴 刻意保留这个壳而不是直接传 dict:守卫要有个**单一的、可被变异打中的落点**。
       合同返回的是数据,守卫是判断 —— 判断留在 P4 这边,数据不再由 P4 生产。
    """

    __slots__ = ("view",)

    def __init__(self, view: dict[str, Any]):
        status = view.get("display_status")
        # 🔴 fail-closed:合同若返回三态之外的值,当场炸。
        #    不做 fallback —— 静默把未知态当成"可写"正是资金/容量类最贵的错法。
        if status not in capacity_contract.CAPACITY_STATUSES:
            raise ValueError(
                f"容量合同返回了三态之外的 display_status={status!r};"
                f"允许值 {capacity_contract.CAPACITY_STATUSES}"
            )
        # 🔴 出处必须在数据里,不在常量里。没有版本串 = 这份容量不是合同给的,
        #    此时若放行,`capacity_source` 会落库一句假话且永不被发现。
        if not str(view.get("contract_version") or "").strip():
            raise ValueError(
                "容量视图缺 contract_version —— 它不是 article_capacity_contract 产出的。"
                "P4 不自算容量;要改口径请改合同,不要在这里绕过。"
            )
        self.view = view

    # —— 只读透传,不重算 ——
    @property
    def authorized(self) -> int:
        return int(self.view.get("authorized_articles") or 0)

    @property
    def reserved(self) -> int:
        return int(self.view.get("reserved_articles") or 0)

    @property
    def available(self) -> int:
        return int(self.view.get("available_articles") or 0)

    @property
    def display_status(self) -> str:
        return str(self.view["display_status"])

    @property
    def source(self) -> str:
        """口径出处 = **真实返回值里的**版本串,不是模块常量。

        写成常量的话,谁把 compute_capacity 改回自算,这里照样印
        "用了 P1 合同" —— 落库审计变成一句永远不会被发现的假话。
        """
        return f"{CAPACITY_SOURCE_PREFIX}{self.view['contract_version']}"

    @property
    def payable(self) -> bool:
        """这单是不是已付款(合同给的,P4 不自判)。

        🔴 键缺失 ⇒ False ⇒ 当未付款处理 ⇒ 写类动作照旧剔除 = **老行为**。
           回落必须落在「什么都没放开」那一侧:把一份读不出付款状态的容量
           当成已付款,等于给未付款的单开了执行口子。
        """
        return bool(self.view.get("quote_is_payable"))

    @property
    def over_capacity(self) -> bool:
        """已付款但槽用完 —— 写照写,只是要在出参上标出来给人看。"""
        return self.payable and not self.executable

    @property
    def executable(self) -> bool:
        """🔴 容量守卫本体。改成恒 True → 报价 372 出现「去写这篇」(变异 M1)。

        判据只看 available:未付款由合同侧折算成 available=0 + capacity_zero
        (合同 §build_capacity_view「未付款一律按容量 0 对外」),
        P4 **不再自己判付款状态** —— 两处各判一次就是第二套口径。
        """
        return self.available > 0

    def as_dict(self) -> dict[str, Any]:
        out = {
            # 合同块原样透传(含 consumed / over_delivered / semantics —— 不藏)
            **{k: v for k, v in self.view.items() if k != "display_status"},
            "display_status": self.display_status,
            "capacity_source": self.source,
            **{k: v for k, v in translate_status(self.display_status).items()
               if k in ("label", "explanation", "tone", "icon")},
        }
        return out


def compute_capacity(quote: dict[str, Any], publications: dict[str, Any]) -> Capacity:
    """读 P1 合同。🔴 `publications` 不再参与容量计算。

    并轨前我拿"已登记发布链接数"当 reserved —— 那是第三套口径:
    合同的占用单位是 topics(交付槽),发布链接数既不是槽也不是文章。
    参数保留只为不改调用方签名;合同当前把 reserved 恒定为 0(计划项还没落成槽)。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        view = capacity_contract.resolve_quote_capacity(cur, int(quote["id"]))
    finally:
        conn.close()
    return Capacity(view)


def shortfall_for(capacity: Capacity, items: list[dict[str, Any]]) -> dict[str, Any]:
    """不足额 + 原因回流(合同 describe_shortfall)。

    🔴 「用不满」不是失败,但**必须说得出为什么**。说不出 → 合同会记成
       `capacity_shortfall_unexplained`,让这件事在数据里留痕而不是被当成正常完成。
    """
    reasons = []
    for item in items:
        code = _SHORTFALL_REASON_BY_ALLOCATION.get(item.get("allocation_code"))
        if code and code not in reasons:
            reasons.append(code)
    return capacity_contract.describe_shortfall(
        capacity_view=capacity.view, reasons=reasons
    )


# ════════════════════════════════════════════════════════════════
# 事实底座
# ════════════════════════════════════════════════════════════════

def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _data_version(facts: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(facts).encode("utf-8")).hexdigest()


def _pick_target_question(keywords: list[dict[str, Any]]) -> dict[str, Any] | None:
    """主词 = 目标问题。取 is_core 的第一条;没有 is_core 就取第一条。"""
    for kw in keywords:
        if kw.get("is_core"):
            return kw
    return keywords[0] if keywords else None


def _gather_facts(quote: dict[str, Any]) -> dict[str, Any]:
    """收集快照的全部源事实。这一份的哈希 = data_version。"""
    from services.media_entity_flywheel import normalize_industry_key

    quote_id = int(quote["id"])
    industry = str(quote.get("industry") or "")
    industry_key = normalize_industry_key(industry)

    keywords = gap_plan_db.list_core_keywords(quote_id)
    target = _pick_target_question(keywords)
    display_query = ""
    if target:
        display_query = str(target.get("monitoring_query") or target.get("keyword") or "")

    # 🔴 先按问题收窄,收窄不到就退回全行业 —— 并把用的是哪一档记进事实。
    #    第一版只做收窄:主词「深圳哪家装修公司靠谱」而答案环境里的 query 是
    #    「深圳装修公司哪家靠谱」,ILIKE 恒 0 命中 → 依据抽屉空着,像"没数据"。
    #    这正是 ranking_source.py 里那条注释警告过的同一类坑(自由文本 vs 受控枚举)。
    environment_scope = "query"
    environment = gap_plan_db.fetch_answer_environment(
        industry_key, query_like=_query_stem(display_query)
    )
    if not environment.get("engines"):
        environment_scope = "industry"
        environment = gap_plan_db.fetch_answer_environment(industry_key)

    media = _media_candidates(
        industry=industry, keyword=display_query, brand_id=quote.get("brand_id") or 0
    )

    # 🔴 可及性核实结论必须进事实集:它变了(运营标了「进不去」)就该出新代际。
    #    第一版把它放在 build_snapshot 里单独查 → 不参与 data_version →
    #    标完「进不去」后快照原样复用,运营看到卡片纹丝不动。锁
    #    test_marking_domain_unreachable_writes_back_and_recomputes 抓到的就是这个。
    assessments = gap_plan_db.get_domain_entry_assessments([m["domain"] for m in media])

    return {
        "environment_scope": environment_scope,
        "assessments": {
            k: {"entry_assessment": v.get("entry_assessment")}
            for k, v in sorted(assessments.items())
        },
        "quote_id": quote_id,
        "brand_id": quote.get("brand_id"),
        "industry": industry,
        "industry_key": industry_key,
        "display_query": display_query,
        "keywords": [
            {"id": k.get("id"), "keyword": k.get("keyword"),
             "required_articles": k.get("required_articles"), "is_core": k.get("is_core")}
            for k in keywords
        ],
        "environment": environment,
        "media": media,
        "total_articles": quote.get("total_articles") or 0,
        "quote_status": quote.get("status"),
        "paid": bool(quote.get("paid_at")),
    }


def _query_stem(query: str) -> str:
    """取问题里最长的中文片段做模糊匹配锚,避免整句 ILIKE 恒不命中。

    生产实测:报价 372 主词「深圳哪家装修公司靠谱」,而答案环境里的 query 是
    「深圳装修公司哪家靠谱」等变体 —— 整句匹配 0 命中,取词干才能对上。
    """
    text = str(query or "").strip()
    if len(text) <= 4:
        return text
    return text[:4]


def _media_candidates(*, industry: str, keyword: str, brand_id: int) -> list[dict[str, Any]]:
    """现役推荐机器的产物。🔴 只取,不重排,不另算分。

    失败时返回空列表并记日志 —— 媒体推荐不可用不该让整个交付计划打不开,
    此时任务卡的「怎么发」会落到 access_unverified 态(有出口,不是死状态)。
    """
    try:
        from services.placement_service import recommend_for_publish_v2

        # [WO_267] 交付计划是这个品牌自己的:品牌名 / 备注 / 种子词当行业判定上下文
        industry_brand = None
        if brand_id:
            try:
                from db.diagnosis_db import get_brand_by_id
                industry_brand = get_brand_by_id(int(brand_id))
            except Exception as exc:               # noqa: BLE001
                logger.warning("读品牌上下文失败 brand_id=%s:%s", brand_id, exc)
        result = recommend_for_publish_v2(
            industry=industry or "", media_type="media", limit=8,
            keyword=keyword or "", brand_id=int(brand_id or 0),
            with_question_family=False, industry_brand=industry_brand,
        ) or {}
    except Exception as exc:                       # noqa: BLE001
        logger.warning("媒体推荐不可用,任务卡将落到「渠道尚未核实」态:%s", exc)
        return []

    out: list[dict[str, Any]] = []
    for group in ("vertical", "generic"):
        for entry in (result.get(group) or []):
            domain = (entry.get("ai_citation_domain") or entry.get("platform") or "").strip()
            if not domain:
                continue
            out.append({
                # 🔴 逐字段白名单转录。推荐机器的原始项里带 price / our_price_points /
                #    our_price_yuan / publish_success_factor —— 那些是内部采购口径,
                #    整包透传就是把成本捅给运营。这里只挑展示需要的四个字段。
                "domain": domain,
                "display_name": (entry.get("media_name") or entry.get("platform") or "").strip(),
                "platform": (entry.get("platform") or "").strip(),
                "group": group,
            })
    return out


# ════════════════════════════════════════════════════════════════
# 组装
# ════════════════════════════════════════════════════════════════

def _access_code_for(domain: str, assessments: dict[str, Any]) -> str:
    row = assessments.get((domain or "").lower())
    if not row:
        return "access_unverified"
    return {
        "self_service": "self_service_publishable",
        "mediated": "mediated_publish_required",
        "unreachable": "domain_unreachable",
        "unknown": "access_unverified",
    }.get(str(row.get("entry_assessment") or ""), "access_unverified")


def _allocation_code_for(*, capacity: Capacity, access_code: str,
                         duplicate_of: str | None, all_unreachable: bool) -> str:
    if duplicate_of:
        return "rejected_duplicate_coverage"
    if all_unreachable:
        return "switch_to_adjacent_query"          # A6 饱和度出口
    # [WO_225-c1 §8.5] 🔴 只有**未付款**才降级成「没订购」。
    #   已付款但槽用完不再降级 —— 否则下面 _actions_for_item 那道守卫放开了也白放:
    #   状态在这里就已经被改成 capacity_zero,而那个分支的 ids 里根本没有写类动作。
    #   (这正是本文件注释里记着的同一个坑:留一段永远走不到的守卫,
    #    读的人以为它在守着。改守卫必须连**上游的降级**一起改。)
    if not can_emit_write_actions(capacity):
        # 🔴 分档只看合同的三态,**不再自己判付款/自己比 authorized**。
        #    并轨前这里读 `capacity.paid` —— 那是 P4 私自维护的第四个事实,
        #    与合同「未付款一律折算成 capacity_zero」重复且迟早打架。
        return ("retained_capacity_no_gap"
                if capacity.display_status == capacity_contract.CAPACITY_STATUS_RESERVED
                else "research_candidate_not_ordered_capacity_zero")
    if access_code in ("mediated_publish_required", "access_unverified"):
        return "hold_until_domain_access_confirmed"
    if access_code == "domain_unreachable":
        return "switch_to_adjacent_query"
    return "ready_to_execute"


def _actions_for_item(*, allocation_code: str, capacity: Capacity) -> list[dict[str, Any]]:
    """🔴🔴 全模块**唯一**签发动作的地方。容量守卫在这里,不在前端。

    [WO_225-c1 §8.5 改口径] 守卫从「容量 0 就剔除」收窄为「**未付款**才剔除」。
    这是**随修法一起改的判据**,不是把锁放松:
      · 未付款 + 容量 0 ⇒ 仍然一条写类动作都不许出(08_billing 未付款不执行);
      · 已付款 + 槽用完 ⇒ 照发,并带 `over_capacity: true` 供显示
        (Owner 2026-09-15 ④「正常计费即可,不用阻断不用提示」)。
    对应锁一分为二:
      · test_capacity_zero_never_emits_write_action —— 未付款那张单,仍必红;
      · test_paid_over_capacity_still_emits_write_action —— 已付款那张单,
        把这里的条件改回 `not capacity.executable` 它立刻转红。
    """
    ids: list[str]
    if allocation_code == "ready_to_execute":
        ids = ["open_writing_task", "submit_publication_link", "explain_plan"]
    elif allocation_code == "hold_until_domain_access_confirmed":
        ids = ["open_media_library", "mark_domain_unavailable"]
    elif allocation_code == "rejected_duplicate_coverage":
        ids = ["show_merged_into"]
    elif allocation_code == "switch_to_adjacent_query":
        ids = ["show_adjacent_queries", "keep_suggestion"]
    elif allocation_code == "retained_capacity_no_gap":
        ids = ["view_checkback_schedule", "open_assistant_advice"]
    else:                                           # capacity_zero / not ordered
        ids = ["request_quote_capacity", "keep_suggestion"]

    # 🔴 唯一的那道:容量不可执行时,写类动作一律剔除。
    #    第一版这里是**两道**(ready 分支里再判一次 + 这一道)。变异 M2 拆掉这一道时
    #    锁没转红 —— 分诊结论不是"锁弱",是那道内层判断**永远走不到**:
    #    _allocation_code_for 在容量不足时早就把状态降级成 capacity_zero 了。
    #    留着一段永远为假的分支 = 一段无法被证伪的代码,读的人还会以为它在守着。
    #    所以删掉内层,只留这一道,并给它配一条直打函数的单元锁
    #    (test_actions_guard_strips_write_actions_even_if_state_says_ready)——
    #    那条锁刻意用"状态说 ready + 容量不足"这个**上游不会产生**的组合来喂它,
    #    因为这道守卫存在的意义就是防上游哪天改松。
    # [WO_225-c1 §8.5 · Owner 2026-09-15 ④] 只对**未付款**剔除写类动作。
    #   已付款项目内写作不设容量闸:想写多少写多少,按篇正常扣 article_gen,
    #   不弹「套餐外」提示。容量合同只做对客户的合同分配/已做读数,不再拦写作。
    #   🔴 `over_capacity` 只进出参给人看,**不改变**任何动作的 enabled ——
    #      Owner 原话「不用阻断不用提示」,所以它不是第二道软闸。
    if not can_emit_write_actions(capacity):
        ids = [a for a in ids if a not in _WRITE_ACTIONS]
    out = []
    for _a in ids:
        _act = translate_action(_a)
        if capacity.over_capacity and _a in _WRITE_ACTIONS:
            _act["over_capacity"] = True
        out.append(_act)
    return out


# 会产生业务副作用(写作任务/发布登记)的动作。
# [WO_225-c1 §8.5] 判据从「容量不足一律不签发」收窄成「**未付款**不签发」。
_WRITE_ACTIONS = frozenset({"open_writing_task", "submit_publication_link"})


def can_emit_write_actions(capacity: Capacity) -> bool:
    """写类动作能不能签发 —— **闸与守卫共用的那一个谓词**。

    🔴 存在的理由不是省几个字符,是 `test_execution_gate_never_diverges_from_
       the_capacity_guard` 抬头写的那句:「两处各判一次就是第二套口径」。
       我这次正是踩了它 —— 改了 `_actions_for_item` 却没动 `_execution_gate`,
       报价 372 当场变成「闸关着但写动作签出来了」,那条锁如实转红。
       抄一遍同样的布尔表达式只是把分叉推迟到下一次改动。

    · 已付款 ⇒ True(哪怕槽用满 —— Owner 2026-09-15 ④ 写作不设容量闸)
    · 未付款 ⇒ False(08_billing:未付款不执行)
    现行合同下未付款必然 `executable is False`,所以这里等价于 `payable`;
    仍写成两项**或**的形式,是因为「能不能写」在语义上就是这两条之一成立,
    而不是「付了款就行」—— 哪天合同让未付款也有可用额度,这里不必跟着改。
    """
    return bool(capacity.executable or capacity.payable)


def _gap_code_for(environment: dict[str, Any], brand_name: str) -> str:
    """客户在不在候选里 → 缺口类型。规则确定性,不过模型。"""
    entities = environment.get("entity_keys") or {}
    if not entities:
        return "entity_gap"
    name = (brand_name or "").strip()
    if name:
        for meta in entities.values():
            if name and name in str(meta.get("entity_name") or ""):
                return "source_gap"        # 已在候选 → 缺的是被引用的内容
    return "attack_absence"                # 同行在、客户不在


_CONTENT_FORMS = ("对比文", "榜单推荐文", "问答解析文", "案例复盘文")


def read_current_snapshot(quote: dict[str, Any]) -> dict[str, Any] | None:
    """纯读取当前事实版本已物化的快照；绝不创建代际或计划项。

    小榜上下文等只读调用方只能走这里。事实/规则版本变化但尚未由现役 P4
    写入口物化时返回 ``None``，避免把旧计划冒充当前计划，也避免助手只读接口
    在用户没有写能力时产生 ``gap_plan_snapshots`` / ``gap_plan_items`` 写入。
    """
    quote_id = int(quote["id"])
    facts = _gather_facts(quote)
    data_version = _data_version(facts)
    snapshot = gap_plan_db.get_snapshot_by_data_version(quote_id, data_version)
    if not snapshot or snapshot.get("rule_version") != RULE_VERSION:
        return None
    return {
        "snapshot": snapshot,
        "items": gap_plan_db.list_items(snapshot["snapshot_id"]),
    }


def build_snapshot(quote: dict[str, Any], *, actor_user_id: int | None = None) -> dict[str, Any]:
    """生成(或复用)一份快照。返回 {"snapshot": ..., "items": [...]}。

    源事实没变(data_version 相同)→ 复用已有代际,不产生新快照。
    这是「三入口消费同一份」的实质保证:同一秒里报价页和小榜各调一次,
    拿到的是同一个 snapshot_id。
    """
    quote_id = int(quote["id"])
    facts = _gather_facts(quote)
    data_version = _data_version(facts)

    existing = gap_plan_db.get_snapshot_by_data_version(quote_id, data_version)
    if existing and existing.get("rule_version") == RULE_VERSION:
        return {"snapshot": existing, "items": gap_plan_db.list_items(existing["snapshot_id"])}

    publications = gap_plan_db.get_publications(quote_id)
    capacity = compute_capacity(quote, publications)

    environment = facts["environment"]
    media = facts["media"]
    assessments = facts["assessments"]          # 已在 _gather_facts 里取,参与 data_version

    # A6 饱和度:候选落点全部核实为进不去 → 建议换题(不新造推荐引擎,只看现有核实结论)
    reachable = [
        m for m in media
        if _access_code_for(m["domain"], assessments) != "domain_unreachable"
    ]
    all_unreachable = bool(media) and not reachable

    latest = gap_plan_db.get_latest_snapshot(quote_id)
    generation = int((latest or {}).get("authority_generation") or 0) + 1

    items = _build_items(
        quote=quote, facts=facts, capacity=capacity,
        media=media, assessments=assessments, all_unreachable=all_unreachable,
        generation=generation,
    )

    summary = _build_summary(
        quote=quote, facts=facts, capacity=capacity, items=items, environment=environment
    )

    snapshot = {
        "snapshot_id": f"dps_{hashlib.sha256(f'{quote_id}:{data_version}:{RULE_VERSION}'.encode()).hexdigest()[:32]}",
        "quote_id": quote_id,
        "brand_id": quote.get("brand_id"),
        "owner_user_id": actor_user_id,
        "snapshot_version": SNAPSHOT_VERSION,
        "rule_version": RULE_VERSION,
        "data_version": data_version,
        "authority_generation": generation,
        "display_query": facts["display_query"],
        "observed_at": _latest_observation(environment),
        "capacity_authorized": capacity.authorized,
        "capacity_reserved": capacity.reserved,
        "capacity_available": capacity.available,
        "capacity_source": capacity.source,
        "summary_jsonb": summary,
        "payload_jsonb": {
            "labels_version": labels_version(),
            "evidence_platforms": _anonymized_platforms(environment),
            # 🔴 不足额原因回流(P1 合同点名要 P4 供给的那份)。
            #    第一版我把它挂在 snapshot 顶层 —— persist_snapshot 用的是具名参数,
            #    根本不收这个键 = **算了但没落库、也没人读**,一段死计算。
            #    现在进 payload_jsonb(真落库)并在 present_snapshot 里翻成人话透出(真消费)。
            "shortfall": shortfall_for(capacity, items),
        },
    }

    try:
        stored = gap_plan_db.persist_snapshot(snapshot, items)
    except Exception as exc:                        # noqa: BLE001
        # 并发下另一个请求先落了同代际 → 读它的,结果一致。
        logger.info("快照落库让位给并发请求,改读最新代际:%s", exc)
        stored = gap_plan_db.get_latest_snapshot(quote_id)
        if not stored:
            raise
        return {"snapshot": stored, "items": gap_plan_db.list_items(stored["snapshot_id"])}

    return {"snapshot": stored, "items": gap_plan_db.list_items(stored["snapshot_id"])}


def _build_items(*, quote, facts, capacity, media, assessments,
                 all_unreachable, generation) -> list[dict[str, Any]]:
    quote_id = int(quote["id"])
    brand_name = str(quote.get("brand_name") or "")
    environment = facts["environment"]
    gap_code = _gap_code_for(environment, brand_name)
    question = facts["display_query"] or str(quote.get("brand_name") or "")

    items: list[dict[str, Any]] = []
    seen_gap_domain: dict[tuple[str, str], str] = {}

    candidates = media[:MAX_ITEMS] or [{"domain": "", "display_name": "", "platform": "", "group": ""}]
    for idx, cand in enumerate(candidates, start=1):
        plan_item_id = f"Q{quote_id}-P{idx:02d}"
        domain = cand.get("domain") or ""
        access_code = _access_code_for(domain, assessments)
        content_form = _CONTENT_FORMS[(idx - 1) % len(_CONTENT_FORMS)]

        # 重复覆盖:同一个(缺口类型, 目标域名)已经有一篇了 → 合并,省一篇额度
        dedupe_key = (gap_code, domain.lower())
        duplicate_of = seen_gap_domain.get(dedupe_key)
        if duplicate_of is None and domain:
            seen_gap_domain[dedupe_key] = plan_item_id

        allocation_code = _allocation_code_for(
            capacity=capacity, access_code=access_code,
            duplicate_of=duplicate_of, all_unreachable=all_unreachable,
        )

        items.append({
            "plan_item_id": plan_item_id,
            "quote_id": quote_id,
            "ordinal": idx,
            "target_question": question,
            "content_form": content_form,
            "target_platform": cand.get("platform") or "",
            "target_domain": domain,
            "gap_code": gap_code,
            "access_code": access_code,
            "allocation_code": allocation_code,
            "duplicate_of_item_id": duplicate_of,
            "rationale_jsonb": {
                "why": translate_status(gap_code)["explanation"],
                "what": f"围绕可核验的服务范围、方法和案例，用{content_form}的写法解释怎么选。",
                "how": translate_status(access_code)["explanation"],
            },
            # 🔴 逐字段写死,**不 `**cand` 展开**。cand 来自现役推荐机器,
            #    原始项里带 price / our_price_points / our_price_yuan /
            #    publish_success_factor;frozen_spec 是会经 item 端点直达浏览器的,
            #    展开一次就把内部采购口径捅出去了(变异 M6 打的就是这里)。
            "frozen_spec_jsonb": {
                "target_question": question,
                "content_form": content_form,
                "single_selling_point": "",
                "target_domain": domain,
                "target_display_name": cand.get("display_name") or "",
                "authority_generation": generation,
                "snapshot_version": SNAPSHOT_VERSION,
            },
        })
    return items


def _latest_observation(environment: dict[str, Any]):
    stamps = [
        meta.get("last_observed_at")
        for meta in (environment.get("engines") or {}).values()
        if meta.get("last_observed_at")
    ]
    return max(stamps) if stamps else None


def _anonymized_platforms(environment: dict[str, Any]) -> list[dict[str, Any]]:
    """依据抽屉的四平台匿名化卡(合同 §4 第三层)。

    🔴 只出:匿名平台名 / 最近观察日期 / 候选数量 / 客户是否出现 / 引用域 / 能否进入。
       不出:原始回答正文、模型名、供应商名、置信度小数。
    """
    engines = environment.get("engines") or {}
    mapping = anonymize_platforms(engines.keys())
    cards = []
    for engine, meta in sorted(engines.items()):
        cards.append({
            "platform_label": mapping.get(engine, "AI 平台"),
            "candidate_count": int(meta.get("candidate_count") or 0),
            "answer_count": int(meta.get("answer_count") or 0),
            "last_observed_at": meta.get("last_observed_at"),
            "citation_domains": list(meta.get("citation_domains") or [])[:8],
        })
    return cards


def _build_summary(*, quote, facts, capacity, items, environment) -> dict[str, Any]:
    engines = environment.get("engines") or {}
    total_candidates = sum(int(m.get("candidate_count") or 0) for m in engines.values())
    brand_name = str(quote.get("brand_name") or "客户")
    customer_present = any(
        brand_name and brand_name in str(meta.get("entity_name") or "")
        for meta in (environment.get("entity_keys") or {}).values()
    )

    counts = {"ready": 0, "waiting": 0, "merged": 0, "switch": 0, "not_ordered": 0}
    for item in items:
        code = item["allocation_code"]
        if code == "ready_to_execute":
            counts["ready"] += 1
        elif code == "hold_until_domain_access_confirmed":
            counts["waiting"] += 1
        elif code == "rejected_duplicate_coverage":
            counts["merged"] += 1
        elif code == "switch_to_adjacent_query":
            counts["switch"] += 1
        else:
            counts["not_ordered"] += 1

    if total_candidates and not customer_present:
        headline = (f"AI 回答这个问题时已经在推荐 {total_candidates} 个同行位置，"
                    f"客户一次没出现。")
        next_step = "先补齐可信信息，再确认一个可以进入的发布落点。"
    elif total_candidates:
        headline = f"客户已经出现在 AI 的候选里（同行位置共 {total_candidates} 个）。"
        next_step = "接下来补被引用的内容，让 AI 更常引到客户这一条。"
    else:
        headline = "还在整理这个问题的真实回答。"
        next_step = "整理完成后会生成交付计划。"

    return {
        "headline": headline,
        "next_step": next_step,
        "status_counts": counts,
        "customer_present": bool(customer_present),
        "total_candidates": total_candidates,
        "capacity_notice": (
            translate_status(capacity.display_status)["explanation"]
            if not capacity.executable else ""
        ),
        # 篇数是上限不是完成率(合同 semantics=upper_bound_0_to_capacity)
        "capacity_semantics": capacity.view.get("semantics"),
    }


# ════════════════════════════════════════════════════════════════
# 出参装配(唯一出口 · 机械闸在这里)
# ════════════════════════════════════════════════════════════════

def _present_shortfall(raw: dict[str, Any] | None) -> dict[str, Any]:
    """不足额块出参形态:原因码 → 人话。

    🔴 不裸出 `domain_not_accessible` 这类码(界面直出内部枚举正是 B5 要防的),
       但也**不藏** —— 用不满且说不出为什么,必须让运营看见 `capacity_shortfall_unexplained`
       翻出来的那句"原因待确认",而不是被当成正常完成。
    """
    if not raw:
        return {"shortfall_articles": 0, "counts_as_failure": False, "reasons": []}
    return {
        "shortfall_articles": int(raw.get("shortfall_articles") or 0),
        "counts_as_failure": bool(raw.get("counts_as_failure", False)),
        "reasons": [translate_status(code) for code in (raw.get("reasons") or [])],
    }


def _customer_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """客户售前版的头部。只留「AI 现在什么样 + 我们接下来做什么」两件事。

    🔴 砍掉的三样都是**服务商口径**,不是"藏起来":
       · status_counts(可开工/等渠道/已合并/建议换题/未订购)—— 运营的工作队列
       · capacity_notice / capacity_semantics —— 额度口径,客户还没下单
       · next_step —— 原文是写给服务商的下一步动作("先补齐可信信息，再确认一个
         可以进入的发布落点"),里面还带着「可以进入」这类 access 话术。
         客户版换成字典里的固定售前话术,不做字符串裁剪(裁剪会随文案变化悄悄失效)。
    """
    return {
        "headline": raw.get("headline") or "",
        "next_step": translate_status("customer_plan_preview_next_step")["explanation"],
        "customer_present": bool(raw.get("customer_present")),
        "total_candidates": int(raw.get("total_candidates") or 0),
    }


def _present_items_for_agent(items: list[dict[str, Any]], *,
                             capacity: Capacity) -> list[dict[str, Any]]:
    """服务商执行面的任务卡。**原行为一字未改**(改造前的那一段整段搬过来)。"""
    out = []
    for item in items:
        domain = item.get("target_domain") or ""
        title = f"第 {item['ordinal']} 篇 · {item.get('content_form') or '内容'}"
        if domain:
            title += f" → 发到{domain}"
        out.append({
            "plan_item_id": item["plan_item_id"],
            "ordinal": item["ordinal"],
            "title": title,
            "target_question": item["target_question"],
            "content_form": item.get("content_form") or "",
            "target_domain": domain,
            "status": translate_status(item["allocation_code"]),
            "gap": translate_status(item["gap_code"]) if item.get("gap_code") else None,
            "access": translate_status(item["access_code"]) if item.get("access_code") else None,
            "duplicate_of_item_id": item.get("duplicate_of_item_id"),
            "rationale": item.get("rationale_jsonb") or {},
            "actions": _actions_for_item(
                allocation_code=item["allocation_code"], capacity=capacity
            ),
        })
    return out


def _present_items_for_customer(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """客户售前版的任务卡:只留「这一篇为什么值得写、写什么」。

    🔴 `_actions_for_item` **一次都不调**。它是全模块唯一签发写动作的出口;
       "调完再把 actions 清空"是后置擦除,哪天有人在中间加一行就漏了。
       不调 = 客户侧与那个出口之间没有代码路径。

    砍掉的四样,每样都有具体理由(不是"客户看不懂所以少给点"):
      · `status`(allocation 态)—— **这是本次锁真的抓到的泄漏**:
        `hold_until_domain_access_confirmed` 是 allocation 码,但它的人话是
        「等渠道确认 / 确认发布渠道后再写,避免文章写好却没有合适落点」;
        `switch_to_adjacent_query` 更直白:「主要被我们进不去的网站占据」。
        只屏蔽 `access` 字段挡不住它们 —— access 话术会从 status 那一栏漏出去。
        而"可开工/等渠道/已合并/建议换题/没有额度"本来就是执行队列状态,
        客户还没下单,这一栏对他没有任何意义。
      · `access` / `target_domain` —— 落点与可进入性判定,同上;
        target_domain 就是 access 判定的对象,留着等于换个字段说同一件事。
      · `rejected_duplicate_coverage` 那些**整条不出现** —— 它们是"已被另一篇覆盖,
        不重复生产"。把一条明确不会单独产出的条目列进客户看的交付清单,
        是把交付量说大了。
      · `duplicate_of_item_id` —— 上面那条都不出现了,指向它的引用只会变成悬空编号。

    保留 `gap`:它说的是"客户为什么需要这件事"(还没进推荐名单 / AI 还不认识客户),
    正是售前要讲的那一半。

    🔴 序号**重新连排**:过滤掉合并项后原 ordinal 会出现 1、3、4 的空档,
       客户读到的是"第 2 篇去哪了"。重排的同时标题里的「第 N 篇」跟着重排 ——
       两处必须用同一个数,分开算迟早对不上。
    """
    out = []
    ordinal = 0
    for item in items:
        if item.get("allocation_code") == "rejected_duplicate_coverage":
            continue
        ordinal += 1
        rationale = dict(item.get("rationale_jsonb") or {})
        rationale["how"] = translate_status("access_arranged_by_team")["explanation"]
        out.append({
            "plan_item_id": item["plan_item_id"],
            "ordinal": ordinal,
            "title": f"第 {ordinal} 篇 · {item.get('content_form') or '内容'}",
            "target_question": item["target_question"],
            "content_form": item.get("content_form") or "",
            "gap": translate_status(item["gap_code"]) if item.get("gap_code") else None,
            "rationale": rationale,
            # 🔴 恒空数组而不是省略这个键:省略的话"客户侧没有动作"这件事
            #    在出参里没有留痕,锁只能断言"键不存在",而键不存在也可能是
            #    序列化时被顺手丢了。恒空是一个**可被断言的事实**。
            "actions": [],
        })
    return out


def present_snapshot(bundle: dict[str, Any], *, capacity: Capacity,
                     audience: str = AUDIENCE_AGENT) -> dict[str, Any]:
    """把落库形态装配成给浏览器的形态,并过一遍内部术语泄漏闸。

    `audience` 决定裁剪档位(默认 agent = 原行为一字不改):
      · agent    服务商执行面 —— 出 actions / access / 容量 / 不足额 / 执行闸
      · customer 客户售前预览 —— actions 恒空、access 全屏蔽、容量与不足额不下发

    🔴 fail-closed:受众不在白名单当场炸。不做 "认不出就当 agent" 的兜底 ——
       那个兜底的失败方向是**把执行按钮发给客户**,是本次改造要防的正主。
    """
    if audience not in AUDIENCES:
        raise ValueError(
            f"未知受众 audience={audience!r};允许值 {sorted(AUDIENCES)}。"
            "不做默认兜底 —— 认错受众的代价是把写操作签发给客户。"
        )
    for_customer = audience == AUDIENCE_CUSTOMER

    snapshot = bundle["snapshot"]
    items = bundle["items"]
    payload = snapshot.get("payload_jsonb") or {}

    presented_items = (
        _present_items_for_customer(items) if for_customer
        else _present_items_for_agent(items, capacity=capacity)
    )

    if for_customer:
        # 客户版:不下发 snapshot_id / 代际 / 容量 / 不足额 / 执行闸。
        # 那些是服务商侧的机读与运营口径,客户页一个都用不上,下发即多一份泄漏面。
        out = {
            "audience": AUDIENCE_CUSTOMER,
            "quote_id": snapshot["quote_id"],
            "query_family": {
                "display_query": snapshot.get("display_query") or "",
                "observed_at": snapshot.get("observed_at"),
            },
            "summary": _customer_summary(snapshot.get("summary_jsonb") or {}),
            "items": presented_items,
            "evidence_platforms": payload.get("evidence_platforms") or [],
            "labels_version": payload.get("labels_version") or labels_version(),
            "generated_at": snapshot.get("generated_at"),
        }
        assert_no_internal_leak(out, where="delivery-plan-customer")
        return out

    out = {
        "audience": AUDIENCE_AGENT,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_version": snapshot["snapshot_version"],
        "rule_version": snapshot["rule_version"],
        "authority_generation": snapshot["authority_generation"],
        "quote_id": snapshot["quote_id"],
        "query_family": {
            "display_query": snapshot.get("display_query") or "",
            "observed_at": snapshot.get("observed_at"),
        },
        "summary": snapshot.get("summary_jsonb") or {},
        "capacity": capacity.as_dict(),
        # 🔴 执行闸(阶段 1):报价页按它分档 —— 关着只渲染一句提示,开着才渲染整段面板。
        #    它**不是第二套容量口径**:`open` 直接读 `Capacity.executable` 那一个属性
        #    (全模块唯一的"能不能写"开关),没有任何自己的算术。
        #    前端因此不需要、也不允许自己判容量(前端门禁有一条正则钉死这件事)。
        "execution_gate": _execution_gate(capacity),
        "items": presented_items,
        "evidence_platforms": payload.get("evidence_platforms") or [],
        "shortfall": _present_shortfall(payload.get("shortfall")),
        "labels_version": payload.get("labels_version") or labels_version(),
        "generated_at": snapshot.get("generated_at"),
    }
    assert_no_internal_leak(out, where="delivery-plan")
    return out


def _execution_gate(capacity: Capacity) -> dict[str, Any]:
    """执行面开放与否 + 关着时的那一句人话。

    关着时的文案走字典(`execution_not_open_yet`),不在这里写中文串 ——
    界面文案的 SSOT 是 config/gap_operation_labels.json,服务端代码里再写一份
    就是第二套字典,两边迟早不一致。
    """
    # [WO_225-c1 §8.5] 🔴 与 `_actions_for_item` **共用**这一个谓词。
    #   闸开 ⇔ 真的签发得出写动作,是那条「不许分叉」锁的全部内容。
    is_open = can_emit_write_actions(capacity)
    if is_open:
        return {"open": True, "code": "", "label": "", "hint": ""}
    label = translate_status("execution_not_open_yet")
    return {
        "open": False,
        "code": label["code"],
        "label": label["label"],
        "hint": label["explanation"],
    }
