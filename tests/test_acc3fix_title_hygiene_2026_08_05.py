"""判别测试 · 项2 标题双地名 + 词尾重复(WO-ACCEPTANCE-3FIX-2026-08-05)。

坐实的事故(2026-08-05 生产 ``geo_douyin_posts`` 现取,不是推断):

    post 14 · keyword='深圳全屋定制哪家好' · city='深圳'
    title = '深圳深圳全屋定制哪家好哪家好？5家实测对比：报价区间、工期和避坑要点一次说清'
             ↑ '深圳' 两次      ↑ '哪家好' 两次

同一形态的**第三次**:诊断 531「深圳深圳龙岗医美哪家靠谱？」是第一次,
R5 那轮的结论是「``has_geo_qualifier`` 只保护矫正闸补前缀那一支」。所以本文件的
锁不是"再补一个 if",而是钉死**共用件 + 不许绕过**。

对照组(同一次现取,证明判据不是恒真):
    post 10/12/13 · keyword 不含城市 → '深圳全屋定制哪家好？…' 城市前缀**照常加上**。

命名 ``*__must_hit`` / ``*__must_not_hit``。变异见 ``tests/mutation_acc3fix_title_hygiene.py``。
"""
from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

import pytest

from services.geo_douyin.title_engine import (
    build_city_matrix,
    build_hashtags,
    build_title,
)
from services.geo_title_hygiene import (
    QUESTION_TAILS,
    compose_title,
    effective_city_prefix,
    join_city_keyword,
    trim_duplicate_tail,
)

REPO = pathlib.Path(__file__).resolve().parents[1]


# ===========================================================================
# A. 规则一:已含地名不再叠加(只"不加",从不"删")
# ===========================================================================
def test_city_already_in_keyword_is_not_prefixed_again__must_hit():
    assert effective_city_prefix("深圳", "深圳全屋定制哪家好") == ""
    assert effective_city_prefix("广东省深圳市", "深圳全屋定制") == ""


def test_literal_city_hit_covers_untokenizable_values__must_hit():
    """整串命中这条路**独立于** token 命中,不是冗余判据。

    ``geo_tokens`` 只收长度 ≥2 的行政块:单字简称(沪/京/穗)拆出来是空元组,
    这时只有"整串在不在关键词里"能救。变异实测:只废掉整串那一支时,
    上面那条深圳的用例仍然绿(token 兜住了),而本条会转红。
    """
    from services.diagnosis_question_quality import geo_tokens

    assert geo_tokens("沪") == (), "前提变了:沪 现在能拆出 token,本条的独立性不再成立"
    assert effective_city_prefix("沪", "沪牌代办哪家好") == ""


def test_literal_city_miss_still_prefixes__must_not_hit():
    assert effective_city_prefix("沪", "车牌代办哪家好") == "沪"


def test_city_absent_from_keyword_is_still_prefixed__must_not_hit():
    """🔴 工单 §2.5 第 3 行:不许为了去重把该加的地名也不加了。

    这是成对反向面 —— 只证明"会去重"是不够的,一个恒返回空串的实现
    也能通过上面那条。
    """
    assert effective_city_prefix("深圳", "全屋定制") == "深圳"
    assert effective_city_prefix("广州", "深圳全屋定制") == "广州"


def test_production_accident_title_has_city_once__must_hit():
    """post 14 的原始入参 → 城市只出现一次,后缀只出现一次。"""
    t = build_title("深圳全屋定制哪家好", "深圳", variant_index=0).title
    assert t.count("深圳") == 1, t
    assert t.count("哪家好") == 1, t


def test_control_group_title_unchanged__must_not_hit():
    """对照组 post 10/12/13 的入参 → 标题与生产**逐字**相同(没被修坏)。"""
    t = build_title("全屋定制", "深圳", variant_index=0).title
    assert t == "深圳全屋定制哪家好？5家实测对比：报价区间、工期和避坑要点一次说清", t


def test_two_different_cities_in_keyword_survive__must_not_hit():
    """🔴 工单 §2.5 第 1 行的反向面:不许一刀切"城市只能出现一次"。

    「深圳到广州搬家」里两个地名都是内容本身,一个都不许删。
    """
    t = build_title("深圳到广州搬家", "深圳", variant_index=0).title
    assert "深圳到广州搬家" in t, t
    assert "广州" in t and "深圳" in t


def test_keyword_without_city_keeps_city_in_matrix__must_hit():
    """城市矩阵:关键词不含城市时每格都带各自的城市。"""
    out = build_city_matrix("全屋定制", ["深圳", "广州", "杭州"])
    assert [v.city for v in out] == ["深圳", "广州", "杭州"]
    for v in out:
        assert v.city in v.title, v.title


