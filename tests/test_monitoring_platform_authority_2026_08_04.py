"""监测平台授权口径收口(R1-R4)· 2026-08-04

生产实证(只读取证,底 = 生产尖 e16ba335):
  · extra_keywords id=23(brand 662 / quote 386)platforms =
    'dashscope,deepseek,doubao,kimi,yuanbao';同 quote 的 8 条合同词全是
    monitoring-classic4-v1(四路)。
  · monitoring_tasks 1543/1544 双 failed,keyword_count=7、platform_count=5、
    total_tests=29。29 = 6(合同词)×4 + 1(手动词)×5 —— 混合矩阵的指纹。
  · monitoring_run_cells 建表至今 804 行,platform 取值只有 classic4 四个,
    yuanbao 单元 0 个;chk_monitoring_run_cells_platform 只允许那四个。

根因:2026-07-26 的 unified5 收口迁移只把**授权侧**推到五引擎(商品矩阵行 +
四处列默认值 + confirmed_keywords.monitoring_product_version 默认值),
**执行账本侧没动**。于是 entitlement 含 yuanbao 的词会让
create_monitoring_run_cells 的 executable ⊆ entitlement 守卫炸掉整批。

Owner 2026-08-04 裁决:账本可执行面先取 classic4 四路(低风险·可逆)。

本文件锁四件事,每条都配**成对反向对照**(只证"必须命中"证明不了判别力):
  R1 手动词从源头继承挂靠 quote 的授权矩阵,不再抄产品默认;
  R2 裁 eligible 与落 cell 读同一个白名单常量,守卫保 fail-closed 且点名到词+平台;
  R3 存量清洗迁移已登记且**不碰列默认值**;
  R4 SSE 外层 catch 不再吞真异常,摘要脱敏截断。
"""
from __future__ import annotations

import ast
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("MONITORING_SCHEDULER_PG_TEST_URL")


# ===========================================================================
# 判据工具:按 AST 切函数体
# ---------------------------------------------------------------------------
# 🔴 不用「整文件 in / 文本窗口」那类断言。上一个包实测被变异打穿两次:
#   · 整文件子串 —— 同一个串在别的端点也有一份,删掉被测那处照样绿;
#   · 文本窗口 —— `if False and not f(x)` 词还在窗口里,锁抓不到。
# 按 AST 取到**这个函数自己的**节点,再看它引用了哪些全局名,换皮和短路都绕不过。
# ===========================================================================
def _function_node(module_path: Path, qualname: str) -> ast.AST:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    parts = qualname.split(".")
    scope = tree
    node = None
    for part in parts:
        node = None
        for child in ast.walk(scope) if scope is tree else ast.iter_child_nodes(scope):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if child.name == part:
                    node = child
                    break
        assert node is not None, f"{module_path.name} 里找不到 {qualname}(切错了,不是实现变了)"
        scope = node
    return node


def _referenced_names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def test_ast_slicer_has_discriminating_power():
    """判据自检:切函数体这件事本身得有判别力,不能恒真。

    这是成对对照的"必须不命中"那一半 —— 没有它,下面所有 AST 锁都可能在
    "其实没切到东西"的情况下全绿。
    """
    node = _function_node(ROOT / "db" / "monitoring_db.py", "create_monitoring_run_cells")
    names = _referenced_names(node)
    assert "MONITORING_RUN_CELL_PLATFORMS" in names
    # 反向:一个确定不在这个函数里的全局名必须**不**出现,证明切的是函数体而非整文件。
    assert "PLATFORM_CANONICAL_ORDER" not in names, (
        "切片没生效:PLATFORM_CANONICAL_ORDER 在本文件别处存在但不在本函数里"
    )
    # 反向:换一个函数,引用集必须不同(否则说明每次都返回了同一坨东西)。
    other = _referenced_names(
        _function_node(ROOT / "db" / "monitoring_db.py", "get_monitoring_config")
    )
    assert names != other


