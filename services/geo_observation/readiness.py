"""web/cron 启动 fail-closed schema readiness(只读,零 DDL)。

catches post-migration tamper(约束被删/篡改/未 VALIDATE/唯一门被改)→ RaiseError 拒绝启动。
镜像 migration 反查的关键断言(services/admin_user_governance_schema.py 的 Python readiness 形状)。
"""
from __future__ import annotations

import re
from typing import Optional

from db.connection import get_connection

# 金标准门 CHECK 的规范定义(pg_get_constraintdef 去空白后)——契约§601 阈值 100/9000/0 + 数据集/哈希非空。
# 任何篡改(CHECK(TRUE)/降阈值/部分弱化如 OR report_hash IS NOT NULL/自引用)都改变此串 → readiness 拦下。
_GOLD_GATE_CHECK_NORM = (
    "CHECK(((outcome_gold_gate_passed=false)OR((COALESCE(gold_sample_count,'-1'::integer)>=100)"
    "AND(COALESCE(gold_macro_f1_bps,'-1'::integer)>=9000)AND(COALESCE(gold_high_risk_false_reco,'-1'::integer)=0)"
    "AND(gold_dataset_versionISNOTNULL)AND(gold_report_hashISNOTNULL))))"
)
# gold_eval append-only 触发器函数体 + 触发器定义的规范形(pg_get_functiondef / pg_get_triggerdef 去空白后)。
# 掏空/条件放行/改写函数体、retarget/改 events/删 FOR EACH ROW 都改变这两串 → readiness 拦下(源级 pin,行为不可伪装)。
_IMMUT_FN_NORM = (
    "CREATEORREPLACEFUNCTIONpublic.geo_obs_gold_eval_immutable()RETURNStriggerLANGUAGEplpgsqlAS$function$"
    "BEGINRAISEEXCEPTION'geo_observation_gold_evalisappend-onlyimmutable;UPDATE/DELETEforbidden';END;$function$"
)
_IMMUT_TRIGGER_NORM = (
    "CREATETRIGGERtrg_geo_obs_gold_eval_immutableBEFOREDELETEORUPDATEON"
    "public.geo_observation_gold_evalFOREACHROWEXECUTEFUNCTIONgeo_obs_gold_eval_immutable()"
)
# gate_passed 一致性 CHECK 与 policy→gold_eval 外键的规范定义(pg_get_constraintdef 去空白)。
# 只核名/表/convalidated 不够:同名换 CHECK(TRUE) / 外键改指伪表或错列都能骗过 → 必须精确匹配定义。
_GOLD_DERIVED_CHECK_NORM = (
    "CHECK((gate_passed=((sample_count>=100)AND(macro_f1_bps>=9000)AND(high_risk_false_reco=0)"
    "AND(length(dataset_version)>0)AND(length(report_hash)>0))))"
)
_GOLD_FK_NORM = (
    "FOREIGNKEY(gold_dataset_version,gold_report_hash)"
    "REFERENCESgeo_observation_gold_eval(dataset_version,report_hash)"
)
_COLLECTION_MODE_CHECK_NORM = (
    "CHECK((collection_mode=ANY(ARRAY['existing_collectors_reconciled'::text,"
    "'native_sampling_driver'::text])))"
)
_POLICY_ACTIVE_BASIS_CHECK_NORM = (
    "CHECK(((active_aggregate_policy_basisISNULL)OR"
    "(active_aggregate_policy_basis~'^[0-9a-f]{64}$'::text)))"
)
_AGGREGATE_BASIS_CHECK_NORM = (
    "CHECK(((policy_basis_hashISNULL)OR"
    "(policy_basis_hash~'^[0-9a-f]{64}$'::text)))"
)
_AGGREGATE_ELIGIBILITY_CHECK_NORM = "CHECK((eligibility_epoch>=0))"
_AGGREGATE_PROMOTION_WATERMARK_CHECK_NORM = "CHECK((promotion_sequence_watermark>=0))"
_EVENT_PROMOTION_SEQ_CHECK_NORM = (
    "CHECK((((processing_state='promoted'::text)AND(promotion_seqISNOTNULL))OR"
    "(processing_state='withdrawn'::text)OR((processing_state<>ALL(ARRAY["
    "'promoted'::text,'withdrawn'::text]))AND(promotion_seqISNULL))))"
)
_ELIGIBILITY_SCOPE_CHECK_NORM = (
    "CHECK((scope_type=ANY(ARRAY['private_brand'::text,'public_industry'::text])))"
)
_ELIGIBILITY_NONNEGATIVE_CHECK_NORM = "CHECK((epoch>=0))"
_MANIFEST_BASIS_CHECK_NORM = "CHECK((policy_basis_hash~'^[0-9a-f]{64}$'::text))"
_MANIFEST_SCOPE_CHECK_NORM = (
    "CHECK((scope_type=ANY(ARRAY['private_brand'::text,'public_industry'::text])))"
)
_MANIFEST_GRANULARITY_CHECK_NORM = (
    "CHECK((bucket_granularity=ANY(ARRAY['day'::text,'week'::text,'month'::text])))"
)
_MANIFEST_BOUNDS_CHECK_NORM = "CHECK((bucket_end>=bucket_start))"
_MANIFEST_COUNTS_CHECK_NORM = (
    "CHECK(((eligibility_epoch>=0)AND(promotion_sequence_watermark>=0)"
    "AND(eligible_observation_count>=0)AND(expected_scope_cell_count>=0)"
    "AND(overall_cell_count>=0)AND(aggregate_row_count>=0)))"
)
_MANIFEST_FINGERPRINTS_CHECK_NORM = (
    "CHECK(((expected_scope_cell_fingerprint~'^[0-9a-f]{64}$'::text)"
    "AND(aggregate_key_fingerprint~'^[0-9a-f]{64}$'::text)))"
)
_INSIGHT_STATE_CHECK_NORM = (
    "CHECK((state=ANY(ARRAY['pending'::text,'running'::text,'paid_call_started'::text,"
    "'completed'::text,'failed'::text,'result_unknown'::text])))"
)
_INSIGHT_SNAPSHOT_CHECK_NORM = (
    "CHECK((((snapshot_idISNULL)AND(state='result_unknown'::text)AND(policy_basis_hashISNULL)AND(scope_typeISNULL)AND"
    "(bucket_granularityISNULL)AND(bucket_startISNULL)AND(bucket_epochISNULL)AND"
    "(promotion_sequence_watermarkISNULL)AND(aggregate_input_watermarkISNULL)AND"
    "(aggregate_contract_versionISNULL)AND(aggregate_aggregation_versionISNULL)AND"
    "(aggregate_metric_versionISNULL))OR((snapshot_idISNOTNULL)AND"
    "(snapshot_id~'^[0-9a-f]{64}$'::text)AND(policy_basis_hashISNOTNULL)AND"
    "(policy_basis_hash~'^[0-9a-f]{64}$'::text)AND(scope_typeISNOTNULL)AND"
    "(scope_type='private_brand'::text)AND(bucket_granularityISNOTNULL)AND"
    "(bucket_granularity=ANY(ARRAY['day'::text,'week'::text,'month'::text]))AND"
    "(bucket_startISNOTNULL)AND(bucket_epochISNOTNULL)AND(bucket_epoch>=0)AND"
    "(promotion_sequence_watermarkISNOTNULL)AND(promotion_sequence_watermark>=0)AND"
    "(aggregate_input_watermarkISNOTNULL)AND(aggregate_contract_versionISNOTNULL)AND"
    "(length(aggregate_contract_version)>0)AND(aggregate_aggregation_versionISNOTNULL)AND"
    "(length(aggregate_aggregation_version)>0)AND(aggregate_metric_versionISNOTNULL)AND"
    "(length(aggregate_metric_version)>0))))"
)
_INSIGHT_BUDGET_RESERVATION_CHECK_NORM = (
    "CHECK((((budget_reserved_atISNULL)AND(paid_call_started_atISNULL))OR"
    "((budget_reserved_atISNOTNULL)AND(paid_call_started_atISNOTNULL)AND"
    "(budget_reserved_at=paid_call_started_at))))"
)
_PROMOTION_SEQ_FN_NORM = (
    "CREATEORREPLACEFUNCTIONpublic.geo_obs_assign_promotion_seq()RETURNStrigger"
    "LANGUAGEplpgsqlAS$function$BEGINIFTG_OP='INSERT'ANDNEW.promotion_seqISNOTNULL"
    "THENRAISEEXCEPTION'promotion_seqisdatabaseassigned';ENDIF;IFTG_OP='UPDATE'"
    "ANDNEW.promotion_seqISDISTINCTFROMOLD.promotion_seqTHENRAISEEXCEPTION"
    "'promotion_seqisimmutable';ENDIF;IFTG_OP='UPDATE'ANDOLD.processing_state='withdrawn'"
    "ANDNEW.processing_state<>'withdrawn'THENRAISEEXCEPTION'withdrawnobservationis"
    "terminal;appendarevision';ENDIF;IFTG_OP='UPDATE'ANDOLD.processing_state='promoted'"
    "ANDNEW.processing_stateNOTIN('promoted','withdrawn')THENRAISEEXCEPTION'promoted"
    "observationmayonlyremainpromotedorbecomewithdrawn';ENDIF;IFTG_OP='UPDATE'AND"
    "OLD.processing_state<>'promoted'ANDNEW.processing_state='promoted'AND"
    "OLD.promotion_seqISNOTNULLTHENRAISEEXCEPTION'firstpromotionrequiresanunassigned"
    "sequence';ENDIF;IFNEW.processing_state='promoted'ANDNEW.promotion_seqISNULLTHEN"
    "NEW.promotion_seq:=nextval('public.geo_observation_promotion_seq'::regclass);"
    "ENDIF;RETURNNEW;END$function$"
)
_PROMOTION_SEQ_TRIGGER_NORM = (
    "CREATETRIGGERtrg_geo_obs_assign_promotion_seqBEFOREINSERTORUPDATEOF"
    "processing_state,promotion_seqONpublic.geo_observation_eventsFOREACHROW"
    "EXECUTEFUNCTIONgeo_obs_assign_promotion_seq()"
)
_PROMOTED_INPUT_IMMUTABLE_FN_NORM = (
    "CREATEORREPLACEFUNCTIONpublic.geo_obs_reject_promoted_input_mutation()"
    "RETURNStriggerLANGUAGEplpgsqlAS$function$DECLAREevent_stateTEXT;BEGINSELECT"
    "processing_stateINTOevent_stateFROMpublic.geo_observation_eventsWHEREid=OLD.event_id;"
    "IFevent_state='promoted'THENRAISEEXCEPTION'promotedobservationinputisimmutable;"
    "withdraweventandappendarevision';ENDIF;IFTG_OP='DELETE'THENRETURNOLD;ENDIF;"
    "RETURNNEW;END$function$"
)
_SIGNAL_IMMUTABLE_TRIGGER_NORM = (
    "CREATETRIGGERtrg_geo_obs_signal_promoted_immutableBEFOREDELETEORUPDATEON"
    "public.geo_observation_signalsFOREACHROWEXECUTEFUNCTION"
    "geo_obs_reject_promoted_input_mutation()"
)
_BUCKET_IMMUTABLE_TRIGGER_NORM = (
    "CREATETRIGGERtrg_geo_obs_bucket_promoted_immutableBEFOREDELETEORUPDATEON"
    "public.geo_observation_contributor_bucketsFOREACHROWEXECUTEFUNCTION"
    "geo_obs_reject_promoted_input_mutation()"
)
_PUBLISHED_EVENT_IMMUTABLE_FN_NORM = (
    "CREATEORREPLACEFUNCTIONpublic.geo_obs_reject_published_event_mutation()RETURNStrigger"
    "LANGUAGEplpgsqlAS$function$BEGINIFTG_OP='DELETE'ANDOLD.processing_stateIN('promoted',"
    "'withdrawn')THENRAISEEXCEPTION'publishedobservationcannotbedeleted;withdrawandappend"
    "arevision';ENDIF;IFTG_OP='DELETE'THENRETURNOLD;ENDIF;IFOLD.processing_state='promoted'"
    "THENIFNEW.processing_state='promoted'THENIF(to_jsonb(NEW)-ARRAY['updated_at','legal_hold',"
    "'retention_until'])ISDISTINCTFROM(to_jsonb(OLD)-ARRAY['updated_at','legal_hold',"
    "'retention_until'])THENRAISEEXCEPTION'promoted"
    "observationbusinessfieldsareimmutable';ENDIF;ELSIFNEW.processing_state='withdrawn'THENIF"
    "(to_jsonb(NEW)-ARRAY['processing_state','withdrawn_at','lease_token','lease_until',"
    "'rejection_codes','updated_at','legal_hold'])ISDISTINCTFROM(to_jsonb(OLD)-ARRAY["
    "'processing_state','withdrawn_at','lease_token','lease_until','rejection_codes','updated_at',"
    "'legal_hold'])THENRAISEEXCEPTION'withdrawalcannotrewritepromotedobservationinputs';ENDIF;"
    "ELSERAISEEXCEPTION'promotedobservationmayonlyremainpromotedorbecomewithdrawn';ENDIF;ELSIF"
    "OLD.processing_state='withdrawn'THENIFNEW.processing_state<>'withdrawn'THENRAISEEXCEPTION"
    "'withdrawnobservationisterminal;appendarevision';ENDIF;IF(to_jsonb(NEW)-ARRAY["
    "'owner_user_id','brand_id','answer_hash','prompt_fingerprint','updated_at','legal_hold'])"
    "ISDISTINCTFROM(to_jsonb(OLD)-ARRAY['owner_user_id','brand_id','answer_hash',"
    "'prompt_fingerprint','updated_at','legal_hold'])THENRAISEEXCEPTION'withdrawnobservationonly"
    "permitsretentionanonymization';ENDIF;ENDIF;RETURNNEW;END$function$"
)
_PUBLISHED_EVENT_IMMUTABLE_TRIGGER_NORM = (
    "CREATETRIGGERtrg_geo_obs_event_published_immutableBEFOREDELETEORUPDATEON"
    "public.geo_observation_eventsFOREACHROWEXECUTEFUNCTIONgeo_obs_reject_published_event_mutation()"
)
_MARK_BUCKETS_DIRTY_FN_NORM = (
    "CREATEORREPLACEFUNCTIONpublic.geo_obs_mark_aggregate_buckets_dirty(p_scope_typetext,"
    "p_observed_attimestampwithtimezone)RETURNSvoidLANGUAGEplpgsqlAS$function$DECLARE"
    "day_startDATE:=(p_observed_atATTIMEZONE'UTC')::date;BEGINIFp_scope_typeNOTIN"
    "('private_brand','public_industry')ORp_observed_atISNULLTHENRAISEEXCEPTION'invalid"
    "aggregatebucketinvalidation';ENDIF;INSERTINTOpublic.geo_observation_aggregate_bucket_revisionASrevision"
    "(scope_type,bucket_granularity,bucket_start,epoch,dirty,updated_at)VALUES(p_scope_type,'day',"
    "day_start,1,TRUE,NOW()),(p_scope_type,'week',date_trunc('week',p_observed_atATTIMEZONE'UTC')::date,"
    "1,TRUE,NOW()),(p_scope_type,'month',date_trunc('month',p_observed_atATTIMEZONE'UTC')::date,1,TRUE,"
    "NOW())ONCONFLICT(scope_type,bucket_granularity,bucket_start)DOUPDATESETepoch="
    "revision.epoch+1,dirty=TRUE,updated_at=NOW();END$function$"
)
_EVENT_ELIGIBILITY_FN_NORM = (
    "CREATEORREPLACEFUNCTIONpublic.geo_obs_bump_eligibility_epoch_on_event()"
    "RETURNStriggerLANGUAGEplpgsqlAS$function$DECLAREinvalidatesBOOLEAN:=FALSE;"
    "affects_privateBOOLEAN:=FALSE;affects_publicBOOLEAN:=FALSE;BEGIN"
    "IFTG_OP='DELETE'THENinvalidates:=OLD.processing_state='promoted';ELSIFTG_OP='UPDATE'"
    "ANDOLD.processing_state='promoted'THENinvalidates:=NEW.processing_state<>'promoted'"
    "ORNEW.owner_user_idISDISTINCTFROMOLD.owner_user_idORNEW.brand_idISDISTINCTFROMOLD.brand_id"
    "ORNEW.industry_keyISDISTINCTFROMOLD.industry_keyORNEW.observed_atISDISTINCTFROMOLD.observed_at"
    "ORNEW.withdrawn_atISDISTINCTFROMOLD.withdrawn_at;ENDIF;IFinvalidatesTHENaffects_private:="
    "OLD.owner_user_idISNOTNULLANDOLD.brand_idISNOTNULL;SELECT(OLD.source_type='research_round')"
    "OREXISTS(SELECT1FROMpublic.geo_observation_contributor_bucketsbWHEREb.event_id=OLD.id)INTO"
    "affects_public;IFaffects_privateTHENPERFORMpublic.geo_obs_mark_aggregate_buckets_dirty"
    "('private_brand',OLD.observed_at);ENDIF;IFaffects_publicTHENPERFORM"
    "public.geo_obs_mark_aggregate_buckets_dirty('public_industry',OLD.observed_at);ENDIF;ENDIF;"
    "IFTG_OP='DELETE'THENRETURNOLD;ENDIF;"
    "RETURNNEW;END$function$"
)
_BUCKET_ELIGIBILITY_FN_NORM = (
    "CREATEORREPLACEFUNCTIONpublic.geo_obs_bump_public_epoch_on_bucket()RETURNStrigger"
    "LANGUAGEplpgsqlAS$function$DECLAREwas_promotedBOOLEAN:=FALSE;event_observed_atTIMESTAMPTZ;"
    "BEGINSELECTprocessing_state='promoted',observed_atINTOwas_promoted,event_observed_atFROM"
    "public.geo_observation_eventsWHEREid=OLD.event_id;IFCOALESCE(was_promoted,FALSE)THENPERFORM"
    "public.geo_obs_mark_aggregate_buckets_dirty('public_industry',event_observed_at);ENDIF;"
    "IFTG_OP='DELETE'THENRETURNOLD;"
    "ENDIF;RETURNNEW;END$function$"
)
_EVENT_ELIGIBILITY_TRIGGER_NORM = (
    "CREATETRIGGERtrg_geo_obs_event_eligibility_epochAFTERDELETEORUPDATEON"
    "public.geo_observation_eventsFOREACHROWEXECUTEFUNCTIONgeo_obs_bump_eligibility_epoch_on_event()"
)
_BUCKET_ELIGIBILITY_TRIGGER_NORM = (
    "CREATETRIGGERtrg_geo_obs_bucket_eligibility_epochAFTERDELETEORUPDATEON"
    "public.geo_observation_contributor_bucketsFOREACHROWEXECUTEFUNCTIONgeo_obs_bump_public_epoch_on_bucket()"
)

