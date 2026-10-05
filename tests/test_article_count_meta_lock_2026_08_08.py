"""元判据锁:全仓不许存在第二套「写死篇数」逻辑(P1 · 2026-08-08)

这不是普通用例,是**元判据** —— 它不校验某个函数算得对不对,而是校验
「篇数这件事只有一个地方说了算」这个结构性事实。工单硬要求 §5。

三条判据(每条都配反向对照,证明不是恒真):
  L1 AST 级:任何 .py 里出现「键含 entry/standard/flagship、值全是整数字面量」的 dict
             = 第二套档位篇数阶梯 → 红。白名单只有 tools/pricing_bands.py。
  L2 AST 级:任何 .py 里 `required_articles` / `*_articles` 取值时内联整数兜底
             (`.get(x, 1)` / `... or 1`)→ 红。必须走 pricing_bands.normalize_article_capacity。
  L3 文本级:前端 .ts/.tsx 里出现三档篇数字面量 → 红(前端不许自己算篇数)。

🔴 白名单纪律:白名单只放**文件**,不放"某文件里的某一行"。放宽白名单 = 判据自废,
   要放必须在这里写清理由 + 何时收回。
"""
from __future__ import annotations

import ast
import functools
import io
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# ============================================================
# 白名单(唯一 SSOT + 三类不构成"活的第二套")
# ============================================================

# L1 唯一允许出现档位篇数阶梯字面量的文件
LADDER_SSOT = "tools/pricing_bands.py"

# 已知的、**不在生产路由链上**的孤儿阶梯。收编它们属于删死代码,不在本包范围;
# 列在这里是为了让判据现在就能全绿,同时把债务显式化(谁删了它谁把这行也删掉)。
#   实证(2026-08-08):`grep -rn "KEYWORD_TIERS\|PACKAGE_QUOTAS" api/ server.py` = 0 命中,
#   整条 tier1/2/3 + PACKAGE_QUOTAS 链没有任何路由能走到。
L1_KNOWN_ORPHANS = {
    "tools/keyword/keyword_tier.py",          # tier1/2/3 articles_needed 12/6/2(孤儿)
    "tools/geo_managed/estimate_engine.py",   # 托管产品线 SOV_TIERS(独立产品,不进 GEO 报价链)
}

# 扫描范围:后端源码。测试/脚本/前端构建产物不在 L1/L2 范围。
BACKEND_DIRS = ("api", "services", "db", "tools", "writing", "workflows", "agents", "middleware", "auth")

# 🔴 `server.py` 在仓库根,不落在任何 BACKEND_DIRS 里 —— 第一版判据**根本没扫它**,
#    而它是全仓最大的 required_articles 消费方之一。范围漏了 = 判据形同虚设。
#    但它同时是 p4gap-20260808(W2)的**活声明热文件**,本窗口不许改 →
#    处置不是"加白名单放过",而是**纳入范围 + 钉成数量锁死的已知盲区清单**(见 §L4)。
SERVER_ENTRY = "server.py"

# L2:这些名字取值时不许内联整数兜底
CAPACITY_NAMES = {
    "required_articles",
    "entry_articles",
    "standard_articles",
    "flagship_articles",
    "strong_articles",
    "total_required_articles",
}

# L2 白名单。**只放"别的窗口正占着的文件"**,而且每一条都必须在
# tests/test_article_capacity_contract_2026_08_08.py 里配一条**行为锁**逐值钉死它 ——
# 白名单让它不因"写法"转红,行为锁让它一旦"语义"漂移立刻转红。只放不锁 = 判据自废。
L2_ALLOWED = {
    LADDER_SSOT,
    # ① writing/keyword_topic_generator.py —— artstyle-p2-20260808 窗口活声明。
    #    它的 `_required_article_count` 语义与 SSOT normalize_article_capacity 完全一致,
    #    由 test_writing_helper_matches_capacity_ssot 逐值对拍。
    "writing/keyword_topic_generator.py",
    # ② ~~services/geo_douyin/**~~ —— 🔴 2026-08-17 **白名单已收回**(GEO 图文全量包 · 裁定 P0-5)。
    #    原豁免理由是「抖音图文线有自己的配额 quota 概念,`or 1` 是它自己的选择」。
    #    Review-CTO 2026-08-17 裁定 P0-5 把这件事定成**轴级缺陷**:0 容量必须端到端恒 0,
    #    `content_plan.py` 与 `topic_distiller.py` 两处 `or 1` 逐处修复。两处已改走
    #    `pricing_bands.normalize_article_capacity(..., when_missing=0)`,豁免随之作废。
    #    纪律照本文件开头那条:**白名单不许比问题活得久** —— 问题没了,白名单同刻删掉。
}

