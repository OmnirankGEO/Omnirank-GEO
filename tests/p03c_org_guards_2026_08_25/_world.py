"""造两条臂的**真**世界:非 org(legacy 冻结)与 org(真 charge link)。

两条臂都不许用 fake 顶替资金原语 —— 本包存在的理由就是"fake 顶过的地方会在生产炸"。
org 臂的 charge link 走**真 `reserve_charge`**(不是手插 organization_charge_links 行),
因为结算侧 `settle_charge` 会核对 link 上的授权证据/限额链,手插的行过不了那一关,
过不了就会退化成"判据在测一个生产不存在的形状"。
"""
from __future__ import annotations

import json
import os
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor

#: 🔴 每造一次世界就换一批用户 id。
#:  ① org 侧有「一个账号只能属于一个团队」的硬约束,复用 uid 第二次就 409;
#:  ② 更隐蔽的是钱包:legacy 臂与 org 臂若共用 OWNER_UID,后造的世界会把先造的
#:     钱包覆盖掉 —— 判据结果就变成了"谁先跑"的函数,而不是被测代码的函数。
_UID_BASE = 903_100
_UID_SEQ = [0]


def _fresh_uids():
    _UID_SEQ[0] += 2
    owner = _UID_BASE + _UID_SEQ[0]
    member = owner + 1
    return owner, member, "139" + str(member).rjust(8, "0")


FEATURE = "geo_diagnosis"

#: 报告"已就绪"的真实形态:{"client": {"modules": {"1": {...}}}}
#: (services/report_html_renderer.py:447)。写错这层包装 → is_client_report_ready 返 False
#: → 结算改道退款,判据会因为**错误的原因**变绿。
REPORT_READY = json.dumps(
    {"client": {"modules": {"1": {"insight": "有结论的正文"}}}}, ensure_ascii=False)


def _install_org_environment():
    """org 子系统的开关与密钥环 —— 取值照 `tests/organization_internal_seats/verify_local.py`
    的 `install_environment`,不自己另发明一套(另发明 = 又一处会漂移的双写)。
    """
    import base64
    os.environ["ORGANIZATION_SEATS_ENABLED"] = "true"
    os.environ["ORGANIZATION_SHARED_PAYER_ENABLED"] = "true"
    os.environ["ORGANIZATION_EXTERNAL_ACTIONS_ENABLED"] = "false"
    encoded = base64.urlsafe_b64encode(b"organization-test-key-material!!").decode("ascii").rstrip("=")
    for name in ("ORGANIZATION_INVITE_HMAC_KEYS",
                 "ORGANIZATION_INVITE_ENCRYPTION_KEYS",
                 "ORGANIZATION_TOKEN_HMAC_KEYS"):
        os.environ[name] = "v1:" + encoded
        os.environ[name + "_ACTIVE_VERSION"] = "v1"


def conn(dsn):
    c = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    c.autocommit = True
    c.cursor().execute("SET search_path = public")
    return c


def _uid(tag):
    return tag + "-" + uuid.uuid4().hex[:12]


def _ensure_user(cur, uid, name):
    cur.execute("INSERT INTO users (id, username, display_name, password_hash, email) "
                "VALUES (%s,%s,%s,'x',%s) ON CONFLICT (id) DO NOTHING",
                (uid, name, name, name + "@example.com"))


def _ensure_wallet(cur, uid, points):
    cur.execute(
        "INSERT INTO user_wallets (user_id, paid_points, bonus_points, commission_points, frozen_points) "
        "VALUES (%s,%s,0,0,0) ON CONFLICT (user_id) DO UPDATE SET "
        "paid_points=EXCLUDED.paid_points, bonus_points=EXCLUDED.bonus_points, "
        "commission_points=EXCLUDED.commission_points, frozen_points=EXCLUDED.frozen_points",
        (uid, points))


def _ensure_pricing(cur, cost=650):
    cur.execute(
        "INSERT INTO feature_pricing (feature_code, feature_name, cost_points) "
        "VALUES (%s,%s,%s) ON CONFLICT (feature_code) DO NOTHING",
        (FEATURE, "GEO 诊断", cost))


