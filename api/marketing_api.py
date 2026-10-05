"""
营销中心(营销军师)管理端 API · 2026-07-04

全部 admin-only(request.state.user.is_admin)。前端路由固定 requiredModule="users",
后端 _require_admin 为最终闸门(照抄 api/ai_ops_api.py 房规)。

端点前缀 /api/admin/marketing/*:
  GET  /overview                          · 今日军情首页(三事实数 · 无预测性收入)
  POST /patrol/run                        · 立即巡逻一轮(force · 不依赖开关)
  GET  /cases  [?status]                  · 建议案件队列(今日机会分组)
  GET  /cases/{id}                        · 案件详情 + 五绿勾实时校验预览
  POST /cases/{id}/approve|reject|request-changes
  GET  /events                            · 近期营销动态时间线
  GET  /campaigns                         · 活动与台账
  GET  /levers                            · 杠杆面板(充值阶梯/SKU货架/首充状态 · 改价格跳原页)
  GET  /signal-rules                      · 10 条信号规则 + 数据接线状态(诚实披露)
  GET  /policies · PATCH /policies/{key} · POST /kill-switch
  GET  /reports/weekly · POST /reports/weekly/generate   (Package F)
  GET  /effect                            (Package F 效果复盘)

红线:本模块只读展示 + 写审批/审计;真实执行(触达/发放)由执行器 + 双闸控制,
      绝不在此处直接改余额/价格/结算。
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from db import marketing_db
from services.marketing import approval, signal_rules
from services.marketing.signal_rules import RULE_GROUP, RULE_DATA_STATUS
from auth.user_ctx import current_user_id

logger = logging.getLogger("Marketing-API")
router = APIRouter(prefix="/api/admin/marketing", tags=["营销军师"])


# ==========================================
# 权限工具(复制自 ai_ops_api · 防跨模块耦合)
# ==========================================
def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _user_id(user: dict) -> int:
    return int(user.get("id") or user.get("user_id") or 0)


# ==========================================
# Pydantic 模型
# ==========================================
class PolicyPatchRequest(BaseModel):
    value: dict = Field(..., description='如 {"enabled": true} 或 {"value": 500}')


class ApprovalActionRequest(BaseModel):
    note: str = Field(default="", max_length=1000)


class KillSwitchRequest(BaseModel):
    enabled: bool


# ==========================================
# 今日军情首页 · overview(三事实数替换"预计转化¥28,500")
# ==========================================
@router.get("/overview", summary="今日军情首页概览")
async def api_overview(request: Request):
    _require_admin(request)
    status_counts = marketing_db.count_cases_by_status()
    pending = status_counts.get("pending", 0)

    # 今日机会四卡:按 rule group 聚合 pending 案件数
    cases = marketing_db.list_cases(status="pending", limit=500)
    groups = {"high_value": 0, "convertible": 0, "dormant": 0, "offer": 0}
    for c in cases:
        g = RULE_GROUP.get(c.get("rule_key"), "offer")
        groups[g] = groups.get(g, 0) + 1

    self_ledger = marketing_db.read_view_one("marketing_v_self_ledger") or {}
    funnel = marketing_db.read_view_one("marketing_v_funnel") or {}
    last_patrol = marketing_db.last_patrol_run()

    # 本月预算水位(active 活动 spent / budget_cap 汇总)
    campaigns = marketing_db.list_campaigns(status="active")
    budget_cap = sum(int(c.get("budget_cap_points") or 0) for c in campaigns)
    budget_spent = sum(int(c.get("spent_points") or 0) for c in campaigns)

    return {
        "ok": True,
        # 三事实数(替代预测性收入)
        "facts": {
            "pending_approvals": pending,
            "touched_today": marketing_db.count_touches_today(),
            "budget_month": {"cap": budget_cap, "spent": budget_spent},
        },
        "opportunity_cards": groups,
        "funnel": funnel,
        "self_ledger": self_ledger,
        "last_patrol": last_patrol,
        "case_status_counts": status_counts,
        "flags": {p["key"]: bool((p.get("value_jsonb") or {}).get("enabled"))
                  for p in marketing_db.get_policies() if p["key"] in marketing_db.POLICY_KEYS},
    }


# ==========================================
# 立即巡逻(force · 人授权绕过 flag · 阻塞跑到 to_thread)
# ==========================================
@router.post("/patrol/run", summary="立即巡逻一轮(不依赖开关)")
async def api_run_patrol(request: Request):
    _require_admin(request)
    from services.marketing.patrol import run_patrol
    result = await asyncio.to_thread(run_patrol, force=True)
    if result is None:
        raise HTTPException(status_code=500, detail="巡逻信号查询失败(看服务端日志)")
    return {"ok": True, **result}


class DraftCaseRequest(BaseModel):
    direction: str = Field(min_length=4, max_length=200, description="一句话方向,如:给沉默的高价值客户做一场唤回")


@router.post("/cases/draft", summary="新建营销方案:一句话方向 → 真人群 + AI 起草完整方案进待审")
async def api_draft_case(request: Request, body: DraftCaseRequest):
    """[P0-B/P0-C 2026-07-05 老板拍板"重新梳理再开发"后重做] 军师从润色工变参谋:
    ① 实时按 cohort SQL 圈真人群(LLM 只在白名单里受限选择,数字永远来自 SQL);
    ② 方案名/文案由 LLM 走技能包起草(fail-soft 回落确定性版);
    ③ 质量闸:圈选 0 人不建案(宁可无案,不出空壳案)。
    幂等:同一方向同一天只建一案。逻辑在 services/marketing/manual_draft.py(纯服务可直测)。"""
    user = _require_admin(request)
    direction = body.direction.strip()
    # 守卫:方向本身不许带承诺词(方案文案层还会再过五绿勾)
    from services.marketing.guards import scan_forbidden
    g = scan_forbidden(direction)
    if not g["passed"]:
        raise HTTPException(status_code=422, detail=f"方向含违规用语,请调整:{list(g['flags'].keys())}")
    from services.marketing import manual_draft
    creator = int(current_user_id(user) or 0) or None
    result = await manual_draft.create_manual_draft(direction, creator)
    if not result.get("ok") and result.get("gate") == "schema":
        raise HTTPException(status_code=422, detail=result.get("note") or "起草未过强校验")
    return result


# ==========================================
# 建议案件队列 + 详情 + 审批三键
# ==========================================
@router.get("/cases", summary="建议案件队列")
async def api_list_cases(request: Request, status: Optional[str] = None,
                         limit: int = 100, offset: int = 0):
    _require_admin(request)
    cases = marketing_db.list_cases(status=status, limit=min(limit, 500), offset=offset)
    for c in cases:
        c["group"] = RULE_GROUP.get(c.get("rule_key"), "offer")
    return {"ok": True, "cases": cases, "status_counts": marketing_db.count_cases_by_status()}


@router.get("/cases/{case_id}", summary="案件详情 + 五绿勾实时校验")
async def api_get_case(case_id: int, request: Request):
    _require_admin(request)
    case = marketing_db.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="案件不存在")
    checks = approval.validate_case(case)
    case["group"] = RULE_GROUP.get(case.get("rule_key"), "offer")
    return {"ok": True, "case": case, "checks": checks}


@router.post("/cases/{case_id}/approve", summary="批准执行(五绿勾真校验 → 自动执行/dry_run)")
async def api_approve(case_id: int, request: Request, body: ApprovalActionRequest):
    admin = _require_admin(request)
    result = approval.approve_case(case_id, approver_id=_user_id(admin), note=body.note)
    if not result.get("ok"):
        # 校验失败:返回 422 + 五绿勾明细,前端标红
        if result.get("error") == "checks_failed":
            raise HTTPException(status_code=422, detail={"msg": "五绿勾未全过,不可批准", **result})
        raise HTTPException(status_code=400, detail=result.get("error"))
    # 批准即执行(flag 关时执行器内部自动 dry_run,零真实动作)
    from services.marketing.executors.reach import execute_approved_case
    exec_result = await asyncio.to_thread(execute_approved_case, case_id)
    return {**result, "execution": exec_result}


@router.post("/cases/{case_id}/execute", summary="重新执行(dry_run 彩排 / 真实由闸控)")
async def api_execute(case_id: int, request: Request):
    _require_admin(request)
    from services.marketing.executors.reach import execute_approved_case
    exec_result = await asyncio.to_thread(execute_approved_case, case_id)
    if not exec_result.get("ok"):
        raise HTTPException(status_code=400, detail=exec_result.get("error"))
    return exec_result


@router.post("/cases/{case_id}/reject", summary="驳回方案")
async def api_reject(case_id: int, request: Request, body: ApprovalActionRequest):
    admin = _require_admin(request)
    result = approval.reject_case(case_id, approver_id=_user_id(admin), note=body.note)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/cases/{case_id}/request-changes", summary="要求修改后重提")
async def api_request_changes(case_id: int, request: Request, body: ApprovalActionRequest):
    admin = _require_admin(request)
    result = approval.request_changes(case_id, approver_id=_user_id(admin), note=body.note)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ==========================================
# 事件流 / 活动台账 / 杠杆面板 / 信号规则
# ==========================================
@router.get("/events", summary="近期营销动态时间线")
async def api_events(request: Request, limit: int = 50):
    _require_admin(request)
    return {"ok": True, "events": marketing_db.list_events(limit=min(limit, 200))}


@router.get("/campaigns", summary="活动与台账")
async def api_campaigns(request: Request, status: Optional[str] = None):
    _require_admin(request)
    campaigns = marketing_db.list_campaigns(status=status)
    return {"ok": True, "campaigns": campaigns}


@router.post("/campaigns/{campaign_id}/activate", summary="活动上线")
async def api_campaign_activate(campaign_id: int, request: Request):
    _require_admin(request)
    from services.marketing.executors.grants import activate_campaign
    return activate_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/end", summary="活动即时下线(回滚)")
async def api_campaign_end(campaign_id: int, request: Request):
    _require_admin(request)
    from services.marketing.executors.grants import end_campaign
    return end_campaign(campaign_id)


@router.post("/grants/{grant_id}/revoke", summary="撤销冲销发放")
async def api_grant_revoke(grant_id: int, request: Request):
    _require_admin(request)
    from services.marketing.executors.grants import revoke_grant
    result = await asyncio.to_thread(revoke_grant, grant_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.get("/levers", summary="杠杆面板(只读快照 · 改价格跳原页)")
async def api_levers(request: Request):
    _require_admin(request)
    # 充值阶梯(config SSOT · 只读展示)
    recharge_packages = []
    try:
        from config import pricing_config as pc
        for name in ("RECHARGE_PACKAGES", "get_recharge_packages"):
            obj = getattr(pc, name, None)
            if callable(obj):
                recharge_packages = obj()
                break
            if obj:
                recharge_packages = obj
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("[marketing] 读充值阶梯失败: %s", e)

    sku_shelf = marketing_db.read_view("marketing_v_levers", limit=100)
    fc = marketing_db.get_campaign_by_code("firstcharge_double")
    return {
        "ok": True,
        "recharge_ladders": recharge_packages,
        "sku_shelf": sku_shelf,
        "first_charge_double": {
            "exists": bool(fc),
            "status": (fc or {}).get("status"),
            "is_resident": (fc or {}).get("is_resident"),
        },
        "note": "改价格类请到定价中心操作;此处只读快照。",
    }


@router.get("/signal-rules", summary="10 条信号规则 + 数据接线状态(诚实披露)")
async def api_signal_rules(request: Request):
    _require_admin(request)
    rules = []
    for rule_key, _fn in signal_rules.RULES:
        rules.append({
            "rule_key": rule_key,
            "group": RULE_GROUP.get(rule_key, "offer"),
            "data_status": RULE_DATA_STATUS.get(rule_key, "unknown"),
        })
    return {"ok": True, "rules": rules}


# ==========================================
# 策略 / Kill Switch
# ==========================================
@router.get("/policies", summary="策略与开关列表")
async def api_policies(request: Request):
    _require_admin(request)
    return {"ok": True, "policies": marketing_db.get_policies(),
            "flag_keys": list(marketing_db.POLICY_KEYS),
            "config_keys": list(marketing_db.CONFIG_KEYS)}


@router.patch("/policies/{key}", summary="更新策略/配置(白名单校验)")
async def api_patch_policy(key: str, request: Request, body: PolicyPatchRequest):
    admin = _require_admin(request)
    if key not in marketing_db.POLICY_KEYS and key not in marketing_db.CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"未知策略 key: {key}")
    marketing_db.set_policy(key, body.value, updated_by=_user_id(admin))
    logger.warning("[marketing] 策略变更 key=%s value=%s by=%s", key, body.value, _user_id(admin))
    marketing_db.add_event(event_type="policy_changed", actor_id=_user_id(admin),
                           severity="security", message=f"{key} = {body.value}",
                           payload={"key": key, "value": body.value})
    return {"ok": True, "key": key, "value": body.value}


@router.post("/kill-switch", summary="总急停(最高优先级瞬时全停)")
async def api_kill_switch(request: Request, body: KillSwitchRequest):
    admin = _require_admin(request)
    marketing_db.set_policy("marketing_agent.kill_switch", {"enabled": body.enabled},
                            updated_by=_user_id(admin))
    logger.warning("[marketing] KILL SWITCH = %s by=%s", body.enabled, _user_id(admin))
    marketing_db.add_event(event_type="kill_switch", actor_id=_user_id(admin), severity="security",
                           message=f"kill_switch={body.enabled}")
    return {"ok": True, "enabled": body.enabled}


# ==========================================
# 效果复盘 / 周报 / 归因 / 技能规则(Package F)
# ==========================================
@router.get("/effect", summary="效果复盘(4级漏斗 + KPI + 相关转化 + 对照组门控)")
async def api_effect(request: Request):
    _require_admin(request)
    from services.marketing.measurement import effect_overview
    return {"ok": True, **effect_overview()}


@router.get("/reports/weekly", summary="最新营销周报(AI 复盘卡)")
async def api_weekly_report(request: Request):
    _require_admin(request)
    from services.marketing.measurement import latest_weekly_report
    return {"ok": True, "report": latest_weekly_report()}


@router.post("/reports/weekly/generate", summary="立即生成营销周报")
async def api_weekly_report_gen(request: Request):
    _require_admin(request)
    from services.marketing.measurement import generate_weekly_report
    return await asyncio.to_thread(generate_weekly_report, True)


@router.post("/measurement/run", summary="立即跑一轮相关转化回测")
async def api_measurement_run(request: Request):
    _require_admin(request)
    from services.marketing.measurement import run_measurement
    return {"ok": True, **await asyncio.to_thread(run_measurement, True)}


@router.get("/skill-packs", summary="技能包列表(策划热加载)")
async def api_skill_packs(request: Request):
    _require_admin(request)
    return {"ok": True, "skill_packs": marketing_db.list_active_skill_packs()}


class SkillPackUpsertRequest(BaseModel):
    pack_code: str
    title: str
    content: str
    bind_signals: list = Field(default_factory=list)
    weight: int = 100
    is_active: bool = True


@router.post("/skill-packs", summary="沉淀为策略规则 / 新增技能包(人批入库)")
async def api_skill_pack_upsert(request: Request, body: SkillPackUpsertRequest):
    admin = _require_admin(request)
    pack = marketing_db.upsert_skill_pack(
        pack_code=body.pack_code, title=body.title, content=body.content,
        bind_signals=body.bind_signals, weight=body.weight, is_active=body.is_active)
    marketing_db.add_event(event_type="skill_pack_upserted", actor_id=_user_id(admin),
                           message=f"技能包 {body.pack_code} 入库/更新")
    return {"ok": True, "skill_pack": pack}
