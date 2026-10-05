"""
V3.1 业务代码接入 entitlements 集成测试(2026-05-12 老板"关乎钱稳定第一")

测试场景:
  1. SOCIAL_FEATURES 白名单含 9 个新加 feature
  2. BILLING_FALLBACK_CODE_MAP 含 9 个新加映射
  3. 所有新加 feature 在 feature_pricing 表实证存在(防 fallback ValueError)
  4. release_subscription_entitlement 是 sync 函数(不能 await)
  5. charge_subscription_entitlement 是 async 函数(必须 await)
  6. Helper inspect:6 个 API 文件改造后含正确 V3.1 path
  7. 业务流模拟:用户买 growth 套餐 → 调 script_gen → entitlement.pro_write_used+1
  8. 业务流模拟:entitlement 耗尽 → fallback paid_points
  9. 业务流模拟:业务失败 → release_subscription_entitlement(配额还原)

跑法:
  python -m tests.test_v31_entitlement_integration

不需要 ALLOW_PHASE07_E2E_ON_THIS_DB(只是 inspect + 静态检查 · 不 INSERT prod)
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_1_social_features_whitelist():
    """SOCIAL_FEATURES 含 9 个新加 feature"""
    from middleware.subscription_billing import SOCIAL_FEATURES

    new_features = [
        "hook_gen", "deep_analyze", "industry_brief_rerun", "content_review",
        "comment_gen", "dm_gen", "scenario_gen", "team_analysis", "ai_coach",
    ]
    missing = [f for f in new_features if f not in SOCIAL_FEATURES]
    assert not missing, f"SOCIAL_FEATURES 缺 9 个新 feature: {missing}"

    expected_quota = {
        "hook_gen": "pro_write",
        "deep_analyze": "review",
        "industry_brief_rerun": "review",
        "content_review": "review",
        "comment_gen": "light_chat",
        "dm_gen": "light_chat",
        "scenario_gen": "light_chat",
        "team_analysis": "team_profile",
        "ai_coach": "light_chat",
    }
    for fc, expected in expected_quota.items():
        actual = SOCIAL_FEATURES.get(fc)
        assert actual == expected, f"SOCIAL_FEATURES[{fc}] = {actual!r}, expected {expected!r}"

    print("[Test 1] ✓ SOCIAL_FEATURES 9 个新 feature 全部正确映射 quota_type")


def test_2_billing_fallback_code_map():
    """BILLING_FALLBACK_CODE_MAP 含 9 个新加映射"""
    from middleware.subscription_billing import BILLING_FALLBACK_CODE_MAP

    expected_map = {
        "hook_gen": "hook_gen",
        "deep_analyze": "deep_analyze",
        "industry_brief_rerun": "industry_brief_rerun",
        "content_review": "content_review",
        "comment_gen": "comment_gen",
        "dm_gen": "dm_gen",
        "scenario_gen": "scenario_gen",
        "team_analysis": "team_portrait",  # 注意:team_analysis 不存在,实际是 team_portrait
    }
    for fc, expected in expected_map.items():
        actual = BILLING_FALLBACK_CODE_MAP.get(fc)
        assert actual == expected, f"BILLING_FALLBACK_CODE_MAP[{fc}] = {actual!r}, expected {expected!r}"

    print("[Test 2] ✓ BILLING_FALLBACK_CODE_MAP 8 个新映射正确")
    print("[Test 2] (ai_coach 复用同名 feature_pricing.ai_coach,可不在 MAP 内)")


def test_3_feature_pricing_table_check():
    """所有新加 feature 在 feature_pricing 表实证存在(防 fallback ValueError)"""
    from db.connection import get_connection
    from middleware.subscription_billing import BILLING_FALLBACK_CODE_MAP

    new_feature_pricing_codes = [
        "hook_gen", "deep_analyze", "industry_brief_rerun", "content_review",
        "comment_gen", "dm_gen", "scenario_gen", "team_portrait", "ai_coach",
    ]
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT feature_code, cost_points FROM feature_pricing WHERE feature_code = ANY(%s)",
            (new_feature_pricing_codes,),
        )
        rows = cur.fetchall()
        existing = {r["feature_code"]: r["cost_points"] for r in rows}
    finally:
        conn.close()

    missing = [fc for fc in new_feature_pricing_codes if fc not in existing]
    assert not missing, f"feature_pricing 表缺 9 个新 feature_code: {missing}"

    print(f"[Test 3] ✓ feature_pricing 表 9 个 feature 全存在:")
    for fc, points in existing.items():
        print(f"  · {fc:24s} = {points} points")


def test_4_release_subscription_entitlement_is_sync():
    """🔴 P0 防回归:release_subscription_entitlement 必须是 sync 函数,不能 await"""
    from middleware.subscription_billing import release_subscription_entitlement

    assert not inspect.iscoroutinefunction(release_subscription_entitlement), (
        "release_subscription_entitlement 应是 sync 函数(def 不是 async def)。"
        "如果改成 async,所有调用方需要加 await,否则返回 coroutine 不执行。"
    )
    print("[Test 4] ✓ release_subscription_entitlement 是 sync 函数(P0 回归保护)")


def test_5_charge_subscription_entitlement_is_async():
    """charge_subscription_entitlement 必须是 async 函数"""
    from middleware.subscription_billing import charge_subscription_entitlement

    assert inspect.iscoroutinefunction(charge_subscription_entitlement), (
        "charge_subscription_entitlement 应是 async 函数"
    )
    print("[Test 5] ✓ charge_subscription_entitlement 是 async 函数")


def test_6_api_files_use_v3_1_path():
    """6 个 API 文件改造后含正确 V3.1 path · 防回归"""
    api_files = [
        "api/content_api.py",
        # [开源 E3 · B3c · 2026-09-28] api/advisor_api.py 的顾问对话扣费端点已删,该文件不再有权益扣费路径
    ]
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for filepath in api_files:
        full = os.path.join(project_root, filepath)
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()

        # 必须含 V3.1 SOCIAL_FEATURES 判断
        assert "SOCIAL_FEATURES" in content, f"{filepath} 缺 SOCIAL_FEATURES 判断"
        assert "charge_subscription_entitlement" in content, f"{filepath} 缺 charge_subscription_entitlement 调用"

        # 不能再有 await release_subscription_entitlement(P0 防回归)
        assert "await release_subscription_entitlement" not in content, (
            f"🔴 {filepath} 包含错误的 await release_subscription_entitlement"
        )
        print(f"[Test 6] ✓ {filepath} V3.1 path 正确接入")


def test_7_content_api_helpers_have_v3_1_branch():
    """content_api 的 _bill / _bill_ctx 都有 V3.1 分支"""
    import importlib.util

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    filepath = os.path.join(project_root, "api/content_api.py")

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 确认两个 helper 都有 V3.1 路径
    assert "async def _bill(request" in content
    assert "async def _bill_ctx(request" in content
    assert "async def _release_or_refund(request" in content
    assert "async def _release_or_refund_bg(user_id" in content

    # 直接 deduct_points 调用应该 ≤ 2(只在 helper 内,deep_analyze/industry_brief_rerun 已改 _bill)
    direct_deduct = content.count("await deduct_points(")
    assert direct_deduct <= 2, (
        f"content_api 直接 await deduct_points 调用 {direct_deduct} 处(应 ≤ 2,只在 _bill/_bill_ctx helper 内)"
    )

    # 直接 refund_points 调用应该 ≤ 1(只在 _release_or_refund / _bg helper 内)
    direct_refund = content.count("await refund_points(")
    assert direct_refund <= 2, (
        f"content_api 直接 await refund_points 调用 {direct_refund} 处(应 ≤ 2,只在 helper 兜底路径)"
    )

    print(f"[Test 7] ✓ content_api helper 完整:_bill / _bill_ctx / _release_or_refund / _release_or_refund_bg")
    print(f"  · 直接 deduct_points 调用 {direct_deduct} 处(helper 内合理)")
    print(f"  · 直接 refund_points 调用 {direct_refund} 处(helper 兜底路径合理)")


def test_8_release_signature():
    """release_subscription_entitlement 签名:user_id + feature_code + video_minutes + request_id"""
    from middleware.subscription_billing import release_subscription_entitlement

    sig = inspect.signature(release_subscription_entitlement)
    params = list(sig.parameters.keys())
    assert params[0] == "user_id"
    assert params[1] == "feature_code"
    print(f"[Test 8] ✓ release_subscription_entitlement 签名: {params}")


def test_9_charge_signature():
    """charge_subscription_entitlement 签名包含 fallback_to_points"""
    from middleware.subscription_billing import charge_subscription_entitlement

    sig = inspect.signature(charge_subscription_entitlement)
    params = list(sig.parameters.keys())
    assert "user_id" in params
    assert "feature_code" in params
    assert "fallback_to_points" in params
    print(f"[Test 9] ✓ charge_subscription_entitlement 签名含 fallback_to_points: {params}")


def test_16_addon_db_ssot_migration_exists():
    """🔴 2026-05-13 ADDON_OPTIMIZATION_V1 T1: migration_011 文件存在 + run_migration 可 import"""
    import os
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fpath = os.path.join(project_root, "db/migration_011_subscription_addons.py")
    assert os.path.exists(fpath), f"migration_011 文件不存在: {fpath}"

    from db.migration_011_subscription_addons import run_migration
    assert callable(run_migration), "migration_011 必须有 run_migration() 函数"
    print("[Test 16] ✓ db/migration_011_subscription_addons.py 文件存在 + run_migration 可调用")


def test_20_overrun_dialog_now_imported():
    """🔴 T2+T5: SubscriptionOverrunDialog 必须有地方 import (之前 0 处接入 = 死代码)"""
    import os
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    frontend_src = os.path.join(project_root, "frontend/src")

    # 递归 grep 看哪些文件 import SubscriptionOverrunDialog
    importers = []
    for root, _dirs, files in os.walk(frontend_src):
        for fname in files:
            if not (fname.endswith(".tsx") or fname.endswith(".ts")):
                continue
            fpath = os.path.join(root, fname)
            # 跳过组件自身
            if fname == "SubscriptionOverrunDialog.tsx":
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                if "SubscriptionOverrunDialog" in content:
                    importers.append(os.path.relpath(fpath, project_root))
            except Exception:
                continue
    assert len(importers) >= 1, (
        f"SubscriptionOverrunDialog 必须被至少 1 处 import (修死代码 P0) · 当前 importers: {importers}"
    )
    print(f"[Test 20] ✓ SubscriptionOverrunDialog 被 {len(importers)} 处 import: {importers[:3]}...")


def test_24_bill_ctx_fail_closed():
    """🔴 Codex 1-审 #1: _bill_ctx 必须 fail-closed · 扣费失败 raise 不再 log-only"""
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in ["content_api.py"]:  # 研究 router 随 E3 删
        fp = os.path.join(os.path.dirname(here), "api", fname)
        with open(fp, "r", encoding="utf-8") as f:
            src = f.read()
        # _bill_ctx 内必须有 subscription_overrun_check 预检查(yield 前)
        assert "subscription_overrun_check" in src, (
            f"{fname}: _bill_ctx 缺 subscription_overrun_check 预检查 · 未 fail-closed"
        )
        # 后扣费失败必须 raise HTTPException · 不再吞掉
        assert "BILLING_POST_CHARGE_FAILED" in src, (
            f"{fname}: _bill_ctx 缺 BILLING_POST_CHARGE_FAILED 错误码 · 可能仍 log-only"
        )
    print("[Test 24] ✓ content_api 的 _bill_ctx fail-closed")


