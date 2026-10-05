"""GEO 抖音图文 · 后台任务进度描述(纯函数 · 无 DB 无 IO)

存在的理由有两条,都不是"为了好看":

1. **前端不该自己拼进度语义。** 阶段权重、百分比、预计剩余、"是不是卡住了"
   这些判定只能有一处;放前端就会出现"列表页说 40%、详情页说 60%"。

2. **纯函数才锁得住。** 之前的教训是断言"源码里含某个词"那种锁,换个写法就假绿。
   这里全部输入靠参数进、结果靠返回值出,测试可以直接喂一个 task dict 断行为。

🔴 时间基准统一由调用方传 `now`(不在函数体里取当前时间)——
   否则测试没法构造"已经跑了 20 分钟"这种场景,只能去 mock 时钟。

🔴 本模块**不翻译失败原因**:那件事在 api 层的 humanize_failure(),
   同一事实不写两处(弱锁四型之一:同一事实写两处,删一处另一处还在)。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services.geo_douyin.config import (
    CARD_SECONDS_ESTIMATE,
    COPY_SECONDS_ESTIMATE,
    TASK_STALE_SECONDS,
)

# 阶段 → 给人看的话(元指令:工程术语全站翻人话,不出现 copy/images/OSS)
STAGE_LABELS: Dict[str, str] = {
    "queued": "排队中",
    "copy": "正在写文案",
    "images": "正在画卡片图",
    "redraw": "正在重画这一张",
    "done": "完成",
}

# §15-2「补齐中」:已达成品门槛、还有卡在补。
# 🔴 它是**非终态**(还没 commit 扣款),但**不是** running ——
#    自动补齐已经跑完了,球在用户那边(手动重抽,那一次单独计费)。
#    所以 active=False(别让前端一直转圈),但也绝不能显示成"失败"。
STATE_COMPLETING = "completing"

# 阶段占整条进度的区间。images 占大头是因为它真的占大头(实测 137s 里 ~112s)。
STAGE_BANDS: Dict[str, tuple] = {
    "queued": (0, 2),
    "copy": (2, 20),
    "images": (20, 98),
    "done": (100, 100),
}

# 终态:到这三个就停轮询
TERMINAL_STATES = ("succeeded", "failed", "cancelled")


def _as_epoch(value: Any) -> Optional[float]:
    """把 DB 回来的时间列转成 epoch 秒。拿不准就返回 None(宁可不显示 ETA)。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def stage_label(stage: Optional[str]) -> str:
    return STAGE_LABELS.get(str(stage or ""), "正在制作")


def compute_percent(stage: Optional[str], done: int, total: int) -> int:
    """百分比 = 阶段区间 + 区间内按已完成张数线性推进。

    🔴 只在 images 阶段用 done/total 细分。别的阶段没有"张"的概念,
       拿 done/total 去插值会得出 0/0 这种没意义的数。
    """
    key = str(stage or "") or "queued"
    lo, hi = STAGE_BANDS.get(key, (20, 98))
    if key == "done":
        return 100
    if key == "images" and total > 0:
        frac = max(0.0, min(1.0, float(done) / float(total)))
        return int(round(lo + (hi - lo) * frac))
    return int(lo)


def estimate_remaining_seconds(stage: Optional[str], done: int, total: int) -> Optional[int]:
    """预计还要多久。基数是**生产实测**的单张耗时,不是模拟出来的数字。

    返回 None = 估不出来(前端就别显示倒计时,不要瞎编一个)。
    """
    key = str(stage or "")
    if key == "done":
        return 0
    if total <= 0:
        return None
    remaining_cards = max(0, int(total) - max(0, int(done)))
    if key == "images":
        # 封面已经在跑/跑完,剩下的是并发的 —— 但并发上限之外仍要排队,
        # 所以不按"全部并行"算(那会给出一个总是偏乐观、然后一直往上跳的数)。
        return int(remaining_cards * CARD_SECONDS_ESTIMATE * 0.6) or CARD_SECONDS_ESTIMATE
    if key in ("copy", "queued", ""):
        return int(COPY_SECONDS_ESTIMATE + total * CARD_SECONDS_ESTIMATE * 0.6)
    return None


def describe_task_progress(task: Optional[Dict[str, Any]], *,
                           now: Optional[float] = None,
                           post_status: str = "") -> Dict[str, Any]:
    """把一行任务记录翻成前端直接能渲染的进度描述。

    task=None(还没建任务行)也要给出可用结果 —— 用户点完"做这条"到后台
    真正建行之间有个窗口,那一瞬间前端不该显示"失败"。
    """
    now_ts = float(now) if now is not None else datetime.now(timezone.utc).timestamp()
    t = dict(task or {})

    raw_state = str(t.get("status") or "")
    stage = str(t.get("stage") or "")
    done = int(t.get("progress_done") or 0)
    total = int(t.get("progress_total") or 0)

    if not task:
        # 没有任务行:作品已经是终态就照作品状态说,否则算"排队中"
        if post_status in ("ready", "published"):
            return {"state": "succeeded", "stage": "done", "stage_label": "完成",
                    "percent": 100, "done": 0, "total": 0, "eta_seconds": 0,
                    "elapsed_seconds": None, "stalled": False, "active": False}
        if post_status == "failed":
            return {"state": "failed", "stage": "", "stage_label": "没做成",
                    "percent": 0, "done": 0, "total": 0, "eta_seconds": None,
                    "elapsed_seconds": None, "stalled": False, "active": False}
        return {"state": "queued", "stage": "queued", "stage_label": STAGE_LABELS["queued"],
                "percent": 0, "done": 0, "total": 0, "eta_seconds": None,
                "elapsed_seconds": None, "stalled": False, "active": True}

    started = _as_epoch(t.get("started_at")) or _as_epoch(t.get("created_at"))
    touched = _as_epoch(t.get("updated_at")) or started
    elapsed = int(now_ts - started) if started is not None else None

    # 🔴 卡住判定:只对**非终态**成立。已经 succeeded 的任务放几天也不是"卡住"。
    idle = (now_ts - touched) if touched is not None else 0.0
    stalled = (raw_state not in TERMINAL_STATES
               and idle >= TASK_STALE_SECONDS)

    if raw_state in TERMINAL_STATES:
        state = raw_state
    elif raw_state == STATE_COMPLETING:
        # 🔴 补齐中不判"卡住":球在用户那边,它可以合法地停很久
        #    (12h sweeper 才是那条死线)。判成 stalled 会让 UI 说"中断了",
        #    而实际上已经有成品、只差补几张。
        state = STATE_COMPLETING
        stalled = False
    elif stalled:
        state = "stalled"
    elif raw_state in ("running", "pending", ""):
        state = "running" if raw_state == "running" else "queued"
    else:
        state = raw_state

    if state == "succeeded":
        percent, eta = 100, 0
    elif state in ("failed", "cancelled", "stalled", STATE_COMPLETING):
        percent, eta = compute_percent(stage, done, total), None
    else:
        percent, eta = (compute_percent(stage, done, total),
                        estimate_remaining_seconds(stage, done, total))

    return {
        "state": state,
        "stage": stage or ("done" if state == "succeeded" else "queued"),
        "stage_label": ("完成" if state == "succeeded" else
                        "没做成" if state == "failed" else
                        "可能中断了" if state == "stalled" else
                        stage_label(stage)),
        "percent": percent,
        "done": done,
        "total": total,
        "eta_seconds": eta,
        "elapsed_seconds": elapsed,
        "stalled": stalled,
        # active=True → 前端继续轮询。stalled 要停(再转下去也不会变)
        "active": state in ("queued", "running"),
    }
