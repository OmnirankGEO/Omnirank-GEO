"""Versioned product-facts SSOT loader for the GEO acquisition content center.

管理与策略导师同款的「可替换不硬编码」哲学:事实内容全部在
``config/product_facts_pack.json``,代码只做加载、校验与按上下文选条目——
改包不发版(文件 mtime 变化即热生效)。

版本与可溯:
- ``resolve_facts_pack(pack_id=None, version=None)`` 返回 ``{pack_id, version,
  facts, ...}``;``version=None`` 读当前包文件声明的版本。
- 生成任务在创建时把 ``{pack_id, version, facts(选定子集副本)}`` 冻结进
  geo_snapshot.product_facts;旧 job 永远按冻结版本生成,不跟随后来的包更新。
- 显式请求一个与当前文件不符的版本 → fail-closed ``product_facts_version_not_found``
  (不做静默版本漂移;历史版本的重放以快照冻结内容为准,不靠重新解析)。

prompt 注入面:``select_facts`` 只取 concept/capability/differentiator/terminology
四类;``forbidden``(禁用表述)由 guards.py 出口守卫硬执行,不重复进 prompt,
管理端点(GET /api/marketing/product-facts)完整可见。
"""
from __future__ import annotations

import copy
import json
import os
from typing import Optional

PACK_ID = "product_facts"
PACK_VERSION = "1.0.0"  # 本代码随附的包版本(config 文件声明值应与之对齐)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "product_facts_pack.json",
)

# prompt 可注入的事实类别;forbidden 由出口守卫执行,管理端点另可见。
PROMPT_KINDS = ("concept", "capability", "differentiator", "terminology")
REQUIRED_FACT_KEYS = ("id", "kind", "name", "one_liner", "provenance")
FACT_KINDS = PROMPT_KINDS + ("forbidden",)

_cache: dict = {"mtime": None, "pack": None}


