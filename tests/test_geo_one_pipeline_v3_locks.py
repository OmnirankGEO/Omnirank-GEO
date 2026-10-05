"""SSOT 锁 · 一条流水线 v3(2026-08-03 收尾包)

与 `test_geo_content_one_pipeline.py` / `test_geo_one_pipeline_v2_locks.py`
同属一套铁律。**SSOT §5 那张表的元锁扫整个 tests 目录**,所以拆文件不影响它。

覆盖本批四件:
  V1  蒸馏定价 130(Owner 08-03 拍板 · 由免费改收费)
  V2  §8.6-10 卡面 headline 与 caption 对齐(机器判据)
  V3  §8.6-11 卡面文字 OCR 逐字核验 + **要点文字必须真的进 prompt**
  V4  §8.8 画幅 3:4 / 9:16 客户自选
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "api" / "geo_douyin_api.py"
CONFIG = ROOT / "services" / "geo_douyin" / "config.py"
TEMPLATES = ROOT / "services" / "geo_douyin" / "card_templates.py"
PIPELINE = ROOT / "services" / "geo_douyin" / "image_pipeline.py"
REDRAW = ROOT / "services" / "geo_douyin" / "redraw.py"
DISTILLER = ROOT / "services" / "geo_douyin" / "topic_distiller.py"
DISTILL_TASK = ROOT / "services" / "geo_douyin" / "distill_task.py"
OCR = ROOT / "services" / "geo_douyin" / "ocr_qa.py"
MANIFEST = ROOT / "db" / "migration_manifest.py"
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23,09-08 #150 §4)。
# 读它的格改指现役 /writing/image-note 的后继文件、按后继内容重判;随整页消失的格原位退役,
# 登记在 tests/RETIRED_TESTS.txt(接替者写在那里)。
WRITING = ROOT / "frontend" / "src" / "pages" / "Writing"
TOPIC_PANEL = WRITING / "ImageNoteTopicPanel.tsx"   # 选词 / 蒸馏 / 开始制作
TOPICS = WRITING / "imageNoteTopics.ts"             # 行的形状(城市 / 状态 / 进度)
PRODUCTION = WRITING / "imageNoteProduction.ts"     # 下单载荷
ROUTE = WRITING / "ImageNoteDetailRoute.tsx"        # 图文工作台路由(已有作品列表)
DETAIL = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"


def _tsx(p: pathlib.Path) -> str:
    """剥注释再断言 —— 本项目同一个坑踩过五次以上。"""
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"^\s*//.*$", " ", s, flags=re.M)
    return s


def _fn(path: pathlib.Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _code(path: pathlib.Path, name: str) -> str:
    """函数源码,**剥掉 docstring**(第 8 次踩的那个坑:判据被注释/docstring 命中)。"""
    fn = _fn(path, name)
    body = list(getattr(fn, "body", []))
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _sql_body(path: pathlib.Path) -> str:
    """SQL 去掉 `--` 注释。同上:注释里写着的东西不能算数。"""
    return "\n".join(re.sub(r"--.*$", "", ln)
                     for ln in path.read_text(encoding="utf-8").splitlines())


def _registered_migrations() -> list:
    """manifest 里**真正登记**的那张清单。

    🔴 不许拿全文 substring 判 —— manifest 的注释里逐字写着
       "rollback_024_*.sql 同样【绝不登记】",substring 判据会被这句注释命中,
       于是"回滚不许登记"这条锁**恒红**(我第一版就是这样,当场被自己抓到)。
       判据要打在解析出来的那个 list 上。
    """
    tree = ast.parse(MANIFEST.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "MIGRATIONS" for t in node.targets)):
            return [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    raise AssertionError("manifest 里找不到 MIGRATIONS 清单")


def _py_identifiers(path: pathlib.Path) -> set:
    """模块里**真实出现的标识符**(名字/属性/import 名),不含注释与 docstring。

    🔴 同一个坑的另一面:docstring 里解释"计费在 API 层用 charge_on_success 包着",
       会把"本模块不许出现 charge_on_success"这条锁打成**恒红**。
       恒红和恒真一样废 —— 一个让人无视,一个让人误信。所以直接做成解析。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
            names.add(node.module or "")
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


# ═════════════════════════════════════════════════════════════
# V1 · 蒸馏定价 130
# ═════════════════════════════════════════════════════════════
def test_distill_price_lives_in_pricing_table_only():
    """价目数字只能在迁移里,**代码与前端一个数字都不许有**(价目表 SSOT 铁律 1)。"""
    mig = _sql_body(ROOT / "db"
                    / "migration_023_geo_douyin_topic_distill_pricing_2026_08_03.sql")
    assert "geo_douyin_topic_distill" in mig and "130" in mig, "迁移里没有这条价目"

    # config 只放 code,不放价
    cfg = CONFIG.read_text(encoding="utf-8")
    code_line = next(ln for ln in cfg.splitlines()
                     if ln.startswith("FEATURE_CODE_TOPIC_DISTILL"))
    assert "geo_douyin_topic_distill" in code_line
    assert "130" not in code_line, "把价格写进了 config"

    # 前端读 /pricing,不写死
    # [WO_271] 改指选题面板(蒸馏按钮随旧入口页搬到这里);按钮文案在 imageNoteTopics.distillLabel,一并查
    tsx = _tsx(TOPIC_PANEL)
    assert "/api/geo-douyin/pricing" in tsx and "d?.topic_distill?.cost_points" in tsx, \
        "前端没从 /pricing 读蒸馏价"
    for src in (tsx, _tsx(TOPICS)):
        assert "130" not in src, "前端把蒸馏价写死了"


def test_distill_migration_registered_rollback_not():
    """迁移登记 / 回滚**绝不登记**(登记 = 上线即把本次定价撤掉)。"""
    registered = _registered_migrations()
    assert ("db/migration_023_geo_douyin_topic_distill_pricing_2026_08_03.sql"
            in registered), "迁移 023 没登记 = 上线不会跑"
    assert not [m for m in registered if "rollback" in m],         "回滚脚本被登记进 manifest = 上线即撤销"
    assert (ROOT / "db"
            / "rollback_023_geo_douyin_topic_distill_pricing_2026_08_03.sql").exists()


def test_distill_failure_raises_inside_charge_context():
    """失败的 `raise` 必须在 `charge_on_success` 的 with **体内**。

    🔴 这是本条最容易写错的地方:把失败分支挪到 with 块外面,
       所有别的锁照样绿,但语义从"没蒸出来不扣钱"变成"没蒸出来照样扣 130"。
       所以判据打在 **AST 的嵌套关系**上,不是打在"这两个词都出现过"上。

    🔴 2026-08-05 改异步后,扣费上下文从端点搬进了后台任务 ——
       锁跟着搬到 `run_distill_task`,**意图一字未变**。
    """
    fn = _fn(DISTILL_TASK, "run_distill_task")
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)]
    charged = [w for w in withs
               if "charge_on_success" in ast.unparse(w.items[0].context_expr)]
    assert charged, "后台任务没有用 charge_on_success 包住"
    inner = "\n".join(ast.unparse(s) for s in charged[0].body)
    assert "distill_topics(" in inner, "真正花钱的那次调用不在扣费上下文里"
    assert "raise " in inner, "失败分支不在 with 体内 = 没蒸出来也会扣费"


def test_distill_throttle_has_no_await_between_check_and_stamp():
    """节流的"判定"与"占位"之间**一个 await 都不能有**。

    🔴 收费之后这条从体验问题升级成资金问题:中间隔一个 await,
       两个并发请求就会双双通过 → 扣两次。
    🔴 2026-08-05 改异步后占位函数搬到 distill_task,锁跟着搬,意图未变。
    """
    fn = _fn(DISTILL_TASK, "try_acquire_inflight")
    assert not [n for n in ast.walk(fn) if isinstance(n, (ast.Await, ast.AsyncFor))], \
        "节流的判定与占位之间有 await = 并发下等于没节流"
    code = _code(DISTILL_TASK, "try_acquire_inflight")
    assert "_INFLIGHT_BRANDS[int(brand_id)] = now" in code, "判定完没有占位"


def test_distiller_module_stays_billing_free():
    """`topic_distiller` 自身**零计费**:资金动作集中在 API 一处才审得动。

    与 `redraw.py` 同一条规矩(那边也有同款锁)。
    """
    names = _py_identifiers(DISTILLER)
    assert not [n for n in names if "billing" in n], "蒸馏模块 import 了计费模块"
    for name in ("charge_on_success", "freeze_points", "deduct_points",
                 "commit_freeze", "release_freeze"):
        assert name not in names, f"蒸馏模块里出现了计费动作 {name}"


