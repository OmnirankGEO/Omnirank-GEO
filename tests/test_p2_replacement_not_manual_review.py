"""[BUG-P2] 部分退款 replacement frozen ledger 永久卡 manual_review_required · 静态守护

根因:V3.5 线上订单 T+3 内部分消费退款(状态 B)写一条正数 replacement frozen ledger
(=服务商应得收益),随即 UPDATE manual_review_required=TRUE。settle daily cron WHERE NOT
manual_review_required → 永不 settled;可提/可换列表又 manual_review_required=FALSE 过滤;
全仓无任何代码把 agent_revenue_ledger.manual_review_required 改回 FALSE → 服务商应得收益永久卡死。
修(老板拍):部分退款 replacement 不打 manual_review(确定性按比例算出·无需人工),正常进 T+3 settle。
(C/D clawback 的已消费损耗承接仍保留 manual_review)。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_replacement_not_flagged_manual_review():
    src = (ROOT / "api" / "referral_api.py").read_text(encoding="utf-8")
    # manual_review_required=TRUE 的 UPDATE 只剩 C/D clawback 一处(状态 B replacement 不再打)
    assert src.count("SET manual_review_required=TRUE") <= 1, \
        "状态 B replacement 仍打 manual_review → 服务商应得收益永久卡死"
    # 仍保留 C/D clawback_id 的人工承接路径
    assert "clawback_id" in src
