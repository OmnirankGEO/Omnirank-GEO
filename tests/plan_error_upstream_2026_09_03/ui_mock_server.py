"""黄框(`data-testid="plan-error"`)的确定性 HTTP 夹具 —— 与 PostgreSQL 完全隔离。

工单 WO_PLAN_SIDE_MISMATCH_UPSTREAM_2026-09-03 §5/§7 的 **A 类判据**要打的是
"**页面上真的还会不会出现那个黄框**",所以必须让 Playwright 渲染真实的
Vite 应用 / 鉴权守卫 / 路由 / `NewDiagnosis.tsx`,而浏览器永不指向真库。

## 场景开关

    GET /__scenario?name=ok | side_mismatch | plan_identity | question_blank | plan_empty | idem

- `ok`             preview 成功 ⇒ **正常路径**。绿臂断言:黄框不出现。
- 其余             preview 按 §15.8 信封返回对应 code ⇒ 断言"仍被服务端拒绝"那一格
                   (Owner 的口径:黄框只留给自动收敛之后仍被拒的情况,且必须带能做事的按钮)。
- `idem`           `IDEMPOTENCY_CONFLICT` —— 前端把它判为 `isSilent`,
                   **既不弹框也不做任何事**。这一格锁的是"静默 ≠ 什么都不做"。

🔴 字段名与 `frontend/src/lib/defensiveGeoApi.ts` 的 `QuestionPlanResponse` 逐字对齐。
   **夹具字段名对不上是最常见的假绿来源**:面板渲染出来了,但内容全是兜底,
   断言测的不是要测的东西。

🔴 错误 payload 放在 `detail` 里 —— 与 FastAPI `_safe_error` 的形状一致
   (`call()` 只从 `body.detail` 取信封;放错层级会退化成 `INTERNAL_ERROR`,
   于是每个场景都测成了同一条兜底路径)。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8018

SCENARIO = {"name": "ok"}

#: code → (HTTP 状态, 对客句, nextAction)。句子取自 `copy_registry.py` 的口径:
#: 服务端给话、前端只渲染。这里**不新造话**,免得判据在跟自己写的文案对话。
_ERRORS = {
    "side_mismatch": (
        422, "plan_side_mismatch",
        "这次选的体检类型只测一类问题,题单里却混进了另一类。"
        "把多出来的那几道删掉,或者改成混合体检。",
        {"kind": "change_plan", "label": "调整题单", "actionRef": "plan.fix",
         "target": {"side": "offensive"}}),
    "plan_identity": (
        422, "plan_identity",
        "题单的编号对不上了(可能是刚才删改时漏了一步)。回去重新生成一次题单就好。",
        {"kind": "change_plan", "label": "调整题单", "actionRef": "plan.fix", "target": {}}),
    "question_blank": (
        422, "plan_question_blank",
        "有一道题还是空的。把它填上,或者删掉这一行,就能继续。",
        {"kind": "change_plan", "label": "调整题单", "actionRef": "plan.fix",
         "target": {"side": "defensive"}}),
    "plan_empty": (
        422, "plan_empty",
        "这份题单还一道题都没有。先加一道你想测的问题,再开始体检。",
        {"kind": "change_plan", "label": "调整题单", "actionRef": "plan.fix", "target": {}}),
    "idem": (409, "IDEMPOTENCY_CONFLICT", None, None),
}


def _plan_payload(body: dict) -> dict:
    qs = body.get("questions") or []
    counts = {"defensive": 0, "offensive": 0}
    for q in qs:
        side = q.get("modeSide")
        if side in counts:
            counts[side] += 1
    return {
        "planId": "plan_fixture_0903",
        "planRevision": 1,
        "canonicalHash": "h" * 64,
        "canonicalHashVersion": "v1",
        "brandId": int(body.get("brandId") or 1),
        "profileRevisionId": str(body.get("profileRevisionId") or "brand-1"),
        "mode": str(body.get("mode") or "defensive"),
        "modeUserLabel": "混合体检" if body.get("mode") == "hybrid" else "只测客户会问的",
        "questions": [
            {"questionIdentityKey": f"q{i}", "questionRevision": 1, "globalOrdinal": i + 1,
             "text": q.get("text", ""), "modeSide": q.get("modeSide", "defensive"),
             "familyKey": q.get("familyKey", "category_choice"),
             "brandExposure": q.get("brandExposure", "unnamed")}
            for i, q in enumerate(qs)
        ],
        "counts": {**counts, "total": len(qs)},
        "pricingRule": {"basePoints": 650, "freeQuestions": 8,
                        "extraPerQuestion": 100, "ruleVersion": "per-question-v1"},
        "expiresAt": "2030-01-01T00:00:00+08:00",
        "copyRegistryVersion": "fixture",
        "idempotentReplay": False,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):        # 静音
        return

    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):                # noqa: N802
        self.send_response(204)
        for h, v in (("Origin", "*"), ("Headers", "*"), ("Methods", "*")):
            self.send_header(f"Access-Control-Allow-{h}", v)
        self.end_headers()

    def do_GET(self):                    # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/__scenario":
            SCENARIO["name"] = (query.get("name") or ["ok"])[0]
            return self._send({"ok": True, "scenario": SCENARIO["name"]})

        if path == "/api/auth/me":
            return self._send({"success": True, "user": {
                "id": 1, "username": "c14-fixture", "display_name": "题单夹具",
                # 🔴 `is_admin` 保持 False:Owner 截图里是**服务商**账号,
                #    拿 admin 跑判据等于换了一条权限路径,测的不是她那一条。
                "is_admin": False, "is_active": 1, "must_change_password": 0,
                "roles": [{"id": 2, "name": "agent", "display_name": "服务商"}],
                # 🔴 `hasModule('diagnosis')` 读的是 permissions 的**前缀**
                #    (`AuthContext.tsx:865`:`p.startsWith(module + ':')`),不是 roles、
                #    也不是 modules 字段。第一版给了空数组 ⇒ 路由守卫直接渲染
                #    「当前账号没有此页面权限」,页面根本没到表单 ——
                #    **夹具权限给不对,判据测的是权限页,不是被测页面。**
                "permissions": ["diagnosis:view", "diagnosis:create",
                                "brands:view", "brands:edit"],
                "client_brand_ids": [1], "agent_level": 1,
            }})

        if path == "/api/my-clients":
            # 🔴 键名是 `clients`,不是 `brands`(`NewDiagnosis.tsx:346` `data?.clients || []`)。
            #    第一版我写成 `brands` ⇒ 列表恒空 ⇒ 品牌名精确匹配那条(:574 要求
            #    `brandOptions.length > 0`)永远不触发 ⇒ 历史不回填 ⇒ 判据在等一个
            #    **永远不会出现**的说明。本文件开头那句"夹具字段名对不上是最常见的
            #    假绿来源"我自己写完就踩了一次 —— 留着这条注释当活证据。
            return self._send({"success": True, "clients": [
                {"id": 1, "name": "浙江岱林", "industry": "医疗器械", "is_test": False},
                {"id": 2, "name": "全新客户", "industry": "", "is_test": False}]})

        # 🔴 **Owner 那一格的真正触发点。**
        #    `#keywords` 那个 Textarea 只在 legacy(offensive)分支渲染,
        #    defensive 下**根本不在 DOM 里** —— 所以关键词不是她当场敲的,
        #    是品牌名精确匹配后自动选中该品牌、再由本端点**从上一次诊断回填**的
        #    (`NewDiagnosis.tsx:433` `keywords: d.keywords.join('\n')`)。
        #    ⇒ 她只是选了个老客户 + 选了"只测会问",题单就必然混侧。
        #    判据必须走这条路;走"往框里打字"那条会测不到,因为框不存在。
        if path.startswith("/api/brands/") and path.endswith("/latest-diagnosis-params"):
            bid = path.split("/")[3]
            if bid == "1":
                return self._send({
                    "has_diagnosis": True, "industry": "医疗器械",
                    "keywords": ["医疗器械", "生产消杀器械"],
                    "own_accounts": [], "competitors": [], "additional_info": "",
                    "client_location": "浙江", "business_scope": "regional",
                })
            return self._send({"has_diagnosis": False, "keywords": []})

        if path.startswith("/api/my-clients/check-duplicate"):
            return self._send({"success": True, "duplicates": []})

        return self._send({"success": True, "items": [], "data": []})

    def do_POST(self):                   # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}

        if parsed.path == "/api/auth/refresh":
            return self._send({"success": True, "token": "c14-fixture-token"})

        if parsed.path == "/api/defensive-geo/question-plans/preview":
            name = SCENARIO["name"]
            if name in _ERRORS:
                status, code, sentence, action = _ERRORS[name]
                envelope: dict = {"code": code}
                if sentence:
                    envelope["publicExplanation"] = sentence
                if action:
                    envelope["nextAction"] = action
                return self._send({"detail": envelope}, status=status)
            return self._send(_plan_payload(body))

        if parsed.path == "/api/my-clients":
            return self._send({"success": True, "brand": {"id": 1, "name": "浙江岱林"}})

        return self._send({"success": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
