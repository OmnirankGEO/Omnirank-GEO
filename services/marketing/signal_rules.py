"""
services/marketing/signal_rules.py — 信号规则库 v1(10 条 · 军师"想办法"来源)

总设计 §3:每日巡逻,信号命中 → 产出建议案件(证据+方案+预算+成品草稿)。
每条规则 = 纯函数 rule_fn(signals: dict) -> list[DraftSpec]。空 list = 无命中(健康)。
信号绑觉醒阶段(施瓦茨)+ 大师包标签(度量学习按标签归因)。

DraftSpec 是"确定性草稿"(不依赖 LLM):touch_copy 已是合规成品(过禁承诺守卫);
LLM(advisor_llm,flag 关时不跑)只在其上润色,数字/结构永远来自规则,不由 LLM 造。

诚实标注:标 [DATA-WIRED] 的规则吃 get_marketing_signals 真数据即产案;
标 [NEEDS-DATA] 的规则依赖尚未接线的 schema(已发布文章 / 服务商库存 / 用户 last_active),
当前返回 [](不假造),接线后即生效 —— 但已在 RULES 注册,凑齐 10 条、admin 可见。
"""
from typing import Optional

# DraftSpec = dict;字段见 patrol._build_case_draft


def _spec(rule_key, fingerprint, awareness, audience, trigger, evidence, impact,
          budget_points, cost_yuan, risk, touch_copy, plan, rollback, packs,
          window=7, owner_scope="platform"):
    return {
        "rule_key": rule_key,
        "fingerprint": str(fingerprint),
        "awareness_stage": awareness,
        "owner_scope": owner_scope,
        "audience": audience,
        "trigger_reason": trigger,
        "evidence": evidence,
        "expected_impact": impact,
        "budget": {"points": budget_points, "cost_estimate_yuan": cost_yuan},
        "risk_level": risk,
        "touch_copy": touch_copy,
        "execution_plan": plan,
        "rollback_plan": rollback,
        "observation_window": window,
        "skill_packs": packs,
    }


# ---------------------------------------------------------------------------
# 1. [DATA-WIRED] pending 订单 >24h 未支付 → 挽单触达
# ---------------------------------------------------------------------------
def rule_pending_recover(signals: dict) -> list[dict]:
    out = []
    for o in (signals.get("pending_orders") or [])[:20]:
        uid = o.get("user_id")
        if uid is None:
            continue
        yuan = o.get("amount_yuan")
        out.append(_spec(
            "pending_recover", uid, "solution_aware",
            {"definition": f"有一笔 ¥{yuan} 充值待支付超 24h 的用户", "count": 1, "segment": "end"},
            f"用户 {o.get('username','')} 有一笔 ¥{yuan} 充值订单挂了 {int((o.get('age_seconds') or 0)/3600)} 小时未支付",
            {"order_id": o.get("order_id"), "amount_yuan": yuan, "age_seconds": o.get("age_seconds"),
             "phone_masked": o.get("phone_masked")},
            "触达后 7 日内相关转化(是否完成该笔充值)",
            0, 0.0, "low",
            {"station": f"您有一笔 ¥{yuan} 的充值还没完成~随时可以继续,算力到账后即可用于诊断 / 写作 / 监测。有疑问点这里联系我们。",
             "wecom": f"提醒:您有一笔 ¥{yuan} 充值待完成,继续支付后算力立即到账。做成才扣,失败自动退。"},
            {"channels": ["station", "wecom"], "type": "reminder", "target_order": o.get("order_id")},
            "触达为一次性提醒,不可撤回;不涉及任何算力发放/余额改动,零回收风险。",
            ["hormozi_value", "schwartz_awareness"],
        ))
    return out


