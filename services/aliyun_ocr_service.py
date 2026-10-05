"""
身份证 OCR 识别服务（DashScope vanchin/deepseek-ocr）

通道: DashScope OpenAI Compatible Mode
模型: vanchin/deepseek-ocr (多模态图像理解 + 文字识别)
鉴权: 复用现有 DASHSCOPE_API_KEY, 无需新开账号

设计:
- OCR 接收 OSS key, 内部生成 5 分钟临时签名 URL 交给 DashScope 拉取
  * DashScope 与 OSS 同属阿里云, 签名 URL 短时暴露给 DashScope 服务器风险可接受
  * 身份证原始字节不出平台 (字节不经前端 base64 传给 DashScope, 降低泄露面)
- 识别返回结构化 JSON, 失败时尝试 json_repair 修复
- 置信度启发式: name + id_card_no 都有 → 0.95; 缺一 → 0.70; 都无 → 0.0
- 所有异常全部包装为 OCRServiceError, 让上层统一处理

用法:
    from services.aliyun_ocr_service import recognize_id_card
    result = recognize_id_card(oss_key="applications/42/7/front_xxx.jpg", side="front")
    # result["name"], result["id_card_no"], result["confidence"]
"""

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("GEO-OCR")


# 临时签名 URL 的有效期（秒）- DashScope 拉图片用, 短一点更安全
_SIGNED_URL_EXPIRES = 300  # 5 分钟

# OCR 模型名
_OCR_MODEL = "vanchin/deepseek-ocr"

# DashScope OpenAI 兼容 endpoint
_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


# Prompt 模板: 引导模型返回结构化 JSON
_FRONT_PROMPT = """你是身份证 OCR 识别助手。请识别这张中国居民身份证**正面**照片的内容，严格按下列 JSON 格式返回。

要求:
1. 只返回 JSON，不要任何解释、前缀、markdown 围栏
2. 识别不到的字段填 null
3. 身份证号必须是 18 位字符串（含末位 X）
4. 出生日期用 YYYY-MM-DD 格式

```
{
  "name": "姓名",
  "id_card_no": "18位身份证号",
  "gender": "男|女|null",
  "nation": "民族",
  "birthday": "YYYY-MM-DD",
  "address": "住址"
}
```
"""

_BACK_PROMPT = """你是身份证 OCR 识别助手。请识别这张中国居民身份证**反面（国徽面）**照片的内容，严格按下列 JSON 格式返回。

要求:
1. 只返回 JSON，不要任何解释、前缀、markdown 围栏
2. 识别不到的字段填 null
3. 有效期限用原始字符串（如 "2019.05.28-2039.05.28" 或 "长期"）

```
{
  "issue_authority": "签发机关",
  "valid_period": "有效期限"
}
```
"""


class OCRServiceError(RuntimeError):
    """OCR 识别失败（网络 / 鉴权 / 解析）"""


class OCRConfigError(RuntimeError):
    """OCR 未配置（DASHSCOPE_API_KEY 缺失）"""


def _get_openai_client():
    """Lazy 构造 OpenAI 客户端（指向 DashScope）"""
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise OCRConfigError("DASHSCOPE_API_KEY 未配置")

    try:
        from openai import OpenAI
    except ImportError as e:
        raise OCRConfigError(f"openai SDK 未安装: {e}")

    return OpenAI(api_key=api_key, base_url=_DASHSCOPE_BASE_URL)


def _extract_json(raw_text: str) -> Optional[dict]:
    """从 LLM 返回中提取 JSON（宽容处理 markdown 围栏、前后缀文字）"""
    if not raw_text:
        return None

    text = raw_text.strip()

    # 去 markdown 围栏
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        # 去前后非 JSON 文字, 保留第一个 {...}
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 最后兜底: json_repair（项目依赖里有）
    try:
        import json_repair
        repaired = json_repair.loads(text)
        if isinstance(repaired, dict):
            return repaired
    except Exception:
        pass

    return None


