"""
库存对账等式返修 · 行为级判别锁(2026-07-29)
====================================================================
合并两件事故:
  (A) `allocated_out` 未定义 → run_audit 每次 NameError → cron 的
      `except Exception` 只 log,连续多天 inventory_audit_runs 一行都不落
      —— 对账程序自己死了,而财务页看到的还是"最近一次 07-28"的旧行。
  (B) 等式是**枚举式**(purchased + admin_adjust − allocated_out,只统计
      ('purchase_prepay','purchase_auto') 等已知 type),`manufacturer_origin_in`
      的 1.3 亿天生落在等式外 → diff 恒为 129,999,870、天天假红。

返修后的口径(审核已定,不再自行推导):
    diff = SUM(wallets.paid + bonus + frozen) − SUM(agent_inventory_transactions.points)
生产实测 2026-07-29:两边均 129,949,312 · diff = 0。

本文件的锁分两层:
  * 静态层(无 PG 也跑):AST 扫 run_audit 里有没有"用了但没绑定"的名字
    —— 就是 (A) 那个 NameError 的通用形态。老的 stage3 静态锁断言
    `"allocated_out" in eq` 反而把这个 bug 认证成了"合规",所以这层必须换成
    "名字必须绑定"而不是"名字必须叫什么"。
  * 行为层(真连 PG · 真跑 run_audit):判别锁 1-5 + 变异 ①②③。

真跑说明:tests/conftest.py 已强制把 DATABASE_URL 切到 TEST_DATABASE_URL,
本文件建的是**真表**(非 TEMP)—— 因为 record_audit_failure 必须自开连接
(异常后原事务已 aborted),TEMP 表在另一条连接里看不见。
本地实证:pgvector/pgvector:pg16 一次性容器,8 用例全过。
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AUDIT_SRC_PATH = ROOT / "services" / "inventory_audit.py"
AUDIT_SRC = AUDIT_SRC_PATH.read_text(encoding="utf-8")


# ============================================================
# 静态层 · 通用形态锁(不依赖 PG)
# ============================================================

def _func_node(src: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _unbound_names(src: str, func: str) -> set:
    """返回函数体内『被读取但从未绑定』的名字(排除模块级/内建/参数)。"""
    tree = ast.parse(src)
    module_level = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    module_level |= {
        n.name.split(".")[0] if n.asname is None else n.asname
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for n in node.names
    }
    module_level |= {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    fn = _func_node(src, func)
    bound = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    if fn.args.vararg:
        bound.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        bound.add(fn.args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for n in node.names:
                bound.add(n.asname or n.name.split(".")[0])
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    import builtins
    builtin_names = set(dir(builtins))
    used = {
        n.id for n in ast.walk(fn)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return used - bound - module_level - builtin_names


@pytest.mark.parametrize("func", ["run_audit", "record_audit_failure", "list_audit_runs"])
def test_static_no_unbound_name_in_audit_module(func):
    """🔴 (A) 的通用形态:函数里不得出现『用了但从没绑定』的名字。

    `allocated_out` 就是这样溜进生产的 —— 老静态锁只查"字符串在不在",
    在不在跟能不能跑是两回事。这条锁与名字无关,换个变量名照样红。
    """
    missing = _unbound_names(AUDIT_SRC, func)
    assert not missing, f"{func} 使用了未绑定的名字(必然 NameError): {sorted(missing)}"


def test_static_equation_is_wallet_minus_ledger_only():
    """🔴 (B):diff 只允许由 wallet_total / ledger_total 两个量构成。

    任何 historical_purchased / admin_adjust / allocated_out /
    platform_consumed / refunded_or_revoked 回到这一行,都是把枚举式请回来。
    """
    m = re.search(r"^\s*diff_total\s*=.*$", AUDIT_SRC, re.MULTILINE)
    assert m, "找不到 diff_total 赋值"
    eq = m.group(0)
    for banned in ("historical_purchased", "admin_adjust", "allocated_out",
                   "platform_consumed", "refunded_or_revoked"):
        assert banned not in eq, f"守恒式又枚举了 {banned} —— 枚举式就是 1.3 亿漏算的根因"
    assert "wallet_total" in eq and "ledger_total" in eq, "守恒式不是账实相符式"


def test_static_ledger_sum_has_no_type_filter():
    """流水侧必须全量 SUM,不得带 `WHERE type IN (...)` —— 否则新 type 又漏。"""
    m = re.search(
        r"SELECT\s+COALESCE\(SUM\(points\),\s*0\)\s+AS\s+s\s+FROM\s+agent_inventory_transactions\s*",
        AUDIT_SRC,
    )
    assert m, "找不到全量流水 SUM(points)"
    tail = AUDIT_SRC[m.end(): m.end() + 60]
    assert "WHERE" not in tail.upper(), "全量流水 SUM 被加了 WHERE 过滤 —— 新 type 会再次漏算"


def test_static_frozen_in_wallet_total():
    """三池必须含 frozen(冻结是池内搬运 · 不写流水 · 漏了立刻漂移)。"""
    assert "frozen_inventory_points" in AUDIT_SRC, "钱包侧没读 frozen_inventory_points"
    m = re.search(r"^\s*wallet_total\s*=.*$", AUDIT_SRC, re.MULTILINE)
    assert m and "agent_frozen" in m.group(0), "wallet_total 没把 frozen 计入"


def test_static_admin_summary_uses_same_ssot_equation():
    """第二份枚举式也必须收敛:/admin/inventory-audit/summary 与 run_audit 同口径。

    同一个 /admin/inventory-audit 页面上方显示本接口的即时差额、下方显示 run_audit
    的历史差额。两份等式不一致就会一红一绿自相矛盾 —— 而它们本来就是同一份漏算。
    """
    src = (ROOT / "api" / "admin_factory_api.py").read_text(encoding="utf-8")
    fn = _func_node(src, "admin_inventory_audit")
    body = ast.get_source_segment(src, fn) or ""
    # 剥掉 docstring —— 里面写着等式说明,会把"字符串在不在"的匹配带偏(老锁就是这么假绿的)
    doc = ast.get_docstring(fn, clean=False)
    if doc:
        body = body.replace(doc, "")
    m = re.search(r"^\s*diff\s*=.*$", body, re.MULTILINE)
    assert m, "找不到 summary 的 diff 赋值"
    eq = m.group(0)
    assert "wallet_total" in eq and "ledger_total" in eq, "summary 仍是枚举式"
    for banned in ("historical_purchased", "admin_adjust", "platform_consumed", "customer_total"):
        assert banned not in eq, f"summary 的 diff 又枚举了 {banned}"
    assert "frozen_inventory_points" in body, "summary 没把冻结池计入账面结存"


def test_static_cron_except_records_failure():
    """变异③的静态面:cron 的 except 分支必须调 record_audit_failure。"""
    sched = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")
    fn = _func_node(sched, "_v35_inventory_audit_daily")
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers, "cron 没有 except 分支?"
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for h in handlers for n in ast.walk(h) if isinstance(n, ast.Call)
    }
    assert "record_audit_failure" in called, \
        "cron 的 except 仍然只 log —— 对账死了没人知道(静默死亡 8 天的原样复发)"


# ============================================================
# 行为层 · 真连 PG
# ============================================================

_W4_SQL = (ROOT / "scripts" / "migration_v35_w4_2026_05_26.sql").read_text(encoding="utf-8")
_EQ_SQL = (ROOT / "scripts" / "migration_inventory_audit_equation_2026_07_29.sql").read_text(encoding="utf-8")

# 只被 SUM/聚合读的上游表 · 本地建最小真表(列名/类型对齐生产实测 schema)
_UPSTREAM_DDL = """
DROP TABLE IF EXISTS inventory_audit_diffs CASCADE;
DROP TABLE IF EXISTS inventory_audit_runs CASCADE;
DROP TABLE IF EXISTS agent_inventory_wallets CASCADE;
DROP TABLE IF EXISTS agent_inventory_transactions CASCADE;
DROP TABLE IF EXISTS customer_agent_credit_wallets CASCADE;
DROP TABLE IF EXISTS customer_credit_transactions CASCADE;
DROP TABLE IF EXISTS ai_ops_alerts CASCADE;
DROP TABLE IF EXISTS ai_ops_tasks CASCADE;

