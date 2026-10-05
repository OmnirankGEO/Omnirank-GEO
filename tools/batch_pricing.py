"""
销售批量报价系统 V2 - 三维交叉验证定价

核心逻辑：
1. 5118 数据：搜索量、SEM出价、竞价公司数（稳定信号）
2. 秘塔搜索：内容密度、竞品数（一次快照，可缓存）
3. LLM 判断：搜索意图、漏斗阶段（确定性推理）

定价公式：
  required_articles = ceil(target_share x competition_count / (1 - target_share))
  selling_price = required_articles x cost_per_article x markup x value_score x difficulty_score

  difficulty_score (1.0-2.0) = f(5118竞价公司数, 秘塔竞品数, 5118搜索量)
  value_score (0.8-2.0) = f(5118 SEM出价, LLM意图分类, 搜索概率, 关键词模式)
"""
import sys
sys.path.insert(0, 'geo_agentscope')

import asyncio
import math
from datetime import datetime
from typing import Optional, Callable, Any
from tools.transparent_pricing import get_cost_per_article, get_cost_config


# [M4 SSOT 2026-06-07] 单个关键词最低售价 import 自 keyword_value_scorer(全仓单一权威源 = ¥400·含品牌词)
from tools.keyword_value_scorer import MIN_KEYWORD_PRICE, MIN_KEYWORD_PRICE_NATIONAL

# [P1 容量合同 2026-08-08] 篇数唯一取数出口(全仓不许再出现第二套阶梯/第二个 `or 1`)
from tools.pricing_bands import (
    LEGACY_MISSING_CAPACITY_DEFAULT,
    normalize_article_capacity as _normalize_capacity_ssot,
)


def _normalize_article_capacity(raw) -> int:
    """本模块口径:缺失 → 上线前同值(1 篇)· 显式 0 保留(覆盖词)· 负数 → 0。"""
    return _normalize_capacity_ssot(raw, when_missing=LEGACY_MISSING_CAPACITY_DEFAULT)


# ========================================
# [CTO-15.23 2026-05-11 P0] 品牌名/industry/brand_name 归一化 helpers
# 老板报"QZQZ美学定制 1745 元 vs 深圳装修公司哪家靠谱 524 元" 3.3x 飙升根治
# ========================================

def _normalize_brand_name(name: str) -> str:
    """品牌名归一化:去空格 + 转小写 + 去前后空白
    防 "QZQZ美学定制" vs "QZQZ 美学定制" 当 2 个不同 brand 缓存 miss 风暴
    """
    if not name:
        return ""
    import re
    return re.sub(r"\s+", "", str(name).strip()).lower()


def _is_brand_keyword(keyword: str, brand_name: str) -> bool:
    """检测 keyword 是否包含 brand_name 子串(归一化后比较)
    品牌名作 keyword 不应走全套定价 · LLM intent 分类不稳 + is_broad 全国词溢价 → 价格爆涨
    """
    if not keyword or not brand_name:
        return False
    norm_kw = _normalize_brand_name(keyword)
    norm_brand = _normalize_brand_name(brand_name)
    if not norm_brand or len(norm_brand) < 2:
        return False
    # brand_name 完整出现在 keyword 中
    return norm_brand in norm_kw


def _lock_brand_industry(brand_name: str, fallback_industry: str = "") -> str:
    """[Bug C] 报价时强制锁定 brands.industry · 不允许 LLM 每次重判
    防 QZQZ 一会"全屋定制" 一会"建筑建材" → 缓存 key 漂移 → 价格不稳
    """
    if not brand_name or brand_name == "客户":
        return fallback_industry
    try:
        from db.diagnosis_db import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT industry FROM brands WHERE name = %s LIMIT 1", (brand_name,))
            row = cur.fetchone()
            if row and row.get("industry"):
                return row["industry"]
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        pass
    return fallback_industry


def _make_brand_keyword_price(keyword: str, markup: float = 1.0, skip_markup: bool = False) -> dict:
    """[Bug A] 品牌名 keyword 走固定低价路径 · 不走 5118/秘塔/LLM 定价
    返回与正常 scored keyword 兼容的结构

    [P1 容量合同 2026-08-08] 原本写死的 1/2/3 篇阶梯收编进 pricing_bands 的
      ARTICLE_LADDER_BRAND_KEYWORD 档案 —— **数值一字未改 = 零价格变化**,
      只是把第二份字面量并回 SSOT。品牌词是否豁免主合同 5/7/10 阶梯 ⏳ 待 Owner 拍板。
    """
    from tools.pricing_bands import ARTICLE_LADDER_BRAND_KEYWORD, tier_article_capacity

    def _cap(tier: str) -> int:
        return tier_article_capacity(tier, ladder=ARTICLE_LADDER_BRAND_KEYWORD)

    entry_a, std_a, flag_a = _cap("entry"), _cap("standard"), _cap("flagship")
    base_unit = MIN_KEYWORD_PRICE  # 120
    effective_markup = 1.0 if skip_markup else markup
    entry_p = max(int(entry_a * 60 * effective_markup), base_unit)
    std_p = max(int(std_a * 60 * effective_markup), base_unit)
    flag_p = max(int(flag_a * 60 * effective_markup), base_unit)
    return {
        "keyword": keyword,
        # 品牌词的可交付容量上限 = 入门档容量(与 entry_articles 同源,不再各写一份)
        "required_articles": entry_a,
        "selling_price": entry_p,
        "cost_per_article": 60.0,
        "total_cost": 60.0,
        "markup_ratio": effective_markup,
        "difficulty_score": 1.0,
        "value_score": 1.0,
        "is_broad": False,
        "geo_multiplier": 1.0,
        "search_volume": 0,
        "sem_price": 0,
        "bidword_company_count": 0,
        "competitor_count": 1,
        "effective_competition": 1,
        "intent": "navigational",
        "funnel_stage": "decision",
        "search_probability": 0.1,
        "entry_price": entry_p,
        "entry_articles": entry_a,
        "standard_price": std_p,
        "standard_articles": std_a,
        "flagship_price": flag_p,
        "flagship_articles": flag_a,
        "is_brand_keyword": True,
        "skip_pricing_reason": "品牌名不参与定价 · 固定低价(CTO-15.23 P0)",
    }


# ========================================
# 批量配置
# ========================================

def get_batch_config() -> dict:
    """从系统设置获取批量配置"""
    try:
        from config.settings_manager import get_current_settings
        settings = get_current_settings()
        return {
            "concurrency": settings.quote_batch_concurrency,
        }
    except Exception:
        return {
            "concurrency": 30,  # [CTO-15.5 2026-04-20 Q1.C] fallback 10→30 · 对齐 settings.quote_batch_concurrency 新默认
        }


BATCH_CONFIG = get_batch_config()


# ========================================
# 批量定价（基于三维评分结果）
# ========================================

def generate_batch_summary(
    scored_keywords: list[dict],
    target_share: float
) -> dict:
    """生成批量报价汇总"""
    from tools.transparent_pricing import calculate_exposure_probability, get_cost_config

    # [§4.2] 超红海词不出保证价 → 不进套餐汇总(总价/篇数/成本/曝光率均排除 · markdown 报价单同口径)
    # [完整修复 2026-06-13 High1] 爆价/放飞词(guarantee_unavailable)同口径不进套餐汇总(参考价人工核·不作保证)
    _quotable = [p for p in scored_keywords
                 if not (p.get("super_red_ocean") or p.get("guarantee_unavailable"))]

    # [模块3② 2026-06-07 flat 死代码防御兜底] flat 路径自 2026-03-28 停用(quote_cluster_mode 默认 True·新报价 100% 走
    #   cluster·且 cluster 失败不 fallback 到 flat·见 selection_api:1395 无 try/except)。仅 admin 翻 quote_cluster_mode=False
    #   才到此。逐词求和对同义词包会爆价(璧山历史 ¥77791)→ 加【防御性同形去重】零风险兜底:按归一化词形(去空格/标点/
    #   大小写·中文内容保留)去重,同形保留 selling_price 最高(与锚词=售价最高口径一致),防 flat 万一被启用逐个同形词累加爆价。
    #   ⚠️ 仅去【同形/格式变体】重复;真【语义同义】去重需 cluster 模式(flat 无聚类·正道是 cluster)。
    if _quotable:
        import re as _re
        _best_by_norm = {}
        _no_norm = []
        for _p in _quotable:
            _norm = _re.sub(r'[\s\W_]+', '', str(_p.get('keyword', '')).lower())
            if not _norm:
                _no_norm.append(_p)
                continue
            _ex = _best_by_norm.get(_norm)
            if _ex is None or int(_p.get('selling_price', 0) or 0) > int(_ex.get('selling_price', 0) or 0):
                _best_by_norm[_norm] = _p
        _deduped = list(_best_by_norm.values()) + _no_norm
        if len(_deduped) < len(_quotable):
            print(f"[flat 防御] flat 已停用·同形去重 {len(_quotable)}→{len(_deduped)} 词(正道用 cluster 模式)")
            _quotable = _deduped

    total_articles = sum(p["required_articles"] for p in _quotable)
    total_cost = sum(p["total_cost"] for p in _quotable)
    total_price = sum(p["selling_price"] for p in _quotable)

    keyword_count = len(scored_keywords)
    final_price = total_price

    # 计算真实平均曝光概率（基于超几何分布/泊松近似）
    config = get_cost_config()
    ai_ref = config["ai_reference_count"]
    probs = []
    for p in _quotable:
        comp = max(p.get("effective_competition", p.get("competitor_count", 1)), 1)
        articles = p["required_articles"]
        prob = calculate_exposure_probability(comp, articles, ai_ref)
        probs.append(prob)
    avg_prob = round((sum(probs) / len(probs)) * 100) if probs else 0

    return {
        "keyword_count": keyword_count,
        "total_articles": total_articles,
        "total_cost": total_cost,
        "total_price_before_discount": total_price,
        "batch_discount": 0,
        "batch_discount_amount": 0,
        "final_price": final_price,
        "avg_price_per_keyword": int(final_price / keyword_count) if keyword_count > 0 else 0,
        "target_share": target_share,
        "avg_exposure_probability": avg_prob,
    }


