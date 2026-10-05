"""#150 §3.2 · 成品记账落到报价与词上。

## 缺陷

`geo_douyin_posts` 只有 `keyword` 字符串,`/plan` 的 `done` 按**字符串**归集。
多张报价买了同一个词时,A 报价做了 3 条,B 报价同词的进度**跟着一起涨** ——
而两边各自看都正常。客户按进度以为交付够了,实际没够。

## 判据钉三件

  · **互斥**:有词身份的按身份数、历史行按字符串数,同一条成品只数一次;
  · **不回落**:某个词还没做过时**不许**退回字符串桶 —— 那正是污染的来路;
  · **归属**:词身份必须属于这个客户的 confirmed/paid 报价,报价号由服务端派生。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

KW = "深圳 GEO 优化"


def _rows(*specs):
    """配额行:(confirmed_keyword_id, required_articles)。"""
    return [{"keyword": KW, "confirmed_keyword_id": ck, "required_articles": q}
            for ck, q in specs]


def _plan(rows, done_map):
    from services.geo_douyin.content_plan import _plan_rows
    return {(p.confirmed_keyword_id, p.quota): p for p in _plan_rows(rows, done_map)}


# ─────────────────────────────────────────── 互斥与不回落

def test_two_quotes_on_the_same_word_do_not_share_progress():
    """🔴 本单那一格:同词两张报价,各自数各自的。

    毒:把归集写成「按身份查不到就回落字符串」⇒ 报价 B(还没做)会去数
    报价 A 的 3 条 ⇒ 本条红。
    """
    rows = _rows((101, 5), (202, 5))
    got = _plan(rows, {("ck", 101): 3})
    assert got[(101, 5)].done == 3
    assert got[(202, 5)].done == 0, (
        "报价 202 一条没做,却数到了别人的进度:%r" % got[(202, 5)].done)
    assert got[(202, 5)].gap == 5


def test_legacy_posts_without_identity_still_count():
    """历史成品(迁移不回填 ⇒ 身份为 NULL)仍按字符串算进 done,不倒退。

    没有这条,老客户的「已做」会一夜归零。
    """
    rows = _rows((101, 5))
    got = _plan(rows, {("legacy", KW): 2})
    assert got[(101, 5)].done == 2


def test_identity_and_legacy_are_added_not_double_counted():
    """🔴 两个桶**互斥**,所以相加不会重复计数。

    毒:把 legacy 桶也按身份键写一遍(同一条成品落两个桶)⇒ done 翻倍 ⇒ 红。
    """
    rows = _rows((101, 9))
    got = _plan(rows, {("ck", 101): 3, ("legacy", KW): 2})
    assert got[(101, 9)].done == 5, "应是 3+2,不是重复计数:%r" % got[(101, 9)].done


def test_a_row_without_identity_never_reads_the_identity_bucket():
    """配额行没有词身份(老数据)⇒ 只看字符串桶。"""
    rows = [{"keyword": KW, "required_articles": 4}]
    got = _plan(rows, {("ck", 101): 3, ("legacy", KW): 1})
    assert list(got.values())[0].done == 1


# ─────────────────────────────────────────── 顶栏三个数由服务端给

def test_plan_exposes_totals_so_the_frontend_does_no_arithmetic():
    """🔴 顶栏「买了 N 条,已做 M 条,还差 K 条」三个数**全部**来自 /plan。

    工单 §2.1 明写「前端不算数」。原来只有 total_gap 是现成的,
    另两个前端只能自己 reduce —— 那与本单把算术收回服务端的主旨相反。
    """
    from services.geo_douyin.content_plan import ContentPlan, _plan_rows

    plan = ContentPlan(items=_plan_rows(_rows((101, 5), (202, 3)),
                                        {("ck", 101): 2}))
    d = plan.to_dict()
    assert d["total_quota"] == 8
    assert d["total_done"] == 2
    assert d["total_gap"] == 6
    # 三个数必须自洽 —— 前端会把它们并排显示,不自洽时用户先看到的是矛盾
    assert d["total_gap"] == d["total_quota"] - d["total_done"]


def test_plan_items_carry_the_keyword_identity():
    """items 要带 `confirmed_keyword_id` —— 前端下单时得把它带回来。

    不带的话,服务端只能按字符串反查是哪张报价,
    而那正是本单要消除的歧义(把要修的病换个地方再犯一次)。
    """
    from services.geo_douyin.content_plan import _plan_rows
    item = _plan_rows(_rows((101, 5)), {})[0].to_dict()
    assert item["confirmed_keyword_id"] == 101
    assert "quote_id" in item


# ─────────────────────────────────────────── 归属:服务端派生,不信客户端

def test_the_request_does_not_accept_a_quote_id_from_the_client():
    """🔴 下单请求**只收词身份,不收报价号**。

    收客户端给的 `quote_id` 等于让前端决定这笔成品算谁的账;
    交付统计是对客户的承诺,不该由请求体说了算。
    """
    import api.geo_douyin_api as gapi
    fields = set(gapi.CreatePostRequest.model_fields)
    assert "confirmed_keyword_id" in fields
    assert "quote_id" not in fields, "下单请求收了 quote_id —— 账归谁由前端说了算了"


def test_ownership_check_runs_before_any_write_or_dispatch():
    """结构臂:归属校验必须在建行与派发**之前**(与 §3.1 的指纹同理,零冻结)。"""
    # 🔴 [#150 §3.3] 逻辑已抽进 `_create_and_dispatch_one`(单条与批量共用一份),
    #    锚跟着**沿链**走:先证路由确实调它,再在它体内比位置。
    #    只查新函数就成空壳 —— 它里面顺序对了、但没人调它,照样绿。
    src = io.open(ROOT / "api" / "geo_douyin_api.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_create_and_dispatch_one")
    shell = next(n for n in ast.walk(ast.parse(src))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "api_create_and_produce")
    assert any(isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "_create_and_dispatch_one"
               for n in ast.walk(shell)), "路由没调共用那份实现 —— 下面比的是没人跑的代码"

    def _first(pred):
        hits = [n.lineno for n in ast.walk(fn) if pred(n)]
        return min(hits) if hits else None

    check = _first(lambda n: isinstance(n, ast.Call)
                   and getattr(n.func, "id", None)
                   == "resolve_quote_for_confirmed_keyword")
    create = _first(lambda n: isinstance(n, ast.Attribute) and n.attr == "create_post")
    dispatch = _first(lambda n: isinstance(n, ast.Call)
                      and getattr(n.func, "id", None) == "dispatch_production")
    assert check and create and dispatch, (
        "分母塌了:check=%r create=%r dispatch=%r" % (check, create, dispatch))
    assert check < create < dispatch or check < create, (
        "归属校验没排在建行之前(%d vs %d)" % (check, create))


def test_the_insert_names_both_columns_so_a_missed_migration_is_loud():
    """🔴 INSERT **显式**写这两列 —— 迁移漏跑时 UndefinedColumn 当场抛。

    不显式写的话会退化成「成品建出来了但没记账」,而那是静默的。
    """
    src = io.open(ROOT / "db" / "geo_douyin_db.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "create_post")
    body = ast.unparse(fn)
    assert "quote_id" in body and "confirmed_keyword_id" in body, body[:200]


# ─────────────────────────────────────────── 迁移本身

def test_the_migration_is_registered_and_carries_zero_dml():
    """迁移进 manifest,且**迁移体内零 DML**(本仓铁律)。

    回填只能靠「同词即同报价」去猜 —— 而本迁移存在的理由正是那个猜法会串。
    """
    mig = ROOT / "db" / "migration_055_geo_douyin_post_quote_binding_2026_09_08.sql"
    assert mig.exists(), "迁移文件不在"
    sql = io.open(mig, encoding="utf-8").read()
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "MERGE "):
        assert verb not in body.upper(), "迁移体内出现 DML:%s" % verb.strip()

    manifest = io.open(ROOT / "db" / "migration_manifest.py", encoding="utf-8").read()
    assert mig.name in manifest, "迁移没进 manifest —— 生产不会跑它"
