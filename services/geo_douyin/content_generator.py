"""GEO 抖音图文管线 v1 · 图文文案 + 卡片要点生成

形态口径全部来自 §6 实证(docs/AI-CONTEXT/DOUYIN_ADOPTED_CONTENT_PATTERNS_2026-08.md):
  - 正文 80-200 字(实证中位 91;工单原写 200-500 偏长)
  - 卡片 1-9 张,默认 5(实证中位 4.5,单图占 23% 所以下限是 1 不是 3)
  - 每卡一句核心信息:榜单位 / 选型要点 / 避坑项

模型:官方 DeepSeek V4 Flash 直连 `api.deepseek.com`(Owner 08-01 规则:
默认官方直连,禁 OpenRouter / DashScope 代理版)。

🔴 广告法约束进【写作 prompt】= 主力防线(Owner 08-01 二次拍板:发布侧硬拦取消,
   写作侧前移)。生成后仍走既有机审提示级,不硬拦。

🔴 口播稿(45-90 秒)属 Phase 2 视频档,本模块**不生成**。
"""
from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from services.geo_douyin.card_templates import (
    BRAND_LINE_MAX,
    CLOSING_TEXT_MAX,
    CLOSING_KINDS,
    CONTENT_TEXT_MAX,
    CONTENT_TEXT_MIN,
    COVER_HOOKS,
    COVER_TEXT_MAX,
    DEFAULT_CLOSING,
    DEFAULT_SKELETON,
    SKELETONS,
    audience_style_block,
    clip_text,
    flatten_text,
    pick_hook,
    promote_block,
)
from services.geo_douyin.config import (
    BODY_LEN_MAX,
    BODY_LEN_MIN,
    CARD_COUNT_DEFAULT,
    CARD_COUNT_MAX,
    CARD_COUNT_MIN,
)
from services.geo_douyin.knowledge_context import build_brand_context

logger = logging.getLogger("GEO-Douyin-Content")

LLM_MODEL = DEEPSEEK_OFFICIAL_FLASH
_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
# 🔴 2026-08-03 · 40.0 → 180.0。上一次热修只提了 max_tokens,卡点当场平移到这里:
#   `max_tokens=8000` + `timeout=40` 生产实测 **3/3 ReadTimeout @40.2–40.4s**,
#   而真实耗时 45.0 / 78.3 / 85.3s —— 40 秒结构上等不到。
#   取值 = 3.8 × 实测最长(16000 档 5 次上限 47.4s)。给足冗余的成本是零:
#   本链已随「生产链异步化」改为后台任务(TASK_TOTAL_TIMEOUT_SECONDS=1800),
#   长超时不再有 nginx 504 风险,而超时太短是**确定性失败**。
_LLM_TIMEOUT_S = 180.0

# 输出额度阶梯。**第一档是常态,第二档只在确实撞顶时才用。**
#
# 🔴 2026-08-06 · 由定值 16000 改成阶梯。生产实证(llm_call_log · 近 7 天 47 次):
#   撞顶 5 次 / 正常 30 次 —— 其中 post 17 的那次 `output_tokens=15999`,
#   离 16000 只差 1,`content` 为空,任务落 `llm_unavailable`。
#   为什么 16000 从"5/5 全绿"退化了:那次定档时 prompt 是 2488 tokens;
#   P0 包给 prompt 加了 promote_block + 视觉身份段 + 实拍图描述,
#   同一条链 input 涨到 **4751** —— 输入吃掉的每一个 token 都是推理少掉的余量。
#   → 天花板不是常数,是"**输入涨了就得跟着涨**"的东西。
#
# 为什么第二档只有 24000 而不是更大:24000 档实测出现过 **169.8s 长尾**
#   (reasoning 放飞到 17243),再往上会顶穿 `_LLM_TIMEOUT_S=180`。
#   额度越大跑飞的空间越大 —— 阶梯的意义是"够用就停",不是"越大越保险"。
MAX_TOKENS_LADDER = (16000, 24000)

# ── 广告法:与 Owner 签发的版本化目录**同源**(WO v3 P1-1)────────────────
#
# 🔴 2026-08-06:本文件原来自带一份 15 词的 `_AD_LAW_FORBIDDEN`,与
#    `config/legal_prohibited_pack.json`(Owner 签发,2026-07-23)**不是同一份**。
#    实测 1,164 条真实抖音语料:签发目录命中 85 条,本地词表只命中 12 条 ——
#    **漏检率 95%**,缺的正是榜单最爱用的
#    「最好/最强/最优/第一/第一名/全国第一/首个/首选/世界级/史上最」。
#    `_PROMPT` 里还硬编码了**第三份**词表,三份互不相同。
#
# 现在三处全部同源自 `services/marketing/guards.legal_pack()`:
#    ① 写作侧 prompt 约束(主防,Owner 08-01「在写作提示词中写清楚」)
#    ② 生成后自检并标记
#    ③ 提示 + 一键修复出口(不阻塞发布)
#
# 匹配方式**不**沿用 `guards._find_words` 的纯子串:「第一」会撞「第一次/第一步/
# 第一梯队」,实测误报 28%。走 `ranking_payload.absolute_law_hits` 的语境匹配。
#
# 🔴 运行时依据 = **Owner 2026-08-01 二次拍板 + 生产现行为**
#    (`services/article_review_gate.py:338-346` 的 §3A 降级已随 `f0f886dc` 上线)。
#    **不是**治理 SSOT v2.5 —— 那一条目前是「Review 暂定·待 Owner 签发」,未生效,
#    不得作为放宽任何既有闸的依据。
#
# ⚠️ 本处改动**没有放宽任何闸**:改前 `ad_law_flags` 就已经是纯留痕、无人阻断;
#    改后仍不阻断,只是把漏检率 95% 的本地词表换成签发目录,并把留痕接成可见提示。
#    即"检出更严 + 执行层级不变",方向与 v2.5 是否签发无关。


def _ad_law_terms() -> tuple[str, ...]:
    """签发目录里的绝对化词 —— **本模块不自建词表**。"""
    try:
        from services.marketing.guards import legal_pack
        return tuple(str(w) for w in (legal_pack().get("ad_law") or ()) if str(w or ""))
    except Exception:  # noqa: BLE001 — 取不到目录不该挡住生成
        return ()


