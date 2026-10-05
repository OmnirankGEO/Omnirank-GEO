"""E3 第 0 片 · 删社媒包前先修的在役断点(Review 09-26 / 09-27 派单)。

守三件事:
1. 询价记录「草稿区」`GET /api/agent/draft-workspace`:社媒包与社媒 agent 不可导入时仍返回 200。
   [开源 E3 · B2 · 2026-09-28] 两件已真删 ⇒ 不再用拦截器模拟:干净子进程里直接证明两件找不到(find_spec 为空),
   端点照样 200;对照臂:同一把 find_spec 对在役的 agents.session_manager 必须找得到(防恒返「找不到」的尺子)。
2. scheduler 的两个社媒 cron(daily_inspiration / event_driven_checks)不在默认任务里,也不能手动触发;
   对照臂:其余默认任务仍在(防「一个任务都没注册」也算绿)。
3. 口播脚本局部重写 regenerate_part 已随 E3 删除(文件随 B2 删):工具表 / 路由表 / SSE 文案里都不再有它,文件不存在,
   业务 .py 一处都不再提它;
   对照臂:相邻工具 confirm_memory_conflict 三处都在。
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOCIAL_PKGS = ("agents.social_agent", "tools.social_operator")  # 两件均已随开源 E3 B2 删;仍按此名拦:防它被加回来


def _run(code: str) -> dict:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(REPO)}
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    lines = [l for l in r.stdout.splitlines() if l.startswith("RESULT=")]
    assert r.returncode == 0 and lines, f"子进程失败 rc={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}"
    return json.loads(lines[-1][len("RESULT="):])


_DRAFT_WORKSPACE = r'''
import json, sys
import importlib.util as u
out = {}
# 两件已真删(开源 E3 B2):找不到;对照:在役模块找得到 —— 同一把尺子两条臂
for p in ("agents.social" + "_agent", "tools.social" + "_operator", "agents.session_manager"):
    out[p] = "missing" if u.find_spec(p) is None else "present"

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import agents.session_manager as sm
sm.get_drafts = lambda user_id, brand_id: [{"id": 7, "item_type": "quote", "item_key": "k", "item_data": {"brand_id": brand_id}}]
from api import agent_api

app = FastAPI()
@app.middleware("http")
async def _user(request: Request, call_next):
    request.state.user = {"user_id": 42, "role": "agent"}
    return await call_next(request)
app.include_router(agent_api.router)
r = TestClient(app).get("/api/agent/draft-workspace", params={"brand_id": 5})
out["status"] = r.status_code
out["body"] = r.json()
print("RESULT=" + json.dumps(out, ensure_ascii=False))
'''


def test_draft_workspace_returns_200_with_social_packages_gone():
    out = _run(_DRAFT_WORKSPACE)
    assert out["agents.social" + "_agent"] == "missing" and out["tools.social" + "_operator"] == "missing", out
    assert out["status"] == 200, out
    assert out["body"]["success"] is True and out["body"]["count"] == 1, out
    assert out["body"]["drafts"][0]["item_data"] == {"brand_id": 5}, out


def test_draft_workspace_ruler_control_arm():
    # 对照臂:同一个子进程里 find_spec 对在役模块报「在」—— 证明上一格的「找不到」不是尺子恒返 None
    out = _run(_DRAFT_WORKSPACE)
    assert out["agents.session_manager"] == "present", out
    assert out["status"] == 200, out


def test_agent_api_has_no_module_level_social_import():
    tree = ast.parse((REPO / "api/agent_api.py").read_text(encoding="utf-8"))
    bad = []
    for node in tree.body:  # 只看模块顶层;函数体内的延迟导入允许(/chat 删包时随之删除)
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.startswith(SOCIAL_PKGS)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            full = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            bad += [n for n in full if n.startswith(SOCIAL_PKGS)]
    assert bad == [], bad


_JOBS = r'''
import json, asyncio
import scheduler as s
s.init_scheduler()
s.setup_default_jobs()
ids = sorted(j.id for j in s.scheduler.get_jobs())
trig = {}
for jid in ("event_driven_checks", "daily_inspiration"):
    try:
        asyncio.run(s.api_trigger_job(jid))
        trig[jid] = "ran"
    except ValueError:
        trig[jid] = "unknown"
print("RESULT=" + json.dumps({"ids": ids, "trigger": trig, "has_funcs": [n for n in ("job_daily_inspiration", "job_event_driven_checks") if hasattr(s, n)]}))
'''

SOCIAL_CRONS = {"daily_inspiration", "event_driven_checks"}
# 对照:与社媒无关、本片不动的默认任务(防「setup_default_jobs 什么都没注册」也算绿)
KEPT_CRONS = {"daily_db_backup", "geo_douyin_publish_converge", "monthly_report", "payment_overdue_check",
              "token_expiry_check", "weekly_report"}


def test_social_crons_are_not_scheduled_and_cannot_be_triggered():
    out = _run(_JOBS)
    assert SOCIAL_CRONS.isdisjoint(out["ids"]), out
    assert KEPT_CRONS <= set(out["ids"]), out
    assert out["trigger"] == {"event_driven_checks": "unknown", "daily_inspiration": "unknown"}, out
    assert out["has_funcs"] == [], out
    src = (REPO / "scheduler.py").read_text(encoding="utf-8")
    # [E3a] 两个 cron 的来源模块已删 ⇒ 反查放宽为「任何提及」(不只 `api.` 前缀那种)
    for needle in ("inspiration_api", "intelligence_api", "run_all_event_checks",
                   "sync_fetch_all_brands_inspirations"):
        assert needle not in src, needle


def test_regenerate_part_is_gone_from_the_tool_table():
    from tools.agent_loop.tool_definitions import TOOL_SCHEMAS
    from tools.agent_loop.tool_router import _HANDLERS
    from tools.agent_loop.sse_events import TOOL_LABELS

    names = {t["name"] for t in TOOL_SCHEMAS}
    assert "regenerate_part" not in names  # 工具随开源 E3 删;仍锁:防它被加回来
    assert "regenerate_part" not in _HANDLERS  # 工具随开源 E3 删;仍锁:防它被加回来
    assert "regenerate_part" not in TOOL_LABELS  # 工具随开源 E3 删;仍锁:防它被加回来
    # [开源 E3 · B2 · 2026-09-28] 文件已随删包那一片删除;本格按注释预告改写:
    #   不再是「除它自己的测试外没人导入它」,而是「文件不在 + 业务 .py 一处都不再提这个名字」。
    assert not (REPO / "tools" / "agent_loop" / "write_tools" / "regenerate_part.py").exists()
    hits = subprocess.run(["git", "grep", "-l", "-z", "regenerate_part"], cwd=str(REPO),
                          capture_output=True, check=True).stdout.decode("utf-8").split("\0")
    tracked = [p for p in hits if p]
    # 尺子自证①:本锁文件自己的文本里有这个名字,搜不到它 ⇒ 搜索本身坏了
    assert "tests/oss_e3s0_live_breakpoints_2026_09_27/test_e3s0_live_breakpoints.py" in tracked, tracked
    # 尺子自证②(范围缩到 tests/ 这剂毒它不能是瞎的):业务目录里仍提这个名字的非 .py 文件必须搜得到 ——
    #   [开源 E3 · 前端 · 2026-10-01 · WO_322] 原来靠前端两处请求字段名,那两个文件随前端 E3 删;按预告改写:
    #   tests/ 之外、非 .py 的已跟踪文件(历史审计记录 / 旧接口快照)仍提这个名字,必须搜得到。
    assert any(not p.startswith("tests/") and not p.endswith(".py") for p in tracked), tracked
    py_business = [p for p in tracked if p.endswith(".py") and not p.startswith("tests/")]
    assert py_business == [], py_business
    # 对照臂:相邻的写工具三处都还在
    assert "confirm_memory_conflict" in names
    assert "confirm_memory_conflict" in _HANDLERS
    assert "confirm_memory_conflict" in TOOL_LABELS
