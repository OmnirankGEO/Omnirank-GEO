"""
ASR 语音转写工具
使用 DashScope Qwen ASR 模型，支持短视频和长视频
"""

import httpx
import json
import asyncio
from typing import Literal
from agentscope.tool import ToolResponse

import sys
sys.path.append('../..')
from config.model_config import DASHSCOPE_CONFIG


# ASR API 端点
ASR_SYNC_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
ASR_ASYNC_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/filetrans"


async def transcribe_short_video(
    audio_url: str,
    enable_itn: bool = False,
    duration_seconds: int = 0,
) -> ToolResponse:
    """
    短视频同步转写（≤5分钟）
    使用 qwen3-asr-flash 模型
    
    Args:
        audio_url (str): 音频/视频 URL（需公网可访问）
        enable_itn (bool): 是否启用逆文本正则化
        
    Returns:
        ToolResponse: 包含转写结果的响应
    """
    api_key = DASHSCOPE_CONFIG["api_key"]
    
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=180) as client:
            metadata = {"enable_itn": enable_itn}
            if duration_seconds:
                metadata["audio_seconds"] = duration_seconds

            async with llm_track(
                "asr_transcription",
                "dashscope",
                model="qwen3-asr-flash",
                metadata=metadata,
            ) as tracker:
                response = await client.post(
                    ASR_SYNC_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "qwen3-asr-flash",
                        "input": {
                            "messages": [
                                {"content": [{"text": ""}], "role": "system"},
                                {"content": [{"audio": audio_url}], "role": "user"}
                            ]
                        },
                        "parameters": {
                            "asr_options": {
                                "enable_itn": enable_itn
                            }
                        }
                    }
                )
                data = response.json() if response.status_code == 200 else {}
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=response.status_code < 400,
                    error_msg=None if response.status_code < 400 else response.text[:200],
                )
            
            if response.status_code != 200:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error: HTTP {response.status_code} - {response.text}"}]
                )
            
            # 提取转写文本
            try:
                text = data.get("output", {}).get("choices", [{}])[0].get("message", {}).get("content", [{}])[0].get("text", "")
            except (KeyError, IndexError):
                text = json.dumps(data, ensure_ascii=False)
            
            return ToolResponse(
                content=[{"type": "text", "text": text}]
            )
    except Exception as e:
        return ToolResponse(
            content=[{"type": "text", "text": f"Error: {str(e)}"}]
        )


async def submit_long_video_transcription(
    audio_url: str,
    duration_seconds: int = 0,
) -> ToolResponse:
    """
    提交长视频异步转写任务（>5分钟，≤12小时）
    使用 qwen3-asr-flash-filetrans 模型
    
    Args:
        audio_url (str): 音频/视频 URL（需公网可访问）
        
    Returns:
        ToolResponse: 包含任务 ID 的响应
    """
    api_key = DASHSCOPE_CONFIG["api_key"]
    
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=60) as client:
            metadata = {}
            if duration_seconds:
                metadata["audio_seconds"] = duration_seconds

            async with llm_track(
                "asr_filetrans_submit",
                "dashscope",
                model="qwen3-asr-flash-filetrans",
                metadata=metadata,
            ) as tracker:
                response = await client.post(
                    ASR_ASYNC_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "X-DashScope-Async": "enable"
                    },
                    json={
                        "model": "qwen3-asr-flash-filetrans",
                        "input": {
                            "file_url": audio_url
                        }
                    }
                )
                data = response.json() if response.status_code == 200 else {}
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=response.status_code < 400,
                    error_msg=None if response.status_code < 400 else response.text[:200],
                )
            
            if response.status_code != 200:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error: HTTP {response.status_code} - {response.text}"}]
                )
            
            task_id = data.get("output", {}).get("task_id", "")
            
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps({
                    "task_id": task_id,
                    "status": "submitted",
                    "message": "长视频转写任务已提交，请稍后查询结果"
                }, ensure_ascii=False)}]
            )
    except Exception as e:
        return ToolResponse(
            content=[{"type": "text", "text": f"Error: {str(e)}"}]
        )


async def get_transcription_result(
    task_id: str
) -> ToolResponse:
    """
    获取长视频转写任务结果
    
    Args:
        task_id (str): 任务 ID
        
    Returns:
        ToolResponse: 包含转写结果或状态的响应
    """
    api_key = DASHSCOPE_CONFIG["api_key"]
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                headers={
                    "Authorization": f"Bearer {api_key}"
                }
            )
            
            if response.status_code != 200:
                return ToolResponse(
                    content=[{"type": "text", "text": f"Error: HTTP {response.status_code} - {response.text}"}]
                )
            
            data = response.json()
            status = data.get("output", {}).get("task_status", "UNKNOWN")
            
            if status == "SUCCEEDED":
                # 获取转写结果
                results = data.get("output", {}).get("results", [])
                transcripts = []
                for result in results:
                    transcript = result.get("transcript", "")
                    transcripts.append(transcript)
                
                return ToolResponse(
                    content=[{"type": "text", "text": "\n".join(transcripts)}]
                )
            elif status == "RUNNING":
                return ToolResponse(
                    content=[{"type": "text", "text": json.dumps({
                        "status": "running",
                        "message": "转写任务正在进行中，请稍后再查询"
                    }, ensure_ascii=False)}]
                )
            else:
                return ToolResponse(
                    content=[{"type": "text", "text": json.dumps({
                        "status": status,
                        "data": data
                    }, ensure_ascii=False)}]
                )
    except Exception as e:
        return ToolResponse(
            content=[{"type": "text", "text": f"Error: {str(e)}"}]
        )


async def transcribe_video(
    audio_url: str,
    duration_seconds: int = 0
) -> ToolResponse:
    """
    智能 ASR 路由 - 根据视频时长自动选择模型
    
    Args:
        audio_url (str): 音频/视频 URL
        duration_seconds (int): 视频时长（秒），如果为0则默认使用短视频模型
        
    Returns:
        ToolResponse: 包含转写结果的响应
    """
    # 短视频阈值：5分钟 = 300秒
    SHORT_VIDEO_THRESHOLD = 300
    
    if duration_seconds <= SHORT_VIDEO_THRESHOLD:
        # 短视频：同步转写
        return await transcribe_short_video(audio_url, duration_seconds=duration_seconds)
    else:
        # 长视频：异步转写
        submit_response = await submit_long_video_transcription(audio_url, duration_seconds=duration_seconds)
        
        # 解析任务 ID
        try:
            result = json.loads(submit_response.content[0]["text"])
            task_id = result.get("task_id")
            
            if not task_id:
                return submit_response
            
            # 轮询等待结果（最多等待10分钟）
            max_wait = 600
            poll_interval = 10
            elapsed = 0
            
            while elapsed < max_wait:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                
                result_response = await get_transcription_result(task_id)
                result_text = result_response.content[0]["text"]
                
                # 检查是否完成
                try:
                    result_data = json.loads(result_text)
                    if result_data.get("status") == "running":
                        continue
                except json.JSONDecodeError:
                    # 纯文本结果，说明转写完成
                    return result_response
                
                return result_response
            
            return ToolResponse(
                content=[{"type": "text", "text": json.dumps({
                    "status": "timeout",
                    "task_id": task_id,
                    "message": "转写任务超时，请手动查询结果"
                }, ensure_ascii=False)}]
            )
        except Exception as e:
            return ToolResponse(
                content=[{"type": "text", "text": f"Error: {str(e)}"}]
            )
