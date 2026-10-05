"""包H · **跨层**判据 —— 前端那几份东西必须和后端 SSOT 对得上。

═══════════════════════════════════════════════════════════════════
🔴 为什么这些判据在 pytest 而不在 build 链
═══════════════════════════════════════════════════════════════════
``Dockerfile`` 的 ``frontend-builder`` 阶段只 ``COPY frontend/ ./`` ——
后端源码**不在那一层**,build 链里读后端会被
``verify-no-backend-refs-in-build-chain`` 判红(判得对)。

窗D 已经为同一个问题做过一次处置(UI-37 前后端动作同源判据搬去
``tests/defensive_geo_w4_2026_08_22/test_frontend_backend_action_parity.py``)。
这里沿用:**判据搬到够得着两层的那一侧**,而不是加 ``try/except → SKIP``
(那是裸奔判据:构建大声跳过、退出 0,而大家只看得到构建绿)。

三条跨层锁:
  X-1  ``frontend/src/lib/defensiveGeoCopy.ts`` 必须与生成器当场导出的内容
       **逐字节相同** —— 手改前端文案 = 红,后端改译文没重新生成 = 红。
  X-2  前端 ``PUBLISH_CUSTOMER_ENTRY_OPEN`` 与后端 ``_CUSTOMER_PUBLISH_ENTRY_OPEN``
       **逐值相同** —— 两个方向都堵:后端开了前端还画"没开放"⇒ 点不到;
       前端开了后端还关着 ⇒ 她按下确认拿到 403。
  X-3  前端深链落点那句「还没开放」与后端 typed 拒绝用的是**同一条** registry 文案。
"""

from __future__ import annotations

import io
import os
import re

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONT = os.path.join(REPO, "frontend", "src")


