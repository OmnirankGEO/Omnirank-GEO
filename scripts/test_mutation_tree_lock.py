# -*- coding: utf-8 -*-
"""机制闸自证(Review 机制令 2026-08-26)。

机制本身也得有判据 —— 「新增要被别人依赖的东西必须同 commit 写接线锁」。
这些判据钉的正是两条机制**失效时的那个样子**:

  · 锁存在却让第二个 runner 跑起来了(= 三伤重演);
  · ``MUTLOCK_BREAK=1`` 这种瞎设也能强解(= 锁形同虚设);
  · 判据文件变了指纹没变(= A 的缓存重放事故重演);
  · 目标路径打错 ⇒ 静默算 0 条判据(= 空分母假绿)。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_tree_lock.py -q
"""
from __future__ import annotations

import json

import pytest

import mutation_tree_lock as M


@pytest.fixture()
def lock_in_tmp(tmp_path, monkeypatch):
    """把锁挪到 tmp,免得自证过程去动真树上那把。"""
    monkeypatch.setattr(M, "LOCK_PATH", tmp_path / ".mutlock")
    monkeypatch.delenv("MUTLOCK_BREAK", raising=False)
    return tmp_path / ".mutlock"


# ── ① 树级排他锁 ──────────────────────────────────────────────────────
def test_lock_path_is_the_tree_root_dot_mutlock():
    """锁必须是**树级**的(一棵 worktree 一把),不是进程级/目录级。"""
    assert M.LOCK_PATH == M.ROOT / ".mutlock"
    assert M.LOCK_PATH.parent == M.ROOT


def test_second_runner_is_refused_while_the_lock_is_held(lock_in_tmp):
    first = M.acquire("runner-甲")
    assert lock_in_tmp.exists()
    with pytest.raises(SystemExit) as e:
        M.acquire("runner-乙")
    assert "拒跑" in str(e.value)
    # 现场必须打印得出来:光说"被锁了"没法判该不该强解。
    assert "runner-甲" in str(e.value) and first in str(e.value)
    M.release(first)
    assert not lock_in_tmp.exists()


def test_a_blind_break_flag_does_not_open_the_lock(lock_in_tmp, monkeypatch):
    """``MUTLOCK_BREAK=1`` 必须**拒**——强解的人得真的读过锁文件。"""
    held = M.acquire("runner-甲")
    for blind in ("1", "true", "yes", "force"):
        monkeypatch.setenv("MUTLOCK_BREAK", blind)
        with pytest.raises(SystemExit):
            M.acquire("runner-乙")
    assert json.loads(lock_in_tmp.read_text(encoding="utf-8"))["token"] == held


def test_break_works_only_with_the_exact_token_from_the_lock_file(lock_in_tmp,
                                                                 monkeypatch):
    held = M.acquire("runner-甲")
    monkeypatch.setenv("MUTLOCK_BREAK", held)
    mine = M.acquire("runner-乙")
    assert mine != held
    assert json.loads(lock_in_tmp.read_text(encoding="utf-8"))["runner"] == "runner-乙"


def test_release_never_drops_someone_elses_lock(lock_in_tmp, monkeypatch):
    """强解之后别人可能已重新持锁 —— 我的 finally 不许把它删了。"""
    stale = M.acquire("runner-甲")
    monkeypatch.setenv("MUTLOCK_BREAK", stale)
    mine = M.acquire("runner-乙")
    M.release(stale)                       # 甲的 finally 迟到
    assert lock_in_tmp.exists(), "乙的锁被甲的 finally 删掉了"
    assert json.loads(lock_in_tmp.read_text(encoding="utf-8"))["token"] == mine


def test_an_unparsable_lock_still_blocks(lock_in_tmp):
    """半写/损坏的锁文件也算有人持锁 —— 拒跑方向永远是安全那侧。"""
    lock_in_tmp.write_text("{半个 json", encoding="utf-8")
    with pytest.raises(SystemExit):
        M.acquire("runner-乙")


def test_tree_lock_releases_even_when_the_body_raises(lock_in_tmp):
    with pytest.raises(RuntimeError):
        with M.tree_lock("runner-甲"):
            raise RuntimeError("跑崩了")
    assert not lock_in_tmp.exists(), "崩溃路径没释放锁 ⇒ 下一轮全被残锁挡死"


