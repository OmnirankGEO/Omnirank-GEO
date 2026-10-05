"""
豆包流式语音识别 (Volcengine Seed-ASR BigModel 2.0)

使用火山引擎大模型流式语音识别 API，通过 WebSocket 二进制协议实现：
1. 文件转录 - 将录音文件流式发送到 Volcengine 获取转录结果（bigmodel_nostream）
2. 实时流式转录 - 代理前端音频流到 Volcengine，返回实时文本（bigmodel_async 优化版）

环境变量：
- VOLCENGINE_ASR_APP_KEY: 火山引擎语音控制台 App ID
- VOLCENGINE_ASR_ACCESS_KEY: 火山引擎语音控制台 Access Token

定价: 1元/小时 (豆包流式语音识别模型2.0, volc.seedasr.sauc.duration)
文档: https://www.volcengine.com/docs/6561/1354869

二进制协议格式:
  请求: Header(4B) + PayloadSize(4B) + Payload
  响应: Header(4B) + [Sequence(4B)] + PayloadSize(4B) + Payload
  错误: Header(4B) + ErrorCode(4B) + ErrorMsgSize(4B) + ErrorMsg
"""

import os
import struct
import gzip
import json
import uuid
import asyncio
import logging
from typing import Dict, Any, AsyncGenerator

logger = logging.getLogger("GEO-ASR-Doubao")

# ========== WebSocket 端点 ==========

# 双向流式 - 每个音频包都返回识别结果
WS_URL_STREAM = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
# 流入单出 - 音频流式输入，最终返回完整结果（精度最高）
WS_URL_NOSTREAM = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
# 双向优化版 - 仅在文本变化时返回，性能最优（推荐实时显示用）
WS_URL_ASYNC = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"

# ========== 二进制协议常量 ==========

PROTOCOL_VERSION = 0b0001
HEADER_SIZE_WORDS = 0b0001  # 1 word = 4 bytes

# Message types (byte1 高 4 位)
MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_AUDIO_ONLY = 0b0010
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_SERVER_ACK = 0b1011
MSG_SERVER_ERROR = 0b1111

# Message type specific flags (byte1 低 4 位)
# bit0: 1=后接 sequence number
# bit1: 1=最后一包(负包)
FLAG_NONE = 0b0000
FLAG_HAS_SEQUENCE = 0b0001
FLAG_LAST_PACKET = 0b0010
FLAG_LAST_WITH_SEQ = 0b0011

# Serialization (byte2 高 4 位)
SER_NONE = 0b0000
SER_JSON = 0b0001

# Compression (byte2 低 4 位)
CMP_NONE = 0b0000
CMP_GZIP = 0b0001

# Audio 分包参数 (官方推荐: 100-200ms, 双向流式200ms最优)
CHUNK_SIZE_100MS = 3200   # 100ms at 16kHz 16-bit mono = 3200 bytes
CHUNK_SIZE_200MS = 6400   # 200ms at 16kHz 16-bit mono = 6400 bytes


def _get_credentials():
    """获取火山引擎 ASR 凭证"""
    app_key = os.environ.get("VOLCENGINE_ASR_APP_KEY", "")
    access_key = os.environ.get("VOLCENGINE_ASR_ACCESS_KEY", "")
    return app_key, access_key


def _build_header(msg_type: int, flags: int = 0,
                  serialization: int = SER_NONE,
                  compression: int = CMP_NONE) -> bytes:
    """构建 4 字节协议头"""
    byte0 = (PROTOCOL_VERSION << 4) | HEADER_SIZE_WORDS
    byte1 = (msg_type << 4) | (flags & 0x0F)
    byte2 = (serialization << 4) | (compression & 0x0F)
    byte3 = 0x00
    return bytes([byte0, byte1, byte2, byte3])


