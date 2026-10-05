"""
社媒操盘手 v3.0 - 客户档案数据库操作
"""

import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path

# 数据库路径（保留用于 init 函数兼容）
DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "geo_diagnosis.db"


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


_ensured_columns = False

def _ensure_columns():
    """确保 client_profiles 表包含所有必需列（仅首次调用时执行）"""
    global _ensured_columns
    if _ensured_columns:
        return
    try:
        conn = get_connection()
        conn.autocommit = True
        cursor = conn.cursor()
        # 检查 structured_knowledge 列是否存在
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'client_profiles' AND column_name = 'structured_knowledge'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE client_profiles ADD COLUMN structured_knowledge TEXT")
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'client_profiles' AND column_name = 'personality_profile'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE client_profiles ADD COLUMN personality_profile TEXT")
        # Phase 3: C端渐进建档字段
        for col, col_type in [
            ('creator_type', 'VARCHAR(50)'),
            ('content_frequency', 'VARCHAR(50)'),
            ('monetization_goal', 'VARCHAR(50)'),
            ('follower_count', 'VARCHAR(50)'),
            ('profile_completeness', 'JSONB DEFAULT \'{}\''),
            ('preferred_advisor_id', 'TEXT'),
            ('preferred_expert_id', 'TEXT'),  # CTO-13.0 2026-04-19: 专家顾问偏好（行业智囊）· advisors 表 role=expert
            ('plan_mode', "VARCHAR(20) DEFAULT 'inspiration'"),  # 计划模式
            # 真相画像（仅管理员可见）
            ('admin_insight', 'TEXT'),          # 真相分析JSON
            ('admin_insight_level', 'VARCHAR(20)'),  # 分析等级: inactive/basic/advanced/deep
            ('admin_insight_updated_at', 'TIMESTAMP'),
            # 深度解析任务状态（避免前端刷新后丢运行状态）
            # CTO-15.16 M1c · industry_brief JSONB 主体也加进来 · 旧库可能没建
            # (deep_analyze 假设此列存在 · 实测某些 docker DB image 缺这一列 → 写入静默失败)
            ('industry_brief', "JSONB"),                  # 深度解析产出 · L3 用户层知识
            ('industry_brief_status', "VARCHAR(20)"),     # idle/running/done/failed
            ('industry_brief_started_at', 'TIMESTAMP'),   # 开始时间，前端用来计算已耗时
            # v3.7 知识库审核工作流（CTO-15.0 2026-04-19）
            ('industry_brief_confirmed', "BOOLEAN DEFAULT FALSE"),           # 是否用户审核确认入库
            ('industry_brief_confirmed_at', 'TIMESTAMP'),                    # 确认时间（用来判断生产链路是否可融合）
            ('industry_brief_partial_fields', "JSONB DEFAULT NULL"),          # 部分入库字段清单 null=全量 / ['my_audience',...] = 指定字段
            ('industry_brief_edits', "JSONB DEFAULT NULL"),                   # 用户手动编辑过的字段快照（key=字段名 value=原 AI 值，方便对比）
            ('industry_brief_version', "INT DEFAULT 0"),                      # 当前版本号（配合 industry_brief_history 表）
            # [CTO-13.0 2026-04-19 S1.4] brief → profile 一键应用 · 5 新字段
            # 按 CTO-15.2 接入指南 §3 字段映射表设计 · differentiation/success_cases 在 S1.1 已加跳过
            ('service_scope', "VARCHAR(20)"),                                 # local/national/hybrid（brief.service_scope 直接覆盖）
            ('service_scope_reasoning', 'TEXT'),                              # scope 判定理由
            ('local_competitors', "JSONB DEFAULT '[]'"),                      # 本地竞品 · 来自 brief.my_local_competitors
            ('brief_applied_version', 'INT DEFAULT 0'),                       # 最近应用了 brief v? · 防过期 brief 重复应用
            ('brief_applied_at', 'TIMESTAMP'),                                # 最近应用时间
            # [CTO-15.9 2026-04-25 M1b M1] business_type SSOT · profile 派生层(不在 brands 主表加 · PRD v1.1)
            ('business_type', "VARCHAR(20) DEFAULT 'B2C'"),                   # B2C / B2B / 政企 · 枚举由应用层校验
            ('city_scope', "VARCHAR(20) DEFAULT 'local'"),                    # local / national · 驱动拓词 prompt 分支(同 service_scope 别名)
            ('social_fields', "JSONB DEFAULT '{}'::jsonb"),                   # 社媒工作台 8 项采访字段
            # [2026-06-02 GEO CTO] 客户联系方式 4 字段(写作结尾引流·自发布完整/媒体软化)
            # 🔴 brands.contact 保持「联系人名」语义不动 · 联系方式独立入 client_profiles
            ('contact_phone', 'TEXT'),
            ('contact_wechat', 'TEXT'),
            ('contact_website', 'TEXT'),
            ('contact_address', 'TEXT'),
            # [WO_220-c2 2026-09-16] 写作大厅基础资料表里**没有同语义真列**的字段
            # (products_services / proof_cases)。其余四个复用真列,不在这里。
            # 权威定义是 db/migration_062_client_profile_basic_info_fields_2026_09_16.sql,
            # 这一行是给**建于迁移之前的旧库**兜底(本仓一张表有四套 schema 定义,
            # 只改迁移会让判据整包红)。两处的列名与类型必须逐字一致。
            ('basic_info_fields', "JSONB DEFAULT '{}'::jsonb"),
        ]:
            cursor.execute(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'client_profiles' AND column_name = '{col}'
            """)
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE client_profiles ADD COLUMN {col} {col_type}")

        # v3.7 industry_brief 版本化历史表（Phase 4）
        # profile_id 必须 VARCHAR —— client_profiles.id 是 shortuuid 或 'auto_xxx' 字符串，不是 INT
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS industry_brief_history (
                id SERIAL PRIMARY KEY,
                profile_id VARCHAR(64) NOT NULL,
                version_num INT NOT NULL,
                brief_data JSONB NOT NULL,
                confirmed BOOLEAN DEFAULT FALSE,
                partial_fields JSONB DEFAULT NULL,
                source VARCHAR(20) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                created_by INT,
                UNIQUE(profile_id, version_num)
            )
        """)
        # Hot-fix：如果表已被错误地建成 profile_id INT（首次部署 bug），强制 ALTER 成 VARCHAR
        # 用 marker 表追踪这个修正，避免每次启动重跑 ALTER
        cursor.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'industry_brief_history' AND column_name = 'profile_id'
        """)
        r = cursor.fetchone()
        if r and r.get("data_type") in ("integer", "bigint"):
            cursor.execute("ALTER TABLE industry_brief_history ALTER COLUMN profile_id TYPE VARCHAR(64) USING profile_id::varchar")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_brief_history_profile
            ON industry_brief_history(profile_id, version_num DESC)
        """)

        cursor.close()
        conn.close()
        # BUG-6-010: Only set flag on success — if migration fails, retry next call
        _ensured_columns = True
    except Exception:
        pass  # 并发竞态或表不存在，忽略；will retry next call


