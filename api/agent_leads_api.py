"""
agent_leads_api — PR-B 操作者通知闭环
CTO-15.23 2026-05-03

定位:
  客户成交链路 · 客户在 v2 决策页留资 → notify_user 通知代理 → 代理进 /agent/leads 看
  · 列表(自己分享的所有 leads · admin 全部)
  · 详情 + 客户行为时间线(从 m3_customer_events 聚合)
  · 5 操作按钮(拨号/复制微信/复制信息/跳客户工作台/标完成)

复用基础设施:
  · report_leads 表(PR-A.1 已写入 · status 字段已有)
  · m3_customer_events 表 + query_events helper(已有)
  · notify_user(已通)

新增表 0 个 · 仅 4 endpoints
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, Field

from db.connection import get_connection

logger = logging.getLogger("GEO-AgentLeads")

router = APIRouter(tags=["代理客户线索"])


# ==================== 幂等 schema 演化 ====================

def init_agent_leads_schema():
    """PR-B backend.1 (Codex 建议) · 加 status_updated_at 字段 · 幂等

    场景: PATCH status 后 timeline 渲染 status_changed 项需要时间戳
          老 report_leads 表只有 created_at · 加这一列承载状态变更时间
    向后兼容: 老行 status_updated_at 默认 NULL · timeline 用 created_at fallback
    """
    conn = get_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute("""
                ALTER TABLE report_leads
                ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMP DEFAULT NULL
            """)
        except Exception as e:
            logger.warning(f"[agent-leads] add status_updated_at failed (possibly concurrent add): {e}")
        # 同时给 status 加 CHECK constraint 是 future-proof · 但 PostgreSQL 不支持 IF NOT EXISTS for constraints
        # 改为 application-level VALID_STATUSES 校验(本文件已实现)
    except Exception as e:
        logger.warning(f"[agent-leads] init_agent_leads_schema failed (non-blocking): {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ==================== Pydantic models ====================

class LeadStatusUpdate(BaseModel):
    """PATCH /api/agent/leads/:id/status payload"""
    status: str = Field(..., description="new / contacted / closed / lost")
    note: Optional[str] = Field(None, description="备注(可选)")


VALID_STATUSES = {"new", "contacted", "closed", "lost"}


# ==================== 时间工具(P1-2 fix) ====================

def _parse_iso(s) -> Optional[datetime]:
    """把 query_events 序列化的 ISO 字符串(可能含 +00:00 或 Z)转回 datetime · None 兜底"""
    if not s:
        return None
    if hasattr(s, "timestamp"):  # 已是 datetime
        return s
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _format_window_text(secs: Optional[int]) -> Optional[str]:
    """把 decision_window_seconds 转友好文本 · 给操作者一眼判断意向高低"""
    if secs is None:
        return None
    if secs < 0:
        return None
    if secs < 60:
        return f"{secs} 秒后留资 · 强意向"
    if secs < 600:
        return f"{secs // 60} 分钟后留资 · 强意向"
    if secs < 3600:
        return f"{secs // 60} 分钟后留资"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h} 小时{m} 分钟后留资" if m else f"{h} 小时后留资"
    d = secs // 86400
    h = (secs % 86400) // 3600
    return f"{d} 天{h} 小时后留资" if h else f"{d} 天后留资"


# ==================== Helpers ====================

def _get_user(request: Request) -> dict:
    """从 request.state 拿当前登录用户 · 401 if 未登录"""
    user = getattr(request.state, "user", None)
    if not user or not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _is_admin(user: dict) -> bool:
    return bool(user.get("is_admin"))


def _get_user_id(user: dict) -> Optional[int]:
    """user_id 可能是 int(JWT 用户)或 str(portal user)· 这里只接受 int"""
    uid = user.get("user_id")
    if isinstance(uid, int):
        return uid
    if isinstance(uid, str) and uid.isdigit():
        return int(uid)
    return None


# ==================== Endpoints ====================

@router.get("/api/agent/leads")
def list_agent_leads(
    request: Request,
    status: Optional[str] = Query(default=None, description="筛 new/contacted/closed/lost · 默认全部"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """代理自己分享得到的客户线索列表(admin 看全部)

    PR-B backend.1 (Codex P1-1/P1-3 反馈):
      · stats 走独立 COUNT(*) GROUP BY status 全量聚合 · 不依赖 limit
      · 排除 status='action'/老 action endpoint 历史行 · 仅返真实线索状态
      · 返回 page_count(本页)+ total(全量)+ by_status(全量分布)分离
    """
    user = _get_user(request)
    user_id = _get_user_id(user)
    if user_id is None and not _is_admin(user):
        raise HTTPException(status_code=403, detail="未识别用户身份")

    # ---- RBAC + 真实线索状态过滤(共用 base where) ----
    base_conds = ["rl.status IN ('new', 'contacted', 'closed', 'lost')"]  # P1-3 排除 action
    base_params: list = []
    if not _is_admin(user):
        base_conds.append("rl.shared_by_user_id = %s")
        base_params.append(user_id)
    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"非法 status · 必须为 {VALID_STATUSES}")
        # status filter 只影响 list · stats 仍按全状态聚合(让前端 tabs 知道每档有多少)
    list_conds = list(base_conds)
    list_params = list(base_params)
    if status:
        list_conds.append("rl.status = %s")
        list_params.append(status)
    list_where = " WHERE " + " AND ".join(list_conds)
    base_where = " WHERE " + " AND ".join(base_conds)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        # ---- 1. 列表查询(带 limit) ----
        cursor.execute(
            f"""
            SELECT rl.id, rl.diagnosis_id, rl.phone, rl.company_name,
                   rl.shared_by_user_id, rl.status, rl.created_at, rl.status_updated_at,
                   d.brand_name, d.industry, d.brand_id
            FROM report_leads rl
            LEFT JOIN diagnosis_records d ON rl.diagnosis_id = d.id
            {list_where}
            ORDER BY rl.created_at DESC
            LIMIT %s
            """,
            list_params + [limit],
        )
        rows = cursor.fetchall() or []

        # ---- 2. P1-1: 全量 stats 独立 COUNT(*) GROUP BY status (不带 limit · 不带 status filter) ----
        cursor.execute(
            f"""
            SELECT rl.status, COUNT(*) AS cnt
            FROM report_leads rl
            {base_where}
            GROUP BY rl.status
            """,
            base_params,
        )
        status_rows = cursor.fetchall() or []
    finally:
        conn.close()

    leads = []
    for r in rows:
        rd = dict(r)
        leads.append({
            "id": rd["id"],
            "diagnosis_id": rd["diagnosis_id"],
            "phone": rd["phone"],
            "company_name": rd.get("company_name") or "",
            "status": rd.get("status") or "new",
            "created_at": rd["created_at"].isoformat() if rd.get("created_at") else None,
            "status_updated_at": (
                rd["status_updated_at"].isoformat()
                if rd.get("status_updated_at") and hasattr(rd["status_updated_at"], "isoformat")
                else None
            ),
            "brand_name": rd.get("brand_name") or "",
            "industry": rd.get("industry") or "",
            "brand_id": rd.get("brand_id"),
        })

    # P1-1: 全量 by_status(不依赖 limit)
    by_status = {s: 0 for s in VALID_STATUSES}
    for sr in status_rows:
        srd = dict(sr)
        s_val = srd.get("status")
        if s_val in by_status:
            by_status[s_val] = int(srd.get("cnt") or 0)
    total_all = sum(by_status.values())

    return {
        "success": True,
        "leads": leads,
        "page_count": len(leads),  # 本页返回数(<= limit)
        "stats": {
            "total": total_all,            # 全量真实线索总数(P1-1 修)
            "by_status": by_status,        # 全量分状态分布(P1-1 修)
        },
    }


def _fetch_lead_with_rbac(lead_id: int, user: dict) -> dict:
    """读 lead · admin 直通 · 否则必须 shared_by_user_id 匹配 + status 必须真实线索"""
    user_id = _get_user_id(user)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT rl.id, rl.diagnosis_id, rl.phone, rl.company_name,
                   rl.shared_by_user_id, rl.status, rl.created_at, rl.status_updated_at,
                   d.brand_name, d.industry, d.brand_id, d.total_score, d.level
            FROM report_leads rl
            LEFT JOIN diagnosis_records d ON rl.diagnosis_id = d.id
                AND (d.result_visibility IS NULL OR d.result_visibility = 'published')  -- [返工2 P1-2] withheld/pending 诊断不带出分数/等级
            WHERE rl.id = %s
              AND rl.status IN ('new', 'contacted', 'closed', 'lost')
            """,  # P1-3: 排除老 action 行 · GET 详情/timeline 也不暴露
            (lead_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="线索不存在")
    rd = dict(row)
    if not _is_admin(user) and rd.get("shared_by_user_id") != user_id:
        # 隐私 · 不暴露 lead 是否存在
        raise HTTPException(status_code=404, detail="线索不存在")
    return rd


