"""
R6(Codex 复审)· SAP 真组装端到端:render_report_html(普通 v2 / PDF 路径)真的把权威背书区组装进 HTML。
覆盖:① SAP 区真进 PDF 渲染输出 ② available=False / 无 3_authority 时降级不报错不出区。
纯渲染(给定 modules_jsonb · 不碰 DB)。

跑法:python -m pytest tests/test_report_render_e2e.py -q --noconftest -o addopts="" -p no:cacheprovider
"""
from services.source_authority_analyzer import build_source_authority_pack
from services.report_writer_v2 import build_module_3_authority  # noqa: F401 (确保可导入)
from services.report_html_renderer import render_report_html


def _authority_pack():
    # 品牌被检出的监测引用 → real_ai_citation;再加一条行业参考
    pack = build_source_authority_pack(
        [{"url": "https://people.com.cn/a", "title": "人民网", "platform": "doubao", "keyword": "k",
          "is_detected": True, "mention_type": "direct"}],
        [{"url": "https://163.com/x", "title": "网易", "_tier": "industry_reference"}],
    )
    # 模拟 build_module_3_authority 的包装(available + plain)
    from services.report_writer_v2 import (
        _authority_plain_conclusion, _authority_plain_suggestion,
        _authority_grouped_counts, _AUTH_PLAIN_NOTE, _AUTH_TYPE_PLAIN, _AUTH_SITUATION_PLAIN,
    )
    problem, meaning, nxt = _authority_plain_suggestion(pack)
    pack["available"] = True
    pack["plain"] = {
        "conclusion": _authority_plain_conclusion(pack),
        "grouped_counts": _authority_grouped_counts(pack.get("tier_counts") or {}),
        "suggestion": {"problem": problem, "meaning": meaning, "next_step": nxt},
        "note": _AUTH_PLAIN_NOTE,
        "score": pack.get("endorsement_score", 0),
        "score_max": pack.get("score_max", 20),
        "sources": [
            {"name": (s.get("display_name") or s.get("domain")),
             "type": _AUTH_TYPE_PLAIN.get(s.get("tier"), "普通网站/自媒体"),
             "situation": _AUTH_SITUATION_PLAIN.get(s.get("evidence_class"), "")}
            for s in (pack.get("top_sources") or [])[:8]
        ],
    }
    return pack


def _base_modules():
    return {
        "1": {"module": 1, "rendered_md": "", "conclusion_text": "测试结论", "differentiation_text": ""},
        "2": {"module": 2, "summary": {"total_score": 50, "max_score": 100, "level": "起步级", "level_meta": {}},
              "dimensions": [], "keyword_strata": []},
        "3": {"module": 3, "evidences": [], "evidence_count": {"A": 0, "B": 0, "C": 0}, "evidence_total": 0},
        "3_raw": {"module": "3_raw", "tests": [], "rendered_md": ""},
        "4": {"module": 4, "declared_competitors": []},
        "5": {"module": 5, "rendered_md": ""},
        "6": {"module": 6, "todos": [], "rendered_md": ""},
        "7": {"module": 7, "rendered_md": ""},
        "8": {"module": 8, "rendered_md": ""},
        "0": {"module": 0, "rendered_md": "", "groups": []},
    }


def _meta():
    return {"brand_name": "测试品牌", "total_score": 50, "level": "起步级",
            "industry": "测试行业", "city": "深圳", "created_at": "2026-06-03", "audience": "client"}


def _completeness():
    return {"score": 60, "level": "中", "groups": [], "impact_notes": [], "missing_summary": ""}


def test_report_html_includes_authority_section():
    modules = _base_modules()
    modules["3_authority"] = _authority_pack()
    html = render_report_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": modules}, "funnel": {"total_score": 50, "level": "起步级", "layers": []}},
        completeness=_completeness(),
        audience="client",
    )
    assert isinstance(html, str) and len(html) > 0
    # SAP 区真被组装进 PDF/v2 HTML
    assert "权威背书情况" in html
    assert "查看证据来源" in html
    # 客户面无工程字段 / 无效果承诺
    low = html.lower()
    for term in ["brand_direct", "authority_score", "tier1", "source_authority", "endorsement_score"]:
        assert term not in low
    for term in ["保证 AI 引用", "一定提升", "提升 AI 推荐"]:
        assert term not in html


def test_report_html_degrades_without_authority():
    # 无 3_authority(老报告)→ 不出权威背书区 · 不报错
    modules = _base_modules()  # 不含 3_authority
    html = render_report_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": modules}, "funnel": {"total_score": 50, "level": "起步级", "layers": []}},
        completeness=_completeness(),
        audience="client",
    )
    assert isinstance(html, str) and len(html) > 0
    assert "权威背书情况" not in html


def test_report_html_authority_unavailable_no_section():
    # 3_authority 存在但 available=False(SAP 取数失败降级)→ 不渲染该区 · 不报错
    modules = _base_modules()
    modules["3_authority"] = {"module": "3_authority", "available": False, "rendered_md": ""}
    html = render_report_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": modules}, "funnel": {"total_score": 50, "level": "起步级", "layers": []}},
        completeness=_completeness(),
        audience="client",
    )
    assert isinstance(html, str) and len(html) > 0
    assert "权威背书情况" not in html
