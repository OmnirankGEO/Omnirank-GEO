"""WO_214 c1 判据 · 内部 LLM 成本表按支出大头对账。

工单 `C:\\AI-Test\\WO_214_LLM_COST_TABLE_TOP_SPENDERS_RECONCILE_RV_2026-09-14.md`
(sha256 前 16 位 `ca38565848136a7b`)。

快照:
  `docs/AI-CONTEXT/DASHSCOPE_PRICING_SNAPSHOT_2026-09-15.md`
  `docs/AI-CONTEXT/KIMI_DOUBAO_PRICING_SNAPSHOT_2026-09-15.md`

🔴 **本单只碰内部成本估算**,一个字都不动对客价
   (`feature_pricing` / `transparent_pricing` / 报价快照)。
   下面有一条结构臂钉这件事。

🔴 工单 §1.3 与 §1.2 有一处**互相矛盾**,这里按能自洽的口径写:
   · §1.3 要「`DEFAULT_PRICING` 命中集合 = 空(在跑组合全部登记)」
   · §1.2 要「查不到厂商页的**标「未定价」并让兜底继续告警**,不用猜的数字填表」
   查不到价的组合**不可能**既登记又继续落兜底。
   所以这里把口径落成:**已知未定价集合冻结** —— 那几个必须**不在**表里
   (继续告警),而**新增**的未登记组合要有人看过才能进这个集合。
   空集那一条等厂商页能查到价再说,不用猜的数字凑。
"""
import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRACKER = ROOT / "tools" / "llm_call_tracker.py"
SNAP_DASHSCOPE = ROOT / "docs" / "AI-CONTEXT" / "DASHSCOPE_PRICING_SNAPSHOT_2026-09-15.md"
SNAP_KIMI_DOUBAO = ROOT / "docs" / "AI-CONTEXT" / "KIMI_DOUBAO_PRICING_SNAPSHOT_2026-09-15.md"


@pytest.fixture(scope="session")
def table():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tracker", TRACKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: 🔴 逐格照抄快照(元 / **百万** tokens)。表里存的是元/千,所以比的时候 ×1000。
#:    这里写百万单位,是为了**和快照文档逐字一致** —— 判据里的数字要能和
#:    签入的快照一眼对上,中间隔一层换算就没人会去核了。
SNAPSHOT_CNY_PER_M = {
    ("dashscope", "qwen3.7-max"): {"input": 12.0, "output": 36.0, "cache_hit": 2.4},
    ("dashscope", "qwen3.8-max"): {"input": 12.0, "output": 36.0, "cache_hit": 1.5},
    # 限时 8 折仍在页面上标着 ⇒ 表里存打折后价,与页面口径一致
    ("dashscope", "qwen3.7-plus"): {"input": 1.6, "output": 6.4},
    ("dashscope", "qwen-plus-latest"): {"input": 0.8, "output": 2.0},
    ("dashscope", "qwen3.6-plus"): {"input": 2.0, "output": 12.0},
    ("kimi", "kimi-k2.6"): {"input": 6.5, "output": 27.0, "cache_hit": 1.1},
}

#: 🔴 **已知未定价**:厂商公开页查不到可用的单值,所以**故意不登记**,
#:    让 `DEFAULT_PRICING` 继续兜底并告警(工单 §1.2)。
#:    每条都要有理由 —— 「其它」是把没读懂的那条藏起来的地方。
UNPRICED_KNOWN = {
    ("doubao", "doubao-seed-2-0-lite-260215"):
        "火山页只给区间 0.6–1.8 / 3.6–10.8 元每百万(按上下文长度阶梯),"
        "**没给档位边界**。填单值就是猜,而猜出来的数会让成本看起来精确。",
    ("glm", "glm-5.2"):
        "只在火山方舟页上看到**转售**价 8/28;我们 llm_call_log 里 platform 是 "
        "`glm`(直连)不是 `doubao` —— 不跨厂商套用别人的转售价。",
    ("metaso", "metaso+qwen3.7-max"):
        "秘塔没有找到公开 API 价目页。表里既有三行按 ¥0.05/次 flat 记,来源不明;"
        "这个组合 30 天 11 次记 ¥0,是真免费还是按次没建模,未定。",
}


