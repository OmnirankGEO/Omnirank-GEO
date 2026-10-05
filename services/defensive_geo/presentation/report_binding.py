"""报告 → v2 运行绑定的**只读**解析(CUR-01 路由判据)。

一份诊断报告要走新的五卡呈现,前提是它**确实是**一次 v2 defensive/hybrid 运行。
这条链在库里是现成的,不需要新表:

    diagnosis_records.run_token
      → defgeo_diagnosis_run_previews.consumed_command_id
        → campaign_mode

🔴 为什么不看 `report_v2_*` 那几列
----------------------------------
``diagnosis_records.report_v2_version`` 等列属于**旧的 v2 报告**
(`ReportV2View` 那套 8 模块 Markdown),与本规格的
``DefensiveGeoPresentationV2`` **同名异物**。拿它判路由会把所有旧报告
一起送进新组件 —— CUR-01 要修的恰恰是"同一个诊断两套报告真相",
再叠一层同名异义只会更糟。

🔴 只读
-------
本模块只有 SELECT。绑定关系由 confirm 那一刻写死,呈现层不该有能力改它。
"""

from __future__ import annotations

from typing import Literal, NamedTuple, Optional

#: 走新五卡呈现的模式。offensive 单侧**也**走新呈现(它同样是 v2 运行),
#: 但五卡里进攻侧那部分才有内容 —— 这由投影层按 side 决定,不在这里判。
V2_MODES: frozenset[str] = frozenset({"defensive", "offensive", "hybrid"})


class ReportBinding(NamedTuple):
    is_v2: bool
    campaign_mode: Optional[str]
    question_plan_id: Optional[str]
    question_plan_revision: Optional[int]
    #: 人话模式名。``None`` 表示不是 v2 —— 前端据此走 legacy 分支。
    mode_user_label: Optional[str]


LEGACY = ReportBinding(False, None, None, None, None)


def _pick(row, name: str, index: int):
    """按列名取值,兼容 tuple cursor。

    🔴 生产连接池把 ``cursor_factory`` 固定成 ``RealDictCursor``
       (``db/connection.py:57``),行是 **dict** 不是 tuple ——
       ``row[0]`` 会抛 ``KeyError: 0``。
       第一版就是按下标写的:纯函数判据全绿,真 HTTP 一打就 500。
       (判据夹具当时用的是裸 ``psycopg2.connect``,cursor 类型与生产不同 ——
       夹具与生产不同构 ⇒ 判据恒绿,本仓记过这个形态。)
       两种都兼容,是因为判据夹具与生产可能各用一种,而这层不该关心。
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    return row[index]


def resolve_binding(cur, diagnosis_id: int) -> ReportBinding:
    """解析一份报告是不是 v2 运行。**拿不到就返回 LEGACY**,不猜。

    注意 ``run_token`` 可能为 NULL(现役绝大多数历史报告都是),
    那种情况下连查都不用查 —— 直接 legacy。
    """
    cur.execute(
        "SELECT run_token FROM diagnosis_records WHERE id = %s AND COALESCE(is_deleted, FALSE) = FALSE",
        (int(diagnosis_id),),
    )
    row = cur.fetchone()
    run_token = _pick(row, "run_token", 0)
    if not run_token:
        return LEGACY
    cur.execute(
        """
        SELECT campaign_mode, question_plan_id, question_plan_revision
          FROM defgeo_diagnosis_run_previews
         WHERE consumed_command_id = %s
         LIMIT 1
        """,
        (str(run_token),),
    )
    preview = cur.fetchone()
    if not preview:
        # 有 run_token 但没有 v2 preview ⇒ 现役 legacy 诊断链跑的。
        return LEGACY

    mode = _pick(preview, "campaign_mode", 0)
    if mode not in V2_MODES:
        return LEGACY

    from services.defensive_geo.presentation.copy_registry import MODE_LABELS

    return ReportBinding(
        is_v2=True,
        campaign_mode=mode,
        question_plan_id=_pick(preview, "question_plan_id", 1),
        question_plan_revision=(
            int(_pick(preview, "question_plan_revision", 2))
            if _pick(preview, "question_plan_revision", 2) is not None else None),
        mode_user_label=MODE_LABELS[mode],
    )
