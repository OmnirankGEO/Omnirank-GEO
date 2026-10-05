"""Registered adapters from existing terminal producers into the observation ledger.

The registry is used by both the reconciler and readiness.  Readiness therefore
cannot claim that a source is wired while the real reconciler uses another path.
Adapters only read already-persisted terminal rows; they never call an AI vendor
or billing code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import source_hooks


@dataclass(frozen=True)
class TerminalSourceAdapter:
    source_type: str
    register_terminal: Callable[[Any, dict], int]


def _register_paid_diagnosis(cur: Any, row: dict) -> int:
    registered = source_hooks.register_paid_diagnosis(
        cur, str(row["run_token"]), row["finished_at"]
    )
    return sum(1 for _, created in registered if created)


def _register_monitoring(cur: Any, row: dict) -> int:
    result = source_hooks.register_monitoring_result(cur, int(row["id"]))
    return int(bool(result and result[1]))


def _register_research(cur: Any, row: dict) -> int:
    result = source_hooks.register_research_raw(cur, int(row["id"]))
    return int(bool(result and result[1]))


SOURCE_ADAPTERS: dict[str, TerminalSourceAdapter] = {
    "paid_diagnosis": TerminalSourceAdapter("paid_diagnosis", _register_paid_diagnosis),
    "recurring_monitoring": TerminalSourceAdapter("recurring_monitoring", _register_monitoring),
    "research_round": TerminalSourceAdapter("research_round", _register_research),
}


def registered_source_adapters() -> dict[str, TerminalSourceAdapter]:
    """Return a copy so tests/readiness cannot mutate the live registry accidentally."""
    return dict(SOURCE_ADAPTERS)
