"""GEO 抖音图文 · 卡片模板 SSOT(首图 / 内容卡 / 尾卡 三类 + 组内风格一致性参数)

模板不是拍脑袋的,逐条对应 §6b 方法论(docs/AI-CONTEXT/DOUYIN_IMAGE_CARD_PATTERNS_2026-08.md):
  M1 五种骨架          → SKELETONS
  M2 首图钩子 + 四必备件 → COVER_RULES
  M3 内容卡同构字段     → CONTENT_FIELD_SCHEMA
  M4 文字层级三件套     → LAYER_RULES / 字数上限
  M6 组内一致性三参数   → StyleTokens
  M7 收口卡三选一       → CLOSING_KINDS

🔴 与 §6c 引用规则实测的关系(为什么这么定):
  - 采纳与**点赞热度完全无关**(中位 12 vs 14)→ 模板不追"爆款感",追**信息完整度**;
  - **标题信息量是最强单一信号**(中位 70 字 vs 55)→ 首图标题不追求短促,要把信息说全;
  - hashtag/数字对采纳零贡献 → 不在卡面上堆这些,留给文案区(帮进抖音检索池)。
"""
from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("GEO-Douyin-Cards")

from services.geo_douyin.config import (
    ASPECT_RATIO_DEFAULT,
    ASPECT_RATIOS,
    normalize_aspect_ratio,
)

# ── M1 五种骨架 ──────────────────────────────────────────────
# key -> (中文名, 内容卡的组织方式说明, 是否 GEO 默认)
SKELETONS: Dict[str, Tuple[str, str, bool]] = {
    "per_entity":    ("逐实体", "每个候选(品牌/机构/产品)一张卡,字段结构完全同构", True),
    "per_question":  ("逐问题", "每个子问题一张卡,顶部固定同一条标题条", False),
    "per_feature":   ("逐卖点", "只推一个主体,每个卖点一张卡", False),
    "pro_con":       ("优缺点", "优点卡 + 缺点卡 对照", False),
    "pain_solution": ("痛点方案", "痛点列表卡 + 方案卡", False),
}
DEFAULT_SKELETON = "per_entity"   # GEO 场景要吃「XX 哪家好」→ 天然产出候选清单

# ── M2 首图钩子(实证占比见 §3)──────────────────────────────
COVER_HOOKS: Dict[str, str] = {
    "ranking":  "榜单式(TOP N / 排行榜 / 红榜 / 十大)",
    "question": "疑问式(怎么选 / 哪家好 / 哪个牌子好)",
    "warning":  "避坑式(别乱买 / 别乱报 / 防踩坑 / 避坑)",
}

# 🔴 2026-08-03 由 `question` 改为 `warning`。这是本批最贵的一条订正,
#    因为**原来那个默认值在我们的试点行业上是负向的**。
#
#    实测(生产飞轮全量 5,593 条抖音信号,同层内带该特征 vs 不带的采纳率差):
#        疑问式:B端 +0.6pp / C端 +0.0pp / 家装(试点) **−5.2pp**
#        榜单式:B端 +2.8pp / C端 +0.4pp / 家装(试点) **−7.1pp**
#        避坑式:B端 +12.7pp / C端 +10.0pp / 家装 +11.9pp / 文娱 +21.5pp
#               / geo服务 +21.6pp / technology +9.7pp —— **唯一跨所有分层都正向**
#
# 🔴 当初选 question 是**判据用错了**:我看的是「§6 实证里疑问式占比高」——
#    那是**分布占比**,不是**提升**。一个 90% 内容都带的特征,在采纳组里
#    当然也占 90%,它什么都不说明。判据必须是**同层内的 lift**。
#    这个错误形状值得记住:占比高 ≠ 有效。
#    报告:docs/AI-CONTEXT/RESEARCH_DOUYIN_EXPRESSION_BY_SEGMENT_2026-08-03.md
DEFAULT_HOOK = "warning"

# 🔴 钩子**按行业选**,不是一个全局默认走天下 —— 同一个表达换个行业结论会反过来。
#    只列**实测有显著正向**的那几个行业;不在表里的一律走 DEFAULT_HOOK(避坑式),
#    因为避坑式是唯一跨层都正的那个,拿它当兜底最不容易错。
INDUSTRY_HOOK: Dict[str, str] = {
    # 疑问式在这两个行业**才**是正的(其余行业多为负)
    "时尚美妆": "question",      # 疑问 +7.3pp
    "education": "question",     # 疑问 +15.2pp(该行业最强)
}


def _ikey(industry_key: str) -> str:
    """自由文本 → 受控枚举键。**本模块所有按行业查表的地方都要先过这一跳。**

    🔴 2026-08-08:这里补的是一个**已经修过一次、但只修了一半**的洞。
       2026-08-06 那一包(`test_geo_douyin_industry_key_wiring_2026_08_06.py`)把
       前端"根本不传 industry_key"改成了"传",目标原话是让钩子/B·C端/配色
       按行业生效。但前端传的是**品牌档案里的自由文本**
       (「家居制造业 / 高端整木全屋定制/木作高定行业(…)」),而本模块四张表
       用的全是**受控枚举**(`时尚美妆` / `business_service` / `home_improvement`…)。
       于是 `.get(自由文本)` 一律落兜底 —— 接线接对了、键空间没对,**修了一半**,
       而且和修之前一样一声不响。

       归一 SSOT 已存在,不在本模块另立第二份;它对生产池 17 个键实测全部幂等,
       所以这一跳只会让本该生效的生效,不会改变已经命中的那些。
    """
    from services.media_entity_flywheel import normalize_industry_key

    raw = str(industry_key or "").strip()
    if not raw:
        return ""
    return normalize_industry_key(raw)


def pick_hook(industry_key: str = "") -> str:
    """按行业选首图钩子。不认识的行业 → 避坑式(唯一跨层都正向的那个)。"""
    return INDUSTRY_HOOK.get(_ikey(industry_key), DEFAULT_HOOK)


# ── B端 / C端 的表达差异(实测,§2)──
# 🔴 不是程度差异,是**方向差异**:同一个手法在两端一正一负。
_B_SIDE_INDUSTRIES = frozenset((
    "business_service", "technology", "geo_优化服务", "电梯行业",
))


def is_b_side(industry_key: str) -> bool:
    return _ikey(industry_key) in _B_SIDE_INDUSTRIES


def audience_style_block(industry_key: str = "") -> str:
    """把 B端/C端 的表达差异写成 prompt 约束。

    实测依据(同层 lift):
      含数字      B端 **+8.0pp** / C端 +1.0pp     → B端要摆数字,C端不强求
      干货教程    B端 **−2.9pp** / C端 +2.8pp     → B端**禁**"攻略/指南/科普"字样
                                                    (technology 更是 −7.7pp)
      厂家自荐    B端 +3.0pp / C端 **+21.3pp**    → C端可自报源头身份
      价格词      B端 +4.1pp / C端 **+6.2pp**     → C端更认价格信息
      第一人称    B端 +10.4pp / C端 +4.5pp        → 两端都要(见 M5)
    """
    if is_b_side(industry_key):
        return ("面向的是**企业采购方**:每张卡至少给一个可比较的数字"
                "(参数/工期/规模/年限);**不要**出现「攻略/指南/科普/教程」"
                "这类字样,他要的是结论和参数,不是教学;")
    return ("面向的是**个人消费者**:讲清楚价格区间或花钱的地方;"
            "可以点明自己是源头厂家/自营(不吹不比较,只说身份);"
            "不必硬凑数字;")

