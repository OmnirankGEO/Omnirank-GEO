# -*- coding: utf-8 -*-
"""官网定价页公开价目 GET /api/public/feature-costs(Review 09-27 · 官网 WO_292 前置)。

经**真实的** server.app 打(auth 中间件、路由注册都是生产那一套),不带任何凭据:
  · 免登录 200;顶层字段恰好 {as_of, items},每项恰好 {feature_name, cost_points, unit}(多一个就红);
  · feature_name 是**对外名称**(a4 定稿名称表 WO_290b,sha256 前缀 f3eca5e7c880),13 个逐字钉死;库里的内部名一个都不出现,
    库里改内部名官网不变(对照臂);
  · 每项的 unit 逐字等于钉死的计价单位(按用量追加的写「起」「另计」「每篇」「每词每天」…),不含倍率 / 现金;
  · 所有按功能对位的格都按 feature_code 对位(不按名字找):价 == 计费读的那个数(db.wallet_db.get_feature_pricing,
    middleware/billing.py 按它扣);白名单里上架且价 > 0 的全都在、顺序同白名单;下架的不出现;价 0 的不出现;
    白名单外的即使上架也不出现(对照臂:把它放进白名单就出现);
  · 响应里没有小数金额 / 货币符号 / 内部字段名的形状(检查器自带牙证);
  · Cache-Control: public, max-age=300;
  · 只读:读库那一笔事务先 SET TRANSACTION READ ONLY,整个请求没有写语句;
  · 读库失败 ⇒ 503 + 固定文案,不带异常细节,Cache-Control: no-store(不带 public,CDN 不缓存错误)。
改库的格都在 finally 里原样复原,并清掉进程内 30 秒缓存。
"""
from __future__ import annotations

import re
from datetime import datetime

import psycopg2
import pytest
from fastapi.testclient import TestClient

import api.public_feature_costs_api as pfc

URL = "/api/public/feature-costs"
#: (feature_code, 对外名称, 单位) —— 名称逐字取自 C:/AI-Test/开源/WO_290b_PRICING_PUBLIC_NAMES_2026-09-27.md
#: (a4 定稿、Review 核过;sha256 前缀 f3eca5e7c880);单位以交付单 C14_313 的追加规则为准。
EXPECTED = (
    ("geo_diagnosis", "GEO 专项诊断", "每次起,自定义题数另计"),
    ("report_regen", "诊断报告重新生成", "每次"),
    ("report_export", "诊断报告导出(PDF / PPTX)", "每次"),
    ("topic_gen", "选题生成", "每个关键词"),
    ("article_gen", "GEO 文章写作", "每篇"),
    ("article_rewrite", "GEO 文章补发 / 重写", "每篇"),
    ("monitor_single", "单次监测", "每词每次"),
    ("monitoring_keyword_daily", "关键词每日监测", "每词每天"),
    ("geo_research_selfserve", "GEO 单行业自助调研", "每次起,超出 15 题另计"),
    ("deep_analyze", "品牌行业深度解析", "每次"),
    ("industry_brief_rerun", "行业知识库字段重新生成", "每次"),
    ("brand_fill", "品牌信息 AI 一键填充", "每次"),
    ("autofill_brand", "客户资料 AI 补齐空缺字段", "每次最多,按实际补齐的字段结算"),
)
NAME = {c: n for c, n, _u in EXPECTED}
UNIT = {c: u for c, _n, u in EXPECTED}
EXPECTED_WHITELIST = tuple(c for c, _n, _u in EXPECTED)
#: 线上内部名(原样,名称表第三列里与对外名不同的那些)—— 一个都不许出现在响应里
INTERNAL_NAMES = ("GEO专项诊断", "报告导出PDF/PPTX", "GEO文章生成", "GEO文章补发/重写", "监测单次检测", "品牌深度行业解析",
                  "知识库字段重跑（AI）", "品牌信息AI填充", "客户资料 AI 补齐")
#: 绝不许进白名单的:社媒 / E3 删除 / 下架 / 内部 / Review 定的边界项
NEVER = {"social_diagnosis", "full_diagnosis", "hook_gen", "script_gen", "rewrite_gen", "learn_viral", "author_breakdown",
         "single_video", "content_review", "video_framework", "team_portrait", "ai_coach", "personality_refresh",
         "meeting_start", "meeting_continue", "task_route", "ai_suggestion", "team_analysis", "keyword_price",
         "managed_campaign_recharge", "managed_brand_recharge", "media_publish", "media_proxy_publish",
         "quote_generate", "geo_plan_unlock", "scheduled_monitoring", "trial_pass_apply", "profile_polish",
         "geo_douyin_image_post", "geo_douyin_image_post_regen", "geo_douyin_image_post_redraw",
         "geo_douyin_image_post_extra_card", "geo_douyin_topic_distill", "mktg_moments_copy", "mktg_poster_basic",
         "mktg_poster_pro", "mktg_bundle_std", "mktg_bundle_pro"}   # 边界项 Review 09-27 定:一律不上官网
