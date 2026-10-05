"""对外文案 SSOT —— §0.5.5 U-1 / U-2 的版本化 copy registry。

**全站唯一叫法在这里定。** U-1 逐字:「内部枚举裸串上屏 = 验收红」。
所以任何要上屏的枚举,都必须能在本模块查到译文;查不到就**抛**,
不回退成原串 —— 回退就是让内部枚举裸串上屏。

判据(MET-40 / UI-24 / POR-20)从这里**机械导出**分母,不手抄。

🔴 本模块只有字符串,没有逻辑
-----------------------------
把判定逻辑放进 copy registry 会让"改文案"变成"改行为"。
等级怎么算在 :mod:`~services.defensive_geo.presentation.registries`,
这里只回答"这个 key 对人怎么说"。
"""

from __future__ import annotations

from typing import Mapping

COPY_REGISTRY_VERSION = "defensive_geo_copy_registry_v2"


class UntranslatedEnum(KeyError):
    """要上屏的枚举没有译文。**必须**炸,不许回退原串。"""


# ═══════════════════════════════════════════════════════════════════
# U-1 · 四态状态词(全站统一 · 废除品牌列表第二套「稳定/有缺口/未建立/未测」)
# ═══════════════════════════════════════════════════════════════════
LEVEL_LABELS: Mapping[str, str] = {
    "guarded": "已守住",
    "needs_strengthening": "待加强",
    "priority_fix": "优先修复",
    "unknown": "暂无结论",
}

#: §0.5.5 U-1:废弃词表。出现在任何对外面 = 红。
#: 写成数据而不是散在注释里 —— 判据要拿它当分母做全站扫描。
RETIRED_LABELS: tuple[str, ...] = (
    "稳定", "有缺口", "未建立", "未测",          # 品牌列表旧四态
    "主动获客", "主动推荐",                      # 进攻侧旧别名
    "问题空洞", "口径不同", "同口径复测",         # 旧说法
    "陈述支持等级", "采集失败",
    "本次未取得有效回答",
)

# ═══════════════════════════════════════════════════════════════════
# U-1 · 目标模式(防守/进攻/混合)
# ═══════════════════════════════════════════════════════════════════
MODE_LABELS: Mapping[str, str] = {
    "defensive": "先守住品牌",
    "offensive": "主动抢推荐",
    "hybrid": "两条线一起看",
}
MODE_SHORT_LABELS: Mapping[str, str] = {
    "defensive": "品牌防守",
    "offensive": "抢推荐",
    "hybrid": "两条线",
}
MODE_EXPLAINERS: Mapping[str, str] = {
    "defensive": "有人点名问你、查你是否靠谱或拿你和竞品比较时,AI 能否认出并说清你",
    "offensive": "客户没有点名品牌,只问“哪家好”时,AI 是否把你列入候选或推荐",
    # 🔴 §9.2 原文此处写「主动获客」,U-1 已废该别名,按 U-1 改「抢推荐」。
    "hybrid": "同一体检分别检查品牌防守与抢推荐,结果分开统计",
}

