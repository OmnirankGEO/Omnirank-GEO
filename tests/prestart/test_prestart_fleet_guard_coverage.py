"""锁:`scripts/prestart.py` 的守卫集必须与 `server.py` web/cron 口径一致(单一来源)。

工单:`docs/AI-CONTEXT/WORKORDER_PRESTART_GUARD_COVERAGE_AND_ROLLBACK_PROBE_2026-07-30.md` §2.3

守的性质(三条 + 反漂移):
1. 任一守卫抛错 → `prestart.main()` **非零返回**(deploy 在 `[2.5/8]` halt · 候选不启动),
   且**后续守卫不再跑**(fail-fast,不把坏 schema 一路验完再说);
2. 全绿 → 返回 0,**且日志里每一道都有自己的 PASS 行**
   —— 只验退出码的话,谁把某道调用删了照样绿(工单 §2.3 锁 2 原话);
3. 清单唯一:`FLEET_SCHEMA_GUARDS` 与本模块实际定义的守卫**互为全集**
   (定义了不挂清单 / 挂清单没实现 都红),server.py 与 prestart.py 都只经
   `run_fleet_schema_guards()` 取用 —— 不允许任一侧再自己列一份。

这里用替身跑 `prestart.main()`(不连库):真库行为另有实证
(fresh PG16 上跑真 prestart → 退出 0 + 六道 PASS 行;空库上单独跑守卫 → 四道各自 raise)。
"""
import ast
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD_MODULE_PATH = ROOT / "services" / "startup_schema_guards.py"
SERVER_PATH = ROOT / "server.py"
PRESTART_PATH = ROOT / "scripts" / "prestart.py"

# 与迁移前 server.py:764-770 web/cron 分支**逐行同序**
EXPECTED_FLEET = [
    ("diagnosis", "verify_diagnosis_schema_fail_closed"),
    ("geo_article_v14", "verify_geo_article_v14_schema_fail_closed"),
    ("article_closed_loop", "verify_article_closed_loop_schema_fail_closed"),
    ("dealer_resale", "verify_dealer_resale_schema_fail_closed"),
    ("geo_observation", "verify_geo_observation_schema_fail_closed"),
    ("monitoring_product", "verify_monitoring_product_schema_fail_closed"),
]
# 本单补上的四道(prestart 历史上一道都没跑)
NEWLY_COVERED_BY_PRESTART = ["diagnosis", "dealer_resale", "geo_observation", "monitoring_product"]


# ======================= 反漂移:清单唯一 =======================

def test_fleet_list_is_the_single_source_and_matches_defined_guards():
    from services.startup_schema_guards import FLEET_SCHEMA_GUARDS, resolve_fleet_guard

    assert list(FLEET_SCHEMA_GUARDS) == EXPECTED_FLEET

    tree = ast.parse(GUARD_MODULE_PATH.read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.endswith("_fail_closed")
    }
    listed = {attr for _key, attr in FLEET_SCHEMA_GUARDS}
    # 定义了却没挂清单 = 白写一道;挂清单却没实现 = 解析期 fail-closed
    assert defined == listed, f"守卫定义与清单漂移: 只定义={defined - listed} 只挂清单={listed - defined}"
    for _key, attr in FLEET_SCHEMA_GUARDS:
        assert callable(resolve_fleet_guard(attr))


def test_missing_guard_in_list_fails_closed_instead_of_being_skipped():
    from services.startup_schema_guards import resolve_fleet_guard

    with pytest.raises(RuntimeError, match="FleetSchemaGuard"):
        resolve_fleet_guard("verify_a_guard_that_does_not_exist")


def _calls_runner(nodes) -> bool:
    """分支里是否**真有**一次 run_fleet_schema_guards(...) 调用(AST · 注释不算)。"""
    for stmt in nodes:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_fleet_schema_guards"
            ):
                return True
    return False