TIER_KEYS = {"entry", "standard", "flagship", "strong"}


def _iter_py(dirs=BACKEND_DIRS):
    for name in dirs:
        root = REPO / name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


@functools.lru_cache(maxsize=None)
def _parse(path: Path):
    # 按路径缓存(Review 09-28 登记进 ONESHOT 时加):L1 / L2 / L4 原来各自把全部后端文件
    # 和 server.py 重新解析一遍(19s),一次运行内文件不变,解析一次即可。判据只读 AST、不改它。
    try:
        return ast.parse(io.open(path, encoding="utf-8").read(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


# ============================================================
# L1 · 档位篇数阶梯
# ============================================================

# 篇数的物理量级上限。ARTICLE_COMPETITION_CAP=100 时公式上界 ceil(0.5*100/0.5)=100,
# 但档位**底线**表(V2_TIER_MIN_ARTICLES / legacy 1-3)历来都在 60 以内。
# 用它把「出现率百分比档」(50/65/75)这类同形状但不同物理量的表挡在判据外。
MAX_LADDER_ARTICLES = 60

# 名字锚定:篇数表的名字里一定有 article/articles(中文注释不算)。
# 没有名字的(内联字面量直接当实参/返回值)一律**保守命中** —— 匿名阶梯是最容易溜过去的形态。
ARTICLE_NAME_RE = re.compile(r"article", re.IGNORECASE)


def _dict_owner_names(tree: ast.AST) -> dict[int, str]:
    """给每个 Dict 节点找一个「名字」:赋值目标 / 作为某个 dict 键的值 / 关键字实参名。"""
    owners: dict[int, str] = {}

    def _bind(node, name: str) -> None:
        if isinstance(node, ast.Dict) and name:
            owners[id(node)] = name

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    _bind(node.value, target.id)
                elif isinstance(target, ast.Attribute):
                    _bind(node.value, target.attr)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            _bind(node.value, node.target.id)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    _bind(value, key.value)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg:
                    _bind(kw.value, kw.arg)
    return owners


def find_tier_article_ladders(tree: ast.AST) -> list[int]:
    """返回命中行号。三个条件同时成立才算「第二套篇数阶梯」:

      a) dict 字面量的键覆盖 ≥2 个档位名(entry/standard/flagship/strong);
      b) **全部值是 [0, MAX_LADDER_ARTICLES] 内的整数** —— 挡掉出现率浮点档
         (V2_TIER_TARGET_SHARE {entry:0.10,...})与百分比档(_TIER_TARGET_MAP {entry:50,...,flagship:75});
      c) 名字里带 article,或**根本没有名字**(匿名字面量保守命中)。
         c) 挡掉 platform_tier_caps 这类"每档几个平台"的同形状非篇数表。

    🔴 b) 和 c) 都是**放宽**判据的条件,每放宽一条就配一条反向对照证明没放宽过头(见下面四条 test)。
    """
    owners = _dict_owner_names(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if len(TIER_KEYS.intersection(keys)) < 2:
            continue
        values = node.values
        if not values:
            continue
        if not all(
            isinstance(v, ast.Constant) and isinstance(v.value, int) and not isinstance(v.value, bool)
            and 0 <= v.value <= MAX_LADDER_ARTICLES
            for v in values
        ):
            continue
        name = owners.get(id(node))
        if name is not None and not ARTICLE_NAME_RE.search(name):
            continue
        hits.append(node.lineno)
    return hits


def test_l1_tier_article_ladder_only_in_ssot():
    offenders: dict[str, list[int]] = {}
    for path in _iter_py():
        rel = _rel(path)
        if rel == LADDER_SSOT or rel in L1_KNOWN_ORPHANS:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        hits = find_tier_article_ladders(tree)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "发现第二套写死篇数阶梯(只允许存在于 %s):\n%s" % (LADDER_SSOT, offenders)
    )


def test_l1_criterion_actually_fires():
    """反向对照:判据必须对真的第二套阶梯转红,否则上面那条全绿毫无意义。"""
    bad = ast.parse('default_articles = {"entry": 1, "standard": 2, "flagship": 3}\n')
    assert find_tier_article_ladders(bad), "L1 判据对真实违规样本零命中 = 恒真,判据作废"


def test_l1_does_not_flag_target_share():
    """反向对照 2:出现率档(浮点)不是篇数阶梯,不该被误伤 —— 否则 SSOT 自己就红了。"""
    ok = ast.parse('V2_TIER_TARGET_SHARE = {"entry": 0.10, "standard": 0.20, "flagship": 0.30}\n')
    assert not find_tier_article_ladders(ok), "L1 判据误伤出现率档 = 会逼人放宽白名单"


def test_l1_ssot_really_contains_a_ladder():
    """反向对照 3:证明白名单文件里**真有**阶梯 —— 否则"只允许在 SSOT"是句空话。"""
    tree = _parse(REPO / LADDER_SSOT)
    assert tree is not None
    assert find_tier_article_ladders(tree), "SSOT 里没有阶梯 = 白名单指向了错的文件"


# ============================================================
# L2 · 内联整数兜底
# ============================================================

def _nonzero_int(node) -> bool:
    """**非零**整数字面量。零单独放行,理由见 find_inline_capacity_defaults 的 docstring。"""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and node.value != 0
    )


def find_inline_capacity_defaults(tree: ast.AST) -> list[tuple[int, str]]:
    """篇数字段取值时内联**非零**整数兜底 → 命中。两种形态:
      A) `x.get("required_articles", 1)`
      B) `x.get("required_articles") or 1`

    🔴 为什么零放行、非零命中(这条线是本判据的全部判别力所在,别随手改):
      · `, 0` / `or 0` —— 「缺失 = 没有容量」,与 0..capacity 语义自洽,也不会把显式 0 变成别的数;
      · `, 1` / `, 5` / `, 7` / `, 10` —— 每一个都是**在 SSOT 之外复述了一遍篇数**。
        本包实抓:`tools/keyword_value_scorer.py` 就地写着 entry 5 / standard 7 / flagship 10,
        是主合同阶梯的**逐字第五份拷贝**,改 SSOT 它不跟 —— 这正是要锁的东西;
      · `or 1` 还额外有害:它把**显式 0 篇**(覆盖词 is_core=False · 生产实测 218 行)当假值吃掉。
    """
    hits: list[tuple[int, str]] = []

    def _is_capacity_str(node) -> bool:
        return isinstance(node, ast.Constant) and node.value in CAPACITY_NAMES

    for node in ast.walk(tree):
        # 形态 A:.get("required_articles", <非零 int>)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and _is_capacity_str(node.args[0])
            and _nonzero_int(node.args[1])
        ):
            hits.append((node.lineno, f'.get("{node.args[0].value}", {node.args[1].value})'))
        # 形态 B:<...get("required_articles")...> or <非零 int>
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            has_capacity = any(
                _is_capacity_str(sub)
                for value in node.values
                for sub in ast.walk(value)
            )
            if has_capacity and any(_nonzero_int(v) for v in node.values):
                hits.append((node.lineno, "`... or <非零 int>`"))
    return hits


