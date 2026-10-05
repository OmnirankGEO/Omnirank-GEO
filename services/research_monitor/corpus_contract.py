"""Versioned Jina corpus boundary and conservative quality grading.

JC grades are data-provenance states, not article-quality or citation scores:
JC0 unknown legacy, JC1 raw fetch persisted, JC3 cleaned canonical body persisted,
JC5 outcome-linked direct observation.  Only an explicit outcome verifier may
promote JC3 to JC5; the crawler deliberately cannot do that.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
import unicodedata
from typing import Final


BODY_BOUNDARY_VERSION: Final = "jina-cleaned-markdown-body-v1"
BODY_HASH_ALGORITHM: Final = "sha256-nfkc-lf-rstrip-v1"
RAW_BOUNDARY_VERSION: Final = "jina-reader-markdown-raw-v1"
CORPUS_CONTRACT_VERSION: Final = "jcc-v1.0"


@dataclass(frozen=True)
class CorpusFingerprint:
    corpus_grade: str
    canonical_body_hash: str
    body_hash_algorithm: str
    content_cluster_id: str
    body_boundary_version: str
    label_provenance_version: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


def canonicalize_body(markdown: str) -> str:
    """Return the exact versioned body used for hashing and leakage splits."""
    text = unicodedata.normalize("NFKC", str(markdown or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{4,}", "\n\n\n", text)


def fingerprint_clean_body(markdown: str) -> CorpusFingerprint:
    body = canonicalize_body(markdown)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return CorpusFingerprint(
        corpus_grade="JC3" if body else "JC0",
        canonical_body_hash=digest if body else "",
        body_hash_algorithm=BODY_HASH_ALGORITHM,
        content_cluster_id=f"body:{digest}" if body else "",
        body_boundary_version=BODY_BOUNDARY_VERSION,
        label_provenance_version="unlabeled",
    )


def validate_jc5_promotion(*, direct_signal_count: int, lineage_complete: bool) -> None:
    """Fail closed before a separate verifier promotes a body to JC5."""
    if int(direct_signal_count or 0) < 1:
        raise ValueError("jc5_requires_direct_observation")
    if not lineage_complete:
        raise ValueError("jc5_requires_complete_lineage")
