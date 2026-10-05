"""
GEO诊断报告 Markdown → 结构化JSON提取器
使用 Kimi K2.5 智能解析报告内容，生成PDF模板所需的JSON数据

用法:
    python scripts/extract_report_data.py <md_path> <json_output_path>
"""
import asyncio
import json
import sys
import os
import re
import io

# Fix Windows console encoding
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.multi_llm_caller import call_kimi

# 用于 LLM 的提示词
EXTRACTION_PROMPT = """你是一个专业的数据提取助手。请从以下诊断报告的Markdown内容中，提取结构化数据并以JSON格式返回。

**首先判断报告类型（scope）：**
- 如果标题包含"AI搜索可见度诊断报告"或"GEO专项" → scope = "geo"
- 如果标题包含"社媒内容生态诊断报告"或"社媒专项" → scope = "social"
- 否则 → scope = "full"

**重要：只返回JSON，不要有任何其他文字、代码块标记或解释。**

需要提取的字段结构如下（请严格遵循）：

```
{
  "brandName": "品牌名称",
  "industry": "行业",
  "date": "诊断日期（格式：YYYY.MM.DD）",
  "scope": "geo"或"social"或"full"（根据上面的判断规则填写）,
  "geoScore": 数字,
  "maxScore": 100,
  "level": "等级（空白/起步/成长/成熟/领先）",
  "contentTotal": 品牌内容总量(数字，仅社媒/全面报告适用，GEO报告填0),
  "platformCount": 平台覆盖数(数字，仅社媒/全面报告适用，GEO报告填0),
  "aiTests": AI测试总次数(数字),
  "aiRecommendRate": AI推荐率(数字，如12.5),
  "executiveSummary": {
    "headline": "一句话总结品牌AI搜索现状（20字以内，有冲击力）",
    "subline": "补充说明，包含关键数据（40字以内）",
    "quickWins": [
      {"action": "快速见效的优化动作（10字以内）", "impact": "预估提分效果如+5-8分", "effort": "低/中/高", "timeline": "所需天数如3天"}
    ]
  },
  "revenueImpact": {
    "monthlySearchVolume": "月搜索量数字如21900",
    "currentMonthlyOrders": "当前月订单如~2单",
    "potentialMonthlyOrders": "优化后月订单如~20单",
    "avgOrderValue": "客单价如¥5000",
    "currentMonthlyRevenue": "当前月收入如¥10,000",
    "potentialMonthlyRevenue": "优化后月收入如¥100,000",
    "annualLoss": "年度损失金额如¥108万",
    "lossNote": "延迟损失说明（20字以内）"
  },
  "dims": [
    {
      "name": "维度简称（按报告实际维度提取，如：AI推荐率/网页内容/权威背书/结构化内容/品牌基础/品牌存在感/竞品活跃度/行业生态/内容质量/运营基础/AI可见度/社媒声量 等）",
      "score": 得分(数字),
      "max": 满分(数字),
      "pct": 百分比(数字，0-100),
      "desc": "一句话描述该维度表现（15字以内，简洁有力）",
      "status": "状态（如：严重不足/完全缺失/基础/一般/良好/优秀）",
      "advice": "最核心的一条优化建议（20字以内）"
    }
  ],
  "engines": [
    {
      "name": "引擎名称（DeepSeek/Kimi/豆包/ChatGPT 或 千问）",
      "status": true或false(是否有推荐),
      "detail": "一句话总结该引擎的表现（20字以内）",
      "mentions": 被提及次数(数字),
      "total": 测试总次数(数字)
    }
  ],
  "testQuestions": [
    {
      "q": "测试问题（缩短到15字以内）",
      "ds": "✓"或"✗",
      "kimi": "✓"或"✗",
      "db": "✓"或"✗",
      "gpt": "✓"或"✗"
    }
  ],
  "industryAvg": 行业平均分(数字，如无数据则估算45),
  "industryBest": 行业最佳分(数字，如无数据则估算82),
  "competitors": [
    {
      "name": "竞品真实名称（必须用报告中的真实名字）",
      "tag": "一个标签（如：AI高频推荐/社媒强势/品牌权威/内容领先）",
      "score": 估算GEO分数(数字),
      "strength": "核心优势（4字以内）",
      "points": ["优势要点1（15字以内）", "要点2", "要点3", "要点4", "要点5"]
    }
  ],
  "funnel": [
    {"label": "月AI渠道搜索量", "value": "~XXX次", "width": 100, "note": "数据来源说明"},
    {"label": "被AI推荐曝光", "value": "~XXX次", "width": 72, "note": "说明"},
    {"label": "用户点击搜索", "value": "~XXX次", "width": 45, "note": "说明"},
    {"label": "产生咨询意向", "value": "~XXX个", "width": 28, "note": "说明"},
    {"label": "最终成交", "value": "~XX单/月", "width": 16, "note": "说明"}
  ],
  "findings": [
    {
      "title": "发现标题（8字以内）",
      "status": "warning"或"danger"或"good",
      "desc": "详细说明（40字以内，突出核心问题）",
      "fix": "修复建议（30字以内）"
    }
  ],
  "existingAdvantages": [
    {"title": "已有优势标题（6字以内）", "desc": "简要说明（15字以内）"}
  ],
  "actions": [
    {
      "phase": "阶段名称（如：基础建设）",
      "days": "Day 1-10",
      "items": [
        {"task": "任务名称（10字以内）", "detail": "具体说明（15字以内）"}
      ],
      "goal": "阶段目标（10字以内）",
      "kpi": "考核指标"
    }
  ],
  "effectPrediction": {
    "day30Score": 30天后预估GEO分数(数字),
    "day60Score": 60天后预估GEO分数(数字),
    "day90Score": 90天后预估GEO分数(数字),
    "day30Rate": "30天后AI推荐率如30%",
    "day60Rate": "60天后AI推荐率如50%",
    "day90Rate": "90天后AI推荐率如65%",
    "risks": [
      {"risk": "风险描述（10字以内）", "level": "high/medium/low", "mitigation": "应对措施（15字以内）"}
    ]
  },
  "methodology": {
    "dataPoints": "原始数据量如280+条",
    "engines": "引擎数如4大AI引擎实测",
    "questions": "问题数如8个行业高频问题",
    "platforms": "平台覆盖如7个关键词×2大平台",
    "version": "系统版本如GEO诊断系统 v4.0"
  }
}
```

**提取规则：**
1. `dims` 按报告中的评分表提取所有维度（GEO专项5个维度、社媒专项5个维度、全面诊断7个维度）
2. `engines` 提取报告中测试过的AI引擎（通常4个）。**社媒专项报告没有AI引擎测试，此字段填空数组[]**
3. `testQuestions` 最多取8个最有代表性的测试问题，问题文字缩短到15字以内。**社媒专项报告没有测试问题，此字段填空数组[]**
4. `competitors` 提取最多3个最重要的竞品，**必须使用报告中的真实名称**。GEO报告优先AI推荐竞品；社媒报告提取社媒热门账号
5. `funnel` 从报告中的漏斗数据提取，width按比例递减（100→72→45→28→16）。**社媒专项报告没有漏斗数据，此字段填空数组[]**
6. `findings` 提取3-4个最关键的发现，按优先级排序
7. `actions` 固定3个阶段（Day 1-10, Day 11-20, Day 21-30），每阶段5个任务
8. `executiveSummary` 要有冲击力，headline直击痛点，quickWins给出3个最快见效的优化动作
9. `revenueImpact` 基于报告中的漏斗数据计算收入影响。**社媒专项报告没有漏斗数据，各字段填空字符串或0**
10. `existingAdvantages` 提取2-3个品牌已有的优势或积极信号
11. `effectPrediction` 基于当前分数和行业数据合理预估30/60/90天效果，risks提取2-3个关键风险。社媒报告中day30Rate等改为"社媒曝光提升XX%"
12. `methodology` 从报告的数据方法说明部分提取
13. 所有文字内容要**精炼、专业、有冲击力**，适合放在PDF视觉报告中
14. 如果报告中某些数据缺失，请合理推断和补充，但不要编造数据
15. `testQuestions`中的引擎列对应关系：ds=DeepSeek, kimi=Kimi, db=豆包, gpt=千问/Qwen/ChatGPT
16. **社媒专项报告**：`aiTests`和`aiRecommendRate`填0，重点提取`contentTotal`（品牌内容总量）和`platformCount`（平台覆盖数）
17. `scope` 必须根据报告类型正确填写（"geo"/"social"/"full"）

**以下是报告Markdown内容：**

{md_content}
"""


