"""抖音选题蒸馏器 · 从「客户知识库 + 被采纳语料 + 竞品」蒸出可做的选题

Owner 2026-08-03:
> 「我们的飞轮应该有一个抖音选题蒸馏器,把每次被抖音成功检索引用的选题正文
>   标签方式方法或者方法论总结成一个提示词,或者选题库,让 LLM 通过用户的知识库,
>   来蒸馏他应该根据前面报价这里添加几条图文,一键把选图和正文蒸馏出来,以及竞品」

## 三个输入

  ① 客户知识库   `services/client_knowledge.build_client_knowledge` + 全量物料
  ② 被采纳语料   `db/douyin_corpus_db.load_fewshot`(**同行业图文帖优先**)
  ③ 竞品与关键词 `keyword_insights.brands_found` + 报价里已选的关键词

## 🔴 为什么 few-shot 喂**原样 caption**,而不是先抽象成"排行榜体/避坑体"

调研实证(被采纳 120 条 vs 仅被检索 142 条,同一批 prompt 下):

| 形态特征 | 被采纳 | 仅检索 |
|---|---|---|
| 榜单排行 | 13.3% | 13.4% |
| 结构化枚举 | 8.3% | **14.1%** |
| 有数字 | 40.8% | **56.3%** |
| 第一人称口播 | **23.3%** | 16.2% |

**形态标签几乎零判别力,一半还是负的。** 把语料蒸馏成"体裁标签"再喂给模型,
等于把这个已经被证伪的维度又固化一遍(`title_engine` 的模板池就是这么来的:
按"被采纳里 55% 是选型问法"配权重,但对照组里也是 55%)。
所以这里**不抽象**,直接给真实 caption 让模型学语感 —— 唯一被数据支持的正向
特征(第一人称口播)恰恰是抽象成标签之后最容易丢掉的东西。

## 🔴 禁抄段 + 串味检测(Review §18a ① 硬要求)

few-shot 是**语感参考**,不是内容素材。样本里的品牌名/机构名/价格/城市
全都是别人的 —— 抄进客户的帖子里就是"串味",轻则不实,重则是替竞品打广告。
两道防线:
  1. prompt 里的 `NO_COPY_BLOCK`(与生图侧 `REFERENCE_NO_COPY_BLOCK` 同一思路:
     实测不加这段,带参考的生成 3/3 都会渗漏);
  2. 生成后 `detect_contamination()` 做**确定性**检测(连续 N 字原样出现即判串味),
     命中就丢掉那条选题 —— fail-closed,不返回半污染的结果。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from tools.pricing_bands import normalize_article_capacity

logger = logging.getLogger("GEO-Douyin-Distill")

# 一次最多蒸几条选题。再多用户也读不完,而且 prompt 会被撑长。
TOPIC_COUNT_MIN = 1
TOPIC_COUNT_MAX = 8

# 蒸馏 LLM 调用预算。
#
# ## 现在这个数是怎么来的(2026-08-05 · WO-DISTILL-ASYNC 第 3 条要求"写明依据")
#
#   生产实测真实耗时 **58.0s**(放开窗口、独立进程、`timeout_s=300` 复测,
#   返回 1077 字符、解析 OK、topics 3 条 —— 功能本身是好的,只是慢)。
#   取 **58 × 2 ≈ 120s** 作预算:一倍余量吃掉推理模型的长尾抖动
#   (同型调用 `content_generator._LLM_TIMEOUT_S` 也是 180,不是孤例)。
#
# ## 🔴 为什么原来是 42,以及为什么那个理由现在**不再成立**
#
#   42 是同步时代的产物:那时整个请求要装进容器内 nginx 的
#   `proxy_read_timeout 60s`,超窗 nginx 直接回 HTML 504,
#   前端 `.json()` 抛 `Unexpected token '<'`,而后端还在跑、跑成了照样扣 130。
#   于是刻意把 LLM 掐在 42s —— 宁可"没蒸出来、不扣钱",也不要"扣了钱 + 一句乱码"。
#   老注释里那句「若失败率偏高,正解是改异步,**而不是把这个数字调大 ——
#   调大只会调回 504**」在当时是对的。
#
#   🔴 **2026-08-05 改异步后,那句话的前提消失了**:提交 2 秒就返回,
#   nginx 只管那 2 秒,LLM 在后台跑,**没有墙了**。
#   而 42 < 58 —— 保留 42 的后果不是"偶尔失败",是**每次都在 42s 被掐死**,
#   成功率仍然 0/5,只是从"504 乱码 + 扣费"变成"礼貌失败 + 不扣费"。
#   把 nginx 时代的取值原样搬进异步世界,等于异步白改。
#   (这正是本包第一版交付漏掉的那一点,Review 全矩阵变异抓出来的。)
#
# ## 跟着这个数走的两处(改这里,它们自动跟上,别再各写一个数)
#
#   · `distill_task.stale_after_seconds()` = 本值 + 60 → 超龄回收阈值(现 180s);
#   · 前端**不再**另设轮询时限:旧入口页的 MAX_POLLS 随页删除(09-08);现在的选题面板每 3 秒
#     问一次,停不停只听服务端给的状态 —— 超龄回收之后服务端回的是终态。[WO_271]
#
# ⚠️ 仍然有上界(不是"越大越好"):预算越大,一次卡死的调用把这个客户的
#    in-flight 占位扣得越久(超龄回收也跟着变长)。守在 tests 里,两个方向都锁。
DISTILL_LLM_TIMEOUT_S = 120.0
TOPIC_COUNT_DEFAULT = 5

# few-shot 条数。6 条足够让模型抓到语感,再多就开始主导输出。
FEWSHOT_LIMIT = 6

# 串味判定:样本里连续多少个字原样出现在产出里,就算抄了。
# 🔴 取 6:中文里 6 字连续重合已经几乎不可能是巧合(常用词组最长 4-5 字),
#    而降到 4 会把"全屋定制哪家"这类**查询词本身**误判成抄袭。
CONTAMINATION_RUN = 6

NO_COPY_BLOCK = (
    "🔴 上面那些【真实样本】只是给你看**别人怎么说话**的,是语感参考。\n"
    "  - 绝对不要照抄样本里的任何品牌名、机构名、人名、门店名、价格、电话、"
    "城市、话题标签或成段文字;\n"
    "  - 样本里的公司不是这个客户的竞品也不是合作方,写进去就是给别人打广告;\n"
    "  - 你输出的每一个具体信息都必须来自【客户自有资料】或【要打的关键词】,"
    "没有依据的就不写,**不要编**。"
)


@dataclass
class DistilledTopic:
    title: str = ""
    angle: str = ""                                     # 正文方向(怎么把词打出去)
    card_outline: List[str] = field(default_factory=list)   # 每张卡讲什么
    competitor_points: List[str] = field(default_factory=list)
    keyword: str = ""

    def to_dict(self) -> dict:
        return {"title": self.title, "angle": self.angle,
                "card_outline": list(self.card_outline),
                "competitor_points": list(self.competitor_points),
                "keyword": self.keyword}


@dataclass
class DistillResult:
    topics: List[DistilledTopic] = field(default_factory=list)
    fewshot_used: int = 0
    fewshot_degraded: str = ""      # 空串 = 没降级;否则是降级原因
    kb_sources: List[str] = field(default_factory=list)
    dropped_contaminated: int = 0   # 因串味被丢掉几条
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {"topics": [t.to_dict() for t in self.topics],
                "fewshot_used": self.fewshot_used,
                "fewshot_degraded": self.fewshot_degraded,
                "kb_sources": list(self.kb_sources),
                "dropped_contaminated": self.dropped_contaminated,
                "ok": self.ok, "error": self.error}


_PUNCT_RE = re.compile(r"[^一-龥a-zA-Z0-9]+")


def _normalize(text: str) -> str:
    """只留中英数 —— 标点/空白/emoji 不参与串味判定,不然改个标点就绕过了。"""
    return _PUNCT_RE.sub("", str(text or ""))


def detect_contamination(output_text: str, samples: List[str],
                         run: int = CONTAMINATION_RUN,
                         allow: Optional[List[str]] = None) -> List[str]:
    """产出里有没有原样抄样本。返回命中的片段(去重,最多 5 条)。

    做法:把样本按 `run` 长度切成滑窗,看有没有原样落在产出里。
    **确定性**,不靠模型自评 —— 让模型判断自己有没有抄,等于没判。

    allow: 白名单片段(客户自己的资料、要打的关键词)。这些字串本来就该出现在
        产出里,如果它恰好也出现在某条样本里,那不是抄袭。
        🔴 没有这个白名单会误伤:样本和产出都在讲同一个行业,
           "全屋定制哪家好" 这种查询词本身就会两边都有。
    """
    out = _normalize(output_text)
    if not out:
        return []
    allow_norm = [_normalize(a) for a in (allow or []) if _normalize(a)]
    hits: List[str] = []
    seen: set = set()
    for s in samples:
        norm = _normalize(s)
        for i in range(0, max(0, len(norm) - run + 1)):
            frag = norm[i:i + run]
            if frag in seen or frag not in out:
                continue
            if any(frag in a for a in allow_norm):
                continue
            seen.add(frag)
            hits.append(frag)
            if len(hits) >= 5:
                return hits
    return hits


def build_distill_prompt(*, keywords: List[str], brand_name: str, city: str,
                         materials_block: str, competitors: List[str],
                         samples: List[dict], want: int) -> str:
    """拼蒸馏 prompt。**样本原样进,不做体裁抽象**(理由见模块 docstring)。"""
    sample_lines = []
    for i, s in enumerate(samples, 1):
        kind = "图文帖" if s.get("is_image_post") else "视频"
        sample_lines.append(f"{i}. [{kind}] {str(s.get('caption') or '').strip()}")
    samples_block = "\n".join(sample_lines) if sample_lines else "(暂无样本)"

    kw_block = "、".join([k for k in keywords if k][:12]) or "(未指定)"
    comp_block = "、".join([c for c in competitors if c][:10]) or "(暂无竞品情报)"

    return f"""你在给一个客户策划要发到抖音的「图文帖」选题。目标不是涨粉,
