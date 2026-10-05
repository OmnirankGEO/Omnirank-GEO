# -*- coding: utf-8 -*-
"""WO_240 · 资金/对客路径上的 fallback 必须出声,且**行为零变化**。

本包钉四件事,分别对应四种已经发生过的失败:

1. **行为零变化** —— 包装一行取价最容易顺手"改好"它(`or {}`、把 None 当缺省),
   而那会改变扣费金额,两边看起来都对。
2. **走了就出声 / 没走就闭嘴** —— 反向臂必须证明"静默"不是因为仪器没跑。
3. **副本必须等于权威值** —— 六处字面量里有五处是目录值的副本;
   副本本身不是缺陷,**副本悄悄漂了没人知道**才是。
4. 🔴 **"它够不着"这个结论本身要有锁** —— 今天这些默认值打不出来,
   靠的是 `get_feature_pricing` 查不到就抛、`cost_points` 是 NOT NULL。
   哪天有人把它改成"查不到返 `{}`",这六处**当天全部变成活的另一条主路**,
   而不会有任何现象。所以把那两个结构性前提钉死在这里。
"""
from __future__ import annotations

import ast
import io
import json
import logging
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.fallback_observability import (       # noqa: E402
    MARKER, copy_diverged, fired, priced, raised,
)

LOGGER_NAME = "GEO-Fallback"


# ══════════════════════════════════════════════════════════════════
# 一、行为零变化:priced() 与 `mapping.get(key, default)` 逐字等价
# ══════════════════════════════════════════════════════════════════

EQUIV_CASES = [
    ({"cost_points": 80}, "cost_points", 390),      # 正常取到
    ({"cost_points": 0}, "cost_points", 390),       # 🔴 取到 0,不许被当成"没取到"
    ({"cost_points": None}, "cost_points", 390),    # 🔴 键在值为 None → .get 返 None,不是 390
    ({}, "cost_points", 390),                       # 键不在 → 390
    ({"other": 1}, "cost_points", 260),
    ({"cost_points": 130}, "cost_points", 130),     # 副本与权威值相等
]


@pytest.mark.parametrize("mapping,key,default", EQUIV_CASES)
def test_priced_returns_exactly_what_get_would(mapping, key, default):
    """🔴 本单的底线。这一条红了,说明我在"让它出声"的路上改了它扣多少钱。"""
    assert priced(dict(mapping), key, default,
                  feature="f", where="w") == mapping.get(key, default)


def test_a_none_mapping_still_raises_like_before():
    """🔴 取价返 None 时原地是 `pricing.get(...)` → AttributeError,**必须照旧炸**。

    顺手写成 `(pricing or {}).get(...)` 看着更健壮,实际是把 fail-fast 改成 fail-soft
    —— 正是本单要治的病。不许在"加观测"的名义下顺手做这个。
    """
    with pytest.raises(AttributeError):
        priced(None, "cost_points", 390, feature="f", where="w")


def _helpful_variant(mapping, key, default):
    """反向对照:一个**看起来更稳**的写法。它必须被上面的判据判红。"""
    return (mapping or {}).get(key, default) or default


def test_the_equivalence_criteria_can_actually_tell_the_difference():
    """🔴 正样本臂:没有这一条,上面那组全绿可能只是"怎么写都过"。

    `_helpful_variant` 正是最常见的"顺手改好":它把 None/0 都吃成 default,
    也把 None mapping 吃成不抛。判据必须至少在一个用例上把它判红。
    """
    diffs = [c for c in EQUIV_CASES
             if _helpful_variant(dict(c[0]), c[1], c[2]) != c[0].get(c[1], c[2])]
    assert diffs, "等价判据对「顺手改好」的写法没有分辨力"
    raised_ok = True
    try:
        _helpful_variant(None, "cost_points", 390)
        raised_ok = False
    except AttributeError:
        pass
    assert not raised_ok, "反向对照居然也抛了 —— 它没能扮演「被改宽」的角色"


# ══════════════════════════════════════════════════════════════════
# 二、走了就出声 / 没走就闭嘴
# ══════════════════════════════════════════════════════════════════

def _marker_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(MARKER)]


def test_taking_the_hardcoded_value_emits_exactly_one_structured_line(caplog):
    """正面臂:取到写死值 ⇒ 恰好一行,且字段齐。"""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        got = priced({}, "cost_points", 390,
                     feature="topic_gen", where="server.py:api_generate_titles")
    assert got == 390
    lines = _marker_lines(caplog)
    assert len(lines) == 1, lines
    payload = json.loads(lines[0][len(MARKER):].strip())
    assert payload["feature"] == "topic_gen"
    assert payload["where"] == "server.py:api_generate_titles"
    assert payload["key"] == "cost_points"
    assert payload["used"] == 390
    assert payload["reason"] == "missing_key"


