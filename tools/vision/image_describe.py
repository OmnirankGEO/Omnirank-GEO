"""
客户图片素材 · 结构化视觉识别(image_describe)

复用 social 的 DashScope 视觉调用模式(现 `services/image_to_text._vision_image_to_text`),
但输出「结构化 JSON」而非纯文字——给客户图库资产打标:
  这是什么图 / 适合放哪 / 图中文字 / 标签 / 外发风险。

- 视觉模型走 admin 配置 **geo_vision_model**(GEO 线专用键;共享的 vision_model 留给社媒)
- 命中风险(二维码/手机号/隐私/水印/无关)→ publish_allowed 建议 0(默认不外发)
- 识别失败保守降级(不抛错 · 不中断上传 · 图已传上来不能丢 · 默认不外发等人工补)

2026-06-02 GEO CTO · 客户资料中心图片素材能力
"""

import os
import base64
import logging
import inspect
from typing import Callable, Optional

from config.vision_routing import resolve_vision_target

logger = logging.getLogger("GEO-ImageDescribe")

#: 🔴 [WO_220-c1] 端点 / key / platform / thinking 四样由 `config.vision_routing`
#:   单点给出 —— 本文件里**不许再出现端点字面量**。
#:   两个 GEO 视觉消费方(本模块与 services/geo_douyin/ocr_qa.py)各写一遍时,
#:   不一致的表现是「一个走新线、一个走旧线」,而没有任何东西会报错。
_VISION_MIME_MAP = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif", "bmp": "bmp"}

# 枚举白名单(LLM 自由发挥时兜回合法值)
_IMAGE_TYPES = {"logo", "product", "case", "certificate", "team", "storefront", "environment", "other"}
_PLACEMENTS = {"hero", "brand_intro", "product_desc", "case_proof", "not_for_external"}
_SCENARIOS = {"brand_intro", "product_desc", "case_proof", "team_intro", "environment_show"}
_RISK_FLAGS = {"qrcode", "phone", "privacy", "watermark", "irrelevant"}

_PROMPT = (
    "你是品牌图片素材分析助手。看这张图,判断它能不能、适不适合放进「对外发布的文章」里,并提取关键信息。\n"
    "只输出一个 JSON 对象(不要 markdown 代码块,不要多余解释),字段如下:\n"
    "- image_type: 从 [logo, product, case, certificate, team, storefront, environment, other] 选最贴切的一个"
    "(logo=品牌标志, product=产品图, case=客户案例/作品成果, certificate=证书/资质/奖项, "
    "team=团队/人物, storefront=门店/店面门头, environment=经营环境/场地, other=其它)\n"
    "- title: 一句话人话标题(给不懂技术的人看,如「门店外观」「产品全家福」「营业执照」)\n"
    "- caption: 放在文章里图片下方的一句配图说明\n"
    "- alt_text: 图片替代文字(无障碍/SEO 用)\n"
    "- vision_summary: 这张图在表现什么(2-3 句话)\n"
    "- ocr_text: 图中所有文字,完整保留不要省略;没有文字就给空字符串\n"
    "- suggested_placement: 从 [hero(文章首图), brand_intro(品牌介绍), product_desc(产品说明), "
    "case_proof(案例证明), not_for_external(不适合对外)] 选一个\n"
    "- usage_scenarios: 数组,这张图能用在哪些文章场景,从 [brand_intro, product_desc, case_proof, "
    "team_intro, environment_show] 选 0 个或多个\n"
    "- tags: 数组,3-6 个中文标签\n"
    "- risk_flags: 数组,命中以下风险就列出,从 [qrcode(含二维码), phone(含手机号/座机/电话), "
    "privacy(含身份证/清晰人脸/车牌等个人隐私), watermark(含其它平台水印/台标/账号), "
    "irrelevant(与品牌经营无关的随手拍/表情包/纯截图)] 选;干净无风险就给空数组 []\n"
    "用中文。"
)


def _fallback(original_filename: str, reason: str) -> dict:
    """识别失败的保守降级:图已传上来不能丢,但默认不外发,等人工补。"""
    logger.warning(f"[image_describe] 视觉识别降级: {reason}")
    return {
        "image_type": "other",
        "title": (original_filename or "图片").rsplit(".", 1)[0][:40],
        "caption": "",
        "alt_text": "",
        "vision_summary": "（AI 自动识别未完成,可手动补充说明）",
        "ocr_text": "",
        "suggested_placement": "not_for_external",
        "usage_scenarios": [],
        "tags": [],
        "risk_flags": [],
        "publish_allowed": 0,   # 保守:识别失败不默认外发
        "vision_ok": False,
    }


