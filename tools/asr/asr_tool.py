"""
ASR 音频转录工具
使用阿里云 DashScope qwen3-asr-flash 模型进行音视频转文字

功能:
1. 从音频/视频 URL 提取文字
2. 批量转录多个视频
3. 从 TikHub 视频数据中提取音频 URL
"""

import os
import json
import ssl
import base64
import asyncio
import httpx
from typing import List, Dict, Any, Optional
from urllib import request

from tools.tikhub_cost_tracking import tracked_tikhub_get


# DashScope ASR 配置
DASHSCOPE_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
ASR_MODEL = "qwen3-asr-flash"

# 文件扩展名 → MIME 类型映射
_MIME_MAP = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


def normalize_duration_seconds(value: Any, *, assume_milliseconds: bool = False) -> float:
    """Normalize seconds or milliseconds from social APIs to seconds."""
    try:
        duration = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if assume_milliseconds or duration >= 10000:
        duration = duration / 1000
    return max(duration, 0)


def _local_file_to_data_uri(filepath: str) -> str:
    """将本地音视频文件转为 base64 data URI，供 DashScope ASR 使用"""
    ext = os.path.splitext(filepath)[1].lower()
    mime = _MIME_MAP.get(ext, "audio/mpeg")
    with open(filepath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def transcribe_audio_sync(
    audio_url: str,
    api_key: str = None,
    duration_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    同步转录音频 (使用 urllib，与 Dify 代码一致)

    Args:
        audio_url: 音频/视频 URL 或本地文件路径
        api_key: DashScope API Key (可选，默认从环境变量获取)

    Returns:
        {"text": "转录文本", "status": "success/error/empty"}
    """
    if not audio_url:
        return {"text": "", "status": "empty"}

    if not api_key:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")

    if not api_key:
        return {"text": "", "status": "no_api_key"}

    # 本地文件 → base64 data URI
    audio_ref = audio_url
    if not audio_url.startswith(("http://", "https://", "data:")):
        if os.path.isfile(audio_url):
            try:
                audio_ref = _local_file_to_data_uri(audio_url)
            except Exception as e:
                return {"text": "", "status": f"file_read_error: {e}"}
        else:
            return {"text": "", "status": f"file_not_found: {audio_url}"}

    # 禁用 SSL 证书验证 (与 Dify 代码一致)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": ASR_MODEL,
        "input": {
            "messages": [
                {"role": "system", "content": [{"text": ""}]},
                {"role": "user", "content": [{"audio": audio_ref}]}
            ]
        },
        "parameters": {
            "result_format": "message",
            "asr_options": {
                "language": "zh",
                "enable_itn": False
            }
        }
    }

    try:
        from tools.llm_call_tracker import llm_track_sync, usage_from_response_payload

        req = request.Request(
            DASHSCOPE_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )

        metadata = {"mode": "sync_urllib"}
        if duration_seconds:
            metadata["audio_seconds"] = duration_seconds

        with llm_track_sync(
            caller="asr_transcription",
            platform="dashscope",
            model=ASR_MODEL,
            metadata=metadata,
        ) as tracker:
            with request.urlopen(req, timeout=120, context=ssl_context) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(result)
            tracker.record(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                success=True,
            )
            choices = result.get("output", {}).get("choices", [])

            if choices:
                content = choices[0].get("message", {}).get("content", [])
                if content:
                    text = content[0].get("text", "")
                    if text:
                        return {"text": text, "status": "success"}

        return {"text": "", "status": "no_content"}

    except Exception as e:
        return {"text": "", "status": str(e)[:200]}


async def transcribe_audio_async(
    audio_url: str,
    api_key: str = None,
    duration_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    异步转录音频 (使用 httpx)
    
    Args:
        audio_url: 音频/视频 URL
        api_key: DashScope API Key
    
    Returns:
        {"text": "转录文本", "status": "success/error/empty"}
    """
    if not audio_url:
        return {"text": "", "status": "empty"}
    
    if not api_key:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    
    if not api_key:
        return {"text": "", "status": "no_api_key"}
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": ASR_MODEL,
        "input": {
            "messages": [
                {"role": "system", "content": [{"text": ""}]},
                {"role": "user", "content": [{"audio": audio_url}]}
            ]
        },
        "parameters": {
            "result_format": "message",
            "asr_options": {
                "language": "zh",
                "enable_itn": False
            }
        }
    }
    
    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        metadata = {"mode": "async_httpx"}
        if duration_seconds:
            metadata["audio_seconds"] = duration_seconds

        async with httpx.AsyncClient(timeout=90.0, verify=False) as client:
            async with llm_track(
                "asr_transcription",
                "dashscope",
                model=ASR_MODEL,
                metadata=metadata,
            ) as tracker:
                response = await client.post(
                    DASHSCOPE_API_URL,
                    headers=headers,
                    json=payload
                )
                result = response.json() if response.status_code == 200 else {}
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(result)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=response.status_code < 400,
                    error_msg=None if response.status_code < 400 else response.text[:200],
                )
            
            if response.status_code == 200:
                choices = result.get("output", {}).get("choices", [])

                if choices:
                    content = choices[0].get("message", {}).get("content", [])
                    if content:
                        text = content[0].get("text", "")
                        if text:
                            return {"text": text, "status": "success"}
                        else:
                            print(f"[ASR] 返回200但文本为空, audio_url={audio_url[:60]}...")
                            return {"text": "", "status": "empty_text"}

                print(f"[ASR] 返回200但无choices/content, resp={str(result)[:200]}")
                return {"text": "", "status": "no_content"}
            else:
                # 返回详细错误信息
                error_body = ""
                try:
                    error_body = response.text[:500]
                except:
                    error_body = "unable to read"
                
                # 检查是否是音频过长/文件过大错误
                error_lower = error_body.lower()
                if ("too long" in error_lower or
                    "audio is too long" in error_lower or
                    "file size is too large" in error_lower or
                    "multimodal file size" in error_lower):
                    # 如果已经是音频URL（mp3/ies-music/douyinstatic），直接回退长视频模型
                    if ".mp3" in audio_url or "ies-music" in audio_url or ("douyinstatic" in audio_url and "/video" not in audio_url):
                        print(f"[ASR] 音频过长，使用长音频模型（MP3直链）...")
                        return await transcribe_long_video_async(audio_url, duration=duration_seconds or 0)
                    # 否则是视频流URL，不自动回退（让调用方替换为MP3再重试）
                    print(f"[ASR] 文件过大且为视频流URL，返回需要MP3替换标记")
                    return {"text": "", "status": "need_mp3", "error_detail": "短视频模型拒绝，需要MP3音频URL"}
                
                return {"text": "", "status": f"http_{response.status_code}", "error_detail": error_body[:200]}
    
    except Exception as e:
        return {"text": "", "status": str(e)[:100]}


