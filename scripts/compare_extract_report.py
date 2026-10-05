"""
PDF 数据提取对比 · Kimi K2.5 vs qwen3.6-plus

场景: 从诊断 MD 报告提取结构化 JSON (scripts/extract_report_data.py 做的事)
目标: 判断能否把 PDF 导出的 Kimi 换成 qwen3.6-plus(便宜 10x)
"""
import asyncio
import os
import time
import json
import httpx
from dotenv import load_dotenv
load_dotenv()

DS_KEY = os.getenv("DASHSCOPE_API_KEY")
# [CTO-15.23 2026-05-09] KIMI_KEY 已退役 · 改 deepseek-v4-flash(老板拍板:脚本工具不用 Kimi)

# 样例 GEO 诊断 MD (覆盖 prompt 期望的所有字段 · 典型长度 ~8KB)
SAMPLE_MD = """# AI搜索可见度诊断报告 - GEO专项

品牌名: 深圳全屋定制工坊
行业: 家居建材/全屋定制
诊断日期: 2026年04月20日
诊断系统: GEO诊断系统 v4.0

## 一、核心指标

GEO总分: 47/100
等级: 成长
AI测试次数: 32
AI推荐率: 12.5%

## 二、维度评分

| 维度 | 得分 | 满分 | 描述 |
|------|-----|-----|------|
| AI推荐率 | 12 | 30 | 品牌在AI搜索结果中出现频率偏低 |
| 网页内容 | 10 | 25 | 官网收录较少,SEO优化不足 |
| 权威背书 | 8 | 20 | 缺乏权威媒体报道和行业奖项 |
| 结构化内容 | 9 | 15 | 产品页结构基本合规但缺Schema |
| 品牌基础 | 8 | 10 | 工商信息完整,注册资本较高 |

## 三、AI引擎测试详情

向 4 大 AI 引擎(千问/DeepSeek/Kimi/豆包)提出 8 个行业高频问题,各引擎表现如下:

- 千问(Qwen): 推荐 1/8 次 | 表现: 偶尔提及,信息不全
- DeepSeek: 推荐 0/8 次 | 表现: 完全未提及该品牌
- Kimi: 推荐 2/8 次 | 表现: 偶有推荐,信息较浅
- 豆包: 推荐 1/8 次 | 表现: 识别品牌但缺细节

### 测试问题明细

| 测试问题 | 千问 | DeepSeek | Kimi | 豆包 |
|----------|-----|---------|------|------|
| 深圳全屋定制哪家好 | ✗ | ✗ | ✓ | ✗ |
| 深圳高端定制工厂推荐 | ✓ | ✗ | ✗ | ✗ |
| 南山全屋定制公司排名 | ✗ | ✗ | ✗ | ✗ |
| 深圳罗湖别墅装修定制 | ✗ | ✗ | ✓ | ✓ |
| 家庭全屋定制选哪家 | ✗ | ✗ | ✗ | ✗ |
| 深圳环保全屋定制 | ✗ | ✗ | ✗ | ✗ |
| 全屋定制十大品牌深圳 | ✗ | ✗ | ✗ | ✗ |
| 新房全屋定制找哪家 | ✗ | ✗ | ✗ | ✗ |

## 四、主要竞品分析

1. **欧派全屋定制** - AI 推荐频率极高,是行业头部
   - 官网 SEO 强,搜索结果首页几乎全是欧派
   - 有明星代言 + 央视广告投放
   - 知乎、小红书等社媒讨论度高
   - 门店覆盖全国所有一二线城市
   - 注册资本 30 亿,品牌历史 20+ 年

2. **索菲亚衣柜定制** - 社媒声量大,年轻用户认知度高
   - 小红书种草内容多,用户 UGC 丰富
   - 抖音直播销售额行业前三
   - 品牌调性偏年轻时尚
   - 产品线覆盖衣柜/厨房/书房等多品类

3. **尚品宅配** - 数字化营销标杆,AI 友好内容多
   - 官网结构化数据完整
   - 3D 云设计工具带流量,百度搜索排名高
   - 新浪家居合作深度
   - 门店服务标准化

## 五、漏斗分析

| 层级 | 数据 |
|------|-----|
| 月AI搜索量 | ~21,900 次 |
| 被AI推荐曝光 | ~2,738 次(12.5%) |
| 用户点击 | ~821 次(30%) |
| 产生咨询意向 | ~82 个(10%) |
| 最终成交 | ~2 单/月 |

客单价约 ¥5000, 当前月收入 ~¥10,000, 年损失约 ¥108 万(对比优化后潜在 ¥100,000/月)。

## 六、关键发现

1. **AI 引擎推荐率低**: 12.5% 远低于行业领先品牌(82%),缺乏结构化内容触发 AI 引用
2. **权威背书缺失**: 无行业协会认证、无权威媒体报道,AI 评估信任度低
3. **官网内容不够"AI 友好"**: 缺 FAQ Schema、无详细产品参数页
4. **社媒沉淀不足**: 小红书/知乎专业内容少

## 七、已有优势

- 品牌基础扎实: 注册资本 1000 万, 6 年品牌历史
- 产品力不错: 自有工厂生产,品控稳定
- 有部分本地口碑

## 八、优化建议

### Day 1-10 基础建设
- 建 FAQ 页面(5个常见问题)
- 补充产品详情页 Schema 标注
- 官网首页增加"为什么选我们"内容块
- 百度资讯投放 3 篇 PR 稿
- 小红书 10 篇品牌词笔记

### Day 11-20 内容矩阵
- 知乎回答 5 个行业高频问题
- 百家号 10 篇 GEO 优化文章
- 搜狐/网易新闻稿 2 篇

### Day 21-30 权威建设
- 行业协会会员资质申请
- 3 家权威媒体专访约稿
- 客户案例故事专题(5 个)

## 九、效果预估

- 30 天后 GEO 分数: 58 / AI 推荐率 30%
- 60 天后 GEO 分数: 68 / AI 推荐率 50%
- 90 天后 GEO 分数: 75 / AI 推荐率 65%

### 风险

- 竞品加速投放(中等风险) → 需持续监控
- 算法更新(低风险) → 保持多引擎覆盖
- 内容同质化(中等风险) → 差异化定位

## 十、数据方法

- 原始数据量: 280+ 条
- 引擎数: 4 大 AI 引擎实测
- 问题数: 8 个行业高频问题
- 平台覆盖: 7 个关键词 × 2 大平台
- 行业平均分: 45 / 行业最佳: 82
"""

