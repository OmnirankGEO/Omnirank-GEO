"""P2 标题年份「分文体条件默认」的实验预登记入口(2026-08-08)。

## 为什么是一个脚本,不是代码里的一张静态清单

`services/article_experiment_registry` 里**没有**任何代码内置实验清单 —— 实验行只活在
`geo_article_experiments` 表里(`create_experiment()` 直接 INSERT)。在代码里再摆一份
静态清单就是死元数据。所以预登记的**声明**放在
`writing/title_element_contract.TITLE_YEAR_EXPERIMENT_PREREGISTRATION`(假设/主指标/
臂位/样本门槛),**建行**这一步由本脚本执行,两边同读一份声明,不各抄一遍。

## 🔴 诚实边界:现有随机化机制接不住本次改动

`assign_article()` 校验 `article.style_version == 该臂的 version_id`,即臂位是钉在
**style version** 上的。本包改的是"文体默认值"(合同层),不是一个 style version ——
所以本脚本**要求调用方显式传入两个已存在的 style version id** 作为两臂载体:
control 侧承载改动前口径、candidate 侧承载合同口径。这两个版本不就位时,脚本
**响亮失败并退出非零**,不会静默建一条永远分不到文章的实验。

这是本包自曝的口子,不是我假装做完了:把标题默认做成可随机化的臂,需要在生成链上
让 style version 真正携带标题合同版本 —— 那是另一个包的活,不在 P2 范围。

## 用法

    python scripts/preregister_title_year_experiment_2026_08_08.py \
        --baseline-version-id <control 侧 style version id> \
        --candidate-version-id <candidate 侧 style version id> \
        --created-by <admin user_id> \
        [--brand-ids 1,2,3] [--industries 全屋定制,装修] [--dry-run]

`--dry-run` 只做全部前置校验并打印将要写入的字段,**不写库**。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _csv_list(raw: str | None) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="预登记 P2 标题年份实验")
    parser.add_argument("--baseline-version-id", required=True)
    parser.add_argument("--candidate-version-id", required=True)
    parser.add_argument("--created-by", required=True, type=int)
    parser.add_argument("--brand-ids", default="")
    parser.add_argument("--industries", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from writing.title_element_contract import (
        TITLE_ELEMENT_CONTRACT_VERSION,
        title_year_experiment_preregistration,
        validate_title_element_contract,
    )
    from services.article_experiment_registry import ALLOWED_DIMENSIONS

    contract_errors = validate_title_element_contract()
    if contract_errors:
        print(f"❌ 标题要素合同自审未过,拒绝登记:{contract_errors}", file=sys.stderr)
        return 2

    prereg = title_year_experiment_preregistration()
    dimension = str(prereg["single_change_dimension"])
    if dimension not in ALLOWED_DIMENSIONS:
        # 反向对照:这一条会在有人把 title_elements 从 ALLOWED_DIMENSIONS 拿掉时响。
        print(
            f"❌ 单变量维 `{dimension}` 不在 ALLOWED_DIMENSIONS 里,"
            "说明合同与实验登记表已经不同源。",
            file=sys.stderr,
        )
        return 2

    scope: dict[str, list] = {}
    brand_ids = [int(v) for v in _csv_list(args.brand_ids)]
    industries = _csv_list(args.industries)
    if brand_ids:
        scope["brand_ids"] = brand_ids
    if industries:
        scope["industries"] = industries

    payload = {
        "style_family": prereg["style_family"],
        "hypothesis": prereg["hypothesis"],
        "single_change_dimension": dimension,
        "baseline_version_id": args.baseline_version_id,
        "candidate_version_id": args.candidate_version_id,
        "scope": scope,
        "created_by": int(args.created_by),
        "min_arm_articles": int(prereg["min_arm_articles"]),
        "minimum_weeks": int(prereg["minimum_weeks"]),
    }

    print(f"标题要素合同版本 = {TITLE_ELEMENT_CONTRACT_VERSION}")
    print(f"回查检查点(天) = {prereg['review_checkpoints_days']}")
    print(f"臂位定义 = {json.dumps(prereg['arms'], ensure_ascii=False)}")
    print(f"效果申报边界 = {prereg['effect_claim_boundary']}")
    print(f"将要写入 = {json.dumps(payload, ensure_ascii=False, indent=2)}")

    if args.dry_run:
        print("✅ dry-run:全部前置校验通过,未写库。")
        return 0

    from services.article_experiment_registry import create_experiment

    try:
        row = create_experiment(**payload)
    except ValueError as exc:
        # 最常见的一条就是 style_version_not_found —— 见模块 docstring 的诚实边界。
        print(f"❌ 建实验失败:{exc}", file=sys.stderr)
        print(
            "   若报 style_version_not_found / two_distinct_style_versions_required,"
            "说明两臂载体还没就位 —— 这正是本包自曝的口子,不要绕过它硬建。",
            file=sys.stderr,
        )
        return 1
    print(f"✅ 已预登记 experiment_key={row.get('experiment_key')} id={row.get('id')} "
          f"state={row.get('state')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
