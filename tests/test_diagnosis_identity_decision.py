"""诊断 brand-cells 决策服务测试(板块 A · 2026-07-22 · 要求 4/6/8/9/12)

DB 层经 get_conn 注入 fake,本地无 PG 可跑,不碰真实库:
  · local-only 重判:verifier/httpx 间谍断言 provider 调用 0 次;SQL 日志断言计费写 0 次
  · 幂等:同 request_id 同参返既有结果;同键异参 409
  · 跨品牌 404 / 跨租户 PermissionError(403)
  · 别名不全局污染:只写本 brand 的共享 name_decisions
  · 确认后原子重算:分数/等级/快照(v2 回写载荷)一致
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest

from services.brand_identity_resolver import BrandIdentity
from services.diagnosis_identity_decision import (
    DiagnosisIdentityReviewConflict,
    DiagnosisIdentityReviewNotFound,
    decide_brand_cell,
    list_brand_cells,
)

BRAND_ID = 17
DIAG_ID = 101
OWNER_ID = 7001
QUESTION = "揭阳住酒店哪家好？"
ENGINE = "doubao"
ANSWER = "商务出差可以选择滨江南路雅栖酒店，停车和洗衣更方便。"


def _norm_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", str(sql)).strip().lower()


class FakeCursor:
    def __init__(self, db: "FakeDB"):
        self.db = db
        self._rows: list[dict] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple | None = None):
        text = _norm_sql(sql)
        params = params or ()
        self._rows = []
        self.rowcount = 0
        self.db.sql_log.append(text)

        if text.startswith(("savepoint ", "release savepoint ", "rollback to savepoint ")):
            # SAVEPOINT 三段式:fake 无真实事务中止态,记录 SQL 日志后 no-op
            return self
        if "from diagnosis_records where id = %s" in text and "for update" in text:
            row = self.db.diagnosis.get(int(params[0]))
            self._rows = [dict(row)] if row else []
        elif text.startswith("select id, owner_user_id, name, company_name, brand_display_names from public.brands"):
            row = self.db.brands.get(int(params[0]))
            self._rows = [dict(row)] if row else []
        elif "from public.user_roles" in text:
            self._rows = [{"?column?": 1}] if int(params[0]) in self.db.admins else []
        elif "from public.user_clients" in text:
            hit = (int(params[0]), int(params[1])) in self.db.user_clients
            self._rows = [{"?column?": 1}] if hit else []
        elif "to_regclass('public.organization_memberships')" in text:
            self._rows = [{"ready": False}]
        elif "from public.organization_memberships" in text:
            self._rows = []
        elif "from public.monitoring_identity_decision_events" and "where request_id" in text:
            row = self.db.events.get(str(params[0]))
            self._rows = [dict(row)] if row else []
        elif "from public.monitoring_identity_name_decisions" in text and "for update" in text:
            brand_id, names = int(params[0]), list(params[1])
            self._rows = [
                {"normalized_name": key[1], "decision": value["decision"]}
                for key, value in self.db.name_decisions.items()
                if key[0] == brand_id and key[1] in names
            ]
        elif text.startswith("insert into public.monitoring_identity_name_decisions"):
            if self.db.name_decision_insert_error is not None:
                raise self.db.name_decision_insert_error  # 注入并发唯一约束冲突等写入异常
            brand_id, name_key, display, decision = int(params[0]), params[1], params[2], params[3]
            key = (brand_id, name_key)
            existing = self.db.name_decisions.get(key)
            if existing and existing["decision"] != decision:
                self.rowcount = 0  # ON CONFLICT WHERE 不命中
            else:
                version = (existing or {}).get("decision_version", 0) + 1
                self.db.name_decisions[key] = {
                    "display_name": display,
                    "decision": decision,
                    "decision_version": version,
                }
                self.rowcount = 1
        elif text.startswith("update public.brands"):
            self.db.brands[int(params[1])]["brand_display_names"] = params[0]
            self.rowcount = 1
        elif text.startswith("update public.client_profiles"):
            self.db.profile_updates.append(params)
            self.rowcount = 1
        elif text.startswith("update diagnosis_records set raw_data_json"):
            self.db.diagnosis[int(params[1])]["raw_data_json"] = params[0]
            self.rowcount = 1
        elif text.startswith("update diagnosis_records set ai_detected_count"):
            # [WO_MENTION_COUNT 2026-08-08] 改写路径新增的列镜像刷新。
            # 假库是严格白名单(未编排的 SQL 直接抛)—— 它把这条新写入抓了出来,
            # 这是对的;这里补上编排,并**记下值**,让老套件也能校这两个数。
            row = self.db.diagnosis[int(params[2])]
            row["ai_detected_count"] = params[0]
            # 真 SQL 是 `ai_mention_rate = COALESCE(%s, ai_mention_rate)` ——
            # 假库照着仿,别把"传 None 就保留原值"这个语义仿丢了。
            if params[1] is not None:
                row["ai_mention_rate"] = params[1]
            self.rowcount = 1
        elif text.startswith("update diagnosis_records set total_score"):
            # R3 · P2 降级写:只落评分 SSOT 列 + report_v2_error(不再标 generated_at)
            row = self.db.diagnosis[int(params[3])]
            row["total_score"] = params[0]
            row["level"] = params[1]
            row["report_v2_error"] = params[2]
            self.rowcount = 1
        elif text.startswith("update brands b set latest_score"):
            brand = self.db.brands[int(params[2])]
            brand["latest_score"] = params[0]
            brand["latest_diagnosis_id"] = params[1]
            self.rowcount = 1
        elif "from client_profiles where brand_id" in text:
            self._rows = []
        elif "from quotes where diagnosis_id" in text:
            # 🔴 [#54/#55 2026-09-04] 报告关联的报价按**本次诊断**取,不再按品牌最新一张。
            #    这里**只认 diagnosis_id 这一个形状**(不兼容旧的 brand_id):
            #    谁把生产查询退回 `WHERE brand_id`,这一支就不再命中,
            #    SQL 落到链尾 AssertionError,本批测试当场红 —— 当接线锁用。
            self._rows = []
        elif text.startswith("insert into public.monitoring_identity_decision_events"):
            if self.db.event_insert_error is not None:
                raise self.db.event_insert_error  # 注入并发唯一约束冲突等写入异常
            metadata = params[9]
            metadata = getattr(metadata, "adapted", metadata)
            event = {
                "event_id": len(self.db.events) + 1,
                "decided_at": "2026-07-22T00:00:00+00:00",
                "result_id": None,
                "brand_id": int(params[0]),
                "action": params[1],
                "selected_name": params[2],
                "normalized_name": params[3],
                "result_version_before": int(params[5]),
                "result_version_after": int(params[6]),
                "actor_user_id": int(params[7]),
                "request_id": str(params[8]),
                "metadata": metadata,
                "source_kind": "diagnosis",
                "source_result_id": int(params[10]),
                "tenant_id": params[11],
                "ip": params[12],
                "reason": params[13],
            }
            self.db.events[event["request_id"]] = event
            self._rows = [{"event_id": event["event_id"], "decided_at": event["decided_at"]}]
            self.rowcount = 1
        elif text.startswith("select id, brand_id, brand_name, raw_data_json from diagnosis_records"):
            row = self.db.diagnosis.get(int(params[0]))
            self._rows = [dict(row)] if row else []
        else:
            raise AssertionError(f"fake DB 未编排的 SQL: {text}")
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, db: "FakeDB"):
        self.db = db
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakeDB:
    def __init__(self):
        self.diagnosis = {
            DIAG_ID: {
                "id": DIAG_ID,
                "brand_id": BRAND_ID,
                "brand_name": "揭阳滨江南路雅栖酒店",
                "raw_data_json": json.dumps(_raw_payload(), ensure_ascii=False),
                "report_v2_version": "v2",
            }
        }
        self.brands = {
            BRAND_ID: {
                "id": BRAND_ID,
                "owner_user_id": OWNER_ID,
                "name": "揭阳滨江南路雅栖酒店",
                "company_name": "揭阳滨江南路雅栖酒店有限公司",
                "brand_display_names": '["揭阳滨江南路雅栖酒店"]',
                "is_deleted": False,
            }
        }
        self.admins: set[int] = set()
        self.user_clients: set[tuple[int, int]] = set()
        self.name_decisions: dict[tuple[int, str], dict] = {}
        self.events: dict[str, dict] = {}
        self.event_insert_error: Exception | None = None
        self.name_decision_insert_error: Exception | None = None
        self.profile_updates: list = []
        self.sql_log: list[str] = []
        self.conn = FakeConn(self)

    def get_conn(self):
        return self.conn


def _raw_payload() -> dict:
    return {
        "data": {
            "ai_visibility": {
                "test_questions": [QUESTION],
                "engines_tested": [ENGINE, "deepseek"],
                "question_types": {QUESTION: "regional_industry"},
                "detail_table": [
                    {
                        "question": QUESTION,
                        "results": {
                            ENGINE: {
                                "answer_summary": ANSWER[:150],
                                "brand_detected": False,
                                "brand_verdict": "UNKNOWN",
                                "detection_reason": "local_evidence_requires_review",
                                "full_response": ANSWER,
                                "status": "error",
                            },
                            "deepseek": {
                                "answer_summary": "推荐揭阳滨江南路雅栖酒店。",
                                "brand_detected": True,
                                "brand_verdict": "YES",
                                "detection_reason": "trusted_exact_alias",
                                "matched_text": "揭阳滨江南路雅栖酒店",
                                "full_response": "推荐揭阳滨江南路雅栖酒店。",
                                "status": "success",
                            },
                        },
                    }
                ],
            }
        },
        "scores": {},
    }


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()

    identity = BrandIdentity(
        brand_id=BRAND_ID,
        canonical_names=("揭阳滨江南路雅栖酒店", "揭阳滨江南路雅栖酒店有限公司"),
        industry="酒店",
    )
    monkeypatch.setattr(
        "services.diagnosis_identity_decision._load_identity_for_review",
        lambda brand_id, fallback_name="": identity,
    )

    captured: dict[str, Any] = {}

    def fake_assemble(**kwargs):
        captured["assemble_kwargs"] = kwargs
        return {"internal": {"full_markdown": "md"}, "client": {"full_markdown": "md"},
                "completeness": {}, "funnel_score": {}, "error": None}

    def fake_update(diagnosis_id, v2_result, *, conn=None):
        captured["update"] = (diagnosis_id, v2_result, conn)
        return True

    monkeypatch.setattr("services.diagnosis_report_v2.assemble_diagnosis_report_v2", fake_assemble)
    monkeypatch.setattr("services.diagnosis_report_v2.update_diagnosis_v2_in_db", fake_update)

    # provider 间谍:任何 verifier/付费调用都立刻失败
    async def forbidden_verifier(**_kwargs):
        raise AssertionError("决策链禁止 provider 调用")

    monkeypatch.setattr(
        "services.brand_identity_resolver._default_structured_verifier",
        forbidden_verifier,
    )
    db.captured = captured
    return db


def _decide(db: FakeDB, **overrides):
    kwargs = dict(
        diagnosis_id=DIAG_ID,
        brand_id=BRAND_ID,
        actor_user_id=OWNER_ID,
        question=QUESTION,
        engine=ENGINE,
        action="confirm_yes",
        selected_name="滨江南路雅栖酒店",
        reason="正文明确是本店",
        request_id=str(uuid.uuid4()),
        expected_version=0,
        ip="10.0.0.8",
        get_conn=db.get_conn,
    )
    kwargs.update(overrides)
    return decide_brand_cell(**kwargs)


# ---------------------------------------------------------------------------
def test_list_brand_cells_five_state_and_candidates(fake_db):
    data = list_brand_cells(DIAG_ID, BRAND_ID, get_conn=fake_db.get_conn)
    cells = {(c["question"], c["engine"]): c for c in data["cells"]}
    pending = cells[(QUESTION, ENGINE)]
    assert pending["state"] == "PENDING_IDENTITY"
    assert pending["candidates"] == ["滨江南路雅栖酒店"]
    assert pending["identity_decision_version"] == 0
    assert cells[(QUESTION, "deepseek")]["state"] == "YES"
    # PENDING 不进确定分母
    assert data["aggregates"]["totals"]["total"] == 1
    assert data["aggregates"]["totals"]["pending_identity"] == 1


def test_confirm_yes_atomic_recompute_and_audit(fake_db):
    result = _decide(fake_db)
    assert result["success"] is True
    assert result["status"] == "resolved"
    # 单元格就地更新
    assert result["cell"]["state"] == "YES"
    assert result["cell"]["identity_review_state"] == "confirmed"
    assert result["cell"]["identity_decision_version"] == 1
    # 原子重算:2/2 命中 regional_industry · 单层覆盖封顶成长级
    assert result["aggregates"]["totals"]["total"] == 2
    assert result["aggregates"]["totals"]["detected"] == 2
    assert result["aggregates"]["totals"]["pending_identity"] == 0
    assert result["score"] == 100
    assert result["level"] == "成长级"
    # v2 回写载荷与响应一致(快照一致性)
    _diag_id, v2_result, conn = fake_db.captured["update"]
    assert conn is fake_db.conn  # 单事务:复用同一连接
    assert v2_result["funnel_score"]["total_score"] == result["score"]
    assert v2_result["funnel_score"]["level"] == result["level"]
    # 原始回答未被覆盖;人工决策追加在 cell 上
    stored = json.loads(fake_db.diagnosis[DIAG_ID]["raw_data_json"])
    cell = stored["data"]["ai_visibility"]["detail_table"][0]["results"][ENGINE]
    assert cell["full_response"] == ANSWER
    assert cell["brand_verdict"] == "YES"
    assert cell["human_decision"]["action"] == "confirm_yes"
    # 共享别名真相:同 brand positive;事件 source_kind=diagnosis + ip/reason
    assert fake_db.name_decisions[(BRAND_ID, "滨江南路雅栖酒店")]["decision"] == "positive"
    event = next(iter(fake_db.events.values()))
    assert event["source_kind"] == "diagnosis"
    assert event["source_result_id"] == DIAG_ID
    assert event["tenant_id"] == OWNER_ID
    assert event["ip"] == "10.0.0.8"
    assert event["reason"] == "正文明确是本店"
    assert event["action"] == "yes"
    # 别名持久化到 brands SSOT
    assert "滨江南路雅栖酒店" in fake_db.brands[BRAND_ID]["brand_display_names"]
    # [WO_MENTION_COUNT 2026-08-08] 汇总计数跟着重算落库(此前只刷 dimension_stats,
    # 客户报告那句「品牌被提及 N 次」因此纹丝不动)。JSON 与列镜像都要对。
    assert stored["data"]["ai_visibility"]["detected_count"] == 2
    assert fake_db.diagnosis[DIAG_ID]["ai_detected_count"] == 2
    assert result["totals_delta"]["detected_count"]["after"] == 2
    # 🔴 本夹具的 ai_visibility **没有 total_tests** → 没有分母就不许算 rate,
    #    更不许写 0(凭空把客户推荐率抹成 0% 比不刷新更坏)。
    assert result["totals_delta"]["rate_recomputed"] is False
    assert "overall_mention_rate" not in stored["data"]["ai_visibility"]
    # COALESCE 保留原值:夹具本来就没有这一列 → 刷完仍然没有(而不是被写成 0)
    assert "ai_mention_rate" not in fake_db.diagnosis[DIAG_ID]
    # 计费写 0:SQL 日志无任何 token_usage/billing/cost 写
    assert not any(
        "token_usage" in sql or "billing" in sql or "cost_ledger" in sql
        for sql in fake_db.sql_log
    )
    # 单事务:恰好 1 次 commit,0 次 rollback
    assert fake_db.conn.commits == 1
    assert fake_db.conn.rollbacks == 0


def test_idempotent_same_request_id_returns_stored_result(fake_db):
    request_id = str(uuid.uuid4())
    first = _decide(fake_db, request_id=request_id)
    commits = fake_db.conn.commits
    second = _decide(fake_db, request_id=request_id)
    assert second["status"] == "idempotent"
    assert second["cell"]["state"] == "YES"
    assert second["score"] == first["score"]
    assert fake_db.conn.commits == commits  # 无二次写入
    assert len(fake_db.events) == 1


def test_same_request_id_different_params_is_409(fake_db):
    request_id = str(uuid.uuid4())
    _decide(fake_db, request_id=request_id)
    with pytest.raises(DiagnosisIdentityReviewConflict, match="请求编号已用于其他确认"):
        _decide(fake_db, request_id=request_id, action="confirm_no")


def test_already_decided_cell_is_409(fake_db):
    _decide(fake_db)
    with pytest.raises(DiagnosisIdentityReviewConflict, match="该记录已经处理"):
        _decide(fake_db, request_id=str(uuid.uuid4()))


def test_expected_version_mismatch_is_409(fake_db):
    with pytest.raises(DiagnosisIdentityReviewConflict, match="确认状态已变化"):
        _decide(fake_db, expected_version=3)


def test_cross_brand_is_404(fake_db):
    with pytest.raises(DiagnosisIdentityReviewNotFound):
        _decide(fake_db, brand_id=999)


def test_cross_tenant_actor_is_forbidden(fake_db):
    with pytest.raises(PermissionError):
        _decide(fake_db, actor_user_id=7999)
    assert fake_db.conn.commits == 0


def test_confirm_no_marks_rejected_alias_scoped_to_brand(fake_db):
    result = _decide(fake_db, action="confirm_no", selected_name=None)
    assert result["cell"]["state"] == "NO"
    # rejected alias 只落在本 brand(不全局污染)
    assert (BRAND_ID, "滨江南路雅栖酒店") in fake_db.name_decisions
    assert fake_db.name_decisions[(BRAND_ID, "滨江南路雅栖酒店")]["decision"] == "negative"
    assert all(key[0] == BRAND_ID for key in fake_db.name_decisions)
    # 确认未提到后聚合:1/2 命中
    assert result["aggregates"]["totals"]["detected"] == 1
    assert result["aggregates"]["totals"]["total"] == 2


def test_negative_decision_on_trusted_name_is_409(fake_db):
    with pytest.raises(DiagnosisIdentityReviewConflict, match="可信名称"):
        _decide(fake_db, action="confirm_no", selected_name="揭阳滨江南路雅栖酒店")


def test_opposite_existing_name_decision_is_409(fake_db):
    fake_db.name_decisions[(BRAND_ID, "滨江南路雅栖酒店")] = {
        "display_name": "滨江南路雅栖酒店",
        "decision": "negative",
        "decision_version": 1,
    }
    with pytest.raises(DiagnosisIdentityReviewConflict, match="相反确认"):
        _decide(fake_db, action="confirm_yes", selected_name="滨江南路雅栖酒店")


def test_explicit_brand_text_originally_no_becomes_yes_after_confirm(fake_db):
    """正文明确出现品牌但原判 NO → 确认后重判 YES(金标准专项在服务层的闭环)。"""
    stored = json.loads(fake_db.diagnosis[DIAG_ID]["raw_data_json"])
    cell = stored["data"]["ai_visibility"]["detail_table"][0]["results"][ENGINE]
    cell["brand_verdict"] = "NO"
    cell["detection_reason"] = "no_identity_candidate"
    cell["status"] = "success"
    fake_db.diagnosis[DIAG_ID]["raw_data_json"] = json.dumps(stored, ensure_ascii=False)

    result = _decide(fake_db, action="confirm_yes", selected_name="滨江南路雅栖酒店")
    assert result["cell"]["state"] == "YES"
    assert result["cell"]["brand_verdict"] == "YES"
    assert result["aggregates"]["totals"]["detected"] == 2


# ---------------------------------------------------------------------------
# 集中严审 R1 · P2-1:错误语义(400 入参 / 409 并发唯一约束)
# ---------------------------------------------------------------------------

def test_concurrent_same_request_id_unique_violation_is_409(fake_db):
    """跨诊断并发同 request_id:events 表 UNIQUE(request_id) 撞约束 → DiagnosisIdentityReviewConflict(409 语义 · 人话),不再冒 500。"""
    import psycopg2

    fake_db.event_insert_error = psycopg2.errors.UniqueViolation(
        'duplicate key value violates unique constraint '
        '"monitoring_identity_decision_events_request_id_key"'
    )
    with pytest.raises(DiagnosisIdentityReviewConflict, match="并发冲突"):
        _decide(fake_db)
    assert fake_db.conn.commits == 0
    assert fake_db.conn.rollbacks >= 1  # 冲突后事务必须回滚,不留半写状态


def _decision_api_client(monkeypatch, service_side_effect):
    """构造 decision API 的 TestClient:鉴权解析打桩,服务层按给定异常/函数替换。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import api.diagnosis_identity_api as dia

    monkeypatch.setattr(
        dia, "_resolve_brand_id", lambda request, diagnosis_id: (BRAND_ID, {"id": diagnosis_id})
    )

    def fake_decide(**_kwargs):
        raise service_side_effect

    monkeypatch.setattr(
        "services.diagnosis_identity_decision.decide_brand_cell", fake_decide
    )

    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user = {"user_id": OWNER_ID, "is_admin": False}
        return await call_next(request)

    app.include_router(dia.router)
    return TestClient(app)


