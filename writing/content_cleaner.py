"""
LLM 输出内容清洗器
清除 AI 模型在文章正文前后添加的角色扮演、解释性话语等非正文内容
"""
import re


# AI 开头废话的典型标记词（出现在第一段内即判定为前缀废话）
_PREAMBLE_MARKERS = [
    '好的，',
    '好的！',
    '好的,',
    '当然，',
    '当然可以，',
    '当然！',
    '没问题，',
    '没问题！',
    '我将为您',
    '我来为您',
    '我将遵循',
    '我会按照',
    '我将按照',
    '我会为您',
    '根据您的要求',
    '根据你的要求',
    '按照您的指令',
    '以下是为您',
    '以下是我为您',
    '以下是根据',
    '下面是为您',
    '下面我将',
    '收到，',
    '明白了，',
    '了解，',
    'Sure,',
    'Of course,',
    'Certainly,',
    'As an',
    'I will',
    'I\'ll',
    'Here is',
    'Here\'s',
]

# AI 结尾废话的典型标记（出现在最后一段即判定为后缀废话）
_EPILOGUE_MARKERS = [
    '以上就是',
    '以上是我',
    '以上是为您',
    '以上内容',
    '希望这篇文章',
    '希望以上内容',
    '如果您需要',
    '如果你需要',
    '如需进一步',
    '如需修改',
    '如需调整',
    '如有任何',
    '需要我修改',
    '需要我调整',
    '是否需要',
    '请告诉我',
    '请让我知道',
    '如果有任何',
    'Let me know',
    'Hope this helps',
    'Feel free to',
]


def clean_llm_article(content: str) -> str:
    """
    清洗 LLM 生成的文章内容，去除 AI 输出的非正文内容。

    处理:
    1. 开头的角色扮演/确认话语（如"好的，作为一名分析师，我将..."）
    2. 结尾的总结/询问话语（如"以上就是...希望对您有帮助"）
    3. 字数标注（如"（200字）"）
    4. 多余空行
    """
    if not content or not content.strip():
        return content

    content = content.strip()

    # ========== 0. 分离 YAML front matter ==========
    # 保存在磁盘的文章可能包含 YAML 头，需要跳过以免干扰清洗
    yaml_header = ''
    if content.startswith('---'):
        end_idx = content.find('---', 3)
        if end_idx != -1:
            yaml_header = content[:end_idx + 3]
            content = content[end_idx + 3:].strip()

    # ========== 1. 清除开头的 AI 废话 ==========
    content = _strip_preamble(content)

    # ========== 2. 清除结尾的 AI 废话 ==========
    content = _strip_epilogue(content)

    # ========== 3. 清除字数标注 ==========
    content = re.sub(r'（约?\d+[-~]?\d*字(?:左右)?）', '', content)
    content = re.sub(r'\(约?\d+[-~]?\d*字(?:左右)?\)', '', content)
    # Models sometimes append a confident self-reported count. Persistence
    # always recomputes the SSOT effective-char count, so remove these claims.
    content = re.sub(
        r'(?im)^\s*(?:全文|本文)?\s*(?:共计?|约有?|字数(?:统计)?)\s*[:：为]?\s*'
        r'\d[\d,，]*\s*(?:字|字符)(?:左右)?\s*[。.!！]?\s*$',
        '',
        content,
    )

    # ========== 4. 统一对客可见的资料依据与评分口径 ==========
    content = _blend_visible_source_labels(content)
    content = _normalize_visible_rating_labels(content)

    # ========== 5. 清除多余空行 ==========
    content = re.sub(r'\n{3,}', '\n\n', content)

    result = content.strip()
    if yaml_header:
        return yaml_header + '\n\n' + result
    return result


def _strip_preamble(content: str) -> str:
    """去除开头的 AI 角色扮演/确认废话

    增强策略：从内容开头逐行扫描，识别 AI 废话行并去除。
    同时保留标题行（# heading 或 **bold title**）和分割线（---），
    这样无论废话出现在标题前还是标题后都能处理。
    """
    lines = content.split('\n')
    lines_to_remove = set()
    scanned_past_title = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if not stripped:
            continue  # 跳过空行，继续扫描

        # 标题行（# heading）、加粗标题（**Title**）、分割线（---）→ 保留，继续往下扫描
        if (re.match(r'^#{1,3}\s+', stripped)
                or re.match(r'^\*\*[^*]+\*\*$', stripped)
                or stripped == '---'):
            scanned_past_title = True
            continue

        # 检查是否为 AI 废话行
        if _has_preamble_markers(stripped) and len(stripped) < 200:
            lines_to_remove.add(i)
        else:
            # 遇到正文内容，停止扫描
            break

    if lines_to_remove:
        content = '\n'.join(line for i, line in enumerate(lines) if i not in lines_to_remove)

    return content.strip()


def _strip_epilogue(content: str) -> str:
    """去除结尾的 AI 总结/询问废话"""
    lines = content.rstrip().split('\n')

    # 从最后一行往前扫描
    lines_to_strip = 0
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            lines_to_strip += 1
            continue
        if stripped == '---':
            # 分割线可能是废话的开始标记
            lines_to_strip += 1
            continue
        if _has_epilogue_markers(stripped) and len(stripped) < 200:
            lines_to_strip += 1
        else:
            break

    if lines_to_strip > 0:
        content = '\n'.join(lines[:len(lines) - lines_to_strip])

    return content.strip()


