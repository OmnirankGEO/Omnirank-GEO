"""GEO 抖音图文 · **内容规划**:客户确定词之后,该做几篇、每篇说什么

Owner 2026-08-03:「用户这边确定词以后,应该做出规划,需要多少篇图文,
然后这些图文说些什么」。

在这之前这一步是**缺的**:代理选完客户就直接填张数下单,
"这个词到底要做几条"全靠他自己拍 —— 而这个数系统本来就算得出来。

## 篇数从哪来:**复用既有的饱和曲线,不另造一个数**

`confirmed_keywords.required_articles` 是报价时就算好的:

    required_articles = ceil(target_share × competitor_count / (1 - target_share))
    —— tools/transparent_pricing.calculate_required_articles

它的含义是「要在这个词上占到 target_share 的答案位,总共需要多少条内容」。
🔴 **不是"多少篇文章"**,是"多少条内容" —— 门户文章和抖音图文
   都在同一个答案位里竞争(豆包两边都引),所以它们**共用这一个配额**。
   再为图文单独发明一个篇数公式,就会出现"文章说要 8 篇、图文说要 5 篇"
   这种两个口径打架的局面,而代理无从判断信哪个。

所以规划 = **配额 − 已产出 = 还差多少**,而不是重算一个新数字。

## 每篇说什么:交给蒸馏器,但**按缺口分配**

`topic_distiller.distill_topics` 已经能按客户知识库 + 同行业被采纳语料
产出选题。本模块只负责回答"要几条"以及"哪个词优先",
把结果喂给它 —— 不重复实现选题逻辑。

## 🔴 优先级排序的依据是实测,不是拍脑袋

行业采纳率跨行业差 2.5 倍(家装 41.0% vs 金融 16.5%,
见 RESEARCH_DOUYIN_EXPRESSION_BY_SEGMENT_2026-08-03.md),
但**同一个客户的词都在同一个行业里**,所以行业差异在客户内不构成排序依据。
客户内的排序只用两件确定的事:**缺口大的优先、已经做了的往后排**。
"哪个词更容易被引"这种判断我们没有词级实证,**不编**。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from tools.pricing_bands import normalize_article_capacity

logger = logging.getLogger("GEO-Douyin-Plan")

# 一条图文帖顶几条"内容"。
# 🔴 取 1,不是 0.5 也不是 2 —— 我们**没有**"一条图文帖抵几篇文章"的实证。
#    取 1 = 一条内容算一条,是唯一不需要额外证据的取法。
#
# 🔴 [WO_225-c1 §8.7] **这个数仍然是 1,而且是刻意的。**
#    工单原文写「POST_COUNTS_AS 改读换算表 douyin 桶」。照字面改的话
#    `done = 帖数 × 5`,读出来是「一条图文帖顶 5 条内容」;
#    而换算表说的恰好相反 —— **5 条图文帖填满 1 个交付槽**。
#    乘在 done 上等于把方向倒过来:7 槽的单做 2 条帖就会显示「做完了」,
#    而客户买的是 35 条。换算因此乘在 **quota** 那一侧(见 `_plan_rows`):
#      quota(槽)× k = quota(条);done 本来就是帖数 = 条数,不用换。
#    订正已在交付单点名,同 §4b 的 pending 一样属于「工单口径与实现方向不符」。
POST_COUNTS_AS = 1

# 一个词一次最多规划几条,防"一个词买了 40 篇"时一次性铺满。
PLAN_MAX_PER_KEYWORD = 9


@dataclass
class KeywordPlan:
    keyword: str
    quota: int              # [WO_225-c1 §8.7] 配额,单位**条**
                            #   = required_articles(槽)× 这单 douyin 桶的换算系数。
                            #   🔴 与容量合同的 authorized_articles(槽)不是同一个单位,
                            #      同一屏上不许两个都叫「篇」(A 侧 §3b 文案锁)。
    done: int               # 这个词已经产出的图文条数
    gap: int                # 还差多少
    suggested: int          # 这次建议做几条(gap 收进上限)
    # [#150 §3.2] 这个词的**身份**(confirmed_keywords.id)。
    #   前端下单时带回来,服务端据此把成品记到这一张报价的这一个词上。
    #   🔴 服务端**不能**按字符串反查是哪张报价 —— 多张报价同词正是本单要消除的歧义,
    #      按字符串猜等于把要修的病换个地方再犯一次。
    confirmed_keyword_id: Optional[int] = None
    quote_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {"keyword": self.keyword, "quota": self.quota, "done": self.done,
                "gap": self.gap, "suggested": self.suggested,
                "confirmed_keyword_id": self.confirmed_keyword_id,
                "quote_id": self.quote_id}


@dataclass
class ContentPlan:
    ok: bool = True
    reason: str = ""
    items: List[KeywordPlan] = field(default_factory=list)

    @property
    def total_gap(self) -> int:
        return sum(i.gap for i in self.items)

    @property
    def total_suggested(self) -> int:
        return sum(i.suggested for i in self.items)

    # [#150 · A 提的契约缺口] 顶栏「买了 N 条,已做 M 条,还差 K 条」三个数
    #   **全部**来自这里。原来只有 total_gap 是现成的,另两个前端只能自己 reduce ——
    #   那正好违反工单 §2.1「全部来自 /plan,前端不算数」,
    #   也与本单把算术收回服务端的主旨相反。纯派生,不动口径、不碰价。
    @property
    def total_quota(self) -> int:
        return sum(i.quota for i in self.items)

    @property
    def total_done(self) -> int:
        return sum(i.done for i in self.items)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason,
                "total_quota": self.total_quota,
                "total_done": self.total_done,
                "total_gap": self.total_gap,
                "total_suggested": self.total_suggested,
                "items": [i.to_dict() for i in self.items]}


def _plan_rows(quota_rows: List[dict], done_map: dict,
               bps_by_quote: Optional[dict] = None) -> List[KeywordPlan]:
    """纯计算,不碰 IO —— 这样它能被直接测,不需要数据库。

    [WO_225-c1 §8.7] `bps_by_quote`:{quote_id: 该单 douyin 桶的 bps}。
    quota 由**槽**换算成**条**;给 None 或查不到 ⇒ 10000 ⇒ 一槽一条 = 老行为。
    🔴 读表那一步留在 `build_content_plan`(IO 层),本函数仍然不碰库 ——
       否则这个能被直接测的纯函数就需要一个数据库才能跑。
    """
    from services.media_slot_conversion import (
        BUCKET_DOUYIN, ONE_SLOT_BPS, posts_for_slots)
    out: List[KeywordPlan] = []
    for row in quota_rows or []:
        kw = str(row.get("keyword") or "").strip()
        if not kw:
            continue
        # 🔴 [P0-5 · 轴级 2026-08-17] 原文是 `max(1, int(... or 1))` —— **两道**把 0 抬成 1:
        #    `or 1` 先把显式 0 当假值吃掉,`max(1, ...)` 再兜一次底。
        #    生产实测 `confirmed_keywords.required_articles = 0` 有 **223 行**(2026-08-17 只读),
        #    也就是说"客户没买这个词的内容量"在真实数据里是常态(覆盖词 is_core=False)。
        #    抬成 1 的后果不是显示错,是**给客户没买的词造出制作缺口** —— 下游据此发起制作、冻结算力。
        #    口径统一走篇数 SSOT:缺失=0(0..capacity 上限语义),显式 0 必须原样保留。
        quota_slots = normalize_article_capacity(row.get("required_articles"), when_missing=0)
        # [WO_225-c1 §8.7] 槽 -> 条。前端(ImageNoteTopicPanel)只渲染,不换算。
        _qid = row.get("quote_id")
        _bps = int((bps_by_quote or {}).get(int(_qid), ONE_SLOT_BPS)) if _qid else ONE_SLOT_BPS
        quota = posts_for_slots(quota_slots, BUCKET_DOUYIN, {BUCKET_DOUYIN: _bps})
        # [#150 §3.2] **优先按词身份归集**,回落字符串。
        #   字符串归集在"多张报价同词"时会互相顶替:A 报价做了 3 条,
        #   B 报价同一个词的进度跟着一起涨,而两边看各自都正常。
        #   历史行没有 confirmed_keyword_id(迁移不回填)⇒ 回落字符串,
        #   与今天逐字节同行为;新行两条路都数得到,但**只数一次**(见下)。
        #   两个桶**互斥**,所以相加不会重复计数:
        #     ("ck", id)     = 有词身份的成品(迁移之后新建的);
        #     ("legacy", kw) = **没有**词身份的历史成品(迁移不回填)。
        #   🔴 不能写成「按身份查不到就回落字符串」:某个词还没做过时
        #      ("ck", id) 取不到,回落会去数**另一张报价**同词的成品 ——
        #      污染原样回来,而屏幕上一切正常。
        #   ⚠️ 残留的不精确只剩历史行:一条 legacy 成品会算给**每一个**同词配额行。
        #      那是本函数原本就有的上界口径(注释已写明方向刻意偏"还差得多"),
        #      新数据不再有这个问题,随时间自然消退。
        ck_id = row.get("confirmed_keyword_id")
        by_id = int(done_map.get(("ck", int(ck_id)), 0)) if ck_id else 0
        legacy = int(done_map.get(("legacy", kw), 0))
        done = (by_id + legacy) * POST_COUNTS_AS
        gap = max(0, quota - done)
        out.append(KeywordPlan(
            keyword=kw, quota=quota, done=done, gap=gap,
            suggested=min(gap, PLAN_MAX_PER_KEYWORD),
            confirmed_keyword_id=(int(ck_id) if ck_id else None),
            quote_id=(int(row["quote_id"]) if row.get("quote_id") else None)))
    # 缺口大的排前面;缺口一样时按配额大的优先(配额大 = 这个词买得重)
    out.sort(key=lambda p: (-p.gap, -p.quota, p.keyword))
    return out


async def build_content_plan(brand_id: Optional[int]) -> ContentPlan:
    """这个客户的图文规划:每个买了的词还差几条。

    🔴 `done` 统计的是**这个词已经做出来的图文**,按 `geo_douyin_posts.keyword`
       归集。它不含门户文章 —— 严格说配额是两边共用的,所以这里的 gap 是
       **上界**(把文章也算进去缺口只会更小)。
       这个偏差方向是刻意的:宁可显示"还差得多"让人少做几条,
       也不要显示"够了"而实际没够 —— 后者会让客户买的量交付不足。
       ⚠️ 等文章侧也按词归集之后,这里要改成两边合并计数。**现在没有,如实标注。**
    """
    import asyncio

    from services.geo_douyin.topic_distiller import load_purchased_keywords

    if not brand_id:
        return ContentPlan(ok=False, reason="没有客户,无法规划")

    rows = await load_purchased_keywords(int(brand_id), limit=50)
    if not rows:
        return ContentPlan(ok=False,
                           reason="这个客户还没有确认的词，先在报价里选词")

    def _done_counts() -> dict:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            # [#150 §3.2] 一次查两个**互斥**的桶:有词身份的按身份数,
            #   没有词身份的(迁移前的历史行)按字符串数。
            #   同一条成品只会落进其中一个 ⇒ 上层相加不会重复计数。
            cur.execute(
                """SELECT confirmed_keyword_id, keyword, count(*) AS n
                     FROM geo_douyin_posts
                    WHERE brand_id = %s
                      AND deleted_at IS NULL
                      AND keyword IS NOT NULL
                      AND status IN ('ready', 'published', 'completing')
                    GROUP BY confirmed_keyword_id, keyword""",
                (int(brand_id),),
            )
            out: dict = {}
            for r in cur.fetchall():
                row = dict(r)
                n = int(row["n"])
                ck = row.get("confirmed_keyword_id")
                key = (("ck", int(ck)) if ck
                       else ("legacy", str(row["keyword"])))
                out[key] = out.get(key, 0) + n
            return out
        finally:
            conn.close()

    try:
        done_map = await asyncio.to_thread(_done_counts)
    except Exception as e:  # noqa: BLE001 - 数不出来就当 0,规划照给
        logger.warning("[douyin-plan] 已产出统计失败 brand=%s: %s",
                       brand_id, str(e)[:160])
        done_map = {}

    # [WO_225-c1 §8.7] 这批词涉及的每张报价,各自的 douyin 桶换算系数。
    #   🔴 逐单读,不是全局读一个:同一个客户可能有多张报价,
    #      而换算版本是**冻结在各自快照里**的(老单没快照 ⇒ 10000 ⇒ 老行为)。
    #   读不出来就空 dict ⇒ 全部回落一槽一条,规划照给(与 done_map 同一条降级纪律)。
    def _bps_map() -> dict:
        from services.media_slot_conversion import BUCKET_DOUYIN, resolve_for_quote
        ids = {int(r["quote_id"]) for r in rows if r.get("quote_id")}
        out: dict = {}
        for qid in ids:
            try:
                out[qid] = int(dict(resolve_for_quote(qid)["bps"]).get(BUCKET_DOUYIN) or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[douyin-plan] 换算系数读不到 quote=%s: %s", qid, str(exc)[:160])
        return {k: v for k, v in out.items() if v}

    try:
        bps_by_quote = await asyncio.to_thread(_bps_map)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[douyin-plan] 换算表整体读不到 brand=%s: %s", brand_id, str(exc)[:160])
        bps_by_quote = {}

    return ContentPlan(items=_plan_rows(rows, done_map, bps_by_quote))
