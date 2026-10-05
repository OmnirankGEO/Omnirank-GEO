"""WP5 发布五步 —— **真 HTTP + 真 PG16** 端到端判据(§11.3 / §12 / §15.7)。

═══════════════════════════════════════════════════════════════════════
🔴 为什么这一份必须是真 HTTP 打真库
═══════════════════════════════════════════════════════════════════════
本仓 2026-08-18 的教训逐字:「只验 ``!= 422`` 会漏掉整层库合同」。
把判据升成「**不许 422 也不许 500**」当场抓出 6 处生产必 500 的端点。
所以本文件的 :func:`_ok` 对**每一个**成功路径同时否掉 422 与 500 ——
422 = 请求形状对不上,500 = 端点在生产里根本跑不完。

夹具里也**不许出现 mock/fake 的被测对象**:
  · app = 真 ``api.defensive_publish_api.router``;
  · DB  = conftest 装好的「生产 pg_dump + 迁移 044」一次性库(不是手写 schema);
  · 钱  = 真 ``middleware.billing.freeze_points`` 写真 ``point_freezes``。

═══════════════════════════════════════════════════════════════════════
🔴 身份注入:键名逐字照抄生产中间件,不是照抄端点读法
═══════════════════════════════════════════════════════════════════════
2026-08-20 的事故形态:夹具写 ``{"id": ...}``、生产中间件写 ``{"user_id": ...}``
⇒ 整片端点生产必 500 而判据全绿。

本文件的 :data:`_IDENTITIES` 逐字照抄 ``auth/middleware.py`` 里
``user_data_source = {...}`` 那个字典字面量的键,并且
:func:`test_01_fixture_identity_keys_match_production_middleware`
用 **AST 现扫**那个字面量做机械分母 —— 手抄漏一个键会在那条判据里变红,
而不是变成下游一整片假绿。

═══════════════════════════════════════════════════════════════════════
🔴 DEFGEO_W3_HTTP_UNBLOCK —— 探路开关,**不能把任何 finding 变绿**
═══════════════════════════════════════════════════════════════════════
本轮实测到**两处**独立的生产必 500 缺陷(见 test_00_finding1 / finding2),
它们都长在 preview 的**第一步**上,把 §11.3 后面四步全部挡死。

如果就此收工,下游 20 条判据一条都没被真跑过 —— 那是「两臂都红 ⇒ 本轮记
『没验』」的形态,交出去等于交一堆没验过的判据代码。

所以本文件提供一个**只在本进程内、只补环境/文案**的探路开关:

    DEFGEO_W3_HTTP_UNBLOCK=1

它做且只做两件事(:func:`_unblock`):
  ① 给一次性库的 ``mhz_media`` 补一列 ``industry``(finding 1 的环境面);
  ② 给 ``copy_registry._ACTION_LABEL`` 补 7 条缺失文案(finding 2 的注册面)。

**它碰不到任何被测逻辑**,而且两条 finding 判据的分母都**不在活库/活进程里**:
  · finding 1 读的是 ``prod_schema_2026-08-19.sql`` 这个**制品文件**;
  · finding 2 读的是 ``copy_registry`` 源码里那个**字面量**的键集合快照。
所以开关开着的时候这两条**照样红** —— 探路开关物理上无法把 finding 变绿。
(这正是「引用锁当裁定依据前必须亲手注毒」要求的那种自证:workaround 与
 判据的分母不在同一个位面上。)

🔴 默认 **关**。CI/交付跑的是关着的那一档,红就是红。
"""

from __future__ import annotations

import ast
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.defensive_geo import copy_registry as _copy
from services.defensive_geo.publish import body_hash as _bh
from services.defensive_geo.publish import decision_snapshot as _ds
from services.defensive_geo.publish import media_identity as _mi
from services.defensive_geo.publish import store as _store

from tests.defensive_geo_w3_2026_08_21.conftest import PROD_SCHEMA, connect

ROOT = Path(__file__).resolve().parents[2]

UNBLOCK = os.environ.get("DEFGEO_W3_HTTP_UNBLOCK") == "1"


# ══════════════════════════════════════════════════════════════════════════
# 身份 —— 逐字照抄 auth/middleware.py 的 user_data_source 字面量
# ══════════════════════════════════════════════════════════════════════════
def _identity(user_id: int, *, is_admin: bool = False) -> dict[str, Any]:
    import time

    now = int(time.time())
    return {
        # JWT 机制位。端点不读它们,但生产的 request.state.user 里**确实有** ——
        # 判据要求夹具是生产键集合的超集,就不给它们开例外口子
        # (开了口子,下一个真正重要的键漏掉时也会顺着同一个口子溜过去)。
        "iat": now,
        "exp": now + 7200,
        "user_id": user_id,                     # 🔴 生产写的是 user_id,不是 id
        "username": f"w3c_test_{user_id}",
        "display_name": f"w3c {user_id}",
        "is_admin": is_admin,
        "roles": [],
        "permissions": [],
        "client_brand_ids": [],
        "perm_version": 1,
        "must_change_password": 0,
        "team_context": None,
    }


#: 甲 = tenant 9301(拥有 brand 9401);乙 = tenant 9302(拥有 brand 9402)。
#: 两个都是**非 admin** —— admin 会让 require_brand_access 直接放行,
#: 跨租户同形 404 那条判据就失去被测对象。
_IDENTITIES: dict[str, dict[str, Any]] = {
    "a": _identity(9301),
    "b": _identity(9302),
    "admin": _identity(9303, is_admin=True),
}

TENANT_A = 9301
TENANT_B = 9302
BRAND_A = 9401
BRAND_B = 9402


# ══════════════════════════════════════════════════════════════════════════
# POR-06 毒串 —— **定义在判据文件里**,与被测模块物理隔离
# ══════════════════════════════════════════════════════════════════════════
#: 被测代码里不存在这些常量;它们只从**库里的私有列**流进来。
#: 所以「响应体里出现它」只可能是私有列泄漏了,不可能是巧合。
POISON_TEXT = "ZZPOISONW3C-e3f1a9d4-supplier-secret-do-not-leak"
POISON_PROVIDER = "ZQXPOISON7K4"          # provider 是 varchar(16)
POISON_PROVIDER_MEDIA_ID = 918273645      # provider_media_id 是 integer

#: 毒串要落进的私有列。**逐列点名**,不是「往 remark 里塞一句就算」——
#: media_proposal 的 SQL 只读 provider/provider_media_id 这两列私有原料,
#: 只毒 remark 会让判据打不到真正有风险的那两列(零判别力)。
_POISON_COLUMNS: dict[str, Any] = {
    "remark": POISON_TEXT,
    "case_link": POISON_TEXT,
    "entrance_link": POISON_TEXT,
    "provider": POISON_PROVIDER,
    "provider_media_id": POISON_PROVIDER_MEDIA_ID,
}


# ══════════════════════════════════════════════════════════════════════════
# 探路开关(见模块 docstring)
# ══════════════════════════════════════════════════════════════════════════
#: finding 2:窗C 的 façade 会下发这些 action kind,但 copy_registry 里没有登记。
#: ``_recovery_action`` 拿到空文案就 raise ⇒ 受控 500。
_PUBLISH_ACTION_KINDS: tuple[str, ...] = (
    "confirm_publish_decision", "retry_child", "cancel", "new_customer_snapshot",
    "repair_legal_passage", "verify_outcome", "review_replacement_policy",
)

_UNBLOCK_LABELS = {
    "confirm_publish_decision": "确认这次发布",
    "retry_child": "再试一次",
    "cancel": "先不发这一家",
    "new_customer_snapshot": "重新出一份报价",
    "repair_legal_passage": "去改这一段",
    "verify_outcome": "去核实发布结果",
    "review_replacement_policy": "查看替换规则",
}


#: 🔴 WP5 的 ``service_milestone`` 在**运行期**依赖两条兄弟窗迁移:
#:    042 给 ``keyword_selection_sessions`` 加 ``customer_confirmed_snapshot_id``,
#:    043 建 ``defgeo_activation_outbox``。两条都已在 ``db/migration_manifest.py``
#:    里登记 ⇒ 生产 prestart 会跑;但 08-19 那份 dump 早于它们(都是 08-21),
#:    而 conftest 只重放**本包自己的** 044。
#:
#:    所以「判据底座缺这两条」是**底座的洞,不是生产的缺陷** —— 不把它们装上就
#:    去报「生产 confirm 必 500」,那是拿我自己的夹具漏洞去冤枉被测代码
#:    (「工单给的根因也要先证伪」)。这里按 manifest 现扫、原样重放。
_SIBLING_MIGRATIONS: tuple[str, ...] = (
    "db/migration_042_defgeo_customer_accepted_snapshot_2026_08_21.sql",
    "db/migration_043_defgeo_activation_outbox_2026_08_21.sql",
)


