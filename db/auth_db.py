"""
RBAC 权限系统 - 数据库操作模块
管理用户、角色、权限、客户映射和审计日志

所有表建在 geo_diagnosis.db（主库）中
"""

import json
import hashlib
import logging

logger = logging.getLogger("GEO-Auth")
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path

# 尝试导入 bcrypt，不可用时降级为 hashlib
try:
    import bcrypt
    USE_BCRYPT = True
except ImportError:
    USE_BCRYPT = False

# 数据库路径（与其他 db 模块一致）
DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "geo_diagnosis.db"


def get_connection():
    """获取数据库连接"""
    from db.connection import get_connection as _pg_get_connection
    return _pg_get_connection()


def _row_to_dict(row) -> Optional[Dict]:
    """将 Row 对象转换为字典"""
    if row is None:
        return None
    return dict(row)


def _rows_to_list(rows) -> List[Dict]:
    """将 Row 列表转换为字典列表"""
    return [dict(row) for row in rows]


# ========================================
# 密码哈希
# ========================================

def hash_password(password: str) -> str:
    """对密码进行哈希"""
    if USE_BCRYPT:
        return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    else:
        # 降级：SHA256 + 随机盐
        salt = os.urandom(16).hex()
        hashed = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
        return f"sha256:{salt}:{hashed}"


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码"""
    if USE_BCRYPT and password_hash.startswith("$2"):
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    elif password_hash.startswith("sha256:"):
        parts = password_hash.split(":")
        if len(parts) != 3:
            return False
        _, salt, stored_hash = parts
        import hmac
        computed = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
        return hmac.compare_digest(computed, stored_hash)
    return False


# ========================================
# 初始化
# ========================================

# 预置的模块权限列表（与 module_mapping.py 对应）
ALL_MODULES = [
    "dashboard", "brands", "diagnosis", "quote", "writing",
    "social", "monitoring", "insights", "reports", "placement", "publish",
    "ai_agents", "history",
    "advisors", "settings", "users"
]

ALL_LEVELS = ["read", "write", "delete", "admin"]

# 预置角色模板
ROLE_TEMPLATES = {
    "admin": {
        "display_name": "超级管理员",
        "description": "拥有所有权限，可管理用户和系统设置",
        "is_system": 1,
        "permissions": []  # admin 角色通过 is_admin 字段跳过权限检查
    },
    "geo_writer": {
        "display_name": "GEO编辑（未启用）",
        "description": "GEO编辑角色，当前保留为存量角色",
        "is_system": 0,
        "permissions": [
            ("dashboard", "read"),
            ("diagnosis", "read"), ("diagnosis", "write"),
            ("history", "read"),
            ("insights", "read"), ("insights", "write"),
            ("monitoring", "read"), ("monitoring", "write"),
            ("reports", "read"), ("reports", "write"),
            ("social", "read"), ("social", "write"),
            ("writing", "read"), ("writing", "write"),
        ]
    },
    "social_ops": {
        # 历史内部名保留不改：prod 中该角色实际承载普通用户。
        "display_name": "普通用户",
        "description": "普通用户角色，保留 social_ops 内部名以兼容历史引用",
        "is_system": 0,
        "permissions": [
            ("diagnosis", "read"), ("diagnosis", "write"),
            ("history", "read"),
            ("insights", "read"),
            ("monitoring", "read"), ("monitoring", "write"),
            ("placement", "read"),
            ("publish", "read"), ("publish", "write"),
            ("quote", "read"), ("quote", "write"),
            ("reports", "read"), ("reports", "write"),
            ("social", "read"), ("social", "write"),
            ("writing", "read"), ("writing", "write"),
        ]
    },
    "sales": {
        "display_name": "销售顾问（未启用）",
        "description": "销售顾问角色，当前未启用",
        "is_system": 0,
        "permissions": [
            ("diagnosis", "read"), ("diagnosis", "write"),
            ("history", "read"),
            ("monitoring", "read"),
            ("quote", "read"), ("quote", "write"),
            ("reports", "read"),
            ("social", "read"), ("social", "write"),
        ]
    },
    "geo_user_basic": {
        "display_name": "GEO 用户",
        "description": "GEO 用户存量角色；当前不通过 role_permissions 授权",
        "is_system": 0,
        "permissions": [],
    },
    "geo_agent_full": {
        "display_name": "GEO 代理",
        "description": "GEO 代理存量角色；当前代理能力由 agent_level 等业务字段控制",
        "is_system": 0,
        "permissions": [],
    },
    "finance_reviewer": {
        "display_name": "财务审核员",
        "description": "财务审核员存量角色；当前未配置 role_permissions",
        "is_system": 0,
        "permissions": [],
    },
}


def init_auth_db():
    """初始化 RBAC 相关的所有表 + 预置数据"""
    conn = get_connection()
    try:
        conn.autocommit = True
        cursor = conn.cursor()

        # ① 角色表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            description TEXT,
            is_system INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # ② 角色权限表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS role_permissions (
            id SERIAL PRIMARY KEY,
            role_id INTEGER NOT NULL,
            module TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'read',
            FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
            UNIQUE(role_id, module, level)
        )
        """)

        # ③ 用户表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            permission_version INTEGER DEFAULT 1,
            must_change_password INTEGER DEFAULT 1,
            avatar_url TEXT,
            phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP
        )
        """)
        # 兼容旧库：补 phone 列（短信登录依赖）
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN NOT NULL DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_for TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL")
        # 在线看板依赖：last_active_at + current_path
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_path TEXT")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active_at) WHERE last_active_at IS NOT NULL")
        # 代理私有报价偏好：只影响代理生成报价，不暴露给客户侧
        # [§4.5/决策5 2026-06-06] 默认报价系数 2.0→1.0(回成本·operator 自控·被邀请客户继承上级系数)
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quote_markup_ratio NUMERIC(4,2) DEFAULT 1.0")
        cursor.execute("ALTER TABLE users ALTER COLUMN quote_markup_ratio SET DEFAULT 1.0")  # 存量库列默认改 1.0
        # [M2 2026-06-07] 服务商自设单篇内容成本(A 完全覆盖):
        #   NULL=走系统动态成本(竞品来源加权 35-350·默认兜底¥60)/ 非NULL=服务商自设值·完全覆盖系统动态。
        #   投央媒服务商可设真实 ¥350 → cost_floor=篇数×350×markup → 报价≥成本(闭合 M1 防亏)。DEFAULT NULL 存量零影响。
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cost_per_article NUMERIC(6,2) DEFAULT NULL")
        # 一次性回填:旧默认 2.0 → 1.0(marker 守卫·只跑一次·防重启重置 operator 后续自设的 2.0)
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS _migration_markers (
                    marker TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, note TEXT
                )
            """)
            cursor.execute("SELECT 1 FROM _migration_markers WHERE marker = %s", ('quote_markup_default_1_0_2026_06_06',))
            if not cursor.fetchone():
                cursor.execute("UPDATE users SET quote_markup_ratio = 1.0 WHERE quote_markup_ratio = 2.0")
                cursor.execute(
                    "INSERT INTO _migration_markers (marker, note) VALUES (%s, %s)",
                    ('quote_markup_default_1_0_2026_06_06',
                     '默认报价系数 2.0→1.0(决策5 回成本·operator 自控)·存量旧默认 2.0 一次性回 1.0 2026-06-06')
                )
        except Exception:
            pass  # marker/backfill 失败不阻塞 init(列默认改已生效)

        # ④ 用户-角色映射（多对多）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, role_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
        )
        """)

        # ⑤ 用户-客户映射（数据隔离）
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_clients (
            user_id INTEGER NOT NULL,
            brand_id INTEGER NOT NULL,
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, brand_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)

        # ⑥ 审计日志表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            action TEXT NOT NULL,
            module TEXT,
            entity_type TEXT,
            entity_id INTEGER,
            summary TEXT,
            before_snapshot TEXT,
            after_snapshot TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 创建审计日志索引（加速查询）
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id)
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action)
        """)

        # [WP2 · 商业治理] request_id / reason 升一等列(对齐 admin_user_governance_audits)。
        # audit_logs 为跨模块共享表(30+ 写入方),新列 NULLABLE + 幂等 ADD,既有写入零影响;
        # 定价审计(services.agent_inventory_pricing.insert_pricing_audit)从此把 request_id
        # 与变更原因写入一等列,而不仅埋在 before/after JSON 里,便于按列检索/审计。
        cursor.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS request_id TEXT")
        cursor.execute("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS reason TEXT")
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_logs_request_id
            ON audit_logs(request_id) WHERE request_id IS NOT NULL
        """)

        # 预置角色和 admin 用户（幂等）
        _seed_roles(conn)
        _seed_admin_user(conn)

        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def _row_get(row, key: str, index: int = 0):
    if isinstance(row, dict):
        return row[key]
    return row[index]


def _seed_roles(conn):
    """预置角色模板（幂等，已存在角色也补齐新权限）"""
    cursor = conn.cursor()

    for role_name, template in ROLE_TEMPLATES.items():
        # 检查是否已存在
        cursor.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
        existing = cursor.fetchone()
        if existing:
            role_id = _row_get(existing, "id", 0)
            cursor.execute("""
                UPDATE roles
                SET display_name = %s, description = %s, is_system = %s
                WHERE id = %s
            """, (template["display_name"], template["description"], template["is_system"], role_id))
        else:
            # 创建角色
            cursor.execute("""
                INSERT INTO roles (name, display_name, description, is_system)
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (role_name, template["display_name"], template["description"], template["is_system"]))
            role_id = _row_get(cursor.fetchone(), "id", 0)

        # 创建/补齐权限
        for module, level in template["permissions"]:
            cursor.execute("""
                INSERT INTO role_permissions (role_id, module, level)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """, (role_id, module, level))

    conn.commit()


