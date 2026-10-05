"""小榜知识库 release 构建脚本（2026-08-10）。

读取 3 个源 · 灌进 kb_chunks 表:
  1. frontend/src/pages/Help/docs-data.ts(29 个 DocNode)
  2. db.faq_items(已 seed 的 10 条 FAQ)
  3. agents.xiaobang_presets.PRESETS(15 条预设)

部署期幂等跑法（Builder 不连接生产执行）::

  python -m tools.xiaobang_kb_indexer --release-sha <40位SHA>

每次在一个数据库事务里淘汰旧 doc/faq/preset chunk、写入完整新 release，
并记录 source hash、内容版本、OperationRegistry 版本和 release SHA。默认不调用
外部模型；只有显式传 ``--with-embeddings`` 才补向量。

依赖:
  - jieba(中文分词 · pip install jieba)
  - 默认无 LLM 依赖、无网络依赖、无 pgvector
"""

import asyncio
import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# 让 `python -m tools.xiaobang_kb_indexer` 跟 `python tools/xiaobang_kb_indexer.py` 都能跑
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db.kb_db import count_chunks, replace_chunks_transactionally
from agents.xiaobang_presets import PRESETS
from services.gap_operation_map import OPERATION_REGISTRY_VERSION, match_operation, resolve_operation

