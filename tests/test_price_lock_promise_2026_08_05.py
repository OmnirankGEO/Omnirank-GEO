"""[价格锁承诺脱钩修 2026-08-05 · WO_PRICE_LOCK_PROMISE] 报价单只承诺真锁住的价。

修的 bug:`price_locked_until` 无条件 = 今天+7,和「本次到底写没写共享缓存」毫无关系。
上游因正当理由(P0-D 信任快照 / 自设成本 / 进货倍率 / 爆价护栏逐词剥离)跳过了锁,
下游照样按锁在承诺。

跑法:
  ALLOW_NONTEST_DB=1 TEST_DATABASE_URL=占位 PYTHONIOENCODING=utf-8 \
    python -m pytest tests/test_price_lock_promise_2026_08_05.py -q

判据纪律(每条「必须命中」都配成对的「必须不命中」):
  · 归一化能吃 datetime/ISO 串  ←→  垃圾串/空串/None 必须返回 None(不编日期)
  · 真写了缓存 → 有锁期          ←→  P0-D 跳过写缓存 → 一个锁期都没有,且 save 调用数必须为 0
  · 爆价护栏剥掉的词 → 无锁期    ←→  同一单里没被剥的词 → 有锁期(证明是【逐词】不是【按单】)
  · 两种情况渲染结果必须不同     ←→  若相同则判据没接上,整条作废(WO §5 反向对照要求)
"""
import os
import re
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.price_lock_promise import (  # noqa: E402
    LOCK_MAP_KEY,
    NO_LOCK_NOTE,
    lock_fields,
    record_locked,
    to_lock_date,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# A. to_lock_date —— 锁期日期只能来自 DB 的 expires_at,取不到就不承诺
# ============================================================================

def test_A1_datetime_hit():
    d = datetime.now() + timedelta(days=7)
    assert to_lock_date(d) == d.strftime("%Y-%m-%d")


def test_A1_none_miss():
    # 必须不命中:拿不到 expires_at 时【不许】编一个日期出来
    assert to_lock_date(None) is None


def test_A2_iso_string_with_microseconds_hit():
    raw = (datetime.now() + timedelta(days=7)).isoformat()   # save_* 就是这么构造的
    assert to_lock_date(raw) == raw[:10]


def test_A2_garbage_string_miss():
    assert to_lock_date("下周") is None
    assert to_lock_date("") is None
    assert to_lock_date("   ") is None


def test_A3_space_separated_timestamp_hit():
    future = (date.today() + timedelta(days=7)).isoformat()
    assert to_lock_date(f"{future} 13:45:00") == future


def test_A3_wrong_type_miss():
    assert to_lock_date(1754400000) is None       # 时间戳数字不认(免得把 epoch 当日期渲染)
    assert to_lock_date(["2026-08-12"]) is None


def test_A4_past_date_miss_future_date_hit():
    """fail-closed:已经过期的锁不是锁。成对判据,两边必须给出【不同】结论。"""
    past = (date.today() - timedelta(days=1)).isoformat()
    future = (date.today() + timedelta(days=1)).isoformat()
    assert to_lock_date(past) is None
    assert to_lock_date(future) == future


# ============================================================================
# B. record_locked / lock_fields —— 登记与渲染
# ============================================================================

def test_B1_record_then_render_hit():
    m = {}
    exp = (datetime.now() + timedelta(days=7)).isoformat()
    assert record_locked(m, ["深圳装修公司哪家好"], exp) == 1
    out = lock_fields(m, "深圳装修公司哪家好")
    assert out["price_locked_until"] == exp[:10]
    assert out["price_lock_note"] is None


def test_B1_no_expires_records_nothing_miss():
    m = {}
    assert record_locked(m, ["深圳装修公司哪家好"], None) == 0
    assert m == {}
    out = lock_fields(m, "深圳装修公司哪家好")
    assert out["price_locked_until"] is None
    assert out["price_lock_note"] == NO_LOCK_NOTE


def test_B2_field_shape_is_stable_both_ways():
    """两个键恒存在 —— 省略键会让下游 .get(key, now()+7) 把假日期复活。"""
    m = {}
    record_locked(m, ["A"], (datetime.now() + timedelta(days=7)).isoformat())
    assert set(lock_fields(m, "A")) == {"price_locked_until", "price_lock_note"}
    assert set(lock_fields(m, "B")) == {"price_locked_until", "price_lock_note"}


def test_B3_per_keyword_not_per_order():
    """map 非空 ≠ 全单都锁了。不在 map 里的词必须【不】显示锁期。"""
    m = {}
    record_locked(m, ["锁住的词"], (datetime.now() + timedelta(days=7)).isoformat())
    assert lock_fields(m, "锁住的词")["price_locked_until"] is not None
    assert lock_fields(m, "没锁的词")["price_locked_until"] is None


def test_B4_note_is_plain_language_not_engineering_term():
    """`feedback_hint_must_help_or_hide`:不许出现工程术语。"""
    banned = ["缓存", "cache", "命中", "miss", "写入", "expires", "TTL", "null", "None"]
    for w in banned:
        assert w.lower() not in NO_LOCK_NOTE.lower(), f"提示文案不许出现工程术语: {w}"
    assert len(NO_LOCK_NOTE) >= 8


# ============================================================================
# C. 定价引擎:同一批词 · 写了缓存 vs 没写缓存 → lock map 必须不同
#    (WO §5 反向对照:若两边一样,说明判据根本没接上,整条作废)
# ============================================================================

def _fake_scored(keyword, **over):
    row = {
        "keyword": keyword,
        "required_articles": 7,
        "selling_price": 900,
        "cost_per_article": 60,
        "total_cost": 420.0,
        "markup_ratio": 2.0,
        "difficulty_score": 1.0,
        "value_score": 1.0,
        "search_volume": 100,
        "sem_price": 2.0,
        "bidword_company_count": 3,
        "competitor_count": 10,
        "effective_competition": 10,
        "content_count": 5,
        "intent": "commercial",
        "funnel_stage": "consideration",
        "search_probability": 0.5,
        "keyword_type": "local_city",
        "is_broad": False,
        "entry_price": 500, "entry_articles": 4,
        "standard_price": 900, "standard_articles": 7,
        "flagship_price": 1300, "flagship_articles": 10,
        "pricing_formula_version": "v2.2_2026-06-11",
        "super_red_ocean": False,
        "guarantee_unavailable": False,
        "blowup_no_cache": False,
        "cost_snapshot_uncacheable": False,
    }
    row.update(over)
    return row


@pytest.fixture
def engine(monkeypatch):
    """把 generate_batch_quote 的外部依赖钉住,只留【写缓存判定 → 锁期登记】这条真链路。

    被打桩的都是与本包无关的外部 IO(评分/审计/行业锁定/意图闸/LLM flag);
    `_should_write_shared_cache` / `_cacheable_rows` / 锁期登记 / 返回结构 **全是真代码**。
    """
    import tools.batch_pricing as bp
    import tools.keyword_value_scorer as kvs
    import tools.pricing_auditor as pa
    import db.diagnosis_db as ddb

    state = {"saved": [], "expires": None}

    async def _fake_score(keywords, **kw):
        return [_fake_scored(k) for k in keywords], {}

    async def _fake_audit(scored, **kw):
        return scored, ""

    def _fake_save(brand_name, rows, industry=None, city=None):
        state["saved"].append([r["keyword"] for r in rows])
        return state["expires"]

    monkeypatch.setattr(kvs, "score_keywords", _fake_score)
    monkeypatch.setattr(pa, "audit_and_correct", _fake_audit)
    monkeypatch.setattr(ddb, "save_keyword_prices_cache", _fake_save)
    monkeypatch.setattr(ddb, "get_cached_keyword_prices", lambda *a, **k: {})
    monkeypatch.setattr(bp, "_lock_brand_industry", lambda b, fallback_industry="": fallback_industry)
    monkeypatch.setattr(bp, "_fetch_trust_asset_for_pricing", lambda *a, **k: None)
    monkeypatch.setattr(bp, "_partition_by_commercial_policy",
                        lambda kws, **k: (list(kws), [], "off"))
    monkeypatch.setattr(bp, "_enrich_keywords_with_tier_prices",
                        lambda rows, **k: rows)
    return bp, state


KWS = ["深圳装修公司哪家好", "深圳全屋定制推荐"]


@pytest.mark.asyncio
async def test_C1_default_path_writes_cache_and_locks(engine):
    """必须命中:默认口径真写了缓存 → 每个词都有锁期,且日期 = 写入返回的 expires_at 的日期。"""
    bp, state = engine
    exp = (datetime.now() + timedelta(days=7)).isoformat()
    state["expires"] = exp

    quote_data, _md = await bp.generate_batch_quote(
        keywords=list(KWS), brand_name="测试品牌", industry="装修", city="深圳",
        allow_cache_write=True,
    )
    lock_map = quote_data[LOCK_MAP_KEY]
    assert state["saved"] == [KWS], "前提没成立:这条用例要求真调了 save"
    assert set(lock_map) == set(KWS)
    for k in KWS:
        assert lock_map[k] == exp[:10]


@pytest.mark.asyncio
async def test_C2_p0d_trust_snapshot_skips_cache_and_locks_nothing(engine, monkeypatch):
    """必须不命中:P0-D 信任快照品牌(per-brand 价·不写共享缓存)→ 一个锁期都不许有。

    🔴 同时钉死 WO 的红线:不许因为要显示锁期就让 P0-D 开始写共享缓存 → save 调用数必须为 0。
    """
    bp, state = engine
    state["expires"] = (datetime.now() + timedelta(days=7)).isoformat()
    monkeypatch.setattr(bp, "_fetch_trust_asset_for_pricing",
                        lambda *a, **k: {"source": "collected"})

    quote_data, _md = await bp.generate_batch_quote(
        keywords=list(KWS), brand_name="测试品牌", industry="装修", city="深圳",
        allow_cache_write=True, brand_id=123,
    )
    assert quote_data[LOCK_MAP_KEY] == {}
    assert state["saved"] == [], "🔴 P0-D 防跨客户污染闸被破坏:不许为了显示锁期去写共享缓存"


@pytest.mark.asyncio
async def test_C3_same_keywords_two_situations_must_render_differently(engine, monkeypatch):
    """WO §5 反向对照:同一批词,写了 vs 没写,两边【显示必须不同】。相同即整条作废。"""
    bp, state = engine
    state["expires"] = (datetime.now() + timedelta(days=7)).isoformat()

    wrote, _ = await bp.generate_batch_quote(
        keywords=list(KWS), brand_name="测试品牌", industry="装修", city="深圳",
        allow_cache_write=True,
    )
    rendered_wrote = [lock_fields(wrote[LOCK_MAP_KEY], k) for k in KWS]

    state["saved"].clear()
    monkeypatch.setattr(bp, "_fetch_trust_asset_for_pricing",
                        lambda *a, **k: {"source": "collected"})
    not_wrote, _ = await bp.generate_batch_quote(
        keywords=list(KWS), brand_name="测试品牌", industry="装修", city="深圳",
        allow_cache_write=True, brand_id=123,
    )
    rendered_not = [lock_fields(not_wrote[LOCK_MAP_KEY], k) for k in KWS]

    assert rendered_wrote != rendered_not, "两边渲染一样 = 判据没接上,整条作废"
    assert all(r["price_locked_until"] for r in rendered_wrote)
    assert all(r["price_locked_until"] is None for r in rendered_not)
    assert all(r["price_lock_note"] == NO_LOCK_NOTE for r in rendered_not)


@pytest.mark.asyncio
async def test_C4_blowup_guard_strips_per_keyword_not_per_order(engine, monkeypatch):
    """必须命中 + 必须不命中在同一单里:爆价放飞词无锁期,同单其他词有锁期。"""
    bp, state = engine
    import tools.keyword_value_scorer as kvs
    state["expires"] = (datetime.now() + timedelta(days=7)).isoformat()

    async def _mixed(keywords, **kw):
        rows = []
        for k in keywords:
            rows.append(_fake_scored(k, guarantee_unavailable=(k == KWS[1])))
        return rows, {}

    monkeypatch.setattr(kvs, "score_keywords", _mixed)

    quote_data, _md = await bp.generate_batch_quote(
        keywords=list(KWS), brand_name="测试品牌", industry="装修", city="深圳",
        allow_cache_write=True,
    )
    lock_map = quote_data[LOCK_MAP_KEY]
    assert lock_fields(lock_map, KWS[0])["price_locked_until"] is not None
    assert lock_fields(lock_map, KWS[1])["price_locked_until"] is None
    assert state["saved"] == [[KWS[0]]], "爆价词必须在写缓存前就被 _cacheable_rows 剥掉"


@pytest.mark.asyncio
async def test_C5_save_failure_locks_nothing(engine, monkeypatch):
    """写缓存抛异常 → 一个锁期都不许有(不许乐观登记)。"""
    bp, state = engine
    import db.diagnosis_db as ddb

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ddb, "save_keyword_prices_cache", _boom)
    quote_data, _md = await bp.generate_batch_quote(
        keywords=list(KWS), brand_name="测试品牌", industry="装修", city="深圳",
        allow_cache_write=True,
    )
    assert quote_data[LOCK_MAP_KEY] == {}


