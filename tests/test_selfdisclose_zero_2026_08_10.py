# -*- coding: utf-8 -*-
"""自曝清零 + 顺序修复 · 锁。

判据纪律(本仓踩过的坑,逐条落成断言):
  1. **打在接线上,不打在函数上** —— 已有六例「函数对了没接线」。
  2. **每个「必须命中」配一个「必须不命中」** —— 恒红与恒真一样废。
  3. **元判据:夹具必须真含自曝形态** —— 否则"清洗后没有自曝"是空话。
  4. **反向对照**:改 SSOT 的值,消费方必须跟着变(防字面量拷贝)。
"""
from __future__ import annotations

import inspect
import re

import pytest

from writing import content_cleaner, source_disclosure_style as sds
from writing.body_internal_marker_sanitizer import sanitize_article_body
from writing.evidence_first_policy import evaluate_content_trust
from writing.post_sanitize_rejudge import DELTA_KEY, rejudge_after_sanitize

BRAND = "晨光富士电梯"
TITLE = "2025年深圳电梯选型参考"

#: 真实生产形态。**每一条都必须能在下面的元判据里被找到**。
SELF_DISCLOSURE_FIXTURE = f"""## {BRAND} 客户情况

{BRAND}的准时交付率为 98.6%（来源：客户提供的服务资料）。
据企业提供资料，其 2024 年交付观光梯 32 台。
以下企业相关信息来自企业提供的业务资料，并标注了各自的资料来源。
企业提交资料（截至 2025 年 3 月）显示，质保期为 24 个月。
公司提供材料（企业官方资料/案例库）显示，客户回访满意度 95%。
来源：客户提供资料

| 项目 | 数值 | 资料来源 |
|---|---|---|
| 提升高度 | 24m | 企业资料 |
| 载重 | 1000kg | 公开记录 |
"""

#: 🔴 反向对照夹具:**同样的数字,挂的是真外部信源**。
#: 它必须**不**被判 `unsourced_outcome_number` —— 否则收窄就是恒红。
EXTERNAL_SOURCE_FIXTURE = f"""## {BRAND} 客户情况

据《中国电梯》2025 年 3 月报道，{BRAND}的准时交付率为 98.6%。
据深圳市市场监管局 2024 年 11 月公示，该公司持有特种设备制造许可。
其适用于 12 层以下商业综合体观光梯改造，不适用于超高层住宅。
存在的局限是产能有限，旺季交付周期可能延长，需核验合同约定的违约条款。
反向来看，若项目层数超过 18 层，应优先考虑其他厂商。
"""

_SEEDED = (
    "客户提供的服务资料", "据企业提供资料", "来自企业提供的业务资料",
    "企业提交资料（截至", "公司提供材料", "来源：客户提供资料",
)

#: 🔴 [变异 M05 存活补锁] 自曝出现在**句中**(前面有句号),删掉后会留下
#: 「。，」这种中缀悬空标点。原夹具里自曝全在行首,`_DANGLING_MID_PUNCT`
#: 一次没被执行 → 删掉它测试照样绿。
MID_LINE_FIXTURE = (
    f"{BRAND}成立于 2009 年。据企业提供资料，其 2024 年交付观光梯 32 台。"
)

#: 🔴 [变异 M11 存活补锁] 裸「截至 20XX」**不与任何自曝短语相邻**。
#: 原夹具里那个「截至」紧挨着「企业提交」,会被自源过滤器吃掉 →
#: 把「截至 20XX」加回 `_SOURCE_MARKER_RE` 在原夹具上无差别。
#: 🔴 [返工 2026-08-10 · 复审必补 ①] **第三方来源披露**夹具。
#:
#: 上一版的「…均来自 X 提供的 Y 资料。」是一条盲删正则,把这四句全删空了。
#: 而原夹具里**没有这种形态**,所以 32 条锁和 32 条变异在这个方向上全是瞎的 ——
#: 变异把这条正则删掉、改宽、改窄,测试照样绿。这正是"夹具没有覆盖的形态 =
#: 该方向零判别力"的又一例。
EXTERNAL_SOURCE_DISCLOSURE_FIXTURES = (
    "本文数据均来自深圳市住建局提供的公开资料。",
    "本文数据均来自深圳市市场监管局提供的公示材料。",
    "相关数据均来自中国建筑装饰协会提供的行业资料。",
    "本文数据均来自国家统计局提供的年度统计资料。",
)
#: 同一句式的**我方单方**版本 —— 必须删。两组成对,缺任一组该方向都测不出东西。
SELF_SIDE_SOURCE_FIXTURES = (
    "本文所有核心数据均来自企业提供材料，未经独立第三方审计。",
    "数据均来自KZ木作定制提供的企业官方资料，截至2026年3月。",
    "相关信息均来自公司提供的内部资料。",
)

