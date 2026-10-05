"""
#3-C2 资金 fail-closed 专项最小测试 (2026-06-07)

老板要求:资金判定改动不能无测试 green。覆盖 3 条:
  1. PlatformAdapter.query: engine_error=True / 'Error:' 前缀 → status=error;
     合法未检出(有实质内容、brand_detected=False)→ 不误标 error(防过度退费)。
  2. run_client_monitoring: run_batch 含 status=error → 不 save、不 completed、release_freeze、返回 status=error。
  3. api.scheduler._async_run_brand: 一个平台 status=error → raise、task failed、不 save、不 trend、不 stage_log。

纯 mock · 不碰真 DB(conftest 仅要求 TEST_DATABASE_URL 占位 + ALLOW_NONTEST_DB=1)。
"""
import asyncio
import json
from contextlib import contextmanager
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from agentscope.tool import ToolResponse


def _tr(payload: dict) -> ToolResponse:
    return ToolResponse(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}])


@contextmanager
def _noctx(*a, **k):
    yield


# ============================================================
# 1. PlatformAdapter.query
# ============================================================
@pytest.mark.asyncio
async def test_platformadapter_engine_error_to_status_error():
    """engine_error:True → status='error'(豆包 429/空响应等失败标记)"""
    from tools.monitoring import batch_monitor

    async def fake_q(**kw):
        return _tr({"engine_error": True, "answer_summary": "豆包空响应(已重试耗尽)", "brand_detected": False})

    with patch.dict(batch_monitor.PlatformAdapter.SUPPORTED_PLATFORMS, {"doubao": fake_q}), \
         patch.object(batch_monitor, "llm_tracking_context", _noctx):
        r = await batch_monitor.PlatformAdapter.query(platform="doubao", question="q", target_brand="B")
    assert r["status"] == "error"
    assert r.get("is_detected") is False


@pytest.mark.asyncio
async def test_platformadapter_error_prefix_to_status_error():
    """纯文本 'Error: ...'(dashscope/kimi/deepseek 异常分支)→ status='error'"""
    from tools.monitoring import batch_monitor

    async def fake_q(**kw):
        return ToolResponse(content=[{"type": "text", "text": "Error: boom timeout"}])

    with patch.dict(batch_monitor.PlatformAdapter.SUPPORTED_PLATFORMS, {"dashscope": fake_q}), \
         patch.object(batch_monitor, "llm_tracking_context", _noctx):
        r = await batch_monitor.PlatformAdapter.query(platform="dashscope", question="q", target_brand="B")
    assert r["status"] == "error"


@pytest.mark.asyncio
async def test_platformadapter_legit_nondetection_not_error():
    """合法未检出:有实质内容但不提品牌 → status='success'、is_detected=False(绝不误标 error→过度退费)"""
    from tools.monitoring import batch_monitor

    content = "市面上做这一行的公司很多，比如甲公司、乙公司、丙公司都不错，建议多对比几家的服务范围和报价再决定。" * 2

    async def fake_q(**kw):
        return _tr({"brand_detected": False, "response": content, "mention_type": "none"})

    # 有内容+未检出会触发 _deepseek_v4_flash_verify_brand LLM 兜底 → mock 成判 False(仍未检出)
    with patch.dict(batch_monitor.PlatformAdapter.SUPPORTED_PLATFORMS, {"kimi": fake_q}), \
         patch.object(batch_monitor, "llm_tracking_context", _noctx), \
         patch("tools.ai_visibility.ai_tester._deepseek_v4_flash_verify_brand", new=AsyncMock(return_value=False)), \
         patch("tools.ai_visibility.ai_tester._extract_mentioned_brands_llm", new=AsyncMock(return_value=[])):
        r = await batch_monitor.PlatformAdapter.query(platform="kimi", question="q", target_brand="目标品牌XYZ不存在")
    assert r["status"] == "success"
    assert r.get("is_detected") is False