# ── ② 判据文件集指纹 ──────────────────────────────────────────────────
@pytest.fixture()
def fake_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "ROOT", tmp_path)
    pkg = tmp_path / "tests" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "test_one.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (pkg / "test_two.py").write_text("def test_b(): pass\n", encoding="utf-8")
    (pkg / "conftest.py").write_text("# pkg conftest\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("# root conftest\n", encoding="utf-8")
    (pkg / "helper.py").write_text("# 不是判据文件\n", encoding="utf-8")
    return tmp_path, pkg


def test_fingerprint_changes_when_a_criteria_file_changes(fake_tree):
    """A 的缓存重放事故就死在这一条上:补了判据、缓存照旧命中。"""
    tmp, pkg = fake_tree
    before, meta = M.criteria_fingerprint(["tests/pkg"])
    (pkg / "test_one.py").write_text(
        "def test_a(): pass\ndef test_c(): pass\n", encoding="utf-8")
    after, _ = M.criteria_fingerprint(["tests/pkg"])
    assert before != after
    assert meta["n_files"] == 4          # 2 test + pkg conftest + root conftest


def test_fingerprint_changes_when_a_criteria_file_is_added(fake_tree):
    tmp, pkg = fake_tree
    before, _ = M.criteria_fingerprint(["tests/pkg"])
    (pkg / "test_three.py").write_text("def test_c(): pass\n", encoding="utf-8")
    after, meta = M.criteria_fingerprint(["tests/pkg"])
    assert before != after and meta["n_files"] == 5


def test_fingerprint_covers_ancestor_conftest_for_single_file_targets(fake_tree):
    """conftest 支配判据行为;漏掉它 = 判据变了指纹没变。"""
    tmp, pkg = fake_tree
    before, meta = M.criteria_fingerprint(["tests/pkg/test_one.py"])
    assert meta["n_files"] == 3          # 自己 + pkg conftest + root conftest
    (tmp / "conftest.py").write_text("# root conftest 改了\n", encoding="utf-8")
    assert M.criteria_fingerprint(["tests/pkg/test_one.py"])[0] != before


def test_fingerprint_ignores_non_criteria_files(fake_tree):
    tmp, pkg = fake_tree
    before, _ = M.criteria_fingerprint(["tests/pkg"])
    (pkg / "helper.py").write_text("# 改了但不是判据\n", encoding="utf-8")
    assert M.criteria_fingerprint(["tests/pkg"])[0] == before


def test_a_missing_target_is_a_hard_stop_not_an_empty_denominator(fake_tree):
    """目标打错 ⇒ 0 条判据 ⇒ 变异全存活 ⇒ 看起来像「判据洞遍地」。"""
    with pytest.raises(SystemExit):
        M.criteria_fingerprint(["tests/pkg_typo"])


def test_fingerprint_follows_helpers_imported_by_criteria(fake_tree):
    """🔴 [E1-3②] 判据 import 的 tests/ 支撑模块必须进指纹。

    夹具(``_seed`` / ``_chain``)变了而指纹不变 = 缓存该失效时没失效。
    两种 import 形态都要认 —— 第一版只认 ``from tests.pkg.helper import x``,
    漏掉了 ``from tests.pkg import _seed`` 这种**子模块**形态,
    而后者恰恰是本仓夹具的主要写法(实测七包漏 14 个)。
    """
    tmp, pkg = fake_tree
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "_seed.py").write_text("SEED = 1\n", encoding="utf-8")
    (pkg / "_chain.py").write_text(
        "from tests.pkg import _seed\nCHAIN = _seed.SEED\n", encoding="utf-8")
    (pkg / "test_one.py").write_text(
        "from tests.pkg import _chain\ndef test_a(): pass\n", encoding="utf-8")

    before, meta = M.criteria_fingerprint(["tests/pkg"])
    names = {p.name for p in M._criteria_files(["tests/pkg"])}
    assert "_chain.py" in names, "直接 import 的 helper 没进指纹"
    assert "_seed.py" in names, "helper 再 import 的 helper 没进指纹(传递闭包缺失)"

    # 改**最里层**那个夹具,指纹必须变 —— 这是传递闭包真正要挡的事。
    (pkg / "_seed.py").write_text("SEED = 2\n", encoding="utf-8")
    assert M.criteria_fingerprint(["tests/pkg"])[0] != before


def test_fingerprint_never_follows_production_modules(fake_tree):
    """🔴 只跟 ``tests.*``。生产代码是**被测对象**,不是判据。

    把它算进指纹 ⇒ 变异 runner 每改一次源文件指纹就变一次 ⇒ 缓存永远失效。
    那不是收紧,是把机制搞坏。
    """
    tmp, pkg = fake_tree
    prod = tmp / "services"
    prod.mkdir()
    (prod / "__init__.py").write_text("", encoding="utf-8")
    (prod / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "test_one.py").write_text(
        "from services.thing import VALUE\ndef test_a(): pass\n", encoding="utf-8")

    before, _ = M.criteria_fingerprint(["tests/pkg"])
    assert "thing.py" not in {p.name for p in M._criteria_files(["tests/pkg"])}
    (prod / "thing.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert M.criteria_fingerprint(["tests/pkg"])[0] == before, (
        "改生产代码换了判据指纹 —— 变异 runner 会每发都判缓存失效")


