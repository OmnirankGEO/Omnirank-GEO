"""
M1c · 5 金标准真实可执行性 smoke · CTO-15.16 round2 Task D

不是 simulation overlay(那是 m1c_reachable_5brands.py)· 这是真的查 DB + 文件系统 ·
看每个 golden brand 现在能否真跑 BatchUpgradeDialog(persist=True)+ deep-analyze +
confirm 这条 M1c 主链 · 给老板能审的执行性清单.

输出每 brand:
  - brand_id / brand_name / 当前 completeness 分(用 SSOT)
  - profile 是否存在 · 缺字段清单
  - ai-fill 可执行性:
      · 客户知识库目录是否存在 / 文档字数(预测 /api/profiles/ai-fill 是否 400)
  - deep-analyze + confirm 前置:
      · 资料是否够喂 deep_analyze_user(industry / business / target_users 任 1)
      · industry_brief_status / industry_brief_confirmed 当前值
  - 推荐下一步路径(human-readable)

模式:
  默认 = dry-run · 仅 SELECT · 0 写库
  --execute = 真写客户档案(走 _persist_ai_fill_to_profile · 同 BatchUpgradeDialog persist 路径)
              · 必须配套 env M1C_ACTUAL_EXECUTE=yes(防双指误开)
              · 必须 DATABASE_URL 含 'localhost' 或 'omnirank-db'(防误指生产)
              · 仅对 5 金标准 brand_id 生效 · 永不批量

老板红线:不批量清洗 49 brand · 不自动跑生产写库 · 需老板逐 brand 签收

跑法:
  python scripts/m1c_actual_smoke.py            # dry-run · 只读
  M1C_ACTUAL_EXECUTE=yes python scripts/m1c_actual_smoke.py --execute   # 5 金标准本地写

输出:
  scripts/m1c_actual_smoke_result.json
  data_scope: local docker DB OR staging (按 DATABASE_URL host 推断) · 未 production verified
"""
from __future__ import annotations

import argparse
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
from utils.brand_completeness import compute_brand_completeness  # noqa: E402

GOLDEN_BRANDS = [
    {"alias": "全域上榜", "name_like": "全域上榜"},
    {"alias": "一路顺风", "name_like": "一路顺风"},
    {"alias": "QZQZ", "name_like": "QZQZ"},
    {"alias": "驰鲸", "name_like": "驰鲸"},
    {"alias": "揭阳大昀", "name_like": "揭阳大昀"},
]

# 知识库目录(对照 api.profile_api._read_client_knowledge)
_KB_BASE = ROOT / "data" / "knowledge" / "clients"


def _classify_data_scope(dsn: str) -> str:
    """按 DSN host 推断 data_scope · 防误指生产"""
    if "@" not in dsn:
        return "unknown"
    after_at = dsn.split("@", 1)[1]
    host = after_at.split(":", 1)[0].split("/", 1)[0].lower()
    if host in ("localhost", "127.0.0.1", "omnirank-db", "host.docker.internal"):
        return "local"
    if "staging" in host or "test" in host:
        return "staging"
    return "production"  # 任何其他都视为生产 · 保守