@pytest.mark.asyncio
async def test_distill_does_not_charge_when_nothing_distilled():
    """行为锁:蒸不出选题时**一分不扣**。

    🔴 只锁源码字样挡不住"把 raise 换成 return 一个空结果"这种改法 ——
       那样 with 体正常结束,扣费照跑,而用户什么都没拿到。所以要真跑一遍。
    🔴 改异步后扣费在后台任务里,所以这里直接跑 `run_distill_task`。
    """
    import middleware.billing as billing
    import services.geo_douyin.distill_task as dt
    import services.geo_douyin.topic_distiller as td
    from services.geo_douyin.topic_distiller import DistillResult

    calls = {"deduct": 0, "precheck": 0}

    async def _fake_precheck(*a, **k):
        calls["precheck"] += 1
        return {"ok": True}

    async def _fake_deduct(*a, **k):
        calls["deduct"] += 1
        return {"ok": True}

    async def _fake_distill(**kwargs):
        return DistillResult(ok=False, error="all_contaminated")

    updates: list = []

    def _fake_update(task_id, **kw):
        updates.append(kw)

    import db.geo_douyin_db as ddb
    orig = (billing.check_balance_only, billing.deduct_points,
            td.distill_topics, ddb.update_distill_task)
    billing.check_balance_only, billing.deduct_points = _fake_precheck, _fake_deduct
    td.distill_topics = _fake_distill
    ddb.update_distill_task = _fake_update
    dt._INFLIGHT_BRANDS.clear()
    dt._INFLIGHT_BRANDS[1] = 0.0
    try:
        await dt.run_distill_task(
            task_id=1, user_id=7, brand_id=1, keywords=["全屋定制"],
            brand_name="测试", city="", industry_key="general", want=5,
            feature_code="geo_douyin_topic_distill")
    finally:
        (billing.check_balance_only, billing.deduct_points,
         td.distill_topics, ddb.update_distill_task) = orig
        dt._INFLIGHT_BRANDS.clear()

    assert calls["precheck"] == 1, "没做余额预检"
    assert calls["deduct"] == 0, "蒸不出选题却扣了费"
    assert any(u.get("status") == "failed" for u in updates), "任务行没落失败态"
    assert not any(u.get("charged") for u in updates), "失败却把 charged 标成 true"


# ═════════════════════════════════════════════════════════════
# V2 · §8.6-10 卡面与文案对齐
# ═════════════════════════════════════════════════════════════
def test_alignment_reports_number_conflict():
    from services.geo_douyin.kb_consistency import check_headline_caption_alignment

    rep = check_headline_caption_alignment(
        body="我们做全屋定制，质保 10 年，欢迎咨询",
        cover={"title": "全屋定制怎么选"},
        cards=[{"headline": "质保", "points": ["整柜质保 15 年"]}],
        closing={})
    assert rep.checked
    kinds = [i.kind for i in rep.issues]
    assert "number_conflict" in kinds, "图上 15 年、文案 10 年,这都不报就没意义了"


def test_alignment_does_not_flag_methodology_counts():
    """🔴 反向锁:卡面有、文案没有 —— 这**不是**矛盾(规范 §8.5 的数字分层)。

    很容易把判据写成"卡面每个数字都要在文案里出现",那样合规产出会被大面积
    误判,然后这套判据就会被当噪声关掉。这条锁专门盯着别写成那样。
    """
    from services.geo_douyin.kb_consistency import check_headline_caption_alignment

    # 🔴 用例必须选**真的会被识别成事实型数字**的写法。
    #    第一版写的是「至少核对 3 项参数」「拆成 4 个部分」—— "项/个"不在
    #    _FACT_UNITS 里,extract_fact_claims 返回空,整条判定路径一次都没跑到,
    #    于是这条锁看着绿、实际什么都没测(变异 V2b 当场存活证明了这点)。
    from services.geo_douyin.kb_consistency import extract_fact_claims
    assert extract_fact_claims("工期 45 天"), "用例本身没有判别力:这句没被识别成事实数字"

    rep = check_headline_caption_alignment(
        body="全屋定制怎么选，看完这条就懂了",           # 文案里**没提**工期
        cover={"title": "全屋定制怎么选"},
        cards=[{"headline": "看工期", "points": ["工期 45 天", "质保 10 年"]}],
        closing={})
    assert rep.checked
    assert rep.ok, f"卡面有、文案没提 —— 这不是矛盾,却被报了:{[i.detail for i in rep.issues]}"


def test_alignment_without_caption_is_not_checked():
    """没有文案 = **没核**,不是"核过没问题"。混起来前端会显示成绿灯。"""
    from services.geo_douyin.kb_consistency import check_headline_caption_alignment

    rep = check_headline_caption_alignment(
        body="", cover={"title": "全屋定制怎么选"}, cards=[], closing={})
    assert rep.checked is False
    assert rep.reason, "没核却不说为什么"


def test_alignment_detects_topic_drift():
    from services.geo_douyin.kb_consistency import check_headline_caption_alignment

    rep = check_headline_caption_alignment(
        body="定制衣柜板材环保等级怎么看",
        cover={"title": "汽车保养多久一次"},
        cards=[], closing={})
    assert "topic_drift" in [i.kind for i in rep.issues], \
        "封面和文案讲的完全是两件事,没报出来"


def test_alignment_does_not_drift_on_real_production_case():
    """🔴 回归锁:**同一个话题**的封面与文案不许被判成跑题。

    生产 post #10 的真实数据。改之前 `_terms()` 用的是「中文极大连续段」——
    中文没空格,一整句就是一个 term:
        封面 {'全屋定制'}  vs  文案 {'深圳全屋定制哪家好', …}
    两边共享"全屋定制"这四个字,集合却毫无交集 → 页面上真的报了一条跑题。

    🔴 上面那条 `test_alignment_detects_topic_drift` **抓不到这个 bug** ——
       它挑的是完全无关的两句,正确实现和错误实现在那种输入上结果一样。
       "只有正向用例"的锁就是这么漏的:必须配一条**同话题必须不报**的反向用例。
       这个 bug 是本机把页面跑起来、看真实数据才发现的。
    """
    from services.geo_douyin.kb_consistency import check_headline_caption_alignment

    rep = check_headline_caption_alignment(
        body="深圳全屋定制哪家好？5家实测对比：报价区间、工期和避坑要点一次说清",
        cover={"title": "全屋定制", "subtitle": "深圳实测避坑思路"},
        cards=[], closing={})
    assert rep.checked
    assert "topic_drift" not in [i.kind for i in rep.issues], \
        f"同一个话题被判成跑题:{[i.detail for i in rep.issues]}"


def test_alignment_surfaced_in_detail_ui_only_when_checked():
    tsx = _tsx(DETAIL)
    assert "alignment" in tsx, "详情页没接对齐核验"
    assert "consistency?.alignment?.checked" in tsx, \
        "没有按 checked 把关 = 会把没核的渲染成核过了"


# ═════════════════════════════════════════════════════════════
# V3 · §8.6-11 OCR 逐字核验 + 要点文字进 prompt
# ═════════════════════════════════════════════════════════════
def test_card_points_text_reaches_the_prompt():
    """🔴 回归锁(本批修的真缺陷):要点**文字**必须进 prompt,不能只用条数。

    改之前 `build_content_prompt` 只用了 `len(points)`,卡面那几行要点是
    生图模型自己编的 —— 与"内容必须从客户知识库蒸馏"直接冲突,
    也让"逐字一致"对内容卡结构性不可能通过。
    """
    from services.geo_douyin.card_templates import build_content_prompt
    from services.geo_douyin.card_templates import StyleTokens

    prompt = build_content_prompt(
        1, "看板材", ["ENF 级最环保", "认准五金品牌"], StyleTokens(),
        metric="45 天工期", caveat="效果因户型而异")
    assert "ENF 级最环保" in prompt, "要点文字没进 prompt(模型只能自己编)"
    assert "认准五金品牌" in prompt
    # 顺带确认没有退回"只说条数"的写法
    assert "45 天工期" in prompt and "效果因户型而异" in prompt


def test_frozen_text_lines_all_appear_in_the_real_prompt():
    """🔴 元锁:`frozen_text_lines` 列的每一行,都必须能在对应的 build_*_prompt
       产出里**逐字**找到。

    它防的是漂移:prompt 里加了一行冻结文案而这里没跟上 —— OCR 核验就会
    安静地少查一项。少查不会红,只会"一直没发现问题"。
    """
    from services.geo_douyin.card_templates import (
        StyleTokens, build_closing_prompt, build_content_prompt,
        build_cover_prompt, frozen_text_lines)

    style = StyleTokens()
    cover = {"title": "深圳全屋定制怎么选", "subtitle": "先看板材再看五金",
             "aux_labels": ["需求", "参数", "工艺", "交付"]}
    cover_prompt = build_cover_prompt(cover["title"], cover["subtitle"], style,
                                      aux_labels=cover["aux_labels"])
    for line in frozen_text_lines("cover", cover):
        assert line in cover_prompt, f"封面冻结文案没进 prompt: {line}"

    card = {"entity": "看板材", "points": ["ENF 级最环保", "五金认品牌"],
            "metric": "45 天", "caveat": "因户型而异"}
    content_prompt = build_content_prompt(
        1, card["entity"], card["points"], style,
        metric=card["metric"], caveat=card["caveat"])
    content_lines = frozen_text_lines("content", card)
    for line in content_lines:
        assert line in content_prompt, f"内容卡冻结文案没进 prompt: {line}"
    # 🔴 **反方向也要锁**:上面那个循环只保证"列出来的都在 prompt 里",
    #    把 points 整段从 frozen_text_lines 里删掉,循环变短照样全绿 ——
    #    而那正是"OCR 核验安静地少查一项"这个缺陷的样子(变异 V3b 就是这么活下来的)。
    #    单向锁的通病:少列 = 少查 = 不会红,只会一直发现不了问题。
    for must in card["points"] + [card["entity"], card["metric"], card["caveat"]]:
        assert any(must in ln for ln in content_lines),             f"这一行会印在图上却没进冻结文案,OCR 永远不会核它: {must}"

    closing = {"headline": "怎么选不踩坑", "summary": "三步走完就不会错",
               "caveat": "效果因人而异", "contact_line": "微信 abc123"}
    closing_prompt = build_closing_prompt(
        closing["headline"], closing["summary"], style,
        contact_line=closing["contact_line"], caveat=closing["caveat"])
    for line in frozen_text_lines("closing", closing):
        assert line in closing_prompt, f"收尾卡冻结文案没进 prompt: {line}"


