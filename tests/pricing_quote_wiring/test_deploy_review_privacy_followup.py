"""Discriminating regressions for the Deploy privacy follow-up review."""

from __future__ import annotations
import ast

import asyncio
from datetime import datetime, timedelta

from starlette.requests import Request

from db.connection import get_db
from services import pricing_readiness

def _request_from_user(user: dict) -> Request:
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/pricing/procurement/catalog",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    })
    request.state.user = user
    return request


def test_procurement_authorizes_real_jwt_shape_from_wallet(monkeypatch):
    """The signed JWT has no agent_level; the endpoint must read the wallet SSOT."""
    from auth import jwt_utils
    from api import pricing_ssot_api

    monkeypatch.setattr(jwt_utils, "get_user", lambda _uid: {
        "id": 100,
        "username": "dealer-100",
        "display_name": "Dealer",
        "is_active": True,
        "is_admin": False,
        "roles": [],
        "permissions": [],
        "client_brand_ids": [],
        "permission_version": 1,
        "must_change_password": 0,
    })
    monkeypatch.setattr("db.team_db.get_user_team", lambda _uid: None)
    token = jwt_utils.create_jwt(100)
    payload = jwt_utils.decode_jwt(token)
    assert payload is not None
    assert "agent_level" not in payload

    monkeypatch.setattr(
        pricing_ssot_api,
        "_pricing_flags_or_503",
        lambda: {"PRICING_DUAL_SSOT_ENABLED": True, "CHANNEL_PRICING_ENABLED": False},
    )
    monkeypatch.setattr(
        pricing_ssot_api.pricing_catalog,
        "get_published_catalog",
        lambda *_args: {
            "version": {
                "id": 1,
                "version_code": "proc-v1",
                "calc_meta_jsonb": {
                    "pricing_config_snapshot": {
                        "wholesale_numer": 120000, "wholesale_denom": 195000,
                        "agent_purchase_bonus_rate": 0, "bonus_validity_months": 12,
                        "founding": {"cap": 10, "min_first_order_yuan": 500,
                                     "first_order_extra_bonus": 0},
                        "agent_tier_config": {},
                    }
                },
            },
            "items": [{
                "id": 1,
                "product_code": "credit_basic",
                "final_price_cents": 120000,
                "paid_points": 195000,
                "bonus_points": 0,
                "source_ref_jsonb": {
                    "kind": "agent_purchase_option", "option_id": "credit_basic",
                    "amount_cents": 120000,
                    "option": {"option_id": "credit_basic", "amount_cents": 120000,
                               "reward_eligible": False},
                },
            }],
        },
    )
    monkeypatch.setattr(pricing_ssot_api, "_product_names", lambda: {"credit_basic": "基础算力包"})

    response = asyncio.run(pricing_ssot_api.procurement_catalog(_request_from_user(payload)))
    assert response["success"] is True
    assert response["data"]["items"][0]["cash_price_cents"] == 120000


def test_procurement_quote_authorizes_real_jwt_shape_from_wallet(monkeypatch):
    from api import pricing_ssot_api
    from auth import jwt_utils

    monkeypatch.setattr(jwt_utils, "get_user", lambda _uid: {
        "id": 100,
        "username": "dealer-100",
        "display_name": "Dealer",
        "is_active": True,
        "is_admin": False,
        "roles": [],
        "permissions": [],
        "client_brand_ids": [],
        "permission_version": 1,
        "must_change_password": 0,
    })
    monkeypatch.setattr("db.team_db.get_user_team", lambda _uid: None)
    payload = jwt_utils.decode_jwt(jwt_utils.create_jwt(100))
    assert payload is not None and "agent_level" not in payload
    monkeypatch.setattr(
        pricing_ssot_api,
        "_pricing_flags_or_503",
        lambda: {"PRICING_DUAL_SSOT_ENABLED": True},
    )
    monkeypatch.setattr(
        pricing_ssot_api.price_quote,
        "issue_procurement_quote",
        lambda **_kwargs: {
            "quote_id": "proc-real-jwt",
            "final_price_cents": 120000,
            "points_granted": 195000,
            "bonus_points": 0,
            "currency": "CNY",
            "expires_at": datetime.now() + timedelta(minutes=15),
        },
    )
    monkeypatch.setattr(
        pricing_ssot_api.price_quote,
        "quote_order_pricing_snapshot",
        lambda _quote, required=False: {"resale_mode": False},
    )

    response = asyncio.run(
        pricing_ssot_api.procurement_quote(
            pricing_ssot_api.ProcurementQuoteRequest(product_code="credit_basic"),
            _request_from_user(payload),
        )
    )
    assert response["success"] is True
    assert response["data"]["quote_id"] == "proc-real-jwt"


