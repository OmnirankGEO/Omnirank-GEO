"""v2.7.1 GEO 文体改造 · 生成行为守护单测

覆盖:
- count_effective_answer_blocks() G22 答案块密度
- H1-H7 hard check + S1-S5 soft warn
- 9 类资产覆盖
- v2.7.1 Codex 中文问号守护(test_is_question_chinese_question_mark)
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from writing.article_writer import (
    ASSET_PATTERNS,
    count_effective_answer_blocks,
    validate_article_structure,
    _sanitize_extra_instruction,
    MIN_BLOCKS_BY_STYLE,
)


# ============================================================
# G22 · v2.7.1 Codex 中文问号守护补丁(5 实例:4 PASS + 1 FAIL)
# ============================================================

def test_is_question_chinese_question_mark():
    """v2.7.1 Codex 补丁:中文问号 ? 必须命中 is_question hard gate

    实例 1:"2026 年 GEO 服务商推荐?"(无问句词 / 无任务词 / 仅靠中文问号)→ 必须 passed
    实例 2:"全域上榜怎么选?"(中文问号 + 问句词)→ 必须 passed
    实例 3:"AI 回答引用规则变了吗?"(中文问号 + "吗")→ 必须 passed(靠中文问号兜底)
    实例 4:"2026 年 GEO 服务商推荐? "(尾部带半角空格 + 中文问号)→ rstrip 后必须 passed
    实例 5:"服务介绍:"(无问号 / 无问句词 / 仅冒号结尾)→ 必须 fail(防泛标题虚过)
    """
    article_md_template = """
## {title}

根据 2026 年 5 月数据显示,GEO 服务商市场规模达 60 亿元,前 10 名服务商占比 35%。
TOP 10 推荐榜单覆盖了头部服务商的核心竞争力,综合评分维度包含交付效果、客户口碑、价格透明度三大方向。
首套客户预算 1.5 - 3 万元/月,标准客户 5 - 10 万元/月,大客户 20 万元起,ROI 周期约 6 个月。
2026 年 5 月最新数据显示,头部服务商 TOP 5 综合推荐 5 家 + 进阶 5 家,具体如下:
① A 公司 综合 92 分 ② B 公司 90 分 ③ C 公司 88 分 ④ D 公司 85 分 ⑤ E 公司 82 分。
不同客户场景对应不同推荐:预算敏感型选择 B 或 C,效果导向型选择 A。
"""

    # v2.7.2:显式 ？ 全角问号 + ： 全角冒号 · 防字面字符歧义(v2.7.1 全 0x3f 半角 bug 修)
    pass_titles = [
        '2026 年 GEO 服务商推荐？',           # 全角问号(Codex 报的核心 case · 0xff1f)
        '全域上榜怎么选？',                   # 全角问号 + 问句词
        'AI 回答引用规则变了吗？',            # 全角问号 + "吗"(靠全角 ? 兜底)
        '2026 年 GEO 服务商推荐？ ',          # 尾部半角空格 + 全角问号(rstrip 测试)
        '2026 年 GEO 服务商推荐？　',     # 尾部全角空格 + 全角问号
        '2026 年 GEO 服务商推荐?',                # 半角问号也兼容(0x3f)
    ]
    for title in pass_titles:
        article_md = article_md_template.format(title=title)
        result = count_effective_answer_blocks(article_md, 'buying_guide')
        assert any(b['passed'] for b in result['blocks']), \
            f"v2.7.2 失败:中/英问号标题 {title!r}(codepoints={[hex(ord(c)) for c in title[-3:]]}) 应 passed 但被判定为非 effective_answer_block"

    # 反例 1:无问号 / 无问句词 / 无任务词 + 半角冒号 → 必须 fail
    fail_title = '服务介绍:'
    article_md = article_md_template.format(title=fail_title)
    result = count_effective_answer_blocks(article_md, 'buying_guide')
    assert not any(b['passed'] for b in result['blocks']), \
        f"v2.7.2 失败:泛标题 '{fail_title}' 应 fail 但 passed(防虚过)"

    # 反例 2:v2.7.2 加 · 无问号 + 全角冒号 → 必须 fail
    fail_title2 = '服务介绍：'  # 全角冒号 ：
    article_md = article_md_template.format(title=fail_title2)
    result = count_effective_answer_blocks(article_md, 'buying_guide')
    assert not any(b['passed'] for b in result['blocks']), \
        f"v2.7.2 失败:泛标题 '{fail_title2}'(全角冒号)应 fail 但 passed"


def test_asset_patterns_year_hit():
    """年份/时效 asset 命中率最高(5 月 49.5%)· 基础测试"""
    sample = "2026 年 1 月 · 最新更新 · 截至 2026 年"
    hit = any(p.search(sample) for p in [ASSET_PATTERNS['年份/时效']])
    assert hit, "年份/时效 asset 应命中"


def test_asset_patterns_v2_7_stricter():
    """Evidence-first: bare branding is not evidence; records are."""
    bare_brand = "我们品牌满足企业要求"
    hit_evidence = ASSET_PATTERNS['证据/核验'].search(bare_brand)
    assert hit_evidence is None, "裸'品牌+要求'不应命中证据资产"

    real_evidence = "证据来源包括监管记录和司法记录，并附核验步骤"
    hit_evidence_real = ASSET_PATTERNS['证据/核验'].search(real_evidence)
    assert hit_evidence_real is not None, "可追溯来源与核验步骤应命中证据资产"


def test_count_effective_answer_blocks_min_required():
    """Active styles keep thresholds; three high-risk legacy styles stay disabled."""
    assert MIN_BLOCKS_BY_STYLE['ranking_v2'] == 0
    assert MIN_BLOCKS_BY_STYLE['authority_ranking'] == 0
    assert MIN_BLOCKS_BY_STYLE['trojan_horse'] == 0
    assert MIN_BLOCKS_BY_STYLE['comparison_review'] == 8
    assert MIN_BLOCKS_BY_STYLE['data_report'] == 8
    assert MIN_BLOCKS_BY_STYLE['buying_guide'] == 7
    assert MIN_BLOCKS_BY_STYLE['price_roi'] == 6
    assert MIN_BLOCKS_BY_STYLE['risk_compliance'] == 6
    assert MIN_BLOCKS_BY_STYLE['qa_recommendation'] == 5
    assert MIN_BLOCKS_BY_STYLE['company_profile'] == 3


def test_count_effective_answer_blocks_empty_article():
    result = count_effective_answer_blocks("", 'buying_guide')
    assert result['total_passed'] == 0
    assert result['meets_threshold'] is False


def test_count_effective_answer_blocks_no_assets_fails():
    """无任何 9 类资产 → has_asset=False → 不 passed"""
    article_md = """
