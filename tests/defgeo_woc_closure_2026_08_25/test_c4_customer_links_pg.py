"""C-4 · 客户链接面板:错对象 + 整体吞异常 + 过期不判 + 重签换错对象。

Codex 终审 P1-11。四件事一起坏在一个函数里,所以判据也按四组拆:

  A. **对象语义**:「报价单·请确认」指向选词确认会话 ``/s/{token}``,
     不是那张连 ``brand_id`` 列都没有的 ``agent_quotes``;
  B. **不吞异常**:查询失败留痕报错,不许静默 ``not_ready``;
  C. **expires_at 参与状态判定**;
  D. **面板所示对象 == 重签对象**。
"""

from __future__ import annotations

import ast
import io

import pytest

from services.defensive_geo import customer_links as _links
from tests.defgeo_woc_closure_2026_08_25 import _seed
from tests.defgeo_woc_closure_2026_08_25.conftest import ROOT, connect


# ══════════════════════════════════════════════════════════════════════════
# [外选 EXTC-08] 过期豁免名单:分母从**现役实现**机械抽,不手抄第二份
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 本仓第三次记「枚举锁范围 ≠ 轴的作用域」。这一格的形状是:
#    ``customer_links._selection_expired`` 里那个 status 元组是现役
#    ``selection_api._check_expired`` 的**第二份手抄**。抄错/掉一档时,
#    客户已确认、正待付款的会话一过 expires_at 就会在面板上显示成
#    「expired · 可重签」,而 ``selection_api`` 那一侧它根本不算过期 ——
#    销售照着重签、催客户重走一遍。
#
#    所以判据不再写第三份名单:分母**从现役那一侧的 AST 抽**,
#    再逐档驱动被测函数。掉一档 ⇒ 那一档的样本当场红。
_LIVE_EXEMPT_FUNCS = ("_check_expired", "_check_expired_locked")


def _status_membership_sets(path, func_names) -> dict:              # noqa: ANN001
    """抽 ``session["status"] in (...)`` 那一处的字符串元组。

    只认「``in`` + 全字符串字面量容器」这一种形态 —— 认多了会把别的
    集合当成豁免名单,认少了会抽到空集合而恒真。两种都由下面那条自证挡住。
    """
    src = io.open(path, encoding="utf-8", newline="").read()
    tree = ast.parse(src)
    out: dict = {}
    for name in func_names:
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert node is not None, f"取不到 {name} —— 锚点已过期"
        found: list[frozenset] = []
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Compare) and len(sub.ops) == 1
                    and isinstance(sub.ops[0], ast.In)):
                continue
            box = sub.comparators[0]
            if not isinstance(box, (ast.Tuple, ast.List, ast.Set)):
                continue
            elts = box.elts
            if elts and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                            for e in elts):
                found.append(frozenset(str(e.value) for e in elts))
        assert len(found) == 1, (
            f"{name} 里的字符串 in-集合不是恰好一处(实得 {len(found)}:{found})—— "
            "抽取口径要重新设计,别猜")
        out[name] = found[0]
    return out


_LIVE_EXEMPT = _status_membership_sets(
    ROOT / "api" / "selection_api.py", _LIVE_EXEMPT_FUNCS)
#: 现役名单里 ``expired`` 是「已经是过期态、别再改一次」,不是豁免;
#: ``customer_links`` 那一侧把它单独处置(返 True)。所以分母减掉它。
_LIVE_NOT_EXPIRED = sorted(_LIVE_EXEMPT[_LIVE_EXEMPT_FUNCS[0]] - {"expired"})


@pytest.fixture(scope="module", autouse=True)
def _identities():
    _seed.install_identities()


