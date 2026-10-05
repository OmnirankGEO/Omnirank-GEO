"""DIA-FIN-12:现役 ``run_status`` 逐值 exhaustive 投影(规格 §3.5 / §18.3)。

判据要防的三件事,逐件都配了「必须命中」+「必须不命中」:

  ① 漏映射一个现役值 → 必红。
     🔴 分母**不是**本文件里的清单,是 census 从迁移 SQL 的 CHECK 约束机械抽的全集。
        手抄分母漏掉的那一项不会让任何判据变红(本仓 2026-08-19 实证)。
  ② 未知值被错映成 completed / released → 必红。
     这两个方向各自对应一次真实损失:白拿结果 / 白退钱。
  ③ 把任一现役值错映成成功或退款方向 → 必红(逐值钉死,不是只钉几个抽样)。

反向对照贯穿全文:每条「必须命中」都配一条「必须不命中」,
证明判据本身有判别力,而不是恒绿。
"""

from __future__ import annotations

import pytest

from scripts.defgeo_census.run_status_census import build as build_census
from services.defensive_geo.run_status_projection import (
    PROJECTION_VERSION,
    known_statuses,
    project,
)

# ── 分母:机械导出,不手抄 ────────────────────────────────────────────────
_CENSUS = build_census()
#: CHECK 约束允许的现役 run_status 全集。这是本文件一切「逐值」判据的分母。
ALLOWED = tuple(_CENSUS["allowed_values"])


def test_census_denominator_is_non_empty_and_plausible():
    """先证明分母本身立得住 —— 零分母判据是恒绿的,只能记「没验」。

    这条是**判据可用性关**:它先于一切逐值检查。census 若因约束改名/移文件
    而抽成空集,下面所有 `for value in ALLOWED` 的循环都会零圈通过,
    那比漏检更坏(本仓 2026-08-19「零分母判据只能记『没验』」)。
    """
    assert len(ALLOWED) >= 10, f"census 分母只有 {len(ALLOWED)} 个,疑似没抽到:{ALLOWED}"
    # 反向对照:分母里必须真的含有已知一定存在的值。
    # 若 census 退化成「抽了个空壳但长度够」,这条会红。
    for anchor in ("running", "committed", "settlement_manual"):
        assert anchor in ALLOWED, f"census 分母缺锚点 {anchor} —— 抽取面不对"
    # 必须不命中:census 不该抽出一个根本不是状态的词。
    assert "run_status" not in ALLOWED
    assert "check" not in ALLOWED


def test_every_live_status_is_mapped():
    """① 逐值覆盖。分母来自 census;删掉 PROJECTION 里任意一行必红。"""
    missing = sorted(set(ALLOWED) - known_statuses())
    assert not missing, (
        f"现役 run_status 有 {len(missing)} 个值没有投影:{missing}。"
        "未映射的值会走 quarantine —— 那是给「真·未知」留的口子,"
        "不是给「忘了写」留的。"
    )


def test_projection_table_has_no_values_outside_the_live_denominator():
    """反向:投影表不许凭空多出现役 CHECK 里没有的值。

    多出来的行是死行 —— 它永远不会被真实数据驱动,却会让
    `test_every_live_status_is_mapped` 看起来更「完整」。
    """
    extra = sorted(known_statuses() - set(ALLOWED))
    assert not extra, f"投影表多出 {extra},现役 CHECK 存不下这些值(死行)"


@pytest.mark.parametrize("raw", ALLOWED)
def test_known_status_is_never_quarantined(raw: str):
    """已知值不许落进 quarantine —— 否则 quarantine 就成了兜底,①就恒绿了。"""
    proj = project(raw)
    assert proj.known is True, f"{raw} 被判成未知"
    assert proj.run_state != "quarantined", f"{raw} 落进 runState=quarantined"
    assert proj.funding_state != "quarantined" or raw in ("settlement_manual", "manual_resolving"), (
        f"{raw} 的 fundingState 被判 quarantined,但只有转人工两态允许"
    )
    assert proj.raw_status == raw, "原始血缘必须原样随行"


# ── ② 未知值:双轴 quarantined ────────────────────────────────────────────
_UNKNOWN_SAMPLES = [
    "some_future_status",      # 以后新加的值
    "COMMITTED",                # 大小写不同 = 不同值,不许模糊匹配吞掉
    "committed ",               # 尾随空格由 strip 归一 → 这条应当**已知**,见下方专测
    "",                         # 空串
    None,                       # 非字符串
    123,                        # 非字符串
]