def _seed_invite(*, issuer_id: int, invitee_id: int, issuer_level: int, code: str) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(id,username,is_active) VALUES (%s,%s,1) ON CONFLICT (id) DO NOTHING",
            (invitee_id, f"customer-{invitee_id}"),
        )
        cur.execute(
            "UPDATE user_wallets SET agent_level=%s WHERE user_id=%s",
            (issuer_level, issuer_id),
        )
        if issuer_level >= 1:
            cur.execute(
                "INSERT INTO public_account_codes(user_id,service_account_code) "
                "VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE "
                "SET service_account_code=EXCLUDED.service_account_code",
                (issuer_id, "SV-ABCDEFGH"),
            )
        cur.execute(
            "INSERT INTO invite_codes(user_id,code,code_type,expires_at,is_active) "
            "VALUES (%s,%s,'user',%s,TRUE)",
            (issuer_id, code, datetime.now() + timedelta(hours=1)),
        )


def _mark_service_provider_ready(user_id: int) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO public_account_codes(user_id,service_account_code) "
            "VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE "
            "SET service_account_code=EXCLUDED.service_account_code",
            (int(user_id), "SV-ABCDEFGH"),
        )


def test_service_provider_invite_records_attribution_and_initial_commercial_binding():
    from services.identity_service import consume_invite_code

    _seed_invite(issuer_id=100, invitee_id=610, issuer_level=1, code="AGENT610")
    result = consume_invite_code("AGENT610", 610)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT agent_user_id,binding_source,source_token,dispute_status "
            "FROM customer_agent_bindings WHERE customer_user_id=610"
        )
        binding = dict(cur.fetchone())
        cur.execute(
            "SELECT COUNT(*) AS n FROM referral_links WHERE referrer_id=100 AND referred_id=610"
        )
        referral_count = int(cur.fetchone()["n"])
    assert result["commercial_binding_action"] == "inserted"
    assert binding["agent_user_id"] == 100
    assert binding["binding_source"] == "invite_code"
    assert binding["source_token"].startswith("sha256:")
    assert "AGENT610" not in binding["source_token"]
    assert binding["dispute_status"] is None
    assert referral_count == 1


def test_ordinary_inviter_remains_promotional_only():
    from services.identity_service import consume_invite_code

    _seed_invite(issuer_id=200, invitee_id=620, issuer_level=0, code="USER620")
    result = consume_invite_code("USER620", 620)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM customer_agent_bindings WHERE customer_user_id=620")
        count = int(cur.fetchone()["n"])
    assert result["commercial_binding_action"] == "inviter_not_eligible"
    assert count == 0


def test_admin_personal_account_invite_never_becomes_commercial_binding():
    from services.identity_service import consume_invite_code

    _seed_invite(issuer_id=100, invitee_id=622, issuer_level=1, code="ADMIN622")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO roles(name) VALUES ('admin') RETURNING id")
        role_id = int(cur.fetchone()["id"])
        cur.execute("INSERT INTO user_roles(user_id,role_id) VALUES (100,%s)", (role_id,))
    result = consume_invite_code("ADMIN622", 622)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM customer_agent_bindings WHERE customer_user_id=622")
        count = int(cur.fetchone()["n"])
    assert result["commercial_binding_action"] == "inviter_not_eligible"
    assert count == 0


def test_legacy_service_provider_registration_code_uses_same_verified_binding_contract():
    from api.referral_api import bind_referral

    _mark_service_provider_ready(100)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users(id,username,is_active) VALUES (625,'customer-625',1)")
        cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (625,0)")
        cur.execute("INSERT INTO referral_codes(user_id,code) VALUES (100,'LEGACY625')")
    result = bind_referral(625, "LEGACY625")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM customer_agent_bindings WHERE customer_user_id=625")
        binding_count = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT COUNT(*) AS n FROM referral_links WHERE referrer_id=100 AND referred_id=625"
        )
        referral_count = int(cur.fetchone()["n"])
    assert result == {"commercial_binding_action": "inserted"}
    assert binding_count == 1
    assert referral_count == 1