def _has_preamble_markers(text: str) -> bool:
    """检查文本是否包含 AI 开头废话标记"""
    return any(marker in text for marker in _PREAMBLE_MARKERS)


def _has_epilogue_markers(text: str) -> bool:
    """检查文本是否包含 AI 结尾废话标记"""
    return any(marker in text for marker in _EPILOGUE_MARKERS)


#: 🔴 [自曝清零 2026-08-10] 我方单方来源 / 泛化类型词的正文形态。
#: 命中一律**删除**,不再标准化成「(资料来源:企业资料)」这类归属。
#:
#: 上一版(2026-08-08,我自己写的)把模型吐的粗糙来源统一**改写成**我们的
#: 规范归属,判断依据是「去掉审计状态、保留来源主体区分」。生产实测把这个
#: 判断证伪了:「企业提交资料」自曝出现在 68.3% 的文章里,而它**不在清洗器
#: 黑名单**,一路活到发布;同期外部信源存在率从 89.0% 掉到 61.0%。
#: 对 AI 引擎来说「据企业提供的资料」= 这家自己说的,是比「未经独立核验」
#: 更常见的软文指纹。
_SELF_DISCLOSURE_PATTERNS: "tuple[re.Pattern[str], ...]" = (
    # 括注形态:（来源：客户提供…）（数据来源：内部资料）（来源：公开信息整理）
    re.compile(
        r'（\s*(?:来源|数据来源|据|基于)\s*[:：]?\s*'
        r'(?:客户|企业|公司|品牌)(?:方|方面)?[^）]{0,45}'
        r'(?:提供|授权|自报|披露|资料|数据|文件|案例|政策|流程|记录|定价|售后)'
        r'[^）]*）'
    ),
    re.compile(
        r'（\s*(?:来源|数据来源|据|基于)\s*[:：]?\s*'
        r'(?:公开信息整理|公开资料整理|公开资料与行业调研|公开信息[^）]{0,20})\s*）'
    ),
    re.compile(
        r'（\s*(?:来源|数据来源)\s*[:：]?\s*'
        r'(?:客户知识库|诊断数据|内部资料|资料库)[^）]*）'
    ),
    # 行文形态:据企业案例资料…/据企业提供资料…
    re.compile(
        r'据(?:企业|公司|客户|品牌)(?:方)?'
        r'(?:案例资料|客户回访记录|售后说明与客户回访记录|报价说明|售后说明|'
        r'资质与获奖资料|提供资料|提供的[^\s，。；、]{0,10}资料|服务说明)'
    ),
    # 裸标签形态:来源：客户提供资料 / 公司定价文件 / 公开资料整理
    re.compile(
        r'(?:来源[:：]\s*)?'
        r'(?:公司客户回访数据|公司内部客户回访数据|公司定价文件|公司报价文件|'
        r'客户提供资料|企业提供资料|公司内部数据)'
    ),
    re.compile(
        r'(?:基于可见公开信息整理，建议进一步核验|基于公开信息整理|'
        r'来源[:：]\s*公开资料整理|来源[:：]\s*公开信息整理)'
    ),
    # 我方单方声明形态:企业提交资料（截至 X 年 X 月）/ 由客户内部资料提供
    re.compile(
        r'(?:以下)?企业(?:相关信息|相关数据|信息)?[^\n。]{0,20}?'
        r'来自企业(?:提交|提供)(?:的)?(?:业务)?资料[^\n。]{0,30}?。'
    ),
    # 🔴 连带吃掉后面那个悬空动词。烟测实证:只删短语会留下「显示，质保期为 24 个月」
    # 这种没有主语的残句 —— 比留着自曝更难读,也更像机器产物。
    re.compile(
        r'企业提交(?:的业务)?资料(?:（[^）]{0,30}）)?'
        r'\s*(?:显示|记载|表明|说明|指出|载明)?\s*[，,、]?'
    ),
    re.compile(r'由(?:客户|企业)(?:内部|方)?(?:资料|材料)提供'),
    # 🔴 [生产真实语料实证 2026-08-10] 整段「数据来源限制」免责声明。
    # 220 条真实自曝行过链后残留 6 条,**全部是这一类** —— 它是最完整的软文指纹:
    #   「本文所有核心数据…均来自企业提供材料，未经独立第三方审计。」
    #   「对于无法独立核验的数据，已明确标注为“[客户材料显示]”。」
    # 一句话把整篇文章的可信度自己否掉,AI 引擎读到它就不用再判断了。
    re.compile(
        r'[^。！？\n]{0,40}?未经[^。！？\n]{0,12}?第三方[^。！？\n]{0,12}?'
        r'(?:审计|核验|验证|认证)[^。！？\n]{0,30}?[。！？]'
    ),
    re.compile(
        r'[^。！？\n]{0,40}?未经[^。！？\n]{0,8}?独立[^。！？\n]{0,12}?'
        r'(?:审计|核验|验证)[^。！？\n]{0,30}?[。！？]'
    ),
    # [R4 §1c → R5 §1 2026-08-11/12] 商业关系自爆改由
    # `_strip_article_commercial_relation()` 处理(判别位是「谁与谁」的关系,
    # 不是关键词 —— 旧三条纯关键词正则把「客户的商业合作客户」「受政府委托推广」
    # 这类**第三方业务事实**整句删光,Review 实测两句成空串 = 新删除机器)。
    # 标注成「[客户材料显示]」这类可见记号
    # 连带吃掉包裹的引号 —— 只删记号会留下「已明确标注为""」这种空引号。
    re.compile(
        "[“”\"‘’'「」]?\\s*"
        "[（(\\[［]?\\s*客户材料显示\\s*[）)\\]］]?\\s*"
        "[“”\"‘’'「」]?"
    ),
    # 小标题:数据来源限制 / 资料来源说明 / 免责声明
    re.compile(r'^#{1,6}\s*(?:数据来源限制|资料来源限制|来源与核验说明|免责声明)\s*$', re.M),
    re.compile(r'\*\*\s*(?:数据来源限制|资料来源限制)\s*[:：]?\s*\*\*'),
)