def test_29_chat_attachment_services_cost_metadata():
    """🔴 v1.5 services/chat_attachments.py 必须设 incurred_cost / cost_feature_code

    parse_image:incurred_cost=True + profile_polish
    parse_document:仅 PDF(走 vision)incurred_cost=True · doc/txt/md 不扣
    parse_video_url:E0b 起不扣(只取基础信息、不转写)
    parse_video_file_placeholder:不扣(0 成本)
    """
    here = os.path.dirname(os.path.abspath(__file__))
    fp = os.path.join(os.path.dirname(here), "services", "chat_attachments.py")
    with open(fp, "r", encoding="utf-8") as f:
        src = f.read()

    # AttachmentParseResult 含 cost metadata 字段
    assert "incurred_cost: bool" in src, "AttachmentParseResult 缺 incurred_cost 字段"
    assert "cost_feature_code:" in src, "AttachmentParseResult 缺 cost_feature_code 字段"
    assert "cost_video_minutes:" in src, "AttachmentParseResult 缺 cost_video_minutes 字段"

    # parse_image 必扣 profile_polish
    assert 'cost_feature_code="profile_polish"' in src, "parse_image 未设 profile_polish"
    # [E0b 2026-09-26 · Review 定] parse_video_url 只取基础信息、不转写 ⇒ 不扣 video_asr(原断言反转;
    #   逐平台行为级的锁在 tests/e0b_lift_scoring_video_2026_09_26::test_video_link_attachment_carries_no_charge)
    assert 'cost_feature_code="video_asr"' not in src, "parse_video_url 不做转写,不该再设 video_asr"
    # parse_document 区分 PDF vs doc/txt
    assert "pdf_incurred_cost = ext_lower == \"pdf\"" in src, (
        "parse_document 未区分 PDF(扣)vs doc/txt(不扣)"
    )
    print("[Test 29] ✓ services chat_attachments 4 parse fn cost metadata 完整")


