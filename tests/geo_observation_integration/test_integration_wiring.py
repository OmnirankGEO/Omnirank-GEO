from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from services.geo_observation import integration
from services.geo_observation import wiring


ROOT = Path(__file__).resolve().parents[2]


VALID_POLICY = {
    "platforms": [
        {
            "platform_key": "doubao",
            "enabled": True,
            "base_weight_bps": 3500,
            "surface_key": "doubao_ark_api_search",
            "legacy_read_only": False,
        },
        {
            "platform_key": "qwen",
            "enabled": True,
            "base_weight_bps": 2500,
            "surface_key": "qwen_dashscope_search",
            "legacy_read_only": False,
        },
        {
            "platform_key": "deepseek",
            "enabled": True,
            "base_weight_bps": 2500,
            "surface_key": "deepseek_dashscope_search_legacy",
            "legacy_read_only": False,
        },
        {
            "platform_key": "yuanbao",
            "enabled": True,
            "base_weight_bps": 1500,
            "surface_key": "yuanbao_hy3_tokenhub",
            "legacy_read_only": False,
        },
    ],
    "source_base_weights_bps": {
        "research": 10000,
        "paid_diagnosis": 4000,
        "monitoring": 7000,
    },
    "sampling_budget": {
        "max_calls_per_round": 5000,
        "max_calls_per_day": 20000,
        "max_cost_micros_per_day": 1000000000,
        "max_retry_calls_per_request": 2,
    },
    "max_single_brand_share_bps": 1000,
    "public_min_independent_brands": 3,
    "public_min_source_types": 2,
    "retention_days": 180,
    "anomaly_confirmation_numerator": 2,
    "anomaly_confirmation_denominator": 3,
    "feature_flags": {
        "ingest_enabled": False,
        "promotion_enabled": False,
        "aggregation_enabled": False,
        "product_enabled": False,
    },
}


def test_database_policy_reader_uses_effective_flags(monkeypatch):
    raw = {
        "policy_version": 7,
        "policy": deepcopy(VALID_POLICY),
        "effective_flags": {
            "ingest_enabled": True,
            "promotion_enabled": False,
            "aggregation_enabled": False,
            "product_enabled": False,
        },
    }
    monkeypatch.setattr("services.geo_observation.policy.get_policy", lambda: raw)
    snapshot = integration.DatabaseObservationPolicyReader().get_policy()
    assert snapshot.policy_version == "7"
    assert snapshot.feature_flags.ingest_enabled is True
    assert snapshot.enabled_surface_by_platform()["yuanbao"] == "yuanbao_hy3_tokenhub"


def test_collection_control_readiness_is_mode_aware_without_fake_driver():
    from services.ai_surface_monitoring.scheduler_wiring import check_readiness

    bridge = check_readiness("existing_collectors_reconciled")
    native = check_readiness("native_sampling_driver")
    assert bridge == {
        "mode": "existing_collectors_reconciled",
        "ready": True,
        "status": "ready",
        "problems": [],
    }
    assert native["ready"] is False
    assert native["status"] == "unavailable"
    assert any("sampling driver" in item for item in native["problems"])


def test_native_mode_missing_driver_fails_closed_even_with_reaper(monkeypatch):
    from services.ai_surface_monitoring import scheduler_wiring as native_wiring

    monkeypatch.setattr(native_wiring, "_driver", native_wiring._NoopDriver())
    monkeypatch.setattr(native_wiring, "_reservation_reaper", lambda: 0)
    result = native_wiring.check_readiness("native_sampling_driver")
    assert result["status"] == "unavailable"
    assert result["problems"] == [
        "sampling driver 未注入(set_sampling_driver);采集不能实跑"
    ]


def test_native_mode_missing_reaper_fails_closed_even_with_driver(monkeypatch):
    from services.ai_surface_monitoring import scheduler_wiring as native_wiring

    class Driver:
        async def run_tick(self, source_kind):
            return {"source_kind": source_kind}

    monkeypatch.setattr(native_wiring, "_driver", Driver())
    monkeypatch.setattr(native_wiring, "_reservation_reaper", None)
    result = native_wiring.check_readiness("native_sampling_driver")
    assert result["status"] == "unavailable"
    assert result["problems"] == [
        "reservation reaper 未注入(set_reservation_reaper);孤儿预留无法回收"
    ]


