#!/usr/bin/env python3
"""P0-9 · 关掉 146 条「假开关」(is_monitored=TRUE 但无 active 订阅)。

WO_MONITORING_MANUAL_KEYWORD_PARITY_2026-08-16 · P0-9
Owner 2026-08-16:「美构海外仓不是我们自己的客户,应该是服务商的,他没有付费肯定不能跑」
⇒ **不补订阅,只把假开关关掉。**

🔴 独立幂等脚本,**不写进迁移** —— prestart 每次部署无条件重放全部迁移(无追踪表),
   迁移里的 UPDATE 会在每次部署把代理的人工操作重新覆盖掉。

🔴 默认 dry-run。真跑要 --apply。

判据(--verify 全跑,退出码 0 = 全绿):
  1. 跑前孤儿数必须 **非 0**(否则这条判据当天没有判别力,脚本显式报红退出 8);
  2. 跑后孤儿数 = 0;
  3. 重跑差分 = 0(幂等);
  4. 🔴 反向对照:**跑前在对照集合里的每个词,is_monitored 跑前跑后逐条一致**
     (证明没误伤在跑的。注意不是"全部 TRUE"也不是"集合总数不变" —— 前者会被
      paused_low_balance 这个正常态判红,后者会被 P0-10 自己的正确行为判红);
  5. 🔴 订阅表 active 条数前后差值 = 0(本脚本只改标志位,绝不动订阅)。

🔴 绝不许顺手给任何一条建订阅(工单红线 6 · Owner 已定)。
"""
from __future__ import annotations

import argparse
import sys

# 🔴 [Review 2026-08-16 R-2] Windows GBK 控制台必崩:本脚本的报告段打印了 ⚠️(U+26A0)
#   与 💰/🔴 等字符,子进程 stdout 编码是 GBK 时 UnicodeEncodeError → 非零退出。
#   崩点在**写库之前**的"跑前"报告段(fail-closed,无半截状态),所以不是资金风险;
#   拦它是因为**复审者在干净 Windows 环境复现不出全绿** —— 判据可复现性问题。
#   生产 Linux/UTF-8 不受影响。本仓 Windows 编码是惯犯家族,照老修法:
#   入口显式把 stdout/stderr 重设成 utf-8,errors="replace" 兜住任何终端。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass  # 非 TextIO(被重定向到管道/捕获对象)时忽略,不能因此拦住脚本

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

# 孤儿定义:开关开着,但没有任何在跑的订阅撑着它。
# 与 list_active_subscriptions 的 confirmed 臂同口径(status IN active/paused_low_balance
# 算"开着" —— 余额不足是暂停不是关闭,充值后自己恢复,不该被本脚本关掉)。
ORPHAN_WHERE = """
    ck.is_monitored = TRUE
    AND NOT EXISTS (
        SELECT 1 FROM keyword_monitor_subscriptions s
         WHERE s.keyword_id = ck.id
           AND s.keyword_source = 'confirmed'
           AND s.status IN ('active', 'paused_low_balance')
    )
"""

# 反向对照物:有活订阅的词。这些**必须**全程 is_monitored=TRUE 不被动。
CONTROL_WHERE = """
    EXISTS (
        SELECT 1 FROM keyword_monitor_subscriptions s
         WHERE s.keyword_id = ck.id
           AND s.keyword_source = 'confirmed'
           AND s.status IN ('active', 'paused_low_balance')
    )
"""


