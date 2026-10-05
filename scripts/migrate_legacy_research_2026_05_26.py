"""
GEO 调研数据迁移 · 本地 AI回答爬虫 (16 轮 ~1.6 万篇) → 公司 PostgreSQL [Phase 9 · 2026-05-26]

老板要求: "所有调研结果都可以保存,包括前两天我们在本地跑的"

源:
  C:\\Users\\OmniR\\Desktop\\AI回答爬虫\\results\\
    20260521_222512_geo服务/
      meta.json              {industry, prompt_count, created_at, pipeline_version}
      prompts.txt            一行一 prompt
      raw_responses.json     [{platform, prompt_id, prompt, answer, citations, ok}]
      articles_clean/        *.md (frontmatter: url/normalized/title/cited_by/prompt_ids/fetch_ok)
      article_index.csv      filename, url, normalized, domain, title, cited_by, ...

目标:
  geo_research_batches      每轮 1 行 · batch_id 前缀 'legacy_<created_at>_<industry>'
  geo_research_raw          每条 citation 1 行 · 11 字段对齐 placement_service:1769
  geo_research_articles     去重 url_hash · 写 cleaned 到 inline_cleaned_content (OSS 不上传)
  geo_research_article_citations  多对多关联 (article × round × platform × prompt)

不动:
  现有跑批数据 · 不影响 month_weights 月份加权 (老 batch_id 自动衰减)
  geo_research_industries / prompts 表 · 仅 INSERT 引用记录

跑法:
  python scripts/migrate_legacy_research_2026_05_26.py --dry-run         # 只算不写
  python scripts/migrate_legacy_research_2026_05_26.py --rounds 1       # 跑前 1 个轮次试
  python scripts/migrate_legacy_research_2026_05_26.py                  # 全量(16 轮)

安全:
  - 每轮独立事务 · 单轮失败不污染其它
  - INSERT IGNORE 模式 (geo_research_batches.batch_id UNIQUE · geo_research_articles.url_hash UNIQUE)
  - 重跑幂等
  - 末尾触发 aggregate_research_stats(['<行业>']) 让矩阵立刻可见
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 让脚本能从 geo_agentscope/ 启动
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from db.connection import get_connection  # noqa: E402

LOCAL_RESULTS_DIR = Path(r"C:\Users\OmniR\Desktop\AI回答爬虫\results")

# URL 归一化(跟公司 services/research_monitor/url_normalizer.py 风格一致)
_WWW_PREFIXES = ('www.', 'm.', 'mobile.', 'wap.', '3g.')


def normalize_url(url: str) -> str:
    """简化版 URL 归一化: 去 www/m 前缀 · 去 query/fragment · 去尾斜杠"""
    if not url:
        return ""
    from urllib.parse import urlparse
    p = urlparse(url)
    netloc = (p.netloc or "").lower()
    for prefix in _WWW_PREFIXES:
        if netloc.startswith(prefix):
            netloc = netloc[len(prefix):]
            break
    path = (p.path or "").rstrip("/")
    return f"{netloc}{path}"


def compute_url_hash(normalized: str) -> str:
    """跟公司 url_normalizer.compute_url_hash 一致(SHA1 hex)"""
    return hashlib.sha1((normalized or "").encode("utf-8")).hexdigest()


def compute_content_hash(content: str) -> str:
    """跟 services/research_monitor/content_hash.py 一致: SHA256(前 1000 字 normalize whitespace)"""
    text = (content or "")[:1000]
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_frontmatter_and_body(md: str) -> tuple[dict, str]:
    """从 articles_clean/*.md 拆 frontmatter 字典 + body"""
    if not md.startswith("---\n"):
        return {}, md
    idx = md.find("\n---\n", 4)
    if idx == -1:
        return {}, md
    fm_raw = md[4:idx]
    body = md[idx + 5:]
    fm = {}
    for line in fm_raw.splitlines():
        if ':' not in line:
            continue
        k, _, v = line.partition(':')
        fm[k.strip()] = v.strip()
    return fm, body


def make_batch_id(created_at: str, industry: str) -> str:
    """legacy_<created_at>_<行业 slug>"""
    safe_industry = re.sub(r'\W+', '_', industry)[:30]
    return f"legacy_{created_at}_{safe_industry}"


def load_round_data(round_dir: Path) -> dict | None:
    """读一个轮次目录的关键数据"""
    meta_file = round_dir / "meta.json"
    raw_file = round_dir / "raw_responses.json"
    clean_dir = round_dir / "articles_clean"
    if not meta_file.exists() or not raw_file.exists():
        print(f"  [skip] {round_dir.name} 缺 meta.json 或 raw_responses.json")
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    raw_responses = json.loads(raw_file.read_text(encoding="utf-8"))
    return {
        "dir": round_dir,
        "industry": meta.get("industry", "").strip(),
        "created_at": meta.get("created_at", round_dir.name[:15]),
        "raw_responses": raw_responses,
        "clean_dir": clean_dir,
    }


def _cleanup_half_migrated(cur, batch_id: str) -> int:
    """P10 fix: 重跑前清理本 batch_id 已存在的半迁数据 · 让重试有干净起点
    deleted_rows = raw 行 + article_citations 行 + round
    geo_research_articles 不删 (跨 batch 复用 · 别的轮次可能也指这个 article)
    """
    deleted = 0
    cur.execute("DELETE FROM geo_research_article_citations WHERE round_id = %s", (batch_id,))
    deleted += cur.rowcount
    cur.execute("DELETE FROM geo_research_raw WHERE batch_id = %s", (batch_id,))
    deleted += cur.rowcount
    cur.execute("DELETE FROM geo_research_round WHERE round_id = %s", (batch_id,))
    deleted += cur.rowcount
    # batches 行不删 (重跑直接用),仅 reset status
    cur.execute(
        "UPDATE geo_research_batches SET status = 'retrying' WHERE batch_id = %s",
        (batch_id,),
    )
    return deleted


def _check_already_migrated(cur, batch_id: str) -> str:
    """P10 fix: 查 batch 是否已完整迁过 · 返回 status:
       'absent'   不存在 → 全新迁
       'completed' 已完整迁过 → SKIP 整轮
       'partial'   半迁 (failed / retrying / processing) → 重跑前先 cleanup
    """
    cur.execute("SELECT status FROM geo_research_batches WHERE batch_id = %s", (batch_id,))
    row = cur.fetchone()
    if not row:
        return 'absent'
    s = (row.get('status') or '').lower()
    if s == 'completed':
        return 'completed'
    return 'partial'  # processing/failed/retrying/其它


def migrate_one_round(round_data: dict, dry_run: bool = False) -> dict:
    """迁移一个轮次 · 整轮单事务 · 重跑幂等"""
    industry = round_data["industry"]
    created_at = round_data["created_at"]
    raw_responses = round_data["raw_responses"]
    clean_dir = round_data["clean_dir"]
    batch_id = make_batch_id(created_at, industry)

    print(f"\n=== 迁移 {round_data['dir'].name} ===")
    print(f"  行业: {industry} · batch_id: {batch_id}")
    print(f"  AI 调用: {len(raw_responses)} 条 · clean MD: {len(list(clean_dir.glob('*.md'))) if clean_dir.exists() else 0} 篇")

    if dry_run:
        print("  [dry-run] 跳过 DB 写入")
        return {"batch_id": batch_id, "industry": industry, "skipped": True, "reason": "dry-run"}

    inserted_citations = 0
    inserted_articles = 0
    inserted_article_citations = 0
    skipped_existing = 0

    # 1. 准备 ts 转 PostgreSQL TIMESTAMPTZ (created_at 是 YYYYMMDD_HHMMSS)
    try:
        ts = datetime.strptime(created_at[:15], "%Y%m%d_%H%M%S")
    except Exception:
        ts = datetime.now()

    conn = get_connection()
    try:
        cur = conn.cursor()

        # P10 fix-A: 整轮幂等检查 · 避免重复灌入(geo_research_raw 没 UNIQUE 约束)
        prev_status = _check_already_migrated(cur, batch_id)
        if prev_status == 'completed':
            conn.commit()
            print(f"  [skip] batch_id={batch_id} 已 completed · 整轮跳过")
            return {
                "batch_id": batch_id, "industry": industry,
                "skipped": True, "reason": "already_completed",
            }
        if prev_status == 'partial':
            cleaned = _cleanup_half_migrated(cur, batch_id)
            print(f"  [recovery] 半迁状态 · 清理 {cleaned} 行 · 重新迁")
            conn.commit()

        # P10 fix-B: 整轮单事务起点 · INSERT batches (UNIQUE batch_id 防重跑)
        cur.execute(
            """
            INSERT INTO geo_research_batches (batch_id, industry, researcher, status, created_at, completed_at)
            VALUES (%s, %s, 'legacy:imported', 'processing', %s, NULL)
            ON CONFLICT (batch_id) DO UPDATE
                SET status = 'processing', completed_at = NULL
            """,
            (batch_id, industry, ts),
        )

        # INSERT round (round_id = batch_id · legacy_xxx 跟新跑批 research_auto_xxx 区分)
        cur.execute(
            """
            INSERT INTO geo_research_round
                (round_id, batch_id, triggered_by, status, current_stage,
                 started_at, finished_at, summary_json, created_at)
            VALUES (%s, %s, 'manual', 'running', 'legacy_imported',
                    %s, NULL, %s::jsonb, %s)
            ON CONFLICT (round_id) DO UPDATE
                SET status = 'running', finished_at = NULL
            """,
            (
                batch_id, batch_id,
                ts,
                json.dumps({
                    "source": "AI回答爬虫 本地迁移",
                    "ai_calls_total": len(raw_responses),
                    "import_at": datetime.now().isoformat(),
                }),
                ts,
            ),
        )

        # 4. 展开 raw_responses · 写 geo_research_raw + 收集 URL 集合
        unique_urls: dict[str, dict] = {}  # normalized → {url, title, prompt_ids: set, platforms: set}
        rows_to_insert: list[tuple] = []
        for r in raw_responses:
            if not r.get("ok"):
                continue
            platform = r.get("platform", "")
            prompt_id = r.get("prompt_id")
            prompt_text = r.get("prompt", "")
            answer = (r.get("answer") or "")[:5000]  # 截 5000 防过大
            for c in r.get("citations", []) or []:
                url = c.get("url")
                if not url:
                    continue
                norm = normalize_url(url)
                title = c.get("title", "") or ""
                # 累积 unique URL 元数据
                if norm not in unique_urls:
                    unique_urls[norm] = {
                        "url": url, "normalized": norm, "title": title,
                        "prompt_ids": set(), "platforms": set(),
                    }
                unique_urls[norm]["prompt_ids"].add(prompt_id)
                unique_urls[norm]["platforms"].add(platform)
                if not unique_urls[norm]["title"] and title:
                    unique_urls[norm]["title"] = title

                # cited_platform = 域名(归一化)
                from urllib.parse import urlparse
                cited_platform = urlparse(url).netloc or ""
                for prefix in _WWW_PREFIXES:
                    if cited_platform.startswith(prefix):
                        cited_platform = cited_platform[len(prefix):]
                        break

                rows_to_insert.append((
                    industry, prompt_text, platform,
                    cited_platform, c.get("rank") or 0,
                    url, title, "", answer,
                    batch_id, "legacy:imported",
                ))

        # P10 fix-B: 整轮单事务 · 不 chunk commit (否则失败时前面已 commit 行变孤儿)
        # geo_research_raw 没 UNIQUE 约束 · 重跑会重复 · 配合上面 _cleanup_half_migrated 保证幂等
        # executemany 一次 INSERT 全部 raw 行 · 一题轮次最多几千行 · PostgreSQL TOAST 能扛
        if rows_to_insert:
            cur.executemany(
                """
                INSERT INTO geo_research_raw
                    (industry, query, engine, cited_platform, cite_position, cite_url,
                     cite_title, cite_excerpt, answer_text, batch_id, researcher)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows_to_insert,
            )
            inserted_citations = len(rows_to_insert)
        print(f"  → geo_research_raw 写入 {inserted_citations} 条引用 (单事务)")

        # 5. 处理 articles_clean/*.md → geo_research_articles + citations
        if clean_dir.exists():
            md_files = list(clean_dir.glob("*.md"))
            for idx, mdf in enumerate(md_files):
                try:
                    md_full = mdf.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                fm, body = parse_frontmatter_and_body(md_full)
                url = fm.get("url", "").strip()
                if not url:
                    continue
                norm = fm.get("normalized") or normalize_url(url)
                url_hash = compute_url_hash(norm)
                title = (fm.get("title") or "")[:500]
                # 从 body 算 content_hash (跟公司 stage 3 算法一致)
                content_hash = compute_content_hash(body)
                cleaned_chars = len(re.sub(r"\s+", "", body))

                # 7 分类推断
                from urllib.parse import urlparse
                domain = urlparse(url).netloc or ""
                for prefix in _WWW_PREFIXES:
                    if domain.startswith(prefix):
                        domain = domain[len(prefix):]
                        break
                try:
                    from services.research_monitor.content_classifier import get_content_type
                    content_type = get_content_type(domain, url, cleaned_chars)
                except Exception:
                    content_type = None

                # 信誉级
                try:
                    from services.research_monitor.domain_tiering import get_domain_tier
                    domain_tier = get_domain_tier(domain) or 'gray'
                except Exception:
                    domain_tier = 'gray'

                # Phase 9 简化: 老数据不区分 dup (本地清洗时已大部去重) · in_library 直接写
                cur.execute(
                    """
                    INSERT INTO geo_research_articles
                        (url, url_hash, domain, title, primary_industry,
                         content_type, domain_tier,
                         oss_key_cleaned, inline_cleaned_content, cleaned_char_count,
                         content_hash, is_duplicate,
                         clean_status, clean_model, review_status,
                         first_seen_round_id, last_seen_at, fetched_at)
                    VALUES (%s, %s, %s, %s, %s,
                            %s, %s,
                            NULL, %s, %s,
                            %s, FALSE,
                            'cleaned', 'legacy_rule_v1', 'in_library',
                            %s, %s, %s)
                    ON CONFLICT (url_hash) DO NOTHING
                    RETURNING id
                    """,
                    (
                        url, url_hash, domain, title, industry,
                        content_type, domain_tier,
                        body, cleaned_chars,
                        content_hash,
                        batch_id, ts, ts,
                    ),
                )
                row = cur.fetchone()
                if row:
                    article_id = row['id']
                    inserted_articles += 1
                else:
                    # 已存在(跨轮 URL 复用)· 拿到 id 才能写 citation
                    cur.execute("SELECT id FROM geo_research_articles WHERE url_hash = %s", (url_hash,))
                    existing = cur.fetchone()
                    if not existing:
                        continue
                    article_id = existing['id']
                    skipped_existing += 1

                # P10 fix-C: article_citations 防重 · 不能用 ON CONFLICT (UNIQUE 含 prompt_id, 但 NULL 不参与冲突)
                # 改用显式 SELECT 1 ... WHERE 去重 · 同 round + 同 article + 同 platform 只写 1 条
                info = unique_urls.get(norm)
                if info:
                    for platform in info["platforms"]:
                        cur.execute(
                            """
                            SELECT 1 FROM geo_research_article_citations
                             WHERE article_id = %s AND round_id = %s AND platform = %s
                               AND prompt_id IS NULL
                             LIMIT 1
                            """,
                            (article_id, batch_id, platform),
                        )
                        if cur.fetchone():
                            continue  # 同 (article, round, platform, prompt_id=NULL) 已存在 · 跳过
                        cur.execute(
                            """
                            INSERT INTO geo_research_article_citations
                                (article_id, round_id, industry_id, prompt_id,
                                 platform, cited_at)
                            VALUES (%s, %s, NULL, NULL, %s, %s)
                            """,
                            (article_id, batch_id, platform, ts),
                        )
                        inserted_article_citations += 1

                # P10 fix-B: 不 chunk commit · 失败时整轮一起 rollback (上面 cleanup 已删半迁数据)
                if (idx + 1) % 200 == 0:
                    print(f"    ... {idx + 1}/{len(md_files)} 篇 (待整轮 commit)")
        print(f"  → geo_research_articles 写入 {inserted_articles} 新 + 复用 {skipped_existing} 老")
        print(f"  → geo_research_article_citations 写入 {inserted_article_citations} 条关联")

        # P10 fix-B: 整轮最终 commit + 标 completed
        cur.execute(
            "UPDATE geo_research_batches SET status = 'completed', completed_at = NOW() WHERE batch_id = %s",
            (batch_id,),
        )
        cur.execute(
            "UPDATE geo_research_round SET status = 'completed', finished_at = NOW() WHERE round_id = %s",
            (batch_id,),
        )
        conn.commit()
        print(f"  ✓ 整轮 commit + 标 completed")
    except Exception as e:
        conn.rollback()
        # 标 failed · 重跑时 _check_already_migrated 会触发 _cleanup_half_migrated
        try:
            cur2 = conn.cursor()
            cur2.execute(
                "UPDATE geo_research_batches SET status = 'failed' WHERE batch_id = %s",
                (batch_id,),
            )
            cur2.execute(
                "UPDATE geo_research_round SET status = 'failed_resumable', error_message = %s WHERE round_id = %s",
                (str(e)[:500], batch_id),
            )
            conn.commit()
        except Exception:
            pass
        print(f"  [error] 轮次迁移失败 · 整轮 rollback + 标 failed: {e}")
        raise
    finally:
        conn.close()

    return {
        "batch_id": batch_id,
        "industry": industry,
        "citations": inserted_citations,
        "articles_new": inserted_articles,
        "articles_reused": skipped_existing,
        "article_citations": inserted_article_citations,
    }


def trigger_aggregate(industries: list[str]) -> int:
    """末尾触发 PlacementService.aggregate_research_stats(industries) · 让月份加权矩阵立刻可见"""
    try:
        from services.placement_service import PlacementService
        svc = PlacementService()
        updated = svc.aggregate_research_stats(industries)
        print(f"\n=== 聚合完成 · {updated} 行 stats 更新 ===")
        return updated
    except Exception as e:
        print(f"[warn] 聚合失败(不阻塞迁移): {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="本地 AI回答爬虫 → 公司 PostgreSQL 迁移")
    parser.add_argument("--dry-run", action="store_true", help="只算不写")
    parser.add_argument("--rounds", type=int, default=0, help="只迁前 N 个轮次(0=全部)")
    parser.add_argument("--source", type=str, default=str(LOCAL_RESULTS_DIR), help="源目录")
    args = parser.parse_args()

    source_dir = Path(args.source)
    if not source_dir.exists():
        print(f"源目录不存在: {source_dir}")
        sys.exit(1)

    # 过滤候选轮次目录(有 meta.json 的)
    candidates = sorted([d for d in source_dir.iterdir()
                         if d.is_dir() and (d / "meta.json").exists()])
    if args.rounds > 0:
        candidates = candidates[:args.rounds]
    print(f"待迁移轮次: {len(candidates)} 个")
    for d in candidates:
        print(f"  - {d.name}")

    if args.dry_run:
        print("\n[dry-run] 仅扫描,不写 DB")
    else:
        ans = input(f"\n确认迁移 {len(candidates)} 轮到公司 PostgreSQL? [y/N] ")
        if ans.strip().lower() != 'y':
            print("中止")
            sys.exit(0)

    summaries = []
    industries_seen = set()
    failed_rounds = []
    for d in candidates:
        round_data = load_round_data(d)
        if not round_data:
            continue
        try:
            s = migrate_one_round(round_data, dry_run=args.dry_run)
            summaries.append(s)
            if s.get("industry") and not args.dry_run:
                industries_seen.add(s["industry"])
        except Exception as e:
            print(f"  ⚠ 轮次失败: {e}")
            failed_rounds.append((d.name, str(e)))
            continue

    # 触发聚合
    if industries_seen:
        trigger_aggregate(sorted(industries_seen))

    # 总结
    print("\n=" * 30 + " 迁移总结 " + "=" * 30)
    total_citations = sum(s.get("citations", 0) for s in summaries)
    total_articles = sum(s.get("articles_new", 0) for s in summaries)
    total_reused = sum(s.get("articles_reused", 0) for s in summaries)
    total_assoc = sum(s.get("article_citations", 0) for s in summaries)
    print(f"  成功轮次: {len(summaries)}/{len(candidates)}")
    print(f"  geo_research_raw 写入: {total_citations} 条")
    print(f"  geo_research_articles 新增: {total_articles} 篇 · 跨轮复用: {total_reused} 篇")
    print(f"  article_citations 关联: {total_assoc} 条")
    print(f"  涉及行业: {len(industries_seen)} 个 → {sorted(industries_seen)}")
    if failed_rounds:
        print(f"\n  ❌ 失败 {len(failed_rounds)} 轮:")
        for n, e in failed_rounds:
            print(f"    - {n}: {e[:120]}")


if __name__ == "__main__":
    main()
