"""
pricing_bands(v2.0 · 2026-06-11)— 算价业务定义 SSOT + 兜底公式 thin wrapper

【v2.0 老板拍 一次性重构】
  - 删 6 类填不满硬编码字典(_FACTORY_BANDS_STANDARD / _COMPETITION_POSITION_STEPS / _COMPETITION_DOWNGRADE /
    _BAND_POSITION_W_COMP/VALUE / _VALUE_SCORE_MIN/MAX / _DEFAULT_BAND_TYPE)
  - 删配套函数(_get_factory_bands / resolve_factory_band / apply_factory_band / _competition_position /
    _value_position / compute_band_position / discretize_competition / _article_competition)
  - 主算价路径已切 pricing_llm_assessor.assess_keyword_pricing(双 LLM 互验 + 6 道护栏)

【保留业务定义类(老板铁律 · v2 不动)】
  - _TIER_BAND_COEFF 0.65/1.0/1.45/1.90:套餐缩放系数(老板 2026-06-05 拍)
  - MIN_ARTICLES_DEFAULT = 5:占位实操铁律(所有词 ≥ 5 篇 · 含 brand_owned)
  - ARTICLE_COMPETITION_CAP = 100:metaso 物理上限
  - _SUPER_RED_OCEAN_RATIO = 0.90 + detect_super_red_ocean:超红海判定(老板拍 · LLM 评估师调用)
  - tier_key_from_target_share:出现率档 → tier_key 映射(套餐 SOV 定义)

【兜底公式(batch_pricing.recalculate_for_tier / batch_pricing_llm 用)】
  - compute_banded_price:成本地板 + (value × difficulty)^0.7 + tier 缩放 + markup 末位 · 无 band 钳(_FACTORY_BANDS_STANDARD 删)
  - compute_required_articles_banded:篇数算法 max(5, ceil(ts × C / (1 - ts)))

红线:不碰 billing.py / 红线 4 文件 baseline · 失败 fail-soft 兜底不阻塞算价。
"""
from __future__ import annotations

import logging
import math
from typing import Optional

logger = logging.getLogger("GEO-PricingBands")

# v2 算价口径版本号(全仓 SSOT · assessor / cache 失效判定 / recalc 分流都用此)
# v2.2_2026-06-11:+价值系数层(高客单决策词溢价 ×[1.0,1.5] · 示例上限)
# v2.1_2026-06-11:成本驱动架构(价 = 篇数 × 单篇成本 × 服务商系数 · LLM 只判物理量)
CURRENT_PRICING_FORMULA_VERSION = "v2.2_2026-06-11"

# ============================================================
# 业务定义类(老板铁律 · v2 保留)
# ============================================================

# 三档(+内部 strong 霸榜档)band 缩放系数 · 入门=标准×0.65 / 旗舰=×1.45 / strong=×1.90
# ⚠️ [v2.1 口径变化 · 老板知情] v2 主路径不再用此固定系数:三档差异由篇数阶梯(V2_TIER_MIN_ARTICLES
#   5/7/10 + 出现率公式)驱动 · 档间比随竞争浮动(低竞争 0.71:1:1.43 / 高竞争 0.56:1:1.48)。
#   本系数仅 legacy 兜底路径(compute_banded_price · 旧缓存词)仍在用。
_TIER_BAND_COEFF = {
    "entry":    0.65,
    "standard": 1.0,
    "flagship": 1.45,
    "strong":   1.90,
}

# 所有词 ≥ 5 篇打底(老板拍 · 占位实操铁律 · 1-2 篇无效)
MIN_ARTICLES_DEFAULT = 5

# 真实竞争封顶 = metaso 搜索物理上限 100
ARTICLE_COMPETITION_CAP = 100

# 超红海判定阈值(老板 2026-06-06 拍)· 由 detect_super_red_ocean 使用
_SUPER_RED_OCEAN_RATIO = 0.90
_SUPER_RED_OCEAN_MIN_SAMPLE = 10
# [2026-06-16 老板修] hard red 触发的竞品/有效竞争下限(接近 metaso 物理上限 100 · 普通套餐真打不动才深度报价)。
_SUPER_RED_OCEAN_HARD_COUNT = 90


