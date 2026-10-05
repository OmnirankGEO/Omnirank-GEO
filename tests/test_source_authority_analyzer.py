"""
Source Authority Pack 聚合服务单测(纯逻辑,不碰 DB)。

覆盖:四级映射 / 域名提取 / 去重合并 / evidence_class 归类 / real_ai 覆盖 brand_direct /
背书分上限 / 行业参考不计分 / 空输入降级 / 命名隔离(endorsement_score 而非 authority_score)/ 禁夸大话术。

跑法:
  $env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
  python -m pytest tests/test_source_authority_analyzer.py -q
"""
from services.source_authority_analyzer import (
    extract_domain,
    classify_sap_tier,
    build_source_authority_pack,
    _compute_endorsement_score,
    _tier_to_evidence_class,
    _extract_diagnosis_citations,
    _dedupe_ai_citations,
)


# ---------- 域名提取 ----------
def test_extract_domain():
    assert extract_domain("https://www.people.com.cn/n1/2024/x.html") == "people.com.cn"
    assert extract_domain("http://36kr.com/p/123") == "36kr.com"
    assert extract_domain("baike.baidu.com/item/x") == "baike.baidu.com"
    assert extract_domain("") == ""
    assert extract_domain(None) == ""


# ---------- 四级(+百科)映射 ----------
def test_classify_sap_tier():
    assert classify_sap_tier("people.com.cn") == "tier1_national"        # S
    assert classify_sap_tier("baike.baidu.com") == "structured_encyclopedia"
    assert classify_sap_tier("36kr.com") == "tier2_portal_vertical"      # A
    assert classify_sap_tier("163.com") == "tier2_portal_vertical"       # B
    assert classify_sap_tier("csdn.net") == "tier3_small_media_wemedia"  # C
    assert classify_sap_tier("douyin.com") == "tier3_small_media_wemedia"  # social(非短链)
    assert classify_sap_tier("t.cn") == "risk_low_quality"               # D + blacklist 短链
    assert classify_sap_tier("some-unknown-blog.example") == "tier3_small_media_wemedia"  # D + gray


# ---------- evidence_class 归类 ----------
def test_tier_to_evidence_class():
    assert _tier_to_evidence_class("brand_direct") == "search_brand_direct"
    assert _tier_to_evidence_class("industry_reference") == "industry_reference"
    assert _tier_to_evidence_class("irrelevant") is None
    assert _tier_to_evidence_class(None) == "industry_reference"   # 缺失保守归行业参考
    assert _tier_to_evidence_class("unknown_x") == "industry_reference"


# ---------- 去重合并 ----------
def test_dedup_and_platform_union():
    monitoring = [
        {"url": "https://people.com.cn/a", "title": "人民网报道A", "platform": "doubao", "keyword": "k1", "is_detected": True, "mention_type": "direct"},
        {"url": "https://people.com.cn/b", "title": "人民网报道BBBB", "platform": "dashscope", "keyword": "k2", "is_detected": True, "mention_type": "direct"},
    ]
    pack = build_source_authority_pack(monitoring, [])
    # 同域名聚合成 1 个 source
    assert len(pack["top_sources"]) == 1
    src = pack["top_sources"][0]
    assert src["domain"] == "people.com.cn"
    assert src["ai_cited_count"] == 2
    assert set(src["platforms"]) == {"doubao", "dashscope"}
    # 保留较长标题
    assert src["sample_title"] == "人民网报道BBBB"
    assert src["evidence_class"] == "real_ai_citation"


# ---------- real_ai 覆盖 brand_direct ----------
def test_real_ai_overrides_brand_direct():
    monitoring = [{"url": "https://36kr.com/x", "title": "36氪", "platform": "doubao", "keyword": "k", "is_detected": True, "mention_type": "direct"}]
    brand_direct = [{"url": "https://36kr.com/x", "title": "36氪", "_tier": "brand_direct"}]
    pack = build_source_authority_pack(monitoring, brand_direct)
    assert len(pack["top_sources"]) == 1
    assert pack["top_sources"][0]["evidence_class"] == "real_ai_citation"