def _row_to_dict(row) -> Optional[Dict]:
    """将Row对象转换为字典"""
    if row is None:
        return None
    return dict(row)


# ========== v3.7 industry_brief 审核工作流 辅助函数 ==========

# 13 个合法字段 · 对应 tools/industry_knowledge_collector.deep_analyze_user 产出
INDUSTRY_BRIEF_KNOWN_FIELDS = {
    "my_audience", "my_differentiation", "content_strategy",
    "local_competitors", "local_platform_tips", "city_context",
    "top_content_formats", "content_pain_points", "acquisition_paths",
    "differentiation_hints", "regional_kw_ideas", "top_cases", "target_users",
}


def get_effective_brief(profile: Dict) -> Dict:
    """读出"对生产链路有效"的 industry_brief 字典（GEO/社媒 prompt 注入唯一入口）

    规则：
    1. industry_brief_status != 'done' → 返空（防未完成的 brief 污染生产）
    2. industry_brief_confirmed != TRUE → 返空（未审核的不给生产端看到）
    3. partial_fields IS NULL → 返完整 brief（全量入库）
    4. partial_fields = ['my_audience',...] → 返仅包含这些 key 的子集

    不调 DB，纯内存操作。调用方需先 get_profile()。
    """
    if not isinstance(profile, dict):
        return {}
    if profile.get("industry_brief_status") != "done":
        return {}
    if not profile.get("industry_brief_confirmed"):
        return {}
    raw = profile.get("industry_brief") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    partial = profile.get("industry_brief_partial_fields")
    if isinstance(partial, str):
        try:
            partial = json.loads(partial)
        except Exception:
            partial = None
    if isinstance(partial, list) and partial:
        return {k: v for k, v in raw.items() if k in partial}
    return raw


