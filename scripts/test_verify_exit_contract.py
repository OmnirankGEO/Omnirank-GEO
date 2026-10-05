# -*- coding: utf-8 -*-
"""#95 · verify-*.mjs 必须三态退出:0 过 / 1 失败 / 3 未评估。

## 缺陷是什么(实测,不是推测)

在一棵**没构建**的树上跑全部 28 个 `frontend/scripts/verify-*.mjs`,记真实退出码:

    修前   rc=0 : 18    rc=1 : 5    rc=3 : 5
    修后   rc=0 : 18    rc=1 : 1    rc=3 : 9

🔴 修前那 5 个 rc=1 里,**4 个其实是「未评估」**:

| 脚本 | 真因 | 真实档 |
|---|---|---|
| `verify-no-lookbehind` | `dist/assets` ENOENT | 未评估 |
| `verify-observation-bundle` | 未找到 `dist-observation-preview` | 未评估 |
| `verify-public-privacy-bundle` | `scandir dist` ENOENT,而且是**未捕获异常**崩退 | 未评估 |
| `verify-safeimage-render` | `EADDRINUSE 127.0.0.1:8793`,自己的探针服务起不来 | 未评估 |
| `verify-organization-enum-labels` | 枚举映射缺 5 项 | **真失败** |

⇒ **80% 的「失败」是假的**,而它们与真失败在退出码上一模一样。
两档的处置正好相反:未评估 = 去构建 / 起服务 / 腾端口;失败 = **改代码**。

## 「未评估」还要再分档

实测撞到三种形态,处置各不相同:
缺产物(去 build)/ 缺服务(去起)/ **端口被占**(腾端口或换 `SAFEIMAGE_PROBE_PORT`)。
所以 `unevaluated()` 要求传**具名条目数组**,拒绝只报计数 —— 只报「3 项未评估」,
读的人不知道该做哪一件。
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCR = ROOT / "frontend" / "scripts"
HELPER = SCR / "_verify_exit.mjs"

#: 已接三态的脚本 —— **只许增不许减**。
#:    减少 = 有人把某个脚本的三态退回两态,而那件事不会留下别的痕迹。
THREE_STATE_AWARE: frozenset[str] = frozenset({
    # 本来就三态(自带 notEvaluated 计数 + exit 3)
    "verify-p0-62-64.mjs", "verify-p65-inline-price.mjs",
    "verify-plan-error-actionable.mjs", "verify-question-list-merge.mjs",
    "verify-stale-price.mjs",
    # #95 本笔改的四个(前置条件不满足 ⇒ exit 3 且具名)
    "verify-no-lookbehind.mjs", "verify-observation-bundle.mjs",
    "verify-public-privacy-bundle.mjs", "verify-safeimage-render.mjs",
})

#: 🔴 冻结例外:「这个脚本的前置守卫就该 exit 1」是一个**需要理由的主张**。
PRECONDITION_EXIT1_BY_DESIGN: dict[str, str] = {}


def verify_scripts() -> list[pathlib.Path]:
    return sorted(SCR.glob("verify-*.mjs"))


def _read(p: pathlib.Path) -> str:
    return io.open(p, encoding="utf-8", errors="replace").read()


# ══ ① 共享件本身要成立 ═════════════════════════════════════════════
def test_the_shared_exit_contract_defines_three_states():
    assert HELPER.exists(), f"三态共享件不在:{HELPER}"
    s = _read(HELPER)
    for k, v in (("PASS", "0"), ("FAIL", "1"), ("UNEVALUATED", "3")):
        assert re.search(rf"{k}\s*:\s*{v}\b", s), f"共享件里没定义 {k}={v}"
    assert "export function unevaluated" in s, "共享件没导出 unevaluated()"


def test_unevaluated_refuses_a_bare_count():
    """🔴 只报「N 项未评估」没有用:缺产物 / 缺服务 / 端口被占,三者处置不同。

    共享件必须**拒绝**只传计数(或空数组)的调用,否则「列名」这条规矩没有执行者。
    """
    s = _read(HELPER)
    assert re.search(r"if \(!Array\.isArray\(items\)[^)]*\|\|[^)]*items\.length === 0\)", s), (
        "`unevaluated()` 没有拒绝「非数组 / 空数组」—— "
        "那「报文必须列名未评估项」就只是一句注释,没人执行。")


# ══ ② 接线:已三态的脚本只许增不许减 ═══════════════════════════════
def test_three_state_scripts_still_are():
    """分母 = `frontend/scripts/verify-*.mjs` 机械枚举。

    判「是三态」= 它要么 import 共享件的 `unevaluated`,要么自己有 `process.exit(3)`。
    """
    present = {p.name for p in verify_scripts()}
    missing = sorted(THREE_STATE_AWARE - present)
    assert not missing, f"登记为三态的脚本已不在盘上:{missing} —— 同笔从登记里删或改名"

    lost = []
    for name in sorted(THREE_STATE_AWARE):
        s = _read(SCR / name)
        # 🔴 必须钉**调用**,不是「文件里出现过这个词」——
        #    `import { unevaluated } from './_verify_exit.mjs'` 会让包含判定恒真:
        #    实测反臂把调用删掉、只留 import,这条锁照样绿。
        #    (「接线锁被 import 行顶住」——同一个病今天第二次。)
        calls = len(re.findall(r"(?<![\w.])unevaluated\s*\(", s))
        ok = calls > 0 or re.search(r"process\.exit\(\s*3\s*\)", s)
        if not ok:
            lost.append(f"{name}(unevaluated 调用 {calls} 处 · 无 exit(3))")
    assert not lost, (
        f"这些脚本**退回两态**了(既不引共享件的 unevaluated,也没有 exit(3)):{lost}\n"
        f"    「未评估」被压回「失败」之后,在没构建的树上跑会产出假的失败 —— "
        f"实测修前 5 个 rc=1 里有 4 个是假的。")


# ══ ③ 新增的前置守卫不许 exit(1) ═══════════════════════════════════
def test_no_new_precondition_guard_exits_one(  ):
    """🔴 `if (!existsSync(x)) { … process.exit(1) }` 这种形状 = 把「没跑成」报成「失败」。

    分母机械枚举全部 verify 脚本;新写这种守卫 ⇒ 红,要求改用 `unevaluated()`。
    """
    bad = []
    for p in verify_scripts():
        s = _read(p)
        # existsSync 判否之后 3 行内 exit(1)
        for m in re.finditer(r"if\s*\(\s*!\s*existsSync\([^)]*\)\s*\)\s*\{(?:[^{}]|\{[^{}]*\})*?\}", s, re.S):
            blk = m.group(0)
            if re.search(r"process\.exit\(\s*1\s*\)", blk):
                bad.append(f"{p.name}: {re.sub(r'\\s+', ' ', blk)[:90]}")
    unexplained = [b for b in bad if b.split(":")[0] not in PRECONDITION_EXIT1_BY_DESIGN]
    assert not unexplained, (
        "这些前置守卫在「东西不存在」时 exit(1):\n    " + "\n    ".join(unexplained)
        + "\n    那是把**未评估**报成**失败** —— 两者处置相反"
          "(去构建/起服务 vs 改代码),而退出码一样。\n"
          "    改用 `unevaluated([...具名条目...], '在验什么')`(见 frontend/scripts/_verify_exit.mjs)。")


def test_no_precondition_exception_without_a_reason():
    assert PRECONDITION_EXIT1_BY_DESIGN == {}, (
        f"有人往 `PRECONDITION_EXIT1_BY_DESIGN` 加了 "
        f"{sorted(PRECONDITION_EXIT1_BY_DESIGN)} 却没改本条 —— 要加就同笔写清理由。")


# ══ ④ 分母自证 ═════════════════════════════════════════════════════
def test_the_denominator_is_mechanical_and_nonempty():
    ss = verify_scripts()
    assert len(ss) >= 20, f"只枚举到 {len(ss)} 个 verify 脚本 —— 分母塌了,先查路径 {SCR}"
    assert len(THREE_STATE_AWARE) <= len(ss), "登记数超过盘上脚本数 —— 登记表有幽灵条目"
