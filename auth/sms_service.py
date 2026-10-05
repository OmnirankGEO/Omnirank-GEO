"""阿里云短信发送服务"""
import hashlib
import os
import random
import time
import json
import logging
from typing import Mapping, Optional

logger = logging.getLogger("SMS")

# 内存降级缓存（仅在 Redis 和 DB 都不可用时使用）
_sms_codes: dict = {}

_client = None

REDIS_KEY_PREFIX = "sms:"
CODE_TTL = 300  # 验证码有效期（秒）


def _get_client():
    """获取阿里云短信客户端"""
    from dotenv import load_dotenv
    load_dotenv()

    from alibabacloud_dysmsapi20170525.client import Client
    from alibabacloud_tea_openapi.models import Config

    ak_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    ak_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")

    if not ak_id or not ak_secret:
        raise RuntimeError("ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET 未配置")

    logger.info(f"[SMS] 初始化客户端, AK_ID={ak_id[:8]}...")

    config = Config(
        access_key_id=ak_id,
        access_key_secret=ak_secret,
        endpoint="dysmsapi.aliyuncs.com",
    )
    return Client(config)


def get_sms_client():
    global _client
    if _client is None:
        _client = _get_client()
    return _client


def generate_code(length: int = 6) -> str:
    """生成随机验证码"""
    return ''.join([str(random.randint(0, 9)) for _ in range(length)])


def _init_sms_table():
    """确保 sms_codes 表存在"""
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sms_codes (
                    phone VARCHAR(20) PRIMARY KEY,
                    code VARCHAR(10) NOT NULL,
                    purpose VARCHAR(30) NOT NULL DEFAULT 'login',
                    attempts INT NOT NULL DEFAULT 0,
                    expires_at DOUBLE PRECISION NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
            cur.close()
            conn.close()
            return True
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"[SMS] sms_codes 表初始化失败: {e}")
        return False


_table_ready = False


def _ensure_table():
    global _table_ready
    if not _table_ready:
        _table_ready = _init_sms_table()
    return _table_ready


