"""[GEO-R9-CAN-007] Regression lock for db/research_answer_entity_db.py.

Source-inspection discriminative lock: the answer-identity hash must fold
`query` AND `industry` into the fact identity key (not just answer_text), so that
the same answer_text produced for two distinct queries/industries within one
engine/batch does NOT collapse into a single fact via GROUP BY + UNIQUE + MIN().

If someone reverts the fix (identity back to MD5(answer_text) only), these
assertions fail. No DB / no server import required.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "db" / "research_answer_entity_db.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def test_identity_hash_constant_folds_query_and_industry():
    """Shared identity-hash SQL constant must include query + industry + answer_text."""
    assert "_ANSWER_IDENTITY_HASH_SQL" in SRC, "identity-hash constant missing"
    # extract the constant definition block
    m = re.search(r"_ANSWER_IDENTITY_HASH_SQL\s*=\s*\(\s*(.*?)\s*\)\s*\n", SRC, re.DOTALL)
    assert m, "could not locate _ANSWER_IDENTITY_HASH_SQL assignment"
    expr = m.group(1)
    low = expr.lower()
    assert "md5(" in low, "identity key must be an MD5 hash"
    assert "query" in low, "identity key must include query"
    assert "industry" in low, "identity key must include industry"
    assert "answer_text" in low, "identity key must still include answer_text"


def test_all_three_query_sites_use_shared_identity_hash():
    """count / fetch / health must all reference the shared identity-hash constant,
    not the old MD5(COALESCE(answer_text, '')) that omitted query+industry."""
    # No call site should still fold identity on answer_text alone.
    assert "MD5(COALESCE(answer_text, ''))" not in SRC, (
        "an answer-identity grouping still uses answer_text-only MD5 — "
        "regression: same text across different query/industry will collapse"
    )
    # The shared constant is referenced at every grouping/pending site (>=3 uses
    # beyond its own definition line).
    uses = SRC.count("_ANSWER_IDENTITY_HASH_SQL")
    assert uses >= 4, f"expected constant defined + used at 3 sites, found {uses} refs"


def test_fetch_and_count_pending_check_share_identity():
    """The pending NOT EXISTS must compare against the same identity key
    (answer_md5 built from the shared constant), so newly-scoped facts are
    correctly recognized as extracted/pending."""
    for fn in ("count_extractable_answers", "fetch_answer_groups_for_extraction"):
        start = SRC.index(f"def {fn}(")
        end = SRC.index("\ndef ", start + 1) if "\ndef " in SRC[start + 1:] else len(SRC)
        body = SRC[start:end]
        assert "_ANSWER_IDENTITY_HASH_SQL} AS answer_md5" in body, (
            f"{fn} must derive answer_md5 from the shared identity hash"
        )
