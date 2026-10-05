"""§13.3 诊断快照与实时监测**分开** + §6.1 报告快照冻结什么。

规格 §13.3 的病灶写得很具体:「当前内部报告嵌入了『当前每日监测』卡,
但**没有清楚标时间**」。于是同一页上"本次诊断得分"和"最新监测"并排,
读的人默认它们同一时刻 —— 而后者每天在动。

v2 的四条:

* 「本次诊断」读**冻结** report snapshot;
* 「最新持续监测」读实时/最新聚合,**必须显示 as_of/updated_at**;
* 实时卡**不参与**本次诊断分数、历史比较或客户 PDF;
* Portal 只展示趋势 + 打开冻结快照的入口,不复制另一份诊断算法。

本模块把这四条写成类型:两种读面是**两个不同的类**,
:class:`FrozenDiagnosisSnapshot` 与 :class:`LiveMonitoringView`,
且 :func:`assert_live_not_in_frozen_surface` 拦住把后者混进前者的调用。

🔴 POR-15 / POR-04:快照**不可原地变态**
----------------------------------------
「同 reportSnapshotId/hash 永不从 processing/failed 原地变 ready」、
「导出后改实时数据,旧 PDF 不漂移」。所以快照行是**只插不改**:
状态推进 = 新 revision,不是 UPDATE。库里由触发器兜底(迁移 046)。

🔴 §13.2 尾句:「**迟到结果不得回写已签发 PDF**;可生成新 snapshot revision」
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, NamedTuple, Sequence

from services.defensive_geo.presentation.commitments import _canonical, _frame, _jcs_dumps

SNAPSHOT_VERSION = "defgeo-report-snapshot-v1"

TABLE = "defgeo_report_snapshots"

#: §6.1 末段逐字:报告快照**同时冻结**这些。少冻一项,日后就无法从前五边界
#: 重建第六边界(§6.2「第六步可从前五步重建」/ MON-06)。
FROZEN_FIELDS: tuple[str, ...] = (
    "plan_snapshot_id",
    "plan_snapshot_hash",
    "sampling_window_start",
    "sampling_window_end",
    "cutoff_at",
    "input_watermark",
    "raw_result_ids",
    "entity_resolver_version",
    "outcome_classifier_version",
    "evidence_extractor_version",
    "metric_definition_version",
)

#: 快照生命周期。**terminal 前不签客户 token/PDF**(POR-15)。
SNAPSHOT_STATES: tuple[str, ...] = (
    "processing", "ready", "partial", "no_conclusion", "failed",
)

TERMINAL_STATES: frozenset[str] = frozenset({"ready", "partial", "no_conclusion"})

#: POR-15 逐字:客户 token/PDF **只绑定** ready/partial/no_conclusion。
CUSTOMER_BINDABLE_STATES: frozenset[str] = TERMINAL_STATES


class SnapshotError(ValueError):
    """快照形态不合法。**不签发**,而不是签一个会漂的快照。"""


class FrozenDiagnosisSnapshot(NamedTuple):
    """「本次诊断」—— 冻结面。没有 as_of 概念,它就是那一刻。"""

    report_snapshot_id: str
    revision: int
    state: str
    content_hash: str
    frozen: Mapping[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class LiveMonitoringView(NamedTuple):
    """「最新持续监测」—— 实时面。**必须**带 as_of,且不参与任何冻结结论。"""

    as_of: str
    updated_at: str
    aggregate: Mapping[str, Any]
    #: Z-2.1:监测整体不可用时保留上次成功快照 + as_of 标注,**不白屏不显示 0**。
    is_stale: bool = False
    stale_reason_code: str | None = None


def content_hash(frozen: Mapping[str, Any]) -> str:
    """冻结内容的 hash。改任一冻结叶子必改 hash(POR-05/UI-31 的承重点)。"""
    missing = [f for f in FROZEN_FIELDS if f not in frozen]
    if missing:
        raise SnapshotError(
            f"报告快照缺冻结项 {missing} —— §6.1 要求这些**同时**冻结,"
            "少一项就无法从前五边界重建第六边界(MON-06)"
        )
    extra = [k for k in frozen if k not in FROZEN_FIELDS]
    if extra:
        raise SnapshotError(f"报告快照出现未登记冻结项 {extra}")
    parts: list[bytes] = [b"defgeo/report-snapshot/v1",
                          SNAPSHOT_VERSION.encode("utf-8")]
    for f in FROZEN_FIELDS:
        parts.append(f.encode("utf-8"))
        parts.append(_jcs_dumps(_canonical(frozen[f])))
    return hashlib.sha256(_frame(*parts)).hexdigest()


def build(
    *, report_snapshot_id: str, revision: int, state: str,
    frozen: Mapping[str, Any],
) -> FrozenDiagnosisSnapshot:
    if state not in SNAPSHOT_STATES:
        raise SnapshotError(f"未知快照状态 {state!r};合法 = {list(SNAPSHOT_STATES)}")
    if revision < 1:
        raise SnapshotError("快照 revision 从 1 起")
    return FrozenDiagnosisSnapshot(
        report_snapshot_id=report_snapshot_id, revision=int(revision), state=state,
        content_hash=content_hash(frozen), frozen=dict(frozen),
    )


def assert_customer_bindable(snap: FrozenDiagnosisSnapshot) -> None:
    """POR-15:非终态快照不许签客户 token / 出 PDF。

    processing 的快照签了 token,客户点开会看到半成品结论,
    而那份结论在几分钟后还会变 —— 已经发出去的链接却指着同一个 id。
    """
    if snap.state not in CUSTOMER_BINDABLE_STATES:
        raise SnapshotError(
            f"快照状态 {snap.state} 不可绑定客户 token/PDF。"
            f"只有 {sorted(CUSTOMER_BINDABLE_STATES)} 可以(POR-15)。"
        )


def assert_no_in_place_state_change(
    *, existing_state: str, existing_hash: str,
    new_state: str, new_hash: str,
) -> None:
    """POR-15 逐字:「同 reportSnapshotId/hash 永不从 processing/failed 原地变 ready」。

    合法的推进方式是**新 revision**。这个函数拦的正是"同 id 同 hash 换个状态"。
    """
    if existing_hash == new_hash and existing_state != new_state:
        raise SnapshotError(
            f"同一 content hash 上把状态从 {existing_state} 原地改成 {new_state} —— "
            "状态推进必须走**新 revision**,原地改会让已发出的链接指向"
            "一份内容没变但结论变了的报告(POR-15 / POR-04)。"
        )


def assert_live_not_in_frozen_surface(payload: Mapping[str, Any]) -> None:
    """§13.3 第三条:实时卡**不参与**本次诊断分数、历史比较或客户 PDF。

    可执行形式:冻结面的 payload 里出现实时面的标记字段就抛。
    这比"约定不要放进去"硬 —— 后者在下一次加卡片时必然被忘掉。
    """
    live_markers = ("liveMonitoring", "latestMonitoring", "realtimeAggregate",
                    "live_monitoring", "latest_monitoring")
    hits = [k for k in _walk_keys(payload) if k in live_markers]
    if hits:
        raise SnapshotError(
            f"冻结诊断面里出现实时监测字段 {sorted(set(hits))} —— "
            "实时数据每天在动,混进冻结快照会让同一个 reportSnapshotId "
            "在不同时刻给出不同结论(§13.3)。"
        )


def _walk_keys(node: Any):
    if isinstance(node, Mapping):
        for k, v in node.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk_keys(v)


def live_view(
    *, as_of: str, updated_at: str, aggregate: Mapping[str, Any],
    is_stale: bool = False, stale_reason_code: str | None = None,
) -> LiveMonitoringView:
    """§13.3 第二条 + Z-2.1。

    🔴 ``as_of`` 是**必填**。这一条就是 §13.3 点名的那个病灶的解药:
       没有时间戳的实时卡与冻结结论并排,读的人默认它们同一时刻。
    """
    if not as_of or not updated_at:
        raise SnapshotError(
            "实时监测卡必须带 as_of 与 updated_at —— §13.3 点名的病灶就是"
            "「嵌了当前每日监测卡但没有清楚标时间」"
        )
    if is_stale and not stale_reason_code:
        raise SnapshotError(
            "stale 的实时卡必须给 reason —— Z-2.1 要求「保留上次成功轮询快照 + "
            "as_of 标注 + 刷新入口,不白屏、不显示 0」,没有原因就给不出那句话"
        )
    return LiveMonitoringView(
        as_of=as_of, updated_at=updated_at, aggregate=dict(aggregate),
        is_stale=bool(is_stale), stale_reason_code=stale_reason_code,
    )


def assert_late_result_not_backwritten(
    *, snapshot_revision: int, late_result_revision: int
) -> None:
    """§13.2 尾句:「迟到结果不得回写已签发 PDF;**可生成新 snapshot revision**」。"""
    if late_result_revision <= snapshot_revision:
        raise SnapshotError(
            f"迟到结果想写进 revision {late_result_revision},而已签发快照已在 "
            f"revision {snapshot_revision} —— 迟到结果只能生成**新** revision,"
            "回写会让已经发给客户的 PDF 与线上报告对不上(§13.2 / POR-04)。"
        )


def census() -> dict[str, Any]:
    return {
        "snapshotVersion": SNAPSHOT_VERSION,
        "table": TABLE,
        "frozenFields": list(FROZEN_FIELDS),
        "snapshotStates": list(SNAPSHOT_STATES),
        "terminalStates": sorted(TERMINAL_STATES),
        "customerBindableStates": sorted(CUSTOMER_BINDABLE_STATES),
    }