def test_api_value_error_maps_to_400(monkeypatch):
    """P2-1①:服务层 ValueError(入参/语义校验失败)→ 400(契约文档口径,不再 422)。"""
    client = _decision_api_client(monkeypatch, ValueError("无效的确认动作"))

    resp = client.post(
        f"/api/diagnosis/{DIAG_ID}/brand-cells/decision",
        json={
            "question": QUESTION,
            "engine": ENGINE,
            "action": "confirm_yes",
            "request_id": str(uuid.uuid4()),
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "无效的确认动作"


def test_api_unique_violation_conflict_maps_to_409(monkeypatch):
    """P2-1②:并发同 request_id 冲突(服务层转 Conflict)→ 409 与同键异参同语义。"""
    client = _decision_api_client(
        monkeypatch,
        DiagnosisIdentityReviewConflict("请求编号与其他确认并发冲突，请刷新后重试"),
    )

    resp = client.post(
        f"/api/diagnosis/{DIAG_ID}/brand-cells/decision",
        json={
            "question": QUESTION,
            "engine": ENGINE,
            "action": "confirm_yes",
            "request_id": str(uuid.uuid4()),
        },
    )

    assert resp.status_code == 409
    assert "并发冲突" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 集中严审 R1 · P3-1:local-only 重判 socket 级禁网
# ---------------------------------------------------------------------------

def test_local_only_rejudge_makes_no_outbound_http(fake_db, monkeypatch):
    """local-only 重判全程禁止任何出站 HTTP:requests/httpx 双保险,命中即 AssertionError。"""
    import requests

    def forbidden_request(*_args, **_kwargs):
        raise AssertionError("local-only 重判禁止外发 HTTP")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_request)
    try:
        import httpx
    except ImportError:  # pragma: no cover - 环境无 httpx 时跳过该层
        httpx = None
    if httpx is not None:
        monkeypatch.setattr(httpx.Client, "request", forbidden_request)
        monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_request)

    result = _decide(fake_db)
    assert result["success"] is True
    assert result["status"] == "resolved"
    assert result["cell"]["state"] == "YES"