CREATE TABLE agent_inventory_wallets (
    agent_user_id integer PRIMARY KEY,
    paid_inventory_points bigint NOT NULL DEFAULT 0,
    bonus_inventory_points bigint NOT NULL DEFAULT 0,
    frozen_inventory_points bigint NOT NULL DEFAULT 0
);
CREATE TABLE agent_inventory_transactions (
    id bigserial PRIMARY KEY,
    agent_user_id integer NOT NULL,
    type text NOT NULL,
    pool text NOT NULL,
    points bigint NOT NULL,
    related_customer_user_id integer,
    related_order_id text
);
CREATE TABLE customer_agent_credit_wallets (
    customer_user_id integer, agent_user_id integer,
    tool_credit_points bigint DEFAULT 0,
    publish_credit_points bigint DEFAULT 0,
    bonus_credit_points bigint DEFAULT 0
);
CREATE TABLE customer_credit_transactions (
    customer_user_id integer, agent_user_id integer, type text, pool text,
    points bigint, related_order_id text
);
CREATE TABLE ai_ops_tasks (id serial PRIMARY KEY);
"""

# W4 migration 尾部的 4 表自验 DO 块依赖另两张表 —— 本文件只关心对账两表,
# 故切出 inventory_audit_runs / inventory_audit_diffs 两段 CREATE 原文(不手抄 · 防漂移)。
def _slice_create(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE IF NOT EXISTS {table}")
    end = sql.index(");", start) + 2
    return sql[start:end]


_BASE_AUDIT_DDL = (
    _slice_create(_W4_SQL, "inventory_audit_runs") + "\n"
    + _slice_create(_W4_SQL, "inventory_audit_diffs") + "\n"
)

# ai_ops_alerts 的真 DDL 同样从 AI Ops migration 里原样切
_AI_OPS_SQL = (ROOT / "scripts" / "migration_ai_ops_center_2026_07_01.sql").read_text(encoding="utf-8")
_ALERTS_DDL = (
    _slice_create(_AI_OPS_SQL, "ai_ops_alerts") + "\n"
    + "CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_ops_alerts_firing "
      "ON ai_ops_alerts (rule_key, fingerprint) WHERE status = 'firing';\n"
)


def _dsn() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("无 TEST_DATABASE_URL / DATABASE_URL")
    dbname = url.rsplit("/", 1)[-1].split("?")[0].lower()
    # 🔴 本文件 DROP/CREATE 真表 —— 库名不含 test 一律拒跑
    assert "test" in dbname and "prod" not in dbname, f"拒绝在非测试库上建表: {dbname}"
    return url


@pytest.fixture()
def db():
    conn = psycopg2.connect(_dsn())
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_UPSTREAM_DDL)
    cur.execute(_BASE_AUDIT_DDL)
    cur.execute(_ALERTS_DDL)
    cur.execute(_EQ_SQL)          # ← 返修 migration 本身也在这里被真跑一遍
    conn.autocommit = False
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def _seed_balanced(cur, *, frozen: int = 30):
    """钱包 paid100 + bonus20 + frozen{frozen} · 流水净额相等 → diff = 0"""
    cur.execute("INSERT INTO agent_inventory_wallets VALUES (1, 100, 20, %s)", (frozen,))
    total = 120 + frozen
    cur.execute(
        "INSERT INTO agent_inventory_transactions (agent_user_id,type,pool,points) "
        "VALUES (1,'purchase_prepay','paid',%s)", (total,))


def _run(cur, **kw):
    from services.inventory_audit import run_audit
    return run_audit(cur, **kw)


def test_lock1_run_audit_no_nameerror_diff_zero_and_row_written(db):
    """判别锁 1:真跑 run_audit → 无 NameError · diff=0 · 落 inventory_audit_runs 一行。"""
    cur = db.cursor()
    _seed_balanced(cur)
    result = _run(cur, triggered_by="cron")
    db.commit()

    assert result["diff_total"] == 0, f"应对平,实际 {result['diff_total']}"
    assert result["has_drift"] is False
    assert result["status"] == "ok"
    assert result["wallet_total"] == result["ledger_total"] == 150

    cur.execute("SELECT COUNT(*) AS c FROM inventory_audit_runs WHERE status='ok'")
    assert cur.fetchone()["c"] == 1, "对账必须落一行"
    cur.execute("SELECT agent_total_frozen, wallet_total, ledger_total FROM inventory_audit_runs")
    row = cur.fetchone()
    assert (row["agent_total_frozen"], row["wallet_total"], row["ledger_total"]) == (30, 150, 150)


def test_lock2_new_ledger_type_is_never_missed(db):
    """判别锁 2:新 type 追加 +100 且不动钱包 → diff 必须 = -100(枚举式会漏成 0)。"""
    cur = db.cursor()
    _seed_balanced(cur)
    cur.execute(
        "INSERT INTO agent_inventory_transactions (agent_user_id,type,pool,points) "
        "VALUES (1,'manufacturer_origin_in','paid',100)")
    result = _run(cur)
    db.commit()
    assert result["diff_total"] == -100, \
        f"新 type 必须自动进等式,实际 diff={result['diff_total']}"
    assert result["has_drift"] is True


def test_lock3_wallet_only_change_turns_red(db):
    """判别锁 3:只改钱包不写流水 → diff 必须非 0。"""
    cur = db.cursor()
    _seed_balanced(cur)
    cur.execute("UPDATE agent_inventory_wallets SET paid_inventory_points = paid_inventory_points + 77")
    result = _run(cur)
    db.commit()
    assert result["diff_total"] == 77
    assert result["has_drift"] is True


def test_lock4_frozen_counted_and_dropping_it_turns_red(db):
    """判别锁 4 + 变异②:有 frozen 的场景现版 diff=0;把 frozen 从三池去掉必须转红。"""
    cur = db.cursor()
    _seed_balanced(cur, frozen=30)
    assert _run(cur)["diff_total"] == 0
    db.rollback()

    # —— 变异②:去掉 frozen —— 同一份数据必须变红
    mutant = _mutate(
        (r"COALESCE\(SUM\(frozen_inventory_points\), 0\) AS frozen", "0 AS frozen"),
    )
    cur = db.cursor()
    _seed_balanced(cur, frozen=30)
    mres = mutant(cur)
    db.commit()
    assert mres["diff_total"] == -30, \
        f"去掉 frozen 必须让 diff 转红,变异体却给出 {mres['diff_total']}"


def test_mutation1_enumerated_equation_misses_the_new_type(db):
    """变异①:还原枚举式等式 → 对锁 2 的场景必须给出错误答案(= 该锁真的在钉这件事)。"""
    mutant = _mutate(
        (r"diff_total = wallet_total - ledger_total",
         "diff_total = (agent_paid + agent_bonus) - (historical_purchased + admin_adjust)"),
    )
    cur = db.cursor()
    _seed_balanced(cur)
    cur.execute(
        "INSERT INTO agent_inventory_transactions (agent_user_id,type,pool,points) "
        "VALUES (1,'manufacturer_origin_in','paid',100)")
    mres = mutant(cur)
    db.commit()
    assert mres["diff_total"] != -100, "枚举式变异体居然也算对了 —— 判别锁 2 没有区分力"


def test_lock5_exception_writes_failed_row_and_fires_alert(db):
    """判别锁 5:run_audit 内部抛真异常 → 落 status='failed' 行 + ai_ops 告警 firing。

    用 DROP TABLE 制造**真** psycopg2 异常(不是替身抛的),顺带证明:
    异常后原事务已 aborted,失败态只能靠 record_audit_failure 自开连接才落得下去。
    """
    from services.inventory_audit import (
        record_audit_failure, ALERT_RULE_KEY, ALERT_FP_FAILURE,
    )
    cur = db.cursor()
    _seed_balanced(cur)
    db.commit()
    cur.execute("DROP TABLE agent_inventory_wallets")
    db.commit()

    with pytest.raises(Exception) as ei:
        _run(cur, triggered_by="cron")
    db.rollback()

    run_id = record_audit_failure(ei.value, triggered_by="cron")
    assert run_id is not None, "失败态必须落库"

    chk = psycopg2.connect(_dsn())
    chk.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        c = chk.cursor()
        c.execute("SELECT status, has_drift, error_message, notes FROM inventory_audit_runs "
                  "WHERE id=%s", (run_id,))
        row = c.fetchone()
        assert row["status"] == "failed"
        # 只认 has_drift 的老消费方(finance_api /reconciliation)也必须变红
        assert row["has_drift"] is True, "失败行 has_drift 必须为真,否则财务页仍显示绿"
        assert row["error_message"], "失败原因必须落库"
        assert row["notes"].startswith("AUDIT_FAILED")

        c.execute("SELECT severity, status FROM ai_ops_alerts "
                  "WHERE rule_key=%s AND fingerprint=%s", (ALERT_RULE_KEY, ALERT_FP_FAILURE))
        alert = c.fetchone()
        assert alert is not None, "对账失败必须触发告警"
        assert alert["status"] == "firing" and alert["severity"] == "critical"
    finally:
        chk.close()


def test_mutation3_without_record_audit_failure_nothing_lands(db, monkeypatch):
    """变异③:异常时只 log 不落库 → 一行都不会有(证明锁 5 不是靠别的路径蒙对的)。"""
    import services.inventory_audit as mod
    monkeypatch.setattr(mod, "record_audit_failure", lambda *a, **k: None)
    cur = db.cursor()
    _seed_balanced(cur)
    db.commit()
    cur.execute("DROP TABLE agent_inventory_transactions")
    db.commit()
    with pytest.raises(Exception) as ei:
        _run(cur)
    db.rollback()
    mod.record_audit_failure(ei.value, triggered_by="cron")  # 已被替换成 no-op

    chk = psycopg2.connect(_dsn())
    chk.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        c = chk.cursor()
        c.execute("SELECT COUNT(*) AS n FROM inventory_audit_runs WHERE status='failed'")
        assert c.fetchone()["n"] == 0, "没有 record_audit_failure 就一行都不该有 —— 锁 5 有区分力"
    finally:
        chk.close()


def test_successful_run_resolves_failure_alert(db):
    """失败告警要能被"下一次跑成功"收掉,否则告警永远挂着 = 又变噪音。"""
    from services.inventory_audit import (
        record_audit_failure, ALERT_RULE_KEY, ALERT_FP_FAILURE,
    )
    record_audit_failure(RuntimeError("boom"), triggered_by="cron")
    cur = db.cursor()
    _seed_balanced(cur)
    _run(cur)
    db.commit()

    chk = psycopg2.connect(_dsn())
    chk.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        c = chk.cursor()
        c.execute("SELECT status FROM ai_ops_alerts WHERE rule_key=%s AND fingerprint=%s "
                  "ORDER BY id DESC LIMIT 1", (ALERT_RULE_KEY, ALERT_FP_FAILURE))
        assert c.fetchone()["status"] == "resolved"
    finally:
        chk.close()


# ============================================================
# 变异体加载器
# ============================================================

def _mutate(*replacements):
    """把 services/inventory_audit.py 源码做替换后编译成独立模块,返回其 run_audit。

    真编译真跑 —— 不是"假装改了一下"。任何一处替换没命中都直接失败,
    防止变异体其实等于原版、变异测试假绿。
    """
    src = AUDIT_SRC
    for pattern, repl in replacements:
        new, n = re.subn(pattern, repl, src, count=1)
        assert n == 1, f"变异未命中: {pattern}"
        src = new
    ns: dict = {"__name__": "inventory_audit_mutant", "__file__": str(AUDIT_SRC_PATH)}
    exec(compile(src, "<mutant:inventory_audit>", "exec"), ns)  # noqa: S102
    return ns["run_audit"]
