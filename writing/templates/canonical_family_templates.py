"""The only production-generating prompt set for the six outward families."""
from __future__ import annotations

from typing import Final, Sequence

from writing.article_style_contract import STYLE_FAMILIES, family_for_style
from writing.evidence_first_policy import RANKING_FORMS_DISCLOSURE_CLAUSE
from .common_rules import COMMON_GUARDRAILS


_SHARED = """
你是一名证据优先的行业内容编辑，可以采用第三方采编视角和媒体化叙事。除非 Evidence Pack 明确支持，
不得声称具体媒体或记者完成了采访、实地调查、独立审计或第三方认证。

工作顺序：
1. 先识别读者真正要做的决定，并在开头直接回答；
2. 事实来自 Evidence Pack、Brand Fact Snapshot 和检索到的真实公开信源。公开信源
   （学术论文、行业报告、权威媒体、政府/机构公开资料）是**可以直接引用的佐证**，
   按其原本表述引用即可，保留来源归属；
3. **引用以对读者自然的方式呈现**：用“据《XX》报道”“XX 大学的研究显示”“根据 XX
   发布的数据”这类正常行文，把来源写进句子。**正文中绝不出现 Evidence ID、BF-/EV-
   编号、证据状态表、核验清单，也不出现“待核验/需逐家核验/需交叉验证/暂无资料”
   这类内部审查语言**——这些是内部字段，读者看到只会觉得文章没写完；
4. 同时呈现支持、限制与适用边界；某条主张找不到可引用的公开资料时，**少写或换个
   说法**，不要在正文里留下检查痕迹；
5. 客户价值来自真实适配与证据，不来自固定首位、增加篇幅、假分数或贬低竞品；
6. 结尾可以自然交代资料截至时间与主要来源，写成读者能读的话，不要写成内部核验表。

""" + RANKING_FORMS_DISCLOSURE_CLAUSE + """
严禁：付费排位、无依据名次、平台自创星级/综合分、匿名权威、虚构品牌/案例/数字/引语、
虚构具体媒体身份或采访、无证据功效/合规/收益保证、把相关性写成因果。
**引用必须真实存在**：只能引用 Evidence Pack 或检索结果里真实出现过的来源，
严禁编造 URL、文献、机构名、研究结论或数据——这是不可越过的底线。
"""


# [P0-6 · Master SSOT v1.7 / 手册 §6.4] GEO 可抽取性层：让证据合格的内容更容易被
# AI 搜索引擎抽取和引用。与证据纪律冲突时，一律以证据纪律为准（本层只改"怎么写"，
# 不改"什么能写"；无证据数字/虚构竞品仍被上方严禁块拦截）。
_GEO_EXTRACTABILITY = """
面向 AI 检索的可抽取性要求（与证据纪律同等重要；冲突时以证据纪律为准）：
1. 答案句：每个主要小节第一句必须是 40-80 字、可脱离上下文独立引用的直接回答，句内包含
   该节结论本身，不用"下面我们来看"式过渡句开头；
2. 问句小标题：小标题优先采用目标读者的真实问法，全文至少一半小标题为问句式；
3. 相邻问句覆盖：除主问题外，自然覆盖 3-5 个相邻真实问题（优先取 Evidence Pack 或结构
   参考中给出的真实用户问句），各以可独立理解的问答块回应；
4. 实体密度：已核验的客户与竞品品牌使用可核验全称，关键段落中品牌全称、地域、品类自然
   共现；首次出现必用全称，同段承接可用"该公司"（所选生产文风另有指代要求时从其要求）；
5. 事实颗粒度：可核验数字必须带单位、口径与时间（如"评分4.6（2026-06，平台A）""180余套
   客房"），具体可查证的事实优于空泛形容词；无证据数字仍然禁止，宁缺毋滥；
6. 地域长尾：关键词或读者场景含地域时，正文覆盖本地资质、服务范围、本地案例或交付条件，
   地域词与品类词在标题和至少两个小节自然共现；
7. 不确定性处理：**正文不写"待核验/暂无资料/仅供参考"这类内部审查语言**。拿不准的
   主张就少写、换表述或不写；确有必要提示局限时，用读者能读的话自然说明（如"该数据
   为 2026 年 6 月口径"）。[C13 订正 2026-08-11] 配额口径：**免责套话/观望句**
   （仅供参考、请自行判断、各有优势式收尾）全文不超过 3 处；**证据等级限定词不计数**
   （"可能/推测/在本次样本内"是证据纪律要求保留的事实边界，删它凑配额 = 把薄证据
   写成满口断言，方向正反）。
"""