# ===========================================================================
# R2 · 两层同一个白名单常量
# ===========================================================================
def test_r2_both_layers_read_the_same_run_cell_whitelist():
    """裁 eligible 的那层和落 cell 的那层必须引用同一个常量。

    修复前:PlatformAdapter 按 5 路 SUPPORTED_PLATFORMS 裁,
    create_monitoring_run_cells 按 4 路 MONITORING_CLASSIC4_ORDER 落 —— 两层各读
    各的,数值差一个 yuanbao 就炸整批。
    """
    cells = _referenced_names(
        _function_node(ROOT / "db" / "monitoring_db.py", "create_monitoring_run_cells")
    )
    eligible = _referenced_names(
        _function_node(
            ROOT / "tools" / "monitoring" / "batch_monitor.py",
            "PlatformAdapter.eligible_monitoring_platforms",
        )
    )
    assert "MONITORING_RUN_CELL_PLATFORMS" in cells
    assert "MONITORING_RUN_CELL_PLATFORMS" in eligible
    # 成对反向对照:cells 层不许再引用 classic4 那个**商品版本**常量当执行面用。
    # 它今天数值相同,正是这个"恰好相同"掩盖了语义错位直到 unified5 上线才暴露。
    assert "MONITORING_CLASSIC4_ORDER" not in cells, (
        "create_monitoring_run_cells 又拿商品版本常量当账本可执行面用了"
    )


def test_r2_constant_agrees_with_the_db_check_it_claims_to_mirror():
    """常量必须和启动守卫逐字比对的那条 CHECK 定义一致。

    这是本包最要害的一条锁:常量改了而 CHECK 没改 → INSERT 被 CHECK 打回;
    CHECK 改了而守卫期望值没改 → assert_monitoring_cell_retry_ready 直接 raise,
    **两槽都起不来**。把三者钉在一起,任何单边改动当场转红。
    """
    from config.ai_engines import MONITORING_RUN_CELL_PLATFORMS

    source = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    marker = '"chk_monitoring_run_cells_platform": ('
    start = source.index(marker)
    expected_definition = source[start:source.index(")", source.index("CHECK", start))]

    for platform in MONITORING_RUN_CELL_PLATFORMS:
        assert f"'{platform}'::character varying" in expected_definition, (
            f"常量里有 {platform},但守卫期望的 CHECK 定义里没有 —— "
            "改了常量没改 CHECK,INSERT 会被数据库打回"
        )
    # 成对反向对照:CHECK 里也不许有常量之外的平台(那是"跑了客户没买的引擎")。
    for platform in ("yuanbao", "metaso"):
        if platform in MONITORING_RUN_CELL_PLATFORMS:
            continue
        assert f"'{platform}'::character varying" not in expected_definition, (
            f"CHECK 允许 {platform} 但常量不允许 —— 两边又漂了"
        )


def test_r2_guard_stays_fail_closed_and_names_the_keyword_and_platform():
    """守卫不许被放宽,且错误必须点名到「哪个词的哪个平台」。

    1543/1544 当时抛的是一句笼统的 "runtime plan exceeds purchased entitlement
    snapshot",既不知道是哪个词也不知道是哪个平台,排查只能靠反推 total_tests。
    """
    node = _function_node(ROOT / "db" / "monitoring_db.py", "create_monitoring_run_cells")

    raises = [
        child for child in ast.walk(node)
        if isinstance(child, ast.Raise)
        and isinstance(child.exc, ast.Call)
        and getattr(child.exc.func, "id", "") == "MonitoringCellConflict"
    ]
    entitlement_raises = [
        child for child in raises
        if "exceeds purchased entitlement" in ast.unparse(child)
    ]
    assert len(entitlement_raises) == 1, "授权守卫的 raise 不见了或变成多处"

    rendered = ast.unparse(entitlement_raises[0])
    for token in ("keyword_id", "excess_platforms", "entitlement"):
        assert token in rendered, f"守卫错误没点名 {token}"

    # 成对反向对照:守卫必须仍是 raise(fail-closed),不许被降级成 log/warning/pass。
    # 用结构判定,不是查关键词 —— `logger.warning` 出现与否证明不了它没被降级。
    guard_ifs = [
        child for child in ast.walk(node)
        if isinstance(child, ast.If)
        and any(isinstance(inner, ast.Raise) for inner in child.body)
        and "exceeds purchased entitlement" in ast.unparse(child)
    ]
    assert len(guard_ifs) == 1
    assert all(isinstance(stmt, ast.Raise) for stmt in guard_ifs[0].body), (
        "守卫分支里混进了 raise 之外的语句 —— fail-closed 被削弱"
    )


