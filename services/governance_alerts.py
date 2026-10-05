"""Domain §13 alert builders — the reusable exits for the rollout (WP8).

Each function converts a specific user-facing failure that today ships as a bare
``str(e)`` / ``{"status":"error","error": ...}`` / partial ``{code,message}``
(accident #8 — 红码无下一步) into one §13-compliant machine contract carrying an
``impact`` field and at least one legal next-step action. They wrap the single
builder ``services.governance_contract.build_alert`` so the contract shape and
the dead-end guard live in exactly one place.

Design rules honoured here:
- **Not a new hard block.** Every builder gives an *exit* to an already-failing
  path; it never introduces a fail-closed the flow lacked (manual §1.6 first
  interpretation principle; §3.6 A1/O1).
- **No supplier / stack leakage.** Raw provider names and exception class names
  stay in logs; user-facing ``reason``/``impact``/``repair_hint`` are clean
  business language (feedback_no_supplier_names_to_users).
- Extra transport/numeric fields (``required``/``available``/…) are merged onto
  the contract by the caller-facing helpers so existing frontends keep reading
  them.
"""
from __future__ import annotations

from typing import Optional

from services.governance_contract import build_alert, build_alert_safe


# ----------------------------------------------------------------------------
# Diagnosis
# ----------------------------------------------------------------------------
def diagnosis_failed_alert(detail_message: str) -> dict:
    """诊断执行抛异常 → 本次未出报告。技术失败但可重试 → 给重试/日志/人工出口。"""
    return build_alert(
        "DIAGNOSIS_RUN_FAILED",
        "本次诊断没有完成，暂时没有生成报告。",
        reason="诊断过程中断，可能是外部数据源暂时不可用或该品牌资料尚不足以完成本轮分析。",
        impact="本次诊断未产出报告；若为付费诊断，冻结的积分已按失败路径退回，未扣费。",
        repair_hint="可稍后重试本次诊断；若多次失败请联系有权限的同事查看诊断日志。",
        actions=[
            {"id": "retry_diagnosis", "label": "重试诊断", "type": "retry"},
            {"id": "view_diagnosis_log", "label": "查看诊断日志", "type": "nav"},
            {"id": "contact_support", "label": "联系客服/管理员", "type": "contact"},
        ],
        rule_version="diagnosis-run-v1",
    )


# ----------------------------------------------------------------------------
# Brand-field autofill (all AI providers failed)
# ----------------------------------------------------------------------------
def autofill_all_providers_failed_alert(last_error: Optional[str] = None) -> dict:
    """自动补全品牌资料时所有 AI 服务均失败(O1:可选 provider 不可用)。

    ``last_error`` 仅供调用方记日志用，绝不进入用户可见文案(禁供应商名/异常类名)。
    """
    return build_alert(
        "AUTOFILL_ALL_PROVIDERS_FAILED",
        "AI 服务暂时不可用，这次没能自动补全资料。",
        reason="AI 联网补全服务当前暂时不可用，本次没有取到可用结果。",
        impact="本次未自动补全品牌资料；本次操作的积分已退回，未扣费。",
        repair_hint="可以稍后再试一次，或先自己手动填写这些资料继续后续流程。",
        actions=[
            {"id": "retry_autofill", "label": "稍后重试自动补全", "type": "retry"},
            {"id": "fill_manually", "label": "自己手动填写", "type": "nav"},
            {"id": "cancel", "label": "暂不补全", "type": "dismiss"},
        ],
        rule_version="autofill-brand-fields-v1",
    )


# ----------------------------------------------------------------------------
# Monitoring
# ----------------------------------------------------------------------------
def monitoring_run_failed_alert(detail_message: str) -> dict:
    """监测执行返回 status=error / 抛异常 → 本次监测未完成，可重试。"""
    return build_alert(
        "MONITORING_RUN_FAILED",
        "本次监测没有完成。",
        reason="监测过程中断，可能是监测引擎暂时不可用或网络波动。",
        impact="本次监测未产出结果；若为付费监测，冻结的积分已按失败路径退回，未扣费。",
        repair_hint="可稍后重试本次监测；若持续失败请联系有权限的同事查看日志。",
        actions=[
            {"id": "retry_monitoring", "label": "重试监测", "type": "retry"},
            {"id": "view_monitoring_log", "label": "查看监测日志", "type": "nav"},
            {"id": "contact_support", "label": "联系客服/管理员", "type": "contact"},
        ],
        rule_version="monitoring-run-v1",
    )


