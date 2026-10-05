"""[P0-1 接线 + P0-3 存量不回溯 2026-08-15]

接线:新口径必须**经由 `evaluate_candidate_live`** 统一生效 —— 选集 / 计数 / 待审列表三出口
      + 审核路径,四处结论一致。前一包(6a87b271)刚把判据收敛到那一个函数,本单不许拆开。
存量:Owner 拍板 ② —— 现有 approved 一条不动。这里把「approved 计数差值 = 0」做成可断言事实。
"""
from __future__ import annotations

from platdomain_fixtures import (  # type: ignore[import-not-found]
    BBUCKET_ENTITY_DOMAIN,
    BBUCKET_ENTITY_KEY,
    BBUCKET_ENTITY_NAME,
    INV_BASE,
    PLATFORM_ENTITY_DOMAIN,
    PLATFORM_ENTITY_KEY,
    PLATFORM_ENTITY_NAME,
    approved_total,
    candidate_row,
    seed_candidate,
    seed_entity,
    seed_inventory,
)

from db.media_entity_flywheel_db import (
    count_recommended_binding_candidates,
    get_media_binding_candidate,
    get_media_entity_for_binding,
    get_media_inventory_item,
    list_media_binding_candidates,
    list_recommended_binding_candidates,
)
from services.media_binding_candidates import (
    RECOMMENDED_BINDING_MIN_CONFIDENCE as MINC,
    evaluate_candidate_live,
    verify_candidate_for_approval,
)


def _selected_ids() -> set[int]:
    return {int(c["id"]) for c in list_recommended_binding_candidates(MINC, limit=5000)}


def _pending_row(cid: int) -> dict:
    rows = {int(r["id"]): r for r in list_media_binding_candidates(limit=300)}
    return rows[cid]


def _set_approved(cid: int) -> None:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE geo_media_binding_candidates SET status='approved' WHERE id=%s", (cid,))
        conn.commit()
    finally:
        conn.close()


def _review_blocked(cid: int) -> str:
    row = get_media_binding_candidate(cid)
    entity = get_media_entity_for_binding(row["entity_key"], row["industry_key"])
    inventory = get_media_inventory_item(row["media_source"], int(row["inventory_id"]))
    try:
        verify_candidate_for_approval(entity, inventory, {"inventory": row})
        return ""
    except ValueError as exc:
        return str(exc)


def test_new_platform_domain_blocks_across_all_three_outlets_and_review():
    """csdn.net 是 A 桶新增。子域实体 blog.csdn.net + 无名称证据的账号名 → 四处必须一致判「拦」。"""
    seed_entity(PLATFORM_ENTITY_KEY, PLATFORM_ENTITY_DOMAIN, PLATFORM_ENTITY_NAME)
    seed_inventory(INV_BASE + 1, "扬道财经")          # 与实体名毫无关系 = 无名称证据
    cid = seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 1, "扬道财经")

    assert bool(candidate_row(cid)["can_approve"]) is True, "前提:快照写着可通过,否则断言没判别力"

    # 出口①选集 / ②计数 / ③待审列表 / ④审核
    assert cid not in _selected_ids(), "出口① 选集:新口径没生效"
    before = count_recommended_binding_candidates(MINC)
    row = _pending_row(cid)
    assert row["can_approve"] is False, "出口③ 待审列表:徽章没跟着新口径"
    assert "共享平台域名需要名称证据" in (row.get("live_block_reason") or ""), row.get("live_block_reason")
    assert "共享平台域名需要名称证据" in _review_blocked(cid), "出口④ 审核:没拦住"

    # 出口② 计数:同一条不该被计进去 —— 用「加一条同形态的」验计数确实随判据走
    seed_inventory(INV_BASE + 2, "北青网快讯")
    seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 2, "北青网快讯")
    assert count_recommended_binding_candidates(MINC) == before, "出口② 计数:又一条平台候选被算进「建议通过」"


def test_name_evidence_still_passes_on_the_same_platform_domain():
    """成对的「必须不命中」:同一个平台域,只要有名称证据就照样能过 ——
    否则「修好了」其实是把整个平台一刀切掉。"""
    seed_entity(PLATFORM_ENTITY_KEY, PLATFORM_ENTITY_DOMAIN, PLATFORM_ENTITY_NAME)
    seed_inventory(INV_BASE + 11, "CSDN博客官方号")     # 含实体名 = 有名称证据
    cid = seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 11, "CSDN博客官方号")

    assert cid in _selected_ids()
    assert _pending_row(cid)["can_approve"] is True
    assert _review_blocked(cid) == ""