def _validate_pack(data: dict, *, path: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("product_facts_pack_invalid")
    if str(data.get("pack_id") or "") != PACK_ID:
        raise ValueError("product_facts_pack_id_mismatch")
    if not str(data.get("version") or "").strip():
        raise ValueError("product_facts_pack_version_missing")
    facts = data.get("facts")
    if not isinstance(facts, list) or not facts:
        raise ValueError("product_facts_pack_facts_missing")
    seen: set = set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("product_facts_pack_fact_invalid")
        for key in REQUIRED_FACT_KEYS:
            if not str(fact.get(key) or "").strip():
                raise ValueError(f"product_facts_pack_fact_missing:{key}")
        if str(fact["id"]) in seen:
            raise ValueError("product_facts_pack_fact_duplicate")
        seen.add(str(fact["id"]))
        if str(fact["kind"]) not in FACT_KINDS:
            raise ValueError("product_facts_pack_fact_kind_unknown")
        if str(fact["provenance"]) != "system_verified":
            # 事实包只收录平台核验事实;其余出处走 evidence 四分,不进包。
            raise ValueError("product_facts_pack_provenance_invalid")
    return data


def _load_pack(path: str = _CONFIG_PATH) -> dict:
    """热加载:mtime 不变走缓存,变了重读重校验。"""
    mtime = os.path.getmtime(path)
    cached = _cache.get("pack")
    if cached is not None and _cache.get("mtime") == mtime:
        return cached
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    pack = _validate_pack(data, path=path)
    _cache["mtime"] = mtime
    _cache["pack"] = pack
    return pack


def resolve_facts_pack(pack_id: Optional[str] = None, version: Optional[str] = None) -> dict:
    """解析当前事实包 → {pack_id, version, facts, ...}。

    ``pack_id=None`` 取平台默认包;显式 pack_id 必须是已注册包。
    ``version=None`` 取包文件声明的当前版本;显式版本必须与文件一致,
    否则 fail-closed(历史版本重放走 geo_snapshot 冻结内容,不重新解析)。
    """
    if pack_id is not None and str(pack_id) != PACK_ID:
        raise ValueError("product_facts_pack_not_found")
    pack = _load_pack()
    if version is not None and str(version) != str(pack["version"]):
        raise ValueError("product_facts_version_not_found")
    return pack


def select_facts(pack, *, quick_task: Optional[str] = None, channel: Optional[str] = None,
                 angle: Optional[str] = None, limit: int = 6, kinds=PROMPT_KINDS) -> list:
    """按 quick_task/渠道/角度选相关条目,确定性排序,limit 控制 token。

    评分:quick_task 命中 +2、channel 命中 +2、angle 命中 +1;kind concept
    额外 +1(GEO 概念优先打底)。relevance 缺失=通配(恒入选)。同分保持
    包内原始顺序(排序稳定),结果可复现。
    """
    facts = pack.get("facts") if isinstance(pack, dict) else pack
    allowed = kinds or PROMPT_KINDS
    scored: list = []
    for fact in facts or []:
        if not isinstance(fact, dict) or str(fact.get("kind") or "") not in allowed:
            continue
        relevance = fact.get("relevance") or {}
        score = 0
        if quick_task and quick_task in (relevance.get("quick_tasks") or []):
            score += 2
        if channel and channel in (relevance.get("channels") or []):
            score += 2
        if angle and angle in (relevance.get("angles") or []):
            score += 1
        if not relevance:
            score = max(score, 1)  # 无 relevance=通配条目
        if not (quick_task or channel or angle):
            score = max(score, 1)
        if str(fact.get("kind") or "") == "concept":
            score += 1
        if score:
            scored.append((score, fact))
    scored.sort(key=lambda item: -item[0])
    return [fact for _, fact in scored[: max(int(limit), 0)]]


def freeze_facts_for_snapshot(pack: dict, *, quick_task: Optional[str] = None,
                              channels: Optional[list] = None, angles: Optional[dict] = None,
                              limit: int = 10) -> dict:
    """冻结进 geo_snapshot 的事实子集:quick_task 命中 ∪ 各渠道/角度命中。

    返回 ``{pack_id, version, facts}``,facts 为深拷贝——之后包文件热更新
    不会回流进已冻结快照,旧 job 永远按冻结版本与冻结条目生成。
    """
    seen: set = set()
    chosen: list = []

    def _add(facts: list) -> None:
        for fact in facts:
            fact_id = str(fact.get("id") or "")
            if fact_id and fact_id not in seen:
                seen.add(fact_id)
                chosen.append(fact)

    _add(select_facts(pack, quick_task=quick_task, limit=limit))
    for channel in channels or []:
        _add(select_facts(pack, channel=str(channel),
                          angle=(angles or {}).get(str(channel)), limit=4))
    return {
        "pack_id": str(pack.get("pack_id") or PACK_ID),
        "version": str(pack.get("version") or ""),
        "facts": copy.deepcopy(chosen[: max(int(limit), 0)]),
    }


def prompt_projection(facts: list, *, max_points: int = 2, point_len: int = 60) -> list:
    """进 LLM prompt 的紧凑投影(控 token):id/name/value/截断 points/provenance。"""
    projection: list = []
    for fact in facts or []:
        projection.append({
            "id": str(fact.get("id") or "")[:40],
            "name": str(fact.get("name") or "")[:40],
            "value": str(fact.get("one_liner") or "")[:120],
            "points": [str(point)[:point_len] for point in (fact.get("points") or [])[:max_points]],
            "provenance": str(fact.get("provenance") or "system_verified"),
        })
    return projection


def pack_summary(pack: dict) -> dict:
    """管理端点只读视图:版本 + 条数 + 分类计数 + 全部条目(不做在线编辑)。"""
    facts = list(pack.get("facts") or [])
    counts: dict = {}
    for fact in facts:
        kind = str(fact.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "pack_id": str(pack.get("pack_id") or PACK_ID),
        "version": str(pack.get("version") or ""),
        "updated_at": str(pack.get("updated_at") or ""),
        "fact_count": len(facts),
        "counts_by_kind": counts,
        "source_docs": list(pack.get("source_docs") or []),
        "facts": copy.deepcopy(facts),
    }
