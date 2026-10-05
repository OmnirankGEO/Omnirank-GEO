"""变异验证 —— 工单 2026-07-29 要求的全部变异逐条注入,断言对应判别锁转红。

用法:python tests/portal_notify_2026_07_29/run_mutations.py

规则(工单 §边界):
  · 变异注入后先做 **import 冒烟**,证明代码还能跑(而不是把语法搞坏了骗一个红);
  · FAILED 与 ERROR **两类都统计**,都算 KILLED;
  · 每条变异必须至少杀掉它对应的那条锁,否则判 SURVIVED(= 锁是假绿)。

每条变异跑完自动还原;中途异常也在 finally 里还原。
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = "tests/portal_notify_2026_07_29"

BACKEND = "python"
PYTEST = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]


# (编号, 说明, 文件, 原串, 变异串, 必须转红的锁)
# 原串/变异串可以是**并列的两个列表**(复合变异):某些不变量由两道独立守卫共同承担,
# 只拆掉一道另一道会兜住 —— 那样的单点变异 SURVIVED 并不证明锁是假绿,
# 但为了真正验证"边界锁得住",这里把该场景写成一次性拆掉全部守卫的复合变异。
MUTATIONS = [
    # ---------------- T1 ----------------
    (
        "T1-①", "把续期实现成调 generate_client_token(旧链接失效)",
        "db/monitoring_db.py",
        """        cursor.execute(
            \"\"\"
            UPDATE client_access_tokens
            SET expires_at = %s
            WHERE id = ANY(%s)
            \"\"\",
            (new_expires_at, [item["id"] for item in renewed]),
        )""",
        """        conn.commit()
        generate_client_token(int(quote_id), days_valid=90)
        cursor = conn.cursor()""",
        [
            f"{TESTS}/test_t1_portal_token_renew.py::test_lock1_renew_extends_expiry_without_rotating_token",
            f"{TESTS}/test_t1_portal_token_renew.py::test_lock2_customer_original_url_still_works",
        ],
    ),
    (
        "T1-②", "去掉服务期上限(无限续期)",
        "db/monitoring_db.py",
        """    service_end = contract_start + timedelta(days=days)
    if service_end < today:
        # 服务期已结束 → 不再续期(工单 §1.3.2),维持原到期日。
        return (None, service_end, "service_ended")""",
        """    service_end = contract_start + timedelta(days=days)""",
        [f"{TESTS}/test_t1_portal_token_renew.py::test_lock3_service_ended_does_not_renew"],
    ),
    (
        "T1-③", "去掉 new > old 判断(幂等破)",
        "db/monitoring_db.py",
        """            if r["expires_at"] is not None and r["expires_at"] < new_expires_at""",
        """            if r["expires_at"] is not None""",
        [f"{TESTS}/test_t1_portal_token_renew.py::test_lock4_idempotent_when_new_not_greater_than_old"],
    ),
    (
        "T1-④", "续期异常冒泡阻断交付",
        "db/monitoring_db.py",
        """    except Exception as exc:  # noqa: BLE001 —— 交付主链不可因续期失败而失败
        print(f"[PortalTokenRenew] 续期失败(不影响交付) trigger={trigger} quote_id={quote_id}: {exc}")
        return {"status": "error", "reason": str(exc)[:200], "renewed": 0}""",
        """    except Exception as exc:  # 变异:让异常冒泡
        raise""",
        [f"{TESTS}/test_t1_portal_token_renew.py::test_lock7_renew_failure_never_breaks_delivery"],
    ),
    (
        "T1-⑤", "续期不落审计(留痕丢失)",
        "db/monitoring_db.py",
        """        _record_portal_token_renew_audit(
            cursor, quote_id=int(quote_id), renewed=renewed,""",
        """        _noop_audit(
            cursor, quote_id=int(quote_id), renewed=renewed,""",
        [f"{TESTS}/test_t1_portal_token_renew.py::test_lock6_renew_audit_is_distinguishable_from_generate"],
    ),
    (
        "T1-⑥", "已过期 token 不复活(服务期内仍打不开)",
        "db/monitoring_db.py",
        """            if r["expires_at"] is not None and r["expires_at"] < new_expires_at""",
        """            if r["expires_at"] is not None and today_guard(r["expires_at"]) and r["expires_at"] < new_expires_at""",
        [f"{TESTS}/test_t1_portal_token_renew.py::test_lock5_expired_token_inside_service_window_is_revived"],
    ),
    # ---------------- T2 ----------------
    (
        "T2-①", "白标脱敏被绕过(门户里出现服务商名)",
        "services/demo_access.py",
        """def _is_private_demo_key(key: str) -> bool:
    if key in DEMO_PRIVATE_KEY_ALLOWLIST:
        return False
    return any(pattern.search(key) for pattern in DEMO_PRIVATE_KEY_PATTERNS)""",
        """def _is_private_demo_key(key: str) -> bool:
    return False""",
        [f"{TESTS}/test_t2_demo_portal_live.py::test_lockD_whitelabel_and_privacy_scrub_still_applies_to_live_payload"],
    ),
    (
        "T2-②", "演示门户改回只认冻结快照(轮换后入口打不开)",
        "services/demo_access.py",
        """    return (
        live_portal_link(context, quote_id=quote_id)
        or live_snapshot_portal_link(context, quote_id=quote_id)
    )""",
        """    return live_snapshot_portal_link(context, quote_id=quote_id)""",
        [
            f"{TESTS}/test_t2_demo_portal_live.py::test_lockA_portal_entry_resolves_even_without_frozen_portal_links",
            f"{TESTS}/test_t2_demo_portal_live.py::test_lockB_plaintext_customer_token_never_leaves_the_server",
        ],
    ),
    (
        "T2-③", "子请求不带真实门户 token(不是真门户链路)",
        "services/demo_access.py",
        """            (b"authorization", f"Bearer {portal_token}".encode("utf-8")),""",
        """            (b"authorization", b"Bearer DEMOFAKE"),""",
        [f"{TESTS}/test_t2_demo_portal_live.py::test_lockC_transport_issues_a_real_subrequest_with_the_real_token"],
    ),
    (
        "T2-④", "跨品牌 quote 也放行(授权边界破 · 复合:两道守卫一起拆)",
        "services/demo_access.py",
        [
            """    if target not in _quote_ids(context):
        return None""",
            """                    WHERE t.quote_id = %s AND q.brand_id = %s
                      AND COALESCE(t.is_active, 0) <> 0""",
        ],
        [
            """    pass""",
            """                    WHERE t.quote_id = %s AND (%s IS NOT NULL)
                      AND COALESCE(t.is_active, 0) <> 0""",
        ],
        [f"{TESTS}/test_t2_demo_portal_live.py::test_lockE2_cross_brand_quote_is_refused"],
    ),
    # ---------------- T3 ----------------
    (
        "T3-④a", "去掉终态去重/覆盖(两条矛盾状态并列)",
        "db/team_db.py",
        """    where = ["n.user_id = %s", "n.id NOT IN (SELECT id FROM superseded)"]
    params = list(cte_params) + [user_id, user_id]
    if unread_only:
        where.append("n.is_read = FALSE")
    cursor.execute(
        f\"\"\"
        WITH {cte}
        SELECT n.id, n.type, n.title, n.content, n.link, n.is_read,""",
        """    where = ["n.user_id = %s", "1=1"]
    params = list(cte_params) + [user_id, user_id]
    if unread_only:
        where.append("n.is_read = FALSE")
    cursor.execute(
        f\"\"\"
        WITH {cte}
        SELECT n.id, n.type, n.title, n.content, n.link, n.is_read,""",
        [f"{TESTS}/test_t3_notification_center.py::test_lock6_conflicting_terminal_states_are_not_shown_side_by_side"],
    ),
    (
        "T3-④b", "终态分组按 event_type 前缀猜(退款被盖掉)",
        "db/team_db.py",
        """            JOIN terminal_map tm ON tm.event_type = split_part(n.event_key, ':', 1)""",
        """            JOIN terminal_map tm ON split_part(tm.event_type,'.',1) = split_part(split_part(n.event_key, ':', 1),'.',1)""",
        [f"{TESTS}/test_t3_notification_center.py::test_lock6c_refund_notification_is_never_superseded"],
    ),
    (
        "T3-②", "feed 正文被截断",
        "db/team_db.py",
        """        "content": row.get("content") or "",""",
        """        "content": (row.get("content") or "")[:20],""",
        [f"{TESTS}/test_t3_notification_center.py::test_lock1_feed_content_is_verbatim_and_untruncated"],
    ),
    (
        "T3-③", "已读不持久化(标已读变 no-op)",
        "db/team_db.py",
        """    raw = str(item_id or "").strip()
    if raw.startswith("sys:"):
        return mark_user_notification_read(int(raw[4:]), user_id)""",
        """    raw = str(item_id or "").strip()
    if raw.startswith("sys:"):
        return True""",
        [f"{TESTS}/test_t3_notification_center.py::test_lock3_read_state_persists_across_reload"],
    ),
    (
        "T3-⑤", "扣费通知路径混入写操作",
        "db/team_db.py",
        """def _billing_item(row: Dict[str, Any], watermark: int) -> Dict[str, Any]:""",
        """def _billing_item(row: Dict[str, Any], watermark: int) -> Dict[str, Any]:
    _c = get_connection()
    try:
        _cur = _c.cursor()
        _cur.execute(
            "INSERT INTO point_transactions (user_id,type,point_type,amount,balance_after,description)"
            " VALUES (0,'consume','paid',-1,0,'mutation')"
        )
        _c.commit()
    finally:
        _c.close()""",
        [f"{TESTS}/test_t3_notification_center.py::test_lock7_billing_render_path_performs_zero_fund_writes"],
    ),
    (
        "T3-⑥", "两类未读数不分开(筛选/计数失真)",
        "db/team_db.py",
        """        billing_unread = _billing_count(cursor, user_id, unread_only=True, watermark=watermark)""",
        """        billing_unread = 0""",
        [f"{TESTS}/test_t3_notification_center.py::test_lock4_two_categories_filter_and_count_independently"],
    ),
    (
        "T3-⑦", "历史页不翻页(只取首屏)",
        "db/team_db.py",
        """        ORDER BY n.created_at DESC, n.id DESC
        LIMIT %s OFFSET %s""",
        """        ORDER BY n.created_at DESC, n.id DESC
        LIMIT %s OFFSET 0*%s""",
        [f"{TESTS}/test_t3_notification_center.py::test_lock5_history_pagination_reaches_older_items"],
    ),
    (
        "T3-根因", "终态 completed 允许被降级成 failed",
        "db/monitoring_db.py",
        """        guard_sql = ""
        if status == "failed":
            guard_sql = " AND status IS DISTINCT FROM 'completed'\"""",
        """        guard_sql = \"\"""",
        [
            f"{TESTS}/test_t3_notification_center.py::test_root_cause_db_level_guard_refuses_completed_to_failed",
        ],
    ),
    (
        # [Deploy-CTO 2026-07-29 部署前小修 · Review 要求]
        # 守卫无声 = 用一个新的静默失败替换旧的,而且更难查(它长得像"正常工作")。
        # 这条变异把留痕整段抽掉,只留原来的 print —— print 不进 logging,
        # caplog 抓不到,留痕锁必须转红。
        "T3-根因留痕", "守卫拦截不留痕(退回 print)",
        "db/monitoring_db.py",
        """            import logging as _lg
            import traceback as _tb

            cursor.execute("SELECT status FROM monitoring_tasks WHERE id = %s", (task_id,))""",
        """            print(f"[MonitoringTask] 拒绝降级 task_id={task_id}")
            cursor.execute("SELECT status FROM monitoring_tasks WHERE id = %s", (task_id,))""",
        [
            f"{TESTS}/test_t3_notification_center.py::test_guard_interception_leaves_a_trace",
        ],
    ),
]

# 前端变异:门禁脚本 verify-notification-center.mjs 必须转红
FRONTEND_MUTATIONS = [
    (
        "T3-①", "去掉详情弹窗(点击又没有详情)",
        "frontend/src/pages/Notifications/NotificationCenter.tsx",
        "      <NotificationDetailDialog",
        "      <NoDialog",
    ),
    (
        "T3-②fe", "弹窗正文重新截断",
        "frontend/src/components/layout/NotificationDetailDialog.tsx",
        "whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground/90",
        "line-clamp-2 break-words text-sm leading-relaxed text-foreground/90",
    ),
    (
        "T3-③fe", "已读只改本地 state(不打后端)",
        "frontend/src/components/layout/NotificationBell.tsx",
        "'/api/user/notifications/feed/read'",
        "'/api/user/nope'",
    ),
    (
        "T3-⑧fe", "历史页退回不翻页",
        "frontend/src/pages/Notifications/NotificationCenter.tsx",
        "const page = await fetchPage(items.length);",
        "const page = await fetchPage(0);",
    ),
]


def _run(cmd, cwd=ROOT):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")


def _read(rel):
    return io.open(ROOT / rel, encoding="utf-8").read()


def _write(rel, text):
    io.open(ROOT / rel, "w", encoding="utf-8", newline="\n").write(text)


def _import_smoke(rel):
    """变异后先证明"代码还跑得起来"——否则红的是语法不是行为。"""
    module = rel.replace("/", ".").removesuffix(".py")
    result = _run([sys.executable, "-c", f"import {module}"])
    return result.returncode == 0, (result.stderr or "")[-400:]


def main() -> int:
    baseline = _run(PYTEST + [TESTS])
    print(f"[baseline] {'PASS' if baseline.returncode == 0 else 'FAIL'} :: {baseline.stdout.strip().splitlines()[-1]}")
    if baseline.returncode != 0:
        print(baseline.stdout[-3000:])
        return 1

    survived = []
    print("\n=== 后端变异 ===")
    for tag, desc, rel, old, new, locks in MUTATIONS:
        original = _read(rel)
        olds = old if isinstance(old, list) else [old]
        news = new if isinstance(new, list) else [new]
        missing = [o for o in olds if o not in original]
        if missing:
            print(f"  ⚠️  {tag} 锚串没命中 {rel} —— 变异未注入(判 SURVIVED)")
            survived.append((tag, desc, "anchor-miss"))
            continue
        try:
            mutated = original
            for o, n in zip(olds, news):
                mutated = mutated.replace(o, n, 1)
            _write(rel, mutated)
            ok, err = _import_smoke(rel)
            smoke = "import-ok" if ok else "import-fail"
            result = _run(PYTEST + locks)
            out = result.stdout
            killed = result.returncode != 0 and ("failed" in out or "error" in out)
            status = "KILLED" if killed else "SURVIVED"
            if not killed:
                survived.append((tag, desc, out.strip().splitlines()[-1] if out.strip() else "no-output"))
            print(f"  {'✅' if killed else '❌'} {tag} {status} [{smoke}] {desc}")
            if not ok:
                print(f"       import 冒烟失败(红可能来自语法而非行为): {err.strip()[:200]}")
        finally:
            _write(rel, original)

    print("\n=== 前端变异(门禁 verify-notification-center.mjs)===")
    fe_cwd = ROOT / "frontend"
    gate = ["node", "scripts/verify-notification-center.mjs"]
    base_gate = _run(gate, cwd=fe_cwd)
    print(f"  [baseline] {'PASS' if base_gate.returncode == 0 else 'FAIL'}")
    if base_gate.returncode != 0:
        print(base_gate.stdout, base_gate.stderr)
        return 1
    for tag, desc, rel, old, new in FRONTEND_MUTATIONS:
        original = _read(rel)
        if old not in original:
            print(f"  ⚠️  {tag} 锚串没命中 {rel}")
            survived.append((tag, desc, "anchor-miss"))
            continue
        try:
            _write(rel, original.replace(old, new, 1))
            result = _run(gate, cwd=fe_cwd)
            killed = result.returncode != 0
            if not killed:
                survived.append((tag, desc, "gate still green"))
            print(f"  {'✅' if killed else '❌'} {tag} {'KILLED' if killed else 'SURVIVED'} {desc}")
        finally:
            _write(rel, original)

    total = len(MUTATIONS) + len(FRONTEND_MUTATIONS)
    print(f"\n=== 汇总:{total - len(survived)}/{total} KILLED ===")
    for tag, desc, why in survived:
        print(f"  ❌ SURVIVED {tag} {desc} :: {why}")

    # 还原后必须回到全绿(证明变异没有留下残渣)
    after = _run(PYTEST + [TESTS])
    print(f"[还原后] {'PASS' if after.returncode == 0 else 'FAIL'} :: {after.stdout.strip().splitlines()[-1]}")
    return 0 if not survived and after.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
