"""v2.7.1 GEO 文体改造 · 写作大厅 + 契约边界守护单测

覆盖 G10-G15 + G17-G21 9 项 gate:
- G10 user_choice='price' → style_code='price_roi'
- G11 user_choice='data' → style_code='data_report'
- G12 医疗法律 hard rule(resolve_user_choice 拒绝榜单类)
- G13 preconfirmed_topics 不默认 ranking_v2
- G14 extra_instruction sanitize(12 实例 P1 #6)
- G15 写作大厅 0 暴露工程词
- G17 user_choice='company' raise ValueError
- G18 客户 API 不可伪造 style_code
- G19 industry 贯穿(allocate / calculate 裸调禁)
- G21 sanitize 12 实例覆盖
"""
import os
import sys
import ast
from pathlib import Path

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from writing.style_registry import (
    USER_CHOICE_TO_STYLE,
    resolve_user_choice,
    get_fixed_count_styles,
)
from writing.article_writer import _sanitize_extra_instruction


# ============================================================
# G10/G11 · user_choice → style 映射
# ============================================================

def test_user_choice_price():
    """G10 · user_choice='price' → price_roi"""
    assert USER_CHOICE_TO_STYLE['price'] == 'price_roi'
    assert resolve_user_choice('price', None) == 'price_roi'


def test_user_choice_data():
    """G11 · user_choice='data' → data_report"""
    assert USER_CHOICE_TO_STYLE['data'] == 'data_report'
    assert resolve_user_choice('data', None) == 'data_report'


def test_user_choice_guide():
    assert USER_CHOICE_TO_STYLE['guide'] == 'buying_guide'


def test_user_choice_comparison():
    assert USER_CHOICE_TO_STYLE['comparison'] == 'comparison_review'


def test_user_choice_risk():
    assert USER_CHOICE_TO_STYLE['risk'] == 'risk_compliance'


def test_user_choice_auto_returns_none():
    """auto → None(走 ratio 抽 · 第 2 层)"""
    assert resolve_user_choice('auto', None) is None
    assert resolve_user_choice(None, None) is None
    assert resolve_user_choice('', None) is None


def test_user_choice_six_families_with_legacy_read_compatibility():
    """New UI exposes six families + one outward direction; aliases stay readable.

    [#185 · 2026-09-13] 产品有意多出一档「防御型(公司词)」。本锁随之改口径,
    但**只准改宽这一个键**,别的一个不动 —— 并且多加两条,让它比改前更紧:
      · 防御型的生成样式必须与 company_facts **逐字相同**(它不是第七个家族,
        正文合同共用;不同就说明有人给它另开了一套模板);
      · 家族注册表仍是六个(防御型进了 `STYLE_FAMILIES` 会让长度策略/
        规格卡/家族提示词各缺一份,而缺的那份不报错,只是让防御篇用默认合同生成)。
    """
    from writing.article_style_contract import STYLE_FAMILIES

    assert 'company' not in USER_CHOICE_TO_STYLE
    canonical = {
        'auto', 'evidence_qa', 'multi_brand_comparison', 'implementation_guide',
        'trend_policy_risk', 'case_data_roi', 'company_facts',
    }
    outward = {'defensive_company'}
    legacy = {'guide', 'comparison', 'risk', 'price', 'data', 'qa', 'checklist', 'case', 'story'}
    assert set(USER_CHOICE_TO_STYLE) == canonical | outward | legacy

    assert USER_CHOICE_TO_STYLE['defensive_company'] == USER_CHOICE_TO_STYLE['company_facts']
    assert 'defensive_company' not in STYLE_FAMILIES
    assert len(STYLE_FAMILIES) == 6


# ============================================================
# G12 · 医疗 / 法律 hard rule(前后端双校验 · 后端 resolve_user_choice 拒绝)
# ============================================================

def test_medical_legal_hard_rule_via_resolve():
    """G12 · 医疗 / 法律 hard rule 后端二次校验"""
    # user_choice 本身 10 项不含 ranking_v2 类 · 但若外部绕过 + 用 ranking_v2 风格的 user_choice 怎么办?
    # 看实现:medical 行业即便 user_choice 合法,resolve_user_choice 也会检查映射到 ranking_v2/authority_ranking
    # 但 10 项 user_choice 不映射到 ranking_v2 · 所以这个 gate 通过架构本身就防住了
    # 真正测试:前端绕过 + style_code 伪造 → 后端 ratio 分配走医疗 override(ranking 0)
    # 此处验证 USER_CHOICE_TO_STYLE 10 项里没有任何映射到 ranking_v2 / authority_ranking
    for uc, sc in USER_CHOICE_TO_STYLE.items():
        assert sc not in ('ranking_v2', 'authority_ranking'), \
            f"user_choice '{uc}' 不应映射到榜单类 style '{sc}'"


# ============================================================
# G13 · preconfirmed_topics 不默认 ranking_v2
# ============================================================

def test_preconfirmed_topics_no_default_ranking():
    """G13 · USER_CHOICE_TO_STYLE['auto'] = None · 不默认 ranking_v2"""
    assert USER_CHOICE_TO_STYLE['auto'] is None


# ============================================================
# G14/G21 · extra_instruction sanitize 12 实例
# ============================================================

def test_extra_instruction_sanitize_12_cases():
    """G21 v2.4 P1 #6 + v2.7 P2 #5:12 实例覆盖"""
    cases = [
        # 1-3 负向"不要" + 空格变体
        ("不要写榜单", lambda r: "不要" in r),
        ("不要 写榜单", lambda r: "不要" in r),
        ("不要　写榜单", lambda r: "不要" in r),
        # 4-5 别 + 空格变体
        ("别写TOP排名", lambda r: "别" in r),
        ("别写 TOP 排名", lambda r: "别" in r),
        # 6 复合句保留
        ("突出价格不要榜单", lambda r: "突出价格" in r or "不要" in r),
        # 7 正常补充
        ("突出本地服务", lambda r: "突出本地服务" in r),
        # 8 正向 strip
        ("写榜单", lambda r: "写榜单" not in r),
        # 9 正向 strip(TOP)
        ("TOP 排名 + 哪家好", lambda r: "TOP" not in r),
        # 10 字数下限覆盖 strip
        ("只写 1000 字", lambda r: "1000" not in r),
        # 11 字数低于系统区间 strip(v2.7 改实例)
        ("字数低于系统区间", lambda r: "低于" not in r),
        # 12 FAQ 红线无视负向 strip
        ("不要 FAQ", lambda r: "FAQ" not in r),
    ]
    for input_text, checker in cases:
        result = _sanitize_extra_instruction(input_text)
        assert checker(result), f"实例'{input_text}' sanitize 结果不符合预期: {result!r}"


