"""变异 runner · 证明 `test_public_report_v2_softauth_2026_08_08.py` 真有判别力。

跑法:
    TEST_DATABASE_URL=postgresql://... \\
      python tests/mutation_runner_public_report_v2_softauth_2026_08_08.py

## 🔴 变异必须打在**调用点**上,不能打在 `_optional_bearer_identity` 内部

这是 `tests/mutation_runner_calibdeadlock_2026_08_08.py` 立的规矩,本单照搬:
本包修的是**接线**(端点内在消费之前补身份),不是那个解析函数本身 ——
函数在生产上早就是好的,坏的一直是"没人调它"。
把变异打进函数内部,测的就变成了别的东西。

## Windows 三坑(同 A 包)
1. `newline=''` 读写,防 CRLF/LF 翻转造成假阳性;
2. 每次变异后清 `__pycache__`,否则跑的是旧 pyc,变异"存活"是假的;
3. 存活先分诊「锁弱 vs 空操作」,不许直接判定判据失效。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TARGET = os.path.join(_ROOT, "api", "share_api.py")
LOCK = os.path.join(_ROOT, "tests", "test_public_report_v2_softauth_2026_08_08.py")

# 本包新加的那四行接线(v2.html 端点内)。注意 `get_public_report` 里有**逐字相同**的
# 一段(第 15 班车的,已上线)—— 所以锚点必须带上本段独有的上文,不能用裸四行去 replace,
# 否则会打到别人那一段上去。
WIRING = '''    _viewer_identity = _optional_bearer_identity(request)
    if _viewer_identity is not None:
        try:
            request.state.user = _viewer_identity
        except Exception:  # pragma: no cover - state 写不进去就当匿名,绝不因此报错
            pass

    should_v3 = False'''

MUTATIONS = [
    (
        "M1 整段接线拿掉(= 回到生产尖那个坏状态)",
        WIRING,
        "    should_v3 = False",
        ["test_1_admin_with_preview_flag_gets_v3"],
    ),
    # (原计划的 M2「接线挪到消费之后」被删:它的 old→new 与 M1 逐字相同 = 重复计数。
    #  写一条假的"不同变异"来把 4/4 凑成 5/5,和自造恒真是同一种坏。顺序正确性
    #  改由判据文件外的机械核验保证:接线行号 < is_admin_preview_request 调用行号,
    #  见交付单「接线位置核验」那一节的 git grep 实输出。)
    (
        "M3 fail-open 破坏:解析失败改成抛 401",
        WIRING,
        WIRING.replace(
            "    if _viewer_identity is not None:",
            "    if _viewer_identity is None:\n"
            "        from fastapi import HTTPException as _HE\n"
            "        raise _HE(401, detail='unauthorized')\n"
            "    if _viewer_identity is not None:",
        ),
        ["test_3_fail_open_never_401"],
    ),
    (
        "M4 无条件放行:不管解析结果直接塞 admin 身份",
        WIRING,
        WIRING.replace(
            "    if _viewer_identity is not None:",
            "    _viewer_identity = {'is_admin': True, 'user_id': 1}\n"
            "    if _viewer_identity is not None:",
        ),
        ["test_2_non_admin_with_preview_flag_does_not_get_v3",
         "test_3_fail_open_never_401"],
    ),
]

NOOP = (
    "N0 空操作对照(只改注释)",
    "#    修法与上面 `get_public_report` 第 1015-1024 行**逐字同形**",
    "#    修法与上面 `get_public_report` 第 1015-1024 行**逐字同形** (noop)",
)


def _read(p: str) -> str:
    return io.open(p, encoding="utf-8", newline="").read()


def _write(p: str, t: str) -> None:
    io.open(p, "w", encoding="utf-8", newline="").write(t)


def _purge_pycache() -> None:
    for root, dirs, _f in os.walk(_ROOT):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)


def _run_lock() -> tuple[int, str]:
    _purge_pycache()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LOCK, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    if not os.getenv("TEST_DATABASE_URL"):
        print("需要 TEST_DATABASE_URL —— 无库跑等于假绿,拒绝降级。")
        return 2

    original = _read(TARGET)
    results: list[tuple[str, bool, str]] = []

    try:
        rc, out = _run_lock()
        if rc != 0:
            print("基线就不绿,变异结论一律无效:\n" + out[-2000:])
            return 2
        print("baseline OK\n")

        for name, old, new, expect_red in MUTATIONS:
            assert expect_red, f"{name}: expect_red 为空 = 零判别力"
            if old not in original:
                results.append((name, False, f"锚点没命中 —— 变异没施加(不是存活):{old[:40]!r}"))
                continue
            # 锚点唯一性:WIRING 那段在 get_public_report 里有同形,必须确认只命中一处
            if original.count(old) != 1:
                results.append((name, False, f"锚点命中 {original.count(old)} 处 —— 会打到别的端点上"))
                continue
            mutated = original.replace(old, new, 1)
            assert mutated != original, f"{name}: 逐字节相同 = 空操作变异"
            _write(TARGET, mutated)
            rc, out = _run_lock()
            _write(TARGET, original)

            if rc == 0:
                results.append((name, False, f"存活(锁没响)· 期望转红: {expect_red}"))
            else:
                missed = [k for k in expect_red if k not in out]
                results.append((name, not missed,
                                f"转红 OK {expect_red}" if not missed else f"响了但打偏:{missed}"))

        name, old, new = NOOP
        if old not in original:
            results.append((name, False, "空操作锚点没命中"))
        else:
            mutated = original.replace(old, new, 1)
            _write(TARGET, mutated)
            rc, _ = _run_lock()
            _write(TARGET, original)
            results.append((name, rc == 0,
                            "保持绿 OK(判据不是恒红)" if rc == 0 else "空操作也转红 = 判据恒红,结论作废"))
    finally:
        _write(TARGET, original)
        _purge_pycache()

    print("\n" + "=" * 68)
    ok = 0
    for name, passed, note in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}\n         {note}")
        ok += bool(passed)
    print("=" * 68)
    print(f"  {ok}/{len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
