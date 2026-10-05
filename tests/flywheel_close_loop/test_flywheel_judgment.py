"""B 段判断层判别锁。

工单硬要求:"所有 LLM 判断点必须有 provider 全挂 → 规则兜底的判别测试"。
这一组把五条通用约束逐条钉住:多 provider 回落 / 每日上限 / 留痕 / advisory / 兜底必然存在。
"""
from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

import pytest

from db.connection import get_db
from db.flywheel_judgment_db import SOURCE_LLM, SOURCE_RULE, init_flywheel_judgment_tables
from services.flywheel_judgment import JUDGMENT_POINTS, judge

pytestmark = pytest.mark.integration

POINT = "writing_strategy_selection"


@pytest.fixture
def judgment_db(monkeypatch):
    init_flywheel_judgment_tables(force=True)
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM flywheel_judgment_log")
    monkeypatch.setenv("FLYWHEEL_JUDGE_ALL", "1")  # 默认关,测试里显式打开
    yield
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM flywheel_judgment_log")


def _logs(point_key: str = POINT) -> list[dict]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM flywheel_judgment_log WHERE point_key=%s ORDER BY id", (point_key,)
        )
        return [dict(r) for r in cur.fetchall() or []]


def test_all_providers_down_falls_back_to_rule(judgment_db, monkeypatch):
    """🔒 判别锁:provider 全挂 → 规则兜底,系统行为不变,只是少一层智能。"""
    monkeypatch.setattr(
        "services.flywheel_judgment._call_one",
        lambda provider, model, prompt, point: {"ok": False, "error": "connection refused"},
    )
    result = judge(
        POINT, prompt="x", rule_fallback=lambda: {"style_family": "guide"},
        input_summary={"case": "all_down"},
    )
    assert result.source == SOURCE_RULE
    assert result.payload == {"style_family": "guide"}
    assert result.fallback_reason == "all_providers_failed"

    rows = _logs()
    assert len(rows) == 1, "兜底那次也必须留痕,否则事后无从复核"
    assert rows[0]["source"] == SOURCE_RULE
    assert "connection refused" in (rows[0]["error"] or "")


def test_falls_through_chain_until_one_succeeds(judgment_db, monkeypatch):
    """禁单点:第一家挂了要自动换下一家,不许直接躺平兜底。"""
    attempts: list[tuple[str, str]] = []

    def _fake(provider, model, prompt, point):
        attempts.append((provider, model))
        if len(attempts) < 3:
            return {"ok": False, "error": "boom"}
        return {"ok": True, "text": '{"style_family": "guide"}',
                "input_tokens": 10, "output_tokens": 5, "cost_cny": 0.001}

    monkeypatch.setattr("services.flywheel_judgment._call_one", _fake)
    result = judge(POINT, prompt="x", rule_fallback=lambda: {"style_family": "fallback"})

    assert result.source == SOURCE_LLM
    assert result.payload["style_family"] == "guide"
    assert len(attempts) == 3
    assert attempts[0][0] == "deepseek", "首选必须是 Owner 指定的 deepseek 通道"


def test_daily_call_cap_downgrades_to_rule(judgment_db, monkeypatch):
    """超日调用上限 → 立刻降级规则兜底,不再花钱。"""
    point = JUDGMENT_POINTS[POINT]
    monkeypatch.setattr(
        "services.flywheel_judgment.today_usage",
        lambda key: {"available": True, "calls": point.daily_call_cap, "cost_cny": 0.0, "tokens": 0},
    )
    called: list[int] = []
    monkeypatch.setattr(
        "services.flywheel_judgment._call_one",
        lambda *a, **kw: called.append(1) or {"ok": True, "text": "{}"},
    )
    result = judge(POINT, prompt="x", rule_fallback=lambda: {"style_family": "rule"})
    assert result.source == SOURCE_RULE
    assert "daily_call_cap_reached" in (result.fallback_reason or "")
    assert called == [], "超限之后不许再发起任何一次 LLM 调用"


def test_daily_cost_cap_downgrades_to_rule(judgment_db, monkeypatch):
    point = JUDGMENT_POINTS[POINT]
    monkeypatch.setattr(
        "services.flywheel_judgment.today_usage",
        lambda key: {"available": True, "calls": 0,
                     "cost_cny": point.daily_cost_cap_cny, "tokens": 0},
    )
    result = judge(POINT, prompt="x", rule_fallback=lambda: {"style_family": "rule"})
    assert result.source == SOURCE_RULE
    assert "daily_cost_cap_reached" in (result.fallback_reason or "")


