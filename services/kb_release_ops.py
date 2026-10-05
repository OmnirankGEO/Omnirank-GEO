"""小榜知识 release 的运维收口(WO-A ①② · 2026-08-20)。

## ① 部署链那一步:接线**在**,判成功的方式是坏的

工单写的现状是「``kb_indexer`` 不在 ``start.sh`` / ``prestart`` / ``go.sh`` 任何
自动步骤里」。这三个文件确实零命中 —— 但**部署链不是这三个文件**:
``go.sh`` 起飞时跑的是 release 工作树里的 ``scripts/deploy-blue-green.sh``,
而那个脚本从 2026-05-25 起就有一段 ``[Post-Deploy] 重建小榜知识库索引``。
前几轮报告漏掉它,是因为分母是手写的三个文件名 —— 手写分母漏掉的那一项
不会让任何判据变红。

接线在,那为什么每次还要人工重建?因为**判成功的方式**是坏的:

```bash
KB_LOG=$(docker exec ... python -m tools.xiaobang_kb_indexer ... 2>&1 | tail -8 || true)
if echo "$KB_LOG" | grep -q "索引完成"; then ...
```

* ``|| true`` 把**退出码**整个吞掉 —— 非零与零在这里没有区别;
* 判成功改看 ``tail -8`` 里有没有「索引完成」。而 indexer 收尾会按
  ``count_chunks()`` 逐类型打印,生产上有 doc/faq/preset + 5 类 sys_*
  ⇒「索引完成」那一行**排在末 8 行之外**,被 ``tail -8`` 切掉
  ⇒ 重建成功也报「⚠️ 重建失败」。人于是不再信它,改成手工跑。
* 失败只落在部署日志里 —— 那份日志下一次切流就被冲掉,没有任何告警面。

## 这个模块做什么

把「先 seed 同步 → 后重建索引」做成**一个可被判据打到的步骤**,
两种 fail-closed 形态分别落日志 + 落 ``ai_ops_alerts`` 告警:

* :data:`SHAPE_RAISED` —— 被调方**抛异常**(术语门拒绝 / 取不到 release SHA /
  库连不上)。旧 release 原样留着,这是 fail-closed 的正确方向,但必须有人知道。
* :data:`SHAPE_NONZERO` —— 被调方**返回非零**(seed 同步完一条已发布 FAQ 都没有 /
  重建「成功」却一行都没插)。「空 release」在退出码上跟成功一模一样,
  正是本仓反复付费的那种假绿,所以它在这里是**失败**。

部署脚本侧再分一层:步骤自己跑起来了(退出码 20/21 ⇒ 容器内已落告警)
vs 步骤压根没跑起来(``docker exec`` 125/126/127 等 ⇒ 容器内没人来得及告警,
由脚本用 ``--report-failure`` 补一条)。

## ② 运行时重建的状态位

``api/faq_api.py`` 的 FAQ CRUD 钩子是 fire-and-forget:接口返 200 不代表索引更了。
:func:`track_reindex` 给每次重建套上「进行中 / 失败」两个状态位:

* **失败**走 ``ai_ops_alerts``(DB · 跨 worker · 跨进程)—— 部署期那条
  ``python -m services.kb_release_ops`` 是**另一个进程**,它的失败也是这样被前台看见的;
* **进行中**是进程内的(重建是秒级的进程内后台任务,没有跨进程的「进行中」可言)。
  蓝绿重叠期两实例各看各的 —— 这一点如实写在这里,不假装它是全局真值。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional

logger = logging.getLogger("kb-release-ops")

#: 部署期步骤失败(①)。
KB_RELEASE_ALERT_RULE = "kb_release_step_failed"
#: 运行时重建失败(②)。
KB_REINDEX_ALERT_RULE = "kb_reindex_failed"
#: 状态位读这两条 —— 部署期失败与运行时失败对用户是同一件事:索引可能是旧的。
KB_STATUS_ALERT_RULES: tuple[str, ...] = (KB_RELEASE_ALERT_RULE, KB_REINDEX_ALERT_RULE)

#: 两种 fail-closed 形态。写进告警 payload,收到的人不用猜是哪一种。
SHAPE_RAISED = "raised"
SHAPE_NONZERO = "nonzero_return"
#: 第三种只可能由**部署脚本**报:步骤压根没启动(docker exec 自己失败)。
SHAPE_LAUNCH_FAILED = "launch_failed"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_SEED_STAGE = 20
EXIT_REBUILD_STAGE = 21

STAGE_SEED = "seed_sync"
STAGE_REBUILD = "rebuild"


# ══════════════════════════════════════════════════════════════════════════
# 告警产线(与图文链同一份实现 · 见 services/ai_ops_alerts.py)
# ══════════════════════════════════════════════════════════════════════════

def _alert(*, rule_key: str, title: str, detail: str, fingerprint: str,
           payload: dict, severity: str = "critical") -> bool:
    from services.ai_ops_alerts import raise_wiring_alert

    return raise_wiring_alert(rule_key=rule_key, title=title, detail=detail,
                              fingerprint=fingerprint, payload=payload,
                              severity=severity)


def _resolve(rule_key: str, fingerprint: str) -> int:
    """重建成功 ⇒ 把同一指纹的旧告警收掉。

    不收的话,一次瞬时失败会让状态位**永久**停在「失败」,下一个人只会把它无视掉。
    """
    try:
        from db.ai_ops_db import resolve_alerts

        return int(resolve_alerts(rule_key, fingerprint=fingerprint) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kb-release-ops] 告警恢复失败(不影响主链)%s/%s: %s",
                       rule_key, fingerprint, exc)
        return 0


# ══════════════════════════════════════════════════════════════════════════
# ① 部署链一步:先 seed 同步 · 后重建索引
# ══════════════════════════════════════════════════════════════════════════

def _published_faq_rows() -> int:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM faq_items WHERE is_published = TRUE")
        return int(cur.fetchone()["n"])
    finally:
        conn.close()


def default_seed_sync() -> tuple[int, dict]:
    """第一步:把 ``db/faq_db._INITIAL_SEED`` 同步进 ``faq_items``。

    🔴 顺序不能反:``build_faq_chunks()`` 是从 ``faq_items`` 表 SELECT 的,
       seed 没同步就重建 = 拿旧答案建新 release。

    🔴 为什么不依赖「应用启动时会跑 ``init_faq_tables()``」:那一跳在
       ``server.py`` 里被 ``try/except Exception`` 包着,失败只记 warning。
       依赖一个允许静默失败的前置 = 依赖不上。这里显式再跑一次(幂等)。
    """
    from db.faq_db import init_faq_tables

    init_faq_tables()
    published = _published_faq_rows()
    if published <= 0:
        # 「一条已发布 FAQ 都没有」不是干净,是分母塌了 —— 接着重建会产出空 faq release。
        return 1, {"published_faq_rows": 0}
    return 0, {"published_faq_rows": published}


def default_rebuild(release_sha: Optional[str] = None) -> tuple[int, dict]:
    """第二步:事务化重建 doc + faq + preset 三类 chunk。"""
    from tools.xiaobang_kb_indexer import rebuild_release

    result = rebuild_release(release_sha)
    inserted = int(result.get("inserted") or 0)
    if inserted <= 0:
        # 空 release 与成功在退出码上同形。这里把它判失败,否则「重建过了」
        # 会变成一句谎话:用户当场问不出答案,而部署日志一片绿。
        return 1, {"inserted": inserted, "manifest": result.get("manifest")}
    return 0, {
        "deleted": int(result.get("deleted") or 0),
        "inserted": inserted,
        "manifest": result.get("manifest"),
    }


def _normalize_outcome(outcome: Any) -> tuple[int, dict]:
    """把被调方的返回值折成 ``(退出码, 明细)``。

    容三种形态:``(code, detail)`` / 裸 int / dict(``ok=False`` 视为非零)。
    ``None`` = 什么都没返回 = 成功(``init_faq_tables`` 那种)。
    """
    if outcome is None:
        return 0, {}
    if isinstance(outcome, tuple) and len(outcome) == 2:
        code, detail = outcome
        return int(code or 0), dict(detail or {})
    if isinstance(outcome, bool):
        return (0 if outcome else 1), {}
    if isinstance(outcome, int):
        return int(outcome), {}
    if isinstance(outcome, dict):
        return (1 if outcome.get("ok") is False else 0), dict(outcome)
    return 0, {"outcome": repr(outcome)[:200]}


def _fail(*, stage: str, shape: str, exit_code: int, detail: str,
          release_sha: Optional[str], extra: dict) -> dict:
    """一条失败 = 一条 error 日志 + 一条告警。两样都要,少一样就是静默。"""
    logger.error("[kb-release-ops] ❌ %s 失败(形态=%s · 旧 release 保持不变): %s",
                 stage, shape, detail)
    alerted = _alert(
        rule_key=KB_RELEASE_ALERT_RULE,
        title="小榜知识 release 部署步失败({0}/{1})".format(stage, shape),
        detail=detail,
        fingerprint=stage,
        payload={"stage": stage, "shape": shape, "release_sha": release_sha or "",
                 "exit_code": exit_code, **extra},
    )
    if not alerted:
        logger.error("[kb-release-ops] 🔴 告警也没写进去 —— 这次失败只剩下这条日志")
    return {"ok": False, "stage": stage, "shape": shape, "exit_code": exit_code,
            "detail": detail, "alerted": alerted}


def run_kb_release_step(
    *,
    release_sha: Optional[str] = None,
    seed_sync: Optional[Callable[[], Any]] = None,
    rebuild: Optional[Callable[[], Any]] = None,
) -> dict:
    """部署链那一步:**先 seed 同步、后重建索引**,两种失败形态分别落日志 + 告警。

    返回 ``{"ok": bool, "exit_code": int, ...}``;**本函数不抛** ——
    它是部署链的末端,抛出去只会变成脚本里的一个裸非零码,没人知道是哪一段。
    """
    stages: tuple[tuple[str, Callable[[], Any], int], ...] = (
        (STAGE_SEED, seed_sync or default_seed_sync, EXIT_SEED_STAGE),
        (STAGE_REBUILD, rebuild or (lambda: default_rebuild(release_sha)), EXIT_REBUILD_STAGE),
    )
    report: dict[str, Any] = {"ok": True, "exit_code": EXIT_OK, "stages": {},
                              "release_sha": release_sha or ""}
    for stage, fn, exit_code in stages:
        try:
            outcome = fn()
        except Exception as exc:  # noqa: BLE001
            return {**report, **_fail(
                stage=stage, shape=SHAPE_RAISED, exit_code=exit_code,
                detail="{0}: {1}".format(type(exc).__name__, exc),
                release_sha=release_sha, extra={},
            )}
        code, detail = _normalize_outcome(outcome)
        if code:
            return {**report, **_fail(
                stage=stage, shape=SHAPE_NONZERO, exit_code=exit_code,
                detail="被调方返回非零({0}) · 明细={1}".format(
                    code, json.dumps(detail, ensure_ascii=False, default=str)[:400]),
                release_sha=release_sha, extra={"returned_code": code},
            )}
        report["stages"][stage] = detail
        logger.info("[kb-release-ops] ✅ %s 完成:%s", stage,
                    json.dumps(detail, ensure_ascii=False, default=str)[:400])
    # 走到这里说明这一版 release 是完整的 —— 把上一次的失败告警收掉。
    report["resolved_alerts"] = (_resolve(KB_RELEASE_ALERT_RULE, STAGE_SEED)
                                 + _resolve(KB_RELEASE_ALERT_RULE, STAGE_REBUILD))
    return report


def report_external_failure(*, shape: str, detail: str,
                            release_sha: Optional[str] = None) -> bool:
    """给**部署脚本**用:步骤压根没跑起来时补一条告警。

    容器内那条 ``python -m services.kb_release_ops`` 没启动成功的话,
    容器内没有任何人来得及告警 —— 失败就只剩部署日志,而那份日志下一次切流就没了。
    """
    logger.error("[kb-release-ops] ❌ 部署步未能启动(形态=%s): %s", shape, detail)
    return _alert(
        rule_key=KB_RELEASE_ALERT_RULE,
        title="小榜知识 release 部署步未能启动({0})".format(shape),
        detail=detail,
        fingerprint="launch",
        payload={"stage": "launch", "shape": shape, "release_sha": release_sha or ""},
    )


# ══════════════════════════════════════════════════════════════════════════
# ② 运行时重建的状态位
# ══════════════════════════════════════════════════════════════════════════

_RUNNING_LOCK = threading.Lock()
_RUNNING: dict[str, dict] = {}


def _enter_running(scope: str) -> None:
    with _RUNNING_LOCK:
        entry = _RUNNING.get(scope)
        if entry:
            entry["count"] += 1
        else:
            _RUNNING[scope] = {"count": 1, "started_at": time.time()}


def _leave_running(scope: str) -> None:
    with _RUNNING_LOCK:
        entry = _RUNNING.get(scope)
        if not entry:
            return
        entry["count"] -= 1
        if entry["count"] <= 0:
            _RUNNING.pop(scope, None)


def _running_snapshot() -> list[dict]:
    now = time.time()
    with _RUNNING_LOCK:
        return [
            {"scope": scope, "count": entry["count"],
             "elapsed_seconds": round(now - entry["started_at"], 1)}
            for scope, entry in sorted(_RUNNING.items())
        ]


@contextmanager
def track_reindex(scope: str):
    """给一次索引重建套上状态位。失败 ⇒ 日志 + 告警;成功 ⇒ 收掉旧告警。

    **不吞异常**:调用方(admin 手动重建)该拿到 500,fire-and-forget 那条
    该继续走它自己的重试。这里只负责让失败**在前台可见**。
    """
    _enter_running(scope)
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        logger.error("[kb-release-ops] ❌ 索引重建失败 scope=%s: %s", scope, exc)
        _alert(
            rule_key=KB_REINDEX_ALERT_RULE,
            title="小榜索引重建失败(scope={0})".format(scope),
            detail="{0}: {1}".format(type(exc).__name__, exc),
            fingerprint=scope,
            payload={"scope": scope, "shape": SHAPE_RAISED},
        )
        raise
    else:
        _resolve(KB_REINDEX_ALERT_RULE, scope)
    finally:
        _leave_running(scope)


def _firing_kb_alerts() -> tuple[list[dict], bool]:
    """读 ``ai_ops_alerts`` 里与索引有关的 firing 行。返回 ``(行, 读到了没)``。

    读不到时**不谎报干净** —— 状态位会带 ``alerts_readable=false``,
    前台据此显示「状态未知」而不是「一切正常」。
    """
    try:
        from db.ai_ops_db import list_alerts

        rows = [
            {"rule_key": r.get("rule_key"), "fingerprint": r.get("fingerprint"),
             "title": r.get("title"), "detail": (r.get("detail") or "")[:400],
             "severity": r.get("severity"),
             "age_seconds": int(r.get("first_seen_age_seconds") or 0),
             "payload": r.get("payload") or {}}
            for r in list_alerts(status="firing", limit=200)
            if r.get("rule_key") in KB_STATUS_ALERT_RULES
        ]
        return rows, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kb-release-ops] 告警读取失败,状态位按「未知」返回: %s", exc)
        return [], False


def reindex_status() -> dict:
    """前台状态位:``running`` / ``failed`` / ``unknown`` / ``idle``。"""
    running = _running_snapshot()
    failures, readable = _firing_kb_alerts()
    if running:
        state = "running"
    elif failures:
        state = "failed"
    elif not readable:
        state = "unknown"
    else:
        state = "idle"
    return {
        "state": state,
        "running": running,
        "failures": failures,
        "alerts_readable": readable,
        "checked_at": time.time(),
    }


# ══════════════════════════════════════════════════════════════════════════
# CLI(部署脚本调这个)
# ══════════════════════════════════════════════════════════════════════════

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="小榜知识 release 部署步:先 seed 同步 · 后重建索引")
    parser.add_argument("--release-sha", help="本次发布对应的 40 位 git SHA")
    parser.add_argument("--report-failure", action="store_true",
                        help="不跑步骤,只补一条「步骤未能启动」告警(部署脚本兜底用)")
    parser.add_argument("--shape", default=SHAPE_LAUNCH_FAILED, help="配合 --report-failure")
    parser.add_argument("--detail", default="", help="配合 --report-failure")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    if args.report_failure:
        ok = report_external_failure(shape=args.shape, detail=args.detail or "(无明细)",
                                     release_sha=args.release_sha)
        print(json.dumps({"reported": ok}, ensure_ascii=False))
        return EXIT_OK if ok else EXIT_USAGE

    result = run_kb_release_step(release_sha=args.release_sha)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return int(result.get("exit_code") or EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
