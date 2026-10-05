"""变异 runner 的中断安全自证(Review 2026-08-08 要求)

🔴 为什么要专门测这个:
   变异后的代码长得跟正常代码**几乎一样**(只多一行 `# MUTATION Mx`)。
   外部超时/强杀落在变异窗口里,工作树就带着变异代码留下来了;
   下一个人拿到的是一棵被改过的树,甚至可能把变异当"实现"提交上去。
   这是本 runner 最坏的失败模式 —— 比"变异存活"坏得多,因为它会污染交付物。

🔴 这些用例**不跑真 runner**(那要几分钟),只针对还原机制本身:
   备份登记 / 信号还原 / 残留检测拒跑。判据打在机制上,不打在"我觉得它会还原"上。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "tests" / "gap_plan_2026_08_08" / "mutation_runner.py"


def _load_runner():
    """按文件加载,避免 import 时触发 __main__ 逻辑。"""
    spec = importlib.util.spec_from_file_location("_gap_mutation_runner", RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_gap_mutation_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    mod = _load_runner()
    # 把备份目录挪到 tmp,绝不碰真仓库
    monkeypatch.setattr(mod, "BACKUP_DIR", tmp_path / ".mutation_backup")
    mod._IN_FLIGHT.clear()
    return mod


def test_restore_all_puts_the_original_bytes_back(runner, tmp_path):
    target = tmp_path / "victim.py"
    original = b"def f():\n    return self.available > 0\n"
    target.write_bytes(original)

    runner._IN_FLIGHT[target] = original
    runner._keep_backup(target, original)
    target.write_bytes(b"def f():\n    return True  # MUTATION\n")
    assert target.read_bytes() != original          # 反向对照:变异真的改到了字节

    runner._restore_all("test")
    assert target.read_bytes() == original          # 逐字节
    assert not runner._IN_FLIGHT                     # 在途表清空
    assert not (runner.BACKUP_DIR / target.name).exists()   # 备份也清掉


def test_stale_backup_makes_the_next_run_refuse_to_start(runner, tmp_path):
    """🔴 硬杀(SIGKILL/taskkill /F)谁也挡不住 —— 唯一能挡的是**下一轮拒跑**。

    残留备份 = 上一轮没正常收尾 = 工作树状态不明。
    """
    assert runner._abort_if_stale_backup() is False   # 反向对照:干净时不误报

    victim = tmp_path / "victim.py"
    victim.write_bytes(b"x = 1\n")
    runner._keep_backup(victim, b"x = 1\n")

    assert runner._abort_if_stale_backup() is True


def test_stale_check_does_not_auto_restore(runner, tmp_path):
    """拒跑而**不自动还原** —— 上一轮状态不明时,静默覆盖别人的工作树更糟。"""
    victim = tmp_path / "victim.py"
    mutated = b"y = 2  # MUTATION\n"
    victim.write_bytes(mutated)
    runner._keep_backup(victim, b"y = 2\n")

    runner._abort_if_stale_backup()
    assert victim.read_bytes() == mutated, "残留检测不该动工作树,只该拒跑"


def test_backup_is_written_before_the_file_is_mutated(runner):
    """顺序判据:登记+备份必须在写文件**之前**。

    反了就存在一个"文件已改但没人知道要还原"的窗口期,硬杀正好落在那里。
    这条直接读 runner 源码核顺序 —— 它是**关于代码顺序**的命题,
    在运行时是观察不到的(2026-08-08 那条「重言式锁」的同一课:
    判据要和命题打在同一个东西上)。
    """
    src = RUNNER_PATH.read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    i_register = body.index("_IN_FLIGHT[path] = original")
    i_backup = body.index("_keep_backup(path, original)")
    i_write = body.index("_write(path, applied.encode")
    assert i_register < i_write, "登记在途必须先于写文件"
    assert i_backup < i_write, "落盘备份必须先于写文件"


def test_guards_cover_signal_and_atexit(runner):
    """三层兜底都在(finally / 信号 / atexit)。缺哪层都少挡一类中断。"""
    src = RUNNER_PATH.read_text(encoding="utf-8")
    assert "atexit.register" in src
    assert "SIGINT" in src and "SIGTERM" in src
    assert "finally:" in src
    # 反向对照:装护栏这一步真的**被 main 调用**了(定义了没接线等于没有)。
    # 🔴 判据要找调用不是定义 —— 第一版 index("_install_guards()") 命中的是
    #    `def _install_guards() -> None:` 那一行,于是"调用在 main 之后"恒假。
    #    与那条重言式锁同一种病:判据打偏了,而且**打偏的方向是恒红**,
    #    恒红和恒真一样废 —— 它证明不了护栏接没接。
    lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    main_at = next(i for i, l in enumerate(lines) if l.startswith("def main("))
    called_at = [i for i, l in enumerate(lines)
                 if l.strip() == "_install_guards()" and l.startswith(" ")]
    assert called_at, "_install_guards 只定义了没调用 —— 护栏没接线"
    assert min(called_at) > main_at, "护栏必须在 main 里装(且在跑变异之前)"