def _role_dispatch_branches():
    """取 server.py 里 ROLE 分派的 (prestart 分支, web/cron 分支) 语句列表。"""
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "_SCHED_ROLE"
            and isinstance(test.ops[0], ast.Eq)
            and getattr(test.comparators[0], "value", None) == "prestart"
        ):
            fleet = [
                inner
                for inner in node.orelse
                if isinstance(inner, ast.If)
                and isinstance(inner.test, ast.Compare)
                and isinstance(inner.test.ops[0], ast.In)
            ]
            assert len(fleet) == 1, "未找到 web/cron 分支"
            return node.body, fleet[0].body
    raise AssertionError("未找到 _SCHED_ROLE == 'prestart' 分派")


def test_both_startup_paths_go_through_the_shared_list_only():
    server_src = SERVER_PATH.read_text(encoding="utf-8")
    prestart_src = PRESTART_PATH.read_text(encoding="utf-8")

    # server.py:两个启动分支(手工 prestart + web/cron)都必须**真调用**共用清单。
    # 🔴 这里刻意用 AST 而不是 `"run_fleet_schema_guards(...)" in source`:
    #    把调用注释掉、字符串照样命中 → 变异能存活(实测 M3 就是这么活下来的)。
    prestart_branch, fleet_branch = _role_dispatch_branches()
    assert _calls_runner(prestart_branch), "server.py 手工 prestart 分支没真跑共用清单"
    assert _calls_runner(fleet_branch), "server.py web/cron 分支没真跑共用清单"
    assert 'elif _SCHED_ROLE in ("web", "cron")' in server_src
    # 任一侧再自己逐道列一份 = 漂移温床复活(老私有名 `_verify_*_fail_closed` 必须绝迹)
    for _key, attr in EXPECTED_FLEET:
        assert f"_{attr}" not in server_src, f"server.py 仍在单独引用 _{attr}"
        assert attr not in prestart_src, f"prestart.py 仍在单独列 {attr}"

    # prestart 真调那份清单
    assert "run_fleet_schema_guards(log=logger, prefix=" in prestart_src
    # dealer_resale 必须是 web/cron 口径(不带 require_full):prestart 比运行时更严会把部署卡死。
    # 注:只禁"真调用",不禁注释里提这个词(否定说法照样会被 grep 命中)。
    assert "require_full=True" not in prestart_src
    assert "require_full=True" not in server_src
    assert "require_full" not in GUARD_MODULE_PATH.read_text(encoding="utf-8").split(
        "# ========== 单一来源清单", 1
    )[1]


def test_prestart_runs_the_guards_after_the_migrations_not_before():
    """🔴 守卫验的是"迁移建完的对象" —— 排在迁移前必然误判(部署在 [2.5/8] 白 halt)。

    空库上的 fail-closed 实证**验不到这个顺序问题**(那里两种顺序都会红),
    所以这里用 AST 直接钉住 prestart.main() 里两者的先后。
    """
    tree = ast.parse(PRESTART_PATH.read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )

    # 收**全部**出现处再比 max/min:只记最后一次的话,"迁移前也插一次"这种变异会存活
    migration_lines, guard_lines = [], []
    for node in ast.walk(main):
        # `for rel in MIGRATIONS: _apply(cur, root, rel)`
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Name)
            and node.iter.id == "MIGRATIONS"
        ):
            migration_lines.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_fleet_schema_guards"
        ):
            guard_lines.append(node.lineno)

    assert migration_lines, "prestart.main 里找不到 MIGRATIONS 循环"
    assert guard_lines, "prestart.main 里找不到 run_fleet_schema_guards 调用"
    assert max(migration_lines) < min(guard_lines), (
        f"守卫(行 {guard_lines})有排在迁移(行 {migration_lines})之前的 · "
        "会对尚未建出的对象误判 → 部署白 halt"
    )


def test_prestart_and_guard_module_never_import_server():
    """禁忌(工单 §2.1 末):ROLE 未设时 `import server` 会跑迁移。"""
    for path in (PRESTART_PATH, GUARD_MODULE_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] != "server" for a in node.names), path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] != "server", path


# ======================= 锁 1 / 锁 2:prestart 行为 =======================

