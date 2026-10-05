# -*- coding: utf-8 -*-
"""WO_252 · P0 · 两件事都属于「**它说成功,而客户被卡住**」。

① **联系方式静默丢弃**:`PUT /api/my-clients/{id}` 取不到 `client_profiles` 行
   就跳过 UPDATE,照样 `return {"success": True}`。生产实测:真品牌 **233/377**
   在 `client_profiles` 没有行;brand 19 当天六次 PUT 全 200,库里一个字段都没有。
   🔴 静默丢弃比报错坏得多 —— 报错用户会重试或找我们,静默丢弃**没人知道**,
      直到写文章时发现没有联系方式可引流。

② **闸死无出口**:`generate-link` / `resend` 未激活服务期直接 400,
   而提示语拿「最新那张报价」解释原因(brand 19 最新是 draft),
   于是对一个**付过款、已到期**的客户说「服务期还没激活 · 最新报价状态 draft」。
   🔴 一个说错原因的拒绝比不说原因更糟:代理照它去重走报价流程,白忙一轮还是发不出去。

本包连**真 PostgreSQL**:两件事都是 SQL 行为(建行 / 取哪张报价),
假对象复现出来的只是"我的假对象按我想的那样工作"。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
from datetime import date, datetime, timedelta

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")

B_NEW, B_HAS, B_CONTACT_ONLY = 960201, 960202, 960203       # ① 用
B_ACTIVE, B_EXPIRED, B_NEVER, B_NOSTART = 960301, 960302, 960303, 960304   # ② 用
ALL_BRANDS = [B_NEW, B_HAS, B_CONTACT_ONLY, B_ACTIVE, B_EXPIRED, B_NEVER, B_NOSTART]

CONTACTS = {
    "contact_phone": "13800000000",
    "contact_wechat": "wx_jieyang_atour",
    "contact_website": "https://example.com",
    "contact_address": "揭阳市榕城区揭阳中路 1 号",
}


def _conn():
    c = psycopg2.connect(DSN)
    c.cursor_factory = psycopg2.extras.RealDictCursor
    return c


@pytest.fixture(scope="module", autouse=True)
def _seed():
    """🔴 不许 `except: pass` —— 吞掉准备阶段的失败会让「没跑成」伪装成「判据红了」。"""
    if not DSN:
        pytest.fail("TEST_DATABASE_URL 未设置 —— 本包必须连真库")
    assert os.environ.get("DATABASE_URL") == DSN, (
        "DATABASE_URL 与 TEST_DATABASE_URL 不一致 —— 被测代码连的不是本包这个库")
    assert "_test" in DSN, "本包只许连测试库:%r" % DSN

    c = _conn()
    cur = c.cursor()
    cur.execute("SELECT to_regclass('brands') b, to_regclass('quotes') q,"
                " to_regclass('client_profiles') p")
    row = cur.fetchone()
    missing = [k for k in ("b", "q", "p") if row[k] is None]
    assert not missing, "测试库缺表 %s —— 先灌生产 schema 快照(见交付单)" % (missing,)

    cur.execute("DELETE FROM client_profiles WHERE brand_id = ANY(%s)", (ALL_BRANDS,))
    cur.execute("DELETE FROM quotes WHERE brand_id = ANY(%s)", (ALL_BRANDS,))
    cur.execute("DELETE FROM brands WHERE id = ANY(%s)", (ALL_BRANDS,))
    for bid, nm in ((B_NEW, "没有档案行的品牌"), (B_HAS, "已有档案行的品牌"),
                    (B_CONTACT_ONLY, "只改联系方式的品牌"),
                    (B_ACTIVE, "在期品牌"), (B_EXPIRED, "到期品牌"),
                    (B_NEVER, "从未付款品牌"), (B_NOSTART, "已付未起期品牌")):
        cur.execute("INSERT INTO brands (id, name) VALUES (%s,%s)", (bid, nm))
    cur.execute(
        "INSERT INTO client_profiles (id, name, brand_id) VALUES (%s,%s,%s)",
        ("p9602021", "已有档案行的品牌", B_HAS))

    today = date.today()
    # ② 四种服务期状态
    cur.execute(
        "INSERT INTO quotes (id, brand_id, status, service_status, service_start_date,"
        " service_end_date, paid_at) VALUES (%s,%s,'paid','active',%s,%s,%s)",
        (96031, B_ACTIVE, today - timedelta(days=10), today + timedelta(days=20),
         datetime.now() - timedelta(days=10)))
    # 到期那张**故意埋得深**:后面再插 6 张草稿,让它掉出「最近 5 张」——
    # 原实现 LIMIT 5 就是这么把付费那张看丢的。
    cur.execute(
        "INSERT INTO quotes (id, brand_id, status, service_status, service_start_date,"
        " service_end_date, paid_at) VALUES (%s,%s,'paid','expired',%s,%s,%s)",
        (96032, B_EXPIRED, today - timedelta(days=130), today - timedelta(days=100),
         datetime.now() - timedelta(days=130)))
    for i in range(6):
        cur.execute(
            "INSERT INTO quotes (id, brand_id, status, service_status) "
            "VALUES (%s,%s,'draft','pending')", (96040 + i, B_EXPIRED))
    cur.execute(
        "INSERT INTO quotes (id, brand_id, status, service_status) "
        "VALUES (%s,%s,'draft','pending')", (96050, B_NEVER))
    cur.execute(
        "INSERT INTO quotes (id, brand_id, status, service_status, paid_at) "
        "VALUES (%s,%s,'paid','pending',%s)",
        (96060, B_NOSTART, datetime.now() - timedelta(days=1)))
    c.commit()
    c.close()
    yield
    c = _conn(); cur = c.cursor()
    cur.execute("DELETE FROM client_profiles WHERE brand_id = ANY(%s)", (ALL_BRANDS,))
    cur.execute("DELETE FROM quotes WHERE brand_id = ANY(%s)", (ALL_BRANDS,))
    cur.execute("DELETE FROM brands WHERE id = ANY(%s)", (ALL_BRANDS,))
    c.commit(); c.close()


# ══════════════════════════════════════════════════════════════════
# ① 联系方式必须真的落库
# ══════════════════════════════════════════════════════════════════

def _upsert(brand_id, updates, fallback_name=None):
    from api.brand_api import _upsert_client_profile
    c = _conn()
    try:
        cur = c.cursor()
        got = _upsert_client_profile(cur, brand_id, dict(updates),
                                     fallback_name=fallback_name)
        c.commit()
        return got
    finally:
        c.close()


def _profile_rows(brand_id):
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute("SELECT * FROM client_profiles WHERE brand_id = %s", (brand_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        c.close()


def test_a_brand_without_a_profile_row_gets_one_and_the_contacts_land():
    """🔴 本单①的全部意义:没有档案行**也要存进去**,而不是假装成功。"""
    got = _upsert(B_NEW, CONTACTS, fallback_name="没有档案行的品牌")
    assert got == sorted(CONTACTS), "persisted 不是这四个字段:%s" % (got,)
    rows = _profile_rows(B_NEW)
    assert len(rows) == 1, "应当正好建出一行,实得 %d 行" % len(rows)
    for k, v in CONTACTS.items():
        assert rows[0][k] == v, "%s 没落库(得到 %r)" % (k, rows[0][k])


def test_an_existing_row_is_updated_not_duplicated():
    """🔴 反向对照:有行的品牌**行为不变** —— 更新那一行,不许再建一行。

    少了这一条,把 helper 写成"永远 INSERT"也能让上面那条绿。
    """
    _upsert(B_HAS, {"contact_phone": "13900000000"})
    rows = _profile_rows(B_HAS)
    assert len(rows) == 1, "变成了 %d 行 —— 重复建行" % len(rows)
    assert rows[0]["contact_phone"] == "13900000000"
    assert rows[0]["name"] == "已有档案行的品牌", "原有字段被冲掉了"


def test_contacts_only_insert_does_not_explode_on_not_null_name():
    """🔴 `client_profiles.name` 是 NOT NULL 且无默认值。

    "只改联系方式"是最常见的请求,它不带 name。没有兜底的话,
    "修好了静默丢弃"会变成"建行时 NotNullViolation 500" —— 换一种失败,不是修好。
    """
    got = _upsert(B_CONTACT_ONLY, CONTACTS, fallback_name="只改联系方式的品牌")
    assert len(got) == 4
    rows = _profile_rows(B_CONTACT_ONLY)
    assert len(rows) == 1
    assert rows[0]["name"] == "只改联系方式的品牌", "兜底名字没用上:%r" % rows[0]["name"]


def test_nothing_to_persist_returns_an_empty_list_not_a_lie():
    """🔴 一列都没落 ⇒ `persisted == []`。空列表是**如实说没写**,不是失败。"""
    assert _upsert(B_NEW, {}) == []


def test_zero_rows_written_is_not_allowed_to_read_as_success():
    """🔴 本单要消灭的就是那种"成功":写了 0 行还回 success。

    这里直接验 helper 的牙:给一个不存在的 brand 去 UPDATE 是不可能的,
    所以造一个"有行但 UPDATE 命不中"的局面 —— 用 is_deleted 把行藏起来后
    再传一个不存在的 brand_id,它会走 INSERT;真正能验的是
    **rowcount 检查这条语句在**,且抛的是 500 而不是静默返回。
    """
    import inspect
    from api.brand_api import _upsert_client_profile
    src = inspect.getsource(_upsert_client_profile)
    assert "cur.rowcount" in src, "没有检查实际写了几行"
    assert "PROFILE_PERSIST_FAILED" in src, "0 行时没有明确的失败码"
    tree = ast.parse(src.lstrip())
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert raises, "rowcount 不对时没有 raise —— 那就还是静默"


@pytest.mark.parametrize("fn_name", ["update_client", "_update_my_brand_impl"])
def test_both_handlers_go_through_the_upsert_and_report_persisted(fn_name):
    """🔴 两端都要走同一个 helper,并把 `persisted` 交出去。

    ⚠️ 分母:工单点名的是**代理端** `update_client`,但自助端
       `_update_my_brand_impl` 是同一类写入(它会建行,却**不给 name 兜底**)。
       点名的实例不是缺陷的类 —— 两个一起收。
    """
    tree = ast.parse((REPO / "api" / "brand_api.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    assert fn is not None, "找不到 %s" % fn_name

    # 🔴 只钉"调了"不够:注毒可以写成 `persisted = [] or _upsert(...)`,
    #    调用还在而结果不决定任何事。钉:结果被 Assign **直接**接住。
    assigned = [n for n in ast.walk(fn)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", None) == "_upsert_client_profile"]
    assert assigned, "%s 没有**直接**把 _upsert_client_profile 的结果接住" % fn_name

    returns = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Return)]
    success = [r for r in returns if "'success': True" in r]
    assert success, "%s 没有成功返回?" % fn_name
    for r in success:
        assert "persisted" in r, "%s 有一个成功返回没带 persisted:%s" % (fn_name, r[:80])


def test_no_brand_keyed_profile_writer_skips_creating_the_row():
    """🔴 分母锁(按后果定域):凡是**按 brand_id 找行再写 client_profiles** 的地方,
    都必须有建行路径 —— 否则它就是下一个"静默丢弃"。

    按 `profile_id` 写的不在此列(那种调用方手里已经有行了)。
    """
    bad = []
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "node_modules" in rel:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "UPDATE client_profiles" not in src:
            continue
        try:
            tree = ast.parse(src, rel)
        except Exception:
            continue
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            body = ast.unparse(fn)
            if "UPDATE client_profiles" not in body:
                continue
            if "client_profiles WHERE brand_id" not in body:
                continue        # 按 profile_id 写的,不是这一类
            if ("INSERT INTO client_profiles" not in body
                    and "_upsert_client_profile" not in body):
                bad.append("%s::%s" % (rel, fn.name))
    assert not bad, (
        "这些地方按 brand_id 写 client_profiles 却没有建行路径(没行就静默丢):%s" % (bad,))


# ══════════════════════════════════════════════════════════════════
# ② 服务期:不拦 + 出声 + 出口
# ══════════════════════════════════════════════════════════════════

def _notice(brand_id):
    from api.m3_material_confirm_api import _service_period_notice
    return _service_period_notice(brand_id)


def test_an_active_brand_reads_active():
    n = _notice(B_ACTIVE)
    assert n["state"] == "active"
    assert n["service_end_date"] and n["paid_at"]


def test_an_expired_brand_is_not_mistaken_for_never_activated():
    """🔴 本单②的全部意义。brand 19 的真实形状:付过款、已到期,
    而**最新那张报价是 draft**。原实现取最新那张解释原因,于是说
    「服务期还没激活 · 最新报价状态 draft」—— 把"该续费"说成"该走流程"。

    这里故意在到期那张之后塞了 6 张草稿,让它掉出「最近 5 张」——
    原实现的 `LIMIT 5` 就是这么把付费那张看丢的。
    """
    n = _notice(B_EXPIRED)
    assert n["state"] == "expired", "得到 %r —— 又把到期读成没付款了" % n["state"]
    assert n["paid_at"], "付款时间丢了 —— 那就还是看不出他付过"
    assert n["service_end_date"], "到期日丢了 —— 提示语就说不出到期时间"
    assert "到期" in n["message"] and "续费" in n["message"], n["message"]


def test_a_never_paid_brand_says_so_without_blocking():
    n = _notice(B_NEVER)
    assert n["state"] == "never_paid"
    assert n["paid_at"] is None and n["service_end_date"] is None
    assert "仍可以发" in n["message"], "没告诉代理「确认链还能发」—— 那还是个死路"


def test_paid_but_not_started_is_not_called_expired():
    """🔴 工单只写了三态,生产上有第四种:付过款但服务期还没起
    (`service_end_date` 为空,销售端还没「标已付」落日期)。

    硬塞进 expired 会让页面叫人**再付一次** —— 那正是本单要治的"话术说错状态"。
    """
    n = _notice(B_NOSTART)
    assert n["state"] == "paid_not_started", "得到 %r" % n["state"]
    assert n["paid_at"], "他明明付过"
    assert "再" not in n["message"].replace("再发", ""), "不该暗示重新付款:%s" % n["message"]


@pytest.mark.parametrize("brand_id", [B_ACTIVE, B_EXPIRED, B_NEVER, B_NOSTART])
def test_every_state_offers_an_exit(brand_id):
    """🔴 Owner 原话「一定不能阻塞,出现问题一定要有出口」。

    四种状态都要给 `renew_url` —— 包括在期的那种:出口是否**显示**由前端按
    state 决定,但后端不许某一态根本没有出口可给。
    """
    n = _notice(brand_id)
    assert n.get("renew_url"), "state=%s 没有出口" % n["state"]
    assert n["message"], "state=%s 没有给人看的话" % n["state"]


def test_the_blocking_helpers_are_gone():
    """🔴 分母锁:两个硬拦 helper **全仓 0 处** —— 留着就会被下一个端点接上。

    工单写的是「调用点恰 3 处且都走新行为」;我改成了**删掉它们**,
    所以这条锁按"0 处"判(更强:0 处不可能有人还在拦)。偏离已写进交付单。
    """
    hits = []
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "node_modules" in rel:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for name in ("_require_service_period_active", "_check_service_period_active"):
            if name in src:
                hits.append("%s:%s" % (rel, name))
    assert not hits, "硬拦 helper 还在:%s" % (hits,)


@pytest.mark.parametrize("fn_name", ["generate_link", "resend"])
def test_every_success_return_carries_the_notice(fn_name):
    """🔴 **每一个**成功返回都要带 service_notice。

    两个端点各有两条返回路径(复用 token / 新建 token)。
    漏掉一条,前端在那条路径上就什么都看不到 —— 而它看起来完全正常。
    """
    tree = ast.parse((REPO / "api" / "m3_material_confirm_api.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    assert fn is not None, "找不到 %s" % fn_name
    returns = [ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) >= 2, "%s 只有 %d 条返回路径?判据前提变了" % (fn_name, len(returns))
    missing = [r[:70] for r in returns if "service_notice" not in r]
    assert not missing, "%s 有返回没带 service_notice:%s" % (fn_name, missing)

    assigned = [n for n in ast.walk(fn)
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "id", None) == "_service_period_notice"]
    assert assigned, "%s 没有**直接**把 _service_period_notice 的结果接住" % fn_name


@pytest.mark.parametrize("fn_name", ["generate_link", "resend"])
def test_the_service_state_does_not_gate_anything(fn_name):
    """🔴 「不拦」本身必须被钉住,不能只钉"带了 notice"。

    删掉两个 helper 之后,再写一行
    ``if service_notice["state"] != "active": raise HTTPException(400, ...)``
    就能把闸原样装回来 —— 四条返回照样带 notice,名字锁也查不到,
    **所有判据全绿而客户照样被卡住**。
    所以这里钉的是:`service_notice` **不许出现在任何分支条件里**,
    它只能被原样交出去。
    """
    tree = ast.parse((REPO / "api" / "m3_material_confirm_api.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == fn_name), None)
    assert fn is not None, "找不到 %s" % fn_name
    gated = []
    for n in ast.walk(fn):
        if isinstance(n, (ast.If, ast.IfExp, ast.While)):
            if "service_notice" in ast.unparse(n.test):
                gated.append(ast.unparse(n.test)[:70])
    assert not gated, (
        "%s 里服务期状态又变成了判断条件(闸装回来了):%s" % (fn_name, gated))


def test_the_charging_gates_were_not_touched():
    """🔴 工单边界:只放开服务期这道闸,**不碰任何扣费闸**。

    这一格钉住"本文件里没有冒出扣费相关调用" —— 放开一道闸时顺手放开另一道,
    是这类改动最容易出的事故,而它在功能测试里完全看不出来。
    """
    src = (REPO / "api" / "m3_material_confirm_api.py").read_text(encoding="utf-8")
    for token in ("deduct_points", "refund_points", "freeze_points", "charge_"):
        assert token not in src, "扣费相关符号出现在本文件:%s" % token
