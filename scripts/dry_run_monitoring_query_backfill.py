"""
缺口 3 dry-run · 统计 + 抽样预览 monitoring_query 回填(只读 · 不写库)

trust-layer · 2026-04-26 · 老板拍板:先 dry-run · 等二次确认才正式 UPDATE

行为:
1. 预检 information_schema · 确认 monitoring_query 字段存在 · 不存在直接 exit 0
2. 统计 confirmed_keywords / extra_keywords 中 monitoring_query IS NULL 的总数
3. 抽样 10+10 条(JOIN quotes JOIN brands 拿 industry / cities)
4. 对每条生成预览的两个版本:
   - simple : build_question(keyword) — 现有 fallback "X哪家好？推荐一下"
   - enhanced : f"{first_city} {industry} {keyword}" — P0.8 PLAN 原始设计意图
5. 输出 markdown 报告(stdout · 可重定向到文件)
6. 不执行任何 UPDATE / INSERT
7. SQL 异常后 rollback · 防 "current transaction is aborted" 级联

用法(Windows PowerShell / Linux 均可):
  python scripts/dry_run_monitoring_query_backfill.py
  python scripts/dry_run_monitoring_query_backfill.py > /tmp/backfill_dry_run.md

退出码:
  0 = 跑完(无论有无空值,字段未迁移也是 0)
  1 = DB 连接失败 / 致命异常
"""
from __future__ import annotations

import os
import sys
from typing import Any

# ============ 1. repo root 加入 sys.path · Windows 默认 cwd 不在 repo root 时也能跑 ============
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ============ 2. Windows GBK stdout · 强制 UTF-8 输出 ============
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass  # 老 Python / 非标准 stream 跳过


def _build_question_simple(keyword: str) -> str:
    """对齐 tools/monitoring/batch_monitor.py:build_question · 只读不 import 防循环"""
    if not keyword:
        return ""
    return f"{keyword}哪家好？推荐一下"


def _first_city(cities_raw: Any) -> str:
    """brands.cities 是逗号 / 中文逗号分隔字符串 · 取第一个"""
    if not cities_raw:
        return ""
    s = str(cities_raw).strip()
    for sep in (",", "，", "、", " "):
        if sep in s:
            return s.split(sep)[0].strip()
    return s


def _build_question_enhanced(keyword: str, industry: str, city: str) -> str:
    """P0.8 PLAN 设计意图 · 给避坑词补行业 + 城市锚点"""
    parts = []
    city = (city or "").strip()
    industry = (industry or "").strip()
    if city:
        parts.append(city)
    if industry:
        parts.append(industry)
    parts.append(keyword)
    return " ".join(parts) + "  哪家靠谱推荐一下"