def _tok(tag: str) -> str:
    """跨 session 唯一的 token。

    🔴 ``keyword_selection_sessions.token`` 上有唯一索引,而一次性库会被
       **反复复用**(conftest 的 _clean 刻意不删会话表 —— 那是既有表,
       不是本包写的)。写死字面量的话第二次跑就撞唯一约束,
       而那种红与被测代码毫无关系(实测:单跑全绿、合跑四条红)。
       派生自库里现有最大 id,单调递增、可复现。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(id),0)+1 AS n FROM keyword_selection_sessions")
        n = int(cur.fetchone()["n"])
        conn.rollback()
    finally:
        conn.close()
    return f"WOC-{tag}-{n:06d}"


def _panel(brand_id: int) -> dict:
    return {e["kind"]: e for e in
            _links.build_panel(brand_id=brand_id, brand_name="工单C 判据品牌")}


# ══════════════════════════════════════════════════════════════════════════
# A. 对象语义
# ══════════════════════════════════════════════════════════════════════════
def test_c4_00_agent_quotes_really_has_no_brand_id_column() -> None:
    """先证明缺陷成因存在:``agent_quotes`` 里**没有** ``brand_id``。

    SQL 四维度核验第 1 条(列名肉眼核对真库)在判据里的形态。
    这一条是下面所有断言的前提 —— 前提垮了,那些断言就只是在测别的东西。
    """
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_schema='public' AND table_name='agent_quotes'")
        cols = {r["column_name"] for r in cur.fetchall()}
        conn.rollback()
    finally:
        conn.close()
    assert cols, "agent_quotes 表不在库里 —— 前提查不了"
    assert "brand_id" not in cols, (
        f"agent_quotes 现在有 brand_id 了(实得 {sorted(cols)})—— "
        "缺陷成因已变,本组判据要重新设计")


def test_c4_01_quote_proposal_points_at_the_selection_session() -> None:
    """🔴 「报价单·请确认」= ``/s/{token}``。

    那一页的 ``POST /s/{token}/confirm-quote`` 才是 accepted snapshot、
    activation outbox、整条商业基线的起点。``/q/:code`` 是白标**展示**页,
    客户在那一页没有"确认"这个动作 —— 把它当"请确认"发出去就是骗人。

    拆红:把查询改回 ``agent_quotes`` ⇒ UndefinedColumn ⇒ 本条红
    (而且现在是**红**,不是静默 not_ready)。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    token = _tok("SEL")
    _seed.new_selection_session(brand_id=brand, quote_id=quote, token=token)

    entry = _panel(brand)["quote_proposal"]
    assert entry["status"] == "available", entry
    assert entry["url"] == f"/s/{token}", entry
    assert entry["objectRef"] == {"kind": "keyword_selection_session",
                                  "token": token}, entry
    assert entry["kindLabel"] == "报价单·请确认", entry


def test_c4_02_portal_is_no_longer_blocked_by_the_quote_query() -> None:
    """🔴 门户那一类**也**被那条坏查询连累过 —— 顺序害的。

    坏查询排在门户查询**之前**,一抛就跳到 except,门户那一段
    从上线起就没跑过。所以"报价能用了"证明不了"门户也能用了",
    必须单独断言同一次调用里门户也拿到了链接。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    _seed.new_selection_session(brand_id=brand, quote_id=quote, token=_tok("SEL"))
    _seed.new_client_access_token(quote_id=quote, token=_tok("POK")[:32],
                                  expires_at="2099-01-01")

    panel = _panel(brand)
    assert panel["quote_proposal"]["status"] == "available", panel["quote_proposal"]
    assert panel["customer_portal"]["status"] == "available", panel["customer_portal"]
    assert panel["customer_portal"]["url"].startswith("/portal/WOC-POK-")


# ══════════════════════════════════════════════════════════════════════════
# B. 不吞异常
# ══════════════════════════════════════════════════════════════════════════
def test_c4_10_query_failure_raises_instead_of_silently_not_ready(monkeypatch) -> None:
    """查询失败 ⇒ 抛 ``CustomerLinksUnavailable``,不再回一组 ``not_ready``。

    「这一步还没做到」与「我们的查询写错了」必须在屏幕上分得开。
    改动前它们长得一模一样,而实际发生的正是后者。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)

    import db.diagnosis_db as _ddb

    class _Boom:
        def cursor(self):                                  # noqa: ANN001
            raise RuntimeError("模拟查询失败")

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(_ddb, "get_connection", lambda: _Boom())
    with pytest.raises(_links.CustomerLinksUnavailable):
        _links.build_panel(brand_id=brand, brand_name="工单C")


def test_c4_11_census_declares_it_does_not_swallow() -> None:
    """可机读断言 —— 让"吞不吞"这件事有一个不靠读注释的答案。"""
    c = _links.census()
    assert c["swallowsQueryErrors"] is False, c
    assert c["quoteProposalObject"] == "keyword_selection_sessions", c


