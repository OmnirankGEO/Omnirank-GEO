"""MET 呈现接线 —— 把 WP7 的**真实观测**喂进窗B 的五卡投影。

🔴 这一层解决的是窗B 那句如实留白
--------------------------------
``api/defensive_geo_report_api.py`` 里 ``cards=[]`` 带着一句注释:
「五卡的**数据**来自 WP1 指标链;本包只交付路由与骨架」。
本模块就是那条指标链的**producer 侧**。

🔴 G-3:全链驱动,禁止夹具直造 DTO
--------------------------------
逐字:「投影判据必须从 raw answer→aggregate **全链驱动**,
禁止夹具直造 DTO 层;每组 MET 抽样配一发 producer 层变异」。

所以本模块的入口 :func:`produce_cards` 只收 **plan cells + attempt 账本行**,
自己算出 evidence cell 状态,再交给窗B 的 ``build_sample_summary``。
**不接受**调用方直接传 summary —— 那正是 G-3 禁的形态。

🔴 等级(level)必须来自签发 policy,未签就只能 unknown
-----------------------------------------------------
MET-36 逐字:「未签 policy 不得 enrollment 对客 v2 terminal Presentation」。
:func:`derive_level` 在 policy 未签时**恒返 unknown**,而不是"先按阈值算一个"。
这不是保守,是因为对客等级一旦发出去就成了承诺。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from services.defensive_geo.monitoring import attempt_ledger as _ledger
from services.defensive_geo.presentation import projection as _proj
from services.defensive_geo.presentation import registries as _reg

CARD_PRODUCER_VERSION = "defgeo-card-producer-v1"

#: 五卡 → 它在监测侧需要哪一类 cell 才算"有数"。
#: 分母来自窗B 的 registry(``_reg.cards()``),**不手抄** ——
#: 手抄的那份会在窗B 改卡时静默漂移。
def card_keys() -> tuple[str, ...]:
    return tuple(spec.key for spec in _reg.cards())


class ProducerError(ValueError):
    """观测算不成卡。**不投影**,而不是投一张有数字但没依据的卡。"""


class EvidenceCell(NamedTuple):
    """一格的终态投影。``status`` 逐值等于窗B 的 ``CELL_STATUSES``。"""

    plan_cell_id: str
    status: str


def _carries_a_result(r: Any) -> bool:
    """这条 attempt 背后有没有一条 raw result 行。

    🔴 [工单 V5-C · C-1 · Codex fix-of-fix2 P1-1] 冻结集约束的判据是**这一位**,
       不是终态的名字。``run_ledger_bridge.close_for_result`` 的 state_map 是
       **两条**(``succeeded→answered`` 与 ``pending_identity→entity_ambiguous``),
       两条都写 ``monitoring_result_id``。上一版按终态名筛,于是整个
       ``entity_ambiguous`` 类越过了冻结边界:一条不在冻结结果集里的
       "身份待定"观测照样进已发报告,而且与"在里面"长得一模一样。

       按名单筛 = 把**推论**当成规则。规则只有一句:这份快照没认下的
       raw result,对这份快照而言不存在。
    """
    return r.monitoring_result_id is not None


def _rows_by_cell(
    cur, *, plan_cell_ids: Sequence[str], cutoff_at=None, raw_result_ids=None
) -> dict[str, list[Any]]:
    """每格的 attempt 行 —— evidence / attemptRecords / 出现率的**唯一**取数口。

    🔴 三个数出自同一份冻结面,所以只能有一个地方去裁。上一版是两处各裁一遍
       (这里按终态名、出现率在 SQL 里另写一份 WHERE),于是同一条边界有两份
       实现、两处口径 —— C-1 与 C-2 是同一个形态的两次发作。
    """
    frozen = None if raw_result_ids is None else {int(i) for i in raw_result_ids}
    out: dict[str, list[Any]] = {}
    for pcid in sorted(set(plan_cell_ids)):
        rows = _ledger.attempts_for_cell(cur, plan_cell_id=pcid,
                                         cutoff_at=cutoff_at)
        if frozen is not None:
            rows = [r for r in rows
                    if not _carries_a_result(r)
                    or int(r.monitoring_result_id) in frozen]
        out[pcid] = rows
    return out


def evidence_cells_from_ledger(
    cur, *, plan_cell_ids: Sequence[str], cutoff_at=None, raw_result_ids=None
) -> tuple[list[EvidenceCell], list[dict[str, Any]]]:
    """从 attempt 账本重建 evidence cell 与 attempt 行。

    🔴 ``status`` 取的是 **canonical attempt** 的终态(§6.1 selection policy),
       不是"最后一次尝试"。MON-10 逐字:首发 engine-error、fallback 成功时
       final cell 可 answered —— 取最后一次会得到同样答案,
       但取 canonical 在"最后一次又失败了"时才是对的。

    🔴 返回的 attempt 行是**全量**的(含已被 canonical 挤掉的那些),
       因为 ``attemptRecords`` / ``attemptErrors`` 要的是真实调用数,
       不是入选数。

    🔴 [工单 V3-C · C-4] "全量"的边界是 ``cutoff_at``,不是"到此刻为止"。
       冻结报告必须传 cutoff;不传 = 实时面。

    🔴 [工单 V4-C · C-2 · Codex fix-of-fix P1-3] ``raw_result_ids`` 同样要在
       这里生效,不能只喂 :func:`occurrence_rate`。
       上一版只有出现率绑了冻结结果集,而 ``status`` / ``attemptRecords`` /
       ``attemptErrors`` —— 也就是卡面上那几个数 —— 仍然由**全部** attempt 决定。
       于是"这份报告认哪些 raw result"这件事只管住了一个百分比,
       管不住它旁边的计数。两者出自同一份冻结面,必须同一条边界。

       🔴 [工单 V5-C · C-1 · Codex fix-of-fix2 P1-1] 约束的判据是
       :func:`_carries_a_result` —— **凡带 result 行的 attempt 都受约束**,
       不是"只筛 answered"。不在冻结集里的,等同于这份快照当时没有认它
       ⇒ 该格退回"还没有终态"。
       没有 result 行的终态(engine_error / policy_skipped)不受约束,
       那是上面那条规则的**推论**:拿结果集去筛它们会把"平台报错"整类
       从卡上抹掉,正是 §6.3 禁止的压平。
       上一版把这个推论写成了规则(按终态名筛),而 ledger 后来给
       ``entity_ambiguous`` 也写了 result id —— 于是它整类漏了出去。
    """
    return _evidence_from_rows(
        _rows_by_cell(cur, plan_cell_ids=plan_cell_ids,
                      cutoff_at=cutoff_at, raw_result_ids=raw_result_ids))


def _evidence_from_rows(
    by_cell: dict[str, list[Any]]
) -> tuple[list[EvidenceCell], list[dict[str, Any]]]:
    """已裁好的行 → evidence cell + attempt 行。零 DB 访问。"""
    cells: list[EvidenceCell] = []
    attempts: list[dict[str, Any]] = []
    for pcid, rows in by_cell.items():
        for r in rows:
            attempts.append({"planCellId": pcid, "error": r.error_code})
        canonical = _ledger.canonical_attempt(rows)
        if canonical is None:
            # 还没有终态 attempt ⇒ 这一格还没进 evidence(仍在跑)。
            continue
        cells.append(EvidenceCell(plan_cell_id=pcid,
                                  status=canonical.terminal_state))
    return cells, attempts


def build_summary(
    *, plan_cell_ids: Sequence[str],
    evidence: Sequence[EvidenceCell],
    attempts: Sequence[Mapping[str, Any]],
) -> _proj.SampleSummary:
    """调窗B 的重建函数。**本模块不自己算 summary** —— 那会变成第二份公式。"""
    return _proj.build_sample_summary(
        plan_cells=[{"planCellId": p} for p in sorted(set(plan_cell_ids))],
        evidence_cells=[{"status": c.status} for c in evidence],
        attempt_ledger=list(attempts),
    )


def occurrence_rate(cur, *, plan_cell_ids: Sequence[str],
                    cutoff_at=None, raw_result_ids=None) -> float | None:
    """出现率(%)—— Z-6 等级口径的**唯一**输入。

    定义:在**拿到了有效回答**的那些格里,品牌真的出现了的比例。
    分母 = canonical 终态为 ``answered`` 的格(``SampleSummary.valid``);
    分子 = 这些格对应的 ``monitoring_results.is_detected`` 为真的个数。

    🔴 [工单 V5-C · C-2 · Codex fix-of-fix2 P1-2] **一格一票**,票由
       canonical attempt 投。上一版直接
       ``COUNT(*) ... WHERE terminal_state='answered'`` 数 **attempt 行**,
       于是同一格重试两次就投两票 —— 注释、summary 与实际计数是两套口径。
       Codex 真 PG16 反例:1 格 2 条 answered、canonical 命中,正确 100.0%,
       上一版给 50.0%,而 summary 同时显示 ``valid=1``。
       出现率是 Z-6 等级口径的**唯一**输入,数错 = 客户拿到错的等级。

    🔴 择优规则**单点**:只能来自 :func:`attempt_ledger.canonical_attempt`。
       在 SQL 里另写一份 ``ORDER BY attempt_ordinal`` 就是同一谓词写两处 ——
       两份实现同义时没有任何判据会红,漂了才红,而那时已经发出去了。
       所以本函数在 Python 侧选格,SQL 只回答"这些 result 命中了没"。

    🔴 分母**不是** planned:平台报错、身份待定、未购平台都不该拉低出现率
       —— 那是"我们没测到",不是"AI 没提到你"。把它们算进分母会让一次
       平台故障在客户报告上变成"你的出现率掉了"。§6.3 / MON-02 同一条理。

    🔴 拿不到数据时返回 ``None``,**不是** 0.0。
       ``None`` 与 0 是不同事实(§15.6 逐字):前者"这一项没测到",
       后者"测了,一次都没出现"。压成 0 会把留白讲成坏消息。

    🔴 数据源是**真实观测**:走 attempt 账本的 ``monitoring_result_id``
       回连现役 ``monitoring_results``。不另存一份、不重算现役 outcome
       (§13.1「保留现役 raw result 与 target_outcome」)。
    """
    ids = sorted(set(str(p) for p in plan_cell_ids if p))
    if not ids:
        return None
    # 🔴 [工单 V3-C · C-4] 冻结面把观测绑到快照冻结的 ``raw_result_ids``:
    #    出现率的分子分母都从 ``monitoring_results`` 取,而那张表在冻结之后
    #    还会长新行。不绑的话,一份已发出的报告上的出现率会自己变。
    #    ``raw_result_ids == []`` 与 ``None`` 是两件事:前者是"这份快照冻结时
    #    一条 raw result 都没有"(⇒ 如实返 None),后者是实时面(不绑)。
    #    裁剪由 ``_rows_by_cell`` 单点完成 —— 与 evidence 用的是同一把刀。

    # 🔴 MIG-04 纪律:``monitoring_results`` 缺失(fixture 库 / 迁移未上)
    #    ⇒ **如实留白**,不是让整条五卡链炸掉。
    #    这一层是判据抓出来的:没有它,``produce_cards`` 在没有现役表的库上
    #    直接 UndefinedTable,而 report 端点那层 try/except 会把它吞成
    #    ``cards=[]`` —— 于是"表没有"与"没有观测数据"长得一模一样,
    #    真出问题时排查不到。这里走 SAVEPOINT 并留 warning,把两者分开。
    from services.defensive_geo.monitoring.run_ledger_bridge import guarded

    rows_by_cell = guarded(
        cur, "occurrence_rate/账本取数",
        lambda: _rows_by_cell(cur, plan_cell_ids=ids, cutoff_at=cutoff_at,
                              raw_result_ids=raw_result_ids))
    if rows_by_cell is None:
        return None
    return _rate_from_rows(cur, rows_by_cell)


def _rate_from_rows(cur, by_cell: dict[str, list[Any]]) -> float | None:
    """已裁好的行 → 出现率。冻结/cutoff 由 :func:`_rows_by_cell` 裁过。

    🔴 拆出来是为了让 :func:`produce_cards` 把账本**只读一遍**:
       evidence 与出现率吃的是同一份行。上一版两条链各查一次账本,
       改成共用一把刀之后如果还各查一次,一份报告的账本往返就翻倍了。
       (判据 ``test_produce_cards_reads_the_ledger_once_per_cell`` 钉住这一位。)
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import guarded

    def _tally() -> tuple[int, int]:
        # 🔴 一格一票:票由 canonical attempt 投,规则单点来自 ledger。
        #    canonical 不是 answered(平台报错 / 身份待定 / 跳过)⇒ 这一格
        #    不进分母,那是"我们没测到"不是"AI 没提到你"(§6.3 / MON-02)。
        by_cell_result: dict[str, int] = {}
        for pcid, rows in by_cell.items():
            canonical = _ledger.canonical_attempt(rows)
            if canonical is None or canonical.terminal_state != "answered":
                continue
            if canonical.monitoring_result_id is None:
                continue
            by_cell_result[pcid] = int(canonical.monitoring_result_id)
        if not by_cell_result:
            return (0, 0)
        cur.execute(
            "SELECT id, is_detected FROM public.monitoring_results "
            "WHERE id = ANY(%s)",
            (sorted(set(by_cell_result.values())),),
        )
        detected_by_id: dict[int, int] = {}
        for r in (cur.fetchall() or []):
            rid = r["id"] if isinstance(r, Mapping) else r[0]
            flag = r["is_detected"] if isinstance(r, Mapping) else r[1]
            detected_by_id[int(rid)] = int(flag or 0)
        valid = detected = 0
        # 🔴 逐**格**累加而不是逐 result 行:两格万一指向同一条 result,
        #    按行数会少算一格。分母的定义是格,就按格数。
        for rid in by_cell_result.values():
            if rid not in detected_by_id:
                # result 行不在了 ⇒ 这一格没有可引用的观测,不进分母。
                # (与上一版 JOIN 的语义逐值一致,只是搬到了 Python 侧。)
                continue
            valid += 1
            if detected_by_id[rid] == 1:
                detected += 1
        return (valid, detected)

    tally = guarded(cur, "occurrence_rate", _tally)
    if tally is None:
        return None
    valid, detected = tally
    if valid <= 0:
        return None
    # 服务端就把百分比算完(§15.6:前端不得自行合并 numerator/denominator)
    return round(detected * 100.0 / valid, 1)