EXTRACTION_PROMPT_TEMPLATE = """你是一个专业的数据提取助手。请从以下诊断报告的Markdown内容中,提取结构化数据并以JSON格式返回。

**首先判断报告类型(scope):**
- 如果标题包含"AI搜索可见度诊断报告"或"GEO专项" → scope = "geo"
- 否则 → scope = "full"

**重要: 只返回JSON, 不要有任何其他文字、代码块标记或解释。**

字段结构:
{{
  "brandName": "品牌名",
  "industry": "行业",
  "date": "YYYY.MM.DD",
  "scope": "geo/social/full",
  "geoScore": 数字, "maxScore": 100, "level": "等级",
  "aiTests": 数字, "aiRecommendRate": 数字,
  "executiveSummary": {{"headline":"20字内", "subline":"40字内", "quickWins":[{{"action":"10字内","impact":"+5-8分","effort":"低","timeline":"3天"}}, ...3项]}},
  "revenueImpact": {{"monthlySearchVolume":"...","currentMonthlyOrders":"...","potentialMonthlyOrders":"...","avgOrderValue":"...","currentMonthlyRevenue":"...","potentialMonthlyRevenue":"...","annualLoss":"...","lossNote":"..."}},
  "dims": [{{"name":"维度","score":数,"max":数,"pct":百分比,"desc":"15字内","status":"状态","advice":"20字内"}}, ...],
  "engines": [{{"name":"引擎名","status":true/false,"detail":"20字内","mentions":数,"total":数}}, ...],
  "testQuestions": [{{"q":"15字内","ds":"✓","kimi":"✓","db":"✗","gpt":"✗"}}, ...8项],
  "industryAvg": 数字, "industryBest": 数字,
  "competitors": [{{"name":"真实名","tag":"4字内","score":数,"strength":"4字内","points":["15字内",..5项]}}, ...3项],
  "funnel": [{{"label":"...","value":"...","width":100,"note":"..."}}, ...5项],
  "findings": [{{"title":"8字内","status":"warning/danger/good","desc":"40字内","fix":"30字内"}}, ...3-4项],
  "existingAdvantages": [{{"title":"6字内","desc":"15字内"}}, ...2-3项],
  "actions": [{{"phase":"阶段名","days":"Day 1-10","items":[{{"task":"10字内","detail":"15字内"}}, ...5项],"goal":"10字内","kpi":"..."}}, ...3阶段],
  "effectPrediction": {{"day30Score":数,"day60Score":数,"day90Score":数,"day30Rate":"30%","day60Rate":"50%","day90Rate":"65%","risks":[{{"risk":"10字内","level":"high/medium/low","mitigation":"15字内"}}, ...2-3项]}},
  "methodology": {{"dataPoints":"...","engines":"...","questions":"...","platforms":"...","version":"GEO诊断系统 v4.0"}}
}}

**报告内容:**

{md_content}
"""


async def call_llm(name: str, url: str, key: str, model: str, prompt: str, extra_params: dict = None) -> dict:
    start = time.time()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 16000,
    }
    if extra_params:
        payload.update(extra_params)
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            elapsed = time.time() - start
            if resp.status_code != 200:
                return {"success": False, "model": name, "error": f"Status {resp.status_code}: {resp.text[:300]}", "elapsed": elapsed}
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return {
                "success": True, "model": name, "content": content, "elapsed": elapsed,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            }
    except Exception as e:
        return {"success": False, "model": name, "error": f"{type(e).__name__}: {e}", "elapsed": time.time() - start}


