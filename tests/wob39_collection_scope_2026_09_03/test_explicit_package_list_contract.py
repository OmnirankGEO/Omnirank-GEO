# -*- coding: utf-8 -*-
"""#48 显式名单合同 —— 守「`DISK_PACKAGES` 是 SSOT,COUNT/SHA256 由它派生」。

立卡时我写的理由(「两个常量会互相漂开」)**是错的**,核过:
只改 COUNT 不改 SHA256,hash 断言会红,不会静默漂。真理由是两条:
  ① hash 说得出「不一样」,说不出**「差的是哪个」**
  ② **让登记提交可核** —— diff 自己说出 `+ "pkg_name",`,
     而不是两个数字加两串哈希、复核的人只能自己跑一遍再对
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import contextlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE9 = ROOT / "scripts" / "gate9_full_denominator_baseline.py"

def _load():
    spec = importlib.util.spec_from_file_location("_g48", GATE9)
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m


# ══ ① 改表示法不改集合:派生值必须逐字等于改前 ═══════════════════════
def _name_the_difference(m) -> str:
    """🔴 [#50 规则] 报文必须带上失败原因 —— 这里的「原因」是**差的是哪个名字**。

    2026-09-04 Review 下毒(往名单里插一个盘上没有的名字)时发现:
    先响的两条锁只给哈希和「首个乱序位置」,**都不点名** ——
    幽灵名只出现在第三条 `w48_03` 的报文里。看第一个失败就会错过。

    这是我**今早刚立的 #50 规则,下午写这批锁时自己违反了**。
    规则不会因为写下来就自动生效;每条报文都得单独问一次「原因带上了吗」。
    """
    listed, disk = set(m.DISK_PACKAGES), set(m.disk_package_names())
    extra, gone = sorted(disk - listed), sorted(listed - disk)
    if extra or gone:
        return f"名单相对盘上:多了 {gone} · 少了 {extra}"
    return ("名单与盘上一致 ⇒ 变的不是集合,而是集合之外的东西 —— "
            "多半是有人改了派生算法(COUNT / SHA256 不再从 DISK_PACKAGES 算)")


def test_w48_01_the_constants_are_actually_derived_from_the_list():
    """🔴 [2026-09-04 二次] 原判据 `..._derived_sha_is_byte_identical_to_the_pre_change_pin`
    **已退役**,继任者就是本条 + `w48_03`。

    原判据钉 `SHA_BEFORE_48`,证的是「#48 改表示法**没有改集合**」——
    那是一次**一次性迁移检查**。当天晚些时候 C 的 #30/#31 合法地加了两个判据包,
    集合变了,它的**前提**随之不成立。
    这属于「正确的修复推翻了存在锁的前提」:处置是**退役并写明继任者**,
    不是把常量改成新值 —— 改了之后它只剩「跟上次一样」,与 `w48_03` 完全重复,
    而且会让人误以为它还在守「表示法未改集合」这件早已过去的事。

    本条守的是**永远成立**的那部分:COUNT / SHA256 确实由 DISK_PACKAGES 派生。
    「名单与盘上一致」由 `w48_03` 守;「差的是哪个」由 `_census_diag` 报。
    """
    import hashlib
    m = _load()
    assert m.DISK_PACKAGES_COUNT == len(m.DISK_PACKAGES), (
        f"COUNT={m.DISK_PACKAGES_COUNT} != len(名单)={len(m.DISK_PACKAGES)} —— 它没在派生")
    want = hashlib.sha256(chr(10).join(m.DISK_PACKAGES).encode("utf-8")).hexdigest()
    assert m.DISK_PACKAGES_SHA256 == want, (
        f"SHA256 与名单算出来的不符 —— 它没在派生。"
        f"得到 {m.DISK_PACKAGES_SHA256[:16]},名单算出 {want[:16]}。"
        f"\n    {_name_the_difference(m)}")


def test_w48_01b_the_derivation_actually_depends_on_the_list():
    """反向臂:名单改一个字符,派生值必须变 —— 否则上一条可能锁在一个常量上。"""
    m = _load()
    tampered = ("zz_" + m.DISK_PACKAGES[0],) + tuple(m.DISK_PACKAGES[1:])
    got = hashlib.sha256(chr(10).join(tampered).encode("utf-8")).hexdigest()
    assert got != m.DISK_PACKAGES_SHA256, "改了名单派生值却没变 ⇒ 派生是假的"


# ══ ② 名单本身的形状:sorted + 无重复(指纹依赖顺序)═══════════════
def test_w48_02_list_is_sorted_and_has_no_duplicates():
    m = _load()
    lst = list(m.DISK_PACKAGES)
    assert lst == sorted(lst), (
        f"名单未按 sorted 排列 —— 指纹是 sha256(chr(10).join(名单)),顺序变指纹就变。"
        f"首个乱序位置:{next((i for i in range(1, len(lst)) if lst[i] < lst[i-1]), None)}")
    dup = sorted({x for x in lst if lst.count(x) > 1})
    assert not dup, f"名单里有重复项:{dup}"


# ══ ③ 名单 == 盘上派生集合(只读版;种目录的破坏臂在毒脚本里)═══════
def test_w48_03_list_equals_the_derived_disk_set():
    m = _load()
    disk = set(m.disk_package_names())
    listed = set(m.DISK_PACKAGES)
    extra, gone = sorted(disk - listed), sorted(listed - disk)
    assert not extra and not gone, (
        f"名单与盘上不一致 —— 盘上多了 {extra} · 盘上少了 {gone}。"
        f"加包 = 建目录 + 在 DISK_PACKAGES 加一行,**两处显式决定**,漏一处就是这条红。")


# ══ ④ 失败报文必须给差集,不许只给指纹 ═════════════════════════════
def test_w48_04_the_failure_message_reports_the_diff_not_only_the_fingerprint():
    """#48 的核心价值就在这条 —— 报文若只给指纹,这单等于没做。"""
    src = GATE9.read_text(encoding="utf-8-sig")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "assert_denominator_frozen")
    body = ast.unparse(fn)
    assert "set(DISK_PACKAGES)" in body, "普查失败路径没有跟名单做差集"
    assert "盘上多了" in body and "盘上少了" in body, (
        "报文里没有「多了/少了」—— 只报指纹的话,读的人还得自己去跑一遍才知道差的是哪个")


