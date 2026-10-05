"""[答案实体] 手动/cron rebuild CLI 包装(文档级 · 不进自动迁移链)。

用法(手动 psql/python,shadow-only,不部署):
    python -m scripts.rebuild_geo_research_answer_entities_shadow --industry 教育培训   # 默认 dry-run 只计数不调 LLM
    python -m scripts.rebuild_geo_research_answer_entities_shadow --industry 教育培训 --write --limit 200  # 真跑 LLM 写 shadow

🔴 默认 dry-run(与服务层/API 一致)· 必须显式 --write 才真跑 LLM + 写 shadow(防误触发成本)。
服务层实现在 services/research_monitor/answer_entity_extractor.rebuild_answer_entities;
本脚本只做入参解析 + 建表兜底,真跑仍走后台/CLI(不在 web 请求内 inline 全库)。
"""

from __future__ import annotations

import argparse
import asyncio
import json


async def rebuild(industry: str = "", limit: int = 200, dry_run: bool = True,
                  only_pending: bool = True) -> dict:
    """薄封装:建表兜底 + 调服务层 rebuild_answer_entities。"""
    from db.research_answer_entity_db import init_research_answer_entity_tables
    from services.research_monitor.answer_entity_extractor import rebuild_answer_entities
    init_research_answer_entity_tables()
    return await rebuild_answer_entities(
        industry=industry, limit=limit, dry_run=dry_run, only_pending=only_pending,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="rebuild GEO research answer-entity shadow")
    parser.add_argument("--industry", default="", help="行业(空=全部)")
    parser.add_argument("--limit", type=int, default=200, help="单批最多处理答案组数")
    parser.add_argument("--write", action="store_true", help="🔴 真跑 LLM + 写 shadow(默认 dry-run 只计数不调 LLM)")
    parser.add_argument("--all", action="store_true", help="重抽已抽过的(默认只抽 pending)")
    args = parser.parse_args()
    result = asyncio.run(rebuild(
        industry=args.industry, limit=args.limit,
        dry_run=not args.write, only_pending=not args.all,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
