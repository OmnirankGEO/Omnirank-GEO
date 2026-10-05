"""C 端 GEO 方案 · 三档容量下发(P1 补完 · 2026-08-08)

【为什么有这个模块】
  `one_click_geo_plan` 产出的 `keyword_package` 只带三档**价格**,不带三档**篇数**。
  前端于是自己拿价格反推:`Math.round(price / 60)` —— 既写死了 ¥60/篇的成本口径
  (08_billing §3.3「前端不算钱」),又反推不准。生产真样本实测(3 个 done 任务 · 33 个核心词):

      32/33 个词反推值 ≠ 真实容量;合计 664 篇 vs 真实 416 篇 —— **多冻结 60%**。
      单词最大偏差:「生成式AI引擎优化公司推荐」¥5170 → 反推 86 篇,真实容量 31 篇。

  偏差不是四舍五入造成的,是公式本身就不成立:
      selling_price = required_articles × cost_per_article × markup × value_score × difficulty_score
  反推只除掉了 cost_per_article(还写死成 60),value_score / difficulty_score 全被算进篇数里。
  品牌词更直接:价格有 MIN_KEYWORD_PRICE=120 的地板,120/60=2 篇,而入门档真实容量是 1 篇。

【这个模块做什么】
  把**已经算好的**三档篇数从 `clusters` 搬到 `keyword_package` 上。
  只搬运,不计算 —— 数值来源是 batch_pricing 写回的 `kw[tier] = {"price": p, "articles": a}`,
  那是主合同阶梯(tools/pricing_bands)算出来的同一个数。本模块**没有**第二套篇数逻辑。

【为什么要能在读取时补】
  存量 `geo_plan_tasks.result_json` 里的 `keyword_package` 没有这三个字段(实测 3/3 都没有),
  但同一份 result 的 `clusters` 里**有**(实测 7/7 都带 `{price, articles}`)。
  所以老方案不需要重算、也不许重算 —— 原地补齐即可,这也是「历史快照不重算」的做法。

红线:只读 result 结构、只补篇数字段;不碰任何价格字段,不写库。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from tools.pricing_bands import normalize_article_capacity

# 与 tools/batch_pricing._enrich_keywords_with_tier_prices 的 tiers_config 同名同序。
# strong 是内部托管第 4 档(前台 3 套餐页不渲染),这里一并补,免得下游再各写一份。
CEND_PLAN_TIERS = ("entry", "standard", "flagship", "strong")

# 词在 clusters 里查不到(超红海/爆价词被跳过写回、或老路径 estimate_user_cost 追加的词)
# → 容量按 0 下发。0 是合法容量(P1 语义:0..capacity),不是「缺失兜底成 1」。
CAPACITY_WHEN_NOT_QUOTED = 0


def _tier_field(tier: str) -> str:
    return f"{tier}_articles"


def _capacity_from_cluster_keyword(kw_obj: Mapping[str, Any], tier: str) -> Optional[int]:
    """从 clusters 里的词对象取某一档容量。取不到返 None(交给调用方决定兜底)。

    两种承载形态都认(都是 batch_pricing 自己写的):
      · 嵌套 `kw[tier] = {"price": p, "articles": a}`   ← _compute_cluster_pricing 写回
      · 扁平 `kw[f"{tier}_articles"]`                    ← _enrich_keywords_with_tier_prices 写的
    """
    nested = kw_obj.get(tier)
    if isinstance(nested, Mapping):
        # 档位对象在 = 这一档报过价。里面没写 articles 就是「这一档 0 篇容量」,
        # 不是「查不到」—— 两者对前端的含义不同(0 篇 vs 回落老路径),别混。
        return normalize_article_capacity(nested.get("articles"), when_missing=CAPACITY_WHEN_NOT_QUOTED)
    flat = _tier_field(tier)
    if flat in kw_obj:
        return normalize_article_capacity(kw_obj.get(flat), when_missing=CAPACITY_WHEN_NOT_QUOTED)
    return None


def index_cluster_keywords(clusters: Optional[Iterable[Any]]) -> dict[str, dict]:
    """keyword → clusters 里的词对象。核心词与附赠词同一个索引(两边都带三档容量)。

    同名词重复出现时**先到先得**:clusters 内同一个词不应出现两次,
    真出现了也只可能是同一份定价的副本,取哪个都一样。
    """
    index: dict[str, dict] = {}
    for cluster in clusters or []:
        if not isinstance(cluster, Mapping):
            continue
        for bucket in ("core_keywords", "covered_keywords"):
            for kw_obj in cluster.get(bucket) or []:
                if not isinstance(kw_obj, Mapping):
                    continue
                name = str(kw_obj.get("keyword") or "").strip()
                if name and name not in index:
                    index[name] = dict(kw_obj)
    return index


def attach_tier_article_capacity(result: Any) -> Any:
    """给 `result["keyword_package"]` 补 entry/standard/flagship/strong `_articles`。

    **幂等**:字段已经在了就原样保留(新方案生成时补一次,老方案读取时补一次,
    同一份 result 走两遍结果相同)。就地修改并返回同一个对象,方便链式调用。

    查不到定价的词(超红海 / 爆价 / 老路径追加词)不写字段 —— 让前端回落到
    `strategy.articles_needed ?? 0`,保持老路径行为不变。
    """
    if not isinstance(result, dict):
        return result
    package = result.get("keyword_package")
    if not isinstance(package, list) or not package:
        return result

    index = index_cluster_keywords(result.get("clusters"))

    for item in package:
        if not isinstance(item, dict):
            continue
        kw_obj = index.get(str(item.get("keyword") or "").strip())
        for tier in CEND_PLAN_TIERS:
            field = _tier_field(tier)
            if field in item:
                # 幂等:已下发过就不覆盖(也防止读取侧把生成侧的值改掉)
                continue
            if kw_obj is None:
                continue
            capacity = _capacity_from_cluster_keyword(kw_obj, tier)
            if capacity is None:
                continue
            item[field] = capacity

    return result