# ============================================================
# tier_key 映射(套餐 SOV 定义)
# ============================================================

def tier_key_from_target_share(target_share: float) -> str:
    """target_share → tier_key · 系统固定 0.10(entry)/ 0.20(standard)/ 0.30(flagship)/ 0.50(strong)"""
    try:
        ts = float(target_share)
    except (TypeError, ValueError):
        return "standard"
    if ts <= 0.15:
        return "entry"
    if ts <= 0.25:
        return "standard"
    if ts <= 0.40:
        return "flagship"
    return "strong"


# ============================================================
# 超红海判定(老板拍 · LLM 评估师调用)
# ============================================================

def detect_super_red_ocean(competition_count, content_count,
                           search_cap: int = ARTICLE_COMPETITION_CAP,
                           effective_competition: int = 0) -> dict:
    """超级红海词判定(纯函数 · 数据现有 · 零新增测量)。

    [2026-06-16 老板修 · 防长句商业搜索项被误标"需深度报价"]
      黄档(yellow · 高饱和风险·仅内部观测):metaso 竞品占比 ≥ 0.90。**不再返回 super_red_ocean=true**。
      红档(red · super_red_ocean=true · 剥离套餐 + 需深度报价):占比 ≥ 0.90 **且**
        (竞品数 competition_count ≥ 90 **或** 有效竞争 effective_competition ≥ 90)。
        —— 即真正普通套餐打不动的词(竞争接近 metaso 物理上限)才深度报价;
           单纯占比高 / 小样本高占比 / 单档超限,都不剥离套餐。
      小样本护栏:content_count < _SUPER_RED_OCEAN_MIN_SAMPLE 不判。
      competition_ratio 观测字段保留(供内部复盘)。
    """
    try:
        comp = max(int(competition_count or 0), 0)
    except (TypeError, ValueError):
        comp = 0
    try:
        raw = max(int(content_count or 0), 0)
    except (TypeError, ValueError):
        raw = 0
    try:
        eff = max(int(effective_competition or 0), 0)
    except (TypeError, ValueError):
        eff = 0
    if raw <= 0:
        return {"super_red_ocean": False, "super_red_ocean_level": "none",
                "competition_ratio": 0.0, "hit_search_cap": False}
    ratio = min(comp / raw, 1.0)
    hit_cap = raw >= search_cap
    sufficient = raw >= _SUPER_RED_OCEAN_MIN_SAMPLE
    is_yellow = sufficient and ratio >= _SUPER_RED_OCEAN_RATIO
    # [2026-06-16 修] 只有占比达标【且】竞品/有效竞争接近物理上限才 hard red。
    #   yellow 仅高饱和风险观测,super_red_ocean=False → 不剥离套餐、不深度报价
    #   (防"深圳小户型法式装修公司推荐"这类长句商业项仅因召回同行占比高被误伤)。
    is_red = is_yellow and (comp >= _SUPER_RED_OCEAN_HARD_COUNT or eff >= _SUPER_RED_OCEAN_HARD_COUNT)
    level = "red" if is_red else ("yellow" if is_yellow else "none")
    return {
        "super_red_ocean": is_red,
        "super_red_ocean_level": level,
        "competition_ratio": round(ratio, 3),
        "hit_search_cap": hit_cap,
    }


# ============================================================
# 【v2.0 成本驱动定价 SSOT】(2026-06-11 老板拍商业逻辑)
#   客户价 = 篇数 × 单篇成本 × 服务商系数
#   篇数 = 竞争决定(可推理) · 单篇成本 = 竞品媒体档次决定(可测) · 系数 = 服务商自设(利润透明)
#   score_keywords 与 recalculate_for_tier 共用本函数 · 单点口径(防两份公式拷贝打架)
# ============================================================

