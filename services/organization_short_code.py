"""[P0-B 追加]团队短代码 —— 让员工登录名有命名空间。

## 为什么要这个

员工登录名走 `users.username`,**全平台唯一**。于是 A 公司先建了 `zhangwei`,
B 公司再想建 `zhangwei` 就冲突 —— 而且冲突要等到员工点开链接设密码那一刻才炸,
团队长根本不知道自己填的名字早被别家占了。

引入团队短代码后,登录名统一是 `{短代码}-{员工名}`(如 `sjkj-zhangwei`),
员工名只需**团队内**唯一,跨公司同名不再打架。

## 规则

- 短代码 4-12 位 `[a-z0-9]`,全局唯一(库里唯一索引兜底);
- 团队创建/首次邀请时自动出候选(从公司名推),团队长**可改一次**;
- 改过一次之后锁定 —— 短代码是登录名的一部分,反复改会让人登不进去;
- **改团队名不影响短代码**,自然也不影响已生成的登录名。

## 候选怎么出

仓里没有拼音库,所以不做汉字转拼音,而是:
1. 公司名里的 ASCII 字母数字(很多公司名带英文/字母缩写)—— 优先;
2. 取不到就用公司名的稳定哈希转 base36 —— 保证同名公司拿到同一个候选,
   团队长看着眼熟,不满意可以改。

**绝不随机**:随机意味着同一家公司每次打开页面看到的候选都不一样,像 bug。
"""

from __future__ import annotations

from db.schema_guard import add_column_if_missing
import hashlib
import re
from typing import Optional

SHORT_CODE_MIN = 4
SHORT_CODE_MAX = 12
_SHORT_CODE_RE = re.compile(rf"^[a-z0-9]{{{SHORT_CODE_MIN},{SHORT_CODE_MAX}}}$")

#: 员工名(短代码后面那截)的规则。比登录名整体宽松,因为只需团队内唯一。
MEMBER_NAME_MIN = 2
MEMBER_NAME_MAX = 20
_MEMBER_NAME_RE = re.compile(rf"^[a-z0-9._-]{{{MEMBER_NAME_MIN},{MEMBER_NAME_MAX}}}$")

#: 保留字:这些短代码会让登录名看起来像系统账号,不给用。
RESERVED_SHORT_CODES = frozenset({
    "admin", "root", "system", "test", "demo", "null", "none",
    "omnirank", "official", "support", "service", "staff",
})

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


