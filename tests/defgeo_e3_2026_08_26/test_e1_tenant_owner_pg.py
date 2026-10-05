"""E3-1 · 监测 attempt 账本租户归属(Codex 二审 P1-F6)。

被修的缺陷(逐条亲证,坐标见迁移 054 文件头):
  六个写点没有一个读得到真租户 —— 四个读 ``cell["billing_user_id"]``
  (该列在 monitoring_run_cells 上**不存在**)恒落 **0**;一个现读可变的
  ``brands.owner_user_id``(品牌转移即改写历史归属);一个把**操作者**
  写成租户。

本文件的分母纪律
----------------
🔴 所有"真列名"分母都取自 **conftest 用真迁移文件建出来的库**的
   ``information_schema`` —— 与注册表、夹具、桥源码三个被测对象全都无关。
   这一条是本单的核心教训:上一次这条锁的分母是手写夹具,于是恒绿。
"""

from __future__ import annotations

import ast
import functools
import re
import uuid
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
LEDGER = "defgeo_monitoring_attempts"

BRAND = 7701
OWNER = 4242
OTHER_OWNER = 9988
ACTOR = 5150


# ══════════════════════════════════════════════════════════════════════
# 取真列名(唯一分母来源)
# ══════════════════════════════════════════════════════════════════════

def _live_columns(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT a.attname FROM pg_catalog.pg_attribute a "
        " WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped",
        (f"public.{table}",))
    return {r["attname"] for r in cur.fetchall()}


def _seed_brand(cur, *, owner: int | None = OWNER, brand_id: int = BRAND) -> None:
    cur.execute(
        "INSERT INTO public.brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET owner_user_id = EXCLUDED.owner_user_id",
        (brand_id, f"brand-{brand_id}", owner))


def _seed_task(cur, *, brand_id: int = BRAND) -> int:
    cur.execute(
        "INSERT INTO public.monitoring_tasks (brand_id, total_tests) "
        "VALUES (%s,0) RETURNING id", (brand_id,))
    return int(cur.fetchone()["id"])


def _seed_cell(cur, *, task_id: int, keyword_id: int, platform: str = "dashscope",
               tenant: int | None = OWNER, is_planned: bool = True,
               state: str = "queued", error_code: str | None = None,
               brand_id: int = BRAND) -> dict:
    """建一格 —— 这是**输入**(计划),不是被测对象。

    列集合逐值取生产 ``create_monitoring_run_cells`` 那条 INSERT 的形状;
    ``tenant_owner_user_id`` 是迁移 054 真加的那一列。
    """
    import hashlib
    ph = hashlib.sha256(
        f"{task_id}:{keyword_id}:{platform}:{tenant}".encode()).hexdigest()
    cur.execute(
        """
        INSERT INTO public.monitoring_run_cells
            (task_id, brand_id, keyword_id, keyword_source, keyword_snapshot,
             question_snapshot, target_brand_snapshot, platform, is_planned,
             state, entitlement_snapshot, order_snapshot, fulfillment_credential,
             fulfillment_state, plan_hash, tenant_owner_user_id,
             error_code, completed_at)
        VALUES (%s,%s,%s,'confirmed','kw','q','tb',%s,%s,%s,
                '{"schema_version":"monitoring-entitlement-snapshot-v1"}'::jsonb,
                '{"schema_version":"monitoring-order-snapshot-v1"}'::jsonb,
                %s,'reserved',%s,%s,%s,
                CASE WHEN %s THEN NULL ELSE NOW() END)
        RETURNING *
        """,
        (task_id, brand_id, keyword_id, platform, is_planned, state,
         str(uuid.uuid4()), ph, tenant, error_code,
         state in ("queued", "running")))
    return dict(cur.fetchone())


