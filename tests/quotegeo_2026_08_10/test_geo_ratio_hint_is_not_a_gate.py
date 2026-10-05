"""T4 首条:`geo_ratio_hint` 只可作为**生成提示**,不能作为资格判据或验收配额。

🔴 诚实交代(交付单也写了):这一半在动手前**就已经是真的** ——
   `geo_ratio_hint` 在生产代码里只出现在两处:LLM prompt 的参考文案、
   以及响应 summary 里的统计字段。没有任何一条 if 读它来决定去留。
   所以本文件不是"修复的证明",是**防回退的锁**:谁把它接成配额,这里必红。

配的成对判据:
  · 正向 —— 把 geo_ratio_hint 改成 0 和 100 两个极端值,同一批候选的
    交付集合必须**逐字相同**(它不参与判定);
  · 反向 —— 同一手法改 market_level(真正参与判定的那个),交付集合必须**变化**。
    没有反向那一条,一个"永远返回同一批词"的坏实现也能让正向绿。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import services.quote_scope_lock as scope_lock_module
from tests.quotegeo_2026_08_10.test_quote_keyword_double_inversion_2026_08_10 import (
    run_expansion,
)

ROOT = Path(__file__).resolve().parents[2]

CANDIDATES = ["商场推荐", "深圳龙岗商场招商电话", "深圳龙岗建材市场有哪些"]


def _delivered(matrix: dict) -> set[str]:
    return {
        kw for kw, row in matrix.items()
        if kw != "__result__" and row.get("default_selected") is True
    }


@pytest.mark.parametrize("hint", [0, 100])
def test_geo_ratio_hint_does_not_change_the_delivered_set(monkeypatch, hint):
    baseline = _delivered(run_expansion(CANDIDATES))

    patched = {
        level: {**policy, "geo_ratio_hint": hint}
        for level, policy in scope_lock_module._GEO_POLICY.items()
    }
    monkeypatch.setattr(scope_lock_module, "_GEO_POLICY", patched)

    assert _delivered(run_expansion(CANDIDATES)) == baseline, (
        f"改 geo_ratio_hint={hint} 改变了交付集合 —— 它被当成资格判据/配额了(违反 T4)"
    )


def test_market_level_does_change_the_delivered_set():
    """反向对照:真正参与判定的旋钮一动,结果必须变 —— 证明上面那条不是恒真。"""
    local = _delivered(run_expansion(CANDIDATES))
    national = _delivered(run_expansion(
        CANDIDATES, market_level="national", service_market=[], city="",
    ))
    assert local != national, (local, national)
    assert "商场推荐" not in local
    assert "商场推荐" in national


def test_no_conditional_reads_geo_ratio_hint_in_the_decision_path():
    """元判据:三轴决策模块里**一次都不许**出现 geo_ratio_hint。

    (这一条是源码断言,但它锁的是"不存在",端到端测试没法证明不存在。)
    """
    text = (ROOT / "services" / "keyword_delivery_decision.py").read_text(encoding="utf-8")
    code_lines = [
        line for line in text.splitlines()
        if not line.strip().startswith("#")
    ]
    body = "\n".join(code_lines)
    # 文档字符串里提到它是允许的(说明它为什么不该被读),条件语句里不允许。
    hits = [
        line for line in body.splitlines()
        if "geo_ratio" in line and re.search(r"\b(if|elif|while|and|or|return)\b", line)
    ]
    assert not hits, hits


def test_expander_still_reports_the_hint_as_hint_only():
    """它仍然要出现在 summary 里(运营要看),但必须自带"只是提示"的标记。"""
    result = run_expansion(CANDIDATES)["__result__"]
    assert "geo_ratio_target" in result["summary"]
    assert result["summary"]["geo_ratio_is_hint_only"] is True
