# -*- coding: utf-8 -*-
"""[E1-3①] WOA runner **退出合同**的判据。

Codex 二审 §8 点了三个洞,每一个都会把「没跑成」伪装成「跑成了且全绿」:

  ① ``rc`` 只打印不检查 —— pytest 的 2/3/4(collect 错 / 库没起 / 用法错)
     配上空的或不存在的 junit,``or []`` 一折就成了"零红";
  ② junit 缺失时 ``red_nodeids`` 返 ``None``,``None or []`` = 空红集 ——
     "证据文件没生成"与"证据显示全绿"长得一模一样;
  ③ 存活时 ``main()`` 仍返 ``None``(rc=0)—— CI 只看退出码时,
     "有判据洞"与"全杀"长得一模一样。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_runner_woa_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "mutation_runner_woa_diag_funding.py"

_spec = importlib.util.spec_from_file_location("woa_runner", RUNNER)
_woa = importlib.util.module_from_spec(_spec)
sys.modules["woa_runner"] = _woa
_spec.loader.exec_module(_woa)


# ── ①② 跑数可信度合同(行为判据)─────────────────────────────────────
def test_woa_01_a_crashed_pytest_is_not_zero_red(tmp_path):
    """rc=2/3/4 ⇒ 抛。这是"没跑完"被折成"零红"的那一条。"""
    junit = tmp_path / "j.xml"
    junit.write_text("<testsuite/>", encoding="utf-8")
    for rc in (2, 3, 4, 5):
        with pytest.raises(_woa.RunnerContractError) as e:
            _woa.assert_run_is_trustworthy(rc, junit, "tail", "自证")
        assert "rc=" in str(e.value)


def test_woa_02_a_missing_junit_is_not_zero_red(tmp_path):
    """junit 没生成 ⇒ 抛。**即使 rc=0** —— 没有证据文件就没有红集。"""
    missing = tmp_path / "nope.xml"
    assert not missing.exists()
    for rc in (0, 1):
        with pytest.raises(_woa.RunnerContractError) as e:
            _woa.assert_run_is_trustworthy(rc, missing, "tail", "自证")
        assert "junit" in str(e.value)


def test_woa_03_a_normal_run_passes_the_contract(tmp_path):
    """反向对照:rc∈{0,1} 且 junit 在 ⇒ 放行。不许把正路也堵死。"""
    junit = tmp_path / "j.xml"
    junit.write_text("<testsuite/>", encoding="utf-8")
    for rc in (0, 1):
        _woa.assert_run_is_trustworthy(rc, junit, "tail", "自证")   # 不抛即通过


def test_woa_04_red_nodeids_still_returns_none_for_a_missing_file(tmp_path):
    """``red_nodeids`` 本身仍返 None —— 合同闸的存在意义就是**不让它被 ``or []`` 吃掉**。

    这一条钉的是"闸和被闸的东西是两件事":哪天有人把 None 改成 [],
    合同闸就成了摆设,这条会红。
    """
    assert _woa.red_nodeids(tmp_path / "nope.xml") is None


# ── ③ 存活 ⇒ 非零退出(结构判据,理由写明)──────────────────────────
def test_woa_05_survivors_make_the_runner_exit_non_zero():
    """存活时 ``main`` 必须返回 1,且 ``__main__`` 必须把它变成进程退出码。

    🔴 如实说明:这一条是**结构判据**,不是行为判据。要行为验就得真造一个
       "会存活"的变异 —— 那意味着故意留一个判据洞在树上。代价与收益不匹配,
       所以这里钉两件可机械核的事:
         · ``main`` 里存在 ``return 1 if survived else 0``;
         · ``__main__`` 分支是 ``raise SystemExit(main())`` 而不是裸 ``main()``。
       ①② 两条是真行为验,这一条是接线验 —— 分清楚,不混着报。
    """
    src = RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    # ① 「存活 ⇒ 返 1」这条返回**存在于模块的某个函数里**。
    #    不钉在 `main` 上:主体后来被树锁包装挪进了 `_main_locked`,
    #    钉函数名的判据会在一次合理重构里失效(而那种失效是**假绿**方向)。
    holders = [name for name, fn in funcs.items()
               if any(isinstance(r.value, ast.IfExp)
                      for r in ast.walk(fn) if isinstance(r, ast.Return))]
    assert holders, "全模块找不到「存活 ⇒ 返 1」那条返回 —— 存活和全杀退出码一样"

    # ② 退出码要**端到端传得出去**:main 必须 return 出去(而不是吞掉),
    #    __main__ 必须把它变成进程退出码。
    main_fn = funcs.get("main")
    assert main_fn is not None, "runner 没有 main()"
    main_returns = [r for r in ast.walk(main_fn) if isinstance(r, ast.Return)]
    assert main_returns and all(r.value is not None for r in main_returns), (
        "main() 有一条 return 是空的 —— 退出码在这里被吞掉了")
    if "_main_locked" in holders:
        assert any(isinstance(r.value, ast.Call) for r in main_returns), (
            "主体已挪进 _main_locked,但 main 没把它的返回值 return 出来")

    assert "raise SystemExit(main())" in src, (
        "__main__ 里不是 raise SystemExit(main()) —— main 返 1 也不会变成进程退出码")
