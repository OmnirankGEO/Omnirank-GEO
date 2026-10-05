"""造真世界:真用户 / 真钱包 / 真品牌 / 真价目 + 打真 defgeo 端点的 client。

一条纪律贯穿本文件:**不给被测代码代劳**。
冻结由 ``middleware.billing.freeze_points`` 真写,run 行由 ``admit_run`` 真插,
状态由状态机真推 —— 这里只负责把"生产会有的那些前置事实"摆好。
夹具替被测代码干活的地方,判据就是恒绿的(本仓 2026-08-20 实录)。
"""
from __future__ import annotations

import uuid

import psycopg2
from psycopg2.extras import RealDictCursor

FEATURE = "geo_diagnosis"

#: 每造一次世界换一批 uid。复用 uid 会让判据结果变成"谁先跑"的函数
#: (钱包会被后造的世界覆盖),而不是被测代码的函数。
_UID_BASE = 951_000
_UID_SEQ = [0]


def fresh_uids(n=3):
    _UID_SEQ[0] += n
    base = _UID_BASE + _UID_SEQ[0]
    return [base + i for i in range(n)]


def conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def ensure_user(cur, uid, name, *, admin=False):
    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (%s,%s,%s,'x',%s,1) ON CONFLICT (id) DO NOTHING",
        (uid, name, name, name + "@example.com"))
    if admin:
        # `roles.display_name` 是 NOT NULL(生产 schema 实测)—— 类型/约束这一维
        # 必须照真 schema 写,不照"看起来该是什么"写。
        cur.execute("INSERT INTO roles (name, display_name) VALUES ('admin','管理员') "
                    "ON CONFLICT (name) DO NOTHING")
        cur.execute("SELECT id FROM roles WHERE name='admin'")
        role_id = int(cur.fetchone()["id"])
        cur.execute("INSERT INTO user_roles (user_id, role_id) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING", (uid, role_id))


def ensure_wallet(cur, uid, *, paid=0, bonus=0, commission=0, frozen=0):
    cur.execute(
        "INSERT INTO user_wallets (user_id, paid_points, bonus_points, commission_points, frozen_points) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET "
        "paid_points=EXCLUDED.paid_points, bonus_points=EXCLUDED.bonus_points, "
        "commission_points=EXCLUDED.commission_points, frozen_points=EXCLUDED.frozen_points",
        (uid, paid, bonus, commission, frozen))


def set_pricing(cur, cost_points, *, feature=FEATURE):
    """价目是**真表真行** —— preview 计价与 freeze_points 读的是同一行。

    A-2 的判据要靠"真的改一次价"来证版本会跟着变;monkeypatch 证不了那件事。
    """
    cur.execute(
        "INSERT INTO feature_pricing (feature_code, feature_name, cost_points) "
        "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO UPDATE SET cost_points=EXCLUDED.cost_points",
        (feature, "GEO 诊断", int(cost_points)))


def new_brand(cur, owner_uid, prefix="P0FIX"):
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                (prefix + "_" + uuid.uuid4().hex[:8], owner_uid))
    return int(cur.fetchone()["id"])


def make_app():
    """只挂 defgeo router 的 app + 测试身份注入中间件。

    身份键名逐字取自 ``auth/middleware`` 真正写进 ``request.state.user`` 的那两个
    (``user_id`` / ``is_admin``)——夹具发一个生产不会发的键,会让端点在生产必炸
    而判据全绿(本仓 2026-08-20 实录)。
    """
    from fastapi import FastAPI

    from api.defensive_geo_api import router

    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        tid = request.headers.get("X-Test-Tenant")
        if tid:
            request.state.user = {
                "user_id": int(tid),
                "is_admin": request.headers.get("X-Test-Admin") == "1",
            }
        return await call_next(request)

    app.include_router(router)
    return app


def headers(tenant, *, idem=None, admin=False):
    h = {"X-Test-Tenant": str(tenant)}
    if idem:
        h["Idempotency-Key"] = idem
    if admin:
        h["X-Test-Admin"] = "1"
    return h