def test_unknown_usage_is_treated_as_over_budget(judgment_db, monkeypatch):
    """用量查不出来 → 按超限处理。绝不能查不到就当零花费接着刷。"""
    monkeypatch.setattr(
        "services.flywheel_judgment.today_usage",
        lambda key: {"available": False, "calls": 0, "cost_cny": 0.0, "tokens": 0},
    )
    result = judge(POINT, prompt="x", rule_fallback=lambda: {"style_family": "rule"})
    assert result.source == SOURCE_RULE
    assert result.fallback_reason == "budget_unknown"


def test_disabled_point_never_calls_llm(judgment_db, monkeypatch):
    """判断点默认关:B 段随 A 段部署但不自动开始花钱,由 Owner 逐点打开。

    关闭态**不留痕** —— B3 挂在发布推荐热路径上,每次都记一行会把留痕表刷成噪音。
    """
    monkeypatch.delenv("FLYWHEEL_JUDGE_ALL", raising=False)
    monkeypatch.delenv(f"FLYWHEEL_JUDGE_{POINT.upper()}", raising=False)
    called: list[int] = []
    monkeypatch.setattr(
        "services.flywheel_judgment._call_one",
        lambda *a, **kw: called.append(1) or {"ok": True, "text": "{}"},
    )
    result = judge(POINT, prompt="x", rule_fallback=lambda: {"style_family": "rule"})
    assert result.source == SOURCE_RULE
    assert result.fallback_reason == "point_disabled"
    assert called == []
    assert _logs() == [], "关闭态不该往留痕表写东西"


def test_invalid_structure_is_rejected_and_logged(judgment_db, monkeypatch):
    """输出结构不合预期 → 不喂给下游,同时留痕供 prompt 回归。"""
    monkeypatch.setattr(
        "services.flywheel_judgment._call_one",
        lambda *a, **kw: {"ok": True, "text": '{"style_family": 123}',
                          "input_tokens": 1, "output_tokens": 1, "cost_cny": 0.0},
    )
    result = judge(
        POINT, prompt="x", rule_fallback=lambda: {"style_family": "rule"},
        validate=lambda p: isinstance(p.get("style_family"), str),
    )
    assert result.source == SOURCE_RULE
    rows = _logs()
    llm_rows = [r for r in rows if r["source"] == SOURCE_LLM]
    assert llm_rows and llm_rows[0]["error"] == "unparseable_or_invalid_structure"


def test_successful_judgment_records_full_audit_trail(judgment_db, monkeypatch):
    monkeypatch.setattr(
        "services.flywheel_judgment._call_one",
        lambda provider, model, prompt, point: {
            "ok": True, "text": '```json\n{"style_family": "guide"}\n```',
            "input_tokens": 120, "output_tokens": 30, "cost_cny": 0.0042,
        },
    )
    judge(POINT, prompt="x", rule_fallback=lambda: {}, input_summary={"industry_key": "geo_test"})
    row = _logs()[0]
    assert row["source"] == SOURCE_LLM
    assert row["provider"] == "deepseek"
    # 🔴 [WO_206 c1c 翻面 2026-09-14] 原来这里写死 "deepseek-v4-flash"。
    #    官方 2026-09-13 把 Flash 档改名 deepseek-flash,Deploy 206-d2 实打:
    #    官方 /models 只剩 deepseek-flash 与 deepseek-v4-pro。继续钉旧名,
    #    这条判据就会和**正确的修法互斥**。改成取常量:本判据守的那件事一个字没变,
    #    而「既定模型叫什么」只剩一个出处(config/deepseek_models)。
    assert row["model"] == DEEPSEEK_OFFICIAL_FLASH
    assert row["prompt_version"]
    assert row["input_summary"]["industry_key"] == "geo_test"
    assert row["output"]["style_family"] == "guide"
    assert row["input_tokens"] == 120 and row["output_tokens"] == 30
    assert float(row["cost_cny"]) == pytest.approx(0.0042)


def test_unregistered_point_is_rejected():
    """新判断点必须先进注册表(否则没有上限、没有归属,等于裸奔花钱)。"""
    with pytest.raises(KeyError):
        judge("not_a_real_point", prompt="x", rule_fallback=lambda: {})


def test_every_point_has_caps():
    for key, point in JUDGMENT_POINTS.items():
        assert point.daily_call_cap > 0, f"{key} 缺日调用上限"
        assert point.daily_cost_cap_cny > 0, f"{key} 缺日成本上限"
        assert len(point.model_chain) >= 2, f"{key} 只有单一 provider,违反禁单点"