def monitoring_stream_failed_alert(detail_message: str) -> dict:
    """监测流式接口建立失败 → 与 run_failed 同族，语义标注为 stream 便于定位。"""
    return build_alert(
        "MONITORING_STREAM_FAILED",
        "监测实时通道暂时打不开。",
        reason="监测的实时进度通道建立失败，通常是短暂的网络或服务波动。",
        impact="本次未开始监测，也未扣费。",
        repair_hint="可稍后重试；若持续失败请联系有权限的同事查看日志。",
        actions=[
            {"id": "retry_monitoring", "label": "重试监测", "type": "retry"},
            {"id": "contact_support", "label": "联系客服/管理员", "type": "contact"},
        ],
        rule_version="monitoring-run-v1",
    )


def monitoring_no_monitorable_keywords_alert() -> dict:
    """没有可监测的词条 → 引导去添加词条/配置监测。"""
    return build_alert(
        # 小写稳定码与同域既有码(requested_platform_not_entitled 等)保持一致。
        "no_monitorable_keywords",
        "还没有可以监测的关键词。",
        reason="当前品牌下没有已配置、可执行监测的关键词。",
        impact="本次监测没有可执行的对象，未开始，也未扣费。",
        repair_hint="先添加要监测的关键词，再发起监测。",
        actions=[
            {"id": "add_monitoring_keyword", "label": "添加监测关键词", "type": "nav"},
            {"id": "open_monitoring_config", "label": "去监测配置", "type": "nav"},
        ],
        rule_version="monitoring-eligibility-v1",
    )


def monitoring_platform_not_entitled_alert() -> dict:
    """请求包含未购买/不可用的监测引擎 → 调整所选引擎/查看已购权益。"""
    return build_alert(
        # 保持既有稳定码(server.py:9409 / monitoring_api:1819 / batch_monitor:763)。
        "requested_platform_not_entitled",
        "所选的部分监测引擎当前不可用或未开通。",
        reason="本次请求里包含了这些词条尚未购买或暂不可用的监测引擎。",
        impact="为避免误扣费，本次监测未开始。",
        repair_hint="改为只用已开通的监测引擎，或先开通所需引擎后再试。",
        actions=[
            {"id": "adjust_platforms", "label": "调整所选监测引擎", "type": "nav"},
            {"id": "view_entitlement", "label": "查看已购监测权益", "type": "nav"},
            {"id": "contact_support", "label": "联系客服开通", "type": "contact"},
        ],
        rule_version="monitoring-eligibility-v1",
    )


def monitoring_no_eligible_platforms_alert() -> dict:
    """所选词条没有可执行的已购监测引擎 → 开通/联系客服。"""
    return build_alert(
        # 保持既有稳定码(server.py:9418 / monitoring_api:1827 / batch_monitor:772)。
        "no_eligible_monitoring_platforms",
        "所选词条还没有可执行的已开通监测引擎。",
        reason="这些词条当前没有任何已开通、可执行的监测引擎。",
        impact="本次监测没有可执行的引擎，未开始，也未扣费。",
        repair_hint="先为这些词条开通监测引擎，或联系客服协助开通。",
        actions=[
            {"id": "view_entitlement", "label": "查看/开通监测引擎", "type": "nav"},
            {"id": "contact_support", "label": "联系客服开通", "type": "contact"},
        ],
        rule_version="monitoring-eligibility-v1",
    )