def test_l2_no_inline_capacity_default_outside_ssot():
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_py():
        rel = _rel(path)
        if rel in L2_ALLOWED:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        hits = find_inline_capacity_defaults(tree)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "篇数缺失兜底必须走 pricing_bands.normalize_article_capacity,"
        "内联整数 = 第二套口径(且 `or 1` 会把显式 0 吃掉):\n%s" % offenders
    )


@pytest.mark.parametrize(
    "src",
    [
        'a = kw.get("required_articles", 1)\n',
        'a = int(kw.get("required_articles") or 1)\n',
        'a = sum(int(k.get("required_articles") or 1) for k in ks)\n',
    ],
)
def test_l2_criterion_actually_fires(src):
    assert find_inline_capacity_defaults(ast.parse(src)), f"L2 判据对 {src!r} 零命中 = 恒真"


@pytest.mark.parametrize(
    "src",
    [
        'a = normalize_article_capacity(kw.get("required_articles"), when_missing=D)\n',
        'a = kw.get("keyword", 1)\n',
        'a = kw.get("required_articles")\n',
        # 零兜底显式放行(见 find_inline_capacity_defaults docstring)。
        # 这两条既是"不过度命中"的对照,也把"零放行"这个决定钉死:
        # 谁把零也改成红,这两条立刻转红,逼他回来读理由。
        'a = kw.get("required_articles", 0)\n',
        'a = int(kw.get("required_articles") or 0)\n',
    ],
)
def test_l2_criterion_does_not_overfire(src):
    assert not find_inline_capacity_defaults(ast.parse(src)), f"L2 判据误伤 {src!r} = 会逼人放宽白名单"


