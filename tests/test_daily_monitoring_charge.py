"""
CTO-15.23 2026-05-09 · daily_monitoring 集成长任务冻结结算 + llm_track 测试
对应任务 3 cherry-pick 49c489f4 后的 scheduler.py 改造

测试不依赖 prod DB · 只读静态代码 + import smoke
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scheduler_uses_list_active_subscriptions_not_get_active_client_keywords():
    """scheduler.py:job_daily_monitoring 必须用 list_active_subscriptions
    (新模型 · 严格按 keyword_monitor_subscriptions.status='active' 过滤)
    不再用 get_active_client_keywords(老逻辑 · 按 confirmed_keywords.is_monitored)
    """
    text = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert "list_active_subscriptions" in text, "必须用新订阅模型 helper"
    # 老 helper get_active_client_keywords 应该不在 import 中(若仍存在 fallback 也 OK · 但 import 段不该有)
    job_section_start = text.find("async def job_daily_monitoring")
    job_section_end = text.find("async def job_token_expiry_check")
    if job_section_end < 0:
        job_section_end = len(text)
    job_section = text[job_section_start:job_section_end]
    assert "list_active_subscriptions" in job_section


def test_process_keyword_uses_durable_freeze_settlement():
    """daily 是异步多 provider 任务：先冻结并绑定账本，再按 dispatch commit/release。"""
    text = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert "freeze_points" in text
    assert "commit_freeze" in text
    assert "release_freeze" in text
    assert "create_monitoring_keyword_settlements" in text
    assert "mark_monitoring_keyword_settlement_dispatched" in text
    assert "record_monitoring_subscription_charge_for_settlement" in text


def test_daily_monitoring_filters_low_balance_subs():
    """daily_monitoring 跑前必须 check_balance_only 过滤 + pause_keyword_monitor_subscription
    余额不足 → pause subscription · is_monitored=FALSE · 不进 LLM 调用
    """
    text = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert "check_balance_only" in text
    assert "pause_keyword_monitor_subscription" in text
    # 充值后恢复(integrated 在 daily 顶部 · 不另开 cron)
    assert "list_paused_subscriptions_for_resume" in text
    assert "resume_keyword_monitor_subscription" in text


def test_run_detection_passes_llm_track_args():
    """run_detection_for_keyword 必须接收 brand_id/user_id/caller
    并把上下文传给 PlatformAdapter.query，由内层真实 LLM HTTP 调用写 llm_call_log。
    """
    text = (ROOT / "api/monitoring_api.py").read_text(encoding="utf-8")
    # 函数签名加新 keyword args
    sig_start = text.find("async def run_detection_for_keyword")
    sig_end = text.find(") -> dict:", sig_start)
    sig = text[sig_start: sig_end + 10]
    assert "brand_id: int = None" in sig
    assert "user_id: int = None" in sig
    assert "caller: str =" in sig
    # 不再外层写 0-token wrapper；由 PlatformAdapter 传递业务上下文给真实调用。
    body_section = text[sig_end:sig_end + 4000]
    assert "PlatformAdapter.query(" in body_section
    assert "caller=caller" in body_section
    assert "brand_id=brand_id" in body_section
    assert "quote_id=quote_id" in body_section
    assert "user_id=user_id" in body_section


def test_scheduler_imports_billing_safely():
    """freeze/commit/release/check_balance_only 必须从 middleware.billing import
    (红线文件 · 只 import 不修改)
    """
    text = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    billing_import = text[text.index("from middleware.billing import ("):]
    billing_import = billing_import[:billing_import.index(")")]
    for name in ("check_balance_only", "freeze_points", "commit_freeze", "release_freeze"):
        assert name in billing_import


def test_p0_4_record_actual_frozen_amount_not_hardcoded_130():
    """daily 订阅流水从 durable freeze 快照取实际金额，admin/零价为 0。

    原 record_subscription_charge(subscription_id, 130):admin 免扣实扣 0 仍记 130 →
    total_charged 虚高失真;feature_pricing 调价后 130 与实扣漂移。
    (admin 免扣"白烧 4 引擎"的根治=订阅主体锚 brand owner,在监测域 enable 端点由 GEO 出)
    """
    scheduler_text = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    db_text = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    assert "record_subscription_charge" not in scheduler_text
    assert "frozen_amount" in db_text
    assert 'int(row.get("frozen_amount") or 0)' in db_text
