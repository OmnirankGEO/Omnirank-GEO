# -*- coding: utf-8 -*-
"""WO_255 · 订单详情把**供应商进货价**回给了服务商。

`GET /api/publish/orders/{order_id}` 与 `/batches/{batch_id}` 把
`publish_order_items` 的整行(`db/publish_db.py:891` 是 `SELECT *`)放进响应,
其中 `mhz_cost_yuan` 就是供应商 VIP 价(写入点 `api/publish_api.py:1328`
存的是 `media["price_vip"]`)⇒ 任何服务商拿自己的 token 打这两个端点
就能读到我方进货价($ 类泄漏)。

🔴 **两套减法名单都漏了它**:
  · `services/media_price_projection.COST_SIDE_FIELDS` —— 没有 `mhz_cost_yuan`
  · `services/defensive_geo/publish/media_identity` 的 `PRIVATE_COLUMNS` +
    `PRIVATE_COLUMN_PREFIXES`(含 `cost_`)—— `mhz_cost_yuan` 前缀是 `mhz_` 不是 `cost_`
  ⇒ 所以修法是**正列**:没列出来的一律不出。减法名单永远在追赶列名。

🔴 **一个命名与语义背离**(判据里钉住,免得下一个人据名字判断):
  `cost_yuan` 是 `our_price_yuan × discount` = **我方售价**,名字叫 cost 但它是卖价;
  `mhz_cost_yuan` 才是进货价。两个都不外露,但理由不同(冗余 vs 泄漏)。
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.publish_order_projection import (  # noqa: E402
    ITEM_PUBLIC_FIELDS, ITEM_WITHHELD_FIELDS,
    project_order_item, project_order_items,
)

PUBLISH_API = REPO / "api" / "publish_api.py"

#: 一行**真实形状**的 item(列名取自 `information_schema`,2026-09-22 现取 14 列)。
FULL_ITEM = {
    "id": 9001, "order_id": 7001, "media_id": 501, "media_name": "某某网",
    "cost_yuan": 38.0,            # 我方售价(元)
    "cost_points": 4940,          # 用户被扣的算力
    "mhz_cost_yuan": 19.0,        # 🔴 供应商进货价
    "mhz_order_id": "MHZ-778899",  # 🔴 供应商侧订单号
    "status": "published", "publish_url": "https://example.com/a",
    "reject_reason": None, "submitted_at": None,
    "published_at": None, "refunded_at": None,
}


def _flat_keys(obj, out=None):
    """把响应体里所有出现过的键名摊平 —— 嵌套几层都要查到。"""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _flat_keys(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flat_keys(v, out)
    return out


# ══════════════════════════════════════════════════════════════════
# 一、投影本身
# ══════════════════════════════════════════════════════════════════

def test_the_supplier_cost_does_not_survive_the_projection():
    """🔴 本单的全部意义。"""
    got = project_order_item(FULL_ITEM)
    assert "mhz_cost_yuan" not in got, "进货价还在:%s" % got
    assert "mhz_order_id" not in got, "供应商订单号还在"
    assert "cost_yuan" not in got


def test_what_the_user_is_entitled_to_still_comes_through():
    """🔴 反臂:别把该给的也删了。

    少了这一条,把投影写成"永远返回 {}"也能让上面那条绿。
    """
    got = project_order_item(FULL_ITEM)
    for k in ("id", "order_id", "media_id", "media_name", "cost_points",
              "status", "publish_url"):
        assert k in got, "%s 被误删了" % k
    assert got["cost_points"] == 4940, "用户自己被扣了多少,他有权知道"


def test_a_missing_column_is_not_backfilled_as_none():
    """🔴 行里没有的键**不补 None**:补了会让「这次没查这列」和「值就是空」同形。

    (WO_251 §4 栽过同一个形状:三态被压成两态,而读数完全一样。)
    """
    got = project_order_item({"id": 1, "status": "pending"})
    assert set(got) == {"id", "status"}, got


# ══════════════════════════════════════════════════════════════════
# 二、端点级:两种身份都不出
# ══════════════════════════════════════════════════════════════════

class _State:
    def __init__(self, user):
        self.user = user


class _Req:
    def __init__(self, user):
        self.state = _State(user)


AGENT = {"user_id": 113, "is_admin": False}
ADMIN = {"user_id": 112, "is_admin": True}


@pytest.fixture()
def patched_db(monkeypatch):
    import api.publish_api as mod
    order_row = {
        "id": 7001, "batch_id": "B-1", "user_id": 113, "article_id": 5,
        "article_title": "标题", "article_content": "正文全文",
        "status": "published", "total_cost_points": 4940, "media_count": 1,
        "created_at": None, "updated_at": None, "brand_id": 19,
    }
    batch_row = {
        "id": "B-1", "user_id": 113, "article_count": 1, "total_publish_count": 1,
        "total_cost_points": 4940, "status": "done", "created_at": None,
    }
    monkeypatch.setattr(mod, "get_order", lambda _id: dict(order_row))
    monkeypatch.setattr(mod, "get_order_items", lambda _id: [dict(FULL_ITEM)])
    monkeypatch.setattr(mod, "get_batch", lambda _id: dict(batch_row))
    monkeypatch.setattr(mod, "get_items_by_batch", lambda _id: [dict(FULL_ITEM)])
    monkeypatch.setattr(mod, "get_user_orders", lambda *a, **k: [dict(order_row)])
    return mod


@pytest.mark.parametrize("who", [AGENT, ADMIN], ids=["服务商", "admin"])
def test_order_detail_response_has_no_cost_side_key(patched_db, who):
    """🔴 工单点名:admin 与服务商**两种身份都不出**。

    (泄漏不因为"他是管理员"就变成不泄漏 —— 这两个端点的 admin 分支只是
     跳过归属校验,不该顺带跳过字段收口。)
    """
    resp = asyncio.run(patched_db.get_order_detail(7001, _Req(who)))
    keys = _flat_keys(resp)
    hit = sorted(k for k in ITEM_WITHHELD_FIELDS if k in keys)
    assert not hit, "%s 的响应里出现了:%s" % (who, hit)


@pytest.mark.parametrize("who", [AGENT, ADMIN], ids=["服务商", "admin"])
def test_batch_detail_response_has_no_cost_side_key(patched_db, who):
    resp = asyncio.run(patched_db.get_batch_detail("B-1", _Req(who)))
    keys = _flat_keys(resp)
    hit = sorted(k for k in ITEM_WITHHELD_FIELDS if k in keys)
    assert not hit, "%s 的响应里出现了:%s" % (who, hit)


def test_both_endpoints_still_return_the_items(patched_db):
    """🔴 反臂:收口不能把明细收没了。"""
    r1 = asyncio.run(patched_db.get_order_detail(7001, _Req(AGENT)))
    r2 = asyncio.run(patched_db.get_batch_detail("B-1", _Req(AGENT)))
    assert r1["items"] and r1["items"][0]["media_name"] == "某某网"
    assert r2["items"] and r2["items"][0]["cost_points"] == 4940
    assert r2["orders"] and r2["orders"][0]["article_title"] == "标题"


#: 🔴 既有名单按 **`mhz_media` 的语义**列的;同名列在 `publish_order_items` 里
#: 含义不同的,在这里点名并写明**为什么是另一回事**。
#: (上一次我在别处用自由文本"理由"把一个对不上的名字解释掉,结果那句话是假的 ——
#:  所以下面那一格会去**代码现场**核这条例外,不只是读这句话。)
CROSSCHECK_EXCEPTIONS: dict[str, str] = {
    "cost_points": (
        "在 publish_order_items 里是**用户被扣的算力**(写入点 api/publish_api.py "
        "把 our_price_points × discount 存进这一列),不是我方成本;"
        "既有名单里的 cost_points 指的是 mhz_media 那张表上的我方成本"),
}


def test_the_crosscheck_exception_is_true_at_the_code_site():
    """🔴 例外要能被机器核:`cost_points` 的写入值必须来自**售价侧**。

    只写一句"它是另一回事"是自由文本,没人校验 —— 那正是把名字对不上
    洗成制度结论的入口(本仓 09-20 栽过)。这里去写入点看它到底存的什么。
    """
    src = PUBLISH_API.read_text(encoding="utf-8")
    i = src.index('"cost_points": discounted_points')
    window = src[max(0, i - 400): i]
    assert "our_price_points" in window and "discount" in window, (
        "cost_points 的写入值不再来自 our_price_points × discount —— "
        "例外的前提没了,这一列要重新分类")


def test_the_two_existing_blacklists_are_also_clean_on_the_response(patched_db):
    """🔴 仓里两套既有减法名单的全集,在响应里也必须 0 命中(点名例外除外)。

    这一格不是重复:它用**别人的名单**来核我的正列 ——
    若我的白名单里混进了某个既有名单认定为私有的列,这里会红。
    """
    from services.defensive_geo.publish.media_identity import (
        PRIVATE_COLUMNS, PRIVATE_COLUMN_PREFIXES,
    )
    from services.media_price_projection import COST_SIDE_FIELDS
    resp = asyncio.run(patched_db.get_order_detail(7001, _Req(ADMIN)))
    keys = _flat_keys(resp)
    bad = sorted(k for k in keys
                 if k not in CROSSCHECK_EXCEPTIONS
                 and (k in COST_SIDE_FIELDS or k in PRIVATE_COLUMNS
                      or any(k.startswith(p) for p in PRIVATE_COLUMN_PREFIXES)))
    assert not bad, "响应里出现了既有名单认定的私有列:%s" % bad
    # 例外表也会过期:点名的列若已经不在响应里,说明分类变了,该删这条例外
    dead = sorted(k for k in CROSSCHECK_EXCEPTIONS if k not in keys)
    assert not dead, "这些例外已经不在响应里了,该删:%s" % dead


# ══════════════════════════════════════════════════════════════════
# 三、枚举锁:新加一列必须有人做决定
# ══════════════════════════════════════════════════════════════════

def test_every_real_column_is_either_public_or_withheld_with_a_reason():
    """🔴 `publish_order_items` 的**每一列**要么在正列里,要么在"不给"表里并写明理由。

    分母不取自我的记忆,取自库里现有的列
    (没有 `TEST_DATABASE_URL` 时退到仓内 DDL —— 两条路都不许"猜")。
    新加一列 ⇒ 这一格红 ⇒ 逼人当场决定给不给,而不是默认跟着 `SELECT *` 流出去。
    """
    cols = _real_columns()
    assert cols, "一列都没拿到 —— 尺子坏了,不是表空了"
    declared = set(ITEM_PUBLIC_FIELDS) | set(ITEM_WITHHELD_FIELDS)
    undecided = sorted(c for c in cols if c not in declared)
    assert not undecided, (
        "这些列没人决定给不给(新加的?):%s" % (undecided,))
    stale = sorted(c for c in declared if c not in cols)
    assert not stale, "登记了库里没有的列,该删:%s" % (stale,)
    for col, why in ITEM_WITHHELD_FIELDS.items():
        assert len(why) > 8, "不给的理由要写清:%s" % col


def _real_columns() -> set:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if dsn:
        try:
            import psycopg2
            import psycopg2.extras
            c = psycopg2.connect(dsn)
            c.cursor_factory = psycopg2.extras.RealDictCursor
            cur = c.cursor()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'publish_order_items'")
            cols = {r["column_name"] for r in cur.fetchall()}
            c.close()
            if cols:
                return cols
        except Exception:
            pass
    # 退路:仓内 DDL(`db/publish_db.py` 的建表语句)
    src = (REPO / "db" / "publish_db.py").read_text(encoding="utf-8")
    start = src.index("CREATE TABLE IF NOT EXISTS publish_order_items")
    body = src[start: src.index('"""', start)]
    cols = set()
    for line in body.splitlines()[1:]:
        tok = line.strip().split(" ")[0].strip(",")
        if tok and tok.isidentifier() and tok not in ("CREATE", "TABLE", "IF", "NOT", "EXISTS"):
            cols.add(tok)
    cols.discard("publish_order_items")
    return cols


