#!/usr/bin/env python
"""「团队与席位」后端人话化判据（工单 2026-08-17 §2 / §3 / §5-3）。

不依赖数据库、不依赖网络 —— 只打**真实的请求模型**与**真实的错误出口函数**,
所以可以在任何机器上原样重跑。

判据成对(每个「必须命中」配一个「必须不命中」):
  1. 422 字段校验 → 人话   ←→ 未登记字段落到通用中文兜底,**且不含英文字段名**
  2. 团队路径才接管 422     ←→ 非团队路径必须**不**被这个 handler 改形状
  3. 内部不变量 → 通用文案  ←→ 未登记的 code 必须**原样透出**(别把可行动文案也吞了)
  4. 用户可见文案零「额度」  ←→ 扫描器对人造「额度」必须报红(证明不是恒真)
  5. 内部不变量原文进日志    ←→ 不进响应
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ORGANIZATION_SEATS_ENABLED", "true")

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, name: str, detail: object = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  → {detail!r}" if not ok and detail != "" else ""))


# ── 1 / 2 · 422 字段校验人话化 ────────────────────────────────────────────
from pydantic import ValidationError  # noqa: E402

from api.organization_api import (  # noqa: E402
    CreateOrganizationRequest,
    PublicInviteCredentialOnboardRequest,
    PublicInviteVerifyRequest,
    ReasonRequest,
    ShortCodeRequest,
)
from services.organization_validation_copy import (  # noqa: E402
    is_organization_path,
    validation_detail,
)


def errors_for(model, payload) -> list[dict]:
    try:
        model(**payload)
    except ValidationError as exc:
        return exc.errors()
    raise AssertionError(f"{model.__name__} 竟然接受了 {payload!r} —— 判据失去意义")


CASES = [
    # (模型, 非法载荷, 必须出现的中文, 说明)
    (PublicInviteVerifyRequest, {"token": "t" * 24, "request_id": "r" * 10, "code": "12345"},
     "验证码是 6 位数字", "接受邀请页少输一位验证码（审计 §1 头号场景）"),
    (PublicInviteCredentialOnboardRequest,
     {"token": "t" * 24, "request_id": "r" * 10, "password": "123", "display_name": "张三",
      "terms_accepted": True, "privacy_accepted": True, "terms_version": "v1", "privacy_version": "v1"},
     "密码至少 6 位", "开户密码不足 6 位"),
    (ShortCodeRequest, {"short_code": "ab"}, "团队代码须为 4-12 位", "团队代码过短"),
    (CreateOrganizationRequest, {"name": "", "request_id": "r" * 10}, "团队名称不能为空", "团队名称为空"),
    (ReasonRequest, {"reason": ""}, "请填写原因", "忘填原因"),
    (PublicInviteVerifyRequest, {"token": "short", "request_id": "r" * 10, "code": "123456"},
     "邀请链接不完整", "邀请链接被截断"),
]

for model, payload, expected, label in CASES:
    detail = validation_detail(errors_for(model, payload), request_id="verify-local")
    check(expected in detail["message"], f"422 人话 · {label}", detail["message"])
    check(detail["message"] != "组织服务暂不可用", f"422 不再落到系统故障文案 · {label}", detail["message"])
    # 必须不命中:message 里不许出现英文字段名 / Pydantic 英文 msg
    leaked = re.findall(r"[A-Za-z_]{4,}", detail["message"])
    check(not leaked, f"422 文案零英文露出 · {label}", leaked)

# 未登记字段 → 通用中文兜底,且**不含**字段名
unknown_detail = validation_detail(
    [{"type": "string_too_short", "loc": ["body", "some_unmapped_field"], "msg": "String too short"}],
    request_id="verify-local",
)
check("some_unmapped_field" not in unknown_detail["message"],
      "未登记字段不把英文字段名甩给用户", unknown_detail["message"])
check(re.search(r"[一-龥]", unknown_detail["message"]) is not None,
      "未登记字段仍给中文兜底", unknown_detail["message"])
check(unknown_detail["details"]["fields"][0]["field"] == "some_unmapped_field",
      "原始字段名仍进 details 供客服排查")

# 路径归属:必须命中团队路径,必须不命中其它模块
for path in ("/api/organization/invites", "/api/public/organization/invites/inspect",
             "/api/admin/organization-product-config"):
    check(is_organization_path(path), f"422 handler 接管团队路径 · {path}")
for path in ("/api/my-clients", "/api/diagnosis/run", "/api/organizationfoo", "/api/wallet"):
    check(not is_organization_path(path), f"422 handler **不**接管其它模块 · {path}")

# ── 3 / 5 · 内部不变量出口收口 ────────────────────────────────────────────
from services.organization_contract import (  # noqa: E402
    INTERNAL_INVARIANT_CODES,
    OrganizationError,
)

# 审计点名的「全模块最严重单条」:函数名直达用户(billing.py:1613)
worst = OrganizationError(
    "ORG_CLAIM_TOKEN_REQUIRED",
    "活跃执行路径结算必须携带真实存活 claim_token；kill-9 恢复/对账请改用 settle_charge_via_reconciliation",
    http_status=409,
)
log_stream = io.StringIO()
handler = logging.StreamHandler(log_stream)
contract_logger = logging.getLogger("GEO-OrganizationContract")
contract_logger.addHandler(handler)
contract_logger.setLevel(logging.WARNING)
detail = worst.as_detail("req-worst")
contract_logger.removeHandler(handler)

for jargon in ("claim_token", "kill-9", "settle_charge_via_reconciliation"):
    check(jargon not in detail["message"], f"内部不变量不把 {jargon} 甩给用户", detail["message"])
check("ORG_CLAIM_TOKEN_REQUIRED" in detail["message"], "通用文案带错误码供客服反查", detail["message"])
check(detail["details"]["error_code"] == "ORG_CLAIM_TOKEN_REQUIRED", "错误码同时进 details")
check("settle_charge_via_reconciliation" in log_stream.getvalue(), "原文降级进日志(排查能力不减)")

# 必须不命中:未登记的 code 一律原样透出 —— 否则会把可行动文案也吞掉
actionable = OrganizationError("ORG_BRAND_NOT_ASSIGNED", "该客户未分配给当前员工", http_status=403)
check(actionable.as_detail("req-ok")["message"] == "该客户未分配给当前员工",
      "未登记 code 原样透出(不误吞可行动文案)")
check("ORG_BRAND_NOT_ASSIGNED" not in INTERNAL_INVARIANT_CODES,
      "可行动 code 没被错登记为内部不变量")
check(len(INTERNAL_INVARIANT_CODES) > 0, "内部不变量登记表非空(空表 = 这条判据恒真)")

# 登记表里的每个 code 都必须在代码里真的被 raise 过 —— 防止表里堆死码
service_source = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted((ROOT / "services").glob("organization_*.py"))
)
orphans = [code for code in INTERNAL_INVARIANT_CODES if f'"{code}"' not in service_source]
check(not orphans, "登记表里没有代码里不存在的 code", orphans)

# ── 4 · 用户可见文案零「额度」 ────────────────────────────────────────────
import ast  # noqa: E402


def user_visible_literals(source: str) -> list[str]:
    """取**会被渲染给用户**的字符串字面量。

    用 AST 而不是正则:
      - 注释天然被 AST 忽略(注释里的用词不上屏,拿它报红就是假阳,
        一个会误报的锁很快就会被人关掉);
      - 文档字符串(模块/类/函数的首个裸字符串表达式)显式跳过,同理。
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            first = body[0] if body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