# ============================================================
# L3 · 前端不许自己写三档篇数
# ============================================================

FRONTEND_LADDER_RE = re.compile(
    r"entry\s*:\s*\d{1,3}\s*,\s*standard\s*:\s*\d{1,3}\s*,\s*flagship\s*:\s*\d{1,3}"
)

# 🔴 L3 第二形态(2026-08-08 交付前复扫补的):`required_articles ... || 1`。
#   第一版 L3 只扫三档阶梯,**漏掉了前端的非零兜底** —— 而漏掉的那处
#   (旧对话 UI 的 GEO 方案卡(已随开源 E3 删) 构造 POST body)是**写入路径**:计划说 0 篇会被静默写成冻结 1 篇容量。
#   教训:元判据在后端锁了"非零内联兜底",前端却只锁了"阶梯",两边口径不对称 = 留门。
#
# 🔴🔴 第二次收紧(同一天,同一条判据):上面那版写的是 `[^;\n]{0,80}`,**排除了换行** ——
#   于是 `旧 C 端 GEO 方案页(已随开源 E3 删):449-451` 那个**跨三行的三元表达式**照样漏检:
#       required_articles: Number(k[`${tier}_price`]
#         ? Math.max(1, Math.round(...))
#         : k.strategy?.articles_needed || 1),
#   而它和 旧对话 UI 的 GEO 方案卡 是**同类同端点**(都 POST /api/writing/projects/from-c-end-plan),
#   `/c/geo-plan` 路由可达(旧 C 端嵌入面板 lazy import)。
#   → 改成允许跨行(`[^;]` 天然含 \n),用「到下一个 `;` 为止」限定表达式边界,
#     再加 300 字符上限防跨语句误吞。教训:**排除换行 = 只锁单行写法,换个格式就绕过去了。**
# 🔴 2026-08-08 补完:锚点补上 `articles_needed`。
#   旧 C 端 GEO 方案页 逐词展示那处写的是 `kw.strategy?.articles_needed ?? 1`,
#   只锚 `required_articles` 扫不到它 —— 同一个文件、同一类 bug,漏了半年。
#   试过再补 `_articles` 通配,但 `[^;]{0,300}` 会跨过好几个字段,在
#   OrderReviewPanel / WritingHall 造出 2 条误报 → 只收窄到这两个确切名字。
FRONTEND_NONZERO_FALLBACK_RE = re.compile(
    r"(?:required_articles|articles_needed)[^;]{0,300}?(?:\|\||\?\?)\s*([1-9]\d*)"
)

# L3 白名单。同 L2 的纪律:**只放行,不静默** —— 每条都配一条锁钉死"放行的到底是什么"。
L3_ALLOWED_INPUT_FORMS = {
    "frontend/src/pages/Writing/WritingHall.tsx": {
        # ① 手动"添加关键词"表单:Math.max(1, Math.min(50, Number(...) || 1))。
        #    这是**显式业务规则**(输入框自己写着 min={1},用户看得见),不是读取容量时的静默兜底。
        "Math.max(1, Math.min(50",
        # ② 逐词徽章 `需{kw.required_articles || 1}篇` 的白名单
        #    —— **已于 2026-09-17(WO_232-c232')删除**。
        #    那条徽章由 `36ca89fad`(#220-a1,进 0913g)从 WritingHall.tsx 移除,
        #    分歧不复存在;原作者定的规矩是「白名单不许比问题活得久」,照办。
        #    (配套的 test_l3_writinghall_badge_divergence_is_still_open 同批退役,
        #     退役理由写在它原来的位置。)
    },
}


