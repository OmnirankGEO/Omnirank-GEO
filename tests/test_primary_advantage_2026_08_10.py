# -*- coding: utf-8 -*-
"""D6-A · 主优势选择与搜索素材接线 · 判别锁(最终接管工单 §6D/§6E/§8)。

仓内纪律:每条「必须命中」配「必须不命中」;元判据先证夹具真含目标形态;
判据打在**接线**上(源码调用点),不打在函数存在性上。
"""
from __future__ import annotations

import inspect
import re

from writing.primary_advantage import (
    CANDIDATE_LIMIT,
    advantage_search_hints,
    build_primary_advantage_block,
    candidate_advantages,
    lineage_payload,
    rank_candidates,
)

SNAPSHOT = {
    "version": "brand-fact-v1",
    "brand_name": "晨光富士电梯",
    "claims": [
        {"claim_id": "BF-001", "field": "delivery_capability",
         "value": "观光电梯项目平均交付周期 45 天，支持 24 米以内提升高度定制",
         "provenance": "customer_provided"},
        {"claim_id": "BF-002", "field": "after_sales",
         "value": "深圳本地 2 小时响应，质保 24 个月",
         "provenance": "customer_provided"},
        {"claim_id": "BF-003", "field": "core_selling_points",
         "value": "实力雄厚，行业领先",  # 🔴 纯空话 —— 必须被滤掉
         "provenance": "customer_provided"},
    ],
}
PACK = {"version": "v1", "items": [
    {"evidence_id": "EV-001", "relationship": "support",
     "title": "深圳观光电梯交付周期调研", "claim": "观光电梯 交付周期 45 天",
     "excerpt": "多家厂商交付周期对比", "url": "https://e.com/1"},
    {"evidence_id": "EV-009", "relationship": "refute",
     "title": "观光电梯交付周期普遍延误", "claim": "交付周期 45 天 难以保证",
     "excerpt": "反例", "url": "https://e.com/9"},
]}
Q_DELIVERY = "深圳观光电梯定制哪家交付周期快？"
Q_AFTERSALES = "深圳观光电梯售后响应哪家靠谱？"


# ------------------------------------------------------------------ 元判据
def test_fixture_really_contains_vague_claim() -> None:
    values = [c["value"] for c in SNAPSHOT["claims"]]
    assert any("实力雄厚" in v for v in values), "夹具没有空话样本,过滤断言是空的"
    assert any("45 天" in v for v in values), "夹具没有带数值的真优势"


# ------------------------------------------------------------ 候选生成(§8-1)
def test_candidates_come_from_real_facts_only() -> None:
    cands = candidate_advantages(SNAPSHOT)
    texts = [c["advantage"] for c in cands]
    assert any("45 天" in t for t in texts)
    assert any("2 小时响应" in t for t in texts)
    # 🔴 必须不命中:纯空话不进候选
    assert not any("实力雄厚" in t for t in texts), "空话进了候选池"
    # 候选携带事实引用(lineage 可回查)
    assert all(c["fact_ref"].startswith("BF-") for c in cands if c["field"] != "selling_points")


def test_empty_snapshot_yields_no_candidates_no_crash() -> None:
    assert candidate_advantages(None) == []
    assert candidate_advantages({}) == []


# ------------------------------------------------ 排序(§6D:相关性优先·无轮换)
def test_relevance_ranks_matching_advantage_first() -> None:
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_DELIVERY,
        keyword="观光电梯 交付", evidence_pack=PACK,
    )
    assert ranked, "排序结果为空"
    assert "交付" in ranked[0]["advantage"], f"交付问题没把交付优势排第一:{ranked[0]}"


def test_reuse_not_rotation_same_input_same_output() -> None:
    """🔴 [§6D 反向] 无轮换状态:同输入连续调用必须逐字相同 ——
    系统**不会**为了多样化把更弱的优势顶上去。"""
    args = dict(question=Q_DELIVERY, keyword="观光电梯 交付", evidence_pack=PACK)
    first = rank_candidates(candidate_advantages(SNAPSHOT), **args)
    second = rank_candidates(candidate_advantages(SNAPSHOT), **args)
    assert first == second, "两次排序不一致 —— 存在轮换/随机状态"