# 三档目标出现率(套餐 SOV 定义 · 老板拍)
V2_TIER_TARGET_SHARE = {
    "entry":    0.10,
    "standard": 0.20,
    "flagship": 0.30,
    "strong":   0.50,   # 内部霸榜档 · 非前台套餐
}

# 三档篇数底线(所有词 ≥5 老板铁律 · 5/7/10 阶梯保低竞争词三档仍有差异)
V2_TIER_MIN_ARTICLES = {
    "entry":    5,
    "standard": 7,
    "flagship": 10,
    "strong":   12,
}


# ============================================================
# 【篇数唯一取数出口 · P1 容量合同 2026-08-08】
#
# 语义(本包确立 · 对齐 GEO 缺口作战计划设计合同 §6.1):
#   报价冻结的 required_articles / tier articles = **可交付容量上限**,不是"必须完成量"。
#   交付允许 0..capacity;用不满不算失败,也**不得为了把进度做成 100% 而制造文章**。
#   不足额要带原因回流 → services/article_capacity_contract.py 的 SHORTFALL_REASONS。
#
# 元判据(tests/test_article_count_meta_lock_2026_08_08.py):
#   **本文件是全仓唯一允许出现「按 entry/standard/flagship 分档的篇数字面量」的地方。**
#   其他任何 .py / .ts / .tsx 里出现同形状阶梯 = 红。别处要用篇数,一律调下面的取数函数。
# ============================================================

# 阶梯档案名(取数函数的 ladder 形参只认这两个)
ARTICLE_LADDER_MAIN = "v2_contract"
ARTICLE_LADDER_BRAND_KEYWORD = "brand_keyword"
ARTICLE_LADDER_LEGACY_FALLBACK = "legacy_1_2_3"

# ⏳ 【待 Owner 拍板】品牌词是否豁免主合同阶梯。
#   当前值 = 2026-08-08 生产现值(tools/batch_pricing._make_brand_keyword_price 原写死的 1/2/3),
#   收编到此处 = **零价格变化**,只是把第二份字面量并进 SSOT,等 Owner 裁定后再动数值。
#   利弊见交付单「Owner 待决 ①」。
_BRAND_KEYWORD_TIER_ARTICLES = {
    "entry":    1,
    "standard": 2,
    "flagship": 3,
    "strong":   3,
}

# 🔴 【上线前需 Owner 知情】Q2.b 数据断供/兜底词的**上线前口径**(api/selection_api 原写死的 1/2/3)。
#   本包按工单「统一走主合同阶梯」把兜底词切到 V2_TIER_MIN_ARTICLES,
#   这条只作**一键回滚值**保留(环境变量 GEO_FALLBACK_ARTICLE_LADDER=legacy_1_2_3),
#   不再是第二条活阶梯。
_LEGACY_FALLBACK_TIER_ARTICLES = {
    "entry":    1,
    "standard": 2,
    "flagship": 3,
    "strong":   3,
}

ARTICLE_LADDERS = {
    ARTICLE_LADDER_MAIN: V2_TIER_MIN_ARTICLES,
    ARTICLE_LADDER_BRAND_KEYWORD: _BRAND_KEYWORD_TIER_ARTICLES,
    ARTICLE_LADDER_LEGACY_FALLBACK: _LEGACY_FALLBACK_TIER_ARTICLES,
}

# 历史遗留的「缺失容量默认值」。
#   落库层(db.diagnosis_db.save_confirmed_keywords)与 tier_data 缺 articles 时的现网行为就是 1。
#   本包**不改这个数值**(改它 = 改已冻结报价的容量 = 对外价格语义变化,不自裁),
#   只是把散在 9 处的 `or 1` / `, 1)` 收敛到这一个具名常量,并让显式 0 不再被吃掉。
LEGACY_MISSING_CAPACITY_DEFAULT = 1

_FALLBACK_LADDER_ENV = "GEO_FALLBACK_ARTICLE_LADDER"