# ══════════════════════════════════════════════════════════════════
# 四、接线与另一条链的分类
# ══════════════════════════════════════════════════════════════════

def _handler(name):
    tree = ast.parse(PUBLISH_API.read_text(encoding="utf-8"), "publish_api.py")
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


@pytest.mark.parametrize("fn_name", ["get_order_detail", "get_batch_detail"])
def test_the_raw_rows_are_not_returned_directly(fn_name):
    """🔴 只钉"调了投影"不够 —— 还要钉**返回的就是投影的结果**。

    注毒可以写成 `"items": items or project_order_items(items)`:
    调用 textually 还在,而返回的仍是裸行。所以这里查每个 `return` 的字典值:
    `items` / `orders` 这些键的值必须是一次 `project_*` 调用。
    """
    fn = _handler(fn_name)
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert returns, "%s 没有返回?" % fn_name
    for r in returns:
        if not isinstance(r.value, ast.Dict):
            continue
        for k, v in zip(r.value.keys, r.value.values):
            key = getattr(k, "value", None)
            if key in ("items", "orders", "order", "batch"):
                assert isinstance(v, ast.Call) and \
                    getattr(v.func, "id", "").startswith("project_"), (
                    "%s 的 %r 不是**直接**由 project_* 产出的:%s"
                    % (fn_name, key, ast.unparse(v)))


def test_the_other_order_item_reader_never_reaches_a_response():
    """🔴 另一条链(`db/meijiehezi_db.py::get_order_items_by_order`)的分类锁。

    该函数 docstring 写「11 个调用点,第 11 个把结果原样返给用户」——
    **那个端点今天已经不在了**(全仓 0 处把它的结果放进 return),
    连它推荐的归属守卫 `get_publish_order_owner` 也一个调用方都没有。
    这一格把"今天没有人把它返出去"钉住:谁要新加一个返回它的端点,这里会红。
    """
    hits = []
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "node_modules" in rel:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "get_order_items_by_order" not in src:
            continue
        try:
            tree = ast.parse(src, rel)
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Return) and n.value is not None:
                if "get_order_items_by_order" in ast.unparse(n.value):
                    hits.append("%s:%d" % (rel, n.lineno))
    assert not hits, "有人把这条链的裸行返出去了:%s" % (hits,)
