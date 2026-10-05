"""Run Social Studio agent-loop model tool-call preflight.

This script validates OpenAI-compatible tool-calling protocol behavior only.
It returns fake tool results and never calls product data tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "scripts" / "eval" / "preflight_8_cases.yaml"
DEFAULT_OUT_DIR = ROOT / "outputs"


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    base_url: str
    model: str
    api_key_env: tuple[str, ...]
    temperature: float | None = None
    thinking_disabled: bool = False


PROVIDERS: dict[str, ProviderConfig] = {
    "deepseek": ProviderConfig(
        provider="deepseek",
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        model=os.getenv("DEEPSEEK_FLASH_MODEL") or os.getenv("DEEPSEEK_DEFAULT_MODEL") or "deepseek-v4-flash",
        api_key_env=(
            "DEEPSEEK_API_KEY_ORCHESTRATOR",
            "DEEPSEEK_API_KEY_REALTIME",
            "DEEPSEEK_API_KEY",
        ),
        temperature=0.2,
    ),
    "kimi": ProviderConfig(
        provider="kimi",
        base_url=os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1").rstrip("/"),
        model=os.getenv("KIMI_MODEL", "kimi-k2.6"),
        api_key_env=("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        # 2026-05-16 实测:thinking=disabled+temperature=0.6 是唯一可用组合
        # (默认 thinking 时 temperature ONLY 1.0 · disabled 时 ONLY 0.6 · tool_calls 流程必须 disabled)
        temperature=0.6,
        thinking_disabled=True,
    ),
}


def _api_key(config: ProviderConfig) -> str:
    for name in config.api_key_env:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _tool_schema(raw_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools = []
    for tool in raw_tools:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def _fake_tool_result(name: str, args: dict[str, Any], *, inject_error: bool = False) -> str:
    if inject_error:
        return json.dumps(
            {
                "ok": False,
                "error": "missing_required_argument",
                "hint": "Retry with profile_id, concept, and query.",
            },
            ensure_ascii=False,
        )
    data: dict[str, Any] = {"ok": True, "tool": name, "args": args}
    if name == "time_now":
        data["time"] = "2026-05-15T21:30:00+08:00"
    elif name == "internal_profile_get":
        data["profile"] = {
            "profile_id": args.get("profile_id", "profile_62"),
            "industry": "跨境洗护用品",
            "brand_voice": "务实、直接、避免夸大",
        }
    elif name == "internal_memory_query":
        data["memories"] = [
            {
                "concept": args.get("concept", "target_customer"),
                "text": "目标客户是德国、美国、印尼的 B 端采购经理；表达要避免空泛低价承诺。",
                "review_status": "auto",
            }
        ]
    elif name == "tikhub_search_topics":
        data["topics"] = ["价格透明对比", "认证和交付能力", "本地渠道痛点"]
    elif name == "industry_knowledge_query":
        data["insights"] = ["教育咨询用户更关心政策节点、录取路径和可信案例。"]
    elif name == "web_visit":
        data["title"] = "Example page"
        data["text"] = "Safe fetched content."
    elif name == "update_profile_memory":
        data["status"] = "pending_confirm" if args.get("source") == "ai_inferred" else "auto"
    return json.dumps(data, ensure_ascii=False)


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


async def _post_json(
    client: httpx.AsyncClient,
    config: ProviderConfig,
    payload: dict[str, Any],
    *,
    stream: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    url = f"{config.base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {_api_key(config)}", "Content-Type": "application/json"}
    if not stream:
        resp = await client.post(url, headers=headers, json=payload)
        text = resp.text
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}", "body": text[:500]}, []
        return resp.json(), []

    stream_payload = {**payload, "stream": True}
    chunks: list[dict[str, Any]] = []
    final_choice: dict[str, Any] = {"message": {"role": "assistant", "content": "", "tool_calls": []}, "finish_reason": None}
    tool_parts: dict[int, dict[str, Any]] = {}
    content_parts: list[str] = []
    async with client.stream("POST", url, headers=headers, json=stream_payload) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            return {"error": f"HTTP {resp.status_code}", "body": body.decode(errors="ignore")[:500]}, []
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            chunks.append(chunk)
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if choice.get("finish_reason"):
                final_choice["finish_reason"] = choice.get("finish_reason")
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index", 0))
                part = tool_parts.setdefault(
                    idx,
                    {"id": tc.get("id") or f"call_{idx}", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    part["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    part["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    part["function"]["arguments"] += fn["arguments"]
    final_choice["message"]["content"] = "".join(content_parts)
    final_choice["message"]["tool_calls"] = [tool_parts[i] for i in sorted(tool_parts)]
    return {"choices": [final_choice]}, chunks


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    return message.get("tool_calls") or []


def _tool_name(call: dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name") or "")


async def run_case(config: ProviderConfig, case: dict[str, Any], raw_tools: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    messages = [dict(item) for item in case["messages"]]
    tools = _tool_schema(raw_tools)
    all_tool_calls: list[dict[str, Any]] = []
    final_text = ""
    raw_rounds: list[dict[str, Any]] = []
    errors: list[str] = []
    stream_chunks = 0

    if not _api_key(config):
        return {
            "case_id": case["id"],
            "provider": config.provider,
            "model": config.model,
            "ok": False,
            "skipped": True,
            "error": f"missing API key env: {'/'.join(config.api_key_env)}",
        }

    async with httpx.AsyncClient(timeout=90.0) as client:
        for round_index in range(int(case.get("max_rounds", 1))):
            payload: dict[str, Any] = {
                "model": config.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": 900,
            }
            if config.temperature is not None:
                payload["temperature"] = config.temperature
            if config.thinking_disabled:
                payload["thinking"] = {"type": "disabled"}

            data, chunks = await _post_json(client, config, payload, stream=bool(case.get("stream")) and round_index == 0)
            stream_chunks += len(chunks)
            raw_rounds.append({"round": round_index + 1, "response": data, "stream_chunks": len(chunks)})
            if data.get("error"):
                errors.append(f"{data.get('error')}: {data.get('body', '')[:200]}")
                break
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            final_text = _message_content(message)
            calls = _tool_calls(message)
            if not calls:
                break
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": final_text or None,
                "tool_calls": calls,
            }
            # DeepSeek V4 thinking-mode responses require reasoning_content to be
            # passed back in multi-round tool-call conversations.
            if message.get("reasoning_content"):
                assistant_message["reasoning_content"] = message.get("reasoning_content")
            messages.append(assistant_message)
            for call_index, call in enumerate(calls):
                all_tool_calls.append(call)
                name = _tool_name(call)
                args = _parse_args((call.get("function") or {}).get("arguments") or "")
                tool_error = bool(case.get("inject_first_tool_error")) and len(all_tool_calls) == 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{round_index}_{call_index}",
                        "name": name,
                        "content": _fake_tool_result(name, args, inject_error=tool_error),
                    }
                )

    result = _evaluate_case(case, all_tool_calls, final_text, errors, stream_chunks)
    result.update(
        {
            "case_id": case["id"],
            "title": case.get("title", ""),
            "provider": config.provider,
            "model": config.model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "tool_calls": [
                {
                    "name": _tool_name(call),
                    "arguments": _parse_args((call.get("function") or {}).get("arguments") or ""),
                }
                for call in all_tool_calls
            ],
            "tool_call_count": len(all_tool_calls),
            "stream_chunks": stream_chunks,
            "final_text_preview": final_text[:300],
            "errors": errors,
            "raw_round_count": len(raw_rounds),
        }
    )
    return result


def _evaluate_case(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
    final_text: str,
    errors: list[str],
    stream_chunks: int,
) -> dict[str, Any]:
    expect = case.get("expect") or {}
    names = [_tool_name(call) for call in calls]
    reasons: list[str] = []
    if errors:
        reasons.extend(errors)
    min_tool_calls = int(expect.get("min_tool_calls", 0))
    if len(calls) < min_tool_calls:
        reasons.append(f"tool_call_count {len(calls)} < {min_tool_calls}")
    for required in expect.get("required_tool_names") or []:
        if required not in names:
            reasons.append(f"missing required tool {required}")
    any_round = expect.get("required_tool_names_any_round") or []
    for required in any_round:
        if required not in names:
            reasons.append(f"missing required tool {required}")
    arg_blob = json.dumps(
        [_parse_args((call.get("function") or {}).get("arguments") or "") for call in calls],
        ensure_ascii=False,
    )
    for text in expect.get("required_argument_substrings") or []:
        if text not in arg_blob:
            reasons.append(f"missing argument substring {text}")
    final_any = expect.get("final_required_substrings_any") or []
    if final_any and not any(text in final_text for text in final_any):
        reasons.append(f"final text missing any of {final_any}")
    if case.get("stream") and stream_chunks <= 0:
        reasons.append("stream produced no chunks")
    if expect.get("allow_no_tool") and not calls and not final_text:
        reasons.append("no tool is allowed, but final clarification is empty")
    return {"ok": not reasons, "reasons": reasons}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--provider", action="append", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()

    case_file = Path(args.cases)
    spec = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_tools = spec["tools"]
    cases = spec["cases"]

    exit_code = 0
    for provider_name in args.provider:
        config = PROVIDERS[provider_name]
        results = []
        for case in cases:
            result = await run_case(config, case, raw_tools)
            results.append(result)
            status = "PASS" if result.get("ok") else "FAIL"
            print(f"{provider_name} {case['id']}: {status} ({result.get('latency_ms', 0)}ms)")
            if not result.get("ok"):
                exit_code = 1
        summary = {
            "provider": provider_name,
            "model": config.model,
            "case_count": len(results),
            "pass_count": sum(1 for item in results if item.get("ok")),
            "results": results,
        }
        (out_dir / f"preflight_{provider_name}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