def ad_law_prompt_block() -> str:
    """注进 `_PROMPT` 的写作侧约束段(主防)。词从签发目录动态生成。"""
    terms = _ad_law_terms()
    if not terms:
        return "- 禁一切绝对化用语(最高级/唯一性/排名第一类表述);"
    shown = "/".join(terms[:14])
    return (f"- 禁绝对化用语:{shown} 等(以 Owner 签发的法律禁止目录为准);\n"
            f"- 这些词**在你这一步就不要写出来**;写出来不会被系统拦下,"
            f"但会给用户留一条提示,等于把活推给他。")

_PROMPT = """你在为抖音写一条"图文帖"(图片轮播 + 文案),目标是被 AI 搜索引擎(豆包)引用。

【实测背书:为什么这么写】(别按常规新媒体套路写)
- 被豆包引用与**点赞热度完全无关**(采纳组中位 12 赞 vs 未采纳 14 赞,71% 都是百赞以下)
  → 不要追爆款感、不要标题党,要追**信息完整度**;
- **标题信息量是最强单一信号**。按长度分档的真实采纳率:
  ≤15字 26.0% / 16-25字 23.1%(最差) / 26-40字 26.6% / 41-60字 27.9%
  / **61-100字 34.4%** / >100字 35.6%
  🔴 抖音是把标题和正文当**一整段 caption** 索引的,而发布平台标题字段硬上限 45 字,
     单靠标题够不到 61+ 的最优档 → **正文第一句必须紧接标题继续说,别重复标题**,
     让「标题 + 正文首句」合起来达到 60-100 字的信息量。
  ⚠️ 但"越长越好"**不跨行业成立**(2026-08-03 分层复核):
     长度差在 business_service +42 字、auto +21 字很强,而在**家装只有 +4 字**、
     geo服务/金融 **0**、电商 **−2 字**(反向)。所以把信息说全就够了,
     **不要为了凑长度往里灌废话** —— 灌出来的水字在多数行业一点用没有。
  → 正文开头**禁止**写"大家好""今天给大家分享"这类空转句,第一句就上干货;
- hashtag 和数字对"被引用"零贡献 → 不在卡面上堆,正文尾部带即可。

【首图钩子】{hook_line}
🔴 这不是风格偏好,是实测:同层内带该表达 vs 不带的**采纳率差**
   避坑式 B端 +12.7pp / C端 +10.0pp / 家装 +11.9pp / 文娱 +21.5pp —— 唯一跨层都正向;
   疑问式 B端 +0.6pp / C端 +0.0pp / 家装 **−5.2pp**;榜单式家装 **−7.1pp**。
   所以除非上面明确让你用疑问式,否则**别写成"怎么选/哪家好"**。

【读者是谁】{audience_line}

🔴🔴【这条内容是给谁拉生意的 —— 本条优先级最高】
{promote_block}

🔴🔴【这一组的视觉身份 —— 你定,不是生图模型定】
你必须**按这个客户**给出一套视觉身份,整组七张共用。依据优先级:
  ① 上面【客户实拍图】/【LOGO 视觉描述】里读得出的场景与配色 —— 有就必须用;
  ② 没有素材时,按这个客户的**行业与业务**推导(例:酸汤火锅→暖木色调的堂食场景;
     工业设备→冷灰调的车间;企业服务→冷静的现代办公空间);
  ③ **禁止**用与这个客户无关的场景 —— 参考样张是家装(深胡桃木衣柜),
     它只演示"怎么做",不是"做成什么样"。家装场景出现在非家装客户的产出里 = 串味,直接判废。
配色写**中文色名**(如"深宝蓝""墨绿""暖橙"),主色一个、强调色一个,对比要够(缩略图下也看得清)。

【骨架】{skeleton_name}:{skeleton_desc}

【这一组的张数与每张职责】(杂志级连续组图规范 §3/§8.1 的降档表,照做)
{role_plan}

内容卡必须**同构填充** —— 每张卡字段完全一样,只换内容:
  entity    实体名/子问题/卖点名(不超过 12 字)
  one_liner 一句话定位(不超过 20 字)
  points    2-4 条要点,每条一行,口语化
  metric    一个可比数字(价格/年限/规模/耗时等);没有就留空,**不要编**
  caveat    一条短板或注意事项  ← **必填**

🔴 caveat 为什么必填:带缺点的卡才像真实测评而不是广告,既提升可信度,
   也顺带满足广告法(不做绝对化承诺)。**编不出真实短板就写使用注意事项,但不能空着。**

【语气】**第一人称,硬要求不是建议**。用「我」「咱」「我去看了」「亲测」这类说法;
禁止「本产品采用先进技术」「致力于为您提供」这种官网腔。
🔴 实测:第一人称几乎全行业正向 —— geo服务 +27.7pp / 母婴 +13.7pp / food +10.9pp
   / B端 +10.4pp / 家装 +8.2pp(唯一例外是文娱游戏 −4.4pp)。这是最稳的通用手法。

【字数】封面 ≤{cover_max} 字;每张内容卡正文 {content_min}-{content_max} 字;收尾卡 ≤{closing_max} 字。
正文文案(帖子描述区)另写 {body_min}-{body_max} 字。

🔴 广告法约束(**这是主力防线,在你这一步就要做到**):
{ad_law_block}
- 禁编造无出处的数字、排名、认证、销量;
- 不承诺效果,不做疗效/收益保证。

🔴 数字分两类,别搞混(规范 §8.5):
- **事实性数字**(价格、参数值、排名、评分、资质年限)—— 只能来自上面的
  【客户自有素材】。素材里没有,就**只讲比较维度不给数值**,
  绝不写一个"看起来很专业"的假数;
- **方法论计数**("至少核对 3 项参数""拆成 4 个部分")—— 允许,
  但必须由你写进要点里,让它成为冻结文案的一部分。

🔴 收尾卡必须带一条 caveat(提醒/前提/因人而异的说明)。
   理由和内容卡一样:带前提的收口才像真实建议而不是广告词。

{ranking_block}
主题关键词:{keyword}
{city_line}{brand_line}{extra_line}{materials_block}

只输出 JSON,不要解释,格式:
{{"body": "正文文案",
  "visual": {{"scene": "整组共用的真实场景(一句话,要具体到能拍出来)",
             "primary_color": "主色中文色名", "accent_color": "强调色中文色名"}},
  "cover": {{"title": "封面大标题(就是查询词本身)", "subtitle": "副标题"}},
  "cards": [{{"entity": "", {ranking_card_field}"one_liner": "", "points": ["",""], "metric": "", "caveat": ""}}],
  "closing": {{"headline": "收尾卡标题", "summary": ["小结第一行", "小结第二行"], "caveat": "一条提醒或前提",
              "brand_line": "点名这个客户的一句话(见上面【给谁拉生意】)"}},
  "title": "发布用标题(26-45 字,信息量优先)",
  "hashtags": ["标签1", "标签2", "标签3", "标签4", "标签5"]}}

🔴 `title` 与 `hashtags` **由你出,不再有模板兜底**(Owner 2026-08-04 拍板)。
   - 标题不要机械拼城市:主题词里**如果已经带了城市就别再加一遍**
     (「深圳AI搜索优化」+「深圳」= 「深圳深圳AI搜索优化」,这是真发生过的事故);
   - 标签 5 个要有**区分度**,别是同一个词加后缀(推荐/哪家好/怎么选…)——
     那种堆法在抖音上会被当重复;至少覆盖:主题词、城市/区域、细分场景、人群或痛点。

🔴 `cards` 数组的长度必须**恰好 {content_count}**(上面职责表里除封面和收尾之外的张数),
   顺序与职责表一一对应。多了会被裁掉、少了整条作废重来 —— 别少给。"""


