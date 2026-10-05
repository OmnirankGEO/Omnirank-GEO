# -*- coding: utf-8 -*-
"""gate9 全分母 runner 的**传输面参数化**合同判据(工单 V5-B B-6 前置 · Review 照准)。

═══ 为什么动这两行 ═══

`gate9_full_denominator_baseline.py` 把传输面写死了两处:
``BASE = "postgresql://geo_admin:testpw@localhost:55488"`` 与
``docker logs gate9-pg``。目标 Python 3.12 的容器 harness 里这两样都不对,
所以 Codex 上一轮是**打补丁跑的归档副本**
(`15_final_transport_shim.diff`,并把 patched 脚本的 sha256 记进证据)。

**证据链里多一份手工 patch 的副本,就多一处会漂的实现。**
改成 env 参数之后,容器轮与本机轮跑的是**同一个文件**,不需要 shim、
不需要在证据里钉一个 patched sha。

═══ 参数化把爆炸半径放大了,所以同笔加栓 ═══

`_assert_latch_armed` 里 ADMIN DSN 那条栓是 ``dsn.startswith(BASE + "/")``
—— 它**跟着 BASE 走**,所以 BASE 一旦可配,"我自己的容器"这句话就由环境说了算。
加三条停机栓,并且每条都有正样本。

🔴 **其中「5432」那条,我第一版想写成"含 :5432 就停",那是错的** ——
Codex 自己的最终 shim 里,容器内 DSN 就是
``postgresql://geo_admin:testpw@codex-fixoffix2-den-pg312-20260828-b79844d:5432``:
内部网里 PG 就听 5432。一刀切会把**我要用的那个 harness** 一起挡掉。
真正危险的是**宿主上暴露的那个 5432**,所以栓的条件是
「host 是 localhost / 127.0.0.1 / ::1 / host.docker.internal」**且**「有效端口 5432」。
(又一次「规则的适用域 < 使用域」—— 这次是我自己提的规则,自己差点写宽。)

跑法::

    python -m pytest scripts/test_mutation_gate9_transport_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
G9 = ROOT / "scripts" / "gate9_full_denominator_baseline.py"

#: 现值 —— 判据钉住「不设 env 时逐字节不变」,所以这里写死字面量。
#: 它变了这条判据就该红:那是一次**行为改变**,必须过一次人。
CURRENT_BASE = "postgresql://geo_admin:testpw@localhost:55488"
CURRENT_PG_CONTAINER = "gate9-pg"

_SRC = G9.read_text(encoding="utf-8-sig")
_TREE = ast.parse(_SRC)


def _gate9_env_names() -> tuple[str, ...]:
    """runner 认的 GATE9_* 环境变量 —— **AST 机械枚举**,不手写。

    🔴 这里第一版是手写的两个名字。加第三个(`GATE9_PG_LOG_FILE`)时忘了同步,
       于是 `_load()` 不再清它:一个测试留下的 tmp 路径漏给了后面的测试,
       后面那些 `_load()` 在"未设 env"的名义下拿到别人的配置,
       报出来的红跟被测代码毫无关系。
       「手写分母漏掉的那一项不会让任何判据变红」—— 这次它长在判据自己的夹具上。
    """
    names = set()
    for n in ast.walk(_TREE):
        if (isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get"
                and n.args and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)
                and n.args[0].value.startswith("GATE9_")):
            names.add(n.args[0].value)
    assert names, "一个 GATE9_* 都没扫到 —— 探针写废了(分母为 0 不是通过)"
    return tuple(sorted(names))


_G9_ENVS = _gate9_env_names()


def _load(env: dict | None = None):
    """按给定 env 重新加载 gate9 模块 —— 传输面是**模块级**解析的,
    所以"env 变了会怎样"只能靠重新加载来问。"""
    saved = {k: os.environ.get(k) for k in _G9_ENVS}
    try:
        for k in _G9_ENVS:
            os.environ.pop(k, None)
        for k, v in (env or {}).items():
            os.environ[k] = v
        name = "g9_probe_" + str(abs(hash(tuple(sorted((env or {}).items())))))
        spec = importlib.util.spec_from_file_location(name, G9)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        sys.path.insert(0, str(G9.parent))
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ══ ① 不设 env ⇒ 行为逐字节不变 ═════════════════════════════════════════
def test_t00_the_env_denominator_is_enumerated_from_the_runner_not_hand_written():
    """🔴 夹具自己的分母锁:``_load()`` 清哪些 env 必须由 runner 源码机械得出。

    手写清单漏一个 ⇒ 上一个测试设的值漏给下一个 ⇒ 后面那些「未设 env」的断言
    其实跑在别人的配置上。我加第三个变量时就是这么漏的。
    """
    assert set(_G9_ENVS) >= {"GATE9_BASE_DSN", "GATE9_PG_CONTAINER",
                             "GATE9_PG_LOG_FILE"}, _G9_ENVS
    for k in _G9_ENVS:
        assert k in _SRC, f"{k} 不在 runner 源码里 —— 枚举器抓错了"


def test_t01_unset_env_resolves_to_the_current_literals():
    """🔴 Review 的条件逐字:不设时**行为逐字节不变**。

    参数化最容易偷偷改掉的就是默认值 —— 而默认值一变,
    「本机轮」和「上一轮本机轮」就不是同一件事了,却没有任何东西会红。
    """
    m = _load()
    assert m.BASE == CURRENT_BASE, f"默认 BASE 变了:{m.BASE!r}"
    assert m.PG_CONTAINER == CURRENT_PG_CONTAINER, f"默认容器名变了:{m.PG_CONTAINER!r}"


def test_t02_an_empty_env_var_is_treated_as_unset():
    """空串不许当成"配了一个空 DSN" —— 那会静默拼出 `/postgres` 这种鬼东西。"""
    m = _load({"GATE9_BASE_DSN": "", "GATE9_PG_CONTAINER": "   "})
    assert m.BASE == CURRENT_BASE
    assert m.PG_CONTAINER == CURRENT_PG_CONTAINER


def test_t03_every_plan_dsn_is_built_on_the_resolved_base():
    """15 包的 DSN 计划表必须整张跟着 BASE 走,不许有漏网的写死项。"""
    m = _load()
    bad = [f"{t}::{k}={v}" for t, plan in m.ENV_PLAN.items()
           for k, v in plan.items() if not v.startswith(m.BASE + "/")]
    assert not bad, f"这些 DSN 没挂在解析后的 BASE 上:{bad}"


def test_t04_the_module_no_longer_hardcodes_the_transport_in_two_places():
    """结构锁:``BASE`` / ``PG_CONTAINER`` 必须由 resolver 产出,
    且 docker logs 的容器名不许再是字面量。"""
    assigns = {}
    for n in _TREE.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                isinstance(n.targets[0], ast.Name):
            assigns[n.targets[0].id] = n.value
    for name in ("BASE", "PG_CONTAINER"):
        v = assigns.get(name)
        assert v is not None, f"模块级没有 {name}"
        assert isinstance(v, ast.Call), f"{name} 还是字面量赋值 —— 没参数化"
    # docker logs 那一处不许再出现裸的容器名字面量
    liveness = next(n for n in ast.walk(_TREE)
                    if isinstance(n, ast.FunctionDef) and n.name == "liveness")
    lits = [n.value for n in ast.walk(liveness)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert CURRENT_PG_CONTAINER not in lits, (
        f"liveness 里还写死着 {CURRENT_PG_CONTAINER!r} —— 容器轮又得打补丁")


# ══ ② 三条停机栓,各配正样本 ═════════════════════════════════════════════
@pytest.mark.parametrize("dsn,why", [
    ("postgresql://geo_admin:pw@localhost:55488/geo_agentscope", "生产库名"),
    ("postgresql://geo_admin:pw@omnirank-db:55488", "生产容器名"),
    ("postgresql://geo_admin:pw@localhost:5432", "宿主上暴露的生产端口"),
    ("postgresql://geo_admin:pw@127.0.0.1:5432", "同上,IP 形态"),
    ("postgresql://geo_admin:pw@localhost", "没写端口 ⇒ PG 默认就是 5432"),
    ("postgresql://geo_admin:pw@host.docker.internal:5432", "容器里回指宿主的 5432"),
])
def test_t10_the_production_latches_stop_the_run(dsn, why):
    """🔴 正样本 —— 点名规则:BASE 指向生产特征,当场停机。

    BASE 可配之后 ADMIN 栓那句"必须是我自己的容器"就由环境说了算,
    没有这三条,一个打错的 env 能让整轮判据连上生产。
    """
    m = _load()
    with pytest.raises(SystemExit) as exc:
        m.assert_transport_is_not_production(dsn)
    assert why or True
    assert "生产" in str(exc.value)


@pytest.mark.parametrize("dsn", [
    CURRENT_BASE,
    "postgresql://geo_admin:testpw@localhost:55589",
    # 🔴 这一条是**适用域**的守卫:容器内部网里 PG 就听 5432,
    #    Codex 最终那轮的 DSN 逐字就是这个形状。一刀切"含 5432 就停"
    #    会把我要用的 harness 挡在门外。
    "postgresql://geo_admin:testpw@codex-fixoffix2-den-pg312-20260828-b79844d:5432",
    "postgresql://geo_admin:testpw@gate9-harness-pg:5432",
])
def test_t11_a_legitimate_throwaway_transport_is_allowed(dsn):
    m = _load()
    m.assert_transport_is_not_production(dsn)        # 不抛即通过


def test_t12_a_production_base_in_the_env_stops_at_import():
    """行为向:把生产 DSN 塞进 env,模块加载时就该炸,不能等到跑起来。"""
    with pytest.raises(SystemExit):
        _load({"GATE9_BASE_DSN": "postgresql://u:p@omnirank-db:5432/geo_agentscope"})


# ══ ③ ADMIN 栓跟着 BASE 走 ══════════════════════════════════════════════
HARNESS = "postgresql://geo_admin:testpw@gate9-harness-pg:5432"


def test_t20_the_admin_latch_judges_by_the_new_base():
    """🔴 Review 点名的那条:换一个 BASE 之后,ADMIN 栓按**新** BASE 判。

    正向:整张计划表挂到新 BASE 上,栓放行。
    """
    m = _load({"GATE9_BASE_DSN": HARNESS})
    assert m.BASE == HARNESS
    admin = [(t, k, v) for t, plan in m.ENV_PLAN.items()
             for k, v in plan.items() if k in m.ADMIN_DSN_ENVS]
    assert admin, "一条 ADMIN DSN 都没扫到 —— 判据在空分母上恒绿"
    for t, k, v in admin:
        assert v.startswith(HARNESS + "/"), f"{t}::{k} 没跟着新 BASE 走:{v}"
    m._assert_latch_armed()                          # 不抛即通过


def test_t21_an_admin_dsn_left_on_the_old_base_is_refused_under_the_new_base():
    """反向对照 —— 没有它,上一条只证明"栓没红",证不了"栓还活着"。

    把一条 ADMIN DSN 按住在**旧** BASE 上,在新 BASE 下必须被拒。
    """
    m = _load({"GATE9_BASE_DSN": HARNESS})
    tgt, key = next((t, k) for t, plan in m.ENV_PLAN.items()
                    for k in plan if k in m.ADMIN_DSN_ENVS)
    m.ENV_PLAN[tgt][key] = CURRENT_BASE + "/postgres"
    with pytest.raises(SystemExit) as exc:
        m._assert_latch_armed()
    assert "不在我的容器上" in str(exc.value)


def test_t22_the_admin_latch_still_refuses_a_non_postgres_admin_db():
    """另一半没被参数化冲掉:ADMIN 入口只许连 ``postgres`` 库。"""
    m = _load({"GATE9_BASE_DSN": HARNESS})
    tgt, key = next((t, k) for t, plan in m.ENV_PLAN.items()
                    for k in plan if k in m.ADMIN_DSN_ENVS)
    m.ENV_PLAN[tgt][key] = HARNESS + "/some_other_test"
    with pytest.raises(SystemExit) as exc:
        m._assert_latch_armed()
    assert "只许连 postgres" in str(exc.value)


# ══ ④ 解析后的传输面必须进证据 ═══════════════════════════════════════════
def test_t30_the_resolved_transport_is_printed(capsys):
    """启动要把**解析后**的 BASE 与容器名打出来 ——
    证据里只有"跑过了"而没有"跑在哪个库上",等于没有传输面这一栏。"""
    m = _load({"GATE9_BASE_DSN": HARNESS, "GATE9_PG_CONTAINER": "harness-pg"})
    capsys.readouterr()
    m.print_transport_identity({"tip": "a" * 40, "tree": "b" * 40, "image_id": None,
                                 "started_at": "2026-08-30T00:00:00Z"})
    out = capsys.readouterr().out
    assert HARNESS in out and "harness-pg" in out
    assert "GATE9_BASE_DSN" in out, "没写明它是从哪个 env 来的(默认值 vs 配置值要能分辨)"


def test_t31_the_entrypoint_prints_it_before_the_latch_check():
    """结构锁:先把传输面打进证据,再进安全栓 ——
    栓拦下来的那一轮,证据里也要有"它当时指向哪儿"。"""
    fn = next(n for n in ast.walk(_TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "_main_locked")

    def idx(callee):
        for i, stmt in enumerate(fn.body):
            for n in ast.walk(stmt):
                if isinstance(n, ast.Call) and (
                        getattr(n.func, "attr", None)
                        or getattr(n.func, "id", None)) == callee:
                    return i
        return -1

    p, latch = idx("print_transport_identity"), idx("_assert_latch_armed")
    assert p >= 0, "_main_locked 没打印传输面"
    assert latch >= 0, "找不到安全栓调用 —— 判据坐标烂了"
    assert p < latch, f"打印在第 {p} 条、安全栓在第 {latch} 条 —— 打印要排在前面"


# ══ ⑤ DDL 活性日志的来源与**时效边界** ═══════════════════════════════════
#
# 起因(2026-08-28 待命期核 shim 时挖到,Review 照准修):
# 容器 harness 给 `docker` 装的 shim 全文只有两行 ——
#     #!/bin/sh
#     cat /pglogs/postgresql-round2.log
# 它**不看任何参数**:既不看容器名,也不看 `--since`。而 runner 那一行紧挨着的
# 注释逐字写着「`--since <本轮开跑时刻>` 把证据钉回这一轮 —— 没有它,上一轮的
# CREATE DATABASE 行永远留在日志里 ⇒ 这条臂**第二轮起永久为真**」。
#
# 也就是说前手专门加的那道边界,在容器轮里是**关着的**,而 runner 无从知道。
# 那一轮结论仍成立,靠的是「PG 容器当轮现建、日志里本来只有这一轮」——
# **布置的巧合,不是机制**。替身少接一个参数不会报错、不会缺字段、不会改格式,
# 四个信号全部正常。所以边界搬回被测方自己手里:`GATE9_PG_LOG_FILE` + Python 侧过滤。

#: 从 gate9-pg 真机取的一行(`log_line_prefix = '%m [%p] '`,`log_timezone = Etc/UTC`)。
#: 🔴 判据必须打**真实日志行**,不能只测 ISO 字符串 —— 时间戳前缀长什么样
#:    是由 PG 配置决定的,自己编一个格式等于在验自己的想象。
REAL_LOG_LINE = ('2026-08-28 20:22:18.088 UTC [218030] LOG:  statement: '
                 'CREATE DATABASE "geo_defgeo_e3_cold_test" '
                 'TEMPLATE "geo_defgeo_e3_tpl_test"')


def test_t40_unset_keeps_the_docker_logs_path_byte_for_byte():
    m = _load()
    assert m.PG_LOG_FILE is None
    assert m.transport_log_source() == f"docker:{CURRENT_PG_CONTAINER}"


def test_t41_a_mounted_log_file_becomes_the_source(tmp_path):
    f = tmp_path / "postgresql.log"
    f.write_text(REAL_LOG_LINE + "\n", encoding="utf-8")
    m = _load({"GATE9_PG_LOG_FILE": str(f)})
    assert m.PG_LOG_FILE == f
    assert m.transport_log_source() == f"file:{f}"


def test_t42_a_missing_log_file_stops_the_run(tmp_path):
    """🔴 正样本:文件不在 ⇒ 停机。**不许静默当成「零 DDL 行」** ——
    零 DDL 行正是"这个包没跑在自己库上"的观测形态,两者不能同形。"""
    with pytest.raises(SystemExit) as exc:
        _load({"GATE9_PG_LOG_FILE": str(tmp_path / "nope.log")})
    assert "GATE9_PG_LOG_FILE" in str(exc.value)


def test_t43_the_real_pg_log_line_parses():
    """拿真机那一行打解析器 —— 前缀格式由 PG 的 log_line_prefix 决定,不能自己编。"""
    import datetime as dt

    m = _load()
    got = m.parse_pg_log_ts(REAL_LOG_LINE)
    assert got == dt.datetime(2026, 8, 28, 20, 22, 18, tzinfo=dt.timezone.utc), got


def test_t44_a_previous_round_line_is_filtered_out():
    """🔴 正样本 —— 就是 shim 吞掉 `--since` 之后失守的那一格:
    上一轮的 CREATE DATABASE 行**不许**被本轮采信。"""
    m = _load()
    old = ('2026-08-27 10:00:00.000 UTC [1] LOG:  statement: '
           'CREATE DATABASE "stale_from_last_round_test"')
    out = m.filter_log_since(old + "\n" + REAL_LOG_LINE + "\n",
                             "2026-08-28T20:00:00Z")
    assert "stale_from_last_round" not in out
    assert "geo_defgeo_e3_cold_test" in out


def test_t45_a_line_from_this_round_is_kept():
    """反向对照 —— 只有 t44 的话,一个「全都过滤掉」的坏过滤器同样能过。"""
    m = _load()
    out = m.filter_log_since(REAL_LOG_LINE + "\n", "2026-08-28T20:00:00Z")
    assert "geo_defgeo_e3_cold_test" in out


def test_t46_a_continuation_line_inherits_the_previous_timestamp():
    """多行语句的续行没有时间戳前缀 —— 不许因此被单独丢掉或单独留下。"""
    m = _load()
    text = REAL_LOG_LINE + "\n\tAND something_else\n"
    out = m.filter_log_since(text, "2026-08-28T20:00:00Z")
    assert "AND something_else" in out
    old = ('2026-08-27 10:00:00.000 UTC [1] LOG:  statement: CREATE DATABASE "x_test"\n'
           '\tCONTINUED_OLD\n')
    assert "CONTINUED_OLD" not in m.filter_log_since(old, "2026-08-28T20:00:00Z")


def test_t47_a_non_utc_log_timezone_stops_instead_of_guessing():
    """🔴 时区缩写是有歧义的 —— 猜一个等于**悄悄挪动边界**。停机,让人去把
    log_timezone 设成 UTC,而不是让过滤器自己发挥。"""
    m = _load()
    with pytest.raises(SystemExit) as exc:
        m.parse_pg_log_ts('2026-08-28 20:22:18.088 CST [1] LOG:  statement: x')
    assert "时区" in str(exc.value)


def test_t48_no_since_means_no_filtering():
    m = _load()
    text = REAL_LOG_LINE + "\n"
    assert m.filter_log_since(text, None) == text


def test_t49_liveness_actually_routes_through_the_file_and_the_filter():
    """结构锁:分母是**调用点** —— 定义了过滤器不等于 liveness 用了它。"""
    fn = next(n for n in ast.walk(_TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "liveness")
    called = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
              for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "filter_log_since" in called, "liveness 没走时效过滤 —— 边界又回到 shim 手里了"
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "PG_LOG_FILE" in names, "liveness 没看日志文件来源"


def test_t50_the_printout_names_the_real_log_source(capsys, tmp_path):
    """证据里必须写出日志**真实**来自哪儿 —— 只印「PG 容器=X」会让人
    以为日志来自容器 X,而它其实来自一个挂载文件。"""
    f = tmp_path / "pg.log"
    f.write_text(REAL_LOG_LINE + "\n", encoding="utf-8")
    m = _load({"GATE9_PG_LOG_FILE": str(f)})
    capsys.readouterr()
    m.print_transport_identity({"tip": "a" * 40, "tree": "b" * 40, "image_id": None,
                                 "started_at": "2026-08-30T00:00:00Z"})
    out = capsys.readouterr().out
    assert f"file:{f}" in out
    m2 = _load()
    capsys.readouterr()
    m2.print_transport_identity({"tip": "a" * 40, "tree": "b" * 40, "image_id": None,
                                 "started_at": "2026-08-30T00:00:00Z"})
    assert f"docker:{CURRENT_PG_CONTAINER}" in capsys.readouterr().out


def test_t60_every_call_site_of_the_printer_passes_the_identity():
    """🔴 分母 = **调用点**(AST),含判据文件自己。

    [2026-08-30] 我把 `print_transport_identity()` 改成收 `identity` 形参,
    生产侧改了,**判据文件里的两个调用点忘了改** ⇒ t30/t50 当场 TypeError。
    与 `_run_targets` 3→4 元组那次同一课:**改签名要做调用点普查**,
    而普查的分母**包括判据自己**。
    """
    import ast as _ast

    bad = []
    for f in sorted((ROOT / "scripts").glob("*.py")):
        try:
            tree = _ast.parse(f.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for n in _ast.walk(tree):
            if (isinstance(n, _ast.Call)
                    and (getattr(n.func, "attr", None)
                         or getattr(n.func, "id", None)) == "print_transport_identity"
                    and not n.args and not n.keywords):
                bad.append(f"{f.name}:{n.lineno}")
    assert not bad, f"这些调用点没传 identity:{bad}"