def get_effective_brief_by_brand(brand_id: int) -> Dict:
    """通过 brand_id 查出有效 brief（writer / agent 生产链路用 · 自带 DB 查询）
    找不到 profile 或未入库 → 返空 dict（调用方应优雅降级）
    """
    if not brand_id:
        return {}
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT industry_brief, industry_brief_status, industry_brief_confirmed,
                       industry_brief_partial_fields
                FROM client_profiles
                WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
                ORDER BY updated_at DESC LIMIT 1
            """, (brand_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        return get_effective_brief(dict(row))
    except Exception:
        return {}


def get_structured_knowledge_by_brand(brand_id: int) -> Dict:
    """通过 brand_id 读取 5 维结构化业务画像(products/painPoints/customers/differentiation/cases)。
    GEO 写作生产链路用 · 代理在「客户资料中心 · 业务事实」维护的第一手结构化资料(比诊断蒸馏更权威)。
    找不到 profile 或字段空 → 返空 dict(调用方优雅降级)。
    """
    if not brand_id:
        return {}
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT structured_knowledge
                FROM client_profiles
                WHERE brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
                ORDER BY updated_at DESC LIMIT 1
            """, (brand_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row.get('structured_knowledge'):
            return {}
        raw = row['structured_knowledge']
        sk = json.loads(raw) if isinstance(raw, str) else raw
        return sk if isinstance(sk, dict) else {}
    except Exception:
        return {}


def get_contact_info_by_brand(brand_id: int) -> Dict:
    """通过 brand_id 读客户联系方式 4 字段 + 品牌名(写作/发布联系方式渲染唯一入口)。
    返回 {phone, wechat, website, address, brand_name}· 找不到/未填各字段返回空串。
    🔴 渲染入口必须传服务端可信 brand_id(不信前端·防越权读到别客户联系方式)。
    """
    out = {"phone": "", "wechat": "", "website": "", "address": "", "brand_name": ""}
    if not brand_id:
        return out
    try:
        _ensure_columns()
    except Exception:
        pass
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT cp.contact_phone, cp.contact_wechat, cp.contact_website, cp.contact_address,
                       COALESCE(b.name, cp.name) AS brand_name
                FROM client_profiles cp
                LEFT JOIN brands b ON b.id = cp.brand_id
                WHERE cp.brand_id = %s AND (cp.is_deleted = 0 OR cp.is_deleted IS NULL)
                ORDER BY cp.updated_at DESC LIMIT 1
            """, (brand_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if row:
            out["phone"] = (row.get("contact_phone") or "").strip()
            out["wechat"] = (row.get("contact_wechat") or "").strip()
            out["website"] = (row.get("contact_website") or "").strip()
            out["address"] = (row.get("contact_address") or "").strip()
            out["brand_name"] = (row.get("brand_name") or "").strip()
    except Exception:
        pass
    return out


def _parse_json_fields(profile: Dict) -> Dict:
    """解析JSON字段"""
    json_fields = [
        'products', 'pain_points', 'competitors',
        'persona_catchphrases', 'persona_golden_quotes',
        'target_platforms', 'successful_patterns', 'negative_feedback',
        'knowledge_files',  # 业务知识库文件列表
        'brand_display_names',  # 品牌对外显示名称
        'content_pillars',  # 内容支柱（JSON数组）
        'structured_knowledge',  # 5维度结构化业务知识
        'personality_profile',  # IP全息画像（MBTI + 6维雷达 + 灵魂标签）
        'profile_completeness',  # 渐进建档完成度
        'industry_brief',  # 深度行业调研/GEO物料中心（含 geo_assets）
        'local_competitors',  # 本地竞品
        'industry_brief_partial_fields',  # v3.7 用户选择入库的字段清单
        'industry_brief_edits',           # v3.7 用户编辑过的字段历史快照
        'success_cases',                  # 成功案例
        'testimonials',                   # 客户评价
        'social_fields',                  # 社媒工作台 采访字段
        # [WO_220-c2] 写作大厅基础资料表里没有同语义真列的字段(dict,不是数组)
        'basic_info_fields',
    ]
    for field in json_fields:
        if profile.get(field):
            try:
                profile[field] = json.loads(profile[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return profile


def _json_text_or_none(value: Any) -> Optional[str]:
    """Serialize structured profile text fields only when callers pass objects."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


# ========== CRUD 操作 ==========

def create_profile(
    name: str,
    industry: str = None,
    business: str = None,
    products: List[str] = None,
    target_users: str = None,
    pain_points: List[str] = None,
    competitors: List[str] = None,
    persona_positioning: str = None,
    persona_tone: str = None,
    persona_catchphrases: List[str] = None,
    persona_background: str = None,
    persona_golden_quotes: List[str] = None,
    target_platforms: List[str] = None,
    brand_constraints: str = None,
    # 公司扩展信息
    company_intro: str = None,
    core_value: str = None,
    selling_points: str = None,
    success_cases: str = None,
    testimonials: str = None,
    # 新增 IP 人设字段
    story_type: str = None,
    differentiation: str = None,
    content_direction: str = None,
    # 品牌别名
    brand_display_names: List[str] = None,  # 对外品牌名（如"奥莱超级会员店"）
    # 统一人设字段（从social_personas合并）
    visual_style: str = None,  # 视觉调性
    content_pillars: List[str] = None,  # 内容支柱（JSON数组）
    # 结构化业务知识（5维度）
    structured_knowledge: dict = None,
    # IP全息画像（JSON）
    personality_profile: str = None,
    # 其他可选字段
    brand_id: int = None,
    **kwargs  # 忽略其他未知字段
) -> str:
    """创建客户档案，返回ID"""
    _ensure_columns()
    conn = get_connection()
    try:
        cursor = conn.cursor()

        profile_id = str(uuid.uuid4())[:8]  # 短UUID

        cursor.execute("""
            INSERT INTO client_profiles (
                id, name, industry, business, products, target_users, pain_points, competitors,
                persona_positioning, persona_tone, persona_catchphrases, persona_background, persona_golden_quotes,
                target_platforms, brand_constraints,
                company_intro, core_value, selling_points, success_cases, testimonials,
                story_type, differentiation, content_direction, brand_id, brand_display_names,
                visual_style, content_pillars, structured_knowledge, personality_profile
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            profile_id, name, industry, business,
            json.dumps(products) if products else None,
            target_users,
            json.dumps(pain_points) if pain_points else None,
            json.dumps(competitors) if competitors else None,
            persona_positioning, persona_tone,
            json.dumps(persona_catchphrases) if persona_catchphrases else None,
            persona_background,
            json.dumps(persona_golden_quotes) if persona_golden_quotes else None,
            json.dumps(target_platforms) if target_platforms else None,
            brand_constraints,
            company_intro, core_value, selling_points,
            _json_text_or_none(success_cases),
            _json_text_or_none(testimonials),
            story_type, differentiation, content_direction, brand_id,
            json.dumps(brand_display_names) if brand_display_names else None,
            visual_style,
            json.dumps(content_pillars) if content_pillars else None,
            _json_text_or_none(structured_knowledge),
            personality_profile,
        ))

        conn.commit()
        return profile_id
    finally:
        conn.close()





def get_profile(profile_id: str) -> Optional[Dict]:
    """获取单个档案"""
    _ensure_columns()
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM client_profiles
            WHERE id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
        """, (profile_id,))

        row = cursor.fetchone()

        if row:
            profile = _row_to_dict(row)
            return _parse_json_fields(profile)
        return None
    finally:
        conn.close()


def list_profiles(include_deleted: bool = False, brand_ids: List[int] = None) -> List[Dict]:
    """获取档案列表
    
    Args:
        include_deleted: 是否包含已删除的档案
        brand_ids: 可选，限定返回的 brand_id 列表（RBAC 数据隔离用）
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        conditions = []
        params = []
    
        if not include_deleted:
            conditions.append("(is_deleted = 0 OR is_deleted IS NULL)")
    
        if brand_ids is not None:
            if not brand_ids:
                conn.close()
                return []  # 空列表 = 无权限
            placeholders = ",".join(["%s"] * len(brand_ids))
            conditions.append(f"brand_id IN ({placeholders})")
            params.extend(brand_ids)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        cursor.execute(f"SELECT * FROM client_profiles WHERE {where_clause} ORDER BY created_at DESC", params)
    
        rows = cursor.fetchall()
        conn.close()
    
        profiles = [_parse_json_fields(_row_to_dict(row)) for row in rows]
        return profiles
    finally:
        try:
            conn.close()
        except Exception: pass


def list_profiles_deduplicated(brand_ids: List[int] = None) -> List[Dict]:
    """获取去重后的档案列表（按 name+brand_id 去重，保留最新一条）

    [CTO-13.0] 附加两个辅助字段帮前端诊断 name 漂移：
      - brand_name: 所挂载品牌的真实 name（LEFT JOIN brands）
      - name_conflict: True 当 profile.name != brand_name（历史 desync 的信号）
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        conditions = ["(cp.is_deleted = 0 OR cp.is_deleted IS NULL)"]
        params = []

        if brand_ids is not None:
            if not brand_ids:
                conn.close()
                return []
            placeholders = ",".join(["%s"] * len(brand_ids))
            conditions.append(f"cp.brand_id IN ({placeholders})")
            params.extend(brand_ids)

        where_clause = " AND ".join(conditions)
        cursor.execute(f"""
            SELECT DISTINCT ON (cp.name, COALESCE(cp.brand_id, -1))
                   cp.*, b.name AS brand_name
            FROM client_profiles cp
            LEFT JOIN brands b ON b.id = cp.brand_id
            WHERE {where_clause}
            ORDER BY cp.name, COALESCE(cp.brand_id, -1), cp.updated_at DESC
        """, params)

        rows = cursor.fetchall()
        conn.close()

        profiles = []
        for row in rows:
            p = _parse_json_fields(_row_to_dict(row))
            bn = p.get("brand_name")
            pn = p.get("name")
            p["name_conflict"] = bool(bn and pn and bn != pn)
            profiles.append(p)
        return profiles
    finally:
        try:
            conn.close()
        except Exception: pass


def find_profile_by_name(name: str, brand_id: int = None) -> Optional[Dict]:
    """按名称查找已有档案（去重用）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if brand_id is not None:
            cursor.execute("""
                SELECT * FROM client_profiles
                WHERE name = %s AND brand_id = %s AND (is_deleted = 0 OR is_deleted IS NULL)
                ORDER BY created_at DESC LIMIT 1
            """, (name, brand_id))
        else:
            cursor.execute("""
                SELECT * FROM client_profiles
                WHERE name = %s AND brand_id IS NULL AND (is_deleted = 0 OR is_deleted IS NULL)
                ORDER BY created_at DESC LIMIT 1
            """, (name,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return _parse_json_fields(_row_to_dict(row))
        return None
    finally:
        try:
            conn.close()
        except Exception: pass


def update_profile(profile_id: str, **kwargs) -> bool:
    """更新档案"""
    # [WO_220-c2] 写之前先自愈列。`get_profile` 一直这么做,这里原来没有 ——
    # 于是「读得到的列」和「写得进的列」在旧库上可以不是同一套。
    _ensure_columns()
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # 白名单：只允许已知的列名，防止 SQL 注入
        allowed_fields = {
            'name', 'industry', 'business', 'products', 'target_users', 'pain_points',
            'competitors', 'persona_positioning', 'persona_tone', 'persona_catchphrases',
            'persona_background', 'persona_golden_quotes', 'target_platforms',
            'brand_constraints', 'company_intro', 'core_value', 'selling_points',
            'success_cases', 'testimonials', 'story_type', 'differentiation',
            'content_direction', 'brand_id', 'brand_display_names', 'visual_style',
            'content_pillars', 'structured_knowledge', 'personality_profile',
            'successful_patterns', 'negative_feedback', 'knowledge_files',
            'status', 'is_deleted',
            'creator_type', 'content_frequency', 'monetization_goal',
            'follower_count', 'profile_completeness', 'preferred_advisor_id', 'preferred_expert_id',
            'admin_insight', 'admin_insight_level',
            'category', 'industry_brief',
            'industry_brief_status', 'industry_brief_started_at',
            # v3.7 知识库审核工作流
            'industry_brief_confirmed', 'industry_brief_confirmed_at',
            'industry_brief_partial_fields', 'industry_brief_edits',
            'industry_brief_version',
            # CTO-15.16 M1c · _ensure_columns 已加列但 update_profile 白名单缺
            # 导致 apply-brief / ai-fill persist 静默丢字段(完整度永远卡 25 的真因之一)
            'service_scope', 'service_scope_reasoning', 'local_competitors',
            'brief_applied_version', 'brief_applied_at',
            'business_type', 'city_scope',
            'social_fields',
            # [2026-06-02 GEO CTO] 客户联系方式 4 字段
            'contact_phone', 'contact_wechat', 'contact_website', 'contact_address',
            # [WO_220-c2 2026-09-16] 写作大厅基础资料表
            # 🔴 `business_summary` 这一列**一直存在**(生产 schema 里有),但从来不在这张
            #    白名单里 ⇒ `update_profile(business_summary=...)` 一直静默返回 False、
            #    列恒为 NULL。实测过:写一段文本,SELECT 回来仍是 None。
            #    「一个从不被写的列」让任何基于它的读数都没有意义。
            'business_summary',
            'basic_info_fields',
        }

        # 过滤掉不在白名单中的字段
        kwargs = {k: v for k, v in kwargs.items() if k in allowed_fields}

        # 处理JSON字段
        json_fields = [
            'products', 'pain_points', 'competitors',
            'persona_catchphrases', 'persona_golden_quotes',
            'target_platforms', 'successful_patterns', 'negative_feedback',
            'knowledge_files',  # 业务知识库文件列表
            'brand_display_names',  # 品牌对外显示名称
            'content_pillars',  # 内容支柱（JSON数组）
            'structured_knowledge',  # 5维度结构化业务知识
            'personality_profile',  # IP全息画像（MBTI + 6维雷达 + 灵魂标签）
            'profile_completeness',  # 渐进建档完成度
            'industry_brief',  # 深度解析结果（L3用户层知识）
            'success_cases',  # 成功案例
            'testimonials',  # 客户评价
            'local_competitors',  # CTO-15.16 M1c · JSONB 本地竞品
            'social_fields',  # 社媒工作台 采访字段
            # [WO_220-c2] 🔴 只进 json_fields,**绝不进下面的 array_fields**:
            #    array_fields 那条分支会把纯字符串「按顿号/逗号拆分成数组」,
            #    而这一列装的正是用户手打的自由文本。products / success_cases
            #    就是栽在那条分支上,所以那两个字段才没复用它们的真列。
            'basic_info_fields',
        ]

        # 应该存为 JSON 数组的字段（纯字符串自动包装）
        array_fields = {'products', 'pain_points', 'competitors',
                        'persona_catchphrases', 'persona_golden_quotes',
                        'target_platforms', 'successful_patterns', 'negative_feedback',
                        'knowledge_files', 'brand_display_names', 'success_cases',
                        'local_competitors'}

        # ═══════════════════════════════════════════════════════════
        # [WO_227-c1 2026-09-16] 拆分规则**按字段配置**,不再一刀切。
        #
        # 🔴 原来所有 `array_fields` 共用一条「按顿号/逗号拆」——
        #    而 `success_cases` 在客户档案页是个 rows=4 的**自由文本框**
        #    (`MarketingTab.tsx:831`,placeholder「成功案例,包含客户名、效果数据...」)。
        #    实测:用户打「某租车公司,3 个月询盘翻倍、成本降 20%」
        #    → 存成 ["某租车公司","3 个月询盘翻倍","成本降 20%"]
        #    → 下次打开显示「某租车公司,3 个月询盘翻倍,成本降 20%」
        #    **顿号被吃掉换成逗号,一个案例被切成三条。**
        #    损坏是一次性的(第二次保存不再继续劣化),但已经改了用户的原话。
        #
        # 🔴 `products` 等**零变化**:它们的数组语义是**有意的**
        #    (客户档案页按数组用 `products`:`match.products || []`),
        #    后端 44 份 / 前端 26 份在读。判据里有反臂钉住这一点。
        #
        # 🔴 不回填历史(Review 裁):已存的数组行不动。
        #    但**「不回填」不等于「呈现不变」** —— 读侧改成按行显示后,
        #    已存的多元素数组会从「一行逗号连排」变成「多行」。
        #    真品牌受影响 **6 行**(227-d1 实测:json_array 9 行,元素 ≥2 的 6 行)。
        # ═══════════════════════════════════════════════════════════
        #: 字段 -> 把纯字符串拆成数组的规则。不在表里的沿用默认(顿号/逗号)。
        split_rules = {
            # 一行一个案例。自由文本里的逗号、顿号都是**句内标点**,不是分隔符。
            'success_cases': 'newline',
        }

        def _normalize_case_list(items):
            """把 list 里的每项 strip、丢掉空项。

            🔴 只对 `split_rules` 里的字段用。前端把 textarea 按换行切开时
               **不滤空**(滤了会吞掉用户正在打的换行),所以送过来的是
               `["A", "", "B", ""]` —— 归一必须在这一侧做,而且是**单点**。
            """
            return [s.strip() for s in items
                    if isinstance(s, str) and s.strip()]

        def _split_plain_text(field_name, text):
            """把纯字符串拆成数组。规则按字段取,取不到用默认。"""
            if split_rules.get(field_name) == 'newline':
                return [s.strip() for s in text.splitlines() if s.strip()]
            # 默认:按顿号/逗号(products 等靠这条,**不许动**)
            return [s.strip() for s in
                    text.replace('，', ',').replace('、', ',').split(',') if s.strip()]

        for field in json_fields:
            if field in kwargs and kwargs[field] is not None:
                val = kwargs[field]
                if isinstance(val, (list, dict)):
                    # [WO_227-c1'] `split_rules` 的字段:list 输入也要 strip + 去空。
                    # 不归一的话,前端送来的 ["A","","B",""] 会原样落库,
                    # 再显示成空行 —— 而前端**不该**滤空(会吞掉正在打的换行)。
                    if isinstance(val, list) and field in split_rules:
                        val = _normalize_case_list(val)
                    kwargs[field] = json.dumps(val, ensure_ascii=False)
                elif isinstance(val, str) and field in array_fields:
                    # 纯字符串 → 尝试 JSON parse，失败则按该字段的规则拆成数组
                    try:
                        parsed = json.loads(val)
                        if isinstance(parsed, list):
                            kwargs[field] = val  # 已经是合法 JSON 数组字符串
                        else:
                            kwargs[field] = json.dumps([val], ensure_ascii=False)
                    except (json.JSONDecodeError, TypeError):
                        items = _split_plain_text(field, val)
                        kwargs[field] = json.dumps(items, ensure_ascii=False)

        # 构建UPDATE语句
        set_clauses = []
        values = []
        for key, value in kwargs.items():
            set_clauses.append(f"{key} = %s")
            values.append(value)

        if not set_clauses:
            return False

        set_clauses.append("updated_at = %s")
        values.append(datetime.now().isoformat())
        values.append(profile_id)

        cursor.execute(f"""
            UPDATE client_profiles
            SET {', '.join(set_clauses)}
            WHERE id = %s
        """, values)

        conn.commit()
        affected = cursor.rowcount

        # 自动反哺：关键字段同步到 brands 表
        if affected > 0:
            _sync_to_brand(cursor, conn, profile_id, kwargs)

        return affected > 0
    finally:
        conn.close()


def _sync_to_brand(cursor, conn, profile_id: str, updated_fields: dict):
    """将 profile 关键字段反哺到 brands 表（name/industry）"""
    try:
        # 只反哺 brand 也有的字段
        brand_sync = {}
        if "name" in updated_fields and updated_fields["name"]:
            brand_sync["name"] = updated_fields["name"]
        if "industry" in updated_fields and updated_fields["industry"]:
            brand_sync["industry"] = updated_fields["industry"]

        if not brand_sync:
            return

        # 查 profile 的 brand_id
        cursor.execute("SELECT brand_id FROM client_profiles WHERE id = %s", (profile_id,))
        row = cursor.fetchone()
        if not row or not row.get("brand_id"):
            return

        brand_id = row["brand_id"]
        sets = [f"{k} = %s" for k in brand_sync]
        vals = list(brand_sync.values()) + [brand_id]
        cursor.execute(f"UPDATE brands SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s", vals)
        conn.commit()
    except Exception:
        pass  # 反哺失败不影响主流程


def soft_delete_profile(profile_id: str) -> bool:
    """软删除档案（移到回收站）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            UPDATE client_profiles
            SET is_deleted = 1, deleted_at = %s
            WHERE id = %s
        """, (datetime.now().isoformat(), profile_id))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
    
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def restore_profile(profile_id: str) -> bool:
    """从回收站恢复档案"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            UPDATE client_profiles
            SET is_deleted = 0, deleted_at = NULL
            WHERE id = %s
        """, (profile_id,))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
    
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


def list_deleted_profiles() -> List[Dict]:
    """获取回收站档案列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("""
            SELECT * FROM client_profiles 
            WHERE is_deleted = 1
            ORDER BY deleted_at DESC
        """)
    
        rows = cursor.fetchall()
        conn.close()
    
        profiles = [_parse_json_fields(_row_to_dict(row)) for row in rows]
        return profiles
    finally:
        try:
            conn.close()
        except Exception: pass


def permanent_delete_profile(profile_id: str) -> bool:
    """永久删除档案"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        cursor.execute("DELETE FROM client_profiles WHERE id = %s", (profile_id,))
    
        conn.commit()
        affected = cursor.rowcount
        conn.close()
    
        return affected > 0
    finally:
        try:
            conn.close()
        except Exception: pass


# ========== 反馈闭环相关 ==========

def add_successful_pattern(profile_id: str, pattern: Dict) -> bool:
    """添加成功套路到档案"""
    profile = get_profile(profile_id)
    if not profile:
        return False
    
    patterns = profile.get('successful_patterns') or []
    if isinstance(patterns, str):
        patterns = json.loads(patterns) if patterns else []
    
    pattern['added_at'] = datetime.now().isoformat()
    patterns.append(pattern)
    
    return update_profile(profile_id, successful_patterns=patterns)


def add_negative_feedback(profile_id: str, feedback: Dict) -> bool:
    """添加失败教训到档案"""
    profile = get_profile(profile_id)
    if not profile:
        return False
    
    feedbacks = profile.get('negative_feedback') or []
    if isinstance(feedbacks, str):
        feedbacks = json.loads(feedbacks) if feedbacks else []
    
    feedback['added_at'] = datetime.now().isoformat()
    feedbacks.append(feedback)
    
    return update_profile(profile_id, negative_feedback=feedbacks)


# ========== 业务知识库相关 ==========

def link_profile_to_brand(profile_id: str, brand_id: int) -> bool:
    """关联档案到诊断系统的品牌"""
    return update_profile(profile_id, brand_id=brand_id)


def get_business_knowledge(profile_id: str) -> Optional[Dict]:
    """
    获取档案关联的业务知识库
    从诊断系统的client_materials表获取
    """
    profile = get_profile(profile_id)
    if not profile:
        return None
    
    brand_id = profile.get('brand_id')
    if not brand_id:
        return None
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 从client_materials表获取业务资料
        cursor.execute("""
            SELECT * FROM client_materials
            WHERE brand_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
        """, (brand_id,))
    
        row = cursor.fetchone()
        conn.close()
    
        if not row:
            # Fallback: 从 structured_knowledge 构建伪 materials
            sk = profile.get('structured_knowledge')
            if sk:
                if isinstance(sk, str):
                    try:
                        sk = json.loads(sk)
                    except (json.JSONDecodeError, TypeError):
                        return None
                if isinstance(sk, dict):
                    cases = sk.get('client_cases', [])
                    case_studies = []
                    for c in cases[:4]:
                        case_studies.append({
                            "client": f"{c.get('client_name', '')}({c.get('age', '')}岁{c.get('occupation', '')})",
                            "industry": c.get("occupation", ""),
                            "result": c.get("result_data", "")[:120],
                            "timeline": c.get("duration", "")
                        })
                    products = sk.get('product_knowledge', {}).get('products', [])
                    selling_points = [{"point": p.get("name", ""), "evidence": p.get("efficacy", "")[:80]} for p in products[:4]]
                    return {
                        "company_intro": sk.get("store_operations", {}).get("stores", [{}])[0].get("name", "") if sk.get("store_operations") else None,
                        "core_selling_points": selling_points if selling_points else [],
                        "unique_value": sk.get("product_knowledge", {}).get("brand_story", ""),
                        "methodology": None,
                        "case_studies": case_studies,
                        "testimonials": [],
                        "credentials": [],
                        "_source": "structured_knowledge_fallback"
                    }
            return None

        # 解析JSON字段
        result = dict(row)
        json_fields = ['core_selling_points', 'case_studies', 'pricing_tiers', 'testimonials', 'credentials']
        for field in json_fields:
            if result.get(field):
                try:
                    result[field] = json.loads(result[field])
                except (json.JSONDecodeError, TypeError):
                    result[field] = []

        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_brand_diagnosis_data(profile_id: str) -> Optional[Dict]:
    """
    获取档案关联品牌的诊断数据
    包含raw_data_json和最新诊断得分
    """
    profile = get_profile(profile_id)
    if not profile:
        return None
    
    brand_id = profile.get('brand_id')
    if not brand_id:
        return None
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
    
        # 获取最新诊断记录 [返工2 P1-2] published-only:排 withheld(退款/失败)+ pending(生成中)·
        #   否则 AI 内容生成会用退款/未完成诊断的分数/raw_data 作上下文
        cursor.execute("""
            SELECT id, brand_name, industry, total_score, level,
                   keywords, raw_data_json, created_at
            FROM diagnosis_records
            WHERE brand_id = %s
              AND (result_visibility IS NULL OR result_visibility = 'published')
            ORDER BY created_at DESC
            LIMIT 1
        """, (brand_id,))
    
        row = cursor.fetchone()
        conn.close()
    
        if not row:
            return None
    
        result = dict(row)
        # 解析raw_data_json
        if result.get('raw_data_json'):
            try:
                result['raw_data'] = json.loads(result['raw_data_json'])
            except:
                result['raw_data'] = {}
    
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


def get_full_business_context(profile_id: str) -> Dict:
    """
    获取完整的业务上下文
    整合档案信息 + 业务资料 + 诊断数据
    """
    profile = get_profile(profile_id) or {}
    business_knowledge = get_business_knowledge(profile_id) or {}
    diagnosis_data = get_brand_diagnosis_data(profile_id) or {}
    
    return {
        'profile': profile,
        'business_knowledge': business_knowledge,
        'diagnosis_data': diagnosis_data,
    }


# ========== 业务知识库文件管理 ==========

def add_knowledge_file(profile_id: str, file_info: Dict) -> bool:
    """
    添加知识库文件记录到档案
    
    Args:
        profile_id: 档案ID
        file_info: 文件信息 {id, name, path, size, created_at}
    
    Returns:
        bool: 是否成功
    """
    profile = get_profile(profile_id)
    if not profile:
        return False
    
    files = profile.get('knowledge_files') or []
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except:
            files = []
    
    # 确保有必要字段
    if 'id' not in file_info:
        file_info['id'] = str(uuid.uuid4())[:8]
    if 'created_at' not in file_info:
        file_info['created_at'] = datetime.now().isoformat()
    
    files.append(file_info)
    return update_profile(profile_id, knowledge_files=files)


def remove_knowledge_file(profile_id: str, file_id: str) -> bool:
    """
    从档案中删除知识库文件记录
    
    Args:
        profile_id: 档案ID
        file_id: 文件ID
    
    Returns:
        bool: 是否成功
    """
    profile = get_profile(profile_id)
    if not profile:
        return False
    
    files = profile.get('knowledge_files') or []
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except:
            files = []
    
    # 过滤掉要删除的文件
    new_files = [f for f in files if f.get('id') != file_id]
    
    if len(new_files) == len(files):
        return False  # 没有找到要删除的文件
    
    return update_profile(profile_id, knowledge_files=new_files)


def list_knowledge_files(profile_id: str) -> List[Dict]:
    """
    获取档案的知识库文件列表
    
    Args:
        profile_id: 档案ID
    
    Returns:
        List[Dict]: 文件列表
    """
    profile = get_profile(profile_id)
    if not profile:
        return []
    
    files = profile.get('knowledge_files') or []
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except:
            files = []
    
    return files


def get_knowledge_file(profile_id: str, file_id: str) -> Optional[Dict]:
    """
    获取单个知识库文件信息
    
    Args:
        profile_id: 档案ID
        file_id: 文件ID
    
    Returns:
        Dict: 文件信息，如果不存在返回None
    """
    files = list_knowledge_files(profile_id)
    for f in files:
        if f.get('id') == file_id:
            return f
    return None


if __name__ == "__main__":
    # 测试
    print("测试客户档案CRUD...")
    
    # 创建测试档案
    test_id = create_profile(
        name="测试档案",
        industry="教育培训",
        business="AI教学工具",
        target_users="B端培训机构",
        persona_positioning="10年编导老兵",
        persona_tone="幽默接地气",
        target_platforms=["douyin", "xiaohongshu"]
    )
    print(f"✅ 创建档案: {test_id}")
    
    # 获取档案
    profile = get_profile(test_id)
    print(f"✅ 获取档案: {profile['name']}")
    
    # 更新档案
    update_profile(test_id, business="AI短视频工具")
    profile = get_profile(test_id)
    print(f"✅ 更新档案: business = {profile['business']}")
    
    # 列表
    profiles = list_profiles()
    print(f"✅ 档案列表: 共 {len(profiles)} 个")
    
    # 软删除
    soft_delete_profile(test_id)
    deleted = list_deleted_profiles()
    print(f"✅ 回收站: 共 {len(deleted)} 个")
    
    # 恢复
    restore_profile(test_id)
    profiles = list_profiles()
    print(f"✅ 恢复后: 共 {len(profiles)} 个活跃档案")
    
    # 永久删除测试数据
    permanent_delete_profile(test_id)
    print(f"✅ 已永久删除测试档案")