@dataclass
class GeneratedContent:
    body: str
    cards: List[dict] = field(default_factory=list)   # 内容卡(同构字段)
    cover: dict = field(default_factory=dict)          # 封面卡 {title, subtitle}
    closing: dict = field(default_factory=dict)        # 收尾卡 {headline, summary}
    ad_law_flags: List[str] = field(default_factory=list)
    skeleton: str = ""
    kb_sources: List[str] = field(default_factory=list)  # 用了哪些客户素材来源(留痕)
    # 🔴 这个客户有没有**已授权的实拍图**(LOGO 不算)。决定「实拍叠字」要不要降级。
    #    默认 False = 降级方向:任何一条没带上这个信号的路径(生成失败的兜底对象、
    #    老快照反序列化)都会走"当作没有实拍图"→ 出文字卡,而不是让模型编实景。
    #    这是**唯一安全的默认值**,不要为了"少改一处"把它默认成 True。
    has_real_photo: bool = False
    # 🔴 2026-08-05 · 视觉身份进冻结合同(规范 §4「业务 AI 先输出冻结内容,
    #    生图模型只排版」)。{scene, primary_color, accent_color},
    #    由业务 AI 按【客户素材 + 行业】给出;给不出时代码按行业兜底。
    visual: dict = field(default_factory=dict)
    # 🔴 2026-08-05 · 标题与标签改由 LLM 出(Owner:「一定不能硬编码」),
    #    `title_engine` 模板池不再参与主路径。
    title: str = ""
    hashtags: List[str] = field(default_factory=list)
    # 期望点名的客户(由生成侧写入,供 promotes_brand 判定)
    brand_name_expected: str = ""
    model: str = LLM_MODEL
    ok: bool = True
    error: str = ""

    @property
    def promotes_brand(self) -> bool:
        """正文里到底有没有点到客户的名字。

        🔴 这是 P0 的验收面:一条不点名客户的内容,豆包引用了也推荐不到客户身上
           —— 花 390 算力做公益。所以它是**硬校验**,不是提示。
           判据放在这里(而不是 production_task)是因为它属于"这份产出合不合格",
           跟 ok/error 是同一类事。
        """
        who = str(self.brand_name_expected or "").strip()
        if not who:
            return False
        # 🔴 2026-08-06 收紧:只看**正文**,不再算上收尾句(Owner:「必须要有客户名字,
        #    不然白打」)。
        #    原来是 `body + brand_line` 二选一 —— 而 prompt 的硬要求写的一直是
        #    「**正文里**必须出现」,闸比 prompt 松,两边不一致。
        #    生产 post 19 正是踩在这个缝里:正文通篇没有客户名,名字只在收尾句和卡片上,
        #    闸照样放行。
        #    正文是抖音把标题和描述当**一整段 caption** 索引的那一段,
        #    也是豆包最容易抓到的文本面 —— 名字不在这里,被引用了也推荐不到客户身上。
        #    ⚠️ 代价是实测 17% 的产出会因此判废(本机真跑 12 次:正文点名率 83%)。
        #       这是 Owner 拍板接受的:白做一条比失败一条贵。
        return who in str(self.body or "")

    @property
    def self_praise(self) -> str:
        """产出里有没有**卖方自称**。命中返回那个词,没有返回空串。

        🔴 Owner 2026-08-06:「一定要拒绝自卖自夸」。生产实测 post 18 写出了
           「**我们**全域上榜（深圳）科技有限公司,2018年就开始做…」——
           这内容发在**素人矩阵账号**上,账号不是品牌本人,用「我们」开口当场穿帮;
           而且自述式推广更难被豆包当成可引用的事实来源。

        🔴 判据只抓**卖方自称**,不抓第一人称本身 —— 这个区分是这条锁的全部价值:
             ✅ 放行「我实测了」「我对比了几家」 —— 测评者视角,
                实证里 geo_优化服务 **+27.7pp**,全行业最稳的一招,不能误伤;
             ❌ 拦截「我们{品牌}」「我司」「本公司」 —— 卖方视角。
           所以不能简单禁"我们"二字:第三方口吻里「我们平时挑服务商」这类
           泛指用法是合法的。只在**指向品牌或自家公司**时才算。
        """
        hay = f"{self.body}\n{self.closing.get('brand_line', '')}"
        # ① 无条件禁:这些词本身就只有卖方会用
        for bad in ("我司", "本公司", "敝公司", "敝司", "咱家",
                    "我们公司", "我们团队", "我们这边", "我们品牌", "本店", "本厂"):
            if bad in hay:
                return bad
        # ② 条件禁:第一人称**紧贴品牌名**(中间最多隔几个虚词)= 把品牌说成自己
        who = str(self.brand_name_expected or "").strip()
        if who:
            # 🔴 隔字类**刻意很窄**(只有「的」和标点空白):
            #    放宽到含「是」就会误伤「我**是**全域上榜的老客户」——
            #    那是素人现身说法,恰恰是我们要的第三方口吻。
            #    同理「我问了全域上榜…」也不该中(「问了」不在隔字类里)。
            for pron in ("我们", "咱们", "我"):
                pat = re.compile(re.escape(pron) + r"[的，,、\s]{0,2}"
                                 + re.escape(who))
                m = pat.search(hay)
                if m:
                    return m.group(0)
        return ""

    def to_dict(self) -> dict:
        return {
            "body": self.body, "cards": list(self.cards),
            "cover": dict(self.cover), "closing": dict(self.closing),
            "ad_law_flags": list(self.ad_law_flags),
            "ad_law_notice": ad_law_notice(self.ad_law_flags),
            "skeleton": self.skeleton,
            "kb_sources": list(self.kb_sources),
            "has_real_photo": bool(self.has_real_photo),
            # 🔴 这四个必须进快照:重抽单张会从 generation_meta.content 重建 prompt,
            #    漏了 visual 就等于重抽那张换了一套配色/场景 —— 组内一致性当场破。
            "visual": dict(self.visual),
            "title": self.title, "hashtags": list(self.hashtags),
            "brand_name_expected": self.brand_name_expected,
            "model": self.model, "ok": self.ok, "error": self.error,
        }


