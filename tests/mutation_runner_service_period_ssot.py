# -*- coding: utf-8 -*-
"""服务期 SSOT · 变异 runner(WO_SERVICE_PERIOD_SSOT_2026-08-06 §1.4 / §3)

每条变异 = 把修复回退成事故前的写法(或拆掉一条接线),
锁必须**当场转红**。全绿才算锁有判别力。

跑法:
    TEST_DATABASE_URL=... python tests/mutation_runner_service_period_ssot.py

🔴 三条自防坑(都是踩过的):
  1. `PYTHONDONTWRITEBYTECODE=1` + 每轮清 `__pycache__` ——
     .pyc 缓存会让"已杀死"被误报成"存活"(单向偏差,比漏报更骗人)。
  2. 写文件一律 tmp + os.replace,且 `newline=""` ——
     `open(path,'w')` 先截断再写,写失败就把源文件清空了(本包踩过一次);
     不指定 newline 会把整个文件改成 CRLF,diff 炸成全文件。
  3. 每条变异**必须点名它该杀死哪个测试**,并核对红的就是那一条 ——
     只看"套件红了"会把"改坏了别的东西"当成"锁生效"。
"""

import io
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCKS = os.path.join("tests", "test_service_period_ssot_2026_08_06.py")

# (名字, 相对路径, 原文, 替换成, 期望被杀死的测试名片段)
MUTATIONS = [
    # ── 后端:把 365 / or 1 兜底加回去 ──
    ("M01 monitoring_db 恢复 COALESCE(service_days,365)",
     "db/monitoring_db.py",
     "                  ), 0) < q.service_days",
     "                  ), 0) < COALESCE(q.service_days, 365)",
     "test_no_coalesce_365"),

    ("M02 monitoring_db CTE 恢复 COALESCE 365",
     "db/monitoring_db.py",
     "                    q.service_days AS service_days,",
     "                    COALESCE(q.service_days, 365) AS service_days,",
     "test_no_coalesce_365"),

    ("M03 monitoring_db 恢复 Python 侧 or 365",
     "db/monitoring_db.py",
     "            service_days = compliance_target_days(r)",
     '            service_days = int(r.get("service_days") or 365)',
     "test_no_or_365"),

    ("M04 offline-mark-paid 恢复 service_months or 1",
     "server.py",
     '            months = quote["service_months"]',
     '            months = quote["service_months"] or 1',
     "test_no_service_months_or_1"),

    # ── 后端:把"日期 + 配额"这口假钟加回去 ──
    ("M05 monitoring_api 恢复 contract_start + service_days",
     "api/monitoring_api.py",
     "    contract_end = _svc_end",
     "    from datetime import timedelta as _td_mut\n"
     "    contract_end = (_contract_start + _td_mut(days=int(quote_service_days))) if _contract_start else None",
     "test_no_date_plus_service_days"),

    ("M06 portal token 恢复 contract_start + service_days",
     "db/monitoring_db.py",
     "    if service_end < today:",
     "    service_end = contract_start + timedelta(days=int(row.get('service_days') or 365))\n"
     "    if service_end < today:",
     "test_no_date_plus_service_days"),

    # ── 后端:换算点扩散 ──
    ("M07 server.py 自己用 relativedelta 算服务期",
     "server.py",
     # [开源 E3 · WO_323 G3a · 2026-10-02] 原锚(一键激活里的服务期换算)随端点删;改指线下激活的同一换算入口
     "            from services.service_period import ServicePeriodError, resolve_activation_period",
     "            from dateutil.relativedelta import relativedelta\n"
     "            _mut_end = date_cls.today() + relativedelta(months=1)\n"
     "            from services.service_period import ServicePeriodError, resolve_activation_period",
     "test_months_to_end_conversion_only_in_ssot_module"),

    # ── 后端:写入点默认值回来 ──
    # [开源 E3 · WO_323 G3a · 2026-10-02] 原锚(一键激活请求模型)随端点删;改指同款必填月数的线下确认请求模型
    ("M08 offline-confirm 恢复 service_months 默认 1",
     "server.py",
     '    service_months: int = Field(..., ge=1, le=24, description="服务月数 1-24 · 必填,系统不猜")\n'
     '    service_start_date: Optional[str] = None  # YYYY-MM-DD',
     '    service_months: int = Field(default=1, ge=1, le=24, description="服务月数 1-24")\n'
     '    service_start_date: Optional[str] = None  # YYYY-MM-DD',
     "test_activation_endpoints_require_explicit_months"),

    ("M09 service-config 恢复写 service_start_date",
     "server.py",
     '            cur.execute("UPDATE quotes SET service_days = %s WHERE id = %s", (service_days, quote_id))',
     '            cur.execute("UPDATE quotes SET service_days = %s, service_start_date = %s WHERE id = %s",\n'
     '                        (service_days, body.get("service_start_date"), quote_id))',
     "test_service_config_no_longer_writes_start_date"),

    # ── 后端:SSOT 模块自己被改坏 ──
    ("M10 normalize_service_months 缺失时兜底 1",
     "services/service_period.py",
     "    if value is None or value == \"\":\n        raise ServicePeriodError(",
     "    if value is None or value == \"\":\n        return 1\n    if False:\n        raise ServicePeriodError(",
     "test_months_missing_raises_not_defaults_to_one"),

    ("M11 read_calendar_period 从 service_days 反推 end",
     "services/service_period.py",
     "    return coerce_date(_row_get(row, \"service_start_date\")), coerce_date(\n        _row_get(row, \"service_end_date\")\n    )",
     "    _s = coerce_date(_row_get(row, \"service_start_date\"))\n"
     "    _e = coerce_date(_row_get(row, \"service_end_date\"))\n"
     "    if _e is None and _s is not None and _row_get(row, \"service_days\"):\n"
     "        _e = _s + timedelta(days=int(_row_get(row, \"service_days\")))\n"
     "    return _s, _e",
     "test_read_calendar_period_never_derives_from_days_or_months"),

    ("M12 compliance_target_days 恢复 365 兜底",
     "services/service_period.py",
     "    raw = _row_get(row, \"service_days\")\n    if raw is None:\n        return None",
     "    raw = _row_get(row, \"service_days\")\n    if raw is None:\n        return 365",
     "test_compliance_target_days_has_no_365_fallback"),

    ("M13 calendar_days_left clamp 到 0(藏起「已过期 N 天」)",
     "services/service_period.py",
     "    return (end_date - (today or date.today())).days",
     "    return max(0, (end_date - (today or date.today())).days)",
     "test_calendar_days_left_is_signed"),

    ("M14 没服务期当成永久有效",
     "services/service_period.py",
     "    left = calendar_days_left(end, today=today)\n    return left is not None and left >= 0",
     "    left = calendar_days_left(end, today=today)\n    return left is None or left >= 0",
     "test_within_period_fail_closed_when_missing"),

    ("M15 compute_service_end 算成 30 天一个月",
     "services/service_period.py",
     "    return start + relativedelta(months=int(months))",
     "    return start + timedelta(days=30 * int(months))",
     "test_compute_service_end_is_calendar_months"),

    ("M16 报错不带出口(违提示铁律)",
     "services/service_period.py",
     "        \"actions\": [\n            {\n                \"label\": \"去设服务期\",",
     "        \"actions\": [] if True else [\n            {\n                \"label\": \"去设服务期\",",
     "test_error_carries_user_message_and_exit"),

    # ── 到期不静默 / 续费桶 / 轮换闸 ──
    ("M17 拆掉 §1.5 内部可见查询",
     "db/monitoring_db.py",
     "def get_service_period_blocked_clients():",
     "def _get_service_period_blocked_clients_disabled():",
     "test_expiry_is_not_silent"),

    ("M18 到期提醒去重键退化(会永久沉默)",
     "api/scheduler.py",
     'terminal_state=f"rotation_blocked:{end or \'not_set\'}",',
     'terminal_state="expiring",',
     "test_expiry_is_not_silent"),

    ("M19 续费桶退回只收 active",
     "api/m3_api.py",
     'lq_service_status in ("active", "expiring", "expired")',
     'lq_service_status == "active"',
     "test_renewal_bucket_includes_expiring_and_expired"),

    ("M20 轮换资格闸的服务期条件被拿掉",
     "db/monitoring_db.py",
     "              AND q.service_end_date >= CURRENT_DATE\n"
     "              AND COALESCE(q.service_status, 'active') NOT IN ('expired', 'paused')\n"
     "              AND EXISTS (\n"
     "                  SELECT 1 FROM confirmed_keywords ck\n"
     "                  WHERE ck.quote_id = q.id\n"
     "                    AND (ck.is_core IS NOT FALSE)\n"
     "                    AND COALESCE(ck.super_red_ocean, FALSE) = FALSE\n"
     "                    AND NOT EXISTS (",
     "              AND COALESCE(q.service_status, 'active') NOT IN ('expired', 'paused')\n"
     "              AND EXISTS (\n"
     "                  SELECT 1 FROM confirmed_keywords ck\n"
     "                  WHERE ck.quote_id = q.id\n"
     "                    AND (ck.is_core IS NOT FALSE)\n"
     "                    AND COALESCE(ck.super_red_ocean, FALSE) = FALSE\n"
     "                    AND NOT EXISTS (",
     "test_rotation_gate_still_reads_service_end_date"),

    # ── 迁移 ──
    ("M21 迁移 028 不再置 NOT NULL",
     "db/migration_028_service_period_ssot_2026_08_06.sql",
     "ALTER TABLE quotes ALTER COLUMN service_days SET NOT NULL;",
     "-- (mutated) no not null",
     "test_migration_028_registered_and_sets_not_null"),

    ("M22 迁移 028 没登记进 manifest",
     "db/migration_manifest.py",
     '    "db/migration_028_service_period_ssot_2026_08_06.sql",',
     '    # "db/migration_028_service_period_ssot_2026_08_06.sql",',
     "test_migration_028_registered_and_sets_not_null"),

    # ── 前端 ──
    ("M23 前端恢复 || 365",
     "frontend/src/pages/Monitoring/components/KeywordTable.tsx",
     "serviceDays={kw.service_days ?? null}",
     "serviceDays={kw.service_days || 365}",
     "test_frontend_no_365_fallback"),

    ("M24 门户恢复 start + service_days 自算到期",
     "frontend/src/pages/Portal/PortalDashboard.tsx",
     "                                    const calendarRemaining = data?.service_remaining_days_natural_signed;",
     "                                    const _e = new Date(start);\n"
     "                                    _e.setDate(_e.getDate() + (data?.service_days ?? 0));\n"
     "                                    const calendarRemaining = data?.service_remaining_days_natural_signed;",
     "test_frontend_never_derives_service_end"),

    # ── 元变异:证明"剥注释"这一步的自检真的会响 ──
    ("M25 剥注释退化成「连字符串一起清空」(会让禁写法扫描恒绿)",
     LOCKS.replace("\\", "/"),
     '            out.append("\\n" if (drop_current and ch == "\\n") else ("" if drop_current else ch))',
     '            out.append("\\n" if ch == "\\n" else "")',
     "test_comment_stripper_has_discriminating_power"),
]


