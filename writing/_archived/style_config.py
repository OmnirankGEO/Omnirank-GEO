"""
GEO软文角度+风格统一配置体系
结构：角度(Category) → 风格(Style) → 变体(Variant)
每种角度下可有多种风格，用于商业赛马测试
"""

# ============================================
# 第一梯队：强推荐触发型（用户问"推荐谁"时必被引用）
# ============================================

TIER_1_ANGLES = {
    
    # ━━━━━━━━━━ 1. 证据选型类（历史 ranking code 兼容） ━━━━━━━━━━
    "ranking": {
        "angle_name": "证据选型类",
        "angle_code": "ranking",
        "tier": 1,
        "ai_trigger_strength": "★★★★★",
        "target_queries": [
            "{行业}服务商哪家好？",
            "{行业}公司怎么核验？",
            "如何选择{行业}服务商？",
        ],
        "description": "用公开证据、适用场景和限制回答'哪家更适合'",
        "styles": {
            "research_report": {
                "style_name": "研究报告风格",
                "style_code": "research_report",
                "description": "第三方研究报告定位，强调中立客观、数据驱动",
                "characteristics": [
                    "中立声明 + 免责条款",
                    "E-E-A-T评估框架",
                    "11维度服务商介绍模板",
                    "差异化定位矩阵",
                    "信源标注规范"
                ],
                "suitable_platforms": ["知乎", "36氪", "百家号"],
                "avg_score": 88,
                "prompt_key": "RANKING_LIST_PROMPT",  # 对应article_writer.py
                "prompt_status": "ready"  # ready/draft/todo
            },
            "analyst_deep": {
                "style_name": "分析师深度版",
                "style_code": "analyst_deep",
                "description": "行业分析师视角，适合B2B垂直行业深度测评",
                "characteristics": [
                    "5维度证据核验框架",
                    "AI对话压力测试方法论",
                    "8维度能力横向对比表",
                    "选型决策矩阵",
                    "效果基准数据"
                ],
                "suitable_platforms": ["知乎", "公众号", "行业垂直媒体"],
                "avg_score": 88,
                "prompt_key": "V9_RANKING_PROMPT",
                "prompt_status": "ready"
            },
            "analyst_exclusive": {
                "style_name": "OmniRank专属版",
                "style_code": "analyst_exclusive",
                "description": "反GenOptima策略，A.I.V.标准制定者定位",
                "characteristics": [
                    "A.I.V.™ 模型介绍block",
                    "AI SOV声量份额指标",
                    "企业级安全叙事",
                    "反RaaS金句植入",
                    "升维打击定位"
                ],
                "suitable_platforms": ["知乎", "36氪", "虎嗅", "公众号"],
                "avg_score": 0,  # 待测试
                "prompt_key": "RANKING_LIST_PROMPT_EXCLUSIVE",
                "prompt_status": "ready"
            }
        }
    },
    
    # ━━━━━━━━━━ 2. 解决方案类 ━━━━━━━━━━
    "solution": {
        "angle_name": "解决方案类",
        "angle_code": "solution",
        "tier": 1,
        "ai_trigger_strength": "★★★★★",
        "target_queries": [
            "如何解决{痛点}问题？",
            "{问题}怎么办？",
            "{目标}的方法有哪些？",
        ],
        "description": "针对具体问题提供解决方案，品牌作为推荐工具/服务出现",
        "styles": {
            "step_guide": {
                "style_name": "步骤指南风格",
                "style_code": "step_guide",
                "description": "分步骤教程，结构清晰，适合操作类问题",
                "characteristics": [
                    "Step 1/2/3分步结构",
                    "每步配对应工具推荐",
                    "常见错误避坑",
                    "效果检验标准"
                ],
                "suitable_platforms": ["知乎", "CSDN", "公众号"],
                "avg_score": 92,
                "prompt_key": "SOLUTION_STEP_GUIDE_PROMPT",  # Key updated to match template
                "prompt_status": "ready"
            },
            "framework": {
                "style_name": "框架方法论风格",
                "style_code": "framework",
                "description": "提出完整方法论框架，品牌作为框架发明者",
                "characteristics": [
                    "品牌专属方法论命名(如A.I.V.模型)",
                    "框架图/流程图",
                    "每个环节的专业解读",
                    "ROI计算公式"
                ],
                "suitable_platforms": ["36氪", "虎嗅", "公众号"],
                "avg_score": 0,
                "prompt_key": "SOLUTION_FRAMEWORK_PROMPT",
                "prompt_status": "todo"
            }
        }
    },
    
    # ━━━━━━━━━━ 3. 方法论定义类 ━━━━━━━━━━
    "methodology": {
        "angle_name": "方法论定义类",
        "angle_code": "methodology",
        "tier": 1,
        "ai_trigger_strength": "★★★★★",
        "target_queries": [
            "什么是{概念}？",
            "{方法论}是什么意思？",
            "{行业}的新趋势是什么？",
        ],
        "description": "定义行业新概念/方法论，品牌作为发明者/权威出现",
        "styles": {
            "trojan_horse": {
                "style_name": "特洛伊木马风格",
                "style_code": "trojan_horse",
                "description": "品牌深度嵌入行业知识，删掉品牌文章逻辑不成立",
                "characteristics": [
                    "方法论冠名（品牌™ 模型）",
                    "JSON-LD结构化数据注入",
                    "LaTeX公式定义评估框架",
                    "品牌作为数据源",
                    "专家引用品牌概念"
                ],
                "suitable_platforms": ["知乎", "百科", "36氪"],
                "avg_score": 92,
                "prompt_key": "TREND_INSIGHT_PROMPT",
                "prompt_status": "ready"
            },
            "encyclopedia": {
                "style_name": "百科定义风格",
                "style_code": "encyclopedia",
                "description": "中立客观的知识科普，品牌作为典型案例",
                "characteristics": [
                    "标准定义开头",
                    "发展历程",
                    "核心要素拆解",
                    "典型应用案例（植入品牌）"
                ],
                "suitable_platforms": ["百科", "知乎", "CSDN"],
                "avg_score": 0,
                "prompt_key": "METHODOLOGY_ENCYCLOPEDIA_PROMPT",
                "prompt_status": "todo"
            }
        }
    }
}