def recalculate_for_tier(
    scored_keywords: list[dict],
    target_share: float,
    markup_override: float | None = None,
) -> list[dict]:
    """
    基于已有的三维评分数据，按不同套餐重新计算价格

    不重新查询任何API，只用已有的 competitor_count 和 value_score 重算

    Args:
        scored_keywords: 评分数据
        target_share: 目标占比（15% / 25% / 33%）
        markup_override: 若传入则替代 config["markup_ratio"]（C 端传 1.0 = 透明成本不溢价）
    """
    # [v2.1 2026-06-11 · 成本驱动接线(老板拍商业逻辑)] 与 score_keywords 共用 SSOT 口径:
    #   v2 词(pricing_formula_version v2.x):出厂三档已存(entry/standard/flagship_price · markup=1)
    #     → 按档直接索引 × markup 末位叠乘(C 端 markup_override=1.0 拿到的就是出厂成本价)
    #     → strong 档(SOV 0.50 内部霸榜)不预存 · 用 compute_v2_tier_price(true_competition, cost)现算
    #   legacy 词(旧缓存无 v2 数据):回落老 compute_banded_price 公式(不破老报价)
    from tools.pricing_bands import (
        compute_banded_price, compute_required_articles_banded, tier_key_from_target_share,
        compute_v2_tier_price,
    )

    config = get_cost_config()
    default_cost = get_cost_per_article()
    markup_ratio = markup_override if markup_override is not None else config["markup_ratio"]
    tier_key = tier_key_from_target_share(target_share)

    results = []
    for kw in scored_keywords:
        eff_comp = max(kw.get("effective_competition", kw.get("competitor_count", 1)), 1)
        cost_per_article = kw.get("cost_per_article", default_cost)
        formula_ver = str(kw.get("pricing_formula_version") or "")
        v2_data = kw.get("v2_assessor_data") or {}

        # ===== v2 路径:SSOT 公式现算(LLM 评估师底盘真正接进客户价)=====
        #   底盘 = v2_assessor_data.true_competition + kw.cost_per_article(不可变 · enrich 覆写不到)
        #   一律现算而非读 {tier}_price 字段:_enrich_keywords_with_tier_prices 会把这些字段
        #   覆写成客户层(× markup),再读会双重溢价。纯函数现算幂等 · 任何调用顺序不漂价。
        if formula_ver.startswith("v2."):
            from tools.pricing_bands import value_multiplier_from_signal
            # [v2.2 hardening② 2026-06-13] true_competition 防 NaN/脏值:int(float('nan')) 会 raise 中断 recalc
            #   (v2.2 自身只写钳过的 int · 仅 DB 篡改/非 v2 写入可注入 · 防御纵深)
            _tc = v2_data.get("true_competition")
            try:
                _tcf = float(_tc) if _tc is not None else None
                true_comp = int(_tcf) if (_tcf is not None and math.isfinite(_tcf)) else int(eff_comp)
            except (TypeError, ValueError):
                true_comp = int(eff_comp)
            # [v2.2 P1 修] 价值乘数优先读底盘持久化的【生效值】(assessor 解析结果 SSOT):
            #   fallback 词 signal=1.0 但 vm=1.0 · 从 signal 反推会得 1.25 = 真实出价静默 +25%(Workflow 坐实)
            #   缺失(v2.1 旧底盘 / 老 draft)才退 signal 反推 · 再缺失 → 0 → ×1.0 不溢价 fail-safe
            _vm_persisted = v2_data.get("value_multiplier")
            vm = float(_vm_persisted) if _vm_persisted is not None                 else value_multiplier_from_signal(v2_data.get("value_signal", 0))
            t = compute_v2_tier_price(true_comp, cost_per_article, tier_key, markup_ratio,
                                      value_multiplier=vm)
            results.append({
                **kw,
                "required_articles": t["articles"],
                "total_cost": round(t["articles"] * cost_per_article, 1),
                "selling_price": t["selling_price"],
                "raw_price_before_band": t["factory_price"],
                "needs_review": kw.get("needs_review", False),
            })
            continue

        # ===== legacy 路径(旧缓存词 · 老公式不动)=====
        value_score = kw.get("value_score", 1.0)
        difficulty_score = kw.get("difficulty_score", 1.0)
        is_broad = kw.get("is_broad", False)
        data_source = kw.get("data_source", "5118")
        keyword_type = kw.get("keyword_type") or ("national_niche" if is_broad else "local_city")

        required_articles = compute_required_articles_banded(eff_comp, target_share, data_source,
                                                             keyword_type=keyword_type)
        base_cost = required_articles * cost_per_article
        banded = compute_banded_price(
            required_articles=required_articles,
            cost_per_article=cost_per_article,
            markup_ratio=markup_ratio,
            value_score=value_score,
            difficulty_score=difficulty_score,
            keyword_type=keyword_type,
            tier_key=tier_key,
            effective_competition=eff_comp,
        )

        results.append({
            **kw,
            "required_articles": required_articles,
            "total_cost": round(base_cost, 1),
            "selling_price": banded["selling_price"],
            "raw_price_before_band": banded["raw_price_before_band"],
            "band_min": banded["band_min"],
            "band_max": banded["band_max"],
            "needs_review": kw.get("needs_review", False) or banded["needs_review"],
        })

    return results


# ========================================
# 完整批量报价流程
# ========================================

MUNICIPALITIES = {"北京", "上海", "天津", "重庆"}

# 直辖市区名映射（用于自动补前缀）
_MUNICIPALITY_DISTRICTS = {
    "上海": [
        "浦东新区", "黄浦区", "静安区", "徐汇区", "长宁区", "虹口区",
        "杨浦区", "普陀区", "闵行区", "宝山区", "嘉定区", "松江区",
        "金山区", "青浦区", "奉贤区", "崇明区",
        "浦东", "黄浦", "静安", "徐汇", "长宁", "虹口",
        "杨浦", "普陀", "闵行", "宝山", "嘉定", "松江",
    ],
    "北京": [
        "朝阳区", "海淀区", "东城区", "西城区", "丰台区", "石景山区",
        "通州区", "顺义区", "大兴区", "昌平区", "房山区", "门头沟区",
        "朝阳", "海淀", "东城", "西城", "丰台", "通州", "顺义", "大兴", "昌平",
    ],
    "天津": [
        "和平区", "河西区", "南开区", "河东区", "河北区", "红桥区",
        "滨海新区", "东丽区", "西青区", "津南区", "北辰区", "武清区",
        "和平", "河西", "南开", "滨海新区", "武清",
    ],
    "重庆": [
        "渝中区", "江北区", "南岸区", "沙坪坝区", "九龙坡区", "大渡口区",
        "渝北区", "巴南区", "北碚区", "璧山区", "万州区", "涪陵区",
        "渝中", "江北", "南岸", "沙坪坝", "九龙坡", "渝北",
    ],
}


def normalize_keywords_for_municipality(keywords: list[str], city: str) -> list[str]:
    """
    直辖市关键词前缀修正：自动给含区名但没有城市名的关键词补上城市名前缀。

    例如 city="上海":
      "浦东新区高端商务车配司机哪家好" → "上海浦东新区高端商务车配司机哪家好"
      "上海黄浦区劳斯莱斯租赁推荐"     → 不变（已有"上海"）
      "商务司机服务哪个平台靠谱"       → 不变（没有区名，属于通用词）
    """
    if city not in MUNICIPALITIES:
        return keywords

    districts = _MUNICIPALITY_DISTRICTS.get(city, [])
    if not districts:
        return keywords

    result = []
    fixed_count = 0
    for kw in keywords:
        if city not in kw:
            # 检查是否包含区名
            has_district = any(d in kw for d in districts)
            if has_district:
                # 找到第一个匹配的区名，在它前面插入城市名
                for d in districts:
                    if d in kw:
                        kw = kw.replace(d, f"{city}{d}", 1)
                        fixed_count += 1
                        break
        result.append(kw)

    if fixed_count > 0:
        print(f"  [直辖市修正] 为 {fixed_count} 个关键词补上「{city}」前缀")

    return result


def _enrich_keywords_with_tier_prices(
    scored: list[dict],
    skip_markup: bool = False,
    markup_override: float | None = None,
) -> list[dict]:
    """
    为每个关键词计算 4 个套餐的单价和所需文章数

    在 scored 列表的每个 dict 中添加:
        entry_price, entry_articles,
        standard_price, standard_articles,
        flagship_price, flagship_articles,
        strong_price, strong_articles   # v1_3 (CTO-15.1 2026-04-19) 对齐托管 SOV_TIERS 4 档

    Args:
        scored: 评分数据
        skip_markup: True 时 markup 用 1.0（C 端透明成本，不走代理端 2.0 溢价）
        markup_override: 代理私有报价系数；skip_markup=True 时忽略
    """
    # [前台3套餐红线 · 2026-06-05] 前台/客户报价页只有 3 套餐(入门/标准/旗舰)·见 selection_api.TIER_CONFIG
    #   + frontend SelectionPage.TIER_META(3 档)+ TierSelector(只渲染 3 档)。
    #   strong(霸榜)= 【内部全自动托管 SOV_TIERS 第 4 档】·非前台套餐 → 这里算出 strong_price 仅供
    #   托管/老C端(旧 C 端 GEO 方案卡 已废弃)消费;前台 3 套餐页不渲染 strong(TIER_META 无 strong)。
    #   grep gate:strong 不得进 selection_api 的 TIER_CONFIG / 前台 TierSelector。
    tiers_config = {
        "entry":    0.10,  # "偶尔被推荐" 入门试水
        "standard": 0.20,  # "经常被推荐" 标准上榜（AI 推荐）
        "flagship": 0.30,  # "优先推荐" 旗舰抢位
        "strong":   0.50,  # 内部托管 SOV 第4档(霸榜)· 非前台套餐 · 前台不渲染
    }

    effective_markup_override = 1.0 if skip_markup else markup_override

    for tier_key, share in tiers_config.items():
        tier_scored = recalculate_for_tier(scored, share, markup_override=effective_markup_override)
        for orig, tier_kw in zip(scored, tier_scored):
            orig[f"{tier_key}_price"] = tier_kw["selling_price"]
            orig[f"{tier_key}_articles"] = tier_kw["required_articles"]
            if tier_key == "standard":
                # Keep the flat summary and markdown aligned with the visible standard tier.
                orig["selling_price"] = tier_kw["selling_price"]
                orig["required_articles"] = tier_kw["required_articles"]
                orig["total_cost"] = tier_kw["total_cost"]
                if effective_markup_override is not None:
                    orig["markup_ratio"] = effective_markup_override

    # 【v1.3 §4.2】超红海词:三档价保留作内部参考(供服务商/admin 单独报价)· 标 guarantee_quotable=False
    #   不出保证价由汇总层(_compute_cluster_pricing + selection_api 套餐总价)显式跳过实现 · 前端按标渲染「需单独报价」
    # [完整修复 2026-06-13 High1] 爆价/放飞词(guarantee_unavailable)同口径:三档价保留作内部参考(供代理人工核起价)·
    #   标 guarantee_quotable=False · 客户端套餐总价显式跳过(参考价人工核·不作保证)
    for orig in scored:
        if orig.get("super_red_ocean") or orig.get("guarantee_unavailable"):
            orig["guarantee_quotable"] = False

    return scored


_UNAVAILABLE_REASON_CN = {
    # 王姐口径人话 · 不出现 API/LLM/秘塔 等工程词
    "llm_unavailable": "网络繁忙,智能评估服务暂时不可用,请稍后重试",
    "metaso_unavailable": "网络繁忙,竞争数据暂时获取不到,请稍后重试",
}


