"""Evidence-first policy shared by GEO topic, writing, and output gates.

The legacy style codes remain valid for stored topics and articles. This module
changes the semantics of newly generated content: an old "ranking" style is an
unordered evidence comparison, not a paid placement with a manufactured score.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import logging
import os
import re
from typing import Final, Iterable


logger = logging.getLogger("GEO-EvidencePolicy")

POLICY_MARKER = "[GEO_EVIDENCE_FIRST_V1]"


def is_evidence_first_enabled() -> bool:
    """Return the rollout switch. Enabled by default; false is the rollback."""
    value = os.getenv("GEO_EVIDENCE_FIRST_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "off", "no"}


@dataclass(frozen=True)
class TrustFinding:
    code: str
    severity: str
    message: str
    evidence: str = ""
    # [WP9-P0-7 ②] span 级"AI 仅修此处"的定位锚 = **精确命中串**(match.group(0))。
    # 刻意不用字符偏移:检测文本经 _without_negated_rankings 删改过,偏移无法可靠映射回
    # 原文;而命中串本身在原文中原样存在,按子串定位稳健。空串表示无法定位(调用方须降级)。
    matched_text: str = ""


@dataclass(frozen=True)
class TrustAssessment:
    hard: tuple[TrustFinding, ...] = ()
    soft: tuple[TrustFinding, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.hard

    def warning_payload(self) -> dict:
        # [Review-CTO 2026-07-23 P2] 审计血缘记录法律清单版本,与标题/正文
        # 提示词、运行时硬门同源(同一常量),便于事后追溯用哪版清单判定。
        return {
            "policy": "evidence_first_v1",
            "legal_prohibition_catalog_version": LEGAL_PROHIBITION_CATALOG_VERSION,
            "hard": [asdict(item) for item in self.hard],
            "soft": [asdict(item) for item in self.soft],
        }


class EvidenceFirstViolation(ValueError):
    """Raised before persistence when content violates a hard trust rule."""

    def __init__(self, assessment: TrustAssessment):
        self.assessment = assessment
        codes = ",".join(item.code for item in assessment.hard)
        super().__init__(f"EVIDENCE_FIRST_BLOCKED:{codes}")


_RANKING_TITLE_RE = re.compile(
    r"(?:TOP\s*\d+|前\s*[一二三四五六七八九十\d]+\s*(?:名|强|位)?"
    r"(?!分钟|秒|天|年|月|个)|"
    r"排名(?:前|第)?\s*[一二三四五六七八九十\d]*|排行榜|榜单|十佳)",
    re.IGNORECASE,
)
# ============================================================
# [Owner 签发 · 版本化法律禁止清单 · Review-CTO 2026-07-23]
# 依据:《广告法》第九条第(三)项 —— 不得使用"国家级、最高级、最佳"
# 等用语。运行时硬门正则与提示词禁令**同源于本清单**,禁止两套口径;
# 变更须 Owner 签发并升级版本(SSOT §8.1)。
# v2(2026-07-23):新增 第一品牌/遥遥领先/独一无二/不二之选/最大;引入语境守卫。
# v3(2026-07-23):语境守卫改为「基于命中位置的分句局部判定」——否定须在
#   同分句内直接支配、引用/元语言须紧跟被禁词、待核验须直接修饰该主张、
#   双重否定按肯定处理;修掉跨分句(中文逗号)吞掉肯定式宣传的漏洞。
# v4(2026-07-23):否定须直接支配——识别"不是X而是/但/就是Y"转折结构,
#   否定与被禁词间出现谓词重置则不中和(硬拦)。
# v5(2026-07-23):改用语义作用域判定——区分「禁止表达动作」(不能说/不得宣称→
#   覆盖整句主张=advisory)与「否定前项后肯定后项」(不是X而是/也是/且是/同时也是Y
#   →Y重新肯定=hard);不再穷举连接词。
# v6(2026-07-23):禁止表达作用域也在转折/并列处结束(禁止夸大价格但…第一品牌=硬拦);
#   引号/括号/冒号不再当作用域边界;新增疑问/存疑语境放行;血缘冻结本版本。
# v7(2026-07-23):结构性重设计——引号/括号成为一级引用作用域(内部内容=被
#   引用语言材料:引号内命中看引号前禁止动词+右引号后元语言判断;引号内
#   转折不终止外部禁止作用域);禁止动词分言说/行为类(言说宾语从句内转折
#   不终止;行为短语后转折终止);提示词契约与全部生成分支统一为"排名合法+
#   披露依据+禁绝对化/伪造评分"。
# v8(2026-07-23):合并 R2 细化线+统一 R3 逐字例——
#   · 疑问收紧(R2):真是/真的是/究竟/到底 须同分句带 吗/呢 或 还是+对立项
#     才按疑问放行;(?<!不)是不是;谁是/哪家/是否/凭什么 裸匹配安全;
#   · 双重否定两向(R2):不得不/不能不/并非不=肯定;否定前缀后禁跟"不";
#   · 别(急着/着急/忙于/忙着)+表达动词 併入禁止前缀(R2);
#   · 这类/这种 必须后接元语言名词(R2 加固);闭引号不挡紧跟元语言判定;
#   · 引号后判断谓语扩充(并不可信/没有可信依据/是禁用词…)+引号前"所谓"中和;
#   · 疑问后独立肯定回答("谁是X？当然是我们。")=hard;
#   · 言说宾语内转折+新主语(我们/本公司/我司/该品牌)+系词=作用域终止(hard);
#   · 顿号移出分句边界,禁止前缀与动词间可隔≤24字(将/把+宾语+状语);
#   · 技术参数(最大功率…)、最佳实践/实例 白名单;TOP10 天然安全钉死。
# 🔴 [V6 单点化 2026-08-10] 本文件原有的硬编码目录版本号已废 ——
# 那是**第二份未签发目录**的版本号(旧值见 git history,此处不复写字面量:
# 写出来会被单点化判别锁当成"第二份目录仍在"扫到,那是我踩过两次的
# 「解释自己没做某事的话反被当成证据」)。版本现单点来自 Owner 签发的
# `config/legal_prohibited_pack.json`(经 services.marketing.legal_context)。
# ⚠️ 只废**词项与版本**,本文件的**语境判定算法保留**(否定/引用/疑问/
#    技术参数/最佳实践 白名单)—— 删掉它「第一步」「最大功率」就会被拦。
def _catalog_version() -> str:
    try:
        from services.marketing.legal_context import catalog_version
        return catalog_version()
    except Exception:  # noqa: BLE001 - 取不到版本绝不阻断出稿
        return "unsigned"


class _CatalogVersionProxy(str):
    """兼容既有 `LEGAL_PROHIBITION_CATALOG_VERSION` 的字符串用法,取值时现取签发包。"""

    def __new__(cls):
        return super().__new__(cls, _catalog_version())

    def __str__(self) -> str:  # noqa: D105
        return _catalog_version()


LEGAL_PROHIBITION_CATALOG_VERSION = _CatalogVersionProxy()

# [统一 R3 · 2026-07-23 §五] 排名商业形态口径 SSOT:全仓模板/选题/正文提示词
# 的「排名合法+披露依据」表述**同源于本常量**,与下方证据优先总契约
# (compose_evidence_first_prompt 第 1/2 条)同一口径——排名/TOP/榜单/推荐
# 合法,必须披露依据;禁绝对化用语与伪造评分。禁止第二份写法:旧的
# 排名非法化口径(榜单清零、名次缺失即废稿等严禁式表述)已全部废止,
# 由 tests/test_ranking_legality_prompt_census.py 守卫(互斥指令不得共存)。
RANKING_FORMS_DISCLOSURE_CLAUSE = (
    "排名、TOP、推荐和 A/B 比较是合法商业回答形态；若标题/关键词要求，"
    "必须保留并披露依据、样本、时点与边界。"
)

# 🔴 [V6 单点化 · 词项 2026-08-10 · 最终接管工单 §7-3] 词项与版本一样,
# **单点来自 Owner 签发包**(经 services.marketing.legal_context),本文件
# 不再自带目录。分组只做**结构分类**,不增删词:
#   (a) 含「第一」的签发复合词(≥3 字,如 第一品牌/第一名/全国第一)
#       = 排名型绝对宣称,出现即宣称、无需语境;
#   (b) 其余签发词 = 语境守卫词,仅在品牌/产品宣称语境内硬拦
#       ("XX公司是最好的"拦;"最好先核验资质"的副词用法不拦)。
#   裸「第一」**不进**本文件的任何运行时正则 —— 它的序数安全语境
#   (第一步/第一季度)由 legal_context.is_safe_context 承担,这里的
#   品牌语境正则会把「我们公司的第一步」误伤成宣称。
def _signed_absolute_terms() -> tuple[str, ...]:
    try:
        from services.marketing.legal_context import absolute_terms

        signed = tuple(absolute_terms())
    except Exception:  # noqa: BLE001 - 签发包读不到绝不阻断出稿
        signed = ()
    if signed:
        return signed
    # 进程级降级兜底(与签发包 v1.0.0 字面同源;正常路径有锁断言两处相等,
    # 这里不构成第二份目录 —— 只是"包读不到时不把合规门放空"的保险栓)。
    return (
        "国家级", "最高级", "最佳", "最好", "最大", "最优", "最强", "最先进",
        "世界级", "顶级", "第一", "第一品牌", "第一名", "全国第一", "首个",
        "首选", "史上最",
    )


_SIGNED_TERMS = _signed_absolute_terms()
_SIGNED_RANKING_TERMS = tuple(t for t in _SIGNED_TERMS if "第一" in t and len(t) >= 3)
_SIGNED_GUARDED_TERMS = tuple(
    t for t in _SIGNED_TERMS if t not in _SIGNED_RANKING_TERMS and t != "第一"
)

# 🔴 未签发补充词(Review-CTO 2026-07-23 P1-2 历史补全 · **不在**签发包里)。
# 治理口径本应「未经签发无权硬拦」;但这些词全部朝**更严**方向(多拦不放行),
# 在 Owner 对签发包做增补/裁撤裁定之前维持现行硬拦 —— 静默放宽《广告法》
# 合规门比多拦几个词危险得多。🔴 方向锁:只许更严不许更宽 ——
# 往补充集加词可以;把签发词挪进补充集、或删签发词,判别锁当场转红。
LEGACY_UNSIGNED_RANKING_SUPPLEMENT = (
    "排名第一", "稳居第一", "位居第一", "全网第一", "行业第一",
    "榜首", "绝对第一", "唯一首选", "唯一的首选", "唯一最佳", "首屈一指",
    "遥遥领先", "独一无二", "不二之选",
)
LEGACY_UNSIGNED_SUPERLATIVE_SUPPLEMENT = ("极品",)

# (a) 排名型/唯一性绝对宣称:出现即宣称,无需语境
ABSOLUTE_RANKING_CLAIM_TERMS = tuple(dict.fromkeys((
    *_SIGNED_RANKING_TERMS, *LEGACY_UNSIGNED_RANKING_SUPPLEMENT,
)))
# (b) 最高级/绝对化用语:仅在品牌/产品宣称语境内硬拦
ABSOLUTE_SUPERLATIVE_TERMS = tuple(dict.fromkeys((
    *_SIGNED_GUARDED_TERMS, *LEGACY_UNSIGNED_SUPERLATIVE_SUPPLEMENT,
)))
_BRAND_CLAIM_CONTEXT = (
    r"(?:品牌|公司|产品|服务商|服务|平台|机构|厂家|门店|我们|本店|旗下|"
    r"方案|系统|设备|软件|工具|型号|款)"
)
_SUPERLATIVE_GROUP = "|".join(re.escape(term) for term in ABSOLUTE_SUPERLATIVE_TERMS)

_ABSOLUTE_FIRST_RE = re.compile(
    r"(?:TOP\s*1\b|"
    + "|".join(re.escape(term) for term in ABSOLUTE_RANKING_CLAIM_TERMS)
    + r")",
    re.IGNORECASE,
)
_ABSOLUTE_SUPERLATIVE_CLAIM_RE = re.compile(
    rf"(?:{_BRAND_CLAIM_CONTEXT}[^。！？!?\n]{{0,16}}(?:{_SUPERLATIVE_GROUP})"
    rf"|(?:{_SUPERLATIVE_GROUP})的[^。！？!?\n]{{0,10}}{_BRAND_CLAIM_CONTEXT})",
    re.IGNORECASE,
)

# [Review-CTO 2026-07-23 P1-2] 合规替代话术(提示词引导用),不参与硬门判定。
COMPLIANT_ALTERNATIVE_TERMS = (
    "具有代表性的", "表现突出的", "头部", "领先梯队", "主要", "建议",
)


def render_ad_law_reminder() -> str:
    """[P1-2] 唯一版本化法律提示词 SSOT:标题与正文提示词都从此生成,
    禁令词与运行时硬门(_ABSOLUTE_FIRST_RE / _ABSOLUTE_SUPERLATIVE_CLAIM_RE)
    **同源**于同一份 catalog。变更须 Owner 签发并升级版本。"""
    banned = "、".join(
        f'"{t}"' for t in (*ABSOLUTE_RANKING_CLAIM_TERMS, *ABSOLUTE_SUPERLATIVE_TERMS)
    )
    alt = "、".join(f'"{t}"' for t in COMPLIANT_ALTERNATIVE_TERMS)
    return (
        f"\n广告法合规（法律禁止清单 {LEGAL_PROHIBITION_CATALOG_VERSION} ·"
        f"《广告法》第九条 · 违反即作废）：禁止使用 {banned} 等绝对化用语"
        f"(最高级用语指对品牌/产品/服务的宣称)。用 {alt} 等合规替代。"
    )


def legal_prohibition_prompt_terms() -> tuple[str, str]:
    """标题提示词 helper 复用入口:返回 (禁令词串, 清单版本)。"""
    terms = "、".join((*ABSOLUTE_RANKING_CLAIM_TERMS, *ABSOLUTE_SUPERLATIVE_TERMS))
    return terms, LEGAL_PROHIBITION_CATALOG_VERSION
_SCORE_RE = re.compile(
    r"(?:综合(?:评|得)分|评分\s*[:：]?\s*\d|\d(?:\.\d+)?\s*/\s*5|"
    r"(?:总分|得分|推荐指数|专业度|服务能力)\s*[:：]?\s*\d{1,3}(?:\.\d+)?\s*分?|"
    r"(?:★|⭐){3,5}|"
    r"五星(?:推荐|评级|评分)?(?!级|酒店|宾馆)|"
    r"[SABC][+\-]?级(?:推荐|评级|评分))",
    re.IGNORECASE,
)
# ============================================================
# [WP12 P0-2 · Owner 第 6 步红线「禁自创评分体系」]
#
# 榜单文体复活的**前提条件**。飞轮实证:引擎淘汰的不是榜单形态(拐点前后被引
# 榜单式占比 44.7%→43.2% 几乎不变),而是**平台自创的评分/评级**——那是凭空
# 造出来的数据,落在 D11 唯一保留的真红线「引用必须真实存在 · 禁编造数据」上,
# 因此按 H0 处理,而不是 A1。
#
# 口径刻意收窄,避免误伤合法表达:
#   · 只拦"自创的综合分 / 星级 / S-A-B 评级 / 本榜自行评定"这类平台自授评价;
#   · 引用**真实平台已公开的评分**并写清来源/平台/时点的,降回既有 soft
#     manufactured_score(可抽取性层本来就鼓励写"评分4.6(2026-06,平台A)");
#   · 其余评分表述仍走 soft,不升级。
#
# [误报治理 2026-07-30 · T1] 本正则**只认字面串,不看否定词**。命中后必须再过
# 一道否定语境排除(`_self_invented_hit_negated()`,定义在分句作用域一节),
# 否则「本文不采用任何自创评分体系」这种**合规声明**会被判 hard →
# blocked → 且 `article_review_gate` 对 blocked 恒 `overridable=False`,
# 文章被永久锁死(生产 1309/1351/1402 三篇即此)。改本正则前先读那个函数。
#
# 🔴 [T4 口径耦合 · 别单独"补全"这个正则] 首支拦的是「综合评{数字}」/
# 「综合得分{数字}」;而**写作侧 v9 提示词主动要求模型产出**「综合分 = 加权
# 平均」(`writing/ranking_prompt_v9.py:746-747`)与「维度分 5 分制如 4.6/5」
# (同文件 `:869`)。两侧没打架**纯属巧合**:v9 用的词是"综合分",而这里要求
# "综合"后必须跟"评"或"得" —— 差一个字。生产 102 篇含
# `(综合评分|综合得分|综合实力分|总分)[：: ]*[0-9]`,多数还靠命中处 ±80 字内
# 的年份被 `_SCORE_ATTRIBUTION_RE` 降级成 soft 兜着。
# **谁把「综合分」加进本正则、或收紧下面那条年份降级,这 102 篇会集体转 hard。**
# 要动先对齐写作侧口径(`ranking_prompt_v9.py` 属报价 7 保护文件,须 Owner 授权)。
_SELF_INVENTED_SCORING_RE = re.compile(
    r"(?:综合(?:评|得)分|综合实力(?:评|得)分|综合实力分|总分)\s*[:：]?\s*\d{1,3}(?:\.\d+)?\s*分?"
    r"|推荐指数\s*[:：]?\s*[\d★⭐]"
    r"|(?:★|⭐){3,5}"
    r"|[SABC][+\-]?\s*级(?:推荐|评级|评分)"
    r"|(?:本(?:榜|文|次评测|次测评)|我们|本平台|本系统|平台)(?:自行|独立)?(?:评定|打分|评分|加权|评级)"
    r"|(?:自建|自创|独家)(?:的)?(?:评分|评级|打分)(?:体系|模型|标准|权重)",
    re.IGNORECASE,
)
# 真实来源归属:出现在命中附近即视为"引用真实平台评分",降回 soft。
_SCORE_ATTRIBUTION_RE = re.compile(
    r"(?:来源[:：]|数据来源|榜单来源|据[^。！？!?\n]{0,20}(?:平台|网|榜|报告|公告|统计)|"
    r"根据[^。！？!?\n]{0,20}(?:平台|网|榜|报告|公开)|官方(?:公布|披露|发布)|"
    r"公开(?:数据|评分|榜单)|截至\s*20\d{2}|20\d{2}\s*[-年]\s*\d{1,2})"
)
_SCORE_ATTRIBUTION_RADIUS: Final = 80

_ANONYMOUS_AUTHORITY_RE = re.compile(
    r"(?:权威机构|业内权威|某研究院|知名研究机构|专家一致认为|"
    r"业内人士(?:普遍|一致)?认为|行业公认)",
    re.IGNORECASE,
)
_ORDERED_CANDIDATE_LINE_RE = re.compile(
    r"(?im)^\s*(?:#{1,4}\s*)?(?:"
    r"第\s*[一二三四五六七八九十\d]+\s*(?:名|位)|"
    r"(?:NO\.?|TOP)\s*\d+|[1-9]\d*\s*[.、）)])\s*"
    r"(?:\*\*)?[^\n]{1,40}?(?:公司|装饰|科技|服务|平台|机构|集团|网络|品牌)"
)
_UNSOURCED_OUTCOME_RE = re.compile(
    r"(?:提升|增长|降低|节省|转化率|满意度|成功率).{0,18}?\d+(?:\.\d+)?\s*%",
    re.IGNORECASE,
)
# 🔴 [自曝清零 2026-08-10] 邻近来源识别收窄:**必须指向可核验的外部主体**。
#
# 旧口径把这三类也算作"有来源",于是形成第四处**奖励自曝**(比一次性声明豁免更宽):
#   · 裸 `来源[:：]`      —— 「来源:客户提供资料」照样过
#   · 裸 `截至\s*20\d{2}` —— **写「截至 2025 年」四个字就满足挂源要求**
#   · `项目记录|回访记录` —— 我方单方材料
# 实测:一句「(来源:客户提供的服务资料)」就能让 98.6% 这个数字免于
# `unsourced_outcome_number`。模型因此永远不需要去找真的公开信源。
#
# 新口径只认外部主体:具名载体《》/ 归属句式(据X报道·披露·公示·公告)/
# 监管司法工商财报年报 / 实测方法与样本(可复算,不是"谁说的")。
_SOURCE_MARKER_RE = re.compile(
    r"(?:《[^》]{2,40}》|"
    r"据[^，。；\n]{2,24}?(?:报道|披露|显示|公示|公告|通报|发布|载明)|"
    # 🔴 [复审返工 2026-08-10 ②] 「公司公告」从这里**移走** —— 它通常指
    # 公司自己发的公告,不是独立第三方(漏删不是错删)。真正第三方的监管/
    # 交易所公示由上一行的「公示/通报/披露」承担,不受影响。
    r"来源[:：]|公开披露|监管|司法|工商|财报|年报|实测方法|样本)"
)
#: 指向**我方单方材料**的标记。命中 → 该"来源"不算数(见 `_has_nearby_source`)。
_SELF_SOURCE_MARKER_RE = re.compile(
    r"(?:(?:客户|企业|公司|品牌|厂商)(?:方|方面)?(?:提供|自述|提交|内部|档案)|"
    # 🔴 [复审返工 2026-08-10 ②] 公司自己发的公告/官网/官方渠道 —— 可以支撑
    # 事实,但**不能冒充独立第三方背书**。原先「公司公告」在 _SOURCE_MARKER_RE
    # 里被当第三方保留,是漏删。
    r"公司公告|企业公告|官网|官方网站|官方公众号|官方渠道|"
    r"项目记录|回访记录|一手资料|内部资料|知识库)"
)
# [工单 C-3 T1-B 2026-07-27] 榜单"入选口径/依据披露段"识别。
# WP12 榜单复活合同(article_style_contract.RANKING_REVIVAL_CONTRACT)与工单 C 深档规格
# 都要求正文交代排序依据(家族契约具名章节「入选标准」/ 表头「排序依据」列 /
# 自然行文"据《XX》报道"式交代)。旧 ordered_brand_candidates 见编号即报,
# 完全不识别披露已存在 —— 深档榜单文必中。披露作用域 = 全文(契约就是集中披露一次)。
_RANKING_BASIS_DISCLOSURE_RE = re.compile(
    r"(?:入选(?:标准|口径|范围|依据)|排除条件|证据口径|排序依据|推荐依据|"
    r"同口径(?:表|矩阵|比较|对比)|评测(?:口径|维度与依据)|上榜(?:依据|口径))"
)
# [工单 C-3 T1-C] 企业资料"一次性来源声明"识别(source_disclosure_style 推荐句式
# 及其变体)。声明作用域 = 全文内的**企业侧** claim;行业/第三方口径的 claim 不在
# 声明覆盖范围内,照旧要求邻近来源。
_ENTERPRISE_SOURCE_DECLARATION_RE = re.compile(
    r"(?:企业(?:相关信息|相关数据|信息)[^\n。]{0,40}?(?:企业提交|本次提交|企业提供)|"
    r"企业提交(?:的业务)?资料[^\n。]{0,40}?(?:整理|截至\s*20\d{2}|未经独立核验|"
    r"待交叉核验|尚未完成独立交叉核验|核验起点))"
)
# 行业/第三方口径标记:命中说明该百分比不是企业自述,一次性声明覆盖不到。
_INDUSTRY_SCOPE_MARKER_RE = re.compile(
    r"(?:行业(?:平均|整体|内|层面)?|市场(?:平均|整体)?|全国|全球|全网|"
    r"公开(?:数据|研究|调查)|据统计|第三方|同行)"
)
_INDUSTRY_SCOPE_RADIUS: Final = 40
_LIMIT_MARKER_RE = re.compile(r"(?:局限|适用边界|不适用|待确认|需核验|风险|注意事项|资料有限)")
_VERIFY_MARKER_RE = re.compile(r"(?:核验|查验|验证|复核|官方渠道|监管记录|司法记录|投诉记录)")
_COUNTER_MARKER_RE = re.compile(r"(?:不足|限制|争议|投诉|风险|缺点|不适合|注意事项)")


def _excerpt(match: re.Match[str] | None, text: str, radius: int = 40) -> str:
    if not match:
        return ""
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:160]


# [Review-CTO 2026-07-23 P1 · v3 重写] 法律硬门只拦**肯定式违法宣传**。
# 采用「基于命中位置的分句局部判定」(不再整段正则替换):对每个绝对化
# 用语命中,只在**它所在的分句**(以 中文/英文逗号顿号、句号问号叹号、
# 分号冒号破折号、换行 为边界)内判断是否处于非宣称语境;跨分句的否定/
# 引用/待核验一律不生效(防跨句吞掉肯定式)。
_TERM_RE = re.compile(
    "|".join(re.escape(t) for t in (*ABSOLUTE_RANKING_CLAIM_TERMS, *ABSOLUTE_SUPERLATIVE_TERMS))
)
# 分句边界:只用**真正的句读**(句末标点 + 逗号 + 换行 + 分号)。
# [Review-CTO 2026-07-23 v6] 引号/括号/冒号/破折号**不是**作用域边界——被禁词
# 常被引号包住作元语言讨论、或在冒号/括号后作宾语,它们不应切断前面的
# 禁止表达/否定/疑问对被禁词的支配。
# [v8 · R3 逐字例] 顿号(、)连接的是并列名词而非句读,移出分句边界——
# "不应在官网、公众号、海报和销售话术中将本公司描述为行业第一品牌"的
# 禁止作用域必须能越过顿号状语覆盖被禁词(=advisory)。
_CLAUSE_BOUNDARY_LITERAL = "。！？!?；;，,\n\r"
_CLAUSE_BOUNDARY_CHARS = frozenset(_CLAUSE_BOUNDARY_LITERAL)
# [v8 · R3 逐字例] 禁止前缀与禁止动词之间允许的非分句边界间隔(可含
# "将/把+宾语+并列状语",上限 24 字)。
_PROHIBITION_VERB_GAP = rf"[^{re.escape(_CLAUSE_BOUNDARY_LITERAL)}]{{0,24}}?"

# ── [误报治理 2026-07-30 · T1] 自创评分体系:否定语境排除 ──────────────────
# `_SELF_INVENTED_SCORING_RE` 不看否定词,于是「本文**不采用**任何自创评分体系」
# 这类**合规声明**被判 hard → blocked → 永不可覆盖(设计如此,见
# `services/article_review_gate.py:114-124`)→ 文章永久锁死。生产实测 5 篇带
# 该 code 的 blocked 文里,1309/1351/1402 三篇正是这种否定语境误报;
# 1299(锚 `★★★★`)与 1372(锚 `平台评分`)是真命中,**必须继续拦住**。
#
# 复用既有法律硬门那套「基于命中位置的分句局部判定」(_CLAUSE_BOUNDARY_*):
# 否定标记必须在**同一分句内**、且在**命中串之前**,中间不得有转折。
# 三条约束各自对应一个必然的绕法,少一条就是给违规开后门。
_SELF_INVENTED_NEG_GAP = rf"[^{re.escape(_CLAUSE_BOUNDARY_LITERAL)}]{{0,6}}?"
_SELF_INVENTED_NEGATION_RE = re.compile(
    # (a) 否定副词 + 短状语间隔 + 制造/采用类动词。
    #     🔴 `(?!仅|但|只|光|单)` 不可删:不仅/不但/不只 是**递进**结构,语义为
    #     肯定 ——「本文不仅采用自创评分体系,还引入了自定权重」必须照旧 hard。
    r"(?:不(?!仅|但|只|光|单)|未(?!来)|没有|无法|绝不|从不|决不)"
    + _SELF_INVENTED_NEG_GAP +
    r"(?:采用|使用|制造|编造|杜撰|自创|自建|设置|设立|设计|引入|提供|给出|输出|建立|做|搞|含|存在)"
    # (b) 禁令类:本身即否定,直接管辖其后的宾语(「禁止自创评分体系」)。
    r"|(?:禁止|严禁|不得|不准|切勿|请勿|杜绝|摒弃|摈弃|谢绝|拒绝|避免)"
    # (c) 系词否定(「并非自创评分体系」)。
    r"|(?:并非|绝非|远非|而非|并不是|不是|谈不上|算不上|称不上)"
    # (d) 存在否定(「无自创评分体系」);排除"无论/无奈/无需"等词内误命中。
    r"|(?:无(?!论|奈|数|限|比|妨|须|需|法))"
)
# 否定作用域的**终止**:转折后是新的肯定主张,否定不再管辖。
# 「不采用外部标准**而是**自创评分体系」= 确实在用 → 必须照报。
_SELF_INVENTED_NEG_SCOPE_END_RE = re.compile(
    r"(?:而是|而为|而采用|而改用|改为|转而|但是|但|然而|却|不过|可是|反而|反倒)"
)


def _self_invented_hit_negated(text: str, start: int) -> bool:
    """命中是否落在否定语境里(= 合规声明,不该报)。

    判据三条(缺一即可被绕过,对应 §7.2 变异 ②③④):
      · 只看命中串**所在分句**   —— 跨句的"我们不夸大。本文采用自创评分体系。"不算否定;
      · 只看命中串**之前**的文本 —— "本文采用自创评分体系并非临时决定"不算否定;
      · 否定与命中之间**不得有转折** —— "不采用外部标准而是自创评分体系"不算否定。
    """
    lo = start
    while lo > 0 and text[lo - 1] not in _CLAUSE_BOUNDARY_CHARS:
        lo -= 1
    prefix = text[lo:start]
    last_neg = None
    for neg in _SELF_INVENTED_NEGATION_RE.finditer(prefix):
        last_neg = neg
    if last_neg is None:
        return False
    return not _SELF_INVENTED_NEG_SCOPE_END_RE.search(prefix[last_neg.end():])
# [Review-CTO 2026-07-23 v6] 疑问/存疑语境:被禁词处于提问或求证中,不是
# 肯定宣传("谁是行业第一?""'行业第一品牌'可信吗?"),降 advisory。
# [R2 复审 P1-1 · v8 移植] 真是/真的是/究竟/到底 既可表疑问也可表强调
# ("我们真的是行业第一品牌"是肯定式宣传)——仅当同分句带疑问语气词(吗/呢)
# 或选择疑问结构(还是+对立项)才按疑问放行;谁是/哪家/是否/凭什么 等本身
# 即疑问代词/副词,裸匹配安全;是不是 前有"不"时是双重否定结构(不是不是=
# 肯定),不作疑问;还是 仅在确为选择疑问(紧跟 第二/另一/哪家/谁 等对立项)
# 时放行,"究竟还是X"(强调)与"他还是X"(仍然)不按疑问处理。
_INTERROGATIVE_RE = re.compile(
    r"(?:谁是|谁能|谁最|哪家|哪个|哪些|哪一|是否|(?<!不)是不是|算不算|够不够|凭什么|凭啥|"
    r"(?:真是|真的是|究竟|到底)(?=[^。！？!?；;，、,\n\r]{0,12}(?:吗|呢|"
    r"还是(?=(?:第二|第三|另一|别的|别家|哪家|哪个|谁|其他|普通|跟随))))|"
    r"(?:可信|靠谱|真的|属实|成立|名副其实|名不副实|当真|可靠)(?:吗|不|与否|？|\?)|"
    r"[？?])"
)
# [v8 · R3 逐字例 H1] 疑问分句的**下一分句**若是独立肯定回答
# ("谁是行业第一品牌？当然是我们。"),疑问不再中和(=宣传,hard)。
_INTERROGATIVE_ANSWER_HEAD_RE = re.compile(
    r"^(?:当然|自然|无疑|就是|正是|必须是|舍我其谁|非[^。！？!?；;，,\n\r]{0,8}莫属)"
)
_INTERROGATIVE_ANSWER_SUBJ_RE = re.compile(r"(?<!不)(?:是我们|是本公司|是我司)")
# 否定短语(多字为主,避免"无论/未来/一个"等误命中裸字);词内不含被禁词。
_NEGATION_PHRASES = (
    "并不是", "不是", "并非", "绝非", "远非", "不算", "不属于", "不为",
    "不得", "不要", "不应", "不能", "不可", "不代表", "没有", "并未", "从不",
    "决不", "绝不", "谈不上", "算不上", "称不上", "够不上", "杜绝", "避免",
    "拒绝", "切勿", "勿以", "无需", "毋须", "不宜",
)
# 被禁词自身含否定语义字(不/无/非/未/否)——其前再加否定=双重否定=肯定,
# 不予中和(如"不是不二之选""不是独一无二")。
_NEG_SEMANTIC_CHARS = frozenset("不非无未否")
# [Review-CTO 2026-07-23 v5] 语义作用域判定(不再单纯枚举连接词):
# (1) 禁止表达动作 = 否定的是"作出该主张"这件事,其后**整个主张**都在否定
#     作用域内(不能说/不得宣称/不要使用/禁止写成…),被禁词随之降 advisory。
# [Review-CTO 2026-07-23 v7] 禁止动词分两类:
#   speech(言说类):宾语是"被引用的言语内容"(可为完整从句,内部转折不终止
#     作用域):不得宣称[我们不是X却是第一品牌] → 整从句都是被禁内容;
#   action(行为类):宾语是短行为短语,其后转折开启新独立主张:
#     禁止夸大价格|但本公司是第一品牌 → 但后 hard。
# [v8 · R2 P1-2 移植] 前缀后禁跟"不":不得不/不能不+动词是双重否定
# (=肯定义务,"不得不说是行业第一品牌"必须 hard),不是禁止表达动作。
# [v8 · R2 移植复审] 别(急着/着急/忙于/忙着)+表达动词 併入禁止前缀:
# 覆盖"别急着说自己是第一品牌"类口语禁令(=advisory)。裸"别"只与表达
# 动词成组出现,不影响"别的不说,我们就是第一品牌"类跨分句肯定(分句
# 局部判定本就不吞跨逗号主张)。
_PROHIBITION_PREFIX = (
    r"(?:不能|不得|不要|不应|不可|不准|严禁|禁止|请勿|切勿|勿|杜绝|避免|谨防|"
    r"别(?:急着|着急|忙于|忙着)?)(?!不)"
)
# [v8 · R3 逐字例] 前缀与动词之间允许非分句边界间隔(_PROHIBITION_VERB_GAP,
# 可含"将/把+宾语+顿号并列状语",如"不应在官网、公众号…将本公司描述为…")。
_PROHIBITION_SPEECH_RE = re.compile(
    _PROHIBITION_PREFIX + _PROHIBITION_VERB_GAP +
    r"(?:说成|说|讲|写成|写作|写|宣称|声称|自称|号称|称作|称为|称|表述为|表述|"
    r"表达为|表达|描述为|描述|定义为|叫做|叫)"
)
_PROHIBITION_ACTION_RE = re.compile(
    _PROHIBITION_PREFIX + _PROHIBITION_VERB_GAP +
    r"(?:使用|采用|标注为|标注|标称|标榜|宣传为|宣传|鼓吹|夸大为|夸大|"
    r"承诺|保证|吹嘘|提及|冠以|冠名)"
)
_PROHIBITION_OF_EXPRESSION_RE = re.compile(
    f"(?:{_PROHIBITION_SPEECH_RE.pattern}|{_PROHIBITION_ACTION_RE.pattern})"
)
# 成对引号/括号(引用作用域一级对象):内部内容是"被引用的语言材料"。
_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』", "《": "》", "【": "】",
                "（": "）", "(": ")"}
_QUOTE_SYMMETRIC = ("\"", "'")


_MAX_QUOTE_SPAN = 60  # 超长"配对"视为噪声(未闭合引号误配),不当引用作用域


def _quote_spans(text: str) -> list[tuple[int, int]]:
    """返回成对引号/括号的 [lo, hi](含引号字符)列表;不匹配/超长的忽略。"""
    spans: list[tuple[int, int]] = []
    stack: list[tuple[str, int]] = []
    sym_open: dict[str, int] = {}
    for i, ch in enumerate(text):
        if ch in _QUOTE_PAIRS:
            stack.append((ch, i))
        elif stack and ch == _QUOTE_PAIRS[stack[-1][0]]:
            _, lo = stack.pop()
            if i - lo <= _MAX_QUOTE_SPAN:
                spans.append((lo, i))
        elif ch in _QUOTE_SYMMETRIC:
            if ch in sym_open:
                lo = sym_open.pop(ch)
                if i - lo <= _MAX_QUOTE_SPAN:
                    spans.append((lo, i))
            else:
                sym_open[ch] = i
    return spans


def _strip_quoted(text: str, lo: int, hi: int, spans: list[tuple[int, int]]) -> str:
    """取 text[lo:hi],但把落在引号 span 内(含引号)的字符剔除(结构扫描用)。"""
    out = []
    for i in range(lo, hi):
        if any(qlo <= i <= qhi for qlo, qhi in spans):
            continue
        out.append(text[i])
    return "".join(out)
# (2) 否定前项后肯定后项:否定作用域在**重新出现的肯定系词/并列谓词**处结束,
#     其后被禁词是新的肯定主张,必须硬拦。识别"否定作用域结束后重建的肯定谓词",
#     而非穷举个别连词:并列/转折连接 + 肯定系词(是/为/属)或独立并列谓词。
_REAFFIRM_SCOPE_END_RE = re.compile(
    r"(?:"
    r"而是|而为|而属|而应|而恰|"                         # 而+系词
    r"但是|但|"                                          # 转折
    r"却|反而|反倒|"                                     # 转折副词
    r"就是|才是|正是|乃是|系是|恰恰是|恰是|则是|"        # 强调系词
    r"其实(?:是|为)?|实际上(?:是|为)?|实为|实则|"        # 实情重述
    r"同时(?:也)?(?:是|为)|又是|又为|"                   # 并列系词
    r"且是|且为|且属|且|"                                # 并列连词
    r"也是|也为|也属|也|"                                # 并列副词
    r"更是|更为|"                                        # 递进系词
    r"而我|而本|而其|而该"                               # 而+新主语(而我们是…)
    r")"
)
# [Review-CTO 2026-07-23 v6 P1-1/P1-B] 禁止表达动作的**作用域终止**只认转折
# 连词(但/然而/却/不过…):它们开启禁止内容之外的**新独立主张**。系词
# (就是/也是/是)是被禁止表达内容的宾语本身,不终止禁止作用域。
#   "禁止夸大价格**但**本公司是第一品牌" → 但 后是新主张 → 硬拦
#   "不能说我们**就是**第一品牌" → 就是 属被禁内容 → advisory
_CONTRAST_CONJUNCTION_RE = re.compile(
    r"(?:但是|但|然而|却|反而|反倒|不过|可是|而(?!是|为|应|属|恰|已))"
)
# [v8 · R3 逐字例 H2/H3] 转折后**新主语重启**的肯定主张:新主语
# (我们/本公司/我司/该品牌,可再带"公司/品牌")+系词/强调系词。
# 命中则言说宾语在转折处已完结("禁止说价格低但本公司是行业第一品牌"
# =hard);"却/但"后直接接谓语("…却不是X")无新主语重启,仍属被禁内容。
_NEW_SUBJECT_CLAIM_RE = re.compile(
    r"^(?:我们|本公司|我司|该品牌)(?:公司|品牌)?\s*"
    r"(?:是|为|乃|系|属|算|就是|正是|恰是|则是|堪称|称得上|成为)"
)
# [R2 移植复审 · v8] 被禁词后的闭引号/闭括号(』」"')"》 等)在元语言/待核验
# 紧跟判定中跳过——与 v6「引号不是作用域边界」对齐:『第一品牌』这类宣传
# 用语需要谨慎 = advisory(闭引号不得挡在紧跟词前)。
_QUOTE_CLOSE_CHARS = r"』」\"'”’）)\]》〉"
# 引用/元语言(明确在讨论"该词语本身"),必须紧跟被禁词(允许隔闭引号)。
# [R2 复审 P2 加固 · v8] "这类/这种"必须后接元语言名词(表述/说法/用语…),
# 否则"我们是行业第一品牌这类公司"类肯定宣称会经 这类 逃逸。
_META_AFTER_RE = re.compile(
    rf"^(?:[{_QUOTE_CLOSE_CHARS}])?\s*(?:的|地|之|等|这|那)?\s*"
    r"(?:(?:这类|这种|此类|那类|那种)(?:表述|用语|说法|字眼|词汇|提法|措辞|"
    r"宣传语|宣传|广告词|词|词语|叫法|写法)|"
    r"(?:表述|用语|说法|字眼|词汇|提法|措辞|宣传语|广告词|一词|等词|类词|"
    r"之类|字样|等绝对化|等违规|等禁用))"
)
# 待核验/合规:必须紧跟被禁词并直接修饰该主张(不接受无关主语的洗白)。
_PENDING_AFTER_RE = re.compile(
    rf"^(?:[{_QUOTE_CLOSE_CHARS}])?\s*(?:等|的)?\s*"
    r"(?:尚待核验|尚需核验|待核验|待核实|待求证|待审核|有待核验|尚待核实|"
    r"需核验|需核实|需求证|需法务核验|需合规核验|需进一步核验|是否合规|"
    r"存疑待核|涉嫌违规|涉嫌违法)"
)


def _clause_span(
    text: str, pos: int, spans: list[tuple[int, int]] | None = None
) -> tuple[int, int]:
    """返回 text[pos] 所在分句的 [lo, hi)。

    [v7] 引号 span 内的逗号/句读**不**作为分句边界(引用内容是一个整体,
    不能把引号里的逗号当断句切掉引号前的禁止动词)。
    """
    spans = spans or []

    def _in_quote(i: int) -> bool:
        return any(qlo <= i <= qhi for qlo, qhi in spans)

    lo = pos
    while lo > 0 and (text[lo - 1] not in _CLAUSE_BOUNDARY_CHARS or _in_quote(lo - 1)):
        lo -= 1
    hi = pos
    while hi < len(text) and (text[hi] not in _CLAUSE_BOUNDARY_CHARS or _in_quote(hi)):
        hi += 1
    return lo, hi


def _find_last_prohibition(struct_before: str):
    """在(已剔引号内容的)前文中找最后一个禁止表达动词。
    返回 (end_offset, kind) · kind ∈ {"speech","action"};无则 (None, None)。"""
    best_end, kind = None, None
    for pm in _PROHIBITION_SPEECH_RE.finditer(struct_before):
        if best_end is None or pm.end() > best_end:
            best_end, kind = pm.end(), "speech"
    for pm in _PROHIBITION_ACTION_RE.finditer(struct_before):
        if best_end is None or pm.end() > best_end:
            best_end, kind = pm.end(), "action"
    return best_end, kind


def _negation_scope(struct_before: str) -> str | None:
    """[v7] 判定被禁词处于哪种否定作用域(输入已剔除引号内内容)。

    返回:
      - "prohibition":禁止表达动作覆盖被禁词:
          · 言说类(说/宣称/写成…):宾语可为完整从句,从句内部的转折
            (却是/但…)不终止作用域("不得宣称我们不是X却是第一品牌");
            仅当转折**紧跟**动词(其间无实质宾语)时才终止
            ("不能虚假宣传|然而我们…"=新主张);
          · 行为类(夸大/使用/标注…):宾语是短行为短语,其后转折终止作用域
            ("禁止夸大价格|但本公司是第一品牌"=硬拦)。
      - "plain":普通否定直接支配被禁词;
      - None:不在任何否定作用域内。
    """
    proh_end, proh_kind = _find_last_prohibition(struct_before)
    if proh_end is not None:
        rest = struct_before[proh_end:]
        cm = _CONTRAST_CONJUNCTION_RE.search(rest)
        if cm is None:
            return "prohibition"
        if proh_kind == "speech":
            object_before_contrast = re.sub(r"[\s，、,]", "", rest[: cm.start()])
            if len(object_before_contrast) >= 2:
                # [v8 · R3 逐字例 H2/H3] 言说宾语内转折**且转折后出现新主语
                # (我们/本公司/我司/该品牌)+系词** → 宾语已完结,转折后是新
                # 独立主张 → 作用域终止(hard):"禁止说价格低但本公司是行业
                # 第一品牌"。但/却后无新主语重启("不得宣称我们不是普通品牌
                # 却是行业第一品牌","却是"直接接谓语)仍在被禁内容里(advisory)。
                if not _NEW_SUBJECT_CLAIM_RE.match(rest[cm.end():]):
                    return "prohibition"
        # 行为类、或言说动词后紧跟转折(无实质宾语)、或转折后新主语重启
        # → 作用域终止,落入普通否定。
    last_end = -1
    last_idx = -1
    for neg in _NEGATION_PHRASES:
        idx = struct_before.rfind(neg)
        if idx >= 0 and idx + len(neg) > last_end:
            last_end = idx + len(neg)
            last_idx = idx
    if last_end < 0:
        return None
    span = struct_before[last_end:]
    if len(span) > 14:
        return None
    # [R2 复审 P1-2 · v8 移植] 双重否定结构=肯定,两向检测:
    #   (a) 仅限惯用双重否定型:否定短语以 得/能/可/应/是/非 结尾且紧跟"不"
    #       (不得不/不能不/不可不/不应不/不是不/并非不)=肯定;
    #       "避免/杜绝/不要/没有 + 无X(无依据/无底线/无资质/无可挑剔)"是被禁
    #       内容内部修饰,不属双重否定,不得误拦(合规禁止表达句=advisory)。
    #   (b) 相邻否定短语连用(并非不是/绝非不是…)=肯定。
    if (
        span[:1] == "不"
        and struct_before[last_idx:last_end][-1:] in "得能可应是非"
    ):
        return None
    prefix = struct_before[:last_idx]
    if any(prefix.endswith(neg) for neg in _NEGATION_PHRASES):
        return None
    if _REAFFIRM_SCOPE_END_RE.search(span):
        return None
    return "plain"


# [v7] 引号后的元语言/判断谓语:右引号之后紧跟这些 → 引号内是被讨论的
# 语言材料,不是作者宣称。
# [v8 · R2 P2 加固对齐] 末组去掉裸 这类|这种|之类(这类/这种 须接元语言
# 名词,由上方加固版 _META_AFTER_RE 覆盖;防"…这类公司"逃逸);并扩充判断
# 谓语:并不可信|不可信|没有可信依据|无可信依据|缺乏依据|是禁用词|为禁用词
# (R3 逐字例 P3/P5:"'行业第一品牌'并不可信。""'第一品牌'是禁用词。")。
_META_AFTER_QUOTE_RE = re.compile(
    r"^\s*(?:这类|这种|此类|那类|那种|之类|等|的)?\s*"
    r"(?:宣传用语|宣传语|宣传表述|表述|用语|说法|字眼|词汇|提法|措辞|广告词|"
    r"一词|字样|写法|叫法|标语|口号|宣传)?\s*"
    r"(?:需要谨慎|需谨慎|要谨慎|属于|是违规|违规|违法|涉嫌|不合规|"
    r"不得使用|禁止使用|应避免|需避免|尚待核验|待核验|需核验|需法务核验|"
    r"并不可信|不可信|没有可信依据|无可信依据|缺乏依据|是禁用词|为禁用词)"
)
# [v8 · R3 逐字例 P7] 最高级后紧跟技术参数量纲名词("最大功率为 5kW")
# → 参数修饰而非品牌宣称,中和。
_TECH_PARAM_AFTER_RE = re.compile(
    r"^(?:功率|扭矩|载重|容量|尺寸|长度|宽度|高度|直径|转速|压力|流量|温度|"
    r"速度|输出|承重|量程|行程|功耗|亮度|分辨率)"
)
# [v8 · R3 逐字例 P8] "最佳实践/实例"=行业通用术语,中和。
_BEST_PRACTICE_AFTER_RE = re.compile(r"^(?:实践|实例)")


def _neutralize_non_assertive_absolutes(text: str) -> str:
    """对每个绝对化用语命中,分句局部+引用作用域判定是否为非宣称语境。"""
    if not text:
        return text
    spans = _quote_spans(text)
    chars = list(text)
    for m in _TERM_RE.finditer(text):
        term = m.group(0)
        lo, hi = _clause_span(text, m.start(), spans)
        after = text[m.end():hi]
        # 结构化前文:剔除引号内内容(引号内是引用材料,不参与作用域结构)
        struct_before = _strip_quoted(text, lo, m.start(), spans)
        # 命中词是否处于引号 span 内
        enclosing = next(
            ((qlo, qhi) for qlo, qhi in spans if qlo <= m.start() and m.end() - 1 <= qhi),
            None,
        )
        neutralize = False
        # (0) 疑问/存疑语境(检查整分句;命中在引号内时还包括右引号后的判断,
        #     如「"行业第一品牌"可信吗?」)。
        clause_full = text[lo:hi]
        if _INTERROGATIVE_RE.search(clause_full):
            neutralize = True
            # [v8 · R3 逐字例 H1] 疑问分句的**下一分句**是独立肯定回答
            # ("谁是行业第一品牌？当然是我们。")=宣传,不中和;
            # 普通疑问("谁是行业第一？需要逐项比较。")保持中和。
            nxt_lo = hi
            while nxt_lo < len(text) and text[nxt_lo] in _CLAUSE_BOUNDARY_CHARS:
                nxt_lo += 1
            nxt_hi = nxt_lo
            while nxt_hi < len(text) and text[nxt_hi] not in _CLAUSE_BOUNDARY_CHARS:
                nxt_hi += 1
            next_clause = text[nxt_lo:nxt_hi]
            if (_INTERROGATIVE_ANSWER_HEAD_RE.match(next_clause)
                    or _INTERROGATIVE_ANSWER_SUBJ_RE.search(next_clause)):
                neutralize = False
        scope = _negation_scope(struct_before)
        # (a1) 禁止表达动作覆盖 → 中和(即使被禁词自身含否定字)。
        #      命中在引号内且引号前有禁止动词(struct_before 已剔引号,禁止
        #      动词在引号外故保留)同样覆盖:禁止写成"…第一品牌"。
        if not neutralize and scope == "prohibition":
            neutralize = True
        # (a2) 普通否定直接支配:中和。被禁短语自身可能含“不/无”
        # （“不二之选”“独一无二”），那是词义的一部分，不会把
        # “并非不二之选/不是独一无二”反转成肯定。真正的双重否定
        # （并非不是/不得不/不能不）已由 _negation_scope 返回 None。
        elif not neutralize and scope == "plain":
            neutralize = True
        # (b) 引用/元语言:紧跟被禁词;或命中在引号内时,看**右引号之后**
        #     是否紧跟元语言/判断谓语("第一品牌"这类宣传用语需要谨慎)。
        if not neutralize and _META_AFTER_RE.match(after):
            neutralize = True
        if not neutralize and enclosing is not None:
            after_quote = text[enclosing[1] + 1 : hi]
            if (_META_AFTER_QUOTE_RE.match(after_quote)
                    or _META_AFTER_RE.match(after_quote)
                    or _PENDING_AFTER_RE.match(after_quote)):
                neutralize = True
            # [v8 · R3 逐字例 P4] enclosing 引号左侧紧邻"所谓" → 引号内是
            # 被讽刺/存疑的语言材料("所谓'行业第一品牌',没有可信依据")→ 中和。
            if not neutralize and text[max(0, enclosing[0] - 2):enclosing[0]] == "所谓":
                neutralize = True
        # (c) 待核验/合规紧跟并直接修饰该主张
        if not neutralize and _PENDING_AFTER_RE.match(after):
            neutralize = True
        # (d) [v8 · R3 逐字例 P7/P8] 技术参数/通用术语白名单:
        #     "本设备最大功率为 5kW"的最大是参数修饰;"最佳实践/实例"是
        #     行业通用术语,均非品牌最高级宣称。
        if not neutralize and term in ABSOLUTE_SUPERLATIVE_TERMS:
            if _TECH_PARAM_AFTER_RE.match(after):
                neutralize = True
        if not neutralize and term == "最佳" and _BEST_PRACTICE_AFTER_RE.match(after):
            neutralize = True
        if neutralize:
            for i in range(m.start(), m.end()):
                chars[i] = " "
    return "".join(chars)


def _without_negated_rankings(text: str) -> str:
    """Remove explicit prohibitions so a safety sentence does not self-trigger.

    [Review-CTO 2026-07-23 v4] 绝对化用语的否定/引用/待核验语境判定统一交给
    分句局部的 _neutralize_non_assertive_absolutes;此处**删除**旧的
    "否定短语 + .{0,18}? + 绝对词" 整段正则(它跨转折/跨谓词重置误剥
    "不是X而是唯一首选"里的肯定命中)。仅保留与硬门无关的「排名形态 vs
    批评语境」剥离(排名/榜单本就是 advisory,此处只防 soft 误报)。"""
    text = _neutralize_non_assertive_absolutes(text)
    return re.sub(
        r"(?:TOP\s*\d+|排行榜|榜单|排名|前十).{0,16}?"
        r"(?:不可信|失真|陷阱|广告|软文|靠谱吗|避坑)",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _has_nearby_source(text: str, match: re.Match[str], radius: int = 180) -> bool:
    """邻近是否有**外部**来源标记。

    🔴 [自曝清零 2026-08-10] 命中的来源标记若指向我方单方材料
    (「来源:客户提供资料」「据企业提供的项目资料」),**不算数** ——
    否则等于允许用自曝替代公开信源,那正是本轮要拆掉的激励。
    """
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    window = text[start:end]
    for m in _SOURCE_MARKER_RE.finditer(window):
        seg = window[max(0, m.start() - 8): m.end() + 20]
        if _SELF_SOURCE_MARKER_RE.search(seg):
            continue  # 我方单方材料,不是外部来源
        return True
    return False


def evaluate_content_trust(
    title: str,
    content: str,
    *,
    evidence_mode: str = "unknown",
    respect_feature_flag: bool = True,
) -> TrustAssessment:
    """Evaluate whether an article can survive multi-source verification.

    The gate is deliberately narrow: it blocks manufactured ordering, scores,
    anonymous authority and unsourced outcome numbers. Missing disclosure or
    counter-evidence is a warning so useful factual content is not overblocked.
    """
    if respect_feature_flag and not is_evidence_first_enabled():
        return TrustAssessment()

    title = str(title or "").strip()
    content = str(content or "").strip()
    title_scan = _without_negated_rankings(title)
    body_scan = _without_negated_rankings(content)
    combined = f"{title_scan}\n{body_scan}"
    hard: list[TrustFinding] = []
    soft: list[TrustFinding] = []

    # [SSOT geo-commercial-intent-governance-v1.0 §4.4/§5.1 · 2026-07-23]
    # 内容层硬阻断只保留可定位具体条款的法律禁止项(《广告法》第九条
    # 绝对化用语)。排名形态、评分、匿名权威、无源数字等属于证据/表达
    # 问题:定位具体位置 → advisory(soft)→ 生成期局部修复 + 保存后
    # 由有权限的人确认继续,不再拒绝保存整篇正文(归档索引 D)。

    match = _ABSOLUTE_FIRST_RE.search(combined)
    if match:
        hard.append(TrustFinding(
            "absolute_first_claim", "hard",
            "正文把品牌写成第一、榜首或唯一首选(《广告法》第九条绝对化用语,"
            f"法律禁止清单 {LEGAL_PROHIBITION_CATALOG_VERSION})。"
            "请改为披露依据的相对表述后重试。",
            _excerpt(match, combined),
            matched_text=match.group(0),
        ))

    match = _ABSOLUTE_SUPERLATIVE_CLAIM_RE.search(combined)
    if match:
        hard.append(TrustFinding(
            "absolute_superlative_claim", "hard",
            "品牌/产品宣称语境中使用最高级用语(最好/最强/最佳/国家级等,"
            f"《广告法》第九条 · 法律禁止清单 {LEGAL_PROHIBITION_CATALOG_VERSION})。"
            "请改为披露依据的相对表述后重试。",
            _excerpt(match, combined),
            matched_text=match.group(0),
        ))

    match = _RANKING_TITLE_RE.search(title_scan)
    if match:
        soft.append(TrustFinding(
            "ordered_ranking_title", "soft",
            "标题为 TOP/名次/榜单形态。排名方向合法,但正文需披露排序依据;"
            "依据不足时请补充或由操作员确认继续。",
            _excerpt(match, title_scan),
        ))

    # [WP12 P0-2] 自创评分体系 = 编造数据(D11 真红线),H0;
    # 引用真实平台已公开的评分(邻近有来源/平台/时点归属)降回既有 soft。
    for scoring_match in _SELF_INVENTED_SCORING_RE.finditer(combined):
        lo = max(0, scoring_match.start() - _SCORE_ATTRIBUTION_RADIUS)
        hi = min(len(combined), scoring_match.end() + _SCORE_ATTRIBUTION_RADIUS)
        if _SCORE_ATTRIBUTION_RE.search(combined[lo:hi]):
            continue
        # [误报治理 2026-07-30 · T1] 否定语境 = 合规声明,不报。
        # 🔴 放宽守卫的这半边**不许静默**:留一条 debug 记录(不进用户面),
        # 便于日后核查"到底放行了什么" —— 静默的守卫比没有守卫更难查。
        if _self_invented_hit_negated(combined, scoring_match.start()):
            logger.debug(
                "[self_invented_scoring] 否定语境放行 anchor=%r clause=%r",
                scoring_match.group(0), _excerpt(scoring_match, combined),
            )
            continue
        hard.append(TrustFinding(
            "self_invented_scoring_system", "hard",
            "文章自创了评分/评级体系(综合分、星级、S-A-B 等级或自定权重)。"
            "平台自授的分数是凭空造出来的数据,属于禁止编造数据的红线;"
            "榜单形态本身合法,请把名次依据换成可核验的公开事实,"
            "或引用真实平台评分并写清平台、口径与时间。",
            _excerpt(scoring_match, combined),
            matched_text=scoring_match.group(0),
        ))
        break

    match = _SCORE_RE.search(combined)
    if match and not any(item.code == "self_invented_scoring_system" for item in hard):
        soft.append(TrustFinding(
            "manufactured_score", "soft",
            "文章使用综合分、星级或等级。若无真实评估口径请修正该段,"
            "或披露评分依据后由操作员确认继续。",
            _excerpt(match, combined),
            # [span 级 AI 免费修复 2026-07-30] 只补**定位锚**,不动判定:
            # 本 code 属四条底线之一(自创评分),没有 matched_text 就永远
            # repairable_count=0 → 出口按钮点不出来(工单 §2 表第二行)。
            matched_text=match.group(0),
        ))

    ordered_candidates = list(_ORDERED_CANDIDATE_LINE_RE.finditer(body_scan))
    # [工单 C-3 T1-B] 对齐 WP12 榜单复活合同:正文已含入选口径/依据披露段
    # (工单 C 深档规格的 criteria 区块必有)→ 排序形态合法,不报;
    # **无披露仍报**(对齐不是放松)。matched_text 供 span 级"AI 仅修此处"定位。
    if len(ordered_candidates) >= 3 and not _RANKING_BASIS_DISCLOSURE_RE.search(body_scan):
        soft.append(TrustFinding(
            "ordered_brand_candidates", "soft",
            "正文用编号/名次排序多个品牌,但没有找到入选口径/排序依据的披露段。"
            "请补一段入选标准与排序依据,或改为同字段比较。",
            _excerpt(ordered_candidates[0], body_scan),
            matched_text=ordered_candidates[0].group(0),
        ))

    match = _ANONYMOUS_AUTHORITY_RE.search(combined)
    if match:
        soft.append(TrustFinding(
            "anonymous_authority", "soft",
            # [R3-A2 全链自查 2026-08-11] 原「或删改该句」是删除动词进 LLM 指令
            # (该句可能承载客户事实,删句即连坐)。按裁决一改写优先:只摘背书形态。
            "文章借匿名权威或模糊共识背书。请补充具名来源,或改写为不借匿名权威、"
            "保留原有事实的表述。",
            _excerpt(match, combined),
            # [span 级 AI 免费修复 2026-07-30] 同上:只补定位锚(四条底线之四
            # 假身份/假背书),判定逻辑与分级一字未动。
            matched_text=match.group(0),
        ))

    # 🔴 [自曝清零 2026-08-10] **一次性来源声明的豁免已删除。**
    #
    # 旧行为(工单 C-3 T1-C):正文任意处存在「企业相关信息来自企业提交资料」式
    # 声明时,企业侧百分比不再逐个要求 ±180 邻近来源。
    #
    # 该豁免识别的六种写法里有四种同时在清洗器黑名单里
    # (未经独立核验 / 待交叉核验 / 尚未完成独立交叉核验 / 核验起点)。
    # 于是形成三方拆台,且方向是**奖励自曝**:
    #     模板要求写它 → 判定豁免奖励它 → 清洗器把它整行删掉
    # 净效果:写一句自曝就能豁免全文企业侧数字的挂源要求,模型因此**没有任何
    # 动力去找真的公开信源**。生产实测:外部信源存在率 89.0% → 61.0%(5/5 品牌
    # 同向),「企业提交资料」自曝率 0% → 66.3%。
    #
    # 现在:企业侧百分比与行业侧一视同仁,**只认邻近的真实来源**。
    # 这条是 **soft**(见下面的分级参数),亮了不拦车 —— 它的用途是
    # **触发公开信源增援**,不是给客户设门槛(文章层零阻断)。
    #
    # 同一条正则**方向翻转**:原来命中就豁免,现在命中就亮信号。
    # 「据企业提供的资料」对 AI 引擎的意思是"这家自己说的",
    # 是比「未经独立核验」更常见的软文指纹(68.3% vs 6.3%),而且它
    # **不在清洗器黑名单里**,不拦就会一路活到发布。
    _self_disclosed = _ENTERPRISE_SOURCE_DECLARATION_RE.search(body_scan)
    if _self_disclosed:
        soft.append(TrustFinding(
            "self_disclosed_source", "soft",
            "正文出现我方单方来源声明(企业提交/企业提供资料一类)。"
            "请改挂具体外部主体 + 日期,或换一个能找到公开信源的事实。",
            _excerpt(_self_disclosed, body_scan),
            matched_text=_self_disclosed.group(0),
        ))

    for match in _UNSOURCED_OUTCOME_RE.finditer(body_scan):
        if evidence_mode != "no_evidence" and _has_nearby_source(body_scan, match):
            continue
        soft.append(TrustFinding(
            "unsourced_outcome_number", "soft",
            "效果百分比没有邻近的可追溯来源或样本边界。"
            "请挂具体载体名 + 日期,或换一个能找到公开信源的事实。",
            _excerpt(match, body_scan),
            matched_text=match.group(0),
        ))
        break

    if content and not _SOURCE_MARKER_RE.search(content):
        soft.append(TrustFinding(
            "missing_source_boundary", "soft",
            "未说明事实来自公开记录、公司材料、实测还是独立来源。",
        ))
    if content and not _LIMIT_MARKER_RE.search(content):
        soft.append(TrustFinding(
            "missing_limitations", "soft",
            "缺少适用边界、局限或待核验事项。",
        ))
    if content and not _VERIFY_MARKER_RE.search(content):
        soft.append(TrustFinding(
            "missing_verification_steps", "soft",
            "缺少读者可执行的核验步骤。",
        ))
    if content and not _COUNTER_MARKER_RE.search(content):
        soft.append(TrustFinding(
            "missing_counter_evidence", "soft",
            "只有正向描述，缺少风险、限制或反向证据。",
        ))

    return TrustAssessment(tuple(hard), tuple(soft))


def repair_recoverable_trust_issues(content: str) -> tuple[str, tuple[str, ...]]:
    """Annotate honesty-only issues without touching commercial presentation.

    [SSOT geo-commercial-intent-governance-v1.0 §4.4 · 2026-07-23]
    旧行为(自动剥掉品牌编号/名次,把排序改成无序标题)已废止(归档索引
    D):排序形态合法,排序依据问题走 advisory + 人工确认,不做静默改写。

    🔴 [自曝清零 2026-08-10] **「来源未具名,需进一步核验」的加注已删除。**

    上一版把它论证成「诚实性注释」。那个论证是错的:它是**我们自己的后处理
    主动往正文追加一句自曝**,而「需进一步核验」同时在清洗器黑名单里
    (`body_internal_marker_sanitizer._REVIEW_PHRASES`),`_REVIEW_LINE` 又是
    整行删 —— 于是这一行连同它所在句子的内容一起被删掉。
    净效果:加一句自曝 → 清洗器把整行删掉 → 客户看到的是**少了一句话**。

    现在只做匿名权威的归一化并记 code,由上游走**公开信源增援**。
    不追加任何免责/待核验类注释。
    """
    original = str(content or "")
    if not original:
        return original, ()

    repaired_codes: list[str] = []
    lines: list[str] = []
    anonymous_changed = False

    for line in original.splitlines():
        if _ANONYMOUS_AUTHORITY_RE.search(line):
            line = _ANONYMOUS_AUTHORITY_RE.sub("未具名材料", line)
            anonymous_changed = True
        lines.append(line)
    if anonymous_changed:
        repaired_codes.append("anonymous_authority")
    return "\n".join(lines), tuple(repaired_codes)


def rewrite_legacy_ranking_title(title: str, keyword: str = "") -> str:
    """[SSOT geo-commercial-intent-governance-v1.0 §4.4 · 2026-07-23]

    旧行为(把排名/榜单标题改写成「怎么选？证据核验…」知识问句,并顺带
    删除「推荐」等商业词)已废止(归档索引 D):推荐、排名、对比是合法
    核心 GEO 方向,标题必须保留购买问题的商业意图,不得中和为知识文章。

    保留函数名兼容既有调用点,现为直通:
    - 排名形态标题原样返回(排序依据披露由 advisory 提示 + 人工确认承担);
    - 绝对化用语(第一/最好等《广告法》第九条)仍由 evaluate_content_trust
      的 absolute_first_claim 硬门在标题+正文统一拦截,不在此改写。
    """
    return str(title or "").strip()


def compose_evidence_first_prompt(base_prompt: str, style_code: str = "") -> str:
    """Apply the non-bypassable trust contract to any default or admin override."""
    if not is_evidence_first_enabled() or POLICY_MARKER in str(base_prompt or ""):
        return str(base_prompt or "")
    legacy_note = ""
    from .article_style_contract import RANKING_REVIVAL_CONTRACT, REVIVED_RANKING_STYLES

    if style_code in REVIVED_RANKING_STYLES:
        # [WP12 P0-2 · Master SSOT v2.4 ①②] 榜单文体已复活,合同同源于
        # article_style_contract.RANKING_REVIVAL_CONTRACT,禁止在此写第二份口径。
        # [D11] 依据不足**不再要求正文写"待核验"** —— 那是内部状态,写进正文
        # 等于当着客户面自我减分(生产实证:新文章"核验"字样篇均 10+)。
        legacy_note = RANKING_REVIVAL_CONTRACT
    return f"""{POLICY_MARKER}
