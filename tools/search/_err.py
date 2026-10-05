"""检索供应商异常 → **永不为空**的可读描述(单点 SSOT)。

## 为什么需要这个文件

[2026-08-08 Deploy-CTO 实测] 两家外部检索供应商失败时,错误信息**双双被吞成空串**,
生产日志里看不出任何东西:

  · 秘塔 `metaso_mcp.py` 三处 `except Exception as e` 都写 `{"error": str(e)}`,
    而 `ssl.SSLEOFError` 的 `str()` 是 **空串** → 消费方拿到 `{"error": ""}`
    = 有响应、字段齐全、错误为空,**看起来像成功**;
  · 豆包 `doubao_search.py` 写 `f"{type(exc).__name__}: {str(exc)[:160]}"`,
    而真实链条是 `ConnectError('') ← EndOfStream('') ← SSLEOFError('[SSL: UNEXPECTED_EOF…]')`
    —— **外两层 str() 全空**,日志只剩 `ConnectError: `,真消息在第 3 层。

当天实测原文:

    秘塔 修前: {"error": ""}
    豆包 修前: DoubaoSearchError: 'ConnectError: '
    cause 链: [('ConnectError',''), ('ConnectError',''), ('EndOfStream',''),
               ('SSLEOFError','[SSL: UNEXPECTED_EOF_WHILE_READING] …')]

🔴 这不只是"日志不好看":任何「调用没抛异常 / 有没有 error 字段」的判据在这种返回上**恒绿**,
排查时会把"供应商全挂"误读成"这次没搜到结果"。

## 口径

`describe_exc()` 沿 `__cause__` / `__context__` 走链,取**第一个非空消息**并带上类型链;
一个消息都没有时回落到类型链本身。**保证返回非空字符串** —— 这是本模块存在的唯一理由。
"""

from __future__ import annotations

_MAX_DEPTH = 6


def _chain(exc: BaseException) -> list[BaseException]:
    """外层 → 内层。`__cause__`(显式 raise from)优先于 `__context__`(隐式)。"""
    out: list[BaseException] = [exc]
    seen = {id(exc)}
    cur: BaseException | None = exc
    while cur is not None and len(out) < _MAX_DEPTH:
        nxt = cur.__cause__ or cur.__context__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        out.append(nxt)
        cur = nxt
    return out


def describe_exc(exc: BaseException, *, limit: int = 160) -> str:
    """把异常(含 cause 链)描述成**永不为空**的一行。

    · 有消息 → ``ConnectError←SSLEOFError: [SSL: UNEXPECTED_EOF…]``
      (类型链保留"外层是什么"这个信息,消息取链上第一个非空的)
    · 全链无消息 → ``ConnectError←EndOfStream <无消息>``(至少让人看见类型)
    """
    chain = _chain(exc)
    types = "←".join(type(e).__name__ for e in chain)
    for e in chain:
        msg = str(e).strip()
        if msg:
            return f"{types}: {msg}"[:limit] if limit else f"{types}: {msg}"
    # 🔴 整条链一个消息都没有(SSLEOFError 就常是这样)—— 绝不返回空串
    return f"{types} <无消息>"[:limit] if limit else f"{types} <无消息>"
