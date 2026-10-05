"""#113 D1:poll-only 抢占不查活租约(整链复现锁)。

事实链(生产 2026-09-05,795 冻结 650 算力 23h):
```
10:46:53 worker claim_live_charge → token live_xxx · lease +1800
10:46:56 mark_external_side_effect_started(live_xxx) → 外部调用已开始
10:48:13 用户浏览器轮询 → claim_poll_only_charge **覆写 token 为 poll_yyy**
         (它的 UPDATE 谓词只有 `status IN ('claimed','running') AND external_... IS NOT NULL`,
          **没有 lease_until<=NOW()**)· 此刻 lease 还剩 30 分钟
10:48:14 原 worker 下一次 mark_external(live_xxx) → `WHERE claim_token=%s` 命中 0 行
         → ORG_WORK_LEASE_LOST → 冻结无人 commit/release
```
🔴 同一条不变量(**活租约不可被第二 claimant 覆写**)在同文件写了两处,
   `claim_live_charge:983` 守了(`status='pending' OR (claimed AND lease_until<=NOW())`),
   `claim_poll_only_charge:1103` **没守**。一处有一处没有,而没守的那处不报错。

触发条件不是极端场景:**用户停在页面上刷新**(Deploy 归档:78 秒内轮询约 35 次)。

⚠️ 本文件复用同包 `test_review_cto_regressions._organization_charge` 的驱动,
   不自己再造一份组织/身份/charge —— 造第二份夹具就是造第二份口径。
"""

from __future__ import annotations

import pytest

from services.organization_billing import (
    claim_poll_only_charge,
    mark_external_side_effect_started,
)
from db import marketing_db
from tests.marketing_content_center.test_review_cto_regressions import _geo, _job


@pytest.fixture(autouse=True)
def _tolerate_fingerprint_only_drift(monkeypatch):
    """🔴 **自限制的**就绪门旁路 —— 只放行「指纹对不上」这一种,别的照旧红。

    实测:本地一次性测试库 `readiness()` 唯一不过项是 `schema_contract`
    (537 列 vs 期望 536,**多一列**;表/约束/索引全对)—— 那是我这个
    throwaway 库重放迁移时的漂移,不是生产条件。

    为什么这不是「夹具替被测代码干活」:就绪门是**部署级检查**,
    与本判据要测的不变量(活租约不可被第二 claimant 覆写)是两件事。
    但旁路必须**自限制**:一旦缺表/缺列/缺约束,`readiness` 的其他键会非空,
    这里就**不放行** —— 否则它会顺手把真正的 schema 缺失也藏起来。
    """
    import db.organization_db as ODB

    real = ODB.assert_ready

    def _lenient(cursor):
        try:
            return real(cursor)
        except Exception:
            r = ODB.readiness(cursor=cursor)
            hard = {k: v for k, v in r.items()
                    if k.startswith(('missing_', 'wrong_')) and v}
            if hard:
                raise AssertionError('组织 schema 真的缺东西,不是指纹漂移:%r' % hard)
            return None

    monkeypatch.setattr(ODB, 'assert_ready', _lenient)
    for mod in ('services.organization_billing', 'services.organization_service'):
        m = __import__(mod, fromlist=['x'])
        if hasattr(m, 'assert_ready'):
            monkeypatch.setattr(m, 'assert_ready', _lenient)


def _outbox_token(charge_link_id: int) -> str:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT claim_token FROM organization_work_outbox WHERE charge_link_id=%s",
                    (int(charge_link_id),))
        row = cur.fetchone()
        return (row["claim_token"] if row else "") or ""
    finally:
        conn.close()


