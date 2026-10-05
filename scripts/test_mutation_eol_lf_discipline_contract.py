# -*- coding: utf-8 -*-
"""``scripts/`` 下 Python 文件的**行尾纪律**(Codex fix-of-fix2 P2-5 · 工单 V5-B B-5)。

本轮实测:``scripts/test_mutation_hardgate_contract.py``(基线 0 CRLF → 候选 269)
与 ``scripts/test_mutation_resume_cache_contract.py``(整文件)被写工具整文件转成
CRLF,``git diff --check 48979fb5..HEAD`` 因此报 **748 行**、退出码 2。

═══ 为什么判据不写成「diff --check 对某个 SHA 为 0」═══

那会钉一个 cutoff SHA 进判据 —— 本仓记过的**定时炸弹**:发车底一换,
这条判据要么恒真要么恒假,而两种都不会有人发现。
所以换成一条**不带时间**的常驻不变量:

    scripts/ 下**每一个**被 git 跟踪的 .py,盘上都必须是纯 LF;
    存量 CRLF 的那几个进冻结豁免集(逐条写理由、**钉大小**、过期自动报)。

``api/defensive_geo_api.py`` 的 22 条 ``trailing whitespace``(它在发车底就已经是
全文件 CRLF)**不在**这条不变量的分母里 —— 不是我手工排除,是它不在 ``scripts/``。
那笔存量债按工单口径走 post-train,不在本单。

═══ 还有一道 git 侧的闸 ═══

判据只在有人跑的时候响。``.gitattributes`` 里给
``scripts/test_mutation_*.py`` 与 ``scripts/mutation_replay_common.py``
钉了 ``text eol=lf``,git 在 ``add`` 时自己归一化 ——
写工具怎么写都带不进索引。本文件末尾那条判据钉住这两行别被人删掉。

跑法::

    python -m pytest scripts/test_mutation_eol_lf_discipline_contract.py -q
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 🔴 冻结豁免:发车底之前就是 CRLF 的存量脚本。逐条写理由,**大小钉死**。
#:    这几个都不是本轮碰的文件 —— 整文件转换会变成一整屏假 diff,
#:    掩掉真正的改动,所以不在本单顺手转(「顺手多修 = 多欠判据」)。
LEGACY_CRLF: dict[str, str] = {
    "scripts/mutation_yuanbao_hy3_2026_08_03.py":
        "2026-08-03 存量 · 本轮未碰 · 整文件转换会产生一整屏假 diff",
    "scripts/research/seed_local_testuser.py":
        "存量 research 脚本 · 本轮未碰",
    "scripts/research/wiring_check_geovid.py":
        "存量 research 脚本 · 本轮未碰",
    "scripts/run_login_gate_mutations_2026_07_29.py":
        "2026-07-29 存量 · 本轮未碰",
}
LEGACY_CRLF_SIZE = 4


def tracked_scripts() -> list[str]:
    """分母 = ``git ls-files scripts/`` 里的 ``.py`` —— **机械枚举**。

    用 git 的清单而不是 ``glob``:未跟踪的文件本来就进不了交付树,
    而 glob 会把它们算进来,让分母里混进跟"交付"无关的东西。
    """
    out = subprocess.run(["git", "ls-files", "-z", "scripts/"],
                         cwd=str(ROOT), capture_output=True, text=True)
    assert out.returncode == 0, f"git ls-files 失败:{out.stderr}"
    return sorted(x for x in out.stdout.split("\0") if x.endswith(".py"))


def crlf_offenders(root: pathlib.Path, rels) -> dict[str, int]:
    """盘上含 CRLF 的文件 → CRLF 行数。纯函数,好让这条闸自己被正样本打。"""
    bad = {}
    for rel in rels:
        p = root / rel
        if not p.is_file():
            continue
        n = p.read_bytes().count(b"\r\n")
        if n:
            bad[rel] = n
    return bad


def test_e01_the_denominator_is_not_empty():
    rels = tracked_scripts()
    assert len(rels) > 50, f"只扫到 {len(rels)} 个脚本 —— 分母为 0/太小不是通过,是探针写废了"


def test_e02_every_tracked_script_is_pure_lf_except_the_frozen_legacy_set():
    """🔴 正样本 —— 点名规则:``scripts/`` 下不许再出现新的 CRLF 文件。"""
    offenders = crlf_offenders(ROOT, tracked_scripts())
    new = {k: v for k, v in offenders.items() if k not in LEGACY_CRLF}
    assert not new, (
        f"这些脚本是 CRLF,且不在冻结豁免集里:{new}\n"
        "  它们会让 `git diff --check` 整片报警(本轮 748 行就是这么来的)。\n"
        "  转成 LF;确实必须保留 CRLF 的话,进 LEGACY_CRLF 并写理由 + 改 SIZE。")


def test_e03_the_legacy_exemption_set_is_pinned_and_not_stale():
    """豁免集必须跟着现实收缩 —— 过期的豁免会慢慢变成一张什么都放行的白名单。"""
    assert len(LEGACY_CRLF) == LEGACY_CRLF_SIZE, (
        f"豁免集大小 {len(LEGACY_CRLF)} ≠ 钉死的 {LEGACY_CRLF_SIZE}")
    assert all(v.strip() for v in LEGACY_CRLF.values()), "有豁免没写理由"
    tracked = set(tracked_scripts())
    gone = sorted(set(LEGACY_CRLF) - tracked)
    assert not gone, f"豁免集里有已经不被跟踪的文件:{gone}"
    still = crlf_offenders(ROOT, LEGACY_CRLF)
    healed = sorted(set(LEGACY_CRLF) - set(still))
    assert not healed, (
        f"这些文件已经不是 CRLF 了,豁免该摘掉:{healed} —— "
        "留着的豁免会替将来某个真的回归背锅")


def test_e04_the_checker_really_flags_a_crlf_file(tmp_path):
    """闸自己的正样本:喂一个 CRLF 文件必须被抓住,喂 LF 必须放行。

    (没有正样本的闸和恒绿的闸在读数上没有区别。)
    """
    (tmp_path / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (tmp_path / "lf.py").write_bytes(b"a = 1\nb = 2\n")
    got = crlf_offenders(tmp_path, ["crlf.py", "lf.py"])
    assert got == {"crlf.py": 2}, got


def test_e05_the_two_files_this_round_converted_are_actually_lf():
    """把本轮真正被点名的那两个文件单独钉住 —— 它们是 Codex P2-5 的原文。"""
    named = ["scripts/test_mutation_hardgate_contract.py",
             "scripts/test_mutation_resume_cache_contract.py"]
    for rel in named:
        assert (ROOT / rel).is_file(), f"{rel} 不在 —— 判据坐标烂了"
    assert crlf_offenders(ROOT, named) == {}


def test_e06_gitattributes_pins_lf_for_the_mechanism_contracts():
    """判据只在有人跑的时候响 —— git 侧那道自动闸不许被人顺手删掉。"""
    ga = (ROOT / ".gitattributes")
    assert ga.is_file(), ".gitattributes 不在"
    src = ga.read_text(encoding="utf-8-sig")
    for line in ("scripts/test_mutation_*.py text eol=lf",
                 "scripts/mutation_replay_common.py text eol=lf"):
        assert line in src, f".gitattributes 缺:{line}"


def test_e07_git_diff_check_is_clean_for_everything_this_round_touched():
    """行为向:本轮**改动过的** ``scripts/`` 文件,``git diff --check`` 必须零警告。

    分母来自 ``git status --porcelain`` + ``git diff --name-only``,机械取,
    不写死 SHA(写死 cutoff 就是定时炸弹)。
    """
    names = set()
    for cmd in (["git", "diff", "--name-only", "HEAD", "--", "scripts/"],
                ["git", "diff", "--name-only", "--cached", "HEAD", "--", "scripts/"]):
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        names.update(x for x in r.stdout.split("\n") if x.strip())
    if not names:
        pytest.skip("工作树相对 HEAD 无 scripts/ 改动(常驻分母由 e02 / e08 守)")
    r = subprocess.run(["git", "diff", "--check", "HEAD", "--", *sorted(names)],
                       cwd=str(ROOT), capture_output=True, text=True)
    assert r.returncode == 0, (
        f"本轮改动的 scripts/ 文件有空白/行尾警告:\n{r.stdout[:2000]}")


#: 变异仪器本身的文件 —— 这一族**永远**有分母,不随"有没有未提交改动"消失。
#: 范围不是随手圈的:就是跑变异 / 复放 / 硬门 / 自证的那一套。
INSTRUMENT_GLOBS = ("scripts/mutation_*.py", "scripts/test_mutation_*.py",
                    "scripts/gate9_*.py", "scripts/extsel_*.py",
                    "scripts/run_mechanism_selftests.py")


def instrument_files() -> list[str]:
    import fnmatch

    return [r for r in tracked_scripts()
            if any(fnmatch.fnmatch(r, g) for g in INSTRUMENT_GLOBS)]


def trailing_ws_offenders(root: pathlib.Path, rels) -> dict[str, int]:
    """行尾空白(空格 / 制表)的行数 —— ``git diff --check`` 报的就是这一类。"""
    bad = {}
    for rel in rels:
        p = root / rel
        if not p.is_file():
            continue
        n = 0
        for line in p.read_bytes().split(b"\n"):
            core = line.rstrip(b"\r")
            if core != core.rstrip(b" \t"):
                n += 1
        if n:
            bad[rel] = n
    return bad


def test_e08_the_mutation_instrument_has_no_trailing_whitespace_at_all():
    """🔴 常驻不变量(**不会 skip**):变异仪器这一族一条行尾空白都不许有。

    e07 只在有未提交改动时才有分母,交付轮(树已全提交)会 skip ——
    而那正是 ``git diff --check`` 最该守住的时刻。**「skip」和「过了」在读数上
    长得太像**,所以补这条:按**在盘字节**直接查,不依赖 diff、不依赖任何 SHA、
    也不依赖工作树脏不脏。

    范围只圈仪器这一族:``scripts/`` 全域另有 20 个存量分析脚本带行尾空白
    (2026-07 之前的老脚本),20 条豁免的白名单没有守卫力 ——
    宽到那个程度的豁免集本身就是一张什么都放行的通行证。
    """
    rels = instrument_files()
    assert len(rels) > 30, f"仪器文件只扫到 {len(rels)} 个 —— 分母塌了,不是通过"
    bad = trailing_ws_offenders(ROOT, rels)
    assert not bad, f"变异仪器里有行尾空白(git diff --check 会逐行报):{bad}"


def test_e09_the_trailing_whitespace_checker_really_flags_something(tmp_path):
    """闸自己的正样本 —— 没有正样本的闸和恒绿的闸在读数上没有区别。"""
    (tmp_path / "ws.py").write_bytes(b"a = 1   \nb = 2\n")
    (tmp_path / "cr.py").write_bytes(b"a = 1\r\n")
    (tmp_path / "ok.py").write_bytes(b"a = 1\nb = 2\n")
    got = trailing_ws_offenders(tmp_path, ["ws.py", "cr.py", "ok.py"])
    assert got == {"ws.py": 1}, got          # CR 由 e02 那条守,这条只看空格/制表
