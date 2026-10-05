"""配置纪元(config epoch)—— 蓝绿容器跨进程缓存失效(P0-17 / §16 Q28)

问题:pricing_config / platform_fee_config 是进程内缓存(30s/60s TTL),reload_*() 只清
本进程缓存。蓝绿双容器里,admin 在 A 容器改价并 reload,B 容器仍供旧价直到 TTL 到。

方案(不引 redis / 不碰 db/connection.py / 不起后台线程):
  - system_settings 存一个整数 'pricing_config_epoch'。
  - 任一价格/费率配置写入后 bump_epoch()(原子 +1)。
  - 各缓存持有 (value, loaded_epoch, ts)。读取时:
      * 若距上次纪元探测 < _EPOCH_PROBE_TTL(2s)→ 直接用缓存(零 DB)。
      * 否则做一次极廉价 PK 读拿当前 epoch:
          - epoch 未变 → 续期缓存(不重读整份配置)。
          - epoch 变了 → 强制 loader() 重读并回读验证生效版本。
  - 跨容器最坏陈旧被压到 ~2s,且改价后所有容器下次探测即失效重读。

这是对现有 30s/60s TTL 的收紧,不改变默认关行为(未 bump 时纪元恒定,行为=旧缓存)。
"""

import logging
import threading
import time
from typing import Any, Callable, Optional, Tuple

from db.connection import get_db

logger = logging.getLogger("GEO-ConfigEpoch")

_EPOCH_KEY = "pricing_config_epoch"
_EPOCH_PROBE_TTL = 2.0  # 秒:两次纪元探测的最小间隔(压 DB 负载)

_epoch_cache: Optional[int] = None
_epoch_cache_ts: float = 0.0
_lock = threading.Lock()


def read_config_epoch(force: bool = False) -> int:
    """读当前配置纪元(极廉价 PK 读 · 2s 内复用)。DB 异常返回上次值 / 0(不阻塞)。"""
    global _epoch_cache, _epoch_cache_ts
    now = time.time()
    if not force and _epoch_cache is not None and (now - _epoch_cache_ts) < _EPOCH_PROBE_TTL:
        return _epoch_cache
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM system_settings WHERE key = %s", (_EPOCH_KEY,))
            row = cur.fetchone()
            val = int(row["value"]) if row and str(row["value"]).lstrip("-").isdigit() else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("read_config_epoch 失败 · 用上次值 %s · %s", _epoch_cache, exc)
        return _epoch_cache if _epoch_cache is not None else 0
    with _lock:
        _epoch_cache = val
        _epoch_cache_ts = now
    return val


def read_config_epoch_strict(cursor=None) -> int:
    """资金/改价事务使用：DB 失败或非法值直接抛错，绝不回落缓存。"""
    def _read(cur) -> int:
        cur.execute("SELECT value FROM system_settings WHERE key = %s", (_EPOCH_KEY,))
        row = cur.fetchone()
        raw = row.get("value") if isinstance(row, dict) else (row[0] if row else "0")
        text = str(raw if raw is not None else "0")
        if not text.isdigit():
            raise RuntimeError("pricing_config_epoch 非法")
        return int(text)

    if cursor is not None:
        return _read(cursor)
    with get_db() as conn:
        return _read(conn.cursor())


def bump_epoch_cursor(cursor) -> int:
    """在调用方现有事务内原子 bump；失败由调用方回滚。"""
    cursor.execute(
        """
        INSERT INTO system_settings (key, value, value_type, description)
        VALUES (%s, '1', 'integer', '价格配置纪元 · 每次改价 +1 · 蓝绿跨容器缓存失效')
        ON CONFLICT (key) DO UPDATE
            SET value = (COALESCE(NULLIF(system_settings.value, '')::bigint, 0) + 1)::text
        RETURNING value
        """,
        (_EPOCH_KEY,),
    )
    row = cursor.fetchone()
    value = int(row.get("value") if isinstance(row, dict) else row[0])
    if value < 1:
        raise RuntimeError("pricing_config_epoch bump 失败")
    return value


def accept_committed_epoch(value: int) -> None:
    """事务提交后更新本进程探测缓存；不访问 DB、不再次 bump。"""
    global _epoch_cache, _epoch_cache_ts
    with _lock:
        _epoch_cache = int(value)
        _epoch_cache_ts = time.time()


def bump_epoch() -> int:
    """价格/费率配置写入后调用:纪元原子 +1 → 通知所有容器下次探测即失效。"""
    global _epoch_cache, _epoch_cache_ts
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO system_settings (key, value, value_type, description)
                VALUES (%s, '1', 'integer', '价格配置纪元 · 每次改价 +1 · 蓝绿跨容器缓存失效')
                ON CONFLICT (key) DO UPDATE
                    SET value = (COALESCE(NULLIF(system_settings.value, '')::bigint, 0) + 1)::text
                RETURNING value
                """,
                (_EPOCH_KEY,),
            )
            new_val = int(cur.fetchone()["value"])
        with _lock:
            _epoch_cache = new_val
            _epoch_cache_ts = time.time()
        logger.info("[ConfigEpoch] bump → %s", new_val)
        return new_val
    except Exception as exc:  # noqa: BLE001
        logger.error("bump_epoch 失败(改价缓存失效可能延迟到 TTL): %s", exc)
        return _epoch_cache if _epoch_cache is not None else 0


class EpochCache:
    """纪元感知的进程内缓存包装。loader() 重读整份配置;纪元变即失效。

    用法:
        _c = EpochCache(load_fn=_load_pricing_config, ttl=30)
        cfg = _c.get()
    """

    def __init__(self, load_fn: Callable[[], Any], ttl: float = 30.0):
        self._load = load_fn
        self._ttl = ttl
        self._value: Any = None
        self._loaded_epoch: Optional[int] = None
        self._ts: float = 0.0
        self._lk = threading.Lock()

    def get(self) -> Any:
        now = time.time()
        if self._value is not None and (now - self._ts) < self._ttl:
            # TTL 内:仍做纪元探测(2s 内复用)以捕捉跨容器改价
            if read_config_epoch() == self._loaded_epoch:
                return self._value
        cur_epoch = read_config_epoch(force=True)
        with self._lk:
            if self._value is not None and cur_epoch == self._loaded_epoch and (now - self._ts) < self._ttl:
                return self._value
            self._value = self._load()
            self._loaded_epoch = cur_epoch
            self._ts = now
            return self._value

    def invalidate(self) -> None:
        with self._lk:
            self._value = None
            self._ts = 0.0
            self._loaded_epoch = None
