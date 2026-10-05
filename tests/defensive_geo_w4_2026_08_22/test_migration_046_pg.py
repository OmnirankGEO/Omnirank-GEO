"""迁移 046 的**结构承重**判据 —— 真 PG16。

这一批打的不是"函数返回值对不对",是**库里的那三条约束在不在**。
理由:MON-03「重试不覆盖原始失败」是 H0-DATA。应用层那条
``WHERE terminal_state IS NULL`` 只防我们自己写错;库里的触发器防的是
**将来某个人在别处写一条 UPDATE**。两层是纵深,判据必须**分别**拆。
"""

from __future__ import annotations

import psycopg2
import pytest

from tests.defensive_geo_w4_2026_08_22.conftest import (
    DML_STATEMENT,
    EXPECTED_TABLES,
    PACKAGE_OWNED_MIGRATIONS,
    ROOT,
    _read_sql,
    strip_sql_comments,
)


def _insert_attempt(cur, *, attempt_id, plan_cell_id, ordinal,
                    terminal=None, error=None, provider_called=True):
    cur.execute(
        """
        INSERT INTO defgeo_monitoring_attempts
            (attempt_id, plan_cell_id, attempt_ordinal, run_authority_id,
             tenant_owner_user_id, brand_id, actual_provider, actual_model,
             actual_surface, actual_search_mode, request_hash,
             provider_called, terminal_state, error_code, terminal_at,
             ledger_version)
        VALUES (%s,%s,%s,'ra1',7,1,'doubao','doubao-pro','ai_search','ai_search',
                'rh', %s, %s, %s, CASE WHEN %s IS NULL THEN NULL ELSE NOW() END,
                'v1')
        """,
        (attempt_id, plan_cell_id, ordinal, provider_called, terminal, error,
         terminal),
    )


# ══════════════════════════════════════════════════════════════════════
# MIG-01:空库装得起来 + 重放幂等
# ══════════════════════════════════════════════════════════════════════

class TestMigrationApplies:
    def test_all_five_tables_exist(self, cur):
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename = ANY(%s)", (list(EXPECTED_TABLES),))
        assert {r["tablename"] for r in cur.fetchall()} == set(EXPECTED_TABLES)

    def test_replay_is_idempotent(self, cur):
        """MIG-01:prestart 每次部署**无条件重放**全部迁移。

        重放炸 = 每次部署都炸。这条判据跑的是真的第二遍。
        """
        cur.execute(_read_sql(PACKAGE_OWNED_MIGRATIONS[0]))
        cur.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname='public' "
            "AND tablename = ANY(%s)", (list(EXPECTED_TABLES),))
        assert cur.fetchone()["n"] == len(EXPECTED_TABLES)

    def test_migration_body_has_zero_dml(self):
        """铁律:迁移体内零 DML(prestart 无条件重放 ⇒ DML 是常驻地雷)。

        🔴 先去注释再普查 —— 不去的话,迁移里那句「体内零 DML」的**说明**
           自己会被判成违规(本仓记过:引用裁决原文触发裸串结构锚)。
        """
        sql = strip_sql_comments(_read_sql(PACKAGE_OWNED_MIGRATIONS[0]))
        hits = DML_STATEMENT.findall(sql)
        assert not hits, f"迁移体内出现 DML:{hits}"

    def test_dml_detector_is_alive(self):
        """探测器活性自证:喂一段真 DML 必须被抓到。

        没有这一条,上面那条"零 DML"既可能是真没有,
        也可能是正则压根匹配不上 —— 两者长得一样。
        """
        assert DML_STATEMENT.findall("INSERT INTO t (a) VALUES (1);")
        assert DML_STATEMENT.findall("UPDATE t SET a=1;")
        assert DML_STATEMENT.findall("DELETE FROM t;")
        # 成对负样本:DDL 与注释里的词必须**不**命中
        assert not DML_STATEMENT.findall("CREATE TABLE t (a int);")
        assert not DML_STATEMENT.findall(
            strip_sql_comments("-- 本文件体内零 DML,没有 INSERT INTO 任何表\n"))


# ══════════════════════════════════════════════════════════════════════
# 承重约束 ①②③
# ══════════════════════════════════════════════════════════════════════