#: ⚠️ 必须用 `_UNSOURCED_OUTCOME_RE` 真认识的词(提升/增长/降低/节省/转化率/
#: 满意度/成功率)。用「准时交付率」这条规则根本不触发 → 断言恒假,我第一版就踩了。
BARE_CUTOFF_FIXTURE = (
    f"## {BRAND}\n\n{BRAND}的客户满意度为 98.6%。该口径截至 2025 年 6 月。\n"
)


def _run_chain(body: str) -> str:
    """走生产同一条后处理链:blend → polish → sanitize。"""
    out = content_cleaner._blend_visible_source_labels(body)
    out = sds.polish_source_disclosure(out)
    out, _ = sanitize_article_body(out)
    return out


def _codes(assessment) -> set[str]:
    return {f"soft:{i.code}" for i in assessment.soft} | {
        f"hard:{i.code}" for i in assessment.hard
    }


# ---------------------------------------------------------------- 元判据
class TestMetaCriteria:
    """夹具本身必须成立,否则后面全是空断言。"""

    def test_fixture_really_contains_self_disclosure(self):
        hit = [p for p in _SEEDED if p in SELF_DISCLOSURE_FIXTURE]
        assert len(hit) >= 5, f"夹具没有真含自曝形态,后续断言全是空的:{hit}"

    def test_fixture_table_really_contains_type_word_cells(self):
        assert re.search(r"\|\s*企业资料\s*\|", SELF_DISCLOSURE_FIXTURE)
        assert re.search(r"\|\s*公开记录\s*\|", SELF_DISCLOSURE_FIXTURE)

    def test_external_fixture_really_contains_external_source(self):
        assert "《中国电梯》" in EXTERNAL_SOURCE_FIXTURE
        assert "深圳市市场监管局" in EXTERNAL_SOURCE_FIXTURE
        assert "98.6%" in EXTERNAL_SOURCE_FIXTURE, "反向夹具必须含同样的数字才可比"


# ---------------------------------------------------------------- SSOT
class TestDisclosureSSOT:
    def test_prose_attribution_no_longer_produces_self_disclosure(self):
        for kind in sds.DEPRECATED_PROSE_SOURCE_ATTRIBUTIONS:
            assert sds.prose_attribution(kind) == "", f"{kind} 仍在产出正文归属"

    def test_table_label_no_longer_produces_type_word(self):
        for kind in sds.DEPRECATED_TABLE_SOURCE_LABELS:
            assert sds.table_label(kind) == "", f"{kind} 仍在产出类型词"

    def test_parenthesized_returns_empty_not_empty_brackets(self):
        # 直接返回空串,而不是 f"（{labeled(kind)}）" —— 后者会留下「（）」
        assert sds.parenthesized("enterprise_generic") == ""
        assert sds.labeled("enterprise_generic") == ""

    def test_blacklist_is_derived_from_ssot_not_hardcoded(self, monkeypatch):
        """反向对照:改 SSOT 的值,黑名单必须跟着变。"""
        sentinel = "＿哨兵来源＿"
        monkeypatch.setitem(
            sds.DEPRECATED_PROSE_SOURCE_ATTRIBUTIONS, "enterprise_generic", sentinel,
        )
        assert sentinel in sds.self_disclosure_blacklist(), (
            "黑名单是字面量拷贝,改 SSOT 不跟着变"
        )


