"""§6 深挖 · 被采纳抖音「视频」条目的口播结构分析(阿里云 ASR)

只处理 douyin_adopted_content_probe.py 判定为 `video` 的条目。
图文帖不走 ASR(没有口播),由 douyin_adopted_imagepost_probe.py 单独分析。

复用既有能力,不新造 ASR:
  - `tools/asr/asr_tool.extract_audio_url_from_douyin` 取音轨 URL
  - `tools/asr/asr_tools.transcribe_video` 智能路由(≤300s 同步 / 超长异步)
  - 成本自动进 `llm_track`(dashscope qwen3-asr-flash, CNY 0.00022/audio-sec)

用法:
    python scripts/research/douyin_adopted_asr_probe.py \
        --probe .tmp_ro/probe_result.json --out .tmp_ro/asr_result.json
"""
from __future__ import annotations

import argparse
import collections
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.asr.asr_tool import extract_audio_url_from_douyin  # noqa: E402
from tools.asr.asr_tools import transcribe_video  # noqa: E402

TIKHUB_ONE_VIDEO = "https://api.tikhub.io/api/v1/douyin/app/v3/fetch_one_video"


# 🔴 fail-closed:上游把错误当【纯文本】返回(如 'Error: HTTP 401 - {"code":
#    "InvalidApiKey"...}'),若照单全收会被计为"转写成功"。实测踩过:54/54 全"成功"
#    而字数只有 48/126 两个值 —— 那是两条错误串,不是口播。任何一条命中即判失败。
#    第二次踩:只列了 "Error: HTTP",漏掉 'Error: Expecting value: line 1 column 1'
#    (上游 JSON 解析失败也走同一条纯文本路径)→ 改为 "任何以 Error: 开头" 一律判错。
_ASR_ERROR_MARKERS = (
    "InvalidApiKey", "API-key", '"code"', "request_id",
    "Throttling", "AccessDenied", "Arrearage",
)
_ASR_ERROR_PREFIXES = ("Error:", "error:", "Exception:", "Traceback")


