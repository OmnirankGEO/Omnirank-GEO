from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_batch_rewrite_job_store_keeps_active_job_and_expires_terminal_result():
    from writing.article_rewrite_jobs import BatchRewriteJobStore

    refs = iter(["batch_a", "batch_b"])
    store = BatchRewriteJobStore(ttl_seconds=600, ref_factory=lambda: next(refs))

    first, first_created = store.get_or_create(
        actor_key="user:24",
        quote_id=372,
        topic_ids=[3, 1, 2],
        payload_fingerprint="default",
        now=1000.0,
    )
    second, second_created = store.get_or_create(
        actor_key="user:24",
        quote_id=372,
        topic_ids=[2, 3, 1],
        payload_fingerprint="default",
        now=1200.0,
    )
    third, third_created = store.get_or_create(
        actor_key="user:24",
        quote_id=372,
        topic_ids=[1, 2, 3],
        payload_fingerprint="default",
        now=1601.0,
    )

    assert first_created is True
    assert second_created is False
    assert third_created is False
    assert first.batch_ref == "batch_a"
    assert second.batch_ref == "batch_a"
    assert third.batch_ref == "batch_a"

    store.mark_completed(first.batch_ref, result={"success": 3}, now=1602.0)
    fourth, fourth_created = store.get_or_create(
        actor_key="user:24",
        quote_id=372,
        topic_ids=[1, 2, 3],
        payload_fingerprint="default",
        now=2203.0,
    )

    assert fourth_created is True
    assert fourth.batch_ref == "batch_b"


def test_batch_rewrite_server_contract_is_async_and_charge_after_worker_success():
    source = (ROOT / "server.py").read_text(encoding="utf-8")

    assert '@app.post("/api/writing/batch-rewrite")' in source
    assert '@app.get("/api/writing/batch-rewrite/{batch_ref}")' in source
    assert "asyncio.create_task(_run_batch_rewrite_job" in source
    assert "batch_ref" in source

    worker_index = source.index("async def _run_batch_rewrite_job")
    worker_source = source[worker_index : source.index('@app.post("/api/writing/batch-rewrite")')]
    rewrite_index = worker_source.index("batch_rewrite_articles")
    charge_index = worker_source.index("deduct_points")
    assert rewrite_index < charge_index


def test_writing_hall_batch_rewrite_uses_polling_and_background_copy():
    source = (ROOT / "frontend/src/pages/Writing/WritingHall.tsx").read_text(
        encoding="utf-8"
    )

    assert "/api/writing/batch-rewrite/" in source
    assert "已提交" in source
    assert "后台生成中" in source
    assert "批量重写请求失败" not in source
