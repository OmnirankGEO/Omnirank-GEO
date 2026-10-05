# P14 调研监测改造 · 部署 Runbook

**Branch:** `feat/geo-research-auto-poc-2026-05-25`
**Author:** 我(claude)+ 老板复核
**Audience:** 部署 CTO
**Date:** 2026-06-01

---

## 0. 这个 PR 包含什么

P14 系列对 GEO 调研监测后台的所有改造,57 commits(rebase 后) · 在 origin/main 之上一次性 ahead 57 / behind 0。

### 0.1 架构方向(老板已确认 · 2026-05-31)
- **删旧审核流**(ReviewPanel / ArticleDetail / BulkDialog / LockButton / ArticleCard / RejectReasonSelect 6 个前端组件 + `api/research_monitor_review_api.py` 后端 + LLM 评分/清洗 stage 4/5/6 + audit log 流程)
- **改文章库直通**:抓到文章后直接进 `geo_research_articles.review_status='in_library'` · 管理员看 / 编 / 加入参考库,代理/客户只看发布参谋聚合结果

### 0.2 业务规格补丁
- **文章去重口径改 "行业内 URL"** · `geo_research_articles` 复合 UNIQUE = `(url_hash, primary_industry)` 替代原 `(url_hash)` · 跨行业同 URL 各自有文章
- **stage 3 流程加固** · 全路径心跳 / 7 类 skip reason 分类 / Jina 1 次 retry / 部分失败 amber 降级 "完成 · 部分抓取失败"
- **跑批触发增强** · cron 时间表后端可配 · cancelled 续跑 · 断点续跑 · sweep 扩到 pending

### 0.3 上线风险打分(老板 2026-05-31 review)修补
| # | 风险 | 修在哪 |
|---|---|---|
| C1 | env 写非数字 → backend 起不来 | commit `b188ce91` |
| C2 | retry 后 stage 3 预算预估偏低 | commit `e96950d1` |
| B | `engine_count` "8 引擎" bug | commit `37b85fb9` |
| A | 矩阵文案"分布"误解 | commit `f4088f9b` |
| E | 加入参考库 500 (`review_status` VARCHAR(20) 撑爆) | commit `32a4db45` |

---

## 1. 上线必跑顺序(顺序错了会出事)

> **绝对不要先 push 代码再补 migration · 反过来也不行。代码上线和 DB schema 必须同窗口完成。**

### Step 1 · 停跑批 + 确认无 running round
```bash
# 后台进 admin 把 cron 关掉(系统配置 · 自动跑批开关)
# 然后 DB 查:
docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope <<'SQL'
SELECT round_id, status, started_at
  FROM geo_research_rounds
 WHERE status IN ('pending', 'running');
SQL
# 期望:0 行。有 running 等它跑完,有 pending 取消或转 cancelled。
```

### Step 2 · 备份关键表(P14.2 跨行业改 UNIQUE 必备)
```bash
docker exec omnirank-db pg_dump -U geo_admin -d geo_agentscope \
  -t geo_research_articles -t geo_research_article_citations \
  -t geo_research_review_log -t geo_research_rounds \
  > "/tmp/p14_backup_$(date +%Y%m%d_%H%M%S).sql"
```

### Step 3 · 跑两个 migration(顺序敏感)

**Migration 1** · `scripts/migration_normalize_url_hash_per_industry.sql`
- 干啥:`geo_research_articles` UNIQUE `(url_hash)` → `(url_hash, primary_industry)`
- 不可逆:删旧 UNIQUE + 加新 UNIQUE + backfill 跨行业 dup article 行
- 已含 marker `url_hash_per_industry` + idempotent
```bash
docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope \
  < scripts/migration_normalize_url_hash_per_industry.sql
```
校验:
```sql
SELECT marker, applied_at FROM _migration_markers
 WHERE marker = 'url_hash_per_industry';
SELECT indexname, indexdef FROM pg_indexes
 WHERE tablename = 'geo_research_articles' AND indexname LIKE '%url_hash%';
-- 期望:UNIQUE (url_hash, primary_industry) 存在
```

