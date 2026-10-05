"""B3 媒体组合判定 · 结果缓存 + 异步预热(2026-07-28 · recommend-v2 性能第二刀 §B)。

为什么需要这一层
----------------
`refine_media_combination` 挂在 `/api/publish/media/recommend-v2` 这条**热路径**上:
代理每打开一次推荐页、每切一次行业,就在请求里同步发一次真实 LLM 请求
(生产 cProfile:``judge`` 1.79s → ``_call_one`` 1.78s → ``httpx.post`` 1.65s)。
延迟是小事,**每次页面加载都在花钱**是大事。

生产实证(``flywheel_judgment_log`` · 截至 2026-07-28 19:31):

* 131 次 ``judge()`` 入口(103 llm + 28 rule),**成对出现**——
  端点里 media / wemedia 两次 ``recommend_for_publish_v2`` 入参只差 ``media_type``,
  而问题族 mix 与 ``media_type`` 无关,于是同一秒发两次**完全一样**的判定,
  其中一份端点根本不读(``publish_api`` 只取 ``media_result`` 的 T2 块);
* 131 次只覆盖 **15 个不同 (industry, keyword)** —— 也就是说 ~89% 是重复算的;
* 当日 101 次真实 LLM 之后触顶 ``daily_call_cap_reached(101/100)``,
  19:03 起当天剩余时间该功能全部降级规则兜底。**灰度第一天、6 个活跃用户就打满了**,
  这不是"贵不贵"的问题,是"再来十倍用户这个功能就基本不工作"的问题。

于是这一层做两件事(工单 §B):

1. **请求路径只读缓存,永不在请求里现算**;
2. 未命中 → 由调用方返回代码算好的确定性组合(= 判断点关闭时的既有行为,不劣化),
   同时投递一次后台预热,下次进来就有了。

存储为什么复用 ``flywheel_judgment_log``
----------------------------------------
判定结果本来就**每次都落这张表**(``judge()`` 的留痕硬约束):
一行 = ``(point_key, prompt_version, input_summary, output, created_at)``,
缓存需要的字段一个不缺。复用它换来三件事:

* **不加表 / 不加迁移 / 不动 schema 指纹** —— 2026-07-28 刚刚出过一次自愈式建列
  导致容器启动门与部署门双双 fail-closed 的 P0,这一单没有任何理由再去碰 schema;
* 蓝绿两个实例 + cron 容器天然共享同一份缓存(进程内 dict 做不到);
* 缓存与留痕不会打架 —— 它们本来就是同一份事实,不存在"日志说发过、缓存说没发过"。

SQL 4 维核验(只读,不建任何对象):
  1. 列名:``point_key`` / ``source`` / ``prompt_version`` / ``input_summary`` /
     ``output`` / ``error`` / ``created_at`` —— 均为 ``db/flywheel_judgment_db.py``
     建表语句里的既有列,逐字对照;
  2. data_type:``input_summary`` / ``output`` 为 JSONB(用 ``->>`` 取文本),
     ``created_at`` 为 TIMESTAMPTZ(与 ``NOW() - make_interval`` 同类型可比);
  3. 字段归属:全部在 ``flywheel_judgment_log`` 这一张表,不 JOIN、不碰计费表;
  4. dry-run:纯 ``SELECT``,无任何写入/DDL。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("GEO-FlywheelMediaMixCache")

#: 缓存 TTL。默认 24 小时 —— 底层 mix 是 **180 天滚动窗口**的聚合,日间几乎不动
#: (改一天数据要 180 天分之一的量级才看得出),所以一天复用一次判定完全够用;
#: 而它同时把每日 LLM 调用量的上界从"请求量"钉到"不同行业数"(生产实测 15 个)。
#: 仓内 ``keyword_price_cache`` 的 7 天价格锁是同一范式(价格稳定感 ≥ 精确度),
#: 这里取更短的 24h,是因为判定要跟着补货/缺货变化走,不需要锁那么死。
DEFAULT_TTL_SECONDS: int = 24 * 3600

#: 预热并发上限。热路径未命中时只投递,不等待;同时最多 2 个后台判定在跑,
#: 防止某个行业刚上线时几十个请求一起把线程和额度打爆。
MAX_INFLIGHT_WARMS: int = 2

#: 同一 key 两次预热之间的最小间隔。预热失败(例如当日额度已满)时靠它节流,
#: 否则每个请求都会发现"还是没缓存"→ 又投一次 → 把额度查询和留痕表刷成噪音。
WARM_COOLDOWN_SECONDS: float = 600.0

_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_COOLDOWN: dict[str, float] = {}


def ttl_seconds() -> int:
    """TTL 可用环境变量覆盖,便于灰度期调参而不必改代码重新部署。"""
    raw = os.getenv("FLYWHEEL_MEDIA_MIX_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("[B3-cache] TTL 环境变量不是整数(%r),回落默认 %ss", raw, DEFAULT_TTL_SECONDS)
        return DEFAULT_TTL_SECONDS
    # 0 / 负数 = 关缓存(每次都算作未命中 → 走后台预热),给紧急排查留一个开关。
    return max(0, min(30 * 24 * 3600, value))


def cache_key(*, point_key: str, industry: str, keyword: str, total_slots: int,
              prompt_version: str) -> str:
    """预热去重键。**与 SQL 的过滤条件逐字段一一对应**,不能只用其中一部分 ——
    否则「按 industry 去重、按 (industry, slots) 查」会出现永远预热不到的组合。"""
    return "|".join((
        str(point_key or ""), str(prompt_version or ""),
        str(industry or "").strip(), str(keyword or "").strip(), str(int(total_slots or 0)),
    ))


def lookup_choice(
    *,
    point_key: str,
    industry: str,
    keyword: str,
    total_slots: int,
    prompt_version: str,
) -> Optional[dict[str, Any]]:
    """读一条仍在 TTL 内的、结构合法的 LLM 判定结果。

    返回 ``{"payload": <模型输出>, "age_seconds": int}``;没有可用结果返回 ``None``。
    **任何异常都返回 None**(= 按未命中处理)—— 缓存层不可用只该让判定少一层智能,
    绝不能让发布推荐这条主路径出错。
    """
    ttl = ttl_seconds()
    if ttl <= 0:
        return None
    try:
        from db.connection import get_connection

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT output,
                       EXTRACT(EPOCH FROM (NOW() - created_at))::bigint AS age_seconds
                  FROM flywheel_judgment_log
                 WHERE point_key = %s
                   AND source = 'llm'
                   AND error IS NULL
                   AND prompt_version = %s
                   AND created_at >= NOW() - make_interval(secs => %s)
                   AND input_summary->>'industry' = %s
                   AND input_summary->>'keyword' = %s
                   AND input_summary->>'total_slots' = %s
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (
                    str(point_key)[:60], str(prompt_version or "")[:40], int(ttl),
                    str(industry or "").strip(), str(keyword or "").strip(),
                    str(int(total_slots or 0)),
                ),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[B3-cache] 缓存查询失败,按未命中处理: %s", exc)
        return None

    if not row:
        return None
    payload = row.get("output")
    if not isinstance(payload, dict):
        # 留痕表里也有"结构不合预期"那类行(带 error),已经被 SQL 挡掉;
        # 这里再兜一层,防止 output 是标量/列表时把畸形结构喂给下游。
        return None
    return {"payload": payload, "age_seconds": int(row.get("age_seconds") or 0)}


def submit_warm(key: str, runner: Callable[[], Any]) -> bool:
    """投递一次后台预热。已在跑 / 冷却中 / 并发已满 → 不投,返回 ``False``。

    冷却在**投递时**就打上(不是完成时):成功的话下次就命中缓存、冷却无关紧要;
    失败的话(例如当日额度已满)它正好把重试压到 10 分钟一次。
    """
    now = time.monotonic()
    with _LOCK:
        if key in _INFLIGHT:
            return False
        if now < _COOLDOWN.get(key, 0.0):
            return False
        if len(_INFLIGHT) >= MAX_INFLIGHT_WARMS:
            return False
        if len(_COOLDOWN) > 500:  # 长跑进程里别让冷却表无限长
            _COOLDOWN.clear()
        _INFLIGHT.add(key)
        _COOLDOWN[key] = now + WARM_COOLDOWN_SECONDS

    def _run() -> None:
        try:
            runner()
        except Exception as exc:  # pragma: no cover - 后台线程绝不能把异常抛到解释器
            logger.warning("[B3-cache] 预热失败 key=%s: %s", key, exc)
        finally:
            with _LOCK:
                _INFLIGHT.discard(key)

    threading.Thread(target=_run, name="b3-mix-warm", daemon=True).start()
    logger.info("[B3-cache] 已投递后台预热 key=%s", key)
    return True


def inflight_count() -> int:
    """给测试/排查用:当前在跑的预热数。"""
    with _LOCK:
        return len(_INFLIGHT)


def reset_state() -> None:
    """给测试用:清空 in-flight 与冷却表。生产不调用。"""
    with _LOCK:
        _INFLIGHT.clear()
        _COOLDOWN.clear()
