# -*- coding: utf-8 -*-
"""#41 盘上普查口径合同 —— 守「计入 = 目录里有未被 git 忽略的文件」这条派生规则。

🔴 **本文件只读**:不种目录、不动树。
   要种目录的那三格(A 未跟踪有内容 / B 只剩 __pycache__ / C 空目录)在
   `scripts/_wob41_poison.py` 里,**手动跑**。
   理由:常驻判据若每跑一次就往 `tests/` 里种东西,两个窗口并发跑就会互相污染 ——
   2026-09-04 上午 `test_ea_12` 两次瞬态红,真因正是另一个窗口在这棵**正在跑的树**里
   种了目录。把破坏性动作留在常驻判据里,等于把那次事故做成每日重演。
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import gate9_full_denominator_baseline as _G   # noqa: E402


def _git(args: list[str]) -> str:
    r = subprocess.run(["git"] + args, cwd=ROOT, capture_output=True)
    assert r.returncode == 0, f"git {' '.join(args)} 退出 {r.returncode}"
    return r.stdout.decode("utf-8", "surrogateescape")


# ══ D 格 · 每一个有 tracked 文件的目录都必须被计入(逐个核,不抽样)══════
def test_w41_01_every_dir_with_tracked_files_is_counted():
    """分母机械枚举:从 `git ls-files` 反推「应当计入」的全集,逐个核。"""
    counted = set(_G.disk_package_names())
    on_disk = {p.name for p in (ROOT / "tests").iterdir() if p.is_dir()}
    want = {l.split("/")[1] for l in _git(["ls-files", "--", "tests"]).splitlines()
            if l.count("/") >= 1} & on_disk
    assert want, "应计入集合为空 —— 探针坏了,别信下面的断言(空集合会让断言恒真)"
    missing = sorted(want - counted)
    assert not missing, (
        f"这些目录有 git 跟踪文件却**没被计入** {len(missing)} 个:{missing[:8]}。"
        f"口径把真判据包漏掉了 —— 分母会小于真实值,而小了不会有人发现。")
    print(f"\n    D 格:{len(want)} 个含 tracked 文件的目录,逐个核,全部计入")


# ══ __pycache__ 必须不计(旧口径靠手写名字,新口径应当靠「被 ignore」派生)══
def test_w41_02_pycache_is_excluded_by_derivation_not_by_name():
    counted = _G.disk_package_names()
    assert "__pycache__" not in counted, "__pycache__ 被计进分母了"
    # 反向臂:证明它确实是**被 ignore** 才不计,不是碰巧目录不存在
    assert (ROOT / "tests" / "__pycache__").is_dir(), (
        "本机 tests/__pycache__ 不存在 ⇒ 上一条断言是**恒真**的,证明不了排除逻辑。"
        "先跑一次 pytest 生成它再来核。")
    out = _git(["check-ignore", "-v", "--", "tests/__pycache__"])
    assert out.strip(), "git 不认为 tests/__pycache__ 被忽略 —— 那它凭什么不计入?"


# ══ 接线锁 · 三个调用点都必须走单点实现,不许各自枚举 ═══════════════════
_CALLERS = ("scripts/gate9_full_denominator_baseline.py",
            "scripts/test_mutation_gate9_extra_args_contract.py")


def test_w41_03_no_call_site_enumerates_the_directory_itself():
    """🔴 同一谓词写多处,必有一处被漏掉 —— 2026-09-04 `_WANT` 刚栽过一次。

    这条锁打的是**代码形状**:除 `disk_package_names` 自己之外,
    不许再有第二处 `iterdir()` 之后按名字排除 `__pycache__`。
    """
    offenders = []
    for rel in _CALLERS:
        src = (ROOT / rel).read_text(encoding="utf-8-sig")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            seg = ast.unparse(node)
            if "__pycache__" in seg and ".name" in seg:
                # 允许出现在 disk_package_names 内部(它就是那个单点)
                offenders.append(f"{rel}: {seg[:60]}")
    assert not offenders, (
        f"仍有调用点自己枚举并手写排除:{offenders}。"
        f"口径必须只有一处实现,否则改口径时必有一处忘改。")


def test_w41_03b_the_shape_detector_would_flag_a_planted_one():
    """反向臂:喂合成的坏形状,检测器必须点名 —— 否则上一条是恒真的。"""
    bad = ast.parse('x = [p for p in d.iterdir() if p.name != "__pycache__"]')
    hits = [ast.unparse(n) for n in ast.walk(bad)
            if isinstance(n, ast.Compare) and "__pycache__" in ast.unparse(n)
            and ".name" in ast.unparse(n)]
    assert hits, "检测器对已知的坏形状没有反应 ⇒ test_w41_03 恒绿"


# ══ 规则本身:计数与钉死值一致(变了要求做一次决定,不是自动接受)══════
def test_w41_04_the_derived_rule_agrees_with_the_pinned_denominator():
    names = _G.disk_package_names()
    assert len(names) == _G.DISK_PACKAGES_COUNT, (
        f"派生口径数出 {len(names)},钉的是 {_G.DISK_PACKAGES_COUNT}。"
        f"若是新包 ⇒ 同批更新 COUNT + SHA256 + 两级反向锚;"
        f"若是残留 ⇒ 先问它是谁的(名字要带窗口前缀),别直接删。")


def test_w41_05_git_is_required_and_failure_is_loud():
    """普查依赖 git。**拿不到必须停机**,不许静默返回空集合 ——
    空集合会让分母读成 0,而 0 与「目录全没了」在读数上同形。"""
    src = (ROOT / "scripts" / "gate9_full_denominator_baseline.py").read_text(
        encoding="utf-8-sig")
    # 🔴 用 AST 取整段,不用字符串切片。第一版写的是
    #    `src[i:src.index("def ", i+10)]` —— 它撞上函数**内部嵌套的** `def _seg1(`,
    #    body 被截断在 SystemExit 之前,于是这条判据红了,而被测代码完全正常。
    #    又一次「红了,但红在另一件事上」:先定位归属再报。
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "disk_package_names")
    body = ast.unparse(fn)
    assert "SystemExit" in body, (
        "disk_package_names 里没有停机路径 —— git 失败时它会安静地返回不完整的集合")
    assert "returncode" in body, "没有检查 git 的退出码"