def find_frontend_ladders(text: str) -> list[str]:
    return FRONTEND_LADDER_RE.findall(text)


def find_frontend_nonzero_fallbacks(text: str, rel: str = "") -> list[str]:
    allowed = L3_ALLOWED_INPUT_FORMS.get(rel, set())
    hits: list[str] = []
    for match in FRONTEND_NONZERO_FALLBACK_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start: line_end if line_end != -1 else len(text)]
        if any(token in line for token in allowed):
            continue
        hits.append(line.strip()[:140])
    return hits


def test_l3_frontend_has_no_article_ladder():
    src_root = REPO / "frontend" / "src"
    if not src_root.is_dir():
        pytest.skip("frontend/src 不存在")
    offenders: dict[str, list[str]] = {}
    for path in list(src_root.rglob("*.ts")) + list(src_root.rglob("*.tsx")):
        text = io.open(path, encoding="utf-8", errors="ignore").read()
        hits = find_frontend_ladders(text)
        if hits:
            offenders[_rel(path)] = hits
    assert not offenders, "前端不许自己写三档篇数阶梯(篇数由后端下发):\n%s" % offenders


def test_l3_criterion_actually_fires():
    assert find_frontend_ladders("const a = { entry: 1, standard: 2, flagship: 3 };"), \
        "L3 判据对真实违规样本零命中 = 恒真"


def test_l3_frontend_has_no_nonzero_capacity_fallback():
    """前端不许用 `|| 1` 给篇数兜底 —— 尤其写入路径,那会把「0 篇」写成「1 篇容量」。"""
    src_root = REPO / "frontend" / "src"
    if not src_root.is_dir():
        pytest.skip("frontend/src 不存在")
    offenders: dict[str, list[str]] = {}
    for path in list(src_root.rglob("*.ts")) + list(src_root.rglob("*.tsx")):
        rel = _rel(path)
        text = io.open(path, encoding="utf-8", errors="ignore").read()
        hits = find_frontend_nonzero_fallbacks(text, rel)
        if hits:
            offenders[rel] = hits
    assert not offenders, "前端篇数非零兜底(`|| 1`)会吃掉显式 0 篇容量:\n%s" % offenders


@pytest.mark.parametrize("src", [
    "required_articles: k.strategy?.articles_needed || 1,",
    "需{kw.required_articles || 1}篇",
    "const n = row.required_articles ?? 1;",
    # 🔴 这条是新补的锚点的定点回归:旧 C 端 GEO 方案页 逐词展示的原文形态,
    #    只锚 required_articles 的第一版对它零命中。
    "const articleCount = tierPrice > 0 ? 1 : (kw.strategy?.articles_needed ?? 1);",
])
def test_l3_nonzero_fallback_criterion_actually_fires(src):
    assert find_frontend_nonzero_fallbacks(src), f"L3 第二形态对 {src!r} 零命中 = 恒真"


# 🔴 这条是"判据自己漏检过"的定点回归。样本是 旧 C 端 GEO 方案页(已随开源 E3 删):449-451 的**修复前原文**
#   (跨三行的三元表达式)。第一版 L3 的 `[^;\n]` 排除换行 → 对它零命中,于是一个**写入路径**的
#   容量 bug 从元判据底下走过去了。判据改宽以后必须对这个真实样本转红,否则"改宽了"是句空话。
CEND_PRE_FIX_SNIPPET = """        required_articles: Number(k[`${selectedTier}_price` as keyof KeywordResult]
          ? Math.max(1, Math.round((Number(k[`${selectedTier}_price` as keyof KeywordResult]) || 0) / 60))
          : k.strategy?.articles_needed || 1),
"""


def test_l3_catches_the_cross_line_form_it_once_missed():
    assert find_frontend_nonzero_fallbacks(CEND_PRE_FIX_SNIPPET), (
        "跨行三元形态仍然漏检 —— 这正是 旧 C 端 GEO 方案页 那处溜过去的原因"
    )


@pytest.mark.parametrize("src", [
    "required_articles: k.strategy?.articles_needed ?? 0,",
    "需{Number(kw.required_articles) || 0}篇",
    "const n = row.some_other_field || 1;",
])
def test_l3_nonzero_fallback_does_not_overfire(src):
    assert not find_frontend_nonzero_fallbacks(src), f"L3 第二形态误伤 {src!r}"


