"""客户优先级排程算法 · CTO-15.18 PM 干预 D.7

老板红线(2026-04-28):
- "现在最紧急"为什么是这个客户 · hover 显示算法权重
- 代理可以看权重 + 公式 hover 解释(Q18 老板裁决:给代理看 / 不给客户看)
- 不给 C 端客户看(代理后台专属)

算法权重(透明给代理):
  1. 资料完整度 · 25%(完整度 < 60% 加分 · 推动代理补资料)
  2. 距上次跟进 · 20%(>= 7 天加分 · 防客户冷掉)
  3. 客户阶段权重 · 30%(stage 4 报价中 / stage 9 续费窗 高分 · stage 1 询价 中分)
  4. 续费窗距离 · 15%(<= 21 天加分 · 防错过续费窗)
  5. 异常监测 · 10%(有异常加分 · 推动代理处理)

输出:
  {
    score: 0-100,
    rank: 1-N,
    factors: [{name, score, weight, reason}],
    recommendation: "为什么这个客户最紧急的一句话",
  }
"""
from __future__ import annotations
from typing import Any
from datetime import datetime, timedelta


# 权重配置(总和 = 1.0)
PRIORITY_WEIGHTS: dict[str, float] = {
    "completeness": 0.25,
    "follow_up_gap": 0.20,
    "stage_weight": 0.30,
    "renewal_window": 0.15,
    "anomaly": 0.10,
}

# Stage 权重(0-100)· 阶段越紧迫分越高
STAGE_PRIORITY_SCORE: dict[int, int] = {
    1: 40,   # 询价 · 中等紧迫
    2: 50,   # 诊断中 · 等结果
    3: 65,   # 待报价 · 该出方案了
    4: 80,   # 报价中 · 该催签约
    5: 30,   # 写作 · 进行中 · 不太紧迫
    6: 20,   # 投放 · 进行中
    7: 25,   # 监测 · 进行中
    8: 35,   # 报告 · 该发月报
    9: 90,   # 续费 · 最紧迫(错过 = 流失)
}


def compute_priority(
    *,
    completeness_score: int = 0,
    last_follow_up_days_ago: int | None = None,
    current_stage: int = 1,
    service_end_date: datetime | None = None,
    has_anomaly: bool = False,
) -> dict[str, Any]:
    """计算客户优先级评分(0-100)

    Args:
        completeness_score: 资料完整度(0-100)
        last_follow_up_days_ago: 距上次跟进天数 · None 表示从未跟进
        current_stage: 当前 stage(1-9)
        service_end_date: 服务期结束日期
        has_anomaly: 是否有监测异常

    Returns:
        {score, factors, recommendation}
    """
    factors = []

    # 因子 1:资料完整度 · 越低越紧迫(< 60% 加分)
    completeness_factor = max(0, 60 - completeness_score) / 60 * 100
    factors.append({
        "name": "资料完整度",
        "score": round(completeness_factor),
        "weight": PRIORITY_WEIGHTS["completeness"],
        "reason": f"资料 {completeness_score}/100 · {'< 60% 需要补资料' if completeness_score < 60 else '已达标'}",
    })

    # 因子 2:距上次跟进 · 越久越紧迫
    if last_follow_up_days_ago is None:
        gap_factor = 70  # 从未跟进 · 中高分
        gap_reason = "从未跟进 · 该联系一下"
    elif last_follow_up_days_ago >= 14:
        gap_factor = 95
        gap_reason = f"{last_follow_up_days_ago} 天没跟 · 客户可能冷了"
    elif last_follow_up_days_ago >= 7:
        gap_factor = 75
        gap_reason = f"{last_follow_up_days_ago} 天没跟 · 该跟进了"
    elif last_follow_up_days_ago >= 3:
        gap_factor = 40
        gap_reason = f"{last_follow_up_days_ago} 天没跟 · 节奏正常"
    else:
        gap_factor = 10
        gap_reason = f"刚跟过 {last_follow_up_days_ago} 天前"
    factors.append({
        "name": "距上次跟进",
        "score": gap_factor,
        "weight": PRIORITY_WEIGHTS["follow_up_gap"],
        "reason": gap_reason,
    })

    # 因子 3:客户阶段权重
    stage_score = STAGE_PRIORITY_SCORE.get(int(current_stage), 30)
    stage_label_map = {1: "询价", 2: "诊断中", 3: "待报价", 4: "报价中", 5: "写作", 6: "投放", 7: "监测", 8: "报告", 9: "续费"}
    factors.append({
        "name": "客户阶段",
        "score": stage_score,
        "weight": PRIORITY_WEIGHTS["stage_weight"],
        "reason": f"阶段:{stage_label_map.get(int(current_stage), '未知')}",
    })

    # 因子 4:续费窗距离
    renewal_score = 0
    renewal_reason = "—"
    if service_end_date:
        try:
            now = datetime.now(service_end_date.tzinfo) if service_end_date.tzinfo else datetime.now()
            delta_days = (service_end_date - now).days
            if delta_days <= 0:
                renewal_score = 100
                renewal_reason = f"已过期 {abs(delta_days)} 天 · 错过续费窗"
            elif delta_days <= 7:
                renewal_score = 95
                renewal_reason = f"还剩 {delta_days} 天 · 续费窗最后冲刺"
            elif delta_days <= 21:
                renewal_score = 80
                renewal_reason = f"还剩 {delta_days} 天 · 进入续费窗"
            elif delta_days <= 45:
                renewal_score = 30
                renewal_reason = f"还剩 {delta_days} 天 · 安全期"
            else:
                renewal_score = 5
                renewal_reason = f"还剩 {delta_days} 天 · 不紧"
        except Exception:
            pass
    factors.append({
        "name": "续费窗距离",
        "score": renewal_score,
        "weight": PRIORITY_WEIGHTS["renewal_window"],
        "reason": renewal_reason,
    })

    # 因子 5:异常监测
    anomaly_score = 90 if has_anomaly else 0
    factors.append({
        "name": "异常监测",
        "score": anomaly_score,
        "weight": PRIORITY_WEIGHTS["anomaly"],
        "reason": "有异常排名掉位" if has_anomaly else "监测稳定",
    })

    # 加权总分
    total = sum(f["score"] * f["weight"] for f in factors)

    # 推荐文案(找最高权重×分数因子)
    top_factor = max(factors, key=lambda f: f["score"] * f["weight"])
    recommendation = f"{top_factor['name']}:{top_factor['reason']}"

    return {
        "score": round(total),
        "factors": factors,
        "recommendation": recommendation,
    }


def explain_weights() -> dict[str, Any]:
    """暴露权重定义给前端 hover tooltip(D.7 算法明示)"""
    return {
        "weights": {
            "资料完整度": PRIORITY_WEIGHTS["completeness"],
            "距上次跟进": PRIORITY_WEIGHTS["follow_up_gap"],
            "客户阶段": PRIORITY_WEIGHTS["stage_weight"],
            "续费窗距离": PRIORITY_WEIGHTS["renewal_window"],
            "异常监测": PRIORITY_WEIGHTS["anomaly"],
        },
        "stage_priority": {str(k): v for k, v in STAGE_PRIORITY_SCORE.items()},
        "formula": "总分 = Σ(因子分 × 权重)· 0-100 · 越高越紧迫",
        "audience": "代理后台专属 · 不给 C 端客户看",
    }
