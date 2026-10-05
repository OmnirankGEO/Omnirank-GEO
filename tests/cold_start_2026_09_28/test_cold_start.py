# -*- coding: utf-8 -*-
"""开源 E10 硬前置 · 空库冷启动(Review 09-28):prestart 在空库上先按 db/cold_start.BOOTSTRAP 建基表再跑 manifest。

真 PG 的整条冷启动(空库 → prestart → import server 零 WARNING → 必跑集)由 C 的 scripts/oss_export/selfcheck.py 跑(约 3 分钟,
不进 ONESHOT 预算);本包是**不连库的结构锁**,守住那条冷启动赖以成立的几件事,每件都有反臂:

  K1 🔴 BOOTSTRAP 每一步的目标都在:sql 文件在仓里;py / pycur 的模块文件在且定义了该顶层函数;import 模块文件在;ddl 是 CREATE TABLE IF NOT EXISTS
  K2 🔴 prestart 在「import db.diagnosis_db」与「for rel in MIGRATIONS」之前调 cold_start.bootstrap
  K3 🔴 热库(哨兵 users 在)⇒ bootstrap 只发一条探测查询、不导入任何模块、不跑任何一步、返回 False(生产空操作)
  K4 🔴 冷库 ⇒ 按列表顺序逐步执行,四种步骤各走各的执行路径;未知种类抛错
  K6 🔴 db/diagnosis_db.py 的 v1.4 列清单里不许写 `DEFAULT NULL`(冷启动时 PG 存成显式默认,v1.4 schema 合同报 unexpected_default)
  K7 🔴 server.py 不再导入不存在的 api.social_quality_api(冷启动 import server 多一条 WARNING)
  K8 🔴 BOOTSTRAP 清单(种类, 目标)按顺序冻结:删一步 / 换顺序 ⇒ 红(真 PG 上删 migration_001_teams ⇒ prestart rc 4 user_notifications)
"""
from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import db.cold_start as cs  # noqa: E402  只导入这个纯模块(不连库)


def _module_path(mod: str) -> Path:
    return ROOT / (mod.replace(".", "/") + ".py")


def _defines(path: Path, fn: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn for n in tree.body)


def target_problems(steps) -> list:
    out = []
    for kind, target, why in steps:
        if not why.strip():
            out.append(f"{target} 没写「谁需要它」")
        if kind == "sql":
            if not (ROOT / target).is_file():
                out.append(f"sql 文件不在:{target}")
        elif kind in ("py", "pycur"):
            mod, _, fn = target.partition(":")
            p = _module_path(mod)
            if not fn or not p.is_file() or not _defines(p, fn):
                out.append(f"{kind} 目标不在:{target}")
        elif kind == "import":
            if not _module_path(target).is_file():
                out.append(f"import 模块不在:{target}")
        elif kind == "ddl":
            if not re.match(r"(?is)^\s*CREATE TABLE IF NOT EXISTS\s+\w+\s*\(", target):
                out.append(f"ddl 不是 CREATE TABLE IF NOT EXISTS:{target[:40]}")
        else:
            out.append(f"未知种类 {kind}")
    return out


def test_k1_every_bootstrap_target_exists():
    assert len(cs.BOOTSTRAP) >= 20
    assert target_problems(cs.BOOTSTRAP) == []
    # 反臂:文件不在 / 函数名错 / 模块不在 / ddl 不是 IF NOT EXISTS / 没写理由 ⇒ 各中
    for bad in [("sql", "scripts/nope_2026.sql", "x"), ("py", "db.auth_db:no_such_fn", "x"), ("import", "db.no_such_mod", "x"),
                ("ddl", "CREATE TABLE t (id int)", "x"), ("sql", cs.BOOTSTRAP[0][1] if cs.BOOTSTRAP[0][0] == "sql" else "db/migration_001_teams.sql", " ")]:
        assert target_problems([bad]), bad


#: 冻结的引导清单(种类, 目标)。每一步都在空库实跑里被点名需要过(删掉任何一步 ⇒ prestart rc 4 红在它下游的迁移上);
#: 改清单必须同笔改这里,并附空库冷启动读数。
FROZEN = [
    ("py", "db.auth_db:init_auth_db"),
    ("py", "db.wallet_db:init_wallet_tables"),
    ("py", "db.publish_db:init_publish_tables"),
    ("py", "db.meijiehezi_db:init_mhz_tables"),
    ("sql", "db/migration_008_pricing_llm_first.sql"),
    ("sql", "scripts/migration_v35_factory_inventory_2026_05_26.sql"),
    ("sql", "scripts/migration_v35_w2_2026_05_26.sql"),
    ("sql", "scripts/migration_v35_w3_2026_05_26.sql"),
    ("sql", "scripts/migration_v35_w4_2026_05_26.sql"),
    ("ddl", "_migrations"),
    ("sql", "scripts/migration_04_geo_plan_tasks.sql"),
    ("py", "db.migration_009_subscription_v2:run_migration"),
    ("pycur", "db.brands_schema:ensure_brands_schema"),
    ("import", "db.diagnosis_db"),
    ("sql", "db/migration_005_compliance.sql"),
    ("sql", "db/migration_006_effective_rate.sql"),
    ("py", "db.monitoring_db:init_monitoring_tables"),
    ("py", "db.publish_records_schema:init_publish_records_table"),
    ("py", "db.media_entity_flywheel_db:init_media_entity_flywheel_tables"),
    ("py", "db.geo_source_signals_db:init_geo_source_signal_tables"),
    ("py", "db.writing_style_flywheel_db:init_writing_style_flywheel_tables"),
    ("sql", "db/migration_001_teams.sql"),
    ("ddl", "client_profiles"),
    ("sql", "scripts/migration_v36_agent_multi_package_2026_06_06.sql"),
]