def promote_block(brand_name: str, city: str = "", industry_key: str = "") -> str:
    """这条内容是**给谁拉生意**的 —— 整条 prompt 里优先级最高的一段。

    🔴 2026-08-05 新增。在此之前 prompt 里只有一行 `品牌:{brand_name}`,
       **没有任何一句告诉模型拿这个品牌做什么**,于是模型把它当背景资料。
       生产实测:一条 390 算力做出来的内容通篇不出现客户名字 ——
       豆包就算引用了,答案里也没有客户。Owner:「我们的目的是推广,不是搞慈善」。

    🔴 2026-08-06 订正 · **口吻改回第三方,但"必须点名"不变**(Owner:「一定要
       拒绝自卖自夸」)。我上一版让模型「以服务方本人的口吻写(我们)」,产出是
       「**我们**全域上榜（深圳）科技有限公司,2018年就开始做…已服务500+企业」——
       一段自述广告。三条实证说明那是我做错了:

       ① **数据越界**:那份 lift 报告的样本是 `metadata->>'title'`(5593 条**标题**),
          里面没有任何一条测过**正文人称**。我拿标题面的结论去定说话人。
       ② **两个特征被我混成一个**(报告 §2 原本是分开两行):
            第一人称(我/亲测/实测)  B端 +10.4 / C端 +4.5 / geo_优化服务 **+27.7**
                                    ← 这个"我"是**测评者**
            厂家自荐(源头/直销/工厂) B端 **+3.0** / C端 +21.3
                                    ← 这个"我们"是**卖方**
          图文链的试点客户是 B 端,我给 B 端用的正是那条几乎无效的 +3.0,
          反而把全行业最稳的 +27.7(测评者第一人称)丢了。
       ③ **人设穿帮**:内容走**素人矩阵**分发,发的账号不是品牌本人。
          以「我们某某公司」开口,账号身份和口吻当场对不上。
       还有一层:目标是被豆包**引用**。自述式推广更容易被当成营销文案,
       而不是可引用的事实来源。

    🔴 但**不许退回"纯中立测评"** —— 那正是 08-05 那次 P0(通篇不点名客户,
       390 算力做公益)。第三方口吻和点名客户不冲突,冲突的是把"点名"写成"自荐":
         ❌ 我们全域上榜（深圳）科技有限公司,2018年就开始做…
         ✅ 全域上榜（深圳）科技有限公司是 2018 年就开始做这块的,自研 OmniRank AI™…
       同一个名字、同一批事实,立场从"卖方自述"换成"被评对象"。

    🔴 **可信是手段,被推荐是目的,不是二选一**:广告法极限词照禁、数字仍只能来自
       知识库、caveat 照旧必填。这一段管的是**立场**,不放松任何事实约束。
    """
    # 🔴 `str(... or "")` 而不是直接 .strip():city/brand_name 都可能是 None
    #    (调用方 `city or None` 的写法很常见)。这段是**每条内容都要走**的路径,
    #    在这里抛 AttributeError = 整条生产失败,而原因看起来跟自荐毫无关系。
    who = str(brand_name or "").strip()
    if not who:
        # 名字都没有就别硬写:让调用方失败,好过产出一条谁也没推荐的内容
        return ("⚠️ 这一条没有拿到客户名称 —— **不要**编一个公司名或写成匿名推荐,"
                "把 closing.brand_line 留空,由上游判失败。")
    loc = str(city or "").strip()
    # 两端**都是第三方口吻**,差别只在"拿什么当依据":
    # B端摆可核验的数字(含数字 +8.0pp),C端讲来历和做法(数字对 C端只有 +1.0pp)。
    if is_b_side(industry_key):
        stance = (f"以**亲自查过/对比过的人**的口吻写。"
                  f"第一人称只能用在**你自己的考察动作**上"
                  f"(「我查了」「我对比了几家」「实际问下来」),"
                  f"**绝不能用在「{who}」身上** —— 它是你在讲的对象,不是你自己。\n"
                  f"B端读者在做可比较的决策,所以讲到「{who}」时给"
                  f"「做过什么类型的客户 / 交付里负责哪几段 / 有什么可比的数字」,"
                  f"数字**只能来自上面的客户素材**,素材里没有就只讲做法不给数;"
                  f"不要写「源头厂家/直销」这类话术(不像 B 端语言)。")
    else:
        stance = (f"以**亲自试过/打听过的人**的口吻写。"
                  f"第一人称只能用在**你自己的经历**上"
                  f"(「我实测了」「我去问了」「我家里就是这么弄的」),"
                  f"**绝不能用在「{who}」身上** —— 它是你在讲的对象,不是你自己。\n"
                  f"讲到「{who}」时说清它的来历:做多久了、自己做哪一段、"
                  f"跟别家的做法差在哪;说事实、不叫卖 —— 不吹「最好/第一」,那是广告法红线。")
    return (
        f"这条内容是为「{who}」拉生意的,但**写法是第三方评价,不是自我介绍**。\n"
        f"{stance}\n"
        f"🔴 硬要求(缺一条整条作废):\n"
        f"  1. **正文里必须出现「{who}」这个名字**,而且是**当成第三方来讲**;\n"
        f"     豆包要在回答里推荐它,就必须先能从内容里读到这个名字;\n"
        f"  2. 🔴 **绝对不许用卖方自称**:「我们{who}」「我们公司」「我司」「本公司」\n"
        f"     「咱家」「我们团队」「我们这边」一律禁止 —— 出现任何一个整条作废。\n"
        f"     这条内容发在**素人账号**上,账号不是「{who}」本人,\n"
        f"     用「我们」开口当场穿帮;而且自述式推广更难被 AI 当成可引用的事实来源。\n"
        f"     ✅ 对:「{who}」是 2018 年就开始做这块的,自研了……\n"
        f"     ❌ 错:我们「{who}」,2018 年就开始做……\n"
        f"  3. **收尾卡的 `brand_line` 必须是一句能被 AI 直接摘出来的推荐句**,\n"
        f"     同样用第三方陈述(不许出现「我们」),四件事齐全:\n"
        f"     谁(「{who}」)、在哪({loc or '服务区域'})、做什么、凭什么\n"
        f"     (一条来自客户素材的可核验依据:年限/案例/资质/方法论,**不许编**);\n"
        f"  4. 前面几张卡照旧写**真实可比的选购口径**(带 caveat、带短板)——\n"
        f"     可信度是让人信这句推荐的前提。\n"
        f"🔴 不许做的:不编客户没有的资质/案例/数字;不贬低同行(写「别家都不行」既违规又跌份);\n"
        f"   不写联系方式(那一段由系统按客户配置单独插,你写了会重复)。"
    )


