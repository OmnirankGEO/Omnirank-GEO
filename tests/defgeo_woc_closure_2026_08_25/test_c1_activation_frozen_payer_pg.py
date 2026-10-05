"""C-1 · 激活 outbox 冻结 payer(Codex 终审 P1-6)。

被测的那一格用一句话说清:**commercial basis 成立那一刻的付款人,
不该被之后的品牌转移改写。**

判据分三组:
  A. 入队真的把 tenant/payer 冻进了那一行(而且是**生产入队函数**写的);
  B. 物化器只消费冻结值 —— 品牌转移之后签出来的预算仍然挂在原付款人身上;
  C. 冻结值缺失(存量行)⇒ **转人工**,不回落现读。
"""

from __future__ import annotations

import hashlib

import pytest

from tests.defgeo_woc_closure_2026_08_25 import _seed
from tests.defgeo_woc_closure_2026_08_25.conftest import connect


@pytest.fixture(scope="module", autouse=True)
def _identities():
    _seed.install_identities()


def _confirmed_session(owner: int) -> dict:
    """建一份「客户即将确认」的报价:brand → quote → session → 冻结快照。"""
    brand = _seed.new_brand(owner=owner)
    quote = _seed.new_quote(brand_id=brand)
    token = "woc-" + hashlib.sha256(f"{brand}:{quote}".encode()).hexdigest()[:24]
    session_id = _seed.new_selection_session(
        brand_id=brand, quote_id=quote, token=token)
    snap_id, snap_hash = _seed.new_pricing_snapshot(
        quote_id=quote, session_id=session_id)
    return {"brand": brand, "quote": quote, "token": token,
            "session_id": session_id, "accepted": snap_id, "hash": snap_hash}


# ══════════════════════════════════════════════════════════════════════════
# A. 入队冻结 —— 走**生产**那条确认路径,不是夹具直插
# ══════════════════════════════════════════════════════════════════════════
def test_c1_01_production_confirm_freezes_tenant_and_payer() -> None:
    """🔴 走生产 ``_commit_frozen_quote_confirmation``,不是夹具自己 INSERT。

    夹具自己插一行只能证明"列能写";只有生产那条路径能证明
    **上线之后真的会写**。改动前那条路径恰恰一个都不传。

    拆红:把 ``selection_api`` 里那四个 kwarg 摘掉 ⇒ 本条红。
    """
    from api.selection_api import _commit_frozen_quote_confirmation

    ctx = _confirmed_session(_seed.TENANT_A)
    _commit_frozen_quote_confirmation(
        token=ctx["token"],
        expected_snapshot_id=ctx["accepted"],
        selected_tier="entry",
        final_keyword_ids="[]",
        confirmed_at="2026-08-25 11:00:00",
        confirmed_total_price=100.0,
        accepted_snapshot_hash=ctx["hash"],
    )

    rows = _seed.outbox_rows(ctx["accepted"])
    assert len(rows) == 1, f"入队没写成一行:{rows}"
    row = rows[0]
    assert int(row["tenant_owner_id"]) == _seed.TENANT_A, row
    assert int(row["payer_user_id"]) == _seed.TENANT_A, row
    assert str(row["payer_funding_policy"]) == "personal_wallet", row
    assert str(row["payer_principal_kind"]) == "personal", row


def test_c1_02_admin_tenant_freezes_the_platform_leg() -> None:
    """判别力:admin 身份必须冻出**平台腿**,不是个人钱包。

    没有这一条,上一条的"personal_wallet"可能只是因为那三列被写死了
    常量 —— 两臂都绿而判别位根本没被调用。
    """
    from api.selection_api import _commit_frozen_quote_confirmation

    ctx = _confirmed_session(_seed.TENANT_ADMIN)
    _commit_frozen_quote_confirmation(
        token=ctx["token"], expected_snapshot_id=ctx["accepted"],
        selected_tier="entry", final_keyword_ids="[]",
        confirmed_at="2026-08-25 11:00:00", confirmed_total_price=100.0,
        accepted_snapshot_hash=ctx["hash"],
    )
    row = _seed.outbox_rows(ctx["accepted"])[0]
    assert str(row["payer_funding_policy"]) == "admin_platform_ledger", row
    assert str(row["payer_principal_kind"]) == "platform_cost_center", row


