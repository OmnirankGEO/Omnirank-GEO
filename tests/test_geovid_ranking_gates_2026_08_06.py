# -*- coding: utf-8 -*-
"""榜单闸锁 · 2026-08-06 二次返工后口径

治理 SSOT §11.1 四级裁决:**全部 A1**(R1 从 H0 降下来了),R6 已删。
每条闸都配**成对的反向对照** —— 只写"必须命中"的锁抓不出"锚点撞到别处"。

## 🔴 这一版为什么整个 R1 节推翻重写

上一版 R1 用中文正则从**自由文本**里猜公司名。Codex 实测四句普通话全被判成
虚构公司且 `blocking=True`:

    「选择时要看电梯」「对比装饰」「这类科技」「智能电梯」

而我上一版的"反向对照"用的是「三家都挺好的」「我去看了一圈」「报价区间大概在两万上下」
—— **这三句一个后缀词都不含**,正则根本不可能命中它们。也就是说那条反向对照
**零判别力**:它对真正的失败模式(含 电梯/装饰/科技 的普通话)完全不设防。
所以本文件的阴性样本集**改用 Codex 那四句**,并额外补了同形态的扩展样本。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from services.geo_douyin import ranking_gates as g


REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "services" / "geo_douyin" / "ranking_gates.py"

ALLOWED = ["深圳市恒通电梯有限公司", "快意电梯", "康力电梯"]
CLIENT = "深圳市晨光富士电梯有限公司"


def _code_only(path: pathlib.Path) -> str:
    """剥掉注释与 docstring 后的源码。

    🔴 为什么必须有这一步:我第一版把 `assert "blocking" not in SRC.read_text()`
       直接打在原文上,结果**锁撞到了自己写的那段"为什么删掉 blocking"的说明**。
       这是同一个坑的第三次(08-06 已经栽过两次:锚点撞自己注释 / 剥注释≠剥字符串)。
       所以剥完之后**必须自证剥干净了** —— 见下面那条自检锁。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
    return ast.unparse(tree)


def test_the_docstring_stripper_actually_strips():
    """自检:剥离器没生效的话,上面那条形态锁就成了恒真。"""
    raw = SRC.read_text(encoding="utf-8")
    assert "二次返工" in raw, "样本句不在源码里 —— 这条自检失去意义"
    assert "二次返工" not in _code_only(SRC), "docstring/注释没被剥掉"

#: 🔴 阴性样本集 —— 全部含「电梯/装饰/科技/传媒/集团/设计」这类**旧正则的后缀词**,
#:    也就是说它们逐条都能让上一版误报。这是这个集合唯一的选样标准:
#:    **能打到真正的失败模式**,而不是"看起来像普通话"。
ORDINARY_PROSE = [
    "选择时要看电梯",          # Codex 实测四句
    "对比装饰",
    "这类科技",
    "智能电梯",
    "这几家的电梯都能装",       # 同形态扩展
    "装饰这一块水很深",
    "看科技含量不如看售后",
    "找传媒公司之前先想清楚要什么",
    "整个集团都在推这个方案",
    "设计要先量尺",
]


# ===========================================================================
# 分级本身
# ===========================================================================

def test_no_gate_is_h0_any_more():
    """🔴 §11.1:证据不足/排名依据不完整/一般事实核验**全是 A1**。

    R1 也从 H0 降到 A1 —— 禁虚构的主防线在源头(候选只能来自读库 +
    写作侧白名单冻结),检测层只是兜底,兜底不该拦整单。
    """
    findings = g.run_gates(
        caption="这3家不错", allowed_names=ALLOWED, actual_count=5,
        entity_slots=[{"entity": "编的", "entity_ref": "编造集团"}],
        caveats=["这家太坑了"], is_ranking_form=True,
        items=[{"display_name": "X", "source": {}}],
        observed_days_ago=999, window_days=90)
    gates = {f.gate for f in findings}
    assert {"R1", "R2", "R5", "R7", "R8"} <= gates, f"少了闸:{gates}"
    for f in findings:
        assert f.level == g.LEVEL_A1, f"{f.gate} 不是 A1 而是 {f.level}"


def test_r6_is_gone():
    """R6 行业门控已删 —— 并入 router 的路由默认版式,不是关闭行业。"""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not any(n.startswith("r6_") for n in fns), "R6 又回来了"


# ===========================================================================
# ③ blocking 字段整个删除(闸输出必须有消费方)
# ===========================================================================

