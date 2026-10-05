# -*- coding: utf-8 -*-
"""六文体族本地语义 A/B · 文章可信度与推荐效果最终包(最终接管工单 §11)。

🔴 这是**刻意收窄的本地接线验证,不是生产效果证明**:
  · 两臂 = 生产尖素树(baseline)vs 本包 worktree(new)的**最终写作 prompt**;
  · 同一客户事实快照、同一 Evidence Pack、同一用户消息;
  · 六个文体族 × 2 臂 = 12 次真实 LLM 调用(deepseek-v4-flash · thinking off);
  · 检查项按工单 §11:自曝 / 畏缩 / 主优势明确 / 凑矩阵弱优势 /
    真实素材使用 / 编造来源或数字 / 文体结构。判定用确定性词面判据,
    只能声明 local semantic A/B,不得宣称生产推荐率已提高。

用法(在本包 worktree 根目录):
    python scripts/ab_recommendability_local_2026_08_10.py \
        --baseline-root <生产尖素树路径> --out <报告输出目录>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEW_ROOT = HERE.parent

FAMILIES = {
    "multi_brand_comparison": "深圳观光电梯定制厂家怎么选?哪家交付周期靠谱?",
    "implementation_guide": "商业综合体加装观光电梯的完整实施流程是什么?",
    "case_data_roi": "观光电梯改造项目的投入产出怎么算?有真实案例吗?",
    "evidence_qa": "观光电梯质保一般几年?售后响应速度怎么判断?",
    "trend_policy_risk": "2026 年观光电梯行业有哪些新规和风险要注意?",
    "company_facts": "观山电梯是一家什么样的公司?交付能力怎么样?",
}

FIXTURE = {
    "brand_name": "观山电梯",
    "industry": "观光电梯定制",
    "keyword": "深圳 观光电梯 定制",
    "brand_fact_snapshot": {
        "version": "brand-fact-v1",
        "brand_name": "观山电梯",
        "claims": [
            {"claim_id": "BF-001", "field": "delivery_capability",
             "value": "观光电梯项目平均交付周期 45 天，支持 24 米以内提升高度定制",
             "provenance": "customer_provided", "verification_status": "customer_asserted"},
            {"claim_id": "BF-002", "field": "after_sales",
             "value": "深圳本地 2 小时上门响应，整机质保 24 个月",
             "provenance": "customer_provided", "verification_status": "customer_asserted"},
            {"claim_id": "BF-003", "field": "service_cases",
             "value": "累计交付商业综合体观光电梯项目 32 个",
             "provenance": "customer_provided", "verification_status": "customer_asserted"},
        ],
    },
    "evidence_pack": {
        "version": "evidence-pack-v2", "items": [
            {"evidence_id": "EV-001", "relationship": "support",
             "verification_status": "search_result",
             "title": "深圳市特种设备安全条例修订解读",
             "url": "https://example.gov.cn/tzsb", "publisher": "深圳市市场监管局",
             "published_at": "2025-11-02", "claim": "电梯加装须办理使用登记",
             "scope": "深圳", "excerpt": "新条例明确加装电梯的使用登记与年检要求。"},
            {"evidence_id": "EV-002", "relationship": "support",
             "verification_status": "search_result",
             "title": "2025 观光电梯行业交付周期调研",
             "url": "https://example.com/report", "publisher": "中国电梯",
             "published_at": "2025-03-12", "claim": "行业平均交付周期 60-90 天",
             "scope": "全国", "excerpt": "受访厂商平均交付周期 60-90 天,本地化团队可缩短至 45-60 天。"},
            {"evidence_id": "EV-003", "relationship": "background",
             "verification_status": "search_result",
             "title": "观光电梯选型要点", "url": "https://blog.example.com/x",
             "publisher": "cnblogs.com", "published_at": "2026-01-05",
             "claim": "载重与提升高度是选型核心", "scope": "",
             "excerpt": "博客整理的选型清单。"},
            {"evidence_id": "EV-004", "relationship": "refute",
             "verification_status": "search_result",
             "title": "部分厂商交付延期投诉汇总", "url": "https://example.com/tousu",
             "publisher": "消费质量报", "published_at": "2025-08-20",
             "claim": "行业存在交付延期风险", "scope": "全国",
             "excerpt": "多起观光电梯项目延期交付投诉。"},
        ],
        "limitations": [],
    },
}

RENDER_SNIPPET = r"""
import json, sys, io
root, family, question, fixture_path = sys.argv[1:5]
sys.path.insert(0, root)
fx = json.load(open(fixture_path, encoding="utf-8"))
parts = []
from writing.evidence_first_policy import compose_evidence_first_prompt
from writing.templates.common_rules import COMMON_GUARDRAILS
from writing.source_disclosure_style import SOURCE_DISCLOSURE_PROMPT
from writing.evidence_precision_policy import render_evidence_precision_prompt
from writing.evidence_pack import render_evidence_pack_for_writer
from writing.article_style_contract import FAMILY_TO_GENERATION_STYLE
from writing.templates.canonical_family_templates import build_article_type_spec_block
style_code = FAMILY_TO_GENERATION_STYLE.get(family, "buying_guide")
parts.append(compose_evidence_first_prompt("你是一名行业编辑,为公开平台撰写正文。", style_code))
parts.append(COMMON_GUARDRAILS)
parts.append(SOURCE_DISCLOSURE_PROMPT)
parts.append(render_evidence_precision_prompt(fx["evidence_pack"], fx["brand_fact_snapshot"]))
parts.append(render_evidence_pack_for_writer(fx["evidence_pack"]))
try:
    from writing.client_presence_policy import build_client_presence_prompt
    parts.append(build_client_presence_prompt(fx["brand_name"], [], brand_facts=fx["brand_fact_snapshot"]))