def _split_price_unavailable(scored: list) -> tuple[list, list]:
    """[2026-06-11 老板拍 · 真实第一] 数据断供词(双 LLM 全挂/metaso 全挂)从报价剥离:
    兜底价一旦进报价单就是对外商业承诺(与真实价可差 2-4 倍)→ 扯皮源。
    断供词不出价、不进聚类、不写 7 天缓存(API 恢复后下次报价自然全链路重算),
    以 unavailable_keywords(人话 reason)返给前端提示重试。
    返回 (可出价行, 断供行)。"""
    available, unavailable = [], []
    for s in scored:
        (unavailable if s.get("price_unavailable") else available).append(s)
    if unavailable:
        print(f"  [真实第一] {len(unavailable)} 个词数据断供不出价(不写缓存 · 提示稍后重试):"
              f" {[s['keyword'] for s in unavailable][:5]}")
    return available, unavailable


def _unavailable_payload(unavailable_rows: list) -> list[dict]:
    return [{
        "keyword": s["keyword"],
        "reason": _UNAVAILABLE_REASON_CN.get(s.get("unavailable_reason"),
                                             "网络繁忙,暂时无法估价,请稍后重试"),
    } for s in unavailable_rows]


def _cacheable_rows(scored_new: list) -> list:
    """[完整修复 2026-06-13 High2] 爆价/放飞词不写全局共享缓存,防一次性极端价污染
    (industry,city,keyword) 7 天共享锁(其他代理/客户会命中这条被污染的价):
      · blowup_no_cache       → 单词 selling 过高(护栏③);
      · guarantee_unavailable → 出厂超绝对天花板 / P0-C 全国放飞灰度(护栏②④)· 已是参考价人工核;
      · cost_snapshot_uncacheable → [收口 2026-06-15 Codex 返修] P0-A 成本非 db active
          (bootstrap 静默回落 / 快照无该档数据回落)· 随码占位/回落成本绝不污染跨代理 7 天锁。
    这些词【仍出现在本次报价】(参考价),只是不落共享缓存。
    三 flag 全关时三字段恒 False(compute_blowup_guards inert / P0-A 关不读快照)→ 返回全集 = 0 行为变化。
    注:不扩成"所有 needs_review 不缓存"(那会改 v2.2 既有行为·降命中率)· 只针对 P0-A 占位/回落成本。"""
    out = [s for s in scored_new
           if not (s.get("blowup_no_cache") or s.get("guarantee_unavailable")
                   or s.get("cost_snapshot_uncacheable"))]
    _dropped = len(scored_new) - len(out)
    if _dropped:
        print(f"  [爆价护栏] {_dropped} 个爆价/放飞词不写共享缓存(本次报价仍含 · 参考价人工核)")
    return out


def _filter_llm_should_quote_false(keywords: list, brand_name: str, business_scope, llm_active: bool) -> list:
    """[模块5 2026-06-07 + 二审隔离 2026-06-13 老板拍] 老公式 fallback carry should_quote_false。

    复用 keyword_price_cache_llm 历史 LLM 判定,把 should_quote=false 信息型词从报价剔除(与 LLM-first 主路径口径一致)。
    **隔离铁律**:仅当 `llm_active`(本次尝试过 LLM-first · 即 LLM-first 失败 fallback 到老公式)才读 LLM cache ——
      还原 模块5 原设计意图("即便本次 LLM-first 失败落到老公式")。
      `LLM_FIRST_PRICING_ENABLED` 全局 false + 白名单空 → `llm_active=False` → **v2.2 公式路径完全不读
      keyword_price_cache_llm**(防旧 should_quote=false 行静默剔词污染 v2.2 报价 · 老板二审要求)。
    best-effort:无 LLM 缓存判定的词照常算(不误剔);若全部词都 should_quote=false 则不剔(避免空报价崩)。
    返回过滤后 keywords(`llm_active=False` 时原样返回)。"""
    if not llm_active:
        return keywords
    try:
        from db.diagnosis_db import get_llm_cached_keyword_prices
        _llm_judged = get_llm_cached_keyword_prices(brand_name, keywords, business_scope=business_scope)
        _info_excluded = [k for k in keywords if _llm_judged.get(k, {}).get("should_quote", True) is False]
        if _info_excluded and len(_info_excluded) < len(keywords):
            _excl_set = set(_info_excluded)
            print(f"  [模块5 信息词剔除] 老公式 fallback 剔除 {len(_info_excluded)} 个 LLM 判定 should_quote=false 信息型词: {_info_excluded[:3]}")
            return [k for k in keywords if k not in _excl_set]
        elif _info_excluded:
            print(f"  [模块5] 全部 {len(keywords)} 词均 should_quote=false · 为避免空报价不剔除(老公式按低权重计)")
    except Exception as _m5e:
        print(f"  [模块5] should_quote 缓存查询失败(降级不剔除): {_m5e}")
    return keywords


# ============================================================================
# [报价意图闸 2026-08-04 · WO-QUOTE-INTENT-GATE] 不会给客户带来推荐的词不进报价
# ----------------------------------------------------------------------------
# 判定与展示逻辑的唯一实现在 services/quote_intent_gate.py(定价引擎与报价 API 共用一份,
# 防"同一件事写两遍"—— cluster 报价路径至今没接 should_quote 闸就是那么来的)。
# 此处只做**薄委托**,不复制逻辑。
# ============================================================================
from services.quote_intent_gate import (  # noqa: E402
    partition_by_commercial_policy as _partition_by_commercial_policy,
    policy_excluded_markdown as _policy_excluded_markdown,
)

# ============================================================================
# [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 逐词锁期事实的产出口
# ----------------------------------------------------------------------------
# 定价引擎知道「哪些词这次真写进了共享缓存 / 哪些词命中了未过期缓存行」,
# 但这个事实原来烂在函数里没传出去,报价渲染层只好无条件 now()+7 —— 假承诺就是这么来的。
# 这里只把【已有的结果】搬到 quote_data[LOCK_MAP_KEY],**不改任何写缓存判据**。
# ============================================================================
from services.price_lock_promise import (  # noqa: E402
    LOCK_MAP_KEY as _LOCK_MAP_KEY,
    record_locked as _record_locked,
)


def _should_write_shared_cache(markup_override, cost_per_article_override, allow_cache_write,
                               cost_multiplier: float = 1.0, trust_price_active: bool = False) -> bool:
    """[价格锁回归修 2026-06-10 audit P1] 是否把新词底盘写入【全局共享】7 天价格锁缓存(keyword_price_cache)。
    解耦「算价用实际系数」与「写缓存资格」:
      · allow_cache_write is None(旧调用方未传)→ 退回旧判据 markup_override is None(向后兼容);
      · 显式 bool → 仅默认口径(True)写;
      · 无论如何 cost_per_article_override 非 None(自设单篇成本)→ 不写(自设成本是底盘字段,会污染共享 cost);
      · [v2.1 2026-06-11] 进货倍率 ≠1(扫码下级)→ 不写(底盘 cost 含上级 SKU 系数,同样污染共享 cost)。
      · [P0-D 2026-06-14 · Codex#1 返修] trust_price_active(品牌有 collected/stale 信任快照·可能调篇数)
        → 不写:共享缓存 key 是 (industry,city,keyword) 不含 brand · P0-D 价是 per-brand · 写了会把 A 客户
        难度价污染给 B 客户。"""
    if cost_per_article_override is not None:
        return False
    if abs(float(cost_multiplier or 1.0) - 1.0) > 0.001:
        return False
    if trust_price_active:
        return False
    if allow_cache_write is None:
        return markup_override is None
    return bool(allow_cache_write)


def _fetch_trust_asset_for_pricing(brand_id, industry=None):
    """[P0-D 2026-06-14 · Codex#1 返修] 报价链路取品牌信任资产(归一化)· 严格按 brand_id 取,绝不按 brand_name 猜。
      · flag 关 → None(报价 0 变化);
      · flag 开 + 无 brand_id → missing 哨兵(降级·不抬价·只内部标·不按名猜串客户);
      · flag 开 + 有 brand_id → latest active 快照归一化(无快照→missing)。
    [B4-1] flag 开时,额外附带飞轮"行业引用格局"(industry_citation_landscape)· 独立字段 ·
      供 _assemble_result 保守消费(±10% 篇数侧);无 industry / 无数据 → 不附带。
    任何异常 fail-soft → None。"""
    try:
        from tools.llm_pricing_flag import is_trust_asset_enabled
        if not is_trust_asset_enabled():
            return None
        from db.trust_asset_db import get_active_trust_snapshot, normalize_trust_asset_for_pricing
        from config.pricing_config import get_trust_asset_config
        stale_days = get_trust_asset_config()["stale_days"]
        if not brand_id:
            result = normalize_trust_asset_for_pricing(None, stale_days=stale_days)  # 拿不到 brand_id → missing
        else:
            row = get_active_trust_snapshot(brand_id=brand_id)
            result = normalize_trust_asset_for_pricing(row, stale_days=stale_days)
        if result is not None and industry:
            try:
                from services.source_authority_analyzer import get_industry_citation_landscape
                landscape = get_industry_citation_landscape(industry)
                if landscape:
                    result = dict(result)
                    result["industry_citation_landscape"] = landscape
            except Exception:
                pass
        return result
    except Exception:
        return None


def _trust_price_active(trust_asset) -> bool:
    """信任资产是否可能影响价格(collected/stale = 有真实数据会调篇数;missing/None = 不动价)。"""
    return bool(trust_asset and trust_asset.get("source") in ("collected", "stale"))


