# -*- coding: utf-8 -*-
"""#85 · 后端 copy registry 版本 == 前端生成物版本。

## 病不是「门会 skip」,是那个字段**只写不读**

`DEFGEO_COPY_REGISTRY_VERSION` 在全仓只有**两处**出现:

    frontend/src/lib/defensiveGeoCopy.ts:22      生成物自己(当前 v6)
    scripts/defgeo_census/emit_frontend_copy.py  写它的那支笔

**零读点。** `frontend/scripts/verify-defgeo-copy-registry.mjs` 里 "version" 一次都没出现
—— 那道 lint 门核的是逐条文案,不核版本。

而后端侧**有**一条判据:`tests/defensive_geo_2026_08_21/test_wp2_user_copy.py:37`

    assert COPY_REGISTRY_VERSION == "defensive-geo-copy-v7"
    assert census()["version"] == COPY_REGISTRY_VERSION

两句都在**后端这一侧**,对前端结构性不可见。
于是 #58 升到 v7 时前端生成物停在 v6,**没有任何东西红**,还随合并班上了线。

> 这比「工具缺席就 skip」更难发现:skip 至少在报告里留一行。
> 只写不读的字段不制造任何问题 —— 它看起来就是一道正在守的闸。

## 这条锁红了不代表锁坏了

本条在 `eb3dac670` 上**是红的**,因为漂移是真的(后端 v7 / 前端 v6)。
C 的修复 `77e00fc54`(两侧同升 v8)落链后它转绿。
```
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "services" / "defensive_geo" / "copy_registry.py"
FRONTEND = ROOT / "frontend" / "src" / "lib" / "defensiveGeoCopy.ts"
EMITTER = ROOT / "scripts" / "defgeo_census" / "emit_frontend_copy.py"

FE_PAT = re.compile(r'export\s+const\s+DEFGEO_COPY_REGISTRY_VERSION\s*=\s*"([^"]+)"')


def _backend_version() -> str:
    """从**真模块**取,不是 grep 字面量 —— grep 会把注释里提到的旧版本也读进来。"""
    spec = importlib.util.spec_from_file_location("_copyreg85", BACKEND)
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m.COPY_REGISTRY_VERSION


def frontend_version(src: str) -> str | None:
    hit = FE_PAT.search(src)
    return hit.group(1) if hit else None


# ══ ① 主锁:两侧版本串必须相等 ══════════════════════════════════════
def test_copy_registry_version_agrees_across_the_seam():
    assert FRONTEND.exists(), (
        f"前端生成物不存在:{FRONTEND}\n"
        f"    🔴 这不是「通过」。文件没了 = 这条锁没有被测对象,必须红。")
    be = _backend_version()
    fe = frontend_version(io.open(FRONTEND, encoding="utf-8", newline="").read())
    assert fe is not None, (
        "前端生成物里找不到 `DEFGEO_COPY_REGISTRY_VERSION` —— "
        "要么生成器改了输出形状,要么有人手改了这个文件。")
    assert be == fe, (
        f"copy registry 版本跨层漂移:后端 {be!r} / 前端生成物 {fe!r}。\n"
        f"    前端拿着旧句子,后端按新句子回查坐标 —— 用户看到的文案与后端登记的对不上账。\n"
        f"    修法:重跑 `scripts/defgeo_census/emit_frontend_copy.py` 重新生成前端文件。\n"
        f"    🔴 为什么之前没人发现:`DEFGEO_COPY_REGISTRY_VERSION` 全仓**零读点** ——\n"
        f"       它看起来是一道版本闸,而没有任何东西读它。")


# ══ ② 前端那个字段从此有读点(这条锁自己就是) ══════════════════════
def test_the_frontend_version_field_now_has_a_reader():
    """🔴 本条钉的是「上面那条锁存在」这件事本身。

    只写不读的字段之所以能漂 5 个班次没人发现,就是因为没有读点。
    哪天有人把主锁删掉、或把它改成只读后端,这条会红。
    """
    me = io.open(pathlib.Path(__file__), encoding="utf-8", newline="").read()
    assert "DEFGEO_COPY_REGISTRY_VERSION" in me, "本文件不再读前端那个字段 —— 读点又归零了"
    assert "_backend_version()" in me and "frontend_version(" in me, \
        "主锁不再同时取两侧的值 —— 它就退化成一条单侧断言了(那正是原来的病)"


# ══ ③ 生成物只许由生成器写 ═════════════════════════════════════════
def test_the_frontend_artifact_has_exactly_one_writer():
    """两侧相等也可能是有人**手改前端**改出来的 —— 那不算同步,那叫伪造。

    钉住:写这个字段的地方只有生成器一处。
    """
    writers = []
    for p in list(ROOT.glob("scripts/**/*.py")) + list(ROOT.glob("frontend/scripts/**/*.mjs")):
        try:
            s = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "DEFGEO_COPY_REGISTRY_VERSION" in s:
            writers.append(str(p.relative_to(ROOT)).replace("\\", "/"))
    assert writers == ["scripts/defgeo_census/emit_frontend_copy.py"], (
        f"写 `DEFGEO_COPY_REGISTRY_VERSION` 的地方是 {writers}(应恰好只有生成器一处)")


# ══ ④ 🔴 自证:主锁真的有牙 ═════════════════════════════════════════
def test_the_lock_actually_bites():
    """毒下在**合成字符串**上,不动真文件;先证字节已变。"""
    src = io.open(FRONTEND, encoding="utf-8", newline="").read()
    be = _backend_version()

    # ① 前端版本被改成别的 ⇒ 必须抓到
    poisoned = FE_PAT.sub('export const DEFGEO_COPY_REGISTRY_VERSION = "defensive-geo-copy-vX"',
                          src, count=1)
    assert poisoned != src, "毒没下成(锚点漂了)—— 先修锚点,别下「锁没牙」的结论"
    assert frontend_version(poisoned) == "defensive-geo-copy-vX"
    assert frontend_version(poisoned) != be, "毒下成了但判定件没抓到 —— 这条锁没牙"

    # ② 字段整个消失 ⇒ 必须是 None(而不是悄悄取到别处的值)
    gone = FE_PAT.sub("// removed", src, count=1)
    assert gone != src
    assert frontend_version(gone) is None, \
        "字段删掉后仍读出一个值 —— 正则钩到了别的东西,这条锁在测别的字段"

    # ③ 反向对照:真文件上判定件能取到值(否则上面两发的红说明不了任何事)
    assert frontend_version(src) is not None
