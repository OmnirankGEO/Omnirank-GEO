"""三来源 source hook —— 只读核验源业务终态,登记 geo_observation_events(不改任何源/资金状态)。

铁律:
- R8 事务边界:hook 接收源事务 cursor(同 SAVEPOINT);无法同事务接线的源由 reconciler 从终态表补登记。
- 付费诊断(R1/R4):只读 diagnosis_runs/diagnosis_records/diagnosis_refund_records;绝不调 billing。
- 公共调研(R2):仅 geo_research_round.status='completed';partial_success 一律不晋升。
- 持续监测(R3):task completed + total/completed 匹配 + 归属一致 + 答案非空;证明不全→private_only,绝不 not_mentioned。
- 血缘(provider/model/surface):历史源行只有粗粒度 engine 键 → 确定性映射派生(非读源列);AI-1 live 信封供真血缘。

登记只把源事实引用写入 events(pending);是否 promoted/private_only/rejected 由 promotion.py 决定。
不可登记(退款/released/未终态)→ 不建 event;既有 event 由 reconciler 撤回。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from . import repository

# 诊断 run 资金成功终态(services/diagnosis_runs.py:43)
DIAG_SUCCESS_TERMINAL = ("committed", "completed_exempt")
# 明确不可登记/不可晋升的诊断终态(R4:禁发明状态)
DIAG_BLOCKED = ("released", "failed_exempt", "cancelled", "cancelled_no_freeze",
                "settlement_manual", "manual_resolving", "delivery_repair_pending")

# 历史源行 engine 键 → 血缘派生(读侧诚实映射;AI-1 live 信封另供真血缘)。surface 必属契约 11 枚举。
# 铁律:历史粗粒度记录只能标真实通道/legacy,绝不冒充官方 native。当前 DeepSeek 真实联网通道 =
#   DashScope 代理 → deepseek_dashscope_search_legacy(provider=dashscope);model 具体版本不可证明 → 保持未知(None)。
_LINEAGE_BY_ENGINE = {
    #  engine      platform     provider(真实通道)  model(DashScope 代理真实模型)  surface(诚实通道)
    "dashscope": ("qwen",     "dashscope",  "qwen3.7-plus",     "qwen_dashscope_search"),
    "qwen":      ("qwen",     "dashscope",  "qwen3.7-plus",     "qwen_dashscope_search"),
    "deepseek":  ("deepseek", "dashscope",  "deepseek-v4-flash", "deepseek_dashscope_search_legacy"),
    "doubao":    ("doubao",   "volcengine", "doubao-ai-search",  "doubao_ark_api_search"),
    "yuanbao":   ("yuanbao",  "tencent_tokenhub", "hy3",         "yuanbao_hy3_tokenhub"),
    "kimi":      ("kimi",     "moonshot",   "kimi-k2.6",         "other_explicit"),  # Kimi 退出默认,无专属 surface
}


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _lineage(engine: str) -> tuple[str, str, str, str]:
    return _LINEAGE_BY_ENGINE.get((engine or "").strip().lower(), (engine or "other", engine or "other", engine or "other", "other_explicit"))


@dataclass
class SourceObservation:
    """一条待登记观测 + 私域载荷(answer/question 供隐私+实体,不写入公共 signal)。"""
    event_fields: dict
    answer_text: str          # 私域;只算 answer_hash 入 event,不入 signal
    question_text: str        # 客户可理解的规范问题(私域)
    is_detected: Optional[bool] = None   # 源已判定的提及(用于 outcome 交叉核)
    citations: list = field(default_factory=list)
    competitor_count: int = 0
    # [CUR-07 · 防御型 GEO WP1] 采集期已判定的推荐档位。
    #   此前这一格在抽取层就被丢掉,报告只能靠启发式反推「是不是推荐」——
    #   规格 §2.2 CUR-07 要求先修 producer 接线,再开放指标卡。
    #   None = 该源没给出判定(不是"判定为否"),下游必须按未知处理,不得当 False。
    #
    #   ✅ 这一格是**真判定**:producer 走
    #      services.geo_observation.entity_review.classify_outcome(确定性分类器,零 provider 调用),
    #      见 tools/ai_visibility/ai_tester.py::_resolve_target_outcome。
    source_target_outcome: Optional[str] = None

    # 🔴 [CUR-07 · 诚实性订正 2026-08-21] 名字里带 heuristic 不是修辞,是**规格红线**。
    #
    #   CUR-07 原文把 ``is_recommended`` 与 ``target_outcome`` 并列成"推荐判定"。
    #   在 41 班底座上逐行核过 producer 后,这个前提**不成立**:
    #     tools/ai_visibility/ai_tester.py:2813-2815
    #         first_position = decision.matched_start          # 答案正文里的**字符偏移**
    #         line_index = ai_response.count("\n", 0, first_position)
    #         visibility_result["is_recommended"] = line_index < 5
    #   —— 它的真实语义是「品牌名出现在答案前 5 行」,一个**叙述位置**启发式,
    #      跟"AI 是否推荐了这个品牌"没有关系。
    #
    #   所以字段接回来(不丢信息),但绝不许冒充推荐:
    #     · 禁止进入 recommendation 分子(§19 变异 29 → MET-02
    #       「mentioned_only 算明确推荐」必红);
    #     · 禁止反推名次(§19 变异 30/33 → MET-03/MET-06);
    #     · 真推荐档位只认 ``source_target_outcome``。
    #   现役唯一合法用法是老行兜底桥(services/public_report_presentation.py
    #   ::_legacy_recommendation_outcome),那里它只是并列条件之一,不单独定档。
    source_recommended_heuristic: Optional[bool] = None


def _normalize_citations(raw: Any) -> list:
    """把各源的引用载荷归一成 ``list[dict]``(``clean_source_domains`` 要的形状)。

    三种真实形态都要吃下:
      · ``list[dict]``  —— 诊断 detail_table 里已经是解析好的对象
      · ``str``         —— ``monitoring_results.search_citations`` 列是 TEXT,存 JSON 字符串
      · 其它/坏 JSON    —— 返回 ``[]``,绝不抛(一条坏引用不该让整条观测登记失败)

    🔴 只保留 dict 元素:``privacy.clean_source_domains`` 读 ``c.get("url")``,
       喂进裸字符串会被它静默跳过 —— 那样「解析对了」和「解析错了」看起来一样。
    """
    if raw in (None, "", b""):
        return []
    value = raw
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _competitor_count(raw: Any) -> int:
    """``monitoring_results.competitors_mentioned``(JSONB 数组)→ 共现家数。

    psycopg2 对 JSONB 列已经解成 Python 对象,但历史行/别的写入路径可能留下 JSON 字符串,
    两种都吃。非数组一律 0 —— 宁可少算也不臆造共现数(共现数会进公共 signal)。
    """
    if raw in (None, "", b""):
        return 0
    value = raw
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return 0
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return 0
    if not isinstance(value, list):
        return 0
    return len(value)


@dataclass
class SourceGate:
    """源终态门。registerable=可登记;promotable=满足严格晋升资格;reasons=拒绝码。"""
    registerable: bool
    promotable: bool
    terminal_state: str
    reasons: list[str] = field(default_factory=list)
    owner_user_id: Optional[int] = None
    brand_id: Optional[int] = None
    industry_key: Optional[str] = None


# ══════════════════════════ 付费诊断(R1/R4)══════════════════════════
def assess_paid_diagnosis_run(cur, run_token: str) -> SourceGate:
    """只读核验诊断 run 资金/交付终态(R1)。绝不调 billing;绝不发明状态(R4)。"""
    # LEFT JOIN 可能命中多条 diagnosis_records(run_token 无 UNIQUE)→ ORDER BY 确定性取最新一条,
    # 与 build 的 raw_data_json 读取绑定同一条(防 gate 校验 A 记录、抽取 B 记录不一致)。
    cur.execute(
        """
        SELECT dr.run_status, dr.billing_mode, dr.owner_user_id, dr.brand_id,
               rec.run_token AS rec_run_token, rec.result_visibility, rec.total_score, rec.brand_id AS rec_brand_id,
               (SELECT count(*) FROM diagnosis_refund_records rf WHERE rf.run_token = dr.run_token) AS refund_cnt
          FROM diagnosis_runs dr
          LEFT JOIN diagnosis_records rec ON rec.id = (
              SELECT id FROM diagnosis_records WHERE run_token = dr.run_token ORDER BY id DESC LIMIT 1)
         WHERE dr.run_token = %s
        """,
        (run_token,),
    )
    row = cur.fetchone()
    if not row:
        return SourceGate(False, False, "missing_run", ["run_not_found"])
    st = row["run_status"]
    refund_cnt = int(row["refund_cnt"] or 0)
    reasons: list[str] = []

    # 行业键从品牌派生(与监测同源,保证跨来源 prompt_family_key 一致)
    industry_key = None
    if row["brand_id"] is not None:
        cur.execute("SELECT industry FROM brands WHERE id=%s", (row["brand_id"],))
        b = cur.fetchone()
        industry_key = (b["industry"] if b else None) or None

    # 撤回信号 / 阻断终态 → 不可登记(既有 event 由 reconciler 撤回)
    if refund_cnt > 0:
        return SourceGate(False, False, st, ["refunded"], row["owner_user_id"], row["brand_id"], industry_key)
    if st in DIAG_BLOCKED:
        return SourceGate(False, False, st, [f"blocked_state:{st}"], row["owner_user_id"], row["brand_id"], industry_key)
    if st not in DIAG_SUCCESS_TERMINAL:
        # 非终态(pending_freeze/running/commit_pending/release_pending)→ 尚不可登记
        return SourceGate(False, False, st, [f"not_terminal:{st}"], row["owner_user_id"], row["brand_id"], industry_key)

    # 已到资金成功终态 → 可登记。以下为严格晋升资格(R1),不满足→promotable=False(promotion 判 private_only)
    if row["owner_user_id"] is None:
        reasons.append("missing_owner")
    if row["brand_id"] is None:
        reasons.append("missing_brand")   # 客户来源必须有品牌(否则无法做实体核验/R6 归属)
    if row["rec_run_token"] is None:
        reasons.append("missing_product")          # 产物不存在
    else:
        if row["rec_run_token"] != run_token:
            reasons.append("run_token_mismatch")
        if row["total_score"] is None:
            reasons.append("empty_shell_product")   # total_score NULL = 空壳非完整产物
        if row["result_visibility"] != "published":  # 严格:NULL 不自动晋升(R1)
            reasons.append(f"visibility_not_published:{row['result_visibility']}")
        if row["rec_brand_id"] is not None and row["brand_id"] is not None and row["rec_brand_id"] != row["brand_id"]:
            reasons.append("brand_mismatch")
    promotable = len(reasons) == 0
    return SourceGate(True, promotable, st, reasons, row["owner_user_id"], row["brand_id"], industry_key)


def _extract_diagnosis_observations(raw_data_json: Any) -> list[dict]:
    """从 diagnosis_records.raw_data_json 防御式抽取 per(engine,question)观测。

    真实生产形状(已对生产 producer 核验 · workflows/diagnosis_workflow.py:786/802-820 + tools/ai_visibility/ai_tester.py):
      data.ai_visibility.detail_table[]              (主问题)
      data.ai_visibility.custom_visibility.detail_table[]  (自定义高意图问题)
      item = {question:<str>, results:{engine:{answer_summary,brand_detected,mentioned_brands}}}
    松散 blob:任何不可识别/非 dict 顶层一律产出空(绝不臆造、绝不崩溃)。引擎失败样本("查询失败"/Error)不产观测。
    """
    if isinstance(raw_data_json, str):
        try:
            raw = json.loads(raw_data_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    else:
        raw = raw_data_json
    if not isinstance(raw, dict):        # 顶层非对象(list/str/number/bool)→ 空,不崩溃
        return []
    data = raw.get("data")
    if not isinstance(data, dict):
        return []
    av = data.get("ai_visibility")
    if not isinstance(av, dict):
        return []
    out: list[dict] = []
    sections = [("primary", av.get("detail_table"))]
    cv = av.get("custom_visibility")
    if isinstance(cv, dict):
        sections.append(("custom", cv.get("detail_table")))
    for section, detail in sections:
        if not isinstance(detail, list):
            continue
        for item in detail:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or item.get("query") or "").strip()
            results = item.get("results")
            if not question or not isinstance(results, dict):
                continue
            for engine, er in results.items():
                if not isinstance(er, dict):
                    continue
                answer = str(er.get("answer_summary") or er.get("response") or "")
                if "查询失败" in answer or answer.startswith("Error"):
                    continue   # 引擎失败样本不产观测(不伪装成功)
                mentioned = er.get("mentioned_brands")
                out.append({
                    "engine": str(engine).strip().lower(),
                    "question": question,
                    "section": section,
                    "detected": bool(er.get("brand_detected")) if er.get("brand_detected") is not None else None,
                    "answer": answer,
                    # 🔴 [CUR-07 真因 · 2026-08-21] 这里原来读的是 ``er["citations"]``,
                    #    而 detail_table 的生产者(tools/ai_visibility/ai_tester.py)从来只发
                    #    ``search_citations``。全仓没有任何 producer 往 results[engine] 写裸
                    #    ``citations`` —— 机械证据见
                    #    ``scripts/defgeo_census/detail_table_key_census.py``
                    #    (它比对 producer 键集合与本函数消费键集合,差集非空即红)。
                    #    后果不是报错,是**恒空**:付费诊断观测的 citations 一直是 [],
                    #    下游 promotion._build_signal 的 source_count / citation_count 恒 0、
                    #    quality_score 恒少 domain 那 +2500 分。全绿、无日志、无告警。
                    #    这就是本仓「读的键生产方从来不发」那类静默零。
                    #
                    #    不留 ``er.get("citations")`` 兼容回退:census 证明该键**从未**被任何
                    #    producer 写过(诊断侧引用能力是 Phase1-A 2026-06-07 才加的,加进来时
                    #    就叫 search_citations)。留一个永不命中的回退 = 让 census 永远报红,
                    #    还给人一种"已兼容历史"的错觉。
                    "citations": _normalize_citations(er.get("search_citations")),
                    "competitor_count": len(mentioned) if isinstance(mentioned, list) else 0,
                    # [CUR-07] 采集期判定原样带走,不在这里重判。
                    #   缺键 → None(未知),不是 False —— "源没说" 与 "源说不是" 必须分开。
                    "target_outcome": (
                        str(er["target_outcome"]).strip()
                        if isinstance(er.get("target_outcome"), str) and er["target_outcome"].strip()
                        else None
                    ),
                    # ⚠️ 键名据实:producer 的 is_recommended = "命中位置在前 5 行",
                    #    是叙述位置启发式,不是推荐判定。详见 SourceObservation 上的说明。
                    "recommended_heuristic": (
                        bool(er["is_recommended"]) if isinstance(er.get("is_recommended"), bool) else None
                    ),
                })
    return out


def build_paid_diagnosis_observations(cur, run_token: str) -> tuple[SourceGate, list[SourceObservation]]:
    """(R1 门 + 观测抽取)。registerable 时返回每条(engine,question)观测的登记草稿。"""
    gate = assess_paid_diagnosis_run(cur, run_token)
    if not gate.registerable:
        return gate, []
    cur.execute("SELECT raw_data_json FROM diagnosis_records WHERE run_token=%s ORDER BY id DESC LIMIT 1", (run_token,))
    rec = cur.fetchone()
    raw = rec["raw_data_json"] if rec else None
    obs_list = _extract_diagnosis_observations(raw)
    observations: list[SourceObservation] = []
    for o in obs_list:
        platform_key, provider_key, model_key, surface_key = _lineage(o["engine"])
        # 稳定 subkey:section+engine+问题哈希(顺序无关,防位置漂移;section 区分主/自定义防同题碰撞)
        subkey = f"{o['section']}:{o['engine']}:{_sha256(o['question'])[:16]}"
        question = o["question"]
        answer = o["answer"]
        observations.append(SourceObservation(
            event_fields=dict(
                source_type="paid_diagnosis", source_table="diagnosis_records",
                source_record_id=str(run_token), source_subkey=subkey,
                owner_user_id=gate.owner_user_id, brand_id=gate.brand_id, industry_key=gate.industry_key,
                prompt_fingerprint=_sha256(question),
                platform_key=platform_key, provider_key=provider_key, model_key=model_key, surface_key=surface_key,
                session_mode="accounted", country_code="CN",
                answer_hash=_sha256(answer),
                source_terminal_state=gate.terminal_state,
                observed_at=None,  # 由登记方补真时间(源无逐观测时间时用 run finished_at)
            ),
            answer_text=answer, question_text=question,
            is_detected=o["detected"], citations=o["citations"],
            competitor_count=o.get("competitor_count", 0),
            source_target_outcome=o.get("target_outcome"),
            source_recommended_heuristic=o.get("recommended_heuristic"),
        ))
    return gate, observations


def register_paid_diagnosis(cur, run_token: str, observed_at) -> list[tuple[dict, bool]]:
    """同事务(cur)登记付费诊断可登记观测(pending)。not registerable → 返回 []。"""
    gate, observations = build_paid_diagnosis_observations(cur, run_token)
    results = []
    for obs in observations:
        fields = dict(obs.event_fields)
        fields["observed_at"] = observed_at
        fields["processing_state"] = "pending"
        results.append(repository.register_event(cur, fields))
    return results


# ══════════════════════════ 公共调研(R2)══════════════════════════
def assess_research_raw(cur, raw_id: int) -> tuple[SourceGate, Optional[SourceObservation]]:
    """只读核验 geo_research_raw + 其 round 终态(R2:仅 completed;partial_success 不晋升)。"""
    cur.execute(
        """
        SELECT r.id, r.industry, r.query, r.engine, r.answer_text, r.is_answer_cited, r.adoption_rank,
               r.batch_id, r.created_at
          FROM geo_research_raw r WHERE r.id = %s
        """,
        (raw_id,),
    )
    row = cur.fetchone()
    if not row:
        return SourceGate(False, False, "missing_raw", ["raw_not_found"]), None
    # round 终态:batch_id = 'batch_' + round_id;取该 round.status
    round_id = str(row["batch_id"] or "").removeprefix("batch_")
    round_status = None
    if round_id:
        cur.execute("SELECT status FROM geo_research_round WHERE round_id=%s", (round_id,))
        rr = cur.fetchone()
        round_status = rr["status"] if rr else None
    reasons: list[str] = []
    if round_status != "completed":  # R2:仅 completed;partial_success/failed/其它一律不晋升
        reasons.append(f"round_not_completed:{round_status}")
    answer = row["answer_text"] or ""
    if not answer.strip():
        reasons.append("empty_answer")
    industry = str(row["industry"] or "").strip()
    if not industry:
        reasons.append("missing_industry")
    registerable = round_status == "completed" and bool(answer.strip())
    promotable = len(reasons) == 0
    if not registerable:
        return SourceGate(False, False, round_status or "unknown", reasons, industry_key=industry or None), None

    engine = str(row["engine"] or "").strip().lower()
    platform_key, provider_key, model_key, surface_key = _lineage(engine)
    question = str(row["query"] or "")
    obs = SourceObservation(
        event_fields=dict(
            source_type="research_round", source_table="geo_research_raw",
            source_record_id=str(row["id"]), source_subkey=round_id,
            owner_user_id=None, brand_id=None, industry_key=industry or None,
            prompt_fingerprint=_sha256(question),
            platform_key=platform_key, provider_key=provider_key, model_key=model_key, surface_key=surface_key,
            session_mode="clean", country_code="CN",
            answer_hash=_sha256(answer),
            source_terminal_state=round_status,
            observed_at=row["created_at"],
        ),
        answer_text=answer, question_text=question,
        is_detected=None, citations=[],
    )
    return SourceGate(True, promotable, round_status, reasons, industry_key=industry or None), obs


def register_research_raw(cur, raw_id: int) -> Optional[tuple[dict, bool]]:
    gate, obs = assess_research_raw(cur, raw_id)
    if not gate.registerable or obs is None:
        return None
    fields = dict(obs.event_fields)
    fields["processing_state"] = "pending"
    return repository.register_event(cur, fields)


# ══════════════════════════ 持续监测(R3)══════════════════════════
def assess_monitoring_result(cur, result_id: int) -> tuple[SourceGate, Optional[SourceObservation]]:
    """只读核验监测结果完整性/归属(R3)。证明不全→registerable=True 但 promotable=False(promotion 判 private_only)。

    要点:task completed + completed_at + total_tests>0 + completed_tests==total_tests + 答案非空 +
    经 task→brand 归属 + 经(quote_id,keyword)确定购买短句(确定性,非 LLM);orphan → 不可登记。
    """
    cur.execute(
        """
        SELECT mr.id, mr.task_id, mr.keyword, mr.platform, mr.is_detected, mr.mention_type,
               mr.identity_review_state, mr.response_status,
               mr.full_response, mr.response_snippet, mr.tested_at,
               -- [CUR-09 · 防御型 GEO WP1] 竞品共现与引用此前根本没被 SELECT 出来,
               --   观测硬编码 citations=[]、competitor_count 走 dataclass 默认 0 ——
               --   共现与归因数据在这里断链(规格 §2.2 CUR-09「先接回原始字段,再做聚合」)。
               --   列类型已按 §16.1 核过(db/monitoring_db.py::_safe_add_column):
               --     search_citations      TEXT                      (JSON 字符串)
               --     competitors_mentioned JSONB DEFAULT '[]'::jsonb (已是数组)
               --   两者类型不同,统一交给 _normalize_citations / _competitor_count 归一。
               mr.search_citations, mr.competitors_mentioned,
               mr.target_outcome,
               mt.status AS task_status, mt.completed_at, mt.total_tests, mt.completed_tests,
               mt.brand_id, mt.client_id
          FROM monitoring_results mr JOIN monitoring_tasks mt ON mt.id = mr.task_id
         WHERE mr.id = %s
        """,
        (result_id,),
    )
    row = cur.fetchone()
    if not row:
        return SourceGate(False, False, "missing_result", ["result_not_found"]), None
    if (
        row["identity_review_state"] == "pending"
        or row["response_status"] == "brand_identity_unresolved"
    ):
        return SourceGate(
            False,
            False,
            "brand_identity_unresolved",
            ["identity_review_pending"],
            brand_id=row["brand_id"],
        ), None

    reasons: list[str] = []
    task_status = row["task_status"]
    if task_status != "completed" or row["completed_at"] is None:
        return SourceGate(False, False, task_status or "unknown", [f"task_not_completed:{task_status}"],
                          brand_id=row["brand_id"]), None
    total = row["total_tests"]
    completed = row["completed_tests"]
    if not total or int(total) <= 0:
        reasons.append("total_tests_zero")
    if completed is None or total is None or int(completed) != int(total):
        reasons.append(f"incomplete_batch:{completed}/{total}")
    answer = row["full_response"] or row["response_snippet"] or ""
    if len(answer.strip()) < 50:  # 空答/连接失败(镜像 reverify realness 50 字门)
        reasons.append("empty_or_short_answer")

    # 归属:brand → owner
    brand_id = row["brand_id"]
    owner_user_id = None
    if brand_id is not None:
        cur.execute("SELECT owner_user_id, industry FROM brands WHERE id=%s", (brand_id,))
        b = cur.fetchone()
        if b:
            owner_user_id = b["owner_user_id"]
    if owner_user_id is None:
        reasons.append("missing_owner")

    # 购买短句(确定性):经(quote_id=client_id, keyword)找 monitoring_query;找不到 → 无法确证购买短句归属
    purchased_phrase = None
    industry_key = None
    quote_id = row["client_id"]
    kw = (row["keyword"] or "").strip()
    if quote_id and kw:
        for tbl in ("confirmed_keywords", "extra_keywords"):
            cur.execute(
                f"SELECT monitoring_query FROM {tbl} WHERE quote_id=%s AND keyword=%s LIMIT 1",
                (quote_id, kw),
            )
            krow = cur.fetchone()
            if krow is not None:
                mq = (krow["monitoring_query"] or "").strip()
                purchased_phrase = mq if mq else kw   # 显式短句或裸关键词(均确定性,非 LLM 改写)
                break
    if purchased_phrase is None:
        reasons.append("keyword_provenance_unresolved")  # 无法确证购买短句 → private_only(R3)

    # 行业:取 brand.industry
    if brand_id is not None:
        cur.execute("SELECT industry FROM brands WHERE id=%s", (brand_id,))
        bb = cur.fetchone()
        industry_key = (bb["industry"] if bb else None)

    engine = str(row["platform"] or "").strip().lower()
    platform_key, provider_key, model_key, surface_key = _lineage(engine)
    question = purchased_phrase or kw
    registerable = task_status == "completed" and row["completed_at"] is not None and owner_user_id is not None
    promotable = len(reasons) == 0
    if not registerable:
        return SourceGate(False, False, task_status, reasons, owner_user_id, brand_id, industry_key), None

    obs = SourceObservation(
        event_fields=dict(
            source_type="recurring_monitoring", source_table="monitoring_results",
            source_record_id=str(row["id"]), source_subkey="",   # result.id 全局唯一
            owner_user_id=owner_user_id, brand_id=brand_id, industry_key=industry_key,
            prompt_fingerprint=_sha256(question),
            platform_key=platform_key, provider_key=provider_key, model_key=model_key, surface_key=surface_key,
            session_mode="accounted", country_code="CN",
            answer_hash=_sha256(answer),
            source_terminal_state=task_status,
            observed_at=row["tested_at"],
        ),
        answer_text=answer, question_text=question,
        is_detected=(int(row["is_detected"]) == 1) if row["is_detected"] is not None else None,
        # [CUR-09] 接回真实引用与共现,不再恒空/恒 0。
        citations=_normalize_citations(row.get("search_citations")),
        competitor_count=_competitor_count(row.get("competitors_mentioned")),
        # 监测侧 target_outcome 是列(services/monitoring_lineage.classify_target_outcome 写的),
        # 'legacy_unknown' 哨兵与 NULL 一律按"源没判定"处理,不冒充判定结果。
        source_target_outcome=(
            str(row["target_outcome"]).strip()
            if isinstance(row.get("target_outcome"), str)
            and row["target_outcome"].strip()
            and row["target_outcome"].strip() != "legacy_unknown"
            else None
        ),
    )
    return SourceGate(True, promotable, task_status, reasons, owner_user_id, brand_id, industry_key), obs


def register_monitoring_result(cur, result_id: int) -> Optional[tuple[dict, bool]]:
    gate, obs = assess_monitoring_result(cur, result_id)
    if not gate.registerable or obs is None:
        return None
    fields = dict(obs.event_fields)
    fields["processing_state"] = "pending"
    return repository.register_event(cur, fields)