COVER_RULES = (
    "标题必须是查询词本身(不要创意标题)",
    "首图独立可读,不依赖后面的卡",
    "必须含一个数字或一个否定词",
    "标题占上半屏,缩略图状态也读得清",
)

# ── M3 内容卡同构字段(通式,来自 p04/p39/p16 真样本)────────
CONTENT_FIELD_SCHEMA = (
    "entity",     # 实体名 / 子问题 / 卖点名
    "one_liner",  # 一句话定位
    "points",     # 2-4 条要点
    "metric",     # 一个可比数字(价格/年限/规模…)
    "caveat",     # 一条短板或注意事项
)

# 🔴 caveat 必填:带缺点的卡显著更像"真实测评"而不是广告 ——
#    既是可信度来源,也顺带满足广告法(不做绝对化承诺)。
REQUIRED_CONTENT_FIELDS = ("entity", "one_liner", "points", "caveat")

# ── M7 收口卡 ───────────────────────────────────────────────
CLOSING_KINDS: Dict[str, str] = {
    "summary":  "选购总结",
    "fit_for":  "适合谁",
    "cta":      "下一步/互动提问",
}
DEFAULT_CLOSING = "summary"

# ── M4 文字密度(§4 实证)────────────────────────────────────
COVER_TEXT_MAX = 30       # 首图 ≤30 字
CONTENT_TEXT_MIN = 60     # 内容卡 60-150 字
CONTENT_TEXT_MAX = 150
CLOSING_TEXT_MAX = 80

# 🔴 `brand_line` 的上限**与卡面无关** —— 它不印在图上
#    (`build_closing_prompt` 只印 headline/summary/caveat/contact),
#    它是给豆包整句摘走的那一句推荐语,只落库。
#    所以约束它的不是版面,是"一句话能不能说完谁/在哪/做什么/凭什么"。
#    本机真跑 12 次(2026-08-06):中位 70 / max 104 / **33% 超过原来的 80**,
#    被截掉的正是"凭什么"那半句。取 140 = 观测 max × 1.35,留够余量;
#    再大就不是"一句话"了,AI 摘起来也不利落。
BRAND_LINE_MAX = 140

# 句读符号,按"能不能当断点"分两档:
#   强断点 —— 断在这里读起来是完整一句
#   弱断点 —— 断在这里是半句,但总好过断在词中间
_STRONG_BREAKS = "。！？；!?;"
_WEAK_BREAKS = "，,、"
# 回退不能太狠:退到只剩三成内容,不如硬切。
_MIN_KEEP_RATIO = 0.6


#: 一行"看起来像 Python/JSON 列表 repr"的形状。
#: 🔴 [WO_262] 用来在出口处把「归一化漏了」认出来:
#:    `"['① 先列预算表', '② 再按…']"` 这种串上了图也 OCR 不到,
#:    最后以「第 4 张少了:['①…','②…']」的样子出现在客户面前。
_LIST_REPR_RE = re.compile(r"^\s*[\[\(].*[\]\)]\s*$", re.S)

#: 开头是 `['` / `("` 这种「括号后**紧跟引号**」的样子。
#: 🔴 [WO_265] 收窄点就在"紧跟":`[重要] 请注意` / `(含税)` / `[1] 第一步`
#:    括号后面跟的是字不是引号,一律不算。
_LIST_REPR_HEAD_RE = re.compile(r"^\s*[\[\(]\s*['\"]", re.S)

#: 元素分隔:引号 + 逗号 + 引号。**不能只按逗号切** ——
#: 中文正文里本来就有「,」,按逗号切会把一句话拆成两句。
_LIST_REPR_SEP_RE = re.compile(r"['\"]\s*,\s*['\"]")


def looks_like_list_repr(text: object) -> bool:
    """这行文字是不是一个**列表被 str() 之后**的样子(**含被截断的**)。

    判据故意**收窄**:括号后必须**紧跟引号**,
    再加一个「有元素分隔」或「以引号收尾」——
    只看括号会把「(含税)」「[重要] 请注意」「[1] 第一步」这类正常文案误判。

    🔴 [WO_265 2026-09-23] 原来还要求**两端**都是括号,于是**被截断的 repr**
       (旧 `clip_text` 按句边界切掉了闭合 `]`)一律判 False ——
       生产 8 条存量里有 **3 条**是这种(含客户 post 43),
       它们在 WO_262 之后仍然原样上图、原样回给客户,
       而出口锁用的也是这个谓词,所以**连一句 warning 都不会出**。
       教训:谓词只认"完整形状"时,**残缺的同类反而全部漏网**。
    """
    s = str(text or "")
    if not _LIST_REPR_HEAD_RE.match(s):
        return False
    if _LIST_REPR_SEP_RE.search(s):
        return True
    # 只有一个元素:闭合的 `['x']` 或被截断但仍以引号收尾的 `['x'`
    return s.rstrip().endswith(("'", '"', "']", '"]', "')", '")'))


