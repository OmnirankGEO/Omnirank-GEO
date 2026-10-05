"""Owner 2026-08-09:选题页展示的体裁标签,默认填进「开始写作」的请求体。

要消灭的是「展示 A、生成 B」:选题卡上写着「选购与多品牌比较」,但请求体里
没带 `user_choices`,后端就按行业权重抽签,交付出来可能是别的文体。

两条锁按 Owner 点名的口径写:
  ① **请求体真带 `user_choices`** —— 断言打在 `authFetch("/api/writing/start-articles")`
     那个 `body: JSON.stringify({...})` 里,不是打在某个 state 变量上;
  ② **默认值来自展示标签的映射一致性** —— 徽章 / 下拉框默认值 / 请求体三处
     必须同源,且前端推出来的 user_choice 必须与后端 `family_for_style` 一致。

🔴 关于这两条锁能证到哪一层,交付单里有明说:仓里前端没有 JS 测试跑器,
   所以这里是**源码级 + 跨层一致性**,不是真浏览器里抓到的那一个请求。
   差的那一层是什么、为什么没做,写在交付单,不在这里假装。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
SRC = TSX.read_text(encoding="utf-8")


# --- ① 请求体 ---------------------------------------------------------------

def _start_articles_request_block() -> str:
    """截出 `authFetch("/api/writing/start-articles", {...})` 那一整段调用。"""
    # 从 map 构造那一行起截 —— 构造与请求体是同一段逻辑,分开截会让
    # 「map 由谁构造」这条判据落在块外,凭空转红(第一版就是这么错的)。
    i = SRC.find("const userChoicesMap = buildUserChoicesMap")
    if i < 0:
        i = SRC.find("const userChoicesMap")
    assert i > 0, "找不到 user_choices 的构造点"
    assert SRC.find('authFetch("/api/writing/start-articles"', i) > i,         "构造点之后没有开始写作的请求 —— 端点改名了?"
    j = SRC.find("const data = await res.json()", i)
    assert j > i, "截不出请求块"
    return SRC[i:j]


def test_request_body_carries_user_choices():
    block = _start_articles_request_block()
    body_at = block.find("body: JSON.stringify({")
    assert body_at > 0, "请求块里找不到 body"
    body = block[body_at:]
    assert "user_choices:" in body, "请求体没有 user_choices 字段"
    assert "user_choices: userChoicesMap" in body, (
        "user_choices 不是由 buildUserChoicesMap 的结果填的 —— 可能被就地改回内联构造"
    )


def test_user_choices_map_is_built_by_the_shared_builder():
    """反向对照:map 必须由那个可被单独断言的函数构造,不许在调用点内联重写。

    上一版就是内联的 `topics.forEach(...)`,于是"展示了标签但没手动选过"的选题
    一个都不进请求体。
    """
    block = _start_articles_request_block()
    assert "buildUserChoicesMap(topics, writableIds)" in block
    assert "userChoicesMap[t.id] = t.user_choice" not in block, "旧的内联构造又回来了"


def test_builder_falls_back_to_industry_lottery_when_no_label():
    """没标签的选题**不许**被塞进 map —— Owner 明示保留行业配比抽签那条路。"""
    fn = _function_body("buildUserChoicesMap")
    assert "if (choice !== 'auto') map[t.id] = choice" in fn, (
        "auto 也被塞进请求体的话,抽签那条路就被堵死了"
    )


# --- ② 展示 / 默认 / 提交 三处同源 ------------------------------------------

def _function_body(name: str) -> str:
    """截函数体。

    🔴 第一版从函数名往后直接数花括号 —— 而 `buildUserChoicesMap` 的**签名里就有
    `{ id: number }`**,计数从那儿开跑、在签名内部就归零,截出来的只是签名片段,
    于是三条锁凭空转红。先跳过参数表,再从函数体那个 `{` 开始数。
    """
    i = SRC.find(f"function {name}(")
    assert i > 0, f"找不到 {name}"
    # 先走完参数表的括号配对
    k = SRC.index("(", i)
    depth = 0
    while k < len(SRC):
        if SRC[k] == "(":
            depth += 1
        elif SRC[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    body_start = SRC.index("{", k)
    depth, j = 0, body_start
    while j < len(SRC):
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:j + 1]
        j += 1
    raise AssertionError(f"{name} 花括号不平衡")


def test_badge_dropdown_and_request_read_the_same_source():
    """三处同源。改动前它们各读各的,那就是「展示 A 生成 B」的直接成因。"""
    # 徽章
    assert SRC.count("humanizeStyle(displayedStyleSource(topic))") == 3, "徽章没走同一个源"
    assert "humanizeStyle(topic.style_family || topic.style_code || topic.article_style)" not in SRC
    # 下拉框默认值
    assert SRC.count("currentValue={effectiveUserChoice(topic)}") == 3, "下拉框没走同一个源"
    assert "normalizeUserChoice(topic.user_choice || topic.style_family)" not in SRC, (
        "下拉框还在读旧的字段组合 —— 它会显示「系统推荐」而我们实际发别的值"
    )
    # 请求体
    assert "effectiveUserChoice(t)" in _function_body("buildUserChoicesMap")


def test_effective_choice_prefers_explicit_user_selection():
    """用户改选过就以用户为准(保留自由度)—— 顺序不许反。"""
    fn = _function_body("effectiveUserChoice")
    exp = fn.find("normalizeUserChoice(t.user_choice)")
    shown = fn.find("userChoiceFromDisplayedStyle")
    assert 0 < exp < shown, "展示标签压过了用户的显式选择"


# --- 🔴 跨层一致性:前端推的值必须与后端 family_for_style 同解 ----------------

def _frontend_style_to_human() -> dict[str, str]:
    """从 TSX 里把 `__STYLE_HUMAN_FACING__` 解析出来(不手抄)。"""
    i = SRC.find("const __STYLE_HUMAN_FACING__")
    j = SRC.find("};", i)
    body = SRC[SRC.find("{", i) + 1:j]
    out = {}
    for m in re.finditer(r"^\s*([A-Za-z0-9_一-鿿]+)\s*:\s*'([^']+)'", body, re.M):
        out[m.group(1)] = m.group(2)
    assert len(out) >= 20, f"解析出来只有 {len(out)} 条,正则跟源码对不上了"
    return out


def _frontend_options() -> list[tuple[str, str]]:
    i = SRC.find("const USER_CHOICE_OPTIONS")
    j = SRC.find("];", i)
    return re.findall(r"value:\s*'([a-z_]+)',\s*label:\s*'([^']+)'", SRC[i:j])


def _norm_label(s: str) -> str:
    return re.sub(r"[（(][^）)]*[）)]\s*$", "", s).strip()


def _frontend_derive(style_code: str) -> str:
    """在 Python 里复算前端那两步(humanize → 反查),用于跨层对账。"""
    human = _frontend_style_to_human().get(style_code, style_code)
    target = _norm_label(human)
    opts = [(v, _norm_label(l)) for v, l in _frontend_options() if v != "auto"]
    for v, l in opts:
        if l == target:
            return v
    for v, l in opts:
        if l.startswith(target) or target.startswith(l):
            return v
    return "auto"


@pytest.mark.parametrize("style_code", sorted(_frontend_style_to_human()))
def test_frontend_default_agrees_with_backend_family(style_code):
    """🔴 最要紧的一条:前端默认发出去的 user_choice,必须与后端
    `family_for_style(style_code)` 落到同一族。

    不一致的后果很隐蔽:改动前 `user_choice=='auto'` 时后端会走
    `_trust_legacy_style` 直接复用 `article_style`;改动后 `user_choice` 非 auto,
    走的是 `resolve_user_choice` —— **两条路必须同解**,否则这个功能会把
    「展示 A 生成 B」换成另一种形态的「展示 A 生成 B」。
    """
    from writing.article_style_contract import family_for_style

    backend = family_for_style(style_code)
    frontend = _frontend_derive(style_code)
    # [2026-08-09] 这里原来是 `pytest.skip` —— 9 个中文别名(问答FAQ / 趋势洞察 / …)
    # 后端一律返 None,于是它们的跨层一致性**从来没被验证过**,却混在通过数里。
    # 别名接上 `style_registry.STYLE_ALIAS_TO_CODE` 之后改成硬断言:
    # 徽章上能展示的每一个值,后端都必须认得,否则「展示什么默认什么」就是空话。
    assert backend is not None, (
        f"{style_code}:徽章会展示它,后端 family_for_style 却不认 —— "
        f"这些选题的默认体裁会静默退回抽签"
    )
    assert frontend == backend, (
        f"{style_code}:前端默认发 {frontend},后端 family_for_style 说 {backend}"
    )


@pytest.mark.parametrize("alias,family", [
    ("问答FAQ", "evidence_qa"),
    ("证据选型", "multi_brand_comparison"),
    ("对比评测", "multi_brand_comparison"),
    ("方法指南", "implementation_guide"),
    ("趋势洞察", "trend_policy_risk"),
    ("案例分享", "case_data_roi"),
    ("价格解读", "case_data_roi"),
    ("品牌故事", "company_facts"),
    ("公司深度报道", "company_facts"),
])
def test_chinese_article_style_aliases_resolve_to_a_family(alias, family):
    """9 个中文 `article_style` 别名必须归到族。

    它们是**选题生成器自己写进 `topics.article_style` 的值** —— 不是脏数据。
    上一版这 9 个全是 skip,等于「展示什么默认什么」在这批存量选题上根本没生效。
    """
    from writing.article_style_contract import family_for_style

    assert family_for_style(alias) == family


def test_alias_resolution_reuses_the_existing_registry_ssot():
    """元判据:别名归一必须**复用** `style_registry` 那张表,不许在这里再抄一份。

    仓里已有 `STYLE_ALIAS_TO_CODE` / `normalize_style_code`;再写一张中文表
    就是第四张映射,迟早跟它打架。
    """
    src = (ROOT / "writing" / "article_style_contract.py").read_text(encoding="utf-8")
    fn_at = src.index("def family_for_style(")
    whole = src[fn_at:src.index("\ndef ", fn_at + 10)]
    assert "normalize_style_code" in whole, "没接现成的别名 SSOT"
    # 🔴 判据只看**可执行的那部分**:docstring 里正当地举了「问答FAQ」当例子,
    #    连 docstring 一起扫会把这条锁变成"不许在注释里提别名",判的不是同一件事
    #    (第一版就是这么写的,基线当场红)。
    doc_open = whole.index('"""')
    body = whole[whole.index('"""', doc_open + 3) + 3:]
    for alias in ("问答FAQ", "趋势洞察", "公司深度报道"):
        assert alias not in body, f"{alias} 被抄进本模块的代码里 = 又一张手抄表"


def test_every_option_value_is_a_legal_backend_user_choice():
    """前端下拉框的值域不许超出后端合法值域。"""
    from writing.article_style_contract import normalize_user_choice

    for value, _label in _frontend_options():
        assert normalize_user_choice(value) == value or value == "auto", (
            f"前端选项 {value} 不是后端认的合法 user_choice"
        )


def test_derivation_has_no_third_mapping_table():
    """元判据:不许再新建第三张 style_code → user_choice 的手抄表。

    展示标签是 `humanizeStyle` 算的,默认值就该由它反查回去 ——
    第三张表一旦出现,「展示什么默认什么」就从构造保证退化成"碰巧写得一样"。
    """
    fn = _function_body("userChoiceFromDisplayedStyle")
    assert "humanizeStyle(" in fn, "没有经过展示标签,而是另起了一套映射"
    assert "USER_CHOICE_OPTIONS" in fn, "没有反查选项表"
    assert "ranking_v2" not in fn and "buying_guide" not in fn, (
        "函数体里出现了 style_code 字面量 = 又抄了一张表"
    )
