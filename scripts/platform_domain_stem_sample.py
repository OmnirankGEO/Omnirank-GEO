"""[补充单 P0-4 2026-08-16] 74% 抽样验证 —— Owner 定「不调只验」。

抽 N 个**被判为平台**的注册域,逐个打印词干测试结果 + 媒体名样例,供人肉复核。
🔴 抽样必须可复跑:固定随机种子,并把抽中的域列出来;不许「我看了一下觉得差不多」。

判读方式(交付单里逐条给结论):
  · 抽中的域里「确实无共同词干(= 真平台)」的占比 ≈ 74% 甚至更高 ⇒ 74% 是生态实况,收下;
  · 明显低于 ⇒ 判据把 B 桶型的域也抓了 ⇒ 修词干判据,**仍然不缩名单**(Owner 定)。

⚠️ 一个必须说清的口径差:74% 是**候选条数**占比,本抽样是**域**占比。
   两者不是一回事,所以下面同时给「按域」和「按候选加权」两种精确度。

用法:
    python scripts/platform_domain_stem_sample.py                  # 默认 seed=20260816 n=30
    python scripts/platform_domain_stem_sample.py --seed 42 --n 20
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db.connection import get_connection  # noqa: E402
from services.citation_domain_weights import normalize_domain as registrable_domain  # noqa: E402
from services.media_entity_flywheel import normalize_domain as host_domain  # noqa: E402
from services.media_name_stem import MIN_NAMES_FOR_JUDGEMENT, classify_domain  # noqa: E402

SQL = """
    SELECT COALESCE(c.media_name,'') AS media_name,
           COALESCE(e.domain,'')     AS entity_domain
      FROM geo_media_binding_candidates c
      JOIN geo_media_entities e ON e.entity_key = c.entity_key
     WHERE COALESCE(c.active, TRUE)
"""


def load():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(SQL)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    names: dict[str, set[str]] = defaultdict(set)
    volume: dict[str, int] = defaultdict(int)
    for r in rows:
        d = registrable_domain(host_domain(r["entity_domain"]))
        if not d:
            continue
        volume[d] += 1
        if r["media_name"]:
            names[d].add(r["media_name"])
    return names, volume


def pick_sample(platform_domains, seed: int, n: int) -> list[str]:
    """🔴 抽样必须可复跑:同 seed 同总体 ⇒ 同结果。抽成纯函数是为了让这条能被**行为**锁住,
    而不是靠 grep 源码里有没有 `random.Random(seed)`(那种锁改成 `random.Random()` 照样绿)。"""
    pool = sorted(platform_domains)
    rng = random.Random(seed)
    return sorted(rng.sample(pool, min(n, len(pool))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20260816)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--samples", type=int, default=8)
    args = ap.parse_args()

    names, volume = load()
    judged = {d: classify_domain(d, ns) for d, ns in names.items()
              if len(ns) >= MIN_NAMES_FOR_JUDGEMENT}
    platform = sorted(d for d, v in judged.items() if v.is_platform)
    print(f"总注册域 {len(names)} · 样本足够可判定 {len(judged)} · 其中被判平台 {len(platform)}")
    if not platform:
        print("🔴 被判平台的域为 0 —— 抽样无意义,拒绝出数")
        return 2

    picked = pick_sample(platform, args.seed, args.n)
    n = len(picked)
    print(f"🔴 seed={args.seed} · 抽 {n} 个(总体 {len(platform)},抽样率 {n/len(platform):.0%})")
    print(f"   抽中: {', '.join(picked)}")
    print()
    for i, d in enumerate(picked, 1):
        v = judged[d]
        print(f"[{i:>2d}] {d}   候选 {volume[d]} 条 · 媒体名 {v.name_count} 个")
        print(f"     主导主体「{v.dominant_stem}」占 {v.dominant_count} · 独立异名主体 {v.distinct_outsider_stems}")
        print(f"     名字样例: {' | '.join(v.name_samples[: args.samples])}")
        if v.outsider_samples:
            print(f"     异名样例: {' | '.join(v.outsider_samples[: args.samples])}")
        print()
    print("—— 以上逐条交人肉判读,结论写进交付单(本脚本不替人下结论)——")
    print(f"抽中域的候选合计 = {sum(volume[d] for d in picked)}"
          f" / 被判平台域候选合计 {sum(volume[d] for d in platform)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