class ShortCodeError(ValueError):
    """短代码不合规。调用方负责翻成 §13 合同。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_short_code(value: str) -> str:
    """归一并校验短代码。不合规直接抛,带机器码。"""
    normalized = str(value or "").strip().casefold()
    if not _SHORT_CODE_RE.match(normalized):
        raise ShortCodeError(
            "ORG_SHORT_CODE_INVALID",
            f"团队代码须为 {SHORT_CODE_MIN}-{SHORT_CODE_MAX} 位小写字母或数字。",
        )
    if normalized in RESERVED_SHORT_CODES:
        raise ShortCodeError("ORG_SHORT_CODE_RESERVED", "这个团队代码不可用，请换一个。")
    return normalized


def normalize_member_name(value: str) -> str:
    """归一并校验**员工名**(登录名里短代码后面那截)。"""
    normalized = str(value or "").strip().casefold()
    if not _MEMBER_NAME_RE.match(normalized):
        raise ShortCodeError(
            "ORG_MEMBER_NAME_INVALID",
            f"员工名须为 {MEMBER_NAME_MIN}-{MEMBER_NAME_MAX} 位小写字母/数字/下划线/点/连字符。",
        )
    return normalized


def compose_login_name(short_code: str, member_name: str) -> str:
    """拼出员工的完整登录名。

    这是**唯一**的拼装点 —— 前端预览、创建邀请、开户三处必须都走它,
    否则三处各拼各的,迟早出现"预览显示 A、实际建成 B"。
    """
    return f"{normalize_short_code(short_code)}-{normalize_member_name(member_name)}"


def _ascii_candidate(organization_name: str) -> Optional[str]:
    """从公司名里的 ASCII 字母数字取候选。"""
    letters = re.sub(r"[^a-z0-9]", "", str(organization_name or "").casefold())
    if len(letters) >= SHORT_CODE_MIN:
        return letters[:SHORT_CODE_MAX]
    return None


def _hash_candidate(organization_name: str) -> str:
    """稳定哈希候选:同名公司永远拿到同一个,不是随机串。"""
    digest = hashlib.sha256(str(organization_name or "").encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big")
    out = []
    while value and len(out) < 6:
        value, rem = divmod(value, 36)
        out.append(_BASE36[rem])
    candidate = "".join(reversed(out)) or "0000"
    return candidate.rjust(SHORT_CODE_MIN, "0")[:SHORT_CODE_MAX]


def suggest_short_code(organization_name: str, *, taken: frozenset[str] = frozenset()) -> str:
    """给团队长一个候选。已被占用就加数字后缀,不抛异常(候选永远给得出)。"""
    base = _ascii_candidate(organization_name) or _hash_candidate(organization_name)
    base = re.sub(r"[^a-z0-9]", "", base)[:SHORT_CODE_MAX]
    if len(base) < SHORT_CODE_MIN:
        base = (base + _hash_candidate(organization_name))[:SHORT_CODE_MAX]
    if base in RESERVED_SHORT_CODES:
        base = (base + "1")[:SHORT_CODE_MAX]

    if base not in taken:
        return base
    for suffix in range(2, 100):
        trimmed = base[: SHORT_CODE_MAX - len(str(suffix))]
        candidate = f"{trimmed}{suffix}"
        if len(candidate) >= SHORT_CODE_MIN and candidate not in taken:
            return candidate
    # 极端情况:回落到哈希候选 + 后缀
    return (_hash_candidate(organization_name) + "9")[:SHORT_CODE_MAX]


def ensure_schema(cursor) -> None:
    """幂等自愈兜底。**正式来源是迁移脚本,不是这里。**

    权威 DDL = ``scripts/migration_organization_short_code_2026_07_28.sql``,
    已登记 ``db/migration_manifest.py``,由 prestart 单飞执行。本函数与它逐字等价,
    留着只是为了"迁移没跑到"的环境不至于在运行时 UndefinedColumn 崩。

    🔴 别再拿它当迁移的替代品。它是**懒初始化**:不跑到这条代码路径就不建列,
    于是 DDL 会在某个说不准的时刻悄悄发生 —— 2026-07-28 它潜伏数日后被第一个真实
    用户触发,当场改表,绕过迁移清单也就绕过了 organization_schema_contract 的目录
    指纹,导致部署就绪门与 server.py 容器启动门**双 fail-closed**,新容器一律起不来。
    新增列请走迁移脚本 + manifest 登记 + 契约指纹轮换,不要再往这里加。

    短代码存小写,唯一索引直接建在列上(归一已保证小写,不必再 lower())。
    `short_code_locked` 记"团队长已经改过一次" —— 短代码是登录名的一部分,
    反复改会让人登不进去。
    """
    add_column_if_missing(cursor, "organizations", "short_code", "TEXT")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
    add_column_if_missing(cursor, "organizations", "short_code_locked", "BOOLEAN NOT NULL DEFAULT FALSE")  # [WO_285b] 列缺失才 ALTER(请求路径可达)
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS organizations_short_code_unique "
        "ON organizations(short_code) WHERE short_code IS NOT NULL"
    )


def taken_short_codes(cursor) -> frozenset[str]:
    cursor.execute("SELECT short_code FROM organizations WHERE short_code IS NOT NULL")
    return frozenset(str(r["short_code"]) for r in (cursor.fetchall() or []) if r.get("short_code"))


def get_or_assign_short_code(cursor, *, organization_id: int, organization_name: str) -> str:
    """取该团队的短代码;没有就按候选自动分配一个(不锁定,团队长仍可改一次)。"""
    ensure_schema(cursor)
    cursor.execute(
        "SELECT short_code FROM organizations WHERE id = %s FOR UPDATE", (int(organization_id),)
    )
    row = cursor.fetchone()
    if row and row.get("short_code"):
        return str(row["short_code"])

    candidate = suggest_short_code(organization_name, taken=taken_short_codes(cursor))
    cursor.execute(
        "UPDATE organizations SET short_code = %s, updated_at = NOW() WHERE id = %s",
        (candidate, int(organization_id)),
    )
    return candidate


def set_short_code(cursor, *, organization_id: int, short_code: str) -> str:
    """团队长改短代码。只允许改一次;重复被别家占用则明确拒绝。"""
    ensure_schema(cursor)
    normalized = normalize_short_code(short_code)

    cursor.execute(
        "SELECT short_code, short_code_locked FROM organizations WHERE id = %s FOR UPDATE",
        (int(organization_id),),
    )
    row = cursor.fetchone()
    if not row:
        raise ShortCodeError("ORG_NOT_FOUND", "团队不存在。")
    if row.get("short_code_locked"):
        raise ShortCodeError(
            "ORG_SHORT_CODE_LOCKED",
            "团队代码只能修改一次,已经改过了。它是员工登录名的一部分,反复修改会让已有员工登不进来。",
        )

    cursor.execute(
        "SELECT 1 FROM organizations WHERE short_code = %s AND id <> %s",
        (normalized, int(organization_id)),
    )
    if cursor.fetchone():
        raise ShortCodeError("ORG_SHORT_CODE_TAKEN", "这个团队代码已被其他团队使用,请换一个。")

    cursor.execute(
        "UPDATE organizations SET short_code = %s, short_code_locked = TRUE, updated_at = NOW() "
        "WHERE id = %s",
        (normalized, int(organization_id)),
    )
    return normalized


# ---------------------------------------------------------------------------
# [P0-B ①] 创建邀请前的登录名前置校验
# ---------------------------------------------------------------------------
# 冲突绝不能推到"员工点开链接设密码"那一刻才炸 —— 那时团队长已经把链接发出去了,
# 员工撞一鼻子灰,团队长还不知道为什么。

#: 三种结果,语义严格分开(Owner 点名:两种提示不许混成一句)
CHECK_AVAILABLE = "available"                # 没人用,可以直接建
CHECK_EXISTING_ACCOUNT = "existing_account"  # 已有账号,可以以既有账号加入团队
CHECK_TAKEN = "taken"                        # 被占用(已属别的团队等),必须换一个


def check_login_name(cursor, *, login_name: str, organization_id: int) -> dict:
    """查这个完整登录名现在能不能用。返回 §13 可直接渲染的结构。

    三态而不是布尔:"已被占用请换一个"和"这是已有账号将以既有账号加入"是**完全
    不同的两件事**,混成一句话会让团队长做错决定。
    """
    normalized = str(login_name or "").strip().casefold()

    cursor.execute(
        "SELECT id, is_active FROM users WHERE username = %s", (normalized,)
    )
    user = cursor.fetchone()
    if not user:
        return {
            "status": CHECK_AVAILABLE,
            "login_name": normalized,
            "message": "这个登录名可以用。",
        }

    # 已有账号 —— 再看他是不是已经属于某个团队
    cursor.execute(
        """SELECT organization_id, status FROM organization_memberships
            WHERE user_id = %s AND status IN ('active','suspended','leaving')""",
        (int(user["id"]),),
    )
    membership = cursor.fetchone()

    if membership:
        same_org = int(membership["organization_id"]) == int(organization_id)
        return {
            "status": CHECK_TAKEN,
            "login_name": normalized,
            "message": (
                "这个登录名已经是你团队的成员了。"
                if same_org else
                "该登录名已被占用,请换一个。"
            ),
            "reason": "already_member_of_this_org" if same_org else "belongs_to_another_team",
        }

    if not user.get("is_active"):
        # 停用账号既不能直接用,也不该被静默"复活"
        return {
            "status": CHECK_TAKEN,
            "login_name": normalized,
            "message": "该登录名已被占用,请换一个。",
            "reason": "account_disabled",
        }

    return {
        "status": CHECK_EXISTING_ACCOUNT,
        "login_name": normalized,
        "message": "这是已有账号,将以既有账号加入团队。",
        "reason": "existing_free_account",
        "hint": "对方需要用原有密码登录后接受邀请,不会重新设密码。",
    }
