"""【工单外发现 · 2026-08-28】``_require_brand`` 的 SELECT 点了三列不存在的列。

🔴 这一条**不在 V5-A 工单里**
----------------------------
它是我跑 A-3 的端点级 409 判据时撞出来的:第一次真跑,九条全红,红在

    psycopg2.errors.UndefinedColumn: column "city" does not exist
    LINE 1: SELECT id, name, company_name, industry, city, business, des...

站点是 ``api/defensive_geo_assist_api.py::_require_brand``。查实之后确认是
**生产缺陷**,不是我的夹具缺列:

  · 生产 dump(``prod_schema_2026-08-19.sql``)里 ``brands`` 33 列,
    有 ``cities``(复数)与 ``city_scope``,**没有** ``city``;
  · ``business`` 长在 ``client_profiles`` 上,不在 ``brands``;
  · ``description`` 两张表都没有;
  · 全仓**没有任何** ``.sql`` 迁移或 ``db/brands_schema.py`` 的自愈列清单创建这三列。

``_require_brand`` 挡在**三个**端点前面 —— Z-3.1「AI 联网补齐」、
GET 客户链接面板、POST 重签同对象链接 —— 所以这三个端点从上线起就是
**确定性 500**,一次都没成功过。

与已知那一条同族:``CustomerLinksUnavailable`` 的文档里记着
「``agent_quotes`` 没有 ``brand_id`` 列 ⇒ 报价与门户两类恒 not_ready」。
那一轮修的是**面板内部**那条查询;而本条排在它**前面**,先炸 ——
所以那次修完,面板照样通不了,只是错误换了个地方出现。

为什么之前没被抓到:客户链接那几条既有判据都打在**域层**
(``build_panel`` / ``reissue_link`` 直接调),没有一条从 HTTP 进来过 ——
而 ``_require_brand`` 只在 HTTP 那一层。本仓记过这条:
「真 HTTP 必打真库」,少了那一层的判据,前置守卫里的洞就没人守。

判据形态
--------
① **列名 census**(主判据):``_require_brand`` 的 SELECT 里点名的每一列,
   都必须在生产 dump 的 ``brands`` 上真实存在。这条不需要库、跑得快,
   而且抓的是**整族**问题(照"调用方想要哪些键"写列名),不是这一个实例。
② **真 HTTP 行为臂**:三个端点在一把 dump 形态的真库上都不许 500。
"""
from __future__ import annotations

import ast
import re

import psycopg2
import psycopg2.extras
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# 🔴 [工单 V5-B · Codex fix-of-fix3 P2-NEW-4] 按**文件路径**载入,不走
#    `from tests.defgeo_v5a_2026_08_28 import _census`。
#    反例:宿主装了一个正规的 `site-packages/tests` 包、而本仓 `tests/` 又不是包时,
#    那句 import 会解析到**别人的** tests,collection 直接 ModuleNotFoundError。
#    失败关闭不是假绿,但它会让整包在别的机器上跑不起来。
#    路径载入对 sys.path 顺序免疫;载入后再断言它确实来自本仓(见 test_00)。
import importlib.util as _ilu
import pathlib as _pl

_spec = _ilu.spec_from_file_location(
    "defgeo_v5a_census", _pl.Path(__file__).with_name("_census.py"))
C = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(C)

ASSIST = "api/defensive_geo_assist_api.py"
PROD_SCHEMA = C.REPO / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"


def _dump_columns(table: str) -> list[str]:
    if not PROD_SCHEMA.is_file():
        pytest.skip("缺生产 schema 夹具 " + str(PROD_SCHEMA))
    txt = PROD_SCHEMA.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"CREATE TABLE public\.%s \((.*?)\n\);" % table, txt, re.S)
    assert m, "生产 dump 里找不到 %s —— 锚点过期" % table
    return [ln.strip().split()[0] for ln in m.group(1).splitlines() if ln.strip()]


