"""监测平台资格 / 计费前置的既有契约测试。

[2026-07-26 · P0-2 Owner 裁决更新] 诊断与监测统一五引擎后，**元宝已是可采集的
监测平台**，不再是"已购但不可采集"的占位。本文件的结构性断言（权益交集、
fail-closed、billing 前置、顺序去重、历史格子投影）一字未减，只把不可采集占位
换成 ``metaso``（config.ai_engines.LEGACY_ENGINES，监测侧确实无 adapter）。
"""

import ast
import sys
import types
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]


def _load_server_function(name, globals_dict):
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment_lines = lines[node.lineno - 1:node.end_lineno]
            while segment_lines and segment_lines[0].lstrip().startswith("@"):
                segment_lines = segment_lines[1:]
            namespace = {"__name__": "server_extracted"}
            namespace.update(globals_dict)
            exec(
                compile("\n".join(segment_lines), f"<server:{name}>", "exec"),
                namespace,
            )
            return namespace[name]
    raise AssertionError(f"server.py missing function: {name}")


def test_organization_artifact_page_fills_past_invisible_newer_rows():
    fill_page = _load_server_function(
        "_organization_artifact_filled_page", {"Request": object}
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            organization_identity=SimpleNamespace(is_member=True)
        )
    )
    stable_time = datetime(2026, 7, 21, tzinfo=timezone.utc)
    candidates = [
        {"id": value, "created_at": stable_time} for value in range(52, 0, -1)
    ]
    cursors = []

    def fetch_page(size, _before_value, before_id):
        cursors.append(before_id)
        offset = 0 if before_id is None else next(
            index + 1 for index, row in enumerate(candidates) if row["id"] == before_id
        )
        return candidates[offset:offset + size]

    def filter_rows(_request, _artifact_type, rows, *, id_key="id"):
        return [row for row in rows if int(row[id_key]) <= 2]

    with patch(
        "auth.brand_access.filter_organization_artifact_rows", side_effect=filter_rows
    ):
        visible = fill_page(request, "monitoring_task", fetch_page, 2)

    assert [row["id"] for row in visible] == [2, 1]
    assert cursors == [None, 3]

    requested_sizes = []
    non_org_request = SimpleNamespace(
        state=SimpleNamespace(organization_identity=None)
    )
    assert fill_page(
        non_org_request,
        "monitoring_task",
        lambda size, _before, _before_id: requested_sizes.append(size) or [],
        9999,
    ) == []
    assert requested_sizes == [200]


def test_monitoring_platform_filter_is_ordered_unique_and_fail_closed():
    from tools.monitoring.batch_monitor import PlatformAdapter

    assert PlatformAdapter.eligible_monitoring_platforms(
        [" metaso ", "DeepSeek", "dashscope", "deepseek", "typo", None]
    ) == ["deepseek", "dashscope"]
    assert PlatformAdapter.eligible_monitoring_platforms("metaso,unknown") == []
    assert PlatformAdapter.eligible_monitoring_platforms(
        "dashscope,deepseek,doubao,metaso",
        "deepseek,metaso",
    ) == ["deepseek"]
    assert PlatformAdapter.eligible_monitoring_platforms(
        "dashscope,deepseek,doubao,metaso",
        "metaso",
    ) == []
    # [P0-2 2026-07-26] 不可采集占位改用 metaso；元宝已进监测执行矩阵。
    assert "metaso" not in PlatformAdapter.get_supported_platforms()
    assert "yuanbao" in PlatformAdapter.get_supported_platforms()


def test_explicit_monitoring_request_must_be_entitled_and_collectable():
    from tools.monitoring.batch_monitor import PlatformAdapter

    assert PlatformAdapter.resolve_requested_monitoring_platforms(
        None,
        "deepseek,metaso",
    ) == {"platforms": ["deepseek"], "rejected": []}
    assert PlatformAdapter.resolve_requested_monitoring_platforms(
        ["kimi"],
        "deepseek",
    ) == {"platforms": [], "rejected": ["kimi"]}
    assert PlatformAdapter.resolve_requested_monitoring_platforms(
        ["deepseek", "kimi"],
        "deepseek",
    ) == {"platforms": ["deepseek"], "rejected": ["kimi"]}
    assert PlatformAdapter.resolve_requested_monitoring_platforms(
        ["metaso"],
        "deepseek,metaso",
    ) == {"platforms": [], "rejected": ["metaso"]}


def test_keyword_plan_uses_persisted_rights_without_cross_running():
    from tools.monitoring.batch_monitor import PlatformAdapter

    keywords = [
        {
            "id": 1,
            "quote_id": 91,
            "source": "confirmed",
            "entitlement_platforms": "deepseek,metaso",
        },
        {
            "id": 2,
            "quote_id": 91,
            "source": "confirmed",
            "entitlement_platforms": "doubao",
        },
    ]
    plan = PlatformAdapter.resolve_keyword_monitoring_plan(
        keywords,
        configured_platforms="dashscope,deepseek,doubao,metaso",
    )

    assert plan["platforms"] == ["deepseek", "doubao"]
    assert [row["_eligible_monitoring_platforms"] for row in plan["keywords"]] == [
        ["deepseek"],
        ["doubao"],
    ]
    assert plan["rejected"] == []

    rejected = PlatformAdapter.resolve_keyword_monitoring_plan(
        keywords,
        requested_platforms=["deepseek"],
        configured_platforms="kimi",
    )
    assert rejected["keywords"] == []
    assert rejected["rejected"] == [{
        "keyword_id": 2,
        "source": "confirmed",
        "quote_id": 91,
        "platforms": ["deepseek"],
    }]


