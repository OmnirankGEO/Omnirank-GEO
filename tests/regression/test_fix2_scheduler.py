"""
Regression test (source-inspection based) for FIX-2 batch on api/scheduler.py.

Discriminative locks. No server.py import, no DB.

[P1-B 2026-07-30] CAN-031 was moved off comment-marker/variable-name matching onto an
AST structural criterion — its marker string was dropped in a refactor, which left the
test permanently red while the invariant it guards was still enforced. A permanently red
test is the same as no test at all. CAN-033 / CAN-016 below still match source strings;
they are green today, and reworking them is filed as follow-up rather than done here.

Covered candidates:
- GEO-R2-CAN-031: batch save non-atomic -> guarantee terminal 'failed' task state on save error.
- GEO-R2-CAN-033: claim fail-open -> fail-closed (skip brand) on claim exception.
- GEO-R6-CAN-016: meijiehezi mass-deactivate on empty/partial snapshot -> completeness guard.

Skipped (documented, no assertion of a fix):
- GEO-R2-CAN-032 / GEO-R2-CAN-034: fund reconciliation (needs-manual-fund-review).
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SCHED = Path(__file__).resolve().parents[2] / "api" / "scheduler.py"
SRC = SCHED.read_text(encoding="utf-8")


def _slice_after(marker: str, n: int = 1200) -> str:
    idx = SRC.find(marker)
    assert idx != -1, f"marker not found: {marker!r}"
    return SRC[idx: idx + n]


# ---------------- GEO-R2-CAN-033: claim fail-open -> fail-closed ----------------

def test_can033_claim_exception_fails_closed_with_continue():
    """On claim exception the scheduler must skip the brand (continue), not fall through."""
    assert "[GEO-R2-CAN-033]" in SRC, "CAN-033 fix marker missing"
    block = _slice_after("except Exception as claim_err:", 600)
    # must contain a continue (skip brand) after logging, i.e. fail-closed
    assert "continue" in block, "CAN-033: claim except must fail-closed with continue"
    # the old fail-open comment '走原流程' must no longer be the behavior on this path
    # (marker + continue present is the discriminating signal)
    assert "fail-closed" in block or "防重复扣费" in block


# ---------------- GEO-R2-CAN-031: batch save terminal state ----------------

def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    table: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            table[id(child)] = node
    return table


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def test_can031_persist_failure_cannot_fall_through_as_success():
    """A persist failure must never fall through as a successful outcome.

    [P1-B 2026-07-30 changed the criterion — read this before "fixing" a red run]
    The previous version asserted on a **comment marker** (`[GEO-R2-CAN-031]`) plus an
    **exception variable name** (`except Exception as save_err`). Both were removed when
    the monitoring path was refactored to settle every provider outcome against its own
    durable cell, so this test went red while the invariant it guards was still enforced.
    A permanently red test is the same as no test at all: nobody notices the day it goes
    red for a real reason. Source-string asserts break both ways — a rename ("换皮") slips
    past them, and a refactor makes them scream about nothing.

    The criterion is now **structural (AST)**, so it survives renames and comment edits
    but still goes red if the guard itself is deleted:

      1. every `save_monitoring_result(...)` call sits inside a `try` whose handler
         durably settles the outcome (`finish_monitoring_cell_error`) — a persist error
         cannot be reported as success;
      2. some block transitions the task to the terminal `'failed'` state and re-raises
         in the same block, so the outer handler releases the freeze.
    """
    tree = ast.parse(SRC)
    parents = _parents(tree)

    # ---- (1) persist call is guarded, and the handler settles durably ----
    save_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "save_monitoring_result"
    ]
    assert save_calls, "save_monitoring_result call not found — scheduler shape changed"

    for call in save_calls:
        node: ast.AST | None = call
        settling_try = None
        while node is not None:
            if isinstance(node, ast.Try) and any(
                "finish_monitoring_cell_error" in _called_names(handler)
                for handler in node.handlers
            ):
                settling_try = node
                break
            node = parents.get(id(node))
        assert settling_try is not None, (
            "CAN-031: save_monitoring_result must sit inside a try whose except "
            "durably settles the cell (finish_monitoring_cell_error), otherwise a "
            "persist failure is reported as success"
        )

    # ---- (2) terminal 'failed' transition + re-raise in the same block ----
    def _marks_failed(stmt: ast.AST) -> bool:
        for sub in ast.walk(stmt):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "update_task_status":
                continue
            if any(
                isinstance(a, ast.Constant) and a.value == "failed" for a in sub.args
            ):
                return True
        return False

    found = False
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            marked_at = next(
                (i for i, stmt in enumerate(block) if _marks_failed(stmt)), None
            )
            if marked_at is None:
                continue
            if any(
                isinstance(stmt, ast.Raise)
                or any(isinstance(s, ast.Raise) for s in ast.walk(stmt))
                for stmt in block[marked_at:]
            ):
                found = True
                break
        if found:
            break
    assert found, (
        "CAN-031: no block transitions the task to terminal 'failed' and re-raises — "
        "without the re-raise the outer handler never releases the freeze"
    )


def test_can031_criterion_still_discriminates():
    """判别力反证：把守卫从源码里拆掉，上面那条必须转红。

    直接在**内存里**改 AST 的等价源码（不动磁盘文件），分别拆掉两处守卫，
    确认判据都不再成立 —— 证明它不是恒真断言。
    """
    # 变异 A：把 save_monitoring_result 的 try/except 摘掉
    mutated_a = SRC.replace("finish_monitoring_cell_error(", "log_something(")
    tree_a = ast.parse(mutated_a)
    parents_a = _parents(tree_a)
    guarded = []
    for call in [
        n for n in ast.walk(tree_a)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "save_monitoring_result"
    ]:
        node: ast.AST | None = call
        ok = False
        while node is not None:
            if isinstance(node, ast.Try) and any(
                "finish_monitoring_cell_error" in _called_names(h) for h in node.handlers
            ):
                ok = True
                break
            node = parents_a.get(id(node))
        guarded.append(ok)
    assert guarded and not any(guarded), (
        "判据 (1) 在拆掉 finish_monitoring_cell_error 后仍成立 → 它是恒真的，无判别力"
    )

    # 变异 B：把终态 'failed' 迁移改掉
    mutated_b = SRC.replace('update_task_status(task_id, "failed")', "pass")
    tree_b = ast.parse(mutated_b)
    still_marks = any(
        isinstance(sub, ast.Call)
        and (sub.func.id if isinstance(sub.func, ast.Name)
             else getattr(sub.func, "attr", None)) == "update_task_status"
        and any(isinstance(a, ast.Constant) and a.value == "failed" for a in sub.args)
        for sub in ast.walk(tree_b)
    )
    assert not still_marks, (
        "判据 (2) 在删掉 failed 迁移后仍能找到它 → 变异没生效，反证无意义"
    )


# ---------------- GEO-R6-CAN-016: snapshot completeness guard ----------------

def test_can016_media_sync_guards_empty_and_partial_snapshot():
    for fn, deact in (
        ("_mhz_media_sync", "deactivate_media"),
        ("_mhz_wemedia_sync", "deactivate_wemedia"),
        ("_mhz_short_video_sync", "deactivate_short_video"),
    ):
        idx = SRC.find(f"async def {fn}(")
        assert idx != -1, f"{fn} not found"
        body = SRC[idx: idx + 1600]
        assert "[GEO-R6-CAN-016]" in body, f"CAN-016 guard marker missing in {fn}"
        # empty-snapshot guard: `if not remote_ids:` must precede the deactivate call
        guard_idx = body.find("if not remote_ids:")
        deact_idx = body.find(deact + "(")
        assert guard_idx != -1, f"CAN-016: empty-snapshot guard missing in {fn}"
        assert deact_idx != -1, f"{deact} call missing in {fn}"
        assert guard_idx < deact_idx, f"CAN-016: guard must precede {deact} in {fn}"
        # guard must neutralize stale (no mass deactivate) on empty snapshot
        seg = body[guard_idx: deact_idx]
        assert "stale = set()" in seg, f"CAN-016: {fn} must clear stale on incomplete snapshot"


def test_can016_partial_snapshot_ratio_guard_present():
    idx = SRC.find("async def _mhz_media_sync(")
    body = SRC[idx: idx + 1600]
    # ratio-based mass-deactivation circuit breaker
    assert "len(stale)" in body and "len(local_ids)" in body, \
        "CAN-016: ratio guard against mass-deactivation must reference stale/local sizes"


# ---------------- GEO-R2-CAN-007: monitoring claim-before-work (SKIPPED) ----------------
# Round-2 fund/concurrency discipline: this finding proposes reworking the atomic
# claim into a release-on-failure lease. That changes a concurrency primitive whose
# idempotency key is tied to billing (freeze/commit) and would also require editing
# db/monitoring_db.py. Mandated skip (needs-manual-concurrency-review). These asserts
# lock in the anti-double-charge guard we deliberately PRESERVED, so any silent
# weakening of the claim-before-work race guard fails here.

def test_can007_atomic_claim_guard_preserved():
    """The atomic claim that prevents blue/green + dual-scheduler double-charge
    (documented P0: one keyword charged 4x130) must remain in place. A naive lease
    rework would loosen/remove it — that must not happen without concurrency review."""
    assert "claim_quote_for_monitoring" in SRC, (
        "atomic claim guard removed — GEO-R2-CAN-007 must stay skipped, not silently "
        "reworked into an unreviewed lease"
    )


def test_can007_no_unreviewed_fix_marker():
    """Documents status=skipped: no [GEO-R2-CAN-007] fix marker was stamped into
    scheduler.py this round. If one appears, reconcile code vs the reported skip."""
    assert "[GEO-R2-CAN-007]" not in SRC, (
        "a [GEO-R2-CAN-007] fix marker appeared but the finding was reported skipped "
        "(needs-manual-concurrency-review)"
    )
