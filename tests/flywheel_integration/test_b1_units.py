"""[B1-3 + B1-6] 纯逻辑单元测试(不碰 DB)。"""
import asyncio
import threading

import pytest


# ----- B1-3: OSS 正文回读 backfill -----
def test_backfill_oss_bodies_reads_only_oss_only_rows(monkeypatch):
    from services import article_structure_analysis as asa

    seen = []

    def fake_dl(key):
        seen.append(key)
        return "OSS BODY CONTENT " * 50

    # _backfill_oss_bodies 内部 `from ...oss_helper import download_markdown`,pat 模块属性即可。
    monkeypatch.setattr("services.research_monitor.oss_helper.download_markdown", fake_dl)

    rows = [
        {"id": 1, "inline_cleaned_content": "", "oss_key_cleaned": "k/1.md"},        # OSS-only → 回读
        {"id": 2, "inline_cleaned_content": "有内联正文", "oss_key_cleaned": "k/2.md"},  # 有内联 → 跳过
        {"id": 3, "inline_cleaned_content": "", "oss_key_cleaned": None},             # 无 key → 跳过
    ]
    loaded = asa._backfill_oss_bodies(rows)

    assert loaded == 1
    assert seen == ["k/1.md"]
    assert rows[0]["cleaned_content"].startswith("OSS BODY")  # 写回 row 让下游提取器读到
    assert "cleaned_content" not in rows[1]                    # 有内联的行不动
    assert "cleaned_content" not in rows[2]


def test_backfill_oss_bodies_caps_downloads(monkeypatch):
    """[review fix] OSS 回读封顶,防 dashboard 自动加载打满线程池。"""
    from services import article_structure_analysis as asa

    calls = []

    def fake_dl(key):
        calls.append(key)
        return "BODY " * 50

    monkeypatch.setattr("services.research_monitor.oss_helper.download_markdown", fake_dl)
    monkeypatch.setattr(asa, "_MAX_OSS_BACKFILL", 5)  # 收紧上限便于断言

    rows = [{"id": i, "inline_cleaned_content": "", "oss_key_cleaned": f"k/{i}.md"} for i in range(20)]
    loaded = asa._backfill_oss_bodies(rows)
    assert loaded == 5            # 只回读上限条
    assert len(calls) == 5        # 只下载 5 次(其余 OSS 行不触发网络)


# ----- B1-6: rebuild 后台化 + 互斥守卫 -----
@pytest.mark.asyncio
async def test_rebuild_guard_busy_returns_in_progress_then_releases():
    from services.flywheel_rebuild_lock import run_rebuild_guarded

    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(timeout=5)
        return {"status": "success", "ran": True}

    task = asyncio.create_task(run_rebuild_guarded("k_test", slow))
    # 等 worker 线程进入 slow()(持有 key)
    await asyncio.get_event_loop().run_in_executor(None, started.wait, 5)

    # 同 key 第二次触发 → 立即 in_progress(不并行、不排队)
    busy = await run_rebuild_guarded("k_test", lambda: {"status": "success"})
    assert busy["status"] == "in_progress"

    release.set()
    first = await task
    assert first["status"] == "success" and first["ran"] is True

    # key 释放后可再次运行
    again = await run_rebuild_guarded("k_test", lambda: {"status": "success", "x": 2})
    assert again.get("x") == 2


@pytest.mark.asyncio
async def test_rebuild_guard_passes_kwargs():
    from services.flywheel_rebuild_lock import run_rebuild_guarded

    def fn(a, b=0):
        return {"sum": a + b}

    out = await run_rebuild_guarded("k_kwargs", fn, a=3, b=4)
    assert out["sum"] == 7
