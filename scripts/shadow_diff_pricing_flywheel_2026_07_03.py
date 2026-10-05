#!/usr/bin/env python3
"""[B4-2] 离线跑飞轮行业引用格局 → 定价影响 shadow 报告(放行门)。

只读 · 不写价格缓存 · 不翻 flag。
用法:
  python scripts/shadow_diff_pricing_flywheel_2026_07_03.py            # 全量有数据行业
  python scripts/shadow_diff_pricing_flywheel_2026_07_03.py 教育培训    # 指定行业

流程:报告交老板 → 批准 → Deploy 开 flag(可按行业白名单灰度)。builder 无权开 flag。
"""
import json
import sys


def main():
    industry = sys.argv[1] if len(sys.argv) > 1 else None
    from services.pricing_shadow_report import build_flywheel_landscape_shadow_report
    report = build_flywheel_landscape_shadow_report(industry=industry)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
