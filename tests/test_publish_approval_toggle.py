"""[发布审批开关]员工发布走"默认直接发 · 可开开关转审批"。

Owner 2026-07-25 拍板:
- 交付员工能直接把文章发到媒体上,**不用等人批**(老板雇人就是为了不当瓶颈);
- 但这要做成**一个开关**:打开 = 要审批,关闭 = 直接发。默认关。

本文件锁死:
1. 员工发布路由**可达**(此前交付角色持有 publish.* 却无任何路由 = 干不了本职工作);
2. 开关**没配**时按"直接发"处理(存量组织不会被 503 挡死);
3. 开关**打开**时不发布、不扣费,转成审批单,并给出 §13 合同(有下一步);
4. 非组织用户完全不受影响;
5. 只有发布这一个动作享受"默认放行",删除/外发/大额扣费仍维持 fail-closed。
"""
import inspect
import types

import pytest


# ---------------------------------------------------------------- 1. 路由可达

def test_publish_routes_are_reachable_by_delivery_employees():
    from services.organization_route_contract import match_member_geo_route

    execute = match_member_geo_route("POST", "/api/meijiehezi/publish/batch")
    assert execute is not None, "员工发不了文章 = 交付岗干不了本职工作"
    assert execute.capability == "publish.execute"

    single = match_member_geo_route("POST", "/api/meijiehezi/publish")
    assert single is not None and single.capability == "publish.execute"


def test_planning_routes_are_read_only_capability():
    """选品/看单是规划动作,不该要执行权。"""
    from services.organization_route_contract import match_member_geo_route

    for path in ("/api/meijiehezi/media", "/api/meijiehezi/orders"):
        policy = match_member_geo_route("GET", path)
        assert policy is not None and policy.capability == "publish.plan", path


def test_delivery_role_actually_holds_publish_execute():
    """路由接了但角色没这能力 = 白接。"""
    from services.organization_contract import (
        DELIVERY_ROLE_CAPABILITIES,
        SALES_ROLE_CAPABILITIES,
    )

    assert "publish.execute" in DELIVERY_ROLE_CAPABILITIES
    # 销售不发文章 —— 分工不越界
    assert "publish.execute" not in SALES_ROLE_CAPABILITIES


def test_publish_routes_carry_billing_tag():
    """发布要花钱,必须带计费标记,否则审计看不出这是笔支出。"""
    from services.organization_route_contract import match_member_geo_route

    assert match_member_geo_route("POST", "/api/meijiehezi/publish/batch").billing_feature


# ---------------------------------------------------------------- 2/5. 默认值只给发布

def test_missing_policy_defaults_to_caller_choice(monkeypatch):
    from services import organization_approvals as approvals
    from services.organization_contract import OrganizationError

    def missing(*a, **k):
        raise OrganizationError("ORG_APPROVAL_POLICY_MISSING", "审批策略缺失，操作已阻止", http_status=503)

    monkeypatch.setattr(approvals, "approval_required", missing)
    required, policy = approvals.approval_required_with_default(
        None, object(), action_type="publish.execute", default_required=False
    )
    assert required is False, "存量组织没配策略就会被 503 挡死 = 员工上不了班"
    assert policy.get("source") == "default"


def test_other_errors_still_propagate(monkeypatch):
    """只兜「没配策略」这一种;其它异常必须照常抛,不能把 fail-closed 吃掉。"""
    from services import organization_approvals as approvals
    from services.organization_contract import OrganizationError

    def denied(*a, **k):
        raise OrganizationError("ORG_CAPABILITY_DENIED", "无权", http_status=403)

    monkeypatch.setattr(approvals, "approval_required", denied)
    with pytest.raises(OrganizationError):
        approvals.approval_required_with_default(
            None, object(), action_type="publish.execute", default_required=False
        )


def test_configured_policy_wins_over_default(monkeypatch):
    """组织一旦配了策略,就以策略为准 —— 默认值只在"没配"时生效。"""
    from services import organization_approvals as approvals

    monkeypatch.setattr(approvals, "approval_required",
                        lambda *a, **k: (True, {"always_require_approval": True}))
    required, _ = approvals.approval_required_with_default(
        None, object(), action_type="publish.execute", default_required=False
    )
    assert required is True


