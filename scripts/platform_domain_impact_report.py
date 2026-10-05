"""[补充单 P0-3 2026-08-16] 平台域口径影响面 —— **可重跑、分母写进查询**的唯一出处。

## 为什么要这个脚本

三方测同一件事出了三组数,没有一组能互相印证:

    Review 工单(12 项名单)          69 实体 / 1,367 approved / 4 待审
    交付书 (42 项 · 08-15 快照)     15 或 105 / 1,615 / 54
    Review 独立复算(42 项 · 近似正则) 55 / 3,590 / 788

**问题不在谁算错,在没人把分母写进查询。** 而且还有第二个原因:
🔴 **三次测的不是同一份数据**。生产在 2026-08-16 03:55 跑过一次 rebuild:
   待审候选 1,559 → 2,846(+1,287),全表 8,045 → 9,332。
   08-15 的数和 08-16 的数本来就不该相等。所以本脚本**先打快照指纹**,
   两次运行的指纹不同就不必比数字。

## 「被拦」的定义(写死,不留解释空间)

Review 问的是「是"被判为平台域"还是"判为平台域**且**拿不出名称证据因而通不过"」。
Owner 按后者做决定,所以本脚本的 `blocked_*` **一律是后者**,并把前者以
`on_platform_domain_*` 另行报出,两个数并列打印,永远不会被混用。

    被拦 = match_method='domain_exact'
           AND 实体注册域 ∈ SHARED_PLATFORM_DOMAINS
           AND 该候选拿不出名称证据(_has_name_evidence 为假)
    ⇒ build_binding_candidates 会打「共享平台域名需要名称证据」风险标 ⇒ can_approve=False

## 时间窗

**没有时间窗。** 唯一过滤是 `COALESCE(active, TRUE)`,写在 SQL 里。
输出里带 min/max created_at 只是**描述数据的自然跨度**,不是筛选条件 ——
上一版交付书把它写成「时间窗 06-17→08-05」是措辞误导,这里订正。

用法:
    python scripts/platform_domain_impact_report.py            # 打表
    python scripts/platform_domain_impact_report.py --json out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db.connection import get_connection  # noqa: E402
from services.citation_domain_weights import normalize_domain as registrable_domain  # noqa: E402
from services.media_binding_candidates import (  # noqa: E402
    SHARED_PLATFORM_DOMAINS,
    _has_name_evidence,
)
from services.media_entity_flywheel import normalize_domain as host_domain  # noqa: E402

#: 生产尖 62b99c94 上现役的名单(host 级写法,原样留档)—— 只用来算「相对现役的增量」,
#: **不参与任何判定**。焊死在这里而不是从模块 import,是因为模块里那份已经被本包改掉了,
#: 拿改后的当基线,增量恒为 0(那就是个恒真对照)。
PROD_TIP_LIST = {
    "163.com", "baijiahao.baidu.com", "bilibili.com", "douyin.com", "iesdouyin.com",
    "mp.weixin.qq.com", "qq.com", "sohu.com", "toutiao.com", "weibo.com", "zhihu.com",
}

#: 🔴 分母写在查询里。两条 SQL 都只读。
SQL_FINGERPRINT = """
    SELECT COUNT(*)                                                          AS candidates_all,
           COUNT(*) FILTER (WHERE COALESCE(active,TRUE))                     AS candidates_active,
           COUNT(*) FILTER (WHERE COALESCE(active,TRUE) AND status='approved')  AS approved_active,
           COUNT(*) FILTER (WHERE COALESCE(active,TRUE) AND status='candidate') AS pending_active,
           COUNT(*) FILTER (WHERE COALESCE(active,TRUE) AND status='deleted')   AS deleted_active,
           COUNT(*) FILTER (WHERE COALESCE(active,TRUE)
                              AND match_method='domain_exact')               AS domain_exact_active,
           MIN(created_at)::text AS min_created_at,
           MAX(created_at)::text AS max_created_at,
           MAX(updated_at)::text AS max_updated_at
      FROM geo_media_binding_candidates
