"""
P14.4 C1 (2026-06) · round_runner.py env 非数字防呆
之前 int(os.getenv('JINA_RETRY_MAX_ATTEMPTS', '2')) 这种写法
env 写成 "abc" / "" 会在 import 期 ValueError → backend 起不来

锁:
  1. _safe_int_env / _safe_float_env 非法值走默认 + log warning
  2. 整个 round_runner 模块在 env 污染状态下仍能 import (不抛)
"""
from __future__ import annotations

import importlib
import logging
import os
import sys

import pytest


@pytest.fixture
def fresh_round_runner_env(monkeypatch):
    """每个 test 清理所有 JINA 相关 env · 防互相污染"""
    for k in ['JINA_CONCURRENCY', 'JINA_RETRY_MAX_ATTEMPTS',
              'JINA_RETRY_DELAY_SECONDS']:
        monkeypatch.delenv(k, raising=False)


class TestSafeIntEnv:
    """_safe_int_env helper · 直接调用层"""

    def _get(self):
        from services.research_monitor.round_runner import _safe_int_env
        return _safe_int_env

    def test_empty_returns_default(self, monkeypatch, fresh_round_runner_env):
        monkeypatch.setenv('TEST_X', '')
        assert self._get()('TEST_X', 7) == 7

    def test_unset_returns_default(self, fresh_round_runner_env):
        os.environ.pop('TEST_X', None)
        assert self._get()('TEST_X', 7) == 7

    def test_valid_int_returned(self, monkeypatch):
        monkeypatch.setenv('TEST_X', '42')
        assert self._get()('TEST_X', 7) == 42

    def test_garbage_returns_default_no_raise(self, monkeypatch, caplog):
        monkeypatch.setenv('TEST_X', 'abc')
        with caplog.at_level(logging.WARNING):
            v = self._get()('TEST_X', 7)
        assert v == 7
        assert any('TEST_X' in rec.message and 'abc' in rec.message
                   for rec in caplog.records), \
            f"应该 log warning · 但没找到: {[r.message for r in caplog.records]}"

    def test_below_minimum_clamps(self, monkeypatch, caplog):
        monkeypatch.setenv('TEST_X', '0')
        with caplog.at_level(logging.WARNING):
            v = self._get()('TEST_X', 7, minimum=1)
        assert v == 1
        assert any('clamp' in rec.message.lower() for rec in caplog.records)

    def test_float_string_treated_as_garbage(self, monkeypatch):
        monkeypatch.setenv('TEST_X', '3.14')
        v = self._get()('TEST_X', 99)
        assert v == 99, "_safe_int_env 不应接受 '3.14' · 应走默认"


class TestSafeFloatEnv:
    def _get(self):
        from services.research_monitor.round_runner import _safe_float_env
        return _safe_float_env

    def test_empty_returns_default(self, monkeypatch, fresh_round_runner_env):
        monkeypatch.setenv('TEST_X', '')
        assert self._get()('TEST_X', 2.5) == 2.5

    def test_valid_float(self, monkeypatch):
        monkeypatch.setenv('TEST_X', '3.14')
        assert self._get()('TEST_X', 2.5) == 3.14

    def test_int_string_works(self, monkeypatch):
        monkeypatch.setenv('TEST_X', '5')
        assert self._get()('TEST_X', 2.5) == 5.0

    def test_garbage_returns_default_no_raise(self, monkeypatch, caplog):
        monkeypatch.setenv('TEST_X', 'abc')
        with caplog.at_level(logging.WARNING):
            v = self._get()('TEST_X', 2.5)
        assert v == 2.5

    def test_negative_below_minimum_clamps(self, monkeypatch):
        monkeypatch.setenv('TEST_X', '-5')
        v = self._get()('TEST_X', 2.5, minimum=0.0)
        assert v == 0.0


class TestRoundRunnerImportSafe:
    """模块级:即使 env 污染 · round_runner 也必须能 import 不炸 (backend 启动安全)"""

    @pytest.mark.parametrize("env_payload", [
        {'JINA_CONCURRENCY': 'abc'},
        {'JINA_RETRY_MAX_ATTEMPTS': 'not_a_number'},
        {'JINA_RETRY_DELAY_SECONDS': 'nan_string'},
        {'JINA_CONCURRENCY': '', 'JINA_RETRY_MAX_ATTEMPTS': '', 'JINA_RETRY_DELAY_SECONDS': ''},
        {'JINA_RETRY_MAX_ATTEMPTS': '0'},  # 边界:0 应 clamp 到 1
        {'JINA_RETRY_DELAY_SECONDS': '-1.5'},  # 边界:负数应 clamp 到 0.0
    ])
    def test_module_reimports_under_polluted_env(self, env_payload, monkeypatch):
        for k, v in env_payload.items():
            monkeypatch.setenv(k, v)
        # 强制重载 module 走 import 期所有 env 读取
        sys.modules.pop('services.research_monitor.round_runner', None)
        # 不该抛
        mod = importlib.import_module('services.research_monitor.round_runner')

        # 全部常量必须落在安全范围
        assert isinstance(mod.JINA_CONCURRENCY, int)
        assert mod.JINA_CONCURRENCY >= 1, \
            f"JINA_CONCURRENCY 应有正数兜底 · 实际 {mod.JINA_CONCURRENCY}"
        assert isinstance(mod.JINA_RETRY_MAX_ATTEMPTS, int)
        assert mod.JINA_RETRY_MAX_ATTEMPTS >= 1, \
            f"JINA_RETRY_MAX_ATTEMPTS 应 clamp ≥ 1 · 实际 {mod.JINA_RETRY_MAX_ATTEMPTS}"
        assert isinstance(mod.JINA_RETRY_DELAY_SECONDS, float)
        assert mod.JINA_RETRY_DELAY_SECONDS >= 0.0, \
            f"JINA_RETRY_DELAY_SECONDS 应 clamp ≥ 0.0 · 实际 {mod.JINA_RETRY_DELAY_SECONDS}"
