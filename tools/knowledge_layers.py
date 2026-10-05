"""
v3.7 道法术器分层 (CTO-15.2 · 2026-04-19)

老板提案:用中国哲学"道法术器"四层模型给行业知识分层 TTL,
变化频率不一样:
  - 道(行业本质): 永不过期,仅 admin 行业洗牌触发
  - 法(中层玩法): 3-6 个月被动刷新,叠加新数据
  - 术(操作方法): 1-3 个月被动刷新,叠加新数据
  - 器(具体素材): 持续叠加,每次 deep_analyze 触发

设计目标:
  - 不破坏 v3.6 接口 (industry_knowledge schema 不动,函数签名不变)
  - 老数据(无 _layer_meta) graceful 处理,按 v3.6 行为兜底
  - 叠加而非覆盖 (按 source_url / term 等业务唯一键去重)
  - 永久归档 (旧版本进 _archived_history,最多保留 5 个)

向下游消费方完全透明 (collect_industry_knowledge/get_industry_context 函数签名零改动)。
"""

from datetime import datetime, timedelta
from typing import Any, Optional

# ========== 字段层级配置 ==========
# 每个字段单独标 layer + ttl_days + merge 策略
# layer: 'dao' | 'fa' | 'shu' | 'qi'
# ttl_days: None=永不过期 / int=多少天后过期需重采
# merge: 'overwrite' (覆盖,适合 string 类) / 'list_dedup' (list 去重叠加) / 'append_only' (永远追加,器层)
# unique_key: list_dedup/append_only 时的去重字段(无则按整体 JSON 序列化去重)

LAYER_CONFIG = {
    # ============ L1 行业层 ============
    # === 道 (永不过期) ===
    "market_overview":     {"layer": "dao", "ttl_days": None, "merge": "overwrite"},
    "market_trend":        {"layer": "dao", "ttl_days": None, "merge": "overwrite"},
    "policy_redlines":     {"layer": "dao", "ttl_days": None, "merge": "list_dedup"},
    "content_redlines":    {"layer": "dao", "ttl_days": None, "merge": "list_dedup"},
    "industry_terms":      {"layer": "dao", "ttl_days": None, "merge": "list_dedup"},
    "common_categories":   {"layer": "dao", "ttl_days": None, "merge": "list_dedup"},

    # === 法 (120 天 / 4 个月) ===
    "industry_jargon":     {"layer": "fa",  "ttl_days": 120, "merge": "list_dedup", "unique_key": "term"},
    "counter_consensus":   {"layer": "fa",  "ttl_days": 120, "merge": "list_dedup", "unique_key": "insight"},
    "top_brands":          {"layer": "fa",  "ttl_days": 120, "merge": "list_dedup"},
    "price_range":         {"layer": "fa",  "ttl_days": 120, "merge": "overwrite"},

    # === 术 (45 天 / 1.5 个月) ===
    "authority_sources":   {"layer": "shu", "ttl_days": 45, "merge": "list_dedup", "unique_key": "url"},

    # ============ L2 品类层 ============
    # === 法 (120 天) ===
    "typical_products":    {"layer": "fa",  "ttl_days": 120, "merge": "list_dedup"},
    "target_audience":     {"layer": "fa",  "ttl_days": 120, "merge": "overwrite"},
    "differentiation_angles": {"layer": "fa", "ttl_days": 120, "merge": "list_dedup"},

    # === 术 (45 天) ===
    "conversion_paths":    {"layer": "shu", "ttl_days": 45, "merge": "list_dedup"},
    "hot_formats":         {"layer": "shu", "ttl_days": 45, "merge": "list_dedup"},
    "content_types":       {"layer": "shu", "ttl_days": 45, "merge": "list_dedup"},
    "cta_templates":       {"layer": "shu", "ttl_days": 45, "merge": "list_dedup"},
    "platform_tips":       {"layer": "shu", "ttl_days": 45, "merge": "overwrite"},

    # === 器 (永远叠加) ===
    "user_voices_pool":    {"layer": "qi",  "ttl_days": None, "merge": "append_only", "unique_key": "__source_url"},
    "case_evidence_pool":  {"layer": "qi",  "ttl_days": None, "merge": "append_only", "unique_key": "__source_url"},
}

