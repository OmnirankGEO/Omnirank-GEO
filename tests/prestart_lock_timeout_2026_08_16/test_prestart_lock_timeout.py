"""[WO_PRESTART_LOCK_TIMEOUT_2026-08-16 §2] prestart 迁移期 lock_timeout 的四条成对判据。

被测行为:prestart 重放迁移时若拿不到锁,**10s 快速失败**而不是无限排队。

🔴 为什么这套判据必须"成对":
  只测「有长事务时失败」会被一个恒失败的实现骗过 —— 所以每条正向都配一条反向:
    §2-1 正向:有长事务 → 10s 内失败 + 错误信息含 blocker pid
    §2-2 反向:无长事务 → 同一 prestart 全量重放**成功**(证明不是恒失败)
    §2-3 回归:干净库 + 生产形状库各重放两遍无新错(证明两个 timeout 没改变成功路径)
    §2-4 变异:拆掉 `SET lock_timeout` → 正向那条必须从「10s 失败」变「挂起」
              (**这一条才证明是 lock_timeout 在起作用**,不是别的东西碰巧让它失败)

运行前置:需要一个本机 PG(容器即可),用 `PRESTART_LT_ADMIN_DSN` 指向它的 postgres 库。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parent.parent.parent
ADMIN_DSN = os.getenv(
    "PRESTART_LT_ADMIN_DSN",
    "postgresql://geo_admin:test@localhost:5439/postgres",
)
#: 🔴 库名必须含 "test" —— tests/conftest.py 的安全栓靠它拦住"误连真库"。
#:   绝不要用 ALLOW_NONTEST_DB=1 绕过(memory: dualab-db-name-safety-false-green)。
DB_CLEAN = "test_prestart_lt_clean"
DB_PROD = "test_prestart_lt_prod"
#: 生产实测被撞过的两张表之一(第 30 班第二次中止就是它)
BLOCK_TABLE = "geo_research_articles"
#: prestart 正常跑完的上限;超过即判定为"挂起"
RUN_TIMEOUT_S = 240
#: lock_timeout=10s 时,失败应远早于这个值
FAST_FAIL_BUDGET_S = 60


def _dsn_for(db: str) -> str:
    return re.sub(r"/[^/]+$", f"/{db}", ADMIN_DSN)


def _admin_exec(sql: str) -> None:
    conn = psycopg2.connect(ADMIN_DSN, connect_timeout=10)
    conn.autocommit = True
    try:
        conn.cursor().execute(sql)
    finally:
        conn.close()


def _recreate(db: str) -> None:
    _admin_exec(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname='{db}' AND pid<>pg_backend_pid()"
    )
    _admin_exec(f'DROP DATABASE IF EXISTS "{db}"')
    _admin_exec(f'CREATE DATABASE "{db}"')


def _run_prestart(db: str, *, timeout: int = RUN_TIMEOUT_S,
                  extra_env: dict | None = None) -> tuple[int | None, str, float]:
    """跑一次 prestart。返回 (退出码, 合并输出, 耗时秒);挂起时退出码为 None。"""
    env = dict(os.environ)
    env["DATABASE_URL"] = _dsn_for(db)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("GEO_SKIP_STARTUP_TASKS", None)
    if extra_env:
        env.update(extra_env)
    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, "-m", "scripts.prestart"],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or ""), time.time() - t0
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return None, out, time.time() - t0


class _Blocker:
    """开一条连接,持 ACCESS SHARE 于 BLOCK_TABLE 不放(模拟飞轮长查询)。"""

    def __init__(self, db: str):
        self.conn = psycopg2.connect(_dsn_for(db), connect_timeout=10)
        self.conn.autocommit = False
        cur = self.conn.cursor()
        cur.execute(f"SELECT count(*) FROM {BLOCK_TABLE}")
        cur.fetchall()          # 事务开着不提交 ⇒ ACCESS SHARE 一直被持有
        cur.execute("SELECT pg_backend_pid()")
        self.pid = cur.fetchone()[0]

    def close(self) -> None:
        try:
            self.conn.rollback()
            self.conn.close()
        except Exception:
            pass


PROD_SNAPSHOT = (REPO / "tests" / "inventory_distribution_chain_2026_08_12"
                 / "prod_schema_2026-08-12.sql")


def _load_prod_shape(db: str) -> None:
    """把生产形状 schema 快照灌进库。

    🔴 夹具头部的 `SET search_path=''` 只毒**同一条连接**;prestart 另开连接,不受影响。
       (memory: pgdump-fixture-search-path-trap-2026-08-12)
    """
    _recreate(db)
    sql = PROD_SNAPSHOT.read_text(encoding="utf-8", errors="replace")
    conn = psycopg2.connect(_dsn_for(db), connect_timeout=10)
    conn.autocommit = True
    try:
        conn.cursor().execute(sql)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def base_db() -> str:
    """基准库 = **生产形状快照 + 跑一遍 prestart**,后续判据都在「重放」场景上做。

    🔴 为什么不用真空库(这是实测结论,不是设计偏好):
       真空库上 prestart 会挂在 `relation "users" does not exist` ——
       迁移依赖 `users`,而 prestart 的 bootstrap 只引导写作基础 schema。
       **2026-08-16 实测:未改动的基线树 `72faac33` 在真空库上同样 rc=4、同样这条错**
       ⇒ 这是**既有状态,与本单改动无关**;真空库本来就不是现役代码支持的起点。
       生产的真实场景本来也是「已有 schema 上重放」,所以基准取生产形状才是同口径。
    """
    if not PROD_SNAPSHOT.exists():
        pytest.skip(f"生产形状夹具不存在: {PROD_SNAPSHOT}")
    _load_prod_shape(DB_CLEAN)
    rc, out, _ = _run_prestart(DB_CLEAN)
    assert rc == 0, f"生产形状库首次 prestart 就失败,后面判据无意义:\n{out[-4000:]}"
    return DB_CLEAN


# ---------------------------------------------------------------- §2-2 反向
def test_no_blocker_prestart_succeeds(base_db):
    """🔴 反向对照:无长事务时全量重放必须成功 —— 证明新加的两个 timeout 不是恒失败。"""
    rc, out, elapsed = _run_prestart(base_db)
    assert rc == 0, f"无阻塞时 prestart 应成功,实得 rc={rc} 耗时={elapsed:.1f}s\n{out[-3000:]}"
    assert "lock_timeout" in out, "日志应打印本次生效的闸门值(可诊断性)"


# ---------------------------------------------------------------- §2-1 正向
def test_blocker_causes_fast_fail_with_pid(base_db):
    """正向:有长事务持 ACCESS SHARE → prestart 快速失败,且错误信息**点名 blocker pid**。"""
    b = _Blocker(base_db)
    try:
        rc, out, elapsed = _run_prestart(base_db, timeout=RUN_TIMEOUT_S)
    finally:
        b.close()

    assert rc is not None, (
        f"prestart 挂起了({elapsed:.1f}s 未返回)—— lock_timeout 没起作用")
    assert rc != 0, f"有阻塞者时 prestart 必须失败,实得 rc=0\n{out[-3000:]}"
    assert elapsed < FAST_FAIL_BUDGET_S, (
        f"应快速失败,实际耗时 {elapsed:.1f}s(预算 {FAST_FAIL_BUDGET_S}s)")
    assert "55P03" in out or "lock_timeout" in out, f"错误应指明是锁超时:\n{out[-2000:]}"
    assert str(b.pid) in out, (
        f"🔴 错误信息必须点名 blocker pid={b.pid},否则下一个人还得自己去 "
        f"pg_stat_activity 摸一遍:\n{out[-3000:]}")


# ---------------------------------------------------------------- §2-1 附:切流前
def test_failure_leaves_db_unmodified(base_db):
    """lock_timeout 失败时迁移一行都没执行 —— 判据打 schema 指纹前后一致。"""
    def fingerprint() -> str:
        conn = psycopg2.connect(_dsn_for(base_db), connect_timeout=10)
        try:
            c = conn.cursor()
            c.execute("SELECT md5(string_agg(t, ',' ORDER BY t)) FROM ("
                      "SELECT table_name||':'||column_name AS t "
                      "FROM information_schema.columns WHERE table_schema='public') s")
            return c.fetchone()[0]
        finally:
            conn.close()

    before = fingerprint()
    b = _Blocker(base_db)
    try:
        rc, _out, _ = _run_prestart(base_db)
    finally:
        b.close()
    assert rc not in (0, None)
    assert fingerprint() == before, "lock_timeout 失败后 schema 不应有任何改动"


# ---------------------------------------------------------------- §2-4 变异
def test_mutation_without_lock_timeout_hangs(base_db):
    """🔴 变异:把 `SET lock_timeout` 关掉(设为 0 = 无限等)→ 正向那条必须从「快速失败」变「挂起」。

    这一条是整套判据的**判别力证明**:它证明上面那个"10s 失败"确实是 lock_timeout 造成的,
    而不是别的什么东西碰巧让它失败。用**超时**来判"挂起"。
    """
    b = _Blocker(base_db)
    try:
        rc, out, elapsed = _run_prestart(
            base_db,
            timeout=FAST_FAIL_BUDGET_S,                     # 只给正向那条的预算
            extra_env={"PRESTART_MIGRATION_LOCK_TIMEOUT": "0"},   # 0 = 无限等 = 变异
        )
    finally:
        b.close()
    assert rc is None, (
        f"拆掉 lock_timeout 后应【挂起】,实得 rc={rc} 耗时={elapsed:.1f}s —— "
        f"说明上面那条'快速失败'不是 lock_timeout 造成的,整套判据作废\n{out[-2000:]}")


# ---------------------------------------------------------------- §2-3 回归
@pytest.mark.parametrize("run", [1, 2])
def test_all_migrations_replay_clean(run, base_db):
    """🔴 回归:两个 timeout 不该改变任何迁移的成功路径 —— 生产形状库连续重放两遍无新错。

    (原判据写「干净库 + 生产形状库」;真空库经实测在**基线树上同样失败**,
     不是现役代码支持的起点 —— 见 `base_db` 注释。故两遍都在生产形状上做。)
    """
    rc, out, elapsed = _run_prestart(base_db)
    tail = out[-4000:]
    assert rc == 0, (
        f"第 {run} 遍全量重放失败(rc={rc} 耗时={elapsed:.1f}s)—— "
        f"两个 timeout 改变了迁移的成功路径:\n{tail}")
