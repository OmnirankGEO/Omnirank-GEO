"""
AI-fill billing paths smoke · CTO-15.16 round2 P0

不真扣费 · 只校验 3 路径所需的"配置"全部到位:
  · DB feature_pricing.autofill_brand 行存在 + cost_points=130
  · api/profile_api.py 真用 _bill_ctx(request, "autofill_brand")
  · 前端 BatchUpgradeDialog / BrandWizardBanner 真用 featureCode="autofill_brand"
  · 残留错口径 grep = 0

对应 3 路径(Deploy-CTO 部署后用 staging / 真账号跑 e2e · 不在本脚本):
  P1 · 0 余额非 admin 调 /api/profiles/ai-fill
       期望: 402 · point_transactions 无新行 · LLM 不被调用
  P2 · 充足余额非 admin · LLM 成功 · persist 成功
       期望: 200 · response.billed.feature_code='autofill_brand' ·
       point_transactions 出现一条 user_id=X / feature_code='autofill_brand' / amount=-130
  P3 · 充足余额 · LLM 返 "[错误]" 或 JSON parse 失败
       期望: 500 真因暴露 · point_transactions 不出新行(_bill_ctx 异常路径不扣)

附在 result JSON 末尾的 SQL 模板供运维:
  -- 跑前
  SELECT cost_points FROM feature_pricing WHERE feature_code = 'autofill_brand';
  -- 跑 P2 后
  SELECT * FROM point_transactions
   WHERE feature_code = 'autofill_brand' AND created_at > NOW() - INTERVAL '5 minutes'
   ORDER BY created_at DESC LIMIT 5;

跑法:
  python scripts/ai_fill_billing_paths_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
import re

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPECTED_FEATURE_CODE = "autofill_brand"
EXPECTED_COST = 130

CHECKS: list[tuple[str, callable, str]] = []


def check(name: str, hint: str = ""):
    def deco(fn):
        CHECKS.append((name, fn, hint))
        return fn
    return deco


@check("feature_pricing.autofill_brand exists with cost_points=130")
def _check_seed() -> tuple[bool, str]:
    try:
        from db.connection import get_connection
    except Exception as e:
        return False, f"无法 import get_connection: {e}"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT cost_points FROM feature_pricing WHERE feature_code = %s", (EXPECTED_FEATURE_CODE,))
        row = cur.fetchone()
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    if not row:
        return False, "feature_pricing 无 autofill_brand 行 · 跑 scripts/m1m2_db_self_heal.py 触发 seed_feature_pricing"
    cost = row.get("cost_points") if hasattr(row, "get") else row[0]
    if cost != EXPECTED_COST:
        return False, f"cost_points={cost} · 期望 {EXPECTED_COST}"
    return True, f"cost_points={cost}"


@check("api/profile_api.py uses _bill_ctx(request, 'autofill_brand')")
def _check_backend_code() -> tuple[bool, str]:
    p = ROOT / "api" / "profile_api.py"
    src = p.read_text(encoding="utf-8")
    if '_bill_ctx(request, "autofill_brand")' not in src:
        return False, "api/profile_api.py 未找到 _bill_ctx(request, 'autofill_brand')"
    if '_bill_ctx(request, "brand_fill")' in src:
        return False, "api/profile_api.py 仍有 _bill_ctx(request, 'brand_fill') · 应已替换"
    return True, "OK"


@check("frontend uses featureCode='autofill_brand'")
def _check_frontend_code() -> tuple[bool, str]:
    files = [
        ROOT / "frontend" / "src" / "pages" / "Brand" / "BatchUpgradeDialog.tsx",
        ROOT / "frontend" / "src" / "components" / "brand" / "BrandWizardBanner.tsx",
    ]
    for f in files:
        src = f.read_text(encoding="utf-8")
        if 'featureCode="autofill_brand"' not in src:
            return False, f"{f.name} 未含 featureCode=\"autofill_brand\""
        if 'featureCode="brand_fill"' in src:
            return False, f"{f.name} 仍有 featureCode=\"brand_fill\" · 应已替换"
    return True, "OK"


@check("BatchUpgradeDialog uses getCost('autofill_brand')")
def _check_get_cost() -> tuple[bool, str]:
    p = ROOT / "frontend" / "src" / "pages" / "Brand" / "BatchUpgradeDialog.tsx"
    src = p.read_text(encoding="utf-8")
    if "getCost('autofill_brand')" not in src:
        return False, "BatchUpgradeDialog 未 getCost('autofill_brand')"
    return True, "OK"


@check("BrandWizardBanner toast not hardcoded 130")
def _check_toast_no_hardcode() -> tuple[bool, str]:
    p = ROOT / "frontend" / "src" / "components" / "brand" / "BrandWizardBanner.tsx"
    src = p.read_text(encoding="utf-8")
    # toast 文案里出现 "130 积分" 算硬编码失败(允许在注释/文档解释)
    # 只看 toast.success / toast.info / toast.error 直接调用
    pattern = re.compile(r"toast\.\w+\([^)]*130[^)]*\)")
    if pattern.search(src):
        return False, "BrandWizardBanner toast 仍含 130 硬编码 · 改成动态价或中性文案"
    return True, "OK"


@check("no wrong-code residue grep")
def _check_no_residue() -> tuple[bool, str]:
    """grep · brand_fill.*130 / 130.*brand_fill / 已扣 130 / autofill_brand.*无此 seed
    本脚本只检查 M1c+M2 territory 改动文件 · 不动 content_api.py 等历史
    """
    targets = [
        ROOT / "api" / "profile_api.py",
        ROOT / "db" / "wallet_db.py",
        ROOT / "frontend" / "src" / "components" / "brand" / "BrandWizardBanner.tsx",
        ROOT / "frontend" / "src" / "pages" / "Brand" / "BatchUpgradeDialog.tsx",
        ROOT / "scripts" / "m1m2_db_self_heal.py",
    ]
    bad_patterns = [
        re.compile(r"brand_fill[^a-zA-Z_].*130"),
        re.compile(r"130.*brand_fill"),
        re.compile(r"已扣 130"),
        re.compile(r"autofill_brand.*无此 seed"),
    ]
    hits: list[str] = []
    for f in targets:
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8")
        for line_no, ln in enumerate(src.splitlines(), 1):
            for pat in bad_patterns:
                if pat.search(ln):
                    hits.append(f"{f.relative_to(ROOT)}:{line_no}: {ln.strip()[:100]}")
    if hits:
        return False, "残留:\n  " + "\n  ".join(hits)
    return True, "0 残留"


def main() -> int:
    print("[ai-fill-billing-paths-smoke] 配置一致性检查 · 不真扣费\n")
    results: list[dict] = []
    all_ok = True
    for name, fn, hint in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"check 抛异常: {e}"
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}: {detail}")
        if hint and not ok:
            print(f"     └ 提示: {hint}")
        results.append({"check": name, "pass": ok, "detail": detail})
        if not ok:
            all_ok = False

    print()
    print("──── 真实 e2e 待 Deploy-CTO 部署后跑(本脚本不调 LLM 不动钱包)────")
    print(
        "  P1 · 0 余额非 admin POST /api/profiles/ai-fill {brand_id, persist:true}\n"
        "       期望: 402 · LLM 不调 · point_transactions 0 行新增"
    )
    print(
        "  P2 · 充足余额 + LLM 成功 · POST 同上\n"
        "       期望: 200 · response.billed.feature_code='autofill_brand' ·\n"
        "       point_transactions 出现 -130 · feature_code='autofill_brand'"
    )
    print(
        "  P3 · 充足余额 · LLM 返 '[错误]' / JSON parse 失败 · POST 同上\n"
        "       期望: 500 真因暴露 · point_transactions 不出新行"
    )
    print()
    print("SQL 模板:")
    print("  -- 跑前 baseline")
    print("  SELECT user_id, balance FROM user_wallets WHERE user_id = <test_user_id>;")
    print(f"  SELECT cost_points FROM feature_pricing WHERE feature_code = '{EXPECTED_FEATURE_CODE}';")
    print("  -- 跑 P2 后")
    print(
        f"  SELECT * FROM point_transactions WHERE feature_code = '{EXPECTED_FEATURE_CODE}' "
        "AND user_id = <test_user_id> ORDER BY created_at DESC LIMIT 5;"
    )

    out_path = Path(__file__).parent / "ai_fill_billing_paths_smoke_result.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_scope": "config consistency check · no real billing called",
        "feature_code": EXPECTED_FEATURE_CODE,
        "expected_cost_points": EXPECTED_COST,
        "checks": results,
        "all_pass": all_ok,
        "e2e_paths_for_deploy_cto": [
            {"id": "P1-no-balance", "expect_status": 402, "expect_ledger_delta": 0, "expect_llm_called": False},
            {"id": "P2-success", "expect_status": 200, "expect_ledger_delta": -130, "expect_llm_called": True},
            {"id": "P3-llm-fail", "expect_status": 500, "expect_ledger_delta": 0, "expect_llm_called": True},
        ],
    }
    out_path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    print(f"\n[ai-fill-billing-paths-smoke] JSON 已写: {out_path}")

    if not all_ok:
        print("\n❌ 配置一致性检查 fail · 上面 ❌ 项必须修后才能合 main")
        return 1
    print("\n✅ 配置一致性 6/6 PASS · e2e 3 路径待 Deploy-CTO staging 验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
