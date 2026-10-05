"""合同链里那些**落进 uuid 列**的标识符,及其入口校验(返工 2026-08-18 · 链 3)。

## 病历

`geo_douyin_post_tasks.request_id` / `.batch_item_request_id`、
`geo_douyin_publish_artifacts.request_id`、`mhz_publish_order_items.item_request_id`
四个列都是 **uuid**。而 DTO 上它们全是 `str`,前端发的是
`item-<slot>` / `pi-<id>` / `prep-<id>-<rev>`。于是:

  · Pydantic **放行**(它只知道"这是个字符串");
  · INSERT 那一刻 `InvalidTextRepresentation` ⇒ **500**;
  · 而 500 出现在事务中途,前面的 claim / slot CAS 全部要回滚 ——
    用户看到的是"服务器错误",不是"你发的东西不对"。

**模型放行 ≠ 库放行。** 契约有两道,DTO 只是第一道。

## 为什么在入口挡而不是等库抛

「等库抛」在功能上也能拒绝,但代价完全不同:

  · 500 vs 422 —— 前者是"我们坏了",后者是"你发的不对"。运维告警、
    用户文案、重试语义三处全不一样;
  · 500 发生在**副作用中途**,靠 rollback 兜底;422 发生在任何副作用之前,
    是结构性的零副作用,不依赖"记得写 try/except";
  · 库层报错的文本是 `invalid input syntax for type uuid`,
    它出现在日志里,而不是出现在**判据**里 —— 没人能据此改前端。

## 为什么不直接把 DTO 字段声明成 `uuid.UUID`

那样 Pydantic 会把值转成 `UUID` 对象,再往下所有 `str(...)` 拼接、
字典键比较、`item_request_id` 与预览响应对表的地方都要跟着改一遍,
改动面比问题大得多。这里只做**形态校验**,值仍是规范化后的字符串。
"""
from __future__ import annotations

import uuid as _uuid
from typing import Annotated

from pydantic import AfterValidator


def normalize_uuid(value: str) -> str:
    """校验并规范化成小写带连字符的 uuid 字符串。

    🔴 规范化(而不是原样通过)的理由:`AAAA...`、`aaaa...`、无连字符三种写法
       在 PG 里是**同一个** uuid,但在 Python 侧当字典键用时是三个不同的键。
       预览响应与提交请求要按 `item_request_id` 对表 —— 不规范化,
       大小写不同就对不上,而错误现场会指向"缺少服务端定价"这种误导性的话。
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("标识符不能为空")
    try:
        return str(_uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            f"{raw!r} 不是合法的 uuid。这个字段会写进数据库的 uuid 列,"
            "请用 imageNoteApi.ts 的 newRequestId() / stableRequestId() 生成"
        ) from None


#: DTO 上直接用:`item_request_id: UuidStr`。
UuidStr = Annotated[str, AfterValidator(normalize_uuid)]
