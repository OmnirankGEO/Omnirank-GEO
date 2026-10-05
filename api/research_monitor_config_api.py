"""
GEO 调研监测 - 系统配置 Admin API (里程碑 A.7 Group 4)

功能:
  对 geo_research_config 表(熔断阈值 / 预算 / TTL 等系统参数)提供 4 个管理接口:
    GET    /config           列出所有配置项
    GET    /config/{key}     获取单项配置
    PUT    /config/{key}     更新单项(含类型校验白名单)
    POST   /config/reset     重置默认(危险操作,需 confirm_code)

权限: 全部需要 admin (request.state.user.is_admin)
SQL: 全部 %s 参数化, JSONB 写入用 %s::jsonb
审计: 用 logger.warning 记录关键变更(不引入 audit_log 表)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("GEO-ResearchMonitor.ConfigAPI")

router = APIRouter(prefix="/api/admin/research-monitor", tags=["调研监测-配置"])


# ============================================================
# 默认配置(用于 reset)
# ============================================================

# P13-v13 (2026-05-27 HIGH review fix): DEFAULT_CONFIGS 同步 Phase 9 归一化
#   旧: clean_attempts_max=3 / undo_window_minutes=30 / article_min_chars=3000 (审核流契约)
#   新: 删 2 deprecated · article_min_chars=100 · 加 4 model_*
#   原因: POST /config/reset 用 DEFAULT_CONFIGS 当真源 (line 387/427)
#         管理员点 reset 会把 Phase 9 归一化(d549a0fe)打回旧版 + 漏掉 model_*
#   修: DEFAULT_CONFIGS 和 db/diagnosis_db.py seed 完全对齐
DEFAULT_CONFIGS: Dict[str, Any] = {
    'circuit_breaker_consecutive': 50,
    'circuit_breaker_rate': 0.5,
    'circuit_breaker_min_processed': 100,
    'budget_per_round_yuan': 350,
    'budget_per_month_yuan': 1000,
    'article_oss_ttl_days': 180,
    'lock_stale_minutes': 5,
    'article_min_chars_for_review': 100,                              # Phase 9: 3000 → 100 (老板拍板入库门槛)
    # Phase 9 模型名 (跟 db/diagnosis_db.py:1505+ seed 一致)
    'model_doubao_app': 'doubao-seed-2-0-lite-260215',
    'model_deepseek_via_dashscope': 'deepseek-v4-flash',
    'model_qwen_default': 'qwen-plus-latest',
    'model_kimi_via_dashscope': 'kimi/kimi-k2.6',
}

# 默认描述(reset 时一并写回) · 跟 db/diagnosis_db.py seed 描述对齐
DEFAULT_DESCRIPTIONS: Dict[str, str] = {
    'circuit_breaker_consecutive': '连续失败 N 次熔断',
    'circuit_breaker_rate': '整体失败率阈值(超过即熔断)',
    'circuit_breaker_min_processed': '至少跑了 N 次才开始判失败率',
    'budget_per_round_yuan': '单轮预算上限(元)',
    'budget_per_month_yuan': '月度预算上限(元)',
    'article_oss_ttl_days': 'OSS 文章保留天数',
    'lock_stale_minutes': '行级锁 stale 自动释放时间(分钟 · 管理员编辑文章库用)',
    'article_min_chars_for_review': '入文章库门槛(清洗后正文字数)',
    'model_doubao_app': '豆包 doubao_app + ai_search 底座模型(env DOUBAO_APP_MODEL 可覆盖)',
    'model_deepseek_via_dashscope': 'DeepSeek 走阿里百炼 dashscope generation 的模型名',
    'model_qwen_default': 'Qwen 默认模型(SystemSettings.research_monitor_tasks.platforms.qwen.model 优先)',
    'model_kimi_via_dashscope': 'Kimi 走阿里百炼 dashscope chat/completions 兼容模式的模型名',
    # P14-v10 半月自动跑批 cron 配置
    'cron_enabled': '自动跑批总开关(true/false · 关掉后只能手动触发)',
    'cron_days': '每月哪几天自动跑(逗号分隔 · 1-28 · 默认 1+16 号)',
    'cron_hour': '自动跑批小时(0-23 · 北京时间 · 默认 02:00 凌晨)',
}


# ============================================================
# 类型校验白名单
# 每个 key 的期望 type/min/max(由 PUT /config/{key} 强制)
# ============================================================

#
# Phase 9 (2026-05-26):
#   - 删 clean_attempts_max / undo_window_minutes (审核 + LLM 清洗已删 · 跟 seed 物理删除对齐)
#   - 加 4 个 model_* (str 类型 · max_length=100 防御 · 跟 geo_research_config seed 对齐)
#   - article_min_chars_for_review min 100 → 50 + desc 更新 (新语义"入文章库门槛")
CONFIG_VALUE_SCHEMA: Dict[str, Dict[str, Any]] = {
    'circuit_breaker_consecutive': {'type': int, 'min': 1, 'max': 10000, 'desc': '连续失败 N 次熔断'},
    'circuit_breaker_rate': {'type': float, 'min': 0.0, 'max': 1.0, 'desc': '失败率阈值(0~1)'},
    'circuit_breaker_min_processed': {'type': int, 'min': 1, 'max': 100000, 'desc': '至少跑 N 次才判失败率'},
    'budget_per_round_yuan': {'type': (int, float), 'min': 1, 'max': 10000, 'desc': '单轮预算上限(元)'},
    'budget_per_month_yuan': {'type': (int, float), 'min': 1, 'max': 100000, 'desc': '月度预算上限(元)'},
    'article_oss_ttl_days': {'type': int, 'min': 1, 'max': 3650, 'desc': 'OSS 保留天数'},
    'lock_stale_minutes': {'type': int, 'min': 1, 'max': 60, 'desc': '行锁 stale 自释放(1~60分钟)'},
    'article_min_chars_for_review': {'type': int, 'min': 50, 'max': 100000, 'desc': '入文章库门槛(清洗后正文字数)'},
    # Phase 9 模型名 (str · 长度 1~100 防御 · 平台代码 _load_model_from_config 用)
    'model_doubao_app': {'type': str, 'min': 1, 'max': 100, 'desc': '豆包 doubao_app 模型名(env DOUBAO_APP_MODEL 优先)'},
    'model_deepseek_via_dashscope': {'type': str, 'min': 1, 'max': 100, 'desc': 'DeepSeek 走百炼的模型名'},
    'model_qwen_default': {'type': str, 'min': 1, 'max': 100, 'desc': 'Qwen 默认模型(SystemSettings 优先)'},
    'model_kimi_via_dashscope': {'type': str, 'min': 1, 'max': 100, 'desc': 'Kimi 走百炼的模型名(完整名含 kimi/ 前缀)'},
    # P14-v10: cron schedule (boolean/string/int) · PUT 后会触发 scheduler reschedule
    'cron_enabled': {'type': bool, 'desc': '自动跑批总开关'},
    # days 格式 "1,16" / "1,10,20" 等 · 1~28 (跳 29-31 避免月末缺日)
    'cron_days': {'type': str, 'min': 1, 'max': 100, 'pattern_days': True,
                  'desc': '每月跑批日(逗号分隔 1-28)'},
    'cron_hour': {'type': int, 'min': 0, 'max': 23, 'desc': '跑批小时(0-23 北京时间)'},
    # [2026-07-16 提并发+自动续跑批] Stage1 并发度 + 4h 超时自动续跑(消费侧 round_runner)
    # max=64 与 round_runner.MAX_FETCH_CONCURRENCY_PER_PLATFORM 对齐(全局有效并发=4×N)
    'fetch_concurrency_per_platform': {'type': int, 'min': 1, 'max': 64,
                                       'desc': 'Stage1 每平台并发数(4 平台并行 × N · 默认 8)'},
    'round_auto_resume_enabled': {'type': bool,
                                  'desc': '4h 硬超时自动续跑开关(仅超时路径 · 预算熔断不自动续)'},
    'round_auto_resume_max': {'type': int, 'min': 0, 'max': 20,
                              'desc': '单轮自动续跑次数上限(达上限保持 failed_resumable 等人工)'},
    # Stage3 URL quality budget. Deploy changes these through this typed API;
    # direct JSONB writes would bypass range validation and audit metadata.
    'stage3_url_budget_max': {'type': int, 'min': 0, 'max': 10000,
                              'desc': 'Stage3 全新 URL 抓取上限；0 表示旧全量行为'},
    'stage3_min_urls_per_industry': {'type': int, 'min': 0, 'max': 1000,
                                     'desc': 'Stage3 每行业覆盖底线'},
    'stage3_min_urls_per_platform': {'type': int, 'min': 0, 'max': 1000,
                                     'desc': 'Stage3 每平台覆盖底线'},
    'stage3_max_urls_per_domain': {'type': int, 'min': 1, 'max': 1000,
                                   'desc': 'Stage3 单域名 URL 上限'},
}

# 重置确认码(硬编码,防止 GUI 上误点)
RESET_CONFIRM_CODE = "RESET_RESEARCH_MONITOR_CONFIG"


# ============================================================
# 工具函数
# ============================================================

def _require_admin(request: Request) -> dict:
    """要求管理员身份,返回 user dict."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _admin_id(user: dict) -> str:
    """从 user dict 提一个稳定的 admin 标识(给 updated_by/审计日志用,长度 ≤ 50)."""
    raw = user.get("id") or user.get("user_id") or user.get("username") or "unknown"
    return str(raw)[:50]