def test_r2_eligible_clamps_to_ledger_surface_with_reverse_control():
    """行为断言:裁 eligible 会打掉账本存不下的平台,且**只**打掉那些。"""
    from config.ai_engines import MONITORING_ENGINES, MONITORING_RUN_CELL_PLATFORMS
    from tools.monitoring.batch_monitor import PlatformAdapter

    eligible = PlatformAdapter.eligible_monitoring_platforms(list(MONITORING_ENGINES))
    assert "yuanbao" not in eligible
    # 反向对照:账本面内的平台一个都不许被顺手打掉(否则就是"少跑了但也绿")。
    for platform in MONITORING_RUN_CELL_PLATFORMS:
        assert platform in eligible, f"账本能跑的 {platform} 被误裁了"

    # 授权更窄时以授权为准(授权闸不因本次改动被放宽)。
    narrowed = PlatformAdapter.eligible_monitoring_platforms(
        list(MONITORING_ENGINES), ["dashscope", "yuanbao"]
    )
    assert narrowed == ["dashscope"]


# ===========================================================================
# R4 · SSE 可见性
# ===========================================================================
def test_r4_plan_error_summary_carries_the_cause_and_redacts_credentials():
    from api.monitoring_api import summarize_monitoring_plan_error
    from db.monitoring_db import MonitoringCellConflict

    summary = summarize_monitoring_plan_error(
        MonitoringCellConflict(
            "runtime plan exceeds purchased entitlement snapshot: keyword_id=23 "
            "source=extra keyword='深圳大户型全屋定制哪家好?' excess_platforms=yuanbao "
            "entitlement=dashscope,deepseek,kimi,doubao"
        )
    )
    # 必须命中:排查真正需要的三样东西都在。
    assert "MonitoringCellConflict" in summary
    assert "keyword_id=23" in summary
    assert "yuanbao" in summary

    # 必须不命中:连接串/口令不许外发。
    leaky = summarize_monitoring_plan_error(
        RuntimeError(
            "could not connect to postgresql://geo_admin:hunter2@10.0.0.1:5432/geo "
            "password=hunter2"
        )
    )
    assert "hunter2" not in leaky
    assert "postgresql://" not in leaky
    assert "[redacted]" in leaky

    # 截断 + 压行:不外发 traceback 那种多行长文。
    long_summary = summarize_monitoring_plan_error(RuntimeError("x\ny\n" + "A" * 500))
    assert "\n" not in long_summary
    assert len(long_summary) == 200


def test_r4_sse_error_event_actually_carries_plan_error():
    """接线锁:helper 存在还不够,SSE 那个 error 事件真的要带上它。

    "死函数 = 复审漏接线"是本仓记录在案的复发型缺陷 —— 只锁 helper 行为,
    helper 零调用照样全绿。
    """
    source = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
    assert "plan_error_summary = summarize_monitoring_plan_error(plan_error)" in source
    marker = "'error': '监测执行计划创建失败，本次未开始调用平台'"
    idx = source.index(marker)
    event_line = source[source.rindex("\n", 0, idx) + 1:source.index("\n", idx)]
    assert "'plan_error': plan_error_summary" in event_line, (
        "SSE error 事件没带 plan_error —— helper 写了但没接线"
    )


# ===========================================================================
# R3 · 存量清洗迁移
# ===========================================================================
MIGRATION_REL = "scripts/migration_monitoring_extra_keyword_entitlement_2026_08_04.sql"


def test_r3_migration_is_registered_in_the_manifest():
    """建了 ≠ 会跑:prestart 只按 manifest 跑,不 glob 目录。"""
    from db.migration_manifest import MIGRATIONS

    assert MIGRATION_REL in MIGRATIONS
    assert (ROOT / MIGRATION_REL).exists()
    # 顺序:必须排在它依赖的两条矩阵迁移之后。
    assert MIGRATIONS.index(MIGRATION_REL) > MIGRATIONS.index(
        "scripts/migration_monitoring_product_matrix_2026_07_21.sql"
    )
    assert MIGRATIONS.index(MIGRATION_REL) > MIGRATIONS.index(
        "scripts/migration_monitoring_unified5_2026_07_26.sql"
    )


