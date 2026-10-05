# -*- coding: utf-8 -*-
"""caveat 缺失降为 A1 锁 · 返工单 §3

`_clamp_cards` 原来缺 `caveat` 直接丢整张卡,而丢卡会让
`len(cards) != content_count` → `card_count_mismatch` → **整单判废 + 退款**。
也就是说"模型少写了一句注意事项"这种 A1 级问题被级联成了整单硬失败 ——
与本单自己 R7 的裁定(caveat 属 A1)正面冲突,也违反永不中断铁律。

🔴 边界(返工单 §3 明列):`卖方口吻判废`**不在本轮范围** ——
那是 W4 已裁设计(不替客户改口吻 + 不扣费 + 一键重试),动它属推翻既有裁决。
本文件有一条反向对照专门盯着它没被顺手改掉。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services.geo_douyin import content_generator as cg


REPO = pathlib.Path(__file__).resolve().parent.parent
GEN = REPO / "services" / "geo_douyin" / "content_generator.py"


def _cards(n: int, with_caveat: bool = True) -> list:
    out = []
    for i in range(n):
        c = {"entity": f"E{i}", "one_liner": "L", "points": ["a", "b"], "metric": ""}
        if with_caveat:
            c["caveat"] = "旧楼加装需先评估"
        out.append(c)
    return out


def test_missing_caveat_no_longer_drops_the_card():
    """🔴 核心:缺 caveat 的卡**必须留下**,不能丢。"""
    out = cg._clamp_cards(_cards(3, with_caveat=False), 3)
    assert len(out) == 3, f"缺 caveat 仍在丢卡:只剩 {len(out)} 张"


def test_autofilled_caveat_is_a_legal_non_fabricating_fallback():
    out = cg._clamp_cards(_cards(1, with_caveat=False), 1)
    assert out[0]["caveat"] == cg.CAVEAT_FALLBACK
    # 反向面:兜底不能是编出来的事实(不含数字/资质/名次类断言)
    for bad in ("第一", "%", "认证", "资质", "获奖"):
        assert bad not in cg.CAVEAT_FALLBACK, f"兜底文案里混进了断言:{bad}"


def test_autofill_is_traceable():
    """留痕:前端要能分出"这条是自动补的"(A1 局部补齐的前提)。"""
    out = cg._clamp_cards(_cards(2, with_caveat=False) + _cards(1), 3)
    assert [c["caveat_autofilled"] for c in out] == [True, True, False]


def test_no_cascade_to_whole_order_failure():
    """🔴 端到端形状:全部缺 caveat 也能凑够张数 → 不会触发 card_count_mismatch。"""
    want = 5
    out = cg._clamp_cards(_cards(want, with_caveat=False), want)
    assert len(out) == want, "张数对不上就会级联成整单判废退款"


def test_structural_missing_still_drops():
    """反向对照:缺 entity / 缺 points 仍然丢卡 —— 那是结构性缺失,不在本轮范围。

    这条防的是"为了让上面那条过,把所有校验都删了"。
    """
    assert cg._clamp_cards([{"points": ["a"], "caveat": "c"}], 1) == []
    assert cg._clamp_cards([{"entity": "E", "caveat": "c"}], 1) == []


def test_real_caveat_is_not_overwritten():
    out = cg._clamp_cards(_cards(1), 1)
    assert out[0]["caveat"] == "旧楼加装需先评估"
    assert out[0]["caveat_autofilled"] is False


def test_clamp_never_raises_on_garbage():
    for bad in ([None], [{"entity": None}], [], None):
        assert isinstance(cg._clamp_cards(bad or [], 3), list)


# ===========================================================================
# 边界:W4 卖方口吻判废**不在本轮范围**,不许被顺手动掉
# ===========================================================================

def test_self_praise_verdict_is_untouched():
    """🔴 返工单 §3 明列的边界 —— `self_praise_voice` 判废是 W4 已裁设计。

    本轮只降 caveat,不许顺手把它也改成 A1(那属推翻既有裁决,要单独走 Owner)。
    """
    src = GEN.read_text(encoding="utf-8")
    assert 'error="self_praise_voice"' in src, "卖方口吻判废被动了 —— 超出本轮范围"


def test_brand_not_promoted_verdict_is_untouched():
    src = GEN.read_text(encoding="utf-8")
    assert 'error="brand_not_promoted"' in src


def test_caveat_is_not_in_any_hard_failure_reason():
    """反向对照:全模块不得再有以 caveat 为由的整单判废。"""
    tree = ast.parse(GEN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "GeneratedContent"):
            continue
        for kw in node.keywords:
            if kw.arg == "error" and isinstance(kw.value, ast.Constant):
                assert "caveat" not in str(kw.value.value), "caveat 又成了判废理由"


# ===========================================================================
# 2026-08-06 二次返工 · `entity_ref` 声明字段必须原样留下
# ===========================================================================

#: 🔴 必须**超过 12 字**,否则 `clip_text(…, 12)` 根本不会动它 ——
#:    这条锁就成了恒真。(我第一版用的 11 字名,变异"把声明也 clip 掉"
#:    从这个洞活着穿了过去。)
_LONG_NAME = "深圳市晨光富士电梯有限公司"


def test_entity_ref_is_not_truncated_by_clamp():
    """🔴 声明字段**不许**被截成 12 字。

    R1 拿它做集合成员判定 —— 截了就变成一个名单里查不到的短名,
    于是每一张卡都会被判成"名单外",A1 提示当场恒红。
    (卡面 `entity` 该截还是截:它是印在图上的,版面有限。)
    """
    cards = [{"entity": _LONG_NAME, "entity_ref": _LONG_NAME,
              "one_liner": "L", "points": ["a"], "caveat": "c"}]
    out = cg._clamp_cards(cards, 1)
    assert out[0]["entity_ref"] == _LONG_NAME, \
        f"entity_ref 被截断了:{out[0]['entity_ref']}"
    assert len(out[0]["entity"]) <= 12, "卡面 entity 反而没截"


def test_entity_ref_defaults_to_empty_for_card_group_orders():
    """反向对照:卡组型下模型不给这个键 —— 取值必须是空串,不是 None/缺键。

    空串 = "这张卡没声明讲哪一家" = R1 不管它。缺键会让下游 `.get()` 到 None,
    再 `str(None)` 就成了字面量 "None",那是个名单外的名字。
    """
    out = cg._clamp_cards(_cards(1), 1)
    assert out[0]["entity_ref"] == ""


def test_ranking_card_field_only_appears_for_ranking_orders():
    """输出格式行里的那个额外字段:卡组型必须是空串(prompt 逐字不变)。"""
    assert cg.ranking_card_field(None) == ""
    assert "entity_ref" in cg.ranking_card_field(object())
