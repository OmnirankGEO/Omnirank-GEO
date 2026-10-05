"""Regression locks for writing/article_rewrite_jobs.py fixes.

GEO-R6-CAN-007: BatchRewriteJobStore.prune must never evict a still-running
(non-terminal) job by wall-clock age. Evicting a running job drops its
idempotency-key mapping so a timeout-retry re-dispatches and double-charges.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_FILE = ROOT / "writing" / "article_rewrite_jobs.py"


def _read_src() -> str:
    return SRC_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-inspection discriminative lock
# ---------------------------------------------------------------------------

def test_prune_guards_terminal_status_source():
    """prune() body must gate eviction on terminal status (fix marker)."""
    src = _read_src()
    prune_start = src.index("def prune(")
    prune_end = src.index("def get(", prune_start)
    prune_body = src[prune_start:prune_end]
    # The fix: only terminal jobs are aged out. A regression (removing the
    # status guard) makes this assertion fail.
    assert "job.status in TERMINAL_STATUSES" in prune_body
    assert "GEO-R6-CAN-007" in prune_body


# ---------------------------------------------------------------------------
# Real behaviour lock (pure in-memory store, no DB / no server.py)
# ---------------------------------------------------------------------------

def _store(ttl=600):
    from writing.article_rewrite_jobs import BatchRewriteJobStore

    counter = {"n": 0}

    def _factory():
        counter["n"] += 1
        return f"ref-{counter['n']}"

    return BatchRewriteJobStore(ttl_seconds=ttl, ref_factory=_factory)


def test_running_job_survives_ttl_and_is_reused():
    store = _store(ttl=600)
    kwargs = dict(actor_key="agent:1", quote_id=42, topic_ids=[3, 1, 2])

    job, created = store.get_or_create(now=0.0, **kwargs)
    assert created is True
    store.mark_running(job.batch_ref, now=1.0)

    # Advance the clock well past the TTL while the job is still running.
    job2, created2 = store.get_or_create(now=5000.0, **kwargs)

    # Must return the SAME job — no new ref, no re-dispatch, no double charge.
    assert created2 is False
    assert job2.batch_ref == job.batch_ref
    assert job2.status == "running"


def test_terminal_job_still_ages_out():
    store = _store(ttl=600)
    kwargs = dict(actor_key="agent:1", quote_id=42, topic_ids=[3, 1, 2])

    job, _ = store.get_or_create(now=0.0, **kwargs)
    store.mark_completed(job.batch_ref, result={"ok": True}, now=1.0)

    # Past TTL a terminal job is evicted, so a fresh submission mints a new job.
    job2, created2 = store.get_or_create(now=5000.0, **kwargs)
    assert created2 is True
    assert job2.batch_ref != job.batch_ref