_FAMILY_INSTRUCTIONS: Final[dict[str, str]] = {
    "evidence_qa": """
围绕一个明确问题组织答案。结构为：直接答案 → 判断依据 → 步骤或适用条件 → 例外与边界 →
延伸提示。每个问答块可以脱离全文独立理解，问答块标题直接用问句本身。若官方定义与专业
来源不一致，说明版本和差异，不替读者做无依据的确定结论。
""",
    "multi_brand_comparison": """
先写入选范围和排除条件，再对所有真实候选使用同一字段、同一日期范围和同一证据门槛。
结构为：结论摘要 → 入选标准 → 同口径矩阵 → 各品牌资料卡 → 场景化条件建议 → 采购要点。
同口径矩阵用无序的场景矩阵组织：按“适用场景”并列展开各候选，不排名次、不做全局第一名。
【组织方式默认（被引语料 N=5,753：候选集并列 37.98% / 分场景推荐 11.47% / 唯一第一 48.65%）】
默认用**候选集并列**或**分场景推荐**二选一。
🔴 [C12 订正 2026-08-11 · R3 订正8 修死信] **自陈式"唯一第一"不写**——上一版"有外部
事实支撑才写唯一第一"的许可与本节"不做全局第一名"正面顶撞，且绝对化名次词是《广告法》
出现即拦项，照写必被硬拦白跑一轮（死信许可）。有真实、可独立核验的外部名次事实时，
按名次分两种**都真实可过闸**的形态落地（实测过真硬门，不是纸面许可）：
· 名次为第 2 及以后："据〈来源方〉〈日期〉发布的榜单/公示，X 位列第 N"——不含绝对化
  名次词，直接可写，来源、口径、时点当场写清；
· 名次为第一：必须用**成对引号的直接引用**＋言说标记——"据〈来源方〉〈日期〉报道，
  『X 位列第一』"（引号内是对方原话才豁免；**无引号的"…榜单，X 位列第一"仍会被硬拦**，
  不要写）。
拿不出这种外部事实就用条件化表述（"在 X 场景下更适合"）。语料里唯一第一占比高，
**那是别人怎么写，不是我们可以无依据照做的理由**。
客户品牌不适合进入名次时，**转为“场景推荐榜”**：按适用场景并列，客户出现在真正匹配的
场景位或收尾服务方位置，不硬塞进名次、也不为此贬低其他候选。
客户品牌仅在证据更强或场景更匹配时突出。候选证据不足就减少数量，
不得凑数。标题/关键词要求排名时可给有依据的排序参考，同时回答“在什么条件下更适合谁”；
依据不足的名次不写死名次、改写成条件化表述（“在 X 场景下更适合”），不把商业问题改写成百科问题。
不得自称权威研究院、独立评测机构或第三方分析师；只做同口径证据对比，不冒充权威背书。
同口径表的关键字段尽量为带单位与时间口径的可核验数值；表格前用一句话直接回答
“什么情况下选谁”。
""",
    "implementation_guide": """
把读者从目标带到可验收结果。结构为：目标 → 前置条件 → 分步实施 → 每步检查点 →
常见失败与风险 → 验收标准。经验必须说明主体、时间、环境和局限；没有一手经验时使用标准、
原厂或专业资料。价格和周期写明版本、范围与变量。
""",
    "trend_policy_risk": """
只保留“变化 → 证据 → 影响 → 风险 → 行动 → 不确定性”链路。说明变化发生时间、规则版本、
适用对象与证据来源；区分已经生效、征求意见、行业预测和作者推断。不得自称分析师、研究机构
或媒体。高风险结论给出升级到持证专业人士或主管机构的路径。
""",
    "case_data_roi": """
只有授权案例或可追溯公开案例才能写成真实案例。结构为：背景 → 行动 → 数据与口径 →
限制和反事实 → 情景测算 → 复核路径。没有真实结果时降级为“情景测算”，明确输入假设，
不得把测算写成客户已实现结果。ROI 不使用保证性语言。
""",
    "company_facts": """
把企业官方事实、客户自述、独立证据和系统推断分开。结构为：快速事实 → 产品/资质 →
适用场景 → 证据 → 适用与不适用 → 联系方式。可以采用第三方报道体，但不得虚构采访、调查或独立核验。
快速事实块用结构化条目呈现（名称/成立时间/资质/服务范围/核验路径各占一行），便于
AI 直接抽取实体事实。
brand_softarticle 历史入口生成的内容必须进入人工审核，未经授权不得写客户故事或评价。
""",
}


# [工单 A 2026-07-27 · §3] 深度评测结构规格。
#
# 数据背书(飞轮 2026-07-27,采纳组 306 / 对照组 4137 结构特征子集):
#   结尾给决策建议 49.0% vs 42.5% ✅  问句标题 28.8% vs 21.0% ✅
#   证据密度分 7.11 vs 6.43 ✅        段落数 80/段均 58 字 vs 99/83 字 ✅ 短段化
#   标题带年份 30.7% vs 42.1% ⚠️ 反向   FAQ 块 14.1% vs 22.2% 🔓 松绑
#   对比表 21.2% vs 30.8% 🔓 松绑      榜单列表 33.3% vs 32.2% 持平
# 相关性非因果、域名效应未剥离:综合判读是"媒体化行文胜过 SEO 结构块堆满"。
#
# ---------------------------------------------------------------------------
# [工单 C 2026-07-27 · §2] 深档结构 v2 —— 预算表驱动
#
# 工单 A 的 v1 规格在生产真跑里暴露了**规格算术不自洽**:
#   客户卡 600-900 + 竞品卡 400-600 × N + 薄外围,加总上限 ~9500,
#   只有 target 16000 的 60%。模型把规格写满就收尾 —— 实测 10037 字正是写满态,
#   不是 max_tokens 截断。
#
# 教训制度化:**规格给出的预算加总必须 ≥ target × 0.85**,并做成程序化判别锁
# (`assert_budget_covers_target`),砍掉任何一层预算都会转红。
# ---------------------------------------------------------------------------

#: 预算表的加总下限系数。低于这个值 = 规格自己就写不满 target,必须转红。
#
# [P3 2026-08-08] 改动前这里是一个**独立的 0.85 字面量**,与
# `article_length_contract.DEEP_OUTPUT_COVERAGE_FLOOR` 靠人工同步才相等
# (那边的注释已经自曝"同值但不同层")。同值不同源 = 早晚漂移,所以改成直接引用。
# **两把锁仍然都在**(一把量规格写不写得满 target,一把量模型真写了多少),
# 只是它们现在共用同一个数,不会各改各的。
def _coverage_floor() -> float:
    from writing.article_length_contract import DEEP_OUTPUT_COVERAGE_FLOOR

    return DEEP_OUTPUT_COVERAGE_FLOOR


BUDGET_COVERAGE_FLOOR: Final = _coverage_floor()
#: 实际编制目标(留 6% 给模型的自然溢出,不逼它硬凑到 100%)。
_BUDGET_FILL_RATIO: Final = 0.94

#: 参考基准(工单 §2 表格):target=16000 / 竞品 11 家 + 客户 1 家 = 12 张卡。
_REF_TARGET: Final = 16000

