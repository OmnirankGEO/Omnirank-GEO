---
description: GEO诊断系统开发规则 - 避免重复修复同一问题
---

# GEO诊断系统开发规则

> 最后更新：2026-01-12

## 🌐 通用性原则（核心！）

**系统是行业通用的，不得为特定行业硬编码数据。**

### ❌ 禁止
- 硬编码行业特定品牌/公司列表
- 为特定客户定制prompt词
- 在Prompt示例中使用具体行业名称（如"GEO"、"TikTok"）

### ✅ 正确做法
- 使用 `{industry}`, `{brand_name}` 等占位符
- 从诊断数据动态提取竞品信息
- 所有Prompt必须行业无关

---

## 🚨 关键决策记录

### 1. 评分逻辑
| 维度 | 正确字段 | 错误字段 |
|-----|---------|---------|
| 网页搜索可见度 | `brand_direct_count` | `result_count` |
| 权威背书 | `brand_direct_citations` 中的权威源 | 所有 `authority_sources` |
| 社媒评分 | `brand_content_stats` | 原始采集数据 |

### 2. 引用分类三层结构
1. `brand_direct` → 用于评分
2. `industry_reference` → 仅展示
3. `filtered` → 过滤

### 3. 内容生成通用性
- `TOPIC_DISPATCHER_PROMPT` 必须包含行业上下文约束
- 竞品优先从 `competitor_analysis` 动态提取
- `config.py` 只保留 `_fallback` 通用兜底竞品

---

## ⚠️ 每次开发前检查清单

### 诊断系统
- [ ] 网页可见度使用 `brand_direct_count`？
- [ ] 权威背书只计算品牌直接引用中的权威源？
- [ ] 三层分类逻辑完整？

### 内容生成
- [ ] Prompt中无硬编码行业/客户名称？
- [ ] 竞品从诊断数据动态提取？
- [ ] 标题模板使用占位符 `{industry}`？

---

## 📋 修复历史（最近7天）

| 日期 | 问题 | 解决方案 | 关键文件 |
|------|------|----------|----------|
| 2026-01-22 | LLM评分器"0内容得6分" | 修改Prompt，确保0内容=0分的硬性规则 | llm_geo_scorer.py |
| 2026-01-12 | TopicDispatcher硬编码GEO行业 | Prompt添加行业约束+动态提取竞品 | topic_dispatcher.py, config.py |

---

## 🔧 常见问题快速修复

### 行业引用被计入品牌评分
**检查**: `geo_scorer.py` 使用 `brand_direct_count`

### 内容生成出现错误行业
**检查**: Prompt是否包含【行业上下文约束】章节

