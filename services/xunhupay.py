"""虎皮椒聚合支付对接（微信支付通道）

API 文档: https://www.xunhupay.com/doc/api/pay.html

核心差异（vs 微信官方 V3）:
- 签名算法：MD5(k1=v1&k2=v2&... + APPSECRET)，不是 RSA
- 金额单位：元（不是分）
- 统一接口：一次请求返 url（手机跳转）+ url_qrcode（PC 扫码）
- 回调：POST form 表单 + MD5 验签，成功返回纯文本 "success"
- 支付/退款回调状态：OD(已支付) / CD(已退款) / RD(退款中) / UD(退款失败)
- 通用订单查询状态是另一份契约：OD(已支付) / WP(待支付) / CD(已取消)

安全：APPSECRET 只从环境变量 XUNHUPAY_APPSECRET 读取，绝不硬编码。
"""
import os
import time
import hashlib
import secrets
import logging
from typing import Dict, Optional

import aiohttp

from services.payment_amounts import fen_to_yuan_str, yuan_to_fen

logger = logging.getLogger("GEO-Xunhupay")

# ========== 配置（全部来自环境变量） ==========

XUNHUPAY_APPID = os.environ.get("XUNHUPAY_APPID", "")
XUNHUPAY_APPSECRET = os.environ.get("XUNHUPAY_APPSECRET", "")
XUNHUPAY_GATEWAY = os.environ.get(
    "XUNHUPAY_GATEWAY",
    "https://api.xunhupay.com/payment/do.html",
)
# [WO_331 · 2026-10-03] 环境变量仍优先;没设或为空串(dotenv 会把 KEY= 注入成空串)时用唯一出处 + 原路径
#   (线上两个变量都显式设了 ⇒ 不变)
from services.owned_image_policy import public_base_origin as _public_base_origin
XUNHUPAY_NOTIFY_URL = (os.environ.get("XUNHUPAY_NOTIFY_URL")
                       or f"{_public_base_origin()}/api/wallet/xunhupay-callback")
XUNHUPAY_RETURN_URL = (os.environ.get("XUNHUPAY_RETURN_URL")
                       or f"{_public_base_origin()}/wallet?payment=success")

# 退款接口/退款回调状态映射。通用订单查询另有 OD/WP/CD 契约，
# 其中 CD 仅表示订单已取消，不能复用本组常量判断退款成功。
STATUS_PAID = "OD"
STATUS_REFUNDED = "CD"
STATUS_REFUNDING = "RD"
STATUS_REFUND_FAILED = "UD"


# ========== 签名算法 ==========

def is_force_xunhupay() -> bool:
    """[2026-06-06 紧急] 营业执照重审期 · system_settings.FORCE_XUNHUPAY=true 时全微信渠道改走 xunhupay
    翻 false 即恢复微信直连(无需 deploy · 每次调用查 DB · 无 cache)
    """
    from db.connection import get_db
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM system_settings WHERE key=%s", ("FORCE_XUNHUPAY",))
            row = cur.fetchone()
            if not row:
                return False
            val = row.get("value") if isinstance(row, dict) else row[0]
            return str(val).strip().lower() == "true"
    except Exception:
        return False


def _generate_hash(params: Dict, appsecret: str) -> str:
    """MD5 签名：key 按 ASCII 升序，拼 key=value&...，末尾加 APPSECRET，md5 32 位小写

    - 排除 hash 字段本身
    - 排除空值（None / '' / 不排除 0 和 False）
    """
    items = sorted(
        (k, v) for k, v in params.items()
        if k != "hash" and v is not None and v != ""
    )
    raw = "&".join(f"{k}={v}" for k, v in items)
    return hashlib.md5((raw + appsecret).encode("utf-8")).hexdigest()


#: 查询响应里可能出现的签名键。文档只写 `hash`;其余是防厂商换名的前瞻。
#: 🔴 只有 `hash` 能用 `_generate_hash` 验 —— 它的排除规则写死了 `k != "hash"`,
#:    换个键名就会把签名本身也拼进待签串,算出来必然不匹配。
#:    所以命中非 `hash` 的键时**不假装验过**,按"签名方案未知"处理(见下)。
_QUERY_SIGNATURE_KEYS = ("hash", "sign", "signature", "sig", "checksum")

#: 未签名查询响应的计数(#170)。给运维一个"厂商到底签没签"的可观测量;
#: 厂商哪天开始签,这个数就不再增长,而条件验签会自动收紧。
UNSIGNED_QUERY_RESPONSES = 0