# ---------------------------------------------------------------- 清洗行为
class TestSelfDisclosureRemoved:
    def test_all_seeded_forms_gone_after_chain(self):
        out = _run_chain(SELF_DISCLOSURE_FIXTURE)
        left = [p for p in _SEEDED if p in out]
        assert not left, f"自曝残留:{left}"

    def test_no_generic_attribution_reintroduced(self):
        out = _run_chain(SELF_DISCLOSURE_FIXTURE)
        for bad in ("据企业提供的资料", "（资料来源：企业资料）",
                    "资料来源：公开记录", "未经独立核验"):
            assert bad not in out, f"清洗把自曝换成了另一种自曝:{bad}"

    def test_no_empty_brackets_left_behind(self):
        out = _run_chain(SELF_DISCLOSURE_FIXTURE)
        assert "（）" not in out and "()" not in out

    def test_no_dangling_verb_left_behind(self):
        """删短语不能留下「显示，质保期…」这种没主语的残句。"""
        out = _run_chain(SELF_DISCLOSURE_FIXTURE)
        assert not re.search(r"^\s*(?:显示|记载|表明|说明|指出|载明)[，,]", out, re.M)

    def test_table_type_word_cells_blanked(self):
        out = _run_chain(SELF_DISCLOSURE_FIXTURE)
        assert not re.search(r"\|\s*企业资料\s*\|", out)
        assert not re.search(r"\|\s*公开记录\s*\|", out)

    def test_real_carrier_cell_survives(self):
        """反向对照:具体载体名**不许**被清空。"""
        body = "| 提升高度 | 24m | 《中国电梯》2025-03 |\n"
        out = content_cleaner._blend_visible_source_labels(body)
        assert "《中国电梯》2025-03" in out

    def test_mid_line_dangling_punct_collapsed(self):
        """[补 M05] 句中自曝删掉后不许留「。，」。"""
        assert "。据企业提供资料，" in MID_LINE_FIXTURE, "夹具没含句中自曝,断言是空的"
        out = _run_chain(MID_LINE_FIXTURE)
        assert "据企业提供资料" not in out
        assert not re.search(r"[。！？]\s*[，,、；;：:]", out), f"留下中缀悬空标点:{out!r}"
        # [P0-5 2026-08-10] 归属从句摘掉,**两句事实都必须存活**。
        assert "成立于 2009 年" in out, "摘归属殃及了邻句"
        assert "32 台" in out, "客户的具体事实必须留下(只摘归属,不删主张)"

    def test_strip_self_disclosure_collapses_mid_punct_directly(self):
        """[变异 M05 实测补锁] `strip_self_disclosure` 自己的中缀标点收敛
        必须有判别力 —— 走 _run_chain 时从句摘除层先把「，」吃掉,
        M05(去掉 _DANGLING_MID_PUNCT)照样全绿。这里直打该函数。"""
        import re as _re

        from writing import source_disclosure_style as _sds

        raw = "成立于 2009 年。据企业提供资料，在营门店 32 家。"
        out = _sds.strip_self_disclosure(raw, _re.compile(r"据企业提供资料"))
        assert "据企业提供资料" not in out
        assert "。，" not in out and not _re.search(r"[。！？][，,、；;：:]", out), (
            f"中缀悬空标点没被收敛:{out!r}"
        )
        assert "32 家" in out, "只摘归属,主张必须留下(反向对照)"

    # ---- [返工 ①] 主语判别:我方单方才删,第三方一律留 ----
    @pytest.mark.parametrize("sentence", EXTERNAL_SOURCE_DISCLOSURE_FIXTURES)
    def test_third_party_source_disclosure_is_kept(self, sentence):
        """🔴 反向对照:第三方来源披露**不许**被删。

        删掉它 = 造出反方向的谎言 —— 本包的目的正是让文章多挂公开信源。
        """
        out = content_cleaner._blend_visible_source_labels(sentence)
        assert out.strip(), f"第三方来源披露被整句删空:{sentence!r}"
        # 主体名必须原样还在,不能被削成半句
        for subject in ("住建局", "市场监管局", "建筑装饰协会", "国家统计局"):
            if subject in sentence:
                assert subject in out, f"来源主体被削掉:{subject}"

    @pytest.mark.parametrize("sentence", SELF_SIDE_SOURCE_FIXTURES)
    def test_self_side_source_sentence_is_removed(self, sentence):
        """成对的正向:同一句式的我方单方版本必须删干净。"""
        out = content_cleaner._blend_visible_source_labels(sentence)
        assert not out.strip(), f"我方单方来源句没删掉:{out!r}"

    def test_subject_predicate_reuses_sibling_module(self):
        """反向对照:主语判别必须**复用** `evidence_first_policy` 那一份,
        不许在 content_cleaner 里另抄一份(两份手抄同串是本仓已犯过的病)。"""
        src = inspect.getsource(content_cleaner._is_self_side_source)
        assert "_SELF_SOURCE_MARKER_RE" in src
        assert "from writing.evidence_first_policy import" in src

    def test_predicate_falls_back_without_sibling(self, monkeypatch):
        """姊妹模块取不到时退回本模块判据,不炸、不漏删。"""
        import builtins
        real_import = builtins.__import__

        def _boom(name, *a, **k):
            if name == "writing.evidence_first_policy":
                raise ImportError("simulated")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _boom)
        assert content_cleaner._is_self_side_source("均来自公司提供的内部资料") is True
        assert content_cleaner._is_self_side_source("均来自住建局提供的公开资料") is False

    # 🔴🔴 [复审返工 2026-08-10 · P1-2] 本条按**新契约**重写。
    #
    # 旧断言要求 "32 台" / "95%" 在清洗后仍然存在 —— 那些数字在夹具里
    # 恰恰挂在「据企业提供资料，…」这类我方单方归属上。留着它们 =
    # 擦掉来源、留下裸数字,复审判为**比自曝更糟**(把可识别的单方材料
    # 洗成看起来客观的无来源事实)。新契约:整条主张一起删。
    #
    # 但本条的**目的**没变,而且必须继续守住:清洗**绝不许把正文洗光**、
    # 绝不许丢稿(D8 零阻断)。所以断言换成「结构与无归属事实必须存活」。
    def test_body_not_emptied(self):
        """🔴🔴 清洗不许把正文洗光,更不许删掉客户的具体事实。

        [P0-5 2026-08-10] 本条一度被我改成"允许删掉 32 台/95%",那是错的:
        客户的交付量、质保、响应时长是**中小企业唯一能被 AI 复述成推荐理由**
        的素材,没有第三方会去报道它们。删光 = 文章更干净、客户更不会被推荐。
        """
        out = _run_chain(SELF_DISCLOSURE_FIXTURE)
        assert out.strip(), "清洗把正文洗空了 —— 丢稿比自曝严重得多"
        for keep in ("98.6%", "32 台", "24 个月", "95%"):
            assert keep in out, f"客户具体事实被删:{keep}(违背第一性原理)"
        assert "##" in out or "|" in out, "结构(标题/表格)被清洗吃掉了"

    def test_self_attribution_stripped_but_claim_kept(self):
        """正向:摘掉我方单方**归属从句**,主张本身留下。"""
        out = _run_chain(SELF_DISCLOSURE_FIXTURE)
        assert "据企业提供资料" not in out, "卖方归属没摘干净"
        assert "32 台" in out, "摘归属时把主张一起删了"


