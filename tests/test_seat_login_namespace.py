"""[P0-B 追加]席位登录名命名空间的判别。

事故形态:员工登录名走 `users.username`,**全平台唯一**。A 公司先建了 zhangwei,
B 公司再建就冲突 —— 而且冲突要等到员工点开链接、填完密码、INSERT 撞唯一键才炸。
链接已经发出去了,员工撞一鼻子灰,团队长还不知道为什么。

Owner 点名的五条判别,逐条锁死:
① 两家不同团队各建 "zhangwei" → 都成功且互不冲突;
② 前置校验在**创建邀请**阶段命中重名(不是设密码阶段);
③ 短代码重复被拒;
④ 存量席位登录名不变且仍可登录(改登录名 = 让人登不进去,绝不能做);
⑤ 改团队名不影响已生成的登录名。
"""
import inspect

import pytest

from services import organization_short_code as sc


class _Cursor:
    """受控游标:按 SQL 关键词返回预置行,不碰真库。"""

    def __init__(self, *, user=None, membership=None, org=None, taken=()):
        self._user = user
        self._membership = membership
        self._org = org or {}
        self._taken = list(taken)
        self.executed: list[tuple[str, tuple]] = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        self.executed.append((self._last, params))

    def fetchone(self):
        sql = self._last
        if "FROM users WHERE username" in sql:
            return self._user
        if "organization_memberships" in sql:
            return self._membership
        if "FROM organizations WHERE id" in sql or "SELECT short_code" in sql:
            return self._org
        if "FROM organizations WHERE short_code" in sql:
            return {"1": 1} if self._org.get("_conflict") else None
        return None

    def fetchall(self):
        if "SELECT short_code FROM organizations" in self._last:
            return [{"short_code": c} for c in self._taken]
        return []


# ---------------------------------------------------------------- ① 两团队同名

def test_two_teams_can_each_have_zhangwei():
    a = sc.compose_login_name("sjkj", "zhangwei")
    b = sc.compose_login_name("xcwl", "zhangwei")
    assert a == "sjkj-zhangwei"
    assert b == "xcwl-zhangwei"
    assert a != b, "两家团队的 zhangwei 撞在一起 = 命名空间没起作用"


def test_member_name_only_needs_to_be_unique_within_team():
    """员工名规则比登录名整体宽松 —— 因为唯一性由短代码前缀保证。"""
    assert sc.normalize_member_name("zhangwei") == "zhangwei"
    assert sc.normalize_member_name("ZhangWei") == "zhangwei", "大小写必须归一,否则同名两个账号"


def test_composition_has_exactly_one_assembly_point():
    """预览、创建邀请、开户必须都走同一个拼装函数。

    三处各拼各的 → 迟早出现"预览显示 A、实际建成 B"。
    """
    from pathlib import Path

    root = Path(__file__).parent.parent
    api = (root / "api" / "organization_api.py").read_text(encoding="utf-8")
    assert "compose_login_name" in api, "接口没走统一拼装点"
    # 接口里不得自己用 f-string 拼 "code-name"
    assert 'f"{code}-{member_name}"' not in api


# ---------------------------------------------------------------- ② 前置校验时机

def test_precheck_happens_at_invite_creation_not_at_password_time():
    from services import organization_service as svc

    src = inspect.getsource(svc.create_invite)
    assert "check_login_name" in src, "创建邀请时没查重 → 冲突还是留到设密码那刻炸"
    # 必须在算 HMAC / 生成 token 之前就拦下
    assert src.index("check_login_name") < src.index("target_hmac(target_kind"), \
        "查重排在生成邀请之后 = 已经把链接造出来了才发现撞名"


def test_taken_name_is_rejected_with_change_it_wording():
    cursor = _Cursor(user={"id": 7, "is_active": 1},
                     membership={"organization_id": 99, "status": "active"})
    verdict = sc.check_login_name(cursor, login_name="sjkj-zhangwei", organization_id=1)
    assert verdict["status"] == sc.CHECK_TAKEN
    assert "请换一个" in verdict["message"]
    assert verdict["reason"] == "belongs_to_another_team"


def test_existing_free_account_gets_the_other_wording():
    """两种语义严格分开 —— 混成一句会让团队长做错决定。"""
    cursor = _Cursor(user={"id": 7, "is_active": 1}, membership=None)
    verdict = sc.check_login_name(cursor, login_name="sjkj-zhangwei", organization_id=1)
    assert verdict["status"] == sc.CHECK_EXISTING_ACCOUNT
    assert "已有账号" in verdict["message"] and "加入团队" in verdict["message"]
    assert "请换一个" not in verdict["message"], "自由账号被误报成占用 = 白白让团队长换名"


def test_free_name_is_available():
    cursor = _Cursor(user=None)
    verdict = sc.check_login_name(cursor, login_name="sjkj-zhangwei", organization_id=1)
    assert verdict["status"] == sc.CHECK_AVAILABLE


def test_disabled_account_is_not_silently_revived():
    cursor = _Cursor(user={"id": 7, "is_active": 0}, membership=None)
    verdict = sc.check_login_name(cursor, login_name="sjkj-zhangwei", organization_id=1)
    assert verdict["status"] == sc.CHECK_TAKEN
    assert verdict["reason"] == "account_disabled"


def test_same_org_member_is_reported_distinctly():
    cursor = _Cursor(user={"id": 7, "is_active": 1},
                     membership={"organization_id": 1, "status": "active"})
    verdict = sc.check_login_name(cursor, login_name="sjkj-zhangwei", organization_id=1)
    assert verdict["reason"] == "already_member_of_this_org"
    assert "你团队的成员" in verdict["message"]