def test_expected_lines_order_matches_card_specs_order():
    """冻结文案的卡序必须与真正出图的卡序一致。

    错位的后果不是"漏报",是**满屏假问题** —— 拿第 2 张的答案去核第 3 张的图。
    """
    from services.geo_douyin.card_templates import StyleTokens
    from services.geo_douyin.image_pipeline import build_prompts_for_group
    from services.geo_douyin.ocr_qa import expected_lines_for_post

    class _C:
        cover = {"title": "全屋定制怎么选", "subtitle": "看这三点"}
        cards = [{"entity": "板材", "points": ["a"], "caveat": "x"},
                 {"entity": "五金", "points": ["b"], "caveat": "y"}]
        closing = {"headline": "总结", "summary": "就这样", "caveat": "z"}

    specs = build_prompts_for_group(_C(), StyleTokens())
    expected = expected_lines_for_post(
        {"cover": _C.cover, "cards": _C.cards, "closing": _C.closing})
    assert len(specs) == len(expected), "冻结文案的张数与实际出图张数对不上"
    for i, spec in enumerate(specs):
        head = str(spec.get("headline") or "")
        assert any(head[:8] in ln or ln[:8] in head for ln in expected[i]), \
            f"第 {i} 张的冻结文案和这张卡的 headline 对不上 = 卡序错位"


def test_ocr_missing_vs_unavailable_are_different():
    """🔴 `None`(没核成)与 `""`(核了但一个字都没有)必须分开。

    混成一个值,视觉服务一挂就会把每张卡都报成"文字全丢",用户看到一片红。
    """
    from services.geo_douyin.ocr_qa import compare_card

    # 核到了,但内容对不上 → 报缺失
    assert compare_card(["深圳全屋定制"], "完全不相干的字") == ["深圳全屋定制"]
    # 核到了且对得上 → 不报
    assert compare_card(["深圳全屋定制"], "标题：深圳全屋定制 怎么选") == []


@pytest.mark.asyncio
async def test_ocr_service_failure_reports_not_checked_not_ok():
    """视觉服务不可用时,报告必须是 `checked=False`,**绝不能是"全都通过"**。"""
    import services.geo_douyin.ocr_qa as ocr

    async def _fail(url, **k):
        return None

    async def _urls(keys, **k):
        return ["https://x/1.png"]

    import services.geo_douyin.image_pipeline as pipe
    orig_ocr, orig_urls = ocr.ocr_image_url, pipe.signed_card_urls
    ocr.ocr_image_url, pipe.signed_card_urls = _fail, _urls
    try:
        rep = await ocr.run_post_ocr_qa({
            "oss_keys": ["k1"],
            "generation_meta": {"content": {"cover": {"title": "全屋定制怎么选"},
                                            "cards": [], "closing": {}}},
        })
    finally:
        ocr.ocr_image_url, pipe.signed_card_urls = orig_ocr, orig_urls

    assert rep.checked is False, "视觉服务挂了却报成核过了"
    assert rep.flagged_count == 0, "没核成不该报出问题条数"
    assert rep.reason, "没核成却不说为什么"


def test_ocr_endpoint_is_free():
    """OCR 核验**不收费**:它核的是我方有没有把客户的字印对,是我方 QA。"""
    code = _code(API, "api_post_ocr_check")
    for name in ("freeze_points", "charge_on_success", "deduct_points"):
        assert name not in code, f"OCR 核验端点里出现了计费动作 {name}"
    # 🔴 判据要打在**写占位**那一下,不是"这个名字出现过" ——
    #    读那一行 `_OCR_LAST_AT.get(post_id)` 也含这个名字,
    #    所以只删掉写的那行,松judge 的锁照样绿(变异 V3f 就是这么活下来的)。
    fn = _fn(API, "api_post_ocr_check")
    writes = [n for n in ast.walk(fn)
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Subscript)
                      and getattr(t.value, "id", "") == "_OCR_LAST_AT"
                      for t in n.targets)]
    assert writes, "没有把这次调用记进节流表 = 节流形同虚设,可以无限打视觉服务"
    assert "_OCR_MIN_GAP_S" in code, "没有节流间隔判定"


def test_ocr_ui_three_states_are_distinct():
    tsx = _tsx(DETAIL)
    assert "ocr-not-checked" in tsx and "ocr-all-good" in tsx and "ocr-missing" in tsx, \
        "OCR 三态(没核成/都对/有缺失)没有各自的渲染分支"
    assert "ocr?.checked && ocr.flagged_count === 0" in tsx, \
        "「都对上了」没有以 checked 为前提 = 没核成会显示成绿的"


# ═════════════════════════════════════════════════════════════
# V4 · §8.8 画幅自选
# ═════════════════════════════════════════════════════════════
def test_aspect_ratio_whitelist_converges():
    from services.geo_douyin.config import (ASPECT_RATIO_DEFAULT,
                                            normalize_aspect_ratio)

    assert normalize_aspect_ratio("9:16") == "9:16"
    assert normalize_aspect_ratio("3:4") == "3:4"
    for bad in ("", None, "16:9", "1024x1536", "'; DROP TABLE", "4:3"):
        assert normalize_aspect_ratio(bad) == ASPECT_RATIO_DEFAULT, \
            f"白名单外的值 {bad!r} 没有落到默认(会被透传给 provider)"


def test_aspect_ratio_changes_both_provider_param_and_prompt():
    """画幅必须**同时**改两处:给 provider 的参数 + 写进 prompt 的排版规格。

    只改一处的后果:图按 9:16 出但 prompt 里还写着 3:4 的排版(或反之),
    文字会排到画面外。
    """
    from services.geo_douyin.card_templates import output_spec
    from services.geo_douyin.image_pipeline import image_size

    assert image_size("9:16") == "9:16" and image_size("3:4") == "3:4"
    assert "9:16" in output_spec("9:16"), "prompt 的输出规格没跟着画幅走"
    assert "3:4" in output_spec("3:4")
    assert output_spec("9:16") != output_spec("3:4")


def test_default_aspect_ratio_keeps_prompts_byte_identical():
    """🔴 向后兼容锁:不传画幅时,三类 prompt 与加这个参数之前**逐字相同**。

    加参数最容易顺手改掉默认行为 —— 那会让所有存量内容重做一次就变样。
    """
    from services.geo_douyin.card_templates import (
        StyleTokens, build_closing_prompt, build_content_prompt,
        build_cover_prompt)

    style = StyleTokens()
    assert build_cover_prompt("标题", "副标题", style) == \
        build_cover_prompt("标题", "副标题", style, aspect_ratio="3:4")
    assert build_content_prompt(1, "头", ["a"], style) == \
        build_content_prompt(1, "头", ["a"], style, aspect_ratio="3:4")
    assert build_closing_prompt("头", "小结", style) == \
        build_closing_prompt("头", "小结", style, aspect_ratio="3:4")


def test_redraw_uses_the_posts_own_aspect_ratio():
    """🔴 单张重抽必须沿用这条内容原来的画幅。

    漏传的后果是"一组里混着两种画幅" —— 只有肉眼能发现,而发现时图已经付过费了。
    判据同时打在**取值**和**传参**两处:少任何一处都不成立。
    """
    build = _code(REDRAW, "build_redraw_prompt")
    assert 'post.get(\'aspect_ratio\')' in build or 'post.get("aspect_ratio")' in build, \
        "重抽 prompt 没取这条内容的画幅"
    assert build.count("aspect_ratio=aspect_ratio") >= 3, \
        "三类卡(封面/内容/收尾)没有都把画幅传下去"
    render = _code(REDRAW, "redraw_one_card")
    assert "aspect_ratio=" in render, "重抽出图没传画幅"


def test_group_render_passes_one_aspect_ratio_to_every_card():
    code = _code(PIPELINE, "render_prompt_group")
    assert "aspect_ratio=aspect_ratio" in code, \
        "整组出图没把画幅传给每一张(封面按选的出、其余按默认出)"


def test_aspect_ratio_migration_registered_rollback_not():
    registered = _registered_migrations()
    assert ("db/migration_024_geo_douyin_aspect_ratio_2026_08_03.sql"
            in registered), "迁移 024 没登记 = 上线不会跑,详情页会打不开"
    assert not [m for m in registered if "rollback" in m],         "回滚脚本被登记 = 上线即把这一列删了"
    mig = _sql_body(ROOT / "db" / "migration_024_geo_douyin_aspect_ratio_2026_08_03.sql")
    assert "ADD COLUMN IF NOT EXISTS aspect_ratio" in mig
    assert "DEFAULT '3:4'" in mig, "没有默认值 = 存量行读出 NULL"


# ═════════════════════════════════════════════════════════════
# V6 · 同步端点必须装进 nginx 窗口(Owner 实测撞到)
# ═════════════════════════════════════════════════════════════
def test_distill_submit_never_waits_for_the_llm():
    """🔴 提交路径**绝不能**同步等 LLM(WO-DISTILL-TIMEOUT-ASYNC-2026-08-05)。

    这是本次改造的全部意义。同步版生产成功率 **0/5** —— 5 次全部精确顶到
    42s 窗口上限;放开窗口独立复测真实耗时 **58.0s**。nginx
    `proxy_read_timeout 60s` 是硬墙,任何同步方案都贴着墙走。

    判据打在 **AST 调用图**上,不是打在源码子串上 ——
    子串判据在这里会**假阳性**:端点自己就叫 `api_distill_topics`,
    它的定义行里天然含有 `distill_topics(` 这个子串(第一版就是这么写错的)。
    """
    def _called_names(fn):
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                out.add(getattr(f, "id", None) or getattr(f, "attr", None) or "")
        return out

    endpoint_calls = _called_names(_fn(API, "api_distill_topics"))
    assert "dispatch_distill" in endpoint_calls, "提交路径没有把蒸馏丢到后台"
    assert "distill_topics" not in endpoint_calls, \
        "端点里直接调了 distill_topics = 又变回同步等 LLM(nginx 60s 硬墙)"
    # 反向对照:真正干活的那个函数里必须有它,否则上面那条断言可以靠"哪都没有"假绿
    assert "distill_topics" in _called_names(_fn(DISTILL_TASK, "run_distill_task")), \
        "后台任务里没有真正的蒸馏调用 —— 上面那条断言会恒真"