def _attempts(cur) -> list[dict]:
    cur.execute(
        f"SELECT * FROM public.{LEDGER} ORDER BY attempt_ordinal")
    return [dict(r) for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════════════════
# ① 分母对账 —— 注册表 / 夹具 / 桥源码 三方都要对得上真 DDL
# ══════════════════════════════════════════════════════════════════════

def test_e1_01_python_registry_matches_the_migration_built_table(cur):
    """``assert_monitoring_cell_retry_ready`` 的列注册表 == 真迁移建出来的列。

    🔴 二审报告说「注册表声称有 billing_user_id」—— 这一句**不成立**
       (亲手核过:那一格没有 billing_user_id)。但注册表与真 DDL 之间
       此前**没有任何判据**在对账,所以"报告说错了"不等于"这里没风险":
       注册表用的是 **exact set equality**(``actual != expected → RuntimeError``),
       任何一次加列忘了同步注册表 = 启动守卫当场炸。这一条把它挪到判据里先炸。
    """
    from db.monitoring_db import assert_monitoring_cell_retry_ready  # noqa: F401
    import inspect

    src = inspect.getsource(assert_monitoring_cell_retry_ready)
    tree = ast.parse("def f():\n" + "\n".join(
        "    " + ln for ln in src.splitlines()[1:]))
    registry: set[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "expected" for t in node.targets):
            continue
        table_map = ast.literal_eval(node.value)
        registry = set(table_map["monitoring_run_cells"])
    assert registry, "没从源码里解析出列注册表 —— 探针失效,分母是空的"

    live = _live_columns(cur, "monitoring_run_cells")
    assert len(live) > 20, f"真列名分母塌了,只解析出 {sorted(live)}"
    assert registry == live, (
        "列注册表与真 DDL 对不上。\n"
        f"  注册表多出(真库没有):{sorted(registry - live)}\n"
        f"  真库多出(注册表漏登):{sorted(live - registry)}\n"
        "  注册表用 exact set equality,漂一列启动守卫就会炸。")


def test_e1_02_the_column_denominator_has_discriminating_power(cur):
    """上一条的判别力自证。

    分母解析成"包含一切"时上一条恒绿。这里直接验两件事:
    一个**明知不存在**的列不在真列名里;一个**明知存在**的在。
    """
    live = _live_columns(cur, "monitoring_run_cells")
    assert "billing_user_id" not in live, (
        "真列名里出现了 billing_user_id —— 那是本单要杀的那个幻列,"
        "它只存在于 monitoring_keyword_settlements 上")
    assert "tenant_owner_user_id" in live, (
        "迁移 054 的 tenant_owner_user_id 不在 —— 底座没跑到 054")


def test_e1_03_the_pkgf_fixture_may_not_invent_columns(cur):
    """pkgF 手写夹具声明的列 ⊆ 真列。

    🔴 这是本单最贵的那条教训的**结构锁**:pkgF 的 ``_LIVE_SCHEMA`` 曾凭空
       多一列 ``billing_user_id``,而它同时是
       ``test_open_for_claim_only_reads_keys_the_live_cell_row_really_has``
       的分母 —— 被污染的分母让那条锁恒绿,桥在生产里恒落租户 0。
       夹具同时是锁的分母 = 本仓「夹具供了生产不会供的东西」的**第三形态**。
       从今往后:任何人往那份夹具里加一列生产没有的,这一条红。
    """
    import re
    from tests.defensive_geo_pkgf_2026_08_23.conftest import _LIVE_SCHEMA

    block = _LIVE_SCHEMA.split(
        "CREATE TABLE IF NOT EXISTS monitoring_run_cells")[1]
    block = block.split("CREATE TABLE")[0]
    fixture_cols = set(re.findall(r"(?m)^\s{4}([a-z_]+)\s+[A-Z]", block))
    assert len(fixture_cols) > 15, f"夹具列解析塌了:{sorted(fixture_cols)}"

    live = _live_columns(cur, "monitoring_run_cells")
    invented = sorted(fixture_cols - live)
    assert not invented, (
        f"pkgF 夹具声明了生产没有的列 {invented} —— "
        "生产会静默拿到 None/0,而以这份夹具为分母的锁不会红")


def test_e1_04_every_key_the_bridges_read_off_a_cell_is_a_real_column(cur):
    """两个桥模块从 ``cell`` / ``row`` 上读的每个键都必须是真列名。

    分母 = 真库列名(与桥源码无关);被 census 的是**真源码**。
    """
    live = _live_columns(cur, "monitoring_run_cells")
    read_keys: set[str] = set()
    for rel in ("services/defensive_geo/monitoring/run_ledger_bridge.py",
                "services/defensive_geo/monitoring/legacy_bridge.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            base = None
            key = None
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get" and isinstance(n.func.value, ast.Name)
                    and n.args and isinstance(n.args[0], ast.Constant)
                    and isinstance(n.args[0].value, str)):
                base, key = n.func.value.id, n.args[0].value
            elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                  and isinstance(n.slice, ast.Constant)
                  and isinstance(n.slice.value, str)):
                base, key = n.value.id, n.slice.value
            # 只认**确定**持有 monitoring_run_cells 行的那两个名字。
            #
            # 🔴 刻意**不**收 ``row``:这两个模块里 ``row`` 是个多义名 ——
            #    ``ledger_is_available`` 用它接 ``SELECT to_regclass`` 的结果
            #    (读 ``row["t"]``)、``_inflight_attempt_id`` 用它接账本行
            #    (读 ``row.get("attempt_id")``)。把它收进来会得到两个
            #    **真实存在但不属于 cell** 的键,判据当场假红 —— 假红与真红
            #    一样会浪费下一个人的时间(pkgF 那条锁的第一版就栽在同形上)。
            #    ``plan_hash`` / ``task_id`` / ``brand_id`` 这些键在 ``cell``
            #    与 ``_row`` 两个名字上都读过,所以覆盖没有因此变窄。
            if base in {"cell", "_row"} and key:
                read_keys.add(key)
    assert len(read_keys) >= 5, f"cell 键 census 塌了,只解析出 {sorted(read_keys)}"

    unknown = sorted(read_keys - live)
    assert not unknown, (
        f"桥从 monitoring_run_cells 上读了不存在的键 {unknown} —— "
        "生产会静默拿到 None(然后 `or 0` 把它变成一个编出来的租户)")