def tier_article_capacity(tier_key, *, ladder: str = ARTICLE_LADDER_MAIN) -> int:
    """档位 → **容量上限**篇数(全仓唯一取数出口)。

    非法 tier_key 一律回落 standard 档(与 compute_v2_tier_price 的 ts 默认同档,
    否则「价按 standard 算、篇数按别的档算」= 口径裂)。
    非法 ladder 回落主合同(fail-safe:宁可给主合同阶梯,不静默给 legacy 低篇数)。
    """
    table = ARTICLE_LADDERS.get(str(ladder), V2_TIER_MIN_ARTICLES)
    value = table.get(str(tier_key))
    if value is None:
        value = table.get("standard", V2_TIER_MIN_ARTICLES["standard"])
    return int(value)


def fallback_tier_article_capacity(tier_key) -> int:
    """数据断供 / batch_pricing 过滤丢词(Q2.b)兜底词的档位容量。

    【2026-08-08 P1】统一走主合同阶梯 —— 原写死的 1/2/3 违反老板铁律
    「所有词 ≥5 篇 · 1-2 篇无效」:按 1 篇卖出去的词交付上限就是 1 篇,本身不成立。

    🔴 对外价格影响(交付单已列,上线前需 Owner 知情):兜底词单价 = max(篇数 × 单篇成本 × 系数,
       MIN_KEYWORD_PRICE)。篇数 1/2/3 → 5/7/10 会把兜底词价格抬上去。
       一键回滚:环境变量 GEO_FALLBACK_ARTICLE_LADDER=legacy_1_2_3(恢复上线前口径,无需改代码)。
    """
    import os

    mode = str(os.getenv(_FALLBACK_LADDER_ENV) or "").strip() or ARTICLE_LADDER_MAIN
    if mode not in ARTICLE_LADDERS:
        mode = ARTICLE_LADDER_MAIN
    return tier_article_capacity(tier_key, ladder=mode)


def normalize_article_capacity(raw, *, when_missing: int) -> int:
    """把任意来源的篇数字段规成合法容量上限。

    - None / 空串 / 不可解析 → when_missing(**调用方必须显式给**,不给默认值是故意的:
      「缺失时算几篇」是业务决定,不该由本函数替谁拍板)
    - 显式 0 **必须保留**(覆盖词 is_core=False 就是 0 篇 · 生产实测 218 行)。
      这条是 `or 1` 写法的真 bug:它把显式 0 当假值吃掉,静默给出 1 篇容量。
    - 负数 → 0(容量不能为负)
    """
    if raw is None:
        return max(0, int(when_missing))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return max(0, int(when_missing))
    return max(0, value)

# 单篇成本物理边界(transparent_pricing.MEDIA_TIER_COSTS:自媒体 ¥35 底 / 央媒 ¥350 顶)
V2_COST_PER_ARTICLE_MIN = 35.0
V2_COST_PER_ARTICLE_MAX = 350.0

# 价值系数(v2.2 · 老板 2026-06-11 拍):高客单决策词在成本之上的价值溢价
#   LLM 判 value_signal 0-3(双 LLM 取均值)→ 乘数 = 1 + signal × 0.25 · 钳 [1.0, 1.5]
#   信息词/低价值 ≈ ×1.0 不动 · 别墅电梯类高客单决策词(2.8)≈ ×1.7
#   有界:最贵 = 50 篇 × ¥350 × 1.75 = ¥30,625 出厂(超红海词必 needs_review 不出保证价)
V2_VALUE_MULT_PER_SIGNAL = 0.25
V2_VALUE_MULT_MAX = 1.5


def value_multiplier_from_signal(value_signal) -> float:
    """value_signal(0-3)→ 价值乘数 [1.0, 1.5] · 脏值/缺失 → 1.0(不溢价 · fail-safe)"""
    try:
        vs = float(value_signal)
        if not math.isfinite(vs):
            return 1.0
    except (TypeError, ValueError):
        return 1.0
    vs = min(max(vs, 0.0), 3.0)
    return round(min(1.0 + vs * V2_VALUE_MULT_PER_SIGNAL, V2_VALUE_MULT_MAX), 3)


