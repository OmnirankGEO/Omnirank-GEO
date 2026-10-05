"""
report_writer_v2 — M2 报告 2.0 · 8 模块装配式 writer

CTO-15.9 2026-04-25 · PRD M2 §4 核心交付

定位(feature flag `report_v2_enabled` · 默认 false · 5 金标准先跑):
- 不替换 ai_write_report endpoint · 内部切换走 v1(现有) vs v2(本模块)
- v2 用 8 模块 Assembly 组装 markdown · 每模块独立数据源 + 独立 prompt
- 复用 scoring_levels.py SSOT(元指令 14)+ report_evidence.py(P0.2)
- M2 Week 6-10 Codex 0424 方案落地

8 模块(PRD M2 §Epic):
  1. 封面结论 · 一眼看懂 · scoring_levels SSOT + LLM 50-80 字
  2. 评分雷达图 · 8 维度(resolved / brand_fit / keyword / engine / content / depth / update / authority)
  3. AI 实测证据 · Evidence A/B/C 3 级 · 豆包 + 通义千问原话
  4. 竞品分析 · search_citations 聚合对比
  5. 机会估算 · 基于行业 median + 当前覆盖计算缺口
  6. 行动优先级 10 项 todos · 按影响度排序
  7. 30 天计划 · 分 3 周 · 每周 3-5 项具体动作
  8. 方案承接 · 报价 CTA + 下一步引导(呼应 P0.5c)

实施策略:
- 每模块 1 个 build_* 函数 · 返回 dict(data + rendered_md)
- assemble_report_v2 主入口 · 串联 8 模块 + 总 markdown
- feature_flag 读 settings.report_v2_enabled · 代理级 whitelist
- 缺数据时 · 写"本期数据不足 · 不做趋势结论"(不粉饰 · 老板铁律)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("GEO-ReportWriterV2")

# v3.6 白标 · 平台默认品牌从 SSOT 导入(本文件不硬编码平台名字面量)
from services.public_whitelabel import _PLATFORM_BRAND as _RW_PLATFORM_BRAND


def _rw_brand_company(branding: Optional[dict]) -> str:
    """branding(resolve_branding_context surface=customer 的 brand dict)取公司名;
    None 或缺省 → 平台默认。决策 C:external_only 客户面用 company_name 兜底,不回退平台。"""
    if branding:
        c = (branding.get("company_name") or "").strip()
        if c:
            return c
    return _RW_PLATFORM_BRAND["company_name"]


# ============================================================================
# Feature flag 读取
# ============================================================================

def is_report_v2_enabled(brand_id: Optional[int] = None, user_id: Optional[int] = None) -> bool:
    """M2 feature flag · 3 级控制:

    1. 全局关(settings.report_v2_enabled=false)· M2 默认
    2. 全局开 · 所有 brand 走 v2
    3. 白名单(settings.report_v2_whitelist: {brand_ids: [], user_ids: []})· 灰度代理/品牌

    生产默认:全局关 · 5 金标准用白名单灰度 · Week 10 末决定全量或回滚

    Args:
        brand_id: 当前报告所属品牌
        user_id: 触发生成的代理

    Returns:
        True = 走 v2 · False = 走 v1(ai_write_report 原 endpoint 逻辑)
    """
    try:
        from config.settings_manager import load_settings
        settings = load_settings()
    except Exception:
        return False  # settings 加载失败降级 v1

    # 全局开关
    flag_enabled = getattr(settings, "report_v2_enabled", None)
    if flag_enabled is True:
        return True
    if flag_enabled is False:
        # 再查白名单
        whitelist = getattr(settings, "report_v2_whitelist", None) or {}
        if not isinstance(whitelist, dict):
            return False
        if brand_id is not None and brand_id in (whitelist.get("brand_ids") or []):
            return True
        if user_id is not None and user_id in (whitelist.get("user_ids") or []):
            return True
        return False

    # None / 未配置 → 默认关(保守)
    return False


# ============================================================================
# CTO-15.16 M1c+M2 桥 · brief 消费 helper
# ============================================================================

def _enrich_with_brief(report_data: dict, brand_id: int | None) -> dict:
    """把 client_profiles.industry_brief(只在 confirmed=TRUE 时有效)注入 report_data

    M1c audit 发现:report_writer_v2 0 处消费 brief · 即使代理 brand 完整度 95
    报告 8 模块仍看不到 service_scope / 本地竞品 / 差异化定位 → "壳级专业感"

    本 helper 只读 · 不动主流程数据 · brief 缺/未确认时安静返原 report_data
    """
    if not brand_id or not isinstance(report_data, dict):
        return report_data
    try:
        from db.profile_db import get_effective_brief_by_brand
        brief = get_effective_brief_by_brand(brand_id) or {}
    except Exception as _e:
        logger.warning(f"[report_v2] brief 拉取失败(降级 0 brief): {_e}")
        brief = {}
    if not brief:
        return report_data

    # 拉 profile.service_scope / local_competitors(brief 之外的 E 组字段)
    service_scope = None
    local_competitors_profile: list = []
    try:
        from db.connection import get_connection as _gc
        conn = _gc()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT service_scope, local_competitors
                FROM client_profiles
                WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
                ORDER BY updated_at DESC NULLS LAST LIMIT 1
                """,
                (brand_id,),
            )
            prow = cur.fetchone()
        finally:
            conn.close()
        if prow:
            pd = dict(prow)
            service_scope = pd.get("service_scope") or None
            lc_raw = pd.get("local_competitors")
            if isinstance(lc_raw, list):
                local_competitors_profile = lc_raw
            elif isinstance(lc_raw, str) and lc_raw.strip():
                import json as _json
                try:
                    parsed_lc = _json.loads(lc_raw)
                    if isinstance(parsed_lc, list):
                        local_competitors_profile = parsed_lc
                except (ValueError, TypeError):
                    pass
    except Exception as _e2:
        logger.warning(f"[report_v2] profile 字段拉取失败: {_e2}")

    enriched = dict(report_data)
    enriched["brief"] = brief
    if service_scope:
        enriched["service_scope"] = service_scope
    if local_competitors_profile:
        enriched["local_competitors"] = local_competitors_profile
    return enriched


# ============================================================================
# WJ-37 · brief 字段消费 helper（保守接入 · 只读 report_data["brief"]，不新增 DB 查询）
#
# 背景:_enrich_with_brief 已把 confirmed=TRUE 的 industry_brief 注入 report_data["brief"],
#       但 CTO-15.16 只消费了 my_differentiation(M1)/ local_competitors(M4)/ service_scope(M5),
#       my_audience / target_users / content_pain_points / acquisition_paths / top_cases
#       等代理为客户填的关键资料仍"零消费",报告像空壳。
#
# 本 helper 把这些已有字段格式化成短文本,作为补充上下文织入 Module 1/6/8 的成文,
# 让结论 / 行动建议 / 方案承接体现客户实际填的资料。
#   · 不调 LLM(本文件全程无 LLM,模块都是确定性 markdown 模板)
#   · 不改 framework / 不改评分 / 不改数据结构 / 不新增 DB 查询
#   · brief 缺/未确认时 _enrich_with_brief 已不注入,这里安静返回空
# ============================================================================

def _brief_field_text(brief: dict | None, *keys: str, sep: str = " · ", limit: int = 3, maxlen: int = 120) -> str:
    """从 brief 取首个有值的字段,统一成短文本(同时兼容 str / list[str] / list[dict])。

    Args:
        brief: report_data["brief"](已 confirmed)· None / 非 dict 安全返空
        keys: 按优先级尝试的字段名(取第一个有值的)
        sep: list 值的连接符
        limit: list 值最多取几项
        maxlen: 结果总长上限(防超长污染版面)
    Returns:
        短文本 · 无有效值返回 ""
    """
    if not isinstance(brief, dict):
        return ""
    for k in keys:
        v = brief.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            t = v.strip()
            if t:
                return t[:maxlen]
        elif isinstance(v, (list, tuple)) and v:
            parts: list[str] = []
            for item in v[:limit]:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    # top_cases / acquisition_paths 等可能是 dict · 取常见标题字段
                    label = (
                        item.get("title") or item.get("name") or item.get("case")
                        or item.get("path") or item.get("text") or item.get("desc")
                    )
                    if isinstance(label, str) and label.strip():
                        parts.append(label.strip())
            if parts:
                return sep.join(parts)[:maxlen]
    return ""


def _brief_context_lines(brief: dict | None) -> list[str]:
    """把客户填的关键 brief 资料整理成"结合你填的资料"展示行(markdown bullet)。

    只输出真正有值的行 · 全空时返回 [](调用方据此决定是否展示整段)。
    字段对应 INDUSTRY_BRIEF_KNOWN_FIELDS(db/profile_db.py)。
    """
    if not isinstance(brief, dict) or not brief:
        return []
    lines: list[str] = []
    audience = _brief_field_text(brief, "my_audience", "target_users")
    if audience:
        lines.append(f"- **目标客户**:{audience}")
    diff = _brief_field_text(brief, "my_differentiation", "differentiation_hints")
    if diff:
        lines.append(f"- **差异化优势**:{diff}")
    pains = _brief_field_text(brief, "content_pain_points")
    if pains:
        lines.append(f"- **客户常见疑问 / 痛点**:{pains}")
    paths = _brief_field_text(brief, "acquisition_paths")
    if paths:
        lines.append(f"- **现有获客路径**:{paths}")
    cases = _brief_field_text(brief, "top_cases")
    if cases:
        lines.append(f"- **可引用案例**:{cases}")
    strategy = _brief_field_text(brief, "content_strategy")
    if strategy:
        lines.append(f"- **内容方向**:{strategy}")
    return lines


# ============================================================================
# Module 0 · 本次判断依据(CTO-B 2026-04-26 W2 · 老板红线·决策点 5)
# ============================================================================

def build_module_0_completeness(report_data: dict) -> dict[str, Any]:
    """Module 0 · 本次判断依据

    告诉客户这份报告是基于哪些现有材料做判断,以及哪些判断会更保守。
    数据源:report_data['completeness'](由 services/diagnosis_report_v2 注入 · W1 SSOT 计算)。
    缺失则降级展示但仍出现 banner(老板红线:不假装数据完整)。
    """
    completeness = report_data.get("completeness") or {}
    score = completeness.get("score")
    if score is None:
        # 判断依据未计算 · 仍展示但提示"未估算"(透明度优先)
        md = (
            "## 本次判断依据\n\n"
            "_本次判断依据未计算 · 报告会按现有数据展示,证据不足处会明确标注。_\n\n"
            "---\n"
        )
        return {"module": 0, "completeness": None, "rendered_md": md}

    level = completeness.get("level", "")
    groups = completeness.get("groups") or []
    impact_notes = completeness.get("impact_notes") or []
    missing_summary = completeness.get("missing_summary", "")

    md_lines = ["## 本次判断依据", ""]
    md_lines.append(f"### {score}/100 · {level}")
    md_lines.append("")
    md_lines.append(f"_本报告基于现有资料做判断:{missing_summary}_")
    md_lines.append("")

    md_lines.append("说明:资料只是诊断的听诊器,不是目标。报告会用已有资料判断问题出在哪里,不会把“填满表格”当成优化目标。")
    md_lines.append("")

    # 4 大组细分
    md_lines.append("| 判断依据 | 充分度 | 状态 | 影响报告哪一部分 |")
    md_lines.append("| --- | --- | --- | --- |")
    impact_by_group = {
        "identity": "品牌认知层、本地获客层问题设计",
        "business": "决策获客层问题是否贴近真实买家",
        "marketing": "为什么 AI 应该推荐你",
        "brief": "竞品差距、机会估算和行动计划",
        "insight": "权威证据和客户说服力",
    }
    for g in groups:
        group_key = str(g.get("key") or "").lower()
        percent_value = g.get("percent")
        percent_label = "满分" if percent_value == 100 else f"{percent_value}%"
        status_label = "可用于判断"
        if g["missing_fields"]:
            status_label = "判断会保守"
        md_lines.append(
            f"| {g['label']} | {g['score']}/{g['weight']} | {status_label}({percent_label}) | "
            f"{impact_by_group.get(group_key, '报告置信度和行动建议具体度')} |"
        )
    md_lines.append("")

    if impact_notes:
        md_lines.append("**对本次判断的影响:**")
        md_lines.append("")
        for note in impact_notes:
            md_lines.append(f"- {note}")
        md_lines.append("")

    md_lines.append("---")
    return {
        "module": 0,
        "completeness": completeness,
        "rendered_md": "\n".join(md_lines),
    }


# ============================================================================
# Module 1 · 封面结论
# ============================================================================

def build_module_1_cover(report_data: dict) -> dict[str, Any]:
    """Module 1 · 一句话定性(CTO-G 2026-04-27 简化)

    旧封面跟 Module 2 漏斗 hero 冗余 · 改为单句业务洞察
    数据源:
      - report_data.funnel_score(漏斗 3 层 · _shape_report_data 注入)
      - report_data.brief.my_differentiation(M1c · 可选附录)

    Returns: {module, level, level_meta, insight, conclusion_text, rendered_md}
      conclusion_text 字段保留供 _render_executive_summary 复用
    """
    funnel = report_data.get("funnel_score") or {}
    layers = funnel.get("layers", [])
    level = funnel.get("level", "隐形级")
    level_meta = funnel.get("level_meta") or {}

    brand_layer = next((l for l in layers if l.get("key") == "brand"), None)
    local_layer = next((l for l in layers if l.get("key") == "local"), None)
    scenario_layer = next((l for l in layers if l.get("key") == "scenario"), None)

    insight = "你的 GEO 现状如下。"
    if brand_layer and local_layer and scenario_layer:
        b = brand_layer.get("rate_pct", 0)
        l = local_layer.get("rate_pct", 0)
        s = scenario_layer.get("rate_pct", 0)
        if b >= 70 and l < 30 and s < 30:
            insight = (
                "客户搜你品牌名时 AI 能提你 · 但客户搜「行业+地区」「行业+方案」时 AI 不推荐你 · "
                "你的新客获取链路在 AI 这一侧基本是断的。"
            )
        elif b < 30:
            insight = (
                "AI 完全不认识你的品牌 · 即使客户主动搜你品牌名也找不到你 · GEO 链路全断。"
            )
        elif l >= 50 and s < 30:
            insight = (
                "AI 在客户搜本地行业关键词时能提到你 · 但高客单决策场景词上 AI 不引用你 · "
                "决策客户拿不到。"
            )
        elif s >= 70 and b >= 70 and l >= 70:
            insight = (
                "AI 在你品牌词、本地词和高客单场景词上都能稳定推荐你 · 守城为主 · "
                "关注竞品反超信号。"
            )
        elif l >= 50 and s >= 50:
            insight = (
                "AI 在多数行业问题上都能给到你 · 持续优化拉高 · 重点拉高高客单场景词命中率。"
            )

    # M1c · 差异化定位附录(只展示已 confirmed 的 brief)
    brief = report_data.get("brief") or {}
    diff_txt = brief.get("my_differentiation") if isinstance(brief, dict) else None
    diff_block = ""
    if isinstance(diff_txt, str) and diff_txt.strip():
        diff_block = f"\n**差异化定位**:{diff_txt.strip()}\n"
    elif isinstance(diff_txt, list) and diff_txt:
        diff_block = "\n**差异化定位**:" + " · ".join(str(x) for x in diff_txt[:3]) + "\n"

    # WJ-37 · 结论里补一行"目标客户"(代理填的 brief 资料 · 让定性贴客户实际)
    audience_txt = _brief_field_text(brief, "my_audience", "target_users")
    if audience_txt:
        diff_block += f"**目标客户**:{audience_txt}\n"

    def _layer_answer(layer: dict | None, good: str, mid: str, low: str) -> str:
        if not layer:
            return "样本不够,本层只做方向判断。"
        rate = float(layer.get("rate") or 0)
        detected = int(layer.get("detected") or 0)
        total = int(layer.get("total") or 0)
        prefix = f"{detected}/{total} 命中" if total else "无样本"
        if rate >= 0.7:
            return f"{good}。{prefix}"
        if rate >= 0.3:
            return f"{mid}。{prefix}"
        return f"{low}。{prefix}"

    brand_answer = _layer_answer(
        brand_layer,
        "认识",
        "部分认识",
        "不稳定",
    )
    local_answer = _layer_answer(
        local_layer,
        "稳定推荐",
        "不稳定",
        "多数不会推荐",
    )
    scenario_answer = _layer_answer(
        scenario_layer,
        "能覆盖高转化问题",
        "覆盖不足",
        "基本没有覆盖",
    )

    md = (
        "## 1 分钟结论\n\n"
        f"> **{insight}**\n"
        f"{diff_block}\n"
        "\n"
        "### 客户最该先看 4 件事\n\n"
        "| 问题 | 本次诊断答案 | 对生意的影响 |\n"
        "| --- | --- | --- |\n"
        f"| AI 认识我吗? | {brand_answer} | 老客户回头搜你时能不能找到你 |\n"
        f"| 新客户会被 AI 推荐给我吗? | {local_answer} | 客户搜“哪家好 / 推荐 / 靠谱”时会不会流向竞品 |\n"
        f"| 高转化问题里有没有我? | {scenario_answer} | 客户搜“怎么做 / 对比 / 避坑”时能不能进入候选 |\n"
        "| 优先核对什么? | 原始回答、可引用来源与复测口径 | 把结论落到证据,不要把单次分数当承诺 |\n"
        "\n"
        "---\n"
    )
    return {
        "module": 1,
        "level": level,
        "level_meta": level_meta,
        "insight": insight,
        "conclusion_text": insight,  # back-compat for _render_executive_summary
        "differentiation_text": diff_txt if isinstance(diff_txt, (str, list)) else None,
        "rendered_md": md,
    }


def _rw_layer_by_key(layers: list[dict], key: str) -> dict | None:
    """从漏斗 layers 按 key 取层;没有 key 时按常见中文名兜底。"""
    for layer in layers or []:
        if not isinstance(layer, dict):
            continue
        if layer.get("key") == key:
            return layer
    label_tokens = {
        "brand": ("品牌", "认知"),
        "local": ("决策", "获客", "本地", "行业"),
        "scenario": ("场景", "转化", "高意向"),
    }.get(key, ())
    for layer in layers or []:
        if not isinstance(layer, dict):
            continue
        label = str(layer.get("label") or "")
        if any(t in label for t in label_tokens):
            return layer
    return None


def _rw_layer_rate_pct(layer: dict | None) -> float:
    if not layer:
        return 0.0
    if layer.get("rate_pct") is not None:
        try:
            return float(layer.get("rate_pct") or 0)
        except (TypeError, ValueError):
            return 0.0
    if layer.get("rate") is not None:
        try:
            return float(layer.get("rate") or 0) * 100
        except (TypeError, ValueError):
            return 0.0
    total = int(layer.get("total") or 0)
    detected = int(layer.get("detected") or 0)
    return round(detected / total * 100, 1) if total > 0 else 0.0


