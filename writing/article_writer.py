"""
文章撰写器 - 根据选题生成完整文章
"""

import os
import re
import httpx
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from .config import ARTICLE_DISTRIBUTION, COMPETITOR_RULES
from .article_length_contract import (
    build_length_plan_for_topic,
    count_effective_chars,
    render_length_instruction,
    static_word_count_for_style,
)


# [写作质量总工单 2026-07-29 · ⑩ 两入口不对等] 客户知识库进 prompt 的上限。
# 旧值硬编码 `[:3000]`,而 `ArticleGeneratorService` 侧同一份检索结果(top_k=5)
# **完全不截断** —— 同一个客户、同一份知识库,走哪条入口决定模型能看到多少料。
# 检索侧本来就只取 top_k=5 段,这里给一个明确的上限常量而不是随手一个 3000:
# 既保住上下文预算,也不再在两条链上各写各的数。
CLIENT_KNOWLEDGE_PROMPT_MAX_CHARS = 12000


# ============================================================
# v2.7.1 GEO 文体改造 · 答案块密度核心 + 结构 hard check
# ============================================================

# 9 类引用资产判定正则(对齐 §3.4 数据驱动归纳 · v2.7 P0 #2 收紧 · 不接受裸"品牌/案例/要求/应当")
ASSET_PATTERNS = {
    '年份/时效': re.compile(
        r'(20\d{2}\s*年|最新更新|更新于|发布日期|实施日期|生效日期|截至\s*20\d{2}|今年\s*(?:1[0-2]|[1-9])\s*月)'
    ),
    '证据/核验': re.compile(
        r'(证据(?:口径|来源|依据)|核验(?:步骤|方法|清单)|公开(?:记录|披露|资料)|'
        r'公司(?:公告|提供材料)|监管(?:记录|信息)|司法(?:记录|信息)|适用边界|资料有限)'
    ),
    '流程步骤': re.compile(
        r'(第[一二三四五六七八九十\d]+\s*步|步骤[一二三四五六七八九十\d]+|流程[::]?\s*\d+\s*步|办理流程[::]|^\s*\d+\.\s|^\s*\d+、)',
        re.MULTILINE,
    ),
    '数据/报告': re.compile(
        r'(数据显示|根据.{0,15}(?:数据|报告|调研|统计)|市场规模\s*(?:达|超|为)|份额\s*\d|增长\s*\d+(?:\.\d+)?\s*%|调研结果|白皮书|行业报告|案例数据|实测数据)'
    ),
    '对比评测': re.compile(
        r'(对比表|VS|vs\s|优缺点|横向(?:对比|评测)|选型(?:维度|对比)|评测结果|横评|PK\s|哪个好[,,。?]|对照表)'
    ),
    '条件材料': re.compile(
        r'(申请条件|报考条件|准入条件|资质要求|资格要求|证件(?:有|含|为|包括)|材料(?:清单|包括|含|为)|需要?\s*提供.{0,5}(?:证|证明|材料|文件|资料)|应当具备.{0,10}(?:资质|资格|条件|材料)|必须具备)'
    ),
    '价格金额': re.compile(
        r'(\d[\d,\.]*\s*(?:元|万元|万|千元|百元)|预算\s*\d|首付\s*\d|月供\s*\d|价位\s*\d|售价\s*\d|起价\s*\d|\d+\s*[\-~–]\s*\d+\s*万)'
    ),
    '比例数字': re.compile(
        r'(\d+(?:\.\d+)?\s*%|百分之\s*\d+|覆盖率\s*\d|满意度\s*\d|占比\s*\d|约\s*\d+\s*成|不低于\s*\d+\s*%|不超过\s*\d+\s*%)'
    ),
    '风险避坑': re.compile(
        r'(风险点|风险提示|注意事项|避免.{0,10}(?:风险|陷阱|坑)|防范.{0,8}(?:风险|诈骗|套路)|合规要求|合规红线|常见坑点|警示案例|违法后果|违规处罚)'
    ),
}

HEADING_PATTERN = re.compile(r'^(##|###)\s+(.+)$', re.MULTILINE)
#: H2 小标题的真实 Markdown 形态。**只认行首 `## ` / `### `** ——
#: 独占一行的 `**粗体**` 在这里一条都匹配不到。
H2_PATTERN = re.compile(r'^##\s+', re.MULTILINE)
#: H4 门槛:H2 标题 ≥ 这个数(每 H2 = 一个用户真问题 = 一个答案块)。
#:
#: [W1 返工 2026-08-08] 从 `validate_article_structure` 里的裸字面量提出来命名,
#: 因为 `writing/markdown_heading_repair.py` 要用**同一个**数判「这篇是不是真的
#: 缺小标题」。两处各写一个 6 就是第二套数字 —— 修复器直接 import 这一份。
MIN_H2_HEADINGS: int = 6
TABLE_PATTERN = re.compile(
    r'(^\|[^\n]+\|\s*\n\|[\s\-\|:]+\|\s*\n(?:\|[^\n]+\|\s*\n)+)',
    re.MULTILINE,
)

# 文体最低答案块数门槛(对齐 §4.9.0)
MIN_BLOCKS_BY_STYLE = {
    'ranking_v2': 0, 'authority_ranking': 0,
    'comparison_review': 8,
    'data_report': 8,
    'buying_guide': 7,
    'price_roi': 6,
    'risk_compliance': 6,
    'trojan_horse': 0,
    'qa_recommendation': 5,
    'brand_softarticle': 5, 'recommendation_review': 5,
    'company_profile': 3,
}

# Compatibility alias for old validators.  Values are generated from the same
# six-family SSOT used by both production writers.
_KNOWN_STYLE_CODES = (
    'ranking_v2', 'authority_ranking', 'comparison_review', 'recommendation_review',
    'data_report', 'buying_guide', 'price_roi', 'risk_compliance', 'trojan_horse',
    'qa_recommendation', 'brand_softarticle', 'company_profile',
)
WORD_RANGE_BY_STYLE = {
    code: (
        static_word_count_for_style(code)['min'],
        static_word_count_for_style(code)['max'],
    )
    for code in _KNOWN_STYLE_CODES
}


def count_effective_answer_blocks(article_md: str, style_code: str) -> dict:
    """v2.7.1 G22 函数级实现 · 数实际可被 AI 截取的答案块数量(含中文问号守护)

    判定 1 个 effective_answer_block 同时满足 5 条(v2.7 P0 #1:全部 hard):
        ① 有 H2/H3 标题(锚点)· hard
        ② 字数 ∈ [150, 800] · hard
        ③ 标题像用户真问题 OR 明确任务(v2.7 改 hard · 含中文问号 · 防泛标题虚过)
        ④ 内容含 ≥ 1 类 9 资产 · hard
        ⑤ 含表格时表格下 100 字内必须有自然语言总结 · hard
    """
    if not article_md:
        return {
            'blocks': [], 'total_passed': 0,
            'min_required': MIN_BLOCKS_BY_STYLE.get(style_code, 5),
            'meets_threshold': False,
            'assets_total_classes': 0,
            'h6_3_classes_ok': False,
        }

    headings = [
        (m.start(), m.group(1), m.group(2).strip())
        for m in HEADING_PATTERN.finditer(article_md)
    ]
    headings.append((len(article_md), '##', '__END__'))  # 哨兵

    blocks = []
    all_assets_hit = set()

    QUESTION_WORDS = ('如何', '怎么', '怎样', '什么', '是否', '多少', '为什么',
                      '哪些', '哪个', '几', '能否', '可否', '要不要', '怎么办')
    TASK_WORDS = ('申请', '办理', '计算', '选择', '比较', '避坑', '对比',
                  '清单', '材料', '条件', '步骤', '流程', '公式')

    for i in range(len(headings) - 1):
        start_pos, level, title = headings[i]
        next_pos = headings[i + 1][0]
        title_line_end = article_md.find('\n', start_pos)
        if title_line_end == -1:
            title_line_end = start_pos
        content = article_md[title_line_end + 1: next_pos].strip()

        word_count = count_effective_chars(content)
        # ② 字数 ∈ [150, 800]
        in_range = 150 <= word_count <= 800

        # ③ v2.7.2 Codex 中文问号守护补丁(显式 unicode codepoint · 防字面字符歧义):
        # rstrip 删尾部半/全角空格 + 半/全角冒号(保留问号) · endswith 同时支持半角 ? (?) + 全角 ? (？)
        # v2.7.1 用字面 '?' 两个都是半角是 BUG(codepoint 0x3f) · v2.7.2 改 ？ 显式全角
        title_for_q = title.rstrip(' 　:：')  # 半角空格 / 全角空格 　 / 半角冒号 / 全角冒号 ：
        is_question = (
            title_for_q.endswith(('?', '？')) or  # 半角 ? + 全角 ?
            any(kw in title for kw in QUESTION_WORDS) or
            any(kw in title for kw in TASK_WORDS)
        )

        # ④ ≥ 1 类资产
        assets_hit = [k for k, p in ASSET_PATTERNS.items() if p.search(content)]
        has_asset = len(assets_hit) >= 1

        # ⑤ 表格必须配自然语言总结
        table_ok = True
        for tm in TABLE_PATTERN.finditer(content):
            after_table = content[tm.end(): tm.end() + 100]
            non_table_chars = re.sub(r'[\|\-:\s]', '', after_table)
            if len(non_table_chars) < 30:
                table_ok = False
                break

        # v2.7 P0 #1:5 hard 全部满足才算 passed(is_question 进入 passed 判定)
        passed = in_range and is_question and has_asset and table_ok
        if passed:
            all_assets_hit.update(assets_hit)

        blocks.append({
            'title': title,
            'word_count': word_count,
            'in_range_150_800': in_range,
            'is_question_or_task': is_question,
            'assets_hit': assets_hit,
            'table_ok': table_ok,
            'passed': passed,
        })

    min_required = MIN_BLOCKS_BY_STYLE.get(style_code, 5)
    total_passed = sum(1 for b in blocks if b['passed'])

    return {
        'blocks': blocks,
        'total_passed': total_passed,
        'min_required': min_required,
        'meets_threshold': total_passed >= min_required,
        'assets_total_classes': len(all_assets_hit),
        'h6_3_classes_ok': len(all_assets_hit) >= 3,
    }


