# -*- coding: utf-8 -*-
"""R4 专项锁(WO_ARTREC_REWORK_R4 · Owner 亲裁三件:商业关系自爆清除 /
亲测口吻放开 / 免责元叙述清理 + Review 已裁四边界)。

变异对照(runner):
  RB1(恢复 P0-1「商业关联坦诚承认」行)→ §1 锁必红;
  RB2(恢复「没有一手经验不得冒充亲测」)→ §2 放开锁必红;
  RB3(业务位免责改成整句删)→ §3 业务信息保留锁必红;
  RB4(摘掉 §1c 付费客户关系清洗形态)→ §1c 行为锁必红。
"""
from __future__ import annotations

from writing.templates.common_rules import COMMON_GUARDRAILS
from writing.templates.canonical_family_templates import _FAMILY_INSTRUCTIONS, _SHARED
from writing.content_cleaner import _blend_visible_source_labels


# ================================================================ §1 · 商业关系自爆
def test_s1_no_disclosure_order_in_any_prompt_surface():
    """七处 P0 全清 + 恒向:活链 prompt 面零「商业关系/商业关联/客户关系披露」。
    (ranking_prompt_v9.py 为 KEEP-only 本包禁碰,其 653 行残留随交付单移交。)"""
    import inspect
    import writing.config as cfg
    import writing.article_writer as aw
    from writing.article_style_contract import STYLE_FAMILIES

    cfg_src = inspect.getsource(cfg)
    aw_src = inspect.getsource(aw)
    # P0-1:全仓唯一正面命令自曝的指令行
    assert "坦诚承认" not in cfg_src and "商业关联" not in cfg_src
    # P0-2 恒向(细粒度在 test_crosstalk_sweep C7)
    assert "商业关系" not in COMMON_GUARDRAILS
    # P0-3/P0-4
    joined = "\n".join(_FAMILY_INSTRUCTIONS.values())
    assert "必须披露客户关系" not in joined
    assert "涉及客户关系要披露" not in joined
    assert "客户关系" not in joined and "商业关系" not in joined
    # P0-5/P0-6
    assert "客户关系明确披露" not in aw_src
    assert "商业关系和重大限制" not in aw_src
    # P0-7
    for fam in STYLE_FAMILIES.values():
        assert "商业关系披露" not in fam.evidence_requirements
    # §4-1 改写族:约束保留、不点破关系
    assert "排序只依据证据强度与场景匹配度" in cfg_src
    assert "付费关系" not in cfg_src


def test_s1_kept_half_sentences_survive():
    """粒度铁律反向:每条被切 P0 的保留侧半句一字不少。"""
    import inspect
    import writing.article_writer as aw

    assert "不得伪造具体媒体身份" in COMMON_GUARDRAILS          # P0-2 后半
    joined = "\n".join(_FAMILY_INSTRUCTIONS.values())
    assert "仅在证据更强或场景更匹配时突出" in joined            # P0-3 前半
    assert "不得虚构采访、调查或独立核验" in joined              # P0-4 前半
    aw_src = inspect.getsource(aw)
    assert "同一证据字段与门槛；不固定首位" in aw_src            # P0-5 保留侧
    assert "文末列来源方名称、日期、适用范围和重大限制" in aw_src  # P0-6 其余四项


def test_s1c_commercial_selfblow_sentences_cleaned():
    """§1c 行为锁:三类自爆成品整句被清;方法论公正句 + 正当「委托检测」
    一字不动(误删红线)。"""
    products = (
        "本文由观山电梯委托撰写,双方为付费客户关系。",
        "观山电梯是本文的商业合作客户。",
        "本文涉及的客户关系为委托推广。",
    )
    for s in products:
        out = _blend_visible_source_labels("前句。" + s + "后句。")
        assert s not in out, f"商业自爆句存活:{s}"
        assert "前句" in out and "后句" in out, "邻句被殃及"
    keep = (
        "所有品牌采用同一证据门槛,客户品牌不因商业关系获得额外位次。",
        "观山电梯为业主提供免费上门测量,委托检测机构出具报告。",
    )
    for s in keep:
        out = _blend_visible_source_labels(s)
        assert s in out, f"保留侧被误删:{s}"