def test_c1_03_legacy_path_still_writes_nothing() -> None:
    """未 enrolled(``accepted_snapshot_hash=None``)⇒ **零入队**,行为逐位不变。

    这一条守的是「旧入参旧行为不变」:C-1 的改动全部在 enrolled 那一支里,
    legacy 报价确认不该因为本包多出任何一行。
    """
    from api.selection_api import _commit_frozen_quote_confirmation

    ctx = _confirmed_session(_seed.TENANT_A)
    _commit_frozen_quote_confirmation(
        token=ctx["token"], expected_snapshot_id=ctx["accepted"],
        selected_tier="entry", final_keyword_ids="[]",
        confirmed_at="2026-08-25 11:00:00", confirmed_total_price=100.0,
        accepted_snapshot_hash=None,
    )
    assert _seed.outbox_rows(ctx["accepted"]) == []


def test_c1_04_missing_brand_owner_refuses_the_whole_confirmation() -> None:
    """归属解析不出来 ⇒ **整笔确认回滚**,不写一条 payer 为空的 activation。

    fail-closed 的方向:宁可这一次确认不成,也不留下一条"谁付钱以后再说"的行。
    """
    from services.quote_pricing_snapshot import QuoteSnapshotError
    from api.selection_api import _commit_frozen_quote_confirmation

    ctx = _confirmed_session(_seed.TENANT_A)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE brands SET owner_user_id=NULL WHERE id=%s", (ctx["brand"],))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(QuoteSnapshotError):
        _commit_frozen_quote_confirmation(
            token=ctx["token"], expected_snapshot_id=ctx["accepted"],
            selected_tier="entry", final_keyword_ids="[]",
            confirmed_at="2026-08-25 11:00:00", confirmed_total_price=100.0,
            accepted_snapshot_hash=ctx["hash"],
        )
    assert _seed.outbox_rows(ctx["accepted"]) == [], "拒绝之后仍然写了 activation 行"
    # 会话状态也必须回滚 —— 半状态(客户看到确认成功、激活没入列)正是要防的
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM keyword_selection_sessions WHERE id=%s",
                    (ctx["session_id"],))
        assert cur.fetchone()["status"] == "quoted", "确认被拒但会话已被改成 confirmed"
        conn.rollback()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# B. 物化只消费冻结值 —— **品牌转移之后**
# ══════════════════════════════════════════════════════════════════════════
def test_c1_10_brand_transfer_does_not_move_the_payer() -> None:
    """🔴 本项的**本体判据**。

    确认时 owner=A ⇒ 冻结 payer=A;之后品牌转给 B;物化器签出来的预算
    仍然必须挂在 **A** 身上。

    拆红:把 ``frozen_identity`` 换回"现读 brands.owner_user_id"⇒ 本条红
    (预算会挂到 B 上)。
    """
    from api.selection_api import _commit_frozen_quote_confirmation
    from services.defensive_geo import activation_materializer as _mat

    ctx = _confirmed_session(_seed.TENANT_A)
    _commit_frozen_quote_confirmation(
        token=ctx["token"], expected_snapshot_id=ctx["accepted"],
        selected_tier="entry", final_keyword_ids="[]",
        confirmed_at="2026-08-25 11:00:00", confirmed_total_price=100.0,
        accepted_snapshot_hash=ctx["hash"],
    )
    # ── 品牌转移 ────────────────────────────────────────────────────────
    _seed.set_brand_owner(ctx["brand"], _seed.TENANT_B)

    out = _mat.materialize_pending()
    assert out["materialized"] >= 1, out

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT tenant_owner_id, payer_user_id, funding_policy "
            "  FROM defgeo_provider_execution_budgets WHERE accepted_snapshot_id=%s",
            (ctx["accepted"],))
        row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    assert row is not None, "物化器没落库"
    assert int(row["tenant_owner_id"]) == _seed.TENANT_A, (
        f"品牌转移把预算挂到了新 owner 身上:{dict(row)} —— 冻结值没被消费")
    assert int(row["payer_user_id"]) == _seed.TENANT_A, dict(row)
    assert str(row["funding_policy"]) == "personal_wallet", dict(row)


