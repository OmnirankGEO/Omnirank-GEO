"""GEO 抖音图文 · 卡面文字与客户资料库的轻量核对(详情页那个黄点)

工单原文:「黄点提示数据源 = 文本阶段与知识库的轻量核对;
          **若本批不实现核对逻辑,黄点整体隐藏,禁止摆假数据**」。
本模块就是那个核对逻辑 —— 黄点有真数据源,不是装饰。

## 核的是什么(范围写死,不含糊)

只核**可核验的事实型数字**,即数字后面跟着这几类单位的:
    价格(元/万/亿/¥) · 时长(年/月/天/小时/工作日) · 比例(%/折) · 面积(㎡/平方米/平)
这几类恰好是"编一个数字出来"风险最高、且客户资料里通常真有出处的。

**故意不核**结构性数字:「5 步决策法」「3 家对比」「TOP10」——
这些是版式,不是事实主张。把它们也标黄会让黄点淹没在噪声里,用户就不看了。

## 判据

卡面上出现的事实型数字,其**数值本身**必须能在客户资料(client_materials +
知识库检索片段)里找到。找不到 = 标黄「这条参数与资料库不一致,建议改文字」。

🔴 这是**提示级**,不阻断发布。数字对不上有两种可能:模型编的,或者资料库没写全。
   我们无法区分,所以只提示、让人去看,不自动改也不拦。

🔴 没有客户资料时 `checked=False`,前端据此**整体隐藏黄点** ——
   没有比对基准却把每张卡都标黄,那是假数据(每条都"不一致"其实是我们没资料)。

不调 LLM:确定性、可解释、零成本、可复现。同样的卡 + 同样的资料永远同样的结果。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# 事实型单位。顺序有意义:长单位排前面,免得「平方米」被「平」先吃掉。
_FACT_UNITS = (
    "工作日", "平方米", "个月", "小时", "㎡",
    "元", "万", "亿", "年", "月", "天", "%", "折", "平",
)

# 数字(允许千分位与小数)+ 紧跟其后的事实型单位
_CLAIM_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(" + "|".join(map(re.escape, _FACT_UNITS)) + r")"
)

# 卡片里参与核对的文字字段(和 M3 同构字段一致)
_CHECKED_FIELDS = ("entity", "one_liner", "metric", "caveat")


@dataclass
class CardCheck:
    card_index: int          # 组内第几张(0 基),与 oss_keys / cards 同序
    ok: bool = True
    mismatches: List[str] = field(default_factory=list)   # 对不上的原文片段

    def to_dict(self) -> dict:
        return {"card_index": self.card_index, "ok": self.ok,
                "mismatches": list(self.mismatches)}


@dataclass
class ConsistencyReport:
    checked: bool = False        # False = 没有比对基准,前端整体隐藏黄点
    reason: str = ""             # checked=False 时说明为什么
    cards: List[CardCheck] = field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        return sum(1 for c in self.cards if not c.ok)

    def to_dict(self) -> dict:
        return {"checked": self.checked, "reason": self.reason,
                "flagged_count": self.flagged_count,
                "cards": [c.to_dict() for c in self.cards]}


def _normalize_number(raw: str) -> str:
    """去千分位、去末尾无意义的 .0,便于跨写法比对(1,200 == 1200 == 1200.0)。"""
    s = raw.replace(",", "").strip()
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def extract_fact_claims(text: str) -> List[tuple]:
    """从一段文字里抽出 (归一化数值, 单位, 原文片段)。"""
    out: List[tuple] = []
    for m in _CLAIM_RE.finditer(str(text or "")):
        out.append((_normalize_number(m.group(1)), m.group(2), m.group(0)))
    return out


def _corpus_numbers(corpus: str) -> set:
    """资料里出现过的所有数值(不限单位)。

    比对**只看数值不看单位**:资料里写「工期 45 天」、卡面写「45 个工作日」,
    单位不同但数出自同一处,不该标黄。宁可漏标,不要错标 ——
    错标会让用户觉得这个黄点没用,然后就不看了,那才是真的失效。
    """
    nums = set()
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(corpus or "")):
        nums.add(_normalize_number(m.group(0)))
    return nums


def check_cards_against_materials(cards: List[dict],
                                  materials_corpus: str) -> ConsistencyReport:
    """核对整组卡片。cards 用 generation_meta.content.cards(带完整字段的那份)。"""
    corpus = str(materials_corpus or "").strip()
    if not corpus:
        return ConsistencyReport(
            checked=False,
            reason="这个客户还没有可比对的资料，先补资料才能帮你核参数")

    known = _corpus_numbers(corpus)
    report = ConsistencyReport(checked=True)
    for i, card in enumerate(cards or []):
        if not isinstance(card, dict):
            report.cards.append(CardCheck(card_index=i))
            continue
        texts: List[str] = [str(card.get(f) or "") for f in _CHECKED_FIELDS]
        texts.extend(str(p) for p in (card.get("points") or []))

        bad: List[str] = []
        for text in texts:
            for value, _unit, snippet in extract_fact_claims(text):
                if value not in known and snippet not in bad:
                    bad.append(snippet)
        report.cards.append(CardCheck(card_index=i, ok=not bad, mismatches=bad))
    return report


async def build_report_for_post(post: dict) -> ConsistencyReport:
    """按作品跑一遍核对。取不到资料 → checked=False(前端隐藏黄点)。"""
    from services.geo_douyin.knowledge_context import build_brand_context

    meta = post.get("generation_meta") or {}
    snapshot = meta.get("content") if isinstance(meta.get("content"), dict) else {}
    cards = list(snapshot.get("cards") or [])
    if not cards:
        return ConsistencyReport(
            checked=False, reason="这条内容没有可核对的卡片要点")

    brand_id = post.get("brand_id")
    if not brand_id:
        return ConsistencyReport(
            checked=False, reason="这条内容没有关联客户，没有可比对的资料")

    ctx = await build_brand_context(int(brand_id), str(post.get("keyword") or ""))
    return check_cards_against_materials(cards, ctx.to_prompt_block())


def card_flag_map(report: ConsistencyReport) -> Dict[int, List[str]]:
    """给前端的 {卡序: [对不上的片段]}。checked=False 时返回空 —— 一个黄点都不给。"""
    if not report.checked:
        return {}
    return {c.card_index: list(c.mismatches) for c in report.cards if not c.ok}


# ─────────────────────────────────────────────────────────────
# 规范 §8.6 第 10 条:卡面 headline 与 caption(desc)同事实同术语
# ─────────────────────────────────────────────────────────────
# 上面那套核的是「卡面 vs 客户资料」。这一套核的是**另一条轴**:
# 「卡面 vs 这条帖子自己的文案」。组图不是孤岛 —— 图上写"质保 15 年"、
# 文案写"质保 10 年",两边分开看都合规,合在一起就是自相矛盾。
#
# 🔴 判据是**检测矛盾,不是检测缺失**。
#    很容易顺手写成"卡面上的每个数字都必须在文案里出现",那是错的:
#    §8.5 明确把数字分了两层 —— 方法论计数(「至少核对 3 项参数」)本来就允许
#    只出现在卡面。按"缺失即违规"去判,合规产出会被大面积误判,
#    然后这套判据就会被当成噪声关掉。
#    所以只在**同一个单位两边都出现、而数值集合完全不相交**时才报。
#
# 🔴 同样是**提示级**,不阻断(与广告法扫描、上面那套黄点同一档)。
#    Owner 2026-08-03 二次拍板:发不发由客户,平台只尽提示义务。

# 术语重合检测时要跳过的通用词 —— 它们在任何一条内容里都会出现,
# 拿它们算"重合"等于判据恒真。
_STOP_TERMS = frozenset((
    "怎么", "哪家", "如何", "什么", "为什么", "选择", "推荐", "注意",
    "avoid", "这些", "那些", "可以", "需要", "建议", "一个", "我们",
))

_TERM_RE = re.compile(r"[一-龥]{2,}|[A-Za-z]{3,}")

# 🔴 2026-08-03 本机拿真实数据跑出来的**误报**,修法记在这里:
#    原来 `_terms()` 直接用 `_TERM_RE.findall`,而它抓的是**中文极大连续段**。
#    中文没有空格 —— 一整句话就是一个 term。于是:
#        封面「全屋定制」        → {'全屋定制'}
#        文案「深圳全屋定制哪家好…」→ {'深圳全屋定制哪家好', …}
#    两边共享"全屋定制"这四个字,集合却毫无交集 → 报 topic_drift。
#    页面上真的红了一条"封面标题和文案没有一个共同的关键词",而它们明明讲的是同一件事。
#
#    我那条锁没抓到,是因为用例挑的是**完全无关**的两句(汽车保养 vs 定制衣柜),
#    那种情况下正确实现和错误实现的结果一样 —— 用例没有判别力。
#
#    改用**中文二元组**(character bigram):按字滑窗切两字一组,
#    「全屋定制」→ 全屋/屋定/定制,「深圳全屋定制…」也含这三组 → 有交集,不报。
#    代价是判别力下降(不相关的两段也可能共享"怎么"这类常见二元组),
#    但这是**刻意的**:提示级判据一旦开始乱叫,用户就再也不看它了。
#    宁可漏报,不要误报 —— 与本模块上面那套黄点同一条取舍。
_BIGRAM_STOP = frozenset((
    "怎么", "哪家", "如何", "什么", "为什", "选择", "推荐", "注意",
    "可以", "需要", "建议", "一个", "我们", "这些", "那些",
))


def _bigrams(text: str) -> set:
    """中文按字切二元组;英文/数字按整词。"""
    out: set = set()
    for run in _TERM_RE.findall(str(text or "")):
        if run.isascii():
            if run.lower() not in _STOP_TERMS:
                out.add(run.lower())
            continue
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in _BIGRAM_STOP:
                out.add(gram)
    return out


@dataclass
class AlignmentIssue:
    kind: str            # "number_conflict" | "topic_drift"
    where: str           # 出在哪(封面 / 第 N 张 / 收尾卡)
    detail: str          # 给人看的一句话

    def to_dict(self) -> dict:
        return {"kind": self.kind, "where": self.where, "detail": self.detail}


@dataclass
class AlignmentReport:
    checked: bool = False
    reason: str = ""
    issues: List[AlignmentIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {"checked": self.checked, "reason": self.reason, "ok": self.ok,
                "issues": [i.to_dict() for i in self.issues]}


def _claims_by_unit(text: str) -> Dict[str, set]:
    """{单位: {数值,…}}。同一段文字里同一单位出现多个值是正常的(区间/枚举)。"""
    out: Dict[str, set] = {}
    for value, unit, _snippet in extract_fact_claims(text):
        out.setdefault(unit, set()).add(value)
    return out


def _terms(text: str) -> set:
    """比对用的"共同点"集合。**用二元组,不用整段** —— 见上面那段。"""
    return _bigrams(text)


def _surface_texts(cover: dict, cards: List[dict], closing: dict) -> List[tuple]:
    """(位置说明, 卡面文字) —— 只取**真的会印在图上**的字段。

    🔴 不含 points 之外的内部字段:判"图上写的和文案说的矛不矛盾",
       比对对象就必须是图上那几行,不是模型的中间产物。
    """
    out: List[tuple] = []
    cov = cover or {}
    cov_text = " ".join(str(cov.get(k) or "") for k in ("title", "subtitle"))
    if cov_text.strip():
        out.append(("封面", cov_text))
    for i, card in enumerate(cards or []):
        if not isinstance(card, dict):
            continue
        parts = [str(card.get(k) or "") for k in ("headline", "entity", "one_liner", "metric")]
        parts.extend(str(p) for p in (card.get("points") or []))
        text = " ".join(parts)
        if text.strip():
            out.append((f"第 {i + 1} 张", text))
    clo = closing or {}
    clo_text = " ".join(str(clo.get(k) or "") for k in ("headline", "summary"))
    if clo_text.strip():
        out.append(("收尾卡", clo_text))
    return out


def check_headline_caption_alignment(body: str, cover: dict,
                                     cards: List[dict],
                                     closing: dict) -> AlignmentReport:
    """规范 §8.6 第 10 条的机器判据。不调 LLM:确定性、可复现、零成本。

    报两类:
      ① number_conflict —— 同一单位两边都写了,值却完全不相交(自相矛盾);
      ② topic_drift     —— 封面与文案连一个实词都不重合(讲的不是一回事)。
    """
    caption = str(body or "").strip()
    if not caption:
        # 没有文案就没有比对基准。**不报 ok**,报 checked=False ——
        # "没查"和"查了没问题"必须分得开,否则前端会把前者显示成绿灯。
        return AlignmentReport(checked=False, reason="这条内容还没有文案，无法比对")

    surfaces = _surface_texts(cover, cards, closing)
    if not surfaces:
        return AlignmentReport(checked=False, reason="这条内容还没有卡面文字，无法比对")

    report = AlignmentReport(checked=True)
    cap_units = _claims_by_unit(caption)

    for where, text in surfaces:
        for unit, values in _claims_by_unit(text).items():
            cap_values = cap_units.get(unit)
            if not cap_values:
                continue          # 文案里没提这个单位 → 不是矛盾,是分工(§8.5)
            if values & cap_values:
                continue          # 有交集就算对得上(区间/多值写法很常见)
            report.issues.append(AlignmentIssue(
                kind="number_conflict", where=where,
                detail=(f"{where}写的是 {'、'.join(sorted(values))}{unit}，"
                        f"文案里写的是 {'、'.join(sorted(cap_values))}{unit}，"
                        f"两处对不上")))

    cap_terms = _terms(caption)
    cover_text = next((t for w, t in surfaces if w == "封面"), "")
    if cover_text and cap_terms and not (_terms(cover_text) & cap_terms):
        report.issues.append(AlignmentIssue(
            kind="topic_drift", where="封面",
            detail="封面标题和文案没有一个共同的关键词，像是两条内容拼在一起"))
    return report