def _get_api_key() -> str:
    """复用平台 DeepSeek key 池(带轮换),兜底 .env 单 key。"""
    try:
        from services.llm.deepseek_key_pool import pick_deepseek_api_key
        key = pick_deepseek_api_key("realtime")
        if key:
            return key
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("DEEPSEEK_API_KEY", "") or ""


def scan_ad_law(text: str) -> List[str]:
    """广告法自检。返回命中的绝对化用语(**提示级,不硬拦** —— Owner 08-01 拍板)。

    词表同源自 Owner 签发目录;匹配走语境判定,不用纯子串
    (「第一次/第一步/第一梯队」实测误报 28%)。
    """
    if not text:
        return []
    from services.geo_douyin.ranking_payload import absolute_law_hits
    return absolute_law_hits(text)


def ad_law_notice(flags: List[str]) -> Optional[dict]:
    """③ 提示 + 一键修复出口。**不阻塞发布**(Owner 08-01;治理 SSOT v2.5 ②)。

    形状与文章链 `article_review_gate` 的 content_notice 对齐 ——
    同一个法律面在两条链上给用户看到的东西应该是一样的。

    🔴 `overridable=True` / 没有任何 `eligible=False` 语义:
       这条通知**只是通知**。发不发由客户决定(Owner:「剩下客户审核不审核是他自己决定」)。
    """
    hits = [str(w) for w in (flags or []) if str(w or "").strip()]
    if not hits:
        return None
    return {
        "class": "legal_hard",
        "reason": "ad_law_absolute",
        "reason_class": "legal_hard",
        "overridable": True,
        "blocking": False,
        "hits": sorted(set(hits)),
        "message": ("内容里有《广告法》绝对化用语("
                    + "、".join(sorted(set(hits))[:5])
                    + ")。这不阻止你发布 —— 是否处理由你决定;建议改掉再发。"),
        "repair_hint": "把这些词换成有依据的具体表述(如「4 个 AI 都提到」而不是「第一」)。",
        "rule_version": _ad_law_rule_version(),
        "actions": [
            {"id": "ai_repair", "label": "一键修复", "type": "action"},
            {"id": "edit_manually", "label": "自己改", "type": "nav"},
            {"id": "publish_anyway", "label": "先发,我知道了", "type": "action"},
        ],
    }


def _ad_law_rule_version() -> str:
    try:
        from services.marketing.guards import legal_pack_version
        return str(legal_pack_version() or "")
    except Exception:  # noqa: BLE001
        return ""


#: `caveat` 缺失时的合法兜底。**不是编事实** —— 它是一句真实的适用边界提醒,
#: 正是 `card_templates.py:133-134` 早就给出的合法出口
#: (「编不出真实短板就写使用注意事项,但不能空着」)。
CAVEAT_FALLBACK = "具体适配情况建议按自己的项目条件再核一次"


#: 榜单形态下,每张内容卡多带的那个**声明字段**在 JSON 格式里的样子。
#: 🔴 它必须出现在 `_PROMPT` 的输出格式行里(而不是只在上面的说明段里讲一句)——
#:    模型照抄的是格式行,说明段里补的字段经常被漏掉。
RANKING_CARD_FIELD = '"entity_ref": "", '


def ranking_card_field(plan: Any) -> str:
    """卡组型返回空串 —— 此时输出格式与加这个参数之前**逐字相同**。"""
    return RANKING_CARD_FIELD if plan is not None else ""


def ranking_prompt_block(plan: Any) -> str:
    """把榜单冻结件翻成写作侧硬约束。

    `plan` 为 None(卡组型)时返回**空串** —— 此时整段 prompt 与加这个参数之前逐字相同。

    🔴 四条都是**冻结**的,模型只能照抄不能改写:
      ① 只能用白名单里的公司名 —— 名单外一个字都不许写(这是主防线,见下);
      ② 讲某一家的卡必须用 `entity_ref` **声明讲的是哪一家**,值逐字抄白名单;
      ③ 名次表述只能照抄 `rank_statement` 那一句 —— 它由 engine/rank/query/observed_at
         **同行四元组**产出,改写一个字这句话就可能变成假的;
      ④ 无据分支不许出现「榜单/排行/第N名」字样(D12④:不许挂榜单名把客户放榜外)。

    🔴 ① 是**禁虚构的主防线**(治理 SSOT D8⑤:禁虚构 = prompt 指导,非阻断项)。
       生成后的 R1 闸只按 `entity_ref` 声明做集合成员判定,是兜底不是主防 ——
       所以这一段的措辞必须够硬。
    """
    if plan is None:
        return ""
    names = plan.allowed_names()
    lines = "\n".join(
        f"  - {it.display_name}:{st}" + ("(本次客户)" if it.is_client else "")
        for it, st in zip(plan.payload.items, plan.statements))
    tag_lines = "\n".join(
        f"  - {it.display_name} 可用标签:" + ("、".join(it.tags) or "(无,那就别写标签)")
        for it in plan.payload.items)
    wording = (
        "本条可以用「榜单/推荐榜」这类说法。"
        if plan.routed["allows_ranking_wording"] else
        "🔴 **本条不许出现「榜单/排行/排行榜/第N名」字样** —— 这一单没有可核验的"
        "实质位次,挂榜单名属虚假呈现。请写成「场景推荐」「选型参考」。"
    )
    return (
        "\n\n🔴🔴【榜单冻结件 —— 逐字照用,不许增删改】\n"
        "可用公司名**白名单**(名单外一个名字都不许出现,包括你觉得很有名的那些):\n"
        "  " + "、".join(names) + "\n\n"
        "每一家的名次表述(**只能照抄,不许改写措辞、不许省略引擎名**):\n"
        + lines + "\n\n"
        "每一家的可用标签(只能从这里选,没有就不写):\n"
        + tag_lines + "\n\n"
        + wording + "\n"
        "禁止给任何一家编造分数、评级、市占率、客户数、资质;\n"
        "禁止绝对化用语(见上面的广告法约束)。\n\n"
        "🔴 每张内容卡多填一个字段 `entity_ref`,规则只有两条:\n"
        "  · 这张卡讲的是上面某一家 → `entity_ref` **逐字抄**那家的名字(抄全,别缩写);\n"
        "  · 这张卡讲的是比较口径 / 成本结构 / 核验细节这类**不针对具体某一家**的内容"
        "→ `entity_ref` 留空字符串。\n"
        "  别自己发明名字往里填 —— 名单以外的名字一律要重做那一张。\n"
    )