# ============================================================
# 2. run_client_monitoring(brand_id 路径·charge=True)
# ============================================================
@pytest.mark.asyncio
async def test_run_client_monitoring_engine_error_isolated_to_failed_cell():
    """单引擎失败只落失败格；成功格持久化，原履约覆盖后续人工重试。"""
    from tools.monitoring import batch_monitor

    kw = {
        "id": 1,
        "quote_id": 10,
        "keyword": "电梯",
        "target_brand": "B",
        "source": "confirmed",
        "entitlement_platforms": "doubao,kimi",
    }

    owner_conn = MagicMock()
    owner_cur = MagicMock()
    owner_conn.cursor.return_value = owner_cur
    owner_cur.fetchone.return_value = {"owner_user_id": 5}

    error_results = [
        {"status": "error", "keyword_id": 1, "keyword": "电梯", "platform": "doubao", "is_detected": False, "cell_id": 801, "cell_claim_token": "claim-801"},
        {"status": "success", "keyword_id": 1, "keyword": "电梯", "platform": "kimi", "is_detected": True, "cell_id": 802, "cell_claim_token": "claim-802"},
    ]

    with patch.object(batch_monitor, "get_monitoring_config", return_value={"default_concurrency": 4, "default_platforms": "doubao,kimi"}), \
         patch.object(batch_monitor, "get_keywords_for_monitoring", return_value=[kw]), \
         patch.object(batch_monitor, "create_monitoring_task", return_value=99), \
             patch.object(batch_monitor, "update_task_status") as m_status, \
             patch.object(batch_monitor, "save_monitoring_result") as m_save, \
             patch.object(batch_monitor, "finish_monitoring_cell_error") as m_cell_error, \
             patch.object(batch_monitor, "resolve_monitoring_query", return_value="电梯哪家好"), \
             patch("db.monitoring_db._resolve_id", return_value=(1, None)), \
             patch("db.monitoring_db.resolve_service_anchored_quote_ids_for_brand", return_value=[10]), \
             patch("db.monitoring_db.create_monitoring_run_cells", return_value=[
                 {"id": 801, "keyword_source": "confirmed", "keyword_id": 1, "platform": "doubao", "is_planned": True},
                 {"id": 802, "keyword_source": "confirmed", "keyword_id": 1, "platform": "kimi", "is_planned": True},
             ]), \
             patch("db.monitoring_db.claim_monitoring_run_cell", side_effect=[{"claim_token": "claim-801"}, {"claim_token": "claim-802"}]), \
             patch("db.monitoring_db.mark_monitoring_cell_dispatched"), \
             patch("db.monitoring_db.set_monitoring_task_fulfillment_state") as m_coverage, \
             patch("db.connection.get_connection", return_value=owner_conn), \
         patch("middleware.billing.freeze_points", new=AsyncMock(return_value={"freeze_id": 7, "amount": 38})), \
         patch("middleware.billing.release_freeze", new=AsyncMock(return_value={"amount": 38})) as m_release, \
         patch("middleware.billing.commit_freeze", new=AsyncMock(return_value={"amount": 38})) as m_commit, \
         patch.object(batch_monitor.MonitoringScheduler, "run_batch", new=AsyncMock(return_value=error_results)):
        result = await batch_monitor.run_client_monitoring(brand_id=1, charge=True)

    assert result["status"] == "success"
    assert result["error_count"] == 1
    m_save.assert_called_once()                       # 成功格不因相邻失败而丢失
    m_cell_error.assert_called_once()
    assert m_cell_error.call_args.kwargs["cell_id"] == 801
    m_release.assert_not_awaited()
    m_commit.assert_awaited_once()                    # 原履约覆盖失败格人工重试
    m_coverage.assert_called_with(99, "covered")
    completed_calls = [c for c in m_status.call_args_list if len(c.args) >= 2 and c.args[1] == "completed"]
    failed_calls = [c for c in m_status.call_args_list if len(c.args) >= 2 and c.args[1] == "failed"]
    assert completed_calls
    assert not failed_calls


@pytest.mark.asyncio
async def test_run_client_monitoring_owner_lookup_failure_is_fail_closed():
    from tools.monitoring import batch_monitor

    keyword = {
        "id": 1,
        "quote_id": 10,
        "keyword": "电梯",
        "target_brand": "B",
        "source": "confirmed",
        "entitlement_platforms": "doubao",
    }
    run_batch = AsyncMock()
    with patch.object(
        batch_monitor,
        "get_monitoring_config",
        return_value={"default_concurrency": 1, "default_platforms": "doubao"},
    ), patch.object(
        batch_monitor, "get_keywords_for_monitoring", return_value=[keyword]
    ), patch.object(
        batch_monitor, "create_monitoring_task", return_value=199
    ), patch.object(
        batch_monitor, "update_task_status"
    ) as task_status, patch(
        "db.monitoring_db._resolve_id", return_value=(1, None)
    ), patch(
        "db.monitoring_db.resolve_service_anchored_quote_ids_for_brand", return_value=[10]
    ), patch(
        "db.monitoring_db.create_monitoring_run_cells", return_value=[]
    ), patch(
        "db.monitoring_db.set_monitoring_task_fulfillment_state"
    ) as fulfillment, patch(
        "db.connection.get_connection", side_effect=RuntimeError("owner db unavailable")
    ), patch(
        "middleware.billing.freeze_points", new=AsyncMock()
    ) as freeze, patch.object(
        batch_monitor.MonitoringScheduler, "run_batch", new=run_batch
    ):
        result = await batch_monitor.run_client_monitoring(brand_id=1, charge=True)

    assert result["error_code"] == "monitoring_billing_owner_unavailable"
    freeze.assert_not_awaited()
    run_batch.assert_not_awaited()
    fulfillment.assert_called_once_with(199, "released")
    task_status.assert_called_once_with(199, "failed")


