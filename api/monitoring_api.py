"""
监测API接口 v2.0
提供词条管理、监测执行、结果查询、配置管理、趋势统计、Token消耗等API
"""

import asyncio
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import re  # [R4 2026-08-04] summarize_monitoring_plan_error 的脱敏模式在模块级编译
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from fastapi import HTTPException  # [CTO-15.23 2026-05-05] catch billing 402
from services import governance_alerts as _gov_alerts  # [§13 rollout] SSE 告警机器合同

from db.monitoring_db import (
    get_connection,  # 添加此行
    init_monitoring_tables,
    get_monitoring_config,
    update_monitoring_config,
    add_keyword,
    batch_add_keywords,
    get_keywords,
    get_keyword_by_id,
    update_keyword_status,
    delete_keyword,
    get_keyword_stats,
    create_monitoring_task,
    get_task,
    get_tasks,
    get_task_results,
    # 新增导入
    get_paid_clients,
    get_client_keywords,
    get_keyword_trend,
    get_token_usage_summary,
    # Phase 1.6 新增
    generate_client_token,
    verify_client_token,
    get_client_token,
    add_publication,
    get_publications,
    get_publication_stats,
    log_operation,
    get_operation_logs,
    # 报告功能
    save_report,
    get_reports,
    # 平台权重
    get_keywords_for_monitoring,
    quote_service_anchor_condition_sql,
    DEFAULT_MONITORING_PLATFORMS,
    MONITORING_CLASSIC4_ORDER,
    PLATFORM_WEIGHTS,
    PLATFORM_CANONICAL_ORDER,
    normalize_active_platform_weights,
    normalize_monitoring_platform,
    get_saved_platform_weights,
    save_platform_weights,
    load_platform_weights_from_db
)

from tools.monitoring.batch_monitor import run_client_monitoring, PlatformAdapter


# ==========================================
# 操作日志归属辅助
# ==========================================