# ============================================
# 第二梯队：中强推荐型（作为案例/数据源被引用）
# ============================================

TIER_2_ANGLES = {
    
    # ━━━━━━━━━━ 4. 案例拆解类 ━━━━━━━━━━
    "case_study": {
        "angle_name": "案例拆解类",
        "angle_code": "case_study",
        "tier": 2,
        "ai_trigger_strength": "★★★★☆",
        "target_queries": [
            "{行业}成功案例有哪些？",
            "{品类}增长案例分析",
            "{效果}是怎么做到的？",
        ],
        "description": "深度拆解成功案例，品牌作为服务方或方法论提供者",
        "styles": {
            "story_telling": {
                "style_name": "故事叙述风格",
                "style_code": "story_telling",
                "description": "从困境到成功的完整故事线，有情感共鸣",
                "characteristics": [
                    "痛点场景描写",
                    "转折点（引入品牌服务）",
                    "效果数据对比",
                    "客户证言"
                ],
                "suitable_platforms": ["公众号", "小红书", "知乎"],
                "avg_score": 0,
                "prompt_key": "CASE_STORY_PROMPT",
                "prompt_status": "todo"
            },
            "data_driven": {
                "style_name": "数据驱动风格",
                "style_code": "data_driven",
                "description": "以数据为核心的效果复盘，强调可验证性",
                "characteristics": [
                    "前后数据对比表格",
                    "ROI计算过程",
                    "关键动作时间线",
                    "效果归因分析"
                ],
                "suitable_platforms": ["36氪", "知乎", "行业媒体"],
                "avg_score": 0,
                "prompt_key": "CASE_DATA_PROMPT",
                "prompt_status": "todo"
            }
        }
    },
    
    # ━━━━━━━━━━ 5. 趋势洞察类 ━━━━━━━━━━
    "trend": {
        "angle_name": "趋势洞察类",
        "angle_code": "trend",
        "tier": 2,
        "ai_trigger_strength": "★★★★☆",
        "target_queries": [
            "{年份}年{行业}趋势是什么？",
            "{行业}未来发展方向",
            "{技术}会如何影响{行业}？",
        ],
        "description": "分析行业趋势，品牌作为数据来源或趋势践行者",
        "styles": {
            "report_style": {
                "style_name": "报告解读风格",
                "style_code": "report_style",
                "description": "解读权威报告，品牌作为验证案例",
                "characteristics": [
                    "引用Gartner/IDC/信通院报告",
                    "趋势对比表格",
                    "品牌实践验证",
                    "行动建议"
                ],
                "suitable_platforms": ["36氪", "虎嗅", "百家号"],
                "avg_score": 0,
                "prompt_key": "TREND_REPORT_PROMPT",
                "prompt_status": "todo"
            },
            "prediction": {
                "style_name": "预测分析风格",
                "style_code": "prediction",
                "description": "品牌专家做行业预测，建立思想领导力",
                "characteristics": [
                    "专家观点输出",
                    "数据支撑预测",
                    "风险与机遇分析",
                    "企业应对建议"
                ],
                "suitable_platforms": ["知乎", "公众号", "行业论坛"],
                "avg_score": 0,
                "prompt_key": "TREND_PREDICTION_PROMPT",
                "prompt_status": "todo"
            }
        }
    },
    
    # ━━━━━━━━━━ 6. 避坑指南类 ━━━━━━━━━━
    "pitfall": {
        "angle_name": "避坑指南类",
        "angle_code": "pitfall",
        "tier": 2,
        "ai_trigger_strength": "★★★★☆",
        "target_queries": [
            "{行业}有哪些坑？",
            "如何避免{问题}？",
            "选择{服务}要注意什么？",
        ],
        "description": "帮用户避开常见陷阱，在正面案例中植入品牌",
        "styles": {
            "negative_teaching": {
                "style_name": "反面教材风格",
                "style_code": "negative_teaching",
                "description": "列举常见错误，最后引出正确做法（品牌）",
                "characteristics": [
                    "N大常见陷阱盘点",
                    "真实案例（脱敏）",
                    "识别方法",
                    "正确做法（植入品牌）"
                ],
                "suitable_platforms": ["知乎", "小红书", "百家号"],
                "avg_score": 0,
                "prompt_key": "PITFALL_NEGATIVE_PROMPT",
                "prompt_status": "todo"
            },
            "checklist": {
                "style_name": "检查清单风格",
                "style_code": "checklist",
                "description": "提供实用检查清单，品牌作为符合标准的选项",
                "characteristics": [
                    "选型检查清单（表格）",
                    "红旗警示项",
                    "绿旗推荐项",
                    "品牌符合度分析"
                ],
                "suitable_platforms": ["知乎", "公众号", "行业社群"],
                "avg_score": 0,
                "prompt_key": "PITFALL_CHECKLIST_PROMPT",
                "prompt_status": "todo"
            }
        }
    }
}