#: 🔴 [R4 §3 2026-08-11 Owner 裁「清」] 免责元叙述(生产实测「仅供参考」597 篇
#: =41.8% /「本文不构成」149 /「未经独立核验」40)。「作者自己都不为内容背书」
#: 是 AI 降权信号。分位处置(Owner「能不删就不删」):
#:   · 元叙述位(本文/本报告 + 仅供参考/不构成建议/不代表观点):零事实,整句删;
#:   · 业务位(「以上价格仅供参考,实际以门店测量为准」):只摘免责小句,
#:     业务限定(以门店测量为准)原样保留,**不许整句删**。
#: prompt 层核查结论:活链无「写免责」指令;`ranking_prompt_v9.py`(KEEP-only,
#: 本包禁碰)第 730/771 行有「仅供参考」输出要求,由本清洗层兜底,随交付单移交。
# ---------------------------------------------------------------------------
# 🔴 [R5 §1 → R6 §1 2026-08-12] 商业关系清洗:判别位是「谁与谁」的关系。
#   R5 只建了一半(Review 实测判红):
#   · 「委托撰写」**动词本身**被当自曝(写作动词捷径)→「观山电梯受南山区政府
#     委托撰写白皮书」被整句删 —— R4 P0 同型复发;
#   · 自指词只认 本文/本篇/本稿 →「本报告由观山电梯委托推广」原样漏出。
#   R6 真建成:
#   · 自曝 ⟺ 关系一方是**自指词族**(本文/本篇/本报告/本内容/本评测/本稿/
#     本报道/这篇文章/笔者/作者/我们)—— 判别只看这一位,**与动词无关**;
#   · 双方都是第三方实体 → **一律保留**(撰写/推广/赞助/委托/合作/采购…同待遇);
#   · 自曝 + 硬事实 → 摘关系从句留事实;事实全在关系从句的极端形态按自曝清除;
#   · 建议语气护栏:「建议与本地服务商合作」是给读者的建议,不是关系声明,不动。
#   关系候选 = 强声明短语 + 陈述式关系形态(由/受/与/为/向 + 实体 + 关系动词);
#   裸动词(合作/采购/联合)不单独作候选 —— 那是误伤面,不是判别位。
_REL_STRONG = (
    r'委托(?:撰写|创作|发布|发文|推广)|付费客户关系|商业合作客户|付费合作关系|'
    r'广告合作客户|付费推广客户|付费赞助|客户关系(?:为|是|属于)'
)
_REL_DECLARATIVE = (
    r'(?:由|受|与|为|向)[^，,。；;！？\n]{1,20}?(?:委托|赞助|合作|联合|授权经销|采购)|'
    r'(?:是|为|系)[^，,。；;！？\n]{0,16}?(?:赞助方|合作方|合作伙伴|授权经销商|采购方)'
)
_COMMERCIAL_RELATION_RE = re.compile(_REL_STRONG + '|' + _REL_DECLARATIVE)
#: 自指词族(R6:成族,不是只认「本文」—— 本报告/本评测漏出即此病)。
_ARTICLE_SELF_RE = re.compile(
    r'本文|本篇|本报告|本内容|本评测|本稿|本报道|这篇文章|笔者|作者|我们'
)
# 🔴 [R7 §1 2026-08-12] 判别依据换代:R4 漏混合句 → R5 漏同义动词 → R6 漏
#   「自指词出现在第三方句子内部」,三轮同型 —— 病根一直是「整句里有没有某个
#   词」。R7 起自曝判别落在**句法角色**上,不再是词的出现:
#   1. 说话者归属:`据/援引 X 介绍/表示/称/指出` 之后是**被引述方**的话,
#      其中的「我们」指受访者不是本文;引号内同理;
#   2. 自指词的句法角色:「作者/笔者」带前缀修饰(第一/原/共同/通讯/联合)
#      或后接人名(作者张三与…)时是**称谓成分**,指第三方;
#   3. 关系双方:两端都是第三方实体 → 一律保留,与句中出现什么词无关。
#: 引述标记:命中后到句末的内容按被引述方的话处理。
_QUOTE_ATTR_RE = re.compile(
    r'(?:据|援引)[^，,。；;：:！？\n]{1,24}?(?:介绍|表示|称|指出|透露|回应)[：:，,]'
)
#: 成对引号区间:引号内的自指词属于说话者,不是本文。
_QUOTED_SPAN_RE = re.compile(r'[“「][^”」]*[”」]')


