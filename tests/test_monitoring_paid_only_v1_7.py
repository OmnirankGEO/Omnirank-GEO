"""monitoring P0 hotfix v1.7 · 老板复审 5 项 Findings 修(P0×2 + P1×2 + P2×1)

老板复审(2026-05-29 v1.2-1.6 后):
  P0-1 list_active_subscriptions 没过滤服务期结束(过期 quote 仍跑/扣)
  P0-2 compliance 只过滤起点 effective_start · 不过滤终点 effective_end(过期日志仍计履约)
  P1-3 portal 自然日剩余 clamp 到 0 · UI 无法显示"已过期 N 天"
  P1-4 backfill 只 create_kms 没回填 ck.monitoring_subscription_id(状态错位)
  P2-5 create_kms 并发 unique 冲突 → 500(应 reselect existing)
"""
from __future__ import annotations
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _extract_sql_block(fn_block: str) -> str:
    exec_idx = fn_block.find("cur.execute")
    if exec_idx < 0:
        exec_idx = fn_block.find("cursor.execute")
    if exec_idx < 0:
        return ""
    triple = fn_block.find('"""', exec_idx)
    if triple < 0:
        return ""
    close = fn_block.find('"""', triple + 3)
    return fn_block[triple + 3:close] if close > 0 else ""


def _strip_sql_comments(sql: str) -> str:
    return "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))


# ============================================================
# v1.7 P0-1 · list_active_subscriptions 服务锚 SSOT
# ============================================================

def test_v17_list_active_filters_service_end():
    """v1.7/P0 后续修正 · scheduler 不按自然日历停服,改按累计达标天数判断。"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def list_active_subscriptions()")
    # 🔴 [parity 2026-08-16] 原来写死 +4000。parity 把这个函数改成 UNION ALL 两臂
    #   (合同臂 + 手动臂),SQL 被推出窗外 → _extract_sql_block 返空串 →
    #   断言以 `'keyword_compliance_log' in ''` 形态红。**尺子量程不够,不是服务锚没了**。
    #   改成量到函数真边界;下面新增的分臂断言证明服务锚确实还在合同臂上。
    _nx = src.find(chr(10) + "def ", fn_start + 1)
    fn_block = src[fn_start:_nx if _nx > 0 else len(src)]
    sql = _extract_sql_block(fn_block)
    sql_clean = _strip_sql_comments(sql)

    assert "quote_service_anchor_condition_sql" in fn_block, \
        "list_active 必须复用服务锚 SSOT,不能自行写一套到期规则"
    assert "keyword_compliance_log" in sql_clean and "service_days" in sql_clean, \
        "服务锚必须按累计达标天数判断是否完成"
    assert "CURRENT_DATE <" not in sql_clean, \
        "自然日历到期不能再作为停止监测条件"

    # 🔴 [parity 2026-08-16] 分臂验:服务锚与达标天数是**合同臂**的口径,不能被 UNION 稀释。
    #   只查"整个函数里有没有出现过"不够 —— 手动臂在同一个函数里,
    #   两条件若挪到手动臂上,原判据照样绿,而合同词就永远跑不停、一直扣费了。
    arms = sql_clean.split("UNION ALL")
    assert len(arms) == 2, f"期望 UNION ALL 两臂(合同/手动),实测 {len(arms)} 段"
    contract_arm = arms[0]
    assert "keyword_compliance_log" in contract_arm and "service_days" in contract_arm, \
        "服务锚/达标天数不在**合同臂**上 —— 合同词会跑过服务期继续扣费"
    assert "quote_service_anchor_condition_sql" in fn_block.split("UNION ALL")[0], \
        "服务锚 SSOT 不在合同臂上"


# ============================================================
# v1.7 P0-2 · helper 返 (start, end) tuple
# ============================================================

def test_v17_effective_window_helper_exists():
    """v1.7 P0-2 · _get_effective_window helper 必须存在(替代 v1.4 单点 helper)"""
    src = _read("db/monitoring_db.py")
    assert "def _get_effective_window(" in src, \
        "v1.7 P0-2 破:_get_effective_window helper 未定义"


def test_v17_effective_window_returns_tuple():
    """v1.7 P0-2 · helper 必须返 (start, end) 二元组 · paid_at NULL 时返 (None, None)"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def _get_effective_window(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 3000]

    # [服务期 SSOT 2026-08-06] 上界的**来源**变了:从 contract_start + service_days
    #   (把履约达标天数配额当日历天用)改成 quotes.service_end_date。
    #   这条测试原本要守的东西(返二元组 + fail-closed + 真的算了上界)一样不少,
    #   只是断言换成新契约,并反过来钉住"不许再用配额算日历上界"。
    assert "service_end" in fn_block, \
        "v1.7 P0-2 破:helper 未取 quotes.service_end_date"
    assert "effective_end" in fn_block, \
        "v1.7 P0-2 破:helper 未算 effective_end"
    assert "(None, None)" in fn_block, \
        "v1.7 P0-2 破:helper fail-closed 返回未用 (None, None) tuple"
    # 注:"不许再用 service_days 算日历上界"这条**不在这里断言** ——
    #   helper 的 docstring 逐字解释了旧写法,在这里做否定断言会抓到自己写的注释。
    #   该条由 tests/test_service_period_ssot_2026_08_06.py 的一致性锁负责(它先剥注释),
    #   并已被变异 M05/M06 证明有判别力。


