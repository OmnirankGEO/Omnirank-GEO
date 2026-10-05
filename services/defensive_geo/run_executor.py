"""正式诊断 run 的**执行器**(门三 G9)。

存在的理由是一条门库实证的资金事故
----------------------------------
confirm 做完「消费 preview + 建 command + 冻结 + outbox + 推进 running」就提交了。
然后**没有任何东西接手**。门库里 ``run_b6ad4e77…`` 就这么在 ``running`` 态挂了
23 分钟,心跳一次没动过,7800 算力一直冻着。

现役 sweeper 会在心跳超 5 分钟时把它收成 ``release_pending`` 退款 —— 但那是
**收尸**,不是干活:客户付了钱,报告永远不会出现。缺的那一环就是本模块。

设计要点(每条都是被某个具体坏结果逼出来的)
--------------------------------------------
1. **不自己写诊断管线**。用冻结题单驱动**现役** ``server.run_diagnosis_task`` ——
   它自带心跳、并发槽、结算(commit/release)与产物可见性。另写一套等于把
   结算谓词写两遍,而"同一谓词写两处 ⇒ 必有一处没人验"在资金上最贵。

2. **领取靠 DB 的 CAS,不靠应用层 select-then-update**。
   ``UPDATE … WHERE defgeo_dispatched_at IS NULL … RETURNING`` 一条语句完成
   「判断 + 占位」,20 个消费者并发也只有一个拿到 —— 承重的是那一行 SQL,
   不是 Python。

3. **``dispatch_attempts`` 是让 run 能被收敛的那一半**。派发反复失败时次数会涨,
   超阈值后本模块**不再领取**,run 落回 sweeper 的判死窗口被退款。
   没有它,一个坏 run 会被无限重投、永远冻着 —— 那就把"卡住"从
   "没人跑"换成了"一直在重跑",客户的钱一样回不来。

4. **只认自己的 run**(``session_id LIKE 'defgeo\\_%'``)。legacy run 崩了也满足
   "running + 从没派发",但它的原始请求体我拿不到,硬跑等于凭空编一次诊断。
   越界去跑别人的 run 是比不跑更坏的结果。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("GEO-DefGeoRunExecutor")


def _non_retryable_dispatch_types() -> tuple:
    """[#157] 哪些异常算「重投也没用」。

    🔴 只收**结构性**失败:DTO 校验不过(题面超限/字段非法)、题单自身不自洽。
       库抖动、下游超时、网络错一律**不**进这里 —— 把可重试的判成终态,
       等于把一次偶发变成一次退款。宁可多重试几次,也不要把能成的单判死。
    """
    types: list = []
    try:
        from pydantic import ValidationError
        types.append(ValidationError)
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.defensive_geo.question_plan import PlanIdentityError
        types.append(PlanIdentityError)
    except Exception:  # noqa: BLE001
        pass
    return tuple(types) or (_NeverRaised,)


class _NeverRaised(Exception):
    """占位:上面两个都导不进来时,不要让 `except ()` 变成捕获一切。"""


_NON_RETRYABLE_DISPATCH = _non_retryable_dispatch_types()


def _humanize_dispatch_failure(exc: Exception) -> str:
    """终态原因写成人话 —— 它会被前端看到。

    pydantic 的原始报文是给开发看的;这里给的是「为什么这单跑不起来」。
    """
    from services.diagnosis_question_pricing import MAX_QUESTION_CHARS

    text = str(exc)
    if "不能超过" in text or "question_too_long" in text:
        return (f"题单里有题超过 {MAX_QUESTION_CHARS} 字,跑不起来。"
                "常见原因是客户名很长、系统按名字拼出来的题就超了 —— "
                "请把那道题改短一点再确认一次。")
    return "这一单的题单或参数没通过校验,重试也不会变好:%s" % text[:300]

#: 本模块只处理 session_id 带这个前缀的 run(confirm 写入时就打好了)。
DEFGEO_SESSION_PREFIX = "defgeo_"

#: 派发失败多少次之后**放手**,让 sweeper 按状态机退款。
#: 不是"重试上限"那种参数感 —— 它是"什么时候承认跑不起来、该把钱还给客户"。
MAX_DISPATCH_ATTEMPTS = int(os.getenv("DEFGEO_MAX_DISPATCH_ATTEMPTS", "3"))


def progress_session_id(run_token: str) -> str:
    """run_token → legacy 进度端点/WS 真正校验的那把键(门三 G8)。

    ``auth.session_access.authorize_session`` 查的是 ``diagnosis_runs.session_id``。
    confirm 写进去的就是这个形状。**只此一处拼**,confirm 响应与执行器共用,
    免得两边各拼一遍、哪天改了前缀只改一处。
    """
    return DEFGEO_SESSION_PREFIX + str(run_token)


# ══════════════════════════════════════════════════════════════════════════
# 领取
# ══════════════════════════════════════════════════════════════════════════
_CLAIM_SQL = f"""
UPDATE diagnosis_runs
   SET defgeo_dispatched_at = NOW(),
       defgeo_dispatch_attempts = defgeo_dispatch_attempts + 1
 WHERE run_token = (
        SELECT run_token
          FROM diagnosis_runs
         WHERE run_status = 'running'
           AND session_id LIKE '{DEFGEO_SESSION_PREFIX}%%'
           AND defgeo_dispatched_at IS NULL
           AND defgeo_dispatch_attempts < %s
         ORDER BY created_at
         FOR UPDATE SKIP LOCKED
         LIMIT 1)
RETURNING run_token, session_id, owner_user_id, brand_id, billing_mode,
          defgeo_dispatch_attempts
"""


def claim_next_unstarted_run(cur, *, max_attempts: int = None) -> Optional[dict]:
    """原子领取一条「已确认、但从没人执行」的 defgeo run。

    领取 = 把 ``defgeo_dispatched_at`` 从 NULL CAS 成 NOW()(§12.3 的
    external-start marker)。并发下只有一个消费者能拿到,靠的是这条 SQL 自己,
    不是调用方的 if 判断。
    """
    cur.execute(_CLAIM_SQL, (int(max_attempts or MAX_DISPATCH_ATTEMPTS),))
    row = cur.fetchone()
    return dict(row) if row else None


def release_claim(cur, run_token: str) -> None:
    """派发失败时把 marker 放回 NULL —— 但**保留** attempts 计数。

    保留计数是关键:清零就等于给了这条 run 无限次重试,它会永远占着冻结的钱。
    attempts 涨到阈值后 `claim_next_unstarted_run` 不再领它,sweeper 会按状态机
    把钱退掉。「跑不起来」必须有终点。
    """
    cur.execute(
        "UPDATE diagnosis_runs SET defgeo_dispatched_at = NULL WHERE run_token = %s",
        (run_token,),
    )


def mark_dispatch_terminal(cur, run_token: str, reason: str) -> None:
    """[#157] **不可重试**的派发失败 ⇒ 判终态。

    🔴 与 `release_claim` 的区别只有一处,但那一处是全部:
       **不把 `defgeo_dispatched_at` 放回 NULL**。marker 留着 ⇒
       `claim_next_unstarted_run` 永远不再领它 ⇒ 一次即终,attempts 不再累加,
       run 落进 sweeper 的判死窗口按既有路径退款。

       归还 marker 对**可重试**的失败(库抖动、下游超时)是对的;
       对"这单结构上就跑不起来"(题面超限 ⇒ DTO 校验失败)是错的 ——
       它会把同一个必然失败重投 N 次,每次都炸在同一行,
       而用户看到的只是「一直没跑起来,最后退了钱」。

    原因落 `defgeo_dispatch_error`,前端/日志都看得见。
    """
    cur.execute(
        "UPDATE diagnosis_runs SET defgeo_dispatch_error = %s WHERE run_token = %s",
        (str(reason or "")[:2000], run_token),
    )


# ══════════════════════════════════════════════════════════════════════════
# 用冻结题单还原执行入参
# ══════════════════════════════════════════════════════════════════════════
def load_frozen_platform_keys(cur, run_token: str) -> list[str]:
    """从被消费的那一份 preview 里取**客户付了钱的那个检索面**(P0-2 返修③)。

    🔴 为什么必须是冻结面里的那一份,不是"现在的默认清单":
       她按下确认时看到的价格 = 题数 × **那一刻**的平台数,而且那份平台集
       和价格一起被冻进 ``frozen_payload``。执行时另取一份,就又变回
       「计价集与真跑集是两套」—— 只不过这次漂移发生在时间轴上。
    """
    cur.execute(
        "SELECT frozen_payload FROM defgeo_diagnosis_run_previews "
        "WHERE consumed_command_id = %s",
        (run_token,),
    )
    row = cur.fetchone()
    if not row:
        raise LookupError(f"run {run_token} 找不到被消费的 preview —— 无法还原付费平台集")
    keys = [str(k).strip() for k in ((row["frozen_payload"] or {}).get("platformKeys") or [])
            if str(k).strip()]
    if not keys:
        raise LookupError(f"run {run_token} 的冻结面里没有平台集 —— 不猜,交给 sweeper 退钱")
    return keys


def load_frozen_questions(cur, run_token: str) -> list[str]:
    """从**被消费的那一份** preview 反查冻结题单的题面。

    刻意按 ``consumed_command_id`` 反查,而不是按 brand 取"最新一份题单" ——
    客户签的是那一版,跑的就必须是那一版。取最新会出现"她确认的是 A、系统跑了 B"。
    """
    cur.execute(
        "SELECT question_plan_id, question_plan_revision "
        "FROM defgeo_diagnosis_run_previews WHERE consumed_command_id = %s",
        (run_token,),
    )
    prev = cur.fetchone()
    if not prev:
        raise LookupError(f"run {run_token} 找不到被消费的 preview —— 无法还原冻结题单")

    cur.execute(
        "SELECT frozen_payload FROM defgeo_question_plans "
        "WHERE plan_id = %s AND plan_revision = %s",
        (prev["question_plan_id"], prev["question_plan_revision"]),
    )
    plan = cur.fetchone()
    if not plan:
        raise LookupError(
            f"run {run_token} 的题单 {prev['question_plan_id']} "
            f"rev{prev['question_plan_revision']} 不存在"
        )

    payload = plan["frozen_payload"] or {}
    questions = [
        str(q.get("text") or "").strip()
        for q in (payload.get("questions") or [])
        if str(q.get("text") or "").strip()
    ]
    if not questions:
        raise LookupError(f"run {run_token} 的冻结题单里一道题都没有")
    return questions


def load_brand_context(cur, brand_id: Optional[int]) -> dict[str, Any]:
    """诊断管线要的品牌上下文。取不到就给保守默认,**不猜业务字段**。"""
    if not brand_id:
        return {"brand_name": "", "industry": ""}
    cur.execute("SELECT name, industry FROM brands WHERE id = %s", (int(brand_id),))
    row = cur.fetchone()
    if not row:
        return {"brand_name": "", "industry": ""}
    return {"brand_name": str(row["name"] or ""), "industry": str(row["industry"] or "")}


def build_diagnosis_request(claimed: dict, questions: list[str], brand: dict):
    """把冻结题单还原成现役 ``DiagnosisRequest``。

    题面走 ``custom_questions`` —— 现役口径就是「客户填啥跑啥」(verbatim,
    不主动优化)。这正是"以冻结题单驱动"的准确落点:不是把题面当灵感,
    是原样送进管线。
    """
    from server import DiagnosisRequest

    return DiagnosisRequest(
        brand_name=brand["brand_name"] or "未命名品牌",
        industry=brand["industry"] or "",
        # keywords 至少一个(min_length=1)。防御型 GEO 的题面**就是**检索单元,
        # 这里用品牌名兜住 schema,真正驱动检索的是 custom_questions。
        keywords=[brand["brand_name"] or "未命名品牌"],
        brand_id=claimed.get("brand_id"),
        custom_questions=questions,
        diagnosis_scope="geo",
    )


# ══════════════════════════════════════════════════════════════════════════
# 派发
# ══════════════════════════════════════════════════════════════════════════
async def dispatch_one() -> Optional[str]:
    """领取并派发一条。返回被派发的 run_token,没有可派发的返回 None。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        claimed = claim_next_unstarted_run(cur)
        if not claimed:
            return None
        run_token = str(claimed["run_token"])
        try:
            questions = load_frozen_questions(cur, run_token)
            platform_keys = load_frozen_platform_keys(cur, run_token)
            brand = load_brand_context(cur, claimed.get("brand_id"))
        except Exception as exc:
            # 还原不出入参 → 放回 marker(保留 attempts),让它最终落进 sweeper 退款
            logger.error("[defgeo-exec] %s 还原冻结题单失败: %s", run_token, exc)
            release_claim(cur, run_token)
            return None

    # ⚠️ 出了事务再派发:run_diagnosis_task 是长任务,把它包在打开的事务里会
    #    让这条连接被占住整场诊断(本仓记过「跑完不关事务」的伤害)。
    try:
        request = build_diagnosis_request(claimed, questions, brand)
        from server import run_diagnosis_task

        await run_diagnosis_task(
            request,
            str(claimed["session_id"]),
            int(claimed["owner_user_id"]),
            run_token=run_token,
            slot_mode="none",   # 槽由 confirm 侧不持有;这里不抢也不释
            # [P0-2 返修③] 把**客户买下的那个检索面**真的交给管线。
            # 不传时管线走自己的默认清单 —— 那正是门四实测到的
            # 「客户为 kimi 付钱、系统跑 yuanbao」。
            ai_engines=list(platform_keys),
        )
        logger.info("[defgeo-exec] %s 派发完成", run_token)
        return run_token
    except _NON_RETRYABLE_DISPATCH as exc:
        # [#157] 不可重试:题面/入参结构上就过不了 DTO —— 重投多少次都是同一行炸。
        #   🔴 **不归还 marker** = 一次即终态;attempts 不再涨,sweeper 按既有路径退款。
        reason = _humanize_dispatch_failure(exc)
        logger.error("[defgeo-exec] %s 派发**终态**(不重试): %s", run_token, reason)
        try:
            with get_db() as conn2:
                mark_dispatch_terminal(conn2.cursor(), run_token, reason)
        except Exception:
            logger.warning("[defgeo-exec] %s 终态原因落库失败(marker 已留,仍是终态)",
                           run_token)
        return None
    except Exception as exc:
        logger.exception("[defgeo-exec] %s 派发失败: %s", run_token, exc)
        try:
            with get_db() as conn2:
                release_claim(conn2.cursor(), run_token)
        except Exception:
            logger.warning("[defgeo-exec] %s 归还 marker 失败(交 sweeper 兜底)", run_token)
        return None


async def consume_confirmed_runs(limit: int = 1) -> dict:
    """周期消费。fail-soft:单条失败不影响下一条,也不影响其它 job。"""
    dispatched: list[str] = []
    for _ in range(max(1, int(limit))):
        try:
            token = await dispatch_one()
        except Exception as exc:  # noqa: BLE001 —— 兜底,job 不许把 scheduler 打挂
            logger.exception("[defgeo-exec] 消费异常: %s", exc)
            break
        if not token:
            break
        dispatched.append(token)
    return {"dispatched": len(dispatched), "run_tokens": dispatched}


def consume_confirmed_runs_sync() -> dict:
    """``new_event_loop`` 包装 —— APScheduler 的 worker 线程里没有 loop。

    与 ``services/research_monitor/selfserve_worker.consume_selfserve_queue_sync``
    同形,不另造一套。
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(consume_confirmed_runs())
    except Exception as exc:  # noqa: BLE001
        logger.error("[defgeo-exec] consume_confirmed_runs_sync 异常: %s", exc, exc_info=True)
        return {"dispatched": 0, "run_tokens": []}
    finally:
        try:
            loop.close()
        except Exception:
            pass