def _rw_layer_hit_text(layer: dict | None) -> str:
    """层命中表述。

    [P0-5 · 2026-07-26] 优先用评分 SSOT 给的合并表述 ``sample_note``，
    避免报告里出现"100% 命中"和"样本不足"并排（生产实证:品牌层 4/4 命中 +
    total=4<5 → 满分与不可信并列，客户读成"你们自己都说不可信"）。
    """
    if not layer:
        return "本层样本不足"
    note = layer.get("sample_note")
    if isinstance(note, str) and note.strip():
        prefix = "初步达标 · 待扩测 · " if layer.get("provisional") else ""
        return prefix + note.strip()
    detected = int(layer.get("detected") or 0)
    total = int(layer.get("total") or 0)
    if total <= 0:
        return "无有效样本"
    return f"{detected}/{total} 命中"


def build_module_1_interpretation(report_data: dict, modules: dict[str, Any] | None = None) -> dict[str, Any]:
    """Module 1_interpretation · 客户报告解读层。

    只把现有漏斗和证据翻译成阅读口径,不生成执行计划、预算建议或固定周期承诺。
    """
    funnel = report_data.get("funnel_score") or {}
    layers = funnel.get("layers") or []
    brand_layer = _rw_layer_by_key(layers, "brand")
    local_layer = _rw_layer_by_key(layers, "local")
    scenario_layer = _rw_layer_by_key(layers, "scenario")

    brand_rate = _rw_layer_rate_pct(brand_layer)
    local_rate = _rw_layer_rate_pct(local_layer)
    scenario_rate = _rw_layer_rate_pct(scenario_layer)

    headline = "这份报告的重点不是单看分数,而是看 AI 在哪一层替品牌说话。"
    business_translation = (
        "建议先看三层漏斗是否断档:品牌认知层回答“AI 是否认识你”,"
        "决策获客层回答“新客户问哪家好时是否会出现你”,"
        "场景转化层回答“客户进入具体需求和对比时是否有你的证据”。"
    )

    # [P1-10 · 2026-07-26] 0 分（或极低分）不得只呈现一串 0 和红点。
    #   生产实证:07 月以来 29 个诊断里有 3 个 0 分。0 分的业务含义是
    #   "现在完全没被 AI 收录" = 起点基线，不是"这家公司没救了"。
    #   同时:如果本次被判为**疑似品牌识别失败**（P0-1 ②），那连"未被收录"
    #   都不能说 —— 那是我们没认出来，必须先改名重测。
    _ai_data = (report_data.get("diagnosis_data") or {}).get("ai_visibility_data") or {}
    _suspicion = _ai_data.get("identity_suspicion") if isinstance(_ai_data, dict) else None
    _identity_suspected = bool(isinstance(_suspicion, dict) and _suspicion.get("suspected"))
    _observed_layers = [
        layer for layer in (brand_layer, local_layer, scenario_layer)
        if layer and int(layer.get("total") or 0) > 0
    ]
    _all_zero = bool(_observed_layers) and all(
        int(layer.get("detected") or 0) == 0 for layer in _observed_layers
    )

    if _identity_suspected:
        headline = "本次没有认出这个品牌，先别把它当成「0 分」。"
        business_translation = (
            "所有问题都没有识别到该品牌，但同时命中了识别异常信号"
            f"（{'、'.join(_suspicion.get('signals') or []) or '识别与原文不一致'}）。"
            "这说明是品牌名没对上，不是品牌真的没被 AI 提到。"
            + (
                f"建议把品牌名改为「{_suspicion.get('suggested_brand_name')}」后重测。"
                if _suspicion.get("suggested_brand_name")
                else "建议核对并填写规范的公司/品牌全称后重测。"
            )
            + "本次结果先按待复核处理，不作为效果结论。"
        )
    elif _all_zero:
        headline = "这是起点基线：目前 AI 还没有把这个品牌收录进答案。"
        business_translation = (
            "本次实测里 AI 一次都没有主动说出这个品牌 —— 这不是评价，而是**起点读数**。"
            "行业里绝大多数品牌一开始都是这个位置：AI 只会引用它检索得到的公开内容，"
            "没有可引用的内容自然点不到名。"
            "首月的具体动作是：①把品牌事实（全称、主营、服务地区、真实案例）在官网/公开页面写清并保持一致；"
            "②围绕本次 0 命中的问题，在能被检索到的权威站点发布可核验的内容；"
            "③把同一批问题按周复测，看命中从 0 变成 1 的那一刻是在哪个问题上先发生。"
            "第一次出现命中通常比分数变化更早、更能说明方向对了。"
        )
    elif brand_rate >= 70 and local_rate < 30 and scenario_rate < 30:
        headline = "不是没有基础,而是新客决策入口还没有被 AI 稳定承接。"
        business_translation = (
            "客户已经知道品牌时,AI 有机会识别你;但客户还不认识你、正在问“哪家靠谱”"
            "或具体需求时,AI 还没有稳定把你纳入候选。"
        )
    elif brand_rate < 30:
        headline = "AI 对品牌本身的识别还不稳定,后续判断需要更保守。"
        business_translation = (
            "品牌认知层偏弱意味着客户主动搜索品牌时,AI 也可能无法稳定说清楚你是谁。"
            "在这个基础上,行业推荐和场景问题里的表现通常也会受影响。"
        )
    elif local_rate >= 50 and scenario_rate < 30:
        headline = "品牌已经能进入部分候选,但高意向问题里的证据说服力不足。"
        business_translation = (
            "AI 可能已经在部分行业推荐问题里提到品牌,但客户进入对比、避坑、方案选择"
            "等更接近决策的问题时,AI 还缺少可引用的公开证据。"
        )
    elif brand_rate >= 70 and local_rate >= 70 and scenario_rate >= 70:
        headline = "当前基础较好,解读重点应放在证据来源稳定性和异常波动。"
        business_translation = (
            "三层漏斗都有较好覆盖时,不应硬找低分问题;更应核对 AI 引用的来源是否可靠、"
            "竞品是否有反超迹象,以及不同引擎之间是否存在异常差异。"
        )

    evidence_total = 0
    if isinstance(modules, dict):
        ev = modules.get("3") or {}
        raw_count = ev.get("evidence_total") or ev.get("evidence_count")
        if isinstance(raw_count, dict):
            evidence_total = sum(int(v or 0) for v in raw_count.values())
        else:
            try:
                evidence_total = int(raw_count or 0)
            except (TypeError, ValueError):
                evidence_total = 0

    insufficient_layers: list[str] = []
    for layer in (brand_layer, local_layer, scenario_layer):
        if not layer:
            continue
        total = int(layer.get("total") or 0)
        if total > 0 and total < 5:
            insufficient_layers.append(str(layer.get("label") or "某一层"))
        elif layer.get("data_sufficient") is False:
            insufficient_layers.append(str(layer.get("label") or "某一层"))

    caveat_parts: list[str] = []
    if evidence_total and evidence_total < 5:
        caveat_parts.append(f"本次有效证据约 {evidence_total} 条,部分结论需要保守理解。")
    # [P0-5] 样本偏少的层已经在上表用"X/Y 命中（样本较少，建议扩测…）"合并说清，
    #   这里只写"扩测后可确认"的方向，不再把"数据不足"当独立结论重复一遍。
    if insufficient_layers:
        caveat_parts.append(
            "、".join(insufficient_layers[:3]) + "的样本量还不够,扩测到 8 题以上可以确认。"
        )
    if not caveat_parts:
        caveat_parts.append("本报告代表本次采样窗口的客观结果,AI 回答会随模型和实时检索变化。")
    data_caveat = "".join(caveat_parts)

    reading_points = [
        "先看三层漏斗:品牌认知、决策获客、场景转化分别回答不同业务问题。",
        "再看原始 AI 回答:关键不只是得分,而是 AI 有没有自然提到品牌、竞品和来源。",
        "最后看数据边界:证据不足处只做保守判断,不把趋势当成确定结论。",
    ]

    md_lines = [
        "## 这份报告应该怎么读",
        "",
        f"**核心解读:**{headline}",
        "",
        "### 阅读顺序",
        "",
    ]
    for point in reading_points:
        md_lines.append(f"- {point}")
    md_lines.extend([
        "",
        "### 业务翻译",
        "",
        business_translation,
        "",
        "| 漏斗层 | 本次结果 | 解读重点 |",
        "| --- | --- | --- |",
        f"| 品牌认知层 | {_rw_layer_hit_text(brand_layer)} | AI 是否稳定认识品牌 |",
        f"| 决策获客层 | {_rw_layer_hit_text(local_layer)} | 新客户问“哪家好”时是否进入候选 |",
        f"| 场景转化层 | {_rw_layer_hit_text(scenario_layer)} | 高意向问题里是否有可引用证据 |",
        "",
        f"**数据边界:**{data_caveat}",
        "",
        "---",
    ])

    return {
        "module": "1_interpretation",
        "headline": headline,
        "reading_points": reading_points,
        "business_translation": business_translation,
        "data_caveat": data_caveat,
        "rendered_md": "\n".join(md_lines),
    }


# ============================================================================
# Module 2 · 评分总览 · 5 维度 100 制 + 3 层关键词推荐率
# CTO-B 2026-04-26 W2 升级 · 复用 services/report_metrics SSOT
# ============================================================================

def build_module_2_radar(report_data: dict) -> dict[str, Any]:
    """Module 2 · 漏斗 3 层加权评分(CTO-G 2026-04-27 重写)

    替换旧 5 维度评分(因果重叠 + 掩盖老客新客差距)
    新模型:漏斗 3 层加权 · 6 档等级名 · 进度条视觉

    数据源:
      - report_data['funnel_score']: 优先用 _shape_report_data 注入
      - 否则从 report_data['diagnosis_data']['ai_visibility_data']['dimension_stats'] 现算

    向后兼容:
      返回值仍含 summary/dimensions/keyword_strata 字段(给 _render_executive_summary 用)
    """
    # [P1-12 fix 2026-05-23] SSOT 统一入口 · scoring_levels re-export funnel_score
    # 元指令 14:scoring_levels.py 单点 · 顶部/正文/前端/PDF 等级映射统一
    from tools.scoring.scoring_levels import (
        calculate_funnel_score, render_funnel_progress_bar,
    )
    from services.report_metrics import (
        get_dimension_breakdown, get_total_score_and_level, get_keyword_strata_rates,
    )

    diagnosis_data = report_data.get("diagnosis_data") or {}

    # 优先用 _shape_report_data 注入的 funnel_score · 不在则现算
    funnel = report_data.get("funnel_score")
    if not funnel:
        ai_data = diagnosis_data.get("ai_visibility_data") or {}
        dim_stats = ai_data.get("dimension_stats") or {}
        brand_st = dim_stats.get("brand_awareness") or {}
        local_st = dim_stats.get("regional_industry") or {}
        scenario_st = dim_stats.get("super_tier1") or {}
        funnel = calculate_funnel_score(
            brand_detected=int(brand_st.get("detected", 0) or 0),
            brand_total=int(brand_st.get("total", 0) or 0),
            local_detected=int(local_st.get("detected", 0) or 0),
            local_total=int(local_st.get("total", 0) or 0),
            scenario_detected=int(scenario_st.get("detected", 0) or 0),
            scenario_total=int(scenario_st.get("total", 0) or 0),
        )

    total = funnel["total_score"]
    level = funnel["level"]
    meta = funnel["level_meta"]
    layers = funnel["layers"]

    # 6 档等级 emoji 映射
    level_emoji = {
        "主导级": "🟢", "健康级": "🟢",
        "成长级": "🟡",
        "边缘级": "🟠",
        "危急级": "🔴", "隐形级": "🔴",
    }
    badge_emoji = level_emoji.get(level, "🔴")

    # ----- 渲染 markdown -----
    md_lines: list[str] = []

    # GEO 总分大字框
    md_lines.append("## GEO 总分")
    md_lines.append("")
    md_lines.append("```")
    md_lines.append("                        ╔══════════════╗")
    md_lines.append("                        ║              ║")
    md_lines.append(f"                        ║   {total:>3} / 100   ║")
    md_lines.append("                        ║              ║")
    md_lines.append(f"                        ║   {level} {badge_emoji}   ║")
    md_lines.append("                        ║              ║")
    md_lines.append("                        ╚══════════════╝")
    md_lines.append("```")
    md_lines.append("")
    md_lines.append(f"> **{level}**:{meta.get('business_meaning', '')}")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")

    # 30 秒看懂 · 3 层漏斗
    md_lines.append("## 30 秒看懂")
    md_lines.append("")
    md_lines.append("先翻译两个词:")
    md_lines.append("")
    md_lines.append("- **GEO**:让 AI 在回答用户问题时主动提到你、推荐你。不是传统排名,而是 AI 答案里的存在感。")
    md_lines.append("- **漏斗层**:把客户搜索问题分成三层:先问你是谁,再问哪家好,最后问方案怎么选。越往后越接近成交。")
    md_lines.append("")
    md_lines.append("```")
    md_lines.append("GEO = 让 AI 在回答用户问题时主动推荐你")
    md_lines.append("")
    md_lines.append("3 层漏斗 · 你的真实状态:")
    md_lines.append("")

    layer_examples = {
        "brand": "客户搜你品牌名时",
        "local": "客户搜行业+地区时",
        "scenario": "客户搜方案/对比/避坑时",
    }

    for idx, layer in enumerate(layers, start=1):
        bar = render_funnel_progress_bar(layer["rate"], width=20)
        # 总分规则:total>0 永远展示 rate%(spec 璧山 4/4 brand 仍显示 100%)
        # · total=0 才用占位符(无样本不下结论)
        # · total<5 但 >0 在结尾追加 "·数据不足" 短缀(老板红线:不假装数据完整)
        if layer["total"] <= 0:
            rate_str = "(无样本 · 数据不足)"
        elif not layer["data_sufficient"]:
            rate_str = f"{layer['rate_pct']}% · 数据不足"
        else:
            rate_str = f"{layer['rate_pct']}%"
        emoji = "✅" if layer["rate"] >= 0.7 else "🔴"
        md_lines.append(
            f"层 {idx} · {layer['label']} (权重 {layer['weight']})         {bar}   {rate_str}"
        )
        md_lines.append(
            f"   {layer_examples.get(layer['key'], '')}  "
            f"{layer['detected']}/{layer['total']} 命中"
        )
        # [audit #7 返修] 分母用 effective_weight(重归一真实上限);空样本层显示"无样本"
        #   (原 score/weight 用原始权重 → 重归一后出现 "100 / 20" 类矛盾数)。
        _eff_w = layer.get('effective_weight')
        if int(layer.get('total') or 0) <= 0:
            md_lines.append(f"   无样本(未计分)  {emoji}")
        else:
            _denom = _eff_w if _eff_w else layer['weight']
            md_lines.append(f"   {layer['score']:>5} / {_denom}  {emoji}")
        md_lines.append("")

    md_lines.append("```")
    md_lines.append("")

    # [2026-07-22 板块A A6] PENDING 单列显示:疑似提到待确认 · 不进确定分母 · 确认后自动重算
    _dim_for_pending = (diagnosis_data.get("ai_visibility_data") or {}).get("dimension_stats") or {}
    _pending_total = sum(
        int((_dim_for_pending.get(_key) or {}).get("pending_identity", 0) or 0)
        for _key in ("brand_awareness", "regional_industry", "super_tier1")
    )
    if _pending_total > 0:
        md_lines.append(
            f"⏳ 另有 {_pending_total} 格「疑似提到」待人工确认 · 未计入上方分数 · "
            f"在「AI 实测证据」模块逐格确认后系统会自动重算分数"
        )
        md_lines.append("")

    # 业务翻译
    md_lines.append("**翻译成业务语言**:")
    md_lines.append("")
    for layer in layers:
        if layer["rate"] >= 0.7:
            verdict = "✅ AI 在这层能稳定提到你"
        elif layer["rate"] >= 0.3:
            verdict = "🟡 AI 部分命中 · 多数客户拿不到"
        else:
            verdict = "❌ AI 几乎不提你 · 客户在这层流失"
        md_lines.append(f"- **{layer['business']}** → {verdict}")
    md_lines.append("")
    md_lines.append("---")

    # ----- 向后兼容字段(给 _render_executive_summary 用)-----
    scores = report_data.get("scores") or {}
    try:
        dims = get_dimension_breakdown(scores)
        legacy_summary = get_total_score_and_level(scores)
    except Exception:
        dims = []
        legacy_summary = {
            "total_score": total, "max_score": 100, "level": level,
            "missing_dimensions": [], "has_missing": False,
        }
    try:
        strata = get_keyword_strata_rates(diagnosis_data)
    except Exception:
        strata = []

    # 漏斗版的 summary 覆盖 total_score / level
    funnel_summary = {
        **legacy_summary,
        "total_score": total,
        "max_score": 100,
        "level": level,
    }

    return {
        "module": 2,
        "funnel": funnel,
        "total_score": total,
        "level": level,
        "summary": funnel_summary,        # back-compat for executive summary
        "dimensions": dims,                # back-compat for executive summary
        "keyword_strata": strata,          # back-compat for executive summary
        "rendered_md": "\n".join(md_lines),
    }


# ============================================================================
# Module 3 · AI 实测证据(Evidence A/B/C · 来自 report_evidence P0.2)
# ============================================================================