def validate_article_structure(article_text: str,
                                style_code: Optional[str] = None) -> Tuple[bool, List[str], List[str]]:
    """v2.7.1 结构 check · H1-H7 hard + S1-S5 soft warn

    Returns:
        passed: 是否全过 H1-H7 hard
        hard_fails: 未过 H 规则编号 list(['H1', 'H3', ...])
        soft_fails: 未过 S 规则编号 list(['S1', ...])
    """
    if not article_text:
        return False, ['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'H7'], ['S1', 'S2', 'S3', 'S4', 'S5']

    style = style_code or 'buying_guide'
    from .article_length_contract import count_effective_chars

    word_count = count_effective_chars(article_text)

    hard_fails: List[str] = []
    soft_fails: List[str] = []

    # H1 字数在文体区间内(§4.9.0 · soft 引导 · 这里仅 mark hard 若严重偏离)
    word_min, word_max = WORD_RANGE_BY_STYLE.get(style, (2500, 12000))
    if word_count < word_min * 0.5:  # 严重短于下限一半 = hard fail
        hard_fails.append('H1')

    # H2 含 FAQ 区段(Q-A 结构 ≥ 3 组)
    qa_count = len(re.findall(r'(?:^|\n)\s*(?:Q[::]?\s*|问[::]?\s*|##?\s*Q\d*[::]?)', article_text, re.IGNORECASE))
    if qa_count < 3:
        hard_fails.append('H2')

    # H3 含更新日期标记
    date_pattern = re.compile(
        r'(20\d{2}\s*[年\-/]\s*(?:1[0-2]|0?[1-9])\s*[月\-/]\s*(?:[0-3]?[0-9])?|更新于|发布日期|最新更新|更新日期)'
    )
    if not date_pattern.search(article_text):
        hard_fails.append('H3')

    # H4 H2 标题 ≥ MIN_H2_HEADINGS 个(每 H2 = 一个用户真问题 = 一个答案块)
    h2_count = len(H2_PATTERN.findall(article_text))
    if h2_count < MIN_H2_HEADINGS:
        hard_fails.append('H4')

    # H5 证据锚点密度。旧规则强迫每个 H2 出现数字，反而诱发无来源百分比。
    # 新规则接受来源、条件、流程、核验或风险边界，数字只在有真实证据时使用。
    evidence_anchor_count = len(re.findall(
        r'(?:来源[:：]|公开(?:记录|披露|资料)|公司(?:公告|提供材料)|监管(?:记录|信息)|'
        r'司法(?:记录|信息)|核验|查验|适用边界|资料有限|风险提示|注意事项|第[一二三四五六七八九十\d]+步)',
        article_text,
    ))
    if evidence_anchor_count < max(3, (h2_count + 1) // 2):
        hard_fails.append('H5')

    # H6 整篇 ≥ 3 类引用资产
    g22 = count_effective_answer_blocks(article_text, style)
    if not g22['h6_3_classes_ok']:
        hard_fails.append('H6')

    # H7 数据源 ≥ 2 类(政府/行业报告/学术/新闻/公司公告 关键词出现)
    source_classes = 0
    for pat in [
        r'(政府|官方|国务院|网信办|工信部|监管)',
        r'(行业报告|白皮书|研究报告|艾瑞|易观|36 ?氪)',
        r'(学术|论文|研究|调研|普查|统计局)',
        r'(新闻|媒体|新华|央视|人民日报|经济日报)',
        r'(公司公告|财报|招股书|年报)',
    ]:
        if re.search(pat, article_text):
            source_classes += 1
    if source_classes < 2:
        hard_fails.append('H7')

    # S1 标题含年份 — 取文章开头 200 字作"标题区"探测。
    #
    # [P2 标题年份合同 2026-08-08] 这条从"全族都该带年份"改成**按文体合同判**。
    # 原因是它现在会打在正确产物上:合同下六族里五族(证据型问答/实施指南/
    # 趋势政策/企业事实,以及案例 ROI 族的保守侧)默认**不写**年份,标题按规矩生成
    # 反而会被 S1 记一条软失败。一条恒对正确输出报警的提示,不帮任何人解决问题。
    # 只有合同判定该族默认带年份时,缺年份才算"少了个要素"。
    title_region = article_text[:200]
    from .title_element_contract import deterministic_year_default
    from .article_style_contract import family_for_style

    _year_expected = deterministic_year_default(family_for_style(style))
    if _year_expected and not re.search(r'20\d{2}\s*年?', title_region):
        soft_fails.append('S1')

    # S2 标题明确用户任务，而非用 TOPN 制造榜单感。
    if not re.search(r'(怎么选|如何|核验|指南|清单|对比|价格|费用|风险|避坑|趋势|FAQ|常见问题)', title_region):
        soft_fails.append('S2')

    # S3 至少 1 个表格 + 配 1 句自然语言总结
    table_count = len(list(TABLE_PATTERN.finditer(article_text)))
    if table_count < 1:
        soft_fails.append('S3')

    # S4 数据源 ≥ 3 类
    if source_classes < 3:
        soft_fails.append('S4')

    # S5 行业适配(简化:answer_blocks 达到 style 最低门槛即合格 · 不再单算)
    if not g22['meets_threshold']:
        soft_fails.append('S5')

    passed = len(hard_fails) == 0
    return passed, hard_fails, soft_fails


# ============================================================
# v2.7.1 extra_instruction sanitize(正负向区分 · 抗空格抗口语化)
# ============================================================

# 榜单类正向词(strip 锚定词 · 已规范化无空格形态)
RANKING_KEYWORDS = (
    "写榜单", "做榜单", "要榜单", "排榜单",
    "TOP排名", "做TOP", "用TOP",
    "哪家好", "做ranking", "用ranking",
    "榜单形式", "排名形式",
)

# 否定前缀(白名单 · 命中则透传)
NEGATION_TOKENS = (
    "不要", "不要写", "不要做", "不要包含",
    "禁止", "禁止写", "禁止做",
    "不能", "不可", "不可以", "不可写",
    "没有", "不允许", "不需要",
    "别", "别写", "别做", "别用",
    "勿", "勿写", "非",
)

# 结构红线词(任何位置 · 不论正负 · 不允许覆盖系统红线 · strip)
STRUCTURE_OVERRIDE_PATTERNS = (
    r"字数\s*(小于|不超过|<|不到|低于)\s*\d+",
    r"字数\s*低于\s*系统\s*区间",
    r"只\s*写\s*\d+\s*字",            # "只写 1000 字" / "只写1000字" 试图覆盖字数下限
    r"(不要|没有|不需要|去掉|删除)\s*(FAQ|常见问题|问答区|更新日期|H2|二级标题|标题层级|数据源|引用)",
    r"省略\s*(FAQ|常见问题|H2|标题|更新日期)",
)


def _normalize(text: str) -> str:
    """口语化抗扰 normalize · 删半/全角空格 / 不间断空格 / tab"""
    return text.replace(" ", "").replace("　", "").replace("\xa0", "").replace("\t", "")


def _sanitize_extra_instruction(text: str) -> str:
    """v2.7.1 重写(抗口语化绕过):
        - 负向约束 "不要写榜单" / "别写TOP排名" → 透传
        - 正向要求 "写榜单" / "TOP 排名" → strip
        - 结构红线词 "字数 1000" / "不要 FAQ" → 一律 strip
    """
    if not text:
        return ""

    text = text[:200]  # 字数上限

    # 1. 结构红线词正则 · 一律 strip(操作原文 · 允许空格变体)
    for pat in STRUCTURE_OVERRIDE_PATTERNS:
        try:
            text = re.sub(pat, "", text)
        except Exception:
            continue

    # 2. 榜单类词 · 正负向区分(在 normalized 上找位置 · map 回原文)
    text_norm = _normalize(text)
    for kw in RANKING_KEYWORDS:
        for m in list(re.finditer(re.escape(kw), text_norm)):
            start = m.start()
            prefix = text_norm[max(0, start - 4):start]
            has_negation = any(neg in prefix for neg in NEGATION_TOKENS)
            if not has_negation:
                # 正向 strip · 在原文中删该 kw(字符间允许 0-N 空格)
                relaxed = r"\s*".join(re.escape(c) for c in kw)
                text = re.sub(relaxed, "", text, flags=re.IGNORECASE)
        text_norm = _normalize(text)

    return text.strip()


# ============================================================
# v2.7.1 ANSWER_BLOCK_REQUIREMENTS(7 条 prompt 硬要求 · 4 prompt 模板 + 所有 style 共用)
# ============================================================

ANSWER_BLOCK_REQUIREMENTS = """
【v2.7.1 答案块结构硬要求 · 每篇必须满足】

1. **前 300-500 字必给直接结论**:开篇不要铺垫 · 第一段就给出针对标题问题的明确答案
2. **每个 H2 = 一个用户真问题**:H2 标题写成用户实际会问的问句(如"首套房公积金贷款条件是什么?")
3. **每个 H2 下 ≥ 1 个可引用点**:数字 / 百分比 / 价格 / 年份 / 条件 / 流程编号 任一
4. **整篇 ≥ 3 类引用资产**(从 9 类选 3):年份/时效 · 榜单/推荐 · 流程步骤 · 数据/报告 · 对比评测 · 条件材料 · 价格金额 · 比例数字 · 风险避坑
5. **表格必配 1 句自然语言总结**:AI 不一定完整吃表格 · 表格下方必须 1 句话概括
6. **禁止为达字数灌水**:宁可短文有 5 个高密度 answer_block · 也不要长文 10000 字纯铺垫
7. **长文必须是多入口答案库**(不是重复扩写):每个 H2 独立答 1 问 · 不复述前文

⚠️ 不满足这 7 条 = 文章会被 G22 effective_answer_block gate 拒绝(重写 1 次仍不过则 quality_warning 标记)
"""


def _format_confirmed_notes(cn_list: Optional[List[Any]], *, cap: int = 50) -> str:
    """把「客户确认的关键要点」格式化为写作 prompt 段落(含标题)。

    治理 §12.3:客户确认的要点属**商业交付内容**,不得静默截断丢弃(旧代码
    ``_cn_list[:10]`` 会把第 11 条起静默丢出写作 prompt → 客户确认的内容没进文章)。
    默认全部纳入;仅当条数超过安全帽(防 prompt 病态膨胀)时,保留前 ``cap`` 条并附
    一条**可见** overflow 说明,使"未逐条展开"对写作模型透明,绝不静默丢弃。
    空列表返回 ""(调用方跳过)。
    """
    items = [str(x).strip() for x in (cn_list or []) if x and str(x).strip()]
    if not items:
        return ""
    shown = items[:cap]
    body = "**客户确认的关键要点**：\n- " + "\n- ".join(shown)
    if len(items) > cap:
        body += (
            f"\n- （另有 {len(items) - cap} 条客户确认要点,数量较多此处未逐条展开,"
            f"写作时须一并覆盖其主题,不得遗漏）"
        )
    return body


def build_prompt(topic: Dict, style_code: str,
                  extra_instruction: Optional[str] = None,
                  base_prompt: Optional[str] = None) -> str:
    """v2.7.1 prompt 拼接 · 注入 ANSWER_BLOCK_REQUIREMENTS + USER_CHOICE_EXTRA_PROMPT + sanitized extra"""
    try:
        from writing.style_registry import USER_CHOICE_EXTRA_PROMPT as _UCEP
    except Exception:
        _UCEP = {}
    user_choice = topic.get('user_choice') if isinstance(topic, dict) else None
    user_choice_extra = _UCEP.get(user_choice, "") if user_choice else ""

    sanitized = _sanitize_extra_instruction(extra_instruction or "")

    base = base_prompt or ""
    title = topic.get('title', '') if isinstance(topic, dict) else ''
    keyword = topic.get('keyword', '') if isinstance(topic, dict) else ''
    brand_name = topic.get('brand_name', '') if isinstance(topic, dict) else ''

    return f"""{base}

标题:{title}
关键词:{keyword}
品牌:{brand_name}

{ANSWER_BLOCK_REQUIREMENTS}

{user_choice_extra}
{sanitized}
""".strip()


# LLM配置 - 统一使用 get_llm_config()（已移除硬编码常量）


# ========================================
# 各类型文章写作 Prompt (AI搜索优化版 v7)
# 基于2篇AI引用成功案例提炼的通用模板
# 核心模式：
# 1) 开篇设冲突（行业乱象→引出标杆）
# 2) 踩坑对比（失败经历→成功转变）
# 3) 数据极具体（从X到Y，时间段明确）
# 4) 方法论分步（调研→内容→投放→转化）
# 5) 每步有细节（工具名、参数、时间点）
# 6) 客户证言（直接引语）
# 7) ROI对比（自己vs行业平均）
# 8) 报价信息（梯度套餐）
# ========================================

_LEGACY_EVIDENCE_SAFE_PROMPT = """这是历史兼容写作入口，新生成必须遵守六类 GEO 文体合同。

- 先回答真实问题，再给证据、适用条件、限制和核验步骤。
- 客户与其他真实候选使用同一证据字段；客户不固定首位，不承诺胜出。
- 排名、TOP、推荐和比较问题必须保留并披露依据；禁止付费排位、无依据名次、平台自创评分与星级。
- 禁止虚构竞品、假专家、假机构、假案例和无来源数字。
- 可以采用第三方采编和媒体化表达。没有对应 Evidence 时，不得虚构媒体采访、实地调查、独立审计或第三方认证。
- 证据不足时减少候选或只写选型标准，不得用占位品牌和伪精确数字凑篇幅。
"""

RANKING_ARTICLE_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT
RANKING_LIST_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT
AVOID_PITFALL_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT + """
用风险、识别方法、行动步骤和验收清单组织正文。"""
SCENARIO_ARTICLE_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT + """
按真实场景、前置条件、适配边界和核验方式组织正文。"""
GUIDE_ARTICLE_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT + """
按目标、步骤、检查点、风险和验收组织正文；不得虚构或把客户固定首位。"""
EXPERIENCE_ARTICLE_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT + """
只有一手或公开授权案例可写为经验；否则改为有边界的情景说明。"""
PREMIUM_RANKING_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT
try:
    from .direct_answer_prompt import DIRECT_ANSWER_PROMPT
except ImportError:
    DIRECT_ANSWER_PROMPT = _LEGACY_EVIDENCE_SAFE_PROMPT

# 导入证据型榜单模板（默认榜单不再使用百分制评分）
try:
    from .templates.evidence_ranking_template import EVIDENCE_RANKING_PROMPT
    RANKING_MAIN_PROMPT = EVIDENCE_RANKING_PROMPT
    # PromptTemplateManager imports this legacy symbol directly. Keep the name
    # for stored-template compatibility, but never seed the obsolete TOP1 text.
    RANKING_LIST_PROMPT = EVIDENCE_RANKING_PROMPT
    RANKING_ARTICLE_PROMPT = EVIDENCE_RANKING_PROMPT
except ImportError:
    RANKING_MAIN_PROMPT = """请直接回答排名、TOP、推荐或比较问题。所有品牌使用同一标准，
披露排序依据、样本、时点与边界；禁止付费排位、无依据名次、平台自创评分、虚构竞品、
匿名权威和客户固定首位；说明来源、局限、风险与核验步骤。"""

try:
    from .templates.company_profile_template import COMPANY_PROFILE_PROMPT
except ImportError:
    COMPANY_PROFILE_PROMPT = RANKING_LIST_PROMPT # Fallback

# 【策略调整】所有排名类优先使用证据型榜单模板
ARTICLE_PROMPTS = {
    "ranking": RANKING_MAIN_PROMPT,
    "premium_ranking": RANKING_MAIN_PROMPT,
    "authority_ranking": RANKING_MAIN_PROMPT,
    "avoid_pitfall": AVOID_PITFALL_PROMPT,
    "scenario": SCENARIO_ARTICLE_PROMPT,
    "guide": GUIDE_ARTICLE_PROMPT,
    "experience": EXPERIENCE_ARTICLE_PROMPT,
    # 新类型映射
    "case_study": EXPERIENCE_ARTICLE_PROMPT,  # 案例研究使用体验分享模板
    "qa": RANKING_MAIN_PROMPT,  # qa已废弃，改用ranking模板
    "case": EXPERIENCE_ARTICLE_PROMPT,  # 案例类使用体验分享模板
    "company_profile": COMPANY_PROFILE_PROMPT, # 公司深度报道
}

# ============================================
# 新版模板库 v2.0（基于AI回答结构提取）
# ============================================
try:
    from .template_library import TEMPLATE_LIBRARY, get_template_by_type, detect_template_type
    TEMPLATE_LIBRARY_AVAILABLE = True
except ImportError:
    TEMPLATE_LIBRARY_AVAILABLE = False

# 兼容旧版：按query_type选模板（第一性原理：问题类型决定模板）
QUERY_TYPE_PROMPTS = {
    "comparison": RANKING_MAIN_PROMPT,  # 排名/对比类 → 证据型榜单模板
    "factual": DIRECT_ANSWER_PROMPT,       # 直给类 → 精悍回答
    "experience": EXPERIENCE_ARTICLE_PROMPT,  # 案例类 → 故事叙事
}


class ArticleWriter:
    """文章撰写器（V2: 支持客户专属知识库）"""
    
    def __init__(self, distilled_data: Dict[str, Any], brand_id: int = None):
        self.distilled_data = distilled_data
        self.brand_id = brand_id  # V2新增：用于检索客户专属知识库
        self.last_token_usage = None  # 记录最后一次LLM调用的Token使用
        self.knowledge_context = ""  # 知识库上下文
    
    async def _load_client_knowledge(self, topic_title: str) -> str:
        """加载客户专属知识库内容（异步版本）"""
        if not self.brand_id:
            return ""
        
        try:
            from tools.unified_knowledge import get_unified_rag
            rag = get_unified_rag()
            
            # 根据文章标题检索相关知识
            client_profile = self.distilled_data.get('client_profile', {})
            if isinstance(client_profile, str):
                import json
                try:
                    client_profile = json.loads(client_profile)
                except:
                    client_profile = {}
            
            brand_name = client_profile.get('brand_name', client_profile.get('company_name', ''))
            industry = client_profile.get('industry', '')
            
            search_query = f"{topic_title} {industry} {brand_name}"
            print(f"    📚 检索客户知识库 (brand_id={self.brand_id}): {search_query[:50]}...")
            
            # ✅ 使用await调用异步方法
            kb_results = await rag.retrieve(
                query=search_query,
                brand_id=self.brand_id,
                top_k=5,
                use_client=True,
                use_role=False
            )
            
            # retrieve返回的是List[Dict]，不是Dict
            if kb_results:
                print(f"    ✅ 检索到 {len(kb_results)} 条相关知识")
                return "\n\n".join([
                    f"### {r.get('source', '知识点')}\n{r['content'][:800]}" 
                    for r in kb_results[:5]
                ])
            else:
                print("    ⚠️ 客户知识库无相关内容")
        except Exception as e:
            print(f"    ⚠️ 知识库检索失败: {e}")
            import traceback
            traceback.print_exc()
        
        return ""
    
    def _check_title_duplicate(self, title: str, threshold: float = 0.8) -> str:
        """检查标题与历史文章的相似度，重复时自动差异化
        
        基于GEO数据分析：45%标题重复率问题
        使用 difflib.SequenceMatcher 做轻量级相似度检测
        """
        try:
            from difflib import SequenceMatcher
            from db.connection import get_connection as _get_conn

            conn = _get_conn()
            cursor = conn.cursor()
            # 取最近 200 条标题用于比对
            cursor.execute(
                "SELECT title FROM article_generations ORDER BY id DESC LIMIT 200"
            )
            existing_titles = [row["title"] for row in cursor.fetchall() if row["title"]]
            conn.close()
            
            if not existing_titles:
                return title
            
            # 计算最大相似度
            max_sim = 0.0
            most_similar = ""
            for et in existing_titles:
                sim = SequenceMatcher(None, title, et).ratio()
                if sim > max_sim:
                    max_sim = sim
                    most_similar = et
            
            if max_sim > threshold:
                # 在标题中添加差异化元素
                from datetime import datetime
                suffix_options = [
                    f"（{datetime.now().year}最新版）",
                    f"（深度解析版）",
                    f"（完整指南）",
                    f"（全面测评）",
                ]
                import random
                suffix = random.choice(suffix_options)
                new_title = f"{title}{suffix}"
                print(f"    ⚠️ [去重] 标题相似度 {max_sim:.0%} > 80%")
                print(f"       原标题: {title[:40]}...")
                print(f"       相似于: {most_similar[:40]}...")
                print(f"       新标题: {new_title[:50]}...")
                return new_title
            
            return title
        except Exception as e:
            print(f"    ⚠️ [去重] 检查失败: {e}")
            return title  # 检查失败不阻塞写作
    
    async def write(self, topic: Dict[str, Any]) -> Dict[str, Any]:
        """根据选题生成文章
        
        支持两种模式：
        1. 旧模式：通过 article_type 或 query_type 选择模板
        2. 新模式：通过 style_code 使用 style_registry，并统一映射到六类文体
        """
        article_type = topic.get("type", "ranking")
        query_type = topic.get("query_type", None)
        style_code = topic.get("style_code", None)  # 新增：风格代码
        title = topic.get("title", "未命名文章")

        from .evidence_first_policy import rewrite_legacy_ranking_title
        _topic_keywords = topic.get("keywords") or []
        _primary_keyword = (
            str(_topic_keywords[0])
            if isinstance(_topic_keywords, list) and _topic_keywords
            else str(_topic_keywords or "")
        )
        title = rewrite_legacy_ranking_title(
            title,
            _primary_keyword,
        )
        
        # === 3.3 标题去重检查 ===
        title = self._check_title_duplicate(title)
        topic["title"] = title  # 回写去重后的标题
        
        print(f"    ✍️ 正在撰写: {title[:30]}...")
        
        # v3.6 白标:先从 brand_id 解析代理品牌名(真白标=非平台默认才注入)·
        # 统一在所有 prompt 分支选定后替换平台名(P1 修:原只 style_code 分支接,
        # query_type/article_type/异常兜底 legacy 分支仍会带 OmniRank/全域上榜)。
        _wl_brand = None
        try:
            from services.public_whitelabel import resolve_branding_context
            _wl_ctx = resolve_branding_context(surface="customer", brand_id=self.brand_id)
            if _wl_ctx.get("source") != "platform_default":
                _wl_brand = (_wl_ctx.get("brand") or {}).get("company_name")
        except Exception:
            _wl_brand = None

        # All paths resolve into the six-family production contract. Legacy
        # article_type/query_type remain input aliases, never independent prompts.
        _legacy_to_style = {
            "comparison": "comparison_review", "ranking": "comparison_review",
            "premium_ranking": "comparison_review", "authority_ranking": "comparison_review",
            "qa": "qa_recommendation", "faq": "qa_recommendation",
            "risk": "risk_compliance", "trend": "risk_compliance",
            "case": "data_report", "report": "data_report",
            "guide": "buying_guide", "howto": "buying_guide",
        }
        resolved_style = style_code or _legacy_to_style.get(query_type or "") or _legacy_to_style.get(article_type or "") or "buying_guide"
        try:
            from .style_registry import get_prompt_for_style, WRITING_STYLES, normalize_style_for_generation
            resolved_style = normalize_style_for_generation(resolved_style) or "buying_guide"
            topic["style_code"] = resolved_style
            style_code = resolved_style
            system_prompt = get_prompt_for_style(resolved_style, brand=_wl_brand)
            style_name = WRITING_STYLES.get(resolved_style, {}).get("name", resolved_style)
            print(f"    📝 使用文体合同: {style_name} (six-family SSOT)")
        except Exception as e:
            print(f"    ⚠️ 文体合同加载失败: {e}，使用证据型实施指南")
            from .templates.canonical_family_templates import prompt_for_style
            resolved_style = "buying_guide"
            topic["style_code"] = resolved_style
            style_code = resolved_style
            system_prompt = prompt_for_style(resolved_style)

        # v3.6 白标:统一对最终 system_prompt 做平台名→代理品牌替换(覆盖全部分支 · 真白标才换)
        if _wl_brand:
            try:
                from .style_registry import _apply_brand_to_prompt
                system_prompt = _apply_brand_to_prompt(system_prompt, _wl_brand)
            except Exception:
                pass

        # Every legacy/query-type path receives the same non-bypassable
        # evidence contract. Admin prompt overrides are constrained too.
        from .evidence_first_policy import compose_evidence_first_prompt
        system_prompt = compose_evidence_first_prompt(
            system_prompt,
            style_code or article_type,
        )

        # [WP12 P0-2/P0-3] 旧路径也要拿到同一份客户存在感三铁律与榜单复活合同,
        # 否则同一套规则在两条生成链上会分叉。
        #
        # [写作质量总工单 2026-07-29 · A-2 实测修复] 旧代码是
        #   build_client_presence_prompt(getattr(self, "brand_name", "") or topic.get("brand_name") or "")
        # 而 `ArticleWriter.__init__` **从来没有** `self.brand_name`,四个现役调用点
        # (tools/article_generator.py:206/1016/1376、writing/batch_processor.py:94)
        # 构造的 topic 里也**都没有** `brand_name` 键 → 恒为 ""  → 合同回落成字面串
        # 「客户品牌」,模型看到的是占位符不是品牌名。真实品牌名一直躺在
        # distilled_data.client_profile 里(`_build_user_message` 就是这么取的),
        # 现在统一走 client_presence_policy.resolve_client_brand 这一份口径。
        # ⚠️ 注入点必须排在 client_profile 解析 / 竞品集合 / Brand Fact Snapshot
        # 之后 —— 见下方 `_build_user_message` 调用之后的 `_inject_client_presence_contract`。
        from .article_style_contract import RANKING_REVIVAL_CONTRACT, family_for_style  # noqa: F401

        # ✅ V2新增：加载客户知识库（异步）
        self.knowledge_context = await self._load_client_knowledge(title)

        # ✅ V12新增：竞品列表加载（优先级：UI手动调研 > distilled_data > 自动联网搜索）
        # 优先级1：检查数据库中是否有UI手动调研的竞品列表
        db_competitors, db_mode = self._load_db_competitors(topic.get('quote_id'))
        topic['_competitor_source'] = db_mode
        if db_competitors and db_mode == 'real':
            topic['_researched_competitors'] = db_competitors
            topic['_competitor_research_candidates'] = db_competitors
            print(f"    ✅ [竞品] 使用已核验名称集合 ({len(db_competitors)}家)")
        elif db_competitors and db_mode == 'semi':
            # 待核验名称只进入 Evidence Pack 搜证，不直接进入正文候选集合。
            topic['_researched_competitors'] = []
            topic['_competitor_research_candidates'] = db_competitors
            print(f"    ℹ️ [竞品] {len(db_competitors)} 家待核验候选仅用于搜证")
        else:
            existing_competitors = topic.get('competitors_to_mention', [])
            if not existing_competitors:
                competitor_analysis = self.distilled_data.get('competitor_analysis', {})
                if isinstance(competitor_analysis, str):
                    import json
                    try:
                        competitor_analysis = json.loads(competitor_analysis)
                    except Exception:
                        competitor_analysis = {}
                if isinstance(competitor_analysis, dict):
                    existing_competitors = competitor_analysis.get('competitors', [])
            topic['_researched_competitors'] = []
            topic['_competitor_research_candidates'] = existing_competitors or []

        client_profile = self.distilled_data.get('client_profile', {})
        if isinstance(client_profile, str):
            try:
                client_profile = json.loads(client_profile)
            except Exception:
                client_profile = {}
        _candidate_names = topic.get('_competitor_research_candidates') or topic.get('_researched_competitors') or []
        _candidate_names = [str(item.get('name') if isinstance(item, dict) else item).split('—', 1)[0].strip() for item in _candidate_names]
        if not topic.get('generation_request_id'):
            from uuid import uuid4
            topic['generation_request_id'] = str(uuid4())
        from writing.evidence_research import collect_evidence_pack
        from tools.llm_call_tracker import llm_tracking_context
        with llm_tracking_context(
            caller="article_evidence_research",
            brand_id=getattr(self, "brand_id", None),
            quote_id=topic.get("quote_id"),
            metadata={
                "generation_request_id": topic.get("generation_request_id"),
                "topic_id": topic.get("id"),
            },
        ):
            topic['_evidence_pack'] = await collect_evidence_pack(
                title=title,
                keyword=_primary_keyword or title,
                industry=str(client_profile.get('industry') or ''),
                client_brand=str(client_profile.get('brand_name') or client_profile.get('company_name') or ''),
                competitor_names=_candidate_names,
                request_id=str(topic.get('generation_request_id') or ''),
            )

        # 构建用户输入
        user_message = self._build_user_message(topic)
        # _build_user_message freezes Brand Fact Snapshot.  Add the local
        # claim-to-source contract only after both source collections exist.
        from .evidence_precision_policy import render_evidence_precision_prompt

        system_prompt = (
            system_prompt
            + "\n\n"
            + render_evidence_precision_prompt(
                topic.get("_evidence_pack") or {},
                topic.get("brand_fact_snapshot") or {},
            )
        )
        # [WP12 P0-2/P0-3 + 总工单 A-2] 客户存在感三铁律 / 榜单复活合同。
        # Brand Fact Snapshot 与竞品集合此刻才齐,所以注入点在这里而不是更靠上。
        system_prompt = self._inject_client_presence_contract(
            system_prompt, topic, style_code or article_type,
        )

        # ── [#185 c3 · T6] 防御型(公司词)正文:这家品牌蒸出来的写法 ──────
        # 🔴 **单点注入**:正文侧只有这一处读 playbook。多一处就会出现
        #    「标题按这一版角度写、正文按另一处拼的规则写」,而两边各自都绿。
        # 🔴 只读不蒸(见 `render_defensive_body_prompt`),非防御篇返空串。
        from .defensive_playbook import render_defensive_body_prompt
        system_prompt += render_defensive_body_prompt(
            self.brand_id, topic.get("user_choice"),
        )

        # 调用LLM（无评审循环，直接生成）
        content = await self._call_llm(system_prompt, user_message)
        from writing.source_disclosure_style import polish_source_disclosure

        content = polish_source_disclosure(content)
        # Legacy/direct ArticleWriter has no contact opt-in surface.  Its safe
        # default is therefore an explicit article-level opt-out.
        from services.contact_placeholder import enforce_contact_opt_out

        content = enforce_contact_opt_out(content, self.brand_id)

        # Direct ArticleWriter callers do not all pass through
        # ArticleGeneratorService._save_article, so fail closed here as well.
        from .evidence_first_policy import EvidenceFirstViolation, evaluate_content_trust
        trust = evaluate_content_trust(
            title,
            content,
            evidence_mode=str(topic.get("evidence_mode") or "unknown"),
        )
        from .evidence_precision_policy import evaluate_evidence_precision

        precision = evaluate_evidence_precision(
            content,
            topic.get("_evidence_pack") or {},
            topic.get("brand_fact_snapshot") or {},
            title=str(title or ""),
        )
        quality_warning = {}
        if trust.hard:
            # [WP9-P0-7 ① · D8 文章层零阻断]legacy 直写路径同样不再拒存:违法绝对化降为
            # 定位标注 + needs_legal_fix 草稿态(保存永不失败·不二次全费);广告法只在对外
            # 发布边界(article_review_gate legal_hard)拦并给一键修复。
            quality_warning["evidence_legal"] = trust.warning_payload()
            quality_warning["needs_legal_fix"] = True
        if trust.soft:
            quality_warning["evidence"] = trust.warning_payload()
        if precision.hard or precision.warnings:
            quality_warning["evidence_precision"] = precision.payload()
        
        result = {
            "id": topic.get("id", 0),
            "type": article_type,
            "style_code": style_code,  # 新增：记录使用的风格
            "title": title,
            "platform": topic.get("platform", ""),
            "keywords": topic.get("keywords", []),
            "content": content,
            "word_count": count_effective_chars(content)
        }
        if quality_warning:
            result["quality_warning"] = quality_warning
        
        # 新增：附加Token使用信息
        if self.last_token_usage:
            result["token_usage"] = self.last_token_usage
        
        return result

    
    def _resolve_client_brand(self, topic: Dict[str, Any]) -> str:
        """本入口的客户品牌名解析 —— 与 `_build_user_message` 同一个数据源。

        [写作质量总工单 2026-07-29 · A-2] 旧代码读 `self.brand_name`(该属性
        从未被赋值)与 `topic['brand_name']`(四个现役调用点都不写这个键),
        于是客户存在感合同恒定拿到空串,回落成字面占位「客户品牌」。
        """
        import json

        from .client_presence_policy import resolve_client_brand

        client_profile = self.distilled_data.get("client_profile", {})
        if isinstance(client_profile, str):
            try:
                client_profile = json.loads(client_profile)
            except Exception:
                client_profile = {}
        return resolve_client_brand(
            getattr(self, "brand_name", ""),
            topic.get("brand_name"),
            client_profile if isinstance(client_profile, dict) else {},
            topic.get("company"),
        )

    def _inject_client_presence_contract(
        self, system_prompt: str, topic: Dict[str, Any], style_or_type: str,
    ) -> str:
        """两条入口同源的客户存在感 / 榜单复活合同注入。"""
        from .article_style_contract import RANKING_REVIVAL_CONTRACT, family_for_style
        from .client_presence_policy import build_client_presence_prompt

        client_brand = self._resolve_client_brand(topic)
        if client_brand:
            topic["brand_name"] = client_brand
        out = system_prompt + "\n\n" + build_client_presence_prompt(
            client_brand,
            [
                str(item.get("name") if isinstance(item, dict) else item).strip()
                for item in (topic.get("_researched_competitors") or [])
            ],
            brand_facts=topic.get("brand_fact_snapshot") or {},
        )
        if family_for_style(style_or_type) == "multi_brand_comparison":
            out = out + "\n\n" + RANKING_REVIVAL_CONTRACT
        return out

    def _load_db_competitors(self, quote_id) -> tuple:
        """从数据库加载竞品列表和模式。返回 (competitors_list, mode)"""
        if not quote_id:
            return [], 'evidence_only'
        try:
            import json
            from db.connection import get_connection as _get_conn
            conn = _get_conn()
            c = conn.cursor()
            try:
                c.execute("SELECT competitor_list, competitor_mode FROM quotes WHERE id = %s", (quote_id,))
            except Exception:
                c.execute("SELECT competitor_list FROM quotes WHERE id = %s", (quote_id,))
            row = c.fetchone()
            conn.close()
            if row:
                comp_list = json.loads(row["competitor_list"]) if row.get("competitor_list") else []
                try:
                    mode = row['competitor_mode'] or 'evidence_only'
                except (IndexError, KeyError):
                    mode = 'real' if comp_list else 'evidence_only'
                if mode not in {'real', 'semi'}:
                    mode = 'evidence_only'
                active = [
                    item for item in comp_list
                    if not (isinstance(item, dict) and item.get('excluded'))
                ]
                if mode == 'real' and (
                    not active
                    or not all(
                        isinstance(item, dict)
                        and (item.get('name_verified') is True or item.get('human_verified_name') is True)
                        for item in active
                    )
                ):
                    mode = 'semi' if active else 'evidence_only'
                # 只传名称给搜证计划；简介/画像仍是待核验材料。
                str_list = [
                    item.get('name', '') if isinstance(item, dict) else str(item)
                    for item in active
                    if str(item.get('name', '') if isinstance(item, dict) else item).strip()
                ]
                return str_list, mode
        except Exception as e:
            print(f"    ⚠️ [竞品] 加载数据库竞品失败: {e}")
        return [], 'evidence_only'

    async def _research_competitors(self, *_args, **_kwargs) -> list:
        """Retired compatibility hook; candidate discovery now belongs to Evidence Pack."""
        return []

    def _format_competitor_list(self, competitors: list) -> str:
        """格式化已核验的候选名称；能力描述仍须 Evidence ID。"""
        lines = []
        for c in competitors:
            if isinstance(c, dict):
                name = c.get('name', '')
                projects = c.get('projects', [])
                desc = c.get('desc', '')
                if projects:
                    lines.append(f"- {name}（已核验项目名称：{'、'.join(projects[:3])}）")
                elif desc:
                    lines.append(f"- {name} — {desc}")
                else:
                    lines.append(f"- {name}")
            else:
                lines.append(f"- {c}")
        return chr(10).join(lines)

    def _get_competitor_instruction_block(self, industry: str) -> str:
        """Return one non-bypassable candidate/evidence rule."""
        return (
            "竞品只能来自已确认候选集合；没有候选时只写选型标准。"
            "名称存在不等于能力已验证，所有事实仍须 Evidence ID。"
        )

    def _get_competitor_rule(self, industry: str) -> str:
        """竞品规则单行版（详细规则在instruction_block中，这里只做提醒）"""
        return "4. 所有候选使用同一证据门槛；不得自行补品牌或虚构能力。"

    def _get_word_count_instruction(self, topic: Dict[str, Any]) -> str:
        """Return the versioned, evidence-aware length instruction."""
        style_code = topic.get('style_code') or topic.get('style') or 'buying_guide'
        plan = build_length_plan_for_topic(style_code, topic)
        topic['_length_plan'] = plan
        return render_length_instruction(plan)
    
    def _build_user_message(self, topic: Dict[str, Any]) -> str:
        """构建用户输入 - V10.4版本：通用模板（无专用内容）"""
        from datetime import datetime
        
        # 获取当前日期
        current_date = datetime.now().strftime("%Y年%m月%d日")
        current_year = datetime.now().year
        current_month = datetime.now().month
        
        # ============================================
        # 从distilled_data中动态提取客户信息
        # ============================================
        client_profile = self.distilled_data.get('client_profile', {})
        if isinstance(client_profile, str):
            import json
            try:
                client_profile = json.loads(client_profile)
            except:
                client_profile = {}
        
        # 动态提取核心字段（无默认值硬编码）
        # 🚨 优先使用brand_name，这是诊断数据中实际存储品牌名的字段
        client_company = client_profile.get('brand_name',  # 最优先：诊断数据中的brand_name
                                           client_profile.get('company_name', 
                                           client_profile.get('client_name', 
                                           topic.get('company', ''))))
        industry = client_profile.get('industry', '')
        
        # 调试输出
        print(f"    🔍 [DEBUG] client_company: {client_company}")
        print(f"    🔍 [DEBUG] client_profile keys: {list(client_profile.keys()) if isinstance(client_profile, dict) else 'N/A'}")
        
        # 动态提取竞品列表
        competitor_analysis = self.distilled_data.get('competitor_analysis', {})
        if isinstance(competitor_analysis, str):
            import json
            try:
                competitor_analysis = json.loads(competitor_analysis)
            except:
                competitor_analysis = {}
        
        # 只有明确处于 real 状态的名称集合可呈现给正文 LLM。
        # semi/legacy 名称只作为 Evidence Pack 的搜索候选，不能靠提示词猜测为事实。
        competitor_source_mode = topic.get('_competitor_source', '')
        competitors = (
            list(topic.get('_researched_competitors') or [])
            if competitor_source_mode == 'real'
            else []
        )

        # 动态提取卖点
        selling_points = self.distilled_data.get('selling_points', '')
        
        # ============================================
        # V4新增：提取社媒数据用于文章写作
        # ============================================
        social_media_section = self._format_social_media_data()
        
        # ============================================
        # v3.7 CTO-15.0：读取用户已审核的 industry_brief（GEO 知识库融合）
        # 只注入 5 个对 GEO 文章有价值的字段 + 避免 competitor 重复/社媒口径噪音
        # ============================================
        brief_block = ""
        if self.brand_id:
            try:
                from db.profile_db import get_effective_brief_by_brand
                brief = get_effective_brief_by_brand(self.brand_id)
                if brief:
                    # GEO 五件套：差异化定位 / 核心案例 / 内容痛点 / 地域关键词 / 差异化素材
                    # 不传 my_audience/content_strategy/top_content_formats/local_platform_tips（社媒口径噪音）
                    # local_competitors 不传（prompt 已有独立 competitors 块，避免冲突）
                    parts = []
                    diff = brief.get("my_differentiation")
                    cases = brief.get("top_cases")
                    pains = brief.get("content_pain_points")
                    kws = brief.get("regional_kw_ideas")
                    hints = brief.get("differentiation_hints")
                    if diff:
                        parts.append(f"## 差异化定位（客户已审核）\n{diff}")
                    if hints:
                        if isinstance(hints, list):
                            hints = "\n- " + "\n- ".join(str(h) for h in hints)
                        parts.append(f"## 差异化素材\n{hints}")
                    if cases:
                        if isinstance(cases, list):
                            cases = "\n- " + "\n- ".join(str(c) for c in cases[:5])
                        parts.append(f"## 核心案例（写作可引用 · 必须与下方官方知识库数据一致）\n{cases}")
                    if pains:
                        if isinstance(pains, list):
                            pains = "\n- " + "\n- ".join(str(p) for p in pains)
                        parts.append(f"## 客户痛点（内容角度素材）\n{pains}")
                    if kws:
                        if isinstance(kws, list):
                            kws = ", ".join(str(k) for k in kws[:15])
                        parts.append(f"## 地域关键词（可自然嵌入）\n{kws}")
                    if parts:
                        brief_block = "\n\n".join(parts)
                        print(f"    📖 [品牌知识库] 已注入 {len(parts)} 组用户审核字段")
            except Exception as e:
                print(f"    ⚠️ [品牌知识库] 读取失败（降级不影响写作）: {e}")

        # ============================================
        # [2026-06-02 老板要求] 5 维结构化业务画像注入（structured_knowledge）
        # 代理在「客户资料中心 · 业务事实」填的画像此前完全没连到 AI 写作。这是代理确认的第一手
        # 结构化资料,比诊断蒸馏的 client_profile 更权威 → 标注「第一手·优先采用」,与上方差异化定位 /
        # 下方蒸馏数据融合,重叠以本段为准,避免 LLM 重复罗列。空/异常优雅降级不影响写作。
        # ============================================
        if self.brand_id:
            try:
                from db.profile_db import get_structured_knowledge_by_brand
                sk = get_structured_knowledge_by_brand(self.brand_id)
                sk_parts = []
                if sk:
                    def _sk_join(val):
                        return "、".join(str(x) for x in val if x) if isinstance(val, list) else str(val)
                    prod = sk.get("products") or {}
                    if prod.get("name") or prod.get("features"):
                        seg = []
                        if prod.get("name"): seg.append(f"名称：{prod['name']}")
                        if prod.get("features"): seg.append(f"核心特性：{_sk_join(prod['features'])}")
                        if prod.get("scenarios"): seg.append(f"使用场景：{_sk_join(prod['scenarios'])}")
                        if prod.get("metrics"): seg.append(f"效果数据：{prod['metrics']}")
                        sk_parts.append("**产品/服务**：" + " · ".join(seg))
                    pain = sk.get("painPoints") or {}
                    if pain.get("scenario") or pain.get("consequences"):
                        seg = []
                        if pain.get("scenario"): seg.append(f"典型场景：{pain['scenario']}")
                        if pain.get("consequences"): seg.append(f"不解决的后果：{pain['consequences']}")
                        sk_parts.append("**客户痛点**：" + " · ".join(seg))
                    cust = sk.get("customers") or {}
                    if cust.get("segments") or cust.get("needs"):
                        seg = []
                        if cust.get("segments"): seg.append(f"客户群体：{_sk_join(cust['segments'])}")
                        if cust.get("needs"): seg.append(f"核心需求：{_sk_join(cust['needs'])}")
                        if cust.get("concerns"): seg.append(f"最关心：{_sk_join(cust['concerns'])}")
                        sk_parts.append("**目标客户**：" + " · ".join(seg))
                    diff = sk.get("differentiation") or {}
                    if diff.get("usp") or diff.get("advantages") or diff.get("killerData"):
                        seg = []
                        if diff.get("usp"): seg.append(f"独特卖点 USP：{diff['usp']}")
                        if diff.get("advantages"): seg.append(f"核心优势：{_sk_join(diff['advantages'])}")
                        if diff.get("killerData"): seg.append(f"关键数据：{_sk_join(diff['killerData'])}")
                        sk_parts.append("**差异化优势**：" + " · ".join(seg))
                    cases = sk.get("cases") or []
                    if isinstance(cases, list) and cases:
                        case_lines = []
                        for c in cases[:3]:
                            if not isinstance(c, dict):
                                continue
                            line = str(c.get("client") or "案例")
                            tail = []
                            if c.get("background"): tail.append(str(c["background"]))
                            if c.get("solution"): tail.append(str(c["solution"]))
                            if c.get("results"): tail.append(f"成果：{c['results']}")
                            if tail: line += "：" + "→".join(tail)
                            if c.get("quote"): line += f"「{c['quote']}」"
                            case_lines.append(line)
                        if case_lines:
                            sk_parts.append("**真实案例**：\n- " + "\n- ".join(case_lines))
                    # [2026-06-02 Deploy-CTO 修隐患·老板授权] 兼容存量 brand 的 legacy 业务事实格式
                    # 新 5 维 reader 只认 products/painPoints/customers/differentiation/cases;存量 brand 的
                    # structured_knowledge 多为老格式(confirmed_notes 数组 / audience-offer-proof 4 字段),
                    # 此前整段被跳过 → 代理确认的第一手知识没喂写作。这里补齐,让存量 brand 也吃到。
                    # 纯追加·与 5 维并存·空则跳·不动上方 5 维逻辑·graceful 在外层 try 内。
                    _legacy_map = [("audience", "目标受众"), ("offer", "核心服务/卖点"),
                                   ("proof", "信任背书/实力证明"), ("constraints", "表达红线/禁忌")]
                    _legacy_seg = []
                    for _lk, _label in _legacy_map:
                        _lv = sk.get(_lk)
                        if isinstance(_lv, str) and _lv.strip():
                            _legacy_seg.append(f"{_label}：{_lv.strip()}")
                    if _legacy_seg:
                        sk_parts.append("**业务要点（客户确认）**：" + " · ".join(_legacy_seg))
                    _cn = sk.get("confirmed_notes")
                    _cn_list = ([str(x).strip() for x in _cn if x and str(x).strip()]
                                if isinstance(_cn, list)
                                else ([str(_cn).strip()] if isinstance(_cn, str) and _cn.strip() else []))
                    # [治理 §12.3] 客户确认要点属商业交付内容,不再静默截断到前 10 条
                    # (旧 _cn_list[:10] 会把客户确认的第 11 条起悄悄丢出写作 prompt)。
                    _cn_seg = _format_confirmed_notes(_cn_list)
                    if _cn_seg:
                        sk_parts.append(_cn_seg)
                if sk_parts:
                    sk_header = ("## 公司确认的业务画像（🔴 仅供内部校核 · 不是可引用来源 · "
                                 "不得在正文里以“公司材料/企业提供”等形式声明它，"
                                 "要写进正文须另挂具体外部来源方 + 日期）")
                    sk_block = sk_header + "\n" + "\n".join(sk_parts)
                    brief_block = (brief_block + "\n\n" + sk_block) if brief_block else sk_block
                    print(f"    📖 [品牌知识库] 已注入结构化业务画像（{len(sk_parts)} 段·含 5 维+legacy 兼容）")
            except Exception as e:
                print(f"    ⚠️ [结构化画像] 读取失败（降级不影响写作）: {e}")

        # ============================================
        # V11新增：读取 DDS 蒸馏洞察（反哺写作）
        # ============================================
        dds_patterns_text = ""
        if self.brand_id:
            try:
                from pathlib import Path
                dds_file = Path(__file__).parent.parent / "data" / "knowledge" / "clients" / str(self.brand_id) / "dds_patterns.md"
                if dds_file.exists():
                    dds_content = dds_file.read_text(encoding="utf-8")
                    # 截取前2000字符，避免上下文过长
                    dds_patterns_text = dds_content[:2000]
                    print(f"    📊 [DDS] 已加载蒸馏洞察 ({len(dds_content)}字符)")
            except Exception as e:
                print(f"    ⚠️ [DDS] 加载失败: {e}")
        
        from writing.brand_fact_snapshot import build_brand_fact_snapshot
        from writing.evidence_pack import render_evidence_pack_for_writer
        from writing.source_disclosure_style import SOURCE_DISCLOSURE_PROMPT
        # [写作质量总工单 2026-07-29 · ⑨ 两入口不对等 · 判定结论]
        # 本入口(ArticleWriter)**没有** `_save_article` 的两步占位符链路
        # (`_insert_default_image_need_placeholder` → `select_images_for_article`
        # → 发布期渲染),它的产物直接落文件/直接回给调用方。因此给它注入
        # [NEED_IMAGE] 规则只会留下一串被 `server.py:14293` 强行 strip 的死占位。
        # 结论:**这是能力缺口而不是有意设计,但正确的补齐方向是"显式关闭"而不是
        # "抄一份选图链"** —— 否则等于第二次造轮子(本轮反复出现的失守形态)。
        # 同时补掉一个真实风险:此前本入口对图片**只字不提**,模型完全可以吐出
        # Markdown 图片语法配上编造的 URL,而 strip 只认 [NEED_IMAGE]/[CLIENT_IMAGE]。
        from services.article_image_selector import IMAGE_OPT_OUT_PROMPT

        if not topic.get("brand_fact_snapshot"):
            topic["brand_fact_snapshot"] = build_brand_fact_snapshot(
                brand_id=self.brand_id,
                brand_name=str(client_company or ""),
                industry=str(industry or ""),
                client_materials={
                    "_material_source": "distilled_current_config",
                    "company_intro": client_profile,
                    "core_selling_points": selling_points,
                    "credentials": brief_block or None,
                },
            )
        # 🔴 [复审返工 2026-08-10 · P1-3/D5] 必须把客户品牌名传下去 ——
        # publisher 里带客户自己名字的条目要判成「自有渠道」,不给归属句。
        # 上一版这里一个参数没传,那道防线恒空(复审实锤:"形同虚设")。
        # 品牌名走仓内既有口径 `_resolve_client_brand`,不另造第二份
        # (`ArticleWriter` 没有 `self.brand_name`,是 2026-07-29 记过的坑)。
        _self_names = tuple(n for n in (self._resolve_client_brand(topic),) if n)
        evidence_text = render_evidence_pack_for_writer(
            topic.get("_evidence_pack") or {}, self_names=_self_names,
        )
        candidate_text = self._format_competitor_list(competitors) if competitors else "无已核验名称；只能写选型标准，不得自行补品牌"

        return f"""# 写作请求

- 当前校验日期：{current_date}（只用于检查证据是否过期，不强制写进标题或正文）
- 标题：{topic.get('title', '')}
- 目标平台：{topic.get('platform', '')}
- 核心关键词：{', '.join(topic.get('keywords', []))}
- 内容方向：{topic.get('content_focus', '')}
- 角度说明：{topic.get('angle_instruction', '')}

{evidence_text}

# 品牌事实卡
- 公司：{client_company}
- 行业：{industry}
- 当前卖点材料：{selling_points}
- 当前资料摘要：{brief_block or '未提供'}
- 客户知识库摘要：{self.knowledge_context[:CLIENT_KNOWLEDGE_PROMPT_MAX_CHARS] if self.knowledge_context else '未提供'}

# 已核验候选名称集合
{candidate_text}

名称核验只证明主体可识别，不证明产品能力。所有能力事实仍需 Evidence ID；资料不足就少写。

{SOURCE_DISCLOSURE_PROMPT}

# 输出要求

1. 标题保持为“{topic.get('title', '')}”；排名/推荐形态原样保留并在正文披露依据。仅法律禁止的绝对化宣称需修正。
2. 开头直接回答读者问题，再给依据、场景、限制与核验步骤。
3. 客户和竞品使用同一证据字段与门槛；不固定首位。
4. 可以采用第三方采编视角。🔴 企业资料是**主张底账**，但**不作为可写进正文的来源声明**：不写“企业档案/项目资料/资质资料/报价或合同”这类类型词，也不写“待核验”这类内部状态——它们写在正文里等于没标来源，且是软文指纹。要标就挂具体外部来源方 + 日期；挂不上就改写成不需要外部归属的表达——**但不因此删掉该事实**。不得虚构独立核验。
5. 没有证据支撑的数字、资质、效果、案例、价格和比较结论不进入正文。🔴 但**不得把 Evidence ID（EV-1 之类编号）写进正文**——那是内部索引，正文只写来源方名称 + 日期。
6. {self._get_word_count_instruction(topic)}；信息不足时允许更短，禁止用重复或虚构内容凑字数。
7. 文末列来源方名称、日期、适用范围和重大限制。🔴 **不列 Evidence ID**（内部索引不进正文）。
8. 本入口未取得插入联系方式授权：不得输出电话、微信、客户官网、地址、联系卡片或导流话术。
9. {IMAGE_OPT_OUT_PROMPT}
"""
    
    def _get_scoring_section(self, topic: Dict[str, Any]) -> str:
        """Return an evidence matrix contract; never manufacture ratings."""
        business_type = topic.get("business_type", "B2B服务商")
        return f"""# 证据核验口径
业务类型识别为：{business_type}
请用“排序/推荐依据｜可核验证据｜来源类型｜适用场景｜适用边界”同口径表表达。
标题/关键词要求排名时允许有序呈现；禁止付费排位、无依据名次、平台自创 5 分制、
百分制、综合评分、星级、S/A/B 等级或权重。"""
    def _format_social_media_data(self) -> str:
        """V4新增：格式化社媒数据供文章写作使用"""
        social_data = self.distilled_data.get('social_media_data', {})
        if not social_data.get('has_social_data'):
            return ""
        
        sections = []
        
        # 抖音热门视频
        douyin_videos = social_data.get('douyin_top_videos', [])
        if douyin_videos:
            sections.append("# 抖音热门内容（真实数据，可引用）")
            for i, v in enumerate(douyin_videos[:3], 1):
                sections.append(f"{i}. 「{v.get('title', '')}」by {v.get('author', '')} - {v.get('likes', 0)}赞")
        
        # 小红书热门笔记
        xhs_notes = social_data.get('xiaohongshu_top_notes', [])
        if xhs_notes:
            sections.append("\n# 小红书热门内容（真实数据，可引用）")
            for i, n in enumerate(xhs_notes[:3], 1):
                sections.append(f"{i}. 「{n.get('title', '')}」by {n.get('author', '')} - {n.get('likes', 0)}赞")
        
        # ASR转写亮点
        asr_highlights = social_data.get('asr_highlights', [])
        if asr_highlights:
            sections.append("\n# 竞品热门视频文案（ASR转写，可参考）")
            for asr in asr_highlights[:2]:
                sections.append(f"- 「{asr.get('title', '')}」{asr.get('likes', 0)}赞")
                sections.append(f"  文案摘录：{asr.get('text_preview', '')[:100]}...")
        
        return "\n".join(sections) if sections else ""
    
    async def _call_llm(self, system_prompt: str, user_message: str) -> str:
        """调用LLM生成文章 - 统一使用 get_llm_config()"""
        from .llm_utils import get_llm_config, get_thinking_disabled_params

        # ========== 统一配置源 ==========
        api_url, api_key, model, provider = get_llm_config("geo_article", "writing")

        if not api_key:
            from writing.article_generation_failure import ArticleProviderUnavailable

            raise ArticleProviderUnavailable()

        print(f"    🤖 使用模型: {provider}/{model}")

        try:
            # 从 settings 读取超时配置
            try:
                from config.settings_manager import get_current_settings
                _timeout = float(get_current_settings().article_timeout)
            except Exception:
                _timeout = 180.0
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                "temperature": 0.7,
                "max_tokens": 16000  # 为六文体 3K-14K 自适应合同留足输出容量
            }
            body.update(get_thinking_disabled_params(api_url, model))
            async with httpx.AsyncClient(timeout=_timeout) as client:
                response = await client.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=body
                )
                response.raise_for_status()
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                
                # 记录Token使用
                usage = result.get("usage", {})
                self.last_token_usage = {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "model_name": f"{provider}/{model}"
                }
                
                # 后处理
                content = self._clean_article_content(content)
                
                return content
        except Exception as e:
            from writing.article_generation_failure import ArticleProviderUnavailable

            raise ArticleProviderUnavailable() from e
    
    def _clean_article_content(self, content: str) -> str:
        """清理文章内容，移除 AI 废话、字数标注等模板痕迹"""
        from writing.content_cleaner import clean_llm_article
        from writing.article_generator_service import _sanitize_customer_facing_article_sources

        return _sanitize_customer_facing_article_sources(clean_llm_article(content))