def _effective_self_spans(sentence: str) -> list[tuple[int, int]]:
    """R7 §1:返回句内**真正指本文自身**的自指词 span。

    引语内(据 X 介绍：… / 引号内)的「我们」→ 受访者,不算;
    「作者/笔者」作称谓修饰(第一作者 / 作者张三与…)→ 第三方,不算。
    """
    excluded: list[tuple[int, int]] = []
    attr = _QUOTE_ATTR_RE.search(sentence)
    if attr:
        excluded.append((attr.end(), len(sentence)))
    for qm in _QUOTED_SPAN_RE.finditer(sentence):
        excluded.append(qm.span())
    spans: list[tuple[int, int]] = []
    for sm in _ARTICLE_SELF_RE.finditer(sentence):
        s, e = sm.span()
        if any(a <= s < b for a, b in excluded):
            continue
        if sm.group() in ('作者', '笔者'):
            # 前缀修饰:第一作者/原作者/共同作者… → 称谓,指第三方
            if re.search(r'(?:第[一二三四五六七八九十]|原|共同|通讯|联合)$',
                         sentence[:s]):
                continue
            # 后接人名再接连接词(作者张三与…)→ 称谓+人名,指第三方。
            # (?![与和…]) 防止「作者与张三」里的连词被当人名首字吞掉。
            if re.match(r'(?![与和同为向在是])[一-鿿]{2,3}[与和同曾、，,]',
                        sentence[e:]):
                continue
        spans.append((s, e))
    return spans


#: 建议语气护栏:建议/推荐类是写给读者的动作建议,不是关系披露。
#: 🔴 R7 §2:护栏从「整句放行」降为**只保护建议那一小句**(R6 的整句放行成了
#: 自曝绕过开关 ——「本文由 X 委托推广，建议读者先核对合同。」原样漏出)。
#: 顺序上自指/自曝判定先于语气豁免。
_ADVICE_MOOD_RE = re.compile(r'建议|推荐|不妨|考虑|可以先?|应当|如何')
#: 被摘自曝从句中的委托方实体(由/受 X 委托…)→ 残句「其与…」的先行词补回用。
_REL_ENTITY_RE = re.compile(
    r'(?:由|受)([^，,。；;：:！？\n]{2,12}?)(?:委托|赞助|付费|资助)'
)
#: 硬事实标记:数字/中文数量词+单位/合同、项目、验收类锚点。
_HARD_FACT_RE = re.compile(
    r'\d|[一二三四五六七八九十百千两]+\s*(?:台|个|套|部|年|月|日|万|亿|元|米|天|小时|次|家)|'
    r'验收|合同|项目|资质|专利|招标|中标'
)


# 🔴 [R8 2026-08-12 · Review 裁定 R7 撤回] 失败方向反转:**宁漏勿毁**。
#   R4→R7 四轮枚举(词表→动词→句法角色→分句符)全收敛不了,因为「未能识别
#   结构」时的失败方向是「删」—— 清单之外的每一种形态都是毁事实的入口。
#   R8 起**删除是需要证明的动作,保留是默认**:
#   · 删(整句或从句)必须有**锚定证明**:有效自指 span 位于句/从句首部
#     (仅隔前导空白/引号)且紧跟关系连接词(由/受/与/为/系/是)——
#     「本文由…」「我们与…」锚上;「我们智能科技有限公司与…」(自指词是
#     实体名成分)、「作者张三在本文中援引…」(自指词在句中)锚不上,保留;
#   · **含硬事实的跨度永不删除**,摘不干净就整跨度保留(漏出自曝可由
#     prompt 侧恢复 —— 六处删除授权已清零;毁掉的客户事实不可恢复);
#   · 什么都没摘到 → 整句原样保留(而不是把残句再过一道丢弃闸);
#   · 分句符补破折号/顿号/省略号/括号/空白(附带,不是修法本身 ——
#     清单外的分隔形态掉进上面三条,方向是保留)。
_SEG_SPLIT_RE = re.compile(r'[^，,；;：:、\s—…()（）]+[，,；;：:、\s—…()（）]*')
_LEAD_TRIVIA_RE = re.compile(r'[\s“「(（　]*')
_REL_CONNECTIVE_RE = re.compile(r'由|受|与|为|向|系|是')


def _self_anchored(sentence: str, self_spans: list[tuple[int, int]],
                   start: int, end: int) -> bool:
    """跨度 [start,end) 内是否存在**锚定的**自指关系:自指词就在跨度首部
    (仅隔空白/引号)且后面紧跟关系连接词 —— 高确信「自指词=关系一方」。"""
    lead = start + _LEAD_TRIVIA_RE.match(sentence[start:end]).end()
    for s, e in self_spans:
        if s == lead and e <= end and _REL_CONNECTIVE_RE.match(sentence, e):
            return True
    return False