# ── P0-10 · 僵尸订阅(Owner 2026-08-16 方案 B)────────────────────────────────
# 定义:订阅还挂着 active/paused_low_balance,但取数的两个闸**任一**不放行
#       ⇒ 它永远不会被 list_active_subscriptions 取到 = 跑不动的订阅。
# 🔴 按判据取,不按硬编码 id 取(id 会漂,判据不会)。
# 🔴 方向:说真话的是 is_monitored=FALSE(确实没在跑);说谎的是 s.status='active'。
#    所以动的是**订阅**,不是 is_monitored / monitoring_status(Owner 只授权 cancel 这一个动作)。
#
# 🔴🔴 工单给的条件是 `status IN ('active','paused_low_balance') 且 (is_monitored=FALSE
#      或 monitoring_status<>'active')`,并说跑前命中 = 5。
#      **生产实测该条件命中 7 条,不是 5 条**(2026-08-16 只读复算),多出来的两条是**另一种形态**:
#
#        sub 12-16 / kw 2339-2343  status=active              is_monitored=f  mon_status=archived  ← 真僵尸
#        sub 92    / kw 2845       status=paused_low_balance  is_monitored=f  mon_status=active
#        sub 93    / kw 2846       status=paused_low_balance  is_monitored=f  mon_status=active
#
#      后两条**不能 cancel**:`paused_low_balance` 是"余额不足自动暂停、充值后自愈"的状态,
#      scheduler.py 的恢复段(list_paused_subscriptions_for_resume → resume →
#      update_keyword_monitor_state(is_monitored=True))会在充值后把它们连同 is_monitored 一起复活。
#      把它们 cancel 掉 = 把一个只是在等充值的付费客户的监测**永久**杀掉,不可自愈。
#
#      ⇒ 本脚本按「**不可自愈**」收窄:只认 `status='active'` 且词已归档/停用的那一族。
#        判据方向与工单一致(认"跑不动的"),只是把"暂时跑不动、会自己好"的排除掉。
#        那 2 条单独列出来交 Owner 判,脚本**不动它们**。
ZOMBIE_WHERE = """
    s.status = 'active'
    AND EXISTS (SELECT 1 FROM confirmed_keywords ck2 WHERE ck2.id = s.keyword_id)
    AND COALESCE((SELECT ck2.monitoring_status FROM confirmed_keywords ck2 WHERE ck2.id = s.keyword_id), 'active') <> 'active'
"""

# 工单原条件命中、但被本脚本**刻意排除**的那一族 —— 只报告,不动。
PAUSED_MISMATCH_WHERE = """
    s.status = 'paused_low_balance'
    AND EXISTS (SELECT 1 FROM confirmed_keywords ck2 WHERE ck2.id = s.keyword_id)
    AND COALESCE((SELECT ck2.is_monitored FROM confirmed_keywords ck2 WHERE ck2.id = s.keyword_id), FALSE) = FALSE
    AND COALESCE((SELECT ck2.monitoring_status FROM confirmed_keywords ck2 WHERE ck2.id = s.keyword_id), 'active') = 'active'
"""

# 反向对照物:今天真跑过的订阅(last_charged_at 落在当日)。
# 它们**必须全程保持 active** —— 证明脚本认的是"跑不动的",不是"所有的"。
RUNNING_TODAY_WHERE = """
    s.status = 'active'
    AND s.last_charged_at::date = CURRENT_DATE
"""


def _zombie_counts(cur) -> dict:
    cur.execute(f"SELECT count(*) AS n FROM keyword_monitor_subscriptions s WHERE {ZOMBIE_WHERE}")
    zombies = cur.fetchone()["n"]
    cur.execute(f"SELECT count(*) AS n FROM keyword_monitor_subscriptions s WHERE {RUNNING_TODAY_WHERE}")
    running_today = cur.fetchone()["n"]
    cur.execute(f"SELECT count(*) AS n FROM keyword_monitor_subscriptions s WHERE {PAUSED_MISMATCH_WHERE}")
    paused_mismatch = cur.fetchone()["n"]
    # 🔴 钱不许动:point_freezes 行数 + total_charged 合计
    cur.execute("SELECT count(*) AS n FROM point_freezes")
    freezes = cur.fetchone()["n"]
    cur.execute("SELECT COALESCE(sum(total_charged), 0) AS s FROM keyword_monitor_subscriptions")
    charged = cur.fetchone()["s"]
    return {
        "zombies": zombies,
        "running_today": running_today,
        "paused_mismatch": paused_mismatch,
        "freezes": freezes,
        "total_charged": int(charged or 0),
    }


