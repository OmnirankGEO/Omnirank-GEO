"""
regen_v2_reports — CTO-B 2026-04-26 quality 验收 · 把已有诊断记录 back-fill 成 v2

不重跑诊断流(LLM 贵 + 慢)· 直接读 raw_data_json 的诊断结果数据 · 喂给
services.diagnosis_report_v2.assemble_diagnosis_report_v2 · 落 v2 列。

用法:
  docker exec omnirank-ai python /app/scripts/regen_v2_reports.py 135 120 110
  docker exec omnirank-ai python /app/scripts/regen_v2_reports.py --all-recent  # 最近 30 天的所有

退出码:
  0 = 全部成功
  1 = 部分失败(stderr 列出 diagnosis_id)
  2 = 参数错误
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# 项目根目录在 sys.path · docker 内 /app
sys.path.insert(0, "/app")

from db.connection import get_connection
from services.diagnosis_report_v2 import assemble_diagnosis_report_v2, update_diagnosis_v2_in_db


def _load_diagnosis_record(diag_id: int) -> dict[str, Any] | None:
    """从 diagnosis_records 拉真实数据"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM diagnosis_records WHERE id = %s", (diag_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def _load_brand_record(brand_id: int) -> dict[str, Any]:
    if not brand_id:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _load_profile_record(brand_id: int) -> dict[str, Any]:
    if not brand_id:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM client_profiles WHERE brand_id = %s ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            (brand_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _load_quote_record(brand_id: int) -> dict[str, Any]:
    if not brand_id:
        return {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM quotes WHERE brand_id = %s ORDER BY created_at DESC LIMIT 1",
            (brand_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _shape_diagnosis_results(record: dict) -> tuple[dict, dict]:
    """从 diagnosis_records 行还原 diagnosis_workflow 期望的 results dict + score_data"""
    raw = record.get("raw_data_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    # 优先用 raw_data_json 自身的 data · 没有就退回 diagnosis_records 字段
    data_dict = raw.get("data") or {}
    if not data_dict:
        # 老记录可能没存 raw_data_json.data · 用列字段重建 ai_visibility 最低结构
        data_dict = {
            "ai_visibility": {
                "total_tests": record.get("ai_total_tests", 0) or 0,
                "detected_count": record.get("ai_detected_count", 0) or 0,
                "overall_mention_rate": record.get("ai_mention_rate", 0) or 0,
                "engines_tested": record.get("ai_engines_tested", []),
                "dimension_stats": {},  # 老记录没有 · 报告会标"数据不足"
            },
            "web_search": {
                "result_count": record.get("web_result_count", 0) or 0,
                "brand_direct_count": record.get("web_brand_direct_count", 0) or 0,
            },
        }

    # action_plan / suggestions · 从 raw 取或 fallback 空
    suggestions = data_dict.get("action_plan") or raw.get("suggestions") or {}

    keywords = record.get("keywords") or ""
    if isinstance(keywords, str) and keywords.startswith("["):
        try:
            keywords = json.loads(keywords)
        except Exception:
            keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    elif isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]

    diagnosis_results = {
        "brand": record.get("brand_name", ""),
        "industry": record.get("industry", ""),
        "session_id": record.get("session_id", ""),
        "keywords": keywords,
        "data": {
            **data_dict,
            "action_plan": suggestions if isinstance(suggestions, dict) else {},
        },
        "scores": {
            "total_score": record.get("total_score") or 0,
            "level": record.get("level") or "",
        },
    }

    # score_data · 5 维 100 制(W1 SSOT)
    # 重要(quality fix 2026-04-26):老 db 列名(ai_visibility_score / web_search_score 等)
    #   是老 7 维制(max 25/20/18/15/12/5/5 = 100)的子集 · 不能直接当 5 维 SSOT(max 30/25/20/15/10)使用
    #   旧 map 会导致评分口径冲突("封面 8/100 vs 5 维评分总览 1/100" P0 bug)
    #
    # 解决:regen 时只传顶部 total/level · 不合成 dimension_scores
    #   让 v2 装配模块从 raw_data.ai_visibility + raw_data.web_search 重新跑
    #   calculate_geo_scope_score()(W1 修复 result_count 兜底后真 5 维 SSOT)
    score_data = (
        raw.get("score_data") or
        raw.get("geo_score") or
        {}
    )
    if not score_data.get("dimension_scores"):
        # 不要合成 0 占位 · 让 v2 装配模块从原始 ai_visibility 数据重算
        score_data = {
            "total_score": record.get("total_score") or 0,
            "max_score": 100,
            "level": record.get("level") or "",
            # 故意不设 dimension_scores · v2 装配会触发 calculate_geo_scope_score 重算
        }
    return diagnosis_results, score_data


def regen_one(diag_id: int) -> tuple[bool, str]:
    """back-fill 单条 v2 报告 · 返 (成功, 信息)"""
    record = _load_diagnosis_record(diag_id)
    if not record:
        return False, f"diagnosis_id={diag_id} 不存在"

    brand_id = record.get("brand_id")
    if not brand_id:
        return False, f"diagnosis_id={diag_id} 无 brand_id · 跳过"

    brand = _load_brand_record(brand_id)
    profile = _load_profile_record(brand_id)
    quote = _load_quote_record(brand_id)

    diag_results, score_data = _shape_diagnosis_results(record)

    try:
        v2_result = assemble_diagnosis_report_v2(
            diagnosis_results=diag_results,
            brand=brand,
            profile=profile,
            quote=quote,
            brand_id=brand_id,
            score_data=score_data,
            # 🔴 [#54/#55] 本脚本没有可透传的游标(装配深处自开连接会重放 08-10 自锁死),
            #    所以**显式**传不可用,并写明 reason —— 不是省略、也不是默认值。
            #    后果:回填/重生成产物在这一格上退出分母,与主链路口径不同,已在交付里点名。
            published={"count": None, "available": False, "reason": "regen_no_cursor"},
        )
    except Exception as e:
        return False, f"v2 装配异常: {e}"

    ok = update_diagnosis_v2_in_db(diag_id, v2_result)
    if not ok:
        return False, "落库失败 · 见 services.diagnosis_report_v2 日志"

    info_parts = [
        f"v2 已落库",
        f"completeness={v2_result.get('completeness',{}).get('score')}",
        f"evidence={v2_result.get('internal',{}).get('evidence_count')}",
        f"internal_md={len((v2_result.get('internal') or {}).get('full_markdown',''))} chars",
        f"client_md={len((v2_result.get('client') or {}).get('full_markdown',''))} chars",
    ]
    if v2_result.get("error"):
        info_parts.append(f"error={v2_result['error']}")
    return True, " · ".join(info_parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("ids", nargs="*", type=int, help="diagnosis_id list")
    p.add_argument("--all-recent", action="store_true", help="所有最近 30 天的诊断")
    args = p.parse_args()

    target_ids: list[int] = list(args.ids)

    if args.all_recent:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM diagnosis_records WHERE created_at >= NOW() - INTERVAL '30 days' "
                "ORDER BY id DESC LIMIT 50"
            )
            target_ids.extend([r["id"] for r in cur.fetchall()])
        finally:
            conn.close()

    if not target_ids:
        print("ERROR: 无 diagnosis_id 输入", file=sys.stderr)
        sys.exit(2)

    print(f"开始 back-fill v2 · 共 {len(target_ids)} 条")
    failed: list[tuple[int, str]] = []
    for did in target_ids:
        ok, info = regen_one(did)
        if ok:
            print(f"  [OK] diagnosis_id={did} · {info}")
        else:
            print(f"  [FAIL] diagnosis_id={did} · {info}", file=sys.stderr)
            failed.append((did, info))

    print()
    print(f"完成 · 成功 {len(target_ids) - len(failed)}/{len(target_ids)}")
    if failed:
        print(f"失败 {len(failed)}:")
        for did, info in failed:
            print(f"  - {did}: {info}")
        sys.exit(1)


if __name__ == "__main__":
    main()