# 🔴 [R9 2026-08-13 · Review 裁定 R8 撤回段 §2] 失败方向的**另一半**:
#   「摘除」在 R8 已需要证明(锚定+零硬事实),但摘除的**粒度**仍由分隔符
#   清单决定 —— 清单外分隔符时 seg 就是整句,「摘一个从句」和「删掉整句」
#   共用了同一份证明门槛,而这两个动作的风险不对等。
#   R9:整句级摘除(seg 覆盖整句 = 没识别到任何内部边界)要一道**更强的
#   证明** —— 自曝跨度之外不再有实质内容才可删,否则整句保留(漏出)。
#   实现用 **span 并集**(有效自指 + 强声明短语 + 声明式关系分别 finditer
#   后取并集),而不是逐个 sub 挖除:工单坑 1(非贪婪的『由观山电梯委托』
#   留尾巴『推广』)在并集里被独立命中的『委托推广』补上;坑 2(坐标错位)
#   不存在 —— 全程只用整句坐标,从不切子串。
#: 倒装声明式:「X 是〈自指〉的…客户/合作方」—— 系词后**紧跟自指词**才算
#: (关系一方=本文),所以「其品牌是行业合作伙伴」「万汇广场是观山电梯的
#: 商业合作客户」这类客户/第三方陈述不命中。关系主语(左侧毗连的实体名)
#: 属于自曝短语本身,由 _selfblow_covers_sentence 并入覆盖。
_REL_DECL_INVERTED_RE = re.compile(
    r'(?:是|为|系|属于)(?:' + _ARTICLE_SELF_RE.pattern + r')'
    r'的?[^，,。；;！？\n]{0,10}?(?:客户|合作方|赞助方|合作伙伴|采购方|授权经销商)'
)


def _selfblow_covers_sentence(sentence: str,
                              self_spans: list[tuple[int, int]]) -> bool:
    """自曝跨度并集之外是否**没有**实质内容(True = 纯自曝,可整句删)。

    覆盖 = 有效自指 + 强声明短语 + 声明式关系 + 倒装声明式(连同其左侧
    毗连的关系主语实体)的 span 并集,再桥接并集内相邻跨度之间 ≤3 个
    纯文字、零硬事实的语法胶水(「涉及的」「的」)。前缀/后缀一律不桥 ——
    「其服务覆盖华南地区」这类独立陈述永远算实质,保留方向不受影响。
    """
    spans = list(self_spans)
    for pat in (_REL_STRONG, _REL_DECLARATIVE):
        for m in re.finditer(pat, sentence):
            spans.append(m.span())
    for m in _REL_DECL_INVERTED_RE.finditer(sentence):
        j = m.start()
        while j > 0 and re.match(r'[\w＿]', sentence[j - 1]):
            j -= 1                      # 关系主语实体并入(倒装式左侧毗连)
        spans.append((j, m.end()))
    if not spans:
        return False
    # 合并 + 桥接短胶水间隙(只桥跨度**之间**,不碰前缀/后缀)
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        gap = sentence[merged[-1][1]:s]
        gap_text = re.sub(r'[\W_＿]+', '', gap)
        # 桥接三条件:≤3 个纯文字 · 间隙内**无任何非文字字符**(分隔样
        # 字符 = 边界证据,「/其品牌」不是胶水)· 零硬事实
        bridgeable = (len(gap_text) <= 3
                      and gap == gap_text
                      and not _HARD_FACT_RE.search(gap))
        if s <= merged[-1][1] or bridgeable:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    covered = [False] * len(sentence)
    for s, e in merged:
        for i in range(s, min(e, len(sentence))):
            covered[i] = True
    rest = ''.join(ch for i, ch in enumerate(sentence) if not covered[i])
    return not re.sub(r'[\W_＿]+', '', rest)


def _strip_article_commercial_relation(content: str) -> str:
    """R7 §1-§3 + R8 宁漏勿毁:按句法角色处理商业关系句(规则见上方注释)。

    · 句级:有效自指为空 → 第三方关系,原样保留;
    · 整句删(纯自曝)与从句摘除都要求**锚定证明**;
    · 含硬事实的从句永不摘;什么都没摘到 → 整句原样保留;
    · 建议小句保护;孤儿指代「其」回填被摘从句的委托方实体;
      残句句首悬空的文章动作动词(报道/介绍…)剥离。
    """
    if not content:
        return content
    out_lines: list[str] = []
    for line in content.splitlines():
        if not _COMMERCIAL_RELATION_RE.search(line):
            out_lines.append(line)
            continue
        kept: list[str] = []
        for sentence in re.findall(r'[^。！？\n]*[。！？]?', line):
            if not sentence:
                continue
            if not _COMMERCIAL_RELATION_RE.search(sentence):
                kept.append(sentence)
                continue
            # R7:判别位=「关系一方在句法上是不是本文自身」。
            self_spans = _effective_self_spans(sentence)
            if not self_spans:
                kept.append(sentence)      # 第三方关系 = 业务事实,原样保留
                continue
            # R8:没有整句删捷径 —— 一律走从句摘除;残句空了句子自然消失
            # (纯自曝=单从句锚定零事实,摘完即空),有内容就保留。
            segs = list(_SEG_SPLIT_RE.finditer(sentence))
            if len(segs) == 1:
                # R9:seg 覆盖整句 = 没识别到内部边界(清单外分隔符即此形态)
                # → 「摘除」退化成「删整句」,必须过更强的证明:自曝跨度之外
                # 不再有实质内容才可删;否则整句保留(漏出可恢复,毁不可恢复)
                if not _selfblow_covers_sentence(sentence, self_spans):
                    kept.append(sentence)
                    continue
            # 从句级摘除:每个从句独立要求锚定 + 零硬事实
            removed_entities: list[str] = []
            kept_segs: list[str] = []
            removed_any = False
            for seg in segs:
                text, s, e = seg.group(), seg.start(), seg.end()
                seg_anchored = _self_anchored(sentence, self_spans, s, e)
                if not seg_anchored and _ADVICE_MOOD_RE.search(text):
                    kept_segs.append(text)     # 建议小句,保
                    continue
                removable = seg_anchored or re.search(_REL_STRONG, text)
                if removable and not _HARD_FACT_RE.search(text):
                    ent = _REL_ENTITY_RE.search(text)
                    if ent:
                        removed_entities.append(ent.group(1))
                    removed_any = True
                    continue               # 锚定自曝从句(零硬事实)→ 摘
                kept_segs.append(text)     # 其余一律保留(宁漏勿毁)
            if not removed_any:
                kept.append(sentence)      # 无高确信自曝可摘 → 整句原样
                continue
            residual = re.sub(r'^[\s，,、；;：:—…)）]+', '', ''.join(kept_segs))
            if removed_entities:
                # 「报道其与南山区政府合作…」→ 其=被摘从句里的委托方
                residual = re.sub(r'其(?=[与和同为向在])',
                                  removed_entities[0], residual, count=1)
            # 主语(本文)被摘后句首悬空的文章动作动词剥离
            residual = re.sub(r'^(?:报道|介绍|呈现|梳理|记录)', '', residual)
            if re.sub(r'[\s。！？，,、；;：:…—*#\-]+', '', residual):
                # R8:摘除后剩余内容**非空即保留**(不再要求含事实/建议 ——
                # 「其服务覆盖华南」类非硬事实的客户陈述也不许毁)
                if not re.search(r'[。！？]$', residual):
                    residual = residual.rstrip('，,；;： ') + '。'
                kept.append(residual)
        out_lines.append(''.join(kept))
    return '\n'.join(out_lines)


