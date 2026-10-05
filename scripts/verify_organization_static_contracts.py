"""Static fail-closed gates for the organization internal-seats package."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SEMANTIC_BASE_SHA = "dc02115cb2c2b977f5d56ea38a9dea1fe425a4a0"
FREEZE_SHA = "00bf26e642e63de3984af86249b122ac171641ab"
FORBIDDEN_FILES = (
    "db/connection.py",
    "auth/middleware.py",
    "auth/jwt_utils.py",
)


class Gate:
    def __init__(self) -> None:
        self.passed: list[str] = []

    def check(self, condition: object, name: str, details: object = None) -> None:
        if not condition:
            raise AssertionError(f"{name}: {details!r}")
        self.passed.append(name)
        print(json.dumps({"test": name, "status": "passed"}, ensure_ascii=False))


def _python_string_set(path: Path, name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and value.args:
            value = value.args[0]
        return {str(item.value) for item in ast.walk(value) if isinstance(item, ast.Constant) and isinstance(item.value, str)}
    raise AssertionError(f"missing Python contract {name}")


def _typescript_record_keys(source: str, name: str) -> set[str]:
    match = re.search(rf"const\s+{re.escape(name)}[^=]*=\s*\{{(?P<body>.*?)\n\}};", source, re.DOTALL)
    if not match:
        raise AssertionError(f"missing TypeScript record {name}")
    return set(re.findall(r"^\s*['\"]([^'\"]+)['\"]\s*:", match.group("body"), re.MULTILINE))


def _typescript_array(source: str, name: str) -> set[str]:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*\[(?P<body>.*?)\];", source, re.DOTALL)
    if not match:
        raise AssertionError(f"missing TypeScript array {name}")
    return set(re.findall(r"['\"]([^'\"]+)['\"]", match.group("body")))


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-c", f"safe.directory={ROOT}", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def _openapi() -> dict[str, object]:
    old_flags = {key: os.environ.get(key) for key in (
        "ORGANIZATION_SEATS_ENABLED",
        "ORGANIZATION_INVITE_ONBOARDING_ENABLED",
        "ORGANIZATION_SHARED_PAYER_ENABLED",
        "ORGANIZATION_EXTERNAL_ACTIONS_ENABLED",
    )}
    try:
        os.environ.update({
            "ORGANIZATION_SEATS_ENABLED": "false",
            "ORGANIZATION_INVITE_ONBOARDING_ENABLED": "false",
            "ORGANIZATION_SHARED_PAYER_ENABLED": "false",
            "ORGANIZATION_EXTERNAL_ACTIONS_ENABLED": "false",
        })
        from fastapi import FastAPI
        from api.organization_api import admin_router, public_router, router

        app = FastAPI()
        app.include_router(router)
        app.include_router(public_router)
        app.include_router(admin_router)
        return app.openapi()
    finally:
        for key, value in old_flags.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    gate = Gate()
    contract_path = ROOT / "services" / "organization_contract.py"
    frontend_path = ROOT / "frontend" / "src" / "pages" / "Organization" / "OrganizationCenter.tsx"
    frontend = frontend_path.read_text(encoding="utf-8")
    invite_frontend = (
        ROOT / "frontend" / "src" / "pages" / "Organization" / "OrganizationInviteAccept.tsx"
    ).read_text(encoding="utf-8")
    delegable = _python_string_set(contract_path, "DELEGABLE_CAPABILITIES")
    defaults = _python_string_set(contract_path, "DEFAULT_MEMBER_CAPABILITIES")
    delivery = _python_string_set(contract_path, "DELIVERY_ROLE_CAPABILITIES")
    readonly = _python_string_set(contract_path, "READONLY_ROLE_CAPABILITIES")
    gate.check(_typescript_record_keys(frontend, "CAPABILITY_LABELS") == delegable, "capability labels share backend SSOT")
    gate.check(_typescript_array(frontend, "DEFAULT_ROLE_CAPABILITIES") == defaults, "sales role shares backend SSOT")
    gate.check(_typescript_array(frontend, "DELIVERY_ROLE_CAPABILITIES") == delivery, "delivery role shares backend SSOT")
    gate.check(_typescript_array(frontend, "READONLY_ROLE_CAPABILITIES") == readonly, "readonly role shares backend SSOT")
    gate.check(not ({"writing.generate", "publish.execute"} & defaults), "sales cannot write or publish by default")
    gate.check(not ({"diagnosis.run", "quote.create"} & delivery), "delivery cannot diagnose or quote by default")
    gate.check(not ({"diagnosis.run", "quote.create", "writing.generate", "publish.execute", "monitoring.run"} & readonly), "readonly cannot trigger provider or billable work")
    gate.check("team.output_read" in delegable and "team.outputs.read" not in delegable, "team output capability spelling frozen")
    gate.check(
        "fragment.get('token')" in invite_frontend
        and "params.get('token')" not in invite_frontend
        and "searchParams.get('token')" not in invite_frontend
        and "searchParams.get('token')" not in frontend,
        "organization invite bearer is fragment-only and never accepted from query strings",
    )

    specification = (ROOT / "docs" / "AI-CONTEXT" / "ORGANIZATION_INTERNAL_SEATS_IMPLEMENTATION_SPEC_2026-07-20.md").read_text(encoding="utf-8")
    machine_contract = (ROOT / "docs" / "AI-CONTEXT" / "ORGANIZATION_INTERNAL_SEATS_BATCH0_2026-07-20" / "MACHINE_CONTRACT_DRAFT.md").read_text(encoding="utf-8")
    owner_override = (ROOT / "docs" / "AI-CONTEXT" / "ORGANIZATION_INTERNAL_SEATS_OWNER_LAUNCH_OVERRIDE_2026-07-22.md").read_text(encoding="utf-8")
    for capability in sorted(delegable):
        # The signed implementation specification is immutable (its original
        # SHA-256 is a hard gate). Review-discovered capability splits may be
        # recorded in the machine contract without rewriting that signed file.
        documented = (
            capability in machine_contract and capability in specification
        ) or capability in owner_override
        gate.check(documented, f"capability documented: {capability}")

    from services.organization_contract import (
        BASE_REANCHOR_REQUIRED,
        CONTRACT_FREEZE_SHA as RUNTIME_FREEZE_SHA,
        SEMANTIC_DEVELOPMENT_BASE_SHA as RUNTIME_BASE_SHA,
        OrganizationError,
        feature_flags,
    )
    gate.check(BASE_REANCHOR_REQUIRED and RUNTIME_BASE_SHA == SEMANTIC_BASE_SHA and RUNTIME_FREEZE_SHA == FREEZE_SHA, "runtime delivery metadata stays reanchor-required")

    previous = {key: os.environ.get(key) for key in (
        "ORGANIZATION_SEATS_ENABLED",
        "ORGANIZATION_INVITE_ONBOARDING_ENABLED",
        "ORGANIZATION_SHARED_PAYER_ENABLED",
        "ORGANIZATION_EXTERNAL_ACTIONS_ENABLED",
    )}
    try:
        os.environ.update({
            "ORGANIZATION_SEATS_ENABLED": "false",
            "ORGANIZATION_INVITE_ONBOARDING_ENABLED": "false",
            "ORGANIZATION_SHARED_PAYER_ENABLED": "false",
            "ORGANIZATION_EXTERNAL_ACTIONS_ENABLED": "false",
        })
        gate.check(not any(feature_flags().values()), "all organization flags default fail closed")
        os.environ["ORGANIZATION_INVITE_ONBOARDING_ENABLED"] = "true"
        try:
            feature_flags()
        except OrganizationError as exc:
            gate.check(exc.code == "ORG_FLAG_DEPENDENCY_INVALID", "invite onboarding requires seats flag")
        else:
            raise AssertionError("invite onboarding dependency accepted")
        os.environ["ORGANIZATION_INVITE_ONBOARDING_ENABLED"] = "false"
        os.environ["ORGANIZATION_SHARED_PAYER_ENABLED"] = "true"
        try:
            feature_flags()
        except OrganizationError as exc:
            gate.check(exc.code == "ORG_FLAG_DEPENDENCY_INVALID", "shared payer requires seats flag")
        else:
            raise AssertionError("shared payer dependency accepted")
        os.environ.update({
            "ORGANIZATION_SEATS_ENABLED": "true",
            "ORGANIZATION_SHARED_PAYER_ENABLED": "false",
            "ORGANIZATION_EXTERNAL_ACTIONS_ENABLED": "true",
        })
        try:
            feature_flags()
        except OrganizationError as exc:
            gate.check(exc.code == "ORG_FLAG_DEPENDENCY_INVALID", "external actions require shared payer")
        else:
            raise AssertionError("external dependency accepted")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    gate.check(
        _git("merge-base", "--is-ancestor", SEMANTIC_BASE_SHA, "HEAD") == "",
        "semantic development base is ancestor",
    )
    gate.check(_git("merge-base", "--is-ancestor", FREEZE_SHA, "HEAD") == "", "contract freeze is ancestor")
    forbidden_diff = _git("diff", "--name-only", SEMANTIC_BASE_SHA, "--", *FORBIDDEN_FILES)
    gate.check(not forbidden_diff, "forbidden auth and connection files unchanged", forbidden_diff)
    incremental_billing_diff = _git("diff", "--name-only", FREEZE_SHA, "--", "middleware/billing.py")
    gate.check(not incremental_billing_diff, "new onboarding batch leaves billing redline unchanged", incremental_billing_diff)

    organization_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "services").glob("organization_*.py"))
    )
    gate.check("charge_on_success(" not in organization_sources, "organization services never use charge_on_success")
    gate.check("deduct_points(" not in organization_sources, "organization services never use legacy post-charge")

    schema = _openapi()
    operation_ids: list[str] = []
    for methods in schema["paths"].values():
        for method in methods.values():
            if isinstance(method, dict) and method.get("operationId"):
                operation_ids.append(str(method["operationId"]))
    gate.check(len(schema["paths"]) >= 36, "organization OpenAPI path census", len(schema["paths"]))
    gate.check(len(operation_ids) == len(set(operation_ids)), "organization OpenAPI operation ids unique")
    required_paths = {
        "/api/organization",
        "/api/organization/work/reservations",
        "/api/organization/approval-policies",
        "/api/organization/artifacts/shares/public",
        "/api/public/organization/links/validate",
        "/api/public/organization/invites/inspect",
        "/api/public/organization/invites/verification-challenges",
        "/api/public/organization/invites/verification-challenges/{challenge_id}/verify",
        "/api/public/organization/invites/onboard",
        "/api/admin/organization-product-config",
    }
    gate.check(required_paths <= set(schema["paths"]), "critical organization OpenAPI paths wired", sorted(required_paths - set(schema["paths"])))
    print(json.dumps({"summary": {"passed": len(gate.passed), "failed": 0, "skipped": 0}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"summary": {"failed": 1, "skipped": 0}, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
