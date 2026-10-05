"""Dry-run-first advisor public identity migration.

Mapping file shape:

{
  "advisors": [
    {
      "advisor_id": "luxury-car-xz",
      "source_name": "骐哥教你做租车",
      "public_name": "豪车租赁专家小臻",
      "identity_status": "legal_approved",
      "identity_notes": "approved by owner/legal"
    }
  ]
}

The script scans text fields and JSONB fields without stringifying JSON in place.
It only writes data with --apply, and --apply requires legal_approved mappings.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


IDENTITY_STATUSES = {"draft", "owner_approved", "legal_approved", "blocked"}
TEXT_COLUMNS = ("name", "description", "base_prompt", "specialty", "credentials", "greeting")
JSONB_COLUMNS = ("industries", "tags", "quick_questions")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_identity_mappings(raw: Any) -> list[dict[str, Any]]:
    """Accept either {"advisors": [...]} or {"advisor_id": {...}} mapping files."""
    if isinstance(raw, Mapping) and isinstance(raw.get("advisors"), list):
        items = raw["advisors"]
    elif isinstance(raw, Mapping):
        items = []
        for advisor_id, item in raw.items():
            if not isinstance(item, Mapping):
                continue
            merged = dict(item)
            merged.setdefault("advisor_id", advisor_id)
            items.append(merged)
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        normalized.append({
            "advisor_id": str(item.get("advisor_id") or item.get("id") or "").strip(),
            "source_name": str(item.get("source_name") or "").strip(),
            "public_name": str(item.get("public_name") or item.get("name") or "").strip(),
            "identity_status": str(item.get("identity_status") or "draft").strip(),
            "identity_notes": str(item.get("identity_notes") or item.get("notes") or "").strip(),
        })
    return normalized


def validate_identity_mappings(mappings: Iterable[Mapping[str, Any]], *, require_approved: bool = False) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(mappings):
        advisor_id = str(item.get("advisor_id") or "").strip()
        label = advisor_id or f"row#{idx + 1}"
        source_name = str(item.get("source_name") or "").strip()
        public_name = str(item.get("public_name") or "").strip()
        status = str(item.get("identity_status") or "draft").strip()
        if not advisor_id:
            errors.append(f"{label}: advisor_id is required")
        elif advisor_id in seen:
            errors.append(f"{label}: duplicate advisor_id")
        seen.add(advisor_id)
        if not source_name:
            errors.append(f"{label}: source_name is required")
        if not public_name:
            errors.append(f"{label}: public_name is required")
        if source_name and public_name and source_name == public_name:
            errors.append(f"{label}: public_name must differ from source_name")
        if status not in IDENTITY_STATUSES:
            errors.append(f"{label}: identity_status must be one of {sorted(IDENTITY_STATUSES)}")
        if require_approved and status != "legal_approved":
            errors.append(f"{label}: --apply requires identity_status=legal_approved")
    return errors


def scan_value_for_sources(value: Any, source_names: Iterable[str], *, path: str) -> list[dict[str, str]]:
    """Scan str/list/dict values while preserving JSONB paths."""
    hits: list[dict[str, str]] = []
    if isinstance(value, str):
        for source_name in source_names:
            source = str(source_name or "").strip()
            if source and source in value:
                hits.append({"path": path, "source_name": source, "excerpt": value[:180]})
        return hits
    if isinstance(value, Mapping):
        for key, child in value.items():
            hits.extend(scan_value_for_sources(child, source_names, path=f"{path}.{key}"))
        return hits
    if isinstance(value, list):
        for idx, child in enumerate(value):
            hits.extend(scan_value_for_sources(child, source_names, path=f"{path}[{idx}]"))
        return hits
    return hits


def replace_sources_in_value(value: Any, replacements: Mapping[str, str]) -> Any:
    """Recursively replace source names without destroying JSON shape."""
    if isinstance(value, str):
        result = value
        for source, public in replacements.items():
            if source:
                result = result.replace(source, public)
        return result
    if isinstance(value, list):
        return [replace_sources_in_value(item, replacements) for item in value]
    if isinstance(value, Mapping):
        return {key: replace_sources_in_value(child, replacements) for key, child in value.items()}
    return value


def load_mapping(path: Path) -> list[dict[str, Any]]:
    return normalize_identity_mappings(json.loads(path.read_text(encoding="utf-8")))


def ensure_identity_schema(conn) -> None:
    cur = conn.cursor()
    for column, column_type in [
        ("public_name", "TEXT"),
        ("source_name", "TEXT"),
        ("identity_status", "TEXT DEFAULT 'draft'"),
        ("identity_notes", "TEXT DEFAULT ''"),
        ("identity_updated_at", "TIMESTAMP"),
    ]:
        cur.execute(f"ALTER TABLE advisors ADD COLUMN IF NOT EXISTS {column} {column_type}")
    conn.commit()


def fetch_advisor_rows(conn, advisor_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = [item for item in advisor_ids if item]
    if not ids:
        return []
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, {", ".join(TEXT_COLUMNS)}, {", ".join(JSONB_COLUMNS)},
               public_name, source_name, identity_status, identity_notes
        FROM advisors
        WHERE id = ANY(%s)
        """,
        (ids,),
    )
    return [dict(row) for row in cur.fetchall()]


