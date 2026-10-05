"""
action_personalizer — M2 W3 · 行动建议个性化(老板硬要求 E)

CTO-B 2026-04-26 · feat/m2-diagnosis-report-v2-full

老板硬要求 E:
  不能再写"完善百科、发媒体、做 FAQ"这种通用建议。
  每条行动至少包含:
    - 对应薄弱维度
    - 目标关键词层(品牌/本地/场景)
    - 推荐发布内容(具体到行业 + 城市 + 关键词)
    - 证据来源 / 为什么这样做
    - 30 天验收口径

输入:
  - todos: Module 6 ICE 排序后的 top N(每条含 issue / action / impact / confidence / ease)
  - context: {brand_name, industry, city, cities, competitors, weak_dimensions, strata, evidence_total}

输出:
  PersonalizedAction(TypedDict):
    {
      original_action,
      weak_dimension,        # "网页内容资产" / "AI引擎推荐率" / ...
      target_stratum,        # "brand" / "local" / "scenario"
      recommended_content,   # 具体内容描述(带行业/城市/关键词)
      evidence_basis,        # 为什么这样做(从证据 + 维度差距推断)
      acceptance_30d,        # 30 天验收口径(可量化)
      original_priority,     # 原 P0/P1/P2
      ice_score,             # 原 ICE 分
    }

降级:
  - 行业未明 → recommended_content 用模板"行业内常见的 X"(明确标"建议先补 industry")
  - 城市未明 → 跳过本地化锚点
  - 竞品空 → 验收口径不引用竞品
  · 但仍输出结构化字段 · 不退回到通用文案(老板红线 E)
"""
from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

logger = logging.getLogger("GEO-ActionPersonalizer")


class PersonalizedAction(TypedDict, total=False):
    original_action: str
    original_issue: str
    weak_dimension: str
    target_stratum: str   # "brand" / "local" / "scenario"
    recommended_content: str
    evidence_basis: str
    acceptance_30d: str
    original_priority: str
    ice_score: int


# 维度关键词 → 标准化弱项名(对应 5 维 SSOT)
_DIMENSION_KEYWORDS = [
    ("网页内容", "网页内容资产"),
    ("品牌直引", "网页内容资产"),
    ("权威", "权威背书"),
    ("百科", "权威背书"),
    ("媒体", "权威背书"),
    ("结构化", "结构化内容"),
    ("FAQ", "结构化内容"),
    ("问答", "结构化内容"),
    ("AI 引擎", "AI引擎推荐率"),
    ("AI推荐", "AI引擎推荐率"),
    ("场景词", "AI引擎推荐率"),
    ("本地词", "AI引擎推荐率"),
    ("品牌基础", "品牌基础"),
    ("品牌词", "品牌基础"),
]


def _detect_weak_dimension(issue: str, action: str) -> str:
    """从 issue/action 文本反推影响的维度名"""
    text = (issue or "") + " " + (action or "")
    for kw, dim in _DIMENSION_KEYWORDS:
        if kw in text:
            return dim
    return "AI引擎推荐率"  # default · 多数 todos 是 AI 推荐相关


def _detect_target_stratum(issue: str, action: str) -> str:
    """从 issue/action 反推目标关键词层"""
    text = (issue or "") + " " + (action or "")
    if any(k in text for k in ("品牌词", "品牌名", "官网", "百科", "品牌直引")):
        return "brand"
    if any(k in text for k in ("本地", "城市", "区县", "周边", "附近")):
        return "local"
    if any(k in text for k in ("场景", "服务", "解决方案", "流程", "对比", "推荐")):
        return "scenario"
    return "scenario"  # default