def flatten_text(value: object) -> str:
    """把模型可能回的 **list / 嵌套 list / 列表串** 归一成一段多行文字。

    🔴 [WO_262 2026-09-22 · 客户反馈] 起因:出参模板写 `"summary": "2-4 行小结"`,
       模型常按"行"回一个 **JSON 数组**;而这里原来是 `str(text or "")` ——
       列表被 `str()` 成 `"['① …', '② …']"` 原样落库,再被印进出图 prompt、
       又被当成"必须逐字出现"的一行,OCR 当然找不到,于是客户看到
       「第 4 张少了:['①…','②…']」。生产 41 条图文里 **8 条**中招。

    形状三种都收:
      · `["a", "b"]`      → `"a\\nb"`
      · `[["a"], ["b"]]`  → 拍平后同上
      · `"['a', 'b']"`    → **先解析回列表**再拼(存量那 8 条就是这种 repr,
                            注意它是 **Python repr(单引号)**,不是合法 JSON,
                            所以 `json.loads` 解不了 —— 要用 `ast.literal_eval`)

    🔴 用**换行**拼不用「、」:小结本来就是多行,拼成一行会挤成一坨上不了版。
    🔴 解析失败就**原样返回**:文案里正常出现的 `[重要] 请注意` 不许被动。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts: List[str] = []
        for item in value:
            piece = flatten_text(item).strip()
            if piece:
                parts.append(piece)
        return "\n".join(parts)
    if isinstance(value, str):
        s = value.strip()
        if looks_like_list_repr(s):
            for parse in (json.loads, ast.literal_eval):
                try:
                    parsed = parse(s)
                except Exception:
                    continue
                if isinstance(parsed, (list, tuple)):
                    return flatten_text(parsed)
            # 🔴 [WO_265] 解析不了 ⇒ 多半是**被截断的 repr**(没有闭合 `]`)。
            #    不补 `']` 再解析:尾元素被切在**中间**时(生产 post 41 就是
            #    ——尾巴是「开发票」,连收尾引号都没有)补齐照样解析失败,
            #    那样又要"再退一步",退到最后还是下面这条路。所以直接走它。
            #    **尾部半个元素保留成一行**:它本来就会被印上图,
            #    半句话比一整串带方括号的 repr 强得多。
            return _split_truncated_list_repr(s)
        return value
    return str(value)


def _split_truncated_list_repr(s: str) -> str:
    """按元素分隔切开一个(可能被截断的)列表 repr,剥掉两端引号。

    只在 :func:`looks_like_list_repr` 已经点头之后调用 —— 它不自己判形状。
    """
    body = s.strip().lstrip("[(").rstrip("])").strip()
    parts = _LIST_REPR_SEP_RE.split(body)
    out: List[str] = []
    for idx, piece in enumerate(parts):
        t = piece.strip()
        if idx == 0:
            t = t.lstrip("'\"")
        if idx == len(parts) - 1:
            t = t.rstrip("'\"")
        t = t.strip()
        if t:
            out.append(t)
    return "\n".join(out) if out else s


def clip_text(text: str, limit: int, keep: str = "") -> str:
    """按句边界截断。**全链唯一截断入口。**

    🔴 [WO_262] 第一行原来是 `s = str(text or "")` —— 模型回列表时它把
       `['a','b']` 变成一串带方括号的文字。改成先过 :func:`flatten_text`:
       **本函数是全链唯一截断入口,所以也是收口这件事的唯一合适位置** ——
       在 40 个调用点各写一次归一化,迟早有一处跟不上。

    keep:一段"截断后必须还在"的文字(用于 `brand_line` 里的客户名)。
        断点落在它前面时,本函数**返回空串**而不是交出半句 ——
        理由见下面第二段:半句推荐语比没有推荐语更糟,
        而且它会让失败原因指向一个完全无关的地方。

    🔴 2026-08-06 · 生产 post 19 实证:原来三处全是 `[:N]` 盲切,后果逐条可见 ——
         summary    切在「…白帽打法的公司，比如全」 ← **客户名被切成「比如全」**
         caveat     切在「…但如果是靠线上」
         brand_line 切在「…能用多引擎实测数据告」   ← 那是给豆包整句摘的推荐句
       本机真跑 LLM 复测,第一条就切掉了「不承诺固定排名。」——
       **合规声明被砍掉**,比读着别扭严重得多。

    🔴 还有一个更隐蔽的连锁:`promotes_brand` 判的是截断**之后**的文本。
       品牌名要是落在上限之后,盲切会把它切没 → 整单报 `brand_not_promoted`。
       也就是说盲切会**按名字的位置随机报废整单**,而失败原因完全指不到真因。

    🔴 为什么放在这里、且必须只有一个:同一段文字在三处各截一次
       (content_generator 落库 / build_closing_prompt 进 prompt /
        frozen_text_lines 给 OCR 核验),三处口径必须逐字一致 ——
       原来靠注释里一句"改一处要改两处"维持,那不是机制。
    """
    s = flatten_text(text).strip()          # [WO_262] 原为 str(text or "")
    must = str(keep or "").strip()
    if len(s) <= limit:
        return s
    floor = int(limit * _MIN_KEEP_RATIO)
    window = s[:limit]
    out = ""
    # ① 优先断在强断点(读起来是完整一句)
    for breaks in (_STRONG_BREAKS, _WEAK_BREAKS):
        pos = max((window.rfind(ch) for ch in breaks), default=-1)
        if pos >= floor:
            cut = window[:pos + 1]
            # 以逗号/顿号收尾读着像没写完,去掉它
            out = cut.rstrip(_WEAK_BREAKS) or cut
            break
    # ② 窗口里没有可用断点 —— 硬切,但不留半个标点
    if not out:
        out = window.rstrip(_WEAK_BREAKS + _STRONG_BREAKS) or window
    # ③ 必留内容被切掉了 → 交空串,不交半句
    #    生产实证:截断把「…比如全域上榜（深圳）科技有限公司」砍成「比如全」,
    #    印在图上就是「比如全」。而 `promotes_brand` 判的是**截断之后**的文本,
    #    所以名字被切掉还会让整单报 `brand_not_promoted` —— 失败原因完全指不到真因。
    #    宁可这一句不要(正文那一处仍会点名),也不发半句推荐语。
    if must and must in s and must not in out:
        return ""
    return out


LAYER_RULES = (
    "字号差:主标题是正文的 2-3 倍",
    "色块底:标题压在纯色条上,不要裸字",
    "彩色高亮:正文里 1-2 处关键结论用对比色标出",
)


@dataclass
class StyleTokens:
    """M6 组内一致性的三个显式参数。**同一组卡必须传同一份**。

    做成参数而不是让模型"保持风格一致" —— 后者在逐张独立生图时根本做不到。
    """
    primary_color: str = "深宝蓝"     # 统一主色 + 边框色
    # 🔴 2026-08-05 新增。规范 §2.3 的视觉身份是**主色 + 强调色**两层
    #    (样张:深宝蓝底 + 琥珀黄强调),原来只有一个色 —— 缺了强调色,
    #    模型就只能自己找一个,组内七张各找各的,一致性从这里破。
    accent_color: str = "琥珀黄"
    # 🔴 整组共用的真实摄影场景。规范 §2.2「一组只使用一套真实行业场景」——
    #    原来**这个字段根本不存在**,每张的照片由模型各自想象,
    #    所以七张凑不成"一个场景世界",这是产出与母版差最远的一处。
    scene: str = ""
    header_bar: str = ""             # 固定顶部标题条文案
    footer_bar: str = ""             # 固定底部信息条(出处/提示)
    font_style: str = "无衬线黑体，标题加粗"

    def to_dict(self) -> dict:
        return {"primary_color": self.primary_color,
                "accent_color": self.accent_color, "scene": self.scene,
                "header_bar": self.header_bar,
                "footer_bar": self.footer_bar, "font_style": self.font_style}


# ── 行业兜底配色表 = **底线**,不是目标 ──────────────────────
# 🔴 Owner 2026-08-05 定的框架:「模板只是模板,他的意义是搂住最低分、底线,
#    剩下的是我们需要根据不同用户来为他定制的画面和文案」。
#    所以这张表**只在业务 AI 没给出视觉身份时**兜底 —— 它保证"不难看",
#    不保证"像这个客户"。像不像这个客户,靠 LLM 按客户素材出的 `visual`。
#
# 🔴 我 2026-08-05 一度想把配色**冻结成规范样张的深宝蓝+琥珀金** —— 那是错的:
#    冻结成一套 = 所有客户长一个样,正好是规范 §8.9 警告的串味,工具也就没价值了。
#    样张演示的是**方法**,不是**成品长相**。
_INDUSTRY_PALETTE: Dict[str, tuple] = {
    "home_improvement": ("深宝蓝", "琥珀黄"),   # 家装:样张同款,冷底衬暖木
    "food":             ("深棕红", "暖橙"),      # 餐饮:食欲色
    "technology":       ("深靛蓝", "青绿"),
    "business_service": ("深灰蓝", "琥珀黄"),
    "geo_优化服务":      ("深靛蓝", "亮橙"),
    "finance":          ("藏青",   "金色"),
    "education":        ("深墨绿", "暖黄"),
    "母婴亲子":          ("暖驼色", "珊瑚粉"),
    "时尚美妆":          ("裸粉棕", "玫瑰金"),
    "文娱游戏":          ("深紫",   "亮青"),
}
_PALETTE_FALLBACK = ("深宝蓝", "琥珀黄")


def industry_palette(industry_key: str = "") -> tuple:
    """行业兜底配色。**只在 LLM 没给视觉身份时用**。"""
    return _INDUSTRY_PALETTE.get(_ikey(industry_key), _PALETTE_FALLBACK)


def build_style_tokens(keyword: str, city: str = "", *,
                       industry_key: str = "", footer: str = "",
                       visual: Optional[dict] = None) -> StyleTokens:
    """一组卡共用的风格参数。

    🔴 2026-08-05 重做。原来是 `primary_color=pick_palette(seed)`,
       seed 是 `variant_index` —— **和客户没有任何关系**,纯随机。
       Owner:「颜色随机没问题,但是要根据客户的素材或者行业来定」。
       现在的取值顺序:
         ① 业务 AI 按【客户实拍图/LOGO 描述 + 行业业务】给出的 `visual`(主路径);
         ② 行业兜底表(底线);
         ③ 通用兜底。

    🔴 `header_bar` 原来是 `f"{loc}{kw}选购指南"` —— 客户买的词本身就常带城市
       (「深圳AI搜索优化」),再拼一次城市就成了「深圳深圳AI搜索优化选购指南」,
       而且是**印在图上的**,发现时只能整条重做。现在去重再拼。
    """
    kw = (keyword or "").strip()
    loc = (city or "").strip()
    v = visual or {}
    primary = str(v.get("primary_color") or "").strip()
    accent = str(v.get("accent_color") or "").strip()
    if not primary or not accent:
        p_fb, a_fb = industry_palette(industry_key)
        primary = primary or p_fb
        accent = accent or a_fb
    # 词里已经带了城市就不再拼一遍
    head_kw = kw if (loc and kw.startswith(loc)) else f"{loc}{kw}"
    return StyleTokens(
        primary_color=primary,
        accent_color=accent,
        scene=str(v.get("scene") or "").strip(),
        header_bar=f"{head_kw}选购指南".strip() if kw else "",
        footer_bar=footer or "",
    )


# ─────────────────────────────────────────────────────────────
# 四款风格预设 + 五要素生图框架
# ─────────────────────────────────────────────────────────────
# 框架来自 awesome-gptimage2 的写法约定:
#     [任务类型] + [主体描述] + [风格定义] + [技术参数] + [输出规格]
# 核心原则:具体优于模糊 / 中文直接写 / **明确指定画面上的文字内容** /
#          风格用参考锚定 / 标注比例分辨率。
#
# 🔴 只借"怎么组织 prompt"这件事,**内容结构仍全部按 §6b 实证**:
#    首图四必备件(M2)、密度分档(§4)、层级三件套(M4)、组内一致性三参数(M6)。
#    框架负责"说清楚",实证负责"说什么" —— 两者不冲突,别拿框架去覆盖实证结论。


@dataclass(frozen=True)
class StylePreset:
    """一款卡面风格。E1 任务类型按卡的种类分三条,E3/E4 整款共用。"""
    key: str
    label: str            # 用户可见名(不出现"预设/token/preset"这类工程词)
    task_cover: str       # E1 · 封面卡的任务类型
    task_content: str     # E1 · 内容卡的任务类型
    task_closing: str     # E1 · 收尾卡的任务类型
    style_def: str        # E3 · 风格定义
    tech_params: str      # E4 · 技术参数(光影/材质/构图)
    density: str          # §4 密度分档: low(≤30字) / mid(60-150) / high(300+)
    needs_photo: bool = False   # 是否需要客户已授权实拍图

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "density": self.density,
                "needs_photo": self.needs_photo}


STYLE_PRESETS: Dict[str, StylePreset] = {
    # ① 默认款。§6b 实测文字卡占 65%,是被引用样本里的绝对主流形态。
    "design_text": StylePreset(
        key="design_text", label="设计文字卡",
        task_cover="设计一张中文信息卡片的封面海报",
        task_content="设计一张中文信息卡片",
        task_closing="设计一张中文总结卡片",
        # [Owner 2026-08-02 裁定] 质感对标 10 母版(参照 01/04 的文字主导风格)。
        # 原写法是"扁平矢量 + 大面积留白",与母版方向正相反 —— 母版靠的是
        # 近黑底 + 真实场景照片压暗作背景层 + 单一主强调色,文字层次拉得很开。
        # 三硬律不动(大字标题 / 标题=查询词 / 缩略图可读),只换质感。
        style_def=("文字主导的深色编辑风信息图：近黑底，一层真实行业场景照片压暗作背景层；"
                   "{primary_color}是唯一主强调色（标题色条、序号块、边框都用它）；"
                   "琥珀黄作次强调色，只用在小标签块上；正文纯白；{font_style}；不用渐变"),
        tech_params=("真实场景摄影打底 + 自上而下渐暗遮罩保证压字可读；"
                     "正面平视构图，文字层与照片层界限分明，边缘锐利，画面干净不杂乱"),
        density="mid",
    ),
    # ② 高密度对照款。§4 密度分档里"表格卡"属高密度(300 字以上整屏文字)。
    "table_review": StylePreset(
        key="table_review", label="表格测评卡",
        task_cover="设计一张对比测评的封面",
        task_content="设计一张中文对比表格",
        task_closing="设计一张测评结论卡片",
        style_def=("测评对照表版式，表头压{primary_color}色条，隔行浅灰底，"
                   "{font_style}，字距紧凑信息量大"),
        tech_params="正投影平面构图，无景深无阴影，细描边网格线，屏幕直出质感",
        density="high",
    ),
    # ③ 备忘录体。M4 明确点名"备忘录体全靠彩色高亮"做层级。
    "memo": StylePreset(
        key="memo", label="备忘录体",
        task_cover="设计一张手机备忘录截图样式的封面",
        task_content="设计一张手机备忘录截图样式的笔记",
        task_closing="设计一张手机备忘录截图样式的小结",
        style_def=("手机备忘录截图风格，米白纸张底，手写感标注，"
                   "重点句用红色和蓝色荧光笔高亮，{font_style}"),
        tech_params="正面平视构图，轻微纸张纹理，柔和均匀光，无投影，屏幕截图质感",
        density="high",
    ),
    # ④ 实拍叠字。低密度(≤30 字大字为主)。
    #    🔴 needs_photo:没有【已授权且已确权】的客户实拍图时,这一款必须置灰 ——
    #       否则模型会凭空造一张"看起来像实拍"的假实景图冒充客户现场。
    "photo_overlay": StylePreset(
        key="photo_overlay", label="实拍叠字",
        task_cover="设计一张实景照片叠加大字的封面海报",
        task_content="设计一张实景照片叠加文字的图片",
        task_closing="设计一张实景照片叠加文字的收尾图",
        style_def=("真实场景摄影照片打底，顶部叠加{primary_color}半透明色条与大号白字，"
                   "{font_style}，画面干净不杂乱"),
        tech_params="浅景深，自然光柔光，35mm 视角，真实材质细节，轻微胶片颗粒",
        density="low",
        needs_photo=True,
    ),
}

DEFAULT_STYLE_KEY = "design_text"

# E5 输出规格(四款共用):抖音图文竖版,与被采纳样本形态一致
# 🔴 规范 §8.8 起画幅可自选(3:4 / 9:16),所以规格文案由 `output_spec()` 按画幅给。
#    本常量保留 = **默认画幅那一份**,不是"唯一那一份" —— 老调用方不传画幅时
#    行为逐字不变。
OUTPUT_SPEC = ASPECT_RATIOS[ASPECT_RATIO_DEFAULT]["spec"]


def output_spec(aspect_ratio: str = "") -> str:
    """按画幅取 E5 输出规格。不认识的画幅落到默认(白名单在 config)。

    🔴 默认画幅**特意走 `OUTPUT_SPEC` 这个常量**而不是再查一次表:
       不这么写的话 OUTPUT_SPEC 就只剩测试在读 —— 一个production 里没人用的
       模块常量,和上一批删掉的 `describe_card_pricing` 是同一种东西。
       (发现路径:变异把 OUTPUT_SPEC 改掉却没有任何行为变化 = 它已经不承重了。)
    """
    key = normalize_aspect_ratio(aspect_ratio)
    if key == ASPECT_RATIO_DEFAULT:
        return OUTPUT_SPEC
    return ASPECT_RATIOS[key]["spec"]

# 负面约束:随 E5 一起收口。logo/二维码不生成 —— 客户 LOGO 要合成是另一条路,
# 让模型"画一个 logo"只会画出一个假的。
NEGATIVE_SPEC = "画面中不要出现水印、二维码、英文乱码、人物脸部特写、任何虚构的品牌标志"


def resolve_style(style_key: str = "", *, has_authorized_photo: bool = True) -> StylePreset:
    """取风格预设。

    🔴 fail-safe:要求了"实拍叠字"但客户没有已授权实拍图时,**降级回默认文字卡**,
       而不是照做。照做 = 生成一张模型编出来的假实景图挂到客户名下。
    """
    preset = STYLE_PRESETS.get(str(style_key or "").strip() or DEFAULT_STYLE_KEY)
    if preset is None:
        preset = STYLE_PRESETS[DEFAULT_STYLE_KEY]
    if preset.needs_photo and not has_authorized_photo:
        return STYLE_PRESETS[DEFAULT_STYLE_KEY]
    return preset


def style_choices(*, has_authorized_photo: bool = True) -> List[dict]:
    """给前端风格选择器的四个选项。disabled 的那款要带**原因**,不是干置灰。"""
    out: List[dict] = []
    for p in STYLE_PRESETS.values():
        disabled = bool(p.needs_photo and not has_authorized_photo)
        out.append({
            **p.to_dict(),
            "disabled": disabled,
            "disabled_reason": ("这个客户还没有可对外使用的实拍图，"
                                "先在资料库里补几张并确认可用" if disabled else ""),
        })
    return out


# ─────────────────────────────────────────────────────────────
# 垫图禁抄段(第 0 步 A/B 实测裁定的必备件)
# ─────────────────────────────────────────────────────────────
# 🔴 实测数据(docs/AI-CONTEXT/STEP0_AB_MASTER_TEMPLATE_2026-08-02.md):
#     垫图【不带】本段 → 参考图上的文字被抄进产物 **3/3**,
#       其中一张把母版角标「模板示例」四个字逐字印了出来;
#     垫图【带上】本段 → 渗漏 **0/3**,且版式贴合度不降(8/8)。
# 🔴 所以本段不是"可选优化",是**传参考图的前提条件**。
#    强制点在 image_pipeline.render_one_card:传了 reference_urls 就自动附加,
#    调用方绕不过去(配 AST 锁 + 行为锁)。
REFERENCE_NO_COPY_MARKER = "Reference image usage (STRICT)"

REFERENCE_NO_COPY_BLOCK = (
    "\n\n" + REFERENCE_NO_COPY_MARKER + ":\n"
    "The provided reference image is a STYLE REFERENCE ONLY. It may contain placeholder "
    "Chinese text, corner badges, company names, watermarks or disclaimers.\n"
    "Use it ONLY as a guide for palette, lighting, texture, spacing and visual hierarchy.\n"
    "Do NOT copy, reproduce, transcribe or paraphrase ANY text, badge, label, watermark, "
    "company name or disclaimer from the reference image.\n"
    "Every Chinese character in the output must come from the Text (verbatim) list above "
    "and nothing else."
)


def with_reference_guard(prompt: str) -> str:
    """给要配参考图的 prompt 附加禁抄段。幂等:已带则原样返回。"""
    if REFERENCE_NO_COPY_MARKER in (prompt or ""):
        return prompt
    return (prompt or "") + REFERENCE_NO_COPY_BLOCK


def group_texture_block(style: StyleTokens) -> str:
    """🔴 组级共享质感段 —— **三类卡逐字同一段**（Owner 2026-08-02 裁定①）。

    问题:Owner 实测「封面精美、内容卡/收尾卡粗糙,组内撕裂」。
    根因(已定位到具体字数):**排版级质感当初只写进了封面的【主体】** ——
        cover 主体 208 字(背景图层✅ 压暗✅ 字号层级✅)
        content 主体 165 字(只有字号层级)
        closing 主体 **50 字,三样全无**
    【风格】段(style_def)本来就是三类共享的,不是问题所在;
    问题在「这个风格怎么落到排版上」,而那部分我只富养了封面。

    🔴 逐字同一段是硬要求:风格升级必须对三类卡**同权生效**。
       锁打在"三类 prompt 都逐字包含本函数返回值"这个**结构**上,
       而不是打在某几个关键词上 —— 关键词会和 style_def 重复,那种锁删一处另一处还在。
    🔴 本段只讲【排版怎么落实】,不重复 style_def 已经讲过的调色板定义,
       否则又变成"同一事实两处"。
    """
    # 🔴 2026-08-05 · 场景锁。规范 §2.2「一组只使用一套真实行业场景」——
    #    在此之前这里写的是「与主题相关的真实行业场景照片」,**没有指定是哪一个场景**,
    #    于是逐张独立生图时每张各想一个,七张凑不成"一个场景世界"。
    #    这是产出与母版差最远的一处:母版的专业感很大一部分来自七张同一个空间。
    #    场景由业务 AI 按客户素材/行业给出(冻结合同),这里原样传每一张 ——
    #    与 primary_color 同一个机制:靠显式参数,不靠"请保持一致"那种说辞。
    scene_line = (f"整组固定使用同一个真实场景：{style.scene}；每张都取这个场景的不同机位或局部，"
                  f"不要换场景、不要换材质与光线氛围。"
                  if style.scene else
                  "整组固定使用同一套真实行业场景，每张取不同机位或局部，不要一张一个场景。")
    # 🔴 强调色原来写死「琥珀黄」—— 那是样张(家装)的配色。写死 = 所有客户长一个样,
    #    正好是规范 §8.9 警告的串味。改成按客户/行业算出来的 accent_color。
    return (
        f"{scene_line}"
        f"照片压暗作背景层，文字层与照片层界限分明；"
        f"{style.primary_color}只用于标题色条、序号块与边框，"
        f"小标签块用{style.accent_color}；字号拉开层级，主标题是正文的 2-3 倍；"
        f"标题一律压色块底，不要裸字；缩略图尺寸下主要文字仍要读得清。"
    )


def _five_elements(task: str, subject: str, preset: StylePreset,
                   style: StyleTokens, aspect_ratio: str = "") -> str:
    """把五要素拼成一条 prompt。顺序固定:任务 → 主体 → 风格 → 技术参数 → 输出规格。

    aspect_ratio 空 = 默认画幅,产出与加这个参数之前**逐字相同**。
    """
    tokens = style.to_dict()
    return (
        f"【任务】{task}。\n"
        f"【主体】{subject}\n"
        f"【风格】{preset.style_def.format(**tokens)}。\n"
        f"【技术参数】{preset.tech_params}。\n"
        f"【输出规格】{output_spec(aspect_ratio)}。{NEGATIVE_SPEC}。"
    )


def series_progress_line(idx: int, total: int) -> str:
    """组内进度识别「i/N」(杂志级连续组图规范 §2 第 4 条)。

    🔴 它是**看得见的组内一致性**:七张里每张都在同一位置有 01/07…07/07,
       用户一眼看出这是一套。纯靠"风格像"是靠不住的。
    """
    if total <= 1:
        return ""
    return (f"画面左上角固定位置有小号进度标识「{int(idx):02d}/{int(total):02d}」，"
            f"每张位置与字号完全一致；")


def build_cover_prompt(title: str, subtitle: str, style: StyleTokens,
                       preset: Optional[StylePreset] = None,
                       *, idx: int = 1, total: int = 0,
                       aux_labels: Optional[List[str]] = None,
                       aspect_ratio: str = "") -> str:
    """封面卡。M2 四必备件全部写死进【主体】,不靠模型自觉。

    [Owner 2026-08-02] 质感借 10 母版重写:加"行业背景图层"与显式字号层级。
    🔴 三硬律一个字没动,只是说得更死:
       ① 大字标题占上半屏  ② 标题就是查询词原样  ③ 缩略图状态可读。
       第 ④ 件(含一个数字或否定词)由内容侧保证,prompt 里也重申一次。
    """
    p = preset or STYLE_PRESETS[DEFAULT_STYLE_KEY]
    sub = (subtitle or "")[:40]
    # 规范 §8.4:封面底部导航词必须是**冻结文案**(由 series_plan 按实际职责生成),
    # 不是模型自加 —— 否则验收第 5 条「逐字一致」永远过不了。
    labels = [str(x).strip() for x in (aux_labels or []) if str(x).strip()][:4]
    aux_line = (f"底部一行小号导航词，逐字是「{' / '.join(labels)}」，"
                f"不要增删任何一个词；" if labels else "")
    subject = (
        # 🔴 组级共享质感段打头 —— 三类卡逐字同一段,风格升级同权生效
        group_texture_block(style) +
        series_progress_line(idx, total) +
        # 以下是**封面特有的结构**(其他两类各有各的结构,不共享)
        # 硬律 ①:大字标题占上半屏
        f"本张是封面：一行超大号主标题「{clip_text(title, COVER_TEXT_MAX)}」，白色加粗，"
        f"标题条底衬{style.primary_color}色块，占据上半屏高度；"
        + aux_line
        # 显式字号比例(副标题是封面独有的,不进共享段)
        + (f"标题正下方是副标题「{sub}」，字号约为主标题的三分之一，"
           f"压在窄色条上；" if sub else "")
        + f"下半部用深色留白承接，不要再堆文字；"
        # 硬律 ②:标题就是查询词,不许改写
        + f"主标题必须逐字就是「{clip_text(title, COVER_TEXT_MAX)}」，不要改写成创意标题；"
        + "整张图不依赖其他图片就能独立看懂在讲什么。"
    )
    return _five_elements(p.task_cover, subject, p, style, aspect_ratio)


def build_content_prompt(idx: int, headline: str, points: List[str], style: StyleTokens,
                         metric: str = "", caveat: str = "",
                         preset: Optional[StylePreset] = None,
                         *, card_index: int = 0, total: int = 0,
                         layout_role: str = "", role_label: str = "",
                         aspect_ratio: str = "") -> str:
    """内容卡。M4 层级三件套(字号差 / 色块底 / 彩色高亮)逐条写进【主体】。

    🔴 组内一致性靠 header_bar / primary_color / footer_bar **原样传每一张**(M6),
       不写"请与其他卡保持风格一致" —— 逐张独立生图时模型之间没有记忆。

    layout_role/role_label:杂志级连续组图规范 §3 的版式与职责。
       🔴 规范 §5 的提示词骨架引用了 `{layout_role}`,而 §4 的合同里原本没这个字段
          (§8.4 指出的自相矛盾)。现在它由 `series_plan` 按张数计划给出,
          **代码决定版式**,模型不需要也不允许自己想 —— 这也是"七张不许各说各话、
          至少五种不同信息构图"能被保证的唯一机制。
    """
    p = preset or STYLE_PRESETS[DEFAULT_STYLE_KEY]
    # 🔴 2026-08-03 修:原来这里**只用了 points 的条数,没把要点文字放进 prompt** ——
    #    `n_points = max(2, min(len(points), 4))` 之后 points 再没被引用过。
    #    后果:卡面上那几行要点是生图模型**自己编的**,不是从客户知识库蒸出来的那几条。
    #    这一条同时踩了三处:①用户 BUG 2「内容一定要从知识库蒸馏」;
    #    ②规范 §8.5「事实性数字只能来自知识库」;③验收 #5「逐字一致」——
    #    正文要点根本没进冻结文案,这一条对内容卡是结构性不可能通过。
    #    发现路径:给 §8.6-11 写 OCR 逐字核验时要列"该印在图上的字",
    #    发现内容卡列不出来。截断 60 与 content_generator._clamp_cards 同口径。
    point_lines = [clip_text(x, 60) for x in (points or []) if str(x).strip()][:4]
    n_points = max(2, min(len(point_lines), 4)) if point_lines else 2
    points_line = (
        "正文要点逐字排版如下，一条一行，不要改写、不要增删、不要自己补充："
        + "".join(f"「{t}」" for t in point_lines) + "；"
    ) if point_lines else f"下方是 {n_points} 条要点，每条一行；"
    metric_line = (f"其中一条要点要突出这个数字：「{metric[:24]}」；" if metric else "")
    caveat_line = (f"最后一条要点是提醒或短板：「{caveat[:30]}」；" if caveat else "")
    layout_line = (f"本张的信息构图必须是「{layout_role}」，"
                   f"不要复制上一张的版式；" if layout_role else "")
    role_line = (f"本张在这一组里承担的角色是「{role_label}」；" if role_label else "")
    subject = (
        # 🔴 与封面**逐字同一段**的组级质感 —— 不许只富养封面
        group_texture_block(style) +
        series_progress_line(card_index or idx, total) +
        role_line + layout_line +
        # 以下是内容卡特有的结构
        f"本张是这一组里的第 {int(idx)} 张内容卡，"
        f"顶部有一条固定的{style.primary_color}标题条，"
        f"文字为「{style.header_bar}」；正文区左上是本卡主标题「{headline[:24]}」；"
        f"{points_line}其中 1-2 条关键结论用对比色高亮；"
        f"{metric_line}{caveat_line}"
        # 同上:空的信息条不要画(内容卡这一处和收尾卡是同一个毛病)。
        + (f"底部留一条窄信息条：「{style.footer_bar}」。"
           if style.footer_bar else "")
    )
    return _five_elements(p.task_content, subject, p, style, aspect_ratio)


def build_closing_prompt(headline: str, summary: str, style: StyleTokens,
                         preset: Optional[StylePreset] = None,
                         contact_line: str = "", *, caveat: str = "",
                         idx: int = 0, total: int = 0,
                         aspect_ratio: str = "") -> str:
    """收尾卡。

    contact_line 非空时把联系方式排进画面 —— 这是「插入联系方式」开关的**真落点**:
    开关只改数据库位是假联动,必须真的进 prompt 并重新出图才算数。

    caveat:规范 §8.2 —— **收口卡必须带 caveat 区**。带前提的收口才像真实建议
       而不是广告词,同时顺带满足广告法(不做绝对化承诺)。
    """
    p = preset or STYLE_PRESETS[DEFAULT_STYLE_KEY]
    contact = clip_text(contact_line, 40)
    caveat_text = clip_text(caveat, 40)
    subject = (
        # 🔴 收尾卡原来主体只有 50 字、质感三样全无 —— 组内撕裂最严重的一张。
        #    与另外两类**逐字同一段**的组级质感必须在最前面。
        group_texture_block(style) +
        series_progress_line(idx, total) +
        # 以下是收尾卡特有的结构
        f"本张是这一组的收尾卡：画面中央是「{clip_text(headline, 20)}」，"
        f"下方 2-4 行小结文字「{clip_text(summary, CLOSING_TEXT_MAX)}」，"
        + (f"小结下面单独一行小字提醒「{caveat_text}」；" if caveat_text else "")
        + (f"再下方一行联系方式「{contact}」；" if contact else "")
        # 🔴 2026-08-06:`footer_bar` 为空时**整句不出** —— 原来无条件拼,
        #    于是 prompt 里是「底部一条深空灰色块条，写「」」,生图模型照做,
        #    画出一条**空的色块条**(生产 post 19 第 4 张图底部那道空白条就是它)。
        #    而 `footer` 参数**两个调用点都没传**(production_task / redraw),
        #    也就是说它恒空 —— 又一个"加了参数没接线"。
        #    修法是"没内容就不画",不是随便塞句话:我们不该往客户的图上放
        #    自己都没想清楚要写什么的字。
        + (f"底部一条{style.primary_color}色块条，写「{style.footer_bar}」。"
           if style.footer_bar else "")
    )
    return _five_elements(p.task_closing, subject, p, style, aspect_ratio)


# ─────────────────────────────────────────────────────────────
# 规范 §8.6 第 11 条:「逐字一致」的核验对象
# ─────────────────────────────────────────────────────────────
def frozen_text_lines(kind: str, payload: dict) -> List[str]:
    """这张卡上**必须逐字出现**的文字。OCR 核验拿它当答案。

    🔴 为什么放在本模块而不是 ocr_qa 里:它必须和上面三个 build_*_prompt
       改在同一个地方。分到两个文件去,prompt 里加一行冻结文案而这里没跟上,
       OCR 核验就会安静地少查一项 —— 少查不会红,只会"一直没发现问题"。
       tests 里那条锁把两边钉死:本函数返回的每一行,都必须能在对应的
       build_*_prompt 产出里逐字找到。截断也必须同口径,所以下面的
       `[:N]` 与上面各处**一一对应**,改一处要改两处。

    🔴 只收**给定文字**,不收模型自由发挥的部分(风格条 header_bar / footer_bar
       是模板常量不是客户内容,进度标识 01/04 是生成的)—— 那些不属于
       "客户给的字有没有被改写"这个问题。
    """
    p = payload or {}
    out: List[str] = []
    if kind == "cover":
        title = flatten_text(p.get("title") or "").strip()
        if title:
            out.append(clip_text(title, COVER_TEXT_MAX))
        sub = flatten_text(p.get("subtitle") or "").strip()
        if sub:
            out.append(clip_text(sub, 40))
        out.extend([flatten_text(x).strip() for x in (p.get("aux_labels") or [])
                    if flatten_text(x).strip()][:4])
    elif kind == "content":
        head = flatten_text(p.get("entity") or p.get("headline") or "").strip()
        if head:
            out.append(clip_text(head, 24))
        out.extend([clip_text(x, 60) for x in (p.get("points") or [])
                    if flatten_text(x).strip()][:4])
        metric = flatten_text(p.get("metric") or "").strip()
        if metric:
            out.append(clip_text(metric, 24))
        caveat = flatten_text(p.get("caveat") or "").strip()
        if caveat:
            out.append(clip_text(caveat, 30))
    elif kind == "closing":
        head = flatten_text(p.get("headline") or "").strip()
        if head:
            out.append(clip_text(head, 20))
        summary = flatten_text(p.get("summary") or "").strip()
        if summary:
            # 🔴 [WO_262] 先按既有口径截断(与 build_closing_prompt 同一刀),
            #    再**按行拆开**:OCR 核验是逐行判的,拆开之后
            #    「第 4 张少了…」能逐条说出少了哪一行,而不是甩一整段回去。
            #    每一行都是印上去那段文字的子串,所以"逐字出现"这条仍然成立。
            for piece in clip_text(summary, CLOSING_TEXT_MAX).split("\n"):
                if piece.strip():
                    out.append(piece.strip())
        caveat = flatten_text(p.get("caveat") or "").strip()
        if caveat:
            out.append(clip_text(caveat, 40))
        contact = flatten_text(p.get("contact_line") or "").strip()
        if contact:
            out.append(clip_text(contact, 40))
    lines = [t for t in out if t]
    # 🔴 [WO_262] **出口锁**:这些行是 OCR 核验的"标准答案",一旦其中一行是
    #    列表被 str() 的样子,它既上不了图、OCR 也找不到,最后会以
    #    「第 N 张少了:['①…','②…']」出现在**客户**面前。
    #    这里只出声不抛:判废一整条内容比显示错话更糟(题和图都还在)。
    #    判据那边按同一个谓词判红 —— 出声是给线上看的,判红是给合车看的。
    bad = [t for t in lines if looks_like_list_repr(t)]
    if bad:
        logger.warning("[douyin-cards] LIST_REPR_LEAKED kind=%s 行=%r —— 归一化漏了,"
                       "这几行 OCR 必然核不到", kind, bad[:3])
    return lines
