"""
小榜长尾验收测试(2026-05-26)

小汤拍板 15 个长尾 case · 不追求全对 · 只确认不出 5 类硬伤:
  1. 乱反问(clarify 选项跟 query 不沾边)
  2. 跳错核心页(链接卡 route 跟意图明显错位)
  3. 越权给 链接卡(越权 query 出了跳转按钮)
  4. 非业务胡答(非业务实体 LLM 编出答案)
  5. 空正文(content 为空且无 clarify)

通过即可推 PR 进灰度。

跑法:
  export XIAOBANG_TEST_TOKEN=eyJ...
  python -m tools.xiaobang_tail_test
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

import httpx

BACKEND_URL = os.getenv("XIAOBANG_TEST_URL", "http://127.0.0.1:8000")
TOKEN = os.getenv("XIAOBANG_TEST_TOKEN", "").strip()


# 15 个长尾 case · (query, kind, route_options_or_None, must_have_link)
# kind: 'business' / 'unauthorized' / 'non_business'
# route_options: 可接受的 route(list), 业务问题至少有一个匹配; None=不要求 route
# must_have_link: True 必出 link · False 必无 link · None 可有可无
TAIL_CASES: list[tuple[str, str, list[str] | None, bool | None]] = [
    # 业务问题(13)· 要有 content 不空 · route 合理 · 必出 link
    ("充值套餐有什么区别",       "business",     ["/wallet", "/feature-pricing"],          True),
    ("赠送积分可以提现吗",       "business",     ["/wallet"],                              True),
    ("充值积分可以提现吗",       "business",     ["/wallet"],                              True),
    ("扣费后任务失败会退吗",     "business",     ["/wallet"],                              True),
    ("诊断报告在哪里看",         "business",     ["/diagnosis", "/diagnosis/new"],         True),
    ("客户看了报告留下手机号在哪看", "business",  ["/agent/leads"],                         True),
    ("我的客户和客户线索区别",   "business",     ["/agent/leads", "/my-clients"],          True),
    ("怎么发布文章",             "business",     ["/publish", "/writing"],                 True),
    ("发布失败会退钱吗",         "business",     ["/publish", "/wallet"],                  True),
    ("监测词怎么添加",           "business",     ["/monitoring"],                          True),
    ("立即测一次扣多少",         "business",     ["/monitoring", "/feature-pricing", "/wallet"], True),
    ("推荐好友返利多久到账",     "business",     ["/referral", "/wallet"],                 True),
    ("怎么修改密码",             "business",     ["/account/profile", "/help/docs/profile"], None),  # FAQ 可能无 link

    # 越权(2)· 必无 链接卡
    ("你能帮我自动发布文章吗",   "unauthorized", None,                                     False),
    # 2026-05-26 review · 补口语化越权 case · 此前漏判返 high+/publish
    ("帮我发文章到自媒体",       "unauthorized", None,                                     False),

    # 非业务(1)· 软拒 · sources 空
    ("朋友怎么样",               "non_business", None,                                     False),
]


async def run_one_sse(client: httpx.AsyncClient, query: str) -> dict:
    """跑一次 SSE · 返回详细结果"""
    result = {
        "http_status": 0,
        "content": "",
        "meta": None,
        "clarify": None,
        "done": False,
        "error": None,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {TOKEN}",
    }
    body = {"message": query, "session_id": "tail_test"}
    try:
        async with client.stream(
            "POST", f"{BACKEND_URL}/api/xiaobang/chat",
            headers=headers, json=body, timeout=60.0,
        ) as resp:
            result["http_status"] = resp.status_code
            if resp.status_code != 200:
                body_text = await resp.aread()
                result["error"] = body_text[:200].decode("utf-8", errors="replace")
                return result
            buffer = ""
            last_event = "message"
            async for line in resp.aiter_lines():
                if not line:
                    if buffer:
                        event_name = last_event
                        data_lines = []
                        for ln in buffer.split("\n"):
                            if ln.startswith("event:"):
                                event_name = ln[6:].strip()
                            elif ln.startswith("data:"):
                                data_lines.append(ln[5:].lstrip())
                        last_event = event_name
                        data_str = "\n".join(data_lines)
                        if data_str:
                            try:
                                data = json.loads(data_str)
                                if event_name == "text":
                                    result["content"] += data.get("delta", "")
                                elif event_name == "meta":
                                    result["meta"] = data
                                elif event_name == "clarify":
                                    result["clarify"] = data
                                elif event_name == "done":
                                    result["done"] = True
                                elif event_name == "error":
                                    result["error"] = data.get("message", "unknown")
                            except json.JSONDecodeError:
                                pass
                        buffer = ""
                    continue
                if line.startswith(":"):
                    continue
                buffer = buffer + "\n" + line if buffer else line
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def find_hard_bugs(query: str, kind: str, route_options: list[str] | None,
                   must_have_link: bool | None, r: dict) -> list[str]:
    """返硬伤列表 · 空 list = OK"""
    bugs: list[str] = []

    # HTTP / 流完整性
    if r["http_status"] != 200:
        bugs.append(f"HTTP {r['http_status']} {r.get('error', '')[:60]}")
        return bugs  # 提前返
    if not r["done"]:
        bugs.append("未收到 done event")

    meta = r.get("meta") or {}
    clarify = r.get("clarify")
    content = r.get("content") or ""
    link = (meta.get("link") or {})

    # 硬伤 5 · 空正文(无 content 且无 clarify)
    if not clarify and not content.strip():
        bugs.append("空正文且无 clarify(用户啥也看不到)")

    # 硬伤 1 · 乱反问 · clarify 选项跟 query 不沾边(标题不含任一 query 字 ≥2)
    if clarify and clarify.get("options"):
        q_lower = query.lower()
        irrelevant = []
        for opt in clarify["options"]:
            title = (opt.get("source_title") or "").lower()
            # 简化:title 跟 query 有 ≥1 个 2-char 子串重叠就算相关
            if not any(title[i:i+2] in q_lower for i in range(len(title) - 1)):
                irrelevant.append(opt.get("source_title"))
        if len(irrelevant) == len(clarify["options"]):
            bugs.append(f"乱反问 · 所有候选都不沾边: {irrelevant}")

    if kind == "business":
        # 硬伤 4 · 非业务胡答不适用 · 但答案不能是错误信息
        bad_signals = ["现在有点忙", "网络好像有点抖", "未配置 deepseek_api_key", "(llm 调用"]
        if any(s in content.lower() for s in bad_signals):
            bugs.append(f"LLM 调用失败遗留文案: {content[:60]}")
        # 硬伤 2 · 跳错核心页
        if must_have_link is True:
            if not link:
                bugs.append("业务问题缺链接卡")
            elif route_options and link.get("route") not in route_options:
                bugs.append(f"路由错位 · 实际 {link.get('route')} 不在 {route_options}")
        elif must_have_link is None and link and route_options:
            if link.get("route") not in route_options:
                bugs.append(f"路由错位 · 实际 {link.get('route')} 不在 {route_options}")
    elif kind == "unauthorized":
        # 硬伤 3 · 越权给 链接卡
        if link:
            bugs.append(f"越权 query 出了链接卡 {link.get('route')}")
        # 内容必须有(解释流程而不是空着)
        if not content.strip():
            bugs.append("越权 query 无 content(应该解释流程)")
    elif kind == "non_business":
        # 硬伤 4 · 非业务胡答
        # 软拒模板的关键词
        soft_signals = ["omnirank", "我只懂", "答不了", "范围"]
        if not any(s in content.lower() for s in soft_signals):
            bugs.append(f"非业务未走软拒 · 答案前 60:{content[:60]}")
        # sources 必须为空(软拒不该带文档来源)
        if meta.get("sources"):
            bugs.append(f"非业务 sources 不空: {[s.get('source_title') for s in meta['sources']]}")
        # 不能有 链接卡
        if link:
            bugs.append(f"非业务出链接卡: {link.get('route')}")

    return bugs


async def main():
    if not TOKEN:
        print("ERROR: XIAOBANG_TEST_TOKEN 未设置")
        return 1
    print(f"=== 小榜长尾验收 · {len(TAIL_CASES)} 个 case ===")
    print(f"Backend: {BACKEND_URL}\n")

    pass_count = 0
    fail_count = 0
    fail_details: list[str] = []

    # 2026-05-26 · case 间 sleep 1s 防 DeepSeek API 限流 + connection 释放
    # 旧版无 sleep · 跑到第 8 个 case 时偶发 SSE 卡死 + 后续 ConnectError
    async with httpx.AsyncClient() as client:
        for i, (query, kind, route_opts, must_link) in enumerate(TAIL_CASES, 1):
            if i > 1:
                await asyncio.sleep(1.0)
            r = await run_one_sse(client, query)
            bugs = find_hard_bugs(query, kind, route_opts, must_link, r)
            meta = r.get("meta") or {}
            link = meta.get("link") or {}
            route = link.get("route", "无")
            conf = meta.get("confidence", "?")
            clarify_n = len((r.get("clarify") or {}).get("options") or []) if r.get("clarify") else 0
            content_len = len(r.get("content") or "")

            status_line = (
                f"[{i:2d}] {kind[:8]:8s} {query[:22]:22s} "
                f"→ route={route:18s} conf={conf:6s} clarify={clarify_n} content={content_len:3d}字"
            )

            if not bugs:
                pass_count += 1
                print(f"[PASS] {status_line}")
            else:
                fail_count += 1
                print(f"[FAIL] {status_line}")
                for b in bugs:
                    print(f"        硬伤: {b}")
                fail_details.append((i, query, bugs, r))

    print()
    print("=" * 60)
    print(f"PASS: {pass_count} / {len(TAIL_CASES)} · FAIL: {fail_count}")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
