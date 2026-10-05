#!/usr/bin/env python3
"""[工单 2026-08-03 ②] 用**新判定逻辑**重扫历史样本,报差异。

用法:python scripts/rescan_registry_correction_2026_08_03.py <samples.json> [brand_id] [canonical_name]

samples.json 形态(由生产只读导出,不含任何凭据):
  [{"q":1,"engine":"deepseek","old_detected":false,"answer":"..."}, ...]

🔴 零 LLM 调用:注入恒返 NO 的复核层,精确复现线上"LLM 判 NO"那一步。
   这样测的是【新闸能不能把它救回待确认】,而不是再花钱问一次模型
   (再问一次也答不了"当时那次为什么判 NO")。
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.brand_identity_resolver import (  # noqa: E402
    BrandIdentity,
    BrandIdentityResolver,
    BrandVerdict,
    VerificationResult,
)


async def _no_verifier(*, identity, evidence_windows):
    return VerificationResult(BrandVerdict.NO, "not_our_brand")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    samples = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    brand_id = int(sys.argv[2]) if len(sys.argv) > 2 else 743
    name = sys.argv[3] if len(sys.argv) > 3 else "深圳市弘匠数科科技有限公司"
    ident = BrandIdentity(brand_id=brand_id, canonical_names=(name,))

    print(f"品牌 {brand_id} · {name} · 样本 {len(samples)} 条\n")
    print(f"{'题':>3}  {'引擎':<10} {'原判定':<8} {'新判定':<9} 原因")
    rescued, regressed = [], []
    for s in samples:
        old = bool(s.get("old_detected"))
        d = asyncio.run(BrandIdentityResolver(ident, verifier=_no_verifier).resolve(s.get("answer") or ""))
        new = d.verdict.value
        mark = ""
        if not old and new == "UNKNOWN":
            mark = "  ← 由【丢分】改为【待确认】"
            rescued.append((s["q"], s["engine"]))
        elif old and new != "YES":
            mark = "  ⚠️ 原判提到→不再是提到"
            regressed.append((s["q"], s["engine"]))
        print(f"{s['q']:>3}  {s['engine']:<10} {'提到' if old else '未提到':<8} {new:<9} {d.reason}{mark}")

    print("\n=== 差异汇总 ===")
    print(f"  「未提到(丢分)」→「待确认」 : {len(rescued)} 条 {rescued}")
    print(f"  「提到」→ 非提到(回归风险)  : {len(regressed)} 条 {regressed}")
    print("\n  计分口径:待确认样本从分母移出进人工队列,不再按'未提到'压低出现率。")
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