# 🔴 生产实测的真实耗时(放开窗口、独立进程、timeout_s=300 复测)。
#    本文件里所有关于预算的断言都从这个数推,不各写各的魔数。
DISTILL_MEASURED_SECONDS = 58.0


def test_distill_budget_must_exceed_measured_duration():
    """🔴 LLM 预算必须**大于实测耗时**,而且要有余量。

    这是本包第一版漏掉的那一点(Review 全矩阵变异抓出来的):
    异步拆掉的是 nginx 那道墙,**不是 `_call_llm` 的超时**。
    预算仍留在同步时代的 42s,而真实耗时 58s —— 后台任务照样每次在 42s 被掐死,
    成功率仍然 0/5,只是从"504 乱码 + 扣费"变成"礼貌失败 + 不扣费"。

    🔴 旧断言是 `0 < X <= 300` —— 42 和 180 都能过,**等于没守**。
       判据必须打在"预算 vs 实测耗时"的**关系**上,不是打在一个宽区间上。
    """
    from services.geo_douyin.topic_distiller import DISTILL_LLM_TIMEOUT_S

    assert DISTILL_LLM_TIMEOUT_S >= DISTILL_MEASURED_SECONDS * 2, (
        f"蒸馏预算 {DISTILL_LLM_TIMEOUT_S}s 不足实测耗时 "
        f"{DISTILL_MEASURED_SECONDS}s 的两倍 —— 后台任务会被自己掐死,成功率仍 0/5")


def test_distill_budget_still_has_an_upper_bound():
    """反向对照:预算也不能无限大。

    预算越大,一次卡死的调用把这个客户的 in-flight 占位扣得越久
    (超龄回收 = 预算 + 60,跟着一起变长)。没有这条,上面那条可以靠
    "把预算设成 99999" 假绿。
    """
    from services.geo_douyin.topic_distiller import DISTILL_LLM_TIMEOUT_S

    assert DISTILL_LLM_TIMEOUT_S <= 300, \
        f"蒸馏预算 {DISTILL_LLM_TIMEOUT_S}s 过大 —— 卡死的调用会长时间锁住这个客户"


def test_distill_budget_is_actually_passed_to_the_llm():
    """算了预算却不传 = 等于没算。"""
    code = _code(DISTILLER, "distill_topics")
    assert "timeout_s=DISTILL_LLM_TIMEOUT_S" in code, \
        "算了预算却没传给 _call_llm(传了才算数)"


def test_distill_stale_reap_follows_the_budget():
    """🔴 超龄回收阈值必须**跟着预算走**,不能是写死的另一个数。

    两个数各写各的,改了预算不改回收 → 正常任务在跑着就被回收判死,
    而且是静默的(用户只看到"没做成")。所以判据打在**跟随关系**上:
    改预算,回收阈值必须跟着变,且必须留出前置查询的余量。
    """
    from services.geo_douyin.distill_task import stale_after_seconds
    from services.geo_douyin.topic_distiller import DISTILL_LLM_TIMEOUT_S

    stale = stale_after_seconds()
    assert stale > DISTILL_LLM_TIMEOUT_S, (
        f"回收阈值 {stale}s 不大于 LLM 预算 {DISTILL_LLM_TIMEOUT_S}s —— "
        "正常任务跑着就会被判死")
    # 余量要够覆盖前置的三次库查询(生产实测 ~2.1s),取 30s 作宽松下界
    assert stale - DISTILL_LLM_TIMEOUT_S >= 30, \
        "回收阈值贴着预算走,前置查询稍慢就会误杀正在跑的任务"


# ── [WO_271] 退役:test_frontend_poll_deadline_matches_backend_reap_threshold ──
# 它读的是已删的旧入口页(MAX_POLLS × INTERVAL_MS 要等于后端回收阈值)。后继选题面板的蒸馏
# 轮询**不再有前端时限**:每 3 秒问一次,停不停只听服务端给的状态(distillProgress().keepPolling),
# 后端回收后回的是终态 —— 「前端先放弃 / 后放弃」两种不一致的前提随旧页一起没了。
# 接替者:frontend/scripts/verify-image-note-topics.mjs A2b / A2c(终态停轮询 / 查不到给终态照办),
# 在 npm run build 链里每次都跑;后端那一半(回收阈值跟着预算走)仍由本文件
# test_distill_stale_reap_follows_the_budget 守。登记:tests/RETIRED_TESTS.txt。


def test_async_production_path_llm_timeout_unchanged():
    """🔴 反向锁:**异步**生产链的超时一个字没动。

    它跑在后台任务里,不受 nginx 约束,180s 是按生产实测定的
    (16000 tokens 档 23.6–47.4s,24000 档出现 169.8s 长尾)。
    给同步端点加预算时顺手把默认值也改小,会让整条生产链开始超时。
    """
    from services.geo_douyin.content_generator import _LLM_TIMEOUT_S

    assert _LLM_TIMEOUT_S == 180.0, "异步生产链的 LLM 超时被改动了"
    sig = _fn(ROOT / "services" / "geo_douyin" / "content_generator.py", "_call_llm")
    kwonly = {a.arg for a in sig.args.kwonlyargs}
    assert "timeout_s" in kwonly, "_call_llm 没有超时预算参数"
    # 默认必须是 None（= 沿用 _LLM_TIMEOUT_S），不能是某个写死的小值。
    # 🔴 2026-08-06:原来这里写的是 `if len(kwonlyargs) == 1 else None` ——
    #    我这一包给 _call_llm 加了第二个关键字参数 `diag`,那个条件当场变假,
    #    default 恒为 None,整条断言**静默跳过**。锁没红,只是不再看任何东西了。
    #    改成按名字定位,加几个参数都不影响。
    names = [a.arg for a in sig.args.kwonlyargs]
    default = sig.args.kw_defaults[names.index("timeout_s")]
    assert isinstance(default, ast.Constant) and default.value is None, \
        "timeout_s 的默认值不是 None —— 会悄悄改掉异步链的行为"


def _raw_message_sites(src: str) -> list:
    """出错分支里把异常的 `.message` 原样交给界面的写法(`X instanceof Error ? X.message`)。"""
    return re.findall(r"\b(\w+) instanceof Error \? \1\.message\b", src)


def test_frontend_never_shows_raw_json_parse_error():
    """🔴 前端不许 `res.json()` 之后才判 `res.ok`。

    服务端返回的不是 JSON 时(nginx 504/502/404 都是 HTML 页面),
    `.json()` 抛 `SyntaxError: Unexpected token '<'`,而调用方普遍用 `e.message`
    展示 —— 用户看到的就是这句解析器内部报错(Owner 截图)。

    修法是接**平台既有**的人话层 `formatApiErrorForDisplay`(它已经有
    401/403/502/504/429/500 的文案),不另起一套。

    [WO_271] 改指现役两个取数文件(选题面板 + 图文工作台路由),按后继重判:后继不用
    readApiJson,改成服务端原话 + 人话兜底;解析器报错能不能上屏,最后只取决于出错分支
    有没有把异常的 `.message` 原样给用户。
    [WO_282-F4 已修] 三处(面板「开始制作」「保存选题」、路由的已有作品列表)都改走
    `imageNoteTopics.userFacingError`:我们自己抛的人话原样、断网统一人话、其余走兜底,
    原话只进 console。它的行为由 build 链 verify-image-note-topics.mjs A13 真调钉住。
    """
    # [WO_283-F9] 扩到三栏详情页:它另有 5 处原样 .message(原锁从没覆盖过它)
    for f in (TOPIC_PANEL, ROUTE, DETAIL):
        src = _tsx(f)
        raw = _raw_message_sites(src)
        assert not raw, (f"{f.name} 还有 {len(raw)} 处出错分支在直接展示 .message —— "
                         "解析器 / 浏览器原话会给用户")
        # 出错分支不许「一句固定话了事」把原话吞掉:要走那个会把原话记进 console 的人话层
        assert "userFacingError(" in src, f"{f.name} 的出错分支没走 userFacingError —— 原话不上屏也不进 console"
    # 路由那一处的病根是先裸 .json() 再判 ok:改成解析失败即当没拿到
    assert "const d = await res.json().catch(() => null);" in _tsx(ROUTE), "工作台路由又先裸 .json() 再判 ok"


def test_raw_message_lock_has_power():
    """反臂(WO_282):出错分支改回原样上屏 ⇒ 上一格的判定必须命中;干净的现役源码一处不报(对照)。"""
    for f in (TOPIC_PANEL, ROUTE, DETAIL):
        assert _raw_message_sites(_tsx(f)) == [], f"对照:{f.name} 现役源码不该命中"
    for poison in ("} catch (e) { setError(e instanceof Error ? e.message : '兜底'); }",
                   "} catch (err) { setError(err instanceof Error ? err.message : '兜底'); }"):
        for f in (TOPIC_PANEL, DETAIL):
            assert _raw_message_sites(_tsx(f) + "\n" + poison + "\n"), f"{f.name} 塞了 {poison!r} 却没报"