def test_portal_slots_follow_eligible_config_without_uncollectable_placeholder():
    from api.monitoring_api import (
        _fill_engine_slots,
        _historical_keyword_platform_details,
        _resolve_keyword_display_platforms,
    )

    details = [
        {"platform": "deepseek", "is_detected": True},
        {"platform": "metaso", "is_detected": True},
    ]
    filled = _fill_engine_slots(
        details,
        "dashscope,deepseek,doubao,metaso",
    )

    assert [row["platform"] for row in filled] == [
        "dashscope",
        "deepseek",
        "doubao",
    ]
    assert filled[1]["is_detected"] is True
    assert all(row["platform"] != "metaso" for row in filled)

    deepseek_only = _resolve_keyword_display_platforms(
        {"entitlement_platforms": "deepseek"},
        "kimi,deepseek,doubao",
    )
    assert deepseek_only == ["deepseek"]
    assert _resolve_keyword_display_platforms(
        {"entitlement_platforms": None},
        "kimi,deepseek,doubao",
    ) == []
    current = _fill_engine_slots(details, deepseek_only)
    historical = _historical_keyword_platform_details(details, deepseek_only)
    assert [row["platform"] for row in current] == ["deepseek"]
    assert historical == [{
        "platform": "metaso",
        "is_detected": True,
        "historical_only": True,
    }]


def test_client_portal_projects_each_keyword_from_its_own_entitlement():
    from api import monitoring_api

    keywords = [
        {
            "id": 1,
            "quote_id": 91,
            "keyword": "深度词",
            "source": "confirmed",
            "entitlement_platforms": "deepseek",
            "is_core": True,
            "super_red_ocean": False,
            "detection_rate": 0,
            "weighted_rate": 0,
        },
        {
            "id": 2,
            "quote_id": 91,
            "keyword": "豆包词",
            "source": "confirmed",
            "entitlement_platforms": "doubao",
            "is_core": True,
            "super_red_ocean": False,
            "detection_rate": 0,
            "weighted_rate": 0,
        },
    ]
    details = {
        1: [
            {"platform": "deepseek", "is_detected": True},
            {"platform": "kimi", "is_detected": True},
        ],
        2: [{"platform": "doubao", "is_detected": False}],
    }

    class FakeCursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return {"service_days": 365, "service_start_date": None, "paid_at": None}

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            return None

    diagnosis_module = types.ModuleType("db.diagnosis_db")
    diagnosis_module.get_connection = lambda: FakeConnection()
    with patch.dict(
        sys.modules, {"db.diagnosis_db": diagnosis_module}
    ), patch.object(
        monitoring_api, "get_client_keywords", return_value=keywords
    ), patch.object(
        monitoring_api, "_resolve_quote_scope", return_value={"brand_id": 7}
    ), patch.object(
        monitoring_api,
        "get_monitoring_config",
        return_value={"default_platforms": "kimi,deepseek,doubao"},
    ), patch(
        "db.monitoring_db.get_keyword_detection_details", return_value=details
    ), patch(
        "db.monitoring_db.get_keyword_compliance_summary", return_value={}
    ), patch.object(
        monitoring_api, "_get_tier_target", return_value={"target_rate": 50}
    ), patch(
        "db.monitoring_db.get_unified_appearance",
        return_value={"per_keyword": {}, "client_avg": 0},
    ):
        payload = monitoring_api.api_get_client_keywords_merged(91)

    rows = {row["id"]: row for row in payload["keywords"]}
    assert [item["platform"] for item in rows[1]["detection_details"]] == [
        "deepseek"
    ]
    assert [item["platform"] for item in rows[2]["detection_details"]] == [
        "doubao"
    ]
    assert rows[1]["historical_detection_details"] == [{
        "platform": "kimi",
        "is_detected": True,
        "historical_only": True,
    }]


@pytest.mark.asyncio
async def test_paid_diagnosis_yuanbao_keeps_paid_delivery_path(monkeypatch):
    from services.geo_observation import integration
    from tools.ai_visibility import ai_tester

    collect_paid_delivery = AsyncMock(
        return_value=SimpleNamespace(
            response_status="answered",
            answer_text="目标品牌被明确推荐。",
            search_enabled=True,
            citations=[],
            platform_key="yuanbao",
            surface_key="yuanbao_hy3_tokenhub",
            observed_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )
    )
    runtime = SimpleNamespace(
        service=SimpleNamespace(collect_paid_delivery=collect_paid_delivery)
    )
    monkeypatch.setattr(integration, "collection_runtime", lambda: runtime)
    monkeypatch.setattr(
        ai_tester,
        "_call_analyze_visibility",
        AsyncMock(
            return_value={
                "response": "目标品牌被明确推荐。",
                "brand_detected": True,
            }
        ),
    )

    result = await ai_tester.query_yuanbao(
        "哪个品牌值得推荐？",
        check_brand="目标品牌",
        brand_id=17,
        observation_source_ref="paid-diagnosis:quote-91",
        observation_request_id="paid-diagnosis-request-91",
        owner_user_id=7,
    )

    assert result.content
    collect_paid_delivery.assert_awaited_once()
    request = collect_paid_delivery.await_args.args[0]
    assert request.source_kind == "paid_diagnosis"
    assert request.surface_key == "yuanbao_hy3_tokenhub"
    assert request.brand_id == 17


