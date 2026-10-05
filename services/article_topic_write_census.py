"""Machine-readable census of every runtime mutation of ``topics``.

The closed-loop sidecar does not replace these legacy entry points.  This
inventory makes additions fail review until their compatibility behaviour is
explicitly classified.  It deliberately excludes tests and one-off scripts.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Final


TOPIC_WRITE_PATTERN: Final = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+topics\b",
    re.IGNORECASE,
)

# path -> function containing one or more SQL topic writes.  ``<module>`` is
# the leased startup recovery query, not a user generation endpoint.
EXPECTED_TOPIC_WRITE_OWNERS: Final[dict[str, frozenset[str]]] = {
    "server.py": frozenset({
        "<module>",
        "_auto_generate_topics_for_new_keyword",
        "api_apply_distribution",
        "api_delete_topic",
        "api_mark_topic_reviewed",
        "api_optimize_generate",
        "api_optimize_retry",
        "api_regenerate_titles",
        # [census 收敛 2026-07-28] api_reset_to_pending / run_in_thread 已从此处摘除:
        # 这两个函数体内**不再含任何 topics 写 SQL**(AST 实扫 0 处),SQL 已被
        # a79aa832 的重构搬进 services/article_generation_reset.py 与
        # services/article_generation_status.py —— 它们在下方按册子规则登记。
        # 摘除不是放宽:本册是**精确相等**匹配,哪天有人把写回 server.py 这两个函数,
        # 会立刻被判 unexpected 而拦下。
        "api_reset_to_recommended",
        "api_start_articles",
        "api_update_topic",
    }),
    "api/scheduler.py": frozenset({"_topic_writing_watchdog"}),
    "db/diagnosis_db.py": frozenset({
        "mark_topics_write_timeout",
        "regenerate_topic",
        "release_topic_writing",
        "save_topics",
        "save_topics_batch",
        "update_topic_status",
        "update_topic_title",
        "update_topic_user_choice",
    }),
    "services/article_closed_loop_metadata.py": frozenset({"bind_topic_to_slot"}),
    # [census 登记 2026-07-28] 全量重生成的 topics 重置。原先写在 server.py::
    # api_reset_to_pending 里,a79aa832 抽成服务函数(article_generation_reset.py:130
    # `UPDATE topics`),server.py 改为调用方。运行时真路径,非死代码。
    "services/article_generation_reset.py": frozenset({"reset_topics_for_full_regeneration"}),
    # [census 登记 2026-07-28] 生成失败标记与退费状态。原先写在 server.py::
    # run_in_thread 的 except 分支里,同批抽成服务函数
    # (article_generation_status.py:67 / :104 两处 `UPDATE topics`)。
    # server.py 有十余处 import 调用,是活跃运行时路径。
    "services/article_generation_status.py": frozenset({"mark_failure_on_cursor", "set_refund_status"}),
    # [census 登记 2026-07-28 · T5 P0] 标题生成"先落库再生成"的受理凭据。
    # 兼容性归类:三个函数只写**空壳凭据行**(optimized_title IS NULL +
    # generation_operation='title_batch'),不产出成品、不参与交付计数、不碰计费:
    #   reserve_title_slots     进 LLM 前 INSERT status='pending' 受理凭据
    #   fail_title_reservations 失败就地 UPDATE 成 failed + 错误码 + 失败阶段
    #   clear_title_reservations 成功后 DELETE 残留空壳(本次 pending + 前次 failed)
    # 加它们是因为 QZQZ(quote 386)整批标题在 topics 落库**之前**就蒸发了 ——
    # 没有行就没有错误码可写。运行时真路径,由 server.py::api_generate_titles 调用。
    "services/topic_generation_reservation.py": frozenset({
        "clear_title_reservations",
        "fail_title_reservations",
        "reserve_title_slots",
    }),
    "services/optimize_title_jobs.py": frozenset({"_mark_failed", "run_optimize_title_job"}),
    "writing/article_generator_service.py": frozenset({"_save_article", "generate_one", "rewrite_article"}),
}


def _owner(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def scan_topic_write_owners(root: Path) -> dict[str, frozenset[str]]:
    """Return runtime files/functions containing literal SQL topic writes."""
    found: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(("tests/", "scripts/", ".git/", "_tmp/")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeDecodeError):
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if TOPIC_WRITE_PATTERN.search(node.value):
                found.setdefault(relative, set()).add(_owner(node, parents))
    return {path: frozenset(owners) for path, owners in sorted(found.items())}


def assert_topic_write_census(root: Path) -> None:
    actual = scan_topic_write_owners(root)
    if actual != EXPECTED_TOPIC_WRITE_OWNERS:
        missing = {
            path: sorted(owners - actual.get(path, frozenset()))
            for path, owners in EXPECTED_TOPIC_WRITE_OWNERS.items()
            if owners - actual.get(path, frozenset())
        }
        unexpected = {
            path: sorted(owners - EXPECTED_TOPIC_WRITE_OWNERS.get(path, frozenset()))
            for path, owners in actual.items()
            if owners - EXPECTED_TOPIC_WRITE_OWNERS.get(path, frozenset())
        }
        raise RuntimeError(f"topic_write_census_mismatch missing={missing} unexpected={unexpected}")
