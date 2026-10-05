# -*- coding: utf-8 -*-
"""变异**定义指纹** `mutation_fp` 的合同判据(V9-B · Codex fof7 P1-4)。

**修之前的实测**:一条续跑缓存记录同时满足 —— 尖对、逐发判据指纹对、
run_meta 结构自洽、语义自洽、裁定合法。五道闸全过。
可它**没有任何一样东西绑「这一发到底是什么变异」**:
把抽取清单里某发的 `file` / `pairs` 换掉(改靶文件、改锚、改替换文本),
上面五样一个都不会变,旧读数照收 —— 而它证的已经是另一发了。

这个文件守两件事,分开写因为它们会往相反方向坏:

    敏感(不许过松)   定义的四个维度任一改动 ⇒ 指纹必变 ⇒ 旧读数作废
    钝感(不许过严)   与定义无关的字段改动 ⇒ 指纹不变 ⇒ 缓存仍可用

过严不是"安全的那一边":它让每次清单里加个注释都全量重跑,人就会去关这道闸。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_definition_fp_contract.py -q
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE / "mutation_runner_extsel_v2_2026_08_27.py"
sys.path.insert(0, str(HERE))


def _load():
    spec = importlib.util.spec_from_file_location("_v2fp", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_v2fp"] = mod
    spec.loader.exec_module(mod)
    return mod


_R = _load()
ROSTER = _R.load_v2()
SAMPLE = next(m for m in ROSTER if m.get("pairs"))


def _fp(m) -> str:
    return _R.mutation_fp_of(m)


# ══ ① 形状与稳定性 ═══════════════════════════════════════════════════════
def test_fp_01_is_a_sha256_hex_and_is_stable():
    a, b = _fp(SAMPLE), _fp(copy.deepcopy(SAMPLE))
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a), a
    assert a == b, "同一份定义两次算出不同指纹 —— 里面混进了不稳定的东西"


def test_fp_02_every_shot_in_the_roster_has_a_distinct_definition():
    """🔴 全清单:id 集合覆盖 + 指纹两两不同。

    两发指纹相同意味着它们**就是同一个变异**(靶/锚/作用域/预期全一样)——
    那样其中一发的读数可以顶替另一发,而清单大小照样是 27。
    """
    table = _R.mutation_fp_by_id()
    ids = [m["id"] for m in ROSTER]
    assert set(table) == set(ids) and len(table) == len(ids)
    dupes = sorted({v for v in table.values() if list(table.values()).count(v) > 1})
    assert not dupes, ("清单里有定义完全相同的两发:"
                       + str(sorted(i for i, v in table.items() if v in dupes)))


# ══ ② 敏感面:分母从函数自己的 payload 机械取,不手写 ═══════════════════
def _payload_keys() -> set[str]:
    """指纹覆盖的维度集 —— **分母不由我手写**。

    🔴 [fof8 P1-1] 上一版从 `mutation_fp_of` 里那个字面 dict 取键。
    payload 改成按 `FP_KEYS` 生成的推导式之后,那个字面 dict 没了 ——
    分母跟着上移到更机械的一层:`FP_KEYS` 本身 + 显式单列的 `scope`。
    (`fp_version` 不是 roster 字段,由 `test_fp_16` 行为级证明它真在 payload 里。)
    """
    return set(_R.FP_KEYS) | {"scope"}


def _perturb_family(m):
    m["family"] = "ZZ"


def _perturb_file(m):
    m["file"] = str(m["file"]) + ".other"


def _perturb_pairs(m):
    m["pairs"] = [dict(p, to=str(p.get("to")) + "X") for p in m["pairs"]]


def _perturb_scope(m):
    _R.QUICK_SCOPE[m["id"]] = tuple(list(_R.QUICK_SCOPE[m["id"]]) + ["tests/zzz"])


def _perturb_expect(m):
    m["expect_red_superset"] = list(m.get("expect_red_superset") or []) + ["k::new"]


def _perturb_must_stay_green(m):
    """🔴 fof8 P1-1 的正主:它**参与裁定**(must_stay_green ∩ 实际红 必须为空),
    修之前却不在指纹里 —— 只改它,新旧指纹同为 `130c11e…`,旧记录照样进 resume。"""
    m["must_stay_green"] = list(m.get("must_stay_green") or []) + ["test_zz_stay_green"]


def _perturb_needs_amendment(m):
    m["needs_amendment"] = list(m.get("needs_amendment") or []) + ["still_open"]


PERTURB = {"family": _perturb_family, "file": _perturb_file,
           "pairs": _perturb_pairs, "scope": _perturb_scope,
           "expect_red_superset": _perturb_expect,
           "must_stay_green": _perturb_must_stay_green,
           "needs_amendment": _perturb_needs_amendment}


def test_fp_03_the_covered_dimensions_are_exactly_the_payload_keys():
    """🔴 分母锁:反例覆盖的维度集必须**等于** payload 的键集。"""
    assert set(PERTURB) == _payload_keys(), (
        f"反例覆盖 {sorted(PERTURB)} 而 payload 是 {sorted(_payload_keys())} —— "
        "有维度没人打反例,或者反例打在已经不存在的维度上")


def test_fp_13_the_key_denominator_is_the_rosters_own_key_set():
    """🔴🔴 [fof8 P1-1] **指纹分母 = 树内 roster 真正带的键全集**,不重不漏。

    这条是本次修复的核心:上一版漏 `must_stay_green` 不是「忘了加一个字段」,
    是**根本没有一个东西在回答「字段齐没齐」**。
    现在:`FP_KEYS ∪ FP_EXEMPT_KEYS` 必须逐字等于 roster 的键全集 ——
    新加一个 roster 字段而没决定它进不进指纹,这条先红,逼人做一次决定。
    """
    keys = set().union(*[set(r) for r in ROSTER])
    covered = set(_R.FP_KEYS) | set(_R.FP_EXEMPT_KEYS)
    assert not (set(_R.FP_KEYS) & set(_R.FP_EXEMPT_KEYS)), "同一个键既进指纹又被豁免"
    assert covered == keys, (
        f"指纹分母与 roster 键全集不符(漏 {sorted(keys - covered)} · "
        f"多 {sorted(covered - keys)})")


def test_fp_14_every_exemption_carries_a_reason_and_the_list_is_pinned():
    """豁免名单每条带理由 + 钉大小 —— 与 `NON_DSN_ENVS` 同一套待遇。

    判准写在 runner 的注释里、也钉在这:**改了它这一发已有的读数还作不作数**。
    """
    assert len(_R.FP_EXEMPT_KEYS) == _R.FP_EXEMPT_SIZE
    thin = {k: v for k, v in _R.FP_EXEMPT_KEYS.items()
            if not isinstance(v, str) or len(v.strip()) < 12}
    assert not thin, f"这些豁免没有像样的理由:{sorted(thin)}"


def test_fp_15_a_record_without_a_version_is_refused():
    """没有 `mutation_fp_version` 的旧缓存 ⇒ 指纹口径未知 ⇒ 拒。"""
    mid = SAMPLE["id"]
    r = _record(mid, "1" * 64)
    r.pop("mutation_fp_version")
    why = _R.validate_record_run_meta(r, _R.QUICK_SCOPE[mid],
                                      mut_fp_by_id={mid: "1" * 64})
    assert why and "没有 mutation_fp_version" in why, why


@pytest.mark.parametrize("bad", [1, 99, "2", 2.0, None, True])
def test_fp_16_a_version_that_is_not_this_rounds_is_refused(bad):
    """🔴 版本不等一律拒,且报文点名**口径**(处置 = 全部重跑),
    与「指纹不等」(处置 = 重跑这一发)分开说 —— 两者处置不同。"""
    mid = SAMPLE["id"]
    r = _record(mid, "1" * 64)
    r["mutation_fp_version"] = bad
    why = _R.validate_record_run_meta(r, _R.QUICK_SCOPE[mid],
                                      mut_fp_by_id={mid: "1" * 64})
    if bad == _R.FP_VERSION and type(bad) is int:
        pytest.skip("这个值就是本轮版本")
    assert why and "指纹口径变了" in why, f"{bad!r}: {why!r}"


def test_fp_17_the_version_really_participates_in_the_fingerprint(monkeypatch):
    """🔴 行为级:`fp_version` 必须**真的**进 payload —— 改版本号 ⇒ 指纹全变。

    这条不能用 AST 看「源码里有没有那一行」:那是裸串锁。
    改常量、重算,看数字动不动。
    """
    before = {m["id"]: _R.mutation_fp_of(m) for m in ROSTER}
    monkeypatch.setattr(_R, "FP_VERSION", _R.FP_VERSION + 1)
    after = {m["id"]: _R.mutation_fp_of(m) for m in ROSTER}
    same = [i for i in before if before[i] == after[i]]
    assert not same, f"版本改了而这些发的指纹没变:{same[:3]} —— 版本没进 payload"


def test_fp_18_the_fixture_version_mirror_equals_the_single_point():
    """夹具那份 `FP_VERSION_FOR_FIXTURES` 是受钉副本 —— 必须与单点相等。"""
    import mutation_criteria_fixtures as _F
    assert _F.FP_VERSION_FOR_FIXTURES == _R.FP_VERSION


@pytest.mark.parametrize("dim", sorted(PERTURB))
def test_fp_04_each_dimension_changes_the_fingerprint(dim):
    """🔴 正样本(点名维度):改这一维 ⇒ 指纹必变。"""
    m = copy.deepcopy(SAMPLE)
    before = _fp(m)
    saved = _R.QUICK_SCOPE.get(m["id"])
    try:
        PERTURB[dim](m)
        assert _fp(m) != before, f"改了 {dim} 指纹却没变 —— 这一维没进指纹"
    finally:
        if saved is not None:
            _R.QUICK_SCOPE[m["id"]] = saved


# ══ ③ 钝感面:无关字段不许把缓存全作废 ═══════════════════════════════════
@pytest.mark.parametrize("key", ["note", "why", "owner"])
def test_fp_05_an_unrelated_field_does_not_change_the_fingerprint(key):
    """阴性对照 —— 过严也是坏:清单里加一句注释就全量重跑,这道闸会被人关掉。"""
    m = copy.deepcopy(SAMPLE)
    before = _fp(m)
    m[key] = "随便写点什么"
    assert _fp(m) == before, f"{key} 不属于变异定义,却把指纹带变了"


def test_fp_06_expectation_order_does_not_matter():
    """`expect_red_superset` 是**集合**语义:换个顺序不是换了一发。"""
    m = copy.deepcopy(SAMPLE)
    m["expect_red_superset"] = ["b::2", "a::1"]
    one = _fp(m)
    m["expect_red_superset"] = ["a::1", "b::2"]
    assert _fp(m) == one


# ══ ④ 落盘接线:写下去的值必须**由函数算出来**,不是字面量 ════════════════
def test_fp_07_the_persisted_value_comes_from_the_function():
    """🔴 数据流锁:`rec["mutation_fp"] = ...` 右边必须是 `mutation_fp_of(...)`。

    抓的毒:留个键但填死值(或填 rec 里已有的别的字段)—— 键在、格式对、
    每条记录都有,而它跟这一发的定义没有任何关系。
    """
    tree = ast.parse(RUNNER.read_text(encoding="utf-8-sig"))
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets
              if isinstance(t, ast.Subscript)
              and isinstance(t.slice, ast.Constant) and t.slice.value == "mutation_fp"]
    assert writes, "源码里没有任何一处往记录里写 mutation_fp"
    for w in writes:
        assert isinstance(w.value, ast.Call) and isinstance(w.value.func, ast.Name) \
            and w.value.func.id == "mutation_fp_of", (
                f"第 {w.lineno} 行写 mutation_fp 的右边不是 mutation_fp_of(...)")


# ══ ⑤ 行为端到端:定义变了,两个入口都必须拒 ═════════════════════════════
def _record(mid, fp_value):
    scope = _R.QUICK_SCOPE[mid]
    meta = {t: {"collected": 10, "passed": 8, "red": 2, "skipped": 0} for t in scope}
    return {"id": mid, "tip": "a" * 40, "criteria_fp": "cfp", "verdict": "KILLED",
            "mutation_fp": fp_value, "mutation_fp_version": _R.FP_VERSION,
            "run_meta": meta,
            "quick_red": [f"m::t{i}" for i in range(2 * len(scope))],
            "quick_green": 8 * len(scope),
            # 🔴 [V9-B · P2-1 之后] 作用域绿两格:钝杀分类要按它们重算。
            #    这个夹具写在 P2-1 落地之前,当时单跑是绿的 —— 全量跑才红。
            #    「单跑绿」从来不是「这条判据成立」的证明。
            "scope_green": 8 * len(scope),
            "scope_base_green": 10 * len(scope)}


def test_fp_08_resume_refuses_a_record_whose_definition_moved():
    """🔴 续跑臂:定义改了 ⇒ 旧读数不许当「已跑过」。"""
    mid = SAMPLE["id"]
    stale = _record(mid, "0" * 64)
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids([stale], "a" * 40, "x.json", {mid: "cfp"},
                           targets_by_id={mid: tuple(_R.QUICK_SCOPE[mid])},
                           mut_fp_by_id={mid: "1" * 64})
    assert "变异定义变了" in str(exc.value), str(exc.value)


def test_fp_09_the_hard_gate_refuses_it_too():
    """🔴 终门臂:同上,rc 必须非零 —— 两个入口不许只守一个。"""
    mid = SAMPLE["id"]
    assert _R.hard_gate_rc([_record(mid, "0" * 64)], "自证", expected={mid},
                           targets_by_id={mid: tuple(_R.QUICK_SCOPE[mid])},
                           mut_fp_by_id={mid: "1" * 64}) != 0


def test_fp_10_the_matching_record_is_accepted():
    """阴性对照:指纹对上就该收 —— 否则上面两条红证明不了是「定义变了」造成的。"""
    mid = SAMPLE["id"]
    good = _record(mid, "1" * 64)
    assert _R.resume_done_ids([good], "a" * 40, "x.json", {mid: "cfp"},
                              targets_by_id={mid: tuple(_R.QUICK_SCOPE[mid])},
                              mut_fp_by_id={mid: "1" * 64}) == {mid}


@pytest.mark.parametrize("bad", [None, "", "zz", 64, "g" * 64, "A" * 64 + "A"])
def test_fp_11_a_malformed_fingerprint_is_refused(bad):
    """形状不对一律拒 —— 不许「坏型就不比」。"""
    mid = SAMPLE["id"]
    why = _R.validate_record_run_meta(_record(mid, bad), _R.QUICK_SCOPE[mid],
                                      mut_fp_by_id={mid: "1" * 64})
    # 🔴 必须点名**形状**那条规则。只断言「被拒了」不够:任何形状不对的值都
    #    不可能等于本轮真指纹,所以「不等于本轮」那一条会替它把红报掉 ——
    #    形状检查删掉这条臂照样绿。
    assert why and "sha256" in why, f"{bad!r} 不是被形状规则拒的:{why!r}"


def test_fp_12_an_id_outside_this_rounds_roster_is_refused():
    """fail-closed:查不到这一发的定义 ⇒ 拒,不许「查不到就放过」。"""
    mid = SAMPLE["id"]
    why = _R.validate_record_run_meta(_record(mid, "1" * 64), _R.QUICK_SCOPE[mid],
                                      mut_fp_by_id={})
    assert why and "查不到" in why, why