def _normalize(raw: dict, original_filename: str) -> dict:
    """白名单校验 + 风险联动 publish_allowed。"""
    def _s(v):
        return v.strip() if isinstance(v, str) else ""

    image_type = _s(raw.get("image_type")).lower()
    if image_type not in _IMAGE_TYPES:
        image_type = "other"

    placement = _s(raw.get("suggested_placement")).lower()
    if placement not in _PLACEMENTS:
        placement = "not_for_external"

    scenarios = [x for x in (raw.get("usage_scenarios") or []) if isinstance(x, str) and x in _SCENARIOS]
    risk_flags = [x for x in (raw.get("risk_flags") or []) if isinstance(x, str) and x in _RISK_FLAGS]
    tags = [x.strip() for x in (raw.get("tags") or []) if isinstance(x, str) and x.strip()][:8]

    # 🔴 风险联动:命中任一风险 → 默认不允许对外发布(等用户显式确认 rights_confirmed 才放行)
    publish_allowed = 0 if risk_flags else 1
    if placement == "not_for_external":
        publish_allowed = 0

    return {
        "image_type": image_type,
        "title": _s(raw.get("title")) or (original_filename or "图片").rsplit(".", 1)[0][:40],
        "caption": _s(raw.get("caption")),
        "alt_text": _s(raw.get("alt_text")),
        "vision_summary": _s(raw.get("vision_summary")),
        "ocr_text": _s(raw.get("ocr_text")),
        "suggested_placement": placement,
        "usage_scenarios": scenarios,
        "tags": tags,
        "risk_flags": risk_flags,
        "publish_allowed": publish_allowed,
        "vision_ok": True,
    }


async def describe_image_structured(
    image_bytes: bytes,
    original_filename: str = "image.png",
    *,
    before_provider_call: Optional[Callable[[], object]] = None,
) -> dict:
    """看图 → 结构化资产标注 dict。失败保守降级(不抛错 · 不中断上传)。"""
    import httpx
    # This exception is a security/charging control-flow signal, not a vision
    # provider failure. It must cross this legacy fail-soft boundary unchanged
    # so the durable image attempt and organization charge can converge.
    from services.marketing.image_client import LiveAuthorityRejected

    target = resolve_vision_target()
    model = target.model

    api_key = os.environ.get(target.api_key_env)
    if not api_key:
        return _fallback(original_filename, f"{target.api_key_env} 未配置")

    ext = (original_filename or "image.png").rsplit(".", 1)[-1].lower()
    mime = _VISION_MIME_MAP.get(ext, "png")
    b64 = base64.b64encode(image_bytes).decode()

    content = None
    payload = None
    echoed = None
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload
        async with httpx.AsyncClient(timeout=60.0) as client:
            #: platform 跟着端点走 —— 记错行会让成本落到另一个供应商名下(WO_214 同族)
            async with llm_track("brand_image_describe", target.platform, model=model) as tracker:
                if before_provider_call is not None:
                    try:
                        guarded = before_provider_call()
                        if inspect.isawaitable(guarded):
                            await guarded
                    except LiveAuthorityRejected:
                        raise
                    except Exception as exc:
                        raise LiveAuthorityRejected(str(exc)) from exc
                resp = await client.post(
                    target.endpoint,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                                {"type": "text", "text": _PROMPT},
                            ],
                        }],
                        "max_tokens": 1500,
                        "temperature": 0.2,
                        #: 🔴 DeepSeek 线必须带 thinking disabled —— 复杂图的推理会把
                        #:   1500 token 预算吃光,content 为空、finish=length,
                        #:   线上静默落 _fallback,表现为「识别失败」。百炼线这里是空字典。
                        **target.extra_body,
                    },
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    echoed = str(payload.get("model") or "").strip()
                    it, ot, ct = usage_from_response_payload(payload)
                    if echoed and echoed != model:
                        #: 🔴 回显 != 请求名 = 供应商静默换了模型。按新价计费、HTTP 200、
                        #:   账单不异常 ⇒ 两个多月无人发现过一次(本仓
                        #:   a-200-can-hide-a-silently-substituted-model)。记成失败并降级:
                        #:   宁可这张图标「未识别」等人工补,也不要把一个不知道是谁
                        #:   给出的描述当成资产标注写进库。
                        tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct,
                                       success=False,
                                       error_msg=f"回显模型 {echoed} != 请求 {model}")
                    else:
                        tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
                else:
                    tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            return _fallback(original_filename, f"HTTP {resp.status_code}")
        if echoed and echoed != model:
            return _fallback(original_filename, f"回显模型不符:{echoed} != {model}")
        content = payload["choices"][0]["message"]["content"]
    except LiveAuthorityRejected:
        raise
    except Exception as e:
        return _fallback(original_filename, f"网络/调用异常: {e}")

    if not content or not isinstance(content, str):
        return _fallback(original_filename, "返回空内容")

    # 提取 JSON 主体(剥 markdown / 前后噪音)+ json_repair 兜底
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        from json_repair import repair_json
        obj = repair_json(text, return_objects=True)
    except Exception as e:
        return _fallback(original_filename, f"JSON 解析失败: {e}")
    if not isinstance(obj, dict):
        return _fallback(original_filename, "返回非 JSON 对象")

    return _normalize(obj, original_filename)