def _industry_content_archetypes(industry: str) -> dict[str, list[str]]:
    """行业 → 建议发布内容原型(按层)

    覆盖常见行业 · fallback general 模板 · 老板红线:不能写"完善百科"通用话
    """
    ind = (industry or "").strip()
    if not ind or ind in ("其他", "未知"):
        return {
            "brand": ["品牌主页核心介绍长文(WHO/WHAT/HOW · 1500 字 +)"],
            "local": ["本地服务介绍页(含营业时间 + 价格 + 案例 + 联系方式)"],
            "scenario": ["核心场景白皮书 · 含 5 个 FAQ + 用户原话 + 案例数据"],
        }

    # 装修/家装/家居
    if any(k in ind for k in ("装修", "家装", "家居", "全屋定制")):
        return {
            "brand": [
                "品牌创始人故事长文(资历 · 案例 · 服务承诺)",
                "百度百科词条申请(基础事实型)",
            ],
            "local": [
                "「{city}{district}全包装修案例」长文(含户型 · 价格 · 避坑)",
                "「{city}本地装修推荐 5 强对比」客户视角长文",
            ],
            "scenario": [
                "「{industry}避坑指南」结构化 FAQ(含价格陷阱 · 工期延误 · 增项套路)",
                "「全屋定制 vs 半包 vs 清包」对比长文 + 决策表",
            ],
        }
    # 餐饮/食品
    if any(k in ind for k in ("餐饮", "餐厅", "食品", "外卖", "美食")):
        return {
            "brand": ["品牌菜系故事长文(主厨 · 食材 · 招牌菜)"],
            "local": ["「{city}美食地图」店铺主页 + 用户评价 5+ 平台聚合"],
            "scenario": ["「{industry}必吃菜单」干货长文 + 高清图 + 价格区间"],
        }
    # 美容/医美
    if any(k in ind for k in ("美容", "医美", "整形", "皮肤", "美甲")):
        return {
            "brand": ["品牌资质墙(医师执照 · 设备认证 · 安全承诺)"],
            "local": ["「{city}{district}医美 Top 5 真实评价」客户视角长文"],
            "scenario": ["「项目术后真实日记 + 价格透明清单」(避免广告法风险)"],
        }
    # SaaS/B2B/工厂出海
    if any(k in ind for k in ("SaaS", "CRM", "ERP", "软件", "外贸", "B2B", "工厂", "制造")):
        return {
            "brand": ["品牌方案白皮书(含 ROI 模型 · 客户案例 · 售后承诺)"],
            "local": ["行业生态链合作伙伴页(供应链 / 服务网络)"],
            "scenario": [
                "「{industry}选型对比指南」(竞品对标 · 决策树)",
                "知乎专栏长文 + B 站演示视频(降低决策门槛)",
            ],
        }
    # 教育/培训
    if any(k in ind for k in ("教育", "培训", "考研", "留学", "辅导")):
        return {
            "brand": ["教师团队资质长文 + 学员真实案例"],
            "local": ["「{city}{industry}口碑」客户视角对比长文"],
            "scenario": ["「{industry}选课指南」FAQ + 试听承诺 + 退费政策"],
        }
    # 律所/法律
    if any(k in ind for k in ("律", "法律", "法务")):
        return {
            "brand": ["律师执业经历 · 代表案例 · 胜诉率(在合规口径下)"],
            "local": ["「{city}{industry}咨询常见问题」FAQ 50+"],
            "scenario": ["「{industry}流程详解」长文 + 案例 + 费用区间"],
        }
    # 房地产/物业
    if any(k in ind for k in ("地产", "房地产", "物业", "楼盘")):
        return {
            "brand": ["楼盘价值长文(配套 · 交付承诺 · 物业服务)"],
            "local": ["「{city}{district}片区分析」干货长文(含交通 · 学区 · 商业)"],
            "scenario": ["「购房决策清单」对比 5 个相似楼盘"],
        }
    # 通用 fallback(行业明确但无模板)
    return {
        "brand": [f"{ind}行业品牌权威介绍(资质 · 案例 · 服务承诺)"],
        "local": [f"「{{city}}{ind}本地服务对比」客户视角长文"],
        "scenario": [f"「{ind}避坑/选型/决策指南」结构化 FAQ + 案例数据"],
    }


def _format_content(template: str, brand: dict, industry: str) -> str:
    """填充 {city}/{district}/{industry} 模板"""
    city = brand.get("city") or ""
    cities = brand.get("cities") or ""
    if isinstance(cities, str) and cities and "," in cities:
        # 取第二个作为 district 候选(第一个是城市)
        parts = [p.strip() for p in cities.replace("、", ",").replace(";", ",").split(",") if p.strip()]
        district = parts[1] if len(parts) >= 2 else ""
    elif isinstance(cities, list) and len(cities) >= 2:
        district = cities[1]
    else:
        district = ""

    return (
        template
        .replace("{city}", city or "本地")
        .replace("{district}", district)
        .replace("{industry}", industry or "")
    )


def _evidence_basis(weak_dim: str, target_stratum: str, context: dict) -> str:
    """为什么这样做 · 来自证据 + 维度差距 + 关键词层数据"""
    strata: list[dict] = context.get("strata") or []
    layer_data = next(
        (s for s in strata if s.get("key") == target_stratum), None
    )

    parts: list[str] = []
    parts.append(f"对应弱项维度:{weak_dim}")

    if layer_data:
        if layer_data.get("data_sufficient"):
            rate_pct = layer_data.get("rate_pct", 0)
            parts.append(
                f"{layer_data['label']}当前推荐率 {rate_pct}%"
                f"({layer_data['detected']}/{layer_data['total']} 命中)"
            )
        else:
            parts.append(
                f"{layer_data['label']}本期测试样本仅 {layer_data['total']} 次 · 数据不足 · "
                f"补内容后建议先扩 ≥ 5 个该层关键词监测"
            )

    evidence_total = context.get("evidence_total") or 0
    if evidence_total < 5:
        parts.append(f"AI 实测证据仅 {evidence_total} 条 · 该结论参考价值降低 · 需补监测")

    competitors: list = context.get("competitors") or []
    if competitors and target_stratum in ("local", "scenario"):
        comp_names = [
            c.get("name") for c in competitors[:3]
            if isinstance(c, dict) and c.get("name")
        ]
        if comp_names:
            parts.append(f"建档竞品:{'、'.join(comp_names)} · 内容应做差异化对比")

    return " · ".join(parts)