def _has_column(cur, table: str, column: str) -> bool:
    """information_schema 预检 · 字段不存在返 False · 异常返 False"""
    try:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
            LIMIT 1
            """,
            (table, column),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _safe_exec(cur, conn, sql: str, params: tuple = ()) -> tuple[bool, list, str]:
    """执行 SQL · 异常即 rollback 防级联 abort
    Returns: (ok, rows, err_msg)
    """
    try:
        cur.execute(sql, params)
        rows = cur.fetchall() or []
        return True, rows, ""
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, [], str(exc)


def main() -> int:
    try:
        from db.connection import get_connection
    except Exception as exc:
        print(f"[ERROR] 无法 import db.connection: {exc}", file=sys.stderr)
        return 1

    try:
        conn = get_connection()
        cur = conn.cursor()
    except Exception as exc:
        print(f"[ERROR] DB 连接失败: {exc}", file=sys.stderr)
        return 1

    out: list[str] = []
    out.append("# monitoring_query 回填 dry-run 报告")
    out.append("")
    out.append("- 生成:`scripts/dry_run_monitoring_query_backfill.py` · 只读 · 0 写库")
    out.append("- 目的:看清存量 monitoring_query IS NULL 的规模 + 抽样预览生成结果")
    out.append("- 老板批后再决定是否做正式 UPDATE")
    out.append("")

    # ========== 0. 字段预检(防 transaction aborted 级联) ==========
    ck_has_mq = _has_column(cur, "confirmed_keywords", "monitoring_query")
    ek_has_mq = _has_column(cur, "extra_keywords", "monitoring_query")

    out.append("## 0 · schema 预检")
    out.append("")
    out.append("| 表 | monitoring_query 字段 |")
    out.append("| --- | --- |")
    out.append(f"| confirmed_keywords | {'[OK] 已存在' if ck_has_mq else '[MISSING] migration 未跑'} |")
    out.append(f"| extra_keywords | {'[OK] 已存在' if ek_has_mq else '[MISSING] migration 未跑'} |")
    out.append("")

    if not ck_has_mq and not ek_has_mq:
        out.append("> **migration 未跑** · 两表均无 `monitoring_query` 字段。")
        out.append("> 后续章节跳过(避免 current transaction is aborted 级联报错)。")
        out.append(">")
        out.append("> **解决方案**:`docker restart omnirank-ai` 触发 `db.monitoring_db.init_db` 的")
        out.append("> `_safe_add_column` 自愈逻辑(参见 `db/monitoring_db.py:413-414`)。")
        try:
            conn.close()
        except Exception:
            pass
        print("\n".join(out))
        return 0

    # ========== 1. 统计总量 ==========
    out.append("## 1 · 统计")
    out.append("")
    ck_null = ck_total = ek_null = ek_total = -1

    if ck_has_mq:
        ok, rows, err = _safe_exec(
            cur,
            conn,
            "SELECT COUNT(*) FILTER (WHERE monitoring_query IS NULL) AS n, COUNT(*) AS t FROM confirmed_keywords",
        )
        if ok and rows:
            r = rows[0]
            ck_null = r["n"] if not isinstance(r, tuple) else r[0]
            ck_total = r["t"] if not isinstance(r, tuple) else r[1]
        elif not ok:
            out.append(f"- ⚠ confirmed_keywords 统计 SQL 异常(已 rollback):`{err}`")

    if ek_has_mq:
        ok, rows, err = _safe_exec(
            cur,
            conn,
            "SELECT COUNT(*) FILTER (WHERE monitoring_query IS NULL) AS n, COUNT(*) AS t FROM extra_keywords",
        )
        if ok and rows:
            r = rows[0]
            ek_null = r["n"] if not isinstance(r, tuple) else r[0]
            ek_total = r["t"] if not isinstance(r, tuple) else r[1]
        elif not ok:
            out.append(f"- ⚠ extra_keywords 统计 SQL 异常(已 rollback):`{err}`")

    out.append("| 表 | 总条数 | monitoring_query IS NULL | 占比 |")
    out.append("| --- | ---: | ---: | ---: |")
    if ck_has_mq:
        ck_pct = (ck_null * 100.0 / ck_total) if ck_total > 0 else 0.0
        out.append(f"| confirmed_keywords | {ck_total} | {ck_null} | {ck_pct:.1f}% |")
    else:
        out.append("| confirmed_keywords | _字段未迁移_ | — | — |")
    if ek_has_mq:
        ek_pct = (ek_null * 100.0 / ek_total) if ek_total > 0 else 0.0
        out.append(f"| extra_keywords | {ek_total} | {ek_null} | {ek_pct:.1f}% |")
    else:
        out.append("| extra_keywords | _字段未迁移_ | — | — |")
    out.append("")

    # ========== 2. 抽样 confirmed_keywords ==========
    out.append("## 2 · confirmed_keywords 抽样(最多 10 条 · 优先近期)")
    out.append("")
    if not ck_has_mq:
        out.append("- _字段未迁移 · 跳过_")
    else:
        ok, rows, err = _safe_exec(
            cur,
            conn,
            """
            SELECT
                ck.id AS keyword_id,
                ck.keyword,
                ck.created_at,
                q.id AS quote_id,
                q.brand_id,
                COALESCE(b.industry, '') AS industry,
                COALESCE(b.cities, q.city, '') AS cities,
                b.name AS brand_name
            FROM confirmed_keywords ck
            LEFT JOIN quotes q ON q.id = ck.quote_id
            LEFT JOIN brands b ON b.id = q.brand_id
            WHERE ck.monitoring_query IS NULL
            ORDER BY ck.created_at DESC NULLS LAST
            LIMIT 10
            """,
        )
        if not ok:
            out.append(f"- ⚠ 抽样 SQL 异常(已 rollback):`{err}`")
        elif not rows:
            out.append("- _无符合条件的 confirmed_keywords(可能本表 monitoring_query 已全部回填)_")
        else:
            out.append("| # | brand | industry | first_city | 原 keyword | 现 fallback(simple) | 建议 enhanced |")
            out.append("| ---: | --- | --- | --- | --- | --- | --- |")
            for i, row in enumerate(rows, 1):
                kw = (row.get("keyword") if not isinstance(row, tuple) else row[1]) or ""
                industry = (row.get("industry") if not isinstance(row, tuple) else row[5]) or ""
                cities = row.get("cities") if not isinstance(row, tuple) else row[6]
                city = _first_city(cities)
                brand = (row.get("brand_name") if not isinstance(row, tuple) else row[7]) or ""
                simple = _build_question_simple(kw)
                enhanced = _build_question_enhanced(kw, industry, city)
                out.append(
                    f"| {i} | {brand[:20]} | {industry[:12]} | {city[:10]} | "
                    f"{kw[:30]} | {simple[:40]} | {enhanced[:60]} |"
                )
    out.append("")

    # ========== 3. 抽样 extra_keywords ==========
    out.append("## 3 · extra_keywords 抽样(最多 10 条 · 优先近期 active)")
    out.append("")
    if not ek_has_mq:
        out.append("- _字段未迁移 · 跳过_")
    else:
        ok, rows2, err = _safe_exec(
            cur,
            conn,
            """
            SELECT
                ek.id AS keyword_id,
                ek.keyword,
                ek.created_at,
                ek.brand_id,
                ek.client_id,
                COALESCE(b.industry, '') AS industry,
                COALESCE(b.cities, '') AS cities,
                b.name AS brand_name
            FROM extra_keywords ek
            LEFT JOIN brands b ON b.id = ek.brand_id
            WHERE ek.monitoring_query IS NULL
              AND COALESCE(ek.status, 'active') = 'active'
            ORDER BY ek.created_at DESC NULLS LAST
            LIMIT 10
            """,
        )
        if not ok:
            out.append(f"- ⚠ 抽样 SQL 异常(已 rollback):`{err}`")
        elif not rows2:
            out.append("- _无符合条件的 extra_keywords_")
        else:
            out.append("| # | brand | industry | first_city | 原 keyword | 现 fallback(simple) | 建议 enhanced |")
            out.append("| ---: | --- | --- | --- | --- | --- | --- |")
            for i, row in enumerate(rows2, 1):
                kw = (row.get("keyword") if not isinstance(row, tuple) else row[1]) or ""
                industry = (row.get("industry") if not isinstance(row, tuple) else row[5]) or ""
                cities = row.get("cities") if not isinstance(row, tuple) else row[6]
                city = _first_city(cities)
                brand = (row.get("brand_name") if not isinstance(row, tuple) else row[7]) or ""
                simple = _build_question_simple(kw)
                enhanced = _build_question_enhanced(kw, industry, city)
                out.append(
                    f"| {i} | {brand[:20]} | {industry[:12]} | {city[:10]} | "
                    f"{kw[:30]} | {simple[:40]} | {enhanced[:60]} |"
                )
    out.append("")

    # ========== 4. 高风险词识别 ==========
    out.append("## 4 · 高风险词初筛(避坑 / 信息 / 噪声 · 监测铁 0 命中风险)")
    out.append("")
    if not ck_has_mq and not ek_has_mq:
        out.append("- _两表字段均未迁移 · 跳过_")
    else:
        # 拼 UNION SQL · 仅纳入存在字段的表
        union_parts: list[str] = []
        if ck_has_mq:
            union_parts.append(
                """
                SELECT 'confirmed' AS src, ck.id AS kid, ck.keyword AS kw,
                       COALESCE(b.industry, '') AS industry, COALESCE(b.cities, '') AS cities
                FROM confirmed_keywords ck
                LEFT JOIN quotes q ON q.id = ck.quote_id
                LEFT JOIN brands b ON b.id = q.brand_id
                WHERE ck.monitoring_query IS NULL
                  AND ck.keyword ~* '(怎么选|避坑|没有套路|如何分辨|攻略|是否|什么是|对比知识|价格大全|骗局)'
                """
            )
        if ek_has_mq:
            union_parts.append(
                """
                SELECT 'extra' AS src, ek.id AS kid, ek.keyword AS kw,
                       COALESCE(b.industry, '') AS industry, COALESCE(b.cities, '') AS cities
                FROM extra_keywords ek
                LEFT JOIN brands b ON b.id = ek.brand_id
                WHERE ek.monitoring_query IS NULL
                  AND COALESCE(ek.status, 'active') = 'active'
                  AND ek.keyword ~* '(怎么选|避坑|没有套路|如何分辨|攻略|是否|什么是|对比知识|价格大全|骗局)'
                """
            )
        sql = " UNION ALL ".join(union_parts) + " ORDER BY kid DESC LIMIT 30"
        ok, risk_rows, err = _safe_exec(cur, conn, sql)
        if not ok:
            out.append(f"- ⚠ 高风险词扫描异常(已 rollback):`{err}`")
        elif not risk_rows:
            out.append("- _未发现明显避坑 / 信息型词_")
        else:
            out.append(f"- 检出 **{len(risk_rows)}** 条疑似避坑 / 信息型词(回填后建议代理人工 review)")
            out.append("")
            out.append("| 表 | id | 关键词 | industry | first_city |")
            out.append("| --- | ---: | --- | --- | --- |")
            for r in risk_rows[:20]:
                src = r.get("src") if not isinstance(r, tuple) else r[0]
                kid = r.get("kid") if not isinstance(r, tuple) else r[1]
                kw = (r.get("kw") if not isinstance(r, tuple) else r[2]) or ""
                ind = (r.get("industry") if not isinstance(r, tuple) else r[3]) or ""
                cit = _first_city(r.get("cities") if not isinstance(r, tuple) else r[4])
                out.append(f"| {src} | {kid} | {kw[:35]} | {ind[:12]} | {cit[:10]} |")
    out.append("")

    # ========== 5. 决策建议 ==========
    out.append("## 5 · 决策建议(供老板拍板)")
    out.append("")
    out.append("- **A · 做正式回填** · 建议用 `enhanced` 版本(city + industry + keyword + 引导后缀)")
    out.append("  - 风险:已发布给客户的报告可能引用旧 keyword 文本 · 监测语义会扩大命中范围(预期更多结果 · 不是更少)")
    out.append("  - 推荐 SQL(示例 · 不要直接跑 · 等老板二次确认 + pg_dump 备份):")
    out.append("    ```sql")
    out.append("    UPDATE extra_keywords ek")
    out.append("    SET monitoring_query = TRIM(BOTH ' ' FROM")
    out.append("        COALESCE(SPLIT_PART(b.cities, ',', 1), '') || ' ' ||")
    out.append("        COALESCE(b.industry, '') || ' ' || ek.keyword")
    out.append("        || '  哪家靠谱推荐一下')")
    out.append("    FROM brands b")
    out.append("    WHERE ek.brand_id = b.id AND ek.monitoring_query IS NULL;")
    out.append("    ```")
    out.append("- **B · 不回填** · 让 `resolve_monitoring_query` 继续 fallback 到 `build_question` · 维持现状")
    out.append("- **C · 仅回填高风险词** · 用第 4 节列出的避坑 / 信息词列表 · 减少范围")
    out.append("")
    out.append("**等老板二次确认才动手 UPDATE · 不在本脚本执行写库**")

    try:
        conn.close()
    except Exception:
        pass

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