def _compute_confidence(parsed: Optional[dict], side: str) -> float:
    """启发式置信度: 关键字段齐全则高, 否则低"""
    if not parsed:
        return 0.0

    if side == "front":
        name = (parsed.get("name") or "").strip()
        idno = (parsed.get("id_card_no") or "").strip()
        if name and idno and len(idno) in (15, 18):
            return 0.95
        if name or idno:
            return 0.70
        return 0.30
    elif side == "back":
        auth = (parsed.get("issue_authority") or "").strip()
        valid = (parsed.get("valid_period") or "").strip()
        if auth and valid:
            return 0.90
        if auth or valid:
            return 0.65
        return 0.30
    return 0.0


def recognize_id_card(oss_key: str, side: str) -> dict:
    """识别身份证图片（OSS key 输入）

    Args:
        oss_key: 存储在私有 bucket 的对象 key
        side:    'front' (人像面) / 'back' (国徽面)

    Returns:
        dict:
          正面: {name, id_card_no, gender, nation, birthday, address, confidence, raw_text}
          反面: {issue_authority, valid_period, confidence, raw_text}

    Raises:
        OCRConfigError: DASHSCOPE_API_KEY 未配置
        OCRServiceError: 网络 / 模型 / 解析失败
    """
    if side not in ("front", "back"):
        raise ValueError(f"side 必须是 'front' 或 'back', 得到 {side}")

    # 1. 生成临时签名 URL（给 DashScope 拉取）
    try:
        from services.oss_service import generate_signed_url
        image_url = generate_signed_url(oss_key, expires_seconds=_SIGNED_URL_EXPIRES)
    except Exception as e:
        raise OCRServiceError(f"生成 OSS 签名 URL 失败: {e}") from e

    # 2. 调 DashScope OCR
    client = _get_openai_client()
    prompt = _FRONT_PROMPT if side == "front" else _BACK_PROMPT

    try:
        from tools.llm_call_tracker import llm_track_sync

        with llm_track_sync(
            caller="aliyun_ocr",
            platform="dashscope",
            model=_OCR_MODEL,
            metadata={"side": side},
        ) as tracker:
            completion = client.chat.completions.create(
                model=_OCR_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url, "detail": "high"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                temperature=0.0,  # OCR 不需要创造力
            )
            usage = getattr(completion, "usage", None)
            tracker.record(
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
                cached_tokens=0,
                success=True,
            )
    except Exception as e:
        logger.error(f"[OCR] DashScope 调用失败 key={oss_key} side={side}: {type(e).__name__}: {e}")
        raise OCRServiceError(f"DashScope 调用失败: {e}") from e

    raw_text = (completion.choices[0].message.content or "").strip() if completion.choices else ""

    # 3. 解析 JSON
    parsed = _extract_json(raw_text)

    # 4. 合成返回
    confidence = _compute_confidence(parsed, side)

    if side == "front":
        result = {
            "name": (parsed or {}).get("name"),
            "id_card_no": (parsed or {}).get("id_card_no"),
            "gender": (parsed or {}).get("gender"),
            "nation": (parsed or {}).get("nation"),
            "birthday": (parsed or {}).get("birthday"),
            "address": (parsed or {}).get("address"),
            "confidence": confidence,
            "raw_text": raw_text,
        }
    else:  # back
        result = {
            "issue_authority": (parsed or {}).get("issue_authority"),
            "valid_period": (parsed or {}).get("valid_period"),
            "confidence": confidence,
            "raw_text": raw_text,
        }

    logger.info(
        f"[OCR] 完成 key={oss_key} side={side} confidence={confidence:.2f}"
    )
    return result


def validate_id_card_checksum(id_card_no: str) -> bool:
    """校验 18 位身份证号的末位校验码（GB 11643-1999）

    15 位老身份证不在本函数支持范围（现代申请不应接受）.
    """
    if not id_card_no or len(id_card_no) != 18:
        return False

    body = id_card_no[:17]
    if not body.isdigit():
        return False

    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    checksum_codes = "10X98765432"

    total = sum(int(ch) * w for ch, w in zip(body, weights))
    expected = checksum_codes[total % 11]

    return id_card_no[-1].upper() == expected