# 所有层
ALL_LAYERS = ("dao", "fa", "shu", "qi")

# 元数据 key 前缀(知识 dict 内部用)
META_PREFIX = "_v37_meta"


def get_layer(field_name: str) -> str:
    """返回字段所属层,未配置默认 'fa'(法层,中等 TTL 安全侧)"""
    return LAYER_CONFIG.get(field_name, {}).get("layer", "fa")


def get_field_ttl_days(field_name: str) -> Optional[int]:
    """返回字段 TTL 天数, None=永不过期"""
    return LAYER_CONFIG.get(field_name, {}).get("ttl_days", 120)


def get_layer_ttl_days(layer: str) -> Optional[int]:
    """每层的代表 TTL(用于判断整层是否需要刷新)"""
    return {"dao": None, "fa": 120, "shu": 45, "qi": None}.get(layer)


def fields_in_layer(layer: str) -> list:
    """返回该层下的所有字段名"""
    return [f for f, cfg in LAYER_CONFIG.items() if cfg["layer"] == layer]


def get_meta(knowledge: dict) -> dict:
    """读取 knowledge 内部的 v3.7 元数据 dict"""
    return knowledge.get(META_PREFIX, {}) if isinstance(knowledge, dict) else {}


def set_layer_refreshed(knowledge: dict, layer: str, when: Optional[datetime] = None) -> dict:
    """标记某层刷新时间(原地修改 + 返回 knowledge)"""
    when = when or datetime.now()
    if META_PREFIX not in knowledge:
        knowledge[META_PREFIX] = {}
    knowledge[META_PREFIX].setdefault("layers", {})[layer] = {
        "refreshed_at": when.isoformat(),
        "next_refresh_at": (when + timedelta(days=ttl)).isoformat() if (ttl := get_layer_ttl_days(layer)) else None,
    }
    return knowledge


def needs_refresh(knowledge: dict, layer: str, now: Optional[datetime] = None) -> bool:
    """判断某层是否需要刷新(到期/不存在 meta)"""
    if layer == "dao":
        # 道层: 仅当字段不存在才采集(rebuild 时强制)
        any_dao_field = any(f in knowledge for f in fields_in_layer("dao"))
        return not any_dao_field
    if layer == "qi":
        # 器层: 永远叠加(不主动刷新,只在 deep_analyze 时累加)
        return False
    now = now or datetime.now()
    meta = get_meta(knowledge).get("layers", {}).get(layer)
    if not meta:
        # 老数据无 meta → 视为需要刷新(首次升级到 v3.7 时全部 refresh)
        return True
    next_refresh = meta.get("next_refresh_at")
    if not next_refresh:
        return False
    try:
        return now > datetime.fromisoformat(next_refresh)
    except Exception:
        return True


def merge_field_value(field_name: str, existing_value: Any, new_value: Any) -> Any:
    """按字段配置合并 existing 和 new

    - overwrite: 直接用 new (适合单值 string)
    - list_dedup: list 合并去重 (按 unique_key 或整体)
    - append_only: list 永远追加 (器层,只过滤已有 unique_key)
    """
    cfg = LAYER_CONFIG.get(field_name, {"merge": "overwrite"})
    strategy = cfg.get("merge", "overwrite")
    unique_key = cfg.get("unique_key")

    if strategy == "overwrite" or new_value is None:
        return new_value if new_value is not None else existing_value

    if not isinstance(existing_value, list):
        existing_value = []
    if not isinstance(new_value, list):
        return existing_value  # 异常情况,保留旧

    if not existing_value:
        return new_value

    # list_dedup / append_only 都需要去重
    seen_keys = set()
    if unique_key:
        # 按 unique_key 字段去重(item 必须是 dict)
        for item in existing_value:
            if isinstance(item, dict):
                k = item.get(unique_key)
                if k:
                    seen_keys.add(k)
        merged = list(existing_value)
        for item in new_value:
            if isinstance(item, dict):
                k = item.get(unique_key)
                if k and k in seen_keys:
                    continue
                if k:
                    seen_keys.add(k)
                merged.append(item)
            else:
                merged.append(item)
        return merged
    else:
        # 按整体 JSON 序列化去重 (适合 string list 如 industry_terms)
        import json as _json
        seen_repr = set()
        merged = []
        for item in existing_value + new_value:
            try:
                key = _json.dumps(item, ensure_ascii=False, sort_keys=True) if not isinstance(item, str) else item
            except Exception:
                key = str(item)
            if key in seen_keys or key in seen_repr:
                continue
            seen_repr.add(key)
            merged.append(item)
        return merged