def _build_full_client_request(config: dict) -> bytes:
    """构建初始配置帧 (Header + PayloadSize + GzipJSON)"""
    payload = gzip.compress(json.dumps(config).encode("utf-8"))
    header = _build_header(
        MSG_FULL_CLIENT_REQUEST,
        flags=FLAG_NONE,
        serialization=SER_JSON,
        compression=CMP_GZIP,
    )
    return header + struct.pack(">I", len(payload)) + payload


def _build_audio_frame(audio_data: bytes, is_last: bool = False) -> bytes:
    """构建音频数据帧 (Header + PayloadSize + AudioBytes)"""
    flags = FLAG_LAST_PACKET if is_last else FLAG_NONE
    header = _build_header(
        MSG_AUDIO_ONLY,
        flags=flags,
        serialization=SER_NONE,
        compression=CMP_GZIP if audio_data else CMP_NONE,
    )
    payload = gzip.compress(audio_data) if audio_data else b""
    return header + struct.pack(">I", len(payload)) + payload


def _parse_response(data: bytes) -> dict:
    """
    解析服务端响应帧

    协议格式:
    - Full server response: Header(4) + [Sequence(4)] + PayloadSize(4) + Payload
    - Error response: Header(4) + ErrorCode(4) + ErrorMsgSize(4) + ErrorMsg
    - Sequence 存在与否由 flags bit0 决定
    """
    if len(data) < 4:
        return {"error": "response too short"}

    msg_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    serialization = (data[2] >> 4) & 0x0F
    compression = data[2] & 0x0F

    # ---- 错误响应 ----
    if msg_type == MSG_SERVER_ERROR:
        if len(data) >= 12:
            error_code = struct.unpack(">I", data[4:8])[0]
            error_msg_size = struct.unpack(">I", data[8:12])[0]
            error_msg = data[12:12 + error_msg_size]
            if serialization == SER_JSON and compression == CMP_NONE:
                # 错误信息可能是 JSON 字符串
                pass
            return {
                "error": f"code={error_code}: {error_msg.decode('utf-8', errors='replace')}"
            }
        return {"error": "unknown server error"}

    # ---- ACK ----
    if msg_type == MSG_SERVER_ACK:
        return {"type": "ack"}

    # ---- Full server response ----
    if msg_type != MSG_FULL_SERVER_RESPONSE:
        return {"type": "unknown", "msg_type": msg_type}

    offset = 4
    has_sequence = (flags & 0x01) != 0
    is_last = (flags & 0x02) != 0
    sequence = 0

    # 读取可选的 sequence number
    if has_sequence:
        if len(data) < offset + 4:
            return {"error": "response too short for sequence"}
        sequence = struct.unpack(">i", data[offset:offset + 4])[0]
        offset += 4

    # 读取 payload
    if len(data) < offset + 4:
        return {"error": "response too short for payload size"}

    payload_size = struct.unpack(">I", data[offset:offset + 4])[0]
    offset += 4
    payload = data[offset:offset + payload_size]

    if compression == CMP_GZIP and payload:
        payload = gzip.decompress(payload)

    result = {}
    if serialization == SER_JSON and payload:
        result = json.loads(payload.decode("utf-8"))
    elif payload:
        result = {"text": payload.decode("utf-8", errors="replace")}

    result["_sequence"] = sequence
    result["_is_last"] = is_last
    return result


def _make_asr_config(language: str = "zh-CN", show_utterances: bool = True) -> dict:
    """构建 ASR 请求配置"""
    return {
        "user": {"uid": "omnirank-interview"},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
            "language": language,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,      # 数字规范化
            "enable_punc": True,     # 启用标点
            "enable_ddc": False,     # 不启用语义顺滑，保留语气词
            "show_utterances": show_utterances,
            "result_type": "full",   # 全量返回
        },
    }