@router.get("/api/agent/leads/{lead_id}")
def get_agent_lead_detail(lead_id: int, request: Request):
    """代理看单条 lead 详情 · 含品牌信息 + 评分等级"""
    user = _get_user(request)
    rd = _fetch_lead_with_rbac(lead_id, user)
    return {
        "success": True,
        "lead": {
            "id": rd["id"],
            "diagnosis_id": rd["diagnosis_id"],
            "phone": rd["phone"],
            "company_name": rd.get("company_name") or "",
            "status": rd.get("status") or "new",
            "created_at": rd["created_at"].isoformat() if rd.get("created_at") else None,
            "status_updated_at": (
                rd["status_updated_at"].isoformat()
                if rd.get("status_updated_at") and hasattr(rd["status_updated_at"], "isoformat")
                else None
            ),
            "brand_name": rd.get("brand_name") or "",
            "industry": rd.get("industry") or "",
            "brand_id": rd.get("brand_id"),
            "total_score": rd.get("total_score"),
            "level": rd.get("level"),
        },
    }


@router.get("/api/agent/leads/{lead_id}/timeline")
def get_agent_lead_timeline(
    lead_id: int,
    request: Request,
    days: int = Query(default=14, ge=1, le=90),
):
    """客户行为时间线 · 从 m3_customer_events 拉 · diagnosis_id 范围

    返回结构 (前端 timeline 组件直接消费):
      {
        timeline: [
          { time: ISO, kind: 'sent_link'|'opened'|'dwell'|'cta_click'|'lead_submitted'|'status_changed', label: str, meta: {...} },
          ...
        ],
        decision_window_seconds: int | null  // lead 提交时间 - 第一次 opened 时间
      }
    """
    user = _get_user(request)
    rd = _fetch_lead_with_rbac(lead_id, user)
    diag_id = rd.get("diagnosis_id")

    # 拉相关事件(public_report source · diagnosis_id 维度)
    events = []
    try:
        from db.m3_events_db import query_events
        events = query_events(
            diagnosis_id=diag_id,
            sources=["public_report"],
            days=days,
            limit=200,
        )
    except Exception as _q_err:
        logger.warning(f"[agent-leads] timeline query_events 失败 lead={lead_id}: {_q_err}")
        events = []

    # PR-B backend.2 (Codex P1 fix · multi-lead 场景)
    # query_events 默认 occurred_at DESC · 必须先升序归一化 · 否则:
    #   1) first_opened 误取"最近一次打开" · 不是"首次打开" · decision window 不准
    #   2) 同 diagnosis 多 lead 时 lead_submitted 会拿错(必须 metadata.lead_id == 当前 lead_id)
    events_asc = sorted(events, key=lambda x: x.get("occurred_at") or "")

    first_opened_iso: Optional[str] = None
    first_opened_dt: Optional[datetime] = None
    own_lead_submit_dt: Optional[datetime] = None  # 仅本 lead 的 lead_submitted

    for e in events_asc:
        et = e.get("event_type")
        occurred = e.get("occurred_at")  # ISO 字符串(_serialize_row 已转)
        if et == "opened" and first_opened_iso is None:
            # 升序遍历 · 第一个 opened = 真"首次打开"
            first_opened_iso = occurred
            first_opened_dt = _parse_iso(occurred)
        if et == "lead_submitted":
            meta = e.get("metadata") or {}
            ev_lead_id = meta.get("lead_id")
            try:
                ev_lead_id_int = int(ev_lead_id) if ev_lead_id is not None else None
            except (TypeError, ValueError):
                ev_lead_id_int = None
            if ev_lead_id_int == lead_id:
                # 只匹配当前 lead 的提交事件 · 防同 diagnosis 多 lead 串
                ls_dt = _parse_iso(occurred)
                if ls_dt:
                    own_lead_submit_dt = ls_dt

    # 构造时间线
    timeline = []
    if first_opened_iso:
        timeline.append({
            "time": first_opened_iso,
            "kind": "opened",
            "label": "客户首次打开报告页",
            "meta": {"event_type": "opened"},
        })

    # 按时间正序加入其它事件(opened 第一次已加 · 其余 opened 跳过)
    seen_first_opened = first_opened_iso is not None
    for e in sorted(events, key=lambda x: x.get("occurred_at") or ""):
        et = e.get("event_type")
        time_iso = e.get("occurred_at")  # 已是 ISO 字符串
        if et == "opened":
            if seen_first_opened:
                continue
            seen_first_opened = True
        # PR-B backend.3 (Codex P2 fix · multi-lead 时间线污染)
        # decision_window 已用 metadata.lead_id 过滤(backend.2)· 但 timeline append
        # 循环还会把同 diagnosis 下其他 lead 的 lead_submitted 加进当前 lead 的时间线
        # · 操作者 Lead A 详情页会看到 Lead B 的"提交联系方式·留资"事件
        # opened/dwell/cta 仍按 diagnosis 聚合(那是匿名访问行为 · 不属任何特定 lead)
        if et == "lead_submitted":
            ev_meta = e.get("metadata") or {}
            ev_lead_id = ev_meta.get("lead_id")
            try:
                ev_lead_id_int = int(ev_lead_id) if ev_lead_id is not None else None
            except (TypeError, ValueError):
                ev_lead_id_int = None
            if ev_lead_id_int != lead_id:
                continue  # 不是当前 lead 的提交 · 跳过 · 不污染时间线
        label_map = {
            "opened": "客户打开报告页",
            "dwell_30s": "停留 30 秒以上",
            "dwell_120s": "停留 2 分钟以上 · 深度阅读",
            "cta_click": "客户点击 CTA 按钮",
            "saw_price": "看到报价信息",
            "lead_submitted": "提交联系方式 · 留资",
            "submitted_keywords": "提交关键词选择",
            "renewed_interest": "续费意向",
        }
        label = label_map.get(et, f"事件 · {et}")
        timeline.append({
            "time": time_iso,
            "kind": et,
            "label": label,
            "meta": e.get("metadata") or {},
        })

    # 最后一步: status 变更(若 != new) · 用 status_updated_at 真时间戳
    if rd.get("status") and rd["status"] != "new":
        status_label_map = {
            "contacted": "已联系客户",
            "closed": "成交 · 已关闭",
            "lost": "线索流失",
        }
        sua = rd.get("status_updated_at")
        sua_iso = sua.isoformat() if sua and hasattr(sua, "isoformat") else None
        timeline.append({
            "time": sua_iso,  # P1-Codex 建议 · 真时间戳(无则 None)
            "kind": "status_changed",
            "label": status_label_map.get(rd["status"], rd["status"]),
            "meta": {"current_status": rd["status"]},
        })

    # PR-B backend.2 (Codex P1 fix · multi-lead 场景)
    # 用 own_lead_submit_dt(已 metadata.lead_id 过滤) · 不再用 last_lead_submit_dt 跨 lead 串
    # 负数 window(opened > submit · 不可能/数据错乱)→ 返 null/null 防 UI 显示"-300 秒后留资"
    decision_window_seconds: Optional[int] = None
    decision_window_text: Optional[str] = None
    if first_opened_dt and own_lead_submit_dt:
        try:
            secs = int((own_lead_submit_dt - first_opened_dt).total_seconds())
            if secs < 0:
                # 数据时序异常 · 不构造结论 · 透明降级
                decision_window_seconds = None
                decision_window_text = None
            else:
                decision_window_seconds = secs
                decision_window_text = _format_window_text(secs)
        except Exception:
            decision_window_seconds = None
            decision_window_text = None

    return {
        "success": True,
        "timeline": timeline,
        "decision_window_seconds": decision_window_seconds,
        "decision_window_text": decision_window_text,  # P1-Codex 建议 · 友好文本给前端直接展示
        "raw_event_count": len(events),
    }