def test_the_normal_path_says_nothing_at_all(caplog):
    """🔴 反向臂。没有这一条,上面那条可能只是「它对什么都喊」。"""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        got = priced({"cost_points": 80}, "cost_points", 390,
                     feature="topic_gen", where="server.py:api_generate_titles")
    assert got == 80
    assert _marker_lines(caplog) == []


def test_lookup_raised_is_recorded_as_a_different_reason(caplog):
    """「表里没这行」与「根本没连上表」 排查方向完全不同,不许压成一种。"""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        raised(feature="monitoring_keyword_daily",
               where="server.py:get_client_monitoring_config",
               key="cost_points", used=130, exc=RuntimeError("db down"))
    payload = json.loads(_marker_lines(caplog)[0][len(MARKER):].strip())
    assert payload["reason"] == "lookup_raised"
    assert payload["detail"] == "RuntimeError"


def test_copy_divergence_carries_both_numbers(caplog):
    """对客副本漂了,一行里必须同时有「显示的」和「目录的」 —— 少一个就没法判谁对。"""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        copy_diverged(feature="geo_diagnosis",
                      where="server.py:api_start_articles",  # 原例 social_agent 随开源 E3 B2 删,换在役发射点
                      used=650, catalog=700)
    payload = json.loads(_marker_lines(caplog)[0][len(MARKER):].strip())
    assert payload["used"] == 650
    assert "700" in payload["detail"]


def test_emitting_can_never_break_the_charging_path(monkeypatch):
    """🔴 出声本身崩了,扣费照走。

    「为了让问题可见而把付费路径搞崩」比问题本身更糟。
    """
    import services.fallback_observability as fo

    def _boom(*a, **k):
        raise RuntimeError("logging backend exploded")

    monkeypatch.setattr(fo.logger, "warning", _boom)
    assert fo.priced({}, "cost_points", 390, feature="f", where="w") == 390
    fo.fired(feature="f", where="w", key="k", used=1, reason="missing_key")


def test_no_dedup_every_entry_speaks(caplog):
    """🔴 不去重。

    去重会让"日志里查不到"同时意味着「没发生」和「发生过但被吞了」,
    而这两件事的处置相反(本仓 derive-the-emission-condition-before-reading-absence)。
    """
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _ in range(3):
            priced({}, "cost_points", 390, feature="topic_gen", where="w")
    assert len(_marker_lines(caplog)) == 3


# ══════════════════════════════════════════════════════════════════
# 三、结构性前提:这些默认值"够不着"这件事本身要有锁
# ══════════════════════════════════════════════════════════════════

WALLET_DB = REPO / "db" / "wallet_db.py"          # 🔴 保护文件 —— 本包只读它


def test_get_feature_pricing_still_raises_instead_of_returning_a_partial_dict():
    """🔴 六处 `.get("cost_points", <字面量>)` 今天打不出来,**全靠这一条**:

    `get_feature_pricing` 查不到行是 `raise ValueError`,不是返 `None`/`{}`;
    查得到就是 `SELECT *` 的整行。哪天有人把它改成「查不到返 {}」,
    那六处**当天全部变成活的另一条主路**,而不会有任何现象。

    (2026-09-18 我就是漏了这一条,把 36 笔真实扣费归因到一条跑不起来的路上。)
    """
    src = io.open(WALLET_DB, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "get_feature_pricing"), None)
    assert fn is not None, "get_feature_pricing 不见了 —— 本包的全部前提变了"
    body = ast.unparse(fn)
    assert "raise ValueError" in body, "查不到行不再抛异常 —— 六处 fallback 可能已变成活路"
    assert not re.search(r"return\s*(None|\{\})", body), \
        "出现了 `return None` / `return {}` —— fallback 默认值从此够得着"


def test_cost_points_is_a_not_null_column():
    """第二个前提:`cost_points` 是真列且 NOT NULL,所以 `SELECT *` 的行里一定有它。"""
    src = io.open(WALLET_DB, encoding="utf-8").read()
    assert re.search(r"cost_points\s+INTEGER\s+NOT\s+NULL", src), \
        "cost_points 不再是 NOT NULL 整数列 —— 键可能缺失,fallback 变成活路"


def test_updated_at_has_no_auto_update_trigger():
    """🔴 `updated_at` 只有 `DEFAULT CURRENT_TIMESTAMP`,**没有 ON UPDATE**。

    钉这一条不是为了代码,是为了**读数**:2026-09-18 我和 Review 各自拿
    `updated_at` 当「最后修改时间」用,一个据它预测、一个据它给 Owner 归因,
    都错。这一列的语义是「插入时刻」。
    """
    src = io.open(WALLET_DB, encoding="utf-8").read()
    m = re.search(r"updated_at\s+TIMESTAMP[^\n,]*", src)
    assert m, "feature_pricing.updated_at 定义找不到了"
    assert "ON UPDATE" not in m.group(0).upper(), (
        "updated_at 现在会自动更新了 —— 基于'它只记插入时刻'的历史归因都要重做")