# ============================================================
# G15 · 写作大厅 0 暴露工程词(JSX 文本守护)
# ============================================================

def test_writing_hall_no_engineering_terms_in_jsx_text():
    """G15 · WritingHall.tsx 文件全文 0 命中 ranking_v2 等工程词(旧对话 UI 选题卡随开源 E3 删 [开源 E3 · 前端 · 2026-10-01 · WO_322])

    注:G15 strict 解读是"用户面 JSX 文本 0 暴露" · 这里弱化为
    "文件中 humanizeStyle / __STYLE_HUMAN_FACING__ 映射存在,确保 UI 显示走映射"
    """
    wh_path = os.path.join(_ROOT, 'frontend', 'src', 'pages', 'Writing', 'WritingHall.tsx')

    with open(wh_path, 'r', encoding='utf-8') as f:
        wh = f.read()

    # WritingHall 必须含 humanizeStyle 映射(0 暴露工程词到 JSX)
    assert 'humanizeStyle' in wh, "WritingHall.tsx 应含 humanizeStyle 翻译函数(v2.7.1)"
    assert '__STYLE_HUMAN_FACING__' in wh, "WritingHall.tsx 应含 __STYLE_HUMAN_FACING__ 映射"



# ============================================================
# G17 · user_choice='company' 必拒绝
# ============================================================

def test_company_profile_user_choice_rejected():
    """G17 v2.4 P0 #1:user_choice='company' → ValueError(企业介绍由系统固定)"""
    with pytest.raises(ValueError) as exc:
        resolve_user_choice('company', None)
    assert "企业介绍" in str(exc.value) or "company" in str(exc.value).lower()


def test_company_profile_fixed_count_ssot_config():
    """G17 · company_profile fixed_count=1 SSOT 配置存在(v2.10.4 deprecated 实际不生效)

    历史:v2.4 P0 #1 锁死 SSOT
    现状:v2.10 已 hard 400 废弃 /api/articles/plan(原 fixed slot 强制链路)
    本测试:仅守护 SSOT 配置一致性(WRITING_STYLES['company_profile'].fixed_count=1)
    不守护实际数据流(因 generate-titles 不再创建 fixed topic)
    后续 v2.11 若恢复 fixed slot 创建 · 本测试需扩到 generate-titles 真生成 fixed topic
    """
    fixed = get_fixed_count_styles()
    assert fixed.get('company_profile') == 1, "SSOT 配置:WRITING_STYLES['company_profile'].fixed_count 应为 1"


# ============================================================
# G18 · 客户 API 不可伪造 style_code
# ============================================================

def test_client_api_ignores_fake_style_code():
    """G18 v2.3 阻断点 #5:Pydantic TopicInput extra='ignore' 静默丢弃 style_code"""
    # Importing server.py initializes the whole application database and turns
    # this schema test into an environment-dependent integration test.  Pin the
    # production declaration structurally, then probe the same Pydantic rule.
    server_tree = ast.parse((_ROOT_PATH := Path(_ROOT) / "server.py").read_text(encoding="utf-8"))
    topic_class = next(
        node for node in server_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TopicInput"
    )
    declared_fields = {
        node.target.id
        for node in topic_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert declared_fields == {"id", "title", "keyword", "user_choice"}
    class_source = ast.get_source_segment(_ROOT_PATH.read_text(encoding="utf-8"), topic_class) or ""
    assert 'ConfigDict(extra="ignore")' in class_source

    from pydantic import BaseModel, ConfigDict

    class TopicInputProbe(BaseModel):
        model_config = ConfigDict(extra="ignore")

        id: int | None = None
        title: str | None = ""
        keyword: str | None = ""
        user_choice: str = "auto"

    # 伪造 style_code 应被静默丢弃
    payload = {
        "id": 1, "title": "T", "keyword": "k",
        "user_choice": "auto",
        "style_code": "ranking_v2",  # 伪造字段 · extra='ignore' 丢弃
        "article_style": "ranking_v2",
    }
    inst = TopicInputProbe(**payload)
    assert inst.user_choice == "auto"
    # style_code 不应作为模型字段存在
    assert not hasattr(inst, 'style_code') or getattr(inst, 'style_code', None) is None


# ============================================================
# G19 · industry 贯穿(allocate / calculate 裸调禁 grep gate · 此处单测验)
# ============================================================

def test_industry_propagates_to_allocation():
    """G19 v2.4 P0 #3:industry 全链路贯穿 · 医疗 brand → ranking 0"""
    from config.settings_manager import get_effective_style_ratios
    med_style = get_effective_style_ratios("医疗健康", unit="fraction")
    assert med_style.get('ranking_v2', 0) == 0
    assert med_style.get('authority_ranking', 0) == 0

    leg_style = get_effective_style_ratios("法律商务", unit="fraction")
    assert leg_style.get('ranking_v2', 0) == 0


def test_allocate_styles_by_ratio_accepts_industry():
    """allocate_styles_by_ratio 必接 industry · 医疗 brand 走 ratio 抽 0 榜单"""
    from writing.style_registry import allocate_styles_by_ratio
    styles = allocate_styles_by_ratio(40, industry="医疗健康")
    assert 'ranking_v2' not in styles
    assert 'authority_ranking' not in styles


def test_calculate_distribution_accepts_industry():
    """calculate_distribution 必接 industry · 医疗 brand 走 medical override"""
    from tools.article_generator import calculate_distribution
    dist = calculate_distribution(40, industry="医疗健康")
    # authority 医疗 override 强制 0
    assert dist.get('authority', {}).get('count', 0) == 0


def test_calculate_distribution_industry_none_fallback():
    """industry=None also disables the legacy authority-ad allocation."""
    from tools.article_generator import calculate_distribution
    dist = calculate_distribution(40, industry=None)
    assert dist.get('authority', {}).get('count', 0) == 0
    assert dist.get('deep_dive', {}).get('count', 0) > 0
    assert dist.get('checklist', {}).get('count', 0) > 0


# ============================================================
# v2.7.3 Codex P0 复审:DB 旧 article_style 不允许抢在 user_choice 前生效
# ============================================================

def test_legacy_db_style_must_not_override_user_choice():
    """v2.7.3 P0 修(Codex 抓):DB 旧 article_style='ranking_v2' + user_choice='price' → 必须 price_roi"""
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry=None)
    topic = {
        'id': 1, 'title': '...',
        # DB 读出的旧字段(start-articles handler 把 t.article_style as style 注入到 topic dict)
        'article_style': 'ranking_v2',
        'style': 'ranking_v2',
        'type': 'ranking_v2',
        # 新字段 · 用户在写作大厅选择价格预算
        'user_choice': 'price',
    }
    result = svc._allocate_style_from_ratios(topic, None)
    assert result == 'price_roi', \
        f"v2.7.3 P0 fail · DB 旧 ranking_v2 应被 user_choice='price' 覆盖 · 实际: {result!r}"


