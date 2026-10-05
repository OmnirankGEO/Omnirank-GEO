"""外发前的广告法规则门(规格 02 §7 · Codex P0-14)。

## 病历

规格要求「外发前查 `legal_prohibited@1.0.0` pack / category / hit / version / 位置
并局部修复」。实现里:

  · 新 materializer / submit **一处都没有**这道校验;
  · `content_generator.py:344` 只在 **prompt 里提示**模型"别写绝对化用语"。

prompt 提示不是闸。模型是概率的,提示只降低概率;而广告法是**二值**的 ——
写了就是违法,少写 90% 不构成合规。更要命的是:提示发生在**生成时**,
而人可以在校对页把文案改回去,再点发布。生成时的提示对那条路径完全不存在。

## 为什么闸放在"外发前"而不是"生成后"

外发是最后一个我们还能拦住的点。作品在库里躺着违规不产生法律后果,
**发出去**才产生。所以闸的位置就是发出去之前那一步,不是更早也不是更晚。

## 局部修复而不是整体拒绝

规格原文是「局部修复」:命中一个绝对化用语,应当给出**这一处**的位置与替换建议,
而不是把整篇拒掉让用户自己找。所以返回的是逐条 hit(term / 位置 / 上下文摘录),
调用方据此给可执行错误。

## 词表来源

唯一 SSOT = `config/legal_prohibited_pack.json`(Owner 2026-07-23 签发),
经 `services.marketing.legal_context` 读取 —— 本模块**不自建第二份词表**。
语境判别(「第一步」「第一季度」不算绝对化)也复用那一份,
不在这里再写一套 —— 两套语境规则必然漂移,而漂移的方向是**误杀**或**放行**,
两个都是事故。
"""
from __future__ import annotations

from typing import Any, Final, Mapping, Sequence

PACK_ID: Final = "legal_prohibited"


class LegalGateBlocked(RuntimeError):
    """外发前命中法律明令项。**必须**在任何 provider 调用之前抛。

    携带逐条命中,调用方据此给出"哪一处、什么词、怎么改"的可执行提示。
    """

    def __init__(self, hits: list[dict[str, Any]], *, pack_version: str):
        super().__init__(f"命中 {len(hits)} 处法律明令用语")
        self.hits = list(hits)
        self.pack_version = str(pack_version)


def scan_outbound(fields: Mapping[str, Any],
                  *, scan_keys: Sequence[str] = ("title", "body_text", "contact_line")
                  ) -> dict[str, Any]:
    """扫一条待外发内容,返回 `{pack_id, pack_version, hits}`。

    🔴 **逐字段扫**而不是拼成一整段:拼接会在两个字段的边界处造出原文里
       不存在的词(标题结尾 "全国第" + 正文开头 "一家" → "第一"),
       那是凭空捏造的违规。位置信息也会失去意义 —— 用户拿到一个
       指向拼接串的 offset,回到界面上找不到对应的地方。
    """
    from services.marketing.guards import legal_pack_version
    from services.marketing.legal_context import find_absolute_violations

    hits: list[dict[str, Any]] = []
    for key in scan_keys:
        text = str(fields.get(key) or "")
        if not text:
            continue
        for hit in find_absolute_violations(text):
            hits.append({
                "field": key,
                "category": "ad_law_absolute",
                "term": hit.term,
                "start": int(hit.start),
                "end": int(hit.end),
                "excerpt": hit.excerpt,
            })
    return {"pack_id": PACK_ID, "pack_version": legal_pack_version(), "hits": hits}


def assert_outbound_clean(fields: Mapping[str, Any], **kwargs) -> dict[str, Any]:
    """闸本体。有命中即抛 —— 调用方**不得**捕获后继续外发。

    返回值(无命中时)带 `pack_version`,调用方应把它随订单快照落库:
    「这次是按哪一版目录放行的」是事后追责唯一能回答的问题。
    """
    result = scan_outbound(fields, **kwargs)
    if result["hits"]:
        raise LegalGateBlocked(result["hits"], pack_version=result["pack_version"])
    return result


def repair_hint(hits: Sequence[Mapping[str, Any]]) -> str:
    """把逐条命中翻成一句人话。工程术语(offset / pack / category)不外露。"""
    terms = []
    for hit in hits:
        term = str(hit.get("term") or "")
        if term and term not in terms:
            terms.append(term)
    if not terms:
        return "请修改文案后重新提交"
    listed = "、".join("「" + t + "」" for t in terms[:5])
    more = "" if len(terms) <= 5 else f" 等 {len(terms)} 处"
    return f"把 {listed}{more} 换成可以核实的说法(例如具体数据、时间范围)后重新提交"
