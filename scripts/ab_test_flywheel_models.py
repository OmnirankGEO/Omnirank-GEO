#!/usr/bin/env python3
"""
R14 LLM-first 建档对话引擎 · A/B 模型对比测试

老板诉求 (2026-05-12):
  · 主用 DeepSeek 官方 API · 用 V4
  · 兜底用阿里 DashScope 的 deepseek 模型
  · A/B 测试看哪个更适合建档对话主流

测试方案:
  Model A · deepseek-v4-flash  (阿里 DashScope · 速度优先 · thinking 不开)
  Model B · deepseek-v4-pro    (DeepSeek 官方 · 质量优先 · thinking 开启)

  5 个 test case 模拟典型用户场景:
    1. 敷衍("不好说")              - 看 AI 能否换角度激发
    2. 长答 800+字业务介绍           - 看 AI 能否提取细节 + 不重复问
    3. 用户给金句("做事比客户老公还认真")- 看 AI 能否捕获 quote
    4. 用户疲惫("差不多了 没啥说的")  - 看 AI 能否识别 user_energy
    5. 技术术语回答(GEO 行业黑话)    - 看 AI 能否跟上 + 深挖

输出:
  · 延迟 (秒)
  · JSON 合法性
  · captured_details 数量
  · ai_response 内容(让老板自己判断质量)
  · completeness_score / user_energy / next_move

跑法:
  # 在 prod 跑(有 DEEPSEEK_API_KEY + DASHSCOPE_API_KEY):
  docker exec -i omnirank-blue python /app/scripts/ab_test_flywheel_models.py

  # 本地跑(需 .env 含两个 key):
  python scripts/ab_test_flywheel_models.py

  # 把 stdout 复制粘贴给 Social-CTO-13.0 看
"""

import asyncio
import json
import os
import re
import time
from typing import Any, Optional

import httpx

# ==================================================================
# 模型配置 · 2 个 candidate
# ==================================================================

CANDIDATES = {
    "A_flash_aliyun": {
        "name": "Model A · deepseek-v4-flash (阿里 DashScope)",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "env_key": "DASHSCOPE_API_KEY",
        "model": "deepseek-v4-flash",
        "extra_body": None,
        "timeout": 45.0,
    },
    "B_pro_official_thinking": {
        "name": "Model B · deepseek-v4-pro (DeepSeek 官方 · thinking enabled)",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "env_key": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-pro",
        "extra_body": {"thinking": {"type": "enabled"}},
        "timeout": 180.0,
    },
}

# ==================================================================
# R14 v2 引擎 prompt · 目标驱动 LLM-first 设计
# ==================================================================

SYSTEM_PROMPT_TEMPLATE = """你是 OmniRank 的 IP 操盘手 · 正在跟一位代理深度聊天 · 帮他建立"AI 写稿用的 IP 画像"。

## 你的目标(SSOT · 不允许偏离)
让 AI 写出像用户自己写的视频脚本/文案。
需要积累:业务、客户画像、真实故事、表达风格、差异化、价值观。
- 用户主动说的有用 · 你顺势深挖
- 用户被动答的也算 · 但别问得太机械
- 细枝末节(口头禅、说话节奏、举例方式)比"标准字段"更值钱
- 用户疲了就给"成就感+留出口" · 不强行推进

## 累积理解
{accumulated_understanding}

## 已抓到的用户金句
{quotes}

## 最近对话历史
{history}

## 用户最新回答
{user_input}

## 你这一轮要做 5 件事(像真人操盘手):

1. **细枝末节捕获**:这次答案里有什么值得记下的具体细节?
   (案例数字、口头禅、做事节奏、行业黑话、价值观流露... 任何打开品牌人格的钩子)

2. **完整度自评**:0-100 分 · 现在的累积够不够支撑写一篇"像用户写的稿"?
   你自己判断 · 没有公式

3. **用户表达欲读取**:high / medium / low
   看他答的长度、用词、有没有反问 · 自己判断

4. **决定下一步**:
   - 表达欲 high → 抓住一个最值钱细节深挖(不要换话题)
   - 表达欲 medium → 复述给他成就感 + 顺势抛新角度激发
   - 表达欲 low → 给"今天差不多了" + 留"再聊一点"的选择权

5. **生成回应**:像真人 · 不要 fallback 套话 · 不许说"收到!这块挺清楚的了"

## 输出严格 JSON(不要 markdown 代码块包裹)
{{
  "captured_details": [
    {{"label": "案例数字感", "content": "..."}},
    ...
  ],
  "extracted_quotes": ["...原话..."],
  "completeness_score": 0-100,
  "user_energy": "high|medium|low",
  "next_move": "deepen|pivot|close",
  "ai_response": "下一句要对用户说的话 · 像真人聊天",
  "understanding_update": "50 字以内更新对这个用户的认知"
}}"""