def compute_v2_tier_price(
    true_competition,
    cost_per_article: float,
    tier_key: str,
    markup_ratio: float,
    value_multiplier: float = 1.0,
) -> dict:
    """【v2 单档价 SSOT】出厂价 = 篇数 × 单篇成本(markup=1 即成本价)· 客户价 = × 服务商系数末位。

    输入钳(护栏即物理边界 · 价格被天然封顶 · 无需价格层天花板):
      - true_competition ∈ [1, ARTICLE_COMPETITION_CAP=100](metaso 物理上限)
      - cost_per_article 只设下限 > 0 · 上限 [35, 350] 物理钳由调用方按来源施加:
        LLM 建议 / 系统动态 → 钳 [35, 350](防幻觉);服务商报价方案自设成本 → 原样生效
        (老板 2026-06-11 拍:设置即固定按此 · 医疗/央媒类真实成本可 > 350 · 钳它 = 报价 < 真实成本 = 亏)
      → LLM 路径最贵可能:flagship 50 篇 × ¥350 = 出厂 ¥17,500(50 篇央媒本来就值这个钱)

    价格层不设钳:出厂价 == 真实成本,任何向下钳价 = 卖价 < 成本 = 亏(2026-06-07 大词 ceiling 亏本教训)。
    """
    ts = V2_TIER_TARGET_SHARE.get(tier_key, 0.20)
    # 非法 tier_key 默认按 standard 档(ts 0.20 配 7 篇底线 · 两套默认必须同档否则口径裂)
    # [P1 2026-08-08] 走唯一取数出口,别处不许再读 V2_TIER_MIN_ARTICLES 原表
    min_articles = tier_article_capacity(tier_key)
    # C 解析:支持数字字符串("80.5")· NaN/inf/非法 → 1(int(float(nan/inf)) 自身 raise 被捕)· 钳 [1, 100]
    try:
        c = int(float(true_competition))
    except (TypeError, ValueError, OverflowError):
        c = 1
    c = min(max(c, 1), ARTICLE_COMPETITION_CAP)
    # cost 解析:NaN/非法/≤0 → 35 底
    try:
        cost = float(cost_per_article)
        if not math.isfinite(cost) or cost <= 0:
            cost = V2_COST_PER_ARTICLE_MIN
    except (TypeError, ValueError):
        cost = V2_COST_PER_ARTICLE_MIN
    # markup 解析:None/NaN/非法/≤0 → 1.0(出厂价兜底 · 绝不让 SSOT 纯函数 raise)
    try:
        markup = float(markup_ratio)
        if not math.isfinite(markup) or markup <= 0:
            markup = 1.0
    except (TypeError, ValueError):
        markup = 1.0
    # 价值乘数钳 [1.0, 1.5](v2.2 · 脏值 → 1.0 不溢价)
    try:
        vm = float(value_multiplier)
        if not math.isfinite(vm):
            vm = 1.0
    except (TypeError, ValueError):
        vm = 1.0
    vm = min(max(vm, 1.0), V2_VALUE_MULT_MAX)

    articles = max(min_articles, math.ceil(ts * c / (1 - ts)))
    factory_price = round(articles * cost * vm, 2)   # ≥ 篇数×成本(vm≥1 永不亏)
    selling_price = max(1, int(factory_price * markup))
    return {
        "articles": articles,
        "factory_price": factory_price,    # 出厂层 markup=1(含价值溢价 · 即服务商进货成本)
        "selling_price": selling_price,    # 客户价 = 出厂 × 服务商系数
        "true_competition_used": c,
        "cost_per_article_used": round(cost, 1),
        "value_multiplier_used": vm,
    }


