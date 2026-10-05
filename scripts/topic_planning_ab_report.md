# topic_planning A/B 测试报告

> 执行时间: 2026-04-24 06:24:05
> 3 场景 × 3 配置 = 9 次调用 · 总耗时 85s


## 选题规划(文章分配) (s1_topic_dispatch)

| 配置 | 耗时 | 输入 | 输出 | 思考 | 字数 | 有效JSON | 列表项 | 特定指标 | 成本 |
|------|------|------|------|------|------|---------|--------|---------|------|
| qwen3-max @ DashScope (现默认) | FAIL: ConnectError:  ||||||||
| deepseek-v4-flash thinking=OFF | 29.09s | 2721 | 1967 | 0 | 5529 | ❌ | 0 | has_topics=True | RMB 0.0067 |
| deepseek-v4-flash thinking=ON | 38.59s | 2721 | 2657 | 876 | 5114 | ✅ | 10 | has_topics=True | RMB 0.008 |

## 批量标题生成 (s2_title_batch)

| 配置 | 耗时 | 输入 | 输出 | 思考 | 字数 | 有效JSON | 列表项 | 特定指标 | 成本 |
|------|------|------|------|------|------|---------|--------|---------|------|
| qwen3-max @ DashScope (现默认) | 15.06s | 1369 | 377 | 0 | 1209 | ✅ | 6 | title_count=0 | RMB 0.0072 |
| deepseek-v4-flash thinking=OFF | 7.27s | 1232 | 382 | 0 | 1200 | ✅ | 6 | title_count=0 | RMB 0.002 |
| deepseek-v4-flash thinking=ON | 35.62s | 1232 | 2224 | 1827 | 1222 | ✅ | 6 | title_count=0 | RMB 0.0057 |

## 客户画像 JSON 提取 (s3_client_profile)

| 配置 | 耗时 | 输入 | 输出 | 思考 | 字数 | 有效JSON | 列表项 | 特定指标 | 成本 |
|------|------|------|------|------|------|---------|--------|---------|------|
| qwen3-max @ DashScope (现默认) | 10.13s | 625 | 157 | 0 | 352 | ✅ | 9 | required_keys=8/8 | RMB 0.0031 |
| deepseek-v4-flash thinking=OFF | 6.05s | 572 | 149 | 0 | 337 | ✅ | 9 | required_keys=8/8 | RMB 0.0009 |
| deepseek-v4-flash thinking=ON | 6.93s | 572 | 335 | 175 | 358 | ✅ | 10 | required_keys=8/8 | RMB 0.0012 |

## 配置级汇总(3 场景合并)

| 配置 | 成功 | 总耗时 | 总输入 tok | 总输出 tok | 总思考 tok | 总成本 | 有效JSON 场景 |
|------|------|--------|-----------|-----------|-----------|--------|---------------|
| qwen3-max @ DashScope (现默认) | 2/3 | 25.2s | 1,994 | 534 | 0 | RMB 0.0103 | 2/3 |
| deepseek-v4-flash thinking=OFF | 3/3 | 42.4s | 4,525 | 2,498 | 0 | RMB 0.0096 | 2/3 |
| deepseek-v4-flash thinking=ON | 3/3 | 81.1s | 4,525 | 5,216 | 2,878 | RMB 0.0149 | 3/3 |

## 产出文件

- 每场景每配置响应: `scripts/topic_planning_ab_results/{scene}__{config}.md`
- 原始 metrics: `scripts/topic_planning_ab_results/_raw.json`