def merge_with_layers(existing: dict, new_data: dict, archive_old: bool = True) -> dict:
    """智能合并 v3.7 分层数据

    Args:
        existing: 当前 industry_knowledge.knowledge dict (可能为空 {} 或老数据无 meta)
        new_data: 本次新采集的字段 dict
        archive_old: 是否把旧版本归档到 _archived_history (建议 True)

    Returns:
        合并后的 knowledge dict (含更新的 _v37_meta)
    """
    if not isinstance(existing, dict):
        existing = {}
    if not isinstance(new_data, dict):
        return existing

    # 拷贝 existing 作为 base (避免原地修改影响调用方)
    merged = dict(existing)

    # 归档旧版本 (在合并前)
    if archive_old and existing:
        meta = merged.setdefault(META_PREFIX, {})
        history = meta.get("archived_history") or []
        # 旧 snapshot 不含历史本身(避免无限嵌套)
        snapshot = {k: v for k, v in existing.items() if k != META_PREFIX}
        snapshot["_snapshot_at"] = datetime.now().isoformat()
        snapshot["_snapshot_version"] = meta.get("version", 1)
        history.append(snapshot)
        # 限制历史最多 5 个版本(避免 JSONB 无限膨胀)
        meta["archived_history"] = history[-5:]

    # 逐字段合并
    for field, new_value in new_data.items():
        if field == META_PREFIX:
            continue  # meta 单独处理
        existing_value = merged.get(field)
        merged[field] = merge_field_value(field, existing_value, new_value)

    # 更新版本号
    meta = merged.setdefault(META_PREFIX, {})
    meta["version"] = (meta.get("version") or 0) + 1
    meta["updated_at"] = datetime.now().isoformat()

    return merged


def select_fields_to_collect(existing: dict, all_fields_by_layer: dict, now: Optional[datetime] = None) -> list:
    """决定本次需要采集哪些字段

    Args:
        existing: 当前 knowledge dict
        all_fields_by_layer: {"dao": [field, ...], "fa": [...], ...} 该 collector 支持的所有字段按层分组
        now: 时间(测试用)

    Returns:
        需要重新采集的字段名列表
    """
    now = now or datetime.now()
    to_collect = []
    for layer, fields in all_fields_by_layer.items():
        if needs_refresh(existing, layer, now):
            to_collect.extend(fields)
    return to_collect


def stamp_layers_refreshed(knowledge: dict, layers: list, when: Optional[datetime] = None) -> dict:
    """批量标记多个层已刷新(采集完后调)"""
    for layer in layers:
        if layer != "qi":  # 器层不需要 refresh 标记(永远叠加)
            set_layer_refreshed(knowledge, layer, when)
    return knowledge


def reset_industry_for_rebuild(knowledge: dict, admin_user_id: int, reason: Optional[str] = None) -> dict:
    """admin 行业洗牌触发: 清空所有层数据(归档旧版) + rebuild_count + 1

    用于行业重大变革(如 GPT-5 发布、AI 搜索引擎重新洗牌)。
    """
    meta = knowledge.get(META_PREFIX, {})
    rebuild_count = (meta.get("rebuild_count") or 0) + 1

    # 归档旧版到历史
    history = meta.get("archived_history") or []
    snapshot = {k: v for k, v in knowledge.items() if k != META_PREFIX}
    snapshot["_snapshot_at"] = datetime.now().isoformat()
    snapshot["_snapshot_reason"] = "rebuild"
    snapshot["_snapshot_admin"] = admin_user_id
    history.append(snapshot)
    history = history[-5:]

    # 返回空 knowledge (只留 meta)
    return {
        META_PREFIX: {
            "version": 0,
            "rebuild_count": rebuild_count,
            "rebuilt_at": datetime.now().isoformat(),
            "rebuilt_by": admin_user_id,
            "rebuild_reason": reason,
            "archived_history": history,
            "layers": {},
        }
    }