def test_matrix_cell_matching_keyword_city_not_doubled__must_hit():
    """矩阵里与关键词自带地名撞车的那一格不叠加,其余格照常。"""
    out = build_city_matrix("深圳全屋定制", ["深圳", "广州"])
    sz = next(v for v in out if v.city == "深圳")
    gz = next(v for v in out if v.city == "广州")
    assert sz.title.count("深圳") == 1, sz.title
    # 🔴 已知且有意保留的边界:关键词自带的是**另一个**城市时前缀照加
    #    （'广州深圳全屋定制…'）。判「加不加」只看"要加的这个城市在不在里面",
    #    因为无法区分「深圳」是数据陈旧还是内容本身（"深圳到广州搬家"）。
    #    详见交付说明 §项2-已知边界。
    assert gz.title.startswith("广州"), gz.title


# ===========================================================================
# B. 规则二:词尾问句后缀不叠加(限定白名单 + 只在拼接边界)
# ===========================================================================
def test_duplicate_question_tail_removed__must_hit():
    assert trim_duplicate_tail("深圳全屋定制哪家好", "哪家好？5家实测对比") == "深圳全屋定制"


def test_tail_kept_when_template_is_not_a_question_tail__must_not_hit():
    """模板紧跟的不是问句后缀 → 关键词的尾巴原样保留(不做通用去重)。"""
    assert trim_duplicate_tail("深圳全屋定制哪家好", "top5分享") == "深圳全屋定制哪家好"
    assert trim_duplicate_tail("深圳全屋定制哪家好", "避坑指南") == "深圳全屋定制哪家好"


def test_keyword_without_tail_untouched__must_not_hit():
    assert trim_duplicate_tail("全屋定制", "哪家好？") == "全屋定制"


def test_tail_only_keyword_not_emptied__must_not_hit():
    """整条关键词就是一个后缀时不许剪成空串(剪空 = 标题里没有品类词)。"""
    assert trim_duplicate_tail("推荐", "推荐：口碑较好的") == "推荐"


def test_normal_repetition_in_body_untouched__must_not_hit():
    """🔴 工单 §2.4:不许把正文里的正常重复也改掉。

    「越用越好用」既不在白名单里、也不在拼接边界上,两条规则都碰不到它。
    """
    body = "越用越好用，回购的人也越来越多"
    assert trim_duplicate_tail(body, "哪家好？") == body


def test_tail_whitelist_has_no_generic_words__must_not_hit():
    """白名单只收查询意图后缀,不许混进品类词/形容词(那就成通用去重了)。"""
    for bad in ("好用", "定制", "服务", "公司", "价格便宜的"):
        assert bad not in QUESTION_TAILS, bad


# ===========================================================================
# C. hashtag 与生图 prompt 同一个成因,必须一起过
# ===========================================================================
def test_hashtags_have_no_doubled_city_or_tail__must_hit():
    tags = build_hashtags("深圳全屋定制哪家好", "深圳")
    assert tags, "hashtag 空,后面的断言全部无效"
    for t in tags:
        assert t.count("深圳") == 1, t
        assert t.count("哪家好") <= 1, t


def test_hashtags_still_carry_city_when_absent__must_not_hit():
    tags = build_hashtags("全屋定制", "深圳")
    assert any(t.startswith("深圳") for t in tags), tags


def test_image_prompt_locale_not_doubled__must_hit():
    from services.geo_douyin.image_pipeline import build_card_prompt

    p = build_card_prompt("标题", "", keyword="深圳全屋定制哪家好", city="深圳")
    assert "主题：深圳全屋定制哪家好" in p, p
    assert "深圳深圳" not in p, p


def test_image_prompt_locale_still_has_city__must_not_hit():
    from services.geo_douyin.image_pipeline import build_card_prompt

    p = build_card_prompt("标题", "", keyword="全屋定制", city="深圳")
    assert "主题：深圳全屋定制" in p, p


def test_topic_dispatcher_title_goes_through_shared_piece__must_hit():
    """第 5 个调用点(writing 侧的对比稿标题)也走共用件。"""
    out = compose_title("{year}{city}{kw}怎么选？证据核验与避坑清单",
                        city="深圳", keyword="深圳全屋定制", year=2026)
    assert out == "2026深圳全屋定制怎么选？证据核验与避坑清单", out


def test_topic_dispatcher_title_keeps_region_when_absent__must_not_hit():
    out = compose_title("{year}{city}{kw}怎么选？证据核验与避坑清单",
                        city="深圳", keyword="全屋定制", year=2026)
    assert out == "2026深圳全屋定制怎么选？证据核验与避坑清单", out