class TestAttemptLedgerLoadBearingConstraints:
    def test_duplicate_ordinal_on_same_cell_is_rejected(self, cur):
        """承重① ``UNIQUE(plan_cell_id, attempt_ordinal)``。

        拆红:把迁移里那条 ADD CONSTRAINT 删掉 —— 本判据立刻红。
        """
        _insert_attempt(cur, attempt_id="a" * 64, plan_cell_id="c1" + "0" * 62,
                        ordinal=1, terminal="engine_error", error="TIMEOUT")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_attempt(cur, attempt_id="b" * 64,
                            plan_cell_id="c1" + "0" * 62, ordinal=1,
                            terminal="answered")

    def test_second_attempt_with_next_ordinal_is_accepted(self, cur):
        """成对正样本:ordinal+1 必须**放行**(证明上一条不是"这张表插不进去")。"""
        pc = "c2" + "0" * 62
        _insert_attempt(cur, attempt_id="c" * 64, plan_cell_id=pc, ordinal=1,
                        terminal="engine_error", error="RATE_LIMITED")
        _insert_attempt(cur, attempt_id="d" * 64, plan_cell_id=pc, ordinal=2,
                        terminal="answered")
        cur.execute("SELECT count(*) AS n FROM defgeo_monitoring_attempts "
                    "WHERE plan_cell_id=%s", (pc,))
        assert cur.fetchone()["n"] == 2

    def test_terminal_row_cannot_be_updated(self, cur):
        """承重② 终态不可改写 —— MON-03 的**结构性**承重点。

        这是本包唯一一条"应用层已经有 CAS 了还要再来一道"的地方。
        理由写在迁移注释里:CAS 防我们自己写错,触发器防将来别处那条 UPDATE。
        拆红:把 ``trg_defgeo_attempt_terminal_immutable`` DROP 掉。
        """
        pc = "c3" + "0" * 62
        _insert_attempt(cur, attempt_id="e" * 64, plan_cell_id=pc, ordinal=1,
                        terminal="engine_error", error="TIMEOUT")
        with pytest.raises(psycopg2.errors.CheckViolation, match="不可改写"):
            cur.execute(
                "UPDATE defgeo_monitoring_attempts SET error_code=NULL, "
                "error_message=NULL WHERE attempt_id=%s", ("e" * 64,))

    def test_inflight_row_can_still_be_closed(self, cur):
        """成对正样本:在飞行的那一行**必须**能被收成终态。

        没有这一条,上一条判据无法区分「只挡终态行」与「整张表不许 UPDATE」。
        """
        pc = "c4" + "0" * 62
        _insert_attempt(cur, attempt_id="f" * 64, plan_cell_id=pc, ordinal=1,
                        terminal=None)
        cur.execute(
            "UPDATE defgeo_monitoring_attempts SET terminal_state='answered', "
            "terminal_at=NOW() WHERE attempt_id=%s AND terminal_state IS NULL",
            ("f" * 64,))
        assert cur.rowcount == 1

    def test_two_inflight_attempts_on_one_cell_are_rejected(self, cur):
        """承重③ partial unique:同一格同时至多一个在飞 attempt。"""
        pc = "c5" + "0" * 62
        _insert_attempt(cur, attempt_id="1" * 64, plan_cell_id=pc, ordinal=1,
                        terminal=None)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_attempt(cur, attempt_id="2" * 64, plan_cell_id=pc, ordinal=2,
                            terminal=None)

    def test_many_terminal_attempts_on_one_cell_are_fine(self, cur):
        """成对正样本:历史尝试可以有任意多条 —— partial 只约束在飞的那一条。"""
        pc = "c6" + "0" * 62
        for i, aid in enumerate(("3" * 64, "4" * 64, "5" * 64), start=1):
            _insert_attempt(cur, attempt_id=aid, plan_cell_id=pc, ordinal=i,
                            terminal="engine_error", error="TIMEOUT")
        cur.execute("SELECT count(*) AS n FROM defgeo_monitoring_attempts "
                    "WHERE plan_cell_id=%s", (pc,))
        assert cur.fetchone()["n"] == 3

    def test_engine_error_without_code_is_rejected(self, cur):
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_attempt(cur, attempt_id="6" * 64,
                            plan_cell_id="c7" + "0" * 62, ordinal=1,
                            terminal="engine_error", error=None)

    def test_policy_skipped_must_not_claim_a_provider_call(self, cur):
        """MON-11:policy_skipped 恒为零 provider 调用。"""
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_attempt(cur, attempt_id="7" * 64,
                            plan_cell_id="c8" + "0" * 62, ordinal=1,
                            terminal="policy_skipped", provider_called=True)

    def test_empty_model_revision_is_rejected_but_null_is_fine(self, cur):
        """§6.1:NULL 与空串不同义 —— 空串会让两个不同身份撞成一个。"""
        cur.execute(
            "INSERT INTO defgeo_monitoring_attempts (attempt_id, plan_cell_id, "
            "attempt_ordinal, run_authority_id, tenant_owner_user_id, brand_id, "
            "actual_provider, actual_model, actual_model_revision, actual_surface, "
            "actual_search_mode, request_hash, ledger_version) "
            "VALUES (%s,%s,1,'ra',7,1,'p','m',NULL,'s','sm','rh','v1')",
            ("8" * 64, "c9" + "0" * 62))
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO defgeo_monitoring_attempts (attempt_id, plan_cell_id, "
                "attempt_ordinal, run_authority_id, tenant_owner_user_id, brand_id, "
                "actual_provider, actual_model, actual_model_revision, actual_surface, "
                "actual_search_mode, request_hash, ledger_version) "
                "VALUES (%s,%s,1,'ra',7,1,'p','m','','s','sm','rh','v1')",
                ("9" * 64, "ca" + "0" * 62))


