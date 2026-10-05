"""P1 篇数容量合同 · 真链路锁(2026-08-08)

覆盖四件事:
  A. 容量语义:0..capacity(纯函数 + 真库真事务两层)
  B. 报价全族 A/B:entry/standard/flagship/strong × 三条阶梯档案,篇数只由 SSOT 说了算
  C. 历史快照绝不重算:「加入报价评估」不许动已冻结的那一版
  D. 白名单行为锁:被 L2 白名单放行的别人家文件,语义一旦漂移这里转红

🔴 判据打在**接线**上,不是函数上:C 组走真 psycopg2 连接 + 真 quote_pricing_snapshots 表,
   断言的是"落库之后 quotes.active_pricing_snapshot_id 有没有被动过",不是"函数有没有被调用"。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE_SQL = (ROOT / "scripts" / "fixtures" / "article_capacity_pg16_base.sql").read_text(encoding="utf-8")
TEST_DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")

from services.article_capacity_contract import (  # noqa: E402
    CAPACITY_STATUS_AVAILABLE,
    CAPACITY_STATUS_RESERVED,
    CAPACITY_STATUS_ZERO,
    SHORTFALL_REASON_NO_GAP,
    SHORTFALL_REASON_UNEXPLAINED,
    build_capacity_view,
    describe_shortfall,
    normalize_capacity,
    quote_is_payable,
    resolve_quote_capacity,
)
from tools.pricing_bands import (  # noqa: E402
    ARTICLE_LADDER_BRAND_KEYWORD,
    ARTICLE_LADDER_LEGACY_FALLBACK,
    ARTICLE_LADDER_MAIN,
    LEGACY_MISSING_CAPACITY_DEFAULT,
    V2_TIER_MIN_ARTICLES,
    normalize_article_capacity,
    tier_article_capacity,
)


# ============================================================
# A. 容量语义(纯函数层)
# ============================================================

def test_capacity_is_an_upper_bound_not_a_completion_target():
    """交付 3 / 授权 10 → 可用 7,**不是"完成率 30%"**,也不产生任何"要补 7 篇"的义务。"""
    view = build_capacity_view(authorized_articles=10, consumed_articles=3)
    assert view["authorized_articles"] == 10
    assert view["consumed_articles"] == 3
    assert view["available_articles"] == 7
    assert view["over_delivered_articles"] == 0
    assert view["display_status"] == CAPACITY_STATUS_AVAILABLE
    assert view["semantics"] == "upper_bound_0_to_capacity"


def test_zero_delivery_is_legal_and_not_a_failure():
    """0..capacity 的下界:一篇没交付也合法,状态是「额度已保留」不是失败。"""
    view = build_capacity_view(authorized_articles=10, consumed_articles=0)
    assert view["display_status"] == CAPACITY_STATUS_RESERVED
    assert view["available_articles"] == 10
    shortfall = describe_shortfall(capacity_view=view, reasons=[SHORTFALL_REASON_NO_GAP])
    assert shortfall["shortfall_articles"] == 10
    assert shortfall["counts_as_failure"] is False
    assert shortfall["reasons"] == [SHORTFALL_REASON_NO_GAP]


def test_shortfall_without_reason_is_recorded_as_unexplained():
    """不足额允许,但**说不出原因**必须留痕,不许静默当成"已完成"。"""
    view = build_capacity_view(authorized_articles=5, consumed_articles=1)
    assert describe_shortfall(capacity_view=view)["reasons"] == [SHORTFALL_REASON_UNEXPLAINED]
    # 反向对照:交付满了就没有不足额,也就不该造一条 unexplained 出来
    full = build_capacity_view(authorized_articles=5, consumed_articles=5)
    assert describe_shortfall(capacity_view=full)["reasons"] == []


def test_report_372_shape_capacity_zero_when_exhausted():
    """生产实证形状(报价 372):授权 15 / 已开 15 交付槽 → 容量 0。

    P4 合同点名用 372 做"容量 0 全程不出现去写"的验收样本,这条把那个数字的来源钉死。
    """
    view = build_capacity_view(authorized_articles=15, consumed_articles=15)
    assert view["available_articles"] == 0
    assert view["display_status"] == CAPACITY_STATUS_ZERO


def test_historical_over_delivery_is_reported_not_hidden():
    """生产 12/137 张单交付已超冻结容量。可用夹 0,但超额如实吐出来,不假装守恒。"""
    view = build_capacity_view(authorized_articles=45, consumed_articles=117)
    assert view["available_articles"] == 0
    assert view["over_delivered_articles"] == 72
    assert view["display_status"] == CAPACITY_STATUS_ZERO


def test_unpaid_quote_is_capacity_zero_regardless_of_authorized():
    """未付款 → 对外恒容量 0(设计合同 §3:只显示研究建议,不创建执行任务)。"""
    view = build_capacity_view(authorized_articles=30, consumed_articles=0, quote_is_payable=False)
    assert view["available_articles"] == 0
    assert view["display_status"] == CAPACITY_STATUS_ZERO
    # 反向对照:同样的数字,付了款就不是 zero —— 证明这条判据不是恒 zero
    paid = build_capacity_view(authorized_articles=30, consumed_articles=0, quote_is_payable=True)
    assert paid["display_status"] == CAPACITY_STATUS_RESERVED


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"status": "confirmed", "service_status": "active", "paid_amount": 2448}, True),
        ({"status": "confirmed", "service_status": "expiring", "paid_amount": 2448}, True),
        ({"status": "draft", "service_status": "pending", "paid_amount": 0}, False),
        ({"status": "confirmed", "service_status": "pending", "paid_amount": 0}, False),
        # 零价写作项目:paid_amount=0 但 source_type 在白名单里 → 已授权
        ({"status": "confirmed", "service_status": "pending", "paid_amount": 0,
          "source_type": "c_end_geo_plan"}, True),
    ],
)
def test_quote_is_payable_matrix(row, expected):
    assert quote_is_payable(row) is expected


def test_explicit_zero_capacity_is_never_upgraded_to_one():
    """`or 1` 那个真 bug 的定点锁:覆盖词显式 0 篇,规整后必须还是 0。"""
    assert normalize_capacity(0) == 0
    assert normalize_article_capacity(0, when_missing=99) == 0
    assert normalize_capacity(None) == LEGACY_MISSING_CAPACITY_DEFAULT
    assert normalize_capacity(-3) == 0
    assert normalize_capacity("7") == 7
    assert normalize_capacity("bogus") == LEGACY_MISSING_CAPACITY_DEFAULT


# ============================================================
# B. 报价全族 A/B:篇数只由 SSOT 说了算
# ============================================================

ALL_TIERS = ("entry", "standard", "flagship", "strong")


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_main_ladder_is_the_v2_contract(tier):
    assert tier_article_capacity(tier) == V2_TIER_MIN_ARTICLES[tier]
    assert tier_article_capacity(tier, ladder=ARTICLE_LADDER_MAIN) == V2_TIER_MIN_ARTICLES[tier]


@pytest.mark.parametrize("tier", ALL_TIERS)
def test_illegal_tier_falls_back_to_standard_not_to_one(tier):
    """非法档位必须回落 standard(与算价的 ts 默认同档),**不许回落成 1 篇**。"""
    assert tier_article_capacity("no-such-tier") == V2_TIER_MIN_ARTICLES["standard"]
    assert tier_article_capacity(None) == V2_TIER_MIN_ARTICLES["standard"]


def test_illegal_ladder_falls_back_to_main_not_to_legacy():
    """fail-safe 方向:阶梯名写错宁可给主合同,不许静默给 legacy 低篇数(那会少卖)。"""
    assert tier_article_capacity("entry", ladder="typo") == V2_TIER_MIN_ARTICLES["entry"]


def test_brand_keyword_ladder_is_frozen_at_production_values():
    """⏳ 待 Owner 拍板:品牌词是否豁免主合同阶梯。

    在 Owner 裁定前,本包对品牌词是**零价格变化** —— 这条把"零变化"钉死:
    谁擅自把品牌词改成 5/7/10,这里转红。
    """
    assert [tier_article_capacity(t, ladder=ARTICLE_LADDER_BRAND_KEYWORD)
            for t in ("entry", "standard", "flagship")] == [1, 2, 3]


def test_brand_keyword_pricing_path_uses_the_ladder():
    """接线锁:_make_brand_keyword_price 的输出必须来自阶梯档案,不是就地写死。"""
    from tools.batch_pricing import _make_brand_keyword_price

    priced = _make_brand_keyword_price("阿里云", markup=1.0)
    assert priced["entry_articles"] == tier_article_capacity("entry", ladder=ARTICLE_LADDER_BRAND_KEYWORD)
    assert priced["standard_articles"] == tier_article_capacity("standard", ladder=ARTICLE_LADDER_BRAND_KEYWORD)
    assert priced["flagship_articles"] == tier_article_capacity("flagship", ladder=ARTICLE_LADDER_BRAND_KEYWORD)
    # 品牌词冻结的容量上限 = 入门档容量(与 entry_articles 同源,不再各写一份)
    assert priced["required_articles"] == priced["entry_articles"]


def test_fallback_ladder_defaults_to_main_contract(monkeypatch):
    """🔴 对外价格影响点:Q2.b 兜底词篇数默认改走主合同(1/2/3 → 5/7/10)。

    这不是"顺手改的"—— 按 1 篇卖出去的词,容量上限就是 1 篇,直接违反老板铁律
    「所有词 ≥5 篇 · 1-2 篇无效」。交付单已列价格影响,回滚开关一并锁在下面那条。
    """
    from tools.pricing_bands import fallback_tier_article_capacity

    monkeypatch.delenv("GEO_FALLBACK_ARTICLE_LADDER", raising=False)
    assert [fallback_tier_article_capacity(t) for t in ("entry", "standard", "flagship")] == [5, 7, 10]


def test_fallback_ladder_rollback_switch_really_rolls_back(monkeypatch):
    """回滚开关必须真能回到上线前口径,否则"一键回滚"是句空话。"""
    from tools.pricing_bands import fallback_tier_article_capacity

    monkeypatch.setenv("GEO_FALLBACK_ARTICLE_LADDER", ARTICLE_LADDER_LEGACY_FALLBACK)
    assert [fallback_tier_article_capacity(t) for t in ("entry", "standard", "flagship")] == [1, 2, 3]


def test_v2_tier_price_articles_come_from_the_same_ladder():
    """算价与冻结必须同源:compute_v2_tier_price 的 articles 下界 = 取数出口给的数。"""
    from tools.pricing_bands import compute_v2_tier_price

    for tier in ALL_TIERS:
        # true_competition=1 → 公式项极小,篇数被档位底线接住
        out = compute_v2_tier_price(1, 60.0, tier, 1.0)
        assert out["articles"] == tier_article_capacity(tier), tier


def test_keyword_value_scorer_no_longer_restates_the_ladder():
    """接线锁:评分器缺 articles 时回落到 SSOT,而不是就地写死的 5/7/10。"""
    import inspect

    from tools import keyword_value_scorer

    src = inspect.getsource(keyword_value_scorer)
    for tier, value in (("entry", 5), ("standard", 7), ("flagship", 10)):
        assert f'"{tier}_articles", {value}' not in src, f"{tier} 档篇数又被就地复述了一遍"
    assert "tier_article_capacity" in src


# ============================================================
# D. 白名单行为锁(被 L2 放行的别人家文件,语义漂了这里红)
# ============================================================

def test_writing_helper_matches_capacity_ssot():
    """writing/keyword_topic_generator._required_article_count 与 SSOT 逐值对拍。

    该文件归 artstyle-p2 窗口占用,本包不改它;但它一旦把显式 0 变成 1、
    或把缺失默认从 1 改成别的数,这条锁转红(而不是元判据白名单静默放过)。
    """
    from writing.keyword_topic_generator import _required_article_count

    for raw in (None, 0, 1, 5, 12, "7", -2):
        assert _required_article_count({"required_articles": raw}) == normalize_capacity(raw), raw


def test_geo_douyin_quota_is_not_geo_capacity():
    """抖音图文线的 quota 与 GEO 报价容量是**两件事**,边界钉在这里。

    🔴 2026-08-17 订正(GEO 图文全量包 · 裁定 P0-5):本 docstring 原文写着
       「它 `max(1, ... or 1)` 把 0 抬成 1 —— 在图文配额语义下是它自己的选择」。
       **那句话现在是假的**:P0-5 已把 0 保真定成轴级要求,两处 `or 1` 都已修掉。
       边界这件事本身没变(抖音配额仍不许接 GEO 容量合同),所以断言留着、按记忆纪律
       「不变式仍成立只是搬了家的要改断言不要退役」,只把过期的理由改对,并补一条
       0 保真的**行为**断言(不是字符串扫描)——它才是 P0-5 真正要守的东西。
    """
    import inspect

    from services.geo_douyin import content_plan

    src = inspect.getsource(content_plan)
    assert "article_capacity_contract" not in src, "抖音配额不许接 GEO 容量合同(两套语义)"
    assert "quota" in src

    # 行为断言:显式 0 容量的词,规划出来必须是 0 配额 / 0 缺口 / 0 建议,不许被抬成 1。
    plans = content_plan._plan_rows(
        [{"keyword": "零容量覆盖词", "required_articles": 0},
         {"keyword": "正常词", "required_articles": 3}],
        done_map={},
    )
    by_kw = {p.keyword: p for p in plans}
    assert by_kw["零容量覆盖词"].quota == 0, "显式 0 容量被抬高了 —— P0-5 的原始缺陷复发"
    assert by_kw["零容量覆盖词"].gap == 0
    assert by_kw["零容量覆盖词"].suggested == 0
    # 反向对照:非零词照常有配额,证明上面三个 0 不是"整个函数恒返回 0"
    assert by_kw["正常词"].quota == 3 and by_kw["正常词"].gap == 3


def test_geo_douyin_distiller_preserves_zero_capacity():
    """P0-5 第二处(`topic_distiller.load_purchased_keywords` 的行映射)同样 0 保真。

    这条打的是**纯映射行为**,不连库:`or 1` 复发时 required_articles 会变成 1,断言当场红。
    """
    from tools.pricing_bands import normalize_article_capacity

    # 蒸馏器行映射用的就是这个 SSOT;逐值对拍,含 None / 显式 0 / 负数三种边界
    assert normalize_article_capacity(0, when_missing=0) == 0, "显式 0 被吃掉 = P0-5 复发"
    assert normalize_article_capacity(None, when_missing=0) == 0, "缺失应为 0(0..capacity 上限语义)"
    assert normalize_article_capacity(-3, when_missing=0) == 0, "负容量应收敛到 0"
    # 反向对照:正常值必须原样透传,证明不是"恒返回 0"
    assert normalize_article_capacity(7, when_missing=0) == 7


# ============================================================
# C. 真库真事务:历史快照绝不重算 + 容量读数
# ============================================================

pytestmark_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_DATABASE_URL not configured")


def _schema() -> str:
    return f"cap_{uuid.uuid4().hex[:12]}"


def _fresh(schema: str) -> None:
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        conn.cursor().execute(BASE_SQL.replace("article_capacity_test", schema))
    finally:
        conn.close()


def _connect(schema: str):
    return psycopg2.connect(
        TEST_DSN, options=f"-csearch_path={schema},public", cursor_factory=RealDictCursor
    )


def _seed(cur, *, authorized: list[int], topics: int, status="confirmed",
          service_status="active", paid=2448.0) -> int:
    cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('容量测试品牌',7) RETURNING id")
    brand_id = int(cur.fetchone()["id"])
    cur.execute(
        "INSERT INTO quotes(brand_id,owner_user_id,status,service_status,source_type,paid_amount) "
        "VALUES (%s,7,%s,%s,'agent_quote',%s) RETURNING id",
        (brand_id, status, service_status, paid),
    )
    quote_id = int(cur.fetchone()["id"])
    first_kw = None
    for index, count in enumerate(authorized):
        cur.execute(
            "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,is_core) "
            "VALUES (%s,%s,%s,TRUE) RETURNING id",
            (quote_id, f"关键词{index}", count),
        )
        first_kw = first_kw or int(cur.fetchone()["id"])
    for _ in range(topics):
        cur.execute(
            "INSERT INTO topics(keyword_id,quote_id,status) VALUES (%s,%s,'completed')",
            (first_kw, quote_id),
        )
    cur.execute(
        "INSERT INTO keyword_selection_sessions(quote_id,brand_id,token,status) "
        "VALUES (%s,%s,%s,'confirmed') RETURNING id",
        (quote_id, brand_id, uuid.uuid4().hex),
    )
    return quote_id


@pytestmark_db
def test_resolve_quote_capacity_counts_topics_not_articles():
    """真库口径锁:容量占用是 topics(交付槽),articles 的重写稿不重复占容量。

    这是生产实证的裁定点(372:15 授权 / 15 topics / 136 articles)。
    谁把 consumed 改成 COUNT(articles),这条立刻转红。
    """
    schema = _schema()
    _fresh(schema)
    conn = _connect(schema)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        quote_id = _seed(cur, authorized=[15], topics=15)
        cur.execute("SELECT id FROM topics WHERE quote_id=%s LIMIT 1", (quote_id,))
        topic_id = int(cur.fetchone()["id"])
        for _ in range(20):  # 20 篇重写草稿,挂在同一个交付槽下
            cur.execute(
                "INSERT INTO articles(topic_id,quote_id,status) VALUES (%s,%s,'draft')",
                (topic_id, quote_id),
            )
        view = resolve_quote_capacity(cur, quote_id)
        assert view["authorized_articles"] == 15
        assert view["consumed_articles"] == 15, "重写草稿被误算成容量占用"
        assert view["available_articles"] == 0
        assert view["display_status"] == CAPACITY_STATUS_ZERO
    finally:
        conn.close()


@pytestmark_db
def test_resolve_quote_capacity_ignores_non_core_zero_rows():
    """覆盖词(is_core=FALSE)不进授权容量;显式 0 的核心词也不虚增。"""
    schema = _schema()
    _fresh(schema)
    conn = _connect(schema)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        quote_id = _seed(cur, authorized=[15, 0], topics=2)
        cur.execute(
            "INSERT INTO confirmed_keywords(quote_id,keyword,required_articles,is_core) "
            "VALUES (%s,'覆盖词',99,FALSE)", (quote_id,),
        )
        view = resolve_quote_capacity(cur, quote_id)
        assert view["authorized_articles"] == 15, "覆盖词被算进了授权容量"
        assert view["available_articles"] == 13
    finally:
        conn.close()


def _install_snapshot(cur, quote_id: int, payload: dict) -> int:
    cur.execute("SELECT id,brand_id FROM keyword_selection_sessions WHERE quote_id=%s", (quote_id,))
    session = cur.fetchone()
    cur.execute(
        "INSERT INTO quote_pricing_snapshots(quote_id,brand_id,selection_session_id,version,"
        "reason,calculation_version,pricing_snapshot,snapshot_hash) "
        "VALUES (%s,%s,%s,1,'freeze','legacy-exact-price-freeze-v1',%s::jsonb,%s) RETURNING id",
        (quote_id, session["brand_id"], session["id"], json.dumps(payload), "0" * 64),
    )
    snapshot_id = int(cur.fetchone()["id"])
    cur.execute("UPDATE quotes SET active_pricing_snapshot_id=%s WHERE id=%s", (snapshot_id, quote_id))
    cur.execute(
        "UPDATE keyword_selection_sessions SET pricing_data=%s WHERE quote_id=%s",
        (json.dumps(payload), quote_id),
    )
    return snapshot_id


@pytestmark_db
def test_capacity_evaluation_never_touches_the_frozen_snapshot(monkeypatch):
    """🔴 资金红线锁:「加入报价评估」写了新版本,但**冻结的那一版一字不动**。

    判据打在落库结果上,不是"函数有没有被调用":
      · quotes.active_pricing_snapshot_id 仍指向 v1
      · v1 行的 pricing_snapshot 逐字节不变
      · keyword_selection_sessions.pricing_data 不变
      · 容量读数不变(登记 ≠ 加容量)
    """
    from services import quote_pricing_snapshot as qps

    schema = _schema()
    _fresh(schema)
    monkeypatch.setattr(qps, "get_connection", lambda: _connect(schema))

    setup = _connect(schema)
    try:
        setup.autocommit = True
        cur = setup.cursor()
        quote_id = _seed(cur, authorized=[15], topics=3)
        frozen_payload = {"keywords": [{"keyword": "深圳哪家装修公司靠谱", "entry": {"price": 900, "articles": 5}}]}
        v1_id = _install_snapshot(cur, quote_id, frozen_payload)
    finally:
        setup.close()

    result = qps.record_capacity_evaluation_request(
        quote_id,
        actor_user_id=7,
        actor_membership_id=None,
        candidates=[{"keyword": "深圳旧房翻新哪家靠谱", "plan_item_id": "Q1-P02"}],
        origin="delivery_plan",
        idempotency_key="idem-" + uuid.uuid4().hex,
    )
    assert result["already_recorded"] is False
    assert result["version"] == 2

    check = _connect(schema)
    try:
        check.autocommit = True
        cur = check.cursor()
        cur.execute("SELECT active_pricing_snapshot_id FROM quotes WHERE id=%s", (quote_id,))
        assert int(cur.fetchone()["active_pricing_snapshot_id"]) == v1_id, "冻结指针被挪动了"
        cur.execute("SELECT pricing_snapshot FROM quote_pricing_snapshots WHERE id=%s", (v1_id,))
        assert cur.fetchone()["pricing_snapshot"] == frozen_payload, "v1 快照被改写了"
        cur.execute("SELECT pricing_data FROM keyword_selection_sessions WHERE quote_id=%s", (quote_id,))
        assert json.loads(cur.fetchone()["pricing_data"]) == frozen_payload, "session 报价数据被改写了"
        # 登记 ≠ 加容量
        view = resolve_quote_capacity(cur, quote_id)
        assert view["authorized_articles"] == 15
        assert view["available_articles"] == 12
        # 新版本里确实带上了候选(否则"登记成功"是空话)
        cur.execute(
            "SELECT pricing_snapshot,calculation_version FROM quote_pricing_snapshots "
            "WHERE quote_id=%s AND version=2", (quote_id,),
        )
        row = cur.fetchone()
        assert row["calculation_version"] == qps.CAPACITY_EVALUATION_VERSION
        requests = row["pricing_snapshot"][qps.CAPACITY_EVALUATION_KEY]
        assert requests[0]["candidates"][0]["keyword"] == "深圳旧房翻新哪家靠谱"
    finally:
        check.close()


@pytestmark_db
def test_capacity_evaluation_is_idempotent(monkeypatch):
    """同一个幂等键重复提交只留一条,不重复开版本(资金类幂等要求)。"""
    from services import quote_pricing_snapshot as qps

    schema = _schema()
    _fresh(schema)
    monkeypatch.setattr(qps, "get_connection", lambda: _connect(schema))
    setup = _connect(schema)
    try:
        setup.autocommit = True
        cur = setup.cursor()
        quote_id = _seed(cur, authorized=[10], topics=0)
        _install_snapshot(cur, quote_id, {"keywords": []})
    finally:
        setup.close()

    key = "idem-" + uuid.uuid4().hex
    kwargs = dict(actor_user_id=7, actor_membership_id=None,
                  candidates=[{"keyword": "重复提交词"}], origin="delivery_plan",
                  idempotency_key=key)
    first = qps.record_capacity_evaluation_request(quote_id, **kwargs)
    second = qps.record_capacity_evaluation_request(quote_id, **kwargs)
    assert first["already_recorded"] is False
    assert second["already_recorded"] is True
    assert second["version"] == first["version"]

    check = _connect(schema)
    try:
        check.autocommit = True
        cur = check.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM quote_pricing_snapshots WHERE quote_id=%s", (quote_id,))
        assert int(cur.fetchone()["n"]) == 2, "幂等失效:重复提交多开了版本"
    finally:
        check.close()


@pytestmark_db
def test_capacity_evaluation_rejects_client_supplied_price(monkeypatch):
    """前端不算钱:候选里夹带 price/articles 一律拒收(不是忽略,是 422)。"""
    from services import quote_pricing_snapshot as qps

    schema = _schema()
    _fresh(schema)
    monkeypatch.setattr(qps, "get_connection", lambda: _connect(schema))
    setup = _connect(schema)
    try:
        setup.autocommit = True
        cur = setup.cursor()
        quote_id = _seed(cur, authorized=[10], topics=0)
        _install_snapshot(cur, quote_id, {"keywords": []})
    finally:
        setup.close()

    with pytest.raises(qps.QuoteSnapshotError) as exc:
        qps.record_capacity_evaluation_request(
            quote_id, actor_user_id=7, actor_membership_id=None,
            candidates=[{"keyword": "夹带价格的词", "required_articles": 5, "price": 999}],
            origin="delivery_plan", idempotency_key="idem-" + uuid.uuid4().hex,
        )
    assert exc.value.code == "QUOTE_CAPACITY_CANDIDATE_PRICE_NOT_ALLOWED"
    assert exc.value.status == 422
