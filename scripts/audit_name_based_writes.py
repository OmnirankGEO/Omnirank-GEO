#!/usr/bin/env python3
"""Repository census for mutable-name identity writes.

Destructive writes keyed by a name fail strict mode.  Non-destructive legacy
cache upserts are reported as an explicit P1 until the pricing cache has a full
brand_id namespace; they are not silently presented as fixed.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
DESTRUCTIVE_SQL = re.compile(
    r"\b(?:DELETE\s+FROM\s+(?P<delete_table>[A-Za-z_][\w.]*)|UPDATE\s+(?P<update_table>[A-Za-z_][\w.]*))\b"
    r"(?P<body>.*?)(?=;|\b(?:DELETE\s+FROM|UPDATE\s+[A-Za-z_][\w.]*)\b|$)",
    re.I | re.S,
)
DOMAIN_NAME_PREDICATE = re.compile(r"\b(?:brand_name|project_name|client_name|quote_name)\s*=", re.I)
GENERIC_NAME_PREDICATE = re.compile(r"\bname\s*=", re.I)
IDENTITY_TABLES = {"brands", "clients", "projects", "quotes", "keyword_selection_sessions"}
NAME_UPSERT = re.compile(r"ON\s+CONFLICT\s*\([^)]*(?:brand_name|project_name|client_name|quote_name)[^)]*\)", re.I)


def _sql_literals(path: Path, source: str) -> tuple[list[tuple[int, str]], Optional[str]]:
    """Return complete SQL-like string literals so multiline WHERE clauses are audited."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [], f"{exc.msg} (line {exc.lineno})"
    literals: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = [part.value if isinstance(part, ast.Constant) else "{expression}" for part in node.values]
            literals.append((node.lineno, "".join(str(part) for part in parts)))
    return literals, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict-destructive", action="store_true")
    args = parser.parse_args()
    destructive = []
    p1_upserts = []
    parse_errors = []
    files = [
        path for path in ROOT.rglob("*.py")
        if not any(part in {"tests", ".git", "node_modules", ".venv", "venv"} for part in path.parts)
        and not path.name.endswith("_backup.py")
    ]
    for path in sorted(files):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(ROOT).as_posix()
        literals, parse_error = _sql_literals(path, text)
        if parse_error:
            parse_errors.append({"file": relative, "error": parse_error})
        for line, literal in literals:
            for match in DESTRUCTIVE_SQL.finditer(literal):
                body = match.group("body")
                where = re.search(r"\bWHERE\b(?P<predicate>.*)", body, flags=re.I | re.S)
                if not where:
                    continue
                predicate = re.split(r"\b(?:RETURNING|ORDER\s+BY|LIMIT)\b", where.group("predicate"), maxsplit=1, flags=re.I)[0]
                target_table = (match.group("delete_table") or match.group("update_table") or "").lower().split(".")[-1]
                name_is_identity = bool(
                    DOMAIN_NAME_PREDICATE.search(predicate)
                    or (target_table in IDENTITY_TABLES and GENERIC_NAME_PREDICATE.search(predicate))
                )
                if name_is_identity:
                    destructive.append({
                        "file": relative,
                        "line": line + literal.count("\n", 0, match.start()),
                        "sql": " ".join(match.group(0).split()),
                    })
        for match in NAME_UPSERT.finditer(text):
            p1_upserts.append({
                "file": relative,
                "line": text.count("\n", 0, match.start()) + 1,
                "priority": "P1",
                "code": "QUOTE_CACHE_BRAND_ID_NAMESPACE_REQUIRED",
            })
    report = {
        "files_scanned": len(files),
        "destructive_name_writes": destructive,
        "remaining_name_keyed_upserts": p1_upserts,
        "parse_errors": parse_errors,
        "status": "pass" if not destructive and not parse_errors else "fail",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict_destructive and (destructive or parse_errors) else 0


if __name__ == "__main__":
    sys.exit(main())