def _find_query_signature(top: Dict, nested: Dict):
    """返回 (键名, 位置) 或 (None, None)。顶层优先。"""
    for k in _QUERY_SIGNATURE_KEYS:
        if top.get(k):
            return k, "top"
        if nested.get(k):
            return k, "nested"
    return None, None


def _query_signature_matches(top: Dict, nested: Dict, key: str) -> bool:
    """按文档口径验查询响应的签名。

    两种口径都试,命中其一即过:
      ① 只对**顶层**字段算(文档示例的形状:errcode/errmsg/data/hash);
      ② 对**摊平后**(顶层 + 嵌套 data)算。
    厂商没写清签的是哪一层,所以两种都试;**都不中才算不匹配**。
    """
    incoming = str(top.get(key) or nested.get(key) or "").lower()
    if not incoming:
        return False
    for candidate in ({k: v for k, v in top.items() if k != "data"},
                      {**{k: v for k, v in top.items() if k != "data"}, **nested}):
        if _generate_hash(candidate, XUNHUPAY_APPSECRET).lower() == incoming:
            return True
    return False


def verify_xunhupay_callback(params: Dict) -> bool:
    """验证回调签名（注意：传入 params 已包含 hash 字段）"""
    if not XUNHUPAY_APPSECRET:
        logger.error("XUNHUPAY_APPSECRET 未配置，无法验签")
        return False
    incoming = (params.get("hash") or "").lower()
    if not incoming:
        return False
    expected = _generate_hash(params, XUNHUPAY_APPSECRET).lower()
    match = incoming == expected
    if not match:
        logger.warning(f"虎皮椒回调签名不匹配: got={incoming[:8]}... expected={expected[:8]}...")
    return match


# ========== 发起支付 ==========

async def create_xunhupay_order(
    order_id: str,
    amount_yuan: float,
    title: str = "OmniRank AI 积分充值",
    attach: str = "",
    plugins: str = "omnirank",
    notify_url: str = None,        # V3.1 Phase 07: 订阅订单可覆盖
) -> Dict:
    """调虎皮椒发起微信支付下单

    返回:
        {
            "url_qrcode": "https://...",       # PC 扫码
            "url": "https://...",              # 手机跳转
            "openid": 订单ID,                   # 虎皮椒内部订单号
            "errcode": 0,
            "errmsg": "success!"
        }

    失败抛 Exception（含虎皮椒 errmsg）
    """
    if not XUNHUPAY_APPID or not XUNHUPAY_APPSECRET:
        raise RuntimeError("虎皮椒支付未配置（需要环境变量 XUNHUPAY_APPID + XUNHUPAY_APPSECRET）")

    params = {
        "version": "1.1",
        "appid": XUNHUPAY_APPID,
        "trade_order_id": order_id,
        # [WO_299] 先精确换成分再格式化:`:.2f` 会把「本来不是两位小数」的金额悄悄四舍五入
        "total_fee": fen_to_yuan_str(yuan_to_fen(amount_yuan)),
        "title": title[:126],  # 文档要求 ≤127 字符
        "time": str(int(time.time())),
        "notify_url": notify_url or XUNHUPAY_NOTIFY_URL,
        "return_url": XUNHUPAY_RETURN_URL,
        "nonce_str": secrets.token_hex(16),
        "plugins": plugins,
    }
    if attach:
        params["attach"] = attach

    params["hash"] = _generate_hash(params, XUNHUPAY_APPSECRET)

    logger.info(f"[虎皮椒] 发起支付: order_id={order_id}, amount={amount_yuan}")

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(XUNHUPAY_GATEWAY, data=params) as resp:
                text = await resp.text()
                # 虎皮椒响应是 JSON 字符串
                import json as _json
                data = _json.loads(text)
    except aiohttp.ClientError as e:
        logger.error(f"[虎皮椒] 网关请求失败: {e}")
        raise RuntimeError(f"支付网关不可达: {e}")
    except Exception as e:
        logger.error(f"[虎皮椒] 响应解析失败: {e}, body={text[:200] if 'text' in dir() else 'N/A'}")
        raise RuntimeError(f"支付响应异常: {e}")

    # 官方支付响应包含 hash；支付链接只有验签通过后才能交给客户端。
    if not verify_xunhupay_callback(data):
        logger.error("[虎皮椒] 支付创建响应签名无效，拒绝返回支付链接")
        raise RuntimeError("虎皮椒支付响应签名无效")

    errcode = data.get("errcode", -1)
    if errcode != 0:
        errmsg = data.get("errmsg", "未知错误")
        logger.error(f"[虎皮椒] 下单失败: errcode={errcode}, errmsg={errmsg}")
        raise RuntimeError(f"虎皮椒下单失败: {errmsg}")

    logger.info(f"[虎皮椒] 下单成功: order_id={order_id}, xhp_order={data.get('openid')}")
    return data


