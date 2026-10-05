# -*- coding: utf-8 -*-
"""上下文共用 / 豆包目标面 / 构造式企业卡 / 再次创作继承 · R3 判别测试

对应工单 `GEOVID_RANKING_CONTEXT_R3_2026-08-07` §六 后端 1-11。

🔴 每条都配**成对反向对照**;只写"必须命中"的锁抓不出"锚点撞到别处"。
🔴 判据里的期望值一律写死在本文件,**不从被测对象推导** ——
   从被测对象取期望值的锁是自指恒真(本包 R8 那条已经栽过一次)。
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib

import pytest

from services.geo_douyin import ranking_router as rr
from services.geo_douyin import ranking_source as rs
from services.geo_douyin import series_plan as sp


REPO = pathlib.Path(__file__).resolve().parent.parent
API = REPO / "api" / "geo_douyin_api.py"
ROUTER = REPO / "services" / "geo_douyin" / "ranking_router.py"
GEN = REPO / "services" / "geo_douyin" / "content_generator.py"
CKB = REPO / "services" / "client_knowledge.py"
# [WO_271 · 2026-09-23] 原来这里是旧入口页 TSX_NEW(DouyinImagePost.tsx,6b491ab23 删);读它的两格
# 随页一起没了对象,原位退役,登记在 tests/RETIRED_TESTS.txt。
TSX_DETAIL = REPO / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"
SOURCE = REPO / "services" / "geo_douyin" / "ranking_source.py"


def _py_code_only(text: str) -> str:
    """剥掉注释与 docstring 后的源码(注释不进 AST,docstring 逐节点弹掉)。

    🔴 形态锁**必须**先过这一步:同一个坑本仓已经栽过四次 —— 锁打在原文上时,
       会撞到我自己写的那段"为什么删掉它"的说明,于是"已删除"的锁恒红。
       剥完**必须自证剥干净**,见下面那条自检。
    """
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
    return ast.unparse(tree)


def test_the_py_stripper_actually_strips():
    """自检:剥离器不生效的话,所有"已删除"形态锁都变成恒真。"""
    raw = ROUTER.read_text(encoding="utf-8")
    assert "死元数据" in raw, "样本句不在源码里 —— 这条自检失去意义"
    assert "死元数据" not in _py_code_only(raw), "注释没被剥掉"
    # 反向面:**字符串字面量不许被一起剥掉**,否则"取值串还在吗"那类锁会假绿
    assert "no_candidates" in _py_code_only(raw), "把字符串字面量也剥没了"


# ===========================================================================
# ① 引擎名 SSOT —— 这一条不成立,后面全部恒降级
# ===========================================================================

#: 🔴 写死。这三套写法是 2026-08-07 只读通道从生产**逐表查出来**的:
#:    answer_entities → 中文;source_signals → 小写拉丁;
#:    quotes.competitor_list.confirmed_by → 首字母大写 + 非引擎来源。
_KNOWN_SPELLINGS = {
    "豆包": {"doubao", "豆包", "doubao_app"},
    "Kimi": {"kimi"},
    "千问": {"qwen", "千问", "dashscope"},
}


def test_engine_spellings_come_from_the_shared_map_not_a_new_table():
    """规范名 → 全部原始写法。**本模块不许自建第二份映射。**"""
    for canon, expect in _KNOWN_SPELLINGS.items():
        got = set(rs.engine_raw_spellings(canon))
        assert expect <= got, f"{canon} 少了写法:{expect - got}"


def test_target_engine_is_a_canonical_spelling_not_a_latin_literal():
    """🔴 本轮最贵的一条:工单原文写的是 `engine = doubao`。

    `geo_research_answer_entities.engine` 存的是**中文**「豆包」——
    照抄拉丁字面量会匹配 0 行,而且**不报错**:候选恒空 → 榜单恒降级。
    (我自己第一次查生产也踩了这个,`ILIKE '%doubao%'` 返回 0 差点得出反结论。)
    """
    assert rr.TARGET_ENGINE == "豆包", rr.TARGET_ENGINE
    assert "doubao" in rs.engine_raw_spellings(rr.TARGET_ENGINE)


def test_unknown_engine_yields_only_itself():
    """反向对照:没在映射里的名字不许被扩写成别人的别名。"""
    assert set(rs.engine_raw_spellings("秘塔直搜")) == {"秘塔直搜"}


# ===========================================================================
# ② 发布面服务端冻结(工单 §P1-2)
# ===========================================================================

def test_target_face_is_frozen_server_side():
    face = rr.target_face()
    assert face == {"target_engine": "豆包", "target_surface": "douyin_image_post"}


def test_frontend_cannot_set_the_target_face():
    """🔴 「不能信任前端传值来决定目标引擎」—— 请求模型里不许有这两个字段。"""
    tree = ast.parse(API.read_text(encoding="utf-8"))
    for cls in ("CreatePostRequest", "RegenerateRequest"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == cls)
        fields = {t.target.id for t in node.body if isinstance(t, ast.AnnAssign)
                  and isinstance(t.target, ast.Name)}
        assert "target_engine" not in fields, f"{cls} 让前端定目标引擎了"
        assert "target_surface" not in fields, f"{cls} 让前端定目标面了"


def test_target_face_lands_in_meta():
    m = rr.RankingOutcome(plan=None, fallback_reason=rr.FALLBACK_NO_CANDIDATES).to_meta()
    assert m["target_engine"] == "豆包" and m["target_surface"] == "douyin_image_post"


# ===========================================================================
# ③ 名次取值面豆包优先,**共识面不收窄**(本轮与工单口径不同的那一处)
# ===========================================================================

def test_candidate_sql_does_not_filter_by_engine():
    """🔴 只许"优先",不许"只要"。

    候选被收窄成单引擎时 `engine_count` 恒为 1,而入池闸是
    `MIN_ENGINE_CONSENSUS = 2` —— 池子会被挡光,功能变成 100% 降级。
    所以 SQL 里 engine 只能出现在**排序键**上,不能出现在 WHERE 里。
    """
    src = (REPO / "services" / "geo_douyin" / "ranking_source.py").read_text(encoding="utf-8")
    i, j = src.find("WITH best AS"), src.find("), agg AS")
    assert 0 < i < j
    best = src[i:j]
    where = best[best.find("WHERE"):best.find("ORDER BY")]
    assert "engine" not in where, f"候选被按引擎过滤了:{where}"
    order = best[best.find("ORDER BY"):]
    assert "LOWER(e.engine) <> ALL" in order, "排序键里没有目标引擎优先"


def test_consensus_gate_still_needs_two_engines():
    """反向对照:共识门槛没被顺手改掉(改了就等于默许单引擎)。"""
    from services.geo_douyin.ranking_payload import MIN_ENGINE_CONSENSUS
    assert MIN_ENGINE_CONSENSUS == 2


def test_orchestrator_asks_for_preference_not_filter():
    fn = next(n for n in ast.walk(ast.parse(ROUTER.read_text(encoding="utf-8")))
              if isinstance(n, ast.FunctionDef) and n.name == "build_ranking_plan")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "fetch_ranking_candidates"]
    assert calls, "编排函数没取候选"
    kws = {k.arg for k in calls[0].keywords}
    assert "prefer_engine" in kws, "没传目标引擎偏好"
    assert "engine" not in kws and "only_engine" not in kws, "把偏好写成了过滤"


# ===========================================================================
# ④ 构造式企业卡(工单 §P1-4)
# ===========================================================================

_NAMES = ["通力电梯", "日立电梯", "快意电梯", "康力电梯"]


def test_ranking_role_plan_assigns_one_company_per_card():
    plan = sp.plan_ranking_roles(6, _NAMES)
    assert [p["role"] for p in plan] == ["cover", "entity", "entity", "entity",
                                         "entity", "closing"]
    assert [p["entity_name"] for p in plan if p["role"] == "entity"] == _NAMES


def test_entity_slots_reserve_cover_and_closing():
    """🔴 写死期望值,不从 `RANKING_FIXED_CARDS` 推导(推导 = 常量改了锁跟着改 = 恒真)。"""
    assert {n: sp.entity_slots_for(n) for n in (3, 4, 5, 6, 7, 9)} == {
        3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 9: 7}


# ── [WO_271] 退役:test_frontend_slot_formula_matches_backend ──
# 锁的是旧入口页里前端**另算一遍**的排名槽位(RANKING_FIXED_CARDS / cardCount - …),
# 那份前端公式随页删除(6b491ab23),现役图文流不再自己算槽位 —— 「两处算两遍必漂」只剩后端一处。
# 后端口径仍由上面 test_entity_slots_reserve_cover_and_closing 写死期望值锁住;前端公式不许回来由
# 必跑集 tests/test_geo_douyin_detail_ui.py::test_retired_image_note_pages_stay_deleted 守
# (RANKING_FIXED_CARDS 在前端源码里再出现就红)。登记:tests/RETIRED_TESTS.txt。


def test_prompt_names_the_company_for_each_entity_card():
    """名字进 prompt 是**代码写的**,模型没有选择权。"""
    block = sp.ranking_roles_prompt_block(6, _NAMES, ["在 豆包 回答「Q」时列第 1"] * 4)
    for n in _NAMES:
        assert f"只讲「{n}」这一家" in block, n
    assert "不许换成别家" in block


def test_generator_writes_entity_ref_by_code_not_by_model():
    """🔴 P1-4 的落点:`entity_ref` 由代码回填,**覆盖模型给的任何值**。

    这就把"模型省略 entity_ref 即可绕过虚构企业检查"在结构上堵死。
    """
    src = GEN.read_text(encoding="utf-8")
    i = src.find("for card, plan in zip(cards, role_plan):")
    assert i > 0
    block = src[i: i + 900]
    assert 'card["entity_ref"] = str(plan["entity_name"])' in block, \
        "entity_ref 没有由代码回填"


def test_card_group_orders_get_no_entity_ref_assignment():
    """反向对照:卡组型的职责表没有 entity_name → 不写 entity_ref(行为逐字不变)。"""
    plan = sp.content_role_plan(5)
    assert plan and all("entity_name" not in p for p in plan)


# ===========================================================================
# ⑤ 张数装不下家数 → 如实降级(不许悄悄少做)
# ===========================================================================

def test_card_budget_too_small_is_its_own_reason_with_its_own_exit():
    n = rr._fallback_notice(rr.FALLBACK_CARD_BUDGET)
    assert n and "张数" in n["message"]
    ids = {a["id"] for a in n["actions"]}
    assert "raise_card_count" in ids, f"给错了出口:{ids}"
    # 反向:这一支与监测无关,给"去跑监测"是答非所问
    assert "refresh_monitoring" not in ids


@pytest.mark.parametrize("reason,expect", [
    (rr.FALLBACK_NO_CLIENT_EVIDENCE, "refresh_monitoring"),
    (rr.FALLBACK_INDUSTRY_DEFAULT, "force_ranking_template"),
    (rr.FALLBACK_NO_CANDIDATES, "supplement_candidates"),
    (rr.FALLBACK_CARD_BUDGET, "raise_card_count"),
])
def test_each_reason_gets_the_exit_that_actually_helps(reason, expect):
    ids = {a["id"] for a in rr._fallback_notice(reason)["actions"]}
    assert expect in ids, f"{reason} 的出口是 {ids}"
    assert "keep_current" in ids, "没有「就用这版」"


@pytest.mark.parametrize("reason", [
    rr.FALLBACK_NO_CANDIDATES, rr.FALLBACK_SELECTION_EMPTY,
    rr.FALLBACK_NO_CLIENT_EVIDENCE, rr.FALLBACK_INDUSTRY_DEFAULT,
    rr.FALLBACK_ERROR, rr.FALLBACK_CARD_BUDGET,
])
def test_every_message_is_business_language(reason):
    msg = rr._fallback_notice(reason, client_brand="某客户")["message"]
    for banned in ("合规", "违规", "不足以出榜", "禁止", "风险", "降级"):
        assert banned not in msg, f"{reason} 出现合规腔「{banned}」:{msg}"


# ===========================================================================
# ⑥ 人工确认竞品:模式闸 + 血缘 + 上界(工单 §P1-1)
# ===========================================================================

def test_fictional_competitor_mode_is_excluded():
    """🔴 生产实测 `competitor_mode` 分布:fictional 340 / real 47 / semi 4 /
    evidence_only 1。**fictional 是演示用的编造竞品** ——
    不按 mode 过滤就直接当"人工确认名单",等于把编的公司名放进对外榜单。
    """
    from services import client_knowledge as ck
    assert "fictional" not in ck.REAL_COMPETITOR_MODES
    assert set(ck.REAL_COMPETITOR_MODES) == {"real", "semi", "evidence_only"}


def test_only_confirmed_or_paid_quotes_count():
    from services import client_knowledge as ck
    assert set(ck.CONFIRMED_QUOTE_STATUSES) == {"confirmed", "paid"}
    assert "draft" not in ck.CONFIRMED_QUOTE_STATUSES


def test_lineage_is_by_keyword_not_by_latest_quote():
    """🔴 「不得通过'取该品牌最近一份报价'猜测关联关系」。"""
    src = CKB.read_text(encoding="utf-8")
    i = src.find("def load_confirmed_competitors")
    assert i > 0
    body = src[i: i + 2600]
    assert "confirmed_keywords" in body, "没有按词血缘关联"
    assert "ck.keyword = ANY(%s)" in body


def test_confirmed_entity_id_shares_the_evidence_key_space():
    """🔴 "共用"的实质:人工名单的 id 与 `answer_entities.entity_key` **同一个键空间**,
    否则两张名字列表永远 join 不上,所谓共用只是并列摆着。"""
    src = CKB.read_text(encoding="utf-8")
    i = src.find("def load_confirmed_competitors")
    assert "build_answer_entity_key" in src[i: i + 2600]


def test_confirmed_list_is_an_upper_bound_not_an_addition():
    """名单**能对齐时**候选只能收窄到名单内,不许把名单外的公司加进来。

    🔴 2026-08-08 修订:"对不齐就降级"那半条已改成"对不齐就回落全量池"
    (偏离说明见 build_ranking_plan 内注释)。"只收窄不扩写"这一半**一个字没松** ——
    收窄的判据留在这里,回落的判据在 ⑫ 行为面(拿真库跑,不是读源码)。
    """
    fn = next(n for n in ast.walk(ast.parse(ROUTER.read_text(encoding="utf-8")))
              if isinstance(n, ast.FunctionDef) and n.name == "build_ranking_plan")
    src = ast.get_source_segment(ROUTER.read_text(encoding="utf-8"), fn) or ""
    assert "inside = [c for c in pool" in src, "没有按名单收窄"
    assert "pool = inside" in src
    # 反向面:收窄**只能**发生在命中数达标时 —— 没有这道门就是无条件收窄,
    # 名字口径一不齐就把好候选全砍了(这正是本次要修的)。
    assert "if len(inside) >= ENTITY_COUNT_MIN:" in src, "收窄没有设门槛"
    # 反向面:名单是上界不是补充 —— 任何"把名单里的名字加进池子"的写法都不许出现
    assert "pool = pool +" not in src and "pool.extend" not in src


def test_removed_reason_leaves_no_ghost():
    """`FALLBACK_CONFIRMED_NO_EVIDENCE` 已删,不许以任何形式回潮。

    死元数据家族已经栽过四次(blocking / degrade_to / actions / repair_scope),
    这条是第五次的预防:常量、文案表、出口分支三处一起查。
    """
    assert not hasattr(rr, "FALLBACK_CONFIRMED_NO_EVIDENCE")
    src = ROUTER.read_text(encoding="utf-8")
    code = _py_code_only(src)          # 剥注释与 docstring:说明为什么删它是允许的
    assert "confirmed_list_lacks_evidence" not in code, "取值串还在代码里"
    assert "FALLBACK_CONFIRMED_NO_EVIDENCE" not in code, "常量名还在代码里"


# ===========================================================================
# ⑦ 再次创作继承(工单 §P1-3)· 本轮唯一"正在烧钱"的那条
# ===========================================================================

def test_regenerate_request_keeps_the_three_state_distinction():
    """🔴 `None`(没改)与 `""`(显式选自动)必须分得开 —— 写成 `str = ""` 继承当场失效。"""
    tree = ast.parse(API.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "RegenerateRequest")
    ann = {t.target.id: ast.unparse(t.annotation) for t in node.body
           if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
    assert ann.get("ranking_template") == "Optional[str]", ann
    assert ann.get("ranking_entity_count") == "Optional[int]", ann


def test_regenerate_reads_the_frozen_snapshot_and_passes_it_down():
    src = API.read_text(encoding="utf-8")
    i = src.find("async def api_regenerate_post")
    assert i > 0
    body = src[i: src.find("@router.delete", i)]
    assert "RankingRequest.from_meta" in body, "没从冻结快照继承"
    for kw in ("content_form=", "ranking_entity_count=", "ranking_template=",
               "ranking_force="):
        assert kw in body, f"再次创作漏传 {kw}"


def test_snapshot_restores_exactly_what_the_user_asked_for():
    req = rr.RankingRequest(entity_count=6, template="tech_spec_matrix",
                            force_ranking=True)
    meta = {"ranking": rr.RankingOutcome(plan=None, request=req,
                                         fallback_reason="").to_meta()}
    back = rr.RankingRequest.from_meta(meta)
    assert back is not None
    assert (back.entity_count, back.template, back.force_ranking) == \
           (6, "tech_spec_matrix", True)


def test_snapshot_stores_what_was_asked_not_what_was_achieved():
    """🔴 存"实际冻结了几家"会让"这次只凑到 4 家"在下次重做时变成"他要 4 家",一路缩水。"""
    req = rr.RankingRequest(entity_count=6)
    meta = rr.RankingOutcome(plan=None, request=req).to_meta()
    assert meta["request"]["entity_count"] == 6


def test_card_group_post_does_not_inherit_a_ranking():
    """反向对照:卡组型作品重做仍是卡组型,不许凭空长出一个榜单请求。"""
    assert rr.RankingRequest.from_meta({"model": "x"}) is None
    assert rr.RankingRequest.from_meta({"ranking": {"request": {}}}) is None
    assert rr.RankingRequest.from_meta(None) is None


# ===========================================================================
# ⑧ 窄 DTO:前端拿得到什么必须是一份可锁清单
# ===========================================================================

def _fat_meta() -> dict:
    return {"ranking": {
        "requested_form": "ranking", "effective_form": "ranking", "degraded": False,
        "template_id": "top3_provider", "target_engine": "豆包",
        "request": {"content_form": "ranking", "entity_count": 5,
                    "template": "top3_provider", "force_ranking": True},
        "frozen": {"contract_hash": "deadbeef",
                   "items": [{"display_name": "A",
                              "source": {"llm_model": "qwen3-max", "row_id": 42,
                                         "extractor_version": "v9"}}]}},
        "ranking_gates": [{"gate": "R1", "level": "A1", "message": "第 2 张对不上",
                           "card_indices": [2]}],
        "model": "deepseek-v4-flash"}


def test_narrow_dto_leaks_no_internal_field():
    blob = json.dumps(rr.ranking_summary(_fat_meta()), ensure_ascii=False)
    for k in rr._INTERNAL_ONLY_KEYS:
        assert k not in blob, f"窄 DTO 漏了内部字段 {k}"
    assert "deepseek" not in blob and "qwen" not in blob, "供应商名泄漏"


def test_narrow_dto_carries_what_the_dialog_needs():
    s = rr.ranking_summary(_fat_meta())
    assert s["content_form"] == "ranking"
    assert s["template"] == "top3_provider" and s["template_label"]
    assert s["entity_count"] == 5           # 用户要的
    assert s["entity_count_actual"] == 1    # 实际做出的
    assert s["gates"] and s["gates"][0]["card_indices"] == [2]


def test_narrow_dto_is_none_for_card_group():
    assert rr.ranking_summary({"model": "x"}) is None
    assert rr.ranking_summary(None) is None


def test_detail_endpoint_exposes_the_narrow_dto():
    src = API.read_text(encoding="utf-8")
    assert '"ranking": ranking_summary(post.get("generation_meta"))' in src


def strip_ts_comments(src: str) -> str:
    """剥掉 TS/TSX 的 `//` 与 `/* */`,**但不动字符串里的同形字符**。

    🔴 为什么非要有这个:形态锁打在原文上会撞到**我自己写的说明文字** ——
       这是本包家族第四次栽在同一处(前三次分别在 Python 源码、docstring、
       和"剥注释≠剥字符串")。所以下面配了一条自检:
       剥离器失效时那条形态锁会变成恒真,自检就是拦它的。
    """
    out: list = []
    i, n = 0, len(src)
    state = "code"      # code / line / block / sq / dq / tpl
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state, i = "line", i + 2
                continue
            if c == "/" and nxt == "*":
                state, i = "block", i + 2
                continue
            if c == "'":
                state = "sq"
            elif c == '"':
                state = "dq"
            elif c == "`":
                state = "tpl"
            out.append(c)
        elif state == "line":
            if c == "\n":
                state = "code"
                out.append(c)
        elif state == "block":
            if c == "*" and nxt == "/":
                state, i = "code", i + 2
                continue
        else:                       # 字符串内:原样保留,处理转义
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(src[i + 1])
                i += 2
                continue
            if (state == "sq" and c == "'") or (state == "dq" and c == '"') \
                    or (state == "tpl" and c == "`"):
                # 起始引号那一次已经在 code 分支写过,这里是收尾
                if len(out) >= 2:
                    state = "code"
        i += 1
    return "".join(out)


def test_the_ts_comment_stripper_actually_strips():
    """自检:剥离器不生效的话,下面那条形态锁就是恒真的。"""
    raw = TSX_DETAIL.read_text(encoding="utf-8")
    assert "前端**不读原始 generation_meta**" in raw, "样本句不在源码里,自检失去意义"
    stripped = strip_ts_comments(raw)
    assert "前端**不读原始 generation_meta**" not in stripped, "注释没被剥掉"
    # 反向:字符串字面量不许被误剥
    assert "'/api/geo-douyin/pricing'" in stripped, "把字符串当注释剥了"


def test_detail_page_reads_the_dto_not_raw_meta():
    """反向对照:前端不许绕过窄 DTO 去读原始 meta。"""
    src = TSX_DETAIL.read_text(encoding="utf-8")
    assert "detail?.ranking?.content_form" in src
    assert "generation_meta" not in strip_ts_comments(src), \
        "详情页又去读原始 generation_meta 了"


# ===========================================================================
# ⑨ 资料共用:如实,不许再写假的"同一个组件"
# ===========================================================================

def test_material_score_is_computed_by_m3_only():
    """🔴 两边**同一份计算**这一条是真的:8 字段完整度只有 m3 一处实现。"""
    src = CKB.read_text(encoding="utf-8")
    i = src.find("def load_display_materials")
    assert i > 0
    assert "_summarize_materials" in src[i: i + 700], "分数不再走 m3 的那一份"


# ── [WO_271] 退役:test_the_false_shared_component_claim_is_gone ──
# 锁的是旧入口页里那句假注释(「同一个组件、同一个端点、同一份计算」)与订正后的如实说明,
# 两句随页一起删了(6b491ab23),没有对象。「同一份计算」这件真事仍由上面
# test_material_score_is_computed_by_m3_only 与下面 test_client_knowledge_is_not_forked 锁住;
# 现役图文流不画知识库卡由必跑集 tests/test_geo_douyin_detail_ui.py::test_retired_image_note_pages_stay_deleted
# 守。登记:tests/RETIRED_TESTS.txt。


def test_client_knowledge_is_not_forked():
    """反向对照:没有人在图文侧另起一套资料计算。"""
    hits = [p for p in REPO.glob("services/**/*.py")
            if "_summarize_materials" in p.read_text(encoding="utf-8", errors="ignore")]
    assert [p.name for p in hits] == ["client_knowledge.py"], \
        f"资料计算出现了第二处实现:{[p.name for p in hits]}"


# ===========================================================================
# ⑩ 计费语义:幂等键是 task_ref,**不是** request_id
# ===========================================================================

def test_idempotency_key_is_task_ref():
    """🔴 工单原文写的是「相同 request_id 重放只产生一次任务和一次扣费」——
    本 API **没有 request_id 这个概念**,幂等键是 `task_ref`(表上有 UNIQUE)。
    照工单写会测一个不存在的东西,永远绿。"""
    from services.geo_douyin.production_task import build_task_ref
    a, b = build_task_ref(7), build_task_ref(7)
    assert a != b, "task_ref 不唯一,freeze/commit/release 会对错账"
    assert a.startswith("geo_douyin_post:7:")


def test_protected_files_untouched():
    """五保护文件零差异 —— 本包不碰计费/连接/鉴权。"""
    import subprocess
    out = subprocess.run(
        ["git", "diff", "--name-only", "00aa2bb1", "HEAD", "--",
         "middleware/billing.py", "db/wallet_db.py", "db/connection.py",
         "auth/middleware.py", "auth/jwt_utils.py"],
        cwd=str(REPO), capture_output=True, text=True)
    assert out.stdout.strip() == "", f"动了保护文件:{out.stdout}"


# ===========================================================================
# ⑪ 真库行为:四引擎不同名次 / 豆包缺席 / 人工名单收窄
# ===========================================================================

_R3_IND = "r3_engine_probe_ind"
_R3_IND_NODB = "r3_no_doubao_ind"


def _ensure_brand(conn, cur, brand_id: int, name: str) -> None:
    """造一行 `brands`(quotes.brand_id 的外键靶子),**按真实列形态来**。

    🔴 2026-08-08:全量批次里这句 `INSERT INTO brands (id, name)` 会炸
       `column "name" of relation "brands" does not exist` —— 批次中有别的模块
       把这张表重建成了另一套形态,于是**结果取决于测试执行顺序**:
       单独跑这个文件全绿,和那 50 个模块一起跑就 setup 报错。
       报错的 fixture = 它下面的锁一条都没跑 = 等于不设防,所以这里必须自适应。

    🔴 所以**不猜列名,查 information_schema**:把"必填且无默认值"的列全部补上,
       按 data_type 给最小合法值。两套形态下都能建出这一行,而且哪天再多一列
       NOT NULL 也不用改这里。(第一版我写的是 try/except 退回 `(id)`,
       当场撞上 `owner_user_id` NOT NULL —— 猜第二次照样错。)
    """
    # 🔴 必须用 `to_regclass` + `pg_attribute`,**不能**用 `information_schema.columns
    #    WHERE table_name='brands'` —— 这个测试库里有**不止一张** `brands`
    #    (不同 schema),按表名查会查到另一张,于是"查出来有 name、INSERT 说没有"。
    #    `to_regclass` 与 INSERT 走完全相同的 search_path 解析,这才是同一张表。
    cur.execute("""
        SELECT a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
               pg_get_expr(d.adbin, d.adrelid) AS column_default
          FROM pg_attribute a
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE a.attrelid = to_regclass('brands')
           AND a.attnum > 0 AND NOT a.attisdropped
    """)
    cols = {r["column_name"]: r for r in cur.fetchall()}

    # 🔴 必填**且带外键**的列不能随便塞 1 —— 值必须是被引用表里真实存在的一行
    #    (`owner_user_id=1` 当场撞 `brands_owner_user_id_fkey`:users 里没有 id=1)。
    cur.execute("""
        SELECT a.attname AS col, cl.relname AS ref_table, af.attname AS ref_col
          FROM pg_constraint c
          JOIN pg_attribute a  ON a.attrelid  = c.conrelid  AND a.attnum  = c.conkey[1]
          JOIN pg_class     cl ON cl.oid      = c.confrelid
          JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = c.confkey[1]
         WHERE c.conrelid = to_regclass('brands') AND c.contype = 'f'
    """)
    fks = {r["col"]: (r["ref_table"], r["ref_col"]) for r in cur.fetchall()}

    fill = {"id": brand_id}
    if "name" in cols:
        fill["name"] = name
    for cname, meta in cols.items():
        if cname in fill or meta["is_nullable"] == "YES" or meta["column_default"]:
            continue
        if cname in fks:
            ref_t, ref_c = fks[cname]
            cur.execute(f'SELECT "{ref_c}" AS v FROM "{ref_t}" ORDER BY 1 LIMIT 1')
            row = cur.fetchone()
            if not row:
                pytest.skip(f"{ref_t} 是空表,造不出合法的 brands.{cname} —— "
                            f"跳过而不是塞个假值把外键撞红")
            fill[cname] = row["v"]
            continue
        t = str(meta["data_type"])
        if "int" in t or "numeric" in t or "double" in t or "real" in t:
            fill[cname] = 1
        elif t == "boolean":
            fill[cname] = False
        elif "json" in t:
            fill[cname] = "[]"
        elif "timestamp" in t or "date" in t:
            continue          # 这类基本都有默认值;真没有就让它报出来,别静默塞值
        else:
            fill[cname] = name

    names = ", ".join(fill)
    holes = ", ".join(["%s"] * len(fill))
    cur.execute(f"INSERT INTO brands ({names}) VALUES ({holes}) "
                "ON CONFLICT (id) DO NOTHING", tuple(fill.values()))


def _seed(industry: str, rows, base_fact_id: int, hseed: str) -> None:
    """rows = [(engine, entity_name, entity_key, rank), ...]。**幂等**。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        engines = sorted({r[0] for r in rows})
        fid = {}
        for k, eng in enumerate(engines):
            fid[eng] = base_fact_id + k
            cur.execute("""
                INSERT INTO geo_research_answer_facts
                    (id, raw_id, industry_key, query, engine, batch_id,
                     answer_hash, extractor_version)
                VALUES (%s, %s, %s, '深圳载货电梯哪家好', %s, 'r3_probe', %s, 'v1')
                ON CONFLICT DO NOTHING
            """, (fid[eng], base_fact_id + k, industry, eng,
                  (hseed + str(k))[:1] * 32))
        for eng, name, key, rank in rows:
            cur.execute("""
                INSERT INTO geo_research_answer_entities
                    (answer_fact_id, raw_id, industry_key, engine, entity_name,
                     entity_key, entity_type, recommendation_rank,
                     evidence_phrases, recommendation_reasons,
                     confidence, llm_model, extractor_version)
                VALUES (%s, %s, %s, %s, %s, %s, 'brand', %s,
                        '[]', '[]', 1.0, 'm', 'v1')
                ON CONFLICT (answer_fact_id, entity_key) DO NOTHING
            """, (fid[eng], base_fact_id, industry, eng, name, key, rank))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded_engines():
    # 同一家在四个引擎里名次各不相同 —— 判据:必须引豆包那一行
    _seed(_R3_IND, [("豆包", "四引擎探针甲", "me_r3a", 2),
                    ("Kimi", "四引擎探针甲", "me_r3a", 7),
                    ("DeepSeek", "四引擎探针甲", "me_r3a", 5),
                    ("千问", "四引擎探针甲", "me_r3a", 9)], 990301, "p")
    # 豆包缺席:只有 Kimi / DeepSeek
    _seed(_R3_IND_NODB, [("Kimi", "无豆包探针乙", "me_r3b", 3),
                         ("DeepSeek", "无豆包探针乙", "me_r3b", 4)], 990311, "q")
    yield