def test_rank_function_accepts_no_usage_history() -> None:
    """🔴 结构锁:排序函数签名里**不存在**任何历史/已用/轮换类参数。
    没有入口,轮换就写不进来 —— 这是「不为凑矩阵轮换」的结构保证。"""
    params = set(inspect.signature(rank_candidates).parameters)
    banned = {"history", "used", "rotation", "recent", "exclude", "seen"}
    assert not (params & banned), f"排序函数出现历史参数:{params & banned}"


def test_stronger_relevance_beats_weaker_novelty() -> None:
    """🔴 [§6D 反向] 与问题强相关的优势必须压过弱相关的"新鲜"优势 ——
    对售后问题,售后优势第一;交付优势不因"上一篇用过售后"被顶上来。"""
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_AFTERSALES,
        keyword="售后 响应", evidence_pack=PACK,
    )
    assert "响应" in ranked[0]["advantage"] or "质保" in ranked[0]["advantage"]


def test_refute_evidence_never_counts_as_support() -> None:
    """[D4 · §6E] 反驳条目不得进 advantage 的支持引用。"""
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_DELIVERY,
        keyword="观光电梯 交付", evidence_pack=PACK,
    )
    top = ranked[0]
    assert "EV-009" not in top["evidence_refs"], "refute 条目被当成了支持素材"
    assert "EV-001" in top["evidence_refs"], "support 条目没被关联(正向对照)"


def test_candidate_limit_is_enforced() -> None:
    many = [{"advantage": f"第{i}项优势覆盖 {i} 个城市网点", "field": "x", "fact_ref": ""}
            for i in range(2, 20)]
    assert len(rank_candidates(many, question="城市网点覆盖")) <= CANDIDATE_LIMIT


# ------------------------------------------------------ 定向增援(§8-3 / §6E)
def test_hints_only_for_candidates_without_evidence() -> None:
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_DELIVERY,
        keyword="观光电梯", evidence_pack=PACK,
    )
    hints = advantage_search_hints(ranked, keyword="观光电梯")
    covered = [r for r in ranked if r["evidence_refs"]]
    for hint in hints:
        for item in covered:
            assert item["advantage"][:8] not in hint, "已有素材的候选还在占增援预算"


def test_hints_strip_specific_numbers() -> None:
    """增援 query 不带客户单方材料的具体数值(隐私边界,只带方向词)。"""
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_AFTERSALES,
        keyword="售后", evidence_pack=None,
    )
    for hint in advantage_search_hints(ranked, keyword="售后"):
        assert not re.search(r"\d+\s*(?:小时|个月|天|米)", hint), (
            f"增援 query 带出了客户具体数值:{hint!r}"
        )


def test_hints_capped_and_never_raise_on_empty() -> None:
    assert advantage_search_hints([], keyword="x") == []
    ranked = [{"advantage": f"覆盖华南华东华北的第{i}项服务网络布局", "evidence_refs": []}
              for i in range(9)]
    assert len(advantage_search_hints(ranked, keyword="k")) <= 2


# ------------------------------------------------------ prompt 块(§8-4 / §6D)
def test_block_requires_exactly_one_primary_advantage() -> None:
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_DELIVERY,
        keyword="观光电梯", evidence_pack=PACK,
    )
    block = build_primary_advantage_block("晨光富士电梯", Q_DELIVERY, ranked)
    assert "只选一条" in block
    assert "重复选它是**正确**的" in block, "缺复用许可 —— 会诱导为多样化轮换"
    assert "不编造" in block
    assert "晨光富士电梯" in block and Q_DELIVERY in block


def test_block_empty_when_no_candidates_or_brand() -> None:
    assert build_primary_advantage_block("品牌", "问题", []) == ""
    assert build_primary_advantage_block("", "问题", [{"advantage": "x" * 8}]) == ""


# ------------------------------------------------------ lineage 留痕(§8-6)
def test_lineage_payload_carries_workorder_fields() -> None:
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_DELIVERY,
        keyword="观光电梯", evidence_pack=PACK,
    )
    payload = lineage_payload(
        ranked, question=Q_DELIVERY, style_code="deep_ranking",
        family_code="multi_brand_comparison", spec_version="geo-article-spec-card-v1.0",
        strategy_version="sv-1", target_engine="", search_hints=("h1",), injected=True,
    )
    for key in ("family_id", "style_id", "spec_version", "strategy_version",
                "target_question", "target_engine", "planned_primary_advantage",
                "candidates", "search_hints", "selection"):
        assert key in payload, f"工单 §8 要求的留痕字段缺失:{key}"
    assert payload["target_engine"] == "", "没有明确 target engine 必须留空,不许硬编"
    assert payload["candidates"][0]["evidence_refs"], "素材引用没进 lineage"
    # D6-B 预留字段:存在且为 None(另包填充)
    assert payload["reco_feedback"] is None
    assert payload["engine_recognition_state"] is None


