"""
Cron 触发闸(WORKERS=4 · SPEC §1/§2)—— 单点决定"本进程是否真正触发 cron"。
====================================================================

WORKERS=4 病根:两个 BackgroundScheduler 单例(api.scheduler 主 + 根 scheduler.py)
今天在每个非 backup worker 的导入期就 .start(),4 个 web worker 各跑一份 → 计费/结算
cron 4 倍触发(仅靠各 job 内部幂等勉强兜底)。

修法(最稳的杠杆 = 控制 .start()):
  - 两个 scheduler 的 .start() 都先查本闸;闸关 → 只 add_job 不 start(未 start 的
    scheduler 永不触发已注册 job)→ web/backup 进程天然不跑 cron。
  - 仅 ROLE=cron 专用进程、且通过 LeaderLock 选主成功后,由 server.py 置闸为 True。
  - 正确性不依赖本闸(它只是"至多一处触发"的优化):真正的恰一次靠 DB
    (sched_job_runs UNIQUE claim + 业务幂等键 + §3 终态 CAS)。

本模块零内部依赖(只用于打破 api.scheduler ↔ scheduler.py ↔ server.py 的引用环)。
"""

_active = False


def set_cron_active(active: bool) -> None:
    """由 server.py 启动路径调用:ROLE=cron 选主成功→True;丢租/降级→False。"""
    global _active
    _active = bool(active)


def cron_should_fire() -> bool:
    """两个 scheduler 的 .start() 查此闸。默认 False(web/backup/prestart/导入期恒不触发)。"""
    return _active