# ── 已退役:test_l3_writinghall_badge_divergence_is_still_open(2026-09-17 · WO_232-c232')
#
# 它断言「WritingHall 逐词徽章 `需{kw.required_articles || 1}篇` 与同屏汇总
# `Number(...) || 0` 的口径分歧**仍然存在**」。**修法已上线,锁随修法退役。**
#
# 事实(在 a4ecdef33 上实读,不是转述):
#   · `36ca89fad`(#220-a1,进 0913g)把那条徽章从 WritingHall.tsx 删掉了;
#   · 该串在 `f6f6ba472`(0913f)里还有 1 处,在 `a4ecdef33` 里 0 处;
#   · 于是这条锁从 0913g 起就红,而 0913g 装车时漏退。
#
# 🔴 它不是"坏了",是**按设计转红的**:本仓 criterion-pinning-the-defect-fights-the-fix。
#    钉住「缺陷仍在」的判据,在缺陷被修好的那一刻必然变红 —— 它的价值是那一刻
#    把人叫回来做善后,而不是一直绿着。
# 🔴 善后它当初就写明了,而修的人没做:「同步删掉 L3_ALLOWED_INPUT_FORMS 里对应
#    那一行白名单」。本单一并删除(见上方 L3_ALLOWED_INPUT_FORMS)——
#    **白名单不许比问题活得久**,那句话是原作者写的,现在轮到我们执行。


# ============================================================
# L4 · server.py 已知盲区清单(**数量锁死**,不是白名单)
# ============================================================
#
# 为什么不是白名单:白名单 = "这个文件随便写",清单 = "这个文件现在恰好有 N 处,多一处少一处都红"。
# 后者能同时挡住两种退化:①有人往里加第 N+1 处;②有人改好了一处却不来更新清单
#   (那说明清单没人维护,下次它就骗人了)。
#
# 为什么不直接修:`server.py` 是 p4gap-20260808(W2)的活声明热文件,
#   本窗口不动别人正在改的热文件(开工纪律)。修它归后续窗口,清单是交接物。
#
# 三种形态各自计数(它们的判据互相独立,合并计数会互相掩盖):
SERVER_BLIND_SPOT_COUNTS = {
    # ① AST 级内联非零兜底(与 L2 同一把判据,只是范围扩到 server.py)
    #    实测 4 处:_first_positive(...) or 1 / .get("required_articles", 1) /
    #    k.get(...) or 1 / int(kw_row.get(...) or 1)
    #    (09-28 Review · 自媒体单生成标题 500:generate-titles 自定义配比那段 `int(_kw.get("required_articles") or 1)`
    #     随「按条排计划」一起删掉 —— 它把 0 槽的词当 1 槽占一份配比。5 → 4。)
    "inline_ast": 4,
    # ② SQL 里的 COALESCE(required_articles, 1) —— AST 扫不到(它在字符串字面量里)
    "sql_coalesce": 3,
    # ③ Pydantic 请求模型默认值 required_articles: int = 1
    #    (这一处**语义上说得过去**:手工建词接口的入参默认;列进来是为了有人改它时被看见)
    #    [开源 E3 · B3a · 2026-09-28] 2 → 1:CEndPlanKeywordItem 随 C 端计划建写作项目端点删除;剩 AddKeywordRequest
    "pydantic_default": 1,
}

SQL_COALESCE_RE = re.compile(r"COALESCE\(\s*(?:\w+\.)?required_articles\s*,\s*([1-9]\d*)\s*\)")
PYDANTIC_DEFAULT_RE = re.compile(r"^\s*required_articles\s*:\s*int\s*=\s*([1-9]\d*)\s*$", re.M)


def count_server_blind_spots() -> dict[str, int]:
    path = REPO / SERVER_ENTRY
    text = io.open(path, encoding="utf-8").read()
    tree = _parse(path)
    assert tree is not None, "server.py 解析失败 —— 判不了,不是 0 个盲区"
    return {
        "inline_ast": len(find_inline_capacity_defaults(tree)),
        "sql_coalesce": len(SQL_COALESCE_RE.findall(text)),
        "pydantic_default": len(PYDANTIC_DEFAULT_RE.findall(text)),
    }


