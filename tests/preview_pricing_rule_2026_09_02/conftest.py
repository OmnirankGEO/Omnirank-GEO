"""包级 conftest:把**重导入**收在这里,一次做完。

🔴 `import server` 会重放迁移、刷几百行日志/print。若它发生在**某个测试模块的
   收集期**,会压垮 pytest 的 capture(`ValueError: I/O operation on closed file`),
   而 pytest 随即**中断收集** —— 同包后面的文件整体离开分母。

   实测三种表现,一种比一种坏:
     ① 在测试函数体内导入 ⇒ 那条判据红(**看得见**);
     ② 在测试模块级裸导入 ⇒ 收集期炸,同包另外 4 个文件 **50 条判据静默消失**
        (**看不见,而且剩下的全绿**);
     ③ 在 conftest 里导入 ⇒ 早于所有测试模块、只做一次,后面都命中 sys.modules 缓存。

   ② 比 ① **严格更坏**,而我一度把 ① 改成 ② 并以为修好了 ——
   **把一条红换成分母缩水,不是修复。**
   这也是本包 `test_collection_denominator_is_intact` 存在的理由:
   分母缩水不会让任何东西变红,只能显式去数。
"""

from __future__ import annotations

import pathlib

_PKG = pathlib.Path(__file__).resolve().parent

#: 盘上应被收集的测试文件数 —— 少一个就说明有文件在收集期被丢掉了。
EXPECTED_TEST_FILES = len(sorted(_PKG.glob("test_*.py")))

import server  # noqa: E402,F401  —— 就是要在这里先做掉