# ---------------------------------------------------------------------------
# 集中严审 R3 · P0:两份 2026-07-22 迁移必须注册进 manifest(prestart 消费)
# ---------------------------------------------------------------------------

def test_migration_manifest_registers_2026_07_22_migrations():
    """diagnosis_identity_review 紧随监测族 identity_review 之后;whitelabel 在册。"""
    from db.migration_manifest import MIGRATIONS

    monitoring = "scripts/migration_monitoring_identity_review_2026_07_21.sql"
    diagnosis = "scripts/migration_diagnosis_identity_review_2026_07_22.sql"
    whitelabel = "scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql"
    assert diagnosis in MIGRATIONS, "诊断人工确认迁移未注册 → prestart 跳过 → 启动自检 fail-closed"
    assert whitelabel in MIGRATIONS, "白标板块 C 迁移未注册 → prestart 跳过 → 启动自检 fail-closed"
    # 诊断迁移泛化 events 表,必须排在其创建者之后
    assert MIGRATIONS.index(monitoring) < MIGRATIONS.index(diagnosis)


# ---------------------------------------------------------------------------
# 集中严审 R3 · P2:v2 回写失败 SAVEPOINT 降级(25P02 不再整体 500)
# ---------------------------------------------------------------------------

def test_v2_write_sql_failure_degrades_via_savepoint(fake_db, monkeypatch):
    """update_diagnosis_v2_in_db 内部 SQL 抛错 → ROLLBACK TO SAVEPOINT 后降级写评分列,
    决策整体仍成功(200 语义),原始决策/别名/单元格重算不回滚。"""
    def boom(_diagnosis_id, _v2_result, *, conn=None):
        raise RuntimeError("simulated v2 write SQL failure (25P02 class)")

    monkeypatch.setattr("services.diagnosis_report_v2.update_diagnosis_v2_in_db", boom)

    result = _decide(fake_db)
    assert result["success"] is True
    assert result["status"] == "resolved"
    assert result["report_rebuilt"] is False

    # 降级写入库:评分 SSOT 列 + report_v2_error 标记
    row = fake_db.diagnosis[DIAG_ID]
    assert row["total_score"] == result["score"]
    assert row["level"] == result["level"]
    assert "v2 报告重生异常" in row["report_v2_error"]
    # R3 · P3:失败却标新鲜会误导消费者 —— 降级分支不得再写 report_v2_generated_at
    assert "report_v2_generated_at" not in row

    # brands.latest_score 同步降级落库
    assert fake_db.brands[BRAND_ID]["latest_score"] == result["score"]
    assert fake_db.brands[BRAND_ID]["latest_diagnosis_id"] == DIAG_ID

    # 原始决策/别名/单元格重算不回滚(SAVEPOINT 只回滚 v2 写段)
    assert fake_db.name_decisions[(BRAND_ID, "滨江南路雅栖酒店")]["decision"] == "positive"
    stored = json.loads(row["raw_data_json"])
    cell = stored["data"]["ai_visibility"]["detail_table"][0]["results"][ENGINE]
    assert cell["brand_verdict"] == "YES"
    assert cell["identity_review_state"] == "confirmed"

    # 审计事件仍落库 + 单事务恰好 1 次 commit
    assert len(fake_db.events) == 1
    assert fake_db.conn.commits == 1
    assert fake_db.conn.rollbacks == 0

    # SAVEPOINT 三段式按序出现:SAVEPOINT → ROLLBACK TO → RELEASE
    log = fake_db.sql_log
    sp = log.index("savepoint sp_v2_write")
    rb = log.index("rollback to savepoint sp_v2_write")
    rel = log.index("release savepoint sp_v2_write")
    assert sp < rb < rel