def _shape(steps) -> list:
    out = []
    for kind, target, _ in steps:
        if kind == "ddl":
            target = re.match(r"(?is)^\s*CREATE TABLE IF NOT EXISTS\s+(\w+)", target).group(1)
        out.append((kind, target))
    return out


def test_k8_bootstrap_list_is_frozen_in_order():
    assert _shape(cs.BOOTSTRAP) == FROZEN
    no_teams = [s for s in cs.BOOTSTRAP if s[1] != "db/migration_001_teams.sql"]
    assert len(no_teams) == len(cs.BOOTSTRAP) - 1 and _shape(no_teams) != FROZEN                               # 反臂:删一步
    swapped = list(cs.BOOTSTRAP); swapped[2], swapped[3] = swapped[3], swapped[2]
    assert _shape(swapped) != FROZEN                                                                            # 反臂:publish/mhz 顺序对调


def _prestart_order(src: str) -> list:
    """prestart.main 里三件事的出现顺序(按源码行):bootstrap 调用、diagnosis_db 导入、manifest 循环"""
    tree = ast.parse(src)
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    marks = []
    for n in ast.walk(main):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_cold_start_bootstrap":
            marks.append(("bootstrap", n.lineno))
        if isinstance(n, ast.Import) and any(a.name == "db.diagnosis_db" for a in n.names):
            marks.append(("diagnosis", n.lineno))
        if isinstance(n, ast.For) and isinstance(n.iter, ast.Name) and n.iter.id == "MIGRATIONS":
            marks.append(("manifest", n.lineno))
    return [k for k, _ in sorted(marks, key=lambda x: x[1])]


def test_k2_prestart_bootstraps_before_everything_else():
    src = (ROOT / "scripts" / "prestart.py").read_text(encoding="utf-8")
    assert _prestart_order(src) == ["bootstrap", "diagnosis", "manifest"]
    # [0913AO] 调用行收下返回值(_was_cold,决定是否执行冷启动对齐基线)
    call = "        _was_cold = _cold_start_bootstrap(cur, root, log=logger, prefix=\"[prestart] \")\n"
    assert src.count(call) == 1, "反臂没下成"
    assert _prestart_order(src.replace(call, "")) != ["bootstrap", "diagnosis", "manifest"]                     # 删掉调用
    moved = src.replace(call, "").replace("        for rel in MIGRATIONS:\n", "        for rel in MIGRATIONS:\n" + "    " + call, 1)
    assert _prestart_order(moved)[0] != "bootstrap"                                                             # 挪到 manifest 循环里


class FakeCursor:
    def __init__(self, cold: bool):
        self.cold, self.sql = cold, []

    def execute(self, q, params=None):
        self.sql.append(q)

    def fetchone(self):
        return (self.cold,)


def test_k3_warm_database_is_untouched(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("热库上不许导入任何建表模块")
    monkeypatch.setattr(importlib, "import_module", boom)
    cur = FakeCursor(cold=False)
    assert cs.bootstrap(cur, ROOT) is False
    assert len(cur.sql) == 1 and "to_regclass" in cur.sql[0]


def test_k4_cold_database_runs_every_step_in_order(monkeypatch):
    calls = []

    class Mod:
        def __getattr__(self, name):
            return lambda *a: calls.append(("call", name, len(a)))

    def fake_import(name):
        calls.append(("import", name))
        return Mod()
    monkeypatch.setattr(importlib, "import_module", fake_import)
    cur = FakeCursor(cold=True)
    assert cs.bootstrap(cur, ROOT) is True
    want = []
    for kind, target, _ in cs.BOOTSTRAP:
        if kind in ("py", "pycur"):
            mod, fn = target.split(":")
            want += [("import", mod), ("call", fn, 1 if kind == "pycur" else 0)]
        elif kind == "import":
            want.append(("import", target))
    assert calls == want
    n_sql = sum(1 for k, _, _ in cs.BOOTSTRAP if k in ("sql", "ddl"))
    assert len(cur.sql) == 1 + n_sql
    monkeypatch.setattr(cs, "BOOTSTRAP", [("zzz", "x", "y")])
    with pytest.raises(ValueError):
        cs.bootstrap(FakeCursor(cold=True), ROOT)


def _v14_block(src: str) -> str:
    a = src.index("# GEO article v1.4 A7/A8")
    return src[a:src.index("]:", a)]


def test_k6_no_explicit_null_default_in_v14_runtime_columns():
    src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    blk = _v14_block(src)
    assert blk.count('("') >= 20, "分母:v1.4 列清单没读到"
    assert "DEFAULT NULL" not in blk
    assert "DEFAULT NULL" in _v14_block(src.replace('("style_family", "VARCHAR(64)"', '("style_family", "VARCHAR(64) DEFAULT NULL"'))  # 反臂


def test_k7_server_does_not_import_the_missing_social_quality_api():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert not (ROOT / "api" / "social_quality_api.py").exists()
    assert "api.social_quality_api" not in src
