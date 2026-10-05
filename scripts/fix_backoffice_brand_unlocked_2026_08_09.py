#!/usr/bin/env python3
"""[客户反馈④(c) 2026-08-09] 后台换肤授权位 · 存量审计(默认只读)。

🔴 工单 ④(c) 原本要求的"UPDATE 存量修正"在这里**故意没有默认执行** —— 因为取证
   推翻了工单对成因的判断。下面是证据链,先看完再决定要不要 --apply。

────────────────────────────────────────────────────────────────────────────
取证(生产快照只读,2026-08-09)
────────────────────────────────────────────────────────────────────────────
工单说:user 132(翔玉咨询)`oem+active+unlocked_by_admin=true` 而
`backoffice_brand_unlocked=FALSE`,是"管理员漏点第三个开关"。

实际不是:
  1. `whitelabel_audit` id=21 —— 2026-07-31 19:57:43,admin(user 1) 把 132 的
     `backoffice_brand_unlocked` 从 False 改成 **True**。审计表是 append-only
     (触发器禁 UPDATE/DELETE),这条改不掉。**管理员点了。**
  2. 今天库里 132 仍是 FALSE,但 `backoffice_brand_granted_by=1` /
     `backoffice_brand_granted_at=2026-07-31 19:57:43` 两个授权戳**还在**。
  3. 审计里**没有**任何一条把它改回 False 的记录。应用侧每次写授权位都留痕
     → 改回去的不是人。
  4. `scripts/migration_whitelabel_backoffice_scope_2026_07_22.sql` §4 是
     `UPDATE ... SET backoffice_brand_unlocked = FALSE WHERE user_id = 132`,
     它在 `db/migration_manifest.py` 里,而 `scripts/prestart.py` **每次部署
     全量重跑清单** → 每次部署都把 admin 的决定无声推翻一次。

  → 真因 = **部署流水线每次把这个账号的授权重置**,不是管理员健忘。
    对照:125(08-07 授权)、133(08-05 授权)至今仍是 TRUE —— 它们没被 §4 点名。

────────────────────────────────────────────────────────────────────────────
所以本次修法
────────────────────────────────────────────────────────────────────────────
🔴 **迁移 §4 一个字没改**(本包对那个 .sql 只加了 24 行注释,SQL 语义零改动)。
   一度想给它加 `NOT EXISTS(admin 显式授权审计行)` 守卫,**当场否掉了**:
   同一份迁移的 §6 `whitelabel_backoffice_schema_blockers()` 把"132 为 TRUE"
   定义成 blocker,prestart / 运行时自检 / release readiness 三处 fail-closed
   → 给 §4 加守卫 = 把"静默重置"升级成"部署卡死",比原状更糟。
   132 固定 customer-only 是**裁决的不变式**,不是忘拨的开关。

- 代码侧(本包已做):**写入侧** PIN 守卫 —— `api/referral_api.py` 对被钉死的账号
  直接 400 并说清楚原因;管理页那个下拉同步置灰。堵的是"能点、点了没用、
  还没人告诉他"这条静默路径,**不改变任何授权语义**。
- 数据侧:**不由脚本代替 Owner 做授权决定**。要放开 132,要改的是迁移 §4 + §6 合同,
  不是这个脚本;那是裁决变更。
- 本脚本因此默认只审计,并把"被 §4 钉死的账号"单列一节交 Owner 裁决。

用法
────
  python scripts/fix_backoffice_brand_unlocked_2026_08_09.py            # 只审计(默认)
  python scripts/fix_backoffice_brand_unlocked_2026_08_09.py --dry-run  # 跑 UPDATE 再 ROLLBACK
  # 真写(先备份:docker exec omnirank-db pg_dump -U geo_admin geo_agentscope > backup_$(date +%Y%m%d_%H%M).sql)
  python scripts/fix_backoffice_brand_unlocked_2026_08_09.py --apply --operator-user-id <admin用户id>

  🔴 --apply **不会**动被 §4 钉死的账号(需 --include-pinned 显式带上,
     那等于替 Owner 做授权决定,请先拿到批准)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 迁移 §4 硬编码钉死的账号。此处只做**显示与排除**,不是第二份授权口径 ——
# 授权口径唯一在那条迁移里;这里写死同一个 id 是为了让审计清单能把它单独列出来。
MIGRATION_PINNED_USER_IDS = (132,)

# 命中判据:客户面已按 oem 生效(三条件齐),唯独后台换肤那一位没开。
SELECT_SQL = """
    SELECT user_id, company_name, whitelabel_mode, whitelabel_status,
           unlocked_by_admin, backoffice_brand_unlocked,
           backoffice_brand_granted_by, backoffice_brand_granted_at
      FROM whitelabel_settings
     WHERE whitelabel_mode = 'oem'
       AND whitelabel_status = 'active'
       AND unlocked_by_admin = TRUE
       AND COALESCE(backoffice_brand_unlocked, FALSE) = FALSE
     ORDER BY user_id
"""

# 反向对照:授权位已开的那些。两个数都不为 0,判据才有判别力。
CONTROL_SQL = """
    SELECT user_id, company_name
      FROM whitelabel_settings
     WHERE COALESCE(backoffice_brand_unlocked, FALSE) = TRUE
     ORDER BY user_id
"""

# "人做过决定"的证据:append-only 审计里 admin 把该位置 True 的记录。
ADMIN_GRANT_AUDIT_SQL = """
    SELECT user_id, actor_user_id, new_value, created_at
      FROM whitelabel_audit
     WHERE field = 'backoffice_brand_unlocked'
       AND actor_role = 'admin'
       AND lower(COALESCE(new_value, '')) IN ('true', 't', '1')
     ORDER BY created_at