def build_module_3_evidence(report_data: dict, brand_id: int, days: int = 30) -> dict[str, Any]:
    """Module 3 · AI 实测证据 · 引豆包 + qwen3-max 原话"""
    try:
        from services.report_evidence import extract_report_evidence
    except Exception as e:
        logger.warning(f"[report_v2] extract_report_evidence import 失败: {e}")
        return {
            "module": 3,
            "evidences": [],
            "rendered_md": "## AI 实测证据\n\n_证据抽取模块不可用 · 本期数据不足_\n\n---\n",
        }

    brand_name = report_data.get("brand_name", "")
    try:
        from datetime import datetime, timedelta
        now = datetime.now()
        period_start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        period_end = now.strftime("%Y-%m-%d")
        evidences = extract_report_evidence(
            brand_id=brand_id,
            period_start=period_start,
            period_end=period_end,
            brand_name=brand_name,
            max_items=12,
        )
    except Exception as e:
        logger.warning(f"[report_v2] extract_report_evidence 异常: {e}")
        evidences = []

    # CTO-B 2026-04-26 W3 · Evidence ≥5 硬底(老板硬要求 E)
    # 用户验收第 9 条:每份客户版报告至少展示 5 条真实 AI 原话或明确数据不足
    EVIDENCE_HARD_FLOOR = 5

    # [Phase 4 2026-06-07] 监测证据为空 → 兜底用本次诊断 detail_table 构造展示证据(只诊断没监测的品牌)。
    # 只做展示兜底 · 不进 GEO 主分 / 不改 brand_detected / 不改检出率 / 不碰扣费·评分·监测执行链路。
    # 有监测证据时此块不触发 → 行为 100% 不变。
    if not evidences:
        try:
            from services.report_evidence import extract_diagnosis_evidence
            _diag_table = (
                (report_data.get("diagnosis_data") or {}).get("ai_visibility_data") or {}
            ).get("detail_table") or []
            evidences = extract_diagnosis_evidence(_diag_table, brand_name=brand_name, max_items=12)
            if evidences:
                logger.info(f"[report_v2] module3 监测证据空 · 诊断兜底 {len(evidences)} 条")
        except Exception as e:
            logger.warning(f"[report_v2] module3 诊断证据兜底失败: {e}")
            evidences = evidences or []

    if not evidences:
        return {
            "module": 3,
            "evidences": [],
            "evidence_count": {"A": 0, "B": 0, "C": 0},
            "evidence_shortfall": EVIDENCE_HARD_FLOOR,  # 完全 0 条 · 缺 5 条
            "rendered_md": (
                "## AI 实测证据\n\n"
                f"⚠️ **数据不足 · 本期 0 条有效 AI 实测证据(目标 ≥ {EVIDENCE_HARD_FLOOR} 条)**\n\n"
                "可能原因:\n"
                "- 该品牌还没启用主流 AI 引擎(豆包 / 通义千问)的持续监测 · 在「监测中心」开启后再做诊断\n"
                "- 关键词池过小 · 监测覆盖度不足\n"
                "- 该品牌 AI 提及率极低 · 实测确无可抽证据\n\n"
                "**本期不做结论推断 · 报告余下模块的趋势性判断置信度降低 · "
                "建议后续用同一组问题再跑一轮 4 引擎复测,对比 AI 是否开始提到品牌。**\n\n"
                "---\n"
            ),
        }

    # 按 level 分组 · A 级优先
    group_a = [e for e in evidences if e.get("evidence_level") == "A"]
    group_b = [e for e in evidences if e.get("evidence_level") == "B"]
    group_c = [e for e in evidences if e.get("evidence_level") == "C"]

    total_evidences = len(evidences)
    shortfall = max(0, EVIDENCE_HARD_FLOOR - total_evidences)

    # B.5 · Codex 0424 Evidence 3 级表达对齐
    md = ["## AI 实测证据", ""]

    # W3 · 顶部硬底声明(透明化 · 决策点 5 老板红线)
    if shortfall > 0:
        md.append(
            f"⚠️ **本期共抽到 {total_evidences} 条 · 距 ≥{EVIDENCE_HARD_FLOOR} 条目标缺 {shortfall} 条 · "
            f"以下结论只做方向判断 · 建议后续用同一组问题复测,不要把本期当最终结论**"
        )
    else:
        md.append(f"_本期共抽到 {total_evidences} 条有效证据(目标 ≥{EVIDENCE_HARD_FLOOR} · 已达标)_")
    md.append("")

    def _evidence_layer(keyword: str) -> str:
        kw = (keyword or "").lower()
        if any(x in kw for x in ("是什么", "怎么样", "品牌", "公司", "官网")):
            return "品牌认知"
        if any(x in kw for x in ("哪家", "推荐", "靠谱", "排名", "服务商")):
            return "决策获客"
        return "场景转化"

    def _mentioned_label(snippet: str) -> str:
        if brand_name and brand_name in snippet:
            return "是"
        if brand_name and any(part and part in snippet for part in brand_name.replace("（", " ").replace("）", " ").split()):
            return "部分"
        return "否"

    def _judgement(layer: str, mentioned: str, level: str) -> str:
        if mentioned == "是" and layer == "品牌认知":
            return "品牌基础可用"
        if mentioned in ("是", "部分") and layer == "决策获客":
            return "有机会,但需要稳定进入推荐名单"
        if mentioned in ("是", "部分") and layer == "场景转化":
            return "场景内容开始被绑定,需要继续放大"
        if level == "A":
            return "AI 有可引用素材,但未稳定转成推荐"
        return "这类问题还没有形成可引用素材"

    md.append("以下是本次样本中的代表性证据。证据用于解释分数,不是装饰性引用。")
    md.append("")
    md.append("| 层级 | 用户问题 | AI 是否提到品牌 | 代表原话 | 判断 |")
    md.append("| --- | --- | --- | --- | --- |")
    for e in evidences[:8]:
        keyword = (e.get("keyword") or "未记录问题").strip()
        snippet = (e.get("response_snippet") or "本条实测未返回可展示原话").replace("\n", " ").strip()
        layer = _evidence_layer(keyword)
        mentioned = _mentioned_label(snippet)
        level = e.get("evidence_level") or "B"
        md.append(
            f"| {layer} | `{keyword[:42]}` | {mentioned} | 「{snippet[:120]}」 | "
            f"{_judgement(layer, mentioned, level)} |"
        )
    md.append("")
    md.append("### 证据解释")
    md.append("")
    md.append("- 品牌认知层代表“客户已经知道你时,AI 能不能说清楚你是谁”。")
    md.append("- 决策获客层代表“客户还不知道你、正在选服务商时,AI 会不会把你列入候选”。")
    md.append("- 场景转化层代表“客户搜方案、对比、避坑等高意向问题时,AI 会不会引用你”。")
    md.append("")

    # 新售诊断固定四平台；同一平台的 provider/model 别名必须归为一行。
    # 历史 Kimi 仅在真实证据存在时追加，不能把 Kimi 数据改名为元宝，也不能在新报告里造一行 0 样本。
    platforms = [
        (("豆包", "doubao", "volcengine"), "豆包"),
        (("通义", "千问", "qwen", "dashscope"), "通义千问"),
        (("deepseek", "深度求索"), "DeepSeek"),
        (("yuanbao", "元宝", "hunyuan", "hy3", "tencent_tokenhub"), "元宝"),
    ]
    if any(
        any(token in (e.get("source_label", "") or "").lower() for token in ("kimi", "moonshot"))
        for e in evidences
    ):
        platforms.append((("kimi", "moonshot"), "Kimi"))
    md.append("### 平台证据分布")
    md.append("")
    md.append(
        "_平台范围来自本次真实采集；引用数据以逐条证据为准。"
        "未采集到结构化引用不等于平台调用失败。_"
    )
    md.append("")
    md.append("| 平台 | 命中条数 | A 级 | B 级 | 代表问题 | 未检出关键词数 |")
    md.append("| --- | --- | --- | --- | --- | --- |")
    all_keywords = set(e.get("keyword", "") for e in evidences if e.get("keyword"))
    for aliases, label in platforms:
        plat_evidences = [
            e for e in evidences
            if any(alias in (e.get("source_label", "") or "").lower() for alias in aliases)
        ]
        plat_a = sum(1 for e in plat_evidences if e.get("evidence_level") == "A")
        plat_b = sum(1 for e in plat_evidences if e.get("evidence_level") == "B")
        sample_q = plat_evidences[0].get("keyword", "—") if plat_evidences else "—"
        plat_kw_hit = set(e.get("keyword", "") for e in plat_evidences if e.get("keyword"))
        not_detected = len(all_keywords - plat_kw_hit)
        # CTO-15.16 round2 Task C · sample_q 是用户原问 · backtick 包起来标记为引文 ·
        # banned_words.lint_report_text(strip_quoted=True) 会跳过(避免"最好/绝对"误报)
        md.append(
            f"| {label} | {len(plat_evidences)} | {plat_a} | {plat_b} | `{sample_q[:30]}` | {not_detected} "
            f"{'✅' if len(plat_evidences) > 0 else '⚠️ 0 命中'} |"
        )
    md.append("")

    # 竞品压制 · 从 evidences.search_citations 推断"AI 提了竞品但未提我们"
    brand_name = (report_data.get("brand_name") or "").strip()
    if brand_name:
        suppressed = []
        for e in evidences:
            snippet = (e.get("response_snippet") or "")
            if brand_name not in snippet:
                cits = e.get("search_citations") or []
                if cits:
                    suppressed.append({
                        "keyword": e.get("keyword", ""),
                        "source": e.get("source_label", ""),
                        "competitor_count": len(cits),
                    })
        if suppressed:
            md.append(f"### 竞品压制信号({len(suppressed)} 条 · AI 推荐竞品 · 未提我们)")
            md.append("")
            md.append("| 关键词 | 平台 | 竞品引用数 |")
            md.append("| --- | --- | --- |")
            for s in suppressed[:5]:
                # CTO-15.16 round2 Task C · keyword 是用户原问 · backtick 标记为引文
                md.append(f"| `{s['keyword']}` | {s['source']} | {s['competitor_count']} |")
            md.append("")

    md.append("---")

    return {
        "module": 3,
        "evidences": evidences,
        "evidence_count": {"A": len(group_a), "B": len(group_b), "C": len(group_c)},
        "evidence_total": total_evidences,
        "evidence_shortfall": shortfall,
        "platform_breakdown": {
            label: sum(
                1 for e in evidences
                if any(alias in (e.get("source_label", "") or "").lower() for alias in aliases)
            )
            for aliases, label in platforms
        },
        "rendered_md": "\n".join(md),
    }


