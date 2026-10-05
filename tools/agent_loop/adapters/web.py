from __future__ import annotations

import aiohttp

from tools.agent_loop.security.html_clean import clean_html
from tools.agent_loop.security.url_check import is_safe_url_async


async def visit(url: str, purpose: str, max_chars: int = 4000) -> dict:
    # Verify B 边缘 #2 修 · 走 async 版 · 防 DNS gethostbyname_ex 阻塞 event loop
    safe, reason = await is_safe_url_async(url)
    if not safe:
        return {"error": f"unsafe_url:{reason}", "url": url, "purpose": purpose}
    html = await _fetch_text(url)
    text = clean_html(html, max_chars=max_chars)
    return {
        "url": url,
        "purpose": purpose,
        "text": text,
        "max_chars": max_chars,
    }


async def _fetch_text(url: str, timeout_seconds: int = 12) -> str:
    """P0-15 fix · 第二层 SSRF 防御 · 禁跟随 302/3xx 跳到 metadata.

    修前:默认 allow_redirects=True · 攻击者 https://evil.com/r 302 → 169.254.169.254
    绕过 url_check.is_safe_url 拿 cloud metadata。
    修后:allow_redirects=False · 3xx 看作非法 · 主动验证每次跳转 host 安全。
    """
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            headers={"User-Agent": "OmniRank-AgentLoop/1.0"},
            allow_redirects=False,  # P0-15 fix
        ) as response:
            # 3xx redirect · 重新跑 url_check_async 验证 Location 安全 · 至多 1 跳
            if 300 <= response.status < 400:
                location = response.headers.get("Location", "")
                if not location:
                    return ""
                safe, _ = await is_safe_url_async(location)
                if not safe:
                    return ""  # 拒跟 metadata 类跳转
                async with session.get(
                    location,
                    headers={"User-Agent": "OmniRank-AgentLoop/1.0"},
                    allow_redirects=False,
                ) as r2:
                    if 300 <= r2.status < 400:
                        return ""  # 双跳禁止(防 redirect loop attack)
                    return await r2.text(errors="ignore")
            return await response.text(errors="ignore")
