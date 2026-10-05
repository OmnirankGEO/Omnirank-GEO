"""[工单 2026-08-06 §3] 「注册邀请归属」面板必须区分三态。

🔴 先说清这不是安全事故:注册闸没有被绕过(工单 §3.1 四条独立取证)。
本锁要防的是**误导性 UI** —— admin 用户治理页把组织操作员账号也塞进「注册邀请归属」,
只写一句「无邀请记录 / 没有邀请归属记录」,看的人分不清那是正常还是注册闸失守。
用户 #161(尚 · demo42-alpha)因此被判成超级 P0,查完发现它完全正常。
撞的是铁律 feedback_hint_must_help_or_hide:**提示要么帮人解决,要么不显示**。
当前这条两样都不占,它只制造工作量。

三态:
  1. organization_member —— 组织邀请进来的操作员,**本来就不该有**注册归属;
  2. self_signup + 有归属 —— 正常;
  3. self_signup + 无归属 —— **仍必须显示为需要关注**(别把真信号一起吞掉)。

🔴 反向对照是本锁的命门:§3.4 要求拿 #107(自助路径 · 真无归属)验证 ——
它必须仍然显示为需要关注,否则说明修改把真信号一起消掉了,整条作废。
"""
import datetime
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[2]

# _relationships 会读几个版本号做展示,与本锁判定无关,给全即可
_VERSIONS = {"commercial_binding": 1, "channel_relationship": 1, "registration": 1}


def _svc():
    spec = importlib.util.spec_from_file_location(
        "admin_user_governance", _ROOT / "services" / "admin_user_governance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCursor:
    """按 SQL 关键词回放行,避免为一条纯读逻辑起真库。

    🔴 只用于**分支判定**这一件事;任何涉及真实列名/归属的结论都不靠它
    (列名已用只读通道核过生产 information_schema)。
    """

    def __init__(self, membership=None, referral_rows=(), actors=None):
        self._membership = membership
        self._referral_rows = list(referral_rows)
        self._actors = actors or {}
        self._last = None
        self._rows = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM organization_memberships m" in s:
            self._last, self._rows = "membership", ([self._membership] if self._membership else [])
        elif "FROM referral_links" in s:
            self._last, self._rows = "referral", list(self._referral_rows)
        elif "FROM customer_agent_bindings" in s:
            self._last, self._rows = "binding", []
        elif "FROM users" in s and "id=%s" in s.replace(" ", ""):
            uid = params[0] if params else None
            self._last, self._rows = "actor", ([self._actors[uid]] if uid in self._actors else [])
        else:
            self._last, self._rows = "other", []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _registration(monkeypatched_actor, cur, user):
    m = _svc()
    m._actor = monkeypatched_actor
    return m._relationships(cur, user, _VERSIONS)["registration"], m


def _fake_actor(cur, uid):
    return {"user_id": int(uid), "display_name": f"用户{uid}", "is_active": True,
            "business_identity": "plain_user", "company": None}


def _user(uid, created="2026-07-01"):
    return {"id": uid, "created_at": datetime.datetime.fromisoformat(created + "T00:00:00")}


def test_state1_organization_member_is_not_shown_as_missing_attribution():
    """#161 / #159 这类:组织操作员,必须看得出所属组织,且不再说「无邀请记录」。"""
    cur = FakeCursor(membership={
        "organization_id": 2, "status": "active", "is_owner": False, "role_id": 7,
        "joined_at": datetime.datetime(2026, 8, 4, 11, 24),
        "org_name": "测试一下", "owner_user_id": 46,
    })
    reg, m = _registration(_fake_actor, cur, _user(161))
    assert reg["account_origin"] == "organization_member"
    assert reg["source"] == "organization_invite"
    assert reg["organization"]["name"] == "测试一下"
    assert reg["organization"]["owner"]["user_id"] == 46
    # 🔴 关键:文案必须回答「这正常吗」,而不是只把「无」说得软一点
    assert "组织操作员账号" in reg["evidence"]["label"]
    assert reg["evidence"]["status"] == "not_required"


def test_state3_self_signup_without_referral_stays_flagged():
    """🔴 反向对照(§3.4 点名):#107 这类自助注册且真无归属的,必须仍显示为需关注。

    这条要是绿不了,说明修改把真信号一起吞掉了 —— 整节作废。
    """
    cur = FakeCursor(membership=None, referral_rows=[])
    reg, m = _registration(_fake_actor, cur, _user(107, created="2026-06-30"))
    assert reg["account_origin"] == "self_signup"
    assert reg["evidence"]["status"] == "incomplete", "自助注册却无归属 —— 不许降级成 not_required"
    assert "需人工核实" in reg["evidence"]["label"]


def test_state3_notice_is_emitted_for_post_gate_self_signup():
    cur = FakeCursor(membership=None, referral_rows=[])
    m = _svc()
    m._actor = _fake_actor
    rel = m._relationships(cur, _user(107, created="2026-06-30"), _VERSIONS)
    codes = {n["code"] for n in rel["notices"]}
    assert "SELF_SIGNUP_WITHOUT_REFERRAL" in codes


def test_pre_gate_old_accounts_are_not_treated_as_anomalies():
    """🔴 必须不命中:闸(2026-06-10)上线前的老号不算异常。

    生产实测那批有 63 个。把它们一起标红 = 让唯一那个真的需要核实的账号淹没在噪声里,
    撞铁律 feedback_alert_minimalism_business_first。
    """
    cur = FakeCursor(membership=None, referral_rows=[])
    m = _svc()
    m._actor = _fake_actor
    rel = m._relationships(cur, _user(50, created="2026-03-01"), _VERSIONS)
    assert rel["registration"]["evidence"]["status"] == "not_required"
    assert "SELF_SIGNUP_WITHOUT_REFERRAL" not in {n["code"] for n in rel["notices"]}


def test_org_owner_is_not_laundered_into_organization_member():
    """🔴 必须不命中:组织**所有者**是自己注册后建的团队,不是被邀请进来的。

    把他也算成「组织操作员」= 把「老板自己没有邀请归属」这个真事实一起藏掉。
    生产实测 7 个受影响账号里有 2 个正是所有者(#46 / #102)。
    """
    cur = FakeCursor(membership={
        "organization_id": 2, "status": "active", "is_owner": True, "role_id": 1,
        "joined_at": datetime.datetime(2026, 7, 1), "org_name": "测试一下", "owner_user_id": 46,
    })
    reg, _ = _registration(_fake_actor, cur, _user(46, created="2026-07-01"))
    assert reg["account_origin"] == "self_signup"
    assert reg["evidence"]["status"] == "incomplete"


def test_frontend_panel_branches_on_account_origin():
    """前端必须真的按来源分流,不能只把文案改软(§3.3:只改文案不算修好)。"""
    src = (_ROOT / "frontend/src/pages/Admin/UserManagement.tsx").read_text(encoding="utf-8")
    assert "account_origin === 'organization_member'" in src, "前端没有按账号来源分流"
    assert "账号来源 · 组织操作员" in src, "组织操作员分支没有自己的标题"
    assert "所属团队" in src and "邀请人（团队所有者）" in src, "看不出所属组织与邀请人"
    # 🔴 反向:自助注册无归属那条必须仍然写成需要关注,不许被一起软化
    assert "需人工核实来历" in src
    # 光秃秃的「无邀请记录」不许再出现(它正是那次误报 P0 的源头)
    assert "'无邀请记录'" not in src