def test_e1_05_the_2026_08_19_production_dump_agrees_with_the_migrations(cur):
    """仓内那份生产整库快照里的列 ⊆ 迁移建出来的列。

    快照日期(2026-08-19)早于迁移 054,所以只能是**子集**关系:
    快照有而迁移没有 ⇒ 迁移漏了生产已经在用的列(危险);
    迁移有而快照没有 ⇒ 054 之后新加的(预期,逐个列出来看得见)。
    """
    import re
    dump = (ROOT / "tests/article_self_report_2026_08_19"
                   "/prod_schema_2026-08-19.sql").read_text(encoding="utf-8")
    block = dump.split("CREATE TABLE public.monitoring_run_cells (")[1]
    block = block.split(");")[0]
    dump_cols = set(re.findall(r"(?m)^\s{4}([a-z_]+)\s+[a-z]", block))
    assert len(dump_cols) > 25, f"生产快照列解析塌了:{len(dump_cols)}"

    live = _live_columns(cur, "monitoring_run_cells")
    assert not (dump_cols - live), (
        f"生产已经有、迁移建不出来的列:{sorted(dump_cols - live)}")
    assert (live - dump_cols) == {"tenant_owner_user_id"}, (
        f"迁移比 2026-08-19 生产快照多出的列不止 054 那一列:"
        f"{sorted(live - dump_cols)}")


# ══════════════════════════════════════════════════════════════════════
# ② 禁 0 的**结构**承重(不靠应用层)
# ══════════════════════════════════════════════════════════════════════

def test_e1_10_the_ledger_refuses_a_fabricated_tenant_zero(cur):
    """账本表层面写不进 0。应用层可以被下一个人改回去,CHECK 不会。"""
    cur.execute("SAVEPOINT sp_probe")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            f"INSERT INTO public.{LEDGER} "
            " (attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,"
            "  tenant_owner_user_id, brand_id, actual_provider, actual_model,"
            "  actual_surface, actual_search_mode, request_hash, ledger_version)"
            " VALUES (repeat('a',64), repeat('b',64), 1, 't', 0, %s,"
            "         'p','m','s','sm','rh','v')", (BRAND,))
    cur.execute("ROLLBACK TO SAVEPOINT sp_probe")


def test_e1_11_a_positive_tenant_is_still_writable(cur):
    """上一条的判别力自证:CHECK 不是"什么都拦"。"""
    cur.execute(
        f"INSERT INTO public.{LEDGER} "
        " (attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,"
        "  tenant_owner_user_id, brand_id, actual_provider, actual_model,"
        "  actual_surface, actual_search_mode, request_hash, ledger_version)"
        " VALUES (repeat('a',64), repeat('b',64), 1, 't', %s, %s,"
        "         'p','m','s','sm','rh','v')", (OWNER, BRAND))
    assert len(_attempts(cur)) == 1


def test_e1_12_a_cell_may_not_carry_a_fabricated_tenant_zero(cur):
    """格上那一列同样禁 0(迁移 054 的 CHECK)。"""
    _seed_brand(cur)
    tid = _seed_task(cur)
    cur.execute("SAVEPOINT sp_probe")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _seed_cell(cur, task_id=tid, keyword_id=1, tenant=0)
    cur.execute("ROLLBACK TO SAVEPOINT sp_probe")


def test_e1_13_a_cell_may_carry_an_unknown_tenant(cur):
    """NULL(未知)必须能表达 —— 否则代码只剩"编一个"这条路。"""
    _seed_brand(cur)
    tid = _seed_task(cur)
    row = _seed_cell(cur, task_id=tid, keyword_id=1, tenant=None)
    assert row["tenant_owner_user_id"] is None


# ══════════════════════════════════════════════════════════════════════
# ③ 六个写点:拿到真租户 / 拿不到就不落账(逐点一发)
# ══════════════════════════════════════════════════════════════════════

def test_e1_20_claim_writes_the_frozen_tenant(cur):
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=1, state="queued")
    assert BRIDGE.open_for_claim(cur, cell) is not None
    rows = _attempts(cur)
    assert len(rows) == 1
    assert rows[0]["tenant_owner_user_id"] == OWNER


def test_e1_21_claim_writes_nothing_when_the_tenant_is_unknown(cur, monkeypatch):
    """取不到租户 ⇒ **零行**,不是一行 0。"""
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    alerts: list[dict] = []
    monkeypatch.setattr(BRIDGE, "_alert_tenant_unresolved",
                        lambda **kw: alerts.append(kw))
    _seed_brand(cur)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=1, tenant=None)
    assert BRIDGE.open_for_claim(cur, cell) is None
    assert _attempts(cur) == []
    assert len(alerts) == 1 and alerts[0]["what"] == "open_for_claim", (
        "取不到租户必须**响亮**(转人工),不许静默跳过")


