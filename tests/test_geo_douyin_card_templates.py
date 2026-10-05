"""§6b 模板锁 · 三类卡模板 + 组内一致性 + 客户素材融入

锁的是**方法论落没落到代码里**,不是"函数能跑"。每条都对应报告里一条实证:
  M1 骨架五选一,GEO 默认 per_entity
  M2 首图钩子三类 + 四必备件
  M3 内容卡同构字段,caveat 必填
  M4 字数分档(封面≤30 / 内容 60-150)
  M6 组内一致性三参数必须传进每一张 prompt
  M7 收口卡三选一
  §6c 实测:热度无关 → 不追爆款;客户素材要真的被引用
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services.geo_douyin.card_templates import (
    CLOSING_KINDS,
    CONTENT_TEXT_MAX,
    CONTENT_TEXT_MIN,
    COVER_TEXT_MAX,
    DEFAULT_SKELETON,
    REQUIRED_CONTENT_FIELDS,
    SKELETONS,
    STYLE_PRESETS,
    StyleTokens,
    build_closing_prompt,
    build_content_prompt,
    build_cover_prompt,
    build_style_tokens,
)
from services.geo_douyin.content_generator import _clamp_cards
from services.geo_douyin.image_pipeline import build_prompts_for_group

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTENT_GEN = ROOT / "services" / "geo_douyin" / "content_generator.py"
KB = ROOT / "services" / "geo_douyin" / "knowledge_context.py"


def _code_strings(path: pathlib.Path) -> str:
    """只取【会执行的】字符串常量,剔除 docstring 与注释。

    🔴 第三次踩同一个坑了:直接扫源码串,会命中**解释"我们为什么不用它"的注释/docstring**,
       于是"没引用竞对表"这条锁在文档里写了原因反而变红。扫描锁必须先剥注释与 docstring。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            out.append(node.value)
    return "\n".join(out)



# ── M1 骨架 ─────────────────────────────────────────────────
def test_five_skeletons_exist():
    assert set(SKELETONS) == {"per_entity", "per_question", "per_feature",
                              "pro_con", "pain_solution"}


def test_geo_default_skeleton_is_per_entity():
    """GEO 要吃「XX 哪家好」→ 必须默认逐实体(天然产出候选清单)。"""
    assert DEFAULT_SKELETON == "per_entity"
    assert SKELETONS["per_entity"][2] is True


# ── M3 同构字段 + caveat 必填 ───────────────────────────────
def test_required_fields_include_caveat():
    assert "caveat" in REQUIRED_CONTENT_FIELDS


def test_card_without_caveat_is_autofilled_not_dropped():
    """🔴 2026-08-06 返工:缺 caveat **不再丢卡**,改为自动填合法出口(A1)。

    本条原名 `test_card_without_caveat_is_dropped`,锁的是旧行为。
    改口径的理由(返工单 §3):丢卡会让 `len(cards) != content_count`
    → `card_count_mismatch` → **整单判废 + 退款**。也就是说"模型少写一句
    注意事项"这种 A1 级问题被级联成整单硬失败,与 R7 裁定(caveat 属 A1)
    和永不中断铁律都冲突。

    「不要纯夸卡」这个**目的没变** —— 现在靠"必有 caveat(真实的或兜底的)"达成,
    而不是靠"没有就整张扔掉"。
    """
    from services.geo_douyin.content_generator import CAVEAT_FALLBACK
    cards = _clamp_cards([
        {"entity": "A", "points": ["好"], "caveat": "工期长"},
        {"entity": "B", "points": ["棒"]},                 # 缺 caveat
        {"entity": "C", "points": ["强"], "caveat": ""},   # caveat 空
    ], 5)
    assert [c["entity"] for c in cards] == ["A", "B", "C"], "又在丢卡"
    assert cards[0]["caveat"] == "工期长" and cards[0]["caveat_autofilled"] is False
    for c in cards[1:]:
        assert c["caveat"] == CAVEAT_FALLBACK
        assert c["caveat_autofilled"] is True
    # 反向对照:每张卡都必须有非空 caveat —— "不要纯夸卡"这条没松
    assert all(str(c["caveat"]).strip() for c in cards)


