from pathlib import Path

from scripts.migrate_legacy_csv_to_research_monitor import (
    ArticleBodyPatch,
    CsvFileInfo,
    ParsedRowMinimal,
    RawWriter,
    collect_article_body_patches,
    discover_csv_files,
    parse_dir_name,
    step1_ensure_industries,
    step3_insert_prompts,
    step5b_backfill_article_cleaned_content,
)


def test_parse_dir_name_accepts_june_underscore_package_format():
    parsed = parse_dir_name("2026-06-04_旅游酒店")

    assert parsed is not None
    started_at, raw_industry, industry_name = parsed
    assert started_at.strftime("%Y-%m-%d") == "2026-06-04"
    assert raw_industry == "旅游酒店"
    assert industry_name == "旅游酒店"


def test_discover_csv_files_prefers_raw_citation_stats_csv(tmp_path: Path):
    industry_dir = tmp_path / "2026-06-04_旅游酒店"
    industry_dir.mkdir()
    article_rollup = industry_dir / "2026-06-04_文章上榜统计.csv"
    raw_citations = industry_dir / "2026-06-04_统计结果.csv"
    article_rollup.write_text("文章文件,原文链接\n", encoding="utf-8")
    raw_citations.write_text(
        "序号,查询内容,AI平台,引用编号,引用标题,引用链接,引用来源平台,引用摘要\n",
        encoding="utf-8",
    )

    discovered = discover_csv_files(tmp_path)

    assert len(discovered) == 1
    assert discovered[0].csv_path == raw_citations


def test_raw_writer_returns_id_for_citation_backlink(tmp_path: Path):
    class FakeCursor:
        def __init__(self):
            self.sql = ""
            self.params = None

        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchone(self):
            return {"id": 12345}

    row = ParsedRowMinimal(
        industry_name="旅游酒店",
        query="亲子酒店怎么选",
        platform="qwen",
        rank=2,
        title="亲子酒店选择指南",
        url_raw="https://example.com/a",
        url_norm="https://example.com/a",
        cite_platform_zh="example.com",
        excerpt="摘要",
        answer_text=None,
    )
    csv_info = CsvFileInfo(
        dir_path=tmp_path / "2026-06-04_旅游酒店",
        csv_path=tmp_path / "2026-06-04_统计结果.csv",
        started_at=parse_dir_name("2026-06-04_旅游酒店")[0],
        raw_industry="旅游酒店",
        industry_name="旅游酒店",
    )
    cur = FakeCursor()

    RawWriter(cur).write_csv([row], csv_info, "round_legacy_2026-06-04_travel")

    assert row.raw_id == 12345
    assert "WHERE NOT EXISTS" in cur.sql
    assert len(cur.params) == 18


def test_step1_reuses_existing_industries_by_name_not_generated_slug(monkeypatch):
    from scripts import migrate_legacy_csv_to_research_monitor as migrate

    monkeypatch.setattr(
        migrate,
        "INDUSTRY_NAME_TO_SLUG",
        {"企业服务": "enterprise", "汽车": "auto"},
    )

    class FakeCursor:
        def __init__(self):
            self.rows = []
            self.select_sql = ""
            self.select_params = None

        def execute(self, sql, params=None):
            if "INSERT INTO geo_research_industries" in sql:
                assert "ON CONFLICT (name) DO NOTHING" in sql
                self.rows.append(params)
                return
            if "SELECT name, id FROM geo_research_industries" in sql:
                assert "WHERE name = ANY(%s)" in sql
                self.select_sql = sql
                self.select_params = params
                return
            raise AssertionError(f"unexpected SQL: {sql}")

        def fetchall(self):
            return [
                {"name": "企业服务", "id": 501},
                {"name": "汽车", "id": 502},
            ]

    cur = FakeCursor()

    industry_id_map = step1_ensure_industries(cur, dry=False)

    assert cur.rows == [
        ("企业服务", "enterprise", 0),
        ("汽车", "auto", 1),
    ]
    assert cur.select_params == (["企业服务", "汽车"],)
    assert industry_id_map == {"企业服务": 501, "汽车": 502}


def test_article_insert_conflict_target_matches_prod_composite_unique():
    source = Path("scripts/migrate_legacy_csv_to_research_monitor.py").read_text(
        encoding="utf-8"
    )

    assert "ON CONFLICT (url_hash, primary_industry) DO NOTHING" in source
    assert "ON CONFLICT (url) DO NOTHING" not in source


