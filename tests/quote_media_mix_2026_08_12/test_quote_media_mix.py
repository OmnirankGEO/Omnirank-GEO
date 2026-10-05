"""投放组合拆分锁（WO_QUOTE_MEDIA_MIX_DYNAMIC_2026-08-12 v2 · §7）。

🔴 判据设计口径（工单 §7 原文）：**测试从 fixture 的计数运行公式，不得在测试代码另写一套期望值。**
   所以本文件里没有任何 `assert anchor == 6` 这类魔法数字 —— 6 是从
   `fixture_v2.csv` 的 `expected_anchor` 列读出来的，而 `expected_anchor` 又必须能被
   同一行的原始计数 + 生产代码里的公式复算出来。两边任一侧改动都会当场红。

   反过来说：如果有人把公式改坏、同时把 fixture 的期望列也改成新值，这套锁抓不到。
   那种情况由「fixture 与裁定书同源文件逐字节一致」那条锁兜（见 test_fixture_not_drifted）。

v1 被判红的三处，本文各配一条**方向性**断言（不是复述结论，是真跑）：
  · 抖音必须先从覆盖侧扣掉 → test_douyin_is_subtracted_not_added
  · 未分类长尾不进任何一侧 → test_unclassified_excluded_from_ratio
  · 不存在 3–5 运营区间 → test_no_three_to_five_band
"""

from __future__ import annotations

import csv
import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.quote_media_mix import (  # noqa: E402
    MIN_ANCHOR_N,
    MIN_CELL_N,
    RATIO_CLAMP_MAX,
    RATIO_CLAMP_MIN,
    MediaMixPool,
    PoolCounts,
    _eligible,
    plan_media_mix,
    round_half_up,
)

FIXTURE = Path(__file__).with_name("fixture_v2.csv")
#: 裁定书同源的那一份（仓外）。存在时必须与仓内副本逐字节一致，防两边各改一半。
CANONICAL_FIXTURE = Path(r"C:\AI-Test\quote_media_mix_fixture_v2.csv")


