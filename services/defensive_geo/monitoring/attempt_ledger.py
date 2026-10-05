"""追加式 attempt 账本 —— MON-01 / MON-03 / MON-08 / MON-10 / MON-11 的承重点。

🔴 为什么必须另存一张表(先证伪"现役够用了")
--------------------------------------------
现役 ``monitoring_run_cells`` 一格**一行**,重试走的是
``db/monitoring_db.finish_monitoring_cell_error``:

    UPDATE public.monitoring_run_cells
       SET state=%s, error_code=%s, error_message=%s, ...
     WHERE id=%s AND state='running' AND claim_token=%s

也就是说第二次尝试**原地覆盖** ``error_code`` / ``error_message``,
``retry_count`` 只 +1。于是:

* MON-03「重试建 child attempt,**不覆盖原始失败**」—— 现役做不到;
* MON-10「首个 attempt engine-error、fallback 成功时 final cell 可 answered,
  但 provider trace/SampleSummary **保留 attempt error**」—— 现役第一条错误已被
  第二次尝试擦掉,保留不了;
* MON-11「attemptRecords 与真实调用数一致」—— ``retry_count`` 是计数不是账本,
  重放/乱序时对不上账。

§13.1 明令「监测老链一行不改语义」,所以**不改** cell 的 UPDATE,
只在同一事务里追加一条 attempt 行。legacy(未 enrolled)路径零写入。

🔴 结构承重,不是应用层自觉
--------------------------
「不覆盖」这件事如果只写在应用代码里,任何人日后加一条 UPDATE 就破了。
所以承重点放在库里(迁移 046):

* ``UNIQUE (plan_cell_id, attempt_ordinal)`` —— 同一格的第 N 次尝试只能有一行;
* 终态行的 ``BEFORE UPDATE`` 触发器直接 RAISE —— 终态不可改写;
* partial unique ``(plan_cell_id) WHERE terminal_state IS NULL`` ——
  同一格同时至多一个在飞 attempt。

判据对这三条各有一发变异(把 UNIQUE 摘掉 / 把触发器摘掉 / 把 partial 摘掉),
必须转红。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Optional, Sequence

from services.defensive_geo.monitoring import lineage as _lin

LEDGER_VERSION = "defgeo-attempt-ledger-v1"

TABLE = "defgeo_monitoring_attempts"

#: 终态集合。**有限**且与 §6.3 守恒式一一对应:
#: ``answered`` 进 canonical outcome / ``entity_ambiguous`` 进 ambiguous /
#: ``engine_error`` 进 final engine error / ``policy_skipped`` 计 terminal 不计 attempted。
TERMINAL_STATES: tuple[str, ...] = (
    "answered",
    "entity_ambiguous",
    "engine_error",
    "policy_skipped",
)

#: MON-11 逐字:「skipped 无 provider attempt、**计 terminal 不计 attempted**」。
#: 于是它在 attempt 账本里是一条 ``provider_called=false`` 的记录 ——
#: 记它是为了让 terminal 数对得上,不记它 progressPct 就永远算不到 100。
NON_ATTEMPTED_TERMINAL_STATES: frozenset[str] = frozenset({"policy_skipped"})

#: MON-02 逐字:「401、429、超时不映射 absent」。这些 code 一律落 engine_error。
#: 判据拿它当分母:任何一个 code 被映射到 answered/not_mentioned 必红。
ENGINE_ERROR_CODES: tuple[str, ...] = (
    "PLATFORM_TEMPORARILY_UNAVAILABLE",
    "TIMEOUT",
    "RATE_LIMITED",
    "NO_VALID_RESPONSE",
    "COLLECTION_FAILED",
)

#: §6.3 逐字:「401、429、超时、解析失败只进入 engine_error;
#: 实体同名无法判断进入 entity_ambiguous。**两者都不能被压成"未提及"**」。
FORBIDDEN_ERROR_MAPPINGS: frozenset[str] = frozenset({
    "not_mentioned", "nonmention", "absent", "answered",
})


class AttemptLedgerError(ValueError):
    """账本写入不合法。**不写**,而不是写一条形态错的记录先用着。"""


class AttemptRecord(NamedTuple):
    attempt_id: str
    plan_cell_id: str
    attempt_ordinal: int
    parent_attempt_id: str | None
    terminal_state: str | None
    error_code: str | None
    provider_called: bool
    monitoring_result_id: int | None
    actual_provider: str
    actual_model: str
    actual_model_revision: str | None
    actual_surface: str
    actual_search_mode: str


def assert_error_not_absent(terminal_state: str, error_code: Optional[str]) -> None:
    """MON-02 的可执行形式。

    这不是"检查一下枚举有没有拼错" —— 它拦的是把平台故障算成"AI 没提到你",
    那会让客户看到一份"品牌提及率下降"的报告,而真相是我们没拿到回答。
    """
    if terminal_state in FORBIDDEN_ERROR_MAPPINGS and error_code:
        raise AttemptLedgerError(
            f"带 error_code={error_code!r} 的 attempt 被记成 {terminal_state!r} —— "
            "401/429/超时/解析失败只能进 engine_error,实体同名无法判断只能进 "
            "entity_ambiguous。两者都不能被压成「未提及」(§6.3 / MON-02)。"
        )
    if terminal_state == "engine_error" and not error_code:
        raise AttemptLedgerError(
            "engine_error 必须带 error_code —— 没有 code 的错误格给不出 reason/action,"
            "而 MON-11 要求「error/skipped 格有 reason/action」。"
        )


def _assert_terminal(state: str) -> str:
    if state not in TERMINAL_STATES:
        raise AttemptLedgerError(
            f"未知终态 {state!r};合法终态 = {list(TERMINAL_STATES)}。"
            "新终态必须进这张表并升 LEDGER_VERSION —— 现场造一个等于守恒式漏一项。"
        )
    return state


def open_attempt(
    cur,
    *,
    plan_cell_id: str,
    attempt_ordinal: int,
    run_authority_id: str,
    tenant_owner_user_id: int,
    brand_id: int,
    monitoring_cell_id: int | None,
    parent_attempt_id: str | None,
    actual_provider: str,
    actual_model: str,
    actual_model_revision: str | None,
    actual_surface: str,
    actual_search_mode: str,
    request_hash: str,
) -> str:
    """派发一次真实调用前登记。返回 ``attempt_id``。

    🔴 在**派发前**登记,不是在拿到结果后登记:拿到结果后才记,
       进程在 provider 调用与落库之间崩掉的那些 attempt 会整条消失,
       ``attemptRecords`` 与真实调用数就对不上(MON-11)。
    """
    aid = _lin.attempt_id(
        plan_cell_id=plan_cell_id,
        attempt_ordinal=attempt_ordinal,
        actual_provider=actual_provider,
        actual_model=actual_model,
        actual_model_revision=actual_model_revision,
        actual_surface=actual_surface,
        actual_search_mode=actual_search_mode,
        request_hash=request_hash,
    )
    cur.execute(
        f"""
        INSERT INTO public.{TABLE}
            (attempt_id, plan_cell_id, attempt_ordinal, parent_attempt_id,
             run_authority_id, tenant_owner_user_id, brand_id, monitoring_cell_id,
             actual_provider, actual_model, actual_model_revision,
             actual_surface, actual_search_mode, request_hash,
             provider_called, ledger_version)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s)
        ON CONFLICT (attempt_id) DO NOTHING
        RETURNING attempt_id
        """,
        (aid, plan_cell_id, int(attempt_ordinal), parent_attempt_id,
         run_authority_id, int(tenant_owner_user_id), int(brand_id),
         monitoring_cell_id,
         actual_provider, actual_model, actual_model_revision,
         actual_surface, actual_search_mode, request_hash, LEDGER_VERSION),
    )
    # ON CONFLICT DO NOTHING + 零行 = 同一 attempt 身份重放(崩溃恢复 / 重复回调)。
    # MON-08「重复回调、重试、镜像不重复入分母」就落在这一位上。
    return aid


def record_policy_skip(
    cur,
    *,
    plan_cell_id: str,
    run_authority_id: str,
    tenant_owner_user_id: int,
    brand_id: int,
    reason_code: str,
) -> str:
    """MON-11:policy-skipped 格 **零 provider 调用**,计 terminal 不计 attempted。

    ordinal 固定 1 —— 它从来没被尝试过,不存在"第二次跳过"。
    """
    aid = _lin.attempt_id(
        plan_cell_id=plan_cell_id,
        attempt_ordinal=1,
        actual_provider="__policy_skipped__",
        actual_model="__policy_skipped__",
        actual_model_revision=None,
        actual_surface="__policy_skipped__",
        actual_search_mode="__policy_skipped__",
        request_hash=reason_code,
    )
    cur.execute(
        f"""
        INSERT INTO public.{TABLE}
            (attempt_id, plan_cell_id, attempt_ordinal, parent_attempt_id,
             run_authority_id, tenant_owner_user_id, brand_id, monitoring_cell_id,
             actual_provider, actual_model, actual_model_revision,
             actual_surface, actual_search_mode, request_hash,
             provider_called, terminal_state, error_code, terminal_at, ledger_version)
        VALUES (%s,%s,1,NULL,%s,%s,%s,NULL,
                '__policy_skipped__','__policy_skipped__',NULL,
                '__policy_skipped__','__policy_skipped__',%s,
                FALSE,'policy_skipped',%s,NOW(),%s)
        ON CONFLICT (attempt_id) DO NOTHING
        """,
        (aid, plan_cell_id, run_authority_id, int(tenant_owner_user_id),
         int(brand_id), reason_code, reason_code, LEDGER_VERSION),
    )
    return aid


def close_attempt(
    cur,
    *,
    attempt_id: str,
    terminal_state: str,
    error_code: str | None = None,
    error_message: str | None = None,
    monitoring_result_id: int | None = None,
    observed_provider: str | None = None,
    observed_model: str | None = None,
    observed_model_revision: str | None = None,
    observed_surface: str | None = None,
    observed_search_mode: str | None = None,
) -> bool:
    """把在飞 attempt 收成终态。返回是否真的收了(False = 已是终态,幂等重放)。

    🔴 ``WHERE terminal_state IS NULL`` 是 CAS —— 已终态的行**不会**被这条改动。
       库里另有 BEFORE UPDATE 触发器兜底(应用层漏了这个 WHERE 也改不动),
       两道是**纵深**不是重复:判据要分别拆开验。

    🔴 ``observed_*``(包F ①)= **收口时才知道**的真 lineage。
       ``model_revision`` 尤其如此:平台只在回答里带版本,派发前拿不到。
       这几列**不参与** ``attempt_id``(见 §6.1 与
       ``run_ledger_bridge._cell_attempt_id`` 的说明),所以在这里写入
       不改变任何唯一性;不写的话账本永远只有"计划发哪个模型",
       拿不到"实际跑的是哪个版本" —— 而后者才是保真度对账要的那一位。

       全部留 ``None`` 时**一列都不动**(``COALESCE`` 语义写在 SQL 里),
       所以老调用点行为逐字节不变。这是"追加谓词、不改写默认路径"。
    """
    _assert_terminal(terminal_state)
    assert_error_not_absent(terminal_state, error_code)
    # 空串与 None 不同义:``chk_defgeo_attempt_no_empty_revision`` 禁空串
    # revision,而"平台没给版本"必须是 NULL。所以空串一律折成 None,
    # 不让它变成一个合法但没有意义的值。
    revision = (observed_model_revision or None)
    if revision is not None and not str(revision).strip():
        revision = None
    cur.execute(
        f"""
        UPDATE public.{TABLE}
           SET terminal_state=%s, error_code=%s, error_message=%s,
               monitoring_result_id=%s, terminal_at=NOW(),
               actual_provider    = COALESCE(NULLIF(%s, ''), actual_provider),
               actual_model       = COALESCE(NULLIF(%s, ''), actual_model),
               -- [工单 E3-4 · P1-9b] 只有**真的**收到回显才把来源改成
               -- provider_echo;否则保持 open 时写下的 planned_fallback。
               -- 🔴 与上一行由**同一个入参**(observed_model)驱动 ——
               --    用两个不同的入参会让"改了模型却没改来源"变得可表达。
               -- 🔴 注释里不写占位符字面量:psycopg2 连 SQL **注释里**的
               --    占位符也算进参数个数(本行第一版就栽在这上面,
               --    错误是 IndexError: tuple index out of range,
               --    与"参数写少了"长得一模一样)。
               actual_model_source = CASE
                   WHEN NULLIF(%s, '') IS NOT NULL THEN 'provider_echo'
                   ELSE actual_model_source END,
               actual_model_revision = COALESCE(%s, actual_model_revision),
               actual_surface     = COALESCE(NULLIF(%s, ''), actual_surface),
               actual_search_mode = COALESCE(NULLIF(%s, ''), actual_search_mode)
         WHERE attempt_id=%s AND terminal_state IS NULL
        """,
        (terminal_state, error_code, (error_message or None),
         monitoring_result_id,
         observed_provider, observed_model, observed_model, revision,
         observed_surface, observed_search_mode,
         attempt_id),
    )
    return int(cur.rowcount) == 1


def next_ordinal(cur, *, plan_cell_id: str) -> int:
    """同一 plan cell 的下一个 attempt 序号。

    🔴 用 ``MAX+1`` 而不是"重试次数 +1":重试计数会被回填/修数改掉,
       而账本里的最大序号是**事实**。并发由 ``UNIQUE(plan_cell_id, ordinal)``
       兜底 —— 两个并发都算出同一个 N 时,后一个 INSERT 撞唯一约束失败,
       这正是我们要的(不是两条第 N 次尝试)。
    """
    cur.execute(
        f"SELECT COALESCE(MAX(attempt_ordinal), 0) AS m FROM public.{TABLE} "
        "WHERE plan_cell_id=%s",
        (plan_cell_id,),
    )
    row = cur.fetchone()
    m = row["m"] if isinstance(row, Mapping) else (row[0] if row else 0)
    return int(m or 0) + 1


def attempts_for_cell(cur, *, plan_cell_id: str,
                      cutoff_at=None) -> list[AttemptRecord]:
    """这一格的 attempt 行。

    🔴 [工单 V3-C · C-4 · Codex 三审 P1-6] ``cutoff_at`` 给定时**只取在冻结
       时刻之前已经终态**的 attempt。冻结报告必须走这条路径:
       不带 cutoff 时,cutoff 之后的迟到 retry 会把一份**已经发给客户**的
       报告改写 —— Codex 真 PG16 反例:同一 report snapshot 初次是
       ``attemptRecords=1 / response=0 / engineErrors=1 / no_conclusion``,
       插入 cutoff 之后的 fallback answered attempt 后,同一 snapshot 变成
       ``attemptRecords=2 / response=1 / engineErrors=0 / ready``。

    🔴 ``terminal_at IS NULL``(还在跑)在有 cutoff 时也**排除**:
       冻结那一刻它还不是事实,不该出现在冻结面里。
    🔴 缺省 ``None`` = 实时面(``LiveMonitoringView``),它本来就该看全量。
       两种读面共用一个函数、由参数分流,好过写两份查询(同一谓词写两处)。
    """
    where = "plan_cell_id=%s"
    params: list = [plan_cell_id]
    if cutoff_at is not None:
        where += " AND terminal_at IS NOT NULL AND terminal_at <= %s"
        params.append(cutoff_at)
    cur.execute(
        f"""
        SELECT attempt_id, plan_cell_id, attempt_ordinal, parent_attempt_id,
               terminal_state, error_code, provider_called, monitoring_result_id,
               actual_provider, actual_model, actual_model_revision,
               actual_surface, actual_search_mode
          FROM public.{TABLE}
         WHERE {where}
         ORDER BY attempt_ordinal
        """,
        tuple(params),
    )
    return [
        AttemptRecord(
            attempt_id=r["attempt_id"], plan_cell_id=r["plan_cell_id"],
            attempt_ordinal=int(r["attempt_ordinal"]),
            parent_attempt_id=r["parent_attempt_id"],
            terminal_state=r["terminal_state"], error_code=r["error_code"],
            provider_called=bool(r["provider_called"]),
            monitoring_result_id=r["monitoring_result_id"],
            actual_provider=r["actual_provider"], actual_model=r["actual_model"],
            actual_model_revision=r["actual_model_revision"],
            actual_surface=r["actual_surface"],
            actual_search_mode=r["actual_search_mode"],
        )
        for r in (cur.fetchall() or [])
    ]


# ══════════════════════════════════════════════════════════════════════
# canonical selection policy(§6.1「为每个 plan cell **最多选择一个**业务
# projection;其它尝试仍作为诊断证据保留」)
# ══════════════════════════════════════════════════════════════════════

CANONICAL_SELECTION_POLICY_VERSION = "defgeo-canonical-attempt-selection-v1"

#: 择优序:先看有没有成功答上来的,再看有没有身份待确认的,最后才是错误。
#: 同档内**取序号最小的那一次** —— 不是最后一次。
#: 🔴 取最小而不是最大:MON-10 逐字「首个 attempt engine-error、fallback 成功时
#:    final cell 可 answered」,择优是按**结果质量**不是按时间;
#:    而同为 answered 时取第一次成功,才不会让"多重试几次"改写已成立的事实。
_SELECTION_RANK: dict[str, int] = {
    "answered": 0,
    "entity_ambiguous": 1,
    "engine_error": 2,
    "policy_skipped": 3,
}


def canonical_attempt(attempts: Sequence[AttemptRecord]) -> AttemptRecord | None:
    """§6.1 的 canonical selection policy。无终态 attempt 时返回 None。

    🔴 「其它尝试仍作为诊断证据保留」—— 本函数**只选**不删。调用方拿到
       canonical 之后仍必须把全量 attempts 下发进 provider trace(MON-10)。
    """
    terminal = [a for a in attempts if a.terminal_state]
    if not terminal:
        return None
    return min(
        terminal,
        key=lambda a: (_SELECTION_RANK[a.terminal_state], a.attempt_ordinal),
    )


def cell_denominator_facts(attempts: Sequence[AttemptRecord]) -> dict[str, Any]:
    """把一格的 attempts 折成 §6.3 守恒式要的那几位。

    ``attempted`` 逐字 = 「至少生成一条**耐久 attempt**」——
    policy_skipped 没有 provider 调用,所以它不进 attempted(MON-11)。
    """
    terminal = [a for a in attempts if a.terminal_state]
    canonical = canonical_attempt(attempts)
    provider_attempts = [a for a in attempts if a.provider_called]
    return {
        "attemptRecords": len(attempts),
        "providerAttempts": len(provider_attempts),
        "isAttempted": bool(provider_attempts),
        "isTerminal": bool(terminal),
        "canonicalState": canonical.terminal_state if canonical else None,
        "attemptErrors": [
            {"attemptOrdinal": a.attempt_ordinal, "errorCode": a.error_code}
            for a in attempts if a.error_code
        ],
    }


def census() -> dict[str, Any]:
    return {
        "ledgerVersion": LEDGER_VERSION,
        "table": TABLE,
        "terminalStates": list(TERMINAL_STATES),
        "nonAttemptedTerminalStates": sorted(NON_ATTEMPTED_TERMINAL_STATES),
        "engineErrorCodes": list(ENGINE_ERROR_CODES),
        "forbiddenErrorMappings": sorted(FORBIDDEN_ERROR_MAPPINGS),
        "selectionPolicyVersion": CANONICAL_SELECTION_POLICY_VERSION,
        "selectionRank": dict(_SELECTION_RANK),
    }
