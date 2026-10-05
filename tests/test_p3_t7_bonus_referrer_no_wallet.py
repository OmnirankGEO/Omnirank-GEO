"""[BUG-P3] T+7 推荐奖励 settle:referrer 无 user_wallets 行时标 settled 但 0 入账,bonus 静默丢失 · 静态守护

根因:service_fee_t7_bonus_settle 先把记录置 'settled',再 UPDATE user_wallets bonus_points;
referrer 在 user_wallets 无行(注销/手工清理/历史脏数据)时 UPDATE 命中 0 行无检查,
INSERT...SELECT FROM user_wallets 同样插 0 行 → 该笔推荐奖励永久消失且账面显示已结算,无日志无告警。
修:UPDATE 后检查 rowcount==0 → 撤销 settled 标 failed + 告警留人工(勿先置 settled 后入账)。
(当前 V3_3_1_ENABLED=false 休眠 · 防开启后静默吞奖励)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_t7_bonus_checks_rowcount():
    src = (ROOT / "services" / "service_fee_cron.py").read_text(encoding="utf-8")
    s = src.find("def service_fee_t7_bonus_settle")
    assert s >= 0
    e = src.find("\nasync def ", s + 10)
    if e < 0:
        e = src.find("\ndef ", s + 10)
    func = src[s: e if e > 0 else s + 4000]
    assert "cur.rowcount == 0" in func, "UPDATE user_wallets 后须检查 rowcount(防 referrer 无行静默丢失)"
    assert "status='failed'" in func, "无 wallet 行须撤销 settled 标 failed 留人工"
