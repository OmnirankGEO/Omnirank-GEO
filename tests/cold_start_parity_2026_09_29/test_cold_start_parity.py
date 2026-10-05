# -*- coding: utf-8 -*-
"""E10 · 0913AO · 空库冷启动与生产 schema 对齐(Review 09-28)。

病:本仓一部分表 / 列只由不在 manifest 里的历史 SQL / 手工 ALTER 建出来,生产上有、冷库上没有。
    C 在 AN(065188aaf)自足格对照臂:title_request_replay 4 格 ERROR「column brand_id of confirmed_keywords
    does not exist」;在役 api/selection_api._mark_keywords_monitored(确认报价 / 开监测)
    `UPDATE confirmed_keywords … RETURNING id, brand_id` 在冷库上报同一句 —— 而且被 fail-soft 吞掉:
    整段回滚、只记一条「该单监测未开通,需人工确认」,确认报价照样返回成功(本机两冷库实测)。
修:scripts/gen_cold_start_parity.py 从生产 schema 快照生成 db/cold_start_parity.sql
    (缺表只收在役代码有 SQL 引用的,缺列全收 ADD COLUMN IF NOT EXISTS);prestart 在**开跑时是空库**时、
    跑完 manifest 之后执行它 ⇒ 生产库一步不走。

真 PG 的整条冷启动(空库 → prestart → import server → 必跑集)由 scripts/oss_export/selfcheck.py 对照臂跑;
本包是不连库的结构锁:
  Q1 🔴 prestart:apply_parity 只在 `_was_cold`(= bootstrap 的返回)为真时调,排在 manifest 循环之后、fleet 守卫之前
  Q2 🔴 对齐 SQL 里有 confirmed_keywords.brand_id(确认报价 RETURNING 它)与其余已知在役列
  Q3 🔴 对齐 SQL 不带 OWNER / GRANT / COMMENT / SET / psql 反斜杠命令(生产注释可能写着内部排障细节)
  Q4 🔴 对齐 SQL 收的每张表,非测试后端代码里都有 SQL 引用(生成规则:未用的表不收)
  Q5 🔴 apply_parity 文件缺失时抛错(冷启动不许静默少建);执行的是去掉 BEGIN/COMMIT/反斜杠后的整份文件
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PARITY = ROOT / "db" / "cold_start_parity.sql"


def _prestart_marks(src: str) -> dict:
    tree = ast.parse(src)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    marks = {}
    for n in ast.walk(main):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                and getattr(n.value.func, "id", None) == "_cold_start_bootstrap":
            marks["was_cold_assigned"] = [t.id for t in n.targets if isinstance(t, ast.Name)]
        if isinstance(n, ast.For) and getattr(n.iter, "id", None) == "MIGRATIONS":
            marks["manifest"] = n.lineno
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "run_fleet_schema_guards":
            marks["guards"] = n.lineno
        if isinstance(n, ast.If) and getattr(n.test, "id", None) == "_was_cold":
            calls = [c for c in ast.walk(n) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_cold_start_parity"]
            if calls:
                marks["parity"] = calls[0].lineno
    return marks


def prestart_problems(src: str) -> list:
    m = _prestart_marks(src)
    out = []
    if m.get("was_cold_assigned") != ["_was_cold"]:
        out.append("bootstrap 的返回没有赋给 _was_cold")
    if "parity" not in m:
        out.append("apply_parity 不在 `if _was_cold:` 之内(或没调)")
    elif not (m.get("manifest", 10 ** 9) < m["parity"] < m.get("guards", -1)):
        out.append("apply_parity 不在 manifest 之后、fleet 守卫之前")
    return out


def test_q1_parity_runs_only_on_a_cold_database_after_the_manifest():
    src = (ROOT / "scripts" / "prestart.py").read_text(encoding="utf-8")
    assert prestart_problems(src) == []
    guard = "        if _was_cold:\n"
    assert src.count(guard) == 1, "毒没下成"
    assert prestart_problems(src.replace(guard, "        if True:\n"))                 # 毒:生产也跑
    assert prestart_problems(src.replace(                                              # 毒:不调
        '            _cold_start_parity(cur, root, log=logger, prefix="[prestart] ")\n', "            pass\n"))


def _parity() -> str:
    return PARITY.read_text(encoding="utf-8")


def test_q2_known_live_columns_are_in_the_parity_sql():
    sql = _parity()
    must = [('confirmed_keywords', 'brand_id')]
    for t, c in must:
        assert 'ALTER TABLE public.%s ADD COLUMN IF NOT EXISTS "%s" ' % (t, c) in sql, (t, c)
    # 在役读写方:确认报价那条 RETURNING
    sel = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    assert "RETURNING id, brand_id" in sel


_FORBIDDEN = re.compile(r"(?im)^\s*(ALTER\s+\S+.*\s+OWNER\s+TO\b|GRANT\b|REVOKE\b|COMMENT\s+ON\b|SET\s|\\\w)")


def test_q3_no_owner_grant_comment_or_psql_meta_commands():
    assert not _FORBIDDEN.findall(_parity())
    assert _FORBIDDEN.findall("COMMENT ON TABLE x IS 'y';\n")                         # 牙证


def _code_texts() -> dict:
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    out = {}
    for f in files:
        if f.startswith(("tests/", "docs/", "frontend/")) or "/tests/" in f:
            continue
        try:
            out[f] = (ROOT / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass
    return out


def test_q4_every_created_table_is_referenced_by_live_code():
    tables = re.findall(r"(?m)^CREATE TABLE public\.(\w+) \(", _parity())
    assert len(tables) >= 50, "分母:对齐 SQL 里的建表没读到"
    blob = "\n".join(_code_texts().values())
    unused = [t for t in tables
              if not re.search(r"(?i)\b(FROM|INTO|UPDATE|JOIN|TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?)\s+(?:public\.)?\"?%s\"?\b"
                               % re.escape(t), blob)]
    assert unused == [], unused


def _manifest_created_tables() -> set:
    from db.migration_manifest import MIGRATIONS
    out = set()
    rx = re.compile(r"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?(\w+)\"?\s*\(")
    for rel in MIGRATIONS:
        p = ROOT / rel
        if p.exists():
            out |= set(rx.findall(p.read_text(encoding="utf-8", errors="ignore")))
    return out


def test_q6_parity_never_creates_a_table_the_manifest_creates():
    """[WO_318 · Review 可选格] 同一张表两份定义 = 迁移改了它、冷库照快照建的那份不跟 —— 不许有。"""
    created = set(re.findall(r"(?m)^CREATE TABLE public\.(\w+) \(", _parity()))
    manifest = _manifest_created_tables()
    assert len(manifest) >= 50, "分母:manifest 建表没读到"
    assert created & manifest == set(), sorted(created & manifest)
    some = sorted(manifest)[0]                                                          # 牙证:拿 manifest 里一张表造一行
    assert {some} & manifest and re.findall(r"(?m)^CREATE TABLE public\.(\w+) \(", "CREATE TABLE public.%s (\n" % some)


def test_q7_type_sensitive_columns_are_aligned_to_the_production_type():
    """[WO_318] 仓内建表写 BOOLEAN、生产是 smallint、在役代码按整数读写的三列,冷库要对齐成 smallint。"""
    sql = _parity()
    for t, c, dflt in (("employee_configs", "is_active", "1"), ("departments", "is_active", "1"),
                       ("employee_meetings", "is_internal", "0")):
        assert 'ALTER TABLE public.%s ALTER COLUMN "%s" TYPE smallint USING ("%s"::integer);' % (t, c, c) in sql, (t, c)
        assert 'ALTER TABLE public.%s ALTER COLUMN "%s" SET DEFAULT %s;' % (t, c, dflt) in sql, (t, c)
    ddb = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    assert "e.is_active = 1" in ddb and "SET is_active = 0" in ddb and "int(is_internal)" in ddb   # 在役读写方仍按整数


def test_q5_apply_parity_fails_loud_when_the_file_is_missing(tmp_path):
    import db.cold_start as cs

    class Cur:
        def __init__(self):
            self.sql = []

        def execute(self, q, params=None):
            self.sql.append(q)

    with pytest.raises(RuntimeError, match="缺文件"):
        cs.apply_parity(Cur(), tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "cold_start_parity.sql").write_text("BEGIN;\nCREATE TABLE t (id int);\nCOMMIT;\n", encoding="utf-8")
    cur = Cur()
    cs.apply_parity(cur, tmp_path)
    assert len(cur.sql) == 1 and "CREATE TABLE t" in cur.sql[0] and "BEGIN" not in cur.sql[0]
