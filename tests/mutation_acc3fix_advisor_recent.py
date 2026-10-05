"""[WO-ACCEPTANCE-3FIX-2026-08-05 项3] recent-conversations 恒返空 · 变异自检。

三关缺一即判无效变异:锚点唯一命中(命中 != 1 记 **SKIP** 不记 SURVIVED)、
落盘后文件真变了、至少一个预期用例转红。

清单覆盖三个方向:
  · **退回本 bug**(错列名 / except 吞成成功返回空);
  · **只修一半**(删了列名但没管 except / 修了 except 但没补 conversation_id);
  · **修坏别的**(把归属过滤弄丢 / 把 401 也吞了 / 真空结果也 500)。

跑法: PYTHONIOENCODING=utf-8 TEST_DATABASE_URL=... python tests/mutation_acc3fix_advisor_recent.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

API = "api/advisor_api.py"
PY_TESTS = "tests/test_acc3fix_advisor_recent_2026_08_05.py"

MUTATIONS: list[tuple[str, str, str, str, str, list[str]]] = [
    # ---------------- 退回本 bug ----------------
    ("C1", "🔴 列名改回 c.session_id(事故本体:UndefinedColumn)",
     API,
     '            SELECT c.id AS row_id, c.conversation_id, c.advisor_id, c.title, c.updated_at,\n',
     '            SELECT c.id, c.advisor_id, c.title, c.updated_at, c.session_id,\n',
     ["test_every_referenced_column_exists__must_hit",
      "test_conversation_id_is_selected__must_hit",
      "test_response_id_matches_sibling_endpoint_contract__must_hit"]),

    ("C2", "🔴 except 改回吞成「成功返回空」(本单最值钱的那条)",
     API,
     '        raise HTTPException(status_code=500, detail="获取最近对话失败,请稍后重试")\n',
     '        return {"success": True, "conversations": []}\n',
     ["test_sql_error_becomes_an_error_not_empty_list__must_hit",
      "test_sql_error_never_returns_success_true__must_not_hit",
      "test_no_other_handler_returns_success_true_on_failure__must_hit"]),

    ("C3", "🔴 只删列名不动 except(工单 §3.4 禁做第 1 条 —— 下次换个字段又是一次恒返空)",
     API,
     '    except HTTPException:\n'
     '        # 🔴 鉴权/权限拒绝原样抛出。守卫虽已挪到 try 外,这条仍要留:\n'
     '        #    try 内任何一层将来加了 HTTPException,不会再被下面那个 except 吞成 200。\n'
     '        raise\n',
     '    except HTTPException as _reraise:\n'
     '        return {"success": True, "conversations": []}\n',
     ["test_auth_rejection_still_401__must_hit",
      "test_http_exception_inside_try_is_not_swallowed__must_hit"]),

    # ---------------- 只修一半 ----------------
    ("C4", "🔴 补了列名但没把 conversation_id 交出去(列表能出来,每条点进去都是新开空对话)",
     API,
     '            item["id"] = item.get("conversation_id")\n',
     '            pass\n',
     ["test_response_id_matches_sibling_endpoint_contract__must_hit"]),

    # ---------------- 修坏别的 ----------------
    ("C6", "🔴 归属过滤弄丢(工单 §3.5:不许因为修这个 bug 把 E 节刚上线的过滤弄丢)",
     API,
     '        _owner_filter = "" if user.get("is_admin") else " AND c.owner_user_id = %s"\n',
     '        _owner_filter = ""\n',
     ["test_non_admin_query_carries_owner_filter__must_hit"]),

    ("C7", "🔴 归属过滤的参数丢了(SQL 带过滤但参数不带 = 直接报错或错配)",
     API,
     '        _params = [limit] if user.get("is_admin") else [user.get("user_id"), limit]\n',
     '        _params = [limit]\n',
     ["test_non_admin_query_carries_owner_filter__must_hit"]),

    ("C8", "admin 也被套上归属过滤(修过头:admin 反而看不到了)",
     API,
     '        _owner_filter = "" if user.get("is_admin") else " AND c.owner_user_id = %s"\n',
     '        _owner_filter = " AND c.owner_user_id = %s"\n',
     ["test_admin_query_has_no_owner_filter__must_not_hit"]),

    ("C9", "blocked 顾问的过滤被顺手删掉(E 节的另一半)",
     API,
     "            WHERE COALESCE(a.identity_status, 'draft') <> 'blocked'\"\"\" + _owner_filter + \"\"\"\n",
     '            WHERE 1=1"""  + _owner_filter + """\n',
     ["test_blocked_advisor_filter_kept__must_hit"]),

    ("C10", "🔴 内部字段漏给前端(顾问真名/来源名绕过 project_advisor_row 的公开身份投影)",
     API,
     '            item.pop("advisor_public_name", None)\n',
     '            pass\n',
     ["test_response_still_carries_display_fields__must_not_hit"]),

    ("C11", "失败路径不关连接(唯一的 finally 分支失效 → 连接泄漏)",
     API,
     '    finally:\n        if conn is not None:\n            try:\n                conn.close()\n',
     '    finally:\n        if conn is None:\n            try:\n                conn.close()\n',
     ["test_connection_closed_on_error_path__must_hit"]),

    ("C12", "🔴 投影里的 conversation_id 被去掉,只剩子查询里那一处(弱锁试探)",
     API,
     '            SELECT c.id AS row_id, c.conversation_id, c.advisor_id, c.title, c.updated_at,\n',
     '            SELECT c.id AS row_id, c.advisor_id, c.title, c.updated_at,\n',
     ["test_conversation_id_is_selected__must_hit"]),
]


def read_src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_src(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def run_locks() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_test:geo_test@127.0.0.1:5432/test_geo_agentscope")
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PY_TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return (1 if p.returncode else 0), (p.stdout or "") + (p.stderr or "")


def red_markers(output: str) -> set[str]:
    marks: set[str] = set()
    for line in output.splitlines():
        if line.startswith(("FAILED ", "ERROR ")) and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
    return marks


def main() -> int:
    rc, out = run_locks()
    if rc != 0:
        print("基线不绿,先修基线:\n" + out[-3000:])
        return 2
    print("基线 GREEN(反向对照:未变异的树必须全绿)")
    print("=" * 72)

    killed, survived, skipped = 0, [], []
    for mid, desc, rel, old, new, expect in MUTATIONS:
        path = REPO / rel
        original = read_src(path)
        hits = original.count(old)
        if hits != 1:
            print(f"[{mid}] ⏭️ SKIP · 锚点命中 {hits} 次(需恰好 1 次): {desc}")
            skipped.append((mid, desc, f"锚点命中 {hits} 次"))
            continue
        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"[{mid}] ⏭️ SKIP · 落盘后文件没变(no-op): {desc}")
            skipped.append((mid, desc, "文件未改变"))
            continue

        write_src(path, mutated)
        try:
            m_rc, m_out = run_locks()
        finally:
            write_src(path, original)
        assert read_src(path) == original, f"[{mid}] 还原失败,树被污染了"

        reds = red_markers(m_out)
        hit = sorted(reds & set(expect))
        if m_rc == 0:
            print(f"[{mid}] ❌ 存活(零转红): {desc}")
            survived.append((mid, desc, "零转红"))
        elif not hit:
            print(f"[{mid}] ⚠️ 转红但不是预期那些: {desc}\n"
                  f"        预期 {expect} / 实际 {sorted(reds)}")
            survived.append((mid, desc, f"红的是 {sorted(reds)}"))
        else:
            killed += 1
            print(f"[{mid}] ✅ 被杀 (含预期 {hit}): {desc}")

    print("=" * 72)
    print(f"变异结果: {killed} 杀 / {len(survived)} 存活 / {len(skipped)} 跳过 "
          f"(共 {len(MUTATIONS)})")
    for tag, rows in (("存活", survived), ("跳过(变异没打上,不算证据)", skipped)):
        if rows:
            print(f"{tag}:")
            for mid, desc, why in rows:
                print(f"  - {mid} [{why}] {desc}")
    return 1 if (survived or skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
