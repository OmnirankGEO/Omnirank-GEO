"""#196 c1/c1b/c1c · 素材准备失败原话 —— 判据包夹具。

🔴 本包**不写库**,但 import 链会建连接池、**要求库真的存在**
   (最早那版抄了别的包的标题写成「不碰库」,那是假的:
    `api.geo_image_note_api` 的 import 链会连库)。

🔴 [c1c] 库名从 `geo_c14_192_test` 改成本包私有的 `geo_c14_196_test`,
   并且**自己建**。借用别人的库时,别人一删库我就整包红 ——
   而那种红看起来像被测代码坏了。空库就够:`init_db()` 会自建它要的表。
"""
import os

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_196_test"
_ADMIN_DSN = "postgresql://geo_admin:testpw@localhost:55492/postgres"
DB_NAME = "geo_c14_196_test"


def pytest_configure(config):
    os.environ.setdefault("TEST_DATABASE_URL", _DSN)
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


def pytest_sessionstart(session):
    """库不在就建。建不出来要**当场说清是环境问题**,不能让每条判据
    各自红在自己的断言上 —— 那读起来像被测代码坏了。"""
    import psycopg2

    try:
        conn = psycopg2.connect(_ADMIN_DSN)
    except Exception as exc:
        raise RuntimeError(
            "连不上测试 PG(容器 defgeo-c14-62-pg / 端口 55492):%r —— "
            "这是环境问题,不是被测代码的问题。" % (exc,))
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB_NAME,))
        if cur.fetchone() is None:
            cur.execute('CREATE DATABASE "%s"' % DB_NAME)
    finally:
        conn.close()
