"""DeepSeek API key selection helpers.

The repo historically used a single DEEPSEEK_API_KEY.  Production Social IP
work now has distinct traffic shapes: realtime orchestration, writing, review,
and async curation.  This module keeps the old variable working while allowing
role-specific pools from Windows/user env.
"""

from __future__ import annotations

import os
import sys
import threading
from collections import defaultdict
from typing import Iterable, List


ROLE_ENV_NAMES = {
    "realtime": ("DEEPSEEK_API_KEY_REALTIME", "DEEPSEEK_API_KEYS_REALTIME"),
    "orchestrator": ("DEEPSEEK_API_KEY_ORCHESTRATOR",),
    "expert": ("DEEPSEEK_API_KEY_EXPERT",),
    "expert_chat": ("DEEPSEEK_API_KEY_EXPERT",),
    "normal": ("DEEPSEEK_API_KEY_REALTIME",),
    "flash": ("DEEPSEEK_API_KEY_REALTIME",),
    "professional": ("DEEPSEEK_API_KEY_PROFESSIONAL", "DEEPSEEK_API_KEY_PRO"),
    "pro": ("DEEPSEEK_API_KEY_PRO", "DEEPSEEK_API_KEY_PROFESSIONAL"),
    "super": ("DEEPSEEK_API_KEY_SUPER",),
    "review": ("DEEPSEEK_API_KEY_REVIEW",),
    "batch": ("DEEPSEEK_API_KEY_BATCH",),
    "curator": ("DEEPSEEK_API_KEY_BATCH",),
}

_COUNTERS = defaultdict(int)
_LOCK = threading.Lock()


def _read_user_env(name: str) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or "")
    except Exception:
        return ""


def _read_env(name: str) -> str:
    # Process env wins when explicitly set for a worker; otherwise read the
    # Windows user env registry so freshly added global keys are visible even
    # before the parent shell is restarted.
    return os.getenv(name) or _read_user_env(name)


def _split_values(values: Iterable[str | None]) -> List[str]:
    keys: List[str] = []
    seen = set()
    for value in values:
        if not value:
            continue
        for part in str(value).replace(";", ",").split(","):
            key = part.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return keys


def _role_env_values(role: str | None) -> List[str | None]:
    role_key = str(role or "").strip().lower()
    names = list(ROLE_ENV_NAMES.get(role_key, ()))
    role_values: List[str | None] = [_read_env(name) for name in names]
    if _split_values(role_values):
        return role_values
    values: List[str | None] = []
    global_pool = _read_env("DEEPSEEK_API_KEYS")
    if global_pool:
        values.append(global_pool)
    else:
        values.append(_read_env("DEEPSEEK_API_KEY"))
    return values


def get_deepseek_api_keys(role: str | None = None) -> List[str]:
    """Return de-duplicated keys for a role, preserving env priority."""
    return _split_values(_role_env_values(role))


def pick_deepseek_api_key(role: str | None = None) -> str:
    """Pick one key by role using a tiny in-process round-robin counter."""
    keys = get_deepseek_api_keys(role)
    if not keys:
        return ""
    role_key = str(role or "default").strip().lower() or "default"
    with _LOCK:
        index = _COUNTERS[role_key] % len(keys)
        _COUNTERS[role_key] += 1
    return keys[index]


async def adeepseek_call_with_failover(do_request, role: str | None = None):
    """[failover 2026-06-11] deepseek 多 key 双保险:逐个 key 调 do_request(key)·成功返回·失败 raise → 自动换下一个 key 重试。
    试遍所有 key 仍失败 → raise 最后异常。**单 key 时 = 直接调一次(无额外开销·向后兼容);
    多 key(老板填 DEEPSEEK_API_KEYS)时才真 failover**。do_request(key) 必须在失败时 raise。"""
    keys = get_deepseek_api_keys(role)
    if not keys:
        raise RuntimeError("deepseek 无可用 key (DEEPSEEK_API_KEY/DEEPSEEK_API_KEYS 未配)")
    role_key = str(role or "default").strip().lower() or "default"
    with _LOCK:
        start = _COUNTERS[role_key] % len(keys)
        _COUNTERS[role_key] += 1
    last_exc = None
    for i in range(len(keys)):
        key = keys[(start + i) % len(keys)]
        try:
            return await do_request(key)
        except Exception as e:
            last_exc = e
            continue  # 该 key 失败 → 换下一个
    raise last_exc if last_exc else RuntimeError("deepseek failover 无结果")


async def adeepseek_post_with_failover(
    body: dict,
    *,
    url: str = "https://api.deepseek.com/v1/chat/completions",
    timeout: float = 60.0,
    role: str | None = None,
    track_name: str | None = None,
    track_model: str | None = None,
):
    """[failover 2026-06-11] DeepSeek /chat/completions 统一调用:多 key failover + httpx async post(+ 可选 llm_track)。

    - 多 key(DEEPSEEK_API_KEYS):某 key 网络异常/非200 → 自动换下一个 key 重试·试遍全部(经 adeepseek_call_with_failover)。
    - 单 key:直接调一次(零额外开销·向后兼容·当前 prod 行为不变)。
    - 返回:status==200 的 httpx.Response(调用方自行 .json() 解析·保持原解析逻辑不动)。
    - raise:无可用 key / 全部 key 失败 / 最后一次非200(调用方用原 except 走降级·行为等价原单 key 失败降级)。
    track_name 给定时按 llm_track 记账(track_model 缺省取 body['model'])。"""
    import httpx

    async def _do(api_key: str):
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            if track_name:
                from tools.llm_call_tracker import llm_track, usage_from_response_payload
                async with llm_track(track_name, "deepseek", model=track_model or body.get("model")) as tracker:
                    resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code == 200:
                        it, ot, ct = usage_from_response_payload(resp.json())
                        tracker.record(input_tokens=it, output_tokens=ot, cached_tokens=ct, success=True)
                    else:
                        tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                resp = await client.post(url, headers=headers, json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"deepseek HTTP {resp.status_code}: {resp.text[:200]}")
            return resp

    return await adeepseek_call_with_failover(_do, role=role)


def deepseek_role_for_model(model: str | None, thinking: str | None = None) -> str:
    """Map a model/thinking pair to the product pool role."""
    model_name = str(model or "").strip().lower()
    thinking_mode = str(thinking or "").strip().lower()
    if "deepseek-v4-pro" in model_name and thinking_mode == "enabled":
        return "super"
    if "deepseek-v4-pro" in model_name:
        return "professional"
    # 🔴 [WO_206] `deepseek-flash` 是官方 2026-09 的新名(同一档,V4.1-Flash)。
    #    不加这一行,新名会掉到最后的 `realtime` 兜底档 —— **换了一批 key**,
    #    而且不报错:只表现为"这个模型怎么老是限流/额度不对"。
    #    子串匹配写成 `"deepseek-flash" in` 而不是 `startswith`,与上面两行同款
    #    (调用方有时传 `deepseek/deepseek-flash` 这种带前缀的写法)。
    if "deepseek-v4-flash" in model_name or "deepseek-flash" in model_name:
        return "normal"
    return "realtime"


def has_deepseek_key(role: str | None = None) -> bool:
    return bool(get_deepseek_api_keys(role))


def mask_deepseek_key(key: str | None) -> str:
    text = str(key or "")
    if len(text) <= 12:
        return "***" if text else ""
    return f"{text[:7]}...{text[-4:]}"