是让 AI 搜索(豆包)在回答相关问题时**引用到这个客户**。

【真实样本 —— 这些内容确实被 AI 搜索引用过】
{samples_block}

{NO_COPY_BLOCK}

【怎么学这些样本】
- 学**说话的方式**:口语、第一人称(我/咱/大家)、把结论直接说出来;
- 不要学"排行榜体""避坑体"这类套路 —— 实测这些形态对"被引用"没有帮助;
- 抖音的标题和正文是**连在一起的一整段**,第一句就要给答案,别写"大家好"。

【这个客户是谁】
品牌:{brand_name or '(未提供)'}
{('城市:' + city) if city else ''}
{materials_block or '(暂无客户自有资料 —— 那就只写通用判断,不要编具体信息)'}

【要打的关键词】{kw_block}
【这个赛道里出现过的其他品牌】{comp_block}
  ⚠️ 竞品只用来判断"客户该从哪个角度说才有区分度",
     **不要在文案里点名贬低任何一家**(广告法 + 平台规则)。

【产出】{want} 条选题。每条都要能直接开做,不要"可以考虑…"这种废话。
每条包含:
  title          帖子标题(就是那一整段 caption 的开头,26-45 字,必须自然含关键词)
  angle          正文方向:用什么角度把这个词打出去、凭什么值得被 AI 引用
  card_outline   每张卡讲什么(3-5 条,一条一张卡,第一张是封面)
  competitor_points 和其他家比,这个客户能站住的差异点(0-3 条,没有就空数组)
  keyword        这条选题主打上面哪个关键词

