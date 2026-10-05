"""角色探针 —— **在子进程里真 import server.py**,报告它注册了什么。

🔴 为什么必须是子进程:``server._IS_CRON_ROLE`` 是**模块导入期**按 ``ROLE``
   算出来的常量,而 ``if _IS_CRON_ROLE: register_v32_core_tasks()`` 也在导入期
   执行。同一个进程里 import 一次就定死了,换环境变量再 import 拿到的是缓存 ——
   两臂会得到同一个答案,而那个答案看起来还是对的(假绿)。

🔴 为什么不是 AST 锁:AST 杀不掉 ``if False:`` / 早 return / 把整块搬进
   一个没人调的函数 —— 语法树里那几行一个字没少。本仓门三 G9 记过这一条。
   这里让**真的 server 模块跑一遍导入**,然后看桩收到了什么。

用法(由 ``test_c7_cron_role_runtime.py`` 以子进程调用)::

    ROLE=web  DATABASE_URL=... python -m tests.defgeo_woc_closure_2026_08_25._role_probe
"""

from __future__ import annotations

import json
import os
import sys


class _StubScheduler:
    """只记账,不真跑。``get_job`` 必须真的返回已加的那条 ——
    注册块用 ``if not scheduler.get_job(id)`` 做幂等,返回恒 None 会让
    同一条被重复 add,那时"注册了几条"就数不准了。"""

    def __init__(self) -> None:
        self.jobs: dict = {}

    def get_job(self, job_id):                            # noqa: ANN001
        return self.jobs.get(job_id)

    def add_job(self, func, **kw):                        # noqa: ANN001
        self.jobs[kw.get("id")] = getattr(func, "__module__", "?") + ":" + \
            getattr(func, "__name__", "?")
        return kw.get("id")

    def remove_job(self, job_id):                         # noqa: ANN001
        self.jobs.pop(job_id, None)


def main() -> int:
    sys.path.insert(0, os.getcwd())

    stub = _StubScheduler()
    register_calls: list = []

    import api.scheduler as sched

    sched.get_scheduler = lambda: stub                    # type: ignore[assignment]
    _real_register = sched.register_v32_core_tasks

    def _recording_register(*a, **kw):                    # noqa: ANN001
        register_calls.append("called")
        return _real_register(*a, **kw)

    sched.register_v32_core_tasks = _recording_register   # type: ignore[assignment]

    import server                                          # noqa: F401  ← 真导入

    print("PROBE_RESULT " + json.dumps({
        "role": server._SCHED_ROLE,
        "isCronRole": bool(server._IS_CRON_ROLE),
        "registerCalls": len(register_calls),
        "jobs": stub.jobs,
        # [工单 C-7(1)] 顺带在**同一次真导入**里跑一遍设置保存 ——
        # 那是幽灵配置 `monitoring_tasks` 真正会炸的地方,见 _settings_probe。
        "settingsSave": _settings_probe(server),
    }, ensure_ascii=False))
    return 0


def _settings_probe(server) -> dict:                       # noqa: ANN001
    """真调 ``server.update_settings(SettingsUpdateRequest())``。

    🔴 为什么是**真调**而不是读源码:`config/settings_manager.SystemSettings`
       上的 `monitoring_tasks` 已按 Owner 2026-08-24 的批复删除,而 server.py
       的保存链一直还在写 `current.monitoring_tasks` —— pydantic v2 上那是
       一个 **AttributeError**,handler 的 `except Exception` 把它翻成 500。
       静态锚只能证明"那行文本不在了";只有真跑一遍能证明"跑起来不炸"。
       (本仓记过:存在性毒 ≠ 执行毒。)

    🔴 不带 `monitoring_tasks` 的请求体正是**触发条件**:带了的会走
       `request.monitoring_tasks or current.monitoring_tasks` 的短路,
       躲过那个属性访问。默认构造的请求体就是不带。

    settings.json 重定向到临时文件 —— 判据不许改工作树里那一份。
    """
    import tempfile
    from pathlib import Path

    import config.settings_manager as _sm

    original = _sm.SETTINGS_FILE
    tmp = Path(tempfile.gettempdir()) / "woc_c7_settings_probe.json"
    _sm.SETTINGS_FILE = tmp
    try:
        out = server.update_settings(server.SettingsUpdateRequest())
        saved = {}
        if tmp.exists():
            saved = json.loads(tmp.read_text(encoding="utf-8"))
        return {
            "ok": bool(out.get("success")),
            "error": None,
            "hasGhostKey": "monitoring_tasks" in saved,
            "savedKeyCount": len(saved),
        }
    except BaseException as exc:                           # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                "hasGhostKey": None, "savedKeyCount": 0}
    finally:
        _sm.SETTINGS_FILE = original
        try:
            tmp.unlink(missing_ok=True)
        except Exception:                                  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