# ══════════════════════════════════════════════════════════════════════════
# C. expires_at 参与状态判定
# ══════════════════════════════════════════════════════════════════════════
def test_c4_20_expired_portal_token_is_not_available() -> None:
    """``is_active=1`` 但 ``expires_at`` 已过 ⇒ **expired**,不是「可以发了」。

    改动前只看 is_active:她把一个失效链接发给客户,客户点开是空白页。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    _seed.new_client_access_token(quote_id=quote, token=_tok("PEXP")[:32],
                                  is_active=1, expires_at="2020-01-01")

    entry = _panel(brand)["customer_portal"]
    assert entry["status"] == "expired", entry
    assert entry["url"] is None, entry
    assert entry["reissuable"] is True, entry


def test_c4_21_revoked_and_expired_are_different_statuses() -> None:
    """撤销 ≠ 过期。两者下一步都是重签,但**说法**不同,不许压成一档。"""
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    _seed.new_client_access_token(quote_id=quote, token=_tok("PREV")[:32],
                                  is_active=0, expires_at="2099-01-01")
    assert _panel(brand)["customer_portal"]["status"] == "revoked"


def test_c4_22_expired_selection_session_is_expired_not_not_ready() -> None:
    """选词会话过期 ⇒ ``expired``。

    ``keyword_selection_sessions.expires_at`` 是 **TEXT**,解析口径必须与现役
    ``selection_api._check_expired`` 同源 —— 另写一套在格式边角上必然分叉。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    _seed.new_selection_session(brand_id=brand, quote_id=quote, token=_tok("SEXP"),
                                expires_at="2020-01-01T00:00:00")
    entry = _panel(brand)["quote_proposal"]
    assert entry["status"] == "expired", entry
    assert entry["url"] is None, entry


