import asyncio
import inspect

from api import scheduler
from services import freeze_sweeper


def test_background_scheduler_wrapper_awaits_freeze_sweeper(monkeypatch):
    observed = {"awaited": False}

    async def fake_sweep():
        await asyncio.sleep(0)
        observed["awaited"] = True
        return {"scanned": 3, "released": 2, "failed": 1}

    monkeypatch.setattr(freeze_sweeper, "run_freeze_sweep_hourly", fake_sweep)

    result = scheduler._run_freeze_sweep_hourly_job()

    assert observed["awaited"] is True
    assert result == {"scanned": 3, "released": 2, "failed": 1}
    assert not inspect.isawaitable(result)


def test_all_freeze_sweeper_registrations_use_sync_wrapper():
    source = inspect.getsource(scheduler)

    assert source.count("scheduler.add_job(\n                _run_freeze_sweep_hourly_job,") == 2
    assert "scheduler.add_job(\n                run_freeze_sweep_hourly," not in source
