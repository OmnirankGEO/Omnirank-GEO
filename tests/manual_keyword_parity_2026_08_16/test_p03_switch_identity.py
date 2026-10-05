# -*- coding: utf-8 -*-
"""P0-3 「开关开 ⟺ 真的在跑」—— 手动词的开关必须真的决定它进不进当日取词集合。"""
from __future__ import annotations

B = 990300


def _seed(cur, *, is_monitored: bool):
    cur.execute("INSERT INTO users (id, username, password_hash, display_name) "
                "VALUES (%s,'p03','x','P03') ON CONFLICT (id) DO NOTHING", (B,))
    cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,'P03品牌',%s) "
                "ON CONFLICT (id) DO NOTHING", (B, B))
    cur.execute("INSERT INTO quotes (id, brand_name, brand_id, status) VALUES (%s,'P03品牌',%s,'paid')", (B, B))
    cur.execute("INSERT INTO extra_keywords (id, client_id, quote_id, brand_id, keyword, target_brand, "
                "monitoring_query, status, is_monitored) VALUES "
                "(%s,%s,%s,%s,'手动词_P03','P03品牌','P03问法','active',%s)",
                (B + 1, B, B, B, is_monitored))
    cur.execute("INSERT INTO keyword_monitor_subscriptions "
                "(user_id, keyword_id, quote_id, brand_id, status, daily_points, feature_code, keyword_source) "
                "VALUES (%s,%s,%s,%s,'active',130,'monitoring_keyword_daily','extra')", (B, B + 1, B, B))


def _dispatched():
    from db.monitoring_db import list_active_subscriptions
    return {(r["keyword_id"], r.get("keyword_source")) for r in list_active_subscriptions()}


def test_P03_switch_off_means_not_dispatched(committed):
    """关着的手动词**不能**被取去跑 —— 即使它身上挂着一条 active 订阅。

    (这正是 opt-in 闸存在的意义:订阅是计费关系,开关是用户意愿,两者都要。)
    """
    cur = committed.cursor()
    _seed(cur, is_monitored=False)
    assert (B + 1, "extra") not in _dispatched(), \
        "开关关着却仍被取去跑 = 用户看着『关』,cron 照跑照扣"


def test_P03_switch_on_means_dispatched(committed):
    """反向对照:同一条词把开关打开就必须进集合(否则上一条零判别力)。"""
    cur = committed.cursor()
    _seed(cur, is_monitored=True)
    assert (B + 1, "extra") in _dispatched(), \
        "开着的手动词没进取词集合 → 上一条断言零判别力"
