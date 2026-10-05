"""[包A] 事件循环阻塞治理 · 锁。

三层:
  1. **并发探针锁(主锁 · 行为锁)** —— 真起 ASGI app,一个重端点(注入 sleep 的同步桩)
     占住线程,同时并发打轻端点,断言轻端点不被拖秒级。
     判别力靠 `test_probe_catches_naked_async_endpoint` 反向对照:同一探针打在
     "裸 async 同步端点" 上必须**转红**,证明探针不是恒绿。
  2. **改法锁** —— 本批点名的只读端点必须是同步 `def`(不是 `async def`),
     且体内不得出现 await(否则 FastAPI 会当同步函数跑而 await 失效)。
  3. **AST 扫描锁** —— 全站"async 端点内裸同步重活"不得新增(基线白名单登记存量)。

不连库:1/2 用桩,3 是纯静态扫描。
"""
from __future__ import annotations

import ast
import concurrent.futures as cf
import contextlib
import io
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 本批按裁定 §A3 改成同步 def 的只读端点(handler 名)
CONVERTED = {
    "api/media_entity_flywheel_api.py": [
        "flywheel_advisory_observability",
        "flywheel_advisory_candidates",
        "source_signal_rollup",
        "answer_adoption_metric_summary",
        "media_flywheel_coverage",
        "flywheel_data_health",
        "media_shadow_recommendations",
        "media_binding_candidates",
        "recommended_binding_candidates_count",
        "auto_approved_bindings_recent",
        "media_takeover_gate",
        "list_engine_weight_candidates_endpoint",
        "answer_entities_summary_endpoint",
        "answer_entities_examples_endpoint",
        "answer_entities_health_endpoint",
        "flywheel_bridge_health_endpoint",
    ],
    "api/writing_style_flywheel_api.py": [
        "writing_strategy_versions",
        "writing_strategy_active",
        "writing_style_simulations_list",
        "writing_style_simulation_detail",
        "writing_strategy_audit",
    ],
}


def _parse(rel: str) -> ast.Module:
    with io.open(ROOT / rel, "r", encoding="utf-8", newline="") as f:
        return ast.parse(f.read(), filename=rel)


def _find_func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _is_route(node) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            if dec.func.attr in ("get", "post", "put", "delete", "patch"):
                return True
    return False


# ---------------------------------------------------------------- 2) 改法锁


@pytest.mark.parametrize(
    "rel,handler",
    [(rel, h) for rel, hs in CONVERTED.items() for h in hs],
)
def test_converted_endpoints_are_sync_def(rel: str, handler: str):
    """点名端点必须是同步 def —— 回退成 async def 就是把阻塞放回事件循环。"""
    tree = _parse(rel)
    node = _find_func(tree, handler)
    assert node is not None, f"{rel}::{handler} 不存在(改名了?锁要跟着改)"
    assert _is_route(node), f"{rel}::{handler} 不再是路由端点"
    assert isinstance(node, ast.FunctionDef), (
        f"{rel}::{handler} 又变回 async def —— 单 worker 下裸同步 DB 调用会占死事件循环"
    )


@pytest.mark.parametrize(
    "rel,handler",
    [(rel, h) for rel, hs in CONVERTED.items() for h in hs],
)
def test_converted_endpoints_have_no_await(rel: str, handler: str):
    """同步 def handler 里写 await 是静默失效(协程对象直接被当返回值)。"""
    node = _find_func(_parse(rel), handler)
    awaits = [n for n in ast.walk(node) if isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))]
    assert not awaits, f"{rel}::{handler} 是同步 def 却含 await(第 {[a.lineno for a in awaits]} 行)"


# ---------------------------------------------------------------- 3) AST 扫描锁


def _scan_keys() -> set[str]:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scan_async_endpoint_blocking_2026_08_01.py"), "--json"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"扫描器跑挂了:\n{proc.stderr[-2000:]}"
    data = json.loads(proc.stdout)
    return {f"{f['file']}::{f['handler']}::{h['call']}" for f in data for h in f["blocking_calls"]}


def _baseline() -> dict:
    p = ROOT / "tests" / "fixtures" / "async_blocking_baseline_2026_08_01.json"
    with io.open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def test_no_new_async_endpoint_blocking():
    """全站不得**新增** async 端点内裸同步重活(存量登记在基线白名单里)。"""
    baseline = set(_baseline()["known_blocking"])
    current = _scan_keys()
    new = sorted(current - baseline)
    assert not new, (
        f"新增 {len(new)} 处 async 端点内裸同步重活(单 worker 下会占死事件循环):\n"
        + "\n".join(f"  - {k}" for k in new[:30])
        + "\n修法:只读重端点改同步 def,或把同步段用 asyncio.to_thread 包起来。"
    )


def test_baseline_excludes_this_batch_fixes():
    """本批修好的 21 个 handler 不得再出现在基线里 —— 否则基线是"补录"而不是"收缩"。"""
    baseline = set(_baseline()["known_blocking"])
    for rel, handlers in CONVERTED.items():
        for h in handlers:
            leaked = [k for k in baseline if k.startswith(f"{rel}::{h}::")]
            assert not leaked, f"{rel}::{h} 已修好却仍在基线白名单里: {leaked}"


