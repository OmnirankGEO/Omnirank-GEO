# -*- coding: utf-8 -*-
"""把生产的基础引导 + manifest 在指定库上重放一遍。

刻意做成**独立子进程**:`db.*` 在 import 期建连接池并缓存 `DATABASE_URL`,
同进程内重放两次读到的是缓存 —— 那不是「重放」,是「同一次」。

🔴 执行器**直接调 `scripts.prestart._apply`**,不自己写一份:
   它会剥 psql 反斜杠命令与裸 BEGIN/COMMIT。自己再写一遍就是「同一谓词两处」,
   而弱的那份会把强的悄悄降级。

用法:REPLAY_ROOT=<repo> DATABASE_URL=<dsn> python replay_driver.py
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(os.environ["REPLAY_ROOT"])
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
QUIET = os.environ.get("REPLAY_QUIET") == "1"
t0 = time.time()

_sink = io.StringIO()
with (contextlib.redirect_stdout(_sink) if QUIET else contextlib.nullcontext()):
    # 与 server.py:501 同序:先 RBAC 基础(users 等),再写作基础(prestart.py:211)。
    # 🔴 prestart 自己**只有后半**,所以真空库上它会在第 1 条迁移挂在 users
    #    —— 那是另一条独立发现(post-train · 灾备路径),见本包 README。
    from db.auth_db import init_auth_db

    init_auth_db()
    import db.diagnosis_db  # noqa: F401

print("[base] 基础引导完成 %ss" % round(time.time() - t0, 1), flush=True)

import psycopg2  # noqa: E402

from db.migration_manifest import MIGRATIONS  # noqa: E402
from scripts.prestart import _apply  # noqa: E402

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()
done = 0
try:
    for rel in MIGRATIONS:
        try:
            _apply(cur, ROOT, rel)
        except Exception as exc:  # noqa: BLE001
            print("[FAIL] %s" % rel, flush=True)
            print("       %s: %s" % (type(exc).__name__, str(exc).strip()[:900]), flush=True)
            raise SystemExit(4)
        done += 1
finally:
    conn.close()
print("[ok] %d/%d 条迁移全过 · %ss" % (done, len(MIGRATIONS), round(time.time() - t0, 1)),
      flush=True)