## 怎么选?

普通段落 普通段落 普通段落 普通段落 普通段落 普通段落 普通段落 普通段落
普通段落 普通段落 普通段落 普通段落 普通段落 普通段落 普通段落 普通段落
普通段落 普通段落 普通段落 普通段落 普通段落 普通段落。
"""
    result = count_effective_answer_blocks(article_md, 'buying_guide')
    # 即使有 H2 + 字数在区间 + 是问句 · 但无 asset → 不 passed
    assert not any(b['passed'] for b in result['blocks'])


def test_validate_article_structure_minimal_empty():
    """空文 / 短文 → 严重失败"""
    passed, hard, soft = validate_article_structure("", style_code='buying_guide')
    assert not passed
    assert 'H1' in hard or len(hard) > 0


def test_validate_article_structure_h_categories_typed():
    """H/S 返回 list[str] · 形如 ['H1', 'H3']"""
    passed, hard, soft = validate_article_structure("少量内容", style_code='buying_guide')
    assert isinstance(hard, list)
    assert isinstance(soft, list)
    assert all(isinstance(x, str) for x in hard)
    assert all(isinstance(x, str) for x in soft)


# ============================================================
# G21 · _sanitize_extra_instruction 12 实例(v2.4 P1 #6 抗空格 / 抗口语化)
# ============================================================

def test_sanitize_negation_pass_through():
    """负向约束透传"""
    assert "不要" in _sanitize_extra_instruction("不要写榜单")
    assert "不要" in _sanitize_extra_instruction("不要 写榜单")  # 半角空格
    assert "不要" in _sanitize_extra_instruction("不要　写榜单")  # 全角空格


def test_sanitize_negation_bie_pass_through():
    """'别' 负向词透传"""
    assert "别" in _sanitize_extra_instruction("别写TOP排名")
    assert "别" in _sanitize_extra_instruction("别写 TOP 排名")


def test_sanitize_compound_pass_through():
    """复合句保留"""
    result = _sanitize_extra_instruction("突出价格不要榜单")
    assert "突出价格" in result or "不要" in result


def test_sanitize_normal_pass_through():
    """正常补充透传"""
    assert "突出本地服务" in _sanitize_extra_instruction("突出本地服务")
    assert "突出价格" in _sanitize_extra_instruction("突出价格")


def test_sanitize_positive_strip():
    """正向要求 strip"""
    # 单"写榜单" → strip
    assert "写榜单" not in _sanitize_extra_instruction("写榜单")
    # 单"TOP 排名" → strip
    assert "TOP" not in _sanitize_extra_instruction("TOP 排名 + 哪家好")


def test_sanitize_structure_redline_strip():
    """结构红线词 strip(不论正负)"""
    # "只写 1000 字" → strip(覆盖字数区间)
    out = _sanitize_extra_instruction("只写 1000 字")
    assert "1000" not in out, f"字数下限覆盖应 strip · 实际: {out!r}"
    # "字数低于系统区间" → strip
    out = _sanitize_extra_instruction("字数低于系统区间")
    assert "低于" not in out and "系统" not in out, f"strip 失败: {out!r}"
    # "不要 FAQ" → strip(FAQ 是系统硬规则 · 无视负向)
    out = _sanitize_extra_instruction("不要 FAQ")
    assert "FAQ" not in out, f"strip FAQ 失败: {out!r}"


def test_sanitize_length_cap():
    """200 字符上限"""
    long_text = "突出价格 " * 60  # > 200
    assert len(_sanitize_extra_instruction(long_text)) <= 200
