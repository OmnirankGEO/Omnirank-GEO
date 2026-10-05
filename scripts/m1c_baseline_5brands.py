"""
M1c 资料端飞轮 · 5 金标准 brand 完整度 baseline 脚本
====================================================

任务: D2 (AI-5 Brand Flywheel · 2026-04-25)
目的: 跑 PRD §0.4 5 金标准 brand 当前完整度 · 出 before 表
方法:
  1. 只读 SELECT
  2. 按 name LIKE 匹配 5 brand
  3. 调 utils.brand_completeness.compute_brand_completeness 算分
  4. 输出 markdown + JSON 到 scripts/m1c_baseline_5brands_result.json

铁律:
  - 不写库 不改数据
  - 失败匹配也输出 missing_match · 让老板知道哪个名字没找到
  - 默认连本地 docker DB · 不直接跑生产
  - 跑生产前必须 .env 改 DATABASE_URL 指向生产 + 老板确认

跑法:
  cd c:/AI-Test/AgentsCope-07
  python scripts/m1c_baseline_5brands.py

输出:
  scripts/m1c_baseline_5brands_result.json (机器可读)
  stdout (人可读 markdown 表)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Windows-safe stdout/stderr (默认 PowerShell 是 GBK · 输出 emoji/中文会 UnicodeEncodeError)
# 用 reconfigure 强切 UTF-8 + errors="replace" · 不强制 PYTHONIOENCODING 也能跑
# JSON 落盘 encoding="utf-8" 保持不变 · 数据本身不变
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # 旧 Python / 非 TextIOWrapper · 忽略不阻断
        pass

# 让 utils.brand_completeness 可 import
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
from utils.brand_completeness import compute_brand_completeness  # noqa: E402


# PRD §0.4 5 金标准 brand · 按老板原话:全域上榜 / 一路顺风 / QZQZ / 驰鲸 / 揭阳大昀
GOLDEN_BRANDS = [
    {"alias": "全域上榜", "name_like": "全域上榜"},
    {"alias": "一路顺风", "name_like": "一路顺风"},
    {"alias": "QZQZ", "name_like": "QZQZ"},
    {"alias": "驰鲸", "name_like": "驰鲸"},
    {"alias": "揭阳大昀", "name_like": "揭阳大昀"},
]


def _fetch_brand_by_name_like(cur, name_like: str) -> dict | None:
    """按 name LIKE %name_like% 拉最新一条 brand · 排除软删
    schema: brands.is_deleted = BOOLEAN (omnirank-db 实测)
    """
    cur.execute(
        """
        SELECT *
        FROM brands
        WHERE name ILIKE %s
          AND (is_deleted IS NOT TRUE)
        ORDER BY updated_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        (f"%{name_like}%",),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_profile_by_brand_id(cur, brand_id: int) -> dict | None:
    """按 brand_id 拉最新一条 client_profiles · 排除软删
    schema: client_profiles.is_deleted = INTEGER (omnirank-db 实测)
    """
    cur.execute(
        """
        SELECT *
        FROM client_profiles
        WHERE brand_id = %s
          AND (is_deleted = 0 OR is_deleted IS NULL)
        ORDER BY updated_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        (brand_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def run_baseline() -> list[dict]:
    """跑 5 金标准 baseline · 返回 list of dict"""
    results: list[dict] = []
    conn = get_connection()
    try:
        # 防御:即使别处误开 autocommit · 这里也强制只读
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        for spec in GOLDEN_BRANDS:
            alias = spec["alias"]
            name_like = spec["name_like"]
            try:
                brand = _fetch_brand_by_name_like(cur, name_like)
            except Exception as e:
                results.append({
                    "alias": alias,
                    "name_like": name_like,
                    "error": f"brand 查询异常: {e}",
                })
                continue

            if not brand:
                results.append({
                    "alias": alias,
                    "name_like": name_like,
                    "matched": False,
                    "reason": "brands 表无 name ILIKE 匹配 (排除软删)",
                })
                continue

            brand_id = brand.get("id")
            try:
                profile = _fetch_profile_by_brand_id(cur, brand_id)
            except Exception as e:
                profile = None
                profile_err = str(e)
            else:
                profile_err = None

            try:
                comp = compute_brand_completeness(brand, profile)
            except Exception as e:
                results.append({
                    "alias": alias,
                    "matched": True,
                    "brand_id": brand_id,
                    "brand_name": brand.get("name"),
                    "error": f"compute_brand_completeness 异常: {e}",
                })
                continue

            results.append({
                "alias": alias,
                "matched": True,
                "brand_id": brand_id,
                "brand_name": brand.get("name"),
                "brand_industry": brand.get("industry"),
                "owner_user_id": brand.get("owner_user_id"),
                "has_profile": profile is not None,
                "profile_err": profile_err,
                "score": comp.get("score"),
                "groups": comp.get("groups"),
                "missing": comp.get("missing"),
                "industry_brief_state": comp.get("industry_brief_state"),
            })
    finally:
        conn.close()
    return results


DATA_SCOPE = "local docker DB snapshot, not production verified"


def render_markdown(results: list[dict]) -> str:
    """人可读 markdown 表"""
    lines = []
    lines.append(f"# M1c baseline · 5 金标准 brand 完整度 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("> 算法 SSOT: utils/brand_completeness.py v3.4 · 5 组 30+30+20+10+10=100")
    lines.append(f"> data_scope: **{DATA_SCOPE}**")
    lines.append("> ⚠ 不要把这份数据当生产真相 · 跑生产前必须 .env DATABASE_URL 切生产 + 老板单独确认")
    lines.append("")
    lines.append("| alias | matched | brand_id | brand_name | score | identity | business | marketing | brief | insight | brief_state | missing |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        if not r.get("matched"):
            lines.append(
                f"| {r['alias']} | ❌ | - | - | - | - | - | - | - | - | - | "
                f"{r.get('reason') or r.get('error', '')} |"
            )
            continue
        if r.get("error"):
            lines.append(
                f"| {r['alias']} | ⚠ | {r.get('brand_id', '-')} | "
                f"{r.get('brand_name', '-')} | err | - | - | - | - | - | - | "
                f"{r['error']} |"
            )
            continue
        g = r.get("groups", {}) or {}
        miss = r.get("missing", []) or []
        miss_short = ", ".join(miss[:6]) + (f" (+{len(miss)-6})" if len(miss) > 6 else "")
        lines.append(
            f"| {r['alias']} | ✅ | {r['brand_id']} | {r.get('brand_name', '-')[:20]} | "
            f"**{r.get('score', 0)}** | {g.get('identity', 0)}/30 | "
            f"{g.get('business', 0)}/30 | {g.get('marketing', 0)}/20 | "
            f"{g.get('deep_analysis', 0)}/10 | {g.get('market_insight', 0)}/10 | "
            f"{r.get('industry_brief_state', '-')} | {miss_short} |"
        )

    matched = [r for r in results if r.get("matched") and not r.get("error")]
    if matched:
        avg = sum(r.get("score", 0) for r in matched) / len(matched)
        ge80 = len([r for r in matched if r.get("score", 0) >= 80])
        lines.append("")
        lines.append(f"**汇总**: 匹配 {len(matched)}/{len(results)} · 平均 **{avg:.1f}** 分 · ≥80 占 **{ge80}/{len(matched)}**")
        lines.append("")
        lines.append("**PRD KR1 目标**: 平均 ≥ 65 · ≥80 占 70%(34/49 全库口径)")
        lines.append("")
        if avg >= 65:
            lines.append("✅ 5 金标准平均分已达 KR1 阈值 · 可推全库做存量批量升级")
        else:
            gap = 65 - avg
            lines.append(f"⏳ 距 KR1 平均 65 差 **{gap:.1f}** 分 · 5 brand 需先打满 D/E 组")
    else:
        lines.append("")
        lines.append("⚠️ 0 brand 匹配 · 检查 name_like 模式或 DB 数据")
    return "\n".join(lines)


def main() -> int:
    db_url = os.getenv("DATABASE_URL", "(unset)")
    masked = db_url
    if "@" in masked and ":" in masked.split("@", 1)[0]:
        head, tail = masked.split("@", 1)
        scheme_user = head.rsplit(":", 1)[0]
        masked = f"{scheme_user}:***@{tail}"
    print(f"[M1c baseline] DATABASE_URL = {masked}")
    print(f"[M1c baseline] 跑 {len(GOLDEN_BRANDS)} 个金标准 brand · 只读 SELECT")
    print("")

    try:
        results = run_baseline()
    except Exception as e:
        print(f"[M1c baseline] 致命错误: {e}", file=sys.stderr)
        return 1

    md = render_markdown(results)
    print(md)
    print("")

    out_path = Path(__file__).parent / "m1c_baseline_5brands_result.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ssot_module": "utils/brand_completeness.py",
        "scoring": "v3.4 · 30+30+20+10+10=100",
        "data_scope": DATA_SCOPE,
        "db_dsn_masked": masked,
        "golden_brands": GOLDEN_BRANDS,
        "results": results,
    }
    # 强制 LF 落盘 · 防 Windows Path.write_text 默认 CRLF
    out_path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    print(f"[M1c baseline] JSON 已写: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