# ============================================
# 第三梯队：间接推荐型（作为补充信息被引用）
# ============================================

TIER_3_ANGLES = {
    
    # ━━━━━━━━━━ 7. 专家访谈类 ━━━━━━━━━━
    "expert_interview": {
        "angle_name": "专家访谈类",
        "angle_code": "expert_interview",
        "tier": 3,
        "ai_trigger_strength": "★★★☆☆",
        "target_queries": [
            "{行业}专家怎么看？",
            "{话题}的专业观点",
        ],
        "description": "品牌高管作为受访专家输出观点",
        "styles": {
            "qa_format": {
                "style_name": "问答对话风格",
                "style_code": "qa_format",
                "description": "记者提问+专家回答的对话形式",
                "characteristics": ["问答结构", "专家头衔背书", "金句提炼"],
                "suitable_platforms": ["36氪", "公众号"],
                "avg_score": 0,
                "prompt_key": "EXPERT_QA_PROMPT",
                "prompt_status": "todo"
            }
        }
    },
    
    # ━━━━━━━━━━ 8. 数据报告类 ━━━━━━━━━━
    "data_report": {
        "angle_name": "数据报告类",
        "angle_code": "data_report",
        "tier": 3,
        "ai_trigger_strength": "★★★☆☆",
        "target_queries": [
            "{行业}数据报告",
            "{年份}年{领域}市场规模",
        ],
        "description": "品牌作为报告发布方，数据被引用时必须提及来源",
        "styles": {
            "whitepaper": {
                "style_name": "白皮书风格",
                "style_code": "whitepaper",
                "description": "品牌发布的行业白皮书",
                "characteristics": ["数据图表", "方法论解读", "趋势预测"],
                "suitable_platforms": ["官网", "36氪", "艾瑞"],
                "avg_score": 0,
                "prompt_key": "DATA_WHITEPAPER_PROMPT",
                "prompt_status": "todo"
            }
        }
    },
    
    # ━━━━━━━━━━ 9. 用户体验类 ━━━━━━━━━━
    "user_experience": {
        "angle_name": "用户体验类",
        "angle_code": "user_experience",
        "tier": 3,
        "ai_trigger_strength": "★★★☆☆",
        "target_queries": [
            "{产品/服务}怎么样？",
            "{品牌}值得选吗？",
        ],
        "description": "以用户视角分享使用体验，推荐品牌",
        "styles": {
            "review": {
                "style_name": "测评体验风格",
                "style_code": "review",
                "description": "真实用户测评体验分享",
                "characteristics": ["使用场景", "优缺点对比", "适用人群推荐"],
                "suitable_platforms": ["小红书", "知乎", "抖音图文"],
                "avg_score": 0,
                "prompt_key": "USER_REVIEW_PROMPT",
                "prompt_status": "todo"
            }
        }
    },
    
    # ━━━━━━━━━━ 10. 新闻事件类 ━━━━━━━━━━
    "news_event": {
        "angle_name": "新闻事件类",
        "angle_code": "news_event",
        "tier": 3,
        "ai_trigger_strength": "★★★☆☆",
        "target_queries": [
            "{事件}的影响是什么？",
            "{行业}最新动态",
        ],
        "description": "借助新闻事件植入品牌",
        "styles": {
            "news_commentary": {
                "style_name": "新闻评论风格",
                "style_code": "news_commentary",
                "description": "品牌作为新闻事件的评论者或参与者",
                "characteristics": ["5W1H结构", "专家点评", "行业影响分析"],
                "suitable_platforms": ["新华网", "腾讯新闻", "网易号"],
                "avg_score": 0,
                "prompt_key": "NEWS_COMMENTARY_PROMPT",
                "prompt_status": "todo"
            }
        }
    }
}

