"""
P14.6 (2026-06-01) · crawler.py JINA_HTTP_PROXY env 支持
生产服务器无法直连 r.jina.ai (Errno 101) · 通过本地 Xray 出境
env JINA_HTTP_PROXY=http://127.0.0.1:10809 → Jina httpx 走代理
env 不设 → None → 直连 (本地开发不变)

锁:
  1. (inspect) crawler.py 必须有 JINA_HTTP_PROXY 模块常量
  2. (inspect) crawl_article 的 httpx.AsyncClient 必须传 proxy=JINA_HTTP_PROXY
  3. (functional) env 不设 → 常量 None
  4. (functional) env 设了 → 常量 = env 值
  5. (functional) env 空 / 空格 → 常量 None (safe parse)
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestCrawlerJinaProxySource:
    """inspect 锁 · 防 proxy 参数被偷偷撤回"""

    def test_crawler_has_jina_http_proxy_constant(self):
        src = (ROOT / "services" / "research_monitor" / "crawler.py").read_text(encoding="utf-8")
        # 跳过注释行 · 防注释里写"JINA_HTTP_PROXY"被误判
        non_comment_src = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        assert "JINA_HTTP_PROXY" in non_comment_src, \
            "crawler.py 必须有 JINA_HTTP_PROXY 模块常量"
        assert re.search(
            r"JINA_HTTP_PROXY\s*=\s*os\.getenv\(['\"]JINA_HTTP_PROXY['\"]",
            non_comment_src,
        ), "JINA_HTTP_PROXY 必须从 env 读"

    def test_crawl_article_passes_proxy_to_client(self):
        """httpx.AsyncClient 必须传 proxy=JINA_HTTP_PROXY · 不能裸 client"""
        src = (ROOT / "services" / "research_monitor" / "crawler.py").read_text(encoding="utf-8")
        non_comment_src = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        # 容忍 proxy= 或 proxies= 写法 (httpx 0.26+ 推荐 proxy 单数)
        # 注:不能用 [^)] · AsyncClient(...) 内部可能嵌套 httpx.Limits(...) 等带 ) 表达式
        assert re.search(
            r"httpx\.AsyncClient\([\s\S]*?prox(?:y|ies)\s*=\s*JINA_HTTP_PROXY",
            non_comment_src,
        ), "httpx.AsyncClient 必须含 proxy=JINA_HTTP_PROXY 参数"


class TestJinaProxyEnvParsing:
    """env 解析 · safe defaults"""

    def _reload(self):
        sys.modules.pop("services.research_monitor.crawler", None)
        return importlib.import_module("services.research_monitor.crawler")

    def test_unset_env_returns_none(self, monkeypatch):
        monkeypatch.delenv("JINA_HTTP_PROXY", raising=False)
        mod = self._reload()
        assert mod.JINA_HTTP_PROXY is None, "env 不设 → None · 本地开发不变"

    def test_empty_env_returns_none(self, monkeypatch):
        monkeypatch.setenv("JINA_HTTP_PROXY", "")
        mod = self._reload()
        assert mod.JINA_HTTP_PROXY is None, "env 空串 → None · 防 httpx 拒接空 proxy"

    def test_whitespace_env_returns_none(self, monkeypatch):
        monkeypatch.setenv("JINA_HTTP_PROXY", "   ")
        mod = self._reload()
        assert mod.JINA_HTTP_PROXY is None, "env 纯空格 → None"

    def test_valid_proxy_url_returned(self, monkeypatch):
        monkeypatch.setenv("JINA_HTTP_PROXY", "http://127.0.0.1:10809")
        mod = self._reload()
        assert mod.JINA_HTTP_PROXY == "http://127.0.0.1:10809"

    def test_trim_whitespace(self, monkeypatch):
        monkeypatch.setenv("JINA_HTTP_PROXY", "  http://127.0.0.1:10809  ")
        mod = self._reload()
        assert mod.JINA_HTTP_PROXY == "http://127.0.0.1:10809"