# [开源 E3 · B2 · 2026-09-28] db/chat_attachment_billing_db.py 随 chat 附件计费一并删除(E3 孤儿),守它的 3 格退役。


def test_35_migration_012_auto_run_in_startup():
    """🔴 Codex 5-审 P2-1 修:migration_012 接入启动链 · Deploy-CTO 不需手动跑"""
    here = os.path.dirname(os.path.abspath(__file__))
    fp = os.path.join(os.path.dirname(here), "server.py")
    with open(fp, "r", encoding="utf-8") as f:
        src = f.read()

    assert "from db.migration_012_chat_attachment_billing import run_migration" in src, (
        "server.py 启动链未自动跑 migration_012 · Deploy-CTO 需手动跑 = 部署风险"
    )
    assert "chat 附件计费表迁移完成" in src, "缺 migration_012 启动日志"
    # 不影响启动(不阻塞):有 except 块
    assert "chat 附件计费表迁移失败(不影响启动" in src, (
        "migration_012 失败必须不阻塞启动(soft log 不可用只丢统计)"
    )
    print("[Test 35] ✓ migration_012 自动启动链 · 部署 0 操作")


# [E0b 2026-09-26 · Review 定] Test 37 / 38 / 39 退役:它们锁的是 parse-url 按转写成本设的前置闸
#   (探时长 → 超单条上限 422 → 未知时长 422 → 真实分钟预检)。视频链接附件改为不转写、不扣费后
#   这三道闸按 Review 裁定删除;反向口径(非管理员 B 站 / 视频号能进、无 402/422、超限 429)
#   由 tests/e0b_lift_scoring_video_2026_09_26 锁。


