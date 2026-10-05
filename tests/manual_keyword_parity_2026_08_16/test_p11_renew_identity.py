# -*- coding: utf-8 -*-
"""P0-11 续费不再产出孤儿词 + 恒等式锁 + K3 + fail-closed。

被测:server.renew_monitoring_keyword(真端点函数)。
RBAC 守卫被 monkeypatch 成 no-op —— 本文件测的是"续费之后系统状态对不对",
RBAC 由 _require_keyword_owner_access 自己那组用例负责,不在这里重复。
"""
from __future__ import annotations
import asyncio
import pytest

B = 990200


class _Req:
    """最小 Request 替身:被测路径只经过被 patch 掉的 RBAC,不读 request 其它字段。"""
    class _S:  # noqa
        user = {"id": B, "username": "p11", "role": "admin"}
    state = _S()


def _renew(kid: int, source: str):
    import server
    server._require_keyword_owner_access = lambda *a, **k: None   # noqa
    return asyncio.run(server.renew_monitoring_keyword(kid, _Req(), source=source))


# ---------------------------------------------------------------- 恒等式锁

IDENTITY_SQL = {
    "confirmed": """
        SELECT k.id FROM confirmed_keywords k
         WHERE COALESCE(k.is_monitored, FALSE) = TRUE
           AND NOT EXISTS (SELECT 1 FROM keyword_monitor_subscriptions s
                            WHERE s.keyword_id = k.id
                              AND s.keyword_source = 'confirmed'
                              AND s.status IN ('active','paused_low_balance'))
    """,
    "extra": """
        SELECT e.id FROM extra_keywords e
         WHERE COALESCE(e.is_monitored, FALSE) = TRUE
           AND NOT EXISTS (SELECT 1 FROM keyword_monitor_subscriptions s
                            WHERE s.keyword_id = e.id
                              AND s.keyword_source = 'extra'
                              AND s.status IN ('active','paused_low_balance'))
    """,
}


def _violations(cur):
    """全库:is_monitored=TRUE 但没有在跑的订阅的词(= 孤儿)。两种 source 各跑一遍。"""
    out = {}
    for src, sql in IDENTITY_SQL.items():
        cur.execute(sql)
        out[src] = {r["id"] for r in cur.fetchall()}
    return out


def _seed(cur, *, owner: int | None = B):
    cur.execute("INSERT INTO users (id, username, password_hash, display_name) "
                "VALUES (%s,'p11','x','P11') ON CONFLICT (id) DO NOTHING", (B,))
    cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,'P11品牌',%s) "
                "ON CONFLICT (id) DO NOTHING", (B, owner))
    cur.execute("INSERT INTO quotes (id, brand_name, brand_id, status, service_start_date) "
                "VALUES (%s,'P11品牌',%s,'paid', DATE '2026-01-01')", (B, B))
    cur.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, monitoring_query, "
                "is_monitored, monitoring_status) VALUES "
                "(%s,%s,'合同词_P11','合同问法',FALSE,'archived')", (B + 1, B))
    cur.execute("INSERT INTO extra_keywords (id, client_id, quote_id, brand_id, keyword, target_brand, "
                "monitoring_query, status, is_monitored) VALUES "
                "(%s,%s,%s,%s,'手动词_P11','P11品牌','手动问法','archived',FALSE)", (B + 2, B, B, B))


def _sub(cur, kid, src):
    cur.execute("SELECT id, status, keyword_source, daily_points, feature_code, user_id "
                "FROM keyword_monitor_subscriptions WHERE keyword_id=%s AND keyword_source=%s "
                "AND status='active'", (kid, src))
    return cur.fetchall()


# ---------------------------------------------------------------- 用例