async def generate_batch_quote(
    keywords: list[str],
    brand_name: str = "客户",
    target_share: float = 0.20,
    cached_competitors: dict = None,
    industry: str = "",
    city: str = "",
    skip_markup: bool = False,
    markup_override: float | None = None,
    agent_user_id: int | None = None,
    business_scope: str = "",
    cost_per_article_override: float | None = None,
    allow_cache_write: bool | None = None,
    cost_multiplier: float = 1.0,
    brand_id: int | None = None,
) -> tuple[dict, str]:
    """
    生成批量关键词报价（三维交叉验证）

    brand_id: [P0-D 2026-06-14] 该报价归属的 GEO 品牌主体 id(报价链路 thread 进来 · 缺省 None)。
      P0-D 信任资产严格按 brand_id 取(不按 brand_name 猜 · 防同名品牌串快照)· flag 关时此参数无副作用。

    Args:
        keywords: 关键词列表
        brand_name: 品牌名称
        target_share: 目标占比
        cached_competitors: 缓存的秘塔竞争度数据
        industry: 行业（供 LLM 参考 + v1_2 全局价格锁 key）
        city: 客户城市（直辖市关键词前缀修正 + v1_2 全局价格锁 key）
        skip_markup: True 时 markup=1.0（C 端透明成本，不走代理端 2.0 溢价）
        markup_override: 代理私有报价系数；skip_markup=True 时忽略
        cost_multiplier: [v2.1] 进货成本倍率(扫码下级=上级 SKU 系数 · 服务商本人 1.0)·
            只乘"系统自动估"成本 · 自设 override 不乘 · ≠1.0 时跳过共享缓存读写(B2 防污染)

    Returns:
        (报价数据, 报价单Markdown)
    """
    from tools.keyword_value_scorer import score_keywords
    from tools.pricing_auditor import audit_and_correct

    # 0. 直辖市关键词前缀修正（补上缺失的城市名）
    if city:
        keywords = normalize_keywords_for_municipality(keywords, city)

    # [CTO-15.23 2026-05-11 P0] Bug C: industry 锁定 brands.industry
    # 防 QZQZ 一会"全屋定制" 一会"建筑建材" → 缓存 key 漂移 · 价格不稳
    industry = _lock_brand_industry(brand_name, fallback_industry=industry)

    # [报价意图闸 2026-08-04 · WO-QUOTE-INTENT-GATE] 不会给客户带来推荐的词不进报价。
    #   放在 LLM-first / 老公式**分叉之前**,两条引擎共用同一口径(元指令 9:C 端与代理端同源);
    #   若放在分叉之后就要写两遍,两遍必然漂移 —— cluster 路径至今零意图过滤就是这么来的。
    #   品牌名 keyword 无条件豁免(见 helper docstring 里 QZQZ 的判定分叉实测)。
    _protected_brand_kws = {kw for kw in keywords if _is_brand_keyword(kw, brand_name)}
    keywords, policy_excluded, policy_gate_status = _partition_by_commercial_policy(
        keywords, brand_name=brand_name, protected_keywords=_protected_brand_kws,
    )

    # [CTO-15.23 2026-05-12 LLM-first] flag 分支
    # 默认 OFF · 不影响生产 · 白名单灰度 · 失败自动 fallback
    # [Codex round-3 P1 修] skip_markup=True 或 markup_override 时禁 LLM-first
    #   原因:LLM 路径未实现 C 端透明价(skip_markup=True) / 私有 markup_override 重算逻辑
    #   走老公式 · 等 Phase 2 后补 LLM markup 模式 + cache key 含 markup_mode 再开
    try:
        from tools.llm_pricing_flag import is_llm_first_enabled, is_fallback_to_formula_enabled
        from tools.batch_pricing_llm import llm_first_batch_quote
        if skip_markup or markup_override is not None:
            print(f"[LLM-first 报价] skip_markup={skip_markup} markup_override={markup_override} · "
                  f"LLM 路径未实现 markup 模式重算 · 走老公式")
            llm_on = False
        else:
            llm_on = is_llm_first_enabled(agent_user_id=agent_user_id)
    except Exception as _exc:
        llm_on = False
        is_fallback_to_formula_enabled = lambda: True  # noqa: E731

    if llm_on:
        try:
            print(f"[LLM-first 报价] flag=ON · agent={agent_user_id} · {len(keywords)} keywords")
            quote_data, markdown, skipped = await llm_first_batch_quote(
                keywords=keywords,
                brand_name=brand_name,
                target_share=target_share,
                industry=industry,
                city=city,
                skip_markup=skip_markup,
                markup_override=markup_override,
                business_scope=business_scope,
                cost_per_article_override=cost_per_article_override,
            )
            # [报价意图闸 2026-08-04] LLM-first 也吃同一份闸结果(闸在分叉前已跑 · 此处只透传)
            quote_data["policy_excluded_keywords"] = policy_excluded
            quote_data["policy_gate_status"] = policy_gate_status
            markdown += _policy_excluded_markdown(policy_excluded, policy_gate_status)
            return quote_data, markdown
        except Exception as exc:
            print(f"[LLM-first 报价] 失败 · fallback 公式: {exc}")
            if not is_fallback_to_formula_enabled():
                raise
            # LLM 失败 → 落到下面老公式 · 老公式自己处理 informational(低权重)
            # Phase 2 可加 carry should_quote_false_keywords · 现 Phase 1 老公式不剔除

    # [CTO-15.23 2026-05-11 P0] Bug A: 品牌名 keyword 分流 · 不走 5118/秘塔/LLM 定价
    # 老板报 "QZQZ美学定制" 作 keyword 报价 1745 元 · 真因是品牌名进 LLM intent 分类不稳定
    # 分流后:品牌名 keyword 固定低价 ¥120 · 其他词走正常 pipeline
    effective_markup_for_brand = 1.0 if skip_markup else (markup_override if markup_override is not None else 1.0)  # [§4.5/决策5] 默认回成本
    brand_keywords = [kw for kw in keywords if _is_brand_keyword(kw, brand_name)]
    non_brand_keywords = [kw for kw in keywords if kw not in brand_keywords]
    if brand_keywords:
        print(f"  [品牌名拦截] {len(brand_keywords)}个 keyword 含品牌名「{brand_name}」→ 走固定低价: {brand_keywords[:3]}")
    keywords = non_brand_keywords

    # [模块5 2026-06-07 堵信息词回流 + 二审隔离 2026-06-13] 老公式 fallback carry should_quote_false:
    #   仅当本次尝试过 LLM-first(llm_on·失败 fallback 到此)才读 keyword_price_cache_llm 剔信息型词;
    #   LLM_FIRST 全局关 + 白名单空 → llm_on=False → v2.2 公式路径完全不读 LLM cache(隔离详见 helper docstring)。
    keywords = _filter_llm_should_quote_false(keywords, brand_name, business_scope, llm_active=llm_on)
    # 注:报价意图闸已在 LLM-first 分叉之前跑过(policy_excluded / policy_gate_status 在那里绑定),
    #   此处不再重复调用 —— 重复调等于对已过闸的词再判一次,纯浪费且给未来留漂移口子。

    print(f"[报价V3] 三维交叉验证 + 异常审计: {len(keywords)}个关键词, 目标占比{target_share}")

    # ========== 7天价格锁定：先查缓存 ==========
    # [Bug B] brand_name 归一化作为缓存 key · 防"QZQZ美学定制" vs "QZQZ 美学定制" 当 2 个不同 brand
    cache_brand_key = _normalize_brand_name(brand_name) or brand_name
    cached_prices = {}
    # [M2 2026-06-07] 服务商自设成本=per-agent · 跳过全局价格缓存【读】:防 per-agent 成本污染全局锁(B2 铁律)·
    #   且确保 override 实时生效(不被旧 system-cost base 缓存盖住)。未设则照常读全局缓存(动态成本·keyword 维·可共享)。
    # [v2.1 2026-06-11] 进货倍率 ≠1(扫码下级)同跳:共享缓存底盘是平台层成本 · 命中后下级按平台价算 = 报价低于真实成本亏
    _has_cost_multiplier = abs(float(cost_multiplier or 1.0) - 1.0) > 0.001
    # [P0-D 2026-06-14 · Codex#1 返修] 按 brand_id 取信任资产(flag 关→None)· collected/stale 会调篇数 → 价 per-brand
    #   → 跳过 (industry,city,keyword) 共享缓存读写(防 A 客户难度价污染 B 客户)。missing/None 不动价 → 缓存照常。
    trust_asset = _fetch_trust_asset_for_pricing(brand_id, industry=industry)  # [B4-1] 带行业格局(flag后·industry键·缓存安全)
    _p0d_cache_skip = _trust_price_active(trust_asset)
    if cost_per_article_override is not None:
        print(f"  [M2 成本自设] 服务商自设单篇成本 ¥{cost_per_article_override} · 跳过全局价格缓存 · 按自设成本实时算价")
    elif _has_cost_multiplier:
        print(f"  [v2.1 进货倍率] 下级进货成本倍率 ×{cost_multiplier} · 跳过全局价格缓存 · 按真实进货成本实时算价")
    elif _p0d_cache_skip:
        print(f"  [P0-D 信任资产] 品牌有信任快照(per-brand 难度价)· 跳过 (industry,city,keyword) 共享缓存读写")
    else:
        try:
            from db.diagnosis_db import get_cached_keyword_prices
            cached_prices = get_cached_keyword_prices(cache_brand_key, keywords, industry=industry or None, city=city or None)
            if cached_prices:
                cached_count = len(cached_prices)
                print(f"  [价格锁定] 命中{cached_count}个关键词的7天缓存，价格保持稳定")
        except Exception as e:
            print(f"  [价格锁定] 缓存查询失败（{e}），将正常计算")

    # 分离：已缓存的词 vs 需要新算的词
    new_keywords = [kw for kw in keywords if kw not in cached_prices]
    cached_keyword_list = [kw for kw in keywords if kw in cached_prices]

    # ========== 对新词进行三维评分 ==========
    scored_new = []
    metaso_cache = {}
    audit_report = ""
    unavailable_rows = []

    if new_keywords:
        print(f"  [新词] {len(new_keywords)}个关键词需要实时评分")

        # 1. 三维评分（5118 + 秘塔 + LLM 并行）
        scored_new, metaso_cache = await score_keywords(
            keywords=new_keywords,
            industry=industry,
            target_share=target_share,
            cached_metaso=cached_competitors,
            concurrency=BATCH_CONFIG["concurrency"],
            brand_name=brand_name,
            city=city,
            cost_per_article_override=cost_per_article_override,  # [M2] 服务商自设成本 A完全覆盖
            cost_multiplier=cost_multiplier,  # [v2.1] 进货倍率(下级=上级 SKU 系数)
            trust_asset=trust_asset,          # [P0-D] 品牌信任资产(上游按 brand_id 取·flag 关 None)
        )

        # [2026-06-11 老板拍 · 真实第一] 数据断供词剥离:不进审计/定价/缓存,单独返回提示重试
        scored_new, unavailable_rows = _split_price_unavailable(scored_new)

        # 2. 异常审计（检测 → LLM审查 → 深探 → 修正）
        scored_new, audit_report = await audit_and_correct(scored_new, industry=industry)

        # 为新词计算三套餐价格（C 端 skip_markup=True 走透明成本）
        scored_new = _enrich_keywords_with_tier_prices(
            scored_new,
            skip_markup=skip_markup,
            markup_override=markup_override,
        )
    else:
        print(f"  [全部命中缓存] 所有{len(keywords)}个关键词使用缓存价格")

    # ========== 从缓存恢复已报价的词 ==========
    scored_cached = []
    for kw in cached_keyword_list:
        c = cached_prices[kw]
        scored_cached.append({
            "keyword": kw,
            # 定价
            "required_articles": _normalize_article_capacity(c.get("standard_articles")),
            "selling_price": int(c.get("standard_price", 0)),
            "cost_per_article": c.get("cost_per_article", 60),
            "total_cost": round(_normalize_article_capacity(c.get("standard_articles"))
                                * c.get("cost_per_article", 60), 1),
            "markup_ratio": c.get("markup_ratio", 1.0),  # [§4.5/决策5] 默认回成本
            # 三维评分
            "difficulty_score": c.get("difficulty_score", 1.0),
            "value_score": c.get("value_score", 1.0),
            # 5118原始数据
            "search_volume": c.get("search_volume", 0),
            "sem_price": c.get("sem_price", 0),
            "bidword_company_count": c.get("bidword_company_count", 0),
            # 秘塔原始数据
            "competitor_count": c.get("competitor_count", 1),
            "effective_competition": c.get("effective_competition", c.get("competitor_count", 1)),
            "content_count": c.get("content_count", 0),
            "recommended_platforms": c.get("recommended_platforms", []),
            "source_authority": c.get("source_authority", {}),
            "intent": c.get("intent", "informational"),
            "funnel_stage": c.get("funnel_stage", "awareness"),
            "search_probability": c.get("search_probability", 0.5),
            "data_source": c.get("data_source", "cache"),
            # [C2-bis 2026-06-05] 算价底盘字段(keyword_type/is_broad 等)从缓存恢复 → 供下方末位重算派生客户价
            #   缓存存的是「算价底盘 + 默认 P0 派生价」· 客户价每次按当前服务商 markup/base 末位重算(不锁成品价)
            "keyword_type": c.get("keyword_type") or ("national_niche" if c.get("is_broad") else "local_city"),
            "is_broad": c.get("is_broad", False),
            "market_scope": c.get("market_scope"),
            "competition_band": c.get("competition_band"),
            "needs_review": c.get("needs_review", False),
            # 三套餐价格
            "entry_price": int(c.get("entry_price", 0)),
            "entry_articles": c.get("entry_articles", 0),
            "standard_price": int(c.get("standard_price", 0)),
            "standard_articles": c.get("standard_articles", 0),
            "flagship_price": int(c.get("flagship_price", 0)),
            "flagship_articles": c.get("flagship_articles", 0),
            # [v2.1 2026-06-11] v2 算价底盘随缓存恢复(漏掉 → recalculate_for_tier 掉 legacy 分支口径打架)
            "pricing_formula_version": c.get("pricing_formula_version"),
            "v2_assessor_data": c.get("v2_assessor_data") or {},
            # 【§4.2】超红海标随缓存恢复(对齐 cluster 路径 · 丢标 → 重进保证价 + 达标计数 = 灾难)
            "super_red_ocean": c.get("super_red_ocean", False),
            "super_red_ocean_level": c.get("super_red_ocean_level", "none"),
            "competition_ratio": c.get("competition_ratio", 0.0),
            # 标记来源
            "_from_cache": True,
            "_cached_at": c.get("cached_at", ""),
            # [价格锁承诺 2026-08-05] 命中缓存 = 这词**真锁着**,锁到这行的 expires_at(不是今天+7)
            "_cache_expires_at": c.get("expires_at"),
        })

    # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 逐词锁期 map(只收录真锁住的词)
    _lock_map: dict = {}
    for _row in scored_cached:
        _record_locked(_lock_map, [_row["keyword"]], _row.get("_cache_expires_at"))

    # ========== 合并：保持原始关键词顺序 ==========
    # [Bug A] 把分流出去的 brand_keywords 固定低价加回结果
    scored_brand = [_make_brand_keyword_price(kw, markup=effective_markup_for_brand, skip_markup=skip_markup) for kw in brand_keywords]
    scored_map = {}
    for s in scored_new:
        scored_map[s["keyword"]] = s
    for s in scored_cached:
        scored_map[s["keyword"]] = s
    for s in scored_brand:
        scored_map[s["keyword"]] = s
    # 用原始入参顺序(含品牌名词)拼回结果
    original_keywords = non_brand_keywords + brand_keywords
    scored = [scored_map[kw] for kw in original_keywords if kw in scored_map]

    # ========== 保存新词到缓存（7天锁定 · [C2-bis] 存「算价底盘 + 默认 P0 派生价」） ==========
    # 缓存权威 = 算价底盘(D2 列:keyword_type/effective_competition/competition_band/value/difficulty/
    #   cost_per_article/pricing_formula_version)。entry/standard/flagship_price 仅【默认 markup=2 P0 派生价快照】,
    #   命中后由上方 _enrich 从底盘末位重算客户价(不锁成品价)→ 将来开放服务商 base/markup 不污染全局锁。
    # 仅默认口径(非 skip_markup / 非 markup_override)写入共享缓存;C 端/per-agent 不写(避免污染)。
    # [价格锁回归修 2026-06-10 audit P1] 写缓存资格用 allow_cache_write 显式控制(解耦「算价系数」与「写缓存」):
    #   allow_cache_write is None(旧调用方)→ 退回旧判据 markup_override is None(向后兼容);
    #   显式 bool → 仅默认口径(True)写。无论如何 cost_per_article_override 非 None 不写(自设成本污染共享底盘 cost)。
    # 根因:定价权批 P1-1 把 markup_override 改为恒传实际值(防回退 settings),致旧 `markup_override is None` 恒 False、共享价格锁全面停写。
    _can_write_cache = _should_write_shared_cache(markup_override, cost_per_article_override, allow_cache_write,
                                                  cost_multiplier=cost_multiplier,
                                                  trust_price_active=_p0d_cache_skip)
    if scored_new:
        try:
            from db.diagnosis_db import save_keyword_prices_cache
            if skip_markup:
                # C 端路径：当前 scored_new 是 markup=1.0，不能直接存（否则污染代理端）
                # 跳过缓存写入（C 端不共享代理端 cache，避免混入）
                print(f"  [价格锁定] C 端路径 skip_markup=True，不写入缓存（避免污染代理端价）")
            elif not _can_write_cache:
                # per-agent 私有系数 / 服务商自设成本 / 下级进货倍率:不写共享缓存,避免污染其他代理默认报价(B2 铁律)。
                print(f"  [价格锁定] 使用代理私有系数/自设成本/进货倍率，不写入共享价格缓存")
            else:
                # [Bug B] 写入也用归一化 brand_name · 跟读取保持一致
                # [完整修复 2026-06-13 High2] 爆价/放飞词剥离后才写共享缓存(防污染全局 7 天锁)
                _to_cache = _cacheable_rows(scored_new)
                if _to_cache:
                    _expires = save_keyword_prices_cache(cache_brand_key, _to_cache, industry=industry or None, city=city or None)
                    # [价格锁承诺 2026-08-05] 写入 commit 成功返回后才登记锁期 —— 抛异常走下面 except,一个都不登记
                    _record_locked(_lock_map, _to_cache, _expires)
                print(f"  [价格锁定] 已缓存{len(_to_cache)}个新词的价格（7天内不变）")
        except Exception as e:
            print(f"  [价格锁定] 缓存保存失败（{e}），不影响本次报价")

    # ========== [C2-bis 2026-06-05] 缓存命中词:客户价一律从底盘末位重算(不锁成品价) ==========
    # 全局缓存锁的是「算价底盘」(keyword_type/effective_competition/value/difficulty/cost_per_article)
    # 不锁「服务商最终卖价」。缓存里的 entry/standard/flagship_price 只是【默认 markup=2 的 P0 派生价快照】,
    # 不作为客户价 source-of-truth → 这里【所有路径】都从底盘经 compute_banded_price 末位重算:
    #   · 默认代理端 markup_override=None → 重算得 markup=2 默认价(与快照同·但已派生不锁)
    #   · C 端 skip_markup → markup=1 透明成本(feedback_cend_agent_shared_system 红线)
    #   · per-agent markup_override → 该代理系数
    # 将来开放服务商自定义 base_cost/markup 时,客户价天然按当前配置派生,不会被烤进共享缓存的成品价污染(C2-bis)。
    if scored_cached:
        _enrich_keywords_with_tier_prices(
            scored_cached,
            skip_markup=skip_markup,
            markup_override=markup_override,
        )
        # scored_cached 是 in-place 修改，scored_map 里的引用已同步更新

    # 3. 生成当前套餐的汇总
    summary = generate_batch_summary(scored, target_share)

    # 4. 生成三个档位的报价（复用已有评分数据，不重新查API）
    tiers = {
        "入门版": 0.10,
        "标准版": 0.20,
        "旗舰版": 0.30,
    }

    tier_summaries = {}
    effective_markup_override = 1.0 if skip_markup else markup_override
    for tier_name, share in tiers.items():
        tier_scored = recalculate_for_tier(scored, share, markup_override=effective_markup_override)
        tier_summaries[tier_name] = generate_batch_summary(tier_scored, share)

    # 5. 生成Markdown（含三套餐单价）
    # [Workflow 审计修 2026-06-11] markdown 头部"关键词数量"按实际计价词(剥离后)计 ·
    #   断供词补人话说明段(markdown 是对客交付物 · 不许数量与明细打架/静默缺词)
    md = generate_batch_markdown(brand_name, [s["keyword"] for s in scored], scored, summary, tier_summaries)
    if unavailable_rows:
        _ukw_names = "、".join(s["keyword"] for s in unavailable_rows[:8])
        _more = f" 等{len(unavailable_rows)}个" if len(unavailable_rows) > 8 else ""
        md += (f"\n\n> ⏳ 另有 {len(unavailable_rows)} 个关键词因网络繁忙本次未能估价"
               f"(未计入上述报价):{_ukw_names}{_more}。稍后重新生成报价即可补算。\n")

    # [报价意图闸 2026-08-04] 被剔的词写进 markdown(对客交付物不许静默缺词 · 与 unavailable 同款处理)
    md += _policy_excluded_markdown(policy_excluded, policy_gate_status)

    return {
        "brand_name": brand_name,
        "keywords": scored,
        "summary": summary,
        "tier_summaries": tier_summaries,
        "competitors_cache": metaso_cache,
        "audit_report": audit_report,
        # [真实第一] 数据断供词(不出价 · 人话 reason · 前端提示稍后重试)
        "unavailable_keywords": _unavailable_payload(unavailable_rows),
        # [报价意图闸 2026-08-04] 不进付费交付的词(只读展示区 · 不计价 · 不进套餐总价)
        "policy_excluded_keywords": policy_excluded,
        "policy_gate_status": policy_gate_status,
        # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] {keyword: "YYYY-MM-DD"} · 只含**真锁住**的词。
        #   不在这个 map 里的词 = 本次没写缓存(P0-D / 自设成本 / 进货倍率 / C 端 / 爆价护栏剥离 / 写失败),
        #   报价层必须不显示锁期。
        _LOCK_MAP_KEY: _lock_map,
    }, md