#: 初始管理员口令的环境变量;不设 ⇒ 随机生成(WO_327)
ADMIN_INITIAL_PASSWORD_ENV = "ADMIN_INITIAL_PASSWORD"
ADMIN_INITIAL_PASSWORD_MIN_LEN = 12


def _initial_admin_password() -> Tuple[str, bool]:
    """(口令, 是否随机生成)。

    [WO_327 · Review 10-02] 原先写死一组固定的默认口令,而「首登强制改密」只在前端 —— 后端只透传
    must_change_password、不拦请求 ⇒ 开源后任何人拿这组口令直接调登录接口就是管理员。
    现在:读 ADMIN_INITIAL_PASSWORD(至少 12 位,否则直接报错让冷启动失败,不静默换一个);
    没设 ⇒ secrets 随机 24 位。只在空库第一次建 admin 时走到这里(生产库早有 admin,永远不进)。
    """
    pw = os.environ.get("ADMIN_INITIAL_PASSWORD", "")
    if pw:
        if len(pw) < ADMIN_INITIAL_PASSWORD_MIN_LEN:
            raise RuntimeError(
                f"{ADMIN_INITIAL_PASSWORD_ENV} 太短(至少 {ADMIN_INITIAL_PASSWORD_MIN_LEN} 位)。"
                f"改长,或删掉这个变量让系统随机生成初始口令。")
        return pw, False
    import secrets
    return secrets.token_urlsafe(18), True


