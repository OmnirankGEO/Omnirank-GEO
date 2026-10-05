# -*- coding: utf-8 -*-
"""行业归并共享服务 · 接线锁 + 四态判别 · 工单 `WO_INDUSTRY_KEY_RESOLVER_WIRING` v2

背景:`normalize_industry_key(自由文本)` 对别名表外的行业退化成 slugify,
两侧文字不同就永远对不上候选池,**不报错、只表现为"这个行业没有数据"**。
同一失败模式已在三条链上各自长出一种行为(见 `services/industry_canonical.py` 头注)。

本文件四层:
  ① 接线锁(AST · 打在**真实调用点**)—— 判据不能打在共享函数自己身上:
     那个函数对着自己的单测全绿、生产一次也不生效,本仓已犯三次。
  ② 禁写共享表的**成对反向** —— 正向"跑一单前后行数逐字不变",
     反向"这个入口被传成写模式必须失败"。只有正向 = 靠调用方自觉,守不住。
  ③ 四态两两不塌 —— 每一态都配一条"另一态在同样条件下**不**出现"的对照。
     四个态如果能互相顶替,断言换成任何一个值都恒绿(判别力的洞)。
  ④ 冻结件(包 B)真被消费 —— 反向:口径过期的冻结件必须被忽略而不是照用。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

# 复用主链那套 stub 夹具,不另造一份(两份夹具 = 两份"我以为的接线")。
from tests.test_geovid_ranking_mainchain_wiring_2026_08_06 import (  # noqa: F401
    _E2E_CLIENT, _E2E_IND, _run, _seed_industry, seeded_candidates, wired,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
ROUTER = REPO / "services" / "geo_douyin" / "ranking_router.py"
PROD = REPO / "services" / "geo_douyin" / "production_task.py"
API = REPO / "api" / "geo_douyin_api.py"
CANON = REPO / "services" / "industry_canonical.py"
BOARD = REPO / "services" / "media_effectiveness_board.py"

#: 一段**平台绝不认识**的行业原文。用于"未归并"与"不许硬塞"两组。
_UNKNOWN = "般若波罗蜜多多罗三藐三菩提"


#: 本文件会写入 / 可能被别的跑法写入的探针原文。见下面的 `_clean_probe_rows`。
_PROBE_ALIASES = (_UNKNOWN, "空库探针行业原文", "探针歧义甲行业与探针歧义乙行业的综合体")


@pytest.fixture(autouse=True)
def _clean_probe_rows():
    """🔴 每条测试前清掉探针别名 —— **幂等是硬要求**。

    实测教训(2026-08-08):跑了一轮"让只读入口偷偷写别名"的变异之后,代码还原了、
    **数据没还原**,`_UNKNOWN` 从此有了别名 → 三条锁在下一轮变成红,
    看着像"我的实现坏了",其实是上一轮的残留。变异 runner 会反复跑同一批锁,
    任何不幂等的前置都会表现成"基线红",整轮变异结论随之作废。
    """
    from db.connection import get_connection

    def _wipe():
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM geo_research_industry_aliases "
                        "WHERE normalized_alias = ANY(%s)", (list(_PROBE_ALIASES),))
            conn.commit()
        finally:
            conn.close()

    _wipe()
    yield
    _wipe()


def _fn(path: pathlib.Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _names_in(node: ast.AST) -> set[str]:
    out = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    out |= {a.name.split(".")[-1] for n in ast.walk(node)
            if isinstance(n, ast.ImportFrom) for a in n.names}
    return out


def _kwargs_of_call(path: pathlib.Path, callee: str) -> list[ast.Call]:
    """找到以 `callee` 为**被调目标**的调用(含 `to_thread(callee, ...)` 形态)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        direct = (isinstance(f, ast.Name) and f.id == callee) or \
                 (isinstance(f, ast.Attribute) and f.attr == callee)
        indirect = any(isinstance(a, ast.Name) and a.id == callee for a in n.args)
        if direct or indirect:
            out.append(n)
    return out


# ===========================================================================
# ① 接线锁 —— 打在真实调用点
# ===========================================================================

