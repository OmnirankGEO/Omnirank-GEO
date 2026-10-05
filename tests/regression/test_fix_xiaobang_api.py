"""Regression lock for GEO-R10-CAN-026 (api/xiaobang_api.py).

Defect: `_AGENT_LEVEL_CACHE` was a process-local dict keyed only on user_id with a
300s TTL and no invalidation on `agent_level` mutation. A downgraded user (agent -> 0)
kept the 'agent' KB retrieval pool for up to 5 minutes.

Fix: cache is now bound to `permission_version` (role/permission changes bump it -> stale
cache misses -> re-fetch), TTL tightened to 60s, and a public `invalidate_agent_level_cache`
hook is exposed for mutation sites. This test is a source-inspection lock plus a pure-logic
behavior check of the invalidation hook (no DB / no server import).
"""

import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "api" / "xiaobang_api.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _slice(anchor_start: str, anchor_end: str) -> str:
    i = SRC.index(anchor_start)
    j = SRC.index(anchor_end, i)
    return SRC[i:j]


# ---------------------------------------------------------------------------
# Source-inspection locks: reverting the fix flips these to failing.
# ---------------------------------------------------------------------------

def test_marker_comment_present():
    assert "GEO-R10-CAN-026" in SRC, "fix marker comment removed"


def test_cache_tuple_carries_perm_version():
    # Cache type annotation must be the 3-tuple (ts, level, perm_version), not the old 2-tuple.
    assert re.search(
        r"_AGENT_LEVEL_CACHE:\s*dict\[int,\s*tuple\[float,\s*int,\s*int\]\]",
        SRC,
    ), "cache no longer keyed with permission_version tuple"


def test_ttl_tightened():
    m = re.search(r"_AGENT_LEVEL_TTL_SEC\s*=\s*(\d+)", SRC)
    assert m, "_AGENT_LEVEL_TTL_SEC not found"
    assert int(m.group(1)) <= 60, f"TTL not tightened (got {m.group(1)}s, expected <=60)"
    assert int(m.group(1)) != 300, "TTL still at the vulnerable 300s"


def test_invalidation_hook_exists():
    assert "def invalidate_agent_level_cache(" in SRC, "invalidation hook removed"


def test_fetch_consults_permission_version():
    body = _slice("def _fetch_agent_level(", "def _resolve_kb_identity(")
    assert "_current_perm_version(" in body, "_fetch_agent_level no longer reads permission_version"
    # The cache-hit guard must require the cached version to match the current one.
    assert re.search(r"cached\[2\]\s*==\s*cur_ver", body), \
        "cache-hit path does not enforce permission_version match"


def test_perm_version_resolver_uses_shared_cache():
    body = _slice("def _current_perm_version(", "def _fetch_agent_level(")
    assert "get_cached_permission_version" in body, \
        "_current_perm_version not wired to auth.perm_cache"
    # fail-closed: unknown version must force a DB re-fetch (return -1 != any real version).
    assert "return -1" in body, "_current_perm_version not fail-closed on error"


# ---------------------------------------------------------------------------
# Pure-logic behavior check of the invalidation hook (no DB, no app).
# ---------------------------------------------------------------------------

def test_invalidate_hook_clears_stale_entry():
    import importlib
    mod = importlib.import_module("api.xiaobang_api")

    # Warm a fake 'agent' entry, then invalidate it — mutation-site simulation.
    mod._AGENT_LEVEL_CACHE[999999] = (10.0**12, 1, 5)
    assert 999999 in mod._AGENT_LEVEL_CACHE
    mod.invalidate_agent_level_cache(999999)
    assert 999999 not in mod._AGENT_LEVEL_CACHE, "invalidation hook did not drop the entry"

    # No-arg call clears everything.
    mod._AGENT_LEVEL_CACHE[1] = (10.0**12, 1, 5)
    mod._AGENT_LEVEL_CACHE[2] = (10.0**12, 0, 5)
    mod.invalidate_agent_level_cache()
    assert not mod._AGENT_LEVEL_CACHE, "no-arg invalidation did not clear the cache"


def test_version_mismatch_forces_refetch(monkeypatch):
    """Stale 'agent' cache under a bumped permission_version must NOT be served."""
    import importlib
    mod = importlib.import_module("api.xiaobang_api")

    uid = 424242
    # Warm cache as agent (level=1) recorded under perm_version 5.
    import time as _t
    mod._AGENT_LEVEL_CACHE[uid] = (_t.time(), 1, 5)

    # Simulate a role change: current permission_version is now 6.
    monkeypatch.setattr(mod, "_current_perm_version", lambda u: 6)
    # And DB now returns 0 (downgraded). Stub the connection to prove re-fetch happened.
    fetched = {"hit": False}

    class _Cur:
        def execute(self, *a, **k):
            fetched["hit"] = True

        def fetchone(self):
            return {"agent_level": 0}

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    import db.connection as dbc
    monkeypatch.setattr(dbc, "get_connection", lambda: _Conn())

    level = mod._fetch_agent_level(uid)
    assert fetched["hit"], "version mismatch did not trigger a DB re-fetch"
    assert level == 0, "stale agent level served despite permission_version bump"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
