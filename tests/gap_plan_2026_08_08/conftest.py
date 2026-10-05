"""P4 缺口作战计划 · 真库 + 真 ASGI 测试底座。

🔴 为什么必须真库真 ASGI(不是 mock):
   本包的两条命根子判据都是**接线判据**,不是函数判据 ——
     · 判据 #10「删容量守卫,报价 372 出现去写」:守卫在 _actions_for_item,
       但真正决定运营看到什么的是端点出参。mock 掉 DB 只能证明函数返回了什么,
       证明不了端点吐出了什么。
     · A5 行级归属:require_quote_access 会真读 quotes 行、真查 brands.owner_user_id。
       mock 掉它 = 把要测的那道门自己拆了。
   这正是记忆里那条「判据要打在接线上,不是函数上」——同一个坑已经踩过三次。

🔴 库锁死在本任务一次性容器上(与 tests/pricing_quote_wiring 同形态,不另造第二套)。
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]

EXACT_THROWAWAY_URL = "postgresql://geo_admin:gaptest@127.0.0.1:55610/geo_gapplan_test"

_configured = os.environ.get("TEST_DATABASE_URL", "").split("?", 1)[0]
if _configured != EXACT_THROWAWAY_URL:
    raise RuntimeError(
        "gap_plan 测试锁死在本任务一次性库上;"
        f"期望 {EXACT_THROWAWAY_URL!r},实得 {_configured!r}。"
    )

os.environ["DATABASE_URL"] = EXACT_THROWAWAY_URL

MIGRATION = ROOT / "db" / "migration_029_gap_operation_plan_2026_08_08.sql"

# 🔴 schema **不手写**:用现役的自愈 DDL 自己建
#    (db.diagnosis_db.init_db + db.research_answer_entity_db.init_research_answer_entity_tables)。
#    第一版我手写了一份精简 schema —— 那是第二套表定义,列一漏就测出假绿;
#    而且现役 init_db 在 import 期就会跑,手写的表还会跟它撞(实测 UndefinedColumn)。
#    用真 DDL 的代价是慢一点,换来的是「测试库的 quotes 与生产的 quotes 同一份定义」。
def _build_schema():
    from db.diagnosis_db import init_db
    from db.research_answer_entity_db import init_research_answer_entity_tables

    init_db()
    init_research_answer_entity_tables()

    # 🔴 补跑几条**早已上线、但不在 init_db 建表语句里**的历史列。
    #    不补 = 测试库比生产少列 → 读那些列的代码根本没被执行到,"通过"是假的。
    #    每条都注明出处,逐字照抄,不自己另写形态:
    #      · confirmed_keywords.monitoring_query — scripts/migration_phase1_m1abc.sql:27
    #        (生产 2026-08-08 实测 confirmed_keywords 31 列,确实有它)
    #      · brands.is_deleted — 软删标志。auth/brand_access._is_brand_owner:61 的 WHERE
    #        里直接引用它,缺列会让那个 try/except 吞成 False → 归属校验对**所有人**
    #        恒 404。这一条最阴:锁会全红,但看起来像"归属校验很严",
    #        实际是判据零判别力(2026-08-08 本包实测踩到)。
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "ALTER TABLE confirmed_keywords ADD COLUMN IF NOT EXISTS monitoring_query TEXT"
        )
        cur.execute(
            "ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE"
        )
        conn.commit()
    finally:
        conn.close()

# 🔴 容量口径已并轨到 services/article_capacity_contract:
#      authorized = SUM(confirmed_keywords.required_articles) WHERE is_core IS NOT FALSE
#      consumed   = COUNT(topics)          ← **交付槽**,不是 articles 行数
#    所以种子必须给 topics 播数,否则测的是"没有交付槽"的空场景,
#    与生产实测的 372(authorized 15 / topics 15 / articles 136)不同形。
#
# 报价 372 的真实形态 —— 逐字段取自 2026-08-08 生产只读实测,**不是编的**:
#   id=372 brand_id=615 brand_name='深圳栖舍设计装修有限公司' industry='建筑建材'
#   status='confirmed' total_articles=0 paid_amount=2448 paid_at='2026-06-22 16:06:57'
#   owner_user_id=NULL(历史空归属)
# 🔴 owner_user_id 为 NULL 这一点必须照搬:归属校验走的是 brand_id → brands.owner_user_id,
#    第一版若按工单 R2 去查 price_quotes.buyer_user_id,对 372 恒 404。
SEED = """
INSERT INTO brands (id, name, industry, owner_user_id) VALUES (615, '深圳栖舍设计装修有限公司', '建筑建材', 4001);
INSERT INTO quotes (id, brand_id, brand_name, industry, city, status, total_keywords,
                    total_articles, paid_amount, paid_at, confirmed_at, owner_user_id)