# ---------- 背书分上限 ----------
def test_endorsement_score_caps():
    # 一级 +5 上限 12
    assert _compute_endorsement_score({"tier1_national": 1}) == 5
    assert _compute_endorsement_score({"tier1_national": 3}) == 12  # min(12, 15)
    assert _compute_endorsement_score({"tier1_national": 10}) == 12
    # 二级 +2 上限 8
    assert _compute_endorsement_score({"tier2_portal_vertical": 4}) == 8
    assert _compute_endorsement_score({"tier2_portal_vertical": 10}) == 8
    # 三级 +0.5 上限 3
    assert _compute_endorsement_score({"tier3_small_media_wemedia": 6}) == 3
    # 组合,总上限 20
    assert _compute_endorsement_score({
        "tier1_national": 3, "tier2_portal_vertical": 4, "tier3_small_media_wemedia": 6
    }) == 20  # 12 + 8 + 3 = 23 → cap 20
    # 结构化百科与一级共用上限 12
    assert _compute_endorsement_score({"structured_encyclopedia": 2, "tier1_national": 1}) == 12  # min(12,15)


def test_real_ai_tier1_scores():
    monitoring = [{"url": "https://people.com.cn/a", "title": "t", "platform": "doubao", "keyword": "k", "is_detected": True, "mention_type": "direct"}]
    pack = build_source_authority_pack(monitoring, [])
    assert pack["endorsement_score"] == 5
    assert pack["tier_counts"]["tier1_national"] == 1


# ---------- P0(Codex 复审):监测引用必须按 is_detected/mention_type 区分,不得全算品牌背书 ----------
def test_monitoring_not_detected_not_scored():
    # 品牌未被检出的回答里的引用 → 不计背书分 · 不算 real_ai · 不显示"AI 实际引用过"
    monitoring = [{"url": "https://people.com.cn/a", "title": "人民网", "platform": "doubao", "keyword": "k", "is_detected": False, "mention_type": "none"}]
    pack = build_source_authority_pack(monitoring, [])
    assert pack["endorsement_score"] == 0
    assert pack["data_quality"]["has_real_ai_citations"] is False
    src = pack["top_sources"][0]
    assert src["evidence_class"] == "industry_reference"
    # 说人话渲染时类别映射为"行业参考",绝不是"AI 实际引用过"
    assert src["evidence_class"] != "real_ai_citation"


def test_monitoring_mention_none_not_scored():
    # is_detected=True 但 mention_type=none → 仍不算真实品牌引用
    monitoring = [{"url": "https://people.com.cn/a", "title": "人民网", "platform": "doubao", "keyword": "k", "is_detected": True, "mention_type": "none"}]
    pack = build_source_authority_pack(monitoring, [])
    assert pack["endorsement_score"] == 0
    assert pack["data_quality"]["has_real_ai_citations"] is False
    assert pack["top_sources"][0]["evidence_class"] == "industry_reference"


def test_monitoring_detected_is_scored():
    # 品牌被检出 + mention_type 非 none → real_ai_citation 计分
    monitoring = [{"url": "https://people.com.cn/a", "title": "人民网", "platform": "doubao", "keyword": "k", "is_detected": True, "mention_type": "direct"}]
    pack = build_source_authority_pack(monitoring, [])
    assert pack["endorsement_score"] == 5
    assert pack["data_quality"]["has_real_ai_citations"] is True
    assert pack["top_sources"][0]["evidence_class"] == "real_ai_citation"


def test_monitoring_missing_flags_not_scored():
    # 缺 is_detected 字段(防御)→ 视为未检出 · 不计分
    monitoring = [{"url": "https://people.com.cn/a", "title": "人民网", "platform": "doubao", "keyword": "k"}]
    pack = build_source_authority_pack(monitoring, [])
    assert pack["endorsement_score"] == 0
    assert pack["top_sources"][0]["evidence_class"] == "industry_reference"


