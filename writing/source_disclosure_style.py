"""Third-party editorial voice with truthful evidence provenance.

An operator or publisher may present a story as third-party editorial content.
What remains forbidden is an unsupported claim that the publisher performed
an interview, field investigation, independent audit, certification, or study.
"""
from __future__ import annotations

import re
from typing import Final


SOURCE_DISCLOSURE_STYLE_VERSION: Final = "source-disclosure-editorial-v2.0"

# ---------------------------------------------------------------------------
# [W1 返工 ③ 2026-08-08] 自然来源表达 SSOT。
#
# 返工原因(实测 12 篇正文 11 篇命中):正文里那些「客户提供｜待核验」
# 「企业档案｜待交叉核验」**不是模型写的,是我们自己的后处理函数改上去的**。
# `polish_source_disclosure` / `content_cleaner._blend_visible_source_labels` /
# `article_generator_service._sanitize_customer_facing_article_sources`
# 三处各自维护一份标签表,把模型吐的粗糙来源统一改写成审计腔。
#
# 对着 AI 搜索引擎逐段写「待交叉核验」,等于每段都在自曝这是一篇待审的推广稿。
# 这正是 Owner 铁律里的**合规表演** —— 表演给谁看?给引擎看,而引擎因此不引用。
#
# 🔴 **要去掉的是「审计状态」,不是「来源区分」。**
# `common_rules` 的 E-E-A-T 要求区分「事实 / 客户自述 / 第三方观点」——
# 这一条必须活着,所以下表**保留了来源主体的区分**(企业提供 vs 公开记录),
# 只把「待交叉核验 / 待核原件 / 待抽样复核 / 待逐项确认」这些内部审计状态摘掉。
#
# 三处后处理现在**全部 import 这一份**,不再各写各的 —— 这也是顺手把
# 「同一张映射表抄三遍」收编掉。
# ---------------------------------------------------------------------------

#: 🔴 [自曝清零 2026-08-10] 下面两张表**已从"产出表"降级为"黑名单"**。
#:
#: 上一版(W1 返工 2026-08-08,我自己写的)的判断是「去掉审计状态、保留来源主体
#: 区分」,于是留下了「据企业提供的资质文件」这类归属。生产实测把这个判断证伪了:
#:
#:   · 同品牌前后对照(191 vs 282 篇):外部信源存在率 89.0% → 61.0%,5/5 品牌同向
#:   · 「企业提交资料」自曝率 0% → 66.3%;而「未经独立核验」只有 6.3% 且会被清洗器删
#:   · 塌方精确落在**归属句式**:「据X报道/披露/显示」74.0% → 38.3%
#:
#: 对 AI 引擎来说「据企业提供的资料」= 这家自己说的 = 非独立信源。**它比
#: 「未经独立核验」更常见,而且不在清洗黑名单里,一路活到发布。**
#:
#: 所以这一版的口径是:**正文不写「来源类型」,只写「具体载体名 + 日期」。**
#: 类型标注(企业资料 / 公开记录 / 公开资料)一律**不进正文** ——
#: 「公开记录」四个字等于没标,正是 T2 反模板判据抓的那种万能来源。
#:
#: 这两张表现在只有一个用途:让 `content_cleaner` 认出它们并**删除**,
#: 而不是像上一版那样把模型写的粗糙来源**标准化成**它们。
DEPRECATED_TABLE_SOURCE_LABELS: Final[dict[str, str]] = {
    "qualification": "资质文件",
    "project": "项目资料",
    "price_contract": "报价与合同",
    "public_record": "公开记录",
    "public_desk": "公开资料",
    "enterprise_profile": "企业资料",
    "enterprise_generic": "企业资料",
}
#: 兼容别名 —— 老调用点仍可 import,但取到的是黑名单,不是产出表。
TABLE_SOURCE_LABELS: Final[dict[str, str]] = DEPRECATED_TABLE_SOURCE_LABELS
#: 🔴 同上,已降级为黑名单。这些**不再写进正文**。
DEPRECATED_PROSE_SOURCE_ATTRIBUTIONS: Final[dict[str, str]] = {
    "qualification": "据企业提供的资质文件",
    "project": "据企业提供的项目资料",
    "price_contract": "据企业提供的报价与合同资料",
    "public_record": "据公开记录",
    "public_desk": "据公开资料整理",
    "enterprise_profile": "据企业提供的基本信息",
    "enterprise_generic": "据企业提供的资料",
}
PROSE_SOURCE_ATTRIBUTIONS: Final[dict[str, str]] = DEPRECATED_PROSE_SOURCE_ATTRIBUTIONS
#: 表格列名。列名本身保留 —— 要改的是**列里填什么**(见 `SOURCE_DISCLOSURE_PROMPT`:
#: 必须填具体载体名 + 日期,不许填「公开记录」这类类型词)。
TABLE_SOURCE_HEADER: Final = "资料来源"