# [工单 C · §2.6-C] 峰 2 子集(12-24k 采纳 82 vs 曝光 868)新挖出的正信号,全部进规格:
#   价格/预算信息 76.8% vs 68.5% · 清单块 59.8%(长文标配) · 案例块 63.4% vs 60.6%
#   本地服务范围 63.4% vs 58.5% · 年份标题 25.6% vs 45.5%(比全体 -11.4pt 更狠,-19.9pt)
# 新增「价格与预算参考」500 + 「典型交付案例深写」800,卡片层相应回收 1300。
_REF_NON_CARD_BLOCKS: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("lede", "标题 + 开头总判断", 400,
     "问句标题、**禁以年份开头**（深档长文年份标题 25.6% vs 45.5%，负得比全体更狠）；"
     "开头 200 字内给出“谁适合谁”的总判断，不写行业导语"),
    ("digest", "要点速读", 500,
     "5-7 条自包含结论，每条 = 结论句 + 数据 + 来源，天然可独立提取"),
    ("criteria", "选型标准与入选口径", 900,
     "怎么选、本文入选标准、核验方式；**禁自创评分体系**（不得出现综合分/百分制/S-A-B 级）"),
    ("matrix", "同口径矩阵 + 解读", 800,
     "字段带单位与时间口径的可核验数值；表前一句话直答“什么情况选谁”；"
     "🔴 **矩阵每个候选一行、行必须齐**——字段取各家公开可核验项（生产模式/计价方式/"
     "主打产品线/质保档）；[C11 缺格 SSOT] **某个字段有候选拿不到 → 整列删除该字段**"
     "（与证据卡对等规则同源；不写“—”占位、不留空格），**严禁只留客户一行的烂尾表**"
     "（宁可字段少、不许行缺）"),
    ("pricing", "价格与预算参考", 500,
     "价格区间必须带**口径与时间**（如“2026-07 市场公开报价”）；客户价格取自知识库，"
     "竞品价格用公开报价并标注信源；**拿不到就写清拿不到，禁编造**"),
    ("cases", "典型交付案例深写", 800,
     "1-2 个深写案例各约 400 字（授权或公开），含背景 / 做法 / 结果口径 / 时间；"
     "与卡内案例不重复"),
    ("scenarios", "分场景建议 + 本地服务范围", 1000,
     "3-4 个真实场景各约 300 字，条件化表述（“预算 X 选 A / 场景 Y 选 B”）；"
     "地域词场景要做实：本地资质、服务范围、交付条件成段"),
    ("risks", "风险与避坑（清单块）", 700,
     "3 条中立陷阱：现象 / 识别信号 / 建议；**不指向任何一家**；用清单块呈现"),
    ("qa", "相邻问答块", 900,
     "3-5 个问句小节，小标题用真实用户问法；**不写“FAQ:”格式化大块**"
     "（深档长文 11.0% vs 26.4%，负信号）——要的是可独立回答的问答段，不是 FAQ 外观"),
    ("closing", "结尾决策建议 + 验收清单 + 资料口径", 500,
     "分预算/规模给行动建议（采纳组正信号 +6.5pt）；采购要点/验收清单模块化呈现；"
     "自然交代资料截至时间与主要来源"),
)

# [工单 C · §2.7-B · P1] 另外两个会进深档的家族,复用同一套预算引擎与同一把算术锁。
#   `case_data_roi` 早就有深档条件(verified≥10 ∧ publishers≥4)却**没有深档规格** ——
#   进了深档就会犯和榜单一模一样的"写不满"病;`implementation_guide` 由 §2.6-A
#   形态路由表要求开深档(N<2 但主题证据充足的攻略文)。两族都不含品牌卡层。
_REF_GUIDE_BLOCKS: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("lede", "标题 + 开头直答", 500,
     "问句标题、**禁以年份开头**；开头 200 字内直接回答“这件事怎么做/怎么选”"),
    ("digest", "要点速读", 600,
     "5-7 条自包含结论，每条 = 结论句 + 数据 + 来源"),
    ("criteria", "怎么选：判断标准与前置条件", 2000,
     "把读者的决策拆成可判断的条件；每条标准给出判断依据与核验方式"),
    ("pricing", "价格构成与预算区间", 1600,
     "拆解价格由哪些部分构成；区间带口径与时间；说明什么会让它变贵/变便宜；禁编造"),
    ("steps", "分步实施", 3300,
     "每一步：目标 → 具体做法 → 检查点 → 常见失败；步骤数由真实流程决定，不凑数"),
    ("acceptance", "验收清单（清单块）", 1500,
     "可逐条打勾的验收项；每项写清“怎么算通过”"),
    ("cases", "案例与情景测算", 1800,
     "1-2 个授权或公开案例深写（含口径与时间）；无一手材料时改为有边界的情景说明并标明是测算"),
    ("risks", "风险与避坑（清单块）", 1200,
     "3-5 条中立陷阱：现象 / 识别信号 / 建议；不指向任何一家"),
    ("qa", "相邻问答块", 1200,
     "3-5 个问句小节，真实用户问法作小标题；不写“FAQ:”格式化大块"),
    ("local", "本地服务范围与交付条件", 700,
     "地域词场景做实：本地资质、服务半径、上门/交付条件"),
    ("closing", "结尾决策建议 + 资料口径", 700,
     "分预算/规模给行动建议；自然交代资料截至时间与主要来源"),
)
_REF_CASE_BLOCKS: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("lede", "标题 + 开头结论", 500,
     "问句标题、**禁以年份开头**；开头 200 字内给出“这组数据说明什么”"),
    ("digest", "要点速读", 600,
     "5-7 条自包含结论，每条 = 结论句 + 数据 + 来源"),
    ("background", "背景与样本口径", 1800,
     "样本范围、时间窗、数据来源与采集方式；**口径先讲清楚再上数字**"),
    ("actions", "采取的行动", 2200,
     "做了什么、为什么这么做、当时的约束条件；行动与结果分开陈述"),
    ("data", "数据与解读", 3000,
     "每个数字带单位、口径、时间窗；**相关性不写成因果**；给出对照或基准"),
    ("limits", "数据的限制", 1200,
     "样本偏差、不可外推的部分、未控制的变量；这一段必须写实，不写等于结论不可信"),
    ("scenario", "情景测算", 2000,
     "明确标注是测算不是实测；写清假设条件与敏感因素；给出上下界"),
    ("pricing", "成本与 ROI 口径", 1500,
     "成本构成与回收周期的口径；价格带时间与来源；禁编造"),
    ("replicate", "复核路径（清单块）", 1400,
     "读者如何自行复核：查什么、问什么、怎么算；可逐条打勾"),
    ("closing", "结尾决策建议 + 资料口径", 900,
     "分预算/规模给行动建议；自然交代资料截至时间与主要来源"),
)