def test_degraded_branch_never_stamps_generated_at():
    """R3 · P3 静态闸:决策模块源码不再出现 report_v2_generated_at(防回潮)。"""
    import inspect

    import services.diagnosis_identity_decision as decision_module

    assert "report_v2_generated_at" not in inspect.getsource(decision_module)


# ---------------------------------------------------------------------------
# 集中严审 R3 · P2:legacy 查询失败格守卫(NOT_COLLECTED 不进分母)
# ---------------------------------------------------------------------------

def test_legacy_query_failure_cell_is_not_collected_not_no():
    """无 brand_verdict 的 legacy 格 + answer_summary 查询失败语义 → NOT_COLLECTED。

    仅用 answer_summary 谓词(不加 full_response 非空要求),避免旧数据反向通胀;
    对照组:普通 legacy NO 格保持历史口径仍进分母。
    """
    from services.diagnosis_identity_review import (
        STATE_NOT_COLLECTED,
        VERDICT_NO,
        aggregate_dimension_stats,
        classify_cell_state,
    )

    legacy_failure = {
        "brand_detected": False,
        "answer_summary": "查询失败: 引擎超时",
        "full_response": "",
    }
    assert classify_cell_state(legacy_failure) == STATE_NOT_COLLECTED

    legacy_failure_error_prefix = {
        "brand_detected": False,
        "answer_summary": "Error: connection reset",
    }
    assert classify_cell_state(legacy_failure_error_prefix) == STATE_NOT_COLLECTED

    detail_table = [{"question": "q1", "results": {"deepseek": legacy_failure}}]
    stats = aggregate_dimension_stats(detail_table, {"q1": "brand_awareness"})
    bucket = stats["brand_awareness"]
    assert bucket["total"] == 0  # 不进分母
    assert bucket["detected"] == 0
    assert bucket["not_collected"] == 1

    # 对照:普通 legacy NO 格(无查询失败语义)保持历史口径
    plain_legacy_no = {
        "brand_detected": False,
        "answer_summary": "回答中未提到该品牌。",
        "full_response": "推荐其他几家酒店……",
    }
    assert classify_cell_state(plain_legacy_no) == VERDICT_NO
    # legacy YES 不受影响
    assert classify_cell_state({"brand_detected": True, "answer_summary": "提到了"}) == "YES"