@pytest.mark.asyncio
async def test_run_client_monitoring_cancel_after_dispatch_commits_owned_freeze():
    import asyncio
    from tools.monitoring import batch_monitor

    keyword = {
        "id": 1,
        "quote_id": 10,
        "keyword": "电梯",
        "target_brand": "B",
        "source": "confirmed",
        "entitlement_platforms": "doubao",
    }
    owner_conn = MagicMock()
    owner_cursor = MagicMock()
    owner_conn.cursor.return_value = owner_cursor
    owner_cursor.fetchone.side_effect = [
        {"owner_user_id": 5},
        {"started": True},
    ]
    commit = AsyncMock(return_value={"amount": 38})
    release = AsyncMock(return_value={"amount": 38})
    with patch.object(
        batch_monitor,
        "get_monitoring_config",
        return_value={"default_concurrency": 1, "default_platforms": "doubao"},
    ), patch.object(
        batch_monitor, "get_keywords_for_monitoring", return_value=[keyword]
    ), patch.object(
        batch_monitor, "create_monitoring_task", return_value=299
    ), patch.object(
        batch_monitor, "update_task_status"
    ), patch(
        "db.monitoring_db._resolve_id", return_value=(1, None)
    ), patch(
        "db.monitoring_db.resolve_service_anchored_quote_ids_for_brand", return_value=[10]
    ), patch(
        "db.monitoring_db.create_monitoring_run_cells",
        return_value=[{
            "id": 901,
            "keyword_source": "confirmed",
            "keyword_id": 1,
            "platform": "doubao",
            "is_planned": True,
        }],
    ), patch(
        "db.monitoring_db.claim_monitoring_run_cell",
        return_value={"claim_token": "claim-901"},
    ), patch(
        "db.monitoring_db.set_monitoring_task_fulfillment_state"
    ) as fulfillment, patch(
        "db.connection.get_connection", return_value=owner_conn
    ), patch(
        "middleware.billing.freeze_points",
        new=AsyncMock(return_value={"freeze_id": 77, "amount": 38}),
    ), patch(
        "middleware.billing.commit_freeze", new=commit
    ), patch(
        "middleware.billing.release_freeze", new=release
    ), patch.object(
        batch_monitor.MonitoringScheduler,
        "run_batch",
        new=AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await batch_monitor.run_client_monitoring(brand_id=1, charge=True)

    commit.assert_awaited_once()
    release.assert_not_awaited()
    fulfillment.assert_called_with(299, "covered")


# ============================================================
# 3. api.scheduler._async_run_brand(daily cron)
# ============================================================
@pytest.mark.asyncio
async def test_async_run_brand_engine_error_keeps_neighbor_success():
    """一个平台失败只终结该格，邻格结果仍持久化且任务保留完整计划。"""
    from api import scheduler as api_scheduler

    client = {"quote_id": 10, "brand_id": 1, "brand_name": "B", "owner_user_id": 5}
    kw = {
        "id": 1,
        "quote_id": 10,
        "keyword": "电梯",
        "target_brand": "B",
        "source": "confirmed",
        "entitlement_platforms": "dashscope,doubao",
    }

    async def fake_query(**kw_):
        # doubao 失败、其它成功
        if kw_.get("platform") == "doubao":
            return {"status": "error", "is_detected": False, "platform": "doubao"}
        return {"status": "success", "is_detected": True, "platform": kw_.get("platform")}

    cells = [
        {"id": 1, "keyword_source": "confirmed", "keyword_id": 1, "platform": "dashscope", "is_planned": True},
        {"id": 2, "keyword_source": "confirmed", "keyword_id": 1, "platform": "deepseek", "is_planned": False},
        {"id": 3, "keyword_source": "confirmed", "keyword_id": 1, "platform": "kimi", "is_planned": False},
        {"id": 4, "keyword_source": "confirmed", "keyword_id": 1, "platform": "doubao", "is_planned": True},
    ]

    class FakeCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return []

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

        def close(self):
            return None

    with patch("db.monitoring_db.get_keywords_for_monitoring", return_value=[kw]), \
         patch("db.monitoring_db.get_monitoring_config", return_value={"default_platforms": "dashscope,doubao"}), \
         patch("db.monitoring_db.create_monitoring_task", return_value=99), \
         patch("db.monitoring_db.create_monitoring_run_cells", return_value=cells), \
         patch("db.monitoring_db.claim_monitoring_run_cell", side_effect=lambda **v: {"claim_token": f"claim-{v['cell_id']}"}), \
         patch("db.monitoring_db.mark_monitoring_cell_dispatched"), \
         patch("db.monitoring_db.finish_monitoring_cell_error") as m_finish, \
         patch("db.monitoring_db.update_task_status") as m_status, \
         patch("db.monitoring_db.save_monitoring_result", return_value=501) as m_save, \
         patch("db.monitoring_db.save_trend_stat") as m_trend, \
         patch("db.monitoring_db.get_connection", return_value=FakeConnection()), \
         patch("tools.monitoring.batch_monitor.resolve_monitoring_query", return_value="电梯哪家好"), \
         patch("tools.monitoring.batch_monitor.PlatformAdapter.query", new=AsyncMock(side_effect=fake_query)), \
         patch("db.pipeline_stage_log_db.log_stage_event") as m_stage:
        result = await api_scheduler._async_run_brand(client)

    assert result["total"] == 1
    assert result["errors"] == 1
    m_save.assert_called_once()
    m_finish.assert_called_once()
    m_trend.assert_not_called()                        # 不 trend
    m_stage.assert_called_once()
    failed_calls = [c for c in m_status.call_args_list if len(c.args) >= 2 and c.args[1] == "failed"]
    assert not failed_calls


@pytest.mark.asyncio
async def test_batch_cancellation_before_dispatch_marks_cell_safe_to_release():
    from tools.monitoring import batch_monitor

    task = batch_monitor.MonitoringTask(
        keyword_id=1,
        keyword="词",
        target_brand="品牌",
        platform="dashscope",
        question="问题",
        cell_id=9,
        cell_claim_token="claim-9",
    )
    scheduler = batch_monitor.MonitoringScheduler(max_concurrency=1)
    scheduler.rate_limiter.wait = AsyncMock(side_effect=asyncio.CancelledError())

    with patch("tools.monitoring.batch_monitor.finish_monitoring_cell_error") as finish, \
         patch("db.monitoring_db.mark_monitoring_cell_dispatched") as mark:
        with pytest.raises(asyncio.CancelledError):
            await scheduler.run_batch([task], monitoring_task_id=1, brand_id=1)

    mark.assert_not_called()
    assert finish.call_args.kwargs["state"] == "failed"
    assert finish.call_args.kwargs["error_code"] == "worker_lost_before_dispatch"


@pytest.mark.asyncio
async def test_batch_cancellation_after_dispatch_quarantines_cell_and_boundary_once():
    from tools.monitoring import batch_monitor

    task = batch_monitor.MonitoringTask(
        keyword_id=1,
        keyword="词",
        target_brand="品牌",
        platform="dashscope",
        question="问题",
        cell_id=10,
        cell_claim_token="claim-10",
    )
    scheduler = batch_monitor.MonitoringScheduler(max_concurrency=1)
    boundary = AsyncMock()

    async def cancelled_provider(**_kwargs):
        raise asyncio.CancelledError()

    with patch("tools.monitoring.batch_monitor.finish_monitoring_cell_error") as finish, \
         patch("db.monitoring_db.mark_monitoring_cell_dispatched"), \
         patch.object(batch_monitor.PlatformAdapter, "query", new=cancelled_provider):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.run_batch(
                [task],
                monitoring_task_id=1,
                brand_id=1,
                before_first_provider=boundary,
            )

    boundary.assert_awaited_once()
    assert finish.call_args.kwargs["state"] == "pending_provider_confirmation"
    assert finish.call_args.kwargs["error_code"] == "provider_outcome_unknown"
