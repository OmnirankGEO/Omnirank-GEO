"""领域拒绝的**单一出口**:记日志与返 4xx 不可分离。

## 为什么要有这个模块

`BUSINESS_IDENTITY_SSOT_UNAVAILABLE` 这一类拒绝在生产上「24 小时零次」——
**不是没发生,是没人写**。翻译点把领域异常变成 `HTTPException` 就直接抛,
整条路径一行日志都没有:用户看到 409,运维什么都看不到。

🔴 **被正确捕获的错误不留痕** —— 它在日志里和「从未发生过」完全同形。
   于是「零次」既可能是「真没发生」,也可能是「发生了很多次」,
   而这两个结论会导向完全相反的处置。

## 形状:一个调用同时做两件事

`refuse()` **返回**一个 `HTTPException`(调用方写 `raise refuse(...) from exc`),
并在返回前把这次拒绝记下来。两件事绑死在一个调用里,就没有
「记了不抛 / 抛了不记」的中间态 —— 靠注释提醒"别忘了记日志"传不出去,门才传得出去。

## 🔴 `detail` 原样进响应

`detail` 参数**不做任何加工**就进 `HTTPException`:今天有的调用点传 dict、
有的传字符串,那是用户可见契约的一部分。本模块只负责**多写一行日志**,
不改任何响应形状 —— 想顺手统一 detail 形状的话,那是另一个单子,
而且要先问前端在读哪个形状。

`code` 只用于**日志归类**(运维按码 grep),不参与响应构造。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import HTTPException


def refuse(
    logger,
    *,
    status: int,
    code: str,
    detail: Any,
    context: Optional[Mapping[str, Any]] = None,
) -> HTTPException:
    """记一行拒绝日志,并**返回**待抛的 `HTTPException`。

    调用方必须写成 `raise refuse(...) from exc` —— 返回而不是直接抛,
    是为了让 `from exc` 的异常链保持在调用点上(它在 traceback 里能指出
    到底是哪个领域异常触发的)。

    参数
    ----
    logger  : 调用方所在模块的 logger,日志按模块名归属,不集中到本模块。
    status  : HTTP 状态码(4xx)。
    code    : 机器可读的拒绝码,**只进日志**,用于运维按码统计。
    detail  : 原样进 `HTTPException(detail=...)` —— dict 或 str 都不加工。
    context : 附加上下文(actor / target / request_id 等),拼进日志尾部。

    🔴 用 `warning` 不用 `info`:这是**被拒绝的业务动作**,
       有人正在被挡住;生产日志级别通常从 INFO 起,但真正要查这类问题时
       第一件事是 grep WARNING。
    """
    tail = " ".join(
        "%s=%s" % (k, v) for k, v in sorted((context or {}).items())
        if v is not None
    )
    logger.warning(
        "[拒绝] code=%s status=%s%s",
        code, status, (" " + tail) if tail else "",
    )
    return HTTPException(status_code=status, detail=detail)
