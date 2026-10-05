"""标准类证据 lane 的**真实边界**冒烟(不 mock 秘塔)。

夹具/单测把 `_metaso_web_search_direct` 换成了替身 —— 那样边界本身的契约就没人验:
内层键写错(documents vs webpages)会让 lane 看着在跑、实际恒空。这个脚本走真调用,
证明 `document_evidence_search` → `standard_lane_admits` 这条链在真响应上成立。

单次 3 credits。用法:
    python scripts/standard_lane_live_probe_2026_07_29.py "住宅性能评定标准"
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


async def main() -> int:
    from tools.search import provider_router as pr
    from writing.evidence_research import (
        classify_standard_document, standard_lane_admits,
    )

    if not os.getenv("METASO_API_KEY"):
        print("METASO_API_KEY 未配置,跳过")
        return 2
    query = sys.argv[1] if len(sys.argv) > 1 else "建筑装饰装修工程质量验收标准"
    entries = await pr.document_evidence_search(query, size=10)
    print(f"query={query!r} entries={len(entries)}")
    if not entries:
        print("🔴 零条目 —— 内层 item_key 可能又写错了(应为 documents)")
        return 1
    admitted = 0
    for entry in entries:
        ok = standard_lane_admits(entry)
        admitted += int(ok)
        print(
            f"  [{'放行' if ok else '拒 '}] "
            f"authorityDomain={entry['authorityDomain'] or '-'} | "
            f"class={classify_standard_document(entry['title'])} | "
            f"{entry['title'][:44]} | {entry['link'][:70]}"
        )
    print(f"→ 放行 {admitted}/{len(entries)}(条数只作观察,验收看规则 · 工单 §6)")
    # 硬断言:每条放行的必须真是 .gov.cn,且 source 是本 lane。
    from writing.evidence_research import _gov_host

    for entry in entries:
        if standard_lane_admits(entry):
            assert entry["source"] == pr.STANDARD_LANE_SOURCE
            assert any(_gov_host(entry[f]) for f in ("link", "url", "authorityDomain"))
    print("✅ 真实边界契约成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
