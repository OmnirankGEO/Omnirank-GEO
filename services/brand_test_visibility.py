"""测试客户可见性规则的**唯一定义处**(#116 · 2026-09-05)。

🔴 背景:同一件事此前在两处各写一份,而且**写反了**——
   `api/brand_api.py` 的「我的客户」列表按 #67 已经**不再**隔离服务商自己名下的
   is_test 品牌;而 `api/dashboard_api.py:570` 的 `brand_count` 仍然排除它们。
   两处都在服务商自己的范围内,却给出**不同的答案** ⇒
   她的首页写着 8 个客户,点进列表看到 9 个,**没有任何东西报错**。

🔴 规则本身只有两支,别把它们混成一个开关:

   · **平台级视图**(admin 看全站、跨租户统计):排除 is_test。
     这是 M3 铁律 5 的目标 —— 测试客户不该污染平台口径与对客话术。

   · **自己名下**(`owner_user_id = 本人` 或 `user_clients.user_id = 本人`):
     **不排除**。她自己建的「测试科技有限公司」被自动打上 is_test 之后就此消失,
     她不知道它去哪了,也没有任何提示(#67 的原始报障)。
     这一支不存在「看到别人的测试客户」——查询本身已经把范围收在她自己身上。

⚠️ 刻意**不**提供「一个布尔参数决定要不要隔离」的写法:那会让调用方在
   platform / own-scope 之间来回猜,而猜错不报错。两支各有各的名字。
"""

from __future__ import annotations

#: 平台级视图:排除测试客户。**SQL 片段以 `b` 为 brands 表别名。**
PLATFORM_TEST_EXCLUSION = " AND (b.is_test IS NULL OR b.is_test = FALSE)"

#: 自己名下:**不排除**。空串是这条规则的**内容**,不是「还没实现」——
#: 判据会钉住它必须为空,免得有人"顺手补上"。
OWN_SCOPE_TEST_EXCLUSION = ""


def platform_test_clause(*, include_test: bool = False, is_admin: bool = False) -> str:
    """平台级视图的测试客户子句。

    `include_test` **仅 admin 生效** —— 防 query 参数绕过(BUG6 2026-06-05)。
    """
    return "" if (include_test and is_admin) else PLATFORM_TEST_EXCLUSION


def own_scope_test_clause() -> str:
    """自己名下视图的测试客户子句 —— 恒为空。

    做成函数而不是让调用方直接写 `""`:调用点长成
    `own_scope_test_clause()` 时,读代码的人**看得见这是一条被决定过的规则**;
    写成裸的空串,下一个人只会看到「这里什么都没有」。
    """
    return OWN_SCOPE_TEST_EXCLUSION