# ========================================
# Markdown 报价单生成
# ========================================

def generate_batch_markdown(
    brand_name: str,
    keywords: list[str],
    scored_keywords: list[dict],
    summary: dict,
    tier_summaries: dict
) -> str:
    """生成批量报价Markdown"""
    date_str = datetime.now().strftime("%Y年%m月%d日")

    intensity_map = {0.10: '3', 0.20: '4', 0.30: '5'}

    def value_label(score: float) -> str:
        if score >= 1.6:
            return "极高"
        elif score >= 1.3:
            return "高"
        elif score >= 1.0:
            return "中"
        else:
            return "低"

    def intent_cn(intent: str) -> str:
        return {"transactional": "交易型", "commercial": "商业型", "informational": "信息型"}.get(intent, "信息型")

    def competition_emoji(count: int) -> str:
        if count >= 60:
            return "红海"
        elif count >= 40:
            return "超激烈"
        elif count >= 15:
            return "激烈"
        elif count >= 8:
            return "中等"
        elif count >= 3:
            return "温和"
        else:
            return "蓝海"

    md = f"""# GEO关键词优化报价单

**日期**: {date_str}
**客户**: {brand_name}
**关键词数量**: {len(keywords)}个
**定价模型**: 三维交叉验证 (市场数据 + AI搜索竞争度 + 智能意图分析)

---

## 套餐报价汇总

| 套餐 | AI引用概率 | 覆盖强度 | **套餐价** |
|:-----|:--------:|:----------:|--------:|
"""

    for tier_name, tier_sum in tier_summaries.items():
        share = tier_sum['target_share']
        prob = f"{tier_sum.get('avg_exposure_probability', '?')}%"
        stars = int(float(intensity_map.get(share, '3')))
        star_str = '★' * stars + '☆' * (5 - stars)
        md += f"| **{tier_name}** | {prob} | {star_str} | **{tier_sum['final_price']:,}元** |\n"

    entry_prob = tier_summaries.get("入门版", {}).get("avg_exposure_probability", "?")
    std_prob = tier_summaries.get("标准版", {}).get("avg_exposure_probability", "?")
    flag_prob = tier_summaries.get("旗舰版", {}).get("avg_exposure_probability", "?")
    md += f"""
> **AI引用概率说明**（基于实际竞争度计算）：
> - **入门版 {entry_prob}%**：用户搜索时，品牌出现在AI回答中的概率约{entry_prob}%
> - **标准版 {std_prob}%**：用户搜索时，品牌出现在AI回答中的概率约{std_prob}%
> - **旗舰版 {flag_prob}%**：用户搜索时，品牌出现在AI回答中的概率约{flag_prob}%
"""

    std = tier_summaries.get("标准版", summary)
    entry = tier_summaries.get("入门版", summary)
    flagship = tier_summaries.get("旗舰版", summary)

    md += f"""
---

## 各套餐详情

| | 入门版 | 标准版 | 旗舰版 |
|:--|:------:|:------:|:------:|
| **关键词数量** | {entry['keyword_count']}个 | {std['keyword_count']}个 | {flagship['keyword_count']}个 |
| **总发布篇数** | {entry['total_articles']}篇 | {std['total_articles']}篇 | {flagship['total_articles']}篇 |
| **套餐总价** | **{entry['final_price']:,}元** | **{std['final_price']:,}元** | **{flagship['final_price']:,}元** |
| **平均单价** | {entry['avg_price_per_keyword']}元/词 | {std['avg_price_per_keyword']}元/词 | {flagship['avg_price_per_keyword']}元/词 |

---

## 关键词明细（三套餐单价对照）

| 关键词 | 搜索热度 | 竞争度 | 意图类型 | 商业价值 | 入门版 | 标准版 | 旗舰版 |
|:-------|:-------:|:------:|:------:|:------:|------:|------:|------:|
"""

    has_estimate = False
    for p in sorted(scored_keywords, key=lambda x: x.get("standard_price", x.get("selling_price", 0)), reverse=True):
        kw_display = p['keyword'][:20] + ('...' if len(p['keyword']) > 20 else '')
        vol = p.get('search_volume', 0)
        is_estimate = p.get('data_source') == 'llm_estimate'
        if is_estimate and vol > 0:
            vol_str = f"~{vol:,}*"
            has_estimate = True
        elif vol > 0:
            vol_str = f"{vol:,}"
        else:
            vol_str = "-"
        comp_str = competition_emoji(p['competitor_count'])
        intent_str = intent_cn(p.get('intent', ''))
        val_str = value_label(p.get('value_score', 1.0))

        if p.get('super_red_ocean'):
            # [§4.2] 超红海词不出保证价 · 报价单三档显「需单独报价」(不混进套餐价)
            entry_str = standard_str = flagship_str = "需单独报价"
        elif p.get('guarantee_unavailable'):
            # [完整修复 2026-06-13 High1] 爆价/放飞词:三档显「参考价·人工核」(不混进套餐保证价)
            entry_str = standard_str = flagship_str = "参考价·人工核"
        else:
            entry_p = p.get('entry_price', '-')
            standard_p = p.get('standard_price', p.get('selling_price', '-'))
            flagship_p = p.get('flagship_price', '-')

            entry_str = f"{entry_p:,}元" if isinstance(entry_p, (int, float)) else str(entry_p)
            standard_str = f"{standard_p:,}元" if isinstance(standard_p, (int, float)) else str(standard_p)
            flagship_str = f"{flagship_p:,}元" if isinstance(flagship_p, (int, float)) else str(flagship_p)

        # 标记缓存命中的关键词
        cache_mark = "^" if p.get("_from_cache") else ""
        md += f"| {kw_display}{cache_mark} | {vol_str} | {comp_str} | {intent_str} | {val_str} | {entry_str} | {standard_str} | {flagship_str} |\n"

    if has_estimate:
        md += "\n> *标注 `~数字*` 的搜索热度为AI预估值（暂无该词市场数据），仅供参考。*\n"

    has_cached = any(p.get("_from_cache") for p in scored_keywords)
    if has_cached:
        md += "\n> *标注 `^` 的关键词使用7天内已报价缓存，价格保持一致。*\n"


    md += f"""
---

## 定价说明

本报价基于**三维交叉验证 + 异常审计**模型，综合以下数据源确定每个关键词的合理价格：

1. **市场热度数据**：搜索量、SEM竞价出价、竞价公司数量
2. **AI搜索竞争度**：现有内容数量、竞品内容密度
3. **智能意图分析**：搜索意图分类、转化漏斗阶段
4. **异常审计**：自动检测数据矛盾，对可疑词进行AI引擎直测验证

> **商业价值说明**：基于SEM竞价出价、搜索意图和转化潜力综合评估，反映该关键词对客户的商业转化价值

---

*报价有效期：7天*
*生成时间：{datetime.now().isoformat()[:19]}*
"""

    return md


