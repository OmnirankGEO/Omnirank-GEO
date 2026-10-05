"""[ADVISOR-OWNERSHIP] 生产只读探针 —— 给 Deploy 随车跑,输出决定存量回填走 A 还是 B。

    docker exec -i omnirank-blue python - < scripts/advisor_ownership_probe.py

🔴 只读保证(两条):
  1. 本脚本一条写语句都没有(除下面那条**注定失败**的探针);
  2. 先做一次必失败的 UPDATE 自证 —— 它不改变连接属性,只是让"我以为是只读"
     这句话**可证伪**:没报错就说明假设错了,整轮取数作废。

为什么必须跑这个而不是照仓库 DDL 写 migration:
  仓库里 advisor_conversations 有**两份互斥 DDL**
    - db/diagnosis_db.py:7337   → PostgreSQL,有 session_id(NOT NULL),无 conversation_id
    - db/migrate_v5_advisor_chat.py:21 → **SQLite 语法**(AUTOINCREMENT/DATETIME),有 conversation_id
  而线上活着的写入路径(api/advisor_api.py::_get_or_create_conversation)
  **只写 conversation_id、从不写 session_id**。若生产表真是第一份,每次顾问对话都会
  NOT NULL 违例 —— 所以**两份 DDL 都不描述生产**,真列只能现取。

输出 JSON 贴回执行方。
"""
import json
import sys

sys.path.insert(0, "/app")


def _probe_readonly(cur, conn) -> str:
    try:
        cur.execute("UPDATE advisor_conversations SET title = title WHERE 1 = 0")
        return "🔴 只读自证失败:那条 UPDATE 没报错 → 本轮取数作废"
    except Exception as exc:
        conn.rollback()
        return f"只读自证 ✅ 写被拒:{type(exc).__name__}"


def main() -> int:
    from db.connection import get_connection
    conn = get_connection()
    out = {}
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        out["readonly_probe"] = _probe_readonly(cur, conn)

        # ① 真列(决定 migration 怎么写、回填有没有线索)
        cur.execute("""
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_name = 'advisor_conversations'
             ORDER BY ordinal_position
        """)
        cols = [dict(r) for r in cur.fetchall()]
        out["columns"] = cols
        names = {c["column_name"] for c in cols}
        out["has_owner_user_id_already"] = "owner_user_id" in names

        # ② 量级(决定要不要 CREATE INDEX CONCURRENTLY)
        cur.execute("SELECT count(*) AS n FROM advisor_conversations")
        total = cur.fetchone()["n"]
        out["total_rows"] = total
        out["index_needs_concurrently"] = total > 500000

        # ③ 回填线索:session_id 有多少条可用、能不能唯一定位
        if "session_id" in names:
            cur.execute("""
                SELECT count(*) FILTER (WHERE session_id IS NOT NULL AND session_id <> '') AS 有session,
                       count(DISTINCT session_id) AS 不同session数
                  FROM advisor_conversations
            """)
            out["session_id_stats"] = dict(cur.fetchone())
        else:
            out["session_id_stats"] = None

        # ④ session_id 能不能反查到用户 —— 逐个候选表试,全部只读
        traceable = {}
        for tbl, col in [("user_sessions", "session_id"),
                         ("diagnosis_sessions", "session_id"),
                         ("keyword_selection_sessions", "session_id")]:
            cur.execute("SELECT to_regclass(%s) AS t", (tbl,))
            if not cur.fetchone()["t"]:
                traceable[tbl] = "表不存在"
                continue
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = %s AND column_name IN ('user_id','owner_user_id')
            """, (tbl,))
            usercols = [r["column_name"] for r in cur.fetchall()]
            traceable[tbl] = {"有用户列": usercols, "可用": bool(usercols)}
        out["backfill_candidates"] = traceable

        # ⑤ 结论建议(仅供参考,最终由 Owner 拍)
        usable = [k for k, v in traceable.items() if isinstance(v, dict) and v["可用"]]
        out["建议"] = (
            "情形 A(可回填):" + ", ".join(usable)
            if usable and out["session_id_stats"] and out["session_id_stats"].get("有session")
            else "情形 B(无可追溯线索)→ 存量全留 NULL = 仅 admin 可见"
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
