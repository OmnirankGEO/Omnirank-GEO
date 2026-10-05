"""
CTO-15.23 2026-05-09 · 关键词监测订阅测试(单分支 fix/monitor-rule-llm-cost-2026-05-09)

老板拍板设计:
- enable 创建订阅 + 设 is_monitored=TRUE · 不扣分
- disable 取消订阅 + 设 is_monitored=FALSE · 不退分
- daily 03:00 跑完每个 keyword 即扣 130(charge_on_success A 类完成才扣)
- update_quote_status 联动:paid 不动 · 其他状态全 cancel

不依赖 DB · 静态 grep + 纯逻辑 mock 验证(失忆友好 · 任何接班 AI 跑得通)
"""
import ast
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_sync_hook():
    """Load only the pure status hook without importing diagnosis_db/init_db."""
    source = (ROOT / "db/diagnosis_db.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_sync_keyword_monitor_state_on_status_change"
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "diagnosis_db.py", "exec"), namespace)
    return namespace["_sync_keyword_monitor_state_on_status_change"]


def _run_sync_hook(status: str):
    calls = []
    fake_monitoring_db = ModuleType("db.monitoring_db")
    fake_monitoring_db.cancel_subscriptions_by_quote = (
        lambda quote_id, reason: calls.append((quote_id, reason))
    )
    previous = sys.modules.get("db.monitoring_db")
    sys.modules["db.monitoring_db"] = fake_monitoring_db
    try:
        _load_sync_hook()(quote_id=999, status=status)
    finally:
        if previous is None:
            sys.modules.pop("db.monitoring_db", None)
        else:
            sys.modules["db.monitoring_db"] = previous
    return calls


# ====================================================================
# Test 1: schema 落地 · keyword_monitor_subscriptions 表在 init 段
# ====================================================================