def _serialize_row(row: dict) -> dict:
    """把一行 geo_research_config 转 JSON-friendly dict."""
    if row is None:
        return None
    # psycopg2 JSONB 字段已自动反序列化为 Python 对象(int/float/str/dict/list)
    return {
        "key": row["key"],
        "value": row["value_json"],
        "description": row.get("description"),
        "updated_by": row.get("updated_by"),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _validate_value(key: str, value: Any) -> None:
    """对 PUT 进来的 value 做白名单类型校验,失败抛 HTTPException(400)."""
    schema = CONFIG_VALUE_SCHEMA.get(key)
    if schema is None:
        # 不在白名单的 key 不接受任何值(404 会在调用前先判)
        raise HTTPException(status_code=400, detail=f"未知配置项: {key}")

    expected_type = schema['type']
    # bool 是 int 的子类 · 只有 type=bool 的 key(cron_enabled /
    # round_auto_resume_enabled)接 bool · 其他全拒绝
    if isinstance(value, bool):
        if expected_type is bool:
            return  # bool 不走 min/max · 直接通过
        raise HTTPException(
            status_code=400,
            detail=f"{key} 不接受布尔值,期望 {expected_type}",
        )
    if expected_type is bool:
        # cron_enabled 必须传 bool · 拒绝其他
        raise HTTPException(
            status_code=400,
            detail=f"{key} 类型错误: 期望 bool, 实际 {type(value).__name__}",
        )
    if not isinstance(value, expected_type):
        # tuple 形式打印更友好
        type_name = (
            "/".join(t.__name__ for t in expected_type)
            if isinstance(expected_type, tuple)
            else expected_type.__name__
        )
        raise HTTPException(
            status_code=400,
            detail=f"{key} 类型错误: 期望 {type_name}, 实际 {type(value).__name__}",
        )

    vmin = schema.get('min')
    vmax = schema.get('max')
    # Phase 9 (2026-05-26): str 类型用 len() 比较 (model_* 等),数字类型保持值比较
    # 不能让 "abc" < 1 直接抛 TypeError · 必须按语义分流
    if isinstance(value, str):
        # str 默认禁全空白 · 但 allow_empty=True 时允许 (P14-v12 调研专用 key 用)
        if not value.strip() and not schema.get('allow_empty'):
            raise HTTPException(status_code=400, detail=f"{key} 不能为空白字符串")
        # P14-v10: cron_days 必须 "1,16" / "1,10,20" 格式 · 每个数字 1-28
        if schema.get('pattern_days'):
            parts = [p.strip() for p in value.split(',') if p.strip()]
            if not parts:
                raise HTTPException(status_code=400, detail=f"{key} 至少要 1 个日期")
            for p in parts:
                if not p.isdigit():
                    raise HTTPException(status_code=400, detail=f"{key} 必须是数字: {p!r}")
                d = int(p)
                if d < 1 or d > 28:
                    raise HTTPException(status_code=400, detail=f"{key} 日期必须 1-28 (避免月末缺日): {d}")
        vlen = len(value)
        if vmin is not None and vlen < vmin:
            raise HTTPException(status_code=400, detail=f"{key} 长度必须 ≥ {vmin}")
        if vmax is not None and vlen > vmax:
            raise HTTPException(status_code=400, detail=f"{key} 长度必须 ≤ {vmax}")
    else:
        if vmin is not None and value < vmin:
            raise HTTPException(status_code=400, detail=f"{key} 必须 ≥ {vmin}")
        if vmax is not None and value > vmax:
            raise HTTPException(status_code=400, detail=f"{key} 必须 ≤ {vmax}")


# ============================================================
# 请求模型
# ============================================================

class UpdateConfigRequest(BaseModel):
    value: Any  # 任意 JSON-serializable, 由 _validate_value 做严格校验
    note: Optional[str] = Field(None, max_length=500, description="改动备注(打 log 用)")


class ResetConfigRequest(BaseModel):
    confirm_code: str = Field(..., description=f"必须等于 {RESET_CONFIRM_CODE}")
    keys: Optional[List[str]] = Field(
        None,
        description="None=全部 reset, 否则只 reset 指定 key",
    )


# ============================================================
# 1. GET /config — 列出所有配置项
# ============================================================

@router.get("/config", summary="列出所有配置项")
async def list_configs(request: Request):
    """返回全部 geo_research_config 行(按 key 升序)."""
    _require_admin(request)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT key, value_json, description, updated_by, updated_at
            FROM geo_research_config
            ORDER BY key ASC
            """
        )
        rows = cur.fetchall()
        cur.close()
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("[配置列表] 查询失败: %s", e)
        raise HTTPException(status_code=500, detail="查询配置列表失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {"configs": [_serialize_row(r) for r in rows]}


# ============================================================
# 2. GET /config/{key} — 单项
# ============================================================

@router.get("/config/{key}", summary="获取单项配置")
async def get_config(request: Request, key: str):
    """key 不存在 → 404."""
    _require_admin(request)

    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT key, value_json, description, updated_by, updated_at
            FROM geo_research_config
            WHERE key = %s
            """,
            (key,),
        )
        row = cur.fetchone()
        cur.close()
    except HTTPException:
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("[配置查询] key=%s 失败: %s", key, e)
        raise HTTPException(status_code=500, detail="查询配置项失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if row is None:
        raise HTTPException(status_code=404, detail=f"配置项不存在: {key}")
    return _serialize_row(row)


# ============================================================
# 3. PUT /config/{key} — 更新单项(含类型校验)
# ============================================================

@router.put("/config/{key}", summary="更新单项配置")
async def update_config(request: Request, key: str, payload: UpdateConfigRequest):
    """
    更新单个配置项。
    - key 不在 seed 列表 → 404(不允许新建未知 key)
    - value 类型/范围不符 → 400
    - 资金/熔断关键改动会写 logger.warning 审计
    """
    user = _require_admin(request)
    admin_id = _admin_id(user)

    # 1) key 必须在白名单 (CONFIG_VALUE_SCHEMA · P13-v13 后共 12 项: 8 业务 + 4 model_*)
    if key not in CONFIG_VALUE_SCHEMA:
        raise HTTPException(status_code=404, detail=f"配置项不存在: {key}")

    # 2) value 类型/范围校验
    _validate_value(key, payload.value)

    # 3) 写库(同事务读旧值 → 写新值)
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()

        # 取旧值(顺便确认行存在,虽然 seed 时已写入,生产环境也应该都在)
        cur.execute(
            """
            SELECT key, value_json, description, updated_by, updated_at
            FROM geo_research_config
            WHERE key = %s
            FOR UPDATE
            """,
            (key,),
        )
        old_row = cur.fetchone()
        if old_row is None:
            cur.close()
            # seed 缺失也按 404 处理(不允许偷偷新建)
            raise HTTPException(status_code=404, detail=f"配置项不存在: {key}")
        old_value = old_row["value_json"]

        # JSONB 写入: %s::jsonb + json.dumps
        cur.execute(
            """
            UPDATE geo_research_config
            SET value_json = %s::jsonb,
                updated_by = %s,
                updated_at = NOW()
            WHERE key = %s
            RETURNING key, value_json, description, updated_by, updated_at
            """,
            (json.dumps(payload.value), admin_id, key),
        )
        new_row = cur.fetchone()
        conn.commit()
        cur.close()
    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("[配置更新] 失败 admin=%s key=%s: %s", admin_id, key, e)
        raise HTTPException(status_code=500, detail="更新配置项失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 4) 审计日志(资金/熔断/锁/TTL 等关键 key 全部 warning,信息全够追溯)
    logger.warning(
        "[配置更新审计] admin=%s key=%s old=%s new=%s note=%s",
        admin_id, key, old_value, payload.value, payload.note or "",
    )

    # 5) P14-v10: cron_* 配置改后触发 scheduler 重注册 · 立即生效不用重启 backend
    next_cron_at = None
    if key in {'cron_enabled', 'cron_days', 'cron_hour'}:
        try:
            from services.research_monitor.scheduler_setup import reschedule_cron_from_db
            resched = reschedule_cron_from_db()
            next_cron_at = resched.get('next_run_at')
            logger.info(
                "[配置更新 · cron reschedule] admin=%s key=%s new=%s · "
                "registered=%s next=%s",
                admin_id, key, payload.value,
                resched.get('registered'), next_cron_at,
            )
        except Exception as e:
            logger.exception("[配置更新 · cron reschedule] 失败: %s", e)
            # 不阻塞 200 返回 (config 已写库 · 重启 backend 也能生效)

    return {
        "key": new_row["key"],
        "value": new_row["value_json"],
        "old_value": old_value,
        "updated_by": new_row.get("updated_by"),
        "updated_at": new_row["updated_at"].isoformat() if new_row.get("updated_at") else None,
        "next_cron_at": next_cron_at,  # cron_* key 改动时附带返回下次触发时间
    }


# ============================================================
# 4. POST /config/reset — 重置默认(危险操作)
# ============================================================

@router.post("/config/reset", summary="重置默认配置(危险操作)")
async def reset_config(request: Request, payload: ResetConfigRequest):
    """
    重置配置到 DEFAULT_CONFIGS。
    - confirm_code 不等于 RESET_RESEARCH_MONITOR_CONFIG → 403
    - keys=None → 全部 DEFAULT_CONFIGS reset (P13-v13 后 12 项: 8 业务 + 4 model_*)
                  否则只 reset 指定 key(unknown key 直接 404)
    - 整批单事务 + 审计 warning(含原值)
    """
    user = _require_admin(request)
    admin_id = _admin_id(user)

    # 1) 双保险: confirm_code
    if payload.confirm_code != RESET_CONFIRM_CODE:
        raise HTTPException(status_code=403, detail="confirm_code 错误,拒绝重置")

    # 2) 决定 reset 范围
    if payload.keys is None:
        target_keys = list(DEFAULT_CONFIGS.keys())
    else:
        if not payload.keys:
            raise HTTPException(status_code=400, detail="keys 列表为空")
        unknown = [k for k in payload.keys if k not in DEFAULT_CONFIGS]
        if unknown:
            raise HTTPException(status_code=404, detail=f"未知配置项: {unknown}")
        # 去重保序
        seen = set()
        target_keys = []
        for k in payload.keys:
            if k not in seen:
                seen.add(k)
                target_keys.append(k)

    from db.connection import get_connection
    conn = get_connection()
    old_values: Dict[str, Any] = {}
    try:
        cur = conn.cursor()

        # 3) 取原值(给审计日志)
        cur.execute(
            """
            SELECT key, value_json
            FROM geo_research_config
            WHERE key = ANY(%s)
            """,
            (target_keys,),
        )
        for r in cur.fetchall():
            old_values[r["key"]] = r["value_json"]

        # 4) 删除原配置 + 重新插入 default(单事务)
        cur.execute(
            "DELETE FROM geo_research_config WHERE key = ANY(%s)",
            (target_keys,),
        )

        for k in target_keys:
            default_val = DEFAULT_CONFIGS[k]
            desc = DEFAULT_DESCRIPTIONS.get(k)
            cur.execute(
                """
                INSERT INTO geo_research_config (key, value_json, description, updated_by, updated_at)
                VALUES (%s, %s::jsonb, %s, %s, NOW())
                """,
                (k, json.dumps(default_val), desc, admin_id),
            )

        conn.commit()
        cur.close()
    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("[配置重置] 失败 admin=%s keys=%s: %s", admin_id, target_keys, e)
        raise HTTPException(status_code=500, detail="重置配置失败")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 5) 审计 warning(含每项原值,便于追溯)
    logger.warning(
        "[配置重置审计] admin=%s 范围=%s 原值=%s",
        admin_id, target_keys, old_values,
    )

    return {
        "reset_count": len(target_keys),
        "keys": target_keys,
    }