def test_c1_11_materializer_no_longer_reads_the_live_brand_owner() -> None:
    """结构锚:物化器的取归属路径上**不再**有 ``brands.owner_user_id`` 现读。

    这一条与上一条是**纵深**不是重复:上一条打行为(转移后签给谁),
    本条打结构(那条现读的 SQL 还在不在)。行为判据可能因为夹具凑巧
    (比如新 owner 也解析成同一个人)而绿;结构锚不会。
    """
    import ast
    import io as _io

    from services.defensive_geo import activation_materializer as _mat

    # ══════════════════════════════════════════════════════════════════════
    # 🔴 剥离口径:去**注释 + docstring**,**保留其余字符串字面量**
    # ══════════════════════════════════════════════════════════════════════
    # 这一条是被自己的撕锁逼出来的订正。第一版用 tokenize 把 COMMENT **和
    # 全部 STRING** 都剥掉,理由是"上面两个函数的 docstring 里逐字引用了旧实现,
    # 那是病历不是缺陷"。病历那半句是对的,**剥全部字符串那半句是错的**:
    #
    #   要抓的东西是一条 SQL(``"SELECT owner_user_id FROM brands ..."``),
    #   而 SQL 在 Python 里**只可能长在字符串字面量里**。
    #   把字符串一起剥掉 ⇒ 这条锁在结构上不可能命中它要抓的那个缺陷。
    #
    # 实证:MUT-C1-01(把现读原样塞回 materialize_one)在第一版下**没有被杀**。
    # 那是一把"看起来在守、实际守不到"的锁 —— 比没有锁更糟。
    #
    # 正确的分界不是"代码 vs 字符串",是"**会被执行的东西** vs 病历":
    # ``ast.unparse`` 天然丢掉注释,再显式删掉 docstring 那一条 Expr 即可;
    # 其余字符串(含 SQL)原样保留。
    src = _io.open(_mat.__file__, encoding="utf-8", newline="").read()
    tree = ast.parse(src)
    targets = {"frozen_identity", "materialize_one"}
    segments: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in targets):
            continue
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                     # 删 docstring = 删病历
        stripped = ast.FunctionDef(
            name=node.name, args=node.args, body=body or [ast.Pass()],
            decorator_list=[], returns=None, type_params=[])
        ast.fix_missing_locations(stripped)
        segments.append(ast.unparse(stripped))
    assert len(segments) == len(targets), (
        f"取不到全部被测函数源码(取到 {len(segments)}/{len(targets)})—— 锚点已过期")
    joined = "\n".join(segments)

    # ══════════════════════════════════════════════════════════════════════
    # 🔴 锚打在「有没有真去查 brands 表」,不是打在 ``owner_user_id`` 这个词上
    # ══════════════════════════════════════════════════════════════════════
    # 第二版(剥 docstring、留字符串)照样自我命中:那句
    # 「不回落现读 brands.owner_user_id」长在 ``_NeedsHuman`` 的**错误消息**里,
    # 而错误消息不是 docstring,剥不掉。**病历只是换了个地方躲**。
    #
    # 结论:不能靠"这个词在不在"当判据 —— 那个词在正当理由下会反复出现。
    # 要判的是**行为形状**:这两个函数里还有没有一条查 ``brands`` 的 SQL。
    # 两条独立的锚,互为纵深:
    import re as _re

    sql_read = _re.compile(r"(?i)\bfrom\s+(?:public\.)?brands\b")
    assert not sql_read.search(joined), (
        "物化器的可执行面里又出现了查 brands 表的 SQL —— 归属回落到现读了")

    # 第二条锚**完全不依赖字符串匹配**:这两个函数在修好之后
    # 一条直接的 ``cur.execute`` 都没有(取归属读冻结值、落库走 _store/_act)。
    # 任何"现读回落"都必然长成一条 ``cur.execute`` —— 这条锚躲不开。
    direct_execs = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in targets):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "execute"):
                direct_execs.append(f"{node.name}:{ast.unparse(sub.func)}")
    assert not direct_execs, (
        f"这两个函数里出现了直接的游标查询:{direct_execs} —— "
        "取归属应当只读冻结值,落库走 _store/_act")

    # ── 探针活性:三向自证 ──────────────────────────────────────────────
    # ① 剥离没剥过头:真代码还在
    assert "frozen_identity" in joined and "payer_user_id" in joined, (
        "剥离之后可执行面是空的 —— 探针失去判别力")
    # ② 正样本:教科书式的现读必须被 sql_read 命中
    assert sql_read.search('cur.execute("SELECT owner_user_id FROM brands WHERE id = %s")'), \
        "sql_read 连教科书式的现读都命中不了 —— 正则写错了"
    assert sql_read.search("select x from public.brands b"), "限定名形态命中不了"
    # ③ 反样本:病历(散文里提到 ``brands.owner_user_id``)**不许**命中。
    #    这一条是本判据两次自我命中之后加的 —— 它钉住"锚不再打在那个词上"。
    assert not sql_read.search("不回落现读 brands.owner_user_id:品牌转移后……"), \
        "锚又退回成裸词匹配了 —— 病历会把它判红"
    assert _mat.census()["readsLiveBrandOwner"] is False