# ---------------------------------------------------------------------------
# 2. [DATA-WIRED] 注册 7 日未做首次诊断 → 激活触达(免费诊断引导)
# ---------------------------------------------------------------------------
def rule_register_no_diagnosis(signals: dict) -> list[dict]:
    out = []
    for u in (signals.get("register_no_diagnosis") or [])[:20]:
        uid = u.get("user_id")
        if uid is None:
            continue
        out.append(_spec(
            "register_no_diagnosis", uid, "problem_unaware",
            {"definition": "注册超 7 天但从未做首次诊断的用户", "count": 1, "segment": "end"},
            f"用户 {u.get('username','')} 注册 {int(u.get('age_days') or 0)} 天仍未做首次 AI 搜索诊断",
            {"user_id": uid, "age_days": u.get("age_days")},
            "触达后 7 日内相关转化(是否完成首次诊断=激活)",
            0, 0.0, "low",
            {"station": "欢迎加入 OmniRank!先花几分钟做一次免费 AI 搜索诊断——看看 AI 在被问到你所在行业时,答案里有没有你。",
             "wecom": "还没体验?一次免费诊断,看看 AI 搜索里有没有你。点开即可开始。"},
            {"channels": ["station"], "type": "activation", "cta": "免费诊断"},
            "触达为一次性提醒,不可撤回;不涉及算力发放,零回收风险。",
            ["schwartz_awareness"],
        ))
    return out


# ---------------------------------------------------------------------------
# 3. [DATA-WIRED·聚合] 体验算力耗尽人群 → 首充推荐(首充双倍卡)
#    注:per-user 活跃度需 last_active 接线;当前按聚合人群产 1 案(最热转化时机)。
# ---------------------------------------------------------------------------
def rule_trial_exhausted(signals: dict) -> list[dict]:
    n = int(signals.get("exhausted_trial") or 0)
    if n <= 0:
        return []
    return [_spec(
        "trial_exhausted_active", "aggregate", "solution_aware",
        {"definition": "体验算力已耗尽的用户群", "count": n, "segment": "end"},
        f"当前约 {n} 位用户体验算力已耗尽——首充双倍是最热的转化时机",
        {"exhausted_trial_cohort": n},
        "触达后 7 日内相关转化(是否发生首充)",
        0, 0.0, "low",
        {"station": "你的体验算力用得差不多了。现在首充可享双倍算力,继续跑诊断 / 写作 / 监测更从容。做成才扣,失败自动退。",
         "wecom": "首充双倍进行中:充多少送多少,算力到账即用。做成才扣,失败自动退。"},
        {"channels": ["station"], "type": "recharge_push", "attach": "first_charge_double_card"},
        "触达为一次性提醒,不可撤回;若同时上线首充双倍活动,回滚=活动即时下线 + 未发放部分停发。",
        ["hormozi_value", "jinqiang_headline", "schwartz_awareness"],
    )]


# ---------------------------------------------------------------------------
# 4. [NEEDS-DATA] 跑完诊断+写作但从未发布 → "发布墙"教育触达
#    依赖:articles.first_published_at / 写作产物发布状态(未接线)→ 暂返 []。
# ---------------------------------------------------------------------------
def rule_written_not_published(signals: dict) -> list[dict]:
    return []  # 需接 articles 发布状态数据后生效(已注册,凑齐 10 条)


# ---------------------------------------------------------------------------
# 5. [DATA-WIRED] 某客户首次达标 → 恭喜触达 + 案例卡草稿
# ---------------------------------------------------------------------------
def rule_first_compliant(signals: dict) -> list[dict]:
    out = []
    for e in (signals.get("recent_compliant") or [])[:20]:
        qid = e.get("quote_id")
        if qid is None:
            continue
        out.append(_spec(
            "first_compliant", f"quote:{qid}", "most_aware",
            {"definition": "近期关键词首次达标的客户", "count": int(e.get("compliant") or 0), "segment": "end"},
            f"报价 {qid} 有 {e.get('compliant')} 个关键词达标(平均出现率 {e.get('avg_rate')})",
            {"quote_id": qid, "check_date": str(e.get("check_date")), "compliant": e.get("compliant"),
             "avg_rate": e.get("avg_rate")},
            "触达后 14 日内相关转化(是否续费 / 扩词 / 沉淀案例)",
            0, 0.0, "low",
            {"station": "好消息:你监测的关键词达标了!要不要把这份成绩沉淀成一张案例卡,发朋友圈?",
             "wecom": "恭喜达标!成绩已生成,可一键做成案例卡对外分享。"},
            {"channels": ["station"], "type": "celebrate", "attach": "case_card"},
            "触达为一次性提醒,不可撤回;不涉及算力发放,零回收风险。",
            ["jinqiang_headline", "schwartz_awareness"],
            window=14,
        ))
    return out


