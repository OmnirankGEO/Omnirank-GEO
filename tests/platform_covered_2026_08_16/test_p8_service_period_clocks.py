# -*- coding: utf-8 -*-
"""§8① 两套钟 —— 横幅文案必须与**真 dispatch 行为**一致。

取证(三层信源逐条读出来的,不是推的):
  层1 横幅谓词  get_service_period_blocked_clients:`service_end_date < CURRENT_DATE`(日历)
  层2 真 dispatch quote_service_anchor_condition_sql + compliant_days < service_days
                 —— **纯履约,整段从不读 service_end_date**
  层3 compliance 闸 同样按累计达标天数

而层1 只取 `service_status NOT IN ('expired','paused')` 的行 ⇒ 这些行在层2 里
`COALESCE(service_status,'active') <> 'expired'` 恒真 ⇒ paid 分支整体为真 ⇒ **仍在跑**。
⇒ 旧文案「服务期已到 · 自动监测已暂停」对它标记的**每一行**都是假话。

按 §8① 要求:横幅与真行为不符就按真行为改文案,**不造第三种说法**。
"""
from __future__ import annotations

SRV = open("server.py", encoding="utf-8").read()
MDB = open("db/monitoring_db.py", encoding="utf-8").read()


def _fn(src: str, name: str) -> str:
    i = src.index("def " + name)
    j = src.index(chr(10) + "def ", i + 1)
    return src[i:j]


def _sql_only(body: str) -> str:
    """只取真发给 PG 的 SQL —— 不看注释/docstring。

    🔴 这条纪律今天已经付过三次学费:判据打在讲代码的话上,拆掉实现照样绿。
    """
    out, pos = [], 0
    while True:
        e = body.find("cur.execute(", pos)
        if e < 0:
            break
        a = body.find('"""', e)
        if a < 0:
            break
        b = body.find('"""', a + 3)
        out.append(body[a + 3:b])
        pos = b + 3
    return chr(10).join(out)


def test_dispatch_predicate_never_reads_service_end_date():
    """层2 取证:真 dispatch 的 SQL 里**没有** service_end_date。

    这是整条结论的地基 —— 它一旦不成立,下面"横幅在说谎"就不成立了。
    """
    sql = _sql_only(_fn(MDB, "list_active_subscriptions"))
    assert sql, "取不到 dispatch SQL —— 判据锚点失效"
    assert "service_end_date" not in sql, \
        "dispatch 开始看日历了 —— 两套钟的关系变了,§8① 的结论必须重新取证"
    assert "keyword_compliance_log" in sql and "service_days" in sql, \
        "dispatch 不再按累计达标天数判断 —— 履约口径(2026-06-04 拍板 A)被改了"


def test_banner_predicate_is_calendar_only():
    """层1 取证:横幅谓词就是日历,且只取 service_status 非 expired/paused 的行。"""
    sql = _sql_only(_fn(MDB, "get_service_period_blocked_clients"))
    assert "service_end_date < CURRENT_DATE" in sql, "横幅谓词不再是日历口径,结论需重新取证"
    assert "NOT IN ('expired', 'paused')" in sql, \
        "横幅不再排除 expired/paused —— 那样它标记的行在 dispatch 里就未必仍在跑了"


def test_banner_copy_matches_real_behavior():
    """🔴 文案锁:横幅不许再说"已暂停"(对它标记的每一行都是假话)。"""
    i = SRV.index("AUTO_MONITORING_PAUSED_BY_SERVICE_PERIOD")
    blk = SRV[i:i + 2200]
    # 只看真正发给用户的那两句文案,不看解释它的注释
    msgs = [ln for ln in blk.splitlines()
            if ('"' in ln or "'" in ln) and not ln.strip().startswith("#")
            and ("服务期" in ln or "自动监测" in ln)]
    joined = " ".join(msgs)
    assert "自动监测已暂停" not in joined, \
        f"横幅仍在说「自动监测已暂停」,而真 dispatch 还在跑 —— 两套钟里这一套在说谎:{joined}"
    assert "仍在" in joined or "继续跑" in joined, \
        f"横幅没有按真行为改写(应说明日历已过但按达标天数继续跑):{joined}"


def test_service_period_edit_entry_exists():
    """§8② 前提复核:管理员编辑入口**已经存在**(2026-08-09 客户反馈⑥ 交付)。

    工单说"没有任何编辑入口"—— 实测被证伪。这条锁把它钉住,
    免得后来者按工单原文再造一个第二入口(两个写口 = 新的分裂点)。
    """
    assert '@app.patch("/api/monitoring/clients/{quote_id}/service-period")' in SRV, \
        "服务期编辑入口不见了 —— 那才是工单描述的状态,需要新建"
    i = SRV.index('@app.patch("/api/monitoring/clients/{quote_id}/service-period")')
    blk = SRV[i:i + 3000]
    assert 'is_admin' in blk, "该入口丢了 admin-only 限制"
    assert 'confirm' in blk, "该入口丢了二次确认"