def test_finding_has_no_blocking_field_at_all():
    """🔴 `blocking` 写进 meta 后全仓没有消费方 —— 既造错误元数据又不真拦。

    删掉而不是置 False:留一个恒 False 的字段,下一个人读到的第一眼仍是
    "有硬拦这回事"(和 `redraw_free` 那次同一个道理)。
    """
    f = g.r2_count_matches("5家实测", 3)
    assert f is not None
    assert not hasattr(f, "blocking"), "GateFinding 又长回 blocking 了"
    assert "blocking" not in f.to_dict(), "to_dict 里还有 blocking"
    assert "blocking" not in _code_only(SRC), "源码里还有 blocking —— 死字段回潮"


def test_blocking_findings_helper_is_gone():
    assert not hasattr(g, "blocking_findings"), "死函数 blocking_findings 又回来了"


def test_findings_carry_a_repair_scope_that_is_consumed():
    """反向对照:删了 blocking 之后必须有**真消费方**。

    🔴 2026-08-07 订正:上一版这里断言的是 `repair_scope([f]) == [2]` ——
       而那个函数全仓唯一的调用者就是这条锁本身。**锁自己不是消费方。**
       真消费方是前端:作品卡上的 A1 提示渲染「(第 N 张)」。
       所以判据改成"作用域进了序列化面 + 前端有渲染点"。
    """
    f = g.r1_entities_in_frozen_list(
        [{"entity": "甲"}, {"entity": "编的", "entity_ref": "编造集团"}], ALLOWED)
    assert f is not None and f.card_indices == (2,)
    assert f.to_dict()["card_indices"] == [2]


# ===========================================================================
# R1 · 结构化比对(A1 + 单卡修复)
# ===========================================================================

@pytest.mark.parametrize("prose", ORDINARY_PROSE)
def test_r1_never_flags_ordinary_prose_in_the_caption(prose):
    """🔴 本文件最重要的一条:普通话正文**一句都不许**被判成虚构公司。

    走的是 `run_gates` 的真实入口(caption + 结构卡),与付费链一致。
    """
    slots = [{"entity": "快意电梯", "entity_ref": "快意电梯"},
             {"entity": "比较口径"}]          # 结构卡:没声明讲哪一家
    findings = g.run_gates(caption=prose, allowed_names=ALLOWED,
                           entity_slots=slots, client_brand=CLIENT)
    assert not [f for f in findings if f.gate == "R1"], \
        f"普通话被判成虚构公司:{prose}"


@pytest.mark.parametrize("prose", ORDINARY_PROSE)
def test_r1_never_flags_ordinary_prose_as_a_card_entity(prose):
    """同一批句子放在卡面 `entity` 上(没有 `entity_ref` 声明)也不许命中 ——
    多数内容卡讲的是比较口径/成本结构,本来就不是公司名。"""
    assert g.r1_entities_in_frozen_list([{"entity": prose}], ALLOWED) is None, prose


def test_r1_ignores_the_caption_entirely():
    """🔴 结构上的保证:R1 不再看自由文本。

    caption 里塞满编的公司名,只要没有卡片声明,R1 就不该出声 ——
    这是"正则猜名"那条路被彻底拔掉的判据。
    """
    caption = "推荐 光引GEO科技、某某虚构集团、随便什么装饰有限公司"
    findings = g.run_gates(caption=caption, allowed_names=ALLOWED,
                           entity_slots=[{"entity": "快意电梯",
                                          "entity_ref": "快意电梯"}])
    assert not [f for f in findings if f.gate == "R1"]


def test_r1_catches_a_declared_name_outside_the_frozen_list():
    f = g.r1_entities_in_frozen_list(
        [{"entity": "快意电梯", "entity_ref": "快意电梯"},
         {"entity": "光引GEO科技", "entity_ref": "光引GEO科技有限公司"}], ALLOWED)
    assert f is not None and f.level == g.LEVEL_A1
    assert f.reason == "entity_not_in_frozen_list"
    # 具体是哪一家折进 message(原来在没人读的 hits 里)
    assert "光引GEO科技" in f.to_dict()["message"]
    assert f.card_indices == (2,), "没定位到具体是哪一张"


def test_r1_catches_display_name_substitution():
    """声明 A、卡面印 B = 冒名顶替,这是结构化比对**独有**的判别力。"""
    f = g.r1_entities_in_frozen_list(
        [{"entity": "康力电梯", "entity_ref": "快意电梯"}], ALLOWED)
    assert f is not None and f.reason == "entity_display_mismatch"
    assert f.card_indices == (1,)


