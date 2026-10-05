"""
AI可见度+策略Agent

职责: 分析品牌在AI搜索中的可见度并给出提升策略
核心功能: AI可见度对标 + 行业基准 + 战略建议

这是Coze工作流中的"VISO可信度策略分析LLM"节点
"""

import logging
from typing import Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


class AIVisibilityAgent:
    """AI可见度+策略专家Agent"""
    
    def __init__(self, brand_name: str):
        """
        初始化AI可见度Agent
        
        Args:
            brand_name: 品牌名称
        """
        self.name = "AI可见度+策略专家"
        self.brand_name = brand_name
        
        # 行业基准（可配置）
        self.industry_benchmarks = {
            "mention_rate": 60,      # 行业理想提及率 60%
            "recommend_rate": 50,    # 行业理想推荐率 50%
            "engine_coverage": 3,    # 理想引擎覆盖 3/3
            "industry_association": 40  # 行业词关联度 40%
        }
    
    def analyze_visibility(
        self,
        ai_visibility_data: Dict[str, Any],
        competitor_data: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        分析AI可见度并生成对标表格
        
        Args:
            ai_visibility_data: AI可见度测试数据
            competitor_data: 竞品数据（可选）
        
        Returns:
            {
                comparison_table: Dict,
                gap_analysis: Dict,
                strategic_recommendations: Dict,
                visibility_interpretation: str
            }
        """
        try:
            # 提取数据
            results = ai_visibility_data.get('results', [])
            detected_in = ai_visibility_data.get('detected_in', 0)
            total_engines = ai_visibility_data.get('total_engines', 3)
            
            # 计算指标
            metrics = self._calculate_metrics(results, detected_in, total_engines)
            
            # 对比表格
            comparison = self._generate_comparison_table(metrics)
            
            # 差距分析
            gap_analysis = self._analyze_gaps(metrics)
            
            # 战略建议
            strategy = self._generate_strategy(metrics, gap_analysis, competitor_data)
            
            # 可见度解读
            interpretation = self._interpret_visibility(metrics)
            
            return {
                'comparison_table': comparison,
                'gap_analysis': gap_analysis,
                'strategic_recommendations': strategy,
                'visibility_interpretation': interpretation,
                'metrics': metrics
            }
            
        except Exception as e:
            logger.error(f"AI可见度分析失败: {e}")
            return self._get_fallback_result()
    
    def _calculate_metrics(
        self,
        results: List[Dict],
        detected_in: int,
        total_engines: int
    ) -> Dict[str, Any]:
        """
        计算AI可见度指标
        
        Returns:
            {
                mention_rate: float,
                mention_fraction: str,
                recommend_rate: float,
                engine_coverage: int,
                coverage_fraction: str
            }
        """
        total_tests = len(results)
        total_mentions = 0
        total_recommendations = 0
        
        # 统计提及和推荐
        for result in results:
            if result.get('error'):
                continue
            
            if result.get('brand_detected') or result.get('brand_mentioned_count', 0) > 0:
                total_mentions += 1
            
            if result.get('is_recommended'):
                total_recommendations += 1
        
        # 计算百分比
        mention_rate = (total_mentions / total_tests * 100) if total_tests > 0 else 0
        recommend_rate = (total_recommendations / total_tests * 100) if total_tests > 0 else 0
        
        return {
            'mention_rate': mention_rate,
            'mention_fraction': f"{total_mentions}/{total_tests}",
            'recommend_rate': recommend_rate,
            'recommend_fraction': f"{total_recommendations}/{total_tests}",
            'engine_coverage': detected_in,
            'total_engines': total_engines,
            'coverage_fraction': f"{detected_in}/{total_engines}"
        }
    
    def _generate_comparison_table(
        self,
        metrics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        生成对比表格数据
        
        Returns:
            {
                your_brand: Dict,
                industry_ideal: Dict,
                gap_assessment: Dict
            }
        """
        mention_rate = metrics['mention_rate']
        recommend_rate = metrics['recommend_rate']
        coverage = metrics['engine_coverage']
        
        # 您的品牌
        your_brand = {
            'mention_rate': f"**{mention_rate:.0f}%** ({metrics['mention_fraction']}次)",
            'recommend_rate': f"**{recommend_rate:.0f}%**",
            'engine_coverage': metrics['coverage_fraction'],
            'industry_association': "**0%**"  # 暂时硬编码，后续可通过LLM分析
        }
        
        # 行业理想
        industry_ideal = {
            'mention_rate': f"~{self.industry_benchmarks['mention_rate']}%",
            'recommend_rate': f"~{self.industry_benchmarks['recommend_rate']}%",
            'engine_coverage': f"{self.industry_benchmarks['engine_coverage']}/3",
            'industry_association': f"~{self.industry_benchmarks['industry_association']}%"
        }
        
        # 差距评估
        mention_gap = max(0, self.industry_benchmarks['mention_rate'] - mention_rate)
        coverage_gap = self.industry_benchmarks['engine_coverage'] - coverage
        
        gap_assessment = {
            'mention_gap': f"差距 **{mention_gap:.0f}%**",
            'recommend_gap': f"差距 **{max(0, self.industry_benchmarks['recommend_rate'] - recommend_rate):.0f}%**",
            'coverage_gap': "✅ 完全覆盖" if coverage_gap == 0 else f"差距 {coverage_gap} 个引擎",
            'association_gap': f"差距 **{self.industry_benchmarks['industry_association']}%**"
        }
        
        return {
            'your_brand': your_brand,
            'industry_ideal': industry_ideal,
            'gap_assessment': gap_assessment
        }
    
    def _analyze_gaps(
        self,
        metrics: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        分析差距并评估严重性
        
        Returns:
            {
                critical_gaps: List[str],
                moderate_gaps: List[str],
                overall_status: str
            }
        """
        mention_rate = metrics['mention_rate']
        coverage = metrics['engine_coverage']
        
        critical_gaps = []
        moderate_gaps = []
        
        # 提及率差距
        if mention_rate < 20:
            critical_gaps.append("品牌提及率极低（<20%），AI几乎不知道该品牌")
        elif mention_rate < 40:
            moderate_gaps.append("品牌提及率偏低（20-40%），需持续优化")
        
        # 引擎覆盖差距
        if coverage == 0:
            critical_gaps.append("未被任何AI引擎识别，急需建立品牌认知")
        elif coverage < 2:
            moderate_gaps.append(f"仅被{coverage}个AI引擎提及，覆盖不足")
        
        # 行业词关联度（暂时硬编码）
        critical_gaps.append("行业词关联度为0，AI无法将品牌与行业关联")
        
        # 整体状态
        if mention_rate >= 60:
            overall_status = "优秀"
        elif mention_rate >= 40:
            overall_status = "良好"
        elif mention_rate >= 20:
            overall_status = "一般"
        else:
            overall_status = "危险区"
        
        return {
            'critical_gaps': critical_gaps,
            'moderate_gaps': moderate_gaps,
            'overall_status': overall_status,
            'total_gap_count': len(critical_gaps) + len(moderate_gaps)
        }
    
    def _generate_strategy(
        self,
        metrics: Dict[str, Any],
        gap_analysis: Dict[str, Any],
        competitor_data: Dict[str, Any] = None
    ) -> Dict[str, List[Dict[str, str]]]:
        """
        生成分层级战略建议（增强版 - 战术化叙事）
        
        Returns:
            {
                immediate: List[Dict],  # 🔴 立即行动
                medium_term: List[Dict],  # 🟡 中期建设
                long_term: List[Dict]  # 🟢 长期优化
            }
        """
        immediate = []
        medium_term = []
        long_term = []
        
        mention_rate = metrics['mention_rate']
        coverage = metrics['engine_coverage']
        
        # 立即行动（1周内）- 战术化叙事
        if mention_rate < 20:
            immediate.append({
                'problem': '品牌词AI认知不足',
                'severity': '🔴 严重',
                'action': '**启动"身份确立战"**：创建百度百科/搜狗百科词条，建立AI训练源的权威锚点。让AI承认您是"实体存在的品牌"，解决"查无此人"的致命问题。',
                'timeline': '1周内启动',
                'expected': '30天内AI收录率达100%'
            })
            
            immediate.append({
                'problem': '缺乏AI可抓取文本',
                'severity': '🔴 严重',
                'action': '**启动"AI投喂计划"**：AI大模型是"文本生物"，看不懂视频。立即在知乎/今日头条发布大量结构化干货（如《2026工厂出海避坑指南》），高频植入品牌词，强制刷新AI知识库。',
                'timeline': '1周内启动',
                'expected': '每周发布2-3篇，30天累计10+篇'
            })
        
        if coverage == 0:
            immediate.append({
                'problem': 'AI引擎零覆盖',
                'severity': '🔴 致命',
                'action': '**紧急刷新索引**：在知乎、今日头条发布带品牌词的高质量文章，强制刷新AI索引。特别针对字节系（豆包）和百度系（文心）。',
                'timeline': '立即行动',
                'expected': '7天内至少1个AI引擎收录'
            })
        
        # 中期建设（1个月内）
        if mention_rate < 60:
            medium_term.append({
                'problem': '权威来源不足',
                'action': '**媒体背书建设**：获取权威媒体报道（36氪/虎嗅/亿邦动力等），提升品牌权威性。AI更信任有媒体报道的品牌。',
                'timeline': '1个月内',
                'expected': '获得2-3篇权威媒体报道'
            })
            
            medium_term.append({
                'problem': '行业认证缺失',
                'action': '**认证体系建设**：获取行业认证或奖项（如"2026出海服务商Top10"），提升品牌可信度和AI识别度。',
                'timeline': '1个月内',
                'expected': '获得1-2个行业认证/奖项'
            })
        
        medium_term.append({
            'problem': '知乎专栏建设不足',
            'action': '**知乎深耕战略**：建立知乎专栏，持续输出专业内容。知乎是AI训练数据的重要来源，高赞回答=AI推荐概率↑。',
            'timeline': '1个月内',
            'expected': '发布10+篇知乎回答，获得100+赞'
        })
        
        # 长期优化（持续）
        long_term.append({
            'problem': '内容更新频率',
            'action': '保持高质量内容的持续更新，建立稳定的内容生产节奏。',
            'timeline': '持续优化',
            'expected': '形成可持续的内容生产体系'
        })
        
        long_term.append({
            'problem': '用户UGC引导',
            'action': '引导用户生成内容（评价、案例分享），扩大品牌声量。真实用户评价是AI最信任的内容类型。',
            'timeline': '持续优化',
            'expected': '月均新增10+条用户UGC'
        })
        
        return {
            'immediate': immediate,
            'medium_term': medium_term,
            'long_term': long_term
        }
    
    def _interpret_visibility(
        self,
        metrics: Dict[str, Any]
    ) -> str:
        """
        生成可见度解读文本（增强版 - 货币化）
        
        Returns:
            解读文本
        """
        mention_rate = metrics['mention_rate']
        
        # ⭐ 计算opportunity loss
        try:
            # 导入损失计算函数
            import sys
            import os
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools', 'scoring'))
            from geo_scorer import calculate_opportunity_loss
            
            loss_data = calculate_opportunity_loss(mention_rate)
            loss_desc = loss_data['description']
            monthly_value_wan = loss_data['monthly_lost_value_wan']
        except:
            loss_desc = ""
            monthly_value_wan = 0
        
        if mention_rate >= 60:
            status = "✅ 提及率 > 60% (优秀)"
            search_status = "主动推荐"
            opportunity = "继续保持优势，巩固AI搜索领先地位"
        elif mention_rate >= 40:
            status = "✅ 提及率 40-60% (良好)"
            search_status = "部分推荐"
            opportunity = "持续优化内容质量，争取更多AI推荐"
        elif mention_rate >= 20:
            status = "⚠️ 提及率 20-40% (一般)"
            search_status = "偶尔提及"
            opportunity = "加强品牌内容建设，提升AI认知度"
        else:
            status = "⚠️ 提及率 < 20% (危险区)"
            search_status = "被动检索"
            
            if loss_desc:
                opportunity = f"由于竞品在AI端表现同样不强，这是**弯道超车**的最佳时机\n\n**商业影响评估**: {loss_desc}"
            else:
                opportunity = "由于竞品在AI端表现同样不强，这是**弯道超车**的最佳时机"
        
        return f"""
**当前状态**: {status}

目前品牌在AI搜索中处于{search_status}状态

**竞品机会**: {opportunity}
""".strip()
    
    def _get_fallback_result(self) -> Dict[str, Any]:
        """
        获取降级结果（分析失败时）
        """
        return {
            'comparison_table': {
                'your_brand': {'mention_rate': 'N/A'},
                'industry_ideal': {'mention_rate': '~60%'},
                'gap_assessment': {'mention_gap': '数据不足'}
            },
            'gap_analysis': {
                'critical_gaps': ['数据分析失败'],
                'moderate_gaps': [],
                'overall_status': '未知'
            },
            'strategic_recommendations': {
                'immediate': [],
                'medium_term': [],
                'long_term': []
            },
            'visibility_interpretation': '数据不足，无法生成解读',
            'metrics': {}
        }
    
    def format_comparison_table_markdown(
        self,
        comparison: Dict[str, Any]
    ) -> str:
        """
        格式化对比表格为Markdown
        
        Returns:
            Markdown表格字符串
        """
        your_brand = comparison['your_brand']
        ideal = comparison['industry_ideal']
        gap = comparison['gap_assessment']
        
        table = f"""
| 指标 | 您的品牌 ({self.brand_name}) | 行业理想状态 | 差距评估 |
|------|---------------------------|------------|----------|
| AI引擎覆盖 | {your_brand['engine_coverage']} | {ideal['engine_coverage']} | {gap['coverage_gap']} |
| 品牌提及率 | {your_brand['mention_rate']} | {ideal['mention_rate']} | {gap['mention_gap']} |
| 被推荐率 | {your_brand['recommend_rate']} | {ideal['recommend_rate']} | {gap['recommend_gap']} |
| 行业词关联度 | {your_brand['industry_association']} | {ideal['industry_association']} | {gap['association_gap']} |
""".strip()
        
        return table
    
    def format_strategy_markdown(
        self,
        strategy: Dict[str, List[Dict]]
    ) -> str:
        """
        格式化策略建议为Markdown
        
        Returns:
            Markdown格式的策略文本
        """
        md = ""
        
        # 立即行动
        if strategy['immediate']:
            md += "\n### 🔴 立即行动（1周内启动）\n\n"
            md += "| 问题 | 严重性 | 具体行动 |\n"
            md += "|------|--------|----------|\n"
            for item in strategy['immediate']:
                md += f"| {item['problem']} | {item['severity']} | {item['action']} |\n"
        
        # 中期建设
        if strategy['medium_term']:
            md += "\n### 🟡 中期建设（1个月内）\n\n"
            for item in strategy['medium_term']:
                md += f"- **{item['problem']}**：{item['action']}\n"
        
        # 长期优化
        if strategy['long_term']:
            md += "\n### 🟢 长期优化（持续建设）\n\n"
            for item in strategy['long_term']:
                md += f"- {item['action']}\n"
        
        return md.strip()


# 简化接口
def analyze_ai_visibility(
    ai_visibility_data: Dict[str, Any],
    brand_name: str
) -> Dict[str, Any]:
    """
    简化的AI可见度分析接口
    
    Args:
        ai_visibility_data: AI可见度测试数据
        brand_name: 品牌名称
    
    Returns:
        分析结果
    """
    agent = AIVisibilityAgent(brand_name)
    return agent.analyze_visibility(ai_visibility_data)


if __name__ == "__main__":
    # 测试示例
    print("=" * 70)
    print("AI可见度+策略Agent 测试")
    print("=" * 70)
    
    # 模拟AI可见度数据
    test_data = {
        'total_engines': 3,
        'detected_in': 0,
        'results': [
            {'engine': 'DeepSeek', 'brand_detected': False, 'brand_mentioned_count': 0,  'is_recommended': False},
            {'engine': 'Kimi', 'brand_detected': False, 'brand_mentioned_count': 0, 'is_recommended': False},
            {'engine': 'Doubao', 'brand_detected': False, 'brand_mentioned_count': 0, 'is_recommended': False},
        ]
    }
    
    agent = AIVisibilityAgent("驰鲸科技")
    result = agent.analyze_visibility(test_data)
    
    print("\n📊 对比表格:")
    print(agent.format_comparison_table_markdown(result['comparison_table']))
    
    print("\n\n💡 可见度解读:")
    print(result['visibility_interpretation'])
    
    print("\n\n🎯 战略建议:")
    print(agent.format_strategy_markdown(result['strategic_recommendations']))
    
    print("\n\n⚠️ 差距分析:")
    gap = result['gap_analysis']
    print(f"整体状态: {gap['overall_status']}")
    print(f"严重问题: {len(gap['critical_gaps'])}个")
    for g in gap['critical_gaps']:
        print(f"  - {g}")
