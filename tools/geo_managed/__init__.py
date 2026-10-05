"""
v3.3/v3.4 GEO 全自动托管 — 引擎模块包

模块清单:
  estimate_engine.py    估算入口（90% 复用 c_end_cost_estimate）
  reverse_calc.py       金额反推 SOV（泊松反演）
  campaign_tick.py      定时任务（每 6h 跑套餐 + pending review 自动发）
  review_engine.py      v3.4 复盘引擎（每周一凌晨）
  knowledge_reflow.py   v3.4 反哺 industry_knowledge L2

核心规则（来自 v2 决策）:
  - 充值即消费，不退款
  - 半自动模式默认 / 全托管模式可切
  - 屏蔽词连续 7 天 0 检出 → 暂停
  - 12 个月无操作 → 转赠送积分
"""

from .estimate_engine import estimate_word_plan, estimate_brand_plan
from .reverse_calc import reverse_calc_from_budget

__all__ = [
    "estimate_word_plan",
    "estimate_brand_plan",
    "reverse_calc_from_budget",
]
