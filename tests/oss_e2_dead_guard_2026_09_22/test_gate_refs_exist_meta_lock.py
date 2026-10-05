"""WO_261 ③ · 元锁 `gate_refs_exist` 的判据。

三态各一格 + 一格**最要紧的反向对照**:它不许惩罚肯定式退役。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "gate_refs_exist.py"
VICTIM = ROOT / "frontend" / "scripts" / "verify-pay-exit.mjs"


def _run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _with_appended(extra: str):
    """往某个真门禁末尾临时追加一段,跑完**按字节还原**。"""
    original = VICTIM.read_bytes()

    class _Ctx:
        def __enter__(self):
            VICTIM.write_bytes(original + extra.encode("utf-8"))
            return _run()

        def __exit__(self, *exc):
            VICTIM.write_bytes(original)
            assert VICTIM.read_bytes() == original, "还原失败"
            return False

    return _Ctx()


@pytest.fixture(scope="module")
def clean_readings(tmp_path_factory):
    """[ONESHOT 提速 4] 没动过的树上的两次读数,三格共用(原先各跑各的:工作树模式 2 次、ref 模式 2 次)。
    ref 模式用**改名后的副本**跑(`g.py`,cwd 仍是仓根)—— 与 preflight 真实形态一致,
    自排除格和两路一致格都读它;改名若改变了读数,两格都会红。"""
    copy = tmp_path_factory.mktemp("gate_refs_copy") / "g.py"
    copy.write_bytes(GATE.read_bytes())
    ref = subprocess.run(
        [sys.executable, str(copy), "--repo", str(ROOT), "--ref", "HEAD"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return {"wt": _run(), "ref_renamed": ref}


def test_baseline_is_zero_missing(clean_readings) -> None:
    """正控:改前读数 0 缺失、rc=0。

    这一格同时是**分母自证**:输出里必须印出两个面的条数,
    「扫了 0 个门」与「扫了 108 个门没发现」不许长得一样。
    """
    r = clean_readings["wt"]
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 条缺失" in r.stdout
    assert "build链" in r.stdout and "scripts/*.py" in r.stdout


def test_an_injected_missing_path_turns_it_red() -> None:
    """反臂①:塞一个不存在的路径 ⇒ rc=1。"""
    probe = "\nconst __probe = 'src/pages/__wo261_missing_zzz__.tsx';\n"
    with _with_appended(probe) as r:
        assert r.returncode == 1, r.stdout + r.stderr
        assert "__wo261_missing_zzz__" in r.stdout


def test_a_negative_assertion_is_not_punished() -> None:
    """🔴 反臂②(本门最要紧的一格):**肯定式退役不得被判红**。

    `ok(!existsSync(...), '已删')` 断言的是「这个路径不该存在」——
    那是让退役可验证的写法。要求它存在,等于逼着人把这种断言删掉。
    实测基线里 17 条「引用了但不存在」**一条真陈旧引用都没有**,
    全是这类否定断言 / 人造探针 / 注释里的历史记录。
    """
    probe = ("\nok(!existsSync(join(ROOT, 'src/pages/__wo261_retired_zzz__.tsx')),"
             " '已删');\n")
    with _with_appended(probe) as r:
        assert r.returncode == 0, (
            "把「断言此路径已不存在」判成红了 —— 本门在惩罚正确的退役写法\n"
            + r.stdout + r.stderr
        )


def test_registries_carry_reasons() -> None:
    """两张豁免登记的每条都必须带理由,不许空着。"""
    sys.path.insert(0, str(ROOT))
    from scripts import gate_refs_exist as g

    assert g.SYNTHETIC_PROBES and g.GENERATED_OUTPUTS
    for table in (g.SYNTHETIC_PROBES, g.GENERATED_OUTPUTS):
        for path, reason in table.items():
            assert reason.strip(), f"{path} 的豁免没写理由"


def test_the_self_calibration_arm_exists() -> None:
    """尺子自校准必须在,且校准不过要作废结论而不是放行。"""
    src = GATE.read_text(encoding="utf-8")
    assert "_selftest" in src
    assert "自校准不过" in src and "rc=3" in src


def test_self_exclusion_survives_being_renamed(clean_readings) -> None:
    """🔴 自排除必须按**仓内路径**判,不按 `__file__` 的文件名。

    preflight 会 `git show` 出本脚本、存成 `g.py` 再跑。按名字排除在那里**当场失效**,
    于是它扫到自己的自校准夹具(`a/b.tsx` / `src/nope_zzz.tsx`)报假阳 ——
    实测发生过,3 条。
    这与 WO_245 给密钥扫描器修过的洞是同一个:
    **「谁决定这个文件叫什么」不是我,就不能拿名字当依据。**
    """
    r = clean_readings["ref_renamed"]
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nope_zzz" not in r.stdout, "改名后扫到了自己的自校准夹具"
    assert "a/b.tsx" not in r.stdout


def test_ref_mode_and_worktree_mode_agree(clean_readings) -> None:
    """两条读取路径必须给同一个答案 —— 否则其中一条在骗人。"""
    wt, ref = clean_readings["wt"], clean_readings["ref_renamed"]
    assert wt.returncode == ref.returncode == 0, wt.stdout + ref.stdout
    assert "0 条缺失" in wt.stdout and "0 条缺失" in ref.stdout