def test_registration_invite_never_touches_existing_admin_binding():
    from services.identity_service import consume_invite_code

    _seed_invite(issuer_id=100, invitee_id=630, issuer_level=1, code="AGENT630")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO customer_agent_bindings"
            "(customer_user_id,agent_user_id,binding_source,source_token,bound_at) "
            "VALUES (630,200,'admin_manual','governance-v1',NOW())"
        )
    result = consume_invite_code("AGENT630", 630)

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM customer_agent_bindings WHERE customer_user_id=630")
        binding = dict(cur.fetchone())
    assert result["commercial_binding_action"] == "existing_binding_preserved"
    assert binding["agent_user_id"] == 200
    assert binding["binding_source"] == "admin_manual"
    assert binding["source_token"] == "governance-v1"
    assert binding["dispute_status"] is None


def test_invitation_compatibility_hook_preserves_existing_governed_binding():
    from services.commercial_service_routing import solidify_service_provider_invitation

    _mark_service_provider_ready(100)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users(id,username,is_active) VALUES (640,'customer-640',1)")

        cur.execute(
            "INSERT INTO customer_agent_bindings"
            "(customer_user_id,agent_user_id,binding_source,source_token,bound_at) "
            "VALUES (640,200,'admin_manual','governance-v2',NOW())"
        )
        cur.execute("INSERT INTO user_wallets(user_id,agent_level) VALUES (640,0)")
        result = solidify_service_provider_invitation(cur, 640, 100, "AGENT640")
    assert result["action"] == "existing_binding_preserved"

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT agent_user_id,binding_source FROM customer_agent_bindings WHERE customer_user_id=640")
        binding = dict(cur.fetchone())
    assert binding == {"agent_user_id": 200, "binding_source": "admin_manual"}


def test_pending_locked_orders_without_resolution_are_explicit_readiness_blockers():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO recharge_orders(
                   id,user_id,amount_cents,payment_status,order_type,sku_template_id,
                   agent_user_id,pricing_snapshot_jsonb,created_at
               ) VALUES (
                   'legacy-pending-resolution',400,180000,'pending','customer_recharge',1,
                   100,'{"amount_source":"sku_snapshot"}'::jsonb,NOW()
               )"""
        )
        result = pricing_readiness._check_order_snapshots(cur)

    assert result["ready"] is False
    assert result["pending_missing_commercial_resolution"] == 1
    assert result["pending_resolution_review_orders"] == ["legacy-pending-resolution"]
    assert any("逐笔" in problem and "commercial_resolution" in problem for problem in result["problems"])


_LEAKY_IDENTITY = ("服务方", "上游", "代理", "经销", "供货")


#: 🔴 三类字符串**不是对客串**,排除理由是**同一条原则**:它们都不会被客户看到。
#:   · 注释      —— AST 里没有,天然排除;
#:   · docstring —— 写给**维护者**;
#:   · 日志实参  —— 写给**运维**(`logger.*` / `print` 的参数)。
#: 三次收窄不是「削到绿为止」:每一次都由同一条原则支撑,
#: 而**对客串一条都没被排除掉**(正样本臂钉住这点)。
_INTERNAL_SINKS = ("debug", "info", "warning", "error", "exception", "critical", "log")


def _internal_only_nodes(tree) -> set:
    """docstring 与日志实参的 Constant 节点 id。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", getattr(fn, "id", "")) or ""
            if name in _INTERNAL_SINKS or name == "print":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        out.add(id(sub))
    return out


def _leaky_customer_strings(source: str) -> list[str]:
    """源码里**对客字符串字面量**内含中间方身份词的那些串。

    🔴 第一版只取 `ast.Constant`,把 `settlement_orchestrator` 的模块/函数
    docstring(里面大方写着 `credit_*:客户授权额度分轨`)和两行 `logger.info`
    (`代理返利 …`)全判成泄漏 —— **分母取宽了,红得有理有据但红错了对象**。
    """
    tree = ast.parse(source)
    skip = _internal_only_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        if any(w in node.value for w in _LEAKY_IDENTITY):
            hits.append(node.value)
    return hits