def test_v17_legacy_helper_delegates_to_window():
    """v1.7 P0-2 · 旧 _get_effective_start_date 必须保留(backward compat)并 delegate 到 window"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def _get_effective_start_date(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 1500]

    assert "_get_effective_window" in fn_block, \
        "v1.7 P0-2 破:旧 _get_effective_start_date 未 delegate 到 _get_effective_window"


def test_v17_compute_v3_accepts_effective_end():
    """v1.7 P0-2 · _compute_effective_rate_v3 签名加 effective_end + 2 处查询加上界
    v1.8 后:上界改排他 < (不是 <=)· 跟 scheduler CURRENT_DATE < contract_end 口径一致
    """
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def _compute_effective_rate_v3(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 5000]

    assert "effective_end" in fn_block, \
        "v1.7 P0-2 破:_compute_effective_rate_v3 签名缺 effective_end"
    # 2 处 check_date < effective_end binding(v1.8 排他)
    upper_count = fn_block.count("check_date < %s") + fn_block.count("check_date <= %s")
    assert upper_count >= 2, \
        f"v1.7 P0-2 破:_compute_effective_rate_v3 上界 binding < 2(实际 {upper_count})"


def test_v17_run_daily_compliance_check_uses_window():
    """v1.7 P0-2 · run_daily_compliance_check 必须 _get_effective_window + 3 处加上界
    v1.8 后:上界改排他 < (跟 scheduler 一致)
    """
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 13000]

    assert "_get_effective_window" in fn_block, \
        "v1.7 P0-2 破:run_daily_compliance_check 未改用 _get_effective_window"
    # 3 处 check_date < effective_end binding(v1.8 排他)
    upper_count = fn_block.count("check_date < %s") + fn_block.count("check_date <= %s")
    assert upper_count >= 3, \
        f"v1.7 P0-2 破:run_daily_compliance_check 中上界 binding < 3(实际 {upper_count})"


def test_v17_summary_cte_uses_brand_scope_without_calendar_end():
    """续费修正 · get_keyword_compliance_summary 必须按品牌服务锚聚合历史,不按自然日历封顶。
    """
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def get_keyword_compliance_summary(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 6000]

    assert "kw_eff_window" in fn_block, \
        "v1.7 P0-2 破:summary CTE 未改名 kw_eff_window(原 kw_eff_start)"
    assert "seed_quote" in fn_block and "scoped_quotes" in fn_block, \
        "续费场景必须从所选 quote 扩展到同品牌服务锚 quote"
    assert "JOIN scoped_quotes q ON q.id = kcl.quote_id" in fn_block, \
        "历史达标必须按真实 keyword_compliance_log.quote_id 回归服务锚 quote"
    pat = re.compile(r"check_date\s*<=?\s*kew\.effective_end", re.IGNORECASE)
    assert not pat.search(fn_block), \
        "summary 不得再用自然日历 effective_end 封顶累计达标历史"


# ============================================================
# v1.7 P1-3 · portal API signed / overdue / expired
# ============================================================

def test_v17_api_returns_signed_overdue_expired():
    """v1.7 P1-3 · api_get_client_keywords_merged 顶层必须返 3 新字段"""
    src = _read("api/monitoring_api.py")
    fn_start = src.find("def api_get_client_keywords_merged(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 8000]

    assert '"service_remaining_days_natural_signed"' in fn_block, \
        "v1.7 P1-3 破:顶层缺 service_remaining_days_natural_signed(可负)"
    assert '"service_overdue_days"' in fn_block, \
        "v1.7 P1-3 破:顶层缺 service_overdue_days"
    assert '"service_expired"' in fn_block, \
        "v1.7 P1-3 破:顶层缺 service_expired 布尔 flag"
    # 旧字段保留(向后兼容)
    assert '"service_remaining_days_natural"' in fn_block, \
        "v1.7 P1-3 破:旧 service_remaining_days_natural 字段被删(破坏旧前端)"


def test_v17_api_per_keyword_signed_expired():
    """v1.7 P1-3 · per-keyword 也加 signed / overdue / expired(UI 单行展示)"""
    src = _read("api/monitoring_api.py")
    fn_start = src.find("def api_get_client_keywords_merged(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 8000]

    assert 'kw["service_remaining_days_natural_signed"]' in fn_block, \
        "v1.7 P1-3 破:per-keyword 缺 service_remaining_days_natural_signed"
    assert 'kw["service_overdue_days"]' in fn_block, \
        "v1.7 P1-3 破:per-keyword 缺 service_overdue_days"
    assert 'kw["service_expired"]' in fn_block, \
        "v1.7 P1-3 破:per-keyword 缺 service_expired"


def test_v17_signed_field_can_be_negative():
    """v1.7 P1-3 · signed 字段必须 NOT clamp(可负)· overdue 必须 max(0, -signed)"""
    src = _read("api/monitoring_api.py")
    fn_start = src.find("def api_get_client_keywords_merged(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 8000]

    # signed 由 (contract_end - today).days 直接赋值 · 不能 max(0, ...) 包裹
    assert "service_remaining_days_natural_signed = signed" in fn_block, \
        "v1.7 P1-3 破:signed 字段未直接赋(contract_end - today).days 原值"
    # overdue 必须 max(0, -signed)
    assert "max(0, -signed)" in fn_block, \
        "v1.7 P1-3 破:service_overdue_days 未用 max(0, -signed) 算法"
    # expired = signed < 0
    assert "signed < 0" in fn_block, \
        "v1.7 P1-3 破:service_expired 未用 signed < 0 判定"


# ============================================================
# v1.7 P1-4 · backfill 回填 ck.monitoring_subscription_id
# ============================================================

def test_v17_backfill_calls_update_state_after_create():
    """v1.7 P1-4 · backfill apply 后必须调 update_keyword_monitor_state 回填 ck"""
    src = _read("scripts/kms_backfill_2026_05_29.py")

    # 必须 import update_keyword_monitor_state
    assert "update_keyword_monitor_state" in src, \
        "v1.7 P1-4 破:backfill 未 import / 调 update_keyword_monitor_state"
    # 回填语义:is_monitored=True · subscription_id=sub_id
    assert "is_monitored=True" in src, \
        "v1.7 P1-4 破:update_keyword_monitor_state 未传 is_monitored=True"
    assert "subscription_id=sub_id" in src, \
        "v1.7 P1-4 破:update_keyword_monitor_state 未传 subscription_id=sub_id 回填"
    # 报告必须含 ck_synced 字段
    assert '"ck_synced"' in src, \
        "v1.7 P1-4 破:report 缺 ck_synced 字段(用户无从判断回填是否成功)"


def test_v17_backfill_ck_sync_failure_recorded():
    """v1.7 P1-4 · ck 同步失败必须记 error · 不致命(主创建已成功 不能抛)"""
    src = _read("scripts/kms_backfill_2026_05_29.py")

    # try/except 包 update_keyword_monitor_state(ck 失败 → errors 加 stage="ck_sync")
    assert 'stage": "ck_sync"' in src or "stage='ck_sync'" in src or '"ck_sync"' in src, \
        "v1.7 P1-4 破:backfill 未区分 ck_sync 阶段失败(只有 create_sub 失败)"


# ============================================================
# v1.7 P2-5 · create_kms 捕获 unique 冲突 reselect
# ============================================================

def test_v17_create_kms_catches_unique_conflict():
    """v1.7 P2-5 · create_keyword_monitor_subscription 必须 catch UniqueViolation → reselect"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def create_keyword_monitor_subscription(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 5000]

    # 必须 try/except 包 INSERT
    # try/except 块在 INSERT 附近
    insert_idx = fn_block.find("INSERT INTO keyword_monitor_subscriptions")
    assert insert_idx > 0
    # try 关键字必须在 INSERT 之前(包 INSERT 块)
    try_idx = fn_block.rfind("try:", 0, insert_idx)
    assert try_idx > 0, \
        "v1.7 P2-5 破:INSERT 未包 try/except"
    # except 之后必须 re-SELECT
    except_idx = fn_block.find("except", insert_idx)
    assert except_idx > 0
    post_except = fn_block[except_idx:]
    # rollback + reselect
    assert "rollback" in post_except, \
        "v1.7 P2-5 破:unique 冲突未 rollback"
    assert "SELECT id FROM keyword_monitor_subscriptions" in post_except, \
        "v1.7 P2-5 破:unique 冲突未 reselect existing active sub"