def test_default_relaxation_is_only_used_for_publish():
    """删除/外发/大额扣费必须维持 fail-closed —— 不得也传 default_required。"""
    from pathlib import Path

    src = (Path(__file__).parent.parent / "api" / "meijiehezi_api.py").read_text(encoding="utf-8")
    # import 行 + 调用行 = 2 次;关键是只在一个动作上用
    assert src.count("approval_required_with_default") == 2
    assert 'action_type="publish.execute"' in src

    # 全仓只有发布这一处使用宽松默认
    root = Path(__file__).parent.parent
    users = [
        path for path in list(root.glob("api/*.py")) + list(root.glob("services/*.py"))
        if "approval_required_with_default" in path.read_text(encoding="utf-8")
        and path.name != "organization_approvals.py"
    ]
    assert [p.name for p in users] == ["meijiehezi_api.py"], f"宽松默认被扩散到: {users}"


# ---------------------------------------------------------------- 3. 开关打开 → 转审批

def test_gate_returns_contract_when_approval_is_on(monkeypatch):
    import api.meijiehezi_api as mjhz

    class _Identity:
        is_member = True
        membership_id = 11

    submitted = {}

    def fake_required(*a, **k):
        return True, {"always_require_approval": True}

    def fake_submit(identity, **kwargs):
        submitted.update(kwargs)
        return {"id": 77, "status": "pending"}

    import services.organization_approvals as approvals
    monkeypatch.setattr(approvals, "approval_required_with_default", fake_required)
    monkeypatch.setattr(approvals, "submit_approval", fake_submit)

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return self
        def commit(self): pass

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_db", lambda *a, **k: _Cur())

    request = types.SimpleNamespace(
        state=types.SimpleNamespace(organization_identity=_Identity())
    )
    result = mjhz._publish_approval_gate(request, articles=3, payload={"items": [1, 2, 3]})

    assert result is not None, "开关开着却直接发了 = 老板的控制失效"
    assert result["approval_submitted"] is True
    detail = result["detail"]
    assert detail["code"] == "PUBLISH_APPROVAL_REQUIRED"
    # §13:必须给下一步
    for field in ("reason", "impact", "repair_hint", "actions"):
        assert detail.get(field), field
    assert detail["actions"], "红码必须带可执行的下一步"
    assert submitted.get("action_type") == "publish.execute"


def test_gate_lets_employee_through_when_switch_is_off(monkeypatch):
    import api.meijiehezi_api as mjhz
    import services.organization_approvals as approvals

    class _Identity:
        is_member = True
        membership_id = 11

    monkeypatch.setattr(approvals, "approval_required_with_default",
                        lambda *a, **k: (False, {"source": "default"}))

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return self
        def commit(self): pass

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_db", lambda *a, **k: _Cur())

    request = types.SimpleNamespace(
        state=types.SimpleNamespace(organization_identity=_Identity())
    )
    assert mjhz._publish_approval_gate(request, articles=1, payload={}) is None


# ---------------------------------------------------------------- 4. 非组织用户零影响

def test_non_org_user_never_enters_the_gate():
    import api.meijiehezi_api as mjhz

    request = types.SimpleNamespace(state=types.SimpleNamespace(organization_identity=None))
    assert mjhz._publish_approval_gate(request, articles=5, payload={}) is None


def test_gate_runs_before_charging():
    """开关打开时不能先扣费再拦 —— 必须在扣费之前返回。"""
    import api.meijiehezi_api as mjhz

    for func in (mjhz.api_publish, mjhz.api_publish_batch):
        src = inspect.getsource(func)
        assert "_publish_approval_gate" in src
        gate_at = src.index("_publish_approval_gate")
        charge_at = src.find("deduct")
        if charge_at > 0:
            assert gate_at < charge_at, f"{func.__name__}: 先扣费后拦审批"
