# DeepSeek V4 vs 现有 GEO 文章生成模型对比报告

> 执行时间: 2026-04-24 05:39:04
> 测试案例: 深圳整装公司TOP10权威推荐（2026） (家装 / 整装 / 深圳)
> 5 配置 × 7 风格 = 35 篇 · 总耗时 1555s

## 1 · 核心指标对比

| 配置 | 成功率 | 平均耗时 | 平均字数 | 平均小标题 | 平均表格 | 平均来源 | 输出 token | 总成本 |
|------|--------|---------|---------|-----------|---------|---------|-----------|-------|
| **当前默认 · deepseek-v3.2@DashScope** | 6/7 | 158.84s | 7859 | 16.5 | 30.5 | 13.2 | 29,539 | ¥0.33 |
| **deepseek-v4-flash · thinking=ON** | 7/7 | 77.77s | 7488 | 20.0 | 44.9 | 8.4 | 38,879 | ¥0.13 |
| **deepseek-v4-flash · thinking=OFF** | 7/7 | 56.97s | 6283 | 19.0 | 26.3 | 10.7 | 28,230 | ¥0.11 |
| **deepseek-v4-pro · thinking=ON** | 7/7 | 159.95s | 8594 | 22.0 | 32.9 | 9.4 | 42,146 | ¥1.69 |
| **deepseek-v4-pro · thinking=OFF** | 7/7 | 128.46s | 7001 | 18.7 | 31.4 | 9.0 | 31,273 | ¥1.43 |

## 2 · 成本规模推演(按生产环境 1 万篇/月)

| 配置 | 35 篇实测成本 | 推演 1 万篇成本 | 相对当前默认 |
|------|--------------|----------------|-------------|
| 当前默认 · deepseek-v3.2@DashScope | ¥0.33 | ¥94 | 100% (基准) |
| deepseek-v4-flash · thinking=ON | ¥0.13 | ¥38 | 41%  |
| deepseek-v4-flash · thinking=OFF | ¥0.11 | ¥32 | 34%  |
| deepseek-v4-pro · thinking=ON | ¥1.69 | ¥482 | 514%  |
| deepseek-v4-pro · thinking=OFF | ¥1.43 | ¥407 | 435%  |

## 3 · 逐风格逐配置明细


### 排行榜单 (ranking_v2)
| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |
|------|------|------|--------|------|------|---------|---------|---------|------|
| 当前默认 · deepseek-v3.2@DashScope | FAIL: ReadTimeout:  |||||||||
| deepseek-v4-flash · thinking=ON | 99.58s | 10228 | 38 | 81 | 18 | 33 | 7430 | 711 | ¥0.0253 |
| deepseek-v4-flash · thinking=OFF | 103.85s | 11962 | 39 | 79 | 8 | 24 | 7727 | 0 | ¥0.0259 |
| deepseek-v4-pro · thinking=ON | 206.07s | 10047 | 39 | 56 | 2 | 16 | 7090 | 449 | ¥0.2955 |
| deepseek-v4-pro · thinking=OFF | 176.53s | 9744 | 39 | 58 | 3 | 12 | 6280 | 0 | ¥0.2761 |

### 权威榜单 (authority_ranking)
| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |
|------|------|------|--------|------|------|---------|---------|---------|------|
| 当前默认 · deepseek-v3.2@DashScope | 221.49s | 11040 | 22 | 101 | 13 | 10 | 7259 | 0 | ¥0.0764 |
| deepseek-v4-flash · thinking=ON | 121.11s | 11636 | 22 | 101 | 11 | 12 | 8515 | 1040 | ¥0.0262 |
| deepseek-v4-flash · thinking=OFF | 69.26s | 7476 | 22 | 37 | 6 | 17 | 4805 | 0 | ¥0.0188 |
| deepseek-v4-pro · thinking=ON | 207.71s | 10666 | 22 | 101 | 5 | 19 | 7488 | 558 | ¥0.2899 |
| deepseek-v4-pro · thinking=OFF | 172.03s | 9291 | 22 | 100 | 11 | 10 | 6161 | 0 | ¥0.2581 |

### 推荐盘点 (recommendation_review)
| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |
|------|------|------|--------|------|------|---------|---------|---------|------|
| 当前默认 · deepseek-v3.2@DashScope | 193.09s | 9764 | 13 | 12 | 42 | 10 | 6053 | 0 | ¥0.0627 |
| deepseek-v4-flash · thinking=ON | 97.94s | 9887 | 16 | 12 | 12 | 20 | 6845 | 442 | ¥0.0208 |
| deepseek-v4-flash · thinking=OFF | 93.38s | 10225 | 17 | 12 | 23 | 21 | 6509 | 0 | ¥0.0202 |
| deepseek-v4-pro · thinking=ON | 156.31s | 7798 | 16 | 12 | 11 | 10 | 5557 | 484 | ¥0.2191 |
| deepseek-v4-pro · thinking=OFF | 151.4s | 8532 | 14 | 12 | 32 | 12 | 5350 | 0 | ¥0.2141 |

