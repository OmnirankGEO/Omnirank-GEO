"""E0d · migration_064 的锁(2026-09-27):新库冷启动补齐 knowledge_chunks + advisors 两列,对生产是空操作。

背景(E3_DELETION_MAP §6-4):在役 GET /api/advisors(`api/advisor_api.py::list_advisors`)LEFT JOIN knowledge_chunks、
读 advisors.knowledge_base_path / last_kb_update;新库上代码里唯一的建表 / 补列处是 E3 要删的
`tools/social_operator/advisor_knowledge_pipeline.py`(已随开源 E3 B2 删)。

在本机测试 PG 服务器上建两个临时库(名字带 test,测完删),每个库在独立子进程里跑(DATABASE_URL 指过去):
  ① 冷启动:照 prestart 顺序先 `import db.diagnosis_db` 引导基础表 →
     a. 不打 064,调 list_advisors ⇒ 必须报错(缺陷真实存在 —— 这一格就是本锁的牙证:064 没生效就红);
     b. 用真执行器 `scripts.prestart._apply` 打 064 ⇒ list_advisors 返回 success(= 端点 200);
     c. 再打一次 064 ⇒ 结构指纹不变(幂等)。
  ② 生产形状:从生产 schema 快照里原样抠出 knowledge_chunks 的表 / 序列 / 约束 / 10 个索引 / 触发器函数 / 触发器
     与 advisors 表 → 打两次 064 ⇒ 结构指纹逐项不变(对生产空操作;生产那份触发器函数体不被覆盖)。
  ③ 形状对齐:① 里 064 建出来的 knowledge_chunks 指纹 == ② 里生产快照的指纹。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
MIG = "db/migration_064_knowledge_chunks_coldstart_2026_09_27.sql"
SNAPSHOT = ROOT / "tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql"  # 仓内最新的生产快照
BASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="需要 TEST_DATABASE_URL(本机测试 PG)")


def _url_for(dbname: str) -> str:
    u = urlparse(BASE_URL)
    return urlunparse(u._replace(path="/" + dbname))


def _new_db(tag: str) -> str:
    name = f"e0d_{tag}_test_{uuid.uuid4().hex[:8]}"
    assert "test" in name and "prod" not in name  # 本机测试库命名规矩
    admin = psycopg2.connect(BASE_URL)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    return _url_for(name)


def _drop_db(url: str) -> None:
    name = urlparse(url).path.lstrip("/")
    admin = psycopg2.connect(BASE_URL)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin.close()


def _fingerprint(url: str) -> dict:
    """knowledge_chunks 的列 / 约束 / 索引 / 触发器 / 触发器函数定义 + advisors 两列类型。"""
    c = psycopg2.connect(url)
    try:
        cur = c.cursor()
        out = {}
        cur.execute("""SELECT column_name, data_type, udt_name, character_maximum_length, column_default, is_nullable
                       FROM information_schema.columns WHERE table_schema='public' AND table_name='knowledge_chunks'
                       ORDER BY ordinal_position""")
        out["kc_columns"] = [list(map(str, r)) for r in cur.fetchall()]
        cur.execute("""SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
                       WHERE conrelid = to_regclass('public.knowledge_chunks') ORDER BY conname""")
        out["kc_constraints"] = cur.fetchall()
        cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='knowledge_chunks' ORDER BY indexname")
        out["kc_indexes"] = cur.fetchall()
        cur.execute("""SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger
                       WHERE tgrelid = to_regclass('public.knowledge_chunks') AND NOT tgisinternal ORDER BY tgname""")
        out["kc_triggers"] = cur.fetchall()
        cur.execute("""SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                       WHERE n.nspname='public' AND p.proname='kc_tsv_trigger'""")
        out["kc_tsv_trigger_def"] = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                       WHERE table_schema='public' AND table_name='advisors'
                         AND column_name IN ('knowledge_base_path','last_kb_update') ORDER BY column_name""")
        out["advisors_cols"] = cur.fetchall()
        return json.loads(json.dumps(out, default=str))
    finally:
        c.close()


_CHILD = r'''
import asyncio, json, os, sys
from types import SimpleNamespace
sys.path.insert(0, os.environ["E0D_ROOT"])
os.chdir(os.environ["E0D_ROOT"])
mode = sys.argv[1]
root = os.environ["E0D_ROOT"]


def apply_064():
    import psycopg2
    from pathlib import Path
    from scripts.prestart import _apply   # 真执行器:与生产 prestart 同一段剥离 + execute
    c = psycopg2.connect(os.environ["DATABASE_URL"]); c.autocommit = True
    _apply(c.cursor(), Path(root), os.environ["E0D_MIG"]); c.close()


def call_list():
    from api.advisor_api import list_advisors
    req = SimpleNamespace(state=SimpleNamespace(user={"user_id": 1, "is_admin": False}), headers={}, query_params={})
    try:
        res = asyncio.run(list_advisors(req, role=None, industry=None, include_empty=False, admin=False))
        return {"ok": bool(res.get("success"))}
    except Exception as e:
        return {"ok": False, "err": type(e).__name__ + ": " + str(e)[:160]}


if mode == "coldstart":
    import db.diagnosis_db  # noqa: F401  与 prestart 同:先引导基础 schema(含 advisors)
    out = {"before": call_list()}
    apply_064()
    out["after"] = call_list()
    print("RESULT " + json.dumps(out))
'''