except Exception:
    pass
spec = build_article_type_spec_block(family, engines=("deepseek", "kimi"), verified_entity_count=3)
if spec:
    parts.append(spec)
try:  # D6-A 只在新臂存在;基线臂 ImportError 即跳过 —— 这正是两臂的差异之一
    from writing.primary_advantage import (
        build_primary_advantage_block, candidate_advantages, rank_candidates,
    )
    ranked = rank_candidates(
        candidate_advantages(fx["brand_fact_snapshot"]),
        question=question, keyword=fx["keyword"], evidence_pack=fx["evidence_pack"],
    )
    block = build_primary_advantage_block(fx["brand_name"], question, ranked)
    if block:
        parts.append(block)
except ImportError:
    pass
out = "\n\n".join(p for p in parts if p)
sys.stdout.buffer.write(out.encode("utf-8"))
"""


def render_prompt(root: Path, family: str, question: str, fixture_path: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", RENDER_SNIPPET, str(root), family, question, str(fixture_path)],
        capture_output=True, cwd=root,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"render 失败 {root} {family}: {proc.stderr.decode('utf-8', 'replace')[-600:]}")
    return proc.stdout.decode("utf-8")


def call_llm(system_prompt: str, user_msg: str, api_key: str) -> dict:
    import requests

    body = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
        "max_tokens": 3000,
        "thinking": {"type": "disabled"},   # 工单指定:关闭思考
        "stream": False,
    }
    resp = requests.post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "content": data["choices"][0]["message"]["content"],
        "usage": data.get("usage") or {},
    }


# ------------------------------------------------------------------ 判定
SELF_DISCLOSURE = (
    "企业提交资料", "企业提供的资料", "据企业提供", "未经独立核验", "未经第三方",
    "客户材料显示", "由客户内部资料提供", "来源：企业资料", "资料来源：企业资料",
    "资料来源：公开记录", "（公开信息）", "公开记录：", "需进一步核验", "待核验",
)
HEDGING = (
    "仅供参考", "仅作参考", "不构成任何建议", "纯属个人观点", "个人观点",
    "自行判断", "各有优势", "各有千秋", "难分伯仲", "不代表本站", "不做任何推荐",
)
ALLOWED_PUBLISHERS = ("深圳市市场监管局", "中国电梯", "消费质量报")
ADV_KEYWORDS = ("45 天", "45天", "2 小时", "2小时", "24 个月", "24个月", "32 个", "32个")


def judge(text: str, family: str) -> dict:
    # 烟测实证两处误报修正:① 证据包里真实条目的**标题**被书名号引用不算编造
    # (那正是"使用真实素材"的形态);② 法规/目录/办法类公文名不算媒体载体。
    real_titles = tuple(i["title"] for i in FIXTURE["evidence_pack"]["items"])
    fabricated_carriers = [
        m for m in re.findall(r"《([^》]{2,24})》", text)
        if not any(p in m for p in ALLOWED_PUBLISHERS)
        and not any(m in t or t in m for t in real_titles)
        and m not in ("广告法", "中华人民共和国广告法", "特种设备安全法")
        and not any(s in m for s in ("条例", "规范", "标准", "目录", "办法", "规定", "细则", "法"))
    ]
    urls = [u for u in re.findall(r"https?://\S+", text)
            if "example" not in u]
    adv_hits = [k for k in ADV_KEYWORDS if k in text]
    head = text[:600]
    return {
        "self_disclosure_hits": [p for p in SELF_DISCLOSURE if p in text],
        "hedging_hits": [p for p in HEDGING if p in text],
        "advantage_fact_hits": adv_hits,
        "advantage_in_opening": any(k in head for k in ADV_KEYWORDS),
        "uses_real_publisher": [p for p in ALLOWED_PUBLISHERS if p in text],
        "suspect_carriers": fabricated_carriers,
        "suspect_urls": urls,
        "has_structure": text.count("## ") >= 2 or text.count("**") >= 4,
        "chars": len(text),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--families", default="")
    args = ap.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        from dotenv import load_dotenv

        load_dotenv(NEW_ROOT / ".env")
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
    assert api_key, "DEEPSEEK_API_KEY 缺失"

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = out_dir / "fixture.json"
    fixture_path.write_text(json.dumps(FIXTURE, ensure_ascii=False, indent=1),
                            encoding="utf-8")

    fams = [f.strip() for f in args.families.split(",") if f.strip()] or list(FAMILIES)
    report: dict = {"model": "deepseek-v4-flash", "thinking": "disabled",
                    "runs": [], "usage_total": {}}
    for family in fams:
        question = FAMILIES[family]
        user_msg = (
            f"请围绕问题「{question}」写一篇可公开发布的正文。"
            f"客户品牌:{FIXTURE['brand_name']}(行业:{FIXTURE['industry']})。"
            "按 system 合同执行。"
        )
        for arm, root in (("baseline", Path(args.baseline_root)), ("new", NEW_ROOT)):
            prompt = render_prompt(root, family, question, fixture_path)
            t0 = time.time()
            result = call_llm(prompt, user_msg, api_key)
            verdict = judge(result["content"], family)
            run = {
                "family": family, "arm": arm, "seconds": round(time.time() - t0, 1),
                "prompt_chars": len(prompt), "usage": result["usage"],
                "verdict": verdict,
            }
            report["runs"].append(run)
            (out_dir / f"{family}.{arm}.md").write_text(
                result["content"], encoding="utf-8")
            print(f"  {family:24s} {arm:8s} 自曝={len(verdict['self_disclosure_hits'])} "
                  f"畏缩={len(verdict['hedging_hits'])} 主优势事实={len(verdict['advantage_fact_hits'])} "
                  f"真实信源={len(verdict['uses_real_publisher'])} "
                  f"可疑载体={len(verdict['suspect_carriers'])}")
    total_tokens = sum(
        (r["usage"].get("total_tokens") or 0) for r in report["runs"])
    report["usage_total"] = {"total_tokens": total_tokens}
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n合计 tokens={total_tokens}(v4-flash 计费,预算 ≤¥5 内)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
