"""V7 · 写作飞轮看板 · 进程内短 TTL 缓存(叶子模块)。

背景(SPEC V7-P1):看板多个 GET(全景/进化看板/结构分析/趋势/outcome 读)是最重聚合,
跨 tab 反复裸拉 = 体感越"智能"越慢。此模块给这些只读聚合加进程内短 TTL 缓存 + 写动作失效钩子。

设计铁律:
- **叶子模块**:只 import 标准库,绝不 import services.* 或 writing.style_control —— 否则成环
  (services/flywheel_panorama.py、services/writing_evolution_board.py 都已 import writing.style_control)。
- **WORKERS=1**(CLAUDE.md 真相表 #1):进程级 dict 一致且安全;若未来扩多 worker,缓存与失效变
  per-worker(读计数可接受,跨 worker 失效不生效)—— 到时需换共享缓存,这里注释留痕。
- **只缓存只读聚合**,绝不缓存写路径 / 乐观锁读(config_version)/ 真回写。
- 数字永远真实:缓存只是"同一份真实聚合复用几十秒",不改任何数值。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("GEO-WritingFlywheel.Cache")

# key -> (expires_at_epoch, value)
_STORE: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()
# 失效代际:每次 invalidate/clear 自增。get_or_compute 在 compute(锁外)前记录代际,
# 回填时若代际已变(期间发生过失效)则**不写**,避免慢 compute 拿到的旧值在失效后被回填成 stale。
_generation = 0

# 缓存命名空间(失效钩子按 scope 前缀清理)。key 形如 "<scope>:<参数...>"。
SCOPE_BOARD = "board"          # get_evolution_board(industry)
SCOPE_PANORAMA = "panorama"    # get_flywheel_panorama()
SCOPE_TRENDS = "trends"        # get_flywheel_trends(weeks)
SCOPE_OUTCOME = "outcome"      # backfill_writing_outcomes(dry_run=True, ...) 只读聚合
SCOPE_HEALTH = "health"        # get_flywheel_data_health(industry)
SCOPE_INSIGHT = "insight"      # V5 flywheel-insight 总汇总
SCOPE_STRUCTURE = "structure"  # analyze_article_structure_patterns / V4 规则
SCOPE_ENTITY_RANK = "entity_rank"  # E1 行业媒体有效性榜(L3 答案实体主榜 · 只读聚合)· 🔴 不进 CONTROL_DERIVED_SCOPES
# [504 治理 2026-08-18] services.article_data_health.get_article_data_health() —— 生产实测单次 4.2s,
#   且**同一次页面加载被算两遍**(evolution-board 一次 + flywheel-insight 的 _safe_health 一次),
#   自身无任何缓存。它只读文章/监测真相链,与 style-control 写动作无关 → 不进 CONTROL_DERIVED_SCOPES,
#   靠 TTL 兜新鲜度(与 SCOPE_HEALTH 同待遇)。
#   🔴 与 SCOPE_HEALTH 分开:那个是 media_entity 的 get_flywheel_data_health(industry),不是同一个函数,
#      共用 scope 会让两者互相污染/互相失效。
SCOPE_ARTICLE_HEALTH = "article_health"

# 6 写动作(style-control _save_state 覆盖的全部)+ 模拟/评审/回写完成时,应失效的 control-state 派生 scope。
# 说明:panorama/trends/health 的 SQL 侧(geo_research_raw/监测入库)不受写动作影响,靠 TTL 到期兜新鲜度即可,
# 但 panorama 的 pending/active 版本数依赖 control-state,故一并纳入失效。
CONTROL_DERIVED_SCOPES = (SCOPE_BOARD, SCOPE_PANORAMA, SCOPE_OUTCOME, SCOPE_INSIGHT, SCOPE_STRUCTURE)


def make_key(scope: str, *parts: Any) -> str:
    """构造缓存 key:scope + 归一化参数。None 归一为 'all',保证同一逻辑请求命中同 key。"""
    norm = [str(p) if p is not None and str(p) != "" else "all" for p in parts]
    return ":".join([scope, *norm]) if norm else scope


# [504 治理 2026-08-18] single-flight:key -> Event(领跑者在算,后到者等它)。
# 背景(生产实测):飞轮页一次冷加载并发打 6 个只读聚合,其中 flywheel-insight 内部**又**要
#   panorama / board / trends / outcome 四份同样的聚合。旧实现「并发 miss 各自 compute 一次」
#   → 同一份重聚合在同一秒被算 2-3 遍,WORKERS=1 下这些 CPU-bound 聚合互相抢 GIL,
#   实测冷并发 wall 9.35s vs 预热后 3.87s(2.4x)。历史 504 正是这种放大把慢时刻推过 60s 网关闸。
# 判据成对:去掉本段 → 冷并发 wall 回到 ~9.3s(锁能拆红)。
_INFLIGHT: dict[str, threading.Event] = {}
# 领跑者等待上限:后到者最多等这么久,超时就自己算(退化回旧行为)。
# 🔴 不设无限等待 —— 否则一个 hang 住的 compute 会把所有后到者一起钉死,
#    把「一个端点慢」升级成「整页挂」,那比 thundering-herd 更糟。
_INFLIGHT_WAIT_SEC = 20.0


def get_or_compute(key: str, ttl: float, compute: Callable[[], Any]) -> Any:
    """命中未过期 → 直接返回;否则 compute()(在锁外,避免慢聚合长期持锁)后回填。

    thundering-herd:同 key 并发 miss 走 single-flight —— 第一个进来的算,其余等它的结果
    (最多等 _INFLIGHT_WAIT_SEC;超时/领跑者失败则自己算,行为退化回旧版,绝不比旧版更差)。
    compute 抛异常 → 不写缓存、异常上抛(由调用方 fail-soft),不会污染缓存为坏值。
    """
    now = time.time()
    with _LOCK:
        hit = _STORE.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        gen0 = _generation
        waiter = _INFLIGHT.get(key)
        leader = waiter is None
        if leader:
            _INFLIGHT[key] = threading.Event()

    if not leader:
        # 后到者:等领跑者回填。等到了就直接用它的值(省掉一次重复的重聚合)。
        waiter.wait(_INFLIGHT_WAIT_SEC)
        with _LOCK:
            hit = _STORE.get(key)
            if hit is not None and hit[0] > time.time():
                return hit[1]
            # 领跑者失败 / 超时 / 期间被 invalidate → 自己算(退化回旧行为)
            gen0 = _generation
        return compute()

    # 顺序:先回填 _STORE,再 set 事件 —— 让被唤醒的后到者一定查得到值。
    # ⚠️ 诚实标注:这条顺序**没有对应的锁**。2026-08-18 实测把顺序反过来(先 set 后回填)
    #    跑 25 轮 × 6 并发,额外 compute 次数仍是 0 —— 领跑者 set 完立刻重进 _LOCK,
    #    GIL 下后到者根本抢不到中间那一瞬。所以两种顺序在本实现里**不可观测**,
    #    写不出能转红的判据(硬写就是个 flaky 锁,比没有更糟)。
    #    之所以仍按这个顺序写:它是两者中防御性正确的那个;且即便顺序反了,后到者也有
    #    "查不到就自己算"的兜底,**值永远是对的**,受影响的只是省不省得下一次重算。
    try:
        value = compute()
    except BaseException:
        with _LOCK:
            ev = _INFLIGHT.pop(key, None)
        if ev is not None:
            ev.set()  # 失败也必须放行等待者(它们会自己算),绝不留下永远等不到的后到者
        raise
    with _LOCK:
        if _generation == gen0:  # 期间无 invalidate 才回填(否则丢弃这次 stale 回填,下次重算)
            _STORE[key] = (now + max(1.0, float(ttl)), value)
        ev = _INFLIGHT.pop(key, None)
    if ev is not None:
        ev.set()
    return value


def invalidate(scopes: "tuple[str, ...] | list[str] | None" = None) -> int:
    """清除指定 scope 前缀的所有 key;scopes=None → 全清。返回清除条数。fail-soft:绝不抛。"""
    global _generation
    try:
        with _LOCK:
            _generation += 1  # 代际自增 → 正在 compute 的 stale 回填将被丢弃
            if not scopes:
                n = len(_STORE)
                _STORE.clear()
                return n
            prefixes = tuple(f"{s}:" for s in scopes) + tuple(scopes)  # 兼容无参 key(scope 本身)
            doomed = [k for k in _STORE if k.startswith(prefixes) or k in scopes]
            for k in doomed:
                _STORE.pop(k, None)
            return len(doomed)
    except Exception as exc:  # 失效永不破坏写路径
        logger.warning("[flywheel_cache] invalidate 失败(忽略): %s", str(exc)[:200])
        return 0


def invalidate_control_derived() -> int:
    """写动作(采纳/驳回/回滚/启用/退役/蒸馏 draft + 模拟/评审/真回写)完成后调用。"""
    return invalidate(CONTROL_DERIVED_SCOPES)


def stats() -> dict[str, int]:
    """诊断用:当前缓存条数 + 各 scope 计数(不含值,便于日志/自检)。"""
    with _LOCK:
        by_scope: dict[str, int] = {}
        for k in _STORE:
            by_scope[k.split(":", 1)[0]] = by_scope.get(k.split(":", 1)[0], 0) + 1
        return {"total": len(_STORE), **by_scope}


def clear_all() -> None:
    """测试/重置用:清空全部缓存。"""
    global _generation
    with _LOCK:
        _generation += 1
        _STORE.clear()
