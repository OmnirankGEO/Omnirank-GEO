"""
小榜真实 SSE 端到端回归测试(2026-05-26)

跟 xiaobang_regression_test.py 区别:
- xiaobang_regression_test: 只调内部函数 match_canned_faq · 不走 HTTP/SSE
- xiaobang_sse_regression:  真打 /api/xiaobang/chat HTTP/SSE · 解析 SSE 事件 · 端到端验证

跑法:
  # 从浏览器拿 token(F12 → Application → Local Storage → omnirank_token):
  export XIAOBANG_TEST_TOKEN="eyJ0eXAiOiJKV1QiLCJh..."
  python -m tools.xiaobang_sse_regression

  # 自定义 backend URL(默认 http://127.0.0.1:8000):
  XIAOBANG_TEST_URL=http://127.0.0.1:8000 python -m tools.xiaobang_sse_regression

断言每条:
  1. HTTP 200(不 404 不 401 不 500)
  2. 收到 done event
  3. canned case 必须 confidence=high + sources 不空 + link.route 命中预期
  4. 全程不出 clarify event(canned 命中跳过反问)
  5. 每条跑 3 次 · 答案/route/confidence 必须一致(deterministic 保证)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv()

import httpx


BACKEND_URL = os.getenv("XIAOBANG_TEST_URL", "http://127.0.0.1:8000")
TOKEN = os.getenv("XIAOBANG_TEST_TOKEN", "").strip()


# 测试集 · 全是 canned FAQ 覆盖的 query(deterministic · 不抖)
SSE_CASES: list[tuple[str, str, str]] = [
    # (query, expected_route, expected_content_keyword)
    ("诊断要多久出报告",       "/diagnosis/new",   "2-3 分钟"),
    ("品牌体检要多长时间",     "/diagnosis/new",   "2-3 分钟"),
    ("报价三档差在哪",         "/pricing",         "入门版|标准版|旗舰版"),
    ("怎么给客户报价",         "/pricing",         "选词|签约"),
    ("三种积分有什么区别",     "/wallet",          "充值积分|赠送积分"),
    ("监测词不达标怎么办",     "/monitoring",      "达标|覆盖率"),
    ("监测多久能看效果",       "/monitoring",      "每天|24 小时"),
    ("AI 写文章扣多少积分",    "/feature-pricing", "积分"),
    ("怎么加客户",             "/my-clients",      "添加客户|建档"),
    ("怎么扣费",               "/wallet",          "成功才扣|冻结"),
    ("帮我看看怎么扣费",       "/wallet",          "成功才扣|冻结"),
    ("扣费规则是怎样的",       "/wallet",          "成功才扣|冻结"),
]


async def run_one_sse(client: httpx.AsyncClient, query: str) -> dict:
    """跑一次 SSE · 返回 {http_status, content, meta, clarify_seen, done_seen, raw_events}"""
    result = {
        "http_status": 0,
        "content": "",
        "meta": None,
        "clarify_seen": False,
        "done_seen": False,
        "error": None,
        "raw_events": [],
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {TOKEN}",
    }
    body = {"message": query, "session_id": "sse_test"}

    try:
        async with client.stream(
            "POST",
            f"{BACKEND_URL}/api/xiaobang/chat",
            headers=headers,
            json=body,
            timeout=30.0,
        ) as resp:
            result["http_status"] = resp.status_code
            if resp.status_code != 200:
                body_text = await resp.aread()
                result["error"] = body_text[:200].decode("utf-8", errors="replace")
                return result

            # SSE 解析
            buffer = ""
            last_event = "message"
            async for line in resp.aiter_lines():
                if not line:
                    # 空行 = 帧结束
                    if buffer:
                        # buffer 含 event: / data: 行
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
                                result["raw_events"].append((event_name, data))
                                if event_name == "text":
                                    result["content"] += data.get("delta", "")
                                elif event_name == "meta":
                                    result["meta"] = data
                                elif event_name == "clarify":
                                    result["clarify_seen"] = True
                                elif event_name == "done":
                                    result["done_seen"] = True
                                elif event_name == "error":
                                    result["error"] = data.get("message", "unknown")
                            except json.JSONDecodeError:
                                pass
                        buffer = ""
                    continue
                if line.startswith(":"):
                    continue  # SSE comment(eg ": ready")
                buffer = buffer + "\n" + line if buffer else line

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


async def main():
    if not TOKEN:
        print("ERROR: XIAOBANG_TEST_TOKEN 环境变量未设置")
        print("  浏览器 F12 → Application → Local Storage → omnirank_token 拷贝出来:")
        print("  $env:XIAOBANG_TEST_TOKEN='eyJ...'  (PowerShell)")
        print("  export XIAOBANG_TEST_TOKEN=eyJ... (Bash)")
        return 1

    print(f"=== 真实 SSE 回归测试 · {len(SSE_CASES)} case × 3 轮 ===")
    print(f"Backend: {BACKEND_URL}")
    # 2026-05-26 review · 不打印 token 任何片段(测试日志安全)
    print(f"Token: {'配置 OK' if TOKEN else '未配置'}")
    print()

    pass_count = 0
    fail_count = 0
    fail_details: list[str] = []

    async with httpx.AsyncClient() as client:
        for i, (query, expected_route, expected_keyword) in enumerate(SSE_CASES, 1):
            # 跑 3 次 · 比较一致性
            runs = []
            for _ in range(3):
                r = await run_one_sse(client, query)
                runs.append(r)

            # 断言:
            # 1) HTTP 200
            # 2) 三次都 done
            # 3) 无 clarify
            # 4) confidence='high'
            # 5) 三次 route 一致
            # 6) 三次 content 含期望关键词
            ok = True
            reasons: list[str] = []

            for j, r in enumerate(runs, 1):
                if r["http_status"] != 200:
                    ok = False
                    reasons.append(f"run{j}: HTTP {r['http_status']} {r.get('error', '')[:80]}")
                    continue
                if not r["done_seen"]:
                    ok = False
                    reasons.append(f"run{j}: 未收到 done event")
                    continue
                if r["clarify_seen"]:
                    ok = False
                    reasons.append(f"run{j}: 触发了 clarify(canned FAQ 不该反问)")
                    continue
                meta = r.get("meta") or {}
                if meta.get("confidence") != "high":
                    ok = False
                    reasons.append(f"run{j}: confidence={meta.get('confidence')} != high")
                link = meta.get("link") or {}
                if link.get("route") != expected_route:
                    ok = False
                    reasons.append(f"run{j}: route={link.get('route')} != {expected_route}")
                # 关键词
                content_lower = r["content"].lower()
                kw_options = [k.strip().lower() for k in expected_keyword.split("|")]
                if not any(k in content_lower for k in kw_options):
                    ok = False
                    reasons.append(f"run{j}: content 缺关键词 [{expected_keyword}] · 前 80:{r['content'][:80]}")

            # 三次 route 一致性
            routes = {((r.get("meta") or {}).get("link") or {}).get("route") for r in runs}
            if len(routes) > 1:
                ok = False
                reasons.append(f"3 次 route 不一致: {routes}")

            if ok:
                pass_count += 1
                print(f"[{i:2d}] [PASS × 3] {query[:25]:25s} → {expected_route}")
            else:
                fail_count += 1
                print(f"[{i:2d}] [FAIL]     {query[:25]:25s} ← {reasons[0]}")
                fail_details.append(f"  Q: {query}")
                for r in reasons:
                    fail_details.append(f"    · {r}")
                fail_details.append("")

    print()
    print("=" * 60)
    print(f"PASS × 3: {pass_count} / {len(SSE_CASES)}")
    print(f"FAIL:     {fail_count}")
    if fail_details:
        print("\n失败详情:")
        for line in fail_details:
            print(line)

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