def _clamp_cards(cards: list, want: int) -> List[dict]:
    """规整内容卡为**同构字段**(M3)。

    🔴 2026-08-06 返工:`caveat` 缺失**不再丢卡**。

       原来缺 caveat 直接 `continue` 丢弃整张卡,而丢卡会让
       `len(cards) != content_count` → `card_count_mismatch` → **整单判废 + 退款**。
       也就是说"模型少写了一句注意事项"这种 A1 级问题,被级联成了整单硬失败 ——
       这与本单自己 R7 的裁定(caveat 属 A1)正面冲突,也违反永不中断铁律。

       现在:缺 caveat → 自动填 `CAVEAT_FALLBACK` 这条合法出口并**留痕**
       (`caveat_autofilled=True`),整条内容继续走。
       `entity` / `points` 缺失仍然丢卡 —— 那是结构性缺失(卡面没有主体或没有内容),
       不在本轮范围。
    """
    out: List[dict] = []
    for c in (cards or []):
        if not isinstance(c, dict):
            continue
        entity = str(c.get("entity") or c.get("headline") or "").strip()
        caveat = str(c.get("caveat") or "").strip()
        if not entity:
            continue
        autofilled = not caveat
        if autofilled:
            caveat = CAVEAT_FALLBACK
        pts = c.get("points") or []
        if isinstance(pts, str):
            pts = [pts]
        points = [clip_text(x, 60) for x in pts if str(x).strip()][:4]
        if not points:
            continue
        out.append({
            "entity": clip_text(entity, 12),
            # 🔴 声明字段原样留下,**不截断成 12 字**:R1 拿它做集合成员判定,
            #    截了就会把「深圳市恒通电梯有限公司」变成一个查不到的短名。
            #    卡组型下模型不会给这个键,取值是空串 —— 与加它之前逐字相同。
            "entity_ref": str(c.get("entity_ref") or "").strip()[:60],
            "one_liner": clip_text(c.get("one_liner"), 20),
            "points": points,
            "metric": clip_text(c.get("metric"), 24),
            "caveat": clip_text(caveat, 30),
            # headline 供生图模板用(与卡面主标题同源)
            "headline": clip_text(entity, 12),
            # 留痕:这张的 caveat 是兜底填的,不是模型给的。
            # 前端据此可以提示"这条注意事项是自动补的,建议按实际改一下"(A1 局部补齐)。
            "caveat_autofilled": autofilled,
        })
    return out[:want]


