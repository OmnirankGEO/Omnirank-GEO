"""社媒板块系统设置 DB · 2026-05-12 R15

2 张表:
  · user_social_preferences  — 用户级偏好(1 user 1 row)
  · social_admin_settings    — 管理员 KV 设置(全局单点)

设计:
  · ensure_schema 启动时调用 · 幂等 CREATE IF NOT EXISTS + seed
  · 读 helper(get_user_prefs / get_admin_setting)有 default fallback · 不抛
  · 写 helper(update_user_prefs / set_admin_setting)只动指定 key 不覆盖其他

不动:
  · GEO 板块 · C 端 · M3
  · users / auth_users 现有 schema(只 FK 引用 user_id)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type, TypeVar

logger = logging.getLogger("GEO-SocialPrefs")

_ensured = False
T = TypeVar("T")


def get_connection():
    from db.connection import get_connection as _get_connection
    return _get_connection()


# ============ 默认值 SSOT(前端 / 后端共用) ============

USER_PREFS_DEFAULTS: Dict[str, Any] = {
    "default_guardrail": "balanced",        # 'balanced' | 'creative'
    "default_duration_seconds": 180,         # 60 | 180 | 300
    "default_research_mode": "auto",         # 'auto' | 'force'
    "default_quality_tier": "normal",        # 'normal' | 'professional' | 'super'
    "theme": "system",                       # 'light' | 'dark' | 'system'
    "notify_email": True,
    "notify_inapp": True,
}

# 校验白名单(防注入)
USER_PREFS_VALIDATORS: Dict[str, callable] = {
    "default_guardrail": lambda v: v in ("balanced", "creative"),
    "default_duration_seconds": lambda v: int(v) in (60, 180, 300),
    "default_research_mode": lambda v: v in ("auto", "force"),
    "default_quality_tier": lambda v: v in ("normal", "professional", "super"),
    "theme": lambda v: v in ("light", "dark", "system"),
    "notify_email": lambda v: isinstance(v, bool),
    "notify_inapp": lambda v: isinstance(v, bool),
}

ADMIN_SETTINGS_DEFAULTS: Dict[str, tuple] = {
    # key: (value, type, description)
    "flywheel_v2_enabled": ("true", "bool", "飞轮 v2 LLM-first 引擎全局开关(替代 env FLYWHEEL_V2_ENABLED)"),
    # 2026-05-18 老板拍 AB 实测后切 flash · 广东乐居 26.6MB PDF 3 页测试 plus vs flash:
    # · 成功率 plus 2/3 vs flash 3/3
    # · 延迟 flash 快 30% (16.3s vs 23.4s)
    # · 输出字数 flash 多 52% (637 vs 418)
    # · 限时价 flash 省 26% · 原价后 flash 省 63% (¥0.10 vs ¥0.27)
    "vision_model": ("qwen3.6-flash", "string", "图片转写 + PDF 视觉模型(默认 qwen3.6-flash · AB 实测完胜 plus)"),
    # 🔴 [WO_220-c1 2026-09-15] GEO 线视觉**另起一个键**,不共用上面那个。
    #   原因:上面那行会被 `ensure_schema()` **播种进库**,于是「模块里的兜底默认」
    #   永远轮不到 —— 实证:走真实代码路径打真厂商时,解析器返回的是
    #   `qwen3.6-flash / dashscope`,而我的 13 条单元判据全都桩掉了模型解析,一条没红。
    #   而这个键有四个消费方,社媒那两个仍只打百炼,改它的值会把它们弄坏。
    #   ⇒ 共享键一旦两条线分叉就不能再共享。社媒继续用 `vision_model`,GEO 用这个。
    "geo_vision_model": ("deepseek-flash", "string", "GEO 图片识别/OCR 质检视觉模型(默认 deepseek-flash · 官方线唯一能识图的档)"),
    "pdf_max_vision_images": ("5", "int", "PDF 嵌入图 vision 转写上限(0-10)· 防成本爆"),
    # 🔴 [WO_224-c1 2026-09-15] 豆包出网并发上限(账号级 QPS 闸)。
    #   Deploy 224-d1 实测:同秒并发 6–13 个**全部被 429 拒**,
    #   同 task 四引擎发起时刻差全部 = 0.0 秒(完全并行扇出)。
    #   ⇒ 根因是并发打爆账号 QPS,不是配额;按小时均值看会得出「量不大」的错觉。
    #   消费方单点:`services/doubao_concurrency`(闸落在豆包唯一出网点
    #   `tools/ai_visibility/ai_tester._query_doubao_search_impl`,
    #   监测/诊断/情感分类三条线共用,因为它们打的是**同一个账号**)。
    #   🔴 数值是**系统系数**,交付文档不记数;这里是它的唯一登记处,
    #      `services/doubao_concurrency._registered_default()` 从这里取,不另写一份。
    "geo_doubao_max_concurrency": ("3", "int", "豆包出网并发上限(账号级 QPS 闸 · 可后台调)"),
    "flywheel_v2_turn_prompt_override": ("", "text", "建档对话 V2_TURN_PROMPT 在线 override · 空=用代码 hardcoded"),
    "ADVISOR_PUBLIC_IDENTITY_V2_ENABLED": ("false", "bool", "专家市场公共身份 v2 灰度开关(公开名/内部来源名隔离)"),
    "flywheel_memory_write_enabled": ("false", "bool", "飞轮对话沉淀到写稿资料库开关"),
    "prompt_known_fields_enabled": ("false", "bool", "建档对话读取已确认资料并避免重复追问"),
    "completeness_v2_enabled": ("false", "bool", "社媒资料完整度 v2 评分开关"),
    "backfill_profile_flywheel_enabled": ("false", "bool", "历史建档对话回填到写稿资料库开关"),
    "agent_loop_enabled": ("true", "bool", "社媒主对话框 Agent Loop 全局开关 · 关闭后走旧 fast-path"),
    "agent_loop_fallback": ("fast_path", "string", "Agent Loop 异常兜底模式:fast_path/dry_run_only/hard_off"),
    "chef_first_prompt_enabled": ("true", "bool", "写稿 chef-first 强约束模式(advisor 知识库作灵魂 + 客户表达习惯)· 关=老温和"),
    # 2026-05-17 老板拍 B · 激活 Deploy-CTO 5/17 已 commit 但未启用的方法策展 + 内容评审(两者所在社媒工具包已随开源 E3 删除)
    # 分两个 flag:
    #   - method_curator_enabled  default false · 翻 true 后 L1 agentic method-selection 进 prompt(改生成行为 · AB 用)
    #   - content_judge_logging_enabled default true · 后台异步 judge 8 维 + 记方法选型日志表(被动收 baseline 数据 · 不改用户感知;消费方已随开源 E3 删除)
    "method_curator_enabled": ("false", "bool", "L1 方法策展 agentic 选型(写稿前先推理客户核心问题 → 检索 query 喂 hybrid_retriever)· 默认关 · 消费方社媒工具包已随开源 E3 删除,本开关已无读者"),
    "content_judge_logging_enabled": ("true", "bool", "写稿完成后异步 8 维评分 + 记方法选型日志表(被动 baseline · 不影响用户感知)· 默认开 · 消费方社媒工具包已随开源 E3 删除,本开关已无读者"),
    # 2026-05-18 老板拍 A · LLM-first chat 决策(根治 markers 穷举补漏)
    # flag ON · build_social_chat_decision 跳过 deterministic preflight · LLM 总控全权决策
    # LLM 失败 / timeout 仍走 deterministic 兜底(不会全炸)
    # 2026-05-18 老板灵魂拷问后拍 GO · default 从 false 翻 true · 默认 LLM-first
    # 现有 prod 记录需 SQL: UPDATE social_admin_settings SET setting_value='true' WHERE setting_key='chat_llm_first_enabled';
    "chat_llm_first_enabled": ("true", "bool", "LLM-first chat 决策(跳 deterministic preflight)· 根治 markers 穷举 · LLM 失败仍兜底 · 老板 2026-05-18 拍 GO 默认开启"),
    # [Deploy-CTO 2026-05-19] Clear-Site-Data middleware mode · 救 mobile bfcache 卡死老用户
    # 3 档:'off' 不加 header / 'cache' 仅清 HTTP cache(default · 不影响 localStorage)/
    #        'cache,storage' 救火核选项(全员重登 + 草稿丢 · 救 mobile webview bfcache 死锁)
    # 救火 SQL: UPDATE social_admin_settings SET setting_value='cache,storage' WHERE setting_key='clear_site_data_mode';
    # 撤回 SQL: UPDATE social_admin_settings SET setting_value='cache' WHERE setting_key='clear_site_data_mode';
    "clear_site_data_mode": ("cache", "string", "API 响应 Clear-Site-Data 模式 · off/cache/cache,storage · storage 模式 24-48h 救火后必须撤回防全员重登"),
    # 2026-05-19 老板拍 A+ · /api/content/chat 调研类意图(平台名+调研动词双命中)走 agent_loop tikhub
    # 双开关 AND:agent_loop_enabled(全局总开关)AND chat_agent_loop_research_enabled(本子开关)
    # 出问题 1 SQL 关:UPSERT setting_value='false' · 详见 docs/AI-CONTEXT/CHAT_AGENT_LOOP_RESEARCH_PLAN_2026-05-19.md
    "chat_agent_loop_research_enabled": ("true", "bool", "/api/content/chat 调研类意图(抖音/B站/小红书热门/最近/对标)走 agent_loop tikhub · 关闭退回老 orchestrator clarify 反问 · 老板 2026-05-19 A+ 灰度"),
}

# 允许 admin 改的 key 白名单(防写入未知 key 攻击)
ADMIN_SETTINGS_KEYS = set(ADMIN_SETTINGS_DEFAULTS.keys())


# ============ Schema 初始化 ============

def ensure_schema():
    """启动时调用 · 幂等"""
    global _ensured
    if _ensured:
        return
    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        # A · user_social_preferences
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_social_preferences (
            user_id INTEGER PRIMARY KEY,
            default_guardrail VARCHAR(20) DEFAULT 'balanced',
            default_duration_seconds INTEGER DEFAULT 180,
            default_research_mode VARCHAR(10) DEFAULT 'auto',
            default_quality_tier VARCHAR(20) DEFAULT 'normal',
            theme VARCHAR(10) DEFAULT 'system',
            notify_email BOOLEAN DEFAULT TRUE,
            notify_inapp BOOLEAN DEFAULT TRUE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # B · social_admin_settings
        cur.execute("""
        CREATE TABLE IF NOT EXISTS social_admin_settings (
            setting_key VARCHAR(64) PRIMARY KEY,
            setting_value TEXT,
            setting_type VARCHAR(16) DEFAULT 'string',
            description TEXT,
            updated_by_user_id INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # Seed 默认值(ON CONFLICT DO NOTHING · 已有不覆盖)
        for key, (value, typ, desc) in ADMIN_SETTINGS_DEFAULTS.items():
            cur.execute(
                """
                INSERT INTO social_admin_settings (setting_key, setting_value, setting_type, description)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (setting_key) DO NOTHING
                """,
                (key, value, typ, desc),
            )
        cur.close()
        _ensured = True
        logger.info("social_preferences_db schema ready")
    except Exception as e:
        logger.error(f"social_preferences_db ensure_schema failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============ 用户偏好 CRUD ============

def get_user_prefs(user_id: int) -> Dict[str, Any]:
    """返用户偏好 · 不存在返默认值(不写库 · lazy create on first PATCH)"""
    ensure_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT default_guardrail, default_duration_seconds, default_research_mode,
                   default_quality_tier, theme, notify_email, notify_inapp, updated_at
            FROM user_social_preferences WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return {**USER_PREFS_DEFAULTS, "updated_at": None}
        # RealDictCursor 返 dict · row 已经是 dict-like
        return {
            "default_guardrail": row["default_guardrail"] or USER_PREFS_DEFAULTS["default_guardrail"],
            "default_duration_seconds": row["default_duration_seconds"] or USER_PREFS_DEFAULTS["default_duration_seconds"],
            "default_research_mode": row["default_research_mode"] or USER_PREFS_DEFAULTS["default_research_mode"],
            "default_quality_tier": row["default_quality_tier"] or USER_PREFS_DEFAULTS["default_quality_tier"],
            "theme": row["theme"] or USER_PREFS_DEFAULTS["theme"],
            "notify_email": bool(row["notify_email"]) if row["notify_email"] is not None else USER_PREFS_DEFAULTS["notify_email"],
            "notify_inapp": bool(row["notify_inapp"]) if row["notify_inapp"] is not None else USER_PREFS_DEFAULTS["notify_inapp"],
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_user_prefs(user_id: int, patch: Dict[str, Any]) -> Dict[str, Any]:
    """单字段 patch · 只接受白名单字段 · 校验值合法"""
    ensure_schema()
    # 过滤 + 校验
    valid_patch: Dict[str, Any] = {}
    for key, value in patch.items():
        if key not in USER_PREFS_VALIDATORS:
            continue
        try:
            if not USER_PREFS_VALIDATORS[key](value):
                continue
        except Exception:
            continue
        valid_patch[key] = value
    if not valid_patch:
        return get_user_prefs(user_id)

    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        # UPSERT pattern
        keys = list(valid_patch.keys())
        values = [valid_patch[k] for k in keys]
        col_list = ", ".join(keys)
        placeholder_list = ", ".join(["%s"] * len(keys))
        update_clause = ", ".join([f"{k} = EXCLUDED.{k}" for k in keys])
        cur.execute(
            f"""
            INSERT INTO user_social_preferences (user_id, {col_list}, updated_at)
            VALUES (%s, {placeholder_list}, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET {update_clause}, updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, *values),
        )
        cur.close()
        return get_user_prefs(user_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def reset_user_prefs(user_id: int) -> Dict[str, Any]:
    """重置为默认值 · DELETE row 让下次 GET 返默认"""
    ensure_schema()
    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DELETE FROM user_social_preferences WHERE user_id = %s", (user_id,))
        cur.close()
        return {**USER_PREFS_DEFAULTS, "updated_at": None}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============ 管理员 KV 设置 ============

def get_admin_setting(key: str, cast: Type[T] = str, default: Optional[T] = None) -> Optional[T]:
    """读单 key · 自动 cast(int / bool / str)· 失败返 default

    被小帮上传解析等在役代码调用(原另有社媒飞轮引擎 v2,已随开源 E3 删除)· 必须容错不抛
    """
    if key not in ADMIN_SETTINGS_KEYS:
        return default
    try:
        ensure_schema()
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT setting_value, setting_type FROM social_admin_settings WHERE setting_key = %s", (key,))
            row = cur.fetchone()
            cur.close()
            if not row or row["setting_value"] is None:
                return default
            raw = row["setting_value"]
            typ = (row["setting_type"] or "string").lower()
            # 优先按 cast 转 · 失败用 setting_type 兜底
            if cast is bool or typ == "bool":
                return bool(str(raw).strip().lower() in ("true", "1", "yes", "on"))
            if cast is int or typ == "int":
                try:
                    return int(raw)
                except Exception:
                    return default
            return str(raw)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"get_admin_setting({key}) failed: {e}")
        return default


def get_all_admin_settings() -> Dict[str, Any]:
    """返所有 admin settings · 给 admin GET 接口"""
    ensure_schema()
    result: Dict[str, Any] = {}
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT setting_key, setting_value, setting_type, description, updated_at FROM social_admin_settings")
        rows = cur.fetchall()
        cur.close()
        existing = {r["setting_key"]: r for r in rows}
        # 合并默认值 + DB 实际值(防新增 key 老库没 seed)
        for key, (default_val, typ, desc) in ADMIN_SETTINGS_DEFAULTS.items():
            row = existing.get(key)
            raw = row["setting_value"] if row else default_val
            actual_type = (row["setting_type"] if row else typ).lower()
            actual_desc = row["description"] if row else desc
            updated_at = row["updated_at"].isoformat() if row and row.get("updated_at") else None
            # 转换值
            value: Any = raw
            if actual_type == "bool":
                value = str(raw).strip().lower() in ("true", "1", "yes", "on")
            elif actual_type == "int":
                try:
                    value = int(raw)
                except Exception:
                    value = 0
            result[key] = {
                "value": value,
                "type": actual_type,
                "description": actual_desc,
                "updated_at": updated_at,
            }
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def set_admin_setting(key: str, value: Any, updated_by_user_id: Optional[int] = None) -> bool:
    """改单 key · 限白名单 · 值按 type 序列化"""
    if key not in ADMIN_SETTINGS_KEYS:
        return False
    ensure_schema()
    typ = ADMIN_SETTINGS_DEFAULTS[key][1]
    # 序列化
    if typ == "bool":
        serialized = "true" if (value in (True, "true", "True", "1", 1, "yes", "on")) else "false"
    elif typ == "int":
        try:
            iv = int(value)
        except Exception:
            return False
        # 简单 sanity check
        if key == "pdf_max_vision_images" and not (0 <= iv <= 10):
            return False
        serialized = str(iv)
    else:
        serialized = str(value) if value is not None else ""
        if key == "agent_loop_fallback" and serialized not in ("fast_path", "dry_run_only", "hard_off"):
            return False
        # 字符串长度限制(防滥用)
        if key == "flywheel_v2_turn_prompt_override" and len(serialized) > 20000:
            return False

    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE social_admin_settings
            SET setting_value = %s,
                updated_by_user_id = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE setting_key = %s
            """,
            (serialized, updated_by_user_id, key),
        )
        cur.close()
        return True
    except Exception as e:
        logger.error(f"set_admin_setting({key}) failed: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
