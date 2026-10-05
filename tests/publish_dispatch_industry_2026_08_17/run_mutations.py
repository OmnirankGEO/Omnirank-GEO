"""变异 runner —— 证明本包的锁有判别力
(WO_PUBLISH_DISPATCH_STATUS_AND_INDUSTRY_2026-08-17 · Part① 判据2「拆锁」+ Part② 判据3)。

做法:逐个把修复**拆掉**,跑对应的锁,要求**转红**;然后复位,要求**转绿**。
存活(拆掉了还全绿)= 那条锁是摆设,退回返工。

🔴 Windows 行尾:本文件是「读文件→改字符串→写回」的典型形态,
   `newline=""` **读写两处都加** —— 少一处会把整份文件的 LF 全翻成 CRLF。
   跑完自带 `git status --porcelain` 自检,不留残迹。

用法:
  TEST_DATABASE_URL=... python -X utf8 tests/publish_dispatch_industry_2026_08_17/run_mutations.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/publish_dispatch_industry_2026_08_17"
T_STATS = f"{SUITE}/test_publish_stats_third_source.py"
T_TAX = f"{SUITE}/test_media_industry_taxonomy.py"

# 第三源整段(M1 用):把它从 UNION 里摘掉 = 回到工单描述的病态口径。
_THIRD_SOURCE = """                UNION ALL
                -- 第三源:本地 item —— 只收**镜像里没有**的那些(不写镜像的渠道全靠它)
                SELECT
                    po2.article_id,
                    {_item_case} AS status,
                    i2.media_name
                FROM mhz_publish_order_items i2
                JOIN mhz_publish_orders po2 ON po2.id = i2.order_id
                WHERE i2.user_id = %s
                  AND po2.article_id IS NOT NULL
                  AND i2.status = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1 FROM mhz_synced_orders s2
                      WHERE COALESCE(i2.mhz_order_id, '') <> ''
                        AND i2.mhz_order_id IN (s2.order_sn, s2.id)
                        AND COALESCE(s2.user_id, i2.user_id) = %s
                  )
"""

# 摘掉第三源后参数也要跟着少两个,否则报的是「参数个数不匹配」而不是「统计缺了一源」。
# 变异要打在**语义**上,不能靠一个 SQL 语法错骗出红。
_PARAMS_FULL = '""", (uid, uid, item_countable_statuses(), uid, uid))'
_PARAMS_NO_THIRD = '""", (uid, uid))'

_ANTIJOIN = """                  AND NOT EXISTS (
                      SELECT 1 FROM mhz_synced_orders s2
                      WHERE COALESCE(i2.mhz_order_id, '') <> ''
                        AND i2.mhz_order_id IN (s2.order_sn, s2.id)
                        AND COALESCE(s2.user_id, i2.user_id) = %s
                  )
"""
# 去重只留一条腿(只按 s2.id 连)—— 这是最容易犯的那个错:
# 生产里 item.mhz_order_id 既可能等于 s.id 也可能等于 s.order_sn。
_ANTIJOIN_ONE_LEG = """                  AND NOT EXISTS (
                      SELECT 1 FROM mhz_synced_orders s2
                      WHERE COALESCE(i2.mhz_order_id, '') <> ''
                        AND i2.mhz_order_id = s2.id
                        AND COALESCE(s2.user_id, i2.user_id) = %s
                  )
"""

