"""
行业案例采集脚本 — 搜索真实案例 → 提取要素 → 脱敏改编 → 存为模板库

用法:
    python -m tools.industry_case_collector                        # 采集全部 20 个行业
    python -m tools.industry_case_collector --industry 全屋定制     # 采集单个行业
    python -m tools.industry_case_collector --industry 全屋定制 --industry 餐饮加盟  # 多个

    或者从项目根目录:
    cd /app && python tools/industry_case_collector.py             # Docker 容器内

产出: data/industry_cases/{industry_key}.json
"""

# 修复 sys.path: 移除 tools/ 目录防止 tools/keyword 遮蔽 stdlib keyword
import sys
import os
_tools_dir = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p) != _tools_dir]

import json
import asyncio
import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

# 项目根目录（不加入 sys.path 避免 tools/keyword 和 stdlib keyword 冲突）
ROOT_DIR = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CaseCollector")

# ========== 20 个热门行业 ==========

INDUSTRIES = [
    {"name": "全屋定制", "key": "home_furniture",
     "typical_numbers": {"客单价范围": "5-50万", "服务周期": "30-90天", "常见节省": "10-30%"},
     "case_structure": {"必备要素": ["客户名称", "房型/面积", "之前方案", "我们方案", "费用对比", "客户反馈"], "加分要素": ["工期对比", "售后故事", "转介绍"]}},
    {"name": "装修公司", "key": "renovation",
     "typical_numbers": {"客单价范围": "10-100万", "服务周期": "60-180天", "常见节省": "15-25%"},
     "case_structure": {"必备要素": ["客户名称", "户型面积", "装修风格", "总价", "工期", "客户评价"], "加分要素": ["环保指标", "工艺亮点", "邻居反馈"]}},
    {"name": "餐饮加盟", "key": "food_franchise",
     "typical_numbers": {"投资范围": "5-50万", "回本周期": "6-18个月", "月营业额": "5-30万"},
     "case_structure": {"必备要素": ["加盟商背景", "品牌选择", "投资金额", "月营业额", "回本周期"], "加分要素": ["选址策略", "淡旺季对比", "二店计划"]}},
    {"name": "奶茶加盟", "key": "milk_tea",
     "typical_numbers": {"投资范围": "8-30万", "回本周期": "4-12个月", "日均杯数": "200-500杯"},
     "case_structure": {"必备要素": ["加盟商背景", "门店位置", "投资金额", "日均销量", "月利润"], "加分要素": ["季节性变化", "爆品策略", "复购率"]}},
    {"name": "美容美发", "key": "beauty_salon",
     "typical_numbers": {"客单价范围": "100-2000元", "会员续费率": "40-70%", "月营收": "5-30万"},
     "case_structure": {"必备要素": ["店铺情况", "之前困境", "改造方案", "业绩变化", "客户评价"], "加分要素": ["员工成长", "新客获取", "口碑传播"]}},
    {"name": "医美机构", "key": "medical_beauty",
     "typical_numbers": {"客单价范围": "3000-50000元", "复购率": "30-60%", "月营收": "20-200万"},
     "case_structure": {"必备要素": ["机构规模", "获客渠道", "客户案例", "效果展示", "满意度"], "加分要素": ["术后跟踪", "口碑传播", "专家IP"]}},
    {"name": "律师事务所", "key": "law_firm",
     "typical_numbers": {"案件收费": "5000-50万", "胜诉率": "70-90%", "客户满意度": "85-95%"},
     "case_structure": {"必备要素": ["案件类型", "客户困境", "法律策略", "判决结果", "客户评价"], "加分要素": ["维权金额", "时间线", "后续服务"]}},
    {"name": "知识付费", "key": "online_education",
     "typical_numbers": {"课程价格": "99-9999元", "完课率": "30-60%", "复购率": "20-40%"},
     "case_structure": {"必备要素": ["学员背景", "学习目标", "课程选择", "学习成果", "收入变化"], "加分要素": ["学习时长", "社群互动", "推荐他人"]}},
    {"name": "电商代运营", "key": "ecommerce_agency",
     "typical_numbers": {"月服务费": "5000-50000元", "GMV提升": "50-300%", "ROI": "1:3-1:10"},
     "case_structure": {"必备要素": ["店铺类型", "之前数据", "代运营方案", "数据变化", "合作时长"], "加分要素": ["爆品打造", "活动策划", "评价管理"]}},
    {"name": "房产中介", "key": "real_estate",
     "typical_numbers": {"成交周期": "15-90天", "佣金比例": "1-3%", "月成交量": "3-15套"},
     "case_structure": {"必备要素": ["客户需求", "看房过程", "成交价格", "节省金额", "客户感受"], "加分要素": ["谈判技巧", "贷款协助", "转介绍"]}},
    {"name": "保险代理", "key": "insurance_agent",
     "typical_numbers": {"件均保费": "5000-50000元", "续保率": "80-95%", "理赔满意度": "90%+"},
     "case_structure": {"必备要素": ["客户家庭情况", "保障需求", "方案设计", "理赔故事", "客户评价"], "加分要素": ["对比分析", "家庭保单", "转介绍"]}},
    {"name": "汽车改装维修", "key": "auto_service",
     "typical_numbers": {"客单价范围": "500-50000元", "复购率": "60-80%", "月营收": "10-50万"},
     "case_structure": {"必备要素": ["车型", "问题描述", "维修/改装方案", "费用", "客户评价"], "加分要素": ["对比4S店价格", "工期", "质保承诺"]}},
    {"name": "摄影工作室", "key": "photography",
     "typical_numbers": {"客单价范围": "2000-20000元", "转介绍率": "30-50%", "月订单": "10-30单"},
     "case_structure": {"必备要素": ["拍摄类型", "客户需求", "拍摄方案", "成片效果", "客户评价"], "加分要素": ["风格对比", "精修细节", "朋友圈晒图"]}},
    {"name": "婚庆策划", "key": "wedding_planning",
     "typical_numbers": {"客单价范围": "2-30万", "满意度": "90%+", "转介绍率": "40-60%"},
     "case_structure": {"必备要素": ["新人背景", "婚礼风格", "预算与实际", "婚礼亮点", "新人评价"], "加分要素": ["应急处理", "宾客反馈", "纪念回访"]}},
    {"name": "健身瑜伽", "key": "fitness_yoga",
     "typical_numbers": {"月卡价格": "200-500元", "年卡价格": "2000-8000元", "续卡率": "40-65%"},
     "case_structure": {"必备要素": ["会员背景", "健身目标", "训练方案", "身体变化", "会员感受"], "加分要素": ["体测数据", "饮食搭配", "社群活动"]}},
    {"name": "宠物店", "key": "pet_shop",
     "typical_numbers": {"客单价范围": "100-3000元", "会员复购率": "50-75%", "月营收": "3-15万"},
     "case_structure": {"必备要素": ["宠物类型", "服务内容", "效果展示", "主人评价", "复购情况"], "加分要素": ["紧急救助", "宠物变化", "社群分享"]}},
    {"name": "母婴用品", "key": "maternity_baby",
     "typical_numbers": {"客单价范围": "200-5000元", "复购率": "60-80%", "月营收": "5-30万"},
     "case_structure": {"必备要素": ["宝妈背景", "需求痛点", "产品推荐", "使用效果", "妈妈评价"], "加分要素": ["安全认证", "对比测评", "群内分享"]}},
    {"name": "农产品土特产", "key": "agriculture",
     "typical_numbers": {"客单价范围": "50-500元", "复购率": "30-50%", "月发货量": "500-5000单"},
     "case_structure": {"必备要素": ["产品特色", "产地故事", "品质保障", "客户评价", "销量数据"], "加分要素": ["溯源体系", "直播带货", "企业团购"]}},
    {"name": "外贸跨境电商", "key": "cross_border",
     "typical_numbers": {"月GMV": "5-100万美元", "利润率": "15-40%", "增长率": "30-200%"},
     "case_structure": {"必备要素": ["卖家背景", "产品品类", "市场选择", "运营策略", "业绩数据"], "加分要素": ["选品策略", "物流方案", "品牌化"]}},
    {"name": "AI/SaaS软件", "key": "ai_saas",
     "typical_numbers": {"客单价范围": "500-50000元/年", "续费率": "70-90%", "获客成本": "500-5000元"},
     "case_structure": {"必备要素": ["客户行业", "业务痛点", "产品方案", "使用效果", "ROI数据"], "加分要素": ["对接周期", "团队效率提升", "客户证言"]}},
]