@router.patch("/api/agent/leads/{lead_id}/status")
def update_agent_lead_status(lead_id: int, payload: LeadStatusUpdate, request: Request):
    """代理标 lead 状态 · new/contacted/closed/lost

    PR-B backend.1 (Codex 建议) · 同步写 status_updated_at = NOW()
    timeline 渲染 status_changed 项时用此时间戳 · 不再 time=null
    """
    user = _get_user(request)
    user_id = _get_user_id(user)
    rd = _fetch_lead_with_rbac(lead_id, user)
    new_status = (payload.status or "").strip().lower()
    if new_status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"非法 status · 必须为 {VALID_STATUSES}")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE report_leads SET status = %s, status_updated_at = NOW() WHERE id = %s "
            "RETURNING status_updated_at",
            (new_status, lead_id),
        )
        ret = cursor.fetchone()
        new_status_updated_at = None
        if ret:
            ret_d = dict(ret)
            sua = ret_d.get("status_updated_at")
            new_status_updated_at = sua.isoformat() if sua and hasattr(sua, "isoformat") else None
        conn.commit()
    finally:
        conn.close()

    logger.info(f"[agent-leads] lead {lead_id} status {rd.get('status')} → {new_status} by user {user_id}")
    return {
        "success": True,
        "lead_id": lead_id,
        "old_status": rd.get("status") or "new",
        "new_status": new_status,
        "status_updated_at": new_status_updated_at,  # Codex 建议 · 给前端可展示
    }
