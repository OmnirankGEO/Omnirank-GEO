"""P3b · 「一键修复全部系统问题」的可恢复批任务(工单 §6 后半)。

工单要求(逐条对应到本模块):
  · 事件 queued/reviewing/locating/repairing/validating/partial/completed/failed/heartbeat;
  · 每条带 job_id/article_id/finding_id/done/total/success_count/failed_count/message;
  · **真实 done/total,禁伪造百分比**;
  · 同 job_id 可重连;
  · 相同幂等键不重复修复 / 不重复调 provider / 不重复扣费;
  · 优先复用现有 job/attempt/event/outbox 表。

────────────────────────────────────────────────────────────────────────────
🔴 三个设计判断,连同它们的代价一起写在这里(不藏在交付单里)
────────────────────────────────────────────────────────────────────────────

**1. 工作单元是 finding,不是文章。**
工单规定每条事件都带 `finding_id` —— 那就说明单元是 finding。于是 locating /
repairing / validating 这几个阶段名是**真的**有对应动作(定位命中段 → 调模型改这一段
→ 重算三路 findings 并刷新机审),而不是围着一个不透明的大调用摆三个假阶段。
围着单个调用发假阶段就是**伪造进度**,工单同一段里刚禁过。

**2. 零新建表 —— 这是"优先复用现有表"的最强形式。**
先看了能不能复用 `ai_ops_tasks` / `ai_ops_task_events`(已在 migration_manifest 里、
生产有):**不复用**,两条理由:
  · 它 `kind` 上有 `CHECK (kind IN ('diagnose','fix','report','ssh_action','code_review'))`。
    往 CHECK 里加允许值**不是 additive** —— 本仓 2026-07-30 刚为这件事付过代价
    (两槽同时失去可启动性,而公网照常 200,零信号 P0)。
  · 语义上它是**运维侧 admin 任务总线**,把面向代理用户的文章修复塞进去,
    等于把两条 RBAC 边界不同的东西混在一张表里。

真正需要持久化的东西**本来就已经持久化了**:
  · 每条 finding 的免费额度与占位 → `articles.quality_warning.span_repair_quota`
    (`services/span_level_repair.reserve_free_repair`,**在调模型之前**占位);
  · 每篇的一键修复轮数 → `articles.quality_warning.auto_repair.manual_rounds`;
  · 修复结果本身 → `articles.content` + 刷新后的 `article_review_status`。
所以"不重复修复 / 不重复调 provider / 不重复扣费"这三条的保证**在库里**,
不在本模块的内存状态里。本模块的内存状态只负责**把事件流接回去**。

**3. 因此:结果是持久的,事件流不是。** 代价如实说:
  · 进程重启 → 内存里的 job 注册表没了,流会断;
  · 客户端重连(同 job_id + 同一份 article_ids)→ 服务端重新驱动,
    已经修过的 finding 因为**库里**的额度/轮数闸而直接跳过,不会再调一次模型、
    不会再改一次正文;
  · 生产当前 `WORKERS=1`,同进程重连是常态路径;将来 WORKERS>1 时重连可能落到
    另一个 worker,退化成上面这条"重新驱动 + 库级幂等"的路径 —— **仍然安全**,
    只是会重放一遍已完成条目的事件。
  这个取舍写在这里,是为了让复审能直接判"要不要为它上一张表",而不是等踩到才发现。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

#: 工单 §6 点名的九种事件。任何不在这个集合里的 kind 都不许发出去 ——
#: 前端按 kind 分支,悄悄多出一种它不认识的,表现是"卡住不动"。
JOB_EVENT_KINDS: frozenset[str] = frozenset({
    "queued", "reviewing", "locating", "repairing", "validating",
    "partial", "completed", "failed", "heartbeat",
})

#: 事件里必须齐的字段(工单原文列的八个)。缺字段 = 前端要到处判 undefined。
JOB_EVENT_FIELDS: tuple[str, ...] = (
    "job_id", "article_id", "finding_id", "done", "total",
    "success_count", "failed_count", "message",
)

#: 单个任务最多处理多少篇。与 P3a 的 MAX_BATCH_ITEMS 一致。
MAX_JOB_ARTICLES: int = 100

#: 注册表里最多留多少个任务(超出按完成时间淘汰最老的已完成任务)。
#: 不设上限 = 内存里慢慢攒完 job,长跑进程会被撑爆。
MAX_REGISTRY_JOBS: int = 64

#: 没有新事件时多久发一次 heartbeat(秒)。CDN / 反代会掐掉长时间无字节的连接。
HEARTBEAT_INTERVAL_SEC: float = 10.0


class JobOwnershipError(PermissionError):
    """重连时 job_id 对得上、但**不是你的任务**。

    不给"任务不存在"的模糊回复:那样两种情况混在一起,反而更难排查;
    但也不回任何任务内容 —— 只回一个 403。
    """


@dataclass
class RepairJob:
    job_id: str
    owner_user_id: Any
    article_ids: list[int]
    events: list[dict[str, Any]] = field(default_factory=list)
    #: 已经**尝试过**的幂等键(article_id, finding_fingerprint)。
    #: 本进程内的去重;跨进程那一层由库里的额度闸兜底(见模块 docstring 第 2 条)。
    attempted_keys: set[tuple[int, str]] = field(default_factory=set)
    done: int = 0
    success_count: int = 0
    failed_count: int = 0
    finished: bool = False
    finished_at: float = 0.0
    task: Any = None
    _signal: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def total(self) -> int:
        """**真实** total:提交时就确定的篇数,全程不变。

        刻意不用"findings 总数"当 total —— 那个数要边跑边发现,会让进度条往回走;
        也刻意不发百分比(工单 §6 明禁)。
        """
        return len(self.article_ids)

    # ------------------------------------------------------------------ 事件

    def emit(
        self,
        kind: str,
        *,
        message: str,
        article_id: int | None = None,
        finding_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        if kind not in JOB_EVENT_KINDS:
            raise ValueError(f"未登记的事件类型: {kind}")
        event = {
            "seq": len(self.events) + 1,
            "kind": kind,
            "job_id": self.job_id,
            "article_id": article_id,
            "finding_id": finding_id,
            # 🔴 真实计数:done 是已经处理完的篇数,total 是提交的篇数。
            # 没有 percent / progress 之类由这两个数推出来的"看着在动"的字段。
            "done": self.done,
            "total": self.total,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "message": message,
            **extra,
        }
        self.events.append(event)
        self._signal.set()
        return event

    def events_since(self, last_seq: int) -> list[dict[str, Any]]:
        """重连回放:把 last_seq 之后的事件补齐。

        用 seq 而不是时间戳:时间戳会撞(同一毫秒发两条),seq 单调且唯一。
        """
        if last_seq <= 0:
            return list(self.events)
        return [e for e in self.events if e["seq"] > last_seq]

    async def wait_for_change(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._signal.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        finally:
            self._signal.clear()

    def mark_finished(self) -> None:
        self.finished = True
        self.finished_at = time.time()
        self._signal.set()


_REGISTRY: dict[str, RepairJob] = {}


def _evict_if_needed() -> None:
    if len(_REGISTRY) <= MAX_REGISTRY_JOBS:
        return
    finished = sorted(
        (j for j in _REGISTRY.values() if j.finished),
        key=lambda j: j.finished_at,
    )
    for job in finished:
        if len(_REGISTRY) <= MAX_REGISTRY_JOBS:
            break
        _REGISTRY.pop(job.job_id, None)


def new_job_id() -> str:
    return f"repair-{uuid.uuid4().hex[:16]}"


def get_job(job_id: str) -> RepairJob | None:
    return _REGISTRY.get(job_id)


def attach_or_create(
    *, job_id: str | None, article_ids: list[int], owner_user_id: Any,
) -> tuple[RepairJob, bool]:
    """重连拿老任务 / 新建。返回 (job, created)。

    🔴 归属校验:job_id 是可猜的字符串,拿到别人的 job_id 就能看别人文章的修复流
    (标题、命中的违规表述都在 message 里)。所以重连必须核 owner。
    """
    if job_id:
        existing = _REGISTRY.get(job_id)
        if existing is not None:
            if existing.owner_user_id != owner_user_id:
                raise JobOwnershipError(job_id)
            return existing, False
    job = RepairJob(
        job_id=job_id or new_job_id(),
        owner_user_id=owner_user_id,
        article_ids=list(article_ids),
    )
    _REGISTRY[job.job_id] = job
    _evict_if_needed()
    return job, True


def reset_registry_for_tests() -> None:
    """只给测试用:清空进程级注册表,避免用例之间互相看到对方的 job。"""
    _REGISTRY.clear()


# ══════════════════════════════════════════════════════════════════════════
# 驱动
# ══════════════════════════════════════════════════════════════════════════

def locate_repairable_findings(
    quality_warning: Any, *, industry: str = "", title: str = "",
) -> list[dict[str, Any]]:
    """定位这篇里**可以让 AI 修**的 finding(locating 阶段的真实动作)。

    直接用既有聚合器,不另写一套 —— 前端「质量参考」面板、逐处修复按钮读的都是它,
    再写一份就会有两种"这篇有几处能修"的说法。
    `ai_repairable=False` 的(医疗/法律/金融高风险等)这里就滤掉:
    端点也会拒,但让它先在 locating 阶段消失,用户才不会看到一排必然失败的 repairing。
    """
    from services.article_findings_aggregate import aggregate_article_findings
    from services.span_level_repair import finding_fingerprint

    located: list[dict[str, Any]] = []
    for card in aggregate_article_findings(quality_warning, industry=industry, title=title):
        for span in card.get("spans") or []:
            if not span.get("ai_repairable"):
                continue
            matched = str(span.get("matched_text") or "").strip()
            if not matched:
                continue
            located.append({
                "code": str(card.get("code") or ""),
                "matched_text": matched,
                # 幂等键 = 既有的稳定指纹(code + 命中串,刻意不含字符偏移 ——
                # 偏移每修一次就漂,含了它额度会被"改一处就重置"绕开)。
                "finding_id": finding_fingerprint(str(card.get("code") or ""), matched),
                "severity": card.get("severity"),
            })
    return located


async def run_repair_job(
    job: RepairJob,
    *,
    load_article: Callable[[int], Awaitable[dict[str, Any] | None]],
    repair_one: Callable[[int, str, str], Awaitable[dict[str, Any]]],
    validate_article: Callable[[int], Awaitable[dict[str, Any]]],
) -> None:
    """按 queued → reviewing → locating → repairing → validating → partial/completed
    的真实顺序驱动整批。

    三个依赖用**注入**而不是直接 import server:
      · 生产接线时传的就是既有的单篇端点处理函数(同一份口径、同一道鉴权、
        同一个额度闸),不复制第二份实现;
      · 测试里传替身,就能在不起 FastAPI、不调模型的前提下验事件序列与幂等。
    """
    try:
        for article_id in job.article_ids:
            job.emit("queued", article_id=article_id, message="排队中")

        for article_id in job.article_ids:
            article_ok = True
            try:
                job.emit("reviewing", article_id=article_id, message="正在读取这篇的问题清单")
                article = await load_article(article_id)
                if article is None:
                    # 🔴 只标记,不在这里加计数:`continue` 照样会走 finally,
                    # 在两处都加 = 这一篇被数两次,done 会超过 total。
                    article_ok = False
                    job.emit("failed", article_id=article_id, message="找不到这篇文章,可能已被删除",
                             retryable=False)
                    continue

                located = locate_repairable_findings(
                    article.get("quality_warning"),
                    industry=str(article.get("industry") or ""),
                    title=str(article.get("title") or ""),
                )
                job.emit("locating", article_id=article_id,
                         message=f"定位到 {len(located)} 处可以自动修",
                         located_count=len(located))

                repaired = failed_here = skipped_here = 0
                for finding in located:
                    key = (article_id, finding["finding_id"])
                    if key in job.attempted_keys:
                        # 幂等:同一个 job 里同一条 finding 只动一次。
                        # 重连重放会走到这里 —— 不再调 provider、不再改正文。
                        skipped_here += 1
                        continue
                    job.attempted_keys.add(key)
                    job.emit("repairing", article_id=article_id, finding_id=finding["finding_id"],
                             message=f"正在修:{finding['matched_text'][:24]}")
                    outcome = await repair_one(article_id, finding["code"], finding["matched_text"])
                    if bool((outcome or {}).get("success")):
                        repaired += 1
                    else:
                        failed_here += 1

                job.emit("validating", article_id=article_id, message="重新跑一次审核")
                verdict = await validate_article(article_id)

                if failed_here or (verdict or {}).get("remaining_hard"):
                    job.emit(
                        "partial", article_id=article_id,
                        message=f"修好 {repaired} 处,还有 {failed_here} 处没修成;可以单独重试或手改",
                        repaired_count=repaired, failed_count_in_article=failed_here,
                        skipped_count_in_article=skipped_here,
                        article_review_status=(verdict or {}).get("article_review_status"),
                    )
                else:
                    job.emit(
                        "completed", article_id=article_id,
                        message=f"这篇处理完了(修好 {repaired} 处)",
                        repaired_count=repaired, skipped_count_in_article=skipped_here,
                        article_review_status=(verdict or {}).get("article_review_status"),
                    )
            except Exception as exc:  # noqa: BLE001 —— 逐篇隔离,与 P3a run_batch 同一条纪律
                article_ok = False
                job.emit("failed", article_id=article_id,
                         message=str(getattr(exc, "detail", None) or exc) or "处理失败,可以单独重试这一篇",
                         retryable=_retryable(exc))
            finally:
                if article_ok:
                    job.success_count += 1
                else:
                    job.failed_count += 1
                job.done += 1

        job.emit(
            "completed",
            message=f"全部处理完:成功 {job.success_count} 篇 / 未成功 {job.failed_count} 篇,共 {job.total} 篇",
        )
    except asyncio.CancelledError:
        job.emit("failed", message="任务被中断;已完成的部分都已保存")
        raise
    except Exception as exc:  # noqa: BLE001
        job.emit("failed", message=f"任务异常终止:{exc};已完成的部分都已保存")
    finally:
        job.mark_finished()


def _retryable(exc: Exception) -> bool:
    """与 P3a `article_batch_review._retryable` 同口径:4xx 不给重试按钮。"""
    status = getattr(exc, "status_code", None)
    try:
        return not (status is not None and 400 <= int(status) < 500)
    except (TypeError, ValueError):
        return True
