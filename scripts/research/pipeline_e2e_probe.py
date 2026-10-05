"""管道端到端实测:确定词 → 规划 → 选题上下文 → 生成 prompt

Owner 2026-08-03:「整个管道要测试好再交付」。

## 为什么需要这个而不是只靠单测

单测里所有 SQL 都被桩掉了 —— 桩过不了「列名对不对」「JOIN 成不成立」
这一关,而那恰恰是这条链最容易错的地方(本批已经栽过两次:
`contact_display` 列不存在、`is_core` 过滤漏掉)。

所以这里用**真 Postgres + 生产同款 DDL** 跑一遍,不打桩:
  ① confirmed_keywords JOIN quotes → 客户买了的词(含 is_core 闸)
  ② 规划:配额 − 已产出 = 缺口
  ③ 按行业选钩子 + B端/C端 表达
  ④ 组装出真实 prompt,检查实测结论是否真的进去了

⚠️ **不跑**的:LLM 生成、生图、发布。那三段要花钱且依赖外部服务,
   本机没有可用 key(见交付单)。所以这不是"全链路通过",
   是**数据链 + 规划 + prompt 组装通过**。别把它读成前者。

用法:
    docker run -d --name geo-e2e-pg -e POSTGRES_PASSWORD=t -e POSTGRES_USER=t \\
        -e POSTGRES_DB=geo_e2e -p 55888:5432 pgvector/pgvector:pg16
    python scripts/research/pipeline_e2e_probe.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("E2E_DATABASE_URL",
                     "postgresql://t:t@127.0.0.1:55888/geo_e2e")
os.environ["DATABASE_URL"] = DSN

# 生产同款 DDL(列名/类型从只读通道核过,不是照着代码猜的)
DDL = """
CREATE TABLE IF NOT EXISTS quotes (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER,
    status TEXT
);
CREATE TABLE IF NOT EXISTS confirmed_keywords (
    id SERIAL PRIMARY KEY,
    quote_id INTEGER,
    keyword TEXT NOT NULL,
    required_articles INTEGER,
    brand_id INTEGER,
    is_core BOOLEAN
);
CREATE TABLE IF NOT EXISTS geo_douyin_posts (
    id SERIAL PRIMARY KEY,
    brand_id INTEGER,
    keyword TEXT,
    status TEXT,
    deleted_at TIMESTAMPTZ
);
"""

FIXTURE = """
TRUNCATE quotes, confirmed_keywords, geo_douyin_posts RESTART IDENTITY;
INSERT INTO quotes (id, brand_id, status) VALUES
    (1, 900, 'confirmed'),
    (2, 900, 'paid'),
    (3, 900, 'draft');          -- 草稿单:它的词**不该**出现
INSERT INTO confirmed_keywords (quote_id, keyword, required_articles, brand_id, is_core) VALUES
    (1, '深圳全屋定制',   8, 900, TRUE),
    (1, '定制衣柜价格',   3, 900, TRUE),
    (2, '板材环保等级',  12, 900, NULL),   -- NULL 也算核心词(IS NOT FALSE)
    (1, '全屋定制覆盖词', 5, 900, FALSE),  -- 覆盖词:**不该**出现
    (3, '草稿单里的词',   9, 900, TRUE);   -- 草稿单:**不该**出现
INSERT INTO geo_douyin_posts (brand_id, keyword, status) VALUES
    (900, '深圳全屋定制', 'ready'),
    (900, '深圳全屋定制', 'published'),
    (900, '定制衣柜价格', 'failed');       -- 失败的不算已产出