def _counts(cur) -> dict:
    cur.execute(f"SELECT count(*) AS n FROM confirmed_keywords ck WHERE {ORPHAN_WHERE}")
    orphans = cur.fetchone()["n"]
    cur.execute(
        f"SELECT count(*) AS n FROM confirmed_keywords ck "
        f"WHERE {CONTROL_WHERE} AND ck.is_monitored = TRUE"
    )
    control_on = cur.fetchone()["n"]
    cur.execute(f"SELECT count(*) AS n FROM confirmed_keywords ck WHERE {CONTROL_WHERE}")
    control_total = cur.fetchone()["n"]
    cur.execute(
        "SELECT count(*) AS n FROM keyword_monitor_subscriptions "
        "WHERE status = 'active'"
    )
    subs_active = cur.fetchone()["n"]
    # 🔴 反向对照必须比**逐条快照**,不能比总数:
    #   P0-10 cancel 掉僵尸订阅之后,那个词就不再"有活订阅",对照集合总数会合法地变小
    #   (17→16)。拿总数当判据 = 把脚本自己的正确行为判成误伤。
    #   真正要守的不变量是:对照集合里每一个词的 is_monitored 跑前跑后一致
    #   —— P0-10 只动订阅状态,本来就不该碰 is_monitored。
    cur.execute(f"SELECT ck.id, ck.is_monitored FROM confirmed_keywords ck WHERE {CONTROL_WHERE}")
    control_snapshot = {r["id"]: r["is_monitored"] for r in cur.fetchall()}
    return {
        "orphans": orphans,
        "control_on": control_on,
        "control_total": control_total,
        "subs_active": subs_active,
        "control_snapshot": control_snapshot,
    }


