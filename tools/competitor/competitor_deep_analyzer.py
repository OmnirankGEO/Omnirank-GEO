"""
竞品深度分析器 V1.0
基于用户主页数据的纯元数据分析（无需ASR）

分级服务：
- 基础版：元数据规律分析（标题/时间/互动/类型）
- 专业版：+ ASR话术提取 + 视频深度理解

ID格式规范：
- 抖音 sec_user_id: MS4wLjABAAAA... (Base64格式)
- 小红书 user_id: 55a1195467bc6516e873f84f (24位Hex)
"""

import asyncio
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from collections import Counter

# 导入现有的采集模块
from .competitor_collector import collect_competitor_data


class CompetitorDeepAnalyzer:
    """竞品深度分析器 - 基础版"""
    
    def __init__(self, client_brand: str):
        self.client_brand = client_brand
        
    def detect_platform(self, user_id: str) -> str:
        """
        根据ID格式自动识别平台
        
        Args:
            user_id: 用户ID
            
        Returns:
            平台名称: 'douyin' | 'xiaohongshu' | 'unknown'
        """
        if not user_id:
            return 'unknown'
            
        # 抖音 sec_user_id: 以 MS4wLjA 开头的Base64
        if user_id.startswith('MS4wLjA'):
            return 'douyin'
        
        # 小红书 user_id: 24位十六进制
        if len(user_id) == 24 and all(c in '0123456789abcdef' for c in user_id.lower()):
            return 'xiaohongshu'
        
        # 尝试其他模式
        if len(user_id) > 30 and 'AAAA' in user_id:
            return 'douyin'
            
        return 'unknown'
    
    async def analyze_specified_competitors(
        self,
        competitor_inputs: List[Dict[str, str]],
        max_posts: int = 30
    ) -> Dict[str, Any]:
        """
        对指定竞品进行深度分析
        
        Args:
            competitor_inputs: 竞品列表，每项格式:
                - 直接传入: {"platform": "douyin", "user_id": "MS4wLjA...", "nickname": "竞品A"}
                - 或自动识别: {"user_id": "MS4wLjA...", "nickname": "竞品A"}
            max_posts: 每个竞品采集的最大作品数
            
        Returns:
            深度分析结果
        """
        print(f"\n📊 开始竞品深度分析 ({len(competitor_inputs)} 个竞品)...")
        
        # 1. 标准化输入并识别平台
        competitors = []
        for input_data in competitor_inputs:
            user_id = input_data.get("user_id", "")
            platform = input_data.get("platform") or self.detect_platform(user_id)
            
            if platform == 'unknown':
                print(f"  ⚠️ 无法识别平台: {user_id[:20]}... (跳过)")
                continue
                
            competitors.append({
                "platform": platform,
                "account_id": user_id,
                "nickname": input_data.get("nickname", f"竞品_{len(competitors)+1}")
            })
        
        if not competitors:
            return {"error": "没有有效的竞品输入", "competitors": []}
        
        # 2. 采集竞品数据
        print(f"  📥 采集 {len(competitors)} 个竞品的作品数据...")
        collected_data = await collect_competitor_data(competitors, max_posts=max_posts)
        
        # 3. 对每个竞品进行深度分析
        analysis_results = []
        for comp_data in collected_data:
            nickname = comp_data.get("nickname", "未知")
            posts = comp_data.get("posts", [])
            
            if not posts:
                print(f"  ⚠️ {nickname}: 无作品数据")
                analysis_results.append({
                    **comp_data,
                    "deep_analysis": {"error": "无作品数据"}
                })
                continue
            
            print(f"  🔍 分析 {nickname} ({len(posts)} 条作品)...")
            
            # 执行深度分析
            deep_analysis = self._analyze_single_competitor(comp_data)
            analysis_results.append({
                **comp_data,
                "deep_analysis": deep_analysis
            })
        
        # 4. 生成综合对比报告
        comparison = self._generate_comparison(analysis_results)
        
        return {
            "client_brand": self.client_brand,
            "competitors_analyzed": len(analysis_results),
            "competitors": analysis_results,
            "comparison": comparison,
            "report_markdown": self._generate_report_markdown(analysis_results, comparison)
        }
    
    def _analyze_single_competitor(self, comp_data: Dict) -> Dict[str, Any]:
        """分析单个竞品的内容策略"""
        posts = comp_data.get("posts", [])
        platform = comp_data.get("platform", "")
        
        if not posts:
            return {}
        
        # 1. 内容策略分析
        content_strategy = self._analyze_content_strategy(posts, platform)
        
        # 2. 爆款规律提取
        viral_patterns = self._extract_viral_patterns(posts, platform)
        
        # 3. 标题规律分析
        title_patterns = self._analyze_title_patterns(posts, platform)
        
        # 4. 发布节奏分析
        posting_rhythm = self._analyze_posting_rhythm(posts)
        
        return {
            "content_strategy": content_strategy,
            "viral_patterns": viral_patterns,
            "title_patterns": title_patterns,
            "posting_rhythm": posting_rhythm
        }
    
    def _analyze_content_strategy(self, posts: List[Dict], platform: str) -> Dict:
        """分析内容策略"""
        total_posts = len(posts)
        
        # 计算互动数据
        if platform == "douyin":
            total_likes = sum(p.get("likes", 0) for p in posts)
            total_comments = sum(p.get("comments", 0) for p in posts)
            total_shares = sum(p.get("shares", 0) for p in posts)
            avg_engagement = (total_likes + total_comments * 2 + total_shares * 3) / total_posts if total_posts else 0
        else:  # xiaohongshu
            total_likes = sum(p.get("likes", 0) for p in posts)
            total_collects = sum(p.get("collects", 0) for p in posts)
            avg_engagement = (total_likes + total_collects * 2) / total_posts if total_posts else 0
        
        # 爆款率（互动>平均值3倍的作品）
        viral_count = 0
        for p in posts:
            if platform == "douyin":
                eng = p.get("likes", 0) + p.get("comments", 0) * 2 + p.get("shares", 0) * 3
            else:
                eng = p.get("likes", 0) + p.get("collects", 0) * 2
            if eng > avg_engagement * 3:
                viral_count += 1
        
        viral_rate = (viral_count / total_posts * 100) if total_posts else 0
        
        return {
            "total_posts_analyzed": total_posts,
            "total_likes": total_likes,
            "avg_engagement": round(avg_engagement, 1),
            "viral_count": viral_count,
            "viral_rate": f"{viral_rate:.1f}%"
        }
    
    def _extract_viral_patterns(self, posts: List[Dict], platform: str) -> List[Dict]:
        """提取TOP5爆款规律"""
        # 按互动排序
        def get_engagement(p):
            if platform == "douyin":
                return p.get("likes", 0) + p.get("comments", 0) * 2 + p.get("shares", 0) * 3
            return p.get("likes", 0) + p.get("collects", 0) * 2
        
        sorted_posts = sorted(posts, key=get_engagement, reverse=True)
        top_posts = sorted_posts[:5]
        
        patterns = []
        for p in top_posts:
            title = p.get("desc", "") or p.get("title", "")
            
            # 分析标题特征
            hooks = []
            if re.match(r'^[0-9]', title):
                hooks.append("数字开头")
            if "？" in title or "?" in title:
                hooks.append("疑问式")
            if "！" in title or "!" in title:
                hooks.append("感叹号")
            if any(w in title for w in ["揭秘", "真相", "必看", "千万别"]):
                hooks.append("悬念词")
            if any(w in title for w in ["干货", "攻略", "教程", "方法"]):
                hooks.append("价值承诺")
                
            patterns.append({
                "title": title[:50] + ("..." if len(title) > 50 else ""),
                "engagement": get_engagement(p),
                "likes": p.get("likes", 0),
                "hooks_detected": hooks if hooks else ["常规标题"]
            })
        
        return patterns
    
    def _analyze_title_patterns(self, posts: List[Dict], platform: str) -> Dict:
        """分析标题规律"""
        titles = [p.get("desc", "") or p.get("title", "") for p in posts]
        
        patterns = {
            "数字开头": 0,
            "疑问句": 0,
            "感叹句": 0,
            "悬念词": 0,
            "价值承诺": 0,
            "emoji使用": 0
        }
        
        for title in titles:
            if re.match(r'^[0-9]', title):
                patterns["数字开头"] += 1
            if "？" in title or "?" in title:
                patterns["疑问句"] += 1
            if "！" in title or "!" in title:
                patterns["感叹句"] += 1
            if any(w in title for w in ["揭秘", "真相", "必看", "千万别", "竟然"]):
                patterns["悬念词"] += 1
            if any(w in title for w in ["干货", "攻略", "教程", "方法", "技巧"]):
                patterns["价值承诺"] += 1
            if re.search(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF]', title):
                patterns["emoji使用"] += 1
        
        total = len(titles)
        return {
            "total_analyzed": total,
            "patterns": {k: f"{v}/{total} ({v/total*100:.0f}%)" for k, v in patterns.items() if v > 0}
        }
    
    def _analyze_posting_rhythm(self, posts: List[Dict]) -> Dict:
        """分析发布节奏"""
        # 提取发布时间
        timestamps = []
        for p in posts:
            ts = p.get("create_time")
            if ts:
                try:
                    if isinstance(ts, int):
                        timestamps.append(datetime.fromtimestamp(ts))
                    elif isinstance(ts, str):
                        timestamps.append(datetime.fromisoformat(ts))
                except:
                    pass
        
        if len(timestamps) < 2:
            return {"insufficient_data": True}
        
        # 计算发布频率
        timestamps.sort()
        days_span = (timestamps[-1] - timestamps[0]).days or 1
        posts_per_week = len(timestamps) / days_span * 7
        
        # 分析星期分布
        weekday_counts = Counter(t.strftime("%A") for t in timestamps)
        
        # 分析时间段分布
        hour_counts = Counter(t.hour for t in timestamps)
        peak_hours = [h for h, c in hour_counts.most_common(3)]
        
        return {
            "posts_per_week": round(posts_per_week, 1),
            "most_active_days": [day for day, _ in weekday_counts.most_common(2)],
            "peak_hours": peak_hours,
            "analysis_period_days": days_span
        }
    
    def _generate_comparison(self, results: List[Dict]) -> Dict:
        """生成竞品对比分析"""
        if not results:
            return {}
        
        comparison = {
            "engagement_ranking": [],
            "viral_rate_ranking": [],
            "posting_frequency_ranking": []
        }
        
        for r in results:
            da = r.get("deep_analysis", {})
            cs = da.get("content_strategy", {})
            pr = da.get("posting_rhythm", {})
            
            comparison["engagement_ranking"].append({
                "name": r.get("nickname", "未知"),
                "avg_engagement": cs.get("avg_engagement", 0)
            })
            comparison["viral_rate_ranking"].append({
                "name": r.get("nickname", "未知"),
                "viral_rate": cs.get("viral_rate", "0%")
            })
            if not pr.get("insufficient_data"):
                comparison["posting_frequency_ranking"].append({
                    "name": r.get("nickname", "未知"),
                    "posts_per_week": pr.get("posts_per_week", 0)
                })
        
        # 排序
        comparison["engagement_ranking"].sort(key=lambda x: x["avg_engagement"], reverse=True)
        
        return comparison
    
    def _generate_report_markdown(self, results: List[Dict], comparison: Dict) -> str:
        """生成Markdown格式的分析报告"""
        lines = [
            f"# 竞品深度分析报告",
            f"",
            f"**分析品牌**: {self.client_brand}",
            f"**分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"**竞品数量**: {len(results)}",
            f"",
            "---",
            "",
            "## 一、竞品概览矩阵",
            "",
            "| 竞品 | 平台 | 作品数 | 平均互动 | 爆款率 | 发文频率 |",
            "|------|------|:------:|:-------:|:------:|:-------:|"
        ]
        
        for r in results:
            da = r.get("deep_analysis", {})
            cs = da.get("content_strategy", {})
            pr = da.get("posting_rhythm", {})
            
            lines.append(
                f"| {r.get('nickname', '?')} | {r.get('platform', '?')} | "
                f"{cs.get('total_posts_analyzed', 0)} | "
                f"{cs.get('avg_engagement', 0):,.0f} | "
                f"{cs.get('viral_rate', '?')} | "
                f"{pr.get('posts_per_week', '?')}篇/周 |"
            )
        
        lines.extend([
            "",
            "---",
            "",
            "## 二、TOP5爆款内容分析",
            ""
        ])
        
        for r in results:
            nickname = r.get("nickname", "未知")
            viral = r.get("deep_analysis", {}).get("viral_patterns", [])
            
            if viral:
                lines.append(f"### {nickname}")
                lines.append("")
                for i, v in enumerate(viral[:3], 1):
                    lines.append(f"**{i}. {v.get('title', '无标题')}**")
                    lines.append(f"- 互动: {v.get('engagement', 0):,}")
                    lines.append(f"- 钩子: {', '.join(v.get('hooks_detected', []))}")
                    lines.append("")
        
        lines.extend([
            "---",
            "",
            "## 三、标题规律总结",
            ""
        ])
        
        for r in results:
            nickname = r.get("nickname", "未知")
            patterns = r.get("deep_analysis", {}).get("title_patterns", {}).get("patterns", {})
            
            if patterns:
                lines.append(f"### {nickname}")
                for k, v in patterns.items():
                    lines.append(f"- {k}: {v}")
                lines.append("")
        
        lines.extend([
            "---",
            "",
            "## 四、对标建议",
            "",
            "基于以上分析，建议关注：",
            ""
        ])
        
        # 添加建议
        if comparison.get("engagement_ranking"):
            top = comparison["engagement_ranking"][0]
            lines.append(f"1. **互动标杆**: {top['name']} (平均互动 {top['avg_engagement']:,.0f})")
        
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("> 💡 **专业版升级**: 获取完整话术提取 + 视频深度理解分析")
        
        return "\n".join(lines)


# 便捷函数
async def analyze_competitors_deep(
    competitor_ids: List[Dict[str, str]],
    client_brand: str,
    max_posts: int = 30
) -> Dict[str, Any]:
    """
    竞品深度分析便捷函数
    
    Args:
        competitor_ids: 竞品列表，每项包含 user_id 和可选的 platform, nickname
        client_brand: 客户品牌名
        max_posts: 每个竞品采集的最大作品数
    """
    analyzer = CompetitorDeepAnalyzer(client_brand)
    return await analyzer.analyze_specified_competitors(competitor_ids, max_posts)


__all__ = ["CompetitorDeepAnalyzer", "analyze_competitors_deep"]
