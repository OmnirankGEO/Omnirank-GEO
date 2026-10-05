"""
品牌账号识别器

核心功能：
1. 识别品牌自有账号
2. 验证内容是否属于品牌
3. 区分品牌内容和行业内容

解决问题：
- 新公司获得虚假满分的问题
- 行业内容被误当成品牌内容评分的问题
"""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class BrandAccountIdentifier:
    """品牌账号识别器"""
    
    def __init__(self, brand_name: str, known_accounts: List[str] = None):
        """
        初始化品牌账号识别器
        
        Args:
            brand_name: 品牌名称
            known_accounts: 已知的品牌账号列表（可选）
        """
        self.brand_name = brand_name
        self.known_accounts = known_accounts or []
        self.brand_variants = self._generate_brand_variants(brand_name)
        
        # 识别结果缓存
        self._identified_accounts = {}
        self._brand_content_ids = set()
        
    def _generate_brand_variants(self, brand_name: str) -> List[str]:
        """
        生成品牌名称变体用于匹配
        
        Args:
            brand_name: 品牌名称
            
        Returns:
            品牌名称变体列表
        """
        variants = [brand_name]
        
        # 去除常见后缀
        suffixes = ['科技', '公司', '有限公司', '集团', '网络', '科技有限公司', 
                   '（深圳）', '(深圳)', '技术', '服务']
        clean_name = brand_name
        for suffix in suffixes:
            clean_name = clean_name.replace(suffix, '')
        
        if clean_name and clean_name != brand_name:
            variants.append(clean_name)
        
        # 处理括号内容
        # "全域上榜（深圳）科技有限公司" -> "全域上榜"
        match = re.match(r'^([^（(]+)', brand_name)
        if match:
            core_name = match.group(1).strip()
            if core_name and core_name not in variants:
                variants.append(core_name)
        
        # 注意：不拆分品牌名为单独的词！
        # "全域上榜" 不应拆成 "全域" + "上榜"，
        # 因为这些是常用词，会导致大量误匹配
        
        # 添加小写版本（用于英文品牌）
        for v in variants[:]:
            if v.lower() != v:
                variants.append(v.lower())
        
        # 添加拼音/英文变体（仅当品牌有明确英文名时由用户提供）
        # 不自动生成拼音，避免误匹配
        
        logger.info(f"品牌名变体: {variants}")
        return variants
    
    def identify_brand_accounts(
        self, 
        social_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        从社媒数据中识别品牌自有账号
        
        Args:
            social_data: 社媒数据（包含douyin和xiaohongshu）
            
        Returns:
            {
                'douyin': {'found': bool, 'accounts': [...], 'content_count': int},
                'xiaohongshu': {'found': bool, 'accounts': [...], 'content_count': int},
                'summary': {'total_brand_accounts': int, 'total_brand_content': int}
            }
        """
        result = {
            'douyin': {'found': False, 'accounts': [], 'content_count': 0},
            'xiaohongshu': {'found': False, 'accounts': [], 'content_count': 0},
            'summary': {'total_brand_accounts': 0, 'total_brand_content': 0}
        }
        
        # 分析抖音数据 - 检查多个可能的键名
        douyin_data = social_data.get('douyin', {})
        if douyin_data:
            # [Fix] 兼容不同数据结构: videos 或 top20
            videos = douyin_data.get('videos', []) or douyin_data.get('top20', [])
            if videos:
                douyin_result = self._analyze_platform_content(videos, platform='douyin')
                result['douyin'] = douyin_result
        
        # 分析小红书数据 - 检查多个可能的键名
        xhs_data = social_data.get('xiaohongshu', {})
        if xhs_data:
            # [Fix] 兼容不同数据结构: notes 或 top20
            notes = xhs_data.get('notes', []) or xhs_data.get('top20', [])
            if notes:
                xhs_result = self._analyze_platform_content(notes, platform='xiaohongshu')
                result['xiaohongshu'] = xhs_result

        
        # 汇总
        result['summary'] = {
            'total_brand_accounts': (
                len(result['douyin']['accounts']) + 
                len(result['xiaohongshu']['accounts'])
            ),
            'total_brand_content': (
                result['douyin']['content_count'] + 
                result['xiaohongshu']['content_count']
            )
        }
        
        logger.info(f"品牌账号识别结果: {result['summary']}")
        return result
    
    def _analyze_platform_content(
        self,
        content_list: List[Dict],
        platform: str
    ) -> Dict[str, Any]:
        """
        分析单个平台的内容，识别品牌账号
        
        Args:
            content_list: 内容列表
            platform: 平台名称
            
        Returns:
            {'found': bool, 'accounts': [...], 'content_count': int}
        """
        brand_accounts = {}  # {account_id: account_info}
        brand_content_count = 0
        
        for content in content_list:
            # 获取作者信息
            author_name = self._get_author_name(content, platform)
            author_id = self._get_author_id(content, platform)
            
            if not author_name:
                continue
            
            # 方式1: 检查账号名称是否包含品牌名
            is_brand, confidence = self._check_brand_account(author_name, author_id)
            
            # 方式2: 检查内容话题/描述是否包含品牌名 [Phase 11.3 新增]
            if not is_brand:
                is_brand, confidence = self._check_brand_in_content(content, platform)
            
            if is_brand:
                # 记录品牌账号
                if author_id not in brand_accounts:
                    brand_accounts[author_id] = {
                        'account_id': author_id,
                        'account_name': author_name,
                        'confidence': confidence,
                        'platform': platform,
                        'content_count': 0
                    }
                
                brand_accounts[author_id]['content_count'] += 1
                brand_content_count += 1
                
                # 记录品牌内容ID
                content_id = content.get('id') or content.get('aweme_id') or content.get('note_id')
                if content_id:
                    self._brand_content_ids.add(content_id)
        
        return {
            'found': len(brand_accounts) > 0,
            'accounts': list(brand_accounts.values()),
            'content_count': brand_content_count
        }
    
    def _check_brand_in_content(self, content: Dict, platform: str) -> Tuple[bool, float]:
        """
        检查内容中是否包含品牌名（话题标签、描述、标题）
        
        Args:
            content: 内容数据
            platform: 平台
            
        Returns:
            (is_brand: bool, confidence: float)
        """
        # 提取内容文本
        text_parts = []
        
        # 抖音字段
        if platform == 'douyin':
            text_parts.append(content.get('desc', ''))
            text_parts.append(content.get('title', ''))
            # 提取话题标签 (从text_extra)
            for hashtag in content.get('text_extra', []):
                text_parts.append(hashtag.get('hashtag_name', ''))
        
        # 小红书字段
        elif platform == 'xiaohongshu':
            text_parts.append(content.get('title', ''))
            text_parts.append(content.get('desc', ''))
            text_parts.append(content.get('note_card', {}).get('title', ''))
            text_parts.append(content.get('note_card', {}).get('desc', ''))
            # 提取话题标签
            for tag in content.get('tag_list', []):
                text_parts.append(tag.get('name', ''))
        
        # 合并文本
        combined_text = ' '.join([str(t) for t in text_parts if t]).lower()
        
        # 同时从desc中用正则提取hashtag内容（内容洞察的方式）
        import re
        desc_text = content.get('desc', '') or content.get('title', '') or ''
        hashtags_in_desc = re.findall(r'#([^\s#]+)', desc_text)
        combined_text += ' ' + ' '.join(hashtags_in_desc).lower()
        
        # 检查品牌名是否在内容中
        for variant in self.brand_variants:
            variant_lower = variant.lower()
            if variant_lower in combined_text:
                # 品牌名在内容中，很可能是品牌相关内容
                return True, 0.7
        
        return False, 0.0


    
    def _get_author_name(self, content: Dict, platform: str) -> str:
        """获取作者名称"""
        if platform == 'douyin':
            # 抖音数据结构
            author = content.get('author', {})
            return author.get('nickname', '') or author.get('name', '')
        else:
            # 小红书数据结构
            user = content.get('user', {}) or content.get('author', {})
            return user.get('nickname', '') or user.get('name', '')
    
    def _get_author_id(self, content: Dict, platform: str) -> str:
        """获取作者ID"""
        if platform == 'douyin':
            author = content.get('author', {})
            return author.get('uid', '') or author.get('sec_uid', '') or author.get('id', '')
        else:
            user = content.get('user', {}) or content.get('author', {})
            return user.get('user_id', '') or user.get('id', '')
    
    def _check_brand_account(
        self, 
        account_name: str,
        account_id: str = None
    ) -> Tuple[bool, float]:
        """
        检查账号是否属于品牌
        
        Args:
            account_name: 账号名称
            account_id: 账号ID
            
        Returns:
            (is_brand: bool, confidence: float)
        """
        # 1. 检查已知账号列表
        if account_id and account_id in self.known_accounts:
            return True, 1.0
        if account_name in self.known_accounts:
            return True, 1.0
        
        # 2. 品牌名匹配
        account_name_lower = account_name.lower()
        
        for variant in self.brand_variants:
            variant_lower = variant.lower()
            
            # 完全匹配
            if variant_lower == account_name_lower:
                return True, 0.95
            
            # 包含匹配（品牌名在账号名中）
            if variant_lower in account_name_lower:
                # 计算匹配比例
                ratio = len(variant) / len(account_name)
                if ratio > 0.5:  # 品牌名占账号名一半以上
                    return True, 0.85
                elif ratio > 0.3:
                    return True, 0.7
            
            # 账号名包含在品牌名中（短昵称）
            if account_name_lower in variant_lower and len(account_name) >= 2:
                ratio = len(account_name) / len(variant)
                if ratio > 0.5:
                    return True, 0.75
        
        # 3. 不匹配
        return False, 0.0
    
    def is_brand_content(self, content: Dict, platform: str = None) -> bool:
        """
        判断内容是否属于品牌
        
        Args:
            content: 内容数据
            platform: 平台
            
        Returns:
            是否是品牌内容
        """
        content_id = content.get('id') or content.get('aweme_id') or content.get('note_id')
        if content_id and content_id in self._brand_content_ids:
            return True
        
        # 实时检查
        author_name = self._get_author_name(content, platform or 'douyin')
        is_brand, confidence = self._check_brand_account(author_name)
        return is_brand and confidence >= 0.7
    
    def categorize_content(
        self, 
        social_data: Dict[str, Any],
        competitor_accounts: List[str] = None
    ) -> Dict[str, Any]:
        """
        对所有内容进行分类标记
        
        Args:
            social_data: 社媒数据
            competitor_accounts: 竞品账号列表
            
        Returns:
            {
                'brand_content': [...],      # 品牌自有内容
                'competitor_content': [...],  # 竞品内容
                'industry_content': [...]     # 行业内容
            }
        """
        competitor_accounts = competitor_accounts or []
        result = {
            'brand_content': [],
            'competitor_content': [],
            'industry_content': []
        }
        
        # 处理抖音内容
        for video in social_data.get('douyin', {}).get('videos', []):
            category = self._categorize_single_content(
                video, 'douyin', competitor_accounts
            )
            video['_source'] = category
            result[f'{category}_content'].append(video)
        
        # 处理小红书内容
        for note in social_data.get('xiaohongshu', {}).get('notes', []):
            category = self._categorize_single_content(
                note, 'xiaohongshu', competitor_accounts
            )
            note['_source'] = category
            result[f'{category}_content'].append(note)
        
        logger.info(
            f"内容分类完成: 品牌{len(result['brand_content'])}条, "
            f"竞品{len(result['competitor_content'])}条, "
            f"行业{len(result['industry_content'])}条"
        )
        
        return result
    
    def _categorize_single_content(
        self,
        content: Dict,
        platform: str,
        competitor_accounts: List[str]
    ) -> str:
        """
        分类单条内容
        
        Returns:
            'brand' | 'competitor' | 'industry'
        """
        author_name = self._get_author_name(content, platform)
        author_id = self._get_author_id(content, platform)
        
        # 1. 检查是否是品牌内容
        is_brand, confidence = self._check_brand_account(author_name, author_id)
        if is_brand and confidence >= 0.7:
            return 'brand'
        
        # 2. 检查是否是竞品内容
        if author_name in competitor_accounts or author_id in competitor_accounts:
            return 'competitor'
        
        # 3. 其他为行业内容
        return 'industry'
    
    def get_brand_content_stats(
        self, 
        brand_identification_result: Dict
    ) -> Dict[str, Any]:
        """
        获取品牌内容统计（用于评分）
        
        Args:
            brand_identification_result: identify_brand_accounts的返回结果
            
        Returns:
            {
                'has_brand_presence': bool,  # 是否有品牌存在
                'douyin_content_count': int,
                'xiaohongshu_content_count': int,
                'total_content_count': int,
                'account_count': int
            }
        """
        summary = brand_identification_result.get('summary', {})
        
        return {
            'has_brand_presence': summary.get('total_brand_accounts', 0) > 0,
            'douyin_content_count': brand_identification_result.get('douyin', {}).get('content_count', 0),
            'xiaohongshu_content_count': brand_identification_result.get('xiaohongshu', {}).get('content_count', 0),
            'total_content_count': summary.get('total_brand_content', 0),
            'account_count': summary.get('total_brand_accounts', 0)
        }


def identify_brand_accounts(
    social_data: Dict[str, Any],
    brand_name: str,
    known_accounts: List[str] = None
) -> Dict[str, Any]:
    """
    便捷函数：识别品牌账号
    
    Args:
        social_data: 社媒数据
        brand_name: 品牌名称
        known_accounts: 已知账号列表
        
    Returns:
        品牌账号识别结果
    """
    identifier = BrandAccountIdentifier(brand_name, known_accounts)
    return identifier.identify_brand_accounts(social_data)


def get_brand_content_for_scoring(
    social_data: Dict[str, Any],
    brand_name: str,
    known_accounts: List[str] = None
) -> Dict[str, Any]:
    """
    便捷函数：获取用于评分的品牌内容统计
    
    Args:
        social_data: 社媒数据
        brand_name: 品牌名称
        known_accounts: 已知账号列表
        
    Returns:
        品牌内容统计（用于评分）
    """
    identifier = BrandAccountIdentifier(brand_name, known_accounts)
    result = identifier.identify_brand_accounts(social_data)
    return identifier.get_brand_content_stats(result)
