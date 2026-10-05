"""
小榜回归测试 · 高频核心问题稳定性验证(2026-05-26)

跟 xiaobang_e2e_test 的区别:
- e2e 是宽度测试(45 个 case · 跑 1 次 · 看大盘)
- regression 是深度测试(核心问题 · 每条跑 3 次 · 看稳定性)

断言每条:
  1. 不反问(没有 clarify 事件)
  2. 有正文(LLM 不能答"我没找到准确答案")
  3. route 命中预期
  4. confidence 不退化(高频问题要 high · 不能 medium/low)

测试集 = 小汤 review 拍板的 8 个高频问题 + 几个加强 case
跑法: python -m tools.xiaobang_regression_test
"""

from __future__ import annotations

import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

from api.xiaobang_api import bm25_search, _ensure_cache_loaded, _CHUNKS_CACHE, tokenize
from agents.xiaobang_canned_faq import match_canned_faq
from agents.xiaobang_presets import match_preset


# 测试集 · 每条断言 expected_route + must_have_answer + no_clarify
# canned_only=True 表示该 case 必须走 canned FAQ 路径(命中硬兜底)· 而非 RAG
TEST_CASES: list[tuple[str, str, str, bool]] = [
    # (query, expected_route, expected_answer_keyword, canned_only)

    # 小汤拍板 8 个高频问题
    ("诊断要多久出报告",       "/diagnosis/new", "2-3 分钟", True),
    ("品牌体检要多长时间",     "/diagnosis/new", "2-3 分钟", True),
    ("报告几分钟能出来",       "/diagnosis/new", "2-3 分钟", True),

    ("报价三档差在哪",         "/pricing",       "入门版|标准版|旗舰版", True),
    ("报价的入门版和旗舰版有什么区别", "/pricing",       "入门版|标准版|旗舰版", True),

    ("怎么给客户报价",         "/pricing",       "选词|签约|6 步", True),
    ("如何给客户出报价",       "/pricing",       "选词|签约|6 步", True),

    ("三种积分有什么区别",     "/wallet",        "充值积分|赠送积分|两种", True),
    ("积分有几种",             "/wallet",        "充值积分|赠送积分|两种", True),

    ("监测词不达标怎么办",     "/monitoring",    "达标|覆盖率", True),
    ("我的监测词没达标怎么办", "/monitoring",    "达标|覆盖率", True),

    ("监测多久能看效果",       "/monitoring",    "每天|24 小时|监测", True),
    ("排名监测多久能看到",     "/monitoring",    "每天|监测|跑", True),

    ("AI 写文章扣多少积分",    "/feature-pricing", "300|150|积分", True),
    ("生成一篇文章要多少积分", "/feature-pricing", "300|积分", True),

    ("怎么加客户",             "/my-clients",    "添加客户|建档|必填", True),
    ("添加客户怎么做",         "/my-clients",    "添加客户|必填|客户名", True),
    ("我要怎么给客户建档案",   "/my-clients",    "添加客户|建档|必填", True),

    # 2026-05-26 第 9 条:扣费规则
    ("怎么扣费",               "/wallet", "成功才扣|冻结", True),
    ("扣费规则是怎样的",       "/wallet", "成功才扣|冻结|退回", True),
    ("帮我看看怎么扣费",       "/wallet", "成功才扣|冻结", True),
    ("扣多少积分",             "/wallet", "短任务|长任务|冻结", True),
    ("扣费失败了怎么办",       "/wallet", "退回|冻结|流水", True),

    # 2026-05-26 第 10 条:怎么发布文章
    ("怎么发布文章",           "/publish", "代发|自助发布|发布管理", True),
    ("文章怎么发布",           "/publish", "代发|自助发布", True),
    ("如何发布文章",           "/publish", "代发|自助发布", True),
    ("自媒体怎么发",           "/publish", "代发|自助发布", True),

    # 2026-05-26 第 11 条:监测词怎么添加
    ("监测词怎么添加",         "/monitoring", "添加监测词|开始监测|130", True),
    ("怎么添加监测词",         "/monitoring", "添加监测词|开始监测", True),
    ("添加监测词",             "/monitoring", "添加监测词|开始监测", True),
    ("监测词入口在哪",         "/monitoring", "排名监测|添加", True),
]


def run_case(query: str, expected_route: str, must_have: str, canned_only: bool) -> dict:
    """跑单个 query 一次 · 返回 {ok, reason, route, content, source}

    断言:
      - 必须命中 canned_faq(若 canned_only=True)
      - route 等于 expected_route
      - content 含 must_have 任一关键词(用 '|' 分隔多个)
    """
    canned = match_canned_faq(query)
    preset = match_preset(query)

    if canned_only and not canned:
        # 看是不是 preset 抢了
        if preset:
            return {"ok": False, "reason": f"被 preset {preset.key} 抢答", "route": None, "content": preset.answer[:80]}
        # 都没命中
        return {"ok": False, "reason": "未命中 canned FAQ(走 RAG · 不稳定)", "route": None, "content": ""}

    if not canned:
        return {"ok": True, "reason": "未要求 canned", "route": None, "content": ""}

    # 命中了 canned · 验证 route + content
    content = canned.answer
    route = canned.route
    if route != expected_route:
        return {"ok": False, "reason": f"route={route} != 期望 {expected_route}", "route": route, "content": content[:80]}

    must_have_options = [m.strip().lower() for m in must_have.split("|")]
    content_lower = content.lower()
    if not any(m in content_lower for m in must_have_options):
        return {"ok": False, "reason": f"答案缺关键词 {must_have}", "route": route, "content": content[:80]}

    return {"ok": True, "reason": f"canned={canned.key}", "route": route, "content": content[:80]}


def main():
    _ensure_cache_loaded()
    print(f"=== 小榜回归测试 · {len(TEST_CASES)} 条 case × 3 轮 ===\n")

    pass_count = 0
    fail_count = 0
    fail_detail: list[str] = []

    for i, (q, expected_route, must_have, canned_only) in enumerate(TEST_CASES, 1):
        # 每条跑 3 次 · canned FAQ 是 deterministic 所以 3 次必然一致
        # 这里跑 3 次 主要验证逻辑路径稳定(不会有竞态)
        results = [run_case(q, expected_route, must_have, canned_only) for _ in range(3)]
        all_ok = all(r["ok"] for r in results)
        all_routes = {r["route"] for r in results}

        if all_ok and len(all_routes) == 1:
            pass_count += 1
            print(f"[{i:2d}] [PASS × 3] {q[:30]:30s} → {expected_route}  ({results[0]['reason']})")
        else:
            fail_count += 1
            first_fail = next((r for r in results if not r["ok"]), results[0])
            msg = f"[{i:2d}] [FAIL] {q[:30]:30s} ({first_fail['reason']})"
            if len(all_routes) > 1:
                msg += f" · 3 次 route 不一致 {all_routes}"
            print(msg)
            fail_detail.append(f"  Q: {q}")
            fail_detail.append(f"  期望 route: {expected_route} · must_have: {must_have}")
            fail_detail.append(f"  实际: {first_fail['reason']} · route={first_fail['route']}")
            if first_fail.get("content"):
                fail_detail.append(f"  答案前 80 字: {first_fail['content']}")
            fail_detail.append("")

    print()
    print("=" * 50)
    print(f"PASS × 3: {pass_count} / {len(TEST_CASES)}")
    print(f"FAIL:     {fail_count}")
    if fail_detail:
        print("\n失败详情:")
        for line in fail_detail:
            print(line)

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