def _seed_admin_user(conn):
    """预置 admin 用户（幂等，已存在则跳过）"""
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM users WHERE username = 'admin'")
    if cursor.fetchone():
        return

    # 创建 admin 用户:口令取 ADMIN_INITIAL_PASSWORD,没设就随机生成并只在这里打印一次(首登强制修改)
    initial_password, generated = _initial_admin_password()
    admin_hash = hash_password(initial_password)
    cursor.execute("""
        INSERT INTO users (username, password_hash, display_name, must_change_password)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, ("admin", admin_hash, "管理员", 1))
    admin_user_id = cursor.fetchone()["id"]

    # 绑定 admin 角色
    cursor.execute("SELECT id FROM roles WHERE name = 'admin'")
    admin_role = cursor.fetchone()
    if admin_role:
        cursor.execute("""
            INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s) ON CONFLICT DO NOTHING
        """, (admin_user_id, admin_role["id"]))

    conn.commit()
    # 落库之后再打印:打出来的口令一定对应已存在的账号。admin 已存在时函数开头就返回了 ⇒ 每个库只会打这一次。
    if generated:
        logger.warning(
            f"[初始管理员] 已创建用户 admin,随机初始口令:{initial_password} —— 只打印这一次,请记下;"
            f"首次登录会要求立即修改。想自己指定,下次空库初始化前设环境变量 {ADMIN_INITIAL_PASSWORD_ENV}。")
    else:
        logger.info(f"[初始管理员] 已创建用户 admin,口令取自环境变量 {ADMIN_INITIAL_PASSWORD_ENV}(不打印);首次登录会要求立即修改。")


# ========================================
# 用户 CRUD
# ========================================

def create_user(username: str, password: str, display_name: str,
                role_ids: List[int] = None, client_brand_ids: List[int] = None,
                must_change_password: int = 1,
                agreement_acceptances: Optional[List[Dict[str, Any]]] = None,
                agreement_ip: Optional[str] = None,
                agreement_ua: Optional[str] = None) -> Optional[int]:
    """创建用户，返回用户ID"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        password_hash = hash_password(password)
        cursor.execute("""
            INSERT INTO users (username, password_hash, display_name, must_change_password)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (username, password_hash, display_name, must_change_password))
        user_id = cursor.fetchone()["id"]

        # 绑定角色
        if role_ids:
            for role_id in role_ids:
                cursor.execute("""
                    INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s) ON CONFLICT DO NOTHING
                """, (user_id, role_id))

        # 绑定客户
        if client_brand_ids:
            for brand_id in client_brand_ids:
                cursor.execute("""
                    INSERT INTO user_clients (user_id, brand_id) VALUES (%s, %s) ON CONFLICT DO NOTHING
                """, (user_id, brand_id))

        # Account creation and mandatory legal evidence are one transaction.
        # Missing schema or evidence fails closed and rolls the user row back.
        for acceptance in agreement_acceptances or []:
            cursor.execute("""
                INSERT INTO agreement_signatures
                    (user_id,agreement_type,agreement_version,content_hash,
                     ip_address,user_agent,evidence_jsonb)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (user_id,agreement_type,agreement_version) DO NOTHING
            """, (
                user_id, acceptance["agreement_type"], acceptance["agreement_version"],
                acceptance["content_hash"], agreement_ip, agreement_ua,
                json.dumps({"surface": "registration", "explicit_acceptance": True}),
            ))

        # 🔴 [#141 · 2026-09-07] 钱包行进**同一个事务**。
        #
        #    生产实证(Deploy 只读取证):用户 #174 是 09-05 自助注册,
        #    有 user_settings/user_roles 却**没有钱包行** —— 它断在
        #    `api/auth_api.py:549` 那个 `try` 里(`except` 只打 warning 然后继续),
        #    然后 `admin_user_governance` 的全表守卫拿它把**整个管理端用户列表**
        #    拒读了两天。全系统恰好这一个人。
        #
        #    🔴 修法不是「失败就 fail-loud」,是**让失败不可能被吞**:
        #    放进 users 行同一个事务 ⇒ 要么两者都有,要么两者都没有,
        #    中间那个「有用户没钱包」的状态**根本不存在**。
        #    fail-loud 仍然留着一个窗口(建号成功、补钱包失败、然后指望有人看日志);
        #    能消灭的轴就别去诊断它。
        #
        #    这里写裸 SQL 而不是调 `db.wallet_db.get_or_create_wallet`:
        #    后者自开连接,放进来就不在本事务里了(等于什么都没改),
        #    而且 `db/wallet_db.py` 是保护文件、本笔不碰。
        #    列默认值:agent_level 0(普通用户)、余额 0 —— 与 `get_or_create_wallet` 同。
        #    `api/auth_api.py:549` 的调用保留:幂等,且它还要发体验包积分。
        cursor.execute(
            "INSERT INTO user_wallets(user_id) VALUES (%s) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        conn.commit()
        return user_id
    except Exception as e:
        conn.rollback()
        # 区分唯一约束冲突（用户名重复）和其他错误
        import psycopg2.errors
        if isinstance(e, psycopg2.errors.UniqueViolation):
            return None  # 用户名重复
        logger.error(f"create_user 失败: {e}")
        raise
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[Dict]:
    """获取用户详情（含角色和权限）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = _row_to_dict(cursor.fetchone())
        if not user:
            conn.close()
            return None

        # 移除敏感字段
        user.pop("password_hash", None)

        # 查询角色
        cursor.execute("""
            SELECT r.id, r.name, r.display_name, r.is_system
            FROM roles r
            JOIN user_roles ur ON r.id = ur.role_id
            WHERE ur.user_id = %s
        """, (user_id,))
        user["roles"] = _rows_to_list(cursor.fetchall())

        # 查询权限（所有角色的并集）
        user["permissions"] = get_user_permissions(user_id, conn=conn)

        # 查询分配的客户 + 用户自己拥有的品牌（owner_user_id）
        cursor.execute("""
            SELECT brand_id FROM user_clients WHERE user_id = %s
            UNION
            SELECT id AS brand_id FROM brands WHERE owner_user_id = %s
        """, (user_id, user_id))
        user["client_brand_ids"] = [row["brand_id"] for row in cursor.fetchall()]

        # 判断是否是 admin
        user["is_admin"] = any(r["name"] == "admin" for r in user["roles"])

        conn.close()
        return user
    finally:
        try:
            conn.close()
        except Exception: pass