# ══════════════════════════════════════════════════════════════════════════
# C. 存量行 ⇒ 转人工,**不回落现读**
# ══════════════════════════════════════════════════════════════════════════
def _insert_legacy_outbox(ctx: dict, *, tenant: int | None) -> None:
    """存量形态:043 时代那种只有 brand(可能带 tenant)的行。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO defgeo_activation_outbox "
            "(accepted_snapshot_id, quote_id, brand_id, event_kind, "
            " accepted_snapshot_hash, status, tenant_owner_id) "
            "VALUES (%s,%s,%s,'commercial_basis_established',%s,'pending',%s)",
            (ctx["accepted"], ctx["quote"], ctx["brand"], ctx["hash"], tenant))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_tenant", [None, _seed.TENANT_A])
def test_c1_20_legacy_rows_go_to_needs_review(legacy_tenant) -> None:      # noqa: ANN001
    """存量 pending 行 ⇒ 立刻 ``needs_review``,且**没有**签出任何预算。

    两个参数化臂都必须红同一件事:
      · ``tenant_owner_id`` 为空 —— 显然缺;
      · ``tenant_owner_id`` 有值但 payer 三列为空 —— 这一臂才是关键:
        它长得"像有归属",而付款人仍然是未知的。回落现读会让这一臂静默通过。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _confirmed_session(_seed.TENANT_A)
    _insert_legacy_outbox(ctx, tenant=legacy_tenant)

    out = _mat.materialize_pending()
    assert out["materialized"] == 0, out
    assert out["needsReview"] >= 1, out

    rows = _seed.outbox_rows(ctx["accepted"])
    assert rows and rows[0]["status"] == "needs_review", rows
    assert "转人工" in str(rows[0]["last_error"] or ""), rows[0]["last_error"]

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM defgeo_provider_execution_budgets "
            " WHERE accepted_snapshot_id=%s", (ctx["accepted"],))
        assert int(cur.fetchone()["n"]) == 0, "转人工的那一条居然签出了预算"
        conn.rollback()
    finally:
        conn.close()


