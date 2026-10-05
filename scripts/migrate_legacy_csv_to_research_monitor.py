#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迁移老板桌面 30+ 个早期 GEO 调研 CSV 到主系统的 9 张
geo_research_* 表 + 同步双写到老 geo_research_raw 表。

【数据源】
  C:/Users/OmniR/Desktop/全域上榜/ai扒取数据集/
  └── YYYY-M-D 行业名/                (如 2026-3-14 企业服务)
      ├── YYYY-MM-DD_统计结果.csv    (本脚本只读这个 CSV)
      └── 回答原文/                   (本脚本不读·原文已并入 CSV 第 9 列)
  跳过子目录: 揭阳大昀地产 (老板说测试用)

【CSV 字段实际探测结果】
  header 永远 8 列:
    序号 / 查询内容 / AI平台 / 引用编号 / 引用标题 / 引用链接 / 引用来源平台 / [引用摘要 或 引用摘要，回答原文]
  数据行:
    - 2026-3-14 企业服务  ➜ 8 列 (无回答原文)
    - 其他 31 个 CSV       ➜ 9 列 (列 7=引用摘要 / 列 8=回答原文)
  注意 header 第 8 个 cell 含中文逗号 "引用摘要，回答原文" 是合并字段名,
       不能用 csv.DictReader (列数 mismatch),必须用 csv.reader 读 list 然后按位置访问。

【目标 9 张表】
  geo_research_industries        16 行 (含 geo+geo服务 / 文娱培训+文娱游戏 合并)
  geo_research_prompts           ~25 × 16 行
  geo_research_round             32 行 (每 CSV 1 round)
  geo_research_round_call        ~ 25 × 4 × 32 行 (distinct prompt × platform 每 round)
  geo_research_articles          ~ 30000-40000 行 (URL 全局唯一)
  geo_research_article_citations ~ 53,396 行 (UNIQUE 去重后略少)
  geo_research_review_log        不灌
  geo_research_cost_log          不灌
  geo_research_config            不灌 (默认 10 项已 seed)

【双写】
  每行 CSV 同步插入 geo_research_raw (老 placement_service 还会读)

【运行】
  默认 dry-run:
      python scripts/migrate_legacy_csv_to_research_monitor.py
  真写 PG:
      python scripts/migrate_legacy_csv_to_research_monitor.py --full
  仅迁某个行业:
      python scripts/migrate_legacy_csv_to_research_monitor.py --only-industry 房地产

【铁律】
  - dry-run 不连 PG
  - 整个 30+ CSV 一个事务 BEGIN/COMMIT (--full 模式)
  - ON CONFLICT DO NOTHING 全程幂等 · 重跑安全
  - 跳过揭阳大昀地产 / 跳过 回答原文/ 子目录
  - 中文注释 + 中文 logger · 无 emoji
  - SQL 全 %s 参数化
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

# ---- 让脚本能 import 主系统模块 ------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.research_monitor.url_normalizer import (  # noqa: E402
    compute_url_hash,
    normalize_url,
)

# psycopg2 仅在 --full 时需要,dry-run 不依赖
try:
    import psycopg2  # noqa: F401
    HAS_PG = True
except ImportError:
    HAS_PG = False


# ============================================================================
# 1. 常量配置
# ============================================================================

CSV_ROOT = Path("C:/Users/OmniR/Desktop/全域上榜/ai扒取数据集")
SKIP_DIRS = {"揭阳大昀地产"}  # 测试目录·跳过
TIMEZONE_CN = timezone(timedelta(hours=8))

# CSV 子目录名 → 标准化行业名
RAW_DIR_TO_INDUSTRY: Dict[str, str] = {
    "geo": "GEO 优化服务",
    "geo服务": "GEO 优化服务",
    "文娱培训": "文娱游戏",
    "文娱游戏": "文娱游戏",
    # 其他直接相等
}

# 标准化行业名 → slug (16 个)
INDUSTRY_NAME_TO_SLUG: Dict[str, str] = {
    "企业服务": "enterprise",
    "房地产": "real-estate",
    "装修建材": "home-decor",
    "医疗健康": "health",
    "教育培训": "education",
    "文娱游戏": "entertainment",   # 含"文娱培训"
    "旅游酒店": "travel",
    "时尚美妆": "beauty",
    "母婴亲子": "maternity",
    "汽车": "auto",
    "法律商务": "legal",
    "电商零售": "ecommerce",
    "科技数码": "tech",
    "金融理财": "finance",
    "食品餐饮": "food",
    "GEO 优化服务": "geo-services",  # 含 geo / geo服务
}

# CSV "AI平台" 列 → 标准化平台名 (适配 schema CHECK 约束: doubao/deepseek/qwen/kimi)
PLATFORM_ZH_TO_EN: Dict[str, str] = {
    "豆包": "doubao",
    "DeepSeek": "deepseek",
    "deepseek": "deepseek",
    "Deepseek": "deepseek",
    "千问": "qwen",
    "通义千问": "qwen",
    "Qwen": "qwen",
    "qwen": "qwen",
    "Kimi": "kimi",
    "kimi": "kimi",
    "KIMI": "kimi",
}

