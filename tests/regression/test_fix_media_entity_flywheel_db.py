"""Regression lock for db/media_entity_flywheel_db.py fixes.

[GEO-R2-CAN-013] list_shadow_media_entities must de-duplicate one snapshot per
entity BEFORE applying LIMIT, so an all-industry board does not let duplicate
cross-industry snapshots of the same entity consume the LIMIT (and does not let
an entity's "global" score be an arbitrary cross-industry latest).

Source-inspection discriminative lock: reverting the fix (dropping the
DISTINCT ON per-entity dedupe subquery) makes these assertions fail. Does not
touch the DB or import server.py.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = (ROOT / "db" / "media_entity_flywheel_db.py").read_text(encoding="utf-8")


def _list_shadow_body() -> str:
    """Isolate the list_shadow_media_entities function body."""
    start = SRC.index("def list_shadow_media_entities(")
    # next top-level def after it
    nxt = SRC.index("\ndef ", start + 1)
    return SRC[start:nxt]


def test_dedupe_marker_present():
    body = _list_shadow_body()
    assert "[GEO-R2-CAN-013]" in body, "fix marker missing in list_shadow_media_entities"


def test_distinct_on_per_entity_dedupe():
    body = _list_shadow_body()
    # per-entity dedupe must be expressed via DISTINCT ON (e.id)
    assert re.search(r"DISTINCT\s+ON\s*\(\s*e\.id\s*\)", body), \
        "expected DISTINCT ON (e.id) per-entity dedupe subquery"


def test_dedupe_happens_before_limit():
    body = _list_shadow_body()
    # The dedupe subquery must be wrapped and LIMIT applied on the OUTER query.
    assert "dedup" in body, "expected wrapping subquery alias 'dedup'"
    distinct_idx = body.index("DISTINCT ON")
    # outer ORDER BY references the dedup alias, and LIMIT comes after dedupe
    outer_order_idx = body.index("ORDER BY dedup.")
    limit_idx = body.rindex("LIMIT %s")
    assert distinct_idx < outer_order_idx < limit_idx, \
        "LIMIT must be applied after the per-entity dedupe subquery"


def test_inner_order_starts_with_entity_id():
    body = _list_shadow_body()
    # DISTINCT ON requires the inner ORDER BY to lead with e.id so the kept row
    # per entity is the best-ranked one (purchasable/shadow/evidence).
    assert re.search(r"ORDER BY\s+e\.id\s*,\s*s\.is_purchasable", body), \
        "inner ORDER BY must lead with e.id then ranking columns"