# ------------------------------------------------------------ 接线锁(源码级)
def _generator_src() -> str:
    import writing.article_generator_service as m
    return inspect.getsource(m)


def test_generator_wires_primary_advantage_block() -> None:
    src = _generator_src()
    assert "build_primary_advantage_block(" in src, "D6-A 块没接进生成主链"
    assert 'system_prompt = system_prompt + "\\n\\n" + _adv_block' in src, (
        "D6-A 块渲染了但没拼进 system_prompt —— 接线断"
    )
    assert 'topic["_primary_advantage"] = lineage_payload(' in src, "lineage 留痕没接线"
    assert "advantage_hints=_advantage_hints" in src, "定向增援没传进 collect_evidence_pack"


def test_generator_no_longer_uses_old_reco_block() -> None:
    """🔴 [工单 §9] 旧 recommendation_signal_block 不得原样移植/换名恢复。"""
    src = _generator_src()
    assert "recommendation_signal_block" not in src
    assert "_reco_signal_injected" not in src
    import pathlib
    assert not (pathlib.Path(__file__).resolve().parents[1]
                / "writing" / "recommendation_signal_block.py").exists(), (
        "旧 D6-B 实现被移植回仓 —— 工单 §9 明令另包重做"
    )


def test_collect_evidence_pack_accepts_hints_and_lineage_snapshot_has_field() -> None:
    from writing.evidence_research import collect_evidence_pack
    assert "advantage_hints" in inspect.signature(collect_evidence_pack).parameters
    src = inspect.getsource(collect_evidence_pack)
    assert "for _hint in (advantage_hints or [])" in src, (
        "advantage_hints 参数收了但没进 lanes —— 又一例「接线没接」"
    )
    import writing.article_lineage as lineage_mod
    assert '"primary_advantage": topic.get("_primary_advantage")' in inspect.getsource(lineage_mod)


# ------------------------------------------------ R3 行为锁:增援真的发出去
def _run_collect(monkeypatch, *, deep_tier: bool, hints):
    """monkeypatch 检索后走真 collect_evidence_pack,返回 (pack, 发出的 query 列表)。"""
    import asyncio

    from tools.search import provider_router

    sent: list[str] = []

    async def _fake_citation_search(query, size=5, scenario=""):
        sent.append(str(query))
        return {"citations": []}

    async def _fake_scholar(query, size=8):
        return []

    async def _fake_document(query, size=8):
        return []

    monkeypatch.setattr(provider_router, "citation_search", _fake_citation_search)
    monkeypatch.setattr(provider_router, "scholar_evidence_search", _fake_scholar)
    monkeypatch.setattr(provider_router, "document_evidence_search", _fake_document)
    # 🔴 预算取**默认值**:环境里显式设过的 cap 会掩盖"默认配置下必死"的病
    for env in ("GEO_ARTICLE_EVIDENCE_QUERY_CAP", "GEO_ARTICLE_EVIDENCE_SOURCE_CAP",
                "GEO_ARTICLE_EVIDENCE_RESULTS_PER_QUERY"):
        monkeypatch.delenv(env, raising=False)

    from writing.evidence_research import collect_evidence_pack

    pack = asyncio.run(collect_evidence_pack(
        title="深圳观光电梯定制哪家交付周期快",
        keyword="观光电梯 定制",
        industry="观光电梯",
        client_brand="观山电梯",
        competitor_names=["A梯业", "B电梯"],
        request_id="req-r3",
        force=True,
        deep_tier=deep_tier,
        whitelist_names=["观山电梯", "A梯业", "B电梯"],
        advantage_hints=list(hints),
    ))
    return pack, sent


