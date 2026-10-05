"""
topic_planning A/B test: qwen3-max(现) vs v4-flash OFF vs v4-flash ON

3 真实调用场景(从生产代码直接 import prompts):
  S1. topic_dispatcher.generate_topics       · 文章选题规划 · 长 prompt 结构化输出
  S2. keyword_topic_generator.generate       · 批量标题生成 · JSON 结构化输出
  S3. distiller.call_llm (CLIENT_PROFILE)    · 画像 JSON 提取 · 小 prompt JSON 严格输出

3 模型配置:
  A. qwen3-max @ DashScope        (现默认 · 基准)
  B. deepseek-v4-flash thinking=OFF   (候选)
  C. deepseek-v4-flash thinking=ON    (reasoning 是否改善 JSON 严谨度?)

3 × 3 = 9 次调用 · 预计 ¥<1 · 5-10 分钟

运行: python scripts/topic_planning_ab_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Import real production prompts
from writing.topic_dispatcher import TOPIC_DISPATCHER_PROMPT  # noqa: E402
from writing.keyword_topic_generator import _build_title_generator_prompt  # noqa: E402
from writing.distiller import CLIENT_PROFILE_PROMPT  # noqa: E402

OUT_DIR = ROOT / "scripts" / "topic_planning_ab_results"
OUT_DIR.mkdir(exist_ok=True, parents=True)

DASHSCOPE_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")

CONFIGS = [
    {
        "key": "qwen3_max",
        "label": "qwen3-max @ DashScope (现默认)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "api_key": DASHSCOPE_KEY,
        "model": "qwen3-max",
        "thinking": None,
        "price_in": 2.5,   # ¥/M
        "price_out": 10.0,  # ¥/M
    },
    {
        "key": "v4_flash_off",
        "label": "deepseek-v4-flash thinking=OFF",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": DEEPSEEK_KEY,
        "model": "deepseek-v4-flash",
        "thinking": "disabled",
        "price_in": 1.0,
        "price_out": 2.0,
    },
    {
        "key": "v4_flash_on",
        "label": "deepseek-v4-flash thinking=ON",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": DEEPSEEK_KEY,
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
        "price_in": 1.0,
        "price_out": 2.0,
    },
]

# ============================================
# Scene 1: topic_dispatcher · 文章选题规划
# ============================================
SCENE1_SYSTEM = TOPIC_DISPATCHER_PROMPT

SCENE1_USER = """品牌名称: 优居整装
行业: 家装 / 整装
城市: 深圳
核心业务: 深圳本地 12 年家装经验 · 主打 699-1299 元/㎡ 三档整装套餐 · 自有工人 90+ 不转包
核心关键词: 深圳整装, 整装公司推荐, 全包装修, 深圳家装TOP10
目标客户: 深圳新房装修刚需业主(80-140㎡ 两室/三室户型为主)· 预算 10-25 万

文章数量分配需求:
- authority(权威测评): 7 篇
- deep_dive(深度对比): 1 篇
- case_study(案例研究): 1 篇
- pitfall(避坑指南): 1 篇

请按 TOPIC_DISPATCHER_PROMPT 的 JSON schema 输出选题列表。"""

SCENE1_PARAMS = {"temperature": 0.5, "max_tokens": 8000}

# ============================================
# Scene 2: keyword_topic_generator · 批量标题生成
# ============================================
SCENE2_SYSTEM = _build_title_generator_prompt()

SCENE2_USER = """品牌名称: 优居整装
行业: 家装 / 整装
城市: 深圳

请为以下关键词生成优化标题(每个关键词生成对应篇数的不同角度标题):

[
  {"keyword": "深圳整装公司推荐", "required_articles": 2, "keyword_id": 1},
  {"keyword": "全包装修报价", "required_articles": 2, "keyword_id": 2},
  {"keyword": "深圳家装避坑", "required_articles": 1, "keyword_id": 3},
  {"keyword": "699元整装是真的吗", "required_articles": 1, "keyword_id": 4}
]
"""

SCENE2_PARAMS = {"temperature": 0.7, "max_tokens": 8000}

# ============================================
# Scene 3: distiller · CLIENT_PROFILE JSON 提取
# ============================================
SCENE3_SYSTEM = CLIENT_PROFILE_PROMPT

SCENE3_USER = """【诊断数据】

品牌: 优居整装
官网: youju-zhuangshi.com
描述: 优居整装成立于 2013 年,专注深圳本地家装市场 12 年,提供 699 元/㎡、999 元/㎡、1299 元/㎡ 三档整装套餐。
主营业务: 整装(含基装 + 主材 + 软装)、局部装修、旧房翻新、别墅整装
服务城市: 深圳(福田 / 罗湖 / 南山 / 宝安 / 龙岗 / 龙华)
目标客户: 新房装修业主 / 二手房翻新业主 · 以 80-140㎡ 户型为主 · 预算集中 10-25 万
团队: 90+ 自有工人 · 不转包 · 15 名项目经理驻场
材料: 马可波罗瓷砖 / 箭牌卫浴 / 欧派橱柜 / 多乐士乳胶漆等一线品牌
案例: 累计 5000+ 深圳家庭装修 · 2025 年交付 400+ 套
服务承诺: 合同即合同不加项 · 2 年保修 · 水电终身