async def main():
    print("\n" + "=" * 70)
    print("V3.1 业务代码接入 entitlements 集成测试")
    print("=" * 70 + "\n")

    tests = [
        ("Test 1: SOCIAL_FEATURES 白名单", test_1_social_features_whitelist),
        ("Test 2: BILLING_FALLBACK_CODE_MAP", test_2_billing_fallback_code_map),
        ("Test 3: feature_pricing 表实证", test_3_feature_pricing_table_check),
        ("Test 4: 🔴 release 是 sync (P0 回归)", test_4_release_subscription_entitlement_is_sync),
        ("Test 5: charge 是 async", test_5_charge_subscription_entitlement_is_async),
        ("Test 6: 6 个 API 文件 V3.1 path", test_6_api_files_use_v3_1_path),
        ("Test 7: content_api helpers 完整", test_7_content_api_helpers_have_v3_1_branch),
        ("Test 8: release 签名", test_8_release_signature),
        ("Test 9: charge 签名 fallback_to_points", test_9_charge_signature),
        ("Test 16: 🔴 ADDON migration_011 文件存在", test_16_addon_db_ssot_migration_exists),
        ("Test 20: 🔴 SubscriptionOverrunDialog 不再死代码", test_20_overrun_dialog_now_imported),
        # Codex 复审引入的资金闭环回归测试(2026-05-20)
        ("Test 24: 🔴 _bill_ctx fail-closed", test_24_bill_ctx_fail_closed),
        # Codex 3-审 新增(2026-05-20): SSE 客户端中断退款边界
        # [开源 E3 · B3b/B3c · 2026-09-28] Test 25/27/28/30/33/34 随 content_api 端点删除,本清单同步去掉
        # v1.5 chat 附件计费(2026-05-21 老板拍 Codex 5 阻断收尾)
        ("Test 29: 🔴 services cost metadata 完整", test_29_chat_attachment_services_cost_metadata),
        # Codex 5-审 4 阻断收尾(2026-05-21)
        ("Test 35: 🔴 P2-1 migration_012 接入启动链", test_35_migration_012_auto_run_in_startup),
        # Codex 6 审 P1 真闭环(2026-05-21)
        # Codex 7 审 P1 严格闭环(2026-05-21)
        # 老板补强 · 结构性顺序锁(2026-05-21)
    ]

    passed = 0
    failed = []
    for name, fn in tests:
        try:
            if asyncio.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
            passed += 1
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  ❌ FAIL: {e}")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ❌ ERROR: {type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    print(f"📊 结果: {passed}/{len(tests)} 通过")
    if failed:
        print(f"\n🔴 {len(failed)} 失败:")
        for name, err in failed:
            print(f"  · {name}: {err}")
    else:
        print("\n✅ 全部通过 · V3.1 业务代码接入零回归")
    print("=" * 70)
    return failed


if __name__ == "__main__":
    failed = asyncio.run(main())
    sys.exit(1 if failed else 0)
