"""
判别性回归测试 · GEO-R2-CAN-011 · services/freeze_sweeper.py

source-inspection 判别锁:断言 freeze_sweeper.py 中修复标志存在。
不 import server.py / 不依赖 DB。回退到 fail-open 版本则本测试失败。

修复要点:排除查询(geo_research_selfserve_queue)失败时,必须 fail-closed —
  引入 exclusion_lookup_ok 标记,失败即置 False,并在其为 False 时保守剔除
  所有 selfres_ 冻结(startswith('selfres_')),绝不 release。
"""
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_SRC = (
    Path(__file__).resolve().parents[2] / "services" / "freeze_sweeper.py"
).read_text(encoding="utf-8")


def test_exclusion_lookup_ok_flag_defined_true():
    # 排除查询前定义成功标记(默认 True)
    assert re.search(r"exclusion_lookup_ok\s*=\s*True", _SRC), (
        "缺 exclusion_lookup_ok=True 初始化 · fail-closed 标记未引入"
    )


def test_exclusion_except_sets_fail_closed():
    # 排除查询的 except 分支必须把标记置 False(fail-closed)
    assert re.search(r"exclusion_lookup_ok\s*=\s*False", _SRC), (
        "排除查询失败未置 exclusion_lookup_ok=False · 仍是 fail-open"
    )


def test_fail_closed_branch_drops_selfres_freezes():
    # fail-closed 分支:标记为 False 时剔除所有 selfres_ 冻结
    assert re.search(r"if\s+not\s+exclusion_lookup_ok\s*:", _SRC), (
        "缺 `if not exclusion_lookup_ok:` fail-closed 分支"
    )
    # 该分支通过 startswith('selfres_') 保守剔除
    assert "startswith(\"selfres_\")" in _SRC or "startswith('selfres_')" in _SRC, (
        "fail-closed 分支未按 selfres_ 前缀保守剔除冻结"
    )


def test_marker_present():
    assert "[GEO-R2-CAN-011]" in _SRC, "缺 [GEO-R2-CAN-011] 修复注释标记"


def test_fail_open_original_form_removed():
    # 原 fail-open:排除后直接无条件 `if active_selfserve_refs:` 独立判断,
    #   现应被 fail-closed 分支包成 `elif active_selfserve_refs:`。
    #   断言不再存在裸的顶层 `if active_selfserve_refs:`(仅 elif 形式)。
    assert not re.search(r"\n        if active_selfserve_refs:", _SRC), (
        "仍存在裸 `if active_selfserve_refs:` · fail-open 结构未收敛为 elif"
    )
    assert re.search(r"elif\s+active_selfserve_refs\s*:", _SRC), (
        "缺 `elif active_selfserve_refs:` · fail-closed 优先分支未接管"
    )


def test_dispatched_monitoring_freezes_are_durably_excluded():
    assert "settlement_reference" in _SRC
    assert "provider_dispatched_at IS NOT NULL" in _SRC
    assert "protected_monitoring_refs" in _SRC
    assert "monitoring_lookup_ok = False" in _SRC
    for prefix in ("monitor_stream_", "monitor_run_", "batch_mon_", "sched_mon_"):
        assert prefix in _SRC


def test_protected_money_filters_run_before_zombie_batch_limit():
    legacy_query = _SRC.index("FROM point_freezes pf")
    first_limit = _SRC.index("LIMIT %s", legacy_query)
    assert _SRC.index("FROM public.organization_charge_links charge", legacy_query) < first_limit
    assert _SRC.index("FROM public.monitoring_run_cells cell", legacy_query) < first_limit
    assert _SRC.index("FROM public.monitoring_keyword_settlements settlement", legacy_query) < first_limit