def test_step3_skips_existing_prompts_before_insert():
    class FakeCursor:
        def __init__(self):
            self.inserted = []
            self.current_industry_id = None

        def execute(self, sql, params=None):
            if "SELECT prompt_text FROM geo_research_prompts" in sql:
                self.current_industry_id = params[0]
                return
            if "INSERT INTO geo_research_prompts" in sql:
                self.inserted.append(params)
                return
            if "SELECT id, industry_id, prompt_text" in sql:
                self.current_industry_id = "all"
                return
            raise AssertionError(f"unexpected SQL: {sql}")

        def fetchall(self):
            if self.current_industry_id == 77:
                return [{"prompt_text": "已有问题"}]
            if self.current_industry_id == "all":
                return [
                    {"id": 7001, "industry_id": 77, "prompt_text": "已有问题"},
                    {"id": 7002, "industry_id": 77, "prompt_text": "新问题"},
                ]
            return []

    cur = FakeCursor()

    prompt_id_map = step3_insert_prompts(
        cur,
        {"旅游酒店": 77},
        {"旅游酒店": ["已有问题", "新问题"]},
        dry=False,
    )

    assert cur.inserted == [(77, "新问题", 1)]
    assert prompt_id_map == {
        ("旅游酒店", "已有问题"): 7001,
        ("旅游酒店", "新问题"): 7002,
    }


def test_collect_article_body_patches_reads_cleaned_markdown(tmp_path: Path):
    industry_dir = tmp_path / "2026-06-04_旅游酒店"
    clean_dir = industry_dir / "引用原文_清洗后"
    clean_dir.mkdir(parents=True)
    (industry_dir / "2026-06-04_统计结果.csv").write_text(
        "序号,查询内容,AI平台,引用编号,引用标题,引用链接,引用来源平台,引用摘要\n",
        encoding="utf-8",
    )
    (industry_dir / "2026-06-04_文章上榜统计.csv").write_text(
        "文章文件,原文链接,标题,来源域名,被几个AI引用,被哪些AI引用,触发的查询编号,抓取是否成功,清洗后字数\n"
        "ctrip_a1.md,https://example.com/a?utm=1,亲子酒店指南,example.com,2,deepseek|qwen,1|2,是,888\n",
        encoding="utf-8-sig",
    )
    (clean_dir / "ctrip_a1.md").write_text(
        "---\nurl: https://example.com/a?utm=1\ntitle: 亲子酒店指南\n---\n# 亲子酒店指南\n这是清洗后的正文。",
        encoding="utf-8",
    )
    csv_info = discover_csv_files(tmp_path)[0]

    patches = collect_article_body_patches([csv_info])

    assert len(patches) == 1
    patch = patches[0]
    assert patch.industry_name == "旅游酒店"
    assert patch.url_raw == "https://example.com/a?utm=1"
    assert patch.cleaned_char_count == 888
    assert "这是清洗后的正文" in patch.body
    assert not patch.body.startswith("---")


def test_backfill_article_cleaned_content_updates_by_url_hash_and_industry():
    class FakeCursor:
        def __init__(self):
            self.calls = []
            self.rowcount = 1

        def execute(self, sql, params):
            self.calls.append((sql, params))

    patch = ArticleBodyPatch(
        industry_name="旅游酒店",
        url_raw="https://example.com/a",
        url_norm="example.com/a",
        url_hash="hash-a",
        article_file="a.md",
        title="标题",
        body="正文" * 300,
        cleaned_char_count=600,
    )
    cur = FakeCursor()

    stats = step5b_backfill_article_cleaned_content(cur, [patch], dry=False)

    assert stats["article_bodies_loaded"] == 1
    assert stats["article_bodies_updated"] == 1
    sql, params = cur.calls[0]
    assert "UPDATE geo_research_articles" in sql
    assert "WHERE url_hash = %s" in sql
    assert "AND primary_industry = %s" in sql
    assert "inline_cleaned_content IS NULL" in sql
    assert params[0] == patch.body
    assert params[2] == "legacy_csv_md_v1"
    assert params[3] == "hash-a"
    assert params[4] == "旅游酒店"


def test_legacy_dedup_script_is_scoped_and_dry_run_by_default():
    sql = Path("scripts/dedup_legacy_research_csv_rerun_2026_06_15.sql").read_text(
        encoding="utf-8"
    )
    upper_sql = sql.upper()

    assert "TRUNCATE" not in upper_sql
    assert "DROP TABLE" not in upper_sql
    assert "ROUND_ID LIKE 'ROUND_LEGACY_%'" in upper_sql
    assert "source = 'seed_history'" in sql
    assert "idx_geo_research_prompts_seed_history_unique_text" in sql
    assert "idx_geo_research_round_call_legacy_unique" in sql
    assert "ON geo_research_round_call(round_id, industry_id, prompt_text, platform)" in sql
    assert sql.rstrip().endswith("ROLLBACK;")
