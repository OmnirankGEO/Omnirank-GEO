"""变异 runner · 证明 `test_brand_latest_guards_2026_08_08.py` 真有判别力。

单跑判据全绿说明不了任何事 —— 恒真的断言也全绿。这里逐个把守卫**改坏**,
锁必须**当场转红**;再配一条空操作对照,证明它不是恒红。

跑法(要真库):
    TEST_DATABASE_URL=postgresql://... DATABASE_URL=$TEST_DATABASE_URL \\
      python tests/mutation_runner_brand_latest_2026_08_08.py

## 🔴 Windows 上踩过的三个坑(照这个写法就绕开了)

1. **行尾翻转**:`io.open(..., encoding='utf-8')` 读 + `newline=''` 写,
   否则 CRLF/LF 一翻转,"等字节变异"会假阳性。
2. **`__pycache__` 骗过等字节变异**:每次变异后清 `__pycache__`,
   否则改了源码却跑的是旧 pyc,变异"存活"是假的。
3. **存活先分诊**:变异存活有两种原因 —— **锁太弱** 或 **变异本身是空操作**。
   不分诊就会把"我改了个不影响行为的字符"误报成"判据没打到"。
   所以每条变异都必须**先自证它真的改变了 SQL 语义**(见 `expect_red` 非空校验)。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TARGET = os.path.join(_ROOT, "services", "brand_latest_ssot.py")
LOCK = os.path.join(_ROOT, "tests", "test_brand_latest_guards_2026_08_08.py")


# (名字, 原串, 替换串, 期望转红的用例关键字)
# 🔴 每条 expect_red 都必须非空 —— 空的等于"改了但不要求任何锁响",那是零判别力。
MUTATIONS = [
    (
        "M1 拆掉「必须是最新那份」子查询(WO_V2_REGEN 那一半守卫)",
        """       AND d.id = (
             SELECT x.id
               FROM diagnosis_records x
              WHERE x.brand_id = b.id
                AND (x.result_visibility IS NULL OR x.result_visibility = 'published')
              ORDER BY x.created_at DESC, x.id DESC
              LIMIT 1
           )""",
        "       AND TRUE",
        ["a3", "a5", "b1"],
    ),
    (
        "M2 拆掉调用方 brand_id 配对校验",
        """       AND (CAST(%(brand_id)s AS INTEGER) IS NULL
            OR b.id = CAST(%(brand_id)s AS INTEGER))""",
        "       AND TRUE",
        ["a2", "b3"],
    ),
    # 🔴 M3/M4 的第一版打偏了,如实记录分诊过程(不是"锁弱",是变异打在被覆盖的那一层):
    #
    #   第一版 M3 = 只拆外层 `AND d.brand_id = b.id` → **存活**。
    #   分诊结论:归属条件在数学上被「最新」子查询蕴含 —— 子查询按 `x.brand_id = b.id` 取数,
    #   所以 `d.id = (latest-of-b)` 已经意味着 `d.brand_id = b.id`。这是**冗余纵深防御**,
    #   拆一层另一层照样挡得住,判据当然不会红。
    #   第一版 M4 = 只拆外层 published 过滤 → **存活**,同一形状(子查询内还有一份)。
    #
    #   → 改成打真正承载该维度的那一层;同时保留"只拆一层"的版本作**期望存活**对照,
    #     把这层冗余关系显式钉住:哪天有人把子查询改了,M3b/M4b 会从"存活"变"转红",
    #     那正是需要有人看一眼的信号。
    (
        "M3 归属维度:外层 + 子查询两层一起拆",
        "       AND d.brand_id = b.id",
        "       AND TRUE",
        # 🔴 只列 a2。第一版我把 b3 也列进来 → "响了但打偏"。
        #    分诊:b3 用的是 5 天前那份诊断,归属两层全拆掉之后,
        #    「最新」子查询仍会算出全局最新 published(1 天前那份)把它挡住 ——
        #    b3 的绿是**另一维度**挡的,它本来就不是归属维度的隔离用例。
        #    真正只由归属维度决定的是 a2。**期望写宽 = 自造假红,和自造恒真一样坏。**
        ["a2"],
        "              WHERE x.brand_id = b.id",  # 第二处同时拆
        "              WHERE TRUE",
    ),
    (
        "M4 published 维度:打在子查询那一层(真正承载它的地方)",
        "                AND (x.result_visibility IS NULL OR x.result_visibility = 'published')",
        "                AND TRUE",
        ["a4"],
    ),
    (
        "M5 守卫写成恒假(= 把功能砍掉的错误实现)",
        "     WHERE d.id = %(diagnosis_id)s",
        "     WHERE FALSE AND d.id = %(diagnosis_id)s",
        ["a1", "a2b", "b2", "b4"],
    ),
]

# 冗余守卫对照(**期望存活**)。它们证明的是「纵深防御真的有两层」——
# 只拆一层,另一层照样挡得住,所以判据保持绿是**正确结果**,不是漏检。
# 🔴 哪天这两条从"存活"变成"转红",说明有人把另一层也改了 → 必须有人看一眼。
REDUNDANCY_CONTROLS = [
    ("R1 只拆外层归属(子查询那层还在)",
     "       AND d.brand_id = b.id", "       AND TRUE"),
    ("R2 只拆外层 published(子查询那层还在)",
     "       AND (d.result_visibility IS NULL OR d.result_visibility = 'published')",
     "       AND TRUE"),
]

# 空操作对照:改注释不改行为 → 全部锁必须**保持绿**。
# 没有这条,一份恒红的判据也能拿到 5/5。
NOOP = ("N0 空操作对照(只改注释)", "# 三重约束一次写清。", "# 三重约束一次写清。 (noop)")


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8", newline="").read()


def _write(path: str, text: str) -> None:
    io.open(path, "w", encoding="utf-8", newline="").write(text)


def _purge_pycache() -> None:
    """改了源码必须清 pyc,否则跑的还是旧字节码 —— 变异会假'存活'。"""
    for root, dirs, _files in os.walk(_ROOT):
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
        print("✗ 需要 TEST_DATABASE_URL —— 无库跑等于假绿,拒绝降级。")
        return 2

    original = _read(TARGET)
    results: list[tuple[str, bool, str]] = []

    try:
        # 0. 基线:未变异必须全绿。基线红了,后面所有"转红"都没有意义。
        rc, out = _run_lock()
        if rc != 0:
            print("✗ 基线就不绿,变异结论一律无效:\n" + out[-2000:])
            return 2
        print("✓ 基线全绿\n")

        for entry in MUTATIONS:
            name, old, new, expect_red = entry[0], entry[1], entry[2], entry[3]
            extra = entry[4:] if len(entry) > 4 else ()
            assert expect_red, f"{name}: expect_red 为空 = 零判别力,不许这么写"
            if old not in original:
                results.append((name, False, "锚点串没命中 —— 变异根本没施加(不是'存活')"))
                continue
            mutated = original.replace(old, new, 1)
            if extra:
                if extra[0] not in mutated:
                    results.append((name, False, f"第二处锚点没命中:{extra[0]!r}"))
                    continue
                mutated = mutated.replace(extra[0], extra[1], 1)
            assert mutated != original, f"{name}: 替换后逐字节相同 = 空操作变异"
            _write(TARGET, mutated)
            rc, out = _run_lock()
            _write(TARGET, original)

            if rc == 0:
                results.append((name, False, f"🔴 存活(锁没响)· 期望转红: {expect_red}"))
            else:
                missed = [k for k in expect_red if k not in out]
                if missed:
                    results.append((name, False, f"响了但打偏:{missed} 没转红"))
                else:
                    results.append((name, True, f"转红 ✓ {expect_red}"))

        # 冗余守卫对照:期望**存活**(保持绿)
        for name, old, new in REDUNDANCY_CONTROLS:
            if old not in original:
                results.append((name, False, "锚点没命中"))
                continue
            mutated = original.replace(old, new, 1)
            _write(TARGET, mutated)
            rc, _ = _run_lock()
            _write(TARGET, original)
            results.append((name, rc == 0,
                            "存活 ✓ 另一层挡住了(纵深防御成立)" if rc == 0
                            else "🔴 转红 = 另一层也没了,冗余关系已破,需人看"))

        # 空操作对照
        name, old, new = NOOP
        mutated = original.replace(old, new, 1)
        assert mutated != original, "空操作对照本身没生效"
        _write(TARGET, mutated)
        rc, _ = _run_lock()
        _write(TARGET, original)
        results.append((name, rc == 0, "保持绿 ✓(证明判据不是恒红)" if rc == 0 else "🔴 空操作也转红 = 判据恒红,全部结论作废"))

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
