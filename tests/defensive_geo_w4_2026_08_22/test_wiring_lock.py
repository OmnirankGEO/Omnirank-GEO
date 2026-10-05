"""接线锁 —— 证明 attempt 账本**真的被现役链调用**,不是个死函数。

本仓反复记过同一条:新增「要被调用的函数」而没有接线锁 = 死函数,
而"我们接了"这句话在复审里没有任何证明力。所以:

* 调用者集合从**真源码 AST** 机械导出,不手抄;
* 探测器覆盖三种 import 形态,并有活性自证;
* 双向判:多一个调用点(没人复核过)与少一个调用点(接线被摘)**都必须红**。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

from services.defensive_geo.monitoring import legacy_bridge as BRIDGE

ROOT = Path(__file__).resolve().parents[2]

#: 普查面 = 生产代码目录。**不含 tests/** ——
#: 「只有测试在调」正是死函数的标准形态(本仓记过:仅测试调用 = NO-GO)。
_PRODUCTION_DIRS = ("db", "api", "services", "workflows", "agents", "tools")

_BRIDGE_FUNCTION = "capture_before_retry_overwrite"


def _iter_production_py():
    for d in _PRODUCTION_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            yield p


def _calls_bridge(tree: ast.AST) -> bool:
    """三种形态全覆盖:

    * ``from ...legacy_bridge import capture_before_retry_overwrite`` + 裸调用
    * ``from services.defensive_geo.monitoring import legacy_bridge`` + 属性调用
    * ``import services.defensive_geo.monitoring.legacy_bridge as b`` + 属性调用

    🔴 **不**用裸符号名 grep:那样 import 语句、注释、字符串都会命中,
       而"import 了"不等于"调用了"(本仓记过:census 裸符号名 = 把 import 当调用)。
       这里只认 ``ast.Call``。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == _BRIDGE_FUNCTION:
            return True
        if isinstance(func, ast.Attribute) and func.attr == _BRIDGE_FUNCTION:
            return True
    return False