def get_user_by_username(username: str) -> Optional[Dict]:
    """根据用户名获取用户（含 password_hash，用于登录验证）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = _row_to_dict(cursor.fetchone())
        conn.close()
        return user
    finally:
        try:
            conn.close()
        except Exception: pass


def get_user_by_phone(phone: str) -> Optional[Dict]:
    """通过手机号查找用户"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE phone = %s AND is_active = 1", (phone,))
        user = _row_to_dict(cursor.fetchone())
        conn.close()
        return user
    finally:
        try:
            conn.close()
        except Exception: pass


def list_users(include_inactive: bool = False) -> List[Dict]:
    """获取所有用户列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if include_inactive:
            cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
        else:
            cursor.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY created_at DESC")

        users = _rows_to_list(cursor.fetchall())

        # 为每个用户附加角色信息
        for user in users:
            user.pop("password_hash", None)
            cursor.execute("""
                SELECT r.id, r.name, r.display_name
                FROM roles r JOIN user_roles ur ON r.id = ur.role_id
                WHERE ur.user_id = %s
            """, (user["id"],))
            user["roles"] = _rows_to_list(cursor.fetchall())
            user["is_admin"] = any(r["name"] == "admin" for r in user["roles"])

            # 分配的客户 + 用户拥有的品牌
            cursor.execute("""
                SELECT brand_id FROM user_clients WHERE user_id = %s
                UNION
                SELECT id AS brand_id FROM brands WHERE owner_user_id = %s
            """, (user["id"], user["id"]))
            client_rows = cursor.fetchall()
            user["client_brand_ids"] = [row["brand_id"] for row in client_rows]
            user["client_count"] = len(user["client_brand_ids"])

        conn.close()
        return users
    finally:
        try:
            conn.close()
        except Exception: pass


def update_user(user_id: int, **kwargs) -> bool:
    """更新用户信息（display_name, is_active, avatar_url 等）"""
    allowed_fields = {
        "display_name", "is_active", "avatar_url", "must_change_password",
        "extension_authorized", "extension_authorized_at", "extension_authorized_by",
        "phone", "phone_verified", "real_name", "email", "email_verified", "email_verified_for", "wechat_id",
        "company", "job_title", "industry", "city", "gender", "bio",
        "profile_completed_at", "quote_markup_ratio",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates:
        return False

    conn = get_connection()
    try:
        cursor = conn.cursor()
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [user_id]
        cursor.execute(f"UPDATE users SET {set_clause} WHERE id = %s", values)
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        return success
    finally:
        try:
            conn.close()
        except Exception: pass


def update_user_password(user_id: int, new_password: str) -> bool:
    """更新用户密码"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        password_hash = hash_password(new_password)
        cursor.execute("""
            UPDATE users SET password_hash = %s, must_change_password = 0,
                             permission_version = permission_version + 1
            WHERE id = %s
            RETURNING permission_version
        """, (password_hash, user_id))
        changed = cursor.fetchone()
        if changed:
            from services.notification_events import NotificationEventType, RecipientKind
            from services.notification_outbox import enqueue_notification_event

            permission_version = int(changed["permission_version"])
            enqueue_notification_event(
                cursor,
                event_type=NotificationEventType.PASSWORD_CHANGED,
                business_id=f"user:{int(user_id)}:password:{permission_version}",
                terminal_state="changed",
                recipient_user_id=int(user_id),
                recipient_kind=RecipientKind.USER,
                facts={
                    "business_no": f"SECURITY-{permission_version}",
                    "status": "密码已修改",
                    "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "summary": "如果不是您本人操作，请立即联系平台处理。",
                },
            )
        conn.commit()
        success = changed is not None
        conn.close()
        return success
    finally:
        try:
            conn.close()
        except Exception: pass