# 子目录名解析正则:
# "2026-3-14 企业服务" / "2026-3-23  法律商务" / "2026-06-04_旅游酒店"
DIR_NAME_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[\s_]+(.+?)\s*$")
ARTICLE_ROLLUP_KEYWORD = "文章上榜统计"
CLEANED_ARTICLE_DIR_NAME = "引用原文_清洗后"
LEGACY_MD_CLEAN_MODEL = "legacy_csv_md_v1"


# ============================================================================
# 2. 日志
# ============================================================================

logger = logging.getLogger("legacy_csv_migrate")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ============================================================================
# 3. 工具函数
# ============================================================================

def parse_dir_name(dir_name: str) -> Optional[Tuple[datetime, str, str]]:
    """
    解析 CSV 子目录名 → (started_at_aware, raw_industry, normalized_industry)
    返回 None 表示无法解析。
    """
    m = DIR_NAME_RE.match(dir_name)
    if not m:
        return None
    yyyy, mm, dd, raw_industry = m.groups()
    raw_industry = raw_industry.strip()
    started_at = datetime(
        int(yyyy), int(mm), int(dd), 9, 0, 0, tzinfo=TIMEZONE_CN
    )
    normalized = RAW_DIR_TO_INDUSTRY.get(raw_industry, raw_industry)
    return started_at, raw_industry, normalized


def make_round_id(started_at: datetime, slug: str) -> str:
    """
    生成 round_id · 形如 round_legacy_2026-03-17_geo-services
    长度 < 50 满足 schema VARCHAR(50)
    """
    return f"round_legacy_{started_at.strftime('%Y-%m-%d')}_{slug}"


def safe_strip(v: Optional[str]) -> str:
    """strip + 过滤 NUL/控制字符 (PG TEXT 不接受 \\x00)"""
    if not v:
        return ""
    s = str(v).strip()
    # 去 NUL / BEL / BS / VT / FF
    s = s.replace("\x00", "").replace("\x07", "").replace("\x08", "").replace("\x0b", "").replace("\x0c", "")
    return s


def safe_int(v: Optional[str]) -> Optional[int]:
    try:
        return int(str(v).strip())
    except Exception:
        return None


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[:200] or "unknown"
    except Exception:
        return "unknown"