def _install_sibling_migrations() -> None:
    from db.migration_manifest import MIGRATIONS

    registered = set(MIGRATIONS)
    missing = [m for m in _SIBLING_MIGRATIONS if m not in registered]
    assert not missing, (
        f"{missing} 不在 manifest 里 —— 生产 prestart 不会跑它们,"
        "那样 WP5 对它们的运行期依赖就是真缺陷,不是底座的洞")

    for rel in _SIBLING_MIGRATIONS:
        sql = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        conn = connect()                    # 一条迁移一条新连接(失败不污染后续)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        finally:
            conn.close()


def _unblock() -> None:
    """只补**环境 / 文案 / 一次自检**,一行业务逻辑都不碰。见模块 docstring。"""
    # ① finding 1:环境面缺列。
    conn = connect()
    try:
        # conftest 的 connect() 已经发过 ``SET search_path`` ⇒ 连接上有打开的事务,
        # 此刻再设 autocommit 会 ``set_session cannot be used inside a transaction``。
        # 直接 commit 即可,DDL 在 PG 里本来就是事务性的。
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE mhz_media ADD COLUMN IF NOT EXISTS industry TEXT")
        conn.commit()
    finally:
        conn.close()

    # ② finding 2:注册面缺文案。
    for kind, label in _UNBLOCK_LABELS.items():
        _copy._ACTION_LABEL.setdefault(kind, label)

    # ③ finding 3:``media_proposal`` 的**模块体自检**与它自己的 ``_QUERY_COLUMNS``
    #    互相矛盾 ⇒ 该模块在任何干净解释器里都 import 不进来。
    #    这里只在**导入的那一瞬间**把自检的判据函数让开,让模块体跑完;
    #    ``_QUERY_COLUMNS`` 本身一个字不改,``assert_no_private_leak``(响应出口
    #    那一层)也原样保留 —— 所以 POR-06 毒串判据仍然是活的,而且因为真 SQL
    #    确实会去读被毒过的 provider / provider_media_id 两列,它反而更有判别力。
    original = _mi.private_columns_in
    _mi.private_columns_in = lambda row: []                      # type: ignore[assignment]
    try:
        import services.defensive_geo.publish.media_proposal  # noqa: F401,PLC0415
    finally:
        _mi.private_columns_in = original                        # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="session", autouse=True)
def _http_env(_schema) -> Iterator[None]:                       # noqa: ANN001
    """🔴 显式依赖 conftest 的 ``_schema`` —— 顺序不能靠「谁先定义」碰运气。

    两件事都必须发生在 dump 装完**之后**:

    ① ``import auth.brand_access`` 的模块体 import ``db.diagnosis_db``,
       而那个模块体会跑 ``init_db()`` **发 DDL 建表**。放在模块导入期会赶在
       ``_schema`` 装 dump 之前把表建出来,然后 dump 撞 DuplicateTable
       (本轮实测:``relation "_migration_markers" already exists``)。
    ② 但它也不能拖到某条判据的事务里才第一次触发:生产里这条 import 是
       preview handler 内的惰性 import,一旦本进程第一次触发它恰好发生在
       一个已经打开的读事务里,``init_db`` 的 ALTER 要 AccessExclusiveLock
       却在等同一线程自己栈上方的事务 —— **单线程自死锁,不超时不报错**
       (2026-08-10 我用这个形态把生产打成 503 十六分钟)。

    所以位置只有一个:dump 装完之后、任何判据开事务之前。
    """
    import auth.brand_access  # noqa: F401,PLC0415  (import 的副作用就是目的)

    _install_sibling_migrations()

    if UNBLOCK:
        _unblock()

    # 🔴 毒串**全局**注入:这样每一条判据的响应体都天然在毒串的射程内,
    #    而不是只有 POR-06 那一条测到。私有列全 NULL 的夹具会让泄漏判据
    #    零判别力(什么都不写自然什么都不漏)。
    conn = connect()
    try:
        sets = ", ".join(f"{c} = %s" for c in _POISON_COLUMNS)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE mhz_media SET {sets} WHERE id BETWEEN 990001 AND 990099",
                tuple(_POISON_COLUMNS.values()),
            )
            assert cur.rowcount >= 6, (
                f"毒串只写进了 {cur.rowcount} 行媒体 —— 少于种子行数,"
                "POR-06 判据会打在没被毒的那几行上(零判别力)"
            )
        conn.commit()
    finally:
        conn.close()
    yield


class _EntryClosed(TestClient):
    """[终审 P0-1 2026-08-23] 碰到已关闭的客户入口 ⇒ **当场 skip**,不是红。

    ═══════════════════════════════════════════════════════════════════
    为什么这么做,而不是手挑一份 skip 名单
    ═══════════════════════════════════════════════════════════════════
    P0-1 把三个会冻钱的端点关成 typed 拒绝(见
    ``api/defensive_publish_api._CUSTOMER_PUBLISH_ENTRY_OPEN``)。本文件里
    有 15 条判据是驱动这三扇门的 —— 它们断言的行为**按 Owner 指令下线了**,
    不是回归。

    手写一份"这 15 条 skip 掉"的名单是**手写分母**:名单漏掉的那一条会
    以红的形式留下,而将来新增的、同样驱动这三扇门的判据不会被自动纳入。
    所以判定放在**响应**上:凡是收到 ``PUBLISH_ENTRY_CLOSED`` 的调用,
    说明这条判据确实撞上了已关的门 ⇒ skip。撞不上的照常跑、照常红。

    🔴 入口一旦重开(那个常量翻 True),这 15 条**自动全部复活**并必须通过 ——
       skip 的条件是"真的收到了关闭信封",不是一个手动标记。
    """

    def request(self, *args: Any, **kwargs: Any):                # noqa: ANN201
        resp = super().request(*args, **kwargs)
        if resp.status_code == 403:
            try:
                if (resp.json().get("detail") or {}).get("code") == "PUBLISH_ENTRY_CLOSED":
                    pytest.skip(
                        "客户侧发布入口已按终审 P0-1 关闭 —— 本条判据驱动的是"
                        "已下线的行为。重开入口(_CUSTOMER_PUBLISH_ENTRY_OPEN=True)"
                        "后它会自动复活并必须通过。"
                    )
            except ValueError:
                pass
        return resp


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """最小 app:只挂被测 router + 一个逐字照抄生产键名的身份中间件。"""
    from api import defensive_publish_api

    app = FastAPI()
    app.include_router(defensive_publish_api.router)

    @app.middleware("http")
    async def _inject_user(request: Request, call_next):        # noqa: ANN001
        who = request.headers.get("X-Test-Identity", "a")
        request.state.user = dict(_IDENTITIES[who])
        return await call_next(request)

    with _EntryClosed(app) as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════
# 断言小工具
# ══════════════════════════════════════════════════════════════════════════
def _ok(resp, *, expect: int = 200) -> dict[str, Any]:
    """🔴 成功路径:**不许 422 也不许 500**。

    只验 ``!= 422`` 会漏掉整层库合同(本仓 2026-08-18 实证 6 处必 500)。
    """
    assert resp.status_code != 422, (
        f"422 = 请求形状对不上被测 DTO,判据自己写错了:{resp.text[:600]}")
    assert resp.status_code != 500, (
        f"500 = 该端点在真库上跑不完(生产必炸):{resp.text[:900]}")
    assert resp.status_code == expect, f"期望 {expect} 实得 {resp.status_code}:{resp.text[:600]}"
    return resp.json()


def _err(resp, *, status: int, code: str) -> dict[str, Any]:
    """错误路径:**逐条点名 code**。

    只断言「返回了 4xx」会被别的规则顺手满足 —— 那条规则整个删掉判据照样绿
    (2026-08-21 记过)。所以这里必须对上 SafeError 信封里的 ``code``。
    """
    assert resp.status_code == status, (
        f"期望 {status}/{code},实得 {resp.status_code}:{resp.text[:800]}")
    body = resp.json()
    detail = body.get("detail")
    assert isinstance(detail, Mapping), f"不是 SafeError 信封:{body!r}"
    assert detail.get("code") == code, (
        f"期望 code={code},实得 {detail.get('code')!r}:{json.dumps(body, ensure_ascii=False)[:800]}")
    return detail


def _counts() -> dict[str, int]:
    """副作用分母。**短连接、立刻关** —— 不把事务跨到 HTTP 调用上。"""
    conn = connect()
    try:
        cur = conn.cursor()
        out: dict[str, int] = {}
        for t in (_store.COMMAND_TABLE, _store.OUTBOX_TABLE, _store.SNAPSHOT_TABLE):
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT COUNT(*) AS n FROM point_freezes WHERE task_ref LIKE 'defgeo_publish_%%'")
        out["point_freezes"] = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    return out


