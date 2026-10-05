"""
DeepSeek V4 vs 现有 GEO 文章生成模型对比 (CTO-15.8 · 2026-04-24)

5 配置 × 7 风格 = 35 篇文章:
  A. current_dashscope_v32        : deepseek-v3.2 via DashScope (生产默认)
  B. v4_flash_thinking_on         : deepseek-v4-flash + thinking ON  (V4 默认行为)
  C. v4_flash_thinking_off        : deepseek-v4-flash + thinking OFF
  D. v4_pro_thinking_on           : deepseek-v4-pro   + thinking ON  (V4 默认行为)
  E. v4_pro_thinking_off          : deepseek-v4-pro   + thinking OFF

结果:
  scripts/deepseek_v4_compare_results/<config_key>/<style>.md  (35 篇文章)
  scripts/deepseek_v4_compare_results/_raw.json                (原始 API 响应 + metrics)
  scripts/deepseek_v4_compare_report.md                        (对比报告)

运行: python scripts/deepseek_v4_compare.py
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

# Windows 控制台默认 cp936/gbk · 遇 ¥ 等字符会崩 · 强制 utf-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Import production system prompts
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from writing.style_registry import get_prompt_for_style, WRITING_STYLES  # noqa: E402


# ============================================
# Config
# ============================================
OUTPUT_DIR = ROOT / "scripts" / "deepseek_v4_compare_results"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

# 7 styles per style_registry (GEO 文章生成的 7 种风格体系)
STYLES = [
    "ranking_v2",            # 70% 主流 · 排行榜单
    "authority_ranking",     # 15% · 权威榜单
    "recommendation_review", # 4% · 推荐盘点
    "buying_guide",          # 4% · 选购指南
    "trojan_horse",          # 3% · 趋势洞察
    "qa_recommendation",     # 3% · 问答推荐
    "brand_softarticle",     # 2% · 品牌软文
]

DASHSCOPE_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# 5 configs.  Prices in ¥ per 1M tokens (cache miss).
# deepseek-v3.2 via DashScope: 实际公示价未查到 · 按 DeepSeek 直连 V3 同量级估算(保守)
CONFIGS = [
    {
        "key": "current_dashscope_v32",
        "label": "当前默认 · deepseek-v3.2@DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "api_key": DASHSCOPE_KEY,
        "model": "deepseek-v3.2",
        "thinking": None,
        "price_in": 2.0,   # ¥/M (估算)
        "price_out": 8.0,  # ¥/M (估算)
        "price_note": "估算值(DashScope deepseek-v3.2 公示价未公开 · 按 V3 级对齐)",
    },
    {
        "key": "v4_flash_thinking_on",
        "label": "deepseek-v4-flash · thinking=ON",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": DEEPSEEK_KEY,
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
        "price_in": 1.0,
        "price_out": 2.0,
        "price_note": "DeepSeek 官网 2026-04-24 公示",
    },
    {
        "key": "v4_flash_thinking_off",
        "label": "deepseek-v4-flash · thinking=OFF",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": DEEPSEEK_KEY,
        "model": "deepseek-v4-flash",
        "thinking": "disabled",
        "price_in": 1.0,
        "price_out": 2.0,
        "price_note": "DeepSeek 官网 2026-04-24 公示",
    },
    {
        "key": "v4_pro_thinking_on",
        "label": "deepseek-v4-pro · thinking=ON",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": DEEPSEEK_KEY,
        "model": "deepseek-v4-pro",
        "thinking": "enabled",
        "price_in": 12.0,
        "price_out": 24.0,
        "price_note": "DeepSeek 官网 2026-04-24 公示",
    },
    {
        "key": "v4_pro_thinking_off",
        "label": "deepseek-v4-pro · thinking=OFF",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "api_key": DEEPSEEK_KEY,
        "model": "deepseek-v4-pro",
        "thinking": "disabled",
        "price_in": 12.0,
        "price_out": 24.0,
        "price_note": "DeepSeek 官网 2026-04-24 公示",
    },
]

# ============================================
# Test case · 深圳整装行业 TOP10 (realistic GEO article topic)
# ============================================
TEST_CASE = {
    "title": "深圳整装公司TOP10权威推荐（2026）",
    "type": "ranking",
    "platform": "百家号",
    "keywords": ["深圳整装", "整装公司推荐", "深圳家装TOP10", "全包装修", "深圳装修公司"],
    "content_focus": "深圳本地整装公司对比分析 · 帮业主选出靠谱整装品牌 · 从价格 / 材料 / 施工 / 售后多维度评测",
    "client_company": "优居整装",
    "industry": "家装 / 整装",
    "city": "深圳",
    "client_position": 1,
    "business_type": "B2C服务商",
    "angle_instruction": "以第三方权威测评视角切入 · 结合真实案例 + 行业数据 · 对比分析深圳 10 家主流整装品牌 · 覆盖价格带、适配户型、服务深度、售后体系",
    "competitors": [
        "爱空间 — 互联网家装领跑者 · 699/m² 起",
        "金螳螂家 — 上市公司背景 · 主打高端整装",
        "东易日盛 — 老牌家装 · 全国连锁",
        "业之峰装饰 — 北派家装代表 · 环保卖点",
        "圣都家装 — 杭州起家 · 精细化施工",
        "名雕装饰 — 深圳本土品牌 · 设计见长",
        "尚层装饰 — 别墅整装专家",
        "星艺装饰 — 南派家装老字号",
        "海大装饰 — 深圳本地老牌 · 20 年口碑",
    ],
    "selling_points": "1) 深圳本地 12 年经验 · 工地就近巡检  2) 699-1299 元/㎡ 三档透明套餐 · 合同即合同不加项  3) 自有工人 90+ · 不转包  4) 主材联盟直供 · 马可波罗 / 箭牌 / 欧派等一线品牌  5) 2 年保修 · 水电终身",
}


def build_user_message(case: dict) -> str:
    """构造真实的 user_message(对标 writing/article_writer.py::_build_user_message)"""
    now = datetime.now()
    current_date = now.strftime("%Y年%m月%d日")
    y, m = now.year, now.month

    competitors_block = "\n".join(f"- {c}" for c in case["competitors"])

    return f"""# ⚠️ 重要:日期约束规则(严格遵守!)