#: 🔴 [R5 §2 → R6 §4 2026-08-12] 免责摘除改**结构判**(Owner:判据打结构不打
#: 词表 —— R5 词表只盖 仅供参考/仅作参考 两形态,不构成投资建议/不作为决策
#: 依据/请以实际为准/具体请咨询专业人士/结果仅供内部参考 全残留)。
#: 结构 = 免责**谓词语义族** + 该从句内**零硬事实**(事实闸,从句粒度):
#:   · 仅[供作]…参考(含"内部参考")
#:   · 不构成/不作为/不代表 + 建议/意见/依据/承诺/观点/立场 族
#:   · 空泛「以实际/最终…为准」(具名参照物的「以门店测量为准」「以项目排期
#:     为准」是业务限定资产,**不命中不删**)
#:   · 泛化「请咨询专业人士/专家」推诿(高风险领域的「请咨询专业医师」等
#:     具体持证建议不在族内,E-E-A-T 保留)
#: 从句删完只剩标点 → 整句本来就全是免责 → 句子消失;有事实从句 → 保留。
_DISCLAIMER_PRED_RE = re.compile(
    r'仅[供作][^，,；;。！？\n]{0,6}?参考|'
    r'不构成[^，,；;。！？\n]{0,12}?(?:建议|意见|依据|承诺|要约)|'
    r'不作为[^，,；;。！？\n]{0,10}?依据|'
    r'不代表[^，,；;。！？\n]{0,10}?(?:观点|立场)|'
    r'请?以(?:实际|最终)(?:情况|结果)?为准|'
    r'(?:具体)?请?咨询[^，,；;。！？\n]{0,8}?(?:专业人士|专家)'
)


def _strip_disclaimer_clauses(content: str) -> str:
    """R6 §4:按结构摘免责从句 —— 从句粒度事实闸,句末免责小句消失、
    事实与具名业务限定保留;整句只剩免责则句子消失。"""
    if not content:
        return content
    out_lines: list[str] = []
    for line in content.splitlines():
        if not _DISCLAIMER_PRED_RE.search(line):
            out_lines.append(line)
            continue
        kept: list[str] = []
        for sentence in re.findall(r'[^。！？\n]*[。！？]?', line):
            if not sentence:
                continue
            if not _DISCLAIMER_PRED_RE.search(sentence):
                kept.append(sentence)
                continue
            segs = re.findall(r'[^，,；;]+[，,；;]?', sentence)
            residual = ''.join(
                seg for seg in segs
                if not (_DISCLAIMER_PRED_RE.search(seg)
                        and not _HARD_FACT_RE.search(seg))
            )
            residual = re.sub(r'^[\s，,、；;：:]+', '', residual)
            if not re.sub(r'[\s。！？，,、；;：:…*#\-]+', '', residual):
                continue                   # 整句全是免责 → 消失
            if not re.search(r'[。！？]$', residual):
                residual = residual.rstrip('，,；; ') + '。'
            kept.append(residual)
        out_lines.append(''.join(kept))
    return '\n'.join(out_lines)

#: 🔴 表格「资料来源」列里的**裸类型词**。烟测实证:上面那批正文形态的正则
#: 一条都盖不到 `| 提升高度 | 24m | 企业资料 |` 这种单元格。
#: 类型词填在来源列里等于这一列没写(T2 反模板判据抓的就是它),
#: 故**清空该单元格** —— 让缺口可见,由 prompt 要求模型填具体载体名 + 日期。
_TABLE_TYPE_WORD_CELL = re.compile(
    r'(?<=\|)(\s*)(?:企业资料|企业档案|项目资料|资质文件|资质资料|报价与合同|'
    r'报价或合同|公开记录|公开资料|公开信息|客户提供|企业提供|内部资料)(\s*)(?=\|)'
)


