# ASR Tools Package
from .asr_tools import (
    transcribe_video,
    transcribe_short_video,
    submit_long_video_transcription,
    get_transcription_result,
)
from .asr_tool import (
    transcribe_audio_sync,
    transcribe_audio_async,
    transcribe_with_mp3_fallback,
    retry_with_mp3,
    extract_audio_url_from_douyin,
    extract_audio_url_from_xhs,
    batch_transcribe_videos,
)

__all__ = [
    # 旧版
    "transcribe_video",
    "transcribe_short_video",
    "submit_long_video_transcription",
    "get_transcription_result",
    # 新版 (DashScope qwen3-asr-flash)
    "transcribe_audio_sync",
    "transcribe_audio_async",
    "transcribe_with_mp3_fallback",  # 抖音 need_mp3 统一兜底入口
    "retry_with_mp3",
    "extract_audio_url_from_douyin",
    "extract_audio_url_from_xhs",
    "batch_transcribe_videos",
]