def test_e1_22_claim_never_falls_back_to_the_live_brand_owner(cur):
    """格上是 NULL 而品牌**有** owner 时,仍然零行。

    这一条把"回落读 brands"这条路堵死:回落在正常数据下永远拿得到一个数,
    于是 e1_21 那种造 NULL 的判据抓不到它 —— 必须专门打这一格。
    """
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur, owner=OWNER)          # 品牌有 owner
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=1, tenant=None)   # 格上没有
    assert BRIDGE.open_for_claim(cur, cell) is None
    assert _attempts(cur) == [], "桥回落读了 brands.owner_user_id —— 归属又可漂移了"


def test_e1_23_policy_skip_writes_the_frozen_tenant(cur):
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=2, is_planned=False,
                      state="unavailable",
                      error_code="not_in_purchased_run_plan")
    assert BRIDGE.record_skip_for_plan(cur, cell) is not None
    rows = _attempts(cur)
    assert len(rows) == 1 and rows[0]["tenant_owner_user_id"] == OWNER


def test_e1_24_policy_skip_writes_nothing_when_the_tenant_is_unknown(cur):
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=2, is_planned=False,
                      state="unavailable", tenant=None,
                      error_code="not_in_purchased_run_plan")
    assert BRIDGE.record_skip_for_plan(cur, cell) is None
    assert _attempts(cur) == []


def test_e1_25_unattempted_closeout_writes_the_frozen_tenant(cur):
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=3, state="failed")
    n = BRIDGE.close_unattempted_for_cells(
        cur, monitoring_cell_ids=[int(cell["id"])],
        reason_code=BRIDGE.UNATTEMPTED_ABANDONED_REASON)
    assert n == 1
    rows = _attempts(cur)
    assert len(rows) == 1 and rows[0]["tenant_owner_user_id"] == OWNER


def test_e1_26_a_brand_transfer_does_not_rewrite_history(cur):
    """建格之后品牌被转移 ⇒ 账本仍然记**当时**那个租户。

    这是 P1-F6 的第二半:⑤ 原来现读 ``brands.owner_user_id``。
    """
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur, owner=OWNER)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=3, state="failed")
    # 品牌转移(真实业务动作)
    cur.execute("UPDATE public.brands SET owner_user_id=%s WHERE id=%s",
                (OTHER_OWNER, BRAND))
    BRIDGE.close_unattempted_for_cells(
        cur, monitoring_cell_ids=[int(cell["id"])],
        reason_code=BRIDGE.UNATTEMPTED_ABANDONED_REASON)
    rows = _attempts(cur)
    assert len(rows) == 1
    assert rows[0]["tenant_owner_user_id"] == OWNER, (
        f"品牌转移改写了历史归属(记成了 {rows[0]['tenant_owner_user_id']})")


def test_e1_27_human_resolution_records_the_tenant_not_the_operator(cur):
    """人工确认那一跳:租户是租户,操作者进 request_hash。"""
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur)
    tid = _seed_task(cur)
    cur.execute("INSERT INTO public.monitoring_results (task_id) VALUES (%s) "
                "RETURNING id", (tid,))
    rid = int(cur.fetchone()["id"])
    cell = _seed_cell(cur, task_id=tid, keyword_id=4, state="queued")
    aid = BRIDGE.close_for_human_resolution(
        cur, plan_cell_id=str(cell["plan_hash"]), monitoring_result_id=rid,
        actor_user_id=ACTOR, brand_id=BRAND, run_authority_id=str(tid),
        tenant_owner_user_id=cell["tenant_owner_user_id"])
    assert aid is not None
    rows = _attempts(cur)
    assert len(rows) == 1
    assert rows[0]["tenant_owner_user_id"] == OWNER, (
        f"人工确认把**操作者**写成了租户(记成了 "
        f"{rows[0]['tenant_owner_user_id']},操作者是 {ACTOR})")
    assert rows[0]["request_hash"] == f"human:{ACTOR}", (
        "操作者应当留在 request_hash 里 —— 两个身份分开记,不是丢掉一个")


