"""[WO-KYB-ROUTING D2] 等价映射的审计与抽检 —— 纯判断逻辑,不连库。

`media_provider_equivalence.rebuild_table(dry_run=True)` 已经能产出候选与转人工清单。
本模块补的是**判断口径**,而且刻意做成纯函数:连库那部分薄薄一层放在
`scripts/d2_equivalence_dryrun.py`,判断这部分可以不连库全量上锁。

三件事:
  1. **独有清单口径** —— 独有 = 在等价表里没有任何一条 approved 映射。
     🔴 **不许用"名字没撞上"粗判**:名字相同未必同一家(「界面」141 条名字命中里
     只有 9 条真发 jiemian.com),名字不同也未必不是同一家(表里有 matched_domain
     就是为这个)。工单 §6-D2 把这条写成硬要求。
  2. **抽检取样** —— 按 confidence 分层,且**确定性取样**(等距,不用随机):
     审计要可复现,随机取样下次跑出来是另一批,没法复核。
  3. **不变量校验** —— 只把"严格更便宜"的配成 kyb;两份清单不许有交集;
     两份之和必须等于 kyb 全量。
"""
from __future__ import annotations

from typing import Any, Iterable

# 抽检比例(工单 §6-D2 建议值,执行方可提更严的)
SAMPLE_RATIO = {
    "human_approved": 0.05,   # 高 confidence:抽 5%
    "auto_fans_ok": 0.20,     # 中:抽 20%
    "auto_low_diff": 1.00,    # 低:逐条审(或整档弃用)
}
_DEFAULT_RATIO = 1.00


def partition_kyb_catalog(all_kyb_ids: Iterable[int],
                          approved_rows: Iterable[dict[str, Any]]) -> dict[str, list[int]]:
    """把快易播全量目录切成【有对家】与【独有】两份。

    `approved_rows` 是等价表里 `is_enabled=TRUE`(已人工审过)的行。
    只认 approved —— 候选但没审过的**不算有对家**,否则 D4 会把一批
    "以为有对家所以不放出来"的媒体永久埋掉。
    """
    all_ids = sorted({int(i) for i in all_kyb_ids})
    paired_ids = {int(r["kyb_media_id"]) for r in approved_rows or ()
                  if r.get("kyb_media_id") is not None}
    paired = [i for i in all_ids if i in paired_ids]
    exclusive = [i for i in all_ids if i not in paired_ids]
    return {"paired": paired, "exclusive": exclusive}


def check_partition_invariants(all_kyb_ids: Iterable[int],
                               partition: dict[str, list[int]]) -> list[str]:
    """两份清单的硬不变量。返回问题列表,空 = 全过。

    工单 §6-D2 反向对照表里的两条:两份之和 = kyb 全量;两份不许有交集。
    """
    problems: list[str] = []
    all_ids = {int(i) for i in all_kyb_ids}
    paired = {int(i) for i in partition.get("paired", ())}
    exclusive = {int(i) for i in partition.get("exclusive", ())}

    overlap = paired & exclusive
    if overlap:
        problems.append(f"两份清单有交集 {len(overlap)} 条:{sorted(overlap)[:10]}")
    missing = all_ids - (paired | exclusive)
    if missing:
        problems.append(f"有 {len(missing)} 条 kyb 媒体两份清单都没收:{sorted(missing)[:10]}")
    extra = (paired | exclusive) - all_ids
    if extra:
        problems.append(f"清单里有 {len(extra)} 条不在 kyb 全量里:{sorted(extra)[:10]}")
    return problems


def check_preferred_provider_rows(rows: Iterable[dict[str, Any]]) -> list[str]:
    """🔴 只有【严格更便宜】才准配成 kyb。持平或更贵一律不配。

    §1.2 实测:自媒体侧 19.9% 的对家快易播**反而更贵**,p10 −75%。
    这不是理论风险 —— 配错方向就是把毛利当场吃掉。
    """
    problems: list[str] = []
    for r in rows or ():
        try:
            mhz = float(r["mhz_price"])
            kyb = float(r["kyb_price"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"行缺价格字段或不可解析:{r!r}")
            continue
        pref = str(r.get("preferred_provider") or "")
        if pref == "kyb" and not (kyb < mhz):
            problems.append(
                f"mhz_media_id={r.get('mhz_media_id')} 配成 kyb 但不更便宜"
                f"(mhz={mhz} kyb={kyb})")
    return problems


def sample_for_review(rows: Iterable[dict[str, Any]], *,
                      ratios: dict[str, float] | None = None) -> dict[str, list[dict]]:
    """按 confidence 分层做**确定性**抽样(等距),返回 {confidence: 抽中的行}。

    🔴 不用随机:审计要可复现。随机取样下次跑是另一批,复核时对不上。
    等距取样按 `mhz_media_id` 排序后每隔 step 取一条,输入不变结果就不变。
    """
    ratios = {**SAMPLE_RATIO, **(ratios or {})}
    buckets: dict[str, list[dict]] = {}
    for r in rows or ():
        buckets.setdefault(str(r.get("confidence") or "unknown"), []).append(r)

    out: dict[str, list[dict]] = {}
    for conf, items in buckets.items():
        items = sorted(items, key=lambda r: int(r.get("mhz_media_id") or 0))
        ratio = ratios.get(conf, _DEFAULT_RATIO)
        if ratio >= 1.0:
            out[conf] = list(items)
            continue
        n = max(1, round(len(items) * ratio)) if items else 0
        step = max(1, len(items) // n) if n else 1
        out[conf] = [items[i] for i in range(0, len(items), step)][:n]
    return out


def summarize(rows: Iterable[dict[str, Any]], human_rows: Iterable[dict[str, Any]],
              partition: dict[str, list[int]]) -> dict[str, Any]:
    """交付说明要如实报的那组数。

    🔴 工单 §6-D2:**不许只报"配了 N 组"而不报弃用数。**
    """
    rows = list(rows or ())
    human = list(human_rows or ())
    by_conf: dict[str, int] = {}
    for r in rows:
        by_conf[str(r.get("confidence") or "unknown")] = \
            by_conf.get(str(r.get("confidence") or "unknown"), 0) + 1
    return {
        "candidates_auto": len(rows),
        "by_confidence": dict(sorted(by_conf.items())),
        "to_human_review": len(human),
        "kyb_paired": len(partition.get("paired", ())),
        "kyb_exclusive": len(partition.get("exclusive", ())),
    }
