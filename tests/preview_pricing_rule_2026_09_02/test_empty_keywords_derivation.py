"""空 keywords 的接受与一处派生(订正二十三 · C 窗 · 2026-09-05)。

🔴 为什么在这个包里:包名是**容器不是概念单位**。新建包要走 gate9 `DISK_PACKAGES`
   的登记仪式(排序重建 + 四处计数 + 两级反向锚滚一级),那是树级改动,不该塞进
   一条马上要签的链。本文件自成一体,不与包内其他文件做任何聚合。

背景:题单编辑器取代旧关键词框后前端不再送 keywords。原 DTO 是
`Field(..., min_length=1)` ⇒ **每一次体检提交 422**,与题走哪个字段无关
(A 的前端笔单独落地就会打穿整条链)。

🔴 这里的锁**打行为不打写法**。A 原本想锁「Field 不再带 min_length=1」——
   那是缺席锁:我把 min_length 删掉、却在别处加个「空列表就 raise」的校验,
   它照绿而提交照样 422。缺席锁只能证「那个写法没了」,证不了「那件事能做了」。

══ 实测 毒→杀手 映射(2026-09-05 · 不是推算)══
  Q1 DTO 退回 min_length=1        → 4 条(含 test_empty_keywords_is_accepted_by_the_dto)
  Q2 拿掉三源皆空的拒绝            → test_all_three_sources_empty_is_rejected
  Q3 派生整个关掉                  → 4 条
  Q4 builder 退回无条件插空串      → test_derived_keywords_actually_reach_a_non_empty_search
  Q5 builder 解接线                → test_the_extracted_builder_is_actually_wired_in
  Q6 消费点自己兜底                → test_consumers_do_not_each_derive_their_own[_run_diagnosis_impl]

🔴 登记边界:Q6 第一轮报 SKIP —— 锚点 `keywords=request.keywords,` 在 **4 个不同
   DTO** 里都存在,毒**根本没落盘**。这与「锁没牙」方向相反而读数同形;
   runner 之所以分得出,是因为每发毒前先数锚点命中数、下毒后用 sha256 自证
   字节真的变了。改用三行唯一上下文锚后 Q6 被杀。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _dto():
    from server import DiagnosisRequest
    return DiagnosisRequest


def test_empty_keywords_is_accepted_by_the_dto():
    """🔴 跨层锁的权威那份(A 的前端包在无库环境跑不了 import server,记「未评估」)。

    A 的前端笔与本笔有**严格次序**:他先落地 ⇒ 整条体检链 422。
    这条绿,他那笔才安全。
    """
    _dto()(brand_name="x", industry="y", keywords=[])


def _consumer_assignments(src: str):
    """`social_search_keywords` 的全部赋值 —— 机械枚举,不预筛。"""
    out = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(tg, ast.Name) and tg.id == "social_search_keywords"
                   for tg in n.targets):
            continue
        is_call = (isinstance(n.value, ast.Call)
                   and isinstance(n.value.func, ast.Name)
                   and n.value.func.id == "build_social_search_keywords")
        out.append((is_call, ast.unparse(n.value)))
    return out


@pytest.mark.parametrize("form,expect,why", [
    ("social_search_keywords = build_social_search_keywords(a, b)", 1, "正常接线"),
    ("social_search_keywords = list(a) or build_social_search_keywords(a, b)", 0, "短路形"),
    ("social_search_keywords = list(a)", 0, "内联形"),
    ("social_search_keywords = list(a)" + chr(10)
     + "_unused = build_social_search_keywords(a, b)", 0, "旁路形(Review 第三形)"),
])
def test_the_wiring_detector_scores_each_known_form(form, expect, why):
    """🔴 正样本自证:**三发已知毒各打一遍**(合成源,不碰真文件)。

    上一版只自证了短路形 —— 于是旁路形在它下面看不见。
    「检测器对我想到的那一形有反应」不等于「它对这类形都有反应」。
    """
    got = sum(1 for ok, _ in _consumer_assignments(form) if ok)
    assert got == expect, "%s:期望 %d 个 builder 形赋值,实得 %d" % (why, expect, got)


def test_the_extracted_builder_is_actually_wired_in():
    """🔴 接线锁 —— 分母按**消费变量**取,不按「提没提到 builder」取。

    上一版筛的是「赋值语句里提到 builder」。Review 的第三形:
        social_search_keywords = list(diagnosis_keywords)
        _unused = build_social_search_keywords(...)
    `_unused` 那行右值确实是 Call,过;而**真正被 batch_collect 吃掉的那个变量**
    根本不在分母里 —— 筛选条件把要考的那一项排除掉了。

    改成:`social_search_keywords` 的全部赋值里,**恰有一处**右值就是 builder 的 Call。
    为什么是「恰有一处」而不是「至少一处」+ 豁免名单:
      :1559 那处(`list(diagnosis_keywords)` + 品牌名**变体**插入)与站点 A 文本同形,
      文本豁免分不开它俩;而站点 A 被打回 `list(...)` 时 builder 形归零 —— 计数就能分。
      (为什么不让 :1559 也走 builder:会改搜索词**顺序**
       ——原 [全称, 简称, ...kw] 变 [简称, 全称, ...kw]。集合没变、顺序变了,
       而集合差式的诊断对顺序这根轴是结构性失明的。不动。)
    """
    src = (ROOT / "workflows" / "diagnosis_workflow.py").read_text(encoding="utf-8")
    found = _consumer_assignments(src)
    assert found, "social_search_keywords 没有任何赋值 —— 分母塌了,不是通过"
    wired = [seg for ok, seg in found if ok]
    assert len(wired) == 1, (
        "恰需 1 处赋值的右值就是 builder 调用,实得 %d 处;全部右值=%s"
        % (len(wired), [seg for _, seg in found]))


@pytest.mark.parametrize("kws,why", [
    (["k"] * 21, "条数上限 20"),
    (["x" * 51], "单条上限 50 字"),
    (["   "], "空白串"),
])
def test_relaxing_the_lower_bound_did_not_relax_the_upper(kws, why):
    """🔴 放开下限**不许**顺手放开上限 —— 顺手多修就多欠一条判据。"""
    with pytest.raises(Exception):
        _dto()(brand_name="x", industry="y", keywords=kws)


# ══════════════════════════════════════════════════════════════════════════
# 一处派生:两个消费点不许各判一次空
# ══════════════════════════════════════════════════════════════════════════

CONSUMERS = ("_run_diagnosis_impl", "start_diagnosis")


def _fallbacks_on_keywords(src: str, func_name: str) -> list:
    """该函数体内是否对 `request.keywords` 做了自己的兜底(`or`/空判)。"""
    hits = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != func_name:
            continue
        for m in ast.walk(n):
            reads = (isinstance(m, ast.Attribute) and m.attr == "keywords"
                     and isinstance(m.value, ast.Name) and m.value.id == "request")
            if isinstance(m, ast.BoolOp) and any(
                    isinstance(v, ast.Attribute) and v.attr == "keywords"
                    and isinstance(v.value, ast.Name) and v.value.id == "request"
                    for v in m.values):
                hits.append(ast.unparse(m))
            elif isinstance(m, ast.If):
                seg = ast.unparse(m.test)
                if "request.keywords" in seg:
                    hits.append(seg)
            del reads
    return hits


def test_the_detector_fires_on_synthetic_bad_code():
    """🔴 正样本自证:喂**合成的**坏代码(不碰真文件),检测器必须响。

    否则「零命中」与「检测器坏了」在观测上同形。
    """
    bad = (
        "def start_diagnosis(request):\n"
        "    kw = request.keywords or [request.brand_name]\n"
    )
    assert _fallbacks_on_keywords(bad, "start_diagnosis"), "检测器对已知坏码不响 —— 尺子坏了"


@pytest.mark.parametrize("func", CONSUMERS)
def test_consumers_do_not_each_derive_their_own(func):
    """🔴 本 DTO 的 keywords 只有两个消费点:跑 workflow 与落库。

    两处各判一次空,就能**落库记空而 workflow 跑了派生值** ——
    事后谁也说不清那次诊断到底按什么词跑的,而且不会有任何东西报错。
    派生放在 DTO 上,两处结构上看到同一份。
    """
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    hits = _fallbacks_on_keywords(src, func)
    assert not hits, "%s 自己兜底了 keywords,派生不再是一处:%s" % (func, hits)


# 🔴 [订正二十六] 以下 5 条已**搬到** `test_keyword_source_ladder.py`:
#    test_empty_keywords_derives_from_the_brand_name / test_derivation_falls_back_to_industry / test_all_three_sources_empty_is_rejected
#    test_supplied_keywords_are_never_overwritten / test_derivation_is_defined_exactly_once
#    派生本身从 DTO 搬到了 `start_diagnosis`(DTO 无库,派生要读档案词)。
#    这里**不留空壳**:留个 `assert True` 之类的残壳,新旧两处就都没人跑。
#    「旧址无残留」由 ladder 那边的 test_the_old_site_left_no_hollow_shell 正面钉。