INDUSTRY_MAP = {ind["name"]: ind for ind in INDUSTRIES}

# ========== LLM 调用 ==========

def _call_llm(system_prompt: str, user_prompt: str, enable_search: bool = False) -> str:
    """同步调用 DashScope qwen-max（脚本简单，不需要全异步）"""
    import dashscope
    from dashscope import Generation
    from tools.llm_call_tracker import llm_track_sync, usage_from_response_payload

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")

    params = {
        "api_key": api_key,
        "model": "qwen-max",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "result_format": "message",
        "max_tokens": 4000,
    }
    if enable_search:
        params["enable_search"] = True

    with llm_track_sync(
        caller="industry_case_collector",
        platform="dashscope",
        model=params["model"],
        metadata={"enable_search": enable_search},
    ) as tracker:
        response = Generation.call(**params)
        input_tokens, output_tokens, cached_tokens = usage_from_response_payload(response)
        tracker.record(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            success=getattr(response, "status_code", 0) == 200,
            error_msg=None if getattr(response, "status_code", 0) == 200 else str(getattr(response, "message", ""))[:200],
        )

    if response.status_code != 200:
        raise RuntimeError(f"DashScope 调用失败: {response.code} {response.message}")

    text = response.output.choices[0].message.content

    # 去除 think 标签
    if "<think>" in text:
        import re
        text = re.sub(r'<think>[\s\S]*?</think>', '', text).strip()

    return text