def monitoring_insufficient_points_alert(
    required=None, available=None
) -> dict:
    """监测冻结积分时余额不足(402) → 去充值/减少词条。保留 required/available。"""
    alert = build_alert(
        "INSUFFICIENT_POINTS",
        "积分不足，本次监测还没开始。",
        reason="账户可用积分不足以支付本次监测。",
        impact="本次监测未开始，未扣费。",
        repair_hint="充值后重试，或减少本次监测的词条数量。",
        actions=[
            {"id": "recharge", "label": "去充值", "type": "nav"},
            {"id": "reduce_keywords", "label": "减少监测词条", "type": "nav"},
        ],
        rule_version="monitoring-billing-v1",
    )
    alert["required"] = required
    alert["available"] = available
    return alert


def monitoring_billing_error_alert(detail_message: str) -> dict:
    """监测计费环节异常 → 可重试/联系客服。detail_message 只进日志不进文案。"""
    return build_alert(
        "MONITORING_BILLING_ERROR",
        "计费环节出现异常，本次监测未开始。",
        reason="预扣积分时出现异常，为保护你的积分，本次监测未开始。",
        impact="本次监测未开始，未扣费。",
        repair_hint="可稍后重试；若持续出现请联系客服核对账户。",
        actions=[
            {"id": "retry_monitoring", "label": "重试监测", "type": "retry"},
            {"id": "contact_support", "label": "联系客服", "type": "contact"},
        ],
        rule_version="monitoring-billing-v1",
    )


# ----------------------------------------------------------------------------
# Publish (media proxy)
# ----------------------------------------------------------------------------
def publish_insufficient_paid_points_alert(required=None, available=None) -> dict:
    """代发余额不足(402·只可用充值积分) → 去充值/减少条目。保留 required/available_paid。"""
    alert = build_alert(
        "INSUFFICIENT_PAID_POINTS",
        "充值积分不足，代发需要使用充值积分。",
        reason="媒体代发只能使用充值积分，当前充值积分不足以支付本次代发。",
        impact="本次代发未提交，未扣费。",
        repair_hint="充值后重试，或减少本次代发的文章/媒体数量。",
        actions=[
            {"id": "recharge", "label": "去充值", "type": "nav"},
            {"id": "reduce_items", "label": "减少代发条目", "type": "nav"},
            {"id": "cancel", "label": "暂不代发", "type": "dismiss"},
        ],
        rule_version="publish-proxy-billing-v1",
    )
    alert["required"] = required
    alert["available_paid"] = available
    return alert


# ----------------------------------------------------------------------------
# Content generation streams (write / topics / corpus)
# ----------------------------------------------------------------------------
_CONTENT_STREAM_META = {
    "write": ("这次写稿没有完成。", "本次写稿未产出成品。", "content-write-v1"),
    "topics": ("这次没能生成选题。", "本次没有产出选题列表。", "content-topics-v1"),
    "corpus": ("这次资料处理没有完成。", "本次资料没有完成处理/打分。", "content-corpus-v1"),
}


def content_stream_failed_alert(kind: str, detail_message: str = "") -> dict:
    """内容生成流(写稿/选题/资料)中断 → 可重试。

    ``kind`` ∈ {write, topics, corpus}。``detail_message``(含异常类/栈)只进日志,
    不进用户可见文案。不硬性声称退款——各调用点的退款/释放由其自身传输键表达。
    """
    message, impact, rule_version = _CONTENT_STREAM_META.get(
        kind, _CONTENT_STREAM_META["write"]
    )
    return build_alert(
        "CONTENT_GENERATION_FAILED",
        message,
        reason="生成过程中断,可能是外部数据源暂时不可用或网络波动。",
        impact=impact,
        repair_hint="可稍后重试;若持续失败请联系客服。",
        actions=[
            {"id": "retry_generation", "label": "重试", "type": "retry"},
            {"id": "contact_support", "label": "联系客服", "type": "contact"},
        ],
        rule_version=rule_version,
    )


# ----------------------------------------------------------------------------
# Advisor (expert) chat
# ----------------------------------------------------------------------------
def advisor_chat_failed_alert(detail_message: str = "") -> dict:
    """专家对话执行异常 → 可重试。detail_message 只进日志不进文案。"""
    return build_alert(
        "ADVISOR_CHAT_FAILED",
        "这位专家这次没能回复。",
        reason="专家对话在生成回复时中断,通常是短暂的服务或网络波动。",
        impact="本次没有得到专家回复。",
        repair_hint="可重试;若持续失败请联系客服。",
        actions=[
            {"id": "retry_chat", "label": "重试", "type": "retry"},
            {"id": "contact_support", "label": "联系客服", "type": "contact"},
        ],
        rule_version="advisor-chat-v1",
    )