#: 家族 → (区块表, 是否含品牌卡层)。卡片层只有多品牌比较族才有。
_FAMILY_DEEP_BLOCKS: Final[dict[str, tuple[tuple[tuple[str, str, int, str], ...], bool]]] = {
    "multi_brand_comparison": (_REF_NON_CARD_BLOCKS, True),
    "implementation_guide": (_REF_GUIDE_BLOCKS, False),
    "case_data_roi": (_REF_CASE_BLOCKS, False),
}
#: 卡片层的权重:客户卡 / TOP2-4 / 其余。伸缩时先加厚 TOP2-4,再加厚其余。
_CARD_WEIGHT_CLIENT: Final = 1500
_CARD_WEIGHT_TOP: Final = 1000
_CARD_WEIGHT_REST: Final = 700
_TOP_TIER_CARDS: Final = 3          # TOP2-4
#: 单卡预算上限。候选很少时(N<8)卡片层会被摊得过厚,超出部分回流到
#: 与候选数无关的方法/场景/问答区块 —— 那些内容少几家候选照样写得实。
_CARD_BUDGET_CEILING: Final = 2200
_SPILL_TARGETS: Final[tuple[str, ...]] = ("criteria", "scenarios", "risks", "qa")


# ---------------------------------------------------------------------------
# [工单 C 复审返工 ② 2026-07-29 · §2.5 客户侧素材自适应]
#
# 生产实证:近 20 天 6 个活跃品牌中 **5 家**的 brand_fact_snapshot 只有
# 1556-2374 字符(真实事实文本约 1000-1600 字)。硬性要求"客户卡 1500 字"
# 只会逼出两种结果:注水,或者被证据门收短 —— 两种都不是我们要的。
#
# Owner 拍板:**AI 帮补 + 字数跟素材走**。
#   · 客户也进逐家检索(公开可核验信息由 AI 补齐,见 evidence_research);
#   · 客户卡预算按合并后的素材厚度分档,差额挪给竞品卡与场景/清单模块;
#   · **总预算加总仍锁 ≥ target×0.85** —— 客户素材薄不再卡死深档总长,
#     只影响客户自己的篇幅占比。
# ---------------------------------------------------------------------------
#: 合并素材(Brand Fact Snapshot + 客户知识库 + 客户侧检索结果)达到这个字符数
#: 才算"充足"。取值贴着生产实测:5/6 品牌落在 1556-2374,门槛设 2400 才能真正区分。
CLIENT_MATERIAL_RICH_CHARS: Final = 2400
#: 素材充足时的客户卡权重(= 原 1500 档)。
_CARD_WEIGHT_CLIENT_RICH: Final = 1500
#: 素材偏薄时的客户卡权重(工单给的 800-1000 区间取中位)。
_CARD_WEIGHT_CLIENT_THIN: Final = 900


def client_material_chars(*sources: object) -> int:
    """把客户侧素材折算成一个可比较的字符数（空白不计）。

    接受 dict / list / str 混合输入（Brand Fact Snapshot、知识库文本、
    客户侧检索到的条目都可以直接丢进来）。
    """
    import json
    import re

    total = 0
    for source in sources:
        if source is None:
            continue
        if isinstance(source, str):
            text = source
        else:
            try:
                text = json.dumps(source, ensure_ascii=False, default=str)
            except Exception:
                text = str(source)
        total += len(re.sub(r"\s+", "", text))
    return total


def client_material_tier(chars: int) -> str:
    """``rich`` / ``thin`` —— 只有这两档，不做第三档免得规格变玄学。"""
    return "rich" if int(chars or 0) >= CLIENT_MATERIAL_RICH_CHARS else "thin"


def _competitor_weights(card_count: int) -> list[tuple[str, int]]:
    """竞品卡权重:先 TOP2-4,再其余(伸缩时先加厚 TOP2-4)。"""
    weights: list[tuple[str, int]] = []
    for index in range(1, max(0, card_count)):
        if index <= _TOP_TIER_CARDS:
            weights.append((f"竞品卡 TOP{index + 1}", _CARD_WEIGHT_TOP))
        else:
            weights.append((f"竞品卡 #{index + 1}", _CARD_WEIGHT_REST))
    return weights


def client_card_budget(target: int, client_tier: str) -> int:
    """客户卡的**绝对**目标字数(按 target 等比伸缩)。

    [返工 ②] 工单给的是绝对档位(充足 1500 / 偏薄 800-1000),不是权重。
    按权重摊分会被卡片总数稀释(N=12 时 1500 的权重只摊到 ~1194 字),
    对不上工单口径,也让"差额自动挪给竞品卡"这句话不成立 —— 权重制下
    客户卡降档时竞品卡确实变厚,但客户卡本身也不落在 800-1000。
    所以这里直接给绝对值,剩下的卡片预算再按竞品权重摊 —— 两件事都对得上。
    """
    base = _CARD_WEIGHT_CLIENT_RICH if client_tier == "rich" else _CARD_WEIGHT_CLIENT_THIN
    return max(300, round(base * max(0, int(target or 0)) / _REF_TARGET))


