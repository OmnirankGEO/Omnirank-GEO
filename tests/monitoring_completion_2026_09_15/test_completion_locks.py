# -*- coding: utf-8 -*-
"""WO_224-c1 §3.3 锁 —— 完成信号必须锚**结果**,不锚「派了几个引擎」。

生产实证(Deploy 224-d1,2026-09-15):47 个 task(14.4%)零结果行却全部
`status='completed'`,各有 4 个 platform 格子。任何锚在「派了几个引擎」
或任务状态字的判据都判它们健康 —— `monitoring_results` 里一行都没有。
本仓第十一件同形:**完成信号由做事方发出**,真判据要取自**被服务方**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _status_and_summary(cur, task_id):
    cur.execute("SELECT status, result_summary FROM monitoring_tasks WHERE id = %s", (task_id,))
    row = dict(cur.fetchone())
    summary = row.get("result_summary")
    if isinstance(summary, str) and summary.strip():
        try:
            summary = json.loads(summary)
        except Exception:  # noqa: BLE001
            summary = {"_unparsed": summary}
    return row.get("status"), (summary if isinstance(summary, dict) else {})


# ════════════════════════════════════════════════════════════════
# ① 零结果不得 completed —— 覆盖全部四条完成路径
# ════════════════════════════════════════════════════════════════

def test_zero_results_can_never_be_completed(cur, make_task):
    """🔴 判据 ①:一行结果都没有的 task,写 completed 必须被拦成 failed。

    这正是生产那 47 单的形态:格子有、状态是 completed、结果零行。
    """
    from db.monitoring_db import update_task_status

    tid = make_task(platform_count=4)
    update_task_status(tid, "completed", 0, {"note": "空跑"})

    status, summary = _status_and_summary(cur, tid)
    assert status == "failed", "零结果却落了 %s" % status
    blocked = summary.get("completion_blocked") or {}
    assert blocked.get("reason") == "no_result_rows", summary
    # 调用方原来的 summary 不许被吃掉
    assert summary.get("note") == "空跑", summary


def test_a_full_task_really_does_complete(cur, make_task, add_results, add_cells):
    """🔴 正向对照:派 4 落 4 **必须** completed。

    没有这一条,把准入改成「谁都不许 completed」也能让上面那条绿 —— 恒红 = 恒废。
    """
    from db.monitoring_db import update_task_status

    plats = ["dashscope", "deepseek", "kimi", "doubao"]
    tid = make_task(platform_count=len(plats))
    add_cells(tid, plats)
    add_results(tid, plats)
    update_task_status(tid, "completed", 4, {"note": "正常"})

    status, summary = _status_and_summary(cur, tid)
    assert status == "completed", (status, summary)
    assert "completion_blocked" not in summary, summary


# ════════════════════════════════════════════════════════════════
# ② 有派发台账 ⇒ 比**集合**,说得出缺哪个引擎
# ════════════════════════════════════════════════════════════════

def test_dispatched_four_delivered_three_is_not_complete(cur, make_task, add_results, add_cells):
    """🔴 派 4 落 3 ⇒ 不得 completed,且要说出**缺的是哪个引擎**。"""
    from db.monitoring_db import update_task_status

    plats = ["dashscope", "deepseek", "kimi", "doubao"]
    tid = make_task(platform_count=4)
    add_cells(tid, plats)
    add_results(tid, plats[:3])            # 少 doubao
    update_task_status(tid, "completed", 3)

    status, summary = _status_and_summary(cur, tid)
    assert status == "failed", status
    blocked = summary.get("completion_blocked") or {}
    assert blocked.get("reason") == "missing_planned_platforms", summary
    assert blocked.get("missing_platforms") == ["doubao"], (
        "只说了缺几个、没说缺哪个 —— after 查还得再翻日志:%s" % blocked)
    assert blocked.get("expected_source") == "run_cells", blocked


def test_unified5_missing_yuanbao_is_not_complete(cur, make_task, add_results):
    """🔴 派 **5** 落 4(unified5 少元宝)⇒ 不得 completed。

    这一条是「引擎集合不许取常量」的落点:仓里三个常量语义三分 ——
      `MONITORING_ENGINES` = **5**(授权面,unified5 含元宝)
      `MONITORING_RUN_CELL_PLATFORMS` = 4(执行面)
      `engine_contract.PLATFORM_CONTRACT` = 4(provider/model 合同)
    `config/ai_engines.py:66-75` 自己写着「数值恰好相同掩盖了语义错位」。
    拿其中任何一个当「这单派了几个引擎」,这张单都会被判健康。

    🔴 夹具**只能走无格子那条路径**,而且这不是夹具偷懒:
       `chk_monitoring_run_cells_platform` 这条 DB CHECK 物理不许写 yuanbao
       (实测 `CHECK (platform = ANY (dashscope, deepseek, kimi, doubao))`),
       所以 unified5 的第五个引擎**根本进不了派发台账**。
       也就是说「授权 5 / 执行 4」这个裂口今天只会在**计数**那一档露出来 ——
       判据必须建在它真会出现的那条路径上,不是我希望它出现的那条。
    """
    from db.monitoring_db import update_task_status

    tid = make_task(platform_count=5)                # 授权 5,无格子
    add_results(tid, ["dashscope", "deepseek", "kimi", "doubao"])   # 落 4,缺元宝
    update_task_status(tid, "completed", 4)

    status, summary = _status_and_summary(cur, tid)
    assert status == "failed", ("授权 5 只落了 4 却放行了:%s" % status)
    blocked = summary.get("completion_blocked") or {}
    assert blocked.get("reason") == "fewer_platforms_than_dispatched", summary
    assert blocked.get("expected") == 5, blocked

def test_four_rows_with_one_misspelled_platform_is_not_complete(
        cur, make_task, add_results, add_cells):
    """🔴 派 4 落 4、但有一行 platform 名写错 ⇒ 仍不得 completed。

    **只有集合比对抓得到**:计数是 4、行数是 4,按数判一切正常。
    名字空间错位在本仓出过(deepseek vs deepseek_official,
    a-name-plays-three-roles:认 / 发 / 比 三处要同一个词表)。
    """
    from db.monitoring_db import update_task_status

    plats = ["dashscope", "deepseek", "kimi", "doubao"]
    tid = make_task(platform_count=4)
    add_cells(tid, plats)
    add_results(tid, ["dashscope", "deepseek", "kimi", "deepseek_official"])  # 名写错
    update_task_status(tid, "completed", 4)

    status, summary = _status_and_summary(cur, tid)
    assert status == "failed", ("按计数看是 4 落 4,只有集合比对能拦:%s" % status)
    blocked = summary.get("completion_blocked") or {}
    assert blocked.get("missing_platforms") == ["doubao"], summary


# ════════════════════════════════════════════════════════════════
# ③ 没派发台账的路径(batch_monitor)⇒ 退到该 task 自记的宽度
# ════════════════════════════════════════════════════════════════

def test_path_without_run_cells_falls_back_to_the_tasks_own_width(
        cur, make_task, add_results):
    """🔴 `tools/monitoring/batch_monitor` 那条路径**不建格子**。

    只用「每个格子都有结果」的话,它在那条路径上**空真** = 假绿。
    所以退到该 task 自己记的 `platform_count`(四条建 task 路径都写了它)。
    """
    from db.monitoring_db import update_task_status

    tid = make_task(platform_count=4)                       # 无 add_cells
    add_results(tid, ["dashscope", "deepseek", "kimi"])     # 只落 3
    update_task_status(tid, "completed", 3)

    status, summary = _status_and_summary(cur, tid)
    assert status == "failed", status
    blocked = summary.get("completion_blocked") or {}
    assert blocked.get("reason") == "fewer_platforms_than_dispatched", summary
    assert blocked.get("expected_source") == "task_platform_count", blocked
    assert blocked.get("expected") == 4, blocked

    # 正向对照:同一条路径落满 4 个就必须放行
    tid2 = make_task(platform_count=4)
    add_results(tid2, ["dashscope", "deepseek", "kimi", "doubao"])
    update_task_status(tid2, "completed", 4)
    assert _status_and_summary(cur, tid2)[0] == "completed"


def test_unknown_width_is_let_through_but_says_so(cur, make_task, add_results, caplog):
    """🔴 `platform_count <= 0` ⇒ 这一档等于没判。**放行,但必须出声**。

    静默放行的话,「宽度没记下来」与「宽度满足了」在数据上分不开 ——
    仪器缺了一条腿却读起来很干净,是本仓最贵的那种绿。
    """
    import logging

    from db.monitoring_db import update_task_status

    tid = make_task(platform_count=0)
    add_results(tid, ["dashscope"])
    with caplog.at_level(logging.WARNING):
        update_task_status(tid, "completed", 1)

    status, _summary = _status_and_summary(cur, tid)
    assert status == "completed", "宽度未知这一档是**放行**的:%s" % status
    assert any("platform_count" in r.getMessage() and str(tid) in r.getMessage()
               for r in caplog.records), (
        "宽度判据没生效却**没出声** —— 读起来像判过了:%s"
        % [r.getMessage() for r in caplog.records])


# ════════════════════════════════════════════════════════════════
# ④ 计费不动(反向钉)
# ════════════════════════════════════════════════════════════════

def test_blocking_a_completion_moves_no_money(cur, make_task):
    """🔴 本单只改**状态写入**,不碰资金:被拦的那一刻资金表零变化。

    Deploy 224-d1 实测:47 个空跑的冻结全部 released、冻结合计 0、已记账 0 ——
    计费层本来就是对的。资金语义属五类之一,不许顺手动。
    """
    from db.monitoring_db import update_task_status

    def _snapshot():
        out = {}
        for tbl in ("point_freezes", "point_transactions"):
            try:
                cur.execute("SELECT COUNT(*) AS n FROM %s" % tbl)
                out[tbl] = int(dict(cur.fetchone())["n"])
            except Exception:  # noqa: BLE001 - 表不在就记 None,断言里两侧一致即可
                out[tbl] = None
        return out

    before = _snapshot()
    tid = make_task(platform_count=4)
    update_task_status(tid, "completed", 0)          # 零结果 ⇒ 会被拦成 failed
    after = _snapshot()

    assert _status_and_summary(cur, tid)[0] == "failed", "前提没成立,这条锁测的是别的"
    assert before == after, "完成准入动了资金表:%s -> %s" % (before, after)
    assert set(before.values()) != {None}, "两张资金表都读不到,这条锁没有被测对象"