def _extract_json(text: str) -> dict | list:
    """从 LLM 输出中提取 JSON（处理 markdown 代码块）"""
    import re
    # 尝试提取 ```json ... ``` 块
    match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if match:
        text = match.group(1)

    try:
        from json_repair import repair_json
        return json.loads(repair_json(text))
    except Exception:
        return json.loads(text)


# ========== 采集流程 ==========

def search_and_extract_cases(industry_name: str, industry_key: str) -> list[dict]:
    """搜索行业案例并提取关键要素"""
    logger.info(f"  搜索「{industry_name}」案例...")

    system_prompt = f"""你是一个行业案例研究员。你要帮「{industry_name}」行业的**从业者/博主**搜集他们服务客户的真实案例。

## 🚨 角色关系红线（最重要！）
案例中有三个角色：
- **从业者/博主**：做短视频的人，他在讲自己怎么帮客户解决问题
- **客户**：找从业者帮忙的人（张姐、李哥、王总这种）
- **平台/工具**：从业者用的工具（和案例无关，不要提！）

✅ 正确案例视角：「李姐来我们店时法令纹很深，做了3次热玛吉，现在同事说她年轻了10岁」
✅ 正确案例视角：「王总的奶茶店开在学校旁边，开业前3天亏了8000，我帮他调了产品结构，第二个月做到日均380杯」
❌ 错误视角（绝对禁止）：「某机构引入了XX数字化平台，留资率提升70%」
❌ 错误视角（绝对禁止）：「该公司使用了XX SaaS系统后，效率提升50%」

如果搜索结果里找到的是"某公司用了某平台/工具"的供应商营销案例，**丢弃它**，用行业常识重新构造一个从业者服务客户的案例。"""

    user_prompt = f"""请搜索「{industry_name}」行业的客户服务案例。
搜索关键词: "{industry_name} 客户反馈 服务案例 客户故事 真实案例 效果对比"

提取 3-5 个案例，每个案例格式：
```json
[
  {{
    "client_name": "常见姓氏+称呼（张姐、李哥、王总）",
    "client_profile": "有画面感的人物描述（如：35岁二胎妈妈，国企上班，平时加班多没时间护肤）",
    "industry": "{industry_name}",
    "problem": "客户遇到的具体问题（要有场景感，不要抽象概括）",
    "previous_attempt": "之前自己试过什么、花了多少冤枉钱",
    "solution": "从业者怎么帮他解决的（具体动作，不是'提供了解决方案'）",
    "result": "效果要有时间线变化（如：第1个月XX，第3个月XX，半年后XX），不要只写最终结论",
    "numbers": {{
      "investment": "客户花了多少钱",
      "return_value": "得到了什么回报",
      "duration": "分阶段的时间节点（如：第1周/第1个月/第3个月）",
      "improvement": "分阶段的提升数据（不要只有最终数字）"
    }},
    "client_quote": "像微信语音转文字的口语（要有'哈哈''真的''太XX了''说实话'这类口语词，不要书面语）",
    "source_hint": "案例来源提示"
  }}
]
```
要求：
- **角色关系必须正确**：client 是从业者/博主的客户，不是某个平台的客户
- 数字要有**时间线变化**，不是只有最终结论（如"第3个月2.8万，第7个月突破4.2万"）
- client_profile 要有**画面感**（年龄、职业、家庭状况、生活习惯），不要写"一位年轻女性"
- client_quote 要**极度口语化**，像微信聊天不像新闻稿
- 3-5 个案例，质量优先"""

    text = _call_llm(system_prompt, user_prompt, enable_search=True)
    cases = _extract_json(text)

    if isinstance(cases, dict):
        cases = cases.get("cases", [cases])
    if not isinstance(cases, list):
        cases = [cases]

    logger.info(f"  提取到 {len(cases)} 个原始案例")
    return cases