def fallback_extract(md_content: str) -> dict:
    """Regex fallback — 当 LLM 调用失败时使用"""
    data = {}

    def _strip_md(s):
        return s.strip().strip('*').strip()

    m = re.search(r'品牌名[：:](.+)', md_content)
    data['brandName'] = _strip_md(m.group(1)) if m else '品牌名称'

    m = re.search(r'行业[：:](.+)', md_content)
    data['industry'] = _strip_md(m.group(1)) if m else ''

    m = re.search(r'(?:GEO|社媒)?总分[：:].*?(\d+)[/／](\d+)', md_content)
    data['geoScore'] = int(m.group(1)) if m else 0
    data['maxScore'] = int(m.group(2)) if m else 100

    m = re.search(r'等级[：:](.+)', md_content)
    data['level'] = _strip_md(m.group(1)) if m else '起步'

    m = re.search(r'AI测试次数[：:].*?(\d+)', md_content)
    data['aiTests'] = int(m.group(1)) if m else 0

    m = re.search(r'AI推荐率[：:].*?([\d.]+)%', md_content)
    data['aiRecommendRate'] = float(m.group(1)) if m else 0

    m = re.search(r'诊断日期.*?(\d{4})年(\d{2})月(\d{2})日', md_content)
    data['date'] = f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else '2026.03.16'

    # Social-specific metrics
    m = re.search(r'品牌内容总量\s*\|\s*(\d+)', md_content)
    data['contentTotal'] = int(m.group(1)) if m else 0
    m = re.search(r'平台覆盖\s*\|\s*(\d+)', md_content)
    data['platformCount'] = int(m.group(1)) if m else 0

    # 检测报告scope
    is_geo_scope = 'AI搜索可见度诊断报告' in md_content or 'GEO专项' in md_content
    is_social_scope = '社媒内容生态诊断报告' in md_content or '社媒专项' in md_content

    # 根据scope选择维度模式
    data['dims'] = []
    if is_geo_scope:
        dim_patterns = [
            ('AI推荐率', r'AI(?:引擎)?推荐率\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('网页内容', r'网页内容(?:资产)?\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('权威背书', r'权威背书\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('结构化内容', r'结构化内容\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('品牌基础', r'品牌基础\s*\|\s*(\d+)\s*\|\s*(\d+)'),
        ]
    elif is_social_scope:
        dim_patterns = [
            ('品牌存在感', r'品牌(?:社媒)?存在感\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('竞品活跃度', r'竞品活跃度\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('行业生态', r'行业(?:内容)?生态\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('内容质量', r'内容质量\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('运营基础', r'(?:账号)?运营基础\s*\|\s*(\d+)\s*\|\s*(\d+)'),
        ]
    else:
        dim_patterns = [
            ('AI可见度', r'AI引擎可见度\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('社媒声量', r'社媒内容资产\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('网页内容', r'网页内容资产\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('权威背书', r'权威背书\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('结构化内容', r'结构化内容\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('内容质量', r'内容质量\s*\|\s*(\d+)\s*\|\s*(\d+)'),
            ('品牌基础', r'品牌基础\s*\|\s*(\d+)\s*\|\s*(\d+)'),
        ]
    for name, pat in dim_patterns:
        m = re.search(pat, md_content)
        if m:
            s, mx = int(m.group(1)), int(m.group(2))
            data['dims'].append({
                'name': name, 'score': s, 'max': mx,
                'pct': round(s/mx*100) if mx else 0,
                'desc': '', 'status': '', 'advice': ''
            })

    data['scope'] = 'social' if is_social_scope else 'geo' if is_geo_scope else 'full'
    data['engines'] = []
    data['testQuestions'] = []
    data['industryAvg'] = 45
    data['industryBest'] = 82
    data['competitors'] = []
    data['funnel'] = []
    data['findings'] = []
    data['actions'] = []

    return data


def clean_json_response(text: str) -> str:
    """清理 LLM 返回的 JSON（去掉 markdown 代码块标记等）"""
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith('```'):
        lines = text.split('\n')
        # 去首行和末行
        if lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines)
    return text.strip()


async def extract_with_llm(md_content: str) -> dict:
    """使用 Kimi K2.5 提取报告数据"""
    prompt = EXTRACTION_PROMPT.replace('{md_content}', md_content)

    print("  📤 Calling Kimi K2.5 for structured extraction...")
    response = await call_kimi(prompt, verbose=True)

    # 清理并解析 JSON
    cleaned = clean_json_response(response)
    try:
        data = json.loads(cleaned)
        print(f"  ✅ LLM extraction successful: {data.get('brandName', '?')} / {len(data.get('dims', []))} dims")
        return data
    except json.JSONDecodeError as e:
        print(f"  ⚠️ JSON parse failed: {e}")
        print(f"  Response preview: {cleaned[:200]}...")
        raise


async def main():
    if len(sys.argv) < 3:
        print("Usage: python extract_report_data.py <md_path> <json_output_path>")
        sys.exit(1)

    md_path = sys.argv[1]
    json_path = sys.argv[2]

    print(f"📄 Reading: {md_path}")
    with open(md_path, 'r', encoding='utf-8') as f:
        md_content = f.read()

    try:
        data = await extract_with_llm(md_content)
    except Exception as e:
        print(f"  ⚠️ LLM extraction failed ({e}), using regex fallback...")
        data = fallback_extract(md_content)

    # 写出 JSON
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ JSON written: {json_path}")


if __name__ == '__main__':
    asyncio.run(main())