def test_r1_lets_listed_names_through():
    """反向对照:名单内的名字**必须放行**,含法定后缀/地域变体/截断。"""
    ok = [
        {"entity": "快意电梯", "entity_ref": "快意电梯"},
        {"entity": "恒通电梯", "entity_ref": "深圳市恒通电梯有限公司"},   # 同主体异写
        {"entity": "深圳市恒通电梯有限", "entity_ref": "深圳市恒通电梯有限公司"},  # clip 截断
        {"entity": "比较口径"},                                        # 结构卡
    ]
    assert g.r1_entities_in_frozen_list(ok, ALLOWED) is None


def test_r1_tolerates_a_truncated_declaration():
    """反向对照:声明本身被截断(模型没抄全)也必须放行。

    🔴 这条是变异「截断容错撤掉」补上的 —— 上一版只测了"卡面被截、声明完整",
       于是 `ref_in_frozen_list` 的前缀分支根本没被跑到,那半条判据是死的。
    """
    assert g.ref_in_frozen_list(
        "深圳市恒通电梯有限",
        g.frozen_name_keys(ALLOWED), _norms(ALLOWED)) is True
    assert g.r1_entities_in_frozen_list(
        [{"entity": "深圳市恒通电梯有限", "entity_ref": "深圳市恒通电梯有限"}],
        ALLOWED) is None


def test_r1_still_rejects_a_declaration_that_is_not_a_prefix():
    """成对反向:前缀容错**不能**宽到把不相干的名字放进来。"""
    assert g.ref_in_frozen_list(
        "深圳市联想电梯", g.frozen_name_keys(ALLOWED), _norms(ALLOWED)) is False


def _norms(names):
    from services.geo_douyin.ranking_payload import _norm
    return {_norm(n) for n in names}


def test_r1_lets_the_client_through_without_being_in_the_list():
    assert g.r1_entities_in_frozen_list(
        [{"entity": CLIENT[:12], "entity_ref": CLIENT}], ALLOWED,
        client_brand=CLIENT) is None


def test_r1_is_silent_when_there_is_no_frozen_list():
    """反向对照:名单为空 = 没有判定依据,**不许报** —— 报了就是恒红。"""
    assert g.r1_entities_in_frozen_list(
        [{"entity": "随便", "entity_ref": "随便集团"}], []) is None


def test_r1_gives_a_single_card_repair_exit_not_a_whole_order_block():
    """🔴 A1 的形状:必须**定位到具体哪几张**,并指向一条真实存在的修复路径。

    🔴 2026-08-07 订正:上一版这里断言 `actions` 里有四个出口且都不重复扣费 ——
       而那个数组序列化后**零渲染方**,`recharge: False` 更是个假声明:
       既有单张重抽链 2026-08-03 起收 100 算力。出口不能凭空写,
       得指向真路径 ——「看看」→ 详情页 → 单张重抽。
    """
    f = g.r1_entities_in_frozen_list(
        [{"entity": "编的", "entity_ref": "某某虚构科技"}], ALLOWED)
    d = f.to_dict()
    assert d["card_indices"] == [1], "没带作用域 = 还是整单级"
    assert "详情页" in d["message"], "没指向任何可执行路径"


def test_r1_regex_company_guessing_is_gone_from_source():
    """🔴 形态锁:源码里不许再出现"按后缀猜公司名"那条路。"""
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "extract_company_like" not in fns, "猜名函数又回来了"
    assert "r1_no_fabricated_entity" not in fns, "旧 R1 又回来了"
    # 剥掉注释与 docstring 再查,免得锁被自己的说明文字骗过
    assert "有限公司|股份有限公司" not in _code_only(SRC), "公司名正则又回来了"


# ===========================================================================
# R2 · 家数一致(A1)
# ===========================================================================

def test_r2_flags_mismatch_as_a1():
    f = g.r2_count_matches("深圳载货电梯｜5家实测对比", 3)
    assert f is not None and f.level == g.LEVEL_A1
    msg = f.to_dict()["message"]
    assert "5家" in msg and "3 家" in msg, msg      # 两个数都要说出来
    assert "改" in msg, "没说该怎么办"


def test_r2_silent_when_matching_or_absent():
    assert g.r2_count_matches("3家实测", 3) is None
    assert g.r2_count_matches("没有数字的标题", 3) is None