def test_orchestrator_merges_before_normalizing():
    """编排入口必须走**归并**,而不是裸归一。"""
    names = _names_in(_fn(ROUTER, "build_ranking_plan"))
    for sym in ("resolve_readonly", "with_inventory", "from_frozen"):
        assert sym in names, f"build_ranking_plan 没用到 {sym} —— 归并这一跳还是缺的"


def test_orchestrator_no_longer_calls_bare_normalize():
    """🔴 成对反向:编排入口**不许**再直呼 `normalize_industry_key`。

    留着它就等于归并可以被绕过 —— 而绕过之后一切照跑、不报错,
    正是本工单根因的形态。归一只允许发生在 `industry_canonical` 内部。
    """
    assert "normalize_industry_key" not in _names_in(_fn(ROUTER, "build_ranking_plan")), \
        "编排入口还在直呼 normalize_industry_key —— 归并可被绕过"


def test_paid_task_passes_the_frozen_industry_down():
    """🔴 判据打在**接线**上:付费任务要把冻结件真传给编排函数。

    只断"传了 frozen_industry=" 不够 —— `frozen_industry=None` 也含这个串,
    锁会被这种变异原样骗过(本仓已犯过一次)。按 AST 断言**值不是常量 None**。
    """
    calls = _kwargs_of_call(PROD, "build_ranking_plan")
    assert calls, "production_task 没有以 build_ranking_plan 为目标的调用"
    kw = [k for c in calls for k in c.keywords if k.arg == "frozen_industry"]
    assert kw, "编排调用漏了 frozen_industry= —— 冻结件传不下去,执行期会重新归并"
    assert not all(isinstance(k.value, ast.Constant) and k.value.value is None
                   for k in kw), "frozen_industry 被写死成 None = 接了等于没接"


def test_order_entry_freezes_before_dispatch():
    """下单口必须**先冻结再派单** —— 冻结晚于派单就等于没冻。"""
    src = API.read_text(encoding="utf-8")
    assert "freeze_industry" in _names_in(ast.parse(src)), "下单口没调 freeze_industry"
    calls = _kwargs_of_call(API, "dispatch_production")
    assert calls, "找不到 dispatch_production 调用"
    assert any(k.arg == "frozen_industry" for c in calls for k in c.keywords), \
        "派单没带 frozen_industry"


def test_redraw_inherits_the_frozen_industry():
    """再次创作要继承冻结件 —— 不继承就会"重做一次同行全变了"。"""
    calls = _kwargs_of_call(API, "dispatch_production")
    with_frozen = [c for c in calls
                   if any(k.arg == "frozen_industry" for k in c.keywords)]
    assert len(with_frozen) >= 2, \
        f"只有 {len(with_frozen)} 处派单带了 frozen_industry —— 首次制作与重做都要带"


def test_board_delegates_instead_of_keeping_a_copy():
    """发布中心那份本地实现要**收编**,不是再留一份副本。"""
    names = _names_in(_fn(BOARD, "_resolve_to_canonical"))
    assert "resolve_readonly" in names, "发布中心没委托给共享服务"
    assert "resolve_alias" not in names, \
        "发布中心还在自己查别名表 —— 第三份本地实现仍在"


# ===========================================================================
# ② 禁写共享表 · 成对反向
# ===========================================================================

def _table_counts():
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        out = {}
        for t in ("geo_research_industries", "geo_research_industry_aliases"):
            cur.execute(f"SELECT count(*) AS c FROM {t}")
            out[t] = int(cur.fetchone()["c"])
        return out
    finally:
        conn.close()


@pytest.mark.usefixtures("wired")
def test_paid_read_path_writes_nothing_and_burns_no_llm(wired, monkeypatch):
    """🔴 正向:跑一单未知行业的付费读路径 —— 两张共享表行数**逐字不变**,零 LLM。

    判据打在**榜单链真实调用点**(真跑 `run_image_post_production`),
    不是直调 `resolve_readonly` —— 后者只能证明那个函数干净,
    证不了付费链上没有别的地方在写。
    """
    import services.research_monitor.industry_resolver as resolver

    burned = []

    async def _tripwire(*a, **k):     # 读路径上**任何** LLM 归并都算越界
        burned.append(1)
        return {}
    monkeypatch.setattr(resolver, "resolve_or_create_industry", _tripwire,
                        raising=False)

    before = _table_counts()
    _run("ranking", industry_key=_UNKNOWN, brand_name="没有的客户")
    after = _table_counts()

    assert after == before, f"付费读路径写了共享表:{before} → {after}"
    assert not burned, "付费读路径调了 LLM 归并 —— 成本泄漏 + 可被打成 DoS"


