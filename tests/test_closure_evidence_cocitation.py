"""[收口 2026-06-07] 权威背书/同频/证据 完整闭环 · Phase 4(证据卡诊断兜底)+ 收口1(分母)

- extract_diagnosis_evidence:监测空时从诊断 detail_table 构造展示证据(只展示·不进主分)
- build_module_3_evidence:监测证据空 → 诊断兜底;监测+诊断都空 → 维持"数据不足"
- build_module_4_competitors:返回 co_citation_total(真实样本数)
- render_customer_decision_page_html:同频对照分母用 co_citation_total · 不再默认 12
纯逻辑/纯渲染 · 不依赖真 DB(监测取数 DB 不可用时自然走兜底)。
"""
from services.report_evidence import extract_diagnosis_evidence
from services.report_writer_v2 import build_module_3_evidence, build_module_4_competitors
from services.report_html_renderer import render_customer_decision_page_html


def _detail_table():
    return [
        {"question": "推荐这个行业的品牌", "results": {
            "dashscope": {"brand_detected": True, "full_response": "AAA 品牌很专业,值得推荐。",
                          "search_citations": [{"url": "https://cctv.com/x", "title": "央视"}]},
            "deepseek": {"brand_detected": False, "response": "行业里有几家可以对比。",
                         "search_citations": [{"url": "https://cctv.com/x", "title": "央视"}]},
            "kimi": {"brand_detected": True, "answer_summary": "AAA 在该领域口碑不错。",
                     "search_citations": [{"url": "https://xinhua.com/y", "title": "新华"}]},
            "doubao": {"brand_detected": False, "full_response": "",  # 空回答 → 跳过
                       "search_citations": []},
        }},
    ]


# ---------- extract_diagnosis_evidence ----------

def test_extract_diagnosis_evidence_basic():
    evs = extract_diagnosis_evidence(_detail_table(), brand_name="AAA")
    assert len(evs) == 3  # doubao 空回答被跳过
    assert {e["source_type"] for e in evs} == {"dashscope", "deepseek", "kimi"}
    lv = {e["source_type"]: e["evidence_level"] for e in evs}
    assert lv["dashscope"] == "A"  # brand_detected → direct → A
    assert lv["kimi"] == "A"
    assert lv["deepseek"] == "B"   # 未检出 + 正文不含 AAA → B
    ds = next(e for e in evs if e["source_type"] == "dashscope")
    assert ds["source_label"] == "通义千问"
    assert ds["search_citations"] == [{"url": "https://cctv.com/x", "title": "央视"}]
    assert "AAA" in ds["response_snippet"]


def test_extract_diagnosis_evidence_malformed_safe():
    assert extract_diagnosis_evidence("not-a-list") == []
    assert extract_diagnosis_evidence([{"results": "bad"}, "x", {"results": {"kimi": "bad"}}]) == []
    # 未知引擎跳过
    assert extract_diagnosis_evidence(
        [{"question": "q", "results": {"metaso": {"full_response": "x", "brand_detected": True}}}]
    ) == []


def test_extract_diagnosis_evidence_dedup_keeps_highest_level():
    dt = [
        {"question": "同一问题", "results": {"kimi": {"brand_detected": True, "full_response": "答1"}}},
        {"question": "同一问题", "results": {"kimi": {"brand_detected": False, "full_response": "答2"}}},
    ]
    evs = extract_diagnosis_evidence(dt, brand_name="")
    assert len(evs) == 1 and evs[0]["evidence_level"] == "A"


# ---------- build_module_3_evidence 诊断兜底 ----------

def test_module3_falls_back_to_diagnosis_when_no_monitoring():
    """无监测(DB 不可用→监测证据空)→ module3 用诊断 detail_table 兜底,不返数据不足。"""
    rd = {"brand_name": "AAA", "diagnosis_data": {"ai_visibility_data": {"detail_table": _detail_table()}}}
    out = build_module_3_evidence(rd, brand_id=999999, days=30)
    assert out["evidences"] and len(out["evidences"]) == 3
    assert out.get("evidence_shortfall") != 5  # 没走"0 条数据不足"分支


def test_module3_data_insufficient_when_no_monitoring_no_diagnosis():
    """监测空 + 诊断也空 → 维持"数据不足"降级(不粉饰)。"""
    rd = {"brand_name": "AAA", "diagnosis_data": {"ai_visibility_data": {"detail_table": []}}}
    out = build_module_3_evidence(rd, brand_id=999999, days=30)
    assert out["evidences"] == []
    assert out.get("evidence_shortfall") == 5


# ---------- build_module_4 co_citation_total ----------

def test_module4_co_citation_total_monitoring_path():
    evidences = [
        {"search_citations": [{"url": "https://a.com/1", "title": "源A"}]},
        {"search_citations": [{"url": "https://a.com/1", "title": "源A"}]},
    ]
    out = build_module_4_competitors({}, evidences=evidences, brand_id=None)
    assert out["co_citation_total"] == 2  # = len(evidences)


def test_module4_co_citation_total_diagnosis_fallback():
    rd = {"diagnosis_data": {"ai_visibility_data": {"detail_table": _detail_table()}}}
    out = build_module_4_competitors(rd, evidences=[], brand_id=None)
    assert dict(out["top_competitors"]).get("央视") == 2
    assert out["co_citation_total"] >= 2  # 诊断问答数 > 默认 1


# ---------- cdv2 渲染分母(函数级客户报告渲染) ----------

def _meta():
    return {"brand_name": "AAA", "total_score": 50, "level": "起步级", "industry": "测试",
            "city": "深圳", "created_at": "2026-06-03", "diagnosis_id": 1}


def _completeness():
    return {"score": 60, "level": "中", "groups": [], "impact_notes": [], "missing_summary": ""}


def _modules(co_total):
    return {
        "1": {"module": 1, "rendered_md": "", "conclusion_text": "结论", "differentiation_text": ""},
        "2": {"module": 2, "summary": {"total_score": 50, "max_score": 100, "level": "起步级", "level_meta": {}},
              "dimensions": [], "keyword_strata": []},
        "3": {"module": 3, "evidences": [], "evidence_count": {"A": 0, "B": 0, "C": 0}, "evidence_total": 0},
        "3_raw": {"module": "3_raw", "tests": [], "rendered_md": ""},
        "4": {"module": 4, "top_competitors": [["央视网", 3], ["新华网", 2]],
              "co_citation_total": co_total, "declared_competitors": []},
        "5": {"module": 5, "rendered_md": ""},
        "6": {"module": 6, "todos": [], "rendered_md": ""},
        "7": {"module": 7, "rendered_md": ""},
        "8": {"module": 8, "rendered_md": ""},
        "0": {"module": 0, "rendered_md": "", "groups": []},
    }


def test_cdv2_cocitation_denominator_uses_co_citation_total():
    """同频对照分母用 module4 的 co_citation_total(=8)· 不再默认 12。"""
    html = render_customer_decision_page_html(
        meta=_meta(),
        modules_jsonb={"client": {"modules": _modules(8)}},
        completeness=_completeness(),
        audience="client",
    )
    assert isinstance(html, str) and len(html) > 0
    assert "央视网" in html
    assert "3 / 8" in html       # 分母 = co_citation_total
    assert "3 / 12" not in html  # 不再默认 12
