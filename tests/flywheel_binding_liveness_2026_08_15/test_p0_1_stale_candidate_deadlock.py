"""[P0-1 2026-08-15] 陈旧候选死锁:选集与审核判据必须同源。

生产实证(2026-08-15 只读复算):
  「一键通过全部行业的建议通过(4 条)」→「已通过 0 条,4 条未成功」,每次点都一样。
  失败的 4 条 = 候选 20564/20565/20693/20694 → 库存 287108/286119/118846/118848,
  `is_active` 全 = f(供应商已下架);同批成功的 20560~20563 对应库存 `is_active` 全 = t。
  选集读 08-05 写入的 `can_approve` 快照列,审核实时重算 → 两者永不一致。

本模块的每条「必须命中」都配一条成对的「必须不命中」;反向对照(下架→消失→重新上架→再出现)
是防「把那几条写死」的唯一办法。
"""
from __future__ import annotations

import pytest

from conftest import (  # type: ignore[import-not-found]
    ENTITY_KEY,
    INV_BASE,
    seed_candidate,
    seed_entity,
    seed_inventory,
    set_inventory_active,
    snapshot_can_approve,
)

from db.media_entity_flywheel_db import (
    count_recommended_binding_candidates,
    list_media_binding_candidates,
    list_recommended_binding_candidates,
)
from services.media_binding_candidates import RECOMMENDED_BINDING_MIN_CONFIDENCE as MINC


def _recommended_ids() -> set[int]:
    return {int(c["id"]) for c in list_recommended_binding_candidates(MINC, limit=5000)
            if c["entity_key"] == ENTITY_KEY}


def test_offline_inventory_leaves_selection_but_online_one_stays():
    """复现那 4 条的形态,并配成对反向断言。

    必须命中:库存 is_active=f 的候选**不在**「建议通过」里(哪怕快照 can_approve=TRUE)。
    必须不命中:库存 is_active=t 的候选**仍在**里面 —— 否则「修法」只是把所有候选一起藏掉。
    """
    seed_entity()
    seed_inventory(INV_BASE + 1, is_active=False)   # 对应生产 20693/20694 形态(mhz_media 下架)
    seed_inventory(INV_BASE + 2, media_source="mhz_wemedia", is_active=False)  # 20564/20565 形态
    seed_inventory(INV_BASE + 3, is_active=True)    # 对应 20560~20563 形态(在架)
    offline_a = seed_candidate(INV_BASE + 1, can_approve=True)
    offline_b = seed_candidate(INV_BASE + 2, media_source="mhz_wemedia", can_approve=True)
    online = seed_candidate(INV_BASE + 3, can_approve=True)

    # 前提:三条的**快照**都写着可通过 —— 不成立的话下面的断言没有判别力
    assert snapshot_can_approve(offline_a) is True
    assert snapshot_can_approve(offline_b) is True
    assert snapshot_can_approve(online) is True

    picked = _recommended_ids()
    assert offline_a not in picked, "下架库存(mhz_media)的候选仍留在「建议通过」= 死锁未修"
    assert offline_b not in picked, "下架库存(mhz_wemedia)的候选仍留在「建议通过」= 死锁未修"
    assert online in picked, "在架库存的候选也被滤掉 = 修法把选集整体清空,不是判据同源"


def test_reverse_control_toggle_inventory_active():
    """🔴 反向对照:下架 → 候选消失;重新上架 → 候选**必须再出现**。

    只做「消失」那一半的话,把那几条写死也能通过。回得来才证明判据是实时的。
    """
    seed_entity()
    seed_inventory(INV_BASE + 11, is_active=True)
    cid = seed_candidate(INV_BASE + 11, can_approve=True)

    assert cid in _recommended_ids(), "起始态:在架库存的候选应在「建议通过」里"

    set_inventory_active(INV_BASE + 11, False)
    assert cid not in _recommended_ids(), "下架后仍在选集里 = 选集没走实时判据"

    set_inventory_active(INV_BASE + 11, True)
    assert cid in _recommended_ids(), (
        "重新上架后回不来 = 判据被写死/被单向回写钉住,不是实时重算"
    )


