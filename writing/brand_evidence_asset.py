"""P2-1 · 历史事实资产复用(2026-08-14,依赖 P0-2 实体绑定)。

研究定稿结论:本仓**没有**按品牌沉淀"已核验事实"的资产层 —— 每篇文章的
evidence_pack 一次性冻结,同品牌下一篇从零检索。本模块把品牌历史文章里
**已核验**(VERIFIED_STATES)且**实体绑定未判异**(P0-2)的证据条目捞回来,
作为新篇初始素材复用:

  - 已核验条目的核验血缘(canonical_body_hash/span/verifier 版本)原样携带 ——
    复用的是**核验过的事实**,不是"上次搜到过"的搜索结果;
  - `binding_state == different_entity` 的条目绝不复用(错实体资产复用一次
    就是污染扩散一次 —— P0-2 的隔离语义在资产层同样成立);
  - 与本篇问题做确定性词面相关性初筛(≥2 个二元组重叠),不相关的历史证据
    不硬塞(复用不是填充);
  - 品牌桥走 quotes.brand_id(研究定稿 J11:articles.brand_id 全表 0 非空,
    直接查它命中恒 0 且不报错)。

分类:O1 —— 任何失败返回空列表,新篇按现行为从零检索,绝不阻断。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Final

logger = logging.getLogger("GEO-BrandEvidenceAsset")

BRAND_EVIDENCE_ASSET_VERSION: Final = "brand-evidence-asset-v1.0"
_TOKEN_RE: Final = re.compile(r"[一-鿿a-zA-Z0-9]+")


def _bigrams(text: str) -> set[str]:
    joined = "".join(_TOKEN_RE.findall(str(text or "")))
    return {joined[i:i + 2] for i in range(len(joined) - 1)} if len(joined) > 1 else set()


def select_reusable_items(
    packs: list[dict[str, Any]],
    *,
    question: str,
    limit: int = 6,
    exclude_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    """从历史 pack 列表里选可复用条目(纯函数,判别测试打这里)。

    规则(R2-1):已核验 ∧ **binding_state == entity_confirmed** ∧ 与问题词面
    相关(≥2 二元组重叠)∧ URL 去重。

    🔴 [R2-1 2026-08-15] 旧口径只排除 different_entity —— 生产 1,097 条 verified
    历史条目**全部缺 binding_state**,会被当成"该品牌的证据"整体放行复用
    (别家公司/行业泛化/旧客户的事实包装成当前客户证据 = 对象身份完整性问题)。
    新口径:**只有确认同主体的条目才可复用**;缺 binding / unverified /
    different 一律保持候选态(不复用、不删数据、不阻断写作 —— 新篇按现行为
    自行检索)。存量重绑走 scripts/rebind_assessment_2026_08_15.py 只读评估。
    """
    from writing.evidence_pack import VERIFIED_STATES, raw_pack_items

    q_grams = _bigrams(question)
    seen: set[str] = set(exclude_urls or set())
    out: list[dict[str, Any]] = []
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        # [R5] 结构访问器取全量;本函数自带比统一门更严的复用门
        # (VERIFIED + entity_confirmed,主题 lane 也不复用)。
        for item in raw_pack_items(pack):
            if item.get("verification_status") not in VERIFIED_STATES:
                continue
            if str(item.get("binding_state") or "") != "entity_confirmed":
                continue  # R2-1:未确认同主体(含缺 binding)= 候选,不复用
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            hay = _bigrams(f"{item.get('title') or ''} {item.get('claim') or ''}")
            if not q_grams or len(q_grams & hay) < 2:
                continue  # 与本篇问题不相关的历史证据不硬塞
            seen.add(url)
            reused = dict(item)
            reused["scope"] = (
                f"{str(item.get('scope') or '').strip()}"
                f"；历史核验资产复用({BRAND_EVIDENCE_ASSET_VERSION})"
            ).strip("；")
            out.append(reused)
            if len(out) >= max(0, int(limit)):
                return out
    return out


def load_brand_verified_evidence(
    brand_id: int,
    *,
    question: str,
    limit: int = 6,
    exclude_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    """捞该品牌近 20 篇文章的 evidence_pack,选可复用的已核验条目。O1 fail-soft。"""
    if not brand_id:
        return []
    try:
        from db.connection import get_db

        with get_db() as conn:
            cur = conn.cursor()
            # 🔴 品牌桥 = quotes.brand_id(J11:articles.brand_id 全表 0 非空)。
            cur.execute(
                """
                SELECT a.evidence_pack
                  FROM articles a
                  JOIN quotes q ON q.id = a.quote_id
                 WHERE q.brand_id = %s
                   AND a.evidence_pack IS NOT NULL
                 ORDER BY a.id DESC
                 LIMIT 20
                """,
                (int(brand_id),),
            )
            packs = [row.get("evidence_pack") for row in cur.fetchall() or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[P2-1] 品牌 %s 历史证据读取失败(按无资产处理): %s", brand_id, str(exc)[:200])
        return []
    return select_reusable_items(
        [p for p in packs if isinstance(p, dict)],
        question=question, limit=limit, exclude_urls=exclude_urls,
    )