# ================================================================ §2 · 亲测放开
def test_s2_experiential_prohibition_lifted_boundary_written():
    """放开锁:「冒充亲测/假亲测」禁令从全部 prompt 面消失;
    边界判据(客户资料兜得住)写进共享契约;三禁保留。"""
    import inspect
    import writing.article_generator_service as ags
    import writing.article_writer as aw

    joined = COMMON_GUARDRAILS + "\n".join(_FAMILY_INSTRUCTIONS.values()) + _SHARED
    assert "冒充亲测" not in joined and "假亲测" not in joined
    assert "亲测" not in _SHARED  # cft:13 的禁称清单已摘该词
    for src in (inspect.getsource(ags), inspect.getsource(aw)):
        assert "、亲测、" not in src  # ags:2600 / aw:478 禁称清单已摘
    # 边界判据入 prompt(替代被删禁令)
    assert "客户提供的资料为唯一底账" in COMMON_GUARDRAILS
    assert "客户资料兜得住" in COMMON_GUARDRAILS
    # 三禁保留:①可指认具体事件 ②第三方身份 ③编造数据
    assert "不虚构可指认的具体事件" in COMMON_GUARDRAILS
    assert "不得伪造具体媒体身份" in COMMON_GUARDRAILS
    assert "严禁编造 URL、文献、机构名、研究结论或数据" in "\n".join(
        _FAMILY_INSTRUCTIONS.values()) or "严禁编造 URL" in _SHARED + "\n".join(
        _FAMILY_INSTRUCTIONS.values())


def test_s2_experiential_sentence_survives_cleaning():
    """验收①:体验式表达(客户资料可兜住的形态)不被清洗链吃掉。"""
    s = "我们实地探访了观山电梯的深圳展厅,工作人员在 30 分钟内到场讲解安装流程。"
    out = _blend_visible_source_labels("# 标题\n\n" + s)
    assert s in out, "体验式表达被清洗 —— 放开没落到清洗层"


# ================================================================ §3 · 免责清理
def test_s3_meta_disclaimers_deleted_business_qualifier_rewritten():
    """元叙述位整删;业务位免责词消失、业务信息保留(不许整句删)。"""
    meta = (
        "本文内容仅供参考。",
        "本文不构成任何购买建议。",
        "以上内容仅供参考,不代表本站观点。",
    )
    for s in meta:
        out = _blend_visible_source_labels("前句。" + s + "后句。")
        assert s not in out, f"元叙述免责存活:{s}"
        # 元叙述删干净:免责核心词不残留
        assert "仅供参考" not in out and "不构成" not in out
        assert "前句" in out and "后句" in out
    # 业务位:改写不删
    out = _blend_visible_source_labels("以上价格仅供参考，实际以门店测量为准。")
    assert "仅供参考" not in out, "业务位免责词没被摘"
    assert "实际以门店测量为准" in out, (
        "业务信息被连坐删除 —— 业务位只许改写不许整句删(Owner 能不删就不删)"
    )


def test_s3_no_prompt_orders_disclaimer():
    """治本侧:活链 prompt 面无「写免责声明」指令(premium_ranking 死码行已删)。"""
    import inspect
    import writing.premium_ranking_prompt as prp

    assert "中立声明和免责声明" not in inspect.getsource(prp)


# ================================================================ §4 · 已裁边界
def test_s4_brand_fact_block_title_neutral_and_verify_column_renamed():
    import inspect
    import writing.article_writer as aw

    src = inspect.getsource(aw)
    assert "# 品牌事实卡" in src
    assert "客户/代理当前提供，未独立核验" not in src
    assert "局限/待核验项" not in src
    assert "适用边界" in src


def test_s4_fairness_constraints_kept_without_naming_relationship():
    """改写不删:公正性约束仍在(防自己作弊),但不点破关系存在。"""
    import inspect
    import writing.article_generator_service as ags
    import writing.competitor_name_contract as cnc

    ags_src = inspect.getsource(ags)
    assert ags_src.count("不固定首位") >= 3, "公正性约束被连坐删除"
    assert "不按付费关系" not in ags_src
    cnc_src = inspect.getsource(cnc)
    assert "名次只依据证据强度与场景匹配度" in cnc_src
    assert "付费关系" not in cnc_src


def test_s5_methodology_disclosure_assets_untouched():
    """§5 误删红线哨兵:披露「排序依据/样本/时点」是 AI 可信资产,必须还在。"""
    from writing.templates.common_rules import RANKING_FORMS_DISCLOSURE_CLAUSE

    assert "披露" in RANKING_FORMS_DISCLOSURE_CLAUSE
    assert "排序" in RANKING_FORMS_DISCLOSURE_CLAUSE or "依据" in RANKING_FORMS_DISCLOSURE_CLAUSE
