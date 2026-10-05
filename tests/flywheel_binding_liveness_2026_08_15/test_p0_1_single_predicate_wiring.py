"""[P0-1 2026-08-15] 结构锁:判据只许有一份,且选集侧与审核侧**都必须接在它上面**。

这里锁的是**接线**不是函数:直接给 `evaluate_candidate_live` 打桩,然后看两条路径是否
都跟着桩走。谁把判定逻辑抄回 `verify_candidate_for_approval`、或者在选集 SQL 里
另写一份 `join mhz_* on is_active`,这两条锁立刻转红 —— 那正是本次 bug 的成因形态。

每条「必须命中」都配一条「必须不命中」:桩改判可通过时也要跟着变,否则锁本身可能恒真。
"""
from __future__ import annotations

import pytest

from conftest import (  # type: ignore[import-not-found]
    ENTITY_KEY,
    INV_BASE,
    seed_candidate,
    seed_entity,
    seed_inventory,
)

import services.media_binding_candidates as mbc
from db.media_entity_flywheel_db import (
    get_media_binding_candidate,
    get_media_entity_for_binding,
    get_media_inventory_item,
    list_recommended_binding_candidates,
)
from services.media_binding_candidates import RECOMMENDED_BINDING_MIN_CONFIDENCE as MINC

STUB_REASON = "STUB_BLOCK_9d1f"


def _stub(block: bool):
    def _fn(entity, inventory, payload=None):
        if block:
            return {"candidate": None, "block_reason": STUB_REASON}
        return {
            "candidate": {
                "can_approve": True, "risk_flags": [], "match_confidence": 0.99,
                "match_method": "domain_exact", "inventory": {},
            },
            "block_reason": "",
        }
    return _fn


def test_review_path_is_wired_to_evaluate_candidate_live(monkeypatch):
    seed_entity()
    seed_inventory(INV_BASE + 101, is_active=True)
    cid = seed_candidate(INV_BASE + 101, can_approve=True)
    row = get_media_binding_candidate(cid)
    entity = get_media_entity_for_binding(row["entity_key"], row["industry_key"])
    inventory = get_media_inventory_item(row["media_source"], int(row["inventory_id"]))

    # 桩说「拦」→ 审核必须抛,且抛的就是桩给的原因(证明原因也是从同一处流出来的)
    monkeypatch.setattr(mbc, "evaluate_candidate_live", _stub(block=True))
    with pytest.raises(ValueError) as exc:
        mbc.verify_candidate_for_approval(entity, inventory, {"inventory": row})
    assert STUB_REASON in str(exc.value), (
        "审核路径没走 evaluate_candidate_live —— 判定被抄回 verify_candidate_for_approval 了"
    )

    # 成对的「必须不命中」:桩说「放行」→ 审核不许抛。只有前半条的锁,恒抛的实现也能骗过。
    monkeypatch.setattr(mbc, "evaluate_candidate_live", _stub(block=False))
    mbc.verify_candidate_for_approval(entity, inventory, {"inventory": row})


def test_selection_path_is_wired_to_the_same_function(monkeypatch):
    seed_entity()
    seed_inventory(INV_BASE + 111, is_active=True)
    cid = seed_candidate(INV_BASE + 111, can_approve=True)

    def picked() -> set[int]:
        return {int(c["id"]) for c in list_recommended_binding_candidates(MINC, limit=5000)
                if c["entity_key"] == ENTITY_KEY}

    # 库存明明在架,只要判据函数说拦,选集就必须空 —— 证明选集读的是这个函数而不是快照列/自写 SQL
    monkeypatch.setattr(mbc, "evaluate_candidate_live", _stub(block=True))
    assert cid not in picked(), (
        "选集没走 evaluate_candidate_live —— 判据被在 SQL 里另写了一份(本 bug 的成因形态)"
    )

    monkeypatch.setattr(mbc, "evaluate_candidate_live", _stub(block=False))
    assert cid in picked()


def test_verify_is_a_thin_wrapper_not_a_second_implementation():
    """再上一道:verify 与选集在同一输入上必须给出同一结论(桩全撤掉,跑真实现)。"""
    seed_entity()
    seed_inventory(INV_BASE + 121, is_active=True)
    seed_inventory(INV_BASE + 122, is_active=False)
    for inv in (INV_BASE + 121, INV_BASE + 122):
        cid = seed_candidate(inv, can_approve=True)
        row = get_media_binding_candidate(cid)
        entity = get_media_entity_for_binding(row["entity_key"], row["industry_key"])
        inventory = get_media_inventory_item(row["media_source"], int(row["inventory_id"]))
        verdict = mbc.evaluate_candidate_live(entity, inventory, {"inventory": row})
        try:
            mbc.verify_candidate_for_approval(entity, inventory, {"inventory": row})
            raised = ""
        except ValueError as exc:
            raised = str(exc)
        assert raised == (verdict["block_reason"] or ""), "verify 与 evaluate 结论不一致 = 两份判据"
