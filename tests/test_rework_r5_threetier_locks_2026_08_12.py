# -*- coding: utf-8 -*-
"""R5 专项锁(WO_ARTREC_REWORK_R5 · Review 撤回 PASS 后返修)。

🔴 §5 夹具三档铁律(本轮根因):清洗类判据的夹具必须成三档 ——
  纯形态(该删)/ 纯事实(该留)/ **混合句(该摘形态留事实)**。
  R4 只测前两档,三个 P0 全部漏网;真实语料里混合句才是主流。
  本文件把裁定书 §1/§2 四条实测句**原样**钉进夹具。

变异对照(runner):
  RC1(关系判别退回关键词匹配 is_self≡True)→ §1 第三方锁必红;
  RC2(免责事实闸摘除)→ §2 混合句锁必红;
  RC3(ags 恢复「必须另找外部来源」准入门槛)→ §3 锁必红;
  RB4(新锚:纯自曝放行)→ 纯形态锁必红。
"""
from __future__ import annotations

from writing.content_cleaner import _blend_visible_source_labels as clean


# ================================================= §1 · 商业关系「谁与谁」判别
def test_r5_s1_third_party_relation_facts_survive():
    """裁定书两条实测句(原样):关系双方都是第三方实体 → 一律保留。
    R4 旧行为:两句整句删成空串(三台交付/2025 验收全没了)。"""
    s1 = "万汇广场是观山电梯的商业合作客户，双方已完成三台观光电梯交付。"
    s2 = "观山电梯受南山区政府委托推广无障碍电梯改造项目，项目于2025年验收。"
    for s, facts in ((s1, ("三台", "交付")), (s2, ("2025", "验收", "无障碍"))):
        out = clean("前句。" + s + "后句。")
        for fact in facts:
            assert fact in out, f"客户的业务事实被删:{fact}(新删除机器)"
        assert "前句" in out and "后句" in out


def test_r5_s1_pure_selfblow_still_cleared():
    """反向(禁做①):三类纯自曝成品仍整句清除。"""
    for s in (
        "本文由观山电梯委托撰写,双方为付费客户关系。",
        "观山电梯是本文的商业合作客户。",
        "本文涉及的客户关系为委托推广。",
    ):
        out = clean("前句。" + s + "后句。")
        assert s not in out, f"纯自曝存活:{s}"
        for leak in ("委托撰写", "付费客户", "商业合作客户", "客户关系为"):
            assert leak not in out
        assert "前句" in out and "后句" in out


def test_r5_s1_mixed_selfblow_strips_relation_keeps_fact():
    """三档铁律第三档:自曝 + 硬事实 → 摘关系从句、事实必须在。"""
    out = clean("本文由观山电梯委托撰写，观山电梯已累计交付三台观光电梯。")
    assert "委托撰写" not in out and "本文" not in out
    assert "三台观光电梯" in out, "混合句事实被连坐删除"


def test_r5_s1_methodology_asset_untouched():
    """反向:方法论公正句一字不改(含「商业关系」词但非声明形态)。"""
    s = "所有品牌采用同一证据门槛,客户品牌不因商业关系获得额外位次。"
    assert s in clean(s)


# ================================================= §2 · 免责事实闸
def test_r5_s2_meta_with_facts_keeps_facts():
    """裁定书两条实测句(原样):「本文」开头 + 含硬事实 → 只摘免责小句。
    R4 旧行为:整句删(2025 租金数据源 / 45 天合同口径全没了)。"""
    out1 = clean("本文测算采用2025年公开租金数据，仅供参考。")
    assert "2025" in out1 and "租金数据" in out1
    assert "仅供参考" not in out1
    out2 = clean("本文列出的45天交付周期来自已签合同，仅供参考，实际以项目排期为准。")
    for fact in ("45天", "合同", "实际以项目排期为准"):
        assert fact in out2, f"混合免责句事实被删:{fact}"
    assert "仅供参考" not in out2


def test_r5_s2_pure_meta_still_deleted():
    """反向:纯元叙述(零事实)仍整句删。"""
    for s in ("本文内容仅供参考。", "以上内容仅供参考,不代表本站观点。"):
        out = clean("前句。" + s + "后句。")
        assert "仅供参考" not in out and "不代表" not in out
        assert "前句" in out and "后句" in out


def test_r5_s2_business_position_unchanged():
    """反向:业务位维持现行正确行为(免责词摘、业务限定留)。"""
    out = clean("以上价格仅供参考，实际以门店测量为准。")
    assert "实际以门店测量为准" in out
    assert "仅供参考" not in out


def test_r5_s5_pure_fact_tier_untouched():
    """三档铁律第二档:纯事实句一字不改。"""
    s = "标配5至10年质保,深圳本土24小时响应。"
    assert s in clean(s)


# ================================================= §3 · prompt 冲突收口
def test_r5_s3_client_material_gate_matches_owner_ruling():
    """ags 客户材料块与 Owner 亲裁统一:自曝禁令保留、准入门槛删除、
    「不因此删掉该事实」在场;与 common_rules 唯一底账句不再互斥。"""
    import inspect
    import writing.article_generator_service as ags
    from writing.templates.common_rules import COMMON_GUARDRAILS

    src = inspect.getsource(ags)
    assert "必须另找具体外部来源方" not in src, "客户事实准入门槛复活(与亲裁反向)"
    assert "找不到就按降级阶梯收短或不写" not in src
    assert "不因此删掉该事实" in src
    assert "不写“据企业资料" in src  # 自曝禁令(正确的前半)保留
    assert "客户提供的资料为唯一底账" in COMMON_GUARDRAILS  # :55 亲裁落地句仍在


# ================================================= §4 · 判读器体验式字段
def test_r5_s4_judge_has_real_experiential_field():
    """A/B 判读器真实存在 experiential_hits 字段(撤回 R4 无依据的「5/6」
    声明后,指标必须由判读器本体产出,不许交付侧另行 grep 造数)。"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "ab_realsource_arm", root / "scripts" / "ab_realsource_arm_2026_08_11.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    v = mod.judge("我们实地探访了观山电梯深圳展厅。", set())
    assert v["experiential_hits"], "体验式检测字段无命中能力"
    v2 = mod.judge("普通句子,没有体验表述。", set())
    assert v2["experiential_hits"] == []
    assert "commercial_selfblow_hits" in v2  # §6-4 新增哨兵字段在