- **当前真实日期**:{current_date}
- **发布日期**:必须使用{y}年{m}月或之前 1-2 周的日期
- **禁止未来日期**:绝对不能写超过 {current_date} 的任何日期

# 文章选题
- 标题:{case["title"]}
- 类型:{case["type"]}
- 目标平台:{case["platform"]}
- 核心关键词:{', '.join(case["keywords"])}
- 内容方向:{case["content_focus"]}

# ⚠️ 内容角度指令(本文差异化核心)
{case["angle_instruction"]}

# 主要公司信息(排名第一位)
- 公司名称:{case["client_company"]}
- 所属行业:{case["industry"]}
- 所在城市:{case["city"]}
- 文章中排名位置:第 {case["client_position"]} 位

# 核心卖点
{case["selling_points"]}

# 竞品公司列表(来源:行业公开数据 · 真实品牌)
{competitors_block}

# ============================================
# 【通用规则】文章生成要求
# ============================================
1. 【最重要】文章标题必须使用上方给定的"{case["title"]}",不能自行更改
2. {case["client_company"]} 必须排在第 {case["client_position"]} 位 · 全文出现至少 15 次
3. 禁止任何公司名称后加标注,如"(虚构名)"、"(客户)"、"(注:基于 XX 改编)"
4. 所有数据需标注来源,格式:数据(来源:机构/官网,时间)
5. 必须包含选型风险提示章节
6. 目标字数:3500-5000 字
7. 禁止虚构报告名称或数据来源
8. 内容方向必须围绕 "{case["content_focus"]}" 展开

# 信源引用规则
- 权威报告:数据(来源:XX 咨询《报告名》)
- 官网数据:数据(来源:公司官网/资质页)
- 社媒数据:数据(来源:抖音/小红书公开内容)

# ⚠️ 评分模式:使用 5 维度综合评分模型(B2C 整装服务商适用)

