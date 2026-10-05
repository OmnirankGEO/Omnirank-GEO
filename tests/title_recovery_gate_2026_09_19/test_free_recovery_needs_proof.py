# -*- coding: utf-8 -*-
"""WO_241 丙(3/3)· 免费补救必须**证明**自己是补救。

拆的依据是**钱有没有付过**:
  · 新加的词出标题 = 从没付过费 ⇒ 全新生产,走批量端点按子集计费;
  · 某批次里缺题的词补救 = 已经付过费 ⇒ 免费。

为什么补救必须免费:退费只在**一条题都没出**时触发,**部分成功一分不退**
⇒ 缺题的那些词用户已经付过了,再收一次就是对同一个词收两次。

为什么必须带证明:不带证明的免费路径 = **一条免费主路**,
任何人对任何词调一次就白拿一次生成。

🔴 跑真 PostgreSQL:授权判断是三条 SQL 的合取,假对象复现不出来。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

from services.title_batch_charge_registry import (      # noqa: E402
    authorize_recovery, record_batch_charge,
)

DSN = os.environ.get("TEST_DATABASE_URL")
OWNER, OTHER = 990501, 990502
QUOTE, OTHER_QUOTE = 77001, 77002
KW_MISSING, KW_DONE, KW_OUTSIDE = 8801, 8802, 8803
RID, OTHER_RID = "rv-batch-A", "rv-batch-B"


def _conn():
    c = psycopg2.connect(DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture()
def db():
    if not DSN:
        pytest.fail("TEST_DATABASE_URL 未设置 —— 本包必须连真库")
    c = _conn()
    cur = c.cursor()
    cur.execute("SELECT to_regclass('title_batch_charges') AS t")
    assert cur.fetchone()["t"] is not None, (
        "title_batch_charges 不在 —— 迁移 063 没跑,判据失去被测对象")
    cur.execute("DELETE FROM title_batch_charges WHERE quote_id IN (%s,%s)",
                (QUOTE, OTHER_QUOTE))
    cur.execute("DELETE FROM topics WHERE quote_id IN (%s,%s)", (QUOTE, OTHER_QUOTE))

    # 🔴 外键是**查出来的**(`pg_constraint` on topics),不是等报错一条条试:
    #    topics.quote_id → quotes(id) · topics.keyword_id → confirmed_keywords(id)。
    #    必填列同样查过(quotes 无必填;confirmed_keywords 只有 keyword)。
    # point_transactions.user_id → users(id)(第四条证明要造退费行,得先有用户)
    cur.execute("DELETE FROM point_transactions WHERE user_id IN (%s,%s)", (OWNER, OTHER))
    for uid in (OWNER, OTHER):
        cur.execute(
            "INSERT INTO users (id, username, password_hash, display_name)"
            " VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            (uid, "rv_gate_%d" % uid, "x", "RV 补救闸测试"),
        )
    for q in (QUOTE, OTHER_QUOTE):
        cur.execute("INSERT INTO quotes (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                    (q,))
    for kid, text in ((KW_MISSING, "缺题词"), (KW_DONE, "已出题词"),
                      (KW_OUTSIDE, "批次外的词")):
        cur.execute(
            "INSERT INTO confirmed_keywords (id, keyword) VALUES (%s, %s)"
            " ON CONFLICT (id) DO NOTHING",
            (kid, text),
        )

    # 这一批付过钱
    record_batch_charge(cur, generation_request_id=RID, user_id=OWNER,
                        quote_id=QUOTE, charge_tx_id=555001)
    # 批次里两个词:一个缺题、一个已出题
    cur.execute(
        "INSERT INTO topics (quote_id, keyword_id, original_keyword,"
        " optimized_title, status, generation_request_id)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (QUOTE, KW_MISSING, "缺题词", None, "failed", RID),
    )
    cur.execute(
        "INSERT INTO topics (quote_id, keyword_id, original_keyword,"
        " optimized_title, status, generation_request_id)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (QUOTE, KW_DONE, "已出题词", "一个成品标题", "completed", RID),
    )
    c.commit()
    yield c
    try:
        c.close()
    except Exception:
        pass


def _ok(db, **kw):
    args = dict(generation_request_id=RID, user_id=OWNER,
                quote_id=QUOTE, keyword_id=KW_MISSING)
    args.update(kw)
    return authorize_recovery(db.cursor(), **args)


# ══════════════════════════════════════════════════════════════════
# 反向对照先行:真批次、真缺题 ⇒ **必须放行**
# ══════════════════════════════════════════════════════════════════

def test_a_real_missing_keyword_in_a_paid_batch_is_allowed(db):
    """🔴 反向对照。少了它,把闸写成"一律拒"也能让下面每一条全绿 ——
    而那会把补救功能整个关掉,用户只能重新付费生成。
    """
    allowed, why = _ok(db)
    assert allowed, why


# ══════════════════════════════════════════════════════════════════
# 拒绝面:三件证明各缺一件
# ══════════════════════════════════════════════════════════════════

def test_no_request_id_is_refused(db):
    """不带凭据 ⇒ 拒。这条一旦放行,它就是一条免费主路。"""
    allowed, why = _ok(db, generation_request_id=None)
    assert not allowed and why == "no_request_id"


def test_a_forged_batch_id_is_refused(db):
    """伪造的批次 id ⇒ 拒。"""
    allowed, why = _ok(db, generation_request_id="rv-batch-does-not-exist")
    assert not allowed and why == "unknown_batch"


def test_another_users_batch_is_refused(db):
    """🔴 别人的批次 ⇒ 拒 —— 否则拿到一个 id 就能白嫖别人付过的批次。"""
    allowed, why = _ok(db, user_id=OTHER)
    assert not allowed and why == "batch_belongs_to_another_user"


def test_a_batch_from_another_quote_is_refused(db):
    """批次不在这个 quote 下 ⇒ 拒(路径与凭据必须指同一件事)。"""
    allowed, why = _ok(db, quote_id=OTHER_QUOTE)
    assert not allowed and why == "batch_belongs_to_another_quote"


def test_a_batch_that_was_never_charged_is_refused(db):
    """🔴 没扣成过的批次 ⇒ 拒。

    「已经付过费」是免费补救的**唯一理由**;付都没付过,就不存在"再收一次是二次付费"。
    组织路径走 reserve/settle 时没有 consume 笔 id ⇒ charge_tx_id 为空 ⇒ 落在这一支。
    """
    cur = db.cursor()
    cur.execute("UPDATE title_batch_charges SET charge_tx_id = NULL"
                " WHERE generation_request_id = %s", (RID,))
    db.commit()
    allowed, why = _ok(db)
    assert not allowed and why == "batch_was_never_charged"


def test_a_keyword_outside_the_batch_is_refused(db):
    """🔴 这个词不属于这一批 ⇒ 拒 —— 否则一个付过的批次能给任意词开免费通道。"""
    allowed, why = _ok(db, keyword_id=KW_OUTSIDE)
    assert not allowed and why == "keyword_not_in_batch"


def test_a_keyword_that_already_has_a_topic_is_refused(db):
    """🔴 已经出过题 ⇒ 不是"缺题",那是重复生成。"""
    allowed, why = _ok(db, keyword_id=KW_DONE)
    assert not allowed and why == "keyword_already_has_a_topic"


# ══════════════════════════════════════════════════════════════════
# 接线与对外形状
# ══════════════════════════════════════════════════════════════════

def test_recording_a_batch_charge_is_idempotent(db):
    """重试不该把 charge 换成另一笔。"""
    cur = db.cursor()
    record_batch_charge(cur, generation_request_id=RID, user_id=OWNER,
                        quote_id=QUOTE, charge_tx_id=999999)
    db.commit()
    cur.execute("SELECT charge_tx_id FROM title_batch_charges"
                " WHERE generation_request_id = %s", (RID,))
    assert cur.fetchone()["charge_tx_id"] == 555001, "重复写覆盖了原来那笔"


def test_the_endpoint_is_gated_before_it_does_any_work():
    """🔴 闸必须排在生成之前 —— 排在后面等于白跑一次 LLM 再说不行。"""
    import ast
    import io
    import re
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read(), "server.py")
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "api_generate_topic_for_keyword"), None)
    assert fn is not None, "找不到逐词端点"
    gate = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "authorize_recovery"]
    assert len(gate) == 1, "闸调 %d 次(应 1 次)" % len(gate)

    # 🔴 **只钉「调了闸」不够**:注毒写成 `(True,'x') or authorize_recovery(...)`,
    #    调用**textually 还在**、这条锁照样绿,**而闸已经不决定任何事**。
    #    (钉了「调用了谁」≠ 钉了「它的结果被谁用了」。)
    #    所以钉:闸的结果要落到一个变量上,且那个变量控制着一处 403 抛出。
    assigned = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", None) == "authorize_recovery"]
    assert assigned, "闸的返回值没有被赋给任何变量 —— 它的结论没人接"
    names = set()
    for a in assigned:
        for t_ in a.targets:
            names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ast.unparse(t_)))
    raising_on_gate = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.If):
            continue
        if not (names & set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                                       ast.unparse(n.test)))):
            continue
        body = ast.unparse(n.body)
        if "raise" in body and "403" in body:
            raising_on_gate.append(n.lineno)
    assert raising_on_gate, (
        "没有任何一处 `if <闸的结论>: raise 403` —— 闸的结论没有变成拒绝")
    work = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
            and "_auto_generate_topics_for_new_keyword" in
            (getattr(n.func, "id", "") or getattr(n.func, "attr", "") or "")]
    if work:
        assert gate[0] < min(work), "闸排在生成之后了"


def test_the_contract_names_where_the_request_id_comes_from():
    """🔴 契约必须写清 `generation_request_id` 的**出处**。

    它来自项目详情的 topics 行,**不是** generate-titles 回包(那里没有这个字段)。
    这一句写错,A 会去一个不存在的地方取,而两边各自都"实现完了"。
    """
    doc = (REPO / "services" / "topic_gen_charge.py").read_text(encoding="utf-8")
    assert "generation_request_id" in doc
    # 🔴 锚要**独一无二**。第一版我查的是 `"不是" in doc and "回包" in doc` ——
    #    这两个词在这份 docstring 里到处都是("不是端点名字"、"回包新增 charge"),
    #    于是把整段出处说明删掉,判据**照样绿**。
    #    (本仓:「某串出现过」被无关行满足。)
    anchor = "那个回包里**没有**这个字段"
    assert doc.count(anchor) == 1, (
        "出处说明的锚不唯一或不在(命中 %d 次)—— A 会去一个不存在的地方取 id"
        % doc.count(anchor))
    assert "TITLE_RECOVERY_NOT_AUTHORIZED" in doc, "契约没写拒绝时的 code"


# ══════════════════════════════════════════════════════════════════
# 契约文件自身:不许出现第二处"权威声明"
# ══════════════════════════════════════════════════════════════════

CONTRACT = REPO / "services" / "topic_gen_charge.py"
AUTHORITY = "以本文件为准,不以消息为准"


def test_the_contract_has_exactly_one_authoritative_section():
    """🔴 一份契约里**只许有一处**「以本文件为准」。

    2026-09-19 A 在实现时抓到本文件自相矛盾:丙 那节说 `isNewKwOnly` 走批量端点
    **显示价**,而旧的 218-c1 那节仍说它走逐词端点**不显示价** ——
    **两节都写着「以本文件为准」**,前端照哪一节建都"有依据"。

    🔴 最毒的地方在于:旧节的**事实句**(那条路径整段无计费)在丙之后**仍然为真**,
       过期的只是「按钮走哪条」这层映射 —— 所以它**读起来不像错的**。
       这正是我自己写进记忆的那条:**同一个事实写第二处,不是冗余,是将来的分歧。**

    反向对照:再贴一份权威声明,这条必须红。
    """
    doc = CONTRACT.read_text(encoding="utf-8")
    hits = doc.count(AUTHORITY)
    assert hits == 1, (
        "契约里有 %d 处「%s」—— 两处都自称权威时,前端照哪一处建都算「有依据」"
        % (hits, AUTHORITY))


def test_the_superseded_section_leaves_no_copyable_statement():
    """🔴 作废的那节必须压成**一行指路**,不留可被照抄的表述。

    留着原文 + 一句"已作废",下一个人照样会照着原文建 ——
    作废标记拦不住复制粘贴。
    """
    doc = CONTRACT.read_text(encoding="utf-8")
    assert "[作废 2026-09-19 · WO_241 丙]" in doc, "作废标记不在"
    # 旧节那句会被照抄的映射(「isNewKwOnly 走逐词端点」)必须不再出现
    assert "走\n     `generateTopicsForMissingKws()`" not in doc
    assert "那条路径整段没有任何计费调用,一分不收" not in doc, (
        "作废节的原文还在 —— 它读起来不像错的,会被照抄")


# ══════════════════════════════════════════════════════════════════
# 第四件证明:这一笔没有被退款(Review 追加 2026-09-19)
# ══════════════════════════════════════════════════════════════════

def _mark_refunded(db, charge_tx_id):
    """照生产的形状造一条退费行:`type='refund'` 且 `order_id = 那笔 consume 的 id`。"""
    cur = db.cursor()
    # 必填列查过:user_id / type / point_type / amount / balance_after
    # (`information_schema` is_nullable='NO' AND column_default IS NULL)。
    cur.execute(
        "INSERT INTO point_transactions"
        " (user_id, type, point_type, amount, balance_after, feature_code, order_id)"
        " VALUES (%s,'refund','paid',%s,%s,%s,%s)",
        (OWNER, 80, 0, "topic_gen", str(charge_tx_id)),
    )
    db.commit()


def test_a_refunded_batch_is_refused(db):
    """🔴 整批失败全额退之后,那批的词**不再是「已付」** ⇒ 免费补救必须拒。

    退完再给免费补救 = 白送一次生成;重试应当走批量、重新收费。
    """
    _mark_refunded(db, 555001)
    allowed, why = _ok(db)
    assert not allowed and why == "batch_charge_was_refunded", why


def test_an_unrefunded_batch_with_a_missing_keyword_is_still_allowed(db):
    """🔴 反向对照(Review 点名):**未退**且缺题 ⇒ 必须放行。

    少了它,把这条证明写成"一律拒"也能让上面那条绿 —— 而那会把补救整个关掉。
    """
    cur = db.cursor()
    cur.execute("DELETE FROM point_transactions WHERE user_id = %s", (OWNER,))
    db.commit()
    allowed, why = _ok(db)
    assert allowed, why


def test_a_refund_on_another_charge_does_not_block_this_one(db):
    """🔴 退的是**别的批次**那笔 ⇒ 不影响本批。

    否则一个用户任何一次退款都会把他其余批次的补救一起关掉
    —— 那是把「这一笔退了没」错读成「这个人退过款没」。
    """
    _mark_refunded(db, 777777)          # 与本批 555001 无关的一笔
    allowed, why = _ok(db)
    assert allowed, why


def test_the_contract_lists_as_many_checks_as_the_gate_performs():
    """🔴 契约里写的件数,必须等于闸**真的做**的件数。

    2026-09-19 Review 复核抓到:契约写「校验三件」,而
    `authorize_recovery` 实现了**四件**(第二件"这一笔没被退款"是当天追加的)。
    契约自称「以本文件为准」,却比实现少一格 —— **少的那一格没人会发现**,
    因为三件也都是真的,读起来完全正常。
    (同一天、同一份文件里的第二次:上一次是旧节与新节互相矛盾。)

    这里不数中文字面,数**实现里真实的拒绝分支**,再要求契约把它们都提到。
    """
    reg = (REPO / "services" / "title_batch_charge_registry.py").read_text(encoding="utf-8")
    doc = CONTRACT.read_text(encoding="utf-8")
    # 闸的拒绝理由里,属于"四件证明"的那些(no_request_id 是入参缺失,不计件)
    proofs = {
        "batch_was_never_charged": "付过",
        "batch_charge_was_refunded": "退款",
        "keyword_not_in_batch": "属于这一批",
        "keyword_already_has_a_topic": "还没出题",
    }
    for reason, must_mention in proofs.items():
        assert reason in reg, "闸里没有 %s 这条拒绝 —— 判据的参照物变了" % reason
        assert must_mention in doc, (
            "闸做了 %s 这一件,而契约里找不到对应说法(应提到「%s」)" % (reason, must_mention))
    assert "四件" in doc, "契约没写清到底几件 —— 件数本身就是会过期的那个数"


def test_the_contract_carries_the_named_todo_for_the_frontend():
    """🔴 契约必须**点名**那两处会被本闸打成 403 的前端调用,并说明要同车。

    事实(逐字核过 `WritingHall.tsx`):`:4180` 与 `:4207` 两处
    `POST .../generate-topic` **都不带 `generation_request_id`**
    ⇒ 本单上线后**全部 403**;而 `:4180` 那处在 `for` 循环里
    `catch { console.error }` 之后**无条件** `toast.success(`已为 N 个新关键词生成标题`)`
    ⇒ **用户看到"成功"而一条都没生成**。

    🔴 为什么这要有一格锁:我原来把它写成「新词走补救端点会被 403」——
       那是**陈述现状**,读的人不会知道自己要做什么。
       Review 复核时点破:跨窗契约里这类句子一律写成**点名的待办**。
       句子一旦被删掉或改回"描述",这一格就红。
    """
    doc = CONTRACT.read_text(encoding="utf-8")
    assert "给 A 的待办" in doc, "契约里没有点名给 A 的待办 —— 只描述现状不算提醒"
    for site in ("4180", "4207"):
        assert site in doc, "契约没点名 WritingHall.tsx:%s 这处调用" % site
    assert "同车" in doc, "契约没写明它必须与前端那半同车上线"