class _FakeCursor:
    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return (1,)

    def close(self):
        pass


class _FakeConnection:
    autocommit = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


def _install_prestart_fakes(monkeypatch, *, failing_guard=None):
    """替掉 prestart.main() 的库/迁移/守卫,只留"清单被真跑"这条链路。

    返回 (prestart 模块, timeline)。timeline 按发生顺序记 ("migration", rel) / ("guard", key),
    所以"守卫跑在迁移之前"这件事是**行为级**可断言的 —— 换个 import 别名也躲不过
    (AST 那条顺序锁按名字匹配,别名就绕过去了;实测变异 M4a 正是这么活下来的)。
    """
    import scripts.prestart as prestart
    from services import startup_schema_guards as guards
    import services.whitelabel_backoffice_schema_contract as whitelabel

    timeline = []

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = lambda *args, **kwargs: _FakeConnection()
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    # main() 里 `import db.diagnosis_db` 是基表 bootstrap · 本锁不测它
    monkeypatch.setitem(sys.modules, "db.diagnosis_db", types.ModuleType("db.diagnosis_db"))
    monkeypatch.setattr(
        prestart, "_apply", lambda cur, root, rel: timeline.append(("migration", rel))
    )
    monkeypatch.setattr(
        whitelabel,
        "assert_whitelabel_backoffice_schema_ready",
        lambda cur, require_settings=False: None,
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://prestart-guard-coverage-lock/fake")

    for guard_key, attr in EXPECTED_FLEET:
        if guard_key == failing_guard:
            def _raise(_key=guard_key):
                timeline.append(("guard", _key))
                raise RuntimeError(f"[SchemaCheck fail-closed] simulated {_key} contract gap")

            monkeypatch.setattr(guards, attr, _raise)
        else:
            monkeypatch.setattr(
                guards, attr, lambda _key=guard_key: timeline.append(("guard", _key))
            )
    return prestart, timeline


def _guard_keys(timeline):
    return [key for kind, key in timeline if kind == "guard"]


def test_prestart_exits_zero_and_logs_every_guard_pass_line(monkeypatch, caplog):
    """锁 2:契约齐全 → 退出 0,四道新补守卫各自有 PASS 行,且**全部守卫都在迁移之后跑**。"""
    prestart, timeline = _install_prestart_fakes(monkeypatch)

    with caplog.at_level(logging.INFO):
        rc = prestart.main()

    assert rc == 0
    assert _guard_keys(timeline) == [key for key, _attr in EXPECTED_FLEET]

    # 🔴 顺序(行为级 · 别名也躲不过):最后一次迁移必须早于第一道守卫
    kinds = [kind for kind, _ in timeline]
    assert "migration" in kinds, "夹具没跑到迁移循环 · 顺序断言会变成空断言"
    assert kinds.index("guard") > len(kinds) - 1 - kinds[::-1].index("migration"), (
        f"有守卫抢在迁移之前跑:{timeline[:8]}"
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    for guard_key, _attr in EXPECTED_FLEET:
        assert f"[prestart] ✅ [FleetSchemaGuard] {guard_key} 契约通过" in messages
    for guard_key in NEWLY_COVERED_BY_PRESTART:
        assert guard_key in messages


@pytest.mark.parametrize("failing_guard", [key for key, _attr in EXPECTED_FLEET])
def test_prestart_exits_nonzero_when_any_single_guard_fails(monkeypatch, failing_guard):
    """锁 1:任一守卫不齐 → 非零退出(deploy halt),且后续守卫不再跑。"""
    prestart, timeline = _install_prestart_fakes(monkeypatch, failing_guard=failing_guard)

    rc = prestart.main()

    assert rc != 0, f"{failing_guard} 契约不齐却零退出 → deploy 会带坏 schema 起候选"
    order = [key for key, _attr in EXPECTED_FLEET]
    # 抛错那道自己会记一笔,之后的都不该跑
    assert _guard_keys(timeline) == order[: order.index(failing_guard) + 1]