@pytest.mark.asyncio
async def test_C6_cache_hit_uses_that_rows_expires_not_today_plus_7(engine):
    """命中缓存的词:锁期 = 那行的 expires_at,不是今天+7(老行只剩 2 天就只能承诺 2 天)。"""
    bp, state = engine
    import db.diagnosis_db as ddb

    row_expires = (datetime.now() + timedelta(days=2)).isoformat()
    cached = {KWS[0]: {"expires_at": row_expires, "standard_articles": 7, "standard_price": 900,
                       "cost_per_article": 60, "entry_price": 500, "entry_articles": 4,
                       "flagship_price": 1300, "flagship_articles": 10,
                       "effective_competition": 10, "competitor_count": 10}}
    import pytest as _pt  # noqa
    ddb.get_cached_keyword_prices = lambda *a, **k: cached  # fixture 已 monkeypatch,这里再改回具体值
    state["expires"] = (datetime.now() + timedelta(days=7)).isoformat()

    quote_data, _md = await bp.generate_batch_quote(
        keywords=list(KWS), brand_name="测试品牌", industry="装修", city="深圳",
        allow_cache_write=True,
    )
    lock_map = quote_data[LOCK_MAP_KEY]
    assert lock_map[KWS[0]] == row_expires[:10]
    assert lock_map[KWS[0]] != (date.today() + timedelta(days=7)).isoformat(), \
        "命中缓存的词被按'今天+7'渲染 = 老 bug 换个地方复发"
    assert lock_map[KWS[1]] == state["expires"][:10]   # 新词走写入返回值