# ==================================================================
# 5 个 test cases
# ==================================================================

TEST_CASES = [
    {
        "id": 1,
        "name": "敷衍回答",
        "context": {
            "accumulated_understanding": "代理做 GEO 优化(AI 搜索优化) · 客户主要是 B 端老板",
            "quotes": [],
            "history": [
                {"role": "assistant", "content": "你平时跟客户聊天是什么风格?有没有常说的口头禅?"},
            ],
            "user_input": "不好说",
        },
        "expect": "识别敷衍 · 换具体角度激发(如举例某次客户对话场景)· 不重问同问题",
    },
    {
        "id": 2,
        "name": "长答业务介绍 800+字",
        "context": {
            "accumulated_understanding": "代理刚开始介绍 · 还没建立明确画像",
            "quotes": [],
            "history": [
                {"role": "assistant", "content": "简单介绍一下你的业务?比如做什么的、主要卖什么?"},
            ],
            "user_input": "我会根据客户的行业来举例子,确保他能听懂,并且说的都是大白话,不会说太多的专业术语。如果说专业术语的话,后面我都会跟上解释。比如说客户会问我们和其他人有什么区别。我会告诉客户,市面上有哪几类的公司在做GEO的业务。第一种是万词霸榜、千词霸榜,他们通常会收客户一万多、两万块钱一年。然后说保证客户上榜。被AI推荐,我通常会告诉客户说里面的底层逻辑,他告诉你现在能上榜,确实现在做的人少,蓝海。他可能发几篇软文。他的品牌可能就上榜了。但是如果后续有其他公司也在做GEO的业务。其他的人用了权重更高的账号或者更好的网站被引用了,那这家公司因为服务周期是一年。他不可能去花钱来砸得更多,因为他也要赚钱。并且还有一个是监测的问题,我们每监测一个关键词,一天就要调用很多次API。每调用一次都是钱。他几百个词、几千个词,调用API一年都不止2万多。会告诉客户非常多的行业现象,然后给他拆解里面符合商业逻辑的和不符合商业逻辑的东西。深圳有一家全屋定制的公司,上个月找我们定了一些词。通过一周的优化,他那边引流来了好几个客户。并且成交了一单,所有的钱全部赚回来了。然后现在不停的跟我推荐客户。还有一家是工厂出海的公司,帮人做代运营的。之前给他打了一个地区的词,但是因为我们的文体非常好,现在被收纳进了全国TOP 1。也有人通过豆包找到了这家公司。",
        },
        "expect": "提取多个细节(讲解风格/万词霸榜对比/API成本/全屋定制案例/工厂出海案例)· 不要丢掉信息 · 抓 1-2 个最值钱的深挖",
    },
    {
        "id": 3,
        "name": "用户给金句",
        "context": {
            "accumulated_understanding": "代理做装修 · 客户多是改善型住房中产",
            "quotes": ["我们师傅干完活会把每颗螺丝都擦干净再走"],
            "history": [
                {"role": "assistant", "content": "客户对你印象最深的一句反馈是什么?"},
            ],
            "user_input": "上个月有个杭州的客户跟我说:你们师傅做事比我老公还认真,这话给我笑死了,我直接发朋友圈。他老婆还在评论区@我说要再来一单。",
        },
        "expect": "捕获金句『师傅做事比我老公还认真』· 识别这是天然好脚本素材 · 顺势挖讲故事场景细节",
    },
    {
        "id": 4,
        "name": "用户疲惫",
        "context": {
            "accumulated_understanding": "代理聊了 8 轮 · 信息已经比较丰富 · 业务/客户/案例/差异化都有",
            "quotes": ["客户问我们多少钱 · 我都先说看你需要什么再算"],
            "history": [
                {"role": "assistant", "content": "你最看重客户身上的什么特质?"},
                {"role": "user", "content": "嗯..."},
                {"role": "assistant", "content": "比如愿意付费的、爱学习的、还是看眼缘的?"},
            ],
            "user_input": "差不多了 没啥可说的 就那样吧",
        },
        "expect": "识别 user_energy=low · 给成就感复述 · 留出'再聊一点'选择权 · 不强行追问",
    },
    {
        "id": 5,
        "name": "技术术语回答",
        "context": {
            "accumulated_understanding": "代理做 GEO/AI 搜索优化",
            "quotes": [],
            "history": [
                {"role": "assistant", "content": "你做 GEO 跟传统 SEO 最大的差别在哪?"},
            ],
            "user_input": "GEO 跟 SEO 最大差别是 Embedding 召回逻辑。SEO 是基于 BM25 + PageRank · GEO 是基于语义相似度 + 实体识别。AI 引擎用 RAG 召回 chunk · 里面有你品牌名 + 行业关键词共现 · 才会被推荐。所以 GEO 不能堆关键词密度 · 要构造高引用价值的内容块。",
        },
        "expect": "跟得上技术语言 · 把术语翻译成营销卖点(如『AI 不看密度看权威 · 一段干货 > 100 句重复』)· 深挖如何转化给客户讲",
    },
]