def test_card_without_points_is_dropped():
    assert _clamp_cards([{"entity": "A", "caveat": "x", "points": []}], 5) == []


def test_valid_card_survives_and_is_homogeneous():
    """反向面:合法卡必须留下,且字段齐全(否则上面两条恒真也能过)。"""
    cards = _clamp_cards([{"entity": "A牌", "one_liner": "性价比之选",
                           "points": ["价格低", "服务好"], "metric": "3000元起",
                           "caveat": "工期偏长"}], 5)
    assert len(cards) == 1
    for f in ("entity", "one_liner", "points", "metric", "caveat"):
        assert f in cards[0]


def test_cards_are_clamped_to_want():
    many = [{"entity": f"E{i}", "points": ["p"], "caveat": "c"} for i in range(10)]
    assert len(_clamp_cards(many, 3)) == 3


# ── M6 组内一致性:三参数必须进每一张 prompt ─────────────────
def _fake_content():
    class C:
        cover = {"title": "深圳全屋定制哪家好", "subtitle": "5家对比"}
        cards = [
            {"entity": "A牌", "points": ["价格低", "服务好"], "metric": "3000元起",
             "caveat": "工期偏长"},
            {"entity": "B牌", "points": ["设计强"], "metric": "", "caveat": "价格高"},
        ]
        closing = {"headline": "选购总结", "summary": "按预算对号入座"}
    return C()


def test_style_tokens_appear_in_every_prompt():
    """🔴 核心锁:主色/顶部条 必须出现在**每一张** prompt 里。

    逐张独立生图时模型之间没有记忆 —— 少传一张,那张就会跑风格。
    """
    style = StyleTokens(primary_color="宝蓝", header_bar="全屋定制选购指南",
                        footer_bar="资料来源:公开信息整理")
    specs = build_prompts_for_group(_fake_content(), style)
    assert len(specs) == 4        # 封面 + 2 内容 + 收尾
    for s in specs:
        assert "宝蓝" in s["prompt"], f"{s['kind']} 卡少了主色"
    # 顶部条只对内容卡强制(封面/收尾另有版式)
    for s in specs:
        if s["kind"] == "content":
            assert "全屋定制选购指南" in s["prompt"]


def test_group_has_three_kinds_in_order():
    specs = build_prompts_for_group(_fake_content(), StyleTokens())
    assert [s["kind"] for s in specs] == ["cover", "content", "content", "closing"]


def test_style_tokens_are_deterministic():
    """同一客户同一行业 → 稳定(便于复现与重抽时对齐)。"""
    a = build_style_tokens("全屋定制", "深圳", industry_key="home_improvement")
    b = build_style_tokens("全屋定制", "深圳", industry_key="home_improvement")
    assert a.to_dict() == b.to_dict()


def test_visual_identity_comes_from_the_client_not_from_a_seed():
    """🔴 视觉身份必须**按这个客户**来,不是随机。

    Owner 2026-08-05:「颜色随机没问题,但是要根据客户的素材或者行业来定」。
    原实现是 `primary_color = PALETTE[seed % 5]`,seed 是 `variant_index` ——
    与客户零关系,同一个客户换个城市变体就换一套色。
    取值顺序:业务 AI 按客户素材/行业给的 visual → 行业兜底表 → 通用兜底。
    """
    # ① LLM 给了就必须用它(主路径)
    # 🔴 夹具的颜色必须**与该行业的兜底色不同**,否则这条断言分不出
    #    "用了 LLM 给的" 和 "落到了行业兜底" —— 变异 V12h 第一版就是这么活下来的:
    #    我原来写的「深棕红」恰好就是 food 的兜底色。
    from services.geo_douyin.card_templates import industry_palette
    fb_p, fb_a = industry_palette("food")
    assert ("孔雀绿", "亮金") != (fb_p, fb_a), "夹具撞上兜底色了,换一个"
    got = build_style_tokens("酸汤火锅", "贵阳", industry_key="food",
                             visual={"scene": "苗家酸汤堂食区，暖木色调",
                                     "primary_color": "孔雀绿", "accent_color": "亮金"})
    assert got.primary_color == "孔雀绿" and got.accent_color == "亮金",         "LLM 给的视觉身份没被采用 —— 落回了行业兜底"
    assert got.scene == "苗家酸汤堂食区，暖木色调", "场景没带出来 —— 七张就凑不成一个场景世界"

    # ② 没给 → 行业兜底(底线),而且**不同行业要给出不同的底**,
    #    否则"按行业定"退化成"所有人一个样"
    fam = {build_style_tokens("k", industry_key=k).primary_color
           for k in ("home_improvement", "food", "finance", "时尚美妆")}
    assert len(fam) > 1, "行业兜底表对所有行业给同一个色 —— 等于没按行业定"

    # ③ 必须不命中面:同一行业内**不再**随变体换色(那正是被移除的随机)
    same = {build_style_tokens("k", industry_key="food").primary_color
            for _ in range(5)}
    assert len(same) == 1, "同一客户同一行业还在换色 —— 随机没删干净"