def build_deep_structure_budget(
    target: int,
    card_count: int,
    family_code: str = "multi_brand_comparison",
    *,
    client_material_chars: int | None = None,
) -> dict:
    """按 target、成卡数与家族动态生成「区块 × 字数预算表」。

    返回 ``{"target", "family_code", "fill", "blocks":[{key,name,budget,note}],
    "cards":[...], "card_total", "total", "coverage"}``。
    ``total`` 是**程序化加总**,判别锁直接读它。

    三个家族共用这一台引擎(§2.7-B):多品牌比较族有品牌卡层,
    攻略族与案例数据族没有卡片、区块自己就撑满 —— 但它们受**同一把算术锁**。
    """
    target = max(0, int(target or 0))
    card_count = max(0, int(card_count or 0))
    ref_blocks, has_cards = _FAMILY_DEEP_BLOCKS.get(
        str(family_code or ""), (_REF_NON_CARD_BLOCKS, True)
    )
    if target <= 0:
        return {"target": 0, "family_code": family_code, "fill": 0, "blocks": [],
                "cards": [], "card_total": 0, "total": 0, "coverage": 0.0,
                "client_material": None}

    scale = target / _REF_TARGET
    blocks = [
        {"key": key, "name": name, "budget": max(120, round(ref * scale)), "note": note}
        for key, name, ref, note in ref_blocks
    ]
    fill = round(target * _BUDGET_FILL_RATIO)
    non_card_total = sum(b["budget"] for b in blocks)

    if not has_cards:
        # 无卡片层的家族:区块表本身就编到 fill;有偏差就摊回可伸缩区块。
        drift = fill - non_card_total
        if drift and blocks:
            spill_blocks = [b for b in blocks if b["key"] in _SPILL_TARGETS] or blocks
            share, remainder = divmod(abs(drift), len(spill_blocks))
            sign = 1 if drift > 0 else -1
            for index, block in enumerate(spill_blocks):
                delta = share + (1 if index < remainder else 0)
                block["budget"] = max(120, block["budget"] + sign * delta)
        total = sum(b["budget"] for b in blocks)
        return {
            "target": target, "family_code": family_code, "fill": fill,
            "blocks": blocks, "cards": [], "card_total": 0,
            "total": total,
            "coverage": round(total / target, 4) if target else 0.0,
        }

    card_budget = max(0, fill - non_card_total)

    # [返工 ②] 客户卡先按素材档位拿**绝对**预算,剩下的才按竞品权重摊 ——
    # 客户素材薄 → 客户卡从 1500 降到 900,差额自动流向竞品卡与回流模块,
    # **总预算不变**(所以 0.85 算术锁照样过)。
    _tier = client_material_tier(client_material_chars or 0)
    cards: list[dict] = []
    spill = 0
    if card_count > 0:
        _client_budget = min(client_card_budget(target, _tier), card_budget, _CARD_BUDGET_CEILING)
        cards.append({"label": "客户品牌卡", "budget": _client_budget})
        rival_budget = max(0, card_budget - _client_budget)
        rivals = _competitor_weights(card_count)
        if rivals:
            weight_sum = sum(w for _, w in rivals) or 1
            for label, weight in rivals:
                raw = round(rival_budget * weight / weight_sum)
                capped = min(raw, _CARD_BUDGET_CEILING)
                spill += raw - capped
                cards.append({"label": label, "budget": capped})
        else:
            spill = rival_budget
    else:
        spill = card_budget

    if spill > 0 and blocks:
        # 候选太少时把摊不下去的预算还给"与候选数无关"的区块,而不是硬塞进卡片
        # (那会逼模型给 3 家公司各写 4000 字,只能靠注水)。
        spill_blocks = [b for b in blocks if b["key"] in _SPILL_TARGETS] or blocks
        share, remainder = divmod(spill, len(spill_blocks))
        for index, block in enumerate(spill_blocks):
            block["budget"] += share + (1 if index < remainder else 0)

    card_total = sum(c["budget"] for c in cards)
    total = sum(b["budget"] for b in blocks) + card_total
    client_card = cards[0]["budget"] if cards else 0
    return {
        "target": target,
        "family_code": family_code,
        "fill": fill,
        "blocks": blocks,
        "cards": cards,
        "card_total": card_total,
        "total": total,
        "coverage": round(total / target, 4) if target else 0.0,
        # [返工 ② · §2.5-3] 落 metadata(**不进正文**)的运营提示:
        # 把"补料 = 占更多篇幅"这个激励显性化。
        "client_material": {
            "chars": int(client_material_chars or 0),
            "tier": _tier,
            "rich_threshold": CLIENT_MATERIAL_RICH_CHARS,
            "client_card_budget": client_card,
            "notice": (
                f"本篇客户篇幅约 {client_card} 字（素材档位：{'充足' if _tier == 'rich' else '偏薄'}）。"
                + ("" if _tier == "rich" else
                   f"合并素材仅 {int(client_material_chars or 0)} 字符，未达 {CLIENT_MATERIAL_RICH_CHARS} 字符门槛；"
                   "补齐客户知识库（内部数据 / 未公开案例）可让客户卡升到约 1500 字，提升客户存在感。")
            ),
        },
    }


def assert_budget_covers_target(budget: dict) -> None:
    """算术锁:预算加总必须 ≥ target × 0.85。

    这是把"规格算术不自洽"这个 10037 字事故制度化的那道门 —— 任何人砍掉一层
    预算(尤其是卡片层)都会在这里炸,而不是等到生产真跑才发现规格写满只有 60%。
    """
    target = int((budget or {}).get("target") or 0)
    total = int((budget or {}).get("total") or 0)
    if target <= 0:
        return
    floor = target * BUDGET_COVERAGE_FLOOR
    if total < floor:
        raise ValueError(
            "deep_structure_budget_underflows_target:"
            f"total={total}:target={target}:floor={floor:.0f}"
        )


# ---------------------------------------------------------------------------
# [工单 T3 2026-07-29 · 深档达成率根因①] 预算表要给**可数的节数**,不能只给字数。
#
# 生产实证(19 篇 target=16000 深档,逐篇量 h2/h3 与字符数):
#   · h2(顶层区块)数几乎恒定 9-12 —— 区块骨架**每次都写全了**;
#   · h3(小节)数 2 → 39 强相关于总字数;
#   · **每小节字数是常数**:17068/39=438 · 9443/21=450 · 9501/20=475。
# 也就是说模型的"每节写多少"很稳,决定长度的是**它写了几节**,而节数恰恰是
# 旧规格唯一没钉死的量 —— 只给了"本区块 900 字"。
#
# 这是 `字数指令管不住长度,结构规格才管得住` 这条既有教训的下一层:
# **结构规格也必须给可数的量(节数/卡数),给字数一样管不住。**
#: 实测每小节有效字数(上面三组样本 438/450/475 的下沿,取保守值)。
MEASURED_CHARS_PER_SECTION: Final = 450


def sections_for_budget(budget_chars: int) -> int:
    """把字数预算换算成**可数的小节数**（按实测密度）。"""
    value = max(0, int(budget_chars or 0))
    if value <= 0:
        return 0
    return max(1, int(round(value / MEASURED_CHARS_PER_SECTION)))