def deactivate_user(user_id: int) -> bool:
    """禁用用户（同时递增 permission_version 使 JWT 立即失效）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users SET is_active = 0, permission_version = permission_version + 1
            WHERE id = %s
        """, (user_id,))
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        return success
    finally:
        try:
            conn.close()
        except Exception: pass


def update_last_login(user_id: int):
    """更新最后登录时间"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users SET last_login_at = %s WHERE id = %s
        """, (datetime.now().isoformat(), user_id))
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


# ========================================
# 用户-角色映射
# ========================================

def set_user_roles(user_id: int, role_ids: List[int]) -> bool:
    """设置用户的角色（替换模式：先删后建）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
        for role_id in role_ids:
            cursor.execute("""
                INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)
            """, (user_id, role_id))
        # 递增 permission_version，触发 JWT 失效
        cursor.execute("""
            UPDATE users SET permission_version = permission_version + 1 WHERE id = %s
        """, (user_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


# ========================================
# 用户-客户映射（数据隔离）
# ========================================

def set_user_clients(user_id: int, brand_ids: List[int]) -> bool:
    """设置用户的客户（替换模式）"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM user_clients WHERE user_id = %s", (user_id,))
        for brand_id in brand_ids:
            cursor.execute("""
                INSERT INTO user_clients (user_id, brand_id) VALUES (%s, %s)
            """, (user_id, brand_id))
        # 递增 permission_version，触发 JWT 刷新（client_brand_ids 嵌入在 JWT 中）
        cursor.execute("""
            UPDATE users SET permission_version = permission_version + 1 WHERE id = %s
        """, (user_id,))
        conn.commit()
        return True
    except Exception as e:
        import logging
        logging.getLogger("GEO-Server").error(f"[set_user_clients] user_id={user_id}, brand_ids={brand_ids}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def auto_assign_brand_to_user(user_id: int, brand_id: int) -> bool:
    """
    自动分配品牌给用户（谁创建谁拥有，先到先得）

    规则：
    - 如果品牌没有被任何非管理员用户拥有 → 分配给当前用户
    - 如果品牌已被某用户拥有 → 不分配（避免客户重叠）
    - 如果用户已拥有该品牌 → 跳过
    - 管理员不需要分配（默认可见全部）

    Returns:
        True=已分配, False=品牌已有归属或已拥有
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # 检查用户是否是管理员（管理员不需要分配）
        cursor.execute("""
            SELECT r.name FROM roles r
            JOIN user_roles ur ON r.id = ur.role_id
            WHERE ur.user_id = %s
        """, (user_id,))
        roles = [row["name"] for row in cursor.fetchall()]
        if "admin" in roles:
            return False  # 管理员默认可见全部，无需分配

        # 原子操作：仅当品牌无归属时插入，避免 TOCTOU 竞态
        cursor.execute("""
            INSERT INTO user_clients (user_id, brand_id)
            SELECT %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM user_clients uc
                JOIN users u ON uc.user_id = u.id
                WHERE uc.brand_id = %s AND u.is_active = 1
            )
            ON CONFLICT DO NOTHING
        """, (user_id, brand_id, brand_id))

        if cursor.rowcount == 0:
            return False  # 品牌已有归属或用户已拥有

        # 递增 permission_version 使 JWT 刷新
        # 注意: 配合 auth/middleware.py 的 soft-refresh 机制,这里 bump 不会
        # 强制踢用户下线,而是让下次请求时 middleware 从 DB 拉取新的
        # client_brand_ids 注入 request.state.user,token 本身继续有效。
        cursor.execute(
            "UPDATE users SET permission_version = permission_version + 1 WHERE id = %s",
            (user_id,)
        )
        conn.commit()

        logger.info(f"[RBAC] 品牌 #{brand_id} 自动分配给用户 #{user_id}（创建者先到先得）")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"[RBAC] 自动分配品牌失败: {e}")
        return False
    finally:
        conn.close()


def get_user_client_ids(user_id: int) -> List[int]:
    """获取用户分配的客户 brand_id 列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT brand_id FROM user_clients WHERE user_id = %s", (user_id,))
        result = [row["brand_id"] for row in cursor.fetchall()]
        conn.close()
        return result
    finally:
        try:
            conn.close()
        except Exception: pass


# ========================================
# 权限查询
# ========================================