def test_doubao_rank_wins_when_four_engines_disagree(seeded_engines):
    """🔴 工单 §六-4:四引擎名次各不相同 → 只有豆包那一个进榜单。

    修前会按"名次最小"取(豆包第 2 恰好也最小),所以判据**必须**用
    豆包不是最小的那一组才有判别力 —— 这里豆包=2、DeepSeek=5、Kimi=7、千问=9,
    最小的就是豆包……那就再加一条:把偏好关掉时结果必须**不同**。
    """
    pool = rs.fetch_ranking_candidates(_R3_IND, prefer_engine=rr.TARGET_ENGINE)
    c = next(x for x in pool if x["entity_name"] == "四引擎探针甲")
    assert c["source"]["engine"] == "豆包", c["source"]["engine"]
    assert c["best_rank"] == 2
    assert c["source"]["from_preferred_engine"] is True
    # 反向面:共识面**没被收窄** —— 四个引擎都还在
    assert set(c["engines"]) == {"豆包", "Kimi", "DeepSeek", "千问"}
    assert c["engine_count"] == 4


def test_preference_is_what_picks_doubao_not_the_rank_order(seeded_engines):
    """🔴 判别力自检:如果不是"偏好"在起作用,而是"名次最小"恰好命中豆包,
    这条锁就没有判别力。这里造一个**豆包名次最差**的实体来分开两者。"""
    # 🔴 自己的 fact id 段。复用上一组的 base 会让 `engines` 排序不同 →
    #    fid 映射错位 → 实体挂到别的引擎的 fact 上,而 ON CONFLICT 把错误吞掉。
    _seed(_R3_IND, [("豆包", "偏好判别丙", "me_r3c", 9),
                    ("Kimi", "偏好判别丙", "me_r3c", 1)], 990321, "r")
    with_pref = rs.fetch_ranking_candidates(_R3_IND, prefer_engine=rr.TARGET_ENGINE)
    without = rs.fetch_ranking_candidates(_R3_IND)
    a = next(x for x in with_pref if x["entity_name"] == "偏好判别丙")
    b = next(x for x in without if x["entity_name"] == "偏好判别丙")
    assert a["source"]["engine"] == "豆包" and a["best_rank"] == 9, a["source"]
    assert b["source"]["engine"] == "Kimi" and b["best_rank"] == 1, b["source"]


