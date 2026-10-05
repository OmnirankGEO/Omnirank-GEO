"""WO_310 · 媒介盒子退款:代申请记付款人 · 兜底路径不批准 · 条目路径封顶不变(2026-09-27)。

背景(开源/WO_310_MHZ_REFUND_FACTS_2026-09-27.md):
  - 管理员代申请时原来记 user_id = 管理员 ⇒ 批准后算力退进管理员钱包,付款的客户拿不到;
  - 找不到本平台下单条目时回落兜底键 refund_request:{id},统一退款 helper 的封顶只认 item:{id} ⇒ 不封顶;
  - 受益人一律是付款人 mhz_publish_orders.user_id;mhz_synced_orders.user_id 不用于钱(Review 09-27 定)。

在本机测试 PG 上建一个私有临时库(名字带 test,测完删),整个场景在子进程里跑(DATABASE_URL 指过去):
按真实建表入口引导(db.diagnosis_db · init_auth_db · init_wallet_tables · init_mhz_tables),周边两张表
(notification_outbox · customer_credit_transactions)从生产快照原样抠,再用真执行器打 migration_065。
然后直接调真的 API 处理函数(申请 / 审批),读库断言。

守七格:
  ① 管理员代申请 ⇒ 申请行 user_id = 付款人,requested_by = 管理员;
  ② 批准 ⇒ 算力进付款人钱包,管理员钱包不动;
  ③ 兜底路径(找不到本平台下单条目):申请 409;一条历史遗留的 pending 申请,批准也 409,状态仍 pending、谁的钱都不动;
  ④ 条目路径封顶不变:申请额 > 真实扣费 ⇒ 只退真实扣费;管理员免扣订单(真实扣费 0)⇒ 退 0;
  ⑤ 历史上记在管理员名下、仍 pending 的申请,批准后退给付款人(不改历史数据);
  ⑥ 本人校验:非管理员对付款人不是自己的订单申请 ⇒ 409,不建申请行(对照臂:付款人本人申请成功);
  ⑦ 系统账户:付款人 user_id ≤ 0 ⇒ 申请 409;批准 409,不动钱、不写流水。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql"
MIG = "db/migration_065_mhz_refund_requested_by_2026_09_27.sql"
BASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="需要 TEST_DATABASE_URL(本机测试 PG)")


def _side_table_sql() -> list[str]:
    s = SNAPSHOT.read_text(encoding="utf-8")
    out = []
    for t in ("notification_outbox", "customer_credit_transactions"):
        pats = [rf"CREATE TABLE public\.{t} \(.*?\n\);",
                rf"CREATE SEQUENCE public\.{t}_id_seq.*?;",
                rf"ALTER SEQUENCE public\.{t}_id_seq OWNED BY public\.{t}\.id;",
                rf"ALTER TABLE ONLY public\.{t} ALTER COLUMN id SET DEFAULT [^;]*;",
                rf"ALTER TABLE ONLY public\.{t}\n    ADD CONSTRAINT {t}_\w+ (?:PRIMARY KEY|UNIQUE) [^;]*;"]
        for p in pats:
            found = re.findall(p, s, flags=re.S)
            assert found, f"快照里找不到:{p}"
            out.extend(found)
    return out


_CHILD = r'''
import asyncio, json, os, sys
from types import SimpleNamespace
root = os.environ["W310_ROOT"]
sys.path.insert(0, root); os.chdir(root)
import psycopg2, psycopg2.extras
from pathlib import Path

import db.diagnosis_db  # noqa: F401  与 prestart 同:基础 schema
from db.auth_db import init_auth_db
init_auth_db()  # users 表(钱包 / 流水对它有外键)
from db.wallet_db import init_wallet_tables
from db.meijiehezi_db import init_mhz_tables
init_wallet_tables(); init_mhz_tables()
raw = psycopg2.connect(os.environ["DATABASE_URL"]); raw.autocommit = True
cur = raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
for stmt in json.loads(os.environ["W310_SIDE"]):
    try:
        cur.execute(stmt)
    except psycopg2.errors.DuplicateTable:
        pass
from scripts.prestart import _apply
_apply(cur, Path(root), os.environ["W310_MIG"])

# 用户:按 users 表真实的 NOT NULL 无默认列补齐
cur.execute("""SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema='public' AND table_name='users' AND is_nullable='NO' AND column_default IS NULL""")
req_cols = [(r["column_name"], r["data_type"]) for r in cur.fetchall() if r["column_name"] != "id"]
def mk_user(uid):
    # init_auth_db 自己会种 id=1 的默认用户 ⇒ 已存在就不再插;钱包行照样补齐、清零
    cur.execute("SELECT 1 FROM users WHERE id = %s", (uid,))
    if not cur.fetchone():
        cols, vals = ["id"], [uid]
        for name, typ in req_cols:
            cols.append(name)
            vals.append(f"u{uid}_{name}" if "char" in typ or typ == "text" else (False if typ == "boolean" else 0))
        cur.execute(f"INSERT INTO users ({','.join(cols)}) VALUES ({','.join(['%s']*len(vals))})", vals)
    cur.execute("""INSERT INTO user_wallets (user_id, paid_points) VALUES (%s, 0)
                   ON CONFLICT (user_id) DO UPDATE SET paid_points = 0""", (uid,))
PAYER, ADMIN, BOGUS, OTHER = 910001, 910002, 1, 910003
RESERVED = -1  # 系统账户(id ≤ 0):被测守卫对任何 ≤ 0 的付款人一律拒
for u in (PAYER, ADMIN, BOGUS, OTHER):
    mk_user(u)

def mk_order(oid, deducted, exempt=False, payer=None):
    cur.execute("""INSERT INTO mhz_publish_orders (id, user_id, article_title, status, total_items, total_cost_points,
                   admin_exempt, actually_deducted_points) VALUES (%s,%s,'t','completed',1,%s,%s,%s)""",
                (oid, payer if payer is not None else PAYER, deducted, exempt, deducted))
def mk_item(iid, oid, sn, payer=None):
    cur.execute("""INSERT INTO mhz_publish_order_items (id, order_id, user_id, media_id, media_name, cost_points, status, mhz_order_id)
                   VALUES (%s,%s,%s,1,'m',100,'published',%s)""", (iid, oid, payer if payer is not None else PAYER, sn))
def mk_synced(sid, sn, price, owner=None):
    # 同步单上的 user_id 默认故意填 1(定时同步的真实行为)——不许被当成受益人
    cur.execute("""INSERT INTO mhz_synced_orders (id, order_sn, title, price, status, user_id)
                   VALUES (%s,%s,'t',%s,2,%s)""", (sid, sn, price, owner if owner is not None else BOGUS))
def n_requests(sid):
    cur.execute("SELECT COUNT(*) AS n FROM mhz_refund_requests WHERE order_id = %s", (sid,)); return int(cur.fetchone()["n"])
def wallet(uid):
    cur.execute("SELECT paid_points FROM user_wallets WHERE user_id=%s", (uid,)); return int(cur.fetchone()["paid_points"])
def req_row(rid):
    cur.execute("SELECT user_id, requested_by, status FROM mhz_refund_requests WHERE id=%s", (rid,)); return dict(cur.fetchone())

from fastapi import HTTPException
from api import meijiehezi_api as api
def call_request(user, sid):
    r = SimpleNamespace(state=SimpleNamespace(user=user), headers={})
    try:
        return asyncio.run(api.api_request_refund(api.RefundRequest(order_id=str(sid), reason="t"), r))
    except HTTPException as e:
        return {"http": e.status_code, "detail": str(e.detail)}
def call_review(user, rid, approved=True):
    r = SimpleNamespace(state=SimpleNamespace(user=user), headers={})
    try:
        return asyncio.run(api.api_review_refund(api.RefundReviewRequest(request_id=rid, approved=approved, admin_note=""), r))
    except HTTPException as e:
        return {"http": e.status_code, "detail": str(e.detail)}
admin = {"user_id": ADMIN, "is_admin": True}
out = {}

# ①② 条目路径:真实扣费 100;price 很大 ⇒ 申请额远超 100 ⇒ ④ 封顶只退 100
mk_order(501, 100); mk_item(601, 501, "SN-A"); mk_synced("S-A", "SN-A", 999)
a = call_request(admin, "S-A"); out["req_A"] = a
out["row_A"] = req_row(a["request_id"]) if "request_id" in a else None
out["review_A"] = call_review(admin, a.get("request_id", -1))
out["wallet_after_A"] = {"payer": wallet(PAYER), "admin": wallet(ADMIN), "bogus": wallet(BOGUS)}

# ③ 兜底路径:同步单有 order_sn,但没有本平台下单条目
mk_synced("S-B", "SN-B", 50)
out["req_B"] = call_request(admin, "S-B")
cur.execute("""INSERT INTO mhz_refund_requests (order_id, user_id, reason, refund_points, status)
               VALUES ('S-B', %s, 'legacy', 6500, 'pending') RETURNING id""", (ADMIN,))
legacy_b = cur.fetchone()["id"]
before = {"payer": wallet(PAYER), "admin": wallet(ADMIN)}
out["review_B"] = call_review(admin, legacy_b)
out["row_B"] = req_row(legacy_b)
out["wallet_B_unchanged"] = before == {"payer": wallet(PAYER), "admin": wallet(ADMIN)}

# ④ 管理员免扣订单(真实扣费 0)⇒ 退 0
mk_order(502, 0, exempt=True); mk_item(602, 502, "SN-C"); mk_synced("S-C", "SN-C", 10)
c_before = wallet(PAYER)
c = call_request(admin, "S-C"); out["review_C"] = call_review(admin, c.get("request_id", -1))
out["exempt_refunded"] = wallet(PAYER) - c_before

# ⑤ 历史遗留:记在管理员名下的 pending 申请(条目路径)⇒ 批准后退给付款人
mk_order(503, 40); mk_item(603, 503, "SN-D"); mk_synced("S-D", "SN-D", 1)
cur.execute("""INSERT INTO mhz_refund_requests (order_id, user_id, reason, refund_points, status)
               VALUES ('S-D', %s, 'legacy', 30, 'pending') RETURNING id""", (ADMIN,))
legacy_d = cur.fetchone()["id"]
d_before = {"payer": wallet(PAYER), "admin": wallet(ADMIN)}
out["review_D"] = call_review(admin, legacy_d)
out["delta_D"] = {"payer": wallet(PAYER) - d_before["payer"], "admin": wallet(ADMIN) - d_before["admin"]}
# ⑥ 本人校验:同步单写的是 OTHER(非管理员),付款人却是 PAYER ⇒ 409,不建申请行
mk_order(504, 20); mk_item(604, 504, "SN-E"); mk_synced("S-E", "SN-E", 1, owner=OTHER)
out["req_E"] = call_request({"user_id": OTHER, "is_admin": False}, "S-E")
out["rows_E"] = n_requests("S-E")
# 对照臂:付款人本人(非管理员)申请自己的单 ⇒ 成功
mk_order(505, 20); mk_item(605, 505, "SN-F"); mk_synced("S-F", "SN-F", 1, owner=PAYER)
out["req_F"] = call_request({"user_id": PAYER, "is_admin": False}, "S-F")
out["rows_F"] = n_requests("S-F")

# ⑦ 系统账户付款人(≤ 0)⇒ 申请 409;历史遗留 pending 批准也 409,不动钱、不写流水
mk_order(506, 50, payer=RESERVED); mk_item(606, 506, "SN-G", payer=RESERVED); mk_synced("S-G", "SN-G", 1)
out["req_G"] = call_request(admin, "S-G")
cur.execute("""INSERT INTO mhz_refund_requests (order_id, user_id, reason, refund_points, status)
               VALUES ('S-G', %s, 'legacy', 50, 'pending') RETURNING id""", (ADMIN,))
legacy_g = cur.fetchone()["id"]
g_before = {"payer": wallet(PAYER), "admin": wallet(ADMIN)}
out["review_G"] = call_review(admin, legacy_g)
out["row_G"] = req_row(legacy_g)
out["wallet_G_unchanged"] = g_before == {"payer": wallet(PAYER), "admin": wallet(ADMIN)}
cur.execute("SELECT COUNT(*) AS n FROM point_transactions WHERE user_id = %s OR order_id = 'item:606'", (RESERVED,))
out["tx_G"] = int(cur.fetchone()["n"])
print("RESULT " + json.dumps(out, default=str))
'''


@pytest.fixture(scope="module")
def result():
    name = f"wo310_refund_test_{uuid.uuid4().hex[:8]}"
    assert "test" in name and "prod" not in name
    u = urlparse(BASE_URL)
    url = urlunparse(u._replace(path="/" + name))
    admin = psycopg2.connect(BASE_URL)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    try:
        env = dict(os.environ, DATABASE_URL=url, W310_ROOT=str(ROOT), W310_MIG=MIG,
                   W310_SIDE=json.dumps(_side_table_sql()), PYTHONIOENCODING="utf-8")
        env.pop("TEST_DATABASE_URL", None)
        r = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
        assert r.returncode == 0 and lines, (r.returncode, r.stdout[-1500:], r.stderr[-2500:])
        yield json.loads(lines[-1][len("RESULT "):])
    finally:
        cur = admin.cursor()
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def test_proxy_request_is_recorded_under_the_payer(result) -> None:
    assert result["req_A"].get("status") == "success", result["req_A"]
    assert result["row_A"]["user_id"] == 910001 and result["row_A"]["requested_by"] == 910002, result["row_A"]


def test_approval_refunds_the_payer_not_the_admin(result) -> None:
    assert result["review_A"].get("status") == "success", result["review_A"]
    w = result["wallet_after_A"]
    assert w["payer"] > 0 and w["admin"] == 0 and w["bogus"] == 0, w


def test_fallback_path_cannot_be_approved(result) -> None:
    assert result["req_B"].get("http") == 409, result["req_B"]
    assert result["review_B"].get("http") == 409, result["review_B"]
    assert result["row_B"]["status"] == "pending", result["row_B"]
    assert result["wallet_B_unchanged"] is True


def test_item_path_cap_is_unchanged(result) -> None:
    assert result["wallet_after_A"]["payer"] == 100, "申请额远超真实扣费 100,只该退 100"
    assert result["review_C"].get("status") == "success", result["review_C"]
    assert result["exempt_refunded"] == 0, "管理员免扣订单退了钱"


def test_legacy_pending_request_under_admin_refunds_the_payer(result) -> None:
    assert result["review_D"].get("status") == "success", result["review_D"]
    assert result["delta_D"] == {"payer": 30, "admin": 0}, result["delta_D"]


def test_non_admin_cannot_request_refund_for_someone_elses_payment(result) -> None:
    assert result["req_E"].get("http") == 409, result["req_E"]
    assert result["rows_E"] == 0, "被拒的申请还是建了行"
    # 对照臂:付款人本人能正常申请(防「一律 409」也算绿)
    assert result["req_F"].get("status") == "success", result["req_F"]
    assert result["rows_F"] == 1


def test_reserved_payer_ids_never_use_the_app_refund_channel(result) -> None:
    assert result["req_G"].get("http") == 409, result["req_G"]
    assert result["review_G"].get("http") == 409, result["review_G"]
    assert result["row_G"]["status"] == "pending", result["row_G"]
    assert result["wallet_G_unchanged"] is True
    assert result["tx_G"] == 0, "系统账户的单写了退款流水"