# ---------- 行业参考不计分 ----------
def test_industry_reference_not_scored():
    brand_direct = [{"url": "https://163.com/x", "title": "网易", "_tier": "industry_reference"}]
    pack = build_source_authority_pack([], brand_direct)
    # 展示出来(tier2),但不计分
    assert pack["tier_counts"]["tier2_portal_vertical"] == 1
    assert pack["source_classes"]["industry_reference"]["count"] == 1
    assert pack["endorsement_score"] == 0


# ---------- 风险来源不计分 ----------
def test_risk_not_scored():
    brand_direct = [{"url": "https://t.cn/abc", "title": "短链", "_tier": "brand_direct"}]
    pack = build_source_authority_pack([], brand_direct)
    assert pack["tier_counts"]["risk_low_quality"] == 1
    assert pack["endorsement_score"] == 0


# ---------- irrelevant 丢弃 ----------
def test_irrelevant_dropped():
    brand_direct = [
        {"url": "https://people.com.cn/a", "title": "t", "_tier": "irrelevant"},
        {"url": "https://36kr.com/b", "title": "t", "_tier": "brand_direct"},
    ]
    pack = build_source_authority_pack([], brand_direct)
    assert len(pack["top_sources"]) == 1
    assert pack["top_sources"][0]["domain"] == "36kr.com"


# ---------- 空输入降级 ----------
def test_empty_input_degrades():
    pack = build_source_authority_pack([], [], has_monitoring_query=True)
    assert pack["endorsement_score"] == 0
    assert pack["top_sources"] == []
    assert pack["data_quality"]["has_real_ai_citations"] is False
    assert pack["data_quality"]["has_search_brand_direct"] is False
    assert any("暂无足够" in g for g in pack["gaps"])


# ---------- 命名隔离:必须是 endorsement_score,绝不出现 authority_score ----------
def test_score_field_isolation():
    pack = build_source_authority_pack(
        [{"url": "https://people.com.cn/a", "title": "t", "platform": "doubao", "keyword": "k"}], []
    )
    assert "endorsement_score" in pack
    assert pack["score_max"] == 20
    # 严防与 5 维评分维度 authority_score 撞名
    assert "authority_score" not in pack
    assert pack["module"] == "3_authority"


# ---------- 禁夸大话术 ----------
def test_no_exaggeration_wording():
    pack = build_source_authority_pack(
        [{"url": "https://people.com.cn/a", "title": "t", "platform": "doubao", "keyword": "k"}],
        [{"url": "https://36kr.com/b", "title": "t", "_tier": "industry_reference"}],
    )
    blob = pack["score_explanation"]
    for cls in pack["source_classes"].values():
        blob += cls["note"]
    blob += pack["data_quality"]["citation_engine_note"]
    for forbidden in ["保证", "一定提升", "已信任", "必然", "确保上榜"]:
        assert forbidden not in blob
    # 平台限制如实标注
    assert "Kimi" in pack["data_quality"]["citation_engine_note"]


# ---------- 数据完整性:tier_counts 五档键齐全 ----------
def test_tier_counts_keys_complete():
    pack = build_source_authority_pack([], [])
    for k in ["tier1_national", "structured_encyclopedia", "tier2_portal_vertical",
              "tier3_small_media_wemedia", "risk_low_quality"]:
        assert k in pack["tier_counts"]


# ========== Phase1-B (2026-06-07): 诊断 detail_table 引用透传 + 合并去重 ==========

_DIAG_DT = [{
    "question": "会议系统方案推荐",
    "results": {
        "dashscope": {"brand_detected": True, "full_response": "x",
                      "search_citations": [{"url": "https://www.sohu.com/a/123", "title": "搜狐"}]},
        "kimi": {"brand_detected": False,
                 "search_citations": [{"url": "https://blog.csdn.net/x", "title": "CSDN"}]},
        "doubao": {"brand_detected": True, "search_citations": []},  # 无来源 → 跳过
    },
}]