# ==================================================================
# 跑 AB
# ==================================================================

def build_prompt(case: dict) -> str:
    """填充 system prompt + user 输入"""
    ctx = case["context"]
    history_text = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
        for m in ctx["history"]
    )
    quotes_text = "\n".join(f"- {q}" for q in ctx.get("quotes") or []) or "(暂无)"
    return SYSTEM_PROMPT_TEMPLATE.format(
        accumulated_understanding=ctx["accumulated_understanding"],
        quotes=quotes_text,
        history=history_text or "(首轮)",
        user_input=ctx["user_input"],
    )


def parse_json_safe(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    # 去 markdown 代码块包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        try:
            from json_repair import repair_json
            return json.loads(repair_json(text))
        except Exception:
            return None


async def call_model(candidate_key: str, prompt: str) -> dict:
    """单次调用 · 返 {success, latency, raw, parsed, error, usage}"""
    cfg = CANDIDATES[candidate_key]
    api_key = os.environ.get(cfg["env_key"])
    if not api_key:
        return {
            "success": False,
            "error": f"环境变量 {cfg['env_key']} 未配置",
            "latency": 0,
            "raw": "",
            "parsed": None,
            "usage": {},
        }

    body: dict = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 2000,
    }
    if cfg.get("extra_body"):
        body.update(cfg["extra_body"])

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            resp = await client.post(
                cfg["url"],
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
            latency = time.time() - t0
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
                    "latency": latency,
                    "raw": "",
                    "parsed": None,
                    "usage": {},
                }
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            parsed = parse_json_safe(content)
            return {
                "success": True,
                "latency": latency,
                "raw": content,
                "parsed": parsed,
                "error": None,
                "usage": usage,
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "latency": time.time() - t0,
            "raw": "",
            "parsed": None,
            "usage": {},
        }