def test_l4_server_blind_spot_inventory_is_frozen():
    """server.py 的「缺失篇数 → 1」盲区**数量锁死**。

    多一处 = 有人又写了一份第二套逻辑;少一处 = 有人修好了但没更新清单。
    两种都必须红,并逼改的人回来把这份清单改对。
    """
    actual = count_server_blind_spots()
    assert actual == SERVER_BLIND_SPOT_COUNTS, (
        "server.py 已知盲区数量变了(期望 %s,实测 %s)。\n"
        "  · 变多 → 你新写了一处「缺失篇数→1」,请改走 pricing_bands.normalize_article_capacity;\n"
        "  · 变少 → 你修好了一处,请同步把上面 SERVER_BLIND_SPOT_COUNTS 的数字改小。"
        % (SERVER_BLIND_SPOT_COUNTS, actual)
    )


def test_l4_inventory_is_not_frozen_at_zero():
    """反向对照:清单里每一类都必须 > 0。

    「锁死在 0」是最容易自造的恒真 —— 那等于没锁。真有 0 的那天,应该是把这一类**删掉**,
    而不是留一个恒真的 0 在这里充数。
    """
    for form, expected in SERVER_BLIND_SPOT_COUNTS.items():
        assert expected > 0, f"{form} 期望值是 0 = 恒真条款,应该删掉这一类而不是留着"
    actual = count_server_blind_spots()
    for form, n in actual.items():
        assert n > 0, f"{form} 实测 0 命中 → 该形态的正则/判据已失效(不是代码变干净了)"


def test_l4_counting_has_discriminating_power():
    """注入一处合成盲区 → 计数必须 +1。证明这不是"数了个恒定的常数"。"""
    text = io.open(REPO / SERVER_ENTRY, encoding="utf-8").read()
    baseline = len(find_inline_capacity_defaults(_parse(REPO / SERVER_ENTRY)))
    injected = ast.parse(text + '\n_probe = _row.get("required_articles", 1)\n')
    assert len(find_inline_capacity_defaults(injected)) == baseline + 1, "L4 计数对新增盲区无反应"

    sql_baseline = len(SQL_COALESCE_RE.findall(text))
    assert len(SQL_COALESCE_RE.findall(
        text + "\nSELECT COALESCE(required_articles, 1) FROM x\n")) == sql_baseline + 1
    pyd_baseline = len(PYDANTIC_DEFAULT_RE.findall(text))
    assert len(PYDANTIC_DEFAULT_RE.findall(
        text + "\n    required_articles: int = 1\n")) == pyd_baseline + 1


def test_l4_zero_default_is_not_counted_as_a_blind_spot():
    """反向对照:零兜底不是盲区(与 L2 同一条线)。否则清单会把好写法也算进去,数字失去意义。"""
    assert not SQL_COALESCE_RE.findall("COALESCE(required_articles, 0)")
    assert not PYDANTIC_DEFAULT_RE.findall("    required_articles: int = 0\n")


# 🔴 2026-08-08 补完:这一格从「钉住 1 处」升级成「**全仓零处**」。
#    原文是 旧 C 端 GEO 方案页(已随开源 E3 删) 的 `Math.round(price / 60)` —— 前端拿价格反推篇数,
#    还写死 ¥60/篇成本口径(08_billing §3.3「前端不算钱」的同一类问题)。
#    生产真样本(3 个 done 方案 · 33 个核心词)实测:32/33 反推值 ≠ 真实容量,
#    合计 664 篇 vs 真实 416 篇 —— 多冻结 60%。根治法 = 后端下发 `{tier}_articles`。
#
# 🔴 第一版正则写的是 `[^)]*?`,而真实代码是 `Math.round((Number(k[`${tier}_price` as keyof X]) || 0) / 60)`
#   —— 括号是**嵌套**的,`[^)]` 跨不过去 → 实测 0 命中,而我却把清单钉成 1。
#   是"锁死处数"这条断言自己把空判据顶红的。这就是"每个必须命中都配一个数字"的价值。
#   现在除数也放宽成 `\d+`:换个成本口径(÷80)照样是前端算价,不能只认 60。
#
# 🔴 第二次放宽(同一天):锚点原本是 `_price`(字段名形态),漏掉了**同一个文件里**
#   `const tierPrice = ...; Math.round(tierPrice / 60)` 这处 —— 局部变量名不带下划线。
#   全仓扫描当时报"零处",而真实的第二处就在那儿;是接线锁(逐字断言)把它顶出来的,
#   不是这条正则。所以锚点改成大小写不敏感的 `price`。
PRICE_DERIVED_RE = re.compile(r"Math\.round\([\s\S]{0,160}?[Pp]rice[\s\S]{0,160}?/\s*\d+\s*\)")