def test_v17_create_kms_unique_classification():
    """v1.7 P2-5 · unique 冲突判定要支持 UniqueViolation / IntegrityError / msg keyword 三路"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def create_keyword_monitor_subscription(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 5000]

    # 至少包含 UniqueViolation 或 IntegrityError 关键字 + msg 兜底
    assert "UniqueViolation" in fn_block or "IntegrityError" in fn_block, \
        "v1.7 P2-5 破:unique 判定未含 UniqueViolation/IntegrityError 类名"
    assert "duplicate" in fn_block.lower() or "uniq" in fn_block.lower(), \
        "v1.7 P2-5 破:unique 判定缺 msg 兜底(支持不同 psycopg2 版本)"


# ============================================================
# 整体 · paid-only 服务期上下界全覆盖
# ============================================================

def test_v17_integrated_window_coverage():
    """v1.7 整体 · 4 处 compliance log 读点 + 1 处 scheduler 都引用服务期边界"""
    src = _read("db/monitoring_db.py")

    # scheduler 引用服务锚 SSOT(累计达标履约)
    list_fn_start = src.find("def list_active_subscriptions()")
    list_block = src[list_fn_start:list_fn_start + 4000]
    assert "quote_service_anchor_condition_sql" in list_block and "service_days" in list_block, \
        "v1.7 整体破:list_active 未复用服务锚/累计达标条件"

    # 4 处 compliance 读点 effective_end 出现次数
    daily_start = src.find("def run_daily_compliance_check(")
    daily_end = src.find("\ndef ", daily_start + 1)
    daily_block = src[daily_start:daily_end if daily_end > 0 else daily_start + 12000]
    assert daily_block.count("effective_end") >= 4, \
        f"v1.7 整体破:run_daily 内 effective_end 引用 < 4(实际 {daily_block.count('effective_end')})"

    v3_start = src.find("def _compute_effective_rate_v3(")
    v3_end = src.find("\ndef ", v3_start + 1)
    v3_block = src[v3_start:v3_end if v3_end > 0 else v3_start + 4000]
    assert v3_block.count("effective_end") >= 2, \
        f"v1.7 整体破:_compute_effective_rate_v3 内 effective_end 引用 < 2(实际 {v3_block.count('effective_end')})"

    summary_start = src.find("def get_keyword_compliance_summary(")
    summary_end = src.find("\ndef ", summary_start + 1)
    summary_block = src[summary_start:summary_end if summary_end > 0 else summary_start + 6000]
    assert "scoped_quotes" in summary_block and "JOIN scoped_quotes" in summary_block, \
        "续费整体破:get_keyword_compliance_summary 未按品牌服务锚聚合"
