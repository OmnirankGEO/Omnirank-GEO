from pathlib import Path
import asyncio


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_token_usage_delegates_to_llm_pricing_ssot():
    """Legacy token_usage cannot keep a second model price table."""
    src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")

    assert "from tools.llm_call_tracker import estimate_cost_for_model" in src
    assert '"deepseek-v4-flash": {"input": 0.0008' not in src


def test_dashboard_uses_unified_api_cost_service():
    src = (ROOT / "api" / "dashboard_api.py").read_text(encoding="utf-8")

    assert "from services.api_costs import" in src
    assert "get_api_cost_summary" in src
    assert "FROM llm_call_log WHERE created_at >= %s" not in src
    assert "FROM token_usage WHERE created_at >= %s" not in src


def test_api_cost_summary_unions_current_and_legacy_sources():
    from services.api_costs import build_api_cost_union_sql

    sql = build_api_cost_union_sql()

    assert "llm_call_log" in sql
    assert "token_usage" in sql
    assert "monitoring_token_usage" in sql
    assert "NOT EXISTS" in sql
    assert "llm.caller IN ('monitoring', 'ai_visibility')" in sql
    assert "metadata" in sql
    assert "task_id" in sql


def test_admin_llm_cost_summary_uses_unified_service():
    src = (ROOT / "api" / "admin_llm_cost_api.py").read_text(encoding="utf-8")

    assert "from services.api_costs import" in src
    assert "get_api_cost_summary" in src
    assert "get_api_cost_by_caller" in src
    assert "get_api_cost_by_platform" in src
    assert "get_api_cost_timeline" in src


def test_major_direct_llm_and_data_api_call_sites_are_tracked():
    targets = [
        ROOT / "advisors" / "base_advisor.py",
        ROOT / "services" / "llm" / "advisor_llm.py",  # E0 上提后的真身;原垫子随开源 E3 B2 删
        ROOT / "tools" / "search" / "metaso_mcp.py",
        ROOT / "tools" / "competition_analyzer.py",
        ROOT / "tools" / "api_5118.py",
        ROOT / "tools" / "tikhub" / "tikhub_tools.py",
    ]

    for path in targets:
        src = path.read_text(encoding="utf-8")
        assert "llm_track(" in src, f"{path} has paid API calls without llm_track"


def test_settings_connection_checks_track_paid_probe_calls():
    src = (ROOT / "config" / "settings_manager.py").read_text(encoding="utf-8")

    assert "settings_health_check" in src
    assert 'model="search"' in src
    assert 'model="longtail"' in src
    assert 'model="search_volume"' in src
    assert 'model="endpoint_probe"' in src


def test_monitoring_placeholder_pricing_delegates_to_pricing_ssot():
    src = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")

    assert "from tools.llm_call_tracker import estimate_cost" in src
    assert "_PLATFORM_PRICING" not in src


def test_pricing_table_covers_current_monitoring_models():
    from tools.llm_call_tracker import DEFAULT_PRICING, PRICING_TABLE, estimate_cost

    assert PRICING_TABLE[("doubao", "doubao-seed-2-0-pro-260215")]["input"] == 0.0013
    assert PRICING_TABLE[("doubao", "doubao-seed-2-0-pro-260215")]["output"] == 0.0026
    assert PRICING_TABLE[("dashscope", "text-embedding-v4")]["input"] == 0.0013
    assert ("dashscope", "qwen-flash") in PRICING_TABLE
    assert ("dashscope", "qwen-turbo-latest") in PRICING_TABLE
    assert ("dashscope", "qwen-max") in PRICING_TABLE
    assert PRICING_TABLE[("dashscope", "deepseek-v4-flash")]["input"] == 0.0013
    assert PRICING_TABLE[("dashscope", "deepseek-v4-flash")]["output"] == 0.0026
    assert PRICING_TABLE[("dashscope", "deepseek-v4-pro")]["input"] == 0.0013
    assert PRICING_TABLE[("dashscope", "deepseek-v4-pro")]["output"] == 0.0026

    # Unknown models must not inherit the first model for the same platform.
    assert estimate_cost("dashscope", "unknown-model", 1000, 1000) == round(
        DEFAULT_PRICING["input"] + DEFAULT_PRICING["output"],
        6,
    )