### 选购指南 (buying_guide)
| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |
|------|------|------|--------|------|------|---------|---------|---------|------|
| 当前默认 · deepseek-v3.2@DashScope | 209.41s | 9791 | 16 | 23 | 10 | 6 | 6139 | 0 | ¥0.0663 |
| deepseek-v4-flash · thinking=ON | 82.27s | 8400 | 25 | 23 | 13 | 18 | 5828 | 298 | ¥0.0202 |
| deepseek-v4-flash · thinking=OFF | 10.39s | 808 | 0 | 0 | 0 | 3 | 517 | 0 | ¥0.0096 |
| deepseek-v4-pro · thinking=ON | 201.25s | 13286 | 24 | 23 | 42 | 24 | 9140 | 505 | ¥0.3223 |
| deepseek-v4-pro · thinking=OFF | 177.58s | 9350 | 22 | 23 | 4 | 7 | 6071 | 0 | ¥0.2486 |

### 趋势洞察 (trojan_horse)
| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |
|------|------|------|--------|------|------|---------|---------|---------|------|
| 当前默认 · deepseek-v3.2@DashScope | 93.53s | 4857 | 17 | 14 | 5 | 4 | 2881 | 0 | ¥0.0348 |
| deepseek-v4-flash · thinking=ON | 12.12s | 596 | 0 | 0 | 0 | 3 | 713 | 366 | ¥0.0073 |
| deepseek-v4-flash · thinking=OFF | 47.39s | 5140 | 28 | 19 | 9 | 10 | 3261 | 0 | ¥0.0124 |
| deepseek-v4-pro · thinking=ON | 98.27s | 6363 | 22 | 19 | 0 | 22 | 4388 | 283 | ¥0.1757 |
| deepseek-v4-pro · thinking=OFF | 85.0s | 4714 | 17 | 13 | 2 | 7 | 2861 | 0 | ¥0.1391 |

### 问答推荐 (qa_recommendation)
| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |
|------|------|------|--------|------|------|---------|---------|---------|------|
| 当前默认 · deepseek-v3.2@DashScope | 139.96s | 6708 | 16 | 19 | 5 | 8 | 4138 | 0 | ¥0.0486 |
| deepseek-v4-flash · thinking=ON | 62.96s | 6001 | 18 | 25 | 1 | 10 | 4630 | 642 | ¥0.017 |
| deepseek-v4-flash · thinking=OFF | 10.97s | 976 | 0 | 0 | 0 | 5 | 591 | 0 | ¥0.0089 |
| deepseek-v4-pro · thinking=ON | 126.32s | 6347 | 16 | 0 | 3 | 7 | 4294 | 338 | ¥0.1959 |
| deepseek-v4-pro · thinking=OFF | 123.63s | 6775 | 17 | 14 | 11 | 10 | 4191 | 0 | ¥0.1934 |

### 品牌软文 (brand_softarticle)
| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |
|------|------|------|--------|------|------|---------|---------|---------|------|
| 当前默认 · deepseek-v3.2@DashScope | 95.53s | 4995 | 15 | 14 | 4 | 3 | 3069 | 0 | ¥0.0391 |
| deepseek-v4-flash · thinking=ON | 68.38s | 5671 | 21 | 72 | 4 | 19 | 4918 | 606 | ¥0.0171 |
| deepseek-v4-flash · thinking=OFF | 63.53s | 7397 | 27 | 37 | 29 | 24 | 4820 | 0 | ¥0.0169 |
| deepseek-v4-pro · thinking=ON | 123.7s | 5648 | 15 | 19 | 3 | 9 | 4189 | 494 | ¥0.188 |
| deepseek-v4-pro · thinking=OFF | 13.03s | 599 | 0 | 0 | 0 | 3 | 359 | 0 | ¥0.0961 |

## 4 · 价格说明

- **当前默认 · deepseek-v3.2@DashScope**: 输入 ¥2.0/M · 输出 ¥8.0/M  (估算值(DashScope deepseek-v3.2 公示价未公开 · 按 V3 级对齐))
- **deepseek-v4-flash · thinking=ON**: 输入 ¥1.0/M · 输出 ¥2.0/M  (DeepSeek 官网 2026-04-24 公示)
- **deepseek-v4-flash · thinking=OFF**: 输入 ¥1.0/M · 输出 ¥2.0/M  (DeepSeek 官网 2026-04-24 公示)
- **deepseek-v4-pro · thinking=ON**: 输入 ¥12.0/M · 输出 ¥24.0/M  (DeepSeek 官网 2026-04-24 公示)
- **deepseek-v4-pro · thinking=OFF**: 输入 ¥12.0/M · 输出 ¥24.0/M  (DeepSeek 官网 2026-04-24 公示)

## 5 · 原始产出

- 35 篇文章: `scripts/deepseek_v4_compare_results/<config_key>/<style>.md`
- 原始 metrics JSON: `scripts/deepseek_v4_compare_results/_raw.json`
- 配置级汇总 JSON: `scripts/deepseek_v4_compare_results/_summary.json`

## 6 · 建议人工抽检

建议挑 3 篇同风格不同配置的文章对比质量(比如 ranking_v2 主流风格,5 个配置全读一遍):

- 当前默认 · deepseek-v3.2@DashScope: `scripts/deepseek_v4_compare_results/current_dashscope_v32/ranking_v2.md`
- deepseek-v4-flash · thinking=ON: `scripts/deepseek_v4_compare_results/v4_flash_thinking_on/ranking_v2.md`
- deepseek-v4-flash · thinking=OFF: `scripts/deepseek_v4_compare_results/v4_flash_thinking_off/ranking_v2.md`
- deepseek-v4-pro · thinking=ON: `scripts/deepseek_v4_compare_results/v4_pro_thinking_on/ranking_v2.md`
- deepseek-v4-pro · thinking=OFF: `scripts/deepseek_v4_compare_results/v4_pro_thinking_off/ranking_v2.md`