def test_readonly_entry_cannot_be_talked_into_writing():
    """🔴 成对反向:这个入口**签名上就不接受写模式**。

    只有正向那条 = 靠"我们约定不传 persist=True"守着,
    而约定守不住(FIX1-01 之前那次就是这么漏的)。这里要它**传都传不了**。
    """
    from services.industry_canonical import resolve_readonly

    with pytest.raises(TypeError):
        resolve_readonly(_UNKNOWN, persist=True)       # type: ignore[call-arg]
    with pytest.raises(TypeError):
        resolve_readonly(_UNKNOWN, allow_llm=True)     # type: ignore[call-arg]


def test_readonly_impl_has_no_write_symbols():
    """只读实现里**不出现**任何写库符号(剥注释后判,注释里说明它为什么不写不算)。"""
    import io
    import tokenize

    src = CANON.read_text(encoding="utf-8")
    code = " ".join(t.string for t in
                    tokenize.generate_tokens(io.StringIO(src).readline)
                    if t.type not in (tokenize.COMMENT, tokenize.STRING))
    fn_names = _names_in(_fn(CANON, "resolve_readonly"))
    for bad in ("write_alias", "ensure_research_industry"):
        assert bad not in fn_names, f"resolve_readonly 里出现了写库符号 {bad}"
    assert "ensure_research_industry" not in code, \
        "本模块出现 ensure_research_industry —— 付费链结构上不许新建行业"


# ===========================================================================
# ③ 四态两两不塌
# ===========================================================================

def _plan(industry_key: str, **kw):
    from services.geo_douyin.ranking_router import build_ranking_plan
    return build_ranking_plan(industry_key=industry_key, keyword="深圳载货电梯哪家好",
                              client_brand=_E2E_CLIENT, card_budget=6, **kw)


def _alias_to_existing_industry(raw: str) -> str:
    """给 `raw` 沉淀一条指向**真实存在但没有候选**的行业的别名,返回该行业名。

    这是"归并成功 + 池子真没货"那一态的唯一造法 —— 没有它,
    `resolved_no_inventory` 与 `alias_missing` 就永远只能观察到一个,
    "两态没塌"这句话也就无从验证。
    """
    from db.connection import get_connection
    from db.research_selfserve_db import write_alias

    name = "空库行业探针"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO geo_research_industries (name, slug, active)
                       VALUES (%s, %s, TRUE)
                       ON CONFLICT (slug) DO UPDATE SET active = TRUE
                       RETURNING id""", (name, "ind_probe_empty"))
        iid = int(cur.fetchone()["id"])
        conn.commit()
    finally:
        conn.close()
    write_alias(raw, iid, 0.9, resolved_by="llm")
    return name


@pytest.mark.usefixtures("seeded_candidates")
def test_ready_when_pool_has_stock():
    """有货 → `resolved_ready`,不降级。"""
    out = _plan(_E2E_IND, force_ranking=True)
    assert out.industry["status"] == "resolved_ready"
    assert out.fallback_reason == ""


def test_unmerged_when_text_unrecognized_and_pool_empty():
    """未识别的写法 + 空池 → `alias_missing`,**不是**"这个行业没数据"。"""
    out = _plan(_UNKNOWN)
    assert out.industry["status"] == "alias_missing"
    assert out.fallback_reason == "industry_unmerged"


def test_no_inventory_when_merged_but_pool_empty():
    """🔴 与上一条**成对**:归并成功但池子真没货 → `resolved_no_inventory`。

    这两条必须同时成立,才能证明四态没有塌成一个 ——
    否则上一条把断言换成任何值都能绿。
    """
    raw = "空库探针行业原文"
    _alias_to_existing_industry(raw)
    out = _plan(raw)
    assert out.industry["status"] == "resolved_no_inventory", \
        f"归并成功却没判成覆盖缺口:{out.industry}"
    assert out.fallback_reason == "no_candidates"


def test_ambiguous_lists_the_colliding_industries():
    """一段同时像好几个行业的原文 → `ambiguous`,并给出候选让人选。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        for name, slug in (("探针歧义甲行业", "ind_amb_a"), ("探针歧义乙行业", "ind_amb_b")):
            cur.execute("""INSERT INTO geo_research_industries (name, slug, active)
                           VALUES (%s, %s, TRUE)
                           ON CONFLICT (slug) DO UPDATE SET active = TRUE""",
                        (name, slug))
        conn.commit()
    finally:
        conn.close()
    # 这段原文把两个行业名都**包含**在内 → 子串互含命中 2 家。
    out = _plan("探针歧义甲行业与探针歧义乙行业的综合体")
    assert out.industry["status"] == "ambiguous", out.industry
    assert len(out.industry.get("candidates") or []) >= 2
    assert out.fallback_reason == "industry_ambiguous"