# ============================================================================
# D. 渲染层:_build_compat_pricing_data(cluster compat DTO)真调用
# ============================================================================

def _cluster_data(keywords):
    return {
        "clusters": [{
            "is_selected": True,
            "core_keywords": [{
                "keyword": k, "is_selected": True,
                "entry": {"price": 500, "articles": 4},
                "standard": {"price": 900, "articles": 7},
                "flagship": {"price": 1300, "articles": 10},
                "intent": "commercial",
            } for k in keywords],
            "covered_keywords": [],
        }],
    }


def test_D1_compat_dto_renders_lock_only_for_locked_keywords():
    from api.selection_api import _build_compat_pricing_data

    locked_until = (date.today() + timedelta(days=7)).isoformat()
    lock_map = {KWS[0]: locked_until}
    out = _build_compat_pricing_data(_cluster_data(KWS), {}, [], lock_map)
    by_kw = {k["keyword"]: k for k in out["keywords"]}

    assert by_kw[KWS[0]]["price_locked_until"] == locked_until
    assert by_kw[KWS[0]]["price_lock_note"] is None
    # 必须不命中:同一单里没锁的词不许有日期
    assert by_kw[KWS[1]]["price_locked_until"] is None
    assert by_kw[KWS[1]]["price_lock_note"] == NO_LOCK_NOTE