def test_c1_21_needs_review_is_immediate_not_after_max_attempts() -> None:
    """🔴 「转人工」= **一轮就到**,不是重试 8 轮之后。

    用 ``_Skip`` 处置这一类的话,``_record_error(terminal=False)`` 永远只写回
    ``pending``,而 ``_claim_pending`` 在 attempt 撞上 MAX_ATTEMPTS 之后
    不再选中它 —— 那是**永久搁置**,不是转人工:运维面上看不见,也没人再管。
    所以这里断言的是 ``attempt_count == 1`` 时状态就已经是终态。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _confirmed_session(_seed.TENANT_A)
    _insert_legacy_outbox(ctx, tenant=_seed.TENANT_A)
    _mat.materialize_pending()

    row = _seed.outbox_rows(ctx["accepted"])[0]
    assert int(row["attempt_count"]) == 1, row
    assert row["status"] == "needs_review", row


def test_c1_22_hand_edited_policy_is_refused() -> None:
    """冻结值被人手改成一个判别位不认识的策略 ⇒ 转人工,不签预算。

    052 的 CHECK 只保证三列同生同死,不保证取值合法 ——
    这一格由应用层的闭集校验守,分母取自 ``payer_classification``。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _confirmed_session(_seed.TENANT_A)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO defgeo_activation_outbox "
            "(accepted_snapshot_id, quote_id, brand_id, event_kind, "
            " accepted_snapshot_hash, status, tenant_owner_id, "
            " payer_user_id, payer_funding_policy, payer_principal_kind) "
            "VALUES (%s,%s,%s,'commercial_basis_established',%s,'pending',%s,%s,"
            " 'sponsor_platform_ledger','personal')",
            (ctx["accepted"], ctx["quote"], ctx["brand"], ctx["hash"],
             _seed.TENANT_A, _seed.TENANT_A))
        conn.commit()
    finally:
        conn.close()

    out = _mat.materialize_pending()
    assert out["materialized"] == 0, out
    assert out["needsReview"] >= 1, out
    # 🔴 [撕锁订正] 断言必须打到**库里那一行的状态**,不能只看返回计数。
    #    ``needsReview`` 计数在 ``_NeedsHuman`` 分支里无条件 +1,
    #    它对"回写用的是 _record_needs_review 还是 _record_error"**零区分力**
    #    —— MUT-C1-03(把转人工换回 _record_error)在只看计数时活了下来。
    #    真正要守的是:这一条**现在**就在运维面上,而不是 8 轮之后。
    rows = _seed.outbox_rows(ctx["accepted"])
    assert rows and rows[0]["status"] == "needs_review", rows
    assert int(rows[0]["attempt_count"]) == 1, rows


#: 🔴 [外选 EXTC-01] 策略 → 应有 principal_kind 的**机械**映射。
#:    分母取 ``payer_classification.census()["cells"]`` —— 判别位那一侧的
#:    自报表,不是这里手抄的一份。判别位加一格(比如 organization_budget
#:    的腿接通)时,下面那张错配矩阵自动跟着长出新样本。
def _policy_kind_matrix() -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    from services.defensive_geo import payer_classification as _p

    expected = {str(cell["funding_policy"]): str(cell["principal_kind"])
                for cell in _p.census()["cells"].values()}
    kinds = sorted(set(expected.values()))
    mismatches = [(pol, kind, expected[pol])
                  for pol in _p.CLASSIFIABLE_POLICIES
                  for kind in kinds if kind != expected[pol]]
    return expected, mismatches


_EXPECTED_KIND, _POLICY_KIND_MISMATCHES = _policy_kind_matrix()


def _insert_frozen_outbox(ctx: dict, *, policy: str, kind: str,
                          tenant: int = _seed.TENANT_A) -> None:
    """三列**全非空**(过得了 052 的 CHECK)、但取值互相矛盾的一行。"""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO defgeo_activation_outbox "
            "(accepted_snapshot_id, quote_id, brand_id, event_kind, "
            " accepted_snapshot_hash, status, tenant_owner_id, "
            " payer_user_id, payer_funding_policy, payer_principal_kind) "
            "VALUES (%s,%s,%s,'commercial_basis_established',%s,'pending',%s,%s,%s,%s)",
            (ctx["accepted"], ctx["quote"], ctx["brand"], ctx["hash"],
             tenant, tenant, policy, kind))
        conn.commit()
    finally:
        conn.close()


def test_c1_23_policy_kind_denominator_is_taken_from_the_classifier() -> None:
    """分母自证:错配矩阵真的是从判别位算出来的,而且**非空**。

    没有这一条,下面那条参数化可能因为矩阵是空列表而一个样本都不跑 ——
    「零样本」与「全通过」在 pytest 的计数上分不开
    (本仓记过:分母漏了一块与分母里没有违规项长得一模一样)。
    """
    from services.defensive_geo import payer_classification as _p

    assert set(_EXPECTED_KIND) == set(_p.CLASSIFIABLE_POLICIES), (
        f"census 自报的策略集与闭集对不上:{sorted(_EXPECTED_KIND)} "
        f"vs {sorted(_p.CLASSIFIABLE_POLICIES)}")
    # 每个策略各有 (kinds-1) 个错配 ⇒ 2 策略 × 2 kind − 2 对角 = 2
    assert len(_POLICY_KIND_MISMATCHES) == len(_EXPECTED_KIND) * (
        len(set(_EXPECTED_KIND.values())) - 1), _POLICY_KIND_MISMATCHES
    assert _POLICY_KIND_MISMATCHES, "错配矩阵是空的 —— 参数化会一个样本都不跑"


