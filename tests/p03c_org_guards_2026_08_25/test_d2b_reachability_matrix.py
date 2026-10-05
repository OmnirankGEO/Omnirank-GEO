"""#113 D2-b 返修 · **可达性**矩阵(驱动真循环,不是再加源码锁)。

## Review 判 NO-GO 的那条洞

窄门 `_try_auto_release_undelivered` 原来**只接在**
`error in {billing_commit_pending, billing_release_pending}` 分支内。
而 D2-a 一旦把 job 停进 `provider_resolution_pending`,它下一轮就落到 `else`
⇒ **永远到不了窄门**。

生产实证(Deploy 只读取证 2026-09-06):freeze 795 的真链是
`job 12 / charge_link 1 / outbox 1 / attempt 9`,job 12 此刻正停在
`provider_resolution_pending` —— 窄门刚上线就对它唯一的目标失效了。

🔴 **我写了 22 条判据、下了 3 发毒,全都在那条不可达的分支里面。**
   毒证明了窄门有牙,没有一条证明它够得着。**存在 ≠ 可达。**
   本文件的每一条都**驱动 `reconcile_recoverable_geo_jobs` 本身**,
   断言打在库里的终态上,不打返回值、不打源码文本。

## 795 的真形与 Owner 的放宽(2026-09-06)

Deploy 只读取证:`marketing_material_assets` 里 job 12 **有 1 行** ——
`asset_kind='copy'`、`is_final=t`、`publish_allowed=1`、正文完整。
也就是**文案交付了、图没交付**,属于「部分交付」而不是「零交付」。

Owner 最初批 D2-b 的措辞是「供应商已成功但**未交付**」,按那个口径窄门会拒绝 795。
**2026-09-06 Owner 拍板放宽:部分交付也退。**

🔴 放宽的依据不是新规则:仓里正常路径 `_settle_geo` 的
`partial = bool(assets) and not full` 那一支走的就是
`release_freeze("GEO 内容包部分成功整单免单")` + `succeeded / partial_free_release`
—— **整单免单**。窄门只是让**恢复路径**用上同一条既有规则,不是自己发明一套。
⇒ 窄门条件 = `not full`;`full` 那一格**没有**放宽,它才是真会赔钱的那格。

🔴 原来钉「795 被拒」的那条(`..._is_refused_under_the_approved_wording`)
   是**改写**成 `..._is_released_and_lands_as_partial`,不是删除;
   同时新增 `test_a_fully_delivered_job_is_never_auto_released` 守住 full。
   只删不补的话,「放宽」就只剩正向臂,没有任何东西守住 full。
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
from pathlib import Path

import pytest

from tests.p03c_org_guards_2026_08_25._world import conn, org_world  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROD_SCHEMA = ROOT / "tests" / "article_self_report_2026_08_19" / "prod_schema_2026-08-19.sql"
MARKETING_TABLES = (
    "marketing_material_jobs",
    "marketing_material_generation_attempts",
    "marketing_material_assets",
)


def _prod_ddl(table: str) -> str:
    """从生产 dump 抠 CREATE TABLE。**不手搓最小表** —— 见 #130 的教训:
    手搓的表与生产不同构时判据照样全绿,却证不了生产上那条 SQL 跑得通。"""
    src = io.open(PROD_SCHEMA, encoding="utf-8", errors="replace").read()
    head = "CREATE TABLE public.%s (" % table
    i = src.index(head)
    j = src.index("\n);", i) + len("\n);")
    return src[i:j].replace("CREATE TABLE public.", "CREATE TABLE IF NOT EXISTS public.", 1)


@pytest.fixture
def world(live_server, live_dsn, monkeypatch):
    """真 org charge + 一个停在 provider_resolution_pending 的 marketing job。"""
    c = conn(live_dsn)
    cur = c.cursor()
    for t in MARKETING_TABLES:
        cur.execute(_prod_ddl(t))
    cur.execute("DELETE FROM public.marketing_material_assets")
    cur.execute("DELETE FROM public.marketing_material_generation_attempts")
    cur.execute("DELETE FROM public.marketing_material_jobs")
    c.close()

    w = org_world(live_dsn)
    cid = int(w["organization_charge_id"])
    state = {"dsn": live_dsn, "cid": cid, "uid": int(w["uid"])}
    return state


def _make_job(state, *, error_summary, components=2):
    """建一个 `_geo` job。`components` 决定 `_full_component_plan` 的期望件数。"""
    c = conn(state["dsn"])
    cur = c.cursor()
    geo = {"brand": {"name": "D2B"}, "evidence": {}, "contact": {},
           "channels": ["xiaohongshu"], "only_components": []}
    cur.execute(
        "INSERT INTO public.marketing_material_jobs"
        " (user_id, status, error_summary, billing_ref, input_fields_jsonb, cost_points)"
        " VALUES (%s,'generating',%s,%s,%s::jsonb,650) RETURNING id",
        (state["uid"], error_summary, "org:%d" % state["cid"],
         json.dumps({"_geo": geo}, ensure_ascii=False)))
    job_id = int(cur.fetchone()["id"])
    c.close()
    state["job_id"] = job_id
    return job_id


def _make_attempt(state, *, poll="succeeded", materialization="failed"):
    c = conn(state["dsn"])
    cur = c.cursor()
    cur.execute(
        "INSERT INTO public.marketing_material_generation_attempts"
        " (job_id, status, submit_state, poll_state, materialization_state,"
        "  resolution_state, component_id)"
        " VALUES (%s,'failed','submitted',%s,%s,'automatic','poster') RETURNING id",
        (state["job_id"], poll, materialization))
    aid = int(cur.fetchone()["id"])
    c.close()
    return aid


def _make_copy_asset(state, slot="professional_poster:copy"):
    """一件已交付的资产(`is_final=t`)。795 的真形就是只有这一件。"""
    c = conn(state["dsn"])
    cur = c.cursor()
    cur.execute(
        "INSERT INTO public.marketing_material_assets"
        " (job_id, asset_kind, bundle_slot, content_text, is_final, publish_allowed,"
        "  rights_confirmed) VALUES (%s,'copy',%s,%s,TRUE,1,0)",
        (state["job_id"], slot, '{"title":"t","body":"b","cta":"c"}'))
    c.close()


def _expected_components(state) -> int:
    """这个 job 的期望件数 —— 从**生产那条**计划函数问,不写死。

    🔴 写死的话,哪天 `_full_component_plan` 改了件数,
       「造满」那格会悄悄变成「没造满」,而它仍然绿(因为窄门会放行)。
    """
    from db import marketing_db
    from services.marketing.geo_factory import delivery_state
    job = marketing_db.get_job(int(state["job_id"]))
    return int(delivery_state(job)["expected"])

def _quarantine(state, *, lease_expired=True, status="unknown"):
    c = conn(state["dsn"])
    cur = c.cursor()
    lease = "NOW() - INTERVAL '1 hour'" if lease_expired else "NOW() + INTERVAL '1 hour'"
    cur.execute(
        "UPDATE organization_charge_links SET status=%s,"
        " external_side_effect_started_at=COALESCE(external_side_effect_started_at,NOW()),"
        " lease_until=" + lease + " WHERE id=%s", (status, state["cid"]))
    c.close()


def _facts(state):
    c = conn(state["dsn"])
    cur = c.cursor()
    cur.execute("SELECT status, actual_points FROM organization_charge_links WHERE id=%s",
                (state["cid"],))
    charge = dict(cur.fetchone())
    cur.execute("SELECT status, error_summary FROM public.marketing_material_jobs WHERE id=%s",
                (state["job_id"],))
    job = dict(cur.fetchone())
    cur.execute(
        "SELECT COALESCE(SUM(reserved_points),0) AS r FROM organization_spend_limits"
        " WHERE organization_id=(SELECT organization_id FROM organization_charge_links"
        "                        WHERE id=%s)", (state["cid"],))
    reserved = int(cur.fetchone()["r"])
    cur.execute(
        "SELECT COUNT(*) AS c FROM organization_audit_events"
        " WHERE action='billing.auto_release_undelivered' AND entity_id=%s",
        (str(state["cid"]),))
    audits = int(cur.fetchone()["c"])
    # 🔴 [返修二] 告警行数进 `_facts`,不是给某一格单加一条断言。
    #    Review 的毒 Pc:在停放分支的**拒绝路径**插一行 `_alert_reconcile_gave_up`
    #    ⇒ **7 条全绿**。因为原来那条 `..._does_not_alert_again` 的世界是
    #    「零交付 + unknown + 租约过期」= 窄门**成立**那格,`continue` 在前,
    #    拒绝路径根本没跑到;而真会重复告警的四格只断言「钱不动」,没人数告警。
    #    ⇒ **判据的名字声称了断言从没验的性质。**
    #    修法不是补一条,是让**每一格**的前后比对都带上它 —— 同一谓词一处实现。
    cur.execute("SELECT COUNT(*) AS c FROM ai_ops_alerts WHERE fingerprint=%s",
                ("geo_reconcile_gave_up:org:%d" % state["cid"],))
    alerts = int(cur.fetchone()["c"])
    c.close()
    return {"charge": charge, "job": job, "org_reserved": reserved,
            "audits": audits, "alerts": alerts}


def _still_recoverable(state) -> bool:
    """下一轮 `list_recoverable_geo_jobs` 还会不会选中它。

    🔴 这条是「终态是不是真终态」的判据。留在 `generating` 的 job 退出队列
       只是因为 error_summary 恰好不在选择器的 IN 列表里 —— 那是靠巧合退出。
    """
    from db import marketing_db
    return any(int(j["id"]) == state["job_id"]
               for j in marketing_db.list_recoverable_geo_jobs(limit=500))


def _run():
    from services.marketing import geo_factory
    return asyncio.run(geo_factory.reconcile_recoverable_geo_jobs(limit=500))


# ══════════════════════════════════════════════════════════════
# 矩阵:error_summary × charge.status × 交付情况
# ══════════════════════════════════════════════════════════════

def test_parked_job_reaches_the_gate_and_the_money_goes_back(world):
    """(provider_resolution_pending, unknown, 零交付)⇒ 真循环必须走到窄门。

    🔴 **修前这条必红** —— 那一支根本没有窄门,job 会走
       `recover_reconciled_geo_job` 拿到 `ignored`,钱一直冻着。
    """
    _make_job(world, error_summary="provider_resolution_pending")
    _make_attempt(world)
    _quarantine(world)
    before = _facts(world)
    assert before["charge"]["status"] == "unknown"
    assert _still_recoverable(world), "夹具没让它进恢复队列 —— 分母塌了,不是通过"

    _run()

    after = _facts(world)
    assert after["charge"]["status"] == "released", "窄门没够着(可达性缺陷复发)"
    assert int(after["charge"]["actual_points"]) == 0
    assert after["org_reserved"] < before["org_reserved"], "上限的 reserved 没归还"
    assert after["audits"] == 1
    assert after["job"]["status"] == "failed", after["job"]
    assert after["job"]["error_summary"] == "all_components_failed", after["job"]
    assert not _still_recoverable(world), "job 还在恢复队列里 —— 终态不是真终态"


def test_the_gate_succeeding_does_not_alert(world):
    """窄门**走成**那一格不许发告警(人已在 D2-a 那轮被叫过)。

    🔴 改名订正:原名 `..._does_not_alert_again` 声称的是「这一支不再告警」,
       但它的世界是零交付 + unknown + 租约过期 = 窄门**成立**那格,
       `continue` 在前,**拒绝路径根本没跑到**。
       Review 的毒 Pc(在拒绝路径插一行告警)⇒ 7 条全绿,把这件事抓了出来。
       现在名字只声称它真正验的那一格;拒绝路径由四格否定臂的
       `after == before`(含 `alerts`)承担。
    """
    _make_job(world, error_summary="provider_resolution_pending")
    _make_attempt(world)
    _quarantine(world)
    c = conn(world["dsn"]); cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM ai_ops_alerts WHERE fingerprint=%s",
                ("geo_reconcile_gave_up:org:%d" % world["cid"],))
    before = int(cur.fetchone()["n"]); c.close()
    _run()
    c = conn(world["dsn"]); cur = c.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM ai_ops_alerts WHERE fingerprint=%s",
                ("geo_reconcile_gave_up:org:%d" % world["cid"],))
    after = int(cur.fetchone()["n"]); c.close()
    assert after == before, "窄门这一支又发了一次告警"


def test_a_reserved_charge_in_the_parked_state_moves_nothing(world):
    """(provider_resolution_pending, **reserved**, 零交付)⇒ 窄门不成立,钱不动。

    零写入否定臂:只断言「没释放」不够,要证明库里一分钱没动。
    """
    _make_job(world, error_summary="provider_resolution_pending")
    _make_attempt(world)
    _quarantine(world, status="reserved")
    before = _facts(world)
    _run()
    after = _facts(world)
    # 🔴 整体比对,不逐字段挑 —— 逐字段挑的写法漏掉了 alerts,
    #    而「拒绝路径重复告警」正好只在 alerts 上显形。
    assert after == before, "窄门拒绝了,但库里有东西动了:%s -> %s" % (before, after)
    assert after["audits"] == 0


def test_a_live_lease_in_the_parked_state_moves_nothing(world):
    """(provider_resolution_pending, unknown, **租约未过期**)⇒ 钱不动。"""
    _make_job(world, error_summary="provider_resolution_pending")
    _make_attempt(world)
    _quarantine(world, lease_expired=False)
    before = _facts(world)
    _run()
    after = _facts(world)
    # 🔴 整体比对,不逐字段挑 —— 逐字段挑的写法漏掉了 alerts,
    #    而「拒绝路径重复告警」正好只在 alerts 上显形。
    assert after == before, "窄门拒绝了,但库里有东西动了:%s -> %s" % (before, after)
    assert after["audits"] == 0


def test_no_provider_success_in_the_parked_state_moves_nothing(world):
    """(provider_resolution_pending, unknown, provider **没**成功)⇒ 钱不动。

    那是另一类(钱可能压根没花),不该走这条窄门。
    """
    _make_job(world, error_summary="provider_resolution_pending")
    _make_attempt(world, poll="failed")
    _quarantine(world)
    before = _facts(world)
    _run()
    after = _facts(world)
    assert after == before, "窄门拒绝了,但库里有东西动了:%s -> %s" % (before, after)
    assert after["audits"] == 0


def test_the_real_795_shape_is_released_and_lands_as_partial(world):
    """🔴 795 的**真形**:provider 成功、图未物化、**文案已交付**。

    Deploy 只读取证:`marketing_material_assets` job 12 有 1 行
    `asset_kind=copy is_final=t publish_allowed=1`,正文完整。

    🔴 本条由 `..._is_refused_under_the_approved_wording` **改写**而来,不是删除。
       Owner 2026-09-06 拍板放宽:部分交付也退。依据不是新规则 ——
       仓里正常路径 `_settle_geo` 对 partial 走的就是
       `release_freeze("GEO 内容包部分成功整单免单")`。窄门只是让恢复路径
       用上同一条既有规则。

    两件事一起断言,缺一不可:
      · 钱回去了(charge released);
      · job 终态是 **succeeded / partial_free_release**,不是 failed ——
        一个已交付可用文案的 job 被标成失败,前端会说「生成失败」而用户手里
        明明有东西。**具体而错误的状态比没有状态更坏。**
    """
    _make_job(world, error_summary="provider_resolution_pending")
    _make_attempt(world)
    _make_copy_asset(world)          # ← 交了 1 件,还差图
    _quarantine(world)
    before = _facts(world)
    _run()
    after = _facts(world)
    assert after["charge"]["status"] == "released", "部分交付没被放行(放宽没生效)"
    assert after["org_reserved"] < before["org_reserved"]
    assert after["audits"] == 1
    assert after["job"]["status"] == "succeeded", after["job"]
    assert after["job"]["error_summary"] == "partial_free_release", after["job"]
    assert not _still_recoverable(world)


def test_a_fully_delivered_job_is_never_auto_released(world):
    """🔴 放宽之后**仍然守住**的那条线,也是真正会赔钱的那格:
    用户东西拿全了,还把钱退回去。

    与上一条成对:只证「partial 放」证明不了「full 拒」。
    没有这一对,把窄门写成「全都放」会有一半判据是绿的。
    """
    _make_job(world, error_summary="provider_resolution_pending")
    _make_attempt(world)
    for _ in range(_expected_components(world)):
        _make_copy_asset(world, slot="slot-%d" % _)
    _quarantine(world)
    before = _facts(world)
    _run()
    after = _facts(world)
    assert after == before, "交付齐了却被自动退款:%s -> %s" % (before, after)
    assert after["audits"] == 0

def test_the_original_pending_branch_still_works(world):
    """(billing_commit_pending, unknown, 零交付)⇒ 原格仍成立(回归)。"""
    _make_job(world, error_summary="billing_commit_pending")
    _make_attempt(world)
    _quarantine(world)
    _run()
    after = _facts(world)
    assert after["charge"]["status"] == "released"
    assert after["job"]["status"] == "failed"
    assert not _still_recoverable(world)


# ══════════════════════════════════════════════════════════════
# 元判据:防止下一次「逐字段挑」再漏一格
# ══════════════════════════════════════════════════════════════

def _cells_claiming_nothing_was_released(src: str) -> list:
    """分母从**断言自身的语义**派生:凡是断言 `after["audits"] == 0`
    (= 声称「没有发生释放」)的用例,都必须整体比对 `after == before`。"""
    marker = "after[" + chr(39) + "audits" + chr(39) + "] == 0"
    out = []
    for fn in ast.parse(src).body:
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
            continue
        body = ast.unparse(fn)          # unparse 统一成单引号,故只需比对一种形状
        if marker not in body:
            continue
        if "after == before" not in body:
            out.append(fn.name)
    return out


def test_every_negative_cell_compares_the_whole_fact_dict():
    """🔴 这条是**我自己的修复漏了一格**换来的。

    Review 的毒 Pc 抓出「拒绝路径重复告警没人验」之后,我把告警行数并进
    `_facts`,并把四格否定臂改成整体比对 —— 但改法是**手写一段固定文本去 replace**,
    而 `test_no_provider_success_...` 那一格的尾巴少一行 `org_reserved`,
    没匹配上、被漏掉。重下 Pc 时它**没有红**,四格只红了三格。

    ⇒ 手写的分母漏掉的那一项,不会让任何判据变红。
      所以这条机械枚举:**声称「没释放」就必须证明一个字段都没动**。

    🔴 分母从**断言自身的语义**派生,不按名字取(`*_moves_nothing` 之类命名约定
       和手写名单一样会漏),也不按「取了 before/after」取 ——
       后者会把**正向**那格(本来就该变)也算进来,我第一版就是这么误报的。
    """
    cells = _cells_claiming_nothing_was_released(
        io.open(__file__, encoding="utf-8").read())
    assert not cells, (
        "这些用例声称没释放,却只逐字段挑 —— 漏掉的字段(如 alerts)不会让它们变红:%s"
        % cells)


def test_the_meta_check_catches_the_bad_shape_and_spares_the_good_one():
    """元判据自证:合成源码喂进去 —— 坏的必须被点名,好的不许误伤。

    只证「抓得到坏的」证明不了「不会误伤好的」——
    误伤会逼后来人加白名单,而白名单一加就再没人回头看。
    """
    tail = '    assert after["audits"] == 0' + chr(10)
    head = ("    before = _facts(world)" + chr(10) +
            "    after = _facts(world)" + chr(10))
    bad = ("def test_x(world):" + chr(10) + head +
           '    assert after["charge"] == before["charge"]' + chr(10) + tail)
    good = ("def test_y(world):" + chr(10) + head +
            "    assert after == before" + chr(10) + tail)
    positive = ("def test_z(world):" + chr(10) + head +
                '    assert after["charge"]["status"] == "released"' + chr(10))
    assert _cells_claiming_nothing_was_released(bad) == ["test_x"], "抓不到坏的"
    assert _cells_claiming_nothing_was_released(good) == [], "误伤了好的"
    assert _cells_claiming_nothing_was_released(positive) == [], "误伤了正向那格"

def test_the_two_delivery_rules_are_known_to_diverge():
    """🔴 本仓有**两处**「交付齐了没有」的判定,算法**不同**:

      `delivery_state`             len(assets) >= expected
      `_reconcile_terminal_job`    len({去重后的 bundle_slot}) >= expected

    资产带 bundle_slot 时两者一致;**不带**时分岔(`effective_assets` 对无 slot 的
    资产按 id 建键,而去重成集合会把它们全压成同一个空串)。

    🔴 本次**没有**合并它们 —— 那是静默的行为变更,而 `_reconcile_terminal_job`
       我回归不了。这条锁不是「要求它们一致」,是**把分岔钉住**:免得后来人
       (包括我自己)以为几处是同一把尺,顺手统一一下就改了钱的方向。
       抽取仍是该做的事(#137),前置是那条路径可回归。
    """
    import inspect

    from services.marketing import geo_factory

    a = inspect.getsource(geo_factory.delivery_state)
    b = inspect.getsource(geo_factory._reconcile_terminal_job)
    assert "delivered >= expected" in a, "delivery_state 的判定形状变了 —— 请重锚"
    assert "bundle_slot" in b, (
        "_reconcile_terminal_job 的判定形状变了 —— 若两处已合并,请连同行为变更一起",
        "复审后删除本条,不要只改锚点")
    assert "delivery_state(" not in b, (
        "_reconcile_terminal_job 开始调 delivery_state 了 —— 两处已统一。"
        "这是**行为变更**(无 bundle_slot 的资产计数会变),必须复审后再删本条。")
