---
name: geo_diagnosis
description: GEO诊断分析技能 - 指导Agent完成品牌GEO可见度诊断的完整流程
---

# GEO 诊断分析技能

## 技能概述
本技能指导你完成一个完整的 GEO（Generative Engine Optimization）诊断分析流程，评估品牌在 AI 搜索引擎中的可见度。

---

## 诊断流程

### 第一步：信息收集
在开始诊断前，确保你拥有以下信息：
- **品牌名称**：要诊断的品牌
- **所属行业**：品牌所在的行业领域
- **核心关键词**：3-5个与品牌相关的核心关键词
- **长尾关键词**：5-10个用户可能搜索的问题型关键词

### 第二步：社媒数据采集
使用以下工具采集社媒平台数据：

1. **抖音视频搜索**
```
search_douyin_videos(keyword="品牌名+行业关键词", page=1)
```
记录：视频数量、互动数据（点赞、评论）、发布者类型

2. **小红书笔记搜索**
```
search_xiaohongshu_notes(keyword="品牌名", page=1)
```
记录：笔记数量、互动数据、笔记类型

3. **微信视频号搜索**（如有）
```
search_wechat_channels(keyword="品牌名")
```

### 第三步：网页搜索分析
```
search_web_for_geo(industry="行业", keywords="核心关键词", company_name="品牌名")
```

检查并记录：
- [ ] 品牌官网是否出现在前10结果
- [ ] 是否有权威媒体报道
- [ ] 是否有百科词条
- [ ] 第三方评测/测评文章数量

### 第四步：AI 引擎可见度检测
使用以下 AI 引擎进行检测：

1. **DeepSeek 检测**
```
query_deepseek(query="用户问题型关键词", check_brand="品牌名")
```

2. **批量长尾关键词检测**
```
check_longtail_keywords(keywords=["关键词1", "关键词2", ...], check_brand="品牌名")
```

记录：
- AI 提及率：品牌被提及的引擎数量 / 总检测引擎数
- AI 覆盖率：品牌被检测到的关键词数 / 总关键词数
- AI 推荐率：品牌被推荐（列表前5）的次数

### 第五步：计算 GEO 评分
```
calculate_geo_score(
    douyin_data={"video_count": X},
    xiaohongshu_data={"note_count": X},
    web_search_data={"result_count": X, "brand_mentions": X, "authority_sources": [...]},
    ai_visibility_data={"brand_detected_count": X, "total_engines": X, "coverage_rate": X, "recommendation_count": X},
    brand_name="品牌名"
)
```

### 第六步：生成诊断报告
```
generate_geo_report(
    brand_name="品牌名",
    industry="行业",
    geo_score_data={...},
    ai_visibility_data={...},
    platform_data={...}
)
```

---

## 评分标准参考

| 维度 | 满分 | 评分标准 |
|------|------|----------|
| 平台覆盖 | 40分 | 抖音20分 + 小红书20分 |
| 内容数量 | 20分 | 总内容≥100得满分 |
| 搜索可见度 | 10分 | 结果数≥100且提及≥10得满分 |
| 权威来源 | 10分 | 权威来源≥5得满分 |
| AI可见度 | 20分 | 覆盖率+提及率+推荐率 |

## 等级划分

| 分数 | 等级 | 建议 |
|------|------|------|
| 80-100 | 优秀 | 保持现状，持续优化 |
| 60-79 | 良好 | 有提升空间，建议局部优化 |
| 40-59 | 一般 | 需要系统化布局 |
| 20-39 | 待改进 | 急需开始 GEO 建设 |
| 0-19 | 空白 | 完全空白，需从零开始 |

---

## 注意事项

1. **数据采集失败处理**：如果某个平台数据采集失败，记录失败原因，继续其他步骤
2. **关键词选择**：长尾关键词应该是用户真实会搜索的问题
3. **客观评价**：评分需基于实际数据，不可主观臆断
4. **建议具体化**：改进建议需要可执行，不要泛泛而谈