FORBIDDEN_WORDS = ("feature_code", "cost_compute", "requires_paid", "wholesale", "platform_cost", "markup", "multiplier",
                   "cents", "supplier", "cost_cny", "price_cny")
DECIMAL = re.compile(r"\d+\.\d+")
MONEY = re.compile(r"[¥￥$€]|元|人民币")
PROBE = "zz_public_costs_probe_test"


def money_or_internal_shapes(text: str) -> list:
    """响应正文里不许出现的形状。返回命中的描述;空 = 干净。"""
    hits = [f"小数:{m}" for m in DECIMAL.findall(text)]
    hits += [f"货币:{m}" for m in MONEY.findall(text)]
    hits += [f"内部字段:{w}" for w in FORBIDDEN_WORDS if w in text]
    return hits


@pytest.fixture(scope="module")
def client():
    import server

    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture
def db():
    import os

    url = os.environ.get("TEST_DATABASE_URL") or ""
    if "test" not in url:
        pytest.skip("需要名字里带 test 的测试库")
    c = psycopg2.connect(url)
    c.autocommit = True
    pfc.reset_cache()
    yield c
    pfc.reset_cache()
    c.close()


def _get(client):
    pfc.reset_cache()
    r = client.get(URL)                                           # 不带 Authorization、不带 cookie
    assert r.status_code == 200, (r.status_code, r.text[:300])
    return r


def _rows(db, codes):
    with db.cursor() as cur:
        cur.execute("SELECT feature_code, feature_name, cost_points, is_active FROM feature_pricing "
                    "WHERE feature_code = ANY(%s)", (list(codes),))
        return {r[0]: r for r in cur.fetchall()}


def _eligible(db):
    rows = _rows(db, pfc.PUBLIC_GEO_FEATURE_CODES)
    return [c for c in pfc.PUBLIC_GEO_FEATURE_CODES if c in rows and rows[c][3] and rows[c][2] > 0], rows


def _expected_items(db):
    """按 feature_code 对位算出应有的 items(对外名称 + 计费读的价 + 单位),顺序同白名单。"""
    from db.wallet_db import get_feature_pricing

    eligible, _rows_ = _eligible(db)
    return [{"feature_name": NAME[c], "cost_points": int(get_feature_pricing(c)["cost_points"]), "unit": UNIT[c]}
            for c in eligible]


def _names(client):
    return [it["feature_name"] for it in _get(client).json()["items"]]


# ================================================================ 白名单常量

def test_whitelist_names_and_units_are_exactly_the_reviewed_table():
    assert pfc.PUBLIC_GEO_FEATURES == EXPECTED
    assert pfc.PUBLIC_GEO_FEATURE_CODES == EXPECTED_WHITELIST
    assert not (set(pfc.PUBLIC_GEO_FEATURE_CODES) & NEVER)
    assert len({n for _c, n, _u in pfc.PUBLIC_GEO_FEATURES}) == len(pfc.PUBLIC_GEO_FEATURES)   # 名称不重
    for code, name, unit in pfc.PUBLIC_GEO_FEATURES:     # 单位只写计量方式:没有倍率、没有现金;名称同样干净
        assert not re.search(r"[×xX*倍]|\d+\.\d+", unit), (code, unit)
        assert money_or_internal_shapes(unit) == [] and money_or_internal_shapes(name) == [], (code, name, unit)
        assert name not in INTERNAL_NAMES, (code, name)


# ================================================================ 接口

def test_public_without_login_and_exact_field_sets(client, db):
    r = _get(client)
    body = r.json()
    assert set(body) == {"as_of", "items"}
    datetime.fromisoformat(body["as_of"])
    assert body["as_of"].endswith("+00:00")
    assert body["items"], "测试库里白名单的功能一项都没上架?这格就没有判别力"
    for it in body["items"]:
        assert set(it) == {"feature_name", "cost_points", "unit"}, it
        assert isinstance(it["cost_points"], int) and it["cost_points"] > 0
        assert isinstance(it["feature_name"], str) and it["feature_name"]