# ========================================
# 快速报价（无API调用，用估算值）
# ========================================

def quick_estimate_price(
    keyword_count: int,
    avg_competitors: int = 30,
    target_share: float = 0.20
) -> dict:
    """快速估算报价（无需API调用），用于销售现场快速给出大概价格范围"""
    cost_per_article = get_cost_per_article()
    config = get_cost_config()

    required_articles = math.ceil(target_share * avg_competitors / (1 - target_share))
    price_per_keyword = required_articles * cost_per_article * config["markup_ratio"]

    total_price = price_per_keyword * keyword_count
    final_price = int(total_price)

    return {
        "keyword_count": keyword_count,
        "avg_competitors": avg_competitors,
        "target_share": target_share,
        "price_per_keyword": int(price_per_keyword),
        "total_before_discount": int(total_price),
        "discount": 0,
        "final_price": final_price,
        "price_range": f"{int(final_price * 0.8):,}元 - {int(final_price * 1.2):,}元",
    }


# ========================================
# 兼容旧接口
# ========================================

async def batch_get_competitors(
    keywords: list[str],
    concurrency: int = 5
) -> dict[str, dict]:
    """兼容旧接口 - 内部转发到新模块"""
    from tools.keyword_value_scorer import fetch_metaso_batch
    return await fetch_metaso_batch(keywords, concurrency=concurrency)


def calculate_batch_price(
    keywords_with_competitors: dict[str, dict],
    target_share: float = 0.20
) -> list[dict]:
    """兼容旧接口"""
    cost_per_article = get_cost_per_article()
    config = get_cost_config()
    markup_ratio = config["markup_ratio"]

    results = []
    for keyword, data in keywords_with_competitors.items():
        competitor_count = data["count"] if isinstance(data, dict) else data
        competitor_count = max(competitor_count, 1)

        if target_share >= 1:
            ts = 0.9
        elif target_share <= 0:
            ts = 0.1
        else:
            ts = target_share

        required_articles = max(1, math.ceil(ts * competitor_count / (1 - ts)))
        base_cost = required_articles * cost_per_article
        selling_price = int(base_cost * markup_ratio)

        results.append({
            "keyword": keyword,
            "competitor_count": competitor_count,
            "required_articles": required_articles,
            "total_cost": base_cost,
            "selling_price": selling_price,
            "recommended_platforms": data.get("platforms", []) if isinstance(data, dict) else [],
        })

    return results


# ========================================
# 主题包报价流程（新模式）
# ========================================

