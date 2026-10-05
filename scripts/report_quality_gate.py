#!/usr/bin/env python3
"""Gate GEO report markdown/html quality and emit JSON diagnostics."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


TEXT_EXTENSIONS = {".md", ".markdown", ".html", ".htm", ".txt"}

TARGET_HEADINGS = (
    "GEO评分详解",
    "GEO评分总览",
    "GEO优化策略",
)

BANNED_SLOGANS = [
    ("500+客户", re.compile(r"500\s*\+\s*客户")),
    ("平均提升40分", re.compile(r"平均\s*提升\s*40\s*分")),
    ("市场规模320亿", re.compile(r"市场规模\s*320\s*亿")),
]

BANNED_VISIBLE_TERMS = [
    ("待开发", re.compile(r"待开发")),
    ("RFC", re.compile(r"\bRFC\b", re.IGNORECASE)),
    ("M1b 后启用", re.compile(r"M1b\s*后启用", re.IGNORECASE)),
    ("v1", re.compile(r"(?<![A-Za-z0-9])v1(?![A-Za-z0-9])", re.IGNORECASE)),
    ("老页", re.compile(r"老页")),
    ("专业版", re.compile(r"专业版")),
]

METRIC_EXPLANATION_RE = re.compile(
    r"口径|指标定义|定义说明|分别|拆分|分场景|"
    r"品牌认知检出率|本地获客推荐率|高转化场景推荐率|"
    # CTO-B 2026-04-26 W2/W3 实际术语 · v2 报告用"品牌认知词"等(更口语化)
    r"品牌认知词|本地获客词|高转化场景词|"
    r"关键词推荐率分层|3\s*层|推荐率分层"
)


@dataclass
class Location:
    line: int
    text: str


@dataclass
class Issue:
    file: str
    rule: str
    severity: str
    message: str
    evidence: list[str]
    locations: list[Location]


def strip_html_tags(text: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</h[1-6]>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def normalize_heading(raw: str) -> str:
    text = strip_html_tags(raw)
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    text = re.sub(r"^\s*[一二三四五六七八九十0-9]+[、.．]\s*", "", text)
    text = re.sub(r"[:：\s]+$", "", text)
    return re.sub(r"\s+", "", text.strip())


def collect_heading_locations(lines: list[str]) -> dict[str, list[Location]]:
    found: dict[str, list[Location]] = {h: [] for h in TARGET_HEADINGS}
    html_heading_re = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.IGNORECASE)

    for idx, line in enumerate(lines, start=1):
        candidates: list[str] = []
        if re.match(r"^\s*#{1,6}\s+", line):
            candidates.append(line)
        candidates.extend(match.group(1) for match in html_heading_re.finditer(line))
        if any(h in line for h in TARGET_HEADINGS):
            candidates.append(line)

        seen_targets_on_line: set[str] = set()
        for candidate in candidates:
            normalized = normalize_heading(candidate)
            for target in TARGET_HEADINGS:
                if normalized == target and target not in seen_targets_on_line:
                    found[target].append(Location(idx, line.strip()[:240]))
                    seen_targets_on_line.add(target)
    return found


def line_locations_for_regex(lines: list[str], regex: re.Pattern[str], limit: int = 20) -> list[Location]:
    locations: list[Location] = []
    for idx, line in enumerate(lines, start=1):
        if regex.search(line):
            locations.append(Location(idx, line.strip()[:240]))
            if len(locations) >= limit:
                break
    return locations


def redact_non_visible_payloads(text: str) -> str:
    """Remove HTML attributes/data payloads that can contain base64-like false positives."""
    text = re.sub(r"(?is)<script.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"data:[^'\"\s>)]+", " ", text)
    text = re.sub(r"\b[A-Za-z0-9+/=_-]{80,}\b", " ", text)
    return text


def visible_line_locations_for_regex(lines: list[str], regex: re.Pattern[str], limit: int = 20) -> list[Location]:
    """Search user-visible line text while preserving original line numbers for diagnostics."""
    locations: list[Location] = []
    for idx, line in enumerate(lines, start=1):
        visible = strip_html_tags(redact_non_visible_payloads(line)).strip()
        if visible and regex.search(visible):
            locations.append(Location(idx, visible[:240]))
            if len(locations) >= limit:
                break
    return locations


def issue_duplicate_headings(file_label: str, lines: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    heading_locations = collect_heading_locations(lines)
    for title, locations in heading_locations.items():
        if len(locations) > 1:
            issues.append(
                Issue(
                    file=file_label,
                    rule="duplicate_heading",
                    severity="error",
                    message=f"重复标题: {title} 出现 {len(locations)} 次",
                    evidence=[title],
                    locations=locations,
                )
            )
    return issues


def find_recommendation_rate_values(text: str) -> list[str]:
    plain = strip_html_tags(text)
    windows = re.findall(r".{0,40}推荐率.{0,60}|.{0,60}推荐率.{0,40}", plain)
    values: list[str] = []
    value_re = re.compile(r"\d+\s*/\s*\d+|\d+(?:\.\d+)?\s*%")
    for window in windows:
        for value in value_re.findall(window):
            normalized = re.sub(r"\s+", "", value)
            if normalized not in values:
                values.append(normalized)
    return values


def issue_metric_conflict(file_label: str, text: str, lines: list[str]) -> list[Issue]:
    values = find_recommendation_rate_values(text)
    if len(values) <= 1:
        return []
    plain = strip_html_tags(text)
    if METRIC_EXPLANATION_RE.search(plain):
        return []

    locations = []
    for value in values:
        pattern = re.compile(re.escape(value).replace("/", r"\s*/\s*").replace("%", r"\s*%"))
        locations.extend(line_locations_for_regex(lines, pattern, limit=5))

    return [
        Issue(
            file=file_label,
            rule="recommendation_rate_conflict",
            severity="error",
            message="同一报告出现多个推荐率口径且未检测到口径解释",
            evidence=values,
            locations=locations[:20],
        )
    ]


def issue_banned_slogans(file_label: str, lines: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    for label, regex in BANNED_SLOGANS:
        locations = line_locations_for_regex(lines, regex)
        if locations:
            issues.append(
                Issue(
                    file=file_label,
                    rule="unsupported_marketing_claim",
                    severity="error",
                    message=f"禁止无证据宣传词: {label}",
                    evidence=[label],
                    locations=locations,
                )
            )
    return issues


def issue_banned_visible_terms(file_label: str, lines: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    for label, regex in BANNED_VISIBLE_TERMS:
        locations = visible_line_locations_for_regex(lines, regex)
        if locations:
            issues.append(
                Issue(
                    file=file_label,
                    rule="forbidden_user_visible_term",
                    severity="error",
                    message=f"报告中不应出现用户可见工程/旧链路词: {label}",
                    evidence=[label],
                    locations=locations,
                )
            )
    return issues


def issue_asset_score_conflict(file_label: str, text: str, lines: list[str]) -> list[Issue]:
    plain = strip_html_tags(text)
    full_asset_re = re.compile(
        r"网页内容资产.{0,80}(?:25\s*/\s*25|满分|100\s*%)|(?:25\s*/\s*25|满分|100\s*%).{0,80}网页内容资产",
        re.DOTALL,
    )
    zero_authority_re = re.compile(
        r"(?:权威引用|品牌基础|品牌直接提及|品牌直引).{0,40}(?:为\s*)?(?:0|零|无)|(?:0|零|无).{0,40}(?:权威引用|品牌基础|品牌直接提及|品牌直引)",
        re.DOTALL,
    )
    if not full_asset_re.search(plain) or not zero_authority_re.search(plain):
        return []

    asset_locations = line_locations_for_regex(lines, re.compile(r"网页内容资产"))
    zero_locations = line_locations_for_regex(lines, re.compile(r"权威引用|品牌基础|品牌直接提及|品牌直引"))
    return [
        Issue(
            file=file_label,
            rule="web_asset_full_score_authority_zero_conflict",
            severity="error",
            message="网页内容资产满分与权威引用/品牌基础为 0 存在冲突",
            evidence=["网页内容资产满分", "权威引用/品牌基础为 0"],
            locations=(asset_locations + zero_locations)[:20],
        )
    ]


def analyze_text(file_label: str, text: str) -> list[Issue]:
    lines = text.splitlines()
    issues: list[Issue] = []
    issues.extend(issue_duplicate_headings(file_label, lines))
    issues.extend(issue_metric_conflict(file_label, text, lines))
    issues.extend(issue_banned_slogans(file_label, lines))
    issues.extend(issue_banned_visible_terms(file_label, lines))
    issues.extend(issue_asset_score_conflict(file_label, text, lines))
    return issues


def iter_input_files(paths: Iterable[str]) -> Iterable[Path]:
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.is_file() and child.suffix.lower() in TEXT_EXTENSIONS:
                    yield child
        elif p.is_file():
            yield p
        else:
            raise FileNotFoundError(raw)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check GEO report markdown/html for duplicate headings, metric conflicts, unsupported claims, and old-route copy."
    )
    parser.add_argument("paths", nargs="*", help="Report files or directories. Use stdin when no paths are provided.")
    parser.add_argument("--stdin-name", default="<stdin>", help="Label to use when reading report text from stdin.")
    parser.add_argument("--json-output", help="Write JSON report to this path as well as stdout.")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 even when issues are found.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    all_issues: list[Issue] = []
    scanned: list[str] = []

    try:
        if args.paths:
            for file_path in iter_input_files(args.paths):
                text = file_path.read_text(encoding="utf-8", errors="replace")
                scanned.append(str(file_path))
                all_issues.extend(analyze_text(str(file_path), text))
        else:
            if sys.stdin.isatty():
                raise ValueError("No input paths provided and stdin is empty.")
            text = sys.stdin.read()
            scanned.append(args.stdin_name)
            all_issues.extend(analyze_text(args.stdin_name, text))
    except Exception as exc:  # noqa: BLE001 - CLI should serialize failures.
        report = {
            "passed": False,
            "error": str(exc),
            "scanned": scanned,
            "summary": {"files": len(scanned), "issues": 1},
            "issues": [],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    issue_dicts = []
    for issue in all_issues:
        item = asdict(issue)
        item["locations"] = [asdict(loc) for loc in issue.locations]
        issue_dicts.append(item)

    report = {
        "passed": len(all_issues) == 0,
        "scanned": scanned,
        "summary": {
            "files": len(scanned),
            "issues": len(all_issues),
            "errors": sum(1 for issue in all_issues if issue.severity == "error"),
            "warnings": sum(1 for issue in all_issues if issue.severity == "warning"),
        },
        "issues": issue_dicts,
    }
    output = json.dumps(report, ensure_ascii=False, indent=2)
    print(output)
    if args.json_output:
        Path(args.json_output).write_text(output + "\n", encoding="utf-8")
    return 0 if args.no_fail or not all_issues else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