def test_scheduler_registers_governance_and_aggregation_but_not_collection(monkeypatch):
    scheduler = BackgroundScheduler()
    monkeypatch.setattr(
        integration,
        "collection_mode_provider",
        lambda *_args, **_kwargs: integration.ObservationCollectionMode.EXISTING_COLLECTORS_RECONCILED,
    )
    first = integration.register_geo_observation_integration_jobs(scheduler)
    second = integration.register_geo_observation_integration_jobs(scheduler)
    ids = {job.id for job in scheduler.get_jobs()}
    assert set(first) == {
        "geo_observation_promotion",
        "geo_observation_reconciler",
        "geo_observation_aggregation_refresh",
    }
    assert second == []
    assert "ai_surface_obs_monitoring_daily" not in ids
    assert "ai_surface_obs_research_tick" not in ids
    assert "ai_surface_obs_reconciler" not in ids


def test_native_mode_registers_zero_collection_or_provider_jobs_until_takeover(monkeypatch):
    scheduler = BackgroundScheduler()
    monkeypatch.setattr(
        integration,
        "collection_mode_provider",
        lambda *_args, **_kwargs: integration.ObservationCollectionMode.NATIVE_SAMPLING_DRIVER,
    )
    monkeypatch.setattr(
        integration,
        "collection_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("native runtime/provider forbidden")),
    )
    monkeypatch.setattr(
        integration,
        "register_observation_collection_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("native collection jobs forbidden")
        ),
    )
    registered = integration.register_geo_observation_integration_jobs(scheduler)
    ids = {job.id for job in scheduler.get_jobs()}
    assert set(registered) == {
        "geo_observation_promotion",
        "geo_observation_reconciler",
        "geo_observation_aggregation_refresh",
    }
    assert not any(job_id.startswith("ai_surface_obs_") for job_id in ids)


def test_reconciler_ingest_flag_only_gates_registration(monkeypatch):
    seen = {}
    monkeypatch.setattr(wiring.policy, "is_flag_enabled", lambda name: False)
    monkeypatch.setattr(
        wiring,
        "run_reconciler",
        lambda **kwargs: seen.update(kwargs) or {"ok": True},
    )
    assert wiring.reconciler_job() == {"ok": True}
    assert seen == {"registration_enabled": False}


def test_successful_reconciler_publishes_real_scheduler_inventory(monkeypatch):
    seen = []
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: None, "interval", seconds=60, id="job-a")
    scheduler.add_job(lambda: None, "interval", seconds=60, id="job-b")
    monkeypatch.setattr(wiring, "reconciler_job", lambda: {"ok": True})
    monkeypatch.setattr(integration, "record_scheduler_inventory", lambda ids: seen.extend(ids))
    assert wiring.reconciler_with_inventory_job(scheduler) == {"ok": True}
    assert sorted(seen) == ["job-a", "job-b"]


def test_aggregation_flag_off_never_opens_database(monkeypatch):
    monkeypatch.setattr(
        integration,
        "policy_provider",
        lambda: {
            "policy_version": 1,
            "policy": deepcopy(VALID_POLICY),
            "effective_flags": deepcopy(VALID_POLICY["feature_flags"]),
        },
    )
    assert integration.aggregation_refresh_job() == {"enabled": False, "refreshed": 0}


def test_policy_shape_drift_is_rejected(monkeypatch):
    raw = {
        "policy_version": 1,
        "policy": deepcopy(VALID_POLICY),
        "effective_flags": deepcopy(VALID_POLICY["feature_flags"]),
    }
    raw["policy"]["platforms"][0]["unexpected"] = True
    monkeypatch.setattr("services.geo_observation.policy.get_policy", lambda: raw)
    with pytest.raises(Exception):
        integration.DatabaseObservationPolicyReader().get_policy()