def test_r3_migration_must_not_touch_column_defaults():
    """🔴 改列默认值会把两槽做成起不来。

    那四处默认值被 migration_monitoring_unified5_2026_07_26.sql 尾部的 DO 块**逐字
    断言**,而 prestart 每次部署全量重跑清单 —— 在本迁移里改默认值,下次部署那条
    断言必炸 → prestart 非零退出 → 候选不启动。

    判据只看**可执行 SQL**,先剥注释:本文件的注释里正好在解释这件事,不剥注释
    这条锁会被自己的说明文字打成恒红(本仓一天踩过三次的形态)。
    """
    raw = (ROOT / MIGRATION_REL).read_text(encoding="utf-8")
    executable = "\n".join(
        line.split("--", 1)[0] for line in raw.splitlines()
    ).upper()
    assert "SET DEFAULT" not in executable
    assert "ALTER COLUMN" not in executable

    # 成对反向对照:同一个判据打在 unified5 那条迁移上**必须命中**,
    # 否则说明"剥注释后找 SET DEFAULT"这个判据压根没有判别力。
    control_raw = (
        ROOT / "scripts" / "migration_monitoring_unified5_2026_07_26.sql"
    ).read_text(encoding="utf-8")
    control = "\n".join(
        line.split("--", 1)[0] for line in control_raw.splitlines()
    ).upper()
    assert "SET DEFAULT" in control, "判据无判别力:连真的改默认值的迁移都抓不到"