def _world(monkeypatch):
    """造一份**真**的世界:组织 + 身份 + 真 job + 真 attempt + reserved charge + live claim。

    🔴 为什么不能直接用同包的 `_organization_charge`:它把 charge 的
    `payload={"job_id": user_id}` 指向一个**并不存在的 job**。
    `claim_poll_only_charge` 会按 `job_id + provider_task_id + 三个 state`
    去锁一条真 attempt(organization_billing.py:1080-1094),对不上就在到达
    那条**脆弱 UPDATE 之前**抛 `ORG_PROVIDER_ATTEMPT_CHANGED` ——
    那样这条判据测的是另一格,而它照样会红。**红了不等于红对了。**
    """
    import asyncio
    from uuid import uuid4

    from db.connection import get_connection
    from services.organization_billing import claim_live_charge, reserve_charge
    from services.organization_service import create_organization, resolve_identity

    monkeypatch.setenv('ORGANIZATION_SEATS_ENABLED', 'true')
    monkeypatch.setenv('ORGANIZATION_SHARED_PAYER_ENABLED', 'true')
    user_id = 100000 + (uuid4().int % 800000)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO users(id,username,display_name,phone,email,password_hash) '
            'VALUES (%s,%s,%s,%s,%s,%s)',
            (user_id, 'p113-%d' % user_id, 'P113 租约', ('13%09d' % user_id)[-11:],
             'p113-%d@example.test' % user_id, 'x'))
        cur.execute('INSERT INTO user_roles(user_id,role_id) '
                    "SELECT %s,id FROM roles WHERE name='user' LIMIT 1", (user_id,))
        cur.execute('INSERT INTO user_wallets(user_id,paid_points,total_recharged,agent_level) '
                    'VALUES (%s,2000000,2000000,1)', (user_id,))
        conn.commit()
    finally:
        conn.close()

    job = _job(_geo(), user_id=user_id)
    task_id = 'apimart-%s' % uuid4().hex[:8]
    attempt = marketing_db.add_attempt(
        job_id=int(job['id']), attempt_no=1,
        component_id='professional_poster:image:professional_poster',
        provider='apimart', provider_task_id=task_id, status='running',
        submit_state='submitted', poll_state='succeeded')

    create_organization(owner_user_id=user_id, name='P113 Lease',
                        request_id='org-%s' % uuid4().hex)
    identity = resolve_identity(user_id, request_id='id-%s' % uuid4().hex)
    charge = asyncio.run(reserve_charge(
        identity, execution_id='charge-%s' % uuid4().hex, feature_code='mktg_poster_basic',
        work_kind='geo_content_package', payload={'job_id': int(job['id'])},
        brand_id=None, task_ref='p113-%d' % user_id))
    claim = claim_live_charge(identity, charge_link_id=int(charge['id']), lease_seconds=1800)
    return identity, int(charge['id']), claim['claim_token'], int(attempt['id']), task_id


def test_a_live_worker_keeps_its_lease_when_the_browser_polls(monkeypatch):
    """🔴 [判据 5 · 整链复现] 活 worker 在浏览器轮询之后**仍能继续**外部调用。

    这是本单的**主判据**:修前它必红(mark_external 抛 ORG_WORK_LEASE_LOST),
    修后必绿。红读数在修前已留档 —— 「修好了」这句话需要一个修前的对照。
    """
    identity, cid, live_token, attempt_id, task_id = _world(monkeypatch)

    # 1. worker 开始外部调用(provider 已被调用)
    mark_external_side_effect_started(
        charge_link_id=cid, claim_token=live_token, lease_seconds=1800)
    assert _outbox_token(cid) == live_token

    # 2. 用户在页面上刷新 → poll-only 恢复路径
    try:
        # provider_task_id 必须给真值:None 会在到达那条脆弱 UPDATE **之前**
        # 就被「轮询恢复缺少 provider task」拦掉 —— 那样这条判据测的是另一格。
        claim_poll_only_charge(identity, charge_link_id=cid, attempt_id=attempt_id,
                               provider_task_id=task_id, lease_seconds=1800)
    except Exception as err:          # 修后这里可能合法地拒绝(也可能返回 in_progress)
        # 🔴 比 **code 属性**,不是比 `str(err)`:`OrganizationError.__init__`
        #    只 `super().__init__(message)`,**str(err) 里根本没有 code** ——
        #    上一版写成 `"ORG_..." in str(err)` 是**永假**子句。
        #    后果不是漏放行,是 Review 的 P1 毒那一红**落在容忍子句上而不是不变量上**:
        #    红了,但红错了地方。
        assert getattr(err, "code", None) == "ORG_POLL_RECLAIM_CONFLICT", (
            "非预期异常 code=%r msg=%r" % (getattr(err, "code", None), err))

    # 3. 🔴 关键:原 worker 的下一次外部调用**必须仍然成功**
    #    修前这一步抛 ORG_WORK_LEASE_LOST —— 冻结从此无人 commit/release。
    mark_external_side_effect_started(
        charge_link_id=cid, claim_token=live_token, lease_seconds=1800)

    # 4. token 同一性:比整串,不比包含(轮询不许把活 token 换掉)
    assert _outbox_token(cid) == live_token, (
        "活 worker 的 claim_token 被轮询覆写了 —— 它下一次 mark_external 就会 LEASE_LOST")


