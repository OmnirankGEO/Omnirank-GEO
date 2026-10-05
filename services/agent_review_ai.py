"""
代理申请 AI 审核引擎（v1.1 简化版，无实名二要素）

输入: OCR 识别结果 + 用户填写 + 风控数据（IP / 设备 / 账户年龄 / 历史 fraud）
输出: {verdict, risk_flags, checks_run}
      verdict = "auto_approve" (无 flag) | "manual_review" (任一 flag)

6 项风险检查:
1. LOW_OCR_CONFIDENCE   OCR 置信度 < 0.90
2. NAME_MISMATCH        OCR 识别姓名 ≠ 用户填写姓名
3. IDCARD_MISMATCH      OCR 识别身份证号 ≠ 用户填写身份证号
4. NEW_ACCOUNT          账户注册不足 7 天
5. SAME_IP_24H          同 IP 24h 内有其他申请
6. SAME_DEVICE_AGENT    同设备指纹已存在审核通过的代理
7. HAS_FRAUD_HISTORY    历史有 referral_bonus_records.fraud_flag=TRUE 记录

AI 不自动拒绝 - 任何 flag 都只进人工审核队列, 尊重审核员最终裁量权.

注意: 本函数接收外部传入的 cursor (由 partner_api 管理), 不自开连接.
"""

import logging
from typing import Optional

logger = logging.getLogger("GEO-Partner-Review-AI")


# OCR 置信度门槛（低于此值进人工）
OCR_CONFIDENCE_THRESHOLD = 0.90
# 账户年龄门槛（天）
MIN_ACCOUNT_AGE_DAYS = 7


def run_ai_review(
    user_id: int,
    ocr_front: dict,
    ocr_back: dict,
    real_name: str,
    id_card_no_plain: str,
    ip_address: Optional[str],
    device_fingerprint: Optional[str],
    cursor,
) -> dict:
    """审核引擎入口

    Args:
        user_id:              申请人 user_id
        ocr_front:            recognize_id_card(side='front') 返回
        ocr_back:             recognize_id_card(side='back') 返回
        real_name:            用户在表单中填写的真实姓名
        id_card_no_plain:     用户填写的身份证号（明文, 比对用）
        ip_address:           申请时客户端 IP
        device_fingerprint:   设备指纹
        cursor:               psycopg2 RealDictCursor

    Returns:
        {
          "verdict":  "auto_approve" | "manual_review",
          "risk_flags": [{"code": str, "detail": str}, ...],
          "checks_run": int,
        }
    """
    flags: list = []

    # Check 1: OCR 置信度
    conf_front = (ocr_front or {}).get("confidence") or 0.0
    conf_back = (ocr_back or {}).get("confidence") or 0.0
    min_conf = min(conf_front, conf_back)
    if min_conf < OCR_CONFIDENCE_THRESHOLD:
        flags.append({
            "code": "LOW_OCR_CONFIDENCE",
            "detail": f"置信度不足: front={conf_front:.2f} back={conf_back:.2f} (阈值 {OCR_CONFIDENCE_THRESHOLD})",
        })

    # Check 2: 姓名一致
    ocr_name = ((ocr_front or {}).get("name") or "").strip()
    real_name_clean = (real_name or "").strip()
    if ocr_name and real_name_clean and ocr_name != real_name_clean:
        flags.append({
            "code": "NAME_MISMATCH",
            "detail": f"OCR 识别={ocr_name} / 用户填写={real_name_clean}",
        })

    # Check 3: 身份证号一致（OCR vs 用户填写）
    ocr_idno = ((ocr_front or {}).get("id_card_no") or "").strip()
    idno_clean = (id_card_no_plain or "").strip()
    if ocr_idno and idno_clean and ocr_idno != idno_clean:
        flags.append({
            "code": "IDCARD_MISMATCH",
            "detail": "OCR 识别身份证号与用户填写不一致",
        })

    # Check 4: 账户年龄
    try:
        cursor.execute(
            """
            SELECT EXTRACT(EPOCH FROM (NOW() - created_at))/86400 AS days_old
            FROM users WHERE id = %s
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        days_old = (row or {}).get("days_old") or 0
        if days_old < MIN_ACCOUNT_AGE_DAYS:
            flags.append({
                "code": "NEW_ACCOUNT",
                "detail": f"账户注册仅 {days_old:.1f} 天 (阈值 {MIN_ACCOUNT_AGE_DAYS})",
            })
    except Exception as e:
        logger.warning(f"[AI-Review] 账户年龄检查失败 user={user_id}: {e}")
        # 查询失败时保守: flag 上, 让人工判断
        flags.append({"code": "NEW_ACCOUNT", "detail": f"账户年龄无法确认: {e}"})

    # Check 5: 同 IP 24h 内其他申请
    if ip_address:
        try:
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt FROM agent_applications
                WHERE ip_address = %s
                  AND user_id != %s
                  AND created_at >= NOW() - INTERVAL '24 hours'
                """,
                (ip_address, user_id),
            )
            row = cursor.fetchone()
            cnt = (row or {}).get("cnt") or 0
            if cnt > 0:
                flags.append({
                    "code": "SAME_IP_24H",
                    "detail": f"同 IP 24h 内 {cnt} 次其他申请",
                })
        except Exception as e:
            logger.warning(f"[AI-Review] 同 IP 检查失败: {e}")

    # Check 6: 同设备指纹已有审核通过的代理
    if device_fingerprint:
        try:
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt FROM agent_applications
                WHERE device_fingerprint = %s
                  AND status = 'approved'
                  AND user_id != %s
                """,
                (device_fingerprint, user_id),
            )
            row = cursor.fetchone()
            cnt = (row or {}).get("cnt") or 0
            if cnt > 0:
                flags.append({
                    "code": "SAME_DEVICE_AGENT",
                    "detail": f"同设备指纹已存在 {cnt} 个审核通过的代理账户",
                })
        except Exception as e:
            logger.warning(f"[AI-Review] 同设备检查失败: {e}")

    # Check 7: 历史 fraud_flag
    try:
        cursor.execute(
            """
            SELECT COUNT(*) AS cnt FROM referral_bonus_records
            WHERE referrer_id = %s AND fraud_flag = TRUE
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        cnt = (row or {}).get("cnt") or 0
        if cnt > 0:
            flags.append({
                "code": "HAS_FRAUD_HISTORY",
                "detail": f"历史返利有 {cnt} 条 fraud_flag 标记",
            })
    except Exception as e:
        logger.warning(f"[AI-Review] fraud 历史检查失败: {e}")

    verdict = "auto_approve" if not flags else "manual_review"
    logger.info(
        f"[AI-Review] user={user_id} verdict={verdict} flags={len(flags)} "
        f"codes=[{','.join(f['code'] for f in flags)}]"
    )

    return {
        "verdict": verdict,
        "risk_flags": flags,
        "checks_run": 7,
    }