# ---------------------------------------------------------------- ③ 短代码规则

@pytest.mark.parametrize("bad", ["abc", "a" * 13, "SJKJ!", "sj kj", "", "汉字代码"])
def test_invalid_short_codes_are_rejected(bad):
    with pytest.raises(sc.ShortCodeError):
        sc.normalize_short_code(bad)


@pytest.mark.parametrize("reserved", ["admin", "system", "omnirank"])
def test_reserved_short_codes_are_rejected(reserved):
    with pytest.raises(sc.ShortCodeError) as exc:
        sc.normalize_short_code(reserved)
    assert exc.value.code == "ORG_SHORT_CODE_RESERVED"


def test_duplicate_short_code_is_rejected():
    """③ 短代码重复必须被拒 —— 否则两家团队的员工登录名会撞。"""
    class _Dup(_Cursor):
        def fetchone(self):
            if "FROM organizations WHERE short_code = %s AND id <> %s" in self._last:
                return {"1": 1}
            if "SELECT short_code, short_code_locked" in self._last:
                return {"short_code": "old1", "short_code_locked": False}
            return None

    with pytest.raises(sc.ShortCodeError) as exc:
        sc.set_short_code(_Dup(), organization_id=1, short_code="sjkj")
    assert exc.value.code == "ORG_SHORT_CODE_TAKEN"


def test_short_code_can_only_be_changed_once():
    class _Locked(_Cursor):
        def fetchone(self):
            if "SELECT short_code, short_code_locked" in self._last:
                return {"short_code": "sjkj", "short_code_locked": True}
            return None

    with pytest.raises(sc.ShortCodeError) as exc:
        sc.set_short_code(_Locked(), organization_id=1, short_code="newx")
    assert exc.value.code == "ORG_SHORT_CODE_LOCKED"
    assert "登不进来" in exc.value.message


def test_unique_index_backs_the_rule():
    """应用层判重之外必须有库层唯一索引兜底(并发下应用层判重会漏)。"""
    src = inspect.getsource(sc.ensure_schema)
    assert "CREATE UNIQUE INDEX" in src and "organizations_short_code_unique" in src


# ---------------------------------------------------------------- 候选生成

def test_candidate_is_stable_not_random():
    """同一家公司每次看到的候选必须一样 —— 随机候选看起来像 bug。"""
    assert sc.suggest_short_code("杭州星辰网络") == sc.suggest_short_code("杭州星辰网络")


def test_candidate_prefers_ascii_in_company_name():
    assert sc.suggest_short_code("深圳数据科技SJKJ").startswith("sjkj")


def test_candidate_works_for_pure_chinese_names():
    code = sc.suggest_short_code("杭州星辰网络")
    assert sc.SHORT_CODE_MIN <= len(code) <= sc.SHORT_CODE_MAX
    sc.normalize_short_code(code)   # 候选本身必须是合法短代码


def test_candidate_avoids_taken_codes():
    first = sc.suggest_short_code("杭州星辰网络")
    second = sc.suggest_short_code("杭州星辰网络", taken=frozenset({first}))
    assert second != first


# ---------------------------------------------------------------- ④ 存量兼容

def test_existing_seats_are_never_renamed():
    """改登录名 = 让人登不进去。全仓不得有批量改 username 的迁移。"""
    from pathlib import Path

    root = Path(__file__).parent.parent
    for path in [root / "services" / "organization_short_code.py",
                 root / "services" / "organization_service.py",
                 root / "api" / "organization_api.py"]:
        src = path.read_text(encoding="utf-8")
        assert "UPDATE users SET username" not in src, f"{path.name} 在改存量登录名"


def test_short_code_is_only_applied_to_new_invites():
    """短代码只影响**新建**席位;存量席位的 username 与它无关。"""
    src = inspect.getsource(sc)
    # 模块只读写 organizations 表,不碰 users 的登录名
    assert "UPDATE users" not in src


def test_schema_change_is_additive_only():
    """加列 + 加索引,不改不删既有列 —— 存量数据零风险。"""
    src = inspect.getsource(sc.ensure_schema)
    # [WO_285b 改指] 加列改走 db/schema_guard.add_column_if_missing(先查目录,列缺失才 ADD COLUMN IF NOT EXISTS);
    #   只加不改的判据不变,它现在由调用这个只会加列的 helper 体现
    assert "add_column_if_missing(" in src
    import db.schema_guard as _sg
    assert "ADD COLUMN IF NOT EXISTS" in inspect.getsource(_sg.add_column_if_missing)
    for destructive in ("DROP COLUMN", "ALTER COLUMN", "DROP INDEX", "RENAME"):
        assert destructive not in src


# ---------------------------------------------------------------- ⑤ 改团队名不影响

def test_renaming_the_team_does_not_touch_the_short_code():
    """短代码一旦分配就与团队名脱钩 —— 否则改个名字全员登不进来。"""
    class _Assigned(_Cursor):
        def fetchone(self):
            if "SELECT short_code FROM organizations WHERE id" in self._last:
                return {"short_code": "sjkj"}
            return None

    cursor = _Assigned()
    # 公司名换了,取到的仍是既有短代码,且不产生 UPDATE
    code = sc.get_or_assign_short_code(cursor, organization_id=1, organization_name="完全不同的新名字")
    assert code == "sjkj"
    assert not [q for q, _ in cursor.executed if q.startswith("UPDATE organizations SET short_code")]


def test_login_name_depends_on_short_code_not_team_name():
    """登录名的输入只有短代码与员工名,团队名不参与。"""
    signature = inspect.signature(sc.compose_login_name)
    assert list(signature.parameters) == ["short_code", "member_name"]
