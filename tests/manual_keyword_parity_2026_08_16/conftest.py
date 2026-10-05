# -*- coding: utf-8 -*-
"""夹具:两种连接。

conn      —— 事务内造数据 + 用例结束回滚(纯 SQL 断言用)
committed —— 🔴 真提交。因为被测的是**生产函数自己开连接**跑的 SQL,
             不提交它看不见;若改成"在测试里把那段 SQL 抄一遍"就变成
             「测量仪器与被测实现不同口径」——那是本仓栽过的坑。
             teardown 按固定 id 段(99xxxx)清干净,并断言真的清掉了。
"""
from __future__ import annotations
import os
import pytest
import psycopg2
from psycopg2.extras import RealDictCursor

FIX_IDS = 990000          # 夹具 id 段起点(生产真实 id 远小于它)


def _connect():
    return psycopg2.connect(os.environ["TEST_DATABASE_URL"], cursor_factory=RealDictCursor)


@pytest.fixture()
def conn():
    c = _connect()
    try:
        yield c
    finally:
        try:
            c.rollback()
        except Exception:
            pass
        c.close()


def _purge(cur):
    cur.execute("DELETE FROM keyword_monitor_subscriptions WHERE keyword_id >= %s OR user_id >= %s",
                (FIX_IDS, FIX_IDS))
    cur.execute("DELETE FROM keyword_trend_stats WHERE keyword_id >= %s", (FIX_IDS,))
    cur.execute("DELETE FROM keyword_compliance_log WHERE keyword_id >= %s", (FIX_IDS,))
    cur.execute("DELETE FROM extra_keywords WHERE id >= %s", (FIX_IDS,))
    cur.execute("DELETE FROM confirmed_keywords WHERE id >= %s", (FIX_IDS,))
    cur.execute("DELETE FROM quotes WHERE id >= %s", (FIX_IDS,))
    cur.execute("DELETE FROM brands WHERE id >= %s", (FIX_IDS,))
    cur.execute("DELETE FROM users WHERE id >= %s", (FIX_IDS,))


@pytest.fixture()
def committed():
    c = _connect()
    c.autocommit = True
    cur = c.cursor()
    _purge(cur)
    try:
        yield c
    finally:
        try:
            _purge(cur)
            # 自证清干净了(不然下一个用例会被上一个的残留污染成假绿/假红)
            cur.execute("SELECT count(*) c FROM keyword_monitor_subscriptions WHERE keyword_id >= %s",
                        (FIX_IDS,))
            assert cur.fetchone()["c"] == 0, "夹具没清干净"
        finally:
            c.close()