def advisor_not_found_alert(conversation_id=None) -> dict:
    """请求的顾问不存在/暂不可用(O1) → 换一位专家/稍后重试。避免"检查顾问ID"死入口。"""
    alert = build_alert(
        "ADVISOR_NOT_AVAILABLE",
        "这位专家暂时用不了了。",
        reason="所选专家当前不可用,可能已下线或正在维护。",
        impact="本次无法与这位专家对话。",
        repair_hint="可以换一位专家,或稍后再试。",
        actions=[
            {"id": "pick_another_advisor", "label": "换一位专家", "type": "nav"},
            {"id": "retry_chat", "label": "稍后重试", "type": "retry"},
        ],
        rule_version="advisor-availability-v1",
    )
    if conversation_id is not None:
        alert["conversation_id"] = conversation_id
    return alert


# ----------------------------------------------------------------------------
# Team workbench (diagnose / weekly report)
# ----------------------------------------------------------------------------
_TEAM_OP_META = {
    "diagnose": ("本次团队诊断没有完成。", "本次没有产出团队诊断结果。"),
    "weekly_report": ("本次团队周报没有生成成功。", "本次没有产出团队周报。"),
}


def team_operation_failed_alert(kind: str, detail_message: str = "") -> dict:
    """团队工作台 AI 操作(诊断/周报)中断 → 可重试。detail_message 只进日志。"""
    message, impact = _TEAM_OP_META.get(kind, _TEAM_OP_META["diagnose"])
    return build_alert(
        "TEAM_OPERATION_FAILED",
        message,
        reason="AI 处理过程中断,通常是短暂的服务或网络波动。",
        impact=impact,
        repair_hint="可稍后重试;若持续失败请联系客服。",
        actions=[
            {"id": "retry_team_operation", "label": "重试", "type": "retry"},
            {"id": "contact_support", "label": "联系客服", "type": "contact"},
        ],
        rule_version="team-operation-v1",
    )


# ----------------------------------------------------------------------------
# [WP9-P0-7 ② · D8] 文章 findings:定位标注 + 一键 AI 修复该处 / 忽略
# ----------------------------------------------------------------------------
def article_finding_alert(finding: dict, *, article_id=None) -> dict:
    """单条文章 finding 的 §13 合同:定位 + "AI 修复此处" + "忽略"(放行权归用户)。

    D8:文章层零阻断——这不是阻断,是给用户的可选出口。草稿照存;仅对外发布时,
    未修复的违法绝对化会在发布边界被拦(那里也给同样的一键修复)。
    """
    code = str((finding or {}).get("code") or "ARTICLE_FINDING")
    message = str((finding or {}).get("message") or "这里有一处需要你确认的表述。")
    matched = str((finding or {}).get("matched_text") or "")
    severity = str((finding or {}).get("severity") or "soft")
    is_legal = severity == "hard"
    alert = build_alert(
        code.upper(),
        message,
        reason=(
            "这段文字命中了《广告法》绝对化用语,对外发布前需要改成有依据的相对表述。"
            if is_legal else
            "这段文字有一处可以更稳妥的表述,不影响保存。"
        ),
        impact=(
            "草稿已正常保存;若不修改,仅在**对外发布**时会被拦下。"
            if is_legal else
            "草稿已正常保存;是否修改由你决定。"
        ),
        repair_hint="可以让 AI 只修这一处(不重写整篇、不额外计费),也可以忽略。",
        actions=[
            {"id": "ai_fix_this_span", "label": "AI 修复此处", "type": "retry"},
            {"id": "ignore_finding", "label": "忽略", "type": "dismiss"},
        ],
        rule_version="article-finding-v1",
    )
    alert["finding_code"] = code
    alert["severity"] = severity
    if matched:
        alert["matched_text"] = matched
    if article_id is not None:
        alert["article_id"] = article_id
    return alert