def test_count_and_selection_are_one_predicate():
    """计数与选集必须同一份判据 —— 两处各写一遍正是本 bug 的原型。"""
    seed_entity()
    seed_inventory(INV_BASE + 21, is_active=True)
    seed_inventory(INV_BASE + 22, is_active=False)
    seed_candidate(INV_BASE + 21, can_approve=True)
    seed_candidate(INV_BASE + 22, can_approve=True)

    before_count = count_recommended_binding_candidates(MINC)
    before_len = len(list_recommended_binding_candidates(MINC, limit=5000))
    assert before_count == before_len

    set_inventory_active(INV_BASE + 21, False)
    after_count = count_recommended_binding_candidates(MINC)
    after_len = len(list_recommended_binding_candidates(MINC, limit=5000))
    assert after_count == after_len
    assert after_count == before_count - 1, "计数没跟着实时判据走"


def test_review_and_selection_agree_on_the_same_row():
    """同一条候选:选集说不能过 ⇔ 审核也说不能过(反之亦然)。这就是「同源」的可观测定义。"""
    from db.media_entity_flywheel_db import get_media_binding_candidate, get_media_entity_for_binding, get_media_inventory_item
    from services.media_binding_candidates import verify_candidate_for_approval

    seed_entity()
    seed_inventory(INV_BASE + 31, is_active=False)
    cid = seed_candidate(INV_BASE + 31, can_approve=True)
    row = get_media_binding_candidate(cid)
    entity = get_media_entity_for_binding(row["entity_key"], row["industry_key"])
    inventory = get_media_inventory_item(row["media_source"], int(row["inventory_id"]))

    in_selection = cid in _recommended_ids()
    try:
        verify_candidate_for_approval(entity, inventory, {"inventory": row})
        review_ok = True
    except ValueError:
        review_ok = False
    assert in_selection is False and review_ok is False, "下架态:两侧都应判不可通过"

    set_inventory_active(INV_BASE + 31, True)
    inventory = get_media_inventory_item(row["media_source"], int(row["inventory_id"]))
    verify_candidate_for_approval(entity, inventory, {"inventory": row})  # 不抛 = 审核放行
    assert cid in _recommended_ids(), "上架态:两侧都应判可通过"


def test_pending_list_badge_follows_live_verdict_but_approved_history_is_frozen():
    """待审列表也走实时判据(否则前端 isRecommendedBinding 仍读陈旧快照,标签与按钮打架);
    已通过/已驳回是人工决策的历史事实,**不许**被今天的库存状态改写。"""
    from db.connection import get_connection

    seed_entity()
    seed_inventory(INV_BASE + 41, is_active=False)
    seed_inventory(INV_BASE + 42, is_active=False)
    pending_id = seed_candidate(INV_BASE + 41, can_approve=True)
    approved_id = seed_candidate(INV_BASE + 42, can_approve=True)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE geo_media_binding_candidates SET status='approved' WHERE id=%s", (approved_id,))
        conn.commit()
    finally:
        conn.close()

    rows = {int(r["id"]): r for r in list_media_binding_candidates(limit=300)}
    assert rows[pending_id]["can_approve"] is False, "待审行的 can_approve 没跟着实时判据走"
    assert rows[pending_id].get("live_block_reason"), "待审行应带上实时拦截原因供 UI 显示"
    assert rows[approved_id]["can_approve"] is True, (
        "已通过行被今天的库存状态改写 = 审计资产被篡改"
    )
    assert not rows[approved_id].get("live_block_reason")


def test_missing_entity_or_inventory_never_counts_as_approvable():
    """实体/库存整个查不到时,绝不能靠残留快照留在「建议通过」里(旧夹具正是这种形态)。"""
    seed_entity()
    # 库存行根本不种 → get_media_inventory_item 返回 None
    ghost = seed_candidate(INV_BASE + 51, can_approve=True)
    assert ghost not in _recommended_ids()

    rows = {int(r["id"]): r for r in list_media_binding_candidates(limit=300)}
    assert rows[ghost]["can_approve"] is False
    assert "库存" in (rows[ghost].get("live_block_reason") or "")


@pytest.mark.parametrize("price,expect_selected", [(88.0, True), (0.0, False)])
def test_price_condition_is_part_of_the_same_predicate(price, expect_selected):
    """成对断言:可采购判定不只有 is_active —— 价格为 0 同样出局,且走的是同一个函数。
    (若有人只 join 了 is_active 而没走 evaluate_candidate_live,这条会转红。)"""
    seed_entity()
    seed_inventory(INV_BASE + 61, is_active=True, price=price)
    cid = seed_candidate(INV_BASE + 61, can_approve=True)
    assert (cid in _recommended_ids()) is expect_selected