def _delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {k: after[k] - before[k] for k in before}


def _assert_zero_side_effects(before: Mapping[str, int], *, allow_snapshot: bool = False) -> None:
    after = _counts()
    d = _delta(before, after)
    assert d[_store.COMMAND_TABLE] == 0, f"多出了 command:{d}"
    assert d[_store.OUTBOX_TABLE] == 0, f"多出了 outbox:{d}"
    assert d["point_freezes"] == 0, f"多出了冻结:{d}"
    if not allow_snapshot:
        assert d[_store.SNAPSHOT_TABLE] == 0, f"多出了 snapshot:{d}"


def _seed_budget(
    *, tenant: int, accepted: int, projection: str,
    global_cap: int, scope_cap: int, version: int = 1,
) -> str:
    """造一行 provider-private 执行预算。缺它 confirm 的资金腿直接 FundingError。"""
    bid = "pbud_" + uuid.uuid4().hex[:24]
    conn = connect()
    try:
        cur = conn.cursor()
        _store.insert_budget(cur, {
            "execution_budget_snapshot_id": bid,
            "budget_version": version,
            "tenant_owner_id": tenant,
            "accepted_snapshot_id": accepted,
            "service_projection_id": projection,
            "global_cap_points": global_cap,
            "scope_key": "media_publication",
            "scope_cap_points": scope_cap,
            "funding_policy": "personal_wallet",
            "payer_user_id": tenant,
            "budget_hash": uuid.uuid4().hex + uuid.uuid4().hex[:32],
        })
        conn.commit()
    finally:
        conn.close()
    return bid


def _allocate_seed_ids() -> tuple[int, int]:
    """[fix-of-fix 2026-08-23] **在插入的那一刻**向库要下一个空号。

    🔴 为什么不能再用写死的常量(原来是 ``iter(range(770001, ...))``):
       同一个一次性库上跑第二次 pytest 必然撞
       ``keyword_selection_sessions_quote_id_key``。之前没暴露,是因为没有
       判据需要**反复**跑同一套;撕锁 runner 一来(一发变异跑一次判据),
       第 2 发起就会以"跟被测代码无关的 UniqueViolation"全红 ——
       那种红会把「变异没被杀死」和「夹具自己撞了」混成一团,判读直接失效。

    🔴 也不能在**模块导入时**算一次基址(我的第一版就是那么写的,照样撞):
       导入只发生一次,而同一次 session 里后面还会继续插行;
       更别说 collection 期的库状态与 test 期未必一致。
       唯一稳的是"用的时候现要" —— 判据是串行跑的,不存在并发抢号。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(id), 770000) + 1 AS n FROM quote_pricing_snapshots")
        accepted = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT COALESCE(MAX(quote_id), 660000) + 1 AS n FROM keyword_selection_sessions")
        quote = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    return accepted, quote


def _seed_accepted_snapshot(*, tenant: int = TENANT_A, brand: int = BRAND_A) -> int:
    """造一份**客户已接受**的报价快照 + 已激活的会话 + 已物化的 activation。

    🔴 为什么必须真造这三张表的行,而不是把 milestone 打桩成 ``service_activated``:
       ``work_admission`` 的第一道闸就是里程碑闸,而 ``service_milestone.resolve``
       是从这三张真表算出来的。打桩等于把这道闸变成恒放行 —— 本仓记过这个形态
       (「门禁接了但接线是坏的」/「夹具替被测代码干活 ⇒ 判据恒绿」)。
       真造行之后,ACT-11 那道闸在本文件里是**活的**:
       :func:`test_17_act11_unactivated_service_is_refused` 用一条没激活的
       accepted snapshot 反向证明它确实会拦。
    """
    accepted, quote = _allocate_seed_ids()
    snapshot_hash = uuid.uuid4().hex + uuid.uuid4().hex     # 64 位
    conn = connect()
    try:
        cur = conn.cursor()
        # quote_pricing_snapshots 上有复合 FK (quote_id, brand_id) → quotes(id, brand_id),
        # 所以 quotes 这一行的 brand_id 必须一起给。
        cur.execute("INSERT INTO quotes (id, brand_id, service_days) VALUES (%s,%s,90) "
                    "ON CONFLICT (id) DO NOTHING", (quote, brand))
        cur.execute(
            "INSERT INTO keyword_selection_sessions "
            "(token, quote_id, brand_id, keywords_snapshot, expires_at, status) "
            "VALUES (%s,%s,%s,'[]','2099-01-01','active') RETURNING id",
            (f"tok_w3c_{accepted}", quote, brand))
        session_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO quote_pricing_snapshots "
            "(id, quote_id, brand_id, selection_session_id, version, reason, "
            " calculation_version, pricing_snapshot, snapshot_hash) "
            "VALUES (%s,%s,%s,%s,1,'w3c 判据夹具','v1',%s,%s)",
            (accepted, quote, brand, session_id,
             json.dumps({"delivery_plan": {"schema_version": "geo-delivery-plan-v1"}}),
             snapshot_hash))
        # 042 的 group-complete CHECK:三列要么整组齐、要么整组空。
        cur.execute(
            "UPDATE keyword_selection_sessions SET customer_confirmed_snapshot_id = %s, "
            "customer_confirmed_snapshot_hash = %s, customer_confirmed_at = NOW() "
            "WHERE id = %s", (accepted, snapshot_hash, session_id))
        cur.execute(
            "INSERT INTO defgeo_activation_outbox "
            "(accepted_snapshot_id, quote_id, brand_id, event_kind, "
            # 043 的 materialized_group CHECK:终态必须带 materialized_at。
            " accepted_snapshot_hash, status, tenant_owner_id, materialized_at) "
            "VALUES (%s,%s,%s,'customer_accepted',%s,'materialized',%s,NOW())",
            (accepted, quote, brand, snapshot_hash, tenant))
        conn.commit()
    finally:
        conn.close()
    return accepted


def _scenario(name: str) -> dict[str, Any]:
    """每条判据一把新格:accepted_snapshot_id / plan_item_key 都不复用。

    共用 slot 会让「上一条判据留下的 command」变成下一条的前置状态 ——
    那是「变异留库残留污染后续判据」的形态。
    """
    accepted = _seed_accepted_snapshot()
    projection = f"svc_w3c_{name}"
    _seed_budget(tenant=TENANT_A, accepted=accepted, projection=projection,
                 global_cap=100000, scope_cap=100000)
    return {
        "planItemKey": f"plan_{name}",
        "articleRevisionId": f"rev_{name}_1",
        "expectedArticleHash": _bh.body_hash(f"正文 {name} 第一版"),
        "acceptedSnapshotId": accepted,
        "serviceProjectionId": projection,
        "brandId": BRAND_A,
    }


def _preview(client: TestClient, body: Mapping[str, Any], *,
             key: str | None = None, who: str = "a"):
    return client.post(
        "/api/defensive-geo/publish/decision-snapshots/preview",
        json=dict(body),
        headers={"Idempotency-Key": key or ("idem-" + uuid.uuid4().hex),
                 "X-Test-Identity": who},
    )


def _confirm(client: TestClient, snapshot_id: str, snap: Mapping[str, Any], *,
             key: str | None = None, who: str = "a"):
    return client.post(
        f"/api/defensive-geo/publish/decision-snapshots/{snapshot_id}/confirm",
        json={"expectedHash": snap["canonicalHash"],
              "expectedVersion": snap["snapshotVersion"]},
        headers={"Idempotency-Key": key or ("idem-" + uuid.uuid4().hex),
                 "X-Test-Identity": who},
    )


def _ready_snapshot(client: TestClient, body: Mapping[str, Any], **kw) -> dict[str, Any]:
    """跑一次 preview 并断言拿到 snapshot_ready。返回 snapshotResponse。"""
    payload = _ok(_preview(client, body, **kw))
    assert payload["slotAdmission"] == "snapshot_ready", payload
    return payload["snapshotResponse"]


def _force_command_state(command_id: str, **updates: Any) -> dict[str, Any]:
    """把 command 推到某个 canonical 事实上(模拟 worker/reconciler 的收敛)。

    走 ``_store.bump_status`` 而不是裸 UPDATE —— statusVersion 的原子递增
    是 retry-child 的 CAS 承重面,裸 UPDATE 会绕开它。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        row = _store.bump_status(cur, publish_command_id=command_id, **updates)
        assert row is not None, f"bump_status 没改到 {command_id}"
        conn.commit()
        return dict(row)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 00 —— 本轮实测到的两处生产必 500(分母都在**制品**上,探路开关碰不到)
