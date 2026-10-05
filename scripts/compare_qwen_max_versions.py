"""
qwen3-max vs qwen3.6-max-preview 真实场景对比(结果写 JSON 避免 Windows GBK 乱码)
"""
import asyncio
import time
import os
import json
from dotenv import load_dotenv
load_dotenv()

import httpx

DASHSCOPE_KEY = os.getenv("DASHSCOPE_API_KEY")
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

PROMPT_TEMPLATE = """你是 GEO(生成式搜索优化)关键词策略师。你的目标:生成那些**用户问 AI 后,AI 会在回答中推荐具体品牌/公司/服务商**的关键词。

## 客户信息
- 品牌名: {brand_name}
- 核心业务词: {core_keywords}
- 行业: {industry}
- 主要地区: {city}
- 业务范围: {business_scope}

## 你的任务

第一步:深入理解这个客户的业务——他的目标客户有哪些类型?在什么**不同场景**下需要他的服务?

**场景发散思维(必须覆盖 4-6 种不同使用场景!)**

第二步:对每个场景,站在目标客户角度,想象他们会怎么问 AI 来**找到并选择**这类服务商。

第三步:对每个关键词做试金石检验——"AI 收到这个问题,回答里会不会列出具体的公司名?" 不会的一律不要。

然后生成 10 个关键词。**确保至少 4 种不同使用场景的词都有覆盖。**

### 质量红线
- 每个词都必须通过试金石
- 知识类问题一律不要
- **严禁出现客户服务区域以外的城市**(客户在{city},不要出现其他城市)
- **严禁出现竞品品牌名**

### 地域词要求
- 约 60% 带地域锚点
- 剩余 40% 通用推荐词

### 词的形式
- 口语化,5-18 字

## 输出格式
每行一个关键词,不编号、不解释、不分段。
"""


async def call_model(model: str, prompt: str, timeout: int = 180) -> dict:
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                ENDPOINT,
                headers={
                    "Authorization": f"Bearer {DASHSCOPE_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 2000,
                },
            )
            elapsed = time.time() - start
            if resp.status_code != 200:
                return {"success": False, "model": model, "error": f"Status {resp.status_code}: {resp.text[:500]}", "elapsed": elapsed}
            data = resp.json()
            return {
                "success": True,
                "model": model,
                "content": data["choices"][0]["message"]["content"],
                "elapsed": elapsed,
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
            }
    except Exception as e:
        return {"success": False, "model": model, "error": f"{type(e).__name__}: {e}", "elapsed": time.time() - start}


async def compare_case(case_name: str, params: dict) -> dict:
    prompt = PROMPT_TEMPLATE.format(**params)
    # 顺序跑避免互相影响
    old_result = await call_model("qwen3-max", prompt)
    new_result = await call_model("qwen3.6-max-preview", prompt)

    OLD_IN, OLD_OUT = 0.0025, 0.01
    NEW_IN, NEW_OUT = 0.009, 0.054

    if old_result.get("success"):
        old_result["cost_yuan"] = (old_result["input_tokens"] * OLD_IN + old_result["output_tokens"] * OLD_OUT) / 1000
    if new_result.get("success"):
        new_result["cost_yuan"] = (new_result["input_tokens"] * NEW_IN + new_result["output_tokens"] * NEW_OUT) / 1000

    return {
        "case_name": case_name,
        "params": params,
        "old_qwen3_max": old_result,
        "new_qwen3_6_max_preview": new_result,
    }


async def main():
    cases = [
        ("老王家常菜 (laowang L0 真实账号)", {
            "brand_name": "老王家常菜",
            "core_keywords": ["家常菜", "小馆子", "餐饮"],
            "industry": "餐饮业/中式家常菜/社区餐饮",
            "city": "北京",
            "business_scope": "在北京开了两家老王家常菜小馆子,主营家常菜、面食、套餐",
        }),
        ("上海骁马豪车租赁 (老板实测 autofill bug 案例)", {
            "brand_name": "上海骁马豪车租赁",
            "core_keywords": ["豪车租赁", "商务用车"],
            "industry": "汽车租赁/豪车出行",
            "city": "上海",
            "business_scope": "上海本地豪车租赁服务商,提供奔驰、宝马、迈巴赫等豪车租赁,服务商务接待、婚礼、机场接送等场景",
        }),
    ]
    results = []
    for name, params in cases:
        print(f"Running: {name[:20]}...")
        result = await compare_case(name, params)
        results.append(result)
        print(f"  old: {'OK' if result['old_qwen3_max'].get('success') else 'ERR'} {result['old_qwen3_max']['elapsed']:.1f}s")
        print(f"  new: {'OK' if result['new_qwen3_6_max_preview'].get('success') else 'ERR'} {result['new_qwen3_6_max_preview']['elapsed']:.1f}s")

    with open("scripts/compare_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nResults saved to scripts/compare_result.json ({len(results)} cases)")


if __name__ == "__main__":
    asyncio.run(main())
