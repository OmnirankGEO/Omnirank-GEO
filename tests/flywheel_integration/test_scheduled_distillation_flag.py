"""[§7] 定时监测蒸馏桥 flag:默认关字节一致零触发 + 开启触发 + 蒸馏失败不影响监测 completed。"""
import asyncio
from pathlib import Path

import pytest


def test_flag_registered_default_off():
    from writing.feature_switches import SWITCH_SPECS
    spec = SWITCH_SPECS.get("monitoring_scheduled_distillation")
    assert spec is not None, "flag 必须注册进 SWITCH_SPECS"
    assert spec.default_enabled is False, "默认必须关"
    assert spec.dangerous is False  # 不需要 confirm token


def test_is_feature_enabled_default_false(monkeypatch, tmp_path):
    # 指向一个不存在的 state 文件 → bootstrap 用 spec 默认值 → False
    monkeypatch.setenv("WRITING_FEATURE_SWITCH_FILE", str(tmp_path / "switches.json"))
    from writing.feature_switches import is_feature_enabled
    assert is_feature_enabled("monitoring_scheduled_distillation") is False


def test_scheduler_wiring_is_flag_gated_and_after_completed():
    """源码级坐实:蒸馏触发在 _async_run_brand 完成段之后、flag 门控、fire-and-forget。"""
    src = Path("api/scheduler.py").read_text(encoding="utf-8")
    fn_start = src.index("async def _async_run_brand")
    fn_body = src[fn_start:]
    completed_at = fn_body.index('update_task_status(task_id, "completed"')
    flag_at = fn_body.index('is_feature_enabled("monitoring_scheduled_distillation")')
    trigger_at = fn_body.index("asyncio.create_task(trigger_distillation(task_id, brand_id))")
    assert completed_at < flag_at < trigger_at, "蒸馏触发必须在标记 completed 之后、且 flag 门控"
    # 语义锚定:出现在 _async_run_brand 内(不是全文件 supersede)
    assert "brand_id and is_feature_enabled" in fn_body


@pytest.mark.asyncio
async def test_flag_off_does_not_trigger_and_failure_is_soft():
    """复刻插入点守卫:flag 关不触发;flag 开即便 trigger 抛错也被 try/except 吞掉(不影响已 completed)。"""
    calls = []

    def guard(brand_id, flag_on, trigger):
        # 与 scheduler.py 插入块同构
        try:
            if brand_id and flag_on:
                trigger()
        except Exception:
            pass  # fail-soft:不影响监测 completed

    def ok_trigger():
        calls.append("t")

    def boom_trigger():
        raise RuntimeError("蒸馏挂了")

    # flag 关 → 不触发
    guard(brand_id=7, flag_on=False, trigger=ok_trigger)
    assert calls == []
    # flag 开 → 触发
    guard(brand_id=7, flag_on=True, trigger=ok_trigger)
    assert calls == ["t"]
    # flag 开但蒸馏抛错 → 被吞,不冒泡(监测 completed 不受影响)
    guard(brand_id=7, flag_on=True, trigger=boom_trigger)  # 不抛即通过
