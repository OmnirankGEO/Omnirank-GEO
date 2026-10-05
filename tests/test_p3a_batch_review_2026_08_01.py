"""P3a · 批量审核入口 + 两处并入项的锁(工单 §6 + P3 派单「并入项」)。

工单:docs/AI-CONTEXT/WORKORDER_REVIEW_GATE_AND_BATCH_AUDIT_2026-07-30.md §6
底:6cfbac47(P1+P2+P4 合批预合树,tree=eff158d3…;生产尖开工实测仍是 eac2200b)

锁 1 逐项隔离:成功项已 commit,不被后续失败回滚/覆盖
锁 2 🔴反向:一键通过挡住全部 H0(法律/平台档/lineage/人工拒稿/对象不存在)
锁 3 失败分级:4xx 不可重试,5xx 与非 HTTP 异常才给"重试这一篇"
锁 4 批量机审跳过已人工签发的文章(不许一次抹掉全部签发)
锁 5 计费边界:面板全部 authFetch URL ⊆ 零扣费白名单 + 两端点 AST 零计费调用
锁 6 真实计数(total/done/success/skipped/failed),禁伪造百分比
锁 7 前端主入口与分组口径(N 只数需处理的组;结果 Map 只 merge 不整体重置)
锁 8 并入项 1:`tenant_or_funds` → `operator_hard`(纯改名,成员集合不变)
锁 9 并入项 2:test_c4 纯静态断言已拆出且新文件无 fixture / 不 import server

⚠️ 覆盖边界(如实交代):
  · 锁 1/2/3/4/6 是**行为锁**,跑真 PG16、调真函数;
  · 锁 5(前端面)/7/9 是**静态结构锁** —— 本仓前端没有 vitest/jsdom 任何 DOM 测试设施
    (frontend/package.json 只有一条 playwright 的 obs:test),钉得住"代码走哪条路",
    钉不住"渲染出来长什么样"。真机 UI 复核仍需人过一遍。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from services.article_batch_review import (
    BatchItemRejected,
    MAX_BATCH_ITEMS,
    assert_bulk_pass_allowed,
    normalize_ids,
    review_one_article,
    run_batch,
)

ROOT = Path(__file__).resolve().parents[1]
WRITING_HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
SERVER = ROOT / "server.py"

_EV = "e" * 64


# ===========================================================================
# 真表(不是 TEMP)· 为什么
# ===========================================================================
# review_one_article 刻意**自带连接**(逐项隔离的落点)。TEMP 表只在建它的那条连接里
# 可见,所以用 TEMP 表根本测不到"另一条连接看不看得到已 commit 的结果" ——
# 而那恰恰是锁 1 唯一要证明的东西。故建真表,module 结束时 DROP。
# 库是一次性容器库(名字含 'test',conftest 的保险栓会核)。
_DDL = """
DROP TABLE IF EXISTS geo_article_review_events, articles, topics, quotes CASCADE;
CREATE TABLE quotes (
  id INTEGER PRIMARY KEY,
  brand_name TEXT,
  industry TEXT
);
CREATE TABLE topics (
  id INTEGER PRIMARY KEY,
  quote_id INTEGER,
  article_id BIGINT,
  optimized_title TEXT,
  original_keyword TEXT,
  user_choice TEXT
);
CREATE TABLE articles (
  id BIGINT PRIMARY KEY,
  quote_id INTEGER,
  topic_id INTEGER,
  content TEXT,
  title TEXT,
  current_content_hash CHAR(64),
  style_code VARCHAR(60),
  style VARCHAR(60),
  style_family VARCHAR(60),
  style_version VARCHAR(60),
  generation_request_id TEXT,
  article_review_status VARCHAR(40),
  article_human_review_status VARCHAR(40),
  article_human_reviewed_by INTEGER,
  article_human_reviewed_at TIMESTAMPTZ,
  article_human_review_reason TEXT,
  article_review JSONB,
  evidence_manifest_hash CHAR(64),
  evidence_pack JSONB,
  brand_fact_snapshot JSONB,
  publication_profile VARCHAR(60),
  platform_review JSONB,
  quality_warning JSONB
);
CREATE TABLE geo_article_review_events (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT NOT NULL,
  actor_user_id INTEGER NOT NULL,
  decision VARCHAR(40) NOT NULL CHECK (decision IN ('approved','rejected','skipped')),
  reason TEXT NOT NULL,
  machine_review_status VARCHAR(40),
  machine_review_version VARCHAR(100),
  reviewed_content_hash CHAR(64),
  evidence_manifest_hash CHAR(64),
  prior_human_review_status VARCHAR(40),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _connect():
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"],
                            cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module", autouse=True)
def _schema():
    conn = _connect()
    try:
        conn.cursor().execute(_DDL)
        yield
    finally:
        try:
            conn.cursor().execute(
                "DROP TABLE IF EXISTS geo_article_review_events, articles, topics, quotes CASCADE"
            )
        finally:
            conn.close()


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "true")
    conn = _connect()
    try:
        c = conn.cursor()
        c.execute("TRUNCATE geo_article_review_events, articles, topics, quotes")
        yield c
    finally:
        conn.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_CLEAN = "本地服务商的公开资质与交付记录可自行查证，建议按合同条款逐条核对。"