def test_kimi_web_search_tool_cost_is_added_from_metadata():
    from tools.llm_call_tracker import estimate_cost

    base_cost = estimate_cost("kimi", "kimi-k2.6", 0, 0)
    with_tool = estimate_cost("kimi", "kimi-k2.6", 0, 0, metadata={"web_search_call_count": 1})

    assert with_tool > base_cost


def test_data_api_prices_match_business_inputs():
    from tools.llm_call_tracker import TIKHUB_CALL_CNY, estimate_cost

    assert estimate_cost("5118", "longtail", 0, 0) == 0.013
    assert estimate_cost("5118", "search_volume", 0, 0) == 0.013
    assert estimate_cost("5118", "search_volume", 0, 0, metadata={"billable_units": 50}) == 0.65
    assert estimate_cost("5118", "search_volume", 0, 0, metadata={"billable_units": 0}) == 0
    assert estimate_cost("tikhub", "video_detail", 0, 0) == TIKHUB_CALL_CNY
    assert estimate_cost("tikhub", "new_endpoint_alias", 0, 0) == TIKHUB_CALL_CNY


def test_tikhub_direct_http_call_sites_use_tracking_helper():
    targets = [
        ROOT / "tools" / "social" / "tikhub_mcp.py",
        ROOT / "tools" / "social" / "smart_mcp.py",
        ROOT / "services" / "video_link.py",  # E0b 2026-09-26:视频链接取数从社媒改写工具(已随开源 E3 B2 删)上提到这里
        ROOT / "tools" / "asr" / "asr_tool.py",
    ]

    for path in targets:
        src = path.read_text(encoding="utf-8")
        assert "tracked_tikhub_" in src, f"{path} has TikHub calls outside the cost helper"
        assert "tools.tikhub_cost_tracking" in src


def test_recent_sdk_llm_call_sites_are_tracked():
    targets = [
        ROOT / "services" / "intake_ai.py",
        ROOT / "tools" / "industry_case_collector.py",
        ROOT / "tools" / "pricing_auditor.py",
        ROOT / "services" / "placement_service.py",
    ]

    for path in targets:
        src = path.read_text(encoding="utf-8")
        assert "llm_track" in src or "llm_track_sync" in src
        assert "usage_from_response_payload" in src


def test_article_writing_cost_infers_openrouter_and_other_platforms_from_url():
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")

    assert "infer_platform_from_url(api_url" in src
    assert "'doubao' if 'volces'" not in src
    assert "usage_from_response_payload(response)" in src
    assert "cached_tokens=_cached_t" in src


def test_report_enhancement_token_usage_saves_actual_provider_model():
    src = (ROOT / "agents" / "report_enhancement_agent.py").read_text(encoding="utf-8")

    assert "self.model_name = None" in src
    # 🔴 [WO_206 c1c 翻面 2026-09-14] 原来这里按**源码文本**钉
    #    `model_name=self.model_name or "deepseek-chat"`。c1c 把那行改成了常量。
    #    本判据守的是「记账时记的是**真实用的那个模型**,不是一个写死的占位」——
    #    那件事一个字没变,变的只是那个名字从字面量换成了常量。
    #    🔴 这一条是两套 import 锚**都没抓到**的一类:它 `read_text` 读源码文本,
    #       既不 import 被改的模块,也不在受影响模块的导入图里。
    #       归因分母因此要有第三条腿:按**被改文件的路径**去 grep 判据。
    assert "model_name=self.model_name or DEEPSEEK_OFFICIAL_FLASH" in src
    assert "model_name=OPENROUTER_MODEL" in src
    assert "model_name=DEEPSEEK_MODEL" in src