def test_read_api_json_does_not_leak_html_into_detail():
    """`readApiJson` 拿到非 JSON 时**不许**把 HTML 原文塞进 detail。

    塞进去等于换个地方继续把乱码给用户看。
    """
    api_ts = _tsx(ROOT / "frontend" / "src" / "lib" / "api.ts")
    assert "export async function readApiJson" in api_ts, "helper 不见了"
    body = api_ts.split("export async function readApiJson", 1)[1][:1200]
    assert "data: isJson ? parsed : undefined" in body, \
        "非 JSON 响应把原文塞进了 data/detail"
    assert "err.response" in body, \
        "抛出的形状不对齐 axios,formatApiErrorForDisplay 的状态码分支不会生效"


# ═════════════════════════════════════════════════════════════
# V7 · 词来自客户买的那些,不是让他自己填(Owner 2026-08-03)
# ═════════════════════════════════════════════════════════════
def test_purchased_keywords_filter_out_non_core():
    """🔴 `is_core IS NOT FALSE` 这一刀不许丢。

    写文章那条链一直有这一刀:`is_core = FALSE` 是**覆盖词**,
    客户买它不是为了拿它产内容。漏掉的后果不是少给,是**多给** ——
    拿客户没买来做内容的词去生成图文。
    生产实测(只读通道):confirmed/paid 下覆盖词 163 / 核心词 602,
    即漏这一刀时返回的词里 **21% 是不该出现的**。
    """
    code = _code(DISTILLER, "load_purchased_keywords")
    assert "is_core IS NOT FALSE" in code,         "覆盖词没被过滤 —— 会拿客户没买来产内容的词去做图文"
    # 与写文章同源的另外两个闸也必须在
    assert "confirmed_keywords" in code and "JOIN quotes" in code,         "数据源和写文章不同源了"
    assert "'confirmed', 'paid'" in code, "报价状态闸丢了"


def test_keyword_query_has_a_single_implementation():
    """两种形状(字符串 / 完整行)必须共用**同一条查询**。

    写两条 SQL 就一定会漂:一条加了 is_core、另一条没加,
    而两处显示的词不一样时,代理只会觉得"系统算错了"。
    """
    thin = _code(DISTILLER, "load_quote_keywords")
    assert "load_purchased_keywords" in thin, "又写了第二条查询"
    assert "SELECT" not in thin.upper(), "薄壳里出现了 SQL = 两份实现"


def test_entry_page_does_not_ask_user_to_type_the_keyword():
    """🔴 入口页不许把「核心词」做成一个空输入框等用户敲。

    Owner:「不应该是让客户自己填,而是我们直接帮他按照他购买的词来
    进行生成选题,这是我们的事」。客户买了什么是我们知道的事。
    手填只作**没买词时**的兜底(元指令 13:永远不中断对话)。
    """
    # [WO_271] 改指选题面板,按后继重判:买的词不再单摆一块 purchased-keywords,
    # 而是列表**默认就是客户关键词**(每行一个已购 / 已确认的词,勾了就做)。
    tsx = _tsx(TOPIC_PANEL)
    assert "/keywords" in tsx, "前端没去取这个客户买的词"
    assert "useState<'keywords' | 'ideas'>('keywords')" in tsx, \
        "列表默认不是客户买的词 —— 又让用户先自己想"
    # 手填兜底必须存在(不阻断),但必须是**有条件的**:默认收起,点「自己写一个选题」才展开
    assert "const [manualOpen, setManualOpen] = useState(false)" in tsx, \
        "手填框默认就开着 —— 那就等于没改"
    assert "{manualOpen && <form" in tsx, "手填框不是按需展开的"
    assert "这个客户还没有已购或已确认的关键词" in tsx, "没买词时没有兜底引导 = 没买词的客户被卡死"


def test_no_dead_route_in_keyword_empty_hint():
    """空态里引导去选词的链接必须指向**真实存在**的路由。

    🔴 我第一版写的是 `/quote-center` —— 那个路由不存在。
       死链不会报错,只会"点了没反应",而这正是空态最需要好用的时候。
       真入口是 `/pricing`(侧栏第 2 步「报价方案」)。
    """
    tsx = _tsx(TOPIC_PANEL)  # [WO_271] 改指选题面板(空态「去报价里确认关键词」)
    assert "/quote-center" not in tsx, "又写了不存在的 /quote-center"
    app = _tsx(ROOT / "frontend" / "src" / "components" / "layout" / "AppSidebar.tsx")
    assert "'/pricing'" in app, "侧栏里找不到 /pricing —— 这条锁的前提本身要先成立"
    assert '"/pricing"' in tsx, "空态没有给去报价选词的入口"


# ═════════════════════════════════════════════════════════════
# V8 · 表达方式实证回写(B端/C端 × 行业)+ 内容规划层
# ═════════════════════════════════════════════════════════════
def test_default_hook_is_not_the_negative_one():
    """🔴 默认首图钩子不许是疑问式。

    实测(生产全量 5,593 条,同层内带该特征 vs 不带的采纳率差):
        疑问式  B端 +0.6pp / C端 +0.0pp / 家装(试点) **−5.2pp**
        榜单式  家装 **−7.1pp**
        避坑式  B端 +12.7 / C端 +10.0 / 家装 +11.9 —— 唯一跨层都正向
    当初选 question 是**判据用错了**:看的是"占比高",不是"提升"。
    """
    from services.geo_douyin.card_templates import DEFAULT_HOOK, pick_hook

    assert DEFAULT_HOOK == "warning", "默认钩子又回到了在试点行业负向的那个"
    assert pick_hook("home_improvement") == "warning", "试点行业没走避坑式"
    assert pick_hook("") == "warning", "未知行业没有落到跨层最稳的那个"
    # 疑问式只在实测为正的行业才用
    assert pick_hook("时尚美妆") == "question"
    assert pick_hook("education") == "question"


def test_hook_is_actually_wired_into_the_prompt():
    """🔴 钩子必须**真的进 prompt**。

    这条锁存在的理由:`COVER_HOOKS` / `DEFAULT_HOOK` 原来是**零调用点的死常量** ——
    M2「首图三选一钩子」整套方法论只写成了常量,从没接进生成侧。
    改默认值在那种状态下**行为上等于没改**。
    同型坑(describe_card_pricing / OUTPUT_SPEC)这是第三次。
    """
    gen = ROOT / "services" / "geo_douyin" / "content_generator.py"
    src = gen.read_text(encoding="utf-8")
    assert "{hook_line}" in src, "prompt 模板里没有钩子占位"
    assert "hook_line=COVER_HOOKS[pick_hook(industry_key)]" in src,         "钩子没有按行业填进 prompt"
    assert "{audience_line}" in src and "audience_line=audience_style_block" in src,         "B端/C端 表达差异没进 prompt"


def test_industry_reaches_the_generator():
    """行业必须一路传到生成侧,否则全站走同一套话术。

    🔴 原来这条链**断在 production_task**:industry_key 存进了 post,
       却没传进 generate_image_post_content。
    """
    # 🔴 2026-08-05:判据从"函数体里有这个串"改成"**这次调用**带了这个参数"。
    #    原写法在 `build_style_tokens(..., industry_key=industry_key, ...)` 出现之后
    #    变成了**恒真** —— 生成侧那个参数删掉,子串仍在别处命中,锁照样绿。
    #    (变异 V8d 当场存活抓到的。同一个串在别处也有 = 弱锁,本仓第三次。)
    import ast as _ast
    fn = _fn(ROOT / "services" / "geo_douyin" / "production_task.py",
             "run_image_post_production")
    calls = [c for c in _ast.walk(fn) if isinstance(c, _ast.Call)
             and getattr(c.func, "id", "") == "generate_image_post_content"]
    assert calls, "找不到生成侧调用 —— 锚点失效"
    assert "industry_key" in {kw.arg for kw in calls[0].keywords},         "行业没传进生成侧(全站会走同一套话术)"
    api = API.read_text(encoding="utf-8")
    assert "industry_key=req.industry_key" in api, "下单时没把行业带上"
    assert 'industry_key=str(post.get("industry_key") or "")' in api,         "整条重做时没沿用这条内容原来的行业"


def test_b_side_and_c_side_styles_differ():
    """B端/C端 是**方向差异**不是程度差异,两段文案不能一样。"""
    from services.geo_douyin.card_templates import audience_style_block, is_b_side

    b = audience_style_block("technology")
    c = audience_style_block("home_improvement")
    assert b != c, "B端和C端给了同一段约束 = 实测结论没落地"
    assert is_b_side("business_service") and not is_b_side("时尚美妆")
    # B端 干货教程 −2.9pp(technology −7.7pp)→ 必须明确禁
    assert "攻略" in b and "科普" in b, "B端没禁「攻略/科普」字样"
    # B端 含数字 +8.0pp / C端 +1.0pp
    assert "数字" in b


def test_content_plan_reuses_the_quote_quota():
    """🔴 篇数**复用报价的饱和曲线**,不另造公式。

    再发明一个就会出现"文章说 8 篇、图文说 5 篇"两个口径打架,
    而代理无从判断信哪个。
    """
    from services.geo_douyin.content_plan import _plan_rows

    rows = [{"keyword": "全屋定制", "required_articles": 8},
            {"keyword": "深圳衣柜", "required_articles": 3}]
    plans = _plan_rows(rows, {"全屋定制": 2, "深圳衣柜": 5})
    by = {p.keyword: p for p in plans}
    assert by["全屋定制"].quota == 8 and by["全屋定制"].gap == 6, "缺口算错"
    # 已经做超了不能给负数(负数会被前端显示成"还差 -2 条")
    assert by["深圳衣柜"].gap == 0, "做超了给出了负缺口"
    # 缺口大的排前面
    assert plans[0].keyword == "全屋定制", "没有按缺口排序"

    plan_code = _code(ROOT / "services" / "geo_douyin" / "content_plan.py",
                      "_plan_rows")
    assert "required_articles" in plan_code, "篇数不是取报价配额"
    for banned in ("target_share", "competitor_count", "math.ceil"):
        assert banned not in plan_code, f"又在这里重算了一遍饱和曲线({banned})"


