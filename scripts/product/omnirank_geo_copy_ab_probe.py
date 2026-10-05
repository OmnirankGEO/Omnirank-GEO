#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OmniRank GEO copy A/B probe across content models.

This is a lightweight product probe, not a production endpoint. It reads the
OmniRank public knowledge base, asks several writing models to produce
human-feeling short-video scripts, then reports repetition and quality signals.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List

import httpx
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")


KB_PATH = Path(
    "C:/AI-Test/AgentsCope-07-frontend-qa/docs/KNOWLEDGE_BASE/"
    "OmniRank_全域上榜_对外企业知识库_v1_2026-04-29.md"
)
OUT_DIR = ROOT / "agent-test-artifacts" / "social-ip-redesign" / "omnirank-geo-copy-ab"
COPY_DOC = ROOT / "docs" / "AI-CONTEXT" / "copy-candidates" / "CODEX_OMNIRANK_GEO_SCRIPT_CANDIDATES_2026-04-30.md"


MODEL_CONFIGS: Dict[str, Dict[str, Any]] = {
    "deepseek-v4-flash": {
        "label": "DeepSeek V4 Flash",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "thinking": "disabled",
        "temperature": 0.86,
        "env_keys": ["DEEPSEEK_API_KEY"],
    },
    "deepseek-v4-pro": {
        "label": "DeepSeek V4 Pro",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "thinking": "disabled",
        "temperature": 0.82,
        "env_keys": ["DEEPSEEK_API_KEY"],
    },
    "doubao": {
        "label": "豆包 Seed 2.0 Pro",
        "provider": "doubao",
        "model": "doubao-seed-2-0-pro-260215",
        "temperature": 0.82,
        "env_keys": ["DOUBAO_API_KEY", "ARK_API_KEY", "DOUBAO_SEED_API_KEY"],
    },
}


STYLE_LENSES = [
    {
        "id": "xuehui",
        "name": "薛辉式：成交表达但不硬卖",
        "brief": "用老板痛点、业务现场、反常识观点切入。先让用户觉得被理解，再把服务边界说清楚。",
    },
    {
        "id": "cange",
        "name": "参哥团队式：老板增长视角",
        "brief": "更直接、更有老板气，说生意损失和机会，不绕技术词，但不能变成夸大承诺。",
    },
    {
        "id": "huang",
        "name": "社恐小黄式：停留理由优先",
        "brief": "第一句要让路人停下来，用冲突、误区、具体画面，不要像产品介绍。",
    },
    {
        "id": "xingyi",
        "name": "星壹式：运营诊断视角",
        "brief": "像在复盘一个获客漏斗：客户在哪里流失、资料哪里缺、下一步补什么。",
    },
]


FORBIDDEN_PATTERNS = [
    "保证AI一定推荐",
    "保证 AI 一定推荐",
    "保证固定排名",
    "固定排名",
    "几天内让所有AI",
    "几天内让所有 AI",
    "必然推荐",
    "100%",
    "稳赚",
    "包上榜",
]

NEGATION_MARKERS = [
    "不保证",
    "不承诺",
    "不能承诺",
    "不敢承诺",
    "不能保证",
    "不是承诺",
    "不是保证",
    "不要承诺",
    "别承诺",
]

AI_FLAVOR_TERMS = [
    "赋能",
    "全链路",
    "闭环",
    "内容资产",
    "结构化",
    "可追溯",
    "品牌可见度",
    "生成式引擎优化",
    "AI搜索可见度",
]

VIVID_TERMS = [
    "没有你",
    "分流",
    "还没见到",
    "输在",
    "上桌",
    "客户问",
    "老板",
    "销售",
    "微信",
    "截图",
    "竞品",
    "流走",
    "见你之前",
]


def _has_env(keys: List[str]) -> bool:
    return any(os.getenv(key) for key in keys)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _extract_kb_brief(raw: str) -> str:
    sections = []
    wanted = [
        "## 1. 一句话介绍",
        "## 3. 市场背景",
        "## 4. 我们解决的核心问题",
        "## 5. 产品与服务模块",
        "## 6. 方法论：OmniRank GEO 闭环",
        "## 8. 适用客户",
        "## 11. 对外表达口径",
        "## 12. FAQ",
        "## 13. 合规与边界",
        "## 16. 不建议对外使用的表达",
    ]
    for idx, marker in enumerate(wanted):
        start = raw.find(marker)
        if start < 0:
            continue
        next_starts = [raw.find(next_marker, start + len(marker)) for next_marker in wanted[idx + 1:]]
        next_starts = [item for item in next_starts if item > start]
        end = min(next_starts) if next_starts else min(len(raw), start + 2600)
        sections.append(raw[start:end].strip())
    brief = "\n\n".join(sections)
    return brief[:18000]


