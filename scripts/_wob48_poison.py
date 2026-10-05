# -*- coding: utf-8 -*-
"""#48 行为毒 —— 证明普查失败报文**真的**说出「差的是哪个」。

锁 `w48_04` 只是形状锁(看源码里有没有 `set(DISK_PACKAGES)` 与「盘上多了」)。
形状对不代表跑起来对:字符串可能被拼进一个永远走不到的分支,
也可能 extra/gone 算错方向。这发毒真跑一次。

两格:
  ① 盘上**多**一个(种未跟踪目录)⇒ 报文必须出现「盘上多了」且**点名**它
  ② 盘上**少**一个(把名单里加一个盘上没有的名字)⇒ 报文必须出现「盘上少了」且点名
第 ② 格单独做,是因为 extra 与 gone 算反方向时,只测 ① 会全绿。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

# 🔴 [2026-09-04] 环境闸:**「跑不起来」不许被报成「锁没牙」。**
#    重跑这批时我忘了 export TEST_DATABASE_URL,判据在 conftest 就 RuntimeError,
#    而本脚本的汇总行照样打印「有锁没牙」—— 一个具体但**错误**的诊断,
#    会让下一个人去改一把本来好的锁。宁可当场停机,也不要给出错的归因。
import os as _os
if not _os.environ.get("TEST_DATABASE_URL"):
    raise SystemExit(
        "🔴 缺 TEST_DATABASE_URL —— 判据会在 conftest 就炸,而那**不是**锁没牙。\n"
        "   先 export TEST_DATABASE_URL=postgresql://nouser:nopass@127.0.0.1:1/nonexistent_test")

ROOT = pathlib.Path(__file__).resolve().parent.parent
G9 = ROOT / "scripts" / "gate9_full_denominator_baseline.py"


def load():
    spec = importlib.util.spec_from_file_location("_g48p", G9)
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m


def frozen_message(mod) -> str:
    """跑普查,把 SystemExit 的报文取回来(没抛就返回空串)。"""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mod.assert_denominator_frozen()
    except SystemExit as e:
        return str(e)
    return ""


res = []

# ── ① 盘上多一个 ────────────────────────────────────────────────────
d = ROOT / "tests" / "wob48_poison_extra_2026_09_04"
try:
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    assert (d / "__init__.py").is_file(), "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    msg = frozen_message(load())
    ok = "盘上多了" in msg and d.name in msg and "盘上少了 0" in msg
    print(f"  {'OK ' if ok else 'XX '}① 盘上多一个 ⇒ 报文点名")
    print(f"       含「盘上多了」={'盘上多了' in msg} · 点名={d.name in msg} · "
          f"「少了 0」={'盘上少了 0' in msg}")
    if not ok:
        print("       报文:" + msg.replace("\n", " | ")[:260])
    res.append(ok)
finally:
    shutil.rmtree(d, ignore_errors=True)
    assert not d.exists(), "① 未清理!"

# ── ② 盘上少一个(往名单里塞一个盘上没有的名字)─────────────────────
orig = G9.read_bytes()
try:
    GHOST = "wob48_poison_ghost_never_on_disk"
    txt = orig.decode("utf-8")
    a = 'DISK_PACKAGES: tuple[str, ...] = (\n'
    assert txt.count(a) == 1, "名单锚点不唯一,不下毒"
    G9.write_bytes(txt.replace(a, a + '    "' + GHOST + '",\n', 1).encode("utf-8"))
    assert G9.read_bytes() != orig, "🔴 施毒自证:不变就停,别把随后的绿读成「锁没牙」(2026-09-04 毒③ 曾是 no-op)"
    msg = frozen_message(load())
    ok = "盘上少了" in msg and GHOST in msg and "盘上多了 0" in msg
    print(f"  {'OK ' if ok else 'XX '}② 名单多一个盘上没有的 ⇒ 报文点名「少了」")
    print(f"       含「盘上少了」={'盘上少了' in msg} · 点名={GHOST in msg} · "
          f"「多了 0」={'盘上多了 0' in msg}")
    if not ok:
        print("       报文:" + msg.replace("\n", " | ")[:260])
    res.append(ok)
finally:
    G9.write_bytes(orig)
    assert G9.read_bytes() == orig, "② 未还原!"

# ── 还原后必须安静通过 ───────────────────────────────────────────────
msg = frozen_message(load())
print(f"\n  还原后普查报文 = {msg[:60]!r}（须空)")
res.append(msg == "")
print(f"\n  {'OK  两个方向都点名了' if all(res) else 'XX  有一格不合格'}")
sys.exit(0 if all(res) else 1)
