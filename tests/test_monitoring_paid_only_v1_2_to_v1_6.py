"""monitoring P0 hotfix v1.2-1.6 · scheduler / portal / compliance / 自然日 / 健康检查 + backfill

老板 6 大任务清单 #2 #3 #5 #6 收口测试

覆盖:
- v1.2 list_active_subscriptions JOIN quotes status='paid'(scheduler 不跑 confirmed 未付)
- v1.3 get_client_keywords brand_quotes status='paid' 收紧(confirmed 未付不展示客户)
- v1.4 compliance 服务窗口 effective_start = MAX(paid_at, kms.enabled_at)
       _get_effective_start_date helper + 4 处读点过滤
- v1.5 portal 自然日剩余 service_remaining_days_natural + contract_start/end_date
- v1.6 scripts/kms_health_check.py 4 类异常 + scripts/kms_backfill_2026_05_29.py paid-only 守护
"""
from __future__ import annotations
import os
import re

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _read(rel: str) -> str:
    with open(os.path.join(_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# v1.2 · list_active_subscriptions JOIN quotes status='paid'
# ============================================================

def _extract_sql_block(fn_block: str) -> str:
    """从 def 函数体中抽取 cur.execute(\"\"\"...\"\"\") 内的 SQL 主体(忽略 docstring 注释)"""
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


def test_v12_list_active_subscriptions_inner_join_quotes():
    """v1.2 P0 · LEFT JOIN quotes → JOIN quotes(INNER · 防 quote_id NULL 脏数据进 scheduler)
    [audit 红线批 2026-06-10] 提取窗 3000→6000(SSOT 口径注释加长推后 SQL)· INNER JOIN 守护不变"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def list_active_subscriptions()")
    assert fn_start > 0
    _next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:_next_def if _next_def > 0 else fn_start + 6000]
    # 只查真 SQL 段(cur.execute 之后)· docstring 历史说明文字里也含 "LEFT JOIN quotes" 字样会误报
    _sql_seg = fn_block[fn_block.find("cur.execute"):]

    # 🔴 [WO_MANUAL_KEYWORD_PARITY 2026-08-16 K3] 这条锁**按臂**判,不再对整段一刀切。
    #   原判据「整段不许出现 LEFT JOIN quotes」保护的是**合同臂**:
    #   quote_id NULL 的脏数据不能喂给 scheduler。那条保护在下面一字不减地保留。
    #   但 parity 之后同一个函数多了**手动臂**:Owner K3 定死手动词
    #   「不挂报价单生命周期」—— 它本来就可以没有 quote,只能 LEFT JOIN。
    #   对手动臂沿用旧判据 = 用一条为合同词立的规矩去否决 Owner 对手动词的裁决。
    #   ⇒ 合同臂:必须 INNER JOIN、不许 LEFT JOIN(原样)
    #     手动臂:允许 LEFT JOIN,但必须自带**等价强度**的兜底 ——
    #             brand_name 不许为空(空 brand_name 正是 2026-05-26
    #             「自动监测 detected=0/176」那个 P0 的真因)。
    _arms = _sql_seg.split("UNION ALL")
    assert len(_arms) == 2, f"期望 UNION ALL 两臂(合同/手动),实测 {len(_arms)} 段"
    _contract_arm, _extra_arm = _arms

    assert "JOIN quotes q ON q.id = s.quote_id" in _contract_arm, \
        "v1.2 P0 破:合同臂未 INNER JOIN quotes"
    assert "LEFT JOIN quotes" not in _contract_arm, \
        "v1.2 P0 破:合同臂仍 LEFT JOIN(quote_id NULL 脏数据会进 scheduler)"

    assert "LEFT JOIN quotes" in _extra_arm, \
        "手动臂改成 INNER JOIN 会把没挂报价单的手动词整批漏掉(违 K3)"
    assert "brand_name" in _extra_arm and "<> ''" in _extra_arm, \
        "手动臂 LEFT JOIN 之后没有 brand_name 非空兜底 —— target_brand='' 会让 LLM 检测恒 False"


def test_v12_list_active_subscriptions_filters_status_paid():
    """[audit 红线批 2026-06-10 口径升级·老板已批] v1.2 paid-only → 服务锚 SSOT 双臂:
    paid(服务期有效) OR confirmed+COALESCE(service_start_date,paid_at)非空(玩法B真客户)。
    旧 paid-only 把 confirmed 真客户订阅滤成僵尸(active 但永不跑·prod quote 282/287/109 冻在 5/28)。
    守护点变为:① paid 臂仍在 ② confirmed 必须带服务锚(纯 confirmed 未付草稿仍不跑·v1.2 初衷保住)"""
    src = _read("db/monitoring_db.py")
    helper_start = src.find("def quote_service_anchor_condition_sql(")
    helper_block = src[helper_start:helper_start + 2600]
    assert "{a}.status = 'paid'" in helper_block, \
        "服务锚 SSOT 破:paid 臂丢失"
    assert "{a}.status = 'confirmed'" in helper_block and "COALESCE({a}.service_start_date, {a}.paid_at)" in helper_block, \
        "服务锚 SSOT 破:confirmed 玩法B臂丢失(订阅会重回僵尸态)"
    assert "<> 'expired'" in helper_block and "quote_unfulfilled_compliance_condition_sql" in helper_block, \
        "续费履约口径破:expired 未完成 quote 未被服务锚放行"

    fn_start = src.find("def list_active_subscriptions()")
    fn_block = src[fn_start:fn_start + 6000]
    assert 'quote_service_anchor_condition_sql("q")' in fn_block, \
        "服务锚 SSOT 破:list_active_subscriptions 未复用统一服务锚"


# ============================================================
# v1.3 · get_client_keywords brand_quotes 收紧 status='paid'
# ============================================================

def test_v13_get_client_keywords_brand_quotes_paid_only():
    """v1.3 P0 · brand_quotes CTE WHERE 必须 status = 'paid'(单值 · 不是 IN)"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def get_client_keywords(")
    assert fn_start > 0
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 8000]

    # 抽 SQL 主体(忽略 SQL 内 -- 行注释)
    sql_start = fn_block.find('cursor.execute(f"""')
    assert sql_start > 0, "找不到 SQL 段"
    sql_end = fn_block.find('"""', sql_start + 20)
    sql_block = fn_block[sql_start:sql_end]

    # 剥离 -- 行注释(SQL 内可能保留历史注释提到旧写法)
    sql_no_comments = "\n".join(
        line for line in sql_block.splitlines()
        if not line.strip().startswith("--")
    )

    # [2026-05-29 D1/D2 锚翻转] paid 可见 + confirmed 凭【服务锚】可见
    # (service_start_date 优先 · paid_at 兜底 · confirmed=服务激活;v1.3 严格 paid-only 已被 v17.1 + D1 取代)
    assert 'quote_service_anchor_condition_sql("q")' in sql_no_comments, \
        "破:brand_quotes 未复用服务锚 SSOT"
    # 不能再无条件 IN ('paid', 'confirmed')(confirmed 必须带服务锚)
    assert "status IN ('paid', 'confirmed')" not in sql_no_comments, \
        "破:真 SQL 仍无条件 IN ('paid', 'confirmed') · confirmed 未带锚仍展示"