@pytest.mark.asyncio
async def test_daily_keyword_reconciler_commits_dispatched_and_releases_undispatched():
    from services.freeze_sweeper import reconcile_stale_monitoring_keyword_settlements

    rows = [
        {
            "settlement_reference": "monitoring_daily:1:contract:11:a",
            "state": "coverage_unknown",
            "provider_dispatched": True,
            "billing_user_id": 7,
            "freeze_id": 101,
            "freeze_table": "legacy",
            "subscription_id": 201,
            "claim_token": "claim-a",
            "previous_claim_at": None,
        },
        {
            "settlement_reference": "monitoring_daily:1:contract:12:b",
            "state": "frozen",
            "provider_dispatched": False,
            "billing_user_id": 7,
            "freeze_id": 102,
            "freeze_table": "legacy",
            "subscription_id": 202,
            "claim_token": "claim-b",
            "previous_claim_at": None,
        },
    ]
    settle = MagicMock()
    charge = MagicMock()
    release_claim = MagicMock(return_value=True)
    with patch(
        "db.monitoring_db.list_stale_monitoring_keyword_settlements", return_value=rows
    ), patch(
        "db.monitoring_db.settle_monitoring_keyword_reference", new=settle
    ), patch(
        "db.monitoring_db.record_monitoring_subscription_charge_for_settlement", new=charge
    ), patch(
        "db.monitoring_db.release_subscription_claim", new=release_claim
    ), patch(
        "middleware.billing.commit_freeze", new=AsyncMock(return_value={"success": True})
    ) as commit, patch(
        "middleware.billing.release_freeze", new=AsyncMock(return_value={"success": True})
    ) as release:
        result = await reconcile_stale_monitoring_keyword_settlements(stale_hours=12)

    assert result == {"scanned": 2, "committed": 1, "released": 1, "failed": 0}
    commit.assert_awaited_once()
    release.assert_awaited_once()
    settle.assert_any_call("monitoring_daily:1:contract:11:a", "committed")
    settle.assert_any_call("monitoring_daily:1:contract:12:b", "released")
    charge.assert_called_once_with("monitoring_daily:1:contract:11:a")
    release_claim.assert_called_once_with(202, "claim-b", None)


@pytest.mark.asyncio
async def test_task_reconciler_commits_dispatched_batch_freeze_and_covers_cells():
    from services.freeze_sweeper import reconcile_stale_monitoring_task_settlements

    row = {
        "settlement_reference": "batch_mon_77_abc",
        "task_id": 901,
        "task_count": 1,
        "provider_dispatched": True,
        "execution_abandoned": True,
        "all_admin_covered": False,
    }
    cover = MagicMock()
    with patch(
        "db.monitoring_db.list_stale_monitoring_task_settlements", return_value=[row]
    ), patch(
        "db.monitoring_db.find_monitoring_task_freeze",
        return_value={
            "id": 333, "billing_user_id": 7, "freeze_table": "legacy",
            "status": "frozen", "amount_total": 130,
        },
    ), patch(
        "db.monitoring_db.set_monitoring_task_fulfillment_state", new=cover
    ), patch(
        "db.monitoring_db.recover_abandoned_monitoring_task_execution"
    ) as recover, patch(
        "db.monitoring_db.refresh_monitoring_task_from_cells"
    ) as refresh, patch(
        "middleware.billing.commit_freeze", new=AsyncMock(return_value={"success": True})
    ) as commit:
        result = await reconcile_stale_monitoring_task_settlements(stale_hours=12)

    assert result == {
        "scanned": 1, "committed": 1, "released": 0,
        "execution_recovered": 1, "admin_recovered": 0,
        "organization_projected": 0, "organization_skipped": 0, "failed": 0,
    }
    commit.assert_awaited_once()
    recover.assert_called_once_with(901)
    cover.assert_called_once_with(901, "covered")
    refresh.assert_called_once_with(901)


