"""monitoring lifecycle regression.

2026-06-23 续费履约修正:
  自然日历到期不再代表服务完成。客户必须累计达标满 service_days 后才停。
  因此 run_daily_compliance_check 只跳过未开始/已累计完成,不再因 today >= effective_end 跳过。
"""
from __future__ import annotations
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# v1.8 P0 · run_daily INSERT 前 today 闸门
# ============================================================

def test_v18_run_daily_today_window_guard():
    """run_daily_compliance_check 必须在 INSERT 前判未开始/已累计完成,但不判自然到期。"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 13000]

    # 必须计数 skipped_outside_service_window
    assert "skipped_outside_service_window" in fn_block, \
        "v1.8 P0 破:run_daily 未维护 skipped_outside_service_window 计数"
    # 必须取 today
    assert "date.today()" in fn_block or "_date.today()" in fn_block or "_date_v18.today()" in fn_block, \
        "v1.8 P0 破:run_daily 未取 today 用于服务期判定"
    # 必须有 today < start 判定
    assert re.search(r"today.*<\s*effective_start", fn_block) or \
           re.search(r"effective_start\s*<=\s*today", fn_block) is None and "today_v18" in fn_block, \
        "v1.8 P0 破:run_daily 未判 today < effective_start(未开始)"
    assert not re.search(r"today.*>=\s*effective_end", fn_block), \
        "自然日历到期不能再跳过未完成客户"
    assert "done_days" in fn_block and ">= _svc_days_int" in fn_block, \
        "必须按累计达标天数判断服务完成"


def test_v18_run_daily_skip_before_insert():
    """v1.8 P0 · 过期/未开始必须在 INSERT 前 continue · 不能写当天 log"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 13000]

    # skipped_outside_service_window 引用位置必须在 INSERT INTO keyword_compliance_log 之前
    skip_idx = fn_block.find("skipped_outside_service_window += 1")
    insert_idx = fn_block.find("INSERT INTO keyword_compliance_log")
    assert 0 < skip_idx < insert_idx, \
        "v1.8 P0 破:skipped_outside_service_window 计数未在 INSERT 前(过期仍可能写当天 log)"

    # 计数后必须 continue
    post_skip = fn_block[skip_idx:skip_idx + 200]
    assert "continue" in post_skip, \
        "v1.8 P0 破:skipped_outside_service_window 后未 continue 跳过 INSERT"


def test_v18_run_daily_return_includes_skip_count():
    """v1.8 P0 · 返回 dict 必须含 skipped_outside_service_window(监控/调试)"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 13000]

    assert '"skipped_outside_service_window"' in fn_block, \
        "v1.8 P0 破:run_daily return 缺 skipped_outside_service_window key"


# ============================================================
# v1.8 P1 · SQL 上界统一排他 check_date < effective_end
# ============================================================

def test_v18_v3_algorithm_exclusive_end():
    """v1.8 P1 · _compute_effective_rate_v3 2 处查询上界改排他(< 不是 <=)"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def _compute_effective_rate_v3(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 5000]

    # 2 处必须用 check_date < %s(排他)· 没有 <=
    less_count = fn_block.count("check_date < %s")
    le_count = fn_block.count("check_date <= %s")
    assert less_count >= 2, \
        f"v1.8 P1 破:_compute_effective_rate_v3 上界 < binding < 2(实际 {less_count})"
    # 旧 <= 不应再有(在 effective_end 路径)
    assert le_count == 0 or "v1.7 服务窗口上限" not in fn_block, \
        f"v1.8 P1 破:_compute_effective_rate_v3 仍有 check_date <= %s binding(应排他)"


def test_v18_run_daily_3_queries_exclusive_end():
    """v1.8 P1 · run_daily 3 处 compliance log 查询上界改排他"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 13000]

    # 至少 3 处 check_date < %s(first_compliant / hist_avg / prev_6d)
    less_count = fn_block.count("check_date < %s")
    assert less_count >= 3, \
        f"v1.8 P1 破:run_daily 上界 < binding < 3(实际 {less_count})"
    # 不应残留 <=(在 effective_end 上界路径)
    # 注意:run_daily 也有 CURRENT_DATE - INTERVAL '1 day' 这种 ≤ 用法是 BETWEEN AND 句法 · 跟 effective_end 无关
    # 精确检查:任何 "check_date <= %s" binding 都不该剩(effective_end 都改 <)
    le_count = fn_block.count("check_date <= %s")
    assert le_count == 0, \
        f"v1.8 P1 破:run_daily 仍残留 check_date <= %s binding(应全改排他 ·实际 {le_count})"


def test_v18_summary_cte_has_no_calendar_end_cap():
    """续费履约 · get_keyword_compliance_summary 不再用 effective_end 封顶历史。"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def get_keyword_compliance_summary(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 6000]

    # filtered CTE 不得 check_date < kew.effective_end
    pat_less = re.compile(r"check_date\s*<\s*kew\.effective_end", re.IGNORECASE)
    assert not pat_less.search(fn_block), \
        "summary filtered CTE 不得用自然日历 end 封顶累计历史"
    # 不应有 <=
    pat_le = re.compile(r"check_date\s*<=\s*kew\.effective_end", re.IGNORECASE)
    assert not pat_le.search(fn_block), \
        "v1.8 P1 破:summary CTE 残留 check_date <= kew.effective_end(应排他)"


def test_v18_consistency_with_scheduler():
    """scheduler 与 compliance 均按服务锚/累计达标口径,不按日历上界停服。"""
    src = _read("db/monitoring_db.py")

    # list_active 用服务锚 helper,不能再用 CURRENT_DATE < contract_end。
    list_fn_start = src.find("def list_active_subscriptions()")
    list_block = src[list_fn_start:list_fn_start + 4000]
    assert "quote_service_anchor_condition_sql" in list_block, \
        "list_active_subscriptions 必须复用服务锚 SSOT"
    assert not re.search(r"CURRENT_DATE\s*<\s*\(", list_block), \
        "list_active_subscriptions 不得再按自然日历上界停服"


# ============================================================
# v1.8 整体 · service_window 闭区间下界 + 排他上界(全文件一致)
# ============================================================

def test_v18_no_residual_inclusive_end_in_effective_paths():
    """v1.8 整体 · 全文件 effective_end 相关 SQL 路径 0 个 <= 残留"""
    src = _read("db/monitoring_db.py")

    # 精确扫所有出现 check_date <= effective_end / kew.effective_end / %s 模式
    # 任何 effective_end 上界 SQL 都应排他
    residual_le = [
        m.group(0) for m in re.finditer(
            r"check_date\s*<=\s*(%s|kew\.effective_end|effective_end)",
            src,
        )
    ]
    assert len(residual_le) == 0, \
        f"v1.8 整体破:全文件残留 check_date <= effective_end 类 binding {len(residual_le)} 处:{residual_le[:3]}"