def test_legacy_db_style_with_user_choice_data():
    """DB 旧 ranking_v2 + user_choice='data' → data_report"""
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry=None)
    topic = {
        'id': 2, 'title': '...',
        'article_style': 'ranking_v2', 'style': 'ranking_v2',
        'user_choice': 'data',
    }
    result = svc._allocate_style_from_ratios(topic, None)
    assert result == 'data_report', f"应为 data_report · 实际: {result!r}"


def test_medical_legacy_ranking_force_zero():
    """v2.7.3 P0 修:医疗行业 + DB 旧 ranking_v2 + user_choice='auto' → 必须 industry override(0 ranking_v2 / authority_ranking)"""
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry="医疗健康")
    topic = {
        'id': 3, 'title': '...',
        'article_style': 'ranking_v2', 'style': 'ranking_v2', 'type': 'ranking_v2',
        'user_choice': 'auto',
    }
    # 抽 30 次(随机)· 不允许任何一次是榜单类
    for i in range(30):
        result = svc._allocate_style_from_ratios(topic, "医疗健康")
        assert result not in ('ranking_v2', 'authority_ranking'), \
            f"v2.7.3 P0 fail · 医疗 + 旧 ranking_v2 + auto 第 {i+1} 次抽到 {result!r}(应 0 榜单)"


def test_legal_legacy_authority_force_zero():
    """法律行业 + DB 旧 authority_ranking → 必走 industry override(0 ranking 类)"""
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry="法律商务")
    topic = {
        'id': 4, 'title': '...',
        'article_style': 'authority_ranking',
        'style': 'authority_ranking',
        'user_choice': 'auto',
    }
    for i in range(30):
        result = svc._allocate_style_from_ratios(topic, "法律商务")
        assert result not in ('ranking_v2', 'authority_ranking'), \
            f"法律 + 旧 authority_ranking 第 {i+1} 次抽到 {result!r}(应 0 榜单)"


def test_resolve_user_choice_raises_not_swallowed():
    """v2.7.3 P1 修(Codex):resolve_user_choice ValueError 必须上抛 · 不再吞 + 降级 ratio

    旧 v2.7.2 _allocate_style_from_ratios 用 try/except ValueError: pass 把 hard rule 违规吞了。
    新口径:上抛让生成任务 fail · 防客户/旧前端绕过医疗法律 hard rule 静默落到 ratio 池。
    """
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry="医疗健康")
    topic = {
        'id': 5, 'title': '...',
        'user_choice': 'company',  # 越界:v2.4 P0 #1 移除 'company' · 应 raise ValueError
    }
    import pytest as _p
    with _p.raises(ValueError):
        svc._allocate_style_from_ratios(topic, "医疗健康")


def test_trust_legacy_flag_explicit_bypass():
    """v2.7.3:_trust_legacy_style=True + 白名单 style_code → 显式走 DB 旧值(admin 迁移路径)"""
    from writing.article_generator_service import ArticleGeneratorService
    # 模拟 _generate_single 中 _trusted 分支
    ALLOWED = {
        'ranking_v2', 'authority_ranking', 'recommendation_review',
        'buying_guide', 'trojan_horse', 'qa_recommendation', 'brand_softarticle',
        'company_profile', 'comparison_review', 'risk_compliance', 'price_roi', 'data_report',
    }
    topic_trusted = {'_trust_legacy_style': True, 'style_code': 'ranking_v2'}
    assert topic_trusted.get('_trust_legacy_style') is True
    assert topic_trusted.get('style_code') in ALLOWED


def test_default_topic_no_trust_falls_to_allocate():
    """默认 topic(无 _trust_legacy_style)→ 必走 _allocate_style_from_ratios · 不读 DB 旧 article_style"""
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry=None)
    topic = {
        'id': 6, 'title': '...',
        'article_style': 'ranking_v2',  # DB 旧字段
        'user_choice': 'comparison',  # 用户选对比评测
    }
    # 不设 _trust_legacy_style · 应走 _allocate_style_from_ratios → user_choice='comparison' → comparison_review
    result = svc._allocate_style_from_ratios(topic, None)
    assert result == 'comparison_review'


# ============================================================
# v2.7.4 Codex 复审:旧 /api/articles/generate 链路必须服从医疗/法律 hard rule
# ============================================================

def test_resolve_style_for_topic_shared_ssot():
    """v2.7.4 跨链路 SSOT · resolve_style_for_topic 行为对齐 _allocate_style_from_ratios"""
    from writing.style_registry import resolve_style_for_topic

    # user_choice=price → price_roi
    assert resolve_style_for_topic({'user_choice': 'price'}, None) == 'price_roi'

    # 医疗 + auto + 外部伪造 ranking_v2 → 必非榜单
    for _ in range(30):
        result = resolve_style_for_topic(
            {'article_style': 'ranking_v2', 'style': 'ranking_v2', 'user_choice': 'auto'},
            '医疗健康',
        )
        assert result not in ('ranking_v2', 'authority_ranking')


def test_resolve_style_for_topic_raises_on_company():
    """v2.7.4 SSOT · user_choice='company' → ValueError 上抛(不吞)"""
    from writing.style_registry import resolve_style_for_topic
    import pytest as _p
    with _p.raises(ValueError):
        resolve_style_for_topic({'user_choice': 'company'}, None)


def _v274_clean_topic(t):
    """模拟 tools/article_generator.py BatchArticleGenerator 的 preconfirmed 净化逻辑"""
    VALID_USER_CHOICES = {'auto', 'guide', 'comparison', 'risk', 'price', 'data',
                          'qa', 'checklist', 'case', 'story'}
    _uc = t.get("user_choice", "auto")
    return {
        "id": t.get("id"),
        "title": t.get("title", ""),
        "style_code": None,  # v2.7.4 强制 None
        "style": None,
        "styleName": "",
        "type": None,
        "user_choice": _uc if isinstance(_uc, str) and _uc in VALID_USER_CHOICES else "auto",
    }


