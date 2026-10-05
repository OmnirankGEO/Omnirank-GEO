"""Gate-2 改写措施全量落地包 · 判别测试。

## 判据打在哪

Owner 点名:**措施标签的落库要有接线锁 —— 打在 INSERT 参数上,别再犯"函数对了没接线"**。
所以本份的主判据是:跑真保存流程,断言 `INSERT INTO articles` 的
**`generation_request_snapshot` 那个参数**里带着 `gate2_measures`。

本仓的「接线没接」已经栽过**五次**(最近一次就在上一批:兑现闸改 `article["content"]`、
保存路径全程用局部 `_content`,标题侧全绿、正文侧空转)。所以:

* 断言取 INSERT 参数元组的**第 13 位**(generation_request_snapshot),不是 helper 返回值;
* 配反向对照:**没注入时 `injected` 必须是 False、`applied` 必须是空** ——
  少了这条,一个"在 lineage 里无脑写 injected=True"的实现照样能让主判据全绿,
  而那正好是给归因喂假数据的形态(归因是这次唯一的效果判据)。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_c4_review_autopilot_2026_07_27 import (  # noqa: E402
    _make_service,
    _wire_fake_db,
)
from writing.gate2_rewrite_measures import (  # noqa: E402
    CHECKERS,
    GATE2_MEASURE_SET_VERSION,
    MEASURES,
    WEAK_CODES,
    build_gate2_measure_block,
    build_gate2_measure_tag,
    observe_gate2_measures,
    validate_gate2_measures,
)

BRAND = "测试品牌"
#: INSERT 参数里 generation_request_snapshot 的位置(按 INSERT 列顺序数出来的)。
SNAPSHOT_IDX = 13


# ===========================================================================
# 一、措施集自身
# ===========================================================================

def test_measure_set_is_self_consistent():
    assert validate_gate2_measures() == []


def test_every_source_measure_from_the_delivery_is_covered():
    """K1-K5 / F1-F5 十条**一条都不许丢** —— Owner 明示"全量进写作链"。"""
    covered = {s for m in MEASURES for s in m.source_measures}
    expected = {f"K{i}" for i in range(1, 6)} | {f"F{i}" for i in range(1, 6)}
    assert covered == expected, f"漏了:{expected - covered};多了:{covered - expected}"


def test_weak_measures_have_no_hard_product_gate():
    """🔴 弱依据措施(Q2 那两条信号)**不许**有确定性产物判据。

    它们依据不足(0/11 vs 5/20,不显著)。给它们写判据 = 把未证实的方向固化成硬约束,
    与"只挂实验标签、不进方案依据"直接冲突。
    """
    assert set(WEAK_CODES) == {"G8", "G9"}
    for code in WEAK_CODES:
        assert code not in CHECKERS, f"{code} 依据弱却上了硬判据"


def test_instructions_are_not_keyword_tables():
    """红线 1 元判据:指令里不许出现枚举式词表。"""
    for m in MEASURES:
        for line in m.instruction.splitlines():
            assert line.count("、") < 4, f"{m.code} 的指令看起来是一张词表:{line[:40]}"


# ===========================================================================
# 二、prompt 块
# ===========================================================================

def test_block_renders_every_measure_and_substitutes_brand():
    block = build_gate2_measure_block(BRAND)
    for m in MEASURES:
        assert m.code in block, f"{m.code} 没渲染进 prompt 块"
    assert "{brand}" not in block, "品牌占位符没被替换"
    assert BRAND in block


def test_block_is_empty_without_a_brand_name():
    """拿不到品牌名就不渲染 —— 带着 `{brand}` 进 prompt 比不注入更糟。"""
    assert build_gate2_measure_block("") == ""
    assert build_gate2_measure_block(None) == ""


# ===========================================================================
# 三、产物判据的正反用例(每条都配反例,否则判据可能恒真)
# ===========================================================================

_G1_YES = ("# T\n\n## 测试品牌 档案\n\n- 经营年限:12 年\n- 服务区域:某市及周边\n"
           "- 资质:某某资质\n- 代表场景:某类场景\n\n以上是概况。\n")
_G1_NO = "# T\n\n测试品牌成立 12 年,服务区域覆盖某市,持有某某资质,做过某类场景。\n"
#: 反例二:四行字段齐了,但这块档案**不是这家客户的** —— 判据不许只数行数。
_G1_ANON = ("# T\n\n## 行业概况\n\n- 经营年限:12 年\n- 服务区域:某市及周边\n"
            "- 资质:某某资质\n- 代表场景:某类场景\n\n测试品牌在文末出现。\n")
#: 🔴 真跑实证抓到的**生产真实形态**:模型把客户档案写成两列表格,不是字段行。
#: 第一版判据只认字段行,把这个完全正确的产物判成了 False —— 判据错、产物没错。
_G1_TABLE = ("# T\n\n### 客户档案:测试品牌\n\n| 字段 | 内容 |\n|------|------|\n"
             "| 经营主体 | 测试品牌 |\n| 服务区域 | 某市及周边 |\n"
             "| 经营品类 | 某品类 |\n| 可承接规格范围 | 某范围 |\n"
             "| 资质与依据 | 某依据 |\n| 代表场景 | 某场景 |\n\n后续正文。\n")
#: 表格反例:同样的两列表格,但既不是客户档案、上方小标题也没有客户名。
_G1_TABLE_ANON = ("# T\n\n### 行业口径对照\n\n| 字段 | 内容 |\n|------|------|\n"
                  "| 甲 | 某值 |\n| 乙 | 某值 |\n| 丙 | 某值 |\n| 丁 | 某值 |\n\n"
                  "测试品牌在文末出现。\n")


def test_g1_needs_a_contiguous_field_block():
    assert observe_gate2_measures("T", _G1_YES, BRAND)["G1"] is True
    # 反例:同样的事实写成散文 —— 信息一个不少,但不是**字段块形态**。
    # 这条反例就是 H1 之死的落地形式:我们要的不是"多说几次品牌",是形态。
    assert observe_gate2_measures("T", _G1_NO, BRAND)["G1"] is False
    # 反例二:字段块齐了,但它不是这家客户的档案 —— 判据不许只数行数。
    assert observe_gate2_measures("T", _G1_ANON, BRAND)["G1"] is False


def test_g1_accepts_the_markdown_table_form_production_actually_uses():
    """🔴 生产真实形态是**两列表格**,不是字段行。

    这条是**真跑实证**加上的:措施上线后模型把客户档案写成六行两列表格,
    形态完全正确,而第一版判据只认字段行、把它判成 False。
    少了这条,一个"看着挺严"的判据会在生产上恒假,归因数据全是 False —— 比没有更糟。
    """
    assert observe_gate2_measures("T", _G1_TABLE, BRAND)["G1"] is True
    # 反例:同样的表格形态,但不是这家客户的档案。
    assert observe_gate2_measures("T", _G1_TABLE_ANON, BRAND)["G1"] is False


def test_g1_does_not_count_a_colon_heading_as_a_field_row():
    """`### 客户档案:某某` 是小标题不是字段行 —— 算进去会把真字段块切断。"""
    only_heading = "# T\n\n### 客户档案:测试品牌\n\n正文一段。\n"
    assert observe_gate2_measures("T", only_heading, BRAND)["G1"] is False


def test_g2_needs_the_brand_inside_the_boundary_section():
    yes = "# T\n\n## 适用边界\n\n测试品牌适合某类需求,不适合另一类。\n"
    no = "# T\n\n## 适用边界\n\n选择时要看资质与案例。\n"
    assert observe_gate2_measures("T", yes, BRAND)["G2"] is True
    assert observe_gate2_measures("T", no, BRAND)["G2"] is False


def test_g3_reports_not_applicable_when_there_is_no_region_at_all():
    """🔴 报 n/a 比报 False 诚实 —— 正文里压根没有地域,这条对本篇无解。"""
    yes = "在深圳市做某事的测试品牌。\n"
    no = "测试品牌做某事。深圳市有很多同行。\n"
    none = "测试品牌做某事,没有任何地域信息。\n"
    assert observe_gate2_measures("T", yes, BRAND)["G3"] is True
    assert observe_gate2_measures("T", no, BRAND)["G3"] is False
    assert observe_gate2_measures("T", none, BRAND)["G3"] is None


def test_g4_reads_the_title_not_the_body():
    assert observe_gate2_measures("2026年怎么选", "正文无年份", BRAND)["G4"] is True
    assert observe_gate2_measures("怎么选", "正文写了 2026 年", BRAND)["G4"] is False


def test_g5_needs_brand_and_a_number_in_the_same_sentence():
    yes = "测试品牌承接 2000kg 以内的场景。\n"
    no = "测试品牌承接常见场景。行业内 2000kg 很常见。\n"
    assert observe_gate2_measures("T", yes, BRAND)["G5"] is True
    assert observe_gate2_measures("T", no, BRAND)["G5"] is False


def test_g6_needs_the_brand_inside_the_comparison():
    yes = "# T\n\n| 对象 | 字段 |\n|---|---|\n| 测试品牌 | 某值 |\n| 甲 | 某值 |\n"
    no = "# T\n\n| 对象 | 字段 |\n|---|---|\n| 甲 | 某值 |\n| 乙 | 某值 |\n\n测试品牌也不错。\n"
    assert observe_gate2_measures("T", yes, BRAND)["G6"] is True
    assert observe_gate2_measures("T", no, BRAND)["G6"] is False


def test_observation_never_raises_on_garbage():
    """D8:判据是观察器,不是闸 —— 任何输入都不许炸。"""
    for content in (None, "", "|" * 5000, "#" * 200):
        out = observe_gate2_measures(None, content, BRAND)
        assert set(out) == set(CHECKERS)


# ===========================================================================
# 四、🔴 接线锁:打在 INSERT 参数上
# ===========================================================================

_BODY = ("# 本地装修怎么选\n\n先说结论。\n\n"
         + "\n\n".join(f"## 第 {i} 节\n\n这一节的正文说明。" for i in range(6)))


def _run_save(monkeypatch, *, injected: bool, body: str = _BODY):
    """跑保存路径 1/3,返回 INSERT 的**完整参数元组**。"""
    import writing.article_generator_service as svc_mod

    async def _noop(t, c, topic, article, trust, target_entity=""):  # [R5.1] 契约同签名
        return c, trust

    monkeypatch.setattr(svc_mod, "apply_review_autopilot", _noop)
    monkeypatch.setattr(svc_mod, "_copy_article_distilled_lineage", lambda *a, **k: None)
    inserts: list = []
    _wire_fake_db(monkeypatch, [
        ("SELECT q.brand_id, b.name AS brand_name", None),
        ("SELECT id FROM topics WHERE id=%s FOR UPDATE", {"id": 11}),
        ("COALESCE(MAX(version),0)", {"max_version": 0}),
        ("SELECT COALESCE(q.owner_user_id", None),
        ("SELECT brand_id FROM quotes", None),
    ], inserts)
    service = _make_service()
    monkeypatch.setattr(
        service, "_freeze_topic_delivery_options",
        lambda topic: topic.update(
            {"_effective_add_images": False, "_effective_add_contact": False}
        ),
        raising=False,
    )
    topic = {
        "id": 11, "publication_profile": "standard", "evidence_mode": "unknown",
        "style_code": "buying_guide", "title": "本地装修怎么选", "keyword": "本地装修",
        # 这一位就是生成侧写下的那一位。锁的核心正是"它必须被如实读取"。
        "_gate2_injected": injected,
    }
    article = {
        "topic_id": 11, "title": "本地装修怎么选", "content": body,
        "word_count": len(body), "style": "buying_guide", "publication_profile": "standard",
    }
    assert asyncio.run(service._save_article(topic, article)) == 777
    assert inserts, "必须真的走到 INSERT INTO articles"
    return inserts[0]


def _snapshot(params):
    raw = params[SNAPSHOT_IDX]
    return getattr(raw, "adapted", raw)


def test_insert_param_carries_the_measure_tag(monkeypatch):
    """🔴 主判据:落库的 generation_request_snapshot 里带着措施标签。"""
    snap = _snapshot(_run_save(monkeypatch, injected=True))
    tag = snap.get("gate2_measures")
    assert tag, f"INSERT 参数里没有 gate2_measures:{sorted(snap)}"
    assert tag["version"] == GATE2_MEASURE_SET_VERSION
    assert tag["injected"] is True
    assert set(tag["applied"]) == {m.code for m in MEASURES}
    assert set(tag["weak"]) == set(WEAK_CODES)
    assert set(tag["observed"]) == set(CHECKERS), "产物观测没落库,归因就没有分层依据"


def test_tag_is_honest_when_the_block_was_not_injected(monkeypatch):
    """🔴 反向对照:没注入就必须记 False + 空 applied。

    少了这条,"在 lineage 里无脑写 injected=True" 能让上面那条全绿 ——
    而那正是**给归因喂假数据**的形态。
    """
    snap = _snapshot(_run_save(monkeypatch, injected=False))
    tag = snap["gate2_measures"]
    assert tag["injected"] is False
    assert tag["applied"] == []
    assert tag["weak"] == []
    assert tag["observed"] == {}


def test_snapshot_index_is_the_generation_request_snapshot(monkeypatch):
    """元判据:锁自己取的那一位必须真的是 snapshot 列。

    INSERT 列顺序一改,上面两条会去断言别的列 —— 那时它们会以"字段不见了"的形式
    转红,但排障方向会被带偏。这条把位置本身钉住。
    """
    snap = _snapshot(_run_save(monkeypatch, injected=True))
    assert snap.get("version") == "generation-request-v1.0", "第 13 位不是 snapshot 列了"
    assert "request_id" in snap and "length_contract_version" in snap


# ===========================================================================
# 五、源码级接线(与产物级判据互补:它判"接没接",产物判"生没生效")
# ===========================================================================

def test_generation_path_injects_the_block_into_the_system_prompt():
    from writing.article_generator_service import ArticleGeneratorService

    src = inspect.getsource(ArticleGeneratorService._generate_single)
    # [返修 C8/C14 2026-08-11] 调用形态带上按篇豁免(G8 让位价格规格 /
    # G9 让位客户联系方式开关);锁随形态适配,注入接线断言原样保留。
    assert "build_gate2_measure_block(" in src, "生成链没注入措施块"
    assert "self.brand_name, exempt_codes=tuple(_gate2_exempt)" in src, (
        "措施块调用没带按篇豁免 —— C8/C14 互打会回潮"
    )
    assert "system_prompt = system_prompt + \"\\n\\n\" + _gate2_block" in src, (
        "算出来了但没拼进 system_prompt —— 经典的'函数对了没接线'"
    )
    assert 'topic["_gate2_injected"] = True' in src, "注入了却不记录,标签会永远是 False"
    assert 'topic["_gate2_injected"] = False' in src, "缺默认值,异常路径会漏记"


def test_lineage_reads_the_injected_flag_instead_of_hardcoding_it():
    """🔴 lineage 不许自己造 `injected` —— 必须读生成侧写下的那一位。"""
    src = (ROOT / "writing" / "article_lineage.py").read_text(encoding="utf-8")
    at = src.index("build_gate2_measure_tag(")
    window = src[at:at + 600]
    assert 'topic.get("_gate2_injected")' in window, "没读生成侧的标志"
    assert "injected=True" not in window, "把 injected 写死成 True = 给归因喂假数据"


def test_tag_lands_before_the_insert_in_every_writing_path():
    """元判据:三条落库路径都经 build_article_lineage,所以打标只需一处。

    这条钉住那个前提 —— 哪天有人绕开 lineage 直接 INSERT,它会转红。
    """
    gen = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    tools = (ROOT / "tools" / "article_generator.py").read_text(encoding="utf-8")
    for name, src in (("generator_service", gen), ("tools", tools)):
        for at in _all_insert_positions(src):
            head = src[:at]
            assert "build_article_lineage(" in head, (
                f"{name} 里有一处 INSERT INTO articles 不经 lineage —— 那一处不会打标"
            )


def _all_insert_positions(src: str) -> list[int]:
    out, i = [], 0
    while True:
        i = src.find("INSERT INTO articles", i)
        if i < 0:
            return out
        out.append(i)
        i += 1


def test_fallback_replacement_chain_injects_too():
    """补发降级链自己拼 prompt(经 angle_instruction),同样要注入。"""
    src = (ROOT / "tools" / "article_generator.py").read_text(encoding="utf-8")
    at = src.index("async def _fallback_generate_replacement")
    window = src[at:at + 4000]
    assert "build_gate2_measure_block(" in window, "补发降级链没注入措施块"
    assert "angle_instruction = (angle_instruction or \"\") + \"\\n\\n\" + _g2_block" in window


def test_fallback_does_not_leak_internal_flags_into_the_prompt():
    """🔴 内部标志不许进 `client_data` —— 它会被 json.dumps 成 client_profile 喂给模型。"""
    src = (ROOT / "tools" / "article_generator.py").read_text(encoding="utf-8")
    at = src.index("async def _fallback_generate_replacement")
    window = src[at:at + 4000]
    cd = window.index("client_data = {")
    assert '"_gate2_injected"' not in window[cd:cd + 900], "内部标志泄进了喂给模型的 client_data"


# ===========================================================================
# 六、tag 构造器本身
# ===========================================================================

@pytest.mark.parametrize("injected", [True, False])
def test_tag_shape_is_stable(injected):
    # [R3 订正7 适配 2026-08-11] tag 新增 `exempted` 键:按篇豁免(C8/C14)
    # 如实留痕,applied/weak/observed 不再是全量常量 —— 键集合契约随之 +1。
    tag = build_gate2_measure_tag(title="T", content=_BODY, brand_name=BRAND, injected=injected)
    assert set(tag) == {"version", "injected", "applied", "weak", "exempted", "observed"}
    assert tag["injected"] is injected


# ===========================================================================
# [R3 订正6/7 2026-08-11] 留痕不扣豁免=撒谎(search_hints 同型)+ G8 覆盖面
# ===========================================================================

def test_r3_tag_excludes_exempt_codes_from_all_three_fields():
    """豁免的措施码不许出现在 applied/weak/observed 任何一处;
    exempted 如实记录。反向:未豁免时 G8/G9 照记(能力不废)。"""
    tag = build_gate2_measure_tag(
        title="T", content=_BODY, brand_name=BRAND, injected=True,
        exempt_codes=("G8", "G9"),
    )
    for field in ("applied", "weak"):
        assert "G8" not in tag[field] and "G9" not in tag[field], (
            f"{field} 仍记被豁免的措施 —— 留痕撒谎"
        )
    assert "G8" not in tag["observed"] and "G9" not in tag["observed"]
    assert tag["exempted"] == ["G8", "G9"]
    # 反向对照:不豁免时全量照记
    full = build_gate2_measure_tag(
        title="T", content=_BODY, brand_name=BRAND, injected=True,
    )
    assert "G8" in full["applied"] and "G9" in full["applied"]
    assert "G8" in full["weak"] and "G9" in full["weak"]


def test_r3_lineage_wiring_passes_exempt_codes_to_tag():
    """接线锁(判据打在接线上):topic 带 `_gate2_exempt_codes` →
    lineage 快照的 gate2_measures 三字段都不含被豁免码。"""
    from writing.article_lineage import build_article_lineage

    lineage = build_article_lineage(
        topic={
            "id": 11, "title": "观光电梯多少钱?", "style_code": "price_roi",
            "_gate2_injected": True, "_gate2_exempt_codes": ["G8"],
            "_evidence_pack": {"items": []},
        },
        article={"title": "观光电梯多少钱?", "content": _BODY, "style": "price_roi"},
        quote_id=21, industry="观光电梯", client_brand=BRAND,
    )
    tag = lineage["generation_request_snapshot"]["gate2_measures"]
    assert tag["injected"] is True
    assert "G8" not in tag["applied"], "lineage 仍记「G8 已应用」而 prompt 里没有"
    assert "G8" not in tag["weak"] and "G8" not in tag["observed"]
    assert tag["exempted"] == ["G8"]
    assert "G9" in tag["applied"], "未豁免码被殃及"


def test_r3_g8_exemption_coverage_is_near_global_not_per_article():
    """覆盖面锁(订正6):`bool(_compact_spec)` 对一切「有 length_plan 且
    未进深档」的 topic 恒真 —— G8 让位接近全局,残余注入面只剩
    **无 length_plan** 的 topic。锁两端:紧凑档必产 spec(⇒豁免);
    无 plan / 深档 plan 产空 spec(⇒残余面走各自条件)。"""
    from writing.templates.canonical_family_templates import build_compact_structure_spec
    from writing.article_length_contract import DEEP_TIER_MIN_CHARS

    # 紧凑档全域:任意 0 < target < 深档线 → spec 非空 → G8 豁免条件真
    for target in (800, 1500, 3000, DEEP_TIER_MIN_CHARS - 1):
        assert build_compact_structure_spec({"target_chars": target}), (
            f"target={target} 紧凑档未产 spec —— 覆盖面口径失真"
        )
    # 残余面:无 plan / 深档 target → spec 空(G8 是否注入由其余条件决定)
    assert build_compact_structure_spec(None) == ""
    assert build_compact_structure_spec({"target_chars": DEEP_TIER_MIN_CHARS}) == ""


def test_tag_survives_a_broken_checker(monkeypatch):
    """D8:观测炸了也要出标签(否则一个正则 bug 能把整条保存链拖下水)。"""
    import writing.gate2_rewrite_measures as g2

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setitem(g2.CHECKERS, "G1", _boom)
    tag = build_gate2_measure_tag(title="T", content=_BODY, brand_name=BRAND, injected=True)
    assert tag["observed"]["G1"] is None
    assert tag["injected"] is True