def _first_sentence(text: str, limit: int = 60) -> str:
    compact = _compact(text)
    for sep in ["。", "？", "?", "！", "!", "\n"]:
        if sep in compact:
            compact = compact.split(sep, 1)[0]
            break
    return compact[:limit]


def _parse_json_object(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    for candidate in (text, _escape_newlines_in_json_strings(text)):
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}
    json_text = match.group(0)
    for candidate in (json_text, _escape_newlines_in_json_strings(json_text)):
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            pass
    return {}


def _escape_newlines_in_json_strings(text: str) -> str:
    """Repair common model JSON: literal newlines inside quoted strings."""
    chars: List[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            chars.append(ch)
            escaped = False
            continue
        if ch == "\\":
            chars.append(ch)
            escaped = True
            continue
        if ch == '"':
            chars.append(ch)
            in_string = not in_string
            continue
        if in_string and ch in "\r\n":
            chars.append("\\n")
            continue
        chars.append(ch)
    return "".join(chars)


def _clean_script_text(text: str) -> str:
    return text.replace("\\n", "\n").strip()


def _build_prompt(kb_brief: str, style: Dict[str, str], run_index: int) -> str:
    return f"""
你是短视频主写手，不是产品说明书编辑。

这次主题：用 OmniRank / 全域上榜的知识库，写一条关于“企业为什么要做 GEO / AI 搜索可见度”的口播稿。

写手风格：
{style["name"]}
{style["brief"]}

必须遵守的产品事实：
- 全域上榜 / OmniRank 是 AI 搜索可见度诊断与增长系统。
- 核心动作：诊断、资料建档、关键词方案、内容资产建设、发布留证、持续监测、报告复盘。
- 它解决的是：AI 是否认识企业、是否在行业/地区/方案问题里提到企业、竞品为什么被推荐、企业缺什么公开资料。
- 不承诺固定排名、固定推荐次数、固定转化结果。
- 不做虚假灌水，不伪造案例，不操纵 AI。

知识库摘要：
{kb_brief}

创作要求：
- 写一条 2-3 分钟口播稿，900-1200 中文字以内。
- 第一秒就要有钩子，像真人开口，不要“你有没有想过”“很多老板还没意识到”这种温吞开场。
- 允许有锋利表达，比如“客户还没见到你，就已经被 AI 分流走了”，但不能触碰上面的红线。
- 术语要翻成人话：GEO 可以出现，但必须用一句大白话解释。
- 不要写成清单课、官网介绍、产品发布会，也不要密集堆“闭环、内容资产、可见度”。
- 每次生成必须换切入角度，不要重复上一条常见句式。
- 结尾自然收，不要硬喊私信。

输出 JSON：
{{
  "title": "短标题",
  "angle": "这条的切入角度",
  "script": "完整口播稿",
  "why_better": "为什么这条比普通说明文更有停留感",
  "risk_notes": ["发布前要注意的边界"]
}}

这是第 {run_index} 次生成。请大胆一点，先写活，再守边界。
""".strip()


async def _call_model(model_key: str, cfg: Dict[str, Any], prompt: str, timeout_s: int) -> Dict[str, Any]:
    provider = cfg["provider"]
    api_key = ""
    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY") or ""
        url = "https://api.deepseek.com/v1/chat/completions"
        model = cfg["model"]
    elif provider == "doubao":
        api_key = os.getenv("DOUBAO_API_KEY") or os.getenv("ARK_API_KEY") or os.getenv("DOUBAO_SEED_API_KEY") or ""
        url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        model = os.getenv("DOUBAO_ENDPOINT_ID") or cfg["model"]
    else:
        return {"success": False, "error": f"unsupported provider: {provider}"}
    if not api_key:
        return {"success": False, "error": f"missing api key for {provider}"}

    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": cfg.get("temperature", 0.82),
        "max_tokens": 2400,
        "stream": False,
    }
    if provider == "deepseek":
        body["thinking"] = {"type": cfg.get("thinking", "disabled")}
        body["response_format"] = {"type": "json_object"}
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
        elapsed_s = round(time.time() - started, 2)
        if resp.status_code != 200:
            return {"success": False, "elapsed_s": elapsed_s, "error": f"HTTP {resp.status_code}: {resp.text[:400]}"}
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_json_object(content)
        return {
            "success": bool(parsed),
            "elapsed_s": elapsed_s,
            "usage": data.get("usage", {}),
            "raw": content,
            "parsed": parsed,
            "error": "" if parsed else "json parse failed",
        }
    except Exception as exc:
        return {"success": False, "elapsed_s": round(time.time() - started, 2), "error": f"{type(exc).__name__}: {exc}"}