class TestDefensiveProjectionConstraints:
    def _insert(self, cur, **over):
        row = dict(projection_id="p" * 64, monitoring_result_id=1,
                   tenant_owner_user_id=7, brand_id=1, plan_item_key="pk",
                   question_revision=1, projection_version="v1",
                   identity_state="confirmed", completeness="complete",
                   support_level="corroborated", has_conflict=False,
                   conflicting_facts=[], adjudication_state="not_required",
                   adjudicated_by=None)
        row.update(over)
        cur.execute(
            "INSERT INTO defgeo_monitoring_defensive_projections "
            "(projection_id, monitoring_result_id, tenant_owner_user_id, brand_id, "
            " plan_item_key, question_revision, projection_version, identity_state, "
            " completeness, support_level, has_conflict, conflicting_facts, "
            " evidence_manifest_revision, adjudication_state, adjudicated_by) "
            "VALUES (%(projection_id)s,%(monitoring_result_id)s,"
            "%(tenant_owner_user_id)s,%(brand_id)s,%(plan_item_key)s,"
            "%(question_revision)s,%(projection_version)s,%(identity_state)s,"
            "%(completeness)s,%(support_level)s,%(has_conflict)s,"
            "%(conflicting_facts)s,'em1',%(adjudication_state)s,%(adjudicated_by)s)",
            row)

    def test_merged_support_level_cannot_even_be_stored(self, cur):
        """§13.1「禁止复活 unsupported_or_conflicting」——库层就挡掉。"""
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(cur, projection_id="q" * 64,
                         support_level="unsupported_or_conflicting")

    def test_corroborated_with_conflict_is_storable(self, cur):
        """MET-31 成对正样本:合法并存必须**存得进去**。"""
        self._insert(cur, projection_id="r" * 64, support_level="corroborated",
                     has_conflict=True, conflicting_facts=["addr"])

    def test_conflict_flag_must_match_the_facts_array(self, cur):
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(cur, projection_id="s" * 64, has_conflict=True,
                         conflicting_facts=[])

    def test_ai_adjudication_without_version_is_rejected(self, cur):
        """R-1 留痕:``ai:<model>@<version>``,没有 @version 存不进去。"""
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(cur, projection_id="t" * 64,
                         adjudication_state="ai_adjudicated",
                         adjudicated_by="ai:qwen3-max")
        self._insert(cur, projection_id="u" * 64,
                     adjudication_state="ai_adjudicated",
                     adjudicated_by="ai:qwen3-max@2026-08")


