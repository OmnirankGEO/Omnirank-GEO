"""``body-sha256-v1`` —— 文章正文指纹(规格 §11.1 / DEL-06 / DEL-08)。

DEL-06 逐字给了三条,每一条都指一个具体的错误方向:

  1. **只做 UTF-8/NFC/换行归一并冻结 version** —— 归一化面就这三样,不许再加;
  2. 同 fingerprint(即使标题/元数据不同)绑定两个 contract items 或冒充
     distinct article 是 **H0-DATA**;
  3. **标点/空白/措辞不同不得被 H0 过度归一**,语义相似只作局部 A1。

🔴 为什么不复用 ``services/research_monitor/corpus_contract.py``
--------------------------------------------------------------
census 2026-08-21 实测:那份是 ``sha256-nfkc-lf-rstrip-v1`` ——
**NFKC**(不是 NFC)+ 逐行 rstrip + 折叠 4+ 空行。
NFKC 会把全角「Ａ」折成半角「A」、把「①」折成「1」、把「㈱」拆开;
逐行 rstrip 会抹掉行尾有意义的空白。那正是 DEL-06 第 3 条明令禁止的
「过度归一」—— 两篇真正不同的稿子会得到同一指纹,进而被 H0 判成 duplicate
并**阻断合法交付**。语料层可以那么做(它要的是模糊匹配),
交付合同层不行(它要的是身份)。

所以本模块自己实现,并且**故意只做三件事**:

  · 解码成 str(拒非 str);
  · Unicode **NFC**;
  · 换行归一 ``\\r\\n`` / ``\\r`` → ``\\n``。

不 strip、不折叠空行、不 casefold、不去标点。
变异「把 NFC 换成 NFKC」或「加一行 strip」必须让 DEL-06 的过度归一判据转红。

🔴 version 冻结在指纹里
-----------------------
返回值形如 ``body-sha256-v1:<64 hex>``。带前缀的理由:将来真要换算法时,
历史 publication 绑的是**当时**那条指纹,一眼能看出它是哪一版算的;
裸 hex 会让「换了算法」和「正文改了」长得一模一样(DEL-08 要的正是
「历史 publication 始终绑定原正文 hash」)。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: 冻结的版本标识。改归一化面**必须**同时改这里 —— 否则旧指纹与新指纹同名异义。
BODY_HASH_VERSION = "body-sha256-v1"

_NEWLINES = re.compile(r"\r\n|\r")


class BodyHashError(ValueError):
    """正文形态不合法。**不指纹一个猜出来的值**。"""


def canonicalize_body(body: object) -> str:
    """DEL-06 允许的**全部**归一化。多一步都是过度归一。"""
    if not isinstance(body, str):
        raise BodyHashError(f"正文必须是 str,实得 {type(body).__name__}")
    return unicodedata.normalize("NFC", _NEWLINES.sub("\n", body))


def body_hash(body: object) -> str:
    """``body-sha256-v1:<sha256 hex>``。"""
    canonical = canonicalize_body(body)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{BODY_HASH_VERSION}:{digest}"


def is_body_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(BODY_HASH_VERSION + ":")
        and len(value) == len(BODY_HASH_VERSION) + 1 + 64
        and all(c in "0123456789abcdef" for c in value[len(BODY_HASH_VERSION) + 1:])
    )


def assert_body_hash(value: object, *, field: str = "articleHash") -> str:
    if not is_body_hash(value):
        raise BodyHashError(
            f"{field} 必须是 {BODY_HASH_VERSION}:<64 hex>,实得 {value!r}。"
            "裸 hex 会让「换了算法」和「正文改了」长得一样"
        )
    return value  # type: ignore[return-value]