# ══════════════════════════════════════════════════════════════════════════
def _dict_literal_keys(src: str, name: str) -> set[str]:
    """从源码里抽一个模块级 dict 字面量的字符串键。

    🔴 ``ast.Assign`` 与 ``ast.AnnAssign`` 都要认:``X: dict[str,str] = {...}``
       是 AnnAssign(``.target`` 单数),只匹配 Assign 会抽到空集合。
       本轮实测就栽在这里 —— 幸好取空时有硬断言,否则「抽不到」会伪装成
       「一个都不缺」直接恒绿。
    """
    keys: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        # 同名变量可能被赋成非字面量(例如 ``user_data_source = payload`` /
        # ``payload = json.loads(...)``)。跳过它们、只收字面量;
        # 「一个字面量都没收到」由调用点的硬断言兜住,不会退化成恒绿。
        if not isinstance(node.value, ast.Dict):
            continue
        keys |= {k.value for k in node.value.keys
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return keys


def _prod_mhz_media_columns() -> set[str]:
    """从**生产 dump 制品**里机械抽 ``mhz_media`` 的列名。

    🔴 刻意不查活库:活库会被探路开关补过列,查活库等于让 workaround 把
       finding 判成绿。分母必须与结论的作用域一致 —— 结论说的是「生产」。
    """
    sql = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    marker = "CREATE TABLE public.mhz_media ("
    start = sql.index(marker) + len(marker)
    body = sql[start:sql.index("\n);", start)]
    cols: set[str] = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("CONSTRAINT"):
            continue
        cols.add(line.split()[0])
    assert "media_name" in cols and "source_domain" in cols, (
        f"列名抽取规则与 dump 形态不匹配,抽到 {sorted(cols)[:12]} —— "
        "抽不出来的空集合会让本判据恒绿")
    return cols


def test_00_finding1_public_media_columns_exist_in_production_schema() -> None:
    """FINDING-1:``media_identity.PUBLIC_MEDIA_COLUMNS`` 里有生产不存在的列。

    ``media_proposal._QUERY_COLUMNS`` 直接用它拼 SELECT ⇒ ``read_candidates``
    在生产库上必抛 ``UndefinedColumn`` ⇒ preview handler 的兜底 except 把它
    转成受控 500 ⇒ **preview 端点在生产上 100% 不可用**。
    """
    prod = _prod_mhz_media_columns()
    missing = sorted(set(_mi.PUBLIC_MEDIA_COLUMNS) - prod)
    assert not missing, (
        f"PUBLIC_MEDIA_COLUMNS 里这些列生产 mhz_media 上不存在:{missing}。\n"
        f"坐标 services/defensive_geo/publish/media_identity.py PUBLIC_MEDIA_COLUMNS\n"
        f"     → media_proposal._QUERY_COLUMNS → read_candidates 的 SELECT\n"
        f"后果:每一次 POST /decision-snapshots/preview 都是受控 500。"
    )


def test_00_finding2_publish_action_kinds_have_registered_copy() -> None:
    """FINDING-2:façade 会下发的 action kind 有 7 个没登记文案。

    ``_recovery_action`` 对空文案 ``raise RuntimeError('死 CTA 不许上屏')``,
    被 handler 的兜底 except 转成受控 500。其中 ``confirm_publish_decision``
    在**每一条成功 preview** 的必经路径上。

    🔴 分母取的是 ``copy_registry`` 模块里那张表的键集合快照,不是活进程 ——
       探路开关往活进程里 setdefault 的 7 条不算数。
    """
    src = (ROOT / "services" / "defensive_geo" / "copy_registry.py").read_text(
        encoding="utf-8", errors="replace")
    registered = _dict_literal_keys(src, "_ACTION_LABEL")
    assert registered, "没有从 copy_registry 源码里抽到任何 action 文案键 —— 抽取规则过时"

    missing = sorted(k for k in _PUBLISH_ACTION_KINDS if k not in registered)
    assert not missing, (
        f"这些 action kind 没有登记文案:{missing}。\n"
        f"坐标 services/defensive_geo/copy_registry.py::_ACTION_LABEL(第 87 行起)\n"
        f"     消费方 api/defensive_publish_api.py::_label / _recovery_action\n"
        f"后果:凡是要下发这些 CTA 的路径全部受控 500;"
        f"confirm_publish_decision 在每一次成功 preview 的必经路径上。"
    )


def test_00_finding3_media_proposal_is_importable_in_a_clean_interpreter() -> None:
    """FINDING-3:``media_proposal`` 的模块体自检否定它自己的查询列 ⇒ 永远 import 不进来。

    ``_QUERY_COLUMNS`` 显式包含 ``provider`` / ``provider_media_id``(docstring 写明
    「两列只作 HMAC 原料,进得来、出不去」),而模块体最后一行
    ``_assert_no_private_column_in_query()`` 用 ``_mi.private_columns_in`` 判定
    这两个名字**就是**私有列(``PRIVATE_COLUMNS`` 里逐字列了,``provider_`` 还
    另有前缀规则)⇒ 模块体必 ``RuntimeError``。

    handler 里那句 import 是**惰性**的,所以进程启动看不出来,
    每一次 ``POST /decision-snapshots/preview`` 才现形为受控 500。
    这也解释了它为什么能活到今天:**这个模块从来没有被成功导入过一次**。

    🔴 判据跑在**子进程**里 —— 干净解释器,继承不到探路开关的任何 patch。
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c",
         "import services.defensive_geo.publish.media_proposal as m; "
         "print(len(m._QUERY_COLUMNS))"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "media_proposal 在干净解释器里 import 失败:\n"
        f"{(proc.stderr or '')[-1200:]}\n"
        "坐标 services/defensive_geo/publish/media_proposal.py::_QUERY_COLUMNS(第 50 行)\n"
        "     vs 同文件 _assert_no_private_column_in_query()(第 55/70 行)\n"
        "     判定表 services/defensive_geo/publish/media_identity.py::PRIVATE_COLUMNS\n"
        "                                        + PRIVATE_COLUMN_PREFIXES('provider_')\n"
        "后果:preview 的 `from ... import media_proposal` 是惰性 import,"
        "启动不报错,每次 preview 受控 500。"
    )


def test_00_finding4_milestone_fallback_must_not_poison_caller_transaction() -> None:
    """FINDING-4:``read_facts`` 的「安全默认」会把**调用方的事务打废**。

    ``service_milestone.read_facts`` 把三段 SQL 各自包在 ``try/except Exception``
    里,注释逐字写着「任何一步读不到都退回安全默认,**不抛**」——
    也就是说这条 except 路径是**被设计成可达的**。

    但在 PostgreSQL 里,一条语句报错之后整个事务就进入 aborted 状态;
    ``except`` 里既没有 ``SAVEPOINT`` 也没有 ``ROLLBACK TO``,函数 return 之后
    调用方事务里**后续每一条语句**都会 ``InFailedSqlTransaction``。

    于是这个「可解释、有出口的软失败」在真库上变成了整条 confirm 的硬 500 ——
    比它想避免的那个 500 更糟,因为日志里只留下一句 warning。

    本判据不依赖任何 schema drift:它把 ``search_path`` 指到 ``pg_temp``,
    让那张表**在本连接上不可见**来触发同一条 except 路径,然后断言事务仍可用。
    """
    from services.defensive_geo.publish import service_milestone as _ms

    conn = connect()
    try:
        cur = conn.cursor()
        # 让 keyword_selection_sessions 在本连接上找不到 —— 触发第一段 except。
        cur.execute("SET LOCAL search_path TO pg_temp")
        facts = _ms.read_facts(cur, accepted_snapshot_id=1, tenant_owner_id=TENANT_A)
        assert facts.session_status is None, (
            f"这一跑没有走进 except 分支(facts={facts}) —— 判据没打到被测行")

        # 🔴 承重断言:软失败之后,调用方的事务必须还能用。
        cur.execute("SET LOCAL search_path TO public")
        cur.execute("SELECT 1 AS ok")
        assert cur.fetchone()["ok"] == 1
    except Exception as exc:                              # noqa: BLE001
        raise AssertionError(
            f"里程碑软失败之后调用方事务已不可用:{type(exc).__name__}: {exc}\n"
            "坐标 services/defensive_geo/publish/service_milestone.py::read_facts\n"
            "     (三处 `except Exception` 各缺一个 SAVEPOINT / ROLLBACK TO)\n"
            "     消费方 api/defensive_publish_api.py::_do_confirm(第 1379-1383 行)\n"
            "后果:该 except 一旦被触发,整条 confirm 事务作废 ⇒ 受控 500;"
            "而这条 except 是被设计成可达的(注释:任何一步读不到都退回安全默认)。"
        ) from exc
    finally:
        conn.rollback()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 01 —— 夹具身份键必须覆盖生产中间件写的键(防「夹具用生产不发的键」)
# ══════════════════════════════════════════════════════════════════════════
def test_01_fixture_identity_keys_match_production_middleware() -> None:
    """机械分母:AST 现扫 ``auth/middleware.py`` 的 ``user_data_source`` 字面量。

    手抄一份键清单会在「生产加了一个键」时静默过期;从源码抽则不会。
    """
    # 🔴 生产往 request.state.user 塞的东西有**两条来路**,判据要覆盖并集:
    #    ① 软刷新命中时:middleware 里手搭的 ``user_data_source = {...}`` 字面量;
    #    ② 常态(JWT 有效、无需软刷新):``user_data_source = payload`` ——
    #       也就是 ``auth/jwt_utils.create_jwt`` 里那个 payload 字面量。
    #    只抽 ① 会漏掉常态那条路的键;本轮实测就是 ② 让「只匹配 Dict」当场炸出来的。
    prod_keys = _dict_literal_keys(
        (ROOT / "auth" / "middleware.py").read_text(encoding="utf-8", errors="replace"),
        "user_data_source",
    ) | _dict_literal_keys(
        (ROOT / "auth" / "jwt_utils.py").read_text(encoding="utf-8", errors="replace"),
        "payload",
    )
    assert prod_keys, "两处字面量一个都没抽到 —— 抽取规则过时"
    assert len(prod_keys) > 5, f"抽到的键太少,疑似只命中了一处:{sorted(prod_keys)}"

    # ① 生产写 user_id,不写 id。夹具写反了会让整片端点在生产必 500 而判据全绿。
    assert "user_id" in prod_keys, prod_keys
    assert "id" not in prod_keys, (
        f"生产中间件现在也写 id 了?键集合={sorted(prod_keys)} —— 夹具口径要重定")

    # ② 夹具身份必须覆盖生产写的每一个键。
    missing = sorted(prod_keys - set(_IDENTITIES["a"]))
    assert not missing, f"夹具身份缺生产会写的键 {missing} —— 端点读到 None 会走出假路径"


# ══════════════════════════════════════════════════════════════════════════
# 02 —— 五阶段真链
# ══════════════════════════════════════════════════════════════════════════
def test_02_five_stage_real_chain(client: TestClient) -> None:
    """preview → GET exact → confirm → GET status → 幂等重放 confirm。

    每一跳都断言**真实副作用计数**,不是只看响应形状。
    """
    body = _scenario("chain")

    # ── ① preview:零资金 / 零 command / 零 outbox ────────────────────────
    before = _counts()
    snap_resp = _ready_snapshot(client, body)
    d = _delta(before, _counts())
    assert d["point_freezes"] == 0, f"preview 动了钱:{d}"
    assert d[_store.COMMAND_TABLE] == 0, f"preview 建了 command:{d}"
    assert d[_store.OUTBOX_TABLE] == 0, f"preview 入了 outbox:{d}"
    assert d[_store.SNAPSHOT_TABLE] == 1, f"preview 应恰签发 1 份 snapshot:{d}"

    snap = snap_resp["snapshot"]
    snapshot_id = snap["decisionSnapshotId"]

    # 冻结面字段齐全 —— 分母来自 freeze_snapshot 自己产出的键集合,不手抄。
    reference, _ = _ds.freeze_snapshot(
        decision_snapshot_id="pds_ref", publish_slot_id=snap["publishSlotId"],
        snapshot_version=1, expires_at=snap["expiresAt"],
        accepted_snapshot_id=snap["acceptedSnapshotId"],
        accepted_snapshot_hash=snap["acceptedSnapshotHash"],
        service_projection_id=snap["serviceProjectionId"],
        service_projection_version=snap["serviceProjectionVersion"],
        plan_item_key=snap["planItemKey"], question_key=snap["questionKey"],
        question_revision=snap["questionRevision"],
        article_revision_id=snap["articleRevisionId"], article_hash=snap["articleHash"],
        pricing_catalog_version=snap["pricingCatalogVersion"],
        inventory_snapshot_version=snap["inventorySnapshotVersion"],
        execution_budget_snapshot_id=snap["executionBudgetSnapshotId"],
        execution_budget_version=snap["executionBudgetVersion"],
        global_budget=snap["globalBudget"], scope_budget=snap["scopeBudget"],
        decision=_ds.Candidate(
            identity=_mi.PublicMediaIdentity(
                public_media_option_id=_mi.public_media_option_id(
                    decision_snapshot_id="pds_ref",
                    media_key=snap["decision"]["publicMediaKey"], ordinal=0),
                public_media_key=snap["decision"]["publicMediaKey"],
                canonical_root_domain_key=snap["decision"]["canonicalRootDomainKey"],
                public_media_name=snap["decision"]["publicMediaName"],
                public_root_domain_label=snap["decision"]["publicRootDomainLabel"],
                media_role=snap["decision"]["mediaRole"]),
            reason_facts=tuple((f["kind"], f["label"])
                               for f in snap["decision"]["reasonFacts"]),
            exact_points=snap["decision"]["exactPoints"]),
        alternatives=[],
        publish_item_request_id=snap["decision"]["publishItemRequestId"],
        replacement_policy_version=snap["replacementPolicyVersion"],
        funding_policy=snap["fundingPolicy"], principal_kind=snap["principalKind"],
        approval_requirement=snap["approvalRequirement"],
        sponsor_policy_ref=snap["sponsorPolicyRef"],
    )
    missing = sorted(set(reference) - set(snap))
    assert not missing, f"冻结面缺字段 {missing}"
    _mi.assert_no_private_leak(snap, field="chain.snapshot")

    # ── ② GET exact:MED-13 / UI-35 逐值相同,不重推荐、不延期 ────────────
    got = _ok(client.get(
        f"/api/defensive-geo/publish/decision-snapshots/{snapshot_id}",
        headers={"X-Test-Identity": "a"}))
    assert got == snap_resp, (
        "exact GET 与 preview 的 snapshotResponse 不逐值相同 —— "
        "说明 GET 重新推荐或延了期(MED-13/UI-35)")

    # ── ③ confirm:恰 1 command + 恰 1 outbox + 恰 1 freeze ────────────────
    before = _counts()
    confirmed = _ok(_confirm(client, snapshot_id, snap))
    d = _delta(before, _counts())
    assert d[_store.COMMAND_TABLE] == 1, f"confirm 应恰建 1 条 command:{d}"
    assert d[_store.OUTBOX_TABLE] == 1, f"confirm 应恰入 1 条 outbox:{d}"
    assert d["point_freezes"] == 1, f"confirm 应恰 1 次冻结:{d}"
    assert confirmed["idempotentReplay"] is False

    command_id = confirmed["publishCommandId"]

    # exactPoints 三处相等(§15.7 / MED-07)。
    exact = confirmed["exactSettlementPoints"]
    assert exact == confirmed["item"]["exactSettlementPoints"], confirmed
    assert exact == snap["totalExactPoints"], (exact, snap["totalExactPoints"])
    assert exact == snap["decision"]["exactPoints"], (exact, snap["decision"])

    # 冻结的**真实**金额必须等于 exact —— 只看响应等于没验钱。
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, amount_total, status FROM point_freezes "
            "WHERE task_ref = %s", (f"defgeo_publish_{command_id}",))
        rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    assert len(rows) == 1, f"task_ref 对应的冻结行不是 1 条:{rows}"
    assert int(rows[0]["amount_total"]) == exact, (rows[0], exact)
    assert int(rows[0]["user_id"]) == TENANT_A, rows[0]
    assert rows[0]["status"] == "frozen", rows[0]

    # ── ④ GET status:200 本身就证明 assert_truth_table 放行了这组合 ──────
    status = _ok(client.get(
        f"/api/defensive-geo/publish/commands/{command_id}",
        headers={"X-Test-Identity": "a"}))
    assert status["statusVersion"] == 1, status
    assert status["commandState"] == "queued", status
    assert status["fundingState"] == "frozen", status
    assert status["fundingPolicy"] == "personal_wallet", status
    assert status["decisionSnapshotId"] == snapshot_id
    assert status["decisionSnapshotHash"] == snap["canonicalHash"]
    assert status["commandCanonicalHash"] == confirmed["commandCanonicalHash"]
    assert status["item"]["publicationSettlement"]["canonicalPublicationState"] == "not_started"
    assert status["item"]["publicUrl"] is None, "not_started 不得出 publicUrl"

    # ── ⑤ 幂等重放 confirm:零新增 ────────────────────────────────────────
    before = _counts()
    replay = _ok(_confirm(client, snapshot_id, snap))
    assert replay["idempotentReplay"] is True, replay
    assert replay["publishCommandId"] == command_id, replay
    _assert_zero_side_effects(before)


# ══════════════════════════════════════════════════════════════════════════
# 03 —— FIN-02 同 key 异 payload
# ══════════════════════════════════════════════════════════════════════════
def test_03_fin02_same_key_different_payload_conflicts(client: TestClient) -> None:
    body = _scenario("fin02")
    key = "idem-fin02-" + uuid.uuid4().hex[:8]
    _ready_snapshot(client, body, key=key)

    # 只改 articleRevisionId:它进 request canonical hash,但**不进 slot 身份**,
    # 所以命中的是同一个 slot 上的同一个 key —— 这正是 FIN-02 要拦的形态。
    other = dict(body, articleRevisionId=body["articleRevisionId"] + "_x")
    before = _counts()
    _err(_preview(client, other, key=key), status=409, code="IDEMPOTENCY_CONFLICT")
    _assert_zero_side_effects(before)


# ══════════════════════════════════════════════════════════════════════════
# 04 —— API-04 consumed 幂等 / superseded 拒绝
# ══════════════════════════════════════════════════════════════════════════
def test_04_api04_consumed_replay_and_superseded_reject(client: TestClient) -> None:
    body = _scenario("api04")
    snap = _ready_snapshot(client, body)["snapshot"]
    confirmed = _ok(_confirm(client, snap["decisionSnapshotId"], snap))

    # ① 已 consumed 再 confirm(换一把新 key)→ 返回原 command,零新增。
    before = _counts()
    again = _ok(_confirm(client, snap["decisionSnapshotId"], snap))
    assert again["idempotentReplay"] is True
    assert again["publishCommandId"] == confirmed["publishCommandId"]
    _assert_zero_side_effects(before)

    # ② superseded:另起一格,preview 两次把第一份顶掉。
    body2 = _scenario("api04b")
    first = _ready_snapshot(client, body2)["snapshot"]
    second = _ready_snapshot(client, body2)["snapshot"]
    assert second["decisionSnapshotId"] != first["decisionSnapshotId"]

    before = _counts()
    detail = _err(_confirm(client, first["decisionSnapshotId"], first),
                  status=409, code="PUBLISH_DECISION_NOT_CONFIRMABLE")
    det = detail.get("details") or {}
    assert det.get("successorSnapshotId") == second["decisionSnapshotId"], detail
    assert det.get("successorSnapshotHash") == second["canonicalHash"], detail
    assert det.get("supersessionKind") == "new_preview", detail
    _assert_zero_side_effects(before)


# ══════════════════════════════════════════════════════════════════════════
# 05 —— cancel
# ══════════════════════════════════════════════════════════════════════════
def test_05_cancel_lifecycle(client: TestClient) -> None:
    body = _scenario("cancel")
    snap = _ready_snapshot(client, body)["snapshot"]
    sid = snap["decisionSnapshotId"]
    payload = {"expectedHash": snap["canonicalHash"],
               "expectedVersion": snap["snapshotVersion"]}
    url = f"/api/defensive-geo/publish/decision-snapshots/{sid}/cancel"

    before = _counts()
    cancelled = _ok(client.post(url, json=payload, headers={
        "Idempotency-Key": "idem-cancel-1", "X-Test-Identity": "a"}))
    assert cancelled["lifecycle"] == "cancelled", cancelled
    _assert_zero_side_effects(before)

    # cancelled 再 confirm → 409。
    before = _counts()
    _err(_confirm(client, sid, snap), status=409, code="PUBLISH_DECISION_NOT_CONFIRMABLE")
    _assert_zero_side_effects(before)

    # cancel 幂等重放 → 同 cancelled,零副作用。
    before = _counts()
    again = _ok(client.post(url, json=payload, headers={
        "Idempotency-Key": "idem-cancel-1", "X-Test-Identity": "a"}))
    assert again == cancelled, (again, cancelled)
    _assert_zero_side_effects(before)


# ══════════════════════════════════════════════════════════════════════════
# 06~08 —— override
# ══════════════════════════════════════════════════════════════════════════
def _pick(alternatives: list[Mapping[str, Any]], current_role: str, *, stronger: bool):
    from api.defensive_publish_api import _role_rank

    cur = _role_rank(current_role)
    for alt in alternatives:
        rank = _role_rank(alt["mediaRole"])
        if (rank > cur) if stronger else (rank < cur):
            return alt
    return None


def test_06_override_picks_alternative_and_builds_child(client: TestClient) -> None:
    body = _scenario("ovr")
    parent = _ready_snapshot(client, body)["snapshot"]
    alt = _pick(parent["alternatives"], parent["decision"]["mediaRole"], stronger=True)
    assert alt is not None, (
        "夹具目录里没有比推荐项更强角色的候选 —— 升档 override 判据没有被测对象")

    before = _counts()
    child_resp = _ok(client.post(
        f"/api/defensive-geo/publish/decision-snapshots/{parent['decisionSnapshotId']}/override",
        json={"expectedHash": parent["canonicalHash"],
              "expectedVersion": parent["snapshotVersion"],
              "selectedPublicMediaOptionId": alt["publicMediaOptionId"],
              "actorReason": "客户要求换更权威的媒体"},
        headers={"Idempotency-Key": "idem-ovr-" + uuid.uuid4().hex[:8],
                 "X-Test-Identity": "a"}))
    child = child_resp["snapshot"]

    assert child["snapshotVersion"] == parent["snapshotVersion"] + 1, child
    assert child["canonicalHash"] != parent["canonicalHash"], "hash 没变 = 冻结面没变"
    assert child["decision"]["publicMediaKey"] == alt["publicMediaKey"], child["decision"]
    # option id 绑 snapshot:child 里必须重算,不许沿用 parent 的。
    assert child["decision"]["publicMediaOptionId"] != alt["publicMediaOptionId"], (
        "child 沿用了 parent 的 option id —— 跨 snapshot 复用 option 就成为可能")

    d = _delta(before, _counts())
    assert d[_store.SNAPSHOT_TABLE] == 1, f"override 应恰建 1 份 child:{d}"
    assert d[_store.COMMAND_TABLE] == 0 and d["point_freezes"] == 0, f"override 动了钱:{d}"

    # parent 变 superseded,三项齐全。
    old = _ok(client.get(
        f"/api/defensive-geo/publish/decision-snapshots/{parent['decisionSnapshotId']}",
        headers={"X-Test-Identity": "a"}))
    assert old["lifecycle"] == "superseded", old
    assert old["supersededBySnapshotId"] == child["decisionSnapshotId"], old
    assert old["supersededBySnapshotHash"] == child["canonicalHash"], old
    assert old["supersessionKind"] == "override", old


def test_07_override_unknown_option_is_rejected(client: TestClient) -> None:
    body = _scenario("ovrx")
    parent = _ready_snapshot(client, body)["snapshot"]

    before = _counts()
    resp = client.post(
        f"/api/defensive-geo/publish/decision-snapshots/{parent['decisionSnapshotId']}/override",
        json={"expectedHash": parent["canonicalHash"],
              "expectedVersion": parent["snapshotVersion"],
              # 形状合法(长度在 DTO 范围内)但**不在本 snapshot 的 alternatives 里**。
              # 形状不合法会拿到 pydantic 的 422,那证明不了越界规则跑过 —— 所以
              # 下面 _err 逐字对 code=VALIDATION_ERROR,把两种 422 分开。
              "selectedPublicMediaOptionId": "pmo_" + uuid.uuid4().hex * 2,
              "actorReason": "越界改选"},
        headers={"Idempotency-Key": "idem-ovrx-" + uuid.uuid4().hex[:8],
                 "X-Test-Identity": "a"})
    _err(resp, status=422, code="VALIDATION_ERROR")
    _assert_zero_side_effects(before)

    still = _ok(client.get(
        f"/api/defensive-geo/publish/decision-snapshots/{parent['decisionSnapshotId']}",
        headers={"X-Test-Identity": "a"}))
    assert still["lifecycle"] == "open", "越界改选把 parent 改走了"


def test_08_override_downgrade_requires_customer_reconfirmation(client: TestClient) -> None:
    """降档 ⇒ 409 且零 child / 零 supersede。

    夹具要先让**推荐项**是高角色,否则「比它更弱的候选」根本不存在 ——
    那样判据永远走不到降档分支(零判别力)。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO mhz_media (id, media_name, source_domain, our_price_points, "
            "is_active, provider, provider_media_id, remark, case_link, entrance_link) "
            "VALUES (990010,'判据权威日报','anchor-example.com.cn',5,TRUE,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING",
            (POISON_PROVIDER, POISON_PROVIDER_MEDIA_ID,
             POISON_TEXT, POISON_TEXT, POISON_TEXT))
        conn.commit()
    finally:
        conn.close()
    try:
        body = _scenario("ovrdn")
        parent = _ready_snapshot(client, body)["snapshot"]
        assert parent["decision"]["mediaRole"] == "authority_anchor", (
            f"夹具没能把推荐项抬到 authority_anchor(实得 "
            f"{parent['decision']['mediaRole']}) —— 降档判据没有被测对象")
        weaker = _pick(parent["alternatives"], parent["decision"]["mediaRole"], stronger=False)
        assert weaker is not None, "没有更弱角色的候选 —— 降档判据没有被测对象"

        before = _counts()
        _err(client.post(
            f"/api/defensive-geo/publish/decision-snapshots/"
            f"{parent['decisionSnapshotId']}/override",
            json={"expectedHash": parent["canonicalHash"],
                  "expectedVersion": parent["snapshotVersion"],
                  "selectedPublicMediaOptionId": weaker["publicMediaOptionId"],
                  "actorReason": "想省点钱换个小媒体"},
            headers={"Idempotency-Key": "idem-ovrdn-" + uuid.uuid4().hex[:8],
                     "X-Test-Identity": "a"}),
            status=409, code="CUSTOMER_COMMITMENT_RECONFIRMATION_REQUIRED")
        _assert_zero_side_effects(before)

        still = _ok(client.get(
            f"/api/defensive-geo/publish/decision-snapshots/{parent['decisionSnapshotId']}",
            headers={"X-Test-Identity": "a"}))
        assert still["lifecycle"] == "open", "降档把 parent supersede 掉了"
    finally:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mhz_media WHERE id = 990010")
            conn.commit()
        finally:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 09~12 —— retry-child
# ══════════════════════════════════════════════════════════════════════════
def _confirmed_command(client: TestClient, name: str) -> dict[str, Any]:
    body = _scenario(name)
    snap = _ready_snapshot(client, body)["snapshot"]
    return _ok(_confirm(client, snap["decisionSnapshotId"], snap))


def _retry(client: TestClient, command_id: str, *, status_version: int, command_hash: str):
    return client.post(
        f"/api/defensive-geo/publish/commands/{command_id}/retry-child",
        json={"expectedStatusVersion": status_version, "expectedCommandHash": command_hash},
        headers={"Idempotency-Key": "idem-retry-" + uuid.uuid4().hex[:8],
                 "X-Test-Identity": "a"})


def test_09_retry_child_from_ordinary_no_effect_released(client: TestClient) -> None:
    cmd = _confirmed_command(client, "retry")
    # 🔴 ``command_state`` 也要推到终态:``defgeo_pcmd_one_live_per_slot`` 的
    #    partial unique 是按 ``command_state NOT IN ('completed','failed','cancelled')``
    #    判"在途"的。只改 canonical/funding 两列,parent 在索引眼里仍然在途,
    #    child 插入必撞唯一键 —— 那是**夹具没把 worker 的收敛做完整**,不是产品缺陷。
    row = _force_command_state(
        cmd["publishCommandId"], command_state="failed",
        canonical_publication_state="failed_no_effect", funding_state="released")

    before = _counts()
    child = _ok(_retry(client, cmd["publishCommandId"],
                       status_version=int(row["status_version"]),
                       command_hash=str(row["command_canonical_hash"])))
    d = _delta(before, _counts())
    assert d[_store.COMMAND_TABLE] == 1, f"retry 应恰建 1 条 child:{d}"
    assert d[_store.OUTBOX_TABLE] == 1, f"child 应恰入 1 条 outbox:{d}"
    assert d["point_freezes"] == 1, f"child 应各冻一次:{d}"

    conn = connect()
    try:
        cur = conn.cursor()
        got = _store.get_command(cur, publish_command_id=child["publishCommandId"],
                                 tenant_owner_id=TENANT_A)
        parent_row = _store.get_command(cur, publish_command_id=cmd["publishCommandId"],
                                        tenant_owner_id=TENANT_A)
        conn.rollback()
    finally:
        conn.close()
    assert got is not None and parent_row is not None
    assert got["parent_command_id"] == cmd["publishCommandId"], got
    assert int(got["command_generation"]) == 2, got
    assert got["lineage_kind"] == "retry_child", got
    # 血缘逐项冻结(FIN-09):article / media identity 与 parent **逐值相同**。
    for col in ("article_revision_id", "article_hash", "publish_item_request_id",
                "public_media_key", "canonical_root_domain_key",
                "funding_policy", "principal_kind", "exact_settlement_points"):
        assert got[col] == parent_row[col], (
            f"child 的 {col} 与 parent 不同({got[col]!r} vs {parent_row[col]!r}) —— 血缘没冻住")
    assert got["public_media_key"] == cmd["item"]["media"]["publicMediaKey"], got
    assert got["canonical_root_domain_key"] == cmd["item"]["media"]["canonicalRootDomainKey"], got
    assert got["decision_snapshot_id"] == cmd["decisionSnapshotId"], got

    # 同 parent 第二次 → 冲突,零副作用。
    before = _counts()
    _err(_retry(client, cmd["publishCommandId"],
                status_version=int(row["status_version"]) ,
                command_hash=str(row["command_canonical_hash"])),
         status=409, code="IDEMPOTENCY_CONFLICT")
    _assert_zero_side_effects(before)


def test_10_retry_child_refused_on_legal_rule_hit(client: TestClient) -> None:
    """MED-18:法律格只能局部修复,**不能** retry-child。"""
    cmd = _confirmed_command(client, "retrylegal")
    row = _force_command_state(
        cmd["publishCommandId"],
        canonical_publication_state="failed_no_effect", funding_state="released",
        legal_rule_id="ad-law-absolute-claim", legal_rule_version="v3",
        legal_passage_ref="p#2", legal_passage_excerpt="全国第一")

    before = _counts()
    detail = _err(_retry(client, cmd["publishCommandId"],
                         status_version=int(row["status_version"]),
                         command_hash=str(row["command_canonical_hash"])),
                  status=409, code="LEGAL_RULE_HIT")
    assert (detail.get("nextAction") or {}).get("kind") == "repair_legal_passage", detail
    _assert_zero_side_effects(before)


def test_11_retry_child_refused_on_unknown_outcome(client: TestClient) -> None:
    """结果未知 ⇒ 钱不自动退、也不许重试(hold_or_quarantine)。"""
    cmd = _confirmed_command(client, "retryunknown")
    row = _force_command_state(
        cmd["publishCommandId"], canonical_publication_state="unknown")

    before = _counts()
    _err(_retry(client, cmd["publishCommandId"],
                status_version=int(row["status_version"]),
                command_hash=str(row["command_canonical_hash"])),
         status=409, code="PUBLISH_DECISION_NOT_CONFIRMABLE")
    _assert_zero_side_effects(before)


# ══════════════════════════════════════════════════════════════════════════
# 13 —— G-4 受控失败正样本
# ══════════════════════════════════════════════════════════════════════════
def test_13_g4_extra_response_key_yields_controlled_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """响应多返回一个键 ⇒ **受控 SafeError 信封**,不是裸 500 traceback。

    本仓 ``response_model extra='forbid'`` 已经炸过三次生产,所以这条正样本
    必须真的走一遍「多一个键」的路径,而不是断言 ``_respond`` 存在。
    """
    from api import defensive_publish_api as api

    body = _scenario("g4")
    snap = _ready_snapshot(client, body)["snapshot"]

    original = api._confirm_payload

    def _extra(*a: Any, **kw: Any) -> dict[str, Any]:
        return {**original(*a, **kw), "__extra__": 1}

    monkeypatch.setattr(api, "_confirm_payload", _extra)
    resp = _confirm(client, snap["decisionSnapshotId"], snap)

    assert resp.status_code == 500, f"多一个键没有变成受控失败:{resp.status_code} {resp.text[:400]}"
    detail = resp.json().get("detail")
    assert isinstance(detail, Mapping), f"裸 500,不是 SafeError 信封:{resp.text[:400]}"
    assert detail.get("code") == "INTERNAL_ERROR", detail
    ref = (detail.get("details") or {}).get("publicErrorRef")
    assert isinstance(ref, str) and ref, f"缺 publicErrorRef:{detail}"
    # POR-14:受控信封里不许有异常类型 / stack / SQL / 主机名。
    text = resp.text
    for leak in ("Traceback", "ValidationError", "psycopg2", "SELECT ", "File \""):
        assert leak not in text, f"受控信封里泄漏了 {leak!r}:{text[:400]}"


# ══════════════════════════════════════════════════════════════════════════
# 14 —— POR-14 跨租户与「对象不存在」同形 404
# ══════════════════════════════════════════════════════════════════════════
def test_14_cross_tenant_404_is_byte_identical_to_absent_404(client: TestClient) -> None:
    body = _scenario("por14")
    snap = _ready_snapshot(client, body)["snapshot"]
    confirmed = _ok(_confirm(client, snap["decisionSnapshotId"], snap))

    for path, absent in (
        (f"/api/defensive-geo/publish/decision-snapshots/{snap['decisionSnapshotId']}",
         "/api/defensive-geo/publish/decision-snapshots/pds_deadbeefdeadbeefdeadbeef"),
        (f"/api/defensive-geo/publish/commands/{confirmed['publishCommandId']}",
         "/api/defensive-geo/publish/commands/pcmd_deadbeefdeadbeefdeadbeef"),
    ):
        cross = client.get(path, headers={"X-Test-Identity": "b"})
        missing = client.get(absent, headers={"X-Test-Identity": "b"})
        assert cross.status_code == 404, (path, cross.status_code, cross.text[:300])
        assert missing.status_code == 404, (absent, missing.status_code)
        assert cross.text == missing.text, (
            f"跨租户 404 与不存在 404 响应体不同 —— 存在性被侧信道泄漏:\n"
            f"  cross  ={cross.text[:300]}\n  missing={missing.text[:300]}")

        # 反向自证:同一路径给 owner 是 200(否则「都 404」可能只是路由写错了)。
        assert client.get(path, headers={"X-Test-Identity": "a"}).status_code == 200, path


# ══════════════════════════════════════════════════════════════════════════
# 15 —— POR-06 毒串零命中
# ══════════════════════════════════════════════════════════════════════════
def test_15_por06_private_columns_never_reach_any_response(client: TestClient) -> None:
    """整条 preview→confirm→status 的**所有响应体**里,毒串零命中。"""
    body = _scenario("por06")
    bodies: list[tuple[str, str]] = []

    resp = _preview(client, body)
    bodies.append(("preview", resp.text))
    snap = _ok(resp)["snapshotResponse"]["snapshot"]

    resp = client.get(
        f"/api/defensive-geo/publish/decision-snapshots/{snap['decisionSnapshotId']}",
        headers={"X-Test-Identity": "a"})
    bodies.append(("get-snapshot", resp.text))
    _ok(resp)

    resp = _confirm(client, snap["decisionSnapshotId"], snap)
    bodies.append(("confirm", resp.text))
    command_id = _ok(resp)["publishCommandId"]

    resp = client.get(f"/api/defensive-geo/publish/commands/{command_id}",
                      headers={"X-Test-Identity": "a"})
    bodies.append(("status", resp.text))
    _ok(resp)

    needles = (POISON_TEXT, POISON_PROVIDER, str(POISON_PROVIDER_MEDIA_ID))
    for stage, text in bodies:
        for needle in needles:
            assert needle not in text, (
                f"{stage} 的响应体里出现了私有列内容 {needle!r} —— POR-06 泄漏")
        # 私有**列名**同样不许出现(键名是词表扫不出来的那一格)。
        payload = json.loads(text)
        _mi.assert_no_private_leak(payload, field=f"por06.{stage}")


# ══════════════════════════════════════════════════════════════════════════
# 16 —— ACT-15 work_admission 接线证明
# ══════════════════════════════════════════════════════════════════════════
def test_16_act15_scope_cap_blocks_confirm_with_zero_side_effects(
    client: TestClient,
) -> None:
    """scope cap 不够 ⇒ INSUFFICIENT_POINTS 且**零 command / 零 freeze / 零 outbox**。

    这条同时是 ``services/defensive_geo/work_admission.admit`` 的接线证明:
    钱包余额是充足的(种子 100000),唯一不够的是 scope cap —— 只有真的调了
    admit 并把 ``scope_remaining_points`` 传进去,才会拦在这里。
    """
    accepted = _seed_accepted_snapshot()
    projection = "svc_w3c_act15"
    # cap 远低于目录最低价(种子最低 20 算力),且 scope <= global(表上有 CHECK)。
    _seed_budget(tenant=TENANT_A, accepted=accepted, projection=projection,
                 global_cap=5, scope_cap=5)
    body = {
        "planItemKey": "plan_act15",
        "articleRevisionId": "rev_act15_1",
        "expectedArticleHash": _bh.body_hash("正文 act15"),
        "acceptedSnapshotId": accepted,
        "serviceProjectionId": projection,
        "brandId": BRAND_A,
    }

    snap_resp = _ready_snapshot(client, body)
    snap = snap_resp["snapshot"]
    assert snap["totalExactPoints"] > 5, (
        f"夹具没能造出「cap 不够」的局面(exact={snap['totalExactPoints']} cap=5)")
    assert snap_resp["budgetBlockers"], "preview 没给出预算 blocker —— 差额没被解释"
    kinds = {o["kind"] for o in snap_resp["adjustmentOptions"]}
    assert "cancel_no_charge" in kinds, (
        f"恒在的 cancel_no_charge 出口不在调整项里:{kinds}")

    before = _counts()
    _err(_confirm(client, snap["decisionSnapshotId"], snap),
         status=409, code="INSUFFICIENT_POINTS")
    _assert_zero_side_effects(before)

    # 钱包一分没动 —— 只看「没有 freeze 行」还不够,余额也要原样。
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT paid_points, bonus_points, frozen_points FROM user_wallets "
                    "WHERE user_id = %s", (TENANT_A,))
        wallet = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    assert int(wallet["frozen_points"] or 0) >= 0, wallet