def _rows() -> list[dict[str, str]]:
    with open(FIXTURE, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _counts(row: dict[str, str]) -> PoolCounts:
    g = lambda k: int(row[k])  # noqa: E731
    return PoolCounts(
        cell_n=g("source_n"),
        authoritative_n=g("authoritative_n"),
        portal_n=g("portal_n"),
        vertical_n=g("vertical_n"),
        self_media_n=g("self_media_n"),
        douyin_n=g("douyin_n"),
        unclassified_n=g("unclassified_n"),
    )


ROWS = _rows()
IDS = [r["scenario"] for r in ROWS]


def test_fixture_not_drifted():
    """仓内 fixture 必须与裁定书同源那份逐字节一致（仓外文件不在时跳过）。"""
    if not CANONICAL_FIXTURE.exists():
        pytest.skip("裁定书同源 fixture 不在本机，跳过防漂移比对")
    a = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    b = hashlib.sha256(CANONICAL_FIXTURE.read_bytes()).hexdigest()
    assert a == b, "仓内 fixture 与裁定书同源文件已漂移 —— 两边各改一半时这条会红"


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_layer_counts_are_self_consistent(row):
    """五层之和 == source_n，且抖音是覆盖侧的子集（不是第六层）。"""
    c = _counts(row)
    assert c.is_consistent, f"{row['scenario']}: 五层之和 != source_n"
    assert c.douyin_n <= c.vertical_n + c.self_media_n, "抖音必须是覆盖层的子集"
    assert c.coverage_n == int(row["coverage_n_excluding_douyin"])
    assert c.anchor_n == c.authoritative_n + c.portal_n


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_mix_matches_fixture(row):
    """核心：从 fixture 的**原始计数**跑生产公式，结果必须等于 fixture 的期望列。"""
    counts = _counts(row)
    industry, engine = row["industry"], row["engine"]
    scope = row["source_scope"]
    key = {
        "global": ("global", "", ""),
        "industry": ("industry", industry, ""),
        "industry_engine": ("industry_engine", industry, engine),
    }[scope]
    pool = MediaMixPool(rows={key: counts})
    # 抖音闸永远看「行业×豆包格」。比例回退到行业汇总时,那一格要单独补进池,
    # 否则测的就不是工单 §4.2 的真实口径(电梯场景正是这一支)。
    if scope == "industry" and engine == "doubao":
        pool.rows[("industry_engine", industry, "doubao")] = PoolCounts(
            cell_n=int(row["cell_n"]), portal_n=int(row["cell_anchor_n"]),
            vertical_n=int(row["cell_n"]) - int(row["cell_anchor_n"]),
            douyin_n=int(row["douyin_n"]),
        )
    engines = [engine] if engine not in ("ALL", "") else []
    ind = industry if industry not in ("ALL", "") else None

    result = plan_media_mix(
        int(row["capacity_total"]), industry=ind, target_engines=engines, pool=pool,
    )
    mix = result["mix"]
    assert mix["focus_media_anchor"] == int(row["expected_anchor"])
    assert mix["industry_platform_coverage"] == int(row["expected_coverage"])
    assert mix["douyin_doubao_only"] == int(row["expected_douyin"])
    # 三类之和恒等于交付额度（工单 §4.3 的 assert，这里再从外部锁一次）
    assert sum(mix.values()) == int(row["capacity_total"])
    assert result["ratio"]["raw"] == pytest.approx(float(row["ratio_raw"]), rel=1e-9)


@pytest.mark.parametrize("row", ROWS, ids=IDS)
def test_fallback_decision_matches_fixture(row):
    """回退判定：请求的格够不够用，必须与 fixture 记录的 source_scope 一致。"""
    cell = PoolCounts(cell_n=int(row["cell_n"]), portal_n=int(row["cell_anchor_n"]))
    used_requested_scope = row["source_scope"] == row["requested_scope"]
    if row["requested_scope"] == "global":
        pytest.skip("全局回退场景没有更窄的格可判")
    assert _eligible(cell) == used_requested_scope, (
        f"{row['scenario']}: cell_n={cell.cell_n} anchor={cell.anchor_n} "
        f"的取用判定与 fixture 的 source_scope 对不上"
    )


def test_douyin_is_subtracted_not_added():
    """v1 判红点①：抖音若不从覆盖侧扣掉，比例会被自己抬高，拆分随之变形。"""
    row = next(r for r in ROWS if r["scenario"] == "home_doubao")
    c = _counts(row)
    correct = c.coverage_n / c.anchor_n
    inflated = (c.vertical_n + c.self_media_n) / c.anchor_n   # v1 的错算法
    assert inflated > correct, "构造前提失效：该场景抖音数必须 > 0 才有判别力"
    good = plan_media_mix(21, industry=row["industry"], target_engines=[row["engine"]],
                          pool=MediaMixPool(rows={("industry_engine",row["industry"], row["engine"]): c}))
    bad = plan_media_mix(21, industry=row["industry"], target_engines=[row["engine"]],
                         pool=MediaMixPool(rows={("industry_engine",row["industry"], row["engine"]):
                                                 PoolCounts(**{**c.__dict__, "douyin_n": 0})}))
    assert good["mix"] != bad["mix"], "扣不扣抖音必须导致不同拆分，否则这条口径是死的"


def test_unclassified_excluded_from_ratio():
    """v1 判红点②：未分类长尾不进任何一侧。塞进覆盖会把比例抬高。"""
    row = next(r for r in ROWS if r["scenario"] == "global_fallback")
    c = _counts(row)
    assert c.unclassified_n > 0, "构造前提失效：该场景必须有未分类样本"
    correct = c.coverage_n / c.anchor_n
    v1_style = (c.coverage_n + c.unclassified_n) / c.anchor_n
    assert v1_style > correct
    # 生产实现必须给出 correct 那一支
    result = plan_media_mix(21, pool=MediaMixPool(rows={("global", "", ""): c}))
    assert result["ratio"]["raw"] == pytest.approx(correct, rel=1e-9)
    assert result["ratio"]["unclassified_n"] == c.unclassified_n, "长尾要披露计数，只是不进公式"


def test_no_three_to_five_band():
    """v1 判红点③：3–5 的运营区间已作废，clamp 只剩 [1.5, 4.0] 防极端值。"""
    assert (RATIO_CLAMP_MIN, RATIO_CLAMP_MAX) == (1.5, 4.0)
    # 反向对照：真的会夹。构造一个极端小分母格（anchor 恰好达标、覆盖极多）
    extreme = PoolCounts(cell_n=1000, portal_n=MIN_ANCHOR_N, vertical_n=980, unclassified_n=0)
    assert extreme.is_consistent
    r = plan_media_mix(21, industry="X", target_engines=["kimi"],
                       pool=MediaMixPool(rows={("industry_engine","X", "kimi"): extreme}))
    assert r["ratio"]["raw"] > RATIO_CLAMP_MAX
    assert r["ratio"]["used"] == RATIO_CLAMP_MAX, "clamp 上界没生效"
    # 下界同样要真的会夹（六场景一次都没触发过，必须构造）
    low = PoolCounts(cell_n=1000, portal_n=900, vertical_n=100, unclassified_n=0)
    r2 = plan_media_mix(21, industry="Y", target_engines=["kimi"],
                        pool=MediaMixPool(rows={("industry_engine","Y", "kimi"): low}))
    assert r2["ratio"]["raw"] < RATIO_CLAMP_MIN
    assert r2["ratio"]["used"] == RATIO_CLAMP_MIN, "clamp 下界没生效"


def test_douyin_never_for_non_doubao():
    """工单 §7.4：非豆包目标永远不产生抖音数量。"""
    row = next(r for r in ROWS if r["scenario"] == "home_doubao")
    c = _counts(row)
    for engine in ("deepseek", "kimi", "qwen"):
        r = plan_media_mix(21, industry=row["industry"], target_engines=[engine],
                           pool=MediaMixPool(rows={("industry_engine",row["industry"], engine): c}))
        assert r["mix"]["douyin_doubao_only"] == 0, f"{engine} 不该有抖音专项"
    # 反向对照：同一份计数换成豆包就必须有（否则上面是恒真）
    r = plan_media_mix(21, industry=row["industry"], target_engines=["doubao"],
                       pool=MediaMixPool(rows={("industry_engine",row["industry"], "doubao"): c}))
    assert r["mix"]["douyin_doubao_only"] > 0


def test_douyin_share_capped_and_gated():
    """抖音闸：份额 < 10% 不启用；≥ 10% 时按 min(share, 20%) 取。"""
    base = _counts(next(r for r in ROWS if r["scenario"] == "home_doubao"))
    low = PoolCounts(cell_n=1000, portal_n=100, vertical_n=200, self_media_n=700,
                     douyin_n=90, unclassified_n=0)          # 9% < 10%
    assert low.is_consistent
    r = plan_media_mix(21, industry="Z", target_engines=["doubao"],
                       pool=MediaMixPool(rows={("industry_engine","Z", "doubao"): low}))
    assert r["mix"]["douyin_doubao_only"] == 0, "9% 不该启用抖音"
    # base 是 35.69%,必须被压到 20% 上限
    assert base.douyin_n / base.cell_n > 0.20
    r2 = plan_media_mix(100, industry="家装家居", target_engines=["doubao"],
                        pool=MediaMixPool(rows={("industry_engine","家装家居", "doubao"): base}))
    assert r2["mix"]["douyin_doubao_only"] == 20, "20% 上限没生效"


def test_small_sample_cell_never_used():
    """工单 §7.2/§7.3：样本不足的格不得进入生产组合，且回退不阻断。"""
    tiny = PoolCounts(cell_n=MIN_CELL_N - 1, portal_n=MIN_ANCHOR_N + 5,
                      vertical_n=4, unclassified_n=0)
    big = PoolCounts(cell_n=4000, portal_n=1000, vertical_n=3000, unclassified_n=0)
    pool = MediaMixPool(rows={("industry_engine","W", "kimi"): tiny, ("OTHER", "kimi"): big})
    r = plan_media_mix(21, industry="W", target_engines=["kimi"], pool=pool)
    assert r["ratio"]["source"] != "industry_engine", "样本不足的格被用了"
    assert sum(r["mix"].values()) == 21, "回退后依然要给出完整拆分,不能阻断"


def test_capacity_is_never_changed():
    """工单 §1.1.1：本包只拆分，不改总额度。任意容量下三类之和恒等于它。"""
    c = _counts(ROWS[0])
    for total in (0, 1, 2, 3, 5, 7, 12, 21, 56, 100):
        r = plan_media_mix(total, pool=MediaMixPool(rows={("global", "", ""): c}))
        assert r["capacity_total"] == total
        assert sum(r["mix"].values()) == total


def test_round_half_up_differs_from_bankers():
    """工单 §4.3：两端必须共享 round_half_up。这条锁住 Python 侧不是 banker's rounding。"""
    assert round_half_up(2.5) == 3 and round(2.5) == 2, "内置 round 是 banker's,不能用"
    assert round_half_up(3.5) == 4
    assert round_half_up(4.5) == 5 and round(4.5) == 4


def test_shipped_pool_reproduces_every_fixture_scenario():
    """端到端:**仓里真正要发的那份池表**必须能复现全部六个场景。

    🔴 这条和 test_mix_matches_fixture 不重复:那条喂的是 fixture 自带的计数(测公式),
       这条走 `config/quote_media_mix_pool_v2.csv` + 真实回退链(测接线)。
       池表少一行、scope 写错、回退顺序改了 —— 公式那条照样绿,只有这条会红。
    """
    from services.quote_media_mix import load_pool

    pool = load_pool()
    assert pool.rows, "仓内池表读不到,下面全是空跑"
    assert pool.version, "池表必须自带 pool_version(数据时点是可追溯的一部分)"
    for row in ROWS:
        ind = row["industry"] if row["industry"] not in ("ALL", "") else None
        engines = [row["engine"]] if row["engine"] not in ("ALL", "") else []
        got = plan_media_mix(int(row["capacity_total"]), industry=ind,
                             target_engines=engines, pool=pool)
        assert got["ratio"]["source"] == row["source_scope"], (
            f"{row['scenario']}: 回退层级取到了 {got['ratio']['source']},"
            f"fixture 记录的是 {row['source_scope']}"
        )
        assert got["mix"]["focus_media_anchor"] == int(row["expected_anchor"]), row["scenario"]
        assert got["mix"]["industry_platform_coverage"] == int(row["expected_coverage"]), row["scenario"]
        assert got["mix"]["douyin_doubao_only"] == int(row["expected_douyin"]), row["scenario"]


def test_shipped_pool_is_provenance_stamped():
    """池表是 2026-08-07 快照,不是当天生产实时值 —— 版本号必须写在数据里,
    否则半年后没人说得清这组比例是什么时候的(裁定书 v2 §0 的已知限制)。"""
    from services.quote_media_mix import load_pool

    assert load_pool().version == "citation-media-tier-v1-2026-08-07"


def test_missing_pool_degrades_to_global_prior_not_clamp_floor():
    """工单 §1.1.5 + 2026-08-12 Review P2-3。

    🔴 这条原来只断言「不阻断 + source==global + 抖音 0」,**没断言数值** ——
       于是实现走了 clamp 下限 1.5、把 21 条拆成 8/13/0(应为 7/14/0),
       测试照样绿。弱断言放跑错值,是 Review 判红的直接原因。
       现在锁死降级后的**具体数字**,并配反向对照证明它不是恰好相等。
    """
    from services.quote_media_mix import GLOBAL_RATIO_RAW, RATIO_CLAMP_MIN

    r = plan_media_mix(21, industry="任意行业", target_engines=["doubao"], pool=MediaMixPool())
    assert sum(r["mix"].values()) == 21, "降级后依然要给出完整拆分,不能阻断"
    assert r["ratio"]["source"] == "global"
    assert r["mix"]["douyin_doubao_only"] == 0, "没有样本时不得启用抖音专项"
    assert r["ratio"]["degraded"] is True, "降级要能被调用方识别(对外文案据此改口径)"

    # 降级必须落在**有出处的全局先验**上
    assert r["ratio"]["raw"] == pytest.approx(GLOBAL_RATIO_RAW, rel=1e-9)
    expected_anchor = max(2, round_half_up(21 / (1 + GLOBAL_RATIO_RAW)))
    assert r["mix"]["focus_media_anchor"] == expected_anchor
    assert r["mix"]["industry_platform_coverage"] == 21 - expected_anchor

    # 反向对照:走 clamp 下限会得到**不同**的数 → 上面不是"两条路碰巧一样"
    floor_anchor = max(2, round_half_up(21 / (1 + RATIO_CLAMP_MIN)))
    assert floor_anchor != expected_anchor, "构造前提失效:两种降级方式必须给出不同结果"
    assert r["mix"]["focus_media_anchor"] != floor_anchor, "又退回 clamp 下限了"


def test_degraded_flag_is_false_when_pool_present():
    """反向对照:池表正常时 degraded 必须为 false,否则上面那条恒真。"""
    from services.quote_media_mix import load_pool

    r = plan_media_mix(21, pool=load_pool())
    assert r["ratio"]["degraded"] is False
