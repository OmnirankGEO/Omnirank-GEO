"""§15.9.3 v2 监测端点 —— **真 HTTP 打真库**。

🔴 为什么必须真库
----------------
本仓记过:「只验 `!=422` 会漏掉整层库合同」。把判据升成
「不许 422 **也不许 500**」时,当场抓出 6 处必 500 的端点。
所以这一批用 **生产 schema dump + 迁移 046** 建库,不手写 schema ——
手写会漏掉真正要证明的那一列。

🔴 pg_dump 的 ``search_path=''`` 是毒(2026-08-12 记过)
------------------------------------------------------
dump 里 ``set_config('search_path','',false)`` 会让**之后**所有不带 schema
限定的语句找不到表。装载前中和掉,并在连接上显式 ``SET search_path``。
"""

from __future__ import annotations

import io
import os
import re

import psycopg2
import psycopg2.extras
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.defensive_geo_w4_2026_08_22.conftest import (
    PACKAGE_OWNED_MIGRATIONS,
    ROOT,
    _read_sql,
)

HTTP_DB_URL = os.getenv(
    "DEFGEO_W4_HTTP_DB_URL",
    "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_w4_http_test",
)

PROD_SCHEMA = (ROOT / "tests" / "article_self_report_2026_08_19"
               / "prod_schema_2026-08-19.sql")


def _neutralize_search_path(sql: str) -> str:
    """中和 dump 里的 ``SELECT pg_catalog.set_config('search_path', '', false);``。

    不中和的话,**它之后**的每一条不带 schema 限定的语句都找不到表 ——
    表面上是"判据在空库上跑",实际是 dump 只装了一半。
    """
    sql = re.sub(
        r"SELECT\s+pg_catalog\.set_config\('search_path',\s*''.*?\);",
        "-- [neutralized] search_path", sql, flags=re.I | re.S)
    # 🔴 新版 pg_dump 会写 psql **元命令**(`\restrict` / `\unrestrict`)。
    #    psycopg2 走 wire protocol,不认元命令 —— 不剥掉整份 dump 第 5 行就炸。
    #    那种红看起来像"判据发现了问题",其实是**夹具自己没跑起来**,
    #    本仓记过:夹具没跑起来的红不算抓到毒。
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("\\"))


