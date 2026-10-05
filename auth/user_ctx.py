# -*- coding: utf-8 -*-
"""`request.state.user` 的**单点**访问器。

## 为什么需要它

`request.state.user` 是 JWT payload(或 perm_version 软刷新时的规范化 dict),
键是 `user_id / username / is_admin / roles / permissions / …` —— **没有 `id`**。
软刷新那条分支显式写 `"user_id": fresh_user["id"]`,把 DB 行的 `id` **改名**成了 `user_id`。

于是全仓出现两种裸读,后果不同:

    user["id"]        ⇒ KeyError ⇒ 端点 500          (响亮)
    user.get("id")    ⇒ 恒 None  ⇒ None 被写进业务字段与日志  (**静默**,更难发现)

2026-09-05 数据流普查(`scripts/user_id_flow_census.py`)实测:
**119 处 / 46 个文件**,其中 114 处读 `'id'`。少数模块自己补过键
(`api/admin_withdrawal_api.py` 的 `_get_admin` 写 `user["id"] = user.get("user_id") or …`),
所以访问器要**同时**认这两种形态。

## 为什么是**一处**,不是每个文件一份

同一个谓词写 44 份,下一次改口径必有一处被漏掉 —— 今天修的
`_operating_profit` ×2 / 渠道费 SQL ×3 / `_is_standard_price` ×5 全是这个病。
`api/faq_api.py` 与 `api/research_monitor_industry_api.py` 里已有各自的 `_user_id`
(C 的 #78 修复),它们应当在合流后改引本模块 —— 不要变成第三、第四份。

本模块**零依赖**(不 import 任何项目内模块),可被任何层安全引用。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = ["current_user_id", "require_user_id"]


def current_user_id(user: Optional[Mapping[str, Any]], default: int = 0) -> int:
    """从 `request.state.user`(或它派生的 dict)取当前用户 id。

    取值顺序 `id` → `user_id` → `default`:
      · `id` 在前 —— 少数模块会先注入它(注入后它才是权威值);
      · `user_id` 是**中间件实际写入**的键,绝大多数情况走这一支;
      · 都没有(user 为 None / 空 dict)⇒ `default`(默认 0)。

    🔴 不抛异常:调用点大多在日志/归属字段上,抛异常会把「取不到 id」升级成 500。
       需要「必须有 id」的语义用 `require_user_id()`。
    """
    if not user:
        return default
    for key in ("id", "user_id"):
        try:
            v = user.get(key)  # type: ignore[union-attr]
        except AttributeError:
            v = None
        # 🔴 `0` 也要跳过 —— 它不是有效用户 id,而是「没取到」的常见占位。
        #    第一版只排除 None / "",于是 `{"id": 0, "user_id": 9}` 返回 **0** 而不是 9。
        #    是下面那组参数化自证抓到的:少了那一格,这个访问器会在
        #    「模块注入了 id=0 的兜底值」时把真 id 吃掉。
        if not v:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            # portal token 形态是 `portal_<quote_id>` 这类非数字 —— 不是整型用户 id
            continue
    return default


def require_user_id(user: Optional[Mapping[str, Any]]) -> int:
    """取不到就抛 —— 用在「没有 id 就不该继续」的写路径上。

    与 `current_user_id` 分开,是因为两者的**失败处置相反**:
    一个要静默兜底(日志/归属),一个必须中断(落库/扣费)。
    压成一个函数,调用点就分不出自己在哪一种语义里。
    """
    uid = current_user_id(user, default=0)
    if not uid:
        raise ValueError(
            "取不到当前用户 id:request.state.user 里既没有 `user_id` 也没有 `id`。"
            "这通常意味着鉴权中间件没跑到,或这是 portal token 形态(非整型用户)。"
        )
    return uid
