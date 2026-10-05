"""
文章内容指纹

用 SHA256(归一化后的全文) 计算指纹,
跨域名转载的同一篇文章应得到同一 hash。
"""
import hashlib
import re


WHITESPACE_RE = re.compile(r'\s+')


def compute_content_hash(content: str) -> str:
    """
    计算文章内容指纹:
    1. 归一化空白(连续空格/换行变单空格)
    2. SHA256 取 64 位 hex

    返回: 64 字符的小写 hex 字符串

    [GEO-R10-CAN-004] 之前只 hash 前 1000 字符, 两篇不同的长文若共享同一段
    1000 字开头(相同样板/导语)会碰撞成同一 hash, 后者被误判 is_duplicate 从而
    被排除出语料库(auto_skipped)。改为 hash 归一化后的全文: 真正逐字相同的转载
    仍得同一 hash, 但仅开头相同、正文不同的长文会得到不同 hash, 消除尾部碰撞误判。
    """
    # 类型校验(防上游传 list/dict 等意外类型)
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise TypeError(f"content 必须是 str, 收到 {type(content).__name__}")

    # [GEO-R10-CAN-004] 归一化全文(不再截断到前 1000 字符)
    normalized = WHITESPACE_RE.sub(' ', content).strip()

    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()