def _read(path):
    with io.open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path, text):
    tmp = path + ".mut.tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _purge_pycache():
    for dirpath, dirnames, _ in os.walk(REPO):
        if "node_modules" in dirpath or ".git" in dirpath:
            continue
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                dirnames.remove(d)


def run_locks():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("TEST_DATABASE_URL", "postgresql://geo_admin:none@127.0.0.1:1/svcperiod_test")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LOCKS, "-q", "-p", "no:cacheprovider", "--tb=no"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    _purge_pycache()
    rc, out = run_locks()
    if rc != 0:
        print("🔴 基线就是红的,变异结果没有意义。先修锁:")
        print(out[-4000:])
        return 2
    print("✅ 基线绿 —— 开始变异\n")

    killed, survived, skipped = [], [], []
    for name, rel, old, new, expect in MUTATIONS:
        path = os.path.join(REPO, rel.replace("/", os.sep))
        if not os.path.exists(path):
            skipped.append((name, "文件不存在"))
            continue
        original = _read(path)
        cnt = original.count(old)
        if cnt != 1:
            # 锚点不唯一 = 这条变异没打到它说要打的地方(不是"实现好",是判据坏)
            skipped.append((name, f"锚点命中 {cnt} 次,应为 1"))
            continue
        try:
            _write(path, original.replace(old, new, 1))
            _purge_pycache()
            rc2, out2 = run_locks()
            if rc2 == 0:
                survived.append((name, expect, ""))
            elif expect not in out2:
                # 套件红了,但红的不是它该杀的那条 → 当作存活(改坏了别的东西不算锁生效)
                survived.append((name, expect, "红的不是点名的那条"))
            else:
                killed.append(name)
        finally:
            _write(path, original)
            _purge_pycache()
        print(f"{'✅ 杀死' if name in killed else '🔴 存活'}  {name}")

    print("\n" + "=" * 72)
    print(f"变异 {len(MUTATIONS)} 条 · 杀死 {len(killed)} · 存活 {len(survived)} · 跳过 {len(skipped)}")
    for n, e, why in survived:
        print(f"  🔴 存活: {n}  (应杀 {e}) {why}")
    for n, why in skipped:
        print(f"  ⚠️  跳过: {n}  ({why})")

    rc3, out3 = run_locks()
    print(f"\n还原后基线复核: {'✅ 绿' if rc3 == 0 else '🔴 红 —— 还原没干净!'}")
    if rc3 != 0:
        print(out3[-2000:])
    return 0 if (not survived and not skipped and rc3 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
