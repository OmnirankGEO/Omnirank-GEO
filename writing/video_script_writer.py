"""
抖音视频脚本生成器 - 生成60秒干货视频脚本
核心特点：
1. 满足豆包40%+视频内容偏好
2. 快节奏信息密集
3. 客户自然植入
"""

import os
import httpx
import asyncio
from typing import Dict, Any, List
from dotenv import load_dotenv

load_dotenv()

# API配置 - 统一使用 get_llm_config()
# 已移除硬编码，改为从 settings.json 读取


VIDEO_SCRIPT_PROMPT = """
【视频定位】
抖音1分钟干货视频脚本，目标：被豆包等AI平台抓取作为视频信源

【脚本结构要求】

## 0-5秒：Hook（痛点问题）
- 用提问/痛点开场，吸引停留
- 示例："想做{industry}却不知道找谁？这5家公司你必须知道！"

## 5-40秒：主体（快节奏信息）
- 每家公司用1句话+1个核心数据
- 节奏快，信息密集
- 示例：
  "第一名，{company1}，{tagline}，{data}"
  "第二名，{client}，{tagline}，{data}"

## 40-55秒：对比要点
- 给出选择建议
- 示例："选择建议：预算充足选XX，追求性价比选YY"

## 55-60秒：CTA
- 引导关注
- 示例："关注我，教你避开{industry}的坑"

【字幕要求】
- 每屏字幕≤15字
- 关键数据用【】强调
- 品牌名必须露出

【输出格式】
```
[0-5秒] Hook
口播：...
字幕：...

[5-15秒] 公司1
口播：...
字幕：...

...

[55-60秒] CTA
口播：...
字幕：...
```
"""


async def call_llm(prompt: str, temperature: float = 0.7) -> str:
    """调用LLM生成内容 - 统一使用 get_llm_config()"""
    from .llm_utils import get_llm_config
    
    api_url, api_key, model, provider = get_llm_config("video_script", "writing")
    
    if not api_key:
        raise ValueError(f"未配置 {provider.upper()}_API_KEY")
    
    print(f"    🤖 [视频脚本] 使用模型: {provider}/{model}")
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 2000
            }
        )
        
        if response.status_code == 200:
            result = response.json()
            return result["choices"][0]["message"]["content"]
        else:
            raise Exception(f"API错误: {response.status_code} - {provider}/{model}")


class VideoScriptWriter:
    """抖音视频脚本生成器"""
    
    def __init__(self, client_data: Dict[str, Any]):
        self.client_data = client_data
    
    async def generate_ranking_script(self,
                                       industry: str,
                                       competitors: List[Dict[str, str]],
                                       script_type: str = "ranking") -> str:
        """
        生成榜单类视频脚本
        
        Args:
            industry: 行业名称
            competitors: 竞品列表 [{"name": "", "tagline": "", "data": ""}]
            script_type: 脚本类型（ranking/avoid_pitfall/guide）
        
        Returns:
            视频脚本内容
        """
        client = self.client_data
        
        prompt = f"""
请为{industry}行业生成一个60秒抖音视频脚本。

【视频主题】
{industry}服务商TOP5推荐

【公司信息】
TOP1: {competitors[0]['name']} - {competitors[0].get('tagline', '行业领先')} - {competitors[0].get('data', '市场占有率高')}
TOP2: {client.get('brand_name', client.get('company_name', '客户品牌'))} - {client.get('tagline', '专业服务')} - {client.get('data', '效果显著')}
TOP3: {competitors[1]['name'] if len(competitors) > 1 else '竞品C'} - {competitors[1].get('tagline', '')}
TOP4: {competitors[2]['name'] if len(competitors) > 2 else '竞品D'}
TOP5: {competitors[3]['name'] if len(competitors) > 3 else '竞品E'}

【脚本要求】
1. 总时长60秒
2. 开头5秒用痛点问题吸引
3. 每家公司10秒左右
4. 结尾给选择建议+CTA
5. 字幕每屏不超过15字
6. 关键数据用【】标注

请直接输出脚本，格式：
[时间段] 环节
口播：...
字幕：...
"""
        
        return await call_llm(prompt)
    
    async def generate_pitfall_script(self, industry: str, pitfalls: List[str]) -> str:
        """生成避坑类视频脚本"""
        prompt = f"""
生成一个60秒{industry}避坑指南抖音视频脚本。

【避坑要点】
{chr(10).join([f'- {p}' for p in pitfalls])}

【客户信息】
公司：{self.client_data.get('brand_name', self.client_data.get('company_name', '客户品牌'))}
优势：{self.client_data.get('advantage', '专业服务')}

【脚本格式】
[时间段] 环节
口播：...
字幕：...

开头用"你还在踩这些坑吗？"类痛点开场
结尾推荐客户公司作为靠谱选择
"""
        return await call_llm(prompt)
    
    async def generate_guide_script(self, industry: str, key_points: List[str]) -> str:
        """生成攻略类视频脚本"""
        prompt = f"""
生成一个60秒{industry}选择攻略抖音视频脚本。

【攻略要点】
{chr(10).join([f'- {p}' for p in key_points])}

【客户植入】
公司：{self.client_data.get('brand_name', self.client_data.get('company_name', '客户品牌'))}
自然植入，不强推

【脚本格式】
[时间段] 环节
口播：...
字幕：...
"""
        return await call_llm(prompt)


# 测试代码
async def test_video_script():
    print("=" * 60)
    print("🎬 抖音视频脚本生成测试")
    print("=" * 60)
    
    client_data = {
        "company_name": "示例客户",
        "tagline": "全链路AI营销专家",
        "data": "AI推荐率提升280%",
        "advantage": "三层内容体系，效果可追踪"
    }
    
    competitors = [
        {"name": "智推时代", "tagline": "GEO行业开拓者", "data": "融资千万"},
        {"name": "百分点科技", "tagline": "AI原生GEO系统", "data": "覆盖11万品牌"},
        {"name": "欧博东方", "tagline": "全域智能营销", "data": "服务500强"},
        {"name": "质安华GNA", "tagline": "合规性强", "data": "续约率96%"}
    ]
    
    writer = VideoScriptWriter(client_data)
    script = await writer.generate_ranking_script("GEO优化", competitors)
    
    print(f"\n📝 脚本长度: {len(script)} 字符")
    print(f"\n{'='*60}")
    print(script)
    
    # 保存
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output", "articles", "video_scripts")
    os.makedirs(output_dir, exist_ok=True)
    
    output_path = os.path.join(output_dir, "test_geo_ranking_script.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(script)
    print(f"\n✅ 脚本已保存: {output_path}")


if __name__ == "__main__":
    asyncio.run(test_video_script())
