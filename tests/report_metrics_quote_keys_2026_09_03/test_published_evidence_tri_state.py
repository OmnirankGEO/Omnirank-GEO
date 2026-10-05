"""#54/#55 · 「已发布证据」三态 + 退出分母
(工单 `WO_C54_55_PUBLISHED_EVIDENCE_2026-09-04.md`,Owner 拍板「按建议」)。

## 改之前是什么样

完整度里有一项标签写着「**已发布**证据 ≥ 1 条」(权重 4,组 `evidence_inputs` 权重 25),
数据源却是 `quotes.total_articles` —— 那是**报价里承诺的篇数**,不是「已发布」。
而且该列由主力选词链路建单时硬编码为 0 且全流程从不回写 ⇒ **这一格实际恒缺失**。

更糟的是**首诊**:报价在诊断**之后**才产生,所以首诊时这一格结构性永远拿不到分。
⇒ 系统性地对每一份首诊报告扣一次注定要扣的分。

## 四态(Review 2026-09-04 裁定 · ② 曾裁「按品牌全部报价汇总」,**已撤回**)

| 态 | `published` | 处置 |
|---|---|---|
| 无报价 | `{count:None, available:False, reason:"no_quote"}` | **退出分母**,文案「报价后开始统计」 |
| 不可用 | `{count:None, available:False, reason:其它}` | **退出分母**,但 reason 与上一档**分开** |
| 0 篇 | `{count:0, available:True}` | 记缺失 |
| ≥1 篇 | `{count:n>0, available:True}` | 满足 |

口径:**quote-scoped**,报价按 `quotes.diagnosis_id = 本次诊断` 取
(多张时 `created_at DESC, id DESC` 确定性取一);已发布篇数**复用**
`services/publication_stage_adapters.quote_published_active(quote_id, cursor=cur)` ——
它内部覆盖四条发布链、状态集、cutoff、撤稿回落与按 unit_key 去重。

🔴 **为什么不按品牌汇总**:`brand_quote_projections` 的 docstring 逐字写着
「绝不能加返回合并后品牌总数的便捷函数 …… Q1 的成果就又会出现在 Q2 的门户上」,
同模块 `assert_no_brand_merge_helper()` 是可执行的形态锁。
在调用方做品牌汇总 = 在锁外复刻被锁的形态。

🔴 **为什么不读 `articles.first_published_at`**:schema 注释自己写着它是
denormalized 加速列、事实源是 `media_publications`;它只被两条链写、只增不减,
代发链与插件链根本不写 ⇒ 只走代发的客户会被读成 0(少算),撤稿后不回退(多算)。

## 🔴 这一族判据在防什么

**① `None` 与 `0` 被压成一档。** `if published_count:` 会让两者同走一条路,
   而它们的处置**正好相反**(退出分母 vs 记缺失)。这是本单的头号毒。
**② 退出分母退成了「送分」或「扣分」。** 正确行为是**重新归一化**:
   该项权重从 `field_total` 里减掉,其余项按比例吸收满这一组的权重,
   **一个权重常量都不改**。
**③ 又回去读 `quotes.total_articles`。** 反臂:往 quote 里塞任意 `total_articles`,
   结论必须**一字不变**。
**④ 只改了纯函数、没接线。** 接线锁枚举 `assemble_diagnosis_report_v2` 的全部调用点。

## 关于「满足」那一态可能在生产上不可达

前置门 `scripts/audit_published_evidence_positive_sample_2026_09_04.sql` 会给出读数。
Review 已裁:**Q6 为 0 行不否决本单** —— #55(首诊分母修复)独立成立,
「满足」臂记「生产当前不可达,夹具证逻辑」。**本文件里那一臂是夹具证的,不是生产实证的**,
这句话必须留着,否则下一个人会以为它被生产数据验证过。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

from services.report_metrics import (
    COMPLETENESS_GROUPS,
    DEFERRED_FIELD_NOTE,
    DEFERRED_FIELD_NOTE_UNAVAILABLE,
    compute_data_completeness,
)

ROOT = Path(__file__).resolve().parents[2]

#: 那一项所在的组与权重 —— 从代码现取,不手抄。
GROUP_KEY = "evidence_inputs"
FIELD_KEY = "publications_count"


def _group():
    for g in COMPLETENESS_GROUPS:
        if g["key"] == GROUP_KEY:
            return g
    raise AssertionError(f"组 {GROUP_KEY} 不见了 —— 本文件的前提没了")


NO_QUOTE = {"count": None, "available": False, "reason": "no_quote"}
UNAVAILABLE = {"count": None, "available": False, "reason": "projection_unavailable"}
def PUB(n):
    return {"count": n, "available": True, "reason": None}


def _state(**kw):
    """返回 (score, 是否记缺失, 是否退出分母)。"""
    out = compute_data_completeness(**kw)
    missing = {m["key"] for g in out["groups"] for m in g.get("missing_fields", [])}
    deferred = {d["key"] for g in out["groups"] for d in g.get("deferred_fields", [])}
    return out["score"], FIELD_KEY in missing, FIELD_KEY in deferred


#: 让 `evidence_inputs` 组里**除了**目标项之外全部达标的输入 ——
#: 只有这样才看得见「退出分母之后其余项吸收了权重」。全空时三态分数都是 0,零区分力。
FULL_OTHERS = dict(
    brand={"competitors_jsonb": [{"name": "A"}, {"name": "B"}, {"name": "C"}]},
    diagnosis={"ai_total_tests": 20, "web_brand_direct_count": 2},
)


def test_group_shape_is_what_this_file_assumes():
    """前提锁:组权重与该字段权重必须是本文件假设的那样。

    **红了说明什么**:有人改了权重表 ⇒ 下面所有关于分数的断言都要重算,
    而不是把断言改到"现在的数"上。
    """
    g = _group()
    weights = {k: w for k, _l, w in g["fields"]}
    assert FIELD_KEY in weights, f"{FIELD_KEY} 不在 {GROUP_KEY} 里了"
    assert g["weight"] == 25 and weights[FIELD_KEY] == 4, (
        f"组权重={g['weight']} 该字段权重={weights[FIELD_KEY]} —— 与本文件假设不符")
    assert sum(weights.values()) == g["weight"], (
        "字段权重之和 != 组权重 —— 归一化的基准变了,下面的分数断言要重算")


# ── 三态三臂 ──────────────────────────────────────────────────────
def test_state_no_quote_leaves_the_denominator():
    """无报价 ⇒ 退出分母:不记缺失,且其余项吸收满整组权重。

    **红了说明什么**:要么退出没生效(仍记缺失=旧行为),
    要么退出退错了方向(既不记缺失也不重新归一化 ⇒ 白扣 4 分)。
    """
    score, missing, deferred = _state(published=NO_QUOTE, **FULL_OTHERS)
    assert deferred and not missing, f"deferred={deferred} missing={missing}"
    assert score == 25, (
        f"其余三项都达标,退出分母后该组应拿满 25,实得 {score} —— 归一化没生效")


def test_state_has_quote_zero_published_is_missing():
    """有报价 0 篇 ⇒ 记缺失(**不是**退出分母)。

    **红了说明什么**:`None` 与 `0` 被压成同一档 —— 这是本单的头号毒。
    """
    score, missing, deferred = _state(published=PUB(0), **FULL_OTHERS)
    assert missing and not deferred, f"missing={missing} deferred={deferred}"
    assert score == 21, f"该扣掉这一项的 4 分,实得 {score}"


def test_state_has_quote_published_is_satisfied():
    """有报价 ≥1 篇 ⇒ 满足。

    🔴 **这一臂是夹具证的,不是生产实证的** —— 前置门若读到生产上一个正样本都没有,
       「满足」在生产上不可达。那不否决本单(#55 独立成立),但**不许把这条读成
       「生产上验过了」**。
    """
    score, missing, deferred = _state(published=PUB(3), **FULL_OTHERS)
    assert not missing and not deferred
    assert score == 25


def test_the_three_states_are_actually_three():
    """三态必须给出**三种**读数组合,否则上面任一条绿都可能是"都一样"造成的。"""
    seen = {_state(published=v, **FULL_OTHERS)[1:] for v in (NO_QUOTE, PUB(0), PUB(3))}
    assert len(seen) == 3 or (len(seen) == 2 and _state(published=NO_QUOTE, **FULL_OTHERS)[0]
                              != _state(published=PUB(0), **FULL_OTHERS)[0]), (
        f"三态没有三种可区分的结果:{seen}")


def test_state_unavailable_is_deferred_but_with_a_different_reason():
    """投影不可用 ⇒ 也退出分母,但 **reason 必须与 no_quote 分开**。

    **红了说明什么**:「还没到该有的时候」与「我们没查到」被压成同一句话 ——
    她会以为系统知道答案(知道是 0),而其实我们不知道。
    这两档的**处置相同、含义相反**,压档不会有任何症状,只能靠这条抓。
    """
    score, missing, deferred = _state(published=UNAVAILABLE, quote={"id": 1}, **FULL_OTHERS)
    assert not missing and score == 25, f"missing={missing} score={score}"
    reasons = {d["reason"] for g in compute_data_completeness(
        published=UNAVAILABLE, quote={"id": 1}, **FULL_OTHERS)["groups"]
        for d in g.get("deferred_fields", [])}
    assert reasons and "no_quote" not in reasons, f"reason={reasons} 与无报价混同了"


# ---------------------------------------------------------------------------
# 🔴 上面那条**杀不掉**「reason 恒定」这种改动,Review 毒②实测 28 全绿存活。
#    机制:它只驱动 `unavailable` 一臂,断言还是**否定式**(`"no_quote" not in`)——
#    把 reason 恒定成 "unavailable" 时它照样成立,而 `no_quote` 那一臂的 reason
#    **从来没有人验过**。半边谓词没人验,压档就没有任何症状。
#    下面三条按「两臂各自钉死 + 一条反臂钉死两者不等」补齐。
#    🔴 文案一律从 `DEFERRED_FIELD_NOTE` / `DEFERRED_FIELD_NOTE_UNAVAILABLE` 读:
#       手写在夹具里的对客文案 = 判据在跟我自己对话,改了登记表也不会红。
# ---------------------------------------------------------------------------

def _deferred_entry(**kw) -> dict:
    """取出目标字段那一条 deferred 记录(必须恰好一条)。"""
    got = [d for g in compute_data_completeness(**kw)["groups"]
           for d in g.get("deferred_fields", []) if d["key"] == FIELD_KEY]
    assert len(got) == 1, f"deferred 里 {FIELD_KEY} 出现 {len(got)} 次,期望 1"
    return got[0]


def test_no_quote_carries_its_own_reason_and_note():
    """`no_quote` ⇒ reason 必须是 no_quote,文案必须是「还轮不到统计」那句。

    **红了说明什么**:reason 没有按入参透传(被就地编造/压成常量),
    于是 note 查表落到兜底句 —— 她会以为系统**知道**答案(知道是 0),
    而其实这次诊断名下压根还没有报价单。处置相同、含义相反,不会有别的症状。
    """
    d = _deferred_entry(published=NO_QUOTE, **FULL_OTHERS)
    assert d["reason"] == "no_quote", f'reason={d["reason"]!r}'
    # 期望值按**入参状态**取键,不读输出的 reason —— 读输出就成了自构中间值。
    assert d["note"] == DEFERRED_FIELD_NOTE[(FIELD_KEY, "no_quote")], f'note={d["note"]!r}'


def test_unavailable_carries_its_own_reason_and_note():
    """`unavailable` ⇒ reason 必须原样透传调用方给的那个,文案走兜底句。

    **红了说明什么**:reason 被就地编造(而不是透传)⇒ 上游区分
    `projection_unavailable` / `lookup_failed` / `<脚本>_no_cursor` 的努力全部作废,
    排查时只看得到一个笼统的 "unavailable",而这几种起因的处置完全不同。
    """
    d = _deferred_entry(published=UNAVAILABLE, quote={"id": 1}, **FULL_OTHERS)
    assert d["reason"] == UNAVAILABLE["reason"], f'reason={d["reason"]!r} 没有透传'
    assert d["note"] == DEFERRED_FIELD_NOTE_UNAVAILABLE, f'note={d["note"]!r}'


def test_the_two_deferred_states_never_collapse_into_one():
    """🔁 反臂:两个退出分母态的 **reason 与 note 都不许相等**。

    这两态在**分数上完全同形**(都退出分母、都 25 分),
    所以它们被压成一档时,除了这条以外没有任何判据会红。
    第二对驱动的是 `reason` 缺省时的兜底推导支
    (`published.get("reason") or (无报价 ? no_quote : unavailable)`)——
    那一支是唯一会**自己造** reason 的地方,压档最容易发生在那里。
    """
    for label, a, b in (
        ("显式 reason",
         dict(published=NO_QUOTE, **FULL_OTHERS),
         dict(published=UNAVAILABLE, quote={"id": 1}, **FULL_OTHERS)),
        ("缺省 reason 的兜底推导",
         dict(published={"count": None, "available": False}, **FULL_OTHERS),
         dict(published={"count": None, "available": False}, quote={"id": 1}, **FULL_OTHERS)),
    ):
        da, db = _deferred_entry(**a), _deferred_entry(**b)
        assert da["reason"] != db["reason"], f'{label}:reason 压成同一档 {da["reason"]!r}'
        assert da["note"] != db["note"], f'{label}:note 压成同一句 {da["note"]!r}'


def test_the_fallback_derivation_points_the_right_way():
    """兜底推导支必须**指对方向**,不能只证"两边不一样"。

    🔴 上面那条反臂对「方向对调」**结构性失明**:不等式只证两者不同,
    不证哪个是哪个 —— 把兜底支写成
    `("unavailable" if not quote else "no_quote")`(两态互换)之后,
    reason 仍不等、note 仍不等,那条照样绿(Review 毒③实测 31 全绿存活)。
    而上面两条正面钉死的判据驱动的都是**显式 reason**(走透传路径),
    兜底推导支一次都没被它们碰到 —— 于是唯一会自己造 reason 的那条支,
    方向上没有任何锁。

    对调的对客后果正好相反:这次诊断名下**还没有报价单**(还轮不到统计)
    会被说成「这项暂时统计不到」—— 她会以为系统查过了、没查着;
    反过来,投影真的挂了却说「报价后开始统计」—— 她会去等一个永远不来的东西。

    期望值按**入参状态**取(有没有 quote),不读输出的 reason 反查。
    """
    no_reason = {"count": None, "available": False}   # 刻意不给 reason ⇒ 逼它走兜底推导
    d_no_quote = _deferred_entry(published=no_reason, **FULL_OTHERS)
    d_unavail = _deferred_entry(published=no_reason, quote={"id": 1}, **FULL_OTHERS)

    assert d_no_quote["reason"] == "no_quote", (
        f'无报价时兜底推出 {d_no_quote["reason"]!r},方向反了')
    assert d_unavail["reason"] == "unavailable", (
        f'有报价但投影不可用时兜底推出 {d_unavail["reason"]!r},方向反了')
    # note 一并钉:防止有人只改文案表把方向问题藏进去
    assert d_no_quote["note"] == DEFERRED_FIELD_NOTE[(FIELD_KEY, "no_quote")]
    assert d_unavail["note"] == DEFERRED_FIELD_NOTE_UNAVAILABLE


def test_required_param_makes_an_unwired_caller_fail_loudly():
    """漏传 `published` 必须当场 TypeError,不能有默认值。

    **红了说明什么**:有人给它加了默认值 ⇒ 漏改的调用点会**安静地**让该项退出分母,
    把分数拉高,而且零报错零日志 —— 那是一个不会提问的绿。
    """
    with pytest.raises(TypeError):
        compute_data_completeness(**FULL_OTHERS)


# ── 反臂:禁读 quotes.total_articles ────────────────────────────────
@pytest.mark.parametrize("noisy", [
    {"total_articles": 99}, {"total_articles": 0}, {"total_articles": None},
    {"publications_count": 7}, {},
])
def test_quote_content_never_changes_the_verdict(noisy):
    """往 quote 里塞任何东西,三态结论**一字不变**。

    **红了说明什么**:又有人把「已发布」接回 quote 上的某个列。
    那是「报价承诺」不是「已发布」,标签会再一次名不副实。
    """
    base = _state(published=PUB(0), **FULL_OTHERS)
    assert _state(published=PUB(0), quote=noisy, **FULL_OTHERS) == base, (
        f"quote={noisy} 改变了结论 ⇒ 已发布证据又从 quote 取数了")


def test_total_articles_is_not_read_anywhere_in_report_metrics():
    """静态锁:`report_metrics.py` 里不许再出现读 `total_articles` 的取值。

    走 AST 取 `.get("...")` 的字面量键,**不 grep** ——
    注释与 docstring 里为了解释历史会提到这个词(本文件上面就提了好几次),
    裸串锁会被那些散文判红。
    """
    src = io.open(ROOT / "services" / "report_metrics.py", encoding="utf-8").read()
    keys = [
        n.args[0].value
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get" and n.args
        and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
    ]
    assert "total_articles" not in keys, (
        "report_metrics 又在读 total_articles —— 本单裁定禁读(它是报价承诺篇数)")
    assert keys, "一个 .get(...) 都没抽到 —— 提取器坏了,不是「没有读取」"


# ── 接线锁 ────────────────────────────────────────────────────────
#: 🔴 手写分母漏掉的那一项不会让任何判据变红 ⇒ 由 AST 机械枚举调用点反查。
#:    参数已是 required ⇒ 漏改本会 TypeError;这条锁多一层:
#:    **连"传了但传的是常量 unavailable"也要看得见**,免得有人为了让它跑起来
#:    随手塞一个不可用值,把该项永久排除在分母外而没人发现。


def _assemble_call_sites() -> dict[str, str]:
    """全仓(排除 tests/)`assemble_diagnosis_report_v2(...)` 调用点 → published 实参的形态。"""
    out: dict[str, str] = {}
    for d in ("services", "workflows", "api", "scripts", "db", "tools"):
        base = ROOT / d
        if not base.is_dir():
            continue
        for f in base.rglob("*.py"):
            try:
                tree = ast.parse(io.open(f, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                if getattr(n.func, "id", getattr(n.func, "attr", None)) != "assemble_diagnosis_report_v2":
                    continue
                kw = {k.arg: k.value for k in n.keywords}
                rel = f.relative_to(ROOT).as_posix()
                if "published" not in kw:
                    out[rel] = "missing"
                elif isinstance(kw["published"], ast.Dict):
                    out[rel] = "literal"      # 脚本:显式常量 unavailable
                else:
                    out[rel] = "computed"     # 主链路:查出来的
    return out


#: 主链路必须是**查出来的**;脚本允许显式常量(它们没有可透传的游标)。
MAIN_PATHS = {
    "services/diagnosis_identity_decision.py",
    "workflows/diagnosis_workflow.py",
}
SCRIPT_PATHS = {
    "scripts/backfill_mention_count_2026_08_08.py",
    "scripts/rebuild_parenthetical_false_mentions_2026_08_06.py",
    "scripts/regen_v2_reports.py",
}


def test_every_call_site_passes_published():
    """五个调用点一个都不许漏。

    **红了说明什么**:新增了一条装配路径没接线 ——
    required 参数会让它 TypeError,但那要**跑到**才炸;这条在静态就能看见。
    """
    sites = _assemble_call_sites()
    assert sites, "一个调用点都没抽到 —— 提取器坏了,不是「没人调用」"
    missing = sorted(p for p, kind in sites.items() if kind == "missing")
    assert not missing, f"这些调用点没传 published:{missing}"


def test_main_paths_compute_it_rather_than_hardcoding_unavailable():
    """主链路必须**查**,不许塞常量。

    **红了说明什么**:有人为了让主链路跑起来,直接传了个常量 unavailable ——
    那会让「已发布证据」在**所有**真实报告上永久退出分母,
    分数一律偏高,而且不会红、不会报错、不会有日志。
    """
    sites = _assemble_call_sites()
    for p in sorted(MAIN_PATHS):
        assert sites.get(p) == "computed", (
            f"{p} 的 published 是 {sites.get(p)},主链路必须是查出来的")
    for p in sorted(SCRIPT_PATHS):
        assert sites.get(p) in ("literal", "computed"), f"{p} 未接线:{sites.get(p)}"


def test_assembly_path_passes_the_cursor_instead_of_opening_a_connection():
    """🔴 装配路径内调 `quote_published_active` 必须透传 `cursor=`。

    **红了说明什么**:不传 cursor 会自开连接,而这两条路都是**先写后装配**、
    人还在事务里 —— `publication_stage_adapters.py:55` 逐字记着
    「本仓 08-10 为此把生产打成过 503」。这条锁的是那次事故的形态,不是风格。
    """
    for rel in sorted(MAIN_PATHS):
        tree = ast.parse(io.open(ROOT / rel, encoding="utf-8").read())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", None)) == "quote_published_active"]
        assert calls, f"{rel} 没有调 quote_published_active —— 接线断了"
        for c in calls:
            assert any(k.arg == "cursor" for k in c.keywords), (
                f"{rel}:{c.lineno} 调 quote_published_active 没传 cursor= ⇒ 会自开连接")


# ── 报价选取锁(毒 A 要抓的那一格)──────────────────────────────────
#: 🔴 第一版判据漏了这一整格:我把「按 diagnosis_id 取报价」改回「按 brand_id 取最新一张」,
#:    24 条判据**全绿**。改动本身是对的,但**没有任何东西守着它** ——
#:    而它恰恰是本单最容易被下一个人"顺手改回去"的一行(旧写法在仓里到处都是)。


def _quotes_select_literals(rel: str) -> list[tuple[int, str]]:
    """该文件里所有含 `FROM quotes` 的 SQL 字面量(行号, 规整后的一行)。"""
    tree = ast.parse(io.open(ROOT / rel, encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "FROM quotes" in n.value:
            out.append((n.lineno, " ".join(n.value.split())))
    return out


@pytest.mark.parametrize("rel", sorted(MAIN_PATHS))
def test_report_quote_is_selected_by_diagnosis_not_by_brand(rel):
    """主链路取报价必须按 `diagnosis_id`,不许按 `brand_id` 取「最新一张」。

    **红了说明什么**:退回了「该品牌最新一张」——那会把**另一次服务**的报价
    算到这份报告头上(与 WP7 修掉的 brand max merge 同一个病:
    Q1 的成果出现在 Q2 的门户上)。这里的后果是「已发布证据」与竞品回落都取错对象。
    """
    lits = _quotes_select_literals(rel)
    assert lits, f"{rel} 里一个 FROM quotes 字面量都没抽到 —— 提取器坏了,不是「没有查询」"
    bad = [(ln, sql) for ln, sql in lits if "brand_id" in sql]
    assert not bad, f"{rel} 按 brand_id 取报价:{bad}"
    assert all("diagnosis_id" in sql for _ln, sql in lits), (
        f"{rel} 有 FROM quotes 没按 diagnosis_id 过滤:{lits}")


@pytest.mark.parametrize("rel", sorted(MAIN_PATHS))
def test_single_quote_pick_is_deterministic(rel):
    """取单张(`LIMIT 1`)时必须有确定性排序,否则「取哪张」由数据库心情决定。

    **红了说明什么**:同一次诊断名下可能有多张报价(改价/重签),
    没有 ORDER BY 时同一份数据两次读可能给出不同的报价 ⇒ 完整度会飘,
    而飘出来的差异**看起来像数据变了**,查起来极贵。
    """
    for ln, sql in _quotes_select_literals(rel):
        if "LIMIT 1" not in sql:
            continue
        assert "ORDER BY" in sql, f"{rel}:{ln} 有 LIMIT 1 却没有 ORDER BY:{sql}"
        assert "id DESC" in sql or "id ASC" in sql, (
            f"{rel}:{ln} 的 ORDER BY 没有以 id 收尾 ⇒ 同 created_at 时仍不确定:{sql}")