def test_falls_back_to_another_engine_honestly_when_doubao_absent(seeded_engines):
    """🔴 工单 §六-5 的正确形态:豆包没提到这一家时,**不冒充豆包**,
    如实写出是哪个引擎 —— 而不是整条失败。"""
    from services.geo_douyin.ranking_payload import AggregationContract, RankingItem, rank_statement

    pool = rs.fetch_ranking_candidates(_R3_IND_NODB, prefer_engine=rr.TARGET_ENGINE)
    c = next(x for x in pool if x["entity_name"] == "无豆包探针乙")
    assert c["source"]["engine"] in ("Kimi", "DeepSeek"), c["source"]["engine"]
    assert c["source"]["from_preferred_engine"] is False
    said = rank_statement(RankingItem(rank=1, display_name=c["entity_name"],
                                      source=c["source"]),
                          AggregationContract(query="Q"))
    assert "豆包" not in said, f"豆包没提到却说成豆包:{said}"
    assert c["source"]["engine"] in said


# ===========================================================================
# ⑫ 行为判据 —— 补上"结构锁抓不到"的那 7 条
#
# 🔴 第一轮变异把这 7 条全放活了,病根一句话:**我写的是结构锁**
#    (AST 里有没有这个 kwarg / 源码里有没有这个字符串),而变异改的是**取值**。
#    `prefer_engine=""` 照样有这个 kwarg;`if False:` 之后那段死代码里的
#    字符串照样在源码里。结构锁只能证明"接了线",证不了"线上有电"。
# ===========================================================================

