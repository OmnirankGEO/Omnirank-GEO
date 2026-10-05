from pathlib import Path
import ast

from services.article_topic_write_census import (
    EXPECTED_TOPIC_WRITE_OWNERS,
    assert_topic_write_census,
    scan_topic_write_owners,
)


ROOT = Path(__file__).resolve().parents[1]


def test_every_runtime_topic_write_entry_is_in_the_frozen_census():
    assert_topic_write_census(ROOT)
    assert scan_topic_write_owners(ROOT) == EXPECTED_TOPIC_WRITE_OWNERS


def test_census_contains_legacy_generation_and_closed_loop_adapter_owners():
    server_owners = EXPECTED_TOPIC_WRITE_OWNERS["server.py"]
    assert "api_optimize_generate" in server_owners
    assert "api_regenerate_titles" in server_owners
    assert "api_delete_topic" in server_owners
    assert EXPECTED_TOPIC_WRITE_OWNERS["services/article_closed_loop_metadata.py"] == frozenset(
        {"bind_topic_to_slot"}
    )


def _function_source(path: Path, function_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function not found: {path}:{function_name}")


def test_every_runtime_topic_insert_entry_has_the_sticky_slot_adapter():
    insert_owners = (
        (ROOT / "db" / "diagnosis_db.py", "save_topics"),
        (ROOT / "db" / "diagnosis_db.py", "save_topics_batch"),
        (ROOT / "server.py", "_auto_generate_topics_for_new_keyword"),
        (ROOT / "server.py", "api_optimize_generate"),
    )
    for path, owner in insert_owners:
        block = _function_source(path, owner)
        assert "INSERT INTO topics" in block
        assert "bind_topic_to_slot" in block
        assert "slot_aware_v1" in block or owner == "save_topics_batch"