_SURFACES = [
    "doubao_ark_api_search", "qwen_dashscope_search", "deepseek_native_no_search",
    "deepseek_native_with_search", "deepseek_metaso_proxy", "deepseek_dashscope_search_legacy",
    "yuanbao_app_verified", "tencent_wsa_search", "yuanbao_hy3_tokenhub", "manual_app_capture", "other_explicit",
]
_OUTCOMES = [
    "recommended", "conditionally_recommended", "candidate_only", "mentioned_only", "criteria_only",
    "refused_no_evidence", "refused_risk", "not_mentioned", "entity_ambiguous", "engine_error",
]
_PROC_STATES = ["pending", "processing", "pending_review", "promoted", "private_only", "rejected", "withdrawn", "error"]
_SOURCE_TYPES = ["research_round", "paid_diagnosis", "recurring_monitoring"]
_RESP_STATES = ["answered", "refused", "timeout", "error", "unknown", "budget_blocked"]
_INTENTS = ["awareness", "category_recommendation", "comparison", "evaluation", "transaction", "risk", "branded", "other"]
_SENTIMENTS = ["positive", "neutral", "negative", "mixed", "unknown"]
_SCOPES = ["private_brand", "public_industry", "admin_shadow"]
_STABILITIES = ["stable", "watch", "insufficient", "shifted"]
_TABLES = [
    "geo_observation_events", "geo_observation_signals", "geo_observation_contributor_buckets",
    "geo_observation_audit", "geo_observation_policy", "geo_observation_aggregates",
    "geo_observation_aggregate_refresh_manifest", "geo_observation_eligibility_epoch",
    "geo_observation_aggregate_bucket_revision", "geo_observation_insight_jobs",
]
_PERSISTENT_TRUTH_TABLES = [
    "geo_observation_events", "geo_observation_signals",
    "geo_observation_contributor_buckets", "geo_observation_audit",
    "geo_observation_policy", "geo_observation_gold_eval",
    "geo_observation_aggregates", "geo_observation_aggregate_refresh_manifest",
    "geo_observation_eligibility_epoch", "geo_observation_aggregate_bucket_revision",
    "geo_observation_insight_jobs",
]