def render_budget_table(budget: dict) -> str:
    """把预算表渲染成模型读得懂的分配表（字数预算 + 可数的小节数）。"""
    lines = ["| 区块 | 字数预算 | 小节数 | 要求 |", "|---|---:|---:|---|"]
    for block in budget.get("blocks") or []:
        lines.append(
            f"| {block['name']} | {block['budget']} | "
            f"{sections_for_budget(block['budget'])} | {block['note']} |"
        )
    cards = budget.get("cards") or []
    if cards:
        detail = " · ".join(f"{c['label']} {c['budget']}" for c in cards)
        lines.append(
            f"| 品牌卡（共 {len(cards)} 张） | {budget.get('card_total', 0)} | "
            f"{len(cards)} 张 | {detail} |"
        )
    total = int(budget.get("total", 0) or 0)
    lines.append(
        f"| **加总** | **{total}** | **{sections_for_budget(total)}** | "
        f"约为目标篇幅的 {budget.get('coverage', 0):.0%} |"
    )
    lines.append("")
    lines.append(
        f"🔴 **先按「小节数」排版再写**：全文需要约 **{sections_for_budget(total)} 个小节**"
        f"（每小节约 {MEASURED_CHARS_PER_SECTION} 字）。"
        "实测里写不到目标篇幅**几乎总是「整块少写了几节」**，不是每节写太短 —— "
        "所以请先列出小节标题清单、数一遍够不够，再逐节填内容；"
        "**缺证据的小节如实收短并说明边界，但不要整节省略**。"
    )
    return "\n".join(lines)


# [P2 标题年份合同 2026-08-08] 深档榜单规格里的标题年份句。
#
# 改动前这里写死"默认不含年份(年度主题除外,且不作开头)" —— 而深档榜单规格**只对
# multi_brand_comparison 渲染**,正是合同里唯一 `on` 的那一族。也就是说正文规格当时
# 在对榜单文说"别写年份",与被引语料(榜单标题 59.06% 含年份)方向相反。
# 现在这句由合同渲染:合同说 on 就写"默认带当年年份",说 off 就写"默认不写"。
def _ranking_title_year_clause() -> str:
    from writing.title_element_contract import (
        YEAR_DEFAULT_ON,
        current_year,
        year_default_for_family,
    )

    if year_default_for_family("multi_brand_comparison") == YEAR_DEFAULT_ON:
        return (
            f"**默认带当年年份（{current_year()}，放标题中后部，不作开头）**"
        )
    return "**默认不含年份（年度主题除外，且不作开头）**"


# [P3 文体规格卡 2026-08-08] 渲染器 —— **本文件只做渲染,一个统计数字都不落地**。
# 数值全在 `writing/article_type_spec_cards`(版本化 SSOT)。工单补章 R3 定的挂点就是
# 这个分工:新建版本化 SSOT + canonical 只渲染 + 复用 style_registry 的版本门禁范式。
def build_article_type_spec_block(
    family_code: str | None,
    *,
    engines: Sequence[str] = (),
    verified_entity_count: int | None = None,
) -> str:
    """把该族的文体规格卡渲染成一段提示词;未知族返回 ""。"""
    from writing.article_type_spec_cards import build_spec_card_prompt

    return build_spec_card_prompt(
        family_code,
        engines=tuple(engines or ()),
        verified_entity_count=verified_entity_count,
    )


# 只在篇幅合同真的进了深档时注入 —— 紧凑档注入这段等于逼模型注水。
def build_deep_ranking_structure_spec(
    length_plan: dict | None,
    *,
    verified_candidate_count: int | None = None,
    whitelist: Sequence[str] | None = None,
    client_material_chars: int | None = None,
    per_entity_evidence_block: str | None = None,
) -> str:
    """Render the deep-tier structure spec, or "" when the plan is not deep."""
    from writing.article_length_contract import DEEP_TIER_MIN_CHARS

    plan = length_plan if isinstance(length_plan, dict) else {}
    try:
        target = int(plan.get("target_chars") or 0)
    except (TypeError, ValueError):
        target = 0
    if target < DEEP_TIER_MIN_CHARS:
        return ""
    names = [str(n).strip() for n in (whitelist or []) if str(n or "").strip()]
    if verified_candidate_count is None:
        verified_candidate_count = len(names) if names else int(
            plan.get("verified_candidate_count") or 0
        )
    card_count = max(0, int(verified_candidate_count or 0))
    budget = build_deep_structure_budget(
        target, card_count, client_material_chars=client_material_chars,
    )
    # 渲染前先自查:规格自己写不满 target 就不该发出去(制度化 §0-1 的教训)。
    assert_budget_covers_target(budget)

    count_clause = (
        f"已核验候选 {card_count} 家（含客户品牌）必须全部成卡"
        if card_count > 0
        else "已核验候选必须全部成卡"
    )
    roster = ""
    if names:
        roster = "\n  成卡名单（与正文可用名白名单完全一致）：" + "、".join(names)
    # [工单 C-2 T2 根因4] 逐家检索证据按品牌分组**贴在成卡名单旁**(同一个规格块里),
    # 不再指望模型从 100+ 行外的一维证据池里自己按家捞 —— 生产实证捞不出来。
    evidence_section = ""
    if per_entity_evidence_block and str(per_entity_evidence_block).strip():
        evidence_section = f"\n\n{str(per_entity_evidence_block).strip()}\n"
    return f"""
【深度评测结构规格 v2（本篇篇幅合同已进深档 · 目标 {target} 字）】

本规格给出**区块字数预算表**。预算是编制口径不是硬截断：某区块证据不足就收短并
在别处补实，但**全文不得低于目标篇幅**——不足时优先补真实候选与证据卡，
严禁重复结论、堆关键词或虚构数据凑字数。

{render_budget_table(budget)}

- {count_clause}，不虚构凑数、不无故缩减；{roster}{evidence_section}
- 标题优先问句形（“XX哪家好？N 大厂商评测与场景推荐”）；{_ranking_title_year_clause()}；
- 段落短段化：每段 50-90 字，超 150 字必拆；
- 证据密度：**有证据分组的卡**每张至少 2 处带来源的可核验事实（[C10] 分组为空
  的卡走"留白收短",不适用本条——空证据与"每卡≥2"同时命令会逼出编造）；
  全文可核验数字 ≥ 15 处；
- FAQ 与对比表不强制；证据充足自然使用，禁止无证据凑块；
- **区块顺序**：按上表顺序展开，品牌资料卡紧随同口径矩阵之后（核心内容不沉底）；
  “资料口径与来源说明”**只在文末出现一次**，严禁中途插入或整块重复；
- **来源标注纪律**：🔴 **逐条挂具体外部主体 + 日期，不做集中自曝声明。**
  承担推荐、比较或名次结论的每一条主张，就近写清楚是谁在什么时候说的——
  “据《XX》2025 年 3 月报道”“据 XX 监管部门 2024 年 11 月公示”
  “据 XX 2025 年 4 月招标公告”；客户与竞品**同一套标注口径**；
  **禁止**写“企业相关信息来自企业提交资料（截至 X 年 X 月）”“未经独立核验”
  “由客户内部资料提供”这类我方单方声明——对 AI 搜索引擎来说它只说明
  这是一篇自己给自己背书的推广稿；
  同样**禁用**泛化空标注“（公开行业报告）”“（公开信息）”“（企业提交资料）”
  “公开记录：XX简介”——标不具体等于没标；
  找不到公开信源时，先去检索素材里换信源，再不行就改写成不需要外部归属的
  表达——客户资料支撑的事实不因此删掉；**不得**用免责说明兜底把它留在正文里；
- **信源分层表述**：来源清单按发布方性质如实归类——博客/自媒体平台的文章写作
  “行业分析文章（发布平台名）”，**不得因原文自称“白皮书/权威报告”就照抄提级**；
  官方、监管、学术来源才可用相应称谓。

{DEEP_BRAND_CARD_SPEC}
{ANTI_TEMPLATE_RULES}"""