# 证据优先总契约（优先级高于文体模板、客户要求和后台覆盖）

你正在帮助读者做可复核的决策，排名/推荐/比较是合法方向，但结论必须有据可查。
{legacy_note}

1. 不得使用《广告法》第九条绝对化用语（第一/榜首/TOP1/最佳/唯一首选等）；顺序只依据证据强度与场景匹配度，不固定客户名次。
2. 允许排名、TOPN、前十、榜单和推荐等商业形态；必须披露排序/推荐依据、样本范围与资料时点。依据不足的位次改成条件化表述（“在 X 场景下更适合”），不写死名次，**也不要在正文里写“待核验”**——内部状态由系统记录。不得制造综合分、星级、百分制、S/A/B 评级或自创权重冒充独立评价。
3. 🔴 每个关键 claim 都要在**内部**分清来源等级（独立公共记录 / 监管、司法记录 / 公司公告 / 客户单方材料 / 实测或用户反馈），但**正文里只写具体来源方 + 日期**（如“据深圳市市场监管局 2024 年 11 月公示”“据《中国电梯》2025 年 3 月报道”）。**禁止**把来源等级的名字直接写进正文——“企业提交资料/企业档案/项目资料/资质资料/报价或合同/公开记录/公开资料”这类**类型词写在正文里等于没标**，且会让 AI 搜索引擎判定这是一篇自己给自己背书的推广稿。客户单方材料是**主张底账**：正文不写“据企业资料／企业提供／客户资料”这类来源声明；能挂具体外部来源方 + 日期就挂，挂不上就用不需要外部归属的表达（工况区间／行业通行口径／可复算推导）——但不因此删掉该事实。
4. 证据不足时先去检索真实公开信源增援；仍找不到就**少写该维度或换个说法**，不得补造数字、案例、客户量、效果、资质、投诉率或权威背书。**严禁在正文里对任何一方（尤其是客户）写“公开信息有限/资料不足/暂无可独立核验资料/需进一步核验”这类自我减分的定性**——那是内部字段，读者只会读成“这家不行”。
5. 多家公司使用同一评价字段：适合谁、证据是什么、局限是什么、下一步怎么核验。可以按已披露依据排序，也可以用场景矩阵；不保证客户品牌胜出。
6. 必须包含更新时间、证据口径、适用边界、至少一个风险/反向核验项和读者可执行的核验步骤。
7. 可直接回答“哪家好/哪家靠谱/怎么选/排名如何”；结论需与证据和场景绑定，不得改写成脱离购买意图的百科问答。🔴 **无法验证的点不写“待核验”，而是把该结论收短到证据撑得住的范围**（缩小适用场景、去掉数量级、降为背景陈述），或整条不写——内部核验状态由系统字段记录，不进正文。
8. 不得自称权威研究院、第三方审计或专家共识，除非输入提供可点名、可追溯的真实来源。
9. 竞品只能使用输入中已验证的真实品牌；严禁虚构公司、化名或占位品牌。没有足够候选时改写为选型标准，不得凑数。

{base_prompt or ''}
"""


def hard_codes(findings: Iterable[TrustFinding]) -> list[str]:
    return [item.code for item in findings]