# ════════════════════════════════════════════════════════════════
# 逐格比快照
# ════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("key", sorted(SNAPSHOT_CNY_PER_M))
def test_each_priced_row_matches_the_signed_snapshot(table, key):
    """表值与签入快照**逐格**一致(元/千 × 1000 = 元/百万)。

    🔴 比的是**相等**不是「误差 <5%」:表里存的就是照抄快照的数,
       不是换算出来的。留一个 5% 的口子,等于给「改一点点没人发现」留门。
       (工单说 <5% 是给**换算过**的行留的余地 —— 本单把那类行都改成了直接照抄,
        所以这里可以要求严格相等,这是比工单更紧的一侧。)
    """
    want = SNAPSHOT_CNY_PER_M[key]
    got = table.PRICING_TABLE.get(key)
    assert got is not None, "%s 不在 PRICING_TABLE 里 —— 它会落兜底价" % (key,)
    for field, cny_per_m in want.items():
        assert field in got, "%s 缺 %s 档(缓存命中缺档 = 按未命中价计,成本高估)" % (key, field)
        assert round(got[field] * 1000, 6) == round(cny_per_m, 6), (
            "%s 的 %s:表里 ¥%.4f/百万,快照 ¥%.4f/百万 —— 对不上就是没重新对账"
            % (key, field, got[field] * 1000, cny_per_m))


def test_no_priced_row_is_computed_from_a_hardcoded_fx_rate(table):
    """🔴 对过账的行不许再用「美元价 × 写死汇率」。

    `kimi-k2.6` 原来就是这么存的:`round(0.95 * USD_TO_CNY / 1000, 6)`,
    而厂商按**人民币**标价。两层误差叠加 —— 美元价可能过期、汇率 7.2 是常量。
    这条钉的是**取源**:厂商用什么币种标价,表里就存什么,不做换算。
    """
    src = TRACKER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Dict)):
            continue
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Tuple) and len(k.elts) == 2):
                continue
            try:
                key = tuple(e.value for e in k.elts)
            except AttributeError:
                continue
            if key not in SNAPSHOT_CNY_PER_M:
                continue
            seg = ast.get_source_segment(src, v) or ""
            if "USD_TO_CNY" in seg:
                bad.append(key)
    assert not bad, "这些已对账的行还在用汇率换算:%s" % bad


# ════════════════════════════════════════════════════════════════
# 未定价集合:冻结 + 必须继续兜底
# ════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("key", sorted(UNPRICED_KNOWN))
def test_a_known_unpriced_combo_stays_unregistered(table, key):
    """查不到厂商价的组合**故意不登记**,让兜底继续告警(工单 §1.2)。

    🔴 反直觉但对:登记一个猜出来的数会让它**不再告警**,
       于是「我们不知道这条链多少钱」这件事从此没人看得见。
       落兜底至少每进程每组合会喊一次。
    """
    assert key not in table.PRICING_TABLE, (
        "%s 被登记了。厂商页查不到可用单值时,登记 = 用一个编的数把告警关掉。"
        "真查到了 ⇒ 连同快照一起更新,并把它从 UNPRICED_KNOWN 里删掉" % (key,))


def test_every_unpriced_entry_carries_a_reason():
    """未定价表的每一条都要有理由,且不许写「其它」。

    没有理由的「未定价」等于没有分类:下一个人无法判断它还成不成立,
    只能选择相信 —— 而那正是本单开头那张停在 2026-05 的表的由来。
    """
    for key, reason in UNPRICED_KNOWN.items():
        assert reason and len(reason.strip()) >= 20, "理由太短:%s" % (key,)
        assert "其它" not in reason and "其他" not in reason, key


# ════════════════════════════════════════════════════════════════
# 边界:不碰对客价 / 不动既有 DeepSeek 锁
# ════════════════════════════════════════════════════════════════
def test_the_official_deepseek_rows_are_untouched(table):
    """官方线那几行由 WO_206 的锁管,本单一个字不动(工单 §1.3 末条)。

    值取自 DeepSeek 官方页高峰档(2026-09-15 复取一次,与 c1p 快照一致):
    flash 2/8、cache 0.04;v4-pro 9/27、cache 0.30(元/百万)。
    """
    for key, want in {
        ("deepseek", "deepseek-flash"): (0.002, 0.008, 0.00004),
        ("deepseek", "deepseek-v4-pro"): (0.009, 0.027, 0.0003),
    }.items():
        row = table.PRICING_TABLE.get(key)
        assert row is not None, "%s 不见了 —— 本单不该动官方线" % (key,)
        assert (row["input"], row["output"], row.get("cache_hit")) == want, (
            "%s 被本单动过了:%s" % (key, row))


