"""
GEO 调研监测 · 规则清洗子模块 [Phase 9 · 2026-05-25]

替换原 cleaner.py (LLM 清洗 qwen-turbo ¥0.002/篇) 为纯规则版,0 LLM 成本。
外部已验证(AI回答爬虫 2026-05-23 跑 16 行业 1.6 万篇文章,无明显效果损失)。

公共 API:
    clean_markdown(raw_md: str) -> str
        输入原始 Jina markdown,输出去链接噪声 + 截尾 + 模板剔除后的正文 md
    text_length(md: str) -> int
        估算正文纯文本字数(去 frontmatter / markdown 标记 / 链接 url / 空白)
    link_char_ratio(line: str) -> float
    strip_links_keep_text(line: str) -> str
    split_frontmatter(md: str) -> tuple[str, str]

清洗策略(激进去链接版):
    1. 多个 H1 选最长段落作为正文(解决潮新闻这种两个 H1 的页面)
    2. 关键词尾部截断: 相关阅读 / 评论区 / 抢首评 / 意见反馈 / Close Ad ...
    3. 推荐位墙截断: 连续 2 行以上 "# [标题](链接)" 形式 → 第一行起整段切掉(搜狐推荐位常见)
    4. 整行链接字符占比 > 50% 直接丢(标签云、友情链接、导航条)
    5. 低链接占比行: 把 [text](url) 收成纯 text、删 ![](...) 图片、删 [](url) 空链接、删裸 URL
    6. 清洗后 < 100 字符则回退原文(防止清洗过头)
"""
import re

# === 单行删除(不截断后续,只删本行)===
INLINE_NOISE_KEYWORDS = [
    "下载APP", "下载app", "下载客户端", "客户端下载",
    "扫描二维码", "扫描下方二维码", "扫码下载",
    "读报", "微博", "分享到",
    "复制链接", "复制成功",
    "点赞", "收藏",
]

# === 尾部截断(遇到就丢掉它和后面所有内容)===
TAIL_NOISE_KEYWORDS = [
    "相关阅读", "相关推荐", "推荐阅读", "热门推荐",
    "热门文章", "为您推荐", "猜你喜欢",
    "免责声明", "版权声明", "本文来自",
    "评论区", "热门评论", "查看评论", "全部评论",
    "抢首评", "发表评论", "我要评论",
    "上一篇：", "下一篇：", "上一篇 ", "下一篇 ",
    "返回首页", "回到顶部", "返回搜狐", "查看更多",
    "转载请注明", "投稿邮箱",
    "意见反馈", "隐私政策", "Close Ad",
    "请搜索查找用户", "选择@的用户",
    "新手上路", "我有疑问", "打开微信",
    "百科协议", "百科任务", "百科商城",
    "选择朗读音色", "朗读音色",
]

# 整行删除模式
LINE_DROP_PATTERNS = [
    re.compile(r"^\s*Image\s+\d", re.IGNORECASE),       # "Image 1" 之类的图片占位
    re.compile(r"^\s*Video\s+\d", re.IGNORECASE),       # "Video 1"
    re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*$"),  # 单独时间行
    re.compile(r"^\s*=+\s*$"),                          # 分隔符
    re.compile(r"^\s*-{3,}\s*$"),                       # markdown 分隔线
    re.compile(r"^\s*\d+\s*$"),                         # 单独的数字行(页码/计数)
    re.compile(r"^\s*\d{1,2}:\d{2}\s*$"),               # 单独视频时长 05:34
    re.compile(r"^\s*\d+(?:\.\d+)?x\s*$", re.IGNORECASE),  # 播放器倍速 1.5x / 1x
    re.compile(r"^\s*[★☆✓✗•‣⁃·]+\s*$"),                 # 单独装饰符号
    re.compile(r"^\s*\|+\s*$"),                         # 空表格线
]

# 嵌套图片链接 [![...](...)](...)
NESTED_IMG_LINK = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")

# Markdown 链接族
_MD_LINK_FULL = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")   # [text](url)
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")            # ![alt](url)
_EMPTY_LINK = re.compile(r"\[\s*\]\([^)]+\)")            # [](url) 空文本链接
_BARE_URL = re.compile(r"https?://\S+")                   # 裸 URL
_JS_LINK = re.compile(r"\[[^\]]*\]\(javascript:[^)]*\)")  # javascript: 链接

# H1/H2 包链接 = 推荐位特征行
_RECO_H_LINK = re.compile(r"^\s*#+\s*\[")

# 计字数用的统计正则
_MD_SYMBOLS = re.compile(r"[#*_`>~+|]")
_WS = re.compile(r"\s+")


def link_char_ratio(line: str) -> float:
    """整行字符里被 markdown 链接(含图片/裸 URL/JS 链接)占据的比例"""
    s = line.strip()
    if not s:
        return 0.0
    link_chars = 0
    for pattern in (_MD_IMG, _JS_LINK, _MD_LINK_FULL, _BARE_URL):
        for m in pattern.finditer(line):
            link_chars += len(m.group(0))
    # 整体长度按 strip 后算(避免行首空白稀释比例)
    return min(1.0, link_chars / len(s))


