"""工单 M-1(监测身份确认交互)判别锁 · 2026-07-28。

后端(真实 PG · 确认→重判→计入全链):
- 确认后用**已确认口径**对本条原文本地重判再计入,不再按动作一刀切;
- custom 名不在本条原文 → 不得凭空计入(is_detected=0);在原文 → 计入;
- 其余条目(同轮其他 pending)不受影响;
- 零额外引擎调用:重判走 resolve_local(确定性),结构化 verifier/httpx 装炸弹仍全链成功。

前端(源码面 · 行为面在既有 workbench 套件 + 新增文案/回写钉):
- ① 确认落库后 onDecided 回写(重取任务详情 + 刷新关键词统计);行级"已确认 · 已计入";
- ③ 候选称呼上下文片段 + 高亮定位(「」括起防撞既有唯一性断言);
- ④ 文案:仅 N 条待确认、其余已正常计入(面板副标题 + 进度面板摘要 + 行级/详情文案);
- ⑤ 卡片容器错版:border-y 段落补横向内边距。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("MONITORING_SCHEDULER_PG_TEST_URL") or os.environ.get("TEST_DATABASE_URL")

MON_INDEX = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "index.tsx"
PANEL = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "components" / "IdentityReviewPanel.tsx"
PROGRESS = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "components" / "ProgressPanel.tsx"
DB_SRC = ROOT / "db" / "monitoring_db.py"


# ===========================================================================
# 后端 · 确认→重判→计入全链(真实 PG)
# ===========================================================================
@pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")
def test_decision_rejudges_locally_and_counts_correctly(monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    from db import connection as connection_db

    database_name = f"m1_rejudge_{uuid.uuid4().hex[:12]}"
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
        monkeypatch.setattr(connection_db, "DATABASE_URL", target_url)
        monkeypatch.setattr(connection_db, "_pool", None)

        from db import diagnosis_db, monitoring_db

        monkeypatch.setattr(diagnosis_db, "get_connection", connect)
        monkeypatch.setattr(monitoring_db, "get_connection", connect)
        monkeypatch.setattr(connection_db, "get_connection", connect)

        diagnosis_db.init_db()
        monitoring_db.init_monitoring_tables()

        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
                ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS brand_display_names TEXT;
                CREATE TABLE IF NOT EXISTS public.users (
                    id BIGINT PRIMARY KEY, username TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS public.roles (
                    id BIGINT PRIMARY KEY, name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS public.user_roles (
                    user_id BIGINT NOT NULL, role_id BIGINT NOT NULL,
                    PRIMARY KEY (user_id, role_id)
                );
                CREATE TABLE IF NOT EXISTS public.user_clients (
                    user_id BIGINT NOT NULL, brand_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, brand_id)
                );
                CREATE TABLE IF NOT EXISTS public.client_profiles (
                    id BIGSERIAL PRIMARY KEY, brand_id INTEGER NOT NULL,
                    brand_display_names TEXT, is_deleted INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS public.brand_aliases (
                    id BIGSERIAL PRIMARY KEY, canonical_name TEXT NOT NULL,
                    alias TEXT NOT NULL, brand_id INTEGER, source TEXT DEFAULT 'manual',
                    UNIQUE (canonical_name, alias)
                );
                """
            )
            for script in (
                "migration_monitoring_yuanbao_default_2026_07_20.sql",
                "migration_monitoring_product_matrix_2026_07_21.sql",
                "migration_monitoring_identity_review_2026_07_21.sql",
                "migration_diagnosis_identity_review_2026_07_22.sql",
            ):
                cursor.execute((ROOT / "scripts" / script).read_text(encoding="utf-8"))
            cursor.execute(
                """
                INSERT INTO public.users (id, username) VALUES (8101, 'm1-owner');
                INSERT INTO public.brands
                    (name, company_name, owner_user_id, brand_display_names, is_deleted)
                VALUES ('目标品牌', '目标品牌有限公司', 8101, '["目标品牌"]', FALSE)
                RETURNING id;
                """
            )
            brand_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO public.monitoring_tasks
                    (client_id, brand_id, task_name, keyword_count, platform_count,
                     total_tests, completed_tests, status, trigger_type)
                VALUES ('m1-rejudge', %s, 'm1 rejudge', 4, 4, 4, 4, 'completed', 'manual')
                RETURNING id
                """,
                (brand_id,),
            )
            task_id = int(cursor.fetchone()["id"])
        conn.close()

        def pending_result(platform: str, candidate: str, full_response: str):
            return {
                "task_id": task_id,
                "keyword": f"{platform} 测试词",
                "platform": platform,
                "brand_id": brand_id,
                "identity_brand_id": brand_id,
                "identity_review_state": "pending",
                "identity_candidates": [candidate],
                "identity_evidence_snippet": full_response[:200],
                "is_detected": False,
                "mention_type": "pending_identity",
                "response_status": "brand_identity_unresolved",
                "full_response": full_response,
                "response_snippet": full_response[:120],
                "sent_question_snapshot": f"{platform} 测试词",
                "provider": platform,
                "model": f"{platform}-model",
                "surface": "monitoring",
                "target_brand": "目标品牌",
            }

        padding = "本行业各家厂商在环保等级与交付周期上的公开资料对比。" * 12
        # 短中文名的确定性命中需要品牌语境边界(推荐 X，)——resolve_local 的既有安全约束
        assert monitoring_db.batch_save_results([
            pending_result("deepseek", "泰某生物", f"推荐泰某生物，{padding}"),
            pending_result("kimi", "别家候选", f"另一个平台回答提到别家候选,{padding}"),
        ]) == 2
        rows = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)
        assert len(rows) == 2
        row_by_platform = {r["platform"]: r for r in rows}
        yes_row = row_by_platform["deepseek"]
        other_row = row_by_platform["kimi"]

        # 零额外引擎调用:结构化 verifier 与 httpx 全程装炸弹(变异改调 resolve → 转红)
        import services.brand_identity_resolver as resolver_mod

        async def verifier_bomb(**kwargs):
            raise AssertionError("确认链不得调用结构化 verifier(零引擎调用)")

        monkeypatch.setattr(resolver_mod, "_default_structured_verifier", verifier_bomb)

        class _HttpxBomb:
            def __init__(self, *args, **kwargs):
                raise AssertionError("确认链不得发起任何 HTTP 调用")

        monkeypatch.setattr(resolver_mod.httpx, "AsyncClient", _HttpxBomb)

        # ① yes:候选在原文 + 被确认 → 重判 YES → 计入检出
        resolved = monitoring_db.decide_monitoring_identity_review(
            result_id=yes_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="yes", selected_name="泰某生物", expected_version=0,
            evidence_hash=yes_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert resolved["status"] == "resolved"
        assert resolved["is_detected"] is True
        assert resolved["mention_type"] == "mentioned"
        assert resolved["rejudge_verdict"] == "YES"

        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT identity_review_state, is_detected, mention_type, response_status "
                "FROM public.monitoring_results WHERE id = %s",
                (yes_row["id"],),
            )
            updated = cursor.fetchone()
            assert updated["identity_review_state"] == "confirmed"
            assert int(updated["is_detected"]) == 1
            assert updated["mention_type"] == "mentioned"  # P0-3:不升档 recommended
            assert updated["response_status"] == "success"
            # 其余条目不受影响:同轮另一条 pending 原样
            cursor.execute(
                "SELECT identity_review_state, is_detected FROM public.monitoring_results WHERE id = %s",
                (other_row["id"],),
            )
            untouched = cursor.fetchone()
            assert untouched["identity_review_state"] == "pending"
            assert int(untouched["is_detected"]) == 0
        conn.close()

        # ② custom 名**不在**本条原文 → 不得凭空计入(旧一刀切会记 is_detected=1)
        custom_absent = monitoring_db.decide_monitoring_identity_review(
            result_id=other_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="custom", selected_name="目标品牌云仓", expected_version=0,
            evidence_hash=other_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert custom_absent["status"] == "resolved"
        assert custom_absent["is_detected"] is False
        assert custom_absent["rejudge_verdict"] == "NO"
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT identity_review_state, is_detected, mention_type "
                "FROM public.monitoring_results WHERE id = %s",
                (other_row["id"],),
            )
            corrected = cursor.fetchone()
            assert corrected["identity_review_state"] == "confirmed"  # 名称决策仍生效
            assert int(corrected["is_detected"]) == 0                  # 但本条按未提及计入
            assert corrected["mention_type"] == "none"
        conn.close()

        # ②b 缺席 + **UNKNOWN 分支**:回答里有近似称呼(落 UNKNOWN),但人手填的名字
        #     字面不在原文 → 仍不得凭空计入。
        #     🔴 自审抓到:②a 那条走的是 NO 分支,UNKNOWN 兜底的"缺席"方向此前无锁,
        #     变异"兜底放宽成任何非 no 都计入"当时存活。
        unknown_answer = f"推荐目标品牌旗舰馆的相关服务。{padding}"
        assert monitoring_db.batch_save_results([
            pending_result("kimi", "目标品牌旗舰馆", unknown_answer),
        ]) == 1
        unknown_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        import services.brand_identity_resolver as _probe_mod

        unknown_probe = _probe_mod.BrandIdentityResolver(
            _probe_mod.BrandIdentity(
                brand_id=brand_id,
                canonical_names=("目标品牌", "目标品牌有限公司"),
                trusted_aliases=("目标品牌",),
            )
        ).resolve_local(unknown_answer)
        assert unknown_probe.verdict is _probe_mod.BrandVerdict.UNKNOWN, (
            "本用例必须落 UNKNOWN 分支,才检验得到兜底的'缺席不计入'方向"
        )
        absent_unknown = monitoring_db.decide_monitoring_identity_review(
            result_id=unknown_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="custom", selected_name="从未出现的名字甲乙", expected_version=0,
            evidence_hash=unknown_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert absent_unknown["rejudge_verdict"] == "UNKNOWN"
        assert absent_unknown["is_detected"] is False, "UNKNOWN + 名字不在原文 → 不得凭空计入"

        # ③a custom 名在原文且**普通行文**(resolve_local 的自动推断护栏会给 UNKNOWN)
        #     → 人工确认必须压过护栏,按字面出现计入。
        #     🔴 自审抓到的回归面:只信 action=='yes' 的兜底会把这种最常见的行文
        #     记成"未提及",比旧一刀切还糟(旧代码这里记 True)。
        assert monitoring_db.batch_save_results([
            pending_result("dashscope", "目标品牌云仓库", f"回答中提到了目标品牌云仓库的案例。{padding}"),
        ]) == 1
        ordinary_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        import services.brand_identity_resolver as _resolver_probe

        probe = _resolver_probe.BrandIdentityResolver(
            _resolver_probe.BrandIdentity(
                brand_id=brand_id, canonical_names=("目标品牌",),
                trusted_aliases=("目标品牌云仓库",),
            )
        ).resolve_local(f"回答中提到了目标品牌云仓库的案例。{padding}")
        assert probe.verdict is _resolver_probe.BrandVerdict.UNKNOWN, (
            "本用例必须落在 UNKNOWN 分支才检验得到人工确认兜底"
        )
        ordinary = monitoring_db.decide_monitoring_identity_review(
            result_id=ordinary_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="custom", selected_name="目标品牌云仓库", expected_version=0,
            evidence_hash=ordinary_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert ordinary["is_detected"] is True, "人工手填且字面在原文 → 必须计入"
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT is_detected FROM public.monitoring_results WHERE id = %s",
                (ordinary_row["id"],),
            )
            assert int(cursor.fetchone()["is_detected"]) == 1
        conn.close()

        # ③b custom 名在原文且行文带品牌语境 → 重判 YES → 计入
        assert monitoring_db.batch_save_results([
            pending_result("doubao", "目标品牌云工厂", f"推荐目标品牌云工厂，{padding}"),
        ]) == 1
        present_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        custom_present = monitoring_db.decide_monitoring_identity_review(
            result_id=present_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="custom", selected_name="目标品牌云工厂", expected_version=0,
            evidence_hash=present_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert custom_present["is_detected"] is True
        assert custom_present["rejudge_verdict"] == "YES"

        # ④ no:重判必须吃到**本次刚写入的否定名**(human_rejected_name),
        #    不是碰巧 NO —— 变异(重判身份漏掉否定名)→ 本断言转红
        assert monitoring_db.batch_save_results([
            pending_result("dashscope", "无关品牌名", f"无关品牌名，{padding}"),
        ]) == 1
        no_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        denied = monitoring_db.decide_monitoring_identity_review(
            result_id=no_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="no", selected_name="无关品牌名", expected_version=0,
            evidence_hash=no_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert denied["is_detected"] is False
        assert denied["rejudge_verdict"] == "NO"
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT metadata FROM public.monitoring_identity_decision_events WHERE result_id = %s",
                (no_row["id"],),
            )
            metadata = cursor.fetchone()["metadata"]
            assert metadata["rejudge_reason"] == "human_rejected_name"
            assert metadata["counted_is_detected"] is False
        conn.close()

        # ⑤ 整段回答里确实没有客户品牌：允许 no + 空名称直接按未提及计入，
        # 但不把空值或同行名称写进跨回答复用的品牌别名决策表。
        absent_full_response = f"回答列出了同行甲、同行乙和同行丙。{padding}"
        absent_payload = pending_result("kimi", "不会落库的占位候选", absent_full_response)
        absent_payload["identity_candidates"] = []
        assert monitoring_db.batch_save_results([absent_payload]) == 1
        absent_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM public.monitoring_identity_name_decisions "
                "WHERE brand_id = %s",
                (brand_id,),
            )
            name_decisions_before_absence = cursor.fetchone()["count"]
        conn.close()

        with pytest.raises(ValueError, match="请选择候选名称"):
            monitoring_db.decide_monitoring_identity_review(
                result_id=absent_row["id"], brand_id=brand_id, actor_user_id=8101,
                action="yes", selected_name="", expected_version=0,
                evidence_hash=absent_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
            )

        absence_request_id = str(uuid.uuid4())
        absent = monitoring_db.decide_monitoring_identity_review(
            result_id=absent_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="no", selected_name="", expected_version=0,
            evidence_hash=absent_row["identity_evidence_hash"], request_id=absence_request_id,
        )
        assert absent["status"] == "resolved"
        assert absent["is_detected"] is False
        assert absent["mention_type"] == "none"
        assert absent["rejudge_verdict"] == "human_confirmed_absent"
        replay = monitoring_db.decide_monitoring_identity_review(
            result_id=absent_row["id"], brand_id=brand_id, actor_user_id=8101,
            action="no", selected_name="", expected_version=0,
            evidence_hash=absent_row["identity_evidence_hash"], request_id=absence_request_id,
        )
        assert replay["status"] == "idempotent"

        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT identity_review_state, response_status, is_detected,
                       mention_type, target_outcome, full_response
                  FROM public.monitoring_results
                 WHERE id = %s
                """,
                (absent_row["id"],),
            )
            stored = cursor.fetchone()
            assert stored["identity_review_state"] == "rejected"
            assert stored["response_status"] == "success"
            assert int(stored["is_detected"]) == 0
            assert stored["mention_type"] == "none"
            assert stored["target_outcome"] == "not_mentioned"
            assert stored["full_response"] == absent_full_response
            cursor.execute(
                "SELECT action, selected_name, normalized_name, metadata "
                "FROM public.monitoring_identity_decision_events WHERE result_id = %s",
                (absent_row["id"],),
            )
            event = cursor.fetchone()
            assert event["action"] == "no"
            assert event["selected_name"] == ""
            assert event["normalized_name"] == ""
            assert event["metadata"] == {
                "source": "monitoring_human_review",
                "decision_scope": "full_answer_absent",
                "decision_name_count": 0,
                "decision_names": [],
                "rejudge_verdict": "human_confirmed_absent",
                "rejudge_reason": "full_answer_reviewed_absent",
                "rejudge_method": "human_full_answer_absence",
                "counted_is_detected": False,
            }
            cursor.execute(
                "SELECT COUNT(*) AS count FROM public.monitoring_identity_name_decisions "
                "WHERE brand_id = %s",
                (brand_id,),
            )
            assert cursor.fetchone()["count"] == name_decisions_before_absence
        conn.close()
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
        finally:
            admin.close()


