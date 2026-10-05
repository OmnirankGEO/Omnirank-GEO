# -*- coding: utf-8 -*-
"""变异引擎**行尾合同**的判据。

起因(2026-08-27 与窗口A 对账):EXTE2-07/08 两轮 after-sha 对不上。
两边的组装**算出来逐字节相同**(在同一份底文件上实测),窗A 的报数能复现、
门9 的复现不出 —— 差的不是组装,是门9 这边多走了一步:

    repl, _ = _fit_newlines_bytes(original, mut["to"])

`_fit_newlines_bytes` 靠 ``raw.count(s)`` 决定 LF/CRLF。对**锚**成立(锚必在
文件里);对**替换**不成立 —— 替换按定义不在文件里,两个计数恒 0,
``counts["crlf"] == 1`` 永假 ⇒ **永远落回 LF 分支**。多行替换于是被写成 LF
注进 CRLF 文件,盘上留下混合行尾。

裁定没被翻(那两发两轮都是"存活"),但:
  · **在盘证据与别人的对不上**,两份证据就不能合并引用;
  · 混合行尾在本仓有前科 —— 同一份文件两种读取视图 ⇒ 预检绿、实跑红。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_engine_eol_contract.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "scripts" / "mutation_runner_extsel_2026_08_26.py"

_spec = importlib.util.spec_from_file_location("eol_engine", ENGINE)
_E = importlib.util.module_from_spec(_spec)
sys.modules["eol_engine"] = _E
sys.path.insert(0, str(ENGINE.parent))
_spec.loader.exec_module(_E)

CRLF_FILE = b"a = 1\r\nb = 2\r\nc = 3\r\n"
LF_FILE = b"a = 1\nb = 2\nc = 3\n"


def _lone_lf(b: bytes) -> int:
    return b.count(b"\n") - b.count(b"\r\n")


def test_eol_01_multiline_repl_into_a_crlf_file_gets_crlf():
    """CRLF 文件 + CRLF 锚 + 多行替换 ⇒ 替换必须也是 CRLF。

    这一条就是被咬的那一条:旧写法在这里给 LF。
    """
    needle = b"b = 2\r\nc = 3"
    repl = _E._fit_repl_bytes(CRLF_FILE, needle, "b = 9\nc = 8")
    assert _lone_lf(repl) == 0, f"替换里有孤立 LF:{repl!r}"
    assert repl == b"b = 9\r\nc = 8"


def test_eol_02_multiline_repl_into_an_lf_file_stays_lf():
    needle = b"b = 2\nc = 3"
    repl = _E._fit_repl_bytes(LF_FILE, needle, "b = 9\nc = 8")
    assert b"\r" not in repl
    assert repl == b"b = 9\nc = 8"


def test_eol_03_single_line_anchor_falls_back_to_the_file_eol():
    """锚是单行 ⇒ 锚本身不带行尾信息,只能看文件整体。"""
    assert _E._fit_repl_bytes(CRLF_FILE, b"b = 2", "b = 9\nx = 0") == b"b = 9\r\nx = 0"
    assert _E._fit_repl_bytes(LF_FILE, b"b = 2", "b = 9\nx = 0") == b"b = 9\nx = 0"


def test_eol_04_mixed_eol_file_with_a_single_line_anchor_stops():
    """混合行尾 + 单行锚 + 多行替换 ⇒ 行尾无从推断,**停机**不猜。

    猜错的代价是在盘上留混合行尾 —— 那正是这条合同要防的东西。
    """
    mixed = b"a = 1\r\nb = 2\nc = 3\r\n"
    with pytest.raises(SystemExit):
        _E._fit_repl_bytes(mixed, b"b = 2", "b = 9\nx = 0")


def test_eol_05_the_real_shot_that_caught_it_writes_no_lone_lf():
    """回归样本 —— **点名规则**:MUT-EXTE2-07 打 api/defensive_geo_api.py。

    该文件是全仓少数纯 CRLF 的 .py 之一。旧写法在这里注进 1 个孤立 LF。
    """
    target = ROOT / "api" / "defensive_geo_api.py"
    raw = target.read_bytes()
    if _lone_lf(raw) != 0 or raw.count(b"\r\n") == 0:
        pytest.skip("底文件已不是纯 CRLF —— 这条回归样本的前提没了,不空跑")
    # 🔴 [2026-08-28 重锚] V4-A 给这条 SQL 加了 `AND is_active`,原锚随之过期。
    #    判据当时报的正是「锚点过期 —— 这条样本失效了,**要重锚不是放行**」,照做。
    #    样本的用途一字未改:真·多行替换打真·纯 CRLF 文件,断言零孤立 LF。
    frm = ('        "FROM feature_pricing WHERE feature_code=%s AND is_active FOR SHARE",\n'
           '        (str(feature_code),),')
    to = ('        "FROM feature_pricing WHERE feature_code=%s AND is_active FOR SHARE",\n'
          '        ("geo_diagnosis",),')
    needle, _c = _E._fit_newlines_bytes(raw, frm)
    assert raw.count(needle) == 1, "锚点过期 —— 这条样本失效了,要重锚不是放行"
    repl = _E._fit_repl_bytes(raw, needle, to)
    mutated = raw.replace(needle, repl, 1)
    assert _lone_lf(mutated) == 0, (
        f"变异后出现 {_lone_lf(mutated)} 个孤立 LF —— 多行替换又被写成 LF 了")


def _bad_callsites(src: str) -> list[str]:
    """找出「把替换串喂给 `_fit_newlines_bytes`」的调用点。"""
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name != "_fit_newlines_bytes":
            continue
        for arg in node.args[1:]:
            seg = ast.get_source_segment(src, arg) or ""
            if '"to"' in seg or "'to'" in seg:
                bad.append(seg)
    return bad


def test_eol_06_no_caller_feeds_a_replacement_to_the_anchor_fitter():
    """结构锁 + 正样本:全仓不许再有 `_fit_newlines_bytes(..., mut["to"])`。"""
    # 🔴 正样本先跑 —— 锁不会响的话,下面那句"全仓干净"没有任何含义。
    probe = 'repl, _ = _fit_newlines_bytes(original, mut["to"])\n'
    assert _bad_callsites(probe), "结构锁自己就抓不到坏形态 —— 它是装饰"

    offenders = []
    for py in sorted((ROOT / "scripts").glob("*.py")):
        if py.name == pathlib.Path(__file__).name:
            continue                      # 本文件自带正样本,会自我命中
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "_fit_newlines_bytes" not in src:
            continue
        for seg in _bad_callsites(src):
            offenders.append(f"{py.name}: {seg}")
    assert not offenders, (
        "这些调用点在用**锚**的行尾规则拼**替换**:\n  " + "\n  ".join(offenders))