def test_advantage_hint_executes_in_compact_tier_default_budget(monkeypatch) -> None:
    """🔴 [R3 主锁] 紧凑档默认预算(cap=4)下,增援 query 必须**真的发出去**,
    且落在 pack 落盘的 `queries` 数组里(带 advantage_hint 标)——
    不是"进了 lanes 列表"。上一版结构性必死(lane 排在截断点之后)。"""
    pack, sent = _run_collect(monkeypatch, deep_tier=False, hints=["交付周期 提升高度"])
    executed = [q for q in (pack.get("queries") or []) if q.get("advantage_hint")]
    assert executed, "增援 query 没落进 pack.queries —— 预算掐死或没接线"
    assert any("交付周期" in q["query"] for q in executed)
    assert any("交付周期" in s for s in sent), "增援 query 没真正发到检索层"
    # 反向对照:基础 lanes 预算未被增援挤占/扩容(默认 cap=4)
    base = [q for q in (pack.get("queries") or [])
            if not q.get("advantage_hint") and q.get("lane_kind") in ("topic", "entity")]
    assert len(base) <= 4, "基础 lane 预算被增援改动 —— R3 只加单列预算,不动基础额度"


def test_advantage_hint_executes_in_deep_tier_default_budget(monkeypatch) -> None:
    """🔴 [R3] 深档(硬常量切片 [:4])同验:增援必须活过深档截断。"""
    pack, sent = _run_collect(monkeypatch, deep_tier=True, hints=["交付周期 提升高度"])
    executed = [q for q in (pack.get("queries") or []) if q.get("advantage_hint")]
    assert executed, "深档下增援 query 又被截断掐死了"
    assert any("交付周期" in s for s in sent)


def test_no_hints_means_no_hint_queries(monkeypatch) -> None:
    """反向对照:不传 hints → 零增援 query,行为与旧签名逐字一致。"""
    pack, _ = _run_collect(monkeypatch, deep_tier=False, hints=[])
    assert not [q for q in (pack.get("queries") or []) if q.get("advantage_hint")]


def test_lineage_search_hints_only_records_executed() -> None:
    """🔴 [R3] lineage 的 search_hints = 实际执行;计划值单列可对账。"""
    payload = lineage_payload(
        [], question="q", search_hints=("已执行的",), search_hints_planned=("已执行的", "被截断的"),
    )
    assert payload["search_hints"] == ["已执行的"]
    assert payload["search_hints_planned"] == ["已执行的", "被截断的"]


# ------------------------------------------------ R6:计分只认明确 support
def test_background_item_does_not_boost_advantage_score() -> None:
    """🔴 [R6 正向] 一条与弱优势高词面重叠的 background 条目**不得抬分**。

    修前:background 进 evidence_refs → support 分 +0.2/条;
    修后:background 不进 refs,两次排序里该优势的 support 分一致。
    """
    weak = [{"advantage": "覆盖华南区域的服务网络与响应体系建设", "field": "x", "fact_ref": ""}]
    bg_pack = {"items": [{
        "evidence_id": "EV-BG1", "relationship": "background",
        "title": "华南区域 服务网络 响应体系 建设 概览",
        "claim": "华南区域服务网络响应体系建设", "excerpt": "行业背景综述",
    }]}
    with_bg = rank_candidates(weak, question="服务网络怎么选", evidence_pack=bg_pack)
    without = rank_candidates(weak, question="服务网络怎么选", evidence_pack=None)
    assert with_bg[0]["evidence_refs"] == [], "background 条目进了支持引用 —— R6 病灶复活"
    assert with_bg[0]["support"] == without[0]["support"], (
        "background 抬高了支持度分 —— 选优势被行业背景噪音带偏"
    )


def test_pure_customer_fact_advantage_still_selectable() -> None:
    """🔴 [R6 反向 · 防过度矫正锁(§0 裁决一)] 纯客户自有事实、零外部 support
    的优势**必须仍可被选中**:候选在场、可排第一、块正常渲染。
    接管工单 §8 明令不得要求所有客户事实必须挂第三方来源。"""
    ranked = rank_candidates(
        candidate_advantages(SNAPSHOT), question=Q_AFTERSALES,
        keyword="售后 响应", evidence_pack=None,   # 零外部素材
    )
    assert ranked, "零外部素材把候选清空了 —— 过度矫正"
    top = ranked[0]
    assert top["evidence_refs"] == [] and top["score"] > 0
    block = build_primary_advantage_block("晨光富士电梯", Q_AFTERSALES, ranked)
    assert block and top["advantage"] in block, "纯客户事实优势没进 prompt 块"