def make_preview(client, tenant, brand_id, *, admin=False, platform_keys=("deepseek",)):
    plan = client.post(
        "/api/defensive-geo/question-plans/preview",
        json={"clientRequestId": "creq-" + uuid.uuid4().hex[:10], "brandId": int(brand_id),
              "profileRevisionId": "prof-1", "mode": "defensive",
              "questions": [{"text": "这个牌子靠谱吗", "modeSide": "defensive",
                             "familyKey": "identity_check", "brandExposure": "named"}]},
        headers=headers(tenant, admin=admin))
    assert plan.status_code == 200, plan.text
    plan = plan.json()
    prev = client.post(
        "/api/defensive-geo/run-previews",
        json={"questionPlanId": plan["planId"], "questionPlanRevision": 1,
              "profileRevisionId": "prof-1", "platformKeys": list(platform_keys)},
        headers=headers(tenant, idem="idem-" + uuid.uuid4().hex[:10], admin=admin))
    assert prev.status_code == 200, prev.text
    return prev.json()


def confirm(client, tenant, preview, *, admin=False, idem=None):
    return client.post(
        "/api/defensive-geo/run-previews/%s/confirm" % preview["previewId"],
        json={"expectedHash": preview["canonicalHash"]},
        headers=headers(tenant, idem=idem or ("cf-" + uuid.uuid4().hex[:10]), admin=admin))


class Counts:
    """三面计数快照。**一次取三个** —— 分开取会在并发下拍到不一致的瞬间。"""

    def __init__(self, db):
        with db.cursor() as cur:
            cur.execute("SELECT count(*) AS c FROM diagnosis_runs")
            self.runs = cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM point_freezes")
            self.freezes = cur.fetchone()["c"]
            cur.execute("SELECT count(*) AS c FROM notification_outbox")
            self.outbox = cur.fetchone()["c"]

    def delta(self, other):
        return (other.runs - self.runs, other.freezes - self.freezes,
                other.outbox - self.outbox)


def run_row(db, run_token):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM diagnosis_runs WHERE run_token=%s", (run_token,))
        row = cur.fetchone()
    return dict(row) if row else None


def freeze_row(db, freeze_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM point_freezes WHERE id=%s", (int(freeze_id),))
        row = cur.fetchone()
    return dict(row) if row else None


def seed_product(db, run):
    """结算前的产物证明(``require_complete_product`` 会真读这两张表)。

    形状照生产:``report_v2_modules_jsonb`` 必须是 ``{"client": {"modules": {...}}}``
    (services/report_html_renderer.py:447)。写错这层包装 → ``is_client_report_ready``
    返 False → 结算改道退款,判据会因为**错误的原因**变绿。
    """
    import json as _json

    ready = _json.dumps({"client": {"modules": {"1": {"insight": "有结论的正文"}}}},
                        ensure_ascii=False)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO diagnosis_records (session_id, brand_name, industry, brand_id, run_token, "
            "result_visibility, total_score, level, report_v2_modules_jsonb, "
            "ai_total_tests, ai_engines_tested, total_questions_tested) "
            "VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s) RETURNING id",
            (run["session_id"], "P0FIX客户", "测试行业", run["brand_id"], run["run_token"],
             61, "成长级", ready, 32,
             _json.dumps(["qwen", "deepseek", "kimi", "doubao"], ensure_ascii=False), 8))
        did = int(cur.fetchone()["id"])
        snap = _json.dumps({"type": "complete", "done": True, "terminal": True,
                            "diagnosis_id": did, "share_token": "shr_" + uuid.uuid4().hex[:8],
                            "result": {"total_score": 61, "level": "成长级"}},
                           ensure_ascii=False)
        cur.execute("UPDATE diagnosis_runs SET final_snapshot_jsonb=%s WHERE run_token=%s",
                    (snap, run["run_token"]))
    return did


def wallet(db, uid):
    with db.cursor() as cur:
        cur.execute("SELECT paid_points, bonus_points, commission_points, frozen_points "
                    "FROM user_wallets WHERE user_id=%s", (int(uid),))
        row = cur.fetchone()
    return dict(row) if row else None