# ===========================================================================
# D. 依赖契约:地名 token 拆分复用诊断链的 SSOT,别人改了要让本包转红
# ===========================================================================
def test_geo_tokens_contract_pinned__must_hit():
    """🔴 ``geo_tokens`` 属另一个在途包(qgate-r567)的文件,本包只读用。

    它若被改成"整串才算命中",``effective_city_prefix`` 会静默退化成
    "永远判没带地名" —— 那正是本单要修的 bug 复活。这条把契约钉死。
    """
    from services.diagnosis_question_quality import geo_tokens

    assert geo_tokens("深圳") == ("深圳",)
    assert "深圳" in geo_tokens("广东省深圳市")
    assert geo_tokens("") == ()


def test_geo_tokens_is_not_everything__must_not_hit():
    """成对反向:不是什么串都能拆出 token(否则上面那条恒真)。"""
    from services.diagnosis_question_quality import geo_tokens

    assert geo_tokens("全国") == ()


# ===========================================================================
# E. 🔴 绕过锁:新增拼标题的调用点不过共用件 → 转红
#    (工单 §2.5 第 4 行:不许用"约定大家都调"代替可执行的锁)
# ===========================================================================
_SKIP_DIRS = {
    ".git", "frontend", "node_modules", "docs", "tests", "scripts",
    "_archive", "_archived", "prompt_archive", "data", "output", "cache",
    "logs", ".venv", "venv", "agent-test-artifacts",
}

# 「地名槽紧挨着品类/关键词槽」的拼接形态。
_CONCAT_RE = re.compile(
    r"\{\s*(?:city|town|region|地域)\s*\}\s*"
    r"\{\s*(?:kw|keyword|industry|category|trade|品类|district)\s*\}"
)

HYGIENE_MODULE = "services.geo_title_hygiene"

# 必须 import 共用件的文件(=真·标题拼接点)。
# 🔴 ``image_pipeline`` 改完后已不再命中扫描正则(它原来是 f-string 形态),
#    所以这里**直接查 import**,不依赖扫描命中 —— 否则改法一变锁就静默失效。
ROUTED_SITES = (
    "services/geo_douyin/title_engine.py",
    "services/geo_douyin/image_pipeline.py",
    "writing/topic_dispatcher.py",
)

# 命中扫描但**不是**确定性标题拼接的地方,逐条登记理由。
# 新增一处而不登记 → 本文件转红,逼着新增者先判"要不要过共用件"。
KNOWN_NON_TITLE_SITES = {
    "services/geo_title_hygiene.py":
        "共用件自己(docstring 里的示例模板)",
    "services/diagnosis_question_quality.py":
        "只在 docstring 里出现;该链有自己的 R1/R4/R5 实现,工单 §2.4 明令不动",
    "writing/title_formula_library.py":
        "渲染进 LLM prompt 的公式说明({地域}{品类} 是给模型看的占位符),"
        "标题由模型写 —— 输入侧去重管不到,要治得在输出侧,不在本单范围",
    "services/action_personalizer.py":
        "行动建议文案模板({city}{district}/{city}{industry}),"
        "district/industry 是系统派生的分类值不是用户关键词,不存在'自带城市'路径",
    "services/theme_package_builder.py":
        "主题包话术模板(问句示例),不是落库标题",
    "api/brand_api.py":
        "brands.business 字段的默认值 f\"{city}{industry}服务\",不是标题",
    "server.py":
        "竞品调研 prompt 与搜索 query 拼接,不是标题",
    "tools/industry_knowledge_collector.py":
        "搜索 query 拼接(带空格分词),不是标题",
}


def _code_only(text: str) -> str:
    # 🔴 [Deploy-CTO 2026-08-05 合车时修] 扫描器原本把**注释与 docstring**也当代码算。
    #   合车实证:geovid 节在 api/geo_douyin_api.py:331 与
    #   services/geo_douyin/production_task.py:235 的注释里**引用事故原文**
    #   (「无条件拼 {city}{kw} → 深圳深圳AI搜索优化」),而那两个文件的代码路径
    #   正好是把模板池删掉了 —— 锁把「写下来的教训」当成了「犯错」。
    #   为什么剪注释而不是登记白名单:登记要写一句理由,理由是**写死的快照**,
    #   等谁哪天真在那两个文件里加了拼接,理由烂了而锁不会响。
    #   🔴 字符串字面量**不剔** —— 真拼接就写在 f-string 里,剔了锁当场失去全部判别力。
    doc_lines: set[int] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            doc_lines.update(
                range(first.lineno, (first.end_lineno or first.lineno) + 1)
            )
    kept: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.start[0] in doc_lines:
                continue
            kept.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    return "\n".join(kept)