# ---------------------------------------------------------------- 判定方向
class TestJudgementDirection:
    def test_self_disclosure_now_flags_instead_of_exempting(self):
        codes = _codes(evaluate_content_trust(
            TITLE, SELF_DISCLOSURE_FIXTURE, evidence_mode="standard"))
        assert "soft:self_disclosed_source" in codes, "自曝没有亮信号(豁免翻转失败)"

    def test_self_sourced_number_no_longer_counts_as_sourced(self):
        """「来源：客户提供的服务资料」不再能替 98.6% 挂源。"""
        codes = _codes(evaluate_content_trust(
            TITLE, SELF_DISCLOSURE_FIXTURE, evidence_mode="standard"))
        assert "soft:unsourced_outcome_number" in codes

    def test_external_source_still_accepted(self):
        """🔴 反向对照:真外部信源必须仍被接受,否则收窄=恒红。"""
        codes = _codes(evaluate_content_trust(
            TITLE, EXTERNAL_SOURCE_FIXTURE, evidence_mode="standard"))
        assert "soft:unsourced_outcome_number" not in codes, (
            "《中国电梯》2025年3月报道 都不算来源 —— 收窄过头,变成恒红"
        )
        assert "soft:self_disclosed_source" not in codes

    def test_bare_cutoff_date_is_not_a_source(self):
        """[补 M11] 裸「截至 2025 年 6 月」不算来源 —— 四个字不能替数字挂源。"""
        from writing.evidence_first_policy import _UNSOURCED_OUTCOME_RE
        assert "截至 2025 年 6 月" in BARE_CUTOFF_FIXTURE
        assert _UNSOURCED_OUTCOME_RE.search(BARE_CUTOFF_FIXTURE), (
            "夹具里的数字根本不触发 unsourced 规则(它只认 提升/增长/降低/节省/"
            "转化率/满意度/成功率),断言是恒假的"
        )
        assert "企业" not in BARE_CUTOFF_FIXTURE.split("截至")[0][-30:], (
            "夹具里「截至」旁边有自曝短语 → 会被自源过滤器吃掉,变异无差别"
        )
        codes = _codes(evaluate_content_trust(
            TITLE, BARE_CUTOFF_FIXTURE, evidence_mode="standard"))
        assert "soft:unsourced_outcome_number" in codes, (
            "裸截至日期仍被当成来源 —— 收窄没生效"
        )

    def test_anonymous_authority_repair_appends_no_disclaimer(self):
        """[补 M22] 后处理不许再往正文追加「来源未具名,需进一步核验」。"""
        from writing.evidence_first_policy import repair_recoverable_trust_issues
        body = "据权威机构统计，行业平均交付周期为 45 天。"
        out, codes = repair_recoverable_trust_issues(body)
        assert "anonymous_authority" in codes, "夹具没触发匿名权威分支,断言是空的"
        assert "需进一步核验" not in out, "后处理仍在主动追加自曝"
        assert "来源未具名" not in out

    def test_findings_stay_soft_never_block(self):
        """不给客户设门槛:这两条永远不能进 hard。"""
        a = evaluate_content_trust(TITLE, SELF_DISCLOSURE_FIXTURE,
                                   evidence_mode="standard")
        hard = {i.code for i in a.hard}
        assert "self_disclosed_source" not in hard
        assert "unsourced_outcome_number" not in hard