def normalize_platform(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    return PLATFORM_ZH_TO_EN.get(raw, None)


# ============================================================================
# 4. CSV 发现 + 解析
# ============================================================================

class CsvFileInfo:
    """单个 CSV 元信息"""

    def __init__(
        self,
        dir_path: Path,
        csv_path: Path,
        started_at: datetime,
        raw_industry: str,
        industry_name: str,
    ):
        self.dir_path = dir_path
        self.csv_path = csv_path
        self.started_at = started_at
        self.raw_industry = raw_industry
        self.industry_name = industry_name
        self.dir_name = dir_path.name

    def __repr__(self) -> str:
        return (
            f"<CsvFileInfo dir={self.dir_name!r} "
            f"industry={self.industry_name!r} csv={self.csv_path.name}>"
        )


def discover_csv_files(
    csv_root: Path, only_industry: Optional[str] = None
) -> List[CsvFileInfo]:
    """扫描根目录,跳过测试目录,返回 CsvFileInfo 列表 (按 started_at 升序)"""
    files: List[CsvFileInfo] = []
    if not csv_root.exists():
        raise FileNotFoundError(f"CSV 根目录不存在: {csv_root}")

    for child in sorted(csv_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in SKIP_DIRS:
            logger.info("跳过测试目录: %s", child.name)
            continue
        parsed = parse_dir_name(child.name)
        if not parsed:
            logger.warning("目录名无法解析,跳过: %s", child.name)
            continue
        started_at, raw_industry, industry_name = parsed
        if only_industry and industry_name != only_industry:
            continue
        # 找 CSV (本目录直接的 *.csv·不递归)
        csv_candidates = sorted(child.glob("*.csv"))
        if not csv_candidates:
            logger.warning("目录无 CSV·跳过: %s", child.name)
            continue
        stats_csv_candidates = [p for p in csv_candidates if "统计结果" in p.stem]
        csv_path = sorted(stats_csv_candidates or csv_candidates)[0]
        if len(csv_candidates) > 1:
            logger.warning(
                "目录有 %d 个 CSV·选择引用明细表: %s -> %s",
                len(csv_candidates), child.name, csv_path.name,
            )
        files.append(
            CsvFileInfo(
                dir_path=child,
                csv_path=csv_path,
                started_at=started_at,
                raw_industry=raw_industry,
                industry_name=industry_name,
            )
        )
    files.sort(key=lambda f: (f.started_at, f.industry_name))
    return files


def iter_csv_rows(csv_path: Path) -> Iterable[List[str]]:
    """
    用 csv.reader 解析 (不用 DictReader · 因为 header 列数 != 数据列数)。
    yield 8-9 元素 list,跳过空行。
    """
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return
        if len(header) < 8:
            logger.error("CSV header 列数 < 8 异常: %s · %r", csv_path, header)
            return
        for row in reader:
            if not row or all(not (c or "").strip() for c in row):
                continue
            yield row


# ============================================================================
# 5. 数据收集 (dry-run 与真跑共享)
# ============================================================================

class ParsedRowMinimal:
    """单行 CSV 处理后字段·用于真跑写入 + dry-run 统计"""

    __slots__ = (
        "industry_name", "query", "platform", "rank", "title", "url_raw",
        "url_norm", "cite_platform_zh", "excerpt", "answer_text", "raw_id",
    )

    def __init__(
        self,
        industry_name: str,
        query: str,
        platform: str,
        rank: Optional[int],
        title: str,
        url_raw: str,
        url_norm: str,
        cite_platform_zh: str,
        excerpt: str,
        answer_text: Optional[str],
    ):
        self.industry_name = industry_name
        self.query = query
        self.platform = platform
        self.rank = rank
        self.title = title
        self.url_raw = url_raw
        self.url_norm = url_norm
        self.cite_platform_zh = cite_platform_zh
        self.excerpt = excerpt
        self.answer_text = answer_text
        self.raw_id: Optional[int] = None


@dataclass(frozen=True)
class ArticleBodyPatch:
    """从 legacy 数据包的清洗 md 生成的文章正文回填项"""

    industry_name: str
    url_raw: str
    url_norm: str
    url_hash: str
    article_file: str
    title: str
    body: str
    cleaned_char_count: int


def parse_csv_rows(csv_info: CsvFileInfo) -> Tuple[List[ParsedRowMinimal], Dict[str, int]]:
    """
    解析单个 CSV · 返回 (有效行列表, 统计)
    统计 keys: total/skipped_empty/skipped_platform/skipped_url/valid
    """
    stats = defaultdict(int)
    out: List[ParsedRowMinimal] = []
    for row in iter_csv_rows(csv_info.csv_path):
        stats["total"] += 1
        # 列布局 (按位置):
        # 0 序号 / 1 查询内容 / 2 AI平台 / 3 引用编号 / 4 引用标题
        # 5 引用链接 / 6 引用来源平台 / 7 引用摘要 / 8 回答原文(可选)
        if len(row) < 7:
            stats["skipped_short"] += 1
            continue
        query = safe_strip(row[1]) if len(row) > 1 else ""
        platform_zh = safe_strip(row[2]) if len(row) > 2 else ""
        rank = safe_int(row[3]) if len(row) > 3 else None
        title = safe_strip(row[4]) if len(row) > 4 else ""
        url_raw = safe_strip(row[5]) if len(row) > 5 else ""
        cite_platform_zh = safe_strip(row[6]) if len(row) > 6 else ""
        excerpt = safe_strip(row[7]) if len(row) > 7 else ""
        answer_text = safe_strip(row[8]) if len(row) > 8 else ""

        if not query or not url_raw:
            stats["skipped_empty"] += 1
            continue

        platform = normalize_platform(platform_zh)
        if not platform:
            logger.warning(
                "未识别平台名 %r · 整行跳过 (csv=%s row=%d)",
                platform_zh, csv_info.dir_name, stats["total"],
            )
            stats["skipped_platform"] += 1
            continue

        url_norm = normalize_url(url_raw)
        if not url_norm:
            stats["skipped_url"] += 1
            continue

        out.append(ParsedRowMinimal(
            industry_name=csv_info.industry_name,
            query=query,
            platform=platform,
            rank=rank,
            title=title,
            url_raw=url_raw,
            url_norm=url_norm,
            cite_platform_zh=cite_platform_zh,
            excerpt=excerpt,
            answer_text=answer_text or None,
        ))
        stats["valid"] += 1
    return out, dict(stats)


def _strip_markdown_frontmatter(text: str) -> str:
    """去掉 legacy 清洗 md 的 YAML frontmatter,保留正文 markdown。"""
    cleaned = safe_strip(text)
    if cleaned.startswith("---"):
        match = re.match(r"^---\r?\n.*?\r?\n---\r?\n?", cleaned, flags=re.S)
        if match:
            cleaned = cleaned[match.end():]
    return safe_strip(cleaned)


def _find_article_rollup_csv(csv_info: CsvFileInfo) -> Optional[Path]:
    candidates = sorted(csv_info.dir_path.glob(f"*{ARTICLE_ROLLUP_KEYWORD}*.csv"))
    return candidates[0] if candidates else None


def _read_cleaned_markdown_body(clean_dir: Path, article_file: str) -> str:
    safe_name = Path(safe_strip(article_file)).name
    if not safe_name:
        return ""
    md_path = clean_dir / safe_name
    if not md_path.exists() or not md_path.is_file():
        return ""
    try:
        return _strip_markdown_frontmatter(
            md_path.read_text(encoding="utf-8", errors="ignore")
        )
    except Exception as exc:
        logger.warning("读取清洗正文失败: %s · %s", md_path, exc)
        return ""


def collect_article_body_patches(csv_files: List[CsvFileInfo]) -> List[ArticleBodyPatch]:
    """
    从 6 月 legacy 数据包的 `文章上榜统计.csv` + `引用原文_清洗后/*.md`
    提取文章正文,用于回填 geo_research_articles.inline_cleaned_content。
    """
    patches_by_key: Dict[Tuple[str, str], ArticleBodyPatch] = {}
    for csv_info in csv_files:
        rollup_csv = _find_article_rollup_csv(csv_info)
        clean_dir = csv_info.dir_path / CLEANED_ARTICLE_DIR_NAME
        if not rollup_csv or not clean_dir.exists():
            continue

        with rollup_csv.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                article_file = safe_strip(row.get("文章文件"))
                url_raw = safe_strip(row.get("原文链接"))
                if not article_file or not url_raw:
                    continue
                fetch_ok = safe_strip(row.get("抓取是否成功"))
                if fetch_ok and fetch_ok not in {"是", "true", "TRUE", "1", "yes", "YES", "ok", "OK"}:
                    continue
                url_norm = normalize_url(url_raw)
                if not url_norm:
                    continue
                body = _read_cleaned_markdown_body(clean_dir, article_file)
                if not body:
                    continue
                cleaned_char_count = safe_int(row.get("清洗后字数")) or len(
                    re.sub(r"\s+", "", body)
                )
                url_hash = compute_url_hash(url_norm)
                patch = ArticleBodyPatch(
                    industry_name=csv_info.industry_name,
                    url_raw=url_raw,
                    url_norm=url_norm,
                    url_hash=url_hash,
                    article_file=article_file,
                    title=safe_strip(row.get("标题"))[:1000],
                    body=body,
                    cleaned_char_count=cleaned_char_count,
                )
                key = (csv_info.industry_name, url_hash)
                old = patches_by_key.get(key)
                if old is None or patch.cleaned_char_count > old.cleaned_char_count:
                    patches_by_key[key] = patch
    return list(patches_by_key.values())


# ============================================================================
# 6. Step 函数
# ============================================================================

def step1_ensure_industries(cur, dry: bool) -> Dict[str, int]:
    """
    16 个标准化行业 INSERT INTO geo_research_industries
    返回 {industry_name: industry_id}
    """
    logger.info("=== Step 1 · ensure_industries (16 行业) ===")
    if dry:
        return {name: i + 1 for i, name in enumerate(INDUSTRY_NAME_TO_SLUG.keys())}

    sql_insert = """
        INSERT INTO geo_research_industries (name, slug, sort_order, active, ai_seed_done)
        VALUES (%s, %s, %s, TRUE, TRUE)
        ON CONFLICT (name) DO NOTHING
    """
    rows = [
        (name, slug, i)
        for i, (name, slug) in enumerate(INDUSTRY_NAME_TO_SLUG.items())
    ]
    for r in rows:
        cur.execute(sql_insert, r)

    # 二次 SELECT 取 id (handle ON CONFLICT 跳过的)
    cur.execute(
        "SELECT name, id FROM geo_research_industries WHERE name = ANY(%s)",
        (list(INDUSTRY_NAME_TO_SLUG.keys()),),
    )
    rows = cur.fetchall()
    if rows and isinstance(rows[0], dict):
        return {row["name"]: row["id"] for row in rows}
    return {row[0]: row[1] for row in rows}


def step2_collect_prompts_per_industry(
    csv_files: List[CsvFileInfo],
) -> Dict[str, List[str]]:
    """
    扫描所有 CSV · 收集每行业的 distinct(查询内容)
    返回 {industry_name: [prompt_text, ...]} (按首次出现顺序)
    """
    logger.info("=== Step 2 · collect_prompts_per_industry ===")
    seen: Dict[str, Set[str]] = defaultdict(set)
    ordered: Dict[str, List[str]] = defaultdict(list)
    for csv_info in csv_files:
        for row in iter_csv_rows(csv_info.csv_path):
            if len(row) < 2:
                continue
            q = safe_strip(row[1])
            if not q:
                continue
            if q in seen[csv_info.industry_name]:
                continue
            seen[csv_info.industry_name].add(q)
            ordered[csv_info.industry_name].append(q)
    for ind, prompts in ordered.items():
        logger.info("  行业 %s · %d 个 distinct prompt", ind, len(prompts))
    return dict(ordered)


def step3_insert_prompts(
    cur,
    industry_id_map: Dict[str, int],
    prompts_per_industry: Dict[str, List[str]],
    dry: bool,
) -> Dict[Tuple[str, str], int]:
    """
    每行业 INSERT distinct prompts
    返回 {(industry_name, prompt_text): prompt_id}
    """
    logger.info("=== Step 3 · insert_prompts ===")
    prompt_id_map: Dict[Tuple[str, str], int] = {}
    if dry:
        next_id = 1
        for ind, prompts in prompts_per_industry.items():
            for p in prompts:
                prompt_id_map[(ind, p)] = next_id
                next_id += 1
        return prompt_id_map

    sql_insert = """
        INSERT INTO geo_research_prompts (industry_id, prompt_text, sort_order, active, source)
        VALUES (%s, %s, %s, TRUE, 'seed_history')
        ON CONFLICT DO NOTHING
    """
    sql_select = """
        SELECT id, industry_id, prompt_text
        FROM geo_research_prompts
        WHERE industry_id = ANY(%s)
    """

    for ind, prompts in prompts_per_industry.items():
        ind_id = industry_id_map.get(ind)
        if ind_id is None:
            logger.error("行业 %s 没有 industry_id · 跳过", ind)
            continue
        cur.execute(
            "SELECT prompt_text FROM geo_research_prompts WHERE industry_id = %s",
            (ind_id,),
        )
        existing_texts = {
            row["prompt_text"] if isinstance(row, dict) else row[0]
            for row in cur.fetchall()
        }
        for sort_order, p in enumerate(prompts):
            if p in existing_texts:
                continue
            cur.execute(sql_insert, (ind_id, p, sort_order))
            existing_texts.add(p)

    # 反查所有 prompt id
    industry_id_to_name = {v: k for k, v in industry_id_map.items()}
    cur.execute(sql_select, (list(industry_id_to_name.keys()),))
    rows = cur.fetchall()
    for row in rows:
        if isinstance(row, dict):
            ind_name = industry_id_to_name.get(row["industry_id"])
            prompt_id_map[(ind_name, row["prompt_text"])] = row["id"]
        else:
            ind_name = industry_id_to_name.get(row[1])
            prompt_id_map[(ind_name, row[2])] = row[0]
    return prompt_id_map


def step4_insert_rounds(
    cur,
    csv_files: List[CsvFileInfo],
    industry_id_map: Dict[str, int],
    dry: bool,
) -> Dict[str, str]:
    """
    每个 CSV 1 个 round · 32 个 round
    返回 {csv_dir_name: round_id}
    """
    logger.info("=== Step 4 · insert_rounds (%d 个) ===", len(csv_files))
    round_id_map: Dict[str, str] = {}

    sql_insert = """
        INSERT INTO geo_research_round (
            round_id, batch_id, triggered_by, industries_filter,
            status, current_stage, snapshot_json,
            started_at, finished_at, summary_json, last_heartbeat_at
        ) VALUES (
            %s, %s, 'manual', %s::jsonb,
            'completed', 'completed', %s::jsonb,
            %s, %s, %s::jsonb, %s
        ) ON CONFLICT (round_id) DO NOTHING
    """

    import json
    for csv_info in csv_files:
        slug = INDUSTRY_NAME_TO_SLUG.get(csv_info.industry_name, "unknown")
        round_id = make_round_id(csv_info.started_at, slug)
        round_id_map[csv_info.dir_name] = round_id

        if dry:
            continue

        finished_at = csv_info.started_at + timedelta(hours=4)
        ind_id = industry_id_map.get(csv_info.industry_name)
        industries_filter = json.dumps([ind_id]) if ind_id else json.dumps([])
        snapshot = {
            "industries": [{"id": ind_id, "name": csv_info.industry_name}],
            "_note": "从 CSV 反推 · 非原始快照 · raw_industry={}".format(csv_info.raw_industry),
            "csv_dir": csv_info.dir_name,
            "csv_filename": csv_info.csv_path.name,
        }
        summary = {
            "source": "legacy_csv_import",
            "csv_dir": csv_info.dir_name,
        }
        cur.execute(sql_insert, (
            round_id,
            round_id,  # batch_id 同 round_id
            industries_filter,
            json.dumps(snapshot, ensure_ascii=False),
            csv_info.started_at,
            finished_at,
            json.dumps(summary, ensure_ascii=False),
            finished_at,
        ))
    return round_id_map


def step5_insert_round_calls_articles_citations(
    cur,
    csv_files: List[CsvFileInfo],
    industry_id_map: Dict[str, int],
    prompt_id_map: Dict[Tuple[str, str], int],
    round_id_map: Dict[str, str],
    dry: bool,
    raw_writer,
) -> Dict[str, int]:
    """
    逐 CSV 处理 · 三表联动写入 + 双写 raw 表。
    返回全局统计字典。
    """
    logger.info("=== Step 5 · insert_round_calls_articles_citations ===")
    global_stats: Dict[str, int] = defaultdict(int)

    sql_insert_round_call = """
        INSERT INTO geo_research_round_call (
            round_id, industry_id, prompt_id, prompt_text, platform,
            status, attempts, citations_count, started_at, finished_at
        ) VALUES (%s, %s, %s, %s, %s, 'success', 1, %s, %s, %s)
        ON CONFLICT DO NOTHING
    """
    sql_insert_article = """
        INSERT INTO geo_research_articles (
            url, url_hash, domain, title, primary_industry,
            review_status, clean_status, first_seen_round_id,
            last_seen_at, fetched_at
        ) VALUES (%s, %s, %s, %s, %s, 'crawled', 'pending', %s, %s, %s)
        ON CONFLICT (url_hash, primary_industry) DO NOTHING
    """
    sql_select_articles = """
        SELECT url_hash, id FROM geo_research_articles
        WHERE url_hash = ANY(%s)
    """
    sql_insert_citation = """
        INSERT INTO geo_research_article_citations (
            article_id, round_id, industry_id, prompt_id, raw_id,
            rank_in_response, platform, cited_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (article_id, round_id, prompt_id, platform)
        DO UPDATE SET
            raw_id = COALESCE(geo_research_article_citations.raw_id, EXCLUDED.raw_id),
            rank_in_response = COALESCE(
                geo_research_article_citations.rank_in_response,
                EXCLUDED.rank_in_response
            )
    """

    for ci, csv_info in enumerate(csv_files, 1):
        round_id = round_id_map[csv_info.dir_name]
        ind_id = industry_id_map.get(csv_info.industry_name)
        if ind_id is None:
            logger.error("CSV %s 行业 %s 无 industry_id·跳过整 CSV",
                         csv_info.dir_name, csv_info.industry_name)
            continue

        rows, csv_stats = parse_csv_rows(csv_info)
        logger.info(
            "  [%d/%d] %s · %d 行 valid (total=%d skipped_empty=%d skipped_platform=%d skipped_url=%d)",
            ci, len(csv_files), csv_info.dir_name,
            csv_stats.get("valid", 0),
            csv_stats.get("total", 0),
            csv_stats.get("skipped_empty", 0),
            csv_stats.get("skipped_platform", 0),
            csv_stats.get("skipped_url", 0),
        )
        for k, v in csv_stats.items():
            global_stats[f"csv_{k}"] += v

        # 5.1 distinct (prompt_text, platform) → round_call
        round_call_pairs: Dict[Tuple[str, str], int] = defaultdict(int)
        for r in rows:
            round_call_pairs[(r.query, r.platform)] += 1
        if not dry:
            for (q, plat), cnt in round_call_pairs.items():
                pid = prompt_id_map.get((csv_info.industry_name, q))
                if pid is None:
                    continue
                started = csv_info.started_at
                finished = started + timedelta(seconds=30)
                cur.execute(sql_insert_round_call, (
                    round_id, ind_id, pid, q, plat,
                    cnt, started, finished,
                ))
        global_stats["round_calls"] += len(round_call_pairs)

        # 5.2 distinct(url) → articles
        # 同时构造同一 (prompt+platform+url_hash) 取 rank 最小的 citation 候选
        # citations UNIQUE = (article_id, round_id, prompt_id, platform)
        # 同 prompt+platform+url 多 rank → 保留 rank 最小那条
        article_meta: Dict[str, Dict] = {}  # url_hash → {url, domain, title}
        citation_candidates: Dict[Tuple[str, str, str], ParsedRowMinimal] = {}
        # key = (query, platform, url_hash) → ParsedRowMinimal (rank 最小那条)

        for r in rows:
            try:
                uh = compute_url_hash(r.url_norm)
            except Exception:
                continue
            if uh not in article_meta:
                article_meta[uh] = {
                    "url": r.url_norm,
                    "domain": domain_of(r.url_norm),
                    "title": r.title[:1000],
                }
            ck = (r.query, r.platform, uh)
            old = citation_candidates.get(ck)
            if (old is None
                    or (r.rank is not None
                        and (old.rank is None or r.rank < old.rank))):
                citation_candidates[ck] = r

        if not dry:
            fetched = csv_info.started_at
            for uh, meta in article_meta.items():
                cur.execute(sql_insert_article, (
                    meta["url"], uh, meta["domain"], meta["title"],
                    csv_info.industry_name,
                    round_id, fetched, fetched,
                ))

            # 反查 article ids
            uh_list = list(article_meta.keys())
            url_hash_to_id: Dict[str, int] = {}
            CHUNK = 1000
            for i in range(0, len(uh_list), CHUNK):
                cur.execute(sql_select_articles, (uh_list[i:i + CHUNK],))
                fetched_rows = cur.fetchall()
                for row in fetched_rows:
                    if isinstance(row, dict):
                        url_hash_to_id[row["url_hash"]] = row["id"]
                    else:
                        url_hash_to_id[row[0]] = row[1]

            # 5.3a 先双写 raw 并拿回 raw.id,再写 citation.raw_id
            if raw_writer is not None:
                raw_writer.write_csv(rows, csv_info, round_id)

            # 5.3 写 citations
            cited_at = csv_info.started_at
            for (q, plat, uh), r in citation_candidates.items():
                aid = url_hash_to_id.get(uh)
                if aid is None:
                    continue
                pid = prompt_id_map.get((csv_info.industry_name, q))
                if pid is None:
                    continue
                cur.execute(sql_insert_citation, (
                    aid, round_id, ind_id, pid, r.raw_id,
                    r.rank, plat, cited_at,
                ))

        global_stats["articles"] += len(article_meta)
        global_stats["citations"] += len(citation_candidates)

        global_stats["raw_rows"] += len(rows)

    return dict(global_stats)


def step5b_backfill_article_cleaned_content(
    cur,
    patches: List[ArticleBodyPatch],
    dry: bool,
) -> Dict[str, int]:
    """
    用 legacy 数据包已清洗的 md 正文回填文章表。
    只补空正文,不覆盖未来真实调研轮/Jina 已写入的正文。
    """
    logger.info("=== Step 5b · backfill_article_cleaned_content ===")
    stats: Dict[str, int] = defaultdict(int)
    stats["article_bodies_loaded"] = len(patches)
    if dry:
        logger.info("  dry-run · 发现可回填正文 %d 篇", len(patches))
        return dict(stats)

    sql_update_article_body = """
        UPDATE geo_research_articles
           SET inline_cleaned_content = %s,
               cleaned_char_count = %s,
               clean_status = 'cleaned',
               clean_model = COALESCE(NULLIF(clean_model, ''), %s),
               last_cleaned_at = COALESCE(last_cleaned_at, NOW())
         WHERE url_hash = %s
           AND primary_industry = %s
           AND (inline_cleaned_content IS NULL OR inline_cleaned_content = '')
    """
    for patch in patches:
        cur.execute(sql_update_article_body, (
            patch.body,
            patch.cleaned_char_count,
            LEGACY_MD_CLEAN_MODEL,
            patch.url_hash,
            patch.industry_name,
        ))
        stats["article_bodies_updated"] += int(getattr(cur, "rowcount", 0) or 0)
    logger.info(
        "  正文回填 loaded=%d updated=%d",
        stats["article_bodies_loaded"],
        stats["article_bodies_updated"],
    )
    return dict(stats)


def step6_skip_oss():
    """老板拍板不传 OSS"""
    logger.info("=== Step 6 · skip_oss (老板拍板) ===")


def step7_reconcile(cur, dry: bool, expected: Dict[str, int]):
    """对账 · 真跑后 SELECT 各表 count vs 期望"""
    logger.info("=== Step 7 · reconcile ===")
    if dry:
        logger.info("  (dry-run · 不查 PG · 用累计统计当对账)")
        for k, v in expected.items():
            logger.info("    %-30s %d", k, v)
        return

    queries = [
        ("industries",                "SELECT COUNT(*) FROM geo_research_industries WHERE active"),
        ("prompts",                   "SELECT COUNT(*) FROM geo_research_prompts WHERE source='seed_history'"),
        ("rounds",                    "SELECT COUNT(*) FROM geo_research_round WHERE round_id LIKE 'round_legacy_%'"),
        ("round_calls",               "SELECT COUNT(*) FROM geo_research_round_call WHERE round_id LIKE 'round_legacy_%'"),
        ("articles",                  "SELECT COUNT(*) FROM geo_research_articles"),
        ("citations",                 "SELECT COUNT(*) FROM geo_research_article_citations WHERE round_id LIKE 'round_legacy_%'"),
        ("raw_legacy_csv_import",     "SELECT COUNT(*) FROM geo_research_raw WHERE researcher='legacy_csv_import'"),
    ]
    for label, q in queries:
        cur.execute(q)
        row = cur.fetchone()
        cnt = row[0] if not isinstance(row, dict) else list(row.values())[0]
        exp = expected.get(label)
        flag = "" if exp is None else (" OK" if cnt >= exp else " MISMATCH(expected>=%d)" % exp)
        logger.info("    %-30s %d%s", label, cnt, flag)


def step8_print_summary(stats: Dict, dry: bool):
    logger.info("=== Step 8 · summary ===")
    mode = "DRY-RUN" if dry else "FULL-RUN"
    logger.info("模式 = %s", mode)
    for k, v in stats.items():
        logger.info("  %-30s %s", k, v)


# ============================================================================
# 7. 老 raw 表双写器
# ============================================================================

class RawWriter:
    """双写 geo_research_raw 表 (老 placement_service 还会读)"""

    SQL = """
        WITH existing AS (
            SELECT id
              FROM geo_research_raw
             WHERE industry = %s
               AND query = %s
               AND engine = %s
               AND COALESCE(cited_platform, '') = COALESCE(%s, '')
               AND COALESCE(cite_position, 0) = %s
               AND cite_url = %s
               AND batch_id = %s
               AND researcher = 'legacy_csv_import'
             ORDER BY id
             LIMIT 1
        ),
        inserted AS (
            INSERT INTO geo_research_raw (
                industry, query, engine, cited_platform, cite_position,
                cite_url, cite_title, cite_excerpt, answer_text,
                batch_id, researcher, created_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'legacy_csv_import', %s
             WHERE NOT EXISTS (SELECT 1 FROM existing)
            RETURNING id
        )
        SELECT id FROM inserted
        UNION ALL
        SELECT id FROM existing
        LIMIT 1
    """

    def __init__(self, cur):
        self.cur = cur

    def write_csv(self, rows: List[ParsedRowMinimal],
                  csv_info: CsvFileInfo, round_id: str):
        if not rows:
            return
        for r in rows:
            cited_platform = r.cite_platform_zh or "unknown"
            cite_position = r.rank or 0
            insert_values = (
                csv_info.industry_name,
                r.query,
                r.platform,
                cited_platform,
                cite_position,
                r.url_norm,
                round_id,
                csv_info.industry_name,
                r.query,
                r.platform,
                cited_platform,
                cite_position,
                r.url_norm,
                r.title or None,
                r.excerpt or None,
                r.answer_text or None,
                round_id,
                csv_info.started_at,
            )
            self.cur.execute(self.SQL, insert_values)
            row = self.cur.fetchone()
            if isinstance(row, dict):
                r.raw_id = row.get("id")
            elif row:
                r.raw_id = row[0]


# ============================================================================
# 8. 主入口
# ============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="迁移老板桌面 30+ CSV 到 geo_research_* + raw 表"
    )
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="(默认) 不连 PG · 只解析 + 打印计划")
    parser.add_argument("--full", action="store_true", default=False,
                        help="真写 PG · 整体单事务")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"),
                        help="PG 连接串 · 默认读 DATABASE_URL")
    parser.add_argument("--only-industry", default=None,
                        help="只迁某个标准化行业(测试用)·按行业名匹配")
    parser.add_argument("--csv-root", default=str(CSV_ROOT),
                        help="CSV 根目录 (默认 %s)" % CSV_ROOT)
    args = parser.parse_args(argv)

    dry = not args.full

    csv_root = Path(args.csv_root)
    logger.info("CSV root = %s", csv_root)
    logger.info("模式     = %s", "DRY-RUN" if dry else "FULL-RUN")
    if args.only_industry:
        logger.info("only-industry = %s", args.only_industry)

    # 1. 发现 CSV
    csv_files = discover_csv_files(csv_root, only_industry=args.only_industry)
    logger.info("发现 CSV = %d", len(csv_files))
    for f in csv_files:
        logger.info("  · %s | industry=%s | started_at=%s",
                    f.dir_name, f.industry_name, f.started_at.isoformat())

    if not csv_files:
        logger.warning("没找到 CSV · 退出")
        return 1

    # 2. dry-run / full-run
    if dry:
        return _run_dry(csv_files)
    else:
        return _run_full(csv_files, args)