def test_unknown_industry_is_never_forced_into_one():
    """🔴 不许硬塞:认不出来就认不出来,**不给它编一个行业**。

    归错行业比归不到严重得多 —— 家装客户拿到金融行业的同行名单,
    而且他不会察觉(榜单看起来完全正常)。
    """
    from services.industry_canonical import resolve_readonly

    ci = resolve_readonly(_UNKNOWN)
    assert ci.industry_id is None, f"未知行业被塞给了 industry_id={ci.industry_id}"
    assert ci.merged is False
    assert ci.canonical_name == _UNKNOWN, "未知行业的规范名被改写了"


def test_ambiguity_is_only_probed_when_the_pool_is_empty(monkeypatch):
    """🔴 反向:有货时**不**去查行业清单(歧义判定只在空池那一支)。

    否则发布中心每次渲染、榜单每条成功路径都白加一次查询。
    """
    import services.industry_canonical as ic

    probed = []
    monkeypatch.setattr(ic, "_active_industry_names",
                        lambda: probed.append(1) or [], raising=True)

    base = ic.resolve_readonly(_UNKNOWN)
    ic.with_inventory(base, 5)
    assert not probed, "池子有货却仍去查了行业清单"
    ic.with_inventory(base, 0)
    assert probed, "池子空了却没判歧义"


# ===========================================================================
# ④ fail-soft 与冻结件
# ===========================================================================

def test_resolve_is_fail_soft_when_db_is_down(monkeypatch):
    """别名表 / DB 挂了 → 原样透传,**不抛** —— 归并失败绝不让整单失败。"""
    import services.industry_canonical as ic

    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ic, "_fetch_one", _boom, raising=True)

    ci = ic.resolve_readonly("电梯维保")
    assert ci.canonical_name == "电梯维保"
    assert ci.resolved_by == "passthrough"


def test_frozen_payload_is_consumed_not_recomputed(monkeypatch):
    """🔴 冻结件真被消费:传了冻结件就**不再现场归并**。"""
    import services.industry_canonical as ic

    called = []
    monkeypatch.setattr(ic, "resolve_readonly",
                        lambda raw: called.append(raw) or ic.CanonicalIndustry(
                            raw=raw, canonical_name=raw, industry_key="不该用到"),
                        raising=True)
    _seed_industry(_E2E_IND)
    frozen = {"raw": "随便什么原文", "canonical_name": _E2E_IND,
              "industry_key": _E2E_IND, "industry_id": 1,
              "status": "resolved_ready", "resolved_by": "frozen",
              "mapping_version": ic.MAPPING_CONTRACT_VERSION}
    out = _plan("完全对不上的自由文本", force_ranking=True, frozen_industry=frozen)
    assert not called, "传了冻结件仍然现场归并了"
    assert out.plan is not None, "冻结键没被用来取候选"


