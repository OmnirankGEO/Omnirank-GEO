"""
test_intake_acceptance — CTO-E 客户资料补全邀请 H 验收清单

直接调 db / service 层 (绕过 HTTP 校验), 打满 token 状态机和 submission 审核流程.

H 验收覆盖:
  H1 token revoke 后公开 GET 返回不可提交
  H2 expires_at 过期后 submit 被拒绝
  H3 客户 submit 后正式 profile 不变
  H4 代理 approve 后 profile 变化 (completeness 增加)
  H5 reject 后 profile 不变
  H6 ai-suggest 超 5 次被拒绝 (counter)
  H7 同 token URL 不能看到内部代理信息/利润/成本 (检公开 view 字段集)

使用:
  docker exec omnirank-ai python /app/scripts/test_intake_acceptance.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timezone, timedelta

from db.connection import get_connection
from db import intake_db
from db.profile_db import find_profile_by_name
from services import intake_diff
from utils.brand_completeness import compute_brand_completeness


PASS = "[PASS]"
FAIL = "[FAIL]"


def _ensure_test_brand() -> int:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM brands WHERE name = %s", ("__cto_e_test_brand",))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute("""
            INSERT INTO brands (name, industry, cities, owner_user_id)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, ("__cto_e_test_brand", "测试行业", "深圳", 1))
        bid = cur.fetchone()["id"]
        conn.commit()
        return bid
    finally:
        conn.close()


def _cleanup(brand_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM profile_update_submissions WHERE brand_id = %s
        """, (brand_id,))
        cur.execute("""
            DELETE FROM intake_tokens WHERE brand_id = %s
        """, (brand_id,))
        # delete any test profile created during merge
        cur.execute("""
            DELETE FROM client_profiles WHERE brand_id = %s
        """, (brand_id,))
        cur.execute("DELETE FROM brands WHERE id = %s", (brand_id,))
        conn.commit()
    finally:
        conn.close()


def _expire_token(token_id: int):
    """直接 set expires_at = past, 触发 expired."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        cur.execute(
            "UPDATE intake_tokens SET expires_at = %s WHERE id = %s",
            (past, token_id),
        )
        conn.commit()
    finally:
        conn.close()


def _profile_snapshot(brand_id: int) -> dict:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        brand = dict(cur.fetchone() or {})
    finally:
        conn.close()
    profile = find_profile_by_name(brand.get("name", ""), brand_id=brand_id) or {}
    return {
        "brand": brand,
        "profile": profile,
        "completeness": compute_brand_completeness(brand, profile).get("score", 0),
    }