def test_e1_27b_the_bridge_records_whatever_the_caller_hands_it(cur):
    """🔴 **错传臂** —— 把上一条的判别力边界钉死。

    上一条(e1_27)自己传的是**对的**那个值,所以它证明的只有一句:
    「传对了会记对」。它证明不了「调用方传的是不是对的」——
    这一条把反面跑一遍:调用方硬把**操作者**当租户递进来时,桥照收照写,
    谓词一声不吭(``actor > 0`` 恒过)。

    这不是缺陷,是分工:桥只负责"不许编造 0",辨认"这个数是不是租户"
    在桥里做不到 —— 它手上没有第二个信源。所以调用方那一侧必须自己有锁:
      · 结构锁 :func:`test_e1_44_no_caller_may_pass_anything_but_the_frozen_cell_column`
      · 行为锁 :pkgF ``test_identity_review_appends_answered_through_the_live_chain``
    外选 MUT-EXTE3-03 正是在调用方复辟第⑥个写点、而这两层当时都够不到,
    于是全分母零红。留这条臂在这里,是免得下一个人以为 e1_27 已经覆盖了调用方。
    """
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur)
    tid = _seed_task(cur)
    cur.execute("INSERT INTO public.monitoring_results (task_id) VALUES (%s) "
                "RETURNING id", (tid,))
    rid = int(cur.fetchone()["id"])
    cell = _seed_cell(cur, task_id=tid, keyword_id=4, state="queued")
    assert cell["tenant_owner_user_id"] == OWNER, "夹具本身摆错了"

    # 调用方把 actor 当租户递进来 —— 与 MUT-EXTE3-03 的谎言逐字同形。
    aid = BRIDGE.close_for_human_resolution(
        cur, plan_cell_id=str(cell["plan_hash"]), monitoring_result_id=rid,
        actor_user_id=ACTOR, brand_id=BRAND, run_authority_id=str(tid),
        tenant_owner_user_id=ACTOR)
    assert aid is not None, "桥把它挡掉了?那本条的前提(桥分辨不出)就不成立"
    rows = _attempts(cur)
    assert len(rows) == 1 and rows[0]["tenant_owner_user_id"] == ACTOR, (
        f"桥居然认出了这是操作者而不是租户 —— 那说明桥手上多了一个信源,"
        f"本条的分工前提要重写:{rows}")


def test_e1_28_human_resolution_writes_nothing_without_a_tenant(cur):
    from services.defensive_geo.monitoring import run_ledger_bridge as BRIDGE

    _seed_brand(cur)
    tid = _seed_task(cur)
    cur.execute("INSERT INTO public.monitoring_results (task_id) VALUES (%s) "
                "RETURNING id", (tid,))
    rid = int(cur.fetchone()["id"])
    cell = _seed_cell(cur, task_id=tid, keyword_id=4, tenant=None)
    assert BRIDGE.close_for_human_resolution(
        cur, plan_cell_id=str(cell["plan_hash"]), monitoring_result_id=rid,
        actor_user_id=ACTOR, brand_id=BRAND, run_authority_id=str(tid),
        tenant_owner_user_id=cell["tenant_owner_user_id"]) is None
    assert _attempts(cur) == [], (
        f"拿不到租户时把操作者 {ACTOR} 当成了兜底 —— 那也是编造")


def test_e1_29_legacy_backfill_writes_the_frozen_tenant(cur):
    """第四个写点(存量失败格抢救)。二审报告的四路径清单里**没有它**。"""
    from services.defensive_geo.monitoring import legacy_bridge as LEGACY

    _seed_brand(cur)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=5, state="failed",
                      error_code="engine_timeout")
    assert LEGACY.capture_before_retry_overwrite(cur, cell) is True
    rows = _attempts(cur)
    assert len(rows) == 1 and rows[0]["tenant_owner_user_id"] == OWNER


def test_e1_30_legacy_backfill_writes_nothing_without_a_tenant(cur):
    from services.defensive_geo.monitoring import legacy_bridge as LEGACY

    _seed_brand(cur)
    tid = _seed_task(cur)
    cell = _seed_cell(cur, task_id=tid, keyword_id=5, state="failed",
                      tenant=None, error_code="engine_timeout")
    assert LEGACY.capture_before_retry_overwrite(cur, cell) is False
    assert _attempts(cur) == []


# ══════════════════════════════════════════════════════════════════════
# ④ 唯一谓词 —— 六个写点不许各自抄一份
# ══════════════════════════════════════════════════════════════════════

