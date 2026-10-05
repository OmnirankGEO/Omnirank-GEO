"""Gate-2 改写措施 SSOT(K1-K5 / F1-F5 去重合并后的统一措施集)。

## 这一包在解决什么

Gate-2 调研的定论(`GATE2_DELIVERY_2026-08-09.md` §1.5):高低分组的差距**不是文体**,
而是四格分解里的一格 —— **「引了不提」**:AI 引了我们的稿,读完仍然不提这个客户。

    D13a「引了不提」率(已扣除检测假阴性,2026-08-09 实测)
      皓琪 0.0%(n=6) · 岱林 26.1%(n=46) · 富士 77.4%(n=292) · QZQZ 88.0%(n=75)

Owner 2026-08-09 裁定:**不做长期对照实验,先开火边打边测**。
所以本模块把两份改写方案**全量**注入写作链,强弱依据的措施都上,不分组、不留对照;
弱依据措施在落库时单独打 `weak` 标签("安针"),供事后归因。

## 🔴 三条边界(工单 §6 红线,写在这里免得下一个人踩)

1. **不写规则表**(红线 1)。本模块给模型的是**写作指令**,不是"什么内容 AI 会喜欢"的词表。
   产物判据只判**有没有这种成分**(结构性),不判内容好坏 —— 好坏由 D13a 滚动读数说了算。
   元判据 `validate_gate2_measures()` 会挡住往 instruction 里塞枚举表。
2. **不碰投放侧**(红线 4)。这里全部是正文与标题的写法,不改篇数/选媒体/采购。
3. **广告法第九条保留**,其余合规表演不做 —— 措施里没有任何"免责声明""审核状态"类要求。

## 🔴 已经死掉的两个假设(别复活)

* **H1**「稿里缺可摘录的品牌事实句」—— **方向相反**:QZQZ 品牌名密度 13.9 次/篇(五家第一)、
  可摘录短句 2.13/篇(第一);高分组的岱林 1.2 次/篇、20 篇里 5 篇一次都没提,却 D5=59.5%。
* **H2**「品牌名只落在联系方式区块被剥掉」—— 实测含占位符 0 篇。

所以本措施集**不再追求"多提几次品牌名"**(那个已经最高了),追求的是**形态**:
把散落在通用知识里的品牌信息,组织成 AI 能整条搬进"供应商名录"的**字段块**。
这个形态依据只有 4 条定性样本(§2.5),强度标 `medium`,**不是既成事实**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Final

#: 措施集版本。落进 `generation_request_snapshot.gate2_measures.version`。
GATE2_MEASURE_SET_VERSION: Final = "gate2-rewrite-v1.0"

#: 证据强度三档。`weak` 的措施照样进写作链(Owner:强弱都上),
#: 但**单独打标**,归因时可以把它们摘出来单独看 —— 这就是"安针"。
STRENGTHS: Final = ("strong", "medium", "weak")


@dataclass(frozen=True)
class Gate2Measure:
    code: str
    name: str
    #: 对应交付单里的原始编号(K=QZQZ 方案 / F=富士方案)。保留是为了可回溯到证据段落。
    source_measures: tuple[str, ...]
    evidence: str
    #: 注入 prompt 的指令。`{brand}` 会被替换成客户全称。
    instruction: str
    #: 有没有确定性的产物判据。没有的必须写明为什么 —— 见 `validate_gate2_measures`。
    product_checkable: bool
    why_not_checkable: str = ""


MEASURES: Final[tuple[Gate2Measure, ...]] = (
    Gate2Measure(
        code="G1",
        name="客户档案区块",
        source_measures=("K1", "F1"),
        evidence="medium",
        instruction=(
            "正文里必须有一个**客户档案区块**,用「字段:值」逐行写,至少 4 行。"
            "字段按这家客户真实有的信息选(经营年限 / 服务区域 / 资质 / 可承接规格范围 / 代表场景 等),"
            "**没有的字段就不写,不要编**。这一块要能被整段读懂,不要把这些信息拆散混进正文段落里。"
        ),
        product_checkable=True,
    ),
    Gate2Measure(
        code="G2",
        name="适用边界点名到客户",
        source_measures=("K2",),
        evidence="medium",
        instruction=(
            "写「适合谁 / 不适合谁」时,**主语必须是 {brand} 本身**,"
            "不要写成不点名的通用选择建议。不适合的场景要照实写,不回避。"
        ),
        product_checkable=True,
    ),
    Gate2Measure(
        code="G3",
        name="地域与客户绑定成句",
        source_measures=("K3",),
        evidence="medium",
        instruction=(
            "地域信息不要只放在标题里。正文中至少有一句把**地域与 {brand} 写在同一句**"
            "(例如「在某地做某事的 {brand}」),让这句话单独拎出来也读得通。"
        ),
        product_checkable=True,
    ),
    Gate2Measure(
        code="G4",
        name="标题年份",
        source_measures=("K4",),
        evidence="strong",
        instruction=(
            "标题按现行标题元素合同处理年份,不要主动删掉年份。"
        ),
        product_checkable=True,
    ),
    Gate2Measure(
        code="G5",
        name="客户 + 具体工况",
        source_measures=("F3",),
        evidence="medium",
        instruction=(
            "至少写一处 **{brand} + 具体工况**的可核验陈述(什么条件、什么规格、什么场景),"
            "**不要写「服务过众多客户」这类没有主语也没有数值的泛化说法**。"
        ),
        product_checkable=True,
    ),
    Gate2Measure(
        code="G6",
        name="对比区块含客户",
        source_measures=("F4",),
        evidence="medium",
        instruction=(
            "做同口径对比时,**{brand} 自己必须在对比里出现**,与其他对象同一套字段、同一个口径;"
            "不要只做行业科普然后在结尾提一句。"
        ),
        product_checkable=True,
    ),
    Gate2Measure(
        code="G7",
        name="品类口径按客户实际经营",
        source_measures=("F2",),
        evidence="medium",
        instruction=(
            "品类词按 {brand} **实际经营的那个品类**的行业口径写,"
            "不要套用相邻行业的说法(相邻行业的词读者认得,但它描述的不是这家客户在做的事)。"
        ),
        product_checkable=False,
        why_not_checkable=(
            "确定性判据要么需要一张「哪些词属于哪个行业」的词表(红线 1 禁),"
            "要么需要在保存链上再调一次 LLM(多一个失败点和一笔钱)。"
            "本条只注入指令 + 打标,交 D13a 滚动读数归因。"
        ),
    ),
    Gate2Measure(
        code="G8",
        name="不主动给价格数值",
        source_measures=("K5", "F5"),
        evidence="weak",
        instruction=(
            "不要主动给出具体价格数值或价格区间;价格相关的话写成影响价格的因素与取舍。"
        ),
        product_checkable=False,
        why_not_checkable=(
            "🔴 依据弱(Q2 分层后 0/11 vs 5/20,不显著)。**只挂实验标签,不进方案依据**。"
            "为它写产物判据等于把一个未证实的方向固化成硬约束。"
        ),
    ),
    Gate2Measure(
        code="G9",
        name="结尾不写联系邀约",
        source_measures=("K5", "F5"),
        evidence="weak",
        instruction=(
            "结尾不要写「联系我们了解更多」「留下需求为您报价」这类邀约句,"
            "把结尾留给读者自己能用的判断依据。"
        ),
        product_checkable=False,
        why_not_checkable=(
            "🔴 依据弱(同 G8)。判据需要一张邀约句式表,属红线 1 禁止的形态。"
        ),
    ),
)

MEASURES_BY_CODE: Final[dict[str, Gate2Measure]] = {m.code: m for m in MEASURES}
WEAK_CODES: Final[tuple[str, ...]] = tuple(m.code for m in MEASURES if m.evidence == "weak")


# ---------------------------------------------------------------------------
# prompt 注入
# ---------------------------------------------------------------------------

_BLOCK_TITLE: Final = "【Gate-2 改写措施 " + GATE2_MEASURE_SET_VERSION + "】"


def build_gate2_measure_block(
    brand_name: str | None, *, exempt_codes: tuple[str, ...] = (),
) -> str:
    """渲染注入 system prompt 的措施块。拿不到品牌名就返回空串(不阻断出稿)。

    只做字符串拼装,**不调任何模型、不查库** —— 生成链上多一个 IO 就是多一个失败点。

    🔴 [返修 C8/C14 2026-08-11] `exempt_codes`:按篇豁免弱依据措施,消除与
    规格/功能的正面互打 ——
      · G8(不主动给价格)让位于价格区块规格:price_roi 文体、含 pricing
        区块的深档、紧凑档 body3 与 G8 必然同现,不豁免则价格块退化成
        「价格受多种因素影响」空话(G8 自标依据弱 0/11 不显著,价格采纳
        76.8% vs 68.5% 是正信号);
      · G9(结尾禁邀约)让位于客户显式开启的联系方式插入:否则
        `[NEED_CONTACT]` 占位永不输出,**客户付费开启的功能静默失效**
        (prompt 版「接线没接」)。豁免时允许文末一处占位引导。
    豁免只跳过注入,不删措施定义(D13a 归因读数照旧)。
    """
    brand = str(brand_name or "").strip()
    if not brand:
        return ""
    exempt = {str(c).strip() for c in (exempt_codes or ()) if str(c).strip()}
    lines = [
        _BLOCK_TITLE,
        "下面每条都是写法要求。目标是让这篇被 AI 引用后,"
        "客户的信息能被整条搬进答案,而不是只留下通用知识。",
    ]
    seq = 0
    for m in MEASURES:
        if m.code in exempt:
            continue
        seq += 1
        lines.append(f"{seq}. [{m.code} {m.name}] " + m.instruction.replace("{brand}", brand))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 产物判据(只判结构性成分,不判内容好坏)
# ---------------------------------------------------------------------------

#: 「字段:值」行。全角半角冒号都认;字段名不超过 12 字,值不为空。
#: 🔴 排除行首 `#` —— `### 客户档案:某某` 是小标题不是字段行,算进去会把一个
#: 孤零零的标题当成"一行字段",把真正的字段块切断(真跑时逮到的)。
_FIELD_LINE = re.compile(r"^\s*(?!#)[-*+]?\s*\**([^\s:：*#][^:：*]{0,11})\**\s*[:：]\s*\S")
#: 两列表格的数据行:`| 字段 | 值 |`。分隔行(`|---|---|`)不算。
_TABLE_ROW = re.compile(r"^\s*\|(?P<a>[^|]+)\|(?P<b>[^|]+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
#: 行政区划后缀。这是**构词法**(后缀),不是地名表 —— 表才是红线 1 禁的东西。
_ADMIN_SUFFIX = re.compile(r"(省|市|自治区|自治州|地区|县|区|镇|街道)")
_HEADING = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$")
_DIGIT = re.compile(r"[0-9]")
_YEAR = re.compile(r"20[0-9]{2}")
#: 「适用边界」类小标题的判定词。取自规格卡合同里已有的栏目名,**不新造一张表**。
_BOUNDARY_HINT: Final = ("适用", "适合", "边界", "不适")

# ---------------------------------------------------------------------------
# 🔴 [判据废话审计 2026-08-10] 下面三个常量是 G2/G5/G6 收口用的。
#
# 审计方法:每个「必须命中」配一个「必须不命中」,**反例专挑该判据 instruction
# 自己明令禁止的写法**。六个判据审出:
#   G5 废话 —— `any(brand in s and _DIGIT.search(s))`,品牌名 + 任意阿拉伯数字即过。
#              其 instruction 明禁「服务过众多客户」,而
#              「XX服务过众多客户,成立于2015年」**判通过**(2015 是数字)。
#   G6 废话 —— 标题分支把「## 同口径对比」整节吞掉,客户只在**节末散文里提一句**
#              也算过 —— 那正是它 instruction 禁止的「只做行业科普然后结尾提一句」。
#   G2 过窄 —— 只认**专门小标题**,行文内写的边界句一律漏判 → 25% 基线是低估。
#
# ⚠️ 这三个 checker **在生产里没有调用点**(只有探针与测试用),所以修它们
#    不改变文章,只让度量诚实。别把它当成内容改进。
# ---------------------------------------------------------------------------

#: 带单位的数值 —— 「32 台」「24 米」「1000kg」「12 层」算;裸年份「2015 年」不算。
#: G5 要的是**工况**(什么条件、什么规格、什么场景),不是"句子里有数字"。
_SPEC_NUMBER = re.compile(
    r"\d+(?:\.\d+)?\s*"
    r"(?:mm|cm|m|km|kg|t|吨|米|毫米|厘米|公里|平方米|㎡|层|台|套|个|人|"
    r"小时|天|周|月|年质保|年保修|%|％|万元|元|度|℃|°C|Pa|kW|V|A|Hz)"
)
#: 纯年份表述(成立于 2015 年 / 2015 年),G5 要把它排除掉。
_BARE_YEAR = re.compile(r"(?:成立于\s*)?20[0-9]{2}\s*年(?![质保保修])")
#: 工况语境词 —— 说明这个数值是"什么条件下的"。
_SPEC_CONTEXT: Final = (
    "以内", "以下", "以上", "不超过", "最大", "最小", "范围", "区间",
    "适用", "适合", "条件", "规格", "工况", "场景", "户型", "层高",
    "载重", "提升", "跨度", "面积", "误差", "精度", "周期", "交付",
)
#: 泛化空话 —— 命中即**不算**具体工况(这些正是 instruction 点名禁止的)。
_VAGUE_CLAIM: Final = (
    "众多客户", "各类客户", "多样化需求", "广泛", "丰富经验", "实力雄厚",
    "行业领先", "一站式", "全方位", "满足不同",
)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"[。！？!?\n]", str(text or "")) if s.strip()]


#: 档案块的最少字段数。
_MIN_PROFILE_FIELDS: Final = 4


def _is_profile_row(line: str) -> bool:
    """一行是不是「字段 → 值」。两种形态都算 —— 见下方 docstring。"""
    if _FIELD_LINE.match(line):
        return True
    if _TABLE_SEP.match(line):
        return False
    m = _TABLE_ROW.match(line)
    return bool(m and m.group("a").strip() and m.group("b").strip())


def _check_g1_profile_block(content: str, brand: str) -> bool:
    """连续 ≥4 条「字段 → 值」,且**这一块或它上面那个小标题**里出现客户全称。

    「字段 → 值」认**两种**形态:
      · `- 经营年限:12 年` 这样的字段行;
      · `| 经营年限 | 12 年 |` 这样的**两列表格行**。

    🔴 表格那一种是**真跑实证时加上的**:措施上线后模型把客户档案写成了
    六行两列表格(经营主体 / 服务区域 / 经营品类 / 可承接规格范围 / 资质与依据 / 代表场景),
    形态完全正确,而第一版判据只认字段行,把它判成了 False。
    **判据错、产物没错** —— 本仓第四次"真跑推翻我自己的前提",所以这条记在这里。

    上方小标题算进判定范围,是因为 `### 客户档案:某某` + 表格 是最自然的落地形态,
    表体里未必再写一遍全称。
    """
    lines = str(content or "").splitlines()
    run: list[str] = []
    start = 0
    for idx, ln in enumerate(lines + [""]):
        if _is_profile_row(ln):
            if not run:
                start = idx
            run.append(ln)
            continue
        if _TABLE_SEP.match(ln) and run:
            continue                       # 表头分隔行不中断连续性
        if len(run) >= _MIN_PROFILE_FIELDS:
            scope = list(run)
            # 往上找最近的非空行;是小标题就并进判定范围。
            j = start - 1
            while j >= 0 and (not lines[j].strip() or _TABLE_SEP.match(lines[j])):
                j -= 1
            if j >= 0 and _HEADING.match(lines[j]):
                scope.append(lines[j])
            if brand in "\n".join(scope):
                return True
        run = []
    return False


def _check_g2_boundary_named(content: str, brand: str) -> bool:
    """客户被点名地写出了适用边界。认**两种**形态。

    [判据废话审计 2026-08-10] 旧版只认形态 ①(专门小标题),行文里写的边界句
    一律漏判 —— 反例实测:「{brand}适合 12 层以下商业综合体的观光梯改造;
    不适合超高层住宅项目。」这是**教科书式的合格边界句**,旧版判 False。
    所以 25% 那个产物基线是**低估**,不是真实占比。

    ① 「适用/适合/边界/不适」类小标题下那一段里出现客户全称;
    ② 同一句里同时有 客户全称 + 边界词 + **具体条件**(带单位的数值,
       或"以内/以下/不超过"这类限定词)。

    🔴 形态 ② 必须要求"具体条件",否则「{brand}服务范围广泛,能满足各类
    客户的多样化需求」会判通过 —— 那正是本条 instruction 明令禁止的写法。
    """
    text = str(content or "")
    lines = text.splitlines()
    # 形态 ①:专门小标题
    for i, ln in enumerate(lines):
        m = _HEADING.match(ln)
        if not m or not any(h in m.group(1) for h in _BOUNDARY_HINT):
            continue
        chunk = []
        for nxt in lines[i + 1:]:
            if _HEADING.match(nxt):
                break
            chunk.append(nxt)
        if brand in "\n".join(chunk):
            return True
    # 形态 ②:行文内的边界句
    for s in _sentences(text):
        if brand not in s:
            continue
        if not any(h in s for h in _BOUNDARY_HINT):
            continue
        if any(v in s for v in _VAGUE_CLAIM):
            continue  # 泛化空话不算边界
        if _SPEC_NUMBER.search(s) or any(c in s for c in _SPEC_CONTEXT):
            return True
    return False


def _check_g3_region_bound(content: str, brand: str) -> bool | None:
    """同一句里同时有客户全称与行政区划后缀。

    客户全称本身不含行政区划、正文里也找不到时返回 ``None``(不适用)——
    **报 n/a 比报 False 诚实**:这条对「QZQZ木作美学定制」这种名字本就无解。
    """
    sents = _sentences(content)
    if not any(_ADMIN_SUFFIX.search(s) for s in sents):
        return None
    for s in sents:
        if brand in s and _ADMIN_SUFFIX.search(s.replace(brand, "")):
            return True
    return False


def _check_g4_title_year(title: str) -> bool:
    return bool(_YEAR.search(str(title or "")))


def _check_g5_brand_with_number(content: str, brand: str) -> bool:
    """客户 + **具体工况**的可核验陈述。

    🔴 [判据废话审计 2026-08-10] 旧实现是
    ``any(brand in s and _DIGIT.search(s) for s in _sentences(content))``
    —— 品牌名与**任意阿拉伯数字**同句即 True。于是:

        「QZQZ服务过众多客户,成立于2015年。」→ **判通过**

    而这句正是本条 instruction 明令禁止的
    「不要写『服务过众多客户』这类没有主语也没有数值的泛化说法」。
    判据不但测不出东西,还给自己禁止的反例放行 —— 在 24 篇产物上 100% 通过
    因此**不是文章写得好,是这条规则没有判别力**,该基线作废。

    新口径:同一句里要 客户全称 + **带单位的数值** + 工况语境,且不含泛化空话。
    裸年份(成立于 2015 年)显式排除 —— 它是身份不是工况。
    """
    for s in _sentences(content):
        if brand not in s:
            continue
        if any(v in s for v in _VAGUE_CLAIM):
            continue
        stripped = _BARE_YEAR.sub("", s)   # 先摘掉裸年份再看还剩什么数值
        if not _SPEC_NUMBER.search(stripped):
            continue
        if any(c in s for c in _SPEC_CONTEXT):
            return True
    return False


def _check_g6_brand_in_comparison(content: str, brand: str) -> bool:
    """客户**真的在同口径对比里**,而不是在对比之外被提了一句。

    🔴 [判据废话审计 2026-08-10] 旧实现的第二个分支把「## 同口径对比」
    整节吞掉,只要客户在该节**任意位置**(包括节末那句散文)出现就判 True。
    反例实测:

        ## 同口径对比
        | 厂商 | 提升高度 | 载重 |
        | A公司 | 30m | 800kg |
        | B公司 | 24m | 1000kg |

        结尾提一句{brand}也不错。

    → 旧版**判通过**,而这正是本条 instruction 禁止的
    「不要只做行业科普然后在结尾提一句」。所以 38% 那个产物基线是**高估**。

    新口径:客户必须出现在**表体行或列表项**里(与其他对象同一套字段),
    散文句里提到不算。
    """
    lines = str(content or "").splitlines()

    def _is_row(ln: str) -> bool:
        """表体行(排除分隔行)或列表项。"""
        if _TABLE_SEP.match(ln):
            return False
        if ln.count("|") >= 2:
            return True
        return bool(re.match(r"^\s*(?:[-*+•]|\d+[.、)])\s+", ln))

    # 全文任意表体行/列表项里出现客户即可 —— 那就是"在对比里"。
    if any(_is_row(ln) and brand in ln for ln in lines):
        return True
    # 对比小标题分支:同样只认该节内的**行**,不认散文。
    for i, ln in enumerate(lines):
        m = _HEADING.match(ln)
        if not m or ("对比" not in m.group(1) and "比较" not in m.group(1)):
            continue
        for nxt in lines[i + 1:]:
            if _HEADING.match(nxt):
                break
            if _is_row(nxt) and brand in nxt:
                return True
    return False


#: code -> checker。签名统一 (title, content, brand) -> bool | None。
CHECKERS: Final[dict[str, Callable[[str, str, str], bool | None]]] = {
    "G1": lambda t, c, b: _check_g1_profile_block(c, b),
    "G2": lambda t, c, b: _check_g2_boundary_named(c, b),
    "G3": lambda t, c, b: _check_g3_region_bound(c, b),
    "G4": lambda t, c, b: _check_g4_title_year(t),
    "G5": lambda t, c, b: _check_g5_brand_with_number(c, b),
    "G6": lambda t, c, b: _check_g6_brand_in_comparison(c, b),
}


def observe_gate2_measures(title: str, content: str, brand_name: str | None) -> dict:
    """跑一遍可判的产物判据。**永不抛异常、永不阻断保存**(D8)。

    返回 ``{code: True/False/None}``;``None`` = 不适用或判不了。
    """
    brand = str(brand_name or "").strip()
    out: dict[str, bool | None] = {}
    for code, fn in CHECKERS.items():
        if not brand:
            out[code] = None
            continue
        try:
            out[code] = fn(str(title or ""), str(content or ""), brand)
        except Exception:                                # noqa: BLE001
            out[code] = None
    return out


def build_gate2_measure_tag(
    *, title: str, content: str, brand_name: str | None, injected: bool,
    exempt_codes: tuple[str, ...] = (),
) -> dict:
    """落库用的措施标签。进 `generation_request_snapshot.gate2_measures`。

    Args:
        injected: 本篇的 prompt 里**真的**注入了措施块吗。
            🔴 这一位很重要 —— 没注入却记 applied,就是给归因喂假数据。
        exempt_codes: 本篇被按篇豁免的措施码(C8/C14 的 G8/G9 让位)。
            🔴 [R3 订正7 2026-08-11] 与 `search_hints` 同型的留痕撒谎:
            applied/weak/observed 原是全量常量,不扣豁免 → 每篇 lineage 都记
            「G8/G9 已应用」而 prompt 里根本没有,D13a 归因被喂假数据。
            现在三个字段都只记**真进了 prompt** 的措施。
    """
    exempt = set(exempt_codes or ())
    applied = [m.code for m in MEASURES if m.code not in exempt] if injected else []
    observed = (
        {k: v for k, v in observe_gate2_measures(title, content, brand_name).items()
         if k not in exempt}
        if injected else {}
    )
    return {
        "version": GATE2_MEASURE_SET_VERSION,
        "injected": bool(injected),
        "applied": applied,
        # 弱依据的单独摘出来:归因时可以把它们从"有效"里剔掉再看一遍。
        "weak": [c for c in WEAK_CODES if c not in exempt] if injected else [],
        # 豁免了哪些也如实留痕(归因方不用反推)。
        "exempted": sorted(exempt) if injected else [],
        "observed": observed,
    }


# ---------------------------------------------------------------------------
# 元判据
# ---------------------------------------------------------------------------

def validate_gate2_measures() -> list[str]:
    """措施集自身的一致性。返回错误列表,空 = 通过。"""
    errors: list[str] = []
    seen = set()
    for m in MEASURES:
        if m.code in seen:
            errors.append(f"duplicate_code:{m.code}")
        seen.add(m.code)
        if m.evidence not in STRENGTHS:
            errors.append(f"unknown_evidence:{m.code}:{m.evidence}")
        if not m.source_measures:
            errors.append(f"no_source_measure:{m.code}")
        if m.product_checkable and m.code not in CHECKERS:
            errors.append(f"checkable_but_no_checker:{m.code}")
        if not m.product_checkable and m.code in CHECKERS:
            errors.append(f"not_checkable_but_has_checker:{m.code}")
        # 说"没法判"就必须说清为什么 —— 否则这个字段会变成偷懒的出口。
        if not m.product_checkable and not m.why_not_checkable.strip():
            errors.append(f"missing_why_not_checkable:{m.code}")
        # 🔴 红线 1:instruction 里不许出现枚举式词表。
        # 判据用构词法(一行里 ≥4 个顿号 = 在列表),不用"哪些词违规"的词表 ——
        # 否则这条元判据自己就是一张规则表。
        for line in m.instruction.splitlines():
            if line.count("、") >= 4:
                errors.append(f"instruction_looks_like_a_keyword_table:{m.code}")
                break
    # 弱依据的措施必须**能被单独摘出来**,否则"安针"就无从归因。
    if set(WEAK_CODES) != {m.code for m in MEASURES if m.evidence == "weak"}:
        errors.append("weak_codes_out_of_sync")
    return errors