async def generate_cluster_quote(
    keywords: list[str],
    brand_name: str = "客户",
    industry: str = "",
    city: str = "",
    cached_competitors: dict = None,
    skip_markup: bool = False,
    markup_override: float | None = None,
    progress_callback: Optional[Callable[[str, str, int], Any]] = None,
    cost_per_article_override: float | None = None,
    allow_cache_write: bool | None = None,
    cost_multiplier: float = 1.0,
    brand_id: int | None = None,
) -> dict:
    """
    完整的主题包报价流程:
    1. 调用现有 score_keywords() 给所有词评分（单词定价公式，不改）
    2. 调用 cluster_keywords() 聚类分包
    3. 用 _enrich_keywords_with_tier_prices() 算三档价格
    4. 对每个包: 汇总核心词/附赠词的三档价格
    5. 返回 clusters_data 结构（写入 session.clusters_data）

    向后兼容: 不修改 generate_batch_quote()，新流程独立运行。

    Returns:
        {
            "clusters": [...],
            "unclustered_keywords": [],
            "stats": {...},
            "tier_summaries": {...},
            "competitors_cache": {...},
            "audit_report": str,
        }
    """
    from tools.keyword_value_scorer import score_keywords
    from tools.pricing_auditor import audit_and_correct
    from tools.keyword_cluster import cluster_keywords

    # 0. 直辖市关键词前缀修正
    if city:
        keywords = normalize_keywords_for_municipality(keywords, city)

    # [CTO-15.23 2026-05-11 P0] Bug C: industry 锁定 brands.industry
    industry = _lock_brand_industry(brand_name, fallback_industry=industry)

    # [CTO-15.23 2026-05-11 P0] Bug A: 品牌名 keyword 分流 · 固定低价不参与定价
    effective_markup_for_brand = 1.0 if skip_markup else (markup_override if markup_override is not None else 1.0)  # [§4.5/决策5] 默认回成本
    brand_keywords_list = [kw for kw in keywords if _is_brand_keyword(kw, brand_name)]
    non_brand_keywords_list = [kw for kw in keywords if kw not in brand_keywords_list]
    if brand_keywords_list:
        print(f"  [品牌名拦截] {len(brand_keywords_list)}个 keyword 含品牌名「{brand_name}」→ 走固定低价: {brand_keywords_list[:3]}")
    keywords = non_brand_keywords_list

    # [报价意图闸 2026-08-04] cluster 路径同口径 —— 原本这条路径连 should_quote 那条死闸都没有,
    #   是三条报价路径里唯一零意图过滤的。C 端与代理端同源(元指令 9),两条路径必须一致。
    #   此处品牌词已在上面 non_brand_keywords_list 剥走 → protected 传空集即可(不是忘了传)。
    keywords, policy_excluded, policy_gate_status = _partition_by_commercial_policy(
        keywords, brand_name=brand_name, protected_keywords=set(),
    )

    print(f"[主题包报价] {len(keywords)}个关键词, 品牌={brand_name}")

    # ========== 7天价格锁定：先查缓存 ==========
    # [Bug B] brand_name 归一化作为缓存 key
    cache_brand_key = _normalize_brand_name(brand_name) or brand_name
    cached_prices = {}
    # [M2 2026-06-07] 服务商自设成本=per-agent · 跳过全局价格缓存【读】(防污染全局锁 + override 实时生效·同 flat 路径)
    # [v2.1 2026-06-11] 进货倍率 ≠1(扫码下级)同跳:共享缓存底盘是平台层成本 · 命中后按平台价算 = 低于真实成本亏
    _has_cost_multiplier = abs(float(cost_multiplier or 1.0) - 1.0) > 0.001
    # [P0-D 2026-06-14 · Codex#1 返修] 同 flat 路径:按 brand_id 取信任资产 · collected/stale 跳共享缓存读写(per-brand 价)
    trust_asset = _fetch_trust_asset_for_pricing(brand_id, industry=industry)  # [B4-1] 带行业格局(flag后·industry键·缓存安全)
    _p0d_cache_skip = _trust_price_active(trust_asset)
    if cost_per_article_override is not None:
        print(f"  [M2 成本自设] 服务商自设单篇成本 ¥{cost_per_article_override} · 跳过全局价格缓存 · 按自设成本实时算价")
    elif _has_cost_multiplier:
        print(f"  [v2.1 进货倍率] 下级进货成本倍率 ×{cost_multiplier} · 跳过全局价格缓存 · 按真实进货成本实时算价")
    elif _p0d_cache_skip:
        print(f"  [P0-D 信任资产] 品牌有信任快照(per-brand 难度价)· 跳过共享缓存读写")
    else:
        try:
            from db.diagnosis_db import get_cached_keyword_prices
            cached_prices = get_cached_keyword_prices(cache_brand_key, keywords, industry=industry or None, city=city or None)
            if cached_prices:
                print(f"  [价格锁定] 命中{len(cached_prices)}个关键词的7天缓存")
        except Exception as e:
            print(f"  [价格锁定] 缓存查询失败({e})")

    new_keywords = [kw for kw in keywords if kw not in cached_prices]
    cached_keyword_list = [kw for kw in keywords if kw in cached_prices]

    # ========== 对新词进行三维评分 ==========
    scored_new = []
    metaso_cache = {}
    audit_report = ""
    cluster_result_parallel = None
    unavailable_rows = []

    # [CTO-15.5 Q1.A+B 2026-04-20] 进度回调支持 + audit/cluster 并行
    # 老板反馈: SSE 卡 45% 久, root cause: 此函数内部 4 步串行但前端只推一次进度
    # 优化: 每阶段 callback + audit(LLM) 和 cluster(LLM) 并行跑(不互相依赖 cluster 只看 keyword 文本)
    async def _notify(stage: str, label: str, percent: int):
        if progress_callback:
            try:
                res = progress_callback(stage, label, percent)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as _e:
                print(f"  [progress_cb] 调用失败(忽略): {_e}")

    # ========== 先构造缓存词评分(0 耗时 · 用于并行 cluster 的完整 scored 集合) ==========
    scored_cached = []
    for kw in cached_keyword_list:
        c = cached_prices[kw]
        scored_cached.append({
            "keyword": kw,
            "required_articles": _normalize_article_capacity(c.get("standard_articles")),
            "selling_price": int(c.get("standard_price", 0)),
            "cost_per_article": c.get("cost_per_article", 60),
            "total_cost": round(_normalize_article_capacity(c.get("standard_articles"))
                                * c.get("cost_per_article", 60), 1),
            "difficulty_score": c.get("difficulty_score", 1.0),
            "value_score": c.get("value_score", 1.0),
            "search_volume": c.get("search_volume", 0),
            "competitor_count": c.get("competitor_count", 1),
            "effective_competition": c.get("effective_competition", c.get("competitor_count", 1)),
            "intent": c.get("intent", "informational"),
            "search_probability": c.get("search_probability", 0.5),
            "geo_multiplier": c.get("geo_multiplier", 1.0),
            "is_broad": c.get("is_broad", False),
            # [C2-bis 2026-06-05] cluster 路径同 flat:恢复算价底盘字段 → 下方所有缓存命中从底盘末位重算
            "data_source": c.get("data_source", "cache"),
            "keyword_type": c.get("keyword_type") or ("national_niche" if c.get("is_broad") else "local_city"),
            "market_scope": c.get("market_scope"),
            "competition_band": c.get("competition_band"),
            "needs_review": c.get("needs_review", False),
            # 【v1.3 §4.2】超红海标随缓存 round-trip(版本 bump 后旧缓存 lazy 失效重算 · 新缓存读回)
            "super_red_ocean": c.get("super_red_ocean", False),
            "super_red_ocean_level": c.get("super_red_ocean_level", "none"),
            "competition_ratio": c.get("competition_ratio", 0.0),
            "entry_price": int(c.get("entry_price", 0)),
            "entry_articles": c.get("entry_articles", 0),
            "standard_price": int(c.get("standard_price", 0)),
            "standard_articles": c.get("standard_articles", 0),
            "flagship_price": int(c.get("flagship_price", 0)),
            "flagship_articles": c.get("flagship_articles", 0),
            # [v2.1 2026-06-11] v2 算价底盘随缓存恢复(漏掉 → recalculate_for_tier 掉 legacy 分支口径打架)
            "pricing_formula_version": c.get("pricing_formula_version"),
            "v2_assessor_data": c.get("v2_assessor_data") or {},
            "_from_cache": True,
            # [价格锁承诺 2026-08-05] 同 flat:命中缓存 = 真锁着,锁到这行的 expires_at
            "_cache_expires_at": c.get("expires_at"),
        })

    # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 逐词锁期 map(同 flat 路径口径)
    _lock_map: dict = {}
    for _row in scored_cached:
        _record_locked(_lock_map, [_row["keyword"]], _row.get("_cache_expires_at"))

    if new_keywords:
        print(f"  [新词] {len(new_keywords)}个关键词需要实时评分")
        await _notify("metaso_search", f"正在秘塔搜索 · {len(new_keywords)} 词", 47)
        scored_new, metaso_cache = await score_keywords(
            keywords=new_keywords,
            industry=industry,
            target_share=0.20,
            cached_metaso=cached_competitors,
            concurrency=BATCH_CONFIG["concurrency"],
            brand_name=brand_name,
            city=city,
            cost_per_article_override=cost_per_article_override,  # [M2] 服务商自设成本 A完全覆盖
            cost_multiplier=cost_multiplier,  # [v2.1] 进货倍率(下级=上级 SKU 系数)
            trust_asset=trust_asset,          # [P0-D] 品牌信任资产(上游按 brand_id 取·flag 关 None)
        )
        # [2026-06-11 老板拍 · 真实第一] 数据断供词剥离:不进聚类(锚词选取)/审计/定价/缓存
        scored_new, unavailable_rows = _split_price_unavailable(scored_new)
        # audit 和 cluster 并行 · cluster 只用 keyword 文本(tools/keyword_cluster.py:508)
        # audit 修 difficulty/price 不影响 cluster Step 1 LLM 分组
        # 并行省 5-8s(两个 LLM 调用并发)
        # cluster 用 audit 前的 scored_new + scored_cached 完整集合(keyword 文本 + value_score 做聚类)
        await _notify("audit_cluster", "竞品审计 + 主题聚类(并行)", 55)
        _pre_audit_scored = list(scored_new) + list(scored_cached)
        audit_task = asyncio.create_task(audit_and_correct(scored_new, industry=industry))
        cluster_task = asyncio.create_task(cluster_keywords(_pre_audit_scored, brand_name, industry))
        try:
            audit_result, cluster_result_parallel = await asyncio.gather(audit_task, cluster_task)
            scored_new, audit_report = audit_result
        except Exception as _parallel_e:
            # 并行失败降级串行(稳)
            print(f"  [并行] audit/cluster 异常降级串行: {_parallel_e}")
            scored_new, audit_report = await audit_and_correct(scored_new, industry=industry)
            cluster_result_parallel = None
        await _notify("pricing", "三档定价汇总", 63)
        scored_new = _enrich_keywords_with_tier_prices(
            scored_new,
            skip_markup=skip_markup,
            markup_override=markup_override,
        )
    else:
        print(f"  [全部命中缓存] 所有{len(keywords)}个关键词使用缓存价格")

    # ========== 合并 ==========
    # [Bug A] 把分流出去的品牌名 keyword 固定低价加回结果
    scored_brand = [_make_brand_keyword_price(kw, markup=effective_markup_for_brand, skip_markup=skip_markup) for kw in brand_keywords_list]
    scored_map = {s["keyword"]: s for s in scored_new}
    for s in scored_cached:
        scored_map[s["keyword"]] = s
    for s in scored_brand:
        scored_map[s["keyword"]] = s
    # 用原始入参顺序(含品牌名词)拼回
    original_keywords = non_brand_keywords_list + brand_keywords_list
    scored = [scored_map[kw] for kw in original_keywords if kw in scored_map]

    # ========== 保存新词到缓存（C 端 skip_markup 时跳过，避免污染代理端价）==========
    # [价格锁回归修 2026-06-10 audit P1] 同 flat:allow_cache_write 显式控制写缓存资格(None=旧判据向后兼容),自设成本恒不写。
    _can_write_cache = _should_write_shared_cache(markup_override, cost_per_article_override, allow_cache_write,
                                                  cost_multiplier=cost_multiplier,
                                                  trust_price_active=_p0d_cache_skip)
    if scored_new and not skip_markup and _can_write_cache:
        try:
            from db.diagnosis_db import save_keyword_prices_cache
            # [Bug B] 写入也用归一化 brand_name
            # [完整修复 2026-06-13 High2] 爆价/放飞词剥离后才写共享缓存(同 flat 路径)
            _to_cache = _cacheable_rows(scored_new)
            if _to_cache:
                _expires = save_keyword_prices_cache(cache_brand_key, _to_cache, industry=industry or None, city=city or None)
                # [价格锁承诺 2026-08-05] 同 flat:commit 成功返回后才登记锁期
                _record_locked(_lock_map, _to_cache, _expires)
            print(f"  [价格锁定] 已缓存{len(_to_cache)}个新词")
        except Exception as e:
            print(f"  [价格锁定] 缓存保存失败({e})")
    elif scored_new and skip_markup:
        print(f"  [价格锁定] C 端路径 skip_markup=True，不写入缓存（避免污染代理端价）")
    elif scored_new:
        print(f"  [价格锁定] 使用代理私有系数/自设成本/进货倍率，不写入共享价格缓存")

    # ========== [C2-bis 2026-06-05] 缓存命中词:客户价一律从底盘末位重算(不锁成品价 · 同 flat 路径) ==========
    # 全局缓存锁「算价底盘」不锁「服务商最终卖价」· 缓存里 entry/standard/flagship_price 仅默认 markup=2 P0 快照。
    # 所有路径(默认代理 / C 端 skip_markup / per-agent override)都从底盘经 compute_banded_price 末位重算客户价
    # → 将来开放服务商 base/markup 不会被烤进共享缓存的成品价污染(C2-bis 主题包路径闭环)。
    if scored_cached:
        _enrich_keywords_with_tier_prices(
            scored_cached,
            skip_markup=skip_markup,
            markup_override=markup_override,
        )

    # ========== 聚类分包 ==========
    # [CTO-15.5 Q1.B] 并行跑过的话,cluster_result_parallel 已有结果,重新关联 audit 后 scored
    # 但 cluster Step 2 _build_groups_from_llm_clusters 读 scored 的 value_score/price,
    # 需要用最新 audit 后的 scored。简单做法:并行 cluster 已跑完 LLM 分组(耗时大头),
    # 再用最新 scored 做 Step 3+(关联+去重+core 选取),但当前 cluster_keywords 不拆分。
    # 保守做法:如果并行 cluster 跑完且聚类 cluster_count>=1,复用其结果(快),
    # 否则串行重跑(稳);新词少的场景(<5)不并行,直接串行跑。
    if cluster_result_parallel and cluster_result_parallel.get("clusters"):
        cluster_result = cluster_result_parallel
        print(f"  [聚类] 复用并行结果: {len(cluster_result.get('clusters', []))} 包")
    else:
        cluster_result = await cluster_keywords(scored, brand_name, industry)

    # ========== 为每个包计算三档汇总价格 ==========
    for cluster in cluster_result["clusters"]:
        cluster["pricing"] = _compute_cluster_pricing(cluster, scored_map)

    # ========== 总报价汇总 ==========
    # v1_3 (CTO-15.1 2026-04-19): 扩到 4 档对齐托管 SOV_TIERS，旧 C 端 GEO 方案卡 消费 + tag "单次投放方案"
    tier_summaries = {}
    for tier_key in ("entry", "standard", "flagship", "strong"):
        total_core_price = 0
        total_full_price = 0
        total_articles = 0
        for c in cluster_result["clusters"]:
            if c.get("is_selected", True):
                total_core_price += c["pricing"][tier_key]["core_price"]
                total_full_price += c["pricing"][tier_key]["full_price"]
                total_articles += c["pricing"][tier_key]["core_articles"]
        tier_summaries[tier_key] = {
            "core_price": total_core_price,
            "full_price": total_full_price,
            "savings": total_full_price - total_core_price,
            "total_articles": total_articles,
        }

    # [Codex 复诊 #1 2026-06-11] 全断供时 cluster_keywords([]) 返回 stats={} · 裸下标 KeyError 炸 500
    #   → .get 防御(空结果照常返回 · 全断供的 503 语义由 API 层守卫给出人话)
    print(f"  [主题包报价] 完成: {cluster_result.get('stats', {}).get('cluster_count', 0)}个包, "
          f"标准版核心价 {tier_summaries['standard']['core_price']:,}元")

    return {
        **cluster_result,
        "tier_summaries": tier_summaries,
        "competitors_cache": metaso_cache,
        "audit_report": audit_report,
        # [真实第一] 数据断供词(不出价 · 人话 reason · 前端提示稍后重试)
        "unavailable_keywords": _unavailable_payload(unavailable_rows),
        # [报价意图闸 2026-08-04] 不进付费交付的词(只读展示区 · 不计价 · 不进套餐总价)
        "policy_excluded_keywords": policy_excluded,
        "policy_gate_status": policy_gate_status,
        # [价格锁承诺 2026-08-05 · WO_PRICE_LOCK_PROMISE] 逐词锁期(同 flat 路径口径 · 只含真锁住的词)。
        #   cluster 的 core_keywords 是**白名单拷贝**(tools/keyword_cluster.py),词级标记传不过去,
        #   所以锁期只能走这张 keyword→date 的 map,不能挂在词 dict 上。
        _LOCK_MAP_KEY: _lock_map,
    }


