"""行业筛选归一化 —— 映射完备性锁 + 语义不缩水对照 + 空行业隐藏。

WO_PUBLISH_DISPATCH_STATUS_AND_INDUSTRY_2026-08-17 Part② 判据 2/3/4。

分母是**生产目录全量 distinct 行业串**
(`tests/fixtures/media_industry_catalog_prod_2026_08_17.json` ·
2026-08-17 只读探针取数:mhz_wemedia 243 个 · mhz_short_video 20 个)。
拿真数据当分母的理由:手挑几个例句的夹具与实现同构盲区,互相验不出来
(本仓 artrec R4/R5 连栽两次)。

锁表:
  D1  映射完备:目录全量成分 **未映射数 = 0**(漏一个红 · 照枚举锁定式)
  D1b 反向对照:塞一个目录里没有的成分 → `unmapped_components` 必须抓到
      (证明 D1 不是恒真)
  D2  语义不缩水:对每个 L1 大类 K,旧口径下「成分里含 K 字样」的原始串
      必须**全部**落在新口径 K 的结果集里(⊇ 关系)
  D3  chip 上限:facet 出来的大类数 ≤ 25 且每个 count > 0
  D4  空行业隐藏 —— 成对:0 家的大类不出现 / 有家的出现
  D5  不许把原始串漏回 UI:facet 的 key 只能是 L1,不能是斜杠组合
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.media_industry_taxonomy import (  # noqa: E402
    L1_ORDER, OTHER_L1, facet_counts, l1_keys_for, raw_values_for_l1,
    split_components, unmapped_components,
)

FIXTURE = ROOT / "tests" / "fixtures" / "media_industry_catalog_prod_2026_08_17.json"


def _catalog() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _all_raws() -> list[str]:
    cat = _catalog()
    return ([r["industry"] for r in cat["mhz_wemedia"]]
            + [r["industry"] for r in cat["mhz_short_video"]])


# ── D1 映射完备性 ───────────────────────────────────────────────────────────
def test_D1_every_production_component_is_mapped():
    raws = _all_raws()
    # 反向对照:分母不能是空的,否则 unmapped==[] 恒真
    assert len(raws) == 263, f"目录夹具行数变了({len(raws)}),先确认是不是换批次了"
    comps = {c for r in raws for c in split_components(r)}
    assert len(comps) >= 80, f"成分只有 {len(comps)} 个,夹具可能没拆开"
    missing = unmapped_components(raws)
    assert missing == [], (
        "目录里有成分没映射到 L1,它们只能靠「其他」兜底 —— 补 _COMPONENT_TO_L1:\n"
        + "\n".join(missing))


def test_D1b_unmapped_detector_has_discriminating_power():
    """反向对照:塞一个目录里不存在的成分,检测器必须报出来。"""
    fake = "量子飞升/汽车"
    assert unmapped_components([fake]) == ["量子飞升"]
    # 而它整串仍能落进「汽车」,不会因为一个陌生成分整条消失
    assert "汽车" in l1_keys_for(fake)


def test_D1c_no_raw_string_falls_through_to_other_by_accident():
    """全目录里落到「其他」的原始串,必须是**显式登记**过的那两类垃圾值。"""
    fell = {r for r in set(_all_raws()) if l1_keys_for(r) == (OTHER_L1,)}
    assert fell == {"北京", "其他"}, f"意外落进「其他」的串:{sorted(fell)}"


# ── D2 语义不缩水 ───────────────────────────────────────────────────────────
def test_D2_no_semantic_shrink_versus_old_literal_reading():
    """旧口径下「标签成分里含大类字样」的媒体,新口径必须一个不少地收进来。

    工单原话:选 L1「家居」返回的媒体 ⊇ 旧口径下所有含「家居」成分标签的媒体。
    这里把它做成对**每一个** L1 大类的机械对照,而不是手挑「家居」一个例子。
    """
    raws = sorted(set(_all_raws()))
    checked = 0
    for key in L1_ORDER:
        if key == OTHER_L1:
            continue
        old_hits = {r for r in raws if any(key in c for c in split_components(r))}
        if not old_hits:
            continue          # 该大类没有同名成分,这一轮无对照物,跳过
        checked += 1
        new_hits = set(raw_values_for_l1(raws, key))
        lost = old_hits - new_hits
        assert not lost, f"大类「{key}」把这些原始串弄丢了:{sorted(lost)}"
    # 反向对照:上面这个循环必须真的比对过若干个大类,不能一个都没进
    assert checked >= 15, f"只对照了 {checked} 个大类,判据没打到分母上"


def test_D2b_家居_case_from_the_work_order():
    """工单点名的那个例子,单独钉一遍(实测该大类由 4 种原始成分喂进来)。"""
    raws = sorted(set(_all_raws()))
    got = set(raw_values_for_l1(raws, "家居"))
    for expect in ("家居", "房产家居", "家居家装", "家居家装/综合", "房产"):
        assert expect in got, f"「家居」漏了原始串:{expect}"


# ── D3 / D5 chip 形态 ───────────────────────────────────────────────────────
def test_D3_chip_count_within_budget_and_all_nonzero():
    cat = _catalog()
    for table in ("mhz_wemedia", "mhz_short_video"):
        chips = facet_counts([(r["industry"], r["count"]) for r in cat[table]])
        assert 0 < len(chips) <= 25, f"{table} chip 数 {len(chips)} 超预算"
        assert all(c["count"] > 0 for c in chips), f"{table} 有 0 计数 chip 被下发"


def test_D5_chip_keys_are_l1_only_no_raw_slash_labels():
    cat = _catalog()
    for table in ("mhz_wemedia", "mhz_short_video"):
        for chip in facet_counts([(r["industry"], r["count"]) for r in cat[table]]):
            assert "/" not in chip["key"], f"{table} 把原始组合串漏回 chip:{chip['key']}"
            assert chip["key"] in L1_ORDER


def test_D5b_unmapped_component_falls_to_other_never_leaks_raw_text():
    """🔴 禁止 `|| 原串` 兜底 —— 未映射成分只能落「其他」,不许以原文形态漏回 chip。

    这条**必须用目录里不存在的成分**来打:生产全量已经 100% 映射,
    拿真数据探这条分支根本走不到(第一版变异 M9 就是因此存活的)。
    """
    novel = "量子飞升/星际漫游"
    assert l1_keys_for(novel) == (OTHER_L1,), f"未映射串泄回原文:{l1_keys_for(novel)}"
    chips = facet_counts([(novel, 7)])
    assert chips == [{"key": OTHER_L1, "count": 7}]
    for chip in chips:
        assert chip["key"] in L1_ORDER and "/" not in chip["key"]
    # 成对反向对照:同一串里补一个已映射成分,就不再落「其他」
    assert l1_keys_for("量子飞升/汽车") == ("汽车",)


def test_D3b_counts_are_conserved_not_invented():
    """每个大类的计数 = 归属它的原始串计数之和 —— 不是拍出来的数。"""
    cat = _catalog()
    pairs = [(r["industry"], r["count"]) for r in cat["mhz_wemedia"]]
    chips = {c["key"]: c["count"] for c in facet_counts(pairs)}
    for key, count in chips.items():
        expect = sum(n for raw, n in pairs if key in l1_keys_for(raw))
        assert count == expect, f"「{key}」计数 {count} != 逐串求和 {expect}"


# ── D4 空行业隐藏(成对) ────────────────────────────────────────────────────
def test_D4_zero_bucket_hidden_nonzero_bucket_rendered():
    # 当前结果集里只有汽车类媒体 → 只出「汽车」一个 chip
    only_auto = facet_counts([("汽车/综合", 3)])
    assert [c["key"] for c in only_auto] == ["汽车", "综合"]
    assert "萌宠" not in [c["key"] for c in only_auto], "0 家的大类被渲染了"
    # 成对:结果集里加进萌宠媒体 → 它必须出现
    with_pet = facet_counts([("汽车/综合", 3), ("萌宠/测评", 2)])
    keys = [c["key"] for c in with_pet]
    assert "萌宠" in keys and "生活" in keys      # 测评 → 生活
    assert dict((c["key"], c["count"]) for c in with_pet)["萌宠"] == 2


def test_D4b_empty_industry_never_occupies_a_chip():
    """没填行业的媒体不该被算进任何大类(包括「其他」)。"""
    assert l1_keys_for("") == ()
    assert l1_keys_for(None) == ()
    assert facet_counts([("", 999)]) == []


def test_D6_chip_order_is_stable_across_filter_changes():
    """切平台重算时 chip 不该乱跳 —— 顺序恒为 L1_ORDER 的子序列。"""
    a = [c["key"] for c in facet_counts([("汽车", 5), ("萌宠", 1), ("生活/综合", 9)])]
    assert a == [k for k in L1_ORDER if k in set(a)]
