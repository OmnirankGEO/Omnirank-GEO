"""自有图库图片判定 —— 前后端同一份口径的 Python 侧实现。

[返修单 REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29 §3/§4]

问题背景：`SafeMarkdown.tsx` 无条件把所有 ``<img>`` 换成文字占位，
理由是「与后端 bleach 剥离 ``<img>`` 对齐」。后端确实剥
（``safe_markdown_renderer._ALLOWED_TAGS`` 里没有 ``img``），
但两边都是**一刀切**：把「外部不可信图片」和「我们自己图库里的图」当成一回事。
结果客户自己上传、已确认可外发、URL 实测 HTTP 200 的图，一张也画不出来。

修法不是拆防线，是**按来源区分**：

* 自有图库资产（同源 + ``/uploads/article-images/<brand_id>/…`` 路径前缀）→ 放行；
* 其它一切（外部域名 / ``data:`` / ``blob:`` / 协议相对 / 内嵌 userinfo）→ 维持拦截。

判定口径（路径前缀、允许协议、brand 段规则、测试向量）全部落在
``config/owned_image_asset_policy.json`` —— **前端 TS 与后端 Python 读同一份**，
谁都不许再写第二份白名单（那句"与后端对齐"的注释就是漂移的产物）。
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit


POLICY_PATH: Final = Path(__file__).resolve().parent.parent / "config" / "owned_image_asset_policy.json"
#: URL 里一旦出现这些字符就直接拒（与前端 safeHttpUrl 同款：控制字符 + 反斜杠）。
_UNSAFE_CHARS_RE: Final = re.compile(r"[\x00-\x20\x7f\\]")


@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    """读取前后端共用的判定口径。文件缺失时抛错 —— 绝不静默退化成"全放行"。"""
    with POLICY_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def public_base_origin() -> str:
    """本站对外 origin —— 全仓**唯一出处**([WO_331 · 2026-10-03])。

    读 ``PUBLIC_BASE_URL``;没设时是 ``http://127.0.0.1:8001``(只有本机能打开;对外服务请在 .env 里设)。
    诊断报告分享链接、图片绝对地址、注册短链、支付回调缺省值都从这里取;别处不许再写站点域名字面量
    (锁:tests/public_base_url_2026_10_03,认 f-string)。只依赖标准库,任何模块都能直接导入。
    """
    return (os.environ.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8001").strip().rstrip("/")


def is_owned_image_url(value: Any, *, site_origin: str | None = None) -> bool:
    """这个 URL 是不是我们自己图库里的图？

    ``site_origin`` 只在测试里显式传；生产走 ``PUBLIC_BASE_URL``。
    """
    policy = load_policy()
    prefix = str(policy["path_prefix"])
    schemes = {str(s).lower() for s in policy["allowed_schemes"]}
    brand_re = re.compile(str(policy["brand_segment_pattern"]))

    url = str(value or "")
    if not url or _UNSAFE_CHARS_RE.search(url):
        return False

    if url.startswith("//"):
        # 协议相对 URL：跟着当前协议走外部域名，是绕过同源判定的经典口子。
        return False

    if url.startswith("/"):
        path = url                       # 相对路径 = 同源，天然满足 origin 条件
    else:
        try:
            parsed = urlsplit(url)
        except (TypeError, ValueError):
            return False
        if parsed.scheme.lower() not in schemes:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        expected = (site_origin if site_origin is not None else public_base_origin()).lower()
        # 同源判定按 host 比，忽略协议差异（http/https 同一站点都算自有）。
        if _host_of(origin) != _host_of(expected):
            return False
        path = parsed.path

    # 查询串/片段不参与路径判定，但也不允许夹带（保持判定面最小）。
    if "?" in path or "#" in path:
        return False
    if not path.startswith(prefix):
        return False
    if ".." in path:                      # 路径穿越（含编码前的字面形态）
        return False
    if "%2f" in path.lower() or "%5c" in path.lower():
        return False

    rest = path[len(prefix):]
    segments = rest.split("/")
    if len(segments) != 2:
        return False
    brand_segment, filename = segments
    return bool(brand_re.fullmatch(brand_segment)) and bool(filename.strip())


def _host_of(origin: str) -> str:
    return origin.split("://", 1)[-1].strip("/").lower()
