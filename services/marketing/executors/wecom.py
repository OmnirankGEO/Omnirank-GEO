"""
services/marketing/executors/wecom.py — 企业微信群机器人 webhook 触达适配器

契约(§C.1 渠道 adapter 接口):send(text) -> (ok: bool, detail: str)。
webhook URL 走 env MARKETING_WECOM_WEBHOOK(密钥零硬编码);未配置 → (False,'not_configured')。
短信 / 微信服务号只建接口位不实现对接(资质问题非代码问题):见 send_sms / send_wecom_service。
"""
import logging
import os

logger = logging.getLogger("GEO-Marketing-WeCom")


def send_wecom(text: str, mentioned: list | None = None) -> tuple[bool, str]:
    """企微群机器人 webhook 发文本。env 未配置 → not_configured。"""
    url = os.environ.get("MARKETING_WECOM_WEBHOOK", "").strip()
    if not url:
        return False, "not_configured"
    try:
        import httpx
        body = {"msgtype": "text", "text": {"content": text[:2000]}}
        if mentioned:
            body["text"]["mentioned_list"] = mentioned
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(url, json=body)
        if resp.status_code == 200 and resp.json().get("errcode", 0) == 0:
            return True, "ok"
        return False, f"http {resp.status_code}: {resp.text[:120]}"
    except Exception as e:  # noqa: BLE001
        logger.warning("[wecom] 发送失败: %s", e)
        return False, str(e)[:120]


# —— 接口位(不实现对接;资质到位后填充)——
def send_sms(phone: str, text: str) -> tuple[bool, str]:
    """短信适配器接口位。契约:send_sms(phone, text) -> (ok, detail)。
    v1 不实现(营销短信需签名报备)。返回 not_implemented,调用方按'待接入'处理。"""
    return False, "not_implemented"


def send_wecom_service(openid: str, template_id: str, data: dict) -> tuple[bool, str]:
    """微信服务号模板消息接口位。契约:send_wecom_service(openid, template_id, data) -> (ok, detail)。
    v1 不实现(服务号推送体系不存在,已实测坐实)。返回 not_implemented。"""
    return False, "not_implemented"
