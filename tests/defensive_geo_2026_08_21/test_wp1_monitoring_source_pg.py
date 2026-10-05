"""CUR-09:recurring monitoring 源钩子接回竞品与引用(真 PG · 规格 §2.2 / §17 WP1)。

为什么这一组必须打真库
----------------------
CUR-09 的修复改的是 ``assess_monitoring_result`` 里**一条 SELECT 的列清单**。
mock 一个 cursor 只能证明「我写的 Python 会读那两个键」,证明不了:
  · 这两列在真表上存在(``identity_review_state`` 就是个反例 —— 它不在
    ``init_monitoring_tables`` 里,靠一条已登记迁移补);
  · 类型吃得下(``search_citations`` 是 TEXT 存 JSON 字符串,
    ``competitors_mentioned`` 是 JSONB 已解成对象 —— 两条不同的解析路径)。
本仓 2026-08-18 记过「真 HTTP 判据必须打真库」,这里同理。

三态夹具(单对象夹具会让没接线也全绿)
  A 有引用有竞品 · B 两列都空 · C 坏 JSON 字符串
"""

from __future__ import annotations

import json

import pytest

from services.geo_observation.source_hooks import assess_monitoring_result

pytestmark = pytest.mark.integration


_CITATIONS = [
    {"url": "https://www.trade-press.com/report/2026?ref=a", "source_type": "citation"},
    {"url": "https://guide.trade-press.com/x", "source_type": "source"},
]
_COMPETITORS = ["甲家搬家", "乙运物流", "丙通货运"]

#: 答案必须 ≥50 字,否则 gate 会打 ``empty_or_short_answer`` 把 promotable 打掉,
#: 那样我们验的就成了「门」而不是「接线」。
_ANSWER = (
    "在本地搬家服务里,甲家搬家、乙运物流和丙通货运都有较多用户反馈。"
    "其中甲家搬家在长途搬运和钢琴等大件上的口碑相对突出,报价也比较透明。"
)