def test_c4_23_confirmed_session_is_not_treated_as_expired() -> None:
    """已确认的会话**不再算过期** —— 与现役同一条豁免。

    没有这条反向对照,上一条的"过期"可能只是因为把所有会话都判成过期了。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    token = _tok("SCFM")
    _seed.new_selection_session(brand_id=brand, quote_id=quote, token=token,
                                status="confirmed", expires_at="2020-01-01T00:00:00")
    entry = _panel(brand)["quote_proposal"]
    assert entry["status"] == "available", entry
    assert entry["url"] == f"/s/{token}", entry


def test_c4_24_the_exemption_denominator_really_came_from_the_live_side() -> None:
    """分母自证 + 现役两处**自己先对账**。

    三件事:
      · 抽出来的名单非空、量级对(空集合会让下面那条参数化恒真);
      · ``_check_expired`` 与 ``_check_expired_locked`` 是现役的**两份手抄**,
        它们自己就该一模一样 —— 分叉的那一格同样没有任何判据会红;
      · ``expired`` 确实在现役名单里(减它是有依据的,不是我顺手挑的)。
    """
    a, b = (_LIVE_EXEMPT[n] for n in _LIVE_EXEMPT_FUNCS)
    assert a == b, (
        f"现役两处豁免名单已经分叉:{sorted(a)} vs {sorted(b)} —— "
        "同一谓词写两处,必有一处没人验")
    assert "expired" in a, sorted(a)
    assert len(_LIVE_NOT_EXPIRED) >= 5, _LIVE_NOT_EXPIRED
    assert "confirmed" in _LIVE_NOT_EXPIRED, _LIVE_NOT_EXPIRED


@pytest.mark.parametrize("status", _LIVE_NOT_EXPIRED)
def test_c4_25_every_live_exempt_status_survives_a_past_expiry(status) -> None:  # noqa: ANN001
    """🔴 [外选 EXTC-08] 现役豁免名单里的**每一档**都不算过期。

    改动前只有 ``confirmed`` 一档有夹具(``test_c4_23``),其余四档零样本:
    从 ``customer_links._selection_expired`` 的元组里删掉任意一档
    (比如 ``pending_payment``)不会让任何判据变红。

    这里两层一起验:
      · ``_selection_expired`` 这个谓词本身(单元级);
      · ``build_panel`` 出口(端到端 —— 谓词对了但面板不消费同样是坏的)。

    拆红:从 ``_selection_expired`` 的 status 元组里删掉任意一档 ⇒ 那一档红。
    """
    past = "2020-01-01T00:00:00"
    assert _links._selection_expired(past, status) is False, (
        f"{status!r} 被判成过期了 —— 与现役 selection_api 的豁免名单对不上")

    brand = _seed.new_brand(owner=_seed.TENANT_A)
    quote = _seed.new_quote(brand_id=brand)
    token = _tok("SX")
    _seed.new_selection_session(brand_id=brand, quote_id=quote, token=token,
                                status=status, expires_at=past)
    entry = _panel(brand)["quote_proposal"]
    assert entry["status"] == "available", (
        f"status={status!r} 的会话在面板上显示成 {entry['status']!r} —— "
        "销售会去重签一个客户手上还能用的链接")
    assert entry["url"] == f"/s/{token}", entry


def test_c4_26_a_status_outside_the_exemption_list_does_expire() -> None:
    """判别力反臂:名单**之外**的状态过了期就是过期。

    没有这一条,上面那一整组参数化可能只是因为
    ``_selection_expired`` 恒返 False(那时"过期"这一档在面板上永不出现,
    她永远看不到需要重签的链接)。
    """
    outside = "quoted"
    assert outside not in _LIVE_EXEMPT[_LIVE_EXEMPT_FUNCS[0]], (
        "样本状态被加进豁免名单了 —— 这条反臂失去判别力,要换一个")
    assert _links._selection_expired("2020-01-01T00:00:00", outside) is True
    assert _links._selection_expired("2020-01-01T00:00:00", "expired") is True


def test_c4_27_portal_expiry_boundary_is_pinned_relative_to_today() -> None:
    """🔴 [外选 EXTC-09] 门户到期的**边界日**:当天到期 = 还没过。

    ``client_access_tokens.expires_at`` 是 **DATE**,被测代码按
    ``exp < date.today()`` 判,注释逐字写着「当天到期的算还没过」。
    改动前的夹具只有 2020-01-01 与 2099-01-01 两端,**边界日零样本** ——
    把 ``<`` 写成 ``<=``(当天到期算过期)不会让任何判据变红,
    而后果是客户手上今天还能用的链接被销售当成失效的作废掉。

    🔴 判据里**不写死任何日期**:三条行都相对 ``date.today()`` 现造
       (本仓铁律「判据里不许有今天」针对的是**写死 cutoff** 配上
        吃列默认 NOW 的夹具 —— 那会让判据过一天就永久红;
        这里夹具与期望取自**同一次** ``date.today()``,不存在漂移)。
       唯一的漂移风险是跑到一半跨了午夜 —— 下面显式检测并重跑,
       不靠"概率很小"糊弄过去。
    """
    from datetime import date, timedelta

    cases = {"yesterday": (-1, "expired"), "today": (0, "available"),
             "tomorrow": (+1, "available")}

    for attempt in range(3):
        today = date.today()
        brand = _seed.new_brand(owner=_seed.TENANT_A)
        got: dict[str, str] = {}
        for name, (delta, _expected) in cases.items():
            quote = _seed.new_quote(brand_id=brand)
            # 🔴 token 唯一:``_tok`` 派生自会话表的 MAX(id),本条一条会话都不建
            #    ⇒ 三档拿到的是**同一个** token。补上档名 + 轮次才真唯一
            #    (一次性库跨 session 复用,撞唯一约束的红与被测代码无关)。
            _seed.new_client_access_token(
                quote_id=quote, token=f"{_tok('PB')}-{attempt}{name}"[:32],
                is_active=1,
                expires_at=(today + timedelta(days=delta)).isoformat())
            # 面板只看 canonical quote(最新那一份)⇒ 一 quote 一 token,逐档单独看
            got[name] = _panel(brand)["customer_portal"]["status"]
        if date.today() == today:
            break                                        # 没跨午夜,这一轮的观测有效
    else:                                                # pragma: no cover - 极罕见
        raise AssertionError("连续三轮都跨了午夜 —— 观测不可用,不按通过记")

    for name, (_delta, expected) in cases.items():
        assert got[name] == expected, (
            f"{name}(相对 {today} 偏 {cases[name][0]} 天)判成 {got[name]!r},"
            f"应为 {expected!r} —— 边界语义反了")
    # 探针活性:三档**不是**同一个答案(否则上面三条可能被一个常量满足)
    assert len(set(got.values())) == 2, got


# ══════════════════════════════════════════════════════════════════════════
# D. 面板所示对象 == 重签对象
# ══════════════════════════════════════════════════════════════════════════
def test_c4_30_panel_and_reissue_agree_on_the_object_in_a_multi_quote_brand() -> None:
    """🔴 多 quote 场景:面板显示的那个 quote **就是**重签动的那个。

    改动前面板按 ``client_access_tokens.id DESC`` 反查 quote、重签按
    ``quotes.created_at DESC`` 正查 —— 两条独立排序可以落在不同 quote 上。
    她看着 A 的过期链接点"重新签发",系统把 B 的门户 token 换掉了:
    A 仍然是坏的,而客户手上 B 那个好好的链接刚被作废。

    拆红:把 ``reissue_link`` 里的 ``canonical_quote_id`` 换回自己的
    ``ORDER BY created_at DESC LIMIT 1`` ⇒ 两边对象不同 ⇒ 本条红。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    old_quote = _seed.new_quote(brand_id=brand, created_at="2026-08-01 10:00:00")
    new_quote = _seed.new_quote(brand_id=brand, created_at="2026-08-20 10:00:00")
    # 🔴 token 只挂在**老** quote 上,且 id 更大 —— 这正是两条排序会分叉的形状:
    #    按 token.id DESC 反查会得到 old_quote,按 quotes.created_at DESC 得到 new_quote。
    old_token = _tok("POLD")[:32]
    _seed.new_client_access_token(quote_id=old_quote, token=old_token,
                                  is_active=0, expires_at="2099-01-01")

    entry = _panel(brand)["customer_portal"]
    assert entry["objectRef"] == {"kind": "quote", "id": new_quote}, (
        f"面板指向的不是 canonical quote:{entry['objectRef']} vs {new_quote}")

    # [工单 V3-A · Codex 三审 P1-8 · 2026-08-28] 重签现在必须带回**面板下发的那个对象**;
    # 本条要验的正是「面板与重签同一个对象」,所以把面板那一格原样传回去 ——
    # 这比原来"服务端自己再取一次最新"更贴近本条判据想说的话。
    out = _links.reissue_link(kind="customer_portal", brand_id=brand,
                              brand_name="工单C 判据品牌",
                              expected_object_ref=entry["objectRef"])
    assert out is not None
    assert out["objectRef"] == entry["objectRef"], (
        f"重签动的对象与面板显示的不是同一个:{out['objectRef']} vs {entry['objectRef']}")

    # 反向自证:老 quote 上那个 token **没被动过**(重签不许误伤别的对象)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT is_active FROM client_access_tokens WHERE token=%s",
                    (old_token,))
        assert int(cur.fetchone()["is_active"]) == 0, "重签动了另一个 quote 的 token"
        conn.rollback()
    finally:
        conn.close()


