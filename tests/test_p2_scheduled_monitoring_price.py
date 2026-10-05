"""[BUG-P2] 定时监测扣费三方打架(scheduler 硬编码 38 vs feature_pricing 130 vs seed False/prod true)· 静态守护

根因:scheduler scheduled_monitoring total_cost=kw_count*38,但 freeze_points 内实扣 = cost_points(130)
+ extra_cost = 130 + 38*(kw-1)(10 词实冻 472 vs 文案宣称 380,多扣 24%);管理员调 feature_pricing 后
硬编码 38 与通知金额都不变 → 调价即错账。且 seed requires_paid_points=False 与 prod=true 打架
(bonus 用户监测被静默暂停 + 402 文案误导"媒体发布")。
修:① scheduler 单价从 feature_pricing 实时取(total_cost=kw*price,extra_cost=total_cost-price,文案用 total_cost);
   ② seed requires_paid_points=true 与 prod 统一。
注:402 文案按 feature 区分(监测≠媒体发布)在 billing 红线,单独 followup。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scheduler_price_from_db():
    src = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")
    assert 'get_feature_pricing("scheduled_monitoring")' in src, "监测单价须从 feature_pricing 实时取"
    assert "_mon_price" in src
    assert "total_cost - _mon_price" in src, "extra_cost 须以 DB 单价为基(freeze 内再加 1 份)"


def test_seed_requires_paid_true():
    src = (ROOT / "db" / "wallet_db.py").read_text(encoding="utf-8")
    assert "('scheduled_monitoring', '定时监测（按次扣费）', 130, 1.0, True)" in src, \
        "scheduled_monitoring seed requires_paid_points 须 = True(与 prod 一致)"