def _seed(conn, *, citations, competitors, keyword="本地靠谱搬家公司", client_id="9001"):
    """插一条完整的可登记监测结果,返回 result_id。

    ⚠️ ``monitoring_tasks.client_id`` 是 **TEXT**,而 ``confirmed_keywords.quote_id``
       是 **INTEGER**(生产 schema 实测)。现役 ``assess_monitoring_result`` 直接拿前者
       去比后者,靠 PG 隐式转型才能匹配上 —— 所以这里刻意用**数字字符串**做 client_id,
       复现生产真实形状。这是一处存量类型错配,已随交付上报,不在 WP1 修复范围内。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO brands (name, owner_user_id, industry) VALUES (%s, %s, %s) RETURNING id",
            ("判据品牌-defgeo", 7001, "本地生活服务"),
        )
        brand_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO quotes (id) VALUES (%s) ON CONFLICT DO NOTHING", (int(client_id),))
        cur.execute(
            "INSERT INTO confirmed_keywords (quote_id, keyword, monitoring_query) VALUES (%s, %s, %s)",
            (int(client_id), keyword, "本地有哪些靠谱的搬家公司?"),
        )
        cur.execute(
            "INSERT INTO monitoring_tasks (client_id, brand_id, status, total_tests, completed_tests, completed_at) "
            "VALUES (%s, %s, 'completed', 1, 1, NOW()) RETURNING id",
            (client_id, brand_id),
        )
        task_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO monitoring_results "
            "(task_id, keyword, platform, is_detected, full_response, tested_at, "
            " search_citations, competitors_mentioned, target_outcome, "
            " identity_review_state, response_status) "
            "VALUES (%s, %s, 'deepseek', 1, %s, NOW(), %s, %s, %s, 'not_required', 'answered') "
            "RETURNING id",
            (task_id, keyword, _ANSWER, citations, competitors, "recommended"),
        )
        return cur.fetchone()["id"]


def test_fixture_actually_lands_the_two_columns(db):
    """判据可用性关:先证明夹具真的把两列写进去了。

    没有这一条,下面所有断言都可能在验「一个空行」—— 而空行也能让
    「接回来的是空」看起来像「本来就没有」。本仓 2026-08-19 记过
    「夹具没跑起来的红不算抓到毒」,反过来也成立。
    """
    rid = _seed(db, citations=json.dumps(_CITATIONS, ensure_ascii=False),
                competitors=json.dumps(_COMPETITORS, ensure_ascii=False))
    with db.cursor() as cur:
        cur.execute(
            "SELECT search_citations, competitors_mentioned FROM monitoring_results WHERE id=%s",
            (rid,),
        )
        row = cur.fetchone()
    assert row["search_citations"], "夹具没写进 search_citations"
    assert row["competitors_mentioned"], "夹具没写进 competitors_mentioned"
    # 反向对照:TEXT 列拿回来必须还是字符串,JSONB 列拿回来必须已是 Python 对象。
    # 两者类型不同正是 _normalize_citations / _competitor_count 要分别处理的原因。
    assert isinstance(row["search_citations"], str)
    assert isinstance(row["competitors_mentioned"], list)


def test_citations_and_competitors_reach_the_observation(db):
    """🔴 CUR-09 主锁:观测里必须真的带回引用与竞品,不再恒空/恒 0。"""
    rid = _seed(db, citations=json.dumps(_CITATIONS, ensure_ascii=False),
                competitors=json.dumps(_COMPETITORS, ensure_ascii=False))
    gate, obs = assess_monitoring_result(db.cursor(), rid)
    assert gate.registerable, f"门没过,验不到接线:{gate.reasons}"
    assert obs is not None
    assert len(obs.citations) == 2, f"引用没接回来:{obs.citations}"
    assert obs.competitor_count == 3, f"竞品数没接回来:{obs.competitor_count}"


def test_citations_survive_the_privacy_pipeline_into_public_signal(db):
    """全链驱动:一路走到公共 signal 要用的 domain 列表。

    只断言 ``obs.citations`` 非空,放过了「接回来了但下游还是空」——
    那正是 CUR-09 原来的病(值在,但没人取)。
    """
    from services.geo_observation import privacy

    rid = _seed(db, citations=json.dumps(_CITATIONS, ensure_ascii=False),
                competitors=json.dumps(_COMPETITORS, ensure_ascii=False))
    _, obs = assess_monitoring_result(db.cursor(), rid)
    domains = privacy.clean_source_domains(obs.citations)
    assert {d["domain"] for d in domains} == {"trade-press.com", "guide.trade-press.com"}
    # 必须不命中:带 query 的完整 URL 不许漏进公共面。
    assert all("?" not in d["domain"] for d in domains)


def test_real_target_outcome_is_carried_but_legacy_sentinel_is_not(db):
    """``target_outcome`` 接回来;但 ``legacy_unknown`` 哨兵不许冒充判定。

    该列的 DEFAULT 就是 ``'legacy_unknown'``(生产 schema 实测),
    整片历史行都是这个值 —— 把它当判定结果会凭空造出一个档位。
    """
    rid = _seed(db, citations=json.dumps(_CITATIONS, ensure_ascii=False),
                competitors=json.dumps(_COMPETITORS, ensure_ascii=False))
    _, obs = assess_monitoring_result(db.cursor(), rid)
    assert obs.source_target_outcome == "recommended"

    with db.cursor() as cur:
        cur.execute("UPDATE monitoring_results SET target_outcome='legacy_unknown' WHERE id=%s", (rid,))
    _, obs2 = assess_monitoring_result(db.cursor(), rid)
    assert obs2.source_target_outcome is None, "legacy_unknown 哨兵被当成了真判定"


def test_empty_columns_degrade_to_empty_not_crash(db):
    """三态之二:两列都空 → 空结果,不抛。"""
    rid = _seed(db, citations=None, competitors=None, client_id="9002")
    gate, obs = assess_monitoring_result(db.cursor(), rid)
    assert gate.registerable
    assert obs.citations == []
    assert obs.competitor_count == 0


def test_malformed_citation_json_degrades_to_empty_not_crash(db):
    """三态之三:坏 JSON → 空结果,不抛。

    一条坏引用不该让整条观测登记失败 —— 那会把「引用解析不了」升级成
    「这次监测根本没发生」,分母当场少一格。
    """
    rid = _seed(db, citations="{ not json at all", competitors=None, client_id="9003")
    gate, obs = assess_monitoring_result(db.cursor(), rid)
    assert gate.registerable, "坏引用把整条观测打掉了 —— 分母会少一格"
    assert obs.citations == []
    assert obs.competitor_count == 0
