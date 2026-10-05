# -*- coding: utf-8 -*-
"""WO_233-c1 · 评分器封顶的等级,不许在对客读取口被推翻。

现场:Owner 质疑防御型 GEO 打分虚高。
`tools/scoring/funnel_score.calculate_funnel_score` 有一道**过度承诺封顶**:
只有单层覆盖(有效层权重和 < 60)时,重归一会让单层满分变成 100 → 主导级
(从一个数据点宣称市场主导),所以强制降到成长级并置 `level_capped=True`。
而对客唯一读取口 `services/report_v2_score.resolve_canonical_score`
**只拿分数反推等级**,把这道封顶整个抹掉。

🔴 `_warn_on_stored_level_mismatch` 每次都 `logger.warning` 了 —— 然后照样返回重推值。
   出声不拦截,本仓 `a-warning-that-blocks-nothing-is-not-a-fix`。
   所以生产日志里每一个被抬高的案例都留着痕迹,而客户看到的仍是被抬高的等级。

🔴 本包**两侧**都立着:
   放行侧(封顶要活下来)与拒绝侧(存储值**不许抬高**等级)。
   只立放行侧的话,把 `_reconcile_level` 改成「一律信存储值」也会全绿 ——
   而那正是模块抬头那句「persisted level text is never trusted」要防的事。

════════════════════════════════════════════════════════════════════
🔴🔴 **本组现在只对「存量形状」承重 —— 为什么改指,写在这里**
════════════════════════════════════════════════════════════════════
同一班次的后一笔 c3(分范围交付)把单层覆盖改成**不出总分与等级**。
而封顶只在 `effective_weight_sum < 60`(= 单层覆盖)时触发
⇒ **`level_capped=True` 在活路径上已不可达**,本组主判据对新报告完全不承重。
它仍然承重的地方是**存量报告**:c3 之前写下的 `level_meta` 没有 `scope_limited` 键,
仍走 c1 这条 reconcile 路,封顶必须在它们身上继续活着(存量报告不静默重写)。

所以本组的 `_row(..., legacy=True)` 不是为了让判据变绿,是**指向变了**。
装上 c3 时这几条当场红过 —— 那不是 c3 写坏,是判据钉的输入被 c3 接管了。

**若将来 c3 被放松**(例如决定单层也该出分),本组要**重新指回活路径**。
那一刻会有人被提醒:`test_scope_limited_delivery.py::
test_level_capped_is_unreachable_on_the_live_path` 会先红。
🔴 判据改指存量是合法动作,但**必须写明为什么改指** ——
   不写的话,下一个人看到一组绿判据,会以为封顶还在保护活路径。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.report_v2_score import resolve_canonical_score          # noqa: E402
from tools.scoring.funnel_score import calculate_funnel_score          # noqa: E402


def _row(calc: dict, *, legacy: bool = False) -> dict:
    """按生产的存法把评分器产出塞进 row(v2 走 report_v2_modules_jsonb.funnel)。

    `legacy=True` 去掉 `scope_limited` 键,模拟 **c3 上线之前写下的存量报告**。

    🔴 为什么要这个开关:c3(分范围交付)之后,**单层覆盖在活路径上根本不出等级了**,
       所以「封顶要活到读取口」这条在活路径上已无从谈起 —— 它现在**只对存量报告承重**。
       老报告的 level_meta 没有 `scope_limited` 键,仍然走 c1 那条 reconcile 路,
       封顶必须继续活着(存量报告不静默重写)。
       第一次把 c3 装上时这几条当场红了 —— 那不是 c3 写坏了,是 c1 的判据钉的
       输入已经被 c3 接管。判据要跟着改指向,而不是把 c3 退回去。
    """
    meta = dict(calc["level_meta"])
    if legacy:
        meta.pop("scope_limited", None)
    return {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "funnel": {
                "total_score": calc["total_score"],
                "level": calc["level"],
                "level_meta": meta,
            }
        },
    }


#: 🔴 单层覆盖三种都要跑。工单表里只有品牌层 —— 只测品牌那一条的话,
#:   有人把封顶做成「只对 brand 生效」我也看不出来。
CAPPED = [
    ("只测品牌 10/10", dict(brand_detected=10, brand_total=10)),
    ("只测品牌 9/10", dict(brand_detected=9, brand_total=10)),
    ("只测决策 6/6", dict(local_detected=6, local_total=6)),
    ("只测场景 5/5", dict(scenario_detected=5, scenario_total=5)),
]

#: 反向对照:这些**不该**被封顶,读取前后必须一致。
NOT_CAPPED = [
    ("双层齐(品牌+决策)", dict(brand_detected=5, brand_total=5,
                                local_detected=5, local_total=5)),
    ("三层齐(全满)", dict(brand_detected=4, brand_total=4, local_detected=4,
                          local_total=4, scenario_detected=4, scenario_total=4)),
    ("三层齐(中等)", dict(brand_detected=2, brand_total=4, local_detected=2,
                          local_total=4, scenario_detected=1, scenario_total=4)),
    ("完全没有观测", dict()),
    ("只测品牌 3/5(分数本就落成长级)", dict(brand_detected=3, brand_total=5)),
]


@pytest.mark.parametrize("name,kw", CAPPED)
def test_a_capped_level_survives_the_read_of_a_legacy_report(name, kw):
    """🔴 主判据(存量报告面):被封顶的样本,读出来仍是封顶后的等级。

    c3 之后这条只对**存量报告**承重 —— 新报告走分范围交付,单层根本不出等级
    (那一侧由 `test_scope_limited_delivery.py` 钉)。老报告不静默重写,
    所以封顶必须在它们身上继续活着。
    """
    calc = calculate_funnel_score(**kw)
    assert calc["level_meta"]["level_capped"] is True, (
        "%s 在评分器那侧就没被封顶 —— 样本没落在缺口上,这条判据是空的" % name)
    got = resolve_canonical_score(_row(calc, legacy=True))
    assert got["level"] == calc["level"], (
        "%s:评分器判 %s(封顶),读出来变成 %s"
        % (name, calc["level"], got["level"]))
    assert got["level_capped"] is True, got


@pytest.mark.parametrize("name,kw", NOT_CAPPED)
def test_an_uncapped_level_reads_back_unchanged(name, kw):
    """反向对照:没被封顶的样本,读取前后逐字一致(修法没顺手改别的)。

    同样按存量形状读:`只测品牌 3/5` 虽然没触发**封顶**(分数本就落成长级),
    但它是单层覆盖,在活路径上会走 c3 的分范围交付 —— 那一侧另有判据。
    这里要问的是「c1 的 reconcile 有没有顺手改坏没封顶的样本」,所以读存量形状。
    """
    calc = calculate_funnel_score(**kw)
    assert calc["level_meta"]["level_capped"] is False, name
    got = resolve_canonical_score(_row(calc, legacy=True))
    assert got["level"] == calc["level"], (name, calc["level"], got["level"])


def test_the_cap_actually_lowers_the_level():
    """🔴 「封顶活下来了」只有在封顶**真的把等级压低过**时才有意义。

    没有这一条,把封顶逻辑本身改成 no-op(`level_capped` 恒 False、等级不降)
    也会让上面每一条绿 —— 两边都不封顶,当然"一致"。
    """
    calc = calculate_funnel_score(brand_detected=10, brand_total=10)
    assert calc["total_score"] == 100, calc["total_score"]
    assert calc["level"] == "成长级", calc["level"]
    from tools.scoring.funnel_score import get_funnel_meta
    assert get_funnel_meta(100)["level"] != "成长级", (
        "按分数反推 100 竟然也是成长级 —— 那封顶什么都没压低,本包全部判据失效")


# ── 拒绝侧:存储值不许**抬高**等级(老约束一字未松)────────────────────

def test_a_stored_level_higher_than_derived_is_refused():
    """🔴 有人把存储等级写成更高的,必须拒绝并回落到反推值。

    这一条钉的是修法的**边界**:我把「一律用反推值」改成「接受不高于反推值的存储值」,
    如果实现成「一律用存储值」,封顶那几条照样绿 —— 只有这条会红。
    """
    row = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "funnel": {"total_score": 40, "level": "主导级",
                       "level_meta": {"level_capped": False}}
        },
    }
    got = resolve_canonical_score(row)
    assert got["level"] != "主导级", got
    assert got["level"] == got["derived_level"], got
    assert got["level_source"] == "derived", got


def test_an_unknown_stored_level_name_is_refused():
    """存储等级不在评分器的等级表里 ⇒ 不认,用反推值。"""
    row = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "funnel": {"total_score": 100, "level": "宇宙无敌级",
                       "level_meta": {"level_capped": True}}
        },
    }
    got = resolve_canonical_score(row)
    assert got["level"] == got["derived_level"], got
    assert got["level_source"] == "derived", got


def test_v1_keeps_the_old_distrust_of_stored_text():
    """v1(5 维旧体系)没有封顶概念,维持老行为:不信存储文本。

    🔴 样本要落在缺口上:存储等级必须是一个**漏斗等级表里认得的、且更低的**名字。
       第一版我写的是 `level="领先"`(v1 体系的名字)—— 那个名字在漏斗表里查不到,
       于是就算 v1 分支被删掉,它也会走「不认识 ⇒ 用反推值」而照样绿。
       注毒台的 P5 当场存活,就是这条样本够不着缺口
       (本仓 a-passing-assertion-can-also-be-a-wrong-invariant 的同族)。
    """
    row = {"report_v2_version": "v1", "total_score": 100, "level": "成长级"}
    got = resolve_canonical_score(row)
    assert got["score_version"] == "v1", got
    assert got["level"] == got["derived_level"], (
        "v1 采用了存储等级 %r —— v1 没有封顶概念,不该信存储文本" % row["level"])
    assert got["level_source"] == "derived", got
    assert got["level_capped"] is False, got


def test_the_reader_hands_out_the_flags_not_just_an_integer():
    """🔴 回包要带封顶/取样标志。

    「只回一个整数、让每个调用方各自推等级」正是本缺陷的成因;
    把标志交出去,下一个消费方不必再猜这个分能不能拿去说「主导级」。
    """
    calc = calculate_funnel_score(brand_detected=10, brand_total=10)
    got = resolve_canonical_score(_row(calc))
    for key in ("level_source", "derived_level", "level_capped",
                "partial_sample", "score_version"):
        assert key in got, "回包缺 %s:%r" % (key, got)
    assert got["partial_sample"] is True, got
    assert got["score_version"] == "v2", got


def test_old_keys_are_untouched():
    """老键一个没动 —— 两个真实调用方(server.py / share_api.py)读的是它们。"""
    calc = calculate_funnel_score(brand_detected=4, brand_total=4,
                                  local_detected=4, local_total=4,
                                  scenario_detected=4, scenario_total=4)
    got = resolve_canonical_score(_row(calc))
    assert got["total_score"] == 100 and got["level"] == "主导级"
    assert got["source"] == "v2_funnel"


def test_the_two_level_vocabularies_are_disjoint():
    """🔴 钉住那条让 v1 分支得以「冗余」的前提。

    注毒台的 P5(把 `if not is_v2:` 删掉)**存活**了。我查过原因:
    v1 的等级名(领先/成熟/成长/起步/待提升/空白)与漏斗等级名
    (主导级/健康级/成长级/边缘级/危急级/隐形级)**完全不相交**,
    所以 v1 的存储等级永远查不到 rank,永远落到「不认识 ⇒ 用反推值」——
    那道分支删了也不改变任何结果。
    这是本仓 `a-poison-on-a-redundant-guard-survives-though-the-lock-has-teeth`,
    不是判据漏了。

    但「冗余」是**当前词表**的性质,不是永久事实:哪天有人把某个 v1 等级
    改名成「成长级」,那道分支立刻变成承重墙,而它是**悄悄**变的。
    所以钉的不是分支,是**让它冗余的那个前提**。
    """
    from tools.scoring.funnel_score import _LEVELS_SORTED
    from tools.scoring import scoring_levels as sl

    funnel_names = {name for name, _lo, _hi in _LEVELS_SORTED}
    v1_names = {sl.get_level(s) for s in range(0, 101)}
    overlap = funnel_names & v1_names
    assert not overlap, (
        "两套等级词表开始重叠了:%s —— `report_v2_score` 里那道 `if not is_v2` "
        "分支从此是承重的,请给它单独补一条行为判据" % sorted(overlap))