scan_targets = sorted((ROOT / "services").glob("organization_*.py")) + \
    sorted((ROOT / "api").glob("organization_*.py"))
BANNED_TERMS = ("额度", "积分")
violations: list[str] = []
scanned = 0
for path in scan_targets:
    for literal in user_visible_literals(path.read_text(encoding="utf-8")):
        scanned += 1
        if any(term in literal for term in BANNED_TERMS):
            violations.append(f"{path.name}: {literal[:40]}")
check(scanned > 0, "术语扫描确实扫到了字符串(分母 > 0,不是空集假绿)", scanned)
check(not violations, "后端用户可见文案零「额度/积分」(全站统一「算力」)", violations)
# 反向对照:同一个扫描口径对人造违规必须报红,证明它不是恒真。
probe = user_visible_literals('X = "员工额度不足"\nY = "正常算力文案"\n')
probe_hits = [literal for literal in probe if any(term in literal for term in BANNED_TERMS)]
check(probe_hits == ["员工额度不足"], "术语扫描口径对人造「额度」报红(证明不是恒真)", probe_hits)
# 反向对照之二:注释与 docstring **不**应被扫进来(否则会误报,锁会被关掉)
noise = user_visible_literals('"""模块 docstring 里写了额度"""\n# 注释里写了额度\nZ = "干净文案"\n')
check(all("额度" not in literal for literal in noise), "注释/docstring 不被误判为用户可见文案", noise)

print()
print(f"summary: {len(PASSED)} passed / {len(FAILED)} failed")
if FAILED:
    for name in FAILED:
        print("  FAILED:", name)
    raise SystemExit(1)