只输出 JSON,不要解释:
{{"topics": [{{"title": "", "angle": "", "card_outline": ["", ""],
  "competitor_points": [], "keyword": ""}}]}}"""


def _parse(raw: str) -> Optional[dict]:
    from services.geo_douyin.content_generator import _parse_llm_json
    return _parse_llm_json(raw)


#: 竞品情报新鲜度窗口(天)。超窗的行不进 prompt —— 半年前的赛道格局拿来定
#: "客户该从哪个角度说才有区分度",给出的角度本身就是过期的。
COMPETITOR_WINDOW_DAYS = 180

#: `keyword_insights.quality_flag` 的 CHECK 取值为 auto/verified/rejected/degraded。
#: 只收前两者:`rejected` 是已判废的蒸馏,`degraded` 是降级产物,都不该当情报用。
COMPETITOR_QUALITY_FLAGS = ("auto", "verified")


async def load_competitors(brand_id: Optional[int], keywords: List[str],
                           client_brand: str = "") -> List[str]:
    """从 `keyword_insights` 取这个客户赛道里出现过的品牌。

    这张表是**监测蒸馏**的既有产物 —— 不新造一套竞品发现。

    🔴 2026-08-06 修四处(WO_GEO_DOUYIN_RANKING_TEMPLATES v3 §6.1):

    1. **`keywords` 形参此前在函数体里从未被引用** —— SQL 只按 `brand_id` 查、
       `LIMIT 20` 再 `names[:10]`,实际吐的是"最近蒸馏那一批的前 10 家",
       与"这条内容要打哪个词"完全无关。现按词收窄。
    2. **无客户自我排除** —— 客户自己会出现在自己的 `brands_found` 里
       (生产实测:brand 1 的名字在自己的 brands_found 里出现 65 次),
       于是客户被当竞品喂进 prompt,再被「不要点名贬低任何一家」当外人处理。
    3. **无时效闸** —— 这是全仓唯一一处读 `keyword_insights` 不带时间窗的。
    4. **无质量闸** —— 已知蒸馏失败/降级的行也会被当情报。
    """
    import asyncio

    if not brand_id:
        return []
    kws = [str(k).strip() for k in (keywords or []) if str(k or "").strip()]

    def _read() -> List[str]:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            params: list = [int(brand_id), str(COMPETITOR_WINDOW_DAYS),
                            list(COMPETITOR_QUALITY_FLAGS)]
            kw_clause = ""
            if kws:
                kw_clause = " AND keyword = ANY(%s)"
                params.append(kws)
            cur.execute(
                f"""SELECT brands_found FROM keyword_insights
                     WHERE brand_id = %s
                       AND brands_found IS NOT NULL
                       AND distilled_at >= NOW() - (%s || ' days')::interval
                       AND quality_flag = ANY(%s){kw_clause}
                     ORDER BY distilled_at DESC NULLS LAST LIMIT 20""",
                tuple(params),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        # 客户自我排除:按 safe_merge_key 比,`深圳市X有限公司` 与 `X` 算同一家
        from services.geo_douyin.ranking_payload import safe_merge_key
        self_key = safe_merge_key(client_brand) if client_brand else ""
        names: List[str] = []
        for r in rows:
            raw = r.get("brands_found")
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                parsed = parsed.get("brands") or []
            for item in (parsed or []):
                name = item.get("name") if isinstance(item, dict) else item
                name = str(name or "").strip()
                if not name or name in names:
                    continue
                if self_key and safe_merge_key(name) == self_key:
                    continue          # 客户本人不是自己的竞品
                names.append(name)
        return names[:10]

    try:
        return await asyncio.to_thread(_read)
    except Exception as e:  # noqa: BLE001 - 竞品取不到不该挡住蒸馏
        logger.warning("[douyin-distill] 竞品读取失败 brand=%s: %s", brand_id, str(e)[:160])
        return []


# 不是城市的城市字段值。`quotes.city` 是自由文本,这几个是生产实测里的真值。
_NON_CITY = frozenset(("全国", "全球", "海外", "不限", "各地", "全网", "线上"))
# 省级行政区后缀,以及"市/区/县"层级 —— 归一化到**市**一级。
_PROVINCE_SUFFIX = ("省", "自治区", "特别行政区")


def parse_cities(raw: str, limit: int = 8) -> List[str]:
    """把 `quotes.city` 的自由文本归一化成城市名列表。

    🔴 规则是照**生产真值**写的,不是照理想格式写的(只读通道 2026-08-03 实测):
        "广东省深圳市"           → ["深圳"]
        "深圳"                   → ["深圳"]
        "上海、北京、杭州、深圳"  → ["上海","北京","杭州","深圳"]
        "云南省曲靖市罗平县"      → ["曲靖"]      (归到市一级)
        "全国" / "全国 海外"      → []            (不是城市,**不编**)

    🔴 认不出来就返回空,让用户自己填 —— 绝不猜一个城市。
       猜错的后果是给贵阳的客户做一组深圳的内容,比空着糟得多。
    """
    import re as _re

    out: List[str] = []
    for token in _re.split(r"[、,，/／;；\s]+", str(raw or "")):
        t = token.strip()
        # 🔴 这里原来也有一道 `t in _NON_CITY` —— 与下面那道**互为兜底**,
        #    删掉任何一道另一道都能接住,于是"漏判非城市"这件事**测不出来**
        #    (变异 V9b 两次存活才暴露)。两道互相掩护 = 等于没有守卫。
        #    只留归一化**之后**那一道:要判的是变换完的结果,那才是会被用出去的值。
        if not t:
            continue
        # 剥省级前缀:"广东省深圳市" → "深圳市"
        for suf in _PROVINCE_SUFFIX:
            idx = t.find(suf)
            if 0 < idx <= 4:
                t = t[idx + len(suf):]
                break
        if not t:
            continue
        # 归到市一级:"曲靖市罗平县" → "曲靖"
        if "市" in t:
            t = t.split("市", 1)[0]
        else:
            t = t.rstrip("区县")
        t = t.strip()
        if t and t not in _NON_CITY and t not in out and len(t) <= 8:
            out.append(t)
        if len(out) >= limit:
            break
    return out


async def resolve_client_cities(brand_id: Optional[int],
                                quote_cities: Optional[List[str]] = None) -> List[str]:
    """这个客户要做哪些城市。**优先客户自己的资料**,报价单只作兜底。

    Owner 2026-08-03:「把这个默认城市去掉,自动引用客户的资料就行」。

    取数顺序:
      1. `brands.cities` —— 客户档案里填的城市(生产 142/335 有值)
      2. 该客户已确认报价单的 `quotes.city`(生产 confirmed/paid 里普遍有值)
      3. 都没有 → **返回空**,让用户自己填

    🔴 第 3 条是重点:**不给默认城市**。原来前端写死
       ['深圳','广州','杭州','成都','武汉'],那是双重错误 ——
       ① 硬编码;② 生产实测购买词本身就带城市(「贵阳观山湖区酸汤火锅推荐」),
       给贵阳的客户默认深圳/广州,会直接做出一组城市全错的内容。
       猜错比空着糟得多。

    两个来源格式一样脏("深圳" / "广东省深圳市" / "全国"),
    所以都过同一个 `parse_cities` —— 不给第二套清洗逻辑。
    """
    import asyncio

    if not brand_id:
        return list(quote_cities or [])[:8]

    def _brand_cities() -> str:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT cities FROM brands WHERE id = %s", (int(brand_id),))
            row = cur.fetchone()
            return str(dict(row).get("cities") or "") if row else ""
        finally:
            conn.close()

    try:
        raw = await asyncio.to_thread(_brand_cities)
    except Exception as e:  # noqa: BLE001 - 取不到就退到报价单,不阻断
        logger.warning("[douyin] 客户城市读取失败 brand=%s: %s", brand_id, str(e)[:120])
        raw = ""

    from_brand = parse_cities(raw)
    if from_brand:
        return from_brand
    return list(quote_cities or [])[:8]


async def load_purchased_keywords(brand_id: Optional[int],
                                  limit: int = 30) -> List[dict]:
    """这个客户**买了、且是用来产内容的**那些词。

    Owner 原话:「不应该是让客户自己填,而是我们直接帮他**按照他购买的词**
    来进行生成选题,这是我们的事」。所以词的来源是报价单,不是输入框。

    🔴 数据源与「写文章」那条链**完全同源**(实核 `get_writing_project_detail`
       / `get_writing_projects`):`confirmed_keywords` JOIN `quotes`,
       状态闸 `quotes.status IN ('confirmed','paid')`。同一件事不许两个口径。

    🔴 `ck.is_core IS NOT FALSE` 这一刀**原来漏了**。写文章那边一直有:
       `is_core = FALSE` 是**覆盖词**,客户买的不是"拿它产内容"。
       漏掉的后果不是少给,是**多给** —— 拿客户没买来做内容的词去生成图文。
       生产实测(2026-08-03 只读通道):confirmed/paid 下
       **覆盖词 163 / 核心词 602**,即我原来的查询里 21% 是不该出现的词。

    ⚠️ 与写文章的两处**刻意不同**,不是疏漏:
       1. 写文章按 `quote_id` 划(一个项目=一张报价单);这里按 `brand_id` 划 ——
          图文入口用户选的是**客户**,不是某一张报价单,所以取这个客户
          所有已确认报价的词并集。
       2. 不带回单价:图文这条链不按词计价(按张计价),露出来只会让人误解。
    """
    import asyncio

    if not brand_id:
        return []

    def _read() -> List[dict]:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            # 列名沿用 get_writing_project_detail 里已经在生产上跑着的那几个,不是猜的
            cur.execute(
                """SELECT ck.id AS confirmed_keyword_id,
                          ck.keyword,
                          ck.required_articles,
                          q.id AS quote_id,
                          q.city AS quote_city
                     FROM confirmed_keywords ck
                     JOIN quotes q ON q.id = ck.quote_id
                    WHERE q.brand_id = %s
                      AND q.status IN ('confirmed', 'paid')
                      AND ck.keyword IS NOT NULL
                      AND (ck.is_core IS NOT FALSE)
                    ORDER BY ck.id DESC
                    LIMIT %s""",
                (int(brand_id), max(1, int(limit))),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
        out: List[dict] = []
        seen: set = set()
        for r in rows:
            kw = str(r.get("keyword") or "").strip()
            if not kw or kw in seen:
                continue
            seen.add(kw)
            out.append({
                "keyword": kw,
                # 🔴 [P0-5 · 轴级 2026-08-17] 与 content_plan.py 同型缺陷(裁定 P0-5 点名的第二处)。
                #    `or 1` 把显式 0 吃掉 → 覆盖词(生产 223 行 required_articles=0)会被当成"还要做 1 条"
                #    带进选题蒸馏,再流向制作与冻结。缺失=0,显式 0 原样保留,口径与篇数 SSOT 一致。
                "required_articles": normalize_article_capacity(
                    r.get("required_articles"), when_missing=0),
                "quote_id": r.get("quote_id"),
                # [#150 §3.2] 词的**身份**。前端按它下单,服务端据此把成品记到
                #   这一张报价的这一个词上 —— 多张报价同词不再互相顶替。
                "confirmed_keyword_id": r.get("confirmed_keyword_id"),
                # 城市来自**这张报价单**,不是一个写死的默认列表。
                "quote_city": str(r.get("quote_city") or ""),
            })
        return out

    try:
        return await asyncio.to_thread(_read)
    except Exception as e:  # noqa: BLE001 - 取不到就退回让用户自己填,不阻断
        logger.warning("[douyin-distill] 报价关键词读取失败 brand=%s: %s",
                       brand_id, str(e)[:160])
        return []


async def load_quote_keywords(brand_id: Optional[int], limit: int = 12) -> List[str]:
    """同上,只要词本身。**唯一查询在 `load_purchased_keywords`**,这里只换个形状。"""
    rows = await load_purchased_keywords(brand_id, limit=limit)
    return [r["keyword"] for r in rows]


async def distill_topics(*, brand_id: Optional[int], keywords: List[str],
                         brand_name: str = "", city: str = "",
                         industry_key: str = "general",
                         want: int = TOPIC_COUNT_DEFAULT) -> DistillResult:
    """一键蒸馏选题。

    🔴 **本模块零计费**。收费(130,Owner 2026-08-03 拍板)在 API 层用
       `charge_on_success` 包着 —— 资金动作集中在一处才审得动,与
       `services/geo_douyin/redraw.py` 同一条规矩(那边也有 AST 锁盯着)。
       所以这里返回 `ok=False` 就够了,**不要**在本模块里做任何退费/扣费动作。

    fail-closed:LLM 不可用 / 解析失败 / 选题全被串味丢光 → ok=False,不返回半成品。
    调用方据此抛异常,`charge_on_success` 就不会走到扣费那一步。
    """
    import asyncio

    from db.douyin_corpus_db import load_fewshot
    from services.geo_douyin.content_generator import _call_llm
    from services.geo_douyin.knowledge_context import build_brand_context
    from services.geo_douyin.series_plan import detect_sample_taint

    n = max(TOPIC_COUNT_MIN, min(int(want or TOPIC_COUNT_DEFAULT), TOPIC_COUNT_MAX))
    kws = [str(k).strip() for k in (keywords or []) if str(k).strip()][:12]
    if not kws:
        return DistillResult(ok=False, error="没有关键词,先选要打的词")

    samples, degraded = await asyncio.to_thread(load_fewshot, industry_key, FEWSHOT_LIMIT)
    ctx = await build_brand_context(brand_id, kws[0], brand_name)
    # client_brand 传进去做**自我排除** —— 客户自己会出现在自己的 brands_found 里
    competitors = await load_competitors(
        brand_id, kws, client_brand=(brand_name or ctx.brand_name or ""))

    prompt = build_distill_prompt(
        keywords=kws, brand_name=brand_name or ctx.brand_name, city=city,
        materials_block=ctx.to_prompt_block(), competitors=competitors,
        samples=samples, want=n)

    diag: dict = {}
    raw = await _call_llm(prompt, timeout_s=DISTILL_LLM_TIMEOUT_S, diag=diag)
    if not raw:
        # 与文案链同源:撞顶要单独报,否则用户按"稍后重试"永远重试不出来。
        return DistillResult(
            ok=False,
            error=("llm_truncated" if diag.get("reason") == "truncated"
                   else "llm_unavailable"),
            fewshot_used=len(samples), fewshot_degraded=degraded)
    obj = _parse(raw)
    if not obj or not isinstance(obj.get("topics"), list):
        return DistillResult(ok=False, error="llm_bad_json",
                             fewshot_used=len(samples), fewshot_degraded=degraded)

    # 串味检测的白名单:客户自己的资料 + 要打的词 —— 这些本来就该出现
    allow = kws + [brand_name or ctx.brand_name, city] + [
        ctx.company_intro, ctx.core_selling_points, ctx.unique_value,
        ctx.case_studies, ctx.credentials, ctx.testimonials,
        ctx.methodology, ctx.pricing_tiers, ctx.service_area, ctx.team_size,
    ]
    sample_texts = [str(s.get("caption") or "") for s in samples]

    topics: List[DistilledTopic] = []
    dropped = 0
    for item in obj["topics"]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        angle = str(item.get("angle") or "").strip()
        if not title or not angle:
            continue
        outline = [str(x).strip() for x in (item.get("card_outline") or []) if str(x).strip()][:5]
        comp = [str(x).strip() for x in (item.get("competitor_points") or []) if str(x).strip()][:3]
        blob = " ".join([title, angle] + outline + comp)
        # 两道串味检测:
        #  ① few-shot 原样抄袭(滑窗,确定性)
        #  ② 规范 §8.9 的**样张专属行业词**(深胡桃木/板材/坤沙…)混进非对应行业
        hits = detect_contamination(blob, sample_texts, allow=allow)
        hits += detect_sample_taint(blob, industry_key)
        if hits:
            # 🔴 丢掉而不是"标记一下照样返回":串味的那条一旦被用户点了做成图文,
            #    发出去就是给别人打广告。宁可少给一条。
            dropped += 1
            logger.warning("[douyin-distill] 选题串味被丢弃 brand=%s 片段=%s",
                           brand_id, hits[:3])
            continue
        topics.append(DistilledTopic(
            title=title[:60], angle=angle[:200], card_outline=outline,
            competitor_points=comp,
            keyword=str(item.get("keyword") or kws[0]).strip()[:40]))

    if not topics:
        return DistillResult(ok=False,
                             error="all_contaminated" if dropped else "llm_incomplete",
                             fewshot_used=len(samples), fewshot_degraded=degraded,
                             dropped_contaminated=dropped)

    return DistillResult(topics=topics, fewshot_used=len(samples),
                         fewshot_degraded=degraded,
                         kb_sources=list(ctx.sources_used),
                         dropped_contaminated=dropped)


async def resolve_quote_for_confirmed_keyword(confirmed_keyword_id: int, *,
                                              brand_id: int):
    """词身份 → 报价号。**校验归属**:词必须属于该客户、且报价 confirmed/paid。

    🔴 服务端派生而不是收客户端的 `quote_id`:收了就等于让前端决定
       这笔成品算谁的账。交付统计是对客户的承诺,不该由请求体说了算。

    🔴 状态闸与 `load_purchased_keywords` **同一份**(`confirmed`/`paid`)——
       两处口径漂开时,表现是「下单能选、规划里不算数」,而两边各自看都正常。

    找不到 / 不属于该客户 ⇒ 返 None,调用方按 4xx 拒(零冻结)。
    """
    import asyncio

    def _read():
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT q.id AS quote_id
                     FROM confirmed_keywords ck
                     JOIN quotes q ON q.id = ck.quote_id
                    WHERE ck.id = %s
                      AND q.brand_id = %s
                      AND q.status IN ('confirmed', 'paid')
                    LIMIT 1""",
                (int(confirmed_keyword_id), int(brand_id)),
            )
            row = cur.fetchone()
            return int(dict(row)["quote_id"]) if row else None
        finally:
            conn.close()

    try:
        return await asyncio.to_thread(_read)
    except Exception as e:  # noqa: BLE001
        logger.warning("[douyin] 词身份归属校验失败 ck=%s brand=%s: %s",
                       confirmed_keyword_id, brand_id, str(e)[:160])
        # 🔴 fail-closed:查不通就当不通过,不许"疑罪从无"地记一个报价号上去。
        return None