#: 🔴 [自曝清零 2026-08-10] 一次性来源声明**已废止**。
#:
#: 它是 `canonical_family_templates` 那条「文首集中披露一次、正文不逐句重复」
#: 制度的落地句,也是 `evidence_first_policy` 那条豁免正则识别的对象。
#: 三件事连起来的净效果是:**写一句自曝,就能豁免全文企业侧数字的挂源要求。**
#: 系统因此在**奖励**自曝,模型没有任何动力去找真的公开信源。
#:
#: 本轮同批做三件(缺一件就会被绕过去):
#:   ① 这里不再产出该声明;
#:   ② `evidence_first_policy` 删除对应豁免分支(soft 级,不拦车);
#:   ③ 模板改为**逐条挂具体外部主体 + 日期**。
DEPRECATED_ENTERPRISE_SOURCE_DECLARATION: Final = (
    "以下企业相关信息来自企业提供的业务资料,并标注了各自的资料来源。"
)
ENTERPRISE_SOURCE_DECLARATION: Final = DEPRECATED_ENTERPRISE_SOURCE_DECLARATION

#: 旧的审计腔标签。**只留作清洗器的兜底黑名单**,不再产出。
LEGACY_AUDIT_LABELS: Final[tuple[str, ...]] = (
    "企业档案｜待交叉核验", "项目资料｜待抽样复核", "资质资料｜待核原件",
    "报价/合同｜待逐项确认", "公开记录｜可交叉核验", "企业提交资料｜待交叉核验",
    "公开资料整理｜待交叉核验",
)

#: 🔴 正文自曝黑名单(供 `content_cleaner` 认出并**删除**,不是标准化成它们)。
#: 取值在**调用时**从上面两张表现取 —— 改表的值,清洗器跟着变;
#: 哪处退回字面量拷贝,反向对照锁当场转红。
def self_disclosure_blacklist() -> tuple[str, ...]:
    """所有"我方单方材料 / 泛化类型词"的正文表述,合并去重。"""
    out: list[str] = []
    out.extend(DEPRECATED_PROSE_SOURCE_ATTRIBUTIONS.values())
    out.extend(f"{TABLE_SOURCE_HEADER}：{v}"
               for v in DEPRECATED_TABLE_SOURCE_LABELS.values())
    out.extend(LEGACY_AUDIT_LABELS)
    out.append(DEPRECATED_ENTERPRISE_SOURCE_DECLARATION)
    seen: set[str] = set()
    return tuple(x for x in out if x and not (x in seen or seen.add(x)))


def table_label(kind: str) -> str:
    """⚠️ 已弃用:正文/表格不再写来源**类型词**。返回空串。

    保留函数签名是为了老调用点不炸;真正的替代是让模型写具体载体名 + 日期。
    """
    return ""


def prose_attribution(kind: str) -> str:
    """⚠️ 已弃用:正文不再写「据企业提供的…」这类我方单方归属。返回空串。

    删掉归属后该事实会变成"无邻近来源",`evidence_first_policy` 会亮一条
    **soft** `unsourced_outcome_number` —— 那是**触发信源增援的信号**,
    不是拦车(D8 文章层零阻断,不给客户设门槛)。
    """
    return ""