def test_keyword_already_carrying_city_is_not_prefixed_again():
    """🔴 客户买的词自带城市时,顶栏不许再拼一次城市。

    生产实测:「深圳AI搜索优化」+ 城市「深圳」→「深圳深圳AI搜索优化选购指南」,
    而且是**印在图上的**,发现时只能整条重做。
    """
    got = build_style_tokens("深圳AI搜索优化", "深圳")
    assert got.header_bar.count("深圳") == 1, f"城市拼了两遍:{got.header_bar}"
    # 反向面:词里没带城市时仍要拼上,否则这条锁把功能一起删了
    plain = build_style_tokens("AI搜索优化", "深圳")
    assert plain.header_bar.startswith("深圳AI搜索优化"), "词里没带城市时应该补上"


# ── M2/M4 三类模板各自的硬要求 ──────────────────────────────
def test_cover_prompt_carries_title_and_bans_watermark():
    p = build_cover_prompt("深圳全屋定制哪家好", "5家对比", StyleTokens())
    assert "深圳全屋定制哪家好" in p
    assert "水印" in p and "3:4" in p


def test_cover_title_clipped_to_text_max():
    p = build_cover_prompt("标" * 100, "", StyleTokens())
    assert "标" * (COVER_TEXT_MAX + 1) not in p


def test_content_prompt_includes_caveat_and_metric():
    p = build_content_prompt(1, "A牌", ["价格低", "服务好"], StyleTokens(),
                             metric="3000元起", caveat="工期偏长")
    assert "3000元起" in p and "工期偏长" in p


def test_content_prompt_omits_empty_metric_gracefully():
    """反向面:没有 metric 时不能在 prompt 里留一句空要求(会让模型编数字)。"""
    p = build_content_prompt(1, "A牌", ["价格低"], StyleTokens(), metric="", caveat="c")
    assert "数字" not in p or "「」" not in p


def test_closing_prompt_carries_headline():
    p = build_closing_prompt("选购总结", "按预算对号入座", StyleTokens())
    assert "选购总结" in p


def test_closing_kinds_are_three():
    assert set(CLOSING_KINDS) == {"summary", "fit_for", "cta"}


def test_text_length_bands_match_evidence():
    """§4 实证:封面 ≤30 字,内容卡 60-150 字。"""
    assert COVER_TEXT_MAX == 30
    assert (CONTENT_TEXT_MIN, CONTENT_TEXT_MAX) == (60, 150)


# ── §6c 实测口径必须写进 prompt(不追爆款 / 信息量优先)──────
def test_prompt_states_engagement_is_irrelevant():
    """🔴 实测:采纳与点赞完全无关。这条必须写进写作 prompt,
    否则模型会默认按"新媒体爆款"套路写,方向直接反。"""
    src = CONTENT_GEN.read_text(encoding="utf-8")
    assert "点赞" in src and "无关" in src
    assert "信息完整度" in src or "信息量" in src