def _selected_columns(rel: str, func: str, table: str) -> list[str]:
    """从 ``func`` 里那条 ``SELECT … FROM <table>`` 抽出被点名的列。

    走 AST 取字符串常量再拼 —— 这条 SQL 是**多段字符串相邻拼接**的,
    只看单个字面量会只拿到半条,漏掉的那半正好是出问题的那半。
    """
    src = (C.REPO / rel).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name != func:
            continue
        for sub in ast.walk(n):
            if not isinstance(sub, ast.Call):
                continue
            for arg in sub.args:
                text = None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    text = arg.value
                elif isinstance(arg, ast.JoinedStr):
                    text = "".join(v.value for v in arg.values
                                   if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if not text:
                    continue
                m = re.search(r"SELECT\s+(.*?)\s+FROM\s+(?:public\.)?%s\b" % table,
                              text, re.S | re.I)
                if m:
                    return [c.strip() for c in m.group(1).split(",") if c.strip()]
    return []


# ══════════════════════════════════════════════════════════════════════════
# ① 列名 census —— 不连库,跑得快,抓的是整族
# ══════════════════════════════════════════════════════════════════════════
def test_00a_census_module_comes_from_this_repo() -> None:
    """载进来的 `_census` 必须是**本仓这一份**。

    路径载入已经排除了第三方 `tests` 包遮蔽,这条把结果钉死:
    万一哪天有人把它改回名字 import,而机器上又恰好有个同名包,
    判据会拿着别人的扫描器给本仓打分 —— 那种绿最贵。
    """
    import pathlib

    here = pathlib.Path(__file__).resolve()
    mod = pathlib.Path(C.__file__).resolve()
    assert mod.parent == here.parent, "_census 不是本目录这一份:%s" % mod
    assert C.REPO == here.parents[2], "_census.REPO 指向了别的树:%s" % C.REPO


def test_00_denominator_is_alive() -> None:
    cols = _selected_columns(ASSIST, "_require_brand", "brands")
    assert cols, "抽不出 _require_brand 的 SELECT 列清单 —— 锚点过期,后面恒绿"
    assert len(cols) >= 3, "只抽到 %d 列(%s)—— 多段拼接可能只取到了半条" % (len(cols), cols)
    assert "name" in cols, "连 name 都没抽到,抽取器坏了:%s" % cols
    assert len(_dump_columns("brands")) >= 30, "生产 dump 的 brands 列数不对 —— 夹具可疑"


def test_01_every_selected_column_exists_on_brands() -> None:
    """主判据:点名的每一列都必须真实存在。

    改动前红在这里:``city / business / description`` 三列都不在 ``brands`` 上。
    列名照着"调用方想要哪些键"写、没照着"表上有哪些列"写 ——
    这正是本仓 SQL 四维核验第一条要挡的东西。
    """
    cols = _selected_columns(ASSIST, "_require_brand", "brands")
    have = set(_dump_columns("brands"))
    missing = [c for c in cols if c not in have]
    assert not missing, (
        "_require_brand 的 SELECT 点名了 brands 上不存在的列 %s。\n"
        "它挡在三个端点前面(Z-3.1 AI 补齐 / GET 客户链接面板 / POST 重签),"
        "这条语句一抛 UndefinedColumn,三个端点全是确定性 500。\n"
        "brands 实有相近列:%s"
        % (missing, sorted(c for c in have if "cit" in c or "busi" in c or "desc" in c)))


def test_02_census_has_discriminating_power() -> None:
    """判别力自证:把幻列塞回去,census 必须转红。

    没有这一发,「列都存在」与「抽取器抽了个空清单」长得一模一样。
    """
    # 🔴 毒样是**写死的一份清单**,不是"当前实际选中的列 + 幻列"。
    #    用后者的话这一发就跟被测代码的当前状态绑上了:在**底**那棵树上
    #    当前清单本来就含 city/description,拼出来会重复,断言变成
    #    arm-dependent —— 一条自证判据不该在红臂里因为"红得太多"而失败。
    have = set(_dump_columns("brands"))
    poisoned = ["id", "name", "city", "description"]
    missing = [c for c in poisoned if c not in have]
    assert missing == ["city", "description"], \
        "把已知幻列塞进去之后 census 没认出来 —— 它没有区分力:%s" % missing


def test_03_the_three_phantom_columns_are_really_absent() -> None:
    """把这次的事实钉死:这三列**确实**不在 brands 上,也不在任何自愈清单里。

    钉住它是为了将来:哪天真有人给 brands 加了 ``city``,这条会红,
    提醒人回来看一眼上面那条 census 的措辞还准不准。
    """
    have = set(_dump_columns("brands"))
    for c in ("city", "business", "description"):
        assert c not in have, "brands 现在有 %s 了 —— 本条的事实基础变了,回来复核" % c
    assert "cities" in have and "city_scope" in have, \
        "brands 的 cities / city_scope 不见了 —— 夹具或 schema 变了"

    from db.brands_schema import _BRANDS_SELF_HEAL_COLUMNS

    self_heal = {c for c, _t in _BRANDS_SELF_HEAL_COLUMNS}
    for c in ("city", "business", "description"):
        assert c not in self_heal, \
            "brands 自愈列清单现在会补 %s —— 那这条 SELECT 的前提变了" % c


# ══════════════════════════════════════════════════════════════════════════
# ② 真 HTTP 行为臂 —— 三个端点在 dump 形态的真库上都不许 500
# ══════════════════════════════════════════════════════════════════════════
pytestmark_integration = pytest.mark.integration


def _conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _bind_db(dsn):
    import db.connection as dbconn

    dbconn.DATABASE_URL = dsn
    dbconn._pool = None


@pytest.mark.integration
def test_04_require_brand_does_not_500_on_a_dump_shaped_db(chain_db) -> None:
    """行为臂:GET 客户链接面板在真库上不许 500。

    census 证的是"列名对得上",这一条证的是"这条路真的走得通" ——
    两者都要:只有 census 的话,SELECT 换成别的写法照样可能炸;
    只有行为臂的话,它只覆盖到它跑到的那一个端点。
    """
    dsn = chain_db("phantom")
    _bind_db(dsn)
    admin = _conn(dsn)
    cur = admin.cursor()
    cur.execute(
        "INSERT INTO users (id, username, display_name, password_hash, email, is_active) "
        "VALUES (9801,'v5a_x1','v5a','x','v5a_x1@example.com',1) ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES ('V5A_X1', 9801) RETURNING id")
    brand_id = int(cur.fetchone()["id"])

    from api import defensive_geo_assist_api as assist

    app = FastAPI()
    app.include_router(assist.router)

    @app.middleware("http")
    async def _inject(request: Request, call_next):           # noqa: ANN001
        request.state.user = {"user_id": 9801, "username": "v5a_x1", "is_admin": True}
        return await call_next(request)

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/defensive-geo/customer-links", params={"brandId": brand_id})
    admin.close()

    assert r.status_code != 500, (
        "GET 客户链接面板返 500 —— _require_brand 那条 SELECT 又炸了:%s"
        % r.text[:300])
    assert r.status_code == 200, "期望 200,实得 %d:%s" % (r.status_code, r.text[:300])