def extract_audio_url_from_douyin(video_data: Dict) -> Optional[str]:
    """
    从抖音视频数据中提取音频 URL
    
    优先级（重要！DashScope ASR只接受音频文件URL）:
    1. music.play_url - 原声/背景音乐的MP3文件（DashScope兼容）
    2. 顶层 audio_url - 简化结构
    3. video.play_addr - 视频URL（包含音轨，部分ASR可用）
    
    注意：对于"原声"类型的视频（is_original_sound=true），
    music.play_url 实际上就是博主的讲话内容，而非背景音乐。
    """
    # [最高优先级] music.play_url - 音频文件，DashScope兼容
    # 抖音的 music.play_url 即使标注为"原声"也是音频格式
    music = video_data.get("music")
    if music:
        play_url = music.get("play_url", {})
        if isinstance(play_url, str) and play_url:
            return play_url
        url_list = play_url.get("url_list", []) if isinstance(play_url, dict) else []
        if url_list:
            return url_list[0]  # 直接返回，不再限制 .mp3/ies-music
        # 备用：music.audio_url
        if music.get("audio_url"):
            return music.get("audio_url")
        # 备用：music.uri 直链
        if music.get("uri"):
            uri = music["uri"]
            if uri.startswith("http"):
                return uri

    # [次优先级] 顶层 audio_url
    if video_data.get("audio_url"):
        return video_data.get("audio_url")

    # [最低优先级] video.play_addr - 视频URL，长视频ASR可能超时
    video = video_data.get("video", {})
    play_addr = video.get("play_addr", {})
    video_urls = play_addr.get("url_list", [])
    if video_urls:
        return video_urls[0]

    return None


