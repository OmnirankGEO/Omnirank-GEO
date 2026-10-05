"""返工链 3b/4/5/6 · **接线**判据(不是函数判据)。

## 为什么这一份必须存在

上一轮整包 PASS 被撤销的头号根因,一句话:**模块层真实、执行链没接通**。
`durable_worker` 的租约/CAS 全对,`artifact_prepare.mark_ready/failed/unknown`
全对,可是仓里**没有任何生产调用者**会去调它们。而当时 481 条判据全绿 ——
因为它们验的都是"这个函数对不对",没有一条在问"这个函数会不会被调到"。

所以本文件的判据形状与其它文件不同:

  · **调用者存在性**:每个承重 helper 必须有非测试的生产调用者(census 求差);
  · **调用位置**:闸必须在副作用之前 —— 用 AST 比语句次序,不靠 code review;
  · **真库行为**:索引谓词、订单身份这类只有 PG 才答得出的事,打真库。

🔴 反向对照贯穿全文:每条「必须有」都配一条「拿掉就必须红」。
   没有反向对照的存在性判据是恒真的 —— 那正是上一轮的病。
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
API = REPO / "api" / "geo_image_note_api.py"
#: [WO-B ② 2026-08-20 · 搬家改断言,不退役] publish-batch 的 §8.2 全序
#: (claim → 审批门 → 资格/定价 → 总价锁 → artifact/锁定 revision + 广告法闸 →
#: 逐项 freeze → 建单)从 handler 闭包**搬进**了这个模块,因为小榜五阶段的
#: execute adapter 要接的就是同一条产线(两处各写一遍 = 两份资金链)。
#: 命题一个字没变,只是**长在别的文件上**了 —— 所以锚跟着搬,并且下面额外加一条
#: 「handler 真的调了它」的接线锁:没有那一条,闸可以整条躺在一个没人调的函数里。
CORE = REPO / "services" / "geo_douyin" / "publish_batch_core.py"
WORKER = REPO / "services" / "geo_douyin" / "contract_worker.py"
SCHEDULER = REPO / "api" / "scheduler.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _production_callers(symbol: str, *, form: str = "call") -> set[str]:
    """全仓找 `symbol` 的**非测试**引用文件。

    🔴 结构锚用符号名 + `git grep`,而不是 import 图:测行为的调用方
       可能只在字符串里出现(monkeypatch),import 图看不见它们。
       本仓 2026-08-17 刚吃过一次"只按 import 算受影响面"的亏。

    🔴 `*.py` 之外还要扫 `*.ts/*.tsx`:前端也有"接了但没人调"的死法 ——
       「后端建了端点、前端从不请求」与「函数没人调」是同一件事。

    🔴 [自证修正] 第一版用**裸符号名**做 pattern —— **变异存活**:
       我把 `fetchPublishCommand(commandId)` 换成 `null as any`,
       文件顶部的 `import { fetchPublishCommand }` 仍然命中 ⇒ 判据恒真。
       「import 存在 ≠ 被调用」正是 census 要防的那件事本身。
       所以默认 `form='call'`,pattern 带 `(`;
       只有**作为回调传递**(`add_job(run_tick, ...)`)的才用 `form='ref'` ——
       那种形态本身就是接线,没有 `(`。
    """
    pattern = symbol + r"\s*\(" if form == "call" else symbol
    out = subprocess.run(["git", "grep", "-l", "-E", pattern,
                          "--", "*.py", "*.ts", "*.tsx"],
                         cwd=REPO, capture_output=True, text=True)
    files = {line.strip() for line in out.stdout.splitlines() if line.strip()}
    return {f for f in files
            if not f.startswith("tests/") and not f.startswith("scripts/")}


# ---------------------------------------------------------------------------
# 0. 生产调用者 census —— 「死函数 = NO-GO」
# ---------------------------------------------------------------------------

#: 承重 helper → `(必须出现在哪个生产文件, 引用形态)`。
#: 形态 `call` = 必须有**调用点**;`ref` = 作为回调被传递(那本身就是接线)。
WIRED_HELPERS = {
    # 三条链的启动者本身必须被调度器挂上,否则整套租约机制是摆设。
    # 形态是 `ref`:它被当作 callable 传给 add_job,不带括号。
    "run_tick": ("api/scheduler.py", "ref"),
    # 素材三态收尾:上一轮它们零生产调用者,artifact 永远停在 preparing
    "mark_ready": ("services/geo_douyin/contract_worker.py", "call"),
    "mark_failed": ("services/geo_douyin/contract_worker.py", "call"),
    "mark_unknown": ("services/geo_douyin/contract_worker.py", "call"),
    # 租约原语:上一轮同样零生产调用者
    "claim_next_task": ("services/geo_douyin/contract_worker.py", "call"),
    "mark_external_start": ("services/geo_douyin/contract_worker.py", "call"),
    "finish_task": ("services/geo_douyin/contract_worker.py", "call"),
    "reconcile_stuck_external_tasks": ("services/geo_douyin/contract_worker.py", "call"),
    # 真供应商链:新链必须走现役下单器,不另造第二条投放链
    "_submit_short_video_order": ("services/geo_douyin/contract_worker.py", "call"),
    # 幂等结果回写:没有它,回放返回空 batch_id
    "record_response": ("api/geo_image_note_api.py", "call"),
    # command 投影三件套:上一轮它们也是零生产调用者
    "project_command_status": ("api/geo_image_note_api.py", "call"),
    "is_retryable": ("api/geo_image_note_api.py", "call"),
    "next_action_for": ("api/geo_image_note_api.py", "call"),
    # 前端必须真的去轮询,否则端点建了也白建(与 worker 同一种死法)
    # 已删（Review #161）：fetchPublishCommand 的生产调用点
    # ImageNotePanel.tsx 随 #150 §4 删除；其余 spec 不变。
    # 三道闸
    # [WO-B ② 2026-08-20] 搬到产线模块了(见 CORE 注释)。锚跟着搬。
    "assert_outbound_clean": ("services/geo_douyin/publish_batch_core.py", "call"),
    "require_approval": ("api/geo_image_note_api.py", "call"),
    "load_slot_authority": ("api/geo_image_note_api.py", "call"),
}


@pytest.mark.parametrize("symbol,spec", sorted(WIRED_HELPERS.items()))
def test_helper_has_a_real_production_caller(symbol, spec):
    """🔴 每个承重 helper 必须有**生产**调用者,而且是预期的那一个。

    只断言"有人引用"不够 —— 定义文件自己也会命中。这里要求那个**具体的**
    生产文件出现在引用集合里,因为"被谁调"本身就是接线事实的一部分。
    """
    expected_caller, form = spec
    callers = _production_callers(symbol, form=form)
    assert expected_caller in callers, (
        f"`{symbol}` 没有出现在预期的生产调用者 {expected_caller} 里。\n"
        f"当前非测试引用:{sorted(callers)}\n"
        "「模块真实但没人调」正是上一轮整包被撤 PASS 的形态。")


def test_census_denominator_is_not_empty():
    """判别力自证:census 用的 `git grep` 真的能找到东西。

    没有这一条,上面那一组在 git 不可用时会**整组恒假**地报错,
    或者(更糟)在某个改动让 grep 恒空时变成一堆看起来很具体的失败。
    """
    assert _production_callers("run_tick"), "结构锚零命中 —— census 没有分母"
    assert not _production_callers("__symbol_that_should_not_exist__"), \
        "不存在的符号也命中了 —— grep 口径有问题,census 恒真"


def test_command_endpoints_exist_and_do_not_collide():
    """🔴 P0-04 后半:command GET / retry 两个端点必须存在。

    上一轮它们**不存在** —— 前端只能永远显示提交那一刻的 `accepted`,
    部分失败在界面上根本不出现。
    """
    src = _src(API)
    for route in ('@router.get("/api/meijiehezi/image-notes/commands/{command_id}")',
                  '@router.post("/api/meijiehezi/image-notes/commands/{command_id}/retry")'):
        assert route in src, f"缺端点:{route}"
    # 反向:两条路径不许互相吞掉(FastAPI 按注册序匹配,GET 的 `{command_id}`
    # 若写成 `{command_id:path}` 会把 retry 也吃掉)
    assert "{command_id:path}" not in src, (
        "command_id 用了 path 转换器 —— 它会把 /retry 一起吃进去")


def test_retry_reuses_the_same_retryable_projector():
    """🔴 可重试判定只能有**一份**。

    读端(GET)告诉用户"这条可以重投",写端(retry)必须用同一个判定 ——
    另写一遍必然漂移,而漂移方向是"读端说不能、写端放行" ⇒ 重复外调。
    """
    tree = ast.parse(_src(API))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "api_retry_publish_command")
    body = ast.unparse(fn)
    assert "is_retryable(" in body, "retry 端点没有复用 is_retryable —— 判定分叉了"
    # 反向:不许在这里自己按 state 拼一套
    assert "'failed'" not in body or "is_retryable(" in body, body


def test_scheduler_registers_worker_unconditionally():
    """🔴 worker 必须挂在**无条件**注册那一段。

    `setup_schedule` 受 `auto_monitor_enabled` 门控(prod 实测 = 0)。
    挂在那里 = 注册了但永远不触发 —— 比"没接线"更难发现,因为
    job 列表里看得见它。所以判据打的是**挂在哪个函数里**。
    """
    tree = ast.parse(_src(SCHEDULER))
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "register_v32_core_tasks"),
                  None)
    assert target is not None, "register_v32_core_tasks 不见了 —— 无条件注册入口没了"
    body = ast.unparse(target)
    # 🔴 [自证修正] 第一版用裸子串 `"geo_image_note_contract_worker" in body`。
    #    变异实验(把 id 改成 `..._DISABLED`)**存活** —— 因为改名后的串
    #    仍然**包含**原串。同一族错误在本仓已有专门教训
    #    (「机密串不能是允许标签的子串」)。判 id 只能打**整个字面量**。
    assert "id='geo_image_note_contract_worker'" in body, (
        "图文 worker 没有以确切 job id 注册在无条件那一段;"
        "挂在 setup_schedule 里等于永不触发,改名等于换了个 job")
    assert "run_tick" in body, "注册的不是 contract_worker.run_tick"
    assert "IntervalTrigger" in body, "worker 不是周期触发 —— 只跑一次等于没跑"


# ---------------------------------------------------------------------------
# 1. 闸的**位置** —— 副作用之前
# ---------------------------------------------------------------------------

def _statement_order(func_name: str, needles: list[str],
                     source: pathlib.Path = API) -> dict[str, int]:
    """在某个 handler 的源码里,给每个片段第一次出现的**行号**。

    🔴 用行号而不是"在不在":闸的全部意义在于**先后**。
       「有没有调用」是上一轮就有的判据形状,它挡不住"放晚了一格"。

    🔴 [自证修正] 第一版用裸符号名做 needle,结果匹配到了 handler 顶部的
       `from ... import freeze_one_item` —— 于是「闸在冻结之后」被误报。
       **测量仪器必须与被测对象同口径**:要测调用次序,就只能匹配**调用点**,
       所以调用类 needle 一律带 `(`。这个错值得留在注释里:
       它和上一轮"验标记不验接线"是同一族 —— 量错了东西,结论再自洽也没用。
    """
    tree = ast.parse(_src(source))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == func_name)
    lines = ast.unparse(fn).splitlines()
    out: dict[str, int] = {}
    for i, line in enumerate(lines):
        for needle in needles:
            if needle in line and needle not in out:
                out[needle] = i
    return out


def test_approval_gate_precedes_every_side_effect_in_create_batch():
    """§7.3:审批门在容量 / 冻结 / 建单之前。"""
    order = _statement_order("api_create_batch", [
        "require_approval(", "claim_slot(", "freeze_one_item(",
        "INSERT INTO geo_douyin_production_batches",
    ])
    assert "require_approval(" in order, "create-batch 没有审批门"
    for later in ("claim_slot(", "freeze_one_item(",
                  "INSERT INTO geo_douyin_production_batches"):
        assert later in order, f"{later} 在 handler 里找不到 —— 判据失去分母"
        assert order["require_approval("] < order[later], (
            f"审批门出现在 {later} **之后** —— 放晚一格就不是闸:"
            "冻结之后被拒还要再退一次,建单之后被拒等于事后追认")


def test_approval_and_legal_gates_precede_side_effects_in_publish_batch():
    """§7.3 + §7(P0-14):审批门与广告法闸都在建单 / 冻结 / 外调之前。"""
    # 🔴 接线锁先行:闸的次序对不对,只有在「这条产线真的被 handler 调到」时才有意义。
    #    没有这一条,四道闸可以整条躺在一个没人调的函数里而本判据全绿。
    assert "materialize_publish_batch(" in _src(API),         "handler 没有调用产线函数 —— 闸的次序判据失去意义(死函数形态)"
    order = _statement_order("materialize_publish_batch", [
        "require_approval(", "assert_outbound_clean(",
        "materialize_command(", "freeze_one_item(",
    ], source=CORE)
    for gate in ("require_approval(", "assert_outbound_clean("):
        assert gate in order, f"publish-batch 缺 {gate}"
        for later in ("materialize_command(", "freeze_one_item("):
            assert later in order, f"{later} 找不到 —— 判据失去分母"
            assert order[gate] < order[later], (
                f"{gate} 出现在 {later} 之后 —— 闸在副作用之后就不是闸")


def test_legal_gate_reads_the_stored_copy_not_the_request_body():
    """🔴 扫的必须是**会被发出去的那一版**。

    扫请求体等于扫一个不会外发的字符串:客户端可以发一份干净的文案、
    库里躺着违规的那一版,而真正提交给渠道的是库里那一版。

    🔴 [第 3 棒 · Codex R2 P0-6 · **搬家改断言,不退役**] 原断言打的是
       `FROM geo_douyin_posts` —— 命题("扫库里那一版")仍然成立,
       但**那一版换了地方**:外发的是**锁定的 revision**,不是 post 的可变列。
       两者在"用户改了文案又没重新准备素材"这一刻就不是同一份 ⇒「审 A 发 B」。
       所以断言跟着搬到 `geo_douyin_post_revisions`,并**加强**一条:
       闸不许再去读 post 的 `body_text`(那是可变列)。
       行为面的正向/反向对照在 `test_r2_seams_pg16.py::test_legal_gate_blocks_the_exact_outbound_copy`。
    """
    assert "materialize_publish_batch(" in _src(API),         "handler 没有调用产线函数 —— 本判据失去分母"
    order = _statement_order("materialize_publish_batch",
                             ["assert_outbound_clean(", "FROM geo_douyin_post_revisions"],
                             source=CORE)
    assert "FROM geo_douyin_post_revisions" in order, \
        "广告法闸没有从**锁定的成品版本**取文案"
    assert order["FROM geo_douyin_post_revisions"] < order["assert_outbound_clean("], \
        "先扫后取 —— 那扫的不是要发的那一版"
    body = _src(CORE)
    handler = body[body.index("def materialize_publish_batch"):]
    handler = handler[:handler.index("\n@router.")] if "\n@router." in handler else handler
    assert "body_text FROM geo_douyin_posts" not in handler, \
        "闸又去读 post 的可变正文了 —— 那一列会被下一次重做整列覆盖"


def test_the_order_instrument_can_actually_distinguish():
    """🔴 仪器自证:次序判据必须真的分得出先后。

    第一版仪器把 import 行也算进去,于是"闸在前"这件事测的是 import 次序。
    这里用两个**已知次序**的片段验证仪器本身 —— 没有它,上面所有
    `assert a < b` 可能只是在比较两个无意义的数。
    """
    order = _statement_order("api_create_batch", ["_user(request)", "conn.commit()"])
    assert set(order) == {"_user(request)", "conn.commit()"}, order
    assert order["_user(request)"] < order["conn.commit()"], (
        "仪器分不出先后 —— 上面所有次序判据失去判别力")


# ---------------------------------------------------------------------------
# 2. 广告法闸的行为 + 反向对照
# ---------------------------------------------------------------------------

def test_legal_gate_blocks_absolute_terms():
    from services.geo_douyin.legal_gate import LegalGateBlocked, assert_outbound_clean

    with pytest.raises(LegalGateBlocked) as exc:
        assert_outbound_clean({"title": "全国第一的装修公司", "body_text": ""})
    assert exc.value.hits, "抛了但没带命中明细 —— 无法局部修复"
    assert exc.value.pack_version, "没带目录版本 —— 事后追责答不出按哪版放行"
    assert all("start" in h and "field" in h for h in exc.value.hits), exc.value.hits


def test_legal_gate_does_not_false_positive_on_safe_context():
    """🔴 反向对照:安全语境不许误伤。

    没有这一条,「把闸调成拒绝一切」也是绿的 —— 而那会让所有发布都发不出去。
    「第一步」是签发目录里裸「第一」的经典误伤面(见 legal_context 病历)。
    """
    from services.geo_douyin.legal_gate import scan_outbound

    result = scan_outbound({"title": "装修第一步该做什么", "body_text": "第一季度复盘"})
    assert not result["hits"], f"安全语境被误判成绝对化用语:{result['hits']}"


def test_legal_gate_scans_fields_separately_not_concatenated():
    """🔴 拼接会在字段边界造出原文里**不存在**的违规词。"""
    from services.geo_douyin.legal_gate import scan_outbound

    # 单看每个字段都干净;拼起来才会出现"第一"
    result = scan_outbound({"title": "本季度做到全国第", "body_text": "一家门店落地"})
    assert not result["hits"], (
        "跨字段拼出了原文没有的违规词 —— 那是凭空捏造的命中:%s" % result["hits"])


# ---------------------------------------------------------------------------
# 3. 发布对象身份(P0-05)· 真库
# ---------------------------------------------------------------------------

PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")
MIGRATIONS = [
    REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql",
    REPO / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    REPO / "db" / "migration_036_publication_stage_strict_source_2026_08_18.sql",
]


@pytest.fixture(scope="module")
def pg():
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extras import RealDictCursor

    if not DSN or not PROD_SCHEMA.is_file():
        pytest.skip("需要 TEST_DATABASE_URL 与生产 schema 夹具")
    admin_dsn = DSN.rsplit("/", 1)[0] + "/postgres"
    name = "geoimg_chain456_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    admin.cursor().execute('CREATE DATABASE "' + name + '"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    c.execute("SET search_path = public")
    for mig in MIGRATIONS:
        c.execute(mig.read_text(encoding="utf-8"))
    try:
        yield c
    finally:
        conn.close()
        admin = psycopg2.connect(admin_dsn)
        admin.autocommit = True
        admin.cursor().execute('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
        admin.close()


def test_live_revision_root_index_uses_or_not_and(pg):
    """🔴 P0-11:谓词必须是「非终态 **或** 仍有效」。

    AND 版在最常见的成功路径上失效:一条 published + active 的项落了
    `terminal_at` 之后就脱离唯一索引,同一 revision 可以再建第二个有效根。
    """
    pg.execute("SELECT pg_get_indexdef(c.oid) AS def FROM pg_class c "
               " WHERE c.relname = 'uq_mhz_item_live_revision_root'")
    row = pg.fetchone()
    assert row is not None, "唯一索引不存在 —— 一篇一账号没有物理保证"
    definition = " ".join(str(dict(row)["def"]).split())
    # 🔴 [自证修正] 第一版写的是 `"OR" in definition` —— **变异存活**:
    #    旧 AND 版的谓词里也有 OR(`availability IS NULL OR availability='active'`),
    #    所以那条断言恒真。判"是不是 OR 版"必须打**那个具体的析取**,
    #    即 `terminal_at IS NULL` 与 availability 之间是 OR 而不是 AND。
    #    "机密串不能是允许标签的子串"是同一族错误,我在这里又犯了一次。
    assert "(terminal_at IS NULL) OR (availability IS NULL)" in definition, (
        f"谓词不是 OR 版(P0-11 未修 / 被改回 AND):{definition}")
    assert "(terminal_at IS NULL) AND" not in definition, (
        f"terminal_at 与 availability 之间仍是 AND:{definition}")


def test_second_terminal_root_is_rejected_by_the_index(pg):
    """行为面:同一 revision 的第二个「已终结但仍 active」根必须被拒。

    这是 P0-11 的真实故障场景 —— 旧 AND 版在这里会**放行**。
    """
    psycopg2 = pytest.importorskip("psycopg2")

    pg.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title,"
               " status) VALUES (42, -1, %s, 'pending') RETURNING id", ("夹具订单",))
    order_id = int(dict(pg.fetchone())["id"])
    rev = 987654
    for _ in range(1):
        pg.execute(
            "INSERT INTO mhz_publish_order_items "
            " (order_id, user_id, media_id, media_name, status, source_post_revision_id,"
            "  terminal_at, availability) "
            "VALUES (%s, 42, 1, '夹具账号', 'published', %s, now(), 'active')",
            (order_id, rev))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        pg.execute(
            "INSERT INTO mhz_publish_order_items "
            " (order_id, user_id, media_id, media_name, status, source_post_revision_id,"
            "  terminal_at, availability) "
            "VALUES (%s, 42, 2, '夹具账号', 'published', %s, now(), 'active')",
            (order_id, rev))


def test_034_replay_does_not_rebuild_the_live_root_index(pg):
    """🔴 重放安全:prestart **每次部署无条件重放全部迁移**。

    裸 `DROP INDEX` + `CREATE` 会让每一次部署都出现一段「唯一约束不存在」的
    窗口。用 oid 是否变化来判 —— 没变 = 没被 DROP 过。
    """
    pg.execute("SELECT oid FROM pg_class WHERE relname='uq_mhz_item_live_revision_root'")
    before = int(dict(pg.fetchone())["oid"])
    pg.execute(MIGRATIONS[0].read_text(encoding="utf-8"))
    pg.execute("SELECT oid FROM pg_class WHERE relname='uq_mhz_item_live_revision_root'")
    after = int(dict(pg.fetchone())["oid"])
    assert before == after, "第二遍重放把索引 DROP 重建了 —— 存在无约束窗口"


def test_publish_item_handle_completeness_check_exists(pg):
    """P1-6:freeze_per_item 的句柄必须整组齐备(半个句柄比没有句柄更危险)。"""
    psycopg2 = pytest.importorskip("psycopg2")

    pg.execute("SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
               " WHERE conname = 'ck_mhz_item_freeze_handle_complete'")
    row = pg.fetchone()
    assert row is not None, "句柄完整性 CHECK 不存在"

    pg.execute("INSERT INTO mhz_publish_orders (user_id, article_id, article_title,"
               " status) VALUES (42, -2, %s, 'pending') RETURNING id", ("夹具订单",))
    order_id = int(dict(pg.fetchone())["id"])
    # 反向对照:声称 direct_freeze 却缺 task_ref ⇒ 必须被拒
    with pytest.raises(psycopg2.errors.CheckViolation):
        pg.execute(
            "INSERT INTO mhz_publish_order_items "
            " (order_id, user_id, media_id, media_name, status, billing_mode,"
            "  settlement_authority, reserved_amount, physical_split_snapshot) "
            "VALUES (%s, 42, 3, '夹具账号', 'queued', 'freeze_per_item',"
            "        'direct_freeze', 100, '{}'::jsonb)", (order_id,))


def test_coordinator_creates_draft_before_order_and_uses_negative_draft_id():
    """🔴 P0-05:`article_id = -draft_id`,而且 draft 必须**先**建。

    打的是语句次序 + 参数来源。`-geo_post_id` 会被现役解析器当成 draft id 去查
    `mhz_short_video_drafts` —— 碰号即解析到别人的品牌(跨租户串号)。
    """
    src = (REPO / "services" / "geo_douyin" / "publish_coordinator.py").read_text(
        encoding="utf-8")
    assert "_INSERT_DRAFT" in src, "协调器没有铸 draft —— 未进真实供应商链"
    assert '"article_id": -draft_id' in src, (
        "订单的 article_id 不是 -draft_id;若仍是 -geo_post_id,"
        "现役归属解析器会解析到编号相同的另一张草稿")
    assert "-abs(int(item[\"geo_post_id\"]))" not in src, "旧的 -geo_post_id 写法还在"
    assert src.index("_INSERT_DRAFT, {") < src.index("_INSERT_ORDER, {"), \
        "先建单后铸稿 —— `-draft_id` 在建单那一刻还不存在"