class _SchemaCursor:
    def __init__(
        self,
        *,
        weak_constraint: bool = False,
        wrong_index: bool = False,
        wrong_default: bool = False,
    ):
        self.weak_constraint = weak_constraint
        self.wrong_index = wrong_index
        self.wrong_default = wrong_default
        self.rows = []

    def execute(self, query, params):
        if "information_schema.columns" in query:
            table = params[0]
            self.rows = []
            for name, (data_type, max_length, nullable, default) in (
                integration._REQUIRED_COLUMN_SPECS[table].items()
            ):
                if default == "__sequence__":
                    default = f"nextval('{table}_{name}_seq'::regclass)"
                if self.wrong_default and table == "geo_observation_insight_jobs" and name == "attempts":
                    default = "1"
                self.rows.append(
                    (name, data_type, max_length, "YES" if nullable else "NO", default)
                )
            return
        if "FROM pg_constraint" in query:
            self.rows = []
            for (table, name), (kind, columns, definition) in (
                integration._REQUIRED_CONSTRAINT_SPECS.items()
            ):
                if self.weak_constraint and name == "geo_ai_surface_cost_ledger_amount_nonneg":
                    definition = "CHECK (true)"
                self.rows.append((table, name, kind, True, definition, list(columns)))
            return
        if "FROM pg_index" in query:
            self.rows = []
            for (table, name), (unique, columns, predicate) in (
                integration._REQUIRED_INDEX_SPECS.items()
            ):
                if self.wrong_index and name == "idx_ai_surface_cost_ledger_src_time":
                    columns = ("recorded_at",)
                self.rows.append(
                    (table, name, unique, True, True, list(columns), predicate)
                )
            return
        raise AssertionError(f"unexpected schema query: {query}")

    def fetchall(self):
        return self.rows


def test_integration_schema_contract_accepts_exact_catalog_shape():
    integration.verify_integration_schema(_SchemaCursor())


def test_integration_schema_contract_rejects_same_name_weakened_check():
    with pytest.raises(RuntimeError, match="约束不符"):
        integration.verify_integration_schema(_SchemaCursor(weak_constraint=True))


def test_integration_schema_contract_rejects_same_name_wrong_index():
    with pytest.raises(RuntimeError, match="索引不符"):
        integration.verify_integration_schema(_SchemaCursor(wrong_index=True))


def test_integration_schema_contract_rejects_wrong_column_default():
    with pytest.raises(RuntimeError, match="默认值不符"):
        integration.verify_integration_schema(_SchemaCursor(wrong_default=True))


def test_single_observation_migration_owns_all_unified_tables():
    from db.migration_manifest import MIGRATIONS

    observation_migrations = [item for item in MIGRATIONS if "geo_observation" in item]
    assert observation_migrations == [
        "scripts/migration_geo_observation_v1_2026_07_17.sql",
        "scripts/migration_geo_observation_collection_mode_2026_07_20.sql",
        "scripts/migration_geo_observation_aggregate_basis_2026_07_20.sql",
    ]
    sql = (ROOT / observation_migrations[0]).read_text(encoding="utf-8")
    for table in integration._REQUIRED_COLUMN_SPECS:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    mode_sql = (ROOT / observation_migrations[1]).read_text(encoding="utf-8")
    assert "existing_collectors_reconciled" in mode_sql
    assert "native_sampling_driver" in mode_sql


def test_frontend_enhances_existing_monitoring_and_diagnosis_routes():
    app = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
    monitoring = (ROOT / "frontend/src/pages/Monitoring/index.tsx").read_text(encoding="utf-8")
    diagnosis = (ROOT / "frontend/src/pages/Diagnosis/DiagnosisReport.tsx").read_text(encoding="utf-8")

    assert 'path="observation"' not in app
    assert 'path="monitoring/methodology"' in app
    assert "MonitoringObservationWorkbench" in monitoring
    assert "unavailableFallback" in monitoring
    assert "LegacyInsightsCenter" in monitoring
    # Production SSOT keeps the paid monitoring workflow as the default view.
    # Unified observation remains an explicit, user-selected analysis tab and
    # an asynchronous identity refresh must never switch the page underneath
    # an operator.
    assert "const [showInsights, setShowInsights] = useState(false)" in monitoring
    assert "selectMonitoringView(true)" in monitoring
    assert "setShowInsights(agentOrAdmin && !sandboxActive)" not in monitoring
    assert "监测执行" in monitoring
    assert "DiagnosisRecommendationBehavior" in diagnosis
    assert "hideWhenUnavailable" in diagnosis


def test_admin_routers_do_not_collide_and_rbac_uses_admin_precedence():
    from api.geo_observation_product_api import build_routers
    from auth.module_mapping import resolve_permission
    from services.geo_observation.wiring import get_admin_router

    _, analytics_admin = build_routers(integration.build_product_api_deps())
    route_keys: list[tuple[str, str]] = []
    for router in (get_admin_router(), analytics_admin):
        for route in router.routes:
            route_keys.extend((method, route.path) for method in route.methods or ())

    assert len(route_keys) == len(set(route_keys))
    assert resolve_permission("/api/geo-observation/brands/1/summary") is None
    assert resolve_permission("/api/admin/geo-observation/overview") == "users"