def test_no_hardcoded_default_cities():
    """🔴 前端不许写死默认城市。

    原来是 `DEFAULT_CITIES = ['深圳','广州','杭州','成都','武汉']`。
    双重错误:① 硬编码;② 生产实测购买词本身就带城市
    (「贵阳观山湖区酸汤火锅推荐」)—— 给贵阳的客户默认深圳/广州,
    会直接做出一组城市全错的内容。猜错比空着糟得多。
    """
    # [WO_271] 改指选题面板,按后继重判:后继**没有城市输入框** —— 城市是每一行自带的
    # (后端按客户资料 / 报价单解析好放在行上),前端只显示、下单原样带回,不给任何初值。
    tsx = _tsx(TOPIC_PANEL)
    assert "DEFAULT_CITIES" not in tsx, "又写死了一份默认城市"
    assert "cityInput" not in tsx, "又长出了城市输入框 —— 它的初值一写死,整组内容城市全错"
    assert "{r.city &&" in tsx, "行上的城市没显示给代理核对"
    topics = _tsx(TOPICS)
    assert "city: str(o.city)" in topics, "选题行没带后端给的城市"
    assert "city: o.post_city || o.quote_city" in topics, "关键词行没带后端给的城市"
    assert "city: row.city" in _tsx(PRODUCTION), "下单时的城市不是这一行自带的那个"


def test_city_parsing_matches_production_values():
    """城市归一化必须能吃下**生产真值**,不是理想格式。

    只读通道 2026-08-03 实测的 `brands.cities` / `quotes.city` 分布:
    "深圳" / "广东省深圳市" / "深圳市" / "贵州省贵阳市" / "全国" /
    "上海、北京、杭州、深圳" / "云南省曲靖市罗平县"
    """
    from services.geo_douyin.topic_distiller import parse_cities

    assert parse_cities("广东省深圳市") == ["深圳"]
    assert parse_cities("深圳市") == ["深圳"]
    assert parse_cities("贵州省贵阳市") == ["贵阳"]
    assert parse_cities("上海、北京、杭州、深圳") == ["上海", "北京", "杭州", "深圳"]
    assert parse_cities("云南省曲靖市罗平县") == ["曲靖"], "没有归到市一级"
    # 🔴 反向:不是城市的**不许编**
    for not_city in ("全国", "全球", "全国 海外", "", None):
        assert parse_cities(not_city) == [], f"把 {not_city!r} 当成了城市"


def test_city_prefers_client_profile_over_quote():
    """城市优先取**客户自己的资料**(Owner:「自动引用客户的资料就行」),
    报价单只作兜底。两个来源共用同一个清洗,不给第二套。"""
    code = _code(DISTILLER, "resolve_client_cities")
    assert "FROM brands" in code and "cities" in code, "没读客户档案的城市"
    assert "parse_cities" in code, "客户档案那一路没走同一个清洗"
    assert "quote_cities" in code, "没有报价单兜底"


# ── [WO_271] 退役:test_plan_is_surfaced_so_agent_does_not_guess ──
# 它读的是已删的旧入口页(取 /plan、画 keyword-plan-hint)。顶栏「买了 / 已做 / 还差」是产品
# 主动撤掉的(0913P · 019ec0f55),不是改写时弄丢的:现在每个关键词行自带「已制作 N 条图文」。
# 接替者:tests/test_geo_douyin_detail_ui.py::test_retired_image_note_pages_stay_deleted
# (必跑集;图文流文件里再读 /plan 就红)。登记:tests/RETIRED_TESTS.txt。


# ═════════════════════════════════════════════════════════════
# V5 · 语料读取(2026-08-03 用真实语料实跑后发现的两个缺陷)
# ═════════════════════════════════════════════════════════════
def test_corpus_caption_is_truncated_in_python_not_sql():
    """🔴 回归锁:caption 截断必须在 Python 侧。

    原来是 `left(caption, 320)`。用 12 条真实生产语料实跑时,某些 caption 上
    `left()` 在**服务端**报 `invalid byte sequence for encoding "UTF8"` ——
    而同一行整取回来与源串逐字节相同。成因没查清,但 Python 切片按码点切,
    构造上不可能切出半个字符,所以这条路是确定安全的。
    """
    corpus = ROOT / "db" / "douyin_corpus_db.py"
    code = _code(corpus, "_select")
    assert "left(caption" not in code, \
        "又在 SQL 里截断 caption 了 —— 真实语料上会服务端报 invalid byte sequence"
    assert "[:CAPTION_MAX]" in code, "没有在 Python 侧截断"


def test_corpus_load_guards_every_query_not_just_the_first():
    """🔴 回归锁:三条降级查询**整体**兜住。

    原来只有第一条在 try 里。同一种读失败,发生在第一条是优雅降级、
    发生在第二三条是未捕获异常 → 蒸馏端点 500。实跑时两种都撞到了。
    同一类故障必须有同一种表现,否则排查会被带偏。
    """
    corpus = ROOT / "db" / "douyin_corpus_db.py"
    fn = _fn(corpus, "load_fewshot")
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, "load_fewshot 没有任何异常兜底"
    guarded = "\n".join(ast.unparse(s) for t in tries for s in t.body)
    for where in ("is_image_post = TRUE", "is_image_post = FALSE",
                  "industry_key <> %s"):
        assert where in guarded, f"这条降级查询没被兜住,读失败会直接 500: {where}"
    # 🔴 光有 try 不够,还要看它**接的是哪一类** ——
    #    把 `except Exception` 收窄成某个具体异常,三条查询还在 try 里、
    #    我的 AST 结构判据照样绿,而真实的读失败(psycopg2.Error)会直接穿出去。
    #    第一版就是漏了这一半,变异 V5b 当场存活。
    handlers = [h for t in tries for h in t.handlers]
    assert any(h.type is None or getattr(h.type, "id", "") == "Exception"
               for h in handlers), \
        "兜底只接了某个具体异常 —— 真实的读失败仍会穿出去变成 500"


def test_frontend_does_not_hardcode_ratio_list():
    """画幅选项由后端给。前端写死一份 → 后端加第三种时会安静地少一个选项。"""
    # [WO_271] 改指选题面板 + 下单载荷
    tsx = _tsx(TOPIC_PANEL)
    prod = _tsx(PRODUCTION)
    assert "d.aspect_ratios" in tsx, "前端没从 /pricing 读画幅选项"
    assert "aspect_ratio: settings.aspectRatio" in prod, "下单时没把画幅传给后端"
    assert "productionBatch(brandId, selectedPending, settings, quote" in tsx, \
        "下单没走带画幅的那份载荷"
    for src in (tsx, prod):
        for ratio in ("9:16", "3:4"):
            assert f"'{ratio}'" not in src and f'"{ratio}"' not in src, "前端写死了画幅列表"


# ═══════════════════════════════════════════════════════════════
# 2026-08-04 返工 · Review 攻破 §3.1 + 验收实测到的重复扣费
# ═══════════════════════════════════════════════════════════════

def test_logo_only_client_must_not_pass_as_having_real_photo():
    """🔴 只有 LOGO、零实拍图的客户，不能被当成"有已授权实拍图"。

    Review 2026-08-04 攻破交付单 §3.1:原判据是
    `"brand_image_assets" in content.kb_sources`，而 `sources_used` 在
    `logo_hints or image_hints` **任一非空**时就置位 —— 于是 logo-only 的客户
    信号为 True，「实拍叠字」不降级，模型凭空画一张假实景图挂到客户名下。
    生产量化:9 个有双闸资产的品牌里 5 个是 logo-only(56%) = 多数场景。

    本条直接对着**行为**判,不判字符串。
    """
    from services.geo_douyin.knowledge_context import BrandContext

    logo_only = BrandContext(brand_name="某客户")
    logo_only.logo_hints.append("一个绿色的圆形标志")
    logo_only.sources_used.append("brand_image_assets")   # 现实里就是会被置位
    assert logo_only.has_real_photo is False, (
        "logo-only 被判成有实拍图 —— 实拍叠字不会降级,模型会编一张假实景图")

    with_photo = BrandContext(brand_name="某客户")
    with_photo.image_hints.append("门店堂食区实拍,暖色灯光")
    assert with_photo.has_real_photo is True, "有实拍图却判成没有 —— 过度降级"

    # 必须不命中面:两者的 sources_used 一样,证明**旧判据分不出这两种情况**
    with_photo.sources_used.append("brand_image_assets")
    assert logo_only.sources_used == with_photo.sources_used, (
        "前提变了:两种情况的 sources_used 不再相同,这条对照失去意义,请重写")


def test_logo_only_client_gets_downgraded_from_photo_style():
    """🔴 端到端方向:logo-only + 选「实拍叠字」→ 必须降级回文字卡。"""
    from services.geo_douyin.card_templates import (DEFAULT_STYLE_KEY,
                                                    resolve_style)
    from services.geo_douyin.knowledge_context import BrandContext

    logo_only = BrandContext(brand_name="某客户")
    logo_only.logo_hints.append("一个绿色的圆形标志")
    got = resolve_style("photo_overlay",
                        has_authorized_photo=logo_only.has_real_photo)
    assert got.key == DEFAULT_STYLE_KEY, (
        f"logo-only 客户拿到了 {got.key} —— 模型会编一张假实景图")

    with_photo = BrandContext(brand_name="某客户")
    with_photo.image_hints.append("门店堂食区实拍")
    kept = resolve_style("photo_overlay",
                         has_authorized_photo=with_photo.has_real_photo)
    assert kept.key == "photo_overlay", "有实拍图的客户被误降级 = 功能白做"


