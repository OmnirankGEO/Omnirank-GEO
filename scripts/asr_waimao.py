"""对外贸 20 分钟对标视频做 ASR 异步转写。"""
import asyncio
import httpx
import json
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

KEY = os.getenv("DASHSCOPE_API_KEY")
ASR_SUBMIT = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/filetrans"


async def submit(audio_url):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            ASR_SUBMIT,
            headers={
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            json={
                "model": "qwen3-asr-flash-filetrans",
                "input": {"file_url": audio_url},
            },
        )
        r.raise_for_status()
        return r.json()["output"]["task_id"]


async def poll(task_id, max_wait=900):
    async with httpx.AsyncClient(timeout=30) as c:
        elapsed = 0
        while elapsed < max_wait:
            await asyncio.sleep(10)
            elapsed += 10
            r = await c.get(
                f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {KEY}"},
            )
            d = r.json()
            status = d.get("output", {}).get("task_status", "?")
            print(f"[{elapsed}s] status={status}", flush=True)
            if status == "SUCCEEDED":
                results = d.get("output", {}).get("results", [])
                # 保存完整 raw
                (ROOT / "scripts" / "waimao_asr_raw.json").write_text(
                    json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                transcripts = []
                for r_item in results:
                    # 可能是 transcript 字符串或者 object 里的 transcription
                    if isinstance(r_item, dict):
                        t = r_item.get("transcript") or r_item.get("transcription", "")
                        if not t:
                            # 可能结果在 transcription_url 里
                            t_url = r_item.get("transcription_url") or r_item.get("text_url")
                            if t_url:
                                async with httpx.AsyncClient(timeout=30) as c2:
                                    rr = await c2.get(t_url)
                                    t = rr.text
                        transcripts.append(t)
                    else:
                        transcripts.append(str(r_item))
                return "\n".join(transcripts)
            if status in ("FAILED", "CANCELED"):
                return f"FAILED: {json.dumps(d, ensure_ascii=False)[:500]}"
        return "TIMEOUT"


async def main():
    raw = json.loads((ROOT / "scripts" / "waimao_video_raw.json").read_text(encoding="utf-8"))
    detail = raw["data"]["aweme_detail"]
    video = detail["video"]
    play_url = video["play_addr"]["url_list"][0]
    duration_ms = video.get("duration", 0)
    print(f"Video: duration={duration_ms / 1000:.1f}s")
    print(f"play_url head: {play_url[:150]}")
    print()
    task_id = await submit(play_url)
    print(f"task_id: {task_id}")
    transcript = await poll(task_id)
    out = ROOT / "scripts" / "waimao_transcript.txt"
    out.write_text(transcript, encoding="utf-8")
    print(f"\nSaved to {out} ({len(transcript)} chars)")
    # 头 500 字 preview
    print("\n--- PREVIEW (first 500 chars) ---")
    print(transcript[:500])


if __name__ == "__main__":
    asyncio.run(main())