def derive_level(card_key: str, *, summary: _proj.SampleSummary,
                 policy_signed: bool, rate: float | None = None, cur=None) -> str:
    """等级派生。**未签 policy ⇒ 恒 unknown**(MET-36)。

    🔴 阈值算法**不在这里**:它属于 ``defensive_presentation_level_policy_v1``
       (Owner 2026-08-24 按 Z-6 签发),实现在
       ``presentation.level_policy``。本函数只做三件事:
       查签发状态、查样本够不够、把出现率交给签发的政策去分档。
       执行方自己定一套阈值 = 用一个没人签过的标准给客户发承诺。

    🔴 ``rate is None`` ⇒ ``unknown``,而不是最低档。
       "没测到"与"测了很差"在客户眼里是两件完全不同的事。
    """
    if card_key not in card_keys():
        raise ProducerError(f"未知卡 {card_key!r}")
    if not policy_signed:
        return "unknown"
    if summary.valid <= 0:
        return "unknown"
    from services.defensive_geo.presentation import level_policy as _lp

    # cur 传下去:阈值是**动态系数**,要现取运行配置。
    # 用调用方已有的 cursor,绝不自己开连接(见 level_policy 里那段说明:
    # 事务中间再开连接 = 2026-08-10 自死锁的形态)。
    return _lp.level_for_rate(rate, cur)