# ========== 发起整单退款 / 查询订单状态 ==========

async def refund_xunhupay_order(order_id: str, reason: str = "用户申请退款") -> Dict:
    """发起虎皮椒整单退款。

    官方接口没有退款金额参数，因此调用方必须先证明持久退款金额等于原单全额；
    部分退款必须转人工资金工单，不能把它伪装成全额渠道退款。
    """
    if not XUNHUPAY_APPID or not XUNHUPAY_APPSECRET:
        raise RuntimeError("虎皮椒退款未配置（需要 XUNHUPAY_APPID + XUNHUPAY_APPSECRET）")
    refund_url = os.environ.get(
        "XUNHUPAY_REFUND_URL",
        "https://api.xunhupay.com/payment/refund.html",
    )
    params = {
        "appid": XUNHUPAY_APPID,
        "trade_order_id": str(order_id),
        "reason": str(reason or "用户申请退款")[:80],
        "time": str(int(time.time())),
        "nonce_str": secrets.token_hex(16),
    }
    params["hash"] = _generate_hash(params, XUNHUPAY_APPSECRET)
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(refund_url, data=params) as resp:
                text = await resp.text()
                import json as _json
                data = _json.loads(text)
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"虎皮椒退款网关不可达: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"虎皮椒退款响应异常: {exc}") from exc
    if not verify_xunhupay_callback(data):
        raise RuntimeError("虎皮椒退款响应签名无效")
    if int(data.get("errcode", -1)) != 0:
        raise RuntimeError(f"虎皮椒退款失败: {data.get('errmsg', '未知错误')}")
    if str(data.get("trade_order_id") or "") != str(order_id):
        raise RuntimeError("虎皮椒退款响应订单号不一致")
    return data