# (编号, 说明, 相对路径, 原文, 变异文, 该转红的测试选择器)
MUTATIONS = [
    (
        "M1", "把第三源从 UNION 拿掉(= 工单描述的病态口径)",
        "api/meijiehezi_api.py", _THIRD_SOURCE, "",
        [f"{T_STATS}::test_C1a_kyb_item_published_without_mirror_counts_as_published",
         f"{T_STATS}::test_C5_in_flight_item_shows_in_progress"],
    ),
    (
        "M1b", "第三源摘掉后的参数同步收缩(证明 M1 的红不是参数个数报错骗来的)",
        "api/meijiehezi_api.py", _PARAMS_FULL, _PARAMS_NO_THIRD,
        [f"{T_STATS}::test_C1a_kyb_item_published_without_mirror_counts_as_published"],
    ),
    (
        "M2", "去重反连整段拆掉(镜像 + item 双计)",
        "api/meijiehezi_api.py", _ANTIJOIN, "",
        [f"{T_STATS}::test_C1b_mirror_and_item_dedupe_counts_once"],
    ),
    (
        "M3", "去重只连 s2.id 一条腿(order_sn 那条形态漏出双计)",
        "api/meijiehezi_api.py", _ANTIJOIN, _ANTIJOIN_ONE_LEG,
        [f"{T_STATS}::test_C1b_mirror_and_item_dedupe_counts_once"],
    ),
    (
        "M4", "第三源不限用户(别人的 item 进我的统计)",
        "api/meijiehezi_api.py",
        "                WHERE i2.user_id = %s\n                  AND po2.article_id IS NOT NULL",
        "                WHERE %s IS NOT NULL\n                  AND po2.article_id IS NOT NULL",
        [f"{T_STATS}::test_C4c_other_users_items_stay_out"],
    ),
    (
        "M5", "cancelled 改成计入(工单明确不计)",
        "db/meijiehezi_db.py",
        "    'cancelled': None,           # 工单明确:不计",
        "    'cancelled': 1,              # 工单明确:不计",
        [f"{T_STATS}::test_C4b_cancelled_item_is_not_counted"],
    ),
    (
        "M6", "状态映射漏登记一个值(会被静默从统计里丢掉)",
        "db/meijiehezi_db.py",
        "    'awaiting_action': 1,        # 等人工动作,仍在管道里\n",
        "",
        [f"{T_STATS}::test_C6_status_map_covers_the_closed_set"],
    ),
    (
        "M7", "CASE 手抄一份(与映射表脱钩)",
        "db/meijiehezi_db.py",
        '    parts = " ".join(\n'
        '        f"WHEN \'{k}\' THEN {v}"\n'
        "        for k, v in ITEM_STATUS_TO_SYNCED_CODE.items() if v is not None\n"
        "    )",
        "    parts = \"WHEN 'published' THEN 2\"",
        [f"{T_STATS}::test_C6_status_map_covers_the_closed_set"],
    ),
    # ── Part② ────────────────────────────────────────────────────────────────
    (
        "M8", "映射表少一个成分(完备性锁必须抓到 + 语义缩水)",
        "services/media_industry_taxonomy.py",
        '    "房产家居": ("家居",),\n',
        "",
        [f"{T_TAX}::test_D1_every_production_component_is_mapped",
         f"{T_TAX}::test_D2_no_semantic_shrink_versus_old_literal_reading",
         f"{T_TAX}::test_D2b_家居_case_from_the_work_order"],
    ),
    (
        # 🔴 第一版这条**存活**:选择器只挂了两条打生产全量的锁,而生产全量已经
        #    100% 映射 → 兜底分支根本走不到,变异是惰性的。教训 = 「驳回之前先证明
        #    探针打得进正样本」。改挂 D5b(专门用目录里不存在的成分去踩那条分支)。
        "M9", "`|| 原串` 兜底复活(未映射成分以原文形态漏回 chip)",
        "services/media_industry_taxonomy.py",
        "    if not keys:\n        return (OTHER_L1,)",
        "    if not keys:\n        return tuple(comps)",
        [f"{T_TAX}::test_D5b_unmapped_component_falls_to_other_never_leaks_raw_text"],
    ),
    (
        "M10", "0 家的大类照样下发(空行业隐藏失效)",
        "services/media_industry_taxonomy.py",
        '    return [{"key": k, "count": totals[k]} for k in L1_ORDER if totals.get(k)]',
        '    return [{"key": k, "count": totals.get(k, 0)} for k in L1_ORDER]',
        [f"{T_TAX}::test_D3_chip_count_within_budget_and_all_nonzero",
         f"{T_TAX}::test_D4_zero_bucket_hidden_nonzero_bucket_rendered"],
    ),
    (
        "M11", "不拆斜杠(整串当一个成分 —— 就是 Owner 报的那个病)",
        "services/media_industry_taxonomy.py",
        "    for sep in _SEPARATORS[1:]:\n        text = text.replace(sep, _SEPARATORS[0])",
        "    return [text.strip()] if text.strip() else []",
        [f"{T_TAX}::test_D1_every_production_component_is_mapped",
         f"{T_TAX}::test_D5_chip_keys_are_l1_only_no_raw_slash_labels"],
    ),
    (
        "M12", "facet 计数拍脑袋(不按原始串求和)",
        "services/media_industry_taxonomy.py",
        "            totals[key] = totals.get(key, 0) + count",
        "            totals[key] = totals.get(key, 0) + 1",
        [f"{T_TAX}::test_D3b_counts_are_conserved_not_invented"],
    ),
    (
        "M13", "列表与 facet 的谓词各写一份(数量与点进去看到的对不上)",
        "db/meijiehezi_db.py",
        "        _facet_where, _facet_params = _wemedia_where(\n"
        "            c, search=search, platform=platform, province=province,",
        "        _facet_where, _facet_params = _wemedia_where(\n"
        "            c, search=search, province=province,",
        [f"{SUITE}/test_industry_filter_db.py::test_E2_facets_follow_the_current_platform_filter"],
    ),
    (
        "M14", "L1 键不展开,退回逐字精确匹配(选大类必空)",
        "db/meijiehezi_db.py",
        "    if not is_l1_key(industry):\n        return \"industry = %s\", [industry]",
        "    if True:\n        return \"industry = %s\", [industry]",
        [f"{SUITE}/test_industry_filter_db.py::test_E1_selecting_l1_returns_all_member_media"],
    ),
    # ── 前端真渲染(判据 1/4 落在 DOM 上) ────────────────────────────────────
    (
        "M15", "chip 不显示数量(用户看不出哪个大类是空的)",
        "frontend/src/pages/Publishing/PublishCenter.tsx",
        ">{i.key} {i.count}</span>",
        ">{i.key}</span>",
        ["PW:chips"],
    ),
    (
        "M16", "filters 请求不带 platform(facet 停在全目录口径上说谎)",
        "frontend/src/pages/Publishing/PublishCenter.tsx",
        "    if (wmPlatform) q.set('platform', wmPlatform);\n"
        "    if (wmProvince) q.set('province', wmProvince);",
        "    if (wmProvince) q.set('province', wmProvince);",
        ["PW:重算", "PW:platform"],
    ),
]