VALUES (372, 615, '深圳栖舍设计装修有限公司', '建筑建材', '广东省深圳市', 'confirmed',
        17, 0, 2448, '2026-06-22 16:06:57', '2026-06-22 16:06:57', NULL);
INSERT INTO confirmed_keywords (quote_id, keyword, monitoring_query, required_articles, is_core, status)
VALUES (372, '深圳哪家装修公司靠谱', '深圳哪家装修公司靠谱', 15, TRUE, 'pending'),
       (372, '深圳装修公司排名前十', NULL, 0, FALSE, 'pending');

-- 有额度的对照报价:除了 total_articles 之外与 372 完全一样。
-- 🔴 它的存在就是为了证明容量守卫**不是恒红** —— 单向断言证明不了判别力。
INSERT INTO brands (id, name, industry, owner_user_id) VALUES (616, '有额度的客户', '建筑建材', 4001);
INSERT INTO quotes (id, brand_id, brand_name, industry, city, status, total_keywords,
                    total_articles, paid_amount, paid_at, confirmed_at, owner_user_id)
VALUES (900, 616, '有额度的客户', '建筑建材', '广东省深圳市', 'confirmed',
        17, 5, 2448, '2026-06-22 16:06:57', '2026-06-22 16:06:57', NULL);
INSERT INTO confirmed_keywords (quote_id, keyword, monitoring_query, required_articles, is_core, status)
VALUES (900, '深圳哪家装修公司靠谱', '深圳哪家装修公司靠谱', 15, TRUE, 'pending');

-- 🔴 372 的 15 个交付槽:authorized 15 - consumed 15 = **可用 0**。
--    这才是设计包「报价 372 容量 0」成立的真正口径(生产实测 topics 恰好 15)。
--    并轨前我按 quotes.total_articles=0 判成 0 是**碰巧对**——那字段 137 张单里
--    52 张是与词表对不上的脏 0,换一张单就错。
-- 🔴 [WO_225-c1 §8.4b] 状态从 draft 改 completed。
--    新口径下 consumed = status IN ('completed','published') ——
--    `draft` 只是出了标题,**不是交付**(生产 3350 条 draft 正在顶爆 8 张单的容量)。
--    夹具若留 draft,372 会变成「15 槽全可用」,本包那条容量锁会因为
--    **另一个原因**转红,看起来像修法出了问题,其实是夹具没跟上口径。
--    生产 372 的 15 条实际是已交付的(authorized 15 / 已用满),所以 completed 才同形。
INSERT INTO topics (quote_id, original_keyword, status)
SELECT 372, '深圳哪家装修公司靠谱', 'completed' FROM generate_series(1, 15);

-- 🔴 [WO_225-c1 §8.5] 未付款的零容量单:Owner ④ 只对**已付款**放开写作闸,
--    未付款仍然一个执行动作都不许出(08_billing 未付款不执行)。
--    没有这张单,「未付款必红」那条锁就没有被测对象 ——
--    拿 372(paid_at 有值)去测未付款,测的是另一件事。
--    形态:status='confirmed' 但 paid_amount=0 / paid_at NULL / service_status NULL
--    ⇒ quote_is_payable() 三个分支全不成立 ⇒ False。
INSERT INTO brands (id, name, industry, owner_user_id) VALUES (617, '还没付款的客户', '建筑建材', 4001);
INSERT INTO quotes (id, brand_id, brand_name, industry, city, status, total_keywords,
                    total_articles, paid_amount, paid_at, confirmed_at, owner_user_id)