def run():
    failures = []

    brand_id = _ensure_test_brand()
    print(f">>> test brand_id = {brand_id}")

    try:
        # ---------- H1: revoke 后不可提交 ----------
        t = intake_db.create_intake_token(
            brand_id=brand_id, inviter_user_id=1, ttl_days=7,
        )
        intake_db.revoke_token(t["id"], reason="test")
        rec = intake_db.get_token_by_string(t["token"])
        reason = intake_db.is_token_actionable(rec)
        if reason == "revoked":
            print(f"{PASS} H1 revoke: actionable reason = {reason}")
        else:
            failures.append(f"H1 revoke: expected 'revoked', got {reason}")

        # ---------- H2: 过期后 submit 被拒 ----------
        t2 = intake_db.create_intake_token(
            brand_id=brand_id, inviter_user_id=1, ttl_days=7,
        )
        _expire_token(t2["id"])
        rec2 = intake_db.get_token_by_string(t2["token"])
        reason2 = intake_db.is_token_actionable(rec2)
        if reason2 == "expired":
            print(f"{PASS} H2 expired: actionable reason = {reason2}")
        else:
            failures.append(f"H2 expired: expected 'expired', got {reason2}")

        # ---------- H3: submit 后正式 profile 不变 ----------
        t3 = intake_db.create_intake_token(
            brand_id=brand_id, inviter_user_id=1, ttl_days=7,
        )
        before = _profile_snapshot(brand_id)
        # 模拟 submit
        payload = {
            "industry": "餐饮/中式快餐",
            "business": "做盖浇饭外卖, 服务白领午餐",
            "target_users": "周边写字楼上班族",
            "selling_points": "30 分钟内必达, 食材当天采购",
            "core_value": "比同行更新鲜便宜",
        }
        norm, _, _ = intake_diff.normalize_payload(payload)
        diff = intake_diff.build_diff(norm, None, before["brand"], before["profile"])
        sub = intake_db.insert_submission(
            token_id=t3["id"], brand_id=brand_id,
            payload=payload, ai_suggested=None, diff=diff,
            submitted_by_name="测试客户",
        )
        intake_db.mark_token_submitted(t3["id"])
        after_submit = _profile_snapshot(brand_id)
        if after_submit["completeness"] == before["completeness"]:
            print(f"{PASS} H3 submit: profile completeness unchanged ({before['completeness']} == {after_submit['completeness']})")
        else:
            failures.append(
                f"H3 submit: profile changed without approve "
                f"({before['completeness']} -> {after_submit['completeness']})"
            )

        # ---------- H4: approve 后 profile 增加 ----------
        merge_result = intake_diff.merge_approved_fields(
            submission=sub,
            brand_id=brand_id,
            approved_form_keys=list(norm.keys()),
        )
        intake_db.update_submission_review(
            submission_id=sub["id"], reviewer_id=1, status="approved",
            approved_fields=merge_result["applied"], review_notes=None,
        )
        after_approve = _profile_snapshot(brand_id)
        if after_approve["completeness"] > before["completeness"]:
            print(
                f"{PASS} H4 approve: profile completeness "
                f"{before['completeness']} -> {after_approve['completeness']} "
                f"(applied {merge_result['applied']})"
            )
        else:
            failures.append(
                f"H4 approve: completeness did not rise "
                f"({before['completeness']} -> {after_approve['completeness']}, "
                f"applied {merge_result['applied']})"
            )

        # ---------- H5: reject 后 profile 不变 ----------
        t4 = intake_db.create_intake_token(
            brand_id=brand_id, inviter_user_id=1, ttl_days=7,
        )
        before_reject = _profile_snapshot(brand_id)
        sub4 = intake_db.insert_submission(
            token_id=t4["id"], brand_id=brand_id,
            payload={"company_intro": "新简介, 应该不被合并"},
            ai_suggested=None, diff={"fields": []},
        )
        intake_db.mark_token_submitted(t4["id"])
        intake_db.update_submission_review(
            submission_id=sub4["id"], reviewer_id=1, status="rejected",
            approved_fields=[], review_notes="test rejection",
        )
        after_reject = _profile_snapshot(brand_id)
        if (
            after_reject["completeness"] == before_reject["completeness"]
            and after_reject["profile"].get("company_intro")
                == before_reject["profile"].get("company_intro")
        ):
            print(
                f"{PASS} H5 reject: profile unchanged "
                f"(completeness {before_reject['completeness']} == {after_reject['completeness']})"
            )
        else:
            failures.append(
                f"H5 reject: profile changed unexpectedly "
                f"(intro before={before_reject['profile'].get('company_intro')!r}, "
                f"after={after_reject['profile'].get('company_intro')!r})"
            )

        # ---------- H6: ai-suggest 超 5 次被拒 ----------
        t6 = intake_db.create_intake_token(
            brand_id=brand_id, inviter_user_id=1, ttl_days=7,
        )
        for i in range(intake_db.MAX_AI_SUGGEST_PER_TOKEN):
            n = intake_db.increment_ai_suggest_count(t6["id"])
            assert n == i + 1, f"counter mismatch at {i}: {n}"
        rec6 = intake_db.get_token_by_string(t6["token"])
        used = int(rec6.get("ai_suggest_count") or 0)
        if used >= intake_db.MAX_AI_SUGGEST_PER_TOKEN:
            print(
                f"{PASS} H6 ai-suggest cap: counter at {used} "
                f">= {intake_db.MAX_AI_SUGGEST_PER_TOKEN} (cap reached)"
            )
        else:
            failures.append(f"H6 ai-suggest cap: counter at {used} < cap")

        # ---------- H7: 公开 view 字段集校验 ----------
        # 我们模拟 _build_public_view 返回, 校验不含敏感字段
        from api.intake_api import _build_public_view
        t7 = intake_db.create_intake_token(
            brand_id=brand_id, inviter_user_id=1, ttl_days=7,
        )
        view = _build_public_view(intake_db.get_token_by_string(t7["token"]))
        forbidden_keys = {
            "inviter_user_id", "owner_user_id", "user_id",
            "monthly_price", "selling_price", "cost", "profit",
            "markup", "agent_level", "commission",
        }
        view_str = json.dumps(view, ensure_ascii=False, default=str).lower()
        leaks = [k for k in forbidden_keys if k.lower() in view_str]
        if not leaks:
            print(f"{PASS} H7 public view: no internal/profit/cost leaks")
            print(f"      keys returned: {sorted(view.keys())}")
        else:
            failures.append(f"H7 public view leak: {leaks}")

    finally:
        _cleanup(brand_id)

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===")
        for f in failures:
            print(f"  {FAIL} {f}")
        sys.exit(1)
    print("=== ALL H-LIST ACCEPTANCE PASSED ===")


if __name__ == "__main__":
    run()