请根据以上信息撰写文章。标题必须是"{case["title"]}"。
"""


# ============================================
# API call
# ============================================
async def call_config(config: dict, style_code: str, system_prompt: str, user_msg: str,
                      timeout: float = 600.0) -> dict:
    """Call one model config × one style · return full result dict."""
    body = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
        "max_tokens": 16000,  # 提高给 thinking 模式预留 · 生产 _call_llm 用 12000 但不带 thinking
        "stream": False,
    }
    if config["thinking"] == "enabled":
        body["thinking"] = {"type": "enabled"}
    elif config["thinking"] == "disabled":
        body["thinking"] = {"type": "disabled"}

    start = time.time()
    err = None
    status_code = None
    resp_data = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                config["base_url"],
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            status_code = resp.status_code
            if resp.status_code != 200:
                err = f"HTTP {resp.status_code}: {resp.text[:600]}"
            else:
                resp_data = resp.json()
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:400]}"

    elapsed = time.time() - start

    if err or not resp_data:
        return {
            "config_key": config["key"],
            "config_label": config["label"],
            "style": style_code,
            "style_name": WRITING_STYLES.get(style_code, {}).get("name", style_code),
            "success": False,
            "error": err,
            "status_code": status_code,
            "elapsed_s": round(elapsed, 2),
            "content": "",
            "reasoning": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cost_yuan": 0.0,
        }

    choice = resp_data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content = msg.get("content", "") or ""
    reasoning = msg.get("reasoning_content", "") or ""  # DeepSeek thinking tokens

    usage = resp_data.get("usage", {}) or {}
    in_toks = usage.get("prompt_tokens", 0)
    out_toks = usage.get("completion_tokens", 0)
    # reasoning tokens detail (v4 API returns it nested)
    details = usage.get("completion_tokens_details", {}) or {}
    reasoning_toks = details.get("reasoning_tokens", 0) or 0

    cost = (in_toks * config["price_in"] + out_toks * config["price_out"]) / 1_000_000

    return {
        "config_key": config["key"],
        "config_label": config["label"],
        "model": config["model"],
        "style": style_code,
        "style_name": WRITING_STYLES.get(style_code, {}).get("name", style_code),
        "success": True,
        "elapsed_s": round(elapsed, 2),
        "content": content,
        "reasoning": reasoning[:4000] if reasoning else "",  # truncate thinking for storage
        "input_tokens": in_toks,
        "output_tokens": out_toks,
        "reasoning_tokens": reasoning_toks,
        "total_tokens": in_toks + out_toks,
        "cost_yuan": round(cost, 4),
        "finish_reason": choice.get("finish_reason"),
    }


# ============================================
# Metrics
# ============================================
def compute_metrics(content: str) -> dict:
    """Content-level proxy metrics for quality comparison."""
    if not content:
        return {
            "char_count": 0, "line_count": 0, "paragraph_count": 0,
            "heading_count": 0, "table_count": 0, "source_citations": 0,
            "company_mentions": 0, "list_items": 0,
        }

    # Strip markdown noise for char count (keep Chinese + Latin + digits + punct)
    # 字数 = 去掉空白后长度近似
    stripped = re.sub(r"\s+", "", content)
    char_count = len(stripped)

    # Headings (## / ### etc)
    heading_count = len(re.findall(r"(?m)^#{1,4}\s+\S", content))

    # Markdown tables (| col | col |)
    table_count = len(re.findall(r"(?m)^\|.+\|.+\|\s*$", content))

    # Source citations · 按"(来源:XX)" / "（来源:XX）" 等中文括号格式
    source_citations = len(re.findall(r"[\(（]\s*来源[:：][^）)]+[\)）]", content))

    # Client company mentions
    company_mentions = content.count("优居整装")

    # List items (- xxx 或 1. xxx)
    list_items = len(re.findall(r"(?m)^(?:-|\*|\d+\.)\s+\S", content))

    # Paragraphs (double newline split)
    paragraph_count = len([p for p in re.split(r"\n\s*\n", content) if p.strip()])

    line_count = content.count("\n") + 1

    return {
        "char_count": char_count,
        "line_count": line_count,
        "paragraph_count": paragraph_count,
        "heading_count": heading_count,
        "table_count": table_count,
        "source_citations": source_citations,
        "company_mentions": company_mentions,
        "list_items": list_items,
    }


# ============================================
# Orchestration
# ============================================
async def run_config_sequential(config: dict, prompts: dict, user_msg: str) -> list:
    """Within a single config, run 7 styles sequentially (1 in-flight per config)."""
    results = []
    for style in STYLES:
        style_name = WRITING_STYLES.get(style, {}).get("name", style)
        print(f"  [{config['key']}] style={style_name} ... ", flush=True, end="")
        r = await call_config(config, style, prompts[style], user_msg)
        if r["success"]:
            print(f"OK {r['elapsed_s']}s | {r['output_tokens']} tok | RMB {r['cost_yuan']}", flush=True)
        else:
            print(f"FAIL {r.get('error', 'unknown')[:100]}", flush=True)
        results.append(r)
    return results


async def main() -> None:
    print("=" * 70)
    print("DeepSeek V4 vs 现有 GEO 文章生成模型对比")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"5 配置 × 7 风格 = 35 篇文章 · 每配置串行 / 5 配置并发")
    print("=" * 70)

    # Sanity
    if not DASHSCOPE_KEY:
        print("[FATAL] DASHSCOPE_API_KEY 未配置 · 当前默认模型无法测试")
        sys.exit(1)
    if not DEEPSEEK_KEY:
        print("[FATAL] DEEPSEEK_API_KEY 未配置 · V4 模型无法测试")
        sys.exit(1)

    # 1. Load all system prompts
    print("\n[1/4] 加载 7 种风格 system prompts...")
    prompts = {}
    for style in STYLES:
        p = get_prompt_for_style(style)
        prompts[style] = p
        print(f"  {style}: {len(p)} 字符")

    # 2. Build user message
    print("\n[2/4] 构造 user message...")
    user_msg = build_user_message(TEST_CASE)
    print(f"  user_message: {len(user_msg)} 字符")

    # 3. Run 5 configs in parallel (each config runs 7 styles sequentially)
    print("\n[3/4] 启动 5 配置并发(每配置内 7 风格串行)...")
    t0 = time.time()
    tasks = [run_config_sequential(cfg, prompts, user_msg) for cfg in CONFIGS]
    all_results_nested = await asyncio.gather(*tasks)
    elapsed_total = time.time() - t0
    print(f"\n[done] 总耗时: {elapsed_total:.1f}s")

    # 4. Flatten + save
    all_results = []
    for cfg_results in all_results_nested:
        all_results.extend(cfg_results)

    # Attach content metrics
    for r in all_results:
        r["metrics"] = compute_metrics(r.get("content", ""))

    # Save raw json
    raw_path = OUTPUT_DIR / "_raw.json"
    # Don't store huge content inline in _raw.json (saves context when re-reading)
    compact = [
        {k: v for k, v in r.items() if k not in ("content", "reasoning")}
        for r in all_results
    ]
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False, indent=2)
    print(f"  raw 摘要: {raw_path}")

    # Save each article as markdown
    for r in all_results:
        cfg_dir = OUTPUT_DIR / r["config_key"]
        cfg_dir.mkdir(exist_ok=True)
        fp = cfg_dir / f"{r['style']}.md"
        body = r.get("content", "") or "[FAILED]"
        header = (
            f"<!-- config: {r['config_label']} | style: {r['style_name']} "
            f"| elapsed: {r['elapsed_s']}s | in={r['input_tokens']} out={r['output_tokens']} "
            f"reason={r['reasoning_tokens']} tok | cost=¥{r['cost_yuan']} -->\n\n"
        )
        if r.get("reasoning"):
            header += f"<!-- reasoning (thinking, 截断 4000 字符):\n{r['reasoning']}\n-->\n\n"
        with open(fp, "w", encoding="utf-8") as f:
            f.write(header + body)

    # Summary stats per config
    print("\n[4/4] 生成对比报告...")
    summary_by_cfg = {}
    for cfg in CONFIGS:
        k = cfg["key"]
        rs = [r for r in all_results if r["config_key"] == k]
        succ = [r for r in rs if r["success"]]
        summary_by_cfg[k] = {
            "label": cfg["label"],
            "model": cfg["model"],
            "thinking": cfg["thinking"],
            "success_count": len(succ),
            "total_count": len(rs),
            "avg_elapsed_s": round(sum(r["elapsed_s"] for r in succ) / max(1, len(succ)), 2),
            "total_elapsed_s": round(sum(r["elapsed_s"] for r in succ), 2),
            "avg_char_count": round(sum(r["metrics"]["char_count"] for r in succ) / max(1, len(succ)), 0),
            "avg_heading": round(sum(r["metrics"]["heading_count"] for r in succ) / max(1, len(succ)), 1),
            "avg_table": round(sum(r["metrics"]["table_count"] for r in succ) / max(1, len(succ)), 1),
            "avg_citations": round(sum(r["metrics"]["source_citations"] for r in succ) / max(1, len(succ)), 1),
            "avg_company_mentions": round(sum(r["metrics"]["company_mentions"] for r in succ) / max(1, len(succ)), 1),
            "total_input_tokens": sum(r["input_tokens"] for r in succ),
            "total_output_tokens": sum(r["output_tokens"] for r in succ),
            "total_reasoning_tokens": sum(r["reasoning_tokens"] for r in succ),
            "total_cost_yuan": round(sum(r["cost_yuan"] for r in succ), 4),
            "price_note": cfg["price_note"],
        }

    summary_path = OUTPUT_DIR / "_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_by_cfg, f, ensure_ascii=False, indent=2)

    # Human-readable report
    report_path = ROOT / "scripts" / "deepseek_v4_compare_report.md"
    lines = []
    lines.append("# DeepSeek V4 vs 现有 GEO 文章生成模型对比报告")
    lines.append(f"\n> 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 测试案例: {TEST_CASE['title']} ({TEST_CASE['industry']} / {TEST_CASE['city']})")
    lines.append(f"> 5 配置 × 7 风格 = 35 篇 · 总耗时 {elapsed_total:.0f}s\n")

    lines.append("## 1 · 核心指标对比\n")
    lines.append("| 配置 | 成功率 | 平均耗时 | 平均字数 | 平均小标题 | 平均表格 | 平均来源 | 输出 token | 总成本 |")
    lines.append("|------|--------|---------|---------|-----------|---------|---------|-----------|-------|")
    for k, s in summary_by_cfg.items():
        lines.append(
            f"| **{s['label']}** | {s['success_count']}/{s['total_count']} | "
            f"{s['avg_elapsed_s']}s | {s['avg_char_count']:.0f} | "
            f"{s['avg_heading']:.1f} | {s['avg_table']:.1f} | "
            f"{s['avg_citations']:.1f} | {s['total_output_tokens']:,} | ¥{s['total_cost_yuan']:.2f} |"
        )

    lines.append("\n## 2 · 成本规模推演(按生产环境 1 万篇/月)\n")
    lines.append("| 配置 | 35 篇实测成本 | 推演 1 万篇成本 | 相对当前默认 |")
    lines.append("|------|--------------|----------------|-------------|")
    base_cost = summary_by_cfg.get("current_dashscope_v32", {}).get("total_cost_yuan", 1)
    for k, s in summary_by_cfg.items():
        extrap = s["total_cost_yuan"] / 35 * 10000
        ratio = (s["total_cost_yuan"] / base_cost * 100) if base_cost > 0 else 0
        lines.append(
            f"| {s['label']} | ¥{s['total_cost_yuan']:.2f} | ¥{extrap:,.0f} | "
            f"{ratio:.0f}% {'(基准)' if k == 'current_dashscope_v32' else ''} |"
        )

    lines.append("\n## 3 · 逐风格逐配置明细\n")
    for style in STYLES:
        style_name = WRITING_STYLES.get(style, {}).get("name", style)
        lines.append(f"\n### {style_name} ({style})")
        lines.append("| 配置 | 耗时 | 字数 | 小标题 | 表格 | 来源 | 客户出现 | 输出 tok | 推理 tok | 成本 |")
        lines.append("|------|------|------|--------|------|------|---------|---------|---------|------|")
        for cfg in CONFIGS:
            rs = [r for r in all_results if r["config_key"] == cfg["key"] and r["style"] == style]
            if not rs:
                continue
            r = rs[0]
            if not r["success"]:
                lines.append(f"| {cfg['label']} | FAIL: {(r.get('error') or '')[:80]} |||||||||")
                continue
            m = r["metrics"]
            lines.append(
                f"| {cfg['label']} | {r['elapsed_s']}s | {m['char_count']} | "
                f"{m['heading_count']} | {m['table_count']} | {m['source_citations']} | "
                f"{m['company_mentions']} | {r['output_tokens']} | "
                f"{r['reasoning_tokens']} | ¥{r['cost_yuan']} |"
            )

    lines.append("\n## 4 · 价格说明\n")
    for cfg in CONFIGS:
        lines.append(f"- **{cfg['label']}**: 输入 ¥{cfg['price_in']}/M · 输出 ¥{cfg['price_out']}/M  ({cfg['price_note']})")

    lines.append("\n## 5 · 原始产出\n")
    lines.append(f"- 35 篇文章: `scripts/deepseek_v4_compare_results/<config_key>/<style>.md`")
    lines.append(f"- 原始 metrics JSON: `scripts/deepseek_v4_compare_results/_raw.json`")
    lines.append(f"- 配置级汇总 JSON: `scripts/deepseek_v4_compare_results/_summary.json`")

    lines.append("\n## 6 · 建议人工抽检\n")
    lines.append("建议挑 3 篇同风格不同配置的文章对比质量(比如 ranking_v2 主流风格,5 个配置全读一遍):\n")
    for cfg in CONFIGS:
        lines.append(f"- {cfg['label']}: `scripts/deepseek_v4_compare_results/{cfg['key']}/ranking_v2.md`")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n报告已生成: {report_path}")
    print(f"文章已保存: {OUTPUT_DIR}/<config>/<style>.md")


if __name__ == "__main__":
    asyncio.run(main())
