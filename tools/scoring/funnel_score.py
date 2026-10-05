"""
funnel_score — GEO 漏斗 3 层加权评分模型 SSOT

CTO-G 2026-04-27 · 老板拍板新评分(继 CTO-B 设计)

定位:
  · 取代旧 5 维度评分(原 5 维有因果重叠 · 掩盖老客新客差距)
  · 漏斗 3 层(品牌认知 + 决策获客 + 场景转化)按业务影响加权
  · 6 档等级名称(主导/健康/成长/边缘/危急/隐形)让客户感受真实程度

旧 5 维度评分仍保留(scoring_levels.LEVEL_META) · 用于 v1 老报告兼容
新报告(v2 客户视角)统一走本模块

使用:
  from tools.scoring.funnel_score import calculate_funnel_score, get_funnel_meta

  result = calculate_funnel_score(
      brand_detected=4, brand_total=4,
      local_detected=5, local_total=20,
      scenario_detected=0, scenario_total=8,
  )
  # → {"total_score": 30, "level": "危急级", "color": "red", ...}
"""
from __future__ import annotations

from typing import Optional


# 6 档等级元数据(按客户感受到的严重程度命名)
FUNNEL_LEVEL_META: dict[str, dict[str, object]] = {
    "主导级": {
        "label": "主导级",
        "min": 85, "max": 100,
        "color": "emerald",
        "color_hex": "#059669",
        "badge_class": "bg-emerald-500 text-white",
        "summary": "你已经是 AI 默认推荐 · 守城为主",
        "business_meaning": "在主流 AI 引擎中你已经是行业 Top · 持续维护现有内容资产 · 关注竞品反超信号",
    },
    "健康级": {
        "label": "健康级",
        "min": 70, "max": 84,
        "color": "green",
        "color_hex": "#16a34a",
        "badge_class": "bg-green-500 text-white",
        "summary": "主流问答 AI 都给到你 · 持续优化拉高",
        "business_meaning": "主要 AI 引擎在多数行业问题上都能给到你 · 重点拉高高客单场景词命中率",
    },
    "成长级": {
        "label": "成长级",
        "min": 55, "max": 69,
        "color": "amber",
        "color_hex": "#d97706",
        "badge_class": "bg-amber-500 text-white",
        "summary": "部分关键问题给到你 · 一半流量没打透",
        "business_meaning": "AI 在你品牌词和部分本地词上能提你 · 但场景转化层薄弱 · 高决策客户拿不到",
    },
    "边缘级": {
        "label": "边缘级",
        "min": 40, "max": 54,
        "color": "orange",
        "color_hex": "#ea580c",
        "badge_class": "bg-orange-500 text-white",
        "summary": "AI 偶尔提你 · 多数新客触达不到",
        "business_meaning": "AI 在你品牌词上能命中但本地词覆盖度低 · 新客获取链路有可见劣势",
    },
    "危急级": {
        "label": "危急级",
        "min": 25, "max": 39,
        "color": "red",
        "color_hex": "#dc2626",
        "badge_class": "bg-red-500 text-white",
        "summary": "AI 几乎只在客户搜品牌名时才提你 · 新客链路基本断",
        "business_meaning": "AI 几乎只在客户主动搜你品牌名时才提到你 · 新客获取链路基本断 · 增长靠老客复购单点支撑",
    },
    "隐形级": {
        "label": "隐形级",
        "min": 0, "max": 24,
        "color": "rose",
        "color_hex": "#9f1239",
        "badge_class": "bg-rose-700 text-white",
        "summary": "AI 完全不认识你 · 增长靠老客单点支撑",
        "business_meaning": "AI 完全不认识你的品牌 · GEO 链路全断 · 必须从基础内容资产开始建设",
    },
}

_LEVELS_SORTED: list[tuple[str, int, int]] = sorted(
    [(name, int(meta["min"]), int(meta["max"])) for name, meta in FUNNEL_LEVEL_META.items()],
    key=lambda x: -x[1],  # 按 min 降序
)


# 3 层定义(漏斗权重)
FUNNEL_LAYERS = [
    {
        "key": "brand",
        "label": "品牌认知层",
        "weight": 20,
        "desc": "客户主动搜你品牌名 · AI 能答对的比例",
        "business": "底线 · 守不住老客回头都丢",
        "stats_key": "brand_awareness",  # ai_visibility_data.dimension_stats 的 key
    },
    {
        "key": "local",
        "label": "决策获客层",
        "weight": 40,
        "desc": "客户搜「行业+地区」「行业+选哪家」· AI 是否把你推进选项",
        "business": "核心 · 新客在做品牌选择时 AI 替不替你说话",
        "stats_key": "regional_industry",
    },
    {
        "key": "scenario",
        "label": "场景转化层",
        "weight": 40,
        "desc": "客户搜「具体方案/对比/避坑」等高决策意图词 · AI 是否引用你",
        "business": "天花板 · 高客单决策客户能不能拿到",
        "stats_key": "super_tier1",
    },
]