def test_this_package_does_not_reach_into_customer_facing_pricing():
    """结构臂:本包只读**内部成本表**,不碰对客价的任何模块。

    工单 §1.4:对客价那几张表一个字不动。这条钉的是**判据自己的边界** ——
    一个会去读对客价的判据,迟早会有人"顺手"在里面改点什么。

    🔴 判法用 `tokenize` 把**注释与字符串整类去掉**再看剩下的代码。
       第一版按行首是不是 `#` / `"` 猜「这行是不是注释」,
       结果把多行 docstring 的**续行**判成了实代码,自己红了自己 ——
       按行首字符猜 token 类型是行不通的,Python 有多行字符串。
    """
    import tokenize

    forbidden = ("feature_pricing", "transparent_pricing", "price_quotes")
    code_only = []
    with io.open(__file__, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_only.append(tok.string)
    blob = " ".join(code_only)
    hit = [f for f in forbidden if f in blob]
    assert not hit, "判据的**实代码**里出现了对客价模块:%s" % hit


def test_the_snapshots_are_checked_in_and_carry_their_fetch_date():
    """快照必须**在树里**,且写明取页日期与币种 —— 它是这份对账的唯一依据。

    🔴 「我查过了」不是证据,签入的快照才是。
       下一个人要能在不重新上网的情况下判断这张表对不对。
    """
    import subprocess

    for snap in (SNAP_DASHSCOPE, SNAP_KIMI_DOUBAO):
        assert snap.exists(), "快照不在:%s" % snap
        rel = snap.relative_to(ROOT).as_posix()
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                                 cwd=str(ROOT), capture_output=True)
        assert tracked.returncode == 0, (
            "快照在磁盘上但**没进树**(docs/AI-CONTEXT/* 被 .gitignore 排除,"
            "要 `git add -f`)—— 那就不是签入快照:%s" % rel)
        text = snap.read_text(encoding="utf-8")
        assert "取页日期" in text and "2026-09-15" in text, "%s 没写取页日期" % rel
        assert "人民币" in text or "元" in text, "%s 没写币种" % rel
        assert "http" in text, "%s 没留来源 URL" % rel


# ════════════════════════════════════════════════════════════════
# c1' · 计价**模式**这一维:表里存哪一档,调用点就得走哪一档
# ════════════════════════════════════════════════════════════════
#: `qwen-plus-latest` 的输出价按思考模式分两档(华北2 北京,0<Token≤128K):
#:   非思考 2 元/百万 · 思考 **8** 元/百万 —— 差 4 倍。
#: 表里存的是非思考档,**前提是调用点没开思考**。
QWEN_PLUS_CALL_SITES = (
    "services/research_monitor/platforms.py",
    "services/ai_surface_monitoring/lineage.py",
)
QWEN_PLUS_THINKING_OUTPUT_CNY_PER_M = 8.0


def test_the_qwen_plus_call_site_still_uses_the_priced_mode():
    """🔴 表里存非思考价 ⇒ 调用点**不许**开思考。

    厂商深度思考文档原文:「千问Plus系列(混合思考模式,**默认不开启思考模式**):
    qwen-plus、qwen-plus-latest」。调用点不传 `enable_thinking` = 走非思考 = 输出 2。

    哪天有人给那两处加上 `enable_thinking=True`(或 `extra_body` 里塞 True),
    输出价从 2 跳到 8(**4 倍**),而表里的数不会自己跟着动 ——
    成本估算会**低估 4 倍**,且没有任何东西会报错。

    🔴 这条是 Review 复核时要回来的。上一笔我把输出从 4.8 改成 2 是**对的**,
       但当时只读到「输出 2」,既没验"有没有思考档"、也没验"调用点走哪一档" ——
       数字碰对了、依据是空的。对的结论不会引起任何人提问
       (本仓 `a-right-conclusion-with-a-wrong-reason-is-invisible`),
       所以缺的那一步得靠判据自己站住。
    """
    for rel in QWEN_PLUS_CALL_SITES:
        path = ROOT / rel
        assert path.exists(), "锚过期:%s 不在了 —— 先看代码再改判据" % rel
        src = path.read_text(encoding="utf-8")
        # 只要出现「开启思考」的写法就红;显式 False 是安全的
        for bad in ('"enable_thinking": True', "'enable_thinking': True",
                    "enable_thinking=True"):
            assert bad not in src, (
                "%s 打开了思考模式,而 PRICING_TABLE 里 qwen-plus-latest 存的是"
                "**非思考**价(2 元/百万)。思考档是 %.0f 元/百万 —— 低估 4 倍。"
                "要么关掉,要么把表改成按模式取价并更新快照。"
                % (rel, QWEN_PLUS_THINKING_OUTPUT_CNY_PER_M))


