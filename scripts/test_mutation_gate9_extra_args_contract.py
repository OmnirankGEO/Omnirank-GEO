# -*- coding: utf-8 -*-
"""逐包额外 pytest 参数 + gate8 收编(18 包)的合同判据(V7-C)。

**为什么这个机制必须自己带判据**(Review 令 2026-08-30):
「在字典里写个字符串」和「那个字符串真的进了跑起来的那条命令」是两件事。
参数没传到时,`tests/defgeo_gate8_2026_08_30` 会因祖先 `tests/conftest.py`
在 import 期 `RuntimeError: TEST_DATABASE_URL 未配置` 而**整包 collection error** ——
而那种红长得像「这个包坏了」,不像「我的机制没接线」。

三种组合的**实测**(不是推理),记在这里备查:

======================================  ==========================
组合                                    结果
======================================  ==========================
不带 confcutdir + 不给 TEST_DATABASE_URL  **rc=4**,collection error
不带 confcutdir + 补 TEST_DATABASE_URL    4 passed
带 confcutdir                            4 passed
======================================  ==========================

选第三种不选第二种:第二种要在 `ENV_PLAN` 里声明一把**没人会创建**的库
—— 往计划里写假话,而且让这个包同时挂 DSN 与铸库前缀、活性归属含糊;
它的绿还依赖 `_bind_app_db`(session autouse)恰好在任何 app 模块 import 之前
硬赋值 `DATABASE_URL` 这一条顺序,那是「碰巧绿」。

跑法::

    python -m pytest scripts/test_mutation_gate9_extra_args_contract.py -q
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import contextlib
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
G9 = ROOT / "scripts" / "gate9_full_denominator_baseline.py"
PKG = "tests/defgeo_gate8_2026_08_30"

#: 🔴 本判据**冻结**的包数。它是手写的,而且必须手写 ——
#:    登记一个新判据包应当是一次**刻意的决定**,
#:    不是"跑个脚本把数字改了"(见 gate9 名单注释)。
#:    但手写点只准有**这一个**:断言与两条诊断报文都读它。
#:    2026-09-10 实测过反例 —— 报文里另写着 124,而断言早已是 134,差了 10。
#:    失败时报文是唯一被读的东西,一个具体但错的目标数
#:    会把复核的人引向不存在的原因。
#:    ⚠️ 不要改成读 `_G.DISK_PACKAGES_COUNT`:那样"期望"永远等于"常量",
#:    而这两个数不一致**正是**这条报文要报的东西。
_EXPECTED_PACKAGES = 136

_spec = importlib.util.spec_from_file_location("eargs_g9", G9)
_G = importlib.util.module_from_spec(_spec)
sys.modules["eargs_g9"] = _G
sys.path.insert(0, str(G9.parent))
with contextlib.redirect_stdout(io.StringIO()):
    _spec.loader.exec_module(_G)


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(G9.read_text(encoding="utf-8-sig"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


# ══ 纯样本:argv 里到底有没有那个参数 ═══════════════════════════════════
def test_ea_01_the_extra_arg_is_in_both_argvs():
    """收编的包:**collect 与真跑两条命令**都要带上它。

    只验一条就漏另一条 —— 而 collect 那条负责「实收几条」,真跑那条负责红集,
    任何一条掉了都会以「这个包坏了」的形状出现。
    """
    want = f"--confcutdir={PKG}"
    for collect in (True, False):
        argv = _G.pytest_argv(PKG, collect=collect, junit="/tmp/x.xml")
        assert want in argv, f"collect={collect} 的 argv 里没有 {want}:{argv}"


def test_ea_02_a_package_without_extras_gets_none():
    """阴性对照:没登记的包不许被顺手加参数,否则上面那条证明不了是登记生效。"""
    argv = _G.pytest_argv("tests/p03_settlement_2026_08_24", collect=False,
                          junit="/tmp/x.xml")
    assert not [a for a in argv if a.startswith("--confcutdir")], argv


def test_ea_03_junit_only_on_the_real_run():
    a_c = _G.pytest_argv(PKG, collect=True)
    a_r = _G.pytest_argv(PKG, collect=False, junit="/tmp/j.xml")
    assert not [x for x in a_c if x.startswith("--junitxml")], a_c
    assert "--junitxml=/tmp/j.xml" in a_r, a_r
    assert "--collect-only" in a_c and "--collect-only" not in a_r


def test_ea_04_extras_are_injectable_and_drive_the_output():
    """纯函数正样本:换一份 extra 表,输出必须跟着变(证明它真的读了那张表)。"""
    argv = _G.pytest_argv("tests/whatever", collect=True,
                          extra={"tests/whatever": ("--zzz",)})
    assert "--zzz" in argv, argv
    assert "--zzz" not in _G.pytest_argv("tests/whatever", collect=True, extra={})


# ══ 🔴 行为臂:参数没传到 ⇒ 必红(真起 pytest 子进程,不需要库)═══════════
def _collect_only(extra_args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 🔴 判据前提:负臂靠「祖先 conftest 缺 TEST_DATABASE_URL 就炸」。
    #    环境里若飘着这个值,负臂会变绿 ⇒ 这条判据零判别力。显式清掉。
    env.pop("TEST_DATABASE_URL", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "--collect-only", "-q",
         "-p", "no:warnings", "-p", "no:cacheprovider", "--no-header", *extra_args],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")


def test_ea_05_precondition_no_dotenv_supplies_the_var():
    """前提自证:仓根不许有 `.env` —— 它会通过 `load_dotenv` 喂进 TEST_DATABASE_URL,
    把下面那条负臂变成恒绿。"""
    assert not (ROOT / ".env").exists(), (
        "仓根有 .env,`tests/conftest.py` 会 load_dotenv 读到 TEST_DATABASE_URL "
        "⇒ 负臂失去判别力。这条判据在这种树上不作数。")


def test_ea_06_without_the_extra_arg_the_package_really_dies():
    """🔴 负臂 —— **参数没传到就是这个下场**,它必须真的红。

    这条不是形式:它证明 `--confcutdir` 不是可有可无的装饰,
    从而让上面那些「argv 里有它」的断言真的有意义。
    """
    r = _collect_only([])
    assert r.returncode != 0, "不带 confcutdir 竟然过了 —— 负臂失效,整组判据作废"
    assert "TEST_DATABASE_URL" in (r.stdout + r.stderr), (r.stdout + r.stderr)[-500:]


def test_ea_07_with_the_argv_the_machine_builds_it_passes():
    """🔴 正臂 —— 用 `pytest_argv` **真产出的那串参数**跑,必须绿且收到 4 条。

    注意用的是机器产出的 argv 尾巴,不是我手写的字符串:
    手写等于判据自己构造中间值,机制改了判据也不会红。
    """
    tail = _G.pytest_argv(PKG, collect=True)[4:]
    extra = [a for a in tail if a.startswith("--confcutdir")]
    assert extra, "机器没产出 confcutdir,正臂无从谈起"
    r = _collect_only(extra)
    assert r.returncode == 0, (r.stdout + r.stderr)[-500:]
    # 🔴 [V9-B] 原来写死「4 tests collected」—— A 往 gate8 包加了三个判据文件之后
    #    收集数变 29,这条判据当场假红。**计数式判据会过期**(本仓早记过这条,
    #    今天长在我自己的判据上)。这条要回答的是「带上参数之后收集**成功**」,
    #    不是「收集了几条」;判别力由负臂(不带参数 ⇒ 必红)提供,不由数字提供。
    mm = re.search(r"(\d+) tests? collected", r.stdout)
    assert mm, r.stdout[-300:]
    assert int(mm.group(1)) >= 1, f"收集数为 0:{r.stdout[-300:]}"


# ══ 数据流锁:两处调用点都必须走 pytest_argv ═════════════════════════════
def test_ea_08_run_target_builds_no_pytest_argv_by_hand():
    """🔴 抓的毒:「只给 collect 那条加参数,真跑那条还是手拼」。

    结构上不许 `run_target` 里再出现手拼的 pytest 命令列表;
    两次 `_sh(...)` 的第一个实参都必须是 `pytest_argv(...)` 的调用。
    """
    fn = _fn("run_target")
    shs = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Name) and n.func.id == "_sh"]
    assert len(shs) == 2, f"run_target 里 _sh 调用 {len(shs)} 次(应 2 次)"
    for c in shs:
        assert c.args, "_sh 没有位置实参"
        a = c.args[0]
        assert isinstance(a, ast.Call) and isinstance(a.func, ast.Name) \
            and a.func.id == "pytest_argv", \
            f"第 {c.lineno} 行的 _sh 不是拿 pytest_argv 的结果跑的:{ast.dump(a)[:100]}"
    # 手拼残留:函数体里不许再出现 "-m" / "pytest" 这对字面量
    lits = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not ("-m" in lits and "pytest" in lits), \
        "run_target 里还留着手拼的 pytest 命令片段"


def test_ea_09_every_extra_key_is_in_the_denominator():
    """登记表不许有孤儿键:给一个分母外的包配参数 = 那份配置永远不会生效。"""
    orphan = sorted(set(_G.EXTRA_PYTEST_ARGS) - set(_G.GATE9_DENOMINATOR))
    assert not orphan, f"EXTRA_PYTEST_ARGS 里有分母外的包:{orphan}"


# ══ 收编(18 包)本身的判据 ═══════════════════════════════════════════════
def test_ea_10_the_denominator_is_twentythree_and_gate8_is_in_it():
    # 🔴 18 -> 19 -> 21 -> 23。这个字面量**就是要人刻意来改的**;
    #    但它三次没被改到,因为 `pytest.ini: testpaths = tests` 从不收集 `scripts/` ——
    #    防分母漂移的锁自己漂了三次没人知道(见本笔 commit message)。
    assert _G.GATE9_DENOMINATOR_SIZE == 23
    assert len(_G.GATE9_DENOMINATOR) == 23
    assert PKG in _G.GATE9_DENOMINATOR
    assert PKG in _G.ENV_PLAN


def test_ea_11_gate8_is_minting_not_nodb_and_its_dsn_is_admin():
    """归属:它 `CREATE DATABASE … TEMPLATE` 建 `defgeo_gate8_<tag>_<hex>_test`
    ⇒ MINTING;它的 DSN 连 `/postgres` ⇒ ADMIN。两者都不许含糊。"""
    assert _G.MINTING_DB.get(PKG) == "defgeo_gate8_", _G.MINTING_DB.get(PKG)
    assert PKG not in _G.NO_DB_PACKAGES, "真碰库的包塞进 NO_DB = 让活性臂闭嘴"
    assert "DEFGEO_GATE8_TEST_DSN" in _G.ADMIN_DSN_ENVS
    plan = _G.ENV_PLAN[PKG]
    assert set(plan) == {"DEFGEO_GATE8_TEST_DSN"}, (
        f"计划里多了别的变量:{sorted(plan)} —— 尤其不许有 TEST_DATABASE_URL,"
        "那把库没人会创建,写进去就是一句假话")
    assert plan["DEFGEO_GATE8_TEST_DSN"].endswith("/postgres")


def _census_diag(root, names) -> str:
    """普查失败时说出**它看到了什么** —— 只在断言为假时被求值(assert 的 message 是惰性的)。

    🔴 为什么必须有这个:2026-09-04 本判据两次瞬态红,报文只有 `assert 119 == 118`。
    真因是另一个窗口在这棵**正在跑的树**里 `mkdir tests/rv_poison_pkg_2026_09_04` 验锁。
    我为此去静态搜「谁往 tests/ 写目录」——搜不到,又差点归档成「找不到污染源」。
    **若当时打印了名单,那个名字会直接跳出来。**

    一条读**盘上实时状态**的锁,它的失败天然是环境性的;
    而环境性失败最需要现场快照 —— 裸断言恰好把现场丢了。

    判别用「**有没有 git 跟踪文件**」:正经判据包一定有跟踪文件,
    临时/毒/残留目录没有。这比按名字猜可靠,也不改变本判据的**判定规则**
    (规则仍是数目 + 指纹;跟踪与否只进**报文**)。
    """
    try:
        out = subprocess.run(["git", "ls-files", "--", "tests"], cwd=ROOT,
                             capture_output=True).stdout.decode("utf-8", "surrogateescape")
        tracked = {p.split("/")[1] for p in out.splitlines() if p.count("/") >= 1}
    except Exception as exc:                       # 诊断路径不许把真失败盖掉
        return (f"盘上 {len(names)} 个 · 常量 {_G.DISK_PACKAGES_COUNT} · 本判据期望 {_EXPECTED_PACKAGES}。"
                f"(取 git 跟踪名单失败:{exc!r})名单={names}")
    untracked = [n for n in names if n not in tracked]
    # 🔴 [2026-09-04 · #48 v2] 先报**名单差集**,再报未跟踪清单。
    #    v1 只列「无 git 跟踪文件的目录」—— 那只覆盖「盘上多出来」这一个方向。
    #    Review 下毒时往 `DISK_PACKAGES` 里加了一个**盘上没有**的名字:
    #    它不在盘上,所以未跟踪清单打出「0 个:(无)」,**幽灵名一个字没出现**;
    #    「盘上多了/少了」那段在后面的断言里,被这条先响的报文遮住了。
    #    ⇒ 与 `s08` 那课同形:**第一条断言的报文没带上原因**。
    #    两个方向都必须在**这一条**里点名,不许靠后面那条补。
    listed = set(getattr(_G, "DISK_PACKAGES", ()))
    extra = sorted(set(names) - listed) if listed else []
    gone = sorted(listed - set(names)) if listed else []
    return (
        f"盘上 {len(names)} 个目录 · `DISK_PACKAGES_COUNT`={_G.DISK_PACKAGES_COUNT} · 本判据期望 {_EXPECTED_PACKAGES}。\n"
        f"    🔴 盘上有而名单没有 {len(extra)} 个:{extra or '（无）'}\n"
        f"    🔴 名单有而盘上没有 {len(gone)} 个:{gone or '（无）'}\n"
        f"    其中**无 git 跟踪文件**的 {len(untracked)} 个:{untracked or '（无）'}\n"
        f"    —— 这几个最可能是临时目录/别的窗口留下的残留/未登记的新包。\n"
        f"    若确属新判据包:同批更新 DISK_PACKAGES_COUNT + SHA256 + 两级反向锚。\n"
        f"    若是残留:先问它是谁的(树/容器/目录名要带窗口前缀),别直接删。\n"
        f"    全部名单={names}")


def test_ea_12_disk_census_matches_and_the_delta_is_exactly_this_package():
    """🔴 盘上普查:数目 + 指纹都要对上,**并且**反向对照 ——
    把 gate8 从名单里去掉重算,必须逐字回到上一轮钉的旧值。

    只核「新值对不对」证明不了「差集恰是这一个包」:别的包同时被加/删,
    新指纹照样自洽。
    """
    root = ROOT / "tests"
    # 🔴 [#41] 判据侧不再自己枚举 —— 同一谓词写两处必有一处被漏掉
    #    (今天 `_WANT` 已经栽过一次)。这里直接调工具的单点实现。
    names = _G.disk_package_names(root)
    assert len(names) == _G.DISK_PACKAGES_COUNT == _EXPECTED_PACKAGES, _census_diag(root, names)
    now = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
    assert now == _G.DISK_PACKAGES_SHA256, _census_diag(root, names)
    # 🔴 反向对照两级:每级去掉**那一轮新收编的包**,回到**上一轮钉过的值**。
    #    上一版钉的是 109/108 那两版的绝对指纹,而普查涨到 115 之后它们**永远回不去**
    #    —— 「计数式判据会过期」的指纹形态:锚绑在再也不会出现的状态上,
    #    判据从「有牙」变成「恒红」,而恒红与真缺陷在退出码上同形。
    #    只核新值证明不了「差集恰是这几个」:别的包同时被加删,新值照样自洽。
    # 🔴 [2026-09-10 · 窗口 C] 本轮收编一个包(C: #170 xunhupay_query_guard)。
    #    少列的话去掉它之后剩下的还在,w1 回不到上一轮钉的 135,
    #    反向对照会红在「差集不止这一个」上,而真实原因是漏登记,不是差集脏。
    NEW_THIS_ROUND = ("xunhupay_query_guard_2026_09_10",)
    NEW_PREV_ROUND = ("mobile_pay_resume_2026_09_10",)
    w1 = [n for n in names if n not in NEW_THIS_ROUND]
    assert len(w1) == 135
    assert hashlib.sha256("\n".join(w1).encode("utf-8")).hexdigest() == \
        "e36ede46cc9604cd7e763f52fb26cd1a743a065654eb57fea4d8f18e1834ea94", \
        "去掉本轮那个包没回到上一轮钉的 135 那版指纹 —— 差集不止这一个"
    w2 = [n for n in w1 if n not in NEW_PREV_ROUND]
    assert len(w2) == 134
    assert hashlib.sha256("\n".join(w2).encode("utf-8")).hexdigest() == \
        "475cb3a545ab540f944674f751769fc29dd55f68577e56a9f366659dced53bf8", \
        "去掉上一轮那个包没回到 134 那版指纹"


def test_ea_13_the_three_attribution_classes_still_partition_the_denominator():
    """三类互斥 + 并集 == 分母(收编之后仍然成立)。"""
    inplace, minting = set(_G.INPLACE_DB), set(_G.MINTING_DB)
    nodb = set(_G.NO_DB_PACKAGES)
    assert not (inplace & minting) and not (inplace & nodb) and not (minting & nodb)
    assert inplace | minting | nodb == set(_G.GATE9_DENOMINATOR)


# ══ census 作用域必须跟着真参数走(V7-C 第二发连锁)═══════════════════════
def test_ea_14_confcut_dir_is_parsed_from_the_real_args():
    """`confcut_dir` 必须从 `EXTRA_PYTEST_ARGS` 解析,**不许手写第二张表**。"""
    assert _G.confcut_dir(PKG) == PKG
    assert _G.confcut_dir("tests/p03_settlement_2026_08_24") is None
    # 可注入 ⇒ 证明它真的读那张表,而不是认死一个包名
    assert _G.confcut_dir("tests/x", extra={"tests/x": ("--confcutdir=tests/x",)}) == "tests/x"
    assert _G.confcut_dir("tests/x", extra={"tests/x": ("--zzz",)}) is None


def test_ea_15_the_conftest_chain_follows_confcutdir():
    """🔴 带 confcutdir 的包**不算**祖先 conftest;不带的照旧一路往上收。

    这条不是洁癖:`env_census` 的分母若把根 `tests/conftest.py` 算进带 confcutdir 的包,
    就会报一条 **pytest 根本不会走的读取路径**(实测:当场把 gate8 判成
    「未计划的 DSN 读」并停机)。而消这条红的唯一办法是往 ENV_PLAN 里塞一个
    永远用不到的变量 —— **为了让锁闭嘴而写假话**,正是这条判据要挡的。
    """
    assert _G.conftest_chain(PKG) == [], \
        f"带 confcutdir 还收了祖先:{_G.conftest_chain(PKG)}"
    other = _G.conftest_chain("tests/p03_settlement_2026_08_24")
    assert any(p.name == "conftest.py" and p.parent.name == "tests" for p in other), \
        f"不带 confcutdir 的包反而没收到根 conftest:{other}"


def test_ea_16_env_census_builds_its_denominator_from_conftest_chain():
    """数据流锁:`env_census` 的文件分母必须来自 `conftest_chain(...)`,
    不许再手撸一遍「一路 .parent 往上找 conftest」的循环 —— 那样这条作用域
    就有两处实现,必有一处没跟上。"""
    fn = _fn("env_census")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "conftest_chain"]
    assert calls, "env_census 没用 conftest_chain"
    src = ast.get_source_segment(G9.read_text(encoding="utf-8-sig"), fn) or ""
    assert "anc.parent" not in src and "anc = " not in src, \
        "env_census 里还留着手撸的祖先遍历 —— 同一作用域两处实现"


def test_ea_17_a_confcut_package_is_not_charged_for_ancestor_reads():
    """端到端语义:gate8 的 DSN 读**不含**根 conftest 那个 TEST_DATABASE_URL。

    反向对照同时给出:一个不带 confcutdir 的包必须仍然被算上它。
    """
    def reads(t):
        files = sorted((ROOT / t).rglob("*.py")) + _G.conftest_chain(t)
        out = set()
        for f in files:
            out |= _G._env_reads(f)
        return {n for n in out if _G._DSNISH.search(n)} - set(_G.NON_DSN_ENVS)
    assert "TEST_DATABASE_URL" not in reads(PKG), \
        "gate8 仍被算上祖先的 TEST_DATABASE_URL"
    assert "DEFGEO_GATE8_TEST_DSN" in reads(PKG)
    assert "TEST_DATABASE_URL" in reads("tests/p03_settlement_2026_08_24"), \
        "阴性对照失效:不带 confcutdir 的包也没算上祖先 —— 那说明是我把祖先全关了"


# ══════════════════════════════════════════════════════════════════════════
# V8-C · lock_timeout(2026-08-31 那次 2h29m 挂死之后加的)
# ══════════════════════════════════════════════════════════════════════════
#
# 事故取证:18 包基线第 1 个包 `tests/defensive_geo_2026_08_21` 内自死锁 ——
#   pid 237 idle in transaction 攥 diagnosis_runs / notification_outbox /
#           point_freezes 共 17 个 AccessShareLock
#   pid 277 跑迁移 SQL 要 diagnosis_runs 的 AccessExclusiveLock,granted=f
#   pg_blocking_pids(277) = {237};包内 `SET statement_timeout = 0` ⇒ 无限期等
# 代价:整轮挂 2h29m **零产出**。加了 lock_timeout 之后,同一个竞态变成
# 几十秒内一条**点名到表和 pid** 的红 —— 红比挂死便宜且可归因。

def test_ea_18_the_lock_timeout_is_in_the_env():
    """PGOPTIONS 必须带 lock_timeout,且**已有值不被吞掉**。"""
    e = _G.env_for("tests/defensive_geo_2026_08_21")
    assert f"-c lock_timeout={_G.DEFAULT_LOCK_TIMEOUT_MS}" in e["PGOPTIONS"], e["PGOPTIONS"]
    e2 = _G.env_for("tests/x", base={"PGOPTIONS": "-c work_mem=64MB"}, plan={},
                    timeout_ms=1234)
    assert e2["PGOPTIONS"] == "-c work_mem=64MB -c lock_timeout=1234", e2["PGOPTIONS"]


def test_ea_19_the_env_really_reaches_a_child_process():
    """🔴 **机械验注入,不是散文**:拿 `env_for` 的产物真起一个子进程,
    让子进程自己把 `PGOPTIONS` 打回来。

    只断言「字典里有这个键」证不了它到得了 pytest ——
    「设了 env 不代表被调方读到」是本仓记过的事故形状。
    """
    e = _G.env_for("tests/defensive_geo_2026_08_21")
    r = subprocess.run([sys.executable, "-c",
                        "import os;print(os.environ.get('PGOPTIONS', ''))"],
                       env=e, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[-300:]
    assert f"lock_timeout={_G.DEFAULT_LOCK_TIMEOUT_MS}" in r.stdout, r.stdout
    # 阴性对照:不给这份 env 的子进程不该凭空有它
    r2 = subprocess.run([sys.executable, "-c",
                         "import os;print(os.environ.get('PGOPTIONS', ''))"],
                        env={k: v for k, v in os.environ.items() if k != "PGOPTIONS"},
                        capture_output=True, text=True, encoding="utf-8")
    assert "lock_timeout" not in r2.stdout, (
        "不给 env 的子进程也看到了 lock_timeout —— 这条判据零判别力")


@pytest.mark.parametrize("raw,why", [("0", "这道闸"), ("-1", "这道闸"),
                                     ("abc", "不是整数")])
def test_ea_20_a_bad_lock_timeout_stops_the_run(raw, why, monkeypatch):
    """0 / 负数 / 非整数一律停机 —— 「关掉」必须是显式改代码,不能靠传个 env。

    针取**短且稳**的那一段:第一版写成「关掉这道闸」,而报文里是
    `关掉**这道闸`(中间有 markdown 星号)⇒ 判据自己红了。
    判据对散文过度 specific 是它自己的病,报文改个措辞就假红。
    另外断言报文**点名了那个值** —— 这条比措辞稳。
    """
    monkeypatch.setenv("GATE9_LOCK_TIMEOUT_MS", raw)
    with pytest.raises(SystemExit) as exc:
        _G.lock_timeout_ms()
    msg = str(exc.value)
    assert why in msg, msg
    assert raw in msg, f"报文没点名那个值 {raw!r}:{msg}"


def test_ea_21_a_good_lock_timeout_is_honoured(monkeypatch):
    """阴性对照:合法值必须被采纳,否则上面三条红证明不了是「坏值被拒」。"""
    monkeypatch.setenv("GATE9_LOCK_TIMEOUT_MS", "12345")
    assert _G.lock_timeout_ms() == 12345
    monkeypatch.delenv("GATE9_LOCK_TIMEOUT_MS", raising=False)
    assert _G.lock_timeout_ms() == _G.DEFAULT_LOCK_TIMEOUT_MS


def test_ea_22_run_target_builds_its_env_through_the_single_point():
    """🔴 接线 + 可达 + 拔牙,三层一起锁(五层病谱 ③④⑤)。

    · `run_target` 的 env 必须**就是** `env_for(...)` 的返回值(同一性,不是包含)
    · 两次 `_sh` 都要把它当 `env=` 传出去
    · `env_for` 里 PGOPTIONS 那行之前不许有无条件 return(不可达)
    · PGOPTIONS 那行必须是函数体**顶层语句**(不许藏 if False / try)
    """
    fn = _fn("run_target")
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "env" for t in n.targets)]
    assert len(assigns) == 1, f"run_target 里给 env 赋值 {len(assigns)} 次(应恰 1)"
    v = assigns[0].value
    assert isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
        and v.func.id == "env_for", \
        f"env 不是 env_for(...) 的返回值:{ast.dump(v)[:120]}"
    shs = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Name) and n.func.id == "_sh"]
    assert len(shs) == 2, f"_sh 调用 {len(shs)} 次"
    for c in shs:
        kw = {k.arg: k.value for k in c.keywords}
        assert "env" in kw and isinstance(kw["env"], ast.Name) and kw["env"].id == "env", \
            f"第 {c.lineno} 行的 _sh 没把 env 传出去"

    ef = _fn("env_for")
    tops = [s for s in ef.body if isinstance(s, ast.Assign)
            and any(isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "PGOPTIONS" for t in s.targets)]
    assert tops, "env_for 里 PGOPTIONS 赋值不在函数体顶层(藏 if / try 里了?)"
    dead = [s.lineno for s in ef.body
            if isinstance(s, (ast.Return, ast.Raise)) and s.lineno < tops[0].lineno]
    assert not dead, f"env_for 在第 {dead} 行就返回了 —— PGOPTIONS 那行不可达"


# ══ V9-B · _DSNISH 扩正则全案(Review 裁定 2026-08-31)════════════════════
#: Review 裁定里逐条点名的 7 个「旧正则视野之外」的变量。
#: 分母不是我概括的一句话,是**逐个**断言 —— 概括过的清单漏掉一项不会红。
_OUT_OF_VIEW_BEFORE = [
    ("AI_SURFACE_PG_TEST_URL", "Postgres"),
    ("BRAND_IDENTITY_PG_TEST_URL", "Postgres"),
    ("LOGIN_GATE_PG_URL", "Postgres"),
    ("MONITORING_SCHEDULER_PG_TEST_URL", "Postgres"),
    ("RBPROBE_PG_URL", "Postgres"),
    ("PROGRESS_BUS_TEST_REDIS_URL", "Redis"),
    ("PROGRESS_BUS_TEST_REDIS_CONTAINER", "容器名(传输身份)"),
]


@pytest.mark.parametrize("name,kind", _OUT_OF_VIEW_BEFORE,
                         ids=[n for n, _ in _OUT_OF_VIEW_BEFORE])
def test_ea_20_each_named_variable_is_in_view_and_not_exempt(name, kind):
    """🔴 逐个点名:这 7 个必须在 `_DSNISH` 视野内,且不在豁免名单里。

    「读了却没落值 ⇒ 静默回落共享默认库 ⇒ 照样全绿」正是 env_census 存在的
    全部理由,而其中 5 个恰恰就是 Postgres URL 的形状。
    """
    assert _G._DSNISH.search(name), f"{name}({kind})不在 census 视野内"
    assert name not in _G.NON_DSN_ENVS, f"{name} 被豁免了 —— 豁免要有理由"


def test_ea_21_the_exemption_list_has_a_reason_for_every_entry():
    """🔴 豁免名单**每条带理由**,且钉大小。

    理由过去写在注释里 —— 而注释不进任何判据,「带理由」于是只是一句
    我对自己的承诺。豁免名单是最容易变成垃圾桶的东西(`NO_DB_PACKAGES`
    已经因为同一个毛病被钉过大小),这里按同一套办。
    """
    assert isinstance(_G.NON_DSN_ENVS, dict), "豁免名单要能带理由 ⇒ 必须是 dict"
    assert len(_G.NON_DSN_ENVS) == _G.NON_DSN_ENVS_SIZE, (
        f"豁免名单 {len(_G.NON_DSN_ENVS)} 条 != 钉的 {_G.NON_DSN_ENVS_SIZE} 条 —— "
        "数变了必须有人解释")
    thin = {k: v for k, v in _G.NON_DSN_ENVS.items()
            if not isinstance(v, str) or len(v.strip()) < 6}
    assert not thin, f"这些豁免没有像样的理由:{sorted(thin)}"


def test_ea_22_the_extension_is_forward_looking_not_a_silent_relaxation():
    """🔴 反向:扩正则只**放大**视野,不许把原来看得见的放出去。

    抓的毒:顺手把正则改写成另一套(比如加了 REDIS 却弄丢了 `TEST_DB`)——
    视野变了但方向反了,而 census 照样一路绿。
    """
    for old in ("X_DSN", "DATABASE_URL", "FOO_DB_URL", "BAR_DB", "TEST_DB_X"):
        assert _G._DSNISH.search(old), f"{old} 原来在视野内,扩正则之后掉出去了"


# ══ V9-B · 运输分裂(Review 裁定 2026-08-31)═══════════════════════════════
def test_ea_23_the_host_transport_set_is_exactly_gate8():
    """🔴 钉死:标了 `transport=host` 的**恰是** gate8 这一个包。

    这条防的是将来有人静默再挪一个包出去 —— 每挪一个,容器段的分母就少一个,
    而「零红在悄悄变小的分母上永远成立」正是本轮 P1-6 抓的病。
    所以运输标注跟豁免名单同样待遇:**钉集合 + 钉大小 + 每条带理由**。
    """
    assert set(_G.host_targets()) == {"tests/defgeo_gate8_2026_08_30"}
    assert len(_G.TRANSPORT) == _G.TRANSPORT_HOST_SIZE == 1
    for k, v in _G.TRANSPORT.items():
        assert k in _G.GATE9_DENOMINATOR, f"{k} 标了运输却不在冻结分母里"
        assert isinstance(v, str) and len(v) >= 20, f"{k} 的运输理由太薄:{v!r}"
        assert v.startswith("host"), f"{k} 的运输值不是 host:{v[:20]!r}"


def test_ea_24_the_two_segments_partition_the_denominator():
    """🔴 两段**不重不漏**地划分冻结分母 —— 纯判定,不读盘。"""
    cont = set(_G.container_targets())
    host = set(_G.host_targets())
    assert cont & host == set(), f"两段重叠:{sorted(cont & host)}"
    assert cont | host == set(_G.GATE9_DENOMINATOR)
    assert len(cont) == len(_G.GATE9_DENOMINATOR) - len(host) == 22


def test_ea_25_container_targets_is_not_a_new_denominator():
    """`container_targets` 只回答「这一段跑哪些」,分母仍是 18。

    两者混成一个名字,就会有人拿 17 当分母去比 —— 那正是「分母悄悄变小」。
    """
    assert len(_G.GATE9_DENOMINATOR) == _G.GATE9_DENOMINATOR_SIZE == 23
    # 注入式:换一张运输表,函数必须跟着变(不许读隐式全局)
    fake = {"tests/p03_settlement_2026_08_24": "host · 合成"}
    assert set(_G.host_targets(fake)) == {"tests/p03_settlement_2026_08_24"}
    assert "tests/p03_settlement_2026_08_24" not in set(
        _G.container_targets(transport=fake))
    assert "tests/defgeo_gate8_2026_08_30" in set(_G.container_targets(transport=fake))


def test_ea_26_identity_claims_its_segment_and_the_claim_is_counter_provable():
    """🔴 identity 里的 `transport` 必须**可被反证**,不是自报。

    判据跑在宿主上,所以这里能真验「声称 container 会被拒」;
    「声称 host 却在容器里」那一侧由 runner 侧的同一段代码守(同一个 if),
    这里用源码锁把两侧都钉住,免得只做了一半。
    """
    ident = _G.repo_identity("host")
    assert ident["transport"] == "host"
    assert ident["python"] and ident["platform"]
    assert ident["image_id"] is None, "宿主段不该有镜像 digest"
    with pytest.raises(SystemExit):
        _G.repo_identity("container")          # 判据跑在宿主 ⇒ 必须被拒
    with pytest.raises(SystemExit):
        _G.repo_identity("whatever")
    src = ast.get_source_segment(
        (ROOT / "scripts" / "gate9_full_denominator_baseline.py")
        .read_text(encoding="utf-8-sig"),
        next(n for n in ast.walk(ast.parse(
            (ROOT / "scripts" / "gate9_full_denominator_baseline.py")
            .read_text(encoding="utf-8-sig")))
            if isinstance(n, ast.FunctionDef) and n.name == "repo_identity")) or ""
    assert 'transport == "host" and in_container' in src, "缺「声称宿主却在容器里」那一侧"
    assert 'transport == "container" and not in_container' in src, "缺反向那一侧"


def test_ea_27_the_per_target_gates_follow_the_segment_not_the_denominator():
    """🔴 适用域锁:per-target 的闸必须吃**这一段真跑的包**,不是整个分母。

    实测踩过:宿主段被四个与它无关的 schema 变量拦停 ——
    闸的适用域宽于使用域,于是它在**最不该说话的时候**说话。
    这里锁的是「main 里这几个调用点的实参是 run,不是 targets」。
    """
    src = (ROOT / "scripts" / "gate9_full_denominator_baseline.py").read_text(
        encoding="utf-8-sig")
    # 🔴 真正的主体是 `_main_locked`(`main` 只负责持树锁再转调)——
    #    锚在 `main` 上会找到一个只有两次调用的壳,判据当场假红。
    #    这一格是我自己踩的:锚点要打在**干活的那个函数**上。
    main = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "_main_locked")
    wanted = {"assert_schema_fixtures": 2, "env_census": 0,
              "cold_reset_inplace_dbs": 0}
    for fname, argi in wanted.items():
        calls = [c for c in ast.walk(main) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Name) and c.func.id == fname]
        assert calls, f"main 里没有调 {fname}"
        for c in calls:
            arg = c.args[argi]
            assert isinstance(arg, ast.Name) and arg.id == "run", (
                f"{fname} 的第 {argi} 个实参是 {ast.dump(arg)[:60]} —— "
                "必须是 run(这一段真跑的包),不是 targets")


# ══ 自打毒抓到的四个洞(T2 / T5 / T6 / T7)—— 逐个定性后的补丁 ═══════════
def test_ea_28_the_in_container_claim_is_refuted_behaviourally(monkeypatch):
    """🔴 T2 补洞:「声称宿主却在容器里 ⇒ 停机」必须**行为级**验。

    自打毒实测:把那个 `if` 改成 `if False and ...` 之后,30 发毒里这一发
    **一条判据都没响** —— 因为我当天写的 `ea_26` 那一侧是**裸串锁**
    (`assert 'transport == "host" and in_container' in src`),
    毒保留了字符串、只断开了作用。第①层病(谓词存在 ≠ 谓词有牙),
    长在本轮通篇都在抓这个病的那条判据自己身上。

    改法:假装 `/.dockerenv` 存在,真调一次。
    """
    real = _G.Path.exists

    def fake(self):
        return True if str(self).replace("\\", "/").endswith("/.dockerenv") else real(self)

    monkeypatch.setattr(_G.Path, "exists", fake)
    with pytest.raises(SystemExit) as exc:
        _G.repo_identity("host")
    assert "却检测到 /.dockerenv" in str(exc.value), str(exc.value)
    # 反向:同一个假环境下,声称 container 且给了镜像身份 ⇒ 必须放行
    monkeypatch.setenv("GATE9_IMAGE_ID", "sha256:" + "a" * 64)
    ident = _G.repo_identity("container")
    assert ident["transport"] == "container"
    assert ident["image_id"] == "sha256:" + "a" * 64
    # 再反向:假环境 + 不给镜像身份 ⇒ 必须停机
    monkeypatch.delenv("GATE9_IMAGE_ID")
    with pytest.raises(SystemExit):
        _G.repo_identity("container")


def test_ea_29_the_artifact_records_the_run_set_not_the_denominator():
    """🔴 T5 补洞:产物里 `segment_targets` 的右边必须是 `run`,不是 `targets`。

    自打毒实测:换成 `list(targets)` 之后 30 发里这一发**零响应** ——
    判据只造合成证据包,没有任何一条把 gate9 的**落盘那一行**钉住。
    这里锁的是同一性(那个 `ast.Name` 就叫 `run`),不是「源码里出现过 run」。
    """
    src = (ROOT / "scripts" / "gate9_full_denominator_baseline.py").read_text(
        encoding="utf-8-sig")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_main_locked")
    hits = []
    for d in [n for n in ast.walk(fn) if isinstance(n, ast.Dict)]:
        for k, v in zip(d.keys, d.values):
            if isinstance(k, ast.Constant) and k.value == "segment_targets":
                hits.append(v)
    assert hits, "产物里根本没写 segment_targets"
    for v in hits:
        assert isinstance(v, ast.Call) and isinstance(v.func, ast.Name) \
            and v.func.id == "list" and len(v.args) == 1 \
            and isinstance(v.args[0], ast.Name) and v.args[0].id == "run", (
                f"segment_targets 的右边是 {ast.dump(v)[:80]} —— 必须是 list(run)")
    # 同一函数里 `run` 与 `targets` 确实是两个不同的东西(否则上面那条恒真)。
    # `targets` 不是本函数内赋的(它来自上游推导),所以按「一个赋值 + 一个引用」核。
    assigns = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    loads = {n.id for n in ast.walk(fn)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    assert "run" in assigns, "run 不是本函数里赋的"
    assert "targets" in loads, "targets 在本函数里根本没被引用 —— 这条判据钉错了地方"


# ══ fof8 · identity 绑 runner 版本(Review 裁定 2026-08-31)═══════════════
def test_ea_30_identity_carries_the_runner_versions():
    """🔴 尖 / 树 / 镜像 / python / platform 都绑了,**用哪个 pytest 跑的**没绑。

    这个缺口是被实证出来的:我清 staging 时删掉了 bind 进容器的 `pytestlib/`
    (镜像里没有 pytest),重建时才发现证据里**从来没有**这根轴 ——
    同一棵树、同一个镜像,换个 runner 版本收集数与行为都可能变。
    """
    ident = _G.repo_identity("host")
    rv = ident.get("runner")
    assert isinstance(rv, dict) and rv, "identity 没有非空的 runner"
    for m in ("pytest", "pytest_asyncio"):
        assert isinstance(rv.get(m), str) and rv[m].strip(), f"runner 缺 {m} 的版本"


def test_ea_31_an_unobtainable_runner_version_stops_the_round(monkeypatch):
    """🔴 取不到就**停机**,不写 None。

    「没记录」与「记了个空」在读表人眼里一样,而后者更坏:
    它让缺口看起来像是被覆盖过。这条用行为验,不看源码里有没有那句 raise。
    """
    import importlib

    real = importlib.import_module

    def boom(name, *a, **kw):
        if name == "pytest_asyncio":
            raise ImportError("boom")
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", boom)
    with pytest.raises(SystemExit) as exc:
        _G.runner_versions()
    assert "取不到 runner 版本" in str(exc.value), str(exc.value)


def test_ea_32_a_blank_version_is_not_a_record(monkeypatch):
    """版本值是空串 / 非字符串 ⇒ 同样停机 —— 键在不等于记住了。"""
    import importlib
    import types

    real = importlib.import_module

    def blank(name, *a, **kw):
        if name == "pytest":
            m = types.ModuleType("pytest")
            m.__version__ = "   "
            return m
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", blank)
    with pytest.raises(SystemExit):
        _G.runner_versions()