# ---------------------------------------------------------------------------
# 6. [NEEDS-DATA] 服务商库存低于近30天消耗均值 → 补货提醒
#    依赖:agent_inventory_wallets 消耗均值(未接线)→ 暂返 []。
# ---------------------------------------------------------------------------
def rule_provider_restock(signals: dict) -> list[dict]:
    return []  # 需接 agent_inventory 消耗数据后生效(已注册)


# ---------------------------------------------------------------------------
# 7. [DATA-WIRED·诊断] 充值转化率偏低 → 运营诊断提案(漏斗对比+假设+建议实验)
#    环比需历史快照;当前用绝对阈值代理(charge_rate_pct < 3%)产诊断案。
# ---------------------------------------------------------------------------
def rule_conversion_low(signals: dict) -> list[dict]:
    funnel = signals.get("funnel") or {}
    rate = funnel.get("charge_rate_pct")
    try:
        rate_f = float(rate) if rate is not None else None
    except (TypeError, ValueError):
        rate_f = None
    if rate_f is None or rate_f >= 3.0:
        return []
    return [_spec(
        "conversion_low", "aggregate", "solution_aware",
        {"definition": "全站充值转化诊断(运营内部)", "count": int(funnel.get("registered") or 0), "segment": "end"},
        f"注册→首充转化率仅 {rate_f}%,低于经验阈值,建议诊断漏斗断点",
        {"funnel": funnel},
        "本案为运营诊断,观察后续 7 日相关转化是否随实验回升",
        0, 0.0, "med",
        {"station": "(运营诊断草案)充值转化偏低——建议排查:注册后引导是否清晰、首充双倍是否露出、发布墙是否解释到位。",
         "wecom": "(运营诊断草案)漏斗断点排查建议:激活引导 / 首充露出 / 发布墙教育。"},
        {"channels": [], "type": "diagnosis", "hypotheses": ["激活引导弱", "首充露出不足", "发布墙突然"]},
        "本案为诊断提案,不触达用户、不发放,零回滚成本。",
        ["hopkins_scientific"],
    )]


# ---------------------------------------------------------------------------
# 8. [DATA-WIRED] 连续无人充值 → 主动提案:限时加赠活动草案
# ---------------------------------------------------------------------------
def rule_no_recharge_streak(signals: dict) -> list[dict]:
    age = signals.get("last_recharge_age_seconds")
    paid_7d = int(signals.get("paid_recharges_7d") or 0)
    try:
        age_h = (float(age) / 3600.0) if age is not None else None
    except (TypeError, ValueError):
        age_h = None
    # 触发:近 7 天 0 笔付费充值,且距上次充值 > 48h
    if paid_7d > 0 or age_h is None or age_h < 48:
        return []
    return [_spec(
        "no_recharge_streak", "aggregate", "solution_aware",
        {"definition": "全站活跃用户(限时加赠候选)", "count": 0, "segment": "end"},
        f"近 7 天 0 笔付费充值,距上次充值已 {int(age_h)} 小时,建议限时加赠拉动",
        {"paid_recharges_7d": paid_7d, "last_recharge_age_hours": int(age_h)},
        "活动上线后观察 7 日内相关转化(充值笔数是否回升)",
        50000, 384.0, "high",
        {"station": "限时加赠:本周充值多送算力,做成才扣、失败自动退。名额有限,先到先得。",
         "wecom": "限时加赠开启:本周充值享额外算力,活动结束即止。"},
        {"channels": ["station"], "type": "campaign_proposal", "campaign": "limited_bonus",
         "params": {"bonus_rate": 0.2, "duration_days": 7}},
        "回滚:活动即时下线(campaign→ended)+ 未发放部分停发;已发放的 bonus 算力不追回(反作弊只 flag 不拦)。",
        ["hormozi_value", "hopkins_scientific"],
    )]