def _make_ws_headers(app_key: str, access_key: str) -> dict:
    """构建 WebSocket 连接头"""
    return {
        "X-Api-App-Key": app_key,
        "X-Api-Access-Key": access_key,
        # 模型2.0 小时版 Resource ID
        "X-Api-Resource-Id": "volc.seedasr.sauc.duration",
        "X-Api-Connect-Id": uuid.uuid4().hex,
    }


async def _convert_to_pcm(input_path: str) -> str:
    """用 ffmpeg 将音频转为 16kHz 16bit mono PCM (s16le)"""
    output_path = input_path + ".pcm"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", input_path,
        "-ar", "16000", "-ac", "1", "-f", "s16le",
        output_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败, returncode={proc.returncode}")
    return output_path


# ========== 公共 API ==========


async def transcribe_file(file_path: str, language: str = "zh-CN") -> Dict[str, Any]:
    """
    转录音频文件 — 流入单出模式 (bigmodel_nostream, 精度最高)

    将文件转为 PCM 后通过 WebSocket 流式发送到火山引擎，
    等待最终完整结果返回。

    Args:
        file_path: 音频文件路径 (webm/mp3/wav/m4a/ogg 等)
        language: 语言代码，默认中文

    Returns:
        {"text": "转录文本", "status": "success/error/..."}
    """
    import websockets

    app_key, access_key = _get_credentials()
    if not app_key or not access_key:
        logger.warning("未配置 VOLCENGINE_ASR_APP_KEY / VOLCENGINE_ASR_ACCESS_KEY")
        return {"text": "", "status": "no_volcengine_asr_keys"}

    # 转为 PCM
    try:
        pcm_path = await _convert_to_pcm(file_path)
    except Exception as e:
        logger.error(f"音频转码失败: {e}")
        return {"text": "", "status": f"convert_error: {e}"}

    try:
        with open(pcm_path, "rb") as f:
            pcm_data = f.read()
    finally:
        try:
            os.unlink(pcm_path)
        except OSError:
            pass

    if len(pcm_data) < 1600:
        return {"text": "", "status": "audio_too_short"}

    config = _make_asr_config(language)
    headers = _make_ws_headers(app_key, access_key)
    full_text = ""
    connect_id = headers["X-Api-Connect-Id"]

    logger.info(f"[{connect_id[:8]}] 开始文件转录, PCM大小={len(pcm_data)}B, "
                f"时长≈{len(pcm_data)/32000:.1f}s")

    try:
        async with websockets.connect(
            WS_URL_NOSTREAM,
            additional_headers=headers,
            ping_interval=None,
            close_timeout=10,
        ) as ws:
            # 发送配置帧
            await ws.send(_build_full_client_request(config))

            # 流式发送音频 (100ms 分包)
            offset = 0
            chunk_size = CHUNK_SIZE_100MS
            while offset < len(pcm_data):
                chunk = pcm_data[offset:offset + chunk_size]
                is_last = (offset + chunk_size) >= len(pcm_data)
                await ws.send(_build_audio_frame(chunk, is_last=is_last))
                offset += chunk_size
                if not is_last:
                    await asyncio.sleep(0.02)

            logger.info(f"[{connect_id[:8]}] 音频发送完毕，等待结果")

            # 接收结果
            while True:
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=30)
                    result = _parse_response(response)

                    if "error" in result:
                        logger.warning(f"[{connect_id[:8]}] ASR错误: {result['error']}")
                        if full_text:
                            break
                        return {"text": "", "status": f"asr_error: {result['error']}"}

                    if result.get("type") == "ack":
                        continue

                    # 提取文本
                    text = result.get("result", {}).get("text", "")
                    if text:
                        full_text = text

                    # 检查 _is_last 标记或所有 utterance 都是 definite
                    if result.get("_is_last"):
                        break

                    utterances = result.get("result", {}).get("utterances", [])
                    all_definite = utterances and all(
                        u.get("definite", False) for u in utterances
                    )
                    if all_definite:
                        break

                except asyncio.TimeoutError:
                    logger.info(f"[{connect_id[:8]}] 接收超时，使用已有结果")
                    break

    except Exception as e:
        logger.error(f"[{connect_id[:8]}] Volcengine ASR 连接失败: {e}")
        if full_text:
            return {"text": full_text.strip(), "status": f"partial: {e}"}
        return {"text": "", "status": f"connection_error: {e}"}

    logger.info(f"[{connect_id[:8]}] 转录完成, 文本长度={len(full_text)}")
    return {
        "text": full_text.strip(),
        "status": "success" if full_text.strip() else "empty",
    }