def compute_blowup_guards(*, tiers: dict, keyword_type: str, value_mult: float,
                          national_unclamped: bool, p0a_on: bool, p0b_relaxed: bool,
                          p0c_on: bool, cfg: Optional[dict] = None,
                          cost: Optional[float] = None,
                          true_competition: Optional[float] = None,
                          baseline: Optional[dict] = None,
                          p0d_on: bool = False,
                          trust_ratio: Optional[float] = None) -> dict:
    """[完整修复 Stage3 · v2.3 DELTA 2/3 加 ratio 观测] 联合爆价护栏(锁【叠乘】· 不锁成本单独上抬 · 老板审核 D3)。

    纯函数。返回 {needs_review, guarantee_unavailable, no_cache, guards[, ratios]}。
    - 成本单独上抬(P0-A 公共因子)是【预期纠偏】不单独触发(否则灰度期全火词误报淹没队列);
    - 入门档售价硬阈值:entry 售价 > 全国 ¥15000 / 地域 ¥10000 → 剥保证价 + review;
      只有最低可售方案都超限时才给「参考价·待人工核」,标准/旗舰单档偏高不隐藏入门价;
    - 单项 flagship selling > ¥10000 → no_cache(不污染全局共享缓存);
    - P0-C 灰度期(p0c_on)national_unclamped 词 → review 审计,但不因全国词本身剥保证价。
    阈值全 pricing_config 热调。

    [v2.3 DELTA 2/3] 当传入 baseline(= flag-off 基线 {cost, value_mult, true_competition, factory_price})
      且 cost/true_competition(flag-on 新值)齐备时,把 §10 的 ratio 口径【落成代码】:
        cost_ratio / value_ratio / comp_ratio / factory_ratio(新值 / flag-off 基线)→ 写入 ratios 输出。
      并消费三个此前【定义却未消费】的 pricing_config 阈值:
        - lever_two(默认 1.5):{cost,value,comp} 任两 ratio > 阈 → needs_review(cost 单独不触发=老板 D3 口径);
        - lever_three(默认 1.3):任三 ratio > 阈 → needs_review(保留报价,不剥保证价);
        - national_unclamped_factory_ratio(默认 2.0 · DELTA 2):national_unclamped 词 factory_ratio > 阈 → needs_review
          (仅 national 专项 · 永不误伤 P0-A 正常成本回真)。
      不传 baseline → 保持 v2.2 现状布尔 lever_compound 逻辑(向后兼容 · 现有调用/测试零变化)。

    [P0-D 2026-06-14] p0d_on 纳入早返 gate(P0-D 单开也算 ratio + 让②入门价阈值兜住篇数上调后的价);
      trust_ratio(信任资产难度因子)仅作第 4 观测 ratio 写入 ratios,【不进】lever_two/three 计数(设计 §5.2)。
    """
    # 四 flag 全关 = 现状(v2.2)· 护栏不介入 = 0 行为变化
    #   [P0-D] p0d_on 也纳入:P0-D 单开时仍需算 ratio(comp/trust)+ 让②绝对天花板兜住篇数上调后的价。
    if not (p0a_on or p0b_relaxed or p0c_on or p0d_on):
        return {"needs_review": False, "guarantee_unavailable": False, "no_cache": False, "guards": {}}
    if cfg is None:
        try:
            from config.pricing_config import get_blowup_guard_config
            cfg = get_blowup_guard_config()
        except Exception:
            cfg = {"factory_ceiling_niche": 6500.0, "factory_ceiling_local": 3500.0,
                   "entry_selling_ceiling_niche": 15000.0, "entry_selling_ceiling_local": 10000.0,
                   "no_cache_selling": 10000.0, "lever_two": 1.5, "lever_three": 1.3,
                   "national_unclamped_factory_ratio": 2.0}
    guards: dict = {}
    needs_review = False
    guarantee_unavailable = False
    no_cache = False

    flagship_factory = float((tiers.get("flagship") or {}).get("factory_price", 0) or 0)

    # ① 叠乘判定:baseline 齐备 → ratio 口径(§10·DELTA 3);否则 → v2.2 现状布尔(向后兼容)
    ratios: Optional[dict] = None
    if baseline is not None and cost is not None and true_competition is not None:
        def _ratio(new_v, base_v) -> Optional[float]:
            try:
                bv = float(base_v)
                if bv <= 0:
                    return None        # 防除零(flag-off 基线为 0 的极端词 · 不产 ratio)
                return round(float(new_v) / bv, 4)
            except (TypeError, ValueError):
                return None
        cost_ratio = _ratio(cost, baseline.get("cost"))
        value_ratio = _ratio(value_mult, baseline.get("value_mult"))
        comp_ratio = _ratio(true_competition, baseline.get("true_competition"))
        factory_ratio = _ratio(flagship_factory, baseline.get("factory_price"))
        ratios = {"cost_ratio": cost_ratio, "value_ratio": value_ratio,
                  "comp_ratio": comp_ratio, "factory_ratio": factory_ratio}
        # [P0-D] trust_ratio 作第 4 观测 ratio · 仅观测(默认不进 lever 计数·设计 §5.2):
        #   篇数侧难度因子(缺背书 >1 / 背书足 <1)· 上限 max_uplift=1.25 远 < lever_two=1.5 不触叠乘。
        if trust_ratio is not None:
            ratios["trust_ratio"] = round(float(trust_ratio), 4)
        lever_two = float(cfg.get("lever_two", 1.5))
        lever_three = float(cfg.get("lever_three", 1.3))
        levers = [r for r in (cost_ratio, value_ratio, comp_ratio) if r is not None]
        n_two = sum(1 for r in levers if r > lever_two)
        n_three = sum(1 for r in levers if r > lever_three)
        # 任两杠杆 > lever_two → review(cost 单独涨=预期纠偏·需≥2 杠杆才算危险叠乘)
        if n_two >= 2:
            needs_review = True
            guards["lever_two_compound"] = {"threshold": lever_two,
                                            "cost_ratio": cost_ratio, "value_ratio": value_ratio,
                                            "comp_ratio": comp_ratio}
        # 任三杠杆 > lever_three → review。是否剥保证价统一交给②入门档售价阈值。
        if n_three >= 3:
            needs_review = True
            guards["lever_three_compound"] = {"threshold": lever_three,
                                              "cost_ratio": cost_ratio, "value_ratio": value_ratio,
                                              "comp_ratio": comp_ratio}
        # DELTA 2:P0-C 全国词专项 —— national_unclamped 词 factory_ratio 超专项阈 → review。
        #   ⚠️ 必须 p0c_on gate(§9.1:此阈值只用于 P0-C 全国词专项 · 绝不误伤 P0-A 正常成本回真:
        #   P0-A 单开时 national_unclamped 词 factory_ratio 因 cost 回真上升是预期,不该被此专项拦)。
        nat_fr_thresh = float(cfg.get("national_unclamped_factory_ratio", 2.0))
        if p0c_on and national_unclamped and factory_ratio is not None and factory_ratio > nat_fr_thresh:
            needs_review = True
            guards["national_unclamped_factory_ratio"] = {"factory_ratio": factory_ratio,
                                                          "threshold": nat_fr_thresh}
    else:
        # v2.2 现状:value 与 competition 同时放大(成本公共因子不计入·D3)
        uplift = []
        if p0b_relaxed and value_mult > 1.0:
            uplift.append("value")
        if national_unclamped:
            uplift.append("competition")
        if len(uplift) >= 2:
            needs_review = True
            guards["lever_compound"] = uplift

    # ② 最低可售档硬阈值 — universal backstop · 与 flag 无关
    #    老板 2026-06-16 拍:不能因标准/旗舰单档偏高就把整项变成「参考价·待人工核」。
    #    只有入门档售价都超过阈值,才说明所有可售方案都不适合自动承诺。
    is_national = keyword_type in ("national_niche", "national_head")
    entry_selling = float((tiers.get("entry") or {}).get("selling_price", 0) or 0)
    entry_ceiling = float(cfg.get("entry_selling_ceiling_niche" if is_national else "entry_selling_ceiling_local",
                                  15000.0 if is_national else 10000.0))
    if entry_selling > entry_ceiling:
        guarantee_unavailable = True
        needs_review = True
        guards["entry_price_ceiling"] = {"entry_selling": entry_selling,
                                         "ceiling": entry_ceiling,
                                         "national": is_national}

    # ③ 搜索项 flagship selling 过高 → 不进共享缓存(防污染 (industry,city,keyword) 全局锁)
    flagship_selling = float((tiers.get("flagship") or {}).get("selling_price", 0) or 0)
    if flagship_selling > float(cfg.get("no_cache_selling", 10000.0)):
        no_cache = True
        guards["no_cache_high_selling"] = flagship_selling

    # ④ D4:P0-C national_unclamped 词仍进审计,但不因全国词本身剥保证价。
    #   「参考价·待人工核」只由 ② 入门档售价硬阈值触发。
    if p0c_on and national_unclamped:
        needs_review = True
        guards["national_unclamped_review"] = True

    out = {"needs_review": needs_review, "guarantee_unavailable": guarantee_unavailable,
           "no_cache": no_cache, "guards": guards}
    if ratios is not None:
        out["ratios"] = ratios
    return out