# ---------------------------------------------------------------------------
# 集中严审 R3 · P2:name_decisions INSERT 撞 UNIQUE(request_id) 也转 409
# ---------------------------------------------------------------------------

def test_name_decision_insert_unique_violation_is_409(fake_db):
    """step 7 name_decisions 表 UNIQUE(request_id) 并发冲突 → 409 人话,不冒 500。"""
    import psycopg2

    fake_db.name_decision_insert_error = psycopg2.errors.UniqueViolation(
        'duplicate key value violates unique constraint '
        '"monitoring_identity_name_decisions_request_id_key"'
    )
    with pytest.raises(DiagnosisIdentityReviewConflict, match="并发冲突"):
        _decide(fake_db)
    assert fake_db.conn.commits == 0
    assert fake_db.conn.rollbacks >= 1  # 冲突后事务必须回滚,不留半写状态


def test_name_decision_insert_fk_violation_still_500(fake_db):
    """收窄语义保持:FK 冲突(brand 中途被删)不是并发重试场景,必须放行冒 500。"""
    import psycopg2

    fake_db.name_decision_insert_error = psycopg2.errors.ForeignKeyViolation(
        'insert or update on table "monitoring_identity_name_decisions" '
        'violates foreign key constraint'
    )
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _decide(fake_db)
    assert fake_db.conn.commits == 0
