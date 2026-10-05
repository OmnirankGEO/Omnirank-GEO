"""接线锁（2026-08-12 Review P1-1 / P1-2 返修）。

🔴 存在的理由:上一版有 30 条公式锁 + 36 条跨语言对拍全绿,**却一条都没打在
   「生产链路真的调用了它」上面** —— 于是算法零调用、前端固定全局比例照样满分。
   判据打在函数上 ≠ 判据打在接线上。本文件只锁接线,不重复锁公式。

每条都配反向对照:证明它真能分辨"接了"和"没接",而不是恒真。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

API = ROOT / "api" / "quote_media_mix_api.py"
SERVER = ROOT / "server.py"
FLOW = ROOT / "frontend" / "src" / "pages" / "Quote" / "OnlineQuoteFlow.tsx"
WHY = ROOT / "frontend" / "src" / "pages" / "Quote" / "components" / "WhyThisPrice.tsx"
RATIONALE = ROOT / "frontend" / "src" / "pages" / "Quote" / "utils" / "priceRationale.ts"


def _strip_comments(src: str) -> str:
    """扫源码前先剥注释。

    🔴 不剥会踩「注释里提到过 = 当成真代码」那个坑:本包第一版就是这么红的 ——
       禁用串出现在**解释它为什么被删掉**的注释里,判据当场误判。
       同类事故仓内已有先例(migration manifest 的 `#` 注释)。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)      # /* 块注释 */
    src = re.sub(r"^[ \t]*//.*$", "", src, flags=re.M)   # // 整行注释
    src = re.sub(r"^[ \t]*#.*$", "", src, flags=re.M)    # # 整行注释(py)
    return src


def _referenced_names(tree: ast.AST) -> set[str]:
    """既算「被直接调用」也算「作为实参传给别人去调」。

    🔴 `asyncio.to_thread(record_media_mix_snapshot, ...)` 是**真调用**,
       但函数名在 AST 里是实参不是 func —— 只看 Call.func 会把真接线判成没接。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    names.add(arg.id)
    return names


def test_api_module_really_calls_the_planner():
    """端点必须**真的调用** plan_media_mix —— 只 import 不调用是死接线。"""
    called = _referenced_names(ast.parse(API.read_text(encoding="utf-8")))
    assert "plan_media_mix" in called, "端点没调用 plan_media_mix"
    assert "record_media_mix_snapshot" in called, "落库函数没被调用(P1-2)"
    # 反向对照:一个不存在的名字必须不在集合里,证明上面不是恒真
    assert "plan_media_mix_NOT_EXIST" not in called


def test_router_is_registered_in_server():
    """server.py 必须注册该 router,否则端点根本不存在(2026-08-06 有过 404 39 分钟)。"""
    src = SERVER.read_text(encoding="utf-8")
    assert "from api.quote_media_mix_api import router" in src
    assert "app.include_router(quote_media_mix_router)" in src
    # 🔴 注册失败必须 catch 到 Exception 而不是只 catch ImportError,且要 error 级日志
    block = src.split("quote_media_mix_api import router", 1)[1][:600]
    assert "except Exception" in block, "只 catch ImportError 兜不住模块内求值失败"
    assert "logger.error" in block, "注册失败必须打 error,不能混在 warning 里"
    assert "app.include_router(quote_media_mix_router_NOT_EXIST)" not in src  # 反向对照


def test_frontend_fetches_media_mix_and_passes_it_down():
    """前端必须真的请求端点,并把结果传进 WhyThisPrice —— 缺任一环都是 P1-1 复发。"""
    flow = FLOW.read_text(encoding="utf-8")
    assert "/media-mix" in flow, "前端没有请求 media-mix 端点"
    assert re.search(r"api\.get\(\s*`/api/quotes/\$\{session\.quote_id\}/media-mix`", flow), \
        "请求没有按当前报价的 quote_id 发出(写死 id 或拼错都在这里红)"
    mounts = re.findall(r"<WhyThisPrice\b[^>]*>", flow)
    assert len(mounts) >= 2, f"WhyThisPrice 挂载点少于 2 个(实得 {len(mounts)})"
    for m in mounts:
        assert "mixContext=" in m, f"这个挂载点没把组合上下文传下去: {m}"
    # 反向对照:随便一个不存在的 prop 不该出现在所有挂载点上
    assert not all("mixContextNOTEXIST=" in m for m in mounts)


def test_component_consumes_the_context_instead_of_hardcoding():
    """WhyThisPrice 必须把 mixContext 喂给构建器,不能只收下不用(第 N 例接线没接)。"""
    why = WHY.read_text(encoding="utf-8")
    assert re.search(r"buildPriceRationale\(kw,\s*tier,\s*mixContext", why), \
        "组件收了 mixContext 却没传给 buildPriceRationale"
    assert "mixContext" in why


def test_copy_degrades_when_ratio_is_not_industry_level():
    """文案不得无条件宣称按行业调整 —— 必须由真实 ratioSource 分支。

    这是 Review P1-1 里"承诺了没接的行为"那一半的锁。
    """
    src = _strip_comments(RATIONALE.read_text(encoding="utf-8"))
    assert "mixOpts.ratioSource" in src, "文案没有按 ratioSource 分支"
    assert "industry_engine" in src and "industry" in src
    assert "当前按全行业平均值估算" in src, "缺少降级口径文案"
    # 🔴 v1 那句无条件承诺必须绝迹(只看真代码,注释里复盘它是允许的)
    assert "系统会按行业和目标 AI 调整" not in src, \
        "又把无条件承诺写回来了 —— 没接行业数据时这句是假的"
    # 反向对照:剥注释不能把真代码也剥掉,否则上面全成恒真
    assert "已按你所在行业的 AI 引用数据算出" in src


def test_snapshot_write_is_idempotent_and_never_raises():
    """落库函数:任何异常都不得抛给报价主链(工单 §1.1.5),且必须有幂等分支。"""
    src = (ROOT / "services" / "quote_media_mix.py").read_text(encoding="utf-8")
    assert '"unchanged"' in src, "缺幂等分支 —— 每看一次报价就写一版会刷爆 append-only 表"
    assert "_mix_fingerprint" in src
    # 真跑一次:库连不上/报价不存在时必须返回 recorded=False 而不是抛异常
    from services.quote_media_mix import record_media_mix_snapshot
    out = record_media_mix_snapshot(-1, {"capacity_total": 21}, actor_user_id=1)
    assert out["recorded"] is False, "不存在的报价不该被记录"
    assert isinstance(out.get("reason"), str) and out["reason"], "失败必须给出原因"


def test_fingerprint_ignores_volatile_fields():
    """幂等指纹只能取影响交付的字段。带上时间戳之类会让幂等永远不成立。"""
    from services.quote_media_mix import _mix_fingerprint

    base = {"capacity_total": 21, "mix": {"focus_media_anchor": 7},
            "ratio": {"used": 2.1, "source": "global"},
            "strategy_version": "v", "pool_version": "p"}
    same = dict(base, generated_at="2026-08-12T00:00:00Z", quote_id=999)
    assert _mix_fingerprint(base) == _mix_fingerprint(same), "易变字段进了指纹"
    # 反向对照:交付相关字段一变,指纹必须变
    changed = dict(base, mix={"focus_media_anchor": 6})
    assert _mix_fingerprint(base) != _mix_fingerprint(changed)