@pytest.mark.parametrize("raw", ["some_future_status", "COMMITTED", "", None, 123])
def test_unknown_status_is_double_quarantined(raw):
    """② 未知值双轴 quarantined,且绝不映成 completed / released。"""
    proj = project(raw)
    assert proj.known is False, f"{raw!r} 不该被当成已知值"
    assert proj.run_state == "quarantined", f"{raw!r} 的 runState={proj.run_state}"
    assert proj.funding_state == "quarantined", f"{raw!r} 的 fundingState={proj.funding_state}"
    # 必须不命中 —— 这两条正是规格点名禁止的两个方向。
    assert proj.run_state != "completed", "未知值绝不能报完成(客户白拿结果)"
    assert proj.funding_state != "released", "未知值绝不能报已退(平台白退钱)"


def test_whitespace_is_normalised_not_treated_as_unknown():
    """反向对照:strip 归一是有意行为,不是漏网。

    没有这条,上面的 `"committed "` 若被判未知也没人发现 ——
    而那会让真实数据(库里带尾随空格的行)整片落进 quarantine。
    """
    proj = project("committed ")
    assert proj.known is True
    assert proj.run_state == "completed"
    assert proj.raw_status == "committed"


# ── ③ 逐值钉死钱向与终态,不抽样 ──────────────────────────────────────────
#: 这张表是**判据自己的**期望,与被测模块各写一次是有意的:
#: 两处同时改错的概率远低于一处。改被测表而不改这里 → 必红。
_EXPECTED = {
    "pending_freeze":           ("queued",             "pending_reconciliation"),
    "running":                  ("running",            "frozen"),
    "commit_pending":           ("settlement_pending", "frozen"),
    "release_pending":          ("settlement_pending", "frozen"),
    "committed":                ("completed",          "committed"),
    "released":                 ("cancelled",          "released"),
    "cancelled":                ("cancelled",          "released"),
    "cancelled_no_freeze":      ("cancelled",          "exempt_recorded"),
    "completed_exempt":         ("completed",          "exempt_recorded"),
    "failed_exempt":            ("failed",             "exempt_recorded"),
    "delivery_repair_pending":  ("needs_action",       "committed"),
    "settlement_manual":        ("needs_action",       "quarantined"),
    "manual_resolving":         ("needs_action",       "quarantined"),
}


def test_expected_table_covers_the_census_denominator():
    """判据自己的期望表也必须盖满分母 —— 否则 ③ 会静默少验几值。"""
    assert sorted(_EXPECTED) == sorted(ALLOWED), (
        "判据期望表与 census 分母不一致:"
        f"缺 {sorted(set(ALLOWED) - set(_EXPECTED))} / 多 {sorted(set(_EXPECTED) - set(ALLOWED))}"
    )


@pytest.mark.parametrize("raw,expected", sorted(_EXPECTED.items()))
def test_each_status_projects_to_exact_pair(raw: str, expected: tuple[str, str]):
    """③ 逐值 exact 相等。任一行改一个字都必红。"""
    proj = project(raw)
    assert (proj.run_state, proj.funding_state) == expected, (
        f"{raw} 投影成 {(proj.run_state, proj.funding_state)},期望 {expected}"
    )


def test_only_terminal_success_paths_may_report_completed():
    """必须不命中:completed 不许出现在任何非成功值上。

    单独立一条是因为 ③ 是「逐值相等」,而这条是「跨值的不变式」——
    以后有人**新加**一个值并顺手给它 completed,③ 不会红(它只管已有行),
    这条会红。
    """
    completed = {v for v in ALLOWED if project(v).run_state == "completed"}
    assert completed == {"committed", "completed_exempt"}, (
        f"报 completed 的值集合 = {sorted(completed)};"
        "只有真成功的两态允许 —— 其余任何值报 completed 都等于让客户白拿结果"
    )


def test_only_real_refund_paths_may_report_released():
    """必须不命中:released 不许出现在「钱没退」的值上。"""
    released = {v for v in ALLOWED if project(v).funding_state == "released"}
    assert released == {"released", "cancelled"}, (
        f"报 released 的值集合 = {sorted(released)};"
        "cancelled_no_freeze 是「压根没冻」不是「退了」,不能混进来"
    )


def test_projection_version_is_pinned():
    """映射是版本化只读投影;改任一行必须升版,否则旧客户端拿新语义当旧语义读。"""
    assert PROJECTION_VERSION == "diagnosis-run-status-projection-v1"