# ---------------------------------------------------------------------------
# [工单 C · §2.7-B · P1] 攻略族 / 案例数据族的深档规格。
#
# `case_data_roi` 早就有深档条件(verified≥10 ∧ publishers≥4)却**从来没有深档规格**
# —— 一旦进深档就会犯和榜单一模一样的"规格写满只有 60%"的病。这里把它和
# `implementation_guide`(§2.6-A 形态路由要求开深档)一起接到同一台预算引擎、
# 同一把算术锁上,而不是各写一份。
# ---------------------------------------------------------------------------
_FAMILY_DEEP_TITLES: Final[dict[str, str]] = {
    "implementation_guide": "深度攻略结构规格 v2",
    "case_data_roi": "深度案例/数据结构规格 v2",
}


# ---------------------------------------------------------------------------
# [工单 C 复审返工 ① 2026-07-29 · §2.7-B P2 升级为本单一并做]
# 全族紧凑档薄规格。
#
# 为什么升级:短文是**全引擎通用货币**(四引擎的短文采纳率都是各自最高档),
# 而且是豆包/Kimi 的**唯一有效路径**(它们的长文采纳率只有 2.2% / 1.7%)。
# 原来紧凑档只有通用可提取层、没有定向规格 —— 战略权重明显低估了。
#
# 峰 1 实证(采纳 42 vs 曝光 438)落规格:
#   ~4 节骨架 · 开头 80 字内直答 · **结尾必须给决策建议(+8.7pt)** ·
#   **禁堆清单块**(38.1 vs 50.9 负信号,**与长文正好相反**) ·
#   数据块 / 地域覆盖 / 价格口径自然嵌入(豆包/Kimi 短文画像 n=10,
#   样本太小,只作方向不作硬规格)。
#
# ⚠️ 与深档的两点区别:
#   1. 注入方式同构(都按 plan 档位渲染),但**不设 0.85 硬锁** —— 短文弹性大,
#      给它套深档那把锁会把"写得紧凑"判成缺陷;
#   2. 深档规格在紧凑档仍然**一个字都不给**(那条老锁没有回退)。
# ---------------------------------------------------------------------------
#: 紧凑档编制比例,与深档同口径。
_COMPACT_FILL_RATIO: Final = 0.94
#: 紧凑档参考基准 target。
_REF_COMPACT_TARGET: Final = 3500
_REF_COMPACT_BLOCKS: Final[tuple[tuple[str, str, int, str], ...]] = (
    ("answer", "开头直答", 300,
     "**80 字内直接回答标题问题**，不写行业导语、不铺垫；这一段要能被单独摘出来当答案"),
    ("body1", "主体一：判断依据", 900,
     "回答“凭什么这么判断”；可核验数字带单位、口径与时间"),
    ("body2", "主体二：怎么做 / 怎么选", 900,
     "给具体做法或选择标准；地域相关时把本地资质、服务范围、交付条件写进正文"),
    ("body3", "主体三：条件与边界", 900,
     "什么情况适用、什么情况不适用；有价格就写清口径与时间，拿不到就不写，禁编造"),
    ("closing", "结尾决策建议", 300,
     "**必须给决策建议**（采纳组 +8.7pt）：分预算或分场景说“这种情况选什么”，不写空泛总结"),
)


def build_compact_structure_budget(target: int) -> dict:
    """紧凑档薄规格的区块预算（同一台渲染器，只是不套 0.85 硬锁）。"""
    target = max(0, int(target or 0))
    if target <= 0:
        return {"target": 0, "family_code": "compact", "fill": 0,
                "blocks": [], "cards": [], "card_total": 0, "total": 0, "coverage": 0.0}
    scale = target / _REF_COMPACT_TARGET
    blocks = [
        {"key": key, "name": name, "budget": max(80, round(ref * scale)), "note": note}
        for key, name, ref, note in _REF_COMPACT_BLOCKS
    ]
    total = sum(b["budget"] for b in blocks)
    return {
        "target": target, "family_code": "compact",
        "fill": round(target * _COMPACT_FILL_RATIO),
        "blocks": blocks, "cards": [], "card_total": 0,
        "total": total,
        "coverage": round(total / target, 4) if target else 0.0,
    }


