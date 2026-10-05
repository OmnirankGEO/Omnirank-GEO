"""permission_version cache with a database-authoritative fail-closed fallback.

Redis is the cross-worker cache.  A Redis miss or outage must query PostgreSQL;
process-local memory is never trusted as an authorization decision because another
worker may already have committed a role change.
"""

import time
import logging
from typing import Dict, Tuple

from cache.redis_client import redis_get, redis_set, redis_delete, get_redis

logger = logging.getLogger("GEO-Auth")

# Local observation cache: useful for diagnostics/publication, never authoritative.
_mem_cache: Dict[int, Tuple[int, float]] = {}
CACHE_TTL = 60  # 秒

REDIS_KEY_PREFIX = "perm:"


def get_cached_permission_version(user_id: int) -> int:
    """
    获取 PostgreSQL 中当前 permission_version，并刷新观察缓存。

    Redis miss/outage always falls through to PostgreSQL.  Reading stale process
    memory here would let a removed administrator keep access on another worker.
    """
    # Redis/local values are observations only.  A successful Redis read can still
    # be stale when a revocation committed but publication failed, so PostgreSQL is
    # consulted on every authorization refresh.
    redis_key = f"{REDIS_KEY_PREFIX}{user_id}"
    now = time.time()
    from db.auth_db import get_user_permission_version
    version = get_user_permission_version(user_id)

    # 写入 Redis（TTL 60s）
    redis_set(redis_key, str(version), ex=CACHE_TTL)

    # 同时写入内存（降级用）
    _mem_cache[user_id] = (version, now)

    return version


def invalidate_cache(user_id: int):
    """
    立即失效指定用户的缓存
    在 admin 修改用户角色/权限时调用
    """
    redis_delete(f"{REDIS_KEY_PREFIX}{user_id}")
    _mem_cache.pop(user_id, None)


def publish_permission_version(user_id: int, version: int) -> bool:
    """Publish the committed version for diagnostics and faster soft refresh."""
    version = int(version)
    published = redis_set(f"{REDIS_KEY_PREFIX}{int(user_id)}", str(version), ex=CACHE_TTL)
    _mem_cache[int(user_id)] = (version, time.time())
    return published


def invalidate_all_cache():
    """清除所有缓存（全局权限变更时使用）"""
    # 清除 Redis 中所有 perm: 前缀的 key
    r = get_redis()
    if r is not None:
        try:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match=f"{REDIS_KEY_PREFIX}*", count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning(f"[Cache] Redis 批量清除失败: {e}")
    _mem_cache.clear()