def clean_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def evaluate_json(content: str, label: str) -> dict:
    """评估 JSON 完整性(按 extract prompt 要求的字段)"""
    cleaned = clean_json(content)
    try:
        data = json.loads(cleaned)
    except Exception as e:
        return {"label": label, "parsed_ok": False, "error": str(e), "preview": cleaned[:200]}

    # 评分: 关键字段是否齐全 + 数组长度是否符合预期
    expected = {
        "brandName": ("str", None),
        "industry": ("str", None),
        "geoScore": ("num", None),
        "dims": ("list", 5),  # GEO 专项应该 5 个
        "engines": ("list", 4),  # 4 引擎
        "testQuestions": ("list", 8),  # 8 个问题
        "competitors": ("list", 3),  # 3 个竞品
        "funnel": ("list", 5),  # 5 层漏斗
        "findings": ("list", 3),
        "actions": ("list", 3),
        "effectPrediction": ("dict", None),
        "executiveSummary": ("dict", None),
        "revenueImpact": ("dict", None),
        "existingAdvantages": ("list", 2),
        "methodology": ("dict", None),
    }
    scores = {}
    total = 0
    perfect = 0
    for k, (typ, expected_len) in expected.items():
        v = data.get(k)
        if v is None or v == "" or v == [] or v == {}:
            scores[k] = "MISSING"
        elif typ == "list" and expected_len:
            actual_len = len(v)
            if actual_len >= expected_len:
                scores[k] = f"OK ({actual_len}/{expected_len})"
                perfect += 1
            else:
                scores[k] = f"SHORT ({actual_len}/{expected_len})"
            total += 1
        else:
            scores[k] = "OK"
            perfect += 1
            total += 1
        total_check = total
    # 计算分数
    fill_rate = sum(1 for v in scores.values() if v != "MISSING") / len(scores) * 100
    perfect_rate = sum(1 for v in scores.values() if v.startswith("OK")) / len(scores) * 100
    return {
        "label": label,
        "parsed_ok": True,
        "brand": data.get("brandName", "?"),
        "dims_count": len(data.get("dims", [])),
        "engines_count": len(data.get("engines", [])),
        "testq_count": len(data.get("testQuestions", [])),
        "competitors_count": len(data.get("competitors", [])),
        "findings_count": len(data.get("findings", [])),
        "actions_count": len(data.get("actions", [])),
        "fill_rate": round(fill_rate, 1),
        "perfect_rate": round(perfect_rate, 1),
        "field_scores": scores,
    }


async def main():
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(md_content=SAMPLE_MD)

    # [CTO-15.23 2026-05-09 Kimi 月烧治理] 文章对比脚本不需 web_search · Kimi → deepseek-v4-flash
    # 老板拍板:脚本工具(文章)用 deepseek-v4-flash · ¥1/M vs Kimi ¥4/M
    print("Calling DeepSeek V4-Flash...")
    deepseek = await call_llm(
        "DeepSeek V4-Flash",
        "https://api.deepseek.com/v1/chat/completions",
        os.environ.get("DEEPSEEK_API_KEY", ""),
        "deepseek-v4-flash",
        prompt,
        extra_params={"thinking": {"type": "disabled"}},
    )
    print(f"  {'OK' if deepseek.get('success') else 'FAIL'} · {deepseek['elapsed']:.1f}s · in={deepseek.get('input_tokens',0)} out={deepseek.get('output_tokens',0)}")

    # qwen3.6-plus
    print("Calling qwen3.6-plus...")
    qwen = await call_llm(
        "qwen3.6-plus",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        DS_KEY,
        "qwen3.6-plus",
        prompt,
    )
    print(f"  {'OK' if qwen.get('success') else 'FAIL'} · {qwen['elapsed']:.1f}s · in={qwen.get('input_tokens',0)} out={qwen.get('output_tokens',0)}")

    # 评估
    report = {"input_md_chars": len(SAMPLE_MD)}
    if deepseek.get("success"):
        report["deepseek_v4_flash"] = {
            **evaluate_json(deepseek["content"], "DeepSeek V4-Flash"),
            "elapsed": deepseek["elapsed"],
            "tokens": f"in={deepseek['input_tokens']} out={deepseek['output_tokens']}",
            "cost_yuan": (deepseek["input_tokens"] * 0.001 + deepseek["output_tokens"] * 0.002) / 1000,
            "raw_content": deepseek["content"],
        }
    if qwen.get("success"):
        report["qwen3_6_plus"] = {
            **evaluate_json(qwen["content"], "qwen3.6-plus"),
            "elapsed": qwen["elapsed"],
            "tokens": f"in={qwen['input_tokens']} out={qwen['output_tokens']}",
            "cost_yuan": (qwen["input_tokens"] * 0.0008 + qwen["output_tokens"] * 0.0048) / 1000,
            "raw_content": qwen["content"],
        }

    with open("scripts/compare_extract_result.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to scripts/compare_extract_result.json")


if __name__ == "__main__":
    asyncio.run(main())