logger = logging.getLogger("kb-indexer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# ==========================================
# 文档 slug 只绑定 operation_id；路径仍从唯一 OperationRegistry 解析。
# ==========================================

DOC_OPERATION_MAP: dict[str, str] = {
    "today-dashboard": "today_workspace",
    "sandbox-mode": "help_center",
    "first-client": "client_list",
    "diagnosis": "diagnosis_new",
    "pricing": "quote_center",
    "writing": "writing_center",
    "publishing": "publish_center",
    "monitoring": "monitoring_center",
    "my-clients": "client_list",
    "my-brand": "brand_center",
    "profile": "personal_settings",
    "wallet": "wallet",
    "feature-pricing": "feature_pricing",
    "referral": "referral",
    "payment-failed": "wallet",
    "publish-failed": "publish_center",
    "no-monitoring-data": "monitoring_center",
    "team-members": "admin_users",
    "audit-log": "admin_audit",
}


def _doc_route(slug: str) -> tuple[str | None, str | None]:
    entry = resolve_operation(DOC_OPERATION_MAP.get(slug, ""))
    return (entry.route_template, f"去{entry.display_name}") if entry else (None, None)

# 这些 slug 不强推跳转(纯概念 / 元指南)
NO_ROUTE_SLUGS = {
    "registration", "read-report", "leads", "roles",
    "what-is-geo", "scoring", "four-engines", "data-export", "retest", "cant-find",
}

# 管理员专属节点
ADMIN_ONLY_SLUGS = {"team-members", "roles", "audit-log"}


# ==========================================
# Markdown 清洗 + 分词
# ==========================================

# strip 图片 ![](url)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
# strip 链接 [text](url) → 保留 text
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
# strip admonition 块语法 > [!TIP] / > [!WARN]
_ADMONITION_RE = re.compile(r"^>\s*\[!(TIP|WARN|NOTE|INFO)\]\s*", re.MULTILINE)


def strip_markdown(text: str) -> str:
    text = _IMG_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _ADMONITION_RE.sub("", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)  # 行内 code
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # bold
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# 中文停用词(常见的)
_STOPWORDS = set("""
的 了 在 是 我 你 他 她 它 我们 你们 他们 这 那 这个 那个 这些 那些
和 与 跟 及 或 或者 但 但是 不过 然后 还有 还是
吗 呢 啊 哦 哈 嗯 哇 呀 哎 哼 呵
有 没 没有 不 不要 别 不能 不会 不是
的话 一下 一次 一个 一种 一些 一直 一定 一起 一样
去 来 在 把 被 给 让 让我
就是 还是 已经 还 又 再 也
什么 怎么 怎么样 为什么 哪里 哪个 哪些 多少
比较 非常 很 太 真 真的 挺 蛮
""".split())

# 数字 + 单字符过滤
_TOKEN_NUM = re.compile(r"^[\d.,，。]+$")


# OmniRank 产品专有名词 · 强制 jieba 当一个词切分
# 否则 "我的客户" 会被拆成 ['我','的','客户'] · "我/的" 是 stopword + 单字被过滤 → 信号丢失
# "生成月报扣多少积分" 不加 "月报" 词时会被错切成 ['生成','月','报扣','多少','积分']
_DOMAIN_WORDS = [
    # 主导航产品名
    "我的客户", "我的品牌", "我的钱包", "我的资料", "我的账号",
    "客户线索", "客户知识库", "客户档案", "客户管理",
    "品牌体检", "诊断报告", "报价方案", "排名监测", "数据导出",
    "推荐赚钱", "写作大厅", "发布管理", "帮助中心",
    "今日看板", "工作台",
    # 产品功能/概念名
    "三档报价", "在线报价", "选词链接", "签约收款",
    "AI 写文章", "AI 引擎", "AI 推荐", "AI 引用",
    "写文章", "发文章", "改文章", "重写文章", "生成文章",
    "知识库", "懂你度", "完整度", "引用率", "覆盖率",
    "GEO", "SEO", "自媒体", "评分维度", "三层漏斗",
    "充值积分", "佣金积分", "赠送积分", "扣费明细", "资金流水",
    "扣费规则", "扣费失败", "积分扣费", "退款", "提现",
    "月报", "周报", "出报告", "做报告", "看报告", "导出报告",
    "复测", "诊断流程", "诊断入口", "诊断分数", "诊断等级",
    "签约", "收款", "录单", "成交",
    "代发", "自助发布", "媒体平台", "发布失败", "发布记录",
    "监测词", "监测词不达标", "监测无数据", "立即测", "定时监测",
    "充值", "退款", "余额", "钱包",
    "推荐码", "推荐链接", "佣金",
    # 4 大引擎
    "豆包", "通义", "Kimi", "DeepSeek",
]
_DOMAIN_WORDS_LOADED = False


def _ensure_jieba_dict_loaded():
    global _DOMAIN_WORDS_LOADED
    if _DOMAIN_WORDS_LOADED:
        return
    try:
        import jieba
        for w in _DOMAIN_WORDS:
            jieba.add_word(w)
        _DOMAIN_WORDS_LOADED = True
    except ImportError:
        pass


def tokenize(text: str) -> list[str]:
    """jieba 分词 + 去停用词 + 去标点 + 小写

    单字中文也过滤(原因:"帮/写/段/到" 等弱信号单字会污染 BM25,
    比如 "帮我写代码" 会因为"帮"在《什么是 GEO》文档里出现而误匹配。
    长度 >= 2 的词才有意义,英文/数字至少 2 个字符也才保留。)
    """
    try:
        import jieba
    except ImportError:
        logger.warning("jieba 未安装 · pip install jieba · 暂用空格分词降级")
        return [w for w in text.lower().split() if w and len(w) >= 2 and w not in _STOPWORDS]

    _ensure_jieba_dict_loaded()
    text = re.sub(r"[^\w一-鿿]+", " ", text)  # 标点替空格
    tokens = jieba.lcut(text)
    out = []
    for t in tokens:
        t = t.strip().lower()
        if not t or len(t) < 2:
            continue  # 单字符全过滤(中英文都是)
        if t in _STOPWORDS:
            continue
        if _TOKEN_NUM.match(t):
            continue
        out.append(t)
    return out


# ==========================================
# 解析 docs-data.ts
# ==========================================

DOCS_FILE = os.path.join(
    _ROOT, "frontend", "src", "pages", "Help", "docs-data.ts"
)
HELP_DOCS_CONTENT_FILE = os.path.join(_ROOT, "api", "help_docs_content.json")


def parse_docs_ts() -> list[dict]:
    """解析帮助文档导航 + 后端正文源。

    返回 [{ category_id, category_title, is_admin, visible_to, slug, title, body }]

    2026-06-03 帮助中心正文已迁到 api/help_docs_content.json,docs-data.ts
    只保留导航。这里必须从后端正文源读取 body + audience,否则旧 doc
    chunk 会残留为 both,导致普通用户小榜检索到代理经营文档。

    策略:先找所有 doc node(slug+title),再为每个 node 反向扫到最近的
    category 起始(`id: '...'` + `title: '...'`)来归类。
    比正向"category 切片"更稳,因为 docCategories 嵌套深。
    """
    with open(DOCS_FILE, "r", encoding="utf-8") as f:
        src = f.read()
    with open(HELP_DOCS_CONTENT_FILE, "r", encoding="utf-8-sig") as f:
        content_by_slug = json.load(f)

    # 1. 找所有 category 起始位置(在 docCategories 数组里 · `  {` 2 空格缩进开头)
    #    每个 category 的特征: id: 'xxx', title: '...', 后续有 nodes: [
    cat_re = re.compile(
        r"\{\s*\n\s*id:\s*['\"]([\w-]+)['\"]\s*,"
        r"\s*\n\s*title:\s*['\"]([^'\"]+)['\"]\s*,",
        re.MULTILINE,
    )
    cat_marks: list[tuple[int, str, str, bool]] = []
    for cm in cat_re.finditer(src):
        cat_id = cm.group(1)
        cat_title = cm.group(2)
        # 在 category 起始之后 200 字符内找 isAdmin: true
        window = src[cm.end(): cm.end() + 400]
        is_admin = bool(re.search(r"isAdmin:\s*true", window))
        cat_marks.append((cm.start(), cat_id, cat_title, is_admin))

    # 2. 找所有 doc node(slug + title · body 已迁后端 JSON)
    node_re = re.compile(
        r"\{\s*slug:\s*['\"]([\w-]+)['\"]\s*,"
        r"\s*title:\s*['\"]([^'\"]+)['\"]"
        r"[\s\S]*?\}",
        re.MULTILINE,
    )

    out: list[dict] = []
    for nm in node_re.finditer(src):
        slug = nm.group(1)
        title = nm.group(2)
        content_entry = content_by_slug.get(slug)
        if not content_entry:
            logger.warning("doc node 找不到后端正文: slug=%s", slug)
            continue
        body = content_entry.get("body") or ""
        audience = content_entry.get("audience") or "both"
        if audience not in ("normal_user", "agent", "l2", "both", "admin"):
            audience = "both"
        node_pos = nm.start()
        # 反向找最近的 category(node_pos 之前最大的 cat_marks 起始)
        owner = None
        for cm in cat_marks:
            if cm[0] < node_pos:
                owner = cm
            else:
                break
        if owner is None:
            # 没找到归属 · 跳过(不应该发生,但防御性处理)
            logger.warning("doc node 找不到归属 category: slug=%s", slug)
            continue
        _pos, cat_id, cat_title, is_admin = owner
        out.append({
            "category_id": cat_id,
            "category_title": cat_title,
            "is_admin": is_admin or (slug in ADMIN_ONLY_SLUGS) or audience == "admin",
            "visible_to": audience,
            "slug": slug,
            "title": title,
            "body": body,
        })

    return out


# ==========================================
# Chunk 切片(按 H2 拆段)
# ==========================================

# 文档末尾的导航/相关链接 section · 不灌进 KB(会污染检索)
# 这种 section 一般就是 bullet list 罗列别的文档链接,本身没实质内容
# 例:"接着做啥"里 "- 我的客户 — 成交后建档管理" 会让 query "我的客户怎么管理"
#     误命中《客户线索·接着做啥》而不是《我的客户》正文
_NAV_SECTION_TITLES = {
    "接着做啥", "接着看", "接着干啥",
    "想再深一点", "更多玩法", "相关链接",
    "别的玩法", "进阶用法", "想再深点",
}


def chunk_doc_body(body: str) -> list[tuple[str | None, str]]:
    """把一篇 doc 按 ## H2 拆成 [(section_title, section_body), ...]

    跳过文档末尾导航类 section(_NAV_SECTION_TITLES)· 这种 section 只是
    bullet 列出相关文档链接,内容碎且会污染 BM25。
    没 H2 的整篇当一个 chunk(section_title=None)
    """
    clean = strip_markdown(body)
    parts = re.split(r"^##\s+(.+?)$", clean, flags=re.MULTILINE)
    # split 后 parts 是 [前导文本, 标题1, 内容1, 标题2, 内容2, ...]
    chunks: list[tuple[str | None, str]] = []
    intro = parts[0].strip()
    if intro and len(intro) > 30:
        chunks.append((None, intro))  # 一句话讲清 等前导段
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        content = (parts[i + 1] if i + 1 < len(parts) else "").strip()
        if not content:
            continue
        # 跳过导航类 section
        if title in _NAV_SECTION_TITLES:
            continue
        chunks.append((title, content))
    if not chunks:
        chunks.append((None, clean))
    return chunks


# 单 chunk 存储上限(与 insert_chunk 存 content[:2000] 对齐)
_MAX_CHUNK_CHARS = 2000
# 切片重叠(避免关键词恰好被切在边界处 → 两片都不完整命中)
_CHUNK_OVERLAP = 200


def _split_oversized(
    content: str,
    max_chars: int = _MAX_CHUNK_CHARS,
    overlap: int = _CHUNK_OVERLAP,
) -> list[str]:
    """[GEO-R10-CAN-028] 把超长 section 切成 <= max_chars 的多个重叠片段。

    修复 token_keywords 与存储正文错位:原实现 tokenize(整段 content) 但只存
    content[:2000],尾部(>2000 字)的词会进 token_keywords 却不在存储正文里
    → BM25 可以靠尾词命中该 chunk,但喂给模型的 2000 字前缀不含该词,模型拿到
    的是「缺了命中依据」的证据(幻觉风险)。切成有界重叠片段后,每片各自 tokenize,
    保证「BM25 命中的词一定在喂给模型的那段正文里」= token 与存储正文单一源。
    """
    content = content or ""
    if len(content) <= max_chars:
        # 短/空正文仍返回单片(保持向后兼容:空 answer 的 FAQ 仍产出一个可检索 chunk)
        return [content]
    pieces: list[str] = []
    start = 0
    n = len(content)
    step = max(1, max_chars - overlap)
    while start < n:
        pieces.append(content[start:start + max_chars])
        start += step
    return pieces


# ==========================================
# 主入口
# ==========================================

_CONTENT_VERSION = "xiaobang-kb-v2"
_STALE_SOURCE_PATTERNS = (
    re.compile(r"销售\s*(?:分组|→|->)"),
    re.compile(r"运营\s*(?:分组|→|->).{0,16}(?:排名监测|效果监测)"),
    re.compile(r"制作\s*(?:→|->).{0,16}发布管理"),
    re.compile(r"AI\s*引用率约\s*\d+%", re.IGNORECASE),
    re.compile(r"固定(?:积分|引用率|评分公式)"),
)


def is_stale_knowledge(question: str, answer: str) -> bool:
    text = f"{question or ''}\n{answer or ''}"
    return any(pattern.search(text) for pattern in _STALE_SOURCE_PATTERNS)


def _chunk_row(**kwargs: Any) -> dict[str, Any]:
    return {
        "section_title": None,
        "route": None,
        "route_label": None,
        "category": "general",
        "is_admin_only": False,
        "token_keywords": [],
        "embedding": None,
        "origin": "manual",
        "visible_to": "both",
        **kwargs,
    }


def build_doc_chunks() -> list[dict[str, Any]]:
    docs = parse_docs_ts()
    rows: list[dict[str, Any]] = []
    for doc in docs:
        # kb_chunks 目前只有 normal_user/agent/both 三档;admin 与 L2 文档不进
        # 小榜 doc 池,避免管理员/L2 专属内容被普通代理或普通用户召回。
        if doc.get("visible_to") in ("admin", "l2"):
            continue
        slug = doc["slug"]
        title = doc["title"]
        cat_id = doc["category_id"]
        is_admin = doc["is_admin"]
        visible_to = doc.get("visible_to") or "both"
        if visible_to not in ("normal_user", "agent", "both"):
            visible_to = "both"
        route, route_label = _doc_route(slug)
        if slug in NO_ROUTE_SLUGS:
            route, route_label = None, None
        chunks = chunk_doc_body(doc["body"])
        for section_title, content in chunks:
            # [GEO-R10-CAN-028] 超长 section 切成有界重叠片段,token 与存储正文同源:
            # tokenize 与 content 都用同一段 piece,杜绝尾词命中但正文缺词的幻觉证据。
            for piece in _split_oversized(content):
                tokens = tokenize(f"{title} {section_title or ''} {piece}")
                if is_stale_knowledge(title, piece):
                    continue
                rows.append(_chunk_row(
                    source_type="doc",
                    source_slug=slug,
                    source_title=title,
                    section_title=section_title,
                    content=piece,  # 已 <= 2000 字 · 与 token_keywords 同一段文本
                    route=route,
                    route_label=route_label,
                    category=cat_id,
                    is_admin_only=is_admin,
                    token_keywords=tokens,
                    visible_to=visible_to,
                ))
    return rows


def reindex_docs() -> int:
    rows = build_doc_chunks()
    replace_chunks_transactionally(source_types=("doc",), chunks=rows)
    return len(rows)


def _faq_keyword_route(question: str, answer: str) -> tuple[str | None, str | None]:
    """FAQ 路由抽取策略(顺序):
      1. question/answer 命中 OperationRegistry → 走唯一注册表
      2. answer_md 显式帮助文档链接 → 仅作为概念帮助链接
      3. 都不命中 → 无路由

    设计原因:大量 FAQ(如"找不到诊断入口在哪")answer_md 没显式 doc 链接,
    但 question 本身就有强信号,补一层关键词路由能让 链接卡 准命中。
    """
    entry = match_operation(f"{question}\n{answer}")
    if entry:
        return entry.route_template, f"去{entry.display_name}"
    # 概念 FAQ 可以链接到帮助文档，但不能把该链接当业务操作路径。
    route_match = re.search(r"\(/help/docs/([\w-]+)\)", answer or "")
    if route_match:
        return (f"/help/docs/{route_match.group(1)}", "看相关文档")
    return None, None


def build_faq_chunks() -> list[dict[str, Any]]:
    """从 faq_items 表读取 · 转 chunks"""
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, question, answer_md, category, visible_to FROM faq_items "
            "WHERE is_published = TRUE ORDER BY id"
        )
        rows = list(cur.fetchall())
    finally:
        conn.close()

    chunks: list[dict[str, Any]] = []
    for r in rows:
        question = r["question"]
        answer = r["answer_md"]
        category = r["category"]
        # FAQ 身份可见性带进小榜 KB:代理专属 FAQ 不会被普通用户的小榜检索到
        visible_to = r.get("visible_to") or "both"
        if visible_to not in ("normal_user", "agent", "both"):
            visible_to = "both"
        if is_stale_knowledge(question, answer or ""):
            logger.warning("旧知识已失效，不进入 release: faq_%s", r["id"])
            continue
        route, route_label = _faq_keyword_route(question, answer or "")
        content = strip_markdown(answer or "")
        # [GEO-R10-CAN-028] 同 reindex_docs:超长 answer 切片,token 与存储正文同源。
        for piece in _split_oversized(content):
            tokens = tokenize(f"{question} {piece}")
            chunks.append(_chunk_row(
                source_type="faq",
                source_slug=f"faq_{r['id']}",
                source_title=question,
                section_title=None,
                content=piece,  # 已 <= 2000 字 · 与 token_keywords 同一段文本
                route=route,
                route_label=route_label,
                category=category,
                is_admin_only=False,
                token_keywords=tokens,
                visible_to=visible_to,
            ))
    return chunks