def _production_call_sites() -> set[str]:
    hits: set[str] = set()
    for path in _iter_production_py():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("services/defensive_geo/monitoring/legacy_bridge.py"):
            continue  # 定义处不算调用者
        try:
            tree = ast.parse(io.open(path, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            continue
        if _calls_bridge(tree):
            hits.add(rel)
    return hits


class TestLedgerIsActuallyWired:
    def test_call_sites_are_exactly_the_sanctioned_set(self):
        """双向判:多一个 / 少一个都红。

        拆红(方向 A · 接线被摘):把 ``db/monitoring_db.py`` 里那一跳删掉。
        拆红(方向 B · 多接一处):在任意别的生产文件里加一次调用。
        """
        actual = _production_call_sites()
        expected = set(BRIDGE.EXPECTED_CALL_SITES)
        assert actual == expected, (
            f"账本接线点漂了。多出来:{sorted(actual - expected)};"
            f"少掉的:{sorted(expected - actual)}。"
            "少 = 死函数(写了没人调);多 = 有人在别处接了一跳,没人复核过。"
        )

    def test_the_call_site_is_not_a_test_file(self):
        """「只有测试在调」= 死函数的标准形态。"""
        for site in BRIDGE.EXPECTED_CALL_SITES:
            assert not site.startswith("tests/"), site

    def test_capture_happens_before_the_destructive_update(self):
        """🔴 顺序判据:捕获必须在**那条把 error_code 置 NULL 的 UPDATE 之前**。

        接在后面等于没接 —— 那时错误已经被抹掉了。
        本仓记过同一形态:「门必须在 DELETE 之前」。
        """
        src = io.open(ROOT / "db" / "monitoring_db.py",
                      encoding="utf-8", errors="replace").read()
        capture_at = src.find(_BRIDGE_FUNCTION + "(cur,")
        assert capture_at > 0, "在 monitoring_db.py 里找不到捕获调用"
        destructive_at = src.find("error_code=NULL, error_message=NULL", capture_at)
        assert destructive_at > capture_at, (
            "捕获调用没有排在『error_code=NULL, error_message=NULL』那条 UPDATE 之前 —— "
            "接在后面时错误已经被抹掉,账本记到的是一片空白"
        )

    def test_detector_distinguishes_import_from_call(self):
        """探测器活性自证:光 import 不算调用;三种调用形态都算。

        没有这一条,上面那条"恰等于"既可能是真接对了,
        也可能是探测器把 import 当成了调用。
        """
        only_import = ast.parse(
            "from services.defensive_geo.monitoring.legacy_bridge import "
            "capture_before_retry_overwrite")
        assert not _calls_bridge(only_import), "光 import 被误判成调用"

        for src in (
            "from x import capture_before_retry_overwrite\n"
            "capture_before_retry_overwrite(cur, cell)",
            "import x\nx.capture_before_retry_overwrite(cur, cell)",
            "import x as b\nb.capture_before_retry_overwrite(cur, cell)",
        ):
            assert _calls_bridge(ast.parse(src)), f"漏检调用形态:{src!r}"

    def test_bridge_fails_open_only_for_observation(self):
        """fail-open 是本包唯一一处,且必须**不在**资金/权限路径上。

        判据把这条声明钉住:哪天有人把 fail-open 扩到别处,
        census 里那句 scope 说明会与事实不符,这条会提醒复核。
        """
        c = BRIDGE.census()
        assert c["failOpen"] is True
        assert "funding" not in c["failOpenScope"] or "no funding" in c["failOpenScope"]

    def test_bridge_uses_a_separate_identity_domain(self):
        """现役桥拿不到 provider/model 逐值 ⇒ **不冒充** §6.1 全字段身份。

        硬塞占位串会得到一个"看起来符合 §6.1 但其实是编的"身份 ——
        比诚实地另起一个域更糟。
        """
        a = BRIDGE._legacy_attempt_id("pc1", 1)
        b = BRIDGE._legacy_attempt_id("pc1", 2)
        assert a != b and len(a) == 64

        from services.defensive_geo.monitoring import lineage as LIN

        v2 = LIN.attempt_id(
            plan_cell_id="pc1", attempt_ordinal=1, actual_provider="p",
            actual_model="m", actual_model_revision=None, actual_surface="s",
            actual_search_mode="sm", request_hash="rh")
        assert a != v2, "现役桥的身份与 v2 全字段身份撞了 —— 两者语义不同,不该相等"


class TestLazyImportIsDeadlockSafe:
    """🔴 2026-08-10 生产自死锁教训的成对判据。

    那次是「在 DB 读事务内惰性 import 一个会触发 ``db.diagnosis_db`` ``init_db``
    的模块」—— init_db 去抢 ACCESS EXCLUSIVE,把生产打成 503 十六分钟。

    本包的接线**正是**一次事务内惰性 import。所以必须证明它的传递闭包
    碰不到任何 ``db.*``,而不是"我看了一眼觉得没事"。
    """

    @staticmethod
    def _closure(entry: str) -> tuple[set[str], set[str]]:
        seen: set[str] = set()
        db_edges: set[str] = set()
        stack = [entry]
        while stack:
            f = stack.pop()
            if f in seen:
                continue
            seen.add(f)
            try:
                tree = ast.parse(io.open(ROOT / f, encoding="utf-8").read())
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    # ``from pkg import mod`` 里 mod 也是个模块 —— 只看 node.module
                    # 会漏掉整条边(第一版实测闭包只有 1 个文件)。
                    mods = [node.module] + [
                        f"{node.module}.{a.name}" for a in node.names]
                for m in mods:
                    if m.split(".")[0] == "db":
                        db_edges.add(m)
                    rel = m.replace(".", "/") + ".py"
                    if (ROOT / rel).exists():
                        stack.append(rel)
        return seen, db_edges

    def test_bridge_import_closure_touches_no_db_module(self):
        closure, db_edges = self._closure(
            "services/defensive_geo/monitoring/legacy_bridge.py")
        assert not db_edges, (
            f"接线模块的传递闭包引到了 {sorted(db_edges)} —— "
            "事务内惰性 import 一个会触发 init_db 的模块 = 2026-08-10 那次自死锁"
        )
        assert len(closure) >= 3, (
            f"闭包只有 {len(closure)} 个文件 —— 探测器多半没跟进去,"
            "这时「零 db 边」是假绿")

    def test_closure_detector_can_see_a_db_edge(self):
        """探测器活性自证:换一个**真的**引了 db 的入口,必须报出来。

        没有这一条,上面那条"零 db 边"既可能是真没有,
        也可能是闭包压根没走进去。
        """
        _, db_edges = self._closure("db/monitoring_db.py")
        assert db_edges, "探测器在一个真引 db 的文件上也报不出 db 边 —— 它是瞎的"


class TestV2MonitoringHasNoExecutor:
    """§15.9.3 逐字禁止「为 v2 新建平行**监测执行器**」。

    「没建执行器」这句话必须可被机械检验,否则下一个人加一个
    `freeze_points(...)` 进去也没人会红。
    """

    #: 执行器的可判定特征 = 调用了这些**副作用原语**中的任何一个。
    #: 分母从现役资金/任务原语机械列出,不是"我觉得危险的词"。
    _EXECUTOR_SINKS = (
        "freeze_points", "commit_freeze", "release_freeze",
        "materialize_publish_batch", "create_monitoring_task",
        "create_monitoring_run_cells", "claim_monitoring_run_cell",
        "reserve_charge", "claim_live_charge",
    )

    _V2_MODULES = (
        "api/defensive_monitoring_api.py",
        "services/defensive_geo/monitoring/v2_service.py",
    )

    def _calls(self, rel: str) -> set[str]:
        tree = ast.parse(io.open(ROOT / rel, encoding="utf-8").read())
        out: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in self._EXECUTOR_SINKS:
                out.add(name)
        return out

    @pytest.mark.parametrize("rel", _V2_MODULES)
    def test_no_route_here_creates_a_task_or_freeze(self, rel):
        """拆红:在任一 v2 模块里加一句 ``freeze_points(...)`` —— 本判据立刻红。"""
        hits = self._calls(rel)
        assert not hits, (
            f"{rel} 调用了执行器原语 {sorted(hits)} —— §15.9.3 禁止 v2 新建"
            "平行监测执行器。真正的执行必须走现役 POST /api/monitoring/run。"
        )

    def test_executor_sink_detector_is_alive(self):
        """探测器活性自证:真调用必须被抓到、纯 import 必须不被抓到。"""
        assert "freeze_points" in {
            n.func.id for n in ast.walk(ast.parse("freeze_points(1)"))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        # 负样本:只 import 不调用,不该算执行器
        tree = ast.parse("from middleware.billing import freeze_points")
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Call)]

    def test_declared_routes_match_the_router(self):
        """路由分母从 router 对象机械导出,不手抄。"""
        from api.defensive_monitoring_api import route_census

        paths = {r["path"] for r in route_census()}
        assert paths == {
            "/api/defensive-geo/monitoring/run-admission",
            "/api/defensive-geo/monitoring/runs/{task_id}/progress",
            "/api/defensive-geo/monitoring/reports/{report_snapshot_id}/comparability",
        }, sorted(paths)