def test_the_snapshot_records_which_mode_and_region_it_copied():
    """快照必须写清它抄的是**哪个地域、哪一档、哪个模式**。

    🔴 这条有实据:同一行我用不同提问读了四次,拿到三个不同答案 ——
       一次漏了思考档、一次读到了**国际地域**那一组。
       多维价目表用摘要模型读时,不点名维度就分不出它挑的是哪一行。
       快照不写维度,下一个人就没法复核,只能选择相信。
    """
    text = SNAP_DASHSCOPE.read_text(encoding="utf-8")
    # 🔴 钉**那一行**,不是"文档里有没有出现这几个词"。
    #    第一版就是后者,于是把表格行里的维度删掉、而文末说明段里还留着同样的词,
    #    判据照样绿 —— 本仓 `a-string-anchor-satisfied-by-an-unrelated-line`。
    # 🔴 快照里以 `| `qwen-plus-latest` |` 开头的行有**两张表各一行**
    #    (价目表 + 与 tracker 的逐格比表),所以不能要求"恰好一行"——
    #    我先写成"行里出现过模型名"(命中 3 行,连文末说明表都算上),
    #    再收紧成"恰好一行"(命中 2 行,仍不对)。
    #    **收紧判据时也要先数一遍**,否则只是把一个太松的锚换成另一个。
    #    这里要的是:**存在一行**把模式分档写清楚了。
    marker = "| `qwen-plus-latest` |"
    rows = [l for l in text.splitlines() if l.strip().startswith(marker)]
    assert rows, "快照里找不到 qwen-plus-latest 的表格行"
    with_mode = [l for l in rows if "非思考" in l and "8" in l]
    assert with_mode, (
        "快照的价目表行没写清模式分档(非思考 2 / 思考 8)——"
        "少了它,下一个人不知道表里存的是哪一档。实得:%s" % rows)

    # 地域与默认值的依据仍要在文档里(它们是全表共用的前提,不在单行上)
    for token in ("华北2", "enable_thinking"):
        assert token in text, "快照没写清前提,缺:%s" % token


def test_the_table_stores_the_non_thinking_tier_for_qwen_plus(table):
    """正面钉:表里那一格就是**非思考**档的 2,不是思考档的 8。

    少了它,上一条可以靠「调用点没开思考」单独满足 ——
    而表里存成 8 同样与调用点不符(反方向:高估 4 倍)。
    两侧都钉,「模式一致」才算真被钉住。
    """
    row = table.PRICING_TABLE[("dashscope", "qwen-plus-latest")]
    assert round(row["output"] * 1000, 6) == 2.0, (
        "表里存的不是非思考档:¥%.4f/百万" % (row["output"] * 1000))


def test_new_pricing_snapshots_do_not_need_a_forced_add():
    """🔴 快照要能**正常** `git add` 进树,不许靠 `git add -f`。

    `.gitignore` 里 `/docs/AI-CONTEXT/*` 把整个目录排掉了,所以此前每份快照
    都是硬塞进去的(c1p 的 DEEPSEEK 快照同病)。
    靠人记得加 `-f` 的规矩迟早会漏 —— 漏掉的那天判据会红在「快照没进树」,
    而那种红读起来像**快照写错了**,排查方向从一开始就偏。
    现在给这一类开了豁免;这条判据钉住豁免还在。
    """
    import subprocess

    probe = "docs/AI-CONTEXT/ZZ_PROBE_PRICING_SNAPSHOT_9999-12-31.md"
    r = subprocess.run(["git", "check-ignore", probe],
                       cwd=str(ROOT), capture_output=True)
    assert r.returncode != 0, (
        "新的价目快照仍会被 .gitignore 吃掉(命中 %s)—— "
        "豁免 `!/docs/AI-CONTEXT/*_PRICING_SNAPSHOT_*.md` 没了或被后面的规则盖掉"
        % r.stdout.decode("utf-8", "replace").strip())
