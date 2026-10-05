"""现役业务页面的**预填投影**(规格 §12.3 · XO-03)。

## 它解决的是哪件事

XO-03 签发的是「复杂核对**打开并预填现役页面**」。deep link 只带一个不可猜的
``xint_...``(§12.2:URL 里不放客户名、供应商、算力公式或完整内容),所以页面
拿到手的信息量 = 一个 opaque id。页面必须能拿它向服务端换回**已授权的预填数据**,
否则就只能打开一张空的默认表单 —— 那正是 §4.1 记的六个 ``prefill`` 动作的存量债。

## 为什么是**投影**而不是把 intent 行直接吐出去

§12.3 对页面提出的是**同屏可见清单**,不是「有哪些字段」:

* 当前客户、当前对象;
* 小榜推荐和原因;
* 所有将产生的外部动作;
* 目标渠道/账号、发布后是否公开、payer 和准确算力 —— **必须在主按钮上方**;
* 主按钮二选一:无需审批的「确认并执行 · 使用 X 算力」/ 需审批的
  「提交负责人确认 · 暂不使用算力」;
* 取消动作**跟真实边界一致**(见 :func:`cancel_action`)。

把 intent 行原样返回,这些语义就落到前端各自拼 —— 拼错的方向恰好是最贵的那种
(把「已开始执行」还显示成「取消 · 不使用算力」= 对用户承诺了一件我们做不到的事)。
所以文案在服务端定,前端只渲染。

## 🔴 这里**不**做的事

* 不做鉴权。调用方必须已经过 ``load_intent`` 的 actor 绑定校验;
* 不读 KB。文案是固定模板 + 实时事实,KB degraded 不影响本投影(§13.2);
* 不编数字。报价拿不到就说拿不到(``compute.state``),绝不给一个看起来精确的值。

## 为什么动作用 ``next_action_id`` 而不是 ``kind``

DLP-A 把裸 ASCII 枚举当泄漏,除非该键在 ``FIVE_PHASE_MACHINE_KEYS`` 里。
``kind`` 不在,而**把 ``kind`` 加进那张共享表**会给所有五阶段出参一起开洞
(该模块自己写着「像 ``state`` 这样的通用键一旦进共享表,别人的出参也会跟着开洞」)。
所以这里复用已声明的机读键 ``next_action_id`` / ``state``,不去拓宽白名单。

同理,主按钮的 id 叫 ``confirm_then_run`` 而不是 ``confirm_and_execute``:
DLP 把 ``<domain>_<object>_<phase>``(phase ∈ 五阶段)当内部 adapter 名,
``..._execute`` 正好撞上。撞了就换自己的名字,**不去放宽那条共享规则** ——
放宽一次,真的 adapter 名以后就漏出去了。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from services.xiaobang_command_contract import SIDE_EFFECT_EXTERNAL
from services.xiaobang_intent import (
    STATE_APPROVAL_PENDING,
    STATE_APPROVAL_REJECTED,
    STATE_AWAITING_CONFIRMATION,
    STATE_CANCELLED,
    STATE_CONFIRMED,
    STATE_EXECUTABLE,
    STATE_EXECUTION_LINKED,
    STATE_EXPIRED,
    STATE_PREPARED,
    STATE_SUPERSEDED,
)

PREFILL_CONTRACT_VERSION = "xiaobang-page-prefill-v1"

#: 取消仍然安全(零外发、零冻结)的协调态。
_CANCEL_STILL_SAFE: frozenset[str] = frozenset({
    STATE_PREPARED, STATE_AWAITING_CONFIRMATION,
    STATE_CONFIRMED, STATE_APPROVAL_PENDING, STATE_EXECUTABLE,
})

#: 已经进入领域执行的协调态。
_ALREADY_RUNNING: frozenset[str] = frozenset({STATE_EXECUTION_LINKED})

#: 终态:没有取消,只有回小榜重开。
_TERMINAL: frozenset[str] = frozenset({
    STATE_CANCELLED, STATE_EXPIRED, STATE_SUPERSEDED, STATE_APPROVAL_REJECTED,
})


def primary_action(
    row: Mapping[str, Any],
    *,
    needs_approval: bool,
    compute: Mapping[str, Any],
    executable: bool = True,
) -> dict:
    """主按钮。二选一由**是否需要审批**决定,不由前端猜。

    🔴 算力数字只在真的报到价时才进文案。报不出价还写「使用 X 算力」,
       用户会按那个数字做决定 —— 那比不显示数字坏得多。

    🔴 [微单 2026-08-20] ``enabled`` 必须跟着 ``contract.executable`` 走。

    在这之前它只看协调态 ⇒ 对 ``executable=False`` 的 operation
    (``writing_center`` / ``geo_content_center``)照样返回
    ``enabled=True`` + 「确认并执行 · 使用 X 算力」——
    而 ``POST …/execute`` 对它们一律 409 ``DOMAIN_ADAPTER_NOT_AVAILABLE``。
    **那就是一颗死按钮**:界面承诺了一件我们做不到的事,用户点下去只会得到一句错误。

    ``executable`` 默认 ``True`` = 本函数历史行为逐字节不变(既有直接调用点不受影响);
    ``build_prefill`` 从注册表合同取真值传进来。
    """
    state = str(row.get("intent_state") or "")
    if state in _TERMINAL:
        return {"next_action_id": "restart", "label": "重新准备", "enabled": True}
    if state in _ALREADY_RUNNING:
        return {"next_action_id": "view_progress", "label": "查看进度", "enabled": True}
    ready = state in (STATE_PREPARED, STATE_AWAITING_CONFIRMATION)
    if needs_approval:
        return {
            "next_action_id": "submit_for_approval",
            "label": "提交负责人确认 · 暂不使用算力",
            "enabled": ready and bool(executable),
        }
    if not executable:
        # 🔴 **诚实降级,不是把按钮画灰了摆在那儿**:
        #    这条 operation 的执行链还没开放,所以既不承诺"确认并执行",
        #    也不显示算力数字(报了价却执行不了,数字只会误导决策)。
        #    下一步是真实存在的那一个:去现役页面继续做。
        return {
            "next_action_id": "open_page",
            "label": "在这一页继续做",
            "enabled": False,
            "note": "这一步暂时不能在小榜里直接执行,已经准备好的内容都在,不会重复。",
        }
    amount = compute.get("amount")
    if compute.get("state") == "quoted" and amount is not None:
        label = "确认并执行 · 使用 {0} {1}".format(amount, compute.get("unit") or "算力")
    else:
        # 没报到价:说清楚下一步是先算价,不给假数字。
        label = "确认并执行 · 算力按你选的内容实时计算"
    return {
        "next_action_id": "confirm_then_run",
        "label": label,
        "enabled": ready,
    }


def cancel_action(row: Mapping[str, Any], projection: Mapping[str, Any]) -> dict:
    """取消动作。文案必须跟**真实边界**一致(§12.3)。

    三档,顺序不能换:

    1. 外部态 ``unknown`` —— 我们不知道对外发生了什么。这时说「已取消」
       「已退款」「可以重试」都是替一件不知道的事下结论 → 只能给「人工核对」;
    2. 已开始执行 —— 不再给普通「取消」,只给「查看进度」,``申请停止`` 仅在
       现役领域**真的**支持时才出现(``supports_stop``);
    3. 尚未 external-start —— 「取消本次操作 · 不使用算力」,这是唯一能承诺的那句。
    """
    external = str((projection.get("external_projection") or {}).get("state") or "")
    state = str(row.get("intent_state") or "")

    if external == "unknown":
        return {
            "next_action_id": "manual_reconcile",
            "label": "人工核对",
            "note": "这一步对外发生了什么还没确认,请人工核对后再判断。",
            "enabled": True,
        }
    if state in _ALREADY_RUNNING or external in ("started", "accepted", "succeeded"):
        return {
            "next_action_id": "view_progress",
            "label": "查看进度",
            "note": "已经开始执行,取消要走对应领域的申请通道。",
            "enabled": True,
            "stop_request": None,
        }
    if state in _CANCEL_STILL_SAFE:
        return {
            "next_action_id": "cancel_free",
            "label": "取消本次操作 · 不使用算力",
            "enabled": True,
        }
    return {"next_action_id": "none", "label": "", "enabled": False}


def _external_actions(row: Mapping[str, Any], preview: Mapping[str, Any]) -> list[dict]:
    """「所有将产生的外部动作」。

    ``compute_only`` 档明确返回空列表 —— 空列表和「还没算出来」必须能区分,
    所以调用方拿到的永远是 list,不是 ``None``。
    """
    if str(row.get("side_effect") or "") != SIDE_EFFECT_EXTERNAL:
        return []
    declared = preview.get("external_actions")
    if isinstance(declared, list):
        return [dict(item) for item in declared if isinstance(item, dict)]
    # 发布域 adapter 接进来之前:如实说"还没解析出来",不假装是空。
    return [{
        "state": "pending_domain_adapter",
        "label": "对外动作要在核对页选定投放账号后才能逐条列出",
    }]


#: [R3-P7 ③] 工程枚举 → 人话。元指令「工程术语全站翻译人话」。
#: 🔴 这不是洁癖:DLP-A 把裸 ASCII 枚举当泄漏并**抛异常**,而这几个值原样出现在
#:    ``preview`` 里 ⇒ 真 HTTP 打过去就是 500。素树(生产尖 99492532)实测同样判红,
#:    也就是说 ``POST /prepare`` 在生产上就是这个形态 —— 详见交付单上报项。
_PENDING_INPUT_LABELS: dict[str, str] = {
    "channel_eligibility": "投放账号资格还没核",
    "rate_limit": "发布频次限制还没核",
    "inventory_version": "可投库存还没核",
}


def humanize_pending_inputs(values) -> list[str]:
    """把未覆盖的 manifest 输入翻成人话;不认识的值**不硬塞英文**。"""
    out: list[str] = []
    for raw in values or []:
        label = _PENDING_INPUT_LABELS.get(str(raw))
        if label:
            if label not in out:
                out.append(label)
        elif "还有一项没核对" not in out:
            out.append("还有一项没核对")
    return out

#: [R3-P7 ③] 对象类型 → 人话前缀。DTO 里同时给机读键 ``resource_kind``
#: (它在 DLP 的机读白名单里)和人话 ``label``,前端不用自己拼。
_OBJECT_KIND_LABELS: tuple[tuple[str, str], ...] = (
    ("brand", "客户"),
    ("quote", "报价"),
    ("article", "文章"),
    ("geo_image_post", "图文"),
    ("publication", "发布任务"),
    ("monitoring_task", "监测任务"),
)


def object_identity(preview: Mapping[str, Any]) -> dict:
    """**可展示的对象标识**(R3-P7 ③)。

    R3-P6 的 ``object`` 只有 ``scope`` 与 ``pending_manifest_inputs`` ——
    那是给工程看的,页面上显示不出"这一步到底在动哪个东西"。
    §12.3 要求「当前对象」同屏可见,所以这里产出**逐条可读的对象清单**。

    只用 ``preview`` 里已授权的字段;拼不出名字时退回「<类型> #<id>」,
    **不编名字**(编一个名字会让用户以为选的是别的对象)。
    """
    declared = preview.get("object_items")
    items: list[dict] = []
    if isinstance(declared, list):
        for raw in declared:
            if not isinstance(raw, Mapping):
                continue
            kind = str(raw.get("resource_kind") or "")
            name = raw.get("label") or raw.get("name")
            ref = raw.get("ref")
            prefix = dict(_OBJECT_KIND_LABELS).get(kind, "对象")
            label = str(name) if name else "{0} #{1}".format(prefix, ref if ref else "?")
            items.append({"resource_kind": kind, "label": label})
    return {
        "label": " · ".join(item["label"] for item in items) or None,
        "items": items,
    }


def build_prefill(
    row: Mapping[str, Any],
    *,
    entry,
    projection: Mapping[str, Any],
    needs_approval: bool = False,
    quote_state: Optional[str] = None,
    quote_note: Optional[str] = None,
) -> dict:
    """把一行 intent + 现役注册项投影成页面预填合同。"""
    preview = dict(row.get("preview") or {})
    settlement = dict(projection.get("settlement_projection") or {})
    compute = {
        "state": quote_state or ("quoted" if row.get("compute_quote_amount") is not None
                                 else "unavailable"),
        "amount": row.get("compute_quote_amount"),
        "unit": row.get("compute_quote_unit") or "算力",
        "expires_at": row.get("compute_quote_expires_at"),
    }
    if quote_note:
        compute["note"] = quote_note

    payload = {
        "contract_version": PREFILL_CONTRACT_VERSION,
        "intent_id": row["intent_id"],
        "intent_state": row["intent_state"],
        "intent_revision": int(row["intent_revision"]),
        "operation_id": entry.operation_id,
        "display_name": entry.display_name,
        "side_effect": row.get("side_effect"),
        "target_route": entry.route_template,
        "help_target": entry.help_target,
        # ── §12.3 主按钮上方必须同屏可见的那批 ──────────────────────────
        "customer": {"label": preview.get("customer_label")},
        "object": {
            # 🔴 ``scope`` 是内部工程标记(``wp1_identity_and_object_refs``),
            #    对用户零信息量,且是裸 ASCII 枚举 ⇒ DLP 判泄漏。不进用户面 DTO。
            "pending_checks": humanize_pending_inputs(preview.get("pending_manifest_inputs")),
            # [R3-P7 ③] 可展示对象标识 —— 缺它页面就显示不出"在动哪个东西"。
            **object_identity(preview),
        },
        "recommendation": {
            "reasons": list(row.get("reason_facts") or []),
        },
        "external_actions": _external_actions(row, preview),
        "payer": {"label": preview.get("payer_label"), "role": preview.get("payer_role")},
        "visibility": preview.get("visibility"),
        "channel": preview.get("channel") or {},
        "compute": compute,
        # ── 动作 ────────────────────────────────────────────────────────
        "primary_action": primary_action(
            row, needs_approval=needs_approval, compute=compute,
            # 🔴 [微单 2026-08-20] 真值从**注册表合同**取,不从 row 猜:
            #    合同是"这条 operation 到底能不能执行"的唯一出处。
            executable=bool(getattr(getattr(entry, "command_contract", None),
                                    "executable", False))),
        "cancel_action": cancel_action(row, projection),
        "secondary_actions": [
            {"next_action_id": "back_to_assistant", "label": "返回小榜"},
            {"next_action_id": "reprepare", "label": "重新准备"},
        ],
        # ── 表单预填(白名单;用户改了这些字段就要重算)────────────────
        "form_prefill": dict(preview.get("form_prefill") or {}),
        "rebind_hint": {
            "label": "内容已改,算力需要更新",
            "primary_label": "重新计算并核对",
            "watched_fields": sorted((preview.get("form_prefill") or {}).keys()),
        },
        "settlement_state": settlement.get("state"),
    }
    return payload
