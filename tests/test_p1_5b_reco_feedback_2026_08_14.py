# -*- coding: utf-8 -*-
"""P1-5b · D6-B 推荐结果回流 · 判别锁(2026-08-14)。

变异点:
  M1 拆样本闸(不足也给 feedback)→ test_lineage_feedback_insufficient_none 红;
  M2 拆限定语(coverage_note 删「已配对子样本」)→ test_coverage_note_qualifier 红;
  M3 拆三消费点接线 → test_three_consumption_points_wired 红;
  M4 拆空榜诚实(0 被引也 available=True)→ test_media_feedback_insufficient_honest 红。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import services.reco_outcome_feedback as rof
from writing.primary_advantage import lineage_payload

ROOT = Path(__file__).resolve().parents[1]


def _agg(total: int, families=None, citations=None) -> dict:
    return {
        "available": True, "version": rof.D6B_VERSION, "window_days": 90,
        "min_sample": 30, "total_observations": total,
        "families": families or [], "citations": citations or [],
        "coverage_note": "口径:仅已配对子样本(target_outcome 有效解析/发布桥配对的行),不代表全量监测面;覆盖率随 P0-3 回填提升。",
    }


def test_lineage_feedback_insufficient_none(monkeypatch) -> None:
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(10))
    assert rof.reco_feedback_for_lineage(662) is None, "样本不足必须诚实 None,不硬凑"


def test_lineage_feedback_sufficient(monkeypatch) -> None:
    # 反向对照:样本充足 → 按引擎聚合(供 engine_recognition_state)
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(40, families=[
                            {"question_family": "a", "provider": "doubao",
                             "observations": 30, "recommended": 6, "mentioned": 0, "not_mentioned": 20},
                            {"question_family": "b", "provider": "doubao",
                             "observations": 10, "recommended": 1, "mentioned": 0, "not_mentioned": 9},
                        ]))
    feedback = rof.reco_feedback_for_lineage(662)
    assert feedback is not None
    assert feedback["engine_recognition_state"]["doubao"] == {"observations": 40, "recommended": 7}
    assert "已配对子样本" in feedback["coverage_note"]


def _citations(total: int, domains: int = 2, providers: int = 2) -> list[dict]:
    """构造 citations 行:total 次被引摊到 domains×providers 网格上(确定性)。"""
    rows = []
    grid = [(f"d{i}.example.com", f"prov{j}") for i in range(domains) for j in range(providers)]
    for idx, count in enumerate([total // len(grid)] * len(grid)):
        rows.append({"question_family": "f", "provider": grid[idx][1],
                     "publish_domain": grid[idx][0], "citations": count})
    remainder = total - sum(r["citations"] for r in rows)
    if remainder:
        rows[0]["citations"] += remainder
    return [r for r in rows if r["citations"] > 0] or rows[:1]


def test_media_feedback_insufficient_honest(monkeypatch) -> None:
    """🔴 R2-6 §0① 反例封死:4 次被引 ≠ 0 也**不许** available(旧口径只判 ==0)。"""
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(100, citations=_citations(4, 1, 1)))
    result = rof.brand_media_feedback(662)
    assert result["available"] is False and result["reason"] == "insufficient_sample"
    assert result["citation_total"] == 4 and result["min_sample"] == 30, "「4/30 暂作观察」的计数没带回"


@pytest.mark.parametrize("total,expect_available", [
    (0, False), (1, False), (29, False), (30, True), (31, True),
])
def test_media_feedback_min_sample_boundaries(monkeypatch, total, expect_available) -> None:
    """R2-6 边界 0/1/29/30/31(域名/引擎多样性满足时,只由样本数定门)。"""
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(100, citations=_citations(total, 2, 2)))
    result = rof.brand_media_feedback(662)
    assert result["available"] is expect_available, f"total={total}: {result}"


def test_media_feedback_diversity_gate(monkeypatch) -> None:
    """R2-6:样本够但**单域名/单引擎**照样不可用(生产反例:各 1 域名)。"""
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(100, citations=_citations(40, 1, 2)))
    assert rof.brand_media_feedback(662)["available"] is False, "单域名 40 次被引仍给了续投结论"
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(100, citations=_citations(40, 2, 1)))
    assert rof.brand_media_feedback(662)["available"] is False, "单引擎 40 次被引仍给了续投结论"
    # 反向对照:2 域 × 2 引擎 × 40 次 → 可用(门槛不是恒关)
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(100, citations=_citations(40, 2, 2)))
    assert rof.brand_media_feedback(662)["available"] is True


def test_media_feedback_ranks_domains(monkeypatch) -> None:
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(100, citations=[
                            {"question_family": "a", "provider": "doubao",
                             "publish_domain": "sohu.com", "citations": 14},
                            {"question_family": "b", "provider": "kimi",
                             "publish_domain": "cnblogs.com", "citations": 16},
                        ]))
    result = rof.brand_media_feedback(662)
    assert result["available"] is True
    assert [d["domain"] for d in result["domains"]] == ["cnblogs.com", "sohu.com"]
    assert "已配对子样本" in result["coverage_note"]


@pytest.mark.parametrize("total,expect_none", [
    (0, True), (1, True), (29, True), (30, False), (31, False),
])
def test_lineage_feedback_min_sample_boundaries(monkeypatch, total, expect_none) -> None:
    """R2-6 边界:lineage 出口同一道闸(0/1/29/30/31)。"""
    fams = [{"question_family": "a", "provider": "doubao", "observations": total,
             "recommended": 0, "mentioned": 0, "not_mentioned": total}]
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(total, families=fams))
    result = rof.reco_feedback_for_lineage(662)
    assert (result is None) is expect_none, f"total={total}: {result}"


@pytest.mark.parametrize("total,expect_empty", [
    (0, True), (1, True), (29, True), (30, False), (31, False),
])
def test_gap_block_min_sample_boundaries(monkeypatch, total, expect_empty) -> None:
    """R2-6 边界:标题缺口出口同一道闸(30 起才可能出块,且要有可下结论的族)。"""
    fams = [{"question_family": "价格对比", "provider": "doubao", "observations": total,
             "recommended": 0, "mentioned": 0, "not_mentioned": total}]
    monkeypatch.setattr(rof, "aggregate_brand_outcomes",
                        lambda brand_id, window_days=90: _agg(total, families=fams))
    block = rof.build_title_gap_block(662)
    assert (block == "") is expect_empty, f"total={total}: {block[:80]!r}"


def test_consumer_enumeration_lock() -> None:
    """🔴 R2-6 最重判据:**枚举全部消费点**,逐个证明过闸。

    仓内 import 本模块的生产文件必须恰好是已知三消费点(+测试);出现新消费者
    本锁转红 → 强制把新出口纳入闸审。三出口的闸(min_sample)都在本模块内
    (build_title_gap_block/reco_feedback_for_lineage 判 total_observations,
    brand_media_feedback 判 citation_total+多样性),消费方拿到的只有
    「空块 / None / available=False」三种降级形态,无法绕闸取数。"""
    import subprocess

    out = subprocess.run(
        ["git", "grep", "-l", "reco_outcome_feedback import", "--",
         "*.py", ":!tests/*", ":!scripts/*"],
        cwd=str(ROOT), capture_output=True, text=True,
    ).stdout.split()
    expected = {
        "services/placement_service.py",
        "writing/article_generator_service.py",
        "writing/keyword_topic_generator.py",
    }
    assert set(out) == expected, (
        f"消费点集合漂移: {sorted(set(out) ^ expected)} —— 新消费者必须先过闸审再解锁"
    )
    src = (ROOT / "services/reco_outcome_feedback.py").read_text(encoding="utf-8")
    for fn in ("def build_title_gap_block", "def reco_feedback_for_lineage",
               "def brand_media_feedback"):
        body = src[src.index(fn): src.index(fn) + 3000]
        assert "min_sample" in body, f"{fn} 内没有 min_sample 闸(闸建了≠闸盖全了)"


def test_coverage_note_qualifier() -> None:
    """红线桥限定语(工单红线 6):聚合产物自带限定,任何引用点不许剥离。"""
    base = rof.aggregate_brand_outcomes(0)  # brand_id=0 → 不打库,返回 base 形状
    assert "已配对子样本" in base["coverage_note"]


def test_min_sample_configurable(monkeypatch) -> None:
    monkeypatch.setenv("GEO_D6B_MIN_SAMPLE", "7")
    assert rof.min_sample() == 7
    monkeypatch.setenv("GEO_D6B_MIN_SAMPLE", "not-a-number")
    assert rof.min_sample() == 30  # 坏配置回默认,不炸


def test_lineage_payload_accepts_feedback() -> None:
    payload = lineage_payload([], question="q", reco_feedback={
        "version": "x", "engine_recognition_state": {"doubao": {"observations": 40, "recommended": 7}},
    })
    assert payload["reco_feedback"]["version"] == "x"
    assert payload["engine_recognition_state"] == {"doubao": {"observations": 40, "recommended": 7}}
    # 反向对照:不传 → 双 None(与预留期行为一致)
    legacy = lineage_payload([], question="q")
    assert legacy["reco_feedback"] is None and legacy["engine_recognition_state"] is None


def test_three_consumption_points_wired() -> None:
    """接线锁:三个消费点(标题缺口 / lineage / 媒体面)都真调了本服务。"""
    ktg = (ROOT / "writing/keyword_topic_generator.py").read_text(encoding="utf-8")
    assert "build_title_gap_block" in ktg, "消费点 1(标题缺口)没接"
    ags = (ROOT / "writing/article_generator_service.py").read_text(encoding="utf-8")
    assert "reco_feedback_for_lineage" in ags and "reco_feedback=_reco_feedback" in ags, (
        "消费点 2(lineage 预留字段)没接"
    )
    ps = (ROOT / "services/placement_service.py").read_text(encoding="utf-8")
    assert "brand_media_feedback" in ps, "消费点 3(发布推荐面)没接"
    papi = (ROOT / "api/publish_api.py").read_text(encoding="utf-8")
    assert '"brand_reco_feedback"' in papi, "发布端点没透出 brand_reco_feedback"
    pc = (ROOT / "frontend/src/pages/Publishing/PublishCenter.tsx").read_text(encoding="utf-8")
    assert "brand_reco_feedback" in pc and "暂无足够样本" in pc, "前端没渲染/没有不足样本诚实态"