# 修复前的原文(逐字)。留作**反向对照样本**:判据必须对它转红,
# 否则"全仓零处"只是因为正则什么都抓不到。
CEND_PRICE_DERIVED_PRE_FIX = """        required_articles: Number(k[`${selectedTier}_price` as keyof KeywordResult]
          ? Math.max(1, Math.round((Number(k[`${selectedTier}_price` as keyof KeywordResult]) || 0) / 60))
          : k.strategy?.articles_needed ?? 0),
"""


def _frontend_sources():
    src_root = REPO / "frontend" / "src"
    if not src_root.is_dir():
        return []
    return list(src_root.rglob("*.ts")) + list(src_root.rglob("*.tsx"))


def test_l4_frontend_derives_zero_capacity_from_price():
    """**前端零处算价**:全仓 frontend/src 不许出现「价格 ÷ 常数 → 篇数」。

    不是白名单、不是钉处数 —— 是 0。篇数只能读后端下发的容量字段。
    """
    paths = _frontend_sources()
    assert paths, "frontend/src 扫不到任何 .ts/.tsx —— 判据没有扫描面 = 恒真"
    offenders: dict[str, list[str]] = {}
    for path in paths:
        text = io.open(path, encoding="utf-8", errors="ignore").read()
        hits = PRICE_DERIVED_RE.findall(text)
        if hits:
            offenders[_rel(path)] = [str(h)[:120] for h in hits][:3]
    assert not offenders, (
        "前端在拿价格反推篇数(08_billing §3.3 前端不算钱)。"
        "篇数走后端 services/c_end_plan_capacity 下发的 `{tier}_articles`:\n%s" % offenders
    )


def test_l4_price_derived_criterion_actually_fires():
    """反向对照:判据对**修复前的真实原文**必须命中,否则"全仓零处"是空话。"""
    assert PRICE_DERIVED_RE.findall(CEND_PRICE_DERIVED_PRE_FIX), \
        "价格反推判据对修复前原文零命中 = 恒真"
    # 换个成本口径照样是前端算价
    assert PRICE_DERIVED_RE.findall("Math.round((Number(k.standard_price) || 0) / 80)")
    # 局部变量形态(第一版漏检的那种)也必须命中
    assert PRICE_DERIVED_RE.findall("const a = Math.max(1, Math.round(tierPrice / 60));")
    # 与价格无关的四舍五入不该误伤
    assert not PRICE_DERIVED_RE.findall("Math.round(progress / 10)")


def test_l3_input_form_whitelist_still_matches_reality():
    """白名单靠"这一行里有 Math.max(1, Math.min(50" 生效。承载体一改,白名单就成了空条款 ——
    这条锁保证白名单不会悄悄失效成"什么都不放行"或"放行一大片"。"""
    rel = "frontend/src/pages/Writing/WritingHall.tsx"
    text = io.open(REPO / rel, encoding="utf-8", errors="ignore").read()
    assert "Math.max(1, Math.min(50" in text, "白名单锚点没了 → 该把这条白名单删掉"
    # 反向对照:不带白名单跑,这一处必须命中(证明白名单确实在放行一个真命中)
    assert find_frontend_nonzero_fallbacks(text), "白名单放行的那处其实压根不命中 = 空条款"


@pytest.mark.parametrize("src", [
    'a = kw.get("entry_articles", 5)\n',
    'a = kw.get("standard_articles", 7)\n',
    'a = kw.get("flagship_articles", 10)\n',
])
def test_l2_catches_main_ladder_restated(src):
    """主合同 5/7/10 被就地复述一遍。这是本包在 keyword_value_scorer 里真抓到的第五份拷贝,
    也是「零放行」之后 L2 仍然有判别力的证明。"""
    assert find_inline_capacity_defaults(ast.parse(src)), "L2 放过了主合同阶梯拷贝: %s" % src