def strip_links_keep_text(line: str) -> str:
    """删图片/空链接/裸 URL/JS 链接,把 [text](url) 收成纯 text"""
    line = _MD_IMG.sub("", line)
    line = _JS_LINK.sub("", line)
    line = _EMPTY_LINK.sub("", line)
    line = _BARE_URL.sub("", line)
    line = _MD_LINK_FULL.sub(lambda m: m.group(1), line)
    return line


def text_length(md: str) -> int:
    """估算正文纯文本字数(去 frontmatter / markdown 标记 / 链接 url / 空白)"""
    if md.startswith("---\n"):
        idx = md.find("\n---\n", 4)
        if idx != -1:
            md = md[idx + 5:]
    text = _MD_IMG.sub("", md)
    text = _JS_LINK.sub("", text)
    text = _BARE_URL.sub("", text)
    text = _MD_LINK_FULL.sub(lambda m: m.group(1), text)
    text = _MD_SYMBOLS.sub("", text)
    text = _WS.sub("", text)
    return len(text)


def split_frontmatter(md: str) -> tuple[str, str]:
    """拆 markdown frontmatter (---\\n...\\n---\\n) 与正文,返回 (front, body)"""
    if md.startswith("---\n"):
        idx = md.find("\n---\n", 4)
        if idx != -1:
            return md[: idx + 5], md[idx + 5:]
    return "", md


def find_main_section(lines: list[str]) -> tuple[int, int]:
    """多个 H1 选最长段落(按字符数)作为正文。返回 (start, end) 行号。"""
    h1_positions = [
        i for i, line in enumerate(lines)
        if line.strip().startswith("# ") and len(line.strip()) > 5
    ]
    if not h1_positions:
        return 0, len(lines)
    if len(h1_positions) == 1:
        return h1_positions[0], len(lines)
    best_idx = 0
    best_len = 0
    for i, pos in enumerate(h1_positions):
        end = h1_positions[i + 1] if i + 1 < len(h1_positions) else len(lines)
        section_chars = sum(len(line) for line in lines[pos:end])
        if section_chars > best_len:
            best_len = section_chars
            best_idx = i
    start = h1_positions[best_idx]
    end = h1_positions[best_idx + 1] if best_idx + 1 < len(h1_positions) else len(lines)
    return start, end


def find_reco_wall_cutoff(lines: list[str], start: int, end: int) -> int:
    """连续 2+ 行 "# [...](...)" 推荐位 → 返回第一行位置(截断点)"""
    count = 0
    first_pos = -1
    for i in range(start, end):
        s = lines[i].strip()
        if not s:
            continue  # 空行不打断也不计数
        is_reco = (
            _RECO_H_LINK.match(s) is not None
            and link_char_ratio(s) > 0.5
        )
        if is_reco:
            if count == 0:
                first_pos = i
            count += 1
            if count >= 2:
                return first_pos
        else:
            count = 0
            first_pos = -1
    return end


def find_keyword_cutoff(lines: list[str], start: int, end: int) -> int:
    """遇到 TAIL_NOISE_KEYWORDS 命中行 → 返回该行位置(截断点)"""
    for i in range(start, end):
        line = lines[i].strip()
        if not line:
            continue
        if any(kw in line for kw in TAIL_NOISE_KEYWORDS):
            return i
    return end


def is_inline_noise(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    return any(kw in s for kw in INLINE_NOISE_KEYWORDS)


def is_drop_pattern(line: str) -> bool:
    return any(p.match(line) for p in LINE_DROP_PATTERNS)


def clean_markdown(md: str) -> str:
    """规则清洗主入口
    输入: 原始 Jina markdown (可带 frontmatter)
    输出: 清洗后 markdown (保留 frontmatter)
    清洗失败/清洗过头 (< 100 字符) 自动回退原文
    """
    front, body = split_frontmatter(md)

    if "# 抓取失败" in body[:300]:
        return md

    # 先把嵌套图片链接整体替换掉
    body = NESTED_IMG_LINK.sub("", body)

    lines = body.splitlines()
    start, end = find_main_section(lines)

    # 两套截断取最早的
    end = min(
        end,
        find_keyword_cutoff(lines, start, end),
        find_reco_wall_cutoff(lines, start, end),
    )

    section = lines[start:end]

    # 逐行过滤
    cleaned: list[str] = []
    blank_streak = 0
    for line in section:
        s = line.rstrip()

        if is_drop_pattern(s):
            continue
        if is_inline_noise(s):
            continue

        stripped = s.strip()
        if stripped:
            # 整行高链接占比 → 丢
            if link_char_ratio(s) > 0.5:
                continue
            # 收链接: [text](url) → text, 删图片/空链接/裸 URL
            s = strip_links_keep_text(s).rstrip()
            # 收完只剩空白或标点 → 当空行处理
            if not s.strip() or len(re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)) < 2:
                blank_streak += 1
                if blank_streak <= 1:
                    cleaned.append("")
                continue
            blank_streak = 0
            cleaned.append(s)
        else:
            blank_streak += 1
            if blank_streak <= 1:
                cleaned.append("")

    new_body = "\n".join(cleaned).strip() + "\n"
    if len(new_body) < 100:
        # 清洗过头,回退原文
        return md
    return front + "\n" + new_body if front else new_body