def _scan_hits() -> dict[str, int]:
    hits: dict[str, int] = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n = len(_CONCAT_RE.findall(_code_only(text)))
        if n:
            hits[rel.as_posix()] = n
    return hits


def _imports(path: pathlib.Path) -> set[str]:
    """AST 取真实 import —— 正则会把注释/字符串里的模块名算进去(假绿)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_scan_is_not_empty__must_hit():
    """反向对照:扫描口径不是空的。

    命中 0 条时下面那条"全部已登记"会**恒真** —— 空集合永远满足任何约束。
    """
    hits = _scan_hits()
    assert len(hits) >= 5, f"扫描只命中 {len(hits)} 个文件,口径可能坏了:{hits}"
    assert "services/geo_douyin/title_engine.py" in hits, hits


def test_no_bypassing_title_concat_site__must_hit():
    """命中拼接形态的文件,要么走共用件,要么在清单里带理由登记。"""
    allowed = set(ROUTED_SITES) | set(KNOWN_NON_TITLE_SITES)
    unknown = sorted(set(_scan_hits()) - allowed)
    assert not unknown, (
        "下列文件在拼「地名+品类/关键词」但既没走 services.geo_title_hygiene、"
        f"也没在 KNOWN_NON_TITLE_SITES 里登记理由:{unknown}\n"
        "→ 先判它是不是标题:是就 import 共用件,不是就登记理由(带一句为什么)。"
    )


def test_routed_sites_really_import_the_shared_piece__must_hit():
    for rel in ROUTED_SITES:
        mods = _imports(REPO / rel)
        assert HYGIENE_MODULE in mods, f"{rel} 没有 import {HYGIENE_MODULE}(实得 {sorted(mods)[:8]}…)"


def _hygiene_call_count(path: pathlib.Path) -> int:
    """共用件被**真正调用**的次数(AST,不是 grep)。

    🔴 只查 import 不够:`import 了但没调` = 功能等于不存在,而 import 检查是绿的。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == HYGIENE_MODULE:
            bound.update((a.asname or a.name) for a in node.names)
    if not bound:
        return 0
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in bound:
                n += 1
            elif isinstance(fn, ast.Attribute) and fn.attr in bound:
                n += 1
    return n


def test_routed_sites_actually_call_it__must_hit():
    """每个登记的调用点至少真调一次;title_engine 的四个拼接点一个都不能少。

    四个拼接点 = build_title / build_city_matrix 各 1 次 compose_title
                + build_hashtags 3 次 join_city_keyword。任一处被改回裸拼 → 计数掉 → 转红。
    """
    for rel in ROUTED_SITES:
        assert _hygiene_call_count(REPO / rel) >= 1, f"{rel} import 了共用件却一次都没调"
    assert _hygiene_call_count(REPO / "services/geo_douyin/title_engine.py") >= 5


def test_call_counter_is_not_always_positive__must_not_hit():
    """成对反向:计数器对没接线的文件必须给 0(否则上面那条恒真)。"""
    assert _hygiene_call_count(REPO / "services/theme_package_builder.py") == 0


def test_scan_catches_a_brand_new_bypassing_file__must_hit(tmp_path, monkeypatch):
    """🔴 判别力自证:凭空造一个绕过共用件的新调用点,扫描必须把它抓出来。

    没有这一条,上面那条"全部已登记"就只是在陈述现状 ——
    它证明不了"以后新增会被抓到"。
    """
    intruder = REPO / "services" / "_acc3fix_probe_intruder.py"
    intruder.write_text(
        'TITLE = "{city}{kw}哪家好？{n}家实测对比"\n', encoding="utf-8"
    )
    try:
        hits = _scan_hits()
        allowed = set(ROUTED_SITES) | set(KNOWN_NON_TITLE_SITES)
        assert "services/_acc3fix_probe_intruder.py" in hits
        assert sorted(set(hits) - allowed) == ["services/_acc3fix_probe_intruder.py"]
    finally:
        intruder.unlink(missing_ok=True)
    # 探针删掉后必须恢复干净 —— 否则这条用例会污染后面所有断言
    assert not sorted(set(_scan_hits()) - (set(ROUTED_SITES) | set(KNOWN_NON_TITLE_SITES)))


def test_douyin_isolation_still_holds__must_not_hit():
    """共用件不许把 writing.* 的标题模板池带进 title_engine(§3 隔离约束)。"""
    mods = _imports(REPO / "services" / "geo_douyin" / "title_engine.py")
    forbidden = {m for m in mods if m.startswith("writing.")}
    assert not forbidden, f"隔离被破坏:{forbidden}"
    hyg = _imports(REPO / "services" / "geo_title_hygiene.py")
    assert not {m for m in hyg if m.startswith("writing.")}, hyg