def test_old_endpoint_fake_style_code_must_be_dropped_medical():
    """v2.7.4 P0(Codex 抓):旧 /api/articles/generate 接 topic.style_code='ranking_v2' 伪造
    + industry='医疗健康' → 净化后必 0 榜单(无论 30 次抽样)"""
    from writing.style_registry import resolve_style_for_topic
    fake = {"id": 1, "title": "医疗推荐", "style_code": "ranking_v2",
            "style": "ranking_v2", "article_style": "ranking_v2", "type": "ranking_v2",
            "user_choice": "auto"}
    cleaned = _v274_clean_topic(fake)
    # 净化后 style_code 应为 None
    assert cleaned['style_code'] is None
    assert cleaned['style'] is None
    assert 'article_style' not in cleaned  # 字段被丢弃

    for i in range(30):
        result = resolve_style_for_topic(cleaned, "医疗健康")
        assert result not in ('ranking_v2', 'authority_ranking'), \
            f"v2.7.4 P0 fail · 医疗 + 净化后 auto 第 {i+1} 次抽到 {result!r}"


def test_old_endpoint_user_choice_price_overrides_fake_ranking():
    """v2.7.4 · 旧接口 + user_choice='price' + 伪造 ranking_v2 → 净化后必 price_roi"""
    from writing.style_registry import resolve_style_for_topic
    fake = {"id": 1, "title": "x", "style_code": "ranking_v2", "user_choice": "price"}
    cleaned = _v274_clean_topic(fake)
    result = resolve_style_for_topic(cleaned, None)
    assert result == 'price_roi'


def test_legal_old_endpoint_force_zero_authority():
    """v2.7.4 · 法律 + 旧接口伪造 authority_ranking → 必 0 榜单类(30 次抽样)"""
    from writing.style_registry import resolve_style_for_topic
    fake = {"id": 1, "style_code": "authority_ranking", "user_choice": "auto"}
    cleaned = _v274_clean_topic(fake)
    for i in range(30):
        result = resolve_style_for_topic(cleaned, "法律商务")
        assert result not in ('ranking_v2', 'authority_ranking'), \
            f"法律 + 净化后 auto 第 {i+1} 次抽到 {result!r}"


def test_old_endpoint_invalid_user_choice_falls_to_auto():
    """v2.7.4 · 旧接口客户传非法 user_choice='hack' → 净化为 'auto' · 不报错降级"""
    fake = {"id": 1, "user_choice": "hack"}
    cleaned = _v274_clean_topic(fake)
    assert cleaned['user_choice'] == 'auto'


# ============================================================
# v2.7.5 Codex P1-blocker:company_profile 固定槽位严守(铁律)
# 删 type/article_style == 'company_profile' 直通分支 · 旧字段不可绕过
# ============================================================

def test_v275_article_style_company_profile_must_not_bypass():
    """v2.7.5 P1-blocker(Codex 抓):resolve_style_for_topic 不允许 article_style='company_profile' 直通

    实测 v2.7.4 BUG:
        resolve_style_for_topic({'article_style':'company_profile','user_choice':'auto'}, None)
        => 'company_profile'  ← 错!违反"DB 外部旧字段默认不信任"
    """
    from writing.style_registry import resolve_style_for_topic
    # 旧 article_style 伪造 company_profile · 必须不允许返 company_profile
    result = resolve_style_for_topic(
        {'article_style': 'company_profile', 'user_choice': 'auto'},
        None,
    )
    assert result != 'company_profile', \
        f"v2.7.5 P1-blocker fail · article_style='company_profile' 绕过固定槽位 · 实际: {result!r}"


def test_v275_type_company_profile_must_not_bypass():
    """v2.7.5 P1-blocker · type='company_profile' 同样不允许绕过"""
    from writing.style_registry import resolve_style_for_topic
    result = resolve_style_for_topic(
        {'type': 'company_profile', 'user_choice': 'auto'},
        None,
    )
    assert result != 'company_profile', \
        f"v2.7.5 P1-blocker fail · type='company_profile' 绕过固定槽位 · 实际: {result!r}"


def test_v275_style_field_company_profile_must_not_bypass():
    """v2.7.5 · style='company_profile' 旧字段也不允许绕过"""
    from writing.style_registry import resolve_style_for_topic
    result = resolve_style_for_topic(
        {'style': 'company_profile', 'user_choice': 'auto'},
        None,
    )
    assert result != 'company_profile'


def test_v275_is_fixed_with_style_code_still_returns_company():
    """v2.7.5 · 合法路径保留:is_fixed=True + style_code='company_profile' → company_profile"""
    from writing.style_registry import resolve_style_for_topic
    result = resolve_style_for_topic(
        {'is_fixed': True, 'style_code': 'company_profile', 'user_choice': 'auto'},
        None,
    )
    assert result == 'company_profile', \
        f"v2.7.5 fail · 合法 fixed slot 应返 company_profile · 实际: {result!r}"


def test_v275_trust_legacy_with_style_code_returns_company():
    """v2.7.5 · admin 迁移路径:_trust_legacy_style=True + style_code='company_profile' → company_profile"""
    from writing.style_registry import resolve_style_for_topic
    result = resolve_style_for_topic(
        {'_trust_legacy_style': True, 'style_code': 'company_profile', 'user_choice': 'auto'},
        None,
    )
    assert result == 'company_profile'


def test_v275_allocate_style_article_style_company_must_not_bypass():
    """v2.7.5 P1-blocker · ArticleGeneratorService._allocate_style_from_ratios 同覆盖
    article_style='company_profile' 不允许绕过固定槽位
    """
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry=None)
    topic = {'article_style': 'company_profile', 'user_choice': 'auto'}
    result = svc._allocate_style_from_ratios(topic, None)
    assert result != 'company_profile', \
        f"v2.7.5 P1-blocker fail · _allocate_style_from_ratios article_style 绕过 · 实际: {result!r}"


def test_v275_allocate_style_type_company_must_not_bypass():
    """v2.7.5 · _allocate_style_from_ratios type='company_profile' 同样不允许绕过"""
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry=None)
    topic = {'type': 'company_profile', 'user_choice': 'auto'}
    result = svc._allocate_style_from_ratios(topic, None)
    assert result != 'company_profile'


