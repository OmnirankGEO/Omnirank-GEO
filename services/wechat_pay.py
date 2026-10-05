"""
微信支付 V3 服务层
支持 Native 扫码支付 + H5 支付
使用微信支付公钥模式（不是平台证书模式）
"""
import os
import time
import json
import uuid
import logging
import base64
from datetime import datetime, timedelta
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
import httpx

from services.payment_amounts import yuan_to_fen

logger = logging.getLogger("GEO-WechatPay")

# ===== 配置 =====
# 商户参数(商户号 / AppID / 证书序列号 / APIv3 密钥 / 平台公钥)一律从环境变量读,见 .env.example;
# 没配时微信支付不可用,其余功能照常。

MCH_ID = os.getenv("WX_MCH_ID", "")
APPID = os.getenv("WX_APPID", "")  # 服务号 AppID(JSAPI 必需)
SERIAL_NO = os.getenv("WX_SERIAL_NO", "")
APIV3_KEY = os.getenv("WX_APIV3_KEY", "")
PUB_KEY_ID = os.getenv("WX_PUB_KEY_ID", "")
# [WO_331 · 2026-10-03] 环境变量仍优先;没设或为空串(dotenv 会把 KEY= 注入成空串)时用唯一出处 + 原路径
#   (线上两个变量都显式设了 ⇒ 不变)
from services.owned_image_policy import public_base_origin as _public_base_origin
NOTIFY_URL = os.getenv("WX_NOTIFY_URL") or f"{_public_base_origin()}/api/wallet/wechat-callback"
REFUND_NOTIFY_URL = os.getenv("WX_REFUND_NOTIFY_URL") or f"{_public_base_origin()}/api/wallet/wechat-refund-callback"

# 密钥文件路径（容器内）
_KEY_PATH = os.getenv("WX_KEY_PATH", "/app/apiclient_key.pem")
_PUB_KEY_PATH = os.getenv("WX_PUB_KEY_PATH", "/app/pub_key.pem")

# ===== 加载私钥 =====

_private_key = None


def _get_private_key():
    global _private_key
    if _private_key is None:
        for path in [_KEY_PATH, "apiclient_key.pem", "/app/apiclient_key.pem"]:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    _private_key = serialization.load_pem_private_key(
                        f.read(), password=None, backend=default_backend()
                    )
                logger.info(f"微信支付私钥已加载: {path}")
                break
        if _private_key is None:
            raise RuntimeError("找不到微信支付私钥文件 apiclient_key.pem")
    return _private_key


def _get_pub_key():
    """加载微信支付公钥（用于验签）"""
    for path in [_PUB_KEY_PATH, "pub_key.pem", "/app/pub_key.pem"]:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return serialization.load_pem_public_key(f.read(), backend=default_backend())
    raise RuntimeError("找不到微信支付公钥文件 pub_key.pem")


# ===== 签名 =====

def _sign(message: str) -> str:
    """用商户私钥对消息签名（SHA256-RSA2048）"""
    key = _get_private_key()
    signature = key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _build_auth_header(method: str, url_path: str, body: str = "") -> str:
    """构建 Authorization 头"""
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"
    signature = _sign(message)
    return (
        f'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{MCH_ID}",'
        f'nonce_str="{nonce}",'
        f'signature="{signature}",'
        f'timestamp="{timestamp}",'
        f'serial_no="{SERIAL_NO}"'
    )


# ===== 验签（回调用）=====