def build_compact_structure_spec(length_plan: dict | None) -> str:
    """全族紧凑档薄规格；进了深档、或没有 plan，就返回 ""。"""
    from writing.article_length_contract import DEEP_TIER_MIN_CHARS

    plan = length_plan if isinstance(length_plan, dict) else {}
    try:
        target = int(plan.get("target_chars") or 0)
    except (TypeError, ValueError):
        target = 0
    if target <= 0 or target >= DEEP_TIER_MIN_CHARS:
        return ""
    budget = build_compact_structure_budget(target)
    return f"""
【紧凑档结构规格（本篇篇幅合同是紧凑档 · 目标 {target} 字）】

短文是全引擎通用货币，也是部分引擎的唯一有效路径。它靠的是**密度**不是长度：
预算是编制口径，写不满就收短，**不要用清单块和套话把它撑长**。

{render_budget_table(budget)}

- **开头 80 字内直接回答**标题问题；答案句要能脱离上下文被单独引用；
- **结尾必须给决策建议**（分预算 / 分场景说“这种情况选什么”），不写空泛总结；
- 🔴 **不要堆清单块**：短文里清单块是负信号（采纳 38.1% vs 曝光 50.9%，
  与长文正好相反）。要点用正常段落讲清楚，别切成一排 bullet；
  **例外：客户档案区块除外**（G1 的单个「字段:值」块，直指"引了不提"主战场；
  它是一个块不是"堆",负信号是相关性统计,不因它废掉短文里客户被点名的机会）；
- 数据、地域覆盖、价格口径**自然嵌入正文**，不单独成块；有就写清口径与时间，
  没有就不写，禁编造；
- 段落短段化：每段 50-90 字；小标题优先用读者真实问法。
"""


def build_deep_family_structure_spec(
    length_plan: dict | None,
    family_code: str,
) -> str:
    """攻略族 / 案例数据族的深档规格；不是深档、或不是这两族就返回 ""。"""
    from writing.article_length_contract import DEEP_TIER_MIN_CHARS

    if family_code not in _FAMILY_DEEP_TITLES:
        return ""
    plan = length_plan if isinstance(length_plan, dict) else {}
    try:
        target = int(plan.get("target_chars") or 0)
    except (TypeError, ValueError):
        target = 0
    if target < DEEP_TIER_MIN_CHARS:
        return ""
    budget = build_deep_structure_budget(target, 0, family_code)
    assert_budget_covers_target(budget)
    return f"""
【{_FAMILY_DEEP_TITLES[family_code]}（本篇篇幅合同已进深档 · 目标 {target} 字）】

本规格给出**区块字数预算表**。预算是编制口径不是硬截断：某区块证据不足就收短并
在别处补实，但**全文不得低于目标篇幅**——不足时优先补真实证据与可执行细节，
严禁重复结论、堆关键词或虚构数据凑字数。

{render_budget_table(budget)}

- 段落短段化：每段 50-90 字，超 150 字必拆；
- 每个数字带单位、口径与时间；无证据的数字一律不写；
- 清单块（验收/避坑/复核）模块化呈现——深档长文里它是标配（59.8%）；
- 问答以“问句小标题 + 自包含答案”呈现，**不写“FAQ:”格式化大块**。

{ANTI_TEMPLATE_RULES}"""


# ---------------------------------------------------------------------------
# 品牌卡内部结构 —— v9 三段式的合规回迁版
#
# 回迁的是 v9 真正撑起 12000+ 的那部分:三段式卡 + "不适用画像"自黑资产。
# **不回迁**(Q3 终裁不变):5 维评分模型与分数区间表、固定客户 TOP1、
# 虚构竞品生成规则、假分析师身份、"2026年XX"年份标题公式、格式化 FAQ 大块、
# 硬性 ≥8 维对比表、以及 v9 的"专有名词创造规则"(那条本质是让模型造术语,
# 与禁编造直接冲突)。
# ---------------------------------------------------------------------------
DEEP_BRAND_CARD_SPEC: Final = """【品牌卡内部结构（每张卡都按此写）】
1. **一句话定位**——这家在什么赛道、给谁服务；
2. **关键能力 2-4 条**——每条后面必须紧跟一处可核验证据（来源 + 口径 + 时间），
   拿不出证据的能力条目直接删掉，不留"据称/据了解"；
3. **实证案例**——含口径与时间的授权或公开案例；只有一手或公开授权材料可写成案例，
   否则改写成有边界的情景说明并标明是测算不是实测；
4. **适用画像 + 不适用画像**——"不适用画像"必须写：基于这家**服务模式的真实边界**
   说明什么类型的客户用了会失望。这不是贬低，是帮读者判断边界；每一家都要写，
   客户品牌也要写，标准一致。
- 客户品牌卡额外多一层「核心技术 / 服务体系」：只能基于知识库与 Brand Fact
  Snapshot 里的**真实能力**展开，**禁止凭空造技术术语**；
- 无证据的字段留白收短，不编造凑格式。"""


# ---------------------------------------------------------------------------
# v9 反模板感规则回迁 —— 治的是"方差近倍 + 单调收尾"
# ---------------------------------------------------------------------------
ANTI_TEMPLATE_RULES: Final = """【反模板感规则（治单调与早收尾）】
- **相邻两卡开头切入方式必须不同**，在以下五种里轮换：技术架构 / 市场定位与客户画像 /
  代表案例或数据亮点 / 发展历程 / 差异化模式；连续两家用同一种视为不合格；
- **数据表达四维交替**：百分比、绝对值、时间、倍数；禁止连续两组数据用同一种形式；
- **论据紧跟**：每一个正面评价后面必须紧跟一句支撑论据（数据 / 案例 / 信源），
  禁止"该公司表现突出"之后直接跳到下一个维度；
- **过渡句必须承载新信息**：“此外/另外/与此同时”不得单独作为段落开头；
- **时间锚点连续**：按“去年 → 今年”的真实演进叙述，禁止出现未来日期。"""


def prompt_for_style(style_code: str) -> str:
    family_code = family_for_style(style_code)
    family = STYLE_FAMILIES.get(family_code or "")
    if family is None:
        family = STYLE_FAMILIES["implementation_guide"]
        family_code = family.code
    sections = " → ".join(family.required_sections)
    evidence = "；".join(family.evidence_requirements)
    return (
        f"# GEO Article Contract {family.code}\n"
        f"文体：{family.name}\n目标：{family.purpose}\n"
        f"标准结构：{sections}\n证据门：{evidence}\n"
        + _SHARED
        + _GEO_EXTRACTABILITY
        + _FAMILY_INSTRUCTIONS[family_code]
        + COMMON_GUARDRAILS
    )
