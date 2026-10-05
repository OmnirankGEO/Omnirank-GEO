"""
P14-v10 · cron 自动跑批后端可配 (C4 cron 后端)

覆盖:
  - cron_enabled/cron_days/cron_hour 在 db seed + config api SCHEMA 注册
  - scheduler_setup 从 DB 读 cron + reschedule + get_next_cron_run_time
  - PUT /config/{key} 改 cron_* 触发 reschedule
  - GET /rounds/cron-status endpoint 注册
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_cron_config_keys_in_seed_and_schema():
    """cron 配置 3 key 必须在 db seed + config api schema 都注册"""
    db_src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    for k in ['cron_enabled', 'cron_days', 'cron_hour']:
        assert f"'{k}'" in db_src, f"db seed 缺 {k}"

    cfg_src = (ROOT / "api" / "research_monitor_config_api.py").read_text(encoding="utf-8")
    for k in ['cron_enabled', 'cron_days', 'cron_hour']:
        assert f"'{k}'" in cfg_src, f"config api schema 缺 {k}"

    # 类型: bool / str / int
    assert "'cron_enabled': {'type': bool" in cfg_src
    assert "'cron_days'" in cfg_src and "'pattern_days': True" in cfg_src
    assert "'cron_hour': {'type': int" in cfg_src


def test_stage3_budget_keys_are_in_typed_config_schema():
    cfg_src = (ROOT / "api" / "research_monitor_config_api.py").read_text(encoding="utf-8")
    for key in (
        'stage3_url_budget_max',
        'stage3_min_urls_per_industry',
        'stage3_min_urls_per_platform',
        'stage3_max_urls_per_domain',
    ):
        assert f"'{key}':" in cfg_src, f"typed config schema missing {key}"


def test_stage3_industry_upgrade_has_its_own_manifest_entry():
    manifest = (ROOT / "db" / "migration_manifest.py").read_text(encoding="utf-8")
    migration = "scripts/migration_stage3_url_budget_industry_name_2026_07_18.sql"
    assert migration in manifest
    assert (ROOT / migration).is_file()


def test_scheduler_reads_from_db_and_supports_reschedule():
    """scheduler_setup 必须从 DB 读 cron 配置 + 提供 reschedule_cron_from_db 函数"""
    from services.research_monitor.scheduler_setup import (
        _load_cron_config_from_db,
        reschedule_cron_from_db,
        get_next_cron_run_time,
    )
    assert callable(_load_cron_config_from_db)
    assert callable(reschedule_cron_from_db)
    assert callable(get_next_cron_run_time)

    cfg = _load_cron_config_from_db()
    assert 'enabled' in cfg and 'days' in cfg and 'hour' in cfg
    assert isinstance(cfg['enabled'], bool)
    assert isinstance(cfg['days'], str)
    assert isinstance(cfg['hour'], int)


def test_config_put_triggers_cron_reschedule():
    """PUT cron_* 配置后必须自动 reschedule · 不依赖 backend 重启"""
    src = (ROOT / "api" / "research_monitor_config_api.py").read_text(encoding="utf-8")
    assert "reschedule_cron_from_db" in src, \
        "PUT /config/{key} 改 cron_* 后必须调 reschedule_cron_from_db"
    assert "'cron_enabled', 'cron_days', 'cron_hour'" in src or \
           '{"cron_enabled", "cron_days", "cron_hour"}' in src, \
        "PUT 必须限定 cron_* key 才触发 reschedule"


def test_rounds_cron_status_endpoint_registered():
    """GET /rounds/cron-status endpoint 必须存在 · UI banner 用"""
    src = (ROOT / "api" / "research_monitor_round_api.py").read_text(encoding="utf-8")
    assert '/rounds/cron-status' in src
    assert "def get_cron_status" in src
    assert "_require_admin(request)" in src  # 必须 admin-only
