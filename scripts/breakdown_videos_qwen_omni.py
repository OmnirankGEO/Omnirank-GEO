"""qwen3.5-omni-flash 对 3 条抖音 AI 创业爆款做音画结构化拆解。"""
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

MODEL = os.getenv("OMNI_MODEL", "qwen3.5-omni-plus")


PROMPT = """你是短视频拆解专家,熟悉黄岛主开篇 36 计和 10 种脚本结构。
观察这条抖音视频的音频 + 画面,输出**严格 JSON**,字段:

{
  "opening_3s": {
    "line": "开篇第一句原话",
    "hook_type": "36 计里第几计+名称(冒犯权威/颠覆认知/戳中痛点/对比反差/内幕揭秘/共鸣观点/信息缺口等)",
    "visual": "开篇画面(正面出镜/偷拍风/空镜/手机屏幕录屏/分屏等)",
    "emotion": "IP 情绪(不爽/激动/嘲讽/真诚/松弛/紧迫)"
  },
  "body": {
    "structure_type": "10 种结构里的哪种(问题解决/避免踩坑/案例解析/反常观点/共鸣观点/泛垂结合/借势大牌/误解纠错/过程记录/个人成就)",
    "key_segments": ["段 1 讲什么(按时间顺序)", "段 2", "段 3", "..."],
    "numbers_or_cases": "具体用了哪些数字/案例(如'月赚 6 位数''一人一手机'等)",
    "pace": "语速(很快/适中/慢)+ 剪辑节奏(快剪/长镜头/分屏)"
  },
  "cta": {
    "text": "CTA 原话",
    "type": "关注/私信/评论/主页/购物车/直接卖",
    "intensity": "强/中/弱"
  },
  "visual_system": {
    "scene": "拍摄场景",
    "persona": "IP 形象(穿着/年龄感)",
    "props": "核心道具(手机/白板/文件/屏幕录屏等)",
    "bgm": "BGM 风格"
  },
  "why_viral": "爆款核心原因 2-3 句(结合钩子/结构/情绪/数据反差)",
  "replicable_framework": "抽象出的通用可复制框架(比如:反差钩子+挑战任务+实时数据曝光+短 CTA)",
  "omnirank_adaptation": "怎么把这套框架套用到 OmniRank(全域上榜 · AI 搜索优化 SaaS · 4 引擎监测 · 白帽 SEO · 城市运营商代理)的内容上,给 2-3 句具体建议"
}

只输出 JSON,不加 markdown 代码块。"""


def breakdown(video_url: str, author: str, digg: int, title: str, duration_sec: float) -> dict:
    t0 = time.time()
    print(f"\n--- {author} ({duration_sec}s, {digg} digg) ---")
    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": video_url}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
            modalities=["text"],
            stream=True,
            stream_options={"include_usage": True},
        )
        full_text = ""
        reasoning_text = ""
        usage = None
        for chunk in completion:
            if chunk.choices:
                delta = chunk.choices[0].delta
                c = getattr(delta, "content", None)
                r = getattr(delta, "reasoning_content", None)
                if c:
                    full_text += c
                if r:
                    reasoning_text += r
            if chunk.usage:
                usage = chunk.usage
        # plus 有时只在 reasoning_content 里输出,fallback 用 reasoning
        if not full_text and reasoning_text:
            full_text = reasoning_text

        elapsed = time.time() - t0
        print(f"  got {len(full_text)} chars in {elapsed:.1f}s")
        if usage:
            print(f"  tokens: video={getattr(usage.prompt_tokens_details, 'video_tokens', '-')}, "
                  f"audio={getattr(usage.prompt_tokens_details, 'audio_tokens', '-')}, "
                  f"text_in={getattr(usage.prompt_tokens_details, 'text_tokens', '-')}, "
                  f"out={usage.completion_tokens}")

        # 尝试解析 JSON(可能有前后杂字符)
        cleaned = full_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```json", 1)[-1].split("```", 1)[0].strip()
        parsed = None
        try:
            parsed = json.loads(cleaned)
        except Exception:
            # 截取第一个 { 到最后一个 }
            l = cleaned.find("{")
            r = cleaned.rfind("}")
            if l >= 0 and r > l:
                try:
                    parsed = json.loads(cleaned[l:r + 1])
                except Exception as e:
                    print(f"  JSON parse failed: {e}")

        return {
            "target": {"author": author, "digg": digg, "title": title, "duration_sec": duration_sec},
            "raw_output": full_text,
            "reasoning_output": reasoning_text,
            "parsed": parsed,
            "elapsed_sec": round(elapsed, 1),
            "usage": {
                "video_tokens": getattr(usage.prompt_tokens_details, "video_tokens", None) if usage else None,
                "audio_tokens": getattr(usage.prompt_tokens_details, "audio_tokens", None) if usage else None,
                "text_in": getattr(usage.prompt_tokens_details, "text_tokens", None) if usage else None,
                "out": usage.completion_tokens if usage else None,
            },
        }
    except Exception as e:
        return {"target": {"author": author}, "error": str(e)}


def main():
    video_detail = json.loads((ROOT / "scripts" / "video_detail.json").read_text(encoding="utf-8"))
    results = []
    for v in video_detail:
        if not v.get("play_url"):
            results.append({"target": v.get("target"), "error": "no play_url"})
            continue
        t = v["target"]
        r = breakdown(
            video_url=v["play_url"],
            author=t["author"],
            digg=t["digg"],
            title=t["title"],
            duration_sec=v["duration_sec"],
        )
        results.append(r)

    suffix = "_plus" if "plus" in MODEL else ("_flash" if "flash" in MODEL else "")
    out = ROOT / "scripts" / f"breakdown_results{suffix}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved to {out} (model={MODEL})")


if __name__ == "__main__":
    main()