# ── ③ 接线 census:就地改源的 runner 必须抢锁 ──────────────────────────
#: 冻结豁免集。**不是"不用抢锁",是"本轮不动它"** ——
#: 这两个是 2026-07 的非 defgeo 一次性脚本,改它们扩大爆炸半径而对 E1 零收益。
#: 写在这里的作用是让它们**显式在册**:数字钉死,谁想再加一个不抢锁的 runner,
#: 得先在这里改数,而不是悄悄多一个。
_LOCK_EXEMPT_LEGACY_RUNNERS = frozenset({
    "mutation_safeimage_2026_07_29.py",
    "mutation_title_length_2026_07_29.py",
})


def test_every_in_place_runner_takes_the_tree_lock():
    """🔴 机制令 ① 的**接线锁**:锁写好了不等于每个 runner 都接上了。

    落地那轮只接了 extsel / wob / survivor 三个,WOA 与 WOC 漏了 ——
    而它们同样就地改源文件。漏一个的后果与 2026-08-26 三伤一模一样。
    「就地改源」按**行为特征**机械枚举(备份 / 还原 / 写回),不手抄名单。
    """
    import ast as _ast

    scripts = M.ROOT / "scripts"
    in_place, locked = set(), set()
    _WRITES = {"write_bytes", "write_text", "copy2", "copy", "move"}
    for f in sorted(scripts.glob("mutation*.py")):
        src = f.read_text(encoding="utf-8-sig")
        text_hit = any(k in src for k in
                       (".mutbak", "shutil.copy2", "apply_mutation",
                        "write_bytes(mutated)"))
        # 🔴 [V5-B] 只看文本会被**散文**带沟里:mutation_replay_common.py 的
        #    停机文案里写着「零 .mutbak、零产物」,而它一个字节都不往盘上写。
        #    (本仓记过:引用裁决原文会触发裸串锁。)
        #    所以两个信号都要 —— 提到了 **且** 真的调了写盘。
        try:
            tree = _ast.parse(src)
        except SyntaxError:
            continue
        ast_hit = any(isinstance(n, _ast.Call)
                      and getattr(n.func, "attr", None) in _WRITES
                      for n in _ast.walk(tree))
        if text_hit and ast_hit:
            in_place.add(f.name)
            if "tree_lock(" in src:
                locked.add(f.name)

    assert in_place, "一个就地改源的 runner 都没扫到 —— 探针写废了"
    unlocked = in_place - locked - _LOCK_EXEMPT_LEGACY_RUNNERS
    assert not unlocked, (
        f"这些 runner 就地改源却没抢树锁:{sorted(unlocked)} —— "
        "并发下毒落在谁的基线上无法归属")

    stale = _LOCK_EXEMPT_LEGACY_RUNNERS - in_place
    assert not stale, (
        f"豁免集里有已经不存在 / 不再就地改源的条目:{sorted(stale)} —— "
        "豁免集必须跟着现实收缩,不然它会慢慢变成一张什么都放行的白名单")
    assert len(_LOCK_EXEMPT_LEGACY_RUNNERS) == 2, (
        f"豁免集大小变了({sorted(_LOCK_EXEMPT_LEGACY_RUNNERS)})—— "
        "新增不抢锁的 runner 必须在这里显式表态")


def test_cache_entry_without_a_fingerprint_is_stale(fake_tree):
    fp, _ = M.criteria_fingerprint(["tests/pkg"])
    assert M.cache_is_fresh({"id": "X", "criteria_fp": fp}, fp) is True
    assert M.cache_is_fresh({"id": "X"}, fp) is False          # 闸之前写的旧条目
    assert M.cache_is_fresh({"id": "X", "criteria_fp": "别的"}, fp) is False


