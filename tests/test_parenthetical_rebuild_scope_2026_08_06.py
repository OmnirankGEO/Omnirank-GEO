"""存量重建脚本的**作用域**锁 · 括号地名 P0(复审 P0 返工)· 2026-08-06。

🔴🔴 存在的理由(复审实测,不是设想):第一版脚本遍历三份诊断里**几乎所有**
非人工裁决格重判,允许 `NO→YES` 也允许 `YES→PENDING`,没有污染清单、
没有旧值血缘、没有预期变更集。那等于拿一个"修括号地名"的包,把三份报告的
分数整体重算一遍 —— resolver 里任何一处别的改动都会顺带改分,而且没人会发现。
**数据完整性 H0。**

本文件锁的是"只动该动的那几格":
  · 清单内(旧 YES + 旧 matched_text 已不再可信)→ 允许改,且只允许 YES → 非 YES;
  · 清单外 → **一格不碰**;若它也会变,整份中止(不写库),交人看。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from services.brand_identity_resolver import BrandIdentity

# 🔴 按**文件路径**加载,不 `import scripts.xxx` —— 后者要给 scripts/ 加
#    `__init__.py`,那是把整个目录变成包、影响全仓导入的仓库级改动,
#    为了一个测试不值当。
_SPEC = importlib.util.spec_from_file_location(
    "_rebuild_parenthetical_2026_08_06",
    Path(__file__).resolve().parent.parent
    / "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

UnexpectedChange = _MOD.UnexpectedChange
_pollution_signature = _MOD._pollution_signature
_rejudge_cells = _MOD._rejudge_cells

#: 修复后的身份:括号城市已不再是可信别名
IDENTITY = BrandIdentity(
    brand_id=629, canonical_names=("全域上榜（深圳）科技有限公司",),
)


def _cell(verdict: str, matched: str | None, answer: str, **extra) -> dict:
    cell = {
        "brand_verdict": verdict,
        "brand_detected": verdict == "YES",
        "full_response": answer,
        "matched_text": matched,
    }
    cell.update(extra)
    return cell


def _table(cells: list[tuple[str, dict]]) -> dict:
    return {"detail_table": [
        {"question": f"问题{i}", "results": {engine: cell}}
        for i, (engine, cell) in enumerate(cells)
    ]}


# 🔴 [返工 2026-08-07] 全部补上 `detection_method`。
#   新判据先看**可重放性**(落库判定是不是本地层产出的),缺这个字段的格
#   一律 fail-closed 判不可重放 → 不评判、不改写、也不中止。
#   生产格**永远带** detection_method,所以不带的 fixture 本来就不真实 ——
#   规则改了,测试跟着改(而不是把判据放宽去迁就旧 fixture)。
#: 污染格:靠 matched_text="深圳" 判 YES(563 的 17/32 就是这个形态)
POLLUTED = _cell("YES", "深圳", "深圳有多家 GEO 服务商，可按交付能力比较。",
                 detection_method="trusted_exact")
#: 干净的 YES:命中的是真可信名
CLEAN_YES = _cell("YES", "全域上榜（深圳）科技有限公司",
                  "推荐全域上榜（深圳）科技有限公司，可进一步了解其 GEO 服务。",
                  detection_method="trusted_exact")
#: 干净的 NO
CLEAN_NO = _cell("NO", None, "推荐别家服务商。", detection_method="deterministic")


def test_pollution_signature_only_flags_the_polluted_cell():
    """【必须命中/不命中成对】污染指纹只认"旧 YES + 旧命中文本已不可信"。"""
    assert _pollution_signature(POLLUTED, IDENTITY) == "深圳"
    assert _pollution_signature(CLEAN_YES, IDENTITY) is None, "真命中不该进清单"
    assert _pollution_signature(CLEAN_NO, IDENTITY) is None, "NO 格不该进清单"


def test_only_polluted_cells_are_rewritten():
    """🔴【必须命中 · 复审 P0】清单内改,清单外一格不碰。"""
    ai = _table([("dashscope", dict(POLLUTED)),
                 ("deepseek", dict(CLEAN_YES)),
                 ("doubao", dict(CLEAN_NO))])
    stats = _rejudge_cells(ai, IDENTITY)

    assert stats["rewritten"] == 1, f"只该改 1 格,实改 {stats['rewritten']}"
    rows = [list(item["results"].values())[0] for item in ai["detail_table"]]
    polluted_after, clean_yes_after, clean_no_after = rows

    assert polluted_after["brand_verdict"] != "YES", "污染格必须被纠正"
    assert polluted_after["matched_text"] != "深圳", (
        "旧命中文本必须清掉 —— 留着就是把真凶继续挂在报告上"
    )
    assert clean_yes_after["brand_verdict"] == "YES", "真命中格不许被动"
    assert clean_yes_after["matched_text"] == CLEAN_YES["matched_text"]
    assert clean_no_after["brand_verdict"] == "NO", "NO 格不许被动"


def test_change_set_carries_old_value_lineage():
    """【必须命中 · 复审 P0】每笔改动都要留旧值血缘(可事后核对/回溯)。"""
    ai = _table([("dashscope", dict(POLLUTED))])
    stats = _rejudge_cells(ai, IDENTITY)
    change = stats["changes"][0]
    for key in ("before", "after", "old_matched_text", "response_sha256",
                "old_detection_reason"):
        assert key in change, f"变更集缺 {key}"
    assert change["before"] == "YES" and change["after"] != "YES"


def test_human_decided_cells_are_never_touched():
    """🔴【必须不命中】人工已决格,机器不许推翻。"""
    decided = dict(POLLUTED)
    decided["identity_review_state"] = "confirmed"
    ai = _table([("dashscope", decided)])
    stats = _rejudge_cells(ai, IDENTITY)
    assert stats["human_skipped"] == 1
    assert stats["rewritten"] == 0
    assert list(ai["detail_table"][0]["results"].values())[0]["brand_verdict"] == "YES"


def test_out_of_scope_change_aborts_instead_of_silently_rewriting():
    """🔴🔴【必须命中 · 复审 P0 核心】清单外的格会变 → 整份中止,不写库。

    构造:一格旧判 NO,但按当前身份重判会变成 YES(= 本包影响面超出
    "括号地名污染")。这种情况**绝不允许静默顺带改分**。
    """
    # 可重放(本地方法)+ 清单外 + 重放后确实变了 = **真意外**
    would_flip = _cell("NO", None,
                       "推荐全域上榜（深圳）科技有限公司，交付能力不错。",
                       detection_method="trusted_exact")
    ai = _table([("dashscope", would_flip)])
    with pytest.raises(UnexpectedChange) as excinfo:
        _rejudge_cells(ai, IDENTITY)
    assert "NO" in str(excinfo.value) and "YES" in str(excinfo.value)


def test_abort_guard_is_not_trigger_happy():
    """【反向对照】清单外**不变**的格不许触发中止。

    没有这条,把守卫写成"只要有清单外的格就中止"也能让上一条绿 ——
    那样脚本永远跑不完,等于没有重建能力。
    """
    ai = _table([("dashscope", dict(POLLUTED)), ("deepseek", dict(CLEAN_NO))])
    stats = _rejudge_cells(ai, IDENTITY)  # 不抛
    assert stats["out_of_scope_stable"] == 1


def test_script_never_touches_billing_or_providers():
    """🔴【必须不命中】重建脚本零模型调用、零扣费(静态自证)。"""
    import re

    src = (
        Path(__file__).resolve().parent.parent
        / "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py"
    ).read_text(encoding="utf-8")
    stripped = re.sub(r'"""[\s\S]*?"""', "", src)
    stripped = re.sub(r"^\s*#.*$", "", stripped, flags=re.M)
    for sink in ("point_transactions", "freeze_points", "deduct_points",
                 "commit_charge", "wallet_db", "httpx", "dashscope"):
        assert sink not in stripped, f"重建脚本出现了 {sink}"
    assert "resolve_local" in stripped, "必须只走纯本地重判"
    assert ".resolve(" not in stripped, "不许走会调 provider 的 resolve()"