def _as_int_or_none(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _resolve_operation_brand_id(
    *,
    brand_id: int = None,
    client_id: str = None,
    quote_id: int = None,
    keyword_id: int = None,
    source: str = None,
) -> Optional[int]:
    """为 operation_logs 补齐 brand_id。

    只从固定表/固定字段反查，避免日志维度继续用 operator_id 混充 brand_id。
    """
    bid = _as_int_or_none(brand_id)
    if bid is not None:
        return bid

    qid = _as_int_or_none(quote_id if quote_id is not None else client_id)
    if qid is not None:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT brand_id FROM quotes WHERE id = %s", (qid,))
            row = cursor.fetchone()
            return _as_int_or_none(row["brand_id"]) if row else None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    kid = _as_int_or_none(keyword_id)
    if kid is None:
        return None

    tables = ["extra_keywords"] if source == "extra" else ["client_keywords", "extra_keywords"]
    conn = get_connection()
    try:
        cursor = conn.cursor()
        for table in tables:
            cursor.execute(
                f"""
                SELECT COALESCE(k.brand_id, q.brand_id) AS brand_id
                FROM {table} k
                LEFT JOIN quotes q ON q.id = k.quote_id
                WHERE k.id = %s
                LIMIT 1
                """,
                (kid,),
            )
            row = cursor.fetchone()
            bid = _as_int_or_none(row["brand_id"]) if row else None
            if bid is not None:
                return bid
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==========================================
# 请求/响应模型
# ==========================================

class KeywordAddRequest(BaseModel):
    brand_id: Optional[int] = None            # ✅ 新增：brand_id 优先
    quote_id: Optional[int] = None
    client_id: Optional[str] = None           # @deprecated: 兼容期保留
    keyword: str
    target_brand: str
    difficulty: str = "中等"
    target_rate: int = 60
    platforms: Optional[str] = None
    note: Optional[str] = None
    # [CTO-15.9 2026-04-25 Codex bug 3 二审修] P0.8 monitoring_query 端到端闭环
    # 代理可在添加词条时自定义实际查询语句(B2B/工具类不被"哪家好"污染)
    monitoring_query: Optional[str] = None


class KeywordBatchAddRequest(BaseModel):
    brand_id: Optional[int] = None            # ✅ 新增
    client_id: Optional[str] = None           # @deprecated
    keywords: List[dict]


class MonitoringRunRequest(BaseModel):
    brand_id: Optional[int] = None           # ✅ 新增：全局业务主键
    quote_id: Optional[int] = None           # @deprecated: 兼容期保留
    client_id: Optional[str] = None          # @deprecated: 兼容期保留
    keyword_ids: Optional[List[int]] = None  # @deprecated 2026-06-07 · 已不参与过滤(真实范围走 keyword_keys)· 仅历史兼容入参·勿依赖
    keyword_keys: Optional[List[str]] = None  # "confirmed-123" 格式 · 真实执行范围入口
    platforms: Optional[List[str]] = None
    concurrency: Optional[int] = None
    test_rounds: int = 3
    search_mode: str = "enhanced"            # "enhanced"(默认增强版) · 旧 standard/auto 兼容入参仍可传


class ConfigUpdateRequest(BaseModel):
    brand_id: Optional[int] = None            # ✅ 新增
    client_id: str = "_global_"               # @deprecated
    default_concurrency: Optional[int] = None
    default_platforms: Optional[str] = None


# ==========================================
# API函数
# ==========================================

_PLAN_ERROR_SUMMARY_MAX = 200
# 连接串 / DSN 一旦被异常文案带出来就是凭据泄漏。异常类型不可穷举(psycopg2 的
# OperationalError 会把整个 DSN 抄进 message),所以按**模式**打掉而不是按类型白名单。
_PLAN_ERROR_REDACT_PATTERNS = (
    re.compile(r"post(?:gresql|gres)://[^\s]*", re.IGNORECASE),
    re.compile(r"password=\S+", re.IGNORECASE),
)


def summarize_monitoring_plan_error(plan_error: BaseException) -> str:
    """把监测计划创建失败的真异常压成一行可外发的摘要。

    [R4 · 2026-08-04] 旧版外层 catch 只发一句「监测执行计划创建失败」,真异常整个
    被吞掉:生产 monitoring_tasks 1543/1544 两次 failed,库里 result_summary 空、
    monitoring_run_cells 0 行、日志也没留下 plan_error —— 排查只能靠反推
    total_tests=29 这种指纹。这是"可见性"本身的缺陷,不是文案问题。

    脱敏口径(刻意窄):
      · 保留**异常类名 + 文案** —— MonitoringCellConflict 的文案已按 R2 点名到
        「哪个词的哪个平台」,那正是排查唯一需要的东西;
      · 打掉连接串/口令(按模式,不按异常类型白名单:psycopg2.OperationalError
        会把整个 DSN 抄进 message,类型是穷举不完的);
      · 压掉换行、截断到 200 字 —— 不外发 traceback,栈只进服务端日志。
    """
    summary = f"{type(plan_error).__name__}: {plan_error}"
    for pattern in _PLAN_ERROR_REDACT_PATTERNS:
        summary = pattern.sub("[redacted]", summary)
    return " ".join(summary.split())[:_PLAN_ERROR_SUMMARY_MAX]


def api_get_supported_platforms() -> dict:
    """获取支持的平台列表"""
    return {
        "status": "success",
        "platforms": PlatformAdapter.get_supported_platforms()
    }


def api_get_config(brand_id: int = None, client_id: str = "_global_") -> dict:
    """获取监测配置（优先 brand_id）"""
    config = get_monitoring_config(brand_id=brand_id, client_id=client_id)
    return {
        "status": "success",
        "config": config
    }


def api_update_config(request: ConfigUpdateRequest) -> dict:
    """更新监测配置"""
    success = update_monitoring_config(
        brand_id=request.brand_id,
        client_id=request.client_id,
        default_concurrency=request.default_concurrency,
        default_platforms=request.default_platforms
    )
    return {
        "status": "success" if success else "error",
        "message": "配置已更新" if success else "更新失败"
    }


# ==========================================
# 付费客户API（新增）
# ==========================================

def api_get_clients(limit: int = 100) -> dict:
    """获取付费客户列表"""
    clients = get_paid_clients(limit)
    return {
        "status": "success",
        "count": len(clients),
        "clients": clients
    }


# 客户门户只补齐当前配置中真正可执行的监测引擎。历史观测仍保留在数据库中，
# 但不可用于监测采集的付费诊断表面不能被补成「待监测」槽位。
def _fill_engine_slots(details, platforms=None):
    """按实际可执行平台补齐某词的检测槽位，保持配置顺序。"""
    configured = (
        platforms
        if platforms is not None
        else DEFAULT_MONITORING_PLATFORMS.split(",")
    )
    engines = PlatformAdapter.eligible_monitoring_platforms(configured)
    by_platform = {}
    for d in (details or []):
        p = d.get("platform")
        if p and p not in by_platform:
            by_platform[p] = d
    filled = []
    for eng in engines:
        if eng in by_platform:
            filled.append(by_platform[eng])
        else:
            filled.append({
                "platform": eng,
                "is_detected": False,
                "mention_type": "none",
                "tested_at": None,
                "citations": [],
                "snippet": "",
                "pending": True,  # 尚无本词该引擎记录 → 前端显「待监测」灰态(不隐藏)
            })
    return filled


def _resolve_keyword_display_platforms(keyword: dict, configured_platforms) -> list:
    """Project current slots from preference and the keyword purchase snapshot."""
    return PlatformAdapter.eligible_monitoring_platforms(
        configured_platforms,
        keyword.get("entitlement_platforms") or [],
    )


def _historical_keyword_platform_details(details: list, current_platforms: list) -> list:
    """Keep old observations auditable without presenting them as pending work."""
    current = set(current_platforms)
    return [
        {**detail, "historical_only": True}
        for detail in details
        if detail.get("platform") not in current
    ]


def api_get_client_keywords_merged(quote_id: int) -> dict:
    """获取客户的所有词条（合并confirmed_keywords和extra_keywords），附带平台检测详情 + 达标倒计时"""
    keywords = get_client_keywords(quote_id)
    scope = _resolve_quote_scope(client_id=str(quote_id))
    display_config = get_monitoring_config(
        brand_id=scope.get("brand_id"),
        client_id=str(quote_id),
    )
    display_preference = display_config.get(
        "default_platforms", DEFAULT_MONITORING_PLATFORMS
    )

    # [P2/green 2026-06-05 老板复审] 监测中心只展示【付费监控核心词】· 相关搜索参考(is_core=False)不被监测引擎跑(无检测数据)
    #   → 不当监控词/合同词/达标词展示(与监测引擎 get_keywords_for_monitoring 的 is_core IS NOT FALSE + 达标 C1 口径一致)
    # [§4.2 闭环 2026-06-06 老板] 超红海词(super_red_ocean=true)从普通监测列表剥离 → 单独「高竞争·需单独报价·不计达标」区:
    #   不进普通监测词/达标词/合同词(与 get_keywords_for_monitoring 排除 + daily 达标 skip 同口径)· 即便客户强行纳入也只单独展示·不混普通监测
    super_red_ocean_keywords = [
        {
            "id": kw.get("id"),
            "keyword": kw.get("keyword"),
            "source": kw.get("source"),
            "super_red_ocean": True,
            "competition_ratio": kw.get("competition_ratio"),
            "entitlement_platforms": kw.get("entitlement_platforms"),
            "note": "高竞争 · 需单独报价 · 不计达标 · 不承诺出现率",
        }
        for kw in keywords
        if kw.get("super_red_ocean") is True and kw.get("is_core") is not False
    ]
    # [2026-06-07 批B 门户] 同义覆盖词(is_core=False · 非超红海)在过滤前捕获 · 仅展示「相关搜索参考」·
    #   绝不进监测/达标/扣费:不改 get_keywords_for_monitoring · daily 达标 loop 也按 is_core 过滤 · 此处纯展示
    covered_raw = [
        kw for kw in keywords
        if kw.get("is_core") is False and not kw.get("super_red_ocean")
    ]
    keywords = [kw for kw in keywords if kw.get("is_core") is not False and not kw.get("super_red_ocean")]

    # [2026-06-07 批B 门户] 构建覆盖词展示列表(仿 super_red_ocean 模式)· keyword/final_price/coverage_relation/parent_core
    #   final_price 取自 confirmed_keywords(get_client_keywords 未返该列)· parent_core = 同 cluster 核心词
    covered_keywords = []
    if covered_raw:
        _cluster_core = {}
        for _ck in keywords:
            _cid = _ck.get("cluster_id")
            if _cid is not None and _cid not in _cluster_core:
                _cluster_core[_cid] = _ck.get("keyword")
        _fp_map = {}
        _cov_ids = [c.get("id") for c in covered_raw if c.get("id") is not None]
        if _cov_ids:
            _cfp = None
            try:
                from db.diagnosis_db import get_connection as _gc_fp
                _cfp = _gc_fp()
                _curfp = _cfp.cursor()
                _curfp.execute("SELECT id, final_price FROM confirmed_keywords WHERE id = ANY(%s)", (_cov_ids,))
                for _r in _curfp.fetchall():
                    _fp_map[_r["id"]] = _r["final_price"]
            except Exception as _fpe:
                print(f"[Portal] covered final_price 查询失败(降级不显价): {_fpe}")
            finally:
                # 异常/正常都关连接 · 门户接口不留连接泄漏口
                if _cfp is not None:
                    try:
                        _cfp.close()
                    except Exception:
                        pass
        for _c in covered_raw:
            covered_keywords.append({
                "id": _c.get("id"),
                "keyword": _c.get("keyword"),
                "source": _c.get("source"),
                "final_price": _fp_map.get(_c.get("id")),
                "coverage_relation": "同义覆盖",
                "parent_core": _cluster_core.get(_c.get("cluster_id")),
                "note": "顺带覆盖 · 不单独监测 · 不承诺达标",
            })

    # 获取每个词条的平台检测详情（最近一轮）
    from db.monitoring_db import get_keyword_detection_details, get_keyword_compliance_summary
    # [2026-06-06 反转 §4.2 · 老板:监测不隐藏] 超红海词也取逐引擎检测明细一并展示(已重新进监测调度·有数据)·
    #   但仍只单独展示·不计达标(daily compliance loop 独立 skip · monitoring_db ~3564)。
    sro_ids = [s["id"] for s in super_red_ocean_keywords if s.get("id") is not None]
    keyword_ids = [kw["id"] for kw in keywords]
    details_map = get_keyword_detection_details(keyword_ids + sro_ids)
    # 超红海词补上当前配置中可执行引擎的检测明细，不虚列付费诊断表面。
    for s in super_red_ocean_keywords:
        current_platforms = _resolve_keyword_display_platforms(s, display_preference)
        historical_details = details_map.get(s["id"], [])
        s["detection_details"] = _fill_engine_slots(
            historical_details, current_platforms
        )
        s["historical_detection_details"] = _historical_keyword_platform_details(
            historical_details, current_platforms
        )

    # 获取达标倒计时数据
    compliance_map = get_keyword_compliance_summary(quote_id)
    tier_data = _get_tier_target(client_id=str(quote_id))

    # 获取合同服务期(SSOT)+ 履约达标天数配额
    # 🔴 [服务期 SSOT 2026-08-06 §1.1/§1.3] 本段是 Owner 报的那口"说还有一年"的假钟原址。
    #   旧:contract_end = contract_start + service_days,且 service_days 取不到就 365。
    #       service_days 是**累计达标天数配额**(单位不是日历天),库默认 365、全站没有任何
    #       写入点 —— 于是 11/13 张 paid 报价的倒计时按"起始日 + 365 天"跑,
    #       而轮换资格闸读的 service_end_date 是"起始日 + service_months(默认 1)"。
    #   新:contract_end **只读 quotes.service_end_date**;没有服务期就是没有,
    #       返 None + 一条带出口的提示,绝不拿配额或默认值糊一个出来。
    from services.service_period import (
        calendar_days_left as _sp_days_left,
        compliance_target_days as _sp_target_days,
        read_calendar_period as _sp_read_period,
        service_period_missing_hint as _sp_missing_hint,
        resolve_auto_monitoring_status as _sp_auto_status,
    )
    # [客户反馈⑥ 2026-08-09] 自动监测真实排班资格 —— 前端横幅不许再自己按日期推断。
    #   多取两个事实:monitoring_enabled(开关)、active 逐词订阅条数(决定挂哪条排班链)。
    #   查不到就是 None,下面按 fail-soft 不下发这个字段(前端退回只报日期不报因果)。
    auto_monitoring = None
    try:
        from db.diagnosis_db import get_connection as _gc
        _conn = _gc()
        _cur = _conn.cursor()
        _cur.execute(
            "SELECT service_days, service_start_date, service_end_date, paid_at,"
            "       COALESCE(monitoring_enabled, FALSE) AS monitoring_enabled"
            "  FROM quotes WHERE id = %s",
            (quote_id,),
        )
        _qrow = _cur.fetchone()
        # 🔴 计数口径必须与 `db.monitoring_db.list_active_subscriptions` 的 WHERE **同形**,
        #   否则会高估。2026-08-09 生产快照实测:只数 `s.status='active'` 时,
        #   晨光富士 quote 286 报 5 条,而真实可排班是 **0** 条
        #   —— 它 5 个核心词全被代理手动关了自动监测(is_monitored=false /
        #   monitoring_status='archived'),KMS 订阅行却仍是 active。
        #   全库这种"只看订阅状态会看错"的报价共 1 张,但一张就足够让横幅继续说假话。
        #   三条件缺一不可:订阅 active ∧ 逐词自动监测开关开 ∧ 达标天数未满。
        from db.monitoring_db import quote_service_anchor_condition_sql as _anchor_sql
        _cur.execute(
            f"""
            SELECT COUNT(*) AS cnt
              FROM keyword_monitor_subscriptions s
              JOIN confirmed_keywords ck ON ck.id = s.keyword_id
              JOIN quotes q ON q.id = s.quote_id
             WHERE s.quote_id = %s
               AND s.status = 'active'
               AND ck.is_monitored = TRUE
               AND COALESCE(ck.monitoring_status, 'active') = 'active'
               AND {_anchor_sql("q")}
               AND COALESCE((
                     SELECT COUNT(*) FILTER (WHERE kcl.is_compliant = TRUE)
                       FROM keyword_compliance_log kcl
                      WHERE kcl.keyword_id = s.keyword_id
                        AND kcl.quote_id = s.quote_id
                        AND kcl.check_date >= COALESCE(q.service_start_date, q.paid_at::date)
                   ), 0) < q.service_days
            """,
            (quote_id,),
        )
        _subrow = _cur.fetchone()
        _conn.close()
        auto_monitoring = _sp_auto_status(
            monitoring_enabled=(_qrow or {}).get("monitoring_enabled"),
            service_end=(_qrow or {}).get("service_end_date"),
            active_subscription_count=(_subrow or {}).get("cnt"),
        )
        quote_service_days = _sp_target_days(_qrow)
        _svc_start, _svc_end = _sp_read_period(_qrow)
        quote_service_start = str(_svc_start) if _svc_start else None
        # [v1.5 → 2026-05-29 D2 锚翻转] service_start_date 优先(权威服务锚)· 兜底 paid_at(财务/老 quote)
        _paid_at = _qrow.get("paid_at") if _qrow else None
        _contract_start = _svc_start
        if _contract_start is None and _paid_at:
            try:
                _contract_start = _paid_at.date() if hasattr(_paid_at, "date") else _paid_at
            except Exception:
                _contract_start = None
    except Exception:
        quote_service_days = None
        quote_service_start = None
        _contract_start = None
        _svc_end = None

    # [v1.5 2026-05-29 / v1.7 2026-05-29 老板复审 P1 / 服务期 SSOT 2026-08-06]
    # 主字段 自然日剩余 = service_end_date − today(唯一一口钟)
    # portal UI:主显示 service_remaining_days_natural · 副显示 compliance_remaining_days(履约 N/M)
    #
    # v1.7 老板复审 P1:不 clamp 到 0 · UI 要能展示"已过期 N 天"
    #   - service_remaining_days_natural_signed: 可正可负 · 负 = 已过期 N 天
    #   - service_overdue_days: 过期天数(0 = 未过期 / 正 = 过期天数)
    #   - service_expired: bool flag
    #   - service_remaining_days_natural: 旧字段保留(向后兼容)· clamp 到 0 维持老前端语义
    service_remaining_days_natural = None
    service_remaining_days_natural_signed = None
    service_overdue_days = None
    service_expired = None
    contract_end = _svc_end
    signed = _sp_days_left(_svc_end)
    if signed is not None:
        service_remaining_days_natural_signed = signed
        service_remaining_days_natural = max(0, signed)  # 向后兼容老前端
        service_overdue_days = max(0, -signed)
        service_expired = signed < 0
    # 没服务期 ≠ 服务无限期。给一条带出口的提示(提示铁律:只报状态不给路径 = UX 缺陷)。
    service_period_hint = None if _svc_end else _sp_missing_hint(quote_id)

    # [2026-06-30 kou-jing tong-yi] single authority: per-kw live effective + client avg
    from db.monitoring_db import get_unified_appearance
    _unified = get_unified_appearance(quote_id, keywords=keywords)
    _live_eff = _unified["per_keyword"]

    for kw in keywords:
        current_platforms = _resolve_keyword_display_platforms(kw, display_preference)
        historical_details = details_map.get(kw["id"], [])
        kw["detection_details"] = _fill_engine_slots(
            historical_details, current_platforms
        )
        kw["historical_detection_details"] = _historical_keyword_platform_details(
            historical_details, current_platforms
        )
        # 附加达标信息
        c = compliance_map.get(kw["id"])
        kw["target_rate"] = tier_data["target_rate"]
        # [CTO-15.23 2026-05-11] detection_rate 从 SQL ROUND(NUMERIC) 返来是 Decimal·
        # psycopg2 序列化成字符串 "0.0" · 前端比较"0.0" >= 60 走隐式转换 OK 但
        # `kw.detection_rate || 0` 在 "0.0" 时是 truthy 字符串 · 显示 "0.0%" 而非 "0%"。
        # 统一转 float · 既给前端展示用 · 也给 LLM 数据源用。
        try:
            raw_detection_rate = float(kw.get("detection_rate", 0) or 0)
        except (TypeError, ValueError):
            raw_detection_rate = 0.0
        try:
            weighted_source = kw.get("weighted_rate")
            weighted_rate = raw_detection_rate if weighted_source is None else float(weighted_source)
        except (TypeError, ValueError):
            weighted_rate = raw_detection_rate
        kw["raw_detection_rate"] = raw_detection_rate
        kw["weighted_rate"] = weighted_rate
        kw["detection_rate"] = weighted_rate
        if c:
            realtime_rate = float(kw.get("detection_rate", 0) or 0)
            effective_rate = float(_live_eff.get(kw["id"], c.get("effective_rate") or realtime_rate))
            target_rate = float(tier_data["target_rate"])

            # [CTO-15.23 2026-05-12 BUG fix · 全面修复 P1] is_compliant 用 effective_rate 判定 ·
            # 跟 UI 主展示(累计平均)同口径。老板 mental:"出现率 75% > 目标 50% → 达标"。
            # 撤回 Social-CTO-13.0 line 192 用 detection_rate 的修法(那导致出现率 vs 达标矛盾)。
            #
            # Social-CTO-13.0 担心"客户展开原文今日没看到品牌但表格说达标信任崩" →
            # 改用 is_today_dropped flag 给前端 ⚠️ 警告标 · 不撞他的担心。
            kw["is_compliant"] = effective_rate >= target_rate
            kw["effective_rate"] = effective_rate

            # [CTO-15.23 2026-05-12 新增] is_today_dropped: 累计已达标但今日实时跌出目标
            # UI 加 ⚠️ "今日实时低于目标" 警告 · 提示客户/代理今日波动
            kw["is_today_dropped"] = (effective_rate >= target_rate) and (realtime_rate < target_rate)

            kw["compliant_days"] = c["compliant_days"]
            kw["remaining_days"] = c["remaining_days"]
            kw["service_days"] = c["service_days"]
            kw["compliance_progress"] = c["compliance_progress"]
            # [CTO-15.23 2026-05-10] is_stable: 最近 7 天 ≥ 5 天达标
            kw["is_stable"] = bool(c.get("is_stable"))
            # [v1.5 2026-05-29] 副字段履约 N/M alias · 显式语义(remaining_days 老字段名易混)
            kw["compliance_remaining_days"] = c["remaining_days"]
            # [履约口径 2026-06-04] A 方案统一字段名 · 还需达标天数 = service_days − compliant_days
            kw["remaining_compliant"] = c.get("remaining_compliant", c["remaining_days"])
        else:
            # 无日志（首次或未运行过 compliance check）：回退到实时检出率
            # [Deploy-CTO 2026-05-14] 修 "清数据后倒计时显待评估" BUG
            #   原: remaining_days/service_days=None → 前端 KeywordTable 走 '待评估' 分支
            #   现: 用 quote 的履约配额兜底 → 前端显示"还需达标 N 天"
            #   compliant_days=0 时 remaining = 配额(自然值 · 不 hardcode)
            # 🔴 [服务期 SSOT 2026-08-06] 这几个字段全是**履约达标天数**口径,不是日历倒计时。
            #   日历倒计时走 service_remaining_days_natural(下方,读 service_end_date)。
            kw["is_compliant"] = (kw.get("detection_rate", 0) or 0) >= tier_data["target_rate"]
            kw["effective_rate"] = float(kw.get("detection_rate", 0) or 0)
            kw["compliant_days"] = 0
            kw["remaining_days"] = quote_service_days
            kw["service_days"] = quote_service_days
            kw["compliance_progress"] = 0
            kw["is_stable"] = False
            kw["compliance_remaining_days"] = quote_service_days  # v1.5 alias
            # [履约口径 2026-06-04] A 方案:无日志时 compliant_days=0 → 还需 = service_days
            kw["remaining_compliant"] = quote_service_days

        # [v1.5 2026-05-29 / v1.7 2026-05-29 老板复审 P1]
        # 主字段:自然日剩余(每个 keyword 共享 quote 级别值)
        # 前端 portal 显示"服务剩余 X 天"用此字段 · 副显示 compliance_remaining_days(N/M)
        # signed/overdue/expired 给 UI 决定怎么显示已过期场景
        kw["service_remaining_days_natural"] = service_remaining_days_natural
        kw["service_remaining_days_natural_signed"] = service_remaining_days_natural_signed
        kw["service_overdue_days"] = service_overdue_days
        kw["service_expired"] = service_expired

        # [CTO-15.23 2026-05-11] display_rate = 客户门户/AI 洞察 统一展示用值
        # 老板报"AI 洞察说 0% 但词条详情显示 75%"打架根因:
        #   detection_rate(本期实时)= 0.0 vs effective_rate(历史滚动平滑)= 75.0
        # 客户视角:看历史平滑(effective_rate)合理 · 单次 0 不代表服务失败
        # display_rate 是新统一字段 · 让前端 UI + LLM insights 都用同一个值
        # 旧字段 detection_rate / effective_rate 保留用于代理端深度分析
        # [v12 item8] display_rate 【不得由自然日 service_expired 决定】—— 旧逻辑"自然日过期→掉成当期实时
        #   detection_rate"会把【未达标(compliant_days=0)但付款超服务天数】的词误判过期 → 展示掉成实时 0%,
        #   与履约口径("未达标不消耗服务天数·继续监测")打架。统一用履约滚动平滑 effective_rate(空则 detection_rate);
        #   服务是否交付由履约口径 compliant_days>=service_days 决定,与本展示值无关。防"AI 洞察 0% vs 详情 75%"打架。
        kw["display_rate"] = kw.get("effective_rate") if (kw.get("effective_rate") is not None) else kw.get("detection_rate", 0)

    return {
        "status": "success",
        "count": len(keywords),
        "client_appearance_rate": _unified["client_avg"],
        "keywords": keywords,
        # [§4.2 闭环] 超红海词单独区(高竞争·需单独报价·不计达标·不承诺出现率)· 前端单独展示·绝不混进普通监测/达标词
        "super_red_ocean_keywords": super_red_ocean_keywords,
        "super_red_ocean_count": len(super_red_ocean_keywords),
        # [2026-06-07 批B 门户] 同义覆盖词(顺带覆盖·不单独监测·不承诺达标)· 前端单独「相关搜索参考」区·不混普通监测/达标
        "covered_keywords": covered_keywords,
        "covered_count": len(covered_keywords),
        "tier": tier_data,
        # 履约达标天数配额(累计达标天数,**不是日历天**)· 前端不许拿它算"服务期至"
        "service_days": quote_service_days,
        "compliance_target_days": quote_service_days,  # 显式命名 alias · 新前端读这个
        "service_start_date": quote_service_start,
        # 没设服务期时的带出口提示(§1.2:不猜、不静默)
        "service_period_hint": service_period_hint,
        # [v1.5 2026-05-29 / v1.7 2026-05-29 老板复审 P1]
        # portal 主字段 · 自然日剩余 = quotes.service_end_date − today(服务期唯一 SSOT)
        # portal UI 改:主显示此字段(服务期剩余天数 · 客户最关心)· 副显示 compliance_remaining_days(履约 N/M)
        # v1.7 加 signed / overdue / expired · UI 决定怎么展示过期场景
        "contract_start_date": str(_contract_start) if _contract_start else None,
        "contract_end_date": str(contract_end) if contract_end else None,
        "service_remaining_days_natural": service_remaining_days_natural,
        "service_remaining_days_natural_signed": service_remaining_days_natural_signed,
        "service_overdue_days": service_overdue_days,
        "service_expired": service_expired,
        # [客户反馈⑥ 2026-08-09] 自动监测**真实**排班资格(active / code / 人话)。
        #   前端横幅只许显示这里的结论,不许再拿 service_expired 自己推"已暂停" ——
        #   日历到期只卡"报价级轮换"那条链,逐词订阅那条链按达标天数继续跑
        #   (实证:quote 287 过期 60 天仍在天天扣费跑)。
        #   取不到时为 None:前端退回中性陈述(只报到期天数,不报因果)。
        "auto_monitoring": auto_monitoring,
    }


# ==========================================
# 词条管理API
# ==========================================

def api_add_keyword(request: KeywordAddRequest) -> dict:
    """添加额外词条

    [fast-follow 2026-05-29] quote_id 归属修:KeywordAddRequest 带 quote_id,但 add_keyword 仅认 brand_id/client_id
    (client_id 实为 quote_id 字符串 · 见 _resolve_id)。原只透传 brand_id/client_id → 只传 quote_id 的合法请求
    会写成无归属的孤儿 extra_keywords(鉴权已按 quote_id 放行,写入却丢了归属)。
    规范化:client_id 缺省时用 str(quote_id) 兜底,确保 quote_id 作为权威归属落库。
    """
    try:
        _client_id = request.client_id or (str(request.quote_id) if request.quote_id is not None else None)
        keyword_id = add_keyword(
            brand_id=request.brand_id,
            client_id=_client_id,
            keyword=request.keyword,
            target_brand=request.target_brand,
            difficulty=request.difficulty,
            target_rate=request.target_rate,
            platforms=request.platforms,
            monitoring_query=request.monitoring_query,  # P0.8 CTO-15.9 bug 3 二审修
        )
        # [Deploy-CTO 2026-05-26 根治 B] 反查 resolved_quote_id 返前端 · 让 frontend 跳到正确 quote
        # 老板报"加 keyword 提示成功但列表不显示"真因之一:
        #   brand-only add 时 _resolve_id 内部挑了 quote A · 但前端 selectedClient 是 quote B
        #   → fetchKeywords(B) 看不到刚写到 A 的 row
        # 修:返 resolved_quote_id · frontend setSelectedClient(resolved) 自动跳过去
        from db.monitoring_db import _resolve_id as _ri
        try:
            _resolved_brand, _resolved_quote = _ri(brand_id=request.brand_id, client_id=_client_id)
            _resolved_quote_id = int(_resolved_quote) if _resolved_quote and str(_resolved_quote).isdigit() else None
        except Exception:
            _resolved_brand = request.brand_id
            _resolved_quote_id = None
        # 记录操作日志
        log_operation(
            operator_id=str(request.brand_id or request.client_id),
            brand_id=_resolve_operation_brand_id(
                brand_id=_resolved_brand,
                client_id=_client_id,
                quote_id=_resolved_quote_id,
            ),
            action="add_keyword",
            target_type="keyword",
            target_id=keyword_id,
            details={"keyword": request.keyword, "brand": request.target_brand}
        )
        return {
            "status": "success",
            "keyword_id": keyword_id,
            "quote_id": _resolved_quote_id,  # 让 frontend 跳到 keyword 真实所在的 quote
            # [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 P0-A.5] 加词不再隐式开启监测。
            #   旧行为:写进 extra_keywords 就 status='active'(库默认)→ 立刻进该品牌每一次监测批次、
            #   按 130 算力/词/次 开始花钱,而界面上没有任何开关能关掉它(只能整条归档)。
            #   现在 is_monitored 默认 FALSE,加词 = 只入库不监测,必须有人点开关才开始跑和扣。
            "is_monitored": False,
            "message": "已添加 · 未开启监测。点开关后才开始监测并按 130 算力/词/次 扣费。",
        }
    except Exception as e:
        # CTO-15.23 2026-05-25 · 老板报"添加关键词 SQL duplicate key 错误弹给客户"
        # · 原始 PostgreSQL 错误 "extra_keywords_quote_id_keyword_key" 不应暴露 UI
        # · 识别 unique constraint violation · 返中文友好 error
        err_str = str(e)
        if "duplicate key" in err_str.lower() or "unique constraint" in err_str.lower():
            # [Deploy-CTO 2026-05-26 根治 B+] duplicate 时也返 resolved_quote_id
            # · 让 frontend 跳到已存在 row 所在 quote(让用户能看到)
            try:
                from db.monitoring_db import _resolve_id as _ri
                _resolved_brand, _resolved_quote = _ri(brand_id=request.brand_id, client_id=_client_id)
                _resolved_quote_id = int(_resolved_quote) if _resolved_quote and str(_resolved_quote).isdigit() else None
            except Exception:
                _resolved_quote_id = None
            return {
                "status": "error",
                "error": f'关键词「{request.keyword}」已添加 · 请勿重复',
                "code": "duplicate_keyword",
                "quote_id": _resolved_quote_id,
            }
        return {
            "status": "error",
            "error": str(e)
        }


def api_batch_add_keywords(request: KeywordBatchAddRequest) -> dict:
    """批量添加词条"""
    try:
        _resolved_brand_id = _resolve_operation_brand_id(
            brand_id=request.brand_id,
            client_id=request.client_id,
        )
        count = batch_add_keywords(
            brand_id=request.brand_id,
            client_id=request.client_id,
            keywords=request.keywords
        )
        # 记录操作日志
        log_operation(
            operator_id=str(request.brand_id or request.client_id),
            brand_id=_resolved_brand_id,
            action="batch_add_keywords",
            target_type="keyword",
            target_id=None,
            details={"count": count}
        )
        return {
            "status": "success",
            "added_count": count
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


def api_get_keywords(brand_id: int = None, client_id: str = None, status: str = "active") -> dict:
    """获取词条列表（优先 brand_id）"""
    keywords = get_keywords(brand_id=brand_id, client_id=client_id, status=status)
    
    # 附加统计信息
    for kw in keywords:
        stats = get_keyword_stats(kw["id"])
        kw["stats"] = stats
    
    return {
        "status": "success",
        "count": len(keywords),
        "keywords": keywords
    }


def api_get_keyword(keyword_id: int) -> dict:
    """获取单个词条详情"""
    keyword = get_keyword_by_id(keyword_id)
    if not keyword:
        return {"status": "error", "error": "词条不存在"}
    
    stats = get_keyword_stats(keyword_id)
    keyword["stats"] = stats
    
    return {
        "status": "success",
        "keyword": keyword
    }


def api_update_keyword_status(keyword_id: int, status: str, source: str = "client") -> dict:
    """更新词条状态

    [A-5-2 安全修 2026-05-29] 透传 source · 让底层更新与上游 RBAC 校验同一张表(防跨表 IDOR)。
    """
    success = update_keyword_status(keyword_id, status, source=source)
    return {
        "status": "success" if success else "error",
        "message": "状态已更新" if success else "更新失败"
    }


def api_delete_keyword(keyword_id: int, source: str = "client") -> dict:
    """删除词条"""
    log_brand_id = _resolve_operation_brand_id(keyword_id=keyword_id, source=source)
    if source == "extra":
        # 删除extra_keywords表中的记录
        from db.monitoring_db import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM extra_keywords WHERE id = %s", (keyword_id,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        success = affected > 0
    else:
        success = delete_keyword(keyword_id)
    
    # 记录操作日志
    if success:
        log_operation(
            operator_id="system",
            brand_id=log_brand_id,
            action="delete_keyword",
            target_type="keyword",
            target_id=keyword_id,
            details={"source": source}
        )
    
    return {
        "status": "success" if success else "error",
        "message": "已删除" if success else "删除失败"
    }


# ==========================================
# 监测任务API
# ==========================================

async def api_run_monitoring(
    request: MonitoringRunRequest,
    *,
    organization_identity=None,
    task_created_callback=None,
    before_first_provider=None,
    settlement_reference=None,
    initial_fulfillment_state="reserved",
) -> dict:
    """启动监测任务"""
    try:
        # [2026-06-07 老板复审 fix1+fix2] 本函数仅被非流式 /api/monitoring/run 调用(server.py 已 freeze/commit/release monitor_single):
        #   charge=False 防 run_client_monitoring 二次 freeze scheduled_monitoring(双扣);
        #   keyword_keys 贯穿 → 实跑词范围 == /run freeze 词范围(否则冻结部分词却跑全量)。
        result = await run_client_monitoring(
            client_id=request.client_id,
            brand_id=request.brand_id,
            # [#1 2026-06-07] 不再转发已废弃的 request.keyword_ids(真实范围走 keyword_keys)
            keyword_keys=request.keyword_keys,
            platforms=request.platforms,
            concurrency=request.concurrency,
            charge=False,
            organization_identity=organization_identity,
            task_created_callback=task_created_callback,
            before_first_provider=before_first_provider,
            settlement_reference=settlement_reference,
            initial_fulfillment_state=initial_fulfillment_state,
        )
        return result
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


import json
import asyncio
from typing import AsyncGenerator


def _quote_rows_for_brand(brand_id: int) -> list[dict]:
    """Resolve monitoring/report quote scope without dropping paid active quotes."""
    if not brand_id:
        return []
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT id, brand_id, status, service_status, confirmed_at, paid_at, created_at
            FROM quotes q
            WHERE q.brand_id = %s
              AND status IN ('confirmed', 'paid')
              -- [2026-06-07 老板复审 P1] scope 按服务锚过滤 · 对齐 get_client_keywords(db/monitoring_db ~1681)口径:
              --   confirmed 必须有服务锚(service_start_date 或 paid_at 任一非空)才进监测范围 ——
              --   否则历史脏草稿 quote(confirmed 且两者皆空)进 scope 后会被下游服务锚闸口当"未启动"挡掉 → 误挡整个品牌正常监测。
              AND {quote_service_anchor_condition_sql("q")}
            ORDER BY
              CASE
                WHEN q.status = 'paid' AND COALESCE(q.service_status, 'active') = 'active' THEN 0
                WHEN q.status = 'paid' THEN 1
                WHEN q.status = 'confirmed' THEN 2
                ELSE 3
              END,
              q.paid_at DESC NULLS LAST,
              q.confirmed_at DESC NULLS LAST,
              q.created_at DESC NULLS LAST,
              q.id DESC
            """,
            (brand_id,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _resolve_quote_scope(*, brand_id: int = None, client_id: str = None) -> dict:
    """Return brand_id plus quote_ids for brand-first APIs.

    client_id is a legacy quote_id on these routes.
    """
    if brand_id:
        rows = _quote_rows_for_brand(int(brand_id))
        return {
            "brand_id": int(brand_id),
            "quote_ids": [int(row["id"]) for row in rows],
            "client_id": str(rows[0]["id"]) if rows else None,
        }

    if client_id:
        quote_id = int(client_id)
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, brand_id FROM quotes WHERE id = %s", (quote_id,))
            row = cursor.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not row:
            return {"brand_id": None, "quote_ids": [], "client_id": str(quote_id)}
        _bid = int(row["brand_id"]) if row.get("brand_id") is not None else None
        # [2026-06-07 老板复审 fix2] client_id 兼容路径(旧接口直传 quote_id)必须套同一服务锚口径 ——
        #   未付/未启动/已过期 → quote_ids=[]·让 /run、/run-stream 在 freeze 前 fail-closed
        #   (配 run-stream/run 的 brand_has_confirmed_or_paid_quote 三路判断 → 有脏 quote 不 fallback、无可监测词条)。
        from db.monitoring_db import is_quote_service_anchored
        _anchored = is_quote_service_anchored(quote_id)
        return {
            "brand_id": _bid,
            "quote_ids": [quote_id] if _anchored else [],
            "client_id": str(quote_id),
        }

    return {"brand_id": None, "quote_ids": [], "client_id": None}


def _publications_for_quotes(quote_ids: list[int], limit: int = 100) -> list[dict]:
    publications = []
    for quote_id in quote_ids or []:
        publications.extend(get_publications(quote_id, limit) or [])
    publications.sort(
        key=lambda row: (str(row.get("publish_date") or ""), str(row.get("created_at") or "")),
        reverse=True,
    )
    return publications[:limit]


def _resolve_brand_and_quotes(request: MonitoringRunRequest) -> tuple:
    """
    从请求中解析 brand_id 和 quote_ids。
    兼容期同时支持 brand_id（新）和 client_id（旧，实为 quote_id）。
    
    Returns: (brand_id: int, quote_ids: list[int])
    """
    if request.brand_id:
        # brand_id 新路径同时纳入 confirmed + paid active，避免已付客户监测漏词。
        scope = _resolve_quote_scope(brand_id=request.brand_id)
        return scope["brand_id"], scope["quote_ids"]
    elif request.client_id:
        # 🔄 兼容旧路径：client_id 实为 quote_id
        scope = _resolve_quote_scope(client_id=request.client_id)
        return scope["brand_id"], scope["quote_ids"]
    else:
        return None, []


async def api_run_monitoring_stream(
    request: MonitoringRunRequest,
    user_id: int = None,
    is_admin: bool = False,
) -> AsyncGenerator[str, None]:
    """
    流式监测任务 - 使用SSE实时推送进度

    V5.0: 支持 brand_id 统一化,兼容旧 client_id

    [CTO-15.23 2026-05-05 P0 fix] 接 B 类异步长任务 billing(freeze/commit/release)
    · 此前 monitoring_api.py 全文 0 个 billing call · 主流量入口完全白送
    · prod 3 个月 502 manual + 160 scheduled tasks · 应扣 ~828K 积分实扣 0
    · 修法: freeze 在 keywords 算完 + create_task 之前 · admin 跳过 · 余额不足 SSE error 不开任务
    · 成功: commit_freeze · 异常: release_freeze

    [CTO-15.23 2026-05-08 P0-1 真修] 签名改 user_id/is_admin 直接传(取代 http_request)
    · 根因:starlette BaseHTTPMiddleware + StreamingResponse 兼容性 bug
      generator 内 access request.state.user 时 middleware 已出栈 · scope 不可达 → user_id=None silent skip
    · Deploy-CTO 5/7 实证 prod 5/5-5/7 三天 5400 LLM calls · point_transactions 0 行 monitor 类
    · 修法:server.py:5765 endpoint 在 middleware 上下文内提取 user · 参数化注入 generator
    """
    import traceback
    import uuid

    # 解析 brand_id 和 quote_ids（兼容期）
    brand_id, quote_ids = _resolve_brand_and_quotes(request)
    client_id_compat = request.client_id or str(request.brand_id or '')  # 兼容期

    # user_id / is_admin 由 endpoint 参数化注入 · 不再访问 request.state(SSE 兼容性 bug)
    print(f"[SSE] Starting stream: brand_id={brand_id}, quote_ids={quote_ids}, keys={request.keyword_keys}, user_id={user_id}, admin={is_admin}")

    # [2026-06-07 P0-3/P0-4 + 老板复审] 监测「列表 vs 启动」口径同源 + 禁未付/未开始/已过期 quote 偷跑扣算力(资金漏洞):
    #   非 admin · 每个 quote_id 走单 quote 服务锚 SSOT(is_quote_service_anchored)统一口径(paid+服务期有效 / confirmed+服务锚)。
    #   ⚠️ 资金闸必须 fail-CLOSED:校验异常【不放行】(宁可本次监测失败·不可漏扣)。
    #   注:经 fix2 后 _resolve_quote_scope 已在源头过滤·此处为纵深防御(任何未来路径塞入未锚 quote_id 也兜住)。
    if user_id and not is_admin and quote_ids:
        _gate_ok = True
        try:
            from db.monitoring_db import is_quote_service_anchored
            for _qid in quote_ids:
                if not is_quote_service_anchored(_qid):
                    _gate_ok = False
                    break
        except Exception as _ge:
            print(f"[SSE][P0-3 gate] 服务锚校验异常 → fail-closed 不放行: {_ge}")
            _gate_ok = False
        if not _gate_ok:
            yield f"data: {json.dumps({'type': 'error', 'error': '该客户尚未付款或服务未开始 · 暂不能监测(请先确认服务已启动)'}, ensure_ascii=False, default=str)}\n\n"
            return

    from db.monitoring_db import get_monitoring_config, get_keywords_for_monitoring
    from tools.monitoring.batch_monitor import (
        MonitoringTask, PlatformAdapter, build_question, resolve_monitoring_query
    )
    from db.monitoring_db import (
        claim_monitoring_run_cell,
        create_monitoring_run_cells,
        create_monitoring_task,
        finish_monitoring_cell_error,
        list_monitoring_run_cells,
        mark_monitoring_cell_dispatched,
        save_monitoring_result,
        set_monitoring_task_fulfillment_state,
        update_task_status,
    )
    import time
    from datetime import datetime

    # 获取配置
    config = get_monitoring_config(
        brand_id=brand_id,
        client_id=(str(quote_ids[0]) if quote_ids else client_id_compat or "_global_"),
    )
    platforms_str = config.get("default_platforms", DEFAULT_MONITORING_PLATFORMS)

    # 获取词条 — 从所有关联的 quote_ids 合并
    keywords = []
    if quote_ids:
        keywords = get_keywords_for_monitoring(
            quote_ids=quote_ids,
            keyword_keys=request.keyword_keys
        )
    elif brand_id:
        # [2026-06-07 老板复审 fix1] quote_ids 为空有两种情形·fallback 行为必须区分:
        #   - 有 confirmed/paid quote 但无有效服务锚(脏草稿/已过期)→ 不监测·不进引擎(禁 fallback 到 brand extra)
        #   - 完全无 quote → 才允许 brand 级 extra_keywords(代理自助订阅·合法)
        from db.monitoring_db import brand_has_confirmed_or_paid_quote
        if brand_has_confirmed_or_paid_quote(brand_id):
            keywords = []
        else:
            keywords = get_keywords_for_monitoring(
                brand_id=brand_id,
                keyword_keys=request.keyword_keys
            )
    
    # 去重（按 keyword 文本）
    seen = set()
    unique_keywords = []
    for kw in keywords:
        key = (kw['keyword'], kw.get('source', ''))
        if key not in seen:
            seen.add(key)
            unique_keywords.append(kw)
    keywords = unique_keywords
    
    # 调试日志
    print(f"[DEBUG] keyword_keys from request: {request.keyword_keys}")
    print(f"[DEBUG] keywords count: {len(keywords)}")
    for kw in keywords[:3]:
        print(f"[DEBUG] kw: {kw['keyword'][:30]} -> target_brand={kw.get('target_brand')}")
    
    if not keywords:
        _a = _gov_alerts.monitoring_no_monitorable_keywords_alert()
        yield f"data: {json.dumps({'type': 'error', 'error': _a['message'], **_a}, ensure_ascii=False, default=str)}\n\n"
        return

    platform_plan = PlatformAdapter.resolve_keyword_monitoring_plan(
        keywords,
        requested_platforms=request.platforms,
        configured_platforms=platforms_str,
    )
    if platform_plan["rejected"]:
        _a = _gov_alerts.monitoring_platform_not_entitled_alert()
        yield f"data: {json.dumps({'type': 'error', 'error': _a['message'], **_a}, ensure_ascii=False, default=str)}\n\n"
        return
    keywords = platform_plan["keywords"]
    platforms = platform_plan["platforms"]
    if not keywords:
        _a = _gov_alerts.monitoring_no_eligible_platforms_alert()
        yield f"data: {json.dumps({'type': 'error', 'error': _a['message'], **_a}, ensure_ascii=False, default=str)}\n\n"
        return

    total_tasks = sum(len(kw["_eligible_monitoring_platforms"]) for kw in keywords)

    # ==========================================
    # [CTO-15.23 2026-05-05 P0] 冻结积分 (B 类异步长任务标准模式)
    # · monitor_single base 130 / 词 · extra_cost = 130 * (N-1)
    # · admin / 无 user_id → skip(管理员免扣)
    # · 余额不足 → 抛 402 → catch 转 SSE error · 不创建 task · 不开始执行
    # ==========================================
    freeze_id = None
    freeze_table = None  # [A0] 冻结句柄表标记(admin/无冻结时保持 None · settle 时回传)
    task_ref = f"monitor_stream_{uuid.uuid4().hex[:12]}"
    n_keywords = len(keywords)

    if user_id and not is_admin:
        try:
            from middleware.billing import freeze_points
            extra_cost = 130 * (n_keywords - 1)  # pricing.cost_points = 130 base · 加 N-1 倍
            freeze_result = await freeze_points(
                user_id=user_id,
                feature_code="monitor_single",
                task_ref=task_ref,
                brand_id=brand_id,
                extra_cost=extra_cost,
                reason=f"流式监测 {n_keywords} 词 · {total_tasks} 次平台检测",
            )
            freeze_id = freeze_result.get("freeze_id")
            freeze_table = freeze_result.get("freeze_table")  # [A0] 句柄带表标记 · settle 时显式回传免猜
            print(f"[SSE-Billing] freeze ok user={user_id} freeze_id={freeze_id} amount={freeze_result.get('amount')} task_ref={task_ref}")
        except HTTPException as he:
            # 402 余额不足 → SSE error · 不开任务 · 走机器合同(去充值/减少词条出口)
            err_detail = he.detail if isinstance(he.detail, dict) else {"message": str(he.detail)}
            _a = _gov_alerts.monitoring_insufficient_points_alert(
                required=err_detail.get("required"), available=err_detail.get("available"),
            )
            yield f"data: {json.dumps({'type': 'error', 'error': _a['message'], **_a}, ensure_ascii=False, default=str)}\n\n"
            print(f"[SSE-Billing] freeze 402 user={user_id}: {err_detail}")
            return
        except Exception as fex:
            # fex(含异常类/栈)只进日志,不进用户可见文案。
            _a = _gov_alerts.monitoring_billing_error_alert(str(fex)[:200])
            yield f"data: {json.dumps({'type': 'error', 'error': _a['message'], **_a}, ensure_ascii=False, default=str)}\n\n"
            print(f"[SSE-Billing] freeze 异常 user={user_id}: {fex}")
            traceback.print_exc()
            return

    # 创建任务记录（直接传 brand_id，不再事后 UPDATE）
    # [CTO-15.23 2026-05-07 P0-2] 显式标 trigger_type='manual_sse' · 不让 default 漏归类
    task_id = None
    try:
        task_id = create_monitoring_task(
            client_id=client_id_compat,
            brand_id=brand_id,
            keyword_ids=[k["id"] for k in keywords],  # [#1 2026-06-07] 废弃 request.keyword_ids 真值开关 · 任务记录恒按实跑词集(keyword_count 准确)
            concurrency=request.concurrency or 10,
            trigger_type="manual_sse",
            planned_test_count=total_tasks,
            planned_platform_count=len(platforms),
        )

        # Provider 调用前一次性持久化完整 classic4 矩阵。未购买/不可执行的平台也以
        # unavailable 单元存在，因此首屏和刷新恢复都不会把晚启动误判为“丢失”。
        fulfillment_credential = str(uuid.uuid4())
        initial_fulfillment_state = "admin_covered" if is_admin else "reserved"
        durable_cells = create_monitoring_run_cells(
            task_id=task_id,
            brand_id=brand_id,
            keywords=keywords,
            search_mode=request.search_mode or "enhanced",
            fulfillment_credential=fulfillment_credential,
            fulfillment_state=initial_fulfillment_state,
            retry_coverage={
                "policy_version": "monitoring-retry-v1",
                "coverage": "included",
                "max_attempts": 1,
            },
            settlement_reference=task_ref,
        )
    except Exception as plan_error:
        if task_id is not None:
            try:
                update_task_status(task_id, "failed")
            except Exception:
                pass
        if freeze_id is not None:
            try:
                from middleware.billing import release_freeze
                await release_freeze(
                    task_ref=task_ref,
                    reason=f"监测计划创建失败: {str(plan_error)[:100]}",
                    user_id=user_id,
                    freeze_table=freeze_table,
                )
            except Exception:
                pass
        plan_error_summary = summarize_monitoring_plan_error(plan_error)
        traceback.print_exc()
        print(f"[SSE-Monitoring] plan 创建失败 task={task_id} brand={brand_id}: {plan_error_summary}")
        yield f"data: {json.dumps({'type': 'error', 'error': '监测执行计划创建失败，本次未开始调用平台', 'plan_error': plan_error_summary}, ensure_ascii=False, default=str)}\n\n"
        return
    public_cells = [
        {
            "id": cell["id"],
            "task_id": cell["task_id"],
            "keyword_id": cell["keyword_id"],
            "keyword_source": cell["keyword_source"],
            "keyword": cell["keyword_snapshot"],
            "platform": cell["platform"],
            "is_planned": cell["is_planned"],
            "state": cell["state"],
            "plan_hash": cell["plan_hash"],
            "error_code": cell.get("error_code"),
        }
        for cell in durable_cells
    ]
    start_event = {
        "type": "start",
        "task_id": task_id,
        "total": total_tasks,
        "keywords_count": len(keywords),
        # [P0-2 · 2026-07-26] 报本次真实解析出的平台列表。旧版硬写 classic4 常量，
        # 是引擎清单双源的第二处：客户在监测面板看到的引擎名和实际跑的可以不一致。
        "platforms": list(platforms),
        "keywords": [k["keyword"] for k in keywords],
        "cells": public_cells,
        "freeze_id": freeze_id,
    }
    yield f"data: {json.dumps(start_event, ensure_ascii=False, default=str)}\n\n"

    # [CTO-15.23 P0] 主执行段 try/except · 异常时 release_freeze
    # [audit P1 2026-06-10] _sse_completed/engine_tasks 供 finally settle:断连(GeneratorExit/
    # CancelledError=BaseException)时 except Exception 接不住,旧版 settle 块整体被跳过
    execution_error = None
    _sse_completed = False
    provider_work_started = False
    engine_tasks: list = []
    update_task_status(task_id, "running")

    # 记录操作日志：启动监测
    log_operation(
        operator_id=client_id_compat,
        brand_id=brand_id,
        action="start_monitoring",
        target_type="task",
        target_id=task_id,
        details={"keywords_count": len(keywords), "platforms": platforms, "search_mode": request.search_mode}
    )

    try:

        # 构建所有监测任务
        # P0.8 (CTO-15.9 2026-04-25 Codex bug 4 修): 优先读 kw.monitoring_query · fallback build_question
        search_mode = request.search_mode or "enhanced"
        monitoring_tasks = []
        for kw in keywords:
            question = resolve_monitoring_query(kw)
            for platform in kw["_eligible_monitoring_platforms"]:
                monitoring_tasks.append(MonitoringTask(
                    keyword_id=kw["id"],
                    keyword=kw["keyword"],
                    target_brand=kw["target_brand"],
                    platform=platform,
                    question=question,
                    search_mode=search_mode,
                    keyword_source=kw.get("source") or "unknown",
                ))
    
        start_time = time.time()
        completed_count = 0
        detected_count = 0
        valid_count = 0
        pending_count = 0
        error_count = 0
        # 监测任务无上下文依赖，可全并行；默认并发提高到20
        concurrency = request.concurrency or config.get("default_concurrency", 20)
        semaphore = asyncio.Semaphore(concurrency)
        results_queue = asyncio.Queue()
        cell_lookup = {
            (cell["keyword_source"], int(cell["keyword_id"]), cell["platform"]): cell
            for cell in durable_cells
            if cell["is_planned"]
        }
    
        async def run_single(task: MonitoringTask):
            """Lease, dispatch and durably finish exactly one planned cell."""
            nonlocal provider_work_started
            cell = cell_lookup[(task.keyword_source, int(task.keyword_id), task.platform)]
            claim = None
            dispatched = False
            try:
                claim = claim_monitoring_run_cell(
                    cell_id=int(cell["id"]), task_id=task_id, brand_id=brand_id,
                    allowed_state="queued", lease_seconds=300,
                )
                await results_queue.put({
                    "_event": "cell_state",
                    "cell_id": int(cell["id"]),
                    "state": "running",
                    "keyword": task.keyword,
                    "platform": task.platform,
                })
                async with semaphore:
                    mark_monitoring_cell_dispatched(
                        cell_id=int(cell["id"]), claim_token=str(claim["claim_token"])
                    )
                    dispatched = True
                    provider_work_started = True
                    result = await PlatformAdapter.query(
                        platform=task.platform,
                        question=task.question,
                        target_brand=task.target_brand,
                        search_mode=task.search_mode,
                        caller="monitoring",
                        brand_id=brand_id,
                        quote_id=cell.get("quote_id"),
                        user_id=user_id,
                        monitoring_task_id=task_id,
                        keyword_id=task.keyword_id,
                        keyword=task.keyword,
                    )
                result.update({
                    "keyword_id": task.keyword_id,
                    "keyword": task.keyword,
                    "platform": task.platform,
                    "keyword_source": task.keyword_source,
                    "keyword_type": task.keyword_type,
                    "keyword_resolver_status": task.keyword_resolver_status,
                    "sent_question_snapshot": task.question,
                    "target_brand": task.target_brand,
                    "cell_id": int(cell["id"]),
                    "plan_hash": cell["plan_hash"],
                })

                if result.get("status") == "error":
                    error_code = str(result.get("error_code") or "platform_unavailable")
                    if error_code in {"platform_unavailable", "provider_outcome_unknown"}:
                        cell_state = "pending_provider_confirmation"
                    elif error_code == "platform_not_supported":
                        cell_state = "unavailable"
                    else:
                        cell_state = "failed"
                    finish_monitoring_cell_error(
                        cell_id=int(cell["id"]),
                        claim_token=str(claim["claim_token"]),
                        state=cell_state,
                        error_code=error_code,
                        error_message=str(result.get("error") or "该平台本次未返回可用结果"),
                    )
                    result["cell_state"] = cell_state
                else:
                    result_id = save_monitoring_result(
                        task_id=task_id,
                        keyword_id=task.keyword_id,
                        keyword=task.keyword,
                        platform=task.platform,
                        is_detected=bool(result.get("is_detected")),
                        mention_type=result.get("mention_type") or "none",
                        response_snippet=result.get("response_snippet") or "",
                        full_response=result.get("full_response") or "",
                        search_citations=result.get("search_citations") or "",
                        competitors_mentioned=result.get("competitors_mentioned") or [],
                        lineage=result,
                        identity_brand_id=brand_id,
                        identity_candidates=result.get("identity_candidates") or [],
                        identity_evidence_snippet=result.get("identity_evidence_snippet") or "",
                        identity_review_state=(
                            "pending" if result.get("status") == "pending_identity" else "not_required"
                        ),
                        cell_id=int(cell["id"]),
                        cell_claim_token=str(claim["claim_token"]),
                    )
                    result["result_id"] = result_id
                    result["cell_state"] = (
                        "pending_identity" if result.get("status") == "pending_identity" else "succeeded"
                    )
            except BaseException as exc:
                # A sent request without a durable result is provider-unknown. It is
                # deliberately not retryable until reconciliation confirms safety.
                result = {
                    "status": "error",
                    "error": "执行中断，已保留单格状态",
                    "error_code": (
                        "provider_outcome_unknown" if dispatched else "worker_lost_before_dispatch"
                    ),
                    "is_detected": False,
                    "keyword_id": task.keyword_id,
                    "keyword": task.keyword,
                    "platform": task.platform,
                    "keyword_source": task.keyword_source,
                    "cell_id": int(cell["id"]),
                    "plan_hash": cell["plan_hash"],
                    "cell_state": (
                        "pending_provider_confirmation" if dispatched else "failed"
                    ),
                }
                if claim is not None:
                    try:
                        finish_monitoring_cell_error(
                            cell_id=int(cell["id"]),
                            claim_token=str(claim["claim_token"]),
                            state=result["cell_state"],
                            error_code=result["error_code"],
                            error_message=str(exc)[:2000],
                        )
                    except Exception:
                        pass
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                    raise
            await results_queue.put(result)
    
        # CTO-15.23 2026-05-25 B 方案 · per-platform fail/total 计数 · 检测某引擎全 fail(如 KIMI 欠费)
        per_platform_fail: Dict[str, int] = {}
        per_platform_total: Dict[str, int] = {}

        # 启动所有任务
        tasks = [asyncio.create_task(run_single(task)) for task in monitoring_tasks]
        engine_tasks = tasks  # [audit P1 2026-06-10] finally 断连收尸用(cancel 未完成引擎调用止损)

        # 边执行边yield结果
        while completed_count < total_tasks:
            result = await results_queue.get()
            if result.get("_event") == "cell_state":
                yield f"data: {json.dumps({'type': 'cell', **result}, ensure_ascii=False, default=str)}\n\n"
                continue
            completed_count += 1
            _platform = result.get("platform", "_unknown")
            per_platform_total[_platform] = per_platform_total.get(_platform, 0) + 1
            if result.get("status") == "error":
                error_count += 1
                per_platform_fail[_platform] = per_platform_fail.get(_platform, 0) + 1
            elif result.get("status") == "pending_identity":
                pending_count += 1
            else:
                valid_count += 1
                if result.get("is_detected"):
                    detected_count += 1
        
            # 发送detail事件
            detail_event = {
                "type": "detail",
                "completed": completed_count,
                "total": total_tasks,
                "keyword": result["keyword"],
                "platform": result["platform"],
                "status": result.get("status", "success"),
                "cell_id": result.get("cell_id"),
                "cell_state": result.get("cell_state"),
                "plan_hash": result.get("plan_hash"),
                "error": result.get("error", ""),
                "error_code": result.get("error_code", ""),
                "detected": result.get("is_detected", False),
                "snippet": result.get("response_snippet", "")[:200],
                "full_response": result.get("full_response", "")  # 完整AI回复
            }
            full_resp = result.get("full_response", "")
            print(f"[SSE] Yielding detail event: {completed_count}/{total_tasks}, full_response len: {len(full_resp)}")
            yield f"data: {json.dumps(detail_event, ensure_ascii=False, default=str)}\n\n"
    
        # 等待所有任务完成（2026-04-17 P1-2: return_exceptions 防一败全败）
        _gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for _r in _gathered:
            if isinstance(_r, Exception):
                print(f"[Monitoring] 单个引擎任务异常（其他已完成保留）: {_r}")

        # 统计结果
        elapsed = round(time.time() - start_time, 1)
        detection_rate = round(detected_count / valid_count * 100, 1) if valid_count > 0 else 0

        summary = {
            "task_id": task_id,
            "attempted_tests": total_tasks,
            "total_tests": valid_count,
            "error_count": error_count,
            "pending_identity_count": pending_count,
            "detected_count": detected_count,
            "detection_rate": detection_rate,
            "elapsed_seconds": elapsed,
            "completed_at": datetime.now().isoformat()
        }

        update_task_status(task_id, "completed", valid_count + pending_count, summary)
        # 全部 planned cell 已到耐久终态；本次履约仅结算一次。失败 cell 的
        # 后续人工重试复用同一 fulfillment_credential，不再走任何 freeze/deduct。
        # 之后的蒸馏/通知/达标/埋点全是"不影响监测结果"附属段 → 其间断连仍应 commit 不应 release
        _sse_completed = True

        # ========== 趋势统计聚合 ==========
        # 手动监测（trigger_type=manual）：不自动同步趋势，等用户确认后手动点"同步数据"
        # 自动监测（trigger_type=scheduled）：不走这个流程（在 scheduler.py 中自动同步）
        print(f"[Trend] 手动监测完成，趋势数据待用户确认后同步（task_id={task_id}）")
    
        # ========== DDS: 异步触发数据蒸馏（不阻塞 SSE 流） ==========
        try:
            from tools.distillation.trigger import trigger_distillation
            if brand_id:
                asyncio.create_task(trigger_distillation(task_id, brand_id))
                print(f"[DDS] 已调度蒸馏任务: task_id={task_id}, brand_id={brand_id}")
            else:
                print(f"[DDS] 缺少 brand_id，跳过蒸馏")
        except Exception as e:
            print(f"[DDS] 调度蒸馏失败（不影响监测结果）: {e}")
    
        # ========== 自动创建通知 ==========
        try:
            from db.notifications import create_notification
        
            # 根据检出率决定通知类型和内容
            if valid_count == 0 and pending_count > 0:
                notification_type = "report"
                content = f"监测已完成，{pending_count} 项品牌名称等待确认；确认前不计入检出率。"
            elif detection_rate >= 70:
                notification_type = "report"
                content = f"监测完成! 检出率 {detection_rate}%，共检测 {total_tasks} 次，品牌被提及 {detected_count} 次。"
            elif detection_rate >= 30:
                notification_type = "alert"
                content = f"注意：检出率 {detection_rate}% 低于预期，建议优化GEO内容策略。"
            else:
                notification_type = "alert"
                content = f"⚠️ 告警：检出率仅 {detection_rate}%，品牌曝光严重不足，需立即关注！"
        
            create_notification(
                brand_id=brand_id,  # ✅ brand_id 统一化
                type=notification_type,
                title=f"监测报告 #{task_id} - 检出率 {detection_rate}%",
                content=content,
                related_id=task_id
            )
            print(f"[Notification] 已为 brand_id={brand_id} 创建 {notification_type} 通知")
        except Exception as e:
            print(f"[Notification] 创建通知失败: {e}")
    
        # ========== 达标判定（历史平滑算法） ==========
        try:
            from db.monitoring_db import run_daily_compliance_check
            compliance_result = run_daily_compliance_check()
            print(f"[Compliance] 监测后自动判定: {compliance_result}")
        except Exception as e:
            print(f"[Compliance] 达标判定失败（不影响监测结果）: {e}")

        # M1a T4 monitor 主路径埋点(CTO-15.23 2026-05-25 C 方案 A3)
        # · SSE 是代理在 /monitoring 主动监测主路径 · 不经 batch_monitor.run_client_monitoring · 之前漏埋点
        if brand_id:
            try:
                from db.pipeline_stage_log_db import log_stage_event
                log_stage_event(
                    brand_id=brand_id,
                    stage_name="monitor",
                    event="complete",
                    meta={
                        "task_id": task_id,
                        "keyword_count": len(keywords),
                        "platform_count": len(platforms),
                        "total_tests": total_tasks,
                        "detected_count": detected_count,
                        "detection_rate": detection_rate,
                        "source": "monitoring_sse",
                    },
                    actor_user_id=user_id,
                )
            except Exception as _le:
                print(f"[SSE] stage_log 埋点失败(非阻塞): {_le}")

        # 发送完成事件
        complete_event = {
            "type": "complete",
            "status": "success",
            "task_id": task_id,
            "summary": summary,
            "cells": list_monitoring_run_cells(task_id, brand_id=brand_id),
        }
        yield f"data: {json.dumps(complete_event, ensure_ascii=False, default=str)}\n\n"
    except Exception as exec_err:
        # [CTO-15.23 P0] 主执行段异常 · 标记 + SSE error · billing 在 finally settle
        execution_error = exec_err
        print(f"[SSE] 主执行段异常: {exec_err}")
        traceback.print_exc()
        # [工单 2026-07-29 T3 §3.4 根因修] 已到终态 completed 之后**绝不降级**成 failed。
        #
        # 真因(Owner 截图 MONITOR-1507 相隔 10 秒两条矛盾通知):
        #   上面 `update_task_status(task_id, "completed", ...)` 之后还有一大段附属工作
        #   (蒸馏调度 / 品牌通知 / 达标判定 / 埋点 / list_monitoring_run_cells 组 complete
        #   事件)。这些块的注释与 billing 口径都写明「不影响监测结果」,`_sse_completed`
        #   也已置 True,但其中 `list_monitoring_run_cells(...)` 与 json.dumps 并没有各自的
        #   try —— 一旦抛错就冒到这里,把已经 completed 的任务改写成 failed,
        #   于是 update_task_status 又发一条 MONITORING_FAILED 通知(event_key 里
        #   terminal_state 不同 → 不被既有幂等键拦住)→ 客户看到"已完成"后 10 秒
        #   再来一条"未完成"。
        #
        # 所以这不是"通知重复投递",是**状态机真的被写反了**;修在写状态这一步,
        # 而不是把后一条通知藏起来。billing 口径不受影响:finally 里
        # `_covered_delivery = _sse_completed or provider_work_started` 本就优先,
        # execution_error 仍照常记录。
        if not _sse_completed:
            try:
                update_task_status(task_id, "failed")
            except Exception:
                pass
        else:
            print(
                f"[SSE] 任务已在终态 completed,附属段异常不降级 task_id={task_id}: {exec_err}"
            )
        yield f"data: {json.dumps({'type': 'error', 'error': str(exec_err)[:300]}, ensure_ascii=False, default=str)}\n\n"
    finally:
        # ==========================================
        # [CTO-15.23 2026-05-05 P0 → audit P1 2026-06-10 finally 化] Billing settle (B 类长任务收尾)
        # · 完整跑完或任一 provider 已派发 → commit_freeze (frozen → consumed)，
        #   已完成格保留、其余失败格由同一履约凭证覆盖单格重试。
        # · provider 尚未派发即失败/断连才 release，避免“已交付部分结果却退款后重跑成功格”。
        # · admin / 无 freeze_id → 跳过 · settle 自身异常只 log
        # [集成 CF-1 2026-06-10] geo-core 的 finally/_settle/shield(BUG-019 断连收口)
        #   + fund-cron 的 A0 freeze_table 显式回传(commit/release 都带·跨表免猜)union 合并
        # ==========================================
        # ① 断连止损:cancel 未完成的引擎调用(旧版断连后 4 引擎继续烧钱,结果只进无人消费的 queue)
        for _et in engine_tasks:
            if not _et.done():
                _et.cancel()
        if engine_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*engine_tasks, return_exceptions=True),
                    timeout=5,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        _covered_delivery = bool(_sse_completed or provider_work_started)
        if provider_work_started and not _sse_completed:
            try:
                from db.monitoring_db import refresh_monitoring_task_from_cells
                refresh_monitoring_task_from_cells(task_id)
            except Exception:
                pass
        if freeze_id is not None:
            async def _settle():
                try:
                    if _covered_delivery:
                        from middleware.billing import commit_freeze
                        # [GEO-R7-CAN-009] commit_freeze 自身抛错时不能只 log:监测输出已交付,
                        #   冻结若不 commit 会悬挂(frozen 既未消费也未释放)。commit_freeze 幂等,
                        #   先有界重试(3 次·递增退避)尽力完成消费;仍失败则冻结【保持 frozen】,
                        #   由 services/freeze_sweeper.py(每小时 · 12h+ zombie 兜底 release)确定性
                        #   回收——落到可恢复的 sweeper 兜底态,而非只留一行 log 静默悬挂。
                        _commit_err = None
                        for _c_attempt in range(3):
                            try:
                                r = await commit_freeze(
                                    task_ref=task_ref,
                                    reason=f"流式监测已产生耐久单格结果 task_id={task_id}",
                                    user_id=user_id,
                                    freeze_table=freeze_table,  # [A0] 显式回传表标记免猜
                                )
                                print(f"[SSE-Billing] commit ok freeze_id={freeze_id} amount={r.get('amount')} reason=success attempt={_c_attempt + 1}")
                                set_monitoring_task_fulfillment_state(task_id, "covered")
                                _commit_err = None
                                break
                            except Exception as _ce:
                                _commit_err = _ce
                                print(f"[SSE-Billing] commit 重试 {_c_attempt + 1}/3 freeze_id={freeze_id} 失败: {_ce}")
                                if _c_attempt < 2:
                                    await asyncio.sleep(0.5 * (_c_attempt + 1))
                        if _commit_err is not None:
                            # [GEO-R7-CAN-009] 重试用尽 · 冻结保持 frozen(可恢复态),交 freeze_sweeper 12h+ 兜底回收
                            print(f"[SSE-Billing] commit 重试用尽 freeze_id={freeze_id} task_ref={task_ref} · 冻结保持 frozen 待 freeze_sweeper 兜底: {_commit_err}")
                            set_monitoring_task_fulfillment_state(task_id, "coverage_unknown")
                            traceback.print_exc()
                    else:
                        if execution_error is not None:
                            _why, _tag = f"流式监测失败: {str(execution_error)[:100]}", "exec_error"
                        else:
                            _why, _tag = f"流式监测未派发即中断 task_id={task_id}", "disconnected_before_dispatch"
                            try:
                                update_task_status(task_id, "failed")  # 断连僵尸 running 行收尸
                            except Exception:
                                pass
                        from middleware.billing import release_freeze
                        r = await release_freeze(task_ref=task_ref, reason=_why, user_id=user_id, freeze_table=freeze_table)
                        set_monitoring_task_fulfillment_state(task_id, "released")
                        print(f"[SSE-Billing] release ok freeze_id={freeze_id} amount={r.get('amount')} reason={_tag}")
                except Exception as bex:
                    print(f"[SSE-Billing] settle 异常 freeze_id={freeze_id} task_ref={task_ref}: {bex}")
                    traceback.print_exc()
            # 独立 task + shield:外层 GeneratorExit/CancelledError 不把 settle 自身二次打断
            _settle_task = asyncio.ensure_future(_settle())
            try:
                await asyncio.shield(_settle_task)
            except BaseException:
                pass  # settle 在独立 task 上继续完成;外层关闭异常由生成器机制继续传播


async def api_retry_monitoring_cell(
    *, task_id: int, cell_id: int, brand_id: int, request_id: str,
    expected_plan_hash: str, user_id: int | None,
) -> Dict[str, Any]:
    """Retry one failed cell under its immutable original fulfillment coverage."""
    from db.monitoring_db import (
        complete_monitoring_cell_retry_request,
        finish_monitoring_cell_error,
        list_monitoring_run_cells,
        mark_monitoring_cell_dispatched,
        refresh_monitoring_task_from_cells,
        reserve_monitoring_cell_retry,
        save_monitoring_result,
    )

    reservation = reserve_monitoring_cell_retry(
        task_id=task_id,
        cell_id=cell_id,
        brand_id=brand_id,
        request_id=request_id,
        expected_plan_hash=expected_plan_hash,
        lease_seconds=300,
    )
    request_row = reservation["request"]
    cell = reservation["cell"]
    if reservation["replay"]:
        response = request_row.get("response_snapshot") or {}
        return {
            "status": "in_progress" if request_row.get("status") == "running" else "replayed",
            "request_id": request_id,
            "data": response,
            "cell": cell,
        }

    claim_token = str(request_row["claim_token"])
    dispatched = False
    try:
        mark_monitoring_cell_dispatched(cell_id=cell_id, claim_token=claim_token)
        dispatched = True
        result = await PlatformAdapter.query(
            platform=cell["platform"],
            question=cell["question_snapshot"],
            target_brand=cell["target_brand_snapshot"],
            search_mode=(cell.get("entitlement_snapshot") or {}).get("search_mode") or "enhanced",
            caller="monitoring_cell_retry",
            brand_id=brand_id,
            quote_id=cell.get("quote_id"),
            user_id=user_id,
            monitoring_task_id=task_id,
            keyword_id=int(cell["keyword_id"]),
            keyword=cell["keyword_snapshot"],
        )
        result.update({
            "task_id": task_id,
            "keyword_id": int(cell["keyword_id"]),
            "keyword": cell["keyword_snapshot"],
            "platform": cell["platform"],
            "keyword_source": cell["keyword_source"],
            "keyword_type": "monitoring",
            "keyword_resolver_status": "resolved",
            "sent_question_snapshot": cell["question_snapshot"],
            "target_brand": cell["target_brand_snapshot"],
        })
        if result.get("status") == "error":
            error_code = str(result.get("error_code") or "platform_unavailable")
            state = (
                "pending_provider_confirmation"
                if error_code in {"platform_unavailable", "provider_outcome_unknown"}
                else ("unavailable" if error_code == "platform_not_supported" else "failed")
            )
            finished = finish_monitoring_cell_error(
                cell_id=cell_id,
                claim_token=claim_token,
                state=state,
                error_code=error_code,
                error_message=str(result.get("error") or "该平台本次未返回可用结果"),
            )
            response = {
                "state": state,
                "error_code": error_code,
                "error": result.get("error") or "该平台本次未返回可用结果",
            }
            complete_monitoring_cell_retry_request(
                request_id=request_id,
                claim_token=claim_token,
                status="failed",
                response_snapshot=response,
            )
            refresh_monitoring_task_from_cells(task_id)
            return {"status": "failed", "request_id": request_id, "data": response, "cell": finished}

        result_id = save_monitoring_result(
            task_id=task_id,
            keyword_id=int(cell["keyword_id"]),
            keyword=cell["keyword_snapshot"],
            platform=cell["platform"],
            is_detected=bool(result.get("is_detected")),
            mention_type=result.get("mention_type") or "none",
            response_snippet=result.get("response_snippet") or "",
            full_response=result.get("full_response") or "",
            search_citations=result.get("search_citations") or "",
            competitors_mentioned=result.get("competitors_mentioned") or [],
            lineage=result,
            identity_brand_id=brand_id,
            identity_candidates=result.get("identity_candidates") or [],
            identity_evidence_snippet=result.get("identity_evidence_snippet") or "",
            identity_review_state=(
                "pending" if result.get("status") == "pending_identity" else "not_required"
            ),
            cell_id=cell_id,
            cell_claim_token=claim_token,
        )
        cell_state = "pending_identity" if result.get("status") == "pending_identity" else "succeeded"
        response = {"state": cell_state, "result_id": result_id}
        complete_monitoring_cell_retry_request(
            request_id=request_id,
            claim_token=claim_token,
            status="succeeded",
            response_snapshot=response,
        )
        summary = refresh_monitoring_task_from_cells(task_id)
        current_cell = next(
            row for row in list_monitoring_run_cells(task_id, brand_id=brand_id)
            if int(row["id"]) == int(cell_id)
        )
        return {
            "status": "success",
            "request_id": request_id,
            "data": response,
            "cell": current_cell,
            "summary": summary,
        }
    except BaseException as exc:
        state = "pending_provider_confirmation" if dispatched else "failed"
        error_code = "provider_outcome_unknown" if dispatched else "worker_lost_before_dispatch"
        try:
            finish_monitoring_cell_error(
                cell_id=cell_id,
                claim_token=claim_token,
                state=state,
                error_code=error_code,
                error_message=str(exc)[:2000],
            )
            complete_monitoring_cell_retry_request(
                request_id=request_id,
                claim_token=claim_token,
                status="blocked" if dispatched else "failed",
                response_snapshot={"state": state, "error_code": error_code},
            )
            refresh_monitoring_task_from_cells(task_id)
        except Exception:
            pass
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            raise
        return {
            "status": "failed",
            "request_id": request_id,
            "data": {"state": state, "error_code": error_code},
        }


async def run_client_monitoring_with_details(
    brand_id: int = None,
    client_id: str = None,
    keyword_ids: List[int] = None,
    platforms: List[str] = None,
    concurrency: int = 10,
    detail_callback = None,
    search_mode: str = "enhanced"
):
    """
    增强版监测函数 - 每完成一个检测就回调详细信息

    ⚠️ [2026-06-07 老板复审] DEPRECATED · 全仓 0 调用方(非 SSE 入口·已被 run-stream / batch_monitor.run_client_monitoring 取代)。
       保留仅作历史参考;brand_id 分支已改走服务锚 SSOT helper·绝不保留绕过服务锚的旧查询。新代码勿调本函数。
    """
    import time
    from datetime import datetime
    from tools.monitoring.batch_monitor import (
        MonitoringTask, MonitoringScheduler, PlatformAdapter, build_question, resolve_monitoring_query
    )
    from db.monitoring_db import (
        get_monitoring_config, get_keywords_for_monitoring,
        create_monitoring_task, update_task_status, batch_save_results,
        _resolve_id
    )
    
    start_time = time.time()
    
    # 获取配置
    config = get_monitoring_config(brand_id=brand_id, client_id=client_id or "_global_")
    platforms_str = config.get("default_platforms", DEFAULT_MONITORING_PLATFORMS)
    
    # 获取词条 - 优先 brand_id 查 quote_ids
    if brand_id is not None:
        # [2026-06-07 老板复审] 统一走服务锚 SSOT helper · 不再自写 `SELECT id FROM quotes WHERE brand_id`(漏服务锚过滤)
        from db.monitoring_db import resolve_service_anchored_quote_ids_for_brand, brand_has_confirmed_or_paid_quote
        quote_ids = resolve_service_anchored_quote_ids_for_brand(brand_id)
        if quote_ids:
            keywords = get_keywords_for_monitoring(quote_ids=quote_ids)
        elif brand_has_confirmed_or_paid_quote(brand_id):
            keywords = []  # 有 quote 但无有效服务锚(脏草稿/已过期)→ 不监测·不进引擎
        else:
            keywords = get_keywords_for_monitoring(brand_id=brand_id)  # 无任何 quote → brand 级 extra_keywords 自助订阅
    elif client_id is not None:
        quote_id = int(client_id)
        keywords = get_keywords_for_monitoring(quote_id=quote_id)
    else:
        keywords = []
    
    if not keywords:
        return {"status": "error", "error": "没有可监测的词条"}

    platform_plan = PlatformAdapter.resolve_keyword_monitoring_plan(
        keywords,
        requested_platforms=platforms,
        configured_platforms=platforms_str,
    )
    if platform_plan["rejected"]:
        return {
            "status": "error",
            "error": "请求包含所选词条未购买或不可用的监测引擎",
            "error_code": "requested_platform_not_entitled",
        }
    keywords = platform_plan["keywords"]
    platforms = platform_plan["platforms"]
    if not keywords:
        return {
            "status": "error",
            "error": "所选词条没有可执行的已购监测引擎",
            "error_code": "no_eligible_monitoring_platforms",
        }
    planned_test_count = sum(
        len(keyword["_eligible_monitoring_platforms"])
        for keyword in keywords
    )
    
    # 创建任务记录（双写 brand_id）
    # [CTO-15.23 2026-05-07 P0-2] 显式标 trigger_type='manual_user' · run_client_monitoring_with_details 是非 SSE 入口
    task_id = create_monitoring_task(
        brand_id=brand_id,
        client_id=client_id,
        keyword_ids=[k["id"] for k in keywords],  # [#1 2026-06-07] 废弃真值开关 · 恒按实跑词集
        concurrency=concurrency,
        trigger_type="manual_user",
        planned_test_count=planned_test_count,
        planned_platform_count=len(platforms),
    )
    
    update_task_status(task_id, "running")

    # 构建所有监测任务
    # P0.8 (CTO-15.9 Codex bug 4): 优先读 kw.monitoring_query · fallback build_question
    monitoring_tasks = []
    for kw in keywords:
        question = resolve_monitoring_query(kw)
        for platform in kw["_eligible_monitoring_platforms"]:
            monitoring_tasks.append(MonitoringTask(
                keyword_id=kw["id"],
                keyword=kw["keyword"],
                target_brand=kw["target_brand"],
                platform=platform,
                question=question,
                search_mode=search_mode,
                keyword_source=kw.get("source") or "unknown",
            ))

    total_count = len(monitoring_tasks)
    completed_count = 0
    results = []
    
    # 逐个执行（为了实时反馈，牺牲一点并发性）
    semaphore = asyncio.Semaphore(concurrency)
    
    async def run_single(task: MonitoringTask) -> dict:
        nonlocal completed_count
        async with semaphore:
            result = await PlatformAdapter.query(
                platform=task.platform,
                question=task.question,
                target_brand=task.target_brand,
                search_mode=task.search_mode,
                caller="monitoring",
                brand_id=brand_id,
                quote_id=int(client_id) if str(client_id or "").isdigit() else None,
                monitoring_task_id=task_id,
                keyword_id=task.keyword_id,
                keyword=task.keyword,
            )
            
            result.update({
                "keyword_id": task.keyword_id,
                "keyword": task.keyword,
                "platform": task.platform,
                "keyword_source": task.keyword_source,
                "keyword_type": task.keyword_type,
                "keyword_resolver_status": task.keyword_resolver_status,
            })

            # [#3-C2 补刀v3 资金·防御] 不在此 save · 只 return · 由 gather 后过失败门禁统一 save
            #   (本函数已 DEPRECATED 0 调用·与 run-stream/scheduler/batch_monitor buffer-then-save 口径一致)
            completed_count += 1

            # 回调详细信息(实时 UI · 与 save 解耦)
            if detail_callback:
                detail_callback({
                    "type": "detail",
                    "completed": completed_count,
                    "total": total_count,
                    "keyword": task.keyword,
                    "platform": task.platform,
                    "detected": result.get("is_detected", False),
                    "snippet": result.get("response_snippet", "")[:200]
                })
            
            return result
    
    # 并发执行（2026-04-17 P1-2: return_exceptions 防一败全败）
    _raw_results = await asyncio.gather(
        *[run_single(task) for task in monitoring_tasks],
        return_exceptions=True,
    )
    results = []
    for _r in _raw_results:
        if isinstance(_r, Exception):
            print(f"[Monitoring] 单个任务异常，跳过统计: {_r}")
            continue
        results.append(_r)

    # [#3-C2 补刀v3 资金·防御] 本函数已 DEPRECATED 0 调用 · buffer-then-save:任一引擎 status=error → 不 save·不标 completed·return failed
    #   (防复活时漏挡 → 半截"部分保存+完成";live 路径 run-stream/batch_monitor/scheduler 均已 buffer-then-save / fail-closed)
    if any(isinstance(r, dict) and r.get("status") == "error" for r in results):
        update_task_status(task_id, "failed")
        return {"status": "error", "error": "监测引擎失败·未完成有效监测", "task_id": task_id}

    # 过失败门禁后才统一 save(全成功·失败已上方 return)
    batch_save_results([
        {
            **r,
            "task_id": task_id,
            "identity_brand_id": brand_id,
            "identity_review_state": (
                "pending" if r.get("status") == "pending_identity" else "not_required"
            ),
        }
        for r in results
    ])

    # 统计结果
    eligible_results = [r for r in results if r.get("status") != "pending_identity"]
    detected_count = sum(1 for r in eligible_results if r.get("is_detected"))
    detection_rate = (
        round(detected_count / len(eligible_results) * 100, 1)
        if eligible_results else 0
    )
    elapsed = round(time.time() - start_time, 1)
    
    summary = {
        "task_id": task_id,
        "attempted_tests": total_count,
        "total_tests": len(eligible_results),
        "detected_count": detected_count,
        "pending_identity_count": total_count - len(eligible_results),
        "detection_rate": detection_rate,
        "elapsed_seconds": elapsed,
        "completed_at": datetime.now().isoformat()
    }
    
    update_task_status(task_id, "completed", total_count, summary)
    
    # ========== 趋势统计聚合：写入 keyword_trend_stats ==========
    try:
        from db.monitoring_db import save_trend_stat, calculate_rate_change
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # 从数据库统计本次任务各词条的结果
        from db.monitoring_db import get_connection as get_mon_conn3
        conn3 = get_mon_conn3()
        cursor3 = conn3.cursor()
        from services.monitoring_identity_review import aggregate_eligible_sql
        cursor3.execute(f"""
            SELECT COALESCE(confirmed_keyword_id, keyword_id) AS keyword_id,
                   COUNT(*) as tests,
                   SUM(CASE WHEN is_detected = 1 THEN 1 ELSE 0 END) as detected
            FROM monitoring_results
            WHERE task_id = %s AND {aggregate_eligible_sql()}
            GROUP BY COALESCE(confirmed_keyword_id, keyword_id)
        """, (task_id,))

        trend_count = 0
        for row in cursor3.fetchall():
            kid = row["keyword_id"]
            tests = row["tests"]
            detected_cnt = row["detected"]
            if tests == 0:
                continue
            # 找到对应的 source
            source = "confirmed"
            for kw in keywords:
                if kw["id"] == kid:
                    source = kw.get("source", "confirmed")
                    break
            current_rate = round(detected_cnt / tests * 100, 1)
            rate_change = calculate_rate_change(kid, source, current_rate, "daily")
            save_trend_stat(
                keyword_id=kid,
                keyword_source=source,
                period_type="daily",
                period_date=today_str,
                test_count=tests,
                detected_count=detected_cnt,
                rate_change=rate_change
            )
            trend_count += 1
        conn3.close()
        print(f"[Trend] 已写入 {trend_count} 条趋势统计")
    except Exception as e:
        print(f"[Trend] 写入趋势统计失败（不影响监测结果）: {e}")
    
    return {"status": "success", **summary}


def api_get_tasks(
    brand_id: int = None, client_id: str = None, limit: int = 20,
    before_created_at=None, before_id: int = None,
) -> dict:
    """获取任务列表（优先 brand_id）"""
    tasks = get_tasks(
        brand_id=brand_id, client_id=client_id, limit=limit,
        before_created_at=before_created_at, before_id=before_id,
    )
    return {
        "status": "success",
        "count": len(tasks),
        "tasks": tasks
    }


def api_get_task_detail(task_id: int) -> dict:
    """获取任务详情+结果"""
    task = get_task(task_id)
    if not task:
        return {"status": "error", "error": "任务不存在"}
    
    results = get_task_results(task_id)
    from db.monitoring_db import list_monitoring_run_cells
    cells = list_monitoring_run_cells(task_id, brand_id=task.get("brand_id"))
    
    return {
        "status": "success",
        "task": task,
        "results": results,
        "cells": cells,
    }


# ==========================================
# 数据回退API
# ==========================================

def api_get_rollback_tasks(
    brand_id: int, limit: int = 20, before_created_at=None, before_id: int = None
) -> dict:
    """获取可回退的监测任务列表"""
    from db.monitoring_db import get_rollback_tasks
    tasks = get_rollback_tasks(
        brand_id, limit, before_created_at=before_created_at, before_id=before_id
    )
    return {"status": "success", "tasks": tasks}


def api_rollback_task(task_id: int) -> dict:
    """回退单个监测任务"""
    from db.monitoring_db import rollback_task
    result = rollback_task(task_id)
    if result.get("success"):
        return {"status": "success", **result}
    return {"status": "error", "error": result.get("error", "回退失败")}


def api_rollback_tasks_batch(task_ids: list) -> dict:
    """批量回退多个监测任务"""
    from db.monitoring_db import rollback_tasks_batch
    result = rollback_tasks_batch(task_ids)
    return {"status": "success", **result}


def api_sync_task_trends(task_id: int) -> dict:
    """手动同步监测任务的趋势数据"""
    from db.monitoring_db import sync_task_trends
    result = sync_task_trends(task_id)
    if result.get("success"):
        return {"status": "success", **result}
    return {"status": "error", "error": result.get("error", "同步失败")}


# ==========================================
# 趋势统计API（新增）
# ==========================================

def api_get_keyword_trend(
    keyword_id: int,
    keyword_source: str = "confirmed",
    period_type: str = "daily",
    limit: int = 30
) -> dict:
    """获取关键词趋势数据"""
    trend_data = get_keyword_trend(keyword_id, keyword_source, period_type, limit)
    return {
        "status": "success",
        "count": len(trend_data),
        "trend": trend_data
    }


# ==========================================
# Token消耗API（新增）
# ==========================================

def api_get_token_usage(
    quote_id: int = None,
    start_date: str = None,
    end_date: str = None
) -> dict:
    """获取Token消耗统计"""
    usage = get_token_usage_summary(quote_id, start_date, end_date)
    return {
        "status": "success",
        "usage": usage
    }


# ==========================================
# 客户Token管理API（Phase 1.6）
# ==========================================

class TokenGenerateRequest(BaseModel):
    quote_id: int
    brand_name: str = None
    days_valid: int = 30


def api_generate_token(
    request: TokenGenerateRequest,
    *,
    actor_user_id: Optional[int] = None,
    actor_username: Optional[str] = None,
    request_id: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> dict:
    """生成客户访问Token。

    [WP7 #4] 传入 actor 上下文时,generate_client_token 会在同一事务内为本次门户
    token 轮换写审计(补 owner/成员/老服务商自助轮换的审计缺口)。actor 缺省时行为不变。
    """
    try:
        result = generate_client_token(
            request.quote_id,
            request.brand_name,
            request.days_valid,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            request_id=request_id,
            ip_address=ip_address,
        )
        return {"status": "success", "data": result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def api_verify_token(token: str) -> dict:
    """验证Token"""
    quote_id = verify_client_token(token)
    if quote_id:
        # 获取品牌名称和brand_id
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT brand_name, brand_id FROM quotes WHERE id = %s", (quote_id,))
        row = cursor.fetchone()
        brand_name = row["brand_name"] if row else ""
        brand_id = row["brand_id"] if row else None
        conn.close()

        # v3.6 白标：客户门户 token 校验下发归属代理品牌（surface 写死 customer · 脱敏）
        # 契约见 EXEC_WHITELABEL_V36_FULL_2026-05-29.md §0「客户页后端下发契约」
        _wl_payload = {"whitelabel": None, "display_scope": "platform"}
        try:
            from services.public_whitelabel import get_public_whitelabel_data
            _wl_payload = get_public_whitelabel_data(
                quote_id=quote_id,
                brand_id=brand_id,
            )
        except Exception:
            pass

        return {
            "status": "success",
            "valid": True,
            "quote_id": quote_id,
            "brand_id": brand_id,
            "brand_name": brand_name,
            "whitelabel": _wl_payload.get("whitelabel"),
            "branding_status": _wl_payload.get("display_scope", "platform"),
            # [audit #10 返修] 去 owner_user_id:白标已内联下发 · 门户用 quote_id 解析白标(防 brand→agent 串联枚举)。
        }
    return {"status": "error", "valid": False, "message": "无效或已过期的Token"}


def api_get_client_token_info(quote_id: int) -> dict:
    """获取客户Token信息"""
    token_info = get_client_token(quote_id)
    return {"status": "success", "token": token_info}


# ==========================================
# 媒体投放API（Phase 1.6）
# ==========================================

class PublicationAddRequest(BaseModel):
    quote_id: int
    platform_name: str
    platform_url: str = None
    article_title: str = None
    publish_date: str = None
    operator_id: str = None


def api_add_publication(request: PublicationAddRequest) -> dict:
    """添加媒体投放记录"""
    try:
        pub_id = add_publication(
            request.quote_id,
            request.platform_name,
            request.platform_url,
            request.article_title,
            request.publish_date,
            request.operator_id
        )
        return {"status": "success", "id": pub_id}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def api_get_publications(quote_id: int, limit: int = 100) -> dict:
    """获取媒体投放列表"""
    pubs = get_publications(quote_id, limit)
    stats = get_publication_stats(quote_id)
    return {
        "status": "success",
        "count": len(pubs),
        "stats": stats,
        "publications": pubs
    }


# ==========================================
# 操作日志API（Phase 1.6）
# ==========================================

class LogRequest(BaseModel):
    operator_id: str
    brand_id: Optional[int] = None
    action: str
    target_type: str = None
    target_id: int = None
    details: dict = None


def api_log_operation(request: LogRequest) -> dict:
    """记录操作"""
    log_id = log_operation(
        request.operator_id,
        request.action,
        request.target_type,
        request.target_id,
        request.details,
        brand_id=request.brand_id,
    )
    return {"status": "success", "id": log_id}


def api_get_logs(operator_id: str = None, target_type: str = None, limit: int = 100, brand_id: int = None) -> dict:
    """查询操作日志(单 operator_id · 老接口保留 · admin 走这条)"""
    logs = get_operation_logs(operator_id=operator_id, target_type=target_type, brand_id=brand_id, limit=limit)
    return {"status": "success", "count": len(logs), "logs": logs}


def api_get_logs_multi(operator_ids: List[str], target_type: str = None, limit: int = 100, brand_id: int = None) -> dict:
    """查询操作日志(多 operator_id 池 · CTO-15.23 2026-05-25)

    · 非 admin 走这条 · 池 = [user_id, ...该 user 拥有的所有 brand_id]
    · 兼容历史 5 处 monitoring_api 写入 operator_id=brand_id 或 'system' 的脏数据
    """
    from db.monitoring_db import get_operation_logs_multi
    logs = get_operation_logs_multi(operator_ids=operator_ids, target_type=target_type, brand_id=brand_id, limit=limit)
    return {"status": "success", "count": len(logs), "logs": logs}


# ==========================================
# 报告生成API（Phase 2.0）
# ==========================================

from datetime import datetime, timedelta

def _to_date_str(val) -> str:
    """将 datetime 或字符串统一转为 YYYY-MM-DD 字符串"""
    if hasattr(val, 'strftime'):
        return val.strftime("%Y-%m-%d")
    return str(val).split("T")[0].split(" ")[0]


# 套餐 → 目标检出率映射
# [WO §8 2026-08-08] 这张表原来只有 entry/standard/**premium** —— 而档位码的 SSOT
#   (api/selection_api.TIER_CONFIG)用的是 **flagship**。于是 tier='flagship' 的报价
#   走 .get(code, standard) **静默按 65% 算**,客户买的是 75%。
#   🔴 修法不是"改名成 flagship":生产实测 quotes.tier 里 premium 还剩 1 条**且正在被监测**,
#      改名会把那家从 75% 打回 65%。所以走单点 + legacy 别名,两个码都认。
#   数值不再手抄,从 SSOT 的 ai_probability 派生。
from services.monitoring_tier_target import tier_target as _tier_target_ssot


def _get_tier_target(brand_id: int = None, client_id: str = None) -> dict:
    """获取品牌的套餐目标检出率"""
    try:
        from db.diagnosis_db import get_connection
        scope = _resolve_quote_scope(brand_id=brand_id, client_id=client_id)
        quote_ids = scope["quote_ids"]
        if not quote_ids:
            return {"tier": "standard", "tier_name": "标准版", "target_rate": 65}

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT tier FROM quotes WHERE id = %s LIMIT 1", (quote_ids[0],))
            row = cur.fetchone()
            conn.close()
            if row:
                # 别名收敛 + 数值取自 SSOT。返回的 tier 是**规范码**
                # (legacy 'premium' 收敛成 'flagship');前端只读 tier_name/target_rate,
                # 不按 tier 码分支(已全仓 grep 核过),所以这一步不改变任何可见行为。
                return _tier_target_ssot(row["tier"])
        finally:
            try:
                conn.close()
            except Exception: pass
    except:
        pass
    return {"tier": "standard", "tier_name": "标准版", "target_rate": 65}


def _aggregate_keyword_stats(tasks: list) -> dict:
    """从任务列表聚合关键词统计（含主题包维度）"""
    from db.monitoring_db import get_aggregate_task_results
    from db.diagnosis_db import get_connection
    keyword_map = {}
    for task in tasks:
        for r in get_aggregate_task_results(task["id"]):
            kw = r.get("keyword", "")
            if not kw:
                continue
            if kw not in keyword_map:
                keyword_map[kw] = {"keyword": kw, "detected": 0, "total": 0, "keyword_id": r.get("keyword_id")}
            keyword_map[kw]["total"] += 1
            if r.get("is_detected"):
                keyword_map[kw]["detected"] += 1
    stats = []
    for kw, s in keyword_map.items():
        rate = round(s["detected"] / s["total"] * 100, 1) if s["total"] > 0 else 0
        stats.append({"keyword": kw, "avg_rate": rate, "tests_count": s["total"], "keyword_id": s.get("keyword_id")})
    stats.sort(key=lambda x: x["avg_rate"], reverse=True)
    top = stats[:5] if len(stats) > 5 else stats
    bottom = sorted(stats, key=lambda x: x["avg_rate"])[:5] if len(stats) > 5 else []

    # 主题包维度聚合
    cluster_stats = []
    keyword_ids = [s["keyword_id"] for s in stats if s.get("keyword_id")]
    if keyword_ids:
        try:
            conn = get_connection()
            cur = conn.cursor()
            placeholders = ",".join(["%s"] * len(keyword_ids))
            cur.execute(f"""
                SELECT ck.id as keyword_id, ck.cluster_id, kc.cluster_name
                FROM confirmed_keywords ck
                LEFT JOIN keyword_clusters kc ON ck.cluster_id = kc.id
                WHERE ck.id IN ({placeholders}) AND ck.cluster_id IS NOT NULL
            """, tuple(keyword_ids))
            cluster_map_rows = cur.fetchall()
            conn.close()

            # keyword_id → (cluster_id, cluster_name)
            kw_cluster = {r["keyword_id"]: (r["cluster_id"], r["cluster_name"] or f"包{r['cluster_id']}") for r in cluster_map_rows}

            # 按cluster聚合
            clusters = {}
            for s in stats:
                kid = s.get("keyword_id")
                if kid and kid in kw_cluster:
                    cid, cname = kw_cluster[kid]
                    if cid not in clusters:
                        clusters[cid] = {"cluster_id": cid, "cluster_name": cname, "detected": 0, "total": 0, "keywords": []}
                    # 回溯原始detected/total
                    orig = keyword_map.get(s["keyword"])
                    if orig:
                        clusters[cid]["detected"] += orig["detected"]
                        clusters[cid]["total"] += orig["total"]
                    clusters[cid]["keywords"].append(s["keyword"])

            for c in clusters.values():
                c["avg_rate"] = round(c["detected"] / c["total"] * 100, 1) if c["total"] > 0 else 0
                c["keyword_count"] = len(c["keywords"])
            cluster_stats = sorted(clusters.values(), key=lambda x: x["avg_rate"], reverse=True)
        except Exception:
            pass

    return {
        "total_keywords": len(stats),
        "keyword_stats": stats,
        "top_keywords": top,
        "bottom_keywords": bottom,
        "cluster_stats": cluster_stats,
    }

def api_generate_report(
    *,
    brand_id: int = None,
    client_id: str = None,
    report_type: str = 'weekly',
    organization_identity=None,
) -> dict:
    """
    生成周报/月报

    Args:
        brand_id: 品牌ID（优先）
        client_id: 客户ID（兼容）
        report_type: 'weekly' | 'monthly'

    Returns:
        报告数据dict

    [2026-05-29 老板 P0 · caller 归因约定]
      当前实现是纯统计聚合 · 无 LLM 调用 · 不入 llm_call_log
      未来若加 LLM 总结(客户复盘 / GEO retrospective)· 必须用以下 caller:
        - monitoring_report_review: 周报/月报/季报/年报 LLM 总结
        - geo_retrospective_report: 客户深度复盘(月度/季度)
      caller 命名族见 services/api_costs.py:KNOWN_CALLERS_FAMILIES
      防"unknown caller"归因污染 dashboard 总运营成本
    """
    scope = _resolve_quote_scope(brand_id=brand_id, client_id=client_id)
    brand_id = scope["brand_id"] or brand_id
    quote_ids = scope["quote_ids"]
    primary_quote_id = quote_ids[0] if quote_ids else None
    client_id = scope["client_id"] or client_id
    if not brand_id and not primary_quote_id:
        return {"status": "error", "error": "缺少 brand_id 或 client_id"}
    
    # 1. 确定时间范围
    today = datetime.now()
    if report_type == 'weekly':
        period_start = today - timedelta(days=7)
        period_label = f"{today.strftime('%Y年%m月')}第{(today.day-1)//7+1}周"
    elif report_type == 'monthly':
        period_start = today - timedelta(days=30)
        period_label = today.strftime('%Y年%m月')
    else:
        period_start = today - timedelta(days=7)
        period_label = '自定义'
    
    # 2-3. Assigned client access is not team-output access.  A member report
    # aggregates only monitoring tasks stamped to that member.
    if organization_identity is not None and organization_identity.is_member:
        from db.connection import get_connection as _get_org_report_connection
        _org_conn = _get_org_report_connection()
        try:
            _org_cur = _org_conn.cursor()
            _org_cur.execute(
                """
                SELECT id FROM monitoring_tasks
                WHERE organization_id=%s AND created_by_membership_id=%s
                  AND brand_id=%s AND status='completed' AND created_at>=%s
                ORDER BY created_at DESC
                """,
                (
                    organization_identity.organization_id,
                    organization_identity.membership_id,
                    brand_id,
                    period_start,
                ),
            )
            _org_tasks = [dict(row) for row in _org_cur.fetchall()]
        finally:
            _org_conn.close()
        if not _org_tasks:
            return {"status": "error", "error": "本账号在该周期暂无可用监测产物"}
        _org_stats = _aggregate_keyword_stats(_org_tasks)
        keyword_stats = list(_org_stats.get("keyword_stats") or [])
        keywords = keyword_stats
        total_rate = sum(float(item.get("avg_rate") or 0) for item in keyword_stats)
    else:
        # [WO_MONITORING_OPTIN 2026-08-15] 周/月报是**渲染历史监测结果**,不是取词去跑。
        #   加开关闸会让代理关掉的词连同它已跑出、客户已付费的历史数据一起从报告里消失
        #   —— 那是数据丢失不是省钱。故显式退出闸;默认值 True 仍然保护所有新调用方。
        keywords = get_keywords_for_monitoring(
            quote_ids=quote_ids, brand_id=brand_id, for_dispatch=False
        )
        if not keywords:
            return {"status": "error", "error": "该客户暂无监测词条"}
        keyword_stats = []
        total_rate = 0
        for kw in keywords:
            trend = get_keyword_trend(kw['id'], 'extra' if kw.get('source') == 'extra' else 'confirmed', 'daily', 7 if report_type == 'weekly' else 30)
            avg_rate = 0
            if trend:
                rates = [t['detection_rate'] for t in trend if t.get('detection_rate') is not None]
                avg_rate = sum(rates) / len(rates) if rates else 0
            keyword_stats.append({
                'keyword': kw['keyword'],
                'target_brand': kw.get('target_brand', ''),
                'avg_rate': round(avg_rate, 1),
                'tests_count': len(trend) if trend else 0
            })
            total_rate += avg_rate
    
    # 4. 汇总数据
    avg_detection_rate = round(total_rate / len(keywords), 1) if keywords else 0
    
    # 获取媒体投放数
    publications = (
        []
        if organization_identity is not None and organization_identity.is_member
        else _publications_for_quotes(quote_ids)
    )
    
    summary_data = {
        'period_label': period_label,
        'total_keywords': len(keywords),
        'avg_detection_rate': avg_detection_rate,
        'total_publications': len(publications) if publications else 0,
        'keyword_stats': keyword_stats,
        'top_keywords': sorted(keyword_stats, key=lambda x: x['avg_rate'], reverse=True)[:3],
        'bottom_keywords': sorted(keyword_stats, key=lambda x: x['avg_rate'])[:3],
        'generated_at': datetime.now().isoformat()
    }
    
    # 5. 保存报告
    report_id = save_report(
        brand_id=brand_id,
        client_id=client_id or (str(primary_quote_id) if primary_quote_id else None),
        report_type=report_type,
        period_start=period_start.strftime('%Y-%m-%d'),
        period_end=today.strftime('%Y-%m-%d'),
        summary_data=summary_data,
        organization_identity=organization_identity,
    )
    
    return {
        "status": "success",
        "report_id": report_id,
        "summary": summary_data
    }


def api_get_reports(*, brand_id: int = None, client_id: str = None, report_type: str = None, limit: int = 20) -> dict:
    """获取报告列表"""
    reports = get_reports(brand_id=brand_id, client_id=client_id, report_type=report_type, limit=limit)
    return {
        "status": "success",
        "count": len(reports),
        "reports": reports
    }


def api_export_report(*, brand_id: int = None, client_id: str = None, format: str = 'csv') -> dict:
    """
    导出报告为CSV或Excel格式
    
    Args:
        brand_id: 品牌ID（优先）
        client_id: 客户ID（兼容）
        format: 'csv' | 'excel'
    
    Returns:
        导出数据dict
    """
    import csv
    import io
    
    scope = _resolve_quote_scope(brand_id=brand_id, client_id=client_id)
    brand_id = scope["brand_id"] or brand_id
    quote_ids = scope["quote_ids"]
    if not brand_id and not quote_ids:
        return {"status": "error", "error": "缺少 brand_id 或 client_id"}
    
    # 1. 获取关键词数据
    # [WO_MONITORING_OPTIN 2026-08-15] CSV 导出同报表:渲染历史结果,显式退出取词闸(理由见 generate_report)。
    keywords = get_keywords_for_monitoring(
        quote_ids=quote_ids, brand_id=brand_id, for_dispatch=False
    )
    if not keywords:
        return {"status": "error", "error": "暂无监测数据"}
    
    # 2. 构建表格数据
    rows = []
    for kw in keywords:
        trend = get_keyword_trend(kw['id'], 'extra' if kw.get('source') == 'extra' else 'confirmed', 'daily', 7)
        avg_rate = 0
        if trend:
            rates = [t['detection_rate'] for t in trend if t.get('detection_rate') is not None]
            avg_rate = sum(rates) / len(rates) if rates else 0
        
        rows.append({
            '监测词条': kw['keyword'],
            '目标品牌': kw.get('target_brand', ''),
            '平均出现率': f"{avg_rate:.1f}%",
            '监测次数': len(trend) if trend else 0,
            '最近监测': trend[0]['period_date'] if trend else '-'
        })
    
    # 3. 生成CSV
    id_label = brand_id or client_id or (quote_ids[0] if quote_ids else "")
    if format == 'csv':
        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(output, fieldnames=['监测词条', '目标品牌', '平均出现率', '监测次数', '最近监测'])
            writer.writeheader()
            writer.writerows(rows)
        
        return {
            "status": "success",
            "format": "csv",
            "filename": f"report_{id_label}_{datetime.now().strftime('%Y%m%d')}.csv",
            "content": output.getvalue(),
            "rows": len(rows)
        }
    
    # 4. Excel格式（返回JSON数据，前端用SheetJS处理）
    return {
        "status": "success",
        "format": "json",
        "filename": f"report_{id_label}_{datetime.now().strftime('%Y%m%d')}.xlsx",
        "headers": ['监测词条', '目标品牌', '平均出现率', '监测次数', '最近监测'],
        "data": rows,
        "rows": len(rows)
    }


# ==========================================
# LLM洞察生成API（Phase 2.0）
# ==========================================

import httpx
import os

def _strip_rate_numbers(text: str) -> str:
    """[CTO-15.23 2026-05-11] 兜底剥除 LLM 输出中的"X%"百分比数字 · 防 AI vs UI 打架

    LLM prompt 已明令"禁止写具体数字" · 但 model 偶尔不守 · 加 regex 兜底:
      "出现率 0%/75%/42.3%" → "出现率 -" (后跟"达标/未达标"等定性词时直接吞)
    """
    import re
    # "出现率 X%" or "X%" 单独出现 → 替换为占位符
    # 防误杀:只剥 5 字以内的数字百分比(避免误删 "100% 满意" 之类非监测数据语境)
    cleaned = re.sub(r'\s*\d{1,3}(?:\.\d+)?\s*%', '', text)
    # 双空格 / 多余 · → 收敛
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    cleaned = re.sub(r'·\s*·', '·', cleaned)
    cleaned = re.sub(r'^[·\s]+|[·\s]+$', '', cleaned)
    return cleaned


def _normalize_insight_items(raw_items, default_type: str = 'tip') -> list[dict]:
    """统一 insight item 形态: 每条都是 {text, type} dict.

    兼容 LLM 偶尔输出 string 的情况(老 prompt format / 模型不守格式)。
    type 不在 {positive, warning, tip} 时强制回落到 tip 防前端 emoji 错位。

    [CTO-15.23 2026-05-11] 加 _strip_rate_numbers 兜底 · 剥 LLM 偶尔输出的"X%"
      根因:老板报 AI 洞察"0%"vs UI"75%"打架 · LLM 不守 prompt 禁数字铁律
    """
    valid_types = {'positive', 'warning', 'tip'}
    out = []
    for item in (raw_items or [])[:3]:
        if isinstance(item, dict):
            text = str(item.get('text') or item.get('insight') or '').strip()
            t = str(item.get('type') or default_type).strip().lower()
            if t not in valid_types:
                t = default_type
        else:
            text = str(item or '').strip()
            t = default_type
        # 兜底剥数字 · 防 AI vs UI 打架
        text = _strip_rate_numbers(text)
        if text:
            out.append({"text": text, "type": t})
    return out


async def api_generate_insights(quote_id: int, period: str = 'weekly') -> dict:
    """
    用 DeepSeek 生成数据洞察

    [CTO-15.23 2026-05-11 老板报"AI 洞察 0% 但词条详情 75% 打架"真因根治]
      Bug A (前端 emoji 硬编码 i===0?'✅') · 改 LLM 同时输出 type 让前端按 type 渲染 ✅ 已修
      Bug B (LLM 喂 7 天 daily 均值"曝光为 0"假象) · 改用 get_client_keywords ✅ 已修
      Bug C (今天发现 · 真根因):
        get_client_keywords 返 detection_rate=0%(本期实时单次结果)
        api_get_client_keywords_merged 返 effective_rate=75%(历史滚动平滑)
        UI 用 effective_rate · LLM 用 detection_rate → **完全不同源** · 打架
      修法:LLM 改调 api_get_client_keywords_merged 拿 display_rate(UI 同源字段)
        + LLM prompt 强约束:数字必须照搬 input · 不允许自己生成

    Args:
        quote_id: 客户ID
        period: 'weekly' | 'monthly'(老 param · 保留兼容 · daily 模型下不影响数据源)

    Returns:
        {status, insights: [{text, type}], source}
    """
    # [CTO-15.23 2026-05-11] 改调 merged 函数 · 拿带 display_rate (UI 同源) 的完整数据
    merged = api_get_client_keywords_merged(quote_id)
    keywords = merged.get("keywords", []) if merged.get("status") == "success" else []
    if not keywords:
        return {"status": "error", "error": "暂无监测数据"}

    # 构建数据摘要 · 严格用 display_rate(与 UI 词条监测详情同源 · 排除铺量期 + 历史平滑)
    keyword_lines = []
    for kw in keywords[:10]:
        rate = kw.get('display_rate')
        rate_str = f"{float(rate):.0f}%" if rate is not None else '-'
        change = kw.get('rate_change')
        change_str = f" 变化 {change:+.0f}%" if change is not None else ''
        lifecycle = kw.get('lifecycle') or 'pending'
        lifecycle_label = {
            'monitoring': '监测中',
            'deploying': '铺量中',
            'pending': '未启动',
        }.get(lifecycle, lifecycle)
        is_monitored = '订阅自动监测' if kw.get('is_monitored') else '未订阅'
        compliance_state = '达标' if kw.get('is_compliant') else '未达标'
        keyword_lines.append(
            f"- {kw['keyword']} · 出现率 {rate_str}{change_str} · {lifecycle_label} · {compliance_state} · {is_monitored}"
        )

    period_label = '近 7 天' if period == 'weekly' else '近 30 天'
    prompt = f"""你是一位 AI 搜索优化分析师。请基于以下 GEO 监测数据,生成 3 条简洁洞察(每条不超过 30 字)。

监测周期: {period}({period_label})
监测词条数: {len(keywords)}
词条表现(与 UI 词条监测详情完全同源):
{chr(10).join(keyword_lines)}

请直接输出 JSON 格式:
{{"insights": [
  {{"text": "洞察 1", "type": "positive|warning|tip"}},
  {{"text": "洞察 2", "type": "positive|warning|tip"}},
  {{"text": "洞察 3", "type": "positive|warning|tip"}}
]}}

type 三选一(决定前端图标 / 颜色):
- positive: 表现好/达标/稳定上榜 等正面结论 → 前端显 ✅
- warning: 0% 曝光/下滑/铺量期未检出 等需立即关注的负面 → 前端显 ⚠️
- tip:     优化建议/可执行动作 → 前端显 💡

🔴 严格规范(务必逐条遵守 · 防 AI 与 UI 数据打架):
1. **禁止在 text 中写出任何具体百分比数字**(如"出现率 0%"/"75%"/"42%")· UI 已显示数字 · AI 重复一次只会引入打架。改用定性词:"已达标"/"未达标"/"稳定上榜"/"需关注"/"急需优化"
2. 引用词条名时 · **必须从上面 input 列表逐字复制** · 不许改字 · 不许造词 · 不许缩写
3. 已达标的词不能写"需排查/未检出/急需优化"· 未达标的词不能写"稳定上榜/表现良好"· 严禁逻辑反转
4. type 必须严格匹配 positive/warning/tip 三个 enum 之一
5. 洞察具体可操作 · 不抽象描述

✅ 正确示例:
{{"text": "罗平最靠谱的装修公司推荐 当前未达标 · 建议加大内容铺量", "type": "warning"}}
{{"text": "建议优先优化未达标词条的本地化内容", "type": "tip"}}

❌ 错误示例(严禁出现):
{{"text": "罗平最靠谱的装修公司推荐 出现率 0%"}}   ← 含具体数字
{{"text": "罗平靠谱的装修公司推荐 需排查"}}        ← 词条名缩字
"""

    api_key = os.getenv("DEEPSEEK_API_KEY", "")

    if not api_key:
        # 无 API Key 时按 keyword 实时数据生成 fallback (用 display_rate · UI 同源)
        # [CTO-15.23 2026-05-11] fallback text 也不含具体数字 · 跟 LLM 风格统一 · UI 数字独占
        good = [k for k in keywords if (k.get('display_rate') or 0) >= 50]
        bad = [k for k in keywords if (k.get('display_rate') or 0) == 0 and k.get('lifecycle') == 'monitoring']
        items = []
        if good:
            items.append({"text": f"{good[0]['keyword']} 已稳定达标", "type": "positive"})
        if bad:
            items.append({"text": f"{bad[0]['keyword']} 当前未检出 · 需关注", "type": "warning"})
        items.append({"text": f"已监测 {len(keywords)} 个关键词 · 建议补充本地化内容", "type": "tip"})
        return {
            "status": "success",
            "insights": items[:3],
            "source": "default",
        }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            from tools.llm_call_tracker import llm_track, usage_from_response_payload

            async with llm_track(
                "monitoring_insights",
                "deepseek",
                model=DEEPSEEK_OFFICIAL_FLASH,
                quote_id=quote_id,
                metadata={"period": period},
            ) as tracker:
                response = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": DEEPSEEK_OFFICIAL_FLASH,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 500,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
                data = response.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=response.status_code < 400,
                    error_msg=None if response.status_code < 400 else response.text[:200],
                )

            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')

            import json
            try:
                if '{' in content:
                    json_str = content[content.find('{'):content.rfind('}') + 1]
                    parsed = json.loads(json_str)
                    insights = _normalize_insight_items(parsed.get('insights'))
                else:
                    insights = _normalize_insight_items(content.split('\n'))
                return {
                    "status": "success",
                    "insights": insights,
                    "source": "deepseek",
                }
            except Exception:
                return {
                    "status": "success",
                    "insights": _normalize_insight_items(content.split('\n')),
                    "source": "deepseek",
                }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "insights": [
                {"text": "数据分析中,请稍后重试", "type": "tip"},
                {"text": f"监测词条数: {len(keywords)}", "type": "tip"},
                {"text": "建议检查 API 配置", "type": "warning"},
            ],
        }


# ==========================================
# 调度器依赖函数
# ==========================================

async def run_detection_for_keyword(
    keyword: str,
    target_brand: str,
    quote_id: int = None,
    platforms: List[str] = None,
    *,
    brand_id: int = None,
    user_id: int = None,
    caller: str = "monitoring",
    monitoring_query: str = None,
) -> dict:
    """
    执行单个关键词的检测（供调度器每日任务使用）

    [CTO-15.23 2026-05-09] 集成 llm_call_tracker 埋点
    PlatformAdapter.query 会把业务上下文传给内层真实 LLM HTTP 调用，避免外层 0 token 重复行。

    [audit P2 2026-06-10 · #8 返修 claim 更正] monitoring_query 闭环【前瞻 plumbing】:旧版硬编码
    "{keyword}哪家好?请推荐几家"忽略代理自定义查询语句 → B2B/工具类词被"哪家好"污染。现优先用
    monitoring_query(daily job 从 sub 透传),缺失才回落默认句式。
    ⚠️ 今日 prod 0 生效:monitoring_query 列当前全空(无代理设过自定义句式)→ 实际仍全走默认句式;
       本改是【前瞻管道】,待代理填了自定义查询才改变线上提问行为。别误读为"今日线上提问已变"。
    ⚠️ 只改提问文本,search_mode / 豆包模式不动(成本不变 · 老板拍 2026-06-10 豆包不切)。

    Args:
        keyword: 待检测关键词
        target_brand: 目标品牌名
        quote_id: 客户ID（可选）
        platforms: 已由调用方根据配置/订单权益解析出的有效平台列表；公开入口不得
            直接把用户请求透传到这里。历史 Kimi 仅在权威权益包含时继续兼容。
        brand_id: 关联 brand_id(给 dashboard 客户排行用)
        user_id: 关联代理 user_id(给 dashboard 用户排行用)
        caller: tracker caller 标识(默认 'monitoring' · daily fire 用)
        monitoring_query: 代理自定义查询语句(对齐手动/quote 级监测 · 缺失回落默认句式)
    """
    if platforms is None:
        platforms = [
            platform.strip()
            for platform in DEFAULT_MONITORING_PLATFORMS.split(",")
            if platform.strip()
        ]
    platforms = PlatformAdapter.eligible_monitoring_platforms(platforms)
    if not platforms:
        return {
            "status": "error",
            "error": "当前配置没有可用的监测引擎",
            "error_code": "no_eligible_monitoring_platforms",
            "results": [{
                "status": "error",
                "error_code": "no_eligible_monitoring_platforms",
                "is_detected": False,
            }],
        }

    results = []
    detected_count = 0

    # [SSOT geo-commercial-intent-governance-v1.0 §4.3] 自定义 query 优先;
    # 无确认问题时按关键词原样发送(不再运行时拼接"哪家好"另一套问题)。
    _custom_q = (monitoring_query or "").strip()
    for platform in platforms:
        # 构建测试问题
        question = _custom_q or str(keyword or "").strip()
        if not question:
            # 单格失败只影响单格(§4.3):无可发送问题时不发引擎、不合成问题
            results.append({
                "platform": platform,
                "status": "error",
                "error_code": "empty_monitoring_question",
                "error": "该关键词没有可发送的监测问题(确认问题与关键词均为空)",
                "is_detected": False,
            })
            continue

        try:
            result = await PlatformAdapter.query(
                platform=platform,
                question=question,
                target_brand=target_brand,
                caller=caller,
                brand_id=brand_id,
                quote_id=quote_id,
                user_id=user_id,
                keyword=keyword,
            )

            is_detected = result.get("is_detected", False)
            if is_detected:
                detected_count += 1

            results.append({
                "platform": platform,
                "is_detected": is_detected,
                "mention_type": result.get("mention_type", "none"),
                "response_snippet": result.get("response_snippet", "")[:200],
                "full_response": (
                    result.get("full_response")
                    or result.get("response")
                    or result.get("text")
                    or result.get("response_snippet", "")
                    or ""
                ),
                "status": result.get("status", "success"),
            })

        except Exception as e:
            results.append({
                "platform": platform,
                "is_detected": False,
                "status": "error",
                "error": str(e),
            })

    total = len(results)
    rate = round(detected_count / total * 100, 1) if total > 0 else 0

    return {
        "keyword": keyword,
        "target_brand": target_brand,
        "quote_id": quote_id,
        "detected_platforms": [r["platform"] for r in results if r.get("is_detected")],
        "total_tested": total,
        "detected_count": detected_count,
        "detection_rate": rate,
        "results": results,
    }


# ==========================================
# 通知中心API（新增）
# ==========================================

from db.notifications import (
    create_notification,
    get_notifications,
    mark_as_read,
    mark_all_as_read,
    get_unread_count,
    get_notification_by_id
)


def api_get_notifications(brand_id: int = None, limit: int = 50, unread_only: bool = False):
    """获取通知列表（brand_id=None时返回所有品牌通知）"""
    notifications = get_notifications(brand_id, limit, unread_only)
    return {
        "status": "success",
        "notifications": notifications,
        "total": len(notifications)
    }


def api_mark_notification_read(notification_id: int):
    """标记通知为已读"""
    success = mark_as_read(notification_id)
    return {
        "status": "success" if success else "error",
        "message": "已标记为已读" if success else "通知不存在"
    }


def api_mark_all_notifications_read(brand_id: int = None):
    """标记所有通知为已读（brand_id=None时标记所有品牌通知）"""
    count = mark_all_as_read(brand_id)
    return {
        "status": "success",
        "message": f"已标记{count}条通知为已读",
        "count": count
    }


def api_get_unread_count(brand_id: int = None):
    """获取未读通知数量（brand_id=None时返回所有品牌未读数量）"""
    count = get_unread_count(brand_id)
    return {
        "status": "success",
        "unread_count": count
    }


def api_get_notification_detail(notification_id: int):
    """获取通知详情"""
    notification = get_notification_by_id(notification_id)
    if notification:
        # 同时标记为已读
        mark_as_read(notification_id)
        return {
            "status": "success",
            "notification": notification
        }
    return {
        "status": "error",
        "message": "通知不存在"
    }


# ==========================================
# 报告自动生成API
# ==========================================

def api_generate_daily_report(*, brand_id: int = None, client_id: str = None, date_str: str = None) -> dict:
    """
    生成客户日报
    
    Args:
        brand_id: 品牍ID（优先）
        client_id: 客户ID（兼容）
        date_str: 报告日期 (YYYY-MM-DD)，默认为昨天
    """
    from datetime import datetime, timedelta
    from db.monitoring_db import get_tasks, save_report, get_paid_clients
    from db.notifications import create_notification
    
    # 确定报告日期
    if not date_str:
        yesterday = datetime.now().date() - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")
    
    # 获取该日期的所有任务
    tasks = get_tasks(brand_id=brand_id, client_id=client_id, limit=50)
    
    # 筛选指定日期的任务
    daily_tasks = []
    for task in tasks:
        completed_at = task.get("completed_at")
        if completed_at and _to_date_str(completed_at) == date_str:
            daily_tasks.append(task)
    
    if not daily_tasks:
        return {
            "status": "warning",
            "message": f"{date_str} 无监测任务，跳过日报生成"
        }
    
    # 聚合统计
    total_tests = 0
    total_detected = 0
    platform_stats = {}
    
    for task in daily_tasks:
        summary = task.get("result_summary", {})
        if isinstance(summary, dict):
            total_tests += summary.get("total_tests", 0)
            total_detected += summary.get("detected_count", 0)
    
    avg_detection_rate = round(total_detected / total_tests * 100, 1) if total_tests > 0 else 0
    
    # 构建报告摘要
    report_summary = {
        "date": date_str,
        "task_count": len(daily_tasks),
        "total_tests": total_tests,
        "total_detected": total_detected,
        "detection_rate": avg_detection_rate,
        "platform_stats": platform_stats,
        "generated_at": datetime.now().isoformat()
    }
    
    # 保存报告
    report_id = save_report(
        brand_id=brand_id,
        client_id=client_id,
        report_type="daily",
        period_start=date_str,
        period_end=date_str,
        summary_data=report_summary
    )
    
    # 创建通知
    try:
        # 确定通知用的 brand_id
        notify_brand_id = brand_id if brand_id else (int(client_id) if client_id else None)
        if notify_brand_id:
            create_notification(
                brand_id=notify_brand_id,
                type="report",
                title=f"日报已生成 - {date_str}",
                content=f"检出率 {avg_detection_rate}%，共执行 {len(daily_tasks)} 次监测，{total_detected}/{total_tests} 检出。",
                related_id=report_id
            )
    except Exception as e:
        print(f"[DailyReport] 创建通知失败: {e}")
    
    return {
        "status": "success",
        "report_id": report_id,
        "summary": report_summary
    }


def api_generate_reports_for_all_clients(date_str: str = None) -> dict:
    """为所有付费客户生成日报"""
    from db.monitoring_db import get_paid_clients
    
    clients = get_paid_clients(limit=100)
    results = []
    
    for client in clients:
        brand_id = client.get("brand_id") or client.get("quote_id")
        client_id = str(client.get("quote_id", ""))
        if brand_id or client_id:
            result = api_generate_daily_report(brand_id=brand_id, client_id=client_id, date_str=date_str)
            results.append({
                "brand_id": brand_id,
                "client_id": client_id,
                "company": client.get("company_name", ""),
                "result": result
            })
    
    success_count = sum(1 for r in results if r["result"]["status"] == "success")
    
    return {
        "status": "success",
        "total_clients": len(results),
        "success_count": success_count,
        "details": results
    }


def api_generate_weekly_report(*, brand_id: int = None, client_id: str = None, week_end_date: str = None) -> dict:
    """
    生成客户周报
    
    Args:
        brand_id: 品牍ID（优先）
        client_id: 客户ID（兼容）
        week_end_date: 周报结束日期 (YYYY-MM-DD)，默认为昨天
    """
    from datetime import datetime, timedelta
    from db.monitoring_db import get_tasks, save_report, get_reports
    from db.notifications import create_notification
    
    # 确定周期
    if not week_end_date:
        end_date = datetime.now().date()  # 包含今天
    else:
        end_date = datetime.strptime(week_end_date, "%Y-%m-%d").date()

    start_date = end_date - timedelta(days=6)  # 7天周期
    
    # 获取任务
    tasks = get_tasks(brand_id=brand_id, client_id=client_id, limit=100)
    
    # 筛选本周任务
    weekly_tasks = []
    for task in tasks:
        completed_at = task.get("completed_at")
        if completed_at:
            try:
                if hasattr(completed_at, 'date'):
                    task_date = completed_at.date()
                else:
                    task_date = datetime.fromisoformat(str(completed_at).replace("T", " ").split(".")[0]).date()
                if start_date <= task_date <= end_date:
                    weekly_tasks.append(task)
            except:
                pass

    if not weekly_tasks:
        return {
            "status": "warning",
            "message": f"{start_date} ~ {end_date} 无监测任务"
        }
    
    # 聚合统计
    total_tests = 0
    total_detected = 0
    daily_rates = {}
    
    for task in weekly_tasks:
        summary = task.get("result_summary", {})
        if isinstance(summary, dict):
            total_tests += summary.get("total_tests", 0)
            total_detected += summary.get("detected_count", 0)
            # 按日期分组
            completed_at = task.get("completed_at")
            if completed_at:
                day = completed_at.strftime("%Y-%m-%d") if hasattr(completed_at, 'strftime') else str(completed_at).split("T")[0].split(" ")[0]
                if day not in daily_rates:
                    daily_rates[day] = []
                daily_rates[day].append(summary.get("detection_rate", 0))
    
    avg_rate = round(total_detected / total_tests * 100, 1) if total_tests > 0 else 0
    
    # 计算与上周对比
    last_week_end = start_date - timedelta(days=1)
    last_week_start = last_week_end - timedelta(days=6)
    last_week_reports = get_reports(brand_id=brand_id, client_id=client_id, report_type="weekly", limit=1)
    
    rate_change = 0
    if last_week_reports:
        last_summary = last_week_reports[0].get("summary_data", {})
        if isinstance(last_summary, str):
            import json
            last_summary = json.loads(last_summary)
        last_rate = last_summary.get("detection_rate", 0)
        rate_change = round(avg_rate - last_rate, 1)
    
    # 聚合关键词表现 + 套餐目标
    kw_data = _aggregate_keyword_stats(weekly_tasks)
    tier_data = _get_tier_target(brand_id=brand_id, client_id=client_id)

    # 报告摘要
    report_summary = {
        "period_start": start_date.strftime("%Y-%m-%d"),
        "period_end": end_date.strftime("%Y-%m-%d"),
        "task_count": len(weekly_tasks),
        "total_tests": total_tests,
        "total_detected": total_detected,
        "detection_rate": avg_rate,
        "avg_detection_rate": avg_rate,
        "target_rate": tier_data["target_rate"],
        "tier_name": tier_data["tier_name"],
        "rate_change": rate_change,
        "daily_rates": daily_rates,
        "generated_at": datetime.now().isoformat(),
        **kw_data,
    }
    
    # 保存报告
    report_id = save_report(
        brand_id=brand_id,
        client_id=client_id,
        report_type="weekly",
        period_start=start_date.strftime("%Y-%m-%d"),
        period_end=end_date.strftime("%Y-%m-%d"),
        summary_data=report_summary
    )
    
    # 创建通知
    change_text = f"↑{rate_change}%" if rate_change > 0 else f"↓{abs(rate_change)}%" if rate_change < 0 else "持平"
    try:
        notify_brand_id = brand_id if brand_id else (int(client_id) if client_id else None)
        if notify_brand_id:
            create_notification(
                brand_id=notify_brand_id,
                type="report",
                title=f"周报已生成 - {start_date.strftime('%m/%d')}~{end_date.strftime('%m/%d')}",
                content=f"检出率 {avg_rate}%（{change_text}），共执行 {len(weekly_tasks)} 次监测。",
                related_id=report_id
            )
    except Exception as e:
        print(f"[WeeklyReport] 创建通知失败: {e}")
    
    return {
        "status": "success",
        "report_id": report_id,
        "summary": report_summary
    }


def api_generate_monthly_report(*, brand_id: int = None, client_id: str = None, month: str = None) -> dict:
    """
    生成客户月报（默认draft状态，需人工审核）
    
    Args:
        brand_id: 品牍ID（优先）
        client_id: 客户ID（兼容）
        month: 月份 (YYYY-MM)，默认为上个月
    """
    from datetime import datetime, timedelta
    from calendar import monthrange
    from db.monitoring_db import get_tasks, save_report, get_reports
    
    # 确定周期
    if not month:
        last_month = datetime.now().replace(day=1) - timedelta(days=1)
        month = last_month.strftime("%Y-%m")
    
    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    start_date = f"{year}-{mon:02d}-01"
    _, last_day = monthrange(year, mon)
    end_date = f"{year}-{mon:02d}-{last_day}"
    
    # 获取任务
    tasks = get_tasks(brand_id=brand_id, client_id=client_id, limit=200)
    
    # 筛选本月任务
    monthly_tasks = []
    for task in tasks:
        completed_at = task.get("completed_at")
        if completed_at and _to_date_str(completed_at)[:7] == month:
            monthly_tasks.append(task)
    
    if not monthly_tasks:
        return {"status": "warning", "message": f"{month} 无监测任务"}
    
    # 聚合统计
    total_tests = 0
    total_detected = 0
    weekly_rates = {}
    
    for task in monthly_tasks:
        summary = task.get("result_summary", {})
        if isinstance(summary, dict):
            total_tests += summary.get("total_tests", 0)
            total_detected += summary.get("detected_count", 0)
    
    avg_rate = round(total_detected / total_tests * 100, 1) if total_tests > 0 else 0
    
    # 月环比
    last_month_reports = get_reports(brand_id=brand_id, client_id=client_id, report_type="monthly", limit=1)
    rate_change = 0
    if last_month_reports:
        last_summary = last_month_reports[0].get("summary_data", {})
        if isinstance(last_summary, str):
            import json
            last_summary = json.loads(last_summary)
        rate_change = round(avg_rate - last_summary.get("detection_rate", 0), 1)
    
    kw_data = _aggregate_keyword_stats(monthly_tasks)
    tier_data = _get_tier_target(brand_id=brand_id, client_id=client_id)

    report_summary = {
        "month": month,
        "period_start": start_date,
        "period_end": end_date,
        "task_count": len(monthly_tasks),
        "total_tests": total_tests,
        "total_detected": total_detected,
        "detection_rate": avg_rate,
        "avg_detection_rate": avg_rate,
        "target_rate": tier_data["target_rate"],
        "tier_name": tier_data["tier_name"],
        "rate_change": rate_change,
        "generated_at": datetime.now().isoformat(),
        **kw_data,
    }
    
    # 生成月报内容（可编辑）
    _cluster_section = ""
    if kw_data.get("cluster_stats"):
        _cluster_lines = []
        for cs in kw_data["cluster_stats"]:
            _cluster_lines.append(f"- **{cs['cluster_name']}**: {cs['keyword_count']}个核心词，检出率 {cs['avg_rate']}%")
        _cluster_section = "\n## 主题包表现\n" + "\n".join(_cluster_lines) + "\n"

    content = f"""# {month} 月度监测报告

## 概述
- 监测次数: {len(monthly_tasks)} 次
- 总测试数: {total_tests}
- 品牌检出: {total_detected} 次
- 检出率: {avg_rate}%
- 环比变化: {"+" if rate_change > 0 else ""}{rate_change}%
{_cluster_section}
## 数据分析
（待运营人员补充分析内容）

## 优化建议
（待运营人员补充优化建议）
"""
    
    # 保存报告（draft状态）
    report_id = save_report(
        brand_id=brand_id,
        client_id=client_id,
        report_type="monthly",
        period_start=start_date,
        period_end=end_date,
        summary_data=report_summary,
        content=content,
        status="draft"
    )
    
    return {
        "status": "success",
        "report_id": report_id,
        "summary": report_summary,
        "message": "月报已生成（草稿），请审核后发送"
    }


def api_generate_quarterly_report(*, brand_id: int = None, client_id: str = None, quarter: str = None) -> dict:
    """
    生成客户季报（默认draft状态，需人工审核）
    
    Args:
        brand_id: 品牍ID（优先）
        client_id: 客户ID（兼容）
        quarter: 季度 (YYYY-Q1/Q2/Q3/Q4)，默认为上个季度
    """
    from datetime import datetime
    from db.monitoring_db import get_tasks, save_report
    
    # 确定周期
    if not quarter:
        today = datetime.now()
        current_q = (today.month - 1) // 3 + 1
        if current_q == 1:
            year = today.year - 1
            q = 4
        else:
            year = today.year
            q = current_q - 1
        quarter = f"{year}-Q{q}"
    else:
        year = int(quarter.split("-Q")[0])
        q = int(quarter.split("-Q")[1])
    
    quarter_months = {1: ("01", "03"), 2: ("04", "06"), 3: ("07", "09"), 4: ("10", "12")}
    start_month, end_month = quarter_months[q]
    start_date = f"{year}-{start_month}-01"
    end_date = f"{year}-{end_month}-{'31' if end_month in ['01','03','05','07','08','10','12'] else '30'}"
    
    # 获取任务
    tasks = get_tasks(brand_id=brand_id, client_id=client_id, limit=500)
    
    # 筛选本季度任务
    quarterly_tasks = []
    for task in tasks:
        completed_at = task.get("completed_at")
        if completed_at:
            task_month = _to_date_str(completed_at)[:7]  # YYYY-MM
            if f"{year}-{start_month}" <= task_month <= f"{year}-{end_month}":
                quarterly_tasks.append(task)
    
    if not quarterly_tasks:
        return {"status": "warning", "message": f"{quarter} 无监测任务"}
    
    # 聚合统计
    total_tests = sum(t.get("result_summary", {}).get("total_tests", 0) for t in quarterly_tasks if isinstance(t.get("result_summary"), dict))
    total_detected = sum(t.get("result_summary", {}).get("detected_count", 0) for t in quarterly_tasks if isinstance(t.get("result_summary"), dict))
    avg_rate = round(total_detected / total_tests * 100, 1) if total_tests > 0 else 0
    
    kw_data = _aggregate_keyword_stats(quarterly_tasks)
    tier_data = _get_tier_target(brand_id=brand_id, client_id=client_id)

    report_summary = {
        "quarter": quarter,
        "period_start": start_date,
        "period_end": end_date,
        "task_count": len(quarterly_tasks),
        "total_tests": total_tests,
        "total_detected": total_detected,
        "detection_rate": avg_rate,
        "target_rate": tier_data["target_rate"],
        "tier_name": tier_data["tier_name"],
        "generated_at": datetime.now().isoformat(),
        **kw_data,
    }
    
    content = f"""# {quarter} 季度监测报告

## 概述
- 监测次数: {len(quarterly_tasks)} 次
- 总测试数: {total_tests}
- 品牌检出: {total_detected} 次
- 平均检出率: {avg_rate}%

## 季度趋势分析
（待运营人员补充）

## 优化建议
（待运营人员补充）
"""
    
    report_id = save_report(
        brand_id=brand_id,
        client_id=client_id,
        report_type="quarterly",
        period_start=start_date,
        period_end=end_date,
        summary_data=report_summary,
        content=content,
        status="draft"
    )
    
    return {
        "status": "success",
        "report_id": report_id,
        "summary": report_summary,
        "message": "季报已生成（草稿），请审核后发送"
    }


def api_generate_yearly_report(*, brand_id: int = None, client_id: str = None, year: int = None) -> dict:
    """
    生成客户年报（默认draft状态，需人工审核）
    """
    from datetime import datetime
    from db.monitoring_db import get_tasks, save_report
    
    if not year:
        year = datetime.now().year - 1
    
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    
    # 获取任务
    tasks = get_tasks(brand_id=brand_id, client_id=client_id, limit=1000)
    
    # 筛选本年任务
    yearly_tasks = [t for t in tasks if t.get("completed_at") and _to_date_str(t["completed_at"]).startswith(str(year))]
    
    if not yearly_tasks:
        return {"status": "warning", "message": f"{year}年 无监测任务"}
    
    # 聚合统计
    total_tests = sum(t.get("result_summary", {}).get("total_tests", 0) for t in yearly_tasks if isinstance(t.get("result_summary"), dict))
    total_detected = sum(t.get("result_summary", {}).get("detected_count", 0) for t in yearly_tasks if isinstance(t.get("result_summary"), dict))
    avg_rate = round(total_detected / total_tests * 100, 1) if total_tests > 0 else 0
    
    kw_data = _aggregate_keyword_stats(yearly_tasks)
    tier_data = _get_tier_target(brand_id=brand_id, client_id=client_id)

    report_summary = {
        "year": year,
        "period_start": start_date,
        "period_end": end_date,
        "task_count": len(yearly_tasks),
        "total_tests": total_tests,
        "target_rate": tier_data["target_rate"],
        "tier_name": tier_data["tier_name"],
        "total_detected": total_detected,
        "detection_rate": avg_rate,
        "generated_at": datetime.now().isoformat(),
        **kw_data,
    }
    
    content = f"""# {year}年度 AI搜索可见度监测报告

## 年度概览
- 全年监测次数: {len(yearly_tasks)} 次
- 总测试数: {total_tests}
- 品牌检出: {total_detected} 次
- 年度平均检出率: {avg_rate}%

## 年度趋势分析
（待运营人员补充月度趋势图表和分析）

## 核心发现
（待运营人员补充）

## 下一年优化建议
（待运营人员补充）
"""
    
    report_id = save_report(
        brand_id=brand_id,
        client_id=client_id,
        report_type="yearly",
        period_start=start_date,
        period_end=end_date,
        summary_data=report_summary,
        content=content,
        status="draft"
    )
    
    return {
        "status": "success",
        "report_id": report_id,
        "summary": report_summary,
        "message": "年报已生成（草稿），请审核后发送"
    }


# ==========================================
# 异常告警功能
# ==========================================

def api_check_and_alert(*, brand_id: int = None, client_id: str = None, detection_rate: float, alert_threshold: float = 30.0) -> dict:
    """
    检查监测结果并在异常时创建告警
    
    Args:
        brand_id: 品牍ID（优先）
        client_id: 客户ID（兼容）
        detection_rate: 当前检出率
        alert_threshold: 告警阈值（低于此值触发告警），默认30%
    """
    from db.notifications import create_notification
    
    if detection_rate >= alert_threshold:
        return {"status": "ok", "message": "检出率正常"}
    
    # 触发告警
    try:
        notify_brand_id = brand_id if brand_id else (int(client_id) if client_id else None)
        if not notify_brand_id:
            return {"status": "error", "error": "缺少 brand_id 或 client_id"}
        notification_id = create_notification(
            brand_id=notify_brand_id,
            type="alert",
            title=f"⚠️ 检出率异常告警",
            content=f"当前检出率 {detection_rate}% 低于阈值 {alert_threshold}%，建议优化内容策略。",
            related_id=None
        )
        return {
            "status": "alert",
            "message": f"已创建告警通知 (ID: {notification_id})",
            "detection_rate": detection_rate,
            "threshold": alert_threshold
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def api_batch_check_alerts() -> dict:
    """批量检查所有客户的异常情况"""
    from db.monitoring_db import get_paid_clients, get_tasks
    from db.notifications import create_notification
    
    clients = get_paid_clients(limit=100)
    alerts_created = 0
    
    for client in clients:
        brand_id = client.get("brand_id") or client.get("quote_id")
        client_id = str(client.get("quote_id", ""))
        if not (brand_id or client_id):
            continue
        
        # 获取最近任务
        tasks = get_tasks(brand_id=brand_id, client_id=client_id, limit=1)
        if not tasks:
            continue
        
        latest_task = tasks[0]
        summary = latest_task.get("result_summary", {})
        if isinstance(summary, dict):
            detection_rate = summary.get("detection_rate", 100)
            
            # 低于30%触发告警
            if detection_rate < 30:
                try:
                    notify_brand_id = brand_id if brand_id else int(client_id)
                    create_notification(
                        brand_id=notify_brand_id,
                        type="alert",
                        title=f"⚠️ 检出率异常",
                        content=f"最近一次监测检出率仅 {detection_rate}%，低于30%阈值。",
                        related_id=latest_task.get("id")
                    )
                    alerts_created += 1
                except:
                    pass
    
    return {
        "status": "success",
        "alerts_created": alerts_created,
        "clients_checked": len(clients)
    }


# ==========================================
# 平台权重管理 API
# ==========================================

class PlatformWeightsUpdateRequest(BaseModel):
    weights: dict  # {"doubao": 0.35, "dashscope": 0.30, ...}
    mau_data: Optional[dict] = None  # {"doubao": "2.27亿", ...}


def api_get_platform_weights() -> dict:
    """获取当前平台权重（优先DB, fallback内存默认值）"""
    saved = get_saved_platform_weights()
    if saved:
        return {
            "status": "success",
            "weights": normalize_active_platform_weights(saved.get("weights", {})),
            "mau_data": saved.get("mau_data", {}),
            "source": saved.get("source", "default"),
            "updated_at": saved.get("updated_at", "")
        }
    return {
        "status": "success",
        "weights": dict(PLATFORM_WEIGHTS),
        "mau_data": {},
        "source": "default",
        "updated_at": ""
    }


def api_update_platform_weights(request: PlatformWeightsUpdateRequest) -> dict:
    """更新平台权重（工作人员确认后保存）"""
    weights = request.weights
    mau_data = request.mau_data or {}

    normalized_keys = [normalize_monitoring_platform(key) for key in weights]
    if len(set(normalized_keys)) != len(normalized_keys) or set(normalized_keys) != set(PLATFORM_CANONICAL_ORDER):
        expected = "、".join(
            {"doubao": "豆包", "dashscope": "通义千问", "deepseek": "DeepSeek",
             "kimi": "Kimi", "yuanbao": "元宝"}.get(p, p)
            for p in PLATFORM_CANONICAL_ORDER
        )
        # 管理员必须能改权重(手册 §1.6 事故 #2:别把治理页做成死胡同)——
        # 报错要说清缺哪些、当前口径是什么，而不是留一句写死的旧四路文案。
        return {"status": "error", "error": f"权重必须完整包含 {expected}"}
    weights = {normalize_monitoring_platform(key): value for key, value in weights.items()}

    # 校验：权重之和应接近 1.0
    total = sum(weights.values())
    if abs(total - 1.0) > 0.05:
        return {"status": "error", "error": f"权重总和为 {total:.2f}，应接近 1.0"}

    # 校验：所有值 > 0
    for k, v in weights.items():
        if v < 0:
            return {"status": "error", "error": f"平台 {k} 权重不能为负数"}

    ok = save_platform_weights(weights, mau_data, source="manual")
    if ok:
        return {
            "status": "success",
            "message": "平台权重已更新",
            "weights": weights,
            "mau_data": mau_data
        }
    return {"status": "error", "error": "保存失败"}


async def api_search_platform_mau() -> dict:
    """搜索最新AI平台MAU数据并建议权重（两轮对话：搜索 → 结构化提取）"""
    import httpx
    import os
    import json as _json
    import re
    import traceback

    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        return {"status": "error", "error": "缺少 DASHSCOPE_API_KEY"}

    # ---------- 第1轮：联网搜索获取最新MAU数据 ----------
    search_prompt = (
        "请搜索2026年最新的中国AI大模型应用月活跃用户数(MAU)排行数据。"
        "必须包含以下4个应用：豆包、通义千问、DeepSeek、腾讯元宝。"
        "请给出每个应用的具体MAU数字。"
    )

    try:
        from tools.llm_call_tracker import llm_track, usage_from_response_payload

        async with httpx.AsyncClient(timeout=90) as client:
            async with llm_track(
                "monitoring_platform_mau",
                "dashscope",
                model="qwen3-max",
                metadata={"step": "search", "enable_search": True},
            ) as tracker:
                resp = await client.post(
                    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "qwen3-max",
                        "input": {"messages": [{"role": "user", "content": search_prompt}]},
                        "parameters": {
                            "result_format": "message",
                            "enable_search": True,
                            "max_tokens": 1500
                        }
                    }
                )
                data = resp.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=resp.status_code < 400,
                    error_msg=None if resp.status_code < 400 else resp.text[:200],
                )

        # 提取搜索结果文本
        search_text = ""
        try:
            search_text = data["output"]["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            try:
                search_text = data["output"]["text"]
            except (KeyError,):
                return {"status": "error", "error": "AI搜索返回格式异常"}

        if not search_text:
            return {"status": "error", "error": "AI搜索未返回内容"}

        print(f"[MAU Search] 第1轮搜索结果: {search_text[:300]}")

        # ---------- 第2轮：从搜索结果中提取结构化数据 ----------
        extract_prompt = (
            f"以下是关于AI大模型应用MAU的搜索结果：\n\n{search_text}\n\n"
            "请从上文中提取以下4个平台的MAU数据，严格按JSON格式输出，不要输出其他文字：\n"
            '```json\n'
            '{\n'
            '  "doubao": {"name":"豆包", "mau":"X亿", "mau_wan": 数字},\n'
            '  "dashscope": {"name":"通义千问", "mau":"X亿", "mau_wan": 数字},\n'
            '  "deepseek": {"name":"DeepSeek", "mau":"X亿", "mau_wan": 数字},\n'
            '  "yuanbao": {"name":"腾讯元宝", "mau":"X万", "mau_wan": 数字}\n'
            '}\n'
            '```\n'
            "mau_wan 是万为单位的数字（例如2.27亿=22700, 993万=993）。"
            "如果搜索结果中某个平台没有明确数据，请根据你的知识给出合理估计并标注。"
        )

        async with httpx.AsyncClient(timeout=60) as client:
            async with llm_track(
                "monitoring_platform_mau",
                "dashscope",
                model="qwen3-max",
                metadata={"step": "extract"},
            ) as tracker:
                resp2 = await client.post(
                    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "qwen3-max",
                        "input": {"messages": [
                            {"role": "user", "content": extract_prompt}
                        ]},
                        "parameters": {
                            "result_format": "message",
                            "max_tokens": 800
                        }
                    }
                )
                data2 = resp2.json()
                input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data2)
                tracker.record(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    success=resp2.status_code < 400,
                    error_msg=None if resp2.status_code < 400 else resp2.text[:200],
                )

        extract_text = ""
        try:
            extract_text = data2["output"]["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            try:
                extract_text = data2["output"]["text"]
            except (KeyError,):
                pass

        print(f"[MAU Search] 第2轮提取结果: {extract_text[:300]}")

        # ---------- 解析JSON ----------
        mau_result = None

        # 尝试从 ```json ... ``` 代码块提取
        code_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', extract_text)
        if code_match:
            try:
                mau_result = _json.loads(code_match.group(1))
            except Exception:
                pass

        # fallback: 直接找最外层 JSON 对象
        if not mau_result:
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', extract_text)
            if json_match:
                try:
                    mau_result = _json.loads(json_match.group())
                except Exception:
                    pass

        # fallback: 从原始搜索文本中用正则提取MAU数字
        if not mau_result:
            mau_result = _extract_mau_from_text(search_text)

        if not mau_result:
            return {
                "status": "error",
                "error": "未能提取结构化MAU数据，请手动输入",
                "raw_text": search_text[:500]
            }

        # ---------- 计算建议权重 ----------
        total_mau = 0
        platform_mau = {}
        mau_display = {}

        # 默认MAU保底值（万），用于搜索不到数据时的 fallback
        # [P0-2 · 2026-07-26] 统一五引擎:kimi 也必须有保底值，否则搜不到 MAU 时
        #   它会拿泛型 1000 兜底而不是行业保底，权重被系统性压低。
        fallback_mau = {
            "doubao": 22700, "dashscope": 20300, "deepseek": 13500,
            "kimi": 3600, "yuanbao": 1000,
        }

        for platform in PLATFORM_CANONICAL_ORDER:
            info = mau_result.get(platform, {})
            if isinstance(info, dict):
                mau_num = info.get("mau_wan", 0) or info.get("mau_number", 0)
                name = info.get("name", platform)
                mau_str = info.get("mau", f"{mau_num}万")
            else:
                mau_num = 0
                name = platform
                mau_str = "未知"

            # 负值或0时使用保底值
            if mau_num <= 0:
                mau_num = fallback_mau.get(platform, 1000)
                mau_str = f"~{mau_num}万(估)"

            platform_mau[platform] = mau_num
            mau_display[platform] = f"{name}: {mau_str}"
            total_mau += mau_num

        suggested_weights = {}
        for platform, mau_num in platform_mau.items():
            raw_weight = mau_num / total_mau if total_mau > 0 else 0.25
            suggested_weights[platform] = round(raw_weight, 2)

        # 四舍五入到5%的倍数
        adjusted = {}
        for p, w in suggested_weights.items():
            adjusted[p] = round(round(w * 20) / 20, 2)
        diff = round(1.0 - sum(adjusted.values()), 2)
        if diff != 0:
            max_p = max(adjusted, key=adjusted.get)
            adjusted[max_p] = round(adjusted[max_p] + diff, 2)

        return {
            "status": "success",
            "mau_result": mau_result,
            "mau_display": mau_display,
            "suggested_weights": adjusted,
            "current_weights": dict(PLATFORM_WEIGHTS),
            "raw_text": search_text[:500]
        }

    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "error": f"搜索失败: {type(e).__name__}: {str(e)}"}


def _extract_mau_from_text(text: str) -> dict:
    """从自然语言文本中用正则提取MAU数据（fallback）"""
    import re

    platform_patterns = {
        "doubao": [r"豆包[^。]*?(\d+\.?\d*)\s*亿", r"豆包[^。]*?(\d+)\s*万"],
        "dashscope": [r"(?:千问|通义)[^。]*?(\d+\.?\d*)\s*亿", r"(?:千问|通义)[^。]*?(\d+)\s*万"],
        "deepseek": [r"[Dd]eep[Ss]eek[^。]*?(\d+\.?\d*)\s*亿", r"[Dd]eep[Ss]eek[^。]*?(\d+)\s*万"],
        "yuanbao": [r"(?:腾讯)?元宝[^。]*?(\d+\.?\d*)\s*亿", r"(?:腾讯)?元宝[^。]*?(\d+)\s*万"],
    }

    name_map = {"doubao": "豆包", "dashscope": "通义千问", "deepseek": "DeepSeek", "yuanbao": "腾讯元宝"}
    result = {}

    for platform, patterns in platform_patterns.items():
        mau_wan = 0
        mau_str = "未知"
        # 先尝试匹配 "亿"
        m = re.search(patterns[0], text)
        if m:
            val = float(m.group(1))
            mau_wan = int(val * 10000)
            mau_str = f"{val}亿"
        else:
            # 再尝试匹配 "万"
            m = re.search(patterns[1], text)
            if m:
                val = int(m.group(1))
                mau_wan = val
                mau_str = f"{val}万"

        if mau_wan > 0:
            result[platform] = {
                "name": name_map[platform],
                "mau": mau_str,
                "mau_wan": mau_wan
            }

    return result if len(result) >= 2 else None


# ==========================================
# 初始化
# ==========================================

def init_monitoring_api():
    """初始化监测API（确保表存在）"""
    init_monitoring_tables()
    load_platform_weights_from_db()
    print("[Monitoring API] 监测API初始化完成")
