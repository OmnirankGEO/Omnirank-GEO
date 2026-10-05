"""[Phase 3 2026-06-07] 同频对照(module 4)诊断兜底 + 渲染层 tuple 兼容 · 纯逻辑单测

同频对照原只数监测 monitoring_results 的 search_citations,"只诊断没监测"的品牌必空。
Phase 3:监测算不出同频时,兜底读本次诊断 detail_table 的 4 引擎 search_citations
(Phase 1/2A/2B 已透传)。仅在监测空时触发,不影响有监测数据的品牌。
"""
from services.report_writer_v2 import build_module_4_competitors
from services.report_html_renderer import _build_competitor_rows_v2


def _evidence(citations):
    return {"search_citations": citations}


def _diag_report_data(detail_table):
    return {"diagnosis_data": {"ai_visibility_data": {"detail_table": detail_table}}}


def test_module4_uses_monitoring_when_present():
    """监测 evidences 有引用 → 用监测算同频 · 不走诊断兜底(有监测品牌行为不变)。"""
    evidences = [
        _evidence([{"url": "https://a.com/1", "title": "源A"}]),
        _evidence([{"url": "https://a.com/1", "title": "源A"}, {"url": "https://b.com/2", "title": "源B"}]),
    ]
    rd = _diag_report_data([
        {"results": {"kimi": {"search_citations": [{"url": "https://z.com/9", "title": "诊断源Z"}]}}}
    ])
    out = build_module_4_competitors(rd, evidences=evidences, brand_id=None)
    top = dict(out["top_competitors"])
    assert top.get("源A") == 2
    assert top.get("源B") == 1
    assert "诊断源Z" not in top  # 监测非空 → 不兜底


def test_module4_fallback_to_diagnosis_when_monitoring_empty():
    """监测 evidences 空 → 兜底读诊断 detail_table 的 4 引擎 search_citations。"""
    detail_table = [
        {"question": "推荐品牌", "results": {
            "dashscope": {"brand_detected": True, "search_citations": [{"url": "https://cctv.com/x", "title": "央视"}]},
            "deepseek": {"brand_detected": False, "search_citations": [{"url": "https://cctv.com/x", "title": "央视"}]},
            "kimi": {"brand_detected": True, "search_citations": [{"url": "https://xinhua.com/y", "title": "新华"}]},
        }},
    ]
    rd = _diag_report_data(detail_table)
    out = build_module_4_competitors(rd, evidences=[], brand_id=None)
    top = dict(out["top_competitors"])
    assert top.get("央视") == 2  # qwen + deepseek 同 URL 各计 1
    assert top.get("新华") == 1


def test_module4_empty_when_no_monitoring_no_diagnosis():
    """监测空 + 诊断空 → top_competitors 空(不抛 · 降级)。"""
    out = build_module_4_competitors({}, evidences=[], brand_id=None)
    assert out["top_competitors"] == []


def test_module4_fallback_handles_malformed_detail_table():
    """诊断 detail_table 非法/缺字段/坏 citations → 不抛 · top 空。"""
    rd = {"diagnosis_data": {"ai_visibility_data": {"detail_table": "not-a-list"}}}
    assert build_module_4_competitors(rd, evidences=[], brand_id=None)["top_competitors"] == []
    rd2 = _diag_report_data([{"results": {"kimi": {"search_citations": "bad"}}}, "x", {"results": "y"}])
    assert build_module_4_competitors(rd2, evidences=[], brand_id=None)["top_competitors"] == []


def test_competitor_rows_v2_accepts_tuple_and_list_and_dict():
    """渲染层兼容 tuple(内存未 JSON 化)/ list(JSON round-trip)/ dict 三种 schema。"""
    rows_tuple = _build_competitor_rows_v2({"top_competitors": [("央视网", 3)]}, 10)
    assert "央视网" in rows_tuple and "3 / 10" in rows_tuple  # 修前 tuple 被 isinstance(list) 跳过 → 空
    rows_list = _build_competitor_rows_v2({"top_competitors": [["新华网", 2]]}, 10)
    assert "新华网" in rows_list
    rows_dict = _build_competitor_rows_v2({"top_competitors": [{"name": "人民网", "count": 1}]}, 10)
    assert "人民网" in rows_dict
    assert _build_competitor_rows_v2({"top_competitors": []}, 10) == ''
