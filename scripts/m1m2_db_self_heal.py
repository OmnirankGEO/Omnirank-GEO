"""
M1c+M2 DB 自愈脚本 · CTO-15.16 2026-04-26
==========================================

任务: 把 client_profiles + monitoring_reports 缺的 v3.7 / M1c / M2 列补回来
目的:
  1. 本地 docker DB 在某些环境(image 滞后 / startup 静默 except)缺关键列
  2. 缺列导致 baseline 跑出全 25 分(industry_brief* 字段读不到 → D 组归零 → completeness 卡 25)
  3. 缺 monitoring_reports.modules_jsonb 等列导致 ai_write_report v2 路径 update 报错

方法:
  调用 db/profile_db.py::_ensure_columns 和 db/monitoring_db.py::init_monitoring_tables
  这两个函数本身是 ALTER TABLE ... ADD COLUMN IF NOT EXISTS 风格 · idempotent 安全

铁律:
  - 不写新表 · 只补缺列(现有 _ensure_columns 列表为唯一权威源)
  - 失败不阻断后续 · 输出明细让人审
  - 默认连本地 docker DB · 不直接连生产
  - 跑生产前必须 .env 改 DATABASE_URL + 老板单独确认

跑法:
  cd c:/AI-Test/AgentsCope-07-m1m2-final
  python scripts/m1m2_db_self_heal.py

输出:
  scripts/m1m2_db_self_heal_result.json (机器可读)
  stdout 人可读对比

data_scope: local docker DB snapshot, not production verified
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

# 期望 client_profiles 必有的 M1c / v3.7 列(对照 db/profile_db.py::_ensure_columns)
EXPECTED_PROFILE_COLS = [
    "structured_knowledge", "personality_profile",
    "creator_type", "content_frequency", "monetization_goal",
    "follower_count", "profile_completeness", "preferred_advisor_id",
    "preferred_expert_id", "plan_mode",
    "admin_insight", "admin_insight_level", "admin_insight_updated_at",
    "industry_brief", "industry_brief_status", "industry_brief_started_at",
    "industry_brief_confirmed", "industry_brief_confirmed_at",
    "industry_brief_partial_fields", "industry_brief_edits", "industry_brief_version",
    "service_scope", "service_scope_reasoning", "local_competitors",
    "brief_applied_version", "brief_applied_at",
    "business_type", "city_scope",
]

# 期望 monitoring_reports 必有的 M2 v2 列
EXPECTED_REPORT_COLS = [
    "content", "status", "reviewed_by", "reviewed_at", "sent_at", "brand_id",
    "version", "evidence_count", "modules_jsonb",
]


def _columns_of(table: str) -> set[str]:
    """只读拿列名 · 不能污染 connection pool readonly 状态(否则后续 ALTER 会 'read-only transaction')"""
    conn = get_connection()
    try:
        # 不调 set_session(readonly=True) · 否则 connection 归还 pool 后 init_db 会爆 read-only error
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        rows = cur.fetchall() or []
        out = set()
        for row in rows:
            try:
                out.add(row.get("column_name") if hasattr(row, "get") else row[0])
            except Exception:
                pass
        return {c for c in out if c}
    finally:
        try:
            # rollback any pending tx · pooled conn 复用前清状态
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    db_url = os.getenv("DATABASE_URL", "(unset)")
    masked = db_url
    if "@" in masked and ":" in masked.split("@", 1)[0]:
        head, tail = masked.split("@", 1)
        scheme_user = head.rsplit(":", 1)[0]
        masked = f"{scheme_user}:***@{tail}"
    print(f"[m1m2-self-heal] DATABASE_URL = {masked}")

    # 1. before snapshot
    before_profile = _columns_of("client_profiles")
    before_reports = _columns_of("monitoring_reports")
    profile_missing_before = sorted(set(EXPECTED_PROFILE_COLS) - before_profile)
    reports_missing_before = sorted(set(EXPECTED_REPORT_COLS) - before_reports)

    print(
        "\n[before] client_profiles 缺列:"
        + (", ".join(profile_missing_before) if profile_missing_before else "(全有)")
    )
    print(
        "[before] monitoring_reports 缺列:"
        + (", ".join(reports_missing_before) if reports_missing_before else "(全有)")
    )

    # 2. 调原迁移函数(idempotent · ALTER ... IF NOT EXISTS 模式)
    profile_err: str | None = None
    reports_err: str | None = None

    try:
        from db.profile_db import _ensure_columns as _ensure_profile_columns
        # 重置 _ensured_columns flag 强制再跑一次
        import db.profile_db as _pdb
        _pdb._ensured_columns = False
        _ensure_profile_columns()
        print("[heal] db.profile_db._ensure_columns 已跑")
    except Exception as e:
        profile_err = str(e)
        print(f"[heal] profile_db._ensure_columns 异常: {e}", file=sys.stderr)

    try:
        from db.monitoring_db import init_monitoring_tables
        init_monitoring_tables()
        print("[heal] db.monitoring_db.init_monitoring_tables 已跑")
    except Exception as e:
        reports_err = str(e)
        print(f"[heal] monitoring_db.init_monitoring_tables 异常: {e}", file=sys.stderr)

    # CTO-15.16 round2 P0(老板拍板 方案 A)·
    # 跑 seed_feature_pricing 把新加的 'autofill_brand' (130 积分) 插入 feature_pricing
    # · ON CONFLICT DO NOTHING 幂等 · 已存在不动
    # · 避免 omnirank-ai 容器没 restart 时本地无法测 _bill_ctx('autofill_brand')
    seed_err: str | None = None
    try:
        from db.wallet_db import seed_feature_pricing
        seed_feature_pricing()
        print("[heal] db.wallet_db.seed_feature_pricing 已跑")
    except Exception as e:
        seed_err = str(e)
        print(f"[heal] wallet_db.seed_feature_pricing 异常: {e}", file=sys.stderr)

    # 3. after snapshot
    after_profile = _columns_of("client_profiles")
    after_reports = _columns_of("monitoring_reports")

    # CTO-15.16 round2 P0 · 验证 'autofill_brand' seed 已落库 + cost_points=130
    autofill_seed_ok = False
    autofill_cost_actual: int | None = None
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT cost_points FROM feature_pricing WHERE feature_code = %s",
                ("autofill_brand",),
            )
            row = cur.fetchone()
            if row:
                autofill_cost_actual = (row.get("cost_points") if hasattr(row, "get") else row[0])
                autofill_seed_ok = (autofill_cost_actual == 130)
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
    except Exception as e:
        print(f"[heal] autofill_brand seed 验证异常: {e}", file=sys.stderr)
    print(
        f"[heal] feature_pricing.autofill_brand: cost_points="
        f"{autofill_cost_actual} · 期望 130 · {'✅' if autofill_seed_ok else '❌'}"
    )
    profile_missing_after = sorted(set(EXPECTED_PROFILE_COLS) - after_profile)
    reports_missing_after = sorted(set(EXPECTED_REPORT_COLS) - after_reports)
    profile_added = sorted(after_profile - before_profile)
    reports_added = sorted(after_reports - before_reports)

    print(
        "\n[after] client_profiles 缺列:"
        + (", ".join(profile_missing_after) if profile_missing_after else "(全有)")
    )
    print(
        "[after] monitoring_reports 缺列:"
        + (", ".join(reports_missing_after) if reports_missing_after else "(全有)")
    )
    print(f"\n[heal] client_profiles 新增列: {profile_added or '(无)'}")
    print(f"[heal] monitoring_reports 新增列: {reports_added or '(无)'}")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_scope": "local docker DB snapshot, not production verified",
        "db_dsn_masked": masked,
        "before": {
            "client_profiles_missing": profile_missing_before,
            "monitoring_reports_missing": reports_missing_before,
        },
        "after": {
            "client_profiles_missing": profile_missing_after,
            "monitoring_reports_missing": reports_missing_after,
        },
        "added": {
            "client_profiles": profile_added,
            "monitoring_reports": reports_added,
        },
        "errors": {
            "profile": profile_err,
            "reports": reports_err,
            "seed": seed_err,
        },
        "autofill_brand_seed": {
            "expected_cost_points": 130,
            "actual_cost_points": autofill_cost_actual,
            "ok": autofill_seed_ok,
        },
    }

    out_path = Path(__file__).parent / "m1m2_db_self_heal_result.json"
    # 强制 LF 落盘 · 防 Windows write_text 默认 CRLF 污染 git diff --check
    out_path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    print(f"\n[heal] JSON 已写: {out_path}")

    # 退出码:
    # - 仍有缺列 → 1
    # - autofill_brand seed 缺 / cost_points 不对 → 1(M1c billing 链路不可用)
    # - 全 OK → 0
    if profile_missing_after or reports_missing_after:
        print("\n⚠ 仍有缺列 · 检查 errors 字段(可能是数据库权限/表不存在)")
        return 1
    if not autofill_seed_ok:
        print(
            "\n❌ FAIL · autofill_brand seed 不正确 · /api/profiles/ai-fill 扣费会失败 · "
            "检查 db.wallet_db.seed_feature_pricing 是否包含 ('autofill_brand', '客户资料 AI 补齐', 130, ...)"
        )
        return 1
    print("\n✅ 关键列齐全 + autofill_brand seed=130 已落库 · M1c+M2 schema baseline 达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
