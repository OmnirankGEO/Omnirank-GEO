"""接线锁 —— 闸有没有被真正接上,以及有没有人偷偷开后门。

工单 §四.3:「新列有真读点,新开关有**真前端入口**(我们刚吃过『服务层✅端点✅前端零入口』)」。
本仓另一条同族教训:「lock_the_wiring_not_the_function」—— 只锁函数本身,
函数被绕过 / 没人调用时,锁照样全绿。

本文件不连数据库,纯静态,快;真行为在 test_monitoring_optin_default_off.py 里打。

W1  唯一漏斗:所有"取词去跑"的执行路径都经 get_keywords_for_monitoring(没有第二条 SQL 直取 extra 去跑)
W2  后门白名单:全仓 `for_dispatch=False` 的出现点**正好**是那两个报表/导出调用点
W3  硬编码 FALSE 已经消失(P0-A.3)
W4  迁移已登记进 manifest(prestart 只按 manifest 跑,不 glob 目录)
W5  迁移里**没有** DML(prestart 每次部署无条件重放 → 迁移里的 UPDATE 会把人工关掉的词重新打开)
W6  前端:逐行开关不再限制 source==='confirmed';批量计数也不再限制
W7  前端:extra 的开关请求带 ?source=extra(不带的话后端会走 confirmed 分支去查订阅 → 404)
W8  加词端点返回体明示"未开启监测"
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MDB = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
MON_API = (ROOT / "api" / "monitoring_api.py").read_text(encoding="utf-8")
MIGRATION = (ROOT / "scripts" / "migration_monitoring_extra_optin_2026_08_15.sql").read_text(encoding="utf-8")
MANIFEST = (ROOT / "db" / "migration_manifest.py").read_text(encoding="utf-8")
KW_TABLE = (ROOT / "frontend" / "src" / "pages" / "Monitoring" / "components" / "KeywordTable.tsx").read_text(encoding="utf-8")

# 允许 for_dispatch=False 的调用点:只有"渲染历史监测结果"的读路径。
# 🔴 加新的白名单条目 = 开一个新后门,必须连同理由一起 review。
ALLOWED_OPTOUT_FILES = {"api/monitoring_api.py"}
EXPECTED_OPTOUT_COUNT = 2  # 周/月报 generate_report + CSV 导出


def _py_sources():
    """全仓生产 Python(排除 tests/ 与 docs/ —— 交付文档描述这个扫描本身也会命中)。"""
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "docs/", "node_modules/", "frontend/")):
            continue
        if "/_archive/" in rel or "/.venv/" in rel:
            continue
        yield rel, p.read_text(encoding="utf-8", errors="ignore")


def _strip_comments_and_docstrings(src: str) -> str:
    """只留真代码。

    🔴 为什么必须有这一步(第一版就踩了):我在 get_keywords_for_monitoring 的 docstring 里
    写了「for_dispatch=False 放行」来解释这个设计,W2 当场把**我自己的说明文字**判成后门。
    同族教训:自愈 DDL 扫描必须排除 tests/ 与 docs/,否则交付文档描述这个扫描本身也会命中。
    判据要打在代码上,不能打在讲代码的话上。
    """
    import io
    import tokenize

    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src  # 解析不了就退回原文(宁可多报,不可漏报)
    return " ".join(out)


# ── W1:结构锚 —— 全仓每一处 `FROM extra_keywords` 都必须被显式归类 ──────────────
#
# 🔴 换尺子的原因(2026-08-15 复审实证):第一版用**语义签名**
#    (窗口内同时有 entitlement_platforms + monitoring_query = "拿去跑")。
#    复审改用**结构锚**(逐个映射到所属函数再定性),当场抓到第 5 个口:
#    `get_active_client_keywords()` 自己 `SELECT … FROM extra_keywords WHERE status='active'`、
#    **无闸**,而它两个签名字段一个都不 SELECT ⇒ 语义签名**看不见它**。
#    「全仓只有 monitoring_db.py」那句结论,是在一个有洞的签名下得的,洞里正好躺着一个无闸取词函数。
#
#    教训:**签名扫描只能证明"我找的那种形状不存在",不能证明"这类东西不存在"。**
#    改法:不再"找可疑形状",而是**枚举全分母 + 逐个归类**,未归类即红 —— 新增出现点无处可藏。
#
# 归类表:每一处 `FROM extra_keywords` → (文件, 所属函数) → 定性。
# 🔴 这张表是**实测建的**,不是猜的:第一版我按印象写函数名,扫出来 16 处对不上 ——
#    正好证明这把尺子在干活。每一条都读过那段 SQL 才定的性。
#   dispatch    唯一取词漏斗(内部再分:跑全部=加闸 / 显式点名=按钮级确认豁免)
#   read_only   展示 / 报表 / 治理 / 权属解析 / 归档列表 / 文本借用 / 存在性检查 —— 不驱动引擎
#   maintenance 一次性脚本(回填 / dry-run / 复审验证)—— 不在请求链上
EXTRA_KEYWORDS_SITES = {
    # ── 唯一漏斗 ──
    ("db/monitoring_db.py", "get_keywords_for_monitoring"): "dispatch",
    # [parity 2026-08-16 P0-2] 每日取词的 extra 臂 —— 它**走 JOIN**,旧正则(只认 FROM)看不见它。
    #   定性 dispatch:这批词直接进 scheduler 当日跑,必须被 opt-in 闸住(ek.is_monitored = TRUE)。
    ("db/monitoring_db.py", "list_active_subscriptions"): "dispatch",
    # ── 应用侧只读 ──
    ("db/monitoring_db.py", "quote_unfulfilled_compliance_condition_sql"): "read_only",  # 服务锚(本单不动·Owner 裁决)
    ("db/monitoring_db.py", "row_value"): "read_only",                    # 平台迁移备份/还原
    ("db/monitoring_db.py", "get_keywords"): "read_only",                 # 词条列表(增删改查)
    ("db/monitoring_db.py", "get_keyword_by_id"): "read_only",
    ("db/monitoring_db.py", "delete_keyword"): "read_only",
    ("db/monitoring_db.py", "create_monitoring_run_cells"): "read_only",  # 权属校验(词由漏斗给定)
    ("db/monitoring_db.py", "get_client_keywords"): "read_only",          # 界面展示(本包改了它的 is_monitored 取值)
    ("db/monitoring_db.py", "reverify_keyword_results"): "read_only",     # docstring 明写 provider-free · 不花钱
    ("api/monitoring_api.py", "api_delete_keyword"): "read_only",
    # [parity 2026-08-16 P0-6] 监测异常统计的 UNION 子查询 —— 只算 rate_change,不驱动引擎。
    ("api/m3_api.py", "get_monitoring_anomaly"): "read_only",
    ("db/publish_db.py", "_get_brand_keywords"): "read_only",             # 发布选词的文本池
    ("services/publication_facts.py", "record_manual_publication"): "read_only",   # 存在性检查(发布后自动补词)
    ("services/report_writer_v2.py", "_build_action_attribution_md"): "read_only", # 报告计数
    ("tools/geo_managed/campaign_tick.py", "_check_rank_and_detection"): "read_only",  # 只借 monitoring_query 文本
    ("server.py", "_resolve_monitoring_keyword_brand"): "read_only",      # 权属解析
    ("server.py", "_do"): "read_only",                                    # 归档词列表
    # ── 一次性脚本 ──
    # 🪦 backfill_monitoring_extra_optin_2026_08_15.py 的两条已随脚本删除
    #    (WO_MONITORING_PLATFORM_COVERED v1.2 P0-0:守卫校验目标集合而非目标状态,
    #     人工再跑 --apply 会推翻 Owner 已作出的关列裁定)。
    #    脚本"复活即红"由 tests/platform_covered_2026_08_16/test_p0_0_dead_script_removed.py 守。
    ("scripts/dry_run_monitoring_query_backfill.py", "main"): "maintenance",
    ("scripts/verify_monitoring_optin_2026_08_15.py", "q1_default_off_is_real"): "maintenance",
}


def _strip_prose(src: str) -> str:
    """剥掉**散文**(`#` 注释 + docstring),保留其余字符串 —— SQL 就住在那些字符串里。
    保持行列偏移不变(空格填充),这样结构锚定位不受影响。

    🔴 为什么必须有这一层(2026-08-15 · 同型**第五次**):
      ① `W2` 命中我 docstring 里的 `for_dispatch=False` → 判成后门
      ② SQL 注释里写了带花括号的前端片段 → f-string SyntaxError,12 条锁当场红
      ③ 注释里抄了 2026-08-10 那条红线字面量 → 撞红**别人的**锁
      ④ 被删函数的墓碑注释里抄了本扫描的关键词 → 扫描把自己的说明当成 2 处取词点
      ⑤ 本函数上游 `q2_how_many_outlets` 的 docstring 里同样抄了它 → 又被当成 2 处
    前四次的收场都是"我下次注意措辞"。**靠人注意措辞 = 没修**。
    这一层把它变成扫描器自己的性质:**判据只看代码,不看讲代码的话**,
    于是任何人怎么写注释都不会再把判据带沟里。

    区分方式:docstring = Module/ClassDef/FunctionDef 的**第一条语句且是纯字符串**;
    传给 cursor.execute 的那些三引号 SQL 是**调用实参**,不是 docstring,原样保留。
    (顺带第六次:上一版这里原样写了三引号,内层引号把本 docstring 提前终结 → SyntaxError。
     所以这段说明里一个三引号都不写 —— 同一条教训,又换了个马甲。)
    """
    import ast
    import io
    import tokenize

    lines = src.splitlines(keepends=True)

    def blank(r0, c0, r1, c1):
        if r0 == r1:
            if 0 <= r0 < len(lines):
                ln = lines[r0]
                lines[r0] = ln[:c0] + " " * max(0, c1 - c0) + ln[c1:]
            return
        for r in range(r0, min(r1 + 1, len(lines))):
            ln = lines[r]
            if r == r0:
                lines[r] = ln[:c0] + " " * max(0, len(ln.rstrip("\n")) - c0) + ("\n" if ln.endswith("\n") else "")
            elif r == r1:
                lines[r] = " " * min(c1, len(ln)) + ln[c1:]
            else:
                lines[r] = (" " * len(ln.rstrip("\n"))) + ("\n" if ln.endswith("\n") else "")

    spans = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                spans.append((tok.start[0] - 1, tok.start[1], tok.end[0] - 1, tok.end[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src  # 解析不了就退回原文(宁可多报,不可漏报)

    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None) or []
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str) and first.end_lineno is not None:
                spans.append((first.lineno - 1, first.col_offset,
                              first.end_lineno - 1, first.end_col_offset))
    except (SyntaxError, ValueError):
        pass

    for s in sorted(spans, key=lambda x: (x[0], x[1]), reverse=True):
        blank(*s)
    return "".join(lines)


def _enclosing_def(src: str, pos: int) -> str:
    """结构锚:这一处 SQL 长在哪个 def 里(取它上方最近的 def)。"""
    best = ""
    for m in re.finditer(r"^\s*(?:async\s+)?def\s+(\w+)", src[:pos], re.M):
        best = m.group(1)
    return best


def _scan_extra_sites(sources):
    """→ {(file, func): [(是否加闸, 是否显式点名), ...]} · 全分母,不做任何"可疑形状"筛选。"""
    out = {}
    for rel, raw in sources:
        src = _strip_prose(raw)
        # 🔴 2026-08-16(parity 合并):正则原来只认 `FROM`。
        #   实测洞:parity 的 P0-2 给 list_active_subscriptions 加的 extra 取词臂走的是
        #   `JOIN extra_keywords ek ON ek.id = s.keyword_id`(主表是订阅表)——
        #   一处**真的会把词送进引擎**的新出口,旧正则完全看不见。
        #   这正是「每次下这种结论,先问一句:这个签名扫不到什么」的第二次兑现。
        for m in re.finditer(r"(?:FROM|JOIN)\s+(?:public\.)?extra_keywords", src, re.I):
            fn = _enclosing_def(src, m.start())
            fwd = src[m.start(): m.start() + 900]
            # 闸的两种写法:唯一漏斗用常量拼接;UNION 臂直接写死条件。
            gated = ("extra_dispatch_gate_sql" in fwd) or ("ek.is_monitored = TRUE" in fwd)
            out.setdefault((rel, fn), []).append((gated, "e.id = %s" in fwd))
    return out


def test_W1_every_extra_keywords_site_is_classified():
    """全仓每一处 `FROM extra_keywords` 都必须在归类表里;多一处未归类当场红。"""
    sites = _scan_extra_sites(_py_sources())
    assert sites, "扫到 0 处 = 判据坏了(恒真)"
    unknown = sorted(k for k in sites if k not in EXTRA_KEYWORDS_SITES)
    assert not unknown, (
        f"出现未归类的 extra 取词点:{unknown}\n"
        "  新增一处 SQL 就必须在 EXTRA_KEYWORDS_SITES 里写明它是 dispatch 还是 read_only ——\n"
        "  2026-08-15 复审就是用这把尺子抓到 get_active_client_keywords 这个无闸出口的。"
    )
    stale = sorted(k for k in EXTRA_KEYWORDS_SITES if k not in sites)
    assert not stale, f"归类表里有已消失的条目(该清理):{stale}"


def test_W1b_dispatch_sites_are_gated_or_explicitly_exempt():
    """唯一 dispatch 函数里:『跑全部』分支必须加闸,『显式点名』分支必须**不**加闸。"""
    sites = _scan_extra_sites(_py_sources())
    branches = sites[("db/monitoring_db.py", "get_keywords_for_monitoring")]
    assert len(branches) == 4, f"漏斗内 extra 分支数变了(期望 4,实测 {len(branches)})"
    bulk = [b for b in branches if not b[1]]
    named = [b for b in branches if b[1]]
    assert len(bulk) == 2 and all(g for g, _ in bulk), f"有『跑全部』分支没加闸:{branches}"
    # [parity 2026-08-16] 漏斗**之外**被定性为 dispatch 的站点,每一处都必须加闸。
    #   原来这条只查 get_keywords_for_monitoring 一个函数 —— 那等于假设"漏斗是唯一的",
    #   而 P0-2 已经合法地加了第二个 dispatch(每日取词的 extra 臂,走 JOIN)。
    others = [k for k, v in EXTRA_KEYWORDS_SITES.items()
              if v == "dispatch" and k != ("db/monitoring_db.py", "get_keywords_for_monitoring")]
    assert others, "漏斗之外的 dispatch 站点为 0 = 这半条判据恒真"
    for key in others:
        for gated, is_named in sites[key]:
            assert gated or is_named, (
                f"{key[0]}::{key[1]} 被定性为 dispatch 但没加 opt-in 闸 —— "
                "未开开关的手动词会被拉去跑并计费"
            )
    assert len(named) == 2 and all(not g for g, _ in named), \
        f"显式点名分支不该加闸(Owner 裁决):{branches}"


def test_W1c_structural_scan_catches_an_ungated_fetcher():
    """🔴 成对反向对照:喂一段**无闸取词函数**给同一把尺子,必须被抓出来。

    这段伪代码逐字照抄被删掉的 `get_active_client_keywords` 的形状 ——
    它两个语义签名字段(entitlement_platforms / monitoring_query)一个都不 SELECT,
    正是**旧签名扫不到、结构锚能抓到**的那一类。这条锁证明换尺子不是换个说法。
    """
    fake = (
        "def some_new_helper():\n"
        "    cursor.execute('''\n"
        "        SELECT e.id, e.quote_id, e.keyword\n"
        "        FROM extra_keywords e JOIN quotes q ON e.quote_id = q.id\n"
        "        WHERE e.status = 'active'\n"
        "    ''')\n"
    )
    sites = _scan_extra_sites([("fake_module.py", fake)])
    assert ("fake_module.py", "some_new_helper") in sites, "结构锚没抓到无闸取词函数"
    assert ("fake_module.py", "some_new_helper") not in EXTRA_KEYWORDS_SITES, \
        "它不在归类表里 ⇒ W1 会红(这正是我们要的)"
    # 反向的反向:旧的语义签名对同一段代码**看不见** —— 记录这个洞,别再退回去
    window = fake
    assert not ("entitlement_platforms" in window and "monitoring_query" in window), \
        "旧语义签名本就抓不到这一类,所以才换成结构锚"


def test_W1d_dead_ungated_fetcher_stays_deleted():
    """`get_active_client_keywords` 必须保持删除态(它是无闸的第 5 个出口)。"""
    assert "def get_active_client_keywords" not in MDB, \
        "无闸取词函数被复活了 —— 要恢复该能力请走 get_keywords_for_monitoring"
    # 反向对照:确认这个文件里**别的**函数定义还在(证明断言不是因为读到空文件而恒真)
    assert "def get_keywords_for_monitoring" in MDB


def test_W2_for_dispatch_optout_whitelist():
    """全仓 for_dispatch=False 的**调用点**必须正好是白名单那两处(不数注释和 docstring)。"""
    found = {}
    for rel, src in _py_sources():
        n = len(re.findall(r"for_dispatch\s*=\s*False", _strip_comments_and_docstrings(src)))
        if n:
            found[rel] = n
    assert set(found) == ALLOWED_OPTOUT_FILES, f"闸的后门出现在预期之外的文件:{found}"
    assert sum(found.values()) == EXPECTED_OPTOUT_COUNT, \
        f"退出闸的调用点数变了(期望 {EXPECTED_OPTOUT_COUNT},实际 {found}) —— 新增的那个必须单独 review"


def test_W2c_optout_scan_has_discriminating_power():
    """证明 W2 的扫描不是恒绿:同一把尺子量一段**含后门**的伪代码,必须量得出来。"""
    fake = "def f():\n    '''for_dispatch=False 只是说明文字'''\n    # for_dispatch=False 注释\n    return g(for_dispatch=False)\n"
    stripped = _strip_comments_and_docstrings(fake)
    assert len(re.findall(r"for_dispatch\s*=\s*False", stripped)) == 1, \
        "剥离后应只剩真调用那一处(docstring 与注释各一处必须被剥掉)"


def test_W2b_gate_sql_is_applied_to_both_bulk_branches():
    """闸必须真的拼进两条『跑全部』的 SQL —— 只定义一个常量不用,是最典型的假修。"""
    assert MDB.count("extra_dispatch_gate_sql") == 3, \
        "期望 1 处定义 + 2 处使用(brand 级 / quote 级两条分支都要)"
    # 两条分支都必须是 f-string(不是 f-string 的话 {extra_dispatch_gate_sql} 会被当字面量发给 PG)
    for marker in ("WHERE e.brand_id = %s AND e.status = 'active'",
                   "WHERE e.quote_id IN ({placeholders}) AND e.status = 'active'"):
        idx = MDB.find(marker)
        assert idx > 0, f"锚点消失,判据失效:{marker}"
        assert "extra_dispatch_gate_sql" in MDB[idx: idx + 700], f"这条分支没接上闸:{marker}"


def test_W3_hardcoded_false_is_gone():
    assert "FALSE as is_monitored" not in MDB, \
        "get_client_keywords 的 extra 分支还写死 FALSE → 前端永远画不出开关"
    # 🔴 必须是**全表名限定**那种写法:裸写会与 2026-08-10 那两条守 confirmed 分支的锁逐字相撞
    #    (A/B 实测抓到:5 条既有锁转红)。这条断言把"让路"这件事钉住,防后人图省事改回裸写。
    assert "COALESCE(is_monitored, FALSE) as is_monitored" not in MDB,         "裸写会撞 2026-08-10 的 confirmed 侧红线锁(它守的是别的东西,该让路的是本包)"
    # [2026-08-16 parity P0-3/P0-8] 原断言是单行 `COALESCE(extra_keywords.is_monitored, FALSE) as is_monitored`。
    # parity 之后手动词也有逐词订阅,展示口径升级成**复合式**:列开着 AND 有在跑的订阅。
    # 🔴 锁跟着改但**只能更严不能更松**:除了原来的全限定 COALESCE,还必须钉住 EXISTS 那一半,
    #    否则后人把 EXISTS 删掉退回"只看列"时(=「界面说开着、cron 不跑」那个形态)这条锁照样绿。
    idx = MDB.find("COALESCE(extra_keywords.is_monitored, FALSE)")
    assert idx > 0, "全限定 COALESCE 消失 —— get_client_keywords 的 extra 分支展示口径被改回去了"
    window = MDB[idx: idx + 600]
    assert "EXISTS (" in window and "keyword_monitor_subscriptions" in window, \
        "展示口径退回『只看列不看订阅』—— 会重新造出「界面说开着、cron 不跑」"
    assert "kms.keyword_source = 'extra'" in window, \
        "EXISTS 没按 keyword_source 限定 → 会被 confirmed 的同号订阅冒名顶替(两表 id 各自独立)"


def test_W4_migration_registered():
    assert "scripts/migration_monitoring_extra_optin_2026_08_15.sql" in MANIFEST, \
        "prestart 只按 manifest 跑、不 glob 目录 → 没登记 = 上线后永远不会跑"


def test_W5_migration_has_no_dml():
    """迁移里禁裸 DML(prestart 每次部署无条件重放,会把人工关掉的词重新打开)。"""
    body = "\n".join(l for l in MIGRATION.splitlines() if not l.strip().startswith("--"))
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "TRUNCATE "):
        assert verb not in body.upper(), f"迁移里出现 DML({verb.strip()}) —— 每次部署重放会覆盖人工操作"
    assert "ADD COLUMN IF NOT EXISTS is_monitored" in body
    assert "DEFAULT FALSE" in body.upper()


def test_W6_frontend_switch_not_confirmed_only():
    """逐行开关 + 批量计数都不再把手动词排除在外。"""
    assert "{kw.source === 'confirmed' && onToggleMonitor && (" not in KW_TABLE, \
        "逐行开关仍限制 source==='confirmed' → 手动词依旧没有开关"
    assert "kw.source === 'confirmed' && !kw.is_monitored" not in KW_TABLE, \
        "批量计数仍只数合同词 → 手动词永远不被批量开关碰到"
    # 反向对照:确认这个文件里 onToggleMonitor 确实还被渲染(而不是整块被删掉了)
    assert "onCheckedChange={(checked) => void onToggleMonitor(kw, checked)}" in KW_TABLE


def test_W7_frontend_passes_source_for_extra():
    # 🔴 [platform-covered 2026-08-16] 原判据找字面量 `?source=extra`。
    #   本包 §3 要在同一个 URL 上再带 billing_mode,拼接改成了 URLSearchParams,
    #   字面量随之消失 —— 这条锁**红得对**(它看不出行为有没有变)。
    #   订正为打真实构造,而且**比原来更严**:既要求 extra 时真的 set 了 source,
    #   也要求这个 URL 是按 params 拼出来的(防有人退回手工拼字符串再漏掉一个参数)。
    assert "_params.set('source', 'extra')" in KW_TABLE,         "extra 的开关请求不带 source → 后端走 confirmed 分支查订阅 → 404"
    assert "new URLSearchParams()" in KW_TABLE and "isExtra" in KW_TABLE,         "开关 URL 不再按参数表拼 —— 手工拼字符串是漏参数的经典形态"
    # 行为侧由 parity 的 Playwright 锁2 兜底(真浏览器断言发出的 URL 里含 source=extra)
    assert "source: batch.source" in KW_TABLE or "source: 'extra'" in KW_TABLE or \
           "keyword_ids: batch.ids, source: batch.source" in KW_TABLE, "批量请求必须带 source"


def test_W8_add_keyword_response_says_not_monitored():
    assert '"is_monitored": False' in MON_API
    assert "未开启监测" in MON_API, "加词端点必须明示『已添加 · 未开启监测』(工单 P0-A.5)"


# ==========================================================================
# W1e —— 站点**计数**断言(从 parity 分支捞回来的那一条)
#
# 结构锚版 W1 只断「(文件,函数) 组合」在不在归类表里。那有个洞:
#   已归类为 read_only 的函数里**再加第 2 处**真取词 SQL,组合没变 ⇒ 不会红。
# 这里把每个组合的**处数**也钉住,新增一处就必须回来重新定性。
# 🔴 分母口径 = 剥散文之后(_strip_prose)。不剥是 36 处,差的 3 处正是散文里的字面量。
# ==========================================================================

EXTRA_KEYWORDS_SITE_COUNTS = {
    ("db/monitoring_db.py", "get_keywords_for_monitoring"): 4,
    ("db/monitoring_db.py", "list_active_subscriptions"): 1,
    ("db/monitoring_db.py", "quote_unfulfilled_compliance_condition_sql"): 1,
    ("db/monitoring_db.py", "row_value"): 1,
    ("db/monitoring_db.py", "get_keywords"): 4,
    ("db/monitoring_db.py", "get_keyword_by_id"): 1,
    ("db/monitoring_db.py", "delete_keyword"): 1,
    ("db/monitoring_db.py", "create_monitoring_run_cells"): 1,
    ("db/monitoring_db.py", "get_client_keywords"): 1,
    ("db/monitoring_db.py", "reverify_keyword_results"): 1,
    ("api/monitoring_api.py", "api_delete_keyword"): 1,
    ("api/m3_api.py", "get_monitoring_anomaly"): 3,
    ("db/publish_db.py", "_get_brand_keywords"): 1,
    ("services/publication_facts.py", "record_manual_publication"): 1,
    ("services/report_writer_v2.py", "_build_action_attribution_md"): 1,
    ("tools/geo_managed/campaign_tick.py", "_check_rank_and_detection"): 1,
    ("server.py", "_resolve_monitoring_keyword_brand"): 2,
    ("server.py", "_do"): 2,
    ("scripts/dry_run_monitoring_query_backfill.py", "main"): 3,
    ("scripts/verify_monitoring_optin_2026_08_15.py", "q1_default_off_is_real"): 3,
}


def test_W1e_site_counts_are_pinned():
    """每个 (文件, 函数) 组合里的取词点**处数**也必须对得上。"""
    sites = _scan_extra_sites(_py_sources())
    actual = {k: len(v) for k, v in sites.items()}
    assert actual, "扫到 0 处 = 判据坏了(恒真)"
    drift = {k: (EXTRA_KEYWORDS_SITE_COUNTS.get(k), n)
             for k, n in actual.items() if EXTRA_KEYWORDS_SITE_COUNTS.get(k) != n}
    drift.update({k: (n, actual.get(k))
                  for k, n in EXTRA_KEYWORDS_SITE_COUNTS.items() if actual.get(k) != n})
    assert not drift, (
        f"取词点处数变了(期望→实测):{drift}" + chr(10)
        + "  同一个函数里新增一处 SQL 也必须回来重新定性 —— 组合没变不代表口径没变。"
    )
    total = sum(actual.values())
    assert total == sum(EXTRA_KEYWORDS_SITE_COUNTS.values()), f"总处数漂移:{total}"


def test_W1e_count_scan_has_discriminating_power():
    """判据自证:给一个已归类函数再塞一处取词 SQL,计数必须变(证明不是恒真)。"""
    fake = (
        "def get_client_keywords():" + chr(10) +
        "    cursor.execute('SELECT 1 FROM extra_keywords')" + chr(10) +
        "    cursor.execute('SELECT 2 FROM extra_keywords')" + chr(10)
    )
    sites = _scan_extra_sites([("db/monitoring_db.py", fake)])
    assert len(sites[("db/monitoring_db.py", "get_client_keywords")]) == 2,         "同一函数里两处取词点没被数成 2 —— 计数尺子失效"


def test_W1f_join_form_is_visible_to_the_ruler():
    """🔴 成对反向:走 `JOIN extra_keywords` 的取词点必须被尺子看见。

    病史(2026-08-16 合并时实测):尺子原来只认 `FROM extra_keywords`。
    parity 的 P0-2 给 `list_active_subscriptions` 加的 extra 臂主表是订阅表,
    用 `JOIN extra_keywords ek ON ek.id = s.keyword_id` 接过去 ——
    一处**真会把词送进引擎**的新出口,旧尺子完全扫不到。
    Review 预判「合并后 W1 会红」没有兑现,原因就是这个洞,不是判据没在干活。
    """
    fake_join = (
        "def sneaky_join_fetcher():" + chr(10) +
        "    cursor.execute('SELECT ek.keyword FROM keyword_monitor_subscriptions s"
        " JOIN extra_keywords ek ON ek.id = s.keyword_id')" + chr(10)
    )
    sites = _scan_extra_sites([("fake_join.py", fake_join)])
    assert ("fake_join.py", "sneaky_join_fetcher") in sites, \
        "JOIN 形态的取词点没被尺子看见 —— 退回只认 FROM 就会漏掉整整一类出口"
    assert not sites[("fake_join.py", "sneaky_join_fetcher")][0][0], \
        "这段伪代码没有闸,应被判为未加闸(否则闸检测恒真)"

    # 反向的反向:同一段代码在**只认 FROM** 的旧尺子下是隐形的 —— 把这个洞钉在案上
    assert not re.findall(r"FROM\s+(?:public\.)?extra_keywords", fake_join, re.I), \
        "旧尺子本就看不到这一类,所以才把 JOIN 加进正则"