# ══════════════════════════════════════════════════════════════════════
# [工单 V4-C · C-3] 重放器的**合同判据**:基线脏必须停机 · DSN 必须喂全
# ══════════════════════════════════════════════════════════════════════
# 机制本身也得有判据。这三条钉的是两条机制**失效时的那个样子**:
#   · 基线里有红/非零 rc,runner 却接着跑 —— 脏基线会被当成正常值从每一发的
#     「新增红」里扣掉,把该抓住的变异显示成"存活/红集精确";
#   · 判据包读的某个 DSN 变量没被喂 —— 那些判据落到各自的**默认**库
#     (往往是别窗容器),而全绿/条数/两臂差值四个信号全部正常。
def _load_script(fname: str, modname: str):
    import importlib.util
    import sys

    path = M.ROOT / "scripts" / fname
    if not path.exists():
        pytest.skip(f"{fname} 不在本树上")
    sys.path.insert(0, str(M.ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def _v4c_runner():
    return _load_script("mutation_replay_v4c_2026_08_28.py", "_v4c_replayer")


def _common():
    """🔴 [V5-B] 这三条闸原来长在 V4-C 身上,而 V3-C 抄的时候没抄上去 ——
    「同一谓词写两处 ⇒ 必有一处没人验」。闸已搬进公共模块,判据跟着搬;
    覆盖面由 test_mutation_replayer_hardgate_contract 机械枚举全部复放器。
    """
    return _load_script("mutation_replay_common.py", "_mr_common")


def test_a_red_baseline_stops_the_run_before_any_mutation():
    """基线里但凡有一条红,或 rc 非零 ⇒ 必须报告出来(调用方据此停机)。

    🔴 三种脏各打一发:红非空 / rc=1(有失败)/ rc=5(一条都没收集到)。
       rc 那两种尤其要打:junit 里可能**一条红都没有**,而那正是
       "整包起不来"的样子 —— 只看 red 集合会把它当成干净基线。
    """
    r = _common()
    assert r.baseline_offenders({"a": (0, set())}) == [], "干净基线被误判成脏"
    assert r.baseline_offenders({"a": (0, {"test_x"})}), "基线有红却没被拦"
    assert r.baseline_offenders({"a": (1, set())}), "rc=1 却没被拦"
    assert r.baseline_offenders({"a": (5, set())}), (
        "rc=5(一条判据都没收集到)却没被拦 —— 那是整包起不来的样子")
    # 多个包时,offender 必须逐个列出来(只说"有脏"没法判该修哪个)
    many = r.baseline_offenders({"a": (0, set()), "b": (1, set()), "c": (0, {"t"})})
    assert len(many) == 2 and any("b(" in m for m in many) and any("c(" in m for m in many), many


def test_the_runner_declares_every_dsn_var_each_package_reads():
    """每个包读的**每一个** DSN 变量都必须在 runner 的 env 清单里。

    🔴 这条闸的由来是一次真事故:w4 的 HTTP 判据只认 ``DEFGEO_W4_HTTP_DB_URL``,
       上一轮我按惯例只喂了另外两个,于是那 9 条判据整轮打在别窗的 55475 上。
       惯例名靠不住,所以分母是**从源码 AST 枚举**出来的。
    """
    c = _common()
    r = _v4c_runner()
    holes = {}
    for pkg, spec in r.PACKAGES.items():
        need = c.package_dsn_vars(M.ROOT, spec["path"])
        need.discard("DATABASE_URL")          # _run 无条件设置
        missing = sorted(need - c.env_vars_of(spec))
        if missing:
            holes[pkg] = missing
    assert not holes, f"这些包有 DSN 变量没被喂:{holes}"
    # 反向自证:枚举器真的抓得到东西,否则上面那条在空分母上恒绿
    w4 = c.package_dsn_vars(M.ROOT, r.PACKAGES["w4"]["path"])
    assert "DEFGEO_W4_HTTP_DB_URL" in w4, (
        f"枚举器没抓到 w4 的 HTTP DSN —— 探针失效:{sorted(w4)}")


def test_the_startup_selfcheck_separates_residue_from_expired_anchors():
    """起跑自证必须把**变异残留**与**锚点过期**分开报。

    🔴 两者的处置完全相反:残留要还原树,过期要退役/改锚。
       上一版合成一句「上一次跑可能被中途杀掉」——§2 退役之后,
       V3-C 那个 runner 就是拿这句**错的**诊断把人引向去找不存在的残留。
       (本仓记过:存量红先分三种 —— 真缺陷/锚点过期/尺子坏。)
    """
    c = _common()
    # 🔴 [V5-B] 分流依据换了:原来是「替换文本在不在」,而那正是 P1-8②/P2-4
    #    两个洞的来源 —— anchor-preserving 的替换判不出残留,空串替换
    #    (`repl in text` 恒真/无意义)把残留报成过期。现在按「与 HEAD 逐字节
    #    是否相同」分流,两个洞由同一个信号关掉。
    #    这条判据也从"读源码字符串"改成**行为向**:源码串证明不了分流真的发生。
    assert c.classify_target_state(differs_from_head=True, anchor_hits=1) == c.RESIDUE
    assert c.classify_target_state(differs_from_head=True, anchor_hits=0) == c.RESIDUE
    assert c.classify_target_state(differs_from_head=False, anchor_hits=0) == c.EXPIRED
    assert c.classify_target_state(differs_from_head=False, anchor_hits=1) == c.CLEAN
    import inspect

    src = inspect.getsource(c.assert_tree_is_unmutated) + inspect.getsource(
        c.classify_target_state)
    assert "锚点过期" in src and "变异残留" in src, (
        "起跑自证没有把两种原因分开报 —— 诊断错了比不报还费时间")
