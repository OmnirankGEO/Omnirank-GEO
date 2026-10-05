# -*- coding: utf-8 -*-
"""变异 runner **断点续跑缓存**的合同判据。

起因(2026-08-28 实测被咬):底从 `dd440a6e6` 换到 `b1fa4f88b` 之后重跑 12 发,
产物文件还是上一轮那份,而续跑判定是 ``done = {r["id"] for r in results}`` ——
只认 id、不认尖。于是:

  · 12 发**全部** `⏭ 已有结果,跳过`,一发没跑;
  · "复放前 / 复放后"表照常打印 **9 杀 3 存活**;
  · 那 9 个"杀"是**上一个尖**的。

它不报错、不空、格式全对 —— **坏消息形状的假数**是最难看出来的一种。
当时抓出来靠的是读数自证:三发存活的 ``full_green=2121`` 是旧尖基线,
本尖基线是 **2133**;``quick_green`` 92/127/82 也全是旧尖的逐包数。

跑法(不进任何交付包的分母,单独跑)::

    python -m pytest scripts/test_mutation_resume_cache_contract.py -q
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "mutation_runner_extsel_v2_2026_08_27.py"

_spec = importlib.util.spec_from_file_location("resume_runner", RUNNER)
_R = importlib.util.module_from_spec(_spec)
sys.modules["resume_runner"] = _R
sys.path.insert(0, str(RUNNER.parent))
_spec.loader.exec_module(_R)

# 🔴 [V7-B] 夹具改用**生产格式**记录(带 run_meta)—— 见 mutation_criteria_fixtures 抬头。
from mutation_criteria_fixtures import mfp, ok_meta, rec, tbi  # noqa: E402

TIP = "b1fa4f88b" * 4          # 40 hex-ish chars,只要是个稳定串就够
OTHER = "dd440a6e6" * 4


def test_resume_01_same_tip_records_count_as_done():
    """同一个尖的读数才是"我这轮已经跑过了"。"""
    recs = [rec("MUT-A", tip=TIP, fp="aaaa", verdict=None),
            rec("MUT-B", tip=TIP, fp="bbbb", verdict=None)]
    # 指纹全给对、run_meta 全合法 —— 这一族考的是**尖**那一轴,
    # 别让指纹轴或 run_meta 轴的红混进来(那会让这条判据在尖检查被删掉后仍然红)。
    fps = {r["id"]: r.get("criteria_fp") for r in recs}
    assert _R.resume_done_ids(recs, TIP, "x.json", fps,
                              targets_by_id=tbi("MUT-A", "MUT-B"),
                              mut_fp_by_id=mfp("MUT-A", "MUT-B")
                              ) == {"MUT-A", "MUT-B"}


def test_resume_02_a_record_from_another_tip_stops_the_run():
    """🔴 正样本 —— **点名规则**:别的尖的读数不许冒充本轮结果。

    这就是 2026-08-28 那一脚:换尖之后旧产物留在原地,12 发全跳过,
    报表照样打印 9 杀 3 存活。
    """
    recs = [{"id": "MUT-A", "tip": TIP, "run_meta": ok_meta()},
            {"id": "MUT-B", "tip": OTHER, "run_meta": ok_meta()}]
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids(recs, TIP, "extsel_v2_results_replay.json",
                           {r["id"]: r.get("criteria_fp") for r in recs},
                           targets_by_id=tbi("MUT-A", "MUT-B"))
    # 🔴 断言点名**尖**那条规则:run_meta 给合法的,免得这条判据靠别的门变红
    #    —— 那样把尖检查删掉它照样红,区分力就没了。
    assert "别的尖" in str(exc.value)


def test_resume_03_a_record_without_a_tip_stops_the_run():
    """没钉尖的老格式产物同样不许被静默复用 —— 它的出处**不可知**。"""
    recs = [{"id": "MUT-A", "run_meta": ok_meta()}]
    with pytest.raises(SystemExit) as exc:
        _R.resume_done_ids(recs, TIP, "x.json",
                           {r["id"]: r.get("criteria_fp") for r in recs},
                           targets_by_id=tbi("MUT-A"))
    assert "别的尖" in str(exc.value), "应因**没钉尖**停机,不是因为别的门"


def test_resume_04_empty_results_are_not_an_error():
    """空产物 = 干净起跑,不是错误。"""
    assert _R.resume_done_ids([], TIP, "x.json", {}, targets_by_id={}) == set()


def test_resume_05_every_written_record_gets_stamped():
    """结构锁:两处落盘都必须写 `tip`,否则下一轮的守卫拿不到东西可比。"""
    src = RUNNER.read_text(encoding="utf-8")
    assert src.count('rec["tip"] = tip') >= 1, "快集落盘没钉尖"
    # 续跑判定不许退回只认 id 的写法。
    assert 'done = {r["id"] for r in results}' not in src, (
        "续跑判定退回了只认 id 的老写法 —— 换尖之后旧读数会冒充本轮结果")
    assert "resume_done_ids(" in src, "守卫没接线"