def test_prompt_requires_caveat_field():
    src = CONTENT_GEN.read_text(encoding="utf-8")
    assert "caveat" in src and "必填" in src


# ── 客户素材融入(Owner 追问)────────────────────────────────
def test_generator_accepts_brand_id_and_uses_context():
    """图文链必须能引用客户自有资料 —— 文章链早就有了,图文链不能没有。"""
    tree = ast.parse(CONTENT_GEN.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "generate_image_post_content")
    kwonly = {a.arg for a in fn.args.kwonlyargs}
    assert "brand_id" in kwonly, "生成器没有 brand_id 入参,接不了客户素材"
    src = ast.unparse(fn)
    assert "build_brand_context" in src, "没有真的去取客户素材"


def test_knowledge_context_only_uses_authorized_images():
    """🔴 版权面 fail-closed:只有 publish_allowed + rights_confirmed 的图才进上下文。

    🔴 断言必须打在【真 SQL 字符串】上,不能打在整份源码或 ast.unparse 的结果上 ——
       那两者都含 docstring,而 docstring 里正好解释了这两个字段,
       于是把 WHERE 条件整条删掉,断言照样绿(实测该变异存活)。
    """
    code = _code_strings(KB)
    assert "publish_allowed" in code, "SQL 里没有 publish_allowed 过滤"
    assert "rights_confirmed" in code, "SQL 里没有 rights_confirmed 过滤"


def test_empty_context_yields_no_placeholder_text():
    """没素材时必须返回空串,**不能**输出「未提供」之类占位 —— 那会诱导模型编。"""
    from services.geo_douyin.knowledge_context import BrandContext
    ctx = BrandContext(brand_name="X")
    assert ctx.is_empty is True
    assert ctx.to_prompt_block() == ""


def test_nonempty_context_renders_materials():
    """反向面:有素材时必须真的渲染出来(否则上一条恒真)。"""
    from services.geo_douyin.knowledge_context import BrandContext
    ctx = BrandContext(brand_name="X", core_selling_points="十年质保",
                       kb_snippets=["我们做过 200 套整装"])
    block = ctx.to_prompt_block()
    assert "十年质保" in block and "200 套整装" in block
    assert ctx.is_empty is False


@pytest.mark.parametrize("bad", ["competitors", "competitor_snapshots"])
def test_competitor_tables_not_queried(bad):
    """竞对表生产 0 行 —— 不接空表,免得做出"永远为空"的假功能。

    只查**真的 SQL/代码字符串**,不查解释性文字。
    """
    assert bad not in _code_strings(KB), f"代码里真的查了空表 {bad}"


def test_competitor_scan_has_discriminating_power():
    """必须不命中面:真写一句查竞对表的 SQL,检测口径要抓得到。"""
    import tempfile  # noqa: F401 - 只构造 AST,不落盘
    fake = ast.parse('def f():\n    """不用竞对表"""\n    q = "SELECT * FROM competitors"\n')
    docs = set()
    for node in ast.walk(fake):
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docs.add(id(body[0].value))
    strings = [n.value for n in ast.walk(fake)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in docs]
    assert any("competitors" in s for s in strings), "检测器抓不到真查询 = 恒真锁"


# ─────────────────────────────────────────────────────────────
# 🔴 张数预算(自审抓到的真缺陷回归锁)
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("want", [1, 2, 3, 4, 5, 9])
def test_group_never_exceeds_requested_card_count(want):
    """生成张数**绝不能超过用户要的卡数**。

    真缺陷:封面+内容+收尾是恒定三段,want=1 出 3 张、want=2 出 4 张。
    用户要 1 张拿到 3 张 —— §6 实证单图占 23%,这是常用路径不是边角。
    """
    content_count = max(1, want - 2) if want >= 3 else want

    class _C:
        cover = {"title": "深圳全屋定制哪家好", "subtitle": "x"}
        closing = {"headline": "选购总结", "summary": "s"}
        cards = [{"entity": f"E{i}", "points": ["p"], "caveat": "c"}
                 for i in range(content_count)]

    specs = build_prompts_for_group(_C(), StyleTokens(), max_cards=want)
    assert len(specs) <= want, f"want={want} 却生成 {len(specs)} 张"