def extract_audio_url_from_xhs(note_data: Dict) -> Optional[str]:
    """
    从小红书笔记数据中提取音频/视频 URL
    
    小红书视频笔记结构可能包含:
    - video.media.stream.h264[0].master_url
    """
    video = note_data.get("video", {})
    media = video.get("media", {})
    stream = media.get("stream", {})
    
    # H264 流
    h264 = stream.get("h264", [])
    if h264:
        return h264[0].get("master_url")
    
    # 备用结构
    if video.get("url"):
        return video.get("url")
    
    return None


async def batch_transcribe_videos(
    videos: List[Dict],
    platform: str = "douyin",
    max_count: int = 10,
    max_concurrent: int = 20
) -> List[Dict[str, Any]]:
    """
    批量转录视频（并行优化版）

    Args:
        videos: 视频数据列表
        platform: 平台 (douyin/xiaohongshu)
        max_count: 最大转录数量
        max_concurrent: 最大并发数（默认20）
    
    Returns:
        转录结果列表
    
    动态模型选择：
    - 短视频 (<5分钟): qwen3-asr-flash
    - 长视频 (>=5分钟): qwen3-asr-flash-filetrans
    """
    
    # 信号量控制并发
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def transcribe_single(video: Dict, index: int) -> Dict[str, Any]:
        """转录单个视频（带信号量控制）"""
        async with semaphore:
            # 获取视频时长（抖音返回毫秒，需要转换为秒）
            duration_raw = video.get("duration", 0) or video.get("video", {}).get("duration", 0) or 0
            duration = normalize_duration_seconds(duration_raw, assume_milliseconds=True)
            # 跳过超过30分钟的视频（直播回放等）
            if duration > 1800:
                print(f"  ⏭️ 跳过超长视频 ({duration//60}分钟): {vid}")
                return {
                    "video_id": vid,
                    "text": "",
                    "status": "skipped_too_long",
                    "duration": duration,
                    "skip_reason": f"超过30分钟({duration//60}分钟)"
                }

            is_long_video = video.get("_asr_use_long_model", False) or duration > 300

            model_hint = "长视频模型(filetrans)" if is_long_video else "短视频模型(flash)"
            duration_info = f"({duration//60}分{duration%60}秒)" if duration > 0 else ""
            print(f"  🎙️ 转录视频 {index+1}/{min(len(videos), max_count)} {duration_info} [{model_hint}]...")
            
            # 提取音频 URL
            if platform == "douyin":
                audio_url = extract_audio_url_from_douyin(video)
            elif platform == "xiaohongshu":
                audio_url = extract_audio_url_from_xhs(video)
            else:
                audio_url = None
            
            # 支持多种video_id字段名称
            vid = video.get("video_id") or video.get("aweme_id") or video.get("note_id") or f"video_{index}"
            
            if not audio_url:
                print(f"  ⚠️ 视频 {vid} 无音频URL")
                return {
                    "video_id": vid,
                    "text": "",
                    "status": "no_audio_url",
                    "duration": duration,
                    "is_long_video": is_long_video
                }
            
            try:
                # 根据视频时长动态选择模型
                if is_long_video:
                    # 长视频使用异步任务模式 (qwen3-asr-flash-filetrans)
                    result = await transcribe_long_video_async(audio_url, duration)
                else:
                    # 短视频使用同步模式 (qwen3-asr-flash)
                    result = await transcribe_audio_async(audio_url, duration_seconds=duration)
                
                result["video_id"] = vid
                result["audio_url"] = audio_url[:100] + "..." if len(audio_url) > 100 else audio_url
                result["duration"] = duration
                result["is_long_video"] = is_long_video
                
                return result
            except Exception as e:
                return {
                    "video_id": vid,
                    "text": "",
                    "status": f"error: {str(e)[:50]}",
                    "duration": duration,
                    "is_long_video": is_long_video
                }
    
    # 构建并行任务
    tasks = [
        transcribe_single(video, i) 
        for i, video in enumerate(videos[:max_count])
    ]
    
    # 并行执行所有转录任务
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 处理异常结果
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                "video_id": f"video_{i}",
                "text": "",
                "status": f"exception: {str(result)[:50]}",
                "duration": 0,
                "is_long_video": False
            })
        else:
            processed_results.append(result)
    
    # 统计结果
    success_count = sum(1 for r in processed_results if r.get("status") == "success")
    print(f"  ✅ 批量转录完成: {success_count}/{len(processed_results)} 成功")
    
    return processed_results