def _fake_decision(name, conf, *, is_new=False, industry_id=1, decouple=False):
    """伪造 resolver 的 LLM 结果(不打网络)。

    🔴 `is_new` / `industry_id` **必须可参数化**:resolver 对 merge 与 new
       **都返回 `resolved_by="llm"`**,且 new 时 `industry_id` 恒 None
       (`preview_id = None if is_new else …`)。上一版 fixture 把两者写死成
       False/1,于是 5 条锁全在 merge 场景里打转,新行业那一支是**盲区**
       —— Review 增量裁定 ② 点名的正是这个组合。
    """
    async def _f(raw, *, allow_llm=True, persist=True):
        assert persist is False, "冻结点必须以 persist=False 调 resolver(结构上不许建行业)"
        # `decouple=True` 解开 is_new 与 industry_id 的天然耦合(现役 resolver 里
        # is_new ⟹ id 恒 None)。用途见 `test_is_new_is_read_not_inferred_from_id`。
        iid = industry_id if (decouple or not is_new) else None
        return {"industry_id": iid,
                "industry_name": name, "confidence": conf,
                "resolved_by": "llm", "is_new": is_new, "user_industry_raw": raw}
    return _f


def _run_freeze(monkeypatch, name, conf, raw="包车", *, is_new=False, industry_id=1,
                decouple=False):
    import asyncio

    import services.research_monitor.industry_resolver as R
    import services.industry_canonical as ic

    monkeypatch.setattr(R, "resolve_or_create_industry",
                        _fake_decision(name, conf, is_new=is_new, industry_id=industry_id,
                                       decouple=decouple),
                        raising=True)
    wrote = []
    import db.research_selfserve_db as sdb

    # 🔴 2026-08-09 返工:桩必须跟上 `write_alias` 的新契约 ——
    #    它现在是**仅首次写入 + 写后重读**,返回「库里最终生效的那一条」。
    #    上一版桩写的是 `lambda *a, **k: wrote.append(a)`,返回 **None**,
    #    而 None 在新实现里的语义是"映射没能建立,不采信" → 本文件的用例当场红。
    #    这条红是**对的**:桩返回 None 等于在断言一个真实现不会产生的状态。
    #    (成对的真库行为锁在 `test_industry_alias_admin_not_overwritten_2026_08_09.py`。)
    def _stub_write_alias(alias, iid, conf, resolved_by="llm", reviewed_by=None):
        wrote.append((alias, iid, conf, resolved_by))
        return {"industry_id": iid, "resolved_by": resolved_by,
                "reviewed_by": reviewed_by, "confidence": conf, "active": True}

    monkeypatch.setattr(sdb, "write_alias", _stub_write_alias, raising=True)
    return asyncio.run(ic.freeze_industry(raw)), wrote


def test_high_confidence_merge_is_taken_and_sedimented(monkeypatch):
    """高置信 → 采信归并,并沉淀别名(第二次起零 LLM,这是复利的来源)。"""
    import services.industry_canonical as ic

    ci, wrote = _run_freeze(monkeypatch, "汽车", 0.95)
    assert ci.status != ic.STATUS_AMBIGUOUS
    assert ci.canonical_name == "汽车"
    assert wrote, "高置信归并没有沉淀别名 —— 每单都要重烧一次 LLM"


def test_low_confidence_merge_is_refused_and_never_sedimented(monkeypatch):
    """🔴 与上一条**成对**:低置信 → 不采信、**更不许沉淀别名**。

    错别名一旦写进共享表就永远缓存命中,**再没有人会发现它是错的**。
    实测样本:`包车 → 旅游酒店`(0.6),而清单里明明有「汽车」。
    """
    import services.industry_canonical as ic

    ci, wrote = _run_freeze(monkeypatch, "旅游酒店", 0.6)
    assert ci.status == ic.STATUS_AMBIGUOUS, f"低置信被当成归并采信了:{ci}"
    assert ci.industry_id is None, "低置信却给了 industry_id = 实质上还是归并了"
    assert not wrote, "🔴 低置信把猜测沉淀成了别名 —— 这是不可逆的错误"
    assert ci.candidates == ("旅游酒店",), "该把 LLM 的建议带上,好让用户确认/否掉"


