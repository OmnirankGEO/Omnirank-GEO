"""小榜知识索引的**健康裁定**(包 C⑥ · 工单 §3.5 / §9.3)。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 修的是什么

`/api/xiaobang/health` 原来返回的是 `{"ok": True, "kb_chunks": …, "bm25_threshold": …}`
—— **`ok` 是个字面量常量,永远为真**。工单 §3.5 原话:
「结果是系统可以显示 health ok / lint ok,实际却在使用不同年代的知识。」

一个只报「有多少条」的 health,分不出下面三种状态:

* **stale**  —— 库是完整的,但它是**按旧规则/旧路由/旧注册表**建的
  (术语规则改了、页面删了、注册表升版了,而 release 没重建);
* **mixed**  —— manifest 说有 N 条,库里实际不是 N 条(有人绕过 release 单独写过);
* **incomplete** —— 压根没有 manifest(从没成功建过,或被清过)。

## 判定形态

**每一条不一致都必须能说出「谁跟谁不一致、各是什么值」** ——
只报 `ok: false` 而不说是哪一项,收到的人无从下手,和不报没区别。

🔴 `built_at` **只作记录**,判定里**不拿它跟当前时间比**。
   「多久算 stale」是一个写死的 cutoff,而写死 cutoff + 时间流逝 = 定时炸弹
   (本仓付过这笔费)。陈旧与否由**版本对账**判定,不由钟表判定。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping, Optional

_LOGGER = logging.getLogger("GEO-KbHealth")

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_MIXED = "mixed"
STATUS_INCOMPLETE = "incomplete"

#: 严重度序:后面的盖前面的(incomplete 最重 —— 连 manifest 都没有)
_SEVERITY = (STATUS_OK, STATUS_STALE, STATUS_MIXED, STATUS_INCOMPLETE)


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY.index(a) >= _SEVERITY.index(b) else b


def current_expectations() -> dict[str, Any]:
    """当前**代码**这一侧的真值 —— manifest 要跟它对账。

    每一项都从现役单一真源取,不手抄:
      · 术语规则版本 ← `services.kb_terminology_gate`
      · 注册表版本   ← `services.gap_operation_map`
      · 路由指纹     ← `declared_frontend_routes()`(机械解析 App.tsx)
    """
    from services.kb_terminology_gate import RULING_RULE_ID, RULING_VERSION
    from services.gap_operation_map import (
        OPERATION_MAP_VERSION,
        declared_frontend_routes,
    )

    routes = sorted(declared_frontend_routes())
    return {
        "terminology_rule_id": RULING_RULE_ID,
        "terminology_rule_version": RULING_VERSION,
        "operation_registry_version": OPERATION_MAP_VERSION,
        "route_manifest_hash": hashlib.sha256(
            json.dumps(routes, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "route_count": len(routes),
    }


def evaluate(
    manifest: Optional[Mapping[str, Any]],
    live_counts: Optional[Mapping[str, int]] = None,
    expectations: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """给出健康裁定。

    返回 ``{"status": ..., "ok": bool, "reasons": [...], "manifest_version": ...}``。
    `reasons` 里每一条都写清**谁跟谁不一致、各是什么值**。
    """
    exp = dict(expectations or current_expectations())
    reasons: list[str] = []

    if not manifest:
        return {
            "status": STATUS_INCOMPLETE,
            "ok": False,
            "reasons": ["没有 release manifest —— 索引从没成功建过,或 manifest 行被清掉了"],
            "manifest_version": None,
            "expectations": exp,
        }

    status = STATUS_OK

    # ── 版本对账(stale)────────────────────────────────────────────
    for key in ("terminology_rule_version", "operation_registry_version",
                "route_manifest_hash"):
        want = exp.get(key)
        got = manifest.get(key)
        if want is None:
            continue
        if got is None:
            reasons.append(
                "manifest 缺字段 {0}(当前代码是 {1})—— 它是更早版本建的".format(key, want))
            status = _worse(status, STATUS_STALE)
        elif got != want:
            shown_got, shown_want = str(got), str(want)
            if key == "route_manifest_hash":
                shown_got, shown_want = shown_got[:12] + "…", shown_want[:12] + "…"
                reasons.append(
                    "路由清单变了:manifest {0}(共 {1} 条)vs 当前 {2}(共 {3} 条)"
                    " —— 库里的跳转可能指向已经不存在的页".format(
                        shown_got, manifest.get("route_count"),
                        shown_want, exp.get("route_count")))
            else:
                reasons.append("{0} 不一致:manifest {1} vs 当前代码 {2}".format(
                    key, shown_got, shown_want))
            status = _worse(status, STATUS_STALE)

    # ── 条数对账(mixed)──────────────────────────────────────────
    declared = manifest.get("chunk_counts") or {}
    if live_counts is not None and isinstance(declared, Mapping):
        for source_type, declared_n in declared.items():
            live_n = live_counts.get(source_type)
            if live_n is None:
                continue
            if int(live_n) != int(declared_n):
                reasons.append(
                    "{0} 条数对不上:manifest 说 {1},库里实际 {2}"
                    " —— 有人绕过 release 单独写过".format(source_type, declared_n, live_n))
                status = _worse(status, STATUS_MIXED)

    return {
        "status": status,
        "ok": status == STATUS_OK,
        "reasons": reasons,
        "manifest_version": {
            "content_version": manifest.get("content_version"),
            "release_sha": manifest.get("release_sha"),
            "terminology_rule_id": manifest.get("terminology_rule_id"),
            "terminology_rule_version": manifest.get("terminology_rule_version"),
            "operation_registry_version": manifest.get("operation_registry_version"),
            "route_count": manifest.get("route_count"),
            # 只作记录 —— 判定里没有任何一条拿它跟当前时间比
            "built_at": manifest.get("built_at"),
        },
        "expectations": exp,
    }