class TestSnapshotAndRenewalConstraints:
    def test_snapshot_inplace_state_change_is_rejected(self, cur):
        """POR-15 的库层承重点。"""
        cur.execute(
            "INSERT INTO defgeo_report_snapshots (report_snapshot_id, revision, "
            " tenant_owner_user_id, brand_id, state, content_hash, plan_snapshot_id, "
            " plan_snapshot_hash, sampling_window_start, sampling_window_end, "
            " cutoff_at, input_watermark, raw_result_ids, entity_resolver_version, "
            " outcome_classifier_version, evidence_extractor_version, "
            " metric_definition_version, snapshot_version) "
            "VALUES ('rs1',1,7,1,'processing',%s,'ps1',%s,NOW(),NOW(),NOW(),'wm',"
            " ARRAY[1,2],'r1','c1','e1','m1','sv1')", ("h" * 64, "g" * 64))
        with pytest.raises(psycopg2.errors.CheckViolation, match="原地改状态"):
            cur.execute("UPDATE defgeo_report_snapshots SET state='ready' "
                        "WHERE report_snapshot_id='rs1' AND revision=1")

    def test_renewal_requires_exactly_six_signals(self, cur):
        """§13.4 六维 —— 少一维就是挑着说。"""
        import json
        six = json.dumps([{"signal": f"s{i}"} for i in range(6)])
        five = json.dumps([{"signal": f"s{i}"} for i in range(5)])
        cur.execute(
            "INSERT INTO defgeo_renewal_recommendations (recommendation_id, "
            " tenant_owner_user_id, brand_id, basis_report_snapshot_id, level, "
            " comparability_level, signals, recommendation_version) "
            "VALUES ('rr1',7,1,'rs1','renew_recommended','full',%s,'v1')", (six,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO defgeo_renewal_recommendations (recommendation_id, "
                " tenant_owner_user_id, brand_id, basis_report_snapshot_id, level, "
                " comparability_level, signals, recommendation_version) "
                "VALUES ('rr2',7,1,'rs1','renew_recommended','full',%s,'v1')", (five,))

    def test_non_comparable_cannot_recommend_renewal(self, cur):
        """§13.2「不可比时不算涨跌」的库层下游。"""
        import json
        six = json.dumps([{"signal": f"s{i}"} for i in range(6)])
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO defgeo_renewal_recommendations (recommendation_id, "
                " tenant_owner_user_id, brand_id, basis_report_snapshot_id, level, "
                " comparability_level, signals, recommendation_version) "
                "VALUES ('rr3',7,1,'rs1','renew_recommended','none',%s,'v1')", (six,))

    def test_renewal_table_has_zero_money_columns(self, cur):
        """§13.4「不自动扣费」的 schema 层证明:表上根本没有钱的落脚点。

        分母从 ``information_schema`` **机械取**,不手抄列名。
        """
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='defgeo_renewal_recommendations'")
        cols = {r["column_name"] for r in cur.fetchall()}
        money = {c for c in cols if any(
            t in c for t in ("amount", "points", "price", "freeze", "wallet", "fee"))}
        assert not money, f"续费表出现资金列 {sorted(money)}"


class TestFrozenReasonConstraints:
    def _insert(self, cur, text, ref="fr1"):
        cur.execute(
            "INSERT INTO defgeo_xiaobang_frozen_reasons (reason_ref, "
            " tenant_owner_user_id, brand_id, subject_kind, subject_ref, "
            " reason_code, public_text, next_action_kind, reasons_version) "
            "VALUES (%s,7,1,'report_snapshot','rs1','rc1',%s,'view_report','v1')",
            (ref, text))

    def test_business_numbers_cannot_be_stored(self, cur):
        """§9.6:库层下限 —— 绕过服务端直接写库也塞不进价格。"""
        for bad in ("大约 300 算力", "¥1200 起", "只要 500 元"):
            with pytest.raises(psycopg2.errors.CheckViolation):
                self._insert(cur, bad, ref=f"bad-{bad[:4]}")

    def test_plain_reason_is_stored(self, cur):
        """成对正样本:人话理由必须存得进去。"""
        self._insert(cur, "这家媒体的读者与你的客户重合度高,适合先发这一篇。")
