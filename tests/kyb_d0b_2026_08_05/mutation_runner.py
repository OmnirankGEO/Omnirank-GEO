"""[WO-KYB D0-b] 变异自检 —— 严格全红。

每条变异期望转红的用例**必须全部转红**,少一条即判未被有效杀死。
三关:锚点恰好命中 1 次 / 文件字节真变了 / 期望的每一条都转红。

    python -X utf8 tests/kyb_d0b_2026_08_05/mutation_runner.py
"""
import io
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

PROJ = os.path.join("services", "media_price_projection.py")
MHZ = os.path.join("api", "meijiehezi_api.py")
PUB = os.path.join("api", "publish_api.py")
FE = os.path.join("frontend", "src", "pages", "Publishing", "PublishCenter.tsx")

MUTATIONS = [
    dict(id="M1", file=PROJ, desc="不删成本侧字段(进货价原样返回=整个包白做)",
         edits=[("            for f in COST_SIDE_FIELDS:\n                row.pop(f, None)\n        out.append(row)",
                 "        out.append(row)")],
         expect_red=["test_all_cost_side_fields_are_stripped",
                     "test_sweet_spot_computed_before_price_is_stripped"]),
    dict(id="M2", file=PROJ, desc="回落到进货价时漏乘 markup(售价少收 markup 倍)",
         edits=[("    return yuan_to_points(row.get(\"price\"), markup)",
                 "    return yuan_to_points(row.get(\"price\"), 1.0)")],
         expect_red=["test_cost_fallback_multiplies_markup", "test_price_points_added"]),
    dict(id="M3", file=PROJ, desc="ceil 改成 round(与服务端扣费口径错开)",
         edits=[("    return int(math.ceil(v)) if v > 0 else 0",
                 "    return int(round(v)) if v > 0 else 0")],
         expect_red=["test_points_formula_matches_charge_formula"]),
    dict(id="M4", file=PROJ, desc="COST_SIDE_FIELDS 漏一个 price1(那一列继续外露)",
         edits=[('    "price", "price1", "price2",', '    "price", "price2",')],
         expect_red=["test_all_cost_side_fields_are_stripped",
                     "test_strip_list_covers_every_cost_column_in_the_whitelists"]),
    dict(id="M5", file=PROJ, desc="性价比判据挪到 pop 之后(徽章全站消失且不报错)",
         edits=[('            row["price_points"] = resolve_price_points(row, markup)\n            row["is_sweet_spot"] = is_sweet_spot(row)\n            for f in COST_SIDE_FIELDS:\n                row.pop(f, None)',
                 '            row["price_points"] = resolve_price_points(row, markup)\n            for f in COST_SIDE_FIELDS:\n                row.pop(f, None)\n            row["is_sweet_spot"] = is_sweet_spot(row)')],
         expect_red=["test_sweet_spot_computed_before_price_is_stripped"]),
    dict(id="M6", file=PROJ, desc="性价比区间上界改成开区间(¥60 那档掉出来)",
         edits=[("    return lo <= p <= hi", "    return lo <= p < hi")],
         expect_red=["test_sweet_spot_boundaries_inclusive"]),
    dict(id="M7", file=PROJ, desc="markup=0 时不再保护,除零",
         edits=[("    return (_num(points, 0.0) / denom) if denom > 0 else 0.0",
                 "    return _num(points, 0.0) / denom")],
         expect_red=["test_points_to_yuan_zero_markup_is_safe"]),
    dict(id="M8", file=PROJ, desc="已有售价算力时仍拿进货价重算(重复加价)",
         edits=[("    for f in _SELL_SIDE_POINTS_FIELDS:\n        v = _num(row.get(f), 0.0)\n        if v > 0:\n            return int(round(v))",
                 "    for f in _SELL_SIDE_POINTS_FIELDS:\n        v = _num(row.get(f), 0.0)\n        if v > 0 and False:\n            return int(round(v))")],
         expect_red=["test_existing_sell_points_win", "test_row_with_only_sell_fields_still_gets_points"]),
    dict(id="M9", file=PROJ, desc="递归投影只按 price 判媒体行(只有 our_price_* 的行漏掉)",
         edits=[("        looks_like_media = any(f in obj for f in COST_SIDE_FIELDS) or any(\n            f in obj for f in (_SELL_SIDE_POINTS_FIELDS + _SELL_SIDE_YUAN_FIELDS)\n        )",
                 "        looks_like_media = \"price\" in obj")],
         expect_red=["test_row_with_only_sell_fields_still_gets_points"]),

    dict(id="M10", file=MHZ, desc="软文目录端点不投影(进货价直出)",
         edits=[('    return {"status": "success", **_project_catalog(result, _markup)}\n\n\n@router.get("/media/filters")',
                 '    return {"status": "success", **result}\n\n\n@router.get("/media/filters")')],
         expect_red=["test_catalog_endpoint_projects"]),
    dict(id="M11", file=MHZ, desc="/markup 摘掉 admin 闸(加价率重新公开)",
         edits=["    user = _get_user(request)\n    _require_admin(user)\n    ratio = get_config",
                "    user = _get_user(request)\n    ratio = get_config"],
         expect_red=["test_markup_endpoint_is_admin_only"]),
    dict(id="M12", file=MHZ, desc="元口径筛选参数复活(二分 price_min 可试出进货价)",
         edits=["    sort_by: str = \"price_points\", sort_dir: str = \"asc\",\n    points_min: int = 0, points_max: int = 0,\n    geo_platform: str = Query(",
                "    sort_by: str = \"price_points\", sort_dir: str = \"asc\",\n    points_min: int = 0, points_max: int = 0, price_min: float = 0, price_max: float = 0,\n    geo_platform: str = Query("],
         expect_red=["test_yuan_filters_removed_from_signature"]),
    dict(id="M13", file=MHZ, desc="排序键不再映射(对外键直接进 SQL)",
         edits=["        _public_sort_key(sort_by), sort_dir,\n        price_min=_points_to_yuan(points_min, _markup),\n        price_max=_points_to_yuan(points_max, _markup),\n        portal_media=portal_media",
                "        sort_by, sort_dir,\n        price_min=_points_to_yuan(points_min, _markup),\n        price_max=_points_to_yuan(points_max, _markup),\n        portal_media=portal_media"],
         expect_red=["test_sort_key_is_mapped"]),
    dict(id="M14", file=MHZ, desc="死端点的 import 复活(留下孤儿符号)",
         edits=["    create_order, get_order_items_by_order, list_orders,",
                "    create_order, get_order_items_by_order, get_publish_order_owner, list_orders,"],
         expect_red=["test_dead_order_items_endpoint_removed"]),

    dict(id="M15", file=PUB, desc="AI 推荐链不投影(设计稿漏掉的那条泄漏复活)",
         edits=["    from services.media_price_projection import get_markup, project_payload\n    payload = project_payload(payload, get_markup())\n",
                ""],
         expect_red=["test_recommendation_payload_is_projected"]),

    dict(id="M16", file=FE, desc="前端总价改回 ceil(Σ)(与服务端逐条 ceil 再求和对不上)",
         edits=["  const mhzTotalPoints = mhzSelList.reduce((s, m) => s + (m.price_points || 0), 0);",
                "  const mhzTotalYuan = 0;\n  const mhzTotalPoints = Math.ceil(mhzTotalYuan * 1.5 * POINTS_PER_YUAN);"],
         expect_red=["test_frontend_totals_sum_per_item_points"]),
    dict(id="M17", file=FE, desc="前端重新自己算价(markup 又回到浏览器)",
         edits=["  const mhzSelList = Array.from(mhzSelMedia).map(id => mhzMediaMap.get(id)).filter(Boolean) as MhzMediaItem[];",
                "  const yuanToPoints = (yuan: number) => Math.ceil(yuan * 1.5 * POINTS_PER_YUAN);\n  const mhzSelList = Array.from(mhzSelMedia).map(id => mhzMediaMap.get(id)).filter(Boolean) as MhzMediaItem[];"],
         expect_red=["test_frontend_no_longer_computes_points"]),
]


