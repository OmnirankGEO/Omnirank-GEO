©ô"""
GEO
 GEO
 Coze 8
"""
import json
from typing import Any, Dict
from agentscope.tool import ToolResponse
def calculate_opportunity_loss(
 mention_rate: float,
 industry: str = "TikTok"
) -> Dict[str, Any]:
 """
 AI
 Args:
  mention_rate: AI (0-100)
  industry:
 Returns:
  {
   daily_lost_searches: int,
   monthly_lost_value: int,
   description: str
  }
 """
 #
 benchmarks = {
  "TikTok": {
   "daily_searches": 2000,  #
   "avg_order_value": 250000, #
   "conversion_rate": 0.01  #
  }
 }
 data = benchmarks.get(industry, benchmarks["TikTok"])
 #
 daily_lost = int(data["daily_searches"] * (1 - mention_rate / 100))
 monthly_lost_value = int(
  daily_lost * 30 *
  data["conversion_rate"] *
  data["avg_order_value"]
 )
 return {
  "daily_lost_searches": daily_lost,
  "monthly_lost_value": monthly_lost_value,
  "monthly_lost_value_wan": monthly_lost_value / 10000,
  "description": f"{daily_lost:,},Â¥{monthly_lost_value/10000:.0f}"
 }
# GEO 8 100- Coze
SCORING_DIMENSIONS = {
 # 1: (15)
 "web_search_score": {
  "weight": 15,
  "desc": " ",
  "full_desc": ""
 },
 # 2: (15)
 "platform_score": {
  "weight": 15,
  "desc": " ",
  "full_desc": ""
 },
 # 3: (15) -
 "content_quality_score": {
  "weight": 15,
  "desc": " ",
  "full_desc": ""
 },
 # 4: (10)
 "authority_score": {
  "weight": 10,
  "desc": " ",
  "full_desc": ""
 },
 # 5: (10) -
 "brand_ownership_score": {
  "weight": 10,
  "desc": " ",
  "full_desc": "AI"
 },
 # 6: AI (20)
 "ai_visibility_score": {
  "weight": 20,
  "desc": " AI",
  "full_desc": "DeepSeek/Kimi/AI"
 },
 # 7: AI (10) -
 "ai_citation_score": {
  "weight": 10,
  "desc": " AI",
  "full_desc": "AI"
 },
 # 8: (5) -
 "update_frequency_score": {
  "weight": 5,
  "desc": " ",
  "full_desc": ""
 }
}
def get_coverage_level(score: float) -> tuple[str, str]:
 """"""
 if score >= 80:
  return ("", "AI")
 elif score >= 60:
  return ("", "")
 elif score >= 40:
  return ("", ",")
 elif score >= 20:
  return ("", ",")
 else:
  return ("", "")
