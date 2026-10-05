"""
品牌安全Agent

职责: 确保报告中不会将客户品牌误认为竞品（双重检查机制）
核心功能: 品牌安全零容忍，发现违规立即拒绝

这是完美方案中的"品牌安全专家Agent"
"""

import logging
import re
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class BrandSafetyAgent:
    """品牌安全专家Agent"""
    
    def __init__(self, brand_name: str):
        """
        初始化品牌安全Agent
        
        Args:
            brand_name: 客户品牌名称（需要保护的品牌）
        """
        self.name = "品牌安全专家"
        self.brand_name = brand_name
        self.brand_variants = self._generate_brand_variants(brand_name)
        
    def _generate_brand_variants(self, brand_name: str) -> List[str]:
        """
        生成品牌名称的变体（用于检测）
        
        Returns:
            品牌名称变体列表（已过滤城市名和通用词）
        """
        variants = [
            brand_name,                          # 原始名称
            brand_name.lower(),                  # 小写
            brand_name.replace(" ", ""),         # 去空格
            brand_name.replace("（", "(").replace("）", ")")  # 中英文括号
        ]
        
        # 提取核心词（如"驰鲸科技" → "驰鲸"）
        core_words = re.findall(r'[\u4e00-\u9fa5]+', brand_name)
        variants.extend(core_words)
        
        # 城市名/地区名/通用词黑名单 — 这些不应被当作品牌名匹配
        BLACKLIST_WORDS = {
            # 直辖市/省会/主要城市
            "北京", "上海", "广州", "深圳", "重庆", "天津", "成都", "杭州",
            "南京", "武汉", "西安", "苏州", "东莞", "佛山", "宁波", "长沙",
            "郑州", "青岛", "大连", "厦门", "合肥", "济南", "珠海", "无锡",
            "福州", "昆明", "贵阳", "海口", "三亚", "哈尔滨", "沈阳",
            # 省份
            "浙江", "江苏", "广东", "山东", "四川", "湖北", "湖南", "河南",
            "福建", "安徽", "云南", "贵州", "海南", "辽宁", "吉林", "黑龙江",
            # 通用词
            "中国", "中华", "集团", "公司", "有限", "控股", "科技", "技术",
            "服务", "网络", "信息", "股份",
        }
        
        # 过滤：去掉纯城市名/通用词（≤3字的变体如果在黑名单中就排除）
        filtered = []
        for v in variants:
            v_clean = v.strip()
            if not v_clean:
                continue
            # 如果变体本身就是一个城市名/通用词，跳过
            if v_clean in BLACKLIST_WORDS:
                continue
            filtered.append(v_clean)
        
        # 去重
        result = list(set(filtered))
        logger.info(f"品牌变体（已过滤城市名）: {result}")
        return result
    
    def check_competitor_list(
        self,
        competitors: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        检查竞品列表（第一道防线）
        
        Args:
            competitors: 竞品列表
        
        Returns:
            {
                safe: bool,
                violations: List[Dict],
                cleaned_competitors: List[Dict]
            }
        """
        violations = []
        cleaned_competitors = []
        
        for comp in competitors:
            comp_name = comp.get('name', '') or comp.get('nickname', '')
            
            if not comp_name:
                continue
            
            # 检查是否违规
            violation = self._check_name_violation(comp_name)
            
            if violation:
                # 发现违规
                violations.append({
                    'competitor_name': comp_name,
                    'reason': violation['reason'],
                    'category': violation['category'],
                    'action': '移除此竞品'
                })
                logger.warning(f"🚨 品牌安全违规: {comp_name} - {violation['reason']}")
            else:
                # 安全，保留
                cleaned_competitors.append(comp)
        
        is_safe = len(violations) == 0
        
        result = {
            'safe': is_safe,
            'violations': violations,
            'cleaned_competitors': cleaned_competitors,
            'original_count': len(competitors),
            'cleaned_count': len(cleaned_competitors),
            'removed_count': len(violations)
        }
        
        if not is_safe:
            logger.error(f"🚨 第一道防线：发现{len(violations)}个品牌安全违规")
        else:
            logger.info(f"✅ 第一道防线：竞品列表安全检查通过")
        
        return result
    
    def check_report(
        self,
        report_content: str
    ) -> Dict[str, Any]:
        """
        检查报告全文（第二道防线）
        
        Args:
            report_content: 报告内容（Markdown文本）
        
        Returns:
            {
                safe: bool,
                violations: List[Dict],
                risk_sections: List[str]
            }
        """
        violations = []
        risk_sections = []
        
        # 提取竞品章节
        competitor_sections = self._extract_competitor_sections(report_content)
        
        for section in competitor_sections:
            # 在竞品章节中查找品牌名
            for variant in self.brand_variants:
                if variant in section['content']:
                    # 检查上下文，确认是否真的是竞品
                    if self._is_listed_as_competitor(section['content'], variant):
                        violations.append({
                            'section': section['title'],
                            'brand_variant': variant,
                            'reason': f'在竞品章节中发现品牌名称"{variant}"',
                            'severity': 'critical'
                        })
                        risk_sections.append(section['title'])
        
        is_safe = len(violations) == 0
        
        result = {
            'safe': is_safe,
            'violations': violations,
            'risk_sections': list(set(risk_sections)),
            'sections_checked': len(competitor_sections)
        }
        
        if not is_safe:
            logger.error(f"🚨 第二道防线：报告中发现{len(violations)}处品牌安全问题")
        else:
            logger.info(f"✅ 第二道防线：报告全文安全检查通过")
        
        return result
    
    def _check_name_violation(
        self,
        competitor_name: str
    ) -> Dict[str, Any] | None:
        """
        检查名称是否违规
        
        Returns:
            违规信息字典，如果安全则返回None
        """
        comp_lower = competitor_name.lower()
        
        # 检查1: 是否包含品牌名
        for variant in self.brand_variants:
            if variant.lower() in comp_lower:
                return {
                    'reason': f'账号名包含品牌名称"{variant}"，可能是客户自己',
                    'category': 'brand_name_included'
                }
        
        # 检查2: 是否高度相似
        for variant in self.brand_variants:
            similarity = self._calculate_similarity(
                comp_lower, 
                variant.lower()
            )
            if similarity > 0.7:
                return {
                    'reason': f'账号名与品牌名称"{variant}"高度相似（相似度{similarity:.0%}）',
                    'category': 'highly_similar'
                }
        
        # 安全
        return None
    
    def _calculate_similarity(self, str1: str, str2: str) -> float:
        """计算字符串相似度"""
        if not str1 or not str2:
            return 0.0
        
        set1 = set(str1)
        set2 = set(str2)
        
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        
        return intersection / union if union > 0 else 0.0
    
    def _extract_competitor_sections(
        self,
        report_content: str
    ) -> List[Dict[str, str]]:
        """
        从报告中提取竞品相关章节
        
        Returns:
            章节列表 [{title, content}, ...]
        """
        sections = []
        
        # 关键词：可能包含竞品的章节标题
        competitor_keywords = [
            '竞品',
            '竞争',
            '对标',
            '标杆',
            'competitor'
        ]
        
        lines = report_content.split('\n')
        current_section = None
        current_content = []
        
        for line in lines:
            # 检查是否是标题
            if line.startswith('#'):
                # 保存上一个章节
                if current_section and current_content:
                    sections.append({
                        'title': current_section,
                        'content': '\n'.join(current_content)
                    })
                
                # 检查是否是竞品相关章节
                is_competitor_section = any(
                    kw in line 
                    for kw in competitor_keywords
                )
                
                if is_competitor_section:
                    current_section = line
                    current_content = []
                else:
                    current_section = None
                    current_content = []
            
            elif current_section:
                # 在竞品章节内
                current_content.append(line)
        
        # 保存最后一个章节
        if current_section and current_content:
            sections.append({
                'title': current_section,
                'content': '\n'.join(current_content)
            })
        
        return sections
    
    def _is_listed_as_competitor(
        self,
        section_content: str,
        brand_variant: str
    ) -> bool:
        """
        检查品牌名称是否被列为竞品
        
        Returns:
            True=被列为竞品（违规），False=只是提及
        """
        # 简化判断：如果在表格中，且不是在"您的品牌"列
        lines = section_content.split('\n')
        
        for line in lines:
            if brand_variant in line:
                # 检查是否在表格行中
                if '|' in line:
                    # 排除表头
                    if '---' not in line and '排名' not in line:
                        # 检查是否在"您的品牌"相关的行
                        if '您的品牌' not in line and '自己' not in line:
                            # 很可能被列为竞品
                            return True
        
        return False
    
    def get_safety_report(
        self,
        check1_result: Dict[str, Any],
        check2_result: Dict[str, Any]
    ) -> str:
        """
        生成品牌安全检查报告
        
        Args:
            check1_result: 第一道防线检查结果
            check2_result: 第二道防线检查结果
        
        Returns:
            Markdown格式的安全报告
        """
        report = f"""
# 🛡️ 品牌安全检查报告

**品牌名称**: {self.brand_name}
**检查时间**: {self._get_current_time()}

---

## 第一道防线：竞品列表检查

"""
        
        if check1_result['safe']:
            report += f"""
✅ **检查结果**: 通过
- 检查竞品数: {check1_result['original_count']}
- 安全竞品数: {check1_result['cleaned_count']}
- 移除数量: {check1_result['removed_count']}
"""
        else:
            report += f"""
🚨 **检查结果**: 发现违规
- 检查竞品数: {check1_result['original_count']}
- 违规数量: {len(check1_result['violations'])}

### 违规详情

| 竞品名称 | 违规原因 | 处理 |
|----------|----------|------|
"""
            for v in check1_result['violations']:
                report += f"| {v['competitor_name']} | {v['reason']} | {v['action']} |\n"
        
        report += """

---

## 第二道防线：报告全文检查

"""
        
        if check2_result['safe']:
            report += f"""
✅ **检查结果**: 通过
- 检查章节数: {check2_result['sections_checked']}
- 风险章节数: 0
"""
        else:
            report += f"""
🚨 **检查结果**: 发现违规
- 检查章节数: {check2_result['sections_checked']}
- 违规数量: {len(check2_result['violations'])}

### 违规详情

| 章节 | 发现内容 | 严重性 |
|------|----------|--------|
"""
            for v in check2_result['violations']:
                report += f"| {v['section']} | {v['reason']} | {v['severity']} |\n"
        
        report += """

---

## 最终判定

"""
        
        overall_safe = check1_result['safe'] and check2_result['safe']
        
        if overall_safe:
            report += """
✅ **整体评估**: 品牌安全检查通过

所有检查点均未发现品牌安全问题，报告可以发布。
"""
        else:
            report += """
🚨 **整体评估**: 品牌安全检查失败

发现品牌安全违规，禁止发布报告！

**必须执行的操作**:
1. 移除所有违规的竞品
2. 修正报告中的品牌安全问题
3. 重新进行品牌安全检查
"""
        
        return report
    
    def _get_current_time(self) -> str:
        """获取当前时间字符串"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 简化接口
def check_brand_safety(
    competitors: List[Dict[str, Any]],
    brand_name: str
) -> Dict[str, Any]:
    """
    简化的品牌安全检查接口
    
    Args:
        competitors: 竞品列表
        brand_name: 品牌名称
    
    Returns:
        {safe: bool, cleaned_competitors: List, ...}
    """
    agent = BrandSafetyAgent(brand_name)
    return agent.check_competitor_list(competitors)


if __name__ == "__main__":
    # 测试示例
    print("=" * 60)
    print("品牌安全Agent 测试")
    print("=" * 60)
    
    agent = BrandSafetyAgent("驰鲸科技")
    
    # 测试竞品列表
    test_competitors = [
        {'name': '兔克出海', 'platform': 'douyin'},           # 安全
        {'name': '驰鲸运营助手', 'platform': 'douyin'},        # 违规：包含品牌名
        {'name': '工厂外贸人carly', 'platform': 'douyin'},    # 安全
        {'name': '吃鲸优选', 'platform': 'xiaohongshu'},      # 可能违规：高度相似
    ]
    
    result = agent.check_competitor_list(test_competitors)
    
    print(f"\n检查结果: {'✅ 安全' if result['safe'] else '🚨 发现违规'}")
    print(f"原始竞品数: {result['original_count']}")
    print(f"清理后竞品数: {result['cleaned_count']}")
    print(f"移除数量: {result['removed_count']}")
    
    if result['violations']:
        print("\n违规详情:")
        for v in result['violations']:
            print(f"  - {v['competitor_name']}: {v['reason']}")