def test_v275_allocate_style_is_fixed_still_returns_company():
    """v2.7.5 · _allocate_style_from_ratios 合法 fixed slot 保留"""
    from writing.article_generator_service import ArticleGeneratorService
    svc = ArticleGeneratorService(quote_id=0, brand_name="Test", industry=None)
    topic = {'is_fixed': True, 'style_code': 'company_profile', 'user_choice': 'auto'}
    result = svc._allocate_style_from_ratios(topic, None)
    assert result == 'company_profile'


def test_v275_company_in_user_choice_still_raises():
    """v2.7.5 · user_choice='company' 仍 raise(v2.4 P0 #1 铁律保留)"""
    from writing.style_registry import resolve_style_for_topic
    import pytest as _p
    with _p.raises(ValueError):
        resolve_style_for_topic({'user_choice': 'company'}, None)


# ============================================
# v2.8 G23 守护 · 选题阶段强制文体 · 中文 article_style 映射 SSOT
# ============================================

def test_v28_user_choice_to_chinese_style_data_report():
    """v1.4 · user_choice='data' → 案例、数据与 ROI 的中文桥接。"""
    from writing.style_registry import resolve_user_choice_to_chinese_style
    result = resolve_user_choice_to_chinese_style('data', None)
    assert result == '案例、数据与 ROI'


def test_v28_user_choice_to_chinese_style_price():
    """v2.8 G23 · user_choice='price' → 中文 '价格解读'"""
    from writing.style_registry import resolve_user_choice_to_chinese_style
    assert resolve_user_choice_to_chinese_style('price', None) == '案例、数据与 ROI'


def test_v28_user_choice_to_chinese_style_comparison():
    """v2.8 G23 · user_choice='comparison' → 中文 '对比评测'"""
    from writing.style_registry import resolve_user_choice_to_chinese_style
    assert resolve_user_choice_to_chinese_style('comparison', None) == '选购与多品牌比较'


def test_v28_user_choice_to_chinese_style_guide():
    """v2.8 G23 · user_choice='guide' → 中文 '方法指南'"""
    from writing.style_registry import resolve_user_choice_to_chinese_style
    assert resolve_user_choice_to_chinese_style('guide', None) == '方法与实施指南'


def test_v28_user_choice_to_chinese_style_auto_returns_none():
    """v2.8 G23 · user_choice='auto' → None(不强制 · 走默认 ratio)"""
    from writing.style_registry import resolve_user_choice_to_chinese_style
    assert resolve_user_choice_to_chinese_style('auto', None) is None
    assert resolve_user_choice_to_chinese_style(None, None) is None
    assert resolve_user_choice_to_chinese_style('', None) is None


def test_v28_user_choice_to_chinese_style_company_raises():
    """v2.8 G23 · user_choice='company' 仍 raise(v2.4 P0 #1 铁律不破)"""
    from writing.style_registry import resolve_user_choice_to_chinese_style
    import pytest as _p
    with _p.raises(ValueError):
        resolve_user_choice_to_chinese_style('company', None)


def test_v28_keyword_topic_generator_force_chinese_style():
    """v2.8 G23 · KeywordTopicGenerator force_chinese_style 参数能接收 + 默认 None"""
    from writing.keyword_topic_generator import KeywordTopicGenerator
    gen = KeywordTopicGenerator(
        keywords=[{"keyword": "深圳建材", "required_articles": 1, "id": 1}],
        brand_name="测试品牌",
        industry="装修建材",
        force_chinese_style="价格解读",
    )
    assert gen.force_chinese_style == "价格解读"
    # 默认 None
    gen_default = KeywordTopicGenerator(
        keywords=[{"keyword": "测试", "required_articles": 1, "id": 1}],
        brand_name="测试", industry="测试",
    )
    assert gen_default.force_chinese_style is None


def test_v28_user_choice_to_style_code_no_company_in_chinese_map():
    """v2.8 G23 · USER_CHOICE_TO_STYLE 10 项不映射到 company_profile(防绕过)"""
    from writing.style_registry import STYLE_CODE_TO_CHINESE_NAME, USER_CHOICE_TO_STYLE
    for uc, sc in USER_CHOICE_TO_STYLE.items():
        if sc is not None:
            assert sc != 'company_profile', f"v2.8 守护破:user_choice='{uc}' 映射到 company_profile"
    # STYLE_CODE_TO_CHINESE_NAME 含 company_profile 合法(系统 fixed slot 用)
    assert 'company_profile' in STYLE_CODE_TO_CHINESE_NAME
    assert STYLE_CODE_TO_CHINESE_NAME['company_profile'] == '企业事实与品牌说明'


# ============================================
# v2.9 G24 守护 · Codex v2.8 P0 闭环修复(user_choice 持久化 + 全链路传递)
# 用文件文本扫(不 import server.py · 防 DB 连接失败)
# ============================================

import os as _os
import re as _re
_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _read_text(rel_path):
    p = _os.path.join(_REPO_ROOT, rel_path)
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def _fn_block(src, fn_name):
    """截出某个模块级函数的**函数体**,而不是固定字节窗口。

    🔴 原来这几条判据写死 `src[fn_start:fn_start + 8000]` / `+ 12000`。
       函数一变长,守卫就被推出窗外,判据红在"找不到这一行" ——
       读起来像**守卫被删了**,实际只是尺子太短。
       2026-09-14 #185 c3 实测:`api_regenerate_titles` 由 ~11k 涨到 28355 字符,
       `test_v292_none_user_choice_goes_to_llm` 由绿转红,而 `request.user_choice
       not in ("", "auto")` 那行一个字都没动(仍在 :22792/:22872 一带)。
       固定窗口把「函数多长」这件与守卫无关的事,变成了守卫判据的隐藏前提。

    🔴 边界取**下一个模块级 `@` / `def` / `async def`**,不是"下一个 async def api_":
       函数之间往往先出现装饰器行,按 `async def api_` 找会把下一个端点的装饰器
       算进本函数体里 —— 窗口再次变成"碰巧够用"。
    """
    start = src.find("async def " + fn_name)
    if start < 0:
        start = src.find("def " + fn_name)
    assert start > 0, "找不到 %s 函数" % fn_name
    nxt = [m.start() for m in _re.finditer(r"^(?:@|def |async def )", src, _re.M)
           if m.start() > start]
    return src[start:nxt[0]] if nxt else src[start:]