# ═══════════════════════════════════════════════════════════════════
# U-1 · outcome 十态补译(§0.5.5 L120 逐字)
# ═══════════════════════════════════════════════════════════════════
# 🔴 [WO_233-c2 · 2026-09-17] `recommended` / `conditionally_recommended` 两档
#    **对客不再叫「推荐」**,改叫「提及」。
#
#    原因不是措辞偏好,是这个判定**证明了会认错**。`entity_review.classify_outcome`
#    在确认提及之后,只看「回答里任意位置有没有正向词」,既不绑定推荐对象、也不识别否定
#    —— 我方实测六例(判据包 tests/geo_outcome_wording_2026_09_17/):
#      · 「甲公司资料不足,无法评价。推荐乙公司…」        → recommended(推荐的是**乙**)
#      · 「甲公司是一家专业从事软件开发的企业。」          → recommended(是**描述**)
#      · 「目前无法推荐甲公司,资料太少。」                → recommended(是**否定**)
#    拿这个函数的结果对客户说「AI 明确推荐了你」,是拿一个会把「无法推荐」读成
#    「推荐」的判定去做正面承诺。
#
#    ⚠️ 本单**不重写语义判定**(那要标注集 + 影子计算,是包二)。
#       这里只把对外的**说法**收回到判定真正测到的东西上:
#       「回答里提到了你,并且出现了正面/条件措辞」—— 这句话即使在上面三个错例里也成立。
#    ⚠️ 也**没有**顺手放宽正向词表或加否定词:同一根轴上放宽会把真阳性一起剔掉
#       (本仓 fixing-a-false-positive-by-sliding-the-same-axis-kills-true-positives)。
#    前端同名文案由 A 同班改(字段 target_outcome,取值集见判据包抬头)。
TARGET_OUTCOME_LABELS: Mapping[str, str] = {
    "recommended": "提及(正面措辞)",
    "conditionally_recommended": "提及(带条件措辞)",
    "candidate_only": "列为候选",
    "mentioned_only": "只是提到",
    "criteria_only": "只讲了怎么选没点名",
    "refused_no_evidence": "因证据不足没正面回答",
    "refused_risk": "因风险提示没推荐",
    "not_mentioned": "没提到",
    "entity_ambiguous": "没认准是哪一家",
    "engine_error": "没能拿到平台回答",
}

#: 🔴 `entityState` 与 `TargetOutcome` 是**两个轴**,不是同一字段的两套名字。
#:    §7.4 L712:misidentified 的 outcome 仍是 not_mentioned,
#:    但报告必须单独展示「AI 认错了谁」。合并两轴会让 MET-26/MET-30 静默变绿。
ENTITY_STATE_LABELS: Mapping[str, str] = {
    "confirmed": "认对了",
    "nonmention": "没提到",
    "misidentified": "AI 认成了别家",      # U-1 L120 逐字
    "ambiguous": "没认准是哪一家",
    "not_evaluated": "本次没测这一项",
}

# ═══════════════════════════════════════════════════════════════════
# U-1 · supportLevel 四档 chip
# ═══════════════════════════════════════════════════════════════════
SUPPORT_LEVEL_LABELS: Mapping[str, str] = {
    "corroborated": "有据可查",
    "inferred": "只有线索",
    "unsupported": "暂无依据",
    "unknown": "暂无法判断",
}

# ═══════════════════════════════════════════════════════════════════
# U-2 · runState / 里程碑 userLabel(服务端下发,前端不得自造)
# ═══════════════════════════════════════════════════════════════════
RUN_STATE_LABELS: Mapping[str, str] = {
    "queued": "排队中",
    "running": "正在体检",
    "settlement_pending": "结果已出,费用结算中(无需操作)",
    "needs_action": "需要你处理一下",
    "quarantined": "结果待平台核实,费用已冻结、不会多扣(无需操作)",
    "pending_reconciliation": "正在向平台核实结果,费用已冻结",
    "activation_pending": "已收款,系统准备开工中",
    "execution_funding_pending": "待补算力,暂未开工",
    "verified_published": "已发布(已核实)",
    "reported_success_unverified": "发布结果核实中",
    "retracted": "已下架(历史保留)",
    "superseded": "已被新方案替代",
}