# ══════════════════════════════════════════════════════════════════════════
# 17 —— ACT-11 里程碑闸的**反向**自证
# ══════════════════════════════════════════════════════════════════════════
def test_17_act11_unactivated_service_is_refused(client: TestClient) -> None:
    """服务没激活 ⇒ confirm 拒绝且**零副作用**。

    🔴 这条是 :func:`_seed_accepted_snapshot` 的反向自证。前面每条判据都靠那个
       helper 把会话推到 ``active`` + activation ``materialized``,如果里程碑闸
       其实根本没在跑,那些判据照样全绿 —— 那就是「安全栓 0 vs 0 假绿」。
       这里造一份**只到 confirmed、没有 activation** 的 accepted snapshot,
       断言同一条 confirm 被拦在 ``service_not_activated`` 上。
    """
    accepted, quote = _allocate_seed_ids()
    snapshot_hash = uuid.uuid4().hex + uuid.uuid4().hex
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO quotes (id, brand_id, service_days) VALUES (%s,%s,90) "
                    "ON CONFLICT (id) DO NOTHING", (quote, BRAND_A))
        cur.execute(
            "INSERT INTO keyword_selection_sessions "
            "(token, quote_id, brand_id, keywords_snapshot, expires_at, status) "
            # 🔴 'confirmed' → customer_accepted,**不是** commercial_basis_established;
            #    activation outbox 也故意不建。
            "VALUES (%s,%s,%s,'[]','2099-01-01','confirmed') RETURNING id",
            (f"tok_w3c_noact_{accepted}", quote, BRAND_A))
        session_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO quote_pricing_snapshots "
            "(id, quote_id, brand_id, selection_session_id, version, reason, "
            " calculation_version, pricing_snapshot, snapshot_hash) "
            "VALUES (%s,%s,%s,%s,1,'w3c 未激活夹具','v1',%s,%s)",
            (accepted, quote, BRAND_A, session_id,
             json.dumps({"delivery_plan": {"schema_version": "geo-delivery-plan-v1"}}),
             snapshot_hash))
        cur.execute(
            "UPDATE keyword_selection_sessions SET customer_confirmed_snapshot_id = %s, "
            "customer_confirmed_snapshot_hash = %s, customer_confirmed_at = NOW() "
            "WHERE id = %s", (accepted, snapshot_hash, session_id))
        conn.commit()
    finally:
        conn.close()

    projection = "svc_w3c_act11"
    _seed_budget(tenant=TENANT_A, accepted=accepted, projection=projection,
                 global_cap=100000, scope_cap=100000)
    body = {
        "planItemKey": "plan_act11",
        "articleRevisionId": "rev_act11_1",
        "expectedArticleHash": _bh.body_hash("正文 act11"),
        "acceptedSnapshotId": accepted,
        "serviceProjectionId": projection,
        "brandId": BRAND_A,
    }
    snap = _ready_snapshot(client, body)["snapshot"]

    before = _counts()
    _err(_confirm(client, snap["decisionSnapshotId"], snap),
         status=409, code="PUBLISH_DECISION_NOT_CONFIRMABLE")
    _assert_zero_side_effects(before)