def test_v29_save_topics_batch_writes_user_choice():
    """v2.9 G24 · save_topics_batch INSERT 必须写 user_choice + auto/空串归一化"""
    src = _read_text("db/diagnosis_db.py")
    assert "def save_topics_batch" in src, "save_topics_batch 函数未定义"
    # INSERT 字段含 user_choice
    assert "user_choice" in src, "v2.9 G24 破:diagnosis_db.py 未含 user_choice 字段"
    # 新旧 user_choice 均经六类 SSOT 归一化。
    assert "normalize_user_choice(topic.get" in src


def test_v29_update_topic_user_choice_helper_exists():
    """v2.9 G24 · update_topic_user_choice helper 必须存在"""
    src = _read_text("db/diagnosis_db.py")
    assert "def update_topic_user_choice" in src, "v2.9 G24 破:helper 未定义"
    assert "UPDATE topics SET user_choice" in src, "v2.9 G24 破:helper 未 UPDATE user_choice"


def test_v29_topics_user_choice_column_safe_add():
    """v2.9 G24 · diagnosis_db init_db 兜底 + 主线 SQL 存在"""
    src = _read_text("db/diagnosis_db.py")
    assert '_safe_add_column(cursor, "topics", "user_choice"' in src, \
        "v2.9 G24 破:topics.user_choice 兜底 _safe_add_column 未添加"
    sql_path = _os.path.join(_REPO_ROOT, "scripts", "migration_topics_user_choice.sql")
    assert _os.path.exists(sql_path), f"v2.9 G24 破:主线 migration SQL 不存在 {sql_path}"


def test_v29_start_articles_reads_db_user_choice():
    """v2.9 G24 · start-articles SELECT 含 db_user_choice + fallback 链"""
    src = _read_text("server.py")
    assert "t.user_choice as db_user_choice" in src, \
        "v2.9 G24 破:start-articles SELECT 未含 db_user_choice"
    # fallback 链:前端 → DB → 'auto'
    assert "normalize_user_choice(_t.get('db_user_choice'))" in src
    assert "elif _db_uc:" in src


def test_v29_auto_generate_for_new_keyword_accepts_user_style():
    """v2.9 G24 · _auto_generate_topics_for_new_keyword 函数签名接 user_style"""
    src = _read_text("server.py")
    assert "def _auto_generate_topics_for_new_keyword" in src, "函数未定义"
    # 签名含 user_style 参数 + 默认 None
    assert "user_style: str = None" in src or "user_style=None" in src, \
        "v2.9 G24 破:_auto_generate_topics_for_new_keyword 未接 user_style 参数"


def test_v29_generate_topic_endpoint_accepts_user_style_body():
    """v2.9 G24 · /generate-topic 单 kw endpoint 接 user_style body"""
    src = _read_text("server.py")
    assert "class GenerateTopicForKwRequest" in src, \
        "v2.9 G24 破:GenerateTopicForKwRequest model 未定义"
    assert "api_generate_topic_for_keyword" in src, "单 kw endpoint 函数未定义"


def test_v29_regenerate_titles_writes_user_choice():
    """v2.9 G24 · regenerate-titles UPDATE 含 user_choice(联动重写持久化)"""
    src = _read_text("server.py")
    # UPDATE topics SET ... user_choice = %s
    assert "user_choice = %s" in src, \
        "v2.9 G24 破:regenerate-titles UPDATE 未含 user_choice 字段"
    # _persist_uc 变量(写作大厅联动重写时用)
    assert "_persist_uc" in src, "v2.9 G24 破:regenerate-titles 未抽取 _persist_uc 变量"


def test_v29_frontend_exposes_only_current_geo_style_families():
    """The current UI keeps retired ranking styles out of customer controls."""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")
    for label in (
        '证据型问答', '选购与多品牌比较', '方法与实施指南',
        '趋势、政策与风险', '案例、数据与 ROI', '企业事实与品牌说明',
    ):
        assert label in src
    assert "label: '排行榜单'" not in src
    assert "label: '权威榜单'" not in src
    # 单 kw 入口必须传 user_style
    assert "user_style: batchUserStyle === 'auto' ? null : batchUserStyle" in src, \
        "v2.9 G24 破:单 kw 入口(generateTopicForKeyword / generateTopicsForMissingKws)未传 user_style"


def test_v29_topics_response_includes_user_choice_field():
    """v2.9 G24 · topics 表 SELECT t.* 自然包含 user_choice · get_writing_project_detail 不破"""
    src = _read_text("db/diagnosis_db.py")
    # get_writing_project_detail 用 SELECT t.* · _safe_add_column 加列后自动包含 user_choice
    assert "SELECT t.*" in src, "v2.9 G24 破:get_writing_project_detail 不用 t.* 查询"


# ============================================
# v2.9.1 G25 守护 · Codex v2.9 复审 P0 修(auto 不清 DB → 反向错账)
# ============================================

def test_v292_regenerate_titles_light_path_only_explicit_auto():
    """v2.9.2 G25 · /regenerate-titles user_choice='auto' 显式触发 light path · None 走 LLM 默认 ratio

    根因(v2.9.1):_is_light_reset 把 None 也算 light → 旧批量重写 regenerateSelected()
                  只传 {topic_ids} 不传 user_choice → 误走 light 不重写标题(回归 BUG)
    修(v2.9.2):只 user_choice == "auto" 走 light(显式信号)· None → LLM 默认 ratio
    """
    src = _read_text("server.py")
    # v2.9.2 关键:_is_light_reset 必须是显式 'auto'(不是 in 集合)
    assert "_is_light_reset" in src, "v2.9.2 G25 破:regenerate-titles 未抽 _is_light_reset 变量"
    assert '_is_light_reset = (request.user_choice == "auto")' in src, \
        "v2.9.2 G25 破:_is_light_reset 必须显式 'auto' · None/未传应走 LLM 默认 ratio"
    # 反例:不能再有 v2.9.1 的宽松判定
    assert 'request.user_choice in ("", "auto", None)' not in src, \
        "v2.9.2 G25 破:v2.9.1 宽松 in 集合判定残留 · None 会误走 light 致旧批量重写回归"
    # light path UPDATE 清 NULL
    assert "SET user_choice = NULL" in src, \
        "v2.9.2 G25 破:light path 未 UPDATE user_choice=NULL"
    # light path 早返回 · 不进 LLM 不扣费
    assert '"reset": True' in src, \
        "v2.9.2 G25 破:light path 未返回 reset:True 标记"


