# -*- coding: utf-8 -*-
"""R3 裁定书四条阻上线的专项锁(REVIEW_VERDICT_ARTREC_V3_2026-08-11)。

A1:搜狐/网易/腾讯/凤凰/新浪主域降档 platform_ugc —— 「据搜狐网报道」=
    Owner 亲裁类别②假冒第三方归属(生产 portal_media 档 584 items 几乎全是
    自发布频道)。变异对照:JSON 把任一域名改回 portal_media → 本文件必红。
A2:`no_verified_evidence_available` 文案会被原文喂进 LLM 修复指令,旧文案
    「正文应只保留方法、问题清单和待核验项」=删除指令的 prompt 变体。
    变异对照:恢复旧文案 → 本文件必红。
A4:C1 豁免血缘化的锁 a 夹具被 `〔BF-002〕`(admissible source)在上游短路,
    `if not (pack_supported or brand_supported)` 分支从未进入(Review 插桩
    实证)。本文件夹具**无 BF 编号**,配正向对照自证分支可达。
    变异对照(Z2 两形态):`brand_supported ≡ False` / 恢复「必须有企业声明
    才豁免」 → `test_a4_*_not_flagged` 必红。
"""
from __future__ import annotations

from writing.evidence_pack import attribution_of, render_evidence_pack_for_writer
from writing.evidence_precision_policy import evaluate_evidence_precision
from writing.article_generator_service import _build_evidence_advisory_repair_instruction

