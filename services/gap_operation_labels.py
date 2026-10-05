"""缺口作战计划 · 人话字典 SSOT(P4 · B5)

字典本体是 `config/gap_operation_labels.json`(设计包 03 draft 转正)。
本模块是**服务端唯一读取口**,并提供两件事:

  1. 翻译:内部枚举 → {label, explanation, tone}
  2. 机械闸:`assert_no_internal_leak()` —— 出参里若还残留未翻译的内部枚举、
     模型/供应商名、成本词,当场抛错。

🔴 为什么闸要放在服务端出参而不是只放前端:
   前端锁只能证明"某个组件渲染时翻译了",证明不了"接口没把内部枚举送出去"。
   小榜是 SSE 流式接口、任务卡是 JSON、门户以后还要复用同一份快照 ——
   出口有三个,渲染层有 N 个。判据要打在**唯一的那个出口**上。
   (这正是 2026-08-08 记忆里那条「判据要打在接线上,不是函数上」的同一个坑。)
"""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "gap_operation_labels.json"

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def load_labels() -> dict[str, Any]:
    """读字典。进程内缓存一次;文件读不到是硬失败(字典缺失 = 界面必然泄漏内部枚举)。"""
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is None:
            with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
                _cache = json.load(fh)
    return _cache


def labels_version() -> str:
    return str(load_labels().get("version", "unknown"))


# ────────────────────────────────────────────────────────────────
# 翻译
# ────────────────────────────────────────────────────────────────

class UntranslatedCode(RuntimeError):
    """内部枚举没有字典条目。

    🔴 刻意做成异常而不是 fallback 到原文:fallback 到原文正是"界面直出内部枚举"
       那个 bug 本身,而且它是**静默的** —— 加一个新状态码忘了补字典,
       线上就会出现一个写着 `attack_absence` 的灰色徽章,没有人会收到通知。
    """


def translate_status(code: str) -> dict[str, str]:
    statuses = load_labels()["statuses"]
    entry = statuses.get(code)
    if entry is None:
        raise UntranslatedCode(
            f"状态码 {code!r} 在 {_CONFIG_PATH.name} 里没有条目。"
            f"新增状态码必须同步补字典(前后端共用这一份),否则界面会直出内部枚举。"
        )
    tone = entry.get("tone", "neutral")
    tone_meta = load_labels()["tones"].get(tone, {})
    return {
        "code": code,
        "label": entry["label"],
        "explanation": entry["explanation"],
        "tone": tone,
        "tone_label": tone_meta.get("label", ""),
        "icon": tone_meta.get("icon", "circle"),
    }


def translate_action(action_id: str) -> dict[str, Any]:
    actions = load_labels()["actions"]
    entry = actions.get(action_id)
    if entry is None:
        raise UntranslatedCode(f"动作 {action_id!r} 不在字典里(动作必须服务端签发)。")
    out = {
        "action_id": action_id,
        "label": entry["label"],
        "icon": entry.get("icon", ""),
        "confirmation": bool(entry.get("confirmation", False)),
        "enabled": bool(entry.get("enabled", True)),
    }
    if "implementation_phase" in entry:
        out["implementation_phase"] = entry["implementation_phase"]
    if "hint" in entry:
        out["hint"] = entry["hint"]
    return out


def known_status_codes() -> frozenset[str]:
    return frozenset(load_labels()["statuses"])


def known_action_ids() -> frozenset[str]:
    return frozenset(load_labels()["actions"])


def progress_labels() -> dict[str, str]:
    return dict(load_labels()["progress"])


# ────────────────────────────────────────────────────────────────
# 平台匿名化
# ────────────────────────────────────────────────────────────────

def anonymize_platforms(engines: Iterable[str]) -> dict[str, str]:
    """真实引擎名 → 「AI 平台一/二/三/四」。

    🔴 映射按**排序后的稳定顺序**建立,不按出现顺序 —— 否则同一个客户两次刷新
       看到的"AI 平台一"可能是不同引擎,依据抽屉就成了随机数。
    """
    display = load_labels()["platform_display"]["labels"]
    ordered = sorted({str(e) for e in engines if e})
    mapping: dict[str, str] = {}
    for idx, engine in enumerate(ordered):
        mapping[engine] = display[idx] if idx < len(display) else f"AI 平台{idx + 1}"
    return mapping


# ────────────────────────────────────────────────────────────────
# 机械闸:出参不得泄漏内部术语
# ────────────────────────────────────────────────────────────────

class InternalTermLeak(RuntimeError):
    """出参里检出内部术语。"""


# 供应商/模型名。取自现役配置里真实会出现的串,不是想象出来的清单。
_SUPPLIER_TERMS = (
    "deepseek", "qwen", "qwen3", "dashscope", "moonshot", "kimi", "doubao",
    "openrouter", "anthropic", "claude", "gpt-", "openai", "metaso", "tikhub",
    "豆包", "千问", "通义", "秘塔", "月之暗面",
)

# 成本/系数类。这些一旦出现在运营面,就是把内部采购口径捅出去了。
_COST_TERMS = (
    "procurement", "upstream_cost", "cost_basis", "multiplier_bps", "markup",
    "进货价", "采购成本", "上游成本", "倍率", "系数",
)

# 工程术语。合同 §7/§9 明令不出现在运营面。
_JARGON_TERMS = (
    "sov", "target_share", "prompt", "token", "confidence", "sql",
    "snapshot_hash", "authority_generation",
)

_ASCII_ENUM = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}$")

