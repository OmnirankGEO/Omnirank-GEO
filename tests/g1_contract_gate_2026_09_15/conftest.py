"""WO_209 c1 判据包 · G1 响应契约门禁本体。

🔴 门禁 `import server`,导入期会建连接池并跑 `init_db()`(有 DDL **和** DML)。
   所以它必须指着一个**一次性本机靶库**,绝不允许指生产 ——
   preflight 里那条「库地址不是本机就拒跑」就是为此存在的。
   本包自建 `geo_g1_gate_test`,从生产 schema 快照恢复。
"""
import io
import os
import subprocess
from pathlib import Path

import psycopg2
import pytest

DB_NAME = "geo_g1_gate_test"
PG_CONTAINER = "defgeo-c14-62-pg"
ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"
TEST_DSN = "postgresql://geo_admin:testpw@localhost:55492/%s" % DB_NAME
PROD_SCHEMA = Path("C:/AI-Test/.deploy_toolkit/prod_schema_2026-09-05.sql")


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", TEST_DSN)
    # 🔴 门禁读的是 DATABASE_URL(它要 import server),不是 TEST_DATABASE_URL。
    os.environ["DATABASE_URL"] = TEST_DSN
    os.environ.setdefault("GEO_SKIP_STARTUP_TASKS", "1")


def _admin(sql):
    c = psycopg2.connect(ADMIN_DSN)
    c.autocommit = True
    try:
        c.cursor().execute(sql)
    finally:
        c.close()


def _db_ok():
    try:
        c = psycopg2.connect(TEST_DSN)
    except Exception:                      # noqa: BLE001
        return False
    try:
        cur = c.cursor()
        cur.execute("SELECT to_regclass('public.topics')")
        return cur.fetchone()[0] is not None
    finally:
        c.close()


def pytest_sessionstart(session):
    if _db_ok():
        return
    try:
        _admin('CREATE DATABASE "%s"' % DB_NAME)
    except Exception:                      # noqa: BLE001
        pass                               # 库壳已在,下面照样恢复 schema
    sql = io.open(PROD_SCHEMA, encoding="utf-8").read()
    sql = chr(10).join(l for l in sql.splitlines() if not l.startswith(chr(92)))
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-v", "ON_ERROR_STOP=0",
         "-U", "geo_admin", "-d", DB_NAME],
        input=sql.encode("utf-8"), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1200:])


@pytest.fixture(scope="session")
def gate():
    """门禁模块本体(按文件加载 —— scripts/ 不是包)。"""
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "g1_gate", root / "scripts" / "response_model_contract_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def collected(gate):
    """真跑一次路由收集。**分母就地断言**:0 条就不是"没有违规"。"""
    routes, total = gate.collect_routes()
    assert total > 0, "一条路由都没收集到 —— 门禁没跑起来,不是仓库干净"
    assert routes, "没有一条路由声明 response_model —— 覆盖数 0 = 门禁是瞎的"
    return routes, total
