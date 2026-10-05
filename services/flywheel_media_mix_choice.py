"""B3 媒体组合决策(2026-07-29)· 接媒体工单 T2。

T2(`services/question_family_mix.py`,包 `codex/media-balance-strict-profile-2026-07-29`)已经把
**统计**做完了:按问题族数真实被引 mix、折算主干 N + 垂类 M、缺货同族降位。
B3 只接管其中**取舍**那一步:同样一份 mix,八个坑位到底给谁,由 v4-flash 判。

工单原话:"判断部分改由 v4-flash 做,**统计口径仍由代码算(防编数)**"。这条在本模块是硬约束:

  · 模型只能吐一个 `selected_domains` 域名列表,**必须是观测 mix 里已有的域**,越界的一律丢弃;
  · 每个坑位的 role / is_trunk / share / citations **全部由代码从 mix 里回填**,模型报的数字一概不采信;
  · trunk_slots / vertical_slots 由代码数出来,不读模型的;
  · mix 同时有主干和垂类时,模型若选出 100% 主干或 100% 垂类 → **判为不合规,退回代码方案** ——
    那正是这台引擎存在的意义(修正发布组合与真实被引域的错配),不能被一次模型抖动毁掉。

零耦合:本模块**不 import** T2 任何符号,只吃 `CombinationPlan.as_dict()` / `QuestionFamilyMix.as_dict()`
产出的普通 dict。所以两个包各自评审、各自部署,合流时只需在 T2 的单一消费点
(`services/placement_service.py::_question_family_annotations`)套一层 `refine_media_combination(...)`。
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-FlywheelMediaMix")

POINT_KEY = "media_mix_decision"


def _substitution_demand_brief(limit: int = 8) -> list[dict[str, Any]]:
    """补货缺口清单(T2 的 `top_substitution_demand`)。T2 不在场时安静返空。"""
    try:
        from services.question_family_mix import top_substitution_demand  # type: ignore

        rows = top_substitution_demand(limit=limit) or []
    except Exception:
        return []
    brief = []
    for row in rows[:limit]:
        brief.append({
            "missing_domain": row.get("missing_domain"),
            "role": row.get("role") or row.get("role_label"),
            "demand_score": row.get("demand_score") or row.get("score"),
        })
    return brief


def _entries_by_domain(mix: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(e.get("domain") or ""): e
        for e in (mix.get("entries") or [])
        if str(e.get("domain") or "")
    }


def _rebuild_slots(selected: list[str], entries: dict[str, dict[str, Any]],
                   previous_slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按模型选中的域重建坑位。**所有数值字段一律取 mix 里的观测值,不取模型输出。**

    履约字段(fulfilled_by / substituted / self_serve_action)沿用代码此前算好的同域坑位 ——
    缺货降位是 T2 的确定性规则,B3 不碰。
    """
    prior = {str(s.get("domain") or ""): s for s in previous_slots or []}
    slots: list[dict[str, Any]] = []
    for domain in selected:
        entry = entries[domain]
        base = dict(prior.get(domain) or {})
        base.update({
            "domain": domain,
            "role": entry.get("role"),
            "role_label": entry.get("role_label"),
            "is_trunk": bool(entry.get("is_trunk")),
            "share_pct": entry.get("share_pct"),
            "citations": entry.get("citations"),
        })
        base.setdefault("fulfilled_by", "")
        base.setdefault("fulfilled_media_id", None)
        base.setdefault("substituted", False)
        base.setdefault("substitution_note", "")
        base.setdefault("self_serve_action", None)
        slots.append(base)
    return slots


def _build_prompt(mix: dict[str, Any], plan: dict[str, Any],
                  demand: list[dict[str, Any]]) -> str:
    entries = [
        {
            "domain": e.get("domain"),
            "citations": e.get("citations"),
            "share_pct": e.get("share_pct"),
            "role_label": e.get("role_label"),
            "is_trunk": e.get("is_trunk"),
            "self_serve": e.get("self_serve"),
        }
        for e in (mix.get("entries") or [])
    ]
    current = [
        {"domain": s.get("domain"), "is_trunk": s.get("is_trunk"),
         "fulfilled_by": s.get("fulfilled_by"), "substituted": s.get("substituted")}
        for s in (plan.get("slots") or [])
    ]
    total_slots = int(plan.get("total_slots") or len(current) or 8)
    return (
        "你在为一篇 B 端品牌文章挑发布媒体组合。目标是让内容出现在 AI 真正会引用的地方。\n\n"
        "【真实被引分布(系统统计,禁止改动或自行推算这些数字)】\n"
        + json.dumps(entries, ensure_ascii=False, indent=2)
        + f"\n\n口径说明:share_pct = 该域在这类问题下占全部 AI 引用的比例;"
        f"is_trunk=true 表示主干媒体(门户/技术社区一类),false 表示垂直站;"
        f"self_serve=true 表示用户可以自己注册发布。\n"
        f"数据范围:{mix.get('explanation') or ''}(来源层级 {mix.get('source')})\n\n"
        "【系统按占比折算出的默认组合】\n"
        + json.dumps(current, ensure_ascii=False, indent=2)
        + ("\n\n【当前缺货最严重的角色(补货信号,仅供参考)】\n"
           + json.dumps(demand, ensure_ascii=False, indent=2) if demand else "")
        + f"\n\n请给出本次的 {total_slots} 个发布位。要求:\n"
        f"1. 只能从上面【真实被引分布】里出现过的 domain 中挑,**不要发明任何域名**;\n"
        f"2. 必须同时包含主干和垂直站(数据里两类都有时),不要做成清一色 ——"
        f"AI 引用本身就是混合结构,做成一边倒会重演当前的错配;\n"
        f"3. 被引占比高的优先,但可以为覆盖不同角色而让位一两个坑;\n"
        f"4. 恰好给 {total_slots} 个,不重复。\n\n"
        '严格只输出 JSON:{"selected_domains": ["a.com", "b.com"], "reason": "一句话说明取舍"}'
    )