def render_case_md(case: dict, results: dict[str, dict]) -> str:
    """单 case markdown 报告"""
    md = []
    md.append(f"## Case {case['id']} · {case['name']}\n")
    md.append(f"**用户输入**: {case['context']['user_input'][:200]}{'...' if len(case['context']['user_input']) > 200 else ''}\n")
    md.append(f"**期望表现**: {case['expect']}\n")
    md.append("")
    md.append("| 维度 | Model A (flash 阿里) | Model B (pro 官方 thinking) |")
    md.append("|---|---|---|")

    a = results["A_flash_aliyun"]
    b = results["B_pro_official_thinking"]

    md.append(f"| 延迟 | {a['latency']:.2f}s | {b['latency']:.2f}s |")
    md.append(f"| 调用成功 | {'✅' if a['success'] else '❌ ' + str(a.get('error', ''))[:50]} | {'✅' if b['success'] else '❌ ' + str(b.get('error', ''))[:50]} |")
    md.append(f"| JSON 合法 | {'✅' if a.get('parsed') else '❌'} | {'✅' if b.get('parsed') else '❌'} |")

    for r, label in [(a, "A"), (b, "B")]:
        if r.get("usage"):
            u = r["usage"]
            md.append(f"| {label} token | in={u.get('prompt_tokens', '?')} out={u.get('completion_tokens', '?')} | — |") if label == "A" else None

    if a.get("parsed"):
        pa = a["parsed"]
        md.append(f"| captured_details 数 | {len(pa.get('captured_details') or [])} | {len(b.get('parsed', {}).get('captured_details') or []) if b.get('parsed') else '—'} |")
        md.append(f"| completeness_score | {pa.get('completeness_score', '?')} | {b.get('parsed', {}).get('completeness_score', '?') if b.get('parsed') else '—'} |")
        md.append(f"| user_energy | {pa.get('user_energy', '?')} | {b.get('parsed', {}).get('user_energy', '?') if b.get('parsed') else '—'} |")
        md.append(f"| next_move | {pa.get('next_move', '?')} | {b.get('parsed', {}).get('next_move', '?') if b.get('parsed') else '—'} |")

    md.append("")
    md.append("### Model A (flash 阿里) 回应")
    md.append("```")
    if a.get("parsed"):
        md.append(f"ai_response: {a['parsed'].get('ai_response', '')}")
        md.append("")
        md.append("captured_details:")
        for d in (a['parsed'].get('captured_details') or [])[:6]:
            md.append(f"  · [{d.get('label', '?')}] {d.get('content', '')[:120]}")
        if a['parsed'].get('extracted_quotes'):
            md.append("")
            md.append("extracted_quotes:")
            for q in a['parsed'].get('extracted_quotes', [])[:4]:
                md.append(f"  · {q}")
    elif a.get("raw"):
        md.append(f"[RAW · JSON parse 失败] {a['raw'][:500]}")
    else:
        md.append(f"[失败] {a.get('error', '')}")
    md.append("```")

    md.append("")
    md.append("### Model B (pro 官方 thinking) 回应")
    md.append("```")
    if b.get("parsed"):
        md.append(f"ai_response: {b['parsed'].get('ai_response', '')}")
        md.append("")
        md.append("captured_details:")
        for d in (b['parsed'].get('captured_details') or [])[:6]:
            md.append(f"  · [{d.get('label', '?')}] {d.get('content', '')[:120]}")
        if b['parsed'].get('extracted_quotes'):
            md.append("")
            md.append("extracted_quotes:")
            for q in b['parsed'].get('extracted_quotes', [])[:4]:
                md.append(f"  · {q}")
    elif b.get("raw"):
        md.append(f"[RAW · JSON parse 失败] {b['raw'][:500]}")
    else:
        md.append(f"[失败] {b.get('error', '')}")
    md.append("```")

    md.append("")
    md.append("---")
    md.append("")
    return "\n".join(md)