def get_user_permissions(user_id: int, conn=None) -> List[str]:
    """获取用户所有角色的权限并集，返回 ['module:level', ...] 格式

    [单1 · WP6 集成缺口] 组织操作员(员工席位)在 `user_roles` 里**一行都没有**,
    授权真相在组织能力表里。这里把组织能力**推导**成同样的 `module:level` 串并入
    结果集,使员工在前端路由(`hasModule`)和后端中间件(`auth/middleware.py` 的
    模块校验)两侧都能通过 —— 二者读的都是这一份权限串。

    为什么推导落在这里而不是只在 `/api/auth/me`:`/me` 只喂前端路由;后端每个
    受保护 API 的权限来自 JWT 或中间件的 DB 软刷新,两条路径都回到本函数。只改
    `/me` 会得到"页面能进、每个接口照样 403"的半截修复。
    (`auth/middleware.py` / `auth/jwt_utils.py` 仍然零改动,红线不动。)

    推导是**只读、fail-closed** 的:任何异常都退化为原有权限,只会少给不会多给。
    不写 `user_roles` —— 授权真相仍然只有组织能力表一份。
    """
    should_close = conn is None
    if conn is None:
        conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT DISTINCT rp.module, rp.level
        FROM role_permissions rp
        JOIN user_roles ur ON rp.role_id = ur.role_id
        WHERE ur.user_id = %s
    """, (user_id,))

    permissions = [f"{row['module']}:{row['level']}" for row in cursor.fetchall()]

    # 🔴 共享主人翁修复(2026-08-21 · 防御型 GEO 窗A 第四班):把推导段圈进 SAVEPOINT。
    #
    # 事故形态 —— `derive_for_user` 内部有一句 `except Exception: return frozenset()`,
    # 把组织表查询的失败**吞掉**了。它的返回值看上去完全正常(空集 = fail-closed,
    # 少给权限),但 PostgreSQL 那一侧**调用方的事务已经废了**。异常要等到下一条
    # 语句才炸,而且炸在毫不相干的地方(`get_user` 的 `SELECT ... FROM user_clients`,
    # 见本文件 L482),真凶在几十行之外 —— 这正是本仓记过的
    # 「try/except 包 SQL 无 SAVEPOINT = 打废调用方事务」。
    #
    # 为什么修在这里而不是修 `derive_for_user`:吞异常是它**故意**的 fail-closed
    # 设计(登录/鉴权主链绝不能因组织体系异常而崩),不该改。既然它不往上抛,
    # 调用方就无从判断要不要清理 —— 所以清理只能是**无条件**的,并且只能由持有
    # 事务的一方(本函数)做。这就是 SAVEPOINT 的用途。
    #
    # 为什么无条件 ROLLBACK TO 是安全的:推导是**只读**的(`resolve_identity` 只
    # SELECT,`derive_module_permissions` 是纯函数),回滚一个只读片段不丢任何东西。
    # 反过来,如果将来有人让推导写库,这一行会静默吞掉那些写 —— 所以配了一条
    # 结构判据钉住「推导链只读」,不靠这段注释被人读到。
    #
    # autocommit 连接不进这条路:那时每条语句自成事务,失败不会污染下一条,而
    # `SAVEPOINT` 在事务块外是非法的,硬发反而会把好路径打红。
    _derive_in_txn = not bool(getattr(conn, "autocommit", False))
    try:
        from auth.organization_module_derivation import derive_for_user

        if _derive_in_txn:
            cursor.execute("SAVEPOINT org_module_derivation")
        try:
            derived = derive_for_user(user_id, cursor=cursor)
        finally:
            if _derive_in_txn:
                # 先 ROLLBACK TO 再 RELEASE:推导失败时事务处于 INERROR 状态,
                # 此刻**只有** ROLLBACK TO 是被允许的语句,直接 RELEASE 会再抛一次。
                cursor.execute("ROLLBACK TO SAVEPOINT org_module_derivation")
                cursor.execute("RELEASE SAVEPOINT org_module_derivation")
        if derived:
            permissions = sorted(set(permissions) | set(derived))
    except Exception as _derive_error:  # pragma: no cover - 主链防崩
        logger.warning(f"[permissions] 组织能力推导失败(按原权限继续): {_derive_error}")

    if should_close:
        conn.close()
    return permissions


def get_user_permission_version(user_id: int) -> int:
    """获取用户的 permission_version（用于 JWT 校验）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT permission_version FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row["permission_version"] if row else -1
    finally:
        try:
            conn.close()
        except Exception: pass


# ========================================
# 角色 CRUD
# ========================================

def create_role(name: str, display_name: str, description: str = "",
                permissions: List[Tuple[str, str]] = None) -> Optional[int]:
    """创建角色，permissions 格式: [('writing', 'read'), ('writing', 'write'), ...]"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO roles (name, display_name, description, is_system)
            VALUES (%s, %s, %s, 0) RETURNING id
        """, (name, display_name, description))
        role_id = cursor.fetchone()["id"]

        if permissions:
            for module, level in permissions:
                cursor.execute("""
                    INSERT INTO role_permissions (role_id, module, level)
                    VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """, (role_id, module, level))

        conn.commit()
        return role_id
    except Exception:
        return None  # 角色名重复
    finally:
        conn.close()