def _read(*parts: str) -> str:
    with io.open(os.path.join(REPO, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


# ══════════════════════════════════════════════════════════════════════════
# X-1 · 前端文案是生成物,必须与后端 registry 逐字节一致
# ══════════════════════════════════════════════════════════════════════════
def _rendered() -> str:
    from scripts.defgeo_census.emit_frontend_copy import render

    return render()


def test_frontend_copy_file_is_byte_identical_to_generator():
    """手改前端文案 = 红;后端改了译文而没重新生成 = 红。"""
    on_disk = _read("frontend", "src", "lib", "defensiveGeoCopy.ts")
    assert on_disk == _rendered(), (
        "frontend/src/lib/defensiveGeoCopy.ts 与后端 registry 漂了。"
        "重新跑:python scripts/defgeo_census/emit_frontend_copy.py"
    )


def test_generator_has_discriminating_power():
    """必须不命中的那一半:改一个字就必须对不上。

    没有这条,上面那条与「两边都是空字符串」无法区分。
    """
    mutated = _rendered().replace("价格没变", "价格没有变化")
    assert mutated != _rendered(), "注毒没生效 —— 这一发**没跑**"
    on_disk = _read("frontend", "src", "lib", "defensiveGeoCopy.ts")
    assert on_disk != mutated


def test_every_exported_key_resolves_in_backend_registry():
    """生成器的分母(EXPORTED)必须条条能在后端 registry 里解析到。"""
    from scripts.defgeo_census.emit_frontend_copy import EXPORTED
    from services.defensive_geo.copy_registry import census

    entries = census()["entries"]
    missing = [f"{k}.{c}" for _n, k, c in EXPORTED if c not in entries.get(k, {})]
    assert not missing, f"这些条目后端 registry 里没有:{missing}"
    assert len(EXPORTED) >= 25, f"导出条目只有 {len(EXPORTED)} 条,疑似分母被砍"


def test_no_phantom_copy_keys_used_in_frontend():
    """前端用到的每个 ``DEFGEO_COPY.xxx`` 都必须真的存在。

    幽灵 key 在 TS 里会被 ``as const`` 挡住,但**只挡到编译期** ——
    这条是编译之外的那一层:分母从生成器现取,不手抄。
    """
    from scripts.defgeo_census.emit_frontend_copy import EXPORTED

    known = {name for name, _k, _c in EXPORTED}
    used: set[str] = set()
    for root, _dirs, files in os.walk(FRONT):
        for fn in files:
            if not fn.endswith((".ts", ".tsx")):
                continue
            text = _read(os.path.relpath(os.path.join(root, fn), REPO))
            used.update(re.findall(r"DEFGEO_COPY\.([A-Za-z0-9_]+)", text))
    assert used, "全仓没有任何地方用 DEFGEO_COPY —— 判据零分母"
    phantom = sorted(used - known)
    assert not phantom, f"前端用了生成器没导出的 key:{phantom}"


# ══════════════════════════════════════════════════════════════════════════
# X-2 · 发布链客户入口:前端镜像 == 后端常量
# ══════════════════════════════════════════════════════════════════════════
def _backend_entry_open() -> bool:
    src = _read("api", "defensive_publish_api.py")
    m = re.search(r"^_CUSTOMER_PUBLISH_ENTRY_OPEN\s*=\s*(True|False)\s*$", src, re.M)
    assert m, "后端 _CUSTOMER_PUBLISH_ENTRY_OPEN 的赋值行没找到 —— 判据锚点失效,记『没验』"
    return m.group(1) == "True"


def _frontend_entry_open() -> bool:
    src = _read("frontend", "src", "pages", "DefensivePublish", "publishEntryGate.ts")
    m = re.search(r"export const PUBLISH_CUSTOMER_ENTRY_OPEN\s*=\s*(true|false);", src)
    assert m, "前端 PUBLISH_CUSTOMER_ENTRY_OPEN 的赋值行没找到 —— 判据锚点失效,记『没验』"
    return m.group(1) == "true"


def test_publish_entry_flag_mirrors_backend():
    """两个常量必须一起翻。

    上一次(终审 P0-1)只摘了前端路由,后端端点还能直接调 —— 靠"记得同时改"
    防不住。这条把它变成机器会喊的事。
    """
    assert _frontend_entry_open() == _backend_entry_open(), (
        "前端 PUBLISH_CUSTOMER_ENTRY_OPEN 与后端 _CUSTOMER_PUBLISH_ENTRY_OPEN 不一致:"
        "后端开前端关 ⇒ 功能上线了但点不到;前端开后端关 ⇒ 她按下确认拿到 403。"
    )


def test_entry_flag_lock_has_discriminating_power():
    """必须不命中的那一半:两边不一致时这条锁必须能判红。"""
    assert _frontend_entry_open() != (not _backend_entry_open()), (
        "自证:把任意一边取反,上面那条断言就不成立"
    )


# ══════════════════════════════════════════════════════════════════════════
# X-3 · 深链落点那句「还没开放」= 后端同一条 registry 文案
# ══════════════════════════════════════════════════════════════════════════
def test_deep_link_closed_sentence_equals_backend_registry():
    from services.defensive_geo.copy_registry import user_label

    backend = user_label("reason", "publish_entry_closed")
    src = _read("frontend", "src", "pages", "DefensivePublish", "PublishDeepLink.tsx")
    m = re.search(r"export const ENTRY_CLOSED_SENTENCE\s*=\s*\n?\s*'([^']+)';", src)
    assert m, "前端 ENTRY_CLOSED_SENTENCE 没找到 —— 判据锚点失效,记『没验』"
    assert m.group(1) == backend, (
        f"深链落点那句与后端 registry 不一致:\n前端 {m.group(1)!r}\n后端 {backend!r}"
    )


def test_closed_sentence_says_money_did_not_move():
    """这句话必须同时说清两件事:①还没开放不是你操作错了 ②钱没动。

    只说其中一件,她会开始怀疑另一件。
    """
    from services.defensive_geo.copy_registry import user_label

    s = user_label("reason", "publish_entry_closed")
    assert "还没有开放" in s, s
    assert "没有扣除任何算力" in s, s


@pytest.mark.parametrize("bad", ["发布这一步还没有开放。", "暂时不能确认；没有扣除任何算力。"])
def test_closed_sentence_lock_rejects_half_the_promise(bad: str):
    """必须不命中的那一半:只说一件事的句子必须过不了上面那条。"""
    assert not ("还没有开放" in bad and "没有扣除任何算力" in bad)


# ══════════════════════════════════════════════════════════════════════════
# X-4 · [工单 V5-C · C-4 · Codex fix-of-fix2 P3-1] 生成物声称存在的那道门
#       必须**真的存在**、真的能跑、真的有判别力
# ══════════════════════════════════════════════════════════════════════════
# ``emit_frontend_copy.py`` 的抬头和它生成的 TS 文件抬头都写着
# 「门:frontend/scripts/verify-defgeo-copy-registry.mjs(build 链内)」,
# 而仓库里根本没有这个文件。于是"漂了就红"这句承诺一直是空的:
# X-1 那条 pytest 是唯一在守的东西,而前端那侧一无所有。
#
# 🔴 分母机械化:不是只补这一个文件,而是把**"被引用的 .mjs 都得存在"**
#    整类钉住 —— 单点修一个,下一次换个名字引用又会漏。
_MJS_REF = re.compile(r"(?:frontend/)?scripts/([A-Za-z0-9_.\-]+\.mjs)")

#: 分母的来源:哪些文件里的 .mjs 引用要算数。
_MJS_REF_SOURCES = (
    ("frontend", "package.json"),
    ("scripts", "defgeo_census", "emit_frontend_copy.py"),
    ("frontend", "src", "lib", "defensiveGeoCopy.ts"),
)


def _referenced_mjs() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for parts in _MJS_REF_SOURCES:
        for name in _MJS_REF.findall(_read(*parts)):
            found.setdefault(name, []).append("/".join(parts))
    return found


def test_every_referenced_frontend_script_actually_exists():
    """被引用的 ``.mjs`` 必须在盘上。分母来自三处引用源,不手抄。"""
    refs = _referenced_mjs()
    assert len(refs) >= 5, f"引用面只找到 {len(refs)} 个 .mjs —— 分母塌了"
    missing = {
        name: where for name, where in refs.items()
        if not os.path.exists(os.path.join(REPO, "frontend", "scripts", name))
    }
    assert not missing, (
        f"这些脚本被引用但不存在:{missing} —— 引用一道不存在的门,"
        f"等于对着自己承诺「漂了会红」")


def test_the_copy_verifier_is_wired_into_a_runnable_npm_script():
    """光有文件不算接线 —— ``package.json`` 里必须有人跑它。"""
    pkg = _read("frontend", "package.json")
    assert "verify-defgeo-copy-registry.mjs" in pkg, (
        "门存在但没接线 —— 不接线的锁等于没有(本仓自己的话)")


def test_the_copy_verifier_passes_on_a_clean_tree():
    """① 真跑一遍:干净树上 rc=0。"""
    rc, out = _run_copy_verifier()
    assert rc == 0, f"干净树上这道门就红了:\n{out}"


def test_the_copy_verifier_catches_a_single_byte_of_drift(tmp_path):
    """② 注毒:TS 生成物改一个字节 ⇒ rc≠0。

    🔴 改**副本**再指过去,绝不动工作树里那份 —— 变异残留穿合法 dirty
       衣服时,下一轮判据读的就是被污染的文件(本仓记过)。
    """
    # 🔴 先证明这道门在干净树上是**绿**的。少了这一步,"脚本根本不存在"
    #    (rc=127)会让下面那条 rc≠0 恒真 —— 非零同样可以只是"工具没跑"。
    clean_rc, clean_out = _run_copy_verifier()
    assert clean_rc == 0, (
        f"干净树上就非零(rc={clean_rc}),下面的注毒臂会因为错误的理由变绿:"
        f"\n{clean_out}")

    rel = os.path.join("frontend", "src", "lib", "defensiveGeoCopy.ts")
    original = io.open(os.path.join(REPO, rel), encoding="utf-8",
                       newline="").read()
    poisoned = original.replace("export const", "export  const", 1)
    assert poisoned != original, "注毒没生效 —— 下面那条会恒绿"
    victim = tmp_path / "defensiveGeoCopy.ts"
    with io.open(str(victim), "w", encoding="utf-8", newline="") as fh:
        fh.write(poisoned)

    rc, out = _run_copy_verifier(ts_override=str(victim))
    assert rc != 0, f"改了一个字节这道门还是绿的 —— 零判别力:\n{out}"


def _run_copy_verifier(ts_override: str | None = None, *,
                       no_python: bool = False, tmp_path=None):
    import shutil
    import subprocess

    script = os.path.join(REPO, "frontend", "scripts",
                          "verify-defgeo-copy-registry.mjs")
    if not os.path.exists(script):
        return (127, f"脚本不存在:{script}")
    node = shutil.which("node")
    if not node:
        # 🔴 不 skip:工具找不到时"跳过"与"通过"在报告里长得一模一样。
        raise AssertionError("找不到 node —— 这道门的判据没有被测对象,不许当成通过")
    argv = [node, script]
    if ts_override:
        argv += ["--ts", ts_override]
    env = dict(os.environ)
    if no_python:
        # 🔴 把 PATH 掐成一个空目录 ⇒ python3 / python / py 三个候选全 ENOENT,
        #    脚本走"找不到解释器"那一臂。node 用**绝对路径**起,所以它自己
        #    不受影响 —— 否则这一臂会变成"连 node 都没起来",测的就不是它了。
        env.pop("PYTHON", None)
        env["PATH"] = str(tmp_path)
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, env=env)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    return (proc.returncode, out)


# ── [工单 V5-C2 · Codex fix-of-fix3 P3-NEW-2] 失败臂不许把临时目录漏在盘上 ──
# `fail()` 里那句 `process.exit()` 在 `try` **内**执行 —— Node 的 `process.exit`
# **不跑 finally**,于是每走一次失败臂就在 os.tmpdir() 留一个 `defgeo-copy-*`。
# 这道门天生是"经常红"的(漂了就红),所以泄漏会**随着它尽职工作而累积**。
_TMP_GLOB = "defgeo-copy-*"


def _tmp_dirs() -> set:
    import glob
    import tempfile

    return set(glob.glob(os.path.join(tempfile.gettempdir(), _TMP_GLOB)))


def test_the_drift_arm_leaves_no_temp_directory_behind(tmp_path):
    """漂移臂(rc=1)跑完,``os.tmpdir()`` 里不许多出目录。

    🔴 两个断言缺一不可:先钉**这一臂真的走到了**(rc=1),再钉零泄漏。
       只钉零泄漏的话,"脚本压根没跑"同样零泄漏 —— 那是另一种恒绿。
    """
    rel = os.path.join("frontend", "src", "lib", "defensiveGeoCopy.ts")
    original = io.open(os.path.join(REPO, rel), encoding="utf-8",
                       newline="").read()
    victim = tmp_path / "defensiveGeoCopy.ts"
    with io.open(str(victim), "w", encoding="utf-8", newline="") as fh:
        fh.write(original.replace("export const", "export  const", 1))

    before = _tmp_dirs()
    rc, out = _run_copy_verifier(ts_override=str(victim))
    leaked = _tmp_dirs() - before

    assert rc == 1, f"漂移臂没走到(rc={rc}),零泄漏说明不了任何事:\n{out}"
    assert not leaked, (
        f"漂移臂在临时目录留下了 {sorted(os.path.basename(p) for p in leaked)}"
        f" —— `process.exit()` 绕过了 finally 的清理")


def test_the_missing_python_arm_leaves_no_temp_directory_behind(tmp_path):
    """找不到解释器那一臂(rc=2)同样不许漏。

    这一臂比漂移臂更该守:它在**没有 python 的机器**上每跑一次漏一个,
    而那正是这道门最可能被反复触发的环境。
    """
    empty = tmp_path / "no-python-here"
    empty.mkdir()

    before = _tmp_dirs()
    rc, out = _run_copy_verifier(no_python=True, tmp_path=empty)
    leaked = _tmp_dirs() - before

    assert rc == 2, (
        f"没走到「找不到解释器」那一臂(rc={rc}),零泄漏说明不了任何事。"
        f"这一臂靠把 PATH 掐空来构造,构造失败时这条判据必须红而不是绿:\n{out}")
    assert not leaked, (
        f"无 python 臂在临时目录留下了 "
        f"{sorted(os.path.basename(p) for p in leaked)}"
        f" —— `process.exit()` 绕过了 finally 的清理")


# ── [工单 V5-C2] 文案里的接线归属必须与 package.json 的**事实**一致 ──────────
_VERIFIER = "verify-defgeo-copy-registry.mjs"


def _verifier_gates() -> set:
    """package.json 里**真的**在跑这道门的 npm script 名。事实的唯一来源。"""
    import json

    scripts = json.loads(_read("frontend", "package.json"))["scripts"]
    return {name for name, cmd in scripts.items()
            if isinstance(cmd, str) and _VERIFIER in cmd}


def test_the_verifier_gates_are_exactly_the_wiring_we_claim():
    """接线**事实**锁:谁在跑这道门,由 package.json 说了算。

    🔴 集合式不是计数式:哪天真的加了一道闸,这条会红 —— 那正是要的,
       因为文案得跟着改。而 ``build`` 永远不该进来(frontend-builder 是
       node:20-alpine,没有 python,那一层也没有后端 registry)。
    """
    gates = _verifier_gates()
    assert "build" not in gates, (
        f"这道门被接进了 build:{sorted(gates)} —— 生产镜像构建会必然失败")
    assert gates == {"lint", "verify:defgeo-copy"}, (
        f"接线变了:{sorted(gates)}。改接线可以,但同笔要把三份文案"
        f"(生成器 / 生成物抬头 / 门自己的抬头)一起改过来")


def test_no_document_about_this_gate_claims_it_runs_in_the_build_chain():
    """这道门**自己那三份**文案里,不许出现肯定式的 build 链归属。

    🔴 分母只取"关于这道门"的三份文件,**不含 package.json** ——
       它整份文件里为**别的**脚本写着合法的「在 build 链里」,
       一刀切会把别人的正确文案判红。package.json 那一侧由上面那条
       事实锁守,比读散文强。

    🔴 否定式("不接 build 链" / "不进 npm run build")**必须放行**:
       它们正是修好之后该说的话。所以锚的是肯定式那两个词形。
    """
    banned = ("build 链内", "build 链里")
    hits = []
    for parts in ((("scripts", "defgeo_census", "emit_frontend_copy.py")),
                  (("frontend", "src", "lib", "defensiveGeoCopy.ts")),
                  (("frontend", "scripts", _VERIFIER))):
        text = _read(*parts)
        for tok in banned:
            if tok in text:
                hits.append(("/".join(parts), tok))
    assert not hits, (
        f"文案还写着这道门在 build 链里,而事实是 {sorted(_verifier_gates())}:"
        f"{hits}")

    # 判别力自证:同一把尺子必须抓到旧写法,且不误伤修好之后的否定式。
    assert any(t in "门:本文件(build 链内)" for t in banned)
    assert any(t in "在 build 链里重新导一次" for t in banned)
    assert not any(t in "**不接 build 链** —— frontend-builder 没有 python"
                   for t in banned)
    assert not any(t in "接在 npm run lint 前置 + 独立 npm run verify:defgeo-copy"
                   for t in banned)


# ══════════════════════════════════════════════════════════════════════════
# X-5 · [工单 V5-C · C-3 · Codex fix-of-fix2 P2-3] OpenAPI 不许承诺
#       已经 scoped-out 的东西
# ══════════════════════════════════════════════════════════════════════════
# 代码层的 ``SCOPED_OUT_KINDS`` 过滤有效、前端文案也已去承诺,而 FastAPI 的
# docstring 仍公开写着「四类 token 链接(报价 / 诊断报告 / 监测快照 / 周月报
# 门户)统一生成」。OpenAPI 是**对外**的:它是我们给出去的一份说明书。
# 说明书上写着的东西必须真的能拿到,否则就是承诺了一个不存在的能力。
def _defgeo_route_texts() -> list[tuple[str, str, str]]:
    """(path, method, 文案) —— 分母 = ``api/defensive_geo*_api.py`` 的每条路由。

    🔴 模块名用 glob 现取,不手抄:手抄的那份在新增一个 defgeo router 时
       不会红,而"新 router 里写了承诺"正是同一个洞的下一次发作。
    """
    import glob
    import importlib

    from fastapi import FastAPI

    mods = sorted(glob.glob(os.path.join(REPO, "api", "defensive_geo*_api.py")))
    assert len(mods) >= 2, f"只 glob 到 {mods} —— 分母塌了"
    app = FastAPI()
    for path in mods:
        name = "api." + os.path.basename(path)[:-3]
        app.include_router(importlib.import_module(name).router)
    spec = app.openapi()
    out: list[tuple[str, str, str]] = []
    for p, ops in spec["paths"].items():
        for method, op in ops.items():
            blob = " ".join(str(op.get(k) or "")
                            for k in ("summary", "description"))
            out.append((p, method, blob))
    assert out, "一条路由都没有 —— 分母塌了"
    return out


def test_openapi_never_mentions_a_scoped_out_link_kind():
    """scoped-out 的那几类,代号与中文名在**任何**路由文案里都不许出现。

    分母两侧都机械来源:kind 集合来自 ``customer_links.SCOPED_OUT_KINDS``,
    中文名来自 ``presentation.copy_registry`` 的 ``customer_link_kind`` 映射。
    """
    from services.defensive_geo import customer_links as cl
    from services.defensive_geo.presentation import copy_registry as pcopy

    kinds = sorted(cl.SCOPED_OUT_KINDS)
    assert kinds, "SCOPED_OUT_KINDS 为空 —— 本条恒真,得先确认这是有意的"
    banned: dict[str, str] = {}
    for kind in kinds:
        banned[kind] = kind
        banned[pcopy.translate("customer_link_kind", kind)] = kind

    hits = [(p, m, tok, banned[tok])
            for p, m, blob in _defgeo_route_texts()
            for tok in banned if tok in blob]
    assert not hits, (
        f"OpenAPI 仍在承诺已经 scoped-out 的链接类型:{hits}。"
        f"代码层藏起来、说明书上还写着 —— 对外看就是承诺了做不到的事")


def test_openapi_does_not_hard_code_how_many_link_kinds_there_are():
    """也不许写死「三类」——数量由常量决定,写死的那份下次照样烂。"""
    pat = re.compile(r"[一二三四五六七八九十两\d]+\s*类[^。;,]{0,6}(?:token|链接)")
    hits = [(p, m, pat.search(blob).group(0))
            for p, m, blob in _defgeo_route_texts() if pat.search(blob)]
    assert not hits, f"路由文案写死了链接类别数:{hits}"
    # 判别力自证:同一把尺子必须能抓到写死数量的句子,且不误伤「每一类」。
    assert pat.search("四类 token 链接(报价 / 诊断报告)统一生成")
    assert pat.search("三类链接统一生成")
    assert not pat.search("不是每一类都能重签(现役只有门户 token 有轮换原语)")