def test_D2_compat_dto_empty_lock_map_shows_no_date_at_all():
    """P0-D 品牌的单(lock map 全空)→ 全部不显示锁期。"""
    from api.selection_api import _build_compat_pricing_data

    out = _build_compat_pricing_data(_cluster_data(KWS), {}, [], {})
    assert all(k["price_locked_until"] is None for k in out["keywords"])
    assert all(k["price_lock_note"] == NO_LOCK_NOTE for k in out["keywords"])
    # 反向对照:同一份 cluster_data 给了非空 lock_map 时必须有日期(证明本用例不是恒真)
    out2 = _build_compat_pricing_data(
        _cluster_data(KWS), {}, [], {k: (date.today() + timedelta(days=3)).isoformat() for k in KWS})
    assert all(k["price_locked_until"] for k in out2["keywords"])


# ============================================================================
# E. 源码级:渲染层不许再自己 now()+7 造锁期
# ============================================================================

_UNCONDITIONAL_LOCK = re.compile(
    r"cache_expires\s*=\s*\(\s*datetime\.now\(\)\s*\+\s*timedelta\(days=7\)")


def _src(rel):
    with open(os.path.join(REPO, rel), "r", encoding="utf-8") as f:
        return f.read()


def _strip_py_comments(src: str) -> str:
    """🔴 剥注释再断言 —— 否则命中的是解释这个 bug 的注释,删掉真代码照样绿。"""
    out = []
    for line in src.splitlines():
        out.append(re.sub(r"#.*$", "", line))
    return "\n".join(out)


