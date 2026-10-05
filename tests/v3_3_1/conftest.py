"""V3.3.1 子目录 conftest · 覆盖根 conftest 强制 TEST_DATABASE_URL 校验

V3.3.1 纯函数测试不依赖 DB · 跑前自动 mock 必要环境变量
"""

import os

# 在 import 任何业务模块前 · 提供假 TEST_DATABASE_URL 占位
# 纯函数测试不会实际连 DB
os.environ.setdefault("TEST_DATABASE_URL", "postgresql://test_user:test@localhost:5432/test_v3_3_1_unit")
os.environ.setdefault("ALLOW_NONTEST_DB", "0")
