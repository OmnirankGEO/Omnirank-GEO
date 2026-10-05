"""
D2 · SAP 报告渲染(_render_authority_md)"说人话"合规单测。

验证客户面 markdown:无工程字段禁词 / 无效果承诺 / 一句话结论 + 三类数量 + 建议三段式。
纯逻辑(用 analyzer 产 pack,不碰 DB)。

跑法:
  $env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
  python -m pytest tests/test_report_authority_render.py -q --noconftest -o addopts="" -p no:cacheprovider
"""
from services.source_authority_analyzer import build_source_authority_pack
from services.report_writer_v2 import (
    _render_authority_md,
    _authority_plain_conclusion,
    _authority_plain_suggestion,
    _AUTH_BANNED_TERMS,
)

# 客户面绝不能出现的工程字段 / 英文 jargon
_FORBIDDEN_SUBSTR = list(_AUTH_BANNED_TERMS) + [
    "source authority", "real_ai_citation", "search_brand_direct",
    "industry_reference", "structured_encyclopedia", "tier1_national",
]
# 禁止的效果承诺
_FORBIDDEN_PROMISE = [
    "保证", "一定提升", "提升 AI 推荐", "保证 AI 引用",
    "投放后会被 AI 收录", "必然", "确保上榜", "一定会",
]


def _assert_plain(md: str):
    low = md.lower()
    for term in _FORBIDDEN_SUBSTR:
        assert term.lower() not in low, f"客户面禁词泄漏: {term}\n{md}"
    for term in _FORBIDDEN_PROMISE:
        assert term not in md, f"出现效果承诺: {term}\n{md}"


def _rich_pack():
    monitoring = [
        {"url": "https://people.com.cn/a", "title": "人民网", "platform": "doubao", "keyword": "k1"},
        {"url": "https://36kr.com/b", "title": "36氪", "platform": "doubao", "keyword": "k2"},
    ]
    brand_direct = [
        {"url": "https://163.com/x", "title": "网易", "_tier": "industry_reference"},
        {"url": "https://baijiahao.baidu.com/y", "title": "百家号", "_tier": "brand_direct"},
        {"url": "https://csdn.net/z", "title": "CSDN", "_tier": "industry_reference"},
    ]
    return build_source_authority_pack(monitoring, brand_direct)


def test_render_rich_is_plain_and_structured():
    md = _render_authority_md(_rich_pack())
    _assert_plain(md)
    assert "## 权威背书情况" in md
    assert "来源构成" in md
    assert "权威媒体/政府/百科" in md
    assert "门户/行业网站" in md
    assert "普通网站/自媒体" in md
    # 建议三段式(王姐可直接讲)
    assert "现在的问题是:" in md
    assert "对客户意味着:" in md
    assert "下一步建议:" in md
    # 背书强度有展示但明确不计入总分
    assert "不计入诊断总分" in md
    # 不暴露工程分级名,但明细类型说人话
    assert "权威媒体/政府" in md or "百科" in md


def test_render_empty_degrades_plain():
    md = _render_authority_md(build_source_authority_pack([], []))
    _assert_plain(md)
    assert "## 权威背书情况" in md
    assert "暂时看不出" in md or "还没有足够" in md
    # 空数据不应出现来源明细表头
    assert "来源明细" not in md


def test_plain_conclusion_no_authority():
    # 全是普通网站 → 结论应点出"权威背书还不够"
    pack = build_source_authority_pack(
        [], [{"url": "https://csdn.net/a", "title": "x", "_tier": "brand_direct"}]
    )
    concl = _authority_plain_conclusion(pack)
    assert "权威" in concl
    for term in _FORBIDDEN_PROMISE:
        assert term not in concl


def test_plain_suggestion_format():
    problem, meaning, suggestion = _authority_plain_suggestion(_rich_pack())
    assert problem and meaning and suggestion
    # 建议必须是"补充来源"导向,不承诺效果
    assert "建议补充" in suggestion
    for term in _FORBIDDEN_PROMISE:
        assert term not in (problem + meaning + suggestion)


def test_module_order_contains_authority():
    # additive competition/e4 模块可位于其间；只锁定 3_authority 在 3_raw 后、4 前。
    import services.report_writer_v2 as rw
    import inspect
    src = inspect.getsource(rw.assemble_report_v2)
    order_line = next(line for line in src.splitlines() if "module_order =" in line)
    assert order_line.index('"3_raw"') < order_line.index('"3_authority"') < order_line.index('"4"')