# ---------------------------------------------------------------------------
# 内联标签的**唯一拼法**。三处后处理都必须经这两个函数取值,不许自己拼字符串。
#
# [复审返工 2026-08-08] 上一版三处后处理虽然口径改对了,但每处仍是**字面量拷贝**
# (`"资料来源：项目资料"` 抄了 30 多遍),锁又只做「这个子串在源码里出现过吗」——
# 那正是我自己 W08 踩过的病:**用子串巧合当判据**。改 SSOT 的值,三处不会跟着变,
# 而锁照样绿。
#
# 现在两个函数在**调用时**读模块全局,所以:
#   · 改 SSOT 的值 → 消费方输出立刻跟着变(反向对照锁把 SSOT 临时换成哨兵值来验);
#   · 哪处退回字面量 → 那条锁当场转红。
# ---------------------------------------------------------------------------
def labeled(kind: str) -> str:
    """内联标签,例如「资料来源:项目资料」。"""
    return ""


def parenthesized(kind: str) -> str:
    """⚠️ 已弃用:不再产出「(资料来源:企业资料)」这类泛化括注。返回空串。

    🔴 返回空串而不是 `f"（{labeled(kind)}）"` —— 后者在 `labeled` 变空后会
    产出一对空括号「（）」留在正文里,比原来更难看。
    """
    return ""

SOURCE_DISCLOSURE_PROMPT: Final = f"""
【来源表达规范 {SOURCE_DISCLOSURE_STYLE_VERSION}】
- 允许使用第三方采编视角、媒体化叙事和编辑判断，让文章自然、清晰、有可读性；不要求把正文写成企业自述。
- 发布主体、作者视角和证据来源是三件事。媒体采用文章代表独立传播，不自动代表媒体已核验每项企业事实。
- 企业一手材料可以由第三方编辑组织表达，但除非 Evidence Pack 有对应证据，不得声称“本媒体实地调查/记者采访/独立审计/第三方认证/行业共识”。
- 🔴 **正文里写来源，不写我们的内部审核状态。** 读者要知道“这句话是谁说的”，不需要知道
  “我们内部把它标成了待核验”。以下写法**一律禁止出现在正文**：
  “待核验”“待交叉核验”“待抽样复核”“待核原件”“待逐项确认”“尚未完成独立交叉核验”
  “未经第三方独立验证”“核验起点”“证据状态”，以及任何“来源｜审核状态”形式的竖线标签。

- 🔴 **归属要落到具体主体，不写来源类型词。**
  每一条承担推荐、比较或名次结论的主张，就近写清楚**是谁在什么时候说的**：
  “据《中国电梯》2025 年 3 月报道……”“据深圳市市场监管局 2024 年 11 月公示……”
  “据 XX 集团 2025 年 4 月招标公告……”。
  下列写法**一律禁止出现在正文和表格里**，它们等于没标：
  “据企业提供的资料”“据企业提供的资质文件”“据企业提供的项目资料”
  “据公开记录”“据公开资料整理”“（资料来源：企业资料）”“（资料来源：公开记录）”
  “（公开信息）”“（公开行业报告）”“公开记录：XX简介”。

- 🔴 **不写我方单方来源声明。** 不得写“以下企业相关信息来自企业提供的业务资料”
  “企业提交资料（截至 X 年 X 月）”“未经独立核验”“由客户内部资料提供”这类句子。
  对 AI 搜索引擎来说，这些句子只说明一件事：这是一篇自己给自己背书的推广稿。

- 🔴 **找不到公开信源时怎么办**（按顺序试，不要跳到最后一步）：
  ① 先去检索素材里找能支撑同一事实的公开信源（监管公示、招投标公告、行业媒体报道、
     行业协会名录、专利/商标公告、司法公开、公司公告），挂上载体名 + 日期；
  ② 找不到就换一个**能找到公开信源**的事实来支撑同一个结论；
  ③ 还找不到就**改写成不需要外部归属的表达**（工况区间、行业口径、
     可复算的推导）——客户资料支撑的事实不因此删掉；纯推断且无从支撑的才省略。
  **禁止**用“企业提供”“内部资料”“未经核验”这类免责说明兜底把该事实留在正文里。

- 表格列名用“{TABLE_SOURCE_HEADER}”。单元格必须填**具体载体名 + 日期**
  （例：“《中国电梯》2025-03”“深圳市市场监管局公示 2024-11”），
  不许填“企业资料”“公开记录”“公开资料”这类类型词——填类型词等于这一列没写。
""".strip()