def _run_tests():
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest",
         os.path.join("tests", "kyb_d0b_2026_08_05"), "-q", "-rfE"],
        cwd=_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ,
             "TEST_DATABASE_URL": os.environ.get(
                 "TEST_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/omnirank_test"),
             "PYTHONIOENCODING": "utf-8"},
    )
    red = set()
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        # 单个 -rfE:FAILED 与 ERROR 都打。写成 "-rf","-rE" 会后者覆盖前者 → 红集恒空。
        if s.startswith("FAILED ") or s.startswith("ERROR "):
            red.add(s.split("::")[-1].split()[0].split("[")[0])
    return red, r.returncode


def main() -> int:
    files = sorted({m["file"] for m in MUTATIONS})
    backup = {f: io.open(os.path.join(_ROOT, f), encoding="utf-8", newline="").read()
              for f in files}

    base_red, base_rc = _run_tests()
    if base_rc != 0:
        print(f"🔴 基线就不绿:{sorted(base_red)}")
        return 1
    print(f"基线:✅ 绿({len(MUTATIONS)} 条变异待跑)\n")

    survivors = []
    for mut in MUTATIONS:
        path = os.path.join(_ROOT, mut["file"])
        orig = backup[mut["file"]]
        src, ok = orig, True
        pairs = [mut["edits"]] if isinstance(mut["edits"][0], str) else mut["edits"]
        for anchor, repl in pairs:
            n = src.count(anchor)
            if n != 1:
                print(f"{mut['id']} 🔴 锚点命中 {n} 次(应=1),变异作废 —— {mut['desc']}")
                ok = False
                break
            src = src.replace(anchor, repl, 1)
        if not ok or src == orig:
            if ok:
                print(f"{mut['id']} 🔴 文件没变,变异无效")
            survivors.append(mut["id"])
            continue

        io.open(path, "w", encoding="utf-8", newline="").write(src)
        try:
            red, _ = _run_tests()
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(orig)

        missing = set(mut["expect_red"]) - red
        if missing:
            print(f"{mut['id']} 🔴 没杀 —— {mut['desc']}\n      期望转红但没红:{sorted(missing)}")
            survivors.append(mut["id"])
        else:
            print(f"{mut['id']} ✅ 转红 {sorted(mut['expect_red'])}  —— {mut['desc']}")

    for f, s in backup.items():
        io.open(os.path.join(_ROOT, f), "w", encoding="utf-8", newline="").write(s)
    _, rc = _run_tests()
    print(f"\n还原后基线:{'✅ 绿' if rc == 0 else '🔴 红(还原有问题)'}")
    print(f"变异 {len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} 被杀")
    return 0 if (not survivors and rc == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