def _run_dry(csv_files: List[CsvFileInfo]) -> int:
    """dry-run 路径 · 完全不连 PG"""
    industry_id_map = step1_ensure_industries(cur=None, dry=True)
    prompts_per_industry = step2_collect_prompts_per_industry(csv_files)
    prompt_id_map = step3_insert_prompts(
        None, industry_id_map, prompts_per_industry, dry=True
    )
    round_id_map = step4_insert_rounds(None, csv_files, industry_id_map, dry=True)
    stats = step5_insert_round_calls_articles_citations(
        None, csv_files, industry_id_map, prompt_id_map, round_id_map,
        dry=True, raw_writer=None,
    )
    article_body_patches = collect_article_body_patches(csv_files)
    stats.update(step5b_backfill_article_cleaned_content(
        None, article_body_patches, dry=True
    ))
    step6_skip_oss()

    # 把行业/prompt/round 等也加进 stats 方便对账
    expected = {
        "industries": len(industry_id_map),
        "prompts": sum(len(v) for v in prompts_per_industry.values()),
        "rounds": len(round_id_map),
    }
    expected.update(stats)
    step7_reconcile(None, dry=True, expected=expected)
    step8_print_summary(expected, dry=True)

    # 历史全量包参考。单月补录只需看上面的 dry-run 实际统计。
    logger.info("--- 期望对照 ---")
    logger.info("  CSV 文件数        历史全量参考 32；单月包不要求匹配")
    logger.info("  行业数            期望 16 (geo+geo服务 / 文娱培训+文娱游戏 合并)")
    logger.info("  CSV 总行          历史全量参考 ~53,396；本次以 dry-run valid 行为准")
    logger.info("  citations 唯一    历史全量参考略 < 53,396；本次以 dry-run citations 为准")
    return 0