def test_production_reads_the_dedicated_signal_not_the_display_list():
    """🔴 生产侧必须读专用信号,**不能**再回去读 `kb_sources`。

    `kb_sources` 的语义是"这条内容用到了哪些来源"(给用户看的留痕,logo 也算用到了),
    拿它判"能不能画实拍"就是 Review 攻破的那个粒度错。
    """
    code = _code(ROOT / "services" / "geo_douyin" / "production_task.py",
                 "run_image_post_production")
    assert "has_real_photo" in code, "生产侧没有读专用信号"
    assert '"brand_image_assets" in' not in code, (
        "生产侧又回去读 kb_sources 了 —— 那是展示用的列表,不是资格判据")


def test_distill_inflight_is_held_until_the_task_finishes():
    """🔴 蒸馏占位必须 held 到**任务**结束,不是请求结束、更不是一个时间窗。

    验收实测到过的重复扣费:窗口 20s < 实际耗时 → 第二个请求照样放行 → 扣两次 130。
    🔴 改异步后这条更要命:请求 2 秒就返回了,占位若跟着**请求**释放,
       用户连点两下就是两次真调用、两次 130。所以释放点必须在**后台任务**里。
    """
    import services.geo_douyin.distill_task as dt

    dt._INFLIGHT_BRANDS.clear()
    assert dt.try_acquire_inflight(9001) is True, "第一次应该放行"
    assert dt.try_acquire_inflight(9001) is False, (
        "第一次还在跑,第二次就放行了 —— 这就是扣两次 130 的口子")
    # 别的客户不该被误伤
    assert dt.try_acquire_inflight(9002) is True, "占位污染到了别的客户"
    dt._INFLIGHT_BRANDS.clear()


def test_distill_inflight_self_heals_so_a_client_is_never_locked_forever():
    """🔴 进程被杀导致 finally 没跑完时，占位必须能自愈。

    僵尸占位会让这个客户**永远**蒸不了,而且是静默的
    (用户只看到"正在蒸选题",永远)。那比偶尔多扣一次更糟。
    """
    import services.geo_douyin.distill_task as dt

    dt._INFLIGHT_BRANDS.clear()
    # 造一条"很久以前开始、从没释放"的僵尸记录
    dt._INFLIGHT_BRANDS[9003] = -10_000.0
    assert dt.try_acquire_inflight(9003) is True, (
        "僵尸占位没被回收 —— 这个客户被永久锁死")
    dt._INFLIGHT_BRANDS.clear()