def _acceptance_30d(weak_dim: str, target_stratum: str, context: dict) -> str:
    """30 天验收口径(可量化)"""
    strata: list[dict] = context.get("strata") or []
    layer_data = next(
        (s for s in strata if s.get("key") == target_stratum), None
    )

    layer_label = layer_data.get("label") if layer_data else ""

    if weak_dim == "网页内容资产":
        return (
            f"30 天后 brand_direct_count(品牌直引数)从当前 0 提至 ≥ 3 条 · "
            f"对应"f"{layer_label or '该层'}"f"AI 推荐率提 ≥ 5%"
        )
    if weak_dim == "权威背书":
        return (
            f"30 天内拿下 ≥ 1 个权威媒体引用(36kr/虎嗅/百度百科/政企域名)· "
            f"authority_score 从当前提 ≥ 4 分"
        )
    if weak_dim == "结构化内容":
        return (
            f"30 天发布 ≥ 5 条 FAQ + 1 篇白皮书摘要 · "
            f"structured_content_score 从当前提 ≥ 3 分"
        )
    if weak_dim == "AI引擎推荐率" and layer_data and layer_data.get("data_sufficient"):
        cur_pct = layer_data.get("rate_pct", 0)
        target_pct = min(100, cur_pct + 15)
        return (
            f"30 天后{layer_label}推荐率从 {cur_pct}% 提至 ≥ {target_pct}% · "
            f"以新发内容覆盖该层 ≥ 3 个关键词为达成标志"
        )
    return f"30 天后 {weak_dim} 维度从当前提 ≥ 5 分(以重测一次诊断报告对比为准)"


def personalize_action(
    todo: dict[str, Any],
    *,
    brand: dict[str, Any],
    industry: str,
    context: dict[str, Any],
) -> PersonalizedAction:
    """单条 todo → PersonalizedAction(老板硬要求 E)"""
    issue = todo.get("issue") or ""
    action = todo.get("action") or ""
    weak_dim = _detect_weak_dimension(issue, action)
    target_stratum = _detect_target_stratum(issue, action)

    archetypes = _industry_content_archetypes(industry)
    candidate_templates = archetypes.get(target_stratum, archetypes["scenario"])
    chosen = candidate_templates[0]
    recommended = _format_content(chosen, brand, industry)

    evidence_basis = _evidence_basis(weak_dim, target_stratum, context)
    acceptance = _acceptance_30d(weak_dim, target_stratum, context)

    return {
        "original_action": action,
        "original_issue": issue,
        "weak_dimension": weak_dim,
        "target_stratum": target_stratum,
        "recommended_content": recommended,
        "evidence_basis": evidence_basis,
        "acceptance_30d": acceptance,
        "original_priority": todo.get("priority", "P1"),
        "ice_score": int(todo.get("ice", 0) or 0),
    }


def personalize_actions(
    todos: list[dict[str, Any]],
    *,
    brand: dict[str, Any],
    industry: str,
    strata: list[dict],
    evidence_total: int = 0,
    competitors: list[dict] | None = None,
) -> list[PersonalizedAction]:
    """批量个性化 · 一次诊断报告调用一次"""
    context = {
        "brand": brand,
        "industry": industry,
        "strata": strata or [],
        "evidence_total": int(evidence_total or 0),
        "competitors": competitors or [],
    }
    out: list[PersonalizedAction] = []
    for t in todos or []:
        try:
            out.append(personalize_action(t, brand=brand, industry=industry, context=context))
        except Exception as e:
            logger.warning(f"[action_personalizer] 跳过 todo (异常 {e}): {t}")
    return out


def render_personalized_actions_md(actions: list[PersonalizedAction]) -> str:
    """渲染为 markdown 段(供 Module 6 末尾追加 / 替换 todos 表)"""
    if not actions:
        return ""
    lines = ["", "### 行动建议(已个性化 · 含弱项维度 / 目标关键词层 / 推荐内容 / 证据基础 / 30 天验收)", ""]
    for i, a in enumerate(actions, start=1):
        lines.append(f"**{i}. [{a.get('original_priority', 'P?')}] {a.get('original_issue', '')}**")
        lines.append("")
        lines.append(f"- **薄弱维度:** {a.get('weak_dimension', '?')}")
        lines.append(f"- **目标关键词层:** {a.get('target_stratum', '?')}")
        lines.append(f"- **推荐发布内容:** {a.get('recommended_content', '')}")
        lines.append(f"- **证据基础:** {a.get('evidence_basis', '')}")
        lines.append(f"- **30 天验收口径:** {a.get('acceptance_30d', '')}")
        lines.append("")
    return "\n".join(lines)