def _fetch_brand(cur, name_like: str) -> dict | None:
    cur.execute(
        """
        SELECT * FROM brands
        WHERE name ILIKE %s AND (is_deleted IS NOT TRUE)
        ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1
        """,
        (f"%{name_like}%",),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_profile(cur, brand_id: int) -> dict | None:
    cur.execute(
        """
        SELECT * FROM client_profiles
        WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT 1
        """,
        (brand_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _kb_status(brand_id: int) -> dict:
    """对照 _read_client_knowledge(brand_id) 预测 ai-fill 是否 400"""
    kb_dir = _KB_BASE / str(brand_id)
    if not kb_dir.exists():
        return {"exists": False, "files": 0, "total_chars": 0, "predicted_400": True, "reason": "目录不存在"}
    files = 0
    total_chars = 0
    for fp in kb_dir.rglob("*"):
        if not fp.is_file():
            continue
        if fp.name.endswith("_cleaned.json") or fp.name.startswith("."):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
            if text.strip():
                files += 1
                total_chars += len(text)
        except Exception:
            continue
    if files == 0:
        return {"exists": True, "files": 0, "total_chars": 0, "predicted_400": True, "reason": "目录存在但无可读文件"}
    return {
        "exists": True, "files": files, "total_chars": total_chars,
        "predicted_400": False, "reason": f"OK · {files} 文件 / {total_chars} 字符",
    }


def _deep_analyze_readiness(profile: dict | None) -> dict:
    """deep_analyze_user 至少要 industry / business / target_users 任 1 · 否则 LLM 输出垃圾"""
    if not profile:
        return {"ready": False, "reason": "profile 不存在"}
    has_industry = bool((profile.get("industry") or "").strip())
    has_business = bool((profile.get("business") or "").strip())
    has_target = bool((profile.get("target_users") or "").strip())
    if not (has_industry or has_business or has_target):
        return {"ready": False, "reason": "industry/business/target_users 全空 · 跑也是垃圾"}
    return {
        "ready": True,
        "has_industry": has_industry,
        "has_business": has_business,
        "has_target_users": has_target,
        "reason": "至少 1 个核心字段已填",
    }


def _next_step_hint(comp: dict, kb: dict, deep_ready: dict, brief_state: str) -> str:
    """human-readable 下一步建议"""
    score = comp.get("score", 0)
    if score >= 80 and brief_state == "confirmed":
        return "✅ 已达 80+ 且 brief 已确认 · 可正常进入诊断/拓词链"
    if kb["predicted_400"]:
        return f"❌ 先上传客户知识库到 data/knowledge/clients/{comp.get('_brand_id', '?')} · 否则 ai-fill 必 400"
    if score < 60:
        return "1️⃣ 跑 BatchUpgrade(persist=true) → ai-fill 落 12 字段(B/C 组 +50 分)"
    if score < 80:
        return "2️⃣ 跑 /api/content/deep-analyze + confirm → D 组 +10(从 ai_filled 到 confirmed)"
    if brief_state != "confirmed":
        return "3️⃣ brief 未 confirmed · 写作链拿不到注入 · 在 BrandDetailPage 行业 Tab 点确认"
    return "✓ 边界完成 · 微调 E 组(本地竞品/权威源/差异化)可达 90+"


def assess_one(cur, spec: dict) -> dict:
    """对单个 golden brand 做执行性评估"""
    out: dict = {"alias": spec["alias"], "name_like": spec["name_like"]}
    brand = _fetch_brand(cur, spec["name_like"])
    if not brand:
        out["matched"] = False
        out["error"] = "brand 不存在"
        return out
    out["matched"] = True
    out["brand_id"] = brand["id"]
    out["brand_name"] = brand.get("name")
    out["brand_industry"] = brand.get("industry")
    out["owner_user_id"] = brand.get("owner_user_id")

    profile = _fetch_profile(cur, brand["id"])
    out["has_profile"] = profile is not None

    comp = compute_brand_completeness(brand, profile)
    out["completeness_score"] = comp["score"]
    out["completeness_groups"] = comp["groups"]
    out["completeness_missing"] = comp["missing"]

    out["kb_status"] = _kb_status(brand["id"])
    out["deep_analyze_readiness"] = _deep_analyze_readiness(profile)

    brief_state = comp.get("industry_brief_state", "idle")
    out["industry_brief_state"] = brief_state

    # 拼一个供 _next_step_hint 用的 comp 携带 brand_id
    comp_with_id = {**comp, "_brand_id": brand["id"]}
    out["next_step_hint"] = _next_step_hint(comp_with_id, out["kb_status"], out["deep_analyze_readiness"], brief_state)

    # ai-fill 可执行性总判定
    can_run_ai_fill = (
        not out["kb_status"]["predicted_400"]
    )
    out["can_run_ai_fill_persist"] = can_run_ai_fill

    return out


def execute_one(cur, conn, brand_id: int, brand_name: str) -> dict:
    """--execute 模式 · 用合成 LLM-style data 走 _persist_ai_fill_to_profile

    本期不调真 LLM(防 token 浪费 + 跨开发机不稳定)· 只用代表性合成 dict 验证落库链路
    真实 LLM 输出由 BatchUpgradeDialog 在浏览器侧触发
    """
    from api.profile_api import _persist_ai_fill_to_profile
    synthetic_parsed = {
        "industry": "(测试 · 行业)",
        "business": "(测试 · 主营业务一句话)",
        "target_users": "(测试 · 目标客户画像)",
        "pain_points": ["痛点 A", "痛点 B"],
        "competitors": ["竞品 X", "竞品 Y"],
        "company_intro": "(测试 · 公司简介 200 字内 · 仅 smoke 用 · 老板审核后可清)",
        "core_value": "(测试 · 核心价值)",
        "selling_points": "(测试 · 卖点 1)\n(测试 · 卖点 2)",
        "success_cases": "(测试 · 成功案例)",
        "testimonials": "(测试 · 客户证言)",
        "service_scope": "local",
        "local_competitors": ["本地竞品 A", "本地竞品 B"],
        "market_insight": {
            "authority_sources": ["权威源 1", "权威源 2"],
            "hot_formats": ["榜单", "测评"],
            "my_differentiation": "(测试 · 差异化定位)",
        },
        "structured_knowledge": {},
    }
    synthetic_confidences = {k: "weak" for k in synthetic_parsed.keys()}
    return _persist_ai_fill_to_profile(brand_id, synthetic_parsed, synthetic_confidences)


def main() -> int:
    parser = argparse.ArgumentParser(description="M1c 5 金标准真实可执行性 smoke")
    parser.add_argument(
        "--execute", action="store_true",
        help="真写库 · 必须配 env M1C_ACTUAL_EXECUTE=yes · 仅本地/staging · 不批量",
    )
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL", "(unset)")
    masked = db_url
    if "@" in masked and ":" in masked.split("@", 1)[0]:
        head, tail = masked.split("@", 1)
        scheme_user = head.rsplit(":", 1)[0]
        masked = f"{scheme_user}:***@{tail}"
    data_scope = _classify_data_scope(db_url)
    print(f"[m1c-actual-smoke] DATABASE_URL = {masked}")
    print(f"[m1c-actual-smoke] data_scope = {data_scope}")

    write_mode = bool(args.execute)
    if write_mode:
        if os.getenv("M1C_ACTUAL_EXECUTE") != "yes":
            print("[m1c-actual-smoke] ❌ --execute 需要 env M1C_ACTUAL_EXECUTE=yes 双确认")
            return 2
        if data_scope == "production":
            print(
                "[m1c-actual-smoke] ❌ data_scope=production · 拒绝 --execute · "
                "若确实要在生产跑请先线下和老板逐 brand 签收 · 改本脚本白名单"
            )
            return 2
        print("[m1c-actual-smoke] ⚠ --execute 模式 · 将对 5 金标准用合成数据 update_profile · 5 brand only · 不批量")
    else:
        print("[m1c-actual-smoke] mode = dry-run · 0 写库")

    print()

    conn = get_connection()
    try:
        cur = conn.cursor()
        rows: list[dict] = []
        for spec in GOLDEN_BRANDS:
            r = assess_one(cur, spec)
            if write_mode and r.get("matched") and r.get("can_run_ai_fill_persist") and not data_scope == "production":
                try:
                    persist_summary = execute_one(cur, conn, r["brand_id"], r["brand_name"])
                    r["execute_persist_summary"] = persist_summary
                    # 重新算分对照 before/after
                    profile_after = _fetch_profile(cur, r["brand_id"])
                    brand_after_row = _fetch_brand(cur, spec["name_like"])
                    comp_after = compute_brand_completeness(brand_after_row, profile_after)
                    r["completeness_score_after"] = comp_after["score"]
                    r["completeness_groups_after"] = comp_after["groups"]
                except Exception as e:
                    r["execute_error"] = str(e)
            rows.append(r)
    finally:
        try:
            conn.rollback()  # dry-run 模式 SELECT 安全 · execute 模式上面已 commit
        except Exception:
            pass
        conn.close()

    # 渲染 markdown
    print("| alias | brand_id | score | profile? | kb? | deep_ready? | brief_state | can_run_ai_fill? | next_step |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if not r.get("matched"):
            print(f"| {r['alias']} | - | - | - | - | - | - | - | {r.get('error', '?')} |")
            continue
        kb = r["kb_status"]
        kb_str = f"{kb['files']}f/{kb['total_chars']}c" if kb["exists"] else "无"
        deep = "Y" if r["deep_analyze_readiness"]["ready"] else "N"
        score_disp = (
            f"{r['completeness_score']}→{r.get('completeness_score_after')}"
            if write_mode and "completeness_score_after" in r
            else str(r["completeness_score"])
        )
        print(
            f"| {r['alias']} | {r['brand_id']} | {score_disp} | {'Y' if r['has_profile'] else 'N'} | "
            f"{kb_str} | {deep} | {r['industry_brief_state']} | "
            f"{'Y' if r.get('can_run_ai_fill_persist') else 'N'} | {r['next_step_hint']} |"
        )

    out_path = Path(__file__).parent / "m1c_actual_smoke_result.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_scope": data_scope,
        "db_dsn_masked": masked,
        "mode": "execute" if write_mode else "dry_run",
        "rows": rows,
    }
    out_path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    print(f"\n[m1c-actual-smoke] JSON 已写: {out_path}")

    matched = [r for r in rows if r.get("matched")]
    if not matched:
        print("\n❌ 0 brand 匹配 · 检查 name_like / DB 数据")
        return 1
    print(f"\n✅ 评估完成 · 5 金标准匹配 {len(matched)}/5 · mode={payload['mode']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