def test_asr_cost_uses_audio_seconds_metadata():
    from tools.llm_call_tracker import estimate_cost

    assert estimate_cost(
        "dashscope",
        "qwen3-asr-flash",
        0,
        0,
        metadata={"audio_seconds": 60},
    ) == 0.012


def test_deepseek_current_price_and_cache_hit_are_applied():
    """🔴 [WO_206 c1p 2026-09-14 翻面] 原来这里钉的是 2026-05 的四个数字
       (当时的四个单价)。官方线 09-14 起是**峰谷两档**,
       表里存高峰价,数字全变了 —— 继续钉旧数字会与正确的对账互斥。

       改成**从表里取**再算:本判据守的那件事没变 ——
       「官方行按它自己的价算、缓存命中那一段确实用了 cache_hit 价」。
       表本身对不对由 `tests/deepseek_pricing_bands_2026_09_14/` 逐格比签入快照。
       (两边都写死数字 = 同一个数字抄两遍,改的时候只会改一处。)
    """
    from tools.llm_call_tracker import (OFF_PEAK_PRICING, PRICING_TABLE,
                                        estimate_cost)

    for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro",
                  "deepseek-reasoner"):
        row = PRICING_TABLE[("deepseek", model)]
        got = estimate_cost("deepseek", model, 1000, 1000)
        assert abs(got - (row["input"] + row["output"])) < 1e-9, model

    # 缓存命中:1000 input 里 500 命中 ⇒ 500 按 input 价、500 按 cache_hit 价
    row = PRICING_TABLE[("deepseek", "deepseek-flash")]
    want = 0.5 * row["input"] + 0.5 * row["cache_hit"]
    got = estimate_cost("deepseek", "deepseek-flash", 1000, 0, cached_tokens=500)
    assert abs(got - want) < 1e-12, "缓存命中价没被用上:%s vs %s" % (got, want)
    assert row["cache_hit"] < row["input"], "缓存命中价不比未命中便宜 —— 表写反了"

    # 反向对照:**确知**空闲时刻时价钱确实是另一档(整两倍关系)。
    # 少了它,「峰谷」可以靠"永远按高峰"满足,而那等于这一维根本没接上。
    from datetime import datetime, timezone
    off = estimate_cost("deepseek", "deepseek-flash", 1000, 1000,
                        at=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc))
    peak_row = PRICING_TABLE[("deepseek", "deepseek-flash")]
    off_row = OFF_PEAK_PRICING[("deepseek", "deepseek-flash")]
    assert abs(off - (off_row["input"] + off_row["output"])) < 1e-9
    assert off < peak_row["input"] + peak_row["output"]


def test_openrouter_prices_use_usd_model_rates_converted_to_cny():
    from tools.llm_call_tracker import PRICING_TABLE

    assert PRICING_TABLE[("openrouter", "anthropic/claude-sonnet-4.6")]["input"] == 0.0216
    assert PRICING_TABLE[("openrouter", "anthropic/claude-sonnet-4.6")]["output"] == 0.108
    assert PRICING_TABLE[("openrouter", "anthropic/claude-opus-4.5")]["input"] == 0.036
    assert PRICING_TABLE[("openrouter", "anthropic/claude-opus-4.5")]["output"] == 0.18
    assert PRICING_TABLE[("openrouter", "openai/gpt-5.1")]["input"] == 0.009
    assert PRICING_TABLE[("openrouter", "openai/gpt-5.1")]["output"] == 0.072
    assert ("openrouter", "google/gemini-3.1-pro-preview") in PRICING_TABLE
    assert ("openrouter", "google/gemini-3-flash-preview") in PRICING_TABLE


def test_asr_retry_uses_real_duration_instead_of_fixed_600_seconds():
    src = (ROOT / "tools" / "asr" / "asr_tool.py").read_text(encoding="utf-8")

    assert "duration=600" not in src
    assert "billing_seconds = dur_sec or duration_seconds or 0" in src