@pytest.mark.asyncio
async def test_batch_monitor_rejects_uncollectable_only_before_any_side_effect():
    from tools.monitoring import batch_monitor

    keyword = {
        "id": 11,
        "quote_id": 91,
        "keyword": "测试词",
        "target_brand": "测试品牌",
        "source": "confirmed",
        "entitlement_platforms": "metaso",
    }

    with patch.object(
        batch_monitor,
        "get_monitoring_config",
        return_value={"default_platforms": "metaso"},
    ), patch.object(batch_monitor, "get_keywords_for_monitoring", return_value=[keyword]) as get_keywords, patch(
        "db.monitoring_db._resolve_id", return_value=(7, None)
    ), patch(
        "db.monitoring_db.resolve_service_anchored_quote_ids_for_brand", return_value=[91]
    ), patch.object(
        batch_monitor, "create_monitoring_task"
    ) as create_task, patch.object(
        batch_monitor.MonitoringScheduler, "run_batch", new=AsyncMock()
    ) as run_batch:
        result = await batch_monitor.run_client_monitoring(
            brand_id=7,
            charge=True,
        )

    assert result == {
        "status": "error",
        "error": "所选词条没有可执行的已购监测引擎",
        "error_code": "no_eligible_monitoring_platforms",
        "task_id": None,
    }
    get_keywords.assert_called_once()
    create_task.assert_not_called()
    run_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_monitor_mixed_config_runs_only_collectable_platforms():
    from tools.monitoring import batch_monitor

    captured = []

    async def fake_run_batch(_self, tasks, *_args, **_kwargs):
        captured.extend(task.platform for task in tasks)
        return [
            {
                "status": "success",
                "keyword_id": task.keyword_id,
                "keyword": task.keyword,
                "platform": task.platform,
                "is_detected": False,
                "cell_id": task.cell_id,
                "cell_claim_token": task.cell_claim_token,
            }
            for task in tasks
        ]

    keyword = {
        "id": 11,
        "quote_id": 18,
        "keyword": "测试词",
        "target_brand": "测试品牌",
        "source": "confirmed",
        "entitlement_platforms": "dashscope,deepseek,doubao,metaso",
    }
    with patch.object(
        batch_monitor,
        "get_monitoring_config",
        return_value={
            "default_concurrency": 4,
            "default_platforms": "dashscope,metaso,deepseek,doubao",
        },
    ), patch.object(
        batch_monitor, "get_keywords_for_monitoring", return_value=[keyword]
    ), patch.object(
        batch_monitor, "create_monitoring_task", return_value=31
    ), patch.object(
        batch_monitor, "update_task_status"
    ), patch.object(
        batch_monitor, "batch_save_results"
    ), patch.object(
        batch_monitor, "save_trend_stat"
    ), patch.object(
        batch_monitor, "get_keyword_trend", return_value=[]
    ), patch(
        "db.monitoring_db._resolve_id", return_value=(501, 18)
    ), patch(
        "db.monitoring_db.create_monitoring_run_cells",
        return_value=[
            {
                "id": cell_id,
                "keyword_source": "confirmed",
                "keyword_id": 11,
                "platform": platform,
                "is_planned": True,
            }
            for cell_id, platform in enumerate(("dashscope", "deepseek", "doubao"), 1)
        ],
    ) as create_cells, patch(
        "db.monitoring_db.claim_monitoring_run_cell",
        side_effect=lambda **kwargs: {"claim_token": f"claim-{kwargs['cell_id']}"},
    ), patch(
        "db.monitoring_db.mark_monitoring_cell_dispatched"
    ), patch.object(
        batch_monitor, "save_monitoring_result", return_value=7001
    ), patch.object(
        batch_monitor.MonitoringScheduler, "run_batch", new=fake_run_batch
    ):
        result = await batch_monitor.run_client_monitoring(
            client_id="18",
            charge=False,
            initial_fulfillment_state="admin_covered",
        )

    assert result["status"] == "success"
    assert result["total_platforms"] == 3
    assert captured == ["dashscope", "deepseek", "doubao"]
    assert "metaso" not in captured
    assert create_cells.call_args.kwargs["fulfillment_state"] == "admin_covered"


@pytest.mark.asyncio
async def test_nonstream_api_forwards_admin_coverage_before_task_creation():
    from api import monitoring_api

    request = SimpleNamespace(
        client_id=None,
        brand_id=501,
        keyword_keys=["confirmed-11"],
        platforms=["dashscope"],
        concurrency=1,
    )
    runner = AsyncMock(return_value={"status": "success", "task_id": 31})
    with patch.object(monitoring_api, "run_client_monitoring", new=runner):
        result = await monitoring_api.api_run_monitoring(
            request,
            settlement_reference="batch_mon_admin_31",
            initial_fulfillment_state="admin_covered",
        )

    assert result["task_id"] == 31
    assert runner.await_args.kwargs["initial_fulfillment_state"] == "admin_covered"
    assert runner.await_args.kwargs["settlement_reference"] == "batch_mon_admin_31"