def span_repair_failed_alert(reason: str) -> dict:
    """段级修复没成功 → 诚实告知 + 给重试/手改/忽略出口(绝不谎报已修好)。"""
    human = {
        "span_not_located": "没能在正文里定位到这处表述(可能已被改动)。",
        "repair_llm_failed": "AI 修复服务暂时不可用。",
        "repair_empty": "AI 这次没有给出可用的修改。",
        "still_violating": "AI 改了一版,但仍然带绝对化表述,已保留你的原文。",
        # [span 级 AI 免费修复 2026-07-30 · §4] 机械校验四项的诚实回报:
        # 每一条都意味着"改动被拒、正文一字未动",绝不落半成品。
        "repair_span_length_out_of_band": "AI 改出来的长度和原句差太多,已按原文保留。",
        "repair_structure_changed": "AI 顺手动了文章结构(标题/列表/表格),已按原文保留。",
        "repair_touched_outside_span": "AI 改到了这一处以外的正文,已整篇回退。",
        "repair_span_multiline": "AI 把一句话拆成了多行,已按原文保留。",
        "repair_introduced_number": "AI 补了一个证据里查不到的数字,已按原文保留。",
        "repair_dropped_protected_term": "AI 把品牌名/专有名词删掉了,已按原文保留。",
        "repair_violation_text_remains": "AI 改了一版,但违规表述还在,已保留你的原文。",
        "not_bottomline_finding": "这一处不属于可 span 级修复的类型。",
    }.get(str(reason or ""), "这次没能完成修复。")
    alert = build_alert_safe(
        "SPAN_REPAIR_FAILED",
        human,
        reason="段级修复未能产出合规且可用的替换文本。",
        impact="正文一字未改,你的原稿完整保留。",
        repair_hint="可以再试一次,或自己手动改这一处,也可以先忽略。",
        actions=[
            {"id": "ai_fix_this_span", "label": "再试一次", "type": "retry"},
            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
            {"id": "ignore_finding", "label": "先忽略", "type": "dismiss"},
        ],
        rule_version="article-finding-v1",
    )
    # [误报治理 2026-07-30 · T2] 出口三态标记:前端据此决定红色"失败" vs
    # 灰色"需人工" vs "服务暂不可用",不再一律渲染成红色失败。
    alert["outcome"] = "failed"
    return alert


def span_repair_service_unavailable_alert() -> dict:
    """[误报治理 2026-07-30 · T2] 基础设施故障(超时 / 5xx / 连接失败)。

    这是**我们的故障,不是用户的问题** → 免费额度不计次(端点已显式退还),
    文案也不该说"修复失败"让用户以为是自己内容的问题。
    """
    alert = build_alert_safe(
        "SPAN_REPAIR_SERVICE_UNAVAILABLE",
        "AI 修复服务暂时不可用,请稍后再试。",
        reason="调用 AI 服务时超时或连接失败,属平台侧临时故障,与你的内容无关。",
        impact="正文一字未改;**本次不消耗免费修复次数**。",
        repair_hint="稍后再点一次即可;若持续出现请联系客服。",
        actions=[
            {"id": "ai_fix_this_span", "label": "稍后重试", "type": "retry"},
            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
        ],
        rule_version="span-repair-v1",
    )
    alert["outcome"] = "service_unavailable"
    return alert


def publish_needs_manual_review_alert(submitted_count=None) -> dict:
    """订单中已发布到媒介的条目不能自助退款 → 联系客服/查看订单(人工出口)。"""
    count = submitted_count if submitted_count is not None else 0
    alert = build_alert(
        "NEED_MANUAL_REVIEW",
        f"订单中 {count} 个媒体已发布到媒介，需要联系客服处理。",
        reason="已经发布到外部媒介的内容无法自助退款，需要人工核对后处理。",
        impact="这部分已发布条目本次不能自助退款；其余可退条目不受影响。",
        repair_hint="联系客服处理已发布条目；未发布的条目可继续自助退款。",
        actions=[
            {"id": "contact_support", "label": "联系客服处理", "type": "contact"},
            {"id": "view_order", "label": "查看订单明细", "type": "nav"},
        ],
        rule_version="publish-refund-v1",
    )
    alert["submitted_count"] = count
    return alert