def test_r2_does_not_confuse_years_with_entity_count():
    """反向对照:「做了6年」不是「6家」。"""
    assert g.promised_entity_count("做推广6年") is None


# ===========================================================================
# R5 / R8 · A1 降级(不是拒绝)
# ===========================================================================

def test_r5_degrades_instead_of_rejecting():
    """🔴 2026-08-07 订正:`degrade_to` 已删 —— 它从来没有人执行,
    留着它只是让文案敢说"已改用某版式"这种假话。判据改成:提示要说清
    **发生了什么 + 该怎么办**,而不是宣称一个没发生的切换。"""
    f = g.r5_evidence_freshness(200, window_days=90)
    assert f is not None and f.level == g.LEVEL_A1
    msg = f.to_dict()["message"]
    assert "监测" in msg, "没给可执行动作 —— 那就是变相拒绝"
    assert "已改用" not in msg, "宣称了一个没发生的版式切换"


def test_r5_silent_inside_window():
    assert g.r5_evidence_freshness(30, window_days=90) is None
    assert g.r5_evidence_freshness(None, window_days=90) is None


def test_r8_degrades_when_provenance_incomplete():
    f = g.r8_provenance_complete([{"display_name": "A", "source": {"engine": "x"}}])
    assert f is not None and f.level == g.LEVEL_A1
    assert "没写名次" in f.to_dict()["message"]


def test_r8_silent_when_provenance_complete():
    full = {k: "v" for k in g.PROVENANCE_KEYS}
    assert g.r8_provenance_complete([{"display_name": "A", "source": full}]) is None


#: 🔴 **写死**在测试里,不从 `g.PROVENANCE_KEYS` 推导。
#:    原来这条锁写成 `@parametrize("drop", list(g.PROVENANCE_KEYS))` ——
#:    常量缩水时参数化跟着缩水,**锁恒真**;变异「只查两个要素」从这个洞活着穿过去了。
#:    判据的期望值不能从被测对象自己身上取。
_EXPECTED_PROVENANCE = ("engine", "recommendation_rank", "extractor_version",
                        "llm_model", "observed_at")


def test_provenance_key_set_is_exactly_the_five():
    assert tuple(g.PROVENANCE_KEYS) == _EXPECTED_PROVENANCE, (
        f"举证链要素被改了:{g.PROVENANCE_KEYS}")


@pytest.mark.parametrize("drop", _EXPECTED_PROVENANCE)
def test_every_provenance_key_is_load_bearing(drop):
    """五要素每一条都承重 —— 防"写进文档但代码只查其中两条"。"""
    src = {k: "v" for k in _EXPECTED_PROVENANCE}
    src[drop] = ""
    assert g.r8_provenance_complete([{"display_name": "A", "source": src}]) is not None, \
        f"缺 {drop} 没被 R8 抓到"


# ===========================================================================
# R7 · caveat 不得成为诋毁
# ===========================================================================

def test_r7_flags_disparaging_caveat_in_ranking_form():
    f = g.r7_caveat_not_disparaging(["这家偷工减料"], is_ranking_form=True)
    assert f is not None and f.level == g.LEVEL_A1


def test_r7_silent_on_neutral_caveats():
    """反向对照:合法出口(使用注意事项/适用边界)**必须放行**。"""
    ok = ["旧楼加装需要先做井道评估", "预算低于 8 万可能排不上期", "适合中小型加工厂"]
    assert g.r7_caveat_not_disparaging(ok, is_ranking_form=True) is None


def test_r7_only_applies_to_ranking_form():
    assert g.r7_caveat_not_disparaging(["这家太坑"], is_ranking_form=False) is None


# ===========================================================================
# 全局
# ===========================================================================

def test_run_gates_never_raises_on_garbage():
    for kw in ({}, {"caption": None}, {"items": [None]}, {"caveats": [None]},
               {"entity_slots": [None]}, {"entity_slots": ["裸字符串"]}):
        out = g.run_gates(**kw)          # type: ignore[arg-type]
        assert isinstance(out, list)


def test_every_finding_is_a1_and_none_can_block():
    findings = g.run_gates(
        caption="5家实测", allowed_names=ALLOWED, actual_count=2,
        entity_slots=[{"entity": "编的", "entity_ref": "某某虚构科技"}],
        caveats=["这家很坑"], is_ranking_form=True,
        items=[{"display_name": "A", "source": {}}],
        observed_days_ago=500, window_days=90)
    assert findings
    assert {f.level for f in findings} == {g.LEVEL_A1}
    for d in (f.to_dict() for f in findings):
        assert "blocking" not in d