# 与 §0 裁决一专项锁同一快照(纯客户事实,customer_provided 血缘)。
SNAPSHOT = {
    "version": "brand-fact-v1",
    "brand_name": "观山电梯",
    "claims": [
        {"claim_id": "BF-001", "field": "after_sales",
         "value": "整机质保 24 个月,深圳本地 2 小时上门",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
        {"claim_id": "BF-002", "field": "delivery_capability",
         "value": "累计交付 32 台观光电梯",
         "provenance": "customer_provided", "verification_status": "customer_asserted"},
    ],
}

# 六域名 + 裁定书点名的真实自发布频道形态(子域经最长后缀命中)。
_SELF_MEDIA_PUBLISHERS = (
    "m.sohu.com",        # 搜狐号(A/B 产物实证「据搜狐网报道」的来源)
    "163.com",           # 网易号 163.com/dy/
    "news.qq.com",       # 企鹅号
    "qq.com",
    "ifeng.com",
    "sina.com.cn",
    "sina.cn",
    "k.sina.cn",         # 新浪看点
    "cj.sina.com.cn",
    "jiaju.sina.cn",     # 新浪家居地方站(*.jiaju.*)
)


# ---------------------------------------------------------------- A1 · 降档
def test_a1_selfmedia_domains_demoted_to_platform_ugc():
    """六域名(含子域形态)全部 platform_ugc:归属句只许「据XX上的公开内容」,
    不含「报道/披露/发布」类媒体动词。"""
    for pub in _SELF_MEDIA_PUBLISHERS:
        a = attribution_of({"publisher": pub, "url": f"https://{pub}/x",
                            "published_at": "2026-07-01"})
        assert a["trust"] == "platform_ugc", f"{pub} 未降档:{a['trust']}"
        assert "上的公开内容" in a["prose"], f"{pub} 归属句不是平台身份:{a['prose']}"
        for verb in ("报道", "披露", "发布"):
            assert verb not in a["prose"], f"{pub} 归属句带媒体动词「{verb}」:{a['prose']}"


def test_a1_render_selfmedia_item_carries_no_report_verb_license():
    """渲染层:搜狐号条目走 platform_ugc 分支 —— 用法行必须带「不写“报道”」
    约束,不许拿到裸「归属句照抄」的媒体待遇。"""
    pack = {"version": "evpack-v1", "items": [{
        "evidence_id": "EV-001", "relationship": "support",
        "verification_status": "verified", "title": "某品牌观光电梯项目落地",
        "url": "https://m.sohu.com/a/123456_789", "publisher": "m.sohu.com",
        "published_at": "2026-07-01", "claim": "项目落地", "scope": "",
        "excerpt": "某品牌观光电梯项目在深圳落地。",
    }]}
    out = render_evidence_pack_for_writer(pack)
    assert "据搜狐网上的公开内容" in out
    assert "不写“报道”" in out, "搜狐号条目未带禁「报道」约束"
    # 不许出现门户档裸照抄(没有平台约束的「归属句照抄:据搜狐网 <日期>」)
    assert "归属句照抄：据搜狐网 2026 年 7 月（" not in out


def test_a1_true_edited_media_still_named():
    """反向:真编辑媒体(官媒 + 澎湃/36氪/界面)保持具名档,不受降档殃及。"""
    expect = {
        "people.com.cn": ("official_media", "据人民网"),
        "xinhuanet.com": ("official_media", "据新华网"),
        "thepaper.cn": ("portal_media", "据澎湃新闻"),
        "36kr.com": ("portal_media", "据36氪"),
        "jiemian.com": ("portal_media", "据界面新闻"),
    }
    for pub, (tier, prose) in expect.items():
        a = attribution_of({"publisher": pub, "url": f"https://{pub}/x"})
        assert a["trust"] == tier, f"{pub} 档位漂移:{a['trust']}"
        assert a["prose"] == prose, f"{pub} 归属句漂移:{a['prose']}"
        assert "上的公开内容" not in a["prose"]


# ------------------------------------------------------- A2 · prompt 侧门
def _zero_evidence_assessment():
    body = (
        "# 观光电梯售后怎么看\n\n## 售后能力\n\n"
        "整机质保 24 个月,深圳本地 2 小时上门。\n"
    )
    return evaluate_evidence_precision(body, {}, SNAPSHOT)


def test_a2_no_verified_evidence_message_is_not_a_deletion_instruction():
    """零外部信源 finding 的 message(会被原文喂进修复指令)不许是删除指令:
    必须保护客户事实(照写),不许出现「只保留」式隐性全删。"""
    assessment = _zero_evidence_assessment()
    nv = [f for f in assessment.warnings if f.code == "no_verified_evidence_available"]
    assert nv, "finding 本身要在(增强建议能力不许删 —— 禁做清单)"
    msg = nv[0].message
    assert "只保留" not in msg, f"删除指令复活:{msg}"
    assert "客户自有事实照写" in msg, f"缺客户事实保护:{msg}"
    assert "公开信源" in msg, f"增强建议能力被删:{msg}"


def test_a2_repair_instruction_end_to_end_carries_no_deletion_order():
    """行为级:真 finding → 真 `_build_evidence_advisory_repair_instruction`,
    产出的 LLM 指令不含「只保留」删除令,且带事实保护 + 增强建议。"""
    assessment = _zero_evidence_assessment()
    warnings = [{"code": f.code, "message": f.message} for f in assessment.warnings]
    instr = _build_evidence_advisory_repair_instruction(
        {"evidence_precision": {"warnings": warnings}})
    assert instr, "修复指令要给得出来(反向:建议能力仍在)"
    assert "只保留" not in instr, f"修复指令携带删除令:{instr}"
    assert "客户自有事实照写" in instr
    assert "公开信源" in instr


# ------------------------------------------------- A4 · C1 真进分支的锁
_BODY_PLAIN = (
    "# 观光电梯售后怎么看\n\n## 售后能力\n\n"
    "整机质保 24 个月,深圳本地 2 小时上门。\n\n"
    "## 交付记录\n\n累计交付 32 台观光电梯。\n"
)
_BODY_ALIEN = (
    "# 观光电梯售后怎么看\n\n## 交付记录\n\n"
    "累计交付 999 台观光电梯,平均故障率 0.01%。\n"
)


def test_a4_pure_customer_fact_without_bf_markers_not_flagged():
    """🔴 [A4] 无 BF 编号、无 admissible source 的纯客户事实 ——
    唯一能走进 `if not (pack_supported or brand_supported)` 分支的形态。
    血缘对齐 brand_fact_snapshot → 不报 claim_missing(裁决一)。
    Z2 两形态(brand_supported≡False / 恢复企业声明前置)都会让本锁转红。"""
    assessment = evaluate_evidence_precision(_BODY_PLAIN, {}, SNAPSHOT)
    codes = {f.code for f in assessment.warnings} | {f.code for f in assessment.hard}
    assert "claim_missing_inline_evidence" not in codes, (
        f"纯客户事实(无 BF 编号)被判缺证据:{codes} —— C1 豁免血缘化被退回"
    )


def test_a4_branch_reachability_positive_control():
    """正向对照(插桩替代):同形态但数字**不在**快照/证据包 → 必报
    claim_missing。证明上一条的绿来自 brand_supported 分支真实执行,
    不是上游短路(Review 插桩发现锁 a 的 `〔BF-002〕` 夹具从未进过该分支)。"""
    assessment = evaluate_evidence_precision(_BODY_ALIEN, {}, SNAPSHOT)
    codes = {f.code for f in assessment.warnings} | {f.code for f in assessment.hard}
    assert "claim_missing_inline_evidence" in codes, (
        "异源数字未被报 —— 目标分支不可达,A4 锁失去判别力"
    )


# ------------------------------------------- 订正8 · C12 名次出路是活路不是死信
def test_c12_rank_exit_forms_are_live_not_dead_letters():
    """[R3 订正8] 行为锁(按 R4 标准,不是文本在场锁):C12 模板教的两种
    名次出路必须真能过发布硬门 —— 死信许可 = 模型守规照写、硬拦白跑一轮。
    同时钉死:无引号「位列第一」照拦(模板明示的死形态;哪天它通了,
    说明 R1 引号豁免被悄悄放宽,同样要红)。"""
    from services.marketing.legal_context import find_absolute_violations

    live_rank_n = "据中国电梯协会 2026 年 3 月发布的榜单，观山电梯位列第三。"
    live_rank1_quoted = "据中国电梯协会 2026 年 3 月报道，『观山电梯位列第一』。"
    dead_rank1_bare = "据中国电梯协会 2026 年 3 月发布的榜单，观山电梯位列第一。"
    assert not find_absolute_violations(live_rank_n), "第 N 名引用式出路被拦 —— 死信"
    assert not find_absolute_violations(live_rank1_quoted), (
        "成对引号+言说标记的第一名引用被拦 —— 模板教的唯一活路死了"
    )
    assert find_absolute_violations(dead_rank1_bare), (
        "无引号『位列第一』被放行 —— R1 引号豁免被放宽"
    )
    # 模板文案与行为同源:教的两形态都写明,死形态也如实警示
    from writing.templates.canonical_family_templates import _FAMILY_INSTRUCTIONS

    spec = _FAMILY_INSTRUCTIONS["multi_brand_comparison"]
    assert "位列第 N" in spec and "成对引号" in spec
    assert "仍会被硬拦" in spec