async def transcribe_long_video_async(audio_url: str, duration: int = 0, api_key: str = None) -> Dict[str, Any]:
    """
    异步转录长视频 (>5分钟)
    使用 qwen3-asr-flash-filetrans 模型
    
    API文档: https://bailian.console.aliyun.com 长音频ASR
    
    Args:
        audio_url: 音频/视频 URL
        duration: 视频时长（秒）
        api_key: DashScope API Key
    
    Returns:
        {"text": "转录文本", "status": "success/error"}
    """
    if not audio_url:
        return {"text": "", "status": "empty"}
    
    if not api_key:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    
    if not api_key:
        return {"text": "", "status": "no_api_key"}
    
    # 长视频使用异步文件转写API
    # 🔧 修复：使用正确的API端点
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable"  # 必须：异步模式
    }
    
    # 🔧 修复：使用官方文档的payload格式
    payload = {
        "model": "qwen3-asr-flash-filetrans",  # 长视频专用模型
        "input": {
            "file_url": audio_url
        },
        "parameters": {
            "channel_id": [0],  # 单声道
            "enable_itn": False,
            "enable_words": True
        }
    }
    
    # 🔧 修复：正确的API端点URL
    async_url = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
    
    try:
        # 🔧 修复：增加httpx超时时间到300秒，禁用SSL验证（抖音CDN需要）
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=300.0, verify=False) as client:
            # 提交任务
            print(f"[ASR] 提交长音频转写任务... URL: {audio_url[:80]}...")
            metadata = {
                "mode": "filetrans_submit",
                "audio_seconds": duration,
            }
            async with llm_track(
                "asr_filetrans_submit",
                "dashscope",
                model="qwen3-asr-flash-filetrans",
                metadata=metadata,
            ) as tracker:
                response = await client.post(async_url, json=payload, headers=headers)
                result = response.json() if response.status_code == 200 else {}
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(result)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=response.status_code < 400,
                    error_msg=None if response.status_code < 400 else response.text[:200],
                )
            
            if response.status_code != 200:
                try:
                    error_detail = response.text[:300]
                except:
                    error_detail = ""
                print(f"[ASR] 长音频提交失败: {response.status_code} - {error_detail}")
                return {"text": "", "status": f"submit_error_{response.status_code}", "error_detail": error_detail}
            
            task_id = result.get("output", {}).get("task_id")
            
            if not task_id:
                print(f"[ASR] 未获取到task_id: {result}")
                return {"text": "", "status": "no_task_id", "raw_response": str(result)[:200]}
            
            print(f"[ASR] 任务提交成功，task_id: {task_id}")
            
            # 轮询获取结果（最多等待10分钟，长视频需要更长时间）
            status_url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
            max_wait = 600  # 最多等待10分钟（长视频可能需要更久）
            wait_time = 0
            
            print(f"[ASR] 开始轮询任务状态...")
            
            while wait_time < max_wait:
                await asyncio.sleep(5)  # 每5秒检查一次
                wait_time += 5
                
                print(f"[ASR] 轮询第{wait_time//5}次，请求状态...")
                
                try:
                    status_resp = await client.get(status_url, headers={"Authorization": f"Bearer {api_key}"})
                    print(f"[ASR] 状态响应码: {status_resp.status_code}")
                except Exception as poll_err:
                    print(f"[ASR] 状态请求异常: {poll_err}")
                    continue
                
                if status_resp.status_code != 200:
                    print(f"[ASR] 状态查询失败: {status_resp.status_code}")
                    continue
                
                status_result = status_resp.json()
                task_status = status_result.get("output", {}).get("task_status")
                print(f"      ⏳ 长视频转写中... ({wait_time}s/{max_wait}s) 状态: {task_status}")
                
                if task_status == "SUCCEEDED":
                    # 获取转写结果
                    output = status_result.get("output", {})
                    results = output.get("results", {})
                    
                    # 🔧 调试：打印完整结构
                    import json
                    print(f"[ASR] ============ SUCCEEDED ============")
                    print(f"[ASR] output keys: {list(output.keys())}")
                    
                    # 🔧 修复：递归搜索包含.json的URL
                    def find_transcription_url(obj, depth=0):
                        """递归搜索transcription_url或.json结尾的URL"""
                        if depth > 5:  # 防止无限递归
                            return None
                        if isinstance(obj, str):
                            if '.json' in obj and ('oss' in obj.lower() or 'aliyun' in obj.lower() or 'http' in obj.lower()):
                                return obj
                        elif isinstance(obj, dict):
                            # 优先检查常见字段名
                            for key in ['transcription_url', 'url', 'file_url', 'result_url']:
                                if key in obj and obj[key]:
                                    return obj[key]
                            # 递归搜索所有值
                            for key, val in obj.items():
                                found = find_transcription_url(val, depth + 1)
                                if found:
                                    return found
                        elif isinstance(obj, list):
                            for item in obj:
                                found = find_transcription_url(item, depth + 1)
                                if found:
                                    return found
                        return None
                    
                    # 搜索transcription_url
                    transcription_url = find_transcription_url(output)
                    print(f"[ASR] 搜索到的transcription_url = {transcription_url[:100] if transcription_url else None}")
                    
                    if transcription_url:
                        print(f"[ASR] 发现transcription_url，正在下载结果...")
                        try:
                            result_resp = await client.get(transcription_url)
                            print(f"[ASR] 下载响应码: {result_resp.status_code}")
                            if result_resp.status_code == 200:
                                result_json = result_resp.json()
                                # 从结果文件中提取文本
                                print(f"[ASR] result_json keys: {list(result_json.keys()) if isinstance(result_json, dict) else type(result_json)}")
                                
                                # 尝试多种格式
                                transcripts = result_json.get("transcripts", [])
                                if not transcripts:
                                    transcripts = result_json.get("sentences", [])
                                if not transcripts:
                                    transcripts = result_json.get("transcript", {}).get("sentences", [])
                                
                                if transcripts:
                                    full_text = " ".join([t.get("text", "") for t in transcripts])
                                    print(f"[ASR] 长音频转写成功（从URL），字数: {len(full_text)}")
                                    return {"text": full_text, "status": "success"}
                                
                                # 直接获取text字段
                                full_text = result_json.get("text", "")
                                if full_text:
                                    print(f"[ASR] 长音频转写成功（text字段），字数: {len(full_text)}")
                                    return {"text": full_text, "status": "success"}
                                
                                print(f"[ASR] 从URL获取的JSON无有效文本: {str(result_json)[:500]}")
                            else:
                                print(f"[ASR] 下载transcription_url失败: {result_resp.status_code}")
                        except Exception as url_err:
                            print(f"[ASR] 请求transcription_url异常: {url_err}")
                    
                    # 备用：直接从results获取
                    # 🔧 修复：results可能是列表（每个文件一个结果）
                    if isinstance(results, list) and len(results) > 0:
                        print(f"[ASR] results是列表，长度: {len(results)}")
                        for res_item in results:
                            if isinstance(res_item, dict):
                                # 尝试获取transcription_url
                                item_url = res_item.get("transcription_url") or res_item.get("url")
                                if item_url:
                                    print(f"[ASR] 从results列表获取URL: {item_url[:80]}...")
                                    try:
                                        item_resp = await client.get(item_url)
                                        if item_resp.status_code == 200:
                                            item_json = item_resp.json()
                                            # 提取文本
                                            item_transcripts = item_json.get("transcripts", []) or item_json.get("sentences", [])
                                            if item_transcripts:
                                                full_text = " ".join([t.get("text", "") for t in item_transcripts])
                                                print(f"[ASR] 从results列表提取成功，字数: {len(full_text)}")
                                                return {"text": full_text, "status": "success"}
                                            item_text = item_json.get("text", "")
                                            if item_text:
                                                print(f"[ASR] 从results列表直接提取text，字数: {len(item_text)}")
                                                return {"text": item_text, "status": "success"}
                                    except Exception as item_err:
                                        print(f"[ASR] 获取results项失败: {item_err}")
                    elif isinstance(results, dict):
                        transcripts = results.get("transcripts", [])
                        if not transcripts:
                            transcripts = results.get("sentences", [])
                        if not transcripts:
                            full_text = results.get("text", "")
                            if full_text:
                                print(f"[ASR] 长音频转写成功，字数: {len(full_text)}")
                                return {"text": full_text, "status": "success"}
                        
                        if transcripts:
                            full_text = " ".join([t.get("text", "") for t in transcripts])
                            print(f"[ASR] 长音频转写成功，字数: {len(full_text)}")
                            return {"text": full_text, "status": "success"}
                    
                    # 🔧 最后尝试：打印完整output结构供调试
                    import json
                    try:
                        output_str = json.dumps(output, ensure_ascii=False, indent=2)[:2000]
                    except:
                        output_str = str(output)[:2000]
                    print(f"[ASR] 未找到转写文本，完整output结构:\n{output_str}")
                    return {"text": "", "status": "no_transcript", "raw_output": output_str}
                
                elif task_status == "FAILED":
                    error_msg = status_result.get("output", {}).get("message", "unknown")
                    print(f"[ASR] 任务失败: {error_msg}")
                    return {"text": "", "status": "task_failed", "error": error_msg}
                
                # 仍在处理中，继续等待
            
            print(f"[ASR] 超时，任务未完成")
            return {"text": "", "status": "timeout"}
            
    except Exception as e:
        error_msg = str(e) if str(e) else repr(e)
        print(f"[ASR] 长音频转录异常: {error_msg}")
        import traceback
        traceback.print_exc()
        return {"text": "", "status": f"long_asr_error: {error_msg[:100]}"}


