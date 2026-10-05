"""V3.3.1 Codex 三审反馈对应的回归测试

仅做 unit/static/contract 级别 · 不连真 DB(staging E2E 由 Deploy-CTO 跑)

覆盖:
- A. consume_invite_code 注册接入(P0-1)· 验签名 + 异常类
- B. 提现 advisory lock + partial unique(P1-5)· 验 SQL 包含
- C. partial_approve FIFO 拆分(P1-7)· 验 SQL 包含
- D. 大额转换 approve_large_conversion 闭环(P1-3)· 验函数存在 + skip_quota
- E. _point_tx_has_source schema 兼容(P0-3)· 验 helper 存在
- F. failed_service_fee_jobs 补偿表 + cron(P1-4)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ============================================
# A. consume_invite_code 注册接入(P0-1)
# ============================================

def test_consume_invite_code_signature_and_exceptions():
    """注册接入要求:identity_service.consume_invite_code 必须导出 · auth_api 引用它"""
    from services.identity_service import (
        consume_invite_code, verify_invite_code, issue_invite_code,
        InviteCodeError, InviteCodeExpiredError, InviteCodeRevokedError,
        InviteCodeNotFoundError, InviteCodeQuotaExceededError,
    )
    # 函数签名验证
    import inspect
    sig = inspect.signature(consume_invite_code)
    assert "code" in sig.parameters
    assert "invitee_user_id" in sig.parameters
    # 异常类继承链
    assert issubclass(InviteCodeExpiredError, InviteCodeError)
    assert issubclass(InviteCodeRevokedError, InviteCodeError)
    assert issubclass(InviteCodeNotFoundError, InviteCodeError)
    assert issubclass(InviteCodeQuotaExceededError, InviteCodeError)


def test_auth_api_consumes_invite_code_when_v3_3_1_enabled():
    """auth_api 注册流程必须包含 consume_invite_code 调用(P0-1)"""
    auth_api_path = Path(__file__).resolve().parents[2] / "api" / "auth_api.py"
    content = auth_api_path.read_text(encoding="utf-8")
    # 必须 import & 调用
    assert "consume_invite_code" in content, \
        "auth_api 未引用 consume_invite_code · V3.3.1 邀请码注册不通"
    assert "is_v3_3_1_enabled" in content, \
        "auth_api 缺 V3.3.1 总开关判断"
    assert "fallback" in content.lower() or "bind_referral" in content, \
        "auth_api 缺 V3.3.1 失败 fallback 老 bind_referral 兼容"


# ============================================
# B. 提现 advisory lock + partial unique(P1-5)
# ============================================

def test_request_withdrawal_uses_advisory_lock():
    """提现入口必须用 pg_advisory_xact_lock 防 user 并发"""
    engine_path = Path(__file__).resolve().parents[2] / "services" / "service_fee_engine.py"
    content = engine_path.read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in content, \
        "request_withdrawal 缺 PG advisory lock · ToCToU 频次缝仍在"
    assert "ADVISORY_LOCK_NAMESPACE" in content, \
        "缺 advisory lock namespace 命名"


def test_migration_has_partial_unique_for_pending_withdrawal():
    """migration 必须有 partial unique 兜底 advisory lock"""
    mig_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    content = mig_path.read_text(encoding="utf-8")
    assert "uniq_settlements_one_pending_withdrawal_per_user" in content, \
        "migration 缺 partial unique 兜底 user-level 提现并发"


# ============================================
# C. partial_approve FIFO 拆分(P1-7)
# ============================================

def test_partial_approve_fifo_split_logic():
    """部分审核 FIFO 消化 + 剩余回 settled · 不是全部 withdrawn"""
    review_path = Path(__file__).resolve().parents[2] / "api" / "service_fee_review_api.py"
    content = review_path.read_text(encoding="utf-8")
    # 关键关键词:partial_amount + 剩余 settled + 拆分
    assert "partial_amount" in content
    assert "partial_remainder_back_to_settled" in content, \
        "partial_approve 缺剩余金额回 settled 的逻辑"
    assert "FIFO" in content or "ORDER BY id ASC" in content


# ============================================
# D. 大额转换 approve 单订单闭环(P1-3 + P1-2)
# ============================================

def test_approve_large_conversion_exists_and_skips_quota():
    """approve_large_conversion 必须存在 + 跳过 quota 检查"""
    from services.service_fee_engine import (
        approve_large_conversion, reject_large_conversion,
        convert_service_fee_to_points,
    )
    import inspect
    # P1-2:convert_service_fee_to_points 有 skip_quota_check 参数
    sig = inspect.signature(convert_service_fee_to_points)
    assert "skip_quota_check" in sig.parameters, \
        "convert_service_fee_to_points 缺 skip_quota_check 参数 · 审批后无法绕 quota"

    # P1-3:approve_large_conversion 必须存在
    sig_approve = inspect.signature(approve_large_conversion)
    assert "review_id" in sig_approve.parameters
    assert "reviewer_id" in sig_approve.parameters

    sig_reject = inspect.signature(reject_large_conversion)
    assert "review_id" in sig_reject.parameters
    assert "reviewer_id" in sig_reject.parameters


def test_decide_conversion_review_uses_single_order_path():
    """decide_conversion_review 必须调 approve_large_conversion · 不再调 convert_service_fee_to_points 生成第二条单"""
    review_path = Path(__file__).resolve().parents[2] / "api" / "service_fee_review_api.py"
    content = review_path.read_text(encoding="utf-8")
    assert "approve_large_conversion" in content, \
        "decide_conversion_review 未调 approve_large_conversion · 仍走 convert_service_fee_to_points 产生两张单"
    assert "reject_large_conversion" in content


def test_request_large_conversion_status_pending_review():
    """request_large_conversion 写 status='pending_review' · 不是 'completed' 占位"""
    engine_path = Path(__file__).resolve().parents[2] / "services" / "service_fee_engine.py"
    content = engine_path.read_text(encoding="utf-8")
    # 找 request_large_conversion 函数体 · 应见 'pending_review' status
    idx = content.find("def request_large_conversion")
    assert idx > 0
    body = content[idx: idx + 2000]
    assert "'pending_review'" in body, \
        "request_large_conversion 仍用 'completed' 状态(占位单)· 应改 'pending_review'"


# ============================================
# E. _point_tx_has_source schema 兼容(P0-3)
# ============================================

def test_insert_transaction_has_schema_detection():
    """insert_transaction 必须有 _point_tx_has_source 检测 · 兼容 migration 未跑"""
    wallet_db_path = Path(__file__).resolve().parents[2] / "db" / "wallet_db.py"
    content = wallet_db_path.read_text(encoding="utf-8")
    assert "_point_tx_has_source" in content, \
        "insert_transaction 缺 schema 检测 · migration 未跑会爆 UndefinedColumn"
    assert "information_schema.columns" in content, \
        "schema 检测应查 information_schema"


def test_insert_transaction_signature_has_source():
    """insert_transaction 必须有 source: str = None 参数(向后兼容)"""
    from db.wallet_db import insert_transaction
    import inspect
    sig = inspect.signature(insert_transaction)
    assert "source" in sig.parameters
    assert sig.parameters["source"].default is None, \
        "source 参数必须默认 None · 老调用方向后兼容"


# ============================================
# F. failed_service_fee_jobs 补偿表 + cron(P1-4)
# ============================================

def test_failed_service_fee_jobs_table_in_migration():
    """failed_service_fee_jobs 表必须由 migration_007 创建"""
    mig_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    content = mig_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS failed_service_fee_jobs" in content, \
        "migration_007 缺 failed_service_fee_jobs 补偿表"
    assert "retry_count" in content
    assert "max_retries" in content
    assert "abandoned" in content


def test_referral_api_writes_to_failed_jobs():
    """referral_api hook 异常时必须写 failed_service_fee_jobs"""
    ref_path = Path(__file__).resolve().parents[2] / "api" / "referral_api.py"
    content = ref_path.read_text(encoding="utf-8")
    assert "failed_service_fee_jobs" in content, \
        "referral_api hook 异常时未写补偿队列"


def test_cron_includes_failed_jobs_retry():
    """service_fee_cron 必须有 service_fee_failed_jobs_retry 函数 + 注册"""
    cron_path = Path(__file__).resolve().parents[2] / "services" / "service_fee_cron.py"
    content = cron_path.read_text(encoding="utf-8")
    assert "service_fee_failed_jobs_retry" in content, \
        "service_fee_cron 缺补扫 cron · failed_jobs 永不重试"
    assert "v3_3_1_failed_jobs_retry" in content, \
        "scheduler 未注册补扫 cron job id"


def test_service_fee_async_jobs_use_sync_scheduler_boundary(monkeypatch):
    """BackgroundScheduler must never receive a raw coroutine function."""
    import inspect
    from apscheduler.schedulers.background import BackgroundScheduler
    import config.v3_3_1_flags as flags
    from services.service_fee_cron import register_v3_3_1_jobs

    monkeypatch.setattr(flags, "get_flag", lambda _key: "any")
    scheduler = BackgroundScheduler()
    register_v3_3_1_jobs(scheduler)

    job_ids = (
        "v3_3_1_service_fee_t3_settle",
        "v3_3_1_bonus_t7_settle",
        "v3_3_1_anomaly_check",
        "v3_3_1_quota_init_monthly",
        "v3_3_1_failed_jobs_retry",
    )
    assert all(scheduler.get_job(job_id) is not None for job_id in job_ids)
    assert all(
        not inspect.iscoroutinefunction(scheduler.get_job(job_id).func)
        for job_id in job_ids
    ), "APScheduler thread executor received a raw coroutine function"


# ============================================
# G. invite_codes 写在 migration · 而不是 identity_service.ensure_*()
# ============================================

def test_invite_codes_table_in_migration():
    """invite_codes 表必须由 migration_007 创建 · 不靠 ensure_*() 临时建"""
    import re
    mig_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    content = mig_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS invite_codes" in content, \
        "migration_007 缺 invite_codes 新表"
    assert "BIGSERIAL PRIMARY KEY" in content
    assert "used_by_user_id" in content
    # 用宽松正则匹配 code 列(允许多空格对齐)
    assert re.search(r"code\s+VARCHAR\(40\)\s+UNIQUE\s+NOT NULL", content), \
        "invite_codes.code 列定义缺失 / 缺 UNIQUE NOT NULL"


def test_identity_service_uses_invite_codes_not_referral_codes():
    """identity_service 所有读写都用 invite_codes · 不用老 referral_codes"""
    id_path = Path(__file__).resolve().parents[2] / "services" / "identity_service.py"
    content = id_path.read_text(encoding="utf-8")
    # 不应再 INSERT/SELECT/UPDATE referral_codes
    assert "INSERT INTO referral_codes" not in content, \
        "identity_service 仍写 referral_codes(老表无 id 字段会爆 RETURNING)"
    assert "FROM referral_codes" not in content, \
        "identity_service 仍读 referral_codes"
    assert "INSERT INTO invite_codes" in content, \
        "identity_service 未迁到 invite_codes"


# ============================================
# H. Codex 四审反馈(P1-1/P1-2/P1-3)
# ============================================

def test_invite_code_already_used_error_exists():
    """Codex 四审 P1-1:InviteCodeAlreadyUsedError 必须存在 + 是 InviteCodeError 子类"""
    from services.identity_service import InviteCodeAlreadyUsedError, InviteCodeError
    assert issubclass(InviteCodeAlreadyUsedError, InviteCodeError)
    assert InviteCodeAlreadyUsedError.code == "invite_code_already_used"
    assert InviteCodeAlreadyUsedError.http_status == 409


def test_auth_api_narrow_fallback_only_not_found():
    """Codex 四审 P1-1:auth_api fallback 必须严选 · 仅 NotFound 走 fallback · 其他不 fallback"""
    auth_api_path = Path(__file__).resolve().parents[2] / "api" / "auth_api.py"
    content = auth_api_path.read_text(encoding="utf-8")
    # 必须 import 4 个细分异常类
    for cls in ("InviteCodeNotFoundError", "InviteCodeExpiredError",
                "InviteCodeRevokedError", "InviteCodeAlreadyUsedError"):
        assert cls in content, f"auth_api 未 import {cls} · fallback 范围限缩失败"
    # 必须有"不 fallback"语义注释
    assert "不 fallback" in content or "不绑定" in content, \
        "auth_api fallback 限缩注释缺失"
    # v3_handled 标记必须存在(区分 NotFound 走 fallback vs 其他不 fallback)
    assert "v3_handled" in content, \
        "auth_api 缺 v3_handled 标记区分 fallback 路径"


def test_instant_trial_bonus_flag_exists_and_default_true():
    """Codex 四审 P1-2:V3_3_1_DISABLE_INSTANT_TRIAL_BONUS flag 默认 true(V3.3.1 启用时跳 3888)"""
    from config import v3_3_1_flags
    # 默认值 true
    assert v3_3_1_flags._FLAG_DEFAULTS.get("V3_3_1_DISABLE_INSTANT_TRIAL_BONUS") is True, \
        "DISABLE_INSTANT_TRIAL_BONUS 默认必须 true · V3.3.1 启用前必须 skip 3888 防套利"
    # helper 函数存在
    assert hasattr(v3_3_1_flags, "is_instant_trial_bonus_disabled")


def test_auth_api_gates_grant_trial_bonus():
    """Codex 四审 P1-2:auth_api.register 必须 gate grant_trial_bonus 调用"""
    auth_api_path = Path(__file__).resolve().parents[2] / "api" / "auth_api.py"
    content = auth_api_path.read_text(encoding="utf-8")
    assert "is_instant_trial_bonus_disabled" in content, \
        "auth_api 未 gate grant_trial_bonus · V3.3.1 启用时仍即时发 3888 套利风险"
    assert "skip_instant_bonus" in content, \
        "auth_api 缺 skip_instant_bonus 判断变量"


def test_cron_role_gate_in_register():
    """Codex 四审 P1-3:cron 注册必须有 ROLE gate 防蓝绿双跑"""
    cron_path = Path(__file__).resolve().parents[2] / "services" / "service_fee_cron.py"
    content = cron_path.read_text(encoding="utf-8")
    assert "V3_3_1_CRON_ROLE_GATE" in content, \
        "register_v3_3_1_jobs 未读 V3_3_1_CRON_ROLE_GATE flag · 蓝绿环境双跑风险"
    assert "ROLE" in content and "os.environ" in content, \
        "register_v3_3_1_jobs 未读 env ROLE"
    assert "拒绝注册" in content or "mismatch" in content.lower(), \
        "register_v3_3_1_jobs 缺 ROLE mismatch 时拒绝注册的逻辑"


def test_role_gate_flag_in_migration():
    """V3_3_1_CRON_ROLE_GATE 必须在 migration 中作为 system_settings 默认值"""
    mig_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    content = mig_path.read_text(encoding="utf-8")
    assert "V3_3_1_CRON_ROLE_GATE" in content, \
        "migration_007 缺 V3_3_1_CRON_ROLE_GATE 默认 system_settings"
    assert "V3_3_1_DISABLE_INSTANT_TRIAL_BONUS" in content, \
        "migration_007 缺 V3_3_1_DISABLE_INSTANT_TRIAL_BONUS 默认 system_settings"


def test_cron_role_gate_in_flag_defaults():
    """Codex 五审 P0-1:V3_3_1_CRON_ROLE_GATE 必须在 _FLAG_DEFAULTS · 否则 env override 不生效

    Codex 实测:四审版本只在 migration system_settings 加 · 没加 _FLAG_DEFAULTS
    get_flag(unknown_key) 直接 return None · env 设了根本不走 · ROLE gate 永远 fallback any
    """
    from config import v3_3_1_flags
    assert "V3_3_1_CRON_ROLE_GATE" in v3_3_1_flags._FLAG_DEFAULTS, \
        "V3_3_1_CRON_ROLE_GATE 必须在 _FLAG_DEFAULTS · 否则 get_flag 走 unknown key 直接返 None"
    assert v3_3_1_flags._FLAG_DEFAULTS["V3_3_1_CRON_ROLE_GATE"] == "any", \
        "default 应是 'any'(兼容老行为)"


def test_cron_role_gate_env_override_works(monkeypatch):
    """Codex 五审 P0-1:env override 必须真实生效"""
    from config import v3_3_1_flags
    monkeypatch.setattr(v3_3_1_flags, "_read_from_db", lambda key: None)
    v3_3_1_flags.clear_cache()

    # 默认 any
    monkeypatch.delenv("V3_3_1_CRON_ROLE_GATE", raising=False)
    v3_3_1_flags.clear_cache()
    assert v3_3_1_flags.get_flag("V3_3_1_CRON_ROLE_GATE") == "any"

    # env override primary
    monkeypatch.setenv("V3_3_1_CRON_ROLE_GATE", "primary")
    v3_3_1_flags.clear_cache()
    assert v3_3_1_flags.get_flag("V3_3_1_CRON_ROLE_GATE") == "primary", \
        "env override V3_3_1_CRON_ROLE_GATE=primary 未生效 · 防双跑门禁失效"

    # env override active
    monkeypatch.setenv("V3_3_1_CRON_ROLE_GATE", "active")
    v3_3_1_flags.clear_cache()
    assert v3_3_1_flags.get_flag("V3_3_1_CRON_ROLE_GATE") == "active"


def test_migrate_verify_uses_point_transactions_not_wallet_transactions():
    """Codex 六审 P0:verify SQL 必须查 point_transactions(不是不存在的 wallet_transactions)"""
    from db import migrate_v3_3_1
    all_source_queries = [q for q in migrate_v3_3_1.VERIFY_QUERIES if "source 字段" in q[0]]
    assert len(all_source_queries) >= 1, "verify 缺 source 字段校验项"
    for q in all_source_queries:
        sql = q[1]
        assert "wallet_transactions" not in sql, \
            f"verify 仍查 wallet_transactions(不存在的表): {q[0]}"


def test_migrate_verify_source_required_vs_optional_split():
    """Codex 七审 P1:source verify 必须拆"必选 2 表"+"可选 subscription_orders"

    防 recharge_orders.source 缺失被 subscription_orders 凑数掩盖
    """
    from db import migrate_v3_3_1
    source_queries = [q for q in migrate_v3_3_1.VERIFY_QUERIES if "source 字段" in q[0]]
    assert len(source_queries) == 2, \
        f"source 校验必须拆 2 项 · 实际 {len(source_queries)} 项"

    # 必选项:点 transactions + recharge_orders
    required_item = next((q for q in source_queries if "必选" in q[0]), None)
    assert required_item is not None, "缺必选 source 字段项"
    assert "point_transactions" in required_item[1] and "recharge_orders" in required_item[1], \
        "必选 source 项必须含 point_transactions + recharge_orders"
    assert "subscription_orders" not in required_item[1], \
        "必选 source 项不应含 subscription_orders(可选)"
    assert required_item[2] == 2, f"必选 expected 应是 2 · 实际 {required_item[2]}"
    # 必选项 required 默认 True(3-tuple)或显式 True
    assert len(required_item) == 3 or required_item[3] is True, \
        "必选 source 项 required 必须 True"

    # 可选项:subscription_orders
    optional_item = next((q for q in source_queries if "可选" in q[0]), None)
    assert optional_item is not None, "缺可选 source 字段项"
    assert "subscription_orders" in optional_item[1]
    assert "point_transactions" not in optional_item[1] and "recharge_orders" not in optional_item[1]
    assert len(optional_item) == 4 and optional_item[3] is False, \
        "可选 source 项 required 必须 False(4-tuple 第 4 元素 False)"


def test_verify_function_handles_optional_items():
    """verify() 函数必须支持 4-tuple(name, sql, expected, required)"""
    from db import migrate_v3_3_1
    import inspect
    src = inspect.getsource(migrate_v3_3_1.verify)
    assert "required" in src, "verify() 未处理 required 标记"
    assert "len(item) == 4" in src or "len(item)" in src, \
        "verify() 必须根据 tuple 长度区分 required / optional"


def test_migration_sql_comment_uses_point_transactions():
    """Codex 七审 P2:SQL 文件底部验收注释必须用 point_transactions · 不能误导 Deploy-CTO"""
    mig_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    content = mig_path.read_text(encoding="utf-8")
    # 找到底部"验证 SQL"注释段
    idx = content.rfind("验证 SQL")
    assert idx > 0, "migration 缺验证 SQL 注释段"
    comment_section = content[idx:]
    # 底部注释不应再出现 wallet_transactions
    assert "wallet_transactions" not in comment_section, \
        "migration 底部验收注释仍写 wallet_transactions · 会误导 Deploy-CTO 手工查"
    # 必须提到 point_transactions
    assert "point_transactions" in comment_section, \
        "migration 底部验收注释必须含 point_transactions"
    # 应说明 subscription_orders 是可选
    assert "可选" in comment_section or "optional" in comment_section.lower(), \
        "migration 底部注释应说明 subscription_orders 是可选(V3.1 未部时不存在)"


def test_staging_e2e_plan_exists():
    """Codex 四审 P2-4:STAGING_E2E_PLAN.md 必须存在 · 列出真 DB 11 类场景"""
    plan_path = Path(__file__).resolve().parents[0] / "STAGING_E2E_PLAN.md"
    assert plan_path.exists(), "STAGING_E2E_PLAN.md 缺失 · Deploy-CTO 不知道跑哪些 E2E"
    content = plan_path.read_text(encoding="utf-8")
    # 11 类场景标识 A-K
    for letter in "ABCDEFGHIJK":
        assert f"### {letter}." in content, f"STAGING_E2E_PLAN 缺类 {letter}"


# ============================================
# Codex 八审 P0/P2 · roles.display_name NOT NULL + lint 假阳性(2026-05-12 prod --force 实证)
# ============================================

def test_migration_roles_insert_has_display_name_column():
    """Codex 八审 P0 主修:3 处 INSERT INTO roles 必须含 display_name 列
    根因:prod roles 表 display_name TEXT NOT NULL · 漏传触发 NotNullViolation
    """
    import re as _re
    sql_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    content = sql_path.read_text(encoding="utf-8")
    # 找全部 INSERT INTO roles 块(从 INSERT 到分号)
    inserts = _re.findall(
        r"INSERT INTO roles\s*\([^)]+\)\s*VALUES\s*\([^)]+\)[^;]*;",
        content, _re.DOTALL | _re.IGNORECASE
    )
    assert len(inserts) >= 3, f"INSERT INTO roles 数量异常(找到 {len(inserts)} · 预期 ≥ 3)"
    for ins in inserts:
        col_part = _re.search(r"INSERT INTO roles\s*\(([^)]+)\)", ins, _re.IGNORECASE)
        assert col_part, f"无法解析列名: {ins[:80]}"
        cols = [c.strip().lower() for c in col_part.group(1).split(",")]
        assert "display_name" in cols, \
            f"INSERT INTO roles 缺 display_name 列 · 会挂 NotNullViolation:{ins[:120]}"
        assert "name" in cols, f"INSERT INTO roles 异常 · 缺 name 列:{ins[:120]}"


def test_migration_roles_insert_idempotent_on_conflict():
    """Codex 八审 P0 主修:INSERT 必须用 ON CONFLICT DO UPDATE(不再 DO NOTHING)
    原因:DO NOTHING 在 prod 已有 role 但 display_name 仍缺时无法 backfill
    """
    import re as _re
    sql_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    content = sql_path.read_text(encoding="utf-8")
    inserts = _re.findall(
        r"INSERT INTO roles\s*\([^)]+\)\s*VALUES\s*\([^)]+\)[^;]*;",
        content, _re.DOTALL | _re.IGNORECASE
    )
    for ins in inserts:
        assert "ON CONFLICT" in ins.upper(), f"INSERT INTO roles 缺 ON CONFLICT · 重跑会爆 UNIQUE:{ins[:120]}"
        assert "DO UPDATE SET" in ins.upper(), \
            f"INSERT INTO roles 必须 DO UPDATE SET(不是 DO NOTHING)以 backfill display_name:{ins[:120]}"
        assert "display_name" in ins.lower(), \
            f"INSERT INTO roles 的 DO UPDATE 必须更新 display_name:{ins[:120]}"


def test_migrate_lint_uses_regex_not_full_count():
    """Codex 八审 P2:lint 必须用 regex 只数顶层 BEGIN;/COMMIT;
    根因:旧版 sql.upper().count('BEGIN') 把 DO $$ BEGIN ... END 内的 BEGIN 也算了
    报 10/1(实际 1 顶层 BEGIN + 9 DO 块)· 误触 RuntimeError
    """
    py_path = Path(__file__).resolve().parents[2] / "db" / "migrate_v3_3_1.py"
    src = py_path.read_text(encoding="utf-8")
    # 必须导入 re
    assert "import re" in src, "migrate_v3_3_1.py 缺 import re · lint regex 不可用"
    # 必须用 regex 匹配独占一行的 BEGIN;
    assert "re.findall" in src or "re.compile" in src or "re.search" in src, \
        "lint 必须用 re.findall/re.compile/re.search 不能全文 count"
    # 必须出现行首 anchor 的 BEGIN 匹配
    assert "^" in src and ("BEGIN" in src), "lint regex 应有 ^...BEGIN...$ 行首匹配"
    # 不应再有未限定的 sql.upper().count("BEGIN")
    assert 'sql.upper().count("BEGIN")' not in src and \
           "sql.upper().count('BEGIN')" not in src, \
        "lint 仍残留全文 count('BEGIN') · 会被 DO $$ BEGIN 误算"


def test_migrate_lint_on_real_migration_passes():
    """Codex 八审 P2:用真 migration_007 SQL 跑 lint · 应 PASS 不报 BEGIN/COMMIT 不匹配
    覆盖真实场景:1 个顶层 BEGIN; + 1 个顶层 COMMIT; + 多个 DO $$ BEGIN END $$;
    """
    import re as _re
    sql_path = Path(__file__).resolve().parents[2] / "db" / "migration_007_identity_v3_3_1.sql"
    sql = sql_path.read_text(encoding="utf-8")
    # 复现 migrate_v3_3_1.py 的 lint 逻辑
    top_begin = _re.findall(r"(?im)^[\t ]*BEGIN[\t ]*;[\t ]*$", sql)
    top_commit = _re.findall(r"(?im)^[\t ]*COMMIT[\t ]*;[\t ]*$", sql)
    assert len(top_begin) == len(top_commit), \
        f"真 migration_007 顶层 BEGIN/COMMIT 不匹配:{len(top_begin)}/{len(top_commit)}"
    assert len(top_begin) >= 1, "migration_007 必须至少 1 个顶层 BEGIN"
    # 同时验证全文 .count('BEGIN') 比顶层多(证明 DO 块 BEGIN 存在 · regex 已排除)
    n_naive_begin = sql.upper().count("BEGIN")
    assert n_naive_begin > len(top_begin), \
        f"全文 count('BEGIN')={n_naive_begin} 应严格大于 顶层={len(top_begin)} · " \
        f"证明 DO $$ BEGIN 块存在 · 旧 lint 会误报"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