**Migration 2** · `scripts/migration_review_status_varchar32_2026-06-01.sql`
- 干啥:`geo_research_articles.review_status` + `geo_research_review_log.{prev,new}_review_status` 列宽 20 → 32
- 修 bug:"加入参考库" 500("imported_to_reference" 21 字撑爆 20)
- 幂等 + 不阻塞(PostgreSQL ALTER COLUMN TYPE 增大列宽不重写表)
```bash
docker exec -i omnirank-db psql -U geo_admin -d geo_agentscope \
  < scripts/migration_review_status_varchar32_2026-06-01.sql
```
校验:
```sql
SELECT character_maximum_length FROM information_schema.columns
 WHERE table_name = 'geo_research_articles' AND column_name = 'review_status';
-- 期望:32
```

### Step 4 · 代码上线
```bash
# 拉新代码 → 重启 backend
git pull
sudo systemctl restart omnirank-backend  # 或对应进程管理命令
```

### Step 5 · backend 启动期 idempotent migration 自动跑(兜底)
`db/diagnosis_db.py:_migrate()` 也含同款 ALTER COLUMN VARCHAR(32) · 即使 Step 3 漏跑也会自动补。
查 log 看:
```text
[OK] geo_research_articles.review_status TYPE VARCHAR(32) [P14.4 E]
```

### Step 6 · 前端 build + deploy
```bash
cd frontend && npm install && npm run build
# 然后部署 dist/ 到 nginx 或对应 CDN
```

### Step 7 · 验证流程(上线后必跑)
1. **后端启动安全**:`curl http://prod/health` 200 · 看 backend log 无 `_safe_int_env / _safe_float_env` warn(env 没污染)
2. **8000 只 1 个 LISTEN**(P14.2 教训 · 多 backend 污染会让浏览器流量乱走):
   ```bash
   ss -ltnp | grep ':8000'  # 期望:仅 1 行
   ```
3. **跑批触发**:admin UI · 跑批管理 → "手动触发" → 选 1 行业 → 看 Stage 1-7 推进顺序
4. **跑批预算保护**:故意把 `JINA_RETRY_MAX_ATTEMPTS=10` 设(测试环境) · 启动 backend · 看 stage 3 进入前 `_assert_round_budget_or_raise` 预估值 × 10
5. **加入参考库**:文章库 → 任选 1 文章 → "加入参考库" → 200 + reference_articles 多 1 行 + `geo_research_articles.review_status = 'imported_to_reference'`
6. **engine_count 不再超 4**:发布参谋 → 任意行业 → "X 引擎命中" 列必 ≤ 4 (跑过 4 引擎的) · 不会再看到 5 / 6 / 8 这种数

### Step 8 · 回滚预案(出事时)
- **代码回滚** · `git revert <P14 起始 commit>..HEAD` + 重启
- **migration 2 回滚** · `scripts/rollback_review_status_varchar32_2026-06-01.sql`
  - 警告:有 `review_status='imported_to_reference'` 的行先清(否则 ALTER 失败) · 会丢"已加入参考库" 标记
- **migration 1 回滚** · 无脚本(不可逆)· 必须从 Step 2 备份 restore 整张表

---

## 2. 已知未做的事(不在本 PR 范围)

| 项 | 状态 | 后续 phase |
|---|---|---|
| FastAPI BackgroundTasks → worker lease | 未做(仍是 BackgroundTask) | 新建 RFC · roadmap 上待排 |
| stage barrier → producer/consumer 流水线("边抓 AI 边抓文章")| 未做 | 强依赖上一项 · 同 RFC |
| 服务器侧 Jina 直连可达性 | **未验证** | 部署 CTO 跑 `scripts/ssh_diag_jina_server.py` 验 · 跑批前必须确认 r.jina.ai TCP 通 · 否则 stage 3 全 jina_failed |

---

## 3. 联系

- 业务 / 架构问题 → 老板
- 代码 / 测试问题 → 我
- 部署 / SSH / migration → 部署 CTO
