"""
Regression lock for GEO-R1-CAN-147 — knowledge-path-traversal-write.

_resolve_target_dir joined caller-controlled kb_id / role_type raw into the
filesystem path, so an absolute kb_id (e.g. '/tmp/pwn') reset the pathlib join
to that absolute dir and '..' segments escaped data/knowledge -> arbitrary
authenticated file write via /upload-file. add_document also joined `filename`
raw. The fix hardens _resolve_target_dir (component validation + containment)
and basenames the filename in add_document.

Primary form = source-inspection discriminative lock: reverting the fix makes
these assertions fail. A pure-behavior check on _resolve_target_dir is added
(no DB, no server import).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "tools" / "unified_knowledge.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


# ---------------- source-inspection discriminative locks ----------------

def test_resolve_target_dir_validates_components():
    """_resolve_target_dir must reject unsafe components and enforce containment."""
    assert "_is_safe_component(" in SRC, "component validator missing"
    assert "_assert_contained(" in SRC, "containment guard missing"
    # containment uses relative_to on resolved paths -> raises on escape
    assert "relative_to(" in SRC


def test_resolve_target_dir_client_role_guarded():
    """Both client and role branches must call the validators before returning."""
    # isolate the _resolve_target_dir body
    m = re.search(r"def _resolve_target_dir\(.*?\n(?=    def |    @staticmethod)", SRC, re.DOTALL)
    body = m.group(0) if m else SRC
    # client branch guards kb_id
    assert body.count("_is_safe_component(kb_id)") >= 2, "kb_id not validated in both branches"
    assert "_is_safe_component(role_type)" in body, "role_type not validated"
    assert body.count("_assert_contained(") >= 2, "containment not enforced in both branches"


def test_add_document_basenames_filename():
    """add_document must basename the caller filename, not join it raw."""
    assert "file_path = target_dir / filename" not in SRC, "raw filename join still present"
    assert "safe_name" in SRC and "file_path = target_dir / safe_name" in SRC


def test_marker_present():
    assert "[GEO-R1-CAN-147]" in SRC


# ---------------- pure-behavior check (no DB / no server) ----------------

def test_behavior_rejects_traversal():
    from tools.unified_knowledge import UnifiedKnowledgeRAG

    rag = UnifiedKnowledgeRAG()

    # absolute kb_id must be rejected (would reset the pathlib join)
    abs_id = "C:/Windows/Temp/pwn" if sys.platform.startswith("win") else "/tmp/pwn"
    target, err = rag._resolve_target_dir("role", kb_id=abs_id, role_type="advisor")
    assert target is None and err, "absolute kb_id not rejected"

    # '..' escape must be rejected
    target, err = rag._resolve_target_dir("client", kb_id="../../etc")
    assert target is None and err, "'..' kb_id not rejected"

    # malicious role_type must be rejected
    target, err = rag._resolve_target_dir("role", kb_id="ok", role_type="../../evil")
    assert target is None and err, "malicious role_type not rejected"

    # a normal id must still resolve and stay contained
    target, err = rag._resolve_target_dir("client", kb_id="123")
    assert err is None and target is not None
    assert str(target.resolve()).startswith(str(rag.clients_path.resolve()))

    target, err = rag._resolve_target_dir("role", kb_id="huang-douyin", role_type="advisor")
    assert err is None and target is not None
    assert str(target.resolve()).startswith(str(rag.roles_path.resolve()))