def verify_callback(timestamp: str, nonce: str, body: str, signature: str) -> bool:
    """用微信支付公钥验证回调签名"""
    try:
        pub_key = _get_pub_key()
        message = f"{timestamp}\n{nonce}\n{body}\n"
        sig_bytes = base64.b64decode(signature)
        pub_key.verify(
            sig_bytes,
            message.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception as e:
        logger.error(f"验签失败: {e}")
        return False


def decrypt_callback(ciphertext: str, nonce: str, associated_data: str) -> dict:
    """解密回调通知的加密数据（AES-256-GCM）"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = APIV3_KEY.encode("utf-8")
    nonce_bytes = nonce.encode("utf-8")
    ad = associated_data.encode("utf-8") if associated_data else b""
    data = base64.b64decode(ciphertext)

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce_bytes, data, ad)
    return json.loads(plaintext.decode("utf-8"))


# ===== 下单 =====

async def create_native_order(
    out_trade_no: str,
    total_yuan: float,
    description: str = "OmniRank AI 积分充值",
    notify_url: str = None,        # V3.1 Phase 07: 订阅订单可覆盖,默认走充值 callback
    attach: str = None,            # V3.1 Phase 07: 透传字段,callback 原样返
) -> dict:
    """
    Native 支付（电脑扫码）
    返回: {"code_url": "weixin://wxpay/..."}

    V3.1 Phase 07 扩展:
      - notify_url: 可指定订单专属 callback URL(默认 NOTIFY_URL 充值用)
      - attach: 透传字段, 用于区分订单类型(如 'subscription:user_id=N')
    """
    if not APPID:
        raise RuntimeError("WX_APPID 未配置")
    url_path = "/v3/pay/transactions/native"
    total_fen = yuan_to_fen(total_yuan)  # [WO_299] 精确换算,不截断

    payload = {
        "appid": APPID,
        "mchid": MCH_ID,
        "out_trade_no": out_trade_no,
        "description": description[:127],
        "notify_url": notify_url or NOTIFY_URL,
        "amount": {
            "total": total_fen,
            "currency": "CNY",
        },
        "time_expire": (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }
    if attach:
        payload["attach"] = str(attach)[:128]

    body = json.dumps(payload, ensure_ascii=False)

    auth = _build_auth_header("POST", url_path, body)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.mch.weixin.qq.com{url_path}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": auth,
            },
        )

    if resp.status_code == 200:
        return resp.json()
    else:
        logger.error(f"微信下单失败: {resp.status_code} {resp.text}")
        raise RuntimeError(f"微信支付下单失败: {resp.text[:200]}")


async def create_h5_order(
    out_trade_no: str,
    total_yuan: float,
    payer_ip: str,
    description: str = "OmniRank AI 积分充值",
    notify_url: str = None,        # V3.1 Phase 07
    attach: str = None,            # V3.1 Phase 07
) -> dict:
    """
    H5 支付（手机浏览器跳转）
    返回: {"h5_url": "https://wx.tenpay.com/..."}

    ⚠️ H5 支付需要商户号先开通 H5 产品权限
       (SaaS / 数字内容服务类目微信整体不批 · 防退款监管 · 不是个人能改的)
    本函数保留代码 · 但调用方应优先选 wechat_native / wechat_jsapi
    若调本函数会返 403 NO_AUTH "商户号该产品权限预开通中"
    api/wallet_api._detect_channel 已绕过此函数 · 手机非微信直接 xunhupay
    未来若微信类目政策变化或开放申请,本函数可立即重新启用
    """
    if not APPID:
        raise RuntimeError("WX_APPID 未配置")
    url_path = "/v3/pay/transactions/h5"
    total_fen = yuan_to_fen(total_yuan)  # [WO_299] 精确换算,不截断

    payload = {
        "appid": APPID,
        "mchid": MCH_ID,
        "out_trade_no": out_trade_no,
        "description": description[:127],
        "notify_url": notify_url or NOTIFY_URL,
        "amount": {
            "total": total_fen,
            "currency": "CNY",
        },
        "time_expire": (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "scene_info": {
            "payer_client_ip": payer_ip,
            "h5_info": {
                "type": "Wap",
            },
        },
    }
    if attach:
        payload["attach"] = str(attach)[:128]

    body = json.dumps(payload, ensure_ascii=False)

    auth = _build_auth_header("POST", url_path, body)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.mch.weixin.qq.com{url_path}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": auth,
            },
        )

    if resp.status_code == 200:
        return resp.json()
    else:
        logger.error(f"微信H5下单失败: {resp.status_code} {resp.text}")
        raise RuntimeError(f"微信H5支付下单失败: {resp.text[:200]}")


async def query_order(out_trade_no: str) -> dict:
    """查询订单状态"""
    url_path = f"/v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={MCH_ID}"
    auth = _build_auth_header("GET", url_path)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.mch.weixin.qq.com{url_path}",
            headers={
                "Accept": "application/json",
                "Authorization": auth,
            },
        )

    if resp.status_code == 200:
        return resp.json()
    else:
        logger.error(f"查询订单失败: {resp.status_code} {resp.text}")
        return {"trade_state": "UNKNOWN"}


# ===== JSAPI 支付(公众号内 / 服务号网页 · 老板 2026-04-25 配置)=====

async def create_jsapi_order(
    out_trade_no: str,
    total_yuan: float,
    openid: str,
    description: str = "OmniRank AI 积分充值",
    attach: str = None,
    notify_url: str = None,        # V3.1 Phase 07: 订阅订单可覆盖
) -> dict:
    """JSAPI 下单(服务号公众号内 / 网页授权拿到 openid 后调用)

    流程:
      1. 用户在公众号网页授权 → 拿 openid(snsapi_base 即可)
      2. 调本函数 → 拿 prepay_id
      3. 前端 wx.requestPayment 用 build_jsapi_invoke_params 拿调起参数
      4. 用户授权支付 → 微信回调 NOTIFY_URL

    Args:
        out_trade_no: 商户订单号(本地生成 · 唯一)
        total_yuan: 金额(元)
        openid: 用户在 AppID 下的 openid(服务号网页授权获取)
        description: 商品描述(<=127 字)
        attach: 商户自定义透传字段(回调原样返 · 可放 user_id 等)

    Returns:
        {"prepay_id": "wx2024..."}
    """
    if not openid:
        raise ValueError("JSAPI 支付必须传 openid · 先做服务号网页授权")
    if not APPID:
        raise RuntimeError("WX_APPID 未配置 · 老板检查 .env(服务号 AppID)")

    url_path = "/v3/pay/transactions/jsapi"
    total_fen = yuan_to_fen(total_yuan)  # [WO_299] 精确换算,不截断

    payload: dict = {
        "appid": APPID,
        "mchid": MCH_ID,
        "out_trade_no": out_trade_no,
        "description": description[:127],
        "notify_url": notify_url or NOTIFY_URL,
        "amount": {
            "total": total_fen,
            "currency": "CNY",
        },
        "payer": {
            "openid": openid,
        },
        "time_expire": (datetime.now() + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }
    if attach:
        payload["attach"] = str(attach)[:128]

    body = json.dumps(payload, ensure_ascii=False)
    auth = _build_auth_header("POST", url_path, body)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.mch.weixin.qq.com{url_path}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": auth,
            },
        )

    if resp.status_code == 200:
        data = resp.json()
        prepay_id = data.get("prepay_id")
        if not prepay_id:
            raise RuntimeError(f"微信 JSAPI 下单未返 prepay_id: {data}")
        return data
    else:
        logger.error(f"微信 JSAPI 下单失败: {resp.status_code} {resp.text}")
        raise RuntimeError(f"微信 JSAPI 下单失败: {resp.text[:200]}")


def build_jsapi_invoke_params(prepay_id: str) -> dict:
    """构造前端 wx.requestPayment 调起参数(含商户私钥签名)

    前端 JSAPI 调用需:
      {appId, timeStamp, nonceStr, package, signType:'RSA', paySign}

    Args:
        prepay_id: create_jsapi_order 返回的 prepay_id

    Returns:
        前端直接传给 wx.requestPayment 的 dict
    """
    if not APPID:
        raise RuntimeError("WX_APPID 未配置")
    if not prepay_id:
        raise ValueError("prepay_id 必填")

    timestamp = str(int(time.time()))
    nonce_str = uuid.uuid4().hex
    package = f"prepay_id={prepay_id}"

    # 签名 message 4 行(微信文档要求 · appid/timestamp/nonceStr/package · 末尾换行)
    message = f"{APPID}\n{timestamp}\n{nonce_str}\n{package}\n"
    pay_sign = _sign(message)

    return {
        "appId": APPID,
        "timeStamp": timestamp,
        "nonceStr": nonce_str,
        "package": package,
        "signType": "RSA",
        "paySign": pay_sign,
    }


# ===== 退款 =====

async def refund_order(
    out_trade_no: str,
    out_refund_no: str,
    refund_yuan: float,
    total_yuan: float,
    reason: str = "用户申请退款",
) -> dict:
    """申请退款(V3)

    Args:
        out_trade_no: 商户订单号(原支付单 · 必填)
        out_refund_no: 商户退款单号(本地生成 · 唯一)
        refund_yuan: 退款金额(元)
        total_yuan: 原订单金额(元)
        reason: 退款原因(<=80 字)

    Returns:
        {refund_id, status, ...} · status='SUCCESS'/'PROCESSING'/'ABNORMAL'
    """
    url_path = "/v3/refund/domestic/refunds"
    refund_fen = yuan_to_fen(refund_yuan)  # [WO_299] 精确换算,不截断
    total_fen = yuan_to_fen(total_yuan)  # [WO_299] 精确换算,不截断

    if refund_fen <= 0 or refund_fen > total_fen:
        raise ValueError(f"退款金额 ¥{refund_yuan} 无效(原单 ¥{total_yuan})")

    body = json.dumps({
        "out_trade_no": out_trade_no,
        "out_refund_no": out_refund_no,
        "reason": reason[:80],
        "notify_url": REFUND_NOTIFY_URL,
        "amount": {
            "refund": refund_fen,
            "total": total_fen,
            "currency": "CNY",
        },
    }, ensure_ascii=False)

    auth = _build_auth_header("POST", url_path, body)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.mch.weixin.qq.com{url_path}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": auth,
            },
        )

    if resp.status_code == 200:
        return resp.json()
    else:
        logger.error(f"微信退款申请失败: {resp.status_code} {resp.text}")
        raise RuntimeError(f"微信退款失败: {resp.text[:200]}")


async def query_refund(out_refund_no: str, *, strict: bool = False) -> dict:
    """查退款状态(对账用)。

    strict=True 供资金执行器使用：仅把微信明确的 RESOURCE_NOT_EXISTS
    视为“尚未发起”，其余网关/鉴权异常必须抛错，禁止把查询故障误当成
    可以再次打款的依据。
    """
    url_path = f"/v3/refund/domestic/refunds/{out_refund_no}"
    auth = _build_auth_header("GET", url_path)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://api.mch.weixin.qq.com{url_path}",
            headers={
                "Accept": "application/json",
                "Authorization": auth,
            },
        )

    if resp.status_code == 200:
        return resp.json()
    try:
        body = resp.json()
    except Exception:
        body = {}
    if resp.status_code == 404 and str(body.get("code") or "") == "RESOURCE_NOT_EXISTS":
        return {"status": "NOT_FOUND", "out_refund_no": str(out_refund_no)}
    logger.error(f"查询退款失败: {resp.status_code} {resp.text}")
    if strict:
        raise RuntimeError(f"微信退款主动查询失败: HTTP {resp.status_code}")
    return {"status": "UNKNOWN"}


# ===== 服务号网页授权(获取 openid · JSAPI 前置)=====

def build_oauth_authorize_url(redirect_uri: str, state: str = "") -> str:
    """生成服务号网页授权链接(snsapi_base · 静默 · 仅拿 openid)

    Args:
        redirect_uri: 授权后跳回的页面(必须在公众号"网页授权域名"白名单 · 当前 omnirank.top)
        state: 业务侧透传(回调 query 原样返)

    Returns:
        微信授权链接 · 用户访问 → 微信跳回 redirect_uri?code=XXX&state=XXX
    """
    if not APPID:
        raise RuntimeError("WX_APPID 未配置")
    from urllib.parse import quote
    encoded_uri = quote(redirect_uri, safe="")
    state_part = f"&state={quote(state)}" if state else ""
    return (
        f"https://open.weixin.qq.com/connect/oauth2/authorize"
        f"?appid={APPID}"
        f"&redirect_uri={encoded_uri}"
        f"&response_type=code"
        f"&scope=snsapi_base"
        f"{state_part}"
        f"#wechat_redirect"
    )


async def fetch_openid_by_code(code: str) -> dict:
    """用授权 code 换 openid(服务号 snsapi_base)

    需要服务号 AppSecret(WX_APP_SECRET) · 老板线下提供
    Returns: {access_token, expires_in, refresh_token, openid, scope}
    """
    app_secret = os.getenv("WX_APP_SECRET", "")
    if not app_secret:
        raise RuntimeError("WX_APP_SECRET 未配置 · 老板线下提供服务号 AppSecret")
    url = (
        f"https://api.weixin.qq.com/sns/oauth2/access_token"
        f"?appid={APPID}&secret={app_secret}&code={code}&grant_type=authorization_code"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
    data = resp.json() if resp.status_code == 200 else {}
    if data.get("errcode"):
        raise RuntimeError(f"换 openid 失败: {data}")
    return data