def test_E1_selection_api_has_no_unconditional_lock_date():
    body = _strip_py_comments(_src("api/selection_api.py"))
    assert not _UNCONDITIONAL_LOCK.search(body), \
        "渲染层又出现无条件 now()+7 的锁期 —— 这正是本 bug 的形状"


def test_E1_reverse_the_regex_can_actually_catch_it():
    """反向对照:同一条正则对 bug 原文必须命中,否则 E1 是恒真的空判据。"""
    buggy = '    cache_expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")'
    assert _UNCONDITIONAL_LOCK.search(_strip_py_comments(buggy))


def test_E2_every_lock_render_site_goes_through_the_single_exit():
    """5 个渲染点必须全走 _lock_fields;不许有裸赋值 price_locked_until = <非 None>。"""
    body = _strip_py_comments(_src("api/selection_api.py"))
    naked = re.findall(r'"price_locked_until"\s*:\s*(?!None)', body) + \
            re.findall(r'\[\s*"price_locked_until"\s*\]\s*=', body)
    assert naked == [], f"存在绕过 _lock_fields 的裸赋值: {naked}"
    assert body.count("_lock_fields(") >= 5


# [开源 E3 · B2 · 2026-09-28] agents/social_agent.py 随 E3 删除,守它不兜底 +7 天的格退役。


def test_E3_reverse_the_regex_can_actually_catch_it():
    buggy = 'expires = kw_info.get("price_locked_until", (_dt.now() + timedelta(days=7)).strftime("%Y-%m-%d"))'
    assert re.search(r'get\(\s*"price_locked_until"\s*,', _strip_py_comments(buggy))


# ============================================================================
# F. 红线:不许动防污染闸的判据本身
# ============================================================================

def test_F1_should_write_shared_cache_behaviour_unchanged():
    """WO 红线:`_should_write_shared_cache` 判据逐位不变(本包只搬运它的结果)。"""
    from tools.batch_pricing import _should_write_shared_cache as f

    assert f(None, None, True) is True                                   # 默认口径 → 写
    assert f(None, 88.0, True) is False                                  # 自设单篇成本 → 不写
    assert f(None, None, True, cost_multiplier=1.2) is False             # 进货倍率 ≠1 → 不写
    assert f(None, None, True, trust_price_active=True) is False         # P0-D 信任快照 → 不写
    assert f(None, None, False) is False                                 # 显式关 → 不写
    assert f(None, None, None) is True                                   # 旧调用方回退旧判据
    assert f(2.0, None, None) is False


# ============================================================================
# G. 真 DB round-trip —— C 段把 DB 打桩了,这里必须真写真读
#    证明:save_* 返回的 expires_at 与 get_* 读回的是同一个,锁期日期确实来自 DB
# ============================================================================

