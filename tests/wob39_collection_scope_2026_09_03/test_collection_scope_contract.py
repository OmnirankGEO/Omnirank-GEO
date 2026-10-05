# -*- coding: utf-8 -*-
"""#39 收集范围合同锁 —— 守「`scripts/` 下的 547 条判据真的在默认收集范围内」。

🔴 **为什么这个文件住在 `tests/` 而不是 `scripts/`**:
   它守的东西是「`scripts` 在不在 `testpaths` 里」。若把锁放进 `scripts/`,
   那么有人一旦把 `scripts` 从 `testpaths` 摘掉,**锁自己也随之不被收集** ——
   守卫和被守物一起消失,退出码全绿,没有任何人会收到消息。
   锁必须活在**比它守的范围更外层**的地方。
   同族教训:判据放在收集范围之外 ⇒ 默认收集 0 个,常量漂五次零信号。

三道锁 + 每道的反向臂(反向臂不是注释,是可执行的 `_*_would_flag` 测试)。
"""
from __future__ import annotations

import ast
import configparser
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
PYTEST_INI = ROOT / "pytest.ini"

# 2026-09-03 实测基线(`pytest --collect-only scripts/`,哑 DSN + 封 socket):
#   19 个判据文件 · 384 个 test_ 函数 · **547 个收集项**
# 锁写**下界**不写等值 —— 新增判据不该让门变红。
MIN_CRITERIA_FILES = 19
MIN_TEST_FUNCS = 384


def _testpaths() -> list[str]:
    cp = configparser.ConfigParser()
    cp.read(PYTEST_INI, encoding="utf-8")
    return cp["pytest"]["testpaths"].split()


def _count_test_funcs(src: str) -> int:
    """数 `test_` 开头的函数 —— AST,不是 `grep "def test"`
    (后者会把注释里、字符串里的写法一起数进去)。"""
    tree = ast.parse(src)
    return sum(1 for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name.startswith("test_"))


def _zero_criteria_offenders(root: pathlib.Path) -> list[str]:
    """分母:机械枚举 —— `rglob`,不是手写清单。
    返回「名字匹配 test_*.py 但一条判据都没有」的文件。"""
    bad = []
    for p in sorted(root.rglob("test_*.py")):
        try:
            if _count_test_funcs(p.read_text(encoding="utf-8-sig")) == 0:
                bad.append(str(p.relative_to(root)))
        except SyntaxError:
            bad.append(f"{p.relative_to(root)}（语法错误,无法判定)")
    return bad


# ══ 锁 ① · 接线:scripts 必须在收集根内 ══════════════════════════════
def test_w39_01_scripts_is_inside_the_collection_root():
    """没有这一条,下面两条锁全部空转 —— 它们检查的文件根本不会被跑到。"""
    tp = _testpaths()
    assert "scripts" in tp, (
        f"pytest.ini 的 testpaths = {tp},不含 `scripts`。"
        f"⇒ scripts/ 下 {MIN_TEST_FUNCS}+ 条判据默认一条都不跑,"
        f"而「不跑」在汇总里长得跟「没问题」一模一样。"
    )
    assert "tests" in tp, f"testpaths = {tp},丢了 `tests`"


def test_w39_01b_the_testpaths_reader_would_notice_a_removal():
    """反向臂:证明上面那条不是恒真 —— 换一份没有 scripts 的 ini,判定必须翻。"""
    cp = configparser.ConfigParser()
    cp.read_string("[pytest]\ntestpaths = tests\n")
    assert "scripts" not in cp["pytest"]["testpaths"].split(), (
        "读 ini 的这段代码对『scripts 不在里面』这种输入没有反应 ⇒ 锁①是恒真的"
    )


# ══ 锁 ② · 命名空间:scripts/ 下不许再出现「叫 test_ 却没判据」的文件 ══
def test_w39_02_no_zero_criteria_file_wears_the_test_prefix():
    """这类文件不是判据,是会**自己干活**的脚本 —— 进收集范围就是 import 执行。

    2026-09-03 清掉的 8 个已移到 `scripts/manual/`(改名去掉 test_ 前缀,
    仅移目录不够:`rglob("test_*.py")` 照样会递归到子目录里去)。
    """
    offenders = _zero_criteria_offenders(SCRIPTS)
    assert offenders == [], (
        f"scripts/ 下出现了 {len(offenders)} 个「叫 test_ 却零判据」的文件:{offenders}。"
        f"它们会在收集期被 import —— 模块级代码当场执行。"
        f"处置:不是判据就移到 scripts/manual/ 并去掉 test_ 前缀。"
    )


def test_w39_02b_the_offender_detector_would_flag_a_planted_one(tmp_path):
    """反向臂:喂一个合成的坏文件,检测器必须点名它。

    🔴 喂的是**合成样本**,不改真文件 —— 我 08-31 栽过一次:
    为了验判据去改真代码,超时被杀后残留把别的门弄红了。
    """
    (tmp_path / "test_planted_no_criteria.py").write_text(
        "import os\nVALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_planted_has_criteria.py").write_text(
        "def test_something():\n    assert True\n", encoding="utf-8")
    found = _zero_criteria_offenders(tmp_path)
    assert found == ["test_planted_no_criteria.py"], (
        f"检测器返回 {found} —— 它要么漏了零判据的那个(锁②恒绿),"
        f"要么把有判据的也算进去了(锁②会误伤)"
    )


# ══ 锁 ③ · 分母:scripts/ 的判据规模不许缩水 ═══════════════════════
def test_w39_03_scripts_criteria_volume_does_not_shrink():
    """写下界不写等值:新增判据不该让门变红,**消失**才该。"""
    files = sorted(SCRIPTS.glob("test_*.py"))
    total = sum(_count_test_funcs(p.read_text(encoding="utf-8-sig")) for p in files)
    assert len(files) >= MIN_CRITERIA_FILES, (
        f"scripts/ 判据文件 {len(files)} 个 < 基线 {MIN_CRITERIA_FILES} —— 有文件消失了")
    assert total >= MIN_TEST_FUNCS, (
        f"scripts/ 判据函数 {total} 条 < 基线 {MIN_TEST_FUNCS} —— 有判据被删或被改名")


def test_w39_03b_the_counter_actually_counts(tmp_path):
    """反向臂:计数器对已知输入必须给出已知答案,否则「≥ 基线」可能是它恒返回大数。"""
    (tmp_path / "a.py").write_text(
        "def test_x(): pass\n"
        "def helper(): pass\n"
        "class C:\n    def test_y(self): pass\n", encoding="utf-8")
    n = _count_test_funcs((tmp_path / "a.py").read_text(encoding="utf-8"))
    assert n == 2, f"计数器数出 {n},应为 2(test_x + C.test_y,不含 helper)"


# ══ 锁 ④ · 隔离:manual/ 里的东西不许再被当判据收集 ═══════════════
def test_w39_04_manual_scripts_do_not_match_the_test_pattern():
    manual = SCRIPTS / "manual"
    if not manual.is_dir():
        pytest.fail("scripts/manual/ 不存在 —— 8 个手工脚本的隔离没落地")
    matched = [p.name for p in manual.glob("test_*.py")]
    assert matched == [], (
        f"scripts/manual/ 下仍有 {matched} 匹配 test_*.py。"
        f"仅移目录不够 —— 收集是递归的,子目录里的 test_*.py 照样被收。"
    )
    assert len(list(manual.glob("*.py"))) >= 8, "manual/ 少于 8 个脚本,可能漏移"
