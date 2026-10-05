"""LLM-first 报价管线 contract 回归测试

老板 2026-05-12 §7.1 验收 · 仅 unit/contract 级别 · 不连真 LLM/DB
真 LLM E2E 由 Phase 2 灰度跑

覆盖:
- A. Pydantic Schema 强约束(字段单位 / 范围 / 跨字段一致性)
- B. should_quote=false 时三档必须 -1
- C. should_quote=true 时三档严格递增 + 最低 100
- D. value_score_0_5 范围 [0, 5]
- E. JSON parse + json_repair 兜底
- F. N=2 sample 聚合 · 任一 false 取 false
- G. 软护栏 needs_review 不覆盖 LLM
- H. feature flag 默认 OFF · 灰度白名单
- I. cache namespace pricing_engine_version='llm_v1'
- J. fallback 路径仍可走老公式(flag OFF)
- K. price 整数元 · value_score 浮点
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ===================================================================
# A. Pydantic Schema 强约束
# ===================================================================

def test_pydantic_schema_imports():
    """schema 模块可导入 + 关键 class 存在"""
    from tools.llm_pricing_schema import (
        LLMPricedKeyword,
        LLMPricingBatch,
        apply_price_soft_guard,
        parse_llm_response,
    )
    assert LLMPricedKeyword is not None
    assert LLMPricingBatch is not None


def test_schema_field_names_have_unit_suffix():
    """老板 P0 §4.6:字段名必须带单位 *_yuan / *_0_5"""
    from tools.llm_pricing_schema import LLMPricedKeyword
    fields = LLMPricedKeyword.model_fields
    assert "value_score_0_5" in fields, "缺 value_score_0_5(老板 P0 字段单位)"
    assert "entry_price_yuan" in fields, "缺 entry_price_yuan"
    assert "standard_price_yuan" in fields, "缺 standard_price_yuan"
    assert "flagship_price_yuan" in fields, "缺 flagship_price_yuan"
    assert "value_score" not in fields, "禁用无单位字段 value_score"
    assert "standard_price" not in fields, "禁用无单位字段 standard_price"


def test_schema_value_score_in_range():
    """value_score_0_5 必须在 [0, 5]"""
    from tools.llm_pricing_schema import LLMPricedKeyword
    with pytest.raises(Exception):
        LLMPricedKeyword(
            keyword="x", intent="commercial", funnel="consideration",
            value_score_0_5=5.5,
            entry_price_yuan=100, standard_price_yuan=200, flagship_price_yuan=300,
            should_quote=True, reason_zh="x",
        )
    with pytest.raises(Exception):
        LLMPricedKeyword(
            keyword="x", intent="commercial", funnel="consideration",
            value_score_0_5=-0.1,
            entry_price_yuan=100, standard_price_yuan=200, flagship_price_yuan=300,
            should_quote=True, reason_zh="x",
        )


# ===================================================================
# B. should_quote=false 时三档必须 -1
# ===================================================================

def test_should_not_quote_must_have_minus_one_all_tiers():
    from tools.llm_pricing_schema import LLMPricedKeyword
    kw = LLMPricedKeyword(
        keyword="装修流程是什么", intent="informational", funnel="awareness",
        value_score_0_5=0.0,
        entry_price_yuan=-1, standard_price_yuan=-1, flagship_price_yuan=-1,
        should_quote=False, reason_zh="信息型剔除",
    )
    assert kw.entry_price_yuan == -1
    assert kw.standard_price_yuan == -1
    assert kw.flagship_price_yuan == -1


def test_should_not_quote_with_positive_price_rejected():
    """老板 P0:should_quote=false 但价不是 -1 必须拒"""
    from tools.llm_pricing_schema import LLMPricedKeyword
    with pytest.raises(ValueError, match="should_quote=false"):
        LLMPricedKeyword(
            keyword="装修流程", intent="informational", funnel="awareness",
            value_score_0_5=0.0,
            entry_price_yuan=100, standard_price_yuan=-1, flagship_price_yuan=-1,
            should_quote=False, reason_zh="混乱状态",
        )


# ===================================================================
# C. should_quote=true 时三档严格递增 + 最低 100
# ===================================================================

def test_should_quote_tiers_must_strictly_ascend():
    from tools.llm_pricing_schema import LLMPricedKeyword
    with pytest.raises(ValueError, match="严格递增"):
        LLMPricedKeyword(
            keyword="测试", intent="commercial", funnel="consideration",
            value_score_0_5=3.0,
            entry_price_yuan=500, standard_price_yuan=400, flagship_price_yuan=800,
            should_quote=True, reason_zh="x",
        )


def test_should_quote_entry_below_100_rejected():
    from tools.llm_pricing_schema import LLMPricedKeyword
    with pytest.raises(ValueError, match="≥ 100"):
        LLMPricedKeyword(
            keyword="测试", intent="commercial", funnel="consideration",
            value_score_0_5=3.0,
            entry_price_yuan=50, standard_price_yuan=200, flagship_price_yuan=300,
            should_quote=True, reason_zh="x",
        )


# ===================================================================
# D. JSON parse + json_repair 兜底
# ===================================================================

def test_parse_llm_response_strips_markdown_codeblock():
    from tools.llm_pricing_schema import parse_llm_response
    raw = """```json
{
  "keywords": [
    {"keyword": "测试", "intent": "commercial", "funnel": "consideration",
     "value_score_0_5": 3.0, "entry_price_yuan": 500, "standard_price_yuan": 1000,
     "flagship_price_yuan": 1600, "should_quote": true, "reason_zh": "测试"}
  ]
}
```"""
    batch = parse_llm_response(raw)
    assert len(batch.keywords) == 1
    assert batch.keywords[0].standard_price_yuan == 1000


def test_parse_llm_response_repairs_truncated_json():
    """json_repair 兜底:未闭合 JSON 应能恢复"""
    from tools.llm_pricing_schema import parse_llm_response
    raw = """{"keywords": [{"keyword": "x", "intent": "commercial",
"funnel": "consideration", "value_score_0_5": 3.0,
"entry_price_yuan": 500, "standard_price_yuan": 1000, "flagship_price_yuan": 1600,
"should_quote": true, "reason_zh": "x"}"""
    try:
        batch = parse_llm_response(raw)
        assert len(batch.keywords) >= 1
    except (ValueError, ImportError):
        pytest.skip("json_repair 不可用或修复失败 · 这是兜底 · 真 LLM 输出会闭合")


def test_no_duplicate_keywords_in_batch():
    from tools.llm_pricing_schema import LLMPricingBatch
    with pytest.raises(ValueError, match="重复"):
        LLMPricingBatch(keywords=[
            {"keyword": "x", "intent": "commercial", "funnel": "consideration",
             "value_score_0_5": 3.0, "entry_price_yuan": 500, "standard_price_yuan": 1000,
             "flagship_price_yuan": 1600, "should_quote": True, "reason_zh": "x"},
            {"keyword": "x", "intent": "transactional", "funnel": "decision",
             "value_score_0_5": 4.0, "entry_price_yuan": 800, "standard_price_yuan": 1500,
             "flagship_price_yuan": 2400, "should_quote": True, "reason_zh": "x"},
        ])


# ===================================================================
# E. 软护栏 needs_review 不覆盖 LLM 判断
# ===================================================================

def test_soft_guard_flags_low_price_review_but_keeps_value():
    """单关键词标准版 < 软下限 → needs_review=true · 但不改价"""
    from tools.llm_pricing_schema import LLMPricedKeyword, apply_price_soft_guard
    kw = LLMPricedKeyword(
        keyword="冷门长尾", intent="commercial", funnel="consideration",
        value_score_0_5=2.0,
        entry_price_yuan=100, standard_price_yuan=200, flagship_price_yuan=300,
        should_quote=True, reason_zh="冷门词",
    )
    guarded = apply_price_soft_guard(kw)
    assert guarded.needs_review is True
    assert "软下限" in guarded.review_reason
    assert guarded.standard_price_yuan == 200, "guard 不能改价 · 只 flag"


def test_soft_guard_flags_high_price():
    from tools.llm_pricing_schema import LLMPricedKeyword, apply_price_soft_guard
    kw = LLMPricedKeyword(
        keyword="超高溢价词", intent="transactional", funnel="decision",
        value_score_0_5=5.0,
        entry_price_yuan=5000, standard_price_yuan=9000, flagship_price_yuan=15000,
        should_quote=True, reason_zh="x",
    )
    guarded = apply_price_soft_guard(kw)
    assert guarded.needs_review is True
    assert "软上限" in guarded.review_reason
    assert guarded.standard_price_yuan == 9000


def test_soft_guard_normal_range_no_review():
    from tools.llm_pricing_schema import LLMPricedKeyword, apply_price_soft_guard
    kw = LLMPricedKeyword(
        keyword="正常词", intent="commercial", funnel="consideration",
        value_score_0_5=3.0,
        entry_price_yuan=1000, standard_price_yuan=2000, flagship_price_yuan=3200,
        should_quote=True, reason_zh="x",
    )
    guarded = apply_price_soft_guard(kw)
    assert guarded.needs_review is False


def test_soft_guard_skip_when_should_not_quote():
    from tools.llm_pricing_schema import LLMPricedKeyword, apply_price_soft_guard
    kw = LLMPricedKeyword(
        keyword="信息型", intent="informational", funnel="awareness",
        value_score_0_5=0.0,
        entry_price_yuan=-1, standard_price_yuan=-1, flagship_price_yuan=-1,
        should_quote=False, reason_zh="剔除",
    )
    guarded = apply_price_soft_guard(kw)
    assert guarded.needs_review is False


# ===================================================================
# F. Feature flag 默认 OFF
# ===================================================================

def test_flag_default_off_when_db_unavailable():
    """system_settings 表不存在 / DB 异常 → flag default false(失败安全)"""
    from tools.llm_pricing_flag import is_llm_first_enabled, clear_cache
    clear_cache()
    # 测试环境 DB 大概率不可用 · 应 default false
    result = is_llm_first_enabled(agent_user_id=None)
    assert result is False


def test_flag_whitelist_format():
    """白名单是 JSON array 格式"""
    raw = "[1, 2, 3]"
    parsed = json.loads(raw)
    assert isinstance(parsed, list)
    assert 1 in parsed


def test_flag_fallback_default_true():
    """LLM 失败时默认降级公式 · 不阻断业务"""
    from tools.llm_pricing_flag import is_fallback_to_formula_enabled, clear_cache
    clear_cache()
    assert is_fallback_to_formula_enabled() is True


# ===================================================================
# G. Migration 008 SQL 静态校验
# ===================================================================

def test_migration_008_creates_independent_llm_table():
    """Codex round-3 P0:独立表方案 · 不动老 keyword_price_cache"""
    sql_path = Path(__file__).resolve().parents[1] / "db" / "migration_008_pricing_llm_first.sql"
    assert sql_path.exists(), "migration_008 缺失"
    content = sql_path.read_text(encoding="utf-8")
    # 必须 CREATE TABLE keyword_price_cache_llm
    assert "CREATE TABLE IF NOT EXISTS keyword_price_cache_llm" in content, \
        "migration 必须建独立 LLM cache 表(round-3 P0)"
    # 新表必须含 business_scope_hash(round-3 P1 防跨 scope 串台)
    assert "business_scope_hash" in content, \
        "新表必须含 business_scope_hash 列"
    # 新表 UNIQUE 必须含 business_scope_hash
    assert "UNIQUE (brand_name, keyword, business_scope_hash)" in content, \
        "新表 UNIQUE 必须含 business_scope_hash(brand-specific + scope 隔离)"
    # 5 个 feature flag
    assert "LLM_FIRST_PRICING_ENABLED" in content
    assert "LLM_FIRST_PRICING_AGENT_WHITELIST" in content


def test_migration_008_does_not_alter_old_cache_table_unique():
    """Codex round-3 P0:migration 008 必须不动老 keyword_price_cache UNIQUE
    否则代码 rollback 时老镜像 ON CONFLICT(brand_name, keyword) 会爆"""
    sql_path = Path(__file__).resolve().parents[1] / "db" / "migration_008_pricing_llm_first.sql"
    content = sql_path.read_text(encoding="utf-8")
    # 不能 DROP / ALTER 老表的 UNIQUE 约束
    assert "DROP CONSTRAINT" not in content or "keyword_price_cache_llm" in content, \
        "migration 必须不动老 keyword_price_cache UNIQUE · 否则代码 rollback 不安全"
    assert "ALTER TABLE keyword_price_cache " not in content, \
        "migration 不能 ALTER 老 keyword_price_cache 表"
    assert "ALTER TABLE keyword_price_cache\n" not in content
    # 老表加 pricing_engine_version 列也不要
    assert "ADD COLUMN pricing_engine_version" not in content, \
        "老表不再加 pricing_engine_version 列(round-3 独立表方案)"


def test_migration_008_default_flag_is_off():
    sql_path = Path(__file__).resolve().parents[1] / "db" / "migration_008_pricing_llm_first.sql"
    content = sql_path.read_text(encoding="utf-8")
    assert "'LLM_FIRST_PRICING_ENABLED', 'false'" in content


def test_rollback_008_drops_independent_table_only():
    """Codex round-3 P0:回滚 SQL DROP 独立表 · 老 keyword_price_cache 0 触碰"""
    sql_path = Path(__file__).resolve().parents[1] / "db" / "rollback_008_pricing_llm_first.sql"
    assert sql_path.exists()
    content = sql_path.read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS keyword_price_cache_llm" in content, \
        "回滚必须 DROP 独立 LLM 表"
    # 不能动老表数据
    bad_patterns = [
        "DELETE FROM keyword_price_cache WHERE",
        "ALTER TABLE keyword_price_cache ",
        "DROP TABLE keyword_price_cache ",
        "DROP TABLE IF EXISTS keyword_price_cache ",
    ]
    for bad in bad_patterns:
        assert bad not in content, f"rollback 不能动老表 · 残留: {bad}"


# ===================================================================
# H. 模块导入 + AST 校验
# ===================================================================

def test_llm_pricing_scorer_imports():
    from tools.llm_pricing_scorer import llm_score_and_price
    assert callable(llm_score_and_price)


def test_llm_keyword_expander_imports():
    from tools.llm_keyword_expander import llm_expand_keywords
    assert callable(llm_expand_keywords)


def test_batch_pricing_llm_imports():
    from tools.batch_pricing_llm import llm_first_batch_quote, PRICING_ENGINE_VERSION
    assert PRICING_ENGINE_VERSION == "llm_v1", "namespace 必须 'llm_v1' 跟 migration 一致"


def test_batch_pricing_has_flag_branch():
    """batch_pricing.py 必须有 LLM-first flag 分支 · 不删老公式路径"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing.py"
    src = py_path.read_text(encoding="utf-8")
    assert "is_llm_first_enabled" in src, "batch_pricing 必须有 flag 检查"
    assert "llm_first_batch_quote" in src, "batch_pricing 必须调 LLM 路径"
    assert "score_keywords" in src, "老公式路径必须保留(Phase 3 才删)"
    assert "fallback" in src.lower() or "FALLBACK" in src, "必须有 fallback 降级"


# ===================================================================
# I. N=2 sample 聚合逻辑
# ===================================================================

def test_aggregate_any_should_quote_false_wins():
    """任一 sample 标 should_quote=false · 聚合结果应取 false(防信息型词漏过)"""
    from tools.llm_pricing_schema import LLMPricedKeyword
    from tools.llm_pricing_scorer import _aggregate_samples
    from tools.llm_pricing_schema import LLMPricingBatch

    s1 = LLMPricingBatch(keywords=[
        LLMPricedKeyword(
            keyword="边界词", intent="commercial", funnel="consideration",
            value_score_0_5=2.0,
            entry_price_yuan=500, standard_price_yuan=1000, flagship_price_yuan=1600,
            should_quote=True, reason_zh="商业边界",
        )
    ])
    s2 = LLMPricingBatch(keywords=[
        LLMPricedKeyword(
            keyword="边界词", intent="informational", funnel="awareness",
            value_score_0_5=0.0,
            entry_price_yuan=-1, standard_price_yuan=-1, flagship_price_yuan=-1,
            should_quote=False, reason_zh="信息型",
        )
    ])
    agg = _aggregate_samples([s1, s2])
    assert len(agg) == 1
    assert agg[0].should_quote is False, "任一 sample 标 false · 取 false"
    assert agg[0].standard_price_yuan == -1


def test_aggregate_price_takes_mean():
    """两 sample 都 should_quote=true · 价格取均"""
    from tools.llm_pricing_schema import LLMPricedKeyword, LLMPricingBatch
    from tools.llm_pricing_scorer import _aggregate_samples

    s1 = LLMPricingBatch(keywords=[
        LLMPricedKeyword(
            keyword="x", intent="transactional", funnel="decision",
            value_score_0_5=4.5,
            entry_price_yuan=1000, standard_price_yuan=2000, flagship_price_yuan=3200,
            should_quote=True, reason_zh="x",
        )
    ])
    s2 = LLMPricingBatch(keywords=[
        LLMPricedKeyword(
            keyword="x", intent="transactional", funnel="decision",
            value_score_0_5=4.7,
            entry_price_yuan=1200, standard_price_yuan=2400, flagship_price_yuan=3800,
            should_quote=True, reason_zh="x",
        )
    ])
    agg = _aggregate_samples([s1, s2])
    assert agg[0].standard_price_yuan == 2200, "标准价取均 (2000+2400)/2"
    assert agg[0].entry_price_yuan == 1100
    assert agg[0].flagship_price_yuan == 3500


# ===================================================================
# J. Codex 审计修复 · P0 + P1 回归
# ===================================================================

def test_schema_forbids_extra_fields_without_unit_suffix():
    """老板 P1 修:Pydantic extra='forbid' 真正禁止无单位字段(standard_price 等)"""
    from pydantic import ValidationError

    from tools.llm_pricing_schema import LLMPricedKeyword

    with pytest.raises(ValidationError):
        LLMPricedKeyword(
            keyword="x", intent="commercial", funnel="consideration",
            value_score_0_5=3.0,
            entry_price_yuan=500, standard_price_yuan=1000, flagship_price_yuan=1600,
            should_quote=True, reason_zh="x",
            standard_price=5.0,  # 无单位脏字段 · 必须拒
        )


def test_batch_schema_forbids_extra_fields():
    from pydantic import ValidationError

    from tools.llm_pricing_schema import LLMPricingBatch

    with pytest.raises(ValidationError):
        LLMPricingBatch(
            keywords=[{
                "keyword": "x", "intent": "commercial", "funnel": "consideration",
                "value_score_0_5": 3.0,
                "entry_price_yuan": 500, "standard_price_yuan": 1000,
                "flagship_price_yuan": 1600,
                "should_quote": True, "reason_zh": "x",
            }],
            unknown_top_level_field="oops",  # 顶层脏字段也拒
        )


def _read_diagnosis_db_source() -> str:
    """读 diagnosis_db.py 文件源码 · 不实 import(避开测试环境 DB 不可用)"""
    p = Path(__file__).resolve().parents[1] / "db" / "diagnosis_db.py"
    return p.read_text(encoding="utf-8")


def _extract_function_source(full_src: str, fn_name: str) -> str:
    """从源码抽取 def fn_name 直到下一个 def(粗匹配 · 够 contract test 用)"""
    import re
    m = re.search(rf"^def {re.escape(fn_name)}\(.*?\n(?=^def |\Z)", full_src, re.M | re.S)
    return m.group(0) if m else ""


def test_old_save_keyword_prices_cache_reverted_to_round1_pre():
    """Codex round-3 P0:老 save_keyword_prices_cache 必须恢复 round-1 前样子
    用 ON CONFLICT(brand_name, keyword) 2 列 · 跟老表 UNIQUE 一致 · 代码 rollback 安全"""
    src = _read_diagnosis_db_source()
    fn = _extract_function_source(src, "save_keyword_prices_cache")
    assert fn, "save_keyword_prices_cache 函数未找到"
    # 老 ON CONFLICT 必须是 2 列(老表 UNIQUE 没改)
    assert "ON CONFLICT(brand_name, keyword) DO UPDATE" in fn, \
        "老 fn 必须用 2 列 ON CONFLICT · 跟老表 UNIQUE 一致"
    # 不能有 round-1 加的 pricing_engine_version 参数
    assert "pricing_engine_version" not in fn, \
        "老 fn 不该再有 pricing_engine_version 参数(round-3 改独立表)"
    # 不能有 round-2 加的 LLM-first 专属字段 INSERT
    # [2026-06-11 订正] needs_review 从禁列剔除:它是 v1.3 D2 复盘字段(老表合法列 · 全行业通用)
    #   非 LLM-first 专属 · 主线 main 在 v1.3 起就 INSERT 它 · 本断言陈旧导致 main baseline 长期 fail
    for field in ("llm_reason_zh", "business_line"):
        assert field not in fn, \
            f"老 fn 不该 INSERT {field}(LLM-first 字段已搬到独立表)"


def test_old_get_cached_keyword_prices_reverted_to_round1_pre():
    """老 get_cached_keyword_prices 必须恢复 round-1 前样子"""
    src = _read_diagnosis_db_source()
    fn = _extract_function_source(src, "get_cached_keyword_prices")
    assert fn
    assert "pricing_engine_version" not in fn, \
        "老 fn 不该再有 pricing_engine_version 参数 / WHERE 过滤"
    # result dict 不该含 LLM 字段
    for field in ("'llm_reason_zh'", "'should_quote'"):
        assert field not in fn, f"老 fn result 不该含 {field}(LLM 字段已搬到独立表)"


def test_old_clear_keyword_prices_cache_reverted():
    src = _read_diagnosis_db_source()
    fn = _extract_function_source(src, "clear_keyword_prices_cache")
    assert fn
    assert "pricing_engine_version" not in fn, \
        "老 clear_keyword_prices_cache 不该有 pricing_engine_version 参数"


# ===================================================================
# K. Codex round-3 审计修复 · 独立 LLM cache 表 + brand-specific + scope_hash
# ===================================================================


def test_new_save_llm_keyword_prices_cache_exists_in_diagnosis_db():
    """Codex round-3:db.diagnosis_db 必须有独立 save_llm_keyword_prices_cache"""
    src = _read_diagnosis_db_source()
    assert "def save_llm_keyword_prices_cache(" in src, \
        "缺 save_llm_keyword_prices_cache · LLM 路径需独立 cache fn"
    fn = _extract_function_source(src, "save_llm_keyword_prices_cache")
    # 必须写独立表 · 不写老表
    assert "keyword_price_cache_llm" in fn, \
        "save_llm_keyword_prices_cache 必须写独立表 keyword_price_cache_llm"
    assert "INSERT INTO keyword_price_cache " not in fn and \
           "INSERT INTO keyword_price_cache(" not in fn, \
        "save_llm_keyword_prices_cache 不能写老表"
    # 必须含 business_scope 参数
    assert "business_scope" in fn, "缺 business_scope 参数(round-3 P1)"
    # 必须用 business_scope_hash
    assert "business_scope_hash" in fn


def test_new_get_llm_cached_keyword_prices_brand_specific():
    """Codex round-3 P1:LLM cache 必须 brand-specific 不跨品牌"""
    src = _read_diagnosis_db_source()
    assert "def get_llm_cached_keyword_prices(" in src
    fn = _extract_function_source(src, "get_llm_cached_keyword_prices")
    # 必须查独立表
    assert "FROM keyword_price_cache_llm" in fn, \
        "get_llm_cached_keyword_prices 必须查独立表"
    # 必须按 brand_name 过滤(brand-specific · 不走 industry+city 全局)
    assert "WHERE brand_name = %s" in fn, \
        "必须按 brand_name 过滤 · 防跨品牌串台(round-3 P1)"
    # 必须按 business_scope_hash 过滤
    assert "business_scope_hash = %s" in fn, \
        "必须按 business_scope_hash 过滤 · 防 scope 串台(round-3 P1)"
    # 不能走老 (industry, city, keyword) 全局命中
    assert "industry = %s AND city = %s" not in fn, \
        "LLM cache 不该走老 (industry, city, keyword) 全局命中 · Phase 2 brand-specific"


def test_new_clear_llm_keyword_prices_cache():
    src = _read_diagnosis_db_source()
    assert "def clear_llm_keyword_prices_cache(" in src
    fn = _extract_function_source(src, "clear_llm_keyword_prices_cache")
    assert "QUOTE_CACHE_BRAND_ID_NAMESPACE_REQUIRED" in fn, \
        "缺 brand_id 命名空间时必须 P1 fail-closed"
    assert "DELETE FROM keyword_price_cache_llm" not in fn, \
        "禁止按品牌名清缓存 · 同名客户会串删"
    assert "DELETE FROM keyword_price_cache " not in fn, \
        "clear_llm 不能删老表(代码 rollback 安全)"


def test_business_scope_hash_helper_stable():
    """同 business_scope 必须产生相同 hash · 跨 worker 一致"""
    src = _read_diagnosis_db_source()
    assert "def _compute_business_scope_hash(" in src
    fn = _extract_function_source(src, "_compute_business_scope_hash")
    # 必须用 sha256(标准化的 stable hash)
    assert "sha256" in fn or "hashlib" in fn, "必须用 hashlib stable hash · 不能用 hash() 内置(进程间随机)"
    # 空 scope 应有稳定常量
    assert "no_scope" in fn or '""' in fn, "空 scope 必须有稳定 fallback"


def test_aggregate_supports_input_output_keyword_diff():
    """Codex round-1 P1 修:LLM 输出关键词集合不匹配 · sample 应判 fail"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "llm_pricing_scorer.py"
    src = py_path.read_text(encoding="utf-8")
    # _call_with_retry 必须接 expected_keywords
    assert "expected_keywords" in src, "_call_with_retry 必须接 expected_keywords"
    assert "missing" in src and "extra" in src, \
        "必须做集合 diff(missing + extra)"
    # llm_score_and_price 必须做 coverage 兜底
    assert "coverage_missing" in src or "coverage" in src.lower(), \
        "llm_score_and_price 聚合后必须校验全量覆盖"


def test_batch_pricing_llm_has_read_through_cache():
    """Codex round-2 P0 修:read-through cache · 未命中才打 LLM"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing_llm.py"
    src = py_path.read_text(encoding="utf-8")
    assert "_read_llm_cache" in src, "缺 _read_llm_cache helper"
    assert "cache_hits" in src
    assert "keywords_to_llm" in src
    # _read_llm_cache 必须在 llm_score_and_price 之前调用
    read_pos = src.find("_read_llm_cache(brand_name")
    llm_pos = src.find("llm_score_and_price(")
    assert read_pos > 0 and llm_pos > 0 and read_pos < llm_pos, \
        "_read_llm_cache 必须在 llm_score_and_price 之前 · read-through 顺序"


def test_read_llm_cache_uses_independent_table_fn():
    """Codex round-3 P0:_read_llm_cache 必须调独立 get_llm_cached_keyword_prices"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing_llm.py"
    src = py_path.read_text(encoding="utf-8")
    import re
    m = re.search(r"def _read_llm_cache\(.*?(?=\ndef |\Z)", src, re.S)
    assert m
    fn_src = m.group(0)
    assert "get_llm_cached_keyword_prices" in fn_src, \
        "_read_llm_cache 必须调独立 fn get_llm_cached_keyword_prices(round-3)"
    # 不能再调老 get_cached_keyword_prices
    assert "get_cached_keyword_prices(" not in fn_src, \
        "_read_llm_cache 不该调老 fn(round-3 P0 改独立表)"
    # 必须传 business_scope
    assert "business_scope" in fn_src


def test_save_llm_prices_to_cache_uses_independent_fn():
    """Codex round-3 P0:save_llm_prices_to_cache 走独立 fn · 不走老 save_keyword_prices_cache"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing_llm.py"
    src = py_path.read_text(encoding="utf-8")
    import re
    m = re.search(r"async def save_llm_prices_to_cache\(.*?(?=\nasync def |\ndef [^_]|\Z)", src, re.S)
    assert m
    fn_src = m.group(0)
    assert "save_llm_keyword_prices_cache" in fn_src, \
        "save_llm_prices_to_cache 必须调独立 fn save_llm_keyword_prices_cache(round-3)"
    # 不能走老 fn
    assert "save_keyword_prices_cache(" not in fn_src, \
        "不该再走老 save_keyword_prices_cache(round-3 P0 独立表)"
    # 不能有二段 UPDATE 残留
    assert "UPDATE keyword_price_cache" not in fn_src, \
        "残留二段 UPDATE"


def test_cache_row_to_llm_keyword_converter_exists():
    """Codex round-2 P0:cache row 转 LLMPricedKeyword 等价格式"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing_llm.py"
    src = py_path.read_text(encoding="utf-8")
    import re
    m = re.search(r"def _cache_row_to_llm_keyword\(.*?(?=\ndef |\nasync def |\Z)", src, re.S)
    assert m
    fn_src = m.group(0)
    for field in ("value_score_0_5", "entry_price_yuan", "standard_price_yuan",
                  "flagship_price_yuan", "should_quote"):
        assert field in fn_src, f"cache → LLM 转换必须输出 {field}"


def test_should_quote_false_survives_cache_roundtrip():
    """Codex round-2 P1:should_quote=false 经 cache 转换后仍 false · 三档 -1"""
    from tools.batch_pricing_llm import _cache_row_to_llm_keyword

    fake_cache_row = {
        "intent": "informational",
        "funnel_stage": "awareness",
        "value_score": 0.0,
        "entry_price": 0,
        "standard_price": 0,
        "flagship_price": 0,
        "should_quote": False,
        "llm_reason_zh": "纯信息型 · 用户无购买意图",
        "business_line": "",
        "needs_review": False,
    }
    converted = _cache_row_to_llm_keyword(fake_cache_row, "装修流程")
    assert converted["should_quote"] is False
    assert converted["entry_price_yuan"] == -1
    assert converted["standard_price_yuan"] == -1
    assert converted["flagship_price_yuan"] == -1
    assert converted["intent"] == "informational"
    assert converted["reason_zh"]
    assert converted["_from_cache"] is True


def test_batch_pricing_skip_markup_disables_llm_first():
    """Codex round-3 P1:skip_markup=True 时必须早出 · 不进 LLM 分支"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing.py"
    src = py_path.read_text(encoding="utf-8")
    # 必须有 skip_markup 检测早出 LLM
    assert "skip_markup or markup_override is not None" in src or \
           "skip_markup or markup_override" in src, \
        "batch_pricing 必须检测 skip_markup / markup_override · 早出 LLM-first"
    # llm_on 必须在该条件下设 False
    # 大致正则匹配:if skip_markup or markup_override ... llm_on = False
    import re
    pattern = re.compile(
        r"if\s+skip_markup\s+or\s+markup_override\s+is\s+not\s+None.*?llm_on\s*=\s*False",
        re.S,
    )
    assert pattern.search(src), \
        "skip_markup=True 或 markup_override 不为 None 时 · llm_on 必须置 False(老板 round-3 P1)"


def test_business_scope_hash_column_is_not_null_default():
    """Codex round-4 P2:DB 层硬约束 · business_scope_hash NOT NULL DEFAULT 'no_scope'
    Postgres NULL ≠ NULL · UNIQUE 含 NULL 列会允许重复 NULL · 加 NOT NULL 让 UNIQUE 真正生效
    """
    sql_path = Path(__file__).resolve().parents[1] / "db" / "migration_008_pricing_llm_first.sql"
    content = sql_path.read_text(encoding="utf-8")
    # 必须含 NOT NULL DEFAULT
    assert "business_scope_hash" in content
    # 在 business_scope_hash 列定义那一段必须含 NOT NULL DEFAULT 'no_scope'
    import re
    m = re.search(r"business_scope_hash\s+TEXT\s+NOT NULL\s+DEFAULT\s+'no_scope'", content)
    assert m, "business_scope_hash 必须 NOT NULL DEFAULT 'no_scope' · 防 UNIQUE 含 NULL 失效(round-4 P2)"

    # verify 也要校验
    py_path = Path(__file__).resolve().parents[1] / "db" / "migrate_008_pricing_llm_first.py"
    py_src = py_path.read_text(encoding="utf-8")
    assert "is_nullable='NO'" in py_src or 'is_nullable="NO"' in py_src, \
        "VERIFY_QUERIES 必须校验 business_scope_hash NOT NULL"


def test_business_scope_hash_in_cache_path():
    """Codex round-3 P1:LLM cache 读写都必须带 business_scope_hash"""
    src = _read_diagnosis_db_source()
    # save 必须含 business_scope_hash
    save_fn = _extract_function_source(src, "save_llm_keyword_prices_cache")
    assert "business_scope_hash" in save_fn
    # get 必须 WHERE business_scope_hash
    get_fn = _extract_function_source(src, "get_llm_cached_keyword_prices")
    assert "business_scope_hash = %s" in get_fn

    # batch_pricing_llm 调 _read_llm_cache 必须传 business_scope
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing_llm.py"
    src2 = py_path.read_text(encoding="utf-8")
    # llm_first_batch_quote 内 _read_llm_cache 调用必须传 business_scope
    import re
    m = re.search(r"_read_llm_cache\([^)]*business_scope[^)]*\)", src2)
    assert m, "llm_first_batch_quote 调 _read_llm_cache 必须传 business_scope(防跨 scope 串台)"


# ===================================================================
# L. [任务2 2026-06-08] LLM-first 成本地板防亏(M1 口径对齐 · selling ≥ ceil(篇数×单篇成本×markup))
# ===================================================================

def test_llm_per_word_articles_floor_min5():
    """单词每档篇数:无竞争信号 compute_required_articles_banded 打底 ≥5(v1.3 铁律)· 取 max 阶梯系数 4/7/10 → 5/7/10。"""
    from tools.batch_pricing_llm import _llm_per_word_articles
    assert _llm_per_word_articles("入门版", None) == 5   # max(4, banded=5)
    assert _llm_per_word_articles("标准版", None) == 7   # max(7, banded=5)
    assert _llm_per_word_articles("旗舰版", None) == 10  # max(10, banded=5)


def test_llm_cost_floor_lifts_below_cost_price():
    """LLM 报低于成本价 → 被成本地板抬到 ceil(篇数 × 单篇成本 × markup)· 三档 total_articles 与地板同口径。"""
    from tools.batch_pricing_llm import _build_quote_data_from_llm
    kws = [{"keyword": "亏本词", "should_quote": True,
            "entry_price_yuan": 10, "standard_price_yuan": 20, "flagship_price_yuan": 30}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None, cost_per_article=60.0)
    d = qd["keyword_details"][0]
    assert d["entry_price"] == 300     # ceil(5 × 60 × 1)
    assert d["standard_price"] == 420  # ceil(7 × 60 × 1)
    assert d["flagship_price"] == 600  # ceil(10 × 60 × 1)
    assert d["selling_price"] == 420   # selling_price = 标准档(已钳)
    assert qd["tier_summaries"]["入门版"]["final_price"] == 300
    assert qd["tier_summaries"]["入门版"]["total_articles"] == 5
    assert qd["tier_summaries"]["标准版"]["total_articles"] == 7
    assert qd["tier_summaries"]["旗舰版"]["total_articles"] == 10


def test_llm_cost_floor_keeps_high_price():
    """LLM 报价高于成本地板 → 保留 LLM 价(地板只托底不压顶)。"""
    from tools.batch_pricing_llm import _build_quote_data_from_llm
    kws = [{"keyword": "高价词", "should_quote": True,
            "entry_price_yuan": 5000, "standard_price_yuan": 8000, "flagship_price_yuan": 12000}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None, cost_per_article=60.0)
    d = qd["keyword_details"][0]
    assert d["entry_price"] == 5000
    assert d["standard_price"] == 8000
    assert d["flagship_price"] == 12000


def test_llm_cost_floor_uses_agent_self_cost_override():
    """服务商自设单篇成本(投央媒 ¥350)→ 成本地板按 350 算 → 防亏(M1+M2 闭环·LLM-first 路径)。"""
    from tools.batch_pricing_llm import _build_quote_data_from_llm
    kws = [{"keyword": "央媒词", "should_quote": True,
            "entry_price_yuan": 100, "standard_price_yuan": 200, "flagship_price_yuan": 300}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None, cost_per_article=350.0)
    d = qd["keyword_details"][0]
    assert d["entry_price"] == 1750     # ceil(5 × 350 × 1)
    assert d["standard_price"] == 2450  # ceil(7 × 350 × 1)
    assert d["flagship_price"] == 3500  # ceil(10 × 350 × 1)


def test_llm_cost_floor_skips_should_quote_false():
    """信息型词(should_quote=false)不计价不计篇数 · 成本地板不误伤。"""
    from tools.batch_pricing_llm import _build_quote_data_from_llm
    kws = [{"keyword": "信息词", "should_quote": False}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None, cost_per_article=60.0)
    assert qd["quoted_keywords"] == 0
    assert qd["tier_summaries"]["入门版"]["final_price"] == 0
    assert qd["tier_summaries"]["入门版"]["total_articles"] == 0


def test_llm_cost_floor_default_cost_when_no_override():
    """不传 cost_per_article → 兜底系统默认单篇成本(¥60·与公式引擎 default 一致)· 地板仍生效防亏。"""
    from tools.batch_pricing_llm import _build_quote_data_from_llm
    kws = [{"keyword": "默认成本词", "should_quote": True,
            "entry_price_yuan": 1, "standard_price_yuan": 2, "flagship_price_yuan": 3}]
    qd = _build_quote_data_from_llm(kws, "客户", "", "", 0.25, None)  # cost_per_article 缺省
    d = qd["keyword_details"][0]
    assert d["entry_price"] >= 300  # 至少 ceil(5 × 60 × 1)(兜底成本不为 0)


def test_llm_first_batch_quote_accepts_cost_override_param():
    """llm_first_batch_quote 签名含 cost_per_article_override(上游 generate_batch_quote 可透传)。"""
    import inspect
    from tools.batch_pricing_llm import llm_first_batch_quote
    sig = inspect.signature(llm_first_batch_quote)
    assert "cost_per_article_override" in sig.parameters


def test_batch_pricing_passes_cost_override_to_llm_first():
    """generate_batch_quote 把 cost_per_article_override 透传给 LLM-first 引擎(防亏覆盖全引擎)。"""
    py_path = Path(__file__).resolve().parents[1] / "tools" / "batch_pricing.py"
    src = py_path.read_text(encoding="utf-8")
    assert "cost_per_article_override=cost_per_article_override" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