def reindex_faq() -> int:
    chunks = build_faq_chunks()
    replace_chunks_transactionally(source_types=("faq",), chunks=chunks)
    return len(chunks)


def build_preset_chunks() -> list[dict[str, Any]]:
    """预设答案也灌进 KB(虽然运行时关键词命中优先 · 但留存方便统一管理)"""
    chunks: list[dict[str, Any]] = []
    for p in PRESETS:
        if is_stale_knowledge(p.triggers[0] if p.triggers else p.key, p.answer):
            continue
        tokens = tokenize(" ".join(p.triggers) + " " + p.answer)
        entry = match_operation(" ".join(p.triggers) + " " + p.answer)
        route = entry.route_template if entry else None
        route_label = f"去{entry.display_name}" if entry else None
        chunks.append(_chunk_row(
            source_type="preset",
            source_slug=f"preset_{p.key}",
            source_title=p.triggers[0] if p.triggers else p.key,
            section_title=None,
            content=p.answer[:2000],
            route=route,
            route_label=route_label,
            category="preset",
            is_admin_only=False,
            token_keywords=tokens,
        ))
    return chunks


def reindex_presets() -> int:
    chunks = build_preset_chunks()
    replace_chunks_transactionally(source_types=("preset",), chunks=chunks)
    return len(chunks)