# ═══════════════════════════════════════════════════════════════════
# U-1 / U-2 · 整句文案(需要逐字一致的那些)
# ═══════════════════════════════════════════════════════════════════
SENTENCES: Mapping[str, str] = {
    # §9.3 进度页单平台失败(U-2 已重写,用新版)
    "engine_error_notice":
        "豆包这次没答上来,不计入本次统计;记录已保留,其他平台的结果不受影响",
    # §15.8 PREVIEW_EXPIRED 用户句(U-2 逐字)
    "preview_expired":
        "刚才那一步已过期,请重新发起;没有扣除任何算力",
    # U-3 客户确认成功页
    "customer_confirmed":
        "已确认。您的服务商将与您完成收款对账后开始服务",
    # §9.5 不可比时(U-1 改写「口径不同」)
    "not_comparable":
        "这次和上次测的问题、平台不一样,结果对照看,不算涨跌",
    # U-1「同口径复测」改写
    "comparable_retest_cta": "按同样的问题和平台再测一次",
    # §9.7 从未诊断
    "never_diagnosed": "还没有测试数据",
    "never_diagnosed_cta": "创建第一次体检",
    # §9.7 全平台失败
    "all_engines_failed":
        "本次没有形成有效结论;可以重试、换平台,或联系我们",
    # U-1 「问题空洞」改写
    "gap_label": "还答不上来的问题",
    # U-1 「陈述支持等级」对外改写
    "support_level_label": "依据核查",
    # 客户首屏(§8.1)
    "customer_headline": "客户问到您时,AI 能不能认对、讲清、给出选择理由?",
    "customer_subhead":
        "这份体检不只看“有没有提到”,还会检查 AI 认的是不是同一家公司、"
        "是否愿意推荐、哪些问题仍答不上来,以及回答依据来自哪里。",
    # U-8 测试数据水印
    "shadow_watermark": "测试数据 · 不可发客户",
    # U-1 demo / 客户面联系人
    "demo_read_only": "演示模式·只能看不能动",
    "contact_provider": "联系您的服务商",
    # D0/D30
    "d0": "首次体检",
    "d30": "30 天后复测",
    # ── 包H · R-4 客户事实带冲突时仍可用(§0.5.2 R-4)────────────────
    # 🔴 冲突**不删事实**,只随行提示。旧做法(冲突就整条消失)会让销售
    #    看到「这条建议没有任何依据」,而真相是"依据在,只是外部有别的说法"。
    "fact_conflict_notice":
        "这条是客户自己确认过的信息;网上还有不一样的说法,发之前建议再跟客户核对一次",
    "fact_basis_customer_confirmed": "客户确认过的信息",
    "fact_basis_public_corroborated": "有公开来源可查",
    # ── 包H · U-5 交付待办清单 ─────────────────────────────────────
    "delivery_todo_title": "这一单还要确认什么",
    "delivery_todo_money_note": "每一项各自确认、各自计费,不会一次性全扣",
    # ── 包H · U-9 发给客户 ─────────────────────────────────────────
    "customer_links_title": "发给客户",
    "customer_links_hint": "复制过去直接发微信;链接过期或收回了,这里可以重新签发一个新的",
    # ── 包H · Z-3.1 AI 联网补齐 ────────────────────────────────────
    "ai_autofill_title": "让 AI 联网帮你查",
    "ai_autofill_pending_notice": "这些是 AI 查到的,还没有生效;你确认之后才会写进这家公司的资料",
}

# ═══════════════════════════════════════════════════════════════════
# 包H · U-1 · RequestedFactKey 八项补译(§15.6)
# ═══════════════════════════════════════════════════════════════════
# 🔴 这八个 key 是 ``fact_collection`` / ``identity_calibration`` 两类
#    PriorityAction 唯一会上屏的东西。没有译文 = 界面上出现
#    ``official_website`` 这种串 = U-1 验收红。
REQUESTED_FACT_LABELS: Mapping[str, str] = {
    "legal_name": "公司全称",
    "brand_alias": "品牌还叫过什么名字",
    "official_website": "官网地址",
    "service_scope": "提供哪些服务",
    "product_scope": "有哪些产品",
    "service_location": "服务哪些地区",
    "public_credential": "可公开的资质",
    "public_contact_channel": "可公开的联系方式",
}

# ═══════════════════════════════════════════════════════════════════
# 包H · U-9 · 四类客户链接的人话名与状态
# ═══════════════════════════════════════════════════════════════════
# U-9 逐字:「自动带人话前缀(【诊断报告】【报价单·请确认】…)」。
# 前缀 = 【 + 这里的名字 + 】,不在别处硬编码第二份。
CUSTOMER_LINK_KIND_LABELS: Mapping[str, str] = {
    "diagnosis_report": "诊断报告",
    # 🔴 带上「请确认」是**刻意**的:客户收到光秃秃一个链接不知道要做什么,
    #    销售就得再补一句微信。前缀替她把这句说了。
    "quote_proposal": "报价单·请确认",
    "monitoring_snapshot": "监测快照",
    "customer_portal": "周月报门户",
}

