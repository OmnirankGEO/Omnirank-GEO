# -*- coding: utf-8 -*-
"""P0-1 的旗舰判据:**每日取词两臂的 source 闸**必须真的把两张词表隔开。

🔴 为什么单独一个文件(Review 2026-08-16 R-1 拦下的洞):
  已有的 p01 锁的是唯一索引、p02 锁的是 dispatch 路径 ——
  **没有一条夹具让"同一个 id 分属两张表"的行真正流经 list_active_subscriptions 的两臂**。
  p02 里 confirmed 用 B+1、extra 用 B+2,id 根本不撞;
  于是把 `AND s.keyword_source = 'confirmed'` 换成 `AND TRUE`,全仓测试照样全绿。
  **「用例过了」≠「被测的闸起作用了」。**

  合同臂的 JOIN 链很长(confirmed_keywords + monitoring_product_platform_matrices
  + quotes + 服务锚 + 达标天数),夹具但凡不满足整链,串上来的那一行本来就浮不出来
  —— 锁会天然瞎。所以本文件的夹具**必须让两边都满足各自整链**,
  这样"闸没了"才会以多出一行的形态暴露出来。

场景(迁移注释里写的那颗雷本身):
  brand A 的合同词 与 brand B 的手动词 **共用同一个 id X**(两表 id 各自独立、今天不撞是侥幸)。
  两条订阅分别是 (X, 'confirmed') 和 (X, 'extra')。
  闸在位 → 各取各的词、各挂各的品牌;闸没了 → 一条订阅会 JOIN 到**另一个客户的词**上去跑、去扣。
"""
from __future__ import annotations

# 两个品牌 / 两个 owner / 两张报价单,但**关键词 id 相同**
COLLIDE_ID = 990601
UA, CA, QA = 990610, 990610, 990610       # A:合同侧
UB, CB, QB = 990620, 990620, 990620       # B:手动侧


def _seed(cur):
    for uid, bid, qid, nm in ((UA, CA, QA, "撞车A品牌"), (UB, CB, QB, "撞车B品牌")):
        cur.execute("INSERT INTO users (id, username, password_hash, display_name) "
                    "VALUES (%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
                    (uid, f"p01b_{uid}", nm))
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT (id) DO NOTHING", (bid, nm, uid))
        cur.execute("INSERT INTO quotes (id, brand_name, brand_id, status, service_days, "
                    "service_start_date, paid_at) VALUES "
                    "(%s,%s,%s,'paid',180, CURRENT_DATE - 3, CURRENT_DATE - 3)",
                    (qid, nm, bid))

    # A 的合同词 —— 与 B 的手动词**同 id**
    cur.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, monitoring_query, "
                "is_monitored, monitoring_status) VALUES "
                "(%s,%s,'合同词_撞车','合同侧的问法',TRUE,'active')", (COLLIDE_ID, QA))
    # B 的手动词
    cur.execute("INSERT INTO extra_keywords (id, client_id, quote_id, brand_id, keyword, "
                "target_brand, monitoring_query, status, is_monitored) VALUES "
                "(%s,%s,%s,%s,'手动词_撞车',%s,'手动侧的问法','active',TRUE)",
                (COLLIDE_ID, UB, QB, CB, "撞车B品牌"))

    for uid, qid, bid, src in ((UA, QA, CA, "confirmed"), (UB, QB, CB, "extra")):
        cur.execute("INSERT INTO keyword_monitor_subscriptions "
                    "(user_id, keyword_id, quote_id, brand_id, status, daily_points, "
                    " feature_code, keyword_source) VALUES "
                    "(%s,%s,%s,%s,'active',130,'monitoring_keyword_daily',%s)",
                    (uid, COLLIDE_ID, qid, bid, src))


def _rows_for_collided_id():
    from db.monitoring_db import list_active_subscriptions
    return [r for r in list_active_subscriptions() if r["keyword_id"] == COLLIDE_ID]


def test_P01b_both_arms_are_reachable(committed):
    """🔴 判据的先决条件:两臂都必须真的**取得到**这条 id。

    合同臂 JOIN 链长,夹具不满足整链时那一行本来就浮不出来 ——
    那种情况下"没有串词"是因为**什么都没有**,不是因为闸在守。
    这条先证明分母非空,后面两条才有意义。
    """
    _seed(committed.cursor())
    got = {r["keyword_source"] for r in _rows_for_collided_id()}
    assert got == {"confirmed", "extra"}, (
        f"两臂没有都取到这条 id(实得 {got}) —— 夹具没满足某一臂的整链,"
        "后面的串词判据会因为分母为空而恒真"
    )


def test_P01b_no_cross_table_keyword_swap(committed):
    """闸在位:各取各的词、各挂各的品牌,一行不多。

    🔴 成对反向:把任一臂的 `s.keyword_source = ...` 换成 `AND TRUE`,
       多出来的那一行会让下面任一条断言当场红(变异脚本 MP7/MP8 实跑证明)。
    """
    _seed(committed.cursor())
    rows = _rows_for_collided_id()

    assert len(rows) == 2, (
        f"同一个 id 上取到 {len(rows)} 行(应为 2)—— 多出来的行就是串词:"
        f"{[(r['keyword_source'], r['keyword'], r['brand_name']) for r in rows]}"
    )

    by_src = {r["keyword_source"]: r for r in rows}
    c, e = by_src["confirmed"], by_src["extra"]

    assert c["keyword"] == "合同词_撞车" and c["monitoring_query"] == "合同侧的问法", \
        f"合同臂取到的不是自己的词:{c['keyword']} / {c['monitoring_query']}"
    assert c["brand_name"] == "撞车A品牌", f"合同臂挂错品牌:{c['brand_name']}"

    assert e["keyword"] == "手动词_撞车" and e["monitoring_query"] == "手动侧的问法", \
        f"手动臂取到的不是自己的词:{e['keyword']} / {e['monitoring_query']}"
    assert e["brand_name"] == "撞车B品牌", f"手动臂挂错品牌:{e['brand_name']}"

    # 计费主体也不许串:各自的订阅必须还挂在各自 owner 上
    assert c["user_id"] == UA and e["user_id"] == UB, \
        f"计费主体串了:confirmed→{c['user_id']}(应 {UA}) / extra→{e['user_id']}(应 {UB})"


def test_P01b_each_arm_labels_its_own_source(committed):
    """来源标签必须与真实来源一致 —— 标签错了,scheduler 的 source 与扣费口径就都错。"""
    _seed(committed.cursor())
    for r in _rows_for_collided_id():
        if r["keyword"] == "合同词_撞车":
            assert r["keyword_source"] == "confirmed", "合同词被标成手动词"
        elif r["keyword"] == "手动词_撞车":
            assert r["keyword_source"] == "extra", "手动词被标成合同词"
        else:
            raise AssertionError(f"取到了夹具之外的词:{r['keyword']}")