@pytest.mark.asyncio
async def test_batch_monitor_rejects_explicit_unpurchased_platform_before_side_effects():
    from tools.monitoring import batch_monitor

    keyword = {
        "id": 11,
        "quote_id": 91,
        "keyword": "测试词",
        "target_brand": "测试品牌",
        "source": "confirmed",
        "entitlement_platforms": "deepseek",
    }

    with patch.object(
        batch_monitor,
        "get_monitoring_config",
        return_value={"default_platforms": "deepseek"},
    ), patch.object(batch_monitor, "get_keywords_for_monitoring", return_value=[keyword]) as get_keywords, patch(
        "db.monitoring_db._resolve_id", return_value=(7, None)
    ), patch(
        "db.monitoring_db.resolve_service_anchored_quote_ids_for_brand", return_value=[91]
    ), patch.object(
        batch_monitor, "create_monitoring_task"
    ) as create_task, patch.object(
        batch_monitor.MonitoringScheduler, "run_batch", new=AsyncMock()
    ) as run_batch:
        result = await batch_monitor.run_client_monitoring(
            brand_id=7,
            platforms=["kimi"],
            charge=True,
        )

    assert result["error_code"] == "requested_platform_not_entitled"
    get_keywords.assert_called_once()
    create_task.assert_not_called()
    run_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_monitor_does_not_turn_tampered_config_into_entitlement():
    from tools.monitoring import batch_monitor

    keyword = {
        "id": 11,
        "quote_id": 91,
        "keyword": "测试词",
        "target_brand": "测试品牌",
        "source": "confirmed",
        "entitlement_platforms": "deepseek",
    }
    with patch.object(
        batch_monitor,
        "get_monitoring_config",
        return_value={"default_platforms": "kimi"},
    ), patch.object(
        batch_monitor, "get_keywords_for_monitoring", return_value=[keyword]
    ), patch.object(
        batch_monitor, "create_monitoring_task"
    ) as create_task, patch.object(
        batch_monitor.MonitoringScheduler, "run_batch", new=AsyncMock()
    ) as run_batch:
        result = await batch_monitor.run_client_monitoring(
            client_id="91",
            charge=True,
        )

    assert result["error_code"] == "no_eligible_monitoring_platforms"
    create_task.assert_not_called()
    run_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_rejects_mixed_request_when_any_platform_is_unpurchased():
    from api import monitoring_api

    request = monitoring_api.MonitoringRunRequest(
        brand_id=7,
        platforms=["deepseek", "kimi"],
    )
    with patch.object(
        monitoring_api,
        "_resolve_brand_and_quotes",
        return_value=(7, [91]),
    ), patch(
        "db.monitoring_db.is_quote_service_anchored",
        return_value=True,
    ), patch(
        "db.monitoring_db.get_monitoring_config",
        return_value={"default_platforms": "deepseek"},
    ), patch(
        "db.monitoring_db.get_keywords_for_monitoring",
        return_value=[{
            "id": 11,
            "quote_id": 91,
            "keyword": "测试词",
            "target_brand": "测试品牌",
            "source": "confirmed",
            "entitlement_platforms": "deepseek",
        }],
    ) as get_keywords, patch(
        "middleware.billing.freeze_points", new=AsyncMock()
    ) as freeze, patch.object(
        monitoring_api.PlatformAdapter, "query", new=AsyncMock()
    ) as provider:
        events = [
            event
            async for event in monitoring_api.api_run_monitoring_stream(
                request,
                user_id=7,
                is_admin=False,
            )
        ]

    assert len(events) == 1
    assert "requested_platform_not_entitled" in events[0]
    get_keywords.assert_called_once()
    freeze.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_publishes_full_plan_before_provider_and_covers_disconnect_retry():
    from api import monitoring_api

    request = monitoring_api.MonitoringRunRequest(brand_id=7)
    keyword = {
        "id": 11,
        "quote_id": 91,
        "keyword": "测试词",
        "target_brand": "测试品牌",
        "source": "confirmed",
        "entitlement_platforms": "deepseek",
    }
    cells = [
        {
            "id": 900 + index,
            "task_id": 501,
            "brand_id": 7,
            "keyword_id": 11,
            "keyword_source": "confirmed",
            "quote_id": 91,
            "keyword_snapshot": "测试词",
            "question_snapshot": "测试词哪家好？推荐一下",
            "target_brand_snapshot": "测试品牌",
            "platform": platform,
            "is_planned": platform == "deepseek",
            "state": "queued" if platform == "deepseek" else "unavailable",
            "plan_hash": f"{index + 1:064x}",
            "error_code": None,
        }
        for index, platform in enumerate(("dashscope", "deepseek", "kimi", "doubao"))
    ]
    provider_started = __import__("asyncio").Event()

    async def blocked_provider(**_kwargs):
        provider_started.set()
        await __import__("asyncio").Future()

    commit = AsyncMock(return_value={"amount": 130})
    release = AsyncMock(return_value={"amount": 130})
    with patch.object(
        monitoring_api, "_resolve_brand_and_quotes", return_value=(7, [91])
    ), patch(
        "db.monitoring_db.is_quote_service_anchored", return_value=True
    ), patch(
        "db.monitoring_db.get_monitoring_config", return_value={"default_platforms": "deepseek"}
    ), patch(
        "db.monitoring_db.get_keywords_for_monitoring", return_value=[keyword]
    ), patch(
        "middleware.billing.freeze_points",
        new=AsyncMock(return_value={"freeze_id": 71, "freeze_table": "point_freezes", "amount": 130}),
    ), patch(
        "db.monitoring_db.create_monitoring_task", return_value=501
    ), patch(
        "db.monitoring_db.create_monitoring_run_cells", return_value=cells
    ), patch(
        "db.monitoring_db.claim_monitoring_run_cell",
        return_value={"claim_token": "00000000-0000-0000-0000-000000000111"},
    ), patch(
        "db.monitoring_db.mark_monitoring_cell_dispatched"
    ), patch(
        "db.monitoring_db.finish_monitoring_cell_error"
    ) as finish_cell, patch(
        "db.monitoring_db.refresh_monitoring_task_from_cells"
    ) as refresh_task, patch(
        "db.monitoring_db.update_task_status"
    ), patch(
        "db.monitoring_db.set_monitoring_task_fulfillment_state"
    ) as coverage, patch.object(
        monitoring_api, "log_operation"
    ), patch.object(
        monitoring_api.PlatformAdapter, "query", new=blocked_provider
    ), patch(
        "middleware.billing.commit_freeze", new=commit
    ), patch(
        "middleware.billing.release_freeze", new=release
    ):
        stream = monitoring_api.api_run_monitoring_stream(
            request, user_id=7, is_admin=False
        )
        start_event = await anext(stream)
        assert '"type": "start"' in start_event
        assert all(platform in start_event for platform in ("dashscope", "deepseek", "kimi", "doubao"))
        assert not provider_started.is_set()

        running_event = await anext(stream)
        assert '"type": "cell"' in running_event
        await __import__("asyncio").wait_for(provider_started.wait(), timeout=1)
        await stream.aclose()

    commit.assert_awaited_once()
    release.assert_not_awaited()
    finish_cell.assert_called_once()
    assert finish_cell.call_args.kwargs["state"] == "pending_provider_confirmation"
    refresh_task.assert_called_once_with(501)
    coverage.assert_called_with(501, "covered")