async def query_xunhupay_order(order_id: str, *, strict: bool = False) -> Optional[Dict]:
    """查询通用订单状态，不查询退款终态。

    虎皮椒查询网关: https://api.xunhupay.com/payment/query.html

    返回:
        {
            "out_trade_order": "...",
            "open_order_id": "...",
            "transaction_id": "...",
            "total_fee": "1.00",
            "status": "OD",     # OD 已支付 / WP 待支付 / CD 已取消
            ...
        }
    通用查询没有 refund_status/refund_fee；其 CD 绝不能作为退款成功证据。
    查不到返 None。
    """
    if not XUNHUPAY_APPID or not XUNHUPAY_APPSECRET:
        return None

    query_url = os.environ.get(
        "XUNHUPAY_QUERY_URL",
        "https://api.xunhupay.com/payment/query.html",
    )

    params = {
        "appid": XUNHUPAY_APPID,
        # Current official query contract uses out_trade_order. Refund creation
        # uses trade_order_id; the two endpoint parameter names are different.
        "out_trade_order": order_id,
        "time": str(int(time.time())),
        "nonce_str": secrets.token_hex(16),
    }
    params["hash"] = _generate_hash(params, XUNHUPAY_APPSECRET)

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(query_url, data=params) as resp:
                text = await resp.text()
                import json as _json
                data = _json.loads(text)
        if data.get("errcode") == 0:
            # 🔴 [#170 · 2026-09-10] 订单号回显校验**必须先于验签**。
            #
            #    改前的顺序是"先验签、再校验订单号"。而实测(Deploy 只读取证 §F)
            #    查询响应里取不到 `hash`,`verify_xunhupay_callback` 在
            #    `if not incoming: return False` 处就退出 ⇒ 验签**恒 False**
            #    ⇒ 下面这段订单号校验**从来没有执行过**。
            #    它才是这条路上真正在挡"网关回了别人那张单"的守卫,却被一道
            #    恒假的闸挡在后面 —— 一个从不运行的守卫和不存在没有区别。
            #
            #    ⚠️ 本笔**不削弱任何校验**:验签原样保留在下面,strict 语义不变
            #    (回显不一致 → 抛;验签不过 → 仍抛)。只是把先后换过来,
            #    让回显校验开始工作,并让失败原因说得准(原来一切都报"签名无效")。
            #
            #    验签口径本身**未决**,见 #170:厂商文档
            #    https://www.xunhupay.com/doc/api/search.html
            #    明确写着查询响应带顶层 `hash`(成功与失败示例都带),
            #    与实测「顶层无 hash」**冲突**。三种可能(打错网关 / 网关漂离文档 /
            #    探针漏看)各自的修法不同,**在查清之前不许据"没有签名"降级校验**。
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            normalized = {**data, **nested}
            response_order_id = str(
                normalized.get("trade_order_id")
                or normalized.get("out_trade_order")
                or ""
            )
            if response_order_id != str(order_id):
                if strict:
                    raise RuntimeError("虎皮椒主动查询响应订单号不一致")
                logger.warning("[虎皮椒] 主动查询响应订单号不一致 —— 丢弃")
                return None

            # 🔴 [#170 · 2026-09-10] **条件验签**:签了就按文档验,没签才退到回显。
            #
            #    厂商文档 https://www.xunhupay.com/doc/api/search.html 写着查询响应
            #    带顶层 `hash`(成功与失败示例都带)。**实测不带**(Deploy 只读取证:
            #    同一 host、`XUNHUPAY_QUERY_URL` 未设、顶层键原样
            #    `['data','errcode','errmsg']`、嵌套 10 键、五种签名候选键全无)
            #    ⇒ 排除"打错网关"与"探针漏看",是**网关实际行为与文档不符**。
            #
            #    ⚠️ 不写死"查询不验签":那等于把厂商今天的一个缺陷固化成我们的契约。
            #    这里按**响应里有没有签名**分流 —— 厂商哪天开始签,**本段自动收紧**,
            #    不需要谁记得回来改。
            #
            #    🔴 回调路径(`verify_xunhupay_callback`)**逐字未动**,仍然强制验签。
            #    回调是钱到账的凭据,查询只是我们主动去问一句;两者的风险不对等。
            sig_key, sig_where = _find_query_signature(data, nested)
            if sig_key == "hash":
                if not _query_signature_matches(data, nested, sig_key):
                    if strict:
                        raise RuntimeError("虎皮椒主动查询响应签名无效")
                    logger.warning("[虎皮椒] 主动查询响应签名无效")
                    return None
            elif sig_key is not None:
                # 命中了一个我们不会算的签名键 —— **不假装验过**。
                # `_generate_hash` 只排除 `hash`,换个键名算出来必然不匹配,
                # 那种"不匹配"是我们算错,不是对方伪造,拿它拒单会误伤。
                logger.warning(
                    "[虎皮椒] 查询响应带未知签名键 %s(%s)—— 本版不会算,"
                    "按未签名路径处理;需按厂商文档补算法", sig_key, sig_where)

            if sig_key != "hash":
                # 未签名(或签名方案未知):只能靠传输层 + 回显。
                # 回显一致性已在上面校过(订单号),这里补 HTTPS 与 appid。
                if not str(query_url).lower().startswith("https://"):
                    if strict:
                        raise RuntimeError("虎皮椒主动查询:响应未签名且通道非 HTTPS")
                    logger.warning("[虎皮椒] 查询响应未签名且通道非 HTTPS —— 丢弃")
                    return None
                # ⚠️ 实测响应里**没有 appid**(顶层 3 键 + 嵌套 10 键都没有),
                #    所以这条今天**不会触发**。写成"有才校"而不是"必须有":
                #    要求一个对方从不返回的字段 = 这条路永远走不通;
                #    而假装校过一个不存在的字段 = 一道恒真的闸,比没有更糟。
                echoed_appid = str(normalized.get("appid") or "")
                if echoed_appid and echoed_appid != str(XUNHUPAY_APPID):
                    if strict:
                        raise RuntimeError("虎皮椒主动查询响应 appid 不一致")
                    logger.warning("[虎皮椒] 查询响应 appid 不一致 —— 丢弃")
                    return None
                global UNSIGNED_QUERY_RESPONSES
                UNSIGNED_QUERY_RESPONSES += 1
                logger.warning(
                    "xunhupay query response unsigned · order=%s · 累计=%d "
                    "(文档称应带 hash,实测不带;靠 HTTPS + 订单号回显放行)",
                    order_id, UNSIGNED_QUERY_RESPONSES)
            return normalized
        logger.warning(f"[虎皮椒] 查询订单失败: {data.get('errmsg')}")
        return None
    except Exception as e:
        logger.error(f"[虎皮椒] 查询订单异常: {e}")
        if strict:
            raise
        return None