"""

SQL_ROWS = """
    SELECT c.id, c.status, c.match_method,
           COALESCE(c.media_name, '')   AS media_name,
           c.entity_key,
           COALESCE(e.domain, '')       AS entity_domain,
           COALESCE(e.canonical_name,'') AS entity_canonical,
           COALESCE(e.aliases::text,'[]') AS entity_aliases
      FROM geo_media_binding_candidates c
      LEFT JOIN geo_media_entities e ON e.entity_key = c.entity_key
     WHERE COALESCE(c.active, TRUE)
"""


def _fetch(sql: str) -> list[dict]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _entity_obj(row: dict) -> dict:
    try:
        aliases = json.loads(row.get("entity_aliases") or "[]")
    except Exception:
        aliases = []
    return {
        "canonical_name": row.get("entity_canonical") or "",
        "domain": row.get("entity_domain") or "",
        "aliases": aliases if isinstance(aliases, list) else [],
    }


def is_blocked_row(row: dict, is_platform_domain) -> bool:
    """🔴 「被拦」的唯一定义,抽成纯函数是为了能被**行为**锁住(而不是 grep 源码里有没有
    `_has_name_evidence` —— 那种锁把整段判定删掉、只留 import 也照样绿,实测存活过)。

        被拦 = 实体域判为平台  AND  match_method='domain_exact'  AND  拿不出名称证据
    三个条件缺一不算。Owner 按这个数做决定。
    """
    if not is_platform_domain(row.get("entity_domain") or ""):
        return False
    if row.get("match_method") != "domain_exact":
        return False
    return not _has_name_evidence(_entity_obj(row), {"media_name": row.get("media_name") or ""})


def compute() -> dict:
    fp = _fetch(SQL_FINGERPRINT)[0]
    rows = _fetch(SQL_ROWS)

    def blocked_under(is_platform) -> list[dict]:
        return [r for r in rows if is_blocked_row(r, is_platform)]

    def new_pred(dom: str) -> bool:
        reg = registrable_domain(host_domain(dom))
        return bool(reg) and reg in SHARED_PLATFORM_DOMAINS

    def baseline_pred(dom: str) -> bool:
        """生产尖 `62b99c94` 现役口径:host 级 + 精确相等 + 原 11 项名单。
        并列算它,是为了让「绝对量」和「相对现役的增量」两个问法在同一份数据上都有答案 ——
        上一版交付书报的 54 是**增量**,Review 复算的 788 是**绝对量**,两边其实在答不同的问题。"""
        return host_domain(dom) in PROD_TIP_LIST

    on_platform = [r for r in rows if new_pred(r["entity_domain"])]
    blocked = blocked_under(new_pred)
    baseline_blocked = blocked_under(baseline_pred)
    baseline_ids = {r["id"] for r in baseline_blocked}
    delta = [r for r in blocked if r["id"] not in baseline_ids]

    def by_status(items: list[dict]) -> dict[str, int]:
        return dict(Counter(i["status"] for i in items))

    # 🔴 快照指纹**只用计数**,不掺时间戳:时间戳在不同环境(生产 / 本地快照库)必然不同,
    #    掺进去会让「同一份数据」被判成不同快照,反而制造假差异。
    count_keys = ["candidates_all", "candidates_active", "approved_active",
                  "pending_active", "deleted_active", "domain_exact_active"]
    fingerprint = hashlib.sha256(
        "|".join(f"{k}={fp[k]}" for k in count_keys).encode("utf-8")
    ).hexdigest()[:16]

    by_domain: dict[str, Counter] = defaultdict(Counter)
    for r in blocked:
        by_domain[registrable_domain(host_domain(r["entity_domain"]))][r["status"]] += 1

    return {
        "snapshot": {**fp, "fingerprint": fingerprint, "fingerprint_inputs": count_keys},
        "list_size": len(SHARED_PLATFORM_DOMAINS),
        "on_platform_domain_total": len(on_platform),
        "on_platform_domain_entities": len({r["entity_key"] for r in on_platform}),
        "on_platform_domain_by_status": by_status(on_platform),
        "blocked_total": len(blocked),
        "blocked_entities": len({r["entity_key"] for r in blocked}),
        "blocked_by_status": by_status(blocked),
        "baseline_blocked_total": len(baseline_blocked),
        "baseline_blocked_by_status": by_status(baseline_blocked),
        "baseline_blocked_entities": len({r["entity_key"] for r in baseline_blocked}),
        "delta_blocked_total": len(delta),
        "delta_blocked_by_status": by_status(delta),
        "delta_blocked_entities": len({r["entity_key"] for r in delta}
                                      - {r["entity_key"] for r in baseline_blocked}),
        "blocked_by_domain": {d: dict(c) for d, c in sorted(
            by_domain.items(), key=lambda kv: -sum(kv[1].values()))},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    r = compute()
    s = r["snapshot"]

    print("=" * 92)
    print("平台域口径影响面 · 分母全部写在 SQL 里 · 无时间窗(唯一过滤 = active)")
    print("=" * 92)
    print(f"  🔴 快照指纹 = {s['fingerprint']}   ← 两次运行指纹不同则数字本来就不该相等")
    print(f"     候选全表 {s['candidates_all']} · active {s['candidates_active']}"
          f"(approved {s['approved_active']} / 待审 {s['pending_active']} / deleted {s['deleted_active']})")
    print(f"     其中 domain_exact = {s['domain_exact_active']}")
    print(f"     created_at 自然跨度 {s['min_created_at']} → {s['max_created_at']}"
          f"(**描述,不是筛选条件**)")
    print(f"     max(updated_at) = {s['max_updated_at']}")
    print(f"     名单规模 = {r['list_size']} 项注册域")
    print()
    print(f"  ① 落在平台域上的 active 候选 = {r['on_platform_domain_total']} / {s['candidates_active']}"
          f" ({r['on_platform_domain_total']/max(1,s['candidates_active']):.2%})"
          f" · 实体 {r['on_platform_domain_entities']}")
    print(f"       按状态 {r['on_platform_domain_by_status']}")
    print(f"     ⚠️ 这个数**不是**「被拦」,它只说明域名落在名单里。")
    print()
    print(f"  ② 🔴 被拦(判为平台域 **且** 无名称证据 ⇒ 打风险标 ⇒ can_approve=False)")
    print(f"       = {r['blocked_total']} / {s['candidates_active']}"
          f" ({r['blocked_total']/max(1,s['candidates_active']):.2%}) · 实体 {r['blocked_entities']}")
    print(f"       按状态 {r['blocked_by_status']}")
    print(f"     🔴 Owner 按这个数做决定。其中 status='candidate' 的"
          f" {r['blocked_by_status'].get('candidate', 0)} 条是**唯一会影响运营**的量;")
    print(f"        status='approved' 的 {r['blocked_by_status'].get('approved', 0)} 条是存量,"
          f"本包不回溯,今天线上行为不变。")
    print()
    print(f"  ③ 相对**生产尖现役口径**(host 级 + 精确相等 + 原 11 项)的增量")
    print(f"       现役口径被拦 = {r['baseline_blocked_total']} · 按状态 {r['baseline_blocked_by_status']}")
    print(f"       本包新增被拦 = {r['delta_blocked_total']} · 按状态 {r['delta_blocked_by_status']}"
          f" · 新增实体 {r['delta_blocked_entities']}")
    print(f"     ⚠️ 上一版交付书报的「54」是**这一行的增量**(且在 08-15 快照上);")
    print(f"        Review 复算的「788」是**②那一行的绝对量**。两个数在答不同的问题,不是谁算错。")
    print()
    print("  ④ 被拦按注册域(前 15):")
    for d, c in list(r["blocked_by_domain"].items())[:15]:
        print(f"       {d:<22s} {sum(c.values()):>5d}  {dict(c)}")

    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=2)
        print(f"\n→ 已写 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