# ===========================================================================
# 后端 · 「没有出现」出口的可信名守卫(返修单 R1 · 2026-08-12)
# ===========================================================================
def _provision_identity_review_database(monkeypatch, tag: str):
    """建一个一次性真库并把三处 get_connection 指过去,返回 (connect, monitoring_db, drop)。"""
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    from db import connection as connection_db

    database_name = f"{tag}_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
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
    monkeypatch.setattr(connection_db, "DATABASE_URL", target_url)
    monkeypatch.setattr(connection_db, "_pool", None)

    from db import diagnosis_db, monitoring_db

    monkeypatch.setattr(diagnosis_db, "get_connection", connect)
    monkeypatch.setattr(monitoring_db, "get_connection", connect)
    monkeypatch.setattr(connection_db, "get_connection", connect)

    diagnosis_db.init_db()
    monitoring_db.init_monitoring_tables()

    conn = connect()
    with conn, conn.cursor() as cursor:
        cursor.execute(
            """
            ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
            ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS brand_display_names TEXT;
            CREATE TABLE IF NOT EXISTS public.users (
                id BIGINT PRIMARY KEY, username TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS public.roles (
                id BIGINT PRIMARY KEY, name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS public.user_roles (
                user_id BIGINT NOT NULL, role_id BIGINT NOT NULL,
                PRIMARY KEY (user_id, role_id)
            );
            CREATE TABLE IF NOT EXISTS public.user_clients (
                user_id BIGINT NOT NULL, brand_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, brand_id)
            );
            CREATE TABLE IF NOT EXISTS public.client_profiles (
                id BIGSERIAL PRIMARY KEY, brand_id INTEGER NOT NULL,
                brand_display_names TEXT, is_deleted INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS public.brand_aliases (
                id BIGSERIAL PRIMARY KEY, canonical_name TEXT NOT NULL,
                alias TEXT NOT NULL, brand_id INTEGER, source TEXT DEFAULT 'manual',
                UNIQUE (canonical_name, alias)
            );
            """
        )
        for script in (
            "migration_monitoring_yuanbao_default_2026_07_20.sql",
            "migration_monitoring_product_matrix_2026_07_21.sql",
            "migration_monitoring_identity_review_2026_07_21.sql",
            "migration_diagnosis_identity_review_2026_07_22.sql",
        ):
            cursor.execute((ROOT / "scripts" / script).read_text(encoding="utf-8"))
    conn.close()

    def drop():
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
                )
        finally:
            admin.close()

    return connect, monitoring_db, drop


@pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")
def test_absent_action_is_guarded_by_the_candidates_actually_on_the_card(monkeypatch):
    """[返修单 R1 · P0] 「没有出现」(no + 空名称)必须走**与 `no + 带名` 同一道**可信名守卫。

    🔴 修复前的结构性洞:absent 分支把 `decision_names` 置空,守卫写成
       `any(... for name_key, _ in decision_names)` ⇒ 对空集恒 False ⇒ 守卫失效。
       两个按钮表达同一个运营意图(「都不是这些品牌」/「没有出现」),修复前只有
       一个受保护 —— 换个按钮就能把「回答里明确提到本品牌可信名」的结果写成
       "未提及"并计进客户可见的出现率,且**无撤销路径**。

    本用例同时含正反两向,故两种变异都会把它打红:
      · 守卫恒假(退回读 `decision_names`)→ ① 组转红(该拦没拦);
      · 守卫恒真 → ②③ 组转红(把 §1 的死锁修回来了)。
    """
    connect, monitoring_db, drop = _provision_identity_review_database(monkeypatch, "monid_guard")
    try:
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.users (id, username) VALUES (8202, 'monid-guard-owner');
                INSERT INTO public.brands
                    (name, company_name, owner_user_id, brand_display_names, is_deleted)
                VALUES ('瀚海云仓', '瀚海云仓科技有限公司', 8202, '["瀚海仓储"]', FALSE)
                RETURNING id;
                """
            )
            brand_id = int(cursor.fetchone()["id"])
            # 第四个可信名来源:brand_aliases 里 source='manual' 的一行(守卫在事务内现查)
            cursor.execute(
                "INSERT INTO public.brand_aliases (canonical_name, alias, brand_id, source) "
                "VALUES ('瀚海云仓', '瀚海物流', %s, 'manual')",
                (brand_id,),
            )
            cursor.execute(
                """
                INSERT INTO public.monitoring_tasks
                    (client_id, brand_id, task_name, keyword_count, platform_count,
                     total_tests, completed_tests, status, trigger_type)
                VALUES ('monid-guard', %s, 'monid guard', 8, 8, 8, 8, 'completed', 'manual')
                RETURNING id
                """,
                (brand_id,),
            )
            task_id = int(cursor.fetchone()["id"])
        conn.close()

        padding = "本行业各家厂商在仓配时效与履约成本上的公开资料对比。" * 12

        def save_pending(tag: str, candidates: list[str], full_response: str) -> dict:
            assert monitoring_db.batch_save_results([{
                "task_id": task_id,
                "keyword": f"{tag} 仓配测试词",
                "platform": "deepseek",
                "brand_id": brand_id,
                "identity_brand_id": brand_id,
                "identity_review_state": "pending",
                "identity_candidates": candidates,
                "identity_evidence_snippet": full_response[:200],
                "is_detected": False,
                "mention_type": "pending_identity",
                "response_status": "brand_identity_unresolved",
                "full_response": full_response,
                "response_snippet": full_response[:120],
                "sent_question_snapshot": f"{tag} 仓配测试词",
                "provider": "deepseek",
                "model": "deepseek-model",
                "surface": "monitoring",
                "target_brand": "瀚海云仓",
            }]) == 1
            rows = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)
            return next(r for r in rows if r["keyword"] == f"{tag} 仓配测试词")

        def result_row(result_id: int) -> dict:
            conn_local = connect()
            with conn_local, conn_local.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM public.monitoring_results WHERE id = %s", (result_id,)
                )
                row = dict(cursor.fetchone())
            conn_local.close()
            return row

        def name_decision_rows() -> dict:
            conn_local = connect()
            with conn_local, conn_local.cursor() as cursor:
                cursor.execute(
                    "SELECT normalized_name, decision FROM public.monitoring_identity_name_decisions "
                    "WHERE brand_id = %s",
                    (brand_id,),
                )
                rows = {r["normalized_name"]: r["decision"] for r in cursor.fetchall()}
            conn_local.close()
            return rows

        def decision_event_count(result_id: int) -> int:
            conn_local = connect()
            with conn_local, conn_local.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM public.monitoring_identity_decision_events "
                    "WHERE result_id = %s",
                    (result_id,),
                )
                count = int(cursor.fetchone()["count"])
            conn_local.close()
            return count

        # ==== ① 候选**非空且含可信名** + absent → 必须被拦 =====================
        # 四个可信名来源逐个验(brands.name / company_name / brand_display_names /
        # brand_aliases[source='manual']),再加一格"同行名 + 可信名混合候选"。
        trusted_cases = {
            "brand_name": ["瀚海云仓"],
            "company_name": ["瀚海云仓科技有限公司"],
            "display_names": ["瀚海仓储"],
            "manual_alias": ["瀚海物流"],
            "mixed": ["云滴优选", "瀚海仓储"],
        }
        guarded_rows = {}
        for tag, candidates in trusted_cases.items():
            guarded_rows[tag] = save_pending(
                tag, candidates, f"推荐{candidates[-1]}，{padding}"
            )

        for tag, row in guarded_rows.items():
            before = result_row(row["id"])
            names_before = name_decision_rows()
            with pytest.raises(monitoring_db.MonitoringIdentityReviewConflict) as excinfo:
                monitoring_db.decide_monitoring_identity_review(
                    result_id=row["id"], brand_id=brand_id, actor_user_id=8202,
                    action="no", selected_name="", expected_version=0,
                    evidence_hash=row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
                )
            # absent 的文案必须与「整组标记为不是」区分开:界面上没有"整组"这个动作,
            # 只回旧文案运营不知道下一步该干嘛。
            assert "不能直接记为未出现" in str(excinfo.value), tag
            assert "请逐条判断该候选" in str(excinfo.value), tag

            # 该条**仍在待确认队列**、`monitoring_results` 一列未改、无审计事件、
            # 别名决策表零写入(整笔事务回滚)
            pending_ids = {
                r["id"] for r in monitoring_db.list_pending_monitoring_identity_reviews(brand_id)
            }
            assert row["id"] in pending_ids, f"{tag}: 被拦的条目必须留在待确认队列"
            assert result_row(row["id"]) == before, f"{tag}: monitoring_results 不得有任何一列改动"
            assert decision_event_count(row["id"]) == 0, tag
            assert name_decision_rows() == names_before, tag

        # ==== ② 反向对照:候选非空但**不含**可信名 + absent → 必须成功 ==========
        # (证明没把「整段全在讲同行 ⇒ 运营无出口」的死锁修回来)
        rival_row = save_pending(
            "rivals", ["云滴优选", "蓝湾仓储"],
            f"回答列出了云滴优选、蓝湾仓储和木野家居三家。{padding}",
        )
        names_before_rivals = name_decision_rows()
        rival = monitoring_db.decide_monitoring_identity_review(
            result_id=rival_row["id"], brand_id=brand_id, actor_user_id=8202,
            action="no", selected_name="", expected_version=0,
            evidence_hash=rival_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert rival["status"] == "resolved"
        assert rival["is_detected"] is False
        assert rival["mention_type"] == "none"
        assert rival["rejudge_verdict"] == "human_confirmed_absent"
        stored = result_row(rival_row["id"])
        assert stored["identity_review_state"] == "rejected"
        assert stored["response_status"] == "success"
        assert int(stored["is_detected"]) == 0
        assert stored["mention_type"] == "none"
        assert stored["target_outcome"] == "not_mentioned"
        # absent 成功路径:跨回答复用的别名决策表零写入
        assert name_decision_rows() == names_before_rivals

        # ==== ③ 反向对照:候选为空 + absent → 维持既有成功行为 ==================
        empty_row = save_pending("empty", [], f"回答只讲了行业整体趋势。{padding}")
        empty = monitoring_db.decide_monitoring_identity_review(
            result_id=empty_row["id"], brand_id=brand_id, actor_user_id=8202,
            action="no", selected_name="", expected_version=0,
            evidence_hash=empty_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert empty["status"] == "resolved"
        assert empty["rejudge_verdict"] == "human_confirmed_absent"
        assert int(result_row(empty_row["id"])["is_detected"]) == 0

        # ==== ④ 回归对照:`no + 带名"可信名"` 仍被拦(既有行为,文案仍是旧那条)===
        # 用 ① 里同一张卡:两个按钮表达同一个运营意图,修复后**两边都被拦**。
        display_row = guarded_rows["display_names"]
        with pytest.raises(monitoring_db.MonitoringIdentityReviewConflict) as named_exc:
            monitoring_db.decide_monitoring_identity_review(
                result_id=display_row["id"], brand_id=brand_id, actor_user_id=8202,
                action="no", selected_name="瀚海仓储", expected_version=0,
                evidence_hash=display_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
            )
        assert "不能整组标记为不是" in str(named_exc.value)

        # ==== ⑤ 反向对照:`no + 带名` 走 candidate_set_rejected 时**必须**写别名表 ==
        # (否则 ② 里"零写入"那条判据零判别力 —— 恒不写也全绿)
        rejected_row = save_pending(
            "rejected", ["星桥严选", "木野家居"],
            f"回答推荐了星桥严选和木野家居。{padding}",
        )
        rejected = monitoring_db.decide_monitoring_identity_review(
            result_id=rejected_row["id"], brand_id=brand_id, actor_user_id=8202,
            action="no", selected_name="星桥严选", expected_version=0,
            evidence_hash=rejected_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        assert rejected["status"] == "resolved"
        written = name_decision_rows()
        assert written.get("星桥严选") == "negative"
        assert written.get("木野家居") == "negative"

        # ==== ⑥ 既有约束不退化:yes / custom + 空名称仍被拒 =====================
        yes_empty_row = save_pending("yesempty", ["云滴优选"], f"回答提到云滴优选。{padding}")
        with pytest.raises(ValueError, match="请选择候选名称"):
            monitoring_db.decide_monitoring_identity_review(
                result_id=yes_empty_row["id"], brand_id=brand_id, actor_user_id=8202,
                action="yes", selected_name="", expected_version=0,
                evidence_hash=yes_empty_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
            )
        with pytest.raises(ValueError, match="请选择候选名称|请填写有效的品牌名称"):
            monitoring_db.decide_monitoring_identity_review(
                result_id=yes_empty_row["id"], brand_id=brand_id, actor_user_id=8202,
                action="custom", selected_name="   ", expected_version=0,
                evidence_hash=yes_empty_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
            )
    finally:
        drop()


# ===========================================================================
# 后端 · 源码钉:重判必须走 resolve_local(零 provider)
# ===========================================================================
def test_decision_rejudge_is_provider_free_source_pin():
    src = DB_SRC.read_text(encoding="utf-8")
    body = src[src.index("def decide_monitoring_identity_review"):]
    body = body[: body.index("\ndef ", 10)]
    assert ".resolve_local(answer_text)" in body
    assert "await" not in body  # 同步函数,物理不可能调异步 provider 链
    assert "llm_track" not in body
    # 一刀切已废:计入必须经重判裁决(变异恢复 decision == "positive" 一刀切 → 转红)
    assert 'is_detected = decision == "positive"' not in body
    assert "rejudge" in body and "BrandVerdict.YES" in body


# ===========================================================================
# 前端 · 源码钉(行为面复用既有 workbench 套件)
# ===========================================================================
def test_panel_shows_candidate_context_with_highlight():
    """③ 确认卡逐候选展示原文上下文片段并高亮定位。变异(删 mark 渲染)→ 转红。"""
    src = PANEL.read_text(encoding="utf-8")
    assert "function candidateContext(" in src
    assert 'data-testid="candidate-context"' in src
    assert "<mark" in src and "「{context.hit}」" in src
    # 找不到片段时如实说明,不是空白
    assert "请在上方回答片段内定位后再判断" in src


def test_panel_copy_says_only_n_pending_and_rest_counted():
    """④ 面板副标题:仅 N 条待确认、其余已计入。变异(回退旧文案)→ 转红。"""
    src = PANEL.read_text(encoding="utf-8")
    assert "仅这 ${items.length} 条待确认" in src
    assert "其余监测结果已正常计入" in src
    assert "确认后才会计入监测结果" not in src  # 旧文案(暗示整轮作废)必须消失


def test_panel_container_has_horizontal_padding_and_testid():
    """⑤ 错版修复:容器必须带横向内边距。

    🔴 [2026-08-03 订正] 本条在生产尖 `c8664f6a` 上**本来就是红的**:
    面板按 Owner 裁定由 `border-y` 全出血带改成同款圆角卡,内边距写法随之
    由 `px-4 py-4` 合并成**等价的 `p-4`**,而这里断言的是**字面 `px-4`**。
    改动与断言等价、行为无变化,断言却红 —— 这正是"源码字符串断言"的脆弱处:
    换个等价写法就假红,换个等价 bug 就假绿。

    所以这里放宽到"两种等价写法都算数",并把真正的判据交给**行为锁**:
    `frontend/tests/identity-full-answer/full-answer.spec.ts` 锁8 用
    getComputedStyle 断言 paddingLeft/paddingRight > 0 且四边有边框 —— 那条才是
    改成任何等价/非等价写法都能正确判定的。
    """
    src = PANEL.read_text(encoding="utf-8")
    section_line = next(line for line in src.splitlines() if 'aria-labelledby="identity-review-title"' in line)
    assert ("px-4" in section_line) or ("p-4" in section_line), (
        "容器丢了横向内边距(px-* 或 p-* 都没有)"
    )
    assert 'data-testid="identity-review-panel"' in section_line


def test_panel_reports_decision_back_for_writeback():
    """① 确认成功 → toast 如实回显计入结论 + onDecided 回写回调。"""
    src = PANEL.read_text(encoding="utf-8")
    assert "onDecided?: (info: { resultId: number; detected: boolean | null }) => void;" in src
    assert "onDecided?.({ resultId: item.id, detected });" in src
    assert "已确认 · 本条已重新判定并计入统计" in src
    assert "已确认 · 本条按「未提及」计入统计" in src
    # 既有耐久锁不回退:30s 轮询 + 幂等 request_id + AbortController
    assert "setTimeout(poll, 30_000)" in src and "decisionRequestIds" in src


def test_monitoring_page_wires_writeback_refresh():
    """① 确认后页面回写:重取任务详情 + 刷新关键词统计。变异(摘 onDecided)→ 转红。"""
    src = MON_INDEX.read_text(encoding="utf-8")
    wiring = src[src.index("<IdentityReviewPanel"):]
    wiring = wiring[: wiring.index(")}", 0) + 2]
    assert "onDecided={() => {" in wiring
    assert "hydrateMonitoringTask(progressTaskId, false)" in wiring
    assert "fetchKeywords(parseInt(selectedClient))" in wiring
    # hydrate 映射回写"已确认"终态标记
    assert "identityConfirmed: ['confirmed', 'rejected'].includes(result.identity_review_state)" in src


def test_progress_panel_rows_and_summary_reflect_confirmation():
    """①④ 进度面板:行级"已确认 · 已计入"回写 + 待确认摘要(仅 N 条,其余已计入)。"""
    src = PROGRESS.read_text(encoding="utf-8")
    assert "已确认 · 已计入(检出)" in src
    assert "已确认 · 已计入(未提及)" in src
    assert "仅此条待确认 · 暂未计入" in src
    assert 'data-testid="identity-pending-summary"' in src
    assert "其余 {counted} 条已正常计入" in src
    # 详情文案不再暗示整轮作废
    assert "仅这 1 条待人工确认，同轮其他结果均已正常计入" in src
    assert "本次结果不计入检出率；为避免重复请求" not in src
