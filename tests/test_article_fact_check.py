"""
test_article_fact_check.py · C3.2 (CTO-15.9 session 3 · 2026-04-25)

P1.5 文章事实核验启发式 · 4 类风险检测
"""
from __future__ import annotations

import pytest


def test_empty_content_no_risks():
    from services.article_fact_check import check_article
    r = check_article("")
    assert r["has_risks"] is False
    assert r["risks"] == []


def test_fake_data_without_citation_caught():
    from services.article_fact_check import check_article
    text = "我们的服务效率提升 73% · 客户增长 5 倍 · 用户超过 100 万。"
    r = check_article(text)
    assert r["has_risks"]
    cats = {x["category"] for x in r["risks"]}
    assert "fake_data" in cats


def test_fake_data_with_citation_passed():
    from services.article_fact_check import check_article
    text = "据 IDC 报告(来源: IDC 2024 中国软件市场白皮书)· 行业增长 73%。"
    r = check_article(text)
    fake_data_risks = [x for x in r["risks"] if x["category"] == "fake_data"]
    assert len(fake_data_risks) == 0, "有引用源不应判定为 fake_data"


def test_fake_award_high_severity():
    from services.article_fact_check import check_article
    text = "我们是国家级认证企业 · 行业第一品牌 · 唯一可靠选择。"
    r = check_article(text)
    award_risks = [x for x in r["risks"] if x["category"] == "fake_award"]
    assert len(award_risks) >= 2  # "国家级" + "唯一" 至少 2 个
    for risk in award_risks:
        assert risk["severity"] == "high"


def test_fake_case_with_real_clients_filter():
    from services.article_fact_check import check_article
    text = "服务过 阿里 客户 · 腾讯 客户 · 华为 客户 · 字节 客户。"
    real = ["阿里", "腾讯"]  # 阿里腾讯真实 · 华为字节虚构
    r = check_article(text, real_client_names=real)
    case_risks = [x for x in r["risks"] if x["category"] == "fake_case"]
    fake_names = {x["phrase"] for x in case_risks}
    assert "华为" in fake_names or "字节" in fake_names


def test_stale_words_detected():
    from services.article_fact_check import check_article
    text = "今年我们获得了最新认证 · 近 3 年增长显著。"
    r = check_article(text)
    stale_risks = [x for x in r["risks"] if x["category"] == "stale"]
    assert len(stale_risks) >= 1
    for risk in stale_risks:
        assert risk["severity"] == "low"


def test_summary_format():
    from services.article_fact_check import check_article
    text = "国家级认证 · 行业第一 · 增长 5 倍。"
    r = check_article(text)
    assert "高风险" in r["summary"] or "中风险" in r["summary"]
    assert r["high_count"] >= 1
    assert r["medium_count"] >= 0


def test_clean_article_passes():
    from services.article_fact_check import check_article
    text = "我们专注于 AI 搜索优化服务 · 帮助品牌在豆包、Kimi、DeepSeek、通义千问中提升可见度。"
    r = check_article(text)
    assert r["has_risks"] is False
    assert "未发现" in r["summary"]


def test_dedup_same_phrase():
    """同一极限词重复出现只计 1 次"""
    from services.article_fact_check import check_article
    text = "国家级 服务 · 国家级 资质 · 国家级 认证。"
    r = check_article(text)
    award_risks = [x for x in r["risks"] if x["category"] == "fake_award" and x["phrase"] == "国家级"]
    assert len(award_risks) == 1


def test_severity_order_in_results():
    """风险按 high → medium → low 排序"""
    from services.article_fact_check import check_article
    text = "国家级 · 增长 5 倍 · 今年。"  # high + medium + low 各 1
    r = check_article(text)
    severities = [x["severity"] for x in r["risks"]]
    sev_order = {"high": 0, "medium": 1, "low": 2}
    indexed = [sev_order[s] for s in severities]
    assert indexed == sorted(indexed), "应按 severity 升序"


@pytest.mark.parametrize("text,expected_min_risks", [
    ("普通文章 · 没有任何风险词", 0),
    ("权威认证 + 央视报道", 2),
    ("增长 50% · 用户 100 万 · 超过 10 倍", 3),
])
def test_param_cases(text, expected_min_risks):
    from services.article_fact_check import check_article
    r = check_article(text)
    assert len(r["risks"]) >= expected_min_risks