_ILLEGAL = "我们是行业第一的装饰公司，服务口碑毋庸置疑。"


def _seed(cur, *, article_id, review_status="approved", content=_CLEAN,
          human=None, reviewed=True, reviewed_content=None, quality_warning=None,
          style="buying_guide", style_family=None, evidence_hash=_EV):
    review_json = None
    if reviewed:
        review_json = json.dumps({
            "reviewed_content_hash": _sha(reviewed_content if reviewed_content is not None else content),
            "reviewed_evidence_manifest_hash": evidence_hash,
            "review_version": "v14-test",
        })
    cur.execute("INSERT INTO quotes(id, brand_name, industry) VALUES (%s,%s,%s) "
                "ON CONFLICT (id) DO NOTHING", (article_id, "测试品牌", "建材家居"))
    cur.execute(
        "INSERT INTO articles(id, quote_id, topic_id, content, title, style_code, style_family, "
        "article_review_status, article_human_review_status, article_review, "
        "evidence_manifest_hash, quality_warning) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)",
        (article_id, article_id, article_id, content, "本地装修怎么选", style, style_family,
         review_status, human, review_json,
         evidence_hash if reviewed else None,
         json.dumps(quality_warning) if quality_warning is not None else None),
    )
    cur.execute("INSERT INTO topics(id, quote_id, article_id, optimized_title, original_keyword) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (article_id, article_id, article_id, "本地装修怎么选", "本地装修"))


# ===========================================================================
# 锁 1 · 逐项隔离:成功项已 commit,不被后续失败回滚/覆盖
# ===========================================================================
def test_lock1_one_item_failure_does_not_abort_or_swallow_the_others():
    """驱动层:第 3 项抛异常 → 其余 4 项照常返回,整批不抛。

    变异「run_batch 不做 per-item try/except(异常直接冒泡)」→ 本条红。
    """
    def worker(item_id: int) -> dict:
        if item_id == 3:
            raise RuntimeError("boom")
        return {"ok_marker": item_id}

    summary = run_batch([1, 2, 3, 4, 5], worker)
    assert summary["total"] == 5
    assert summary["success_count"] == 4
    assert summary["failed_count"] == 1
    got = {r["article_id"]: r["status"] for r in summary["results"]}
    assert got == {1: "ok", 2: "ok", 3: "failed", 4: "ok", 5: "ok"}
    # 成功项的 payload 没有被失败项覆盖掉
    assert [r for r in summary["results"] if r["article_id"] == 5][0]["ok_marker"] == 5


def test_lock1b_successful_items_are_already_committed_when_a_later_item_fails(db, monkeypatch):
    """🔴 真库面:第 3 篇炸掉时,前两篇必须**已经落库**(另一条连接看得见)。

    这是"成功项不回滚"的实质证明,也是 TEMP 表做不到的那部分 ——
    review_one_article 自带连接,TEMP 表在别的连接里根本不存在。

    变异「整批共用一个连接、循环结束才 commit」→ 第 3 篇的 rollback 会把前两篇一起
    抹掉,本条红。
    """
    import services.article_review_gate as gate

    for aid in (301, 302, 303, 304):
        _seed(db, article_id=aid, review_status="legacy_unreviewed", reviewed=False)

    real_refresh = gate.refresh_article_review

    def flaky(article_id, cursor=None):
        if article_id == 303:
            raise RuntimeError("模拟第 3 篇处理中途炸掉")
        return real_refresh(article_id, cursor=cursor)

    monkeypatch.setattr(gate, "refresh_article_review", flaky)

    summary = run_batch([301, 302, 303, 304], review_one_article)
    assert summary["failed_count"] == 1
    assert summary["success_count"] == 3

    # 换一条全新连接读:成功项的机审结论必须已经落库
    fresh = _connect()
    try:
        c = fresh.cursor()
        c.execute("SELECT id, article_review_status, article_review FROM articles "
                  "WHERE id = ANY(%s) ORDER BY id", ([301, 302, 303, 304],))
        rows = {r["id"]: r for r in c.fetchall()}
    finally:
        fresh.close()
    for aid in (301, 302, 304):
        assert rows[aid]["article_review"] is not None, f"成功项 {aid} 没落库 = 被回滚了"
    assert rows[303]["article_review"] is None, "失败项不该留下半成品"


# ===========================================================================
# 锁 2 · 🔴 反向:一键通过挡住全部 H0
# ===========================================================================
def test_lock2_bulk_pass_guard_rejects_every_h0_class(db):
    """工单 §6 红线:"一键通过"不得越过资金、权限、租户隔离、对象完整性。

    🔴 [§3A 降级 2026-08-01] 原红线含"法律 H0"。Owner 拍板内容类硬门全降提示级后,
    法律/平台档已不是 H0,继续把它们算进"不得越过"就是**与新口径直接打架**。
    因此这两类移到放行面,H0 面只剩 lineage / 人工拒稿 / 对象不存在三类。
    提示本身不因"一键通过"消失(由发布门 content_notices 承载,见三态锁 6c)。

    两面都必须非空:全拒(把功能做没了)与全放(把红线拆了)各自都会红。
    """
    _seed(db, article_id=401, review_status="blocked", content=_ILLEGAL)           # [§3A] 广告法 → 提示级,可通过
    _seed(db, article_id=402, review_status="rewrite_required")                    # [§3A] 平台档 → 提示级,可通过
    _seed(db, article_id=403, review_status="approved",
          content="改过的正文", reviewed_content="审核时的正文")                     # lineage 不一致 → 仍拒
    _seed(db, article_id=404, review_status="approved", human="rejected")          # 人工拒稿 → 仍拒
    _seed(db, article_id=405, review_status="legacy_unreviewed", reviewed=False)   # 未审 → 可通过

    rejected, passed = [], []
    for aid in (401, 402, 403, 404, 405, 999999):   # 999999 = 对象不存在
        try:
            assert_bulk_pass_allowed(aid)
            passed.append(aid)
        except BatchItemRejected as exc:
            assert exc.code == "h0_not_clear", f"{aid} 的拒绝原因不对: {exc.code}"
            rejected.append(aid)

    # [§3A] 401/402 由拒→放(内容类降级);剩下三类是仍然不许越过的真红线。
    assert set(rejected) == {403, 404, 999999}, f"H0 面不对: {rejected}"
    # 🔴 分布断言:两边都必须非空。全拒(把功能做没了)与全放(把红线拆了)各自都会红。
    assert passed == [401, 402, 405], f"该放行的没放行: {passed}"

    # 🔴 反向:降级不得连"对象完整性/人工拒稿"一起放掉。上面 set 相等已覆盖,
    # 这里再单独钉一次最容易被顺手放掉的那两条,避免日后有人把 set 改宽而不自知。
    assert 403 in rejected and 404 in rejected, "lineage / 人工拒稿 必须仍是 H0"


def test_lock2b_guard_recomputes_instead_of_trusting_caller_state(db):
    """护栏必须**当场重算**。构造:先 clear,再把正文改掉(lineage 就对不上了),
    第二次调用必须转为拒绝 —— 若它缓存了第一次的结论,本条红。"""
    _seed(db, article_id=411, review_status="approved")
    assert_bulk_pass_allowed(411)                      # 第一次:放行
    db.execute("UPDATE articles SET content=%s WHERE id=%s", ("被改过的正文", 411))
    with pytest.raises(BatchItemRejected):
        assert_bulk_pass_allowed(411)                  # 第二次:必须拒绝


# ===========================================================================
# 锁 3 · 失败分级(4xx 不给"重试这一篇")
# ===========================================================================
def test_lock3_retryable_grading_by_status_class():
    """403/404/409 原样重试还是同样结果 → retryable False;
    500 与非 HTTP 异常才值得重试。

    变异「_retryable 恒 True」→ 本条红(403 那几项会变成可重试)。
    """
    from fastapi import HTTPException

    def raiser(exc):
        def _w(_item_id):
            raise exc
        return _w

    cases = {
        403: HTTPException(status_code=403, detail="无权访问"),
        404: HTTPException(status_code=404, detail="不存在"),
        409: HTTPException(status_code=409, detail="状态冲突"),
        500: HTTPException(status_code=500, detail="服务器错误"),
    }
    retryable = {}
    for tag, exc in cases.items():
        summary = run_batch([1], raiser(exc))
        retryable[tag] = summary["results"][0]["retryable"]
    assert retryable == {403: False, 404: False, 409: False, 500: True}, retryable

    plain = run_batch([1], raiser(RuntimeError("db timeout")))
    assert plain["results"][0]["retryable"] is True

    # 主动拒绝(护栏命中)永远不可重试
    rejected = run_batch([1], raiser(BatchItemRejected("h0_not_clear", "有硬门")))
    assert rejected["results"][0]["retryable"] is False
    assert rejected["results"][0]["status"] == "skipped"


# ===========================================================================
# 锁 4 · 批量机审跳过已人工签发的文章
# ===========================================================================
def test_lock4_batch_review_never_wipes_an_existing_human_signoff(db):
    """🔴 refresh_article_review 会清空人工签发四字段(单篇编辑路径的正确行为)。
    但"审核全部已完成"照做 = 一次抹掉生产上全部人工签发,那是不可逆的数据破坏。

    变异「去掉 human_approved 跳过」→ 签发被抹 → 本条红。
    """
    _seed(db, article_id=501, review_status="approved", human="approved")
    _seed(db, article_id=502, review_status="legacy_unreviewed", reviewed=False)

    summary = run_batch([501, 502], review_one_article)
    by_id = {r["article_id"]: r for r in summary["results"]}
    assert by_id[501]["status"] == "skipped"
    assert by_id[501]["error_code"] == "human_approved_skipped"
    assert by_id[501]["retryable"] is False
    # 反向面:没有签发的那篇必须真的被处理(否则"全跳过"也能让上面几条全绿)
    assert by_id[502]["status"] == "ok"

    fresh = _connect()
    try:
        c = fresh.cursor()
        c.execute("SELECT id, article_human_review_status FROM articles WHERE id = ANY(%s)",
                  ([501, 502],))
        rows = {r["id"]: r["article_human_review_status"] for r in c.fetchall()}
    finally:
        fresh.close()
    assert rows[501] == "approved", "人工签发被批量机审抹掉了"


# ===========================================================================
# 锁 5 · 计费边界(P2 同型锁)
# ===========================================================================
BILLING_CALL_NAMES = {
    "deduct_points",
    "deduct_points_with_preference",
    "charge_on_success",
    "freeze_points",
    "commit_freeze",
    "consume_points",
    "charge_points",
}

#: 批量面板允许打的端点(全部零扣费)。
#: 🔴 白名单是**枚举式**的:新加一个端点就必须在这里显式登记并说明它为什么不扣费,
#: 而不是"看着像免费就放过去"。
ZERO_CHARGE_BATCH_URLS = {
    "/api/articles/batch-review",             # 纯规则机审,无 LLM、无 provider、零扣费
    "/api/articles/batch-advisory-continue",  # 只写确认标记与审计事件,零扣费
    # [P3b 2026-08-01] 一键修复全部系统问题(SSE)。会调模型,但成本在平台侧:
    # 它逐条复用既有免费端点 api_repair_article_finding(返回体自带 charged: False,
    # 额度由 span_repair_quota 限死),端点与 job 模块本身零扣费调用 ——
    # 这两条分别由 test_p2_writing_hall_advisory::lock4 与
    # test_p3b_repair_job::lock9 用 AST 钉住。
    # 🔴 这一行是被这条锁**逼**出来的:P3b 往面板加端点时它当场转红,
    #    必须来这里显式登记并写清为什么免费,不能"看着像免费就放过去"。
    "/api/articles/batch-repair-stream",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_ts_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释。

    🔴 必要性与 P2 同款:本包的注释里**刻意写了**"绝不落到 /api/writing/rewrite",
    否定说法同样会被 grep 命中 —— 不剥注释,下面这条锁会被自己的说明文字喂成假红。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def _batch_panel_block(source: str) -> str:
    start_marker = "function BatchReviewPanel(props: {"
    end_marker = "// [P3a] 批量面板到此为止"
    start = source.find(start_marker)
    assert start >= 0, f"找不到起始标记: {start_marker}"
    end = source.find(end_marker, start)
    assert end > start, f"找不到结束标记: {end_marker}"
    return source[start:end]


def test_lock5_every_network_call_from_the_batch_panel_is_zero_charge():
    """🔴 计费边界(派单点名的 P2 同型锁):枚举批量面板里**全部** authFetch 的 URL,
    逐个必须落在零扣费白名单内;**抓不到 URL 就自炸**。

    "抓不到就自炸"这一条不是装饰:提取口径一旦失效(比如以后换成别的请求封装),
    `set() - allowlist` 恒为空集,这条锁会安静地全绿到永远。

    变异「把 batch-review 换成 /api/writing/rewrite」→ 本条红。
    """
    panel = _strip_ts_comments(_batch_panel_block(_read(WRITING_HALL)))
    urls = re.findall(r"authFetch\(`([^`]+)`", panel)
    assert urls, "没抓到任何 authFetch 调用 —— 提取口径失效了,这条锁等于没跑"
    unexpected = sorted(set(urls) - ZERO_CHARGE_BATCH_URLS)
    assert not unexpected, f"批量面板打到了不在零扣费白名单里的端点: {unexpected}"
    assert "/api/writing/rewrite" not in panel, "批量面板里出现了收费的整篇重写端点"


def _endpoint_ast(func_name: str):
    tree = ast.parse(_read(SERVER))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    raise AssertionError(f"server.py 里找不到函数 {func_name}")


def _called_names(node: ast.AST) -> set:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_lock5b_batch_endpoints_and_service_never_call_any_billing_function():
    """AST 而非 grep:两个批量端点 + 整个服务模块都不存在扣费调用节点。

    AST 的意义:字符串匹配会被注释、日志文案、变量名喂假;AST 只认真正的调用节点。
    """
    for func_name in ("api_batch_review_articles", "api_batch_advisory_continue"):
        leaked = _called_names(_endpoint_ast(func_name)) & BILLING_CALL_NAMES
        assert not leaked, f"{func_name} 调用了扣费函数: {sorted(leaked)}"
    service = ast.parse(_read(ROOT / "services" / "article_batch_review.py"))
    leaked = _called_names(service) & BILLING_CALL_NAMES
    assert not leaked, f"article_batch_review 模块调用了扣费函数: {sorted(leaked)}"


def test_lock5c_batch_run_declares_zero_charge_in_response():
    """正面证据:返回体显式声明 charged=False,不只是"没调扣费函数"(那可能只是还没写)。"""
    summary = run_batch([1], lambda _i: {})
    assert summary["charged"] is False


# ===========================================================================
# 锁 6 · 真实计数,禁伪造百分比
# ===========================================================================
def test_lock6_counts_are_real_and_self_consistent():
    """工单 §6:"用真实 done/total,禁伪造百分比"。

    三条自洽 + 一条去重口径:
      · total == 归一化后的条目数(重复 id 只算一次);
      · done == len(results);
      · success + skipped + failed == done。
    变异「normalize_ids 不去重」→ total 与实际处理数对不上 → 本条红。
    """
    def worker(item_id: int) -> dict:
        if item_id == 2:
            raise RuntimeError("boom")
        if item_id == 3:
            raise BatchItemRejected("h0_not_clear", "有硬门")
        return {}

    summary = run_batch([1, 2, 3, 1, 2, "x", None], worker)
    assert summary["total"] == 3, "重复 id / 非法值没被归一化掉"
    assert summary["done"] == len(summary["results"]) == 3
    assert summary["success_count"] + summary["skipped_count"] + summary["failed_count"] == summary["done"]
    assert (summary["success_count"], summary["skipped_count"], summary["failed_count"]) == (1, 1, 1)
    # 不许出现任何"看起来在动"的伪造进度字段
    for forged in ("percent", "progress", "percentage", "eta"):
        assert forged not in summary, f"返回体出现了伪造进度字段: {forged}"


def test_lock6b_batch_size_is_capped_and_order_is_preserved():
    """上限真的生效(不然一个请求能占住线程池一格很久),且保序
    —— set() 会打乱顺序,用户对不上号就会以为"漏了几篇"。"""
    ids = normalize_ids(list(range(1000, 1000 + MAX_BATCH_ITEMS + 50)))
    assert len(ids) == MAX_BATCH_ITEMS
    assert ids == sorted(ids)
    assert normalize_ids([9, 3, 7, 3, 9]) == [9, 3, 7]


# ===========================================================================
# 锁 7 · 前端主入口与分组口径
# ===========================================================================
def test_lock7_main_entry_exists_and_opens_a_modal():
    """主入口「审核与修复(N)」存在,且开的是 Modal(Radix Dialog 走 Portal 挂 body,
    与列表行高无关)—— 与 P2 锁 2 同一条纪律:不许再用折叠元素撑开文章行。"""
    source = _read(WRITING_HALL)
    assert 'data-testid="open-batch-review-panel"' in source
    assert "审核与修复" in source
    panel = _strip_ts_comments(_batch_panel_block(source))
    assert "<Dialog " in panel and "<DialogContent" in panel
    assert panel.count("<" + "details") == 0, "批量面板不许用内联折叠结构"


def test_lock7b_attention_count_only_counts_groups_that_need_attention():
    """N 的口径:只数"还有待处理项"的组,`ready` 不计入。

    判据打在**数据表**上(REVIEW_GROUP_META 的 needsAttention 标记),不是打在
    某个数字上 —— 后者换个分组名就红,前者换名不红、改语义才红。
    """
    source = _read(WRITING_HALL)
    meta = _strip_ts_comments(source[source.index("const REVIEW_GROUP_META"):source.index("function reviewGroupOf")])
    # [P1-2 四档文案 2026-08-14] ready 组标签按 frontend_state_copy §1 更名
    # 「已就绪」→「可进入投放准备」;本锁判据本体是下面的 needsAttention 数据表,
    # 标签串只是定位锚,随文案 SSOT 同步。
    assert "ready: { label: '可进入投放准备'" in meta
    # ready 是唯一 needsAttention:false 的组
    assert meta.count("needsAttention: false") == 1
    assert meta.count("needsAttention: true") == 5
    counter = source[source.index("function attentionCount"):source.index("function BatchReviewPanel")]
    assert "REVIEW_GROUP_META[key].needsAttention" in counter, "N 没按 needsAttention 过滤"


def test_lock7c_results_map_is_merged_never_wholesale_reset():
    """🔴 "成功项不回滚、不被后续失败覆盖" 的前端面。

    判据是**不存在整体重置**:面板里不允许出现 `setReviewResults({})` /
    `setAdvisoryResults({})` 这类把累积结果清空的调用;写入路径必须基于 `prev` 展开。
    变异「mergeResults 从空对象起手」→ 本条红。
    """
    panel = _strip_ts_comments(_batch_panel_block(_read(WRITING_HALL)))
    for reset in ("setReviewResults({})", "setAdvisoryResults({})",
                  "setReviewResults({ })", "setAdvisoryResults({ })"):
        assert reset not in panel, f"批量面板整体重置了累积结果: {reset}"
    merge = panel[panel.index("const mergeResults"):panel.index("const runBatchReview")]
    assert "{ ...prev }" in merge, "结果写入没有基于 prev 展开 = 会把上一批的成功抹掉"


def test_lock7d_grouping_reads_only_the_tristate_never_reinfers():
    """分组口径只认后端三态,不许前端拿 article_review_status 自己再猜一遍。

    本仓踩过:界面说"待审核"、服务端说"法律硬门",用户按界面提示跑一次审核,
    跑完还是发不出去。分档 SSOT 只有 evaluate_publication_eligibility 一个。
    """
    source = _read(WRITING_HALL)
    grouping = _strip_ts_comments(
        source[source.index("function reviewGroupOf"):source.index("function attentionCount")]
    )
    assert "publication_h0_state" in grouping
    assert "article_review_status" not in grouping, "分组又开始拿机审快照自己推断了"


# ===========================================================================
# 锁 10 · Owner 2026-08-01 口径:AI 评估是主路径,本面板 = L3 残留 + 手动覆盖
# ===========================================================================
def test_lock10_panel_is_positioned_as_l3_residue_not_human_first():
    """定位必须写在界面上,不能只写在交接文档里。

    判据:定位说明块存在,且同时讲清两件事 ——
      · 主路径是 AI 自动评估;
      · 这里承载 L3(AI 判不了)与法律硬门。
    """
    panel = _batch_panel_block(_read(WRITING_HALL))
    assert 'data-testid="batch-panel-positioning"' in panel
    assert "AI 自动评估" in panel
    assert "L3" in panel and "法律真硬门" in panel


def test_lock10b_l3_groups_are_marked_and_ordered_first():
    """L3 三档(法律 / 平台档 / 需拍板)带 l3 标记,且在分组顺序里排在非 L3 之前。

    判据打在数据表与顺序数组上,不是打在某句文案上。
    """
    source = _read(WRITING_HALL)
    meta_block = _strip_ts_comments(
        source[source.index("const REVIEW_GROUP_META"):source.index("function reviewGroupOf")]
    )
    assert meta_block.count("l3: true") == 3, "L3 档数不是 3(法律 / 平台档 / 需拍板)"
    order_block = source[source.index("const REVIEW_GROUP_ORDER"):source.index("const REVIEW_GROUP_META")]
    order = re.findall(r"'([a-z_]+)'", order_block)
    l3_keys = {"legal_hard", "platform_profile_hard", "operator_hard"}
    l3_positions = [i for i, key in enumerate(order) if key in l3_keys]
    other_positions = [i for i, key in enumerate(order) if key not in l3_keys]
    assert max(l3_positions) < min(other_positions), f"L3 组没排在最前: {order}"


def test_lock10c_missing_ai_verdict_is_never_rendered_as_a_pass():
    """🔴 AI 结论缺席时**不许编**。

    这条是本轮口径变更里唯一有安全后果的地方:如果"没有结论"被渲染成
    "AI 说没问题",用户就会在 L3 卡片(法律硬门)上一键放过。
    判据:兜底文案明说"尚未给出结论",且兜底分支里不出现"无问题/已通过"这类结论词。
    """
    panel = _strip_ts_comments(_batch_panel_block(_read(WRITING_HALL)))
    assert 'data-testid="batch-row-ai-verdict"' in panel
    assert "topic.ai_review?.summary" in panel, "AI 结论没有走可选链读取(字段缺席会炸)"
    verdict_block = panel[panel.index('data-testid="batch-row-ai-verdict"'):]
    verdict_block = verdict_block[:verdict_block.index("</p>")]
    assert "尚未给出结论" in verdict_block
    for forged in ("无问题", "已通过", "AI 通过", "没有问题"):
        assert forged not in verdict_block, f"AI 结论缺席时被渲染成了结论:{forged}"


def test_lock10d_per_card_confirm_uses_the_guarded_free_path():
    """每卡「一键确认」必须走那条**带 H0 护栏的免费端点**,不是另开一条旁路。

    (URL 层面已由锁 5 的白名单覆盖;这一条钉的是"确认按钮接的就是那条链路"。)
    """
    panel = _strip_ts_comments(_batch_panel_block(_read(WRITING_HALL)))
    assert 'data-testid="batch-row-confirm"' in panel
    confirm_block = panel[panel.index('data-testid="batch-row-confirm"'):]
    confirm_block = confirm_block[:confirm_block.index("</Button>")]
    assert "runBatchAdvisoryContinue([topic.id])" in confirm_block, "一键确认没接批量确认链路"


def test_lock10e_confirm_button_is_not_rendered_where_it_can_only_fail():
    """🔴 别让按钮说谎:L3 三档点「确认」服务端必拒(护栏在服务端),
    渲染一颗必然失败的按钮 = 骗用户点第二次。

    判据:确认按钮的渲染条件是"真有待确认提示"(advisory_state === 'open'),
    否则渲染的是一句说明下一步的文字,而不是按钮。
    变异「去掉条件、无脑渲染按钮」→ 本条红。
    """
    panel = _strip_ts_comments(_batch_panel_block(_read(WRITING_HALL)))
    row_block = panel[panel.index('data-testid="batch-row-ai-verdict"'):]
    assert "topic.advisory_state === 'open' ?" in row_block, "确认按钮没有按待确认提示做条件渲染"
    assert 'data-testid="batch-row-next-step"' in row_block, "非确认场景没有给出下一步说明"


# ===========================================================================
# 锁 8 · 并入项 1:tenant_or_funds → operator_hard(纯改名)
# ===========================================================================
def test_lock8_operator_hard_rename_is_complete_and_behaviour_preserved(db):
    """改名要么全改要么别改:留一处旧值 = 前端 switch 落到 default,徽章说错话。

    行为面同时钉住:成员集合没变(人工拒稿仍在这个桶里、仍 eligible False)。
    """
    from services.article_review_gate import (
        H0_CLEAR, H0_LEGAL, H0_OPERATOR_HARD, H0_PLATFORM,
        evaluate_publication_eligibility,
    )

    assert H0_OPERATOR_HARD == "operator_hard"
    assert {H0_CLEAR, H0_LEGAL, H0_PLATFORM, H0_OPERATOR_HARD} == {
        "clear", "legal_hard", "platform_profile_hard", "operator_hard"
    }, "四档取值集合被改动了 —— 这不再是纯改名"

    _seed(db, article_id=801, review_status="approved", human="rejected")
    verdict = evaluate_publication_eligibility(801)
    assert verdict["publication_h0_state"] == "operator_hard"
    assert verdict["reason"] == "human_rejected"
    assert verdict["eligible"] is False, "改名把行为改了:人工拒稿不再阻断"


def test_lock8b_no_stale_tenant_or_funds_value_left_in_the_tree():
    """全仓扫一遍:`tenant_or_funds` 不得再作为**值**出现(注释里做改名记录不算)。"""
    # 🔴 豁免**恰好两个文件**,都是这条锁自己的器材:
    #   · 本文件 —— 要搜一个串就必须写出这个串,把自己算进去 = 这条锁永远红,等于没有;
    #   · 配套变异 runner —— M9 的职责就是把旧档名注回去,证明这条锁真有牙。
    # 豁免按**绝对路径逐个列举**,不是"所有 tests/"或"所有 scripts/" ——
    # 别人在这两个目录里新写的死用法照样会被抓。
    exempt = {
        Path(__file__).resolve(),
        (ROOT / "scripts" / "mutation_p3a_batch_review_2026_08_01.py").resolve(),
    }
    stale = []
    for path in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.ts")) + list(ROOT.rglob("*.tsx")):
        if "node_modules" in path.parts or ".git" in path.parts:
            continue
        if path.resolve() in exempt:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "tenant_or_funds" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "tenant_or_funds" not in line:
                continue
            bare = line.strip()
            if bare.startswith("#") or bare.startswith("//") or bare.startswith("#:"):
                continue
            stale.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not stale, f"还有地方在用旧档名 tenant_or_funds: {stale}"


# ===========================================================================
# 锁 9 · 并入项 2:test_c4 静态断言拆分
# ===========================================================================
_STATIC_FILE = ROOT / "tests" / "test_c4_static_assertions_2026_08_01.py"
_C4_FILE = ROOT / "tests" / "test_c4_review_autopilot_2026_07_27.py"

_MOVED = (
    "test_auto_repair_endpoint_refreshes_machine_review",
    "test_frontend_soft_is_background_reference_not_confirmation",
    "test_frontend_hard_summary_card_replaces_flat_cards",
    "test_admin_quality_panel_hard_only_uses_real_path",
)


def test_lock9_static_assertions_moved_out_of_the_fixture_bearing_file():
    static_src = _read(_STATIC_FILE)
    c4_src = _read(_C4_FILE)
    for name in _MOVED:
        assert f"def {name}(" in static_src, f"{name} 没搬到静态文件"
        assert f"def {name}(" not in c4_src, f"{name} 还留在 test_c4 里(搬了个副本?)"


def test_lock9b_static_file_has_no_fixture_and_no_env_dependent_import():
    """拆出来的意义全在这一条:**任何环境都能跑**。

    判据用 AST:文件里不得有任何 pytest fixture 装饰器,也不得 import
    server / db.* / psycopg2 —— 只要有一个,它就又会在无库环境里整体 ERROR,
    死断言继续静默积累。
    """
    tree = ast.parse(_read(_STATIC_FILE))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                text = ast.unparse(deco)
                assert "fixture" not in text, f"静态文件里出现了 fixture: {text}"
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {name for name in imported
                 if name == "server" or name == "psycopg2"
                 or name.startswith("db.") or name.startswith("services.")}
    assert not forbidden, f"静态文件 import 了依赖环境的模块: {sorted(forbidden)}"