class ObservationSchemaNotReady(RuntimeError):
    pass


def _constraint_def(cur, conname: str, table: str) -> Optional[str]:
    relation = table if "." in table else f"public.{table}"
    cur.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conname=%s AND conrelid=%s::regclass",
        (conname, relation),
    )
    row = cur.fetchone()
    return row["d"] if row else None


def _enum_check_error(cur, conname: str, table: str, col: str, enums: list) -> Optional[str]:
    """P1-3:强枚举 CHECK 校验(抵御 substring 假绿:TRUE OR 重言式/额外非法枚举/错列/弱化)。返回错误串或 None。

    substring "枚举都在场" 会被 `CHECK (TRUE OR col IN (...合法枚举...))` 骗过(字面量仍在,但约束恒真)。
    这里额外验:① 期望列名在场(防错列)② 去字面量后 skeleton 无 true/false/or 重言式 ③ 字面量数==枚举数(防塞额外值)。
    """
    d = _constraint_def(cur, conname, table)
    if not d:
        return f"{conname} 缺失"
    if any(f"'{v}'" not in d for v in enums):
        return f"{conname} 枚举不全(值级): {d}"
    # 精确规范骨架匹配(最稳):去字面量 + 去类型转换 + 去空白 → 必须恰等于 `CHECK((col=ANY(ARRAY[N-1 逗号])))`。
    #   任何结构偏离——运算符换(<> ANY)、自引用恒真(ARRAY[col,...])、额外 token(OR/TRUE/IS NULL)、额外元素、错列——都改变骨架被拦。
    skel = re.sub(r"'[^']*'", "", d)
    skel = re.sub(r"::(?:text\[\]|text|character varying|varchar|bpchar)", "", skel)
    skel = re.sub(r"\s+", "", skel)
    expected = "CHECK((" + col + "=ANY(ARRAY[" + "," * (len(enums) - 1) + "])))"
    if skel != expected:
        return f"{conname} 非规范 = ANY 成员式(疑弱化/篡改):期望骨架={expected} 实际={skel} 原文={d}"
    return None


def _index_def(cur, idx: str) -> Optional[str]:
    relation = idx if "." in idx else f"public.{idx}"
    cur.execute("SELECT pg_get_indexdef(to_regclass(%s)) AS d", (relation,))
    row = cur.fetchone()
    return row["d"] if row else None