def test_confidence_threshold_is_the_thing_that_decides(monkeypatch):
    """🔴 判别力锁:换掉门槛,结论必须跟着变(否则断言换成任何值都恒绿)。"""
    import services.industry_canonical as ic

    ci, _ = _run_freeze(monkeypatch, "旅游酒店", 0.75)
    assert ci.status == ic.STATUS_AMBIGUOUS
    monkeypatch.setattr(ic, "MERGE_CONFIDENCE_MIN", 0.5, raising=True)
    ci2, _ = _run_freeze(monkeypatch, "旅游酒店", 0.75)
    assert ci2.status != ic.STATUS_AMBIGUOUS, \
        "把门槛降到 0.5 后 0.75 仍被拒 —— 说明拒它的不是门槛,判据没判别力"


def test_new_industry_low_confidence_is_never_offered_as_a_candidate(monkeypatch):
    """🔴🔴 Review 增量裁定 ②:`is_new=True` + 低置信 **不许落 ambiguous**。

    resolver 对 merge 与 new **都返回 `resolved_by="llm"`**,上一版闸没读 `is_new`,
    于是 LLM 判「这是个新行业」时,那个**平台根本不存在的名字**会被当候选推给用户确认。
    实测漏网样本:`test` → 新行业「测试」conf=0.1。
    正确去向 = `alias_missing`(我们没认出来),**不是**「我们倾向归到「测试」」。
    """
    import services.industry_canonical as ic

    ci, wrote = _run_freeze(monkeypatch, "测试", 0.1, raw="test", is_new=True)
    assert ci.status != ic.STATUS_AMBIGUOUS, f"新行业名被当候选推给用户了:{ci}"
    assert not ci.candidates, f"candidates 里出现了平台不存在的行业:{ci.candidates}"
    assert ci.industry_id is None and not wrote
    # 与只读结果一致 = 落穿到 alias_missing 那一支
    assert ic.with_inventory(ci, 0).status == ic.STATUS_ALIAS_MISSING


def test_new_industry_high_confidence_also_creates_nothing(monkeypatch):
    """成对:`is_new=True` 高置信同样**不建行业、不沉淀**(高置信不是建行业的通行证)。"""
    import services.industry_canonical as ic

    ci, wrote = _run_freeze(monkeypatch, "宠物行业", 0.95, raw="宠物店", is_new=True)
    assert ci.industry_id is None, "新行业被建出来了 —— 付费链结构上不许建行业"
    assert not wrote, "为不存在的行业沉淀了别名"
    assert ic.with_inventory(ci, 0).status == ic.STATUS_ALIAS_MISSING


def test_merge_to_unresolvable_target_is_not_offered_either(monkeypatch):
    """merge 但取不到 id(如目标行业已软删)+ 低置信 → 同样不拿去让用户确认。

    与上面两条同因:`candidates` 里只允许出现**真实存在的 active 行业**。
    """
    import services.industry_canonical as ic

    ci, wrote = _run_freeze(monkeypatch, "已软删行业", 0.6, is_new=False, industry_id=None)
    assert ci.status != ic.STATUS_AMBIGUOUS and not ci.candidates
    assert not wrote


def test_is_new_is_read_not_inferred_from_id(monkeypatch):
    """🔴🔴 **只翻 `is_new` 一个位**(id 保持有值),结论必须跟着翻。

    为什么要解开耦合:现役 resolver 里 `is_new=True` ⟹ `industry_id` 恒 None
    (`preview_id = None if is_new else …`),所以**光靠 `industry_id is not None`
    也能挡住新行业** —— 变异实测:摘掉 `is_new` 判定、只留 id 判定,29 条锁全绿。
    那说明上一版这条锁**名不副实**,它证明不了闸真的读了 `is_new`。

    本条用 `decouple=True` 造一个「is_new=True 但带 id」的将来态:
    resolver 若哪天改成「新行业也先建再返 id」,只靠 id 判定的闸会**fail-open**,
    把平台刚建出来的空行业推给用户当候选。显式读 `is_new` 才守得住。
    """
    import services.industry_canonical as ic

    merged, _ = _run_freeze(monkeypatch, "旅游酒店", 0.6, is_new=False)
    fresh, _ = _run_freeze(monkeypatch, "旅游酒店", 0.6, is_new=True, decouple=True)
    assert merged.status == ic.STATUS_AMBIGUOUS, "归并侧本该落 ambiguous"
    assert fresh.status != ic.STATUS_AMBIGUOUS, \
        "只翻 is_new(id 不变)结论没变 —— 闸没读这个信号,是靠 id 顺手挡的"
    assert not fresh.candidates