抖音账号: 优居整装深圳官方 · 发布视频 128 条 · 最高单条点赞 8.2k
小红书账号: 优居整装 · 发布笔记 67 篇 · 粉丝 1.4w
主要竞品: 爱空间、金螳螂家、名雕装饰、海大装饰"""

SCENE3_PARAMS = {"temperature": 0.5, "max_tokens": 2000}


SCENES = [
    {"key": "s1_topic_dispatch", "name": "选题规划(文章分配)",
     "system": SCENE1_SYSTEM, "user": SCENE1_USER, "params": SCENE1_PARAMS},
    {"key": "s2_title_batch", "name": "批量标题生成",
     "system": SCENE2_SYSTEM, "user": SCENE2_USER, "params": SCENE2_PARAMS},
    {"key": "s3_client_profile", "name": "客户画像 JSON 提取",
     "system": SCENE3_SYSTEM, "user": SCENE3_USER, "params": SCENE3_PARAMS},
]


async def call_config(cfg: dict, scene: dict, timeout: float = 300.0) -> dict:
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": scene["system"]},
            {"role": "user", "content": scene["user"]},
        ],
        "temperature": scene["params"]["temperature"],
        "max_tokens": scene["params"]["max_tokens"],
        "stream": False,
    }
    if cfg["thinking"] == "enabled":
        body["thinking"] = {"type": "enabled"}
    elif cfg["thinking"] == "disabled":
        body["thinking"] = {"type": "disabled"}

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                cfg["base_url"],
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            elapsed = time.time() - t0
            if resp.status_code != 200:
                return {"config_key": cfg["key"], "scene_key": scene["key"],
                        "success": False, "error": f"HTTP {resp.status_code}: {resp.text[:400]}",
                        "elapsed_s": round(elapsed, 2)}
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content", "") or ""
            reasoning = msg.get("reasoning_content", "") or ""
            usage = data.get("usage", {}) or {}
            in_tok = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
            reason_tok = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
            cost = (in_tok * cfg["price_in"] + out_tok * cfg["price_out"]) / 1_000_000
            return {
                "config_key": cfg["key"], "config_label": cfg["label"],
                "scene_key": scene["key"], "scene_name": scene["name"],
                "success": True,
                "elapsed_s": round(elapsed, 2),
                "content": content,
                "reasoning": reasoning[:2000] if reasoning else "",
                "input_tokens": in_tok, "output_tokens": out_tok, "reasoning_tokens": reason_tok,
                "cost_yuan": round(cost, 4),
            }
    except Exception as e:
        return {"config_key": cfg["key"], "scene_key": scene["key"],
                "success": False, "error": f"{type(e).__name__}: {str(e)[:300]}",
                "elapsed_s": round(time.time() - t0, 2)}


def evaluate_output(scene_key: str, content: str) -> dict:
    """Proxy quality metrics · 按 scene 类型不同指标"""
    m = {"content_len": len(content), "valid_json": False, "json_keys": 0, "list_items": 0}
    if not content:
        return m

    # Try parse JSON (all 3 scenes output JSON)
    try:
        # Extract biggest {} or [] block
        json_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", content)
        if json_match:
            obj = json.loads(json_match.group(1))
            m["valid_json"] = True
            if isinstance(obj, dict):
                m["json_keys"] = len(obj.keys())
                # Count nested list items
                for v in obj.values():
                    if isinstance(v, list):
                        m["list_items"] += len(v)
            elif isinstance(obj, list):
                m["list_items"] = len(obj)
    except Exception:
        pass

    # Scene-specific
    if scene_key == "s1_topic_dispatch":
        # 期望: JSON with "topics" array
        m["has_topics_array"] = '"topics"' in content or "'topics'" in content
    elif scene_key == "s2_title_batch":
        m["title_count"] = len(re.findall(r'"title"\s*:', content))
    elif scene_key == "s3_client_profile":
        # 期望键: company_name / industry / core_business / target_customer / geographic_focus / target_cities / service_keywords / seed_keywords
        required = ["company_name", "industry", "core_business", "target_customer",
                    "geographic_focus", "target_cities", "service_keywords", "seed_keywords"]
        m["required_keys_hit"] = sum(1 for k in required if f'"{k}"' in content)
        m["required_total"] = len(required)

    return m


async def run_scene(scene: dict) -> list:
    """Run 1 scene against 3 configs in parallel"""
    print(f"\n==> [{scene['key']}] {scene['name']}")
    tasks = [call_config(cfg, scene) for cfg in CONFIGS]
    results = await asyncio.gather(*tasks)
    for r in results:
        if r["success"]:
            r["eval"] = evaluate_output(scene["key"], r["content"])
            print(f"    [{r['config_key']:20s}] OK {r['elapsed_s']:6.1f}s | out={r['output_tokens']:5d} | "
                  f"reason={r['reasoning_tokens']:5d} | valid_json={r['eval']['valid_json']} | RMB {r['cost_yuan']}")
        else:
            r["eval"] = {}
            print(f"    [{r['config_key']:20s}] FAIL {r.get('error','')[:100]}")
    return results


async def main():
    print("=" * 70)
    print("topic_planning A/B test · 3 scenes × 3 configs = 9 calls")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    t0 = time.time()
    all_results = []
    for scene in SCENES:
        scene_results = await run_scene(scene)
        all_results.extend(scene_results)

    total_elapsed = time.time() - t0
    print(f"\n[done] 总耗时 {total_elapsed:.1f}s\n")

    # Save raw
    raw_path = OUT_DIR / "_raw.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        compact = [{k: v for k, v in r.items() if k != "reasoning"} for r in all_results]
        json.dump(compact, f, ensure_ascii=False, indent=2)

    # Save each response
    for r in all_results:
        if r["success"]:
            fp = OUT_DIR / f"{r['scene_key']}__{r['config_key']}.md"
            header = (
                f"<!-- scene: {r['scene_name']} | config: {r['config_label']} "
                f"| elapsed: {r['elapsed_s']}s | in={r['input_tokens']} out={r['output_tokens']} "
                f"reason={r['reasoning_tokens']} | cost: RMB {r['cost_yuan']} -->\n\n"
            )
            if r.get("reasoning"):
                header += f"<!-- reasoning (截断):\n{r['reasoning']}\n-->\n\n"
            with open(fp, "w", encoding="utf-8") as f:
                f.write(header + r.get("content", ""))

    # Build report
    lines = []
    lines.append("# topic_planning A/B 测试报告")
    lines.append(f"\n> 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 3 场景 × 3 配置 = 9 次调用 · 总耗时 {total_elapsed:.0f}s\n")

    for scene in SCENES:
        lines.append(f"\n## {scene['name']} ({scene['key']})\n")
        lines.append("| 配置 | 耗时 | 输入 | 输出 | 思考 | 字数 | 有效JSON | 列表项 | 特定指标 | 成本 |")
        lines.append("|------|------|------|------|------|------|---------|--------|---------|------|")
        for cfg in CONFIGS:
            rs = [r for r in all_results if r["config_key"] == cfg["key"] and r["scene_key"] == scene["key"]]
            if not rs:
                continue
            r = rs[0]
            if not r["success"]:
                lines.append(f"| {cfg['label']} | FAIL: {r.get('error','')[:80]} ||||||||")
                continue
            e = r.get("eval", {})
            extra = ""
            if scene["key"] == "s1_topic_dispatch":
                extra = f"has_topics={e.get('has_topics_array')}"
            elif scene["key"] == "s2_title_batch":
                extra = f"title_count={e.get('title_count', 0)}"
            elif scene["key"] == "s3_client_profile":
                extra = f"required_keys={e.get('required_keys_hit', 0)}/{e.get('required_total', 8)}"
            lines.append(
                f"| {cfg['label']} | {r['elapsed_s']}s | {r['input_tokens']} | {r['output_tokens']} | "
                f"{r['reasoning_tokens']} | {e.get('content_len', 0)} | "
                f"{'✅' if e.get('valid_json') else '❌'} | {e.get('list_items', 0)} | {extra} | "
                f"RMB {r['cost_yuan']} |"
            )

    # Summary per config
    lines.append("\n## 配置级汇总(3 场景合并)\n")
    lines.append("| 配置 | 成功 | 总耗时 | 总输入 tok | 总输出 tok | 总思考 tok | 总成本 | 有效JSON 场景 |")
    lines.append("|------|------|--------|-----------|-----------|-----------|--------|---------------|")
    for cfg in CONFIGS:
        rs = [r for r in all_results if r["config_key"] == cfg["key"]]
        succ = [r for r in rs if r["success"]]
        total_in = sum(r["input_tokens"] for r in succ)
        total_out = sum(r["output_tokens"] for r in succ)
        total_reason = sum(r["reasoning_tokens"] for r in succ)
        total_cost = sum(r["cost_yuan"] for r in succ)
        total_elapsed = sum(r["elapsed_s"] for r in succ)
        valid_json = sum(1 for r in succ if r.get("eval", {}).get("valid_json"))
        lines.append(
            f"| {cfg['label']} | {len(succ)}/{len(rs)} | {total_elapsed:.1f}s | "
            f"{total_in:,} | {total_out:,} | {total_reason:,} | RMB {total_cost:.4f} | {valid_json}/3 |"
        )

    lines.append("\n## 产出文件\n")
    lines.append(f"- 每场景每配置响应: `scripts/topic_planning_ab_results/{{scene}}__{{config}}.md`")
    lines.append(f"- 原始 metrics: `scripts/topic_planning_ab_results/_raw.json`")

    report_path = ROOT / "scripts" / "topic_planning_ab_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"报告: {report_path}")
    print(f"响应文件: {OUT_DIR}/*.md ({len([r for r in all_results if r['success']])} 篇)")


if __name__ == "__main__":
    asyncio.run(main())