def _diagnosis_rows(cur, *, uid, brand_id, run_token, session_id,
                    engines, successful_tests, total_score, level,
                    billing_mode, freeze_task_ref=None, freeze_id=None, freeze_backend=None):
    """产物行 + run 行。engines/successful_tests 落**耐久列**,让被测代码自己去读。

    `diagnosis_runs.freeze_task_ref` 是 **NOT NULL**(生产 schema 实测),
    exempt / org 单也有 —— 它是 run 的资金锚,与"有没有 legacy 冻结行"是两回事。
    所以缺省就按生产同一个算法补上,不留 NULL。
    """
    if freeze_task_ref is None:
        from services.diagnosis_runs import freeze_task_ref as _ref
        freeze_task_ref = _ref(run_token)
    cur.execute(
        "INSERT INTO diagnosis_records (session_id, brand_name, industry, brand_id, run_token, "
        "result_visibility, total_score, level, report_v2_modules_jsonb, "
        "ai_total_tests, ai_engines_tested, total_questions_tested) "
        "VALUES (%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s) RETURNING id",
        (session_id, "P03C客户", "测试行业", brand_id, run_token, total_score, level,
         REPORT_READY, successful_tests, json.dumps(engines, ensure_ascii=False), 8))
    diagnosis_id = cur.fetchone()["id"]

    snapshot = {
        "type": "complete", "stage": "done", "progress": 100, "done": True, "terminal": True,
        "diagnosis_id": diagnosis_id, "share_token": "shr_" + uuid.uuid4().hex[:8],
        "message": "诊断完成",
        "result": {"total_score": total_score, "level": level},
    }
    cur.execute(
        "INSERT INTO diagnosis_runs (run_token, session_id, owner_user_id, brand_id, "
        "client_request_id, billing_mode, freeze_task_ref, freeze_id, freeze_backend, "
        "run_status, final_snapshot_jsonb) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',%s)",
        (run_token, session_id, uid, brand_id, "req_" + uuid.uuid4().hex[:10], billing_mode,
         freeze_task_ref, freeze_id, freeze_backend,
         json.dumps(snapshot, ensure_ascii=False)))
    return diagnosis_id, snapshot


