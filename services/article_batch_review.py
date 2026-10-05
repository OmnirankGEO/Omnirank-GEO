"""P3a · 批量审核与批量"忽略普通提示并继续"的服务层(工单 §6)。

放在 service 层而不是直接写进 server.py 的理由:这一层的三条硬性质
(逐项隔离 / 一键通过的 H0 护栏 / 零扣费)必须能被**不起 FastAPI、不发 HTTP**
的用例直接调到 —— 否则锁就只能去断源码串,而源码串锁是双向脆的
(换皮绕过 + 重构误伤,本仓 07-30 已判过)。

三条硬性质:

1. 🔴 **逐项隔离**(工单 §6 "失败项单独显示可单独重试;成功项不回滚、不被后续失败覆盖")
   每一项自带连接与事务,循环里 catch 每一项的异常。第 7 项炸了,前 6 项**已经 commit
   落库**,不会被回滚,也不会因为第 7 项的异常而在返回体里消失。
   反面(本模块刻意不做的写法):整批共用一个 cursor + 最后一次 commit —— 那样
   任意一项异常会把整批 rollback,"成功项不回滚"当场失效。

2. 🔴 **一键通过的 H0 护栏**(工单 §6 红线 + SSOT §11.1)
   `assert_bulk_pass_allowed` 在每一项**动手之前**重算发布资格,只有
   `publication_h0_state == 'clear'` 才放行。这一条把法律硬门 / 平台档 /
   完整性·授权·运营裁决桶(operator_hard:跨租户、资金、hash lineage、人工拒稿、
   对象不存在)全部挡在批量之外 —— 批量的权限**严格窄于**单篇端点,不是等于。
   单篇 `/api/topics/{id}/mark-reviewed` 的既有行为一字未改(它是 P1 的地盘)。

3. 🔴 **零扣费**:本模块不 import、不调用任何计费函数。
   批量机审走 `refresh_article_review`(纯规则,无 LLM、无 provider);
   批量修复走已有的免费端点(repair-finding / auto-repair),不在本模块内。

另:批量机审**跳过**已有人工签发的文章。`refresh_article_review` 会把
`article_human_review_status` 四字段清空(这是单篇编辑路径的正确行为:正文改了,
签发自然失效)。但"审核全部已完成"如果照做,会一次抹掉生产上全部人工签发
—— 那是不可逆的数据破坏,不是刷新。故这类文章返回 status='skipped' 并说明原因,
由用户自己决定要不要单篇重审。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

#: 单次请求处理的条目上限。批量机审是 CPU 型规则评估(每篇要重跑 review_article),
#: 不设上限会让一个请求长时间占住线程池里的一格。前端按这个值分片。
MAX_BATCH_ITEMS: int = 100

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class BatchItemRejected(Exception):
    """这一项被**主动拒绝**(护栏命中 / 前置条件不满足),不是运行时故障。

    与普通异常分开是有意义的:被拒绝的项 `retryable=False` —— 原样重试
    一定还是同样的结果,把它渲染成"可重试"是骗用户点第二次。
    """

    def __init__(self, code: str, message: str, *, status: str = STATUS_SKIPPED):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def normalize_ids(raw: Iterable[Any]) -> list[int]:
    """去重(保序)+ 丢非法值 + 截断到上限。

    保序是为了让返回体的顺序和用户在界面上勾选的顺序一致 —— set() 会打乱,
    用户对不上号就会以为"漏了几篇"。
    """
    seen: set[int] = set()
    out: list[int] = []
    for item in raw or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
        if len(out) >= MAX_BATCH_ITEMS:
            break
    return out


def assert_bulk_pass_allowed(article_id: int, *, cursor=None) -> dict[str, Any]:
    """🔴 一键通过的护栏。返回该文章当前的判定结果;不 clear 就抛 BatchItemRejected。

    刻意**重算**而不是信前端传来的状态:前端那份可能是几分钟前 loadProjectDetail
    拿的,期间文章可能被改过(改完 lineage 就对不上了)。批量动作一次影响几十篇,
    拿旧快照做放行判断的代价比单篇大得多。
    """
    from services.article_review_gate import (
        H0_CLEAR,
        evaluate_publication_eligibility,
    )

    verdict = evaluate_publication_eligibility(article_id, cursor=cursor)
    h0 = verdict.get("publication_h0_state")
    if h0 != H0_CLEAR:
        raise BatchItemRejected(
            "h0_not_clear",
            str(verdict.get("message") or "这篇还有对外发布硬门,批量通过不能覆盖,请单篇处理。"),
        )
    return verdict


def review_one_article(article_id: int) -> dict[str, Any]:
    """跑一次机审并重评发布资格。**自带连接与事务**(逐项隔离的落点)。

    已有人工签发的文章直接 skipped:见模块 docstring 的第四段。
    """
    from db.connection import get_connection
    from services.article_review_gate import (
        evaluate_publication_eligibility,
        refresh_article_review,
    )

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT article_human_review_status FROM articles WHERE id = %s",
            (article_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise BatchItemRejected("article_not_found", "找不到这篇文章,可能已被删除。")
        if str((row or {}).get("article_human_review_status") or "") == "approved":
            raise BatchItemRejected(
                "human_approved_skipped",
                "这篇已有人工签发,重跑机审会让签发失效;需要重审请单篇操作。",
            )
        refresh_article_review(article_id, cursor=cursor)
        verdict = evaluate_publication_eligibility(article_id, cursor=cursor)
        conn.commit()
        return {
            "review_state": verdict.get("review_state"),
            "advisory_state": verdict.get("advisory_state"),
            "advisory_open_count": verdict.get("advisory_open_count"),
            "publication_h0_state": verdict.get("publication_h0_state"),
            "publication_eligible": bool(verdict.get("eligible")),
            "publication_eligibility_reason": verdict.get("reason"),
            "publication_eligibility_message": verdict.get("message"),
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_batch(
    item_ids: Iterable[Any],
    worker: Callable[[int], dict[str, Any]],
    *,
    access_check: Callable[[int], None] | None = None,
    id_field: str = "article_id",
) -> dict[str, Any]:
    """逐项隔离地跑一批。**任何一项的异常都不许中断整批,也不许回滚别项。**

    `access_check` 单独传进来(而不是写死在 worker 里)是为了让鉴权也走逐项路径:
    一批里混进一篇越权的文章,应当是那一篇 403、其余照常,而不是整批 403。
    """
    ids = normalize_ids(item_ids)
    results: list[dict[str, Any]] = []
    success_count = skipped_count = failed_count = 0
    for item_id in ids:
        try:
            if access_check is not None:
                access_check(item_id)
            payload = worker(item_id)
            results.append({id_field: item_id, "status": STATUS_OK, "retryable": False, **(payload or {})})
            success_count += 1
        except BatchItemRejected as rejected:
            results.append({
                id_field: item_id,
                "status": rejected.status,
                "error_code": rejected.code,
                "message": rejected.message,
                # 主动拒绝 → 原样重试结果不变,不给"重试"按钮(见 BatchItemRejected docstring)
                "retryable": False,
            })
            if rejected.status == STATUS_FAILED:
                failed_count += 1
            else:
                skipped_count += 1
        except Exception as exc:  # noqa: BLE001 —— 逐项隔离要求在这里吃掉一切
            results.append({
                id_field: item_id,
                "status": STATUS_FAILED,
                "error_code": _error_code(exc),
                "message": _human_message(exc),
                "retryable": _retryable(exc),
            })
            failed_count += 1
    return {
        # 真实计数,不做百分比(工单 §6 "用真实 done/total,禁伪造百分比")
        "total": len(ids),
        "done": len(results),
        "success_count": success_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "results": results,
        # 本批全程零扣费:本模块与两个免费端点都不含任何计费调用(锁 4 用 AST 钉)
        "charged": False,
    }


def _retryable(exc: Exception) -> bool:
    """4xx 是"你这么请求本身就不对"(越权 / 不存在 / 状态冲突),原样重试还是同样结果。

    只有 5xx 与非 HTTP 异常(数据库抖动、超时之类)才值得给"重试这一篇"按钮。
    把 403 渲染成可重试 = 让用户对着一个永远不会成功的按钮点第二次。
    """
    status = getattr(exc, "status_code", None)
    try:
        return not (status is not None and 400 <= int(status) < 500)
    except (TypeError, ValueError):
        return True


def _error_code(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return f"http_{status}"
    return type(exc).__name__


def _human_message(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("reason") or detail)
    if detail:
        return str(detail)
    return str(exc) or "处理失败,可以单独重试这一篇。"