async def calculate_geo_score(
 douyin_data: dict = None,
 xiaohongshu_data: dict = None,
 web_search_data: dict = None,
 ai_visibility_data: dict = None,
 brand_name: str = ""
) -> ToolResponse:
 """
  GEO (8 - Coze)
 Args:
  douyin_data (dict):
  xiaohongshu_data (dict):
  web_search_data (dict):
  ai_visibility_data (dict): AI
  brand_name (str):
 Returns:
  ToolResponse:
 """
 scores = {}
 # ========================================
 # - NoneType
 # ========================================
 if douyin_data is None:
  douyin_data = {}
 if xiaohongshu_data is None:
  xiaohongshu_data = {}
 if web_search_data is None:
  web_search_data = {}
 if ai_visibility_data is None:
  ai_visibility_data = {}
 # ========================================
 # 1: (15)
 # ========================================
 web_search_score = 0
 if web_search_data:
  result_count = web_search_data.get("result_count", 0)
  brand_mentions = web_search_data.get("brand_mentions", 0)
  if brand_mentions >= 100:
   web_search_score = 15
  elif brand_mentions >= 50:
   web_search_score = 12
  elif brand_mentions >= 20:
   web_search_score = 9
  elif brand_mentions >= 10:
   web_search_score = 6
  elif brand_mentions >= 5:
   web_search_score = 4
  elif brand_mentions >= 1:
   web_search_score = 2
  elif result_count >= 50:
   web_search_score = 7
  elif result_count >= 20:
   web_search_score = 4
 scores["web_search_score"] = web_search_score
 # ========================================
 # 2: (15)
 # ========================================
 platform_score = 0
 douyin_active = False
 xhs_active = False
 # 8
 if douyin_data:
  video_count = douyin_data.get("video_count", 0) or len(douyin_data.get("videos", []))
  if video_count >= 10:
   platform_score += 8
   douyin_active = True
  elif video_count >= 5:
   platform_score += 6
   douyin_active = True
  elif video_count >= 1:
   platform_score += 3
   douyin_active = True
 # 7
 if xiaohongshu_data:
  note_count = xiaohongshu_data.get("note_count", 0) or len(xiaohongshu_data.get("notes", []))
  if note_count >= 10:
   platform_score += 7
   xhs_active = True
  elif note_count >= 5:
   platform_score += 5
   xhs_active = True
  elif note_count >= 1:
   platform_score += 2
   xhs_active = True
 scores["platform_score"] = platform_score
 # ========================================
 # 3: (15) -
 # ========================================
 content_quality_score = 0
 total_engagement = 0
 content_count = 0
 #
 if douyin_data:
  videos = douyin_data.get("videos", []) or douyin_data.get("top20", [])
  for v in videos:
   stats = v.get("stats", {})
   total_engagement += (stats.get("digg", 0) or v.get("like_count", 0))
   total_engagement += (stats.get("share", 0) or v.get("share_count", 0)) * 2
  content_count += len(videos)
 #
 if xiaohongshu_data:
  notes = xiaohongshu_data.get("notes", []) or xiaohongshu_data.get("top20", [])
  for n in notes:
   total_engagement += n.get("like_count", 0)
   total_engagement += n.get("collect_count", 0) * 2
  content_count += len(notes)
 avg_engagement = total_engagement / max(content_count, 1)
 #
 if content_count >= 20 and avg_engagement >= 500:
  content_quality_score = 15
 elif content_count >= 10 and avg_engagement >= 200:
  content_quality_score = 12
 elif content_count >= 5 and avg_engagement >= 100:
  content_quality_score = 9
 elif content_count >= 3 and avg_engagement >= 50:
  content_quality_score = 6
 elif content_count >= 1:
  content_quality_score = 3
 scores["content_quality_score"] = content_quality_score
 # ========================================
 # 4: (10)
 # ========================================
 authority_score = 0
 if web_search_data:
  authority_sources = web_search_data.get("authority_sources", [])
  brand_mentions = web_search_data.get("brand_mentions", 0)
  if len(authority_sources) >= 5:
   authority_score = 10
  elif len(authority_sources) >= 3:
   authority_score = 7
  elif len(authority_sources) >= 1:
   authority_score = 4
  elif brand_mentions >= 100:
   authority_score = 10
  elif brand_mentions >= 50:
   authority_score = 7
  elif brand_mentions >= 20:
   authority_score = 4
 scores["authority_score"] = authority_score
 # ========================================
 # 5: (10) - AI
 # ========================================
 brand_ownership_score = 0
 if ai_visibility_data:
  engines_detected = ai_visibility_data.get("brand_detected_count", 0)
  total_engines = ai_visibility_data.get("total_engines", 3)
  # AI
  if engines_detected >= total_engines:
   brand_ownership_score = 10 # AI
  elif engines_detected >= 2:
   brand_ownership_score = 7
  elif engines_detected >= 1:
   brand_ownership_score = 4
 scores["brand_ownership_score"] = brand_ownership_score
 # ========================================
 # 6: AI (20) - GEO
 # ========================================
 ai_visibility_score = 0
 if ai_visibility_data:
  engines_detected = ai_visibility_data.get("brand_detected_count", 0)
  total_engines = ai_visibility_data.get("total_engines", 3)
  successful_engines = ai_visibility_data.get("successful_engines", total_engines)
  total_mentions = ai_visibility_data.get("total_mentions", 0)
  is_recommended = ai_visibility_data.get("recommendation_count", 0)
  # (10)
  coverage_rate = engines_detected / max(successful_engines, 1)
  if coverage_rate >= 1.0:
   ai_visibility_score += 10
  elif coverage_rate >= 0.66:
   ai_visibility_score += 7
  elif coverage_rate >= 0.33:
   ai_visibility_score += 4
  elif engines_detected >= 1:
   ai_visibility_score += 2
  # (6)
  if total_mentions >= 50:
   ai_visibility_score += 6
  elif total_mentions >= 20:
   ai_visibility_score += 5
  elif total_mentions >= 10:
   ai_visibility_score += 4
  elif total_mentions >= 5:
   ai_visibility_score += 3
  elif total_mentions >= 1:
   ai_visibility_score += 2
  # (4)
  if is_recommended >= total_engines:
   ai_visibility_score += 4
  elif is_recommended >= 1:
   ai_visibility_score += 2
  #
  results = ai_visibility_data.get("results", [])
  total_negative = sum(r.get("negative_mentions", 0) for r in results)
  total_positive = sum(r.get("positive_mentions", 0) for r in results)
  if total_negative > 0:
   total_sentiment = total_positive + total_negative
   if total_sentiment > 0:
    negative_ratio = total_negative / total_sentiment
    if negative_ratio > 0.5 and total_negative >= 3:
     ai_visibility_score = max(0, ai_visibility_score - 4)
    elif negative_ratio > 0.3 and total_negative >= 2:
     ai_visibility_score = max(0, ai_visibility_score - 2)
 scores["ai_visibility_score"] = ai_visibility_score
 # ========================================
 # 7: AI (10) -
 # ========================================
 ai_citation_score = 0
 # /
 # TODO: /API
 #
 if web_search_data and web_search_data.get("brand_mentions", 0) >= 20:
  ai_citation_score = 5 #
  if content_quality_score >= 9:
   ai_citation_score += 3 #
  if authority_score >= 7:
   ai_citation_score += 2 #
 elif content_count >= 20:
  ai_citation_score = 3 #
 scores["ai_citation_score"] = min(10, ai_citation_score)
 # ========================================
 # 8: (5)
 # ========================================
 update_frequency_score = 0
 #
 if douyin_active and xhs_active:
  update_frequency_score = 5 #
 elif douyin_active or xhs_active:
  update_frequency_score = 3 #
 elif content_count >= 5:
  update_frequency_score = 2 #
 scores["update_frequency_score"] = update_frequency_score
 # ========================================
 #
 # ========================================
 score_keys = list(SCORING_DIMENSIONS.keys())
 total_score = sum(scores.get(k, 0) for k in score_keys)
 level, description = get_coverage_level(total_score)
 result = {
  "brand": brand_name,
  "total_score": total_score,
  "level": level,
  "level_description": description,
  "dimension_scores": scores,
  "dimension_details": {
   dim: {
    "score": scores.get(dim, 0),
    "max_score": config["weight"],
    "description": config["desc"],
    "full_description": config.get("full_desc", "")
   }
   for dim, config in SCORING_DIMENSIONS.items()
  },
  "recommendations": generate_recommendations_v2(scores, total_score)
 }
 return ToolResponse(
  content=[{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
 )
def generate_recommendations(scores: dict, total_score: float) -> list[str]:
 """ (,)"""
 recommendations = []
 if scores.get("platform_score", 0) < 8:
  recommendations.append("")
 if scores.get("content_quality_score", 0) < 8:
  recommendations.append(",")
 if scores.get("web_search_score", 0) < 8:
  recommendations.append(",SEO")
 if scores.get("authority_score", 0) < 5:
  recommendations.append(",")
 if scores.get("ai_visibility_score", 0) < 10:
  recommendations.append("AI , GEO ")
 if total_score < 40:
  recommendations.insert(0, " ,GEO")
 return recommendations
def generate_recommendations_v2(scores: dict, total_score: float) -> dict:
 """
  8 (Coze )
 :
 {
  "high_priority": [...], #
  "medium_priority": [...], # []
  "ongoing": [...],   # []
  "summary": "..."   #
 }
 """
 high = []  #
 medium = [] # []
 ongoing = [] # []
 # ========== ==========
 # AI GEO
 if scores.get("ai_visibility_score", 0) < 10:
  high.append({
   "issue": "AI",
   "impact": " ",
   "action": ",AI"
  })
 #
 if scores.get("brand_ownership_score", 0) < 7:
  high.append({
   "issue": "AI",
   "impact": " ",
   "action": "/,AI"
  })
 # AI
 if scores.get("ai_citation_score", 0) < 5:
  high.append({
   "issue": "AI",
   "impact": " ",
   "action": "FAQAI"
  })
 # ========== ==========
 #
 if scores.get("web_search_score", 0) < 9:
  medium.append({
   "issue": "",
   "action": ","
  })
 #
 if scores.get("platform_score", 0) < 10:
  medium.append({
   "issue": "",
   "action": "/,"
  })
 #
 if scores.get("content_quality_score", 0) < 9:
  medium.append({
   "issue": "",
   "action": ","
  })
 #
 if scores.get("authority_score", 0) < 7:
  medium.append({
   "issue": "",
   "action": "36/"
  })
 # ========== ==========
 if scores.get("update_frequency_score", 0) < 4:
  ongoing.append({
   "issue": "",
   "action": ","
  })
 # ,
 if total_score >= 70:
  ongoing.append({
   "issue": "",
   "action": ",AI"
  })
 #
 if total_score >= 80:
  summary = "GEO,AI"
 elif total_score >= 60:
  summary = ",AI"
 elif total_score >= 40:
  summary = ",GEO"
 else:
  summary = "AI,GEO"
 return {
  "high_priority": high,
  "medium_priority": medium,
  "ongoing": ongoing,
  "summary": summary,
  "total_issues": len(high) + len(medium) + len(ongoing)
 }
async def generate_geo_report(
 brand_name: str,
 industry: str,
 geo_score_data: dict,
 ai_visibility_data: dict = None,
 platform_data: dict = None,
 company_profile: str = "",
 additional_info: str = ""
) -> ToolResponse:
 """
  GEO (Coze )
 """
 from datetime import datetime
 score = geo_score_data.get("total_score", 0)
 level = geo_score_data.get("level", "")
 dimensions = geo_score_data.get("dimension_details", {})
 recommendations = geo_score_data.get("recommendations", {})
 #
 if score >= 80:
  level_emoji = " "
 elif score >= 60:
  level_emoji = "[] "
 elif score >= 40:
  level_emoji = " "
 elif score >= 20:
  level_emoji = " "
 else:
  level_emoji = " "
 report = f"""# {brand_name} GEO
> ****:{datetime.now().strftime('%Y%m%d')}
> **GEO**:{score}/100 | ****:{level_emoji}
> ****:GEO (AgentScope)
---
> ****
>
> **GEO**,AI:
>
> | | | |
> |------|----------|----------|
> | | TOP20 + TOP10 | |
> | | TOP20 + TOP10 | |
> | AI | DeepSeek/Kimi/ | |
>
> ****: â†’ â†’
---
##
**{brand_name}** GEO **{score}/100**,{geo_score_data.get("level_description", "")}
"""
 # recommendations
 if isinstance(recommendations, dict):
  summary = recommendations.get("summary", "")
  if summary:
   report += f"> ****:{summary}\n\n"
 #
 report += """###
| | |
|------|------|
"""
 #
 high_priority = recommendations.get("high_priority", []) if isinstance(recommendations, dict) else []
 if high_priority:
  report += f"| **** | {high_priority[0].get('issue', 'AI')} |\n"
 else:
  report += "| **** | AI |\n"
 medium_priority = recommendations.get("medium_priority", []) if isinstance(recommendations, dict) else []
 if medium_priority:
  report += f"| [] **** | {medium_priority[0].get('issue', '')}, |\n"
 else:
  report += "| [] **** | , |\n"
 report += "| [] **** | /,AI |\n\n"
 #
 if company_profile:
  summary_text = company_profile[:300] + "..." if len(company_profile) > 300 else company_profile
  report += f"### \n{summary_text}\n\n"
 if additional_info:
  report += f"> ****:{additional_info}\n\n"
 # ========== ==========
 report += """---
##
| | |
|:------:|----------|
"""
 #
 if high_priority:
  for item in high_priority[:3]:
   issue = item.get("issue", "")
   action = item.get("action", "")
   report += f"| | **{issue}**:{action[:80]}... |\n"
 else:
  report += "| | **AI**:AI,AI |\n"
 if medium_priority:
  for item in medium_priority[:2]:
   issue = item.get("issue", "")
   report += f"| [] | {issue} |\n"
 report += "\n"
 # ========== GEO 8 ==========
 report += """---
## GEO
| | | | | |
|------|:----:|:----:|:----:|------|
"""
 for dim_name, dim_data in dimensions.items():
  dim_score = dim_data.get("score", 0)
  max_score = dim_data.get("max_score", 10)
  description = dim_data.get("description", dim_name)
  full_desc = dim_data.get("full_description", "")
  #
  ratio = dim_score / max_score if max_score > 0 else 0
  if ratio >= 0.8:
   evaluation = " "
  elif ratio >= 0.6:
   evaluation = "[] "
  elif ratio >= 0.3:
   evaluation = " "
  else:
   evaluation = " "
  report += f"| {description} | {dim_score} | {max_score} | {evaluation} | {full_desc} |\n"
 # ========== ==========
 report += """
---
##
"""
 if isinstance(recommendations, dict):
  #
  high_priority = recommendations.get("high_priority", [])
  if high_priority:
   report += "### \n\n"
   report += "| | | |\n"
   report += "|------|:--------:|----------|\n"
   for item in high_priority:
    report += f"| {item.get('issue', '')} | {item.get('impact', '')} | {item.get('action', '')} |\n"
   report += "\n"
  #
  medium_priority = recommendations.get("medium_priority", [])
  if medium_priority:
   report += "### [] \n\n"
   for item in medium_priority:
    report += f"- **{item.get('issue', '')}**:{item.get('action', '')}\n"
   report += "\n"
  #
  ongoing = recommendations.get("ongoing", [])
  if ongoing:
   report += "### [] \n\n"
   for item in ongoing:
    report += f"- {item.get('action', '')}\n"
   report += "\n"
 else:
  #
  for i, rec in enumerate(recommendations if isinstance(recommendations, list) else [], 1):
   report += f"{i}. {rec}\n"
 # ========== AI ==========
 if ai_visibility_data:
  engines_detected = ai_visibility_data.get('brand_detected_count', 0)
  total_engines = ai_visibility_data.get('total_engines', 3)
  mention_rate = round(engines_detected / max(total_engines, 1) * 100)
  report += f"""---
## AI
| | | |
|------|------|------|
| AI | {engines_detected}/{total_engines} | {" " if engines_detected >= total_engines else " AI"} |
| | {mention_rate}% | {"" if mention_rate >= 50 else ""} |
| AI | {ai_visibility_data.get('recommendation_count', 0)} | {"" if ai_visibility_data.get('recommendation_count', 0) > 0 else ""} |
"""
  #
  results = ai_visibility_data.get('results', [])
  if results:
   report += "### AI\n\n"
   for r in results:
    engine = r.get('engine', 'Unknown')
    if 'error' in r:
     report += f"#### {engine} \n\n"
     report += f"> : {r.get('error', 'Unknown error')}\n\n"
    else:
     detected = r.get('brand_detected', False)
     recommended = r.get('is_recommended', False)
     mentions = r.get('brand_mentioned_count', 0)
     sentiment = r.get('sentiment_label', 'unknown')
     status_icon = "" if detected else ""
     rec_text = "" if recommended else ""
     sentiment_emoji = "" if sentiment == 'positive' else ("" if sentiment == 'neutral' else "")
     report += f"#### {engine} {status_icon}\n\n"
     report += f"- ****: {'' if detected else ''} | ****: {mentions} | ****: {rec_text}\n"
     #
     query = r.get('query', '')
     if query:
      query_short = query[:80] + "..." if len(query) > 80 else query
      report += f"- ****: {query_short}\n"
     #
     response = r.get('response', '')
     if response:
      # 200
      response_short = response[:200].replace('\n', ' ') + "..." if len(response) > 200 else response.replace('\n', ' ')
      report += f"- **AI**: {response_short}\n"
     #
     if detected:
      report += f"- ****: {sentiment_emoji} {sentiment}\n"
     report += "\n"
   # ========== AI Agent==========
   if ai_visibility_data:
    # AIVisibilityAgent
    try:
     import sys
     import os
     sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
     from agents.ai_visibility_agent import AIVisibilityAgent
     visibility_agent = AIVisibilityAgent(brand_name)
     analysis_result = visibility_agent.analyze_visibility(ai_visibility_data)
     #
     report += "\n### AI \n\n"
     report += visibility_agent.format_comparison_table_markdown(analysis_result['comparison_table'])
     report += "\n\n**AI**:\n"
     report += analysis_result['visibility_interpretation']
     report += "\n"
     # :
     if analysis_result['strategic_recommendations']:
      report += "\n### AI\n"
      report += visibility_agent.format_strategy_markdown(analysis_result['strategic_recommendations'])
      report += "\n"
    except Exception as e:
     # :
     print(f" AIVisibilityAgent: {e},")
     detected_count = ai_visibility_data.get("detected_in", 0)
     total_engines = 3
     #
     results = ai_visibility_data.get("results", [])
     total_tests = len(results)
     total_mentions = sum(r.get("brand_mentioned_count", 0) for r in results if not r.get("error"))
     mention_rate = (total_mentions / total_tests * 100) if total_tests > 0 else 0
     report += """
### AI
| | ({brand_name}) | | |
|------|----------------------|------------|----------|
| AI | {detected}/{total} | 3/3 | {coverage_gap} |
| | **{mention_rate:.0f}%** ({total_mentions}/{total_tests}) | ~60% | **{mention_gap:.0f}%** |
| | **0%** | ~40% | **40%** |
**AI**:
- {visibility_status}: AI{search_status}
- ****: {opportunity_text}
""".format(
      brand_name=brand_name,
      detected=detected_count,
      total=total_engines,
      coverage_gap=" " if detected_count == 3 else f" {3-detected_count} ",
      mention_rate=mention_rate,
      total_mentions=total_mentions,
      total_tests=total_tests,
      mention_gap=max(0, 60 - mention_rate),
      visibility_status=" > 20% ()" if mention_rate > 20 else " < 20% ()",
      search_status="" if mention_rate > 20 else "",
      opportunity_text=",AI" if mention_rate > 20 else "AI,****"
     )
 # ========== ==========
 if platform_data:
  # : douyin/xiaohongshu data
  if "data" in platform_data and isinstance(platform_data.get("data"), dict):
   # : platform_data.data.douyin
   data_root = platform_data.get("data", {})
  else:
   # :
   data_root = platform_data
  douyin_data = data_root.get("douyin", {})
  xhs_data = data_root.get("xiaohongshu", {})
  # - : videos, top20, items
  douyin_items = []
  if isinstance(douyin_data, dict):
   douyin_items = douyin_data.get("videos", douyin_data.get("top20", douyin_data.get("items", [])))
  elif isinstance(douyin_data, list):
   douyin_items = douyin_data
  # - : notes, top20, items
  xhs_items = []
  if isinstance(xhs_data, dict):
   xhs_items = xhs_data.get("notes", xhs_data.get("top20", xhs_data.get("items", [])))
  elif isinstance(xhs_data, list):
   xhs_items = xhs_data
  if douyin_items or xhs_items:
   report += """---
##
"""
   #
   if douyin_items:
    # - like_count digg_count
    sorted_dy = sorted(
     douyin_items,
     key=lambda x: x.get("like_count", x.get("digg_count", 0)) if isinstance(x, dict) else 0,
     reverse=True
    )[:5]
    report += """### TOP5
| | | | | | |
|:----:|----------|------|-----:|-----:|----------|
"""
    def analyze_content_type(title):
     """ - """
     if any(kw in title for kw in ["", "", "", ""]):
      return ""
     elif any(kw in title for kw in ["", "", "", "", ""]):
      return ""
     elif any(kw in title for kw in ["", "", "", ""]):
      return ""
     elif any(kw in title for kw in ["", "", ""]):
      return ""
     return ""
    for i, item in enumerate(sorted_dy, 1):
     desc = item.get("desc", item.get("title", ""))
     title = desc[:30] + "..." if len(desc) > 30 else desc
     # -
     author_data = item.get("author", {})
     if isinstance(author_data, dict):
      author = author_data.get("nickname", author_data.get("name", ""))
     else:
      author = item.get("author_nickname", str(author_data)[:10] if author_data else "")
     likes = item.get("like_count", item.get("digg_count", 0))
     shares = item.get("share_count", 0)
     #
     content_type = analyze_content_type(desc)
     report += f"| {i} | {title} | {author[:10]} | {likes:,} | {shares:,} | {content_type} |\n"
    report += "\n"
   #
   if xhs_items:
    #
    sorted_xhs = sorted(
     xhs_items,
     key=lambda x: x.get("liked_count", x.get("like_count", 0)) if isinstance(x, dict) else 0,
     reverse=True
    )[:5]
    report += """### TOP5
| | | | | | |
|:----:|----------|------|-----:|-----:|----------|
"""
    def analyze_xhs_content_type(title):
     """"""
     if any(kw in title for kw in ["", "", "", ""]):
      return ""
     elif any(kw in title for kw in ["", "", "", ""]):
      return ""
     elif any(kw in title for kw in ["", "", ""]):
      return ""
     elif any(kw in title for kw in ["", "", ""]):
      return ""
     return ""
    for i, item in enumerate(sorted_xhs, 1):
     raw_title = item.get("title", item.get("display_title", item.get("desc", "")))
     title = raw_title[:25] + "..." if len(raw_title) > 25 else raw_title
     #
     user_data = item.get("user", {})
     if isinstance(user_data, dict):
      author = user_data.get("nickname", user_data.get("name", ""))
     else:
      author = item.get("nickname", str(user_data)[:10] if user_data else "")
     likes = item.get("liked_count", item.get("like_count", 0))
     collects = item.get("collected_count", item.get("collect_count", 0))
     #
     content_type = analyze_xhs_content_type(raw_title)
     report += f"| {i} | {title} | {author[:10]} | {likes:,} | {collects:,} | {content_type} |\n"
    report += "\n"
   # ========== ==========
   report += """
> ****
"""
   #
   if douyin_items and len(douyin_items) >= 3:
    top_videos = sorted(douyin_items, key=lambda x: x.get("like_count", 0), reverse=True)[:3]
    avg_engagement = sum(v.get("like_count", 0) + v.get("share_count", 0) * 2 for v in top_videos) / len(top_videos) if top_videos else 0
    report += f"""****:
- TOP3 :{avg_engagement:,.0f}
- :
 -
 -
 -
"""
   #
   if xhs_items and len(xhs_items) >= 3:
    zero_like_count = sum(1 for x in xhs_items if x.get("liked_count", 0) == 0)
    zero_like_ratio = zero_like_count / len(xhs_items) * 100 if xhs_items else 0
    if zero_like_ratio > 30:
     report += f"""****:
- {zero_like_ratio:.1f}% 0,""
- :,
- :B2B,****
"""
    else:
     report += """****:
-
- :
"""
 # ========== ==========
 competitor_data = platform_data.get("competitor_analysis", {}) if platform_data else {}
 competitors = competitor_data.get("competitors", [])
 if competitors:
  report += """---
##
"""
  #
  report += """###
> /,
> ****:
> - ****: =
> - ****: = + Ã—2 + Ã—3 | = + Ã—2
| | | | | | |
|:----:|----------|----------|------|:--------:|----------|
"""
  for i, comp in enumerate(competitors[:5], 1):
   name = comp.get("name", comp.get("nickname", ""))
   platform = comp.get("platform", "")
   if platform == "douyin":
    platform_display = ""
    source = ""
   elif platform == "xiaohongshu":
    platform_display = ""
    source = ""
   else:
    platform_display = platform
    source = ""
   #
   engagement = comp.get("total_engagement", 0)
   #
   if isinstance(engagement, (int, float)):
    if engagement >= 10000:
     engagement_str = f"{engagement / 10000:.1f}"
    else:
     engagement_str = f"{int(engagement):,}"
   else:
    engagement_str = str(engagement)
   #
   content_type = ""
   if engagement >= 30000:
    content_type = "KOL"
   elif engagement >= 10000:
    content_type = ""
   report += f"| {i} | {name[:15]} | {source} | {platform_display} | {engagement_str} | {content_type} |\n"
  report += "\n"
  #
  benchmark = competitor_data.get("benchmark", {})
  if benchmark:
   gap_analysis = benchmark.get("gap_analysis", {})
   if gap_analysis and isinstance(gap_analysis, dict):
    status = gap_analysis.get("status", "neutral")
    content_gap = gap_analysis.get("content_gap_percent", 0)
    engagement_gap = gap_analysis.get("engagement_gap_percent", 0)
    report += """###
"""
    #
    if content_gap != 0:
     icon = "" if content_gap > 0 else ""
     direction = "" if content_gap > 0 else ""
     report += f"- {icon} ****: {direction} {abs(content_gap):.1f}%\n"
    #
    if engagement_gap != 0:
     icon = "" if engagement_gap > 0 else ""
     direction = "" if engagement_gap > 0 else ""
     report += f"- {icon} ****: {direction} {abs(engagement_gap):.1f}%\n"
    #
    if status == "leading":
     report += "- ****: \n"
    elif status == "lagging":
     report += "- ****: \n"
    else:
     report += "- ****: \n"
    report += "\n"
  # ========== ==========
  if competitors and len(competitors) > 0:
   #
   benchmark_competitor = competitors[0]
   comp_name = benchmark_competitor.get("name", benchmark_competitor.get("nickname", ""))
   comp_platform = benchmark_competitor.get("platform", "")
   comp_engagement = benchmark_competitor.get("total_engagement", 0)
   report += f"""### :{comp_name}
- ****: {"" if comp_platform == "douyin" else "" if comp_platform == "xiaohongshu" else comp_platform}
- ****: {comp_engagement / 10000:.1f}
####
**1. **
**""**.,."",:
- :
- :""
- :
**""**B2B.
**2. **
**""**:
- /
- "X""XX"
- ****
PPT.
**3. **
   # Competitor analysis section
- : >
- : >
- : >
"",.
---
####
**""**:
**""**:
- 30%,?
- ,?
- "",?
****:
```
 + + =
```
****:
- ""
- B2B
-
   # ========== ==========
   new_emoji = "\U0001F195" #
   check_emoji = "\u2705" #
   star_emoji = "\u2B50" #
   chart_emoji = "\U0001F4CA" #
   landscape_text = f"""### {chart_emoji}
****: "",,AI.
| | | | |
|----------|----------|----------|----------|
| | | | |
| | 2 | 2-3 | |
| | | | |
| {new_emoji} AI | 0% | ~5-10% | **** {star_emoji} |
****:
1. {check_emoji} **AI**: AI,
2. {check_emoji} ****: ,
3. {check_emoji} ****: AI,AI
"""
   report += landscape_text
 # ========== AI ==========
 if ai_visibility_data and isinstance(ai_visibility_data, dict):
  detected_count = ai_visibility_data.get("detected_in", 0)
  total_engines = 3
  total_questions = ai_visibility_data.get("total_questions", 3)
  #
  mention_data = ai_visibility_data.get("mentions", {})
  total_mentions = sum(mention_data.values()) if isinstance(mention_data, dict) else 0
  total_tests = total_questions * total_engines # 3 questions x 3 engines = 9 tests
  mention_rate = (total_mentions / total_tests * 100) if total_tests > 0 else 0
  # AI
  if "ai_visibility_score" in geo_score_data:
   ai_score = geo_score_data.get("ai_visibility_score", 0)
   # AI
   coverage_gap = "" if detected_count == 3 else f" {3-detected_count} "
   mention_gap = max(0, 60-mention_rate)
   status_text = " > 20% ()" if mention_rate > 20 else " < 20% ()"
   search_status = "" if mention_rate > 20 else ""
   opportunity = "" if mention_rate > 20 else "AI,"
   report += f"""
### AI
| | () | () | |
|------|-------------------|----------------------|----------|
| AI | {detected_count}/{total_engines} | 3/3 | {coverage_gap} |
# | | **{mention_rate:.0f}%** ({total_mentions}/{total_tests}) | ~60% | **{mention_gap:.0f}%** |
| | **0%** | ~40% | **40%** |
**AI**:
- {status_text}: AI{search_status}
- ****: {opportunity}
"""
    # ========== Report Footer ==========
    report += f"""---

## Next Steps

| Priority | Action Item |
|:--------:|------------|
| P0 | Implement immediate recommendations |
| P1 | Execute medium-term strategies |
| P2 | Plan long-term optimizations |

---
*Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*  
*Powered by GEO Diagnostic System (AgentScope)*
"""
 return ToolResponse(
  content=[{"type": "text", "text": report}]
 )
 *cascade08 *cascade08A *cascade08AG*cascade08Gq *cascade08q¶ *cascade08¶·*cascade08·Ü *cascade08Üä *cascade08äæ*cascade08æé *cascade08éï *cascade08ïŠ *cascade08Šó *cascade08ó”	 *cascade08”	–	*cascade08–	¤	 *cascade08¤	¥	 *cascade08
¥	¹	 ¹	Â	 *cascade08Â	Ğ	 *cascade08Ğ	Ù	 *cascade08Ù	á	*cascade08á	ø	 *cascade08ø	ú	*cascade08ú	ˆ
 *cascade08ˆ
‰
 *cascade08
‰

 
¦
 *cascade08¦
²
 *cascade08²
´
 *cascade08´
¶
¶
·
 *cascade08·
¼
¼
×
 *cascade08×
Û
 *cascade08Û
ç
 *cascade08ç
ì
 *cascade08ì
ñ
*cascade08ñ
ù
 *cascade08ù
‚ *cascade08‚ *cascade08‘ *cascade08‘“*cascade08“” *cascade08”—*cascade08—˜ *cascade08˜›*cascade08›œ *cascade08œ*cascade08Ÿ *cascade08Ÿ *cascade08 È *cascade08
Èß ßå *cascade08åô *cascade08ô­ *cascade08­® *cascade08®° *cascade08° *cascade08Ü *cascade08Üİ*cascade08İ„ *cascade08„…*cascade08…û *cascade08û… *cascade08…Æ *cascade08Æî*cascade08îñ *cascade08ñò*cascade08òó *cascade08ó´ *cascade08´¶ *cascade08¶· *cascade08·» *cascade08»¾ *cascade08¾è*cascade08èë *cascade08ëû*cascade08ûü *cascade08üş*cascade08ş… *cascade08…*cascade08™ *cascade08™š*cascade08š› *cascade08›Ÿ*cascade08Ÿ¨ *cascade08¨²*cascade08²½ *cascade08½¾*cascade08¾¿ *cascade08¿Ã*cascade08ÃÒ *cascade08ÒÖ*cascade08Ö× *cascade08×Ù*cascade08ÙÚ *cascade08ÚÛ*cascade08ÛÜ *cascade08Üİ*cascade08İŞ *cascade08Şß*cascade08ßá *cascade08áâ*cascade08âã *cascade08ãç*cascade08çê *cascade08êó*cascade08óõ *cascade08õú*cascade08úû *cascade08ûş*cascade08şÿ *cascade08ÿ‚*cascade08‚ƒ *cascade08ƒ„*cascade08„… *cascade08…ˆ*cascade08ˆ‰ *cascade08‰ *cascade08’ *cascade08’–*cascade08–˜ *cascade08˜š*cascade08šœ *cascade08œ *cascade08 ¤ *cascade08¤¥*cascade08¥¦*cascade08¦­ *cascade08­³*cascade08³µ *cascade08µ·*cascade08·À *cascade08ÀÂ*cascade08ÂË *cascade08ËÏ*cascade08ÏÑ *cascade08ÑÓ*cascade08ÓÕ *cascade08ÕÙ*cascade08Ùİ *cascade08İŞ*cascade08Şß*cascade08ßå *cascade08åë*cascade08ëí *cascade08íï*cascade08ïù *cascade08ùú*cascade08úƒ *cascade08ƒ‡*cascade08‡‰ *cascade08‰‹*cascade08‹ *cascade08‘*cascade08‘• *cascade08•—*cascade08— *cascade08£*cascade08£¥ *cascade08¥§*cascade08§° *cascade08°±*cascade08±º *cascade08º¾*cascade08¾À *cascade08ÀÂ*cascade08ÂÄ *cascade08ÄÈ*cascade08ÈÌ *cascade08ÌÍ*cascade08ÍÎ*cascade08ÎÔ *cascade08ÔÚ*cascade08ÚÜ *cascade08ÜŞ*cascade08Şç *cascade08çè*cascade08èì *cascade08ìî*cascade08îñ *cascade08ñó*cascade08óö *cascade08öú*cascade08úû *cascade08û„*cascade08„Š *cascade08Š‹ *cascade08‹Œ *cascade08Œ*cascade08 *cascade08–*cascade08–˜ *cascade08˜š*cascade08š *cascade08¤ *cascade08¤¥ *cascade08¥©*cascade08©¬ *cascade08¬®*cascade08®± *cascade08±²*cascade08²´ *cascade08´µ*cascade08µ¶ *cascade08¶»*cascade08»½ *cascade08½À *cascade08ÀÄ*cascade08ÄÆ *cascade08ÆÇ*cascade08ÇÈ *cascade08ÈÉ*cascade08ÉÊ *cascade08ÊË*cascade08ËÌ *cascade08ÌÍ*cascade08ÍÏ *cascade08ÏĞ*cascade08ĞÑ *cascade08ÑÒ*cascade08ÒÓ *cascade08ÓÔ*cascade08ÔÖ *cascade08ÖØ *cascade08ØÚ*cascade08Úİ *cascade08İâ*cascade08âí *cascade08íî*cascade08îõ *cascade08õû*cascade08ûı *cascade08ıÿ*cascade08ÿˆ *cascade08ˆ‰*cascade08‰’ *cascade08’“*cascade08“” *cascade08”˜*cascade08˜¢ *cascade08¢¤*cascade08¤ª *cascade08ª°*cascade08°² *cascade08²´*cascade08´½ *cascade08½¾*cascade08¾Á *cascade08ÁÃ*cascade08ÃÄ *cascade08ÄÅ*cascade08ÅÆ *cascade08ÆÌ*cascade08ÌÍ *cascade08ÍÕ*cascade08Õ× *cascade08×Û*cascade08ÛŞ *cascade08Şî*cascade08îñ *cascade08ñÖ *cascade08Öì *cascade08ìù*cascade08ùú *cascade08úû*cascade08ûü *cascade08üƒ*cascade08ƒ *cascade08 *cascade08“*cascade08“” *cascade08”Ÿ *cascade08Ÿ¢ *cascade08¢§*cascade08§¨ *cascade08¨¶ *cascade08¶· *cascade08·É*cascade08ÉÊ *cascade08ÊÍ*cascade08ÍÎ *cascade08Î“ *cascade08“ *cascade08*cascade08Ÿ*cascade08Ÿ· *cascade08·¸*cascade08¸º *cascade08º½ *cascade08½¾ *cascade08¾¿ *cascade08¿Ä*cascade08ÄÆ *cascade08ÆÉ*cascade08ÉÍ *cascade08ÍÎ*cascade08ÎÏ *cascade08ÏĞ*cascade08ĞÕ *cascade08ÕÙ*cascade08ÙÚ *cascade08Úå*cascade08åæ *cascade08æë*cascade08ëí *cascade08íî *cascade08îğ *cascade08ğõ*cascade08õö *cascade08öø*cascade08øÿ *cascade08ÿ€*cascade08€‚ *cascade08‚ƒ*cascade08ƒ… *cascade08…‰ *cascade08‰Š *cascade08Š*cascade08 *cascade08*cascade08’ *cascade08’”*cascade08”˜ *cascade08˜œ*cascade08œ  *cascade08 ¢*cascade08¢¥ *cascade08¥§*cascade08§¨ *cascade08¨¬*cascade08¬° *cascade08°µ*cascade08µ» *cascade08»¾ *cascade08¾¿ *cascade08¿À*cascade08ÀÁ *cascade08ÁÃ*cascade08ÃÄ *cascade08ÄÅ*cascade08ÅÇ *cascade08ÇÈ*cascade08ÈÍ *cascade08ÍÓ *cascade08ÓÛ *cascade08ÛÜ*cascade08ÜŞ *cascade08Şß*cascade08ßà *cascade08àå*cascade08åæ *cascade08æê *cascade08êí *cascade08íˆ  *cascade08ˆ ‰ *cascade08‰ ‹  *cascade08‹ Œ *cascade08Œ   *cascade08  *cascade08 »  *cascade08» ½ *cascade08½ ¾  *cascade08¾ ß *cascade08ß à  *cascade08à ä *cascade08ä è  *cascade08è ë  *cascade08ë ì *cascade08ì î  *cascade08î ï *cascade08ï ò  *cascade08ò ó *cascade08ó ù  *cascade08ù ú *cascade08ú ÿ  *cascade08ÿ „! *cascade08„!…! *cascade08…!‰!*cascade08‰!! *cascade08!‘!*cascade08‘!“! *cascade08“!–! *cascade08–!£! *cascade08£!¤! *cascade08¤!ª!*cascade08ª!¬! *cascade08¬!®! *cascade08®!³! *cascade08³!´!*cascade08´!¶! *cascade08¶!·!*cascade08·!º! *cascade08º!»!*cascade08»!È! *cascade08È!É!*cascade08É!Ê! *cascade08Ê!Ë!*cascade08Ë!Ì! *cascade08Ì!Í!*cascade08Í!Î! *cascade08Î!Ğ!*cascade08Ğ!Ñ! *cascade08Ñ!Ò!*cascade08Ò!Ô! *cascade08Ô!Õ!*cascade08Õ!×! *cascade08×!Ø!*cascade08Ø!Ú! *cascade08Ú!Û!*cascade08Û!à! *cascade08à!â! *cascade08â!ã! *cascade08ã!å!*cascade08å!æ! *cascade08æ!é!*cascade08é!í! *cascade08í!ñ!*cascade08ñ!ó! *cascade08ó!õ! *cascade08õ!ú! *cascade08ú!û!*cascade08û!ı! *cascade08ı!ş!*cascade08ş!" *cascade08"‚"*cascade08‚"‰" *cascade08‰"Œ" *cascade08Œ"" *cascade08"" *cascade08"”" *cascade08”"•" *cascade08•"—"*cascade08—"" *cascade08"Ÿ"*cascade08Ÿ"¡" *cascade08¡"¢"*cascade08¢"¥" *cascade08¥"§" *cascade08§"©"*cascade08©"ª" *cascade08ª"¬"*cascade08¬"­" *cascade08­"°"*cascade08°"´" *cascade08´"¸"*cascade08¸"Ã" *cascade08Ã"È"*cascade08È"É" *cascade08É"Ë"*cascade08Ë"Ö" *cascade08Ö"Û"*cascade08Û"Ü" *cascade08Ü"Ş"*cascade08Ş"é" *cascade08é"“#*cascade08“#–# *cascade08–#š# *cascade08š#›# *cascade08›#œ#*cascade08œ#Ì# *cascade08Ì#Ï# *cascade08Ï#Ó#*cascade08Ó#Ô# *cascade08Ô#Ù#*cascade08Ù#Ú# *cascade08Ú#Ş#*cascade08Ş#ë# *cascade08ë#û#*cascade08û#ü# *cascade08ü#†$ *cascade08†$‡$ *cascade08‡$‰$*cascade08‰$‹$ *cascade08‹$£$ *cascade08£$­$ *cascade08­$°$*cascade08°$²$ *cascade08²$³$*cascade08³$¶$ *cascade08¶$¼$*cascade08¼$Ç$ *cascade08Ç$Ê$*cascade08Ê$Ë$ *cascade08Ë$Ì$*cascade08Ì$Í$ *cascade08Í$Ø$*cascade08Ø$Ú$ *cascade08Ú$Ü$*cascade08Ü$İ$ *cascade08İ$à$*cascade08à$á$ *cascade08á$í$*cascade08í$ğ$ *cascade08ğ$ò$*cascade08ò$÷$ *cascade08÷$ù$*cascade08ù$ú$ *cascade08ú$ş$*cascade08ş$ÿ$ *cascade08ÿ$‚%*cascade08‚%„% *cascade08„%% *cascade08%% *cascade08%%*cascade08%”% *cascade08”%—%*cascade08—%˜% *cascade08˜%›%*cascade08›%œ% *cascade08œ%%*cascade08%% *cascade08%°% *cascade08°%±% *cascade08±%´%*cascade08´%µ% *cascade08µ%Á%*cascade08Á%Ã% *cascade08Ã%Å%*cascade08Å%Ë% *cascade08Ë%Ö%*cascade08Ö%×% *cascade08×%Û%*cascade08Û%İ% *cascade08İ%à%*cascade08à%á% *cascade08á%å%*cascade08å%æ% *cascade08æ%ç%*cascade08ç%è% *cascade08è%é%*cascade08é%í% *cascade08í%î%*cascade08î%ô% *cascade08ô%„&*cascade08„&…& *cascade08…&‡&*cascade08‡&ˆ& *cascade08ˆ&›&*cascade08›&œ& *cascade08œ&&*cascade08&Ÿ& *cascade08Ÿ&¡&*cascade08¡&¢& *cascade08¢&¶&*cascade08¶&·& *cascade08·&º&*cascade08º&»& *cascade08»&¼&*cascade08¼&½& *cascade08½&¾&*cascade08¾&Â& *cascade08Â&Ä&*cascade08Ä&Å& *cascade08Å&Æ&*cascade08Æ&Ç& *cascade08Ç&É& *cascade08É&Ë&*cascade08Ë&Ì& *cascade08Ì&Í&*cascade08Í&Î& *cascade08Î&Ï&*cascade08Ï&Ğ& *cascade08Ğ&Ñ&*cascade08Ñ&Ó& *cascade08Ó&Ş&*cascade08Ş&á& *cascade08á&â&*cascade08â&ä& *cascade08ä&ê&*cascade08ê&ë& *cascade08ë&ì&*cascade08ì&î& *cascade08î&ù&*cascade08ù&ı& *cascade08ı&€'*cascade08€'' *cascade08'„'*cascade08„'…' *cascade08…'‡'*cascade08‡'ˆ' *cascade08ˆ'‹'*cascade08‹'Œ' *cascade08Œ''*cascade08'‘' *cascade08‘'—'*cascade08—'˜' *cascade08˜'›'*cascade08›'œ' *cascade08œ''*cascade08'' *cascade08'«'*cascade08«'¬' *cascade08¬'¯'*cascade08¯'±' *cascade08±'²'*cascade08²'³' *cascade08³'Ã'*cascade08Ã'Å' *cascade08Å'Ë'*cascade08Ë'Ï' *cascade08Ï'Ò'*cascade08Ò'Ó' *cascade08Ó'Ô'*cascade08Ô'Õ' *cascade08Õ'×'*cascade08×'Ø' *cascade08Ø'Û'*cascade08Û'Ü' *cascade08Ü'İ'*cascade08İ'Ş'*cascade08Ş'ã' *cascade08ã'æ' *cascade08æ'ç' *cascade08ç'è'*cascade08è'é' *cascade08é'ï'*cascade08ï'ñ' *cascade08ñ'ó' *cascade08ó'ÿ'*cascade08ÿ'€( *cascade08€(„(*cascade08„(…( *cascade08…(†(*cascade08†(‡( *cascade08‡(Š(*cascade08Š(Œ( *cascade08Œ((*cascade08(’( *cascade08’(˜(*cascade08˜(™( *cascade08™(›(*cascade08›(œ( *cascade08œ(²(*cascade08²(³( *cascade08³(´(*cascade08´(·( *cascade08·(¼(*cascade08¼(½( *cascade08½(Á(*cascade08Á(Â( *cascade08Â(Ã(*cascade08Ã(Ç( *cascade08Ç(É(*cascade08É(Ê( *cascade08Ê(Ë(*cascade08Ë(Î( *cascade08Î(Ğ(*cascade08Ğ(Ñ( *cascade08Ñ(Ò(*cascade08Ò(Ó( *cascade08Ó(Ô(*cascade08Ô(Õ( *cascade08Õ(Ö(*cascade08Ö(Ø( *cascade08Ø(â(*cascade08â(å( *cascade08å(ó(*cascade08ó(ô( *cascade08ô(õ(*cascade08õ(ö( *cascade08ö(†)*cascade08†)‡) *cascade08‡)ˆ)*cascade08ˆ)‰) *cascade08‰)Š)*cascade08Š)‹) *cascade08‹))*cascade08)) *cascade08)”)*cascade08”)—) *cascade08—)›)*cascade08›)œ) *cascade08œ)¡) *cascade08¡)¢)*cascade08¢)¤) *cascade08¤)¥) *cascade08¥)§) *cascade08§)¨) *cascade08¨)¼)*cascade08¼)¿) *cascade08¿)Ã)*cascade08Ã)Ä) *cascade08Ä)Ê)*cascade08Ê)Ò) *cascade08Ò)Õ)*cascade08Õ)Ú) *cascade08Ú)Ş)*cascade08Ş)ß) *cascade08ß)ä)*cascade08ä)å) *cascade08å)é)*cascade08é)ò) *cascade08ò)ô)*cascade08ô)ü) *cascade08ü)€**cascade08€** *cascade08*‚**cascade08‚** *cascade08***cascade08** *cascade08*¨**cascade08¨*®* *cascade08®*²**cascade08²*³* *cascade08³*¸**cascade08¸*¹* *cascade08¹*½**cascade08½*¾* *cascade08¾*Â**cascade08Â*Ã* *cascade08Ã*Ç* *cascade08Ç*È**cascade08È*Í* *cascade08Í*Î* *cascade08Î*Ğ* *cascade08Ğ*Ô**cascade08Ô*Õ* *cascade08Õ*Ö**cascade08Ö*Ù* *cascade08Ù*Ú* *cascade08Ú*Û**cascade08Û*Ü* *cascade08Ü*İ**cascade08İ*ã* *cascade08ã*æ**cascade08æ*ç* *cascade08ç*õ**cascade08õ*ö* *cascade08ö*ø**cascade08ø*ù* *cascade08ù*ÿ**cascade08ÿ*+ *cascade08+…+*cascade08…+†+ *cascade08†+‹+*cascade08‹+Œ+ *cascade08Œ++*cascade08+™+ *cascade08™+š+*cascade08š+¢+ *cascade08¢+¦+*cascade08¦+§+ *cascade08§+¨+*cascade08¨+³+ *cascade08³+Ì+*cascade08Ì+Ò+ *cascade08Ò+Ö+*cascade08Ö+×+ *cascade08×+Ü+*cascade08Ü+İ+ *cascade08İ+á+*cascade08á+ê+ *cascade08ê+ë+*cascade08ë+ó+ *cascade08ó+÷+*cascade08÷+ø+ *cascade08ø+ù+*cascade08ù+Š, *cascade08Š,,*cascade08,, *cascade08,”,*cascade08”,•, *cascade08•,™,*cascade08™,®, *cascade08®,²,*cascade08²,³, *cascade08³,¸,*cascade08¸,¹, *cascade08¹,½,*cascade08½,È, *cascade08È,Ì,*cascade08Ì,Í, *cascade08Í,Ò,*cascade08Ò,Ó, *cascade08Ó,×,*cascade08×,â, *cascade08â,- *cascade08-- *cascade08-‘-*cascade08‘-’- *cascade08’-“- *cascade08“-•- *cascade08•-–-*cascade08–-˜- *cascade08˜-›- *cascade08›-Ã-*cascade08Ã-Å- *cascade08Å-µ. *cascade08µ.ô. *cascade08ô.¦0 *cascade08¦0“1 *cascade08“1”1*cascade08”1É1 *cascade08É1ª2 *cascade08ª2«2 *cascade08«2¶2 *cascade08¶2¹2 *cascade08¹2º2*cascade08º2»2 *cascade08»2å2*cascade08å2æ2 *cascade08æ2ï2 *cascade08ï2ğ2 *cascade08ğ2‚3 *cascade08‚3…3 *cascade08…3Ç3 *cascade08Ç3Í3 *cascade08Í3Ó3*cascade08Ó3Ô3 *cascade08Ô3Ù3*cascade08Ù3Û3 *cascade08Û3í3 *cascade08í3î3 *cascade08î34*cascade0844 *cascade084“4*cascade08“4”4 *cascade08”4˜4*cascade08˜4™4 *cascade08™4½4 *cascade08½4Á4 *cascade08Á4ì4 *cascade08ì4î4 *cascade08î4‘5 *cascade08‘5—5 *cascade08—5™5*cascade08™5š5 *cascade08š5£5*cascade08£5¤5 *cascade08¤5©5*cascade08©5«5 *cascade08«5¶5 *cascade08¶5·5 *cascade08·5Ã5*cascade08Ã5Ä5 *cascade08Ä5ã5 *cascade08ã5ì5 *cascade08ì5í5*cascade08í5ø5 *cascade08ø5ú5*cascade08ú5û5 *cascade08û5ş5*cascade08ş5ÿ5 *cascade08ÿ5‚6*cascade08‚6ƒ6 *cascade08ƒ6…6*cascade08…6†6 *cascade08†6‡6*cascade08‡6’6 *cascade08’6”6*cascade08”6•6 *cascade08•6˜6*cascade08˜6™6 *cascade08™6œ6*cascade08œ66 *cascade086Ÿ6*cascade08Ÿ6 6 *cascade08 6¡6*cascade08¡6¬6 *cascade08¬6Û6 *cascade08Û6Ş6 *cascade08Ş6à6*cascade08à6â6 *cascade08â6ä6*cascade08ä6é6 *cascade08é6ì6 *cascade08ì6™7 *cascade08™7›7 *cascade08›7¦7*cascade08¦7Í8 *cascade08Í8Î8*cascade08Î8Ó8 *cascade08Ó8Õ8*cascade08Õ8Ö8 *cascade08Ö8Ş8*cascade08Ş8ß8 *cascade08ß8à8*cascade08à8á8 *cascade08á8ã8*cascade08ã8ä8 *cascade08ä8å8*cascade08å8€9 *cascade08€9‚9*cascade08‚9ƒ9 *cascade08ƒ9‹9*cascade08‹9Œ9 *cascade08Œ9˜9*cascade08˜9™9 *cascade08™99*cascade0899 *cascade089 9*cascade08 9¡9 *cascade08¡9¬9 *cascade08¬9­9 *cascade08­9Ç9*cascade08Ç9É9 *cascade08É9Ì9*cascade08Ì9Í9 *cascade08Í9Ş9*cascade08Ş9®: *cascade08®:¯: *cascade08¯:°:*cascade08°:²: *cascade08²:³:*cascade08³:·: *cascade08·:¹: *cascade08¹:»:*cascade08»:¼: *cascade08¼:Ã:*cascade08Ã:Ç: *cascade08Ç:È:*cascade08È:É: *cascade08É:Ê:*cascade08Ê:Ì: *cascade08Ì:Ñ:*cascade08Ñ:Ò: *cascade08Ò:İ:*cascade08İ:Ş: *cascade08Ş:û: *cascade08û:‰; *cascade08‰;‹;*cascade08‹;; *cascade08;;*cascade08;; *cascade08;˜; *cascade08˜;£;*cascade08£;­; *cascade08­;®; *cascade08®;°;*cascade08°;±; *cascade08±;²; *cascade08²;Í; *cascade08Í;Ò; *cascade08Ò;Õ; *cascade08Õ;à;*cascade08à;ê; *cascade08ê;î; *cascade08î;ƒ<*cascade08ƒ<„< *cascade08„<‰<*cascade08‰<‹< *cascade08‹<< *cascade08<‘< *cascade08‘<œ<*cascade08œ<¬< *cascade08¬<Ã< *cascade08Ã<Í< *cascade08Í<Ø<*cascade08Ø<è< *cascade08è<é< *cascade08é<ê< *cascade08ê<ë<*cascade08ë<ò< *cascade08ò<ø< *cascade08ø<ù<*cascade08ù<û< *cascade08û<ü<*cascade08ü<ı< *cascade08ı<ş<*cascade08ş<€= *cascade08€=†=*cascade08†== *cascade08=š=*cascade08š=­= *cascade08­=´=*cascade08´=¶= *cascade08¶=·=*cascade08·=¸= *cascade08¸=¹=*cascade08¹=»= *cascade08»=É= *cascade08É=Ê= *cascade08Ê=Õ=*cascade08Õ=Ö=*cascade08Ö=×= *cascade08×=Ù=*cascade08Ù=Û= *cascade08Û=Ü=*cascade08Ü=Ş= *cascade08Ş=è= *cascade08è=î= *cascade08î=ï=*cascade08ï=ñ= *cascade08ñ=ò=*cascade08ò=ó= *cascade08ó=ô=*cascade08ô=÷= *cascade08÷=ù=*cascade08ù=ú= *cascade08ú=û=*cascade08û=…> *cascade08…>>*cascade08>£> *cascade08£>ª>*cascade08ª>¬> *cascade08¬>­>*cascade08­>®> *cascade08®>¯>*cascade08¯>±> *cascade08±>¾> *cascade08¾>¿> *cascade08¿>Ê>*cascade08Ê>Î>*cascade08Î>Ï> *cascade08Ï>Ø> *cascade08Ø>Ù> *cascade08Ù>İ>*cascade08İ>Ş> *cascade08Ş>ä>*cascade08ä>å> *cascade08å>ë>*cascade08ë>ù> *cascade08ù>„?*cascade08„?”? *cascade08”?•? *cascade08•?–?*cascade08–?—?*cascade08—?Æ? *cascade08Æ?Ñ?*cascade08Ñ?Ú? *cascade08Ú?Û?*cascade08Û?€@ *cascade08€@‹@*cascade08‹@”@ *cascade08”@•@*cascade08•@™@ *cascade08™@ä@ *cascade08ä@æ@*cascade08æ@„A *cascade08„A‡A*cascade08‡AˆA *cascade08ˆA‰A*cascade08‰AŠA *cascade08ŠAŒA*cascade08ŒAA *cascade08A—A*cascade08—A™A *cascade08™AŸA*cascade08ŸA¬A *cascade08¬A¯A*cascade08¯AÌA *cascade08ÌAÏA*cascade08ÏAĞA *cascade08ĞAÑA*cascade08ÑAÒA *cascade08ÒAÔA*cascade08ÔAÕA *cascade08ÕAßA*cascade08ßAáA *cascade08áAãA*cascade08ãAêA *cascade08êAëA*cascade08ëAìA *cascade08ìAîA*cascade08îAöA *cascade08öA÷A*cascade08÷A½C *cascade08½CÂC*cascade08ÂCÄC *cascade08ÄCÈC*cascade08ÈCÉC *cascade08ÉCÊC*cascade08ÊCËC *cascade08ËCÌC*cascade08ÌCÎC *cascade08ÎCĞC*cascade08ĞCÓC *cascade08ÓCÙC*cascade08ÙCÚC *cascade08ÚCíC*cascade08íCîC *cascade08îCïC*cascade08ïCğC *cascade08ğCòC*cascade08òC±D *cascade08±D¶D*cascade08¶D¸D *cascade08¸DÃD*cascade08ÃDÄD *cascade08ÄDÙD*cascade08ÙDÚD *cascade08ÚDÛD*cascade08ÛDÜD *cascade08ÜDàD*cascade08àDáD *cascade08áDêD *cascade08êDëD *cascade08ëDüD*cascade08üDıD *cascade08ıDŠE*cascade08ŠE‹E *cascade08‹E™E*cascade08™EšE *cascade08šEŸE *cascade08ŸE¡E *cascade08¡EÉE*cascade08ÉEÌE *cascade08ÌEÍE*cascade08ÍEÎE *cascade08ÎEĞE *cascade08ĞEÑE *cascade08ÑEÓE *cascade08ÓEÔE *cascade08ÔEØE *cascade08ØEÙE *cascade08ÙEÚE*cascade08ÚEÜE *cascade08ÜEİE *cascade08İE‰F*cascade08‰F‹F *cascade08‹FŒF*cascade08ŒFF *cascade08FF*cascade08F“F *cascade08“FšF*cascade08šFF *cascade08FŸF*cascade08ŸF¢F *cascade08¢F£F*cascade08£F¤F *cascade08¤F§F *cascade08§F¨F *cascade08¨F©F*cascade08©FªF *cascade08ªF¯F*cascade08¯F°F *cascade08°F¶F *cascade08¶F·F *cascade08·F¸F*cascade08¸FºF *cascade08ºF»F *cascade08»FÂF*cascade08ÂFÄF *cascade08ÄFÏF*cascade08ÏFĞF *cascade08ĞFßF*cascade08ßFàF *cascade08àFíF*cascade08íFñF *cascade08ñF‡G *cascade08‡G‰G *cascade08‰GŒG*cascade08ŒGG *cascade08G–G*cascade08–G™G *cascade08™GšG*cascade08šGœG *cascade08œG G *cascade08 G¾G*cascade08¾GÀG *cascade08ÀGÁG*cascade08ÁGÃG *cascade08ÃGÆG *cascade08ÆGÏG*cascade08ÏGÕG *cascade08ÕGÖG*cascade08ÖGØG *cascade08ØGáG *cascade08áGãG *cascade08ãGèG*cascade08èGéG *cascade08éGëG*cascade08ëGòG *cascade08òGüG *cascade08üGıG*cascade08ıG€H *cascade08€HƒH*cascade08ƒH‡H *cascade08‡H’H*cascade08’H”H *cascade08”H—H *cascade08—H›H*cascade08›HœH *cascade08œH©H*cascade08©HªH *cascade08ªH¬H*cascade08¬H­H *cascade08­H°H*cascade08°H´H *cascade08´HÊH *cascade08ÊHËH *cascade08ËHÎH *cascade08ÎHÖH *cascade08ÖH×H*cascade08×HÚH *cascade08ÚHİH*cascade08İHáH *cascade08áHçH*cascade08çHìH *cascade08ìHîH*cascade08îHïH *cascade08ïHõH*cascade08õHøH *cascade08øHûH*cascade08ûHÿH *cascade08ÿH†I*cascade08†I‰I *cascade08‰IóI *cascade08óIôI *cascade08ôI÷I*cascade08÷IøI *cascade08øIúI*cascade08úIüI *cascade08üIşI*cascade08şIÿI *cascade08ÿIƒJ*cascade08ƒJ‡J *cascade08‡JˆJ*cascade08ˆJ‹J *cascade08‹JŒJ*cascade08ŒJJ *cascade08JJ *cascade08J‘J*cascade08‘J’J *cascade08’J“J*cascade08“J”J *cascade08”J–J*cascade08–J—J *cascade08—J›J*cascade08›JŸJ *cascade08ŸJ¡J*cascade08¡J¢J *cascade08¢J£J*cascade08£J¤J *cascade08¤J¨J*cascade08¨J©J *cascade08©JªJ*cascade08ªJ®J *cascade08®J¯J*cascade08¯J³J *cascade08³JÑJ *cascade08ÑJÓJ *cascade08ÓJŞJ*cascade08ŞJßJ *cascade08ßJàJ*cascade08àJäJ *cascade08äJåJ*cascade08åJæJ *cascade08æJçJ*cascade08çJèJ *cascade08èJëJ*cascade08ëJíJ *cascade08íJîJ*cascade08îJòJ *cascade08òJóJ*cascade08óJ÷J *cascade08÷JùJ*cascade08ùJúJ *cascade08úJüJ*cascade08üJıJ *cascade08ıJ‚K*cascade08‚KƒK *cascade08ƒK…K*cascade08…K†K *cascade08†KŠK*cascade08ŠK‹K *cascade08‹KK*cascade08KK *cascade08K–K *cascade08–K—K *cascade08—KK*cascade08K¥K *cascade08¥K¦K*cascade08¦K§K *cascade08§K®K*cascade08®K²K *cascade08²KÎK *cascade08ÎKĞK *cascade08ĞKÙK *cascade08ÙKÜK*cascade08ÜKİK *cascade08İKßK*cascade08ßKàK *cascade08àKèK*cascade08èKôK *cascade08ôK÷K*cascade08÷KøK *cascade08øK„L*cascade08„LL *cascade08L»L *cascade08»L½L*cascade08½L¿L *cascade08¿LÀL *cascade08ÀLèL*cascade08èLùL *cascade08ùLúL*cascade08úLüL *cascade08üL„M*cascade08„M…M *cascade08…M‘M*cascade08‘M”M *cascade08”M—M*cascade08—M³M *cascade08³M´M*cascade08´MµM *cascade08µM¶M*cascade08¶M·M *cascade08·M»M*cascade08»M¼M *cascade08¼MĞM*cascade08ĞM±P *cascade08±PçP *cascade08çPÔQ *cascade08ÔQ×Q*cascade08×Q­S *cascade08­S¯S *cascade08¯S°S*cascade08°S±S *cascade08±SóS *cascade08óSôS*cascade08ôS­T *cascade08­TµT*cascade08µTÂT *cascade08ÂTÃT*cascade08ÃTàT *cascade08àTáT*cascade08áTõT *cascade08õTùT*cascade08ùTU *cascade08UU*cascade08U«U *cascade08«U¬U*cascade08¬UøU *cascade08øUùU*cascade08ùUÊV *cascade08ÊVËV*cascade08ËV‰W *cascade08‰WŠW*cascade08ŠW¨W *cascade08¨WÌX *cascade08ÌXÎXÎXèX *cascade08èXêXêX¬Y *cascade08¬Y®Y®YÀY *cascade08ÀYÂYÂYŞZ *cascade08ŞZßZ*cascade08ßZå[ *cascade08å[æ[*cascade08æ[ô] *cascade08ô]õ]*cascade08õ]à^ *cascade08à^á^*cascade08á^Ñ_ *cascade08Ñ_Ò_*cascade08Ò_Ça *cascade08ÇaÈa*cascade08ÈaÔa *cascade08ÔaÕa*cascade08Õa¢b *cascade08¢b£b*cascade08£bÚb *cascade08ÚbÛb*cascade08Ûb‡c *cascade08‡cˆc*cascade08ˆc´c *cascade08´cµc*cascade08µcÒc *cascade08ÒcÓc*cascade08Óc†e *cascade08†e¢f *cascade08¢fÜf *cascade08Üfşf *cascade08şf‚g*cascade08‚g„g *cascade08„gˆg *cascade08ˆg‹g*cascade08‹gg *cascade08gg*cascade08gg *cascade08g’g*cascade08’g–g *cascade08–g—g*cascade08—gšg *cascade08šg›g*cascade08›gg *cascade08gŸg *cascade08Ÿg g*cascade08 g¦g *cascade08¦g¨g *cascade08¨gªg*cascade08ªgúh *cascade08úhüh*cascade08üh€i *cascade08€iÏi *cascade08ÏiÑiÑiÅj *cascade08ÅjÔj *cascade08Ôjæj *cascade08æjçj*cascade08çjêj *cascade08êjìj *cascade08ìjíj*cascade08íjğj *cascade08ğjôj*cascade08ôjöj *cascade08öjk *cascade08kk *cascade08k’k *cascade08’k“k*cascade08“k–k *cascade08–k™k *cascade08™k›k *cascade08›kœk*cascade08œkk *cascade08k k*cascade08 k¡k *cascade08¡k®k *cascade08®k¯k*cascade08¯k»k *cascade08»k¼k *cascade08¼k¾k *cascade08¾k¿k*cascade08¿kÂk *cascade08ÂkÄk *cascade08ÄkÅk*cascade08ÅkÜk *cascade08Ükğk *cascade08ğkñk*cascade08ñkók *cascade08ókôk*cascade08ôkşl *cascade08şlÿl*cascade08ÿlm *cascade08m²m *cascade08²m³m *cascade08³m¶m *cascade08¶m·m*cascade08·mæm *cascade08æmém *cascade08émım *cascade08ımƒn *cascade08ƒn‰n*cascade08‰nŠn *cascade08Šn–n*cascade08–n—n *cascade08—nŸn*cascade08Ÿn¡n *cascade08¡n½n *cascade08½nÁn *cascade08ÁnÌn*cascade08ÌnÍn *cascade08ÍnÙn *cascade08ÙnÛn *cascade08Ûnãn*cascade08ãnæn *cascade08ænùn *cascade08ùnûn *cascade08ûnün*cascade08ünın *cascade08ıno*cascade08o‚o *cascade08‚o†o*cascade08†oŒo *cascade08Œoo *cascade08o¼r *cascade08¼r¾r¾rër *cascade08ërìr*cascade08ìrŠs *cascade08ŠsŒsŒs”s *cascade08”s•s*cascade08•sªs *cascade08ªs¬s¬sµs *cascade08µs¶s*cascade08¶sÃs *cascade08Ãsäs *cascade08äsés*cascade08ésıs *cascade08ısşs*cascade08şs¤t *cascade08¤t¥t*cascade08¥tÌt *cascade08ÌtÍt*cascade08ÍtÚt *cascade08Útßt*cascade08ßtàt *cascade08àtât*cascade08âtçt *cascade08çtèt *cascade08ètÿt *cascade08ÿt‹u *cascade08‹u‘u *cascade08‘u’u*cascade08’u¥u *cascade08¥uœw *cascade08œww*cascade08wÓw *cascade08ÓwÔw*cascade08ÔwÖw *cascade08Öw×w*cascade08×wÍx *cascade08ÍxÏxÏxìx *cascade08ìxîx *cascade08îxïx *cascade08ïxñx *cascade08ñxòx *cascade08òxóx *cascade08óxşx*cascade08şxƒy *cascade08ƒy…y *cascade08…yy *cascade08y‘y *cascade08‘y’y *cascade08’y“y*cascade08“y”y *cascade08”y•y*cascade08•y˜y *cascade08˜y›y*cascade08›yy *cascade08y¢y*cascade08¢y¦y *cascade08¦y§y *cascade08§yªy *cascade08ªy´y *cascade08´yµy *cascade08µyÁy *cascade08ÁyÂy*cascade08ÂyÆy *cascade08ÆyÉy*cascade08ÉyÍy *cascade08ÍyĞy*cascade08ĞyÔy *cascade08ÔyÕy*cascade08Õy–z *cascade08–zz*cascade08zŸz *cascade08Ÿz²z*cascade08²z´z *cascade08´z·z*cascade08·z¸z *cascade08¸zÈz *cascade08ÈzÊz *cascade08Êzáz*cascade08ázâz *cascade08âzãz*cascade08ãzäz *cascade08äzùz *cascade08ùz{ *cascade08{‡{*cascade08‡{’{ *cascade08’{«{ *cascade08«{¬{ *cascade08¬{­{*cascade08­{¶{ *cascade08¶{€| *cascade08€|…| *cascade08…|â| *cascade08â|ä|ä|Í} *cascade08Í}Ö} *cascade08Ö}à}*cascade08à}í} *cascade08í}‡~*cascade08‡~Œ~ *cascade08Œ~~*cascade08~~ *cascade08~¦~ *cascade08¦~¿~ *cascade08¿~Æ~ *cascade08Æ~È~ *cascade08È~É~ *cascade08É~Ø~*cascade08Ø~Ù~ *cascade08Ù~û~ *cascade08û~ü~ *cascade08ü~‚*cascade08‚ƒ *cascade08ƒ*cascade08‘ *cascade08‘¨*cascade08¨ª *cascade08ªÇ *cascade08ÇÉ *cascade08É‚ *cascade08‚ƒ *cascade08ƒ*cascade08 *cascade08*cascade08 *cascade08*cascade08Ÿ *cascade08Ÿ¡*cascade08¡¢ *cascade08¢± *cascade08±² *cascade08²¼*cascade08¼½ *cascade08½Ë *cascade08ËÍ *cascade08ÍÚ*cascade08ÚÛ *cascade08Û®‚ *cascade08
®‚°‚°‚½‚ *cascade08½‚¾‚ *cascade08¾‚†ƒ *cascade08†ƒ‡ƒ*cascade08‡ƒ’ƒ *cascade08’ƒ“ƒ *cascade08“ƒ”ƒ*cascade08”ƒ•ƒ *cascade08•ƒ„ *cascade08
„„„µ… *cascade08µ…È… *cascade08È…×…*cascade08×…Ü… *cascade08Ü…ß… *cascade08ß…ú… *cascade08ú…û… *cascade08û…•† *cascade08•†²† *cascade08²†€ˆ *cascade08€ˆ–ˆ *cascade08–ˆ˜ˆ *cascade08˜ˆšˆ *cascade08šˆ²ˆ *cascade08²ˆ³ˆ *cascade08³ˆ¼ˆ*cascade08¼ˆ½ˆ *cascade08½ˆ¿ˆ*cascade08¿ˆÂˆ *cascade08ÂˆÃˆ *cascade08ÃˆÆˆ*cascade08ÆˆÇˆ *cascade08ÇˆÊˆ*cascade08ÊˆËˆ *cascade08Ëˆßˆ*cascade08ßˆàˆ *cascade08àˆâˆ*cascade08âˆãˆ *cascade08ãˆìˆ *cascade08ìˆíˆ *cascade08íˆòˆ*cascade08òˆóˆ *cascade08óˆöˆ*cascade08öˆøˆ *cascade08øˆùˆ*cascade08ùˆûˆ *cascade08ûˆüˆ*cascade08üˆıˆ *cascade08ıˆƒ‰*cascade08ƒ‰‰ *cascade08‰—‰ *cascade08—‰˜‰ *cascade08˜‰›‰ *cascade08›‰œ‰ *cascade08œ‰‰*cascade08‰ ‰ *cascade08 ‰¡‰*cascade08¡‰¢‰ *cascade08¢‰ã‰ *cascade08ã‰ÿ‰ *cascade08ÿ‰‚Š*cascade08‚ŠƒŠ *cascade08ƒŠ„Š*cascade08„Š…Š *cascade08…ŠŠŠ*cascade08ŠŠ—Š *cascade08—Š˜Š*cascade08˜Š™Š *cascade08™Š›Š *cascade08›Š Š*cascade08 ŠÑŠ *cascade08ÑŠİŠ *cascade08İŠŞŠ *cascade08ŞŠçŠ *cascade08çŠ°• *cascade08°•Ã• *cascade08Ã•È• *cascade08È•ó• *cascade08ó•Ïš *cascade08ÏšĞš*cascade08ĞšÉœ *cascade08ÉœÊœ*cascade08Êœğœ *cascade08ğœñœ*cascade08ñœúœ *cascade08úœµ *cascade08µ·*cascade08·Í *cascade08ÍÎ *cascade08Î‘Ÿ *cascade08‘Ÿ’Ÿ*cascade08’Ÿ£ *cascade08£ƒ£ *cascade08ƒ£º¦ *cascade08º¦»¦*cascade08»¦Ü¦ *cascade08Ü¦İ¦*cascade08İ¦ì¦ *cascade08ì¦ú¦ *cascade08ú¦œ§ *cascade08œ§ § *cascade08 §¦§ *cascade08¦§Ğ§ *cascade08Ğ§Ö§ *cascade08Ö§Ù§*cascade08Ù§Ú§ *cascade08Ú§å§*cascade08å§ù§ *cascade08ù§£¨ *cascade08£¨¨¨ *cascade08¨¨Ğ¨ *cascade08Ğ¨Ú¨ *cascade08Ú¨Ü¨*cascade08Ü¨İ¨ *cascade08İ¨ì¨ *cascade08ì¨ğ¨ *cascade08ğ¨õ¨*cascade08õ¨…© *cascade08…©Ê© *cascade08Ê©ç© *cascade08ç©é© *cascade08é©ê© *cascade08ê©ñ© *cascade08ñ©ò©*cascade08ò©ó© *cascade08ó©ù©*cascade08ù©ÿ© *cascade08ÿ©’ª *cascade08’ªçª *cascade08çªøª *cascade08øª‘«*cascade08‘«›« *cascade08›«œ«*cascade08œ«« *cascade08«¡« *cascade08¡«¢« *cascade08¢«¤« *cascade08¤«¿« *cascade08¿«Ê« *cascade08Ê«Ë« *cascade08Ë«Î«*cascade08Î«Ï« *cascade08Ï«ì« *cascade08ì«í« *cascade08í«ğ« *cascade08ğ«ñ« *cascade08ñ«ó«*cascade08ó«ô«*cascade08ô«õ« *cascade08õ«û«*cascade08û«ş« *cascade08ş«ÿ« *cascade08ÿ«‡¬ *cascade08‡¬ˆ¬ *cascade08ˆ¬‹¬*cascade08‹¬Œ¬ *cascade08Œ¬¬*cascade08¬—¬ *cascade08—¬º¬ *cascade08º¬Ô¬ *cascade08Ô¬ê¬*cascade08ê¬€­*cascade08€­Š­ *cascade08Š­‹­*cascade08‹­Œ­ *cascade08Œ­­ *cascade08­‘­ *cascade08‘­“­ *cascade08“­«­ *cascade08«­¬­*cascade08¬­­­ *cascade08­­®­*cascade08®­°­ *cascade08°­³­*cascade08³­¸­ *cascade08¸­¹­ *cascade08¹­¼­*cascade08¼­½­ *cascade08½­¿­*cascade08¿­À­ *cascade08À­Ê­*cascade08Ê­¬® *cascade08¬®Ã® *cascade08Ã®Ü® *cascade08Ü®ã® *cascade08ã®ğ® *cascade08ğ®÷® *cascade08÷®Œ¯ *cascade08Œ¯ ¯*cascade08 ¯®¯ *cascade08®¯¯¯*cascade08¯¯Ï¯ *cascade08Ï¯Ö¯ *cascade08Ö¯â¯ *cascade08â¯è¯ *cascade08è¯•° *cascade08•°—° *cascade08—°Á° *cascade08Á°Ì°*cascade08Ì°Ñ° *cascade08Ñ°¿³ *cascade08¿³í³ *cascade08í³î³*cascade08î³ï³ *cascade08ï³ñ³*cascade08ñ³›´ *cascade08›´®´ *cascade08®´×´ *cascade08×´Ş´ *cascade08Ş´á´ *cascade08á´î´ *cascade08î´ó´*cascade08ó´Œµ *cascade08Œµ“µ *cascade08“µ¡µ *cascade08¡µ¯µ*cascade08¯µ°µ *cascade08°µ±µ*cascade08±µ²µ *cascade08²µĞµ *cascade08ĞµÖµ *cascade08Öµáµ*cascade08áµçµ *cascade08çµñµ*cascade08ñµòµ *cascade08òµ÷µ*cascade08÷µùµ *cascade08ùµüµ*cascade08üµıµ *cascade08ıµ„¶ *cascade08„¶ˆ¶ *cascade08ˆ¶™¶ *cascade08™¶¶¶ *cascade08¶¶à¶*cascade08à¶ü¶ *cascade08ü¶ı¶*cascade08ı¶ş¶ *cascade08ş¶€·*cascade08€·Ô· *cascade08Ô·Œ¸ *cascade08Œ¸Ï¸ *cascade08Ï¸à¸*cascade08à¸³¹ *cascade08³¹º¹ *cascade08º¹Ä¹ *cascade08Ä¹Ë¹ *cascade08Ë¹î¹ *cascade08î¹‚º*cascade08‚ºƒº *cascade08ƒº„º*cascade08„º¤º *cascade08¤º«º *cascade08«º·º *cascade08·º½º *cascade08½ºêº *cascade08êºìº *cascade08ìº–» *cascade08–»¡»*cascade08¡»¦» *cascade08¦»‰¾ *cascade08‰¾¼¾ *cascade08¼¾À¾*cascade08À¾ô¾ *cascade08ô¾…¿*cascade08…¿‰¿ *cascade08‰¿¢¿ *cascade08¢¿·¿ *cascade08·¿»¿*cascade08»¿Ì¿ *cascade08Ì¿ğ¿ *cascade08ğ¿ú¿ *cascade08ú¿‹À *cascade08‹ÀŒÀ *cascade08ŒÀ“À*cascade08“À”À *cascade08”À—À*cascade08—À˜À *cascade08˜À¢À*cascade08¢À¤À *cascade08¤À¨À*cascade08¨À©À *cascade08©ÀªÀ*cascade08ªÀ¬À *cascade08¬À®À *cascade08®À»À *cascade08»ÀÂÀ*cascade08ÂÀÃÀ *cascade08ÃÀÄÀ*cascade08ÄÀÙÀ *cascade08ÙÀŞÀ*cascade08ŞÀîÀ *cascade08îÀñÀ *cascade08ñÀöÀ *cascade08öÀøÀ*cascade08øÀúÀ *cascade08úÀŒÁ *cascade08ŒÁ–Á *cascade08–Á¥Á*cascade08¥Á©Á *cascade08©Á®Á*cascade08®Á¯Á *cascade08¯Á·Á*cascade08·Á¸Á *cascade08¸Á½Á*cascade08½Á¾Á *cascade08¾Á¿Á*cascade08¿ÁÀÁ *cascade08ÀÁÁÁ*cascade08ÁÁÌÁ *cascade08ÌÁÍÁ*cascade08ÍÁñÁ *cascade08ñÁˆÂ*cascade08ˆÂ‰Â *cascade08‰ÂŠÂ*cascade08ŠÂ¹Â *cascade08¹ÂÓÂ*cascade08ÓÂÔÂ *cascade08ÔÂÕÂ*cascade08ÕÂİÂ *cascade08İÂÃ *cascade08ÃãÃ *cascade08ãÃôÃ*cascade08ôÃÄ *cascade08ÄÇ *cascade08Ç‚Ç*cascade08‚Ç‹Ç *cascade08‹ÇŒÇ*cascade08ŒÇ¥Ç *cascade08¥Ç¦Ç*cascade08¦ÇÁÉ *cascade08ÁÉÂÉ*cascade08ÂÉŞÉ *cascade08ŞÉßÉ*cascade08ßÉåÉ *cascade08åÉæÉ*cascade08æÉçÉ*cascade08çÉëÉ *cascade08ëÉìÉ*cascade08ìÉïÉ *cascade08ïÉğÉ*cascade08ğÉœÊ *cascade08œÊÊ*cascade08Ê¤Ê *cascade08¤Ê¥Ê*cascade08¥Ê¬Ê *cascade08¬ÊºÊ *cascade08ºÊŸÌ *cascade08ŸÌ¡Ì *cascade08¡Ì¢Ì *cascade08¢Ì£Ì*cascade08£Ì¥Ì *cascade08¥ÌŞÌ *cascade08ŞÌèÌ *cascade08èÌêÌ *cascade08êÌ€Í *cascade08€Í‹Í*cascade08‹Í’Í *cascade08’Í”Í*cascade08”ÍÍ *cascade08Í Í *cascade08 ÍöÎ *cascade08öÎ‡Ï *cascade08‡ÏÆÏ *cascade08ÆÏ×Ï *cascade08×Ï†Ğ *cascade08†Ğ‘Ğ *cascade08‘Ğ“Ğ *cascade08“Ğ–Ğ *cascade08–Ğ—Ğ*cascade08—Ğ©Ğ *cascade08©Ğ³Ğ *cascade08³Ğ´Ğ*cascade08´ĞµĞ *cascade08µĞ·Ğ*cascade08·Ğ¸Ğ *cascade08¸Ğ¹Ğ*cascade08¹ĞºĞ *cascade08ºĞÁĞ*cascade08ÁĞÈĞ *cascade08ÈĞÎĞ *cascade08ÎĞâĞ *cascade08âĞëĞ*cascade08ëĞíĞ *cascade08íĞîĞ*cascade08îĞñĞ *cascade08ñĞùĞ*cascade08ùĞ„Ñ *cascade08„Ñ‰Ñ*cascade08‰ÑŠÑ *cascade08ŠÑÑ*cascade08Ñ Ñ *cascade08 Ñ©Ñ*cascade08©Ñ³Ñ *cascade08³ÑºÑ*cascade08ºÑ»Ñ *cascade08»Ñ½Ñ*cascade08½ÑÅÑ *cascade08ÅÑÉÑ*cascade08ÉÑİÑ *cascade08İÑäÑ*cascade08äÑåÑ *cascade08åÑçÑ*cascade08çÑñÑ *cascade08ñÑúÑ*cascade08úÑûÑ *cascade08ûÑ€Ò*cascade08€Ò”Ò *cascade08”Ò›Ò*cascade08›ÒœÒ *cascade08œÒÒ*cascade08Ò¹Ò *cascade08¹ÒÀÓ *cascade08ÀÓãÓ *cascade08ãÓîÓ*cascade08îÓƒÔ *cascade08ƒÔ„Ô*cascade08„Ô…Ô *cascade08…ÔŠÔ*cascade08ŠÔ‹Ô *cascade08‹ÔÔ*cascade08ÔÔ *cascade08ÔÔ*cascade08ÔÔ *cascade08Ô‘Ô*cascade08‘Ô–Ô *cascade08–Ô˜Ô*cascade08˜Ô™Ô *cascade08™ÔšÔ*cascade08šÔÔ *cascade08ÔŸÔ*cascade08ŸÔ¢Ô*cascade08¢ÔŒÕ *cascade08ŒÕ”Õ*cascade08”Õ¶Õ *cascade08¶Õ¸Õ*cascade08¸ÕÄÕ *cascade08ÄÕÊÕ*cascade08ÊÕËÕ *cascade08ËÕğÕ*cascade08ğÕ÷Õ *cascade08÷Õ­× *cascade08­×È× *cascade08È×Ğ× *cascade08Ğ×Ñ× *cascade08Ñ×Ó×*cascade08Ó×Ô× *cascade08Ô×Ú×*cascade08Ú×Ş× *cascade08Ş×ê× *cascade08ê×ë× *cascade08ë×í×*cascade08í×ï× *cascade08ï×ÿ× *cascade08ÿ×‚Ø *cascade08‚Ø‰Ø*cascade08‰ØŠØ *cascade08ŠØØ *cascade08Ø—Ø *cascade08—Ø˜Ø*cascade08˜Ø™Ø *cascade08™Ø›Ø*cascade08›Ø¡Ø *cascade08¡Ø¯Ø *cascade08¯Ø²Ø *cascade08²ØÆØ *cascade08ÆØÇØ *cascade08ÇØÊØ*cascade08ÊØËØ *cascade08ËØĞØ*cascade08ĞØÑØ *cascade08ÑØáØ *cascade08áØãØ *cascade08ãØäØ*cascade08äØåØ *cascade08åØéØ*cascade08éØêØ *cascade08êØïØ *cascade08ïØğØ *cascade08ğØòØ*cascade08òØôØ *cascade08ôØ…Ù*cascade08…Ù†Ù *cascade08†ÙˆÙ*cascade08ˆÙŒÙ *cascade08ŒÙÙ*cascade08ÙÙ *cascade08ÙÙ *cascade08ÙŸÙ *cascade08ŸÙ Ù *cascade08 Ù¢Ù*cascade08¢Ù£Ù *cascade08£ÙµÙ *cascade08µÙ¸Ù *cascade08¸ÙÀÙ *cascade08ÀÙÄÙ *cascade08ÄÙÇÙ*cascade08ÇÙÈÙ *cascade08ÈÙÊÙ*cascade08ÊÙËÙ *cascade08ËÙÓÙ*cascade08ÓÙÔÙ *cascade08ÔÙÕÙ*cascade08ÕÙßÙ *cascade08ßÙâÙ*cascade08âÙãÙ *cascade08ãÙäÙ*cascade08äÙëÙ *cascade08ëÙóÙ *cascade08óÙ÷Ù *cascade08÷ÙúÙ*cascade08úÙûÙ *cascade08ûÙüÙ*cascade08üÙıÙ *cascade08ıÙ„Ú*cascade08„Ú…Ú *cascade08…Ú†Ú*cascade08†Ú‡Ú *cascade08‡ÚˆÚ*cascade08ˆÚ’Ú *cascade08’ÚœÚ*cascade08œÚÚ *cascade08ÚŸÚ*cascade08ŸÚ Ú *cascade08 Ú®Ú *cascade08®Ú°Ú *cascade08°Ú±Ú*cascade08±Ú²Ú *cascade08²Ú¶Ú*cascade08¶Ú·Ú *cascade08·Ú¼Ú *cascade08¼Ú½Ú *cascade08½ÚÆÚ*cascade08ÆÚÇÚ *cascade08ÇÚÍÚ*cascade08ÍÚÎÚ *cascade08ÎÚÖÚ*cascade08ÖÚÜÚ *cascade08ÜÚâÚ *cascade08âÚñÚ *cascade08ñÚøÚ*cascade08øÚùÚ *cascade08ùÚşÚ *cascade08şÚÿÚ *cascade08ÿÚ‚Û *cascade08‚ÛƒÛ *cascade08ƒÛ‡Û*cascade08‡ÛˆÛ *cascade08ˆÛ‹Û*cascade08‹ÛŒÛ *cascade08ŒÛ–Û *cascade08–Û—Û *cascade08—Û™Û *cascade08™ÛŸÛ *cascade08ŸÛ­Û *cascade08­Û´Û*cascade08´ÛµÛ *cascade08µÛ½Û *cascade08½Û¾Û *cascade08¾Û¿Û *cascade08¿ÛÆÛ*cascade08ÆÛÈÛ *cascade08ÈÛÊÛ*cascade08ÊÛÒÛ *cascade08ÒÛÙÛ *cascade08ÙÛàÛ *cascade08àÛâÛ *cascade08âÛåÛ*cascade08åÛ‘Ü *cascade08‘Ü£Ş *cascade08£ŞæŞ *cascade08æŞûŞ *cascade08ûŞüŞ*cascade08üŞñß *cascade08ñß–à *cascade08–à˜à *cascade08˜àœà *cascade08œà à*cascade08 à¥à *cascade08¥à«à *cascade08«à­à *cascade08­à®à*cascade08®à¯à*cascade08¯à°à*cascade08°à²à *cascade08²à³à*cascade08³à´à*cascade08´à·à *cascade08·à¸à *cascade08¸à¹à*cascade08¹à½à *cascade08½à¾à*cascade08¾à¿à *cascade08¿àÀà *cascade08ÀàÂà *cascade08ÂàÃà*cascade08ÃàÄà *cascade08ÄàÅà*cascade08ÅàÎà *cascade08ÎàÏà *cascade08ÏàĞà *cascade08ĞàÑà*cascade08ÑàÓà *cascade08ÓàÕà *cascade08ÕàØà*cascade08ØàÚà *cascade08Úààà *cascade08ààâà *cascade08âàãà*cascade08ãàåà *cascade08åàæà*cascade08æàçà *cascade08çàëà *cascade08ëàìà *cascade08ìàöà *cascade08öàûà *cascade08ûà€á *cascade08€áá*cascade08á…á *cascade08…áŒá *cascade08Œáá 
ááá¬á ¬á°á *cascade08°á±á*cascade08±á·á *cascade08·á¸á*cascade08¸á¾á *cascade08¾á¿á*cascade08¿áÅá *cascade08ÅáÆá*cascade08ÆáÇá*cascade08ÇáÒá *cascade08ÒáÔá *cascade08ÔáÖá *cascade08ÖáÚá *cascade08ÚáÛá*cascade08Ûáİá *cascade08İáãá *cascade08ãáäá*cascade08äáëá *cascade08ëáìá*cascade08ìáíá*cascade08íáñá *cascade08ñáòá*cascade08òáóá*cascade08óá÷á *cascade08÷áøá *cascade08øáùá *cascade08ùáúá*cascade08úáûá*cascade08ûáâ *cascade08â‚â*cascade08‚â‰â *cascade08‰âŠâ *cascade08Šâ‹â *cascade08‹ââ *cascade08ââ *cascade08â”â*cascade08”â–â *cascade08–âšâ *cascade08šâ›â*cascade08›âŸâ *cascade08Ÿâ¡â *cascade08¡â­â *cascade08­âÌâ *cascade08Ìâ‹ã *cascade08‹ãŒã *cascade08Œãã*cascade08ãã *cascade08ãã*cascade08ãã *cascade08ã§ã *cascade08§ã©ã *cascade08©ã¯ã*cascade08¯ã²ã *cascade08²ã³ã *cascade08³ãØã *cascade08ØãÚã *cascade08ÚãŞã *cascade08Şãëã*cascade08ëãõã *cascade08õãöã*cascade08öã÷ã*cascade08÷ãùã *cascade08ùãúã*cascade08úãßä *cascade08ßäêä*cascade08êäƒå *cascade08ƒåå*cascade08åå *cascade08åªå*cascade08ªåµå *cascade08µå¶å*cascade08¶å»å *cascade08»åÈå*cascade08ÈåÏå *cascade08ÏåĞå*cascade08ĞåÕå *cascade08Õåâå*cascade08âåëå *cascade08ëåìå*cascade08ìåîå *cascade08îåóå *cascade08óåæ *cascade08æÀë *cascade08Àëáí *cascade08áíıî *cascade08ıîşî*cascade08şîÿî *cascade08ÿî‚ï*cascade08‚ï„ï *cascade08„ï‡ï*cascade08‡ïŒï *cascade08
Œïïïßï *cascade08ßïàï*cascade08àïáï *cascade08áïâï*cascade08âï™ğ *cascade08™ğšğ*cascade08šğğ *cascade08ğğ*cascade08ğŸğ *cascade08Ÿğ¡ğ*cascade08¡ğ¢ğ *cascade08¢ğ¤ğ*cascade08¤ğªğ *cascade08ªğ«ğ*cascade08«ğ¬ğ *cascade08¬ğ²ğ*cascade08²ğµğ *cascade08µğ¶ğ*cascade08¶ğÃğ *cascade08ÃğÊğ*cascade08ÊğÍğ *cascade08ÍğÎğ*cascade08ÎğÖğ *cascade08Öğ×ğ *cascade08×ğÚğ*cascade08Úğçğ *cascade08çğõğ*cascade08õğñ *cascade08ñ„ñ*cascade08„ñ˜ñ *cascade08˜ñšñ*cascade08šñœñ *cascade08œñ©ñ*cascade08©ñ­ñ *cascade08­ñ¶ñ*cascade08¶ñ·ñ *cascade08·ñÃñ*cascade08ÃñÍñ *cascade08ÍñÑñ*cascade08ÑñÓñ *cascade08ÓñÖñ*cascade08ÖñŞñ *cascade08Şñßñ*cascade08ßñäñ *cascade08äñæñ*cascade08æñéñ *cascade08éñò*cascade08ò’ò *cascade08’ò”ò*cascade08”ò—ò *cascade08—òµò*cascade08µò»ò *cascade08»ò½ò*cascade08½òÀò *cascade08ÀòÜò*cascade08ÜòŞò *cascade08Şòàò*cascade08àòèò *cascade08èòøò*cascade08øòùò*cascade08ùòúò*cascade08úò–ó *cascade08–ó—ó*cascade08—ó™ó *cascade08™óšó*cascade08šó¦ó *cascade08¦ó¨ó*cascade08¨ó«ó *cascade08«óµó*cascade08µó¹ó *cascade08¹óËó*cascade08ËóÙó *cascade08Ùó§ô *cascade08§ô©ô*cascade082Kfile:///c:/AI-Test/AgentsCope-07/geo_agentscope/tools/scoring/geo_scorer.py