def _quality_signals(script: str) -> Dict[str, Any]:
    compact = _compact(script)
    forbidden = []
    for term in FORBIDDEN_PATTERNS:
        start = 0
        while True:
            idx = compact.find(term, start)
            if idx < 0:
                break
            nearby = compact[max(0, idx - 12): idx + len(term) + 2]
            if not any(marker in nearby for marker in NEGATION_MARKERS):
                forbidden.append(term)
                break
            start = idx + len(term)
    ai_terms = [term for term in AI_FLAVOR_TERMS if term in script]
    vivid_terms = [term for term in VIVID_TERMS if term in script]
    sentences = [item for item in re.split(r"[。！？!?]\s*", script) if item.strip()]
    avg_sentence_len = round(mean(len(_compact(item)) for item in sentences), 1) if sentences else 0
    human_score = 100
    human_score -= len(ai_terms) * 5
    human_score -= max(0, avg_sentence_len - 34) * 1.2
    human_score += min(18, len(vivid_terms) * 3)
    human_score -= len(forbidden) * 40
    human_score = max(0, min(100, int(round(human_score))))
    return {
        "human_score": human_score,
        "opening": _first_sentence(script),
        "forbidden": forbidden,
        "ai_terms": ai_terms,
        "vivid_terms": vivid_terms,
        "avg_sentence_len": avg_sentence_len,
        "chars": len(script),
    }