def test_cover_is_kept_first_when_budget_is_tight():
    """预算不够时优先保封面(钩子最重要),不是随便截。"""
    class _C:
        cover = {"title": "封面", "subtitle": ""}
        closing = {"headline": "收尾", "summary": "s"}
        cards = [{"entity": "E1", "points": ["p"], "caveat": "c"}]

    specs = build_prompts_for_group(_C(), StyleTokens(), max_cards=1)
    assert [o["kind"] for o in specs] == ["cover"]


def test_budget_zero_means_no_cap():
    """反向面:不传预算时不得截断,否则默认路径会被悄悄砍卡。"""
    class _C:
        cover = {"title": "封面", "subtitle": ""}
        closing = {"headline": "收尾", "summary": "s"}
        cards = [{"entity": f"E{i}", "points": ["p"], "caveat": "c"} for i in range(3)]

    assert len(build_prompts_for_group(_C(), StyleTokens())) == 5


def test_idx_is_renumbered_after_truncation():
    """截断后 idx 必须连续,否则落库的卡序会有空洞。"""
    class _C:
        cover = {"title": "封面", "subtitle": ""}
        closing = {"headline": "收尾", "summary": "s"}
        cards = [{"entity": f"E{i}", "points": ["p"], "caveat": "c"} for i in range(4)]

    specs = build_prompts_for_group(_C(), StyleTokens(), max_cards=3)
    assert [o["idx"] for o in specs] == [1, 2, 3]


# ═════════════════════════════════════════════════════════════
# 封面质感升级(Owner 2026-08-02 裁定 · 对标 10 母版 01/04 文字主导风格)
# ═════════════════════════════════════════════════════════════
def _cover(title="深圳全屋定制哪家好？5 家实测对比", sub="报价区间、工期一次说清"):
    return build_cover_prompt(title, sub, StyleTokens(primary_color="宝蓝"))


def _cover_subject(**kw) -> str:
    """只取封面 prompt 的【主体】段。

    🔴 必须切段再断言:背景图层这件事在【主体】和【风格】(style_def)里各写了一次,
       整串断言时删掉【主体】那句、【风格】那句还在,锁照样绿(实测该变异存活)。
       断言要打在"排版指令"这一段上,不能被"风格描述"顶包。
    """
    p = _cover(**kw)
    assert "【主体】" in p and "【风格】" in p, "五要素分段没了 —— 锚点失效"
    return p.split("【主体】", 1)[1].split("【风格】", 1)[0]


def test_cover_subject_section_is_isolated():
    """必须不命中面:切出来的【主体】里不能含【风格】段的内容,否则上面的隔离是假的。"""
    subject = _cover_subject()
    assert "扁平" not in subject
    assert "唯一主强调色" not in subject, "切段没生效,风格段漏进来了"


def test_cover_has_industry_backdrop_layer():
    """🔴 母版质感的来源是【真实行业场景照片压暗作背景层】。

    没有这一层,封面就退回"纯色卡片",这正是 Owner 对比后要改的点。
    断言打在【主体】段 —— 那是真正指挥排版的地方。
    """
    subject = _cover_subject()
    assert "真实行业场景" in subject, "封面【主体】里缺行业背景图层"
    assert "压暗" in subject, "背景层没压暗 —— 压字会读不清"


def test_cover_states_explicit_size_hierarchy():
    """层级要写死字号比例。母版靠 2-3 倍字号差做层次,不写模型会拉平。"""
    subject = _cover_subject()
    assert "三分之一" in subject, "副标题没有显式字号比例"
    assert "窄色条" in subject, "副标题没有压色条 —— 层级三件套里的'色块底'丢了"


def test_size_hierarchy_only_applies_when_subtitle_exists():
    """反向面:没有副标题时不该凭空冒出字号比例(那会指挥模型画一条不存在的副标题)。"""
    subject = _cover_subject(sub="")
    assert "三分之一" not in subject