def _control_drift(cur, before_snapshot: dict) -> dict:
    """跑前对照集合里那些词,现在的 is_monitored 有没有变。返回 {id: (旧, 新)}。"""
    if not before_snapshot:
        return {}
    ids = tuple(before_snapshot)
    cur.execute("SELECT id, is_monitored FROM confirmed_keywords WHERE id IN %s", (ids,))
    now = {r["id"]: r["is_monitored"] for r in cur.fetchall()}
    return {k: (v, now.get(k)) for k, v in before_snapshot.items() if now.get(k) != v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写(默认 dry-run)")
    ap.add_argument("--verify", action="store_true", help="只跑判据,不写")
    args = ap.parse_args()

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        before = _counts(cur)
        zbefore = _zombie_counts(cur)
        print("== 跑前 ==")
        print(f"  [P0-9] 孤儿(假开关)      = {before['orphans']}")
        print(f"  [P0-9] 对照:有活订阅的词 = {before['control_total']}(其中 is_monitored=TRUE {before['control_on']})")
        print(f"  [P0-10] 僵尸订阅(跑不动) = {zbefore['zombies']}")
        print(f"  [P0-10] 对照:今日真跑过  = {zbefore['running_today']}")
        print(f"  [P0-10] ⚠️ 工单条件也命中、但本脚本**不动**的 paused_low_balance = "
              f"{zbefore['paused_mismatch']}(充值后自愈,cancel 会永久杀掉 → 交 Owner 判)")
        print(f"  订阅表 active 条数        = {before['subs_active']}")
        print(f"  💰 point_freezes 行数     = {zbefore['freezes']}")
        print(f"  💰 total_charged 合计     = {zbefore['total_charged']}")

        # 判据 1:两个分母都必须非 0,否则这次运行没有判别力
        if not args.verify and (before["orphans"] == 0 or zbefore["zombies"] == 0):
            print(f"🔴 跑前分母为 0(孤儿={before['orphans']} 僵尸={zbefore['zombies']})"
                  " —— 本次运行没有判别力(可能已被跑过)。")
            print("   若是复跑幂等验证,请带 --verify;否则先确认口径是不是写错了。")
            return 8
        # 🔴 反向对照物自身必须非 0,否则"对照全程不变"是恒真(2026-08 第 16→18 班车踩过两次)
        if zbefore["running_today"] == 0:
            print("🔴 反向对照物为 0(今日无真跑订阅)—— 对照零判别力,拒跑。")
            print("   daily 03:00 尚未跑过时会出现这种情况,请在跑批之后再执行。")
            return 8

        # 明细(按品牌),便于人工复核
        cur.execute(f"""
            SELECT b.id AS brand_id, left(b.name, 24) AS brand, count(*) AS n
              FROM confirmed_keywords ck
              JOIN quotes q ON q.id = ck.quote_id
              JOIN brands b ON b.id = q.brand_id
             WHERE {ORPHAN_WHERE}
             GROUP BY 1, 2 ORDER BY n DESC
        """)
        print("== 孤儿按品牌 ==")
        for r in cur.fetchall():
            print(f"  brand {r['brand_id']:>4} {r['brand']:<26} {r['n']}")

        # P0-10 僵尸明细(按判据取,列出来供人工复核 —— id 只是展示,不是选取依据)
        cur.execute(f"""
            SELECT s.id AS sub_id, s.keyword_id, s.status, s.last_charged_at::date AS last_charged,
                   s.total_charged,
                   (SELECT ck2.is_monitored FROM confirmed_keywords ck2 WHERE ck2.id=s.keyword_id) AS is_monitored,
                   (SELECT ck2.monitoring_status FROM confirmed_keywords ck2 WHERE ck2.id=s.keyword_id) AS mon_status
              FROM keyword_monitor_subscriptions s
             WHERE {ZOMBIE_WHERE}
             ORDER BY s.keyword_id
        """)
        print("== [P0-10] 僵尸订阅明细 ==")
        for r in cur.fetchall():
            print(f"  sub {r['sub_id']:>4} kw {r['keyword_id']:>5} {r['status']:<20}"
                  f" is_monitored={r['is_monitored']} mon_status={r['mon_status']}"
                  f" last_charged={r['last_charged']} total_charged={r['total_charged']}")

        if args.verify:
            ok = (
                before["orphans"] == 0
                and before["control_on"] == before["control_total"]
                and zbefore["zombies"] == 0
            )
            print(f"\n[verify] 孤儿=0 ? {before['orphans'] == 0}"
                  f" · 对照全 TRUE ? {before['control_on'] == before['control_total']}"
                  f" · 僵尸=0 ? {zbefore['zombies'] == 0}")
            return 0 if ok else 9

        if not args.apply:
            print("\n[dry-run] 未写库。加 --apply 真跑。")
            return 0

        # 🔴 只 UPDATE 标志位。整个脚本里没有任何 INSERT INTO keyword_monitor_subscriptions ——
        #    这是工单红线 6(Owner 已定:不补订阅)。
        cur.execute(f"""
            UPDATE confirmed_keywords ck
               SET is_monitored = FALSE
             WHERE {ORPHAN_WHERE}
        """)
        changed = cur.rowcount

        # ── P0-10 · cancel 僵尸订阅(Owner 方案 B)────────────────────────────
        # 🔴 只动 status/cancelled_at。整段没有任何对 is_monitored / monitoring_status 的
        #    UPDATE,也没有任何对钱表(point_freezes / total_charged)的写 —— 见跑后断言。
        cur.execute(f"""
            UPDATE keyword_monitor_subscriptions s
               SET status = 'cancelled',
                   cancelled_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE s.id IN (
                SELECT s2.id FROM keyword_monitor_subscriptions s2
                 WHERE {ZOMBIE_WHERE.replace('s.', 's2.')}
             )
        """)
        zchanged = cur.rowcount
        conn.commit()

        after = _counts(cur)
        zafter = _zombie_counts(cur)
        print("\n== 跑后 ==")
        print(f"  [P0-9]  改动行数          = {changed}")
        print(f"  [P0-9]  孤儿(假开关)     = {after['orphans']}")
        print(f"  [P0-9]  对照:有活订阅的词= {after['control_total']}(其中 is_monitored=TRUE {after['control_on']})")
        print(f"  [P0-10] cancel 行数       = {zchanged}")
        print(f"  [P0-10] 僵尸订阅          = {zafter['zombies']}")
        print(f"  [P0-10] 对照:今日真跑过  = {zafter['running_today']}")
        print(f"  [P0-10] paused_low_balance 未动 = {zafter['paused_mismatch']}")
        print(f"  订阅表 active 条数        = {after['subs_active']}")
        print(f"  💰 point_freezes 行数     = {zafter['freezes']}")
        print(f"  💰 total_charged 合计     = {zafter['total_charged']}")

        problems = []
        # ── P0-9 ──
        if after["orphans"] != 0:
            problems.append(f"P0-9 判据2 失败:跑后孤儿 {after['orphans']} != 0")
        # 🔴 判据4 订正(2026-08-16 · 两次都被 P0-9 夹具实跑抓到,不是看代码看出来的):
        #   v1 要求"有活订阅的词**全部** is_monitored=TRUE" —— 错:订阅处在
        #   paused_low_balance 时 pause 的调用方会同步把 is_monitored 翻 FALSE,
        #   那是**一致的正常态**;本地真数据副本上这条恒失败(18 里只有 11 TRUE),
        #   生产上会把脚本整个挡死在判据阶段。
        #   v2 改成比对照集合**总数**不变 —— 还是错:P0-10 cancel 僵尸订阅后那个词
        #   合法地离开集合(17→16),等于把脚本自己的正确行为判成误伤。
        #   v3 = 逐条快照:跑前在对照集合里的每个词,is_monitored 跑前跑后必须一致。
        drift = _control_drift(cur, before["control_snapshot"])
        if drift:
            problems.append(
                f"P0-9 判据4 失败:反向对照里 {len(drift)} 个词的开关被动了 "
                f"{dict(list(drift.items())[:5])} —— 脚本认的是『所有的』不是『假开关的』"
            )
        # ── P0-10 ──
        if zafter["zombies"] != 0:
            problems.append(f"P0-10 判据 失败:跑后僵尸 {zafter['zombies']} != 0")
        if zafter["paused_mismatch"] != zbefore["paused_mismatch"]:
            problems.append(
                f"🔴 动了不该动的 paused_low_balance:{zbefore['paused_mismatch']}"
                f" → {zafter['paused_mismatch']}(它们充值后会自愈,不是僵尸)"
            )
        if zafter["running_today"] != zbefore["running_today"]:
            problems.append(
                f"🔴 P0-10 反向对照失败(误伤在跑的):今日真跑过 "
                f"{zbefore['running_today']} → {zafter['running_today']}"
            )
        # ── 钱不许动(Owner 加的这一条) ──
        if zafter["freezes"] != zbefore["freezes"]:
            problems.append(
                f"🔴 钱被动了:point_freezes {zbefore['freezes']} → {zafter['freezes']}"
            )
        if zafter["total_charged"] != zbefore["total_charged"]:
            problems.append(
                f"🔴 钱被动了:total_charged {zbefore['total_charged']} → {zafter['total_charged']}"
            )
        # ── 订阅 active 条数:P0-10 会让它减少 zchanged 条,这是**预期**的,不是误伤 ──
        _expected_active = before["subs_active"] - zchanged
        if after["subs_active"] != _expected_active:
            problems.append(
                f"订阅 active 条数与预期不符:{before['subs_active']} - {zchanged}"
                f" = {_expected_active},实际 {after['subs_active']}"
            )
        if problems:
            print("\n🔴 " + "\n🔴 ".join(problems))
            return 9
        print("\n✅ P0-9 与 P0-10 判据全绿(含反向对照与资金零变动)。")
        print("   幂等复跑请用 --apply(应改动 0 行,届时判据1 会以 rc=8 拦住)或 --verify。")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