"""

UPDATE_SQL = """
    UPDATE whitelabel_settings
       SET backoffice_brand_unlocked = TRUE,
           backoffice_brand_granted_by = %s,
           backoffice_brand_granted_at = NOW()
     WHERE user_id = %s
       AND whitelabel_mode = 'oem'
       AND whitelabel_status = 'active'
       AND unlocked_by_admin = TRUE
       AND COALESCE(backoffice_brand_unlocked, FALSE) = FALSE
 RETURNING user_id, backoffice_brand_unlocked
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="后台换肤授权位存量审计")
    parser.add_argument("--apply", action="store_true", help="真写(默认只审计)")
    parser.add_argument("--dry-run", action="store_true",
                        help="真跑 UPDATE 后 ROLLBACK(验证影响行数,不落库)")
    parser.add_argument("--include-pinned", action="store_true",
                        help="连迁移 §4 钉死的账号一起改 —— 这是**授权决定**,需 Owner 批准")
    parser.add_argument("--operator-user-id", type=int, default=None,
                        help="执行本次修正的 admin 用户 id(写进 granted_by 与 audit_log)")
    args = parser.parse_args()

    if args.apply and args.operator_user_id is None:
        print("❌ --apply 必须同时给 --operator-user-id(授权位要留下是谁开的)")
        return 2

    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(SELECT_SQL)
        hits = [dict(r) for r in cur.fetchall()]
        cur.execute(CONTROL_SQL)
        control = [dict(r) for r in cur.fetchall()]
        cur.execute(ADMIN_GRANT_AUDIT_SQL)
        grants = [dict(r) for r in cur.fetchall()]

        pinned = [r for r in hits if int(r["user_id"]) in MIGRATION_PINNED_USER_IDS]
        plain = [r for r in hits if int(r["user_id"]) not in MIGRATION_PINNED_USER_IDS]
        granted_ids = {int(g["user_id"]) for g in grants}

        print("=" * 78)
        print(f"命中(oem+active+unlocked_by_admin 齐 · 后台换肤位没开)= {len(hits)} 户")
        for row in hits:
            uid = int(row["user_id"])
            marks = []
            if uid in MIGRATION_PINNED_USER_IDS:
                marks.append("⛔迁移§4钉死")
            if uid in granted_ids:
                marks.append("⚠️admin 曾显式授权过(审计有痕)")
            print(f"  - user_id={uid:<6} {row.get('company_name') or '(无公司名)'}"
                  f"  {' '.join(marks)}")
        print(f"反向对照(授权位已开)= {len(control)} 户 {[r['user_id'] for r in control]}")
        print("  ↑ 两个数都不为 0 才说明判据有判别力(全 0=没东西可修;对照 0=判据可能恒真)")
        print("-" * 78)
        print(f"审计里 admin 显式授权过的记录 = {len(grants)} 条")
        for g in grants:
            flag = "❗当前却是 FALSE" if int(g["user_id"]) in {int(r['user_id']) for r in hits} else ""
            print(f"  - user_id={g['user_id']} by admin={g['actor_user_id']} at {g['created_at']} {flag}")
        print("  ↑ 出现「❗当前却是 FALSE」= 有人授权过、后来被无声改回去了"
              "(本包已在写入侧加 PIN 守卫:被钉死的账号现在点不动,也不会再被无声改回)")
        print("=" * 78)

        if pinned:
            print("⛔ 下列账号被 `migration_whitelabel_backoffice_scope_2026_07_22.sql` §4")
            print("   硬编码钉死为 customer-only。要不要放开是**授权决定**,归 Owner:")
            for row in pinned:
                print(f"     user_id={row['user_id']} {row.get('company_name') or ''}")
            print("   本脚本默认**不动它们**(要动请显式 --include-pinned,并附 Owner 批准)。")

        targets = hits if args.include_pinned else plain
        if not targets:
            print("✅ 无可自动修正的账号(命中集合为空,或全部属于需 Owner 裁决的钉死账号)。")
            return 0
        if not (args.apply or args.dry_run):
            print("ℹ️  只审计模式,未做任何写入。")
            return 0

        changed = []
        for row in targets:
            cur.execute(UPDATE_SQL, (args.operator_user_id, row["user_id"]))
            got = cur.fetchone()
            if got:
                changed.append(int(got["user_id"]))

        print(f"UPDATE 影响 {len(changed)} 行: {changed}")
        if len(changed) != len(targets):
            print("⚠️  影响行数与命中数不一致 —— 两次读之间数据变了,已整体回滚,请重跑审计。")
            conn.rollback()
            return 3

        if args.dry_run:
            conn.rollback()
            print("✅ dry-run 完成,已 ROLLBACK,库里一个字没改。")
            return 0

        try:
            from db.auth_db import create_audit_log
            for uid in changed:
                create_audit_log(
                    user_id=args.operator_user_id,
                    username=None,
                    action="whitelabel_backoffice_unlock_backfill",
                    module="whitelabel",
                    entity_type="whitelabel_settings",
                    entity_id=uid,
                    summary="存量修正:oem 已生效但后台换肤授权位没开(客户反馈④c)",
                    before={"backoffice_brand_unlocked": False},
                    after={"backoffice_brand_unlocked": True},
                )
        except Exception as exc:
            print(f"⚠️  audit_log 写入失败,整体回滚:{exc}")
            conn.rollback()
            return 4

        conn.commit()
        print(f"✅ 已提交。修正 {len(changed)} 户:{changed}")
        print("🔴 提醒:后台换肤**总闸**(服务端环境开关)不开的话,这一列改成 TRUE 也不生效。")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