def test_c4_31_deleted_quotes_are_never_the_canonical_object() -> None:
    """软删掉的 quote 不该成为发给客户的对象。

    改动前面板那条 JOIN **没有** ``deleted_at IS NULL``、重签有 ——
    又一处两边口径不一致。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    live = _seed.new_quote(brand_id=brand, created_at="2026-08-01 10:00:00")
    dead = _seed.new_quote(brand_id=brand, created_at="2026-08-22 10:00:00")
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE quotes SET deleted_at=NOW() WHERE id=%s", (dead,))
        conn.commit()
    finally:
        conn.close()

    conn = connect()
    try:
        cur = conn.cursor()
        assert _links.canonical_quote_id(cur, brand) == live
        conn.rollback()
    finally:
        conn.close()


def test_c4_32_canonical_quote_is_deterministic_under_a_created_at_tie() -> None:
    """``created_at`` 并列时顺序仍然**确定**(补 ``id DESC``)。

    只按 created_at 排的话,"最新的那一份"在两次查询之间可以变 ——
    而面板与重签正是两次查询。
    """
    brand = _seed.new_brand(owner=_seed.TENANT_A)
    q1 = _seed.new_quote(brand_id=brand, created_at="2026-08-10 10:00:00")
    q2 = _seed.new_quote(brand_id=brand, created_at="2026-08-10 10:00:00")
    conn = connect()
    try:
        cur = conn.cursor()
        picks = {_links.canonical_quote_id(cur, brand) for _ in range(5)}
        conn.rollback()
    finally:
        conn.close()
    assert picks == {max(q1, q2)}, picks
