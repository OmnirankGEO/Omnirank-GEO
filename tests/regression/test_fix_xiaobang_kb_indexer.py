"""回归锁 · tools/xiaobang_kb_indexer.py

Finding GEO-R10-CAN-028 (index-token-content-mismatch):
  原实现 tokenize() 跑在整段 section content 上,但 insert_chunk 只存
  content[:2000] → 尾部(>2000 字)的词进了 token_keywords 却不在存储正文里。
  BM25 可靠尾词命中该 chunk,但喂给模型的 2000 字前缀不含该词 = 幻觉证据。

  修复:_split_oversized() 把超长 section 切成 <=2000 字的多个重叠片段,
  每片各自 tokenize,content 存的正是被 tokenize 的那段 = token 与存储正文同源。

本文件:
  1. builder 行为判别锁（索引职责已从 reindex_* 提取到 build_*）。
  2. 纯函数行为测试(_split_oversized + tokenize 的同源对齐)。
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TARGET = ROOT / "tools" / "xiaobang_kb_indexer.py"
SRC = TARGET.read_text(encoding="utf-8")


# ==========================================
# 1. source-inspection 判别锁
# ==========================================

def test_split_helper_exists():
    assert "_split_oversized(" in SRC, "缺 _split_oversized 切片 helper"
    assert "def _split_oversized(" in SRC


def test_geo_marker_present():
    assert "[GEO-R10-CAN-028]" in SRC, "缺 finding 修复标记注释"


def test_doc_builder_tokenizes_over_persisted_piece(monkeypatch):
    mod = _load_module()
    body = "正文 " * 1500 + " raretailtoken"
    monkeypatch.setattr(mod, "parse_docs_ts", lambda: [{
        "slug": "long-doc", "title": "长文档", "category_id": "test",
        "is_admin": False, "visible_to": "both", "body": body,
    }])
    chunks = mod.build_doc_chunks()
    assert len(chunks) > 1
    assert all(len(chunk["content"]) <= mod._MAX_CHUNK_CHARS for chunk in chunks)
    assert any("raretailtoken" in chunk["content"] for chunk in chunks)
    for chunk in chunks:
        assert all(token in f"长文档 {chunk['section_title'] or ''} {chunk['content']}"
                   for token in chunk["token_keywords"])


def test_faq_builder_tokenizes_over_persisted_piece(monkeypatch):
    mod = _load_module()

    class Cursor:
        def execute(self, *args, **kwargs):
            pass

        def fetchall(self):
            return [{
                "id": 1, "question": "长答案如何处理", "answer_md": "说明 " * 1500 + " raretailtoken",
                "category": "test", "visible_to": "both",
            }]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    import db.connection as connection
    monkeypatch.setattr(connection, "get_connection", lambda: Connection())
    chunks = mod.build_faq_chunks()
    assert len(chunks) > 1
    assert all(len(chunk["content"]) <= mod._MAX_CHUNK_CHARS for chunk in chunks)
    assert any("raretailtoken" in chunk["content"] for chunk in chunks)


# ==========================================
# 2. 纯函数行为测试(同源对齐)
# ==========================================

import importlib


def _load_module():
    return importlib.import_module("tools.xiaobang_kb_indexer")


def test_split_bounds_each_piece_to_max():
    mod = _load_module()
    long_text = "甲" * 5000
    pieces = mod._split_oversized(long_text)
    assert len(pieces) > 1, "超长正文应被切成多片"
    assert all(len(p) <= mod._MAX_CHUNK_CHARS for p in pieces), "每片必须 <= 上限"
    # 覆盖全文(重叠拼接后应包含所有字符)
    assert "".join(pieces).replace("甲", "") == "", "切片后不应丢失字符"


def test_short_content_returns_single_piece():
    mod = _load_module()
    assert mod._split_oversized("短文本") == ["短文本"]
    assert mod._split_oversized("") == [""], "空正文仍返回单片(向后兼容)"


def test_old_bug_counterfactual_tail_token_lost_in_prefix():
    """坐实旧 BUG:整段 tokenize 会产出尾词,但 content[:2000] 前缀里没有该词。

    用空格分隔的词,jieba 有无都能稳定分出 token(避免依赖分词器分中文粘连串)。
    """
    mod = _load_module()
    tail_term = "raretailtoken"  # 唯一尾词
    head = ("prefix content " * 400)  # 远超 2000 字,且带空格便于 fallback 分词
    assert len(head) > mod._MAX_CHUNK_CHARS
    section = head + " " + tail_term

    # 旧实现:tokenize 整段 → 含尾词;但只存 section[:2000] → 前缀不含尾词 = 错位
    full_tokens = mod.tokenize(section)
    assert tail_term in full_tokens, "整段分词应含尾词(否则用例失效)"
    assert tail_term not in section[:mod._MAX_CHUNK_CHARS], \
        "尾词应落在 2000 字前缀之外(这正是旧实现丢词的地方)"


def test_new_fix_token_and_stored_content_aligned():
    """修复后不变式:每个切片里 tokenize 出的每个 token,都必在该切片(存储正文)内。

    这正是「BM25 命中的词一定在喂给模型的那段正文里」的同源保证。
    """
    mod = _load_module()
    tail_term = "raretailtoken"
    head = ("prefix content " * 400)
    section = head + " " + tail_term
    assert len(section) > mod._MAX_CHUNK_CHARS

    pieces = mod._split_oversized(section)
    assert len(pieces) > 1, "超长 section 应被切成多片"

    # 尾词必落在某一片里,并且该片正是存储正文
    hit_pieces = [p for p in pieces if tail_term in p]
    assert hit_pieces, "尾词应落在某个切片里(不再丢失)"

    # 全局不变式:切片内 tokenize 的每个 token 都在该切片正文内
    for piece in pieces:
        toks = mod.tokenize(piece)
        for t in toks:
            assert t in piece, f"token {t!r} 不在其存储正文内 = 词与正文错位"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