#: 🔴 必须**仍被抓到**的形态:这些都是客户看得见的地方。
#:    三次收窄(注释/docstring/日志)的风险正是「顺手把对客的也排掉」——
#:    这张表就是防这一格的。


def test_the_probe_still_catches_every_customer_facing_shape():
    """🔴 三次收窄之后,**对客的几种形态一条都不许被排掉**。

    「削到绿」与「削对了」在读数上同形 —— 只有这张表能分开。
    """
    leak = "您在服务方的账户额度"
    shapes = {
        "裸赋值": 'msg = "%s"' % leak,
        "HTTP 报错 detail": 'raise HTTPException(status_code=409, detail="%s")' % leak,
        "返回体": 'def f():' + chr(10) + '    return {"message": "%s"}' % leak,
        "f-string 拼接": 'x = f"前缀 %s 后缀"' % leak,
        "列表里": 'items = ["a", "%s"]' % leak,
    }
    missed = [k for k, src in shapes.items() if not _leaky_customer_strings(src)]
    assert not missed, "这些**对客**形态被收窄顺手排掉了:%s" % missed


def test_the_probe_ignores_internal_only_shapes():
    """反向:注释 / docstring / 日志实参**不算**对客串(排除的三类)。"""
    leak = "您在服务方的账户额度"
    internal = {
        "注释": '# %s' % leak + chr(10) + 'x = 1',
        "模块 docstring": '"""%s"""' % leak + chr(10) + 'x = 1',
        "函数 docstring": 'def f():' + chr(10) + '    """%s"""' % leak,
        "日志实参": 'logger.info("%s")' % leak,
        "print": 'print("%s")' % leak,
    }
    caught = [k for k, src in internal.items() if _leaky_customer_strings(src)]
    assert not caught, "这些**内部**形态被误判成泄漏:%s" % caught


def test_public_settlement_copy_does_not_reveal_hidden_service_provider():
    """🔴 结算编排器的**客户可见串**不许泄漏中间方身份。

    原判据锚的是一句具体文案(`"平台账户额度" in source`)作正样本对照。
    2026-07-27 单账本收敛(`2aef3b59b`)把 `allocate_credit` 连同它的文案一起搬走,
    **那个文件里现在一句对客余额文案都没有** —— 正样本锚失效,判据红,而代码没错。

    🔴 改成守**不变量**而不是守某一句话:检测器只看**字符串字面量**
    (不是全文 grep)—— 变量名叫 `代理` 不是对客串,注释里提到「服务方」也不是。
    搬家不会再让它失效。

    🔴 但否定臂**单独是恒真的**:那个文件已经没有对客文案,「没找到」与
    「根本没有可找的」同形。所以配正样本自证臂 —— 同一个检测函数喂合成源:
    含泄漏串的必须命中,只在注释里含这些词的必须不命中。
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2]
              / "services/settlement_orchestrator.py").read_text(encoding="utf-8")
    leaks = _leaky_customer_strings(source)
    assert not leaks, "结算编排器的客户可见串泄漏了中间方身份:%r" % leaks[:5]

def test_admin_finance_label_does_not_claim_a_retired_ledger_is_live():
    """🔴 [#118-10a] admin 面那个标签的锁 —— **修了但没锁,等于没修**。

    实测:把标签退回「服务方账户额度」时,本文件 12 条**一条都不红** ——
    (a) 那半修复当时没有任何东西守着它。

    两件事一起守:① 不含禁词「额度」;② 必须写明它指向的账本**已退役**,
    否则 admin 会以为 `customer_credit_transactions` 还在用
    (它自 2026-07-27 `2aef3b59b` 单账本收敛起已停写)。
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "api/finance_api.py").read_text(encoding="utf-8")
    m = re.search(r'"key": "customer_credit",\s*"label": "([^"]+)"', src)
    assert m, "finance_api 里找不到 customer_credit 那一项 —— 分母塌了,不是通过"
    label = m.group(1)
    assert "额度" not in label, "admin 标签仍含禁词「额度」:%r" % label
    assert ("退役" in label or "历史" in label), (
        "标签没写明这本账已退役,admin 会以为它还在用:%r" % label)
