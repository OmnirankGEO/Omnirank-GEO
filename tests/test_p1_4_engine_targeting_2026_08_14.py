# -*- coding: utf-8 -*-
"""P1-4 · 引擎定向(复用 geo_douyin 先例) · 判别锁(2026-08-14)。

变异点:
  M1 拆键 SSOT(engine_targeting 自造键名)→ test_engine_keys_join_monitoring_contract 红;
  M2 拆生成注入(server 不给 topic 写 target_engine)→ test_generation_injection_wiring 红;
  M3 拆归一(非法值原样透传)→ test_invalid_engine_normalized_empty 红;
  M4 拆前端入口 → test_frontend_selector_wired 红。
"""
from __future__ import annotations

from pathlib import Path

from writing.engine_targeting import (
    available_target_engines,
    normalize_target_engine,
    target_engine_label,
)

ROOT = Path(__file__).resolve().parents[1]


def test_engine_keys_derived_from_ssot(monkeypatch) -> None:
    """🔴 R2-9:写作层不是第二份目录 —— 键集合从监测 SSOT **派生**。

    判据不是「两边集合相等」(那只在测试运行时成立):**模拟 SSOT 增删一个
    引擎,写作层必须自动跟随**;新引擎无显示名时回落键名(引擎不丢)。"""
    import services.monitoring_lineage as ml

    assert set(available_target_engines()) == set(ml._PLATFORM_CONTRACT)
    # SSOT 增一个引擎 → 写作层自动出现(显示名回落键名)
    patched = dict(ml._PLATFORM_CONTRACT)
    patched["newengine"] = {"provider": "newengine", "model": "x", "surface": "ai_search"}
    monkeypatch.setattr(ml, "_PLATFORM_CONTRACT", patched)
    engines = available_target_engines()
    assert "newengine" in engines and engines["newengine"] == "newengine"
    assert normalize_target_engine("newengine") == "newengine"
    # SSOT 删一个引擎 → 写作层同步失效(归一拒收,A1 归空)
    reduced = dict(ml._PLATFORM_CONTRACT)
    reduced.pop("doubao")
    monkeypatch.setattr(ml, "_PLATFORM_CONTRACT", reduced)
    assert "doubao" not in available_target_engines()
    assert normalize_target_engine("doubao") == ""


def test_no_second_directory_in_source() -> None:
    """反向锁:engine_targeting 源码里不存在手写键目录(只许显示名适配器)。"""
    src = (ROOT / "writing/engine_targeting.py").read_text(encoding="utf-8")
    assert "_PLATFORM_CONTRACT" in src, "派生源丢了"
    assert "ARTICLE_TARGET_ENGINES" not in src, "第二份手写目录回潮"


def test_normalize_aliases() -> None:
    assert normalize_target_engine("豆包") == "doubao"
    assert normalize_target_engine("qwen") == "dashscope"
    assert normalize_target_engine("DOUBAO") == "doubao"
    assert target_engine_label("doubao") == "豆包"


def test_invalid_engine_normalized_empty() -> None:
    # 反向对照(A1):非法/空 → ""(不定向走现行为),绝不透传垃圾值进 lineage
    assert normalize_target_engine("gpt-4") == ""
    assert normalize_target_engine("") == ""
    assert normalize_target_engine(None) == ""
    assert target_engine_label("gpt-4") == ""


def test_migration_registered() -> None:
    assert (ROOT / "scripts/migration_quotes_target_engine_2026_08_14.sql").exists()
    manifest = (ROOT / "db/migration_manifest.py").read_text(encoding="utf-8")
    assert "scripts/migration_quotes_target_engine_2026_08_14.sql" in manifest
    # 运行时兜底(双保险,同 user_choice 先例)
    ddb = (ROOT / "db/diagnosis_db.py").read_text(encoding="utf-8")
    assert '("target_engine", "VARCHAR(32) DEFAULT NULL")' in ddb


def test_generation_injection_wiring() -> None:
    """接线锁:生成链把 quote 现值归一后注入每个 topic(继承语义 = 取现值)。"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "_t['target_engine'] = _quote_target_engine" in src
    assert '_norm_te((quote or {}).get("target_engine"))' in src, (
        "注入没走归一 —— 库里脏值会原样进 lineage"
    )


def test_lineage_snapshot_reads_topic() -> None:
    """快照语义:lineage 按篇记录生成当时的 topic 值(ags:2475 既有读点)。"""
    ags = (ROOT / "writing/article_generator_service.py").read_text(encoding="utf-8")
    assert 'target_engine=str(topic.get("target_engine") or "")' in ags


def test_endpoint_exists_with_access_guard() -> None:
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    marker = '@app.post("/api/writing/projects/{quote_id}/target-engine")'
    assert marker in src
    body = src[src.index(marker):src.index(marker) + 2500]
    assert "require_quote_access(request, quote_id)" in body, "端点没有品牌归属校验"
    assert "INVALID_TARGET_ENGINE" in body, "非法值必须 422 而不是静默归空"


def test_frontend_selector_wired() -> None:
    wh = (ROOT / "frontend/src/pages/Writing/WritingHall.tsx").read_text(encoding="utf-8")
    assert "/target-engine" in wh, "新端点没有前端入口(死功能)"
    assert 'value="doubao"' in wh and 'value="dashscope"' in wh
    assert "不定向（默认）" in wh, "缺省档(不定向)没暴露 —— 用户设了就退不回现行为"