CUSTOMER_LINK_STATUS_LABELS: Mapping[str, str] = {
    "available": "可以发了",
    "expired": "已过期",
    "revoked": "已收回",
    # 「还没到时候」而不是「不可用」:后者听起来像坏了。
    "not_ready": "还没到时候",
}

# ═══════════════════════════════════════════════════════════════════
# 包H · Z-3.1 · PriorityAction 四出口(第四出口 = AI 联网补齐)
# ═══════════════════════════════════════════════════════════════════
# 规格 Z-3.1 逐字:删掉 ``fact_collection`` 上的
# ``Exclude<..., {availability:'business_available'}>``,**增加第四出口**。
# 四个出口都要有人话 —— 出口本身是枚举,上屏必须翻译。
AVAILABILITY_LABELS: Mapping[str, str] = {
    "business_available": "这一项包含在服务里,可以直接做",
    "requires_quote": "这一项要先出一份报价给客户确认",
    "manual_route": "这一项由人工处理",
    # 🔴 第四出口。它与前三个的差别是:她**当场就能点**,不用等人、不用等报价。
    "ai_autofill_available": "可以让 AI 联网帮你补齐,补完你确认了才生效",
}

#: 全部译文表。判据拿它做全量遍历,免得新增一张表却忘了纳入扫描。
ALL_TABLES: Mapping[str, Mapping[str, str]] = {
    "level": LEVEL_LABELS,
    "mode": MODE_LABELS,
    "mode_short": MODE_SHORT_LABELS,
    "mode_explainer": MODE_EXPLAINERS,
    "target_outcome": TARGET_OUTCOME_LABELS,
    "entity_state": ENTITY_STATE_LABELS,
    "support_level": SUPPORT_LEVEL_LABELS,
    "run_state": RUN_STATE_LABELS,
    "sentence": SENTENCES,
    # 包H 新增两表。**必须挂进 ALL_TABLES** —— 不挂就等于新增了一张
    # 判据扫不到的表,U-10 的 census 分母会漏掉它(「手写分母漏掉的那一项
    # 不会让任何判据变红」)。
    "requested_fact": REQUESTED_FACT_LABELS,
    "availability": AVAILABILITY_LABELS,
    "customer_link_kind": CUSTOMER_LINK_KIND_LABELS,
    "customer_link_status": CUSTOMER_LINK_STATUS_LABELS,
}


def translate(table: str, key: str) -> str:
    """查译文。查不到**抛** :class:`UntranslatedEnum`,绝不回退成原串。"""
    try:
        return ALL_TABLES[table][key]
    except KeyError:
        raise UntranslatedEnum(
            f"{table}.{key} 没有对外译文。内部枚举裸串上屏 = 验收红(§0.5.5 U-1);"
            f"请先在 copy_registry 补译,不要在调用点回退原串。"
        ) from None


def looks_like_internal_enum(text: str) -> bool:
    """判定一段将要上屏的文字**是不是**内部枚举裸串。

    判据打形态不打词表:``snake_case`` 与全 ASCII 小写下划线串是内部名的形态,
    人话文案里不会出现。词表会漏,形态不会
    (「补一条词漏三条」—— 开发原则第 4 条)。
    """
    if not text:
        return False
    stripped = text.strip()
    return bool(
        stripped
        and all(c.isascii() for c in stripped)
        and "_" in stripped
        and stripped.replace("_", "").replace(".", "").isalnum()
        and stripped.lower() == stripped
    )


def registry_census() -> dict[str, int]:
    """机械导出分母(POR-20:registry keys 与 DTO union exact)。"""
    return {name: len(table) for name, table in ALL_TABLES.items()}