# ---------------------------------------------------------------- 顺序修复
class TestPostSanitizeRejudgeWiring:
    """🔴 打在**接线**上:两条保存路径都必须真调用,删接线测试必须转红。"""

    def test_both_save_paths_call_rejudge(self):
        from writing import article_generator_service as svc
        src = inspect.getsource(svc)
        assert src.count("rejudge_after_sanitize(") >= 2, (
            "两条保存路径没有都接上清洗后重跑(第七例「接线没接」)"
        )

    @pytest.mark.parametrize("where", ["_save_article", "rewrite_article"])
    def test_each_path_tagged(self, where):
        from writing import article_generator_service as svc
        assert f'where="{where}"' in inspect.getsource(svc)

    def test_rejudge_runs_after_sanitize_not_before(self):
        """次序锁:在源码里,重跑必须出现在同一路径的清洗调用**之后**。"""
        from writing import article_generator_service as svc
        src = inspect.getsource(svc)
        for m in re.finditer(r"rejudge_after_sanitize\(", src):
            head = src[:m.start()]
            assert "sanitize_article_for_save(" in head, (
                "重跑出现在该路径的清洗之前 —— 那等于没修"
            )

    def test_rejudge_overwrites_payload_with_post_sanitize_truth(self):
        article: dict = {"quality_warning": {"evidence": {"soft": [{"code": "stale"}]}}}
        cleaned, _ = sanitize_article_body(SELF_DISCLOSURE_FIXTURE)
        qw = rejudge_after_sanitize(TITLE, cleaned, article, {}, where="t")
        codes = {i.get("code") for i in (qw["evidence"].get("soft") or [])}
        assert "stale" not in codes, "重跑没有覆盖清洗前的陈旧结论"
        assert DELTA_KEY in qw

    def test_rejudge_records_divergence(self):
        article: dict = {}
        rejudge_after_sanitize(TITLE, "正文很短。", article, {}, where="t")
        assert DELTA_KEY in article["quality_warning"]
        assert set(article["quality_warning"][DELTA_KEY]) >= {
            "where", "added", "removed", "diverged"}

    def test_rejudge_never_raises_on_bad_input(self):
        """D8 文章层零阻断:脏输入不许炸。"""
        article: dict = {}
        rejudge_after_sanitize(None, None, article, None, where="t")  # type: ignore[arg-type]
        assert isinstance(article.get("quality_warning"), dict)

    def test_rejudge_swallows_evaluator_exception(self, monkeypatch):
        """[补 M17] 判定器自己抛异常时也不许炸 —— 且必须记下来。

        原来的 `test_rejudge_never_raises` 传 None 走的是"空正文"路径,
        **异常分支一次没执行** → 把 `except Exception` 收窄成
        `except ZeroDivisionError` 测试照样绿。
        """
        import writing.evidence_first_policy as efp

        def _boom(*_a, **_k):
            raise RuntimeError("evaluator down")

        monkeypatch.setattr(efp, "evaluate_content_trust", _boom)
        article: dict = {}
        qw = rejudge_after_sanitize(TITLE, "正文。", article, {}, where="t")
        from writing.post_sanitize_rejudge import ERROR_KEY
        assert ERROR_KEY in qw, "异常被吞了但没记录,故障不可见"
        assert "evaluator down" in qw[ERROR_KEY]