def test_items_are_code_aligned_public_name_billing_price_and_unit(client, db):
    """按 feature_code 对位:每项 = (对外名称, 计费读的价, 单位),一项不少、顺序同白名单。
    名称对调 / 单位串位 / 漏一项 / 价不是计费读的数,都会让整张表不相等。"""
    assert _get(client).json()["items"] == _expected_items(db)


def test_only_public_names_appear_never_internal_ones(client, db):
    """按名字**逐字相等**判(不用子串:内部名「客户资料 AI 补齐」恰是对外名「客户资料 AI 补齐空缺字段」的前缀)。"""
    eligible, rows = _eligible(db)
    names = set(_names(client))
    assert names == {NAME[c] for c in eligible}
    for c in pfc.PUBLIC_GEO_FEATURE_CODES:
        internal = rows[c][1] if c in rows else None
        if internal and internal != NAME[c]:
            assert internal not in names, (c, internal)                                 # 库里的内部名不出库
    assert not (names & set(INTERNAL_NAMES)), names & set(INTERNAL_NAMES)


def test_renaming_the_internal_name_in_the_db_does_not_change_the_public_list(client, db):
    """对照臂:只改库里的内部名,对外不变(官网名称与库解耦)。"""
    eligible, rows = _eligible(db)
    code = eligible[0]
    before = _get(client).json()["items"]
    old = rows[code][1]
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET feature_name = %s WHERE feature_code = %s", ("内部改名探针", code))
        r = _get(client)
        assert r.json()["items"] == before
        assert "内部改名探针" not in r.text
    finally:
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET feature_name = %s WHERE feature_code = %s", (old, code))


def test_delisted_whitelisted_feature_disappears_and_comes_back(client, db):
    eligible, _rows_ = _eligible(db)
    code = eligible[0]
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET is_active = FALSE WHERE feature_code = %s", (code,))
        assert NAME[code] not in _names(client)
    finally:
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET is_active = TRUE WHERE feature_code = %s", (code,))
    assert NAME[code] in _names(client)                                                   # 对照:复原后又在


def test_zero_priced_whitelisted_feature_is_not_listed(client, db):
    eligible, rows = _eligible(db)
    code = eligible[0]
    price = rows[code][2]
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET cost_points = 0 WHERE feature_code = %s", (code,))
        assert NAME[code] not in _names(client)
    finally:
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET cost_points = %s WHERE feature_code = %s", (price, code))


def test_active_feature_outside_the_whitelist_never_appears(client, db, monkeypatch):
    """白名单外、已上架、价 > 0 的功能不出现(连它的内部名也不出现);对照臂:同一行放进白名单就以对外名称出现。"""
    internal = "对外价目探针功能"
    with db.cursor() as cur:
        cur.execute("""INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute,
                                                    requires_paid_points, is_active)
                       VALUES (%s, %s, 777, 1.23, FALSE, TRUE)
                       ON CONFLICT (feature_code) DO UPDATE SET is_active = TRUE, cost_points = 777""",
                    (PROBE, internal))
    try:
        assert "hook_gen" not in pfc.PUBLIC_GEO_FEATURE_CODES
        before = _get(client).json()["items"]
        assert before == _expected_items(db)                                            # 探针与上架的社媒功能都不在
        assert internal not in _get(client).text
        monkeypatch.setattr(pfc, "PUBLIC_GEO_FEATURES", pfc.PUBLIC_GEO_FEATURES + ((PROBE, "探针对外名", "每次"),))
        r = _get(client)
        assert r.json()["items"] == before + [{"feature_name": "探针对外名", "cost_points": 777, "unit": "每次"}]
        assert internal not in r.text
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM feature_pricing WHERE feature_code = %s", (PROBE,))


def test_no_decimal_money_or_internal_field_shapes(client, db):
    text = _get(client).text
    assert money_or_internal_shapes(text) == [], money_or_internal_shapes(text)
    # 牙证:检查器对这几种形状都会喊
    assert money_or_internal_shapes('{"cost_points": 1.5}')
    assert money_or_internal_shapes('{"feature_name": "诊断 ¥10"}')
    assert money_or_internal_shapes('{"feature_code": "x"}')
    assert money_or_internal_shapes('{"cost_compute": 3}')


def test_cache_header(client, db):
    r = _get(client)
    assert r.headers.get("cache-control") == "public, max-age=300"