#: 🔴 [返工 2026-08-10 · 复审必补 ①] 「…均来自 X 提供的 Y 资料。」这一句
#: **必须看主语再决定删不删**。
#:
#: 上一版是一条盲删正则,实测把第三方来源披露整句删空:
#:     「本文数据均来自深圳市住建局提供的公开资料。」        → ''
#:     「本文数据均来自深圳市市场监管局提供的公示材料。」    → ''
#:     「相关数据均来自中国建筑装饰协会提供的行业资料。」    → ''
#: 那是**反方向的谎言** —— 本包的目的是让文章多挂公开信源,结果把公开信源
#: 的披露句删了。而我的夹具里没有这种形态,所以 32 条锁和 32 条变异**全瞎**。
#:
#: 现在:候选句先由 `_SELF_SOURCE_SENTENCE` 找出来,再由
#: `_is_self_side_source()` 判主语 —— 我方单方才删,第三方一律留。
#: 主语判别**复用姊妹模块** `evidence_first_policy._SELF_SOURCE_MARKER_RE`,
#: 不在这里另写一份(两份手抄同串是本仓已犯过的病)。
_SELF_SOURCE_SENTENCE = re.compile(
    r'[^。！？\n]{0,60}?均?来自[^。！？\n]{0,24}?(?:提供|提交)的?'
    r'[^。！？\n]{0,14}?(?:资料|材料)[^。！？\n]{0,40}?[。！？]'
)
#: 我方单方**材料名词**。补 `_SELF_SOURCE_MARKER_RE` 盖不到的形态:
#: 「KZ木作定制提供的**企业官方资料**」—— 主语是品牌名,但材料名词是我方的。
#: ⚠️「公开资料 / 公示材料 / 行业资料 / 公司公告」**不在**表内 —— 那些是第三方口径。
_SELF_MATERIAL_NOUN = re.compile(
    r'(?:企业(?:官方)?|公司|客户方?|品牌|厂商|内部|自有)(?:的)?(?:业务)?(?:资料|材料|档案)'
)


def _is_self_side_source(span: str) -> bool:
    """这句「均来自…提供的…」说的是**我方单方材料**还是第三方来源?"""
    try:
        from writing.evidence_first_policy import _SELF_SOURCE_MARKER_RE
        if _SELF_SOURCE_MARKER_RE.search(span):
            return True
    except Exception:  # noqa: BLE001 - 取不到就退回本模块判据,绝不阻断
        pass
    return bool(_SELF_MATERIAL_NOUN.search(span))


def _strip_self_side_source_sentences(content: str) -> str:
    """只删我方单方来源那一句;第三方来源披露**原样保留**。"""
    def _sub(match: "re.Match[str]") -> str:
        span = match.group(0)
        return "" if _is_self_side_source(span) else span

    return _SELF_SOURCE_SENTENCE.sub(_sub, str(content or ""))


def _blank_table_type_word_cells(content: str) -> str:
    """把表格里只填了来源**类型词**的单元格清空。

    只处理**整格就是类型词**的情况(前后只允许空白),不碰
    「《中国电梯》2025-03」这类具体载体,也不碰正文散文。
    """
    out: list[str] = []
    for line in str(content or "").splitlines():
        if line.count("|") >= 2:
            prev = None
            while prev != line:          # 相邻两格都是类型词时要反复扫
                prev = line
                line = _TABLE_TYPE_WORD_CELL.sub(r"\1\2", line)
        out.append(line)
    return "\n".join(out)


#: 「据…企业提供的资料…显示，」这一整个**归属从句**(含引导词与谓语和逗号)。
#: 上一版只删中间的名词短语,把「据」和「显示，」留在原地 → 悬空动词。
#: 🔴 [最终接管返工 2026-08-10] 前后文字符类**必须把括号与冒号排除在外**:
#: 旧类只排句读,懒惰前缀会从「报价清晰（来源：公司内部数据）」的句首开始吞,
#: 把主张「报价清晰」连同归属一起删掉 —— 括注形态归 `_SELF_DISCLOSURE_PATTERNS`
#: 的括注黑名单管,本从句正则只管行文形态(「据企业提供资料，…」)。
_SELF_ATTRIB_CLAUSE = re.compile(
    r"(?:据|根据|依据|按照)?[^，。！？；\n（）()：:]{0,20}?"
    r"(?:企业|公司|客户方?|品牌|厂商|内部|自有)(?:的)?(?:官方)?(?:业务)?"
    r"(?:提供|提交|自述|内部)?的?(?:资料|材料|档案|数据|记录)"
    r"[^，。！？；\n（）()：:]{0,12}?(?:显示|表明|介绍|称|反映|记载|统计)?[，,、]?"
)