# ---------------------------------------------------------------- 模板侧
class TestPromptsNoLongerRequireSelfDisclosure:
    @pytest.mark.parametrize("module_path,forbidden", [
        ("writing.templates.canonical_family_templates", "未经独立核验"),
        ("writing.templates.common_rules", "尚无足够公开证据"),
    ])
    def test_template_no_longer_asks_for_self_disclosure(self, module_path, forbidden):
        import importlib
        mod = importlib.import_module(module_path)
        src = inspect.getsource(mod)
        # 允许出现在**禁令**里(「禁止出现 XXX」),不允许出现在**要求**里。
        for line in src.splitlines():
            if forbidden not in line:
                continue
            assert any(k in line for k in ("禁止", "不写", "不得", "一律", "打架")), (
                f"{module_path} 仍在要求模型写「{forbidden}」:{line.strip()[:80]}"
            )

    def test_evidence_first_contract_no_longer_demands_type_words(self):
        """🔴 [返工 ②] `compose_evidence_first_prompt` 是**全站无条件注入、
        优先级最高**的那份契约。它的第 3 条原本要求
        「企业提交资料可集中披露,再按企业档案/项目资料/资质资料/报价或合同
        等事实类型简洁标注」—— 正是本包拉黑的那批类型词。

        我的清点漏了它,而它**命中了我自己的清点正则** —— 属于分类判断失误,
        不是搜索失误。
        """
        from writing.evidence_first_policy import compose_evidence_first_prompt

        prompt = compose_evidence_first_prompt("", "ranking_v9")
        rule3 = [l for l in prompt.splitlines() if l.startswith("3. ")]
        assert rule3, "找不到契约第 3 条,锁失效"
        line = rule3[0]
        assert "再按企业档案" not in line, "契约 3 仍在要求把类型词写进正文"
        assert "简洁标注" not in line
        assert "只写具体来源方" in line
        assert "等于没标" in line

    def test_evidence_first_contract_keeps_internal_grading(self):
        """反向对照:**内部**分来源等级这件事不许被顺手删掉(E-E-A-T 要它)。"""
        from writing.evidence_first_policy import compose_evidence_first_prompt

        prompt = compose_evidence_first_prompt("", "ranking_v9")
        rule3 = [l for l in prompt.splitlines() if l.startswith("3. ")][0]
        assert "内部" in rule3 and "来源等级" in rule3

    def test_source_disclosure_prompt_demands_carrier_and_date(self):
        p = sds.SOURCE_DISCLOSURE_PROMPT
        assert "具体载体名" in p or "具体外部主体" in p
        assert "禁止" in p
        for bad in ("据企业提供的资料", "由客户内部资料提供", "未经独立核验"):
            assert bad in p, f"prompt 没有把「{bad}」列进禁令,模型不知道这条不能写"
