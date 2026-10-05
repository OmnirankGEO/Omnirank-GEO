"""图片 → 文字(视觉模型看图 + OCR)。

E0c · 2026-09-27 从社媒主路径 router 上提,逐字搬运(`_VISION_MIME_MAP` · `_vision_image_to_text`)。
在役调用方:`services/chat_attachments.py::parse_image`(小帮识图 `POST /api/xiaobang/parse-image` 经它进来)。
旧位置原留一行 re-export 兼容垫;那个 router 已随 E3 B1b-2b 整删,垫子一起走了。

🔴 本模块不许 import 社媒代码(社媒工具包 / 社媒主路径 router / 社媒 agent)——
   函数体内的延迟 import 也算;锁见 tests/e0c_lift_vision_2026_09_27,带牙证。
"""
import os

from fastapi import HTTPException


_VISION_MIME_MAP = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif", "bmp": "bmp"}


async def _vision_image_to_text(image_bytes: bytes, original_filename: str = "image") -> str:
    """qwen3.6-plus(DashScope · 原生多模态)看图 → 文字描述 + OCR

    输入:image 原始字节 + 文件名(用于 mime 识别)
    输出:统一文字(完整 OCR + 视觉描述 · ≤ 800 字)· 之后走文本 KB 流程

    2026-05-12 R15:vision 模型可由 admin 在 settings 改(qwen3.6-plus / qwen-vl-max)
    """
    import base64
    import httpx
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="DASHSCOPE_API_KEY 未配置 · 图片转写不可用")

    # admin 可改模型 · 2026-05-18 AB 测后 fallback 改 qwen3.6-flash
    # (实测 flash 比 plus 速度快 30% · 输出多 52% · 价格便宜 63% · 唯一胜出)
    # prod admin_settings 即使没翻 vision_model='qwen3.6-flash' · 这里 fallback 也保 flash 不退 plus
    try:
        from db.social_preferences_db import get_admin_setting
        vision_model = get_admin_setting("vision_model", str, "qwen3.6-flash") or "qwen3.6-flash"
    except Exception:
        vision_model = "qwen3.6-flash"

    ext = (original_filename or "image.png").rsplit(".", 1)[-1].lower()
    mime = _VISION_MIME_MAP.get(ext, "png")
    b64 = base64.b64encode(image_bytes).decode()

    prompt_text = (
        "你是图片转写助手 · 把下面这张图变成 AI 写稿能用的文字。任务:\n"
        "1. 提取图中所有文字(完整保留 · 不要省略 · 表格保留结构)\n"
        "2. 描述图片视觉重点(谁/什么/什么数据/什么场景)\n"
        "3. 如果是聊天截图 · 标发送者和大意\n"
        "4. 如果是产品图/海报 · 描述产品和卖点\n"
        "用中文 · ≤ 800 字 · 不要寒暄"
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            from tools.llm_call_tracker import llm_track, usage_from_response_payload

            async with llm_track(
                "social_image_transcription",
                "dashscope",
                model=vision_model,
            ) as tracker:
                resp = await client.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": vision_model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
                                {"type": "text", "text": prompt_text},
                            ],
                        }],
                        "max_tokens": 1500,
                        "temperature": 0.2,
                    },
                )
                if resp.status_code == 200:
                    payload_for_usage = resp.json()
                    input_tokens, output_tokens, cached_tokens = usage_from_response_payload(payload_for_usage)
                    tracker.record(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        success=True,
                    )
                else:
                    tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"图片转写网络异常: {e}")
        if resp.status_code != 200:
            print(f"[vision] {vision_model} HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
            raise HTTPException(status_code=502, detail=f"图片转写失败 ({resp.status_code})")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            raise HTTPException(status_code=502, detail="图片转写返回格式异常")
        if not content or not isinstance(content, str):
            raise HTTPException(status_code=502, detail="图片转写返回空内容")
        return content.strip()