def test_read_only_transaction_and_no_writes(client, db, monkeypatch):
    """只读:读库那一笔事务的第一句是 SET TRANSACTION READ ONLY,整个请求没有写语句;SQL 不取 feature_name 列。"""
    import db.connection as dbc

    seen = []
    real = dbc.get_connection

    class _Cur:
        def __init__(self, cur):
            self._c = cur

        def execute(self, sql, *a, **k):
            seen.append(" ".join(str(sql).split()))
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._c, n)

    class _Conn:
        def __init__(self, conn):
            self._c = conn

        def cursor(self, *a, **k):
            return _Cur(self._c.cursor(*a, **k))

        def __getattr__(self, n):
            return getattr(self._c, n)

    monkeypatch.setattr(dbc, "get_connection", lambda: _Conn(real()))
    _get(client)
    assert seen and seen[0] == "SET TRANSACTION READ ONLY", seen
    assert not [s for s in seen if re.match(r"(?i)\s*(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE)\b", s)], seen
    assert not [s for s in seen if "feature_name" in s], seen


def test_db_failure_is_503_fixed_text_no_detail_and_not_publicly_cacheable(client, db, monkeypatch):
    """Review 09-27 r2 ①:读库失败 ⇒ 503 + 固定文案;响应里不带异常细节;不带 public 缓存头(no-store)。"""
    import db.connection as dbc

    def boom():
        raise RuntimeError("secret-dsn-host:5432 password=hunter2")

    monkeypatch.setattr(dbc, "get_connection", boom)
    pfc.reset_cache()
    r = client.get(URL)
    assert r.status_code == 503
    assert r.json() == {"detail": pfc.UNAVAILABLE} and pfc.UNAVAILABLE == "价目暂时不可用,请稍后再试"
    assert "secret" not in r.text and "hunter2" not in r.text and "RuntimeError" not in r.text
    cc = r.headers.get("cache-control") or ""
    assert "public" not in cc and "max-age" not in cc and cc == "no-store", cc
    monkeypatch.undo()                                   # 对照:库恢复后照常 200、带 public 缓存头
    r = _get(client)
    assert r.headers.get("cache-control") == pfc.CACHE_CONTROL


# ================================================================ 单行业自助调研(r4 改判上官网)

def test_selfserve_research_is_listed_with_its_base_price_and_unit(client, db):
    """库里设 3900 上架 ⇒ 以对外名称出现,单位逐字,价 == 计费读的那个数;设 0 ⇒ 不出现。按 code 对位。"""
    from db.wallet_db import get_feature_pricing

    code = "geo_research_selfserve"
    before = _rows(db, [code]).get(code)
    try:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO feature_pricing (feature_code, feature_name, cost_points, cost_compute,
                                                        requires_paid_points, is_active)
                           VALUES (%s, '内部名不出库', 3900, 0, FALSE, TRUE)
                           ON CONFLICT (feature_code) DO UPDATE SET cost_points = 3900, is_active = TRUE""", (code,))
        items = _get(client).json()["items"]
        assert items == _expected_items(db)
        hit = [it for it in items if it["feature_name"] == NAME[code]]
        assert hit == [{"feature_name": "GEO 单行业自助调研", "cost_points": 3900, "unit": "每次起,超出 15 题另计"}], hit
        assert hit[0]["cost_points"] == int(get_feature_pricing(code)["cost_points"])
        with db.cursor() as cur:
            cur.execute("UPDATE feature_pricing SET cost_points = 0 WHERE feature_code = %s", (code,))
        assert NAME[code] not in _names(client)                                           # 价 0 不上
    finally:
        with db.cursor() as cur:
            if before is None:
                cur.execute("DELETE FROM feature_pricing WHERE feature_code = %s", (code,))
            else:
                cur.execute("UPDATE feature_pricing SET feature_name = %s, cost_points = %s, is_active = %s "
                            "WHERE feature_code = %s", (before[1], before[2], before[3], code))


def test_selfserve_unit_follows_the_included_prompt_constant():
    """单位里的 15 == 自助调研「基础价含到第几题」的常量;最少题数不超过它(「起」才成立)。
    字面量留在白名单里(端点不 import 调研模块),由这格对着常量钉住。"""
    import api.research_selfserve_api as rs

    unit = {c: u for c, _n, u in pfc.PUBLIC_GEO_FEATURES}["geo_research_selfserve"]
    nums = [int(x) for x in re.findall(r"\d+", unit)]
    assert nums == [rs.SELFSERVE_BASE_INCLUDED_PROMPTS], (unit, rs.SELFSERVE_BASE_INCLUDED_PROMPTS)
    assert "题" in unit and "项" not in unit                        # 与后端对用户的报错用词一致(题目数)
    assert rs.SELFSERVE_MIN_PROMPTS <= rs.SELFSERVE_BASE_INCLUDED_PROMPTS
