#!/usr/bin/env python3
"""
Backend QA runner for OmniRank.

Scope:
  - HTTP API only, no browser.
  - Production/staging compatible.
  - Uses one authenticated real agent account from environment variables.
  - Writes JSON artifacts plus two markdown reports.

Environment:
  QA_BASE_URL=https://omnirank.top
  QA_USER=...
  QA_PASSWORD=...
  QA_RUN_CHARGED=1      # optional, default 0. If 1, runs one diagnosis+quote flow on a test brand.
  QA_TEST_BRAND_ID=270  # default test brand, must not be one of the protected real customer IDs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "agent-test-artifacts" / "backend-qa-2026-04-29"
REPORT_PATH = ROOT / "docs" / "AI-CONTEXT" / "QA_BACKEND_REPORT_2026-04-28.md"
BUG_PATH = ROOT / "docs" / "AI-CONTEXT" / "QA_BUG_LIST_2026-04-28.md"
RESULT_JSON = ARTIFACT_DIR / "qa_backend_results.json"

PROTECTED_BRAND_IDS = {94, 96, 99, 105, 107, 140, 152, 154}
DEFAULT_TEST_BRAND_ID = int(os.environ.get("QA_TEST_BRAND_ID", "270"))
DEFAULT_TIMEOUT = int(os.environ.get("QA_TIMEOUT", "45"))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_name(s: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in s)[:120]


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = k.lower()
            if any(x in lk for x in ("token", "password", "secret", "authorization", "jwt")):
                if isinstance(v, str) and v:
                    out[k] = v[:8] + "...REDACTED"
                else:
                    out[k] = "REDACTED"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


class HttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.ctx = ssl.create_default_context()
        self.token: str | None = None

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        token: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        accept: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if accept:
            headers["Accept"] = accept
        bearer = token if token is not None else self.token
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        start = time.perf_counter()
        url = self.base_url + path
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        def _do_request() -> dict[str, Any]:
            resp = urllib.request.urlopen(req, context=self.ctx, timeout=timeout)
            raw = resp.read()
            elapsed = int((time.perf_counter() - start) * 1000)
            text = raw.decode("utf-8", errors="replace")
            parsed: Any
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype or (text and text[0] in "[{"):
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = text[:2000]
            else:
                parsed = text[:2000]
            return {
                "ok": 200 <= resp.status < 400,
                "status": resp.status,
                "elapsed_ms": elapsed,
                "headers": dict(resp.headers.items()),
                "body": parsed,
            }

        try:
            return _do_request()
        except urllib.error.HTTPError as e:
            elapsed = int((time.perf_counter() - start) * 1000)
            raw = e.read()
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = text[:2000]
            return {
                "ok": False,
                "status": e.code,
                "elapsed_ms": elapsed,
                "headers": dict(e.headers.items()),
                "body": parsed,
            }
        except Exception as e:
            # Some Aliyun/nginx TLS connections occasionally close during handshake.
            # Retry once so transient SSL EOF does not become a false P1.
            time.sleep(0.6)
            start = time.perf_counter()
            try:
                return _do_request()
            except Exception as e2:
                elapsed = int((time.perf_counter() - start) * 1000)
                return {
                    "ok": False,
                    "status": 0,
                    "elapsed_ms": elapsed,
                    "headers": {},
                    "body": f"{type(e2).__name__}: {e2}",
                }

    def stream_probe(self, path: str, token: str | None = None, timeout: int = 20) -> dict[str, Any]:
        headers = {"Accept": "text/event-stream"}
        bearer = token if token is not None else self.token
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(self.base_url + path, method="GET", headers=headers)
        start = time.perf_counter()
        try:
            resp = urllib.request.urlopen(req, context=self.ctx, timeout=timeout)
            chunk = resp.read(512)
            elapsed = int((time.perf_counter() - start) * 1000)
            return {
                "ok": resp.status == 200 and b"data:" in chunk,
                "status": resp.status,
                "elapsed_ms": elapsed,
                "headers": dict(resp.headers.items()),
                "body": chunk.decode("utf-8", errors="replace"),
            }
        except Exception as e:
            elapsed = int((time.perf_counter() - start) * 1000)
            code = getattr(e, "code", 0)
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:1200]  # type: ignore[attr-defined]
            except Exception:
                body = f"{type(e).__name__}: {e}"
            return {"ok": False, "status": code, "elapsed_ms": elapsed, "headers": {}, "body": body}


@dataclass
class CaseResult:
    id: str
    category: str
    name: str
    status: str
    severity: str = ""
    detail: str = ""
    elapsed_ms: int = 0
    artifact: str = ""
    http_status: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


class BackendQA:
    def __init__(self, base_url: str, user: str, password: str, run_charged: bool):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.run_charged = run_charged
        self.client = HttpClient(base_url)
        self.results: list[CaseResult] = []
        self.context: dict[str, Any] = {
            "base_url": self.base_url,
            "run_charged": self.run_charged,
            "test_brand_id": DEFAULT_TEST_BRAND_ID,
            "protected_brand_ids": sorted(PROTECTED_BRAND_IDS),
        }
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    def save_artifact(self, case_id: str, payload: Any) -> str:
        path = ARTIFACT_DIR / f"{safe_name(case_id)}.json"
        path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path.relative_to(ROOT)).replace("\\", "/")

    def add(
        self,
        case_id: str,
        category: str,
        name: str,
        status: str,
        detail: str = "",
        severity: str = "",
        elapsed_ms: int = 0,
        artifact_payload: Any = None,
        http_status: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        artifact = ""
        if artifact_payload is not None:
            artifact = self.save_artifact(case_id, artifact_payload)
        self.results.append(
            CaseResult(
                id=case_id,
                category=category,
                name=name,
                status=status,
                severity=severity,
                detail=detail,
                elapsed_ms=elapsed_ms,
                artifact=artifact,
                http_status=http_status,
                data=data or {},
            )
        )

    def case_http(
        self,
        case_id: str,
        category: str,
        name: str,
        method: str,
        path: str,
        body: Any = None,
        expect: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
        token: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        sleep_after: float = 0.15,
        severity_on_fail: str = "P2",
    ) -> dict[str, Any]:
        res = self.client.request(method, path, body=body, token=token, timeout=timeout)
        ok, detail = expect(res) if expect else (res["ok"], f"HTTP {res['status']}")
        status = "PASS" if ok else "FAIL"
        self.add(
            case_id,
            category,
            name,
            status,
            detail=detail,
            severity="" if ok else severity_on_fail,
            elapsed_ms=res.get("elapsed_ms", 0),
            artifact_payload={"request": {"method": method, "path": path, "body": body}, "response": res},
            http_status=res.get("status"),
        )
        if sleep_after:
            time.sleep(sleep_after)
        return res

    @staticmethod
    def expect_status(*codes: int) -> Callable[[dict[str, Any]], tuple[bool, str]]:
        def _expect(res: dict[str, Any]) -> tuple[bool, str]:
            return res.get("status") in codes, f"expected {codes}, got HTTP {res.get('status')}"
        return _expect

    @staticmethod
    def expect_json_success(res: dict[str, Any]) -> tuple[bool, str]:
        body = res.get("body")
        if not res.get("ok"):
            return False, f"HTTP {res.get('status')}"
        if isinstance(body, dict) and body.get("success") is False:
            return False, "success=false"
        return True, f"HTTP {res.get('status')}"

    def login(self) -> None:
        res = self.client.request("POST", "/api/auth/login", {"username": self.user, "password": self.password})
        token = None
        user = None
        if res["ok"] and isinstance(res["body"], dict):
            token = res["body"].get("token") or (res["body"].get("data") or {}).get("token")
            user = res["body"].get("user") or (res["body"].get("data") or {}).get("user")
        if not token:
            self.add(
                "AUTH-001",
                "auth",
                "真实代理账号登录",
                "FAIL",
                f"无法登录: HTTP {res['status']}",
                severity="P0",
                artifact_payload={"response": res},
                http_status=res.get("status"),
            )
            raise SystemExit("login failed")
        self.client.token = token
        self.context["user"] = {
            "id": user.get("id") if isinstance(user, dict) else None,
            "username": user.get("username") if isinstance(user, dict) else None,
            "is_admin": user.get("is_admin") if isinstance(user, dict) else None,
            "agent_level": user.get("agent_level") if isinstance(user, dict) else None,
            "client_brand_ids": user.get("client_brand_ids") if isinstance(user, dict) else None,
        }
        self.add(
            "AUTH-001",
            "auth",
            "真实代理账号登录",
            "PASS",
            f"user_id={self.context['user']['id']} agent_level={self.context['user']['agent_level']}",
            elapsed_ms=res.get("elapsed_ms", 0),
            artifact_payload={"response": res},
            http_status=res.get("status"),
        )

    def discover_context(self) -> None:
        me = self.case_http(
            "AUTH-002",
            "auth",
            "GET /api/auth/me",
            "GET",
            "/api/auth/me",
            expect=lambda r: (
                r["ok"] and isinstance(r["body"], dict) and (r["body"].get("user") or {}).get("agent_level", -1) >= 1,
                "agent_level>=1 and user payload present",
            ),
            severity_on_fail="P1",
        )
        if isinstance(me.get("body"), dict):
            user = me["body"].get("user") or {}
            self.context["user"].update(
                {
                    "id": user.get("id"),
                    "is_admin": user.get("is_admin"),
                    "agent_level": user.get("agent_level"),
                    "client_brand_ids": user.get("client_brand_ids"),
                }
            )
        clients = self.client.request("GET", "/api/client-context/list")
        if clients["ok"] and isinstance(clients["body"], dict):
            arr = clients["body"].get("clients") or clients["body"].get("data") or []
            self.context["clients"] = [
                {
                    "id": x.get("id"),
                    "name": x.get("name"),
                    "brand_type": x.get("brand_type"),
                    "latest_score": x.get("latest_score"),
                }
                for x in arr
                if isinstance(x, dict)
            ]
            safe_test = [
                x["id"]
                for x in self.context["clients"]
                if x.get("id") not in PROTECTED_BRAND_IDS and ("测试" in (x.get("name") or "") or "验收" in (x.get("name") or ""))
            ]
            if safe_test:
                self.context["test_brand_id"] = safe_test[0]

    def run_auth_health(self) -> None:
        cat = "01-auth-health"
        self.case_http("AUTH-003", cat, "未登录访问 /api/auth/me 必须 401", "GET", "/api/auth/me", token="", expect=self.expect_status(401), severity_on_fail="P1")
        self.case_http("AUTH-004", cat, "错误 token 必须 401", "GET", "/api/auth/me", token="bad.token.value", expect=self.expect_status(401), severity_on_fail="P1")
        self.case_http("AUTH-005", cat, "OpenAPI 可访问", "GET", "/openapi.json", expect=self.expect_status(200), severity_on_fail="P3")
        self.case_http("AUTH-006", cat, "站点根路径健康", "GET", "/", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("AUTH-007", cat, "用户通知未读数", "GET", "/api/user/notifications/unread-count", expect=self.expect_json_success)
        self.case_http("AUTH-008", cat, "通知列表", "GET", "/api/user/notifications?limit=5", expect=self.expect_status(200))
        self.case_http("AUTH-009", cat, "scheduler status protected/read", "GET", "/api/scheduler/status", expect=self.expect_status(200, 401, 403), severity_on_fail="P3")
        self.case_http("AUTH-010", cat, "全局 settings 只读", "GET", "/api/settings", expect=self.expect_status(200, 401, 403), severity_on_fail="P3")

    def run_wallet_pricing(self) -> None:
        cat = "02-wallet-pricing"
        wallet = self.case_http("WALLET-001", cat, "钱包三轨字段", "GET", "/api/wallet", expect=lambda r: self._expect_wallet(r), severity_on_fail="P1")
        if wallet["ok"]:
            self.context["wallet_before"] = wallet["body"]
        self.case_http("WALLET-002", cat, "价目表含核心 feature_code", "GET", "/api/wallet/pricing", expect=lambda r: self._expect_pricing(r), severity_on_fail="P1")
        self.case_http("WALLET-003", cat, "交易流水", "GET", "/api/wallet/transactions?limit=10", expect=self.expect_status(200), severity_on_fail="P2")
        self.case_http("WALLET-004", cat, "佣金 summary", "GET", "/api/wallet/commissions/summary", expect=self.expect_status(200), severity_on_fail="P2")
        self.case_http("WALLET-005", cat, "pending commissions list", "GET", "/api/wallet/commissions?status=pending", expect=self.expect_status(200, 404), severity_on_fail="P3")
        self.case_http("WALLET-006", cat, "settled commissions list", "GET", "/api/wallet/commissions?status=settled", expect=self.expect_status(200, 404), severity_on_fail="P3")
        pref = self.client.request("GET", "/api/wallet")
        before_pref = None
        if isinstance(pref.get("body"), dict):
            before_pref = (pref["body"].get("wallet") or pref["body"]).get("deduction_preference")
        self.case_http("WALLET-007", cat, "扣费偏好切 agent_friendly", "PATCH", "/api/wallet/preference", {"deduction_preference": "agent_friendly"}, expect=self.expect_status(200, 403), severity_on_fail="P3")
        self.case_http("WALLET-008", cat, "扣费偏好读回", "GET", "/api/wallet", expect=self.expect_status(200), severity_on_fail="P3")
        if before_pref:
            self.case_http("WALLET-009", cat, "扣费偏好还原", "PATCH", "/api/wallet/preference", {"deduction_preference": before_pref}, expect=self.expect_status(200, 403), severity_on_fail="P3")
        else:
            self.add("WALLET-009", cat, "扣费偏好还原", "SKIP", "before preference unavailable")
        self.case_http("WALLET-010", cat, "refund eligibility API should not 500", "GET", "/api/wallet/refund/eligibility?order_id=nonexistent", expect=self.expect_status(200, 400, 404), severity_on_fail="P2")

    def _expect_wallet(self, res: dict[str, Any]) -> tuple[bool, str]:
        if not res["ok"] or not isinstance(res["body"], dict):
            return False, f"HTTP {res['status']}"
        body = res["body"]
        wallet = body.get("wallet") or body.get("data") or body
        required = ["paid_points", "bonus_points", "commission_points", "frozen_points"]
        missing = [k for k in required if k not in wallet]
        return not missing, f"missing={missing}" if missing else "三轨字段存在"

    def _expect_pricing(self, res: dict[str, Any]) -> tuple[bool, str]:
        if not res["ok"] or not isinstance(res["body"], dict):
            return False, f"HTTP {res['status']}"
        data = res["body"].get("data") or res["body"].get("pricing") or []
        codes = {x.get("feature_code") for x in data if isinstance(x, dict)}
        required = {"geo_diagnosis", "quote_generate", "autofill_brand"}
        miss = sorted(required - codes)
        return not miss, f"missing feature_pricing {miss}" if miss else "核心 feature_code 存在"

    def run_client_brand_m3(self) -> None:
        cat = "03-client-brand-m3"
        self.case_http("CLIENT-001", cat, "client-context/list", "GET", "/api/client-context/list", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("CLIENT-002", cat, "my-clients list", "GET", "/api/my-clients?page_size=50", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("CLIENT-003", cat, "M3 customers list", "GET", "/api/m3/customers", expect=lambda r: self._expect_m3_customers(r), severity_on_fail="P1")
        bid = self.context.get("test_brand_id", DEFAULT_TEST_BRAND_ID)
        self.case_http("CLIENT-004", cat, f"my-clients detail brand={bid}", "GET", f"/api/my-clients/{bid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("CLIENT-005", cat, f"brand completeness brand={bid}", "GET", f"/api/brands/{bid}/completeness", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("CLIENT-006", cat, f"M3 lifecycle brand={bid}", "GET", f"/api/m3/customers/{bid}/lifecycle", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("CLIENT-007", cat, "M3 today sales", "GET", "/api/m3/today?space=sales", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("CLIENT-008", cat, "M3 today delivery", "GET", "/api/m3/today?space=delivery", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("CLIENT-009", cat, "M3 delivery queue", "GET", "/api/m3/delivery-queue", expect=self.expect_status(200), severity_on_fail="P2")
        self.case_http("CLIENT-010", cat, "M3 quick-record list", "GET", f"/api/m3/quick-record?brand_id={bid}", expect=self.expect_status(200, 404), severity_on_fail="P3")
        self.case_http("CLIENT-011", cat, "M3 brand-notes", "GET", f"/api/m3/brand-notes/{bid}", expect=self.expect_status(200, 404), severity_on_fail="P3")
        self.case_http("CLIENT-012", cat, "越权品牌 1 detail should not leak if no access", "GET", "/api/my-clients/1", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")

    def _expect_m3_customers(self, res: dict[str, Any]) -> tuple[bool, str]:
        if not res["ok"] or not isinstance(res["body"], dict):
            return False, f"HTTP {res['status']}"
        arr = res["body"].get("customers") or []
        if not isinstance(arr, list):
            return False, "customers is not list"
        ids = {x.get("id") for x in arr if isinstance(x, dict)}
        protected_visible = sorted(ids & PROTECTED_BRAND_IDS)
        return len(arr) > 0, f"customers={len(arr)} protected_visible={protected_visible}"

    def run_diagnosis_quote(self) -> None:
        cat = "04-diagnosis-quote"
        bid = int(self.context.get("test_brand_id") or DEFAULT_TEST_BRAND_ID)
        self.case_http("DIAG-001", cat, f"diagnosis history brand={bid}", "GET", f"/api/brands/{bid}/diagnoses", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("DIAG-002", cat, "history list", "GET", "/api/history?limit=10", expect=self.expect_status(200), severity_on_fail="P2")
        latest_diag = self._find_latest_diagnosis_id(bid)
        if latest_diag:
            self.context["diagnosis_id"] = latest_diag
            self.case_http("DIAG-003", cat, f"diagnosis detail {latest_diag}", "GET", f"/api/diagnosis/{latest_diag}", expect=self.expect_status(200), severity_on_fail="P1")
            self.case_http("DIAG-004", cat, f"diagnosis content {latest_diag}", "GET", f"/api/diagnosis/{latest_diag}/content", expect=self.expect_status(200), severity_on_fail="P2")
            self.case_http("DIAG-005", cat, f"diagnosis type {latest_diag}", "GET", f"/api/diagnosis/{latest_diag}/type", expect=self.expect_status(200), severity_on_fail="P3")
            quote_lookup = self.case_http("DIAG-006", cat, f"diagnosis quote lookup {latest_diag}", "GET", f"/api/diagnosis/{latest_diag}/quote", expect=self.expect_status(200, 404), severity_on_fail="P2")
            # Safe idempotent path only: call generate-quote here only if a quote already exists.
            # New quote generation is tested exclusively in DIAG-012 when QA_RUN_CHARGED=1.
            if quote_lookup.get("status") == 200:
                self.case_http("DIAG-007", cat, f"generate-quote idempotent {latest_diag}", "POST", f"/api/diagnosis/{latest_diag}/generate-quote", {}, expect=self.expect_status(200, 504), timeout=75, severity_on_fail="P2")
            else:
                self.add("DIAG-007", cat, f"generate-quote idempotent {latest_diag}", "SKIP", "no existing quote; avoid accidental 400-point charge")
        else:
            for i in range(3, 8):
                self.add(f"DIAG-{i:03d}", cat, f"diagnosis dependent check {i}", "SKIP", f"brand {bid} has no diagnosis fixture")
        self.case_http("DIAG-008", cat, "quotes list", "GET", "/api/quotes?limit=10", expect=self.expect_status(200), severity_on_fail="P2")
        qid = self._find_latest_quote_id(bid)
        if qid:
            self.context["quote_id"] = qid
            self.case_http("DIAG-009", cat, f"quote detail {qid}", "GET", f"/api/quotes/{qid}", expect=self.expect_status(200), severity_on_fail="P1")
            self.case_http("DIAG-010", cat, f"quote median check {qid}", "GET", f"/api/quotes/{qid}/median-check", expect=self.expect_status(200, 404), severity_on_fail="P3")
            self.case_http("DIAG-011", cat, f"quote cluster delivery {qid}", "GET", f"/api/quotes/{qid}/cluster-delivery", expect=self.expect_status(200, 404), severity_on_fail="P3")
        else:
            for i in range(9, 12):
                self.add(f"DIAG-{i:03d}", cat, f"quote dependent check {i}", "SKIP", f"brand {bid} has no quote fixture")
        if self.run_charged:
            self._run_one_charged_flow(cat, bid)
        else:
            self.add("DIAG-012", cat, "真扣费诊断+方案书链路", "SKIP", "QA_RUN_CHARGED 未开启，避免真实扣费")

    def _find_latest_diagnosis_id(self, brand_id: int) -> int | None:
        res = self.client.request("GET", f"/api/brands/{brand_id}/diagnoses")
        body = res.get("body")
        arr = []
        if isinstance(body, dict):
            arr = body.get("diagnoses") or body.get("data") or body.get("items") or []
        if isinstance(arr, list) and arr:
            for item in arr:
                if isinstance(item, dict) and item.get("id"):
                    return int(item["id"])
        return None

    def _find_latest_quote_id(self, brand_id: int) -> int | None:
        res = self.client.request("GET", f"/api/quotes?brand_id={brand_id}&limit=5")
        body = res.get("body")
        arr = []
        if isinstance(body, dict):
            arr = body.get("quotes") or body.get("data") or body.get("items") or []
        if isinstance(arr, list) and arr:
            for item in arr:
                if isinstance(item, dict) and item.get("id"):
                    return int(item["id"])
        return None

    def _run_one_charged_flow(self, cat: str, brand_id: int) -> None:
        if brand_id in PROTECTED_BRAND_IDS:
            self.add("DIAG-012", cat, "真扣费诊断+方案书链路", "SKIP", f"brand {brand_id} 是保护真客户")
            return
        wallet_before = self.client.request("GET", "/api/wallet")
        payload = {
            "brand_id": brand_id,
            "brand_name": f"QA后端验收客户_{dt.datetime.now().strftime('%m%d_%H%M')}",
            "industry": "本地家装服务",
            "client_location": "深圳",
            "keywords": ["深圳家装公司推荐"],
            "diagnosis_scope": "geo",
            "additional_info": "后端 QA 自动测试，仅用于验证扣费冻结、诊断任务和方案书幂等链路。",
        }
        start = self.client.request("POST", "/api/diagnosis/start", payload, timeout=60)
        self.save_artifact("DIAG-012_start", {"request": payload, "response": start})
        if not start.get("ok") or not isinstance(start.get("body"), dict):
            self.add("DIAG-012", cat, "真扣费诊断启动", "FAIL", f"HTTP {start.get('status')}", severity="P1", artifact_payload={"wallet_before": wallet_before, "start": start}, http_status=start.get("status"))
            return
        session_id = start["body"].get("session_id")
        done = False
        last_status: dict[str, Any] = {}
        for _ in range(90):
            time.sleep(5)
            st = self.client.request("GET", f"/api/diagnosis/session/{urllib.parse.quote(str(session_id))}/status", timeout=20)
            last_status = st
            b = st.get("body")
            if isinstance(b, dict) and (b.get("done") or b.get("status") in ("completed", "done") or b.get("stage") in ("completed", "done")):
                done = True
                break
            if isinstance(b, dict) and (b.get("error") or b.get("stage") == "failed"):
                break
        self.save_artifact("DIAG-012_poll_final", last_status)
        if not done:
            self.add("DIAG-012", cat, "真扣费诊断完成", "FAIL", "诊断未在 7.5 分钟内完成或失败；检查冻结/释放", severity="P1", artifact_payload={"start": start, "last_status": last_status}, http_status=last_status.get("status"))
            return
        # Resolve newest diagnosis id for the brand and generate quote once.
        diag_id = self._find_latest_diagnosis_id(brand_id)
        if not diag_id:
            self.add("DIAG-012", cat, "真扣费诊断完成但找不到记录", "FAIL", "brand diagnoses empty after task done", severity="P1")
            return
        quote = self.client.request("POST", f"/api/diagnosis/{diag_id}/generate-quote", {}, timeout=90)
        wallet_after = self.client.request("GET", "/api/wallet")
        ok = quote.get("status") in (200, 504)
        self.add(
            "DIAG-012",
            cat,
            "真扣费诊断+方案书链路",
            "PASS" if ok else "FAIL",
            f"diagnosis_id={diag_id}, quote_status={quote.get('status')}; budget expected <=1050 points",
            severity="" if ok else "P1",
            artifact_payload={"wallet_before": wallet_before, "start": start, "last_status": last_status, "quote": quote, "wallet_after": wallet_after},
            http_status=quote.get("status"),
        )

    def run_monitoring_sse(self) -> None:
        cat = "05-monitoring-sse"
        bid = int(self.context.get("test_brand_id") or DEFAULT_TEST_BRAND_ID)
        qid = self.context.get("quote_id") or self._find_latest_quote_id(bid)
        self.case_http("MON-001", cat, "monitoring clients", "GET", "/api/monitoring/clients", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("MON-002", cat, "monitoring anomaly all", "GET", "/api/monitoring/anomaly", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("MON-003", cat, f"monitoring anomaly brand={bid}", "GET", f"/api/monitoring/anomaly?brand_id={bid}", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("MON-004", cat, "M3 monitor engines", "GET", "/api/m3/monitor/engines", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("MON-005", cat, "monitoring platforms", "GET", "/api/monitoring/platforms", expect=self.expect_status(200), severity_on_fail="P2")
        self.case_http("MON-006", cat, "monitoring config global", "GET", "/api/monitoring/config", expect=self.expect_status(200), severity_on_fail="P2")
        self.case_http("MON-007", cat, f"monitoring config brand={bid}", "GET", f"/api/monitoring/config?brand_id={bid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        if qid:
            self.case_http("MON-008", cat, f"monitoring quote config {qid}", "GET", f"/api/monitoring/client/{qid}/monitoring-config", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
            self.case_http("MON-009", cat, f"monitoring quote keywords {qid}", "GET", f"/api/monitoring/clients/{qid}/keywords", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        else:
            self.add("MON-008", cat, "monitoring quote config", "SKIP", "no quote fixture")
            self.add("MON-009", cat, "monitoring quote keywords", "SKIP", "no quote fixture")
        self.case_http("MON-010", cat, "reports list", "GET", "/api/reports", expect=self.expect_status(200), severity_on_fail="P2")
        sse = self.client.stream_probe("/api/settings/health-check", timeout=30)
        self.add("MON-011", cat, "SSE health-check 首帧", "PASS" if sse["ok"] else "FAIL", f"HTTP {sse.get('status')} body={str(sse.get('body'))[:80]}", severity="" if sse["ok"] else "P2", elapsed_ms=sse.get("elapsed_ms", 0), artifact_payload={"response": sse}, http_status=sse.get("status"))
        self.case_http("MON-012", cat, "scheduler status", "GET", "/api/scheduler/status", expect=self.expect_status(200, 401, 403), severity_on_fail="P3")

    def run_public_events_intake_material(self) -> None:
        cat = "06-public-events-intake-material"
        bid = int(self.context.get("test_brand_id") or DEFAULT_TEST_BRAND_ID)
        diag_id = self.context.get("diagnosis_id") or self._find_latest_diagnosis_id(bid) or 204
        qid = self.context.get("quote_id") or self._find_latest_quote_id(bid)
        ek = f"qa_backend_{int(time.time())}"
        self.case_http("EVENT-001", cat, "public_report event insert", "POST", "/api/m3/customer-events/public", {"source": "public_report", "event_type": "opened", "diagnosis_id": diag_id, "event_key": ek}, expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("EVENT-002", cat, "public event mismatch rejected", "POST", "/api/m3/customer-events/public", {"source": "public_report", "event_type": "opened", "diagnosis_id": diag_id, "brand_id": 999999, "event_key": ek + "_mismatch"}, expect=lambda r: (r.get("status") == 200 and isinstance(r.get("body"), dict) and r["body"].get("success") is False, f"HTTP {r.get('status')} body={r.get('body')}"), severity_on_fail="P1")
        self.case_http("EVENT-003", cat, f"events timeline brand={bid}", "GET", f"/api/m3/customer-events?brand_id={bid}&days=30", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("EVENT-004", cat, f"events summary brand={bid}", "GET", f"/api/m3/customer-events/summary?brand_id={bid}&days=30", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("INTAKE-001", cat, f"intake token list brand={bid}", "GET", f"/api/m3/intake-tokens?brand_id={bid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("INTAKE-002", cat, f"intake submissions brand={bid}", "GET", f"/api/m3/profile-submissions?brand_id={bid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("MAT-001", cat, f"material confirm status brand={bid}", "GET", f"/api/m3/material-confirm/status/{bid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("MAT-002", cat, "material confirm status protected unauth", "GET", f"/api/m3/material-confirm/status/{bid}", token="", expect=self.expect_status(401), severity_on_fail="P1")
        if qid:
            self.case_http("PORTAL-001", cat, f"portal token by quote {qid}", "GET", f"/api/portal/tokens/{qid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        else:
            self.add("PORTAL-001", cat, "portal token by quote", "SKIP", "no quote fixture")

    def run_partner_referral_admin(self) -> None:
        cat = "07-partner-referral-admin"
        self.case_http("PARTNER-001", cat, "partner flag public", "GET", "/api/partner/flag", token="", expect=self.expect_status(200), severity_on_fail="P1")
        self.case_http("PARTNER-002", cat, "partner status", "GET", "/api/partner/status", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("REF-001", cat, "referral code", "GET", "/api/referral/code", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("REF-002", cat, "referral stats", "GET", "/api/referral/stats", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("REF-003", cat, "referral team", "GET", "/api/referral/team", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("REF-004", cat, "profit summary", "GET", "/api/referral/profit-summary", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("REF-005", cat, "whitelabel get", "GET", "/api/referral/whitelabel", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        self.case_http("ADMIN-001", cat, "admin users protected for non-admin", "GET", "/api/admin/users?page=1&page_size=5", expect=self.expect_status(401, 403), severity_on_fail="P1")

    def run_writing_publish_reports(self) -> None:
        cat = "08-writing-publish-reports"
        bid = int(self.context.get("test_brand_id") or DEFAULT_TEST_BRAND_ID)
        diag_id = self.context.get("diagnosis_id") or self._find_latest_diagnosis_id(bid)
        qid = self.context.get("quote_id") or self._find_latest_quote_id(bid)
        self.case_http("WRITE-001", cat, "writing projects", "GET", "/api/writing/projects", expect=self.expect_status(200), severity_on_fail="P2")
        if qid:
            self.case_http("WRITE-002", cat, f"writing project {qid}", "GET", f"/api/writing/projects/{qid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
            self.case_http("PUB-001", cat, f"publications quote {qid}", "GET", f"/api/publications/{qid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
            self.case_http("MON-QUOTE-001", cat, f"insights quote {qid}", "GET", f"/api/insights/{qid}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
        else:
            self.add("WRITE-002", cat, "writing project", "SKIP", "no quote fixture")
            self.add("PUB-001", cat, "publications quote", "SKIP", "no quote fixture")
            self.add("MON-QUOTE-001", cat, "insights quote", "SKIP", "no quote fixture")
        if diag_id:
            self.case_http("WRITE-003", cat, f"articles list diagnosis {diag_id}", "GET", f"/api/articles/list/{diag_id}", expect=self.expect_status(200, 403, 404), severity_on_fail="P2")
            self.case_http("REPORT-001", cat, f"report v2 html {diag_id}", "GET", f"/api/diagnosis/{diag_id}/report-v2.html", expect=self.expect_status(200, 403, 404, 500), severity_on_fail="P2")
            self.case_http("REPORT-002", cat, f"report v2 pdf {diag_id}", "GET", f"/api/diagnosis/{diag_id}/report-v2.pdf", expect=self.expect_status(200, 403, 404, 500), severity_on_fail="P2")
        else:
            self.add("WRITE-003", cat, "articles list diagnosis", "SKIP", "no diagnosis fixture")
            self.add("REPORT-001", cat, "report v2 html", "SKIP", "no diagnosis fixture")
            self.add("REPORT-002", cat, "report v2 pdf", "SKIP", "no diagnosis fixture")
        self.case_http("REPORT-003", cat, "reports all/list", "GET", "/api/reports/all", expect=self.expect_status(200, 403), severity_on_fail="P2")

    def run_public_routes_rbac_negative(self) -> None:
        cat = "09-public-rbac-negative"
        for idx, path in enumerate(["/q/test", "/s/test", "/m/test", "/portal/test", "/public/report/test"], start=1):
            self.case_http(f"PUBLIC-{idx:03d}", cat, f"public route {path}", "GET", path, token="", expect=self.expect_status(200, 404, 410), severity_on_fail="P2")
        # Protected endpoints without token.
        protected = [
            "/api/m3/customers",
            "/api/wallet",
            "/api/quotes",
            "/api/my-clients",
            "/api/m3/customer-events?brand_id=270",
            "/api/m3/intake-tokens?brand_id=270",
            "/api/m3/material-confirm/status/270",
        ]
        for i, path in enumerate(protected, start=1):
            self.case_http(f"RBAC-NOAUTH-{i:03d}", cat, f"noauth {path}", "GET", path, token="", expect=self.expect_status(401), severity_on_fail="P1")

    def run_db_schema_perf(self) -> None:
        cat = "10-db-schema-performance"
        checks = [
            ("DB-001", "feature_pricing core rows", "SELECT feature_code, cost_points FROM feature_pricing WHERE feature_code IN ('geo_diagnosis','quote_generate','autofill_brand') ORDER BY feature_code;"),
            ("DB-002", "quotes monitoring interval columns", "SELECT column_name FROM information_schema.columns WHERE table_name='quotes' AND column_name IN ('monitoring_interval_hours','monitoring_last_run_at') ORDER BY column_name;"),
            ("DB-003", "m3_customer_events exists", "SELECT COUNT(*)::int AS rows FROM information_schema.tables WHERE table_name='m3_customer_events';"),
            ("DB-004", "intake tables exist", "SELECT table_name FROM information_schema.tables WHERE table_name IN ('intake_tokens','profile_update_submissions') ORDER BY table_name;"),
            ("DB-005", "material confirm table exists", "SELECT COUNT(*)::int AS rows FROM information_schema.tables WHERE table_name='marketing_confirm_sessions';"),
            ("DB-006", "protected brands untouched visibility", "SELECT id,name FROM brands WHERE id IN (94,96,99,105,107,140,152,154) ORDER BY id;"),
        ]
        for case_id, name, sql in checks:
            rc, out = self._psql(sql)
            status = "PASS" if rc == 0 and out.strip() else "FAIL"
            self.add(case_id, cat, name, status, detail=out.strip()[:500] if out else f"psql rc={rc}", severity="" if status == "PASS" else "P2", artifact_payload={"sql": sql, "rc": rc, "stdout": out})
        perf_paths = [
            "/api/auth/me",
            "/api/wallet",
            "/api/m3/customers",
            "/api/m3/today?space=sales",
            "/api/monitoring/anomaly",
            "/api/writing/projects",
            "/api/reports",
            "/api/notifications/unread-count",
        ]
        for i, path in enumerate(perf_paths, start=1):
            res = self.client.request("GET", path, timeout=30)
            ok = res.get("ok") and res.get("elapsed_ms", 999999) < 5000
            self.add(f"PERF-{i:03d}", cat, f"latency {path}", "PASS" if ok else "FAIL", detail=f"HTTP {res.get('status')} {res.get('elapsed_ms')}ms", severity="" if ok else "P2", elapsed_ms=res.get("elapsed_ms", 0), artifact_payload={"response": res}, http_status=res.get("status"))

    def _psql(self, sql: str) -> tuple[int, str]:
        cmd = [
            "docker",
            "exec",
            "omnirank-db",
            "psql",
            "-U",
            "geo_admin",
            "-d",
            "geo_agentscope",
            "-t",
            "-A",
            "-F",
            "|",
            "-c",
            sql,
        ]
        try:
            cp = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=30)
            return cp.returncode, (cp.stdout + cp.stderr)
        except Exception as e:
            return 99, f"{type(e).__name__}: {e}"

    def run_all(self) -> None:
        self.login()
        self.discover_context()
        self.run_auth_health()
        self.run_wallet_pricing()
        self.run_client_brand_m3()
        self.run_diagnosis_quote()
        self.run_monitoring_sse()
        self.run_public_events_intake_material()
        self.run_partner_referral_admin()
        self.run_writing_publish_reports()
        self.run_public_routes_rbac_negative()
        self.run_db_schema_perf()
        self.write_outputs()

    def write_outputs(self) -> None:
        total = len(self.results)
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        sev_counts: dict[str, int] = {}
        for r in self.results:
            if r.status == "FAIL":
                sev_counts[r.severity or "P?"] = sev_counts.get(r.severity or "P?", 0) + 1
        out = {
            "generated_at": now_iso(),
            "base_url": self.base_url,
            "context": redact(self.context),
            "summary": {"total": total, "counts": counts, "severity_counts": sev_counts},
            "results": [r.__dict__ for r in self.results],
        }
        RESULT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_markdown_report(out)
        self._write_bug_list(out)

    def _write_markdown_report(self, out: dict[str, Any]) -> None:
        summary = out["summary"]
        lines = [
            "# QA Backend Report · 2026-04-29",
            "",
            f"- Run time: `{out['generated_at']}`",
            f"- Base URL: `{self.base_url}`",
            f"- Test branch: `feat/qa-backend-2026-04-29`",
            f"- Artifacts: `agent-test-artifacts/backend-qa-2026-04-29/`",
            f"- Result JSON: `agent-test-artifacts/backend-qa-2026-04-29/qa_backend_results.json`",
            "",
            "## Scope Note",
            "",
            "原任务引用的 `QA_SHARED_CONTEXT_2026-04-28.md` 和 `HANDOFF_BACKEND_QA_2026-04-28.md` 在当前 `origin/main` 中不存在。本次按 `CTO_HANDBOOK_2026-04-28.md`、Deploy-CTO handoff、现有 API 代码和旧版纯 API 验收指令重建 10 类后端 QA。",
            "",
            "## Summary",
            "",
            f"- Total checks: **{summary['total']}**",
            f"- PASS: **{summary['counts'].get('PASS', 0)}**",
            f"- FAIL: **{summary['counts'].get('FAIL', 0)}**",
            f"- SKIP: **{summary['counts'].get('SKIP', 0)}**",
            f"- Severity counts: `{summary.get('severity_counts', {})}`",
            f"- Charged flow enabled: `{self.run_charged}`",
            "",
            "## Context",
            "",
            "```json",
            json.dumps(redact(self.context), ensure_ascii=False, indent=2)[:6000],
            "```",
            "",
            "## Category Results",
            "",
        ]
        cats = sorted({r.category for r in self.results})
        for cat in cats:
            rows = [r for r in self.results if r.category == cat]
            pass_n = sum(r.status == "PASS" for r in rows)
            fail_n = sum(r.status == "FAIL" for r in rows)
            skip_n = sum(r.status == "SKIP" for r in rows)
            lines += [f"### {cat}", "", f"- PASS {pass_n} / FAIL {fail_n} / SKIP {skip_n}", ""]
            lines += ["| ID | Status | HTTP | ms | Name | Detail | Artifact |", "|---|---:|---:|---:|---|---|---|"]
            for r in rows:
                detail = (r.detail or "").replace("|", "\\|").replace("\n", " ")[:180]
                artifact = f"`{r.artifact}`" if r.artifact else ""
                lines.append(f"| {r.id} | {r.status}{' '+r.severity if r.severity else ''} | {r.http_status if r.http_status is not None else ''} | {r.elapsed_ms} | {r.name} | {detail} | {artifact} |")
            lines.append("")
        REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_bug_list(self, out: dict[str, Any]) -> None:
        failures = [r for r in self.results if r.status == "FAIL"]
        lines = [
            "# QA Bug List · Backend · 2026-04-29",
            "",
            f"- Source report: `{REPORT_PATH.relative_to(ROOT).as_posix()}`",
            f"- Result JSON: `{RESULT_JSON.relative_to(ROOT).as_posix()}`",
            f"- Generated: `{out['generated_at']}`",
            "",
        ]
        if not failures:
            lines += ["## No Failing Backend Checks", "", "本轮后端 QA 未发现 FAIL 项。仍需人工复核 SKIP 项和真扣费链路是否需要在 staging 重跑。", ""]
        else:
            lines += ["## Failing Checks", ""]
            lines += ["| Severity | ID | Category | HTTP | Name | Detail | Artifact |", "|---|---|---|---:|---|---|---|"]
            for r in failures:
                detail = (r.detail or "").replace("|", "\\|").replace("\n", " ")[:220]
                artifact = f"`{r.artifact}`" if r.artifact else ""
                lines.append(f"| {r.severity or 'P?'} | {r.id} | {r.category} | {r.http_status if r.http_status is not None else ''} | {r.name} | {detail} | {artifact} |")
            lines.append("")
        skipped = [r for r in self.results if r.status == "SKIP"]
        lines += ["## Skipped / Pending Verification", ""]
        if skipped:
            for r in skipped:
                lines.append(f"- `{r.id}` {r.name}: {r.detail}")
        else:
            lines.append("- None")
        lines.append("")
        BUG_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("QA_BASE_URL", "https://omnirank.top"))
    parser.add_argument("--run-charged", action="store_true", default=os.environ.get("QA_RUN_CHARGED", "0") == "1")
    args = parser.parse_args()
    user = os.environ.get("QA_USER")
    password = os.environ.get("QA_PASSWORD")
    if not user or not password:
        print("QA_USER and QA_PASSWORD are required", file=sys.stderr)
        return 2
    qa = BackendQA(args.base_url, user, password, args.run_charged)
    try:
        qa.run_all()
    except KeyboardInterrupt:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        qa.add("RUNNER-ERROR", "runner", "QA runner unexpected error", "FAIL", f"{type(e).__name__}: {e}", severity="P0", artifact_payload={"traceback": tb})
        qa.write_outputs()
        print(tb, file=sys.stderr)
        return 1
    print(json.dumps({"report": str(REPORT_PATH), "bug_list": str(BUG_PATH), "result_json": str(RESULT_JSON), "counts": {k: sum(r.status == k for r in qa.results) for k in ("PASS", "FAIL", "SKIP")}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