def legacy_world(dsn, *, engines=("qwen", "deepseek", "kimi", "doubao"),
                 successful_tests=32, frozen=650, paid=10_000,
                 total_score=61, level="成长级", split=None):
    """非 org 臂:真 `point_freezes` 冻结行 + paid run。分派应走 `commit_run`。

    `split` 可给三池分布(默认全在 paid)。**多池 + 无拆分快照 + 降级交付**
    是走到 `reserved_split_order_unknown → settlement_manual` 的唯一自然路径,
    settlement-manual-ux 那一档的判据要靠它造真世界。
    (加的是**带默认值的可选参数**,原有调用点行为逐字不变。)
    """
    from services.diagnosis_runs import freeze_task_ref, mint_run_token
    uid, _member, _phone = _fresh_uids()
    OWNER_UID = uid
    c = conn(dsn)
    cur = c.cursor()
    _ensure_user(cur, OWNER_UID, "p03c_owner_%d" % OWNER_UID)
    _ensure_wallet(cur, OWNER_UID, paid)
    cur.execute("UPDATE user_wallets SET frozen_points=%s WHERE user_id=%s", (frozen, OWNER_UID))
    _ensure_pricing(cur)
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                ("P03C客户_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = cur.fetchone()["id"]

    run_token = mint_run_token()
    session_id = "sess_" + uuid.uuid4().hex[:12]
    task_ref = freeze_task_ref(run_token)
    pools = split or {"bonus": 0, "commission": 0, "paid": frozen}
    assert sum(pools.values()) == frozen, "三池之和必须等于冻结总额,否则造出来的是脏世界"
    cur.execute(
        "INSERT INTO point_freezes (user_id, feature_code, amount_total, amount_bonus, "
        "amount_commission, amount_paid, status, task_ref, brand_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,'frozen',%s,%s) RETURNING id",
        (OWNER_UID, FEATURE, frozen, pools["bonus"], pools["commission"], pools["paid"],
         task_ref, brand_id))
    freeze_id = cur.fetchone()["id"]

    diagnosis_id, snapshot = _diagnosis_rows(
        cur, uid=OWNER_UID, brand_id=brand_id, run_token=run_token, session_id=session_id,
        engines=list(engines), successful_tests=successful_tests,
        total_score=total_score, level=level, billing_mode="paid",
        freeze_task_ref=task_ref, freeze_id=freeze_id, freeze_backend="legacy")
    c.close()
    return {"arm": "legacy", "dsn": dsn, "run_token": run_token, "session_id": session_id,
            "diagnosis_id": diagnosis_id, "brand_id": brand_id, "uid": OWNER_UID,
            "freeze_id": freeze_id, "frozen": frozen, "paid": paid, "snapshot": snapshot,
            "organization_charge_id": None, "organization_claim_token": None,
            "organization_charge_points": None, "organization_identity": None}


def org_world(dsn, *, engines=("qwen", "deepseek", "kimi", "doubao"),
              successful_tests=32, total_score=61, level="成长级", points=650):
    """org 臂:真 org + 真 `reserve_charge` 出来的 charge link。分派应走 `settle_charge`。

    org 成员的 legacy 侧是 `billing_mode='exempt'`(server.py:3471)——钱不走
    `point_freezes` 而走 charge link,所以这条臂**没有**冻结行。
    """
    _install_org_environment()

    import asyncio

    from db.organization_db import resolve_identity
    from services.diagnosis_runs import mint_run_token
    from services.organization_approvals import configure_policy
    from services.organization_billing import claim_live_charge, reserve_charge
    from services.organization_limits import configure_limit
    from services.organization_payer_policy import put_payer_policy
    from services.organization_service import accept_invite, assign_brands, create_invite, create_organization

    OWNER_UID, MEMBER_UID, MEMBER_PHONE = _fresh_uids()
    c = conn(dsn)
    cur = c.cursor()
    _ensure_user(cur, OWNER_UID, "p03c_owner_%d" % OWNER_UID)
    _ensure_user(cur, MEMBER_UID, "p03c_member_%d" % MEMBER_UID)
    # 接受邀请要求受邀账号**已绑定并验证**邀请指定的联系方式
    # (organization_service.py:1584 `phone_verified` / :1610 target 必须非空)。
    # ⚠️ `users.is_active` 在生产 schema 里是 **integer DEFAULT 1**,不是 boolean
    #    (`phone_verified` 才是 boolean)。写 TRUE 会 DatatypeMismatch —— 类型这一维
    #    必须照真 schema 核过再写,不能照"看起来该是什么"写。
    cur.execute("UPDATE users SET phone=%s, phone_verified=TRUE, is_active=1 WHERE id=%s",
                (MEMBER_PHONE, MEMBER_UID))
    _ensure_wallet(cur, OWNER_UID, 2_000_000)
    _ensure_pricing(cur)
    cur.execute("INSERT INTO brands (name, owner_user_id) VALUES (%s,%s) RETURNING id",
                ("P03C_ORG客户_" + uuid.uuid4().hex[:6], OWNER_UID))
    brand_id = cur.fetchone()["id"]

    overview = create_organization(owner_user_id=OWNER_UID, name="P0-3c 判据团队 %d" % OWNER_UID,
                                   request_id=_uid("org-create"))
    organization_id = int(overview["id"])
    owner = resolve_identity(OWNER_UID, request_id=_uid("owner"))

    # 🔴 这条臂必须由**员工**下单,不能图省事用 owner:
    #   `resolve_identity(owner).is_member` 实测为 **False**,而 server.py:3388 正是拿
    #   `is_member` 判"这单走不走 org 计费口"—— owner 跑诊断在生产里根本不是 org 单。
    #   (这个假夹具被 `_world` 里那条 `assert owner.is_member` 当场拦下,没混进判据。)
    put_payer_policy(identity=owner, actor_user_id=OWNER_UID, shared_payer_enabled=True,
                     overage_enabled=True, per_action_limit_points=100_000,
                     daily_limit_points=1_000_000, monthly_limit_points=2_000_000,
                     reason="pytest 开启员工费用代付", expected_version=0,
                     request_id=_uid("payer-policy"), source_ip="192.0.2.7")
    # 建组织时 `billing.execute_high_cost` 的默认阈值是 **0**
    #   (organization_onboarding.py:237)⇒ 任何金额都要审批,员工一下单就 409。
    #   真实组织会把阈值配到业务线以上;这里照真 API 配,不是绕过审批。
    configure_policy(owner, request_id=_uid("policy"),
                     action_type="billing.execute_high_cost",
                     always_require_approval=False, threshold_points=100_000,
                     expected_version=1, reason="pytest 高额审批阈值抬到业务线以上")

    cur.execute("SELECT id FROM organization_roles WHERE organization_id=%s AND code='sales'",
                (organization_id,))
    role_id = int(cur.fetchone()["id"])
    invite = create_invite(owner, target_kind="phone", target=MEMBER_PHONE, role_id=role_id,
                           request_id=_uid("invite"), source_ip="192.0.2.5", brand_ids=[brand_id])
    accepted = accept_invite(authenticated_user_id=MEMBER_UID, token=invite["delivery_token"],
                             request_id=_uid("accept"), source_ip="192.0.2.6")
    membership_id = int(accepted["membership_id"])
    assign_brands(owner, membership_id=membership_id, brand_ids=[brand_id],
                  reason="pytest 指派诊断品牌", request_id=_uid("assign"))
    # 员工使用上限必须配全,否则 reserve 走到 ORG_LIMIT_POLICY_MISSING(409)。
    configure_limit(owner, membership_id=membership_id, limit_kind="daily_total",
                    limit_points=1_000_000, reason="pytest 日上限")
    configure_limit(owner, membership_id=membership_id, limit_kind="monthly_total",
                    limit_points=2_000_000, reason="pytest 月上限")
    member = resolve_identity(MEMBER_UID, request_id=_uid("member"))
    assert member.is_member, "员工不算 member ⇒ 这条臂不会走 org 计费口,夹具在测假路径"

    # 🔴 顺序照 server.py:3619-3655 的生产序列,不自己发明:
    #   run_token 先 mint(reserve 要拿它当 task_ref)→ reserve → **claim_live_charge**。
    #   少了 claim 那一步,charge 上没有活租约,分派段第一件事
    #   `mark_external_side_effect_started`(server.py:2740)就抛「任务租约已失效」,
    #   整单被外层 except 兜走 release —— 我第一版夹具就是这么错的,
    #   它会把"org 臂"判据变成实际在测 2970 那条退款分支。
    run_token = mint_run_token()
    session_id = "sess_" + uuid.uuid4().hex[:12]
    charge = asyncio.run(reserve_charge(
        member, execution_id=_uid("exec"), feature_code=FEATURE,
        work_kind="diagnosis.run", payload={"p03c": True, "run_token": run_token},
        brand_id=brand_id, task_ref=run_token, dispatch_ttl_seconds=1800))
    assert charge.get("status") == "reserved", charge
    charge_id = int(charge["id"])
    points = int(charge["reserved_ceiling_points"])
    claim = claim_live_charge(member, charge_link_id=charge_id, lease_seconds=1800)
    assert not claim.get("in_progress"), claim
    claim_token = str(claim["claim_token"])

    diagnosis_id, snapshot = _diagnosis_rows(
        cur, uid=MEMBER_UID, brand_id=brand_id, run_token=run_token, session_id=session_id,
        engines=list(engines), successful_tests=successful_tests,
        total_score=total_score, level=level, billing_mode="exempt")
    c.close()
    return {"arm": "org", "dsn": dsn, "run_token": run_token, "session_id": session_id,
            "diagnosis_id": diagnosis_id, "brand_id": brand_id, "uid": MEMBER_UID,
            "freeze_id": None, "snapshot": snapshot,
            "organization_id": organization_id,
            "organization_charge_id": charge_id,
            "organization_claim_token": claim_token,
            "organization_charge_points": points,
            "organization_identity": member,
            "charge": charge}


def observe(dsn, world):
    """结算后从库里读**可观察终态** —— 不读时间列(时间进判据 = 定时炸弹)。"""
    c = conn(dsn)
    cur = c.cursor()
    out = {}
    cur.execute("SELECT run_status, last_settlement_error FROM diagnosis_runs WHERE run_token=%s",
                (world["run_token"],))
    out["run"] = dict(cur.fetchone() or {})
    cur.execute("SELECT result_visibility FROM diagnosis_records WHERE id=%s",
                (world["diagnosis_id"],))
    out["record"] = dict(cur.fetchone() or {})
    if world.get("freeze_id"):
        cur.execute("SELECT status, amount_total FROM point_freezes WHERE id=%s",
                    (world["freeze_id"],))
        out["freeze"] = dict(cur.fetchone() or {})
        cur.execute("SELECT paid_points, bonus_points, commission_points, frozen_points "
                    "FROM user_wallets WHERE user_id=%s", (world["uid"],))
        out["wallet"] = dict(cur.fetchone() or {})
    if world.get("organization_charge_id"):
        cur.execute("SELECT status, actual_points FROM organization_charge_links WHERE id=%s",
                    (world["organization_charge_id"],))
        out["charge"] = dict(cur.fetchone() or {})
    c.close()
    return out