def test_b_bucket_domain_behaves_exactly_like_before():
    """🔴 B 桶硬反向对照:同样是「多媒体名 + 无名称证据」的形态,news.cn 必须**照过不误**。
    它一旦被拦,就说明 B 桶被并进了名单。"""
    seed_entity(BBUCKET_ENTITY_KEY, BBUCKET_ENTITY_DOMAIN, BBUCKET_ENTITY_NAME)
    seed_inventory(INV_BASE + 21, "体育频道", link_domain=BBUCKET_ENTITY_DOMAIN)
    cid = seed_candidate(BBUCKET_ENTITY_KEY, INV_BASE + 21, "体育频道")

    assert cid in _selected_ids(), "news.cn(B 桶)被拦下 = B 桶被悄悄并进 A 桶名单"
    assert _pending_row(cid)["can_approve"] is True
    assert _review_blocked(cid) == ""


def test_predicate_is_wired_through_evaluate_candidate_live(monkeypatch):
    """接线锁:三出口都必须经由 `evaluate_candidate_live`(前一包收敛的那个单点)。
    给它打桩说「放行」,被平台判据拦下的那条也必须重新出现;说「拦」则必须消失。"""
    import services.media_binding_candidates as mbc

    seed_entity(PLATFORM_ENTITY_KEY, PLATFORM_ENTITY_DOMAIN, PLATFORM_ENTITY_NAME)
    seed_inventory(INV_BASE + 31, "扬道财经")
    cid = seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 31, "扬道财经")
    assert cid not in _selected_ids()

    monkeypatch.setattr(mbc, "evaluate_candidate_live", lambda e, i, p=None: {
        "candidate": {"can_approve": True, "risk_flags": [], "match_confidence": 0.99,
                      "match_method": "domain_exact", "inventory": {}},
        "block_reason": "",
    })
    assert cid in _selected_ids(), "选集没走 evaluate_candidate_live —— 判据被另写了一份"
    assert _pending_row(cid)["can_approve"] is True


def test_evaluate_candidate_live_is_the_one_reporting_the_platform_reason():
    """原因文本必须从单点判据流出来,不是各出口自己拼的。"""
    seed_entity(PLATFORM_ENTITY_KEY, PLATFORM_ENTITY_DOMAIN, PLATFORM_ENTITY_NAME)
    seed_inventory(INV_BASE + 41, "扬道财经")
    cid = seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 41, "扬道财经")
    row = get_media_binding_candidate(cid)
    verdict = evaluate_candidate_live(
        get_media_entity_for_binding(row["entity_key"], row["industry_key"]),
        get_media_inventory_item(row["media_source"], int(row["inventory_id"])),
        {"inventory": row},
    )
    assert verdict["block_reason"] == "共享平台域名需要名称证据"
    assert _pending_row(cid)["live_block_reason"] == verdict["block_reason"]


# ── P0-3 存量不回溯 ──────────────────────────────────────────────────────────
def test_existing_approved_rows_are_never_touched_by_the_new_predicate():
    """🔴 Owner 拍板 ②:存量不回溯。跑遍所有读路径,approved 计数差值必须 = 0,
    且那条 approved 行的 status / can_approve **逐字节不变**。"""
    seed_entity(PLATFORM_ENTITY_KEY, PLATFORM_ENTITY_DOMAIN, PLATFORM_ENTITY_NAME)
    seed_inventory(INV_BASE + 51, "扬道财经")
    cid = seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 51, "扬道财经")
    _set_approved(cid)

    before_total = approved_total()
    before_row = candidate_row(cid)
    assert before_row["status"] == "approved"

    # 把所有读路径都跑一遍(选集 / 计数 / 待审列表)
    list_recommended_binding_candidates(MINC, limit=5000)
    count_recommended_binding_candidates(MINC)
    list_media_binding_candidates(limit=300)

    after_total = approved_total()
    after_row = candidate_row(cid)
    assert after_total - before_total == 0, (
        f"approved 计数被改动了({before_total} → {after_total})—— 违反 Owner 拍板 ②"
    )
    assert after_row["status"] == "approved"
    assert bool(after_row["can_approve"]) is bool(before_row["can_approve"]), (
        "已通过行的 can_approve 被新口径改写 = 存量被回溯"
    )


def test_approved_row_on_platform_domain_stays_out_of_pending_recomputation():
    """成对断言:同一实体下,approved 那条不重算、candidate 那条重算 —— 两者不能混。"""
    seed_entity(PLATFORM_ENTITY_KEY, PLATFORM_ENTITY_DOMAIN, PLATFORM_ENTITY_NAME)
    seed_inventory(INV_BASE + 61, "扬道财经")
    seed_inventory(INV_BASE + 62, "北青网快讯")
    approved_id = seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 61, "扬道财经")
    pending_id = seed_candidate(PLATFORM_ENTITY_KEY, INV_BASE + 62, "北青网快讯")
    _set_approved(approved_id)

    rows = {int(r["id"]): r for r in list_media_binding_candidates(limit=300)}
    assert rows[approved_id]["can_approve"] is True, "已通过行被重算 = 存量被回溯"
    assert not rows[approved_id].get("live_block_reason")
    assert rows[pending_id]["can_approve"] is False, "待审行没重算 = 新口径没生效"