def _parse_llm_json(raw: str) -> Optional[dict]:
    """LLM 有时会包 ```json 围栏或前后加话,这里做容错解析。"""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 退一步:抓第一个 {...} 块
    brace = re.search(r"\{.*\}", text, re.S)
    if brace:
        try:
            obj = json.loads(brace.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def _call_llm(prompt: str, *,
                    timeout_s: Optional[float] = None,
                    diag: Optional[dict] = None) -> Optional[str]:
    """真实 HTTP 调用(真异步 httpx,绝不同步阻塞事件循环 —— 工单 §3.2 红线)。

    timeout_s:超时预算。**不传 = 沿用 `_LLM_TIMEOUT_S`(180s)**,
        生产那条异步链行为逐字不变 —— 它跑在后台任务里,不受 nginx 约束。

    🔴 为什么要有这个参数:**同步端点**没有 180s 可用。
       生产 nginx 对 `/api/` 是 `proxy_read_timeout 60s`,超了就返回一个
       **HTML 504 页面** —— 前端 `.json()` 当场抛 `Unexpected token '<'`,
       而后端还在跑,跑成了照样扣费。用户付了钱只看到一句乱码。
       (2026-08-03 Owner 实测撞到,截图为证。)
       所以同步调用方必须自己传一个**装得进 nginx 窗口**的预算,
       让它在窗口内**干净失败** → 走 `charge_on_success` 的不扣费路径。

    diag:调用方给一个空 dict,本函数往里写 `reason`(失败原因),让上游能把
        **"服务不可用"和"写超了被截断"分开报**。返回值形态一个字没改
        (仍是 `Optional[str]`),monkeypatch 这个 seam 的测试假体不受影响。

    测试 monkeypatch 这个 seam。
    """
    if diag is not None:
        diag.clear()
    api_key = _get_api_key()
    if not api_key:
        logger.warning("[douyin-content] 无 DeepSeek key")
        if diag is not None:
            diag["reason"] = "no_key"
        return None
    import httpx
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        # 🔴 2026-08-03 热修(止血)· 1600 → 8000。**只动这一个参数。**
        #   根因:`deepseek-v4-flash` 是**推理模型**,`reasoning_tokens` 计入 `max_tokens`。
        #   生产实测(同一条真实 prompt,1388 字符,card_count=5):
        #     max_tokens=1600 → finish_reason=length · completion=1600 **全部是 reasoning**
        #                       · content 长度 **0** → 解析必失败
        #     max_tokens=4000 → finish_reason=stop   · completion=757 仍全是 reasoning
        #                       · content 长度 **0** → 仍失败(**说明不是线性关系,小幅上调无效**)
        #     max_tokens=8000 → finish_reason=stop   · completion=6057(reasoning 5489 + 内容 568)
        #                       · content 1120 字符 → 解析出 body/cards/cover/closing ✅
        #   即:1600 这个值下本链**不可能成功**,不是偶发。Owner 试跑 post=4
        #   落库 `stage=copy · error_msg=llm_bad_json`,image_client 零日志(根本没到生图那跳)。
        #   🔴 换模型/关推理/加 JSON mode 都不在本次范围 —— 止血只调这一个值,余下走审计。
        #
        # 🔴 2026-08-03 二次定值 · 8000 → 16000(Owner「提额度」· 5 次实测定档)。
        #   8000 档实测只有 2/3:失败那次 `completion=7999 reasoning=7999 content=0`。
        #   机理:**撞天花板本身就是病因** —— 推理被截断,内容无处可去。给足余量后
        #   推理反而自然收敛。同一条 prompt 各 5 次:
        #     16000 → **5/5 解析 OK** · 23.6–47.4s · reasoning 仅 1463–4050 · content 841–1153
        #     24000 → 也全绿,但出现 **169.8s 长尾**(reasoning 放飞到 17243)
        #   → 取 16000:全绿且耗时上限最低。**更大不是更好**,额度越大跑飞的空间越大。
        #
        # 🔴 2026-08-06 · 定值改成阶梯(生产实证 `MAX_TOKENS_LADDER`,见常量处)。
        "max_tokens": MAX_TOKENS_LADDER[0],
    }
    from tools.llm_call_tracker import llm_track

    for budget in MAX_TOKENS_LADDER:
        body["max_tokens"] = budget
        try:
            async with llm_track("geo_douyin_content", "deepseek_official",
                                 model=LLM_MODEL) as tracker:
                async with httpx.AsyncClient(
                        # 🔴 二次尝试**不放大调用方给的预算**。上一次事故正是
                        #    "后端自己用了比调用方窗口更长的时间" —— nginx 60s 掐断、
                        #    后台还在跑、钱照扣。预算装不下就干净失败,不偷时间。
                        timeout=float(timeout_s or _LLM_TIMEOUT_S)) as client:
                    resp = await client.post(
                        _DEEPSEEK_URL,
                        headers={"Authorization": f"Bearer {api_key}",
                                 "Content-Type": "application/json"},
                        json=body,
                    )
                if resp.status_code != 200:
                    tracker.record(success=False, error_msg=f"HTTP {resp.status_code}")
                    logger.warning("[douyin-content] LLM HTTP %s", resp.status_code)
                    if diag is not None:
                        diag["reason"] = "http_error"
                        diag["http_status"] = resp.status_code
                    return None
                payload = resp.json()
                usage = payload.get("usage") or {}
                choice = (payload.get("choices") or [{}])[0] or {}
                text = str((choice.get("message") or {}).get("content") or "").strip()
                finish = str(choice.get("finish_reason") or "")

                # 🔴 撞顶 = `finish_reason == "length"` 且正文为空。
                #    这不是"服务不可用" —— HTTP 是 200,模型也真跑了,只是推理把
                #    额度吃光、正文无处可放。旧代码把它和"连不上"一起返回 None,
                #    上游一律报「写作服务暂时不可用,稍后重试」——
                #    于是用户重试一次撞一次(生产 7 天 5 次全是这样)。
                truncated = (not text) and finish == "length"
                tracker.record(
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    # 🔴 观测面不许撒谎:撞顶时旧代码记的是 success=True,
                    #    于是日志说"成功"、任务说"失败",排障时两边对不上。
                    success=not truncated,
                    error_msg=(f"truncated: finish=length "
                               f"completion={usage.get('completion_tokens', 0)} "
                               f"budget={budget}") if truncated else "",
                )
                if text:
                    return text
                if not truncated:
                    # 200 但正文空、finish 也不是 length —— 说不清的空,不硬猜。
                    logger.warning("[douyin-content] LLM 返回空正文 finish=%s", finish)
                    if diag is not None:
                        diag["reason"] = "empty_content"
                        diag["finish_reason"] = finish
                    return None
                logger.warning(
                    "[douyin-content] 撞 max_tokens 天花板 budget=%s completion=%s "
                    "prompt=%s —— %s", budget, usage.get("completion_tokens", 0),
                    usage.get("prompt_tokens", 0),
                    "换更大额度重试" if budget != MAX_TOKENS_LADDER[-1] else "阶梯已用尽")
                if diag is not None:
                    diag["reason"] = "truncated"
                    diag["budget_used"] = budget
                    diag["prompt_tokens"] = usage.get("prompt_tokens", 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("[douyin-content] LLM 异常: %s", str(e)[:160])
            if diag is not None:
                diag["reason"] = "exception"
            return None
    return None


async def generate_image_post_content(
    keyword: str,
    *,
    city: Optional[str] = None,
    brand_name: str = "",
    card_count: int = CARD_COUNT_DEFAULT,
    extra_hint: str = "",
    brand_id: Optional[int] = None,
    skeleton: str = DEFAULT_SKELETON,
    closing_kind: str = DEFAULT_CLOSING,
    industry_key: str = "",
    # 🔴 榜单冻结件(`ranking_router.RankingPlan`)。None = 卡组型,
    #    此时下面拼 prompt 的产出与加这个参数之前**逐字相同**。
    ranking_plan: Any = None,
) -> GeneratedContent:
    """生成图文帖的正文 + 封面/内容卡/收尾卡。

    🔴 **会引用客户自己的资料**(brand_id 传了才生效):公司简介/核心卖点/案例/资质
       + 客户知识库检索。理由不只是"更贴客户" —— §6c 实测显示豆包采纳看的是
       **信息量/可回答性**(热度完全无关),客户真实资料正好是最高质量的信息来源。

    fail-closed:LLM 不可用/解析失败 → ok=False,由调用方 release_freeze 退积分,
    **绝不返回半成品当成功**(半成品发出去比不发更糟)。
    """
    kw = (keyword or "").strip()
    if not kw:
        return GeneratedContent(body="", ok=False, error="keyword 不能为空")

    want = max(CARD_COUNT_MIN, min(int(card_count or CARD_COUNT_DEFAULT), CARD_COUNT_MAX))
    # 🔴 张数控制第 1 层(规范 §8.1a):`want` 是**代码层结构化参数**,
    #    它决定计费与产出数量,不交给任何模型。下面的职责计划由它推出来。
    from services.geo_douyin.series_plan import content_role_plan, roles_prompt_block

    # 🔴 2026-08-07 P1-4:榜单形态走**构造式职责表** —— 每张企业卡讲哪一家
    #    由代码指派,不交给模型。通用杂志阶梯(需求定义/比较口径/成本结构…)
    #    里根本没有"这张讲哪一家"这个概念,缺了它就只能反过来去正文里猜公司名。
    _ranking_names: List[str] = []
    if ranking_plan is not None:
        _ranking_names = [it.display_name for it in ranking_plan.payload.items]
    if _ranking_names:
        from services.geo_douyin.series_plan import (plan_ranking_roles,
                                                     ranking_roles_prompt_block)
        full_plan = plan_ranking_roles(want, _ranking_names)
        role_plan = [r for r in full_plan if r["role"] not in ("cover", "closing")]
        # 🔴 2026-08-08 A 层:人工确认名单里的**画像**进 prompt 当选材背景。
        #    只影响"这一张从哪个角度写",不影响讲哪一家(那仍由代码指派)。
        role_block = ranking_roles_prompt_block(want, _ranking_names,
                                                list(ranking_plan.statements),
                                                profiles=getattr(ranking_plan,
                                                                 "profiles", None))
    else:
        role_plan = content_role_plan(want)      # 除封面/收尾之外的内容卡职责
        role_block = roles_prompt_block(want)    # 第 2 层:降档表逐字进 prompt
    content_count = len(role_plan)

    skel = skeleton if skeleton in SKELETONS else DEFAULT_SKELETON
    skel_name, skel_desc, _ = SKELETONS[skel]
    closing = closing_kind if closing_kind in CLOSING_KINDS else DEFAULT_CLOSING

    # 客户素材(取不到就是空,降级为通用内容,不阻断)
    ctx = await build_brand_context(brand_id, kw, brand_name)
    materials = ctx.to_prompt_block()
    materials_block = (
        f"\n【客户自有素材 —— 优先用这些真实信息,不要凭空编】\n{materials}\n"
        if materials else ""
    )

    prompt = _PROMPT.format(
        ad_law_block=ad_law_prompt_block(),
        ranking_block=ranking_prompt_block(ranking_plan),
        ranking_card_field=ranking_card_field(ranking_plan),
        body_min=BODY_LEN_MIN, body_max=BODY_LEN_MAX,
        card_count=want, content_count=content_count,
        role_plan=role_block,
        skeleton_name=skel_name, skeleton_desc=skel_desc,
        hook_line=COVER_HOOKS[pick_hook(industry_key)],
        audience_line=audience_style_block(industry_key),
        closing_name=CLOSING_KINDS[closing],
        cover_max=COVER_TEXT_MAX, content_min=CONTENT_TEXT_MIN,
        content_max=CONTENT_TEXT_MAX, closing_max=CLOSING_TEXT_MAX,
        keyword=kw,
        city_line=f"目标城市:{city}\n" if city else "",
        brand_line=f"品牌:{brand_name}\n" if brand_name else "",
        # 🔴 2026-08-05:原来只有上面那行 `品牌:xxx`,**没有任何一句说明拿它做什么**。
        #    这一段才是"这条内容是给谁拉生意的"。
        promote_block=promote_block(brand_name or ctx.brand_name,
                                    city=city, industry_key=industry_key),
        extra_line=f"补充要求:{extra_hint}\n" if extra_hint else "",
        materials_block=materials_block,
    )

    # 🔴 张数控制第 3 层(规范 §8.1a):**硬校验 + 少了就重试**,禁止静默凑数。
    #    多给 → `_clamp_cards` 裁到 content_count(裁掉多余的不影响叙事闭环);
    #    少给 → 重试一次(LLM 少给通常是偶发,不是系统性的);
    #    仍不齐 → **明确失败**,让上游 release_freeze 退款。
    #    绝不"有几张算几张":用户按 N 张付的钱,拿到 N-2 张是少交付,
    #    而静默少给最难被发现 —— 图都在,只是少了两张。
    obj = None
    cards: List[dict] = []
    body = ""
    for attempt in (1, 2):
        diag: dict = {}
        raw = await _call_llm(prompt, diag=diag)
        if not raw:
            # 🔴 "写超了被截断"和"服务连不上"是两回事,报同一句话会把用户
            #    引到一个永远无效的动作上(重试 → 同一条 prompt → 再撞顶)。
            return GeneratedContent(
                body="", ok=False,
                error=("llm_truncated" if diag.get("reason") == "truncated"
                       else "llm_unavailable"))
        obj = _parse_llm_json(raw)
        if not obj:
            if attempt == 2:
                return GeneratedContent(body="", ok=False, error="llm_bad_json")
            continue
        body = str(obj.get("body") or "").strip()
        cards = _clamp_cards(obj.get("cards") or [], content_count)
        if body and len(cards) == content_count:
            break
        logger.warning("[douyin-content] 第 %s 次要 %s 张内容卡,只拿到 %s 张",
                       attempt, content_count, len(cards))
    else:
        obj = obj or {}

    cover = obj.get("cover") if isinstance(obj.get("cover"), dict) else {}
    closing_obj = obj.get("closing") if isinstance(obj.get("closing"), dict) else {}

    if not body or len(cards) != content_count:
        return GeneratedContent(
            body=body, cards=cards, ok=False,
            error=f"card_count_mismatch: 需要 {content_count} 张,实得 {len(cards)} 张"
                  if body else "llm_incomplete")

    # 职责回填:role / layout_role 由**代码**给,模型对版式和张数都没有话语权。
    for card, plan in zip(cards, role_plan):
        card["role"] = plan["role"]
        card["layout_role"] = plan["layout_role"]
        card["role_label"] = plan["label"]
        # 🔴 P1-4:榜单形态下 `entity_ref` 也由**代码**写死,覆盖模型给的任何值。
        #    这就把"模型省略 entity_ref 就能绕过虚构企业检查"这条路
        #    **在结构上**堵死 —— 那个字段根本不由模型填。
        #    R1 剩下的活变成"卡面印的名字与代码指派的那一家对不对得上"。
        if plan.get("entity_name"):
            card["entity_ref"] = str(plan["entity_name"])
            card["entity_index"] = int(plan.get("entity_index") or 0)
            # 🔴 [WP3 · 规格 02 §10.2 · 2026-08-17] P1-4 当时只写死了 `entity_ref`,
            #    可见的 `entity` / `headline` 仍是模型给的那个(上面 normalize 里
            #    `clip_text(entity, 12)`)。而 `image_pipeline.py:160,164` 烘进图片的
            #    **正是可见字段** —— 于是"引用对了"和"图上印的名字对了"是两条路,
            #    P1-4 只堵了引用那条。品牌占位符能出现在成品图上就是这么来的。
            #    现在三处同源:引用 / 可见名 / 生图 headline 全部由代码从同一个
            #    冻结名写入,模型对 display name 没有话语权。
            from services.geo_douyin.frozen_entity import DISPLAY_NAME_MAX

            _frozen_name = str(plan["entity_name"])
            card["entity"] = _frozen_name[:DISPLAY_NAME_MAX]
            card["headline"] = _frozen_name[:DISPLAY_NAME_MAX]
            card["frozen_entity"] = {
                "entity_id": str(plan.get("entity_id") or f"ranking:{plan.get('entity_index') or 0}"),
                "display_name": _frozen_name,
                "source": str(plan.get("entity_source") or "ranking_frozen_candidate"),
                "source_snapshot_hash": str(plan.get("entity_snapshot_hash") or ""),
            }

    flags = scan_ad_law(body)
    for c in cards:
        for k in ("entity", "one_liner", "metric", "caveat"):
            flags.extend(scan_ad_law(c.get(k, "")))
        for p in c.get("points", []):
            flags.extend(scan_ad_law(p))
    flags.extend(scan_ad_law(str(cover.get("title", ""))))
    # [WO_262] 广告法扫描也要看**归一化之后**的文字:模型回列表时,
    # `str(list)` 出来的串里词是被引号和逗号切开的,极限词可能扫不到。
    flags.extend(scan_ad_law(flatten_text(closing_obj.get("summary", ""))))

    from services.geo_douyin.series_plan import cover_aux_labels
    from services.geo_douyin.title_engine import _title_hard_limit

    # 🔴 视觉身份:只收白名单三个键,值裁短。模型多给的字段一律丢 ——
    #    生图 prompt 是拼进去的,放任意字段进去等于给了模型一条注入通道。
    raw_visual = obj.get("visual") if isinstance(obj.get("visual"), dict) else {}
    visual = {k: str(raw_visual.get(k) or "").strip()[:60]
              for k in ("scene", "primary_color", "accent_color")}
    visual = {k: v for k, v in visual.items() if v}

    hashtags = [str(h).strip().lstrip("#")[:20]
                for h in (obj.get("hashtags") or []) if str(h).strip()][:5]
    who = (brand_name or ctx.brand_name or "").strip()

    out = GeneratedContent(
        body=body, cards=cards,
        cover={"title": clip_text(cover.get("title") or kw, COVER_TEXT_MAX),
               "subtitle": clip_text(cover.get("subtitle"), 40),
               # 🔴 规范 §8.4:封面底部导航词必须是**冻结文案**。
               #    由代码按这一组的实际职责生成 —— 模型自加的话,
               #    验收第 5 条"逐字一致"永远过不了。
               "aux_labels": cover_aux_labels(want)},
        closing={"headline": clip_text(closing_obj.get("headline") or "选购总结", 20),
                 "summary": clip_text(closing_obj.get("summary"), CLOSING_TEXT_MAX),
                 # 规范 §8.2:收口卡必须带 caveat。模型没给就用通用前提兜底 ——
                 # 这一条是**合规提示**,不是编造事实,兜底不违反"不编"的原则。
                 "caveat": clip_text(closing_obj.get("caveat")
                                     or "具体方案和费用因需求而异，以实际沟通为准", 40),
                 # 🔴 豆包要摘出来的那一句(谁/在哪/做什么/凭什么)。
                 #    **不兜底**:兜底就等于我们替客户编了一句推荐语。
                 # 客户名截断后必须还在 —— 切掉就交空串,不交半句推荐语。
                 # 🔴 上限 80 → BRAND_LINE_MAX(140)。80 是我当初拍的,**没有版面理由**:
                 #    `build_closing_prompt` 只印 headline/summary/caveat/contact,
                 #    brand_line 根本不上图,它是给豆包摘的那一句、只落库。
                 #    本机真跑 12 次实测:中位 70、max 104、**33% 超 80** ——
                 #    也就是每三条就有一条被截,而截断处正是"凭什么"那半句。
                 "brand_line": clip_text(closing_obj.get("brand_line"),
                                         BRAND_LINE_MAX, keep=who)},
        skeleton=skel, kb_sources=list(ctx.sources_used),
        # 🔴 与 kb_sources 分开带:后者是给用户看的留痕(logo 也算"用到了"),
        #    前者是"能不能画实拍"的资格。拿留痕去判资格正是 Review 攻破的那个洞。
        has_real_photo=ctx.has_real_photo,
        visual=visual,
        # 🔴 硬上限的 SSOT 在发布侧(平台限制),这里复用 title_engine 的取法,
        #    不另立一份数字 —— 两份必漂。
        title=str(obj.get("title") or "").strip()[:_title_hard_limit()],
        hashtags=hashtags,
        brand_name_expected=who,
        ad_law_flags=sorted(set(flags)))

    # 🔴 P0(Owner 2026-08-05):**不点名客户的内容一律作废**。
    #    这是这个产品存在的理由 —— 豆包要在回答里推荐这个客户,
    #    就必须先能从内容里读到这个名字。做了一条不含客户名的内容 = 花 390 做公益。
    #    失败而不是降级:降级(比如自己把名字塞进去)会产出一句我们替客户编的推荐语。
    if who and not out.promotes_brand:
        logger.warning("[douyin-content] 产出没有点名客户 brand=%s —— 判废", who)
        return GeneratedContent(body=body, cards=cards, ok=False,
                                error="brand_not_promoted",
                                brand_name_expected=who)
    if not who:
        logger.warning("[douyin-content] 没拿到客户名称,无法判定是否点名 —— 判废")
        return GeneratedContent(body=body, cards=cards, ok=False,
                                error="brand_name_missing")

    # 🔴 P0(Owner 2026-08-06):**自卖自夸一律作废**。与上面那条配成一对 ——
    #    一头拦"不点名"(等于没推广),一头拦"第一人称自述"(等于打广告),
    #    中间那条窄路(第三方口吻 + 点名)才是这个产品要的东西。
    #    同样失败而不降级:降级=我们替客户改口吻,改出来的还是我们写的推荐语。
    hit = out.self_praise
    if hit:
        logger.warning("[douyin-content] 产出用了卖方自称「%s」brand=%s —— 判废",
                       hit, who)
        return GeneratedContent(body=body, cards=cards, ok=False,
                                error="self_praise_voice",
                                brand_name_expected=who)
    return out