def test_ambiguous_verdict_survives_inventory_check():
    """freeze 定的歧义**不许被 with_inventory 改判**(空池时会降成 alias_missing)。"""
    import services.industry_canonical as ic

    ci = ic.CanonicalIndustry(raw="包车", canonical_name="包车", industry_key="包车",
                              status=ic.STATUS_AMBIGUOUS, candidates=("旅游酒店",))
    assert ic.with_inventory(ci, 0).status == ic.STATUS_AMBIGUOUS
    assert ic.with_inventory(ci, 0).candidates == ("旅游酒店",)


def test_single_candidate_notice_is_not_a_broken_sentence():
    """单个建议时要说「倾向归到 X 但没把握」,不能说「同时像好几个行业(X)」。"""
    from services.geo_douyin.ranking_router import _fallback_notice

    one = _fallback_notice("industry_ambiguous",
                           industry={"raw": "包车", "candidates": ["旅游酒店"]})["message"]
    many = _fallback_notice("industry_ambiguous",
                            industry={"raw": "甲乙", "candidates": ["甲行业", "乙行业"]})["message"]
    assert "同时像好几个行业" not in one, f"单候选被说成像好几个行业,是病句:{one}"
    assert "旅游酒店" in one and "没有把握" in one
    assert "同时像好几个行业" in many, "多候选反而不提像好几个行业了"


def test_stale_frozen_payload_is_ignored():
    """🔴 成对反向:口径过期的冻结件必须**被忽略**,回落现场归并。

    拿旧口径的键去查新池子 = 静默错配,正是本工单根因的老路。
    """
    from services.industry_canonical import MAPPING_CONTRACT_VERSION, from_frozen

    assert from_frozen({"industry_key": "x", "status": "resolved_ready",
                        "mapping_version": "industry-canonical-v0.0"}) is None
    # [WO_267 · v1.0 → v1.1 逐条改] 这一条测的是「状态不合法 ⇒ 忽略」,必须用**现役**口径版本 ——
    #   原来写死 v1.0,升版后它会因「版本不符」照样返回 None,状态那一支就再也没人测了(空转)。
    assert from_frozen({"industry_key": "x", "status": "编的态",
                        "mapping_version": MAPPING_CONTRACT_VERSION}) is None
    # [WO_267] 升版语义:v1.0 冻结件哪怕状态合法也被忽略 ⇒ 调用方现场重归并
    assert from_frozen({"industry_key": "x", "status": "resolved_ready",
                        "mapping_version": "industry-canonical-v1.0"}) is None
    # 对照臂:现役版本 + 合法状态 ⇒ 读得回来(证明上面三条的 None 不是 from_frozen 恒返 None)
    assert from_frozen({"industry_key": "x", "status": "resolved_ready",
                        "mapping_version": MAPPING_CONTRACT_VERSION}) is not None


# ===========================================================================
# ⑤ 文案不许说假话
# ===========================================================================

def test_unmerged_notice_does_not_claim_the_industry_has_no_data():
    """🔴 `industry_unmerged` 的话里**不许**断言"这个行业没有数据" —— 我们不知道。"""
    from services.geo_douyin.ranking_router import _fallback_notice

    nt = _fallback_notice("industry_unmerged",
                          industry={"raw": "AI搜索优化/GEO服务"}) or {}
    msg = nt["message"]
    assert "还没攒够" not in msg, "对未归并的行业断言了它没有数据 —— 那是假话"
    ids = {a["id"] for a in nt["actions"]}
    assert "confirm_industry" in ids, "没给「确认行业」这个真正能解决问题的出口"
    assert "supplement_candidates" not in ids, \
        "给了「去跑监测攒候选」—— 跑再多监测也对不上这个写法,是答非所问"


def test_no_inventory_notice_keeps_the_honest_sentence():
    """成对:`no_candidates` **保留**那句话 —— 对"真没货"它是真话。"""
    from services.geo_douyin.ranking_router import _fallback_notice

    nt = _fallback_notice("no_candidates") or {}
    assert "还没攒够" in nt["message"]
    assert "supplement_candidates" in {a["id"] for a in nt["actions"]}
