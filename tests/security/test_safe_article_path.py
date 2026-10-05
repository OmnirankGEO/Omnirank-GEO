"""判别性回归测试 · GEO-R1-CAN-016/017/020 P0 路径穿越/任意文件读

锁定:resolve_safe_article_path 必须拒绝 output/articles 根外的任意路径
(尤其绝对路径 /proc/self/environ、/etc/passwd、.env),只放行根内真实文件。

判别性:若把修复回退成旧的"仅当含 '..' 且不以 output/articles 开头才拒"逻辑,
本测试的 test_rejects_absolute_outside_paths_without_dotdot 必然失败
(旧逻辑会放行 /etc/passwd 这类无 '..' 的绝对越界路径)。

纯逻辑测试 · 不依赖 server.py / DB。
跑: pytest tests/security/test_safe_article_path.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.safe_article_path import resolve_safe_article_path


@pytest.fixture
def articles_root(tmp_path):
    """构造一个隔离的 output/articles 根 + 一篇合法文章 + 一个根外敏感文件。"""
    root = tmp_path / "output" / "articles"
    root.mkdir(parents=True)
    valid = root / "brand1" / "a.md"
    valid.parent.mkdir(parents=True)
    valid.write_text("hello", encoding="utf-8")
    # 根外敏感文件(模拟 .env / /etc/passwd)
    secret = tmp_path / "secret.env"
    secret.write_text("DATABASE_URL=postgres://x", encoding="utf-8")
    return {"root": str(root), "valid": str(valid), "secret": str(secret), "base": str(tmp_path)}


class TestRejectsTraversal:
    def test_rejects_absolute_outside_paths_without_dotdot(self, articles_root):
        """核心判别:无 '..' 的绝对越界路径(如 /etc/passwd)必须被拒。
        旧逻辑会放行这类路径 → 任意文件读。"""
        # 根外真实文件,绝对路径,无 '..'
        assert resolve_safe_article_path(articles_root["secret"], articles_root["root"]) is None
        # 系统敏感文件(可能不存在,realpath 仍规范化;不在根内 → None)
        assert resolve_safe_article_path("/etc/passwd", articles_root["root"]) is None
        assert resolve_safe_article_path("/proc/self/environ", articles_root["root"]) is None

    def test_rejects_dotdot_escape(self, articles_root):
        escape = os.path.join(articles_root["root"], "..", "..", "secret.env")
        assert resolve_safe_article_path(escape, articles_root["root"]) is None

    def test_rejects_empty_and_none(self, articles_root):
        assert resolve_safe_article_path("", articles_root["root"]) is None
        assert resolve_safe_article_path(None, articles_root["root"]) is None  # type: ignore

    def test_rejects_sibling_prefix_confusion(self, tmp_path):
        """'output/articles' 前缀混淆:output/articles_evil 不应被当作在 output/articles 内。"""
        root = tmp_path / "output" / "articles"
        root.mkdir(parents=True)
        evil = tmp_path / "output" / "articles_evil"
        evil.mkdir(parents=True)
        f = evil / "x.md"
        f.write_text("x", encoding="utf-8")
        assert resolve_safe_article_path(str(f), str(root)) is None


class TestAllowsValid:
    def test_allows_valid_article_in_root(self, articles_root):
        got = resolve_safe_article_path(articles_root["valid"], articles_root["root"])
        assert got is not None
        assert got == os.path.realpath(articles_root["valid"])

    def test_allows_root_itself(self, articles_root):
        got = resolve_safe_article_path(articles_root["root"], articles_root["root"])
        assert got == os.path.realpath(articles_root["root"])