def _drop_self_attributed_number_claims(content: str) -> str:
    """摘掉我方单方**归属从句**;主张本身保留(P0-5 终态,函数名保留作血缘)。

    🔴 [P0-5 2026-08-10 · 最终接管工单 §9 同向] 本函数曾按复审 P1-2 实现
    「单方归属 + 硬数字 → 整条主张删掉」,后被 P0-5 撤销:证据不足不能成为
    删除整条客户事实的理由 —— 中小企业客户的交付量、质保、响应时长只可能
    来自自己的材料,删光 = 客户唯一能被 AI 复述成推荐理由的素材被删光。
    「去伪精度」由 prompt 侧降级阶梯承担(见 common_rules 第五节),
    清洗器只做确定性动作,不做语义改写。

    第三方归属(据《中国电梯》报道 / 据市场监管局公示)**不在本函数射程内**:
    正则要求出现我方单方材料名词,第三方句一个字都不碰
    (返工①的教训:我上一版的盲删正则把住建局、协会、统计局的披露一起删了)。
    """
    out_lines: list[str] = []
    for line in str(content or "").splitlines():
        if line.count("|") >= 2:          # 表格行不做句级删除
            out_lines.append(line)
            continue
        sentences = re.split(r"(?<=[。！？])", line)
        kept: list[str] = []
        for sentence in sentences:
            if not _SELF_ATTRIB_CLAUSE.search(sentence):
                kept.append(sentence)
                continue
            # 🔴🔴 [P0-5 撤销 2026-08-10] 这里原本是:带硬数字 → **整条主张删掉**。
            # 已撤销。理由(第一性原理:我们要的是客户被 AI 推荐):
            #   · 中小企业客户的具体事实**只可能来自自己的材料**,没有第三方会去
            #     报道一家小公司的交付量、质保年限、响应时长;
            #   · 整条删掉 = 把客户最能被复述成推荐理由的素材删光,
            #     文章变干净了,但 AI 更没有理由推荐这家。
            # 现行为:**只摘掉我方单方归属从句,主张本身保留**,由第三方叙述者转述。
            # 「去伪精度」由 prompt 侧三级事实合同承担(有凭证留精确值 /
            # 仅模型推断则改成不带伪精度的表达),清洗器不做语义改写。
            residual = _SELF_ATTRIB_CLAUSE.sub("", sentence)
            # 整句本来就只是一个归属从句 → 删完只剩「。」,那是残渣不是内容。
            # (实测抓到:「相关信息均来自公司提供的内部资料。」→ '。')
            if not re.sub(r"[\s。！？，,、；;：:…—－\-]+", "", residual):
                continue
            kept.append(residual)
        out_lines.append("".join(kept))
    return "\n".join(out_lines)


def _blend_visible_source_labels(content: str) -> str:
    """删除对客正文里的我方单方来源 / 泛化类型标注。

    [自曝清零 2026-08-10] 旧行为是把它们**标准化成**我们的规范归属;
    现在改为**删除**。删掉后该事实变成"无邻近来源",`evidence_first_policy`
    会亮 **soft** `unsourced_outcome_number` —— 那是触发公开信源增援的信号,
    不是拦车(文章层零阻断,不给客户设门槛)。

    🔴 删除动作经 `source_disclosure_style.strip_self_disclosure` 现取,
    这里不留任何字面量拷贝 —— 改 SSOT 的行为,这里必须跟着变。

    🔴 [顺序契约 2026-08-10 · 最终接管] **整句判别必须先于从句摘除**:
    「数据均来自KZ木作定制提供的企业官方资料，截至2026年3月。」是整句
    我方来源声明,该被 `_strip_self_side_source_sentences` 看主语后整句删;
    若让从句摘除先跑,它会把句中「企业官方资料」咬掉,整句判据从此
    认不出这句话,留下一句残缺的「数据均来自KZ木作定制提供的截至…」。
    """
    if not content:
        return content
    # ① 整句级:我方单方来源**声明句**(看主语,第三方一律保留)
    content = _strip_self_side_source_sentences(content)
    # ② 从句级:摘掉挂在事实主张上的我方归属从句,主张保留(P0-5)
    content = _drop_self_attributed_number_claims(content)

    from writing import source_disclosure_style as _sds

    for pattern in _SELF_DISCLOSURE_PATTERNS:
        content = _sds.strip_self_disclosure(content, pattern)

    # ③ [R6 §1] 商业关系句按「谁与谁」处理(第三方保留/纯自曝整删/自曝+事实摘从句)
    content = _strip_article_commercial_relation(content)
    # ④ [R6 §4] 免责从句按结构摘除(从句粒度事实闸,单趟替代旧"元叙述+业务位"双路)
    content = _strip_disclaimer_clauses(content)

    return _blank_table_type_word_cells(content)


def _normalize_visible_rating_labels(content: str) -> str:
    """Remove synthetic ratings instead of converting them to another score."""
    if not content:
        return content

    content = re.sub(r'五[维档]能力评级表', '五维证据核验表', content)
    content = re.sub(r'五[维档]能力评级', '五维证据核验', content)
    content = re.sub(r'(\|\s*)(?:评级|表现分|综合评分)(\s*\|)', r'\1证据与适用说明\2', content)

    rating_replacements = [
        ('五星半', '需按证据核验'),
        ('五星', '需按证据核验'),
        ('四星半', '需按证据核验'),
        ('四星', '需按证据核验'),
        ('三星半', '需按证据核验'),
        ('三星', '需按证据核验'),
    ]
    for label, value in rating_replacements:
        content = re.sub(fr'{label}(?!级|酒店|宾馆)', value, content)

    return content