def get_role(role_id: int) -> Optional[Dict]:
    """获取角色详情（含权限列表）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM roles WHERE id = %s", (role_id,))
        role = _row_to_dict(cursor.fetchone())
        if not role:
            conn.close()
            return None

        # 查询该角色的权限
        cursor.execute("""
            SELECT module, level FROM role_permissions WHERE role_id = %s
        """, (role_id,))
        role["permissions"] = _rows_to_list(cursor.fetchall())
        role["permission_strings"] = [f"{p['module']}:{p['level']}" for p in role["permissions"]]

        # 查询使用该角色的用户数
        cursor.execute("SELECT COUNT(*) as cnt FROM user_roles WHERE role_id = %s", (role_id,))
        role["user_count"] = cursor.fetchone()["cnt"]

        conn.close()
        return role
    finally:
        try:
            conn.close()
        except Exception: pass


def list_roles() -> List[Dict]:
    """获取所有角色列表"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM roles ORDER BY is_system DESC, created_at ASC")
        roles = _rows_to_list(cursor.fetchall())

        for role in roles:
            cursor.execute("""
                SELECT module, level FROM role_permissions WHERE role_id = %s
            """, (role["id"],))
            role["permissions"] = _rows_to_list(cursor.fetchall())
            role["permission_strings"] = [f"{p['module']}:{p['level']}" for p in role["permissions"]]

            cursor.execute("SELECT COUNT(*) as cnt FROM user_roles WHERE role_id = %s", (role["id"],))
            role["user_count"] = cursor.fetchone()["cnt"]

        conn.close()
        return roles
    finally:
        try:
            conn.close()
        except Exception: pass


def update_role(role_id: int, display_name: str = None, description: str = None,
                permissions: List[Tuple[str, str]] = None) -> bool:
    """更新角色信息和权限"""
    conn = get_connection()
    cursor = conn.cursor()

    # 检查是否为系统角色（只能改 description，不能改权限）
    cursor.execute("SELECT is_system FROM roles WHERE id = %s", (role_id,))
    role = cursor.fetchone()
    if not role:
        conn.close()
        return False

    try:
        # 更新基本信息
        if display_name is not None:
            cursor.execute("UPDATE roles SET display_name = %s WHERE id = %s", (display_name, role_id))
        if description is not None:
            cursor.execute("UPDATE roles SET description = %s WHERE id = %s", (description, role_id))

        # 更新权限（替换模式）— 系统角色也允许改权限（admin 可能需要自定义）
        if permissions is not None:
            cursor.execute("DELETE FROM role_permissions WHERE role_id = %s", (role_id,))
            for module, level in permissions:
                cursor.execute("""
                    INSERT INTO role_permissions (role_id, module, level)
                    VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """, (role_id, module, level))

            # 递增所有使用该角色的用户的 permission_version
            cursor.execute("""
                UPDATE users SET permission_version = permission_version + 1
                WHERE id IN (SELECT user_id FROM user_roles WHERE role_id = %s)
            """, (role_id,))

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()


def delete_role(role_id: int) -> Tuple[bool, str]:
    """删除角色（系统角色不可删）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT name, is_system FROM roles WHERE id = %s", (role_id,))
        role = cursor.fetchone()
        if not role:
            conn.close()
            return False, "角色不存在"
        if role["is_system"]:
            conn.close()
            return False, "系统内置角色不可删除"

        # 检查是否有用户在使用
        cursor.execute("SELECT COUNT(*) as cnt FROM user_roles WHERE role_id = %s", (role_id,))
        if cursor.fetchone()["cnt"] > 0:
            conn.close()
            return False, "该角色仍有用户在使用，请先解除绑定"

        cursor.execute("DELETE FROM roles WHERE id = %s", (role_id,))
        conn.commit()
        conn.close()
        return True, "删除成功"
    finally:
        try:
            conn.close()
        except Exception: pass


# ========================================
# 审计日志
# ========================================

MAX_SNAPSHOT_SIZE = 4096  # 4KB

def write_audit_log_with_cursor(cursor, user_id: int = None, username: str = None,
                                action: str = "", module: str = None,
                                entity_type: str = None, entity_id: int = None,
                                summary: str = "", before: Any = None, after: Any = None,
                                ip_address: str = None):
    """用**调用方的 cursor** 写审计 —— 让"业务写入 + 审计"能落在同一个事务里。

    [Review 2026-08-09 P1-2] `create_audit_log` 自己开连接、自己 commit,
    于是"业务已提交、审计失败"这种不可追责的中间态是可能的
    (管理员改了资金邻接字段,却查无对证)。需要原子性的调用方改用本函数,
    在自己的事务里调,失败就跟着一起回滚。

    🔴 本函数**不 commit**:提交与回滚都归调用方,否则"同一事务"这句话就是假的。
    🔴 序列化/截断口径与 `create_audit_log` **同源**(都走 `_audit_snapshots`),
      不许在这里另抄一份 —— 那就是第二套审计口径。
    """
    before_snapshot, after_snapshot = _audit_snapshots(
        before, after, entity_type=entity_type, entity_id=entity_id, summary=summary)
    cursor.execute("""
        INSERT INTO audit_logs
            (user_id, username, action, module, entity_type, entity_id,
             summary, before_snapshot, after_snapshot, ip_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (user_id, username, action, module, entity_type, entity_id,
          summary, before_snapshot, after_snapshot, ip_address))


