# -*- coding: utf-8 -*-
"""榜单主链接线锁 + 端到端判别测试 · 返工单 §1(本次返工的主体)

背景:上一版 `fetch_ranking_candidates` / `route_template` / `decide_form` /
`FrozenRankingPayload` / `rank_statement` 在运行时代码里**零调用方**,
`production_task` 仍直呼 `generate_image_post_content`。
**200 条锁证明的是模块自洽,不是付费链成立。**

本文件两层:
  ① 接线锁(静态)—— 从付费任务入口能不能到达这些符号;删接线行必须转红;
  ② 端到端判别测试(行为)—— 从付费任务入口真跑一次,断言
     `template_id + frozen payload + rendered cards` **三者在产出里同时成立**;
     并配反向对照:卡组型订单**不**触发榜单分支(现有行为逐字不变)。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
PROD = REPO / "services" / "geo_douyin" / "production_task.py"
API = REPO / "api" / "geo_douyin_api.py"
GEN = REPO / "services" / "geo_douyin" / "content_generator.py"


# ===========================================================================
# ① 接线锁(静态可达)
# ===========================================================================

def _calls_in(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.name.split(".")[-1])
    return out


def test_paid_task_calls_the_ranking_orchestrator():
    """🔴 付费任务入口必须真的调编排函数 —— 这是"零调用方"那条的直接对治。"""
    assert "build_ranking_plan" in _calls_in(PROD), \
        "production_task 没有调 build_ranking_plan —— 榜单链仍未接进付费链"


def _names_in_fn(path: pathlib.Path, fn_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == fn_name)
    out = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    out |= {a.name.split(".")[-1] for n in ast.walk(fn)
            if isinstance(n, ast.ImportFrom) for a in n.names}
    return out


def test_paid_task_passes_the_ranking_knobs_down():
    """🔴 家数 / 版式 / 坚持榜单三个旋钮都要真的传到编排函数。

    只断"调了 build_ranking_plan"不够 —— 参数漏传的表现是"选了没反应",
    和画幅那次的闭包 bug 一模一样(功能在、入口在、就是不生效)。
    """
    tree = ast.parse(PROD.read_text(encoding="utf-8"))
    # 找到 `asyncio.to_thread(build_ranking_plan, ...)` 那一次调用,按 **AST 关键字**
    # 断言,而不是在源码字符串里数 `xxx=` —— 后者会被注释里的示例骗过。
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and any(isinstance(a, ast.Name) and a.id == "build_ranking_plan"
                     for a in n.args)]
    assert calls, "没有以 build_ranking_plan 为目标的 to_thread 调用"
    kws = {k.arg for c in calls for k in c.keywords}
    for kw in ("want", "force_template", "force_ranking",
               "industry_key", "keyword", "city", "client_brand"):
        assert kw in kws, f"编排调用漏了 {kw}=(选了没反应就是这么来的)"


@pytest.mark.parametrize("sym", [
    "fetch_ranking_candidates", "route_template", "FrozenRankingPayload",
    "rank_statement", "select_entities", "clean_phrases",
])
def test_orchestrator_directly_uses(sym):
    """编排函数**直接**用到的符号。"""
    router = REPO / "services" / "geo_douyin" / "ranking_router.py"
    assert sym in _names_in_fn(router, "build_ranking_plan"), f"编排函数没用到 {sym}"


def test_decide_form_is_reached_through_route_template():
    """`decide_form` 不在编排函数里直呼 —— 它由 `route_template` 调。

    🔴 按**真实调用图**分层断言,而不是把七个符号一股脑塞进一个集合里查:
       前一版就是那么写的,结果这条锁在正确实现上转红(判据比实现还严)。
    """
    router = REPO / "services" / "geo_douyin" / "ranking_router.py"
    assert "decide_form" in _names_in_fn(router, "route_template"), \
        "route_template 没有调 decide_form —— 两半式判定断了"
    assert "route_template" in _names_in_fn(router, "build_ranking_plan")


def test_paid_task_runs_the_gates():
    assert "run_gates" in _calls_in(PROD), "付费链没跑榜单闸"


def test_api_exposes_the_trigger_and_passes_it_down():
    src = API.read_text(encoding="utf-8")
    i = src.find("class CreatePostRequest")
    assert i > 0
    assert "content_form" in src[i: i + 1600], "CreatePostRequest 没有 content_form"
    j = src.find("dispatch_production(")
    assert j > 0
    assert "content_form=" in src[j: j + 900], "dispatch 没把 content_form 传下去"


def test_generator_accepts_the_frozen_payload():
    tree = ast.parse(GEN.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "generate_image_post_content")
    args = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "ranking_plan" in args, "生成侧不收冻结件 —— 白名单进不了 prompt"


def test_frozen_payload_actually_reaches_the_prompt():
    """反向对照:收了参数但不拼进 prompt = 假接线。"""
    src = GEN.read_text(encoding="utf-8")
    assert "{ranking_block}" in src, "_PROMPT 没有榜单占位符"
    assert "ranking_block=ranking_prompt_block(" in src, "占位符没接线,format 会 KeyError"


# ===========================================================================
# ② 端到端判别测试(从付费任务入口真跑)
# ===========================================================================

class _FakeContent:
    ok = True
    error = ""
    body = "这一单我跑了一圈,把几家放一起看了下。"
    title = "深圳载货电梯怎么选｜实测对比,报价与工期一次说清"
    hashtags = ["电梯", "载货电梯", "深圳", "选型", "实测"]
    cards = [{"entity": "测试甲", "one_liner": "L", "points": ["a", "b"],
              "metric": "", "caveat": "旧楼加装需先评估", "headline": "测试甲",
              "caveat_autofilled": False}]
    cover = {"title": "T", "subtitle": "S"}
    closing = {"headline": "H", "summary": "M", "caveat": "C", "brand_line": "B"}
    ad_law_flags: list = []
    skeleton = "per_entity"
    model = "stub"
    kb_sources: list = []
    has_real_photo = False
    visual: dict = {}

    def to_dict(self) -> dict:
        return {"body": self.body, "cards": self.cards, "cover": self.cover,
                "closing": self.closing, "visual": self.visual,
                "skeleton": self.skeleton}


@pytest.fixture
def wired(monkeypatch):
    """把付费任务的外部依赖全 stub 掉,只留**接线本身**受测。"""
    import db.geo_douyin_db as ddb
    import middleware.billing as billing
    import services.geo_douyin.content_generator as cg
    import services.geo_douyin.image_pipeline as ip
    import services.geo_douyin.pricing as pricing

    saved: dict = {}

    monkeypatch.setattr(ddb, "create_task", lambda **k: 1, raising=False)
    monkeypatch.setattr(ddb, "set_task_freeze", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ddb, "update_task", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ddb, "set_post_status", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ddb, "bump_task_progress", lambda *a, **k: None, raising=False)

    def _upd(post_id, **kw):
        saved.update(kw)
    monkeypatch.setattr(ddb, "update_post_content", _upd, raising=False)
    monkeypatch.setattr(ddb, "update_post_cards", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ddb, "get_post", lambda *a, **k: {}, raising=False)

    async def _freeze(*a, **k):
        return {"freeze_id": 1}
    monkeypatch.setattr(billing, "freeze_points", _freeze, raising=False)

    async def _commit(**k):
        return {}
    monkeypatch.setattr(billing, "commit_freeze", _commit, raising=False)

    async def _release(**k):
        return {}
    monkeypatch.setattr(billing, "release_freeze", _release, raising=False)

    async def _price(n):
        return 0
    monkeypatch.setattr(pricing, "extra_card_points", _price, raising=False)

    seen: dict = {}

    async def _gen(keyword, **kw):
        seen["ranking_plan"] = kw.get("ranking_plan")
        return _FakeContent()
    monkeypatch.setattr(cg, "generate_image_post_content", _gen, raising=False)

    monkeypatch.setattr(ip, "build_prompts_for_group",
                        lambda *a, **k: [{"kind": "cover", "headline": "T",
                                          "prompt": "P", "role_label": "封面",
                                          "layout_role": "全幅场景照 + 大标题色块"}],
                        raising=False)

    async def _render(post_id, specs, **kw):
        class R:
            ok = True
            oss_key = "k"
        return [R()]
    monkeypatch.setattr(ip, "render_prompt_group", _render, raising=False)

    return saved, seen


#: 🔴 端到端用的候选**必须由本文件自己造**。
#:    上一版直接用了一个我在会话里手插进测试库的 `test_ind` —— 于是这条
#:    "第一验项"的端到端测试在**干净库上根本跑不起来**,它绿只是因为残留数据。
#:    交付冻结的规矩是"只认干净检出实跑",fixture 不自带数据就等于没跑过。
_E2E_IND = "e2e_ranking_ind"
_E2E_CLIENT = "端到端探针客户"


#: 每个行业占两个 fact id(两个引擎)。写死映射,避免用自增/随机导致重跑不幂等。
_FACT_IDS = {_E2E_IND: (990201, 990202), "auto": (990211, 990212)}
_HASH_SEED = {_E2E_IND: ("e", "f"), "auto": ("g", "h")}


def _seed_industry(industry: str) -> None:
    """造 4 家 × 2 引擎的候选池,其中一家就是客户本人(且有实名次)。

    两个引擎是**硬要求**:`select_entities` 要 `engine_count >= 2` 才让进榜,
    只造一行的话池子是空的,端到端会静默走成"无候选降级"而不是真榜单。
    **全程幂等** —— 变异 runner 会反复跑,不幂等就是"基线红"。
    """
    from db.connection import get_connection

    names = [(_E2E_CLIENT, "me_e2e0", 1), ("探针甲电梯", "me_e2e1", 2),
             ("探针乙电梯", "me_e2e2", 3), ("探针丙电梯", "me_e2e3", 4)]
    fid_a, fid_b = _FACT_IDS[industry]
    ha, hb = _HASH_SEED[industry]
    conn = get_connection()
    try:
        cur = conn.cursor()
        # answer_hash 是 character(32):多一位就 StringDataRightTruncation。
        cur.execute("""
            INSERT INTO geo_research_answer_facts
                (id, raw_id, industry_key, query, engine, batch_id, answer_hash,
                 extractor_version)
            VALUES (%s, 11, %s, '深圳载货电梯哪家好', 'kimi',
                    'e2e_ranking', %s, 'v1'),
                   (%s, 12, %s, '深圳载货电梯哪家好', 'deepseek',
                    'e2e_ranking', %s, 'v1')
            ON CONFLICT DO NOTHING
        """, (fid_a, industry, ha * 32, fid_b, industry, hb * 32))
        for fact_id, raw_id, engine in ((fid_a, 11, "kimi"), (fid_b, 12, "deepseek")):
            for name, key, rank in names:
                # 幂等:`(answer_fact_id, entity_key)` 上有唯一约束,不加 ON CONFLICT
                # 第二次跑就 UniqueViolation → 表现为"基线红" → 整轮变异结论作废。
                cur.execute("""
                    INSERT INTO geo_research_answer_entities
                        (answer_fact_id, raw_id, industry_key, engine, entity_name,
                         entity_key, entity_type, recommendation_rank,
                         evidence_phrases, recommendation_reasons,
                         confidence, llm_model, extractor_version)
                    VALUES (%s, %s, %s, %s, %s, %s, 'brand', %s,
                            '[]', '[]', 1.0, 'm', 'v1')
                    ON CONFLICT (answer_fact_id, entity_key) DO NOTHING
                """, (fact_id, raw_id, industry, engine, name, key, rank))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded_candidates():
    _seed_industry(_E2E_IND)
    yield


#: 🔴 2026-08-07:榜单形态下封面与收尾各占一张,剩下的才是企业卡。
#:    原来这里写的是 3 张 —— 只剩 1 个企业卡位,装不下最少 3 家,
#:    于是所有端到端都会落到 `card_budget_too_small` 那一支。
#:    这不是测试坏了,是**新增的结构约束真的生效了**;张数跟着改。
_E2E_CARDS = 6          # 6 张 = 封面 + 4 家 + 收尾


def _run(content_form: str, *, industry_key: str = _E2E_IND,
         brand_name: str = _E2E_CLIENT, card_count: int = _E2E_CARDS, **kw):
    from services.geo_douyin.production_task import run_image_post_production
    return asyncio.run(run_image_post_production(
        post_id=1, user_id=1, keyword="深圳载货电梯哪家好",
        brand_name=brand_name, city="深圳", card_count=card_count,
        industry_key=industry_key, content_form=content_form, **kw))


@pytest.mark.usefixtures("wired", "seeded_candidates")
def test_end_to_end_ranking_order_produces_all_three_at_once(wired):
    """🔴 本文件最重要的一条:从**付费任务入口**跑一单榜单,断言
    `template_id` + `frozen payload` + `rendered cards` **三者同时成立**。

    只断言其中一个都不够 —— 上一版的问题正是"模块能产出冻结件"与
    "付费链真的用了它"被混为一谈。

    `ranking_force=True` = 用户手动选了榜单版式(覆盖"这个行业默认不走榜单"),
    这样本条走的是**真榜单**那一支,而不是降级支。
    """
    saved, seen = wired
    _run("ranking", ranking_force=True)

    plan = seen.get("ranking_plan")
    assert plan is not None, "付费链没把冻结件传进生成侧"

    meta = saved.get("generation_meta") or {}
    rk = meta.get("ranking") or {}
    assert rk.get("template_id") in (
        "top3_provider", "tech_spec_matrix", "scenario_fit", "regional_service",
        "industrial_overview", "investment_stage", "delivery_capability",
        "collab_mode", "retrofit_upgrade", "enterprise_scale",
    ), f"落库没有 template_id:{rk}"

    frozen = rk.get("frozen") or {}
    assert frozen.get("contract_hash"), "落库没有冻结快照的 contract_hash"
    assert frozen.get("items"), "冻结快照没有 items"

    cards = saved.get("cards") or []
    assert cards, "没有渲染出的卡"

    # 三者必须**同时**成立 —— 分开断言会掩盖"有 payload 但没进产出"这类假接线
    assert rk.get("template_id") and frozen.get("contract_hash") and cards
    # 真榜单这一支不该有降级留痕
    assert rk.get("effective_form") == "ranking", rk
    assert not rk.get("degraded"), f"真榜单被标成降级:{rk.get('fallback_reason')}"


@pytest.mark.usefixtures("wired", "seeded_candidates")
def test_entity_count_knob_actually_limits_the_frozen_list(wired):
    """反向对照:家数旋钮不是摆设 —— 传 3 就只能冻 3 家。

    (画幅那次的教训:入口加了、参数没接上,全程不报错。)
    """
    saved, _ = wired
    _run("ranking", ranking_force=True, ranking_entity_count=3)
    items = ((saved.get("generation_meta") or {}).get("ranking") or {}) \
        .get("frozen", {}).get("items", [])
    assert len(items) == 3, f"要 3 家,冻了 {len(items)} 家"


@pytest.mark.usefixtures("wired", "seeded_candidates")
def test_template_knob_actually_selects_the_master(wired):
    saved, _ = wired
    _run("ranking", ranking_force=True, ranking_template="tech_spec_matrix")
    rk = (saved.get("generation_meta") or {}).get("ranking") or {}
    assert rk.get("template_id") == "tech_spec_matrix", rk.get("template_id")


@pytest.mark.usefixtures("wired")
def test_card_group_order_does_not_touch_the_ranking_branch(wired):
    """🔴 反向对照:卡组型订单**逐字不变** —— 不传冻结件、meta 里不出现 ranking 键。"""
    saved, seen = wired
    _run("")            # 空 = 卡组型

    assert seen.get("ranking_plan") is None, "卡组型订单被塞了冻结件"
    meta = saved.get("generation_meta") or {}
    assert "ranking" not in meta, "卡组型订单的 meta 里混进了 ranking"
    assert "ranking_gates" not in meta


@pytest.mark.usefixtures("wired", "seeded_candidates")
def test_gates_run_and_are_persisted_for_ranking_orders(wired):
    saved, _ = wired
    _run("ranking", ranking_force=True)
    meta = saved.get("generation_meta") or {}
    assert "ranking_gates" in meta, "闸结果没落库"
    for f in meta["ranking_gates"]:
        assert f["level"] == "A1", f"{f['gate']} 在付费链上不是 A1"
        assert "blocking" not in f, "落库的闸结果里还有 blocking 死字段"


# ===========================================================================
# ③ 降级不许静默换货
# ===========================================================================

@pytest.mark.usefixtures("wired")
def test_no_candidates_falls_back_instead_of_failing(wired):
    """反向对照:候选取不到 → 退回卡组型,**不让整单失败**(永不中断)。"""
    saved, seen = wired
    out = _run("ranking", industry_key="根本不存在的行业", brand_name="没有的客户")
    assert seen.get("ranking_plan") is None
    assert out.error in ("", None) or "ranking" not in str(out.error)


@pytest.mark.usefixtures("wired")
def test_silent_swap_is_impossible_when_falling_back_to_card_group(wired):
    """🔴 用户点的是榜单、拿到的是卡组 —— **必须落痕并给出人话与出口**。

    上一版这里只有一行服务器日志,用户永远不知道自己拿到的不是他点的东西。
    """
    saved, _ = wired
    _run("ranking", industry_key="根本不存在的行业", brand_name="没有的客户")
    rk = (saved.get("generation_meta") or {}).get("ranking") or {}
    assert rk, "退回卡组型时 meta 里连 ranking 键都没有 = 静默换货"
    assert rk["requested_form"] == "ranking"
    assert rk["effective_form"] == "card_group"
    # 🔴 2026-08-08 口径变更(包 A · 四态合同):「根本不存在的行业」是一句**没被认出来的
    #    自由文本**,不是"这个行业确实没有同行数据" —— 我们并不知道后者。
    #    旧值 `no_candidates` 把这两件事说成同一句,而那句话对本例是**假话**。
    #    `no_candidates` 现在**只**表示 `resolved_no_inventory`(归并成功、行业真没货),
    #    它的反向对照在 `test_industry_canonical_wiring_2026_08_08.py` 里成对锁着 ——
    #    两态不能塌成一个,否则这条断言换成任何一个值都恒绿。
    assert rk["fallback_reason"] == "industry_unmerged"
    assert rk["degraded"] is True
    nt = rk.get("fallback_notice") or {}
    assert nt.get("message"), "没有给用户一句人话"
    ids = {a["id"] for a in nt.get("actions", [])}
    assert "keep_current" in ids, "没有「就用这版」这个出口"
    assert len(ids) >= 2, f"少于两个出口:{ids}"


@pytest.mark.usefixtures("wired", "seeded_candidates")
def test_industry_default_downgrade_is_also_disclosed(wired):
    """有据但这个行业默认不走榜单 → 同样是"没给他点的那个",同样要告知。"""
    saved, _ = wired
    _run("ranking")          # 不 force → 走面×行业默认
    rk = (saved.get("generation_meta") or {}).get("ranking") or {}
    assert rk["requested_form"] == "ranking"
    assert rk["effective_form"] != "ranking"
    assert rk["degraded"] is True
    # 🔴 判**具体那一支**,不写成 in (A, B) —— 二选一的断言在两支互换时
    #    照样绿,变异「有据判定被短路」就是从这个洞活着穿过去的。
    assert rk["fallback_reason"] == "industry_default_not_ranking", rk["fallback_reason"]


@pytest.mark.usefixtures("wired", "seeded_candidates")
def test_no_client_evidence_downgrade_names_its_own_reason(wired):
    """客户查无位次 → 必须是 `no_client_evidence`,不能笼统报成行业默认。

    两支的**出口不一样**(一支是去跑监测、一支是手动选榜单版式),
    报错原因串了,给的出口就是错的。
    """
    saved, _ = wired
    _run("ranking", brand_name="池子里根本没有的客户", ranking_force=True)
    rk = (saved.get("generation_meta") or {}).get("ranking") or {}
    assert rk["fallback_reason"] == "no_client_evidence", rk["fallback_reason"]
    ids = {a["id"] for a in (rk.get("fallback_notice") or {}).get("actions", [])}
    assert "refresh_monitoring" in ids, f"给错了出口:{ids}"


@pytest.mark.usefixtures("wired", "seeded_candidates")
def test_positive_industry_keeps_ranking_without_forcing(wired):
    """反向对照:实测为正的行业**不 force 也该出榜单**。

    🔴 这条盯的是 `force_ranking=bool(...)` 这种写法:没勾选时传 False
       会把"用默认"变成"要求非榜单",反手关掉本来默认为正的行业 ——
       而且全程不报错,只是这些行业再也出不了榜单。
    """
    saved, _ = wired
    _seed_industry("auto")
    _run("ranking", industry_key="auto")
    rk = (saved.get("generation_meta") or {}).get("ranking") or {}
    assert rk["effective_form"] == "ranking", rk
    assert not rk["degraded"], rk.get("fallback_reason")


@pytest.mark.parametrize("reason", [
    "no_candidates", "selection_empty", "no_client_evidence",
    "industry_default_not_ranking", "orchestration_error",
])
def test_every_fallback_message_is_business_language(reason):
    """🔴 Owner 顶层铁律:禁一切合规表演。**每一支**都要查,不能只查一支。"""
    from services.geo_douyin import ranking_router as rr
    nt = rr.RankingOutcome(plan=None, fallback_reason=reason,
                           client_brand="某客户").notice()
    assert nt and nt["message"]
    for banned in ("合规", "违规", "不足以出榜", "禁止", "风险", "降级"):
        assert banned not in nt["message"], \
            f"{reason} 的文案里出现了合规腔「{banned}」:{nt['message']}"


@pytest.mark.usefixtures("wired")
def test_nav_actions_always_carry_a_real_href(wired):
    """反向对照:nav 类出口必须自带 href —— 前端不许猜路由(编过一次死链)。"""
    from services.geo_douyin import ranking_router as rr
    for reason in (rr.FALLBACK_NO_CANDIDATES, rr.FALLBACK_SELECTION_EMPTY,
                   rr.FALLBACK_NO_CLIENT_EVIDENCE, rr.FALLBACK_INDUSTRY_DEFAULT,
                   rr.FALLBACK_ERROR):
        nt = rr.RankingOutcome(plan=None, fallback_reason=reason).notice()
        assert nt, reason
        for a in nt["actions"]:
            if a["type"] == "nav":
                assert a.get("href", "").startswith("/"), f"{reason} 的 nav 没有 href"


def test_no_fallback_notice_when_nothing_degraded():
    """反向对照:没降级就**不许**弹提示(提示要么帮人解决要么不显示)。"""
    from services.geo_douyin import ranking_router as rr
    o = rr.RankingOutcome(plan=None, effective_form="ranking",
                          fallback_reason=rr.FALLBACK_NONE)
    assert o.notice() is None
    assert o.degraded is False
    assert "fallback_notice" not in o.to_meta()
