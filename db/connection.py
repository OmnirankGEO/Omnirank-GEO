"""
统一数据库连接模块
提供 PostgreSQL 连接池，返回 RealDictCursor 兼容原 sqlite3.Row 行为

所有 db/*.py 文件的 get_connection() 统一导入此模块
"""

import os
import logging
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from dotenv import load_dotenv

logger = logging.getLogger("GEO-DB")

_env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(_env_path)

DATABASE_URL = os.getenv('DATABASE_URL')

# ========== 连接池 ==========
# 2026-06-10 订正(audit P3 · 去除失实注释):现实为 WORKERS=1 单 worker、PgBouncer 已下线、
#   blue/green 直连 PG。psycopg2 ThreadedConnectionPool 的 putconn 只把空闲连接保留到 minconn 条,
#   超出的直接物理 close —— 即"min=2/max=30"实际长期只复用 minconn(=2)条最热连接。
#   (P0-5 已修:每次 getconn 强制复位 autocommit,这 2 条保留连接不再是污染载体。)
#   [并发-4 2026-06-10 打广告高并发] minconn 2→15 / maxconn 30→60:让保留池真实存在
#   (原长期只复用 2 条最热连接 → 并发 >2 每请求新建 TCP+认证+backend fork = 连接风暴)。
#   容量实证:PG max_connections=500、当前用量~10。蓝绿双实例 minconn 15×2=30 常驻 /
#   maxconn 60×2=120 峰值,远 < 500 安全。生效需 force-recreate 容器(进程重启读)。
# 旧注释(已废 · 勿据此做容量决策):min=2/max=30、4 worker×30=120、PgBouncer MAX_CLIENT_CONN=500。
_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                'DATABASE_URL 未配置。请在 .env 中设置 '
                'DATABASE_URL=postgresql://user:pass@host:port/dbname'
            )
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=5,
            maxconn=25,
            dsn=DATABASE_URL,
        )
        logger.info("[DB] 连接池已初始化 (min=5, max=25)")
    return _pool


def get_connection():
    """从连接池获取连接，调用者必须 conn.close() 归还（连接池会自动回收）"""
    pool = _get_pool()
    conn = pool.getconn()
    # 确保使用 RealDictCursor
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def put_connection(conn):
    """显式归还连接到连接池（conn.close() 在池化模式下也会归还）"""
    pool = _get_pool()
    try:
        pool.putconn(conn)
    except Exception:
        pass


def close_pool():
    """关闭连接池（应用退出时调用）"""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("[DB] 连接池已关闭")


# 重写 conn.close() → 归还到池
class _PooledConnection:
    """包装连接，close() 时归还到池而非真正关闭"""
    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        """显式关闭：归还连接到池，不真的关 socket"""
        if self._conn is None:
            return  # 已经 close 过，幂等
        try:
            self._pool.putconn(self._conn)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
        finally:
            self._conn = None   # 标记已归还，防止 __del__ 再次 putconn

    def __del__(self):
        """2026-04-18: GC 兜底 —— 代码忘 close() 时自动归还池槽位

        全项目有 ~720 处 get_connection() 没用 try-finally 保护。
        异常路径下连接不归还，池 slots 会被"虚假占用"撑满。
        加 __del__ 让 GC 自动归还，把永久泄漏降级为"延迟归还"。
        """
        try:
            if self._conn is not None:
                try:
                    self._pool.putconn(self._conn)
                except Exception:
                    # pool 已关闭或 conn 已异常 → 直接关 socket
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                self._conn = None
        except Exception:
            # __del__ 绝不能抛异常，否则 GC 报错
            pass

    @property
    def autocommit(self):
        return self._conn.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self._conn.autocommit = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


# 替换 get_connection 为池化版本
_orig_get_connection = get_connection

def get_connection():
    """从连接池获取包装连接，close() 自动归还池。指数退避，最长 10 秒超时。"""
    pool = _get_pool()
    import time
    start = time.monotonic()
    max_wait = 10.0
    attempt = 0
    while True:
        try:
            raw = pool.getconn()
            if getattr(raw, "closed", 1):
                try:
                    pool.putconn(raw, close=True)
                except Exception:
                    try:
                        raw.close()
                    except Exception:
                        pass
                continue
            raw.cursor_factory = psycopg2.extras.RealDictCursor
            # [P0-5 autocommit 污染根治 · 2026-06-10 audit BUG] psycopg2 ThreadedConnectionPool
            # 的 putconn 只 rollback、不重置 autocommit 属性;minconn 保留槽的连接一旦被某处
            # 置 autocommit=True 后归还,会被 LIFO 永久复用为"污染连接",导致拿到它的请求里
            # SELECT...FOR UPDATE 行锁在语句结束即释放、commit/rollback 变 no-op —— 全站悲观锁、
            # 幂等闸、多步资金写的原子性被系统性无声解除。每次取出强制复位为 False 即根治。
            # 注:故意设 autocommit=True 的 init/只读函数(diagnosis_db 等)拿到连接后会自己再设 True,
            # 不依赖"初始即 autocommit",不受此复位影响;它们归还的污染也由此处兜底。
            if raw.autocommit:
                raw.autocommit = False
            return _PooledConnection(raw, pool)
        except (psycopg2.pool.PoolError, psycopg2.OperationalError) as e:
            elapsed = time.monotonic() - start
            if elapsed >= max_wait:
                logger.error(f"[DB] 连接池耗尽 {elapsed:.1f}秒 ({attempt}次重试): {e}")
                raise RuntimeError(f"数据库连接池已满（等待{elapsed:.1f}秒），请检查连接泄漏或增加池大小")
            # 指数退避：0.1s, 0.2s, 0.4s, 0.8s, 1.0s(上限)
            wait = min(1.0, 0.1 * (2 ** attempt))
            attempt += 1
            time.sleep(wait)
        except Exception as e:
            logger.error(f"[DB] 获取连接意外错误: {e}")
            raise


@contextmanager
def get_db():
    """上下文管理器：获取连接，自动 commit/rollback + 归还

    [CTO-15.23 2026-05-10 Deploy-CTO P0-1 加固]
      老路径: rollback() 自身若抛 (连接已断 / PgBouncer 异常),后面的 raise 会被替掉,
              真实业务异常被吞 + 池里留 in_failed_state 连接给下个 request 用 → 雪崩。
      新路径: rollback() 包内层 try/except + logger.warning,绝不让 rollback 失败盖掉
              业务原因; commit() 同样保护; close() 已在 _PooledConnection.close() 内幂等。
    """
    conn = get_connection()
    try:
        yield conn
        try:
            conn.commit()
        except Exception as e:
            logger.error(f"[get_db] commit 失败 (连接将 rollback 后归还): {e}")
            try:
                conn.rollback()
            except Exception as re:
                logger.warning(f"[get_db] rollback 失败 (连接已废): {re}")
            raise
    except Exception:
        # 业务代码 yield 内抛异常 · rollback 后 reraise 原始异常 · 不要被 rollback 自身错误覆盖
        try:
            conn.rollback()
        except Exception as re:
            logger.warning(f"[get_db] rollback 失败 (连接已废 · 抛原始异常): {re}")
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