@pytest.mark.asyncio
async def test_nonstream_route_rejects_unpurchased_platform_before_freeze_or_provider():
    from tools.monitoring.batch_monitor import PlatformAdapter

    request = SimpleNamespace(
        brand_id=7,
        quote_id=None,
        client_id=None,
        keyword_keys=None,
        platforms=["dashscope"],
    )
    http_request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 7, "is_admin": False})
    )
    freeze = AsyncMock()
    provider = AsyncMock()
    api_module = types.ModuleType("api.monitoring_api")
    api_module._resolve_brand_and_quotes = lambda _request: (7, [91])
    db_module = types.ModuleType("db.monitoring_db")
    db_module.DEFAULT_MONITORING_PLATFORMS = "dashscope,deepseek,doubao,metaso"
    db_module.get_monitoring_config = lambda **_kwargs: {
        "default_platforms": "deepseek"
    }
    db_module.get_keywords_for_monitoring = lambda **_kwargs: [{
        "id": 11,
        "quote_id": 91,
        "keyword": "测试词",
        "target_brand": "测试品牌",
        "source": "confirmed",
        "entitlement_platforms": "deepseek",
    }]
    batch_module = types.ModuleType("tools.monitoring.batch_monitor")
    batch_module.PlatformAdapter = PlatformAdapter
    billing_module = types.ModuleType("middleware.billing")
    billing_module.freeze_points = freeze
    logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    route = _load_server_function(
        "run_monitoring",
        {
            "MonitoringRunRequest": object,
            "Request": object,
            "HTTPException": HTTPException,
            "logger": logger,
        },
    )
    with patch.dict(
        sys.modules,
        {
            "api.monitoring_api": api_module,
            "db.monitoring_db": db_module,
            "tools.monitoring.batch_monitor": batch_module,
            "middleware.billing": billing_module,
        },
    ), patch.object(PlatformAdapter, "query", new=provider):
        with pytest.raises(HTTPException) as exc_info:
            await route(request, http_request)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "requested_platform_not_entitled"
    freeze.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_deprecated_manual_helper_rejects_unpurchased_platform_before_task():
    from api import monitoring_api

    keyword = {
        "id": 11,
        "quote_id": 91,
        "keyword": "测试词",
        "target_brand": "测试品牌",
        "source": "confirmed",
        "entitlement_platforms": "deepseek",
    }

    with patch(
        "db.monitoring_db.get_monitoring_config",
        return_value={"default_platforms": "deepseek"},
    ), patch(
        "db.monitoring_db.get_keywords_for_monitoring", return_value=[keyword]
    ) as get_keywords, patch(
        "db.monitoring_db.resolve_service_anchored_quote_ids_for_brand", return_value=[91]
    ), patch(
        "db.monitoring_db.create_monitoring_task"
    ) as create_task, patch.object(
        monitoring_api.PlatformAdapter, "query", new=AsyncMock()
    ) as provider:
        result = await monitoring_api.run_client_monitoring_with_details(
            brand_id=7,
            platforms=["kimi"],
        )

    assert result["error_code"] == "requested_platform_not_entitled"
    get_keywords.assert_called_once()
    create_task.assert_not_called()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_detection_helper_rejects_uncollectable_before_adapter_query():
    from api import monitoring_api

    with patch.object(
        monitoring_api.PlatformAdapter, "query", new=AsyncMock()
    ) as query:
        result = await monitoring_api.run_detection_for_keyword(
            keyword="测试词",
            target_brand="测试品牌",
            platforms=["metaso"],
        )

    assert result["status"] == "error"
    assert result["error_code"] == "no_eligible_monitoring_platforms"
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_subscription_job_uses_per_subscription_platforms_before_billing():
    import scheduler

    subscription = {
        "id": 41,
        "user_id": 7,
        "keyword_id": 17,
        "quote_id": 91,
        "brand_id": None,
        "feature_code": "monitoring_keyword_daily",
        "entitlement_platforms": "metaso",
    }
    with patch(
        "db.monitoring_db.list_paused_subscriptions_for_resume",
        return_value=[],
    ), patch(
        "db.monitoring_db.list_active_subscriptions",
        return_value=[subscription],
    ), patch(
        "db.monitoring_db.get_monitoring_config",
        return_value={"default_platforms": "dashscope,deepseek,doubao,metaso"},
    ) as get_config, patch(
        "middleware.billing.check_balance_only", new=AsyncMock()
    ) as check_balance, patch(
        "db.monitoring_db.claim_subscription_for_today_with_token"
    ) as claim, patch(
        "api.monitoring_api.run_detection_for_keyword", new=AsyncMock()
    ) as provider:
        await scheduler.job_daily_monitoring()

    get_config.assert_called_once_with(brand_id=None, client_id="91")
    check_balance.assert_not_awaited()
    claim.assert_not_called()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_subscription_without_persisted_entitlement_has_zero_side_effects():
    import scheduler

    subscription = {
        "id": 43,
        "user_id": 7,
        "keyword_id": 19,
        "quote_id": 93,
        "brand_id": None,
        "feature_code": "monitoring_keyword_daily",
        "entitlement_platforms": None,
    }
    with patch(
        "db.monitoring_db.list_paused_subscriptions_for_resume",
        return_value=[],
    ), patch(
        "db.monitoring_db.list_active_subscriptions",
        return_value=[subscription],
    ), patch(
        "db.monitoring_db.get_monitoring_config",
        return_value={"default_platforms": "dashscope,deepseek,doubao"},
    ), patch(
        "middleware.billing.check_balance_only", new=AsyncMock()
    ) as check_balance, patch(
        "db.monitoring_db.claim_subscription_for_today_with_token"
    ) as claim, patch(
        "db.monitoring_db.create_monitoring_task"
    ) as create_task, patch(
        "api.monitoring_api.run_detection_for_keyword", new=AsyncMock()
    ) as provider:
        await scheduler.job_daily_monitoring()

    check_balance.assert_not_awaited()
    claim.assert_not_called()
    create_task.assert_not_called()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_subscription_runs_only_purchased_collectable_intersection_once():
    import scheduler

    provider_platforms = []

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

    async def fake_provider(**kwargs):
        provider_platforms.append(kwargs["platform"])
        return {
            "status": "success",
            "platform": "deepseek",
            "is_detected": False,
            "mention_type": "none",
            "response_snippet": "",
            "full_response": "ok",
        }

    subscription = {
        "id": 42,
        "user_id": 7,
        "keyword_id": 18,
        "quote_id": 92,
        "brand_id": 12,
        "keyword": "测试词",
        "monitoring_query": "测试词推荐",
        "brand_name": "测试品牌",
        "feature_code": "monitoring_keyword_daily",
        "entitlement_platforms": "deepseek,yuanbao",
    }
    claim_state = {
        "claim_token": "claim-42",
        "previous_value": None,
    }
    durable_cells = [
        {"id": 1, "keyword_source": "contract", "keyword_id": 18, "platform": "dashscope", "is_planned": False},
        {"id": 2, "keyword_source": "contract", "keyword_id": 18, "platform": "deepseek", "is_planned": True},
        {"id": 3, "keyword_source": "contract", "keyword_id": 18, "platform": "kimi", "is_planned": False},
        {"id": 4, "keyword_source": "contract", "keyword_id": 18, "platform": "doubao", "is_planned": False},
    ]
    record_charge = MagicMock()
    settlement_mocks = {
        "create_monitoring_keyword_settlements": MagicMock(return_value=[]),
        "mark_monitoring_keyword_settlement_dispatched": MagicMock(),
        "record_monitoring_keyword_settlement_freeze": MagicMock(),
        "settle_monitoring_keyword_reference": MagicMock(),
    }
    with patch(
        "db.monitoring_db.list_paused_subscriptions_for_resume",
        return_value=[],
    ), patch(
        "db.monitoring_db.list_active_subscriptions",
        return_value=[subscription],
    ), patch(
        "db.monitoring_db.get_monitoring_config",
        return_value={"default_platforms": "dashscope,deepseek,doubao,metaso"},
    ), patch(
        "middleware.billing.check_balance_only", new=AsyncMock()
    ) as check_balance, patch.multiple(
        "middleware.billing",
        freeze_points=AsyncMock(return_value={
            "freeze_id": 501, "freeze_table": "legacy", "amount": 130,
        }),
        commit_freeze=AsyncMock(return_value={"success": True}),
        release_freeze=AsyncMock(return_value={"success": True}),
    ), patch(
        "db.monitoring_db.claim_subscription_for_today_with_settlement",
        return_value=claim_state,
    ) as claim, patch(
        "db.monitoring_db.record_monitoring_subscription_charge_for_settlement",
        new=record_charge,
    ), patch(
        "db.monitoring_db.create_monitoring_task",
        return_value=701,
    ), patch.multiple(
        "db.monitoring_db", **settlement_mocks,
    ), patch(
        "db.monitoring_db.create_monitoring_run_cells",
        return_value=durable_cells,
    ), patch(
        "db.monitoring_db.claim_monitoring_run_cell",
        return_value={"claim_token": "cell-claim"},
    ), patch(
        "db.monitoring_db.mark_monitoring_cell_dispatched",
    ), patch(
        "db.monitoring_db.update_task_status",
    ), patch(
        "db.monitoring_db.save_monitoring_result",
        return_value=901,
    ), patch(
        "db.monitoring_db.get_connection",
        return_value=FakeConnection(),
    ), patch(
        "tools.monitoring.batch_monitor.PlatformAdapter.query",
        new=fake_provider,
    ):
        await scheduler.job_daily_monitoring()

    assert provider_platforms == ["deepseek"]
    check_balance.assert_awaited_once_with(7, "monitoring_keyword_daily")
    claim.assert_called_once()
    assert claim.call_args.args[0] == 42
    assert claim.call_args.args[1].startswith("monitoring_daily:701:contract:18:")
    record_charge.assert_called_once_with(claim.call_args.args[1])