def _current_release_sha(explicit: str | None = None) -> str:
    value = str(explicit or os.getenv("RELEASE_SHA") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    # 生产镜像按部署合同把双信源之一写在 /app/RELEASE_SHA；镜像不带 .git，
    # 因此不能把 git rev-parse 当部署期唯一后备。
    release_file = Path("/app/RELEASE_SHA")
    try:
        value = release_file.read_text(encoding="utf-8").strip().lower()
    except OSError:
        value = ""
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True, timeout=5
        ).strip().lower()
    except Exception:
        value = ""
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    raise RuntimeError(
        "无法确认本次 release SHA；请传 --release-sha 或提供 /app/RELEASE_SHA，旧索引保持不变"
    )


def _utc_now_iso() -> str:
    """构建时间戳(UTC ISO)。

    🔴 只作**记录**用。任何判据都不许拿它跟当前时间比 —— 「判据里不许有今天」
    是本仓反复付过费的一条(固定 cutoff + 夹具吃列默认 NOW = 定时炸弹)。
    对账用的是相对先后与存在性。
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rebuild_release(release_sha: str | None = None) -> dict[str, Any]:
    """幂等部署期入口：三类 chunk 与 manifest 同一事务切换。"""
    groups = {
        "doc": build_doc_chunks(),
        "faq": build_faq_chunks(),
        "preset": build_preset_chunks(),
    }
    all_rows = [row for rows in groups.values() for row in rows]
    hashes = {
        key: hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for key, rows in groups.items()
    }
    # [包 C④ 2026-08-21] 补齐工单点名的三项:**术语规则版本 / 路由清单 / 构建时间**。
    # 前四项(content_version / release_sha / registry 版本 / source_hashes /
    # chunk_counts)是 35/38 班已经做好的,这里只加缺的三样,不重做。
    #
    # 为什么这三样值钱:health 要能判「stale / mixed」就必须有**可对账的锚**。
    #   · 术语规则版本变了而 release 没重建 ⇒ 库里还是按旧规则清洗过的文本;
    #   · 路由清单变了(有人删了页面)⇒ 库里的跳转指向已经不存在的页;
    #   · 构建时间是给人看「上一次成功是什么时候」,**判据不拿它跟 now 比**
    #     (判据里不许有「今天」),只用它做相对先后与「有没有」。
    from services.kb_terminology_gate import RULING_RULE_ID, RULING_VERSION
    from services.gap_operation_map import declared_frontend_routes

    routes = sorted(declared_frontend_routes())
    manifest = {
        "content_version": _CONTENT_VERSION,
        "release_sha": _current_release_sha(release_sha),
        "operation_registry_version": OPERATION_REGISTRY_VERSION,
        "source_hashes": hashes,
        "chunk_counts": {key: len(rows) for key, rows in groups.items()},
        "terminology_rule_id": RULING_RULE_ID,
        "terminology_rule_version": RULING_VERSION,
        # 存**指纹 + 条数**而不是整张表:整张表进 manifest 会让这一行几十 KB,
        # 而对账只需要「变没变」和「分母还在不在」(条数为 0 = 取数坏了,不是没路由)。
        "route_manifest_hash": hashlib.sha256(
            json.dumps(routes, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "route_count": len(routes),
        "built_at": _utc_now_iso(),
    }
    result = replace_chunks_transactionally(
        source_types=("doc", "faq", "preset"), chunks=all_rows, manifest=manifest
    )
    return {**result, "manifest": manifest}


async def backfill_embeddings(batch_size: int = 10, source_types: list[str] | None = None) -> dict:
    """为缺 embedding 的 chunks 批量生成向量

    设计:reindex 插入时不算 embedding(纯本地操作快),这一步统一调 DashScope API
    批量算。失败 fallback 到纯 BM25。

    source_types: 只处理指定类型的 chunk(如系统知识库 reindex 后只补
                  sys_page/sys_field/sys_button/sys_error/sys_qa,不动旧 doc/faq/preset)。
                  None=处理所有缺 embedding 的 chunk。
    """
    from dotenv import load_dotenv
    load_dotenv()  # 确保 DASHSCOPE_API_KEY 可读
    from db.connection import get_connection
    from tools.xiaobang_embed import embed_texts

    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = """
            SELECT id, source_title, section_title, content, route, source_type
            FROM kb_chunks
            WHERE embedding IS NULL
        """
        params: list = []
        if source_types:
            sql += " AND source_type = ANY(%s)"
            params.append(list(source_types))
        sql += " ORDER BY id"
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    finally:
        conn.close()

    if not rows:
        return {"ok": True, "skipped": "no chunks missing embedding"}

    # 构造 embedding 输入文本(富文本,不止正文):
    #   页面名 ｜ 路由 ｜ chunk类型 ｜ 小节(字段/按钮名) ｜ 正文
    # 让"这个红色提示什么意思""这里为什么点不了"也能命中 sys_error/sys_button。
    texts = []
    for r in rows:
        title = r.get("source_title") or ""
        route = r.get("route") or ""
        stype = r.get("source_type") or ""
        sec = r.get("section_title") or ""
        body = r.get("content") or ""
        # 单条文本控制在 800 字以内(DashScope 单文本 token 上限友好)
        combined = f"{title}｜{route}｜{stype}｜{sec}\n{body}"[:800]
        texts.append(combined)

    logger.info("调 DashScope 批量算 embedding · 共 %d 条 chunks", len(texts))
    vectors = await embed_texts(texts, text_type="document")
    if vectors is None:
        logger.warning("embedding 调用失败 · 跳过(BM25 仍可用)")
        return {"ok": False, "reason": "dashscope failed", "fallback": "bm25_only"}

    # 写回 DB
    import json as _json
    conn = get_connection()
    try:
        cur = conn.cursor()
        updated = 0
        for r, vec in zip(rows, vectors):
            cur.execute(
                "UPDATE kb_chunks SET embedding = %s::jsonb, updated_at = NOW() WHERE id = %s",
                (_json.dumps(vec), r["id"]),
            )
            updated += 1
        conn.commit()
        logger.info("embedding 写入完成 · %d 行", updated)
        return {"ok": True, "updated": updated}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="事务化重建小榜知识 release")
    parser.add_argument("--release-sha", help="本次发布对应的 40 位 git SHA")
    parser.add_argument(
        "--with-embeddings",
        action="store_true",
        help="显式调用现役 embedding 服务；默认关闭",
    )
    args = parser.parse_args()
    if args.release_sha and not re.fullmatch(r"[0-9a-fA-F]{40}", args.release_sha):
        parser.error("--release-sha 必须是 40 位十六进制 git SHA")

    result = rebuild_release(args.release_sha)
    logger.info(
        "索引完成 · release 切换完成 · deleted=%d inserted=%d",
        result["deleted"], result["inserted"],
    )
    logger.info("manifest=%s", json.dumps(result["manifest"], ensure_ascii=False, sort_keys=True))

    if args.with_embeddings:
        logger.info("显式补 embedding...")
        embed_result = asyncio.run(backfill_embeddings(source_types=["doc", "faq", "preset"]))
        logger.info("  → %s", embed_result)

    counts = count_chunks()
    logger.info("=" * 40)
    logger.info("当前 kb_chunks 表（manifest 不参与召回）:")
    for k, v in counts.items():
        logger.info("  %s: %d", k, v)
    logger.info("  合计: %d", sum(counts.values()))


if __name__ == "__main__":
    main()