def adapt_case(case: dict, industry_name: str, case_index: int) -> dict:
    """将真实案例脱敏改编"""

    system_prompt = """你是内容改编专家。将案例脱敏改编为短视频脚本素材。
输出严格 JSON 格式，不要多余文字。

## 🚨 角色关系检查（改编前必须先检查）
如果原案例的角色关系不对（写成了"某公司使用了XX平台/系统/工具"），你必须**转换视角**：
- 把"平台/工具/系统"的角色删掉
- 把案例改成"从业者/博主帮自己的客户解决问题"的视角
- 客户是找从业者帮忙的个人或小老板，不是某个大公司

✅ 正确：「张姐来我们店时皮肤暗沉，做了3次光子嫩肤，现在素颜出门都被夸」
❌ 错误：「某机构引入了XX数字化平台后，获客效率提升70%」"""

    user_prompt = f"""将以下「{industry_name}」行业案例进行脱敏改编：
1. 人名改成常见姓氏+称呼（张哥、李姐、王总等）
2. 城市改成同级别但不同的城市
3. 数字在原数基础上 ±15% 范围内微调，保持合理
4. 保留行业特征和因果逻辑不变
5. 去掉任何可识别具体公司/品牌/平台的信息
6. **确保角色关系正确**：client 是博主/从业者的客户，不是某个平台的客户
7. **result 必须有时间线变化**：如"第1个月XX，第3个月XX"，不要只写最终数字
8. **client_quote 必须极度口语化**：像微信语音转文字，要有"哈哈""真的""说实话""太XX了"等口语词，绝不能像新闻稿

原始案例：
{json.dumps(case, ensure_ascii=False, indent=2)}

输出改编后的 JSON（同样格式），额外增加字段：
"is_adapted": true,
"adaptation_note": "基于真实行业案例改编",
"quality_score": 0.0-1.0 的质量评分（角色关系正确性 + 数字时间线 + 语录口语化 + 故事性）"""

    text = _call_llm(system_prompt, user_prompt, enable_search=False)
    adapted = _extract_json(text)

    if not isinstance(adapted, dict):
        adapted = adapted[0] if isinstance(adapted, list) and adapted else case

    # 确保关键字段
    adapted["is_adapted"] = True
    adapted["adaptation_note"] = "基于真实行业案例改编"
    adapted.setdefault("quality_score", 0.7)

    return adapted