_BEH_IND = "r3_behavior_ind"


@pytest.fixture(scope="module")
def seeded_behavior():
    """4 家 x 2 引擎(豆包 + Kimi),豆包给的名次**故意比 Kimi 差** ——
    这样"按偏好取"与"按名次最小取"两种实现会给出不同答案。"""
    rows = []
    for i, name in enumerate(["行为甲电梯", "行为乙电梯", "行为丙电梯", "行为丁电梯"]):
        rows.append(("豆包", name, f"me_beh{i}", 8 - i))
        rows.append(("Kimi", name, f"me_beh{i}", 1 + i))
    _seed(_BEH_IND, rows, 990401, "s")
    yield


def test_behavior_orchestrator_really_prefers_doubao(seeded_behavior):
    """① 变异「不传偏好」必须在这里转红。"""
    out = rr.build_ranking_plan(industry_key=_BEH_IND, keyword="Q",
                                city="深圳", client_brand="", want=4, card_budget=6,
                                force_ranking=True)
    assert out.plan is not None, out.fallback_reason
    engines = {it.source.get("engine") for it in out.plan.payload.items}
    assert engines == {"豆包"}, f"名次不是从豆包那一行取的:{engines}"


def test_behavior_snapshot_keeps_the_requested_count(seeded_behavior):
    """② 变异「快照存 0」必须在这里转红 —— 走真编排,不是手搓 RankingRequest。"""
    out = rr.build_ranking_plan(industry_key=_BEH_IND, keyword="Q", want=4,
                                card_budget=9, force_ranking=True)
    assert out.to_meta()["request"]["entity_count"] == 4


def test_behavior_card_budget_actually_degrades(seeded_behavior):
    """④ 变异「装不下也硬做」必须在这里转红。"""
    out = rr.build_ranking_plan(industry_key=_BEH_IND, keyword="Q", want=4,
                                card_budget=4, force_ranking=True)   # 4 张 -> 只剩 2 个卡位
    assert out.plan is None
    assert out.fallback_reason == rr.FALLBACK_CARD_BUDGET, out.fallback_reason
    # 反向面:张数够时不许误报
    ok = rr.build_ranking_plan(industry_key=_BEH_IND, keyword="Q", want=4,
                               card_budget=6, force_ranking=True)
    assert ok.plan is not None
    assert ok.fallback_reason != rr.FALLBACK_CARD_BUDGET, ok.fallback_reason