def high_risk_no_ai_repair_alert(block_reason: str = "high_risk_article") -> dict:
    """[span 级 AI 免费修复 2026-07-30 · §2.1] 医疗/法律/金融高风险 → 不给 AI 修。

    🔴 出口刻意**只有两个**:走人工签发 / 自己手动改。这一类缺的是**人工签发**,
    不是措辞 —— 让 AI 把结论性判断软化成"可能有帮助"会让判定转绿而风险一点没减,
    还留下"系统认为它合规"的记录。所以这里绝不给"再试一次"。
    """
    scope = (
        "这篇文章属于医疗/法律/金融高风险题材。"
        if block_reason == "high_risk_article" else
        "这一处是医疗/法律/金融的结论性判断。"
    )
    alert = build_alert_safe(
        "HIGH_RISK_NO_AI_REPAIR",
        f"{scope}这类内容不做 AI 自动修复。",
        reason="高风险内容缺的是有资质的人签字,不是把措辞改软 —— AI 改措辞会让审核转绿而风险一点没减。",
        impact="正文一字未改,你的原稿完整保留。",
        repair_hint="请自己按事实改写这一处,或提交人工签发由有权限的同事复核。",
        actions=[
            {"id": "request_human_review", "label": "提交人工签发", "type": "contact"},
            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
        ],
        rule_version="span-repair-v1",
    )
    # [误报治理 2026-07-30 · T2] 同属"需人工",不是失败。
    alert["outcome"] = "needs_human"
    return alert


def cannot_fix_without_fabrication_alert() -> dict:
    """[§7 锁 5] 模型返回 CANNOT_FIX_WITHOUT_FABRICATION → 正文零改动、转人工。

    这不是失败,是**正确行为**:宁可交回人工,也不许补一个新的编造。
    因此出口里刻意没有"再试一次" —— 再抽一次只会逼它编一个新的。
    """
    alert = build_alert_safe(
        "CANNOT_FIX_WITHOUT_FABRICATION",
        "AI 判断这一处不编造新信息就改不好,已原样交回给你。",
        reason="这处主张缺的是真实来源。AI 只会换措辞,补不出来源;硬改就是再编一个。",
        impact="正文一字未改,你的原稿完整保留。",
        repair_hint="补一条真实来源后重新审核,或删掉这处主张,也可以提交人工签发。",
        actions=[
            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
            {"id": "request_human_review", "label": "提交人工签发", "type": "contact"},
        ],
        rule_version="span-repair-v1",
    )
    # [误报治理 2026-07-30 · T2] 这是**按设计的正确行为**,不是失败。
    # 前端据此渲染成中性的"这一处需要人工",而非红色"失败"。
    alert["outcome"] = "needs_human"
    return alert


def span_repair_quota_exhausted_alert(code: str = "", used: int = 0, limit: int = 0) -> dict:
    """[§6 / 锁 7] 同一条 finding 的免费修复额度用尽(首修 + 1 次重试)。

    额度按 **finding 条数**给,不按点击次数;用尽后转人工,**零计费**
    (整篇重写才计费,而且不是硬门的默认出路)。
    """
    alert = build_alert_safe(
        "SPAN_REPAIR_QUOTA_EXHAUSTED",
        f"这一处的免费 AI 修复已用满 {limit} 次,继续自动重试大概率还是修不好。",
        reason="同一处连修两次仍不达标,说明它需要人来判断,不是再抽一次模型。",
        impact="正文一字未改;之前修好的部分都已保存。**本次零扣费**。",
        repair_hint="自己手动改这一处,或提交人工签发;确需整篇重写会另行明示计费。",
        actions=[
            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},
            {"id": "request_human_review", "label": "提交人工签发", "type": "contact"},
        ],
        rule_version="span-repair-v1",
    )
    alert["finding_code"] = str(code or "")
    alert["free_repairs_used"] = int(used)
    alert["free_repairs_limit"] = int(limit)
    return alert