def _expire_lease(charge_link_id: int):
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE organization_work_outbox SET lease_until=NOW()-INTERVAL '1 second' "
                    "WHERE charge_link_id=%s", (int(charge_link_id),))
        conn.commit()
    finally:
        conn.close()


def test_poll_only_can_take_over_once_the_lease_actually_expired(monkeypatch):
    """🔴 [判据 1 第二臂] 租约**真的过期**后,轮询必须能接管,且新 token 前缀 poll_。

    没有这一臂,「活租约不许被抢」那条可以用「永远不许接管」来满足 ——
    那样 worker 真死了之后冻结就永远悬着,**比原缺陷更坏**。

    🔴 这一臂还有第二个作用:它是**唯一会真正执行那条 UPDATE** 的路径。
    第一臂走 `in_progress` 提前返回,**UPDATE 一行都不跑** ——
    我第一版的 SQL 里 `ESCAPE` 参数被转义吃空(`ESCAPE ''`),
    而第一臂**照样绿**。判据绿 ≠ 那段代码跑过。
    """
    identity, cid, live_token, attempt_id, task_id = _world(monkeypatch)
    mark_external_side_effect_started(
        charge_link_id=cid, claim_token=live_token, lease_seconds=1800)
    _expire_lease(cid)

    claim_poll_only_charge(identity, charge_link_id=cid, attempt_id=attempt_id,
                           provider_task_id=task_id, lease_seconds=1800)

    token = _outbox_token(cid)
    assert token != live_token, "租约已过期却没接管 —— 冻结会永远悬着"
    assert token.startswith("poll_"), "接管后的 token 前缀不是 poll_:%r" % token


def test_poll_to_poll_handover_is_allowed(monkeypatch):
    """轮询之间互相接管无害(原 token 本身是 poll_)—— 否则她刷第二次就卡死。"""
    identity, cid, live_token, attempt_id, task_id = _world(monkeypatch)
    mark_external_side_effect_started(
        charge_link_id=cid, claim_token=live_token, lease_seconds=1800)
    _expire_lease(cid)
    claim_poll_only_charge(identity, charge_link_id=cid, attempt_id=attempt_id,
                           provider_task_id=task_id, lease_seconds=1800)
    first_poll = _outbox_token(cid)
    # 第二次轮询:此时 lease 是新的(未过期),但原 token 是 poll_ ⇒ 允许接管
    claim_poll_only_charge(identity, charge_link_id=cid, attempt_id=attempt_id,
                           provider_task_id=task_id, lease_seconds=1800)
    second_poll = _outbox_token(cid)
    assert second_poll.startswith("poll_")
    assert second_poll != first_poll, "poll→poll 接管被拒了 —— 用户刷第二次就卡死"