def _store_code(phone: str, code: str, purpose: str):
    """存储验证码到 DB（降级到 Redis，再降级到内存）"""
    record = {
        "code": code,
        "expires": time.time() + CODE_TTL,
        "attempts": 0,
        "purpose": purpose,
    }

    # 优先存 DB（跨 worker 共享）
    if _ensure_table():
        try:
            from db.connection import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sms_codes (phone, code, purpose, attempts, expires_at)
                VALUES (%s, %s, %s, 0, %s)
                ON CONFLICT (phone) DO UPDATE
                SET code = EXCLUDED.code, purpose = EXCLUDED.purpose,
                    attempts = 0, expires_at = EXCLUDED.expires_at, created_at = NOW()
            """, (phone, code, purpose, time.time() + CODE_TTL))
            conn.commit()
            cur.close()
            conn.close()
            logger.info(f"[SMS] 验证码已存入 DB: {phone[:3]}****{phone[-4:]}")
            return
        except Exception as e:
            logger.warning(f"[SMS] DB 存储失败，降级: {e}")

    # 降级到 Redis
    try:
        from cache.redis_client import redis_set
        redis_set(f"{REDIS_KEY_PREFIX}{phone}", json.dumps(record), ex=CODE_TTL)
    except Exception:
        pass
    # 最终降级到内存
    _sms_codes[phone] = record


def _get_code(phone: str) -> Optional[dict]:
    """从 DB 读取验证码（降级到 Redis，再降级到内存）"""
    # 优先从 DB 读
    if _ensure_table():
        try:
            from db.connection import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT code, purpose, attempts, expires_at FROM sms_codes WHERE phone = %s",
                (phone,)
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                # RealDictCursor 返回字典，按列名取
                return {
                    "code": row["code"],
                    "purpose": row["purpose"],
                    "attempts": row["attempts"],
                    "expires": row["expires_at"],
                }
            return None
        except Exception as e:
            logger.warning(f"[SMS] DB 读取失败，降级: {e}")

    # 降级到 Redis
    try:
        from cache.redis_client import redis_get
        val = redis_get(f"{REDIS_KEY_PREFIX}{phone}")
        if val:
            return json.loads(val)
    except Exception:
        pass
    return _sms_codes.get(phone)


def _update_attempts(phone: str, record: dict):
    """更新验证码尝试次数"""
    if _ensure_table():
        try:
            from db.connection import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "UPDATE sms_codes SET attempts = %s WHERE phone = %s",
                (record["attempts"], phone)
            )
            conn.commit()
            cur.close()
            conn.close()
            return
        except Exception as e:
            logger.warning(f"[SMS] DB 更新失败，降级: {e}")

    try:
        from cache.redis_client import redis_set, get_redis
        r = get_redis()
        if r:
            ttl = r.ttl(f"{REDIS_KEY_PREFIX}{phone}")
            if ttl and ttl > 0:
                redis_set(f"{REDIS_KEY_PREFIX}{phone}", json.dumps(record), ex=ttl)
    except Exception:
        pass
    _sms_codes[phone] = record


def _delete_code(phone: str):
    """删除验证码"""
    if _ensure_table():
        try:
            from db.connection import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM sms_codes WHERE phone = %s", (phone,))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning(f"[SMS] DB 删除失败: {e}")

    try:
        from cache.redis_client import redis_delete
        redis_delete(f"{REDIS_KEY_PREFIX}{phone}")
    except Exception:
        pass
    _sms_codes.pop(phone, None)


def send_sms_code(phone: str, purpose: str = "register") -> dict:
    """
    发送短信验证码（登录/注册专用路径：自生成码 + 写 sms_codes SSOT）

    Returns:
        {"success": True} 或 {"success": False, "error": "错误信息"}
    """
    code = generate_code()

    # 优先用主模板，备用模板兜底
    template_code = os.getenv("SMS_TEMPLATE_CODE", "SMS_504900316")
    fallback_template = os.getenv("SMS_TEMPLATE_FALLBACK", "SMS_333925051")

    for tmpl in [template_code, fallback_template]:
        result = _do_send(phone, tmpl, f'{{"code":"{code}"}}')
        if result["success"]:
            _store_code(phone, code, purpose)
            logger.info(f"SMS sent to {phone[:3]}****{phone[-4:]}, purpose={purpose}, template={tmpl}")
            return {"success": True}
        logger.warning(f"模板 {tmpl} 发送失败: {result.get('error')}")

    return {"success": False, "error": "短信服务暂时不可用，请稍后再试"}


# ========== 组织邀请等场景的公共送达原语 ==========
#
# 与登录路径的边界：
#   - send_verification_code 绝不自生成验证码（调用方供码）
#   - 绝不写 sms_codes（登录 SSOT 不被邀请链污染）
#   - 日志只记指纹，不记手机号/验证码/密钥明文
#
# 阿里云确定性错误码（重试不会成功 → 终态，不重试）
SMS_TERMINAL_PROVIDER_CODES = frozenset({
    "isv.MOBILE_NUMBER_ILLEGAL",      # 手机号格式非法
    "isv.INVALID_PARAMETERS",         # 参数错误
    "isv.TEMPLATE_MISSING_PARAMETERS",  # 模板缺少变量
    "isv.INVALID_TEMPLATE_CODE",      # 模板不存在
    "isv.TEMPLATE_NOT_PASS",          # 模板未审核通过
    "isv.SMS_TEMPLATE_ILLEGAL",       # 模板非法
    "isv.SIGN_NOT_PASS",              # 签名未审核通过
    "isv.INVALID_SIGN_NAME",          # 签名不存在
    "isv.SMS_SIGN_ILLEGAL",           # 签名非法
    "isv.SMS_SIGNATURE_SCENE_ILLEGAL",  # 签名与模板类型不匹配
    "isv.PRODUCT_UNSUBSCRIPT",        # 未开通短信服务
    "isv.ACCOUNT_NOT_EXISTS",         # 账户不存在
    "isv.ACCOUNT_ABNORMAL",           # 账户异常
    "isv.AMOUNT_NOT_ENOUGH",          # 余额不足
    "isv.OUT_OF_SERVICE",             # 业务停机
    "isv.BLACK_KEY_CONTROL_LIMIT",    # 黑名单管控
    "Forbidden.RAM",                  # RAM 权限拒绝
    "isv.RAM_PERMISSION_DENY",
    "isp.RAM_PERMISSION_DENY",
})

# 阿里云可重试错误码（限流/对端抖动 → 指数退避后重试）
SMS_RETRYABLE_PROVIDER_CODES = frozenset({
    "isv.BUSINESS_LIMIT_CONTROL",     # 触发流控
    "isv.DAY_LIMIT_CONTROL",          # 日限额
    "isv.HOUR_LIMIT_CONTROL",         # 小时限额
    "isv.MOBILE_COUNT_OVER_LIMIT",    # 单手机号条数上限
    "Throttling",
    "Throttling.User",
    "ServiceUnavailable",
    "InternalError",
    "isp.SYSTEM_ERROR",
    "isv.SYSTEM_ERROR",
    "SMS_SDK_MISSING",                # 部署缺 SDK：等待部署修复
    "SMS_SEND_EXCEPTION",             # 网络/超时等传输异常
    "SMS_CLIENT_CONFIG_MISSING",      # AK 未配置：等待部署修复
})


def classify_sms_provider_error(provider_code: str) -> str:
    """``terminal`` = 确定性失败（直接终态）；``retryable`` = 可退避重试。

    未收录的错误码一律按可重试处理；送达 worker 有 max_attempts +
    dead-letter 兜底，未知错误不会无限重试。
    """
    code = str(provider_code or "").strip()
    if code in SMS_TERMINAL_PROVIDER_CODES:
        return "terminal"
    return "retryable"


def _fingerprint(*parts: str) -> str:
    """不可逆短指纹：日志关联用，不含任何明文。"""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


class SmsPurposeNotApproved(ValueError):
    """send_verification_code 收到未登记 purpose 或越界模板/参数时抛出。

    继承 ValueError：这是调用方契约错误（编程错误），不是 provider 失败，
    绝不进入"发送失败可重试"语义。
    """


# 公共送达原语 purpose 白名单：purpose → 模板/参数约束。
#
# 原语对调用方供码与模板全透传，若不登记 purpose，任何新调用方都能把它
# 当成任意短信通道（营销/外发/模板注入），绕过模板治理。未登记 purpose、
# 越界显式模板、越界模板参数一律 fail closed（SmsPurposeNotApproved）。
#
# 登录 send_sms_code 不走本白名单（自生成码 + sms_codes SSOT，行为零变化）。
APPROVED_PURPOSES: Mapping[str, Mapping[str, object]] = {
    # 组织邀请验证码：模板固定取自 SMS_TEMPLATE_INVITE 环境配置，
    # 不允许调用方覆盖；模板参数只有 code（自动构造）。
    "organization_invite": {
        "allow_explicit_template": False,
        "require_explicit_template": False,
        "param_keys": frozenset({"code"}),
    },
    # 组织邀请链接短信：调用方必须显式给模板（来自 SMS_TEMPLATE_INVITE_LINK
    # 环境配置），模板参数只有 url。
    "organization_invite_link": {
        "allow_explicit_template": True,
        "require_explicit_template": True,
        "param_keys": frozenset({"url"}),
    },
}


def _enforce_approved_purpose(
    purpose: str,
    *,
    template_code: Optional[str],
    template_param: Optional[str],
) -> None:
    """purpose 白名单闸：任何越界都抛 SmsPurposeNotApproved，不发送。"""
    policy = APPROVED_PURPOSES.get(str(purpose or ""))
    if policy is None:
        raise SmsPurposeNotApproved(f"SMS purpose 未登记，已拒绝送达: {purpose!r}")
    explicit_template = bool(str(template_code or "").strip())
    if explicit_template and not policy["allow_explicit_template"]:
        raise SmsPurposeNotApproved(f"SMS purpose {purpose!r} 不允许调用方覆盖模板")
    if policy["require_explicit_template"] and not explicit_template:
        raise SmsPurposeNotApproved(f"SMS purpose {purpose!r} 必须显式指定已登记模板")
    if template_param is not None:
        try:
            parsed = json.loads(template_param)
        except (TypeError, ValueError) as exc:
            raise SmsPurposeNotApproved(f"SMS purpose {purpose!r} 模板参数不是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise SmsPurposeNotApproved(f"SMS purpose {purpose!r} 模板参数必须是 JSON 对象")
        unapproved = sorted(set(parsed) - set(policy["param_keys"]))
        if unapproved:
            raise SmsPurposeNotApproved(
                f"SMS purpose {purpose!r} 模板参数越界: {unapproved}"
            )


def send_verification_code(
    *,
    phone: str,
    caller_supplied_code: str,
    purpose: str,
    request_id: str,
    template_code: Optional[str] = None,
    template_param: Optional[str] = None,
) -> dict:
    """公共验证码送达原语（组织邀请等场景）。

    调用方负责生成验证码并决定存储策略；本函数只负责真实送达。
    purpose 必须在 APPROVED_PURPOSES 登记（模板/参数约束随之强制），
    未登记或越界抛 SmsPurposeNotApproved，绝不发送。

    Returns:
        成功: {"success": True, "provider_code": "OK", "receipt": "<biz_id>"}
        失败: {"success": False, "provider_code": <阿里云错误码>,
               "error": <人话>, "retryable": bool}
    """
    # purpose 白名单闸先于一切模板解析与外发：未登记 purpose / 越界模板 /
    # 越界参数都是调用方契约错误，fail closed 且不产生任何 provider 流量。
    _enforce_approved_purpose(
        purpose,
        template_code=template_code,
        template_param=template_param,
    )
    template = str(template_code or os.getenv("SMS_TEMPLATE_INVITE", "")).strip()
    phone_fp = _fingerprint("phone", phone)
    request_fp = _fingerprint(str(purpose), str(request_id))
    if not template:
        logger.warning(
            "[SMS] 送达拒绝：模板未配置 purpose=%s req=%s phone_fp=%s",
            purpose, request_fp, phone_fp,
        )
        return {
            "success": False,
            "provider_code": "SMS_TEMPLATE_NOT_CONFIGURED",
            "error": "邀请短信模板未配置",
            "retryable": False,
        }
    param = template_param
    if param is None:
        param = json.dumps({"code": str(caller_supplied_code)}, ensure_ascii=False)
    del caller_supplied_code  # 码只进入模板参数，绝不进入日志/返回值
    result = _do_send(phone, template, param)
    if result["success"]:
        logger.info(
            "[SMS] 验证码已送达 purpose=%s req=%s phone_fp=%s template=%s",
            purpose, request_fp, phone_fp, template,
        )
        return {
            "success": True,
            "provider_code": "OK",
            "receipt": str(result.get("biz_id") or ""),
        }
    code = str(result.get("provider_code") or "SMS_SEND_EXCEPTION")
    retryable = classify_sms_provider_error(code) == "retryable"
    logger.warning(
        "[SMS] 验证码送达失败 purpose=%s req=%s phone_fp=%s provider_code=%s retryable=%s",
        purpose, request_fp, phone_fp, code, retryable,
    )
    return {
        "success": False,
        "provider_code": code,
        "error": str(result.get("error") or ""),
        "retryable": retryable,
    }


def _do_send(phone: str, template_code: str, template_param: str) -> dict:
    """实际发送短信（只与阿里云交互；不写 sms_codes、不记明文手机号/验证码）"""
    try:
        from alibabacloud_dysmsapi20170525.models import SendSmsRequest
    except ImportError:
        logger.error(f"SMS exception ({template_code}): alibabacloud_dysmsapi20170525 未安装")
        return {"success": False, "provider_code": "SMS_SDK_MISSING", "error": "短信 SDK 未安装"}
    try:
        client = get_sms_client()
        request = SendSmsRequest(
            phone_numbers=phone,
            sign_name=os.getenv("SMS_SIGN_NAME", ""),
            template_code=template_code,
            template_param=template_param,
        )

        response = client.send_sms(request)
        body = response.body

        if body.code == "OK":
            return {"success": True, "provider_code": "OK", "biz_id": getattr(body, "biz_id", None)}
        return {
            "success": False,
            "provider_code": str(body.code or ""),
            "error": f"短信发送失败: {body.message}",
        }

    except RuntimeError as e:
        # _get_client 配置缺失（ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET 未配置）
        logger.error(f"SMS exception ({template_code}): {type(e).__name__}")
        return {"success": False, "provider_code": "SMS_CLIENT_CONFIG_MISSING", "error": str(e)}
    except Exception as e:
        logger.error(f"SMS exception ({template_code}): {type(e).__name__}")
        return {"success": False, "provider_code": "SMS_SEND_EXCEPTION", "error": str(e)}


def _verify_db_atomic(phone: str, code: str, purpose: str = None) -> Optional[dict]:
    """
    [GEO-R1-CAN-036] 单事务内用 SELECT ... FOR UPDATE 行锁完成
    读→判过期→判上限→比对→原子自增，防止并发下 read-check-increment-write
    非原子导致 last-write-wins（多次并发错误猜测只落一次 +1）绕过 3 次上限。

    返回结果 dict；返回 None 表示 DB 不可用，调用方走降级（Redis/内存）路径。
    """
    if not _ensure_table():
        return None
    conn = None
    try:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.cursor()
        # 行锁串行化同一 phone 的并发校验
        cur.execute(
            "SELECT code, purpose, attempts, expires_at FROM sms_codes "
            "WHERE phone = %s FOR UPDATE",
            (phone,)
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return {"success": False, "error": "请先获取验证码"}

        if time.time() > row["expires_at"]:
            cur.execute("DELETE FROM sms_codes WHERE phone = %s", (phone,))
            conn.commit()
            return {"success": False, "error": "验证码已过期，请重新获取"}

        if row["attempts"] >= 3:
            cur.execute("DELETE FROM sms_codes WHERE phone = %s", (phone,))
            conn.commit()
            return {"success": False, "error": "验证码错误次数过多，请重新获取"}

        if purpose and row["purpose"] != purpose:
            conn.commit()
            return {"success": False, "error": "验证码用途不匹配"}

        if row["code"] != code:
            # 原子条件自增：行锁下 attempts=attempts+1 保证每次错误都真实累加
            cur.execute(
                "UPDATE sms_codes SET attempts = attempts + 1 WHERE phone = %s "
                "RETURNING attempts",
                (phone,)
            )
            new_attempts = cur.fetchone()["attempts"]
            conn.commit()
            remaining = max(0, 3 - new_attempts)
            return {"success": False, "error": f"验证码错误，还有{remaining}次机会"}

        cur.execute("DELETE FROM sms_codes WHERE phone = %s", (phone,))
        conn.commit()
        return {"success": True}
    except Exception as e:
        logger.warning(f"[SMS] DB 原子校验失败，降级: {e}")
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def verify_sms_code(phone: str, code: str, purpose: str = None) -> dict:
    """
    校验短信验证码

    Returns:
        {"success": True} 或 {"success": False, "error": "错误信息"}
    """
    # [GEO-R1-CAN-036] 优先走单事务 FOR UPDATE 原子校验，防并发绕过 3 次上限
    db_result = _verify_db_atomic(phone, code, purpose)
    if db_result is not None:
        return db_result

    # DB 不可用时降级到原有非原子路径（Redis/内存 best-effort）
    record = _get_code(phone)

    if not record:
        return {"success": False, "error": "请先获取验证码"}

    if time.time() > record["expires"]:
        _delete_code(phone)
        return {"success": False, "error": "验证码已过期，请重新获取"}

    if record["attempts"] >= 3:
        _delete_code(phone)
        return {"success": False, "error": "验证码错误次数过多，请重新获取"}

    if purpose and record.get("purpose") != purpose:
        return {"success": False, "error": "验证码用途不匹配"}

    if record["code"] != code:
        record["attempts"] += 1
        remaining = 3 - record["attempts"]
        _update_attempts(phone, record)
        return {"success": False, "error": f"验证码错误，还有{remaining}次机会"}

    _delete_code(phone)
    return {"success": True}


def cleanup_expired():
    """清理过期的验证码"""
    # 清理 DB
    if _ensure_table():
        try:
            from db.connection import get_connection
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM sms_codes WHERE expires_at < %s", (time.time(),))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning(f"[SMS] DB 清理过期验证码失败: {e}")

    # 清理内存
    now = time.time()
    expired = [phone for phone, rec in _sms_codes.items() if now > rec["expires"]]
    for phone in expired:
        del _sms_codes[phone]