@pytest.mark.asyncio
async def test_task_reconciler_closes_pre_freeze_crash_without_money_movement():
    from services.freeze_sweeper import reconcile_stale_monitoring_task_settlements

    row = {
        "settlement_reference": "batch_mon_pre_freeze_crash",
        "task_id": 905,
        "task_count": 1,
        "provider_dispatched": False,
        "execution_abandoned": True,
        "all_admin_covered": False,
    }
    cover = MagicMock()
    with patch(
        "db.monitoring_db.list_stale_monitoring_task_settlements", return_value=[row]
    ), patch(
        "db.monitoring_db.find_monitoring_task_freeze", return_value=None
    ), patch(
        "db.monitoring_db.set_monitoring_task_fulfillment_state", new=cover
    ), patch(
        "db.monitoring_db.recover_abandoned_monitoring_task_execution"
    ) as recover, patch(
        "db.monitoring_db.refresh_monitoring_task_from_cells"
    ) as refresh, patch(
        "middleware.billing.commit_freeze", new=AsyncMock()
    ) as commit, patch(
        "middleware.billing.release_freeze", new=AsyncMock()
    ) as release:
        result = await reconcile_stale_monitoring_task_settlements(stale_hours=12)

    assert result == {
        "scanned": 1, "committed": 0, "released": 1,
        "execution_recovered": 1, "admin_recovered": 0,
        "organization_projected": 0, "organization_skipped": 0, "failed": 0,
    }
    recover.assert_called_once_with(905)
    cover.assert_called_once_with(905, "released")
    refresh.assert_called_once_with(905)
    commit.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_reconciler_delegates_organization_physical_freeze():
    from services.freeze_sweeper import reconcile_stale_monitoring_task_settlements

    row = {
        "settlement_reference": "monitor:organization:77",
        "task_id": 902,
        "task_count": 1,
        "provider_dispatched": True,
        "execution_abandoned": True,
        "all_admin_covered": False,
    }
    cover = MagicMock()
    refresh = MagicMock()
    with patch(
        "db.monitoring_db.list_stale_monitoring_task_settlements", return_value=[row]
    ), patch(
        "db.monitoring_db.find_monitoring_task_freeze",
        return_value={
            "id": 334, "billing_user_id": 7, "freeze_table": "legacy",
            "status": "frozen", "amount_total": 130, "organization_linked": True,
        },
    ), patch(
        "db.monitoring_db.set_monitoring_task_fulfillment_state", new=cover
    ), patch(
        "db.monitoring_db.refresh_monitoring_task_from_cells", new=refresh
    ), patch(
        "db.monitoring_db.recover_abandoned_monitoring_task_execution"
    ) as recover, patch(
        "middleware.billing.commit_freeze", new=AsyncMock()
    ) as commit, patch(
        "middleware.billing.release_freeze", new=AsyncMock()
    ) as release:
        result = await reconcile_stale_monitoring_task_settlements(stale_hours=12)

    assert result == {
        "scanned": 1, "committed": 0, "released": 0,
        "execution_recovered": 1, "admin_recovered": 0,
        "organization_projected": 0, "organization_skipped": 1, "failed": 0,
    }
    commit.assert_not_awaited()
    release.assert_not_awaited()
    cover.assert_not_called()
    refresh.assert_called_once_with(902)
    recover.assert_called_once_with(902)


