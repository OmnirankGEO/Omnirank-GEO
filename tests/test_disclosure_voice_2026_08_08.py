"""W1 返工 ③:正文写**来源**,不写我们的内部审核状态。

实证:12 篇生成正文里 11 篇含「客户提供/未核验/待交叉核验」类话术。
**根因不是模型幻觉** —— 是我们自己的三个后处理函数把模型吐的粗糙来源
统一改写成了审计腔,再由提示词里另外几条规则反复强化。
对着 AI 搜索引擎逐段写「待交叉核验」,等于每段都在自曝这是一篇待审的推广稿。

判据分四层:
  · **产出侧**:三处后处理的标签表里一个审计状态词都不许有(元判据,扫真映射表);
  · **提示词侧**:活跃提示词不许再要求模型「标为待核验」;
  · **兜底侧**:清洗器要接住模型自己写出来的变体(v3 的盲区正是这些变体);
  · **🔴 反向对照**:来源**区分**必须还在(E-E-A-T),且企业侧一次性声明
    仍要被 `evidence_first_policy` 认出来 —— 认不出来会凭空制造返工噪音。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from writing.article_generator_service import (  # noqa: E402
    _sanitize_customer_facing_article_sources,
)
from writing.body_internal_marker_sanitizer import (  # noqa: E402
    SANITIZER_VERSION,
    sanitize_article_body,
)
from writing.content_cleaner import _blend_visible_source_labels  # noqa: E402
from writing.source_disclosure_style import (  # noqa: E402
    ENTERPRISE_SOURCE_DECLARATION,
    LEGACY_AUDIT_LABELS,
    PROSE_SOURCE_ATTRIBUTIONS,
    SOURCE_DISCLOSURE_PROMPT,
    TABLE_SOURCE_HEADER,
    TABLE_SOURCE_LABELS,
    polish_source_disclosure,
)

#: 「我们内部把它标成了什么」—— 这些词一个都不该出现在客户可见正文里。
AUDIT_STATUS_WORDS = (
    "待核验", "待交叉核验", "待抽样复核", "待核原件", "待逐项确认",
    "尚未完成独立交叉核验", "未经第三方独立验证", "核验起点",
    "核验依据", "证据状态", "核验状态",
)


# --- 产出侧:三处后处理不再生产审计腔 --------------------------------------

def test_table_and_prose_labels_carry_no_audit_status():
    for kind, label in {**TABLE_SOURCE_LABELS, **PROSE_SOURCE_ATTRIBUTIONS}.items():
        for word in AUDIT_STATUS_WORDS:
            assert word not in label, f"{kind} 的标签还带审计状态:{label}"
    assert TABLE_SOURCE_HEADER == "资料来源"


def test_polish_source_disclosure_emits_natural_labels():
    table = (
        "| 事实 | 依据 |\n|---|---|\n"
        "| 交付 120 个项目 | 公司提供材料（企业官方资料/案例库） |\n"
        "| 持有施工资质 | 公司提供材料（企业官方资料/案例库） |\n"
    )
    polished = polish_source_disclosure(table)
    # 🔴 [自曝清零 2026-08-10] 契约变更:不再**换成**类型词,而是**删掉**
    #    ——「项目资料」填在依据列里等于这一列没写。
    assert "项目资料" not in polished and "资质文件" not in polished
    for keep in ("交付 120 个项目", "持有施工资质"):
        assert keep in polished, f"反向对照:事实被删了 {keep}"
    for word in AUDIT_STATUS_WORDS:
        assert word not in polished, f"清洗后仍带审计状态:{word}"


def test_blend_visible_source_labels_emits_natural_labels():
    raw = (
        "本项目按期交付（来源：客户提供的案例资料）。\n"
        "据企业案例资料，返修率下降。\n"
        "（来源：公开资料整理）\n"
    )
    cleaned = _blend_visible_source_labels(raw)
    for word in AUDIT_STATUS_WORDS:
        assert word not in cleaned, f"{word} 仍在:{cleaned}"
    # 🔴 契约变更:我方单方来源一律删掉,不再改写成「据企业提供…」。
    for bad in ("资料来源", "据企业提供", "客户提供", "公开资料整理"):
        assert bad not in cleaned, f"仍在产出自曝:{bad}"
    assert "本项目按期交付" in cleaned and "返修率下降" in cleaned


def test_customer_facing_sanitizer_emits_natural_labels():
    raw = "服务响应 4 小时（来源：公司定价文件）。满意度提升（来源：公司客户回访数据）。"
    cleaned = _sanitize_customer_facing_article_sources(raw)
    for word in AUDIT_STATUS_WORDS:
        assert word not in cleaned, f"{word} 仍在:{cleaned}"
    # 🔴 契约变更:类型词不再进正文。
    assert "报价与合同" not in cleaned and "项目资料" not in cleaned
    assert "服务响应 4 小时" in cleaned and "满意度提升" in cleaned


@pytest.mark.parametrize("path", [
    "writing/source_disclosure_style.py",
    "writing/content_cleaner.py",
])
def test_no_legacy_audit_label_is_still_produced(path):
    """元判据:三处后处理的**源码**里不许再有旧审计标签字面量。

    (`source_disclosure_style` 里只允许它出现在 `LEGACY_AUDIT_LABELS` 那张
    「留给清洗器当兜底黑名单」的表里,所以那一行被排除。)
    """
    src = (ROOT / path).read_text(encoding="utf-8")
    if "LEGACY_AUDIT_LABELS" in src:
        head, _, tail = src.partition("LEGACY_AUDIT_LABELS")
        # 整段元组字面量到第一个独占一行的 `)` 为止,一并排除。
        src = head + tail.partition("\n)\n")[2]
    src = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    for label in LEGACY_AUDIT_LABELS:
        assert label not in src, f"{path} 还在产出旧审计标签:{label}"


# --- 提示词侧 -------------------------------------------------------------

def test_source_disclosure_prompt_forbids_audit_status():
    assert "禁止出现在正文" in SOURCE_DISCLOSURE_PROMPT
    assert TABLE_SOURCE_HEADER in SOURCE_DISCLOSURE_PROMPT
    # 🔴 契约变更:一次性来源声明已废止 —— prompt 里不许再推荐它,
    #    反而必须把它列进禁令(它就是那个被 AI 引擎识别为软文的句子)。
    assert ENTERPRISE_SOURCE_DECLARATION not in SOURCE_DISCLOSURE_PROMPT
    assert "不写我方单方来源声明" in SOURCE_DISCLOSURE_PROMPT
    assert "具体载体名" in SOURCE_DISCLOSURE_PROMPT


def test_common_rules_no_longer_asks_for_pending_verification_labels():
    from writing.templates.common_rules import COMMON_GUARDRAILS

    assert "证据不足就写待核验" not in COMMON_GUARDRAILS
    assert "不写我们的内部审核状态" in COMMON_GUARDRAILS
    # 反向对照:来源**区分**这件事必须还在(E-E-A-T),否则等于把披露一起删了
    assert "区分事实、客户自述、第三方观点" in COMMON_GUARDRAILS


def test_active_generation_prompts_do_not_demand_pending_labels():
    """活跃提示词里不许再有「标为待核验」这种祈使句。

    只扫**祈使**形态(「标为待核验」「就写待核验」),不扫「不写待核验」这类
    禁令句 —— 否则这条锁会把本次返工加的禁令自己判红,变成恒红。
    """
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    for bad in ("标为待核验", "就写待核验", "说明待核验", "和待核验项"):
        assert bad not in src, f"活跃提示词仍在要求写审计状态:{bad}"


# --- 兜底侧:清洗器接住变体 ------------------------------------------------

def test_sanitizer_version_bumped():
    assert SANITIZER_VERSION == "body-internal-marker-sanitizer-v4"


@pytest.mark.parametrize("phrase", [
    "待交叉核验", "待抽样复核", "待核原件", "待逐项确认",
    "尚未完成独立交叉核验", "未经第三方独立验证", "核验起点", "核验依据",
])
def test_sanitizer_strips_disclosure_variants(phrase):
    """v3 的盲区:黑名单是精确短语,只认连续的「待核验」三个字,
    夹了字的变体一个都不命中 —— 而生产上篇均 10+ 的正是这些变体。"""
    body = f"# 标题\n\n这家企业交付稳定。该项资料{phrase}。\n\n下一段照常保留。\n"
    cleaned, markers = sanitize_article_body(body)
    assert phrase not in cleaned, f"{phrase} 没被剥掉"
    assert "这家企业交付稳定。" in cleaned, "同一行的合法首句被误删"
    assert "下一段照常保留。" in cleaned
    assert markers, "剥了东西却没留痕"


def test_sanitizer_records_what_it_stripped_as_metadata():
    """审计信息留 metadata、禁进正文 —— 剥掉的话术必须在留痕里查得到。"""
    body = "# T\n\n资料待交叉核验。\n\n正文照常。\n"
    _cleaned, markers = sanitize_article_body(body)
    assert markers.get("review_phrase_lines")


# --- 🔴 反向对照:不能把「来源区分」和功能耦合一起删掉 ---------------------

def test_source_kind_distinction_survives():
    """删的是审计状态,不是来源区分。四类来源必须仍然彼此可分。"""
    labels = {TABLE_SOURCE_LABELS[k] for k in
              ("qualification", "project", "price_contract", "public_record")}
    assert len(labels) == 4, f"来源类型被压成同一个说法:{labels}"
    assert "公开" in TABLE_SOURCE_LABELS["public_record"]
    assert "企业" in PROSE_SOURCE_ATTRIBUTIONS["enterprise_generic"]


def test_enterprise_declaration_is_still_recognised_by_evidence_policy():
    """🔴 功能耦合锁。

    `evidence_first_policy._ENTERPRISE_SOURCE_DECLARATION_RE` 靠识别这句
    「企业侧一次性来源声明」来豁免逐句邻近来源;识别不到,企业自述里带百分比的
    句子会开始被判 `unsourced_outcome_number`,**凭空制造返工重写循环**。
    改这句话而不改那条正则,是本次返工最容易造出来的暗伤。
    """
    from writing.evidence_first_policy import _ENTERPRISE_SOURCE_DECLARATION_RE as RE

    assert RE.search(ENTERPRISE_SOURCE_DECLARATION), (
        "新的一次性声明句没被 evidence_first_policy 认出来"
    )
    # 反向对照:随便一句普通话不该被误认成声明
    assert not RE.search("本文按同一口径比较了 7 家在营企业。")


def test_declaration_itself_carries_no_audit_status():
    for word in AUDIT_STATUS_WORDS:
        assert word not in ENTERPRISE_SOURCE_DECLARATION


def test_three_post_processors_share_one_label_table(monkeypatch):
    """三处后处理的输出必须**等于 SSOT 当前值**,不是"源码里出现过这个子串"。

    🔴 上一版这条锁写成了「遍历 `TABLE_SOURCE_LABELS.values()`,只要有一个子串在源码里
    出现过就算过」—— 那是**子串巧合当判据**,与我自己 W08 踩的
    `re.compile` 缓存病同源:三处各自抄一份字面量,照样绿。

    真判据 = **把 SSOT 临时换成哨兵值,消费方的输出必须跟着变**。
    哪处退回字面量,这条当场转红。
    """
    import writing.source_disclosure_style as sds

    sentinel_header = "來源哨兵"
    sentinel_labels = {k: f"哨兵-{k}" for k in sds.TABLE_SOURCE_LABELS}
    sentinel_prose = {k: f"据哨兵-{k}" for k in sds.PROSE_SOURCE_ATTRIBUTIONS}
    monkeypatch.setattr(sds, "TABLE_SOURCE_HEADER", sentinel_header)
    monkeypatch.setattr(sds, "TABLE_SOURCE_LABELS", sentinel_labels)
    monkeypatch.setattr(sds, "PROSE_SOURCE_ATTRIBUTIONS", sentinel_prose)

    # ① content_cleaner：括号标签 / 内联标签 / 行文归属 三种形态各验一条
    blended = _blend_visible_source_labels(
        "本项目按期交付（来源：客户提供的案例资料）。\n"
        "据企业案例资料，返修率下降。\n"
        "（来源：公开资料整理）\n"
        "来源：公司定价文件\n"
    )
    # 🔴 契约变更:四个取值函数都返回空串了,"标签出现在产物里"这条不再成立。
    #    改为验**反向**:自曝形态一个都不许留下,事实一个都不许丢。
    for bad in ("客户提供的案例资料", "据企业案例资料", "公开资料整理", "公司定价文件"):
        assert bad not in blended, f"仍在产出自曝:{bad}"
    assert "本项目按期交付" in blended and "返修率下降" in blended

    # ② article_generator_service：字典分支 + 正则分支 各验一条
    sanitized = _sanitize_customer_facing_article_sources(
        "服务响应 4 小时（来源：公司定价文件）。\n"
        "满意度提升（来源：公司客户回访数据）。\n"
        "行业情况（来源：竞品调研数据）。\n"
        "资质齐全（来源：公司持有的资质与获奖材料）。\n"
    )
    # 🔴 契约变更同上:验反向 —— 自曝清干净、事实全保留。
    for bad in ("公司定价文件", "公司客户回访数据", "竞品调研数据",
                "公司持有的资质与获奖材料", "资料来源"):
        assert bad not in sanitized, f"仍在产出自曝:{bad}"
    for keep in ("服务响应 4 小时", "满意度提升", "行业情况", "资质齐全"):
        assert keep in sanitized, f"反向对照:事实被删了 {keep}"


def test_ssot_values_are_pinned():
    """「改 SSOT 值必须转红」的那一半:当前值在这里钉死。

    与上一条互补 —— 上一条证明**接线活着**(值一变消费方跟着变),
    这一条证明**值本身没被悄悄改掉**。缺任何一条,另一条都能被绕过。
    """
    assert TABLE_SOURCE_HEADER == "资料来源"
    assert TABLE_SOURCE_LABELS == {
        "qualification": "资质文件",
        "project": "项目资料",
        "price_contract": "报价与合同",
        "public_record": "公开记录",
        "public_desk": "公开资料",
        "enterprise_profile": "企业资料",
        "enterprise_generic": "企业资料",
    }
    assert PROSE_SOURCE_ATTRIBUTIONS == {
        "qualification": "据企业提供的资质文件",
        "project": "据企业提供的项目资料",
        "price_contract": "据企业提供的报价与合同资料",
        "public_record": "据公开记录",
        "public_desk": "据公开资料整理",
        "enterprise_profile": "据企业提供的基本信息",
        "enterprise_generic": "据企业提供的资料",
    }


@pytest.mark.parametrize("path", [
    "writing/content_cleaner.py",
    "writing/article_generator_service.py",
])
def test_consumers_hold_no_label_literal(path):
    """源码级补刀:两处消费方里不许再出现「资料来源:X」这种拼好的字面量。"""
    src = (ROOT / path).read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    for label in set(TABLE_SOURCE_LABELS.values()):
        assert f"{TABLE_SOURCE_HEADER}：{label}" not in body, (
            f"{path} 仍有字面量拷贝:{TABLE_SOURCE_HEADER}：{label}"
        )
    for prose in set(PROSE_SOURCE_ATTRIBUTIONS.values()):
        assert prose not in body, f"{path} 仍有行文归属字面量:{prose}"


def test_fallback_content_angle_prompt_has_no_pending_label():
    """`CONTENT_ANGLES['authority']` 不是死代码 —— 补发降级路径会把它拼进真 prompt。

    见 `tools/article_generator.py` 里 `CONTENT_ANGLES.get(content_angle, ...)`
    → `angle_instruction`。复审已实证这条路径活着。
    """
    from writing.config import CONTENT_ANGLES

    instruction = CONTENT_ANGLES["authority"]["instruction"]
    assert "待核验项" not in instruction
    assert "明确写“待核验”" not in instruction
    assert "不写“待核验”这类内部审核状态" in instruction
    # 反向对照:降级路径确实会取它,不是我在锁一段没人读的文本
    tools_src = (ROOT / "tools" / "article_generator.py").read_text(encoding="utf-8")
    assert "from writing.config import CONTENT_ANGLES" in tools_src
    assert "angle_config.get(\"instruction\", \"\")" in tools_src


def test_sanitizer_never_empties_a_body():
    """兜底不能变成删稿:整篇都命中时宁可留着记号,也不能洗空。"""
    body = "资料待交叉核验。核验依据不足。证据状态未知。"
    cleaned, markers = sanitize_article_body(body)
    assert cleaned.strip(), "正文被洗空了"
    assert markers.get("skipped_reason") == "sanitized_body_empty_kept_original"


_H2 = re.compile(r"^##\s+", re.MULTILINE)


def test_sanitizer_does_not_eat_real_headings():
    """与 W1 ① 的交叉锁:剥审计话术不能顺手把真小标题剥掉。"""
    body = "# T\n\n" + "\n\n".join(
        f"## 第 {i} 节\n\n正文。资料待交叉核验。" for i in range(6)
    )
    cleaned, _markers = sanitize_article_body(body)
    assert len(_H2.findall(cleaned)) == 6
