"""Regression lock for placement_service GEO fixes.

[GEO-R9-CAN-006] _content_hash / LLM-check cache key must be bound to brand
context (brand_name + verified_names), not the article content prefix alone.
Otherwise brand B receives brand A's pass/fail verdict for identical content.

Primary form = source-inspection discriminative lock (does not touch DB / server).
Secondary = pure-function behavior test on _content_hash (no DB/app needed).
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "services" / "placement_service.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _content_hash_region() -> str:
    """Slice the _content_hash method body for targeted assertions."""
    start = SRC.index("def _content_hash(")
    # end at the next top-level method def after _content_hash
    nxt = SRC.index("def _batch_llm_check(", start)
    return SRC[start:nxt]


# ---------- source-inspection locks ----------

def test_content_hash_signature_takes_brand_context():
    """Regressing to `_content_hash(self, content)` (no brand args) fails."""
    region = _content_hash_region()
    assert "def _content_hash(self, content" in region
    assert "verified_names" in region, "cache key must include verified_names"
    assert "brand_name" in region, "cache key must include brand_name"


def test_content_hash_no_longer_truncates_prefix():
    """The 8000-char prefix hashing must be gone from _content_hash."""
    region = _content_hash_region()
    assert "content[:8000]" not in region, "must hash full content, not 8000-char prefix"


def test_content_hash_key_includes_version():
    region = _content_hash_region()
    assert "_LLM_CHECK_CACHE_VERSION" in region, "cache key must include model/prompt version"
    assert "_LLM_CHECK_CACHE_VERSION" in SRC


def test_batch_llm_check_callsites_pass_brand_context():
    """Both cache lookup and cache write must pass brand context into _content_hash."""
    # cache-lookup call site
    assert "self._content_hash(content, verified_names, brand_name)" in SRC, (
        "cache-lookup call site must pass verified_names + brand_name"
    )
    # cache-write call site
    assert "self._content_hash(art.get('content', ''), verified_names, brand_name)" in SRC, (
        "cache-write call site must pass verified_names + brand_name"
    )
    # no bare content-only invocation should remain
    bare = [m for m in re.findall(r"self\._content_hash\([^\n]*", SRC)
            if "verified_names" not in m]
    assert not bare, f"found content-only _content_hash call site(s): {bare}"


# ---------- pure-function behavior test (no DB/app) ----------

def test_different_brands_get_distinct_cache_keys():
    """Same content, different verified_names -> different cache key (brand isolation)."""
    from services.placement_service import PlacementService

    svc = PlacementService.__new__(PlacementService)  # skip __init__ (no DB)
    content = "揭阳某某牙科排行榜 TOP10 综合评分 95.5分 第一名"

    key_brand_a = svc._content_hash(content, ["揭阳康贝口腔", "揭阳美莱齿科"], "品牌A")
    key_brand_b = svc._content_hash(content, ["揭阳仁爱医院", "揭阳华美口腔"], "品牌B")

    assert key_brand_a != key_brand_b, (
        "identical content with different verified_names must not collide"
    )


def test_same_brand_same_content_stable_key():
    from services.placement_service import PlacementService

    svc = PlacementService.__new__(PlacementService)
    content = "some article body " * 500  # >8000 chars, ensures full-content hashing works
    k1 = svc._content_hash(content, ["A公司", "B公司"], "品牌X")
    k2 = svc._content_hash(content, ["B公司", "A公司"], "品牌X")  # order-independent
    assert k1 == k2, "verified_names order must not change the key"


def test_full_content_matters_beyond_8000_chars():
    """Content differing only after char 8000 must produce different keys now."""
    from services.placement_service import PlacementService

    svc = PlacementService.__new__(PlacementService)
    base = "x" * 8000
    k1 = svc._content_hash(base + "AAA", ["A"], "b")
    k2 = svc._content_hash(base + "BBB", ["A"], "b")
    assert k1 != k2, "content past 8000 chars must affect the hash"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
