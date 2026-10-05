"""小榜统一客户运营计划（只读）。

本模块不创建任务、不写快照、不计算价格。它只把现役报价容量、P4 快照、客户
知识、写作、发布和监测事实装配成一个 ``CustomerOperationPlan``，供页面与小榜
共同消费。任何一个可选读侧暂时不可用都局部降级，并给出可执行出口。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from auth.brand_access import require_brand_access, require_quote_access
from services.gap_operation_map import all_operations

logger = logging.getLogger("GEO-CustomerOperationPlan")

PLAN_VERSION = "customer-operation-plan-v1"


def _positive_int(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} 格式无效，请重新选择客户") from exc
    if parsed <= 0:
        raise HTTPException(status_code=422, detail=f"{field} 格式无效，请重新选择客户")
    return parsed


def _iso(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value or "").strip()
    return text or None


def _latest_timestamp(values: list[Any]) -> str | None:
    clean = [_iso(v) for v in values]
    return max((v for v in clean if v), default=None)


def _page_context(current_page: str) -> tuple[str, str]:
    path = urlsplit(str(current_page or "")).path or "/"
    candidates = [
        entry for entry in all_operations()
        if "{" not in entry.route_template
        and urlsplit(entry.route_template).path == path
    ]
    entry = candidates[0] if candidates else None
    return path[:256], entry.display_name if entry else "当前页面"


def _fetch_row(sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _resource_hint(kind: str, resource_id: int) -> dict[str, Any] | None:
    # 列名已于 2026-08-10 对 PostgreSQL 16 replica schema 逐表核验。
    queries = {
        "article":
            "SELECT a.id, COALESCE(a.brand_id, q.brand_id) AS brand_id, "
            "a.quote_id, a.updated_at FROM articles a "
            "LEFT JOIN quotes q ON q.id = a.quote_id WHERE a.id = %s",
        "publication":
            "SELECT p.id, COALESCE(p.brand_id, q.brand_id) AS brand_id, p.quote_id, "
            "p.created_at AS updated_at FROM media_publications p "
            "LEFT JOIN quotes q ON q.id = p.quote_id WHERE p.id = %s",
        "monitoring":
            "SELECT t.id, COALESCE(t.brand_id, q.brand_id) AS brand_id, t.quote_id, "
            "COALESCE(t.completed_at, t.started_at, t.created_at) AS updated_at "
            "FROM monitoring_tasks t LEFT JOIN quotes q ON q.id = t.quote_id WHERE t.id = %s",
        # [R3-P11 ①] GEO 图文。列名已对 PostgreSQL 16 schema 核过:
        # geo_douyin_posts 有 brand_id / deleted_at / updated_at,**没有 quote_id**
        # —— 所以显式给 NULL,不假装它能带出报价(带错报价 = 算错钱)。
        #
        # 🔴 `deleted_at IS NULL` 是刻意加的:软删的图文查不出行 ⇒ 走与
        #    「跨租户 / 猜 id」完全相同的那一个 404,不新增出口。
        #    兄弟 kind(article/publication/monitoring)没有这一笔是因为它们的表
        #    没有软删列;quotes 的软删则在下面单独判过。判据见
        #    test_soft_deleted_geo_post_is_404_like_any_other_unavailable_object。
        "geo_post":
            "SELECT p.id, p.brand_id, NULL::integer AS quote_id, p.updated_at "
            "FROM geo_douyin_posts p WHERE p.id = %s AND p.deleted_at IS NULL",
    }
    return _fetch_row(queries[kind], (resource_id,))


def _latest_quote_for_brand(brand_id: int) -> dict[str, Any] | None:
    return _fetch_row(
        "SELECT * FROM quotes WHERE brand_id = %s AND deleted_at IS NULL "
        "ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'paid' THEN 1 "
        "WHEN 'active' THEN 2 ELSE 3 END, updated_at DESC NULLS LAST, created_at DESC LIMIT 1",
        (brand_id,),
    )


@dataclass(frozen=True)
class AuthorizedAssistantContext:
    brand_id: int | None
    brand_name: str | None
    quote_id: int | None
    article_id: int | None
    publication_id: int | None
    monitoring_task_id: int | None
    current_page: str
    page_name: str
    data_updated_at: str | None
    quote: Mapping[str, Any] | None
    # [R3-P11 ①] 带默认值 + 追加在**最后一位**:既有构造点(含判据里那处)
    # 全是关键字调用,这样加一字节都不用改它们 —— 「默认参数 = 旧行为不变」
    # 必须落到字节,不是落到语义。
    geo_post_id: int | None = None

    def refs(self) -> dict[str, int]:
        return {
            key: value for key, value in {
                "brand_id": self.brand_id,
                "quote_id": self.quote_id,
                "article_id": self.article_id,
                "publication_id": self.publication_id,
                "monitoring_task_id": self.monitoring_task_id,
                "geo_post_id": self.geo_post_id,
            }.items() if value is not None
        }

    def public_context(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "brand_name": self.brand_name,
            "quote_id": self.quote_id,
            "current_page": self.current_page,
            "page_name": self.page_name,
            "data_updated_at": self.data_updated_at,
            "has_customer": self.brand_id is not None,
            # [R3-P11 ①] 只暴露 id,不暴露标题 —— 图文标题可能含客户敏感表述,
            # 而这个投影会进 preview、进前端。要名字走 object_items 的 label 通道。
            "geo_post_id": self.geo_post_id,
        }


def resolve_authorized_context(
    request: Request,
    context_refs: Mapping[str, Any] | None,
    current_page: str,
) -> AuthorizedAssistantContext:
    """逐个重验浏览器提示；任何跨客户组合统一 404，不回显名称或 id。"""
    refs = dict(context_refs or {})
    brand_id = _positive_int(refs.get("brand_id") or refs.get("client_id"), "客户提示")
    quote_id = _positive_int(refs.get("quote_id"), "报价提示")
    article_id = _positive_int(refs.get("article_id"), "文章提示")
    publication_id = _positive_int(
        refs.get("publication_id") or refs.get("publication_task_id"), "发布任务提示"
    )
    monitoring_task_id = _positive_int(refs.get("monitoring_task_id"), "监测任务提示")
    geo_post_id = _positive_int(refs.get("geo_post_id"), "图文提示")

    candidate_brand_ids: set[int] = set()
    candidate_quote_ids: set[int] = set()
    updated_values: list[Any] = []
    quote: dict[str, Any] | None = None

    if brand_id is not None:
        require_brand_access(request, brand_id, allow_null=False)
        candidate_brand_ids.add(brand_id)

    if quote_id is not None:
        quote = require_quote_access(request, quote_id, allow_null=False)
        if quote.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="上下文不可用，请重新选择客户")
        candidate_quote_ids.add(quote_id)
        candidate_brand_ids.add(int(quote["brand_id"]))
        updated_values.extend((quote.get("updated_at"), quote.get("created_at")))

    for kind, resource_id in (
        ("article", article_id),
        ("publication", publication_id),
        ("monitoring", monitoring_task_id),
        ("geo_post", geo_post_id),          # [R3-P11 ①]
    ):
        if resource_id is None:
            continue
        row = _resource_hint(kind, resource_id)
        if not row or not row.get("brand_id"):
            raise HTTPException(status_code=404, detail="上下文不可用，请重新选择客户")
        require_brand_access(request, int(row["brand_id"]), allow_null=False)
        candidate_brand_ids.add(int(row["brand_id"]))
        if row.get("quote_id"):
            hinted_quote = require_quote_access(request, int(row["quote_id"]), allow_null=False)
            candidate_quote_ids.add(int(row["quote_id"]))
            candidate_brand_ids.add(int(hinted_quote["brand_id"]))
            if quote is None:
                quote = hinted_quote
        updated_values.append(row.get("updated_at"))

    if len(candidate_brand_ids) > 1 or len(candidate_quote_ids) > 1:
        raise HTTPException(status_code=404, detail="上下文不可用，请重新选择客户")

    resolved_brand_id = next(iter(candidate_brand_ids), None)
    if quote is None and resolved_brand_id is not None:
        quote = _latest_quote_for_brand(resolved_brand_id)
        if quote:
            quote = require_quote_access(request, int(quote["id"]), allow_null=False)
            quote_id = int(quote["id"])
            updated_values.extend((quote.get("updated_at"), quote.get("created_at")))
    elif quote is not None:
        quote_id = int(quote["id"])

    brand_name = None
    if resolved_brand_id is not None:
        brand = _fetch_row(
            "SELECT id, name, updated_at FROM brands WHERE id = %s "
            "AND (is_deleted IS NULL OR is_deleted = FALSE)",
            (resolved_brand_id,),
        )
        if not brand:
            raise HTTPException(status_code=404, detail="上下文不可用，请重新选择客户")
        brand_name = str(brand.get("name") or "").strip() or None
        updated_values.append(brand.get("updated_at"))

    page, page_name = _page_context(current_page)
    return AuthorizedAssistantContext(
        brand_id=resolved_brand_id,
        brand_name=brand_name,
        quote_id=quote_id,
        article_id=article_id,
        publication_id=publication_id,
        monitoring_task_id=monitoring_task_id,
        current_page=page,
        page_name=page_name,
        data_updated_at=_latest_timestamp(updated_values),
        quote=quote,
        geo_post_id=geo_post_id,
    )


def _empty_metrics(*, reason: str = "no_customer_context") -> dict[str, Any]:
    return {
        "keywords": {
            "available": False, "total": None, "confirmed": None, "monitored": None,
        },
        "writing": {
            "available": False, "pending": None, "in_progress": None,
            "completed": None, "failed": None,
        },
        "publication": {
            "available": False, "monitoring_available": False,
            "published": None, "indexed": None, "url_cited": None,
            "brand_mentioned": None, "recommended": None,
        },
        "monitoring": {
            "available": False, "tasks": None, "completed_tasks": None,
            "observations": None, "classified_observations": None,
            "valid_observations": None, "unclassified_observations": None,
            "unresolved_observations": None,
        },
        "updated_at": None,
        "load_errors": [] if reason == "no_customer_context" else [
            {"section": "operations", "message": "运营数据暂时无法读取，请重试。"}
        ],
    }


def _load_operational_metrics(quote_id: int | None) -> dict[str, Any]:
    if quote_id is None:
        return _empty_metrics()
    from db.connection import get_connection

    out = _empty_metrics(reason="pending_read")
    out["load_errors"] = []
    conn = get_connection()
    try:
        cur = conn.cursor()
        updated: list[Any] = []

        def read(section: str, sql: str) -> dict[str, Any] | None:
            try:
                cur.execute(sql, (quote_id,))
                return dict(cur.fetchone() or {})
            except Exception as exc:
                # PostgreSQL 一条 SELECT 失败后事务会进入 aborted；局部降级前必须回滚，
                # 否则后续本来可读的 section 会被连坐成不可用。
                conn.rollback()
                logger.warning("[customer-plan] %s metrics unavailable quote=%s: %s", section, quote_id, exc)
                out["load_errors"].append({
                    "section": section,
                    "message": "该项数据暂时无法读取，请重试。",
                })
                return None

        row = read(
            "keywords",
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status = 'confirmed') AS confirmed, "
            "COUNT(*) FILTER (WHERE is_monitored IS TRUE AND monitoring_status = 'active') AS monitored, "
            "MAX(created_at) AS updated_at FROM confirmed_keywords WHERE quote_id = %s",
        )
        if row is not None:
            out["keywords"] = {
                "available": True,
                **{k: int(row.get(k) or 0) for k in ("total", "confirmed", "monitored")},
            }
            updated.append(row.get("updated_at"))

        row = read(
            "writing",
            "SELECT COUNT(*) FILTER (WHERE status IN ('draft','pending','titles_ready')) AS pending, "
            "COUNT(*) FILTER (WHERE status IN ('processing','writing','generating')) AS in_progress, "
            "COUNT(*) FILTER (WHERE status = 'completed' AND article_id IS NOT NULL) AS completed, "
            "COUNT(*) FILTER (WHERE status = 'failed') AS failed, "
            "MAX(COALESCE(completed_at, writing_started_at, created_at)) AS updated_at "
            "FROM topics WHERE quote_id = %s",
        )
        if row is not None:
            out["writing"] = {
                "available": True,
                **{k: int(row.get(k) or 0) for k in ("pending", "in_progress", "completed", "failed")},
            }
            updated.append(row.get("updated_at"))

        # [WP7 cutover 2026-08-17] 原来只数 `media_publications`(人工登记那一条链),
        # 代发 / 插件两条链的发布物一篇都没算进来。改走 quote-scoped 六阶段投影。
        pub = read(
            "publication",
            "SELECT MAX(COALESCE(publish_timestamp, created_at)) AS updated_at "
            "FROM media_publications WHERE quote_id = %s",
        )
        try:
            from services.publication_stage_adapters import quote_stage_tuple

            _proj = quote_stage_tuple(int(quote_id), cursor=cur)
            _stage = (_proj.get("stages") or {}).get("published_active") or {}
            out["publication"]["available"] = bool(_stage.get("available"))
            out["publication"]["published"] = _stage.get("count")
            out["publication"]["stages"] = _proj.get("stages")
            out["publication"]["source_versions"] = _proj.get("source_versions")
        except Exception as _exc:
            # 🔴 必须 rollback,理由同上面 `read()` 里那段注释:PostgreSQL 一条
            #    SELECT 失败后整个事务进入 aborted,不回滚会把**后面本来读得到的**
            #    monitoring / gap 等 section 一起连坐成"不可用"。
            #    一处失败连累一片,而且看不出真因 —— 那是最难查的那种降级。
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning("[customer-plan] 六阶段投影不可用 quote=%s: %s", quote_id, _exc)
            out["publication"]["available"] = False
        if pub is not None:
            updated.append(pub.get("updated_at"))

        mon = read(
            "monitoring",
            "SELECT COUNT(DISTINCT t.id) AS tasks, "
            "COUNT(DISTINCT t.id) FILTER (WHERE t.status = 'completed') AS completed_tasks, "
            "COUNT(r.id) AS observations, "
            "COUNT(r.id) FILTER (WHERE r.target_outcome IN "
            "('recommended','conditionally_recommended','candidate_only','mentioned_only',"
            "'criteria_only','refused_no_evidence','refused_risk','not_mentioned',"
            "'entity_ambiguous','engine_error')) AS classified_observations, "
            "COUNT(r.id) FILTER (WHERE r.target_outcome IN "
            "('recommended','conditionally_recommended','candidate_only','mentioned_only',"
            "'criteria_only','refused_no_evidence','refused_risk','not_mentioned')) AS valid_observations, "
            "COUNT(r.id) FILTER (WHERE r.target_outcome IS NULL OR r.target_outcome NOT IN "
            "('recommended','conditionally_recommended','candidate_only','mentioned_only',"
            "'criteria_only','refused_no_evidence','refused_risk','not_mentioned',"
            "'entity_ambiguous','engine_error')) AS unclassified_observations, "
            "COUNT(r.id) FILTER (WHERE r.target_outcome IN ('entity_ambiguous','engine_error') "
            "OR r.target_outcome IS NULL OR r.target_outcome NOT IN "
            "('recommended','conditionally_recommended','candidate_only','mentioned_only',"
            "'criteria_only','refused_no_evidence','refused_risk','not_mentioned',"
            "'entity_ambiguous','engine_error')) AS unresolved_observations, "
            "COUNT(r.id) FILTER (WHERE r.target_outcome IN "
            "('mentioned_only','candidate_only','conditionally_recommended','recommended')) AS brand_mentioned, "
            "COUNT(r.id) FILTER (WHERE r.target_outcome IN "
            "('conditionally_recommended','recommended')) AS recommended, "
            "MAX(COALESCE(r.tested_at, t.completed_at, t.created_at)) AS updated_at "
            "FROM monitoring_tasks t LEFT JOIN monitoring_results r ON r.task_id = t.id "
            "WHERE t.quote_id = %s",
        )
        if mon is not None:
            valid = int(mon.get("valid_observations") or 0)
            out["monitoring"] = {
                "available": True,
                **{
                    key: int(mon.get(key) or 0)
                    for key in (
                        "tasks", "completed_tasks", "observations", "classified_observations",
                        "valid_observations", "unclassified_observations", "unresolved_observations",
                    )
                },
            }
            out["publication"]["monitoring_available"] = True
            # 有效判定分母为 0 时，“被提及 0 / 被推荐 0”不是事实，只能是未知。
            out["publication"]["brand_mentioned"] = (
                int(mon.get("brand_mentioned") or 0) if valid > 0 else None
            )
            out["publication"]["recommended"] = (
                int(mon.get("recommended") or 0) if valid > 0 else None
            )
            updated.append(mon.get("updated_at"))
        out["updated_at"] = _latest_timestamp(updated)
        return out
    finally:
        conn.close()


def _load_existing_gap_plan(quote: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """纯读取现行 P4 已物化的当前快照，不在助手请求内产生业务写入。

    源事实或规则变化但新代际尚未物化时返回 ``None``；助手继续给基础运营建议，
    不拿旧快照冒充当前状态。快照物化仍由 P4 自己的受权流程负责。
    """
    if not quote:
        return None
    try:
        from db import gap_plan_db
        from services import gap_operation_plan as gap_plan

        quote_id = int(quote["id"])
        bundle = gap_plan.read_current_snapshot(dict(quote))
        if bundle is None:
            return None
        snapshot = bundle["snapshot"]
        items = bundle["items"]
        publications = gap_plan_db.get_publications(quote_id)
        capacity = gap_plan.compute_capacity(dict(quote), publications)
        presented = gap_plan.present_snapshot(
            {"snapshot": snapshot, "items": items}, capacity=capacity
        )
        evidence = {
            "published": sum(bool(v.get("evidence_published")) for v in publications.values()),
            "indexed": sum(bool(v.get("evidence_indexed")) for v in publications.values()),
            "url_cited": sum(bool(v.get("evidence_cited")) for v in publications.values()),
            "recommended": sum(bool(v.get("evidence_recommended")) for v in publications.values()),
        }
        return {**presented, "evidence_totals": evidence}
    except Exception as exc:  # optional P4 migration/read must not stop core workflows
        logger.warning("[customer-plan] P4 read unavailable quote=%s: %s", quote.get("id"), exc)
        return None


def _load_capacity_contract(quote: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """即使尚无 P4 快照，也从篇数容量 SSOT 读取当前可展示合同。"""
    if not quote:
        return None
    try:
        from services import gap_operation_plan as gap_plan

        return gap_plan.compute_capacity(dict(quote), {}).as_dict()
    except Exception as exc:
        logger.warning("[customer-plan] capacity read unavailable quote=%s: %s", quote.get("id"), exc)
        return None


def _publication_outcome_summary(brand_id: int | None,
                                 quote_id: int | None = None) -> dict[str, Any]:
    """复用飞轮第一关 SSOT；不在助手里另算被引率或分母。

    [WP7 2026-08-17] 加 `quote_id`。旧签名只吃 brand_id,于是同品牌 Q1 的被引
    会显示在 Q2 的运营计划里 —— 规格 03 §10 点名要修的 `brand max merge` 形态。
    传了 quote_id 就按 quote 严格隔离;没传时**明确降级**并标注,不假装是本合同的数。
    """
    if brand_id is None:
        return {
            "available": False, "load_failed": False,
            "articles_cited": None, "articles_observable": None,
        }
    if quote_id is not None:
        try:
            from services.publication_stage_adapters import quote_stage_tuple

            proj = quote_stage_tuple(int(quote_id))
            stages = proj.get("stages") or {}
            strict = stages.get("strictly_attributed") or {}
            published = stages.get("published_active") or {}
            return {
                "available": bool(strict.get("available")),
                "load_failed": False,
                "articles_cited": strict.get("count"),
                "articles_observable": published.get("count"),
                "scope": "quote",
                "quote_id": int(quote_id),
                "metric_version": (proj.get("source_versions") or {}).get("attribution_metric"),
                "denominator_note": "分母 = 本报价 cutoff 时点真发布有效篇数",
                "source_versions": proj.get("source_versions"),
            }
        except Exception as exc:
            logger.warning("[customer-plan] quote 级被引不可用 quote=%s: %s", quote_id, exc)
            return {
                "available": False, "load_failed": True, "scope": "quote",
                "articles_cited": None, "articles_observable": None,
            }
    try:
        from services.publication_outcome_facts import article_level_gate1

        raw = article_level_gate1(brand_id=brand_id)
        return {
            "available": bool(raw.get("available")),
            "load_failed": False,
            "articles_cited": int(raw.get("articles_cited") or 0),
            "articles_observable": int(raw.get("articles_observable") or 0),
            # 🔴 未传 quote_id 时口径是**品牌级**,必须自曝,否则下游会把它
            #    当成"本合同的成果"渲染 —— 那就是 WP7 在修的那个 bug。
            "scope": "brand",
            "metric_version": raw.get("metric_version"),
            "denominator_note": raw.get("denominator_note"),
        }
    except Exception as exc:
        logger.warning("[customer-plan] publication outcome unavailable brand=%s: %s", brand_id, exc)
        return {
            "available": False, "load_failed": True,
            "articles_cited": None, "articles_observable": None,
        }


def _knowledge_summary(brand_id: int | None) -> dict[str, Any]:
    if brand_id is None:
        return {"available": False, "load_failed": False, "filled": 0, "total": 0, "missing": []}
    try:
        from services.client_knowledge import build_client_knowledge

        raw = build_client_knowledge(brand_id, with_thumbs=False)
        materials = raw.get("materials") or {}
        return {
            "available": bool(raw.get("has_brand")),
            "load_failed": bool(raw.get("load_failed")),
            "filled": int(materials.get("filled") or 0),
            "total": int(materials.get("total") or 0),
            "missing": [
                str(item.get("label") or "") for item in (materials.get("items") or [])
                if not item.get("present") and item.get("label")
            ][:8],
        }
    except Exception as exc:
        logger.warning("[customer-plan] knowledge read unavailable brand=%s: %s", brand_id, exc)
        return {"available": True, "load_failed": True, "filled": 0, "total": 0, "missing": []}


def _recommendations(plan: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not plan.get("context", {}).get("has_customer"):
        return (
            {"operation_id": "client_list", "reason": "先选择客户，才能读取真实运营状态。"},
            {"operation_id": "help_center", "reason": "也可以先看这套系统的主流程说明。"},
        )
    knowledge = plan["knowledge"]
    metrics = plan["metrics"]
    delivery = plan.get("delivery_plan") or {}
    if knowledge.get("load_failed"):
        return (
            {"operation_id": "client_list", "reason": "客户资料暂时没取到，先重新进入客户档案。"},
            {"operation_id": "help_center", "reason": "若重试仍失败，从帮助中心提交问题。"},
        )
    if knowledge.get("total") and knowledge.get("filled", 0) < knowledge.get("total", 0):
        missing = knowledge.get("missing") or []
        return (
            {"operation_id": "client_list", "reason": f"知识库还缺 {len(missing)} 项关键信息，先补资料。"},
            {"operation_id": "diagnosis_new", "reason": "资料不完整不阻断体检，也可以先跑一次诊断。"},
        )
    writing = metrics["writing"]
    writing_available = bool(writing.get("available", True))
    if writing_available and writing.get("in_progress"):
        return (
            {"operation_id": "writing_center", "reason": f"有 {writing['in_progress']} 篇正在写，先看进度。"},
            {"operation_id": "publish_center", "reason": "已完成内容可继续进入发布投放。"},
        )
    ready = int((delivery.get("summary") or {}).get("status_counts", {}).get("ready_to_execute") or 0)
    if ready or (writing_available and writing.get("pending")):
        amount = ready or int(writing.get("pending") or 0)
        return (
            {"operation_id": "writing_center", "reason": f"当前有 {amount} 个可继续的内容任务。"},
            {"operation_id": "gap_plan_block", "reason": "先核对交付计划与证据也可以。"},
        )
    publication = metrics["publication"]
    publication_available = bool(publication.get("available", True))
    if (
        writing_available and publication_available
        and writing.get("completed") is not None and publication.get("published") is not None
        and int(writing["completed"]) > int(publication["published"])
    ):
        return (
            {"operation_id": "publish_center", "reason": "已有完成文章尚未全部进入发布记录。"},
            {"operation_id": "writing_center", "reason": "也可以先检查成稿和人工确认状态。"},
        )
    monitoring = metrics["monitoring"]
    if not bool(monitoring.get("available", True)):
        return (
            {"operation_id": "monitoring_center", "reason": "监测数据暂时无法读取，进入监测页重试。"},
            {"operation_id": "client_list", "reason": "也可以重新进入当前客户后再试。"},
        )
    if monitoring.get("tasks") == 0:
        return (
            {"operation_id": "monitoring_center", "reason": "还没有监测任务，先建立效果基线。"},
            {"operation_id": "publish_center", "reason": "监测前可先确认发布链接是否齐全。"},
        )
    return (
        {"operation_id": "monitoring_center", "reason": "已有监测数据，今天先看未覆盖的问题。"},
        {"operation_id": "writing_center", "reason": "对监测短板补内容时回到创作中心。"},
    )


def build_customer_operation_plan(context: AuthorizedAssistantContext) -> dict[str, Any]:
    metrics = _load_operational_metrics(context.quote_id)
    gap_plan = _load_existing_gap_plan(context.quote)
    capacity = (gap_plan or {}).get("capacity") or _load_capacity_contract(context.quote)
    publication_outcome = _publication_outcome_summary(context.brand_id, context.quote_id)
    if gap_plan:
        totals = gap_plan.get("evidence_totals") or {}
        # [WP7 cutover 2026-08-17] 原来这里对 canonical 计数与 gap plan 的
        # evidence 计数取 `max`。规格 03 §10 直接点名要拆:
        #   「`gap_plan_publications` 只保留 plan/checkback metadata 与 canonical
        #     source 引用;其 URL/current published/evidence bool **不再是独立真值**。
        #     客户 operation plan 不再把 brand outcome 与 quote evidence 取 max。」
        # 🔴 max 的危害不是"数偏大",是**把两个不同口径的数混成一个**:
        #    canonical 会因撤稿回落,gap 的 evidence bool 只增不减 → 取 max 之后
        #    撤稿在界面上永远不生效。现在 gap 侧只作为**并列的 checkback 线索**展示。
        metrics["publication"]["gap_plan_checkback"] = {
            key: int(totals.get(key) or 0) for key in ("published", "indexed", "url_cited")
        }
        metrics["publication"]["gap_plan_checkback"]["note"] = (
            "计划回访记录,非发布事实源;发布事实以本报价的六阶段投影为准")
        # P4 的正向证据可以证明“至少有一次推荐”，但 0 条证据不能把尚未完成
        # 10 类判定的监测结果改写成“被推荐 0”。
        recommended_total = int(totals.get("recommended") or 0)
        recommended_current = metrics["publication"].get("recommended")
        if recommended_total > 0 or recommended_current is not None:
            metrics["publication"]["recommended"] = max(
                int(recommended_current or 0), recommended_total
            )
        if totals:
            metrics["publication"]["available"] = True
    if publication_outcome.get("available") and publication_outcome.get("articles_cited") is not None:
        metrics["publication"]["url_cited"] = max(
            int(metrics["publication"].get("url_cited") or 0),
            int(publication_outcome["articles_cited"]),
        )
    knowledge = _knowledge_summary(context.brand_id)
    public_context = context.public_context()
    public_context["data_updated_at"] = _latest_timestamp([
        context.data_updated_at,
        metrics.get("updated_at"),
        (gap_plan or {}).get("generated_at"),
    ])
    keywords = metrics["keywords"]
    writing = metrics["writing"]
    publication = metrics["publication"]
    monitoring = metrics["monitoring"]

    keyword_evidence = (
        f"{keywords['confirmed']}/{keywords['total']} 个已确认"
        if keywords.get("available", True) and keywords.get("total") is not None
        else "暂时无法读取 · 请重试"
    )
    knowledge_evidence = (
        "暂时无法读取 · 请重试"
        if knowledge.get("load_failed")
        else (
            f"已填 {knowledge.get('filled', 0)}/{knowledge.get('total', 0)} 项 · "
            f"待补 {len(knowledge.get('missing') or [])} 项"
        )
    )
    writing_evidence = (
        f"待写 {writing['pending']} · 写作中 {writing['in_progress']} · 已完成 {writing['completed']}"
        if writing.get("available", True) and writing.get("pending") is not None
        else "暂时无法读取 · 请重试"
    )
    publication_parts: list[str] = []
    if publication.get("available", True) and publication.get("published") is not None:
        publication_parts.extend([
            f"已发布 {publication['published']}",
            f"收录 {int(publication.get('indexed') or 0)}",
            f"URL 被引 {int(publication.get('url_cited') or 0)}",
        ])
    else:
        publication_parts.append("发布记录暂时无法读取")
    if monitoring.get("available", True) and monitoring.get("observations") is not None:
        valid = int(monitoring.get("valid_observations") or 0)
        unresolved = int(monitoring.get("unresolved_observations") or 0)
        if valid > 0:
            publication_parts.append(
                f"已判定 {valid} 条中被提及 {int(publication.get('brand_mentioned') or 0)}、"
                f"被推荐 {int(publication.get('recommended') or 0)}"
            )
        else:
            publication_parts.append("暂无有效判定，不能解读为未被提及或推荐")
        if unresolved > 0:
            publication_parts.append(f"另有 {unresolved} 条待判定或不可用，不能按整体 0 解读")
    else:
        publication_parts.append("监测结果暂时无法读取 · 请重试")

    plan: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "data_state": "partial" if metrics.get("load_errors") else "available",
        "context": public_context,
        "delivery_plan": gap_plan,
        "capacity": capacity,
        "publication_outcome": publication_outcome,
        "knowledge": knowledge,
        "metrics": metrics,
        "primary_action": None,
        "backup_action": None,
        "evidence": [
            {"label": "词包", "value": keyword_evidence},
            {"label": "客户资料", "value": knowledge_evidence},
            {"label": "写作", "value": writing_evidence},
            {"label": "发布与效果", "value": " · ".join(publication_parts)},
        ],
        "warnings": [],
    }
    for load_error in metrics.get("load_errors") or []:
        plan["warnings"].append({
            "message": str(load_error.get("message") or "运营数据暂时无法读取，请重试。"),
            "next_action_operation_id": "client_list",
        })
    if knowledge.get("load_failed"):
        plan["warnings"].append({
            "message": "客户资料暂时没取到。",
            "next_action_operation_id": "client_list",
        })
    if publication_outcome.get("load_failed"):
        plan["warnings"].append({
            "message": "发布效果暂时无法读取，请稍后重试。",
            "next_action_operation_id": "monitoring_center",
        })
    primary, backup = _recommendations(plan)
    plan["primary_action"] = primary
    plan["backup_action"] = backup
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str)
    plan["plan_id"] = "cop-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return plan


def load_customer_operation_plan(
    request: Request,
    context_refs: Mapping[str, Any] | None,
    current_page: str,
) -> tuple[AuthorizedAssistantContext, dict[str, Any]]:
    context = resolve_authorized_context(request, context_refs, current_page)
    return context, build_customer_operation_plan(context)