def _pairwise_similarity(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a = _compact(items[i].get("script") or "")
            b = _compact(items[j].get("script") or "")
            ratio = difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0
            pairs.append({
                "i": i,
                "j": j,
                "model_i": items[i].get("model_key"),
                "model_j": items[j].get("model_key"),
                "ratio": round(ratio, 4),
            })
    return {
        "avg": round(mean(pair["ratio"] for pair in pairs), 4) if pairs else 0,
        "max": round(max(pair["ratio"] for pair in pairs), 4) if pairs else 0,
        "pairs": pairs,
    }


def _markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# OmniRank GEO Copy A/B Probe",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- runs_per_model: {report['runs_per_model']}",
        f"- similarity_avg: {report['similarity']['avg']}",
        f"- similarity_max: {report['similarity']['max']}",
        "",
        "## Summary",
        "",
        "| model | ok | avg_s | avg_human | max_sim_in_model | note |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for key, summary in report["summary"].items():
        lines.append(
            f"| {key} | {summary['ok']}/{summary['total']} | {summary['avg_elapsed_s']} | "
            f"{summary['avg_human_score']} | {summary['max_similarity']} | {summary['note']} |"
        )
    lines.extend(["", "## Scripts"])
    for item in report["results"]:
        lines.extend([
            "",
            f"### {item['model_label']} · {item['style_name']} · human={item.get('human_score')} · elapsed={item.get('elapsed_s')}",
            "",
            f"- title: {item.get('title')}",
            f"- angle: {item.get('angle')}",
            f"- opening: {item.get('opening')}",
            f"- risk: {', '.join(item.get('risk_notes') or [])}",
            "",
            "```text",
            str(item.get("script") or item.get("error") or "").strip(),
            "```",
        ])
    return "\n".join(lines) + "\n"


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for model_key in sorted({item["model_key"] for item in results}):
        rows = [item for item in results if item["model_key"] == model_key]
        ok_rows = [item for item in rows if item.get("success")]
        sim = _pairwise_similarity(ok_rows)
        human_scores = [item.get("human_score") for item in ok_rows if isinstance(item.get("human_score"), int)]
        elapsed = [item.get("elapsed_s") for item in ok_rows if isinstance(item.get("elapsed_s"), (int, float))]
        forbidden_count = sum(len(item.get("forbidden") or []) for item in ok_rows)
        note = "可用"
        if forbidden_count:
            note = "有红线词"
        elif sim["max"] >= 0.62:
            note = "重复偏高"
        elif human_scores and mean(human_scores) < 78:
            note = "偏说明书"
        summary[model_key] = {
            "ok": len(ok_rows),
            "total": len(rows),
            "avg_elapsed_s": round(mean(elapsed), 2) if elapsed else 0,
            "avg_human_score": round(mean(human_scores), 1) if human_scores else 0,
            "max_similarity": sim["max"],
            "forbidden_count": forbidden_count,
            "note": note,
        }
    return summary


def _append_to_copy_doc(report: Dict[str, Any], md_path: Path) -> None:
    best = sorted(
        [item for item in report["results"] if item.get("success")],
        key=lambda item: (item.get("human_score") or 0, -len(item.get("ai_terms") or [])),
        reverse=True,
    )[:4]
    lines = [
        "",
        "---",
        "",
        f"## 第三轮：模型 A/B 变体 - {report['generated_at']}",
        "",
        f"> 完整报告：`{md_path}`",
        "",
        "### 模型结论",
        "",
    ]
    for key, summary in report["summary"].items():
        lines.append(
            f"- {key}: 成功 {summary['ok']}/{summary['total']}，人味均分 {summary['avg_human_score']}，"
            f"模型内最大相似度 {summary['max_similarity']}，判断：{summary['note']}。"
        )
    lines.extend(["", "### 候选变体"])
    for item in best:
        lines.extend([
            "",
            f"#### {item['model_label']} · {item['style_name']} · human={item.get('human_score')}",
            "",
            f"- angle: {item.get('angle')}",
            f"- opening: {item.get('opening')}",
            "",
            "```text",
            str(item.get("script") or "").strip(),
            "```",
        ])
    existing = COPY_DOC.read_text(encoding="utf-8") if COPY_DOC.exists() else ""
    COPY_DOC.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    raw = KB_PATH.read_text(encoding="utf-8")
    kb_brief = _extract_kb_brief(raw)
    selected_models = args.model or list(MODEL_CONFIGS)
    results: List[Dict[str, Any]] = []
    for model_key in selected_models:
        cfg = MODEL_CONFIGS[model_key]
        if not _has_env(cfg["env_keys"]):
            results.append({
                "success": False,
                "model_key": model_key,
                "model_label": cfg["label"],
                "style_name": "",
                "script": "",
                "error": f"missing env key: {'/'.join(cfg['env_keys'])}",
            })
            continue
        for idx in range(args.runs):
            style = STYLE_LENSES[(idx + selected_models.index(model_key)) % len(STYLE_LENSES)]
            prompt = _build_prompt(kb_brief, style, idx + 1)
            response = await _call_model(model_key, cfg, prompt, args.timeout_seconds)
            parsed = response.get("parsed") or {}
            script = _clean_script_text(str(parsed.get("script") or ""))
            signals = _quality_signals(script) if script else {}
            results.append({
                "success": bool(response.get("success") and script),
                "model_key": model_key,
                "model_label": cfg["label"],
                "style_id": style["id"],
                "style_name": style["name"],
                "run_index": idx + 1,
                "elapsed_s": response.get("elapsed_s"),
                "title": parsed.get("title", ""),
                "angle": parsed.get("angle", ""),
                "script": script,
                "why_better": parsed.get("why_better", ""),
                "risk_notes": parsed.get("risk_notes") or [],
                "error": response.get("error", ""),
                "raw": response.get("raw", ""),
                **signals,
            })
    ok_results = [item for item in results if item.get("success")]
    report = {
        "success": bool(ok_results),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runs_per_model": args.runs,
        "models": selected_models,
        "summary": _summarize(results),
        "similarity": _pairwise_similarity(ok_results),
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = OUT_DIR / f"omnirank-geo-copy-ab-{stamp}.json"
    md_path = OUT_DIR / f"omnirank-geo-copy-ab-{stamp}.md"
    report["json_path"] = str(json_path)
    report["md_path"] = str(md_path)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    if args.append_doc:
        _append_to_copy_doc(report, md_path)
    return report


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OmniRank GEO copy A/B probe")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--model", action="append", choices=sorted(MODEL_CONFIGS))
    parser.add_argument("--append-doc", action="store_true")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    report = asyncio.run(run(args))
    print(f"success={report['success']}")
    print(f"markdown={report.get('md_path')}")
    print(f"similarity_max={report['similarity']['max']}")
    for key, summary in report["summary"].items():
        print(
            f"- {key}: ok={summary['ok']}/{summary['total']} "
            f"human={summary['avg_human_score']} sim={summary['max_similarity']} note={summary['note']}"
        )
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
