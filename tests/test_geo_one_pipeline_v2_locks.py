"""SSOT 锁 · 一条流水线 v2(2026-08-03 全量包)

与 `tests/test_geo_content_one_pipeline.py` 同属一套铁律,拆成两个文件只是
因为单文件已经 300+ 行。**SSOT §5 那张表的元锁会同时扫这两个文件**。

覆盖本批四个议题:
  L2c-L2e 客户作用域统一(选客户上提到创作中心 + 大厅真过滤)
  L6c-L6e 知识库同框(一个分母 / 全量物料不丢 / 组件共用)
  L8      杂志级连续组图规范接入(张数三层控制 / caveat / 冻结文案 / 串味)
  L9      选题蒸馏器(few-shot 优先级 / 降级如实 / 禁抄 / 串味)
  L10     按张数计价(唯一实现 / 不减价 / fail-closed / 重抽三条腿)
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23,09-08 #150 §4)。
# 读它的格改指现役 /writing/image-note 的后继文件;随整页消失的格原位退役,
# 登记在 tests/RETIRED_TESTS.txt(接替者写在那里)。
WRITING = ROOT / "frontend" / "src" / "pages" / "Writing"
TOPIC_PANEL = WRITING / "ImageNoteTopicPanel.tsx"   # 选词 / 蒸馏 / 开始制作
TOPICS = WRITING / "imageNoteTopics.ts"             # 行的形状 / 进度的纯函数
HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"


def _tsx(p: pathlib.Path) -> str:
    """剥注释 —— 本项目同一个坑踩过五次以上:断言命中的是**解释它的注释**,
    把真代码删掉锁照样绿。"""
    import re

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
    """函数源码,**剥掉 docstring**。

    🔴 本项目第 7 次踩同一个坑:`ast.unparse` 会把 docstring 一起吐出来,
       而 docstring 里往往正好写着"不要用 redraw_one_card"这种解释 ——
       于是"必须不命中"的判据被**解释它的那句话**命中,把真代码删掉锁照样绿。
       断言前先剥。
    """
    fn = _fn(path, name)
    body = list(getattr(fn, "body", []))
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


# ═════════════════════════════════════════════════════════════
# L2c-L2e · 客户作用域统一
# ═════════════════════════════════════════════════════════════
def test_writing_hall_filters_by_current_brand():
    """写作大厅必须按**当前客户**过滤,而且传的是 brand_id 不是 brand_name。

    🔴 根因留痕:改之前前端传的是 `brand_name=`,而后端签名里根本没有这个参数
       —— FastAPI 忽略未声明的 query 参数,所以这个过滤**从来没生效过**。
       不是"前端没传",是传了后端不收。判据必须打在 brand_id 上,
       只断言"有过滤代码"会把老写法一起放行。
    """
    src = _tsx(HALL)
    assert "brand_id=${currentBrandId}" in src, "大厅没按当前客户过滤"
    # 🔴 变异 W2 教训:只断言那个模板字符串在,把 if 条件短路成 `false && …`
    #    照样绿 —— URL 拼接语句还在源码里,只是永远不执行。判据要打到条件本身。
    assert "if (currentBrandId !== null && status !== 'optimizing') {" in src, \
        "过滤条件被改坏(比如被 false && 短路掉),等于没过滤"
    assert "brand_name=" not in src, "又回到了后端不认的 brand_name 过滤"


def test_projects_endpoint_takes_optional_brand_id():
    """后端接受可选 brand_id,且**不拿它替代 RBAC**。

    🔴 必须不命中面:视角过滤绝不能顶替权限过滤。传一个自己没权限的 brand_id
       应该被 allowed_brands 拦成空列表,而不是拿到别人的数据。
    """
    fn = _fn(ROOT / "server.py", "api_get_writing_projects")
    args = {a.arg for a in fn.args.args}
    assert "brand_id" in args, "端点没有 brand_id 参数"
    # 🔴 剥 docstring:本函数的 docstring 里正好写着 get_user_brand_filter,
    #    不剥的话把那行真代码删掉锁照样绿(docstring 陷阱,本项目第 8 次)。
    code = _code(ROOT / "server.py", "api_get_writing_projects")
    assert "get_user_brand_filter" in code, "RBAC 过滤被拿掉了"
    assert "allowed_brands" in code, "没有把 RBAC 结果传下去"


# ═════════════════════════════════════════════════════════════
# L6c-L6e · 知识库同框
# ═════════════════════════════════════════════════════════════
def test_display_score_comes_from_m3_summarizer():
    """显示分数的**唯一**来源是 m3 的 `_summarize_materials`。

    🔴 根因留痕:同一个客户,写文章那边显示 7/8、图文这边 x/10 —— 两个分母。
       Review 裁定以 m3 的 8 字段为准。这条锁防的是有人再算第三份。
    """
    fn = _fn(ROOT / "services" / "client_knowledge.py", "load_display_materials")
    code = ast.unparse(fn)
    assert "_summarize_materials" in code, "分数没走 m3 的口径,又自己算了一份"
    # 必须不命中面:不许在这里重新数字段(那就是第三份分母)
    assert "len(M3_FIELD_LABELS)" not in code, "分母是自己数出来的,不是 m3 给的"


def test_generation_context_keeps_full_materials():
    """统一分母之后,**生成上下文不许跟着缩水**(Review 硬要求)。

    🔴 根因留痕:`load_client_materials` 原来只 SELECT 6 列,而填充度按 10 项算 ——
       「资料 9/10」的客户,喂给 LLM 的其实只有 6 项:方法论 / 价格档位 /
       服务区域 / 团队规模**四项白填了**。用户看得到分数、模型看不到内容。
    """
    kb = ROOT / "services" / "geo_douyin" / "knowledge_context.py"
    # 🔴 变异 K2 教训:这个函数的 docstring 里正好写着 MATERIAL_FIELDS,
    #    不剥就是"解释它的那句话"在替真代码顶包(docstring 陷阱第 8 次)。
    loader_code = _code(kb, "load_client_materials")
    assert "MATERIAL_FIELDS" in loader_code, \
        "取素材的列名又是手写的,会和填充度口径漂移"
    # 🔴 变异 K3 教训:只断言 `self.methodology` 出现过,把条件改成
    #    `if False and self.methodology:` 照样绿。判据要打到**那个条件**上。
    block_code = _code(kb, "to_prompt_block")
    for field in ("methodology", "pricing_tiers", "service_area", "team_size"):
        assert f"if self.{field}:" in block_code, f"{field} 没进生成上下文(或条件被短路)"


# ── [WO_271] 退役:test_knowledge_card_is_shared_component ──
# 它要的是「写文章与做图文渲染同一个知识库卡」,图文那一半读的是已删的旧入口页。现役图文流
# (/writing/image-note)不画任何知识库卡,「共用」没有对象了。反向由必跑集
# tests/test_geo_douyin_detail_ui.py::test_retired_image_note_pages_stay_deleted 守(图文流里再画
# 知识库卡就红);知识库计数与生成同一授权判据由同文件 test_knowledge_card_uses_same_authorization_bits
# 在数据层守。登记:tests/RETIRED_TESTS.txt。


# ═════════════════════════════════════════════════════════════
# L8 · 杂志级连续组图规范接入
# ═════════════════════════════════════════════════════════════
def test_card_count_is_code_owned():
    """张数是**代码层结构化参数**,不交给模型(规范 §8.1a 第 1 层)。

    张数直接决定计费 —— 把它交给模型等于把资金语义交给模型。
    """
    from services.geo_douyin.series_plan import content_role_plan, plan_roles

    for n in range(1, 10):
        assert len(plan_roles(n)) == n, f"N={n} 的职责计划张数不对"
        expect = n - 1 - (1 if n >= 2 else 0)     # 减封面,N>=2 时再减收口
        assert len(content_role_plan(n)) == expect, f"N={n} 的内容卡数不对"


def test_card_count_mismatch_fails_loudly():
    """张数不齐必须**明确失败**,禁止静默凑数(规范 §8.1a 第 3 层)。

    🔴 静默少给最难被发现:图都在,只是少了两张 —— 而用户是按 N 张付的钱。
    """
    fn = _fn(ROOT / "services" / "geo_douyin" / "content_generator.py",
             "generate_image_post_content")
    assert "card_count_mismatch" in ast.unparse(fn), "张数不齐没有明确失败"
    # 判据打在**那个比较**上:必须是等值硬校验,不是"有几张算几张"
    compares = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    assert any("len(cards) != content_count" in c for c in compares), \
        "没有对张数做等值硬校验"


def test_role_ladder_always_closes_the_loop():
    """任何 N>=2 都必须同时有封面与收口 —— 封面提问、收口必答(规范 §8.1)。"""
    from services.geo_douyin.series_plan import plan_roles

    for n in range(2, 10):
        roles = [r["role"] for r in plan_roles(n)]
        assert roles[0] == "cover", f"N={n} 的第一张不是封面"
        assert roles[-1] == "closing", f"N={n} 的最后一张不是收口(有头无尾)"
    assert [r["role"] for r in plan_roles(4)] == \
        ["cover", "compare", "cost", "closing"], "默认 4 张的降档表与规范不符"


def test_closing_card_always_has_caveat():
    """收口卡必须带 caveat 区(规范 §8.2)。"""
    from services.geo_douyin.card_templates import (build_closing_prompt,
                                                    build_style_tokens)

    style = build_style_tokens("全屋定制", "深圳")
    p = build_closing_prompt("怎么选", "小结一二三", style, caveat="因需求而异")
    assert "因需求而异" in p, "caveat 没有进收口卡 prompt"
    # 必须不命中面:没给 caveat 时不许凭空造一句进画面
    p2 = build_closing_prompt("怎么选", "小结一二三", style, caveat="")
    assert "单独一行小字提醒" not in p2, "没有 caveat 时不该硬塞一段提醒"


def test_cover_aux_labels_are_frozen_copy():
    """封面导航词必须是**代码给的冻结文案**(规范 §8.4)。

    🔴 根因留痕:样张封面底部"需求/参数/工艺/交付"四个词不在冻结文案任何字段里,
       按原合同就是模型自加 —— 于是验收第 5 条"逐字一致"恒 fail。
    """
    from services.geo_douyin.card_templates import (build_cover_prompt,
                                                    build_style_tokens)
    from services.geo_douyin.series_plan import cover_aux_labels

    labels = cover_aux_labels(4)
    assert labels, "4 张的封面导航词是空的"
    style = build_style_tokens("全屋定制", "深圳")
    p = build_cover_prompt("深圳全屋定制哪家好", "副标题", style,
                           idx=1, total=4, aux_labels=labels)
    for label in labels:
        assert label in p, f"导航词 {label} 没进封面 prompt"
    assert "不要增删任何一个词" in p, "没有要求逐字排版导航词"


def test_layout_role_reaches_the_image_prompt():
    """版式必须**真的进生图 prompt**(规范 §3/§5 的 `{layout_role}`)。

    🔴 变异 G6 教训:原来只有"合同里有 layout_role 字段"这层锁,
       把它在 prompt 里置空照样绿 —— 于是七张又回到同一个版式各说各话。
       这是行为锁:给定版式,产出里必须能找到它。
    """
    from services.geo_douyin.card_templates import (build_content_prompt,
                                                    build_style_tokens)

    style = build_style_tokens("全屋定制", "深圳")
    p = build_content_prompt(1, "参数比较", ["要点一", "要点二"], style,
                             card_index=2, total=4,
                             layout_role="对比矩阵 / 三对象并列",
                             role_label="比较口径")
    assert "对比矩阵 / 三对象并列" in p, "版式没进生图 prompt"
    assert "不要复制上一张的版式" in p, "没有要求换版式"
    assert "比较口径" in p, "本张职责没进 prompt"
    # 必须不命中面:没给版式时不该硬编一个进去
    p2 = build_content_prompt(1, "参数比较", ["要点一"], style)
    assert "对比矩阵" not in p2, "没给版式却凭空塞了一个"


def test_distill_actually_runs_both_taint_checks():
    """两道串味检测必须**真的被调用**,不是只定义在那里。

    🔴 变异 D4 教训:原来只测了 `detect_contamination` 这个函数本身的行为,
       把调用点改成 `hits = []` 照样绿 —— 函数是对的,只是没人用它。
    """
    code = _code(ROOT / "services" / "geo_douyin" / "topic_distiller.py",
                 "distill_topics")
    assert "detect_contamination(blob" in code, "没有对产出跑抄袭检测"
    assert "detect_sample_taint(blob" in code, "没有对产出跑样张串味检测"
    # 命中必须导致**丢弃**,不是标记后照样返回
    assert "dropped += 1" in code, "串味命中后没有丢掉那条选题"


def test_sample_taint_is_industry_scoped():
    """样张行业词只在**跨行业**时算串味(规范 §8.9)。

    🔴 必须不命中面:客户本身就是家装行业时,"板材/五金"是他真正的业务词。
       一刀切会把合规产出误判成违约。
    """
    from services.geo_douyin.series_plan import detect_sample_taint

    assert detect_sample_taint("我们用深胡桃木板材", "food"), \
        "跨行业出现样张词没有被判串味"
    assert not detect_sample_taint("我们用深胡桃木板材", "home_improvement"), \
        "客户就是这个行业,却把他自己的业务词判成了串味"


# ═════════════════════════════════════════════════════════════
# L9 · 选题蒸馏器
# ═════════════════════════════════════════════════════════════
def test_fewshot_prefers_same_industry_image_post():
    """few-shot **同行业图文帖优先**(Review §18a ① 硬要求)。"""
    fn = _fn(ROOT / "db" / "douyin_corpus_db.py", "load_fewshot")
    code = ast.unparse(fn)
    assert "industry_key = %s AND is_image_post = TRUE" in code, \
        "第一优先级不是同行业图文帖"
    # 顺序判据:同行业图文帖那句必须出现在跨行业那句之前
    assert code.index("is_image_post = TRUE") < code.index("industry_key <> %s"), \
        "跨行业样本排在了同行业前面"


def test_fewshot_degradation_is_reported():
    """降级必须**如实回报**,不许悄悄降。

    🔴 悄悄降级比没有语料更糟:用视频样本去教模型写图文帖,
       产出走形时没人知道为什么。
    """
    code = ast.unparse(_fn(ROOT / "db" / "douyin_corpus_db.py", "load_fewshot"))
    for reason in ("corpus_unavailable", "corpus_empty",
                   "no_same_industry_image_post", "partial_same_industry_image_post"):
        assert reason in code, f"降级原因 {reason} 没有回报"
    # [WO_271] 原来的最后一条(前端显示降级提示)读的是已删的旧入口页;后继选题面板没接 →
    #   拆到下一格 xfail(WO_283-F2)。这一格守后端「如实回报」这一半,不跟着一起 xfail。


def _degrade_screen_problems(topics: str, panel: str) -> list:
    """降级原因上屏的缺口清单(空 = 没问题)。分母取**生产方**:load_fewshot 写得出的每一种原因都要有人话。"""
    import re as _re
    code = ast.unparse(_fn(ROOT / "db" / "douyin_corpus_db.py", "load_fewshot"))
    produced = sorted(set(_re.findall(r"'((?:corpus|no_same|partial_same)_[a-z_]+)'", code)))
    out = [] if len(produced) >= 4 else [f"生产方原因只读出 {produced} —— 分母塌了"]
    out += [f"降级原因 {c} 没有人话" for c in produced if f"{c}: '" not in topics]
    if "o.fewshot_degraded" not in topics:
        out.append("蒸馏回包里的 fewshot_degraded 没人读")
    if 'data-testid="topics-distill-degraded"' not in panel or "{degradedNote}" not in panel:
        out.append("面板没把降级那句话画出来")
    if "/latest-topics" not in panel:
        out.append("进页面没把上一批的降级情况读回来(切一次页面就忘了)")
    return out


def test_fewshot_degradation_reaches_the_screen():
    """🔴 降级原因要让代理看得到(上一格原来的最后一条,WO_271 拆出)。

    旧入口页有 distill-degraded 提示;后继选题面板蒸完只重读列表,没读、也没显示
    `fewshot_degraded` —— 后端如实回报了,屏幕上还是「悄悄降」。
    [WO_283-F2 已修] imageNoteTopics 的 DEGRADE_LABEL 把 load_fewshot 写得出的四种原因逐个译成人话,
    distillProgress 在成功时带出那句话;面板常驻显示(不是一闪的 toast),进页面时读 latest-topics 恢复。
    """
    problems = _degrade_screen_problems(_tsx(TOPICS), _tsx(TOPIC_PANEL))
    assert not problems, "\n".join(problems)


def test_fewshot_degradation_lock_has_power():
    """反臂(WO_283):删掉一种原因的人话 / 面板不画 / 不读 latest-topics ⇒ 上一格必须报;现役源码不报(对照)。"""
    topics, panel = _tsx(TOPICS), _tsx(TOPIC_PANEL)
    assert _degrade_screen_problems(topics, panel) == []
    for poisoned in ((topics.replace("partial_same_industry_image_post: '", "x_: '"), panel),
                     (topics, panel.replace('data-testid="topics-distill-degraded"', "")),
                     (topics, panel.replace("/latest-topics", "/latest-x"))):
        assert _degrade_screen_problems(*poisoned), "毒下去了却没报"


def test_distill_prompt_has_no_copy_block():
    """蒸馏 prompt 必须带禁抄段(Review §18a ① + 规范 §8.7)。

    🔴 08-02 实测:带参考不加禁抄段,3/3 把「模板示例」字样逐字印进成品。
    """
    from services.geo_douyin.topic_distiller import (NO_COPY_BLOCK,
                                                     build_distill_prompt)

    p = build_distill_prompt(
        keywords=["全屋定制"], brand_name="某某", city="深圳",
        materials_block="【公司简介】某某", competitors=["甲", "乙"],
        samples=[{"caption": "样本文案", "is_image_post": True}], want=3)
    assert NO_COPY_BLOCK in p, "蒸馏 prompt 没有禁抄段"
    assert "语感参考" in p, "没有说明样本只是语感参考"


def test_contamination_detection_is_deterministic():
    """串味检测必须是**确定性**的,且不误伤客户自己的词。

    🔴 让模型判断自己有没有抄,等于没判。
    """
    from services.geo_douyin.topic_distiller import detect_contamination

    sample = ["某某木业专注高端定制十五年，服务过三千家庭"]
    assert detect_contamination("我们某某木业专注高端定制十五年", sample), \
        "原样抄袭没被抓到"
    # 必须不命中面:白名单里的词(客户自己的关键词)不算抄
    assert not detect_contamination(
        "高端定制怎么选", sample, allow=["高端定制怎么选"]), \
        "客户自己的关键词被误判成抄袭"


# ═════════════════════════════════════════════════════════════
# L10 · 按张数计价
# ═════════════════════════════════════════════════════════════
def test_extra_card_pricing_has_one_implementation():
    """套餐张数口径只在允许的几个文件里出现 —— 加张计价不许有第二份实现。

    🔴 两份实现的后果是"前端显示 890、后端扣 790",只要有两份就会发生。
    """
    import subprocess

    out = subprocess.run(
        ["git", "grep", "-l", "CARD_COUNT_INCLUDED", "--", "services", "api", "db"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8").stdout
    files = {line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()}
    allowed = {"services/geo_douyin/config.py", "services/geo_douyin/pricing.py",
               "services/geo_douyin/series_plan.py", "api/geo_douyin_api.py"}
    unexpected = files - allowed
    assert not unexpected, f"套餐张数口径散进了别的文件: {unexpected}"


def test_below_included_is_not_discounted():
    """少于套餐张数**不减价**(Owner 2026-08-03 明确)。"""
    from services.geo_douyin.pricing import extra_cards

    for n in (1, 2, 3, 4):
        assert extra_cards(n) == 0, f"{n} 张算出了非零加价(或负数抵扣)"
    assert extra_cards(5) == 1 and extra_cards(9) == 5, "超出部分的张数算错了"


def test_pricing_unavailable_blocks_order():
    """价目读不到必须 **fail-closed**,绝不按 0 放行。

    🔴 按 0 继续 = 多做的卡白送,一个读库抖动就能变成资金漏。
    """
    import asyncio

    from services.geo_douyin import pricing

    def _boom(code):
        raise RuntimeError("db down")

    original = pricing._read_unit_points_sync
    pricing._read_unit_points_sync = _boom
    try:
        with pytest.raises(pricing.PricingUnavailable):
            asyncio.run(pricing.extra_card_points(9))
        # 必须不命中面:4 张及以下**不读价目**,不该被价目故障连累
        assert asyncio.run(pricing.extra_card_points(4)) == 0
    finally:
        pricing._read_unit_points_sync = original


def test_extra_cost_actually_reaches_freeze():
    """加价额必须**真的进那次冻结**,不是算完就扔。

    🔴 变异 P3 教训:把 `extra_cost=extra_cost` 改成 `extra_cost=0` 之后,
       所有账目锁照样绿 —— 因为没有一条锁看过"冻的是不是含加价的那个数"。
       后果是按 4 张收钱做 9 张,每单白送 5 张的成本。
    """
    code = _code(ROOT / "services" / "geo_douyin" / "production_task.py",
                 "run_image_post_production")
    assert "extra_cost = await extra_card_points(want)" in code, \
        "没有按张数算加价额"
    assert "extra_cost=extra_cost" in code, \
        "加价额没有传进 freeze_points(算了但没扣)"


def test_auto_fill_never_charges_user():
    """系统自动补齐**不收费、不占额度**(§15 + 重抽改收费后的边界)。

    🔴 重抽改收费之后这条边界比以前更重要:自动补齐是系统在补自己没做完的活,
       让用户为此付钱是错的。判据打在"走的是哪个函数"上。
    """
    code = _code(ROOT / "services" / "geo_douyin" / "production_task.py", "_auto_fill")
    assert "render_one_card" in code, "自动补齐没走免费的 render_one_card"
    assert "redraw_one_card" not in code, "自动补齐走了收费且占额度的重抽链"
