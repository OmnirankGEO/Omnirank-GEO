"""[P0-2 2026-08-15] 失败原因必须能被看见 —— 后端透传这一半。

前端那一半(可折叠明细 + toast 分档)由 Playwright 打真渲染断言,不在这里 grep 源码。

修前:批量端点只回 `{candidate_id, error}`。一键通过是**跨行业全库选集**,前端列表只有 300 条,
拿 candidate_id 映射不回媒体名 → 原因事实上无法展示,只剩「4 条未成功」。
"""
from __future__ import annotations

from conftest import (  # type: ignore[import-not-found]
    INV_BASE,
    seed_candidate,
    seed_entity,
    seed_inventory,
)

from api.media_entity_flywheel_api import _binding_failure_entry, _review_binding_candidate_core
from fastapi import HTTPException


def _make_offline_candidate() -> int:
    seed_entity()
    seed_inventory(INV_BASE + 201, is_active=False, name="下架了的测试媒体")
    return seed_candidate(INV_BASE + 201, can_approve=True, name="下架了的测试媒体")


def test_review_core_still_blocks_offline_inventory_with_a_readable_reason():
    """红线:不许为了让计数归零而放宽 verify —— 下架媒体照样必须被拦,且原因说人话。"""
    cid = _make_offline_candidate()
    try:
        _review_binding_candidate_core(cid, "approve", "测试", operator_id=None)
        raise AssertionError("下架库存竟然通过了审核 = verify 被放宽了(红线)")
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "库存不可采购" in str(exc.detail), f"原因不可读:{exc.detail}"


def test_failure_entry_carries_media_name_and_reason():
    """失败项必须自带媒体名 —— 否则前端只能显示一串 id,等于原因还是丢了。"""
    cid = _make_offline_candidate()
    try:
        _review_binding_candidate_core(cid, "approve", "测试", operator_id=None)
        raise AssertionError("应当被拦")
    except HTTPException as exc:
        entry = _binding_failure_entry(cid, str(exc.detail))

    assert entry["candidate_id"] == cid
    assert entry["media_name"] == "下架了的测试媒体", "失败项没带媒体名(前端映射不回名字)"
    assert "库存不可采购" in entry["error"]
    assert entry["industry_key"], "失败项没带行业(跨行业批量时无法定位)"


def test_failure_entry_never_invents_a_name_when_row_is_gone():
    """成对的「必须不命中」:候选行不存在时不许编名字,只留空串让前端退回显示 id。"""
    entry = _binding_failure_entry(-12345, "ValueError")
    assert entry["candidate_id"] == -12345
    assert entry["media_name"] == ""
    assert entry["error"] == "ValueError"


def test_name_lookup_is_fail_soft(monkeypatch):
    """取名字只是展示用。它挂了必须退化成空名字,**不许**把「部分失败」升级成整批异常 ——
    否则批量端点凭空多了一个必须可用的依赖,失败原因反而更看不见了。"""
    import api.media_entity_flywheel_api as api_mod

    def boom(_cid):
        raise RuntimeError("db down")

    monkeypatch.setattr(api_mod, "get_media_binding_candidate", boom)
    entry = api_mod._binding_failure_entry(4321, "库存不可采购")
    assert entry["error"] == "库存不可采购", "原因必须原样保住"
    assert entry["media_name"] == ""


def test_approve_all_response_reports_failures_with_names(monkeypatch):
    """端到端形状:一键通过的响应里 failed[] 必须是「带名字+原因」的形状。"""
    import api.media_entity_flywheel_api as api_mod

    cid = _make_offline_candidate()
    seed_inventory(INV_BASE + 202, is_active=True, name="在架的测试媒体")
    ok_id = seed_candidate(INV_BASE + 202, can_approve=True, name="在架的测试媒体")

    scan = api_mod.recommended_binding_candidates_scan(
        api_mod.RECOMMENDED_BINDING_MIN_CONFIDENCE, limit=5000)
    picked = {int(c["id"]) for c in scan["items"]}
    # 下架那条根本不该进选集(P0-1 已修);在架那条必须进 —— 成对断言
    assert cid not in picked
    assert ok_id in picked

    # 直接验失败项形状:把一条下架候选强行送进审核核心,模拟「选集之后库存刚被下架」的竞态
    try:
        api_mod._review_binding_candidate_core(cid, "approve", "测试", operator_id=None)
        failures = []
    except HTTPException as exc:
        failures = [api_mod._binding_failure_entry(cid, str(exc.detail))]
    assert failures and set(failures[0]) >= {
        "candidate_id", "error", "media_name", "entity_key", "industry_key", "media_source",
    }, "failed[] 形状不含媒体名/行业 = 前端仍然只能显示计数"
