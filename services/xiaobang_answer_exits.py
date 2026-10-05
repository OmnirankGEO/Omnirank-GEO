"""小榜「每一轮至少有一个出口」+ 重复兜底自动升级(包 D①③⑤)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 修的是什么

工单 §5.3 / §8 S05-S06:
* 「禁止以『去帮助中心』作为唯一答复或唯一动作」;
* 「同一会话第二次准备输出同一个无解文案或同一个帮助 route 时,
   必须改为澄清或人工接管;禁止连续两次帮助中心 fallback」。

改之前:低置信兜底那支下发 `handoff: true`,但**没有任何结构化出口** ——
用户读到「点下方反馈」,下方什么都没有(前端那半截见包 D①,已修)。
而且**没有任何东西记得上一轮给过什么**,所以同一句话可以无限重复。

## 重复检测靠什么

🔴 **不新建服务端会话表**(工单 §7.4 明令)。
   靠的是包 A③ 已经在做的事:浏览器每次请求随身带 `recent_turns`。
   服务端因此**看得见上一轮自己说过什么**,不需要记住任何东西 ——
   有界、可审计、用户清掉浏览器历史后服务端零残留。

## 出口的最小集合

`clarify` / `retry` / `manual_path` / `handoff` / `resolved` / `unresolved`。
每一个都带 `exit_id`(前端据此渲染,不猜文案)与人话 `label`。
**任何一轮低置信/失败都至少有一个出口** —— `assert_has_exit()` 把这条钉住。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Mapping, Optional

EXIT_CLARIFY = "clarify"
EXIT_RETRY = "retry"
EXIT_MANUAL = "manual_path"
EXIT_HANDOFF = "handoff"
EXIT_RESOLVED = "resolved"
EXIT_UNRESOLVED = "unresolved"

ALL_EXIT_IDS = (EXIT_CLARIFY, EXIT_RETRY, EXIT_MANUAL,
                EXIT_HANDOFF, EXIT_RESOLVED, EXIT_UNRESOLVED)

#: 兜底文案的**形态**特征 —— 判结构不判具体措辞。
#: 措辞会改(而且一定会改),形态不会:「答不上来」+「去看文档/找人」。
_FALLBACK_SHAPE = re.compile(
    r"(没找到|找不到|不够|无法|回答不了|不清楚|暂时不支持)"
)

#: 归一化时要抹掉的东西:标点、空白、以及会逐轮变化的页面名。
_NOISE = re.compile(r"[\s,，。;;;：:!！?？、「」『』()()\[\]【】·—\-]+")


def looks_like_fallback(text: str) -> bool:
    """这段答复是不是「我答不上来」形态。"""
    return bool(_FALLBACK_SHAPE.search(str(text or "")))


def fallback_signature(text: str) -> Optional[str]:
    """给一段兜底答复算一个稳定指纹;不是兜底就返回 None。

    指纹**不含**页面名之类逐轮变化的部分:同一句兜底套在不同页面上,
    对用户来说仍然是「又一次没答上来」,必须被认成同一条。
    """
    body = str(text or "")
    if not looks_like_fallback(body):
        return None
    # 只取形态词本身 + 是否指向文档/人工,不取整句
    shape = "|".join(sorted(set(_FALLBACK_SHAPE.findall(body))))
    punt = "doc" if re.search(r"(帮助中心|帮助文档|使用说明|文档)", body) else ""
    manual = "manual" if re.search(r"(反馈|工作人员|人工|客服)", body) else ""
    key = _NOISE.sub("", shape + punt + manual)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def previous_fallback_signatures(recent_turns: Iterable[Mapping[str, Any]]) -> list[str]:
    """从有界历史里把**助手说过的**兜底指纹捞出来(正序)。"""
    out: list[str] = []
    for turn in recent_turns or ():
        if not isinstance(turn, Mapping):
            continue
        if turn.get("role") != "assistant":
            continue
        sig = fallback_signature(turn.get("content") or "")
        if sig:
            out.append(sig)
    return out


def is_repeat_fallback(recent_turns, candidate_text: str) -> bool:
    """这一轮又要给同一条兜底了吗。

    🔴 判的是「**同一条**又来一次」,不是「以前兜底过」——
       上一轮兜底、这一轮换了个不同的兜底,那是进展不是循环。
    """
    sig = fallback_signature(candidate_text)
    if not sig:
        return False
    return sig in previous_fallback_signatures(recent_turns)


def _exit(exit_id: str, label: str, **extra) -> dict[str, Any]:
    out = {"exit_id": exit_id, "label": label}
    out.update(extra)
    return out


def build_exits(
    *,
    confidence: str = "high",
    handoff: bool = False,
    repeat_fallback: bool = False,
    retryable: bool = False,
) -> list[dict[str, Any]]:
    """给这一轮组装出口清单。

    规则:
      · 只要 `handoff` 或 `repeat_fallback` → 必须有 `handoff` 出口;
      · `repeat_fallback` 额外给 `clarify`(第二次同样答不上来时,先问清楚再说);
      · 低置信 → 至少给 `unresolved`(用户能一键说「没解决」);
      · 任何一轮都给 `resolved`(闭环信号,也是飞轮的输入);
      · `retryable`(只读请求失败/动态源不可用)→ 给 `retry` + `manual_path`。
    """
    exits: list[dict[str, Any]] = []
    if repeat_fallback:
        exits.append(_exit(EXIT_CLARIFY, "告诉我更具体一点,我再定位一次"))
    if handoff or repeat_fallback:
        exits.append(_exit(
            EXIT_HANDOFF, "还没解决,提交给工作人员",
            # 让用户**知道会提交什么** —— 工单 §6 包 D③ 原话
            submits=["你这条问题原文", "当前页面", "最近几轮对话", "我刚才的回答"],
        ))
    if retryable:
        exits.append(_exit(EXIT_RETRY, "重试一次"))
        exits.append(_exit(EXIT_MANUAL, "先按手动步骤走"))
    if confidence == "low" and not any(e["exit_id"] == EXIT_HANDOFF for e in exits):
        exits.append(_exit(EXIT_UNRESOLVED, "没解决"))
    exits.append(_exit(EXIT_RESOLVED, "解决了"))
    return exits


def assert_has_exit(exits: list[dict[str, Any]], *, where: str = "") -> None:
    """🔴 任何失败/低置信答复都**至少有一个**出口。

    工单 §5.1 兜底条 + §9.1「所有失败响应至少有一个 action;
    故意删除 action 后测试必须失败」。
    """
    if not exits:
        raise ValueError("{0}:这一轮一个出口都没有 —— 用户会走进死胡同".format(where))
