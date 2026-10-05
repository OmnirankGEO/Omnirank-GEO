"""
P0 回归防护:诊断资金 sweeper/reconciler 必须**无条件注册**(register_v32_core_tasks)
================================================================================
对抗审核抓出:初版把 diagnosis_run_sweeper + diagnosis_reconciler 放在 gated setup_schedule
(受 auto_monitor_enabled 控制 · prod=0 → 永不注册)→ 资金自愈(判死/收尸/结算退避)+ 终态回补
在生产全死(BUG-004 同坑)。本测直接跑 register_v32_core_tasks,断言两 job 真被注册 —— 若有人
把它挪回 setup_schedule 或注册块因 NameError(IntervalTrigger 未导入)静默失败,此测立即变红。
"""
import pytest
import inspect


def test_diagnosis_sweeper_and_reconciler_registered_in_register_v32(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler
    import api.scheduler as sch

    fake = BackgroundScheduler()
    monkeypatch.setattr(sch, "get_scheduler", lambda: fake)
    try:
        sch.register_v32_core_tasks()
    finally:
        try:
            if fake.running:
                fake.shutdown(wait=False)
        except Exception:
            pass

    # 无条件注册断言(prod auto_monitor_enabled=0 也必须有)
    assert fake.get_job("diagnosis_run_sweeper") is not None, \
        "diagnosis_run_sweeper 未在 register_v32_core_tasks 注册(BUG-004 同坑:放回 gated setup_schedule 或 IntervalTrigger 未导入静默失败)"
    assert fake.get_job("diagnosis_reconciler") is not None, \
        "diagnosis_reconciler 未在 register_v32_core_tasks 注册"
    assert not inspect.iscoroutinefunction(fake.get_job("diagnosis_run_sweeper").func), \
        "BackgroundScheduler 不会 await coroutine，sweeper 必须注册为同步适配 callable"
    assert not inspect.iscoroutinefunction(fake.get_job("diagnosis_reconciler").func), \
        "BackgroundScheduler 不会 await coroutine，reconciler 必须注册为同步适配 callable"
