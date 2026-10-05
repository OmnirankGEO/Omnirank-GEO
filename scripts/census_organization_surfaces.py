"""Generate and verify the organization route/task/frontend surface census."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.organization_route_contract import MEMBER_GEO_ROUTE_POLICIES


OUTPUT = ROOT / "docs" / "SYSTEM_TRUTH" / "ORGANIZATION_SURFACE_CENSUS.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "websocket"}
TASK_NAME = re.compile(
    r"(?:task|worker|scheduler|schedule|callback|webhook|recover|recovery|sweep|tick|heartbeat|outbox|occurrence|poll)",
    re.IGNORECASE,
)
FRONTEND_API = re.compile(r"[\"'`](?P<path>/api/[A-Za-z0-9_./:?-]+)")


def canonical_route_template(path: str) -> str:
    return re.sub(r"\{[^{}]+\}", "{}", path)


MEMBER_ROUTE_TEMPLATES = {
    (policy.method, canonical_route_template(policy.path_template))
    for policy in MEMBER_GEO_ROUTE_POLICIES
}


def literal_string(node: ast.AST | None, default: str = "") -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return default


def source_line(path: Path, line: int) -> str:
    try:
        return path.read_text(encoding="utf-8").splitlines()[line - 1].strip()
    except (IndexError, OSError, UnicodeError):
        return ""


def classify(path: str, method: str, handler: str, kind: str) -> dict[str, Any]:
    value = f"{path} {handler}".lower()
    if "organization" in value:
        module = "organization"
    elif any(word in value for word in ("diagnos", "report")):
        module = "diagnosis_reports"
    elif any(word in value for word in ("quote", "pricing", "keyword")):
        module = "quote_pricing"
    elif any(word in value for word in ("article", "writing", "content")):
        module = "writing"
    elif any(word in value for word in ("publish", "media", "placement")):
        module = "publishing"
    elif "monitor" in value:
        module = "monitoring"
    elif any(word in value for word in ("material", "intake", "brand", "client")):
        module = "clients_materials"
    elif any(word in value for word in ("wallet", "billing", "recharge", "refund", "settle", "withdraw", "inventory")):
        module = "funds_commerce"
    elif any(word in value for word in ("auth", "user", "role", "permission")):
        module = "identity"
    else:
        module = "other"
    read = method in {"GET", "HEAD", "OPTIONS", "WEBSOCKET"}
    spends = any(word in value for word in ("generate", "diagnos", "monitor", "publish", "charge", "recharge", "top-up", "execute"))
    external = any(word in value for word in ("public", "share", "portal", "publish", "webhook", "callback", "send", "resend"))
    signs_token = "token" in value or any(word in value for word in ("public", "share", "portal", "intake"))
    is_contracted_member_route = (
        method,
        canonical_route_template(path),
    ) in MEMBER_ROUTE_TEMPLATES
    if is_contracted_member_route or path.startswith("/api/organization") or "organization_" in handler:
        member_mode = "organization_native"
    elif path.startswith("/api/public/organization"):
        member_mode = "public_token_live_check"
    elif kind in {"background_task", "scheduled_function", "task_dispatch"}:
        member_mode = "actor_snapshot_required_or_fail_closed"
    else:
        member_mode = "legacy_owner_only_member_fail_closed"
    return {
        "module": module,
        "action": "read" if read else "write",
        "client_scope": "assigned_or_owner" if module not in {"identity", "other"} else "not_applicable_or_handler_specific",
        "object_scope": "organization_actor_owned" if member_mode == "organization_native" else "legacy_handler_specific",
        "actor_ownership": "owner_member_system_explicit" if member_mode == "organization_native" else "legacy_owner_or_fail_closed",
        "spends": spends,
        "external_side_effect": external,
        "signs_or_accepts_token": signs_token,
        "approval_required": external and not read,
        "member_mode": member_mode,
    }


def backend_surfaces() -> list[dict[str, Any]]:
    surfaces: list[dict[str, Any]] = []
    files = sorted((ROOT / "api").glob("*.py")) + [ROOT / "server.py"]
    for file in files:
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (OSError, UnicodeError, SyntaxError):
            continue
        prefixes: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "APIRouter":
                    prefix = ""
                    for keyword in value.keywords:
                        if keyword.arg == "prefix":
                            prefix = literal_string(keyword.value)
                    for target in targets:
                        if isinstance(target, ast.Name):
                            prefixes[target.id] = prefix
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                        continue
                    method = decorator.func.attr.lower()
                    owner = decorator.func.value.id if isinstance(decorator.func.value, ast.Name) else ""
                    if method not in HTTP_METHODS or owner not in {*prefixes, "app", "router"}:
                        continue
                    route = literal_string(decorator.args[0] if decorator.args else None)
                    full_path = f"{prefixes.get(owner, '')}{route}" or "<dynamic>"
                    item = {
                        "kind": "websocket_route" if method == "websocket" else "http_route",
                        "source": file.relative_to(ROOT).as_posix(),
                        "line": int(node.lineno),
                        "method": "WEBSOCKET" if method == "websocket" else method.upper(),
                        "path": full_path,
                        "handler": node.name,
                    }
                    item.update(classify(full_path, item["method"], node.name, item["kind"]))
                    surfaces.append(item)
                if TASK_NAME.search(node.name) and not node.decorator_list:
                    item = {
                        "kind": "scheduled_function",
                        "source": file.relative_to(ROOT).as_posix(),
                        "line": int(node.lineno),
                        "method": "INTERNAL",
                        "path": "<background>",
                        "handler": node.name,
                    }
                    item.update(classify(item["path"], item["method"], node.name, item["kind"]))
                    surfaces.append(item)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if attr not in {"add_task", "create_task", "add_job", "submit"}:
                    continue
                target = "<dynamic>"
                if node.args:
                    argument = node.args[0]
                    if isinstance(argument, ast.Name):
                        target = argument.id
                    elif isinstance(argument, ast.Attribute):
                        target = argument.attr
                item = {
                    "kind": "task_dispatch",
                    "source": file.relative_to(ROOT).as_posix(),
                    "line": int(node.lineno),
                    "method": "INTERNAL",
                    "path": "<background>",
                    "handler": f"{attr}:{target}",
                }
                item.update(classify(item["path"], item["method"], item["handler"], item["kind"]))
                surfaces.append(item)
    return surfaces


def worker_surfaces() -> list[dict[str, Any]]:
    surfaces: list[dict[str, Any]] = []
    roots = [ROOT / "services", ROOT / "workflows", ROOT / "workers", ROOT / "tasks"]
    for directory in roots:
        if not directory.exists():
            continue
        for file in sorted(directory.rglob("*.py")):
            try:
                tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
            except (OSError, UnicodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not TASK_NAME.search(node.name):
                    continue
                item = {
                    "kind": "background_task",
                    "source": file.relative_to(ROOT).as_posix(),
                    "line": int(node.lineno),
                    "method": "INTERNAL",
                    "path": "<background>",
                    "handler": node.name,
                }
                item.update(classify(item["path"], item["method"], node.name, item["kind"]))
                surfaces.append(item)
    return surfaces


def frontend_surfaces() -> list[dict[str, Any]]:
    surfaces: list[dict[str, Any]] = []
    source_root = ROOT / "frontend" / "src"
    for file in sorted(source_root.rglob("*")):
        if file.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for match in FRONTEND_API.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            path = match.group("path")
            item = {
                "kind": "frontend_api_call",
                "source": file.relative_to(ROOT).as_posix(),
                "line": line,
                "method": "CALLSITE",
                "path": path,
                "handler": source_line(file, line)[:240],
            }
            item.update(classify(path, item["method"], item["handler"], item["kind"]))
            surfaces.append(item)
    return surfaces


def generate() -> dict[str, Any]:
    backend = backend_surfaces()
    live_member_routes = {
        (item["method"], canonical_route_template(item["path"]))
        for item in backend
        if item["kind"] in {"http_route", "websocket_route"}
    }
    missing_member_routes = sorted(MEMBER_ROUTE_TEMPLATES - live_member_routes)
    if missing_member_routes:
        raise RuntimeError(
            "organization member route contract has no live decorator: "
            + json.dumps(missing_member_routes, ensure_ascii=False)
        )
    misclassified_member_routes = [
        {
            "method": item["method"],
            "path": item["path"],
            "source": item["source"],
            "line": item["line"],
        }
        for item in backend
        if (item["method"], canonical_route_template(item["path"])) in MEMBER_ROUTE_TEMPLATES
        and item["member_mode"] != "organization_native"
    ]
    if misclassified_member_routes:
        raise RuntimeError(
            "organization member route contract was not classified as native: "
            + json.dumps(misclassified_member_routes, ensure_ascii=False)
        )
    surfaces = backend + worker_surfaces() + frontend_surfaces()
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in surfaces:
        key = (item["kind"], item["source"], item["line"], item["method"], item["path"], item["handler"])
        unique[key] = item
    ordered = sorted(unique.values(), key=lambda item: (item["kind"], item["source"], item["line"], item["path"]))
    kinds = Counter(item["kind"] for item in ordered)
    modes = Counter(item["member_mode"] for item in ordered)
    return {
        "contract": "organization_internal_seats_all_accounts_2026_07_22_v3",
        "semantic_development_base_sha": "dc02115cb2c2b977f5d56ea38a9dea1fe425a4a0",
        "base_reanchor_required": True,
        "contract_freeze_commit": "00bf26e642e63de3984af86249b122ac171641ab",
        "generator": "scripts/census_organization_surfaces.py",
        "scope": ["api/*.py", "server.py", "services/**/*.py", "workflows/**/*.py", "workers/**/*.py", "tasks/**/*.py", "frontend/src/**/*.{ts,tsx,js,jsx}"],
        "counts": {"total": len(ordered), "by_kind": dict(sorted(kinds.items())), "by_member_mode": dict(sorted(modes.items()))},
        "surface_enumeration_proof": "--check recomputes the AST/TS callsite census and requires byte-equivalent canonical JSON",
        "member_route_contract_proof": {
            "declared_routes": len(MEMBER_ROUTE_TEMPLATES),
            "missing_live_decorators": [],
            "misclassified_live_decorators": [],
            "classification_source": "services.organization_route_contract.MEMBER_GEO_ROUTE_POLICIES",
        },
        "surfaces": ordered,
    }


def canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = canonical(generate())
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print("organization surface census is stale", file=sys.stderr)
            return 1
        print(json.dumps({"status": "passed", "failed": 0, "skipped": 0, "output": str(OUTPUT)}, ensure_ascii=False))
        return 0
    OUTPUT.write_bytes(rendered.encode("utf-8"))
    print(json.dumps({"status": "generated", "output": str(OUTPUT), "count": generate()["counts"]["total"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
