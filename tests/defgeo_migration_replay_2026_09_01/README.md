# 迁移重放床 · 「第一次能过、第二次必挂」进分母

> 来历:2026-09-01 P0。7 个新迁移(040/041/044/046/051/052/054)用
> `pg_get_constraintdef()` 的**渲染文本**跟写死的字符串比。`pg_dump`→`pg_restore`
> 会把 `IN (...)` 形的 CHECK 重渲染 ⇒ **首次部署过、在还原副本上重放必炸**。
>
> **这一格此前不在任何分母里** —— 18 包 / 门八 / 462 idxguard 都没模拟「在还原副本上重放」。

---

## 1 · 机制(最小复现,实测)

```
建库:  CHECK (((mode)::text = ANY ((ARRAY['defensive'::character varying, …])::text[])))
还原:  CHECK (((mode)::text = ANY (ARRAY[('defensive'::character varying)::text, …])))
```

同一台 PG16 两侧,**不是版本问题**,是 DDL 文本被重新解析后表达式树重渲染。
同表上纯数值那条 `chk_defgeo_qplan_counts` **逐字不变** ⇒ 只有 `IN (...)`/varchar-cast 那一形会漂。

**实测漂移面**(让库自己算,不靠静态正则):**869 条 CHECK 里 29 条**重渲染后定义变了(3.3%),
其中 26 条在 `defgeo_*`、3 条在存量表 `geo_article_delivery_slots`(后者由 035 用语义比对守着,不炸)。

---

## 2 · 三把锁

| 判据 | 钉什么 |
|---|---|
| `test_mrb_01` **床 A** | 生产 schema 还原 → 连续重放 ×2 全绿(覆盖存量渲染差异) |
| `test_mrb_02` **床 B** | 还原 → **重渲染往返** → 重放 ×2 全绿 ← 唯一抓得到本 bug 的那张床 |
| `test_mrb_03` **oid 终态信号** | 第二遍重放**没把对象 DROP 重建**(oid 不变) |
| `test_mrx_01..05` **静态锁** | manifest 里不许拿系统目录渲染文本做全等比较 + 检测器自证 |

**床 A 与床 B 不是复制品**:修前实测 A 绿 / B 红(点名 `[040] 约束 chk_defgeo_qplan_mode 定义漂移`);
C 的第一版修法(attnum)上 **A 就红**(`cols=16,17` vs `17,18`)。四格各自兑现过。

### 2.1 为什么 oid 那条不能省

「重放两次全绿」还有另一种读法:**每次 DROP 重建都成功**。那样每次部署都有一段
「约束不存在」的窗口,并发写在那一瞬间不受约束。oid 不变才证 no-op。
形态抄自 `tests/geo_image_note_2026_08_17::test_034_replay_does_not_rebuild_the_live_root_index`。

### 2.2 床自己的活性

`_rerender()` 快照往返前后,断言**渲染真的变了 ≥ 20 条**(实测基线 29)。
没有这条的话,床坏了(比如副本两边本来就都是已还原形,没东西可漂)会表现成**全绿** ——
与「修好了」在退出码上同形。

---

## 3 · 为什么用纯 SQL 往返,不用真 `pg_dump`

**跑批镜像里 `pg_dump` 与 `psql` 都没有**(实测 `which pg_dump psql` ⇒ 两个都没有),
真 dump/restore 形态**进不了容器段**。

而漂移发生在「DDL 文本被重新解析」那一刻,不在 dump 那一刻 ——
`pg_dump` 输出的约束**就是** `pg_get_constraintdef()` 原文。所以:
取 constraintdef → `DROP CONSTRAINT` → 用那段原文 `ADD CONSTRAINT`,
产出与真 `pg_dump --schema-only | psql` **逐字节相同**的渲染(2026-09-01 并排比对过)。

---

## 4 · 🔴 如实记的边界(两条都还没解决)

### 4.1 空库床**未通过**,不在本包内

`scripts/prestart.py` 的基础引导**只有** `import db.diagnosis_db`(:211),
而 `server.py:501` 是先 `init_auth_db()` 再跑迁移。真空库上:

- prestart 原样:第 1 条迁移挂 `relation "users" does not exist`
- 补上 auth 之后:第 2 条挂 `relation "recharge_orders" does not exist` —— 还需要更多基础模块

⇒ prestart 注释声称支持的「Disaster-recovery/fresh databases」这条路**今天跑不通,
也没有任何自动化在验**。本包因此用**生产 schema dump 当地基**
(`tests/article_self_report_2026_08_19/prod_schema_2026-08-19.sql`,490 表)。
**依赖 post-train 的「DR / fresh-database 路径」那一项**;那一项落地后,这里应补第三张床。

### 4.2 `_rerender()` 只往返 `contype='c'`

UNIQUE/PK/FK 渲染成列名清单,本来就稳定;且它们背后有索引与外键依赖,
`DROP` 会连带别的对象 —— 动它们会引入与本 bug 无关的红。
**代价**:索引的 `WHERE` 子句里若带 `IN (...)`,仍可能漂,**本包没覆盖**。记 post-train。

---

## 5 · 静态锁的存量例外

现役尖上 manifest 124 条里剩 **12 处 / 5 个文件**(都是 2026-07 存量):

| 文件 | 处 |
|---|---|
| `scripts/migration_dealer_inventory_resale_2026_07_15.sql` | 2 |
| `scripts/migration_direct_service_refund_agreements_2026_07_15.sql` | 2 |
| `scripts/migration_geo_observation_v1_2026_07_17.sql` | 3 |
| `scripts/migration_article_generation_task_state_2026_07_21.sql` | 3 |
| `scripts/migration_organization_short_code_2026_07_28.sql` | 2 |

🔴 其中三个文件是**第一版检测器整族漏掉的** —— 索引那半写作
`regexp_replace(lower(pg_get_indexdef(i.oid)),'\s','','g')`,最外层是**包装函数**,
而我只判「最外层就是渲染函数」,污染穿不过包装(「接线锁用包含判定」那一族的镜像:
该用包含的地方用了同一性)。是顺着窗口C 问「索引谓词要不要也加一发」才查出来的。

**实测这 12 处当前都不会炸**:新库上 295 个 partial 索引里 **13 个会漂**
(`defgeo_pcmd_one_live_child` / `idx_defgeo_pout_claimable` / `idx_defgeo_pcmd_settlement_queue` …),
与这 5 个文件点名的 39 个对象**交集为 0**。
按 `LEGACY_EXEMPT`(文件 → 当前违规点数)+ `LEGACY_EXEMPT_SIZE=5` 冻结:
**多一处就红** —— 在例外名单 ≠ 免死金牌。

---

## 6 · 检测器自己栽过两次(所以它现在可信)

1. 第一版按「渲染函数后 400 字符内的 `INTO`」标污染 ⇒ 把
   `SELECT count(*) … WHERE pg_get_constraintdef(oid) <> '…' INTO mismatch_count`
   里的**计数变量**全误报了(07 月那批一片红)。**会误报的锁最后一定被关掉**,
   改成「被赋的表达式**最外层**就是渲染函数」。
2. 收紧之后**正样本当场报 0**:修前 040 是
   `SELECT pg_get_constraintdef(oid), convalidated INTO actual, ok`(多项),
   而 `SELECT(.*?)INTO` 惰性前向匹配还会从更早的 `SELECT * FROM (VALUES` 跨语句起头。
   改成「先找 `INTO` 再向前找同一语句内最近的 `SELECT`」+ 顶层逗号按位配对。

**六个控制组**(正样本用 `git show 3edfc0031:` 取的**真代码**,不是我手写的):
修前 040 片段 1 处 · 修前 040 全文 3 处 · 修前 046 全文 4 处 ·
现役 040 / 现役 035 / `scripts/defgeo_readiness_gen.py` 各 0 处。

---

## 7 · 跑法

```bash
TEST_DATABASE_URL=postgresql://<user>:<pw>@<host>:<port>/migreplay_g9_test \
  python -m pytest tests/defgeo_migration_replay_2026_09_01/ -q
```

要一台**能建库**的 PG(包内自建自删 `migreplay_*_test`,归 `MINTING_DB`,前缀 `migreplay_`)。
全套约 100 秒。