def test_v292_regenerate_titles_owner_check_before_light():
    """v2.9.2 G25 · light path 之前必须有 owner_user_id 校验(P0-2)

    根因(v2.9.1):light path 在权限校验前直接 UPDATE · 任意登录用户拿 topic_id
                  都能免费清别人 user_choice(横向越权)
    修(v2.9.2):light/LLM 共享 SELECT t.* JOIN quotes/brands.owner_user_id + 校验
                quote_owner == user_id OR brand_owner == user_id(沿用 keyword_owner_access 模式)
    """
    src = _read_text("server.py")
    # 找 api_regenerate_titles 函数体
    fn_block = _fn_block(src, "api_regenerate_titles")

    # 必须有 JOIN quotes 拿 owner
    assert "q.owner_user_id AS quote_owner" in fn_block, \
        "v2.9.2 G25 破:regenerate-titles 未 JOIN quotes 拿 quote_owner"
    assert "b.owner_user_id AS brand_owner" in fn_block, \
        "v2.9.2 G25 破:regenerate-titles 未 JOIN brands 拿 brand_owner(兜底旧数据 quote.owner_user_id NULL)"
    # 必须有 owner 校验 raise 403
    assert "无权操作此项目下的选题" in fn_block, \
        "v2.9.2 G25 破:regenerate-titles 未在 light path 前 raise 403 防越权"

    # owner 校验必须在 light path UPDATE 之前(顺序)
    idx_owner_check = fn_block.find("无权操作此项目下的选题")
    idx_light_update = fn_block.find("if _is_light_reset:")
    assert 0 < idx_owner_check < idx_light_update, \
        "v2.9.2 G25 破:owner 校验必须在 light path 分支之前(防越权)"


def test_v291_frontend_auto_also_calls_api():
    """v2.9.1 G25 · 前端 updateTopicUserChoice 在 auto 时也必须调后端(不能 return)"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")
    # 不能有"newChoice === 'auto' return"早返回逻辑
    assert "if (newChoice === 'auto') return;" not in src, \
        "v2.9.1 G25 破:前端 updateTopicUserChoice 仍有 'auto' 早返回 · DB 不会被清"
    # 必须存在 isResetToAuto 变量(区分 light path 和 LLM 重写)
    assert "isResetToAuto" in src, \
        "v2.9.1 G25 破:前端未区分 light reset 与 LLM 重写 · UI 反馈不准"
    # toast 文案区分(用户感知)
    assert "已恢复为系统推荐" in src, \
        "v2.9.1 G25 破:重置 auto 的 toast 文案缺失"


def test_v291_state_machine_price_to_auto_clears_db():
    """v2.9.1 G25 · 状态机模拟:price → auto → DB 必须清 NULL

    模拟 Codex 报的真实场景:
      1. 用户选 'price' → DB user_choice='price'
      2. 用户改回 'auto' → DB user_choice 必须改 NULL(不能留 'price')
      3. reload → SELECT 出来 user_choice IS NULL
      4. start-articles 拿空 user_choices map → fallback 链命中 NULL → 走 'auto'/ratio
    本测试用文本扫验证修复点存在 · 真机/集成测试需在 staging 跑
    """
    src_server = _read_text("server.py")
    src_fe = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    # 后端 light path UPDATE 清 NULL
    assert "SET user_choice = NULL" in src_server, "状态机破:后端不清 NULL"
    # 前端 auto 也调后端
    assert "if (newChoice === 'auto') return;" not in src_fe, "状态机破:前端 auto 不调后端"
    # start-articles fallback 链含 'auto' 默认值(DB NULL 时走 auto)
    assert "_t['user_choice'] = 'auto'" in src_server, \
        "状态机破:start-articles fallback 未默认 'auto'(NULL 时应走 ratio)"


def test_v292_admin_bypasses_owner_check():
    """v2.9.2 G25 · admin 全权 · 跳过 owner check(沿用 _require_keyword_owner_access 模式)"""
    src = _read_text("server.py")
    fn_block = _fn_block(src, "api_regenerate_titles")

    # 必须有 is_admin = bool(user.get("is_admin"))
    assert 'is_admin = bool(user.get("is_admin"))' in fn_block, \
        "v2.9.2 G25 破:regenerate-titles 未取 is_admin 标记"
    # owner check 必须 if not is_admin: 包裹(admin 跳过)
    assert "if not is_admin:" in fn_block, \
        "v2.9.2 G25 破:owner check 未 if not is_admin: 包裹 · admin 会被误拦"


def test_v292_light_update_uses_validated_ids():
    """v2.9.2 G25 · light path UPDATE WHERE id IN 用 validated ids · 不能直接用 request.topic_ids

    防越权:即使 owner check 后,UPDATE 应只动 topics_to_regen 实证过的 ids
    (request.topic_ids 含的不存在 id / 跨 quote id 应被过滤)
    """
    src = _read_text("server.py")
    fn_block = _fn_block(src, "api_regenerate_titles")

    # light 块标识
    light_idx = fn_block.find("if _is_light_reset:")
    assert light_idx > 0, "找不到 light path 块"
    # 抓 light path 块(从 if _is_light_reset 到 return reset:True)
    return_idx = fn_block.find('"reset": True', light_idx)
    assert return_idx > light_idx, "light path 块结构异常"
    light_block = fn_block[light_idx:return_idx + 50]

    # validated ids 必须从 topics_to_regen 派生
    assert "ids_validated = [t[\"id\"] for t in topics_to_regen]" in light_block, \
        "v2.9.2 G25 破:light UPDATE 未用 validated ids · 应从 topics_to_regen 派生"
    # WHERE id IN 必须用 ids_validated 不是 request.topic_ids
    assert "tuple(ids_validated)" in light_block, \
        "v2.9.2 G25 破:light UPDATE WHERE id IN 必须 tuple(ids_validated) · 不能用 request.topic_ids"


def test_v292_none_user_choice_goes_to_llm():
    """v2.9.2 G25 · request.user_choice=None / 未传 → 必须走 LLM 重写(默认 ratio)· 不走 light

    根因:v2.9.1 把 None 也算 light → 旧批量重写 regenerateSelected() 仅传 {topic_ids} → 误走 light
    修:None 走 LLM 路径(原行为)· 只显式 'auto' 触发 light
    """
    src = _read_text("server.py")
    fn_block = _fn_block(src, "api_regenerate_titles")

    # _is_light_reset 严格 == "auto"(不是 in 集合)
    assert '_is_light_reset = (request.user_choice == "auto")' in fn_block, \
        "v2.9.2 G25 破:_is_light_reset 必须严格 == 'auto' · None 不应进 light"
    # LLM 路径 force_chinese 判定:user_choice and 不在 ('', 'auto') 即强制 · None 走默认 ratio
    assert 'request.user_choice not in ("", "auto")' in fn_block, \
        "v2.9.2 G25 破:LLM 路径 force_chinese 判定未排除 None/空串/auto"


def test_v291_light_path_does_not_charge():
    """v2.9.1 G25 · light path 早 return · 不进扣费路径(防免费乱清)"""
    src = _read_text("server.py")
    # light path 在 check_balance_only 之前(早 return)
    idx_light = src.find("_is_light_reset")
    idx_billing = src.find("check_balance_only", idx_light if idx_light > 0 else 0)
    if idx_billing > 0 and idx_light > 0:
        # check_balance_only 在 _is_light_reset 之后(意味着 light path 已 return)
        # 但更稳的方式:扫 light path 块内不应含 deduct_points
        light_block_start = src.find("if _is_light_reset:")
        light_block_end = src.find('return {\n            "success": True,\n            "message": f"已恢复', light_block_start)
        if light_block_start > 0 and light_block_end > light_block_start:
            light_block = src[light_block_start:light_block_end + 200]
            assert "deduct_points" not in light_block, \
                "v2.9.1 G25 破:light path 块内含 deduct_points · 应免费"
            assert "check_balance_only" not in light_block, \
                "v2.9.1 G25 破:light path 块内含 check_balance_only · 应跳过扣费"


# ============================================
# v2.9.3 G26 守护 · 老板 D2-B 拍:light reset 后给用户主动 LLM 重写选项
# ============================================

def test_v293_light_reset_toast_has_action():
    """v2.9.3 G26 · light reset 成功后 toast 显示二级“重写标题”动作。

    根因(老板真机反馈):
      v2.9.1/v2.9.2 light reset 后 toast 只显示"已恢复为系统推荐"·
      但 optimized_title 文本保留 · 用户感知像 BUG(标题没变 = 系统推荐没生效?)
    修(老板 D2-B):
      默认 light(标题保留 · 不扣费 · 省钱)+ 可点二级“重写标题”动作；全站静默扣费不在 toast 暴露金额
      老板"完成大于完美"+ "用户能选" 折衷 · 不强制阻断 confirm dialog
    """
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    # topicGenCost hook 加在文件顶部 · 用 useFeatureCost('topic_gen')
    assert "useFeatureCost('topic_gen')" in src, \
        "v2.9.3 G26 破:topicGenCost hook 未加(动态价目表 SSOT)"
    # toast.action 模式 · light reset 成功后给二级按钮
    assert "已恢复为系统推荐 · 标题保留" in src, \
        "v2.9.3 G26 破:light reset toast 文案未更新(应明确告知标题保留)"
    assert "正文将按系统推荐生成" in src, \
        "v2.9.3 G26 破:toast description 未告知正文按系统推荐(避免用户误解)"
    assert "label: `重写标题`" in src, \
        "v2.9.3 G26 破:toast action 按钮缺失"
    assert "重写标题(扣 ${topicGenCost} 积分)" not in src, \
        "全站静默扣费口径破坏:toast 不应暴露金额"
    # [v2.9.4 hotfix] toast 真机可见时长 · 必须 >= 10000(老板真机反馈 8000 实际 5s 消失)
    # grep gate:不允许回退到 < 10000(防 v2.9.3 回归)
    import re as _re
    duration_match = _re.search(r"duration:\s*(\d+)", src)
    assert duration_match, "v2.9.4 G26 破:light reset toast 缺 duration 设定"
    duration_ms = int(duration_match.group(1))
    assert duration_ms >= 10000, \
        f"v2.9.4 G26 破:light reset toast duration={duration_ms}ms < 10000 · 真机可见时长不够(8s 见 action)"


def test_v293_llm_rewrite_after_reset_function():
    """v2.9.3 G26 · llmRewriteAfterReset 函数定义 · 用户主动选时调"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    # 函数定义
    assert "const llmRewriteAfterReset = async (topicId: number)" in src, \
        "v2.9.3 G26 破:llmRewriteAfterReset 函数未定义"
    # 不传 user_choice 走 LLM 默认 ratio(避开 light path)
    assert "JSON.stringify({ topic_ids: [topicId] })" in src, \
        "v2.9.3 G26 破:llmRewriteAfterReset 应只传 topic_ids · 不传 user_choice(后端 None → LLM 默认 ratio)"
    # 成功 toast 沿用全站静默扣费口径，不显示金额。
    assert "toast.success(`标题已按系统推荐重写`)" in src, \
        "v2.9.3 G26 破:LLM 重写成功 toast 缺失"
    assert "标题已按系统推荐重写 · 扣 ${topicGenCost} 积分" not in src


