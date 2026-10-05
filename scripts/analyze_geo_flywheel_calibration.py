"""Offline calibration analyzer for GEO flywheel crawler data packages.

The analyzer is deliberately schema-tolerant: crawler exports have changed
shape over time, so it scans JSON/JSONL/CSV/TXT/MD files for answer-like text,
URLs, engines, and [n] adoption markers.

Usage:
  python scripts/analyze_geo_flywheel_calibration.py --data-dir "C:\\path\\AI回答爬虫_数据包_20260526" --markdown
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable
from urllib.parse import urlparse


ANSWER_MARKER_RE = re.compile(r"(?<![A-Za-z0-9_])[\[【](\d{1,3})[\]】]")
CJK_ADJACENT_MARKER_RE = re.compile(r"[\u4e00-\u9fff][\[【]\d{1,3}[\]】]")
URL_RE = re.compile(r"https?://[^\s\"'<>，。；、）)】\]]+")
ANSWER_KEYS = {
    "answer", "response", "content", "text", "output", "final_answer",
    "回答", "答案", "模型回答",
}
ENGINE_KEYS = {"engine", "platform", "model", "provider", "llm", "引擎", "模型", "平台"}
SOURCE_KEYS = {
    "citations", "sources", "references", "search_results", "web_results",
    "links", "引用", "来源", "搜索结果",
}
SHARED_HOSTS = {
    "mp.weixin.qq.com",
    "baijiahao.baidu.com",
    "mbd.baidu.com",
    "toutiao.com",
    "sohu.com",
    "163.com",
    "weibo.com",
    "m.weibo.cn",
}


@dataclass
class AnswerRecord:
    file_path: str
    engine: str = "unknown"
    answer_text: str = ""
    source_urls: list[str] = field(default_factory=list)


def _normalize_engine(value: Any, fallback: str = "unknown", *, allow_custom: bool = True) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    if "doubao" in text or "豆包" in text:
        return "doubao"
    if "deepseek" in text or "深度求索" in text:
        return "deepseek"
    if "qwen" in text or "通义" in text or "千问" in text:
        return "qwen"
    if "kimi" in text or "moonshot" in text:
        return "kimi"
    return text[:80] if allow_custom else fallback


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _extract_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        urls.extend(URL_RE.findall(value))
    elif isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in {"url", "uri", "source_url", "link"} and child:
                urls.append(str(child))
            else:
                urls.extend(_extract_urls(child))
    elif isinstance(value, list):
        for child in value:
            urls.extend(_extract_urls(child))
    return list(dict.fromkeys(urls))


def _strings_for_keys(obj: Any, keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_text = str(key).strip()
            if key_text in keys or key_text.lower() in keys:
                if isinstance(value, str):
                    found.append(value)
                elif not isinstance(value, (dict, list)):
                    found.append(str(value))
            found.extend(_strings_for_keys(value, keys))
    elif isinstance(obj, list):
        for child in obj:
            found.extend(_strings_for_keys(child, keys))
    return found


def _source_lists(obj: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_text = str(key).strip()
            if key_text in SOURCE_KEYS or key_text.lower() in SOURCE_KEYS:
                found.append(value)
            found.extend(_source_lists(value))
    elif isinstance(obj, list):
        for child in obj:
            found.extend(_source_lists(child))
    return found


def _record_from_object(obj: Any, file_path: Path, fallback_engine: str) -> list[AnswerRecord]:
    records: list[AnswerRecord] = []
    if isinstance(obj, dict):
        engine_values = _strings_for_keys(obj, ENGINE_KEYS)
        engine = _normalize_engine(engine_values[0], fallback_engine) if engine_values else fallback_engine
        answers = [s for s in _strings_for_keys(obj, ANSWER_KEYS) if len(s.strip()) >= 20]
        source_urls: list[str] = []
        for source_list in _source_lists(obj):
            source_urls.extend(_extract_urls(source_list))
        if not source_urls:
            source_urls = _extract_urls(obj)
        if answers:
            # Use the longest answer-like string to avoid counting short labels.
            answer = max(answers, key=len)
            records.append(AnswerRecord(str(file_path), engine, answer, list(dict.fromkeys(source_urls))))
        for value in obj.values():
            records.extend(_record_from_object(value, file_path, engine))
    elif isinstance(obj, list):
        for child in obj:
            records.extend(_record_from_object(child, file_path, fallback_engine))
    return records


def _records_from_json_text(text: str, file_path: Path, fallback_engine: str) -> list[AnswerRecord]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        records: list[AnswerRecord] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.extend(_record_from_object(json.loads(line), file_path, fallback_engine))
            except json.JSONDecodeError:
                continue
        return records
    return _record_from_object(data, file_path, fallback_engine)


def _records_from_csv(file_path: Path, fallback_engine: str) -> list[AnswerRecord]:
    records: list[AnswerRecord] = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            engine = fallback_engine
            for key in row:
                if key and (key.strip() in ENGINE_KEYS or key.strip().lower() in ENGINE_KEYS):
                    engine = _normalize_engine(row.get(key), fallback_engine)
                    break
            answer = ""
            for key, value in row.items():
                if key and (key.strip() in ANSWER_KEYS or key.strip().lower() in ANSWER_KEYS):
                    if value and len(value) > len(answer):
                        answer = value
            urls = _extract_urls(row)
            if answer or urls:
                records.append(AnswerRecord(str(file_path), engine, answer, urls))
    return records


def _records_from_text(file_path: Path, fallback_engine: str) -> list[AnswerRecord]:
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    urls = _extract_urls(text)
    if len(text.strip()) < 20 and not urls:
        return []
    return [AnswerRecord(str(file_path), fallback_engine, text[:20000], urls)]


def _infer_engine_from_path(path: Path) -> str:
    return _normalize_engine(
        " ".join(part.lower() for part in path.parts),
        "unknown",
        allow_custom=False,
    )


def collect_records(
    data_dirs: Iterable[Path],
    *,
    max_files: int = 0,
    max_file_mb: float = 20.0,
) -> list[AnswerRecord]:
    records: list[AnswerRecord] = []
    scanned_files = 0
    for data_dir in data_dirs:
        if not data_dir.exists():
            continue
        for file_path in data_dir.rglob("*"):
            if not file_path.is_file():
                continue
            suffix = file_path.suffix.lower()
            if suffix not in {".json", ".jsonl", ".csv", ".txt", ".md"}:
                continue
            if max_files and scanned_files >= max_files:
                return records
            try:
                if file_path.stat().st_size > max_file_mb * 1024 * 1024:
                    continue
            except OSError:
                continue
            scanned_files += 1
            fallback_engine = _infer_engine_from_path(file_path)
            try:
                if suffix in {".json", ".jsonl"}:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                    records.extend(_records_from_json_text(text, file_path, fallback_engine))
                elif suffix == ".csv":
                    records.extend(_records_from_csv(file_path, fallback_engine))
                else:
                    records.extend(_records_from_text(file_path, fallback_engine))
            except OSError:
                continue
    return records


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return float(ordered[index])


def summarize(records: list[AnswerRecord]) -> dict[str, Any]:
    by_engine: dict[str, list[AnswerRecord]] = defaultdict(list)
    domain_counts: Counter[str] = Counter()
    shared_domain_counts: Counter[str] = Counter()
    marker_count = 0
    cjk_adjacent_count = 0
    code_index_like_count = 0

    for record in records:
        by_engine[record.engine].append(record)
        marker_count += len(ANSWER_MARKER_RE.findall(record.answer_text))
        cjk_adjacent_count += len(CJK_ADJACENT_MARKER_RE.findall(record.answer_text))
        code_index_like_count += len(re.findall(r"[A-Za-z_][A-Za-z0-9_]*\[\d{1,3}\]", record.answer_text))
        for url in record.source_urls:
            domain = _domain(url)
            if not domain:
                continue
            domain_counts[domain] += 1
            if domain in SHARED_HOSTS:
                shared_domain_counts[domain] += 1

    engine_summary: dict[str, Any] = {}
    for engine, engine_records in sorted(by_engine.items()):
        source_counts = [len(r.source_urls) for r in engine_records]
        engine_summary[engine] = {
            "records": len(engine_records),
            "avg_sources": round(mean(source_counts), 2) if source_counts else 0,
            "median_sources": median(source_counts) if source_counts else 0,
            "p90_sources": _percentile(source_counts, 0.9),
            "answers_with_markers": sum(1 for r in engine_records if ANSWER_MARKER_RE.search(r.answer_text)),
            "answers_with_cjk_adjacent_markers": sum(
                1 for r in engine_records if CJK_ADJACENT_MARKER_RE.search(r.answer_text)
            ),
        }

    return {
        "record_count": len(records),
        "engine_summary": engine_summary,
        "marker_count": marker_count,
        "cjk_adjacent_marker_count": cjk_adjacent_count,
        "code_index_like_count": code_index_like_count,
        "top_domains": domain_counts.most_common(30),
        "shared_host_domains": shared_domain_counts.most_common(),
        "recommendations": _recommendations(engine_summary, shared_domain_counts),
    }


def _recommendations(engine_summary: dict[str, Any], shared_counts: Counter[str]) -> list[str]:
    notes: list[str] = []
    averages = {
        engine: data["avg_sources"]
        for engine, data in engine_summary.items()
        if engine != "unknown" and data.get("records", 0) > 0
    }
    if averages:
        max_engine = max(averages, key=averages.get)
        min_engine = min(averages, key=averages.get)
        if averages[max_engine] >= max(1.0, averages[min_engine] * 2):
            notes.append(
                f"{max_engine} average source count is much higher than {min_engine}; keep 1/sqrt(N) fairness normalization enabled."
            )
    if shared_counts:
        notes.append("Shared-host domains appear in the source set; keep mp.weixin/baijiahao/sohu account-level entity splitting in the backlog.")
    if engine_summary.get("unknown", {}).get("records", 0) > 0:
        notes.append("Some rows could not be mapped to a known engine; inspect export schema before using this report for final weight tuning.")
    notes.append("Use answer adoption markers as the highest-confidence signal; search-only exposure should remain lower weight.")
    return notes


def to_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# GEO Flywheel Calibration Summary",
        "",
        f"- Records scanned: {summary['record_count']}",
        f"- Answer markers found: {summary['marker_count']}",
        f"- CJK-adjacent markers found: {summary['cjk_adjacent_marker_count']}",
        f"- Code-index-like patterns ignored by production regex: {summary['code_index_like_count']}",
        "",
        "## Engine Source Counts",
        "",
        "| Engine | Records | Avg Sources | Median | P90 | Answers w/ Markers | CJK Adjacent |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for engine, data in summary["engine_summary"].items():
        lines.append(
            f"| {engine} | {data['records']} | {data['avg_sources']} | "
            f"{data['median_sources']} | {data['p90_sources']} | "
            f"{data['answers_with_markers']} | {data['answers_with_cjk_adjacent_markers']} |"
        )
    lines.extend(["", "## Shared Hosts", ""])
    if summary["shared_host_domains"]:
        for domain, count in summary["shared_host_domains"]:
            lines.append(f"- {domain}: {count}")
    else:
        lines.append("- None detected")
    lines.extend(["", "## Top Domains", ""])
    for domain, count in summary["top_domains"][:15]:
        lines.append(f"- {domain}: {count}")
    lines.extend(["", "## Recommendations", ""])
    for note in summary["recommendations"]:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument("--max-file-mb", type=float, default=20.0)
    args = parser.parse_args()

    default_dirs = [
        Path(r"C:\Users\DemoUser\Desktop\AI回答爬虫_数据包_20260526"),
        Path(r"C:\Users\DemoUser\Downloads\AI回答爬虫_数据包_20260604"),
    ]
    data_dirs = [Path(p) for p in args.data_dir] if args.data_dir else default_dirs
    summary = summarize(collect_records(
        data_dirs,
        max_files=args.max_files,
        max_file_mb=args.max_file_mb,
    ))

    if args.markdown:
        output = to_markdown(summary)
    else:
        output = json.dumps(summary, ensure_ascii=False, indent=2)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