@pytest.mark.parametrize(("policy", "kind", "should_be"), _POLICY_KIND_MISMATCHES)
def test_c1_24_frozen_kind_that_contradicts_the_policy_is_refused(
        policy, kind, should_be) -> None:                 # noqa: ANN001
    """🔴 [外选 EXTC-01] 策略合法、但 ``principal_kind`` 与它对不上 ⇒ 转人工。

    ``frozen_identity`` 里有**两道**守卫,而改动前只有第一道有夹具
    (``test_c1_22`` 造的 ``sponsor_platform_ledger`` 不在闭集,走第一道就被拦):

      ① 策略不在 ``CLASSIFIABLE_POLICIES`` 闭集里;
      ② 策略合法,但 ``payer_principal_kind`` 与该策略应有的 kind 不符。

    ② 零样本的代价很具体:被人手改成
    「``personal_wallet`` + ``platform_cost_center``」这种组合的行会照常物化,
    ``derive(funding_policy="personal_wallet", ...)`` 签出预算 ——
    付款腿与主体类别脱钩,平台成本中心的单被签在个人钱包语义上(或反向)。

    拆红:把 ② 那道 ``if`` 短路掉(``if False and ...``)⇒ 本条红。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _confirmed_session(_seed.TENANT_A)
    _insert_frozen_outbox(ctx, policy=policy, kind=kind)

    out = _mat.materialize_pending()
    rows = _seed.outbox_rows(ctx["accepted"])
    assert rows, "夹具那一行不见了"
    assert rows[0]["status"] == "needs_review", (
        f"policy={policy!r} 配 kind={kind!r}(应为 {should_be!r})居然被物化了:{rows[0]}")
    assert int(rows[0]["attempt_count"]) == 1, rows[0]
    assert "principal_kind" in str(rows[0]["last_error"] or ""), (
        f"转人工了,但理由不是 kind 对不上:{rows[0]['last_error']}")
    assert out["needsReview"] >= 1, out

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM defgeo_provider_execution_budgets "
            " WHERE accepted_snapshot_id=%s", (ctx["accepted"],))
        assert int(cur.fetchone()["n"]) == 0, "矛盾身份的那一条居然签出了预算"
        conn.rollback()
    finally:
        conn.close()


def test_c1_25_the_matching_pair_still_materializes() -> None:
    """判别力反臂:**对得上**的那一对必须照常物化。

    只验"错配被拦"的话,一个"什么都拦"的实现同样全绿 ——
    而那种实现会把每一单正常的激活都推进转人工队列。
    """
    from services.defensive_geo import activation_materializer as _mat

    ctx = _confirmed_session(_seed.TENANT_A)
    policy = "personal_wallet"
    _insert_frozen_outbox(ctx, policy=policy, kind=_EXPECTED_KIND[policy])

    _mat.materialize_pending()
    rows = _seed.outbox_rows(ctx["accepted"])
    assert rows and rows[0]["status"] == "materialized", (
        f"对得上的那一对没被物化:{rows}")


def test_c1_30_migration_052_forbids_half_frozen_identity() -> None:
    """库层承重:三个 payer 列**同生同死**。只写一半必须被 CHECK 打回。

    应用层的检查是纵深;承重在 052 的
    ``defgeo_activation_outbox_payer_group`` 上。两道分别拆验。
    """
    import psycopg2

    ctx = _confirmed_session(_seed.TENANT_A)
    conn = connect()
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO defgeo_activation_outbox "
                "(accepted_snapshot_id, quote_id, brand_id, event_kind, "
                " accepted_snapshot_hash, status, payer_user_id) "
                "VALUES (%s,%s,%s,'commercial_basis_established',%s,'pending',%s)",
                (ctx["accepted"], ctx["quote"], ctx["brand"], ctx["hash"],
                 _seed.TENANT_A))
        conn.rollback()
    finally:
        conn.close()


def test_c1_31_frozen_identity_columns_denominator_is_not_hand_written() -> None:
    """分母自证:``FROZEN_IDENTITY_COLUMNS`` 里的每一列都**真的在库里**。

    分母里写一个不存在的列名,那一格的检查恒不命中(``row.get`` 返 None
    ⇒ 永远"缺" ⇒ 全部转人工),或者反过来漏一列就恒不检查。
    两个方向都要靠这条把分母钉在真 schema 上。
    """
    from services.defensive_geo import activation_materializer as _mat

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_schema='public' AND table_name='defgeo_activation_outbox'")
        cols = {r["column_name"] for r in cur.fetchall()}
        conn.rollback()
    finally:
        conn.close()
    missing = [c for c in _mat.FROZEN_IDENTITY_COLUMNS if c not in cols]
    assert not missing, f"冻结身份分母里有库里不存在的列:{missing}"
    assert len(_mat.FROZEN_IDENTITY_COLUMNS) == 4, _mat.FROZEN_IDENTITY_COLUMNS


# ══════════════════════════════════════════════════════════════════════════
# [外选 EXTC-02] CHECK 的「同生同死」要**两个方向**都有样本
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 分母从哪里来:``FROZEN_IDENTITY_COLUMNS`` 里 ``payer_`` 前缀那几列。
#    刻意**不**从 ``pg_get_constraintdef`` 反解 —— 那是「用被测对象自己
#    当分母」:CHECK 被弱化成两列时,反解出来的分母也跟着变成两列,
#    枚举出的样本自动避开被删掉的那一格,判据恒绿。
#    (``tenant_owner_id`` 不在这条 CHECK 里 —— 它是 043 的老列,
#     052 头部逐字写明刻意不并进来。)
PAYER_GROUP_COLUMNS: tuple[str, ...] = tuple(
    c for c in ("tenant_owner_id", "payer_user_id",
                "payer_funding_policy", "payer_principal_kind")
    if c.startswith("payer_"))

#: 每列一个合法取值。键集与上面那个分母**逐格对账**(见 test_c1_32)。
_PAYER_VALUES: dict[str, object] = {
    "payer_user_id": _seed.TENANT_A,
    "payer_funding_policy": "personal_wallet",
    "payer_principal_kind": "personal",
}


def _partial_payer_subsets() -> list[tuple[str, ...]]:
    """三列里的每一个**真非空子集**(1 列 / 2 列)。机械枚举,不手抄。"""
    import itertools

    cols = list(PAYER_GROUP_COLUMNS)
    return [tuple(c) for n in range(1, len(cols))
            for c in itertools.combinations(cols, n)]


_PARTIAL_SUBSETS = _partial_payer_subsets()


def _insert_with(ctx: dict, columns: tuple[str, ...]):
    """只写 ``columns`` 里那几列的 payer 值,其余留 NULL。"""
    cols = ["accepted_snapshot_id", "quote_id", "brand_id", "event_kind",
            "accepted_snapshot_hash", "status", *columns]
    vals = [ctx["accepted"], ctx["quote"], ctx["brand"],
            "commercial_basis_established", ctx["hash"], "pending",
            *[_PAYER_VALUES[c] for c in columns]]
    return ("INSERT INTO defgeo_activation_outbox (" + ",".join(cols) + ") VALUES ("
            + ",".join(["%s"] * len(cols)) + ")", vals)


def test_c1_32_partial_subset_denominator_is_mechanical_and_complete() -> None:
    """分母自证:子集枚举既没漏、也没把被测对象当分母。

    ``2**3 - 2 = 6`` 个真非空子集。数不对说明分母的取法变了 ——
    那时下面那条参数化守的已经不是「三列同生同死」这条轴。
    """
    assert set(_PAYER_VALUES) == set(PAYER_GROUP_COLUMNS), (
        f"取值表与分母对不上:{sorted(_PAYER_VALUES)} vs {sorted(PAYER_GROUP_COLUMNS)}")
    assert len(PAYER_GROUP_COLUMNS) == 3, PAYER_GROUP_COLUMNS
    assert len(_PARTIAL_SUBSETS) == 2 ** len(PAYER_GROUP_COLUMNS) - 2, _PARTIAL_SUBSETS
    # 两列那一档必须真的在里面 —— 它正是改动前**零夹具**的那个方向。
    assert any(len(s) == 2 for s in _PARTIAL_SUBSETS), _PARTIAL_SUBSETS


@pytest.mark.parametrize("columns", _PARTIAL_SUBSETS,
                         ids=["+".join(c) for c in _PARTIAL_SUBSETS])
def test_c1_33_every_partial_payer_identity_is_refused_by_the_db(columns) -> None:  # noqa: ANN001
    """🔴 [外选 EXTC-02] 三列里**只写一部分**,库层一律打回。

    ``test_c1_30`` 只插了**一列**(``payer_user_id``)。把 CHECK 的第二支
    弱化成「只看两列」之后,那一条仍然因为第三列为空而 CheckViolation ⇒ 照样绿:
    「同生同死」只在 1/3 缺的方向上有样本,**2/3 在**的方向零样本。
    半冻结身份(冻了 user_id + policy、没冻 principal_kind)会重新变得可入库,
    而 052 病历里「库层不可能出现只冻了一半」这句话当场变假。

    拆红:把 052 CHECK 第二支里任何一列的 ``IS NOT NULL`` 摘掉 ⇒ 对应子集红。
    """
    import psycopg2

    ctx = _confirmed_session(_seed.TENANT_A)
    sql, vals = _insert_with(ctx, columns)
    conn = connect()
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(sql, vals)
        conn.rollback()
    finally:
        conn.close()


def test_c1_34_the_complete_triple_is_still_insertable() -> None:
    """判别力反臂:三列**全写**必须能进去。

    没有这一条,上面六个子集全红有可能只是因为这条 CHECK 恒假
    (比如写成 ``CHECK (false)``)—— 那时每一次客户确认都会被库层打回,
    比半冻结身份严重得多,而参数化那六条一条都不会红。
    """
    ctx = _confirmed_session(_seed.TENANT_A)
    sql, vals = _insert_with(ctx, PAYER_GROUP_COLUMNS)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, vals)                            # 不抛 = 通过
        cur.execute("SELECT COUNT(*) AS n FROM defgeo_activation_outbox "
                    " WHERE accepted_snapshot_id=%s", (ctx["accepted"],))
        assert int(cur.fetchone()["n"]) == 1
        conn.rollback()                                   # 不留行给后面的判据
    finally:
        conn.close()


def test_c1_35_the_check_definition_binds_all_three_columns_in_both_directions() -> None:
    """结构纵深:CHECK 的**定义文本**里三列的两个方向都在。

    行为判据(上面那六个子集)与这一条互为纵深,而且这一条守的是
    行为判据够不到的一格:``ADD CONSTRAINT`` 包在 ``IF NOT EXISTS``(按**名字**
    幂等)里,所以库上留着一条**弱化版同名约束**时,重跑 052 会静默跳过、
    052 自己的反向自证也只查存在性 —— 光看"约束在不在"永远发现不了。

    🔴 实测:2026-08-26 接手时判据库上那条 CHECK 的第二支只剩两列
       (外选 EXTC-02 跑完没还原干净),而当时全包 76/76 全绿。
    """
    import re

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
            " WHERE conname='defgeo_activation_outbox_payer_group'")
        row = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()
    assert row is not None, "payer 同生同死那条 CHECK 不在库上"
    defn = str(row["d"])
    for col in PAYER_GROUP_COLUMNS:
        assert re.search(rf"\b{col}\s+IS\s+NULL\b", defn), (
            f"CHECK 定义里没有 {col} 的「全空」那一支:{defn}")
        assert re.search(rf"\b{col}\s+IS\s+NOT\s+NULL\b", defn), (
            f"CHECK 定义里没有 {col} 的「全满」那一支 —— 半冻结身份可表达了:{defn}")
    # 探针活性:正则真的能判假(否则上面两条对任何文本都成立)
    assert not re.search(r"\bpayer_user_id\s+IS\s+NOT\s+NULL\b",
                         "CHECK ((payer_user_id IS NULL))"), "正则写错了"