@pytest.fixture(scope="module")
def http_db():
    name = HTTP_DB_URL.rsplit("/", 1)[-1].lower()
    for token in ("defgeo", "test"):
        assert token in name, f"HTTP 判据库名 {name!r} 缺 {token!r}(安全栓)"

    admin = psycopg2.connect(HTTP_DB_URL.rsplit("/", 1)[0] + "/postgres")
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
        if not c.fetchone():
            c.execute(f'CREATE DATABASE "{name}"')
    admin.close()

    conn = psycopg2.connect(HTTP_DB_URL,
                            cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute("SET search_path TO public")
        dump = _neutralize_search_path(
            io.open(PROD_SCHEMA, encoding="utf-8", errors="replace").read())
        cur.execute(dump)
        for rel in PACKAGE_OWNED_MIGRATIONS:
            cur.execute(_read_sql(rel))
        # 活性自证:两侧都装上了才算数。
        cur.execute("SELECT to_regclass('public.monitoring_run_cells') AS a, "
                    "       to_regclass('public.defgeo_monitoring_attempts') AS b")
        row = cur.fetchone()
        assert row["a"] and row["b"], (
            "生产 dump 或迁移 046 有一侧没装上 —— 这时判据全绿是假绿")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def client(http_db, monkeypatch_module):
    """路由级 app + 真库连接。

    🔴 **不**挂 server.py 全量 app:那会把 200+ 个无关 router 拉进来,
       任何一个 import 失败都会让本批判据以"跟本包无关的理由"变红,
       分不清是我的端点坏了还是别人的。
    """
    from db import connection as _conn_mod

    def _fake_get_connection():
        return psycopg2.connect(HTTP_DB_URL,
                                cursor_factory=psycopg2.extras.RealDictCursor)

    monkeypatch_module.setattr(_conn_mod, "get_connection", _fake_get_connection)

    import api.defensive_monitoring_api as mod

    monkeypatch_module.setattr(mod, "_db", _fake_get_connection)

    app = FastAPI()

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        # 模拟现役中间件注入。🔴 键名用 ``user_id`` —— 生产中间件写的就是这个;
        #    夹具用 ``id`` 会造出"生产从来不会发的键",整片端点线上必 500
        #    而判据全绿(本仓 2026-08-20 记过)。
        request.state.user = {"user_id": 7, "is_admin": False}
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(mod.router)
    return _SkipWhenProgressClosed(app, raise_server_exceptions=False)


class _SkipWhenProgressClosed(TestClient):
    """[fix-of-fix P1-B 2026-08-23] 撞上进度入口的关闭信封 ⇒ **skip**,不是红。

    P1-B 把 ``/runs/{task_id}/progress`` 关成了 typed 拒绝(账本还没接线,
    对客户下发"进度 XX%"等于拿一个没人在写的账本回答"我的东西做到哪了")。
    本文件里驱动那条端点的判据断言的是**还没接线**的行为。

    🔴 判定放在**响应**上,不手挑名单:撞不上的照常跑、照常红;
       入口一旦重开(``_MONITORING_PROGRESS_OPEN=True``),这些判据
       **自动复活并必须通过**。而临时开闸的那些判据(P1-C 一族)
       根本收不到这个信封,不受影响 —— 那正是"端点关着,代码仍要是对的"。
    """

    def request(self, *args: Any, **kwargs: Any):                # noqa: ANN201
        resp = super().request(*args, **kwargs)
        if resp.status_code == 403:
            try:
                if (resp.json().get("detail") or {}).get("code") == "MONITORING_PROGRESS_CLOSED":
                    pytest.skip(
                        "监测进度入口当前是关的(P1-B)—— 本条判据驱动的是还没接线的行为。"
                        "重开后它会自动复活并必须通过。"
                    )
            except ValueError:
                pass
        return resp


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(autouse=True)
def _allow_brand(monkeypatch):
    """放行归属校验,让判据能打到 handler 主体。

    🔴 归属**本身**的判据单列(见 ``test_cross_tenant_is_same_shaped_404``),
       那一条不打这个补丁 —— 否则"授权有没有生效"就没人验了。
    """
    import auth.brand_access as ba

    monkeypatch.setattr(ba, "require_brand_access",
                        lambda request, brand_id, allow_null=False: None)


# ══════════════════════════════════════════════════════════════════════

class TestRunAdmissionHttp:
    def test_legacy_brand_is_admitted_and_labelled_legacy(self, client):
        """MON-12 方向②:新门**不得**阻断 legacy。

        库里没有任何 v2 报价 ⇒ enrollment 恒 LEGACY ⇒ 必须放行。
        这一条同时证明 §19 变异 140 的另一半有人在守。
        """
        r = client.post("/api/defensive-geo/monitoring/run-admission",
                        json={"brandId": 1})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["isV2"] is False
        assert body["admitted"] is True
        assert body["path"] == "legacy_monitoring_run"
        assert body["reasonCode"] is None

    def test_unknown_request_key_is_422_not_500(self, client):
        """API-01:未知/错 casing ⇒ 422,**不是** 500。"""
        r = client.post("/api/defensive-geo/monitoring/run-admission",
                        json={"brandId": 1, "surprise": "x"})
        assert r.status_code == 422, r.text

    def test_field_name_form_is_accepted_and_that_is_the_contract(self, client):
        """``populate_by_name=True`` ⇒ snake 字段名也收。**把现状钉住**。

        🔴 第一版这条写的是 ``assert r.status_code in (200, 422)`` ——
           那等于没判据:两种结果都通过,合同怎么变它都绿。
           现在钉死实际合同(收),日后有人改成只收 alias,这条会红并
           提醒他同步改前端与 OpenAPI 示例(API-01 要求三者逐字段一致)。
        """
        r = client.post("/api/defensive-geo/monitoring/run-admission",
                        json={"brand_id": 1})
        assert r.status_code == 200, r.text

    def test_response_uses_camel_case_aliases(self, client):
        """API-01:wire 形状是 camelCase,不是 python 字段名。"""
        r = client.post("/api/defensive-geo/monitoring/run-admission",
                        json={"brandId": 1})
        body = r.json()
        assert "isV2" in body and "is_v2" not in body
        assert "freezeCountExpected" in body


class TestRunProgressHttp:
    def _seed_cells(self, http_db, *, task_id, brand_id, plan_hashes):
        with http_db.cursor() as cur:
            # 🔴 ``monitoring_run_cells.brand_id`` 对 ``brands`` 有真 FK。
            #    真 schema 会逼夹具把上游对象也建出来 —— 手写 schema 常常
            #    连 FK 都没有,于是"测试造得出的数据"生产根本插不进去。
            cur.execute(
                "INSERT INTO public.brands (id, name) VALUES (%s,%s) "
                "ON CONFLICT (id) DO NOTHING",
                (brand_id, f"_w4_test_brand_{brand_id}"))
            # 🔴 ``client_id`` 在真 schema 上是 NOT NULL。
            #    手写 schema 时这一列多半会被写成可空,于是夹具"跑得通"
            #    而生产必炸 —— 用生产 dump 的价值就在这一格。
            cur.execute(
                "INSERT INTO public.monitoring_tasks (id, brand_id, client_id) "
                "VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (task_id, brand_id, f"_w4_test_{brand_id}"))
            for i, ph in enumerate(plan_hashes, start=1):
                cur.execute(
                    "INSERT INTO public.monitoring_run_cells "
                    "(task_id, brand_id, keyword_id, keyword_source, "
                    " keyword_snapshot, question_snapshot, target_brand_snapshot, "
                    " platform, is_planned, entitlement_snapshot, order_snapshot, "
                    " fulfillment_credential, plan_hash) "
                    "VALUES (%s,%s,%s,'confirmed','k','q','b','doubao',TRUE,"
                    " '{}'::jsonb,'{}'::jsonb, gen_random_uuid(), %s)",
                    (task_id, brand_id, i, ph))

    def test_progress_is_rebuilt_from_the_attempt_ledger(self, client, http_db):
        """MON-11:planned/attempted/terminal/progressPct 从账本重建且可复算。"""
        ph1, ph2 = "a" * 64, "b" * 64
        self._seed_cells(http_db, task_id=9001, brand_id=1,
                         plan_hashes=[ph1, ph2])
        with http_db.cursor() as cur:
            # 一格:先失败再成功(两条 attempt,第一条 error 保留)
            for ordinal, state, err in ((1, "engine_error", "TIMEOUT"),
                                        (2, "answered", None)):
                cur.execute(
                    "INSERT INTO public.defgeo_monitoring_attempts "
                    "(attempt_id, plan_cell_id, attempt_ordinal, run_authority_id, "
                    " tenant_owner_user_id, brand_id, actual_provider, actual_model, "
                    " actual_surface, actual_search_mode, request_hash, "
                    " terminal_state, error_code, terminal_at, ledger_version) "
                    "VALUES (%s,%s,%s,'ra',7,1,'doubao','m','s','sm','rh',"
                    " %s,%s,NOW(),'v1')",
                    (f"{ordinal}" * 64, ph1, ordinal, state, err))

        r = client.get("/api/defensive-geo/monitoring/runs/9001/progress",
                       params={"brandId": 1})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["plannedCells"] == 2
        assert b["attemptedCells"] == 1        # 只有 ph1 有 durable attempt
        assert b["terminalCells"] == 1
        assert b["attemptRecords"] == 2        # 真实调用数,不是 retry_count
        # MON-10:fallback 成功后**首发失败仍在**
        assert b["attemptErrors"] == [
            {"planCellId": ph1, "attemptOrdinal": 1, "errorCode": "TIMEOUT"}]
        assert b["progressPct"] == pytest.approx(50.0)

    def test_progress_on_a_task_without_a_v2_plan_is_typed_not_a_zero(self, client, http_db):
        """🔴 [工单 E3-4 · P1-10 · 2026-08-26 · 本条**期望反转**,已报 Review]

        原名 ``test_progress_is_never_500_on_an_existing_but_empty_task``,
        断言的是「存在但 0 格 ⇒ 200 + progressPct 0.0」,理由写着
        「0 是**真的** 0」。

        ═══════════════════════════════════════════════════════════════
        Codex 二审 P1-10 逐字推翻的正是这一句:
        「legacy task 不能继续返回成功 200/progressPct=0;应转旧链或
          typed unavailable + nextAction」。
        推翻的理由不是"0 算错了",而是**这个 0 会被读成什么**:
        销售看到 0%,判断是"卡住了/没跑",于是**重跑一次**(再花一次钱)。
        而 0 格的两种真实成因都不该导向重跑:
          · 老链跑的 —— 结果早就有了,只是不在这条口径上;
          · v2 建格失败 —— 那是故障,该显式说,不该伪装成"刚开始"。
        生产三个调用点都在**同一个请求**里建 task + 建格
        (``create_monitoring_run_cells``),所以"v2 task 存在但 0 格"
        不是稳态,是上面第二种。
        ═══════════════════════════════════════════════════════════════

        🔴 原不变式**原样保留**:仍然不许 500,仍然与"不存在 ⇒ 404"分开
           (下一条守那一格)。变的只是"空"这一格的对外形状:
           从一个会被误读的 0,变成一句说得清、且自带下一步的 typed 拒绝。
        """
        self._seed_cells(http_db, task_id=9002, brand_id=1, plan_hashes=[])
        r = client.get("/api/defensive-geo/monitoring/runs/9002/progress",
                       params={"brandId": 1})
        assert r.status_code != 500, r.text
        assert r.status_code == 409, r.text
        body = r.json()["detail"]
        assert body["code"] == "MONITORING_LEGACY_RUN", body
        # 不可重试:再点一次仍然是同一次没有 v2 计划的运行。
        assert body["retryable"] is False, body
        assert body["publicExplanation"], body
        assert body["nextAction"]["label"], body

    def test_progress_on_an_unknown_task_is_404_not_a_fabricated_zero(self, client):
        """[P1-3] 不存在的 task ⇒ typed NOT_FOUND,不是 200 + 全零。

        改之前 ``-1`` / ``0`` / 任意不存在的 id 全都返回 200 + 全零 payload。
        那是**无中生有的谎报**:"这次跑了 0 格、完成 0%" 与 "根本没有这次跑"
        在客户眼里是两件完全不同的事,而我们把后者说成了前者。
        """
        r = client.get("/api/defensive-geo/monitoring/runs/99999/progress",
                       params={"brandId": 1})
        assert r.status_code != 500, f"越界/不存在把端点打成了 500:{r.text}"
        assert r.status_code != 200, f"仍在编造零进度:{r.text}"
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "NOT_FOUND", r.text


class TestComparabilityHttp:
    def test_no_baseline_still_gives_an_executable_next_action(self, client, http_db):
        """🔴 Z-2.3:``no_comparable_baseline`` 分支**不许**是死路。

        规格原文把 ``nextAction: null`` 改成了指向「建立同口径复测计划」。
        判据打的就是这一位:动作非空且带可渲染 label。
        """
        with http_db.cursor() as cur:
            cur.execute(
                "INSERT INTO public.defgeo_report_snapshots (report_snapshot_id, "
                " revision, tenant_owner_user_id, brand_id, state, content_hash, "
                " plan_snapshot_id, plan_snapshot_hash, sampling_window_start, "
                " sampling_window_end, cutoff_at, input_watermark, raw_result_ids, "
                " entity_resolver_version, outcome_classifier_version, "
                " evidence_extractor_version, metric_definition_version, "
                " snapshot_version) "
                "VALUES ('rs-http-1',1,7,1,'ready',%s,'ps',%s,NOW(),NOW(),NOW(),"
                " 'wm',ARRAY[1],'r1','c1','e1','m1','sv1') "
                "ON CONFLICT DO NOTHING",
                ("h" * 64, "g" * 64))

        r = client.get(
            "/api/defensive-geo/monitoring/reports/rs-http-1/comparability",
            params={"brandId": 1})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["level"] == "none"
        assert b["reasonCode"] == "no_baseline"
        assert b["nextAction"] is not None
        assert b["nextAction"].get("label"), "没有 label 的动作是死路(§0.5.6)"

    def test_missing_snapshot_is_safe_404_not_500(self, client):
        """POR-14:不存在与无权**同形**,且走 SafeError 信封不裸 500。"""
        r = client.get(
            "/api/defensive-geo/monitoring/reports/does-not-exist/comparability",
            params={"brandId": 1})
        assert r.status_code == 404, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "NOT_FOUND"
        assert detail.get("publicExplanation"), "SafeError 必须带用户句"
        assert detail.get("nextAction", {}).get("label")