def test_behavior_entity_ref_reaches_the_cards(seeded_behavior, monkeypatch):
    """③ 变异「不走构造式职责表」必须在这里转红。

    真跑 `generate_image_post_content`(LLM 打桩),断言产出的每张内容卡
    都带着**代码指派**的 `entity_ref`,且与冻结名单逐一对应。
    """
    import services.geo_douyin.content_generator as cg

    plan = rr.build_ranking_plan(industry_key=_BEH_IND, keyword="深圳载货电梯哪家好",
                                 want=4, card_budget=6, force_ranking=True).plan
    assert plan is not None
    names = [it.display_name for it in plan.payload.items]

    async def _fake_llm(prompt, **kw):
        # 模型**故意不填** entity_ref —— 这正是要堵的那条路
        cards = [{"entity": f"卡{i}", "one_liner": "L", "points": ["a", "b"],
                  "metric": "", "caveat": "注意"} for i in range(len(names))]
        return json.dumps({"body": "我去看了一圈,把几家放一起比了比。行为客户 也在里面。",
                           "cover": {"title": "T", "subtitle": "S"},
                           "cards": cards,
                           "closing": {"headline": "H", "summary": "M", "caveat": "C",
                                       "brand_line": "行为客户 值得看"},
                           "title": "深圳载货电梯怎么选 实测对比,报价与工期一次说清",
                           "hashtags": ["a", "b", "c", "d", "e"]}, ensure_ascii=False)

    monkeypatch.setattr(cg, "_call_llm", _fake_llm, raising=True)

    class _Ctx:
        brand_name = "行为客户"
        sources_used: list = []
        has_real_photo = False

        def to_prompt_block(self):
            return ""

    async def _no_ctx(*a, **k):
        return _Ctx()

    monkeypatch.setattr(cg, "build_brand_context", _no_ctx, raising=True)

    out = asyncio.run(cg.generate_image_post_content(
        "深圳载货电梯哪家好", brand_name="行为客户", card_count=6,
        ranking_plan=plan))
    assert out.ok, out.error
    refs = [c.get("entity_ref") for c in out.cards]
    assert refs == names[:len(refs)], f"卡上的声明不是代码指派的:{refs} vs {names}"
    assert all(refs), "有卡没拿到 entity_ref —— 省略即绕过那条路又开了"


# -- 人工确认竞品:真库行为(567)-------------------------------------
_BEH_BRAND = 990501
_BEH_KW = "行为血缘词"


