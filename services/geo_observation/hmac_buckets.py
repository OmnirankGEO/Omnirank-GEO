"""受限 HMAC contributor bucket(R7)。

治理铁律(Codex 裁定 R7):
- 密钥只来自环境变量 GEO_OBSERVATION_HMAC_KEY_V1(base64 32 字节),不进 DB/API/日志/审计 before-after。
- user/brand 使用域分离前缀:'user:{id}' / 'brand:{id}'(防跨域碰撞)。
- v1 只允许一个 active key version(BUCKET_KEY_VERSION=1)。
- 缺密钥:私域事件仍可登记,但公共晋升 fail-closed(抛 HmacKeyUnavailable,调用方不写 bucket、不 promote)。
- 禁在线换 key 致同贡献者双计;轮换必须走离线重建受限桶 + 聚合的受控流程(不在本模块在线支持)。

复用现役 services/kyc_crypto._load_key 的形状(env-only、base64、独立密钥、禁硬编码)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

# v1 只允许一个 active key version。轮换需离线重建 + 版本号推进,不在此在线切换。
BUCKET_KEY_VERSION = 1
_ENV_VAR = "GEO_OBSERVATION_HMAC_KEY_V1"


class HmacKeyUnavailable(RuntimeError):
    """HMAC 密钥缺失/非法 —— 公共晋升必须 fail-closed;私域登记不受影响。"""


def _load_key() -> bytes:
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        raise HmacKeyUnavailable(
            f"{_ENV_VAR} 未配置。公共晋升 fail-closed(私域事件仍可登记);"
            f"密钥只能从 secret/env 注入,禁入库/禁回前端。"
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HmacKeyUnavailable(f"{_ENV_VAR} 非合法 base64: {exc}") from exc
    if len(key) < 16:
        raise HmacKeyUnavailable(f"{_ENV_VAR} 解码后不足 16 字节(不安全)")
    return key


def is_configured() -> bool:
    try:
        _load_key()
        return True
    except HmacKeyUnavailable:
        return False


def _bucket(domain_value: str) -> str:
    key = _load_key()
    return hmac.new(key, domain_value.encode("utf-8"), hashlib.sha256).hexdigest()


def user_bucket(user_id: int) -> str:
    """域分离:'user:{id}'。缺密钥抛 HmacKeyUnavailable。"""
    return _bucket(f"user:{int(user_id)}")


def brand_bucket(brand_id: int) -> str:
    """域分离:'brand:{id}'。缺密钥抛 HmacKeyUnavailable。"""
    return _bucket(f"brand:{int(brand_id)}")
