# -*- coding: utf-8 -*-
"""复放对账**集合式**的合同判据(Review 收口单② · 2026-08-28)。

起因是本轮实跑的一个现成反例:我跑的是「11 发清单 − MUT-EXTE3-05 + MUT-EXTE2-04」,
执行集与清单集**已经不同**,而 `REPLAY_LIST_SIZE`(= 11)那道**计数**闸
从头到尾没响过 —— 它数的是清单长度,不是"到底跑了哪几发"。

**计数式判据会过期,集合式不会。** 最刺眼的形态就是「换一个成员、大小不变」:
计数闸一声不吭,而账已经错了。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_replay_setwise_recon_contract.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "gate9_replay_from_list.py"

_spec = importlib.util.spec_from_file_location("replay_driver", DRIVER)
_D = importlib.util.module_from_spec(_spec)
sys.modules["replay_driver"] = _D
sys.path.insert(0, str(DRIVER.parent))
_spec.loader.exec_module(_D)

LIST_IDS = [
    "MUT-EXTE2-07", "MUT-EXTE2-08", "MUT-EXTE2-09", "MUT-EXTE2-10",
    "MUT-EXTE2-12", "MUT-EXTE3-03", "MUT-EXTE3-05", "MUT-EXTE3-07",
    "MUT-EXTE3-11", "MUT-EXTE3-13", "MUT-EXTE3-15",
]


def test_recon_01_expected_set_applies_the_registered_adjustments():
    """应跑集合 = 清单 ± 登记增删,数目也跟着变(11 − 1 + 2 = 12)。"""
    want = _D.expected_replay_set(LIST_IDS)
    assert "MUT-EXTE3-05" not in want, "退役的原 id 还在应跑集合里"
    assert "MUT-EXTE3-05b" in want, "重锚继任者没进应跑集合"
    assert "MUT-EXTE2-04" in want, "收编后要记正式杀的那发没进应跑集合"
    assert len(want) == len(LIST_IDS) - 1 + 2


def test_recon_02_swapping_one_member_at_the_same_size_must_go_red():
    """🔴 正样本 —— **点名规则**:04 换 05,大小不变、集合不同,必须判红。

    这就是计数式对账看不见的那一脚:`len(got) == len(want)`,
    而跑的根本不是同一批发。
    """
    want = _D.expected_replay_set(LIST_IDS)
    got = set(want)
    got.discard("MUT-EXTE2-04")
    got.add("MUT-EXTE3-05")            # 换回退役的那个 id
    assert len(got) == len(want), "这条正样本的前提是**同大小**,否则它证不到点子上"
    with pytest.raises(SystemExit) as exc:
        _D.assert_set_equal(got, want, "自证")
    msg = str(exc.value)
    assert "MUT-EXTE3-05" in msg and "MUT-EXTE2-04" in msg, (
        "两向差集没打全 —— 读的人还得自己猜是多了还是少了")


def test_recon_03_equal_sets_pass():
    want = _D.expected_replay_set(LIST_IDS)
    _D.assert_set_equal(set(want), want, "自证")      # 不抛即通过


def test_recon_04_every_adjustment_cites_the_ledger():
    """每一笔增删都要引台账 —— 没有出处的增删就是「我记得应该这样」。"""
    assert _D.REPLAY_ADJUSTMENTS, "增删表空了 —— 那这条判据在空转"
    for adj in _D.REPLAY_ADJUSTMENTS:
        assert adj.get("ledger"), f"{adj['id']} 的增删没有台账出处"
        assert adj.get("why"), f"{adj['id']} 的增删没有理由"
        assert adj["op"] in ("+", "-")


def test_recon_05_a_stale_removal_stops_the_run():
    """删一个**不在清单里**的 id ⇒ 这笔增删自己过期了,必须停机。

    否则增删表会像旧锚一样悄悄失效,而失效的增删**不会让任何东西变红**。
    """
    with pytest.raises(SystemExit):
        _D.expected_replay_set([i for i in LIST_IDS if i != "MUT-EXTE3-05"])


def test_recon_06_the_driver_actually_calls_the_setwise_check():
    """结构锁:驱动里必须真的调集合式对账,而不是只定义了它。"""
    src = DRIVER.read_text(encoding="utf-8")
    assert src.count("assert_set_equal(") >= 3, (
        "集合式对账没有在开跑前 + 跑完后都调 —— 只调一次就只守住一半")


def test_recon_08_the_selftest_runner_enumerates_mechanically_not_by_hand():
    """🔴 点名规则:跑全套自证的**分母必须机械枚举**,不许手写文件清单。

    2026-08-28 我每次都手打 7 个文件名。再加第八个合同文件而忘了写进命令,
    它会**静默不跑**,而「73 passed」照样好看 ——
    「手写分母漏掉的那一项不会让任何判据变红」,本仓老病。

    另一半:那支 runner 必须把 pytest 的**真退出码**原样交出去。
    我用 `pytest ... | tail -2 && git commit` 时,管道的退出码是 `tail` 的 0,
    `&&` 没短路,红着也 commit 了。
    """
    import ast as _ast

    runner = ROOT / "scripts" / "run_mechanism_selftests.py"
    assert runner.is_file(), "自证 runner 不在 —— 那就还是靠手打命令"
    src = runner.read_text(encoding="utf-8")
    tree = _ast.parse(src)

    # ① 分母来自 glob,不是字面清单
    assert ".glob(" in src, "自证 runner 没用 glob 枚举 —— 分母又变成手写的了"

    # ② 空分母不算通过
    assert "分母为 0" in src or "if not files" in src, "没有空分母守卫"

    # ③ 退出码原样返回:main 里必须 `return rc`,不许无条件 return 0
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "main")
    bare_zero = [n.lineno for n in _ast.walk(fn)
                 if isinstance(n, _ast.Return)
                 and isinstance(n.value, _ast.Constant) and n.value.value == 0]
    assert not bare_zero, f"第 {bare_zero} 行无条件 return 0 —— 真退出码被吞了"

    # ④ 覆盖:盘上每个合同文件都落在它的枚举里
    import importlib.util as _iu

    spec = _iu.spec_from_file_location("_selftest_runner", runner)
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    enumerated = {p.name for p in mod.discover()}
    on_disk = {p.name for p in (ROOT / "scripts").glob("test_mutation_*.py")}
    assert on_disk, "盘上一个合同文件都没有 —— 探针写废了"
    assert on_disk <= enumerated, (
        f"这些合同文件不在自证分母里:{sorted(on_disk - enumerated)}")


def test_recon_07_every_contract_suite_on_disk_is_tracked_by_git():
    """🔴 判据文件必须**在树上** —— 不在树上的绿等于没有。

    起因(2026-08-28 我自己踩的):本文件最初叫
    ``scripts/test_replay_setwise_recon_contract.py``,跑出 6 passed 也报了,
    但 ``.gitignore:209`` 的 ``test_*.py`` 把它吞了 —— 负例
    ``.gitignore:326 !scripts/test_mutation_*.py`` 只放开 ``test_mutation_*``,
    我的文件名不在其内。于是 ``git add -A`` **静默**跳过、
    ``git status --porcelain`` 因为 ignored 文件不显示而报"干净",
    我据此报了「工作树 0 改动」+「38/38」。**报的绿必须出自最终 commit 的树。**

    更刺的是:那条负例上面两行的注释逐字写着
    「那种"要记得"的规矩迟早漏」—— 前手已经预言过,我还是漏了。
    所以把它从"要记得"变成"跑起来会自己报错"。
    """
    import subprocess

    suites = sorted(p.name for p in (ROOT / "scripts").glob("test_*contract*.py"))
    assert suites, "一个合同判据文件都没扫到 —— 探针写废了(分母为 0 不是通过)"

    untracked = []
    for name in suites:
        rel = f"scripts/{name}"
        rc = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                            cwd=str(ROOT), capture_output=True).returncode
        if rc != 0:
            untracked.append(rel)
    assert not untracked, (
        f"这些判据文件在盘上但**不在 git 索引里**:{untracked}\n"
        "  它们跑出来的绿进不了交付树 —— 检查 .gitignore(scripts/ 下只有 "
        "test_mutation_*.py 被负例放开),改名或补负例,别只在本地跑绿。")