# ============================================================
# v1.4 · compliance 服务窗口 effective_start
# ============================================================

def test_v14_effective_start_helper_exists():
    """v1.4 P0 · _get_effective_start_date helper 必须存在"""
    src = _read("db/monitoring_db.py")
    assert "def _get_effective_start_date(" in src, \
        "v1.4 P0 破:_get_effective_start_date helper 未定义"


def test_v14_effective_start_helper_returns_max_paid_kms():
    """v1.4 P0 · helper(v1.4 _get_effective_start_date / v1.7 _get_effective_window)语义守护

    v1.7 后:_get_effective_start_date 是 _get_effective_window 的 backward-compat alias
    核心约束(paid_at / enabled_at / status='paid' / fail-closed)迁移到 _get_effective_window
    """
    src = _read("db/monitoring_db.py")
    # 优先看 v1.7 的 window helper(单源真理)
    fn_start = src.find("def _get_effective_window(")
    if fn_start < 0:
        fn_start = src.find("def _get_effective_start_date(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 3500]

    assert "paid_at" in fn_block, "v1.4 破:helper 未取 quote.paid_at"
    assert "enabled_at" in fn_block, "v1.4 破:helper 未取 kms.enabled_at"
    # 必须 paid-only(SELECT WHERE status='paid')
    assert "q.status = 'paid'" in fn_block, \
        "v1.4 破:helper 未过滤 quote.status='paid'"
    # fail-closed(v1.7 后是 (None, None) tuple · v1.4 是 None)
    assert "return None" in fn_block or "(None, None)" in fn_block, \
        "v1.4 破:helper 未 fail-closed(paid_at NULL 仍返起算点)"


def test_v14_run_daily_compliance_check_uses_service_anchor():
    """v1.4/v2 · run_daily_compliance_check 必须复用服务锚,不能回退到无条件 status IN"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 10000]

    # 旧版 IN ('confirmed','paid') 必须删
    assert "status IN ('confirmed', 'paid')" not in fn_block, \
        "v1.4 破:run_daily_compliance_check 仍 IN ('confirmed','paid')(confirmed 未付仍跑 compliance)"
    assert 'quote_service_anchor_condition_sql("quotes")' in fn_block, \
        "续费口径破:run_daily_compliance_check 未复用服务锚 SSOT"


def test_v14_run_daily_compliance_check_skips_no_effective_start():
    """v1.4 P0 · effective_start None 时必须 skip + 计数(fail-closed)
    v1.7 后:_get_effective_start_date → _get_effective_window(任一调用都接受)
    """
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 12000]

    assert "skipped_no_effective_start" in fn_block, \
        "v1.4 破:run_daily_compliance_check 未维护 skipped_no_effective_start 计数"
    assert (
        "_get_effective_start_date(" in fn_block
        or "_get_effective_window(" in fn_block
    ), "v1.4 破:run_daily_compliance_check 未调 effective helper"
    # 必须 continue(skip)
    assert "continue" in fn_block, \
        "v1.4 破:effective_start None 时未 continue 跳过该 keyword"


def test_v14_compliance_log_queries_filter_effective_start():
    """v1.4 P0 · 至少 3 处 compliance log 查询加 effective_start 下限

    1. run_daily_compliance_check first_compliant_date 查询
    2. run_daily_compliance_check hist_avg(达标后累计) GREATEST(first_compliant_date, effective_start)
    3. run_daily_compliance_check recent 6d stable 查询
    """
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def run_daily_compliance_check(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 10000]

    # 至少 2 处 "v1.4 服务窗口下限" 注释或 effective_start binding
    eff_bindings = fn_block.count("effective_start")
    assert eff_bindings >= 4, \
        f"v1.4 破:run_daily_compliance_check 中 effective_start 引用 < 4(实际 {eff_bindings})"


def test_v14_summary_uses_effective_start_cte():
    """v1.4 P0 · get_keyword_compliance_summary 必须用 effective_start CTE 过滤 check_date
    v1.7 后 CTE 改名 kw_eff_window(含上下界)· 命名兼容老 kw_eff_start
    """
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def get_keyword_compliance_summary(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 5000]

    # v1.4 用 kw_eff_start · v1.7 改名 kw_eff_window(含 end)· 任一通过
    assert "kw_eff_start" in fn_block or "kw_eff_window" in fn_block, \
        "v1.4 破:get_keyword_compliance_summary 未用 effective_window CTE"
    assert "effective_start" in fn_block, \
        "v1.4 破:get_keyword_compliance_summary 无 effective_start 字段"
    assert "GREATEST" in fn_block, \
        "v1.4 破:effective_start 计算未用 GREATEST(paid_at, kms enabled_at)"
    assert "paid_at" in fn_block, \
        "v1.4 破:effective_start 未取 quote.paid_at"
    # [audit 红线批 2026-06-10] 旧 paid-only 字面断言已被服务锚口径取代(2026-06-07 服务锚批·
    # confirmed 玩法B按 COALESCE(service_start_date, paid_at) 取窗)· 守护点改为 COALESCE 服务锚
    assert "COALESCE(q.service_start_date, q.paid_at::date)" in fn_block, \
        "服务锚口径破:get_keyword_compliance_summary 未用 COALESCE 服务锚"


def test_v14_v3_algorithm_accepts_effective_start():
    """v1.4 P0 · _compute_effective_rate_v3 必须接 effective_start 参数(避免双查 helper)"""
    src = _read("db/monitoring_db.py")
    fn_start = src.find("def _compute_effective_rate_v3(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 4000]

    assert "effective_start" in fn_block, \
        "v1.4 破:_compute_effective_rate_v3 签名缺 effective_start"
    # 查询必须用 effective_start 作下限
    pat = re.compile(r"check_date\s*>=\s*%s", re.IGNORECASE)
    assert pat.search(fn_block) or "IS NULL OR check_date >= " in fn_block, \
        "v1.4 破:_compute_effective_rate_v3 查询未加 effective_start 下限"


# ============================================================
# v1.5 · portal 自然日剩余 service_remaining_days_natural
# ============================================================

def test_v15_api_returns_service_remaining_days_natural():
    """v1.5 P0 · api_get_client_keywords_merged 顶层必须返 service_remaining_days_natural"""
    src = _read("api/monitoring_api.py")
    fn_start = src.find("def api_get_client_keywords_merged(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 6000]

    assert '"service_remaining_days_natural"' in fn_block, \
        "v1.5 P0 破:api_get_client_keywords_merged 顶层未返 service_remaining_days_natural"
    # 含 contract_start_date / contract_end_date(辅助调试 + 前端展示用)
    assert '"contract_start_date"' in fn_block, \
        "v1.5 P0 破:未返 contract_start_date"
    assert '"contract_end_date"' in fn_block, \
        "v1.5 P0 破:未返 contract_end_date"


def test_v15_service_remaining_days_natural_paid_at_priority():
    """v1.5 P0 · 自然日起算点优先 paid_at · 兜底 service_start_date"""
    src = _read("api/monitoring_api.py")
    fn_start = src.find("def api_get_client_keywords_merged(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 6000]

    # 必须 SELECT paid_at
    assert "paid_at" in fn_block, \
        "v1.5 P0 破:未取 quote.paid_at 作自然日起算点"
    # 必须 max(0, ...) 不返负数
    assert "max(0" in fn_block, \
        "v1.5 P0 破:service_remaining_days_natural 未 clamp 0(过期会显示负数)"


def test_v15_per_keyword_compliance_remaining_days_alias():
    """v1.5 P0 · per-keyword 加 compliance_remaining_days alias(履约维度显式命名)"""
    src = _read("api/monitoring_api.py")
    fn_start = src.find("def api_get_client_keywords_merged(")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_block = src[fn_start:next_def if next_def > 0 else fn_start + 6000]

    assert "compliance_remaining_days" in fn_block, \
        "v1.5 破:未加 compliance_remaining_days alias(remaining_days 老字段语义混淆)"


# ============================================================
# v1.6 · KMS 健康检查脚本 + backfill 脚本
# ============================================================

def test_v16_health_check_script_exists():
    """v1.6 P0 · scripts/kms_health_check.py 必须存在"""
    p = os.path.join(_ROOT, "scripts", "kms_health_check.py")
    assert os.path.exists(p), "v1.6 P0 破:scripts/kms_health_check.py 不存在"


def test_v16_health_check_4_anomalies():
    """v1.6 P0 · 健康检查 4 类异常都必须扫"""
    src = _read("scripts/kms_health_check.py")
    # 4 类异常 helper 函数
    assert "def check_dup_active(" in src, \
        "v1.6 破:缺 check_dup_active(同 keyword 多 active sub)"
    assert "def check_is_monitored_but_no_active_kms(" in src, \
        "v1.6 破:缺 check_is_monitored_but_no_active_kms(ck.is_monitored=TRUE 但 KMS NULL)"
    assert "def check_active_kms_but_quote_not_paid(" in src, \
        "v1.6 破:缺 check_active_kms_but_quote_not_paid(active KMS 但 quote 未付)"
    assert "def check_active_kms_but_ck_not_monitored(" in src, \
        "v1.6 破:缺 check_active_kms_but_ck_not_monitored(active KMS 但 ck.is_monitored=FALSE)"


def test_v16_health_check_readonly():
    """v1.6 P0 · 健康检查必须只读 · 不能含 UPDATE/DELETE/INSERT"""
    src = _read("scripts/kms_health_check.py")
    # 大写形式扫(避免误伤注释)
    for danger in ("UPDATE ", "DELETE ", "INSERT INTO", "TRUNCATE", "DROP "):
        assert danger not in src.upper(), \
            f"v1.6 P0 破:健康检查包含写操作 '{danger}'(违背只读原则)"


def test_v16_backfill_script_exists():
    """v1.6 P0 · scripts/kms_backfill_2026_05_29.py 必须存在"""
    p = os.path.join(_ROOT, "scripts", "kms_backfill_2026_05_29.py")
    assert os.path.exists(p), "v1.6 P0 破:scripts/kms_backfill_2026_05_29.py 不存在"


def test_v16_backfill_walks_endpoint_not_raw_sql():
    """v1.6 P0 · backfill 必须走 create_keyword_monitor_subscription · 不能裸 INSERT(老板铁律)"""
    src = _read("scripts/kms_backfill_2026_05_29.py")

    # 必须导入 / 调 endpoint 同路径函数
    assert "create_keyword_monitor_subscription" in src, \
        "v1.6 P0 破:backfill 未调 create_keyword_monitor_subscription(走 endpoint)"
    # 绝不裸 INSERT INTO keyword_monitor_subscriptions
    assert "INSERT INTO keyword_monitor_subscriptions" not in src.upper(), \
        "v1.6 P0 破:backfill 走 SQL 裸插(违背老板'走 endpoint 不裸插'铁律)"


def test_v16_backfill_paid_only_guard():
    """v1.6 P0 · backfill 必须 paid-only guard(未付 quote 跳过)"""
    src = _read("scripts/kms_backfill_2026_05_29.py")

    assert "_paid_only_check" in src or "paid_only_check" in src, \
        "v1.6 P0 破:backfill 未实现 paid-only 守护"
    # 必须有 dry-run 默认(防误执行)
    assert "--apply" in src, \
        "v1.6 P0 破:backfill 未提供 --apply flag(应默认 dry-run)"
    assert "dry_run" in src.lower() or "dry-run" in src.lower(), \
        "v1.6 P0 破:backfill 未实现 dry-run 模式"


# ============================================================
# 整体一致性 · enable / list / get_client / run_compliance 各守自己的服务口径
# ============================================================

def test_consistency_monitoring_subscription_service_anchor_policy():
    """整体 · enable helper / list_active / get_client 必须对齐服务锚口径"""
    src = _read("db/monitoring_db.py")

    # 1. enable helper(单点+batch enable 共享)— server.py
    server_src = _read("server.py")
    assert "_assert_keyword_quote_service_anchored_blocking" in server_src
    helper = server_src[server_src.find("def _assert_keyword_quote_service_anchored_blocking"):]
    assert "service_start_date" in helper[:3500] and "paid_at" in helper[:3500]
    assert "service_anchor_blocked" in server_src
    # 2. v1.2 list_active_subscriptions → [audit 红线批 2026-06-10] 服务锚 SSOT 双臂(注释推后扩窗 6000)
    list_fn = src[src.find("def list_active_subscriptions("):]
    assert 'quote_service_anchor_condition_sql("q")' in list_fn[:6000]
    # 3. v1.3 get_client_keywords brand_quotes
    gck_fn = src[src.find("def get_client_keywords("):]
    next_def = gck_fn.find("\ndef ", 1)
    gck_block = gck_fn[:next_def if next_def > 0 else 8000]
    # [2026-05-29 D2] 可见性已改服务锚口径(paid OR confirmed-with-COALESCE(service_start_date,paid_at))
    assert 'quote_service_anchor_condition_sql("q")' in gck_block
    # 4. v1.4 run_daily_compliance_check WHERE 复用服务锚
    cmp_fn = src[src.find("def run_daily_compliance_check("):]
    next_def = cmp_fn.find("\ndef ", 1)
    cmp_block = cmp_fn[:next_def if next_def > 0 else 10000]
    assert 'quote_service_anchor_condition_sql("quotes")' in cmp_block, \
        "v1.4 整体:run_daily_compliance_check 未复用服务锚 SSOT"