def verify_geo_observation_schema(cur) -> None:
    """只读核验;任一不满足抛 ObservationSchemaNotReady(errors)。"""
    errors: list[str] = []

    for t in _TABLES:
        cur.execute("SELECT to_regclass(%s) AS r", (f"public.{t}",))
        if cur.fetchone()["r"] is None:
            errors.append(f"表缺失: {t}")
    if errors:
        raise ObservationSchemaNotReady(errors)

    cur.execute(
        """SELECT cls.relname,cls.relkind,cls.relpersistence
             FROM pg_class cls JOIN pg_namespace n ON n.oid=cls.relnamespace
            WHERE n.nspname='public' AND cls.relname=ANY(%s)""",
        (_PERSISTENT_TRUTH_TABLES,),
    )
    relation_defs = {
        row["relname"]: (row["relkind"], row["relpersistence"])
        for row in cur.fetchall()
    }
    for table in _PERSISTENT_TRUTH_TABLES:
        if relation_defs.get(table) != ("r", "p"):
            errors.append(f"关键真相表非 public permanent table: {table}")

    # 所有命名 CHECK/FK 必须 convalidated=TRUE(防 NOT VALID 篡改假绿:定义文本对但未校验存量行/未生效)
    cur.execute(
        """SELECT cl.relname, c.conname FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid
           WHERE cl.relname = ANY(%s) AND c.contype IN ('c','f') AND NOT c.convalidated""",
        (_TABLES,),
    )
    _unvalidated = [f"{r['relname']}.{r['conname']}" for r in cur.fetchall()]
    if _unvalidated:
        errors.append(f"存在未 VALIDATE 的 CHECK/FK(convalidated=false 假绿): {_unvalidated}")

    # signals 核心列 NOT NULL(半成品升级漂移防线,与 events 同)
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema='public' AND table_name='geo_observation_signals'
             AND is_nullable='YES' AND column_name = ANY(%s)""",
        (["event_id", "industry_key", "prompt_family_key", "prompt_intent", "is_branded_prompt",
          "platform_key", "provider_key", "model_key", "surface_key", "response_status", "target_outcome",
          "sentiment", "quality_score_bps", "base_weight_bps", "effective_weight_bps", "confidence_bps", "observed_at"],),
    )
    _sig_null = [r["column_name"] for r in cur.fetchall()]
    if _sig_null:
        errors.append(f"signals 核心列不应可空(半成品漂移): {_sig_null}")

    # events:三命名枚举 CHECK 强校验(P1-3:抵御 TRUE OR/额外枚举/错列)
    for _err in (
        _enum_check_error(cur, "chk_geo_obs_event_surface_key", "geo_observation_events", "surface_key", _SURFACES),
        _enum_check_error(cur, "chk_geo_obs_event_processing_state", "geo_observation_events", "processing_state", _PROC_STATES),
        _enum_check_error(cur, "chk_geo_obs_event_source_type", "geo_observation_events", "source_type", _SOURCE_TYPES),
    ):
        if _err:
            errors.append(_err)
    # events 核心列必须 NOT NULL(防半成品升级路径 ADD COLUMN 无 NOT NULL 的静默漂移)
    cur.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema='public' AND table_name='geo_observation_events'
             AND is_nullable='YES' AND column_name = ANY(%s)""",
        (["event_uuid", "source_type", "source_table", "source_record_id", "source_subkey",
          "source_event_key", "platform_key", "provider_key", "model_key", "surface_key",
          "session_mode", "observed_at", "processing_state"],),
    )
    _nullable = [r["column_name"] for r in cur.fetchall()]
    if _nullable:
        errors.append(f"events 核心列不应可空(半成品漂移): {_nullable}")
    cur.execute(
        """SELECT data_type,is_nullable FROM information_schema.columns
             WHERE table_schema='public' AND table_name='geo_observation_events'
               AND column_name='promotion_seq'"""
    )
    promotion_seq_col = cur.fetchone()
    if not promotion_seq_col or dict(promotion_seq_col) != {
        "data_type": "bigint", "is_nullable": "YES"
    }:
        errors.append("events.promotion_seq 类型/可空漂移")
    if re.sub(r"\s+", "", _constraint_def(
        cur, "chk_geo_obs_event_promotion_seq", "geo_observation_events"
    ) or "") != _EVENT_PROMOTION_SEQ_CHECK_NORM:
        errors.append("chk_geo_obs_event_promotion_seq 缺失/定义漂移")
    cur.execute(
        """SELECT data_type::text AS data_type,start_value,min_value,max_value,
                  increment_by,cycle,cache_size
             FROM pg_sequences
            WHERE schemaname='public' AND sequencename='geo_observation_promotion_seq'"""
    )
    seq = cur.fetchone()
    expected_seq = {
        "data_type": "bigint", "start_value": 1, "min_value": 1,
        "max_value": 9223372036854775807, "increment_by": 1,
        "cycle": False, "cache_size": 1,
    }
    if seq is None or dict(seq) != expected_seq:
        errors.append("public.geo_observation_promotion_seq 定义漂移")
    else:
        cur.execute(
            """SELECT c.relpersistence FROM pg_class c
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname='geo_observation_promotion_seq'
                  AND c.relkind='S'"""
        )
        sequence_class = cur.fetchone()
        if sequence_class is None or sequence_class["relpersistence"] != "p":
            errors.append("public.geo_observation_promotion_seq 持久性漂移")
        cur.execute(
            "SELECT (SELECT last_value FROM public.geo_observation_promotion_seq) AS last_value,"
            "(SELECT is_called FROM public.geo_observation_promotion_seq) AS is_called,"
            "COALESCE(MAX(promotion_seq),0) AS max_event FROM public.geo_observation_events"
        )
        seq_head = cur.fetchone()
        if (
            not bool(seq_head["is_called"])
            or int(seq_head["last_value"]) < int(seq_head["max_event"])
            or int(seq_head["last_value"]) >= 9223372036854775807
        ):
            errors.append("promotion sequence 下一值不能严格大于现有事件最大序列")

    # events 幂等门
    d = _index_def(cur, "uq_geo_obs_event_business")
    if not d or "UNIQUE" not in d.upper() or "source_subkey" not in d:
        errors.append("uq_geo_obs_event_business 缺失/列不符")
    d = _index_def(cur, "uq_geo_obs_event_key")
    if not d or "UNIQUE" not in d.upper() or "source_event_key" not in d:
        errors.append("uq_geo_obs_event_key 缺失")

    # signals:四命名枚举 CHECK 强校验(P1-3) + FK convalidated + 一事件一信号 + 无 owner/brand
    for _err in (
        _enum_check_error(cur, "chk_geo_obs_signal_outcome", "geo_observation_signals", "target_outcome", _OUTCOMES),
        _enum_check_error(cur, "chk_geo_obs_signal_response", "geo_observation_signals", "response_status", _RESP_STATES),
        _enum_check_error(cur, "chk_geo_obs_signal_intent", "geo_observation_signals", "prompt_intent", _INTENTS),
        _enum_check_error(cur, "chk_geo_obs_signal_sentiment", "geo_observation_signals", "sentiment", _SENTIMENTS),
    ):
        if _err:
            errors.append(_err)
    cur.execute(
        """SELECT 1 FROM pg_constraint WHERE conname='fk_geo_obs_signal_event'
           AND conrelid='public.geo_observation_signals'::regclass AND contype='f' AND convalidated"""
    )
    if cur.fetchone() is None:
        errors.append("fk_geo_obs_signal_event 缺失/未 VALIDATE")
    d = _index_def(cur, "uq_geo_obs_signal_event")
    if not d or "UNIQUE" not in d.upper() or "event_id" not in d:
        errors.append("uq_geo_obs_signal_event 缺失")
    cur.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema='public' AND table_name='geo_observation_signals'
             AND column_name IN ('owner_user_id','brand_id')"""
    )
    if cur.fetchone() is not None:
        errors.append("geo_observation_signals 违规含 owner_user_id/brand_id(隐私)")

    # contributor 公共投票门(R6:5 列 · 不含 source_type)
    d = _index_def(cur, "uq_geo_obs_contributor_vote")
    if not d or "UNIQUE" not in d.upper():
        errors.append("uq_geo_obs_contributor_vote 缺失")
    else:
        for col in ("contributor_user_bucket", "contributor_brand_bucket", "prompt_family_key", "platform_key", "contribution_date"):
            if col not in d:
                errors.append(f"uq_geo_obs_contributor_vote 缺列 {col}")
        if "source_type" in d:
            errors.append("uq_geo_obs_contributor_vote 违规含 source_type(R6)")

    # audit:event_id 可空 + audit_event_key 唯一
    cur.execute(
        """SELECT is_nullable FROM information_schema.columns
           WHERE table_schema='public' AND table_name='geo_observation_audit'
             AND column_name='event_id'"""
    )
    row = cur.fetchone()
    if not row or row["is_nullable"] != "YES":
        errors.append("audit.event_id 应可空")
    d = _index_def(cur, "uq_geo_obs_audit_key")
    if not d or "UNIQUE" not in d.upper() or "audit_event_key" not in d:
        errors.append("uq_geo_obs_audit_key 缺失")

    # policy:默认行存在 + 晋升开关默认 false(R11)
    cur.execute(
        "SELECT policy_json, collection_mode FROM public.geo_observation_policy "
        "WHERE singleton_id=1"
    )
    prow = cur.fetchone()
    if not prow:
        errors.append("geo_observation_policy 默认行缺失")
    else:
        if prow["collection_mode"] not in (
            "existing_collectors_reconciled", "native_sampling_driver"
        ):
            errors.append(f"collection_mode 非法: {prow['collection_mode']!r}")
    cur.execute(
        """SELECT data_type, is_nullable, column_default
             FROM information_schema.columns
            WHERE table_schema='public' AND table_name='geo_observation_policy'
              AND column_name='collection_mode'"""
    )
    _mode_col = cur.fetchone()
    if _mode_col is None:
        errors.append("geo_observation_policy.collection_mode 缺失")
    elif (
        _mode_col["data_type"] != "text"
        or _mode_col["is_nullable"] != "NO"
        or re.sub(r"\s+", "", _mode_col["column_default"] or "")
        != "'existing_collectors_reconciled'::text"
    ):
        errors.append(f"collection_mode 类型/可空/默认值漂移: {dict(_mode_col)}")
    cur.execute(
        """SELECT convalidated, pg_get_constraintdef(oid) AS d
             FROM pg_constraint
            WHERE conname='chk_geo_observation_collection_mode'
              AND conrelid='public.geo_observation_policy'::regclass AND contype='c'"""
    )
    _mode_check = cur.fetchone()
    if _mode_check is None:
        errors.append("chk_geo_observation_collection_mode 缺失")
    else:
        if not _mode_check["convalidated"]:
            errors.append("chk_geo_observation_collection_mode 未 VALIDATE")
        if re.sub(r"\s+", "", _mode_check["d"]) != _COLLECTION_MODE_CHECK_NORM:
            errors.append(f"chk_geo_observation_collection_mode 定义被弱化/漂移: {_mode_check['d']}")
    cur.execute(
        """SELECT data_type,is_nullable FROM information_schema.columns
             WHERE table_schema='public' AND table_name='geo_observation_policy'
               AND column_name='active_aggregate_policy_basis'"""
    )
    _policy_basis_col = cur.fetchone()
    if not _policy_basis_col or dict(_policy_basis_col) != {
        "data_type": "text", "is_nullable": "YES"
    }:
        errors.append("policy.active_aggregate_policy_basis 类型/可空漂移")
    _active_basis_def = _constraint_def(
        cur, "chk_geo_obs_policy_active_aggregate_basis", "geo_observation_policy"
    )
    if (re.sub(r"\s+", "", _active_basis_def or "")
            != _POLICY_ACTIVE_BASIS_CHECK_NORM):
        errors.append("chk_geo_obs_policy_active_aggregate_basis 缺失/定义漂移")
    # P1:金标准门 DB 约束**定义级精确匹配**(只核名字/所属表不够——同名换 CHECK(TRUE)/部分弱化/降阈值 也要拦):
    #   与枚举 CHECK 同一威胁模型(精确规范骨架),比单向量功能反查稳(单探针会被"探针可过、别的放行"的部分弱化绕过)。
    #   ① 存在 + convalidated(NOT VALID 不校验存量违规行)② pg_get_constraintdef 去空白后逐字符 == 规范式。
    cur.execute(
        """SELECT convalidated, pg_get_constraintdef(oid) AS d FROM pg_constraint
             WHERE conname='ck_geo_obs_policy_gold_gate' AND conrelid='public.geo_observation_policy'::regclass AND contype='c'"""
    )
    _gc = cur.fetchone()
    if _gc is None:
        errors.append("ck_geo_obs_policy_gold_gate 缺失(金标准门可被无证据强开)")
    else:
        if not _gc["convalidated"]:
            errors.append("ck_geo_obs_policy_gold_gate 未 VALIDATE(convalidated=false 假绿)")
        if re.sub(r"\s+", "", _gc["d"]) != _GOLD_GATE_CHECK_NORM:
            errors.append(f"ck_geo_obs_policy_gold_gate 定义被篡改/弱化(非契约§601 规范阈值式): {_gc['d']}")
    cur.execute("SELECT to_regclass('public.geo_observation_gold_eval') AS r")
    if cur.fetchone()["r"] is None:
        errors.append("geo_observation_gold_eval 不可变评估表缺失")
    else:
        # append-only 完整性:**pin 触发器函数体 + 触发器定义**(源级精确匹配,只读、不可绕过)。
        #   行为探针(试 UPDATE 看是否被拦)总能被"专拦探针放行真行"的触发器绕过(no-op/内容/时间判别都可 key);
        #   定义 pin 直接核"触发器函数是不是那段规范源码 + 触发器是不是 BEFORE UPDATE/DELETE FOR EACH ROW 指向它",无从伪装。
        #   ① 函数体(掏空/条件放行/改写 → 变)② 触发器定义(retarget/改 events/删 FOR EACH ROW → 变)
        #   ③ tgenabled 生效(DISABLE→'D')④ session_replication_role=origin('replica' 让 'O' 触发器主库不触发)。
        cur.execute("SELECT pg_get_functiondef(oid) AS d FROM pg_proc "
                    "WHERE proname='geo_obs_gold_eval_immutable' AND pronamespace='public'::regnamespace")
        _fn = cur.fetchone()
        if _fn is None or re.sub(r"\s+", "", _fn["d"]) != _IMMUT_FN_NORM:
            errors.append("gold_eval append-only 触发器函数体被篡改/缺失(可改写删除评估记录)")
        cur.execute("""SELECT pg_get_triggerdef(oid) AS d, tgenabled FROM pg_trigger
                       WHERE tgname='trg_geo_obs_gold_eval_immutable'
                       AND tgrelid='public.geo_observation_gold_eval'::regclass AND NOT tgisinternal""")
        _tg = cur.fetchone()
        if _tg is None or re.sub(r"\s+", "", _tg["d"]) != _IMMUT_TRIGGER_NORM:
            errors.append("gold_eval append-only 触发器定义被篡改/缺失")
        elif _tg["tgenabled"] not in ("O", "A"):
            errors.append("gold_eval append-only 触发器被 DISABLE/仅 replica(主库不触发)")
        cur.execute("SELECT current_setting('session_replication_role') AS r")
        if cur.fetchone()["r"] != "origin":
            errors.append("session_replication_role 非 origin(append-only/FK 触发器主库不触发)")
        cur.execute("""SELECT convalidated, pg_get_constraintdef(oid) AS d FROM pg_constraint
                       WHERE conname='chk_geo_obs_gold_derived'
                       AND conrelid='public.geo_observation_gold_eval'::regclass AND contype='c'""")
        _cd = cur.fetchone()
        if _cd is None or re.sub(r"\s+", "", _cd["d"]) != _GOLD_DERIVED_CHECK_NORM:
            errors.append("chk_geo_obs_gold_derived 缺失/定义被篡改(可伪造指标不达标却 passed=true 的评估)")
        elif not _cd["convalidated"]:
            errors.append("chk_geo_obs_gold_derived 未 VALIDATE(convalidated=false 假绿)")
    cur.execute("""SELECT convalidated, pg_get_constraintdef(oid) AS d FROM pg_constraint
                   WHERE conname='fk_geo_obs_policy_gold_eval'
                   AND conrelid='public.geo_observation_policy'::regclass AND contype='f'""")
    _fkd = cur.fetchone()
    if _fkd is None or re.sub(r"\s+", "", _fkd["d"]) != _GOLD_FK_NORM:
        errors.append("policy→gold_eval 外键缺失/定义被篡改(策略指针可指向伪造评估表/错列)")
    elif not _fkd["convalidated"]:
        errors.append("policy→gold_eval 外键未 VALIDATE(convalidated=false 假绿)")

    # aggregates:scope/stability 强枚举 CHECK(P1-3) + 隐私 CHECK + 唯一 aggregate_key + partial predicate
    for _err in (
        _enum_check_error(cur, "chk_geo_obs_agg_scope", "geo_observation_aggregates", "scope_type", _SCOPES),
        _enum_check_error(cur, "chk_geo_obs_agg_stability", "geo_observation_aggregates", "stability_status", _STABILITIES),
    ):
        if _err:
            errors.append(_err)
    for cn in ("chk_geo_obs_agg_private_scope", "chk_geo_obs_agg_public_scope", "chk_geo_obs_agg_bps_range", "chk_geo_obs_agg_counts_nonneg"):
        if _constraint_def(cur, cn, "geo_observation_aggregates") is None:
            errors.append(f"{cn} 缺失")
    d = _index_def(cur, "uq_geo_obs_agg_key")
    if not d or "UNIQUE" not in d.upper() or "aggregate_key" not in d:
        errors.append("uq_geo_obs_agg_key 缺失")
    d = _index_def(cur, "idx_geo_obs_agg_private")
    if not d or "WHERE" not in d.upper() or "private_brand" not in d:
        errors.append("idx_geo_obs_agg_private partial predicate 不符")
    cur.execute(
        """SELECT data_type,is_nullable FROM information_schema.columns
             WHERE table_schema='public' AND table_name='geo_observation_aggregates'
               AND column_name='policy_basis_hash'"""
    )
    _agg_basis_col = cur.fetchone()
    if not _agg_basis_col or dict(_agg_basis_col) != {
        "data_type": "text", "is_nullable": "YES"
    }:
        errors.append("aggregates.policy_basis_hash 类型/可空漂移")
    _agg_basis_def = _constraint_def(
        cur, "chk_geo_obs_agg_policy_basis", "geo_observation_aggregates"
    )
    if re.sub(r"\s+", "", _agg_basis_def or "") != _AGGREGATE_BASIS_CHECK_NORM:
        errors.append("chk_geo_obs_agg_policy_basis 缺失/定义漂移")
    cur.execute(
        """SELECT data_type,is_nullable,lower(column_default) AS column_default
             FROM information_schema.columns
            WHERE table_schema='public' AND table_name='geo_observation_aggregates'
              AND column_name='eligibility_epoch'"""
    )
    agg_epoch_col = cur.fetchone()
    if not agg_epoch_col or dict(agg_epoch_col) != {
        "data_type": "bigint", "is_nullable": "NO", "column_default": "0"
    }:
        errors.append("aggregates.eligibility_epoch 类型/可空/默认值漂移")
    if re.sub(r"\s+", "", _constraint_def(
        cur, "chk_geo_obs_agg_eligibility_epoch", "geo_observation_aggregates"
    ) or "") != _AGGREGATE_ELIGIBILITY_CHECK_NORM:
        errors.append("chk_geo_obs_agg_eligibility_epoch 缺失/定义漂移")
    cur.execute(
        """SELECT data_type,is_nullable,lower(column_default) AS column_default
             FROM information_schema.columns
            WHERE table_schema='public' AND table_name='geo_observation_aggregates'
              AND column_name='promotion_sequence_watermark'"""
    )
    agg_promotion_col = cur.fetchone()
    if not agg_promotion_col or dict(agg_promotion_col) != {
        "data_type": "bigint", "is_nullable": "NO", "column_default": "0"
    }:
        errors.append("aggregates.promotion_sequence_watermark 类型/可空/默认值漂移")
    if re.sub(r"\s+", "", _constraint_def(
        cur, "chk_geo_obs_agg_promotion_sequence_watermark", "geo_observation_aggregates"
    ) or "") != _AGGREGATE_PROMOTION_WATERMARK_CHECK_NORM:
        errors.append("chk_geo_obs_agg_promotion_sequence_watermark 缺失/定义漂移")

    cur.execute(
        """SELECT column_name,data_type,is_nullable,lower(column_default) AS column_default
             FROM information_schema.columns
            WHERE table_schema='public' AND table_name='geo_observation_eligibility_epoch'"""
    )
    epoch_columns = {
        row["column_name"]: (row["data_type"], row["is_nullable"], row["column_default"])
        for row in cur.fetchall()
    }
    expected_epoch_columns = {
        "scope_type": ("text", "NO", None),
        "epoch": ("bigint", "NO", "0"),
        "updated_at": ("timestamp with time zone", "NO", "now()"),
    }
    for column, expected in expected_epoch_columns.items():
        if epoch_columns.get(column) != expected:
            errors.append(f"eligibility epoch 列定义漂移: {column}")
    if re.sub(r"\s+", "", _constraint_def(
        cur, "geo_observation_eligibility_epoch_pkey", "geo_observation_eligibility_epoch"
    ) or "") != "PRIMARYKEY(scope_type)":
        errors.append("eligibility epoch PK 缺失/定义漂移")
    if re.sub(r"\s+", "", _constraint_def(
        cur, "chk_geo_obs_eligibility_epoch_scope", "geo_observation_eligibility_epoch"
    ) or "") != _ELIGIBILITY_SCOPE_CHECK_NORM:
        errors.append("chk_geo_obs_eligibility_epoch_scope 缺失/定义漂移")
    if re.sub(r"\s+", "", _constraint_def(
        cur, "chk_geo_obs_eligibility_epoch_nonnegative", "geo_observation_eligibility_epoch"
    ) or "") != _ELIGIBILITY_NONNEGATIVE_CHECK_NORM:
        errors.append("chk_geo_obs_eligibility_epoch_nonnegative 缺失/定义漂移")
    cur.execute(
        "SELECT scope_type FROM public.geo_observation_eligibility_epoch ORDER BY scope_type"
    )
    if [row["scope_type"] for row in cur.fetchall()] != ["private_brand", "public_industry"]:
        errors.append("eligibility epoch 必需 scope 行缺失/多余")

    cur.execute(
        """SELECT column_name,data_type,is_nullable,lower(column_default) AS column_default
             FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='geo_observation_aggregate_bucket_revision'"""
    )
    revision_columns = {
        row["column_name"]: (row["data_type"], row["is_nullable"], row["column_default"])
        for row in cur.fetchall()
    }
    expected_revision_columns = {
        "scope_type": ("text", "NO", None),
        "bucket_granularity": ("text", "NO", None),
        "bucket_start": ("date", "NO", None),
        "epoch": ("bigint", "NO", "0"),
        "dirty": ("boolean", "NO", "false"),
        "updated_at": ("timestamp with time zone", "NO", "now()"),
        "published_receipt": ("jsonb", "YES", None),
    }
    for column, expected in expected_revision_columns.items():
        if revision_columns.get(column) != expected:
            errors.append(f"aggregate bucket revision 列定义漂移: {column}")
    revision_constraints = {
        "geo_observation_aggregate_bucket_revision_pkey":
            "PRIMARYKEY(scope_type,bucket_granularity,bucket_start)",
        "chk_geo_obs_bucket_revision_scope": _ELIGIBILITY_SCOPE_CHECK_NORM,
        "chk_geo_obs_bucket_revision_granularity": _MANIFEST_GRANULARITY_CHECK_NORM,
        "chk_geo_obs_bucket_revision_epoch": _ELIGIBILITY_NONNEGATIVE_CHECK_NORM,
    }
    for constraint_name, expected in revision_constraints.items():
        actual = _constraint_def(cur, constraint_name, "geo_observation_aggregate_bucket_revision")
        if re.sub(r"\s+", "", actual or "") != expected:
            errors.append(f"{constraint_name} 缺失/定义漂移")
    dirty_index = _index_def(cur, "idx_geo_obs_bucket_revision_dirty")
    if re.sub(r"\s+", "", dirty_index or "") != (
        "CREATEINDEXidx_geo_obs_bucket_revision_dirtyONpublic."
        "geo_observation_aggregate_bucket_revisionUSINGbtree(dirty,bucket_start,"
        "scope_type,bucket_granularity)WHERE(dirty=true)"
    ):
        errors.append("idx_geo_obs_bucket_revision_dirty 缺失/定义漂移")
    receipt_index = _index_def(cur, "idx_geo_obs_bucket_revision_receipt_gin")
    if re.sub(r"\s+", "", receipt_index or "") != (
        "CREATEINDEXidx_geo_obs_bucket_revision_receipt_ginONpublic."
        "geo_observation_aggregate_bucket_revisionUSINGgin"
        "(published_receiptjsonb_path_ops)WHERE(published_receiptISNOTNULL)"
    ):
        errors.append("idx_geo_obs_bucket_revision_receipt_gin 缺失/定义漂移")

    cur.execute(
        """SELECT column_name,data_type,is_nullable,character_maximum_length
             FROM information_schema.columns
            WHERE table_schema='public' AND table_name='geo_observation_insight_jobs'
              AND column_name IN ('snapshot_id','policy_basis_hash','scope_type',
                  'bucket_granularity','bucket_start','bucket_epoch',
                  'promotion_sequence_watermark','aggregate_input_watermark',
                  'aggregate_contract_version','aggregate_aggregation_version',
                  'aggregate_metric_version','paid_call_started_at','paid_call_unknown_at',
                  'budget_reserved_at')"""
    )
    insight_columns = {
        row["column_name"]: (
            row["data_type"], row["is_nullable"], row["character_maximum_length"]
        ) for row in cur.fetchall()
    }
    expected_insight_columns = {
        "snapshot_id": ("character", "YES", 64),
        "policy_basis_hash": ("text", "YES", None),
        "scope_type": ("text", "YES", None),
        "bucket_granularity": ("text", "YES", None),
        "bucket_start": ("date", "YES", None),
        "bucket_epoch": ("bigint", "YES", None),
        "promotion_sequence_watermark": ("bigint", "YES", None),
        "aggregate_input_watermark": ("timestamp with time zone", "YES", None),
        "aggregate_contract_version": ("text", "YES", None),
        "aggregate_aggregation_version": ("text", "YES", None),
        "aggregate_metric_version": ("text", "YES", None),
        "paid_call_started_at": ("timestamp with time zone", "YES", None),
        "paid_call_unknown_at": ("timestamp with time zone", "YES", None),
        "budget_reserved_at": ("timestamp with time zone", "YES", None),
    }
    for column, expected in expected_insight_columns.items():
        if insight_columns.get(column) != expected:
            errors.append(f"insight durable paid/snapshot 列定义漂移: {column}")
    insight_constraints = {
        "geo_obs_insight_state_chk": _INSIGHT_STATE_CHECK_NORM,
        "chk_geo_obs_insight_snapshot": _INSIGHT_SNAPSHOT_CHECK_NORM,
        "chk_geo_obs_insight_budget_reservation":
            _INSIGHT_BUDGET_RESERVATION_CHECK_NORM,
        "geo_obs_insight_scope_snapshot_uk":
            "UNIQUE(owner_user_id,brand_id,input_hash,snapshot_id)",
    }
    for constraint_name, expected in insight_constraints.items():
        actual = _constraint_def(cur, constraint_name, "geo_observation_insight_jobs")
        if re.sub(r"\s+", "", actual or "") != expected:
            errors.append(f"{constraint_name} 缺失/定义漂移")
    cur.execute(
        """SELECT c.conname,array_agg(a.attname ORDER BY key.ordinality) AS columns
             FROM pg_constraint c
             JOIN LATERAL unnest(c.conkey) WITH ORDINALITY key(attnum,ordinality)
               ON TRUE
             JOIN pg_attribute a
               ON a.attrelid=c.conrelid AND a.attnum=key.attnum
            WHERE c.conrelid='public.geo_observation_insight_jobs'::regclass
              AND c.contype='u'
            GROUP BY c.conname ORDER BY c.conname"""
    )
    insight_unique_constraints = [
        (row["conname"], list(row["columns"])) for row in cur.fetchall()
    ]
    expected_insight_unique = [(
        "geo_obs_insight_scope_snapshot_uk",
        ["owner_user_id", "brand_id", "input_hash", "snapshot_id"],
    )]
    if insight_unique_constraints != expected_insight_unique:
        errors.append(f"insight jobs UNIQUE 集合漂移: {insight_unique_constraints!r}")
    cur.execute(
        """SELECT idx.relname AS index_name,
                  array_agg(att.attname ORDER BY key.ordinality) AS columns
             FROM pg_index i
             JOIN pg_class idx ON idx.oid=i.indexrelid
             JOIN LATERAL unnest(i.indkey::smallint[]) WITH ORDINALITY key(attnum,ordinality)
               ON TRUE
             LEFT JOIN pg_attribute att
               ON att.attrelid=i.indrelid AND att.attnum=key.attnum
             LEFT JOIN pg_constraint c ON c.conindid=i.indexrelid
            WHERE i.indrelid='public.geo_observation_insight_jobs'::regclass
              AND i.indisunique AND NOT i.indisprimary AND c.oid IS NULL
            GROUP BY idx.relname"""
    )
    raw_unique_indexes = [
        (row["index_name"], list(row["columns"])) for row in cur.fetchall()
    ]
    if raw_unique_indexes:
        errors.append(f"insight jobs 存在额外 UNIQUE 索引: {raw_unique_indexes!r}")
    insight_recovery_index = _index_def(cur, "idx_geo_obs_insight_paid_recovery")
    if re.sub(r"\s+", "", insight_recovery_index or "") != (
        "CREATEINDEXidx_geo_obs_insight_paid_recoveryONpublic.geo_observation_insight_jobs"
        "USINGbtree(state,lease_until)WHERE(state=ANY(ARRAY['pending'::text,'running'::text,"
        "'paid_call_started'::text,'result_unknown'::text]))"
    ):
        errors.append("idx_geo_obs_insight_paid_recovery 缺失/定义漂移")

    expected_manifest_columns = {
        "manifest_key": ("text", "NO"),
        "policy_basis_hash": ("text", "NO"),
        "policy_version": ("text", "NO"),
        "contract_version": ("text", "NO"),
        "aggregation_version": ("text", "NO"),
        "metric_version": ("text", "NO"),
        "scope_type": ("text", "NO"),
        "bucket_granularity": ("text", "NO"),
        "bucket_start": ("date", "NO"),
        "bucket_end": ("date", "NO"),
        "input_watermark": ("timestamp with time zone", "YES"),
        "eligibility_epoch": ("bigint", "NO"),
        "promotion_sequence_watermark": ("bigint", "NO"),
        "expected_scope_cell_count": ("bigint", "NO"),
        "expected_scope_cell_fingerprint": ("text", "NO"),
        "aggregate_key_fingerprint": ("text", "NO"),
        "eligible_observation_count": ("bigint", "NO"),
        "overall_cell_count": ("bigint", "NO"),
        "aggregate_row_count": ("bigint", "NO"),
        "completed_at": ("timestamp with time zone", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
    }
    cur.execute(
        """SELECT column_name,data_type,is_nullable FROM information_schema.columns
             WHERE table_schema='public'
               AND table_name='geo_observation_aggregate_refresh_manifest'"""
    )
    actual_manifest_columns = {
        row["column_name"]: (row["data_type"], row["is_nullable"])
        for row in cur.fetchall()
    }
    for column, expected in expected_manifest_columns.items():
        if actual_manifest_columns.get(column) != expected:
            errors.append(f"aggregate manifest 列定义漂移: {column}")
    cur.execute(
        """SELECT column_name,lower(column_default) AS column_default
             FROM information_schema.columns
            WHERE table_schema='public'
              AND table_name='geo_observation_aggregate_refresh_manifest'
              AND column_name IN ('created_at','updated_at')"""
    )
    manifest_defaults = {
        row["column_name"]: row["column_default"] for row in cur.fetchall()
    }
    for column in ("created_at", "updated_at"):
        if manifest_defaults.get(column) != "now()":
            errors.append(f"aggregate manifest 默认值漂移: {column}")
    cur.execute(
        """SELECT column_name,column_default FROM information_schema.columns
             WHERE table_schema='public'
               AND table_name='geo_observation_aggregate_refresh_manifest'
               AND column_name IN ('promotion_sequence_watermark','expected_scope_cell_count',
                                   'expected_scope_cell_fingerprint','aggregate_key_fingerprint')"""
    )
    for row in cur.fetchall():
        if row["column_default"] is not None:
            errors.append(f"aggregate manifest snapshot 默认值漂移: {row['column_name']}")
    manifest_checks = {
        "chk_geo_obs_agg_manifest_basis": _MANIFEST_BASIS_CHECK_NORM,
        "chk_geo_obs_agg_manifest_scope": _MANIFEST_SCOPE_CHECK_NORM,
        "chk_geo_obs_agg_manifest_granularity": _MANIFEST_GRANULARITY_CHECK_NORM,
        "chk_geo_obs_agg_manifest_bounds": _MANIFEST_BOUNDS_CHECK_NORM,
        "chk_geo_obs_agg_manifest_counts": _MANIFEST_COUNTS_CHECK_NORM,
        "chk_geo_obs_agg_manifest_fingerprints": _MANIFEST_FINGERPRINTS_CHECK_NORM,
        "geo_observation_aggregate_refresh_manifest_pkey": "PRIMARYKEY(manifest_key)",
    }
    for constraint_name, expected in manifest_checks.items():
        actual = _constraint_def(
            cur, constraint_name, "geo_observation_aggregate_refresh_manifest"
        )
        if re.sub(r"\s+", "", actual or "") != expected:
            errors.append(f"{constraint_name} 缺失/定义漂移")
    expected_indexes = {
        "idx_geo_obs_agg_policy_basis_scope_bucket":
            "CREATEINDEXidx_geo_obs_agg_policy_basis_scope_bucketONpublic.geo_observation_aggregatesUSINGbtree(policy_basis_hash,scope_type,bucket_granularity,bucket_start)",
        "uq_geo_obs_agg_manifest_cell":
            "CREATEUNIQUEINDEXuq_geo_obs_agg_manifest_cellONpublic.geo_observation_aggregate_refresh_manifestUSINGbtree(policy_basis_hash,contract_version,aggregation_version,metric_version,scope_type,bucket_granularity,bucket_start)",
        "idx_geo_obs_agg_manifest_basis_scope":
            "CREATEINDEXidx_geo_obs_agg_manifest_basis_scopeONpublic.geo_observation_aggregate_refresh_manifestUSINGbtree(policy_basis_hash,scope_type,bucket_granularity,bucket_start)",
        "uq_geo_obs_event_promotion_seq":
            "CREATEUNIQUEINDEXuq_geo_obs_event_promotion_seqONpublic.geo_observation_eventsUSINGbtree(promotion_seq)WHERE(promotion_seqISNOTNULL)",
    }
    for index_name, expected in expected_indexes.items():
        actual = _index_def(cur, index_name)
        if re.sub(r"\s+", "", actual or "") != expected:
            errors.append(f"{index_name} 缺失/定义漂移")
    for function_name, expected in (
        ("geo_obs_bump_eligibility_epoch_on_event", _EVENT_ELIGIBILITY_FN_NORM),
        ("geo_obs_bump_public_epoch_on_bucket", _BUCKET_ELIGIBILITY_FN_NORM),
        ("geo_obs_assign_promotion_seq", _PROMOTION_SEQ_FN_NORM),
        ("geo_obs_reject_promoted_input_mutation", _PROMOTED_INPUT_IMMUTABLE_FN_NORM),
        ("geo_obs_reject_published_event_mutation", _PUBLISHED_EVENT_IMMUTABLE_FN_NORM),
        ("geo_obs_mark_aggregate_buckets_dirty", _MARK_BUCKETS_DIRTY_FN_NORM),
    ):
        cur.execute(
            """SELECT pg_get_functiondef(oid) AS d FROM pg_proc
                 WHERE proname=%s AND pronamespace='public'::regnamespace""",
            (function_name,),
        )
        function = cur.fetchone()
        if function is None or re.sub(r"\s+", "", function["d"]) != expected:
            errors.append(f"{function_name} 缺失/函数体漂移")
    for trigger_name, table_name, expected in (
        ("trg_geo_obs_event_eligibility_epoch", "geo_observation_events",
         _EVENT_ELIGIBILITY_TRIGGER_NORM),
        ("trg_geo_obs_bucket_eligibility_epoch", "geo_observation_contributor_buckets",
         _BUCKET_ELIGIBILITY_TRIGGER_NORM),
        ("trg_geo_obs_assign_promotion_seq", "geo_observation_events",
         _PROMOTION_SEQ_TRIGGER_NORM),
        ("trg_geo_obs_signal_promoted_immutable", "geo_observation_signals",
         _SIGNAL_IMMUTABLE_TRIGGER_NORM),
        ("trg_geo_obs_bucket_promoted_immutable", "geo_observation_contributor_buckets",
         _BUCKET_IMMUTABLE_TRIGGER_NORM),
        ("trg_geo_obs_event_published_immutable", "geo_observation_events",
         _PUBLISHED_EVENT_IMMUTABLE_TRIGGER_NORM),
    ):
        cur.execute(
            """SELECT pg_get_triggerdef(oid) AS d,tgenabled FROM pg_trigger
                 WHERE tgname=%s AND tgrelid=%s::regclass AND NOT tgisinternal""",
            (trigger_name, f"public.{table_name}"),
        )
        trigger = cur.fetchone()
        if trigger is None or re.sub(r"\s+", "", trigger["d"]) != expected:
            errors.append(f"{trigger_name} 缺失/定义漂移")
        elif trigger["tgenabled"] not in ("O", "A"):
            errors.append(f"{trigger_name} 被禁用")

    if errors:
        raise ObservationSchemaNotReady(errors)


def verify_on_startup() -> None:
    """web/cron 启动调用:fail-closed。"""
    conn = get_connection()
    try:
        verify_geo_observation_schema(conn.cursor())
    finally:
        conn.close()
