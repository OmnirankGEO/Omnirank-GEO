# -*- coding: utf-8 -*-
"""R2-1 · 存量已核验证据的**只读**重绑评估(2026-08-15)。

生产实证(Codex 只读取证):verified 历史条目 1,097 条**全部缺 binding_state**,
其中 847 条无 entity 字段(无轴)、250 条带 entity(候选)。

R5/R5.1 后的口径(裁定 §9.1 + 开发原则第 1/3 条):
  - 消费面收口为**唯一消费 API**(evidence_pack.iter_entity_admissible_items):
    写作渲染两段、修复链两处(来源提示/数字资格)、优势评分、正文 marker
    归属替换全走它;业务代码禁止裸取 items 列表(tests/test_p0_2 白名单
    计数锁,语法污点扫描连输两回后弃用);
  - 门语义:binding 存在只认 entity_confirmed;缺 binding 带 entity = 候选;
    **无轴条目零降级零标记**(「无 lane 标记=候选」方案已撤回 —— 拿元数据
    不完整代替真实性判断 = 批量降级网络素材);
  - 无轴条目的**错实体归属**风险由修复链**主体核对**在消费端管
    (span_level_repair.repair_subject_admits:修品牌 X 的 span 只认
    confirmed 于 X,或无 entity 且标题/claim 不含白名单里 X 以外的名字;
    白名单 = pack 自带 entity 名 = 客户+竞品;单品牌 pack 无主体素材照常可用)。
本脚本的分布统计按 entity_binding_state 复算,另按「有无 entity 字段」分桶:
无轴桶(847)不降级、不重绑,列出仅作口径对账;候选桶(250)的出路才是
Owner 另行签发的注记回填。
本脚本做的是**评估**:
用现役确定性判别器(entity_binding_state,R2-4 核心词口径)对存量逐条重算
「若重绑会得到什么态」,输出分布报告与逐条 CSV,供 Owner/Review 决定是否
另行签发一次注记回填。

🔴 只读:本脚本**零写入**(无 --apply 模式;真要回填是另一张单独签发的单,
不由评估脚本代拍)。评估对象 = articles.evidence_pack 里
verification_status ∈ VERIFIED_STATES 且缺 binding_state 的条目。

用法:
    python scripts/rebind_assessment_2026_08_15.py [--csv out.csv]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def assess_item(item: dict[str, Any], brand_name: str) -> str:
    """单条评估(纯函数,判别测试打这里)。

    实体基准 = 条目自带 entity 归属;缺 entity 的用品牌名(评估"能否绑回本品牌")。
    返回「若重绑」的态;评估不写库。
    """
    from writing.evidence_research import entity_binding_state

    entity = str(item.get("entity") or "").strip() or str(brand_name or "").strip()
    if not entity:
        return "no_entity_reference"
    return entity_binding_state(item, entity)


def run_assessment(csv_path: str | None) -> dict[str, Any]:
    from db.connection import get_db
    from writing.evidence_pack import VERIFIED_STATES

    dist: dict[str, int] = {}
    rows_out: list[dict[str, Any]] = []
    scanned_items = 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.id AS article_id, a.evidence_pack, b.name AS brand_name
              FROM articles a
              JOIN quotes q ON q.id = a.quote_id
              LEFT JOIN brands b ON b.id = q.brand_id
             WHERE a.evidence_pack IS NOT NULL
             ORDER BY a.id
            """
        )
        for row in cur.fetchall() or []:
            pack = row.get("evidence_pack")
            if not isinstance(pack, dict):
                continue
            from writing.evidence_pack import raw_pack_items

            for item in raw_pack_items(pack):
                if not isinstance(item, dict):
                    continue
                if item.get("verification_status") not in VERIFIED_STATES:
                    continue
                if str(item.get("binding_state") or "").strip():
                    continue  # 已有绑定的不在本评估范围
                scanned_items += 1
                verdict = assess_item(item, str(row.get("brand_name") or ""))
                dist[verdict] = dist.get(verdict, 0) + 1
                # [R5.1] 无轴桶单独立账(847 形态):零降级零标记,仅作
                # 口径对账;would_bind 对这桶只是参考值。候选桶(带 entity)
                # 才是重绑评估的对象。
                axis_bucket = ("has_entity" if str(item.get("entity") or "").strip()
                               else "no_axis_unmarked")
                dist[f"bucket:{axis_bucket}"] = dist.get(f"bucket:{axis_bucket}", 0) + 1
                rows_out.append({
                    "article_id": row["article_id"],
                    "evidence_id": item.get("evidence_id"),
                    "url": item.get("url"),
                    "entity": item.get("entity") or "",
                    "axis_bucket": axis_bucket,
                    "would_bind": verdict,
                })
    if csv_path:
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["article_id", "evidence_id", "url", "entity",
                            "axis_bucket", "would_bind"],
            )
            writer.writeheader()
            writer.writerows(rows_out)
    return {"read_only": True, "scanned_verified_unbound": scanned_items, "distribution": dist}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=None, help="逐条评估结果 CSV 输出路径")
    args = parser.parse_args()
    print(f"[READ-ONLY] {run_assessment(args.csv)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