def build_module_3_raw_ai_appendix(report_data: dict) -> dict[str, Any]:
    """客户版附录 · 展示全部测试词和 4 引擎原始回答(默认收起)

    数据源:diagnosis_workflow 写入 raw_data.ai_visibility.detail_table。
    新采集链路会保存每个引擎较完整回答;老报告如果历史上只保存了截断片段,这里只能展示当时保存内容。
    """
    diagnosis_data = report_data.get("diagnosis_data") or {}
    ai_data = diagnosis_data.get("ai_visibility_data") or {}
    detail_table = ai_data.get("detail_table") or []
    if not isinstance(detail_table, list) or not detail_table:
        return {
            "module": "3_raw",
            "rendered_md": (
                "## 原始测试数据\n\n"
                "_本次报告没有拿到完整的 4 引擎明细表 · 只能展示上方摘要证据。_\n\n"
                "---\n"
            ),
        }

    engine_labels = {
        "dashscope": "通义千问",
        "qwen": "通义千问",
        "deepseek": "DeepSeek",
        "yuanbao": "元宝",
        "kimi": "Kimi",
        "doubao": "豆包",
        "metaso": "秘塔/DeepSeek联网",
    }
    engines = ai_data.get("engines_tested") or ai_data.get("engines") or []
    if not engines:
        seen: list[str] = []
        for item in detail_table:
            results = item.get("results") or {}
            for engine in results.keys():
                if engine not in seen:
                    seen.append(engine)
        engines = seen

    question_types = ai_data.get("question_types") or {}
    layer_labels = {
        "brand_awareness": "品牌认知层",
        "regional_industry": "决策获客层",
        "super_tier1": "场景转化层",
        "local": "决策获客层",
        "scenario": "场景转化层",
    }

    # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 客户产物里的 layer / layer_key 是
    #   公开展示层(public_report_presentation)唯一能读到的分层信号。上游错标时
    #   必须在**写进产物之前**按题面纠正,否则展示层再怎么算都是错的。
    from services.brand_directed_question import (
        is_brand_directed_text,
        load_confirmed_aliases,
    )

    _raw_brand_name = (report_data.get("brand_name") or "").strip()
    _raw_aliases = load_confirmed_aliases(
        report_data.get("brand_id") or diagnosis_data.get("brand_id")
    )

    def _effective_layer_key(question: str) -> str:
        declared = question_types.get(question) or "super_tier1"
        if declared in {"brand_awareness", "brand"}:
            return declared
        if is_brand_directed_text(question, _raw_brand_name, aliases=_raw_aliases):
            return "brand_awareness"
        return declared

    def _engine_label(engine: str) -> str:
        return engine_labels.get((engine or "").lower(), engine or "未知引擎")

    def _public_result_metadata(result: dict[str, Any]) -> dict[str, Any]:
        """Keep only the observation fields needed by the public presentation.

        Older client artifacts dropped recommendation and citation facts even
        though the diagnosis collector had already produced them.  Preserve a
        strict allowlist here; provider metadata and internal errors never enter
        the customer artifact.
        """

        metadata: dict[str, Any] = {}
        outcome = result.get("target_outcome")
        if isinstance(outcome, str) and outcome in {
            "recommended", "conditionally_recommended", "candidate_only",
            "mentioned_only", "criteria_only", "refused_no_evidence",
            "refused_risk", "not_mentioned", "entity_ambiguous", "engine_error",
        }:
            metadata["target_outcome"] = outcome
        if isinstance(result.get("is_recommended"), bool):
            metadata["is_recommended"] = result["is_recommended"]
        # [P1-7 · 2026-07-26] 同行名单抽取状态进客户产物：让"这条回答没点名公司"
        # 和"我们没抽出来"在报告里可区分，不再把系统缺口画成空竞争格局。
        # 白名单枚举，不透传任意 provider 文本。
        if result.get("mentioned_brands_status") in {
            "ok", "no_brands_in_answer", "answer_too_short", "reused_upstream",
            "identity_unresolved", "extractor_not_configured", "extractor_error",
            "extractor_unparsable",
        }:
            metadata["mentioned_brands_status"] = result["mentioned_brands_status"]
        for field in ("brand_position", "matched_start", "matched_end"):
            value = result.get(field)
            if isinstance(value, int) and value >= 0:
                metadata[field] = value
        if isinstance(result.get("matched_text"), str):
            metadata["matched_text"] = result["matched_text"][:160]
        if result.get("evidence_level") in {"A", "B", "C"}:
            metadata["evidence_level"] = result["evidence_level"]
        # [WO_PAREN_UNKDISCLOSE_REWORK 2026-08-07 §4②] 「身份待确认」布尔。
        #
        # 🔴 存在的理由(生产实证,不是设想):出现率分母的明示在**真实报告里恒不出现**。
        # 同一份诊断 561,`raw_data_json.detail_table` 里待确认格 = 16,而报告模块层 = 0
        # —— 因为这个白名单在装配时就把 `detection_reason` / `brand_verdict` 丢掉了,
        # 下游 `is_identity_pending_cell` 只读落库字段、缺 `detection_reason` 时
        # fail-closed,于是恒返回 False → `identityPendingSamples` 恒 null →
        # 前端 `> 0` 条件恒不成立。全量 196 份 v2 报告里非 null = **0 份**。
        # 🔴 **这不是"存量不追溯"** —— 字段是在构建时丢的,新做的诊断同样不会亮。
        #
        # 为什么是**布尔**而不是把 `detection_reason` 透传下去:
        #   · `detection_reason` 的值长这样 `invalid_matched_text` —— 工程串绝不进客户产物
        #     (本函数 docstring 自己的铁律);
        #   · 布尔由 **`is_identity_pending_cell` 这个既有 SSOT 函数**产出,不是第二套判定
        #     —— 披露口径仍然单点,说出去的 N 等于分母里扣掉的 N;
        #   · 本白名单是严格枚举,加一个受控布尔比开一个字符串字段安全。
        # 装配时的 `result` 来自 `raw_data_json`,`detection_reason` 是齐的(实测缺失=0)。
        try:
            from services.diagnosis_identity_review import is_identity_pending_cell

            metadata["identity_pending"] = bool(is_identity_pending_cell(result))
        except Exception:  # pragma: no cover - 判据不可用不得拖垮报告装配
            pass
        for field in ("tested_at", "created_at"):
            value = result.get(field)
            if isinstance(value, str) and value.strip():
                metadata[field] = value[:64]

        for source_field in ("search_citations", "citations"):
            if source_field not in result:
                continue
            raw = result.get(source_field)
            if not isinstance(raw, (list, dict)):
                continue
            values = list(raw.values()) if isinstance(raw, dict) else raw
            safe: list[dict[str, str]] = []
            for citation in values:
                url = citation if isinstance(citation, str) else (
                    citation.get("url") or citation.get("link") or citation.get("source_url")
                    if isinstance(citation, dict)
                    else None
                )
                if isinstance(url, str) and url.strip():
                    try:
                        parsed = urlsplit(url.strip())
                        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                            continue
                        host = parsed.hostname.encode("idna").decode("ascii").lower()
                    except (UnicodeError, ValueError):
                        continue
                    # The customer artifact needs only a citation observation
                    # and public domain.  Paths, query strings and signatures
                    # are neither needed nor safe to persist here.
                    safe.append({"url": f"https://{host}"})
            metadata[source_field] = safe
        return metadata

    def _clean_text(text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "本引擎未返回可展示回答。"
        # Markdown/HTML 同时安全:保留原文语义,避免破坏 details/pre 结构
        text = text.replace("</", "<\\/")
        return text

    def _fold_repeated_response(text: str) -> tuple[str, str, bool, str]:
        """折叠连续重复句,避免客户版附录被模型循环输出刷屏.

        保留 raw_full_response 供 HTML 二级展开查看,默认展示只折叠连续重复片段。
        """
        original = _clean_text(text)
        if original == "本引擎未返回可展示回答。":
            return original, original, False, ""

        repeat_notes: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            block = match.group(0)
            unit = match.group(1)
            repeat_count = block.count(unit)
            if repeat_count < 4:
                return block
            repeat_notes.append(f"检测到同一句连续重复 {repeat_count} 次,已默认折叠。")
            return f"{unit}\n[重复片段已折叠: 上一句在原文中连续出现 {repeat_count} 次]\n"

        # 主要处理中文/英文句号结尾的模型循环输出;只折叠连续重复,不改写正常长文。
        folded = re.sub(r"(.{12,220}?[。！？!?])(?:\s*\1){3,}", _replace, original)
        was_folded = folded != original
        note = " ".join(dict.fromkeys(repeat_notes)) if repeat_notes else ""
        return folded, original, was_folded, note

    def _blockquote(text: str) -> str:
        return "\n".join(f"> {line}" if line.strip() else ">" for line in _clean_text(text).splitlines())

    md: list[str] = []
    total_tests = ai_data.get("total_tests") or sum(
        1
        for item in detail_table
        for result in (item.get("results") or {}).values()
        if isinstance(result, dict) and not str(result.get("answer_summary", "")).startswith("查询失败")
    )
    total_planned = ai_data.get("total_planned") or (len(detail_table) * max(1, len(engines)))
    detected_count = ai_data.get("detected_count")
    if detected_count is None:
        detected_count = sum(
            1
            for item in detail_table
            for result in (item.get("results") or {}).values()
            if isinstance(result, dict) and result.get("brand_detected")
        )

    structured_tests: list[dict[str, Any]] = []

    # [CTO-15.23 2026-05-21 老板订正] verbatim 模式下主表是题单 · 标题需明示
    # [#149 2026-09-08] 题单里现在可能全是 AI 出的题 ——
    #   「您填写的问题」「按您填写的 N 个原题」在那种题单上是**假话**,
    #   而假话在屏幕上和真话长得一模一样。按 origin 计数改写。
    #   🔴 `is_custom_mode` 的语义据此收窄为「题单里**含**客户自己写的题」,
    #      并由 diagnosis_question_origin 同一处产出。
    from services.diagnosis_question_origin import (
        has_customer_questions, is_verbatim as _is_verbatim, origin_counts)
    _qs = [i.get("question", "") for i in (detail_table or []) if isinstance(i, dict)]
    is_verbatim_mode = _is_verbatim(ai_data)
    _counts = origin_counts(ai_data, _qs)
    _ai_n, _cust_n = _counts.get("ai_suggested", 0), _counts.get("customer", 0)
    _has_customer = has_customer_questions(ai_data, _qs)
    main_title = ("## 原始测试数据(您填写的问题)"
                  if (is_verbatim_mode and _has_customer and not _ai_n)
                  else ("## 原始测试数据(本次题单)" if is_verbatim_mode
                        else "## 原始测试数据"))
    md.append(main_title)
    md.append("")
    if is_verbatim_mode:
        if _ai_n and _cust_n:
            _origin_phrase = f"本次题单 **{len(detail_table)} 道**(AI 出的 {_ai_n} 道 + 您填的 {_cust_n} 道)"
        elif _ai_n:
            _origin_phrase = f"本次题单 **{len(detail_table)} 道**(全部由 AI 为您出题)"
        else:
            _origin_phrase = f"本次按您填写的 **{len(detail_table)} 个原题**"
        md.append(
            f"{_origin_phrase} verbatim 跑 · 计划 **{total_planned} 次 AI 问答** · "
            f"有效 **{total_tests} 次** · 品牌被提及 **{detected_count} 次**。"
        )
        md.append("")
        md.append("> 系统完全按照题单原文跑(不改写不补足)")
    else:
        md.append(
            f"本次共设计 **{len(detail_table)} 个测试问题** · 计划 **{total_planned} 次 AI 问答** · "
            f"有效 **{total_tests} 次** · 品牌被提及 **{detected_count} 次**。"
        )
    md.append("")
    md.append("默认收起,方便客户先看结论;需要核对证据时,可以展开查看每个问题和各引擎原文。")
    md.append("")
    md.append("<details>")
    md.append("<summary>展开查看全部测试问题和 4 引擎原始回答</summary>")
    md.append("")
    md.append("### 测试词清单")
    md.append("")
    md.append("| # | 层级 | 测试问题 | 命中引擎 |")
    md.append("| --- | --- | --- | --- |")
    for idx, item in enumerate(detail_table, start=1):
        question = item.get("question") or ""
        if is_verbatim_mode:
            # verbatim 模式:用户题没经 LLM 分类 · 显"自定义"避免误导
            layer = "自定义"
        else:
            layer = layer_labels.get(_effective_layer_key(question), "场景转化层")
        results = item.get("results") or {}
        hit_engines = [
            _engine_label(engine)
            for engine, result in results.items()
            if isinstance(result, dict) and result.get("brand_detected")
        ]
        hit_label = "、".join(hit_engines) if hit_engines else "未命中"
        md.append(f"| {idx} | {layer} | `{question}` | {hit_label} |")
    md.append("")

    for idx, item in enumerate(detail_table, start=1):
        question = item.get("question") or ""
        if is_verbatim_mode:
            layer = "自定义"
        else:
            layer = layer_labels.get(_effective_layer_key(question), "场景转化层")
        results = item.get("results") or {}
        hit_count = sum(1 for result in results.values() if isinstance(result, dict) and result.get("brand_detected"))
        structured_results: list[dict[str, Any]] = []
        md.append("<details>")
        md.append(f"<summary>{idx}. {question} · {layer} · 命中 {hit_count}/{max(1, len(results) or len(engines))}</summary>")
        md.append("")
        for engine in engines:
            result = results.get(engine) or {}
            if not isinstance(result, dict):
                result = {}
            status = "提到品牌" if result.get("brand_detected") else "未提到品牌"
            brands = result.get("mentioned_brands") or []
            brand_line = f" · 提到:{'、'.join(str(x) for x in brands[:5])}" if brands else ""
            full_response = result.get("full_response") or result.get("response") or result.get("answer_summary") or ""
            display_response, raw_response, was_folded, fold_note = _fold_repeated_response(full_response)
            structured_results.append({
                "engine": engine,
                "engine_label": _engine_label(engine),
                "brand_detected": bool(result.get("brand_detected")),
                "status": status,
                "mentioned_brands": brands,
                "full_response": display_response,
                "raw_full_response": raw_response if was_folded else "",
                "was_folded": was_folded,
                "fold_note": fold_note,
                **_public_result_metadata(result),
            })
            md.append(f"#### {_engine_label(engine)} · {status}{brand_line}")
            md.append("")
            md.append(_blockquote(display_response))
            if was_folded:
                md.append("")
                md.append(f"> 注:{fold_note or '原文存在连续重复片段,客户版默认折叠;HTML 可展开查看未折叠原文。'}")
            md.append("")
        md.append("</details>")
        md.append("")
        structured_tests.append({
            "index": idx,
            "question": question,
            "layer": layer,
            # [P1-6 · 2026-07-26] 机器可判的层 key。展示层要按"品牌定向题不进
            # 竞争格局/提及率分母"处理，只有中文 label 会被文案改动带崩。
            # verbatim 模式没有分层 → 给 None，展示层按非定向题处理。
            "layer_key": (None if is_verbatim_mode else _effective_layer_key(question)),
            "hit_count": hit_count,
            "engine_count": max(1, len(results) or len(engines)),
            "results": structured_results,
        })

    md.append("</details>")
    md.append("")

    # [CTO-15.23 2026-05-21] 自定义检测问题独立 section(老板 5/21 截图实证需求)
    # 老板填的题在 ai_data.custom_visibility 里 · 主表只显系统题 · 客户报告这里追加
    # 让代理/客户能看到本次诊断除系统 baseline 8 题外 · 还跑了哪些自定义业务题命中情况
    custom_vis = ai_data.get("custom_visibility") or {}
    custom_detail_list = custom_vis.get("detail_table") or [] if isinstance(custom_vis, dict) else []
    custom_tests_structured: list[dict[str, Any]] = []
    if custom_detail_list:
        cu_total = custom_vis.get("total_tests", 0)
        cu_detected = custom_vis.get("detected_count", 0)
        cu_rate_pct = round((custom_vis.get("mention_rate", 0) or 0) * 100, 1)
        md.append("---")
        md.append("")
        md.append("## 自定义检测问题(您本次填写)")
        md.append("")
        md.append(
            f"您填写了 **{len(custom_detail_list)} 个自定义业务问题** · "
            f"有效 **{cu_total} 次** · 品牌被提及 **{cu_detected} 次** · "
            f"出现率 **{cu_rate_pct}%**。"
        )
        md.append("")
        md.append("> 自定义问题为您业务专属测试 · 独立于系统 baseline · 不影响对外 GEO 主分。")
        md.append("")
        md.append("<details open>")
        md.append("<summary>展开查看自定义问题 4 引擎原始回答</summary>")
        md.append("")
        md.append("### 自定义问题清单")
        md.append("")
        md.append("| # | 您填写的问题 | 命中引擎 |")
        md.append("| --- | --- | --- |")
        for idx, item in enumerate(custom_detail_list, start=1):
            question = item.get("question") or ""
            results = item.get("results") or {}
            hit_engines = [
                _engine_label(engine)
                for engine, result in results.items()
                if isinstance(result, dict) and result.get("brand_detected")
            ]
            hit_label = "、".join(hit_engines) if hit_engines else "未命中"
            md.append(f"| {idx} | `{question}` | {hit_label} |")
        md.append("")
        for idx, item in enumerate(custom_detail_list, start=1):
            question = item.get("question") or ""
            results = item.get("results") or {}
            hit_count = sum(
                1 for result in results.values()
                if isinstance(result, dict) and result.get("brand_detected")
            )
            md.append("<details>")
            md.append(
                f"<summary>{idx}. {question} · 命中 "
                f"{hit_count}/{max(1, len(results) or len(engines))}</summary>"
            )
            md.append("")
            cust_struct_results: list[dict[str, Any]] = []
            for engine in engines:
                result = results.get(engine) or {}
                if not isinstance(result, dict):
                    result = {}
                status = "提到品牌" if result.get("brand_detected") else "未提到品牌"
                brands = result.get("mentioned_brands") or []
                brand_line = f" · 提到:{'、'.join(str(x) for x in brands[:5])}" if brands else ""
                full_response = result.get("full_response") or result.get("response") or result.get("answer_summary") or ""
                display_response, raw_response, was_folded, fold_note = _fold_repeated_response(full_response)
                cust_struct_results.append({
                    "engine": engine,
                    "engine_label": _engine_label(engine),
                    "brand_detected": bool(result.get("brand_detected")),
                    "status": status,
                    "mentioned_brands": brands,
                    "full_response": display_response,
                    "raw_full_response": raw_response if was_folded else "",
                    "was_folded": was_folded,
                    "fold_note": fold_note,
                    **_public_result_metadata(result),
                })
                md.append(f"#### {_engine_label(engine)} · {status}{brand_line}")
                md.append("")
                md.append(_blockquote(display_response))
                if was_folded:
                    md.append("")
                    md.append(f"> 注:{fold_note or '原文存在连续重复片段,客户版默认折叠'}")
                md.append("")
            md.append("</details>")
            md.append("")
            custom_tests_structured.append({
                "index": idx,
                "question": question,
                "source": "custom",
                "hit_count": hit_count,
                "engine_count": max(1, len(results) or len(engines)),
                "results": cust_struct_results,
            })
        md.append("</details>")
        md.append("")

    md.append("---")

    return {
        "module": "3_raw",
        "questions": len(detail_table),
        "engines": engines,
        "total_planned": total_planned,
        "total_tests": total_tests,
        "detected_count": detected_count,
        "tests": structured_tests,
        # [CTO-15.23 2026-05-21] 自定义题独立结构 · 给前端/PDF 单独渲染用
        "custom_tests": custom_tests_structured,
        "custom_summary": {
            "questions": len(custom_detail_list),
            "total_tests": (custom_vis.get("total_tests", 0) if isinstance(custom_vis, dict) else 0),
            "detected_count": (custom_vis.get("detected_count", 0) if isinstance(custom_vis, dict) else 0),
            "mention_rate": (custom_vis.get("mention_rate", 0) if isinstance(custom_vis, dict) else 0),
        } if custom_detail_list else None,
        "rendered_md": "\n".join(md),
    }


# ============================================================================
# E3 报价竞争位 · E4 诊断体检交叉(答案实体消费接线 SPEC · additive 只读模块)
# ----------------------------------------------------------------------------
# 两者只读 report_data['diagnosis_data']['ai_visibility_data'] 的 detail_table +
# engine_stats · 不重跑引擎 / 不改算价 / 不动既有模块。命中口径直接复用
# detail_table.brand_detected(ai_tester 上游已用 _exact_or_strict_match 判过 · 零重算);
# 名次用 mentioned_brands 有序 index+1。数字同源:M/N 优先取 engine_stats.detected/total
# (与 module2 漏斗 / diagnosis summary reconcile)· engines_hit 对齐 engines_mentioned。
# 🔴 算价函数(calculate_*/generate_tiered_quotes/generate_batch_quote)0 碰 0 import。
# ============================================================================

# 引擎中文名映射 · 与 build_module_3_raw_ai_appendix 内 engine_labels 闭包保持同步
_E3E4_ENGINE_LABELS = {
    "dashscope": "通义千问",
    "qwen": "通义千问",
    "deepseek": "DeepSeek",
    "yuanbao": "元宝",
    "kimi": "Kimi",
    "doubao": "豆包",
    "metaso": "秘塔/DeepSeek联网",
}


def _e3e4_engine_label(engine: str) -> str:
    return _E3E4_ENGINE_LABELS.get((engine or "").lower(), engine or "未知引擎")


def _load_brand_matchers():
    """懒加载 ai_tester 命中判定件(复用 · 禁重写)· import 失败给最小兜底保 fail-safe。

    正常路径复用 tools.ai_visibility.ai_tester._exact_or_strict_match / _normalize_brand_name;
    仅当该模块 import 失败(理论不会 · 保险)才降级到 normalize 相等的最小匹配,保证不抛。
    """
    try:
        from tools.ai_visibility.ai_tester import (
            _exact_or_strict_match,
            _normalize_brand_name,
        )
        return _exact_or_strict_match, _normalize_brand_name
    except Exception:  # pragma: no cover · 仅 ai_tester 不可用时兜底
        import re as _re_fallback

        def _norm(s: str) -> str:
            if not s:
                return ""
            out = _re_fallback.sub(r"[（(][^）)]*[）)]", "", s)
            for ch in (" ", "　", "·", "•", "・"):
                out = out.replace(ch, "")
            return out.strip().lower()

        def _match(full: str, cands) -> tuple[bool, str]:
            fn = _norm(full)
            if not fn:
                return False, ""
            for c in cands or []:
                if c and _norm(c) == fn:
                    return True, c
            return False, ""

        return _match, _norm


def _e3e4_describe_occurrence(prob: float) -> str:
    """客户面出现率话术层 · 复用 transparent_pricing.describe_probability SSOT(禁裸 %/SOV)。

    SSOT 在 prob>=0.90 会返"约 X% 被 AI 提及"(含 %)· 客户面禁裸 % → 收敛成"几乎每次都出现";
    import 失败时用同阈值内联兜底(均不含 %)。绝不 import 算价主函数。
    """
    try:
        prob = max(0.0, min(1.0, float(prob or 0)))
    except (TypeError, ValueError):
        prob = 0.0
    try:
        from tools.transparent_pricing import describe_probability
        text = describe_probability(prob)
        return "几乎每次都出现" if "%" in text else text
    except Exception:  # pragma: no cover · 镜像 describe_probability 阈值(无 %)
        if prob >= 0.90:
            return "几乎每次都出现"
        if prob >= 0.73:
            return "问4次约出现3次"
        if prob >= 0.58:
            return "问3次约出现2次"
        if prob >= 0.43:
            return "问2次约出现1次"
        if prob >= 0.25:
            return "问4次约出现1次"
        return "偶尔出现"


def build_module_3_competition(
    report_data: dict, *, audience: str = "internal", brand_id: int | None = None
) -> dict[str, Any]:
    """E3 · AI 推荐竞争位(additive 只读模块)

    读 ai_visibility_data.detail_table(每 result 含有序 mentioned_brands + brand_detected)+
    engine_stats · 聚合出「AI 现在推荐谁(top 实体 + 频次)· 客户被推荐 M/N 次 · 平均排第 K」。

    audience 分支(铁律):
      - client:命中率走 _e3e4_describe_occurrence(describe_probability 话术层 · 禁裸 %/SOV)·
                名次给定性档位(前列/中部/靠后)· 未进名单给转化话术
      - internal(默认):代理内审 · 精确 M/N + rate% + 平均名次

    数字同源:M/N 优先取 engine_stats.detected/total(缺则 detail_table.brand_detected 兜底);
              名次取 mentioned_brands 有序 index+1(仅命中项)· 是 detected 的子集(不硬绑相等)。
    fail-safe:detail_table 空/缺 → 返占位 dict · 绝不抛(不打断 assemble_report_v2)。
    """
    is_client = (audience or "").lower() == "client"
    diagnosis_data = report_data.get("diagnosis_data") or {}
    ai_data = diagnosis_data.get("ai_visibility_data") or {}
    detail_table = ai_data.get("detail_table") or []
    brand_name = (report_data.get("brand_name") or "").strip()

    if not isinstance(detail_table, list) or not detail_table:
        return {
            "module": "3_competition",
            "available": False,
            "top_brands": [],
            "client_detected_count": 0,
            "valid_total": 0,
            "avg_rank": None,
            "rendered_md": (
                "## AI 推荐竞争位\n\n"
                "_数据不足 · 完成诊断后展示 AI 当前推荐的品牌名单与你的竞争位。_\n\n"
                "---\n"
            ),
        }

    _exact_or_strict_match, _normalize_brand_name = _load_brand_matchers()
    engine_stats = ai_data.get("engine_stats") or {}
    client_norm = _normalize_brand_name(brand_name) if brand_name else ""

    # [P1-6 · 2026-07-26] 品牌定向题("XX 是做什么的")必然命中，不进竞争格局分母，
    #   否则提及率被自己的品牌词顶高，客户看到的竞争位是假的。
    question_types = ai_data.get("question_types") if isinstance(ai_data.get("question_types"), dict) else {}
    # [#149 2026-09-08] 豁免逐题化:谁享豁免只由 diagnosis_question_origin 判定。
    #   本处**本来就是逐题**传 verbatim= 的,所以只把「整批同一个值」换成
    #   「这道题在不在豁免集里」—— AI 出的品牌定向题不再享豁免。
    from services.diagnosis_question_origin import brand_filter_exempt_questions
    _exempt_questions = frozenset(brand_filter_exempt_questions(
        ai_data, [i.get("question", "") for i in (detail_table or [])
                  if isinstance(i, dict)]))

    # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 只信标签 = 上游错标则全链错。
    #   报告 551 实证:第二道公司题挂着 super_tier1 标签,这里就把它当竞争面样本收进分母。
    #   加**与标签独立的文本判据**,两者不一致时以文本为准并留痕(relabeled 计数即错标率)。
    from services.brand_directed_question import (
        is_client_own_brand,
        load_confirmed_aliases,
        resolve_brand_directed,
    )

    _aliases = load_confirmed_aliases(
        brand_id
        or report_data.get("brand_id")
        or (report_data.get("diagnosis_data") or {}).get("brand_id")
    )
    # 按**题**留痕(下面两个循环都会问一遍同一道题,按次数计会翻倍)。
    brand_directed_relabeled_questions: set[str] = set()

    def _is_brand_directed(question: str) -> bool:
        verdict = resolve_brand_directed(
            question,
            layer_key=question_types.get(question),
            brand_name=brand_name,
            aliases=_aliases,
            # [#149] 逐题:只有**客户自己写的**题才享豁免。
            verbatim=(question in _exempt_questions),
        )
        if verdict.relabeled_by_text:
            brand_directed_relabeled_questions.add(question)
        return verdict.is_brand_directed

    def _is_client_self(name: str) -> bool:
        """[§2.3 双保险] 分类再错,客户也绝不能出现在自己的竞品榜里。"""
        return is_client_own_brand(name, brand_name, aliases=_aliases)

    # ── 聚合 AI 推荐名单频次 + 客户名次(纯读 mentioned_brands · 零 LLM · 零重跑)──
    brand_freq: dict[str, dict[str, Any]] = {}
    client_ranks: list[int] = []
    # [P1-7] 抽取状态计数:分清"AI 回答里真的没点名公司"与"我们没抽出来"
    extraction_status_counts: dict[str, int] = {}
    for item in detail_table:
        results = item.get("results") or {}
        if not isinstance(results, dict):
            continue
        if _is_brand_directed(item.get("question") or ""):
            continue
        for _engine, result in results.items():
            if not isinstance(result, dict):
                continue
            _status = result.get("mentioned_brands_status")
            if isinstance(_status, str) and _status:
                extraction_status_counts[_status] = extraction_status_counts.get(_status, 0) + 1
            mb = result.get("mentioned_brands") or []
            if not isinstance(mb, list):
                mb = []
            seen_here: set[str] = set()
            for b in mb:
                name = str(b).strip()
                if not name:
                    continue
                norm = _normalize_brand_name(name)
                if not norm:
                    continue
                # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.3] 旧判据是**归一后精确相等**,
                #   而客户登记名常与 AI 回答里的写法差一截法人后缀:
                #   brand_name「深圳市晨光富士电梯」vs 回答里「深圳市晨光富士电梯有限公司」
                #   → 不相等 → 客户以 4 次出现在自家竞品榜(报告 551 实证)。
                #   改走命中判定 SSOT(双向包含 + 短方 ≥6 字防误中)+ 已确认别名,
                #   与分类结果**无关**地无条件剔除。
                if _is_client_self(name):
                    continue  # 排除客户品牌自身 · 只统计竞争对手
                if norm in seen_here:
                    continue  # 单条回答内去重(避免刷频次)
                seen_here.add(norm)
                entry = brand_freq.setdefault(norm, {"name": name, "count": 0})
                entry["count"] += 1
            # 客户名次:brand_detected 命中项在有序 mentioned_brands 里的 index+1
            if brand_name and mb:
                matched, matched_text = _exact_or_strict_match(brand_name, mb)
                if matched:
                    m_norm = _normalize_brand_name(matched_text)
                    idx = next(
                        (i for i, b in enumerate(mb) if _normalize_brand_name(str(b)) == m_norm),
                        None,
                    )
                    if idx is not None:
                        client_ranks.append(idx + 1)

    # ── M/N 竞争口径分母(P1-6):**排除品牌定向题**后逐格统计。
    #   旧版优先取 engine_stats.detected/total —— 那是含品牌定向题的全量口径，
    #   定向题必然命中，会把"客户在竞争问题里的出现率"顶高（驰鲸案例:品牌层 4/4
    #   进分母后，竞争位读起来比真实情况好）。竞争格局必须只看非定向题。
    detected_total = 0
    valid_total = 0
    brand_directed_valid = 0
    brand_directed_detected = 0
    for item in detail_table:
        directed = _is_brand_directed(item.get("question") or "")
        for result in (item.get("results") or {}).values():
            if not isinstance(result, dict):
                continue
            answer = str(result.get("answer_summary", "") or "")
            if "查询失败" in answer or answer.startswith("Error"):
                continue
            if result.get("engine_error"):
                continue
            if directed:
                brand_directed_valid += 1
                if result.get("brand_detected"):
                    brand_directed_detected += 1
                continue
            valid_total += 1
            if result.get("brand_detected"):
                detected_total += 1

    hit_rate = (detected_total / valid_total) if valid_total > 0 else 0.0
    avg_rank = round(sum(client_ranks) / len(client_ranks), 1) if client_ranks else None
    top_brands = sorted(
        brand_freq.values(),
        key=lambda x: (-x["count"], x["name"]),
    )[:8]

    # ── 渲染 ──
    md: list[str] = ["## AI 推荐竞争位", ""]
    md.append("这是 AI 现在实测最常主动推荐的品牌名单 · 以及你目前所处的竞争位。")
    md.append("")

    # [P1-7 · 2026-07-26] 本次实测抽不到同行名单时，回落到蒸馏飞轮的真实竞品源。
    #   生产实证:competitors / competitor_snapshots 表 0 行，真实竞品在
    #   keyword_insights.brands_found（监测蒸馏侧）。旧版只读本次 mentioned_brands，
    #   抽取一失败竞争格局就只剩"本品牌"一条 —— 客户以为自己没有对手。
    competitor_source = "diagnosis_answers" if top_brands else None
    if not top_brands:
        top_brands, competitor_source = _competitor_fallback_from_flywheel(
            report_data, client_norm, _normalize_brand_name, brand_id=brand_id,
            is_client_self=_is_client_self,
        )

    # 名单为空时说明**原因 + 下一步**（A1：不给一个没有出口的空块）
    extraction_failed = any(
        extraction_status_counts.get(key)
        for key in ("extractor_not_configured", "extractor_error", "extractor_unparsable")
    )

    if top_brands:
        md.append(
            "### AI 当前推荐名单(实测高频实体)"
            if competitor_source == "diagnosis_answers"
            else "### 同行名单(近 90 天监测实测汇总)"
        )
        md.append("")
        md.append("| 排序 | 品牌 | 被 AI 推荐次数 |")
        md.append("| --- | --- | --- |")
        for i, b in enumerate(top_brands, start=1):
            md.append(f"| {i} | {b['name']} | {b['count']} 次 |")
        md.append("")
        if competitor_source != "diagnosis_answers":
            md.append("> 本次实测没能从回答里汇总出同行名单 · 上表来自该品牌近 90 天的监测记录。")
            md.append("")
    elif extraction_failed:
        md.append(
            "_同行名单本次未采集成功(名单汇总环节失败,不代表该行业没有同行)。"
            "可以重跑一次诊断,或在监测里积累几天数据后再看这一节。_"
        )
        md.append("")
    else:
        md.append(
            "_本次 AI 回答里没有点名任何具体同行(多为泛化描述)。"
            "这类问题下 AI 还没有形成品牌名单 —— 反而是抢先进入名单的机会。_"
        )
        md.append("")

    if detected_total > 0:
        if is_client:
            occ = _e3e4_describe_occurrence(hit_rate)
            md.append(f"**你的竞争位**:{brand_name or '你的品牌'} 已经进入 AI 推荐名单 · {occ}。")
            if avg_rank is not None:
                band = "前列" if avg_rank <= 3 else ("中部" if avg_rank <= 6 else "靠后")
                md.append(f"目前通常出现在推荐名单的{band}位置 · 继续建设可往前挤。")
        else:
            rate_pct = round(hit_rate * 100, 1)
            md.append(
                f"**你的竞争位**:{brand_name or '客户品牌'} 被推荐 "
                f"**{detected_total}/{valid_total}** 次(命中率 **{rate_pct}%**)。"
            )
            if avg_rank is not None:
                md.append(f"在被提及的回答里 · 平均排在第 **{avg_rank}** 位。")
    else:
        md.append(f"**你的竞争位**:{brand_name or '你的品牌'} 目前还没有进入这份 AI 推荐名单。")
        md.append("**目标是让你进入这份名单** · 让 AI 在被问到时主动把你说出来。")

    md.append("")
    md.append("---")

    return {
        "module": "3_competition",
        "available": True,
        "audience": ("client" if is_client else "internal"),
        "top_brands": top_brands,
        "client_detected_count": detected_total,
        "valid_total": valid_total,
        "hit_rate": round(hit_rate, 4),
        "avg_rank": avg_rank,
        "in_list": detected_total > 0,
        # [P1-6] 竞争口径已剔除的品牌定向题样本(报告注明口径用)
        "brand_directed_valid": brand_directed_valid,
        "brand_directed_detected": brand_directed_detected,
        # 🔴 [WO_236-c1a 2026-09-17] 原来这里是**硬编码常量**
        #    `"excludes_brand_directed_questions"` —— 它是一句关于"本代码打算排除"的
        #    声明,不是关于"这次到底排没排"的事实。真客户报告 #700/#726 里
        #    `brand_directed_valid == 0`(题单 verbatim ⇒ 全部题豁免 ⇒ 一条没排),
        #    而这个常量照样说"已排除",页面脚注据此宣称口径干净。
        #    改成**按实际发生的事算**:一条都没排就如实说没排。
        "denominator_scope": (
            "excludes_brand_directed_questions" if brand_directed_valid > 0
            else "all_valid_answers"),
        # [WO_BRAND_QUESTION_LEAK 2026-08-05 §2.2] 留痕:标签说不是品牌题、题面说是
        #   → 以文本为准。这个数就是上游出题层的**错标率观测口**,不进报告文案。
        "brand_directed_relabeled_by_text": len(brand_directed_relabeled_questions),
        # [P1-7] 名单来源与抽取状态(空名单要能说清是"真没有"还是"没抽到")
        "competitor_source": competitor_source,
        "competitor_extraction_status": extraction_status_counts,
        "competitor_extraction_failed": bool(extraction_failed),
        "rendered_md": "\n".join(md),
    }


def _competitor_fallback_from_flywheel(
    report_data: dict,
    client_norm: str,
    normalize_brand_name,
    *,
    brand_id: int | None = None,
    is_client_self=None,
) -> tuple[list[dict[str, Any]], str | None]:
    """本次实测拿不到同行名单时，回落到蒸馏飞轮 keyword_insights.brands_found。

    只读、零 LLM、零 provider。取不到（无 brand_id / 无数据 / DB 异常）就返回空，
    由调用方给出"为什么空 + 下一步"，绝不编造名单。
    """
    brand_id = (
        brand_id
        or report_data.get("brand_id")
        or (report_data.get("diagnosis_data") or {}).get("brand_id")
    )
    try:
        brand_id = int(brand_id)
    except (TypeError, ValueError):
        return [], None
    if brand_id <= 0:
        return [], None
    try:
        from db.distillation_db import get_competitor_radar

        radar = get_competitor_radar(brand_id, days=90) or {}
    except Exception as exc:  # 只读兜底失败不得影响报告装配
        print(f"[3_competition] 飞轮竞品回落失败({type(exc).__name__})")
        return [], None

    rows: list[dict[str, Any]] = []
    for entry in (radar.get("brands") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("brand_name") or "").strip()
        if not name:
            continue
        norm = normalize_brand_name(name)
        if not norm:
            continue
        # [§2.3 双保险] 飞轮回落路径同口径:客户自身及其已确认别名无条件剔除。
        # is_client_self 缺省(旧调用方)时退回归一相等,行为与改前一致。
        if is_client_self(name) if callable(is_client_self) else (client_norm and norm == client_norm):
            continue  # 本品牌不进竞品名单
        count = int(entry.get("mention_count") or 0)
        if count <= 0:
            continue
        rows.append({"name": name, "count": count})
    rows.sort(key=lambda x: (-x["count"], x["name"]))
    if not rows:
        return [], None
    return rows[:8], "flywheel_keyword_insights"


def build_module_e4_health_cross(report_data: dict, *, audience: str = "internal") -> dict[str, Any]:
    """E4 · AI 体检交叉(additive 只读模块 · 克隆 build_module_3_raw_ai_appendix 结构)

    pivot 成「按 AI 引擎分组:客户品牌是否命中(brand_detected)vs 该引擎实际推荐名单
    (mentioned_brands 并集)」· 命中口径直接复用 engine_stats.detected/total + brand_detected
    (零重算命中 · 与 diagnosis summary reconcile · 不按 mentioned_brands 自行重数命中)。

    audience(铁律):v1 默认 internal(代理内审 · 可露精确命中数)。
      · client 面:命中率走 _e3e4_describe_occurrence 话术层(禁裸 %/SOV)· 命中显 ✅/❌ 不露精确数。
      · 建议 v1 只 internal(client 门户接入前需二次评审话术),但已按 audience 参数就绪。
    fail-safe:detail_table 空 → 返占位 dict · 不抛。
    """
    is_client = (audience or "").lower() == "client"
    diagnosis_data = report_data.get("diagnosis_data") or {}
    ai_data = diagnosis_data.get("ai_visibility_data") or {}
    detail_table = ai_data.get("detail_table") or []

    if not isinstance(detail_table, list) or not detail_table:
        return {
            "module": "e4",
            "available": False,
            "engines": [],
            "engines_hit_count": 0,
            "engine_count": 0,
            "rendered_md": (
                "## AI 体检交叉\n\n"
                "_数据不足 · 完成诊断后展示各 AI 引擎命中与推荐名单的交叉体检。_\n\n"
                "---\n"
            ),
        }

    _exact_or_strict_match, _normalize_brand_name = _load_brand_matchers()
    engine_stats = ai_data.get("engine_stats") or {}
    client_norm = _normalize_brand_name((report_data.get("brand_name") or "").strip())

    # 引擎清单:优先 engines_tested/engines · 缺则从 detail_table results 收集(同 build_module_3_raw)
    engines = ai_data.get("engines_tested") or ai_data.get("engines") or []
    if not engines:
        seen: list[str] = []
        for item in detail_table:
            for engine in (item.get("results") or {}).keys():
                if engine not in seen:
                    seen.append(engine)
        engines = seen

    pivot: list[dict[str, Any]] = []
    engines_hit = 0
    for engine in engines:
        # 推荐名单并集(排除客户自身)· 纯读 mentioned_brands
        union_names: list[str] = []
        union_seen: set[str] = set()
        for item in detail_table:
            result = (item.get("results") or {}).get(engine)
            if not isinstance(result, dict):
                continue
            for b in (result.get("mentioned_brands") or []):
                name = str(b).strip()
                if not name:
                    continue
                norm = _normalize_brand_name(name)
                if not norm or (client_norm and norm == client_norm):
                    continue
                if norm in union_seen:
                    continue
                union_seen.add(norm)
                union_names.append(name)
        # 命中数:engine_stats 权威(数字同源)· 缺则 detail_table.brand_detected 兜底(排除查询失败)
        est = engine_stats.get(engine) if isinstance(engine_stats, dict) else None
        if isinstance(est, dict) and int(est.get("total", 0) or 0) > 0:
            detected = int(est.get("detected", 0) or 0)
            total = int(est.get("total", 0) or 0)
        else:
            detected = 0
            total = 0
            for item in detail_table:
                result = (item.get("results") or {}).get(engine)
                if not isinstance(result, dict):
                    continue
                answer = str(result.get("answer_summary", "") or "")
                if "查询失败" in answer or answer.startswith("Error"):
                    continue
                total += 1
                if result.get("brand_detected"):
                    detected += 1
        hit = detected > 0
        if hit:
            engines_hit += 1
        pivot.append({
            "engine": engine,
            "engine_label": _e3e4_engine_label(engine),
            "brand_detected": hit,
            "detected": detected,
            "total": total,
            "rate": round((detected / total), 4) if total > 0 else 0.0,
            "recommended_union": union_names[:10],
        })

    # ── 渲染 ──
    md: list[str] = ["## AI 体检交叉", ""]
    md.append("按 AI 引擎逐个体检:你的品牌是否被这台 AI 主动推荐 · 以及它现在实际在推荐谁。")
    md.append("")
    if is_client:
        # client 面:命中率走话术 · 不露 %/精确命中数
        md.append("| AI 引擎 | 是否被推荐 | 出现频率 | 这台 AI 常推荐的品牌 |")
        md.append("| --- | --- | --- | --- |")
        for p in pivot:
            flag = "✅ 命中" if p["brand_detected"] else "❌ 未命中"
            occ = _e3e4_describe_occurrence(p["rate"]) if p["total"] > 0 else "本次无有效回答"
            rec = "、".join(p["recommended_union"][:5]) if p["recommended_union"] else "—"
            md.append(f"| {p['engine_label']} | {flag} | {occ} | {rec} |")
    else:
        md.append("| AI 引擎 | 你的品牌是否命中 | 命中/测试 | 该引擎实测推荐名单 |")
        md.append("| --- | --- | --- | --- |")
        for p in pivot:
            flag = "✅ 命中" if p["brand_detected"] else "❌ 未命中"
            rec = "、".join(p["recommended_union"][:8]) if p["recommended_union"] else "—"
            md.append(f"| {p['engine_label']} | {flag} | {p['detected']}/{p['total']} | {rec} |")
    md.append("")
    md.append(f"**汇总**:{len(pivot)} 个 AI 引擎中 · 你在 **{engines_hit}** 个引擎被 AI 推荐过。")
    md.append("")
    md.append("---")

    return {
        "module": "e4",
        "available": True,
        "audience": ("client" if is_client else "internal"),
        "engines": pivot,
        "engines_hit_count": engines_hit,
        "engine_count": len(pivot),
        "rendered_md": "\n".join(md),
    }


# ============================================================================
# Module 4 · 竞品分析(从 evidences.search_citations 聚合)
# ============================================================================

def build_module_4_competitors(
    _report_data: dict,
    evidences: list[dict] | None = None,
    brand_id: int | None = None,
) -> dict[str, Any]:
    """Module 4 · 竞品分析 · search_citations 启发式 + brand 建档真竞品(C2.2)

    B.5 · Codex 0424 P1.3d 阶段目标话术
    C2.2 (CTO-15.9 session 3 · 2026-04-25) · brand_id 给定时优先用代理建档的竞品
      · brands.competitors_jsonb 真数据(代理 PUT /api/brands/{id}/competitors)
      · 没建档时退回 search_citations 启发式
    """
    evidences = evidences or []
    citation_counter: dict[str, int] = {}
    for e in evidences:
        citations = e.get("search_citations") or []
        for c in citations:
            if isinstance(c, dict):
                url = c.get("url", "") or c.get("link", "")
                title = c.get("title", "") or c.get("name", "")
                key = title or url
                if key and key.strip():
                    citation_counter[key] = citation_counter.get(key, 0) + 1

    top = sorted(citation_counter.items(), key=lambda x: -x[1])[:5]
    total_evidences = max(1, len(evidences))  # 防 0 除

    # [Phase 3 2026-06-07] 同频对照空兜底:监测 evidences 算不出同频引用源时(只诊断没监测 /
    # 监测来源为空),改读【本次诊断 detail_table 自己的 search_citations】。Phase 1/2A/2B 已把
    # 4 引擎来源透传进 detail_table.results.<engine>.search_citations,这里与 build_module_3_authority
    # 同源同口径。仅在监测算不出同频(citation_counter 为空)时触发 → 有监测数据的品牌行为完全不变。
    if not citation_counter:
        diag_table = (
            ((_report_data or {}).get("diagnosis_data") or {}).get("ai_visibility_data") or {}
        ).get("detail_table") or []
        diag_answers = 0
        if isinstance(diag_table, list):
            for item in diag_table:
                if not isinstance(item, dict):
                    continue
                results = item.get("results") or {}
                if not isinstance(results, dict):
                    continue
                for _engine, res in results.items():
                    if not isinstance(res, dict):
                        continue
                    diag_answers += 1
                    for c in (res.get("search_citations") or []):
                        if isinstance(c, dict):
                            url = c.get("url", "") or c.get("link", "")
                            title = c.get("title", "") or c.get("name", "")
                            key = title or url
                            if key and key.strip():
                                citation_counter[key] = citation_counter.get(key, 0) + 1
        if citation_counter:
            top = sorted(citation_counter.items(), key=lambda x: -x[1])[:5]
            total_evidences = max(total_evidences, diag_answers, 1)

    # C2.2 (CTO-15.9 session 3 · 2026-04-25 · M2 §Epic 4) 拉 brand 建档真竞品
    declared_competitors: list[dict] = []
    if brand_id:
        try:
            from db.connection import get_connection as _gc
            conn = _gc()
            try:
                cur = conn.cursor()
                cur.execute("SELECT competitors_jsonb FROM brands WHERE id = %s", (brand_id,))
                row = cur.fetchone()
                if row:
                    raw = row.get("competitors_jsonb") or []
                    if isinstance(raw, str):
                        import json as _json
                        try:
                            raw = _json.loads(raw)
                        except (ValueError, TypeError):
                            raw = []
                    if isinstance(raw, list):
                        declared_competitors = [c for c in raw if isinstance(c, dict) and c.get("name")]
            finally:
                conn.close()
        except Exception as _ce:
            logger.warning(f"[report_v2 module 4] 拉 brand competitors 失败(降级启发式): {_ce}")

    md = ["## 竞品与信源差距", ""]
    md.append("AI 推荐谁,通常不是因为它主观判断谁更好,而是因为谁有更多可引用、可验证、可复述的公开素材。")
    md.append("")

    # C2.2 · 优先展示代理建档的真竞品 + 在 evidences 里命中次数交叉
    if declared_competitors:
        md.append(f"### 代理建档竞品({len(declared_competitors)} 个 · 真数据)")
        md.append("")
        md.append("| 竞品名 | 在 AI 实测中出现 | URL | 备注 |")
        md.append("| --- | --- | --- | --- |")
        for c in declared_competitors:
            cname = c.get("name", "")
            # 数 evidences 中提到该名字的条数
            mention_count = sum(
                1 for e in evidences
                if cname and cname in (e.get("response_snippet") or "")
            )
            md.append(
                f"| {cname} | {mention_count} 次 / {total_evidences} 条 | "
                f"{c.get('url') or '—'} | {c.get('note') or ''} |"
            )
        md.append("")

    # CTO-15.16 M1c · 没建档时再退回 brief.local_competitors / profile.local_competitors
    # 优先级:brand.competitors_jsonb > brief.local_competitors > profile.local_competitors > metaso 启发式
    brief_local: list = []
    if not declared_competitors:
        brief_obj = (_report_data or {}).get("brief") or {}
        if isinstance(brief_obj, dict):
            blc = brief_obj.get("local_competitors")
            if isinstance(blc, list):
                brief_local = [
                    x for x in blc
                    if isinstance(x, (str, dict)) and (x if isinstance(x, str) else x.get("name"))
                ]
        if not brief_local:
            plc = (_report_data or {}).get("local_competitors")
            if isinstance(plc, list):
                brief_local = [
                    x for x in plc
                    if isinstance(x, (str, dict)) and (x if isinstance(x, str) else x.get("name"))
                ]
    if brief_local:
        md.append(f"### Brief 行业洞察 · 本地竞品候选({len(brief_local)} 个 · 来自代理审核 brief)")
        md.append("")
        md.append("| 竞品名 | 在 AI 实测中出现 | 来源 |")
        md.append("| --- | --- | --- |")
        for c in brief_local[:10]:
            cname = c if isinstance(c, str) else c.get("name", "")
            if not cname:
                continue
            mention_count = sum(
                1 for e in evidences
                if cname in (e.get("response_snippet") or "")
            )
            md.append(f"| {cname} | {mention_count} 次 / {total_evidences} 条 | brief |")
        md.append("")
        md.append("_这些是 AI 从资料里推测的候选竞品,需要顾问确认后再进入正式对标。_")
        md.append("")

    if not top and not declared_competitors and not brief_local:
        md.append("_本期未抽到可靠竞品引用数据 · 本段只说明信息差,不做竞品排名。_")
    elif top:
        md.append(f"### AI 可引用素材差距(启发式 · {total_evidences} 条数据)")
        md.append("")
        md.append("| 排名 | AI 可引用素材 / 竞品源 | 同频出现 | 大致频率 | 对你的启发 |")
        md.append("| --- | --- | --- | --- | --- |")
        for i, (title, cnt) in enumerate(top, start=1):
            ratio = cnt / total_evidences
            if ratio >= 0.8:
                freq = "几乎每次都出现"
            elif ratio >= 0.5:
                freq = f"超过半数(每 2 次约 {round(ratio * 2)} 次)"
            elif ratio >= 0.25:
                freq = f"约 1/4(每 4 次约 {max(1, round(ratio * 4))} 次)"
            else:
                freq = f"零星出现(共 {cnt} 次)"
            # CTO-15.16 round2 Task C · title 是第三方文章标题 · backtick 标记为引文
            md.append(
                f"| {i} | `{title[:60]}` | {cnt} 次 / {total_evidences} 条 | {freq} | "
                "把对应主题做成官网 FAQ、案例页或第三方报道素材 |"
            )
    md.append("")
    md.append("### AI 还看不到的信源类型")
    md.append("")
    md.append("| 信源类型 | 现在常见缺口 | 为什么影响 AI 推荐 |")
    md.append("| --- | --- | --- |")
    md.append("| 官网结构化 FAQ / 服务页 | 只有简介,没有问答和场景页 | AI 缺少可直接复述的答案素材 |")
    md.append("| 第三方行业报道 / 评测 | 没有外部平台验证 | AI 判断可信度时缺少权威背书 |")
    md.append("| 客户案例 / 业主评价 | 案例没有公开、没有细节 | AI 不敢在“哪家靠谱”里推荐你 |")
    md.append("| 对比 / 避坑 / 方案内容 | 高意向问题没有你的答案 | 场景转化层拿不到新客 |")
    md.append("")
    md.append("---")

    return {
        "module": 4,
        "top_competitors": top,
        # [收口 2026-06-07] 本次同频计算的真实总样本数(监测=evidence 条数 / 诊断兜底=诊断问答数)
        # 渲染分母优先用它 → 只诊断没监测时不再显示 "2/12" 默认分母
        "co_citation_total": total_evidences,
        "declared_competitors": declared_competitors,
        "has_declared": bool(declared_competitors),
        "brief_local_competitors": brief_local,  # CTO-15.16 M1c · brief 候选
        "rendered_md": "\n".join(md),
    }


# ============================================================================
# Module 5 · 机会估算(基于 industry_median · M1b T3)
# ============================================================================

def build_module_5_opportunity(report_data: dict) -> dict[str, Any]:
    """Module 5 · 机会估算 · 基于 industry p50 vs 当前覆盖"""
    industry = report_data.get("industry", "")
    current_score = report_data.get("total_score", 0) or 0
    try:
        from tools.industry_median import get_industry_median
        median = get_industry_median(industry)
    except Exception:
        median = {"p50": 0, "p90": 0}

    # WJ-36 等级名统一到 funnel_score SSOT 6 档(主导/健康/成长/边缘/危急/隐形)
    # 70 = 健康级下界(FUNNEL_LEVEL_META["健康级"].min) · 85 = 主导级下界
    gap_to_p50 = max(0, 70 - current_score)   # 「健康级」门槛 70
    gap_to_p90 = max(0, 85 - current_score)   # 「主导级」门槛 85

    # 脏数据降级(元指令)· industry 为空或杂值 · 不估
    if not industry or industry in ("其他", "未知", ""):
        md = (
            "## 机会解读\n\n"
            "_品牌行业未明确 · 本段需先在资料端补齐行业信息 · 不硬写结论_\n\n---\n"
        )
        return {"module": 5, "skipped": "industry_empty", "rendered_md": md}

    p50 = median.get('p50', 0)
    p90 = median.get('p90', 0)

    # 启发式缺口刻度:
    #   potential_lift_score = min(gap_to_p50, 30)
    #   est_aiq_lift_pct ≈ potential_lift_score * 1.0%~1.8%
    # 仅用于解释当前与健康级之间的可见度差距,不作为收益、周期或执行承诺。
    potential_lift = min(gap_to_p50, 30)
    est_aiq_lift_pct_low = round(potential_lift * 1.0, 1)
    est_aiq_lift_pct_high = round(potential_lift * 1.8, 1)
    has_real_volume = bool(report_data.get("monthly_search_volume"))

    if has_real_volume:
        vol = int(report_data.get("monthly_search_volume", 0))
        roi_block = (
            f"- 月搜索量基线: ~{vol:,} 次\n"
            f"- AI 可见度缺口折算参考: **+{int(vol * est_aiq_lift_pct_low / 100)} ~ "
            f"+{int(vol * est_aiq_lift_pct_high / 100)}** 次/月\n"
            f"  (按 +{est_aiq_lift_pct_low}% ~ +{est_aiq_lift_pct_high}% 可见度缺口刻度折算)\n\n"
        )
    else:
        roi_block = (
            f"- AI 可见度缺口参考: **+{est_aiq_lift_pct_low}% ~ +{est_aiq_lift_pct_high}%**\n"
            f"- _具体引用次数需关键词月搜索量数据 · 当前未配置 · 这里只做缺口刻度说明_\n\n"
        )

    # CTO-15.16 M1c · service_scope 校准提示(只补不算 · 数值仍走 industry_median)
    scope = (report_data.get("service_scope") or "").strip().lower()
    scope_label_map = {
        "local": "本地/区域",
        "national": "全国",
        "hybrid": "混合(本地为主 · 全国延伸)",
    }
    scope_block = ""
    if scope in scope_label_map:
        scope_block = (
            f"_服务范围 brief: **{scope_label_map[scope]}**(影响"
            f"{'城市级' if scope == 'local' else '全国级'}竞品池 · "
            f"本段只按行业基线做解释 · 后续可按 scope 进一步分层校准)_\n\n"
        )

    md = (
        "## 机会解读\n\n"
        + scope_block
        + "**行业基线**:以健康级 70 分、主导级 85 分作为可见度参照线。\n\n"
        f"- 距「健康级」(70 分)还差 **{gap_to_p50} 分**\n"
        f"- 距「主导级」(85 分)还差 **{gap_to_p90} 分**\n\n"
        f"### 可见度缺口解读(启发式)\n\n"
        + roi_block
        + f"### 解读边界\n\n"
        f"- 这部分只说明当前与健康级/主导级之间的差距,不是收益承诺。\n"
        f"- 是否需要进一步沟通,应结合原始 AI 回答、竞品占位和公开信源一起判断。\n"
        f"- 如果行业资料、关键词样本或证据数量不足,本段只能作为方向参考。\n\n"
        f"_公式说明:每提升 1 分约对应 +1.0%~1.8% AI 可见度缺口刻度(行业经验区间 · 待真实数据校准)_\n\n"
        f"---\n"
    )
    return {
        "module": 5,
        "industry_median": median,
        "gap_to_p50": gap_to_p50,
        "gap_to_p90": gap_to_p90,
        "potential_lift_score": potential_lift,
        "est_aiq_lift_pct_low": est_aiq_lift_pct_low,
        "est_aiq_lift_pct_high": est_aiq_lift_pct_high,
        "has_real_volume": has_real_volume,
        "service_scope": scope or None,  # CTO-15.16 M1c
        "rendered_md": md,
    }


# ============================================================================
# Module 6 · 行动优先级 10 项 todos(复用现有 high/medium/ongoing)
# ============================================================================

_ICE_DIM_TO_IMPACT: dict[str, int] = {
    # 影响最大的维度 → impact 8-10 · 中等 5-7 · 小 3-4
    "keyword_coverage": 9,
    "content_quality": 8,
    "engine_penetration": 8,
    "authority": 7,
    "brand_fit": 7,
    "depth": 6,
    "update_frequency": 5,
    "resolved": 5,
    "general": 6,
}

_ICE_KEYWORD_TO_EASE: list[tuple[str, int]] = [
    # 子串 → ease(执行难度倒序 · 易=高分)· 顺序 == 优先匹配
    ("FAQ", 9), ("问答", 9), ("答疑", 9),
    ("更新", 8), ("发布", 8), ("再发布", 8),
    ("权威", 5), ("背书", 5), ("白皮书", 4),
    ("对接", 4), ("竞品", 5), ("合作", 4),
    ("深度", 4), ("案例", 5),
]


def _ice_score_for(item: dict, default_priority: str) -> dict[str, int]:
    """B3 (CTO-15.9 session 3 · 2026-04-25 · M2 §Epic 6 ICE 打分启动版)

    简化 ICE(无 LLM 投票 · 用本地启发):
      - Impact:从 issue/action 文本 + suggestion 维度反推(_ICE_DIM_TO_IMPACT)
      - Confidence:已有数据支撑则 7 · 否则 5 · 来自 P0/P1/P2 优先级反推
      - Ease:从 action 关键词反推(_ICE_KEYWORD_TO_EASE 子串匹配)· 默认 5

    返:{impact, confidence, ease, ice}(ice = impact × confidence × ease · max 1000)
    """
    issue_text = (item.get("issue") or "") + " " + (item.get("action") or "")
    text = issue_text.lower()

    # Impact:维度匹配
    impact = 6  # default
    for dim, score in _ICE_DIM_TO_IMPACT.items():
        if dim in text or dim.replace("_", " ") in text:
            impact = max(impact, score)

    # Confidence:P0=8 / P1=6 / P2=4(数据闸门)
    conf_map = {"P0": 8, "P1": 6, "P2": 4}
    confidence = conf_map.get(default_priority, 5)

    # Ease:关键词查
    ease = 5
    for kw, score in _ICE_KEYWORD_TO_EASE:
        if kw in (item.get("action") or "") or kw in (item.get("issue") or ""):
            ease = score
            break

    ice = impact * confidence * ease
    return {"impact": impact, "confidence": confidence, "ease": ease, "ice": ice}


def _build_action_attribution_md(brand_id: int | None, days: int = 7) -> str:
    """C3.1 (CTO-15.9 session 3 · 2026-04-25 · Codex P1.1e 动作归因)

    本周/本期内容 + 媒体 + 主题包动作 → 哪些 keyword 监测命中提升 / 哪些未起效

    数据源:
      - articles.first_published_at(P0.4)· 本周新发文章
      - media_publications · 本周新发布
      - confirmed_keywords / extra_keywords · 该 brand 监测中的关键词
      - keyword_trend_stats · 检出率近 7 天 vs 前 7 天的对比

    简化版:不算复杂归因 · 列"本周新发布 N 篇 · 命中关键词 X 个 · 检出率 +Y%"
    """
    if not brand_id:
        return ""

    try:
        from db.connection import get_connection as _gc
        from datetime import datetime as _dt, timedelta as _td

        cutoff = (_dt.now() - _td(days=days)).isoformat()

        conn = _gc()
        try:
            cur = conn.cursor()

            # [WP7 cutover 2026-08-17] 原来这两条 SQL 都是 `q.brand_id = %s`,
            # 把该品牌**全部报价**的发布数合成一个数写进周/月报。同品牌两张报价
            # (续费/加词是常态)时,Q1 的成果会出现在 Q2 客户看到的报告里。
            #
            # 改法:按 quote **逐张**投影,报告里也逐张列,不合并。
            # 一个品牌只有一张报价时读起来和以前一样;有两张时读者立刻看得见
            # 是哪张报价的成果 —— 这正是规格 03 §10「不串 quote」要的效果。
            from services.publication_stage_adapters import brand_quote_projections
            from datetime import timezone as _tz

            # 🔴 本段说的是「**本期**新发」,而六阶段投影按定义是**截至 cutoff 的累计**。
            #    直接拿累计数填进"本期新发 N 篇",数字会一路虚高 —— 换算口径而不换
            #    标签,是比读错数更隐蔽的错。
            #    正确做法:同一投影取两个 cutoff(期末 / 期初)相减。
            #    这样"本期新增"仍然是本期的,而两端都是合同口径、都按 quote 隔离。
            _cutoff_dt = _dt.now(_tz.utc)
            _period_start = _cutoff_dt - _td(days=days)
            _end = {p.get("quote_id"): p for p in
                    brand_quote_projections(int(brand_id), cutoff=_cutoff_dt, cursor=cur)}
            _begin = {p.get("quote_id"): p for p in
                      brand_quote_projections(int(brand_id), cutoff=_period_start, cursor=cur)}

            def _stage_count(proj, name):
                return ((proj or {}).get("stages") or {}).get(name, {}).get("count")

            quote_stage_lines = []
            new_pubs_count = 0
            new_articles_count = 0
            for _qid, _proj in _end.items():
                _active = _stage_count(_proj, "published_active")
                _produced = _stage_count(_proj, "produced_ready")
                if _active is None:
                    continue
                _active0 = _stage_count(_begin.get(_qid), "published_active") or 0
                _produced0 = _stage_count(_begin.get(_qid), "produced_ready") or 0
                # 撤稿会让期末小于期初 → 增量为负。报告里"本期新发"不显示负数,
                # 钳到 0;真实的下架事实由期末的 `published_active` 累计值体现。
                new_pubs_count += max(0, int(_active) - int(_active0))
                new_articles_count += max(0, int(_produced or 0) - int(_produced0))
                quote_stage_lines.append({
                    "quote_id": _qid,
                    "published_active": int(_active),
                    "produced_ready": int(_produced or 0),
                    "strictly_attributed": _stage_count(_proj, "strictly_attributed"),
                })

            # 监测关键词数(confirmed + extra)
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM confirmed_keywords ck JOIN quotes q ON ck.quote_id = q.id
                     WHERE q.brand_id = %s AND (ck.is_core IS NOT FALSE)) as confirmed,
                    (SELECT COUNT(*) FROM extra_keywords e JOIN quotes q ON e.quote_id = q.id
                     WHERE q.brand_id = %s AND e.status = 'active') as extra
            """, (brand_id, brand_id))
            row = cur.fetchone()
            kw_total = ((row.get("confirmed") if row else 0) or 0) + ((row.get("extra") if row else 0) or 0)
        finally:
            conn.close()
    except Exception as _ae:
        logger.warning(f"[report_v2 attribution] 拉数据失败: {_ae}")
        return ""

    if new_articles_count == 0 and new_pubs_count == 0:
        return (
            "\n### 动作归因(本期)\n\n"
            "_本期(近 7 天)无新内容/新发布动作 · 检出率变化主要来自历史内容长尾_\n\n"
        )

    # 一个品牌有多张报价时**必须**拆开列。合成一个数会让读者以为"本合同发了 N 篇",
    # 而其中一部分属于另一张报价 —— 那正是 WP7 修掉的串 quote 形态。
    breakdown = ""
    if len(quote_stage_lines) > 1:
        rows = "\n".join(
            f"  - 报价 #{line['quote_id']}:已做好 {line['produced_ready']} 篇 · "
            f"已发布在线 {line['published_active']} 篇"
            + (f" · 已被 AI 引用 {line['strictly_attributed']} 篇"
               if line.get("strictly_attributed") is not None else "")
            for line in quote_stage_lines)
        breakdown = f"- 按报价拆分(本品牌有 {len(quote_stage_lines)} 张报价):\n{rows}\n"

    return (
        "\n### 动作归因(本期)\n\n"
        f"- 本期新发文章:**{new_articles_count}** 篇\n"
        f"- 本期新建媒体发布:**{new_pubs_count}** 条\n"
        f"{breakdown}"
        f"- 当前监测关键词总数:**{kw_total}** 个\n\n"
        f"_后续每条动作对单 keyword 检出率提升的精确归因 · 待 article→keyword 链路打通(P0.4 后续)_\n\n"
    )


def build_module_6_actions(
    report_data: dict,
    brand_id: int | None = None,
    *,
    strata: list[dict] | None = None,
    evidence_total: int = 0,
    report_type: str = "diagnosis",
    audience: str = "internal",
) -> dict[str, Any]:
    """Module 6 · 10 项 todos · 按 ICE 排序(M2 Epic 6 启动版)

    B3:ICE 排序 · 启发式打分
    C3.1 (CTO-15.9 session 3 · 2026-04-25):末尾追加动作归因(P1.1e)
      · brand_id 给定时拉本期文章/发布/关键词数据
      · 不重复 monitoring 数据 · 只展示"做了什么 → 监测到什么"

    CTO-B 2026-04-26 W3 · 老板硬要求 E:行动建议必须具体到客户
      · 在 ICE 表后追加"个性化行动建议"段(action_personalizer)
      · 每条带:对应弱项 / 目标关键词层 / 推荐发布内容(行业 + 城市) / 证据基础 / 30 天验收口径
      · strata + evidence_total 由调用方(assemble_report_v2)从 Module 2/3 传入
    """
    suggestions = report_data.get("suggestions") or {}
    high = suggestions.get("high_priority") or []
    medium = suggestions.get("medium_priority") or []
    ongoing = suggestions.get("ongoing") or []

    todos = []
    for item in high:
        ice = _ice_score_for(item, "P0")
        todos.append({"priority": "P0", "issue": item.get("issue"), "action": item.get("action"), **ice})
    for item in medium:
        ice = _ice_score_for(item, "P1")
        todos.append({"priority": "P1", "issue": item.get("issue"), "action": item.get("action"), **ice})
    for item in ongoing:
        ice = _ice_score_for(item, "P2")
        todos.append({"priority": "P2", "issue": item.get("issue"), "action": item.get("action"), **ice})
    # ICE 降序 · 同 ICE 时 P0/P1/P2 优先级保序
    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    todos.sort(key=lambda t: (-t.get("ice", 0), priority_order.get(t["priority"], 9)))
    todos = todos[:10]

    is_client_diagnosis = (
        (report_type or "diagnosis").lower() == "diagnosis"
        and (audience or "internal").lower() == "client"
    )
    if is_client_diagnosis:
        md = ["## 报告观察清单", ""]
        md.append("_以下只用于理解报告里哪些问题需要核对,不构成后续执行安排。_")
        md.append("")
        if not todos:
            md.append("- 本期未生成明确观察项 · 请优先查看原始 AI 回答、竞品同频和数据边界。")
        else:
            md.append("| 观察点 | 需要核对的证据 | 保守解读 |")
            md.append("| --- | --- | --- |")
            for t in todos[:5]:
                issue = str(t.get("issue") or "有一项报告弱项").replace("|", "/")
                md.append(
                    f"| {issue} | 原始 AI 回答、竞品同频、公开信源是否支持该判断 | "
                    "先确认是否与真实业务一致,再讨论后续沟通重点 |"
                )
        md.append("")
        md.append("---")
        return {
            "module": 6,
            "todos": todos,
            "personalized_actions": [],
            "rendered_md": "\n".join(md),
        }

    md = ["## 行动优先级(Top 10)", ""]
    if not todos:
        md.append("_本期未生成行动项 · 数据不足_")
    else:
        md.append("_排序规则:ICE 评分(影响力 × 置信度 × 易实施)· 大值优先_")
        md.append("")
        md.append("| 序 | 优先级 | 问题 | 建议动作 | ICE | 影响 | 置信 | 易做 |")
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, t in enumerate(todos, start=1):
            md.append(
                f"| {i} | {t['priority']} | {t.get('issue', '')} | {t.get('action', '')} | "
                f"{t.get('ice', 0)} | {t.get('impact', '-')} | {t.get('confidence', '-')} | {t.get('ease', '-')} |"
            )
    md.append("")

    # CTO-B W3 · 个性化行动建议(老板硬要求 E)· 必须接客户行业/城市/竞品/弱项
    personalized: list[dict] = []
    if todos:
        try:
            from services.action_personalizer import (
                personalize_actions, render_personalized_actions_md,
            )
            brand_meta = {
                "name": report_data.get("brand_name") or "",
                "city": report_data.get("city") or "",
                "cities": (report_data.get("diagnosis_data") or {}).get("brand_cities"),
                "industry": report_data.get("industry") or "",
            }
            # 竞品列表(W3 暂从 report_data 取 · Module 4 已拉)
            competitors = report_data.get("declared_competitors") or []
            personalized = personalize_actions(
                todos,
                brand=brand_meta,
                industry=report_data.get("industry") or "",
                strata=strata or [],
                evidence_total=evidence_total,
                competitors=competitors,
            )
            if personalized:
                md.append(render_personalized_actions_md(personalized))
        except Exception as _pe:
            logger.warning(f"[report_v2 module 6] personalize_actions 异常(降级 ICE 表): {_pe}")

    # WJ-37 · 结合代理填的 brief 资料 · 让行动建议落到客户的真实痛点/获客路径/案例上
    # (brief 已由 assemble_report_v2 → _enrich_with_brief 注入 report_data · 这里只读)
    _brief_lines = _brief_context_lines(report_data.get("brief"))
    if _brief_lines:
        md.append("### 结合你填的资料")
        md.append("")
        md.append("_落地动作时优先围绕客户实际情况展开,而不是泛泛而谈:_")
        md.append("")
        md.extend(_brief_lines)
        md.append("")

    # C3.1 · 末尾追加动作归因(brand_id 给定时拉数据)
    attribution_md = _build_action_attribution_md(brand_id, days=7)
    if attribution_md:
        md.append(attribution_md.rstrip())

    md.append("---")

    return {
        "module": 6,
        "todos": todos,
        "personalized_actions": personalized,
        "rendered_md": "\n".join(md),
    }


# ============================================================================
# Module 7 · 30 天计划(3 周 · 每周 3-5 项)
# ============================================================================

def build_module_7_plan_30d(
    report_data: dict,
    todos: list[dict] | None = None,
    *,
    report_type: str = "diagnosis",
) -> dict[str, Any]:
    """Module 7 · 行动计划 · 按 report_type 切版式

    C1.2 (CTO-15.9 session 3 · 2026-04-25 · M2 §Epic 7 + Codex 0424 P1.1f)
      - report_type='diagnosis' → "30 天计划"(原版 · 第 1/2/3-4 周)
      - report_type='weekly' → "下周 7 天计划"(P0 周一 / P1 周三 / P2 周五-日)
      - report_type='monthly' → "下月 4 周计划"(每周一行动主题)
      - report_type='quarterly'/'yearly' → "未来 N 月计划"(粗化版)

    B.5 + B3 复用:行业 4 模板 + 空段降级
    """
    todos = todos or []
    industry = (report_data or {}).get("industry", "")
    week1 = [t for t in todos if t.get("priority") == "P0"][:5]
    week2 = [t for t in todos if t.get("priority") == "P1"][:5]
    week3 = [t for t in todos if t.get("priority") == "P2"][:5]

    # industry → 行业模板分类(粗略 子串匹配)· 用于空段降级建议
    def _industry_template(ind: str) -> str:
        ind = (ind or "").strip()
        if not ind:
            return "general"
        for kw in ("家装", "装修", "美容", "医美", "餐饮", "婚", "摄影", "教育", "培训", "律", "口腔", "医院"):
            if kw in ind:
                return "local_service"
        for kw in ("电商", "美妆", "零食", "服装"):
            if kw in ind:
                return "consumer"
        for kw in ("SaaS", "CRM", "ERP", "软件", "外贸"):
            if kw in ind:
                return "B2B"
        for kw in ("机械", "化工", "制造", "设备", "工业"):
            if kw in ind:
                return "industrial"
        return "general"

    template = _industry_template(industry)
    template_label = {
        "local_service": "本地服务",
        "consumer": "消费品",
        "B2B": "B2B",
        "industrial": "工业采购",
        "general": "通用",
    }[template]

    # 空段降级建议(按行业模板)
    fallback_p0 = {
        "local_service": "- 制作 1 篇本地服务说明(含城市/区县 + 营业时间 + 价格示例)",
        "consumer": "- 整理 3 条产品 SKU 详情页(规格 + 卖点 + 适用场景)",
        "B2B": "- 写 1 篇行业方案白皮书摘要(问题 + 方案 + 客户案例)",
        "industrial": "- 整理设备参数表(型号 + 性能 + 应用工况)",
        "general": "- 制作 1 篇品牌主页核心介绍(WHO/WHAT/HOW)",
    }[template]

    # C1.2 · 标题 + 时段切版式(P1.1f 周报"下周 7 天"/月报"下月 4 周"/末报"未来 N 月")
    rt = (report_type or "diagnosis").lower()
    if rt == "diagnosis":
        md = [
            "## 报告解读提纲",
            "",
            "_本段只帮助阅读报告,不替代后续商业沟通。_",
            "",
            "### 1. 先确认 AI 是否认识品牌",
            "",
            "- 看品牌认知层命中情况,判断客户主动搜索品牌时 AI 能否说清楚品牌是谁。",
            "- 如果本层样本不足,只做方向判断,不要把单次回答当成稳定结论。",
            "",
            "### 2. 再看新客决策入口是否断档",
            "",
            "- 看决策获客层和场景转化层,判断客户还不认识品牌时 AI 是否会把品牌纳入候选。",
            "- 同时核对 AI 提到的竞品和引用来源,理解“谁占了答案位置”。",
            "",
            "### 3. 最后核对证据和数据边界",
            "",
            "- A 级证据适合支持明确判断;B 级证据只能说明相关倾向;C 级证据只作为推断线索。",
            "- 若关键词、行业资料或原始问答不足,报告应保守解读,不做确定性判断。",
            "",
            "---",
        ]
        return {
            "module": 7,
            "report_type": rt,
            "industry_template": template,
            "week1": week1, "week2": week2, "week3": week3,
            "rendered_md": "\n".join(md),
        }

    if rt == "weekly":
        plan_title = "## 下周 7 天计划"
        section_titles = (
            "### 周一 · P0 关键修",
            "### 周三 · P1 能力补齐",
            "### 周五-日 · P2 持续优化 + 复盘",
        )
    elif rt == "monthly":
        plan_title = "## 下月 4 周计划"
        section_titles = (
            "### 第 1 周(本月初)· P0 关键修",
            "### 第 2 周 · P1 能力补齐",
            "### 第 3-4 周 · P2 持续优化 + 月末复盘",
        )
    elif rt == "quarterly":
        plan_title = "## 下季 3 月计划"
        section_titles = (
            "### 月 1 · 关键修(P0 主导)",
            "### 月 2 · 能力补齐(P1 主导)",
            "### 月 3 · 持续优化 + 季度复盘",
        )
    elif rt == "yearly":
        plan_title = "## 未来 4 季度计划"
        section_titles = (
            "### Q1 · 关键修(P0 主导)",
            "### Q2 · 能力补齐(P1 主导)",
            "### Q3-Q4 · 持续优化 + 年度复盘",
        )
    else:
        plan_title = "## 30 天行动计划"
        section_titles = (
            "### 第 1 周 · 关键修(P0 优先)",
            "### 第 2 周 · 能力补齐(P1 加固)",
            "### 第 3-4 周 · 持续优化(P2 + 监测复盘)",
        )

    md = [plan_title, ""]
    md.append(f"_行业模板: {template_label} · 报告类型: {rt}_")
    md.append("")

    md.append(section_titles[0])
    if week1:
        for t in week1:
            md.append(f"- {t.get('action', '')}")
    else:
        md.append(fallback_p0)
        md.append("- _本期无 P0 阻塞项 · 上述为行业基础内容建议_")
    md.append("")

    md.append(section_titles[1])
    if week2:
        for t in week2:
            md.append(f"- {t.get('action', '')}")
    else:
        md.append("- _无 P1 项 · 可推进首段未完动作 + 扩大复测覆盖度_")
    md.append("")

    md.append(section_titles[2])
    if week3:
        for t in week3:
            md.append(f"- {t.get('action', '')}")
    else:
        md.append("- 持续监测 4 引擎数据 · 复盘前段动作效果")
        md.append("- 高频问题 1-2 条针对性补内容")
    md.append("")

    if rt == "diagnosis":
        md.append("### 30 天验收标准")
        md.append("")
        md.append("| 时间点 | 要看到什么 | 不达标时怎么处理 |")
        md.append("| --- | --- | --- |")
        md.append("| 第 7 天 | 品牌基础页 / FAQ / 公开案例至少完成 1 组 | 先把问题说清楚,不要急着扩大关键词 |")
        md.append("| 第 15 天 | 4 引擎复测中品牌认知层稳定命中 | 修正品牌名、服务范围和公开资料一致性 |")
        md.append("| 第 30 天 | 决策获客层开始出现新增命中或引用源 | 围绕未命中的问题制作专题内容和第三方信源 |")
        md.append("")

    # C1.4 · 续费埋点 P1.1g · 周报/月报/季报/年报末尾加 "下期动作 N 项"标语 · 不影响诊断版
    if rt in ("weekly", "monthly", "quarterly", "yearly"):
        next_period = {"weekly": "下周", "monthly": "下月", "quarterly": "下季", "yearly": "下年度"}[rt]
        total_planned = len(week1) + len(week2) + len(week3)
        md.append(f"_{next_period}计划共 **{total_planned}** 项行动 · 完成率将进 Module 8 续费建议_")
        md.append("")

    md.append("---")

    return {
        "module": 7,
        "report_type": rt,
        "industry_template": template,
        "week1": week1, "week2": week2, "week3": week3,
        "rendered_md": "\n".join(md),
    }


# ============================================================================
# Module 8 · 方案承接(呼应 P0.5c CTA)
# ============================================================================

def build_module_8_offer(
    report_data: dict,
    brand_id: int = None,
    *,
    report_type: str = "diagnosis",
    audience: str = "internal",
    branding: Optional[dict] = None,
) -> dict[str, Any]:
    """Module 8 · 接下来怎么办(CTO-G 2026-04-27 重写)

    替换旧的多 CTA(下单 + 套餐 + 续费 等)为单一规划师咨询
    audience='client' / 'internal' 都用同一份 CTA 文案 · 不再做差异(老板红线:CTA 不能两套)
    internal 版可在末尾追加代理协同提示(独立段)· 但主 CTA 一致
    branding: v3.6 白标 brand dict(resolve_branding_context surface=customer)· None → 平台默认。
    """
    is_client = (audience or "").lower() == "client"
    rt = (report_type or "diagnosis").lower()
    if rt == "diagnosis":
        md = (
            "## 报告局限说明\n\n"
            "- AI 引擎回答会随模型更新和实时检索变化,本报告代表本次采样窗口的结果。\n"
            "- 部分平台受引用字段限制,有些证据只能用于判断可见度,不能完整还原引用源。\n"
            "- 样本不足、行业资料不足或关键词覆盖不足时,对应结论应保守理解。\n\n"
            "---\n\n"
            "## 如需进一步沟通,建议围绕这份报告逐项解读\n\n"
            "后续沟通主要用于厘清:\n\n"
            "- 哪些结论有直接证据支持。\n"
            "- 哪些结论只是趋势判断,需要保守理解。\n"
            "- 哪些结论因为数据不足,暂时不能下确定判断。\n"
            "- 客户自己的业务范围、目标客户和现有公开资料是否与报告判断一致。\n\n"
            "这份报告更适合作为一次客观诊断快照,而不是单独看分数或单独看某条 AI 回答。\n\n"
        )
        _brief_recap = _brief_context_lines(report_data.get("brief"))
        if _brief_recap:
            md += (
                "### 可同步核对的已填资料\n\n"
                + "\n".join(_brief_recap)
                + "\n\n"
            )
        if not is_client:
            md += (
                "_代理视角:建议围绕证据、趋势和数据边界与客户沟通;具体商业沟通可另行展开,"
                "不要把报告解读写成效果承诺。_\n\n"
            )
        md += "---\n"
        return {
            "module": 8,
            "report_type": rt,
            "audience": "client" if is_client else "internal",
            "rendered_md": md,
        }

    # [2026-06-06 白牌留白 · 老板拍] 客户面无白标 → CTA 不带平台前缀(中性"GEO 规划师");internal/agent → 平台兜底
    from services.public_whitelabel import is_real_agent_brand
    # [P1 Codex finding2] 平台默认 dict 误传 → 视作无白标(否则 client 面 CTA 露平台名)
    _wl_company = (branding.get("company_name") or "").strip() if is_real_agent_brand(branding) else ""
    if _wl_company:
        _planner_brand = _wl_company
    elif is_client:
        _planner_brand = ""  # 客户面无白标 → 留白(不露平台名)
    else:
        _planner_brand = _rw_brand_company(None)

    # 主 CTA · 单一动作:预约规划师
    md = (
        "## 报告局限说明\n\n"
        "- AI 引擎回答会随模型更新和实时检索变化,本报告代表本次采样窗口的结果。\n"
        "- 部分 AI 平台不提供完整的结构化引用字段；此类证据只能做可见度判断，不能完整还原引用源。\n"
        "- 建议 30 天后使用同一组问题复测,对比品牌认知层、决策获客层和场景转化层的变化。\n\n"
        "---\n\n"
        "## 接下来怎么办\n\n"
        f"> **🎯 和 {_planner_brand + ' ' if _planner_brand else ''}GEO 规划师 1 对 1 聊 30 分钟**\n"
        ">\n"
        "> 规划师会带着这份诊断报告 + 你公司的真实情况"
        "(业务范围、当前预算、目标客户、现有资料),"
        "给你一份**针对你的方案**:\n"
        ">\n"
        "> - 应该先打哪一层(决策获客层 vs 场景转化层)\n"
        "> - 哪些关键词最该抢\n"
        "> - 需要什么内容资产 · 优先级 + 节奏\n"
        "> - 预期 30 / 60 / 90 天 AI 搜索可见度与答案采纳变化区间\n"
        "> - 投入测算\n"
        ">\n"
        "> 👉 **[预约 GEO 规划师 30 分钟咨询]** · 免费 · 带这份报告进会\n\n"
    )

    # WJ-37 · 承接段补客户已填资料速览 · 让"针对你的方案"不是空话
    # (brief 已由 assemble_report_v2 → _enrich_with_brief 注入 report_data · 这里只读)
    _brief_recap = _brief_context_lines(report_data.get("brief"))
    if _brief_recap:
        md += (
            "**规划师会带着你已经填好的这些资料进会:**\n\n"
            + "\n".join(_brief_recap)
            + "\n\n"
        )

    # internal 版加代理协同提示(不影响主 CTA · 独立小段)
    if not is_client:
        md += (
            "---\n\n"
            "_(代理视角:你可以把这份报告发给客户 · 然后约规划师一起进 30 分钟会议 · "
            "三方沟通效率最高 · 帮你提高签单率)_\n\n"
        )

    md += "---\n"

    return {
        "module": 8,
        "report_type": rt,
        "audience": "client" if is_client else "internal",
        "rendered_md": md,
    }


# ============================================================================
# Module 3_authority · Source Authority Pack(权威背书证据 · 独立解释层)
# ============================================================================
# 🔴 endorsement_score 绝不进 5 维评分 SSOT 主分(与 geo_scope_scorer.authority_score 物理隔离)。
# 本模块只产展示数据 + markdown,失败降级,绝不打断报告装配。

# 客户面"说人话"映射:绝不出现 tier1/brand_direct/citation/source_authority 等工程字段
_AUTH_TYPE_PLAIN = {
    "tier1_national": "权威媒体/政府",
    "structured_encyclopedia": "百科",
    "tier2_portal_vertical": "门户/行业网站",
    "tier3_small_media_wemedia": "普通网站/自媒体",
    "risk_low_quality": "低可信来源",
}
_AUTH_SITUATION_PLAIN = {
    "real_ai_citation": "AI 实际引用过",
    "search_brand_direct": "搜索时提到",
    "industry_reference": "行业参考",
}
# 客户面禁词(工程字段)· 渲染自检兜底用
_AUTH_BANNED_TERMS = (
    "brand_direct", "authority_score", "tier1", "tier2", "tier3",
    "jsonb", "citation", "source_authority", "domain_tier", "endorsement_score",
)


def _authority_grouped_counts(tc: dict) -> dict:
    """五档归并成客户能听懂的三组 + 低可信。"""
    tc = tc or {}
    return {
        "authority": tc.get("tier1_national", 0) + tc.get("structured_encyclopedia", 0),
        "portal": tc.get("tier2_portal_vertical", 0),
        "common": tc.get("tier3_small_media_wemedia", 0),
        "risk": tc.get("risk_low_quality", 0),
    }


def _authority_plain_conclusion(pack: dict) -> str:
    """一句话人话结论(不做效果承诺)。"""
    g = _authority_grouped_counts(pack.get("tier_counts") or {})
    total = g["authority"] + g["portal"] + g["common"] + g["risk"]
    if total == 0:
        return "目前还没有足够的来源数据,暂时看不出权威背书情况。"
    if g["authority"] == 0 and g["portal"] == 0:
        return "AI 现在主要引用的是普通网站和自媒体内容,权威媒体、百科类的背书还不够。"
    if g["authority"] == 0 and g["portal"] > 0:
        return "客户品牌已经有一些门户、行业网站提到,但能让 AI 放心引用的权威媒体、百科类来源还偏少。"
    if g["authority"] > 0 and (g["portal"] + g["common"]) > g["authority"]:
        return "已经有权威来源提到品牌,但更多还是普通网站,权威背书还不够集中。"
    if g["authority"] > 0:
        return "品牌已经获得权威媒体或百科类来源的背书,基础不错,可以继续巩固。"
    return "目前更像是“有内容”,还不是“有权威背书”。"


def _authority_plain_suggestion(pack: dict) -> tuple:
    """返回(现在的问题, 对客户意味着, 下一步建议)· 不做效果承诺。"""
    g = _authority_grouped_counts(pack.get("tier_counts") or {})
    if g["authority"] == 0:
        problem = "权威媒体、政府或百科类的可引用来源偏少,AI 能看到的多是普通网站。"
    elif (g["portal"] + g["common"]) > g["authority"]:
        problem = "已有少量权威来源,但整体仍以普通网站为主,权威背书不够集中。"
    else:
        problem = "权威来源已具备,可继续扩大覆盖面与内容新鲜度。"
    meaning = "AI 在回答时更倾向引用它认为可靠的来源,普通网站不容易被当作可靠依据。"
    suggestion = "建议补充权威媒体报道、行业网站或百科类内容,让 AI 更容易找到关于品牌的可靠资料。"
    return problem, meaning, suggestion


_AUTH_PLAIN_NOTE = "说明:部分 AI 平台暂不提供引用来源,以上基于能获取来源的平台与公开搜索核验,不代表全部 AI 平台。"


def _render_authority_md(pack: dict) -> str:
    """SAP → 客户能看懂的 markdown(说人话 · 无工程字段 · 不做效果承诺)。
    供 full_markdown 拼接 / PDF / v3 fallback;HTML 端可用 _md_lite_v2 转表 + 折叠。
    """
    g = _authority_grouped_counts(pack.get("tier_counts") or {})
    total = g["authority"] + g["portal"] + g["common"] + g["risk"]
    md = ["## 权威背书情况", ""]

    if total == 0:
        md.append(_authority_plain_conclusion(pack))
        md.append("")
        md.append("建议先完成内容投放与持续监测,积累可被 AI 引用的来源后再评估。")
        md.append("")
        md.append(f"_{_AUTH_PLAIN_NOTE}_")
        md.append("")
        md.append("---")
        return "\n".join(md)

    # 1) 一句话结论(首屏)
    md.append(_authority_plain_conclusion(pack))
    md.append("")
    # 背书强度数字 · 次要展示 · 明确与诊断总分隔离(老板拍板:展示但不作主评分)
    score = pack.get("endorsement_score", 0)
    smax = pack.get("score_max", 20)
    md.append(f"_(权威背书强度 {score}/{smax} · 仅供参考 · 不计入诊断总分)_")
    md.append("")
    # 2) 三类来源数量(首屏)
    parts = [
        f"权威媒体/政府/百科 {g['authority']} 个",
        f"门户/行业网站 {g['portal']} 个",
        f"普通网站/自媒体 {g['common']} 个",
    ]
    if g["risk"] > 0:
        parts.append(f"低可信来源 {g['risk']} 个")
    md.append("**来源构成**:" + " · ".join(parts))
    md.append("")

    # 3) 来源明细(详情 · HTML 端折叠到“查看证据”)
    top_sources = pack.get("top_sources") or []
    if top_sources:
        md.append("### 来源明细")
        md.append("| 来源 | 类型 | 来源情况 |")
        md.append("| --- | --- | --- |")
        for s in top_sources[:8]:
            name = (s.get("display_name") or s.get("domain") or "未知来源").replace("|", "/")
            type_plain = _AUTH_TYPE_PLAIN.get(s.get("tier"), "普通网站/自媒体")
            sit_plain = _AUTH_SITUATION_PLAIN.get(s.get("evidence_class"), "")
            md.append(f"| {name} | {type_plain} | {sit_plain} |")
        md.append("")

    # 4) 给你的建议(王姐可直接讲客户)
    problem, meaning, suggestion = _authority_plain_suggestion(pack)
    md.append("### 给你的建议")
    md.append(f"现在的问题是:{problem}")
    md.append("")
    md.append(f"对客户意味着:{meaning}")
    md.append("")
    md.append(f"下一步建议:{suggestion}")
    md.append("")
    md.append(f"_{_AUTH_PLAIN_NOTE}_")
    md.append("")
    md.append("---")
    return "\n".join(md)


def build_module_3_authority(report_data: dict, brand_id: int, days: int = 30) -> dict[str, Any]:
    """Module 3_authority · 权威背书证据(Source Authority Pack)

    独立解释层:展示引用信源等级分布 + 权威背书分(endorsement_score 0-20,绝不进主分)+
    区分"真实 AI 引用 / 搜索品牌直引 / 行业参考"。失败降级(available=False · rendered_md=""),
    绝不打断报告装配。无 mhz 媒体候选(二期)。
    """
    try:
        from services.source_authority_analyzer import aggregate_source_authority
    except Exception as e:
        logger.warning(f"[report_v2] source_authority_analyzer import 失败: {e}")
        return {"module": "3_authority", "available": False, "rendered_md": ""}

    try:
        diagnosis_data = report_data.get("diagnosis_data") or {}
        web_search_data = diagnosis_data.get("web_search_data") or {}
        # [Phase1-C 2026-06-07] 把诊断自己的 4 引擎引用喂进权威背书聚合(此前只喂 monitoring + brand_direct,
        #   导致"只诊断没监测"的品牌权威背书必空)。真实路径已核 = diagnosis_data.ai_visibility_data.detail_table
        #   (与本文件 build_module_3 同源 · 见 line 836-838),不拍路径。
        diagnosis_detail_table = (diagnosis_data.get("ai_visibility_data") or {}).get("detail_table") or []
        pack = aggregate_source_authority(
            brand_id=brand_id,
            web_search_data=web_search_data,
            days=days,
            diagnosis_detail_table=diagnosis_detail_table,
        )
        pack["available"] = True
        # 预存"说人话"文本 + 分组数量,供 HTML 渲染端零跨模块依赖直接读(decision/普通v2/v3)
        problem, meaning, next_step = _authority_plain_suggestion(pack)
        plain_sources = [
            {
                "name": (s.get("display_name") or s.get("domain") or "未知来源"),
                "type": _AUTH_TYPE_PLAIN.get(s.get("tier"), "普通网站/自媒体"),
                "situation": _AUTH_SITUATION_PLAIN.get(s.get("evidence_class"), ""),
            }
            for s in (pack.get("top_sources") or [])[:8]
        ]
        pack["plain"] = {
            "conclusion": _authority_plain_conclusion(pack),
            "grouped_counts": _authority_grouped_counts(pack.get("tier_counts") or {}),
            "suggestion": {"problem": problem, "meaning": meaning, "next_step": next_step},
            "note": _AUTH_PLAIN_NOTE,
            "score": pack.get("endorsement_score", 0),
            "score_max": pack.get("score_max", 20),
            "sources": plain_sources,
        }
        pack["rendered_md"] = _render_authority_md(pack)
        return pack
    except Exception as e:
        logger.warning(f"[report_v2] build_module_3_authority 异常: {e}")
        return {"module": "3_authority", "available": False, "rendered_md": ""}


def _landscape_activity_label(rate: float) -> str:
    """[B2-1] 行业引用率 → 人话活跃度带(禁裸露 SOV 百分比)。rate 为 0..1。"""
    try:
        r = float(rate or 0)
    except (TypeError, ValueError):
        r = 0.0
    if r >= 0.5:
        return "高频引用"
    if r >= 0.25:
        return "中频引用"
    if r > 0:
        return "偶有引用"
    return "暂无引用"


def build_module_industry_landscape(report_data: dict) -> dict[str, Any]:
    """[B2-1] 「你所在行业的 AI 引用格局」段(读侧 · 零风险)。

    读飞轮聚合(get_industry_engine_scores)给出该行业当前被 AI 引用最多的内容平台 + 各引擎活跃度,
    让报告从"我们测了你"升级为"我们掌握你所在行业的真实引用地图"。
    - 不改评分、不改收费、不动既有段落结构(纯新增段落)。
    - 面向非代理:禁裸露 SOV 百分比,用高频/中频/偶发话术带。
    - 无数据行业优雅缺省(整段隐藏,禁空表格)。
    数字口径与飞轮页一致(同一 get_industry_engine_scores 函数)。
    """
    industry = (report_data.get("industry") or "").strip()
    if not industry or industry in ("其他", "未知", ""):
        return {"module": "industry_landscape", "skipped": "industry_empty", "rendered_md": ""}
    try:
        from services.placement_service import get_placement_service
        scores = get_placement_service().get_industry_engine_scores(industry) or {}
    except Exception as e:
        logger.warning(f"[report_v2] build_module_industry_landscape 异常: {e}")
        return {"module": "industry_landscape", "skipped": "error", "rendered_md": ""}

    if not scores:
        # 无数据行业:整段隐藏(不出破损空表格)
        return {"module": "industry_landscape", "skipped": "no_data", "rendered_md": ""}

    # 取综合引用分 top 平台(与飞轮页同口径:score 降序)
    ranked = sorted(scores.items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
    top = [(name, d) for name, d in ranked if (d.get("citation_rates") or d.get("score"))][:6]
    if not top:
        return {"module": "industry_landscape", "skipped": "no_data", "rendered_md": ""}

    lines = [
        "## 你所在行业的 AI 引用格局\n",
        f"我们持续监测【{industry}】行业在 AI 搜索里的真实引用情况。"
        "下面是这个行业当前被 AI 引用最多的内容平台，反映 AI 更倾向从哪些平台取材：\n",
    ]
    for name, d in top:
        ptype = (d.get("type") or "").strip()
        head = f"- **{name}**" + (f"（{ptype}）" if ptype else "")
        rates = d.get("citation_rates") or {}
        # 只展示有引用的引擎,按活跃度排序;人话带,不露百分比
        eng_bits = []
        for eng, rate in sorted(rates.items(), key=lambda kv: kv[1], reverse=True):
            label = _landscape_activity_label(rate)
            if label != "暂无引用":
                eng_bits.append(f"{eng}·{label}")
        if eng_bits:
            head += "：" + " ／ ".join(eng_bits)
        lines.append(head)
    lines.append(
        f"\n_说明：这是【{industry}】行业的整体引用地图，"
        "反映 AI 当前更常从哪些平台取材。你的品牌要提升 AI 可见度，"
        "优先在这些高引用平台建设优质、可被引用的内容会更高效。_\n\n---\n"
    )
    return {"module": "industry_landscape", "available": True, "rendered_md": "\n".join(lines)}


# ============================================================================
# 主 assembler
# ============================================================================

def assemble_report_v2(
    report_data: dict,
    brand_id: int,
    *,
    report_type: str = "diagnosis",
    audience: str = "internal",
    branding: Optional[dict] = None,
) -> dict[str, Any]:
    """M2 · 8 模块 Assembly 主入口

    C1.2/C1.3/C1.4 (CTO-15.9 session 3 · 2026-04-25 · M2 §Epic 7-8 + Codex P1.1f/g/j)
      - report_type ∈ {diagnosis/weekly/monthly/quarterly/yearly}
        · 切 Module 7 时段标题 + Module 8 续费 CTA 强度
      - audience ∈ {client/internal}
        · client 简化:Module 8 隐藏代理利润提示
        · internal:全量(代理审用)

    Args:
        report_data: diagnosis 主流程产出 dict · 含 total_score / scores / suggestions / brand_name / industry
        brand_id: 品牌 ID · Module 3 evidence 抽取需要
        report_type: 'diagnosis'(默认)/ 'weekly' / 'monthly' / 'quarterly' / 'yearly'
        audience: 'internal'(默认 · 代理审)/ 'client'(客户门户展示)

    Returns:
        {
          "modules": { "1": {...}, "2": {...}, ..., "8": {...} },
          "full_markdown": "所有 module rendered_md 合并",
          "version": "v2",
          "evidence_count": {"A": N, "B": N, "C": N},
          "report_type": rt,
          "audience": ad,
        }
    """
    # CTO-15.16 M1c · 在分发模块前做 brief 注入 · 让 1/4/5 模块统一拿到 brief/service_scope/local_competitors
    # brief 由 get_effective_brief_by_brand 强制 confirmed=TRUE 守门 · 未确认 brief 不污染报告
    report_data = _enrich_with_brief(report_data, brand_id)

    modules: dict[str, dict[str, Any]] = {}

    # Module 1-2 · 无外部依赖
    # 客户版报告必须先给业务结论,再解释输入完整度。否则客户第一眼看到内部检查表,不利于成交。
    modules["1"] = build_module_1_cover(report_data)
    modules["0"] = build_module_0_completeness(report_data)
    modules["2"] = build_module_2_radar(report_data)

    # Module 3 · 需要 brand_id
    modules["3"] = build_module_3_evidence(report_data, brand_id=brand_id)
    modules["3_raw"] = build_module_3_raw_ai_appendix(report_data)
    # E3/E4 · 答案实体消费接线 SPEC · additive 只读模块 · fail-safe 包裹绝不打断主链
    #   E3 报价竞争位:AI 推荐名单 + 客户竞争位 · E4 诊断体检交叉:逐引擎命中 vs 推荐名单 pivot
    #   都只读 ai_visibility_data.detail_table/engine_stats · 不重跑引擎 · 不碰算价
    try:
        modules["3_competition"] = build_module_3_competition(
            report_data, audience=audience, brand_id=brand_id
        )
    except Exception as _e3_err:
        logger.warning(f"[report_v2] E3 competition 模块降级: {_e3_err}")
        modules["3_competition"] = {"module": "3_competition", "available": False, "rendered_md": ""}
    try:
        modules["e4"] = build_module_e4_health_cross(report_data, audience=audience)
    except Exception as _e4_err:
        logger.warning(f"[report_v2] E4 health-cross 模块降级: {_e4_err}")
        modules["e4"] = {"module": "e4", "available": False, "rendered_md": ""}
    # SAP · 权威背书证据(独立解释层 · endorsement_score 绝不进 5 维主分)· 失败降级不打断
    modules["3_authority"] = build_module_3_authority(report_data, brand_id=brand_id)

    # Module 1_interpretation · 客观解读层
    # 依赖 Module 3 的 evidence_total,但在 markdown 中排在 Module 1 后面。
    modules["1_interpretation"] = build_module_1_interpretation(report_data, modules=modules)

    # Module 4 · 依赖 Module 3 evidences · C2.2 · 拉 brand 建档真竞品
    modules["4"] = build_module_4_competitors(
        report_data,
        evidences=modules["3"].get("evidences", []),
        brand_id=brand_id,
    )

    # Module 5 · 依赖 industry_median
    modules["5"] = build_module_5_opportunity(report_data)

    # [B2-1] 行业 AI 引用格局(读侧 · 零风险 · 无数据行业整段隐藏)
    modules["industry_landscape"] = build_module_industry_landscape(report_data)

    # CTO-B W3 · Module 6 个性化建议依赖 Module 2 strata + Module 3 evidence_total + Module 4 真竞品
    _strata_for_module6 = modules["2"].get("keyword_strata") or []
    _evidence_total_for_module6 = int(modules["3"].get("evidence_total") or 0)
    # 把 declared_competitors 注入 report_data 让 Module 6 内部可读(action_personalizer 走)
    _module6_data = dict(report_data)
    _module6_data["declared_competitors"] = modules["4"].get("declared_competitors") or []
    modules["6"] = build_module_6_actions(
        _module6_data,
        brand_id=brand_id,
        strata=_strata_for_module6,
        evidence_total=_evidence_total_for_module6,
        report_type=report_type,
        audience=audience,
    )

    # Module 7 · 依赖 Module 6 todos · C1.2 切版式
    modules["7"] = build_module_7_plan_30d(
        report_data,
        todos=modules["6"].get("todos"),
        report_type=report_type,
    )

    # Module 8 · CTA · B3 portal_token + C1.3 audience + C1.4 续费埋点
    modules["8"] = build_module_8_offer(
        report_data,
        brand_id=brand_id,
        report_type=report_type,
        audience=audience,
        branding=branding,
    )

    # 合 markdown · 先结论,再输入完整度,然后进入评分/证据/行动。
    # E3/E4 additive 插入:'3_raw' 之后、'3_authority' 之前 · 既有键相对顺序不变(只增不改)
    module_order = ["1", "1_interpretation", "2", "3", "3_raw", "3_competition", "e4", "3_authority", "4", "5", "industry_landscape", "6", "7", "8", "0"]
    full_md = "\n".join(modules[key].get("rendered_md", "") for key in module_order)

    # B.5 (CTO-15.9 session 3 · 2026-04-25) · P1.1h 禁词 lint 兜底
    # 命中只 warn · 不 block(老板批 · 报告先出 · 后期再 retry/人工)
    lint_warnings: list[str] = []
    try:
        from utils.banned_words import lint_report_text, format_lint_warning
        # CTO-15.16 round2 Task C · strip_quoted=True · 跳过 backtick / 「」 引用容器 ·
        # 报告 v2 evidence 引用 / 第三方文章标题 / 用户原问 都在引用块里 · 不该判违法
        lint_result = lint_report_text(full_md, strip_quoted=True)
        if lint_result.has_any:
            warning_str = format_lint_warning(lint_result)
            lint_warnings.append(warning_str)
            logger.warning(f"[report_v2] lint 命中 · brand={brand_id} · {warning_str}")
    except Exception as e:
        logger.warning(f"[report_v2] lint 异常: {e}")

    return {
        "version": "v2",
        "modules": modules,
        "full_markdown": full_md,
        "evidence_count": modules["3"].get("evidence_count", {"A": 0, "B": 0, "C": 0}),
        "report_type": report_type,
        "audience": audience,
        "lint_warnings": lint_warnings,  # 空数组 = 干净 · 非空 = 命中(运营/广告法/无支撑)
        # CTO-15.16 M1c · 报告 brief 消费观测点 · 0/0 时说明 brief 没起作用(完整度未达 confirm)
        "brief_consumption": {
            "has_brief": bool(report_data.get("brief")),
            "service_scope": report_data.get("service_scope"),
            "local_competitors_count": len(report_data.get("local_competitors") or []),
            "brief_local_competitors_in_module4": len(
                modules["4"].get("brief_local_competitors") or []
            ),
            "differentiation_in_cover": bool(modules["1"].get("differentiation_text")),
            # WJ-37 · 新增 brief 字段消费观测 · 非 0 说明客户填的资料已织入结论/行动/承接
            "brief_context_lines": len(_brief_context_lines(report_data.get("brief"))),
        },
    }