def test_P11_renew_confirmed_creates_subscription_and_keeps_identity(committed):
    cur = committed.cursor()
    _seed(cur)
    before = _violations(cur)

    _renew(B + 1, "confirmed")

    cur.execute("SELECT is_monitored, monitoring_status FROM confirmed_keywords WHERE id=%s", (B + 1,))
    row = cur.fetchone()
    assert row["is_monitored"] is True, "续费没把词重新打开"
    subs = _sub(cur, B + 1, "confirmed")
    assert len(subs) == 1, f"续费没建订阅 = 又一条孤儿生产线:{subs}"
    assert subs[0]["daily_points"] == 130 and subs[0]["feature_code"] == "monitoring_keyword_daily", \
        "计费口径不是 130/词/天 monitoring_keyword_daily"
    assert subs[0]["user_id"] == B, "计费主体不是品牌 owner(2026-06-10 audit 特意锚死的那条)"

    after = _violations(cur)
    assert after["confirmed"] - before["confirmed"] == set(), \
        f"续费**新造**了孤儿词:{after['confirmed'] - before['confirmed']}"
    assert after["extra"] - before["extra"] == set()


def test_P11_K3_extra_renew_does_not_touch_service_start_date(committed):
    """🔴 K3 反例 + 判别力对照:同一个操作对两种 source 结果必须不同。"""
    cur = committed.cursor()
    _seed(cur)

    _renew(B + 2, "extra")
    cur.execute("SELECT service_start_date FROM quotes WHERE id=%s", (B,))
    assert str(cur.fetchone()["service_start_date"]) == "2026-01-01", \
        "K3 被违反:续一个手动词把整张单的服务期推倒重算了(同单所有合同词已履约天数清零)"
    cur.execute("SELECT status, is_monitored FROM extra_keywords WHERE id=%s", (B + 2,))
    r = cur.fetchone()
    assert r["status"] == "active" and r["is_monitored"] is True, "手动词没被真正续活"
    assert len(_sub(cur, B + 2, "extra")) == 1, "手动词续费没建订阅"

    # 对照:同一个端点对合同词**必须**重置服务期(否则上面那条零判别力)
    _renew(B + 1, "confirmed")
    cur.execute("SELECT service_start_date = CURRENT_DATE AS reset FROM quotes WHERE id=%s", (B,))
    assert cur.fetchone()["reset"] is True, \
        "合同词续费没重置服务期 → 上面那条 K3 断言零判别力(两支根本没区别)"


def test_P11_failclosed_rolls_back_everything(committed):
    """🔴 fail-closed:计费主体解析不到 → 整笔回滚,**不许**留下"标了没建"的中间态。"""
    cur = committed.cursor()
    _seed(cur, owner=None)          # 品牌无 owner_user_id
    before = _violations(cur)

    with pytest.raises(Exception):
        _renew(B + 1, "confirmed")

    cur.execute("SELECT is_monitored, monitoring_status FROM confirmed_keywords WHERE id=%s", (B + 1,))
    row = cur.fetchone()
    assert row["is_monitored"] is False, "fail-closed 失效:标了 is_monitored 却没建订阅 = 新孤儿"
    assert row["monitoring_status"] == "archived", "词条状态没回滚(部分提交)"
    assert _sub(cur, B + 1, "confirmed") == [], "回滚后仍留下订阅"
    cur.execute("SELECT service_start_date FROM quotes WHERE id=%s", (B,))
    assert str(cur.fetchone()["service_start_date"]) == "2026-01-01", "服务期被改了却没建订阅(部分提交)"
    assert _violations(cur) == before, "fail-closed 路径新造了孤儿"


def test_P11_identity_lock_has_discriminating_power(committed):
    """🔴 判据自证:恒等式锁不是恒真 —— 手工造一条"开着但没订阅"的词,必须被它抓到。"""
    cur = committed.cursor()
    _seed(cur)
    base = _violations(cur)
    cur.execute("UPDATE confirmed_keywords SET is_monitored = TRUE WHERE id=%s", (B + 1,))
    now = _violations(cur)
    assert (now["confirmed"] - base["confirmed"]) == {B + 1}, \
        "恒等式锁量不出人为造的孤儿 = 锁是恒真的,等于没写"
    cur.execute("UPDATE extra_keywords SET is_monitored = TRUE WHERE id=%s", (B + 2,))
    assert (_violations(cur)["extra"] - base["extra"]) == {B + 2}, "extra 侧的锁恒真"