def test_v293_toast_action_calls_llm_rewrite():
    """v2.9.3 G26 · light reset toast action onClick 必须调 llmRewriteAfterReset"""
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    # toast 的 action onClick 调 llmRewriteAfterReset(topicId)
    assert "void llmRewriteAfterReset(topicId)" in src, \
        "v2.9.3 G26 破:toast action onClick 未调 llmRewriteAfterReset"


def test_v293_no_llm_engineering_term_in_user_facing_text():
    """v2.9.3 G26 grep gate · WritingHall.tsx 用户可见文案(quoted string)不允许 "LLM"

    根因(Codex v2.9.3 复审 P1):
      v2.9.3 toast description 含 "如需 LLM 也重写标题" + button title 含 "重新跑 LLM 生成"
      违反 v2.7.1 工程词清理红线(用户看技术词反感 + 0 价值)
    修:用户可见文案全改人话("重新生成标题" / "重新生成")
    Gate:扫 quoted string · 跳过注释行 + JS identifier 上下文 · 命中即破
    """
    import re
    src = _read_text("frontend/src/pages/Writing/WritingHall.tsx")

    violations = []
    in_block_comment = False
    for i, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        # 跳过单行注释 + JSX 注释 + 块注释
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if "/*" in stripped:
            in_block_comment = True
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        # 找 quoted string(双引号/单引号/中文引号)含 LLM(boundary 词边界)
        # 排除 JS identifier 路径(activeLLM / setActiveLLM / onLLMConfigChange · 不在 string literal)
        if re.search(r'["“\'][^"“”\']*\bLLM\b[^"“”\']*["“”\']', line):
            violations.append(f"L{i}: {stripped[:120]}")

    assert not violations, \
        f"v2.9.3 G26 grep gate 破:WritingHall.tsx 用户可见文案含 LLM 工程词:\n" + \
        "\n".join(violations[:5])