# 允许出现在出参里的机读字段名(它们是**键或标识**,不是给人读的文案)。
# 🔴 这份白名单是判据的一部分:每加一个,等于承认它不会被直接渲染。
_MACHINE_FIELD_KEYS = frozenset({
    "code", "action_id", "plan_item_id", "snapshot_id", "snapshot_version",
    "rule_version", "data_version", "tone", "icon", "capacity_source",
    "implementation_phase", "operation_map_version", "labels_version",
    "gap_code", "access_code", "allocation_code", "duplicate_of_item_id",
    "signed_target", "target_route", "operation_id", "help_target",
    "idempotency_key", "error_code", "next_generation_hint",
    # 这两个是 2026-08-08 本闸**真的抓到我自己**才加进来的,不是预先想好的:
    #   · display_status:容量状态码,人话在同级的 label/explanation 里
    #   · key:证据链四格的机读键(published/indexed/cited/customer_recommended),
    #     人话在同级 label 里
    # 🔴 加白名单的规矩:必须同时确认「同级已经有一个给人看的字段」,
    #    否则就是把真泄漏洗白了。这两条都满足。
    "display_status", "key",
    # 小榜 meta 里对同一个快照 id 用的字段名(设计包 04 §4 定的对外名),
    # 与 plan 出参的 snapshot_id 是同一个东西,只是键名不同。同样是机读 id,不渲染。
    "plan_snapshot_id",
    # 容量合同的两个机读值(P1 · article-capacity-v1):
    #   semantics = upper_bound_0_to_capacity —— 语义护栏常量,同级有 label/explanation
    #   contract_version —— 版本号
    # 🔴 白名单规矩不变:确认同级已有给人看的字段才放行。
    "semantics", "capacity_semantics", "contract_version",
})


def _iter_user_facing_strings(
    node: Any,
    key: str | None = None,
    path: str = "$",
    machine_keys: frozenset[str] = _MACHINE_FIELD_KEYS,
):
    """只遍历**会被渲染给人看**的字符串,跳过机读字段。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_user_facing_strings(
                v, key=str(k), path=f"{path}.{k}", machine_keys=machine_keys
            )
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _iter_user_facing_strings(
                v, key=key, path=f"{path}[{i}]", machine_keys=machine_keys
            )
    elif isinstance(node, str):
        if key in machine_keys:
            return
        yield path, node


def assert_no_internal_leak(
    payload: Any,
    *,
    where: str = "response",
    extra_machine_keys: Iterable[str] = (),
    machine_face_exempt: Callable[[str, str], bool] | None = None,
) -> None:
    """出参机械闸。检出即抛,不做"清洗后放行"。

    🔴 不清洗的理由:清洗会让 bug 变成静默的。真正要修的是"谁把它放进去的",
       而不是"在最后一道把它擦掉"。

    ``extra_machine_keys``:**按调用点**追加的机读键,不写进共享白名单。
    🔴 为什么不直接往 ``_MACHINE_FIELD_KEYS`` 里加:那份白名单是全仓共用的,
       往里加一个像 ``state`` 这样的通用键,等于给**所有**出参开了同一个洞 ——
       别人的 payload 里真有个未翻译的 ``state`` 枚举也会被一起放行。
       白名单要跟着调用点走,加的人才为它负责。

    ``machine_face_exempt(path, value)``:**字段级机器面豁免**钩子。
    🔴 它只作用在「未翻译的内部枚举」这一条规则上 —— 供应商名 / 成本术语 /
       工程术语三条**照旧全量生效**。理由:豁免要豁的是「这一格本来就是机器契约、
       没有人会把它当文案读」,不是「这一格随便放什么都行」。
       往 bind 里塞一个供应商名仍然必须响亮判红。
    🔴 也不是按 key 名豁免:``extra_machine_keys`` 那条路是 key 粒度的,
       而 ``bind`` 的枚举是**列表值**(它们继承父键 ``bind``),
       key 粒度豁免会把任何叫 ``bind`` 的东西整片放行。
       本钩子拿到 ``(path, value)`` 两样,调用方可以按 (端点, 字段路径, 取值集合)
       三重收窄 —— 见 :mod:`services.xiaobang_facade_dlp` 的豁免登记表。
    🔴 默认 ``None`` = 与本函数历史行为**逐字节一致**(既有调用点一个都不受影响)。
    """
    machine_keys = _MACHINE_FIELD_KEYS | frozenset(str(k) for k in extra_machine_keys)
    problems: list[str] = []
    for path, text in _iter_user_facing_strings(payload, machine_keys=machine_keys):
        low = text.lower()
        for term in _SUPPLIER_TERMS:
            if term in low:
                problems.append(f"{path}: 供应商/模型名 {term!r} → {text[:60]!r}")
        for term in _COST_TERMS:
            if term in low:
                problems.append(f"{path}: 成本/系数术语 {term!r} → {text[:60]!r}")
        for term in _JARGON_TERMS:
            if re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9])", low):
                problems.append(f"{path}: 工程术语 {term!r} → {text[:60]!r}")
        stripped = text.strip()
        if _ASCII_ENUM.match(stripped) and not (
            machine_face_exempt is not None and machine_face_exempt(path, stripped)
        ):
            problems.append(f"{path}: 未翻译的内部枚举 {text!r}")
    if problems:
        raise InternalTermLeak(
            f"[{where}] 出参检出 {len(problems)} 处内部术语泄漏:\n  - "
            + "\n  - ".join(problems[:20])
        )


@lru_cache(maxsize=1)
def leak_scanner_term_count() -> int:
    """给锁用:证明术语表非空(反向对照 —— 空表扫描器恒绿)。"""
    return len(_SUPPLIER_TERMS) + len(_COST_TERMS) + len(_JARGON_TERMS)
