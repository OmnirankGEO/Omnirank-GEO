"""单视频拆解,带 (model, key) failover:plus/primary → plus/backup → flash/primary → flash/backup。"""
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

KEY_PRIMARY = os.getenv("DASHSCOPE_API_KEY")
KEY_BACKUP = os.getenv("DASHSCOPE_API_KEY_BACKUP")
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

ATTEMPTS = [
    ("qwen3.5-omni-plus", KEY_PRIMARY, "plus/primary"),
    ("qwen3.5-omni-plus", KEY_BACKUP, "plus/backup"),
    ("qwen3.5-omni-flash", KEY_PRIMARY, "flash/primary"),
    ("qwen3.5-omni-flash", KEY_BACKUP, "flash/backup"),
]


PROMPT = """你是短视频拆解专家,熟悉黄岛主开篇 36 计和 10 种脚本结构。
观察这条抖音视频的音频+画面,输出**严格 JSON**:

{
  "opening_3s": {
    "line": "开篇第一句原话",
    "hook_type": "36 计里第几计+名称(冒犯权威/颠覆认知/戳中痛点/对比反差/内幕揭秘/共鸣观点/信息缺口等)",
    "visual": "开篇画面(正面出镜/偷拍风/空镜/手机屏幕录屏/分屏/第一视角手持等)",
    "emotion": "IP 情绪(不爽/激动/嘲讽/真诚/松弛/紧迫/疲惫)"
  },
  "body": {
    "structure_type": "10 种结构里哪种(问题解决/避免踩坑/案例解析/反常观点/共鸣观点/泛垂结合/借势大牌/误解纠错/过程记录/个人成就)",
    "key_segments": ["段 1 讲什么(按时间)", "段 2", "段 3", "..."],
    "numbers_or_cases": "具体用了哪些数字/场景/案例(如'一天见 5 个客户''某小区业主'等)",
    "pace": "语速快慢 + 剪辑节奏(第一视角长镜头/手机快剪/分屏等)"
  },
  "cta": {
    "text": "CTA 原话",
    "type": "关注/私信/评论/主页/购物车/直接卖",
    "intensity": "强/中/弱"
  },
  "visual_system": {
    "scene": "拍摄场景(地铁/写字楼电梯/客户门店/自己工位等)",
    "persona": "IP 形象(穿着/性别年龄/道具)",
    "props": "核心道具(宣传单/手机屏幕/名片/签单等)",
    "bgm": "BGM 风格"
  },
  "why_viral": "爆款核心原因 2-3 句",
  "replicable_framework": "抽象通用可复制框架(比如'第一视角跟拍 + 真实遭拒 + 某个惊喜转折 + 签单金额曝光')",
  "omnirank_adaptation": "怎么把这套框架套用到 OmniRank(全域上榜 · AI 搜索优化 SaaS · 4 引擎监测 · 白帽 SEO · 城市运营商代理体系 · 28%/5% 分润 · 3888 积分免费送),给 2-3 句具体建议"
}

只输出 JSON,不加 markdown code block。"""


def call_once(model, api_key, video_url, label):
    print(f"\n[try {label}] model={model}")
    try:
        client = OpenAI(api_key=api_key, base_url=BASE_URL)
        t0 = time.time()
        completion = client.chat.completions.create(
            model=model,
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
                d = chunk.choices[0].delta
                c = getattr(d, "content", None)
                r = getattr(d, "reasoning_content", None)
                if c:
                    full_text += c
                if r:
                    reasoning_text += r
            if chunk.usage:
                usage = chunk.usage
        if not full_text and reasoning_text:
            full_text = reasoning_text
        elapsed = time.time() - t0
        return {
            "ok": bool(full_text),
            "model": model,
            "key_label": label,
            "elapsed_sec": round(elapsed, 1),
            "raw_output": full_text,
            "reasoning_output": reasoning_text,
            "usage_video": getattr(usage.prompt_tokens_details, "video_tokens", None) if usage else None,
            "usage_audio": getattr(usage.prompt_tokens_details, "audio_tokens", None) if usage else None,
            "usage_out": usage.completion_tokens if usage else None,
        }
    except Exception as e:
        print(f"  ERR: {str(e)[:200]}")
        return {"ok": False, "model": model, "key_label": label, "error": str(e)[:500]}


def parse_json(text):
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```json", 1)[-1].split("```", 1)[0].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        l, r = cleaned.find("{"), cleaned.rfind("}")
        if l >= 0 and r > l:
            try:
                return json.loads(cleaned[l:r + 1])
            except Exception:
                return None
    return None


def main():
    target = json.loads((ROOT / "scripts" / "resolved_target.json").read_text(encoding="utf-8"))
    video = target["video"]
    print(f"Target: {video['author']} | {video['duration_sec']}s | digg={video['digg']}")
    print(f"Desc: {video['desc'][:120]}")

    attempts_log = []
    result = None
    for model, key, label in ATTEMPTS:
        if not key:
            print(f"[skip {label}] key missing")
            continue
        r = call_once(model, key, video["play_url"], label)
        attempts_log.append(r)
        if r.get("ok"):
            result = r
            print(f"  SUCCESS ({label}) in {r['elapsed_sec']}s, "
                  f"video_tok={r['usage_video']}, out={r['usage_out']}, chars={len(r['raw_output'])}")
            break

    parsed = parse_json(result["raw_output"]) if result else None

    out_data = {
        "target": video,
        "winning_attempt": result,
        "parsed": parsed,
        "attempts_log": attempts_log,
    }
    out = ROOT / "scripts" / "breakdown_single_result.json"
    out.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved to {out}")
    if parsed:
        print("\n=== PARSED ===")
        print(json.dumps(parsed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
