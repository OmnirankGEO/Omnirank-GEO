"""前端接线包判据 · 报告绑定(CUR-01)+ legacy 零漂移。

真库判据:CUR-01 的路由开关取决于一条**跨三张表**的链
(``diagnosis_records.run_token`` → ``defgeo_diagnosis_run_previews.consumed_command_id``
→ ``campaign_mode``)。mock 一个 cursor 只能证明"我写的 Python 会读那几个键",
证明不了"这条链在真表上接得上" —— 本仓明确记过「真 HTTP/真库判据必须打真库」。
"""

from __future__ import annotations

import ast
import re
import uuid

import pytest

from services.defensive_geo.presentation.report_binding import (
    LEGACY, V2_MODES, resolve_binding,
)


@pytest.fixture()
def wire_rows(conn):
    """一份 legacy 报告 + 一份 v2 报告 + 一份「有 run_token 但无 v2 preview」的报告。

    第三种是**最容易被漏掉**的一格:现役 legacy 诊断链也会写 run_token,
    只按"有没有 run_token"判 v2 会把它误判进新组件。
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id,username,password_hash,display_name)"
                    " VALUES (9301,'wireprobe','x','W') ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO brands (id,name,owner_user_id)"
                    " VALUES (9301,'WireBrand',9301) ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO quotes (id,brand_id) VALUES (9301,9301)"
                    " ON CONFLICT DO NOTHING")
        cur.execute(
            "INSERT INTO keyword_selection_sessions"
            " (id,token,quote_id,brand_id,keywords_snapshot,expires_at)"
            " VALUES (9301,'wiretok',9301,9301,'[]','2099-01-01')"
            " ON CONFLICT DO NOTHING")

        # plan_id / preview_id / question_plan_id 是 **UUID 列**,不是 text
        # (第一轮夹具用 'plan-wire-1' 直接 InvalidTextRepresentation)。
        plan_uuid = str(uuid.uuid4())
        preview_uuid = str(uuid.uuid4())
        v2_token = "cmd-" + uuid.uuid4().hex[:16]
        legacy_run_token = "cmd-" + uuid.uuid4().hex[:16]

        cur.execute(
            # NOT NULL 列由 information_schema 现查后逐列填
            # (session_id / brand_name / industry —— 手写夹具第一轮漏了后两个)
            "INSERT INTO diagnosis_records"
            " (id,session_id,brand_name,industry,brand_id,run_token)"
            " VALUES (9301,'sess-9301','WireBrand','测试',9301,NULL),"
            "        (9302,'sess-9302','WireBrand','测试',9301,%s),"
            "        (9303,'sess-9303','WireBrand','测试',9301,%s)"
            " ON CONFLICT DO NOTHING",
            (v2_token, legacy_run_token))

        # 🔴 必填列由 information_schema 现查后逐列填(前两轮各漏一批:
        #    先漏 created_by_user_id,再漏 approval_requirement/idempotency_key)。
        cur.execute(
            "INSERT INTO defgeo_question_plans"
            " (plan_id,plan_revision,tenant_owner_user_id,brand_id,"
            "  profile_revision_id,mode,question_set_version,canonical_hash,"
            "  frozen_payload,client_request_id,request_content_hash,"
            "  expires_at,created_by_user_id,defensive_count,offensive_count,total_count)"
            " VALUES (%s,1,9301,9301,'prof-1','defensive','v1',%s,"
            "         '{}'::jsonb,'req-1',%s, now() + interval '1 day',9301,1,0,1)"
            " ON CONFLICT DO NOTHING",
            (plan_uuid, "a" * 64, "b" * 64))

        cur.execute(
            "INSERT INTO defgeo_diagnosis_run_previews"
            " (preview_id,tenant_owner_user_id,brand_id,created_by_user_id,"
            "  question_plan_id,question_plan_revision,question_plan_hash,"
            "  profile_revision_id,campaign_mode,frozen_payload,canonical_hash,"
            "  feature_code,pricing_catalog_version,base_points,extra_points,"
            "  exact_total_points,funding_policy,principal_kind,approval_requirement,"
            "  planned_cells,idempotency_key,canonical_request_hash,expires_at,"
            # consumed 三件套必须同时给 —— 窗A 的 chk_defgeo_preview_consumed_pair
            # 不允许「有 command_id 没 consumed_at」这种半状态。
            "  lifecycle,consumed_command_id,consumed_at)"
            " VALUES (%s,9301,9301,9301,%s,1,%s,'prof-1','defensive','{}'::jsonb,%s,"
            "         'geo_diagnosis','cat-1',10,0,10,'personal_wallet','personal',"
            "         'not_required',3,%s,%s, now() + interval '1 day',"
            "         'consumed',%s, now())"
            " ON CONFLICT DO NOTHING",
            (preview_uuid, plan_uuid, "a" * 64, "c" * 64,
             "idem-wire-1", "d" * 64, v2_token))
    return {"legacy_null": 9301, "v2": 9302, "legacy_with_token": 9303,
            "plan_uuid": plan_uuid}


def test_v2_defensive_run_binds_to_new_presentation(conn, wire_rows):
    """活性自证:真 v2 运行必须被识别出来。它红了,下面的否定判据就没有区分力。"""
    with conn.cursor() as cur:
        b = resolve_binding(cur, wire_rows["v2"])
    assert b.is_v2 is True
    assert b.campaign_mode == "defensive"
    assert b.question_plan_id == wire_rows["plan_uuid"]
    assert b.mode_user_label == "先守住品牌"


def test_report_without_run_token_is_legacy(conn, wire_rows):
    with conn.cursor() as cur:
        assert resolve_binding(cur, wire_rows["legacy_null"]) == LEGACY


def test_run_token_without_v2_preview_is_still_legacy(conn, wire_rows):
    """🔴 最容易漏的一格:现役 legacy 诊断链**也**写 run_token。

    只按"有没有 run_token"判 v2,会把全部现役诊断送进新组件 —— 那是 ACT-01
    「legacy 响应/行为不漂」的反面。
    """
    with conn.cursor() as cur:
        assert resolve_binding(cur, wire_rows["legacy_with_token"]) == LEGACY


def test_missing_diagnosis_is_legacy_not_an_exception(conn):
    """不存在的 id 走 legacy,不抛 —— 报告页不该因为绑定查不到就打不开。"""
    with conn.cursor() as cur:
        assert resolve_binding(cur, 99999999) == LEGACY


#: 写语句的**语句形态**,不是裸动词 —— 裸动词会被列名撞上
#: (实测 `is_deleted` 里的 "DELETE" 让第一版恒红)。
_DML_STATEMENT = re.compile(
    # NOTE: 本行用 Write/Edit 维护,禁用 bash heredoc —— heredoc 会把
    # \b 吃成退格字节 0x08,pattern 变 '\b(INSERT...' 从而恒不命中。
    r"\b(INSERT\s+INTO|UPDATE\s+\S|DELETE\s+FROM|TRUNCATE\s+\S)", re.I)


def _sql_literals(path) -> list[str]:
    """用 AST 取出**非 docstring** 的字符串字面量。

    🔴 为什么不能用「剥掉所有三引号」那招:本仓两个模块的 SQL 都写在
       三引号字符串里,一剥就把要查的 SQL 一起剥掉了 —— 判据会**恒绿**。
       第一版就是这么写的,是反向对照(拿一个确实会写库的模块量)把它打红的。
       AST 能精确区分「函数/模块开头的说明文字」与「装 SQL 的字符串」。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    out: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):        # f-string
            out.append("".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)))
    return out


def _write_statements(path) -> list[str]:
    return [m for lit in _sql_literals(path) for m in _DML_STATEMENT.findall(lit)]


def test_binding_is_read_only():
    """呈现层不得有能力改绑定关系。源码里零写语句。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    target = root / "services" / "defensive_geo" / "presentation" / "report_binding.py"
    assert _write_statements(target) == []


def test_dml_detector_actually_detects_dml():
    """反向对照:同一把尺子量一个**确实会写库**的模块必须非空。

    没有这条,「零写语句」与「正则根本没在工作」长得一模一样 ——
    而实测第一版正则确实没在工作(把 SQL 连同 docstring 一起剥掉了)。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    writer = root / "services" / "defensive_geo" / "activation_outbox.py"
    found = _write_statements(writer)
    assert len(found) > 0, "尺子坏了:确实会写库的模块也没量出写语句"


def test_v2_modes_denominator_is_the_three_campaign_modes():
    assert V2_MODES == {"defensive", "offensive", "hybrid"}