VALUES (998, 617, '还没付款的客户', '建筑建材', '广东省深圳市', 'confirmed',
        17, 0, 0, NULL, '2026-06-22 16:06:57', NULL);
INSERT INTO confirmed_keywords (quote_id, keyword, monitoring_query, required_articles, is_core, status)
VALUES (998, '深圳哪家装修公司靠谱', '深圳哪家装修公司靠谱', 15, TRUE, 'pending');

-- 对照报价 900:authorized 15、**0 个交付槽** → 可用 15,可执行。
--   (它证明容量守卫不是恒红;并轨后它的三态是 capacity_reserved =「额度已保留」,
--    因为 consumed=0 且 reserved=0 —— 合同刻意区分「有额度且已排过」与「一篇没排」。)

-- 别人家的报价(A5 越权用例的靶子)
INSERT INTO brands (id, name, industry, owner_user_id) VALUES (700, '别人家的客户', '建筑建材', 9999);
INSERT INTO quotes (id, brand_id, brand_name, industry, status, total_articles, paid_at)
VALUES (701, 700, '别人家的客户', '建筑建材', 'confirmed', 5, '2026-06-22 16:06:57');

-- 四引擎答案环境:同行实体在、客户不在(= 报价 372 的真实处境)
-- 🔴 industry_key 必须写**归一后**的值 'home_improvement',不是原文 '建筑建材'。
--    2026-08-08 生产实测:answer_facts/entities 两侧存的都是归一键
--    (home_improvement 5593 行 entities),而 normalize_industry_key('建筑建材')
--    → 'home_improvement'。第一版用原文播种 → 查询恒 0 命中 → 依据抽屉空着,
--    看起来像"这个行业没数据",实际是种子和生产不同形。
INSERT INTO geo_research_answer_facts (id, raw_id, industry, industry_key, query, engine, citation_urls)
VALUES (1,101,'装修建材','home_improvement','深圳装修公司哪家靠谱','DeepSeek','["https://www.cnblogs.com/a","https://zhuanlan.zhihu.com/b"]'),
       (2,102,'装修建材','home_improvement','深圳装修公司哪家靠谱','Kimi','["https://www.cnblogs.com/c"]'),
       (3,103,'装修建材','home_improvement','深圳装修公司哪家靠谱','千问','["https://baijiahao.baidu.com/d"]'),
       (4,104,'装修建材','home_improvement','深圳装修公司哪家靠谱','豆包','["https://www.cnblogs.com/e"]');
INSERT INTO geo_research_answer_entities
    (answer_fact_id, industry_key, engine, entity_name, entity_key, entity_type, recommendation_rank)
VALUES (1,'home_improvement','DeepSeek','某同行装饰一','tongxing_1','brand',1),
       (1,'home_improvement','DeepSeek','某同行装饰二','tongxing_2','brand',2),
       (2,'home_improvement','Kimi','某同行装饰一','tongxing_1','brand',1),
       (3,'home_improvement','千问','某同行装饰三','tongxing_3','brand',1),
       (4,'home_improvement','豆包','某同行装饰二','tongxing_2','brand',1);

-- 媒体核实结论:一个可自发、一个需代发、一个进不去
INSERT INTO media_outlets (name, platform, media_type, entry_assessment, verified_at, verified_by)
VALUES ('博客园','cnblogs.com','traditional','self_service', NOW(), 1),
       ('某行业榜单','ranking.example.com','traditional','mediated', NOW(), 1),
       ('进不去的站','closed.example.com','traditional','unreachable', NOW(), 1);
"""


def _connect():
    conn = psycopg2.connect(EXACT_THROWAWAY_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    conn.autocommit = True
    return conn


@pytest.fixture(scope="session")
def _schema():
    """建一次 schema(自愈 DDL 很重,每个用例重建要 5s+)。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        # 池里可能还攥着指向已 DROP 对象的旧连接,先清干净再让自愈 DDL 重建
        try:
            from db.connection import close_pool
            close_pool()
        except Exception:
            pass
        _build_schema()
        with conn.cursor() as cur:
            cur.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return True


