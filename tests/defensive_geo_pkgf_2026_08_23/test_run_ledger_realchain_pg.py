"""包F ① 真链判据 —— 走**真** monitoring_db 函数 + **真** PG + **真** HTTP。

工单指定的口径逐字:「真实执行后 HTTP progress 证明 0 → 运行中 → 100」。

🔴 为什么必须打真函数而不是自己拼 INSERT
----------------------------------------
本仓记过两次同形的:
* 「夹具用的键是生产从来不会发的键」—— 整片端点生产必 500 而判据全绿;
* 「单对象夹具让整条链没接线也全绿」。
所以这里 cell 的**状态迁移**全部由 ``claim_monitoring_run_cell`` /
``finish_monitoring_cell_error`` / ``save_monitoring_result`` 这些**真函数**
驱动。只有"计划"这一步(建格)用直接 INSERT —— 它是**输入**,
不是被测对象;而它的形状完整性由 ``test_run_ledger_wiring`` 里那条
列名探针单独守。

🔴 progress 走真 HTTP
--------------------
本仓记过「真 HTTP 判据必须打真库」:只验 ``!= 422`` 会漏掉整层库合同。
这里 TestClient 打真 router,router 连本包那个一次性真库。
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

import psycopg2.extras
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.defensive_geo.monitoring import attempt_ledger as LEDGER

BRAND = 9101
TASK_BRAND = BRAND
#: 格上**冻结**的租户(``monitoring_run_cells.tenant_owner_user_id``)——
#: 由 :func:`_seed_cell` 无条件写死,代表"这一格建出来那一刻的归属"。
USER = 4242
#: 🔴 [E3 补洞 2026-08-27 · 外选 MUT-EXTE3-03] 品牌**转移之后**的当前 owner。
#: 与 :data:`USER` 不同值是有意的,而且正是本单要判的那件事:
#: 归属**不许**随品牌转移漂移 —— 冻结的是 USER,当前 owner 是这一个。
#: 身份判据的操作者也用它(走 owner 授权臂;admin 授权臂由 R4 那条判据走)。
#: 判别力自证见 ``test_the_identity_fixture_keeps_the_three_ids_distinct``。
OWNER_AFTER_TRANSFER = USER + 1
PROGRESS = "/api/defensive-geo/monitoring/runs/{tid}/progress"

_PLAN_COLUMNS = (
    "task_id", "brand_id", "keyword_id", "keyword_source", "platform",
    "is_planned", "state", "plan_hash", "entitlement_snapshot",
    "tenant_owner_user_id", "error_code", "error_message", "completed_at",
)


def _plan_hash(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


@pytest.fixture
def task(cur) -> int:
    cur.execute(
        "INSERT INTO monitoring_tasks (brand_id, total_tests) VALUES (%s,%s) "
        "RETURNING id", (TASK_BRAND, 0))
    tid = int(cur.fetchone()["id"])
    # 🔴 必须 commit:``cur`` 现在是 autocommit=False(见 conftest 里那段说明),
    #    而被测的真函数(claim_monitoring_run_cell 等)开的是**自己的连接** ——
    #    没提交的行它们看不见,于是会一路报 MonitoringCellNotFound。
    cur.connection.commit()
    return tid


def _seed_cell(cur, *, task_id: int, keyword_id: int, platform: str,
               is_planned: bool = True, state: str = "queued",
               keyword_snapshot: str | None = None,
               question_snapshot: str | None = None,
               target_brand_snapshot: str | None = None,
               retry_coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    """建一格 —— 这是**计划**(输入),不是被测对象。

    列集合逐值取生产 ``create_monitoring_run_cells`` 那条 INSERT 的形状。

    三个 ``*_snapshot`` 默认 ``None`` = 建这个 helper 时的原样(那几列本就
    不在 INSERT 列表里,写 NULL 与不写同结果)。R2 F-1 需要它们:
    ``save_monitoring_result`` 会拿 lineage 与这三列**逐值比对**,对不上
    直接 ``MonitoringCellConflict`` —— 走成功路径就必须先让它们对得上。
    """
    ph = _plan_hash(f"{task_id}:{keyword_id}:{platform}")
    cur.execute(
        """
        INSERT INTO monitoring_run_cells
            (task_id, brand_id, keyword_id, keyword_source, platform,
             is_planned, state, plan_hash, entitlement_snapshot,
             tenant_owner_user_id, error_code, error_message, completed_at,
             keyword_snapshot, question_snapshot, target_brand_snapshot)
        VALUES (%s,%s,%s,'confirmed',%s,%s,%s,%s,%s,%s,%s,%s,
                CASE WHEN %s THEN NULL ELSE NOW() END, %s,%s,%s)
        RETURNING *
        """,
        (task_id, TASK_BRAND, keyword_id, platform, is_planned, state, ph,
         psycopg2.extras.Json({"schema_version": "monitoring-entitlement-snapshot-v1",
                               "search_mode": "enhanced",
                               "platforms": [platform],
                               # R3:重试臂判据要它;默认 None 时这一键不出现,
                               # 与建这个 helper 时的形状逐字相同。
                               **({"retry_coverage": retry_coverage}
                                  if retry_coverage else {})}),
         USER,
         None if is_planned else "not_in_purchased_run_plan",
         None if is_planned else "当前履约计划不包含该平台",
         is_planned,
         keyword_snapshot, question_snapshot, target_brand_snapshot),
    )
    row = dict(cur.fetchone())
    cur.connection.commit()   # 同上:跨连接可见性
    return row


def _attempts(cur, plan_cell_id: str) -> list[dict[str, Any]]:
    # 🔴 先结束当前只读快照:真函数在**别的连接**里提交了行,
    #    而 REPEATABLE READ 之外的默认 READ COMMITTED 也需要新语句才看得见
    #    上一次 commit 之后的数据。不 commit 的话这里读的是陈旧快照 ——
    #    本仓记过「幂等端点让真链判据读陈旧行」,同一类坑。
    cur.connection.commit()
    cur.execute(
        f"SELECT * FROM {LEDGER.TABLE} WHERE plan_cell_id=%s ORDER BY attempt_ordinal",
        (plan_cell_id,))
    return [dict(r) for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════════════════
# HTTP 面
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def client(live_db) -> Iterator[TestClient]:
    import importlib

    mod = importlib.import_module("api.defensive_monitoring_api")
    app = FastAPI()
    app.include_router(mod.router)

    @app.middleware("http")
    async def _inject(request: Request, call_next):        # noqa: ANN001
        # 🔴 键名是 ``user_id`` —— 本仓记过一次很贵的:夹具写 ``id``
        #    而中间件写的是 ``user_id``,于是整片端点生产必 500 而判据全绿。
        request.state.user = {"user_id": USER, "id": USER, "role": "agent",
                              "agent_level": 1}
        return await call_next(request)

    # 归属:本包库里没有 brands 表,直接放行到对象级之后的逻辑。
    import auth.brand_access as ba
    orig = ba.require_brand_access
    ba.require_brand_access = lambda *a, **k: None
    try:
        with TestClient(app) as c:
            yield c
    finally:
        ba.require_brand_access = orig


def _progress(client: TestClient, task_id: int) -> dict[str, Any]:
    resp = client.get(PROGRESS.format(tid=task_id), params={"brandId": BRAND})
    assert resp.status_code == 200, (
        f"progress 不是 200(入口关了?库合同不对?):{resp.status_code} {resp.text[:400]}")
    return resp.json()


# ══════════════════════════════════════════════════════════════════════
# 工单指定判据:0 → 运行中 → 100
# ══════════════════════════════════════════════════════════════════════

def test_progress_walks_zero_to_running_to_hundred(cur, live_db, client, task):
    """🔴 工单口径逐字:真实执行后 HTTP progress 证明 0 → 运行中 → 100。

    三个观测点各自都是**真状态**,不是构造出来的中间值
    (本仓记过:判据自己构造中间值 ⇒ 没驱动那一行 ⇒ 变异存活)。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  finish_monitoring_cell_error)

    c1 = _seed_cell(cur, task_id=task, keyword_id=1, platform="dashscope")
    c2 = _seed_cell(cur, task_id=task, keyword_id=1, platform="kimi")

    # ── ① 还没跑:0% ─────────────────────────────────────────────────
    p0 = _progress(client, task)
    assert p0["plannedCells"] == 2, p0
    assert p0["terminalCells"] == 0, p0
    assert p0["progressPct"] == 0.0, p0
    assert p0["attemptRecords"] == 0, f"还没 claim 就有 attempt 了:{p0}"

    # ── ② 真 claim 一格:运行中(attempt 在飞,但还没 terminal)────────
    claim_monitoring_run_cell(cell_id=int(c1["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    p1 = _progress(client, task)
    assert p1["attemptRecords"] == 1, (
        f"claim 之后账本里没有 attempt —— 接线点①没生效:{p1}")
    assert p1["terminalCells"] == 0, f"在飞不该算 terminal:{p1}"
    assert p1["attemptedCells"] == 1, f"在飞就该算 attempted:{p1}"
    assert 0.0 == p1["progressPct"], f"在飞不该推进百分比:{p1}"

    # ── ③ 两格都收口:100% ───────────────────────────────────────────
    finish_monitoring_cell_error(
        cell_id=int(c1["id"]), claim_token=_claim_token(cur, c1["id"]),
        state="failed", error_code="TIMEOUT", error_message="probe")
    claim_monitoring_run_cell(cell_id=int(c2["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    finish_monitoring_cell_error(
        cell_id=int(c2["id"]), claim_token=_claim_token(cur, c2["id"]),
        state="unavailable", error_code="RATE_LIMITED", error_message="probe")

    p2 = _progress(client, task)
    assert p2["terminalCells"] == 2, f"收口了却不算 terminal:{p2}"
    assert p2["progressPct"] == 100.0, f"两格全收口却不是 100%:{p2}"
    assert p2["attemptRecords"] == 2, f"attemptRecords 与真实调用数不符:{p2}"
    # MON-11:error 格必须带 reason
    codes = {e["errorCode"] for e in p2["attemptErrors"]}
    assert codes == {"TIMEOUT", "RATE_LIMITED"}, (
        f"error 格的 reason 丢了或串了:{p2['attemptErrors']}")


# ══════════════════════════════════════════════════════════════════════
# [R2 F-1] 成功路径的真链判据
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 R1 缺的就是这一条。上面那条 0→100 判据两格收口走的**都是错误路径**
#    (TIMEOUT / RATE_LIMITED),于是"从 ``save_monitoring_result`` 穿到账本
#    answered 行"这条线**没有任何判据在驱动**。
#    Review 亲手注毒(把 ``close_for_result`` 改绑成 no-op lambda ——
#    调用行还在,所以 AST 接线锁照绿)⇒ **70 条全绿存活**。
#
#    教训是老的:接线锁证明"这一行写在那儿了",证明不了"它干的事是对的"。
#    承重的那一格必须有行为判据驱动。

_LINEAGE_KEYWORD = "防御型监测关键词"
_LINEAGE_QUESTION = "这个行业里有哪些值得关注的品牌?"
_LINEAGE_BRAND = "被测品牌"


def _success_lineage(**over: Any) -> dict[str, Any]:
    """一份**通得过** ``save_monitoring_result`` 全部一致性校验的 lineage。

    这些值不是随手填的:``keyword_source`` / ``sent_question_snapshot`` /
    ``target_brand`` 三项会与 cell 上的快照逐值比对,对不上直接冲突。

    🔴 [工单 V3-C 补笔 · 2026-08-28] ``model_source`` **必须显式带上**。
       C-3(b0ee6197e)之后的合同是:``model`` 从哪来由调用方**声明**,
       不再由"给没给 model"推断 —— 因为唯一的生产调用方恒给一个硬编码
       **计划**模型名,旧推断会把每一行都标成"已被供应商证实"(Codex 三审 P1-5)。

       生产侧 ``PlatformAdapter.query`` 的两个血缘返回点现在都铺开
       ``_lineage_model_fields()``,**永远同时**给出 ``model`` 与 ``model_source``
       (五个 `save_monitoring_result` 生产调用点全部由它喂,已 census)。
       所以"有 model、无 model_source"是一个**生产不会再发的形状** ——
       夹具继续造它,就是拿一个生产不存在的形状去验生产代码(本仓记过)。

       这里取 ``provider_echo``:本夹具自称是一次真实成功调用,而且它填了
       ``model_revision``(按本文件自己的说法:平台只在回答里带版本,派发前
       拿不到)—— 也就是说它模拟的本来就是"平台把这些告诉了我们"。
       声明为真回显,与它自己的语义一致,且与 C-3 之前的**实际效果逐值相同**
       (旧规则下 model 非空即 provider_echo),所以本文件其余 7 处用它的判据
       命题一个都没有移动。
    """
    payload = {
        "platform": "dashscope",
        "provider": "dashscope",
        "model": "qwen3.7-plus",
        "model_source": "provider_echo",
        "model_revision": "20260824",
        "surface": "ai_search",
        "search_mode": "enhanced",
        "keyword_source": "confirmed",
        "keyword_type": "brand",
        "keyword_resolver_status": "resolved",
        "sent_question_snapshot": _LINEAGE_QUESTION,
        "target_brand": _LINEAGE_BRAND,
        "response_status": "success",
        "request_id": "req-r2-f1",
        "sent_at": "2026-08-24T00:00:00Z",
    }
    payload.update(over)
    return payload


def _seed_success_cell(cur, *, task_id: int, keyword_id: int) -> dict[str, Any]:
    return _seed_cell(
        cur, task_id=task_id, keyword_id=keyword_id, platform="dashscope",
        keyword_snapshot=_LINEAGE_KEYWORD,
        question_snapshot=_LINEAGE_QUESTION,
        target_brand_snapshot=_LINEAGE_BRAND,
    )


def test_success_path_writes_an_answered_row_through_the_live_chain(
        cur, live_db, task):
    """[R2 F-1] 真 ``save_monitoring_result`` ⇒ 账本 ``answered`` 行 + 回连 + observed_*。

    四件事一起判,因为它们在同一跳里要么全对要么全错:
      ① terminal_state = answered(不是 not_mentioned,不是空);
      ② monitoring_result_id **回连到真实那一行**(不是 0 / 不是别人的 id);
      ③ observed_* 逐值 = 收口时的真 lineage(保真对账要的就是这几位);
      ④ provider_called = True(这一跳真的打过 provider,与人工确认那跳相反)。
    """
    from db.monitoring_db import claim_monitoring_run_cell, save_monitoring_result

    c = _seed_success_cell(cur, task_id=task, keyword_id=31)
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")

    result_id = save_monitoring_result(
        task_id=task, keyword_id=31, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=True, mention_type="direct",
        full_response="回答里提到了被测品牌。", lineage=_success_lineage(),
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )
    assert isinstance(result_id, int) and result_id > 0, result_id

    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 1, f"成功收口后账本行数不对:{rows}"
    row = rows[0]

    # ① 终态
    assert row["terminal_state"] == "answered", (
        f"成功路径没写成 answered —— 接线点②没真正生效:{row}")
    # ② 回连
    assert row["monitoring_result_id"] == result_id, (
        f"账本回连到了 {row['monitoring_result_id']},真实结果行是 {result_id} —— "
        f"回连错了等于账本指着别人家的证据")
    # ③ observed_* 逐值
    #    (列名是 ``actual_*``:开 attempt 时写的是**计划**值,收口时由
    #     ``close_attempt`` 用 ``COALESCE(NULLIF(...))`` 覆盖成**实际**值。
    #     一列两阶段,所以"覆盖有没有真发生"必须判 —— 下一条专门判它。)
    assert row["actual_provider"] == "dashscope", row
    assert row["actual_model"] == "qwen3.7-plus", row
    assert row["actual_model_revision"] == "20260824", (
        f"model_revision 没落位:{row['actual_model_revision']!r} —— "
        f"平台只在回答里带版本,派发前拿不到,这一跳不写就永远没有")
    assert row["actual_surface"] == "ai_search", row
    assert row["actual_search_mode"] == "enhanced", row
    # ④ 这一跳是真打过 provider 的
    assert row["provider_called"] is True, row


def test_success_path_overwrites_the_planned_lineage_with_the_observed_one(
        cur, live_db, task):
    """[R2 F-1] 收口必须把**计划值**覆盖成**实际值**,而不是原样留着。

    🔴 没有这一条,上一条会被一个"``close_for_result`` 干脆不传 observed_*"
       的实现骗过去 —— 那时 ``actual_model`` 里躺的还是开 attempt 时写的
       **计划**值,和真跑的那个长得一模一样(正常情况下二者本来就相等),
       于是判据全绿而"实际跑了什么"这一位从来没被记录过。
       保真对账(包F ⑥)要的正是这一位。

    做法:故意让实际值与合同计划值**不同**,再看账本记的是哪一个。
    ``actual_*`` 是一列两阶段(开=计划 / 收口=实际),所以这条判据同时
    证明了"覆盖真的发生了"。
    """
    from db.monitoring_db import claim_monitoring_run_cell, save_monitoring_result
    from services.engine_contract import PLATFORM_CONTRACT

    planned_model = PLATFORM_CONTRACT["dashscope"]["model"]
    drifted = "qwen3.7-plus-hotfix-b"
    assert drifted != planned_model, "构造失败:实际值必须与计划值不同"

    c = _seed_success_cell(cur, task_id=task, keyword_id=32)
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")

    # 派发前:账本里记的是**计划**发哪个模型(平台合同值)
    planned = _attempts(cur, c["plan_hash"])[0]
    assert planned["actual_model"] == planned_model, (
        f"派发前记的计划模型不是合同值:{planned['actual_model']}")
    assert planned["actual_model_revision"] is None, (
        "派发前就有 model_revision —— 那不可能是真的,平台只在回答里带版本")

    save_monitoring_result(
        task_id=task, keyword_id=32, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=False, mention_type="none",
        full_response="没提到。", lineage=_success_lineage(model=drifted),
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["actual_model"] == drifted, (
        f"收口后 actual_model 还是 {row['actual_model']} —— 计划值没被实际值"
        f"覆盖,等于'实际跑了什么'这一位从来没记过,保真对账永远发现不了漂移")
    assert row["actual_model_revision"] == "20260824", row


def test_an_undeclared_model_is_not_written_as_the_observed_one(
        cur, live_db, task):
    """[工单 V3-C · C-3 的正样本落在 pkgf 的形态] **没声明来源**的模型值
    不许被当成观测值覆盖上去。

    上一条是"真回显 ⇒ 覆盖";这一条是它的**反例臂**:同一个漂移模型名,
    只是调用方**没有声明**它来自供应商回显 ⇒ 账本必须保留派发时写下的
    **计划**值,并且 ``actual_model_source`` 保持 ``planned_fallback``。

    🔴 为什么这一条必须在 pkgf 而不是只在 e3:
       e3 打的是 ``build_monitoring_lineage`` 与 ``normalize_lineage_payload``
       的纯函数层;**"计划值不许被写成 observed"这一跳落在真链上**——
       ``save_monitoring_result`` → ``close_for_result(observed_model=...)``
       → ``close_attempt`` 的 ``COALESCE(NULLIF(%s,''), actual_model)``。
       纯函数层全绿而这一跳接反了的话,``actual_model`` 照样被一个计划值
       "确认"过 —— 而它同时是保真度对账与计价的取数口。

    🔴 少了这一条,把 ``monitoring_db`` 里那个 ``model_source == "provider_echo"``
       的条件删掉(= 回到无条件覆盖)在 pkgf 全分母下**零红**。
    """
    from db.monitoring_db import claim_monitoring_run_cell, save_monitoring_result
    from services.engine_contract import PLATFORM_CONTRACT

    planned_model = PLATFORM_CONTRACT["dashscope"]["model"]
    drifted = "qwen3.7-plus-hotfix-b"
    assert drifted != planned_model, "构造失败:实际值必须与计划值不同"

    c = _seed_success_cell(cur, task_id=task, keyword_id=36)
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")

    save_monitoring_result(
        task_id=task, keyword_id=36, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=False, mention_type="none",
        full_response="没提到。",
        # 🔴 与上一条**只差这一个键**:模型名一模一样,但没有声明来源。
        lineage=_success_lineage(model=drifted, model_source=""),
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["actual_model"] == planned_model, (
        f"没声明来源的模型值 {drifted!r} 被写成了 observed(实得 "
        f"{row['actual_model']!r})—— 那等于用一个**计划**值去「确认」实际值,"
        f"而这一列同时是保真度对账与计价的取数口")
    assert row["actual_model_source"] == "planned_fallback", (
        f"来源列记成了 {row['actual_model_source']!r} —— 没有声明就不是回显,"
        f"保守缺省不许被「给了 model」这件事推翻")


def test_cost_row_records_the_called_model(cur, live_db, task):
    """[R2 · ⑥.2] 计价那一行真的写进去了,而且用的是**真被调的那个模型**。

    🔴 为什么这条要在真链上判:⑥.2 声称"记账错位随换代自然闭合",R1 给它
       配的只有一条 AST 锁(源码里 ``_QWEN_MODEL`` 在不在)。而那段 INSERT
       外面裹着 ``SAVEPOINT sp_token_usage`` + ``except`` ——
       表不存在也好、列错了也好、算价抛了也好,**一律静默吞掉**。
       换句话说:AST 锁绿着,而这行 INSERT 可以从来没成功过一次
       (R1 的夹具里就没建这张表,所以它确实一次没成功过)。

    这条判据只判两件事:行**在**,且算出来的钱与"真被调的模型"同源。
    不钉具体金额 —— 单价是可调系数,钉死会变成"改价必红"的假判据。
    """
    from db.monitoring_db import claim_monitoring_run_cell, save_monitoring_result
    from services.engine_contract import QWEN_ENGINE
    from tools.llm_call_tracker import estimate_cost

    c = _seed_success_cell(cur, task_id=task, keyword_id=34)
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    body = "回答正文。" * 40
    save_monitoring_result(
        task_id=task, keyword_id=34, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=True, mention_type="direct",
        full_response=body, lineage=_success_lineage(),
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )

    cur.connection.commit()
    cur.execute("SELECT * FROM monitoring_token_usage WHERE task_id=%s", (task,))
    rows = [dict(r) for r in cur.fetchall()]
    assert len(rows) == 1, (
        f"计价行没落库(SAVEPOINT 把失败吞了,外面看不出来):{rows}")
    row = rows[0]
    assert row["platform"] == "dashscope", row
    assert row["output_tokens"] == max(1, len(body) // 3), row

    # 🔴 承重:钱是按**被测引擎 SSOT 里那个模型**算的。
    #    换代前这里写死 qwen3.7-plus 而实际发 qwen3-max —— 按一个模型收钱、
    #    用另一个模型干活。现在两边同源,这条判据钉的就是"同源"。
    expected = round(estimate_cost("dashscope", QWEN_ENGINE["model"],
                                   row["input_tokens"], row["output_tokens"]), 4)
    assert abs(float(row["estimated_cost"]) - expected) < 1e-6, (
        f"算价用的模型与真被调的模型对不上:落库 {row['estimated_cost']},"
        f"按 SSOT 模型 {QWEN_ENGINE['model']} 应为 {expected}")
    # 反向:这条判据必须有区分力 —— 换个模型算出来的钱必须不一样,
    # 否则上面那句在"价格表对所有模型同价"时会恒真。
    other = round(estimate_cost("dashscope", "qwen3-max",
                                row["input_tokens"], row["output_tokens"]), 4)
    if other == expected:
        pytest.skip("qwen3-max 与 qwen3.7-plus 现价相同 —— 本条此刻零区分力,"
                    "如实记'没验'而不是假装验过了")


def test_pending_identity_closes_as_entity_ambiguous_through_the_live_chain(
        cur, live_db, task):
    """[R2 F-1] 成功路径的**另一臂**:身份待定 ⇒ entity_ambiguous。

    与上面成对:只判 answered 那一臂的话,一个"无脑写 answered"的实现
    照样全绿 —— 而那会让"AI 说的可能不是你"在客户卡上直接消失(§6.3)。
    """
    from db.monitoring_db import claim_monitoring_run_cell, save_monitoring_result

    c = _seed_success_cell(cur, task_id=task, keyword_id=33)
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")

    result_id = save_monitoring_result(
        task_id=task, keyword_id=33, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=True, mention_type="direct",
        full_response="提到了一个同名的别家。", lineage=_success_lineage(),
        identity_brand_id=TASK_BRAND,
        identity_candidates=[_LINEAGE_BRAND],
        identity_evidence_snippet="同名歧义",
        identity_review_state="pending",
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["terminal_state"] == "entity_ambiguous", (
        f"身份待定被收成了 {row['terminal_state']} —— 抬成 answered 会让"
        f"'AI 说的可能不是你'在卡上消失,压成 not_mentioned 违反 §6.3:{row}")
    assert row["monitoring_result_id"] == result_id, row


def _claim_token(cur, cell_id) -> str:
    cur.connection.commit()
    cur.execute("SELECT claim_token FROM monitoring_run_cells WHERE id=%s",
                (int(cell_id),))
    return str(cur.fetchone()["claim_token"])


# ══════════════════════════════════════════════════════════════════════
# 逐点行为
# ══════════════════════════════════════════════════════════════════════

def test_error_is_engine_error_never_absent(cur, live_db, task):
    """MON-02:平台故障**不许**被压成"未提及"。

    这不是枚举拼写检查 —— 它拦的是让客户看到一份"品牌提及率下降"的报告,
    而真相是我们没拿到回答。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  finish_monitoring_cell_error)

    c = _seed_cell(cur, task_id=task, keyword_id=7, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    finish_monitoring_cell_error(
        cell_id=int(c["id"]), claim_token=_claim_token(cur, c["id"]),
        state="pending_provider_confirmation",
        error_code="provider_outcome_unknown", error_message="x")

    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 1, rows
    assert rows[0]["terminal_state"] == "engine_error", rows[0]
    assert rows[0]["error_code"] == "provider_outcome_unknown", rows[0]
    assert rows[0]["terminal_state"] not in LEDGER.FORBIDDEN_ERROR_MAPPINGS


def test_retry_appends_a_child_attempt_and_never_overwrites(cur, live_db, task):
    """MON-03:重试建 child attempt,**不覆盖**原始失败。

    这条判据的价值在于第一次失败的 ``error_code`` 在重试之后**还在** ——
    现役 cell 那一行早被 ``SET error_code=NULL`` 抹掉了。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  finish_monitoring_cell_error)

    c = _seed_cell(cur, task_id=task, keyword_id=8, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    finish_monitoring_cell_error(
        cell_id=int(c["id"]), claim_token=_claim_token(cur, c["id"]),
        state="failed", error_code="FIRST_FAILURE", error_message="one")

    # 第二次:再 claim(现役重试链的等价动作 —— 同一格再开一次)
    cur.execute("UPDATE monitoring_run_cells SET state='queued', "
                "error_code=NULL, error_message=NULL WHERE id=%s", (int(c["id"]),))
    cur.connection.commit()
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    finish_monitoring_cell_error(
        cell_id=int(c["id"]), claim_token=_claim_token(cur, c["id"]),
        state="failed", error_code="SECOND_FAILURE", error_message="two")

    rows = _attempts(cur, c["plan_hash"])
    assert [r["attempt_ordinal"] for r in rows] == [1, 2], rows
    assert rows[0]["error_code"] == "FIRST_FAILURE", (
        "第一次失败被覆盖了 —— MON-03 的整个理由就是它不许被覆盖")
    assert rows[1]["error_code"] == "SECOND_FAILURE", rows
    assert rows[1]["parent_attempt_id"] == rows[0]["attempt_id"], (
        "child attempt 没挂到父 attempt 上")
    # 现役 cell 上那条信息确实已经没了 —— 这正是账本存在的理由
    cur.execute("SELECT error_code FROM monitoring_run_cells WHERE id=%s",
                (int(c["id"]),))
    assert cur.fetchone()["error_code"] == "SECOND_FAILURE"


def test_terminal_row_is_immutable_at_the_db_level(cur, live_db, task):
    """终态行不可改写 —— 承重点在**库**,不是应用层自觉。

    应用层那条 ``WHERE terminal_state IS NULL`` 是纵深第一层;
    这里单独拆开验第二层(触发器),因为「相关判据全绿」证明不了
    某一层真的在守(本仓记过:两把锁叠在同一条路径上)。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  finish_monitoring_cell_error)
    import psycopg2

    c = _seed_cell(cur, task_id=task, keyword_id=9, platform="kimi")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    finish_monitoring_cell_error(
        cell_id=int(c["id"]), claim_token=_claim_token(cur, c["id"]),
        state="failed", error_code="LOCKED_IN", error_message="x")

    rows = _attempts(cur, c["plan_hash"])
    with pytest.raises(psycopg2.Error) as exc:
        cur.execute(
            f"UPDATE {LEDGER.TABLE} SET error_code='TAMPERED' WHERE attempt_id=%s",
            (rows[0]["attempt_id"],))
    assert exc.value, "终态行被改写成功了 —— 库层触发器没在守"


def test_policy_skip_counts_terminal_but_not_attempted(cur, live_db, task, client):
    """MON-11 逐字:skipped 无 provider attempt、**计 terminal 不计 attempted**。

    并且它**不进**进度端点的分母(那条链按 is_planned=TRUE 取格)——
    两个分母不同是有意的,所以这里同时验两侧。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        record_skip_for_plan,
    )

    planned = _seed_cell(cur, task_id=task, keyword_id=11, platform="dashscope")
    skipped = _seed_cell(cur, task_id=task, keyword_id=11, platform="doubao",
                         is_planned=False, state="unavailable")

    assert record_skip_for_plan(cur, skipped), "未计划的格没落 policy_skip"
    # 反向:已计划的格**不许**落 skip
    assert record_skip_for_plan(cur, planned) is None, (
        "已计划的格被记成了 policy_skipped —— 那会让一格真要跑的活凭空消失")

    rows = _attempts(cur, skipped["plan_hash"])
    assert len(rows) == 1 and rows[0]["terminal_state"] == "policy_skipped"
    assert rows[0]["provider_called"] is False, (
        "policy_skipped 带了 provider 调用 —— attemptRecords 会虚高")

    facts = LEDGER.cell_denominator_facts(
        LEDGER.attempts_for_cell(cur, plan_cell_id=skipped["plan_hash"]))
    assert facts["isTerminal"] is True, facts
    assert facts["isAttempted"] is False, (
        f"skipped 被算进 attempted —— MON-11 逐字禁止:{facts}")

    # 进度端点分母:只数 planned 的那一格
    p = _progress(client, task)
    assert p["plannedCells"] == 1, (
        f"未购平台进了进度分母 —— 进度会被系统性低报:{p}")


def test_human_identity_resolution_appends_answered(cur, live_db, task):
    """接线点⑤:人工确认 pending_identity 之后,卡上必须变成"认出来了"。

    不接这一跳的后果是**真实的数据错误**:客户已经人工确认过"这就是我家",
    而五卡上永远写着"身份待确认"。
    """
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        close_for_human_resolution, close_for_result, open_for_claim,
    )

    c = _seed_cell(cur, task_id=task, keyword_id=12, platform="dashscope")
    open_for_claim(cur, c)
    close_for_result(cur, plan_cell_id=c["plan_hash"],
                     cell_state="pending_identity", monitoring_result_id=555)

    before = LEDGER.canonical_attempt(
        LEDGER.attempts_for_cell(cur, plan_cell_id=c["plan_hash"]))
    assert before.terminal_state == "entity_ambiguous", before

    close_for_human_resolution(cur, plan_cell_id=c["plan_hash"],
                               monitoring_result_id=555, actor_user_id=USER,
                               brand_id=TASK_BRAND, run_authority_id=str(task),
                               # [工单 E3-1] 租户从格上冻结的那一列来,
                               # 不再借用 actor_user_id(那是操作者不是租户)。
                               tenant_owner_user_id=c["tenant_owner_user_id"])

    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 2, f"人工确认没追加 attempt:{rows}"
    assert rows[0]["terminal_state"] == "entity_ambiguous", (
        "原始那条身份待定的观测被改写了 —— MON-03 禁止")
    after = LEDGER.canonical_attempt(
        LEDGER.attempts_for_cell(cur, plan_cell_id=c["plan_hash"]))
    assert after.terminal_state == "answered", (
        f"人工确认之后 canonical 还不是 answered:{after}")
    assert after.provider_called is False, (
        "人工确认那一跳被记成了 provider 调用 —— attemptRecords 会虚高一次")


def test_crashed_inflight_attempt_does_not_block_the_cell_forever(cur, live_db, task):
    """🔴 一期审计点名的那个雷:崩在 open 与 close 之间。

    ``uq_defgeo_attempt_single_inflight`` 是 partial unique ——
    那条在飞行会**永久**占位,该格从此再也 open 不进第二个 attempt。
    现象是"重试永远不进账本",很难从症状反推。
    """
    from db.monitoring_db import claim_monitoring_run_cell

    c = _seed_cell(cur, task_id=task, keyword_id=13, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 1 and rows[0]["terminal_state"] is None, (
        f"第一次 claim 没留下在飞 attempt:{rows}")

    # 模拟崩溃:进程死了,在飞行原样留着;运维/调度把格放回 queued
    cur.execute("UPDATE monitoring_run_cells SET state='queued', "
                "claim_token=NULL, claim_until=NULL WHERE id=%s", (int(c["id"]),))
    cur.connection.commit()

    # 第二次 claim 必须能开进第二个 attempt
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 2, (
        f"崩溃后第二次 claim 没能开进新 attempt —— 在飞行永久占位了:{rows}")
    assert rows[0]["terminal_state"] == "engine_error", (
        f"旧在飞行没被收口:{rows[0]}")
    # 🔴 两个收口码都合法,而且**哪一个先到**是有意义的信息:
    #    · worker_lost_before_dispatch / provider_outcome_unknown ⇒
    #      现役自己的崩溃恢复 chokepoint(_recover_expired_monitoring_cells)
    #      先到了 —— 这是更好的结果,因为账本用的是现役自己的词;
    #    · attempt_superseded_by_new_claim ⇒ open_for_claim 里那道
    #      逻辑必然的收口兜住了它。
    #    判据接受两者、但**不接受第三种**:一个不在收口码表里的值意味着
    #    有人在别处收了这一行,而没人复核过。
    from services.defensive_geo.monitoring import run_ledger_bridge as _RB
    assert rows[0]["error_code"] in (
        _RB.RECLAIM_CODE_SUPERSEDED, _RB.RECLAIM_CODE_NOT_DISPATCHED,
        _RB.RECLAIM_CODE_DISPATCHED), rows[0]
    assert rows[1]["terminal_state"] is None, "新 attempt 应当在飞"


# ── legacy backfill 分叉:两臂各一发 ────────────────────────────────
def test_backfill_adds_nothing_when_the_cell_is_already_ledgered(cur, live_db, task):
    """臂 A:该格已有 attempt 行 ⇒ 一期那条抢救**零新增**(不许双算)。

    🔴 这条判据欠了很久才补上:MUT-F10(把 backfill 守卫摘掉)在第一轮
       **存活**了 —— 因为当时唯一相关的判据走的是 ``claim`` 两次,
       根本没经过 ``capture_before_retry_overwrite``。
       我改了 legacy_bridge 的行为却没在**行为真正分岔的那一格**上留判据,
       正是本仓记过的「顺手多修一处 = 顺手多欠一条判据」。

    为什么必须零新增:包F ① 之后 ``finish_monitoring_cell_error`` 已经在
    失败**发生的那一刻**把它落成 engine_error attempt 了。抢救再插一条,
    同一次真实调用在账本里就有两行 ⇒ ``attemptRecords`` 比真实调用数多一,
    而 MON-11 逐字要求两者一致。多算的那一次会让 progressPct 与
    五卡的 attempt 守恒式**同时**失真。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  finish_monitoring_cell_error)
    from services.defensive_geo.monitoring.legacy_bridge import (
        capture_before_retry_overwrite,
    )

    c = _seed_cell(cur, task_id=task, keyword_id=21, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    finish_monitoring_cell_error(
        cell_id=int(c["id"]), claim_token=_claim_token(cur, c["id"]),
        state="failed", error_code="ALREADY_LEDGERED", error_message="x")

    before = _attempts(cur, c["plan_hash"])
    assert len(before) == 1, f"前提不成立(执行链没落账):{before}"

    cur.execute("SELECT * FROM monitoring_run_cells WHERE id=%s", (int(c["id"]),))
    row = dict(cur.fetchone())
    wrote = capture_before_retry_overwrite(cur, row)

    after = _attempts(cur, c["plan_hash"])
    assert wrote is False, "已落账的格又被抢救了一次 —— 双算"
    assert len(after) == len(before), (
        f"attempt 行数从 {len(before)} 变成 {len(after)} —— 同一次真实调用双算了")


def test_backfill_still_rescues_a_pre_wiring_cell(cur, live_db, task):
    """臂 B:该格**零** attempt 行(上线前的存量)⇒ 恰新增一行。

    不能因为怕双算就把这一跳删掉:上线前就已经 failed 的存量格在账本里
    一行都没有,它们的重试仍然需要这条抢救,否则那次失败**永久丢失**
    (现役 cell 那一行的 error_code 马上会被 ``SET error_code=NULL`` 抹掉)。

    两臂合起来才说明"守卫装对了方向";只有臂 A 的话,把整跳删掉也全绿。
    """
    from services.defensive_geo.monitoring.legacy_bridge import (
        capture_before_retry_overwrite,
    )

    # 存量格:直接置成 failed + 带 error_code,**不**经过执行链 ⇒ 账本零行
    c = _seed_cell(cur, task_id=task, keyword_id=22, platform="kimi",
                   state="queued")
    cur.execute(
        "UPDATE monitoring_run_cells SET state='failed', error_code=%s, "
        "error_message=%s, completed_at=NOW() WHERE id=%s",
        ("PRE_WIRING_FAILURE", "上线前就失败的存量格", int(c["id"])))
    cur.connection.commit()
    assert _attempts(cur, c["plan_hash"]) == [], "前提不成立:这一格应当零 attempt"

    cur.execute("SELECT * FROM monitoring_run_cells WHERE id=%s", (int(c["id"]),))
    row = dict(cur.fetchone())
    wrote = capture_before_retry_overwrite(cur, row)

    after = _attempts(cur, c["plan_hash"])
    assert wrote is True, "存量格的历史失败没被抢救 —— 那条信息将永久丢失"
    assert len(after) == 1, f"应当恰新增一行,实得 {len(after)}"
    assert after[0]["terminal_state"] == "engine_error", after[0]
    assert after[0]["error_code"] == "PRE_WIRING_FAILURE", (
        f"抢救下来的不是那条历史失败:{after[0]}")


def test_ledger_sql_failure_never_blocks_the_live_chain(cur, live_db, task,
                                                       monkeypatch):
    """🔴 fail-soft 第一层(**事务完整性** / SAVEPOINT)的真链证明。

    一期那句承诺是**反的** —— 裸 try/except 包 SQL 会让整个事务 aborted,
    于是 ``reserve_monitoring_cell_retry`` 后面的 INSERT + UPDATE 全部失败,
    用户的重试被一条**观测**账本挡下来了。

    注毒选在**账本自己的 SQL** 上,而且是一个**真实**的失败形态:
    让 ordinal 变成 0 ⇒ 撞 ``chk_defgeo_attempt_ordinal_positive`` ⇒
    INSERT 在 ``guarded`` 的 SAVEPOINT 里报错。
    断言两件事:claim 返回 running,**且** cell 状态真的落了库
    (后者才是"事务没被打废"的证据 —— 只看返回值会漏掉回滚)。
    """
    from db.monitoring_db import claim_monitoring_run_cell
    from services.defensive_geo.monitoring import attempt_ledger as AL

    c = _seed_cell(cur, task_id=task, keyword_id=14, platform="dashscope")
    monkeypatch.setattr(AL, "next_ordinal", lambda *a, **k: 0)

    claimed = claim_monitoring_run_cell(
        cell_id=int(c["id"]), task_id=task, brand_id=TASK_BRAND,
        allowed_state="queued")
    assert claimed["state"] == "running", (
        "账本写崩把用户的 claim 一起带下去了 —— fail-soft 是假的")

    cur.connection.commit()
    cur.execute("SELECT state FROM monitoring_run_cells WHERE id=%s",
                (int(c["id"]),))
    assert cur.fetchone()["state"] == "running", (
        "cell 状态没落库 —— 说明那个事务被账本打废后整体回滚了。"
        "这正是一期裸 try/except 的形态。")
    # 账本那一行确实没写进去(证明毒真的生效了,不是"毒没起作用所以全绿")
    assert _attempts(cur, c["plan_hash"]) == [], (
        "毒没生效 —— ordinal=0 竟然写进去了,那么这条判据什么都没证明")


def test_ledger_python_failure_never_blocks_the_live_chain(cur, live_db, task,
                                                          monkeypatch):
    """🔴 fail-soft 第二层(**控制流** / never_raises)的真链证明。

    与上一条是**纵深**不是重复,所以断言不同:

    · SAVEPOINT 护的是事务 —— 上一条断言 cell **落了库**;
    · ``never_raises`` 护的是控制流 —— 本条注的毒是一条**绕过** SAVEPOINT 的
      异常(整个函数被换掉),此时事务确实会被那条裸 SQL 打废、
      claim 的 commit 退化成回滚,这是 PostgreSQL 的语义,**没法也不该**
      在应用层"修好";本条要证明的是**异常不逃出去**——
      ``claim_monitoring_run_cell`` 不抛,用户拿到的不是一个 500。

    🔴 这个区分很重要:如果这里也断言"cell 落了库",判据会要求一个
       数据库层面做不到的事,然后逼下一个人去写一个假的 workaround。
    """
    from db.monitoring_db import claim_monitoring_run_cell
    from services.defensive_geo.monitoring import run_ledger_bridge as RB

    c = _seed_cell(cur, task_id=task, keyword_id=15, platform="kimi")

    def _poison(cur_, **kwargs):        # noqa: ANN001
        cur_.execute("SELECT 1/0")      # 绕过 guarded 的裸 SQL
        return 0

    monkeypatch.setattr(RB, "reclaim_superseded", _poison)

    # 唯一断言:**不抛**。抛出去的话现役监测会以 500 结束。
    try:
        claim_monitoring_run_cell(
            cell_id=int(c["id"]), task_id=task, brand_id=TASK_BRAND,
            allowed_state="queued")
    except Exception as exc:      # noqa: BLE001
        pytest.fail(f"账本的 Python 异常逃到了现役链上:{exc!r}")


def test_the_two_poisons_really_poison(cur, live_db, task):
    """两条注毒的判别力自证:它们**真的**会炸。

    没有这一条,``_poison`` 写错(比如 ``SELECT 1``)、或 ordinal=0 恰好合法时,
    上面两条恒绿 —— 而它们本来要证明的是"炸了也不阻断"。
    本仓记过:夹具没跑起来的红不算抓到毒。
    """
    import psycopg2

    # 毒一:ordinal=0 必须被 CHECK 拦住
    ph = _plan_hash("poison-probe")
    with pytest.raises(psycopg2.Error):
        cur.execute(
            f"INSERT INTO {LEDGER.TABLE} (attempt_id, plan_cell_id, attempt_ordinal,"
            " run_authority_id, tenant_owner_user_id, brand_id, actual_provider,"
            " actual_model, actual_surface, actual_search_mode, request_hash,"
            " ledger_version) VALUES (%s,%s,0,'1',1,1,'p','m','s','sm','h','v')",
            ("0" * 64, ph))
    cur.connection.rollback()

    # 毒二:裸 1/0 必须炸
    with pytest.raises(psycopg2.Error):
        cur.execute("SELECT 1/0")
    cur.connection.rollback()


def test_the_poison_probe_really_poisons(cur, live_db, task):
    """上一条的判别力自证:那条注毒语句**真的**会炸。

    没有这一条,``_poison`` 写错(比如 ``SELECT 1``)时上面那条恒绿 ——
    而它本来要证明的是"炸了也不阻断"。本仓记过:夹具没跑起来的红不算抓到毒。
    """
    import psycopg2
    with pytest.raises(psycopg2.Error):
        cur.execute("SELECT 1/0")


# ══════════════════════════════════════════════════════════════════════
# [R2 F-1] 三条"注释里引用过、仓里却不存在"的判据 —— 补齐
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 这三个名字在 R1 的代码注释里被当成"唯一判据"引用,而全仓一条都没有
#    (Review census 实扫)。这是 claimed-artifact-never-written:
#    注释在替一条不存在的判据背书,读代码的人会以为那一行被守着。
#    补齐比改注释好 —— 前两条本来就是行为缺口,第三条是假绿保险丝。


def test_savepoint_guard_is_actually_active_in_this_fixture(cur):
    """🔴 保险丝:本夹具里 ``guarded`` 的 SAVEPOINT 真的能用。

    这是本包**最贵**的那个坑的自证。``cur`` 一旦回到 autocommit=True,
    PostgreSQL 会拒绝 ``SAVEPOINT``(``can only be used in transaction
    blocks``),于是 ``guarded`` 第一步就失败返回 None、
    ``ledger_is_available`` 恒 False、**每一个桥函数变成空转** ——
    而所有接线判据会安静地全绿,因为它们断言的东西一行也没被写过。

    同形教训:「psql 无 BEGIN 时只读护栏静默不存在」。
    没有这条自证,整片真链判据都可能在测空气。
    """
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    # ① 连接确实是事务模式(不是 autocommit)
    assert cur.connection.autocommit is False, (
        "cur 回到 autocommit 了 —— SAVEPOINT 会静默失效,"
        "整片接线判据将在测空气")

    # ② guarded 真的能跑通并把返回值透出来
    sentinel = object()
    assert BRIDGE.guarded(cur, "自证", lambda: sentinel) is sentinel, (
        "guarded 正常路径返回不了值 —— SAVEPOINT 没生效")

    # ③ guarded 吞掉 SQL 错误之后,**事务还能继续用**
    #    (这正是 SAVEPOINT 的意义:裸 try/except 会让事务进入 aborted,
    #     后面每一条语句都 InFailedSqlTransaction)
    def _boom():
        cur.execute("SELECT 1/0")

    assert BRIDGE.guarded(cur, "注毒", _boom) is None
    cur.execute("SELECT 42 AS still_alive")
    assert cur.fetchone()["still_alive"] == 42, (
        "guarded 吞了错但事务已经废了 —— 那不是 fail-soft,"
        "是把调用方的事务打死了")

    # ④ 账本可用性在本夹具里为真(前三条都过了才有意义)
    assert BRIDGE.ledger_is_available(cur) is True


def test_planned_model_tracks_the_platform_contract(cur, live_db, task):
    """派发前记的模型 = 平台合同表里的值,**不是抄的一份**。

    反向锁:``_planned_lineage`` 里但凡把模型名手写成字面量,合同表一改
    这条就红。R1 注释说"判据反向锁住(抄一份就会红)"—— 那条判据当时
    不存在,现在存在了。

    两段一起判:
      ① 真链上开出来的 attempt,四个 lineage 位逐值 = 合同表;
      ② 源码里 ``_planned_lineage`` 不许出现任何模型名字面量。
    """
    from db.monitoring_db import claim_monitoring_run_cell
    from services.engine_contract import PLATFORM_CONTRACT

    for platform in ("dashscope", "kimi"):
        c = _seed_cell(cur, task_id=task, keyword_id=41, platform=platform)
        claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                                  brand_id=TASK_BRAND, allowed_state="queued")
        row = _attempts(cur, c["plan_hash"])[0]
        contract = PLATFORM_CONTRACT[platform]
        assert row["actual_provider"] == contract["provider"], (platform, row)
        assert row["actual_model"] == contract["model"], (
            f"{platform} 派发前记的模型 {row['actual_model']} != 合同 "
            f"{contract['model']} —— 账本记的和真发的不是一回事")
        assert row["actual_surface"] == contract["surface"], (platform, row)
        # search_mode 来自 cell 的 entitlement_snapshot,不来自合同表
        assert row["actual_search_mode"] == "enhanced", row

    # ② 结构:不许在 _planned_lineage 里手写模型名
    import ast
    import io
    from pathlib import Path
    src = io.open(
        Path(__file__).resolve().parents[2]
        / "services/defensive_geo/monitoring/run_ledger_bridge.py",
        encoding="utf-8", newline="").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_planned_lineage")
    known_models = {v["model"] for v in PLATFORM_CONTRACT.values()}
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            assert n.value not in known_models, (
                f"_planned_lineage 里手写了模型名 {n.value!r}(第 {n.lineno} 行)"
                f" —— 合同表一改,账本就记着旧名字,而没有判据会红")


def test_reclaim_prefers_cell_state_over_timeout(cur, live_db, task):
    """收敛优先级:cell 状态是**权威**,租约超时只是回落。

    R1 注释说这条判据"打这个优先级:把 ① 摘掉只剩 ② 时,一条 cell 早已
    failed 但只过了 1 秒的在飞 attempt 收不掉 ⇒ 红"。判据当时不存在。

    这里构造的正是那个场景:
      · cell 已经是 ``failed``(现役恢复已把它终态化);
      · attempt 才刚建出来,**远没到**租约超时。
    只靠超时的实现收不掉它 —— 而收不掉的后果是这一格的在飞行占位
    一直挂着,唯一索引拦住下一次 open,这一格再也跑不了。
    """
    from db.monitoring_db import claim_monitoring_run_cell
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        ATTEMPT_LEASE_SECONDS, reclaim_orphans,
    )

    c = _seed_cell(cur, task_id=task, keyword_id=42, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")

    inflight = _attempts(cur, c["plan_hash"])[0]
    assert inflight["terminal_state"] is None, "构造失败:这条应该还在飞"

    # 现役恢复把 cell 终态化(provider 已派发过 ⇒ 结果未知)
    cur.execute(
        "UPDATE monitoring_run_cells SET state='failed', "
        "provider_dispatched_at=NOW() WHERE id=%s", (int(c["id"]),))
    cur.connection.commit()

    # 🔴 关键:这条 attempt **远没到**租约超时。
    cur.execute(
        f"SELECT NOW() - created_at < (%s || ' seconds')::interval AS fresh "
        f"FROM {LEDGER.TABLE} WHERE attempt_id=%s",
        (ATTEMPT_LEASE_SECONDS, inflight["attempt_id"]))
    assert cur.fetchone()["fresh"] is True, (
        "构造失败:attempt 已经超租约了,那这条判据就分不出①和②")

    n = reclaim_orphans(cur, monitoring_cell_ids=[int(c["id"])])
    assert n == 1, (
        f"cell 已 failed 却没被收掉(收了 {n} 条)—— 说明只看了租约超时。"
        f"后果:在飞行占位永久挂着,唯一索引拦住下一次 open,这一格再也跑不了")

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["terminal_state"] == "engine_error", row
    # 权威分支还要给对 error_code:派发过 ⇒ 结果未知,不是"没派发"也不是"超时"
    assert row["error_code"] == "provider_outcome_unknown", (
        f"error_code 是 {row['error_code']} —— 派发过的那一格必须落"
        f"provider_outcome_unknown,落成 attempt_lease_expired 说明走的是回落分支")


def test_reclaim_falls_back_to_the_lease_only_when_no_cell_row_exists(
        cur, live_db, task):
    """上一条的**另一臂**:没有 cell 行可查时,才轮到租约超时说话。

    成对判。只判权威分支的话,一个"干脆删掉回落分支"的实现照样全绿 ——
    而那批"cell 行已被清理"的在飞 attempt 会永远挂着,没人收得掉。
    """
    from db.monitoring_db import claim_monitoring_run_cell
    from services.defensive_geo.monitoring.run_ledger_bridge import (
        ATTEMPT_LEASE_SECONDS, reclaim_orphans,
    )

    c = _seed_cell(cur, task_id=task, keyword_id=43, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    plan_hash = c["plan_hash"]
    cell_id = int(c["id"])

    # cell 行消失(被清理),attempt 还在飞
    cur.execute("DELETE FROM monitoring_run_cells WHERE id=%s", (cell_id,))
    cur.connection.commit()

    # ① 还没到租约:不许收 —— 现役可能真的还在跑
    assert reclaim_orphans(cur, plan_cell_ids=[plan_hash]) == 0, (
        "cell 行查不到就急着收口 —— 会在现役还在跑的时候先把账本收了")

    # ② 把 created_at 拨到租约之外:该收了
    cur.execute(
        f"UPDATE {LEDGER.TABLE} SET created_at = NOW() - (%s || ' seconds')::interval "
        f"WHERE plan_cell_id=%s", (ATTEMPT_LEASE_SECONDS + 60, plan_hash))
    cur.connection.commit()
    assert reclaim_orphans(cur, plan_cell_ids=[plan_hash]) == 1, (
        "超了租约又没有 cell 行可查,却还是收不掉 —— 回落分支没了,"
        "这批 attempt 会永久挂着")

    row = _attempts(cur, plan_hash)[0]
    assert row["error_code"] == "attempt_lease_expired", (
        f"回落分支必须落 attempt_lease_expired,实际 {row['error_code']}")


# ══════════════════════════════════════════════════════════════════════
# [R2 F-3] 消费方全集:三条绕过账本把 cell 打到终态的路径
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 五点接线覆盖的是 claim/收口**主链**。Review census 实扫出三条现役
#    可达、却绕过账本的路径。前两条挂在**每小时无条件跑**的 cron 上
#    (api/scheduler.py freeze_sweeper_hourly → services/freeze_sweeper.py),
#    所以不是理论风险,是每天都在发生。
#
#    后果不是"报错",是**账本永远说这一格还在跑**:cell 早已终态,
#    在飞 attempt 行悬挂着。五卡的分母据此算,MON-11 的守恒也就永远
#    对不上,而没有任何东西会报警。


def _inflight_cell(cur, task, *, keyword_id: int, dispatched: bool):
    """建一格并真 claim 成 running,账本里留一条**在飞** attempt。"""
    from db.monitoring_db import claim_monitoring_run_cell

    c = _seed_cell(cur, task_id=task, keyword_id=keyword_id, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    assert _attempts(cur, c["plan_hash"])[0]["terminal_state"] is None
    if dispatched:
        cur.execute(
            "UPDATE monitoring_run_cells SET provider_dispatched_at=NOW() "
            "WHERE id=%s", (int(c["id"]),))
        cur.connection.commit()
    return c


def test_sweeper_reaping_an_abandoned_task_also_closes_the_ledger(
        cur, live_db, task):
    """[R2 F-3a] ``recover_abandoned_monitoring_task_execution`` 收割后账本收口。

    这条路径每小时无条件跑。R1 没接它 ⇒ 被 reap 掉的每一格都在账本里
    留一条永久悬挂的在飞行。
    """
    from db.monitoring_db import recover_abandoned_monitoring_task_execution

    c = _inflight_cell(cur, task, keyword_id=51, dispatched=True)
    # 租约过期 —— 这是 sweeper 认定"这一格被抛弃了"的条件
    cur.execute(
        "UPDATE monitoring_run_cells SET claim_until=NOW() - interval '1 hour' "
        "WHERE id=%s", (int(c["id"]),))
    cur.connection.commit()

    changed = recover_abandoned_monitoring_task_execution(task)
    assert changed == 1, f"sweeper 没收到这一格(改了 {changed} 行)"

    cur.execute("SELECT state FROM monitoring_run_cells WHERE id=%s",
                (int(c["id"]),))
    assert cur.fetchone()["state"] == "pending_provider_confirmation"

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["terminal_state"] == "engine_error", (
        f"cell 被 sweeper 收割了,账本却还说在飞:{row} —— "
        f"这条在飞行会永久悬挂,五卡分母从此长期偏")
    assert row["error_code"] == "provider_outcome_unknown", (
        f"派发过的那一格必须落 provider_outcome_unknown,实际 {row['error_code']}")


def test_sweeper_reaping_an_undispatched_cell_says_so(cur, live_db, task):
    """[R2 F-3a 另一臂] 没派发过的格,收口理由必须是"发出去之前就丢了"。

    成对判:只判 dispatched 那一臂的话,一个"一律写 provider_outcome_unknown"
    的实现照样绿 —— 而那会把"根本没花钱"说成"钱花了但结果未知",
    直接影响这一格能不能安全重试。
    """
    from db.monitoring_db import recover_abandoned_monitoring_task_execution

    c = _inflight_cell(cur, task, keyword_id=52, dispatched=False)
    cur.execute(
        "UPDATE monitoring_run_cells SET claim_until=NOW() - interval '1 hour' "
        "WHERE id=%s", (int(c["id"]),))
    cur.connection.commit()

    assert recover_abandoned_monitoring_task_execution(task) == 1
    cur.execute("SELECT state FROM monitoring_run_cells WHERE id=%s",
                (int(c["id"]),))
    assert cur.fetchone()["state"] == "failed"

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["terminal_state"] == "engine_error", row
    assert row["error_code"] == "worker_lost_before_dispatch", (
        f"没派发过却记成 {row['error_code']} —— 会把'没花钱'说成'钱花了'")


def test_refund_release_also_closes_the_ledger(cur, live_db, task):
    """[R2 F-3b] ``revoke_monitoring_task_coverage_for_organization_refund`` 同理。

    同一个 cron 链(services/freeze_sweeper.py:207)。
    """
    from db.monitoring_db import (
        revoke_monitoring_task_coverage_for_organization_refund,
    )

    c = _inflight_cell(cur, task, keyword_id=53, dispatched=True)
    ref = f"settle-{task}-53"
    cur.execute(
        "UPDATE monitoring_run_cells SET settlement_reference=%s, "
        "fulfillment_state='reserved' WHERE id=%s", (ref, int(c["id"])))
    cur.execute("INSERT INTO point_freezes (task_ref) VALUES (%s) RETURNING id",
                (ref,))
    freeze_id = int(cur.fetchone()["id"])
    cur.execute(
        "INSERT INTO organization_charge_links "
        "(status, physical_backend, physical_freeze_id) "
        "VALUES ('refunded','legacy_user_wallet',%s) RETURNING id",
        (str(freeze_id),))
    charge_id = int(cur.fetchone()["id"])
    cur.connection.commit()

    changed = revoke_monitoring_task_coverage_for_organization_refund(
        task, charge_id)
    assert changed == 1, f"退款释放没作用到这一格(改了 {changed} 行)"

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["terminal_state"] == "engine_error", (
        f"退款把格停了,账本却还说在飞:{row}")
    assert row["error_code"] == "provider_outcome_unknown", row


def test_every_producer_of_pending_provider_confirmation_closes_the_ledger(cur):
    """[R2 F-3c] admin 复核那条路径**良性,而且是可证的良性**。

    ``decide_monitoring_provider_review`` 只接受
    ``state='pending_provider_confirmation'`` 的格。所以它是否需要接线,
    取决于一个可枚举的问题:**能把格送进这个状态的路径有哪几条,
    它们是不是都已经收了账本**。

    🔴 分母不是我手数出来的 —— 是从源码里机械枚举,而且**两种形态都扫**:
       ① 字面量:SQL 里直接 ``UPDATE ... SET state='pending_provider_confirmation'``;
       ② 参数驱动:状态由调用方传入、函数只做白名单校验
          (``if state not in {..., 'pending_provider_confirmation'}``)。
       只扫 ① 的话,最主要的那条产出路径 ``finish_monitoring_cell_error``
       根本不在分母里,而判据照样全绿 —— 这是本条判据的活性自证当场
       逼出来的。手写分母漏掉的那一项不会让任何判据变红(本仓记过)。

    枚举结果(**四**条,全部已接线):
      · ``finish_monitoring_cell_error``  → 接线点③ ``close_for_error``
      · ``_recover_expired_monitoring_cells`` → R1 接线点⑤ ``reclaim_orphans``
      · ``recover_abandoned_monitoring_task_execution`` → F-3a 本次接的
      · ``revoke_monitoring_task_coverage_for_organization_refund`` → F-3b 本次接的

    🔴 第四条是**这条判据自己扫出来的**:返修单的 census 与我复核时都只
       数出三条,而机械枚举当场多出一个 ``_recover_expired_monitoring_cells``。
       它恰好在 R1 就已接线,所以不是缺口 —— 但"两个人手数都漏了同一条"
       正是手写分母的典型失效:漏掉的那一项不会让任何判据变红。
    于是"凡进 pending_provider_confirmation 的格,账本必已有终态行"成立,
    admin 复核那一跳无需再接 —— 它接手时账本早已收口。

    ⚠️ 这条判据守的是**这个前提**:哪天有人加了第四条产出路径而没接账本,
       它当场红。
    """
    import ast
    import io
    from pathlib import Path

    src = io.open(Path(__file__).resolve().parents[2] / "db/monitoring_db.py",
                  encoding="utf-8", newline="", errors="replace").read()

    # 已证明会收账本的产出者。名字之外的任何产出者都会让这条判据红。
    wired_producers = {
        "finish_monitoring_cell_error",
        "_recover_expired_monitoring_cells",
        "recover_abandoned_monitoring_task_execution",
        "revoke_monitoring_task_coverage_for_organization_refund",
    }
    # 只**读**这个状态的函数不算产出者。
    readers = {"decide_monitoring_provider_review",
               "list_monitoring_provider_review_queue"}

    tree = ast.parse(src)
    producers: dict[str, list[int]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                continue
            if "pending_provider_confirmation" not in n.value:
                continue
            upper = n.value.upper()
            # 形态① 字面量产出者:SQL 里直接**写**这个状态。
            if "UPDATE" in upper and "SET" in upper:
                producers.setdefault(fn.name, []).append(n.lineno)
        # 形态② 参数驱动产出者:状态由调用方传入,函数只做白名单校验 ——
        #   源码里没有"UPDATE ... SET state='pending_provider_confirmation'"
        #   这样的字面量,所以形态① 扫不到它。
        #   🔴 这一格是活性自证逼出来的:只写形态① 时,
        #      ``finish_monitoring_cell_error`` 落在分母之外而判据全绿 ——
        #      整条最主要的产出路径根本没进分母。
        for n in ast.walk(fn):
            if not isinstance(n, ast.Compare) or not isinstance(n.ops[0], ast.NotIn):
                continue
            allowed = n.comparators[0]
            if not isinstance(allowed, ast.Set):
                continue
            values = {e.value for e in allowed.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if "pending_provider_confirmation" in values:
                producers.setdefault(fn.name, []).append(n.lineno)

    assert producers, (
        "一个产出者都没扫到 —— 结构锚失效了,这条判据此刻零区分力")

    unexpected = {k: v for k, v in producers.items()
                  if k not in wired_producers and k not in readers}
    assert not unexpected, (
        "有新的路径能把 cell 送进 pending_provider_confirmation,而它没在"
        "已接线名单里:\n  " + "\n  ".join(f"{k}(第 {v} 行)"
                                          for k, v in unexpected.items()) +
        "\n后果:这些格的在飞 attempt 会永久悬挂,而 admin 复核接手时"
        "账本还说在跑。要么接账本,要么证明它良性后加进名单。")

    # 活性自证:三条已知产出者确实被扫到了(否则上面那条是空扫)
    missed = sorted(wired_producers - set(producers))
    assert not missed, (
        f"已知产出者 {missed} 没被扫到 —— 结构锚的匹配面变了,分母塌了")


# ══════════════════════════════════════════════════════════════════════
# [R3] 四个「只有结构锁、无行为判据」的接线点 —— 补真链
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 Review 亲毒实证:把 ④ `record_skip_for_plan` 与 ⑤ `close_for_human_resolution`
#    **改绑成 no-op**(调用行保留 ⇒ AST 接线锁照绿)⇒ pkgF 90 条全绿存活。
#    真因是这两点的"判据"直接调桥函数本身 —— **那是在自证桥,不证接线**。
#
# 🔴 本轮的 meta 锁(见 test_run_ledger_wiring)机械扫出来的不是两个缺口,
#    是**四个**:除 ④⑤ 外,
#      · `reserve_monitoring_cell_retry`(①的重试臂)—— 老判据用
#        "手工把 cell 改回 queued + 再 claim 一次"来**等价模拟**重试,
#        注释里就写着"现役重试链的等价动作"。等价声明不是判据。
#      · `_recover_expired_monitoring_cells`(租约恢复)—— 老判据同样是
#        手工 UPDATE 模拟,真正的恢复函数从没被驱动过。
#    两者我也各毒了一发,同样 90 条全绿存活。所以这里补四条,不是两条。


def _grant_admin(cur, user_id: int) -> None:
    cur.execute("INSERT INTO roles (name, display_name) VALUES ('admin','管理员') "
                "ON CONFLICT DO NOTHING RETURNING id")
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id FROM roles WHERE name='admin'")
        row = cur.fetchone()
    cur.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s,%s)",
                (int(user_id), int(row["id"])))
    cur.connection.commit()


# ── ④ create_monitoring_run_cells ────────────────────────────────────

def test_create_run_cells_records_policy_skip_through_the_live_chain(
        cur, live_db, task):
    """[R3 ④] 真 `create_monitoring_run_cells` ⇒ 未购平台落 policy_skip 行。

    这条与老的 `test_policy_skip_counts_terminal_but_not_attempted` 不重复:
    那条直接调 `record_skip_for_plan(cur, row)`(自证桥),这条驱动**生产函数**,
    证明生产函数真的调了桥。Review 的改绑毒只有这条能抓到。
    """
    from db.monitoring_db import create_monitoring_run_cells
    from config.ai_engines import MONITORING_RUN_CELL_PLATFORMS

    entitled = ["dashscope", "deepseek"]
    unbought = [p for p in MONITORING_RUN_CELL_PLATFORMS if p not in entitled]
    assert unbought, "构造失败:必须有没买的平台,否则这条判据零区分力"

    # [工单 E3-1] 生产 `create_monitoring_run_cells` 现在从 brands 冻结租户归属。
    # 这条判据原来根本没建 brands 行(本包夹具的 monitoring_run_cells 手写版
    # 没有指向 brands 的 FK,所以缺行一直没人发现)——
    # 品牌不存在 ⇒ 租户 NULL ⇒ 账本按新规矩拒绝落账。品牌是**输入**,补上。
    cur.execute(
        "INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET owner_user_id = EXCLUDED.owner_user_id",
        (TASK_BRAND, "被测品牌", USER))

    cur.execute(
        "INSERT INTO extra_keywords (client_id, keyword, target_brand, brand_id, "
        "monitoring_query) VALUES ('c-r3',%s,%s,%s,%s) RETURNING id",
        ("防御型监测词", "被测品牌", TASK_BRAND, "这个行业里有哪些值得关注的品牌?"))
    kw_id = int(cur.fetchone()["id"])
    # 生产函数会把 executable_count 与 monitoring_tasks.total_tests 逐值比对
    cur.execute("UPDATE monitoring_tasks SET total_tests=%s WHERE id=%s",
                (len(entitled), task))
    cur.connection.commit()

    cells = create_monitoring_run_cells(
        task_id=task, brand_id=TASK_BRAND,
        keywords=[{
            "id": kw_id, "source": "extra", "quote_id": None,
            "keyword": "防御型监测词", "target_brand": "被测品牌",
            "monitoring_query": "这个行业里有哪些值得关注的品牌?",
            "entitlement_platforms": entitled,
            "_eligible_monitoring_platforms": set(entitled),
        }],
        search_mode="enhanced",
        fulfillment_credential=str(uuid.uuid4()),
        fulfillment_state="covered",
    )
    assert len(cells) == len(MONITORING_RUN_CELL_PLATFORMS), cells

    by_platform = {c["platform"]: c for c in cells}
    # ① 没买的平台 → 账本里有 policy_skipped 终态行
    for platform in unbought:
        rows = _attempts(cur, by_platform[platform]["plan_hash"])
        assert len(rows) == 1, (
            f"{platform} 没落 policy_skip —— 生产函数没真的调到桥。"
            f"后果:五卡 terminal 永远到不了 planned,守恒判"
            f"'数据坏了',而真相是这几个平台客户没买:{rows}")
        assert rows[0]["terminal_state"] == "policy_skipped", rows[0]
        assert rows[0]["provider_called"] is False, (
            "policy_skipped 记成了 provider 调用 —— attemptRecords 会虚高")
    # ② 买了的平台 → 此刻**零**账本行(它们还没跑,不是 skip)
    for platform in entitled:
        assert _attempts(cur, by_platform[platform]["plan_hash"]) == [], (
            f"{platform} 是买了的,却被记成 policy_skipped —— "
            f"一格真要跑的活会凭空消失")


# ── ⑤ decide_monitoring_identity_review ──────────────────────────────

def test_identity_review_appends_answered_through_the_live_chain(
        cur, live_db, task):
    """[R3 ⑤] 真 `decide_monitoring_identity_review` ⇒ 账本追加 answered。

    与老的 `test_human_identity_resolution_appends_answered` 不重复:
    那条直接调 `close_for_human_resolution(...)`(自证桥)。这条从
    claim → save(pending_identity) → **人工确认** 全程走生产函数。

    不接这一跳的后果是**真实的数据错误**:客户已经人工确认过"这就是我家",
    而五卡上永远写着"身份待确认"。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  decide_monitoring_identity_review,
                                  save_monitoring_result)

    # 🔴 [E3 补洞 2026-08-27] 品牌**已经转移**:当前 owner 不是格上冻结的那个人。
    #    原来这里写的是 ``USER`` —— 与格上 ``tenant_owner_user_id`` 和下面的
    #    ``actor_user_id`` **三者同值 4242**,于是"把操作者写成租户"这个谎
    #    在本判据里根本不可观测(外选 MUT-EXTE3-03 就是从这个缝里活下来的)。
    cur.execute(
        "INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)",
        (TASK_BRAND, "被测品牌", OWNER_AFTER_TRANSFER))
    cur.connection.commit()

    c = _seed_success_cell(cur, task_id=task, keyword_id=61)
    assert c["tenant_owner_user_id"] == USER != OWNER_AFTER_TRANSFER, (
        "夹具没摆成「转移后」的样子,本判据会退回零判别力")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    result_id = save_monitoring_result(
        task_id=task, keyword_id=61, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=True, mention_type="direct",
        full_response="提到了一个同名的别家。", lineage=_success_lineage(),
        identity_brand_id=TASK_BRAND,
        identity_candidates=["被测品牌"],   # 生产读 str(v).strip(),必须是字符串
        identity_evidence_snippet="同名歧义",
        identity_review_state="pending",
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )
    before = _attempts(cur, c["plan_hash"])
    assert len(before) == 1 and before[0]["terminal_state"] == "entity_ambiguous", before

    cur.execute("SELECT identity_evidence_hash, identity_decision_version "
                "FROM monitoring_results WHERE id=%s", (result_id,))
    meta = cur.fetchone()

    # 🔴 [E3 补洞 2026-08-27 · 外选 MUT-EXTE3-03] **操作者 ≠ 租户**。
    #    这一行原来传的是 ``actor_user_id=USER`` —— 与 ``brands.owner_user_id``
    #    和格上的 ``tenant_owner_user_id`` **三者同值 4242**。于是"把操作者
    #    写成租户"这个谎在本条判据里根本不可观测:归属记成谁都一样是 4242。
    #    外选变异把 ``db/monitoring_db.py`` 的调用点改成
    #    ``tenant_owner_user_id=int(actor_user_id)``,全分母零红,就是这里漏的。
    #    本条走 **owner 授权臂**(操作者 = 当前 owner),admin 授权臂由
    #    ``test_identity_review_by_a_non_owner_admin_also_appends_answered``
    #    走 —— 两条臂都要有人守,换臂不算补洞。
    decide_monitoring_identity_review(
        result_id=int(result_id), brand_id=TASK_BRAND,
        actor_user_id=OWNER_AFTER_TRANSFER,
        action="yes", selected_name="被测品牌",
        expected_version=int(meta["identity_decision_version"]),
        evidence_hash=str(meta["identity_evidence_hash"]),
        request_id=str(uuid.uuid4()),
    )

    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 2, (
        f"人工确认没在账本里追加一行 —— 生产函数没真的调到桥。"
        f"后果:客户确认过'这就是我家',五卡上仍写'身份待确认':{rows}")
    assert rows[0]["terminal_state"] == "entity_ambiguous", (
        "原始那条身份待定的观测被改写了 —— MON-03 禁止")

    # 🔴 追加那一行的归属:必须是格上冻结的**租户**,不是点确认的那个人。
    appended = rows[1]
    assert appended["tenant_owner_user_id"] == USER, (
        f"人工确认那一跳记错了归属:账本写 {appended['tenant_owner_user_id']},"
        f"格上冻结的租户是 {USER}、点确认的人是 {OWNER_AFTER_TRANSFER}。"
        "两种错法都在这一格上:①把**操作者**当租户(管理员替客户确认之后,"
        "那条 attempt 归到操作者名下);②现读**当前** brands.owner_user_id"
        "(品牌一转移,历史归属跟着改写)。按租户对账/计价的每个口径都从这里取数")
    assert appended["request_hash"] == f"human:{OWNER_AFTER_TRANSFER}", (
        f"操作者没留在 request_hash 里:{appended['request_hash']} —— "
        "两个身份要分开记,不是丢掉一个")

    after = LEDGER.canonical_attempt(
        LEDGER.attempts_for_cell(cur, plan_cell_id=c["plan_hash"]))
    assert after.terminal_state == "answered", (
        f"人工确认之后 canonical 还不是 answered:{after}")
    assert after.provider_called is False, (
        "人工确认那一跳被记成了 provider 调用 —— attemptRecords 会虚高一次")


def test_the_identity_fixture_keeps_the_three_ids_distinct(cur, task):
    """🔴 夹具判别力自证 —— 上面那条判据的全部力量都系在"三个 id 互不相同"。

    合流前这三个数(格上 ``tenant_owner_user_id`` / ``brands.owner_user_id`` /
    ``actor_user_id``)**全是 4242**,于是"归属记成操作者"、"归属现读当前 owner"
    与"归属记对了"在账本里长得一模一样,判据照绿。本条把这件事本身钉成判据:
    谁把它们改回同值,先在这里红,而不是等下一次外选变异来告诉我们。

    三个身份各自的角色:
      · ``USER`` = 建格那一刻冻结的租户(**账本该记的那个**);
      · ``OWNER_AFTER_TRANSFER`` = 品牌转移后的当前 owner,也是 owner 臂的操作者;
      · R4 那条 admin 臂判据里的 ``admin_user`` = 既不是租户也不是 owner 的第三个人。
    """
    assert USER != OWNER_AFTER_TRANSFER, (
        f"租户与当前 owner 又被摆成同一个 id({USER}) —— "
        "真链判据对「归属漂移 / 归属记成操作者」会重新失去判别力")
    cur.execute(
        "INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET owner_user_id = EXCLUDED.owner_user_id",
        (TASK_BRAND, "被测品牌", OWNER_AFTER_TRANSFER))
    cur.connection.commit()
    c = _seed_success_cell(cur, task_id=task, keyword_id=62)
    cur.execute("SELECT owner_user_id FROM brands WHERE id=%s", (TASK_BRAND,))
    live_owner = int(cur.fetchone()["owner_user_id"])
    assert c["tenant_owner_user_id"] == USER, (
        f"格上冻结的租户不是 {USER}:{c['tenant_owner_user_id']} —— "
        "夹具与断言的分母对不上,那条判据判的就不是它以为的那件事")
    assert live_owner != c["tenant_owner_user_id"], (
        "当前 owner 与格上冻结的租户同值 —— 「转移后不漂移」这件事无从观测")


# ── ①重试臂 reserve_monitoring_cell_retry ────────────────────────────

def test_retry_reservation_opens_a_new_attempt_through_the_live_chain(
        cur, live_db, task):
    """[R3 ①重试臂] 真 `reserve_monitoring_cell_retry` ⇒ 账本开出第二条 attempt。

    老判据用"手工把 cell 改回 queued + 再 claim 一次"来**等价模拟**重试 ——
    等价声明不是判据,重试臂那一处 `open_for_claim` 从没被驱动过
    (毒它一发,90 条全绿存活)。

    不记这一次的后果:重试那次调用在账本里根本不存在,
    attemptRecords 与真实调用数对不上(MON-11);并且这一格从此没有在飞行,
    后续 close_for_result / close_for_error 都收不到东西(rowcount 0)。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  finish_monitoring_cell_error,
                                  reserve_monitoring_cell_retry)

    c = _seed_cell(cur, task_id=task, keyword_id=62, platform="dashscope",
                   retry_coverage={"policy_version": "monitoring-retry-v1",
                                   "coverage": "included", "max_attempts": 3})
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    finish_monitoring_cell_error(
        cell_id=int(c["id"]), claim_token=_claim_token(cur, c["id"]),
        state="failed", error_code="FIRST_FAILURE", error_message="one")
    assert len(_attempts(cur, c["plan_hash"])) == 1

    out = reserve_monitoring_cell_retry(
        task_id=task, cell_id=int(c["id"]), brand_id=TASK_BRAND,
        request_id=str(uuid.uuid4()), expected_plan_hash=str(c["plan_hash"]))
    assert out["replay"] is False, out

    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 2, (
        f"重试预约没在账本里开出新 attempt —— 重试臂的 open_for_claim 没生效。"
        f"这一格从此没有在飞行,后续收口全部 rowcount 0:{rows}")
    assert [r["attempt_ordinal"] for r in rows] == [1, 2], rows
    assert rows[0]["error_code"] == "FIRST_FAILURE", (
        "第一次失败被覆盖了 —— MON-03 的整个理由就是它不许被覆盖")
    assert rows[1]["terminal_state"] is None, (
        f"重试那条一开出来就是终态 —— 它应该在飞:{rows[1]}")


# ── 租约恢复 _recover_expired_monitoring_cells ───────────────────────

def test_expired_lease_recovery_closes_the_ledger_through_the_live_chain(
        cur, live_db, task):
    """[R3 恢复点] 真 `_recover_expired_monitoring_cells` ⇒ 在飞行被收口。

    老判据同样是手工 UPDATE 模拟崩溃,真正的恢复函数从没被驱动过
    (毒它一发,90 条全绿存活)。

    这一跳收不掉的后果:`uq_defgeo_attempt_single_inflight` 让那条在飞行
    永久占位,账本一直说"还在跑",而 cell 早已终态。
    """
    from db.monitoring_db import (_recover_expired_monitoring_cells,
                                  claim_monitoring_run_cell)

    c = _seed_cell(cur, task_id=task, keyword_id=63, platform="dashscope")
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    assert _attempts(cur, c["plan_hash"])[0]["terminal_state"] is None

    # 租约过期 + 已派发(⇒ 结果未知,不是"没派发")
    cur.execute("UPDATE monitoring_run_cells SET claim_until=NOW() - interval '1 hour', "
                "provider_dispatched_at=NOW() WHERE id=%s", (int(c["id"]),))
    cur.connection.commit()

    n = _recover_expired_monitoring_cells(cur, cell_id=int(c["id"]))
    cur.connection.commit()
    assert n == 1, f"恢复函数没认领这一格(改了 {n} 行)"

    row = _attempts(cur, c["plan_hash"])[0]
    assert row["terminal_state"] == "engine_error", (
        f"cell 被租约恢复终态化了,账本却还说在飞:{row} —— "
        f"在飞行永久占位,五卡分母从此长期偏")
    assert row["error_code"] == "provider_outcome_unknown", (
        f"派发过的那一格必须落 provider_outcome_unknown,实际 {row['error_code']}")


def test_identity_review_by_a_non_owner_admin_also_appends_answered(
        cur, live_db, task):
    """[R4] ⑤ 的**另一条授权臂**:操作者不是品牌 owner,而是 admin。

    生产函数先查 `user_roles`/`roles` 认 admin,认不出才回落到
    owner / `user_clients` / 组织授权。上一条走的是 **owner** 那一臂,
    这条走 **admin** 那一臂 —— 两条臂进的是同一段接线代码,但**授权前置
    条件完全不同**,只验一臂等于没验另一臂能不能走到接线那一步。

    (这条同时把 `_grant_admin` 用起来:R3 我写了那个 helper 却没有调用方,
     那本身就是本仓最忌讳的形态 —— 死代码,而且是判据里的死代码。)
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  decide_monitoring_identity_review,
                                  save_monitoring_result)

    owner_user = USER + 1                      # 品牌属于**别人**
    admin_user = USER + 2                      # 操作者只是 admin
    cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)",
                (TASK_BRAND, "被测品牌", owner_user))
    _grant_admin(cur, admin_user)

    c = _seed_success_cell(cur, task_id=task, keyword_id=64)
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    result_id = save_monitoring_result(
        task_id=task, keyword_id=64, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=True, mention_type="direct",
        full_response="提到了一个同名的别家。", lineage=_success_lineage(),
        identity_brand_id=TASK_BRAND,
        identity_candidates=["被测品牌"],
        identity_evidence_snippet="同名歧义",
        identity_review_state="pending",
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )
    cur.execute("SELECT identity_evidence_hash, identity_decision_version "
                "FROM monitoring_results WHERE id=%s", (result_id,))
    meta = cur.fetchone()

    decide_monitoring_identity_review(
        result_id=int(result_id), brand_id=TASK_BRAND,
        actor_user_id=admin_user,              # ← 非 owner
        action="yes", selected_name="被测品牌",
        expected_version=int(meta["identity_decision_version"]),
        evidence_hash=str(meta["identity_evidence_hash"]),
        request_id=str(uuid.uuid4()),
    )

    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 2, (
        f"admin 臂确认后账本没追加 —— 这一臂根本没走到接线那一步:{rows}")
    # 🔴 [E3 补洞 2026-08-27] 这一臂的三个身份天然互不相同
    #    (租户 USER / owner USER+1 / admin USER+2),正好是钉归属最干净的一格:
    #    admin 替客户确认,账本必须仍记**格上冻结的租户**,不是这位 admin,
    #    也不是品牌**当前**的 owner。
    appended = rows[1]
    assert appended["tenant_owner_user_id"] == USER, (
        f"admin 臂把归属记成了 {appended['tenant_owner_user_id']} —— "
        f"应当是格上冻结的 {USER}(操作者 {admin_user} / 当前 owner {owner_user} "
        "都不是这一行的租户)")
    assert appended["request_hash"] == f"human:{admin_user}", (
        f"操作者没留在 request_hash 里:{appended['request_hash']}")
    after = LEDGER.canonical_attempt(
        LEDGER.attempts_for_cell(cur, plan_cell_id=c["plan_hash"]))
    assert after.terminal_state == "answered", after
    assert after.provider_called is False, after


def test_a_stranger_cannot_resolve_someone_elses_identity_review(
        cur, live_db, task):
    """[R4] 上一条的**反向臂**:既不是 owner 也不是 admin ⇒ 拒绝。

    没有这条,上一条证明不了"admin 这个身份起了作用" ——
    一个把授权检查整个删掉的实现会让上一条照样绿。
    """
    from db.monitoring_db import (claim_monitoring_run_cell,
                                  decide_monitoring_identity_review,
                                  save_monitoring_result)

    owner_user = USER + 1
    stranger = USER + 3                        # 什么都不是
    cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s)",
                (TASK_BRAND, "被测品牌", owner_user))
    cur.connection.commit()

    c = _seed_success_cell(cur, task_id=task, keyword_id=65)
    claim_monitoring_run_cell(cell_id=int(c["id"]), task_id=task,
                              brand_id=TASK_BRAND, allowed_state="queued")
    result_id = save_monitoring_result(
        task_id=task, keyword_id=65, keyword=_LINEAGE_KEYWORD,
        platform="dashscope", is_detected=True, mention_type="direct",
        full_response="提到了一个同名的别家。", lineage=_success_lineage(),
        identity_brand_id=TASK_BRAND,
        identity_candidates=["被测品牌"],
        identity_evidence_snippet="同名歧义",
        identity_review_state="pending",
        cell_id=int(c["id"]), cell_claim_token=_claim_token(cur, c["id"]),
    )
    cur.execute("SELECT identity_evidence_hash, identity_decision_version "
                "FROM monitoring_results WHERE id=%s", (result_id,))
    meta = cur.fetchone()

    with pytest.raises(PermissionError):
        decide_monitoring_identity_review(
            result_id=int(result_id), brand_id=TASK_BRAND,
            actor_user_id=stranger,
            action="yes", selected_name="被测品牌",
            expected_version=int(meta["identity_decision_version"]),
            evidence_hash=str(meta["identity_evidence_hash"]),
            request_id=str(uuid.uuid4()),
        )

    rows = _attempts(cur, c["plan_hash"])
    assert len(rows) == 1 and rows[0]["terminal_state"] == "entity_ambiguous", (
        f"被拒的确认竟然在账本里留了痕 —— 未授权的人改动了客户的结论:{rows}")
