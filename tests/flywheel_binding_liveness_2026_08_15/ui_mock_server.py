"""[P0-2 2026-08-15] 飞轮绑定审核页的确定性 HTTP 夹具(与 PostgreSQL 完全隔离)。

存在的理由:出口闸要求「失败原因在前端**可见**(打真渲染,不是 grep 源码)」+
「toast 全失败必须是非 success 样式,且**能被自动化断言**」。所以让 Playwright 渲染
真实的 Vite 应用 / 鉴权守卫 / 路由 / 页面,而浏览器永不指向真库。

场景开关:`GET /__scenario?name=all_fail|partial_fail|all_ok`
  · all_fail     一键通过返回 approved=0 + failed=4  → 必须 toast.error(非绿勾)+ 明细可见
  · partial_fail approved=2 + failed=1               → 必须 toast.warning
  · all_ok       approved=3 + failed=[]              → toast.success,且**不得**出现明细面板

🔴 字段名与 GeoPlacementFlywheel.tsx 逐字对齐(failed[].media_name / error / industry_key)。
   夹具字段名对不上是最常见的假绿来源:面板渲染出来了但内容全是兜底文案,断言测的不是要测的东西。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCENARIO = {"name": "all_fail"}

#: 与生产实证一一对应的四条(候选 20564/20565/20693/20694 → 库存全部 is_active=f)
FAILED_FOUR = [
    {"candidate_id": 20564, "error": "库存不可采购", "media_name": "扬道财经（csdn博客）",
     "entity_key": "me_9ca2dc5b3fd5", "industry_key": "technology", "media_source": "mhz_wemedia"},
    {"candidate_id": 20565, "error": "库存不可采购", "media_name": "北青网快讯（CSDN)",
     "entity_key": "me_9ca2dc5b3fd5", "industry_key": "finance", "media_source": "mhz_wemedia"},
    {"candidate_id": 20693, "error": "库存不可采购", "media_name": "网易城市（山东）",
     "entity_key": "me_1e7cd914a301", "industry_key": "real_estate", "media_source": "mhz_media"},
    {"candidate_id": 20694, "error": "库存不可采购", "media_name": "网易城市(安徽)",
     "entity_key": "me_1e7cd914a301", "industry_key": "auto", "media_source": "mhz_media"},
]

EMPTY_LIST = {"status": "success", "items": []}


def _approve_all_payload() -> dict:
    name = SCENARIO["name"]
    if name == "all_ok":
        return {"status": "success", "approved_count": 3, "failed_count": 0,
                "approved": [], "failed": [], "selected": 3, "remaining": 0}
    if name == "partial_fail":
        return {"status": "success", "approved_count": 2, "failed_count": 1,
                "approved": [], "failed": FAILED_FOUR[:1], "selected": 3, "remaining": 1}
    return {"status": "success", "approved_count": 0, "failed_count": len(FAILED_FOUR),
            "approved": [], "failed": FAILED_FOUR, "selected": 4, "remaining": 4}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        return

    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/__scenario":
            SCENARIO["name"] = (query.get("name") or ["all_fail"])[0]
            return self._send({"ok": True, "scenario": SCENARIO["name"]})

        if path == "/api/auth/me":
            return self._send({"success": True, "user": {
                "id": 1, "username": "admin-flywheel", "display_name": "飞轮管理员",
                "is_admin": True, "is_active": 1, "must_change_password": 0,
                "roles": [{"id": 1, "name": "admin", "display_name": "系统管理员"}],
                "permissions": ["users.view", "users.edit"],
                "client_brand_ids": [], "agent_level": 0,
            }})

        base = "/api/admin/geo-placement-flywheel"
        if path.startswith(base):
            sub = path[len(base):]
            if sub == "/media/binding-candidates/recommended-count":
                # 场景与选集口径一致:全失败场景下按钮显示 4 条(正是生产现象)
                n = 3 if SCENARIO["name"] == "all_ok" else (3 if SCENARIO["name"] == "partial_fail" else 4)
                return self._send({"status": "success", "recommended_count": n,
                                   "scanned": n, "scan_truncated": False, "min_confidence": 0.9})
            if sub == "/media/coverage":
                return self._send({"status": "success", "coverage": {}})
            if sub.startswith("/health"):
                return self._send({"status": "success", "health": {}})
            if sub.startswith("/answer-adoption/summary"):
                return self._send({"status": "success"})
            if sub.startswith("/media/takeover-gate"):
                return self._send({"status": "success", "ready": False, "production_takeover": False})
            if sub == "/advisory/observability":
                return self._send({"status": "success"})
            if sub.startswith("/writing/article-structure/analyze"):
                return self._send({"status": "success"})
            if sub == "/bridge/health":
                return self._send({"status": "success", "health": {}})
            if sub.startswith("/writing/strategy-active"):
                return self._send({"status": "success", "item": None})
            return self._send(EMPTY_LIST)

        return self._send({"success": True})

    def do_POST(self):  # noqa: N802
        if self.path == "/api/auth/refresh":
            return self._send({"success": True, "token": "playwright-flywheel-token"})
        if self.path.endswith("/media/binding-candidates/approve-all-recommended"):
            return self._send(_approve_all_payload())
        return self._send({"success": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8017), Handler).serve_forever()