def test_the_sql_gate_is_kept_as_defence_in_depth():
    """🔴 SQL 里那条活租约谓词**故意保留**,即使注毒删掉它三条行为臂都不红。

    实测毒 L2(只删 SQL 谓词、留 Python 前置检查)⇒ **3 条全绿**。
    对称的另一形(只删 Python 门、留 SQL 谓词)⇒ **1 红**(arm1)。
    🔴 两形**都不是洞**:行锁让两处等价,任一处单独存在都守得住不变量;
    留两处是纵深防御。差别只在**谁先拒**:留 SQL 门时走 409,
    留 Python 门时走 in_progress —— 后者对用户更友好,所以两处都保留。
    定性是**冗余不是洞**,依据是量出来的:
    `claim_poll_only_charge` 在前置检查**之前**已调
    `_read_work_snapshot(..., for_update=True)`,那条 `SELECT ... FOR UPDATE`
    锁住了 outbox 行;`mark_external_side_effect_started` 同样锁 ⇒
    前置检查与 UPDATE 处在**同一把行锁内**,两个 claimant 串行。

    保留它的理由:**行锁是别处的实现细节**。哪天有人把 `for_update=True` 改掉、
    或把前置检查挪到锁之前,竞态窗口就回来了 —— 而那时**三条行为臂仍然全绿**。
    这条结构锁的作用就是让「顺手清掉这个冗余」当场变红,
    并把上面那段推理留在原地,免得下一个人重新推一遍。
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "services" / "organization_billing.py").read_text(encoding="utf-8")
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == "claim_poll_only_charge":
            seg = ast.get_source_segment(src, n) or ""
            assert "lease_until<=NOW()" in seg, "SQL 里的活租约谓词被删了(纵深防御)"
            assert "LEFT(claim_token, 5) = 'poll_'" in seg, "poll→poll 接管条件被删了"
            assert "for_update=True" in seg, (
                "outbox 行锁没了 —— 前置检查与 UPDATE 之间出现竞态窗口,"
                "而三条行为臂对它是盲的")
            return
    raise AssertionError("找不到 claim_poll_only_charge")

def test_the_tolerance_clause_can_actually_match():
    """🔴 自证:上面那个容忍子句**必须真能为真**。

    上一版比的是 `str(err)`,而 `OrganizationError` 只把 message 传给 super ——
    子句**永假**。一个永假的容忍子句不会漏放行,但会让「异常来自哪一格」这件事
    永远判错;而它自己**不制造任何要去看的问题**。
    这条拿一个真的 OrganizationError 打一发,证明 code 取得到、str 里确实没有。
    """
    from services.organization_contract import OrganizationError

    err = OrganizationError("ORG_POLL_RECLAIM_CONFLICT", "已付费任务轮询权获取失败")
    assert getattr(err, "code", None) == "ORG_POLL_RECLAIM_CONFLICT"
    assert "ORG_POLL_RECLAIM_CONFLICT" not in str(err), (
        "str(err) 里现在含 code 了 —— 上面那条注释的前提变了,回来重读")


# ══════════════════════════════════════════════════════════════════════════
# 归类:租约被抢 ≠ 授权撤销
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("code,want", [
    ("ORG_WORK_LEASE_LOST", "worker_lease_lost_after_provider"),
    ("ORG_CHARGE_LEASE_LOST", "worker_lease_lost_after_provider"),
    ("ORG_CHARGE_AUTHORITY_REVOKED", "live_authority_revoked_after_provider"),
    (None, "live_authority_check_failed_after_provider"),
    ("ORG_SOMETHING_NEW", "live_authority_check_failed_after_provider"),
])
def test_authority_failure_is_classified_by_the_underlying_code(code, want):
    """🔴 **分类默认值不许取具体档**。

    原来无差别贴 `live_authority_revoked_after_provider`,对客文案是
    「品牌授权已撤销,成图未交付」。而真实原因常常是**租约被轮询抢走** ——
    她的授权好好的,系统却告诉她授权没了。
    **一句具体但错误的话比一句笼统的话更坏**:笼统让人继续找,错的具体让人停止找。

    最后两格是**默认档**:认不出的 code(含 None、含将来新增的)必须落到笼统档,
    不许落到任何一个具体档。
    """
    from services.marketing.geo_factory import _authority_failure_code
    from services.organization_contract import OrganizationError

    exc = OrganizationError(code, "x") if code else RuntimeError("x")
    assert _authority_failure_code(exc) == want


def test_the_code_survives_the_image_client_hop():
    """🔴 底层 code 必须**穿过** `image_client` 那一跳 —— 驱动**真的 raise 点**。

    原来 `raise LiveAuthorityRejected(str(exc))` 只留 message,code 在这里就丢了,
    上游想分流也无从分起。

    🔴 上一版这条**自己构造** `LiveAuthorityRejected(..., code=...)` 再断言 code 在 ——
    那是**判据自构中间值**:实测毒 M3(把 raise 点改回丢 code)**13 条全绿**。
    判据必须让**被测代码**去构造那个异常,不能替它构造。
    """
    import asyncio

    from services.marketing import image_client
    from services.marketing.geo_factory import _authority_failure_code
    from services.organization_contract import OrganizationError

    def _guard_that_loses_the_lease():
        raise OrganizationError('ORG_WORK_LEASE_LOST', '任务租约已失效')

    with pytest.raises(image_client.LiveAuthorityRejected) as ex:
        asyncio.run(image_client._guard(_guard_that_loses_the_lease))

    # 由**生产代码**造出来的那个异常,必须带得住 code
    assert getattr(ex.value, 'code', None) == 'ORG_WORK_LEASE_LOST', (
        'image_client 那一跳把 code 丢了:%r' % (getattr(ex.value, 'code', None),))
    assert _authority_failure_code(ex.value) == 'worker_lease_lost_after_provider'


def _copy_tables_carrying(code: str) -> dict:
    """机械枚举 content_center 里收录了某个 code 的**全部**文案表。"""
    from services.marketing import content_center as C

    out = {}
    for name in dir(C):
        if name.startswith('_'):
            continue
        val = getattr(C, name)
        if isinstance(val, dict) and code in val:
            out[name] = val
    return out


def test_the_table_census_actually_finds_several():
    """正样本自证:普查必须真的找到多张表,否则下面那条是空转。"""
    tables = _copy_tables_carrying('live_authority_revoked_after_provider')
    assert len(tables) >= 4, '只找到 %d 张表,普查可疑:%s' % (len(tables), sorted(tables))


def test_lease_lost_copy_never_says_authorization_revoked():
    """🔴 对客文案:**凡是收录旧码的表,新码必须都在**。

    分母**机械枚举**,不手写表名:`content_center` 有 7 张文案表,其中 4 张
    收录了 `live_authority_revoked_after_provider`。我第一版只补了
    `ERROR_MESSAGES` 一张、判据也只查那一张 —— 另外三张会落到兜底文案
    或**直接把原始 code 露给用户**,而那一版判据**全绿**。

    外加一条内容约束:租约被抢**不许说「授权」** —— 她的授权好好的,
    说授权撤销会让她去找一个根本不存在的问题。
    """
    tables = _copy_tables_carrying('live_authority_revoked_after_provider')
    for new_code in ('worker_lease_lost_after_provider',
                     'live_authority_check_failed_after_provider'):
        missing = sorted(n for n, tbl in tables.items() if new_code not in tbl)
        assert not missing, (
            '这些表收录了旧码却没有新码 %s:%s —— 用户会看到兜底文案或原始 code'
            % (new_code, missing))
    for name, tbl in tables.items():
        val = tbl.get('worker_lease_lost_after_provider')
        text = val if isinstance(val, str) else str(val)
        assert '授权' not in text, '%s 里租约文案仍在说授权:%r' % (name, text)


# ══════════════════════════════════════════════════════════════════════════
# D2-a:进 unknown 必告警 · 恢复链不再每 60s 空转
# ══════════════════════════════════════════════════════════════════════════

def _alert_rows(fingerprint: str) -> int:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM ai_ops_alerts WHERE fingerprint=%s",
                    (fingerprint,))
        return int((cur.fetchone() or {}).get("n") or 0)
    finally:
        conn.close()


def test_going_unknown_opens_a_critical_alert(monkeypatch):
    """🔴 进 unknown **必须有人知道**。

    unknown 的唯一出口是 `force_release_charge`(手工,须 lease 过期)。
    在此之前**没有任何告警** —— 795 冻着 650 算力 23 小时,恢复链每 60 秒撞一次 409,
    24h 跑了 564 次,**没有一个人被通知**。**沉默的死锁比报错的死锁难发现得多。**

    这条驱动**真的** `quarantine_charge`,不自己构造告警行。
    """
    import asyncio

    from services.organization_billing import quarantine_charge

    identity, cid, live_token, attempt_id, task_id = _world(monkeypatch)
    mark_external_side_effect_started(
        charge_link_id=cid, claim_token=live_token, lease_seconds=1800)

    fp = "org_charge_unknown:%d" % cid
    before = _alert_rows(fp)
    asyncio.run(quarantine_charge(charge_link_id=cid, reason="RECOVERY_AMBIGUOUS_AFTER_START"))
    after = _alert_rows(fp)
    assert after == before + 1, (
        "进 unknown 没开告警(fingerprint=%s):%d -> %d" % (fp, before, after))


def test_the_alert_fingerprint_is_per_charge(monkeypatch):
    """🔴 fingerprint 按 charge 精确 —— 不同 charge 不许互相盖住。

    盖住的后果是:第二笔冻结进 unknown 时,告警面上**什么都不会新增**,
    而它和「没有第二笔」在读数上完全同形。
    """
    import asyncio

    from services.organization_billing import quarantine_charge

    ids = []
    for _ in range(2):
        identity, cid, live_token, attempt_id, task_id = _world(monkeypatch)
        mark_external_side_effect_started(
            charge_link_id=cid, claim_token=live_token, lease_seconds=1800)
        asyncio.run(quarantine_charge(charge_link_id=cid,
                                      reason="RECOVERY_AMBIGUOUS_AFTER_START"))
        ids.append(cid)
    assert ids[0] != ids[1]
    for cid in ids:
        assert _alert_rows("org_charge_unknown:%d" % cid) >= 1, (
            "charge %s 的告警被另一笔盖住了" % cid)


def _count_settle_for(monkeypatch, job_id: int) -> list:
    """只数 job_id 这一个 job 的 `_settle_geo` 调用。

    reconcile 扫整表,同库残留的别的 job 会把计数染脏 —— 见下面两条用例的抬头。
    """
    from services.marketing import geo_factory

    calls = []
    real = geo_factory._settle_geo

    def _spy(job, *a, **k):
        if int((job or {}).get('id') or 0) == int(job_id):
            calls.append(1)
        return real(job, *a, **k)

    monkeypatch.setattr(geo_factory, '_settle_geo', _spy)
    return calls


def _park_job_as_pending(cid: int):
    """把 job 摆成生产里那个卡住的姿势;返回 job_id(摆不出返 None)。"""
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE marketing_material_jobs SET billing_ref=%s,"
            " error_summary='billing_commit_pending', status='generating' WHERE id=("
            "  SELECT (payload_snapshot->'request_payload'->>'job_id')::int"
            "    FROM organization_work_outbox WHERE charge_link_id=%s LIMIT 1"
            ") RETURNING id",
            ('org:%d' % cid, cid))
        row = cur.fetchone()
        conn.commit()
        return int(row['id']) if row else None
    finally:
        conn.close()


def _job_error_summary(job_id: int) -> str:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute('SELECT error_summary FROM marketing_material_jobs WHERE id=%s',
                    (int(job_id),))
        return str((cur.fetchone() or {}).get('error_summary') or '')
    finally:
        conn.close()


def _unknown_job(monkeypatch):
    """建世界 → 越过外部边界 → quarantine 成 unknown → 摆成卡住姿势。"""
    import asyncio

    from services.organization_billing import quarantine_charge

    identity, cid, live_token, attempt_id, task_id = _world(monkeypatch)
    mark_external_side_effect_started(
        charge_link_id=cid, claim_token=live_token, lease_seconds=1800)
    asyncio.run(quarantine_charge(charge_link_id=cid,
                                  reason='RECOVERY_AMBIGUOUS_AFTER_START'))
    job_id = _park_job_as_pending(cid)
    if not job_id:
        pytest.skip('夹具没摆出候选 job')
    return cid, job_id


def test_reconcile_gives_up_and_tells_someone(monkeypatch):
    """🔴 **停手不等于沉默**:钱还冻着,出口(force_release_charge)需要人。

    这条**独立于计数**。上一版把告警断言排在被污染的计数之后,
    于是它**从来没跑到过** —— 删掉告警调用的毒读到的红,红在计数上。
    """
    import asyncio

    from services.marketing import geo_factory

    cid, job_id = _unknown_job(monkeypatch)
    fp = 'geo_reconcile_gave_up:org:%d' % cid
    before = _alert_rows(fp)
    asyncio.run(geo_factory.reconcile_recoverable_geo_jobs(limit=200))
    assert _alert_rows(fp) == before + 1, (
        '停手了却没人被通知(fingerprint=%s)' % fp)
    assert _job_error_summary(job_id) == 'provider_resolution_pending', (
        'job 没被摆成需要人工的那一档:%r' % _job_error_summary(job_id))


def test_reconcile_stops_retrying_once_the_charge_is_unknown(monkeypatch):
    """🔴 计数**只数本用例这一个 job**。

    上一版数的是所有 job 的 `_settle_geo` 调用,而 reconcile **扫整表** ——
    同库里别的用例残留的 job(仍 reserved + 活租约)每轮都进 settle。
    实测:`-k` 单独跑得 (2,2),全文件跑得 (0,0) —— **那个 0 是顺序运气,不是修复**。
    连带后果更坏:告警类毒的红**全落在这条被污染的计数上**,
    于是「停手告警」这件事**当时没有任何锁真的证明它有牙**。**红了 ≠ 红对了。**
    """
    import asyncio

    from services.marketing import geo_factory

    cid, job_id = _unknown_job(monkeypatch)
    calls = _count_settle_for(monkeypatch, job_id)
    asyncio.run(geo_factory.reconcile_recoverable_geo_jobs(limit=200))
    first = len(calls)
    asyncio.run(geo_factory.reconcile_recoverable_geo_jobs(limit=200))
    second = len(calls) - first
    assert (first, second) == (0, 0), (
        'unknown charge 仍在调结算:第一轮 %d 次、第二轮 %d 次' % (first, second))


def test_reconcile_does_call_settle_while_the_charge_is_still_reserved(monkeypatch):
    """🔴 区分力臂:charge 仍 reserved 时,**这个 job 的**计数必须 ≥1。

    没有这一臂,上面那条 (0,0) 是恒真的 —— 把整个恢复链删掉它照样绿。
    同样按 job 过滤:数整表的话,别的用例残留会让它**恒 ≥1**,也是恒真。
    """
    import asyncio

    from services.marketing import geo_factory

    identity, cid, live_token, attempt_id, task_id = _world(monkeypatch)
    mark_external_side_effect_started(
        charge_link_id=cid, claim_token=live_token, lease_seconds=1800)
    job_id = _park_job_as_pending(cid)          # 不 quarantine,charge 保持 reserved
    if not job_id:
        pytest.skip('夹具没摆出候选 job')
    calls = _count_settle_for(monkeypatch, job_id)
    asyncio.run(geo_factory.reconcile_recoverable_geo_jobs(limit=200))
    assert len(calls) >= 1, (
        'reserved 的 charge 也没调结算 —— 上面那条 (0,0) 断言是恒真的')