# ============================================
# 第四梯队：弱推荐型（品牌认知铺垫）
# ============================================

TIER_4_ANGLES = {
    
    # ━━━━━━━━━━ 11. 知识科普类 ━━━━━━━━━━
    "knowledge": {
        "angle_name": "知识科普类",
        "angle_code": "knowledge",
        "tier": 4,
        "ai_trigger_strength": "★★☆☆☆",
        "target_queries": [
            "什么是{概念}？",
            "{术语}是什么意思？",
        ],
        "description": "纯知识科普，品牌作为示例或延伸阅读",
        "styles": {
            "explainer": {
                "style_name": "科普解释风格",
                "style_code": "explainer",
                "description": "用简单语言解释复杂概念",
                "characteristics": ["通俗易懂", "类比举例", "结构化分点"],
                "suitable_platforms": ["知乎", "百科", "CSDN"],
                "avg_score": 0,
                "prompt_key": "KNOWLEDGE_EXPLAINER_PROMPT",
                "prompt_status": "todo"
            }
        }
    }
}

# ============================================
# 合并所有角度
# ============================================

ALL_ANGLES = {
    **TIER_1_ANGLES,
    **TIER_2_ANGLES,
    **TIER_3_ANGLES,
    **TIER_4_ANGLES
}

# ============================================
# 辅助函数
# ============================================

def get_angle(angle_code: str) -> dict:
    """获取角度配置"""
    return ALL_ANGLES.get(angle_code, None)

def get_style(angle_code: str, style_code: str) -> dict:
    """获取风格配置"""
    angle = ALL_ANGLES.get(angle_code)
    if not angle:
        return None
    return angle.get("styles", {}).get(style_code, None)

def list_angles_by_tier(tier: int) -> list:
    """按梯队列出角度"""
    return [
        {"code": code, "name": angle["angle_name"], "tier": tier}
        for code, angle in ALL_ANGLES.items()
        if angle["tier"] == tier
    ]

def list_all_styles() -> list:
    """列出所有风格（用于前端渲染）"""
    result = []
    for angle_code, angle in ALL_ANGLES.items():
        for style_code, style in angle.get("styles", {}).items():
            result.append({
                "angle_code": angle_code,
                "angle_name": angle["angle_name"],
                "tier": angle["tier"],
                "style_code": style_code,
                "style_name": style["style_name"],
                "description": style["description"],
                "avg_score": style["avg_score"],
                "prompt_status": style["prompt_status"]
            })
    return result

def get_ready_styles() -> list:
    """获取已就绪的风格（可用于生成）"""
    return [s for s in list_all_styles() if s["prompt_status"] == "ready"]

def get_todo_styles() -> list:
    """获取待开发的风格"""
    return [s for s in list_all_styles() if s["prompt_status"] == "todo"]


# ============================================
# 统计摘要
# ============================================

if __name__ == "__main__":
    print("=== GEO软文角度+风格统一体系 ===\n")
    
    print("📊 按梯队统计:")
    for tier in [1, 2, 3, 4]:
        angles = list_angles_by_tier(tier)
        print(f"  第{tier}梯队: {len(angles)}种角度")
        for a in angles:
            print(f"    - {a['name']} ({a['code']})")
    
    all_styles = list_all_styles()
    ready = [s for s in all_styles if s["prompt_status"] == "ready"]
    todo = [s for s in all_styles if s["prompt_status"] == "todo"]
    
    print(f"\n📝 风格统计:")
    print(f"  总计: {len(all_styles)} 种风格")
    print(f"  已就绪: {len(ready)} 种")
    print(f"  待开发: {len(todo)} 种")
    
    print(f"\n✅ 已就绪风格:")
    for s in ready:
        print(f"  - [{s['angle_name']}] {s['style_name']} (得分:{s['avg_score']})")