def test_baseline_is_not_stale():
    """基线不得登记已经不存在的项 —— 陈旧白名单会掩盖真实新增。

    允许 baseline ⊋ current(修好了会收缩),但差集里不能出现本批之外的大量幽灵项:
    只断言 current ⊆ baseline 的反向不成立时能看到具体差异(诊断友好)。
    """
    baseline = set(_baseline()["known_blocking"])
    current = _scan_keys()
    gone = baseline - current
    batch = {f"{rel}::{h}::" for rel, hs in CONVERTED.items() for h in hs}
    unexpected_gone = [k for k in gone if not any(k.startswith(p) for p in batch)]
    assert not unexpected_gone, (
        "基线里有既不属于本批修复、当前也扫不到的幽灵项(基线该重生成):\n"
        + "\n".join(f"  - {k}" for k in sorted(unexpected_gone)[:20])
    )


# ---------------------------------------------------------------- 1) 并发探针锁


def _build_probe_app(*, slow_is_async: bool):
    """起一个最小 FastAPI app:一个"重端点"(同步 sleep 桩 = 同步 DB 调用的替身)+ 一个轻端点。

    slow_is_async=True  → 重端点是 `async def` 里裸调同步 sleep(= 修复前的形态);
    slow_is_async=False → 重端点是同步 `def`(= 本批修法),FastAPI 自动送线程池。
    """
    from fastapi import FastAPI

    app = FastAPI()
    SLOW_SECONDS = 1.5

    def _blocking_db_stub():
        # 同步 psycopg2 调用的替身:占住当前线程,不 yield 给事件循环
        time.sleep(SLOW_SECONDS)
        return {"rows": 71000}

    if slow_is_async:
        @app.get("/heavy")
        async def heavy():                      # noqa: D401 — 反向对照:裸同步在协程里
            return _blocking_db_stub()
    else:
        @app.get("/heavy")
        def heavy():                            # 本批修法:同步 def → run_in_threadpool
            return _blocking_db_stub()

    @app.get("/light")
    async def light():
        return {"ok": True}

    return app


@contextlib.contextmanager
def _serve(app):
    """真起一个**单 worker** uvicorn(生产 WORKERS=1 铁律的同构环境),yield base_url。

    🔴 不能用 httpx.ASGITransport 在同一个事件循环里测:那样 t0 只有等循环空出来才执行,
    量到的是"服务端开始处理之后"的耗时,阻塞被计时起点本身抹掉(第一版探针就栽在这)。
    nginx 的 rt 是从**收到请求**算起的,所以必须真开 socket + 用独立线程计时。
    """
    import socket
    import threading

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", workers=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("探针 uvicorn 起不来")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def _measure_light_rt_under_load(app, *, heavy_concurrency: int = 8) -> float:
    """并发打 heavy 的同时(在别的线程里)打 light,返回 light 的最坏**墙钟**耗时(秒)。"""
    import httpx

    with _serve(app) as base:
        with cf.ThreadPoolExecutor(max_workers=heavy_concurrency + 6) as pool:
            def _heavy():
                with httpx.Client(base_url=base, timeout=120.0) as c:
                    return c.get("/heavy").status_code

            def _light():
                with httpx.Client(base_url=base, timeout=120.0) as c:
                    t0 = time.perf_counter()          # 计时起点 = 发请求那一刻(同 nginx rt)
                    r = c.get("/light")
                    dt = time.perf_counter() - t0
                    assert r.status_code == 200
                    return dt

            heavy_futs = [pool.submit(_heavy) for _ in range(heavy_concurrency)]
            time.sleep(0.30)                          # 让 heavy 先把 worker 占住
            light_futs = [pool.submit(_light) for _ in range(5)]
            worst = max(f.result() for f in light_futs)
            for f in heavy_futs:
                f.result()
    return worst


@pytest.mark.timeout(120)
def test_light_endpoint_not_starved_when_heavy_is_sync_def():
    """主锁:重端点是同步 def 时,并发下轻端点必须仍 < 1s。"""
    pytest.importorskip("httpx")
    app = _build_probe_app(slow_is_async=False)
    worst = _measure_light_rt_under_load(app)
    assert worst < 1.0, (
        f"轻端点在并发重查询下被拖到 {worst:.2f}s —— 事件循环被占住了(修法没生效)"
    )


@pytest.mark.timeout(120)
def test_probe_catches_naked_async_endpoint():
    """🔴 反向对照:把重端点还原成"async def 里裸调同步"(= 修复前形态),
    上面那条主锁的判据必须**转红**。证明探针有判别力,不是恒绿。
    """
    pytest.importorskip("httpx")
    app = _build_probe_app(slow_is_async=True)
    worst = _measure_light_rt_under_load(app)
    assert worst >= 1.0, (
        f"还原成裸 async 同步端点后,轻端点最坏仅 {worst:.2f}s —— 探针抓不到阻塞,"
        "主锁等于恒绿,必须修探针(并发不够 / sleep 太短 / 事件循环没被真正占住)"
    )
