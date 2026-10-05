"""



:
1.  ()
2.  GEOAgent (LLM + )
3.  Agent ()
"""

from typing import List, Dict, Set, Any
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
from collections import Counter
import sys
import os

# agents
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

# Simple response class
class ToolResponse:
    def __init__(self, success, data, message=""):
        self.success = success
        self.data = data
        self.message = message

# 
from .user_works_analyzer import (
    analyze_candidate_accounts,
    select_final_asr_videos
)

#  Agents
try:
    from agents.author_identifier_agent import AuthorIdentifierAgent
    from agents.brand_safety_agent import BrandSafetyAgent
    AGENTS_AVAILABLE = True
except ImportError as e:
    print(f" Agents: {e}")
    AGENTS_AVAILABLE = False


async def identify_competitors(
    social_data: dict,
    brand_name: str,
    industry: str = "",
    deep_analysis: bool = False,
    keyword_count: int = 5,
    use_author_verification: bool = True,  #  
    own_accounts: list = None,              # [NEW] 客户自有账号（排除在竞品外）
    specified_competitors: list = None      # [NEW] 指定竞品（优先分析）
) -> ToolResponse:
    """
     - Agents
    
    
    1. 
    2. 
    3.  GEOAgent
    4.  Agent
    
    Args:
        social_data: 社媒数据
        brand_name: 品牌名
        industry: 行业
        deep_analysis: 是否深度分析
        keyword_count: 关键词数量
        use_author_verification: 是否使用Agent验证
        own_accounts: 客户自有账号列表（这些账号将被排除在竞品外）
        specified_competitors: 指定竞品列表（优先深度分析这些竞品）
    
    Returns:
        ToolResponse 竞品识别结果
    """
    try:
        import re
        
        def parse_count(val) -> int:
            """ - /w"""
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str):
                val = val.strip()
                multiplier = 1
                if "w" in val.lower() or "" in val:
                    multiplier = 10000
                    val = re.sub(r'[wW]', '', val)
                try:
                    num_part = re.search(r'\d+(\.\d+)?', val)
                    if num_part:
                        return int(float(num_part.group()) * multiplier)
                except:
                    pass
            return 0
        
        def calc_engagement(item: dict, platform: str) -> int:
            """ - Coze : ×3 + ×2 + ×1"""
            if platform == "douyin":
                likes = parse_count(item.get("like_count", 0) or item.get("digg_count", 0) or 0)
                collects = parse_count(item.get("collect_count", 0) or 0)
                shares = parse_count(item.get("share_count", 0) or 0)
                return likes + collects * 2 + shares * 3
            elif platform == "xiaohongshu":
                likes = parse_count(item.get("liked_count", 0) or item.get("like_count", 0) or 0)
                collects = parse_count(item.get("collected_count", 0) or item.get("collect_count", 0) or 0)
                return likes + collects * 2
            return 0
        
        competitors = []
        seen_names = set()
        
        # [ENHANCED] 构建排除集合：品牌变体 + 客户自有账号
        brand_variants = {
            brand_name,
            brand_name.lower(),
            brand_name.replace(" ", ""),
        }
        
        # [NEW] 添加客户自有账号到排除集合
        own_accounts_set = set()
        if own_accounts:
            for acc in own_accounts:
                own_accounts_set.add(acc)
                own_accounts_set.add(acc.lower())
            print(f"   📋 已排除客户自有账号: {len(own_accounts)}个 ({', '.join(own_accounts[:3])}{'...' if len(own_accounts) > 3 else ''})")
        
        
        # ==========  ==========
        douyin_data = social_data.get("douyin", {})
        douyin_videos = douyin_data.get("videos", []) or douyin_data.get("top20", [])
        
        for video in douyin_videos[:20]:
            if not isinstance(video, dict):
                continue
            
            #  - 
            author = ""
            if "author" in video and isinstance(video["author"], dict):
                author = video["author"].get("nickname", "")
            if not author:
                author = video.get("author_nickname", "")
            
            # [ENHANCED] 排除：品牌变体 + 客户自有账号
            if not author or any(v in author for v in brand_variants):
                continue
            if author in own_accounts_set or author.lower() in own_accounts_set:
                continue
            # [Phase 12.9] 粉丝量阈值过滤：只保留可对标学习的账号
            # - 过滤: >50万粉丝（官方/大V，无参考价值）
            # - 保留: 2000-200000粉丝（核心对标群体）
            follower_count = 0
            if "author" in video and isinstance(video["author"], dict):
                follower_count = parse_count(video["author"].get("follower_count", 0))
            if follower_count > 500000:  # 50万+粉丝直接过滤
                continue
            
            if author in seen_names:
                continue
            seen_names.add(author)
            
            engagement = calc_engagement(video, "douyin")
            
            #  ID
            author_id = ""
            if "author" in video and isinstance(video["author"], dict):
                author_id = video["author"].get("id", "") or video["author"].get("uid", "")
                # follower_count已在上面提取
            
            competitors.append({
                "name": author,
                "nickname": author,
                "platform": "douyin",
                "total_engagement": engagement,
                "appearance_count": 1,
                "user_id": author_id,
                "follower_count": follower_count
            })
        
        # ==========  ==========
        xhs_data = social_data.get("xiaohongshu", {})
        xhs_notes = xhs_data.get("notes", []) or xhs_data.get("top20", [])
        
        for note in xhs_notes[:20]:
            if not isinstance(note, dict):
                continue
            
            # 
            author = ""
            user_data = note.get("user", {})
            if isinstance(user_data, dict):
                author = user_data.get("nickname", "") or user_data.get("name", "")
            if not author:
                author = note.get("author_nickname", "") or note.get("nickname", "")
            
            # [ENHANCED] 排除：品牌变体 + 客户自有账号
            if not author or any(v in author for v in brand_variants):
                continue
            if author in own_accounts_set or author.lower() in own_accounts_set:
                continue
            # [Phase 12.9] 粉丝量阈值过滤：只保留可对标学习的账号
            follower_count = 0
            if isinstance(user_data, dict):
                follower_count = parse_count(user_data.get("fans", 0) or user_data.get("follower_count", 0))
            if follower_count > 500000:  # 50万+粉丝直接过滤
                continue
            
            if author in seen_names:
                continue
            seen_names.add(author)
            
            engagement = calc_engagement(note, "xiaohongshu")
            
            #  ID
            user_id = ""
            if isinstance(user_data, dict):
                user_id = user_data.get("user_id", "") or user_data.get("id", "")
            
            competitors.append({
                "name": author,
                "nickname": author,
                "platform": "xiaohongshu",
                "total_engagement": engagement,
                "appearance_count": 1,
                "user_id": str(user_id) if user_id else "",
                "follower_count": follower_count
            })
        
        print(f"   : {len(competitors)} ")
        
        # ==========  : GEOAgent ==========
        if use_author_verification and AGENTS_AVAILABLE:
            try:
                author_agent = AuthorIdentifierAgent(llm_client=None)  # LLM
                
                verification_result = await author_agent.batch_verify(
                    competitors,
                    brand_name
                )
                
                verified_competitors = verification_result['verified_competitors']
                excluded = verification_result['excluded']
                
                print(f"   Agent:  {len(verified_competitors)} ,  {len(excluded)} ")
                
                if excluded:
                    print(f"   :")
                    for exc in excluded[:3]:  # 3
                        print(f"     - {exc['account']}: {exc['reason']}")
                
                competitors = verified_competitors
                
            except Exception as e:
                print(f"   Agent: {e}")
        
        # ==========  : ==========
        if AGENTS_AVAILABLE:
            try:
                safety_agent = BrandSafetyAgent(brand_name)
                safety_check = safety_agent.check_competitor_list(competitors)
                
                if not safety_check['safe']:
                    print(f"   :  {safety_check['removed_count']} ")
                    competitors = safety_check['cleaned_competitors']
                else:
                    print(f"   : ")
                    
            except Exception as e:
                print(f"   : {e}")
        
        # 排序
        competitors.sort(key=lambda x: x["total_engagement"], reverse=True)
        
        # ========== [NEW] 指定竞品深度分析 ==========
        specified_analysis = None
        asr_candidates = []
        
        if specified_competitors and len(specified_competitors) > 0:
            print(f"   🎯 开始指定竞品深度分析 ({len(specified_competitors)}个)...")
            try:
                from .specified_competitor_analyzer import analyze_specified_competitors
                
                specified_analysis = await analyze_specified_competitors(
                    competitor_inputs=specified_competitors,
                    brand_name=brand_name,
                    works_per_user=20,   # 获取最新20条
                    top_asr_count=3       # 每个竞品选Top3
                )
                
                # 提取ASR候选
                asr_candidates = specified_analysis.get("asr_candidates", [])
                
                print(f"   ✅ 指定竞品分析完成: {len(specified_analysis.get('competitors', []))}个竞品, {len(asr_candidates)}个ASR候选")
                
            except Exception as e:
                print(f"   ⚠️ 指定竞品分析失败: {e}")
                import traceback
                traceback.print_exc()
        
        return ToolResponse(
            success=True,
            data={
                "competitors": competitors[:10],
                "total_identified": len(competitors),
                "method": "hot_content_extraction_with_agents",
                # [NEW] 指定竞品深度分析数据
                "specified_competitor_analysis": specified_analysis,
                "asr_candidates": asr_candidates
            },
            message=f"识别 {len(competitors)} 个竞品，指定竞品 {len(specified_competitors or [])} 个已深度分析"
        )
        
    except Exception as e:
        return ToolResponse(
            success=False,
            data={"competitors": [], "error": str(e)},
            message=f": {str(e)}"
        )


