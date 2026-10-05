"""[FIND-MHZ-ORDERITEMS-2026-08-04] 变异自检(多文件)。

三关缺一不可:锚点恰好命中 1 次 / 文件字节真的变了 / **期望的每一条都转红**(严格全红)。

🔴 用 read_bytes/write_bytes —— text 模式在 Windows 上每跑一次翻一次 CRLF。
🔴 变异跑期间禁止任何 git 写操作;跑完必须 `git diff` 逐段复核。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TESTS = os.path.join("tests", "order_items_idor_2026_08_04")

API = os.path.join("api", "meijiehezi_api.py")
DB = os.path.join("db", "meijiehezi_db.py")
MAT = os.path.join("api", "material_api.py")
SCAN = os.path.join("tests", "order_items_idor_2026_08_04", "impostor_guard_scan.py")

ENV = {
    **os.environ,
    "TEST_DATABASE_URL": "postgresql://u:p@127.0.0.1:5432/geo_test",
    "PYTHONIOENCODING": "utf-8",
}

FAKE_HELPER = (
    'def _get_profile_safe(request, profile_id):\n'
    '    """获取 profile（宽松版，不做 brand 权限校验）"""\n'
    '    from db.profile_db import get_profile\n'
    '    profile = get_profile(profile_id)\n'
    '    if not profile:\n'
    '        raise HTTPException(status_code=404, detail="档案不存在")\n'
    '    return profile\n\n\n'
)

# [WO-KYB D0-b 2026-08-05] 原 M1-M5 打在 `GET /orders/{order_id}/items` 的归属守卫上。
# 该端点前端零调用、Owner 2026-08-05 拍板删除 → 五条变异失去锚点,一并移除,
# 换成下面这条 M1:钉住"端点必须保持已删除"。
# 🔴 若将来把端点加回来,必须同时恢复归属守卫 + 那 7 条端点级用例 + 这五条变异。
MUTATIONS = [
    {"id": "M1", "file": API, "desc": "死端点被加回来(且没带归属守卫)",
     "edits": [('@router.get("/mhz-orders")',
                '@router.get("/orders/{order_id}/items")\n'
                'async def api_order_items(order_id: int, request: Request):\n'
                '    _get_user(request)\n'
                '    return {"status": "success", "items": []}\n\n\n'
                '@router.get("/mhz-orders")')],
     "expect_red": ["test_dead_endpoint_stays_deleted"]},

    {"id": "M6", "file": DB, "desc": "归属改读 items 上的冗余副本,不读订单主表",
     "edits": [('        c.execute("SELECT user_id FROM mhz_publish_orders WHERE id = %s", (oid,))',
                '        c.execute("SELECT user_id FROM mhz_publish_order_items WHERE order_id = %s LIMIT 1", (oid,))')],
     "expect_red": ["test_ownership_read_from_orders_table"]},

    {"id": "M7", "file": DB, "desc": "订单不存在时返 0 而不是 None(0 会被当成合法 user_id)",
     "edits": [('        if not row or row.get("user_id") is None:\n            return None',
                '        if not row or row.get("user_id") is None:\n            return 0')],
     "expect_red": ["test_owner_helper_returns_none_for_missing"]},

    {"id": "M8", "file": MAT, "desc": "本地假守卫 _get_profile_safe 又被加回来",
     "edits": [('@router.get("/{material_id}", summary="获取单个素材")',
                FAKE_HELPER + '@router.get("/{material_id}", summary="获取单个素材")')],
     "expect_red": ["test_local_impostor_helper_is_gone", "test_no_impostor_guards_in_api_tree"]},

    {"id": "M9", "file": MAT, "desc": "素材详情端点的守卫被摘掉(调用点少一个)",
     "edits": [('            if m.get("profile_id"):\n                require_profile_access(request, m["profile_id"])\n            return {"success": True, "material": m}',
                '            return {"success": True, "material": m}')],
     "expect_red": ["test_calls_migrated_to_canonical_guard"]},

    {"id": "M10", "file": SCAN, "desc": "原语名单漏登记 require_quote_access(真守卫会被误报)",
     "edits": [('    "require_report_access", "require_quote_access",', '    "require_report_access",')],
     "expect_red": ["test_primitive_list_covers_all_require_helpers", "test_no_impostor_guards_in_api_tree"]},

    {"id": "M11", "file": SCAN, "desc": "扫描器退回一层判定(不再跨函数追踪 → 噪声淹没真信号)",
     "edits": [('    for nm in _called_names(fn):\n        callee = local_defs.get(nm)\n'
                '        if callee is not None and reaches_authz(callee, local_defs, src, seen, depth + 1):\n'
                '            return True\n    return False',
                '    return False')],
     "expect_red": ["test_no_impostor_guards_in_api_tree"]},
]


def run_tests():
    p = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", TESTS, "-q", "--tb=no", "-rf"],
        cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return p.returncode, set(re.findall(r"FAILED\s+\S+::([\w]+)", p.stdout)), p.stdout


def main():
    files = {rel: open(os.path.join(ROOT, rel), "rb").read()
             for rel in {m["file"] for m in MUTATIONS}}

    rc, _f, out = run_tests()
    if rc != 0:
        print("🔴 基线就不是绿的,变异结果无意义\n" + out[-2500:])
        return 1
    print(f"基线 ✅ 全绿(涉及 {len(files)} 个文件)\n")

    bad = []
    for mut in MUTATIONS:
        rel = mut["file"]
        path = os.path.join(ROOT, rel)
        original = files[rel]
        src = original.decode("utf-8")
        ok = True
        for anchor, repl in mut["edits"]:
            n = src.count(anchor)
            if n != 1:
                print(f"🔴 {mut['id']} 锚点命中 {n} 次(必须恰好 1 次)")
                ok = False
                break
            src = src.replace(anchor, repl, 1)
        if not ok:
            bad.append(mut["id"])
            continue
        mutated = src.encode("utf-8")
        if mutated == original:
            print(f"🔴 {mut['id']} 文件没变 → no-op 变异")
            bad.append(mut["id"])
            continue

        open(path, "wb").write(mutated)
        try:
            _rc, failed, _o = run_tests()
        finally:
            open(path, "wb").write(original)

        want = set(mut["expect_red"])
        missing = want - failed
        if not missing:
            print(f"{mut['id']} ✅ 转红 {sorted(want)}  —— {mut['desc']}")
        else:
            print(f"{mut['id']} 🔴 期望未全红!没红={sorted(missing)} 实际红={sorted(failed) or '无'}"
                  f"  —— {mut['desc']}")
            bad.append(mut["id"])

    for rel, original in files.items():
        assert open(os.path.join(ROOT, rel), "rb").read() == original, f"🔴 {rel} 没还原干净"
    rc2, _f2, _o2 = run_tests()
    print(f"\n还原后基线:{'✅ 绿' if rc2 == 0 else '🔴 红'}")
    print(f"变异 {len(MUTATIONS) - len(bad)}/{len(MUTATIONS)} 被杀")
    if bad:
        print("未被杀:", bad)
    return 0 if (not bad and rc2 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