@pytest.fixture(scope="module")
def seeded_quotes():
    """两张报价:一张 real(带甲/乙),一张 fictional(带编造的丙)。
    再挂一条 confirmed_keywords 血缘,只指向 real 那一张。"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 quotes.brand_id 有外键 → 必须先有 brands 行。
        #    (SQL 4 维核验的第 3 维:字段归属/约束,这条我漏了。)
        _ensure_brand(conn, cur, _BEH_BRAND, "行为客户")
        cur.execute(
            "INSERT INTO quotes (id, brand_id, status, competitor_mode, "
            "competitor_list, updated_at) VALUES "
            "(%s, %s, 'confirmed', 'real', %s, NOW()), "
            "(%s, %s, 'confirmed', 'fictional', %s, NOW()) "
            # 🔴 DO UPDATE 不是 DO NOTHING:这一行是**上几轮跑留下的**,
            #    DO NOTHING 会让新加的画像样本悄悄写不进去,判据当场变假红。
            "ON CONFLICT (id) DO UPDATE SET "
            "  competitor_list = EXCLUDED.competitor_list, "
            "  competitor_mode = EXCLUDED.competitor_mode, "
            "  status = EXCLUDED.status",
            (990601, _BEH_BRAND,
             json.dumps([{"name": "行为甲电梯", "verify_source": "http://x",
                          "metaso_verified": True, "confirmed_by": ["Doubao"],
                          "source_count": 2,
                          # B 层:可引用要求逐条 name_verified(生产 394 条里 64 条带)
                          "name_verified": True,
                          # A 层样本:这段画像必须能一路走到 prompt,且不许被抄上卡面
                          "profile": "行为甲电梯隶属行为甲集团，主营高速客梯与自动扶梯，"
                                     "核心优势为自研永磁同步曳引机与 99.2% 准点交付率。",
                          "confidence": "high"},
                         {"name": "行为乙电梯", "verify_source": "http://y",
                          "metaso_verified": True, "confirmed_by": ["Kimi"],
                          "source_count": 1}], ensure_ascii=False),
             990602, _BEH_BRAND,
             json.dumps([{"name": "编造丙集团", "verify_source": "",
                          "metaso_verified": False, "confirmed_by": []}],
                        ensure_ascii=False)))
        cur.execute(
            # 🔴 monitoring_product_version 是 **NOT NULL + 默认值 + 外键**三者叠加:
            #    置 NULL 撞非空,用默认值撞外键 -> 只能先把被引用的那一行造出来。
            #    (SQL 4 维核验第 3 维「字段归属/约束」,我连踩两次才查 \d。)
            "INSERT INTO monitoring_product_platform_matrices (version, platforms) "
            "VALUES ('monitoring-unified5-v1', '[]') ON CONFLICT (version) DO NOTHING")
        cur.execute(
            "INSERT INTO confirmed_keywords "
            "(id, quote_id, keyword, brand_id, status) "
            "VALUES (990701, 990601, %s, %s, 'confirmed') "
            "ON CONFLICT (id) DO NOTHING",
            (_BEH_KW, _BEH_BRAND))
        conn.commit()
    finally:
        conn.close()
    yield


def test_behavior_fictional_quote_never_enters_the_allowlist(seeded_quotes):
    """56 的前提:编造模式那张报价**一条都不许出来**。"""
    from services.client_knowledge import load_confirmed_competitors
    got = {c["display_name"] for c in load_confirmed_competitors(_BEH_BRAND)}
    assert "编造丙集团" not in got, "编造竞品进了人工确认名单"
    assert {"行为甲电梯", "行为乙电梯"} <= got


def test_behavior_lineage_narrows_by_keyword(seeded_quotes):
    """5 变异「血缘退回按品牌取最近一单」必须在这里转红。"""
    from services.client_knowledge import load_confirmed_competitors
    hit = load_confirmed_competitors(_BEH_BRAND, [_BEH_KW])
    miss = load_confirmed_competitors(_BEH_BRAND, ["这个词没挂在任何报价上"])
    assert hit, "血缘命中的词反而取不到名单"
    assert miss == [], "不相干的词也取到了名单(血缘没生效)"


def test_behavior_entity_id_joins_the_evidence_table(seeded_quotes):
    """7 变异「entity_id 用原名」必须在这里转红 —— 键空间对不上就 join 不了。"""
    from services.client_knowledge import load_confirmed_competitors
    from services.research_monitor.answer_entity_extractor import build_answer_entity_key
    got = {c["display_name"]: c["entity_id"] for c in load_confirmed_competitors(_BEH_BRAND)}
    assert got
    for name, eid in got.items():
        assert eid == build_answer_entity_key(name), f"{name} 的 id 不在证据表键空间里"
        assert eid.startswith("me_"), eid


def test_behavior_confirmed_list_that_cannot_align_falls_back(seeded_quotes, seeded_behavior):
    """🔴 2026-08-08 改判:名单命中不足**不再整单降级**,回落全量池 + 留痕。

    人工名单里的甲/乙在候选池里能找到,但只有 2 家,低于最少 3 家。
    旧行为是 `_give_up(confirmed_list_lacks_evidence)`;生产实证
    (quote 386 / home_improvement)证明这条会误伤:A 源写「好莱客**全屋定制**」、
    候选池写「好莱客」,同一家两种写法,精确键 12 家只命中 3 家 ——
    把索菲亚/好莱客/欧派这些 4 引擎第 1 名的全砍在门外。
    """
    out = rr.build_ranking_plan(industry_key=_BEH_IND, keyword=_BEH_KW,
                                want=4, card_budget=6, force_ranking=True,
                                client_brand_id=_BEH_BRAND)
    assert out.plan is not None, f"对不齐就把整单降级了:{out.fallback_reason}"
    audit = out.plan.to_meta()["confirmed_allowlist"]
    assert audit["applied"] is False, "命中不足却仍施加了上界"
    assert audit["size"] == 2 and audit["hits"] == 2, audit
    # 反向面:回落用的是**全量池**(4 家),不是名单命中的那 2 家 ——
    # 少了这一条,"回落"可能只是把 2 家原样放行,判据分不出来。
    assert len(out.plan.payload.items) == 4, [it.display_name for it in out.plan.payload.items]
    assert {"行为丙电梯", "行为丁电梯"} <= {it.display_name for it in out.plan.payload.items}


def test_behavior_upper_bound_still_applies_when_it_can_align(seeded_behavior):
    """成对反向:名单**够得着**时上界一个字没松 —— 只收窄,不扩写。

    没有这一条,上面那条"回落"就可能是把上界整个删掉的假绿。
    """
    from db.connection import get_connection
    brand, quote = 990511, 990611
    conn = get_connection()
    try:
        cur = conn.cursor()
        # 🔴 `brands` 的列是 `name`,不是 `brand_name`(`brand_name` 在 `quotes` 上)。
        #    同一个坑今天已经栽过一次(生产探针报过 `b.brand_name does not exist`),
        #    还是照抄了记忆里的列名 —— SQL 4 维第 1 维要查 schema,不是回忆。
        _ensure_brand(conn, cur, brand, "对齐客户")
        cur.execute(
            "INSERT INTO quotes (id, brand_id, status, competitor_mode, "
            "competitor_list, updated_at) VALUES (%s, %s, 'confirmed', 'real', %s, NOW()) "
            "ON CONFLICT (id) DO NOTHING",
            (quote, brand,
             json.dumps([{"name": n, "verify_source": "http://z",
                          "metaso_verified": True, "confirmed_by": ["Doubao"],
                          "source_count": 2}
                         for n in ("行为甲电梯", "行为乙电梯", "行为丙电梯")],
                        ensure_ascii=False)))
        cur.execute("INSERT INTO monitoring_product_platform_matrices (version, platforms) "
                    "VALUES ('monitoring-unified5-v1', '[]') ON CONFLICT (version) DO NOTHING")
        cur.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, brand_id, status) "
                    "VALUES (990711, %s, '对齐血缘词', %s, 'confirmed') "
                    "ON CONFLICT (id) DO NOTHING", (quote, brand))
        conn.commit()
    finally:
        conn.close()

    out = rr.build_ranking_plan(industry_key=_BEH_IND, keyword="对齐血缘词",
                                want=4, card_budget=6, force_ranking=True,
                                client_brand_id=brand)
    assert out.plan is not None, out.fallback_reason
    audit = out.plan.to_meta()["confirmed_allowlist"]
    assert audit["applied"] is True, f"名单够得着却没施加上界:{audit}"
    got = {it.display_name for it in out.plan.payload.items}
    assert "行为丁电梯" not in got, f"名单外的公司进了榜:{got}"
    assert got == {"行为甲电梯", "行为乙电梯", "行为丙电梯"}, got


# ===========================================================================
# ⑬ 行业键归一(2026-08-08 P0)—— 上线后第一张真实榜单请求就死在这里
#
# 🔴 病根与本文件 ① 的引擎名是**同一类**:两张表用两套键空间,直传不报错、
#    只是恒空。所以判据也照 ① 的写法:拿**生产实值**做样本,不用我编的样例。
# ===========================================================================

#: 逐字复制自生产 `geo_douyin_posts.id=20`(2026-08-08 只读取证)。
_PROD_FREE_TEXT_INDUSTRY = (
    "家居制造业 / 高端整木全屋定制/木作高定行业"
    "（住宅室内木作整装、实木定制家具设计生产安装一体化服务）"
)
#: 候选池实际使用的受控枚举(生产 17 个键之一,家装是货最多的那个)。
_PROD_ENUM_INDUSTRY = "home_improvement"


@pytest.fixture(scope="module")
def seeded_home_improvement():
    """把候选种在**真实枚举键**上,再拿**真实自由文本**去取 —— 这一对就是生产现场。"""
    rows = []
    for i, name in enumerate(["归一探针甲", "归一探针乙", "归一探针丙", "归一探针丁"]):
        rows.append(("豆包", name, f"me_norm{i}", 1 + i))
        rows.append(("Kimi", name, f"me_norm{i}", 2 + i))
    _seed(_PROD_ENUM_INDUSTRY, rows, 990801, "n")
    yield


def test_behavior_free_text_industry_now_finds_the_pool(seeded_home_improvement):
    """🔴 本包 P0 本体:自由文本行业必须能取到候选。

    修之前 `WHERE e.industry_key = '<自由文本>'` 命中 0 行且**不报错**,
    上层照着空池子说「这个行业还没攒够可以点名的同行数据」——
    而家装是池子里货最多的行业。说错比不说更坏。
    """
    names = {c["entity_name"] for c in rs.fetch_ranking_candidates(_PROD_FREE_TEXT_INDUSTRY)}
    assert "归一探针甲" in names, "自由文本行业仍然取不到候选(归一没生效)"


def test_behavior_enum_key_is_unaffected(seeded_home_improvement):
    """成对反向:归一是**幂等**的 —— 不许把现在能取到的打坏。

    生产池 17 个键实测全部幂等;这里用其中一个做运行时复核。
    """
    a = rs.fetch_ranking_candidates(_PROD_ENUM_INDUSTRY)
    b = rs.fetch_ranking_candidates(_PROD_FREE_TEXT_INDUSTRY)
    assert a, "枚举键本身取不到候选 —— 这条自检失去意义"
    assert [c["entity_key"] for c in a] == [c["entity_key"] for c in b], \
        "枚举键与自由文本取到的不是同一批"


def test_behavior_unrelated_industry_is_not_forced_into_a_bucket(seeded_home_improvement):
    """成对反向:不认识的行业**不许**被硬塞进某个枚举 ——
    硬塞的后果是家装客户拿到别的行业的同行名单,比取不到严重得多。"""
    assert rs.fetch_ranking_candidates("般若波罗蜜多") == []


def test_behavior_normalized_key_flows_downstream(seeded_home_improvement):
    """🔴 只在取数处归一**不够**:`route_template`(→`default_prefers_ranking`)
    与冻结件也按枚举键查表。变异「只在 fetch 里归一、往下游仍传自由文本」
    必须在这里转红。"""
    out = rr.build_ranking_plan(industry_key=_PROD_FREE_TEXT_INDUSTRY, keyword="Q",
                                want=4, card_budget=6, force_ranking=True)
    assert out.plan is not None, out.fallback_reason
    assert out.plan.payload.industry_key == _PROD_ENUM_INDUSTRY, \
        f"冻结件记的还是自由文本:{out.plan.payload.industry_key!r}"


def test_behavior_normalization_is_not_a_noop_on_this_sample():
    """判别力自检:样本必须**真的**需要归一。

    若哪天 `normalize_industry_key` 对这句自由文本原样返回,上面几条就成了恒真。
    """
    from services.media_entity_flywheel import normalize_industry_key
    assert normalize_industry_key(_PROD_FREE_TEXT_INDUSTRY) == _PROD_ENUM_INDUSTRY
    assert _PROD_FREE_TEXT_INDUSTRY != _PROD_ENUM_INDUSTRY


def test_behavior_no_confirmed_list_falls_through_to_research(seeded_behavior):
    """反向对照:**没有**人工名单时,链路要自然落到 监测/研究 ——
    生产 392 张报价里只有 51 张模式为真且带名单,降级链才是主路径。"""
    out = rr.build_ranking_plan(industry_key=_BEH_IND, keyword="Q",
                                want=4, card_budget=6, force_ranking=True,
                                client_brand_id=999999)   # 这个品牌没有任何报价
    assert out.plan is not None, out.fallback_reason
    assert len(out.plan.payload.items) == 4


@pytest.fixture(scope="module")
def seeded_education():
    """种在 `education`(实测榜单**为正**的行业)—— 判别力必须靠 POSITIVE 行业。"""
    rows = []
    for i, name in enumerate(["归一教育甲", "归一教育乙", "归一教育丙", "归一教育丁"]):
        rows.append(("豆包", name, f"me_edu{i}", 1 + i))
        rows.append(("Kimi", name, f"me_edu{i}", 2 + i))
    _seed("education", rows, 990901, "e")
    yield


def test_behavior_industry_default_reads_the_normalized_key(seeded_education):
    """🔴 判别力自检的产物:第一轮变异这条**存活**了,病根是我选错样本。

    `home_improvement` 在 `_CAPTION_RANKING_NEGATIVE` 里 —— 归一前(自由文本落空)
    与归一后(命中 NEGATIVE)`default_prefers_ranking` **都返 False**,
    两条路撞在同一个答案上,锁看着全绿却什么都没证明。
    必须拿 POSITIVE 行业(education)才分得开。
    """
    out = rr.build_ranking_plan(industry_key="教育培训机构招生", keyword="Q",
                                client_brand="归一教育甲",
                                client_position={"best_rank": 2},
                                want=4, card_budget=6)   # 不传 force_ranking,让行业默认说话
    assert out.plan is not None, out.fallback_reason
    assert out.plan.routed["form"] == rr.FORM_RANKING, out.plan.routed["form"]
    assert out.fallback_reason == rr.FALLBACK_NONE, out.fallback_reason


def test_behavior_negative_industry_still_defaults_away_from_ranking(seeded_home_improvement):
    """成对反向:家装实测榜单效果为负,归一之后必须**仍然**默认不走榜单。

    防的是"归一顺手把 NEGATIVE 那两个行业也放行了" —— 那会把一条实测结论抹掉。
    """
    out = rr.build_ranking_plan(industry_key=_PROD_FREE_TEXT_INDUSTRY, keyword="Q",
                                client_brand="归一探针甲",
                                client_position={"best_rank": 2},
                                want=4, card_budget=6)
    assert out.plan is not None, out.fallback_reason
    assert out.plan.routed["form"] != rr.FORM_RANKING
    assert out.fallback_reason == rr.FALLBACK_INDUSTRY_DEFAULT, out.fallback_reason


# ===========================================================================
# ⑭ 竞品画像进内容创作 · A 层(2026-08-08)
#
# 🔴 A 层的全部安全论证是「只当选材背景,不外露原句」。所以判据分两半:
#      前一半证明画像**真的到得了** prompt(此前它在读取层就被丢掉);
#      后一半证明"不许照抄"**是被检查的**,不只是 prompt 里的一句要求。
#    少了后一半,这一层就是拿别人家没核验的宣称在赌。
# ===========================================================================

def test_loader_carries_the_profile_out(seeded_quotes):
    """🔴 病根本体:画像此前在 `load_confirmed_competitors` 这一层就被丢了。"""
    from services.client_knowledge import load_confirmed_competitors
    got = {c["display_name"]: c for c in load_confirmed_competitors(_BEH_BRAND)}
    assert "行为甲电梯" in got
    prof = got["行为甲电梯"].get("profile") or ""
    assert "永磁同步曳引机" in prof, f"画像没带出来:{got['行为甲电梯']}"
    # 反向面:分级信号也要在(B 层要用它分级;A 层不用但不能丢)
    assert got["行为甲电梯"]["evidence"]["confidence"] == "high"
    assert got["行为甲电梯"]["evidence"]["source_count"] == 2


def test_profile_reaches_the_entity_card_prompt():
    """画像真的进了那一张卡的 prompt,而且**按名字对上号**(不是随便贴一段)。"""
    names = ["通力电梯", "日立电梯", "快意电梯"]
    from services.geo_douyin.ranking_payload import safe_merge_key
    profiles = {safe_merge_key("通力电梯"):
                {"text": "通力电梯主营高速客梯,核心优势是自研曳引机。", "citable": False}}
    block = sp.ranking_roles_prompt_block(5, names, ["在 豆包 回答「Q」时列第 1"] * 3,
                                          profiles=profiles)
    assert "自研曳引机" in block, "画像没进 prompt"
    # 反向面:没有画像的那两家不许被安上别人的背景
    i_ok = block.index("通力电梯")
    assert block.count("自研曳引机") == 1, "同一段背景贴到了多张卡上"
    assert i_ok >= 0


def test_prompt_forbids_copying_and_truncates():
    """两条护栏:①明写不许照抄 ②截断(整段给出去等于邀请模型搬运)。"""
    from services.geo_douyin.ranking_payload import safe_merge_key
    long_bg = "甲" * 400
    block = sp.ranking_roles_prompt_block(
        5, ["通力电梯", "日立电梯", "快意电梯"], ["S"] * 3,
        profiles={safe_merge_key("通力电梯"): {"text": long_bg, "citable": False}})
    assert "一个字都不许照抄进卡面" in block
    assert "甲" * sp.PROFILE_CONTEXT_CHARS in block
    assert "甲" * (sp.PROFILE_CONTEXT_CHARS + 1) not in block, "没截断"


def test_no_profile_no_extra_instruction():
    """成对反向:没有画像时不许凭空多出一段"不许照抄"的话 —— 那是噪声。"""
    block = sp.ranking_roles_prompt_block(5, ["通力电梯", "日立电梯", "快意电梯"],
                                          ["S"] * 3)
    assert "一个字都不许照抄进卡面" not in block


def test_r9_catches_verbatim_copy():
    """🔴 A 层安全论证的落点:整段搬运必须被抓到。"""
    import services.geo_douyin.ranking_gates as g
    from services.geo_douyin.ranking_payload import safe_merge_key
    bg = "通力电梯核心优势为自研永磁同步曳引机与 99.2% 准点交付率。"
    slots = [{"entity_ref": "通力电梯", "entity": "通力电梯",
              "body": "选梯要看什么?通力电梯核心优势为自研永磁同步曳引机与 99.2% 准点交付率。"}]
    f = g.r9_no_verbatim_profile(
        slots, {safe_merge_key("通力电梯"): {"text": bg, "citable": False}})
    assert f is not None, "整段抄了却没抓到"
    assert f.to_dict()["card_indices"] == [1]
    assert "通力电梯" in f.to_dict()["message"]


def test_r9_does_not_fire_on_normal_cards():
    """成对反向 ①:自己写的卡面不许被误报。"""
    import services.geo_douyin.ranking_gates as g
    from services.geo_douyin.ranking_payload import safe_merge_key
    bg = "通力电梯核心优势为自研永磁同步曳引机与 99.2% 准点交付率。"
    slots = [{"entity_ref": "通力电梯", "entity": "通力电梯",
              "body": "这家胜在梯速与维保响应,老楼加装要先看井道尺寸能不能装得下。"}]
    assert g.r9_no_verbatim_profile(
        slots, {safe_merge_key("通力电梯"): {"text": bg, "citable": False}}) is None


def test_r9_tolerates_industry_common_phrases():
    """成对反向 ②:行业通用词组的自然重合不算搬运(否则闸会天天误报)。"""
    import services.geo_douyin.ranking_gates as g
    from services.geo_douyin.ranking_payload import safe_merge_key
    bg = "某某全屋定制隶属上市集团,主营全屋定制与整家定制,环保标准高。"
    slots = [{"entity_ref": "通力电梯", "entity": "通力电梯",
              "body": "主营全屋定制的这几家,报价口径差得远。"}]   # 重合「主营全屋定制」6 字
    assert g.r9_no_verbatim_profile(
        slots, {safe_merge_key("通力电梯"): {"text": bg, "citable": False}}) is None


def test_r9_is_wired_into_run_gates_and_the_paid_chain():
    """🔴 闸接上了 ≠ 判据接上了:`entity_profiles` 不从付费链传进来,
    R9 拿到空 dict 会直接 return None —— 闸在、判据不在。这条打的是**接线**。"""
    import inspect
    import services.geo_douyin.ranking_gates as g
    assert "entity_profiles" in inspect.signature(g.run_gates).parameters
    src = (REPO / "services" / "geo_douyin" / "production_task.py").read_text(encoding="utf-8")
    i = src.find("run_gates(")
    assert i > 0, "付费链没跑闸"
    assert "entity_profiles=" in src[i: i + 900], "付费链没把画像传给闸"


def test_generator_passes_profiles_to_the_prompt():
    """同上,另一条接线:画像要真从 plan 走到 prompt 构造处。

    🔴 第一版我写的是 `"profiles=" in src` —— 变异体改成 `profiles=None)` 时
       **照样含这个串**,当场存活。查"有没有这个参数"证明不了任何事,
       要查**值是什么**:必须是从 `ranking_plan` 取的,不能是常量。
    """
    tree = ast.parse(GEN.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "ranking_roles_prompt_block"]
    assert calls, "生成器没调职责表"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "profiles" in kw, "生成器没把画像传给职责表"
    val = kw["profiles"]
    assert not (isinstance(val, ast.Constant) and val.value is None), \
        "profiles 被写成了常量 None —— 参数在,值是空的"
    names = {n.id for n in ast.walk(val) if isinstance(n, ast.Name)}
    assert "ranking_plan" in names, f"画像不是从 plan 取的:{ast.dump(val)[:120]}"


def test_profiles_never_land_in_meta():
    """🔴 画像**不许**进 `generation_meta` —— 详情端点目前裸吐 meta,
    落进去就等于对外发布了一段我们自己综述的第三方描述。"""
    plan = rr.RankingPlan(payload=None, routed={}, statements=[],
                          profiles={"k": "一段画像"})
    assert "profiles" not in rr.RankingPlan.to_meta.__doc__ .lower() or True
    # 直接看真实输出:构造一个能 to_meta 的 plan
    class _P:
        def to_dict(self):
            return {}
    plan = rr.RankingPlan(
        payload=_P(),
        routed={"template_id": "t", "form": "ranking",
                "allows_ranking_wording": True, "has_client_evidence": True},
        statements=[], allowlist={}, profiles={"k": "一段画像"})
    meta = plan.to_meta()
    assert "profiles" not in meta
    assert "一段画像" not in json.dumps(meta, ensure_ascii=False)


def test_dead_comp_profiles_variable_is_gone():
    """`placement_service` 里那份 `comp_profiles` 收集了却零读取方 —— 已删。"""
    src = (REPO / "services" / "placement_service.py").read_text(encoding="utf-8")
    code = _py_code_only(src)
    assert "comp_profiles" not in code, "死变量回潮了"


# ===========================================================================
# ⑮ B 层 · 画像可引用(2026-08-08 · Owner 拍板)
#
# 🔴 门槛为什么不是 `source_count` —— 生产实测(394 条有画像的竞品条目):
#      verify_source 非空 390/394(不分级) · source_count>=2 仅 106 · name_verified 64
#    抽样 6 条有 4 条**根本不是公司**:「"环保防霉胶"并非单一品牌,而是指…」
#    —— 竞品识别把**品类词**当公司名去检索。而**源数最高那条(7 源)恰恰最不像公司**。
#    `source_count` 是检索命中量,不是可信度;拿它当门槛会正好放行最不该放行的。
# ===========================================================================

_REAL_ITEM = {"name": "欧派全屋定制", "name_verified": True, "source_count": 2,
              "verify_source": "http://news.example/x",
              "profile": "欧派全屋定制隶属欧派家居集团,主营橱柜、衣柜与整家定制。"}
#: 逐字取自生产(2026-08-08 只读取证)的品类型画像 —— 7 源、有链接,但主体不成立。
_CATEGORY_ITEM = {"name": "环保防霉胶", "name_verified": True, "source_count": 7,
                  "verify_source": "http://news.example/y",
                  "profile": "“环保防霉胶”并非单一品牌,而是指具有防霉、环保特性的密封胶类产品。"}


def test_citable_contract_uses_subject_not_source_count():
    from writing.competitor_name_contract import is_profile_citable
    assert is_profile_citable(_REAL_ITEM) is True
    # 🔴 判别力核心:7 源 + 有链接,仍然不可引用 —— 它根本不是一家公司
    assert is_profile_citable(_CATEGORY_ITEM) is False
    # 反向面:没过名字核验的一律不可引用
    assert is_profile_citable({**_REAL_ITEM, "name_verified": False}) is False
    # 反向面:没有画像就无所谓可不可引用
    assert is_profile_citable({"name": "x", "name_verified": True}) is False


def test_source_count_alone_would_have_been_the_wrong_gate():
    """把上面那条结论写成判据:若哪天有人把门槛改回 source_count,这条转红。"""
    from writing.competitor_name_contract import is_profile_citable
    assert _CATEGORY_ITEM["source_count"] > _REAL_ITEM["source_count"]
    assert is_profile_citable(_CATEGORY_ITEM) is False
    assert is_profile_citable(_REAL_ITEM) is True


def test_loader_reports_citability(seeded_quotes):
    from services.client_knowledge import load_confirmed_competitors
    got = {c["display_name"]: c for c in load_confirmed_competitors(_BEH_BRAND)}
    assert got["行为甲电梯"]["profile_citable"] is True, got["行为甲电梯"]
    # 反向面:没画像那条不可引用
    assert got["行为乙电梯"]["profile_citable"] is False


def test_prompt_wording_differs_for_citable():
    from services.geo_douyin.ranking_payload import safe_merge_key
    names = ["通力电梯", "日立电梯", "快意电梯"]
    cit = sp.ranking_roles_prompt_block(
        5, names, ["S"] * 3,
        profiles={safe_merge_key("通力电梯"): {"text": "通力电梯主营高速客梯。",
                                             "citable": True}})
    assert "公开资料(可以用里面的**事实**" in cit
    assert "不许整段照抄" in cit
    # 反向面:不可引用那一支仍是"一个字都不许照抄"
    non = sp.ranking_roles_prompt_block(
        5, names, ["S"] * 3,
        profiles={safe_merge_key("通力电梯"): {"text": "通力电梯主营高速客梯。",
                                             "citable": False}})
    assert "一个字都不许照抄进卡面" in non
    assert "公开资料(可以用里面的**事实**" not in non


def test_ad_law_terms_are_scrubbed_before_the_prompt():
    """🔴 绝对化用语在**进 prompt 之前**就剥掉 —— 少一次赌。

    生产实测 394 条画像里 27 条含这类词;词表走签发目录同源,本模块不自建。
    """
    from services.geo_douyin.ranking_payload import safe_merge_key
    from services.marketing.guards import legal_pack
    terms = [str(w) for w in (legal_pack().get("ad_law") or ()) if str(w or "")]
    assert terms, "签发目录取不到词 —— 这条判据失去意义"
    bad = terms[0]
    block = sp.ranking_roles_prompt_block(
        5, ["通力电梯", "日立电梯", "快意电梯"], ["S"] * 3,
        profiles={safe_merge_key("通力电梯"):
                  {"text": f"通力电梯是{bad}的电梯品牌,主营高速客梯。", "citable": True}})
    assert bad not in block, f"绝对化用语「{bad}」被原样递给了模型"
    assert "主营高速客梯" in block, "剥词把正常内容也剥没了"


def test_r9_lets_citable_profiles_through():
    """🔴 B 层的行为差:可引用的那几家,逐字重合**不再判**。

    合同已经允许把里面的事实写进卡面,再拿"逐字重合"判它就是自相矛盾的闸。
    约束改由 prompt 承担(用自己的话重写),与广告法同一档:主防 + 提示级。
    """
    import services.geo_douyin.ranking_gates as g
    from services.geo_douyin.ranking_payload import safe_merge_key
    bg = "通力电梯核心优势为自研永磁同步曳引机与准点交付率。"
    slots = [{"entity_ref": "通力电梯", "entity": "通力电梯", "body": "选梯要看什么?" + bg}]
    key = safe_merge_key("通力电梯")
    assert g.r9_no_verbatim_profile(slots, {key: {"text": bg, "citable": True}}) is None
    # 成对反向:同一段、同一张卡,不可引用时**必须**判 —— 证明上面那条不是恒 None
    f = g.r9_no_verbatim_profile(slots, {key: {"text": bg, "citable": False}})
    assert f is not None and f.to_dict()["card_indices"] == [1]


# ===========================================================================
# ⑯ 上游修复 · 画像自陈无主体 → 自动排除(2026-08-08)
#
# 🔴 之前修过两次,都没接住:`_sanitize_writing_competitors`(正则)与
#    `_sanitize_competitors_with_llm`(LLM 守门员)**都跑在画像生成之前** ——
#    不是失效,是那会儿证据还不存在。所以补在画像落地那一跳。
# ===========================================================================

#: 逐字取自生产(2026-08-08 只读取证)。两种形态,都要判无主体。
_PROD_CATEGORY_PROFILE = '“混凝土”并非单一品牌，而是一类建筑材料的统称。市场上有多家知名企业生产商品混凝土及相关产品。'
_PROD_NO_ENTITY_PROFILE = '经核查当前权威资料及知识库内容，未发现名为“Z建筑材料供应商”的具体品牌或企业信息'
#: 反向对照:真公司的画像(同样逐字取自生产)
_PROD_REAL_PROFILE = '「深企在线」隶属深圳市深企在线技术开发有限公司，主营官网定制、智能营销系统、SaaS工具及全网推广。'


def test_contract_covers_both_no_subject_forms():
    """🔴 我第一版只枚举了品类词,**「查无此企业」实测漏网** —— 枚举表必问全集缺口。"""
    from writing.competitor_name_contract import (profile_denies_the_entity,
                                                  profile_describes_a_category,
                                                  profile_lacks_subject)
    assert profile_describes_a_category(_PROD_CATEGORY_PROFILE) is True
    assert profile_denies_the_entity(_PROD_NO_ENTITY_PROFILE) is True
    # 两种形态各自都能让总判定为真
    assert profile_lacks_subject(_PROD_CATEGORY_PROFILE) is True
    assert profile_lacks_subject(_PROD_NO_ENTITY_PROFILE) is True
    # 🔴 成对反向:真公司画像**一个都不许命中**(误杀比漏杀贵 —— 会把真同行踢出榜)
    assert profile_lacks_subject(_PROD_REAL_PROFILE) is False
    assert profile_describes_a_category(_PROD_REAL_PROFILE) is False
    assert profile_denies_the_entity(_PROD_REAL_PROFILE) is False


def test_upstream_marks_no_subject_candidates_excluded():
    """🔴 接线:画像落地那一跳必须**当场判并标 excluded**。

    判据打在真出口上(server.py 那个循环),不是打在合同函数上 ——
    合同对不对与"有没有人调它"是两件事,本仓这条教训吃过多次。
    """
    src = (REPO / "server.py").read_text(encoding="utf-8")
    # 🔴 锚点打在**循环本身**,不打在段标题 + 定长窗口上:
    #    我第一版取 `Step 4` 往后 4200 字,结果被自己写的长注释挤出了窗口,
    #    判据当场假红。定长窗口是脆判据,锚点要贴着被判的代码。
    i = src.find("for comp, profile_text in zip(")
    assert i > 0, "找不到画像落地那一跳 —— 判据失效,先修判据"
    block = src[i: i + 1400]
    assert "profile_lacks_subject" in block, "画像落地后没判主体"
    assert 'comp["excluded"] = True' in block, "判了却没标排除"
    # 反向面:必须复用**既有** excluded 机制,不许新造一个平行字段
    assert 'comp["exclude_reason"]' in block
    # 反向面:判定必须来自合同,不许在 server.py 里另写一份词表。
    # 🔴 **必须剥注释**:我第一版直接查原文,当场撞到自己写的那段
    #    "模型答「…并非单一品牌…」"的说明 —— 同一个坑今天第二次。
    assert "并非单一品牌" not in _py_code_only(src), "在 server.py 里另立了第二份词表"


def test_ranking_chain_filters_excluded(seeded_quotes):
    """🔴 独立的第二个 bug:榜单链此前**不过滤 excluded**。

    文章链在过滤、前端在显示「N家已排除」,只有榜单链会把代理手工排除掉的
    竞品捞回来放进对外发布的榜单。生产 428 条里 28 条已被人工排除。
    """
    from db.connection import get_connection
    from services.client_knowledge import load_confirmed_competitors
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE quotes SET competitor_list = %s WHERE id = 990601",
            (json.dumps([
                {"name": "行为甲电梯", "name_verified": True, "source_count": 2,
                 "verify_source": "http://x", "metaso_verified": True,
                 "profile": "行为甲电梯隶属行为甲集团，主营高速客梯。"},
                {"name": "被排除的那家", "name_verified": True, "source_count": 2,
                 "verify_source": "http://y", "metaso_verified": True,
                 "excluded": True,
                 "profile": "被排除的那家隶属某集团，主营电梯。"},
            ], ensure_ascii=False),))
        conn.commit()
    finally:
        conn.close()
    got = {c["display_name"] for c in load_confirmed_competitors(_BEH_BRAND)}
    assert "行为甲电梯" in got, "正常那家反而没了"
    assert "被排除的那家" not in got, "代理手工排除的竞品被榜单链捞了回来"
