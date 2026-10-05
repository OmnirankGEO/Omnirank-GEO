"""[R5 批1 返修] 全仓测试文件 basename 唯一性锁。

病(Review 合跑撞到,我逐目录 A/B 撞不到):
    pytest 在**没有 `__init__.py`** 的目录里靠 basename 当模块名 import 测试文件。
    两个目录各有一个同名文件 ⇒ 第二个 import 时报
        `import file mismatch ... use a unique basename for your test file modules`
    ⇒ **收集阶段当场炸**。批 1 我给六个目录各放了一个
    `test_brands_fixture_ssot_lock.py`,正好踩中(已改成带目录名的唯一 basename)。

🔴 为什么锁写成**静态扫描**而不是 `pytest tests/ --collect-only` 的 rc:
    实测底(9d359800d)上 `pytest tests/ --collect-only -q` 就已经 **rc=1**,
    9 个 error 里有 3 个是**别人的 DSN 安全栓**(dealer_inventory_resale / gap_plan /
    gap_plan_migration 在 import 期就 raise RuntimeError,拒绝非专用库),
    其余随本机 docker 起没起而变。拿 rc=0 当判据 = 判据红不红取决于**环境**而不是代码,
    这种尺子量不出「有人又加了同名文件」。
    ⇒ 本锁只打**这个缺陷本身**:零依赖、零 DB、纯文件系统枚举,分母是全仓每一个 test_*.py。
    (配套的 `--collect-only` 冒烟另有一条,只断言**不许出现 import file mismatch**,
     见 test_collect_only_has_no_import_file_mismatch —— 那条对环境不敏感。)
"""
import os
import pathlib
import subprocess
import sys
from collections import defaultdict

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"


def _walk_test_files():
    for dirpath, dirnames, filenames in os.walk(TESTS):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for f in filenames:
            if f.startswith("test_") and f.endswith(".py"):
                yield pathlib.Path(dirpath) / f


def test_no_two_test_files_share_a_basename_outside_packages():
    """🔴 同名 test_*.py 只有在**两边都有 __init__.py** 时才安全,否则收集当场炸。"""
    by_name = defaultdict(list)
    for p in _walk_test_files():
        by_name[p.name].append(p)

    assert by_name, "一个 test_*.py 都没扫到 —— 零分母,判据等于恒绿"

    offenders = {}
    for name, paths in by_name.items():
        if len(paths) < 2:
            continue
        # 两个文件都在 package 里(各自目录有 __init__.py)时模块名不冲突
        unpackaged = [p for p in paths if not (p.parent / "__init__.py").exists()]
        if unpackaged:
            offenders[name] = sorted(str(p.relative_to(REPO)) for p in paths)

    assert not offenders, (
        "以下 basename 在多个**非 package** 目录里重复,`pytest tests/` 收集会炸:\n"
        + "\n".join("  %s\n    %s" % (n, "\n    ".join(v)) for n, v in sorted(offenders.items()))
        + "\n→ 改成唯一 basename(例如加目录名后缀)。"
          "**不要**加 __init__.py:那会改既有目录的 import 语义。")


def test_the_scanner_would_actually_catch_a_collision():
    """配对反向:证明上面那把尺子**量得出**冲突,不是恒绿。

    在内存里塞一对假的同名文件,复用同一段归并逻辑,必须判出来。
    """
    fake = defaultdict(list)
    fake["test_thing.py"] = [TESTS / "aaa" / "test_thing.py", TESTS / "bbb" / "test_thing.py"]
    offenders = {n: v for n, v in fake.items()
                 if len(v) > 1 and [p for p in v if not (p.parent / "__init__.py").exists()]}
    assert offenders, "扫描逻辑连人造冲突都判不出来 —— 上面那条判据是恒绿的"


def test_collect_only_has_no_import_file_mismatch():
    """`pytest tests/ --collect-only` 的输出里不许出现 import file mismatch。

    🔴 只断言这一种签名,**不断言 rc=0**:底上 rc 本来就是 1(别人的 DSN 安全栓在
       import 期 raise + 本机 docker 起没起),拿 rc 当判据是在量环境不是量代码。
       这条对环境不敏感 —— 无论几个目录因为缺库报错,都不会冒出 mismatch 这个签名。
    """
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", "tests/",
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=1800,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    out = proc.stdout + proc.stderr
    assert "tests collected" in out or "collected" in out, (
        "收集根本没跑起来 —— 零分母:\n" + out[-2000:])
    assert "import file mismatch" not in out, (
        "收集期出现 basename 冲突:\n"
        + "\n".join(l for l in out.splitlines() if "mismatch" in l or "unique basename" in l))
