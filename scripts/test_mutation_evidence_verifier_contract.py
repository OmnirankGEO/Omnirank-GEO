# -*- coding: utf-8 -*-
"""证据验收器的合同判据(V7-B 八臂 · **V8-B 封闭世界反例矩阵**)。

V7-B 那一版的病根:验收器**先 unlink 五份 SUMS 再重新生成**,然后宣布「非 OK 0 条」
—— 核验器改写被核验物 = 自问自答。

V8-B 关的是**拒绝能力**:Codex fof6 用六个洞协同伪造出 **12 个 rc=0**。
下面每一发都是那 12 个之一(或 V7-B 八臂的回归),**逐个必须转红**。

🔴 **这批臂的关键设计**(V7-B 立的,V8-B 继续):凡是改**内容**的臂都先
   `freeze_sums.py` 重冻一遍 SUMS(连根锚)再核 —— 模拟「动手的人顺便把
   SUMS 和根锚都刷了」。不这么做,每条臂都只撞在 SUMS 那一格上,
   实体重算 / 精确差集 / 树内合同这些深层检查**一条都没被驱动过**。

跑法::

    python -m pytest scripts/test_mutation_evidence_verifier_contract.py -q
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify_fof_evidence.py"
FREEZE = ROOT / "scripts" / "freeze_sums.py"

TIP = "a1b2c3d4" * 5
TREE = "b2c3d4e5" * 5
OLD_TIP = "f9e8d7c6" * 5

sys.path.insert(0, str(ROOT / "scripts"))

import mutation_replay_common as _RC          # noqa: E402


def _tree():
    """树内权威合同 —— 夹具必须按它造,否则造出来的包连合同都过不了。"""
    spec = importlib.util.spec_from_file_location(
        "_tvc_v2", ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py")
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    roster = m.load_v2()
    ids = sorted(x["id"] for x in roster)
    return m, ids, {i: tuple(m.QUICK_SCOPE[i]) for i in ids}, tuple(m.FULL)


_M, _IDS, _SCOPE, _PKGS = _tree()
#: 树内真 pairs —— 合法证据包必须冻结它,不能自己编一份。
_M_PAIRS = {m["id"]: m["pairs"] for m in _M.load_v2()}

_HEAD = '<?xml version="1.0" encoding="utf-8"?>\n'
_OK_CASE = '  <testcase classname="{cls}" name="{name}"/>\n'
_RED_CASE = ('  <testcase classname="{cls}" name="{name}">'
             '<failure message="boom">tb</failure></testcase>\n')


def _xml(path: pathlib.Path, cls: str, ok_n: int, red_names=(), *,
         hdr=None) -> None:
    """写一份 junit。`hdr` 给定时用它当 header(**用来造 header 撒谎的反例**)。"""
    cases = "".join(_OK_CASE.format(cls=cls, name=f"test_ok{i}") for i in range(ok_n))
    cases += "".join(_RED_CASE.format(cls=cls, name=n) for n in red_names)
    h = hdr or {"tests": ok_n + len(red_names), "failures": len(red_names),
                "errors": 0, "skipped": 0}
    path.write_text(
        _HEAD + '<testsuite name="pytest" tests="{tests}" failures="{failures}" '
        'errors="{errors}" skipped="{skipped}">\n{c}</testsuite>\n'.format(c=cases, **h),
        encoding="utf-8")


def _cls(target: str) -> str:
    """junit classname —— 最后一段进红集 nodeid,所以每个 target 要各不相同。"""
    return "tests." + target.replace("/", "_")


def _freeze(root: pathlib.Path, tip: str = TIP) -> None:
    r = subprocess.run([sys.executable, str(FREEZE), str(root),
                        "--tip", tip, "--tree", TREE],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"freeze_sums 失败:{r.stdout}{r.stderr}"


def _refreeze(root: pathlib.Path) -> None:
    """改完证据文件之后重冻 SUMS + 根锚 —— 否则红的会是「SUMS 对不上」,
    而我要考的是别的那一轴。判别力靠这一步保住。"""
    _freeze(root)


OKN, REDN = 9, 1                      # 每个 (发, target):9 绿 1 红


def build(root: pathlib.Path) -> pathlib.Path:
    """造一份**满足树内合同**的合法证据包:18 包 / 27 发 / 33 份 junit / 9 指纹。"""
    for sub in ("baseline17/junit", "baseline_host/junit", "shots27/junit",
                "shots27/junit_base", "arms", "harness", "findings"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    # ── 基线两段:段①容器 17 包 · 段②宿主 gate8 ──
    #    🔴 [V9-B] 运输分裂。合法证据包必须长成两段的样子,否则这个夹具
    #    供的是一种**生产不会再产出**的形状,那条闸的分母就是假的。
    HOSTP = list(_M._B.host_targets())
    CONTP = [p for p in _PKGS if p not in set(HOSTP)]
    _IDENT = {"tip": TIP, "tree": TREE, "python": "3.12.14",
              "platform": "Linux-x", "started_at": "2026-08-30T10:00:00Z",
              # 🔴 [fof8] 生产每段都绑 runner 版本;夹具漏了它,
              #    这个「合法证据包」就是生产不会产出的形状。
              "runner": {"pytest": "8.3.5", "pytest_asyncio": "0.25.3"}}

    def _seg(scope, pkgs, transport, started, image):
        sd = root / scope
        (sd / "junit").mkdir(parents=True, exist_ok=True)
        for pkg in pkgs:
            _xml(sd / "junit" / f"junit_{pkg.replace('/', '_')}.xml", _cls(pkg), 5)
        ident = dict(_IDENT, transport=transport, started_at=started,
                     image_id=image)
        (sd / "baseline.json").write_text(json.dumps({
            "identity": ident,
            "targets": list(_PKGS),
            "segment_targets": list(pkgs),
            "totals": {"collected": 5 * len(pkgs), "tests": 5 * len(pkgs),
                       "passed": 5 * len(pkgs), "red": 0, "skipped": 0,
                       "failures": 0, "errors": 0},
            "records": [{"target": pkg, "collected": 5, "tests": 5, "passed": 5,
                         "red": 0, "failures": 0, "errors": 0, "skipped": 0,
                         "skipped_nodes": [], "red_nodes": [], "rc": 0,
                         "collect_rc": 0} for pkg in pkgs],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        (sd / "base.log").write_text(f"起跑 {started}\n", encoding="utf-8")

    _seg("baseline17", CONTP, "container", "2026-08-30T10:00:00Z",
         "sha256:" + "d" * 64)
    _seg("baseline_host", HOSTP, "host", "2026-08-30T11:00:00Z", None)

    # ── 快集基线:每个被用到的 target 一份,零红 ──
    s = root / "shots27"
    for tgt in sorted({t for v in _SCOPE.values() for t in v}):
        _xml(s / "junit_base" / f"junit_v2base_{tgt.replace('/', '_')}.xml",
             _cls(tgt), OKN + REDN)

    # ── 27 发 ──
    recs = []
    for rid in _IDS:
        scope = _SCOPE[rid]
        meta, qred = {}, []
        for tgt in scope:
            # 🔴 [V9-B · P1-7] 红的那几条必须是**基线里也存在的那几条**:
            #    真实情况就是同一批用例跑两趟,其中一条从绿转红。
            #    造一个基线里根本没有的名字,等于夹具自己制造了「两趟收集到
            #    不同用例」这种病态 —— 那正是 P1-7 要抓的东西。
            rn = [f"test_ok{OKN + k}" for k in range(REDN)]
            _xml(s / "junit" / f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml",
                 _cls(tgt), OKN, rn)
            meta[tgt] = {"collected": OKN + REDN, "passed": OKN,
                         "red": REDN, "skipped": 0}
            qred += [f"{_cls(tgt).split('.')[-1]}::{n}" for n in rn]
        recs.append({"id": rid, "tip": TIP, "verdict": "KILLED",
                     "criteria_fp": _M.criteria_fp_of(scope),
                     # 🔴 [V9-B] 变异定义指纹取**树内真值**,不自己造一个:
                     #    这个包合成的是真 id 的整份产物,fp 也必须是那一发的真 fp,
                     #    否则夹具替被测代码把这道闸答了。
                     "mutation_fp": _M.mutation_fp_by_id()[rid],
                     # 🔴 [fof8 P1-1] 生产同时写指纹口径版本;夹具漏了它,
                     #    这个「合法证据包」就是一种生产不会产出的形状。
                     "mutation_fp_version": _M.FP_VERSION,
                     "quick_scope": list(scope), "run_meta": meta,
                     "quick_red": qred, "quick_green": OKN * len(scope),
                     # 🔴 [V9-B] 作用域绿两格:生产每条都由 `_verdict` 写,
                     #    钝杀分类按它们重算。基线零红 ⇒ 基线绿 == collected。
                     "scope_green": OKN * len(scope),
                     "scope_base_green": (OKN + REDN) * len(scope)})
    (s / "extsel_v2_results_final.json").write_text(
        json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    (s / "roster_ids.json").write_text(json.dumps(_IDS), encoding="utf-8")
    # 🔴 [V9-B · P1-8] 冻结的 pairs 用**树内真值**:合成值会让这个包
    #    在「内容级相等」那道闸下必红,而它本来是「合法证据包」的定义。
    (s / "roster_pairs.json").write_text(
        json.dumps(_M_PAIRS, ensure_ascii=False), encoding="utf-8")
    (s / "shots.log").write_text("27 发\n", encoding="utf-8")
    (root / "arms" / "a1.log").write_text("rc=0\n", encoding="utf-8")
    (root / "harness" / "g9.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (root / "findings" / "note.log").write_text("note\n", encoding="utf-8")
    _freeze(root)
    return root


def snap(root: pathlib.Path) -> dict:
    """独立快照 —— **不采信验收器自己说的「零写入」**(自报自证位不当判据)。"""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (
                st.st_size, st.st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest())
    return out



def _reason(out: str, tail: int = 800) -> str:
    """报文必须带上**失败原因** —— 有 `XX` 行就先摘全部,再附尾部;没有就退回纯尾部。

    🔴 [2026-09-04 · #50] 为什么必须这样:验收器把每条失败打成 `XX ...`,
       而它们出现在输出的**最开头**(第 0 项就是「树内权威合同」)。
       原来的 `out[-800:]` 只切尾巴,**恰好把原因切掉**。
       2026-09-04 我因此误判「6 条 evidence_verifier 红根因各不相同」——
       grep「权威合同变了」在 5 条里 0 命中,不是根因不同,是报文没带上它。

       这跟同日 `test_ea_12` 的**裸断言**是同一类病:
       判据知道原因,却没把原因带到报文里。一个完全不说,一个只说末尾。

    退回纯尾部这一支是**故意**的:对「原因本来就在末尾」的输出(子进程 traceback、
    pytest summary),尾部才是该看的地方。本 helper 因此向后兼容,
    不会把那类报文改哑 —— 全仓另有 47 处尾截断是**对的**,不在本轮范围。
    """
    marks = [l for l in out.splitlines() if l.lstrip().startswith("XX")]
    if not marks:
        return out[-tail:]
    return ("【失败项 " + str(len(marks)) + " 条】\n" + "\n".join(marks)
            + "\n【输出尾 " + str(tail) + " 字符】\n" + out[-tail:])

def verify(root: pathlib.Path, tip: str = TIP, tree: str = TREE,
           manifest_sha: str | None = None) -> tuple[int, str, bool]:
    argv = [sys.executable, str(VERIFY), str(root), "--tip", tip, "--tree", tree]
    if manifest_sha is not None:
        argv += ["--manifest-sha256", manifest_sha]
    before = snap(root)
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, r.stdout + r.stderr, snap(root) == before


def _load(root, rel):
    return json.loads((root / rel).read_text(encoding="utf-8"))


def _save(root, rel, obj):
    (root / rel).write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                            encoding="utf-8")


@pytest.fixture()
def pkg(tmp_path):
    return build(tmp_path / "ev")


# ══ 阴性对照:合法包必须 rc=0(否则下面所有红都证明不了是伪造造成的)════════
def test_ev_00_a_clean_package_passes(pkg):
    rc, out, nowrite = verify(pkg)
    assert rc == 0, f"合法包被判红:\n{out}"
    assert nowrite, "验收器动了证据"


# ══════════════════════════════════════════════════════════════════════════
# V8-B · Codex 的 12 个 rc=0,逐个转红
# ══════════════════════════════════════════════════════════════════════════
def test_ev_c01_zero_by_zero_denominator(pkg):
    """① 0/0 分母:产物清空 + roster 清空 + XML 全删,「0==0==0」当成功。"""
    _save(pkg, "shots27/extsel_v2_results_final.json", [])
    _save(pkg, "shots27/roster_ids.json", [])
    for p in (pkg / "shots27/junit").glob("*.xml"):
        p.unlink()
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "空的" in out, _reason(out, 600)
    assert nw


def test_ev_c02_header_and_json_collude(pkg):
    """② header + JSON 协同:header 说 12 条、实体只有 10 条,run_meta 跟着改。

    🔴 这一发是整批最要紧的:上一版按 header 累加,协同改完**完全自洽**。
    只有以 testcase 实体为 SSOT 才看得出 header 在撒谎。
    """
    rid = _IDS[0]
    tgt = _SCOPE[rid][0]
    f = pkg / "shots27/junit" / f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml"
    _xml(f, _cls(tgt), OKN, [f"r_{rid}_{tgt.replace('/', '_')}_0"],
         hdr={"tests": 12, "failures": 1, "errors": 0, "skipped": 0})
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[0]["run_meta"][tgt]["collected"] = 12
    recs[0]["run_meta"][tgt]["passed"] = 11
    recs[0]["quick_green"] = recs[0]["quick_green"] + 2
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "header 与实体重算不符" in out, _reason(out, 800)
    assert nw


def test_ev_c03_nested_testsuites_aggregation(pkg):
    """③ 嵌套 testsuites 聚合 —— 计数被重复累加。"""
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    f = pkg / "shots27/junit" / f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml"
    f.write_text(_HEAD + "<testsuites><testsuites>"
                 '<testsuite name="p" tests="10" failures="1" errors="0" skipped="0">'
                 f'<testcase classname="{_cls(tgt)}" name="x">'
                 "<failure message=\"b\">t</failure></testcase>"
                 "</testsuite></testsuites></testsuites>", encoding="utf-8")
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "非根" in out, _reason(out, 600)
    assert nw


def test_ev_c04_passed_changed_to_seven(pkg):
    """④ run_meta.passed 改成 7 —— JSON 内部仍加得平,XML 实体说 9。"""
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[0]["run_meta"][tgt]["passed"] = 7
    recs[0]["run_meta"][tgt]["collected"] = 8
    recs[0]["quick_green"] = 7 + OKN * (len(_SCOPE[rid]) - 1)
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "XML 实体 tests=" in out, _reason(out, 800)
    assert nw


def test_ev_c05_quick_green_999(pkg):
    """⑤ quick_green 改 999 —— 语义谓词那一格(绿数与 passed 总数不符)。"""
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[0]["quick_green"] = 999
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "quick_green=999" in out, _reason(out, 600)
    assert nw


def test_ev_c06_empty_roster_pairs(pkg):
    """⑥ roster_pairs 清空 —— 空 roster 短路那一格。"""
    _save(pkg, "shots27/roster_pairs.json", {})
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "roster_pairs.json 是空的" in out, _reason(out, 600)
    assert nw


def test_ev_c07_a_fourth_red_nobody_claims(pkg):
    """⑦ XML 里多一条红,`quick_red` 不认领 —— 子集判定看不见,精确相等看得见。"""
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    f = pkg / "shots27/junit" / f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml"
    base = f"r_{rid}_{tgt.replace('/', '_')}_0"
    _xml(f, _cls(tgt), OKN - 1, [base, "sneaky_extra_red"])
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[0]["run_meta"][tgt] = {"collected": OKN + 1, "passed": OKN - 1,
                                "red": 2, "skipped": 0}
    recs[0]["quick_green"] = (OKN - 1) + OKN * (len(_SCOPE[rid]) - 1)
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0, _reason(out, 800)
    assert "不精确相等" in out or "!= len(quick_red)" in out, _reason(out, 800)
    assert nw


def test_ev_c08_empty_manifest(pkg):
    """⑧ 根锚清空 —— 没有根锚就只剩「重冻自洽」,原真无从谈起。"""
    (pkg / "MANIFEST.json").write_text("{}", encoding="utf-8")
    rc, out, nw = verify(pkg)
    assert rc != 0 and "根锚" in out, _reason(out, 600)
    assert nw


def test_ev_c09_one_scope_too_many(pkg):
    """⑨ 根下多出一个 scope —— 封闭世界那一格。

    🔴 名字与报文都不写死「第几个」:V9-B 运输分裂把 scope 从 5 加到 6 之后,
    写死数字的那一版当场假红(**计数式判据会过期**,本轮第二次踩)。
    分母改从 `EXPECT_SCOPES` 现取。
    """
    (pkg / "extra_scope").mkdir()
    (pkg / "extra_scope" / "x.log").write_text("x\n", encoding="utf-8")
    rc, out, nw = verify(pkg)
    assert rc != 0, _reason(out, 600)
    assert f"根下目录不是恰好那 {len(_VM.EXPECT_SCOPES)} 个" in out, _reason(out, 600)
    assert nw


def test_ev_c10_still_red_after_refreezing(pkg):
    """⑩ 改内容 + **重冻 SUMS 与根锚** ⇒ 仍然红。

    这一发直接回答「重冻能不能洗白」:不能 —— SUMS 与根锚只证自洽,
    实体重算 / 精确差集 / 树内合同这三层不吃这一套。
    """
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[0]["run_meta"][tgt]["red"] = 0
    recs[0]["run_meta"][tgt]["passed"] = OKN + 1
    recs[0]["quick_red"] = [q for q in recs[0]["quick_red"]
                            if tgt.replace("/", "_") not in q]
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)                      # 顺手把 SUMS 和根锚都刷了
    rc, out, nw = verify(pkg)
    assert rc != 0, "重冻之后就洗白了 —— 那三层没起作用\n" + _reason(out, 800)
    assert "内容对不上" not in out, "这一臂不该靠 SUMS 抓 —— 它已被重冻"
    assert nw


def test_ev_c11_a_junction_at_the_root(tmp_path):
    """⑪ 根下挂一个 junction —— 内容对得上也不算,它指向的东西不在这份证据里。"""
    real = build(tmp_path / "ev")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.log").write_text("x\n", encoding="utf-8")
    link = real / "findings" / "linked"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"这台机器建不了 junction(mklink rc={r.returncode}):"
                    f"{(r.stdout + r.stderr).strip()[:120]} —— 这一臂本轮无从验证")
    rc, out, nw = verify(real)
    assert rc != 0 and "重解析点" in out, _reason(out, 600)


def test_ev_c12_a_shrunk_baseline_denominator(pkg):
    """⑫ 缩分母:基线少冻一个包的 junit —— 「17 == 17」自洽,但树内合同是 18。"""
    victim = sorted((pkg / "baseline17/junit").glob("*.xml"))[0]
    victim.unlink()
    b = _load(pkg, "baseline17/baseline.json")
    b["targets"] = [t for t in b["targets"]
                    if victim.name != f"junit_{t.replace('/', '_')}.xml"]
    b["totals"] = {**b["totals"], "collected": 5 * 17, "tests": 5 * 17,
                   "passed": 5 * 17}
    _save(pkg, "baseline17/baseline.json", b)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and ("树内包集不符" in out or "!= 树内" in out), _reason(out, 800)
    assert nw


# ══════════════════════════════════════════════════════════════════════════
# V7-B 八臂回归 —— 不许退化
# ══════════════════════════════════════════════════════════════════════════
def test_ev_r01_a_tampered_harness_file_is_caught(pkg):
    (pkg / "harness" / "g9.sh").write_text("#!/bin/sh\necho PWNED\n", encoding="utf-8")
    rc, out, nw = verify(pkg)
    assert rc != 0 and "内容对不上" in out, _reason(out, 500)
    assert nw


def test_ev_r02_a_single_json_field_change_is_caught(pkg):
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[0]["run_meta"][tgt]["collected"] += 1
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "内容对不上" not in out, _reason(out, 500)
    assert nw


def test_ev_r03_a_deleted_xml_is_caught(pkg):
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    (pkg / "shots27/junit" / f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml").unlink()
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "缺原始 junit" in out, _reason(out, 500)
    assert nw


def test_ev_r04_an_unclaimed_extra_xml_is_caught(pkg):
    _xml(pkg / "shots27/junit" / "junit_EXTE9-99_tests_z.xml", "tests.z", 1)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "没有任何记录认领" in out, _reason(out, 500)
    assert nw


def test_ev_r05_an_extra_unlisted_file_is_caught(pkg):
    (pkg / "arms" / "sneaky.log").write_text("x\n", encoding="utf-8")
    rc, out, nw = verify(pkg)
    assert rc != 0 and "SUMS 没列" in out, _reason(out, 500)
    assert nw


def test_ev_r06_a_record_from_an_older_tip_is_caught(pkg):
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[0]["tip"] = OLD_TIP
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "没绑本轮尖" in out, _reason(out, 500)
    assert nw


def test_ev_r07_a_duplicate_record_is_caught(pkg):
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    recs[1] = dict(recs[0])
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "重复 id" in out, _reason(out, 500)
    assert nw


# ══ 结构锁:验收器不许写、不许调生成器 ═══════════════════════════════════
def test_ev_r08_the_verifier_never_writes_and_never_calls_the_freezer():
    """🔴 第一版这条是**裸串锁**,被验收器自己的文档字符串触发
    (它在抬头解释「生成拆到 freeze_sums.py」)—— 散文触发裸串锁,本仓第三次。
    改成只看 **AST 里真会执行的东西**。"""
    tree = ast.parse(VERIFY.read_text(encoding="utf-8"))
    imported = {(n.module or "") for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                 for a in n.names}
    assert not any("freeze" in m for m in imported), f"import 了生成器:{sorted(imported)}"

    # `replace` 故意不在名单:`Path.replace` 是写,`str.replace` 无害,
    # AST 分不出接收者类型。这一格有行为同伴 —— 每条臂都由 snap() 独立量过。
    WRITERS = {"write_text", "write_bytes", "unlink", "mkdir", "touch", "rmdir",
               "remove", "rename", "copy", "copy2", "copytree", "rmtree",
               "makedirs", "chmod", "symlink_to", "hardlink_to"}
    hits = [f"{n.func.attr}() @ 第 {n.lineno} 行" for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in WRITERS]
    assert not hits, "验收器里有写操作:" + " · ".join(hits)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "open")
                or (isinstance(n.func, ast.Name) and n.func.id == "open")):
            modes = [x.value for x in n.args if isinstance(x, ast.Constant)]
            modes += [k.value.value for k in n.keywords
                      if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            assert modes, f"第 {n.lineno} 行 open() 没写模式"
            for mo in modes:
                assert isinstance(mo, str) and mo.startswith("r") and "+" not in mo, \
                    f"第 {n.lineno} 行 open 模式不是只读:{mo!r}"

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in {"run", "Popen", "call", "check_output"} \
                and getattr(n.func.value, "id", "") == "subprocess":
            raise AssertionError(f"第 {n.lineno} 行起了子进程 —— 写操作外包一样是写")


def test_ev_r09_the_authoritative_contract_does_not_come_from_the_evidence(pkg):
    """🔴 权威分母必须来自**树**,不是证据目录。

    做法:把证据里的 roster 换成一份**自洽但更小**的(只留 3 发,产物也只留 3 条)。
    若分母来自证据,这份包自洽 ⇒ 会绿;来自树 ⇒ 立刻红。
    """
    keep = _IDS[:3]
    recs = [r for r in _load(pkg, "shots27/extsel_v2_results_final.json")
            if r["id"] in keep]
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _save(pkg, "shots27/roster_ids.json", keep)
    _save(pkg, "shots27/roster_pairs.json", {i: [{"from": "x", "to": "y"}] for i in keep})
    for p in (pkg / "shots27/junit").glob("*.xml"):
        if not any(p.name.startswith(f"junit_{i[4:]}_") for i in keep):
            p.unlink()
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "树内" in out, _reason(out, 800)
    assert nw


# ══════════════════════════════════════════════════════════════════════════
# V8-B 补:三格「门在、线接了、没人驱动」—— 两格 Review 亲毒挖出,一格我自打的
# ══════════════════════════════════════════════════════════════════════════
#
# 覆盖边界如实记:首报那 22 臂**没有**盖到下面这三格。
#   ① 树内合同漂移(18/27/33/9 与推导不符)—— r09 打的是「分母不从证据取」,方向不同
#   ② 逐发 criteria_fp 与树内不符      —— 12 臂里没有任何一臂改单发 fp
#   ③ 产物 id 集与树内 roster 不符      —— r09 是靠**另一条**带「树内」字样的检查过的
#      (冻结 roster 交叉核对),两把锁叠同一路径 ⇒ 中和其一照样全绿
#
# ①③ 的行为臂做不到隔离(少一条记录必然连带 junit 认领数也不对),
# 所以抽纯裁决 + 逐路正样本 + 数据流锁;② 能隔离,用行为臂。

def _V():
    spec = importlib.util.spec_from_file_location("_vfe_probe", VERIFY)
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m


_VM = _V()
#: 🔴 [2026-09-04 · #40] packages 18 → 23,与 `verify_fof_evidence.CONTRACT` 同批。
#:    `test_ev_p24a` 守这两份**逐字相等** —— 我先只改了工具那份,p24a 当场红。
#:    同一谓词写两处,必有一处会被漏掉;这条锁存在的理由就是接住那一处。
_WANT = {"packages": 23, "ids": 27, "junit": 33, "fps": 9}


@pytest.mark.parametrize("key", sorted(_WANT))
def test_ev_p01_every_contract_dimension_alone_drives_a_mismatch(key):
    """🔴 纯样本:四路**各自单独**驱动合同不符。

    抓的毒:「裁决函数无视某一维」—— 四维写在一个推导式里,漏掉一维
    不改任何字符串、不动任何位置,只有逐维正样本抓得住。
    """
    assert _VM.contract_mismatches(dict(_WANT), _WANT) == [], "全相等时不该报"
    got = dict(_WANT)
    got[key] = _WANT[key] + 1
    out = _VM.contract_mismatches(got, _WANT)
    assert len(out) == 1 and out[0].startswith(key), f"{key} 单独一路没驱动:{out}"


def test_ev_p02_a_missing_contract_key_is_a_mismatch():
    """缺键 ⇒ `got.get(k)` 是 None ⇒ 必须报,不许当成「没这一维就不查」。"""
    got = {k: v for k, v in _WANT.items() if k != "junit"}
    assert any(o.startswith("junit") for o in _VM.contract_mismatches(got, _WANT))


@pytest.mark.parametrize("shape", ["empty", "dup", "missing", "extra"])
def test_ev_p03_every_record_set_shape_alone_drives_a_mismatch(shape):
    """🔴 纯样本:产物 id 集的四种坏形状各自单独驱动。"""
    roster = ["A", "B", "C"]
    ids = {"empty": [], "dup": ["A", "A", "B", "C"],
           "missing": ["A", "B"], "extra": ["A", "B", "C", "Z"]}[shape]
    assert _VM.record_set_mismatch(roster, roster) == [], "全相等时不该报"
    out = _VM.record_set_mismatch(ids, roster)
    assert out, f"{shape} 没驱动任何一条"
    needle = {"empty": "为空", "dup": "重复 id",
              "missing": "两向不空", "extra": "两向不空"}[shape]
    assert any(needle in o for o in out), f"{shape} 的报文没点名规则:{out}"


def test_ev_p04_the_two_verdicts_are_wired_into_fail():
    """🔴 数据流锁:两个纯裁决的**返回值**必须驱动 `fail`,不许只是算完丢掉。

    抓的毒:「把 for 换成 `for d in []`」/「算了不用」—— 那样纯样本还绿,
    而验收器已经不看它了。
    """
    tree = ast.parse(VERIFY.read_text(encoding="utf-8"))
    main = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    for fname in ("contract_mismatches", "record_set_mismatch"):
        # 🔴 [自打 T2 存活后收紧] 第一版写的是「迭代表达式里**出现过**这次调用」,
        #    于是 `for d in [] and f(...)` 短路成 `[]` 之后 —— 调用仍在 AST 里 ——
        #    这把锁照样绿,而验收器已经不看它了。实测存活。
        #    「出现过」和「就是」差一个字,差的是整条接线。
        loops = [n for n in ast.walk(main) if isinstance(n, ast.For)
                 and isinstance(n.iter, ast.Call)
                 and isinstance(n.iter.func, ast.Name)
                 and n.iter.func.id == fname]
        assert loops, (
            f"main 里没有一处 `for … in {fname}(...)`:迭代器必须**就是**那次调用,"
            "不许包在 BoolOp / IfExp / 下标之类能短路或改写它的表达式里")
        for lp in loops:
            calls = [c for c in ast.walk(lp) if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Name) and c.func.id == "fail"]
            assert calls, f"迭代 {fname} 的那个循环体里没有 fail(...)"
        # 结果不许被丢进一个变量后再也不用
        assert not [n for n in ast.walk(main) if isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Name)
                    and n.value.func.id == fname], \
            f"{fname} 被当成语句调用了(算完丢掉)"


def test_ev_p05_a_swapped_criteria_fp_is_caught(pkg):
    """🔴 [Review 亲毒 ②] 行为臂 —— **把两发 scope 不同的记录的 fp 对调**。

    这样指纹**集合不变**(仍 9 种),只有「逐发 fp 与树内算出的不符」那一格错 ——
    才隔离得出它。首报 12 臂里没有任何一臂改单发 fp。
    """
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    by = {r["id"]: r for r in recs}
    a = b = None
    for i in _IDS:
        for j in _IDS:
            if i != j and set(_SCOPE[i]) != set(_SCOPE[j]):
                a, b = i, j
                break
        if a:
            break
    assert a and b, "找不到两发 scope 不同的记录 —— 这条臂的前提不成立"
    before = {r["criteria_fp"] for r in recs}
    by[a]["criteria_fp"], by[b]["criteria_fp"] = by[b]["criteria_fp"], by[a]["criteria_fp"]
    assert {r["criteria_fp"] for r in recs} == before, "对调之后指纹集合应当不变"
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0, "对调 fp 没被抓到\n" + _reason(out, 800)
    assert "criteria_fp 与树内算出的不符" in out, _reason(out, 800)
    assert "指纹集与树内" not in out, "集合那一格不该响 —— 这一臂要隔离的是逐发那格"
    assert nw


def test_ev_p06_a_collapsed_fingerprint_set_is_caught(pkg):
    """把所有发的 fp 塌成同一个 —— 驱动「指纹集与树内 9 种不符」那一格。"""
    recs = _load(pkg, "shots27/extsel_v2_results_final.json")
    one = recs[0]["criteria_fp"]
    for r in recs:
        r["criteria_fp"] = one
    _save(pkg, "shots27/extsel_v2_results_final.json", recs)
    _freeze(pkg)
    rc, out, nw = verify(pkg)
    assert rc != 0 and "指纹集与树内" in out, _reason(out, 800)
    assert nw


def test_ev_p07_the_wiring_lock_rejects_every_wrapping_form():
    """🔴 把 p04 的判定**本身**当被测物:喂合成源码,十一种包装形只许原样通过。

    由来:p04 第一版写的是「迭代表达式里**出现过**那次调用」(`ast.walk` 是包含关系),
    自打毒 `for d in [] and f(...)` **存活** —— 短路成 `[]` 之后调用还在 AST 里。
    收紧成同一性判定之后,不能只靠「我这次想到了短路和三元」——
    把边界钉在判据里,以后谁换个包装形都由它来回答。

    这是同一族病的第三层:①谓词存在≠有牙 ②有牙≠进终态 **③接线锁存在≠有牙**。
    """
    def wired(src: str, fname: str = "contract_mismatches") -> bool:
        main = next(n for n in ast.parse(src).body
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        return bool([n for n in ast.walk(main) if isinstance(n, ast.For)
                     and isinstance(n.iter, ast.Call)
                     and isinstance(n.iter.func, ast.Name)
                     and n.iter.func.id == fname])

    FORMS = [
        ("原样", "for d in contract_mismatches(a, b):", True),
        ("短路 [] and f", "for d in [] and contract_mismatches(a, b):", False),
        ("短路 f and []", "for d in contract_mismatches(a, b) and []:", False),
        ("三元 [] if T else f", "for d in ([] if True else contract_mismatches(a, b)):", False),
        ("三元 f if F else []", "for d in (contract_mismatches(a, b) if False else []):", False),
        ("下标 f()[:0]", "for d in contract_mismatches(a, b)[:0]:", False),
        ("下标 f()[0:0]", "for d in contract_mismatches(a, b)[0:0]:", False),
        ("包一层 list(f())[:0]", "for d in list(contract_mismatches(a, b))[:0]:", False),
        ("or 兜底 f() or []", "for d in (contract_mismatches(a, b) or []):", False),
        ("整个换掉", "for d in []:", False),
        ("算了丢掉", "contract_mismatches(a, b)\n    for d in []:", False),
    ]
    bad = []
    for tag, body, want in FORMS:
        src = f"def main():\n    {body}\n        fail(d)\n    return 0\n"
        if wired(src) != want:
            bad.append(f"{tag}(判成 {'通过' if not want else '拒绝'})")
    assert not bad, "这些包装形被判反了:" + " · ".join(bad)

    # 这套判定必须**就是** p04 用的那套 —— 不许两处各写一版然后各自漂
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef)
              and n.name == "test_ev_p04_the_two_verdicts_are_wired_into_fail")
    seg = ast.get_source_segment(src, fn) or ""
    for needle in ("isinstance(n.iter, ast.Call)", "n.iter.func.id == fname"):
        assert needle in seg, f"p04 里没有同一性判定 `{needle}` —— 两处判定漂了"


# ══ P1-5 根锚 tree + 台账 sha ═════════════════════════════════════════════
def test_ev_t01_a_manifest_tree_that_disagrees_is_caught(tmp_path):
    """🔴 正样本:根锚里的 `tree` 与本轮树不符 ⇒ 必须红。

    修之前 `MANIFEST.json` 的 `tree` 是**只写不读**的:freeze 老实写进去,
    验收器一眼没看过。只写不读最坏的不是「没用」,是它**看起来像证据** ——
    发车台账上「产物绑树」那一格靠的正是这个从没被核过的值。
    """
    root = build(tmp_path)
    rc, out, ro = verify(root, tree="9" * 40)
    assert rc != 0 and ro
    assert "根锚 tree=" in out, _reason(out, 600)


def test_ev_t02_a_baseline_tree_that_disagrees_is_caught(tmp_path):
    """三向里的第三向:基线 identity 的 tree 被换掉。"""
    root = build(tmp_path)
    bj = root / "baseline17" / "baseline.json"
    b = json.loads(bj.read_text(encoding="utf-8"))
    b["identity"]["tree"] = "0" * 40
    bj.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "三向树对不上" in out, _reason(out, 600)


@pytest.mark.parametrize("where", ["manifest", "baseline"])
def test_ev_t03_a_missing_tree_is_not_a_pass(where, tmp_path):
    """🔴 缺失一律算不符 —— 「没有树就不比」等于把闸对最该管的产物关掉。"""
    root = build(tmp_path)
    if where == "baseline":
        bj = root / "baseline17" / "baseline.json"
        b = json.loads(bj.read_text(encoding="utf-8"))
        b["identity"].pop("tree")
        bj.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        _refreeze(root)
    else:
        mf = root / "MANIFEST.json"
        man = json.loads(mf.read_text(encoding="utf-8"))
        man.pop("tree")
        mf.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    rc, out, ro = verify(root)
    assert rc != 0 and ro


def test_ev_t04_the_ledger_sha_is_compared_before_the_payload_is_read(tmp_path):
    """🔴 台账登记的根锚自身 sha 传进来 ⇒ 对得上放行、对不上必红。

    顺序也钉住:比 sha 在读 payload **之前**。反过来等于
    「先信了它说的话,再问它是不是本人」。
    """
    root = build(tmp_path)
    real = hashlib.sha256((root / "MANIFEST.json").read_bytes()).hexdigest()
    rc, out, ro = verify(root, manifest_sha=real)
    assert rc == 0 and ro, _reason(out, 800)
    rc2, out2, ro2 = verify(root, manifest_sha="0" * 64)
    assert rc2 != 0 and ro2
    assert "不是台账里那一份" in out2, _reason(out2, 600)
    # 顺序:sha 不符时,后面那些读 payload 才会报的错**一条都不许出现**
    assert "根锚 tip=" not in out2 and "根锚覆盖的 scope 不对" not in out2


def test_ev_t05_the_three_way_is_a_pure_decision():
    """纯判定单点:三向逻辑必须能单独喂反例,不许长在 IO 里。"""
    assert _VM.tree_three_way("a", "a", "a") == []
    assert _VM.tree_three_way("a", "b", "a")
    assert _VM.tree_three_way("a", None, "a")
    assert _VM.tree_three_way("", "", "")


# ══ P1-6 逐包守恒 ═════════════════════════════════════════════════════════
def _brec(**kw):
    r = {"target": "tests/x", "collected": 5, "tests": 5, "passed": 5, "red": 0,
         "skipped": 0, "skipped_nodes": []}
    r.update(kw)
    return r


def _bj(**kw):
    j = {"tests": 5, "failures": 0, "errors": 0, "skipped": 0,
         "skipped_nodes": set(), "red_nodes": set(), "node_ids": set()}
    j.update(kw)
    return j


def test_ev_k01_a_clean_package_conserves():
    """阴性对照 —— 干净的一包必须零抱怨,否则下面每条红都说明不了问题。"""
    assert _VM.conservation_mismatches(_brec(), _bj(), frozenset()) == []


@pytest.mark.parametrize("name,rec,j,needle", [
    ("没产物", _brec(), None, "没有可用的原始 junit"),
    ("收集到 0", _brec(collected=0, tests=0, passed=0), _bj(tests=0),
     "收集到 0 条不算全绿"),
    ("collected != tests", _brec(collected=6), _bj(), "collected=6 != tests=5"),
    ("记录与 XML 跑数不符", _brec(), _bj(tests=6), "!= XML 实体 6"),
    # 记录说有 1 条红,XML 实体里一条都没有 —— 我第一版把两边写成一致的,
    # 那条反例当场零抱怨:**反例自己不成立**,这一格等于没测。
    ("红口径不符", _brec(red=1, passed=4), _bj(),
     "记录 red=1 != XML 实体红 0"),
    ("passed 不守恒", _brec(passed=4), _bj(), "passed=4 !="),
    ("skip 只记数字", _brec(skipped=1, passed=4), _bj(skipped=1),
     "只记数字的话白名单无从比对"),
    ("坏型", _brec(passed="5"), _bj(), "不是 int"),
])
def test_ev_k02_each_conservation_rule_names_itself(name, rec, j, needle):
    """🔴 每条守恒规则各自点名 —— 只断言「有抱怨」的话,把加法检查换成
    类型检查也照样绿,而两者抓的是不同的伪造。"""
    out = _VM.conservation_mismatches(rec, j, frozenset())
    assert any(needle in x for x in out), f"{name}: {out}"


def test_ev_k03_a_skip_outside_the_whitelist_is_named():
    rec = _brec(skipped=1, passed=4, skipped_nodes=["tests/x/test_a.py::test_z"])
    j = _bj(skipped=1, skipped_nodes={"test_a::test_z"})
    assert _VM.conservation_mismatches(rec, j, frozenset()) == [
        "tests/x: 白名单之外的 skip:['tests/x/test_a.py::test_z']"]
    ok = _VM.conservation_mismatches(
        rec, j, frozenset({"tests/x/test_a.py::test_z"}))
    assert ok == [], ok


def test_ev_k04_the_two_nodeid_spaces_are_normalised_not_compared_raw():
    """🔴 归一化必须是个**有名字的函数**。

    记录侧是完整 pytest nodeid、XML 侧是模块短式 —— 两个 id 空间。
    直接对比这次会红(好),但同一个错误反过来就是最危险的形态:
    两边都空 ⇒ 集合相等 ⇒ 一路绿。
    """
    assert _VM.short_nodeid("tests/x/test_a.py::test_z") == "test_a::test_z"
    assert _VM.short_nodeid("test_a::test_z") == "test_a::test_z"
    assert _VM.short_nodeid("没有分隔符") == "没有分隔符"


def test_ev_k05_a_truncated_baseline_package_is_caught(tmp_path):
    """🔴 端到端接线臂:某个包 collected 0 —— `totals.red == 0` 纹丝不动。

    这就是「零红在悄悄变小的分母上永远成立」的具体形态。
    """
    root = build(tmp_path)
    pkg = _PKGS[0]
    _xml(root / "baseline17" / "junit" / f"junit_{pkg.replace('/', '_')}.xml",
         _cls(pkg), 0)
    bj = root / "baseline17" / "baseline.json"
    b = json.loads(bj.read_text(encoding="utf-8"))
    for r in b["records"]:
        if r["target"] == pkg:
            r.update(collected=0, tests=0, passed=0)
    b["totals"]["red"] = 0            # 那个数照样是 0
    bj.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "收集到 0 条不算全绿" in out, _reason(out, 700)


def test_ev_k06_a_duplicate_target_in_the_records_is_caught(tmp_path):
    root = build(tmp_path)
    bj = root / "baseline17" / "baseline.json"
    b = json.loads(bj.read_text(encoding="utf-8"))
    b["records"].append(dict(b["records"][0]))
    bj.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "重复 target" in out, _reason(out, 700)


# ══ P1-7 身份集 ═══════════════════════════════════════════════════════════
def test_ev_k07_identity_sets_must_match_per_target():
    # 🔴 [fof8 P1-2] 跨趟比较改吃**无损身份**(node_ids / skipped_ids),
    #    夹具跟着改 —— 还喂 skipped_nodes 的话它验的是一种不存在的调用形状。
    j = {"node_ids": {"m::a", "m::b"}, "skipped_ids": set()}
    b = {"node_ids": {"m::a", "m::b"}, "skipped_ids": set()}
    assert _VM.identity_mismatches("M", "t", j, b) == []
    j2 = {"node_ids": {"m::a"}, "skipped_ids": set()}
    out = _VM.identity_mismatches("M", "t", j2, b)
    assert out and "用例身份集不同" in out[0], out


def test_ev_k08_an_empty_baseline_is_not_a_pass():
    """空分母不是通过 —— 基线一条用例都没有时,差集恒等于变异侧那一坨。"""
    out = _VM.identity_mismatches("M", "t", {"node_ids": {"m::a"},
                                             "skipped_ids": set()},
                                  {"node_ids": set(), "skipped_ids": set()})
    assert out and "空分母不是通过" in out[0], out


def test_ev_k09_a_mutation_that_skips_the_killer_is_caught():
    """🔴 变异把判据 skip 掉 ⇒ 「没红」—— 那不是存活,是没跑。"""
    b = {"node_ids": {"m::a"}, "skipped_ids": set()}
    j = {"node_ids": {"m::a"}, "skipped_ids": {"m::a"}}
    out = _VM.identity_mismatches("M", "t", j, b)
    assert out and "skip 集不同" in out[0], out


def test_ev_k10_a_dropped_testcase_is_caught_end_to_end(tmp_path):
    """端到端:变异那趟少收集一条(计数自洽,身份集不同)。"""
    root = build(tmp_path)
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    f = root / "shots27" / "junit" / f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml"
    _xml(f, _cls(tgt), OKN - 1, [f"test_ok{OKN}"])
    res = root / "shots27" / "extsel_v2_results_final.json"
    recs = json.loads(res.read_text(encoding="utf-8"))
    for r in recs:
        if r["id"] == rid:
            r["run_meta"][tgt]["collected"] = OKN
            r["run_meta"][tgt]["passed"] = OKN - 1
            r["quick_green"] -= 1
            r["scope_green"] -= 1
            r["scope_base_green"] -= 1
    res.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "用例身份集不同" in out, _reason(out, 900)


# ══ P1-8 roster_pairs 内容级 ═══════════════════════════════════════════════
def test_ev_k11_pairs_are_compared_by_content_not_by_keys():
    tree = {"A": [{"from": "x", "to": "y"}]}
    assert _VM.pairs_mismatches({"A": [{"from": "x", "to": "y"}]}, tree) == []
    out = _VM.pairs_mismatches({"A": [{"from": "x", "to": "z"}]}, tree)
    assert out and "与树内不符" in out[0], out
    assert _VM.pairs_mismatches({"A": []}, tree)
    assert _VM.pairs_mismatches({"B": [{"from": "x", "to": "y"}]}, tree)


def test_ev_k12_pair_order_matters():
    """顺序是定义的一部分:替换逐对按序应用,前一对的结果可能是后一对的锚。"""
    tree = {"A": [{"from": "1", "to": "2"}, {"from": "3", "to": "4"}]}
    swapped = {"A": [{"from": "3", "to": "4"}, {"from": "1", "to": "2"}]}
    assert _VM.pairs_mismatches(swapped, tree)


def test_ev_k13_a_swapped_pair_value_is_caught_end_to_end(tmp_path):
    """🔴 端到端:键全对、值被换掉 —— 一份完全合法的 JSON,描述的是另一套变异。"""
    root = build(tmp_path)
    fp = root / "shots27" / "roster_pairs.json"
    d = json.loads(fp.read_text(encoding="utf-8"))
    d[_IDS[0]] = [{"from": "被换掉的锚", "to": "被换掉的替换文本"}]
    fp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "冻结的 pairs 与树内不符" in out, _reason(out, 700)


def test_ev_k14_a_narrowed_quick_scope_is_caught(tmp_path):
    """作用域写窄一格,junit 份数/红集/指纹会跟着一起自洽。"""
    root = build(tmp_path)
    rid = next(i for i in _IDS if len(_SCOPE[i]) > 1)
    res = root / "shots27" / "extsel_v2_results_final.json"
    recs = json.loads(res.read_text(encoding="utf-8"))
    for r in recs:
        if r["id"] == rid:
            r["quick_scope"] = list(_SCOPE[rid])[:-1]
    res.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "quick_scope" in out, _reason(out, 700)


# ══ P2-6 逐 suite ═════════════════════════════════════════════════════════
def test_ev_k15_a_sibling_empty_suite_cannot_carry_the_header(tmp_path):
    """🔴 两个 sibling suite 互相顶数:一个 header 说 5 条却没有实体,
    另一个有 5 条却 header 写 0 —— **总和相等**,上一版全收。"""
    root = build(tmp_path)
    pkg = _PKGS[0]
    f = root / "baseline17" / "junit" / f"junit_{pkg.replace('/', '_')}.xml"
    cls = _cls(pkg)
    cases = "".join(_OK_CASE.format(cls=cls, name=f"test_ok{i}") for i in range(5))
    f.write_text(
        _HEAD + '<testsuites>'
        '<testsuite name="empty" tests="5" failures="0" errors="0" skipped="0">'
        '</testsuite>'
        '<testsuite name="real" tests="0" failures="0" errors="0" skipped="0">'
        + cases + '</testsuite></testsuites>' + chr(10), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "兄弟 suite 之间可以互相顶数" in out, _reason(out, 800)


def test_ev_k16_a_duplicate_testcase_identity_is_caught(tmp_path):
    """同一个 (classname, name) 出现两次:任何计数式检查都看不见它。"""
    root = build(tmp_path)
    pkg = _PKGS[0]
    f = root / "baseline17" / "junit" / f"junit_{pkg.replace('/', '_')}.xml"
    cls = _cls(pkg)
    cases = "".join(_OK_CASE.format(cls=cls, name="test_ok0") for _ in range(5))
    f.write_text(
        _HEAD + '<testsuite name="pytest" tests="5" failures="0" errors="0" '
        'skipped="0">' + cases + '</testsuite>' + chr(10), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "有重复的 (classname, name)" in out, _reason(out, 800)


# ══ P2-4 判据合同自身 ═════════════════════════════════════════════════════
def test_ev_p24a_the_criteria_copy_of_the_contract_equals_the_tool_s():
    """🔴 判据里那份 `_WANT` 必须**逐字等于**验收器的 `CONTRACT`。

    没有这条,合同漂移是**双盲**的:工具把 18 改成 17,判据仍拿自己那份
    `_WANT` 去测纯函数,一路绿;而真正跑起来的验收器已经在用另一个分母。
    判据与被测物各存一份常量,本身就是「同一谓词写两处」。
    """
    assert _WANT == _VM.CONTRACT, (
        f"判据 _WANT={_WANT} != 验收器 CONTRACT={_VM.CONTRACT}")


def test_ev_p24b_a_drifted_contract_really_reaches_the_exit_code(tmp_path):
    """🔴 行为级毒臂:把验收器的 `CONTRACT` 换成漂移过的值,**真跑一遍**。

    上一版这一格只有 AST 锁(「循环体里有 fail(...)」)。AST 锁能证「牙在」,
    证不了「结果进退出码」—— 本轮我毒 `:322` 那一格时它就存活过。
    这条臂把整条链走完:合同漂移 ⇒ fail ⇒ 非零退出码。

    在进程内跑而不是起子进程:要毒的正是那个模块级常量。
    毒完必还原,并自证还原(与自打毒同一套规矩)。
    """
    root = build(tmp_path)
    saved = dict(_VM.CONTRACT)
    bad_before = len(_VM._bad)
    try:
        _VM.CONTRACT = dict(saved, packages=saved["packages"] - 1)
        buf = io.StringIO()
        argv = [str(VERIFY), str(root), "--tip", TIP, "--tree", TREE]
        with contextlib.redirect_stdout(buf):
            old, sys.argv = sys.argv, argv
            try:
                rc = _VM.main()
            finally:
                sys.argv = old
        out = buf.getvalue()
    finally:
        _VM.CONTRACT = saved
        del _VM._bad[bad_before:]          # 进程内跑会往全局红账里加,清回去
    assert _VM.CONTRACT == saved, "还原自证失败"
    assert rc != 0, _reason(out, 800)
    assert "树里的权威合同变了" in out, _reason(out, 800)
    assert "packages" in out, _reason(out, 800)


def test_ev_p24c_the_same_package_passes_with_the_real_contract(tmp_path):
    """阴性对照 —— 同一份包、同一条进程内路径,合同没被毒时必须 rc=0。

    没有这一条,上面那条红证明不了是「合同漂移」造成的:
    也可能是进程内跑这条路本身就跑不通。
    """
    root = build(tmp_path)
    bad_before = len(_VM._bad)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            old, sys.argv = sys.argv, [str(VERIFY), str(root), "--tip", TIP,
                                       "--tree", TREE]
            try:
                rc = _VM.main()
            finally:
                sys.argv = old
    finally:
        del _VM._bad[bad_before:]
    assert rc == 0, _reason(buf.getvalue(), 1200)


# ══ V9-B · 两段运输守恒(Review 裁定 2026-08-31)═══════════════════════════
def _seg_json(root, scope):
    return json.loads((root / scope / "baseline.json").read_text(encoding="utf-8"))


def _put(root, scope, b):
    (root / scope / "baseline.json").write_text(
        json.dumps(b, ensure_ascii=False), encoding="utf-8")


def test_ev_s01_a_missing_segment_is_not_a_pass(tmp_path):
    """🔴 少一段 ⇒ 18 包覆盖不满。这是运输分裂最直接的失败形态:
    段② 没跑而段① 自己看着「全绿」。"""
    root = build(tmp_path)
    shutil.rmtree(root / "baseline_host")
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "两段缺一段" in out or "根下目录不是恰好" in out, _reason(out, 700)


def test_ev_s02_a_package_run_by_both_segments_is_caught(tmp_path):
    """🔴 同包两种运输 = 同包两个真相。它还会让「合起来覆盖满」这句话
    在漏了别的包时照样成立(重复顶掉缺失)。"""
    root = build(tmp_path)
    b = _seg_json(root, "baseline_host")
    dup = _seg_json(root, "baseline17")["segment_targets"][0]
    b["segment_targets"].append(dup)
    b["records"].append({"target": dup, "collected": 5, "tests": 5, "passed": 5,
                         "red": 0, "failures": 0, "errors": 0, "skipped": 0,
                         "skipped_nodes": [], "red_nodes": []})
    _put(root, "baseline_host", b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "被两段都跑了" in out, _reason(out, 800)


def test_ev_s03_two_segments_from_two_tips_do_not_make_one_evidence(tmp_path):
    """🔴 两段的 tip/tree 必须**逐字相等**。

    「两个尖各跑一段、拼起来当一份证据」正是本仓的老坑
    (两个不同的尖曾跑出逐字节相同的 baseline.json)。
    """
    root = build(tmp_path)
    b = _seg_json(root, "baseline_host")
    b["identity"]["tip"] = "e" * 40
    _put(root, "baseline_host", b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "!= 本轮尖" in out, _reason(out, 800)


@pytest.mark.parametrize("scope,bad", [("baseline17", "host"),
                                       ("baseline_host", "container")])
def test_ev_s04_a_segment_wearing_the_other_ones_label_is_caught(scope, bad,
                                                                 tmp_path):
    """段的自报身份必须与它所在的 scope 对上 —— 两段不许互相冒充。"""
    root = build(tmp_path)
    b = _seg_json(root, scope)
    b["identity"]["transport"] = bad
    _put(root, scope, b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "identity.transport" in out, _reason(out, 800)


def test_ev_s05_the_container_segment_must_carry_an_image_id(tmp_path):
    """容器段没有镜像身份 = 这份证据说不出它跑在哪个环境里。

    宿主段没有镜像可绑,所以它那一侧由 python + platform 承担 —— 两段的
    环境身份要求**不同**,写成一条会有一边被放过。
    """
    root = build(tmp_path)
    b = _seg_json(root, "baseline17")
    b["identity"]["image_id"] = None
    _put(root, "baseline17", b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "没有 image_id" in out, _reason(out, 800)


@pytest.mark.parametrize("k", ["python", "platform"])
def test_ev_s06_the_host_segment_must_carry_its_environment_identity(k, tmp_path):
    root = build(tmp_path)
    b = _seg_json(root, "baseline_host")
    b["identity"].pop(k)
    _put(root, "baseline_host", b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert f"identity 缺 {k}" in out, _reason(out, 800)


def test_ev_s07_the_tree_decides_which_packages_go_to_the_host_segment(tmp_path):
    """🔴 哪些包走宿主段由**树内 TRANSPORT** 说了算,不由产物说了算。

    抓的毒:把一个容器包挪进宿主段的产物里 —— 两段仍然不重不漏、
    合起来仍是 18,只有「树内标注」这一条看得见。
    """
    root = build(tmp_path)
    b17 = _seg_json(root, "baseline17")
    bh = _seg_json(root, "baseline_host")
    moved = b17["segment_targets"][0]
    b17["segment_targets"] = [t for t in b17["segment_targets"] if t != moved]
    rec = [r for r in b17["records"] if r["target"] == moved][0]
    b17["records"] = [r for r in b17["records"] if r["target"] != moved]
    bh["segment_targets"].append(moved)
    bh["records"].append(rec)
    b17["totals"]["tests"] -= 5
    b17["totals"]["collected"] -= 5
    b17["totals"]["passed"] -= 5
    bh["totals"]["tests"] += 5
    bh["totals"]["collected"] += 5
    bh["totals"]["passed"] += 5
    shutil.move(str(root / "baseline17" / "junit" /
                    f"junit_{moved.replace('/', '_')}.xml"),
                str(root / "baseline_host" / "junit" /
                    f"junit_{moved.replace('/', '_')}.xml"))
    _put(root, "baseline17", b17)
    _put(root, "baseline_host", bh)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "宿主段覆盖的包" in out, _reason(out, 800)


def test_ev_s08_a_clean_two_segment_package_passes(tmp_path):
    """阴性对照 —— 两段都干净时必须 rc=0,否则上面每条红都说明不了问题。"""
    root = build(tmp_path)
    rc, out, ro = verify(root)
    assert rc == 0, _reason(out, 1500)
    assert ro
    assert "段①容器 22 + 段②宿主 1" in out, _reason(out, 800)


def test_ev_s09_a_package_covered_by_neither_segment_is_caught(tmp_path):
    """🔴 T6 补洞:两段各自**内部自洽**,但合起来漏了一个包。

    自打毒实测:把 `if missing or extra:` 断开之后 30 发里这一发零响应 ——
    既有的臂要么删整段(走「两段缺一段」那条路)、要么造重叠,
    没有一条造出「两段都在、都自洽、合起来少一个包」这种形状。
    而那正是运输分裂最自然的失败方式:某个包**谁都没跑**。
    """
    root = build(tmp_path)
    b = _seg_json(root, "baseline17")
    drop = b["segment_targets"][0]
    b["segment_targets"] = [t for t in b["segment_targets"] if t != drop]
    b["records"] = [r for r in b["records"] if r["target"] != drop]
    n = 5
    for k in ("collected", "tests", "passed"):
        b["totals"][k] -= n
    _put(root, "baseline17", b)
    (root / "baseline17" / "junit" /
     f"junit_{drop.replace('/', '_')}.xml").unlink()
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "没覆盖满冻结分母" in out, _reason(out, 900)


def test_ev_s10_two_segments_that_started_at_the_same_instant_are_caught(tmp_path):
    """两段是**分别跑的**,起跑时刻不该逐字撞上。

    🔴 这条与「两段同尖同树」不同:后者被逐段的
    `identity.tip == 本轮尖` / `tree_three_way` **完全蕴含**
    (两段都等于同一个 CLI 值 ⇒ 两段必然相等),所以那一格是
    **冗余存活**而不是洞;`started_at` 这一格没人蕴含,是真的要单独钉。
    """
    root = build(tmp_path)
    b1 = _seg_json(root, "baseline17")
    b2 = _seg_json(root, "baseline_host")
    b2["identity"]["started_at"] = b1["identity"]["started_at"]
    _put(root, "baseline_host", b2)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "started_at 逐字相同" in out, _reason(out, 900)


def test_ev_s11_the_cross_segment_tip_check_is_redundant_by_construction():
    """🔴 把「T7 是冗余不是洞」这句话**钉成判据**,而不是写在交付文里。

    论证是机械的:第 8 段对**每一段**都做
      · `ident.tip != a.tip`  ⇒ fail
      · `tree_three_way(a.tree, man_tree, ident.tree)` ⇒ fail
    两段都过了这两关 ⇒ 两段的 tip/tree 都等于同一个 CLI 值 ⇒ 两段必然相等。
    所以 `i1[k] != i2[k]` 那一格**在既有守卫下不可达**,毒掉它没有输入能让它单独红。
    留着它是纵深(万一将来有人把逐段那关放松),但它**不该被记成判据缺口**。

    这条判据锁的就是「逐段那两关还在」——纵深的前提一旦没了,它先红。
    """
    src = (pathlib.Path(_VM.__file__).read_text(encoding="utf-8-sig"))
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    body = ast.get_source_segment(src, fn) or ""
    assert 'ident.get("tip") != a.tip' in body, "逐段 tip 那一关没了 ⇒ 冗余论证不成立"
    assert 'tree_three_way(a.tree, man_tree, ident.get("tree"))' in body, (
        "逐段 tree 那一关没了 ⇒ 冗余论证不成立")
    # 且这两关必须在**逐段循环体内**(不是循环外只做一次)
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    inseg = [lp for lp in loops
             if 'ident.get("tip") != a.tip' in (ast.get_source_segment(src, lp) or "")]
    assert inseg, "逐段 tip 检查不在 segs 循环里 —— 只做一次就不蕴含两段相等"


# ══ fof8 P1-2:跨趟身份必须无损 ═══════════════════════════════════════════
def test_ev_i01_the_display_predicate_is_lossy_and_the_identity_one_is_not():
    """🔴 两条谓词各司其职 —— 这条把「它们不是同一个东西」钉死。

    `red_nodeid` 有损(取模块尾名)是**设计**:产物里的红集本来就用短式记。
    坏的是拿它当身份用。所以不是「修好 red_nodeid」,是**拆成两条**。
    """
    a = "tests.defgeo_funding_p0_2026_08_25.test_a1_platform_leg_pg"
    b = "codex_shadow.test_a1_platform_leg_pg"
    assert _RC.red_nodeid(a, "t") == _RC.red_nodeid(b, "t"), (
        "展示谓词现在无损了?那它与身份谓词就没区别了,这一族判据要重写")
    assert _RC.testcase_identity(a, "t") != _RC.testcase_identity(b, "t")
    # 🔴 [fof9] 身份从「拼出来的串」改成**二元组**(分隔符裸拼可被制造碰撞)。
    assert _RC.testcase_identity(a, "t") == (a, "t")


def test_ev_i02_identity_sets_are_built_with_the_lossless_predicate():
    """🔴 接线锁(同一性,不是包含):`node_ids` / `skipped_ids` 的右边
    必须是 `testcase_identity(...)`,不许是 `red_nodeid(...)`。

    抓的毒:把身份集换回短式 —— 集合形状一模一样,只是悄悄有损。
    """
    src = pathlib.Path(_VM.__file__).read_text(encoding="utf-8-sig")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "parse_junit")
    seen = {}
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        for tgt in n.targets:
            if (isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant)
                    and tgt.slice.value in ("node_ids", "skipped_ids")):
                seen[tgt.slice.value] = n.value
    assert set(seen) == {"node_ids", "skipped_ids"}, f"只找到 {sorted(seen)}"
    for key, val in seen.items():
        calls = [c for c in ast.walk(val) if isinstance(c, ast.Call)]
        names = {c.func.attr for c in calls if isinstance(c.func, ast.Attribute)}
        assert "testcase_identity" in names, f"{key} 不是用无损身份建的:{names}"
        assert "red_nodeid" not in names, f"{key} 还在用有损的展示谓词"


def test_ev_i03_the_cross_run_comparison_never_touches_the_lossy_predicate():
    """`identity_mismatches` 整个函数里不许出现 `red_nodeid`,
    也不许再读短式的 `red_nodes` / `skipped_nodes`。"""
    src = pathlib.Path(_VM.__file__).read_text(encoding="utf-8-sig")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "identity_mismatches")
    body = ast.get_source_segment(src, fn) or ""
    assert "red_nodeid" not in body, "跨趟比较里出现了有损谓词"
    subs = {n.slice.value for n in ast.walk(fn)
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
            and isinstance(n.slice.value, str)}
    assert "skipped_nodes" not in subs, "跨趟 skip 比较还在吃短式集合"
    assert {"node_ids", "skipped_ids"} <= subs


def test_ev_i04_same_tail_different_module_is_caught_end_to_end(tmp_path):
    """🔴🔴 Codex 的端到端反例,逐字复刻:

    只把**一份 mutation XML** 的 classname 模块前缀换掉(尾名与用例名保持不变),
    然后**完整重冻**所有 SUMS 与根锚 —— 也就是说自洽性一格不缺、
    根锚是新算的、三参验收全带。修之前:`rc=0 · OK 全项通过`。

    这一发之所以致命:基线与变异可以实际跑**不同模块**,只要尾名相同,
    P1-7 就照报「同一批 testcase」,而它正是差集口径的全部前提。
    """
    root = build(tmp_path)
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    f = root / "shots27" / "junit" / f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml"
    src = f.read_text(encoding="utf-8")
    old_cls = _cls(tgt)
    new_cls = "codex_shadow." + old_cls.split(".")[-1]      # 换前缀,留尾名
    assert old_cls in src
    f.write_text(src.replace(old_cls, new_cls), encoding="utf-8")
    _refreeze(root)                                         # 完整重冻洗白
    rc, out, ro = verify(root)
    assert rc != 0, "同尾名异模块 + 完整重冻之后仍然全过 —— 身份比较是有损的"
    assert ro, "验收器写盘了"
    assert "用例身份集不同" in out, _reason(out, 900)


def test_ev_i05_a_module_prefix_change_in_the_baseline_side_is_caught_too(tmp_path):
    """反方向:动**基线**那一侧的模块前缀,同样必须红。

    只测一个方向的话,「差集」这件事只有一半被守住。
    """
    root = build(tmp_path)
    tgt = _SCOPE[_IDS[0]][0]
    f = root / "shots27" / "junit_base" / f"junit_v2base_{tgt.replace('/', '_')}.xml"
    src = f.read_text(encoding="utf-8")
    old_cls = _cls(tgt)
    f.write_text(src.replace(old_cls, "codex_shadow." + old_cls.split(".")[-1]),
                 encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "用例身份集不同" in out, _reason(out, 900)


def test_ev_i06_a_clean_package_still_passes(tmp_path):
    """阴性对照 —— 换成无损身份之后,干净的包必须照样 rc=0。

    过严也是坏:身份集若把两边本来就相同的用例判成不同,整条链会全红,
    而那时上面几条红证明不了任何东西。
    """
    root = build(tmp_path)
    rc, out, ro = verify(root)
    assert rc == 0, _reason(out, 1500)
    assert ro


def test_ev_i07_a_tail_name_collision_inside_one_run_is_caught(tmp_path):
    """🔴 「两空间同势」闸:同一趟里两条**同尾名异模块**的红,
    短式空间会压成一条,无损空间是两条 —— 差集口径当场不可用。

    这一臂特意把身份集造成**相等**(基线也含两个模块),好让
    `identity_mismatches` 不响 —— 否则响的是别的锁,这一格等于没测。
    修之前那个差集安全**只因为基线被强制零红**,也就是「靠别处的不变量才安全」。
    """
    root = build(tmp_path)
    rid, tgt = _IDS[0], _SCOPE[_IDS[0]][0]
    a_cls = _cls(tgt)                          # tests.<pkg>
    b_cls = "other_pkg." + a_cls.split(".")[-1]   # 同尾名,异模块
    base = (root / "shots27" / "junit_base" /
            f"junit_v2base_{tgt.replace('/', '_')}.xml")
    mut = (root / "shots27" / "junit" /
           f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml")

    def _pair_xml(path, red_names):
        cases = ""
        n_ok = 0
        for cls in (a_cls, b_cls):
            for i in range(4):
                cases += _OK_CASE.format(cls=cls, name=f"test_ok{i}")
                n_ok += 1
            for nm in red_names:
                cases += _RED_CASE.format(cls=cls, name=nm)
        tot = n_ok + 2 * len(red_names)
        path.write_text(
            _HEAD + f'<testsuite name="pytest" tests="{tot}" '
            f'failures="{2 * len(red_names)}" errors="0" skipped="0">'
            + cases + "</testsuite>" + chr(10), encoding="utf-8")
        return tot

    _pair_xml(base, [])                        # 基线:两模块各 4 绿,零红
    _pair_xml(mut, ["test_boom"])              # 变异:两模块各多一条同名红
    res = root / "shots27" / "extsel_v2_results_final.json"
    recs = json.loads(res.read_text(encoding="utf-8"))
    for r in recs:
        if r["id"] == rid:
            r["run_meta"][tgt] = {"collected": 10, "passed": 8, "red": 2,
                                  "skipped": 0}
            #: 短式空间里那两条红压成一条 —— 产物如实照抄短式(生产就是这么记的)
            r["quick_red"] = [f"{a_cls.split('.')[-1]}::test_boom"]
            r["quick_green"] = 8
            r["scope_green"] = 8
            r["scope_base_green"] = 10
            r["quick_scope"] = [tgt]
    res.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "同尾名异模块被压掉了" in out, _reason(out, 1200)


@pytest.mark.parametrize("scope", ["baseline17", "baseline_host"])
def test_ev_r01_a_segment_without_runner_versions_is_caught(scope, tmp_path):
    """🔴 两段都要绑 runner —— 少一段就说不出那一段是谁跑的。"""
    root = build(tmp_path)
    b = _seg_json(root, scope)
    b["identity"].pop("runner")
    _put(root, scope, b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "identity 缺 runner" in out or "没绑 runner 版本" in out, _reason(out, 800)


@pytest.mark.parametrize("bad", [{}, {"pytest": ""}, {"pytest": 8}, "8.3.5", []])
def test_ev_r02_a_malformed_runner_record_is_caught(bad, tmp_path):
    """空 dict / 空版本 / 非字符串 / 根本不是 dict —— 一律拒。

    「键在」不等于「记住了」:上面每一种都长得像记过了。
    """
    root = build(tmp_path)
    b = _seg_json(root, "baseline17")
    b["identity"]["runner"] = bad
    _put(root, "baseline17", b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0 and ro
    assert "runner" in out, _reason(out, 800)


def test_ev_r03_the_two_segments_may_legitimately_differ(tmp_path):
    """🔴 阴性对照:两段的 runner 版本**允许不同**(容器 3.12 / 宿主 venv)。

    这一格必须绿,否则我就把「记全了」写成了「必须一样」——
    而后者会在两段运输下永远红,然后被人关掉。
    """
    root = build(tmp_path)
    b = _seg_json(root, "baseline_host")
    b["identity"]["runner"] = {"pytest": "9.1.1", "pytest_asyncio": "1.4.0"}
    _put(root, "baseline_host", b)
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc == 0, _reason(out, 1200)
    assert ro


# ══ fof9 P1:身份不许拍平成字符串 ═══════════════════════════════════════════
def test_ev_j01_the_identity_is_a_tuple_not_a_joined_string():
    """🔴 身份是**二元组**,不是拼出来的串。

    上一版 `f"{classname}::{name}"` 让 `("a::b","c")` 与 `("a","b::c")` 撞成一个。
    换个更罕见的分隔符只是把概率调小,病还在:**有结构的东西别拍平**。
    """
    ident = _RC.testcase_identity("qa::boundary", "same_case")
    assert isinstance(ident, tuple) and len(ident) == 2, f"身份不是二元组:{ident!r}"
    assert ident == ("qa::boundary", "same_case")


@pytest.mark.parametrize("a,b", [
    (("qa::boundary", "same_case"), ("qa", "boundary::same_case")),   # Codex 原反例
    (("a::b", "c"), ("a", "b::c")),
    (("tests.pkg.test_mod", "test_a::extra"),
     ("tests.pkg.test_mod::test_a", "extra")),
    (("", "a::b"), ("a", "b")),                                       # 空 classname 那一格
])
def test_ev_j02_boundary_shifted_pairs_never_collide(a, b):
    """🔴 双向反例:把 `::` 从一边挪到另一边,身份**必须**还是两个。"""
    ia, ib = _RC.testcase_identity(*a), _RC.testcase_identity(*b)
    assert ia != ib, f"{a} 与 {b} 撞成同一个身份:{ia!r}"
    assert len({ia, ib}) == 2, "放进 set 之后仍然只剩一个 —— 集合比较会被骗"


def test_ev_j03_identity_sets_hold_tuples(tmp_path):
    """🔴 退化锁:`parse_junit` 产出的三个身份集合,元素必须是**二元组**。

    抓的毒:有人把 `testcase_identity` 改回拼串 —— 集合形状一模一样,
    只是每个元素从元组变回 str,而碰撞就是在那一刻回来的。
    """
    root = build(tmp_path)
    tgt = _SCOPE[_IDS[0]][0]
    f = (root / "shots27" / "junit_base" /
         f"junit_v2base_{tgt.replace('/', '_')}.xml")
    j = _VM.parse_junit(f)
    assert j, "解析不出来,这条判据钉错了地方"
    for key in ("node_ids", "skipped_ids", "red_ids"):
        s = j[key]
        assert isinstance(s, set), f"{key} 不是 set"
        for e in s:
            assert isinstance(e, tuple) and len(e) == 2, (
                f"{key} 里有非二元组元素 {e!r} —— 身份被拍平了")
    assert j["node_ids"], "身份集是空的,上面那圈断言等于没跑"
    # 展示空间那两个仍然是 str —— 两个空间**各自成立**,不是都改成元组
    for key in ("red_nodes", "skipped_nodes"):
        for e in j[key]:
            assert isinstance(e, str), f"{key} 该留在展示空间(str)"


def test_ev_j04_no_consumer_flattens_the_identity_back_into_a_string():
    """🔴 接线锁:没有任何消费方把身份二元组重新拼成字符串。

    抓的毒:`"::".join(ident)` / `f"{c}::{n}"` 悄悄回来 —— 集合还是集合,
    比较还是比较,只是又变回了可碰撞的空间。
    这里锁的是「`testcase_identity` 的返回值不进任何 join / f-string 拼接」。
    """
    src = pathlib.Path(_VM.__file__).read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    bad = []
    for n in ast.walk(tree):
        # f"...{testcase_identity(...)}..." 之类
        if isinstance(n, ast.JoinedStr):
            for v in ast.walk(n):
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                        and v.func.attr == "testcase_identity"):
                    bad.append(f"JoinedStr@{n.lineno}")
        # "sep".join(<含 testcase_identity 的东西>)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "join"):
            for v in ast.walk(n):
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                        and v.func.attr == "testcase_identity"):
                    bad.append(f"join@{n.lineno}")
    assert not bad, f"身份又被拼回字符串:{bad}"
    # 本体也不许自己拼:返回语句必须是 Tuple
    rc_src = pathlib.Path(_RC.__file__).read_text(encoding="utf-8-sig")
    fn = next(x for x in ast.walk(ast.parse(rc_src))
              if isinstance(x, ast.FunctionDef) and x.name == "testcase_identity")
    rets = [r for r in ast.walk(fn) if isinstance(r, ast.Return)]
    assert rets and all(isinstance(r.value, ast.Tuple) for r in rets), (
        "testcase_identity 的 return 不是元组字面量")


def test_ev_j05_a_boundary_shift_is_caught_end_to_end(tmp_path):
    """🔴🔴 Codex 的端到端反例:基线与**共享它的全部** mutation junit 同步
    把 `::` 从 classname 挪到 name。

    两趟的 testcase 条数、结果、红数、skip 数**全都不变**,
    只有「字段边界」不同 —— 修之前两边序列化成同一个串,身份集相等,一路绿。

    🔴 「同步改全部」是这条臂**成立的前提**:我第一版只改了一份 mutation junit,
    于是其余 26 发因为**共享基线被改**而红 —— 红是红了,红的却是别的规则。
    红臂必须红在它声称的那一条上,否则修好之后它照样绿,而我会以为修对了。
    """
    root = build(tmp_path)
    tgt = _SCOPE[_IDS[0]][0]
    cls = _cls(tgt)
    old = f'classname="{cls}" name="test_ok0"'
    base = (root / "shots27" / "junit_base" /
            f"junit_v2base_{tgt.replace('/', '_')}.xml")
    b = base.read_text(encoding="utf-8")
    assert old in b
    base.write_text(b.replace(old, f'classname="{cls}::seam" name="test_ok0"'),
                    encoding="utf-8")
    # 共享这条基线的**每一发**都要同步挪边界,否则红的是「基线被改」而不是边界
    touched = 0
    for rid in _IDS:
        if tgt not in _SCOPE[rid]:
            continue
        mut = (root / "shots27" / "junit" /
               f"junit_{rid[4:]}_{tgt.replace('/', '_')}.xml")
        m = mut.read_text(encoding="utf-8")
        assert old in m, f"{rid} 的 junit 里没有锚点"
        mut.write_text(m.replace(old, f'classname="{cls}" name="seam::test_ok0"'),
                       encoding="utf-8")
        touched += 1
    assert touched >= 2, f"只改到 {touched} 发,这条臂的前提是「全部同步」"
    _refreeze(root)
    rc, out, ro = verify(root)
    assert rc != 0, "边界挪一格之后仍然全过 —— 身份还是拍平的"
    assert ro, "验收器写盘了"
    assert "用例身份集不同" in out, _reason(out, 900)