def test_module_has_no_raise():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]


# ===========================================================================
# 序列化合同 · 2026-08-07 微修补(R2 欠账)
#
# 🔴 这一节的写法与前几次不同,原因值得记:
#    前三次是**逐个字段**判"它该不该留",判一次漏一次(blocking → degrade_to
#    → actions,同一家族第四次)。这次改成**给序列化面一份白名单**,
#    并要求名单上每个键都能在前端 grep 出真渲染点 —— 判据从"我记得都有人读"
#    变成"逐键机器核"。
# ===========================================================================

import re as _re

_GATES_SRC = REPO / "services" / "geo_douyin" / "ranking_gates.py"
# [WO_271 · 2026-09-23] 旧入口页 DouyinImagePost.tsx 已删(6b491ab23)。排名闸的发现现在跟着作品
# 走进详情页(RankingSummary.gates),消费方改指详情页。
_TSX_LIST = REPO / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"

#: 🔴 写死在测试里,**不从 `g.SERIALIZED_KEYS` 推导** ——
#:    从被测对象取期望值的锁是自指恒真(本包 R8 那条栽过一次)。
_EXPECTED_SERIALIZED = ("gate", "message", "card_indices")

#: 每个键在前端的**真渲染点**。锁按这张表逐条 grep;
#: 加了新键却没有渲染点 → 没有对应条目 → 红。
_FRONTEND_CONSUMER = {
    "gate": "key={f.gate}",
    "message": "{f.message}",
    "card_indices": "f.card_indices",
}


def test_serialized_contract_is_exactly_three_keys():
    import services.geo_douyin.ranking_gates as g
    f = g.r2_count_matches("5家实测", 3)
    assert f is not None
    assert tuple(g.SERIALIZED_KEYS) == _EXPECTED_SERIALIZED, g.SERIALIZED_KEYS
    assert tuple(f.to_dict().keys()) == _EXPECTED_SERIALIZED, list(f.to_dict())


def _missing_frontend_consumers(tsx: str, keys) -> list:
    """序列化了却在前端找不到渲染点的键(空 = 都有人读)。"""
    out = []
    for key in keys:
        needle = _FRONTEND_CONSUMER.get(key)
        if not needle:
            out.append(f"序列化了 {key} 却没登记它的渲染点 —— 先证明有人读它")
        elif needle not in tsx:
            out.append(f"{key} 的渲染点 `{needle}` 在前端找不到了")
    return out


def test_every_serialized_key_has_a_real_frontend_consumer():
    """🔴 Review 点名的那条判据:序列化的每个字段都要有消费方,否则删掉。

    [WO_271] 改指详情页:它把 `gates` 收进了类型(RankingSummary),却一处都没画。
    [WO_283-F5 已修] 详情页 `data-testid="ranking-gates"` 逐条画 message,card_indices 变成
    「看第 N 张」可点(切到那一张)。
    """
    import services.geo_douyin.ranking_gates as g
    missing = _missing_frontend_consumers(_TSX_LIST.read_text(encoding="utf-8"), g.SERIALIZED_KEYS)
    assert not missing, "\n".join(missing)


def test_frontend_consumer_lock_has_power():
    """反臂(WO_283):把任一渲染点拿掉 ⇒ 上一格必须报;现役源码不报(对照)。"""
    import services.geo_douyin.ranking_gates as g
    tsx = _TSX_LIST.read_text(encoding="utf-8")
    assert _missing_frontend_consumers(tsx, g.SERIALIZED_KEYS) == []
    for key in g.SERIALIZED_KEYS:
        assert _missing_frontend_consumers(tsx.replace(_FRONTEND_CONSUMER[key], ""), g.SERIALIZED_KEYS), key


def test_dead_metadata_fields_are_gone_from_the_dataclass():
    """三个死字段**从 dataclass 上删除**,不是留着不序列化。

    留一个没人读的字段,下一个人读到的第一眼仍是"这里有出口/有降级目标"。
    """
    import services.geo_douyin.ranking_gates as g
    fields = set(g.GateFinding.__dataclass_fields__)
    for dead in ("blocking", "actions", "hits", "degrade_to"):
        assert dead not in fields, f"死字段 {dead} 还在 GateFinding 上"
    # level / reason 保留在 dataclass(锁在读),但**不序列化**
    assert {"level", "reason"} <= fields
    f = g.r2_count_matches("5家实测", 3)
    assert "level" not in f.to_dict() and "reason" not in f.to_dict()