"""

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"   {'✅' if cond else '🔴'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def setup() -> None:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        cur.execute(FIXTURE)
        conn.commit()
    finally:
        conn.close()


async def main() -> int:
    print(f"管道端到端实测 · 真 Postgres({DSN.rsplit('@', 1)[-1]})\n")
    setup()

    # ── ① 客户买了的词 ──
    print("① 确定词(confirmed_keywords JOIN quotes,真 SQL 不打桩)")
    from services.geo_douyin.topic_distiller import load_purchased_keywords
    rows = await load_purchased_keywords(900)
    kws = [r["keyword"] for r in rows]
    print(f"   取到: {kws}")
    check("只取已确认/已付款的报价", "草稿单里的词" not in kws)
    check("覆盖词被 is_core 挡掉", "全屋定制覆盖词" not in kws,
          "生产实测 21% 是覆盖词,漏这刀会拿客户没买的词做内容")
    check("is_core=NULL 视为核心词", "板材环保等级" in kws)
    check("三个核心词齐了", len(kws) == 3, f"实得 {len(kws)}")
    check("配额带回来了",
          any(r["required_articles"] == 12 for r in rows))

    # ── ② 规划 ──
    print("\n② 规划(配额 − 已产出 = 缺口)")
    from services.geo_douyin.content_plan import build_content_plan
    plan = await build_content_plan(900)
    by = {p.keyword: p for p in plan.items}
    for p in plan.items:
        print(f"   {p.keyword:<14} 配额{p.quota:>3} 已做{p.done:>3} "
              f"还差{p.gap:>3} 建议{p.suggested:>3}")
    check("已产出只数成品态", by["深圳全屋定制"].done == 2,
          "ready+published 算,failed 不算")
    check("失败的不计入已产出", by["定制衣柜价格"].done == 0)
    check("缺口 = 配额 − 已产出", by["深圳全屋定制"].gap == 6)
    check("单词建议数收进上限 9", by["板材环保等级"].suggested == 9,
          f"配额 12 → 建议 {by['板材环保等级'].suggested}")
    check("缺口大的排前面", plan.items[0].keyword == "板材环保等级")

    # ── ③ 按行业选表达 ──
    print("\n③ 表达方式(实测回写)")
    from services.geo_douyin.card_templates import (audience_style_block,
                                                    pick_hook)
    check("试点行业走避坑式(疑问式实测 −5.2pp)",
          pick_hook("home_improvement") == "warning")
    check("时尚美妆才走疑问式(+7.3pp)", pick_hook("时尚美妆") == "question")
    check("未知行业落到跨层最稳的避坑式", pick_hook("xxx") == "warning")
    b, c = audience_style_block("technology"), audience_style_block("home_improvement")
    check("B端/C端 给的是不同约束", b != c)
    check("B端禁「攻略/科普」(实测 technology −7.7pp)", "攻略" in b)

    # ── ④ 真实 prompt 组装 ──
    print("\n④ prompt 组装(检查实测结论真的进去了)")
    import services.geo_douyin.content_generator as cg
    captured = {}

    async def _spy(prompt, **kw):
        captured["prompt"] = prompt
        return None            # 不真调 LLM(要花钱且本机无 key)

    orig = cg._call_llm
    cg._call_llm = _spy
    try:
        await cg.generate_image_post_content(
            "深圳全屋定制", city="深圳", card_count=4,
            industry_key="home_improvement")
    finally:
        cg._call_llm = orig

    p = captured.get("prompt", "")
    check("prompt 真的生成了", bool(p), f"{len(p)} 字符")
    check("首图钩子进了 prompt", "避坑式" in p)
    check("没有把负向的疑问式当默认写进去",
          "疑问式(怎么选" not in p)
    check("B端/C端 受众约束进了 prompt", "个人消费者" in p)
    check("第一人称是硬要求", "第一人称,硬要求不是建议" in p or "硬要求" in p)
    check("长度别一刀切的限定进了 prompt", "不跨行业成立" in p)
    check("张数职责表进了 prompt", "封面" in p and "收尾" in p)

    print()
    if FAILURES:
        print(f"🔴 {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 数据链 + 规划 + 表达 + prompt 组装 全通过")
    print("⚠️ 未覆盖(本机无 key,非本脚本能力问题):LLM 生成 / 生图 / 发布")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
