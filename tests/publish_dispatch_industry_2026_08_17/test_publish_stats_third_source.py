"""「已分发」状态黑洞 —— `/article-publish-stats` 第三源行为锁。

WO_PUBLISH_DISPATCH_STATUS_AND_INDUSTRY_2026-08-17 Part① R1。

病灶(2026-08-17 生产只读实证 · `SET TRANSACTION READ ONLY` + 必失败 UPDATE 自证):
  统计口径的分母只有两源 —— 镜像表 `mhz_synced_orders` ∪ 自助 `publish_records`。
  而**快易播(kyb)那条渠道从不写镜像表**:它的状态回流是
  `services/kuaiyibo/status_sync.py` 直接 UPDATE `mhz_publish_order_items.status`。
  实测 items 581-591(user_id=1 · 文章 1564/1570/1565/1568/1566/1582/1579/1587/1584/1567/1573 ·
  媒体「列举网(可指定地区)」· media_id 100656461 > 1e8 = kyb 主键偏移 ·
  单号 `26…` 族)已是 `published`,在镜像表 **0 命中** → 发布中心 tab 恒「未分发」。
  全量看:有单号但镜像 0 命中的 item 共 62 条(submitted 47 / published 13 / failed 2)。

锁表(工单判据逐条对号):
  判据1a  item=published + 镜像 0 行 → stats 该文章 = published,且媒体名带出来
  判据1b  镜像行与 item **并存** → 计 1 不计 2(镜像优先)
  判据2   拆锁在 `run_mutations.py`(把第三源从 UNION 拿掉 → 1a 必转红)
  判据4   既有分类零漂移:两源就能判定的文章,加第三源前后主状态**逐篇相同**
  枚举锁  `ITEM_STATUS_TO_SYNCED_CODE` 必须覆盖 item.status 闭集,漏一个当场红

🔴 夹具用**生产整库 pg_dump --schema-only**(2026-08-17 现取,488 张表)。
   不手搓建表:本仓栽过"手写 schema 类型不同构照样全绿,到生产副本上才炸"。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PG_URL = os.environ.get("TEST_DATABASE_URL")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SCHEMA_SQL = Path(__file__).resolve().parent / "prod_schema_2026-08-17.sql"

UID = 4101          # 本包的代发用户
OTHER_UID = 4102    # 越权对照:他的单不许进我的统计


def _skip_unless_throwaway_pg():
    if not PG_URL:
        pytest.skip("需要 TEST_DATABASE_URL(一次性 loopback 测试库)")
    parsed = urlsplit(PG_URL)
    if (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
        pytest.skip(f"只允许 loopback 一次性容器 DSN:{parsed.hostname}")
    base_db = (parsed.path or "").lstrip("/").lower()
    if "prod" in base_db or "test" not in base_db:
        pytest.skip(f"基础库名必须含 test 且不含 prod:{base_db}")


@pytest.fixture(scope="module")
def db():
    _skip_unless_throwaway_pg()
    assert SCHEMA_SQL.exists(), f"缺 schema 快照:{SCHEMA_SQL}"
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    name = f"pubdisp_test_{uuid.uuid4().hex[:10]}"
    assert "test" in name and "prod" not in name
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parsed = urlsplit(PG_URL)
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}", parsed.query, parsed.fragment))

    raw = SCHEMA_SQL.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in raw.splitlines()
        if not line.startswith("\\") and not line.startswith("CREATE SCHEMA public;")
    )
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    with conn.cursor() as cur:
        for _ext in ("vector", "pg_trgm"):
            cur.execute(f"CREATE EXTENSION IF NOT EXISTS {_ext}")
        cur.execute(body)
        # 🔴 pg_dump 的 set_config('search_path','',false) 对**本连接**生效,不还原
        #    后面全是 "relation does not exist"(本仓踩过)。
        cur.execute("SET search_path TO public")
        # [WO_ARTICLE_BROWSER_SELF_REPORT_2026-08-19 R2] 本包夹具的 schema 快照取自
        # 2026-08-17,不含自报收口新加的 publish_records.public_url_verification_state。
        # 生产的顺序是 prestart 先跑完全部迁移、应用才起来,所以夹具也照着跑一遍 ——
        # 否则这里测的是一个**线上不存在**的中间形状。
        _selfreport_migration = (
            ROOT / "scripts" / "migration_publish_records_url_verification_2026_08_19.sql")
        if _selfreport_migration.exists():
            cur.execute(_selfreport_migration.read_text(encoding="utf-8"))
    try:
        yield type("DB", (), {
            "url": url,
            "connect": staticmethod(lambda: psycopg2.connect(url, cursor_factory=RealDictCursor)),
            "conn": conn,
        })
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.close()


def _reset(db):
    with db.conn.cursor() as cur:
        for t in ("mhz_publish_order_items", "mhz_publish_orders",
                  "mhz_synced_orders", "publish_records"):
            cur.execute(f"DELETE FROM {t}")


def _order(db, order_id: int, article_id: int, user_id: int = UID):
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mhz_publish_orders (id, user_id, article_id, article_title) "
            "VALUES (%s, %s, %s, %s)", (order_id, user_id, article_id, f"文章{article_id}"))


def _item(db, item_id: int, order_id: int, *, status: str, media_name: str,
          mhz_order_id=None, user_id: int = UID):
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mhz_publish_order_items "
            "(id, order_id, user_id, media_id, media_name, mhz_order_id, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (item_id, order_id, user_id, 100656461, media_name, mhz_order_id, status))


def _synced(db, sid: str, *, order_sn: str, status: int, media_name: str,
            article_id=None, user_id: int = UID):
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mhz_synced_orders (id, order_sn, status, media_name, article_id, user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (sid, order_sn, status, media_name, article_id, user_id))


def _stats(db, monkeypatch, uid: int = UID) -> dict:
    """真调 `/article-publish-stats` 的 handler —— 锁打在要上线的那份实现上。"""
    import asyncio

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_connection", db.connect)
    import api.meijiehezi_api as api_mod
    monkeypatch.setattr(api_mod, "_get_user", lambda _req: {"user_id": uid, "is_admin": False})
    out = asyncio.run(api_mod.api_article_publish_stats(object()))
    assert out["status"] == "success"
    return out["stats"]


# ── 判据 1a:kyb 形态(item published + 镜像 0 行)→ 已分发 ───────────────────
def test_C1a_kyb_item_published_without_mirror_counts_as_published(db, monkeypatch):
    _reset(db)
    _order(db, 9001, 1564)
    _item(db, 8001, 9001, status="published", media_name="列举网(可指定地区)",
          mhz_order_id="26081712130149")
    # 反向对照物:镜像表**确实**一行都没有(否则这条判据什么都没证明)
    with db.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM mhz_synced_orders")
        assert cur.fetchone()["n"] == 0

    stats = _stats(db, monkeypatch)
    assert "1564" in stats, "kyb 已发布的文章必须出现在统计里(旧口径这里是空 dict)"
    assert stats["1564"]["status"] == "published"
    assert stats["1564"]["published"] == 1
    assert "列举网(可指定地区)" in stats["1564"]["published_media"]


# ── 判据 1b:镜像行与 item 并存 → 计 1 不计 2 ────────────────────────────────
@pytest.mark.parametrize("join_key", ["by_id", "by_order_sn"])
def test_C1b_mirror_and_item_dedupe_counts_once(db, monkeypatch, join_key):
    """镜像有的以镜像为准。两种连法都要去重 —— 只堵一条腿就会漏出双计。"""
    _reset(db)
    _order(db, 9002, 1700)
    ord_id = "11250817001"
    _item(db, 8002, 9002, status="published", media_name="搜狐网", mhz_order_id=ord_id)
    if join_key == "by_id":
        _synced(db, ord_id, order_sn="SN-1700", status=2, media_name="搜狐网", article_id=1700)
    else:
        _synced(db, "mirror-1700", order_sn=ord_id, status=2, media_name="搜狐网", article_id=1700)

    stats = _stats(db, monkeypatch)
    assert stats["1700"]["published"] == 1, f"同一单被计了两次({join_key})"
    assert stats["1700"]["published_media"] == ["搜狐网"]


# ── 判据 4:既有文章分类零漂移 ───────────────────────────────────────────────
def test_C4_existing_mirror_only_classification_unchanged(db, monkeypatch):
    """纯镜像 / 纯自助的文章,加了第三源之后主状态与计数**逐篇不变**。"""
    _reset(db)
    # 纯镜像:一拒一成功 → 仍是 published(优先级 published > in_progress > rejected)
    _synced(db, "m-1", order_sn="SN-1", status=2, media_name="凤凰网", article_id=2001)
    _synced(db, "m-2", order_sn="SN-2", status=-1, media_name="网易网", article_id=2001)
    # 纯镜像:只有待接单 → in_progress
    _synced(db, "m-3", order_sn="SN-3", status=0, media_name="新浪网", article_id=2002)
    # 纯自助
    with db.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO publish_records (user_id, article_id, platform, status) "
            "VALUES (%s, %s, %s, %s)", (str(UID), 2003, "小红书", "success"))

    stats = _stats(db, monkeypatch)
    assert stats["2001"]["status"] == "published" and stats["2001"]["published"] == 1
    assert stats["2001"]["rejected"] == 1
    assert stats["2002"]["status"] == "in_progress" and stats["2002"]["in_progress"] == 1
    # [WO_ARTICLE_BROWSER_SELF_REPORT_2026-08-19 R2 §②③] 🔴 这条断言的**不变式搬了家**,
    # 不是退役:自助发布的 status='success' 只是浏览器回报的**操作回执**,R2 之后
    # 不再等价代发的"已发布"。没核实过 → 主状态 reported_success_unverified,
    # 且**不进** published 计数。代发两条(2001/2002)的分类一字未动 ——
    # 本用例原本要守的"加第三源不许让代发分类漂移"仍然守着。
    assert stats["2003"]["status"] == "reported_success_unverified"
    assert stats["2003"]["published"] == 0
    assert stats["2003"]["reported_success_unverified"] == 1
    # 成对反向对照:同一行升成已核实 → 立刻回到 published(证明上面不是恒不为 published)
    with db.conn.cursor() as cur:
        cur.execute(
            "UPDATE publish_records SET public_url_verification_state='verified',"
            " public_url_verification_source='human_attestation' "
            "WHERE user_id=%s AND article_id=2003", (str(UID),))
    stats2 = _stats(db, monkeypatch)
    assert stats2["2003"]["status"] == "published"
    assert stats2["2003"]["published"] == 1


def test_C4b_cancelled_item_is_not_counted(db, monkeypatch):
    """工单明确:cancelled 不计。它不该把一篇「未分发」变成别的。"""
    _reset(db)
    _order(db, 9003, 2100)
    _item(db, 8003, 9003, status="cancelled", media_name="某某网", mhz_order_id=None)
    assert "2100" not in _stats(db, monkeypatch)
    # 成对反向对照:同形状但状态改成 failed → 必须出现且是 rejected
    _reset(db)
    _order(db, 9004, 2101)
    _item(db, 8004, 9004, status="failed", media_name="某某网", mhz_order_id=None)
    stats = _stats(db, monkeypatch)
    assert stats["2101"]["status"] == "rejected" and stats["2101"]["rejected"] == 1


def test_C4c_other_users_items_stay_out(db, monkeypatch):
    """第三源不能顺手扩大可见范围 —— 别人的 item 不进我的统计。"""
    _reset(db)
    _order(db, 9005, 2200, user_id=OTHER_UID)
    _item(db, 8005, 9005, status="published", media_name="列举网(可指定地区)",
          mhz_order_id="26081712999999", user_id=OTHER_UID)
    assert "2200" not in _stats(db, monkeypatch, uid=UID)
    # 成对:换成本人视角必须看得见(证明上一条不是恒空)
    assert _stats(db, monkeypatch, uid=OTHER_UID)["2200"]["status"] == "published"


def test_C5_in_flight_item_shows_in_progress(db, monkeypatch):
    """submitted(生产 47 条)是「发布中」,不是「未分发」。"""
    _reset(db)
    _order(db, 9006, 1576)
    _item(db, 8006, 9006, status="submitted", media_name="列举网(可指定地区)",
          mhz_order_id="26081718355983")
    stats = _stats(db, monkeypatch)
    assert stats["1576"]["status"] == "in_progress"
    assert "列举网(可指定地区)" in stats["1576"]["in_progress_media"]


# ── 枚举锁:状态映射必须覆盖闭集 ─────────────────────────────────────────────
def test_C6_status_map_covers_the_closed_set():
    from db.meijiehezi_db import (ITEM_STATUS_ALL, ITEM_STATUS_TO_SYNCED_CODE,
                                  item_countable_statuses, item_status_case_sql)
    missing = [s for s in ITEM_STATUS_ALL if s not in ITEM_STATUS_TO_SYNCED_CODE]
    assert missing == [], f"item.status 有值没登记映射,会被静默从统计里丢掉:{missing}"
    # 反向对照:闭集本身不能是空的(空集合让上面那条恒真)
    assert len(ITEM_STATUS_ALL) >= 7
    # 生产 2026-08-17 实测存在的 7 个值必须都在闭集里
    for s in ("published", "rejected", "failed", "submitted",
              "cancelled", "withdrawn", "awaiting_action"):
        assert s in ITEM_STATUS_ALL
    # SQL 由映射表生成,不是手抄的第二份真相
    case = item_status_case_sql("i2.status")
    for k, v in ITEM_STATUS_TO_SYNCED_CODE.items():
        if v is None:
            assert f"'{k}'" not in case, f"{k} 标了不计,却出现在 CASE 里"
            assert k not in item_countable_statuses()
        else:
            assert f"WHEN '{k}' THEN {v}" in case
