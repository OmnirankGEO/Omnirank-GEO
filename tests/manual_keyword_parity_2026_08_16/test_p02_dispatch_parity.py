# -*- coding: utf-8 -*-
"""P0-2 每日取词:手动词与合同词同权,且 K3(手动词不受报价单状态约束)真的成立。

被测的是生产函数 db.monitoring_db.list_active_subscriptions 本身。
"""
from __future__ import annotations
import pytest

B = 990100   # 本文件夹具 id 基址


def _seed(cur, *, quote_status: str, paid: bool):
    cur.execute("INSERT INTO users (id, username, password_hash, display_name) "
                "VALUES (%s,'p02_owner','x','P02') ON CONFLICT (id) DO NOTHING", (B,))
    cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,'P02品牌',%s) "
                "ON CONFLICT (id) DO NOTHING", (B, B))
    cur.execute("INSERT INTO quotes (id, brand_name, brand_id, status, paid_at) "
                "VALUES (%s,'P02品牌',%s,%s,%s)",
                (B, B, quote_status, "NOW()" if False else (None if not paid else "2026-01-01")))
    # 合同词
    cur.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, monitoring_query, is_monitored) "
                "VALUES (%s,%s,'合同词_P02','合同词问法',TRUE)", (B + 1, B))
    # 手动词
    cur.execute("INSERT INTO extra_keywords (id, client_id, quote_id, brand_id, keyword, target_brand, "
                "monitoring_query, status, is_monitored) VALUES "
                "(%s,%s,%s,%s,'手动词_P02','P02品牌','手动词问法','active',TRUE)", (B + 2, B, B, B))
    for kid, src in ((B + 1, "confirmed"), (B + 2, "extra")):
        cur.execute("INSERT INTO keyword_monitor_subscriptions "
                    "(user_id, keyword_id, quote_id, brand_id, status, daily_points, feature_code, keyword_source) "
                    "VALUES (%s,%s,%s,%s,'active',130,'monitoring_keyword_daily',%s)", (B, kid, B, B, src))


def _fetch():
    from db.monitoring_db import list_active_subscriptions
    rows = list_active_subscriptions()
    return {(r["keyword_id"], r.get("keyword_source")): r for r in rows}


def test_P02_both_sources_dispatched_when_quote_paid(committed):
    """正向:报价单 paid 时,合同词与手动词**都**进当日取词集合。"""
    cur = committed.cursor()
    _seed(cur, quote_status="paid", paid=True)
    got = _fetch()
    assert (B + 1, "confirmed") in got, "合同词没进取词集合(基线就坏了,后面的判据无意义)"
    assert (B + 2, "extra") in got, "手动词没进取词集合 —— P0-2 的 UNION extra 臂没生效"
    assert got[(B + 2, "extra")]["keyword"] == "手动词_P02"
    assert got[(B + 2, "extra")]["monitoring_query"] == "手动词问法", \
        "extra 臂拿的不是自己的问法 = 串词"
    assert got[(B + 2, "extra")]["brand_name"] == "P02品牌", \
        "brand_name 为空 → target_brand='' → LLM 检测恒 False(2026-05-26 那个 detected=0/176 的形态)"


def test_P02_K3_manual_keyword_not_gated_by_quote_status(committed):
    """🔴 K3 + 判别力:报价单是 draft、无 paid_at 时——

    手动词**仍在**取词集合里(K3:手动词的运行不受报价单状态约束),
    而同一条件下的合同词**不在**(证明这条判据有判别力,不是"两边都进"的恒真)。
    """
    cur = committed.cursor()
    _seed(cur, quote_status="draft", paid=False)
    got = _fetch()
    assert (B + 2, "extra") in got, "K3 被违反:手动词因报价单没付款被挡在取词之外"
    assert (B + 1, "confirmed") not in got, \
        "合同词在 draft 报价单下也被取走 = 本判据零判别力(不能证明两支真的不同)"