#: 前端锁走 Playwright(判据打真渲染)。`PW:<grep>` 形态的选择器由这里分发。
PW_CONFIG = "playwright.publish-industry.config.ts"


def _read(rel: str) -> str:
    # 🔴 newline="" 读:不让 Python 把 CRLF 归一成 LF(否则写回时会整份翻)
    with open(ROOT / rel, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(rel: str, text: str) -> None:
    # 🔴 newline="" 写:不让 Python 把 LF 翻成 CRLF
    with open(ROOT / rel, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _pytest(selectors: list[str]) -> int:
    py = [s for s in selectors if not s.startswith("PW:")]
    pw = [s[3:] for s in selectors if s.startswith("PW:")]
    rc = 0
    if py:
        rc |= subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "pytest", "-q", "--no-header", *py],
            cwd=ROOT, capture_output=True, text=True,
        ).returncode
    for grep in pw:
        # 🔴 前端锁必须真跑浏览器:源码正则扫描换个写法就绕过,证明不了用户看到什么。
        rc |= subprocess.run(
            ["npx", "playwright", "test", "-c", PW_CONFIG, "-g", grep],
            cwd=ROOT / "frontend", capture_output=True, text=True, shell=True,
        ).returncode
    return rc


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("🔴 需要 TEST_DATABASE_URL —— 没有库的变异是假绿(锁会 skip,skip 不是红)")
        return 2

    # 基线:动手之前必须全绿,否则后面分不清"变异杀死"和"本来就红"
    base_all = _pytest([SUITE, "PW:."])   # `-g .` = 前端三条全跑
    if base_all != 0:
        print(f"🔴 基线不绿(rc={base_all}),先修基线再跑变异")
        return 2
    print("✅ 基线全绿")

    survived, killed = [], []
    for mid, desc, rel, old, new, selectors in MUTATIONS:
        original = _read(rel)
        if old not in original:
            print(f"🔴 {mid} 锚点不存在于 {rel} —— 变异没生效等于没测(判据失效)")
            return 2
        try:
            _write(rel, original.replace(old, new, 1))
            rc_mut = _pytest(selectors)
        finally:
            _write(rel, original)
        rc_restore = _pytest(selectors)

        if rc_mut == 0:
            survived.append((mid, desc))
            print(f"🔴 {mid} 存活(拆掉了还全绿):{desc}")
        elif rc_restore != 0:
            survived.append((mid, f"{desc} · 复位后没转回绿(锁不稳定)"))
            print(f"🔴 {mid} 复位后仍红:{desc}")
        else:
            killed.append(mid)
            print(f"✅ {mid} 被杀 · 复位转绿:{desc}")

    porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
    print("\n================ 变异结果 ================")
    print(f"杀死 {len(killed)}/{len(MUTATIONS)} · 存活 {len(survived)}")
    for mid, desc in survived:
        print(f"  存活 {mid}:{desc}")
    print(f"git status --porcelain 行数 = {len(porcelain.splitlines()) if porcelain else 0}")
    if porcelain:
        print(porcelain)
        print("🔴 变异 runner 留下了残迹(多半是行尾被翻)")
        return 1
    return 0 if not survived else 1


if __name__ == "__main__":
    sys.exit(main())
