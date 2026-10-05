"""
诊断 session 归属校验(WORKERS=4 · foundation-fix #1b)—— WS / 轮询 / 快照读取统一入口。
====================================================================
把原 server.py `_ws_authorize_session`(audit #731/#13 fail-closed)抽成可注入、可单测的模块,
让 WS 与轮询端点共用同一归属校验(boss 收口#1:"统一先做 owner/brand RBAC 再读 Redis/DB")。

规则(fail-CLOSED):
- admin 放行;
- 反查 diagnosis_records.brand_id(session_id 最新一行)→ brands.owner_user_id==uid 或 user_clients 分配;
- 未知 session / 无 brand 归属的临时诊断 / **DB 异常** → 一律拒(fail-closed · 防构造 DB 故障绕过)。

列名铁律(audit #13):只取 diagnosis_records.brand_id(prod 确定存在);不取 owner_user_id/operator_user_id
(本机/prod 列名相反,取错列 UndefinedColumn → 被 except 吞成放行 = 假绿后门)。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("GEO-SessionAccess")


def authorize_session(user: dict, session_id: str,
                      get_conn: Optional[Callable] = None) -> bool:
    """诊断进度归属校验。user=已解码 JWT(dict);get_conn 可注入(测试)· 默认 db.connection.get_connection。
    返回 True=放行;任何不确定/异常 → False(fail-closed)。

    [返工3 P1] 归属源优先级:**diagnosis_runs.owner_user_id**(资金状态机权威归属 · 无品牌诊断 brand_id=NULL
    也能授权 owner)→ 只有该 session **无 run**(旧数据)才回退 diagnosis_records→brand。
    原实现只按 diagnosis_records.brand_id,brand_id=NULL(允许不选品牌发起诊断)时直接拒 → 合法 owner 自己被 403。
    diagnosis_runs 表缺失/查询异常 → 回滚后落 records 回退(不因表缺失放行);两链都判否/异常 → 拒(fail-closed)。"""
    try:
        if not user or not isinstance(user, dict):
            return False
        if user.get("is_admin"):
            return True
        uid = user.get("user_id") or user.get("id")
        if not uid:
            return False
        if get_conn is None:
            from db.connection import get_connection as get_conn  # type: ignore
        conn = get_conn()
        try:
            cur = conn.cursor()
            # ① 优先 diagnosis_runs 归属(owner_user_id 是迁移定义的确定列 · 无列名歧义)
            try:
                cur.execute(
                    "SELECT 1 FROM diagnosis_runs WHERE session_id = %s AND owner_user_id = %s LIMIT 1",
                    (session_id, uid),
                )
                if cur.fetchone() is not None:
                    return True  # 本人是 run owner → 放行(含无品牌诊断)
                # run 挂在 uid 被分配的 brand 上(品牌协作)→ 放行
                cur.execute(
                    "SELECT brand_id FROM diagnosis_runs WHERE session_id = %s AND brand_id IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    (session_id,),
                )
                brow = cur.fetchone()
                if brow is not None:
                    rb = brow["brand_id"] if isinstance(brow, dict) else brow[0]
                    # [返工3 修复净增量] run 挂的 brand 的 **owner** 也放行(brand owner 可能不在 user_clients ·
                    #   如 get_or_create_brand 只写 brands.owner_user_id 不写 user_clients · 或 admin 指派归属)。
                    #   原 run 路径只查 {creator, user_clients} 漏了 brand owner → 别人在其品牌起诊断时真 owner 自己被 403。
                    #   与 records 回退路径的 owner 检查对齐(两路授权集必须一致)。
                    cur.execute("SELECT owner_user_id FROM brands WHERE id = %s", (rb,))
                    _bo = cur.fetchone()
                    if _bo is not None and (_bo["owner_user_id"] if isinstance(_bo, dict) else _bo[0]) == uid:
                        return True
                    cur.execute(
                        "SELECT 1 FROM user_clients WHERE user_id = %s AND brand_id = %s LIMIT 1",
                        (uid, rb),
                    )
                    if cur.fetchone() is not None:
                        return True
                # 该 session 是否存在**任何** run(存在=run 是权威归属源且已判否 → 拒 · 不回退 records)
                cur.execute("SELECT 1 FROM diagnosis_runs WHERE session_id = %s LIMIT 1", (session_id,))
                if cur.fetchone() is not None:
                    return False
                # 到这:确认无 run(旧数据)→ 落 records 回退
            except Exception as _dre:
                # diagnosis_runs 表缺失/查询异常 → 回滚(防污染后续查询)· 落 records 回退(不因表缺失放行)
                logger.warning(f"[session-access] diagnosis_runs 归属查询异常 · 回退 records session={session_id}: {_dre}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            # ② 回退 diagnosis_records→brand(仅当无 run 或 diagnosis_runs 查询异常)
            #    只取 brand_id(prod 确定存在的列);不取归属列(列名本机/prod 相反,取错=UndefinedColumn 假绿)
            cur.execute(
                "SELECT brand_id FROM diagnosis_records WHERE session_id = %s ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return False  # 无 run 且无 record → 未知 session → 拒(防猜测枚举他人进度)
            bid = row["brand_id"] if isinstance(row, dict) else row[0]
            if not bid:
                # 旧无品牌诊断且无 run → 无法授权 → fail-closed 拒(旧数据·安全优先 · 新诊断都有 run 走 ① 授权)
                return False
            cur.execute("SELECT owner_user_id FROM brands WHERE id = %s", (bid,))
            b = cur.fetchone()
            if b and (b["owner_user_id"] if isinstance(b, dict) else b[0]) == uid:
                return True
            cur.execute(
                "SELECT 1 FROM user_clients WHERE user_id = %s AND brand_id = %s LIMIT 1",
                (uid, bid),
            )
            return cur.fetchone() is not None
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        # DB 异常 fail-CLOSED 拒(原 server.py fail-open 是后门:构造 DB 故障即绕过越权订阅)。
        logger.warning(f"[session-access] 归属校验异常 · fail-closed 拒 session={session_id}: {e}")
        return False