def test_extract_diagnosis_citations_basic():
    flat = _extract_diagnosis_citations(_DIAG_DT)
    assert len(flat) == 2  # sohu + csdn(doubao 无 cit 跳过)
    sohu = [c for c in flat if "sohu.com" in c["url"]][0]
    csdn = [c for c in flat if "csdn.net" in c["url"]][0]
    # brand_detected=True → mention_type=direct(计入背书口径);False → none(仅行业参考)
    # [P0-3 2026-07-26] 词表归一：direct → mentioned
    assert sohu["is_detected"] is True and sohu["mention_type"] == "mentioned" and sohu["platform"] == "dashscope"
    assert csdn["is_detected"] is False and csdn["mention_type"] == "none" and csdn["platform"] == "kimi"
    assert sohu["keyword"] == "会议系统方案推荐"


def test_extract_diagnosis_citations_safe_on_bad_input():
    assert _extract_diagnosis_citations(None) == []
    assert _extract_diagnosis_citations("x") == []
    assert _extract_diagnosis_citations([{"results": "bad"}]) == []
    assert _extract_diagnosis_citations([{"results": {"qwen": {"search_citations": "bad"}}}]) == []


def test_dedupe_ai_citations_merges_and_upgrades_detected():
    mon = [{"url": "https://www.sohu.com/a/123", "platform": "dashscope",
            "keyword": "会议系统方案推荐", "is_detected": False, "mention_type": "none", "title": "搜狐"}]
    flat = _extract_diagnosis_citations(_DIAG_DT)  # sohu(detected) + csdn
    merged = _dedupe_ai_citations(mon, flat)
    assert len(merged) == 2  # sohu 合并(同 url+platform+keyword)
    sohu = [c for c in merged if "sohu.com" in c["url"]][0]
    assert sohu["is_detected"] is True  # 任一来源 detected → 升级,保留背书计分


def test_diagnosis_citation_flows_to_real_ai_citation():
    """诊断侧 brand_detected 的引用,经合并 → build_source_authority_pack 应计为 real_ai_citation。"""
    flat = _extract_diagnosis_citations(_DIAG_DT)
    merged = _dedupe_ai_citations([], flat)
    pack = build_source_authority_pack(merged, [], has_monitoring_query=True)
    assert len(pack["top_sources"]) > 0  # 监测为空也非空态
    # sohu 是 brand_detected → real_ai_citation;csdn 未检出 → industry_reference
    assert pack["source_classes"]["real_ai_citation"]["count"] >= 1


def test_aggregate_source_authority_diagnosis_only_entry(monkeypatch):
    """[返修2 2026-06-07] 聚合入口级:监测查询失败/空 + 诊断 detail_table 有引用 →
    top_sources 非空 · has_real_ai_citations=True · has_monitoring_citations=False(不误标)· has_diagnosis_citations=True。"""
    import services.source_authority_analyzer as sa
    monkeypatch.setattr(sa, "_fetch_monitoring_citations", lambda *a, **k: ([], False))
    monkeypatch.setattr(sa, "_build_ai_tier_classifier", lambda *a, **k: sa.classify_sap_tier)
    pack = sa.aggregate_source_authority(brand_id=1, web_search_data={}, diagnosis_detail_table=_DIAG_DT)
    dq = pack["data_quality"]
    assert len(pack["top_sources"]) > 0
    assert dq["has_real_ai_citations"] is True
    assert dq["has_monitoring_citations"] is False  # 无真实监测引用 → 字段不误标
    assert dq["has_diagnosis_citations"] is True


def test_has_monitoring_citations_backward_compat():
    """[返修2] 不传新字段时 has_monitoring_citations 仍按旧逻辑(有监测引用)· has_diagnosis_citations 默认 False。"""
    mon = [{"url": "https://www.gov.cn/x", "platform": "dashscope", "keyword": "q",
            "is_detected": True, "mention_type": "direct", "title": "gov"}]
    pack = build_source_authority_pack(mon, [], has_monitoring_query=True)
    assert pack["data_quality"]["has_monitoring_citations"] is True
    assert pack["data_quality"]["has_diagnosis_citations"] is False