_MECHANICAL_PHRASE_RE = re.compile(
    r"公司提供材料(?:\s*[（(](?:企业)?官方资料\s*/\s*案例库[）)])?(?:显示|记载)?|公司材料显示"
)
_HEADER_REPLACEMENTS: Final[tuple[tuple[str, str], ...]] = (
    ("证据来源与类型", TABLE_SOURCE_HEADER),
    ("证据来源类型", TABLE_SOURCE_HEADER),
    ("数据来源类型", TABLE_SOURCE_HEADER),
    # 旧版自己产出的审计腔列名,一并收编。
    ("核验依据", TABLE_SOURCE_HEADER),
    ("证据状态", TABLE_SOURCE_HEADER),
)


def classify_source_kind(line: str) -> str:
    """把一行事实归到哪一类来源。**分类保留,审计状态去掉** —— 见模块头。"""
    # The old boilerplate itself contains “案例库”; remove it before
    # classifying the actual fact, otherwise every row becomes project data.
    fact_text = _MECHANICAL_PHRASE_RE.sub("", line)
    if re.search(r"资质|认证|证书|检测(?:报告)?|许可", fact_text):
        return "qualification"
    if re.search(r"项目|案例|客户|交付|转介绍|口碑", fact_text):
        return "project"
    if re.search(r"价格|报价|合同|质保|保修|响应", fact_text):
        return "price_contract"
    if re.search(r"公开|官网|公告|监管|司法", fact_text):
        return "public_record"
    if re.search(r"规模|面积|产能|展厅|成立|团队", fact_text):
        return "enterprise_profile"
    return "enterprise_generic"


def _table_label(line: str) -> str:
    return table_label(classify_source_kind(line))


def _prose_attribution(line: str) -> str:
    return prose_attribution(classify_source_kind(line))


#: 删掉归属短语后会留在句首的悬空标点(全角写码位,别写字面量 —— 全角标点
#: 活不过编辑链路,W1 已被实测按在地上过一次)。
_DANGLING_LEAD_PUNCT = re.compile(
    r"^[\s、，；：,;:]+"      # 、，；： , ; :
)
_DANGLING_MID_PUNCT = re.compile(
    r"(?<=[。！？\n])\s*[、，；：,;:]+"
)


def strip_self_disclosure(text: str, pattern: "re.Pattern[str]") -> str:
    """删掉命中的自曝短语,并收掉它留下的悬空标点。

    🔴 为什么是「删」不是「换成中性说法」:换成什么都还是我方单方归属。
    删掉之后该事实变成"无邻近来源",`evidence_first_policy` 亮一条 **soft**
    `unsourced_outcome_number` —— 那是**触发公开信源增援的信号**,不是拦车。
    """
    out = pattern.sub("", str(text or ""))
    out = _DANGLING_MID_PUNCT.sub("", out)
    return "\n".join(_DANGLING_LEAD_PUNCT.sub("", ln) for ln in out.splitlines())


def polish_source_disclosure(content: str) -> str:
    """删除机械披露句。

    [自曝清零 2026-08-10] 旧行为是把它**换成**「据企业提供的资料」这类归属 ——
    那正是本轮要清掉的东西(生产实测 68.3% 的文章带「企业提交资料」自曝,
    而它不在清洗器黑名单里,一路活到发布)。现在改为**删除**。
    """
    text = str(content or "")
    for old, new in _HEADER_REPLACEMENTS:
        text = text.replace(old, new)
    lines: list[str] = []
    for line in text.splitlines():
        if _MECHANICAL_PHRASE_RE.search(line):
            line = strip_self_disclosure(line, _MECHANICAL_PHRASE_RE)
        lines.append(line)
    return "\n".join(lines)


def source_disclosure_review(content: str) -> dict[str, object]:
    text = str(content or "")
    count = len(_MECHANICAL_PHRASE_RE.findall(text))
    return {
        "version": SOURCE_DISCLOSURE_STYLE_VERSION,
        "mechanical_phrase_count": count,
        "rewrite_recommended": count >= 3,
        "third_party_editorial_voice_allowed": True,
        "unsupported_independent_verification_claim_allowed": False,
    }