# 每个用例清空的表。🔴 只清本包会写的 + 种子表,不 TRUNCATE 全库 ——
# 全库 TRUNCATE 会把 init_db 灌进去的默认配置行(geo_research_config 等)也清掉。
_RESET_TABLES = (
    "gap_plan_checkbacks", "gap_plan_publications", "gap_plan_items", "gap_plan_snapshots",
    "gap_assistant_audit", "topics", "confirmed_keywords", "quotes", "brands", "media_outlets",
    "geo_research_answer_entities", "geo_research_answer_facts",
)


@pytest.fixture()
def db(_schema):
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE %s RESTART IDENTITY CASCADE" % ", ".join(_RESET_TABLES))
        cur.execute(SEED)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def no_media_recommender(monkeypatch):
    """默认把现役推荐机器拔掉,由用例自己喂候选。

    🔴 不是"因为它难 mock" —— 是因为 recommend_for_publish_v2 会去查
       geo_engine_stats / citation_domain_weights 等一大票生产表,
       在测试库里它们不存在。真正要测的是「本包怎么消费它的产物」,
       所以把边界画在它的**返回值**上,并单独有一条锁证明本包不重排它的结果。
    """
    def _fake(**kwargs):
        return {
            "vertical": [
                {"media_name": "博客园", "platform": "cnblogs.com",
                 "ai_citation_domain": "cnblogs.com",
                 # 🔴 故意混入内部成本字段,证明本包做的是白名单转录而不是整包透传
                 "price": 888, "our_price_points": 130, "our_price_yuan": 66.6,
                 "publish_success_factor": 0.42},
                {"media_name": "某行业榜单", "platform": "ranking.example.com",
                 "ai_citation_domain": "ranking.example.com", "price": 1200},
            ],
            "generic": [
                {"media_name": "进不去的站", "platform": "closed.example.com",
                 "ai_citation_domain": "closed.example.com", "price": 100},
            ],
            "matched_industry": "建筑建材",
        }

    import services.placement_service as ps
    monkeypatch.setattr(ps, "recommend_for_publish_v2", _fake, raising=True)
    return _fake


def make_app(user: dict):
    """真 ASGI 应用:挂真 router + 注入 request.state.user,不 mock 任何归属校验。"""
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware

    from api.gap_plan_api import router

    app = FastAPI()

    class InjectUser(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            request.state.user = dict(user)
            request.state.organization_identity = None
            return await call_next(request)

    app.add_middleware(InjectUser)
    app.include_router(router)
    return app


_AGENT_PERMISSIONS = [
    "diagnosis:read", "quote:read", "writing:read", "publish:read", "monitoring:read",
]
AGENT_USER = {
    "user_id": 4001, "username": "agent_a", "is_admin": False, "agent_level": 1,
    "permissions": _AGENT_PERMISSIONS, "client_brand_ids": [],
}
OTHER_USER = {
    "user_id": 5002, "username": "agent_b", "is_admin": False, "agent_level": 1,
    "permissions": _AGENT_PERMISSIONS, "client_brand_ids": [],
}


@pytest.fixture(autouse=True)
def _materialize_plan_before_readonly_assistant_contract(request):
    """现役 P4 页面负责物化；小榜助手合同只验证读取同一代际。"""
    if request.path.name != "test_assistant_action_routes.py" or "db" not in request.fixturenames:
        yield
        return

    request.getfixturevalue("db")
    request.getfixturevalue("no_media_recommender")

    from fastapi.testclient import TestClient

    client = TestClient(make_app(AGENT_USER), raise_server_exceptions=False)
    for quote_id in (372, 900):
        response = client.get(f"/api/quotes/{quote_id}/delivery-plan")
        assert response.status_code == 200
    yield
ADMIN_USER = {"user_id": 1, "username": "root", "is_admin": True, "client_brand_ids": []}