def _run_full(csv_files: List[CsvFileInfo], args) -> int:
    """真跑 · 单事务 BEGIN/COMMIT"""
    if not HAS_PG:
        logger.error("psycopg2 未安装 · 不能 --full")
        return 2
    if not args.db_url:
        logger.error("缺 --db-url 或 DATABASE_URL · 不能 --full")
        return 2

    import psycopg2
    from psycopg2.extras import RealDictCursor

    logger.info("连 PG ...")
    conn = psycopg2.connect(args.db_url)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1
        industry_id_map = step1_ensure_industries(cur, dry=False)
        # 2
        prompts_per_industry = step2_collect_prompts_per_industry(csv_files)
        # 3
        prompt_id_map = step3_insert_prompts(
            cur, industry_id_map, prompts_per_industry, dry=False
        )
        # 4
        round_id_map = step4_insert_rounds(
            cur, csv_files, industry_id_map, dry=False
        )
        # 5
        raw_writer = RawWriter(cur)
        stats = step5_insert_round_calls_articles_citations(
            cur, csv_files, industry_id_map, prompt_id_map, round_id_map,
            dry=False, raw_writer=raw_writer,
        )
        article_body_patches = collect_article_body_patches(csv_files)
        stats.update(step5b_backfill_article_cleaned_content(
            cur, article_body_patches, dry=False
        ))
        # 6
        step6_skip_oss()

        # 7 对账(真跑前提交·这里仍在事务里·先 reconcile 后 commit)
        expected = {
            "industries": len(industry_id_map),
            "prompts": sum(len(v) for v in prompts_per_industry.values()),
            "rounds": len(round_id_map),
        }
        expected.update(stats)
        step7_reconcile(cur, dry=False, expected=expected)

        conn.commit()
        logger.info("commit OK")
        step8_print_summary(expected, dry=False)
        return 0
    except Exception as e:
        conn.rollback()
        logger.exception("发生异常 · 整体 rollback: %s", e)
        return 3
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
