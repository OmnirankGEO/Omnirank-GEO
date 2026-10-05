from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_weight_matrix_covers_all_unified_monitoring_platforms():
    """[P0-2 2026-07-26] 统一五引擎后权重矩阵覆盖五个平台。

    旧断言是"恢复 kimi 且不含 yuanbao"（四路时代）。Owner 已裁决诊断与监测
    统一五引擎，元宝进监测矩阵，因此权重必须覆盖它 —— 否则该平台的出现率
    在加权口径里等于被吞掉。保留的强度：完整覆盖 + 归一到 1 + 缺失项补齐。
    """
    from config.ai_engines import MONITORING_ENGINES
    from db.monitoring_db import normalize_active_platform_weights

    weights = normalize_active_platform_weights(
        {"doubao": 0.35, "dashscope": 0.30, "deepseek": 0.25, "kimi": 0.10}
    )

    assert set(weights) == set(MONITORING_ENGINES)
    assert weights["kimi"] > 0
    assert weights["yuanbao"] > 0          # 缺失项按默认补齐，不被别人顶替
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_monitoring_api_fills_only_executable_platform_slots():
    """历史 classic4 配置仍按购买时的四路投影（不追溯改写已售口径）。"""
    from api.monitoring_api import _fill_engine_slots

    configured = "dashscope,deepseek,kimi,doubao"
    assert [row["platform"] for row in _fill_engine_slots([], configured)] == [
        "dashscope",
        "deepseek",
        "kimi",
        "doubao",
    ]


def test_platform_weight_read_uses_database_value_across_workers(monkeypatch):
    from api import monitoring_api

    monkeypatch.setattr(
        monitoring_api,
        "get_saved_platform_weights",
        lambda: {
            "weights": {
                "doubao": 0.35,
                "dashscope": 0.30,
                "deepseek": 0.25,
                "kimi": 0.10,
            },
            "source": "admin",
        },
    )
    monkeypatch.setitem(monitoring_api.PLATFORM_WEIGHTS, "doubao", 0.99)

    result = monitoring_api.api_get_platform_weights()

    from config.ai_engines import MONITORING_ENGINES

    assert result["source"] == "admin"
    # [P0-2 2026-07-26] 五引擎统一：admin 存的四路权重读出来后按默认补齐并重归一，
    #   所以键集合 = 全量平台，具体数值不再等于存入的原始值（归一化后的占比）。
    assert set(result["weights"]) == set(MONITORING_ENGINES)
    assert result["weights"]["doubao"] > result["weights"]["kimi"]
    assert abs(sum(result["weights"].values()) - 1.0) < 1e-9


def test_scheduled_monitoring_reads_persisted_platform_config():
    source = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_run_brand"
    )
    segment = ast.get_source_segment(source, target) or ""

    assert "get_monitoring_config(brand_id=brand_id" in segment
    assert "DEFAULT_MONITORING_PLATFORMS" in segment
    assert '["dashscope", "deepseek", "kimi", "doubao"]' not in segment


def test_new_keywords_persist_resolved_platform_matrix():
    source = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "add_keyword"
    )
    segment = ast.get_source_segment(source, target) or ""

    assert "target_rate," in segment
    assert "platforms, status, monitoring_query" in segment
    assert "target_rate, platforms" in segment


def test_yuanbao_default_migration_is_manifested_and_preserves_history_tables():
    from db.migration_manifest import MIGRATIONS

    migration = "scripts/migration_monitoring_yuanbao_default_2026_07_20.sql"
    assert migration in MIGRATIONS
    sql = (ROOT / migration).read_text(encoding="utf-8")
    assert "monitoring_platform_matrix_backup_20260720" in sql
    assert "dashscope,deepseek,doubao,yuanbao" in sql
    assert "UPDATE monitoring_results" not in sql
    assert "UPDATE monitoring_reports" not in sql


def test_monitoring_product_matrix_restoration_is_manifested_and_narrow():
    from db.migration_manifest import MIGRATIONS

    migration = "scripts/migration_monitoring_product_matrix_2026_07_21.sql"
    assert migration in MIGRATIONS
    sql = (ROOT / migration).read_text(encoding="utf-8")
    assert "monitoring_product_platform_matrices" in sql
    assert "monitoring_product_version" in sql
    assert "dashscope,deepseek,kimi,doubao" in sql
    assert "public.monitoring_platform_matrix_backup_20260720" in sql
    assert "monitoring_platform_matrix_restore_backup_20260721" not in sql
    assert "backup.source_table = 'client_keywords'" in sql
    assert "backup.source_table = 'extra_keywords'" in sql
    assert "backup.source_table = 'monitoring_config'" in sql
    assert "backup.source_id = target.id" in sql
    assert "backup.old_value = 'dashscope,deepseek,kimi,doubao'" in sql
    assert "target.platforms = 'dashscope,deepseek,doubao,yuanbao'" in sql
    assert "target.default_platforms = 'dashscope,deepseek,doubao,yuanbao'" in sql
    assert "SET LOCAL search_path = pg_catalog, public" in sql
    assert "btrim(regexp_replace(p.prosrc" in sql
    assert "UPDATE monitoring_results" not in sql
    assert "UPDATE monitoring_reports" not in sql