def produce_cards(
    cur, *, plan_cell_ids: Sequence[str], policy_signed: bool | None = None,
    cutoff_at=None, raw_result_ids=None,
) -> tuple[_proj.CardView, ...]:
    """五卡投影 —— 全链:账本 → evidence → summary → cards。

    ``policy_signed`` 留 ``None`` 时**现取**窗B 的签发状态,
    不接受调用方自报"已签"(自报 = 绕过签发闸)。
    """
    if policy_signed is None:
        policy_signed = not _reg.unsigned_policies()

    # 🔴 [工单 V5-C · C-2] 账本**只读一遍**:evidence 与出现率吃同一份行。
    #    两条链共用一把裁剪刀(``_rows_by_cell``)之后,若各自再查一次账本,
    #    一份报告的往返次数就翻倍 —— 收口不该以性能倒退为代价。
    by_cell = _rows_by_cell(cur, plan_cell_ids=plan_cell_ids,
                            cutoff_at=cutoff_at, raw_result_ids=raw_result_ids)
    evidence, attempts = _evidence_from_rows(by_cell)
    summary = build_summary(plan_cell_ids=plan_cell_ids,
                            evidence=evidence, attempts=attempts)

    # 🔴 五卡当前**共用**同一份 sample summary。
    #    这是如实的:监测侧目前逐格只有一个终态,还没有逐卡切片
    #    (逐卡切片要 §15.6 的 MetricSlice,属 MET-17/MET-28 的作用域)。
    #    共用而不是伪造五份不同的数 —— 伪造会让"每张卡各自有依据"这件事
    #    看起来成立,而实际上五张卡背后是同一批格。
    summary_by_card = {k: summary for k in card_keys()}
    # [包F ⑧] 出现率来自**真实观测**(账本 → monitoring_results),
    # 等级由 Owner 签发的 Z-6 阈值政策分档。算不出来时 rate=None ⇒ unknown,
    # 而不是压成 0(那会把"没测到"讲成"一次都没出现")。
    rate = _rate_from_rows(cur, by_cell)
    level_by_card = {
        k: derive_level(k, summary=summary, policy_signed=bool(policy_signed),
                        rate=rate, cur=cur)
        for k in card_keys()
    }
    return _proj.project_cards(summary_by_card=summary_by_card,
                               level_by_card=level_by_card)


