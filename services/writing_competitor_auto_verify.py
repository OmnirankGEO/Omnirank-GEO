"""[WP9-P0-2] 竞品名称自动核验桥 —— 从本品牌监测蒸馏取"AI 真实答案里高频出现"的品牌,
作为写作 competitor_mode 门的自动核验源。

原则(禁虚构竞品一字不动):
- 本桥**只核验已经在 quote.competitor_list 里的名称**,绝不新增/虚构任何品牌名。
- 核验依据 = 该品牌自己的监测蒸馏(db.distillation_db.get_competitor_radar,同租户·同行业
  的真实 AI 答案聚合)里,某竞品被 AI 真实答案提及 ≥ N 次(默认 3)。命中即视同
  name_verified=True,证据链落血缘 = 监测蒸馏 + 出现次数。
- 蒸馏无数据 / 读取失败 → 返回空 map → 写作门行为与接桥前完全一致(fail-soft)。

名称对齐:竞品名先经 brand_aliases 归一(distillation_db.normalize_brand_name),避免别名把
同一主体拆成多条计数不足 3。
"""
from __future__ import annotations

from typing import Any, Dict

# 血缘常量:落到被核验竞品项上,便于审计/回溯核验来源。
AUTO_VERIFY_METHOD = "distillation_radar_v1"
AUTO_VERIFY_SOURCE = "monitoring_distillation"
DEFAULT_MIN_MENTIONS = 3
DEFAULT_WINDOW_DAYS = 30


def _norm(name: str) -> str:
    """归一竞品名(经别名表);别名表无记录时回落原名 strip。"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    try:
        from db.distillation_db import normalize_brand_name
        canonical = normalize_brand_name(raw)
        return str(canonical or raw).strip()
    except Exception:
        return raw


def auto_verified_competitor_map(
    brand_id: Any,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    min_mentions: int = DEFAULT_MIN_MENTIONS,
) -> Dict[str, Dict[str, Any]]:
    """返回 {归一化竞品名: {mention_count, method, source}}。

    只含在本品牌监测蒸馏里 AI 真实答案出现 ≥ ``min_mentions`` 次的竞品。
    brand_id 不可用 / 蒸馏无数据 / 任何异常 → 返回空 dict(行为不变)。
    """
    try:
        bid = int(brand_id)
    except (TypeError, ValueError):
        return {}
    if bid <= 0:
        return {}
    try:
        from db.distillation_db import get_competitor_radar
        radar = get_competitor_radar(bid, days=days)
    except Exception:
        return {}
    brands = (radar or {}).get("brands") or []
    out: Dict[str, Dict[str, Any]] = {}
    for entry in brands:
        if not isinstance(entry, dict):
            continue
        try:
            count = int(entry.get("mention_count") or 0)
        except (TypeError, ValueError):
            continue
        if count < int(min_mentions):
            continue
        canonical = _norm(entry.get("brand_name"))
        if not canonical:
            continue
        # 同一归一名保留最高出现次数
        prev = out.get(canonical)
        if prev is None or count > int(prev.get("mention_count") or 0):
            out[canonical] = {
                "mention_count": count,
                "method": AUTO_VERIFY_METHOD,
                "source": AUTO_VERIFY_SOURCE,
            }
    return out


def annotate_competitor_verification(
    competitors: list,
    verified_map: Dict[str, Dict[str, Any]],
) -> int:
    """把 verified_map 命中的竞品项就地标 name_verified=True + 血缘;返回本次新核验条数。

    只标已存在的项(禁虚构);已 human_verified_name / name_verified 的项不覆盖其原始来源,
    但仍确保 name_verified=True。verified_map 为空 → 零改动。
    """
    if not verified_map:
        return 0
    newly = 0
    for item in competitors:
        if not isinstance(item, dict):
            continue
        if item.get("name_verified") is True or item.get("human_verified_name") is True:
            continue  # 已核验:不动其原始血缘
        canonical = _norm(item.get("name"))
        hit = verified_map.get(canonical)
        if not hit:
            continue
        item["name_verified"] = True
        item["name_verification_method"] = hit["method"]
        item["name_verified_mention_count"] = hit["mention_count"]
        item["verify_source"] = hit["source"]
        newly += 1
    return newly