def test_keyword_monitor_subscriptions_schema_in_init():
    """schema 必须在 init_monitoring_tables 内 · 否则部署时表不会自动建"""
    text = (ROOT / "db/monitoring_db.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS keyword_monitor_subscriptions" in text
    # 确保关键字段都有
    assert "user_id INTEGER NOT NULL" in text
    assert "keyword_id INTEGER NOT NULL" in text
    assert "daily_points INTEGER DEFAULT 130" in text
    assert "feature_code TEXT DEFAULT 'monitoring_keyword_daily'" in text
    # confirmed_keywords 加 monitoring_subscription_id 列
    assert '("confirmed_keywords", "monitoring_subscription_id"' in text


# ====================================================================
# Test 2: feature_pricing seed 有 monitoring_keyword_daily=130
# ====================================================================

def test_feature_pricing_seed_has_monitoring_keyword_daily():
    """seed_feature_pricing PRICING_DATA 必须含 monitoring_keyword_daily=130"""
    text = (ROOT / "db/wallet_db.py").read_text(encoding="utf-8")
    assert "'monitoring_keyword_daily'" in text
    assert "'关键词每日监测'" in text
    # 同时检查 upsert(防 prod 老库 ON CONFLICT DO NOTHING 跳过)
    assert "INSERT INTO feature_pricing" in text
    upsert_section = text.split("'monitoring_keyword_daily'")[-1]
    assert "ON CONFLICT (feature_code) DO UPDATE" in upsert_section[:500]


# ====================================================================
# Test 3: 6 个 db helper 函数都存在
# ====================================================================

def test_db_helpers_exist():
    """6 个 helper 函数必须在 monitoring_db.py 公开"""
    text = (ROOT / "db/monitoring_db.py").read_text(encoding="utf-8")
    helpers = [
        "def create_keyword_monitor_subscription(",
        "def cancel_keyword_monitor_subscription(",
        "def pause_keyword_monitor_subscription(",
        "def get_subscription_by_keyword(",
        "def list_active_subscriptions(",
        "def update_keyword_monitor_state(",
        "def record_subscription_charge(",
        "def cancel_subscriptions_by_quote(",
        "def cancel_subscriptions_by_brand(",
        "def get_confirmed_keyword_with_brand(",
    ]
    missing = [h for h in helpers if h not in text]
    assert not missing, f"缺少 helper: {missing}"


# ====================================================================
# Test 4: enable/disable endpoint 落地
# ====================================================================

def test_enable_disable_endpoints_exist():
    """enable/disable/list 3 个 endpoint 必须在 server.py"""
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '@app.post("/api/monitoring/keyword/{keyword_id}/enable")' in text
    assert '@app.post("/api/monitoring/keyword/{keyword_id}/disable")' in text
    assert '@app.get("/api/monitoring/subscriptions")' in text
    # enable 必须用 charge_on_success / feature_code
    assert "monitoring_keyword_daily" in text


# ====================================================================
# Test 5: update_quote_status 联动 hook
# ====================================================================

def test_update_quote_status_has_sync_hook():
    """update_quote_status 必须调 _sync_keyword_monitor_state_on_status_change"""
    text = (ROOT / "db/diagnosis_db.py").read_text(encoding="utf-8")
    assert "def _sync_keyword_monitor_state_on_status_change(" in text
    assert "_sync_keyword_monitor_state_on_status_change(quote_id, status)" in text
    assert _run_sync_hook("paid") == []
    assert _run_sync_hook("confirmed") == []


# ====================================================================
# Test 6: brand 软删时 cancel 监测订阅
# ====================================================================

def test_brand_soft_delete_cancels_monitor_subs():
    """api/brand_api.py 软删客户必须调 cancel_subscriptions_by_brand"""
    text = (ROOT / "api/brand_api.py").read_text(encoding="utf-8")
    assert "cancel_subscriptions_by_brand" in text
    assert "from db.monitoring_db import cancel_subscriptions_by_brand" in text


# ====================================================================
# Test 7-8: _sync hook 纯逻辑(mock cancel_subscriptions_by_quote)
# ====================================================================

def test_sync_hook_paid_skips_cancel():
    """paid/confirmed 都是有效服务锚，不取消代理已开的订阅。"""
    assert _run_sync_hook("paid") == []
    assert _run_sync_hook("confirmed") == []


def test_sync_hook_cancels_for_non_paid_statuses():
    """无有效服务锚的状态必须取消，且 reason 精确记录来源状态。"""
    for status in ("archived", "cancelled", "refunded", "draft", "pending_payment"):
        assert _run_sync_hook(status) == [(999, f"quote_status_{status}")]


# ====================================================================
# Test 9: get_client_keywords 返回 is_monitored 字段
# ====================================================================

def test_get_client_keywords_returns_is_monitored():
    """get_client_keywords SQL 必须 SELECT is_monitored 字段(前端 toggle 用)
    且不再按 is_monitored=TRUE 过滤(否则代理 disable 后该词消失就没法再 enable)
    """
    text = (ROOT / "db/monitoring_db.py").read_text(encoding="utf-8")
    # 1. SELECT 段必须包含 is_monitored
    assert "kb.is_monitored" in text
    assert "kb.monitoring_subscription_id" in text
    # 2. 不再按 is_monitored 过滤
    assert "AND COALESCE(is_monitored, TRUE) = TRUE" not in text, (
        "不应再过滤 is_monitored=TRUE · 代理 disable 后该词必须仍可见才能 re-enable"
    )
    # 3. [WO_ORPHAN_MONITOR_FIX 2026-08-10 ②] 口径改了,这条断言跟着改 —— 不是绕过。
    #    旧:`COALESCE(is_monitored, FALSE) as is_monitored`(直读 confirmed_keywords 那一列)。
    #    问题:那一列可以在**没有任何订阅**的情况下为 TRUE(C 端确认报价的历史路径就是这么
    #    产的,存量 147 条),而每日 cron 只认 keyword_monitor_subscriptions ——
    #    于是"界面开着、永远不跑"。现在改成派生自订阅真值。
    #    行为锁在 tests/orphanmon_2026_08_10/(真库真跑 get_client_keywords,正反两态给不同值)。
    assert "COALESCE(is_monitored, FALSE) as is_monitored" not in text, (
        "又退回直读 is_monitored 列了 · 那会让孤儿词重新显示成'开'"
    )
    assert "FROM keyword_monitor_subscriptions kms" in text, (
        "is_monitored 必须派生自订阅真值(EXISTS active/paused_low_balance)"
    )
