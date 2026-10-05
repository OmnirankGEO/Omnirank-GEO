# -*- coding: utf-8 -*-
"""§4 通知链 —— 判据打**落库内容**,不是"函数被调用过"。"""
from __future__ import annotations

import services.monitor_billing_notify as N


class _Spy:
    def __init__(self): self.user_calls = []; self.admin_calls = []
    def user(self, **kw): self.user_calls.append(kw); return 4242
    def admin(self, **kw): self.admin_calls.append(kw); return 3


def _patch(monkeypatch, spy):
    import services.notification_outbox as OB
    monkeypatch.setattr(OB, "enqueue_user_notification_event_durable", spy.user, raising=False)
    monkeypatch.setattr(OB, "enqueue_admin_notification_events_durable", spy.admin, raising=False)
    monkeypatch.setattr(N, "_charge_notify_level", lambda uid: "all")


def test_N3_pause_notifies_billing_subject(monkeypatch):
    """正向:余额不足暂停 → 通知真的发出,且内容里带得出恢复方式。"""
    spy = _Spy(); _patch(monkeypatch, spy)
    r = N.notify_monitor_paused_low_balance(
        subscription_id=92, billing_user_id=28, billing_mode="brand_owner",
        brand_id=592, brand_name="岱林生物", keyword="灭菌验证", daily_points=130)
    assert r["sent"] == 1 and r["recipient_user_id"] == 28, f"没通知到计费主体:{r}"
    assert len(spy.user_calls) == 1 and not spy.admin_calls
    facts = spy.user_calls[0]["facts"]
    assert "岱林生物" in facts["summary"] and "灭菌验证" in facts["summary"], "通知没说清是哪个品牌哪个词"
    assert "130" in facts["summary"] and "恢复" in facts["summary"], "通知没说清充多少能恢复"


def test_N3_platform_mode_notifies_admin_not_service_provider(monkeypatch):
    """🔴 平台承担暂停 → 通知落管理员侧,**断言那个服务商没收到**。

    他没付钱,收到"你的余额不足"是错的,还会让他去充一笔本不该他出的钱。
    """
    spy = _Spy(); _patch(monkeypatch, spy)
    r = N.notify_monitor_paused_low_balance(
        subscription_id=92, billing_user_id=136, billing_mode="platform",
        brand_id=592, brand_name="岱林生物", keyword="灭菌验证")
    assert r["recipient_kind"] == "admin" and r["sent"] == 3
    assert r["service_provider_notified"] is False
    assert spy.user_calls == [], f"服务商侧收到了通知:{spy.user_calls}"


def test_N1_notifies_wallet_owner_when_operator_differs(monkeypatch):
    """正向:管理员替服务商开 → 通知钱包主人,内容含谁/哪个品牌/每天多少。"""
    spy = _Spy(); _patch(monkeypatch, spy)
    r = N.notify_monitor_enabled_by_other(
        subscription_id=7, billing_user_id=28, operator_user_id=1,
        billing_mode="brand_owner", brand_id=592, brand_name="岱林生物", daily_points=130)
    assert r["sent"] == 1 and r["recipient_user_id"] == 28
    summary = spy.user_calls[0]["facts"]["summary"]
    assert "#1" in summary and "岱林生物" in summary and "130" in summary


def test_N1_reverse_same_subject_sends_nothing(monkeypatch):
    """🔴 反向(防恒发):自己给自己开 → 一条都不发。"""
    spy = _Spy(); _patch(monkeypatch, spy)
    r = N.notify_monitor_enabled_by_other(
        subscription_id=7, billing_user_id=28, operator_user_id=28,
        billing_mode="brand_owner", brand_id=592)
    assert r["sent"] == 0 and r["reason"] == "same_subject"
    assert not spy.user_calls and not spy.admin_calls


def test_N3_is_wired_at_the_real_pause_site():
    """🔴 接线锁:真正的暂停点必须调它 —— 设施在、没人调是本仓的惯犯形态
    (MONITORING_PAUSED 这个事件类型连模板都有,却一直没有订阅级调用方)。"""
    sched = open("scheduler.py", encoding="utf-8").read()
    i = sched.index('pause_keyword_monitor_subscription, sub["id"], "insufficient_balance"')
    assert "notify_monitor_paused_low_balance" in sched[i:i + 1800], \
        "余额不足暂停点没有接通知 —— 岱林静默 5 天的形态会原样重演"


def test_notify_failure_never_blocks_the_pause(monkeypatch):
    """🔴 fail-open:通知发不出去不许影响暂停本身(暂停是保护性动作)。"""
    import services.notification_outbox as OB
    def boom(**kw): raise RuntimeError("outbox down")
    monkeypatch.setattr(OB, "enqueue_user_notification_event_durable", boom, raising=False)
    monkeypatch.setattr(N, "_charge_notify_level", lambda uid: "all")
    r = N.notify_monitor_paused_low_balance(
        subscription_id=92, billing_user_id=28, billing_mode="brand_owner", brand_id=592)
    assert r["sent"] == 0 and "error" in r, "通知失败应被吞掉并记录,而不是抛出去打断暂停"
