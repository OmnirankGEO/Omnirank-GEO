"""
KYC 敏感字段加密工具（身份证号）

提供 4 个函数:
- encrypt_id_card(plain)  → base64(nonce || ciphertext || tag)  AES-256-GCM 加密, 随机 nonce
- decrypt_id_card(cipher) → 明文                                 审核员解密
- hmac_id_card(plain)     → hex(32B)                              HMAC-SHA256, 同明文 → 同 hash
- mask_id_card(plain)     → "510***********23"                    脱敏展示

设计要点:
- AES 每次加密输出不同（随机 nonce），无法用加密值做等值查询
- HMAC 单向 + 确定性, 用于"这个身份证号是否已被绑定"的唯一性查询
- 两把密钥独立: KYC_ENCRYPT_KEY (AES 用) + KYC_HMAC_KEY (HMAC 用), 不可复用
- 密钥从环境变量读取, 代码内无任何硬编码

密钥生成（老板本地执行, 不经过 AI）:
    python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
"""

import base64
import hashlib
import hmac
import logging
import os

logger = logging.getLogger("GEO-KYC-Crypto")


class KYCCryptoError(RuntimeError):
    """加密/解密失败"""


class KYCCryptoConfigError(RuntimeError):
    """KYC 密钥未配置或格式错误"""


def _load_key(env_var: str) -> bytes:
    """从环境变量加载 32 字节 AES/HMAC key"""
    raw = os.getenv(env_var, "").strip()
    if not raw:
        raise KYCCryptoConfigError(f"{env_var} 未配置")

    try:
        key = base64.b64decode(raw)
    except Exception as e:
        raise KYCCryptoConfigError(f"{env_var} 不是合法 base64: {e}") from e

    if len(key) != 32:
        raise KYCCryptoConfigError(
            f"{env_var} 解码后长度 {len(key)} 字节, 期望 32 字节"
        )
    return key


def encrypt_id_card(plaintext: str) -> str:
    """AES-256-GCM 加密身份证号

    输出格式: base64(nonce[12B] || ciphertext || tag[16B])
    每次加密 nonce 不同, 输出不同 (安全属性, 但同时意味着不能用加密值做等值查询)
    """
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext 必须是非空字符串")

    key = _load_key("KYC_ENCRYPT_KEY")

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:
        raise KYCCryptoConfigError(f"cryptography 库未安装: {e}") from e

    nonce = os.urandom(12)
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce))
    encryptor = cipher.encryptor()
    ct = encryptor.update(plaintext.encode("utf-8")) + encryptor.finalize()
    tag = encryptor.tag
    return base64.b64encode(nonce + ct + tag).decode("ascii")


def decrypt_id_card(ciphertext_b64: str) -> str:
    """解密 encrypt_id_card 产出的密文"""
    if not isinstance(ciphertext_b64, str) or not ciphertext_b64:
        raise ValueError("ciphertext_b64 必须是非空字符串")

    key = _load_key("KYC_ENCRYPT_KEY")

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:
        raise KYCCryptoConfigError(f"cryptography 库未安装: {e}") from e

    try:
        blob = base64.b64decode(ciphertext_b64)
    except Exception as e:
        raise KYCCryptoError(f"密文不是合法 base64: {e}") from e

    if len(blob) < 12 + 16 + 1:
        raise KYCCryptoError(f"密文长度异常: {len(blob)}")

    nonce, ct, tag = blob[:12], blob[12:-16], blob[-16:]

    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag))
    decryptor = cipher.decryptor()
    try:
        plaintext = decryptor.update(ct) + decryptor.finalize()
    except Exception as e:
        raise KYCCryptoError(f"解密失败（密钥错误或密文被篡改）: {e}") from e

    return plaintext.decode("utf-8")


def hmac_id_card(plaintext: str) -> str:
    """HMAC-SHA256 指纹, 用于唯一性查询

    同明文 → 同 hash, 单向不可逆.
    输出 64 字符 hex.
    """
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext 必须是非空字符串")

    key = _load_key("KYC_HMAC_KEY")

    digest = hmac.new(key, plaintext.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def mask_id_card(plaintext: str) -> str:
    """脱敏展示: 前 3 后 2, 中间星号

    18 位: "510101199001012341" → "510*************41"  (13 个星)
    15 位: "510101900101234"    → "510**********34"     (10 个星)
    其他长度: 全星号 (防御)
    """
    if not plaintext:
        return ""
    n = len(plaintext)
    if n < 6:
        return "*" * n
    if n == 18:
        return f"{plaintext[:3]}{'*' * 13}{plaintext[-2:]}"
    if n == 15:
        return f"{plaintext[:3]}{'*' * 10}{plaintext[-2:]}"
    # 通用
    return f"{plaintext[:3]}{'*' * (n - 5)}{plaintext[-2:]}"


def is_configured() -> bool:
    """轻量探测 KYC 密钥是否都配置好（不实际解码，仅看环境变量存在）"""
    return bool(
        os.getenv("KYC_ENCRYPT_KEY", "").strip()
        and os.getenv("KYC_HMAC_KEY", "").strip()
    )