@pytest.mark.asyncio
async def test_scheduled_brand_executes_heterogeneous_keywords_without_union_cross_run():
    from api import scheduler as api_scheduler

    provider_pairs = []
    keyword_plan = [
        {
            "id": 101,
            "quote_id": 91,
            "keyword": "深度词",
            "target_brand": "测试品牌",
            "source": "confirmed",
            "entitlement_platforms": "deepseek",
            "_eligible_monitoring_platforms": ["deepseek"],
        },
        {
            "id": 102,
            "quote_id": 91,
            "keyword": "豆包词",
            "target_brand": "测试品牌",
            "source": "confirmed",
            "entitlement_platforms": "doubao",
            "_eligible_monitoring_platforms": ["doubao"],
        },
    ]

    async def fake_query(**kwargs):
        provider_pairs.append((kwargs["keyword_id"], kwargs["platform"]))
        return {
            "status": "success",
            "is_detected": False,
            "mention_type": "none",
            "full_response": "ok",
        }

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

        def rollback(self):
            return None

        def close(self):
            return None

    client = {
        "quote_id": 91,
        "brand_id": 7,
        "brand_name": "测试品牌",
        "owner_user_id": 5,
        "_monitoring_keywords": keyword_plan,
        # This union is display/statistics metadata only and must never drive tasks.
        "_monitoring_platform_summary": ["deepseek", "doubao"],
    }
    durable_cells = [
        {"id": 11, "keyword_source": "confirmed", "keyword_id": 101, "platform": "deepseek", "is_planned": True},
        {"id": 12, "keyword_source": "confirmed", "keyword_id": 102, "platform": "doubao", "is_planned": True},
    ]
    stage_log = MagicMock()
    with patch(
        "db.monitoring_db.create_monitoring_task", return_value=801
    ), patch(
        "db.monitoring_db.create_monitoring_run_cells", return_value=durable_cells
    ), patch(
        "db.monitoring_db.claim_monitoring_run_cell",
        side_effect=lambda **value: {"claim_token": f"claim-{value['cell_id']}"},
    ), patch(
        "db.monitoring_db.mark_monitoring_cell_dispatched"
    ), patch(
        "db.monitoring_db.update_task_status"
    ), patch(
        "db.monitoring_db.save_monitoring_result"
    ), patch(
        "db.monitoring_db.get_connection", return_value=FakeConnection()
    ), patch(
        "db.monitoring_db.save_trend_stat"
    ), patch(
        "tools.monitoring.batch_monitor.resolve_monitoring_query",
        side_effect=lambda kw: kw["keyword"],
    ), patch(
        "tools.monitoring.batch_monitor.PlatformAdapter.query", new=fake_query
    ), patch(
        "writing.feature_switches.is_feature_enabled", return_value=False
    ), patch(
        "db.pipeline_stage_log_db.log_stage_event",
        stage_log,
    ):
        result = await api_scheduler._async_run_brand(client)

    assert result["total"] == 2
    assert provider_pairs == [(101, "deepseek"), (102, "doubao")]
    stage_log.assert_called_once()
    assert stage_log.call_args.kwargs["meta"]["platform_count"] == 2