def _floor_keyword_price(scored_kw: dict, raw_price: int) -> int:
    """[¥400 地板修 2026-06-10 audit P1] cluster 单词售价对齐 flat(selection_api:1512/1517):
    报价词套 max(价, 地板·全国词 NATIONAL / 否则 MIN_KEYWORD_PRICE)→ 过 approve 终验闸(≥MIN);
    信息型(should_quote=False)不套地板(终验闸豁免·价该归零·不被抬到 400)。
    修前 cluster 价直接来自出厂 band(如 local_county entry 260~585·markup=1 时 <400),整单被终验闸 ¥400 拦发送。"""
    if scored_kw.get("should_quote", True) is False:
        return raw_price
    _kw_min = MIN_KEYWORD_PRICE_NATIONAL if scored_kw.get("is_broad") else MIN_KEYWORD_PRICE
    return max(int(raw_price), int(_kw_min))


def _compute_cluster_pricing(cluster: dict, scored_map: dict) -> dict:
    """
    计算单个主题包的 4 档定价汇总。
    v1_3 (CTO-15.1 2026-04-19): 扩 strong 档对齐托管 SOV_TIERS

    core_price = sum(选中核心词的单价)
    full_price = sum(所有词的单价，含附赠可升级词)
    savings = full_price - core_price
    """
    pricing = {}

    for tier_key in ("entry", "standard", "flagship", "strong"):
        price_field = f"{tier_key}_price"
        articles_field = f"{tier_key}_articles"

        core_price = 0
        core_articles = 0
        full_price = 0
        full_articles = 0

        # 核心词（客户选中的）
        for kw in cluster["core_keywords"]:
            kw_text = kw["keyword"]
            scored = scored_map.get(kw_text, {})
            # 【v1.3 §4.2】超红海词:标透传到聚类核心词(供前端 🔴 渲染)+ 不出保证价(不进 core/full 汇总·不写回三档价)
            if scored.get("super_red_ocean") or kw.get("super_red_ocean"):
                kw["super_red_ocean"] = True
                kw["super_red_ocean_level"] = scored.get("super_red_ocean_level", kw.get("super_red_ocean_level", "yellow"))
                kw["competition_ratio"] = scored.get("competition_ratio", kw.get("competition_ratio", 0.0))
                kw["needs_review"] = True
                continue
            # [完整修复 2026-06-13 High1] 爆价/放飞词同口径:标透传到聚类核心词 + 不进 core/full 汇总(不写回三档价 → 客户端取 0)
            if scored.get("guarantee_unavailable") or kw.get("guarantee_unavailable"):
                kw["guarantee_unavailable"] = True
                kw["needs_review"] = True
                continue
            p = _floor_keyword_price(scored, int(scored.get(price_field, kw.get("selling_price", 0))))
            # [P1 容量合同 2026-08-08] 走 SSOT 规整:显式 0 篇(覆盖词)不再被 int() 之外的写法吃掉,
            #   缺失时的兜底值来自具名常量 LEGACY_MISSING_CAPACITY_DEFAULT(数值与上线前一致)
            a = _normalize_article_capacity(
                scored.get(articles_field, kw.get("required_articles")))

            if kw.get("is_selected", True):
                core_price += p
                core_articles += a
            full_price += p
            full_articles += a

            # 写回三档价格到核心词数据
            kw.setdefault(tier_key, {})
            kw[tier_key] = {"price": p, "articles": a}

        # 附赠词（可升级的真实词，不含变体）
        for kw in cluster["covered_keywords"]:
            if kw.get("source") == "generated_variant":
                continue  # 变体词无价格
            kw_text = kw["keyword"]
            scored = scored_map.get(kw_text, {})
            # 【v1.3 §4.2】超红海附赠词:标透传 + 不进汇总(不出保证价)
            if scored.get("super_red_ocean") or kw.get("super_red_ocean"):
                kw["super_red_ocean"] = True
                continue
            # [完整修复 2026-06-13 High1] 爆价/放飞附赠词同口径:标透传 + 不进汇总
            if scored.get("guarantee_unavailable") or kw.get("guarantee_unavailable"):
                kw["guarantee_unavailable"] = True
                continue
            p = _floor_keyword_price(scored, int(scored.get(price_field, kw.get("selling_price", 0))))
            # [P1 容量合同 2026-08-08] 走 SSOT 规整:显式 0 篇(覆盖词)不再被 int() 之外的写法吃掉,
            #   缺失时的兜底值来自具名常量 LEGACY_MISSING_CAPACITY_DEFAULT(数值与上线前一致)
            a = _normalize_article_capacity(
                scored.get(articles_field, kw.get("required_articles")))
            full_price += p
            full_articles += a

            # 写回三档价格到附赠词（升级时需要）
            kw.setdefault(tier_key, {})
            kw[tier_key] = {"price": p, "articles": a}

        pricing[tier_key] = {
            "core_price": core_price,
            "core_articles": core_articles,
            "full_price": full_price,
            "full_articles": full_articles,
            "savings": full_price - core_price,
        }

    return pricing


# ========================================
# 测试
# ========================================

if __name__ == "__main__":
    async def main():
        keywords = [
            "深圳GEO优化公司推荐",
            "深圳AI搜索优化公司哪家好",
            "GEO优化公司排名",
            "AI搜索优化价格",
            "深圳社媒优化公司推荐",
            "抖音搜索优化怎么做",
            "小红书搜索优化公司",
            "GEO生成式引擎优化",
        ]

        print("=" * 70)
        print(f"报价V2测试 - {len(keywords)}个关键词")
        print("=" * 70)

        data, md = await generate_batch_quote(keywords, "全域上榜", 0.25)

        print(f"\n汇总:")
        for tier_name, tier_sum in data["tier_summaries"].items():
            print(f"   {tier_name}: {tier_sum['final_price']:,}元/月 "
                  f"({tier_sum['total_articles']}篇)")

        print(f"\n关键词明细:")
        for kw in data["keywords"]:
            print(f"   {kw['keyword']}: {kw['selling_price']}元 "
                  f"(难度{kw['difficulty_score']}, 价值{kw['value_score']}, "
                  f"意图={kw['intent']}, 竞品={kw['competitor_count']})")

        with open("batch_quote_v2.md", "w", encoding="utf-8") as f:
            f.write(md)
        print("\n报价单已保存到 batch_quote_v2.md")

    asyncio.run(main())