# ---------------------------------------------------------------------------
# 9. [NEEDS-DATA] 高消费用户 30 天沉默 → 唤回触达
#    依赖:per-user 消费分层 + last_active(未接线)→ 暂返 []。
# ---------------------------------------------------------------------------
def rule_high_value_silent(signals: dict) -> list[dict]:
    return []  # 需接 per-user last_active + 消费分层后生效(已注册)


# ---------------------------------------------------------------------------
# 10. [DATA-WIRED·代理] 新功能/低使用率功能 → 卖点文案 + 触达建议
#     "上线7天"需 launch date;当前用"最低使用率功能"代理产卖点案。
# ---------------------------------------------------------------------------
def rule_feature_underused(signals: dict) -> list[dict]:
    out = []
    for f in (signals.get("feature_usage_low") or [])[:3]:  # 只取最低 3 个,避免刷屏
        code = f.get("feature_code")
        if not code:
            continue
        name = f.get("feature_name") or code
        out.append(_spec(
            "feature_underused", f"feature:{code}", "product_aware",
            {"definition": f"未充分使用【{name}】的用户群", "count": int(f.get("users") or 0), "segment": "end"},
            f"功能【{name}】使用次数仅 {f.get('usage')},建议做卖点触达",
            {"feature_code": code, "usage": f.get("usage"), "users": f.get("users")},
            "触达后 7 日内相关转化(该功能使用是否上升)",
            0, 0.0, "low",
            {"station": f"你可能还没试过【{name}】—— 花少量算力就能体验。要不要现在试一下?",
             "wecom": f"【{name}】还没用过?少量算力即可体验,做成才扣、失败自动退。"},
            {"channels": ["station"], "type": "feature_push", "feature": code},
            "触达为一次性提醒,不可撤回;不涉及发放,零回收风险。",
            ["jinqiang_headline", "schwartz_awareness"],
        ))
    return out


# 静态注册表(rule_key, fn)· 10 条 · admin 可见(与 UI '今日机会' 分组映射)
RULES: list[tuple[str, object]] = [
    ("pending_recover", rule_pending_recover),
    ("register_no_diagnosis", rule_register_no_diagnosis),
    ("trial_exhausted_active", rule_trial_exhausted),
    ("written_not_published", rule_written_not_published),   # NEEDS-DATA
    ("first_compliant", rule_first_compliant),
    ("provider_restock", rule_provider_restock),              # NEEDS-DATA
    ("conversion_low", rule_conversion_low),
    ("no_recharge_streak", rule_no_recharge_streak),
    ("high_value_silent", rule_high_value_silent),            # NEEDS-DATA
    ("feature_underused", rule_feature_underused),
]

# 数据接线状态(诚实披露 · admin 设置页展示 / 回包用)
RULE_DATA_STATUS = {
    "pending_recover": "wired",
    "register_no_diagnosis": "wired",
    "trial_exhausted_active": "wired_aggregate",
    "written_not_published": "needs_data",
    "first_compliant": "wired",
    "provider_restock": "needs_data",
    "conversion_low": "wired",
    "no_recharge_streak": "wired",
    "high_value_silent": "needs_data",
    "feature_underused": "wired",
}

# rule_key → 今日机会分组(参考图 01 四卡)
RULE_GROUP = {
    "pending_recover": "convertible",       # 可提升转化
    "conversion_low": "convertible",
    "register_no_diagnosis": "dormant",     # 沉睡促活
    "high_value_silent": "dormant",
    "trial_exhausted_active": "offer",      # 可推优惠
    "no_recharge_streak": "offer",
    "first_compliant": "high_value",        # 高价值客户机会
    "provider_restock": "high_value",
    "feature_underused": "offer",
    "written_not_published": "dormant",
}