def test_asr_duration_normalizer_handles_seconds_millis_and_strings():
    from tools.asr.asr_tool import normalize_duration_seconds

    assert normalize_duration_seconds(60) == 60
    assert normalize_duration_seconds(60000) == 60
    assert normalize_duration_seconds("45000") == 45
    assert normalize_duration_seconds(9733, assume_milliseconds=True) == 9.733
    assert normalize_duration_seconds("bad") == 0




def test_total_tokens_only_usage_counts_as_input_tokens():
    from tools.llm_call_tracker import usage_from_response_payload

    input_tokens, output_tokens, cached_tokens = usage_from_response_payload({"usage": {"total_tokens": 79}})

    assert input_tokens == 79
    assert output_tokens == 0
    assert cached_tokens == 0


def test_usage_extractor_handles_sdk_response_objects():
    from types import SimpleNamespace
    from tools.llm_call_tracker import usage_from_response_payload

    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=34,
            prompt_cache_hit_tokens=5,
        )
    )

    assert usage_from_response_payload(response) == (12, 34, 5)


def test_tracking_context_overrides_nested_ai_visibility_call(monkeypatch):
    import tools.llm_call_tracker as tracker

    rows = []
    monkeypatch.setattr(tracker, "_write_log_row", lambda **kwargs: rows.append(kwargs))

    async def run():
        with tracker.llm_tracking_context(
            caller="monitoring",
            brand_id=7,
            user_id=11,
            metadata={"task_id": 13, "monitoring_platform": "kimi"},
        ):
            async with tracker.llm_track(
                "ai_visibility",
                "kimi",
                model="kimi-k2.6",
                metadata={"engine": "kimi_search"},
            ) as call:
                call.record(input_tokens=10, output_tokens=20, success=True)

    asyncio.run(run())

    assert rows[0]["caller"] == "monitoring"
    assert rows[0]["brand_id"] == 7
    assert rows[0]["user_id"] == 11
    assert rows[0]["metadata"]["task_id"] == 13
    assert rows[0]["metadata"]["original_caller"] == "ai_visibility"
    assert rows[0]["metadata"]["engine"] == "kimi_search"


def test_monitoring_adapter_propagates_llm_tracking_context():
    src = (ROOT / "tools" / "monitoring" / "batch_monitor.py").read_text(encoding="utf-8")

    assert "llm_tracking_context" in src
    assert "caller: str = \"monitoring\"" in src
    assert "monitoring_task_id" in src
    assert "keyword_id" in src
    assert "monitoring_platform" in src


def test_monitoring_api_passes_context_to_platform_adapter():
    src = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")

    assert "task_id=task_id" in src
    assert "keyword_id=task.keyword_id" in src
    assert "caller=caller" in src
    assert "brand_id=brand_id" in src
    assert "user_id=user_id" in src


def test_scanned_llm_api_files_have_tracking_hook():
    patterns = [
        "chat/completions",
        "compatible-mode",
        "api.deepseek",
        "api.moonshot",
        "openrouter",
        "siliconflow",
        "ark.cn-beijing.volces",
        "services/embeddings",
        "services/rerank",
        "audio/asr",
        "audio.transcriptions",
    ]
    post_markers = [
        "client.post",
        "session.post",
        "httpx.post",
        "chat.completions.create",
        "Generation.call",
    ]
    track_markers = ["llm_track", "llm_track_sync", "_write_log_row", "save_token_usage"]
    roots = ["api", "services", "tools", "advisors", "writing", "agents"]
    misses = []

    for root in roots:
        for path in (ROOT / root).rglob("*.py"):
            if path.name.endswith(".bak"):
                continue
            src = path.read_text(encoding="utf-8", errors="ignore")
            if any(p in src for p in patterns) and any(m in src for m in post_markers):
                if not any(t in src for t in track_markers):
                    misses.append(str(path.relative_to(ROOT)))

    assert misses == []