def audit_advisor_rows(rows: Iterable[Mapping[str, Any]], mappings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id = {item["advisor_id"]: item for item in mappings}
    hits: list[dict[str, Any]] = []
    for row in rows:
        advisor_id = row["id"]
        mapping = by_id.get(advisor_id)
        if not mapping:
            continue
        source_names = [mapping["source_name"]]
        for column in TEXT_COLUMNS + JSONB_COLUMNS:
            for hit in scan_value_for_sources(row.get(column), source_names, path=f"advisors.{advisor_id}.{column}"):
                hits.append({"advisor_id": advisor_id, "column": column, **hit})
    return hits


def fetch_grouped_review_summary(conn, mappings: Iterable[Mapping[str, Any]], *, limit: int = 50) -> dict[str, Any]:
    """Group large knowledge/message reviews instead of asking humans to inspect every row."""
    sources = [m["source_name"] for m in mappings if m.get("source_name")]
    result = {"knowledge_chunks": [], "advisor_messages": []}
    cur = conn.cursor()
    for source in sources:
        like = f"%{source}%"
        cur.execute(
            """
            SELECT advisor_id, COUNT(*) AS count, MIN(chunk_id) AS sample_id
            FROM knowledge_chunks
            WHERE content ILIKE %s
            GROUP BY advisor_id
            ORDER BY count DESC
            LIMIT %s
            """,
            (like, limit),
        )
        for row in cur.fetchall():
            result["knowledge_chunks"].append({"source_name": source, **dict(row)})
        cur.execute(
            """
            SELECT c.advisor_id, COUNT(*) AS count, MIN(m.id) AS sample_id
            FROM advisor_messages m
            JOIN advisor_conversations c ON c.conversation_id = m.conversation_id
            WHERE m.content ILIKE %s
            GROUP BY c.advisor_id
            ORDER BY count DESC
            LIMIT %s
            """,
            (like, limit),
        )
        for row in cur.fetchall():
            result["advisor_messages"].append({"source_name": source, **dict(row)})
    return result


def apply_advisor_mappings(conn, rows: Iterable[Mapping[str, Any]], mappings: Iterable[Mapping[str, Any]]) -> int:
    by_id = {item["advisor_id"]: item for item in mappings}
    updated = 0
    cur = conn.cursor()
    for row in rows:
        advisor_id = row["id"]
        mapping = by_id.get(advisor_id)
        if not mapping:
            continue
        replacements = {mapping["source_name"]: mapping["public_name"]}
        set_parts = [
            "name = %s",
            "public_name = %s",
            "source_name = %s",
            "identity_status = %s",
            "identity_notes = %s",
            "identity_updated_at = CURRENT_TIMESTAMP",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        values: list[Any] = [
            mapping["public_name"],
            mapping["public_name"],
            mapping["source_name"],
            mapping["identity_status"],
            mapping.get("identity_notes") or "",
        ]
        for column in TEXT_COLUMNS:
            if column == "name":
                continue
            set_parts.append(f"{column} = %s")
            values.append(replace_sources_in_value(row.get(column), replacements))
        for column in JSONB_COLUMNS:
            set_parts.append(f"{column} = %s::jsonb")
            values.append(json.dumps(replace_sources_in_value(row.get(column), replacements), ensure_ascii=False))
        values.append(advisor_id)
        cur.execute(f"UPDATE advisors SET {', '.join(set_parts)} WHERE id = %s", values)
        updated += cur.rowcount
    conn.commit()
    return updated


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    from db.connection import get_connection

    mappings = load_mapping(Path(args.mapping))
    errors = validate_identity_mappings(mappings, require_approved=bool(args.apply))
    if errors:
        raise SystemExit("Mapping validation failed:\n" + "\n".join(f"- {err}" for err in errors))

    output_dir = Path(args.output_dir or "agent-test-artifacts/advisor-identity")
    stamp = _now_stamp()
    conn = get_connection()
    try:
        ensure_identity_schema(conn)
        rows = fetch_advisor_rows(conn, [m["advisor_id"] for m in mappings])
        advisor_hits = audit_advisor_rows(rows, mappings)
        grouped_review = fetch_grouped_review_summary(conn, mappings, limit=int(args.review_limit or 50))
        report = {
            "generated_at": stamp,
            "mode": "apply" if args.apply else "dry-run",
            "mapping_count": len(mappings),
            "matched_advisor_count": len(rows),
            "missing_advisor_ids": sorted(set(m["advisor_id"] for m in mappings) - set(row["id"] for row in rows)),
            "advisor_field_hits": advisor_hits,
            "grouped_review": grouped_review,
        }
        write_json(output_dir / f"advisor_identity_audit_{stamp}.json", report)
        if args.apply:
            snapshot = {"generated_at": stamp, "advisors": rows, "report": report}
            write_json(output_dir / f"advisor_identity_rollback_{stamp}.json", snapshot)
            report["updated_advisors"] = apply_advisor_mappings(conn, rows, mappings)
            write_json(output_dir / f"advisor_identity_apply_{stamp}.json", report)
        return report
    finally:
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit/apply advisor public identity mappings")
    parser.add_argument("--mapping", required=True, help="Path to advisor public identity mapping JSON")
    parser.add_argument("--output-dir", default="agent-test-artifacts/advisor-identity")
    parser.add_argument("--review-limit", type=int, default=50)
    parser.add_argument("--apply", action="store_true", help="Apply updates; requires legal_approved mappings")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    report = run(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