def _ledger_write_functions() -> dict[str, ast.FunctionDef]:
    """机械枚举:两个桥模块里**每一个**会往账本写 tenant 的函数。

    判定 = 函数体内出现「INSERT ... tenant_owner_user_id」或
    「调用 record_policy_skip / open_attempt」。分母来自源码形状,不手抄名单 ——
    手抄的名单漏掉谁都不会让任何判据变红(本仓记过)。
    """
    found: dict[str, ast.FunctionDef] = {}
    for rel in ("services/defensive_geo/monitoring/run_ledger_bridge.py",
                "services/defensive_geo/monitoring/legacy_bridge.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        # 🔴 只枚举**模块级**函数。两个模块里真正执行 INSERT 的都是内层闭包
        #    (``write`` / ``_write``,因为它们要被 ``guarded`` 包进 SAVEPOINT),
        #    而解析租户的那一句必须在闭包**外面**(取不到就整跳不发生,
        #    不留半条账)。按内层闭包记账会把每一个写点都判成"没走谓词",
        #    那是把正确实现判成错的假红。
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = False
            for sub in ast.walk(node):
                # 🔴 两个模块的 INSERT 都是 f-string(``f"INSERT INTO {TABLE}..."``),
                #    在 AST 里是 JoinedStr 而**不是** Constant ——
                #    只看 Constant 会漏掉 legacy_bridge 那个写点,
                #    而"漏掉的那一个"正是二审报告四路径清单里漏的同一个。
                sql = ""
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    sql = sub.value
                elif isinstance(sub, ast.JoinedStr):
                    sql = "".join(
                        v.value for v in sub.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if sql and "tenant_owner_user_id" in sql and "INSERT" in sql.upper():
                    writes = True
                if (isinstance(sub, ast.Call)
                        and getattr(sub.func, "attr", "") == "record_policy_skip"):
                    writes = True
                if (isinstance(sub, ast.keyword)
                        and sub.arg == "tenant_owner_user_id"):
                    writes = True
            if writes:
                found[f"{Path(rel).stem}.{node.name}"] = node
    return found


def test_e1_40_every_ledger_write_goes_through_the_one_predicate():
    """每一个写点都必须调 ``_require_tenant_owner``,不许自己算。

    🔴 同一谓词写两处 ⇒ 必有一处没人验(本仓记过)。本单之前正是六处
       各写各的:四处 ``or 0``、一处 SQL 里 ``COALESCE(...,0)``、一处写 actor。
    """
    sites = _ledger_write_functions()
    assert len(sites) >= 5, f"写点 census 塌了,只找到 {sorted(sites)}"

    missing = []
    for name, node in sites.items():
        ok = any(
            isinstance(sub, ast.Call)
            and (getattr(sub.func, "id", "") == "_require_tenant_owner"
                 or getattr(sub.func, "attr", "") == "_require_tenant_owner")
            for sub in ast.walk(node))
        if not ok:
            missing.append(name)
    assert not missing, (
        f"这些写点没走唯一谓词:{sorted(missing)} —— 各写各的必有一处没人验")


def test_e1_41_no_bridge_still_reads_the_phantom_column():
    """两个桥模块里不许再有**会被执行**的 ``billing_user_id``。

    它在 monitoring_run_cells 上不存在;出现即等于"我以为我读到了租户"。

    🔴 判的是**可执行位置**,不是文本里出现过。第一版写成裸串扫描,当场
       被自己的注释与 docstring 判红(那两处正是在解释这个幻列为什么危险)——
       本仓记过「引用裁决原文会让裸串结构锁判红」。所以这里走 AST:
       docstring 与注释天然不在被扫的节点里,而任何一个真的会跑的字符串
       常量(SQL、``.get()`` 的键、下标)都在。
    """
    for rel in ("services/defensive_geo/monitoring/run_ledger_bridge.py",
                "services/defensive_geo/monitoring/legacy_bridge.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        docstrings = set()
        for holder in ast.walk(tree):
            if isinstance(holder, (ast.Module, ast.FunctionDef,
                                   ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(holder, "body", None) or []
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                if "billing_user_id" in node.value:
                    hits.append((node.lineno, node.value[:60]))
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if "billing_user_id" in text:
                    hits.append((node.lineno, text[:60]))
        assert not hits, f"{rel} 仍在可执行位置读幻列 billing_user_id:{hits}"


def test_e1_42_the_write_site_census_can_actually_fail():
    """上一组的判别力自证:census 必须认得出六个写点各自的模块归属。"""
    sites = _ledger_write_functions()
    by_module = {name.split(".")[0] for name in sites}
    assert by_module == {"run_ledger_bridge", "legacy_bridge"}, (
        f"写点 census 只覆盖了 {sorted(by_module)} —— "
        "legacy_bridge 那个写点在二审报告的四路径清单里就是漏的")


# ══════════════════════════════════════════════════════════════════════
# ⑤ 调用方 —— 谓词守得住桥内,守不住「调用方递进来的是什么」
#
# 🔴 外选 MUT-EXTE3-03 坐实的洞:第⑥个写点(把**操作者**当租户)可以从桥内
#    挪到**调用方**复辟,而当时三层防线一层都够不到 ——
#      · 直调判据自己传对参数(见 e1_27 / e1_27b);
#      · 真链判据的夹具里 actor == owner **同值**(零判别力,pkgF 已补);
#      · 写点 census 的分母只有**两个桥模块**,不含 db/monitoring_db.py。
#    这一组把第三层补上:分母收编**调用方**。
# ══════════════════════════════════════════════════════════════════════

#: 生产代码根。**不含 tests/**(判据不是被测对象)。
#: ``scripts/mutation_runner_*.py`` 里那些调用都是**字符串常量**,
#: AST 天然不把它们当 Call —— 不需要额外排除,也排除不掉才对。
_PRODUCTION_ROOTS = ("api", "db", "services", "tools", "workflows",
                     "agents", "middleware", "scripts")

#: 桥自己的两个模块不算"调用方"。
_BRIDGE_RELS = ("services/defensive_geo/monitoring/run_ledger_bridge.py",
                "services/defensive_geo/monitoring/legacy_bridge.py")

_CELL_COLUMN = "tenant_owner_user_id"

#: 读到"那一列本身"的两种合法写法。别的写法(变量、常量、int(actor))全不算。
_READS_THE_COLUMN = re.compile(
    r"""\[\s*['"]tenant_owner_user_id['"]\s*\]"""
    r"""|\.get\(\s*['"]tenant_owner_user_id['"]""")

#: 一眼可辨的"这是人不是租户"的名字。命中即判红 —— 操作者进 request_hash,
#: 不进 tenant。
_OPERATOR_WORDS = ("actor", "operator", "admin_user", "current_user")


def _entry_point_classes() -> dict[str, str]:
    """把写点分母按「租户**怎么**进桥」分三类。分类规则取自**签名**,不手抄。

    · ``kwarg``:签名里显式收 ``tenant_owner_user_id`` ⇒ 调用方负责递对值;
    · ``cell`` :第二个位置参数叫 ``cell`` ⇒ 调用方负责把**原样**的格递进来;
    · ``self`` :两者都不是 ⇒ 桥自己去库里读,调用方没有义务。

    分类必须是**全覆盖**的:多出一种新形状而没人分类,就等于多了一个
    没人守的入口 —— e1_43 会当场红。
    """
    classes: dict[str, str] = {}
    for qualified, node in _ledger_write_functions().items():
        short = qualified.split(".", 1)[1]
        positional = [a.arg for a in node.args.args]
        kwonly = [a.arg for a in node.args.kwonlyargs]
        if _CELL_COLUMN in positional or _CELL_COLUMN in kwonly:
            classes[short] = "kwarg"
        elif len(positional) >= 2 and positional[1] == "cell":
            classes[short] = "cell"
        else:
            classes[short] = "self"
    return classes


@functools.lru_cache(maxsize=None)
def _production_calls(names: tuple[str, ...]) -> tuple[tuple[str, str, int, ast.Call], ...]:
    """全生产树里对这些函数的**调用点**(rel, 源码, 行号, Call 节点)。"""
    hits: list[tuple[str, str, int, ast.Call]] = []
    for root in _PRODUCTION_ROOTS:
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in _BRIDGE_RELS:
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if not any(n in src for n in names):      # 便宜的预筛
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if fn in names:
                    hits.append((rel, src, node.lineno, node))
    return tuple(hits)


def _kwarg_violation(src: str, call: ast.Call) -> str | None:
    """kwarg 类调用点的检查:递进去的必须是**格上那一列**本身。"""
    for kw in call.keywords:
        if kw.arg != _CELL_COLUMN:
            continue
        expr = (ast.get_source_segment(src, kw.value) or "").strip()
        if not _READS_THE_COLUMN.search(expr):
            return (f"递进去的不是格上的 {_CELL_COLUMN} 那一列,而是 {expr!r}")
        low = expr.lower()
        for word in _OPERATOR_WORDS:
            if word in low:
                return (f"递进去的表达式里混进了操作者({word}):{expr!r} —— "
                        "「谁点的」进 request_hash,「这是谁的运行」才进 tenant")
        return None
    return f"调用点没有显式递 {_CELL_COLUMN}"


def _cell_violation(src: str, call: ast.Call) -> str | None:
    """cell 类调用点的检查:格必须**原样**递进去,不许在调用点重新拼装。

    在调用点拼一个新 dict,是把冻结列悄悄摘掉的最短路径
    (外选 MUT-EXTE3-04 的逐字形态:``{k: v for ... if k != "tenant_..."}``)。
    """
    if len(call.args) < 2:
        return "调用点没有把格递进去"
    cell = call.args[1]
    if isinstance(cell, (ast.Dict, ast.DictComp)):
        return (f"在调用点重新拼装了格:"
                f"{(ast.get_source_segment(src, cell) or '')[:80]!r} —— "
                "冻结列可以在这里被静默摘掉")
    if (isinstance(cell, ast.Call)
            and (getattr(cell.func, "id", "") == "dict")
            and (len(cell.args) != 1 or cell.keywords)):
        return (f"``dict(...)`` 带了额外参数:"
                f"{(ast.get_source_segment(src, cell) or '')[:80]!r} —— "
                "那等于在调用点改写格")
    return None


def test_e1_43_the_write_point_denominator_partitions_by_how_the_tenant_arrives():
    """分类必须全覆盖写点分母,且三类都非空。

    🔴 这是下面两条的分母自证:少一类没人分类 = 多一个没人守的入口形状,
       而"没人守"和"守住了"在结果上长得一模一样。
    """
    sites = set(name.split(".", 1)[1] for name in _ledger_write_functions())
    classes = _entry_point_classes()
    assert set(classes) == sites, (
        f"分类没覆盖全部写点:漏 {sorted(sites - set(classes))}")
    buckets: dict[str, list[str]] = {}
    for name, kind in classes.items():
        buckets.setdefault(kind, []).append(name)
    assert set(buckets) == {"kwarg", "cell", "self"}, (
        f"写点入口出现了没被分类的新形状:{buckets}")
    for kind in ("kwarg", "cell", "self"):
        assert buckets[kind], f"{kind} 这一类空了,针对它的判据会对着空气说话"


def test_e1_44_no_caller_may_pass_anything_but_the_frozen_cell_column():
    """🔴 调用方侧的结构锁 —— 第⑥个写点不许在 caller 复辟。

    桥内的唯一谓词只管"不许是 0";"这个数到底是不是租户"桥分辨不出来
    (见 e1_27b)。所以显式收 tenant 的那些入口,**每一个调用点**递进去的
    都必须是格上那一列本身。

    拆红方式(亲毒验过):把 ``db/monitoring_db.py`` 里
    ``tenant_owner_user_id=_cell_row.get("tenant_owner_user_id")``
    改成 ``tenant_owner_user_id=int(actor_user_id)`` —— 管理员替客户确认身份时
    那一行 attempt 会归到管理员名下,而桥、谓词、老 census 全都不会红。
    """
    names = tuple(sorted(n for n, k in _entry_point_classes().items()
                         if k == "kwarg"))
    assert names, "kwarg 类入口为空 —— 本条没有被测对象"
    calls = _production_calls(names)
    assert calls, (
        f"全生产树里找不到 {list(names)} 的调用点 —— 这是探针失效"
        "(改了导入方式 / 挪了模块),不能读成「没人调用所以安全」")

    bad = []
    for rel, src, lineno, call in calls:
        why = _kwarg_violation(src, call)
        if why:
            bad.append(f"{rel}:{lineno} {why}")
    assert not bad, "调用方把别的东西当租户递进了账本:\n  " + "\n  ".join(bad)


def test_e1_45_no_caller_may_rebuild_the_cell_dict_at_the_call_site():
    """cell 类入口:格必须**原样**递进去。

    调用点重新拼装 dict = 冻结列可以在那里被静默摘掉,
    而桥只会抛 ``TenantOwnerUnresolved`` 然后**整跳不落账** ——
    账本里那次调用干脆不存在,比落错值更难发现。
    """
    names = tuple(sorted(n for n, k in _entry_point_classes().items()
                         if k == "cell"))
    assert names, "cell 类入口为空 —— 本条没有被测对象"
    calls = _production_calls(names)
    assert calls, f"全生产树里找不到 {list(names)} 的调用点 —— 探针失效"

    bad = []
    for rel, src, lineno, call in calls:
        why = _cell_violation(src, call)
        if why:
            bad.append(f"{rel}:{lineno} {why}")
    assert not bad, "调用点改写了要递给桥的格:\n  " + "\n  ".join(bad)


def test_e1_46_the_caller_census_can_actually_fail():
    """判别力自证:两个检查器必须真的认得出那两种谎言。

    正样本**逐字取自**外选变异 MUT-EXTE3-03 / MUT-EXTE3-04 的替换文本 ——
    不是我自己编一个更好抓的形状。
    """
    def _first_call(source: str) -> tuple[str, ast.Call]:
        tree = ast.parse(source)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        return source, calls[0]

    # ── MUT-EXTE3-03 的逐字形态:调用方把操作者当租户 ──
    src, call = _first_call(
        'close_for_human_resolution(cur, plan_cell_id=p,\n'
        '                           tenant_owner_user_id=int(actor_user_id))\n')
    assert _kwarg_violation(src, call), "检查器放过了「把操作者当租户」"

    # 同一形状换个马甲:先赋值再递,表达式里连 actor 都看不见。
    src, call = _first_call(
        'close_for_human_resolution(cur, tenant_owner_user_id=who)\n')
    assert _kwarg_violation(src, call), "检查器放过了「递一个来路不明的变量」"

    # 正确写法必须放行,否则这把锁是恒红的(恒红=没人看)。
    src, call = _first_call(
        'close_for_human_resolution(cur,\n'
        '    tenant_owner_user_id=_cell_row.get("tenant_owner_user_id"))\n')
    assert _kwarg_violation(src, call) is None, "检查器把正确写法判红了"
    src, call = _first_call(
        'close_for_human_resolution(cur,\n'
        '    tenant_owner_user_id=row["tenant_owner_user_id"])\n')
    assert _kwarg_violation(src, call) is None, "下标写法被误判"

    # ── MUT-EXTE3-04 的逐字形态:调用点把冻结列从格里剥掉 ──
    src, call = _first_call(
        'open_for_claim(cur, {k: v for k, v in dict(claimed).items()\n'
        '                     if k != "tenant_owner_user_id"})\n')
    assert _cell_violation(src, call), "检查器放过了「调用点剥掉冻结列」"
    src, call = _first_call('open_for_claim(cur, dict(claimed))\n')
    assert _cell_violation(src, call) is None, "检查器把正确写法判红了"