# ============================================================
# 篇数算法(老板拍 · 所有词 ≥ 5)
# ============================================================

def compute_required_articles_banded(
    effective_competition,
    target_share: float,
    data_source: Optional[str] = None,
    keyword_type: Optional[str] = None,
) -> int:
    """篇数 = max(5, ceil(ts × C / (1 - ts)))· C 封顶 100 · llm_estimate 保守 0.85x"""
    try:
        c = max(int(effective_competition or 1), 1)
    except (TypeError, ValueError):
        c = 1
    c = min(c, ARTICLE_COMPETITION_CAP)
    if data_source == "llm_estimate":
        c = max(1, int(c * 0.85))
    ts = float(target_share)
    if ts >= 1:
        ts = 0.9
    if ts <= 0:
        ts = 0.1
    required = ts * c / (1 - ts)
    return max(MIN_ARTICLES_DEFAULT, math.ceil(required))


# ============================================================
# 兜底公式(简化 compute_banded_price · 无 band 钳)
# ============================================================

def compute_banded_price(
    required_articles: int,
    cost_per_article: float,
    markup_ratio: float,
    value_score: float,
    difficulty_score: float,
    keyword_type: Optional[str] = None,
    tier_key: str = "standard",
    effective_competition=None,
) -> dict:
    """[v2.0 兜底公式]单词价 = max(成本地板, ceil(篇数 × cost × (v×d)^0.7 × tier_coef × markup))

    主算价路径已切 pricing_llm_assessor(双 LLM + 6 护栏)· 本函数仅给:
      - batch_pricing.recalculate_for_tier 兜底(scored 无 v2 字段时)
      - batch_pricing_llm 篇数取 max 时调用

    返回字段对老调用方完全兼容(selling_price/raw_price_before_band/band_min/band_max/
    needs_review/combined_factor/band_position/cost_floor/cost_floor_applied)。
    """
    try:
        v = max(float(value_score or 1.0), 0.5)
        d = max(float(difficulty_score or 1.0), 0.5)
    except (TypeError, ValueError):
        v, d = 1.0, 1.0
    combined = (v * d) ** 0.7
    tier_coef = _TIER_BAND_COEFF.get(tier_key, 1.0)

    articles = max(int(required_articles), 1)
    cost = float(cost_per_article)
    markup = float(markup_ratio)

    factory_raw = articles * cost * combined * tier_coef
    selling_price = max(1, int(factory_raw * markup))

    cost_floor = math.ceil(articles * cost * markup)
    cost_floor_applied = cost_floor > selling_price
    if cost_floor_applied:
        selling_price = cost_floor

    return {
        "selling_price": selling_price,
        "needs_review": False,
        "band_min": 0,
        "band_max": 0,
        "raw_price_before_band": round(factory_raw, 2),
        "combined_factor": round(combined, 3),
        "band_position": None,
        "cost_floor": cost_floor,
        "cost_floor_applied": cost_floor_applied,
    }
