"""变异验证 · 工单 2026-07-29(协议门禁 + 零售目录守卫 + 监测加载态)

每条变异把某一处修复精确改回事故形态,重跑对应判别锁,要求**至少一条转红**。
一条变异不转红 = 那条锁是假绿,必须先修锁本身。

三条历史踩坑写进流程:
  1. **FAILED 与 ERROR 两类都数** —— 只数 FAILED 会把 collection error 当成"没红";
  2. **变异后先跑 import 冒烟** —— 语法炸了的"红"不算判别力;
  3. **git 精确回滚** —— 靠 checkout 还原,不靠字符串再替换回去。

用法:  python scripts/run_login_gate_mutations_2026_07_29.py [--only M5]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PY = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
PG_LOCKS = PY + ["tests/login_gate_pg"]
HANDLER_LOCKS = PY + ["tests/test_login_gate_handlers_2026_07_29.py"]
# Windows 上 npx 不是可直接 CreateProcess 的可执行文件 —— 直接用本地 .bin 里的 shim。
_PW = str(ROOT / "frontend" / "node_modules" / ".bin" /
          ("playwright.cmd" if sys.platform == "win32" else "playwright"))
PW_AGREEMENT = [_PW, "test", "--config=playwright.login-gate.config.ts",
                "tests/login-gate/agreement-gate.spec.ts"]
PW_MONITORING = [_PW, "test", "--config=playwright.login-gate.config.ts",
                 "tests/login-gate/monitoring-loading.spec.ts"]

IMPORT_SMOKE = [
    sys.executable, "-c",
    "import api.auth_api, api.pricing_ssot_api, services.pricing_publication, "
    "services.retail_catalog_health, services.registration_agreement_gate_metrics",
]


@dataclass
class Mutation:
    key: str
    what: str
    path: str
    old: str
    new: str
    locks: list = field(default_factory=list)
    frontend: bool = False


MUTATIONS = [
    Mutation(
        "M1", "协议识别改回直接读 detail(事故形态本体)",
        "frontend/src/context/AuthContext.tsx",
        "    const detail = readDetailContract(err) as any;",
        "    const detail = (err?.response?.data?.detail) as any;",
        locks=["pw_agreement"], frontend=True,
    ),
    Mutation(
        "M2", "readDetailContract 优先返回 detail 而不是 detail_contract",
        "frontend/src/lib/api.ts",
        "    return record.detail_contract !== undefined ? record.detail_contract : record.detail;",
        "    return record.detail !== undefined ? record.detail : record.detail_contract;",
        locks=["pw_agreement"], frontend=True,
    ),
    Mutation(
        "M3", "去掉 AuthContext 的 428 fail-safe 分支(识别失败即降级红字)",
        "frontend/src/context/AuthContext.tsx",
        "            if (isAgreementGateError(err)) {\n"
        "                return {\n"
        "                    success: false,\n"
        "                    requiresAgreement: true,\n"
        "                    error: authErrorMessage(err, '为继续使用，请确认当前《用户服务协议》和《隐私政策》'),\n"
        "                };\n"
        "            }\n",
        "",
        locks=["pw_agreement"], frontend=True,
    ),
    Mutation(
        "M4", "登录页不再渲染补签入口(只剩红字)",
        "frontend/src/pages/Login/LoginPage.tsx",
        "            {agreementGateBlocked && (\n"
        "              <AgreementGateExit onGo={() => navigate('/agreement-update')} />\n"
        "            )}\n",
        "",
        locks=["pw_agreement"], frontend=True,
    ),
    Mutation(
        "M5", "补签页无 session 时弹回登录页(死循环那一环)",
        "frontend/src/pages/Login/AgreementUpdatePage.tsx",
        "  if (!session) {\n    return (\n      <AgreementSessionRecovery",
        "  if (!session) {\n    navigate('/login?agreement=missing', { replace: true });\n"
        "    return null;\n  }\n  if (false) {\n    return (\n      <AgreementSessionRecovery",
        locks=["pw_agreement"], frontend=True,
    ),
    Mutation(
        "M6", "监测面板还原 !loading &&(加载态漏在判空外)",
        "frontend/src/pages/Monitoring/components/IdentityReviewPanel.tsx",
        "    if (loading || items.length === 0) return null;",
        "    if (!loading && items.length === 0) return null;",
        locks=["pw_monitoring"], frontend=True,
    ),
    Mutation(
        "M7", "去掉零条目零售目录发布期守卫",
        "services/pricing_publication.py",
        "        if str(plan[\"scope_key\"]) not in confirmed:\n"
        "            raise EmptyRetailCatalogRejected(str(plan[\"scope_key\"]))",
        "        if False:\n"
        "            raise EmptyRetailCatalogRejected(str(plan[\"scope_key\"]))",
        locks=["pg"],
    ),
    Mutation(
        "M8", "确认清单退化成'填了就放行'(不再逐 scope 精确匹配)",
        "services/pricing_publication.py",
        "        if str(plan[\"scope_key\"]) not in confirmed:",
        "        if not confirmed:",
        locks=["pg"],
    ),
    Mutation(
        "M9", "体检去掉 NOT EXISTS(零条目判定失效)",
        "services/retail_catalog_health.py",
        "               AND NOT EXISTS (\n"
        "                     SELECT 1 FROM pricing_catalog_entries e WHERE e.version_id = v.id\n"
        "                   )\n",
        "",
        locks=["pg"],
    ),
    Mutation(
        "M10", "体检去掉 effective_to IS NULL(把历史空版本也报成断供)",
        "services/retail_catalog_health.py",
        "               AND v.effective_to IS NULL\n",
        "",
        locks=["pg"],
    ),
    Mutation(
        "M11", "补签只落一份协议(锁 2 的判别力)",
        "services/legal_agreements.py",
        "    for document in registration_agreements():",
        "    for document in registration_agreements()[:1]:",
        locks=["pg"],
    ),
    Mutation(
        "M12", "门禁不再留痕(被挡的人不出现在任何计数里)",
        "api/auth_api.py",
        "    record_gate_trigger(user_id=int(user_id), auth_method=auth_method)",
        "    pass",
        locks=["handler"],
    ),
    Mutation(
        "M13", "自助重取端点顺手签发 JWT(把凭证升级成登录态)",
        "api/auth_api.py",
        "    logger.info(f\"补签凭证重新签发: user_id={user_id} from {_get_client_ip(request)}\")\n"
        "    return {\"success\": True, **session}",
        "    logger.info(f\"补签凭证重新签发: user_id={user_id} from {_get_client_ip(request)}\")\n"
        "    return {\"success\": True, \"token\": create_jwt(user_id), **session}",
        locks=["handler"],
    ),
    Mutation(
        "M14", "自助重取端点跳过限流",
        "api/auth_api.py",
        "    allowed, msg = check_rate_limit(req.username)\n"
        "    if not allowed:\n"
        "        logger.warning(f\"补签凭证限流: {req.username} from {_get_client_ip(request)}\")\n"
        "        raise HTTPException(status_code=429, detail=msg)",
        "    allowed, msg = True, \"\"",
        locks=["handler"],
    ),
    Mutation(
        "M15", "协议已齐全时也发凭证(端点语义被放宽)",
        "api/auth_api.py",
        "    if complete:\n"
        "        # 不是错误,是\"你不需要补签了\"。给明确下一步,不给死路。\n"
        "        return {",
        "    if False:\n"
        "        return {",
        locks=["handler"],
    ),
]


def run(cmd, cwd=ROOT):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", shell=False)


def pytest_red_count(output: str) -> int:
    """FAILED 与 ERROR **两类都数**(历史踩坑:只数 FAILED 会把收集期错误当成没红)。

    只在**末尾汇总行**里取数字:早先在全文匹配 `(\\d+) error`,结果 traceback 里的
    任意数字都会被算进来(M13 一度报出 reds=5433)。红不红的结论没受影响,
    但计数会误导读者,所以收紧到汇总区。
    """
    # 只认 pytest 的**末尾汇总行**(形如 `1 failed, 7 passed in 2.44s`)。
    # 先前在全文正则找 `(\d+) failed`,结果撞上 psycopg2 报错里的
    # "port 5432 failed",把 M13 算成 reds=5433。
    summary = ""
    for line in reversed(output.strip().splitlines()):
        if re.search(r"\b\d+ (?:failed|passed|error|errors|skipped)\b", line) and " in " in line:
            summary = line
            break
    total = 0
    for pattern in (r"\b(\d+) failed\b", r"\b(\d+) errors?\b"):
        match = re.search(pattern, summary)
        if match:
            total += int(match.group(1))
    total += len(re.findall(r"^(?:FAILED|ERROR) ", output, re.MULTILINE))
    return total


def playwright_red_count(output: str) -> int:
    total = 0
    for pattern in (r"(\d+) failed", r"(\d+) did not run", r"(\d+) timed out"):
        match = re.search(pattern, output)
        if match:
            total += int(match.group(1))
    return total


def run_locks(names):
    reds, detail = 0, []
    if "pg" in names:
        out = run(PG_LOCKS)
        red = pytest_red_count(out.stdout + out.stderr)
        reds += red
        detail.append(f"pg={red}")
    if "handler" in names:
        out = run(HANDLER_LOCKS)
        red = pytest_red_count(out.stdout + out.stderr)
        reds += red
        detail.append(f"handler={red}")
    for key, cmd in (("pw_agreement", PW_AGREEMENT), ("pw_monitoring", PW_MONITORING)):
        if key in names:
            out = run(list(cmd), cwd=ROOT / "frontend")
            red = playwright_red_count(out.stdout + out.stderr)
            if red == 0 and out.returncode != 0:
                red = 1  # 非零退出但没解析出计数 —— 按红计,宁可多查一次
            reds += red
            detail.append(f"{key}={red}")
    return reds, ", ".join(detail)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    targets = [m for m in MUTATIONS if not args.only or m.key == args.only]
    results = []

    for mutation in targets:
        path = ROOT / mutation.path
        original = path.read_text(encoding="utf-8")
        if mutation.old not in original:
            results.append((mutation, "PATTERN-MISS", 0, ""))
            print(f"[{mutation.key}] [ANCHOR-MISS] 变异锚点没匹配上 —— {mutation.path}")
            continue
        try:
            path.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            if not mutation.frontend:
                smoke = run(IMPORT_SMOKE)
                if smoke.returncode != 0:
                    results.append((mutation, "SMOKE-FAIL", 0,
                                    smoke.stderr.strip().splitlines()[-1:] and
                                    smoke.stderr.strip().splitlines()[-1] or ""))
                    print(f"[{mutation.key}] [SMOKE-FAIL] import 冒烟不过 —— 这种红不算判别力")
                    continue
            reds, detail = run_locks(mutation.locks)
            status = "RED" if reds > 0 else "GREEN(假绿!)"
            results.append((mutation, status, reds, detail))
            print(f"[{mutation.key}] {"[RED-OK]" if reds else "[NOT-RED]"} {status:12} reds={reds:<3} "
                  f"({detail})  {mutation.what}")
        finally:
            subprocess.run(["git", "checkout", "--", mutation.path], cwd=ROOT, check=False)

    print("\n" + "=" * 78)
    bad = [r for r in results if r[1] != "RED"]
    print(f"变异总数 {len(results)} · 转红 {len(results) - len(bad)} · 未转红 {len(bad)}")
    for mutation, status, reds, detail in bad:
        print(f"  [NOT-RED] {mutation.key} {status} — {mutation.what}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