def _audit_snapshots(before: Any, after: Any, *, entity_type=None, entity_id=None, summary=""):
    """before/after 序列化 + 超限截断 —— 审计快照的唯一口径。"""
    def _one(value):
        if value is None:
            return None
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > MAX_SNAPSHOT_SIZE:
            return json.dumps({
                "_truncated": True,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "summary": summary,
                "original_size": len(text),
            }, ensure_ascii=False)
        return text

    return _one(before), _one(after)


def create_audit_log(user_id: int = None, username: str = None,
                     action: str = "", module: str = None,
                     entity_type: str = None, entity_id: int = None,
                     summary: str = "", before: Any = None, after: Any = None,
                     ip_address: str = None):
    """写入审计日志(自开连接自提交 · 历史调用方保持逐位不变)。

    需要"业务与审计同一事务"的调用方请改用 `write_audit_log_with_cursor`。
    """
    # 序列化快照，限制大小(与 write_audit_log_with_cursor 同源,禁第二份口径)
    before_snapshot, after_snapshot = _audit_snapshots(
        before, after, entity_type=entity_type, entity_id=entity_id, summary=summary)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO audit_logs
                (user_id, username, action, module, entity_type, entity_id,
                 summary, before_snapshot, after_snapshot, ip_address)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (user_id, username, action, module, entity_type, entity_id,
              summary, before_snapshot, after_snapshot, ip_address))
        conn.commit()
        conn.close()
    finally:
        try:
            conn.close()
        except Exception: pass


def list_audit_logs(page: int = 1, page_size: int = 50,
                    user_id: int = None, action: str = None,
                    module: str = None, start_date: str = None,
                    end_date: str = None) -> Dict:
    """查询审计日志（分页 + 筛选）"""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        conditions = []
        params = []

        if user_id is not None:
            conditions.append("user_id = %s")
            params.append(user_id)
        if action:
            conditions.append("action = %s")
            params.append(action)
        if module:
            conditions.append("module = %s")
            params.append(module)
        if start_date:
            conditions.append("created_at >= %s")
            params.append(start_date)
        if end_date:
            conditions.append("created_at <= %s")
            params.append(end_date)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # 总数
        cursor.execute(f"SELECT COUNT(*) as total FROM audit_logs WHERE {where_clause}", params)
        total = cursor.fetchone()["total"]

        # 分页数据
        offset = (page - 1) * page_size
        cursor.execute(f"""
            SELECT * FROM audit_logs
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])

        logs = _rows_to_list(cursor.fetchall())

        # 解析 JSON 快照
        for log in logs:
            if log.get("before_snapshot"):
                try:
                    log["before_snapshot"] = json.loads(log["before_snapshot"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if log.get("after_snapshot"):
                try:
                    log["after_snapshot"] = json.loads(log["after_snapshot"])
                except (json.JSONDecodeError, TypeError):
                    pass

        conn.close()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "logs": logs
        }
    finally:
        try:
            conn.close()
        except Exception: pass


def get_audit_log(log_id: int) -> Optional[Dict]:
    """获取单条审计日志详情"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs WHERE id = %s", (log_id,))
        log = _row_to_dict(cursor.fetchone())
        if log:
            for field in ["before_snapshot", "after_snapshot"]:
                if log.get(field):
                    try:
                        log[field] = json.loads(log[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
        conn.close()
        return log
    finally:
        try:
            conn.close()
        except Exception: pass


# ========================================
# 模块信息查询（供前端权限矩阵编辑器使用）
# ========================================

def get_all_modules() -> List[Dict]:
    """返回所有模块和级别的元数据"""
    module_names = {
        "dashboard": "仪表盘",
        "brands": "客户管理",
        "diagnosis": "GEO诊断",
        "quote": "报价管理",
        "writing": "写作中心",
        "social": "社媒操盘手",
        "monitoring": "监测中心",
        "insights": "数据洞察",
        "reports": "报告管理",
        "ai_agents": "AI员工",
        "history": "历史记录",
        "advisors": "顾问团队",
        "settings": "系统设置",
        "users": "用户管理",
    }
    return [
        {"id": m, "label": module_names.get(m, m), "levels": ALL_LEVELS}
        for m in ALL_MODULES
    ]


# ========================================
# 初始化入口
# ========================================

if __name__ == "__main__":
    print("初始化 RBAC 数据库...")
    init_auth_db()
    print("✅ 6 张表创建完成")

    # 验证
    roles = list_roles()
    print(f"✅ 预置角色: {[r['display_name'] for r in roles]}")

    users = list_users()
    print(f"✅ 预置用户: {[u['display_name'] for u in users]}")

    # 验证 admin 权限
    admin = get_user(1)
    if admin:
        print(f"✅ Admin 用户: {admin['display_name']}, is_admin={admin['is_admin']}")
        print(f"   角色: {[r['display_name'] for r in admin['roles']]}")

    print("\n✅ RBAC 数据库初始化完成！")
