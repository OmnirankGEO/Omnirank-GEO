"""微单 C-5 判别锁(2026-07-28 建 · 2026-08-08 P2 改口径)。

## 本锁的历史与这次为什么改

2026-07-28 建锁时的口径是「**全族**默认不含年份」,依据是飞轮实证(采纳组标题含年份
30.7% vs 曝光组 42.1%,长文差距近 20pt)。

2026-08-08 P2 把它改成「**分文体条件默认**」。两侧证据不冲突而是互补:
飞轮那组数字量的是**全体**标题;被引语料量的是**逐文体**,榜单/推荐族标题含年份率
59.06%(且含年份样本里约 87% 是当年),数据报告 40.92%,而深度长文只有 19.04%
——两侧对长文的判断完全一致。所以年份不是全站开关,是分文体默认。

改口径的同时把散在四层的年份口径收成一份合同(`writing/title_element_contract`),
本锁因此从"断言某句文案在不在"升级为"断言**四层都读同一份合同**"。

## 本文件的锁清单

① 年份规则块在场且是**合同渲染**的(不是手抄的第二份文案);
② 三态口径正确落到六族(榜单 on / 案例数据 conditional / 其余 off);
③ 反向锁:旧的"全族默认不含年份"与旧的硬性"每个标题包含年份"都不得复活;
④ 公式库硬规则④原句零回退(不以年份开头),且选题 prompt 不再声称"覆盖"它;
⑤ 深档榜单规格的年份句由合同渲染(改动前它对榜单文说"默认不含年份",方向相反);
⑥ 标题×文体实证矩阵四条全部在场(2026-07-28 原锁,零回退)。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _title_prompt() -> str:
    from writing.keyword_topic_generator import _build_title_generator_prompt

    return _build_title_generator_prompt("全屋定制")


# ===========================================================================
# ① 年份规则块在场,且来自合同(不是第二份手抄文案)
# ===========================================================================
def test_prompt_year_rule_block_is_rendered_by_contract():
    """选题 prompt 里的年份口径必须**逐字**等于合同渲染的那一段。

    变异(把合同渲染换成手抄一段等价文案)→ 本锁转红:手抄那份改合同不会跟着变,
    正是这次要根治的病(改动前四层各写一份、三种意见)。
    """
    from writing.title_element_contract import (
        TITLE_ELEMENT_CONTRACT_VERSION,
        build_title_year_rule_prompt,
        current_year,
    )

    prompt = _title_prompt()
    rendered = build_title_year_rule_prompt(year=current_year())
    assert rendered in prompt, "选题 prompt 必须内嵌合同渲染的年份规则块"
    assert TITLE_ELEMENT_CONTRACT_VERSION in prompt, "合同版本号必须随规则下发"
    # 要素默认表(P2-2 可解释默认)同样必须是合同渲染的
    from writing.title_element_contract import build_title_element_defaults_prompt

    assert build_title_element_defaults_prompt() in prompt


def test_year_is_dynamic_not_hardcoded():
    """年份必须动态取。变异(把 current_year 换成写死 2026)→ 本锁转红。"""
    from writing.title_element_contract import build_title_year_rule_prompt

    assert "「2999」" in build_title_year_rule_prompt(year=2999)
    src = (ROOT / "writing" / "title_element_contract.py").read_text(encoding="utf-8")
    assert "datetime.now().year" in src
    # 渲染层不许出现任何四位年份字面量(数据表里的 N 与百分比不是年份)
    import re

    rule = build_title_year_rule_prompt(year=2999)
    assert not re.search(r"\b20\d{2}\b", rule), f"渲染结果里有写死年份: {rule}"


# ===========================================================================
# ② 三态口径落到六族
# ===========================================================================
def test_year_default_three_states_match_corpus_evidence():
    """三态由数据 + 两条规则推出,不是手填。

    变异(把 SPEC_LEVEL_RECOMMENDED_MIN 从 40 改成 70,或把榜单卡年份率改到 40 以下)
    → 榜单族掉出 on,本锁转红。
    """
    from writing.title_element_contract import (
        YEAR_DEFAULT_CONDITIONAL,
        YEAR_DEFAULT_OFF,
        YEAR_DEFAULT_ON,
        validate_title_element_contract,
        year_default_for_family,
    )

    assert validate_title_element_contract() == []
    assert year_default_for_family("multi_brand_comparison") == YEAR_DEFAULT_ON
    assert year_default_for_family("case_data_roi") == YEAR_DEFAULT_CONDITIONAL
    for family in ("evidence_qa", "implementation_guide",
                   "trend_policy_risk", "company_facts"):
        assert year_default_for_family(family) == YEAR_DEFAULT_OFF, family
    # 未知族按最保守侧,**不猜**
    assert year_default_for_family("no_such_family") == YEAR_DEFAULT_OFF


def test_prompt_states_each_family_explicitly():
    """六族每一族都必须在年份块里被点名(不许有族落在"没说"里)。"""
    from writing.article_style_contract import STYLE_FAMILIES
    from writing.title_element_contract import build_title_year_rule_prompt

    rule = build_title_year_rule_prompt(year=2026)
    for family in STYLE_FAMILIES.values():
        assert family.name in rule, f"{family.name} 没在年份规则里被点名"
    assert "默认带**当年年份" in rule       # on 态
    assert "条件默认" in rule               # conditional 态
    assert "默认**不写**年份" in rule       # off 态


# ===========================================================================
# ③ 反向锁:两个旧口径都不得复活
# ===========================================================================
def test_old_global_no_year_rule_is_gone():
    """旧的"全族默认不含年份"必须绝迹 —— 它与榜单族 on 直接矛盾。"""
    prompt = _title_prompt()
    assert "标题默认不含年份" not in prompt
    assert "默认口径收紧为不写" not in prompt


def test_prompt_old_mandatory_year_rule_is_gone():
    """更旧的硬性"每个标题包含「{year}」"同样不得复活(2026-07-28 原锁,零回退)。"""
    prompt = _title_prompt()
    assert "每个标题包含「" not in prompt
    assert "年风格匹配的标题" not in prompt


# ===========================================================================
# ④ 公式库硬规则④零回退 + 不再声称"覆盖"
# ===========================================================================
def test_formula_rule_four_intact_and_not_overridden():
    """"不以年份开头"是**同一条**规则,不是两套。

    改动前选题 prompt 写着"本条覆盖下方公式库硬规则④",那是在说两处口径不一致;
    现在合同把年份**位置**规则与公式库对齐,"覆盖"这句话必须消失。
    """
    prompt = _title_prompt()
    assert "不以年份开头" in prompt              # 公式库块仍在
    assert "覆盖下方公式库硬规则④" not in prompt  # 不再互相打架
    assert "不作标题开头" in prompt              # 合同侧同一条
    src = (ROOT / "writing" / "title_formula_library.py").read_text(encoding="utf-8")
    assert "④ 不以年份开头——年份可写，但要放在标题中后部" in src  # 库本体未动


def test_deterministic_fallback_year_follows_contract():
    """[退役 → 搬家] 原锁把「年份跟着合同走」打在**硬编码兜底模板表**上。

    Owner 2026-08-17 裁决后那张表已整体退役 —— 但年份合同本身
    (`title_element_contract`,本包禁区,一字未动)没有任何变化。
    这条锁的语义因此有两个去处:
      · 「兜底模板年份跟合同」→ **退役**(没有兜底模板了);
      · 「年份口径只有一份 SSOT」→ 由本条 + 紧随其后的
        `test_evidence_first_fallback_year_follows_contract` 继续承担。
    """
    from writing.article_style_contract import STYLE_FAMILIES
    from writing.title_element_contract import (
        deterministic_year_default,
        family_code_for_label,
    )
    import writing.keyword_topic_generator as ktg

    # 退役实证:模板表符号不在运行时
    for name in ("_fallback_style_title_map", "_fallback_form_completion_map"):
        assert not hasattr(ktg, name), f"退役模板表又回来了:{name}"

    # 合同本体未动:六族标签仍能接回合同,且每族有确定的年份默认
    defaults = {}
    for family in STYLE_FAMILIES.values():
        code = family_code_for_label(family.name)
        assert code is not None, f"族名接不回合同: {family.name}"
        defaults[code] = deterministic_year_default(code)
    assert len(defaults) == 6, f"六族分母不对:{defaults}"
    # 成对反向:合同确实**区分**族(全 True / 全 False 都说明它没在管事)
    assert len(set(defaults.values())) == 2, f"年份合同对六族无区分力:{defaults}"

def test_evidence_first_fallback_year_follows_contract():
    """evidence-first 硬兜底同样读合同。改动前它是"指南/趋势写年份、榜单不写",
    与语料方向相反 —— 这条锁钉死它不能再自成一套。"""
    import re

    from writing.article_style_contract import STYLE_FAMILIES, evidence_first_title_for_family
    from writing.title_element_contract import deterministic_year_default

    for code in STYLE_FAMILIES:
        title = evidence_first_title_for_family(
            brand_name="深圳栖舍", industry="装修", family_or_style=code,
            keywords=["深圳装修公司"], index=1,
        )
        assert bool(re.search(r"20\d{2}", title)) == deterministic_year_default(code), (
            f"{code} 兜底标题年份与合同不一致: {title}")
        assert not re.match(r"^20\d{2}", title), f"年份不得作标题开头: {title}"


def test_s1_soft_check_does_not_fire_on_correct_year_free_titles():
    """`validate_article_structure` 的 S1「标题含年份」必须按文体合同判。

    改动前 S1 不分文体,一律要求标题含年份 —— 新合同下六族里五族默认不写年份,
    按规矩生成的标题会被 S1 记软失败,即"对正确产物报警"。这是本包必须一起
    收掉的一处口径(Owner 规则:提示要么帮人解决,要么不显示)。

    变异(把 S1 改回不分文体)→ 本锁转红。
    """
    from writing.article_writer import validate_article_structure

    # 无年份正文:年份 off 的族不该记 S1;year on 的榜单族该记。
    body_no_year = "深圳装修公司怎么选？常见问题一次说清\n\n" + "内容。" * 400
    _, _, soft_off = validate_article_structure(body_no_year, style_code="buying_guide")
    assert "S1" not in soft_off, "指南族默认不写年份,不该被 S1 报警"
    _, _, soft_on = validate_article_structure(body_no_year, style_code="comparison_review")
    assert "S1" in soft_on, "榜单族默认带年份,缺年份才应当记 S1"
    # 带年份时榜单族不再记 S1(反向对照:判据不是恒真)
    body_year = "深圳装修公司怎么选？2026年这 3 点最容易踩坑\n\n" + "内容。" * 400
    _, _, soft_on2 = validate_article_structure(body_year, style_code="comparison_review")
    assert "S1" not in soft_on2


# ===========================================================================
# ⑤ 深档规格年份句由合同渲染
# ===========================================================================
def test_deep_spec_year_clause_follows_contract():
    """深档榜单规格只对 multi_brand_comparison 渲染,而该族在合同里是 on。

    变异(把该句写死回"默认不含年份")→ 本锁转红。
    """
    from writing.templates.canonical_family_templates import (
        _ranking_title_year_clause,
        build_deep_ranking_structure_spec,
    )
    from writing.title_element_contract import current_year

    plan = {"target_chars": 16000, "verified_candidate_count": 8}
    spec = build_deep_ranking_structure_spec(
        plan, whitelist=[f"品牌{i}" for i in range(8)],
    )
    assert _ranking_title_year_clause() in spec
    assert "默认带当年年份" in spec
    assert str(current_year()) in spec
    assert "默认不含年份（年度主题除外，且不作开头）" not in spec
    # 位置规则零回退:年份仍不许作开头
    assert "不作开头" in spec


# ===========================================================================
# ⑥ 标题×文体实证矩阵(2026-07-28 原锁,零回退)
# ===========================================================================
def test_prompt_carries_style_title_matrix():
    """分文体规则四条全部在场。变异(删矩阵表)→ 本锁转红。"""
    prompt = _title_prompt()
    assert "标题×文体规则" in prompt
    assert "问句形全族优先" in prompt
    assert "只允许" in prompt and "选购与多品牌比较" in prompt
    assert "禁用榜单词" in prompt
    assert "指南/攻略" in prompt and "字样" in prompt
    assert "地域词仅对比族鼓励" in prompt


def test_matrix_bindings_scoped_to_correct_families():
    """绑定关系钉到正确文体(防"换族"式漂移)。"""
    prompt = _title_prompt()
    matrix_start = prompt.index("标题×文体规则")
    matrix_end = prompt.index("## 六类文体与标题结构")
    matrix = prompt[matrix_start:matrix_end]
    assert "榜单词" in matrix
    assert "案例、数据与 ROI" in matrix
    assert "方法与实施指南" in matrix
    assert "选购与多品牌比较" in matrix


# ===========================================================================
# ⑦ P2-2 可解释默认 + P2-3 血缘保持
# ===========================================================================
def test_title_element_defaults_are_explainable_not_a_checklist():
    """要素表必须带出现率(可解释),并明说不是四件套硬塞。"""
    from writing.title_element_contract import build_title_element_defaults_prompt

    table = build_title_element_defaults_prompt()
    assert "71.11%" in table and "67.84%" in table and "67.96%" in table
    assert "默认必备" in table and "推荐" in table and "可选" in table
    assert "不是勾选清单" in table
    assert "永不作保存或发布硬拦" in table


def test_keyword_lineage_untouched():
    """P2-3 血缘保持:标题仍从购买问题血缘生成,本包一个字都没动血缘链。"""
    # [标题 AI-only 2026-08-17] 原实现拿兜底模板产物验血缘;模板退役后
    # 改验**血缘判据本身**(对齐器)—— 血缘链一个字没动,这条锁的语义不变。
    from writing.title_keyword_alignment import assess_title_keyword_alignment

    for title in (
        "深圳全屋定制怎么挑？先看板材、工期和售后三项",
        "深圳全屋定制报价怎么比？逐项对齐口径再谈价",
        "深圳全屋定制多久能回本？按两种常见情况算",
    ):
        assert assess_title_keyword_alignment(title, "深圳全屋定制").aligned, title
    # 成对反向:换掉购买对象必须判不过(否则对齐器恒真)
    assert not assess_title_keyword_alignment(
        "广州全铝家居怎么挑？先看三项", "深圳全屋定制").aligned


# ===========================================================================
# ⑧ 实验预登记(工单补章 R1:终局由 A/B 数据裁)
# ===========================================================================
def test_experiment_preregistration_is_wired_to_registry():
    """预登记声明的单变量维必须真的被实验登记表接受,否则登记跑不起来。"""
    from services.article_experiment_registry import ALLOWED_DIMENSIONS
    from writing.title_element_contract import (
        YEAR_DEFAULT_ON,
        title_year_experiment_preregistration,
        year_default_for_family,
    )

    prereg = title_year_experiment_preregistration()
    assert prereg["single_change_dimension"] in ALLOWED_DIMENSIONS
    # 且必须是**导入**过去的,不是两边各敲一遍同名字符串(命名巧合会随值改动而漂移)
    registry_src = (ROOT / "services" / "article_experiment_registry.py").read_text(
        encoding="utf-8")
    assert (
        "from writing.title_element_contract import TITLE_YEAR_EXPERIMENT_DIMENSION"
        in registry_src
    )
    # 且集合里放的是那个**名字**,不是等值的字面量
    assert "    TITLE_YEAR_EXPERIMENT_DIMENSION," in registry_src
    assert '"title_elements"' not in registry_src
    # 实验族必须就是本次被改默认的那一族,否则测的不是这次改动
    assert year_default_for_family(str(prereg["style_family"])) == YEAR_DEFAULT_ON
    assert int(prereg["min_arm_articles"]) >= 30
    assert tuple(prereg["review_checkpoints_days"]) == (7, 14, 30)
    # 效果申报边界必须随声明一起存在(coded 阶段不许申报效果)
    assert "不许" in str(prereg["effect_claim_boundary"])
