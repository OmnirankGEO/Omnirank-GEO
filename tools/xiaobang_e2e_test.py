"""
小榜端到端测试脚本(直接调内部函数 · 不通过 HTTP)

跑法: python -m tools.xiaobang_e2e_test
输出: 每个 case 标 [PASS/WARN/FAIL] · 最后汇总

测试维度:
  - preset 命中:必走预设答案,不调 LLM
  - BM25 召回:返回 doc/faq · 看 source_title 是否合理
  - LLM 答案:看是否基于 context、是否有套话/编造
  - 软拒:非业务问题应返回 SOFT_REJECT_TEMPLATE
  - 路由:链接卡 是否指向正确页面
"""

from __future__ import annotations

import asyncio
import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

from api.xiaobang_api import (
    bm25_search, stream_llm, SYSTEM_PROMPT_TPL,
    _ensure_cache_loaded, _CHUNKS_CACHE, BM25_THRESHOLD,
    STRONG_THRESHOLD, _has_business_keyword,
)
from agents.xiaobang_presets import match_preset, SOFT_REJECT_TEMPLATE


# ============================================================
# Test cases · (question, kind, expected_signal)
#   kind: 'preset' / 'business' / 'soft_reject' / 'admin'
#   expected_signal: 关键词 · 答案里必须出现的字眼(LLM 检验用)或 preset key
# ============================================================

CASES: list[tuple[str, str, str | None, str | None]] = [
    # (question, kind, expected_in_answer (支持 '|' 多 signal 任一命中), expected_route_or_preset_key)

    # ============ 预设命中(12 个) ============
    ("你是谁",                    "preset", "小榜",          "who_are_you"),
    ("你叫啥",                    "preset", "小榜",          "who_are_you"),
    ("你是 AI 吗",                "preset", "智能助手",      "are_you_ai"),
    ("你用的什么模型",            "preset", "自研",          "which_model"),
    ("谁开发的",                  "preset", "OmniRank",      "who_developed"),
    ("你能干嘛",                  "preset", "GEO",           "what_can_you_do"),
    ("你好",                      "preset", "你好",          "greeting"),
    ("谢谢",                      "preset", "不客气",        "thanks"),
    ("怎么联系客服",              "preset", "管理员",        "contact_support"),
    ("多少钱",                    "preset", "定价",          "pricing_meta"),
    ("拜拜",                      "preset", "再见",          "bye"),
    ("数据安全吗",                "preset", "数据",          "security"),

    # ============ 业务问题 · 销售线(8 个) ============
    ("怎么发起诊断",              "business", "诊断",                  "/diagnosis/new"),
    ("品牌体检流程是怎样的",      "business", "诊断",                  "/diagnosis/new"),
    ("诊断要多久出报告",          "business", "分钟|出报告",            "/diagnosis/new"),
    ("看不懂诊断报告里的分数",    "business", "分数|总分",              None),
    ("报价三档差在哪",            "business", "档|入门|标准|旗舰",      "/pricing"),
    ("签约后怎么收款",            "business", "签约|收款",              "/pricing"),
    ("客户没看报告是不是没意向",  "business", "意向|线索|跟进",         "/agent/leads"),
    ("我的客户怎么管理",          "business", "客户|管理",              "/my-clients"),

    # ============ 业务问题 · 制作线(6 个) ============
    ("AI 写文章扣多少积分",       "business", "积分|扣费",              "/writing"),
    ("怎么写好客户知识库",        "business", "知识库|资料",            None),  # writing 或 my-clients 都对
    ("发布到自媒体平台要钱吗",    "business", "发布|平台",              "/publish"),
    ("发布失败怎么办",            "business", "失败|重发|退款",          "/publish"),
    ("一篇文章能发到几个平台",    "business", "平台|发布",              "/publish"),
    ("文章写出来怎么改",          "business", "重写|修改",              "/writing"),

    # ============ 业务问题 · 运营线(4 个) ============
    ("监测多久能看效果",          "business", "监测|每天|定时",          "/monitoring"),
    ("监测词不达标怎么办",        "business", "监测|达标",              None),
    ("我加了监测词为啥没数据",    "business", "监测|数据",              "/monitoring"),
    ("生成月报扣多少积分",        "business", "月报|积分",              "/monitoring"),

    # ============ 业务问题 · 账号 + 计费(5 个) ============
    ("扣费失败会退吗",            "business", "退|失败",                "/wallet"),
    ("怎么充值",                  "business", "充值",                  "/wallet"),
    ("我的余额在哪看",            "business", "余额|钱包",              "/wallet"),
    ("怎么改密码",                "business", "密码|修改",              None),
    ("推荐别人能赚多少",          "business", "推荐|佣金|返利",          "/referral"),

    # ============ 业务问题 · 元/概念(3 个) ============
    ("什么是 GEO",                "business", "GEO|生成式",             None),
    ("评分等级是怎么分的",        "business", "等级|领先|空白|分",       None),
    ("4 大 AI 引擎是哪 4 个",      "business", "豆包|通义|Kimi|DeepSeek", None),

    # ============ 软拒(7 个 · 业务无关) ============
    ("今天天气怎么样",            "soft_reject", "OmniRank", None),
    ("帮我写段 Python 代码",      "soft_reject", "OmniRank", None),
    ("讲个笑话",                  "soft_reject", "OmniRank", None),
    ("北京到上海多远",            "soft_reject", "OmniRank", None),
    ("最近股市怎么样",            "soft_reject", "OmniRank", None),
    ("帮我写一封请假邮件",        "soft_reject", "OmniRank", None),
    ("中国人口有多少",            "soft_reject", "OmniRank", None),
]


