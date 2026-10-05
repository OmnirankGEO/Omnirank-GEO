"""
D3 · SAP HTML 渲染单测(_render_authority_inner_html / _render_authority_page)。

验证:说人话无工程禁词 / 无效果承诺 / XSS 转义 / 折叠 details / 空与不可用降级 / page 包装。
跑法:python -m pytest tests/test_authority_html_render.py -q --noconftest -o addopts="" -p no:cacheprovider
"""
from services.report_html_renderer import _render_authority_inner_html, _render_authority_page

BANNED = [
    "brand_direct", "authority_score", "tier1", "tier2", "tier3", "jsonb",
    "citation", "source_authority", "domain_tier", "endorsement_score",
    "real_ai_citation", "industry_reference", "search_brand_direct", "source authority",
]
PROMISE = ["保证", "一定提升", "提升 AI 推荐", "保证 AI 引用", "必然", "确保上榜", "一定会"]


def _pack():
    return {
        "available": True,
        "plain": {
            "conclusion": "AI 现在主要引用的是普通网站和自媒体内容,权威媒体、百科类的背书还不够。",
            "grouped_counts": {"authority": 1, "portal": 1, "common": 3, "risk": 1},
            "suggestion": {
                "problem": "权威媒体、政府或百科类的可引用来源偏少。",
                "meaning": "AI 在回答时更倾向引用可靠来源,普通网站不容易被当作可靠依据。",
                "next_step": "建议补充权威媒体报道、行业网站或百科类内容,让 AI 更容易找到关于品牌的可靠资料。",
            },
            "note": "说明:部分 AI 平台暂不提供引用来源,以上基于能获取来源的平台与公开搜索核验,不代表全部 AI 平台。",
            "score": 7, "score_max": 20,
            "sources": [{"name": "人民网", "type": "权威媒体/政府", "situation": "AI 实际引用过"}],
        },
    }


def _assert_plain(html: str):
    low = html.lower()
    for b in BANNED:
        assert b.lower() not in low, f"客户面禁词泄漏: {b}"
    for p in PROMISE:
        assert p not in html, f"出现效果承诺: {p}"


def test_inner_plain_and_structured():
    html = _render_authority_inner_html(_pack())
    _assert_plain(html)
    assert "现在的问题是:" in html
    assert "对客户意味着:" in html
    assert "下一步建议:" in html
    assert "查看证据来源" in html
    assert "<details" in html
    assert "权威媒体/政府/百科" in html
    assert "不计入诊断总分" in html


def test_inner_empty_total_no_table():
    pack = _pack()
    pack["plain"]["grouped_counts"] = {"authority": 0, "portal": 0, "common": 0, "risk": 0}
    pack["plain"]["conclusion"] = "目前还没有足够的来源数据,暂时看不出权威背书情况。"
    html = _render_authority_inner_html(pack)
    _assert_plain(html)
    assert "暂时看不出" in html
    assert "<details" not in html  # 空数据不出折叠明细


def test_inner_unavailable_returns_empty():
    assert _render_authority_inner_html(None) == ""
    assert _render_authority_inner_html({"available": False}) == ""
    assert _render_authority_inner_html({}) == ""


def test_xss_escaped():
    pack = _pack()
    pack["plain"]["sources"] = [
        {"name": "<script>alert(1)</script>", "type": "普通网站/自媒体", "situation": "搜索时提到"}
    ]
    html = _render_authority_inner_html(pack)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_page_wrapper():
    html = _render_authority_page({"3_authority": _pack()})
    assert "权威背书情况" in html
    assert "<section" in html
    _assert_plain(html)
    # 无 3_authority / 不可用 → 空串(不渲染不报错)
    assert _render_authority_page({}) == ""
    assert _render_authority_page({"3_authority": {"available": False}}) == ""