def test_cover_keeps_three_hard_rules():
    """🔴 三硬律一条都不能因为改质感而丢:

    ① 大字标题占上半屏 ② 标题逐字就是查询词 ③ 缩略图状态可读
    """
    p = _cover()
    assert "超大号主标题" in p and "占据上半屏高度" in p, "硬律①:大字标题丢了"
    assert "逐字就是" in p and "不要改写成创意标题" in p, "硬律②:标题=查询词丢了"
    assert "缩略图" in p, "硬律③:缩略图可读丢了"


def test_cover_title_appears_verbatim_twice():
    """标题在 prompt 里出现两次(排版位置 + 逐字约束),少一次约束就松了。"""
    title = "深圳全屋定制哪家好？5 家实测对比"
    p = build_cover_prompt(title, "副标题", StyleTokens())
    assert p.count(title) >= 2, f"标题只出现 {p.count(title)} 次"


def test_design_text_preset_is_dark_editorial_not_flat_whitespace():
    """必须不命中面:default 款不能退回"扁平矢量 + 大面积留白"。

    那是改版前的写法,与母版质感方向相反 —— 留着它这次升级等于没做。
    """
    sd = STYLE_PRESETS["design_text"].style_def
    assert "近黑底" in sd and "真实行业场景照片" in sd, "默认款没有深色编辑风质感"
    assert "大面积留白" not in sd, "默认款仍是改版前的扁平留白写法"
    assert "矢量插画质感" not in STYLE_PRESETS["design_text"].tech_params, (
        "技术参数仍是矢量插画,与照片背景层自相矛盾")


def test_group_texture_block_is_verbatim_in_all_three_cards():
    """🔴 裁定①:三类卡的质感描述必须是**逐字同一段**。

    Owner 实测「封面精美、内容卡/收尾卡粗糙,组内撕裂」,根因已定位:
    排版级质感当初只写进了封面的【主体】(cover 208 字 / content 165 / closing 仅 50)。

    🔴 锁打在"同一段整体出现"这个**结构**上,不打关键词 ——
       关键词会和 style_def 重复,那种锁删一处另一处还在(C9 就是这么存活过一次的)。
    """
    from services.geo_douyin.card_templates import group_texture_block
    st = StyleTokens(primary_color="宝蓝", header_bar="选购指南", footer_bar="来源")
    p = STYLE_PRESETS["design_text"]
    block = group_texture_block(st)
    assert len(block) > 60, "共享质感段太短,等于没写"

    for name, prompt in (
        ("封面", build_cover_prompt("标题", "副标题", st, p)),
        ("内容卡", build_content_prompt(1, "A牌", ["要点"], st, preset=p)),
        ("收尾卡", build_closing_prompt("总结", "小结", st, p)),
    ):
        assert block in prompt, f"{name}没有拿到组级质感段 —— 又变成只富养封面"


def test_group_texture_block_carries_layout_discipline():
    """共享段必须真的带排版纪律,不能是一句空话。"""
    from services.geo_douyin.card_templates import group_texture_block
    block = group_texture_block(StyleTokens(primary_color="宝蓝"))
    assert "真实行业场景" in block and "压暗" in block, "缺背景图层"
    assert "2-3 倍" in block, "缺字号层级"
    assert "压色块底" in block, "缺色块底纪律"
    assert "缩略图" in block, "缺缩略图可读"


def test_group_texture_locks_one_scene_for_the_whole_set():
    """🔴 一组只用一个场景 —— 规范 §2.2,也是产出与母版差最远的一处。

    在此之前共享段只说「与主题相关的真实行业场景照片」,**没说是哪一个场景**,
    逐张独立生图时每张各想一个,七张凑不成"一个场景世界"。
    母版的专业感很大一部分正来自七张同一个空间。
    """
    from services.geo_douyin.card_templates import group_texture_block
    with_scene = group_texture_block(
        StyleTokens(primary_color="深棕红", accent_color="暖橙",
                    scene="苗家酸汤堂食区，暖木色调"))
    assert "苗家酸汤堂食区" in with_scene, "场景没进共享段 —— 每张会各想一个"
    assert "不要换场景" in with_scene, "没有禁止换场景的约束"
    # 没有场景时也要有兜底口径,不能整句消失
    assert "同一套真实行业场景" in group_texture_block(StyleTokens())