def _extract_lowest_bitrate_url(detail: Dict) -> Optional[str]:
    """从 TikHub 视频详情中提取最低画质的视频URL（体积最小，适合ASR转录）。"""
    video = detail.get("video", {})
    bit_rate = video.get("bit_rate", [])
    if not bit_rate:
        return None
    # 按 data_size 排序取最小的
    candidates = []
    for br in bit_rate:
        pa = br.get("play_addr", {})
        urls = pa.get("url_list", [])
        size = pa.get("data_size", 0)
        if urls and size > 0:
            candidates.append((size, urls[0]))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    chosen_size, chosen_url = candidates[0]
    size_mb = chosen_size / 1024 / 1024
    print(f"[ASR-MP3重试] 选择最低画质: {size_mb:.1f}MB")
    # 超过100MB（大概率是直播回放），跳过
    if size_mb > 100:
        print(f"[ASR-MP3重试] 文件过大({size_mb:.0f}MB > 100MB)，跳过转录")
        return None
    return chosen_url


async def transcribe_with_mp3_fallback(
    audio_url: str,
    aweme_id: Optional[str] = None,
    api_key: Optional[str] = None,
    tikhub_api_key: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    [CTO-13.3 2026-04-20] transcribe_audio_async 的"抖音 need_mp3 自动兜底"封装.

    统一业务调用入口: 所有走抖音短视频 ASR 的路径都应该用这个 helper, 不要直接调
    transcribe_audio_async 后手撸 need_mp3 分支 (容易漏导致老板截图"仿写失败"类 bug).

    Args:
        audio_url: 抖音视频流 URL 或 MP3 URL
        aweme_id: 抖音 aweme_id (无则跳过 retry, 返原 need_mp3 错误)
        api_key: DashScope (默认环境变量)
        tikhub_api_key: TikHub (默认环境变量 TIKHUB_API_KEY)

    Returns:
        同 transcribe_audio_async: {"text", "status", ...}
        · need_mp3 + 有 aweme_id + 有 tikhub_key → 自动调 retry_with_mp3 转长音频模型
        · 非抖音场景 (aweme_id=None) → 返原 need_mp3 错误不做重试
    """
    result = await transcribe_audio_async(audio_url, api_key=api_key, duration_seconds=duration_seconds)
    if result.get("status") != "need_mp3":
        return result
    if not aweme_id:
        return result  # 非抖音场景 / 调用方没传 aweme_id, 无法 TikHub 兜底
    tk = tikhub_api_key or os.environ.get("TIKHUB_API_KEY", "")
    if not tk:
        return result  # 无 TikHub key, 保留原错误
    retry = await retry_with_mp3(str(aweme_id), audio_url, tk, duration_seconds=duration_seconds)
    if retry.get("status") == "success" and retry.get("text"):
        return retry
    return result  # retry 也失败, 返原 need_mp3 保持诊断语义


async def retry_with_mp3(
    aweme_id: str,
    original_audio_url: str,
    tikhub_api_key: str,
    duration_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """need_mp3 时，通过 TikHub 单视频接口获取音频/最低画质URL并重试长音频转录。

    策略:
    1. 优先取 music.play_url（真正的音频MP3）
    2. 若无音频URL，取 bit_rate 中最低画质视频URL（体积最小，避免 CONTENT_LENGTH_CHECK_FAILED）
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await tracked_tikhub_get(
                client,
                "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video",
                caller="asr_mp3_fallback_video_detail",
                model="video_detail",
                params={"aweme_id": aweme_id},
                headers={"Authorization": f"Bearer {tikhub_api_key}"},
            )
            if resp.status_code != 200:
                print(f"[ASR-MP3重试] TikHub返回{resp.status_code}, aweme_id={aweme_id}")
                return {"text": "", "status": "mp3_fetch_failed"}

            detail = resp.json().get("data", {}).get("aweme_detail", {})
            if not detail:
                print(f"[ASR-MP3重试] 无aweme_detail, aweme_id={aweme_id}")
                return {"text": "", "status": "mp3_no_detail"}

            # 检查时长，超过30分钟跳过
            dur_sec = normalize_duration_seconds(detail.get("duration", 0), assume_milliseconds=True)
            billing_seconds = dur_sec or duration_seconds or 0
            if dur_sec > 1800:
                print(f"[ASR-MP3重试] 视频超长({dur_sec//60}分钟)，跳过: aweme_id={aweme_id}")
                return {"text": "", "status": "skipped_too_long"}

            # 优先音频URL
            audio_url = extract_audio_url_from_douyin(detail)
            # 检查是否真的是音频URL（非视频流）
            is_real_audio = audio_url and (
                ".mp3" in audio_url or "ies-music" in audio_url or
                ("douyinstatic" in audio_url and "/video" not in audio_url)
            )

            if is_real_audio and audio_url != original_audio_url:
                print(f"[ASR-MP3重试] 使用音频URL: {audio_url[:80]}")
                return await transcribe_long_video_async(audio_url, duration=billing_seconds)

            # 无真实音频URL → 取最低画质视频URL（体积最小）
            low_url = _extract_lowest_bitrate_url(detail)
            if low_url and low_url != original_audio_url:
                print(f"[ASR-MP3重试] 使用最低画质视频: {low_url[:80]}")
                return await transcribe_long_video_async(low_url, duration=billing_seconds)

            print(f"[ASR-MP3重试] 无可用替代URL, aweme_id={aweme_id}")
            return {"text": "", "status": "mp3_same_url"}
    except Exception as e:
        print(f"[ASR-MP3重试] 异常 aweme_id={aweme_id}: {e}")
        return {"text": "", "status": "mp3_retry_error"}


# ==========================================
# [Phase 12.7] 视频评论获取与钩子效果分析
# ==========================================

async def fetch_video_comments(
    aweme_id: str,
    max_count: int = 50
) -> Dict[str, Any]:
    """
    获取抖音视频评论
    
    Args:
        aweme_id: 视频ID
        max_count: 最大获取评论数
    
    Returns:
        {
            "status": "success/error",
            "total": 评论总数,
            "comments": [评论列表],
            "error": 错误信息(如有)
        }
    """
    import os
    
    api_key = os.environ.get("TIKHUB_API_KEY", "")
    if not api_key:
        return {"status": "no_api_key", "total": 0, "comments": []}
    
    url = f"https://api.tikhub.io/api/v1/douyin/app/v3/fetch_video_comments"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await tracked_tikhub_get(
                client,
                url,
                caller="asr_video_comments",
                model="video_comments",
                metadata={"max_count": max_count},
                params={
                    "aweme_id": aweme_id,
                    "cursor": 0,
                    "count": max_count
                },
                headers={"Authorization": f"Bearer {api_key}"}
            )
            
            if response.status_code == 200:
                data = response.json()
                comments_data = data.get("data", {}).get("comments", [])
                
                # 提取关键信息
                comments = []
                for c in comments_data[:max_count]:
                    comments.append({
                        "text": c.get("text", ""),
                        "digg_count": c.get("digg_count", 0),
                        "reply_comment_total": c.get("reply_comment_total", 0),
                        "user_nickname": c.get("user", {}).get("nickname", "")
                    })
                
                return {
                    "status": "success",
                    "total": len(comments),
                    "comments": comments
                }
            else:
                return {
                    "status": f"http_{response.status_code}",
                    "total": 0,
                    "comments": []
                }
    except Exception as e:
        return {
            "status": str(e)[:100],
            "total": 0,
            "comments": []
        }


def analyze_hook_engagement(
    comments: List[Dict],
    hook_text: str,
    hook_patterns: List[str] = None
) -> Dict[str, Any]:
    """
    分析评论区钩子词响应效果
    
    Args:
        comments: 评论列表
        hook_text: 视频话术中的钩子词 (如"回复666")
        hook_patterns: 额外的钩子模式 (如["666", "私信", "咨询"])
    
    Returns:
        {
            "total_comments": 评论总数,
            "hook_responses": 钩子响应数,
            "response_rate": 响应率,
            "top_hooks": 热门钩子词统计,
            "engagement_score": 转化效果评分
        }
    """
    import re
    
    if hook_patterns is None:
        # 常见的钩子模式
        hook_patterns = [
            "666", "888", "111", "999",
            "私信", "私", "dm", "DM",
            "咨询", "了解", "链接",
            "想要", "想学", "求",
            "怎么联系", "如何合作",
            "加微", "v信", "微信"
        ]
    
    total = len(comments)
    if total == 0:
        return {
            "total_comments": 0,
            "hook_responses": 0,
            "response_rate": 0,
            "top_hooks": {},
            "engagement_score": 0
        }
    
    # 统计钩子响应
    hook_counts = {}
    hook_responses = 0
    
    for comment in comments:
        text = comment.get("text", "")
        matched = False
        
        for pattern in hook_patterns:
            if pattern.lower() in text.lower():
                hook_counts[pattern] = hook_counts.get(pattern, 0) + 1
                matched = True
        
        if matched:
            hook_responses += 1
    
    # 计算响应率
    response_rate = round(hook_responses / total * 100, 1) if total > 0 else 0
    
    # 排序热门钩子
    top_hooks = dict(sorted(hook_counts.items(), key=lambda x: x[1], reverse=True)[:5])
    
    # 评估转化效果 (0-100分)
    # 响应率>20%=高效, 10-20%=中等, <10%=一般
    if response_rate >= 20:
        engagement_score = 90 + min(10, (response_rate - 20))
    elif response_rate >= 10:
        engagement_score = 70 + (response_rate - 10) * 2
    elif response_rate >= 5:
        engagement_score = 50 + (response_rate - 5) * 4
    else:
        engagement_score = response_rate * 10
    
    return {
        "total_comments": total,
        "hook_responses": hook_responses,
        "response_rate": response_rate,
        "top_hooks": top_hooks,
        "engagement_score": round(engagement_score)
    }


async def analyze_video_engagement(
    aweme_id: str,
    video_text: str = ""
) -> Dict[str, Any]:
    """
    综合分析视频的评论区互动效果
    
    Args:
        aweme_id: 视频ID
        video_text: 视频话术文本（用于识别钩子词）
    
    Returns:
        完整的评论分析结果
    """
    # 获取评论
    comments_result = await fetch_video_comments(aweme_id, max_count=50)
    
    if comments_result["status"] != "success" or not comments_result["comments"]:
        return {
            "status": comments_result["status"],
            "analysis": None
        }
    
    # 从视频文本中提取钩子词
    hook_patterns = ["666", "888", "私信", "咨询", "了解"]
    
    # 如果视频文本中有特定钩子词，添加到检测列表
    if video_text:
        # 检测数字类钩子 (如 "回复666")
        import re
        numbers = re.findall(r'回复?(\d{3})', video_text)
        for num in numbers:
            if num not in hook_patterns:
                hook_patterns.insert(0, num)
    
    # 分析钩子效果
    analysis = analyze_hook_engagement(
        comments_result["comments"],
        video_text,
        hook_patterns
    )
    
    return {
        "status": "success",
        "comments_count": comments_result["total"],
        "analysis": analysis
    }


# 导出
__all__ = [
    "transcribe_audio_sync",
    "transcribe_audio_async",
    "extract_audio_url_from_douyin",
    "extract_audio_url_from_xhs",
    "batch_transcribe_videos",
    "fetch_video_comments",
    "analyze_hook_engagement",
    "analyze_video_engagement"
]