def _child(url: str, mode: str) -> dict:
    env = dict(os.environ, DATABASE_URL=url, E0D_ROOT=str(ROOT), E0D_MIG=MIG, PYTHONIOENCODING="utf-8")
    env.pop("TEST_DATABASE_URL", None)
    r = subprocess.run([sys.executable, "-c", _CHILD, mode], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=240)
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
    assert r.returncode == 0 and lines, (r.returncode, r.stdout[-800:], r.stderr[-1200:])
    return json.loads(lines[-1][len("RESULT "):])


def _prod_shape_statements() -> list[str]:
    """从生产 schema 快照原样抠出 knowledge_chunks 相关对象与 advisors 表。"""
    s = SNAPSHOT.read_text(encoding="utf-8")
    pats = [
        r"CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;",
        r"CREATE FUNCTION public\.kc_tsv_trigger\(\).*?\n\$\$;",
        r"CREATE TABLE public\.advisors \(.*?\n\);",
        r"CREATE TABLE public\.knowledge_chunks \(.*?\n\);",
        r"CREATE SEQUENCE public\.knowledge_chunks_id_seq.*?;",
        r"ALTER SEQUENCE public\.knowledge_chunks_id_seq OWNED BY public\.knowledge_chunks\.id;",
        r"ALTER TABLE ONLY public\.knowledge_chunks ALTER COLUMN id SET DEFAULT [^;]*;",
        r"ALTER TABLE ONLY public\.knowledge_chunks\n    ADD CONSTRAINT [^;]*;",
        r"CREATE (?:UNIQUE )?INDEX \w+ ON public\.knowledge_chunks [^;]*;",
        r"CREATE TRIGGER kc_tsv_update [^;]*;",
    ]
    out = []
    for p in pats:
        found = re.findall(p, s, flags=re.S)
        assert found, f"快照里找不到:{p}"
        out.extend(found)
    return out


def _apply_in_process(url: str) -> None:
    """[ONESHOT 提速 4] 只打迁移的那几次不再起子进程:同一个真执行器 `scripts.prestart._apply`、
    同样 autocommit 连接到那个临时库。冷启动那次仍在子进程里(它要一个干净进程里的首次 import)。"""
    from scripts.prestart import _apply

    c = psycopg2.connect(url)
    c.autocommit = True
    try:
        _apply(c.cursor(), ROOT, MIG)
    finally:
        c.close()


def _both(fn, *args):
    """两个临时库的建 / 删各自独立,并行做(建库、删库是本包大头)。"""
    with ThreadPoolExecutor(max_workers=len(args)) as ex:
        return list(ex.map(fn, args))


@pytest.fixture(scope="module")
def readings():
    """两个临时库各跑一次,三格测试共享读数(冷启动那一次起子进程)。"""
    cold, shape = _both(_new_db, "cold", "shape")
    try:
        r = {"cold": _child(cold, "coldstart")}
        r["cold_fp1"] = _fingerprint(cold)
        _apply_in_process(cold)
        r["cold_fp2"] = _fingerprint(cold)
        c = psycopg2.connect(shape)
        c.autocommit = True
        for stmt in _prod_shape_statements():
            c.cursor().execute(stmt)
        c.close()
        r["prod_fp0"] = _fingerprint(shape)
        _apply_in_process(shape)
        _apply_in_process(shape)
        r["prod_fp2"] = _fingerprint(shape)
        yield r
    finally:
        _both(_drop_db, cold, shape)


# ---------- ① 冷启动 ----------

def test_coldstart_advisors_list_breaks_without_064_and_works_with_it(readings) -> None:
    before, after = readings["cold"]["before"], readings["cold"]["after"]
    assert before["ok"] is False, f"新库上不打 064 就能跑通?那 §6-4 的缺陷不成立,或本锁没咬到:{before}"
    assert after["ok"] is True, after
    assert readings["cold_fp2"] == readings["cold_fp1"], "064 再跑一次改了结构 —— 不幂等"
    assert readings["cold_fp1"]["advisors_cols"] == [["knowledge_base_path", "text"],
                                                     ["last_kb_update", "timestamp without time zone"]]


# ---------- ② 生产形状:空操作 ----------

def test_064_is_a_noop_on_production_shape(readings) -> None:
    fp0 = readings["prod_fp0"]
    assert fp0["kc_indexes"] and fp0["kc_triggers"] and fp0["kc_tsv_trigger_def"]
    assert readings["prod_fp2"] == fp0, "064 在生产形状的库上改了东西 —— 不是空操作"


# ---------- ③ 形状对齐 ----------

def test_064_creates_the_production_shape_on_a_fresh_db(readings) -> None:
    """新库上 064 建出来的 knowledge_chunks 与生产快照逐项相同(列 / 约束 / 索引 / 触发器 / 函数体)。"""
    mine, prod = readings["cold_fp1"], readings["prod_fp0"]
    for key in ("kc_columns", "kc_constraints", "kc_indexes", "kc_triggers", "kc_tsv_trigger_def"):
        assert mine[key] == prod[key], (key, mine[key], prod[key])