def test_repair_scope_is_gone():
    """全仓唯一调用者是它自己的锁 —— 那不叫消费方。"""
    import services.geo_douyin.ranking_gates as g
    assert not hasattr(g, "repair_scope")


def test_no_dead_metadata_word_survives_in_code():
    """形态锁:剥掉注释与 docstring 后,四个死字段名一个都不许出现。"""
    src = _code_only(_GATES_SRC)
    for dead in ("blocking", "actions", "degrade_to", "repair_scope"):
        assert dead not in src, f"{dead} 在代码里回潮了"


# ── R5 / R8 文案不许再宣称"已经切了版式" ────────────────────────────────

def test_r5_does_not_claim_a_switch_that_never_happened():
    """🔴 闸跑在生成之后,form 由 route_template 定,本闸切不了任何版式。

    原文案写着「已改用不依赖时效的场景推荐版式」—— 什么都没切。
    告诉用户"我们已经改了"而实际没改,比不提示更坏。
    """
    import services.geo_douyin.ranking_gates as g
    msg = g.r5_evidence_freshness(200, window_days=90).to_dict()["message"]
    assert "已改用" not in msg and "已切" not in msg, msg
    assert "200 天" in msg and "监测" in msg      # 反向面:有用的信息没被删掉


def test_r8_says_what_actually_happened():
    import services.geo_douyin.ranking_gates as g
    msg = g.r8_provenance_complete(
        [{"display_name": "甲公司", "source": {"engine": "x"}}]).to_dict()["message"]
    assert "已改用" not in msg, msg
    assert "甲公司" in msg, "缺哪几家没说出来(原来它在没人读的 hits 里)"
    assert "没写名次" in msg


def test_r7_names_the_offending_line():
    import services.geo_douyin.ranking_gates as g
    msg = g.r7_caveat_not_disparaging(["这家偷工减料"],
                                      is_ranking_form=True).to_dict()["message"]
    assert "这家偷工减料" in msg, "是哪一句没说出来"


def test_r1_points_at_the_existing_paid_path():
    """出口指向既有路径(看看 → 详情页 → 单张重抽),不新造一条、也不留空。"""
    import services.geo_douyin.ranking_gates as g
    f = g.r1_entities_in_frozen_list(
        [{"entity": "编的", "entity_ref": "某某虚构科技"}], ["通力电梯"])
    assert f.to_dict()["card_indices"] == [1]
    assert "详情页" in f.to_dict()["message"]


# ── 前缀容错下限(Review 顺手项)────────────────────────────────────────

def test_single_char_ref_cannot_slip_through_the_prefix_path():
    """🔴 `ref="深"` 原来能蹭过 —— 它是「深圳市恒通电梯有限公司」的前缀。"""
    import services.geo_douyin.ranking_gates as g
    from services.geo_douyin.ranking_payload import _norm
    allowed = ["深圳市恒通电梯有限公司", "通力"]
    keys = g.frozen_name_keys(allowed)
    norms = {_norm(x) for x in allowed}
    assert g.ref_in_frozen_list("深", keys, norms) is False


def test_two_char_prefix_still_passes():
    """成对反向:下限取 2 不取 4。

    🔴 判据必须**只能**从前缀那条路命中,否则没有判别力 ——
       我第一版用的是「通力」而名单里就有「通力」,它走的是**精确键**那条路,
       于是把下限抬到 4 也照样绿(变异存活)。
       这里名单里只有「通力电梯」,「通力」进不了精确键(行业词是身份的一部分,
       是 P0 换来的边界),只能走前缀 —— 下限一变它就红。
    """
    import services.geo_douyin.ranking_gates as g
    from services.geo_douyin.ranking_payload import _norm, safe_merge_key
    allowed = ["通力电梯", "深圳市恒通电梯有限公司"]
    keys = g.frozen_name_keys(allowed)
    norms = {_norm(x) for x in allowed}
    assert safe_merge_key("通力") not in keys, "样本走了精确键 —— 这条锁没判别力"
    assert g.ref_in_frozen_list("通力", keys, norms) is True
    assert g.ref_in_frozen_list("深圳市恒通电梯有限", keys, norms) is True