# ============================================================
# 测试 runner
# ============================================================

async def run_business_case(q: str) -> tuple[str, list[dict], str]:
    """返回 (answer_text, sources, route_or_None) · LLM 实际跑一次"""
    results = bm25_search(q, is_admin=False, top_k=3)
    top_score = results[0][1] if results else 0.0
    if not results or top_score < BM25_THRESHOLD:
        # 应该走软拒分支
        return SOFT_REJECT_TEMPLATE, [], None

    parts = []
    for c, _s in results:
        hdr = f"《{c['source_title']}》"
        if c.get("section_title"):
            hdr += f" · {c['section_title']}"
        parts.append(f"{hdr}\n{c['content'][:600]}")
    context = "\n\n---\n\n".join(parts)
    sp = SYSTEM_PROMPT_TPL.format(context=context, question=q)
    queue, _status = await stream_llm(sp, q)
    full = ""
    while True:
        d = await queue.get()
        if d is None:
            break
        full += d
    # 取 top1 的 route
    route = results[0][0].get("route")
    sources = [results[0][0]]
    return full, sources, route


def assert_signal(answer: str, signal: str) -> bool:
    """signal 支持 '|' 表示多个关键词,任一命中即 PASS"""
    if not signal:
        return True
    a = (answer or "").lower()
    for s in signal.split("|"):
        if s.strip().lower() in a:
            return True
    return False


async def main():
    print("==== 小榜端到端测试 · 30 个 case ====\n")
    _ensure_cache_loaded()
    print(f"KB 池 doc+faq: {len(_CHUNKS_CACHE['non_admin'])} chunks · BM25 阈值: {BM25_THRESHOLD}\n")

    pass_count = 0
    warn_count = 0
    fail_count = 0
    detail_failures: list[str] = []

    for i, (q, kind, signal, expected) in enumerate(CASES, 1):
        verdict = "?"
        notes = ""
        if kind == "preset":
            preset = match_preset(q)
            if not preset:
                verdict, notes = "FAIL", f"预设没命中 · 期望 key={expected}"
            elif preset.key != expected:
                verdict, notes = "WARN", f"命中 {preset.key} ≠ 期望 {expected}"
            else:
                ok = assert_signal(preset.answer, signal or "")
                verdict = "PASS" if ok else "WARN"
                if not ok:
                    notes = f"答案没出现关键词 '{signal}'"
        elif kind == "soft_reject":
            # 先看是否被 preset 误命中
            preset = match_preset(q)
            if preset:
                verdict, notes = "WARN", f"被 preset {preset.key} 抢答(可能是关键词冲突)"
            else:
                # 跟 xiaobang_api.event_stream 的软拒判定逻辑保持一致:
                #   - 分数 < BM25_THRESHOLD                  → 一定软拒
                #   - 分数 < STRONG_THRESHOLD 且 无业务关键词  → 软拒
                results = bm25_search(q, is_admin=False, top_k=1)
                top_score = results[0][1] if results else 0.0
                has_biz_kw = _has_business_keyword(q)
                should_soft_reject = (
                    not results
                    or top_score < BM25_THRESHOLD
                    or (top_score < STRONG_THRESHOLD and not has_biz_kw)
                )
                if should_soft_reject:
                    verdict = "PASS"
                    notes = f"软拒触发 · top_score={top_score:.2f} biz_kw={has_biz_kw}"
                else:
                    verdict, notes = "FAIL", (
                        f"BM25 误召回 top={results[0][0]['source_title']} "
                        f"score={top_score:.2f} biz_kw={has_biz_kw}"
                    )
        elif kind == "business":
            preset = match_preset(q)
            if preset:
                verdict, notes = "WARN", f"业务问题被 preset {preset.key} 抢答"
            else:
                answer, sources, route = await run_business_case(q)
                if not answer or answer == SOFT_REJECT_TEMPLATE:
                    verdict, notes = "FAIL", "BM25 没召回 · 走了软拒"
                elif answer.startswith("(后端未配置") or answer.startswith("(LLM "):
                    verdict, notes = "FAIL", "LLM 调用失败:" + answer[:80]
                else:
                    signal_ok = assert_signal(answer, signal or "")
                    route_ok = (expected is None) or (route == expected)
                    if signal_ok and route_ok:
                        verdict = "PASS"
                        notes = f"路由={route} · 答案 {len(answer)} 字"
                    elif signal_ok:
                        verdict = "WARN"
                        notes = f"答案 OK 但路由={route} ≠ 期望 {expected}"
                    elif route_ok:
                        verdict = "WARN"
                        notes = f"路由 OK 但答案没出现 '{signal}'"
                    else:
                        verdict = "WARN"
                        notes = f"路由 + 关键词都不达标 · 路由={route} 答案前 80:{answer[:80]}"

        if verdict == "PASS":
            pass_count += 1
            print(f"[{i:2d}] [{verdict}] {q}")
        elif verdict == "WARN":
            warn_count += 1
            print(f"[{i:2d}] [{verdict}] {q}  ← {notes}")
        else:
            fail_count += 1
            detail_failures.append(f"[{i:2d}] {q} → {notes}")
            print(f"[{i:2d}] [{verdict}] {q}  ← {notes}")

    print("\n==== 汇总 ====")
    print(f"PASS: {pass_count}  WARN: {warn_count}  FAIL: {fail_count}  total: {len(CASES)}")
    if detail_failures:
        print("\n失败详情:")
        for f in detail_failures:
            print(f"  {f}")


if __name__ == "__main__":
    asyncio.run(main())
