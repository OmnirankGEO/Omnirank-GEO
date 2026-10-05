# 本包必跑集(Deploy NO-GO 2026-08-13 返修后固化)

> 🔴 存在的理由:第一版热修**只跑了自己那 41 条**,漏掉
> `tests/admin_user_governance/test_governance_integration.py`,
> 于是「详情端点上线即 500」这条一路走到 Deploy 才被拦下。
> 「我跑了我写的测试」不等于「我跑了会被我改坏的测试」。

## 必跑

| 套件 | 为什么必跑 | 独立库 |
|---|---|---|
| `tests/inventory_relation_model_2026_08_13` | 本包判据 | `test_hotfix` |
| `tests/admin_user_governance/test_governance_integration.py` | 本包改了 `services/admin_user_governance.py` 与 `schemas/admin_user_governance.py`,**它是这两个文件的既有回归** | `test_gov` |
| `tests/inventory_distribution_chain_2026_08_12` | 本包改了 `services/agent_inventory.py` / `inventory_lot_ledger.py` | `test_hotfix` 可共用 |
| `tests/frontend_privacy_final` | R5 脱敏清单相关 | `test_hotfix` 可共用 |
| `frontend/tests/invrel-inventory.spec.cjs` | 浏览器真行为(死按钮 / 出口 / 接线) | — |

## 🔴 两条纪律

**① 每个套件用自己的库,且跑前 DROP DATABASE 重建。**
`tests/admin_user_governance` 会重建 schema,与本包共用一个库时会把
`agent_inventory_transactions` 等表跑掉,后续用例报 `UndefinedTable` /
`InFailedSqlTransaction` —— 那是**库状态级联**,不是真失败,极易掩盖真红。

```bash
for db in test_hotfix test_gov; do
  docker exec -i <pg> psql -U geo_admin -d postgres \
    -c "DROP DATABASE IF EXISTS $db WITH (FORCE);" -c "CREATE DATABASE $db;"
  docker exec -i <pg> psql -U geo_admin -d $db -c "CREATE EXTENSION IF NOT EXISTS vector;"
done
```

**② 两边都红的节点,必须比【错误签名】,不只比节点集合。**
`NEW RED = 0` 只保证没有**新变红的节点**,不保证已红节点的**红因**没变。
本次事故里 8 条 ValidationError 就藏在"两臂都红"的同名节点下,
按 node-ID 集合做的 A/B 判据看不出来。

已知既有红(2026-08-13 双臂各自干净库实测,**签名逐字相同**,非本包引入):

```
pkg  臂:73 passed / 1 failed
base 臂:73 passed / 1 failed
节点:test_legacy_role_user_remains_readable_and_permissions_still_resolve
签名:psycopg2.errors.InFailedSqlTransaction
```