# 测试
if __name__ == "__main__":
    import asyncio
    import json
    from dotenv import load_dotenv
    load_dotenv()
    
    # 模拟数据
    test_distilled = {
        "client_profile": json.dumps({
            "company_name": "驰鲸科技",
            "industry": "TikTok代运营"
        }),
        "selling_points": json.dumps({
            "absolute_pain_point": "B2B工厂不懂TikTok内容运营",
            "unique_value": "专注工厂出海TikTok获客"
        }),
        "competitor_analysis": json.dumps({
            "competitors": [{"name": "卧兔网络"}, {"name": "PandaMobo"}]
        })
    }
    
    test_topic = {
        "id": 1,
        "type": "ranking",
        "title": "2026年深圳TikTok代运营公司TOP5推荐",
        "platform": "百家号",
        "keywords": ["TikTok代运营", "深圳", "TOP5"],
        "client_position": 1,
        "competitors_to_mention": ["卧兔网络", "PandaMobo"],
        "content_focus": "强调B2B工厂出海专业能力"
    }
    
    async def test():
        writer = ArticleWriter(test_distilled)
        article = await writer.write(test_topic)
        print(f"\n=== 文章生成完成 ===")
        print(f"标题: {article['title']}")
        print(f"字数: {article['word_count']}")
        print(f"\n{article['content'][:500]}...")
    
    asyncio.run(test())