def test_G1_shared_cache_roundtrip_exposes_expires_at():
    from db.diagnosis_db import get_cached_keyword_prices, save_keyword_prices_cache
    from tools.pricing_bands import CURRENT_PRICING_FORMULA_VERSION

    kw = "价格锁 round-trip 测试词"
    brand, industry, city = "锁期测试品牌", "装修", "深圳"
    row = _fake_scored(kw, pricing_formula_version=CURRENT_PRICING_FORMULA_VERSION)

    returned = save_keyword_prices_cache(brand, [row], industry=industry, city=city)
    assert returned, "save_keyword_prices_cache 必须返回本次写入的 expires_at"
    assert to_lock_date(returned) == (date.today() + timedelta(days=7)).isoformat()

    got = get_cached_keyword_prices(brand, [kw, "从没写过的词"], industry=industry, city=city)
    assert kw in got, "前提没成立:刚写的行读不回来"
    assert to_lock_date(got[kw]["expires_at"]) == to_lock_date(returned), \
        "读回的 expires_at 与写入返回值对不上 = 锁期日期无法自证"
    # 必须不命中:没写过的词不许出现(否则 G1 的"命中"没有判别力)
    assert "从没写过的词" not in got


def _ensure_llm_cache_table():
    """keyword_price_cache_llm 由 migration_008 建,不在 init_db 里 → 测试库自己跑一次那份 DDL。
    用真 migration 文件而不是手抄 DDL:手抄会和线上 schema 漂移,漂了这条用例就成了自欺。"""
    from db.connection import get_connection
    with open(os.path.join(REPO, "db/migration_008_pricing_llm_first.sql"), "r",
              encoding="utf-8") as f:
        ddl = f.read()
    conn = get_connection()
    try:
        conn.autocommit = True
        conn.cursor().execute(ddl)
    finally:
        conn.close()
    # 建表后再跑一次 init_db:生产靠它的 _safe_add_column 自愈补 assessor_version 列
    #   (模块 import 时 init_db 先跑过一次,那会儿表还不存在 → 补列是空转)。
    #   复用生产那条自愈路径,不手抄 ALTER。
    from db.diagnosis_db import init_db
    init_db()


def test_G2_llm_cache_roundtrip_exposes_expires_at():
    from db.diagnosis_db import get_llm_cached_keyword_prices, save_llm_keyword_prices_cache
    _ensure_llm_cache_table()

    kw = "LLM 价格锁 round-trip 测试词"
    brand, scope = "锁期测试品牌LLM", "装修施工"
    llm_row = {"keyword": kw, "intent": "commercial", "funnel": "consideration",
               "value_score_0_5": 3.0, "entry_price_yuan": 500,
               "standard_price_yuan": 900, "flagship_price_yuan": 1300,
               "should_quote": True, "reason_zh": "测试", "business_line": "",
               "needs_review": False, "review_reason": ""}

    returned = save_llm_keyword_prices_cache(brand_name=brand, llm_keywords=[llm_row],
                                            industry="装修", city="深圳", business_scope=scope)
    assert returned, "save_llm_keyword_prices_cache 必须返回本次写入的 expires_at"

    got = get_llm_cached_keyword_prices(brand_name=brand, keywords=[kw, "没写过"],
                                        business_scope=scope)
    assert kw in got
    assert to_lock_date(got[kw]["expires_at"]) == to_lock_date(returned)
    assert "没写过" not in got
    # 必须不命中:空入参不写库也不编日期
    assert save_llm_keyword_prices_cache(brand_name=brand, llm_keywords=[],
                                        industry="", city="", business_scope=scope) is None


def test_F2_cacheable_rows_behaviour_unchanged():
    from tools.batch_pricing import _cacheable_rows

    rows = [_fake_scored("a"), _fake_scored("b", blowup_no_cache=True),
            _fake_scored("c", guarantee_unavailable=True),
            _fake_scored("d", cost_snapshot_uncacheable=True)]
    assert [r["keyword"] for r in _cacheable_rows(rows)] == ["a"]
    # 反向对照:三 flag 全关时返回全集(0 行为变化)
    assert len(_cacheable_rows([_fake_scored(k) for k in "abcd"])) == 4