def test_distill_release_has_exactly_one_exit():
    """🔴 占位的释放只能有**一个**出口,且必须在 `finally` 里。

    漏一个分支就锁死一个客户,多走一个就重复放。
    🔴 改异步后这个出口在**后台任务**的 finally 里(不是请求的)。
    """
    fn = _fn(DISTILL_TASK, "run_distill_task")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_release_inflight"]
    assert len(calls) == 1, f"释放点有 {len(calls)} 个,必须恰好 1 个"
    in_finally = any(
        any(c is n for t in ast.walk(fn) if isinstance(t, ast.Try)
            for stmt in t.finalbody for n in ast.walk(stmt)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", "") == "_release_inflight")
        for c in calls)
    assert in_finally, "释放不在 finally 里 —— 异常/取消路径会漏放,客户被锁死"


def test_distill_inflight_has_a_db_level_backstop():
    """🔴 进程内那把锁**不是**唯一防线 —— DB 必须有部分唯一索引兜底。

    进程内字典在 WORKERS>1 时失效(提交打到 worker A、另一次打到 B,
    两边各有一份字典 → 双双放行 → 两次 130)。生产当前 WORKERS=1,
    但那是 .env 强制的、不是代码保证的 —— 有人调回 compose 默认的 4 就破。

    判据打在**迁移文件的 DDL** 上:索引必须是 partial(带 WHERE 且限定非终态),
    否则一个客户蒸过一次之后就再也建不了第二单。
    """
    sql = (ROOT / "db"
           / "migration_025_geo_douyin_distill_tasks_2026_08_05.sql").read_text(encoding="utf-8")
    norm = " ".join(sql.split())
    # [索引守卫加固二单 2026-08-24] 形态换成表绑定守卫,验的仍是同一件事。
    assert ("-- @index-guard uq_geo_douyin_distill_inflight "
            "ON geo_douyin_distill_tasks unique") in norm, \
        "没有 DB 级在飞唯一索引 = 进程内锁是唯一防线"
    assert ("CREATE UNIQUE INDEX uq_geo_douyin_distill_inflight ON "
            "public.geo_douyin_distill_tasks (brand_id) "
            "WHERE status IN ('pending', 'running')") in norm, \
        "唯一索引不是 partial 或没限定非终态 —— 会把历史任务也算进唯一性,客户只能蒸一次"


@pytest.mark.asyncio
async def test_generator_carries_the_real_photo_signal_out_of_the_context():
    """🔴 专用信号必须**真的从上下文带到产出对象上**。

    变异 V11c 第一版存活暴露的缺口:上面两条锁分别验了
    「BrandContext 怎么算」和「生产侧读哪个字段」,中间那一跳
    (generator 有没有把它带出来)**没有任何锁**。
    带丢了的后果是静默的:默认 False → 有实拍图的客户也被降级回文字卡,
    「实拍叠字」这一款等于永远选不上,而且不报错。

    行为判据:把 LLM 和上下文都换掉,真跑一次 `generate_image_post_content`。
    """
    import services.geo_douyin.content_generator as cg
    from services.geo_douyin.knowledge_context import BrandContext

    import json

    async def _fake_llm(prompt, **kw):
        # 🔴 _call_llm 返的是**原始字符串**,不是 dict —— 返 dict 的话
        #    _parse_llm_json 会当场 AttributeError,这条锁就变成在测别的东西。
        # 🔴 卡必须带 entity + caveat + points,少一样 `_clamp_cards` 就整张丢掉,
        #    然后走 card_count_mismatch 提前返回 —— 那条路上 has_real_photo
        #    本来就是默认 False(安全方向),这条锁会测到别的东西上去。
        # 🔴 2026-08-05 起产出必须点名客户、必须自带 title/hashtags,
        #    否则整条判废(P0:不点名 = 豆包引用了也推荐不到客户身上)。
        #    桩要跟着契约走,不然这条锁测的是"判废逻辑"而不是它要测的东西。
        return json.dumps({
            "body": "我实测了几家，某客户在深圳做这一行，下面是对比。",
            "cards": [{"entity": f"品牌{i}", "caveat": "样本有限",
                       "points": [f"要点{i}"]} for i in range(1, 3)],
            "cover": {"title": "标题"}, "closing": {"headline": "收尾"},
            "title": "深圳某某服务哪家好？5家实测对比与避坑要点说清",
            "hashtags": ["深圳服务", "服务避坑", "选型对比", "报价参考", "实测"]},
            ensure_ascii=False)

    async def _ctx_with_photo(*a, **k):
        c = BrandContext(brand_name="某客户")
        c.image_hints.append("门店堂食区实拍")
        return c

    async def _ctx_logo_only(*a, **k):
        c = BrandContext(brand_name="某客户")
        c.logo_hints.append("绿色圆形标志")
        c.sources_used.append("brand_image_assets")
        return c

    orig_llm, orig_ctx = cg._call_llm, cg.build_brand_context
    cg._call_llm = _fake_llm
    try:
        cg.build_brand_context = _ctx_with_photo
        got = await cg.generate_image_post_content("测试词", city="深圳", card_count=4,
                                                     brand_name="某客户")
        assert got.has_real_photo is True, (
            "有实拍图的客户,信号没被带出来 —— 实拍叠字永远选不上,且不报错")

        cg.build_brand_context = _ctx_logo_only
        got2 = await cg.generate_image_post_content("测试词", city="深圳", card_count=4,
                                                     brand_name="某客户")
        assert got2.has_real_photo is False, (
            "logo-only 的客户信号为真 —— 模型会编一张假实景图")
        # 反向对照:两者的 kb_sources 相同,证明旧判据分不出这两种情况
        assert "brand_image_assets" in got2.kb_sources, (
            "前提变了:logo-only 不再进 kb_sources,这条对照失去意义,请重写")
    finally:
        cg._call_llm, cg.build_brand_context = orig_llm, orig_ctx


# ═══════════════════════════════════════════════════════════════
# 2026-08-05 · Owner P0:内容必须推荐客户 + 视觉身份按客户定制
# ═══════════════════════════════════════════════════════════════

def test_prompt_actually_asks_to_promote_the_client():
    """🔴 P0:prompt 里必须有一段"这条内容是给谁拉生意的"。

    事故形态:改之前 prompt 里只有一行 `品牌:{brand_name}`,
    **没有任何一句说明拿这个品牌做什么** —— 模型当成背景资料,
    生产实测一条 390 算力的内容通篇不出现客户名字。
    豆包引用了也推荐不到客户身上 = 花钱做公益(Owner 2026-08-05 原话)。
    """
    from services.geo_douyin.card_templates import promote_block
    blk = promote_block("全域上榜（深圳）科技有限公司", city="深圳",
                        industry_key="geo_优化服务")
    assert "全域上榜（深圳）科技有限公司" in blk, "自荐段里没有客户名"
    assert "必须出现" in blk, "没有把'点名'写成硬要求"
    # 必须不命中面:不能因为要推销就放松事实约束
    assert "不许编" in blk or "不编" in blk, "放松了'不编造'约束"
    assert "不贬低同行" in blk, "缺少'不踩同行'约束"

    src = (ROOT / "services" / "geo_douyin" / "content_generator.py").read_text(encoding="utf-8")
    assert "{promote_block}" in src, "prompt 模板里没有自荐段占位"
    assert "promote_block=promote_block(" in src, "自荐段没有真的传进去"


def test_self_promotion_wording_differs_by_audience():
    """B端/C端 自荐**措辞**不同(实测 lift 差 7 倍),但"必须点名"两端一样。

    依据:RESEARCH_..._2026-08-03 §2 —— 厂家自荐 C端 +21.3pp / B端 +3.0pp。
    B端不该写"源头厂家"(不像 B 端语言,且只有 +3.0pp)。
    """
    from services.geo_douyin.card_templates import promote_block
    b = promote_block("某公司", industry_key="technology")      # B 端
    c = promote_block("某公司", industry_key="food")            # C 端
    assert b != c, "两端给了同一段话 —— 实证差异没落地"
    # 🔴 2026-08-06 改判据(Owner「一定要拒绝自卖自夸」):
    #    原来断言 C 端要出现"源头" —— 那是**卖方自称**话术(「我们是源头厂家」),
    #    口吻改成第三方之后它必须消失。锁的主体没变:**两端仍要分岔**,
    #    只是依据从"自称措辞"换成"拿什么当依据":
    #      B 端摆可核验的数字(含数字 B端 +8.0pp / C端 +1.0pp)
    #      C 端讲来历和做法
    assert "数字" in b, "B端没有'摆可核验数字'的要求(含数字 B端 +8.0pp)"
    assert "源头" not in c, "C端还留着'源头'这类卖方自称话术"
    assert "源头厂家" not in b or "不要写" in b, "B端不该鼓励'源头厂家'话术"
    for blk in (b, c):
        assert "必须出现" in blk, "有一端没把'点名'写成硬要求"


@pytest.mark.asyncio
async def test_content_without_the_client_name_is_voided():
    """🔴 行为锁:产出不点名客户 → **整条判废**,不是提示、不是降级。

    降级(比如代码自己把名字塞进去)会产出一句**我们替客户编的推荐语**,
    比不推荐更糟。所以只能失败。
    """
    import json

    import services.geo_douyin.content_generator as cg
    from services.geo_douyin.knowledge_context import BrandContext

    def _payload(body: str) -> str:
        return json.dumps({
            "body": body,
            "cards": [{"entity": f"品牌{i}", "caveat": "样本有限",
                       "points": [f"要点{i}"]} for i in range(1, 3)],
            "cover": {"title": "标题"}, "closing": {"headline": "收尾"},
            "title": "深圳某某服务哪家好？5家实测对比与避坑要点说清",
            "hashtags": ["a", "b", "c", "d", "e"]}, ensure_ascii=False)

    async def _ctx(*a, **k):
        return BrandContext(brand_name="某客户")

    orig_llm, orig_ctx = cg._call_llm, cg.build_brand_context
    cg.build_brand_context = _ctx
    try:
        # 不点名 → 判废
        cg._call_llm = _stub_llm(_payload("这是一段通用科普，谁也没提。"))
        bad = await cg.generate_image_post_content("词", card_count=4,
                                                   brand_name="某客户")
        assert bad.ok is False and bad.error == "brand_not_promoted", (
            f"没点名却通过了:ok={bad.ok} error={bad.error}")

        # 反向面:点了名就要通过 —— 否则这条锁把功能一起废了
        cg._call_llm = _stub_llm(
            _payload("我实测了几家，某客户在深圳做这一行，下面是对比。"))
        good = await cg.generate_image_post_content("词", card_count=4,
                                                    brand_name="某客户")
        assert good.ok is True, f"点了名反而失败:{good.error}"
        assert good.promotes_brand is True
    finally:
        cg._call_llm, cg.build_brand_context = orig_llm, orig_ctx


def _stub_llm(value):
    """把固定串包成 `_call_llm` 的替身(它会被 await,且带关键字参数)。"""
    async def _f(*a, **k):
        return value
    return _f


def test_client_visual_assets_reach_the_prompt():
    """🔴 客户的 LOGO / 实拍图描述必须进 prompt。

    改之前它们被读出来、挂在 context 上,却**只用于算 has_real_photo**,
    一个字都没给到生成侧 —— 于是"按客户素材定视觉身份"根本无从谈起。
    本仓第三次同型缺口:数据取到了,没接到消费端。
    """
    from services.geo_douyin.knowledge_context import BrandContext
    ctx = BrandContext(brand_name="某客户")
    ctx.image_hints.append("门店堂食区实拍，暖木色调")
    ctx.logo_hints.append("墨绿色圆形标志")
    blk = ctx.to_prompt_block()
    assert "门店堂食区实拍" in blk, "实拍图描述没进 prompt"
    assert "墨绿色圆形标志" in blk, "LOGO 描述没进 prompt"
    # 反向面:没素材时不许输出占位(会诱导模型去编)
    assert "实拍" not in BrandContext(brand_name="x").to_prompt_block()


def test_title_and_hashtags_have_no_template_fallback():
    """🔴 Owner:「一定不能硬编码,LLM 出问题直接挂掉就行」。

    生产链不许再从 `title_engine` 取标题 —— 那个模板池造成过两连错:
    无条件拼城市(「深圳深圳AI搜索优化」)、家装模板套到别的行业(「工期」)。
    """
    import ast
    prod = ROOT / "services" / "geo_douyin" / "production_task.py"
    # 🔴 只扫 ast.Name 是**不够的**(变异 V12j 第一版就是这么活下来的):
    #    `__import__('...title_engine').build_title(...)` 是 Attribute 不是 Name。
    #    判据要打在"生产链里根本不该出现 title_engine / build_title 这两个词"上。
    code_all = _code(prod, "run_image_post_production")
    for bad in ("title_engine", "build_title"):
        assert bad not in code_all, f"生产链里又出现了 {bad} —— 模板池回潮"
    code = _code(prod, "run_image_post_production")
    assert "content.title" in code and "content.hashtags" in code, \
        "标题/标签没有改用业务 AI 的产出"
    assert "title_or_hashtags_missing" in code, \
        "AI 没给标题时没有明确失败 —— 会静默退回某种兜底"


def test_title_preview_step_is_gone_entirely():
    """🔴 「先看看标题」整步删除(Owner 2026-08-06)。

    上一版把它从"编标题"降级成"列城市×词",但那样它就**什么也没做** ——
    不产出标题、不改变任何参数,只是让用户多点一次、多滚一屏。
    Owner 原话:「上方已经蒸馏好标题,下方直接写生成就行了,没必要搞两个标题」。

    锁两面(缺一面都会被绕过):
      正面 —— 端点、请求模型、前端调用**全部**不存在;
      反面 —— 前端确实有一个直接下单的入口,不是把功能删光了完事。
    """
    import ast
    api_src = API.read_text(encoding="utf-8")
    assert "/title-variants" not in api_src, "标题预览端点还在"
    assert "CityMatrixRequest" not in api_src, "标题预览的请求模型还在"
    fn_names = {n.name for n in ast.walk(ast.parse(api_src))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "api_title_variants" not in fn_names, "标题预览端点函数还在"

    # [WO_271] 改指选题面板 + 下单载荷;反向入口按后继重判(勾选行 → 开始制作 → 直接建单)
    tsx = _tsx(TOPIC_PANEL)
    for src in (tsx, _tsx(PRODUCTION)):
        assert "title-variants" not in src, "前端还在调标题预览"
        assert "先看看标题" not in src, "前端还有「先看看标题」按钮"
    # 🔴 反向:必须真的有替代入口。否则"把按钮删了"也能让上面三条全绿。
    assert 'data-testid="topics-start"' in tsx and "/api/geo-douyin/posts/batch" in tsx, \
        "删了预览却没有直接下单的入口"
    assert 'data-testid="topic-check"' in tsx, "没有逐条勾选 —— 「一次做几条」没处选"


def test_redraw_rebuilds_visual_identity_from_the_frozen_snapshot():
    """🔴 重抽单张必须用**这条内容当初冻结的那套视觉身份**。

    不从快照读的话,重抽出来的那一张会换配色/换场景 ——
    而组内一致性正好破在"用户看着不顺眼所以去重抽"的那一张上,
    体验是:越修越乱。
    快照缺失(老数据)时退回按行业兜底,**仍然不随机**。
    """
    code = _code(ROOT / "services" / "geo_douyin" / "redraw.py",
                 "build_redraw_prompt")
    assert "snapshot.get('visual')" in code, "重抽没从快照取视觉身份"
    assert "seed=" not in code, "重抽还在按 seed 重算配色"
    assert "industry_key=" in code, "快照缺失时没有行业兜底"


def test_generated_content_snapshot_carries_the_visual_identity():
    """🔴 视觉身份必须进 `to_dict()` 快照 —— 否则上一条锁读到的永远是空。

    两条锁分别管"写进去"和"读出来",中间这一跳(存不存)单独锁。
    本包已经因为漏了"中间那一跳"被变异抓到过一次(V11c)。
    """
    from services.geo_douyin.content_generator import GeneratedContent
    got = GeneratedContent(body="x", visual={"scene": "堂食区", "primary_color": "深棕红"},
                           title="t", hashtags=["a"]).to_dict()
    assert got["visual"]["scene"] == "堂食区", "视觉身份没进快照"
    assert got["title"] == "t" and got["hashtags"] == ["a"], "标题/标签没进快照"
