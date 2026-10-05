"""
判别性回归锁 · api/knowledge_api.py GEO 缺陷修复(5 条)

主形态 = source-inspection:读源码文本断言修复标志存在。
回退任一修复 → 对应断言失败。不依赖 DB / 不 import server.py。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_SRC = (ROOT / "api" / "knowledge_api.py").read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    """截取 start_marker 到 end_marker 之间的源码片段(handler 区域)。"""
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def test_admin_helper_exists():
    """管理员闸 helper 存在,且 fail-closed(not is_admin -> 403)。"""
    assert "def _require_admin(request: Request)" in _SRC
    helper = _slice(_SRC, "def _require_admin(", "\n\n\n")
    assert 'user.get("is_admin")' in helper
    assert "status_code=403" in helper


def test_can054_generic_upload_public_role_gated():
    """CAN-054: upload_file_document 非 client 分支写入前有 admin 闸。"""
    region = _slice(
        _SRC,
        'async def upload_file_document(',
        'async def reindex_client_documents(',
    )
    # client 分支之后、rag.add_document 之前必须有 _require_admin
    tail = region[region.index("return _client_document_response") :]
    assert "_require_admin(request)" in tail, "非 client 分支缺 admin 闸"
    assert tail.index("_require_admin(request)") < tail.index("rag.add_document"), \
        "admin 闸必须在 rag.add_document 之前"
    assert "GEO-R1-CAN-054" in region


def test_can057_list_knowledge_role_public_gated():
    """CAN-057: list_knowledge 的 role/public(else)分支有 admin 闸。"""
    region = _slice(
        _SRC,
        'async def list_knowledge(',
        'async def delete_knowledge(',
    )
    assert "else:" in region
    assert "_require_admin(request)" in region
    assert "GEO-R1-CAN-057" in region


def test_can126_list_role_documents_gated():
    """CAN-126: list_role_documents 注入 Request 并加 admin 闸。"""
    region = _slice(
        _SRC,
        'async def list_role_documents(',
        'async def upload_role_document(',
    )
    assert "http_request: Request" in region, "缺 Request 参数注入"
    assert "_require_admin(http_request)" in region
    assert "GEO-R1-CAN-126" in region


def test_can055_upload_size_limit():
    """CAN-055: 上传体积/文本上限常量存在,且解析前先卡原始字节。"""
    assert "KB_UPLOAD_MAX_BYTES" in _SRC
    assert "KB_EXTRACTED_TEXT_MAX_CHARS" in _SRC
    region = _slice(
        _SRC,
        'async def upload_file_document(',
        'async def reindex_client_documents(',
    )
    # 体积检查(413)必须出现在 _extract_text_from_bytes 之前
    assert "status_code=413" in region
    assert region.index("KB_UPLOAD_MAX_BYTES") < region.index("_extract_text_from_bytes"), \
        "体积上限检查必须在文本解析之前"
    assert "GEO-R1-CAN-055" in region


def test_can064_stats_admin_gated():
    """CAN-064: get_knowledge_stats 注入 Request 并加 admin 闸。"""
    region = _slice(
        _SRC,
        'async def get_knowledge_stats(',
        'async def list_knowledge(',
    )
    assert "request: Request" in region, "缺 Request 参数注入"
    assert "_require_admin(request)" in region
    assert region.index("_require_admin(request)") < region.index("rag.get_stats()"), \
        "admin 闸必须在 get_stats 之前"
    assert "GEO-R1-CAN-064" in region


def test_source_compiles():
    """整文件可编译(语法自检)。"""
    import py_compile
    py_compile.compile(str(ROOT / "api" / "knowledge_api.py"), doraise=True)