async def identify_competitors_old(
    social_data: dict,
    brand_name: str,
    own_accounts: List[str] = None,
    industry_keywords: List[str] = None,
    industry: str = "",
    top_n: int = 5,
    deep_analysis: bool = True,
    keyword_count: int = 5
) -> dict:
    """
    
    
    Args:
        social_data:  ( douyin, xiaohongshu)
        brand_name:  ()
        own_accounts:  ()
        industry_keywords:  ()
        top_n: N
    
    Returns:
        {
            "own_accounts_found": [...],  # 
            "competitors": [...],         # 
            "stats": {...}                # 
        }
    """
    if own_accounts is None:
        own_accounts = []
    if industry_keywords is None:
        industry_keywords = []
    
    # 
    result = {
        "own_accounts_found": [],
        "competitors": [],
        "stats": {
            "total_authors_scanned": 0,
            "own_accounts_excluded": 0,
            "competitors_identified": 0
        }
    }
    
    #  ()
    brand_variants = _generate_brand_variants(brand_name)
    own_accounts_set = set(acc.lower() for acc in own_accounts)
    
    # 
    author_data = {}  # {author_id: {...}}
    
    # 1. 
    douyin_data = social_data.get("douyin", {})
    videos = douyin_data.get("videos", []) or douyin_data.get("top20", [])
    
    for video in videos:
        author = video.get("author", {})
        author_id = author.get("id", "") or author.get("sec_uid", "")
        nickname = author.get("nickname", "")
        
        if not author_id or not nickname:
            continue
        
        # 
        if author_id not in author_data:
            author_data[author_id] = {
                "platform": "douyin",
                "account_id": author_id,
                "nickname": nickname,
                "videos": [],
                "total_engagement": 0,
                "appearance_count": 0,
                "keywords_matched": set()
            }
        
        # 
        author_data[author_id]["videos"].append(video)
        author_data[author_id]["appearance_count"] += 1
        author_data[author_id]["total_engagement"] += (
            video.get("like_count", 0) +
            video.get("share_count", 0) * 2 +
            video.get("collect_count", 0)
        )
        
        # 
        kw = video.get("_search_keyword", "")
        if kw:
            author_data[author_id]["keywords_matched"].add(kw)
    
    # 2. 
    xhs_data = social_data.get("xiaohongshu", {})
    notes = xhs_data.get("notes", []) or xhs_data.get("top20", [])
    
    for note in notes:
        user = note.get("user", {})
        user_id = user.get("user_id", "") or user.get("id", "")
        nickname = user.get("nickname", "")
        
        if not user_id or not nickname:
            continue
        
        # 
        if user_id not in author_data:
            author_data[user_id] = {
                "platform": "xiaohongshu",
                "account_id": user_id,
                "nickname": nickname,
                "notes": [],
                "total_engagement": 0,
                "appearance_count": 0,
                "keywords_matched": set()
            }
        
        # 
        if "notes" not in author_data[user_id]:
            author_data[user_id]["notes"] = []
        author_data[user_id]["notes"].append(note)
        author_data[user_id]["appearance_count"] += 1
        author_data[user_id]["total_engagement"] += (
            note.get("like_count", 0) +
            note.get("collect_count", 0) * 2
        )
        
        # 
        kw = note.get("_search_keyword", "")
        if kw:
            author_data[user_id]["keywords_matched"].add(kw)
    
    result["stats"]["total_authors_scanned"] = len(author_data)
    
    # 3.  vs 
    own_found = []
    potential_competitors = []
    
    for author_id, data in author_data.items():
        nickname = data.get("nickname", "")
        nickname_lower = nickname.lower()
        
        # 
        is_own = False
        
        # 1: 
        if nickname_lower in own_accounts_set:
            is_own = True
        
        # 2: 
        if not is_own:
            for variant in brand_variants:
                if variant.lower() in nickname_lower:
                    is_own = True
                    break
        
        if is_own:
            own_found.append({
                "nickname": nickname,
                "platform": data.get("platform"),
                "account_id": author_id,
                "match_reason": ""
            })
            result["stats"]["own_accounts_excluded"] += 1
        else:
            #  LLM 
            potential_competitors.append(data)
    
    # 4. LLM  - 
    if potential_competitors:
        # 
        candidate_names = [c.get("nickname", "") for c in potential_competitors]
        
        #  LLM 
        llm_result = await _llm_check_own_accounts(
            brand_name=brand_name,
            account_names=candidate_names,
            industry=industry
        )
        
        llm_own_accounts = set(llm_result.get("own_accounts", []))
        result["llm_reasoning"] = llm_result.get("reasoning", "")
        
        # 
        filtered_competitors = []
        for comp in potential_competitors:
            nickname = comp.get("nickname", "")
            if nickname in llm_own_accounts:
                own_found.append({
                    "nickname": nickname,
                    "platform": comp.get("platform"),
                    "account_id": comp.get("account_id"),
                    "match_reason": "LLM"
                })
                result["stats"]["own_accounts_excluded"] += 1
            else:
                filtered_competitors.append(comp)
        
        potential_competitors = filtered_competitors
    
    # 4.5  ()
    asr_candidates = []
    
    if deep_analysis and potential_competitors:
        print("    4.5: ...")
        
        # 
        works_result = await analyze_candidate_accounts(
            candidates=potential_competitors,
            brand_name=brand_name,
            works_per_user=15
        )
        
        # 
        for own in works_result.get("own_accounts", []):
            own_found.append({
                "nickname": own.get("nickname"),
                "platform": own.get("platform"),
                "account_id": own.get("account_id"),
                "match_reason": f": {own.get('works_analysis', {}).get('reason', '')}"
            })
            result["stats"]["own_accounts_excluded"] += 1
        
        # 
        potential_competitors = works_result.get("competitors", [])
        
        #  ()
        potential_competitors.extend(works_result.get("uncertain", []))
        
        #  ASR 
        asr_candidates = select_final_asr_videos(
            works_result.get("asr_candidates", []),
            keyword_count=keyword_count
        )
        
        result["asr_candidates"] = asr_candidates
        print(f"    {len(asr_candidates)}  ASR")
    
    # 5. 
    for comp in potential_competitors:
        priority_score = _calculate_priority_score(comp, industry_keywords)
        comp["priority_score"] = priority_score
        comp["keywords_matched"] = list(comp.get("keywords_matched", []))
    
    result["own_accounts_found"] = own_found
    
    # 4.  TOP N 
    # 
    by_platform = {}
    for comp in potential_competitors:
        platform = comp.get("platform", "unknown")
        if platform not in by_platform:
            by_platform[platform] = []
        by_platform[platform].append(comp)
    
    # 
    for platform in by_platform:
        by_platform[platform].sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    
    # 
    top_competitors = []
    platforms = list(by_platform.keys())
    per_platform_min = max(1, top_n // len(platforms)) if platforms else 0
    
    #  per_platform_min 
    for platform in platforms:
        for comp in by_platform[platform][:per_platform_min]:
            if comp not in top_competitors:
                top_competitors.append(comp)
    
    #  top_n
    all_remaining = []
    for platform in platforms:
        all_remaining.extend(by_platform[platform][per_platform_min:])
    all_remaining.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    
    for comp in all_remaining:
        if len(top_competitors) >= top_n:
            break
        if comp not in top_competitors:
            top_competitors.append(comp)
    
    # 
    top_competitors.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    top_competitors = top_competitors[:top_n]
    
    for comp in top_competitors:
        comp["reason"] = _generate_reason(comp)
        # 
        comp.pop("videos", None)
        comp.pop("notes", None)
    
    result["competitors"] = top_competitors
    result["stats"]["competitors_identified"] = len(top_competitors)
    result["stats"]["platforms_covered"] = list(set(c.get("platform") for c in top_competitors))
    
    return result


def _generate_brand_variants(brand_name: str) -> List[str]:
    """
     ()
    
    : LLM _llm_check_own_accounts 
    """
    variants = [brand_name]
    
    # 
    for suffix in ["", "", "", "", "", "", "", ""]:
        if brand_name.endswith(suffix):
            variants.append(brand_name[:-len(suffix)])
            break
    
    #  ( ""  "")
    core_name = variants[-1] if len(variants) > 1 else brand_name
    if len(core_name) >= 2:
        variants.append(core_name)
    
    return list(set(variants))


async def _llm_check_own_accounts(
    brand_name: str,
    account_names: List[str],
    industry: str = ""
) -> dict:
    """
     LLM 
    
    Args:
        brand_name: 
        account_names: 
        industry:  ()
    
    Returns:
        {
            "own_accounts": ["1", "2"],
            "reasoning": ""
        }
    """
    import os
    import json
    from services.llm.deepseek_key_pool import has_deepseek_key, adeepseek_post_with_failover

    if not has_deepseek_key():
        #  API Key
        return {"own_accounts": [], "reasoning": "No API key available"}
    
    #  Prompt - 
    prompt = f""" "{brand_name}" 

: {brand_name}
{f": {industry}" if industry else ""}

:
{chr(10).join(f"- {name}" for name in account_names[:20])}

:

1. ****:  "-"  "+"
   - : "-""-" 
   - 

2. ****: 

3. **/**:  "-""+"

4. **/**: /Slogan


-  "{brand_name}"
- 

 JSON :
{{
  "own_accounts": ["1", "2"],
  "detected_patterns": [""],
  "reasoning": ""
}}

 JSON"""

    try:
        # [failover 2026-06-11] 多 key 失败自动换下一个重试(单 key=直调·向后兼容)·内含 llm_track
        response = await adeepseek_post_with_failover(
            {
                "model": DEEPSEEK_OFFICIAL_FLASH,  # 官方DeepSeek API
                "messages": [
                    {"role": "system", "content": "You are a brand account identification expert. Always respond in valid JSON format."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 500
            },
            timeout=30.0,
            track_name="competitor_own_account_identifier",
            track_model=DEEPSEEK_OFFICIAL_FLASH,
        )
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        #  JSON
        try:
            #  JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            parsed = json.loads(content)
            return {
                "own_accounts": parsed.get("own_accounts", []),
                "reasoning": parsed.get("reasoning", "")
            }
        except json.JSONDecodeError:
            return {"own_accounts": [], "reasoning": f"JSON parse error: {content[:100]}"}

    except Exception as e:
        return {"own_accounts": [], "reasoning": f"Exception: {str(e)}"}



def _calculate_priority_score(data: dict, industry_keywords: List[str]) -> float:
    """"""
    score = 0.0
    
    #  ( 30%)
    appearance = data.get("appearance_count", 0)
    score += min(appearance * 10, 30)
    
    #  ( 40%)
    engagement = data.get("total_engagement", 0)
    if engagement >= 10000:
        score += 40
    elif engagement >= 5000:
        score += 30
    elif engagement >= 1000:
        score += 20
    elif engagement >= 500:
        score += 10
    elif engagement >= 100:
        score += 5
    
    #  ( 20%)
    keywords_matched = len(data.get("keywords_matched", []))
    score += min(keywords_matched * 5, 20)
    
    #  ( 10%)
    content_count = len(data.get("videos", [])) + len(data.get("notes", []))
    score += min(content_count * 2, 10)
    
    return round(score, 2)


def _generate_reason(comp: dict) -> str:
    """"""
    reasons = []
    
    appearance = comp.get("appearance_count", 0)
    if appearance >= 3:
        reasons.append(f" {appearance} ")
    elif appearance >= 2:
        reasons.append("")
    
    engagement = comp.get("total_engagement", 0)
    if engagement >= 1000:
        reasons.append(f" ({engagement:,})")
    
    keywords = comp.get("keywords_matched", [])
    if len(keywords) >= 2:
        reasons.append(f": {', '.join(keywords[:2])}")
    
    if not reasons:
        reasons.append("")
    
    return "; ".join(reasons)


# 
__all__ = ["identify_competitors"]