def test_accent_color_follows_the_client_not_a_hardcoded_amber():
    """🔴 强调色不许写死「琥珀黄」—— 那是家装样张的配色。

    写死 = 所有客户长一个样,正好是规范 §8.9 警告的串味,工具也就没有价值了
    (Owner 2026-08-05:「剩下的是我们需要根据不同用户来为他定制的画面和文案」)。
    """
    from services.geo_douyin.card_templates import group_texture_block
    blk = group_texture_block(StyleTokens(primary_color="深棕红", accent_color="暖橙"))
    assert "暖橙" in blk, "强调色没跟着客户走"
    assert "琥珀黄" not in blk, "强调色还写死着琥珀黄 —— 所有客户会长一个样"


def test_three_card_types_still_differ_in_structure():
    """必须不命中面:共享的是**质感**,不是整段 prompt。

    三类卡各自的结构指令必须仍然不同 —— 否则等于把三张卡写成同一张。
    """
    st = StyleTokens(primary_color="宝蓝", header_bar="选购指南", footer_bar="来源")
    p = STYLE_PRESETS["design_text"]
    from services.geo_douyin.card_templates import group_texture_block
    block = group_texture_block(st)
    uniq = {
        name: prompt.split("【主体】", 1)[1].split("【", 1)[0].replace(block, "")
        for name, prompt in (
            ("cover", build_cover_prompt("标题", "副标题", st, p)),
            ("content", build_content_prompt(1, "A牌", ["要点"], st, preset=p)),
            ("closing", build_closing_prompt("总结", "小结", st, p)),
        )
    }
    assert len(set(uniq.values())) == 3, "三类卡的特有结构变成一样了"
    for name, body in uniq.items():
        assert len(body.strip()) > 40, f"{name} 的特有结构被掏空了({len(body.strip())} 字)"

    # 🔴 只判"三段互不相同"不够:把收尾卡开头换成封面的说法、后半段留着,
    #    三段照样"不同",锁照样绿(实测该变异存活)。必须判**有没有串味**:
    #    每类卡只能出现自己那套结构词,不能出现别类的。
    # 判别词必须是**各自唯一**的:选「标题条」就不行 —— 封面里也有
    #("标题条底衬…色块"),那样这条锁会自己把自己判红。用三个开头标识。
    OWN = {"cover": "本张是封面", "content": "张内容卡", "closing": "本张是这一组的收尾卡"}
    for name, body in uniq.items():
        assert OWN[name] in body, f"{name} 丢了自己的结构词 {OWN[name]}"
        for other, word in OWN.items():
            if other != name:
                assert word not in body, (
                    f"{name} 的结构里混进了 {other} 的说法「{word}」—— 三类卡串味了")


def test_closing_card_is_no_longer_the_poor_relation():
    """回归锁:收尾卡曾经主体只有 50 字(全组最贫瘠),不能再退回去。"""
    st = StyleTokens(primary_color="宝蓝", header_bar="选购指南", footer_bar="来源")
    p = STYLE_PRESETS["design_text"]
    subj = build_closing_prompt("总结", "小结", st, p).split("【主体】", 1)[1].split("【", 1)[0]
    assert len(subj.strip()) > 150, f"收尾卡主体又缩回去了({len(subj.strip())} 字)"


def test_design_text_uses_one_primary_and_one_secondary_accent():
    """母版的配色纪律:单一主强调色 + 一个只给小标签的次强调色。

    不写死的话模型会满屏撞色,层次就没了。
    """
    sd = STYLE_PRESETS["design_text"].style_def
    assert "唯一主强调色" in sd, "没有限定单一主强调色"
    assert "次强调色" in sd and "只用在小标签块上" in sd, "次强调色没有限定用途"