def cards_as_payload(cards: Sequence[_proj.CardView]) -> list[dict[str, Any]]:
    """转成 wire 形状。内部键由窗B 的 ``crop_for_audience`` 另行裁剪。"""
    return [
        {
            "key": c.key,
            "question": c.question,
            "ordinal": c.ordinal,
            "levelKey": c.level_key,
            "levelLabel": c.level_label,
            "levelTone": c.level_tone,
            "state": c.state,
            "actions": list(c.actions),
            "summary": {
                "planned": c.summary.planned,
                "attempted": c.summary.attempted,
                "terminal": c.summary.terminal,
                "policySkipped": c.summary.policySkipped,
                "attemptRecords": c.summary.attemptRecords,
                "attemptErrors": c.summary.attemptErrors,
                "response": c.summary.response,
                "valid": c.summary.valid,
                "identityAmbiguous": c.summary.identityAmbiguous,
                "engineErrors": c.summary.engineErrors,
            },
        }
        for c in cards
    ]


def census() -> dict[str, Any]:
    return {
        "producerVersion": CARD_PRODUCER_VERSION,
        "cardKeys": list(card_keys()),
        "projectionVersion": _proj.PROJECTION_VERSION,
        "unsignedPolicies": list(_reg.unsigned_policies()),
    }