def collect_industry(industry_info: dict) -> dict:
    """采集单个行业的完整案例数据"""
    name = industry_info["name"]
    key = industry_info["key"]

    logger.info(f"采集行业: {name} ({key})")

    # 1. 搜索+提取原始案例
    raw_cases = search_and_extract_cases(name, key)

    # 2. 逐个脱敏改编
    adapted_cases = []
    for i, case in enumerate(raw_cases[:5]):  # 最多 5 个
        logger.info(f"  改编案例 {i+1}/{min(len(raw_cases), 5)}...")
        try:
            adapted = adapt_case(case, name, i)
            adapted["id"] = f"{key}_{i+1:03d}"
            adapted_cases.append(adapted)
        except Exception as e:
            logger.warning(f"  改编失败: {e}")
            # 用原始案例兜底
            case["id"] = f"{key}_{i+1:03d}"
            case["is_adapted"] = False
            case["adaptation_note"] = f"改编失败，保留原始: {e}"
            case["quality_score"] = 0.5
            adapted_cases.append(case)

    # 3. 组装输出
    result = {
        "industry": name,
        "industry_key": key,
        "typical_numbers": industry_info.get("typical_numbers", {}),
        "case_structure": industry_info.get("case_structure", {}),
        "cases": adapted_cases,
        "updated_at": datetime.now().strftime("%Y-%m-%d"),
    }

    # 4. 保存
    output_dir = ROOT_DIR / "data" / "industry_cases"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{key}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"  已保存 {len(adapted_cases)} 个案例 → {output_path}")
    return result


# ========== 并发采集 ==========

async def collect_industry_async(industry_info: dict, semaphore: asyncio.Semaphore) -> dict:
    """带信号量限制的异步采集（LLM 调用本身是同步的，用 to_thread 包装）"""
    async with semaphore:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, collect_industry, industry_info)


async def run_concurrent(targets: list[dict], concurrency: int) -> list[dict]:
    """并发采集多个行业"""
    semaphore = asyncio.Semaphore(concurrency)

    async def _safe_collect(ind: dict) -> dict:
        try:
            result = await collect_industry_async(ind, semaphore)
            return {"industry": ind["name"], "cases": len(result["cases"])}
        except Exception as e:
            err_msg = str(e)
            logger.error(f"采集 {ind['name']} 失败: {err_msg}")
            return {"industry": ind["name"], "cases": 0, "error": err_msg}

    return await asyncio.gather(*[_safe_collect(ind) for ind in targets])


# ========== 入口 ==========

def main():
    parser = argparse.ArgumentParser(description="行业案例采集脚本")
    parser.add_argument("--industry", action="append", help="指定行业名称（可多次使用）")
    parser.add_argument("--list", action="store_true", help="列出所有支持的行业")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已有 JSON 文件的行业")
    parser.add_argument("--concurrency", type=int, default=10, help="并发数（默认 10）")
    args = parser.parse_args()

    if args.list:
        for ind in INDUSTRIES:
            output_path = ROOT_DIR / "data" / "industry_cases" / f"{ind['key']}.json"
            exists = "✓" if output_path.exists() else " "
            print(f"  [{exists}] {ind['name']:12s}  ({ind['key']})")
        return

    # 加载 .env
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT_DIR / ".env")
    except ImportError:
        pass

    if not os.environ.get("DASHSCOPE_API_KEY"):
        logger.error("DASHSCOPE_API_KEY 未设置，请检查 .env 文件")
        sys.exit(1)

    # 确定要采集的行业
    if args.industry:
        targets = []
        for name in args.industry:
            if name in INDUSTRY_MAP:
                targets.append(INDUSTRY_MAP[name])
            else:
                logger.error(f"未知行业: {name}，用 --list 查看支持的行业")
                sys.exit(1)
    else:
        targets = list(INDUSTRIES)

    # --skip-existing: 跳过已有文件的行业
    if args.skip_existing:
        output_dir = ROOT_DIR / "data" / "industry_cases"
        before = len(targets)
        targets = [ind for ind in targets if not (output_dir / f"{ind['key']}.json").exists()]
        skipped = before - len(targets)
        if skipped:
            logger.info(f"跳过 {skipped} 个已采集的行业")

    if not targets:
        logger.info("所有行业已采集完毕，无需操作")
        return

    logger.info(f"开始采集 {len(targets)} 个行业（并发={args.concurrency}）...")
    t0 = time.time()

    results = asyncio.run(run_concurrent(targets, args.concurrency))

    elapsed = time.time() - t0
    total_cases = sum(r["cases"] for r in results)

    # 汇总
    logger.info("=" * 50)
    logger.info(f"采集完成！{len(targets)} 个行业，{total_cases} 个案例，耗时 {elapsed:.0f}s")
    for r in results:
        status = f"{r['cases']} 个案例" if r["cases"] > 0 else f"失败: {r.get('error', '?')}"
        logger.info(f"  {r['industry']:12s} → {status}")


if __name__ == "__main__":
    main()