async def stream_transcribe(
    audio_chunks: AsyncGenerator[bytes, None],
    language: str = "zh-CN",
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    实时流式转录 — 双向流式优化版 (bigmodel_async)

    接收前端发来的 PCM 音频块，实时推送识别结果。
    使用优化版端点：仅在文本变化时返回，性能最优。

    Args:
        audio_chunks: 异步生成器，产出 PCM 音频块 (16kHz 16bit mono)
        language: 语言代码

    Yields:
        {"text": "当前识别文本", "is_final": bool}
    """
    import websockets

    app_key, access_key = _get_credentials()
    if not app_key or not access_key:
        yield {"error": "未配置火山引擎 ASR 凭证", "text": ""}
        return

    config = _make_asr_config(language)
    headers = _make_ws_headers(app_key, access_key)
    connect_id = headers["X-Api-Connect-Id"]

    logger.info(f"[{connect_id[:8]}] 开始流式转录")

    try:
        async with websockets.connect(
            WS_URL_ASYNC,  # 使用优化版双向流式端点
            additional_headers=headers,
            ping_interval=None,
            close_timeout=10,
        ) as ws:
            # 发送配置帧
            await ws.send(_build_full_client_request(config))

            # 并行：发送音频 + 接收结果
            send_done = asyncio.Event()
            full_text = ""

            async def _send_audio():
                try:
                    async for chunk in audio_chunks:
                        if chunk:
                            await ws.send(_build_audio_frame(chunk, is_last=False))
                    # 发送最后一包（负包）
                    await ws.send(_build_audio_frame(b"", is_last=True))
                    logger.info(f"[{connect_id[:8]}] 音频发送完毕（负包已发）")
                finally:
                    send_done.set()

            send_task = asyncio.create_task(_send_audio())

            try:
                while True:
                    try:
                        timeout = 5 if not send_done.is_set() else 15
                        response = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        result = _parse_response(response)

                        if "error" in result:
                            logger.warning(f"[{connect_id[:8]}] 流式ASR错误: {result['error']}")
                            yield {"error": result["error"], "text": full_text}
                            break

                        if result.get("type") == "ack":
                            continue

                        text = result.get("result", {}).get("text", "")
                        if text:
                            full_text = text

                        # 判断是否为最终结果
                        is_final_response = result.get("_is_last", False)
                        utterances = result.get("result", {}).get("utterances", [])
                        all_definite = utterances and all(
                            u.get("definite", False) for u in utterances
                        )

                        yield {
                            "text": full_text,
                            "is_final": is_final_response or (all_definite and send_done.is_set()),
                        }

                        if is_final_response or (all_definite and send_done.is_set()):
                            break

                    except asyncio.TimeoutError:
                        if send_done.is_set():
                            logger.info(f"[{connect_id[:8]}] 流式接收超时")
                            yield {"text": full_text, "is_final": True}
                            break
            finally:
                send_task.cancel()
                try:
                    await send_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        logger.error(f"[{connect_id[:8]}] 流式ASR异常: {e}")
        yield {"error": str(e), "text": ""}

    logger.info(f"[{connect_id[:8]}] 流式转录结束, 文本长度={len(full_text)}")


def is_available() -> bool:
    """检查豆包 ASR 是否已配置"""
    app_key, access_key = _get_credentials()
    return bool(app_key and access_key)