@pytest.mark.asyncio
async def test_task_reconciler_recovers_admin_execution_without_freeze_lookup():
    from services.freeze_sweeper import reconcile_stale_monitoring_task_settlements

    row = {
        "settlement_reference": "batch_mon_admin_903",
        "task_id": 903,
        "task_count": 1,
        "provider_dispatched": True,
        "execution_abandoned": True,
        "all_admin_covered": True,
    }
    with patch(
        "db.monitoring_db.list_stale_monitoring_task_settlements", return_value=[row]
    ), patch(
        "db.monitoring_db.find_monitoring_task_freeze"
    ) as find_freeze, patch(
        "db.monitoring_db.recover_abandoned_monitoring_task_execution"
    ) as recover, patch(
        "db.monitoring_db.refresh_monitoring_task_from_cells"
    ) as refresh, patch(
        "middleware.billing.commit_freeze", new=AsyncMock()
    ) as commit, patch(
        "middleware.billing.release_freeze", new=AsyncMock()
    ) as release:
        result = await reconcile_stale_monitoring_task_settlements(stale_hours=12)

    assert result == {
        "scanned": 1, "committed": 0, "released": 0,
        "execution_recovered": 1, "admin_recovered": 1,
        "organization_projected": 0, "organization_skipped": 0, "failed": 0,
    }
    find_freeze.assert_not_called()
    recover.assert_called_once_with(903)
    refresh.assert_called_once_with(903)
    commit.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("organization_status", "projected_state"),
    (("committed", "covered"), ("refunded", "released")),
)
async def test_task_reconciler_projects_terminal_organization_charge_without_moving_money(
    organization_status, projected_state,
):
    from services.freeze_sweeper import reconcile_stale_monitoring_task_settlements

    row = {
        "settlement_reference": "monitor:organization:904",
        "task_id": 904,
        "task_count": 1,
        "provider_dispatched": True,
        "execution_abandoned": False,
        "all_admin_covered": False,
    }
    cover = MagicMock()
    refresh = MagicMock()
    with patch(
        "db.monitoring_db.list_stale_monitoring_task_settlements", return_value=[row]
    ), patch(
        "db.monitoring_db.find_monitoring_task_freeze",
        return_value={
            "id": 335, "billing_user_id": 7, "freeze_table": "legacy",
            "status": "consumed", "amount_total": 130,
            "organization_linked": True, "organization_status": organization_status,
            "organization_charge_id": 44,
        },
    ), patch(
        "db.monitoring_db.set_monitoring_task_fulfillment_state", new=cover
    ), patch(
        "db.monitoring_db.refresh_monitoring_task_from_cells", new=refresh
    ), patch(
        "db.monitoring_db.revoke_monitoring_task_coverage_for_organization_refund"
    ) as revoke, patch(
        "middleware.billing.commit_freeze", new=AsyncMock()
    ) as commit, patch(
        "middleware.billing.release_freeze", new=AsyncMock()
    ) as release:
        result = await reconcile_stale_monitoring_task_settlements(stale_hours=12)

    assert result == {
        "scanned": 1, "committed": 0, "released": 0,
        "execution_recovered": 0, "admin_recovered": 0,
        "organization_projected": 1, "organization_skipped": 0, "failed": 0,
    }
    if organization_status == "refunded":
        revoke.assert_called_once_with(904, 44)
        cover.assert_not_called()
    else:
        cover.assert_called_once_with(904, projected_state)
        revoke.assert_not_called()
    refresh.assert_called_once_with(904)
    commit.assert_not_awaited()
    release.assert_not_awaited()


def test_task_settlement_candidates_prioritize_refunds_and_actionable_work():
    from db.monitoring_db import list_stale_monitoring_task_settlements

    ordinary = {
        "settlement_reference": "batch_mon_ordinary",
        "task_id": 1,
        "task_count": 1,
    }
    quarantined = [
        {
            "settlement_reference": f"batch_mon_unknown_{index}",
            "task_id": index + 10,
            "task_count": 1,
        }
        for index in range(101)
    ]
    refunded = {
        "settlement_reference": "batch_mon_refunded",
        "task_id": 999,
        "task_count": 1,
        "organization_refunded": True,
        "organization_status_hint": "refunded",
    }
    cursor = MagicMock()
    cursor.fetchall.side_effect = [
        [ordinary, *quarantined],
        [
            {
                "settlement_reference": row["settlement_reference"],
                "organization_status_hint": "unknown",
            }
            for row in quarantined
        ],
        [refunded],
    ]
    cursor.fetchone.return_value = {"relation": "organization_charge_links"}
    connection = MagicMock()
    connection.cursor.return_value = cursor
    with patch("db.monitoring_db.get_connection", return_value=connection):
        rows = list_stale_monitoring_task_settlements(12)

    assert rows[0]["settlement_reference"] == "batch_mon_refunded"
    assert rows[1]["settlement_reference"] == "batch_mon_ordinary"
    assert all(
        row["organization_status_hint"] == "unknown" for row in rows[2:]
    )