def calculate_funnel_score(
    *,
    brand_detected: int = 0, brand_total: int = 0,
    local_detected: int = 0, local_total: int = 0,
    scenario_detected: int = 0, scenario_total: int = 0,
) -> dict:
    """漏斗 3 层加权评分主入口

    Args:
        brand_detected/total: 品牌认知层(brand_awareness) 测试样本
        local_detected/total: 决策获客层(regional_industry) 测试样本
        scenario_detected/total: 场景转化层(super_tier1) 测试样本

    Returns:
        {
            "total_score": int 0-100,
            "level": str(主导/健康/成长/边缘/危急/隐形),
            "level_meta": dict(全 FUNNEL_LEVEL_META[level] 内容),
            "layers": [
                {key, label, detected, total, rate(0-1), rate_pct(0-100),
                 weight, score(该层得分), data_sufficient(bool), confidence(high/medium/low)},
                ...
            ],
        }

    数据置信度判定(老板红线 · 不假装数据完整):
        total >= 15 → high
        total >= 5  → medium
        total < 5   → low(报告里需提示数据不足)
    """
    layers_out = []
    total_score = 0.0

    samples = {
        "brand": (brand_detected, brand_total),
        "local": (local_detected, local_total),
        "scenario": (scenario_detected, scenario_total),
    }

    # [audit P2 2026-06-10] 权重重归一:某层 total=0(verbatim 自定义题分层 LLM 失败 / 该层无题)→
    # 旧版该层 score=0 但权重不重分配 → 总分被静默砍掉该层权重上限(如 scenario 0 样本 → 封顶 60 分·
    # 低估打击客户)。修:按【有样本层】的权重占比重归一到 100,使分数反映有数据层的真实表现;
    # 标 partial_sample 让报告提示"部分层无样本·按有效层折算"。
    # ⚠️ 仅在"有层 0 样本"时改变结果:三层都有样本时 effective_weight==原始 weight,正常诊断分数不变。
    effective_weight_sum = sum(
        layer["weight"] for layer in FUNNEL_LAYERS if int(samples[layer["key"]][1] or 0) > 0
    )
    has_empty_layer = any(int(samples[layer["key"]][1] or 0) == 0 for layer in FUNNEL_LAYERS)

    for layer in FUNNEL_LAYERS:
        det, tot = samples[layer["key"]]
        det = int(det or 0)
        tot = int(tot or 0)
        rate = (det / tot) if tot > 0 else 0.0
        # 重归一权重:有样本层按占比放大到 100;0 样本层不得分也不占权重
        if tot > 0 and effective_weight_sum > 0:
            eff_weight = layer["weight"] / effective_weight_sum * 100.0
        else:
            eff_weight = 0.0
        score = round(rate * eff_weight, 1)
        total_score += score

        if tot >= 15:
            confidence = "high"
            sufficient = True
        elif tot >= 5:
            confidence = "medium"
            sufficient = True
        else:
            confidence = "low"
            sufficient = False

        # [P0-5 · 2026-07-26] 「满分 + 数据不足」左右脑互搏的合并表述。
        #
        #   生产实证(驰鲸 brand 712):品牌层 4/4 命中 → 100%，同时 total=4 < 5
        #   触发 data_sufficient=False → 报告同一层里并排显示"100%"和"样本不足"。
        #   客户读到的是"你们自己都说不可信"。
        #
        #   这里不改分数、不改 data_sufficient(其他消费点靠它),只**追加**给
        #   展示层用的合并字段:命中率高但样本少 → "初步达标 · 待扩测";
        #   其余情况给一句把两件事说在一起的话。展示层禁止再单独摆一个
        #   "样本不足"角标(判别测试锁这一点)。
        provisional = bool(tot > 0 and not sufficient and rate >= 0.7)
        if tot <= 0:
            sample_note = "本层未实测"
            headline_label = None
        elif sufficient:
            sample_note = f"{det}/{tot} 命中"
            headline_label = None
        else:
            suggested = max(8, tot * 2)
            sample_note = (
                f"{det}/{tot} 命中（样本较少，建议扩测到 {suggested} 题以上确认）"
            )
            headline_label = "初步达标 · 待扩测" if provisional else None

        layers_out.append({
            "key": layer["key"],
            "label": layer["label"],
            "weight": layer["weight"],                      # 展示仍用原始权重(UI 不变)
            "effective_weight": round(eff_weight, 1),        # 重归一后实际计分权重(0 样本层=0)
            "desc": layer["desc"],
            "business": layer["business"],
            "detected": det,
            "total": tot,
            "rate": round(rate, 4),
            "rate_pct": round(rate * 100, 1),
            "score": score,
            "data_sufficient": sufficient,
            "confidence": confidence,
            # [P0-5] 展示层合并表述(additive · 老消费点不受影响)
            "sample_note": sample_note,
            "provisional": provisional,
            "headline_label": headline_label,
        })

    total_score_int = int(round(total_score))
    level = _get_funnel_level(total_score_int)

    # [audit #7 返修] 过度承诺封顶:有空样本层且【有效层权重和 < 60】(= funnel 只剩单层覆盖,
    #   如只测品牌名 → brand 权重 20 被重归一放大到 100)→ 重归一会让单层满分变成 100/100 主导级
    #   (从一个数据点宣称市场主导)。封顶:此时最高只到成长级,主导/健康 降级为成长级。
    #   partial_sample 据此被【消费】(不再是死标志);rate_label 仍提示"无样本·数据不足"。
    #   ⚠️ 双层(60/80)及三层(100)覆盖不封顶 —— 重归一在此区间是合理的折算,不低估也不夸大。
    level_capped = False
    if has_empty_layer and effective_weight_sum < 60 and level in ("主导级", "健康级"):
        level = "成长级"
        level_capped = True

    # [WO_233-c3 · Owner 2026-09-17 拍板「分范围交付」] 单层覆盖时**不出全局总分**。
    #
    # 封顶(上面那段)是 2026 年那次返修的止血:不让单层满分冒充主导级。
    # 但封顶仍然给出一个 0-100 的总分,而那个数**没有分母意义** ——
    # 只测了品牌层,把品牌层的命中率重归一成"整体 GEO 可见度",本身就是在
    # 用一层的观测替三层说话。Owner 拍板:防御结果照常交付,增长位置写「本次未测」,
    # **不出总分、不扣零分、不放大补满**。
    #
    # 🔴 判据取的是**观测到的层覆盖**,不是诊断的 mode,也不是题单来源。
    #    A 查出:用户手动删光两道增长题同样得到 off=0 —— 前端保证不了某层非空,
    #    「题单合法但某层无题」是正常输入。所以这里只问"实际有几层有样本",
    #    谁也不指望(本仓 the-signal-is-emitted-by-the-wrong-party 的同一条道理:
    #    问被服务方的观测,不问做事方声明的模式)。
    # 🔴 要求 `effective_weight_sum > 0`:**完全没有观测**那一格不走分范围交付,
    #    仍是 0 · 隐形级(与改前逐字一致)。
    #    我第一版漏了这个下界,于是"零观测"也变成不出分 —— 那是**另一件事**:
    #    零观测该不该说「隐形级」是一个口径问题(等于从零证据做一个负面陈述),
    #    Owner 这次拍的是"防御模式分范围交付",没拍它。
    #    悄悄把它一起改了,就是拿一次授权去动两件事(而且会让 c1 的反向对照失真)。
    #    已单独报 Review 定。
    scope_limited = bool(
        has_empty_layer and 0 < effective_weight_sum < 60)

    return {
        "total_score": total_score_int,
        "max_score": 100,
        "level": level,
        # partial_sample=True → 至少一层 0 样本(分数已按有效层折算)· 报告/前端据此提示数据不全
        # level_capped=True → 因覆盖过薄(单层)被封顶,未让单层满分冒充主导/健康级
        # scope_limited=True → 覆盖太薄,**总分不成立**,按分范围交付(见上方说明)
        "level_meta": {
            **FUNNEL_LEVEL_META[level], "level": level,
            "partial_sample": has_empty_layer,
            "level_capped": level_capped,
            "scope_limited": scope_limited,
        },
        "layers": layers_out,
    }


def _get_funnel_level(score: int) -> str:
    """分数 → 等级名"""
    s = max(0, min(100, int(score)))
    for name, lo, hi in _LEVELS_SORTED:
        if s >= lo:
            return name
    return "隐形级"


def get_funnel_meta(score: int) -> dict:
    """便捷调用 · 返回 {level, label, color, ...}"""
    level = _get_funnel_level(score)
    return {**FUNNEL_LEVEL_META[level], "level": level}


def render_funnel_progress_bar(rate: float, width: int = 20) -> str:
    """渲染 ASCII 进度条 · 用于 markdown 报告

    rate=0.25, width=20 → "█████░░░░░░░░░░░░░░░"
    """
    if rate is None or rate < 0:
        return "░" * width
    rate = min(1.0, rate)
    filled = round(rate * width)
    return "█" * filled + "░" * (width - filled)


def assert_funnel_score_consistency():
    """开发期断言 · 防权重漂移"""
    weight_sum = sum(int(layer["weight"]) for layer in FUNNEL_LAYERS)
    if weight_sum != 100:
        raise AssertionError(f"FUNNEL_LAYERS 权重和 {weight_sum} ≠ 100")
    return True