def _looks_like_error(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(_ASR_ERROR_PREFIXES) or any(
        m in text for m in _ASR_ERROR_MARKERS
    )


def _text_from_response(resp: Any) -> tuple[str, str]:
    """返回 (text, status)。status != 'ok' 时 text 是诊断信息而非转写结果。"""
    try:
        raw = resp.content[0]["text"]
    except Exception as exc:  # noqa: BLE001
        return f"unreadable ToolResponse: {exc}", "bad_response"
    if not raw or not str(raw).strip():
        return "", "empty"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        text = str(raw).strip()
        # 纯文本路径:先过错误标记,别把报错当转写
        if _looks_like_error(text):
            return text[:300], "upstream_error"
        return text, "ok"

    if isinstance(parsed, dict):
        if parsed.get("code") or parsed.get("error") or parsed.get("status") in (
            "timeout", "failed", "error"
        ):
            return json.dumps(parsed, ensure_ascii=False)[:300], "upstream_error"
        for key in ("text", "transcription", "result"):
            if parsed.get(key):
                val = str(parsed[key]).strip()
                if _looks_like_error(val):
                    return val[:300], "upstream_error"
                return val, "ok"
        return json.dumps(parsed, ensure_ascii=False)[:300], "no_text_field"
    return str(parsed), "ok"


async def one(
    client: httpx.AsyncClient, sem: asyncio.Semaphore,
    row: Dict[str, Any], api_key: str,
) -> Dict[str, Any]:
    async with sem:
        out = {k: row.get(k) for k in
               ("aweme_id", "industry_key", "desc", "duration_sec",
                "title_from_signal", "text_extra")}
        try:
            resp = await client.get(
                TIKHUB_ONE_VIDEO,
                params={"aweme_id": row["aweme_id"]},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=45.0,
            )
            if resp.status_code != 200:
                return {**out, "asr_status": f"detail_http_{resp.status_code}"}
            detail = (resp.json().get("data") or {}).get("aweme_detail") or {}
            audio_url = extract_audio_url_from_douyin(detail)
            if not audio_url:
                return {**out, "asr_status": "no_audio_url"}

            dur = int(row.get("duration_sec") or 0)
            tr = await transcribe_video(audio_url, duration_seconds=dur)
            text, status = _text_from_response(tr)
            return {
                **out,
                "asr_status": status,
                "transcript": text if status == "ok" else "",
                "diagnostic": "" if status == "ok" else text,
                "transcript_chars": len(text) if status == "ok" else 0,
            }
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断全批
            return {**out, "asr_status": "exception", "error": str(exc)[:200]}


async def main_async(args: argparse.Namespace) -> int:
    api_key = os.getenv("TIKHUB_API_KEY")
    if not api_key:
        print("[ERR] TIKHUB_API_KEY 未设置", file=sys.stderr)
        return 2

    probe: List[Dict[str, Any]] = json.loads(
        Path(args.probe).read_text(encoding="utf-8")
    )
    videos = [r for r in probe if r.get("kind") == "video"]
    if args.limit:
        videos = videos[: args.limit]
    total_sec = sum(float(r.get("duration_sec") or 0) for r in videos)
    print(f"[asr] 视频 {len(videos)} 条, 合计 {total_sec/60:.1f} 分钟, "
          f"预估 ASR 成本 CNY {total_sec * 0.00022:.2f}")

    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(one(client, sem, r, api_key) for r in videos)
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    ok = [r for r in results if r.get("asr_status") == "ok"]
    print(f"[asr] 成功 {len(ok)}/{len(results)}")
    if ok:
        chars = sorted(r["transcript_chars"] for r in ok)
        mid = chars[len(chars) // 2]
        print(f"[asr] 转写字数中位 {mid}")
        # 🔴 反向对照:全批字数只有 1-2 个取值 = 返回的是同一串错误文案而非口播
        distinct = len({r["transcript_chars"] for r in ok})
        if len(ok) >= 5 and distinct <= 2:
            print(f"[asr] 🔴 可疑:{len(ok)} 条成功但字数只有 {distinct} 种取值,"
                  f"极可能是错误串被当成转写 —— 人工核对后再用")
            return 3

    bad = collections.Counter(
        r.get("asr_status") for r in results if r.get("asr_status") != "ok"
    )
    for status, n in bad.most_common():
        sample = next(r for r in results if r.get("asr_status") == status)
        print(f"    - {status}: {n} 条 · 例:"
              f"{(sample.get('diagnostic') or sample.get('error') or '')[:120]}")
    print(f"[asr] 明细写入 {out}")

    # fail-closed:一条都没成功 = 这次深挖没有产出,绝不能返回 0 让上层当成功
    if not ok:
        print("[asr] 🔴 零条成功 —— ASR 深挖未产出,退出码 3")
        return 3
    return 0


class _FakeResp:
    def __init__(self, text: str):
        self.content = [{"text": text}]


def selftest() -> int:
    """拿【已知答案】当靶子核验 fail-closed 检测器。

    必须双向:只有"必须命中"会让恒真检测器(永远判错)也满分 —— 所以配上
    "必须不命中"的真转写样本。两组都过才算这个检测器有判别力。
    """
    # 🔴 断言【具体 status】而非"只要不是 ok"—— 后者太松:去掉 dict 错误码分支后
    #    这些样本会落到 no_text_field(同样不是 ok)从而漏杀变异(实测 M4 存活)。
    #    锁要打在真机制上,不是打在"最终都没返回成功"这种恒真结论上。
    must_catch = [
        ('Error: HTTP 401 - {"code":"InvalidApiKey","message":"API-key is blocked.",'
         '"request_id":"4d3aee93"}', "真实踩到的 401 错误串", "upstream_error"),
        ("Error: Expecting value: line 1 column 1 (char 0)",
         "第二次踩:JSON 解析失败串", "upstream_error"),
        ('{"code":"Throttling","message":"rate limit"}',
         "JSON 形态的上游错误", "upstream_error"),
        ('{"status":"failed"}', "任务失败态", "upstream_error"),
        ('{"status":"timeout","task_id":"x"}', "长音频轮询超时", "upstream_error"),
        # 下面两条专打 _ASR_ERROR_MARKERS 表本身:纯文本、不以 Error: 开头、
        # 也不是 JSON —— 只有 marker 表能抓到。缺了它们清空 marker 表照样满分(实测 M5 存活)。
        ("转写失败 request_id=4d3aee93-2469-980e", "纯文本+request_id", "upstream_error"),
        ("API-key is blocked.", "纯文本+API-key", "upstream_error"),
    ]
    must_not_catch = [
        ("大家好，今天给大家分享深圳全屋定制哪家好，我整理了五个品牌。", "正常口播转写"),
        ("本期对比 3 款产品，第一款价格 1999 元，第二款……", "含数字的正常转写"),
        ('{"text":"这是一段正常的转写结果，介绍了装修选材要点。"}', "JSON 包裹的正常转写"),
    ]

    failures: List[str] = []
    for payload, label, expected in must_catch:
        _, status = _text_from_response(_FakeResp(payload))
        if status != expected:
            failures.append(
                f"[漏报] {label}: 期望 status={expected} 实得 {status} -> {payload[:60]}"
            )
    for payload, label in must_not_catch:
        text, status = _text_from_response(_FakeResp(payload))
        if status != "ok":
            failures.append(f"[误报] 应判成功却判错: {label} -> status={status}")
        elif not text.strip():
            failures.append(f"[空文本] {label}")

    total = len(must_catch) + len(must_not_catch)
    if failures:
        print(f"[selftest] 🔴 {len(failures)}/{total} 不通过:")
        for f in failures:
            print("   ", f)
        return 1
    print(f"[selftest] ✅ {total}/{total} 通过 "
          f"(必须命中 {len(must_catch)} + 必须不命中 {len(must_not_catch)})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--selftest", action="store_true",
                   help="用已知答案核验 fail-closed 检测器,不打任何外部 API")
    p.add_argument("--probe")
    p.add_argument("--out")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    if args.selftest:
        return selftest()
    if not args.probe or not args.out:
        p.error("--probe 与 --out 必填(或用 --selftest)")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