def render_summary(all_results: list) -> str:
    """所有 case 汇总"""
    md = []
    md.append("# 📊 汇总")
    md.append("")
    md.append("| Case | A 延迟 | B 延迟 | A success | B success | A JSON | B JSON | A captured | B captured |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    a_lat_sum = 0
    b_lat_sum = 0
    a_ok = b_ok = a_json = b_json = 0
    a_det_sum = b_det_sum = 0
    n = len(all_results)
    for case, results in all_results:
        a = results["A_flash_aliyun"]
        b = results["B_pro_official_thinking"]
        a_lat_sum += a["latency"]
        b_lat_sum += b["latency"]
        if a["success"]: a_ok += 1
        if b["success"]: b_ok += 1
        if a.get("parsed"): a_json += 1
        if b.get("parsed"): b_json += 1
        a_det = len(a.get('parsed', {}).get('captured_details') or []) if a.get('parsed') else 0
        b_det = len(b.get('parsed', {}).get('captured_details') or []) if b.get('parsed') else 0
        a_det_sum += a_det
        b_det_sum += b_det
        md.append(f"| {case['id']} · {case['name'][:10]} | {a['latency']:.1f}s | {b['latency']:.1f}s | {'✅' if a['success'] else '❌'} | {'✅' if b['success'] else '❌'} | {'✅' if a.get('parsed') else '❌'} | {'✅' if b.get('parsed') else '❌'} | {a_det} | {b_det} |")
    md.append(f"| **平均/总计** | **{a_lat_sum/n:.1f}s** | **{b_lat_sum/n:.1f}s** | **{a_ok}/{n}** | **{b_ok}/{n}** | **{a_json}/{n}** | **{b_json}/{n}** | **{a_det_sum}** | **{b_det_sum}** |")
    md.append("")
    md.append("> A 平均延迟低于 5s = 适合主对话(用户能等)")
    md.append("> B 平均延迟高但 captured_details 多 / json 稳 / energy 判断准 = 适合关键判定")
    md.append("> 建议:看完每 case 详情自己判断 · 不只看汇总")
    md.append("")
    return "\n".join(md)


async def main():
    print("# R14 LLM-first 建档对话引擎 · A/B 模型对比报告\n")
    print(f"测试时间:{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    print(f"Model A · `deepseek-v4-flash` (阿里 DashScope · DASHSCOPE_API_KEY)")
    print(f"Model B · `deepseek-v4-pro` (DeepSeek 官方 · DEEPSEEK_API_KEY · thinking enabled)\n")

    # 预 check key
    missing_keys = [cfg["env_key"] for cfg in CANDIDATES.values() if not os.environ.get(cfg["env_key"])]
    if missing_keys:
        print(f"⚠️ 缺失环境变量:{', '.join(set(missing_keys))}")
        print("请确保在 prod docker 内跑 · 或本地 .env 含这两个 key\n")

    all_results = []
    for case in TEST_CASES:
        print(f"🔄 跑 Case {case['id']} · {case['name']}...", flush=True)
        prompt = build_prompt(case)
        # 串行避免限流
        a_result = await call_model("A_flash_aliyun", prompt)
        b_result = await call_model("B_pro_official_thinking", prompt)
        all_results.append((case, {"A_flash_aliyun": a_result, "B_pro_official_thinking": b_result}))
        print(f"   A: {a_result['latency']:.2f}s {'✅' if a_result['success'] else '❌'} · B: {b_result['latency']:.2f}s {'✅' if b_result['success'] else '❌'}", flush=True)

    print("\n" + "=" * 80 + "\n")
    print(render_summary(all_results))
    print("\n" + "=" * 80 + "\n")
    for case, results in all_results:
        print(render_case_md(case, results))


if __name__ == "__main__":
    asyncio.run(main())
