"""
回填 geo_research_industries + geo_research_prompts (来自已迁入的 raw 数据) [P12 · 2026-05-26]

老板反馈:
  调研后台 → 行业管理/Prompts 管理 显示空 ("暂无行业") · 但 raw 数据有 17 行业 × ~25 prompts
  根因: migrate_legacy 只写了 raw/articles/citations/batches/stats · 没回填 industries/prompts

修法:
  1. SELECT DISTINCT industry FROM geo_research_raw → INSERT geo_research_industries (active=TRUE)
     name = industry · slug = pinyin-style 或 industry (中文 slug OK · DB 约束 100 字符)
  2. SELECT industry, DISTINCT query FROM geo_research_raw GROUP BY industry, query
     → INSERT geo_research_prompts (industry_id JOIN · active=TRUE · source='seed_history')
  3. 幂等: ON CONFLICT (name) DO NOTHING (industries) · (industry_id, prompt_text) 应用层去重 (prompts 表无 UNIQUE)
  4. (P12-fix-v2 2026-05-26 删) 行业 prompts 不设上限 · 灌入所有 raw 唯一 query
     跑批配额由后端 budget_guard 按月度预算自然限速

跑法:
  python scripts/backfill_industries_prompts_from_raw_2026_05_26.py --dry-run
  python scripts/backfill_industries_prompts_from_raw_2026_05_26.py

安全:
  - 单事务 · 失败回滚
  - 不动 raw / articles / citations 任何字段
  - industries.slug 用 simple slug-ify (中文保留 · 因 DB 是 VARCHAR(100) 没 ASCII 约束)
  - prompts.source='seed_history' 标记来源 · admin 可后续编辑/删除
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能从 geo_agentscope/ 启动
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv()

from db.connection import get_connection  # noqa: E402


# P12-fix-v2 (2026-05-26 老板拍板): prompts 行业总数不限
# 旧 MAX_PROMPTS_PER_INDUSTRY = 25 已删 · 本脚本灌入所有 raw 唯一 query · 去重幂等
# 跑批配额由后端 budget_guard 按月度预算自然限速


# Issue 1 修 (HIGH · 2026-05-26): backfill 不能再写中文 slug
# 前后端都要 ASCII slug (前端 ^[a-z0-9_]+$ · 后端同正则)
# 老板找的 review 指出: 中文 slug 入库后,管理员一编辑保存就 422 拒
# 修: 维护 15 行业 → ASCII slug 显式映射 (跟项目里 default_industries 对齐)
# 未来加新行业 · 要么先经管理页 CRUD (前端会强制 ASCII) · 要么往下加 entry
INDUSTRY_NAME_TO_SLUG: dict[str, str] = {
    '房地产':     'real_estate',
    '汽车':       'automotive',
    '教育培训':   'education',
    '医疗健康':   'healthcare',
    '金融理财':   'finance',
    '科技数码':   'tech',
    '食品餐饮':   'food',
    '旅游酒店':   'travel',
    '母婴亲子':   'parenting',
    '时尚美妆':   'fashion',
    '法律商务':   'legal',
    '装修建材':   'home_decor',
    '企业服务':   'enterprise',
    '电商零售':   'ecommerce',
    '文娱游戏':   'entertainment',
}

import re as _re  # local alias · 仅 _slugify 用
_SLUG_RE = _re.compile(r'^[a-z0-9_]+$')


def _slugify(name: str) -> str:
    """Issue 1 修: 必须输出 ASCII slug (前后端校验)
    优先映射表 → 全 ASCII fallback (lowercase) → 否则抛错让运维补映射
    """
    s = name.strip()
    if s in INDUSTRY_NAME_TO_SLUG:
        return INDUSTRY_NAME_TO_SLUG[s]
    # 全 ASCII 行业名直接 lowercase
    ascii_lower = s.replace(' ', '_').replace('-', '_').lower()
    if _SLUG_RE.match(ascii_lower):
        return ascii_lower[:100]
    # 真不知道怎么转 · 抛错 · 提示运维加 INDUSTRY_NAME_TO_SLUG entry
    raise ValueError(
        f"行业 {name!r} 没拼音 slug 映射 · "
        f"请到 scripts/backfill_industries_prompts_from_raw_2026_05_26.py "
        f"的 INDUSTRY_NAME_TO_SLUG 加 entry (例: '{name}': 'your_ascii_slug')"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='只算不写')
    parser.add_argument('--exclude-industries', nargs='*', default=['GEO', '通用'],
                        help='排除的伪行业(默认: GEO + 通用 · raw 里少量测试数据)')
    args = parser.parse_args()

    conn = get_connection()
    try:
        c = conn.cursor()

        # 1) 找出 raw 里所有真实行业 (排除测试用伪行业)
        excludes = args.exclude_industries
        if excludes:
            placeholder = ','.join(['%s'] * len(excludes))
            c.execute(
                f"SELECT DISTINCT industry FROM geo_research_raw "
                f"WHERE industry IS NOT NULL AND industry NOT IN ({placeholder}) "
                f"ORDER BY industry",
                excludes,
            )
        else:
            c.execute("SELECT DISTINCT industry FROM geo_research_raw "
                      "WHERE industry IS NOT NULL ORDER BY industry")
        industries = [r['industry'] for r in c.fetchall()]
        print(f"[scan] raw 有 {len(industries)} 个真实行业 (排除 {excludes}):")
        for i in industries:
            print(f"  - {i}")

        if args.dry_run:
            print()
            print("[dry-run] 跳过写库 · 仅扫描")
        else:
            # 2) 回填 industries 表 (幂等 ON CONFLICT)
            print()
            print(f"[backfill] industries 回填...")
            ind_new = 0
            ind_existing = 0
            ind_id_map: dict[str, int] = {}
            for idx, name in enumerate(industries):
                slug = _slugify(name)
                c.execute(
                    """
                    INSERT INTO geo_research_industries
                        (name, slug, sort_order, weight, active, ai_seed_done, version)
                        VALUES (%s, %s, %s, 1.0, TRUE, FALSE, 1)
                        ON CONFLICT (name) DO UPDATE
                            SET updated_at = NOW()
                        RETURNING id, (xmax = 0) AS inserted
                    """,
                    (name, slug, idx * 10),
                )
                row = c.fetchone()
                ind_id_map[name] = row['id']
                if row['inserted']:
                    ind_new += 1
                else:
                    ind_existing += 1
            print(f"  industries: 新增 {ind_new} 已存在 {ind_existing}")

            # 3) 回填 prompts 表
            #    策略 (P12-fix-v2): 一个 industry 内取 DISTINCT query 按 raw 引用数 DESC 全量导入
            #          已存在 (industry_id, prompt_text) 不重复 insert (应用层去重)
            #          行业 prompts 数量不再有业务上限 · 跑批配额由 budget_guard 限速
            print()
            print(f"[backfill] prompts 回填 (无行业上限 · 全量 raw 唯一 query)...")
            prompts_new_total = 0
            prompts_skipped_dup_total = 0
            for name in industries:
                ind_id = ind_id_map.get(name)
                if not ind_id:
                    continue

                # P12-fix-v2: 删 quota · 灌入所有 raw 唯一 query · 去重幂等
                c.execute(
                    """
                    SELECT query, COUNT(*) AS cnt
                      FROM geo_research_raw
                     WHERE industry = %s AND query IS NOT NULL AND query != ''
                     GROUP BY query
                     ORDER BY cnt DESC, query ASC
                    """,
                    (name,),
                )
                queries = c.fetchall()
                total = len(queries)

                # 已存在的 prompt_text (无论 active 与否 · 防 INSERT 重复)
                c.execute(
                    "SELECT prompt_text FROM geo_research_prompts WHERE industry_id = %s",
                    (ind_id,),
                )
                existing = {r['prompt_text'] for r in c.fetchall()}

                added = 0
                for sort_idx, q in enumerate(queries):
                    if q['query'] in existing:
                        prompts_skipped_dup_total += 1
                        continue
                    c.execute(
                        """
                        INSERT INTO geo_research_prompts
                            (industry_id, prompt_text, sort_order, active, source,
                             is_sensitive, version)
                            VALUES (%s, %s, %s, TRUE, 'seed_history', FALSE, 1)
                        """,
                        (ind_id, q['query'], sort_idx * 10),
                    )
                    added += 1
                    prompts_new_total += 1
                skip_msg = f" · 跳重 {total - added}" if added < total and len(existing) > 0 else ''
                print(f"  {name:15} +{added} prompts (raw 有 {total} 个唯一 query){skip_msg}")

            conn.commit()
            print()
            print(f"[done]")
            print(f"  industries: 新增 {ind_new} · 已存在 {ind_existing}")
            print(f"  prompts:    新增 {prompts_new_total} · 跳重 {prompts_skipped_dup_total}")

    except Exception as e:
        conn.rollback()
        print(f"[FAIL] {type(e).__name__}: {e}")
        raise
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