def test_daily_subscription_source_filters_before_balance_claim_and_provider():
    source = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    active_section = source[source.index("active_subs ="):]
    active_section = active_section[:active_section.index("async def _process_keyword")]
    assert active_section.index("eligible_monitoring_platforms") < active_section.index(
        "check_balance_only"
    )
    process = source[source.index("async def _process_keyword"):]
    assert process.index('eligible_platforms = kw.get("eligible_platforms")') < process.index(
        "claim_subscription_for_today_with_settlement"
    )
    assert "for platform in eligible_platforms" in process
    assert "PlatformAdapter.query(" in process


def test_member_retry_route_is_exactly_classified_for_monitoring_run():
    from services.organization_route_contract import match_member_geo_route

    policy = match_member_geo_route(
        "POST", "/api/monitoring/tasks/123/cells/456/retry"
    )
    assert policy is not None
    # [Review-CTO 2026-07-26] fafb44a6(拆分角色团队)把重试路由能力名
    # monitoring.run → monitoring.retry 时未同步本断言,base 即红(陈年失修,
    # 非诊断包新增)。测试意图(重试路由精确分类)不变,同步能力名。
    assert policy.capability == "monitoring.retry"
    assert policy.resource_kind == "monitoring_task"
    assert match_member_geo_route(
        "POST", "/api/monitoring/tasks/0/cells/456/retry"
    ) is None
    assert match_member_geo_route(
        "GET", "/api/monitoring/tasks/123/cells/456/retry"
    ) is None


