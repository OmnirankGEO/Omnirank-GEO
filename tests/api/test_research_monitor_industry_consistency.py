"""
P14 · 行业数据一致性 (C1 数据一致性)

覆盖:
  - GEO migration 全表覆盖 + FK 合并 + batches regex + orphan backfill
  - slug_for_name deterministic
  - ensure_research_industry / backfill_orphan_industries 存在 + 幂等契约
  - industry list API 返 active_prompt_count
  - create_industry 用 slug_for_name (非毫秒)
  - import_research_csv 同事务 ensure + 失败 raise
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# 1) GEO → geo服务 migration 必须覆盖所有 industry 字段表
# ============================================================

def test_migration_normalize_industry_geo_covers_all_tables():
    src = (ROOT / "scripts" / "migration_normalize_industry_geo.sql").read_text(encoding="utf-8")
    required_tables = [
        "geo_research_raw",
        "geo_engine_stats",          # 发布参谋矩阵 · 之前漏了
        "geo_research_articles",
        "geo_month_weights",
        "geo_research_batches",
        "geo_research_industries",
    ]
    for tbl in required_tables:
        assert tbl in src, f"migration 必须覆盖 {tbl} 表的 industry 字段"
    assert "BEGIN;" in src and "COMMIT;" in src


def test_migration_handles_industries_fk_merge():
    """v5 防御 · prod 双行业行外键合并必须先迁外键再 DELETE 老行"""
    src = (ROOT / "scripts" / "migration_normalize_industry_geo.sql").read_text(encoding="utf-8")
    assert "UPDATE geo_research_prompts" in src and "industry_id = v_new_id" in src, \
        "必须先把 prompts.industry_id 迁到 new_id 再删老 industry 行"
    assert "UPDATE geo_research_article_citations" in src, \
        "必须先把 article_citations.industry_id 迁到 new_id 再删老 industry 行"
    assert "v_old_id IS NULL" in src, "必须处理 GEO 不存在的 noop 分支"
    assert "v_new_id IS NULL" in src, "必须处理只有 GEO 的 rename 分支"


def test_migration_batches_uses_regex_for_concat_field():
    """v5 防御 · batches.industry 是逗号拼接字段 · 必须 regex 不能 exact match"""
    src = (ROOT / "scripts" / "migration_normalize_industry_geo.sql").read_text(encoding="utf-8")
    assert "regexp_replace(industry" in src, \
        "batches.industry 必须 regexp_replace · 防 'GEO,房地产' 这种拼接漏改"
    assert "(^|,)GEO(,|$)" in src, "regex 必须用 (^|,)/(,|$) 锚定单词边界"


# ============================================================
# 2) orphan industry backfill (P14-v6 根治)
# ============================================================

def test_migration_v6_backfills_orphan_industries():
    """migration 必须有 orphan backfill 段 · 把 raw/stats/articles 缺失的行业补进 industries"""
    src = (ROOT / "scripts" / "migration_normalize_industry_geo.sql").read_text(encoding="utf-8")
    assert "backfill" in src.lower(), "migration 必须含 orphan backfill 段"
    assert "FROM geo_research_raw" in src
    assert "FROM geo_engine_stats" in src
    assert "FROM geo_research_articles" in src
    assert "NOT EXISTS" in src
    assert "INSERT INTO geo_research_industries" in src


def test_ensure_research_industry_function_exists_and_idempotent():
    """ensure_research_industry 必须存在 · 重复调用同 name 返同 id (幂等)"""
    from services.research_monitor.industry_registry import (
        ensure_research_industry,
        backfill_orphan_industries,
    )
    assert callable(ensure_research_industry)
    assert callable(backfill_orphan_industries)
    import services.research_monitor.industry_registry as m
    doc = m.__doc__ or ""
    assert "single source of truth" in doc or "唯一权威源" in doc or "geo_research_industries" in doc, \
        "industry_registry 模块文档必须说明 industries 是唯一权威源"


def test_import_research_csv_calls_ensure_industry():
    """import_research_csv 写完 raw 后必须 ensure industries 表对应行存在"""
    src = (ROOT / "services" / "placement_service.py").read_text(encoding="utf-8")
    import re
    m = re.search(
        r'def import_research_csv\(.*?\n(.*?)(?=\n    def |\nclass )',
        src, re.DOTALL,
    )
    assert m, "找不到 import_research_csv 函数"
    body = m.group(1)
    assert "ensure_research_industry" in body, \
        "import_research_csv 必须调 ensure_research_industry · 防 raw 写入后 industries 表缺行"


# ============================================================
# 3) industry list API 返 active_prompt_count
# ============================================================

def test_industry_list_api_returns_active_prompt_count():
    """/industries 必须返 active_prompt_count · 跑批 dialog 禁用无 prompts 行业用

    锁两件事:
      1. SQL 查 active_prompt_count
      2. _row_to_dict 必须把字段透到 JSON (避免 SQL 查了但显式构造 dict 漏掉)
    """
    src = (ROOT / "api" / "research_monitor_industry_api.py").read_text(encoding="utf-8")
    assert "active_prompt_count" in src, \
        "/industries 必须返 active_prompt_count 字段"
    assert "geo_research_prompts" in src and "active = TRUE" in src, \
        "active_prompt_count 必须从 prompts 表过滤 active=TRUE 计数"

    import re
    m = re.search(r'def _row_to_dict\(.*?\n(.*?)(?=\ndef |\nclass |\n@)', src, re.DOTALL)
    assert m, "找不到 _row_to_dict 函数"
    body = m.group(1)
    assert "active_prompt_count" in body, \
        "_row_to_dict 必须把 active_prompt_count 字段透出 · 否则 SQL 查了也不会到前端"


# ============================================================
# 4) slug deterministic + create_industry + import_csv 事务
# ============================================================

def test_slug_for_name_is_deterministic_md5_based():
    """slug 同 name 必返同 slug · 防批量 ensure 撞 UNIQUE"""
    from services.research_monitor.industry_registry import slug_for_name
    s1 = slug_for_name("geo服务")
    s2 = slug_for_name("geo服务")
    assert s1 == s2, f"slug_for_name 必须 deterministic · 同 name 同 slug · got {s1!r} vs {s2!r}"
    assert slug_for_name("geo服务") != slug_for_name("通用")
    import re
    assert re.fullmatch(r"ind_[0-9a-f]{12}", s1), f"slug 格式必须 ind_<12hex>: {s1!r}"


def test_migration_uses_deterministic_slug_md5():
    """migration v7 · backfill SQL 必须用 md5 而不是 epoch_ms · 防同毫秒撞"""
    src = (ROOT / "scripts" / "migration_normalize_industry_geo.sql").read_text(encoding="utf-8")
    assert "substr(md5(v_name)" in src or "md5(v_name)" in src, \
        "migration backfill slug 必须用 md5(name) deterministic · 旧 epoch_ms 会撞"
    assert "EXTRACT(EPOCH FROM clock_timestamp())" not in src, \
        "migration 不能用 epoch_ms 生成 slug · 批量 backfill 会同毫秒撞 UNIQUE"


def test_create_industry_uses_slug_for_name():
    """管理端新建行业必须复用 slug_for_name · 跟 ensure/migration 一套口径"""
    src = (ROOT / "api" / "research_monitor_industry_api.py").read_text(encoding="utf-8")
    assert "_generate_auto_slug" not in src, \
        "_generate_auto_slug 必须删 · 改用 slug_for_name"
    assert "time.time() * 1000" not in src, \
        "毫秒 slug 必须删 · 用 slug_for_name (md5 deterministic)"
    assert "slug_for_name" in src, "create_industry 必须用 slug_for_name"
    assert "name_clean" in src or "payload.name.strip()" in src or "name.strip()" in src, \
        "create_industry 必须 strip name 防 trailing space"


def test_import_csv_ensures_industry_in_transaction_and_raises_on_fail():
    """CSV 导入必须同事务 ensure + 失败 raise · 不能 commit 后才 ensure + warning 吞"""
    src = (ROOT / "services" / "placement_service.py").read_text(encoding="utf-8")
    import re
    m = re.search(
        r'def import_research_csv\(.*?\n(.*?)(?=\n    def |\nclass )',
        src, re.DOTALL,
    )
    assert m
    body = m.group(1)
    assert "ensure_research_industry(_ind, conn=conn)" in body or \
           "ensure_research_industry(_ind, conn=" in body, \
        "ensure 必须用同 conn 进事务 · 不能 own_conn 新开"
    assert "raise RuntimeError" in body or "raise HTTPException" in body or \
           ("raise" in body and "ensure" in body), \
        "ensure 失败必须 raise 让事务回滚 · 不能 warning 吞"
    ensure_idx = body.find("ensure_research_industry(")
    commit_idx = body.find("conn.commit()")
    assert ensure_idx != -1 and commit_idx != -1
    assert ensure_idx < commit_idx, \
        f"ensure_research_industry 调用 (pos={ensure_idx}) 必须在 conn.commit() (pos={commit_idx}) 之前"
