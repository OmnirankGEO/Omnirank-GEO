"""
AI Ops 脱敏 · 2026-07-01

给 Codex 的任何上下文(ops_context.md)、任何写进 artifact/event 的文本,
都必须先过 redact()。脱敏是纵深防御的一层(密钥隔离主要靠 clean worktree + 无密钥 checkout),
但仍要兜底清洗明文密钥/token/URL 口令/私钥/手机号/银行卡。

黑名单兜底覆盖(CTO 审核 P0-1 / P2-5 要求):
  - .env 全部 KEY=VALUE(任何名字含 KEY/SECRET/TOKEN/PASSWORD/PWD/APIKEY 的键)
  - 微信支付凭证:WX_APIV3_KEY / WX_APP_SECRET / apiclient_key.pem / pub_key.pem
  - 所有 *.pem 私钥块内容(-----BEGIN ... -----END ...)
  - DATABASE_URL / 任意带口令的连接串(scheme://user:pass@host)
  - JWT / Bearer / sk-* / portal_token 及各类后台 token
  - 手机号 / 银行卡号 / 支付账号
"""

import re

_REDACTED = "«REDACTED»"

# 明确点名的最高危键(即使正则没覆盖也硬删)
SENSITIVE_KEY_NAMES = (
    "DATABASE_URL", "POSTGRES_PASSWORD",
    "WX_APIV3_KEY", "WX_APP_SECRET", "WX_MCH_ID", "WX_APPID",
    "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
    "KIMI_API_KEY", "DOUBAO_API_KEY", "DOUBAO_ENDPOINT_ID",
    "TIKHUB_API_KEY", "METASO_API_KEY",
    "API_5118_LONGTAIL_KEY", "API_5118_SEARCH_VOLUME_KEY",
    "JWT_SECRET", "SECRET_KEY", "OSS_ACCESS_KEY_SECRET", "OSS_ACCESS_KEY_ID",
)

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM 私钥/证书块(含 apiclient_key.pem / pub_key.pem 内容)
    (re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL), _REDACTED),
    # 带口令的连接串 scheme://user:pass@host
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+):([^\s:/@]+)@"), r"\1:" + _REDACTED + "@"),
    # KEY=VALUE / KEY: VALUE,键名含敏感词 → 删值
    (re.compile(
        r"(?im)^([\t ]*[A-Za-z0-9_.\-]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|APIKEY)[A-Za-z0-9_.\-]*)"
        r"[\t ]*([=:])[\t ]*.+$"),
     r"\1\2 " + _REDACTED),
    # JSON 形态:"<键名含敏感词>": "值" → 删值(P2-1:{"WX_APIV3_KEY":"..."} 也要脱敏)
    (re.compile(
        r'(?i)("[A-Za-z0-9_.\-]*(?:key|secret|token|password|passwd|pwd|apikey|apiv3)"\s*:\s*)"[^"]*"'),
     r'\1"' + _REDACTED + '"'),
    # JWT(三段 base64url)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), _REDACTED),
    # Bearer / Authorization
    (re.compile(r"(?i)\b(bearer|authorization:?)\s+[A-Za-z0-9._\-]+"), r"\1 " + _REDACTED),
    # OpenAI 风格 sk-...
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), _REDACTED),
    # 中国大陆手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), _REDACTED),
    # 银行卡号 16-19 位
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), _REDACTED),
]


def redact(text: str) -> str:
    """把明文密钥/token/URL 口令/私钥/手机号/银行卡从文本里清洗掉。"""
    if not text:
        return text or ""
    out = text
    # 点名键名兜底:任何 `NAME=...` / `NAME: ...` 直接删值
    for name in SENSITIVE_KEY_NAMES:
        out = re.sub(
            rf"(?im)^([\t ]*{re.escape(name)})[\t ]*([=:])[\t ]*.+$",
            r"\1\2 " + _REDACTED,
            out,
        )
        # 也覆盖行内 NAME=... 出现
        out = re.sub(
            rf"\b({re.escape(name)})\s*=\s*\S+",
            r"\1=" + _REDACTED,
            out,
        )
        # JSON 形态 "NAME": "值"(点名键即使不含通用敏感词也删,如 DOUBAO_ENDPOINT_ID)
        out = re.sub(
            rf'(?i)("{re.escape(name)}"\s*:\s*)"[^"]*"',
            r'\1"' + _REDACTED + '"',
            out,
        )
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


_SENSITIVE_KEY_RE = re.compile(r'(?i)(key|secret|token|password|passwd|pwd|apikey|apiv3)')


def _key_is_sensitive(key) -> bool:
    ks = str(key)
    return ks in SENSITIVE_KEY_NAMES or bool(_SENSITIVE_KEY_RE.search(ks))


def redact_dict(data: dict) -> dict:
    """
    递归脱敏 dict:值按内容脱敏;键名敏感(含 key/secret/token/password/apiv3 等)的
    直接删值(P2-1:{"WX_APIV3_KEY":"abc"} 这种 JSON/嵌套对象要按 key 脱敏)。
    """
    if not isinstance(data, dict):
        return data
    cleaned = {}
    for k, v in data.items():
        if _key_is_sensitive(k) and isinstance(v, (str, int, float)):
            cleaned[k] = _REDACTED
        elif isinstance(v, str):
            cleaned[k] = redact(v)
        elif isinstance(v, dict):
            cleaned[k] = redact_dict(v)
        elif isinstance(v, list):
            cleaned[k] = [redact(i) if isinstance(i, str) else (redact_dict(i) if isinstance(i, dict) else i) for i in v]
        else:
            cleaned[k] = v
    return cleaned