# ══ ⑤ COUNT / SHA256 不许再被手写赋值 ══════════════════════════════
def _hand_assignments(src: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in ("DISK_PACKAGES_COUNT",
                                                    "DISK_PACKAGES_SHA256"):
                v = ast.unparse(node.value)
                # 派生形态:len(DISK_PACKAGES) / hashlib.sha256(...join(DISK_PACKAGES)...)
                if "DISK_PACKAGES" not in v:
                    out.append(f"L{node.lineno}: {t.id} = {v[:40]}")
    return out


def test_w48_05_count_and_sha_are_never_hand_assigned():
    left = _hand_assignments(GATE9.read_text(encoding="utf-8-sig"))
    assert not left, (
        f"COUNT / SHA256 仍有手写赋值:{left}。它们必须从 DISK_PACKAGES 派生 ——"
        f"手写的那一刻,名单就不再是 SSOT,而「哪个是真的」这个问题就回来了。")


def test_w48_05b_the_detector_would_flag_a_hand_assignment():
    """反向臂:喂合成的手写赋值,检测器必须点名。"""
    bad = 'DISK_PACKAGES_COUNT = 119\nDISK_PACKAGES_SHA256 = "abc"\n'
    good = ('DISK_PACKAGES = ("a",)\n'
            'DISK_PACKAGES_COUNT = len(DISK_PACKAGES)\n')
    assert len(_hand_assignments(bad)) == 2, "检测器对已知坏形状没反应 ⇒ 锁⑤恒绿"
    assert _hand_assignments(good) == [], "检测器把派生形态也算成手写 ⇒ 锁⑤会误伤"