def test_member_identity_review_routes_are_exactly_classified():
    from services.organization_route_contract import match_member_geo_route

    listing = match_member_geo_route("GET", "/api/monitoring/identity-reviews")
    decision = match_member_geo_route(
        "POST", "/api/monitoring/identity-reviews/123/decision"
    )
    assert listing is not None and listing.capability == "monitoring.read_assigned"
    assert decision is not None and decision.capability == "monitoring.run"
    assert match_member_geo_route(
        "POST", "/api/monitoring/identity-reviews/0/decision"
    ) is None


def test_all_entrypoints_filter_before_task_billing_or_provider():
    batch_source = (ROOT / "tools" / "monitoring" / "batch_monitor.py").read_text(
        encoding="utf-8"
    )
    stream_source = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    scheduler_source = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")

    batch_fn = batch_source[batch_source.index("async def run_client_monitoring"):]
    assert batch_fn.index("resolve_keyword_monitoring_plan") < batch_fn.index(
        "create_monitoring_task("
    )
    assert batch_fn.index("resolve_keyword_monitoring_plan") < batch_fn.index(
        "freeze_points"
    )

    stream_fn = stream_source[stream_source.index("async def api_run_monitoring_stream"):]
    stream_fn = stream_fn[:stream_fn.index("async def run_client_monitoring_with_details")]
    assert "get_monitoring_config(\n        brand_id=brand_id," in stream_fn
    assert stream_fn.index("resolve_keyword_monitoring_plan") < stream_fn.index(
        "freeze_points"
    )
    assert stream_fn.index("resolve_keyword_monitoring_plan") < stream_fn.index(
        "create_monitoring_task("
    )

    route_fn = server_source[server_source.index('async def run_monitoring(request:'):]
    route_fn = route_fn[:route_fn.index('from starlette.responses import StreamingResponse')]
    assert route_fn.index("_resolve_brand_and_quotes") < route_fn.index(
        "get_monitoring_config"
    )
    assert route_fn.index("resolve_keyword_monitoring_plan") < route_fn.index("freeze_points")

    scheduled_fn = scheduler_source[
        scheduler_source.index("def _run_clients_with_billing"):
    ]
    scheduled_fn = scheduled_fn[:scheduled_fn.index("async def _async_run_brand")]
    assert scheduled_fn.index("resolve_keyword_monitoring_plan") < scheduled_fn.index(
        "claim_quote_for_monitoring_with_token"
    )
    assert scheduled_fn.index("resolve_keyword_monitoring_plan") < scheduled_fn.index(
        "freeze_points"
    )

    async_brand_fn = scheduler_source[scheduler_source.index("async def _async_run_brand"):]
    assert 'client.get("_monitoring_keywords")' in async_brand_fn
    assert 'for platform in kw["_eligible_monitoring_platforms"]' in async_brand_fn
    assert "for platform in platforms" not in async_brand_fn
    assert 'client.get("_monitoring_platform_summary")' not in async_brand_fn


def test_client_keyword_projection_carries_persisted_platform_rights():
    source = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    start = source.index("def get_client_keywords(")
    body = source[start:source.index("def get_keyword_detection_details", start)]
    assert "monitoring_product_platform_matrices" in body
    assert "confirmed_keywords.monitoring_product_version" in body
    assert "platforms as entitlement_platforms" in body
    assert "confirmed_keywords.platforms" not in body
    assert "kb.entitlement_platforms" in body


def test_paid_diagnosis_yuanbao_implementation_is_untouched():
    source = (ROOT / "tools" / "ai_visibility" / "ai_tester.py").read_text(
        encoding="utf-8"
    )
    start = source.index("async def query_yuanbao")
    body = source[start:start + 10000]
    assert "collect_paid_delivery" in body
    assert "source_kind" in body