# ===========================================================================
# ①②③④ 真 PostgreSQL 端到端(带真 CHECK 约束,唯一能证明"不再炸整批"的地方)
# ===========================================================================
@pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")
def test_acceptance_end_to_end_real_postgres(monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    from config.ai_engines import (
        MONITORING_PLATFORMS_CLASSIC4_CSV,
        MONITORING_PLATFORMS_CSV,
    )

    database_name = f"platauth_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        parsed = urlsplit(PG_URL)
        target_url = urlunsplit(
            (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
        )

        def connect():
            return psycopg2.connect(target_url, cursor_factory=RealDictCursor)

        monkeypatch.setenv("DATABASE_URL", target_url)
        monkeypatch.setenv("TEST_DATABASE_URL", target_url)
        from db import connection as connection_db
        monkeypatch.setattr(connection_db, "DATABASE_URL", target_url)
        monkeypatch.setattr(connection_db, "_pool", None)
        from db import diagnosis_db, monitoring_db
        monkeypatch.setattr(diagnosis_db, "get_connection", connect)
        monkeypatch.setattr(monitoring_db, "get_connection", connect)

        diagnosis_db.init_db()
        monitoring_db.init_monitoring_tables()
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.organization_charge_links(
                    id BIGSERIAL PRIMARY KEY, status TEXT NOT NULL,
                    physical_backend TEXT NOT NULL, physical_freeze_id TEXT)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.point_freezes(
                    id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL,
                    feature_code TEXT NOT NULL, amount_total BIGINT NOT NULL,
                    status TEXT NOT NULL, task_ref TEXT, brand_id INTEGER)
                """
            )
            for filename in (
                "migration_monitoring_yuanbao_default_2026_07_20.sql",
                "migration_monitoring_product_matrix_2026_07_21.sql",
                "migration_monitoring_identity_review_2026_07_21.sql",
                "migration_monitoring_cell_retry_2026_07_21.sql",
                "migration_monitoring_unified5_2026_07_26.sql",
            ):
                cursor.execute((ROOT / "scripts" / filename).read_text(encoding="utf-8"))
            # 启动守卫必须在本包改动之后仍然通过(常量与 CHECK 没漂)。
            monitoring_db.assert_monitoring_cell_retry_ready(cursor)

            cursor.execute("INSERT INTO brands(name) VALUES ('QZQZ') RETURNING id")
            brand_id = int(cursor.fetchone()["id"])
            cursor.execute("INSERT INTO brands(name) VALUES ('别人家') RETURNING id")
            other_brand_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "INSERT INTO quotes(brand_id,brand_name,status,paid_at) "
                "VALUES (%s,'QZQZ','paid',NOW()) RETURNING id",
                (brand_id,),
            )
            quote_id = int(cursor.fetchone()["id"])
            cursor.execute(
                "INSERT INTO quotes(brand_id,brand_name,status,paid_at) "
                "VALUES (%s,'别人家','paid',NOW()) RETURNING id",
                (other_brand_id,),
            )
            other_quote_id = int(cursor.fetchone()["id"])

            # 复刻 quote 386:6 条 classic4 合同词。
            contract_ids = []
            for n in range(6):
                cursor.execute(
                    "INSERT INTO confirmed_keywords"
                    "(quote_id,keyword,monitoring_product_version) "
                    "VALUES (%s,%s,'monitoring-classic4-v1') RETURNING id",
                    (quote_id, f"深圳全屋定制词{n}"),
                )
                contract_ids.append(int(cursor.fetchone()["id"]))
            # 别人家的 quote 走 unified5(用来证明归属核验不串矩阵)。
            cursor.execute(
                "INSERT INTO confirmed_keywords"
                "(quote_id,keyword,monitoring_product_version) "
                "VALUES (%s,'别人家的词','monitoring-unified5-v1')",
                (other_quote_id,),
            )
        conn.commit()

        # ---------------- ② 新加手动词的 platforms 与其 quote 矩阵逐字一致 -------
        extra_id = monitoring_db.add_keyword(
            brand_id=brand_id,
            client_id=str(quote_id),
            keyword="深圳大户型全屋定制哪家好?",
            target_brand="QZQZ",
        )
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute("SELECT platforms FROM extra_keywords WHERE id=%s", (extra_id,))
            written = cursor.fetchone()["platforms"]
        assert written == MONITORING_PLATFORMS_CLASSIC4_CSV, (
            "手动词没继承挂靠 quote 的矩阵(逐字)"
        )
        # 成对反向对照:老口径(产品默认五引擎)必须**不**再出现。
        assert written != MONITORING_PLATFORMS_CSV

        # 归属核不实时不许继承别人 quote 的矩阵(fail-closed)。
        # 🔴 不比对最终字符串:别人 quote 是 unified5,而 brand 口径(monitoring_config
        #    缺行 → DEFAULT_MONITORING_PLATFORMS)也是五引擎,两者**字面相同** ——
        #    比字符串在这里零判别力。验归属解析本身返 None。
        assert monitoring_db.resolve_extra_keyword_platforms(
            brand_id=brand_id, client_id=str(other_quote_id)
        ) is None, "归属核不实却继承了别人 quote 的授权矩阵"
        # 成对正向对照:换成自己的 quote 必须解析得出来,证明上面的 None 不是恒 None。
        assert monitoring_db.resolve_extra_keyword_platforms(
            brand_id=brand_id, client_id=str(quote_id)
        ) == MONITORING_PLATFORMS_CLASSIC4_CSV

        # 给**迁移**的归属核验造一个能分辨的样本:brand 与 quote 对不上的错挂行。
        # 🔴 必须直接 INSERT,不能走 add_keyword —— _resolve_id(brand_id=X, ...) 会
        #    忽略传入的 client_id、改查该 brand 自己最新的 quote,压根造不出错挂行。
        #    (第一版就是这么写的,于是"跨租户"样本根本不存在,变异 M11 当场存活。)
        # 形态:brand_id=别人家,quote_id=QZQZ 的 quote(classic4),platforms=五引擎。
        #   · 有归属核验 → 两边 brand 对不上,迁移不碰它,值保持五引擎;
        #   · 丢了归属核验 → 匹配上 classic4 矩阵且值不同 → 会被改写 → 下面断言转红。
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO extra_keywords(quote_id,client_id,brand_id,keyword,"
                "target_brand,platforms) VALUES (%s,%s,%s,'错挂归属探针词','QZQZ',%s) "
                "RETURNING id",
                (quote_id, str(quote_id), other_brand_id, MONITORING_PLATFORMS_CSV),
            )
            mis_attributed_id = int(cursor.fetchone()["id"])
            conn.commit()
        # 成对正向对照:自己 quote 必须解析得出来,证明上面的 None 不是恒 None。
        assert monitoring_db.resolve_extra_keyword_platforms(
            brand_id=brand_id, client_id=str(quote_id)
        ) == MONITORING_PLATFORMS_CLASSIC4_CSV

        # ---------------- ① id=23 场景原样复现:7 词一批必须建得起来 -------------
        from tools.monitoring.batch_monitor import PlatformAdapter

        keywords = [
            {
                "id": kid, "quote_id": quote_id, "keyword": f"深圳全屋定制词{n}",
                "monitoring_query": f"深圳全屋定制词{n}", "target_brand": "QZQZ",
                "source": "confirmed",
                "monitoring_product_version": "monitoring-classic4-v1",
                "entitlement_platforms": MONITORING_PLATFORMS_CLASSIC4_CSV,
            }
            for n, kid in enumerate(contract_ids)
        ] + [
            {
                "id": extra_id, "quote_id": quote_id,
                "keyword": "深圳大户型全屋定制哪家好?",
                "monitoring_query": "深圳大户型全屋定制哪家好?",
                "target_brand": "QZQZ", "source": "extra",
                "entitlement_platforms": written,
            }
        ]
        plan = PlatformAdapter.resolve_keyword_monitoring_plan(
            keywords, configured_platforms=MONITORING_PLATFORMS_CSV
        )
        assert plan["rejected"] == []
        planned = plan["keywords"]
        assert len(planned) == 7
        # 手动词与合同词**同** classic4 跑 —— 不再是 6×4 + 1×5 = 29 那个混合矩阵。
        per_keyword = {len(k["_eligible_monitoring_platforms"]) for k in planned}
        assert per_keyword == {4}
        total_tests = sum(len(k["_eligible_monitoring_platforms"]) for k in planned)
        assert total_tests == 28, f"仍是混合矩阵(生产炸掉那次是 29): {total_tests}"

        task_id = monitoring_db.create_monitoring_task(
            brand_id=brand_id, client_id=str(quote_id),
            keyword_ids=[k["id"] for k in planned], trigger_type="manual_sse",
            planned_test_count=total_tests, planned_platform_count=len(plan["platforms"]),
        )
        cells = monitoring_db.create_monitoring_run_cells(
            task_id=task_id, brand_id=brand_id, keywords=planned,
            search_mode="enhanced", fulfillment_credential=str(uuid.uuid4()),
            fulfillment_state="covered", settlement_reference="monitor_stream_platauth",
            retry_coverage={
                "policy_version": "monitoring-retry-v1",
                "coverage": "included", "max_attempts": 1,
            },
        )
        assert len(cells) == 28
        assert {row["state"] for row in cells} == {"queued"}
        assert {row["platform"] for row in cells} == set(
            MONITORING_PLATFORMS_CLASSIC4_CSV.split(",")
        )

        # ---------------- ④ 合同词行为零变化 -----------------------------------
        contract_cells = [c for c in cells if c["keyword_source"] == "confirmed"]
        extra_cells = [c for c in cells if c["keyword_source"] == "extra"]
        assert len(contract_cells) == 24 and len(extra_cells) == 4
        for row in contract_cells:
            snapshot = row["entitlement_snapshot"]
            assert snapshot["platforms"] == list(
                MONITORING_PLATFORMS_CLASSIC4_CSV.split(",")
            )
            assert snapshot["monitoring_product_version"] == "monitoring-classic4-v1"

        # ---------------- ③ 单词条授权矛盾不再炸整批,且报错点名到词 -------------
        contradicted = [dict(k) for k in planned]
        contradicted[3]["_eligible_monitoring_platforms"] = ["dashscope", "yuanbao"]
        bad_task_id = monitoring_db.create_monitoring_task(
            brand_id=brand_id, client_id=str(quote_id),
            keyword_ids=[k["id"] for k in contradicted], trigger_type="manual_sse",
            planned_test_count=28, planned_platform_count=4,
        )
        with pytest.raises(monitoring_db.MonitoringCellConflict) as excinfo:
            monitoring_db.create_monitoring_run_cells(
                task_id=bad_task_id, brand_id=brand_id, keywords=contradicted,
                search_mode="enhanced", fulfillment_credential=str(uuid.uuid4()),
                fulfillment_state="covered",
                settlement_reference="monitor_stream_platauth_bad",
                retry_coverage={
                    "policy_version": "monitoring-retry-v1",
                    "coverage": "included", "max_attempts": 1,
                },
            )
        message = str(excinfo.value)
        assert f"keyword_id={contradicted[3]['id']}" in message, "报错没点名到出问题那个词"
        assert "excess_platforms=yuanbao" in message, "报错没点名到出问题那个平台"
        # 成对反向对照:不许点错人 —— 别的词的 id 不能出现在这条错误里。
        innocent = contradicted[0]["id"]
        assert f"keyword_id={innocent}" not in message

        # 整批回滚:炸掉的这一批不许留下半拉子 cell(资金/履约账本不能半写)。
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) AS n FROM monitoring_run_cells WHERE task_id=%s",
                (bad_task_id,),
            )
            assert int(cursor.fetchone()["n"]) == 0
            # 反向对照:好的那一批还在,证明上面的 0 不是"这张表恒空"。
            cursor.execute(
                "SELECT count(*) AS n FROM monitoring_run_cells WHERE task_id=%s",
                (task_id,),
            )
            assert int(cursor.fetchone()["n"]) == 28

        # ---------------- R3 迁移:存量清洗 + 幂等 ------------------------------
        conn = connect()
        with conn, conn.cursor() as cursor:
            # 造一条"写歪的存量行"(就是生产 id=23 的形态)。
            cursor.execute(
                "UPDATE extra_keywords SET platforms=%s WHERE id=%s",
                (MONITORING_PLATFORMS_CSV, extra_id),
            )
            conn.commit()
            migration_sql = (ROOT / MIGRATION_REL).read_text(encoding="utf-8")
            cursor.execute(migration_sql)
            conn.commit()
            cursor.execute("SELECT platforms FROM extra_keywords WHERE id=%s", (extra_id,))
            assert cursor.fetchone()["platforms"] == MONITORING_PLATFORMS_CLASSIC4_CSV
            cursor.execute(
                "SELECT old_value,new_value FROM "
                "monitoring_extra_keyword_entitlement_backup_20260804 WHERE source_id=%s",
                (extra_id,),
            )
            provenance = cursor.fetchone()
            assert provenance["old_value"] == MONITORING_PLATFORMS_CSV, "没留痕原值"
            assert provenance["new_value"] == MONITORING_PLATFORMS_CLASSIC4_CSV
            # 幂等:重跑不炸,且不把已修正的值当"原值"覆盖上去。
            cursor.execute(migration_sql)
            conn.commit()
            cursor.execute(
                "SELECT old_value FROM "
                "monitoring_extra_keyword_entitlement_backup_20260804 WHERE source_id=%s",
                (extra_id,),
            )
            assert cursor.fetchone()["old_value"] == MONITORING_PLATFORMS_CSV
            # 反向对照:brand 与 quote 对不上的错挂行,迁移必须**没碰**。
            # 值和留痕两头都验:只验留痕表,"改了值但漏写留痕"的实现会溜过去。
            cursor.execute(
                "SELECT count(*) AS n FROM "
                "monitoring_extra_keyword_entitlement_backup_20260804 WHERE source_id=%s",
                (mis_attributed_id,),
            )
            assert int(cursor.fetchone()["n"]) == 0, "迁移改了归属核不实的行(留痕表里出现了)"
            cursor.execute(
                "SELECT platforms FROM extra_keywords WHERE id=%s", (mis_attributed_id,)
            )
            assert cursor.fetchone()["platforms"] == MONITORING_PLATFORMS_CSV, (
                "迁移把归属核不实的行改写成了它错挂那个 quote 的矩阵"
            )
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
        admin.close()