def _prepare(annotations: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把 annotations 拆成取舍需要的零件。没有取舍空间时返回 ``None``。"""
    mix = (annotations or {}).get("question_family_mix")
    plan = (annotations or {}).get("combination_plan")
    if not isinstance(mix, dict) or not isinstance(plan, dict):
        return None
    entries = _entries_by_domain(mix)
    if len(entries) < 2:
        return None  # 没有取舍空间,不浪费一次调用
    previous_slots = list(plan.get("slots") or [])
    return {
        "mix": mix,
        "plan": plan,
        "entries": entries,
        "previous_slots": previous_slots,
        "total_slots": int(plan.get("total_slots") or len(previous_slots) or 8),
        "has_trunk": any(bool(e.get("is_trunk")) for e in entries.values()),
        "has_vertical": any(not bool(e.get("is_trunk")) for e in entries.values()),
    }


def _make_validator(parts: dict[str, Any]) -> Any:
    """结构守卫。**缓存命中时也要跑一遍** —— 缓存写下之后 mix 可能已经变了
    (补货/缺货会改 entries),旧选择若已越界或已一边倒,必须当作没命中。"""
    entries = parts["entries"]
    total_slots = parts["total_slots"]
    has_trunk, has_vertical = parts["has_trunk"], parts["has_vertical"]

    def _validate(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        domains = payload.get("selected_domains")
        if not isinstance(domains, list) or not domains:
            return False
        picked = [d for d in domains if isinstance(d, str) and d in entries]
        if len(set(picked)) < min(2, total_slots):
            return False
        # 混合结构守卫:数据里两类都有,选择却一边倒 → 判不合规。
        if has_trunk and has_vertical:
            trunk_n = sum(1 for d in set(picked) if entries[d].get("is_trunk"))
            if trunk_n == 0 or trunk_n == len(set(picked)):
                return False
        return True

    return _validate


def _input_summary(parts: dict[str, Any], keyword: str, industry: str) -> dict[str, Any]:
    """留痕的输入摘要。**同时是缓存键的载体** —— ``keyword`` / ``industry`` /
    ``total_slots`` 三个字段被 ``flywheel_media_mix_cache.lookup_choice`` 按
    ``input_summary->>'…'`` 反查,改名等于把缓存全部打穿,改前先改那边的 SQL。"""
    return {
        "keyword": keyword, "industry": industry,
        "mix_source": parts["mix"].get("source"),
        "candidates": len(parts["entries"]), "total_slots": parts["total_slots"],
        "total_citations": parts["mix"].get("total_citations"),
    }


def _apply_selection(parts: dict[str, Any], payload: dict[str, Any],
                     choice_meta: dict[str, Any]) -> bool:
    """把模型选中的域落到 plan 上。**所有数值字段回填自 mix,不采信模型。**

    返回 ``True`` = 已应用;``False`` = 选出来的太少,调用方应保留代码方案。
    """
    plan, entries = parts["plan"], parts["entries"]
    total_slots, previous_slots = parts["total_slots"], parts["previous_slots"]

    selected: list[str] = []
    for value in payload.get("selected_domains") or []:
        if isinstance(value, str) and value in entries and value not in selected:
            selected.append(value)
    selected = selected[:total_slots]
    if len(selected) < min(2, total_slots):
        return False

    plan["slots"] = _rebuild_slots(selected, entries, previous_slots)
    # 主干/垂类计数由代码数,不读模型。
    plan["trunk_slots"] = sum(1 for s in plan["slots"] if s.get("is_trunk"))
    plan["vertical_slots"] = len(plan["slots"]) - plan["trunk_slots"]
    plan["substitutions"] = [s for s in plan["slots"] if s.get("substituted")]
    plan["self_serve_offers"] = [s for s in plan["slots"] if s.get("self_serve_action")]
    plan["advisory"] = True
    plan["llm_choice"] = dict(choice_meta, applied=True,
                              reason=str(payload.get("reason") or "")[:300],
                              code_plan_domains=[str(s.get("domain") or "")
                                                 for s in previous_slots])
    logger.info(
        "[B3] 媒体组合改由模型取舍(%s):%s → %s",
        choice_meta.get("source"), [s.get("domain") for s in previous_slots], selected,
    )
    return True


def refine_media_combination(
    annotations: dict[str, Any], *, keyword: str = "", industry: str = ""
) -> dict[str, Any]:
    """给 T2 产出的 annotations 附加/替换一份由 v4-flash 取舍的组合。**会同步调 LLM。**

    入参与出参都是 `_question_family_annotations` 的返回结构
    ``{"question_family_mix": ..., "combination_plan": ...}``。
    **任何异常路径都返回原样 annotations** —— 发布推荐绝不因为智能层不可用而变差。

    ⚠️ 2026-07-28 起**请求路径不再直接调本函数**(它会同步阻塞 ~1.8 秒并每次花钱),
    热路径改走 :func:`refine_media_combination_cached`。本函数保留为
    「真正去做一次判定」的唯一实现,由后台预热线程调用,判定语义与改前逐行一致。
    """
    try:
        parts = _prepare(annotations)
        if parts is None:
            return annotations

        from services.flywheel_judgment import judge

        result = judge(
            POINT_KEY,
            prompt=_build_prompt(parts["mix"], parts["plan"], _substitution_demand_brief()),
            rule_fallback=lambda: None,
            input_summary=_input_summary(parts, keyword, industry),
            validate=_make_validator(parts),
        )

        if not result.from_llm or not isinstance(result.payload, dict):
            # 规则兜底 = 原封不动用代码算好的组合(改前行为)。
            parts["plan"]["llm_choice"] = {
                "source": result.source, "fallback_reason": result.fallback_reason,
                "applied": False,
            }
            return annotations

        applied = _apply_selection(
            parts, result.payload,
            {"source": result.source, "provider": result.provider, "model": result.model},
        )
        if not applied:
            parts["plan"]["llm_choice"] = {"source": result.source, "applied": False,
                                           "fallback_reason": "selection_too_small"}
        return annotations
    except Exception as exc:
        logger.warning("[B3] 组合取舍异常,沿用代码方案: %s", exc)
        return annotations


def refine_media_combination_cached(
    annotations: dict[str, Any], *, keyword: str = "", industry: str = ""
) -> dict[str, Any]:
    """**热路径专用**:只读缓存,绝不在请求里调 LLM。

    * 命中且仍合规 → 按缓存里的选择重建坑位(与同步路径同一段 ``_apply_selection``);
    * 未命中 → 保留 T2 代码算好的确定性组合(= 判断点关闭时的既有行为,**不劣化**),
      并投递一次后台预热,下次进来就有了。

    这里刻意**不返回"暂无组合建议"**:代码方案是 T2 纯统计算出来的,不花钱也不慢,
    质量等同于 ``FLYWHEEL_JUDGE_MEDIA_MIX_DECISION`` 关闭时代理一直在看的那一份。
    把它换成空态,等于为了省一次 LLM 调用而主动让推荐面变差,方向反了。
    ``llm_choice.applied=False`` + ``warming`` 已经把"这次没走模型"说清楚了。
    """
    try:
        parts = _prepare(annotations)
        if parts is None:
            return annotations

        from services.flywheel_judgment import PROMPT_VERSION, judgment_enabled
        from services.flywheel_media_mix_cache import (
            cache_key, lookup_choice, submit_warm,
        )

        plan = parts["plan"]
        if not judgment_enabled(POINT_KEY):
            # 判断点没开 = 本来就该原样用代码方案,连缓存都不必查。
            plan["llm_choice"] = {"source": "rule", "applied": False,
                                  "fallback_reason": "point_disabled"}
            return annotations

        hit = lookup_choice(
            point_key=POINT_KEY, industry=industry, keyword=keyword,
            total_slots=parts["total_slots"], prompt_version=PROMPT_VERSION,
        )
        if hit and _make_validator(parts)(hit["payload"]):
            applied = _apply_selection(
                parts, hit["payload"],
                {"source": "cache", "cache_age_seconds": hit["age_seconds"]},
            )
            if applied:
                return annotations
            # 缓存里的选择在当下 mix 里已经缩到不够用 → 当作没命中,往下走预热。

        key = cache_key(point_key=POINT_KEY, industry=industry, keyword=keyword,
                        total_slots=parts["total_slots"], prompt_version=PROMPT_VERSION)
        # 预热用**当下这份 annotations 的深拷贝**:后台线程会往里写坑位,
        # 直接传引用会和正在返回给用户的那份对象打架。
        snapshot = copy.deepcopy({"question_family_mix": parts["mix"],
                                  "combination_plan": parts["plan"]})
        warming = submit_warm(
            key,
            lambda: refine_media_combination(snapshot, keyword=keyword, industry=industry),
        )
        plan["llm_choice"] = {
            "source": "cache", "applied": False,
            "fallback_reason": "cache_miss", "warming": bool(warming),
        }
        return annotations
    except Exception as exc:
        logger.warning("[B3] 缓存取舍异常,沿用代码方案: %s", exc)
        return annotations
