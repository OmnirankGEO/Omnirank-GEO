# -*- coding: utf-8 -*-
"""WO_240 · 分母锁 + 副本一致锁。

两把锁,针对的是**这一类缺陷唯一还活着的那一面**:

1. **分母锁** —— server.py 里凡是「从取价结果里读一个数、取不到用字面量」的地方,
   必须全部走 `priced()`。工单点的是两处、我扫出来是六处
   (本仓 the-named-instance-is-not-the-defect-class);这把锁管**下一处**。
2. **副本一致锁** —— 那些字面量绝大多数是目录值的**副本**。
   副本本身不是缺陷,**副本悄悄漂了没人知道**才是。

🔴 **这把锁能证明什么、不能证明什么(先说清楚)**:
   它比的是**仓内**的权威写法(`PRICING_DATA` seed + 显式 UPSERT + migration_015),
   **不是生产库现值**。`PRICING_DATA` 是 `ON CONFLICT DO NOTHING`,
   对已存在的行**一个字都不改** —— 它自己就是一份可能陈旧的副本
   (文件里写着「最后同步时间:2026-04-11」)。
   所以:**这把锁只管仓内两份写死值互相不打架;生产现值要去库里量。**
   把它当成"价对了"的证明,就是把锚放在了另一个总体上。
"""
from __future__ import annotations

import ast
import io
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SERVER = REPO / "server.py"
WALLET_DB = REPO / "db" / "wallet_db.py"                 # 🔴 保护文件 · 只读
PRICING_CONFIG = REPO / "config" / "pricing_config.py"   # 🔴 保护文件 · 只读
MIGRATION_015 = REPO / "scripts" / "migration_015_social_pricing_ssot_align.sql"

#: 已知与权威值不符、且**故意不改**的副本。进这张表必须写清楚为什么。
#: 🔴 这不是豁免,是**登记**:没登记的不一致必须红。
KNOWN_STALE = {
    # [WO_241 乙 2026-09-19] 原有唯一一条 ("topic_gen", 390) **已退役**:
    #   Owner 裁定把「生成标题」两处取价统一成 503 PRICING_UNAVAILABLE、删掉 390。
    #   默认值本身没有了 ⇒ 没有「陈旧副本」可登记。
    #   🔴 这不是把锁改小来凑绿:被登记的那个对象消失了,登记就必须跟着消失,
    #      否则它会变成一张「解释一个不存在的问题」的条目,而真正新增的陈旧副本
    #      就从这个缺口溜过去(下面 test_every_registered_stale_entry_still_exists 钉它)。
}

#: `feature` 是**变量**的接线点:它替很多功能取价,没有单一目录值可比。
#: 🔴 登记在这里 = "我知道这一处比不了目录,并写下了它的默认值是什么意思"。
UNCHECKABLE_FEATURE_SITES = {
    # server.py:_bill_feature_ctx · 通用计费上下文
    # 默认值 0 **不是恒等元**,是「这一份免费」:取价没走通 ⇒ extra_cost 归零
    # ⇒ multiplier 份只收一份,**静默少收**。资金语义,改否等 Owner。
    ("server.py:_bill_feature_ctx", 0):
        "通用上下文 · feature_code 是变量 · 默认 0 意为免费(少收)· 改否等 Owner",
}


# ══════════════════════════════════════════════════════════════════
# 仓内权威值:seed + 显式 UPSERT + migration_015,三处必须自洽
# ══════════════════════════════════════════════════════════════════

_SQL_INSERT = re.compile(
    r"INSERT\s+INTO\s+feature_pricing[^)]*\)\s*VALUES\s*(.+?);",
    re.I | re.S)
_SQL_TUPLE = re.compile(r"\(\s*'([a-z0-9_]+)'\s*,\s*'[^']*'\s*,\s*(\d+)", re.I)


def _seed_from_wallet_db():
    """`PRICING_DATA` 里的 `('code', 'name', N, ...)` 元组。"""
    tree = ast.parse(io.open(WALLET_DB, encoding="utf-8").read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "PRICING_DATA" for t in node.targets):
            for elt in node.value.elts:
                vals = [getattr(v, "value", None) for v in elt.elts]
                if len(vals) >= 3 and isinstance(vals[0], str) and isinstance(vals[2], int):
                    out[vals[0]] = vals[2]
    return out


def _upserts_from_sql_text(text):
    out = {}
    for block in _SQL_INSERT.findall(text):
        for code, points in _SQL_TUPLE.findall(block):
            out[code] = int(points)
    return out


def catalog_in_repo():
    """仓内权威值。后写的覆盖先写的:seed → wallet_db 显式 upsert → migration_015。"""
    value = _seed_from_wallet_db()
    value.update(_upserts_from_sql_text(io.open(WALLET_DB, encoding="utf-8").read()))
    if MIGRATION_015.exists():
        value.update(_upserts_from_sql_text(
            io.open(MIGRATION_015, encoding="utf-8").read()))
    return value


def test_the_repo_catalog_parser_actually_found_prices():
    """🔴 仪器自检。解析器静默返回 `{}` 的话,下面每一条都会"全绿"。"""
    cat = catalog_in_repo()
    assert len(cat) >= 30, "仓内目录只解析出 %d 项 —— 解析器坏了,不是价对了" % len(cat)
    for code in ("topic_gen", "article_gen", "article_rewrite",
                 "monitoring_keyword_daily", "geo_diagnosis", "full_diagnosis"):
        assert code in cat, "解析不到 %s —— 下面用到它的判据都没有参照物" % code


# ══════════════════════════════════════════════════════════════════
# 分母锁:server.py 里取价的 fallback 必须全部走 priced()
# ══════════════════════════════════════════════════════════════════

PRICE_KEYS = {"cost_points", "cost_compute"}

#: 🔴 分母**按后果定域,不按文件定域**。
#: 第一轮我只扫了 `server.py`,于是结构上看不见两个文件以外
#: `middleware/subscription_billing.py` 里那个同形同后果的 `except: extra = 0`
#: —— **限制不在排除项里,在输入里**:一把没有任何排除条件的锁,
#: 也只能看见你喂给它的那些文件。
WIRED_FILES = [
    "server.py",
    "middleware/subscription_billing.py",
    "api/scheduler.py",
    "api/content_api.py",
    "api/marketing_material_api.py",
]

#: 枚举到了、**故意没接线**的,连同理由。登记 ≠ 豁免。
# [开源 E3 · B3b · 2026-09-28] 原唯一一条登记 api/content_api.py `cost_points or 40` 在
#   _precheck_chat_attachment_quota 里;该函数随 chat 附件两端点整段删除,被观测的对象消失 ⇒ 登记退役。
#   「取不到」与「取到 0」的资金语义问题随之不再有代码落点(交付单已报 Review)。
#   空表不是恒绿:test_no_unwrapped_price_fallback_is_left_in_any_wired_file 仍逐文件扫,新出现一处未登记即红。
KNOWN_UNWRAPPED: dict = {}


def _raw_price_fallbacks(tree):
    """还没包装的取价 fallback,两种形状一起收:

    · A `<x>.get("cost_points", <字面量>)`
    · C `<...cost_points...>.get(...) or <字面量>`   ← 第一轮漏的形状
    """
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get" \
                and len(n.args) == 2:
            key = getattr(n.args[0], "value", None)
            dflt = getattr(n.args[1], "value", None)
            if key in PRICE_KEYS and isinstance(dflt, (int, float)) \
                    and not isinstance(dflt, bool):
                hits.append((n.lineno, key, dflt))
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or) and len(n.values) == 2:
            dflt = getattr(n.values[1], "value", None)
            if isinstance(dflt, (int, float)) and not isinstance(dflt, bool) and dflt:
                lhs = ast.unparse(n.values[0])
                if any(k in lhs for k in PRICE_KEYS):
                    hits.append((n.lineno, "cost_points", dflt))
    return hits


def _priced_calls(tree):
    """走了 `priced(mapping, key, default, feature=..., where=...)` 的。"""
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if name not in ("priced", "_fb_priced") or len(n.args) < 3:
            continue
        kw = {k.arg: k.value for k in n.keywords}
        feat = kw.get("feature")
        out.append({
            "line": n.lineno,
            "key": getattr(n.args[1], "value", None),
            "default": getattr(n.args[2], "value", None),
            # 🔴 `feature` 可能是**变量**(通用计费上下文)。用 `getattr(v,"value",None)`
            #    会把它读成 None,看起来像"没给 feature" —— 那是仪器在撒谎。
            #    常量记值,非常量记它的源码文本,并单独标出来。
            "feature": getattr(feat, "value", None) if isinstance(feat, ast.Constant)
                       else (ast.unparse(feat) if feat is not None else None),
            "feature_is_literal": isinstance(feat, ast.Constant),
            "where": getattr(kw.get("where"), "value", None),
        })
    return out


def test_no_unwrapped_price_fallback_is_left_in_any_wired_file():
    """🔴 分母锁。任一在册文件里新加一处没包装的取价 fallback ⇒ 这条红。

    工单点名两处 → 我按出处枚举六处 → 这把锁找回八处 →
    **Review 复核指出分母按文件划是错的**,现在按后果划、扫一张文件清单。
    每一步的教训是同一条:**我说"没有排除条件"时,输入本身就是排除条件。**
    """
    bad = []
    for rel in WIRED_FILES:
        p = REPO / rel
        tree = ast.parse(io.open(p, encoding="utf-8").read(), rel)
        for line, key, dflt in _raw_price_fallbacks(tree):
            if (rel, key, dflt) in KNOWN_UNWRAPPED:
                continue
            bad.append((rel, line, key, dflt))
    assert bad == [], "还有没接线、也没登记的取价 fallback:%s" % (bad,)


def test_the_unwrapped_registry_is_not_empty_by_accident():
    """🔴 登记表里那几处**必须还在源码里找得到**。

    登记一条已经不存在的东西,等于给未来真的漏网留一个静默通道
    ——「有登记」会让人以为"已经想过了"。
    """
    for (rel, key, dflt), reason in KNOWN_UNWRAPPED.items():
        tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read(), rel)
        found = [h for h in _raw_price_fallbacks(tree) if h[1] == key and h[2] == dflt]
        assert found, "登记了 %s 的 %s=%s,但源码里已经没有它了 —— 该删登记" % (
            rel, key, dflt)
        assert len(reason) > 20, "登记必须写清理由:%s" % (rel,)


def test_the_unwrapped_detector_can_still_see_one():
    """🔴 正样本臂:上面那条的 `== []` 必须不是"什么都没数到"。"""
    sample = ast.parse('def f():\n    return int(p.get("cost_points", 390))\n')
    assert _raw_price_fallbacks(sample) == [(2, "cost_points", 390)]


def test_all_six_known_sites_are_wrapped_and_labelled():
    """八处都在,且每处都带得上 feature / where —— 少一个字段,日志就不可归因。

    🔴 [WO_241 乙 2026-09-19] 由 8 降为 **6**:`topic_gen` 那两处(generate/regenerate)
       已统一成 503 PRICING_UNAVAILABLE 并删掉默认值 ⇒ **没有默认值就没有"走进默认值"
       这件事可报**,发射点随之退役。这是被观测的对象消失了,不是把数字改小凑绿。

    🔴 历史:我先按"出处"枚举得 6 处,而那 6 里少了两处 ——
       `_bill_feature_ctx`(默认值是 0,被我的脚本当恒等元排除)和
       退费路径那处(接收方追不到取价调用)。**两处都是我自己的排除条件藏起来的。**
       上面那条分母锁不带任何排除,是它把这两处找回来的。
    """
    calls = _priced_calls(ast.parse(io.open(SERVER, encoding="utf-8").read()))
    assert len(calls) == 6, "priced() 接线 %d 处(应 6 处):%s" % (
        len(calls), [(c["line"], c["feature"]) for c in calls])
    for c in calls:
        assert c["feature"], "行 %s 的 priced() 没给 feature" % c["line"]
        assert c["where"] and ":" in str(c["where"]), (
            "行 %s 的 where 不是 `文件:函数` 形式:%r" % (c["line"], c["where"]))
        assert isinstance(c["default"], (int, float)), c

    features = sorted({c["feature"] for c in calls if c["feature_is_literal"]})
    # [WO_241 乙] `topic_gen` 两处已退役(改 503、删默认值)⇒ 它不该再出现在这里。
    assert features == ["article_gen", "article_rewrite",
                        "monitoring_keyword_daily"], features
    variable = [c["where"] for c in calls if not c["feature_is_literal"]]
    assert variable == ["server.py:_bill_feature_ctx"], variable


# ══════════════════════════════════════════════════════════════════
# 副本一致锁
# ══════════════════════════════════════════════════════════════════

def test_every_price_literal_matches_the_repo_catalog_or_is_registered():
    """🔴 每个写死的价要么等于仓内权威值,要么在 KNOWN_STALE 里登记过原因。

    登记 ≠ 豁免:登记是"我知道它不一致、并写下了为什么"。
    没登记的不一致必须红 —— 那就是一份**正在悄悄漂**的副本。
    """
    cat = catalog_in_repo()
    calls = _priced_calls(ast.parse(io.open(SERVER, encoding="utf-8").read()))
    bad = []
    for c in calls:
        feature, used = c["feature"], c["default"]
        if not c["feature_is_literal"]:
            # feature 是变量 ⇒ 没有单一目录值可比,必须登记
            if (c["where"], used) not in UNCHECKABLE_FEATURE_SITES:
                bad.append((c["line"], feature, used, "feature 是变量且未登记"))
            continue
        if feature not in cat:
            bad.append((c["line"], feature, used, "仓内目录里没有这个 feature_code"))
            continue
        if cat[feature] == used:
            continue
        if (feature, used) in KNOWN_STALE:
            continue
        bad.append((c["line"], feature, used, "仓内现值 %s" % cat[feature]))
    assert not bad, "写死价与仓内目录不一致且未登记:%s" % (bad,)


def test_the_registry_does_not_hide_a_value_that_now_agrees():
    """🔴 登记表要会**过期**。

    哪天 topic_gen 那个 390 被改成 80(或目录被改成 390),这条登记就成了
    一条"解释一个不存在的问题"的注释 —— 而它会继续让真的不一致被放行。
    """
    cat = catalog_in_repo()
    stale = [(f, v) for (f, v) in KNOWN_STALE if cat.get(f) == v]
    assert not stale, "KNOWN_STALE 里这些已经和目录一致了,该删登记:%s" % (stale,)


def test_the_consistency_lock_would_catch_a_drifted_copy():
    """🔴 正样本臂:构造一份"漂了的副本",锁必须判它红。

    没有这一条,上面那条全绿可能只是因为 `_priced_calls` 什么都没返回。
    """
    cat = catalog_in_repo()
    drifted = {"feature": "article_gen", "default": cat["article_gen"] + 1,
               "line": 1, "key": "cost_points", "where": "x:y"}
    assert drifted["default"] != cat[drifted["feature"]]
    assert (drifted["feature"], drifted["default"]) not in KNOWN_STALE


#: `where` 的末段**不是**最内层函数、但**故意如此**的,连同理由。
WHERE_LABEL_OVERRIDES = {
    # 限定名比裸名更有用:`generate_task` 是嵌套 worker,单看名字不知道属于谁
    ("server.py", "api_start_articles.generate_task"): "限定名 · 嵌套 worker",
    # 发射点在 helper 里,但运维要找的是**哪个业务动作**出的声
}


def test_no_where_label_names_a_function_that_does_not_exist():
    """🔴 `where` 里那个函数名必须**真的在那个文件里**。

    2026-09-18 我一次写错 5 个:`_video_asr_overrun_balance_check` /
    `daily_monitoring` / `consume_subscription_entitlement` /
    `estimate_workflow_cost` / `get_pricing_summary` —— **五个都不存在**,
    是我照着上下文"推"出来的名字。
    这种错**永远不会红**:日志照常出、字段照常齐,
    只有半夜照着它去 grep 的人会发现文件里根本没有这个函数。
    (本仓 feedback_cite-a-helper-by-its-def-line-not-by-a-name-you-inferred。)
    """
    bad = []
    for rel in WIRED_FILES:
        src = io.open(REPO / rel, encoding="utf-8").read()
        tree = ast.parse(src, rel)
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        spans = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            nm = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if nm not in ("priced", "_fb_priced", "fired", "_fb_fired",
                          "raised", "_fb_raised", "copy_diverged"):
                continue
            kw = {k.arg: getattr(k.value, "value", None) for k in n.keywords}
            where = kw.get("where")
            if not where:
                bad.append((rel, n.lineno, "没有 where")); continue
            file_part, _, fn_part = str(where).partition(":")
            if file_part != rel:
                bad.append((rel, n.lineno, "where 的文件段是 %r" % file_part)); continue
            leaf = fn_part.split(".")[-1]
            if leaf not in names:
                bad.append((rel, n.lineno, "%r 不是本文件里的函数" % leaf)); continue
            if (rel, fn_part) in WHERE_LABEL_OVERRIDES:
                continue
            inner = sorted([s for s in spans if s[0] <= n.lineno <= s[1]],
                           key=lambda s: s[1] - s[0])
            if inner and inner[0][2] != leaf:
                bad.append((rel, n.lineno,
                            "标的是 %r,实际在 %r 里(要么改标签,要么登记理由)"
                            % (leaf, inner[0][2])))
    assert not bad, "where 标签对不上:%s" % (bad,)


#: 发射点花名册:`(文件, where 末段, 发射器)`。
#: 🔴 这是**发射面的分母**。少一条就红 —— 没有它,删掉任一处发射只会让
#:    "扫不到违规"更容易达成:**扫不到违规**和**没东西可扫**读数完全同形。
EXPECTED_EMISSIONS = {
    ("server.py", "get_client_monitoring_config", "priced"),
    ("server.py", "get_client_monitoring_config", "raised"),
    ("server.py", "_bill_feature_ctx", "priced"),
    # [WO_241 乙] api_generate_titles / api_regenerate_titles 两条已退役 ——
    #   改 503 + 删默认值之后,这两处不再有 fallback 可进。19 → 17。
    ("server.py", "api_start_articles", "priced"),
    ("server.py", "api_start_articles.generate_task", "priced"),
    ("server.py", "_run_batch_rewrite_job", "priced"),
    ("server.py", "api_batch_rewrite_articles", "priced"),
    ("middleware/subscription_billing.py", "_feature_fallback_points", "fired"),
    ("middleware/subscription_billing.py", "charge_subscription_entitlement", "raised"),
    ("api/scheduler.py", "_run_clients_with_billing", "raised"),
    # [开源 E3 · B3b · 2026-09-28] content_api._precheck_chat_attachment_quota 随 chat 附件端点整段删除,不再是发射点
    ("api/marketing_material_api.py", "api_pricing", "fired"),
}

_EMITTERS = {"priced": "priced", "_fb_priced": "priced",
             "fired": "fired", "_fb_fired": "fired",
             "raised": "raised", "_fb_raised": "raised",
             "copy_diverged": "copy_diverged"}


def _emission_sites():
    found = set()
    for rel in WIRED_FILES:
        tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read(), rel)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            nm = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if nm not in _EMITTERS:
                continue
            kw = {k.arg: getattr(k.value, "value", None) for k in n.keywords}
            where = str(kw.get("where") or "")
            found.add((rel, where.partition(":")[2], _EMITTERS[nm]))
    return found


def test_the_emission_roster_is_exactly_what_it_should_be():
    """🔴 发射面的分母锁:少一处、多一处未登记的,都红。"""
    found = _emission_sites()
    missing = EXPECTED_EMISSIONS - found
    extra = found - EXPECTED_EMISSIONS
    assert not missing, "这些发射点不见了:%s" % (sorted(missing),)
    assert not extra, "多了未登记的发射点(是好事,但要登记):%s" % (sorted(extra),)


def test_the_two_shadow_catalog_outcomes_are_not_collapsed():
    """🔴 影子目录里**有**这个 feature、和**连影子目录里都没有**,是两件事。

    前者是「按一份与目录各写一遍的副本收费」;
    后者是 `hardcoded.get(feature_code, 100)` —— **对着一个谁也没定过价的功能开价 100**。
    压成一种,日志读起来像同一件事,而要找的人不一样。

    🔴 第一版这条只查了「这两个 reason 串在文件里都出现过」 ——
       注毒把判别条件改成 `if True:`(`unknown_feature` 那支从此**永不执行**),
       串还在,**判据全绿**。这是 static-sees-could / runtime-sees-does 的又一次:
       两个分支都"存在"不等于两个分支都**到得了**。
       所以改成钉**判别式本身**:那个 `if` 的条件必须真的在
       `feature_code` 与 `hardcoded` 之间做成员判断。
    """
    src = io.open(REPO / "middleware" / "subscription_billing.py", encoding="utf-8").read()
    tree = ast.parse(src)

    def _reasons_in(nodes):
        out = set()
        for b in nodes:
            for n in ast.walk(b):
                if isinstance(n, ast.Call) and (getattr(n.func, "id", None)
                                                or getattr(n.func, "attr", None)) in _EMITTERS:
                    kw = {k.arg: getattr(k.value, "value", None) for k in n.keywords}
                    if kw.get("reason"):
                        out.add(kw["reason"])
        return out

    discriminating = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.If):
            continue
        yes, no = _reasons_in(n.body), _reasons_in(n.orelse)
        if "lookup_raised" in yes and "unknown_feature" in no:
            discriminating.append(n)
    assert discriminating, (
        "找不到一个 if:一支记 lookup_raised、另一支记 unknown_feature "
        "—— 两种结果没有被分开")
    test_src = ast.unparse(discriminating[0].test)
    assert "feature_code" in test_src and "hardcoded" in test_src, (
        "判别条件是 %r —— 它不在 feature_code 与影子目录之间做判断,"
        "两支里必有一支永远到不了(而两个 reason 串照样都在文件里)" % test_src)


# [开源 E3 · B2 · 2026-09-28] AI 助手 agents/social_agent.py 随 E3 删除:它的诊断确认卡副本(对客半)与
#   副本漂移观测器 _fb_check_diagnosis_cost_copy 一起没了,守它们的 4 格退役。


def test_topic_gen_pricing_is_fail_closed_on_both_title_paths():
    """🔴 [WO_241 乙] 取代原 `test_the_two_paths_still_disagree_on_the_same_exception`。

    那条锁钉的是「`api_generate_titles` fail-fast vs `_run_batch_rewrite_job` fail-soft
    这个分歧仍然存在」—— 而 **Owner 09-19 已经把 topic_gen 那一侧裁掉了**:
    两条「生成标题」路径统一 503 `PRICING_UNAVAILABLE`、删默认值。
    「仍然存在」型的交接锁必须**与修法同班退役**,否则它会在修好之后继续报红,
    下一个人只能靠把它删掉来过 —— 那时真正的分歧也一起没人看了。
    (本仓 a-lock-that-asserts-a-defect-is-still-open-must-be-retired-with-the-fix。)

    新锁钉现在为真的三件:
      ① 两条标题路径都走**同一个** `_require_topic_pricing`;
      ② 它们都不再有 `priced()` 默认值(花名册里已无 topic_gen);
      ③ 取不到价时抛的是 503 `PRICING_UNAVAILABLE`,不是 500、不是默认值。
    """
    tree = ast.parse(io.open(SERVER, encoding="utf-8").read(), "server.py")
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    for name in ("api_generate_titles", "api_regenerate_titles"):
        fn = fns.get(name)
        assert fn is not None, "找不到 %s —— 判据前提变了" % name
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "_require_topic_pricing"]
        # 🔴 这里只钉「走了守卫」,**不钉处数**。
        #    处数(generate=2 含组织路径 / regenerate=1)钉在
        #    `tests/topic_gen_pricing_fail_closed_2026_09_19::GUARD_CALLS` —— 那是它的主人。
        #    两个包各钉一遍同一个事实,就会在下一次改动时一处跟不上另一处
        #    (我刚加组织路径那一处时,这条就是这么红的)。
        assert calls, "%s 没有走 _require_topic_pricing" % name

    # ② topic_gen 不该再出现在发射面花名册里
    assert not [e for e in EXPECTED_EMISSIONS if "titles" in e[1]], (
        "花名册里还留着标题路径的发射点 —— 默认值已删,它们不该还在")

    # ③ 单点函数抛的必须是 503 PRICING_UNAVAILABLE
    guard = fns.get("_require_topic_pricing")
    assert guard is not None, "找不到 _require_topic_pricing"
    gsrc = ast.unparse(guard)
    assert "PRICING_UNAVAILABLE" in gsrc, "没有用工单指定的 code"
    assert "503" in gsrc, "不是 503"
    # 🔴 数 390 要**只数代码**:这个函数的 docstring 正当地讲了"改前是按 390 扣钱",
    #    而 `ast.unparse` 会把 docstring 一起 unparse 出来。第一版我漏了这一步,
    #    被自己的锁判红 —— 与 WO_242 里「注释复述代码把注毒的锚变成两处」同一个形状:
    #    **文本计数分不出代码与说明**。所以剥掉 docstring 再数。
    body = list(guard.body)
    if body:
        first = body[0]
        is_docstring = (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str))
        if is_docstring:
            body = body[1:]
    code_only = " ".join(ast.unparse(n) for n in body)
    assert "390" not in code_only, "单点函数的**代码**里又出现了写死的 390"


def test_the_article_rewrite_fail_soft_divergence_is_still_registered():
    """🔴 topic_gen 那一侧裁掉了,`article_rewrite` 那一侧**还没有**。

    `_run_batch_rewrite_job` / `api_batch_rewrite_articles` 仍是
    `(pricing or {})` 静默兜底 + 260 默认值。Owner 只裁了「生成标题」。
    **不许顺手一起统一** —— 那是改计费失败时的资金行为,属五类之一。
    这一格保证它不会因为"隔壁修好了"而被静默带走或被遗忘。
    """
    src = io.open(SERVER, encoding="utf-8").read()
    calls = _priced_calls(ast.parse(src, "server.py"))
    soft = [c for c in calls if c["feature"] == "article_rewrite"]
    assert len(soft) == 2, "article_rewrite 的兜底取价应仍有 2 处,实得 %d" % len(soft)
    lines = src.splitlines()
    for c in soft:
        assert "(pricing or {})" in lines[c["line"] - 1], (
            "行 %d 的静默兜底被去掉了 —— 资金语义,要 Owner 裁,不能顺手统一" % c["line"])


def test_every_registered_stale_entry_still_exists():
    """🔴 `KNOWN_STALE` 里的每一条,都必须在 server.py 里找得到对应的默认值。

    登记一条已经不存在的东西,会让「有登记 ⇒ 已经想过了」变成一句空话,
    而真正新增的陈旧副本就从这个缺口溜过去。
    (WO_241 乙 删掉 390 之后,这张表清空了 —— 清空是对的,留着才是错的。)
    """
    calls = _priced_calls(ast.parse(io.open(SERVER, encoding="utf-8").read()))
    present = {(c["feature"], c["default"]) for c in calls if c["feature_is_literal"]}
    dead = [k for k in KNOWN_STALE if k not in present]
    assert not dead, "KNOWN_STALE 里这些默认值在代码里已经没有了,该删登记:%s" % (dead,)


def test_the_preset_refund_literal_is_the_same_copy_as_the_lookup_default():
    """🔴 退费路径上 `_partial_refund = _err * 390` 是同一个价的**第三份副本**。

    它在 `try` 外面,防的是取价抛异常时 `except` 里引用 `_base_ag` 会 NameError。
    也就是说:**取价一崩,真正生效的退款金额就是这个字面量。**
    它和下一行取价的默认值必须是同一个数 —— 两份不一致时,
    退的钱会取决于"崩在哪一步",而两处看起来都对。
    """
    # 🔴 用 AST 不用正则:注释里若出现同样的字样,正则会把注释当代码。
    #    (注毒时我自己的注释就让锚一次命中两处,读数变成「毒没下成」。)
    src = io.open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)
    presets = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and getattr(n.targets[0], "id", None) == "_partial_refund" \
                and isinstance(n.value, ast.BinOp) and isinstance(n.value.op, ast.Mult):
            for side in (n.value.left, n.value.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, int):
                    presets.append(side.value)
    assert len(presets) == 1, "预置退款金额有 %d 处(应 1 处):%s" % (len(presets), presets)
    preset = presets[0]
    calls = _priced_calls(tree)
    refund_site = [c for c in calls
                   if c["where"] == "server.py:api_start_articles.generate_task"]
    assert len(refund_site) == 1, refund_site
    assert preset == refund_site[0]["default"], (
        "预置退款用 %d、取价默认用 %d —— 同一个价两份副本已经分家"
        % (preset, refund_site[0]["default"]))
    assert preset == catalog_in_repo()["article_gen"], (
        "退费路径这两份副本一致,但都和仓内目录 %s 不一样"
        % catalog_in_repo()["article_gen"])


# ══════════════════════════════════════════════════════════════════
# 对客半:确认卡上那份副本
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# 保护文件 config/pricing_config.py —— 只读、只比、不改
# ══════════════════════════════════════════════════════════════════

def _pricing_defaults():
    tree = ast.parse(io.open(PRICING_CONFIG, encoding="utf-8").read())
    for n in ast.walk(tree):
        tgt = None
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "_PRICING_DEFAULTS":
            tgt = n.value
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", None) == "_PRICING_DEFAULTS" for t in n.targets):
            tgt = n.value
        if tgt is not None:
            return ast.literal_eval(tgt)
    return None


def test_pricing_config_call_site_defaults_match_the_defaults_dict():
    """🔴 `config/pricing_config.py` 里 33 处调用点默认值 == `_PRICING_DEFAULTS`。

    同一个价在一个文件里写了两遍。今天两遍一致(所以没出事),
    但**没有任何东西保证它们一起改** —— 这条就是那个保证。

    🔴 该文件是保护文件七项之一:本判据**只读它**,WO_240 一个字节没动它。
    """
    defaults = _pricing_defaults()
    assert defaults and len(defaults) >= 40, "解析不到 _PRICING_DEFAULTS —— 仪器坏了"
    tree = ast.parse(io.open(PRICING_CONFIG, encoding="utf-8").read())
    checked, bad, absent = 0, [], []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get"
                and len(n.args) == 2):
            continue
        try:
            key = ast.literal_eval(n.args[0])
            dflt = ast.literal_eval(n.args[1])
        except Exception:                                # noqa: BLE001
            continue
        if not isinstance(key, str):
            continue
        recv = ast.unparse(n.func.value)
        if "cfg" not in recv and "pricing_config" not in recv:
            continue
        if key not in defaults:
            absent.append((n.lineno, key, dflt))
            continue
        checked += 1
        if defaults[key] != dflt:
            bad.append((n.lineno, key, dflt, defaults[key]))
    assert checked >= 30, "只比到 %d 处 —— 扫描器坏了,不是都一致" % checked
    assert not bad, "同一个配置值在文件里写了两份且不相等:%s" % (bad,)
    assert not absent, "这些键不在 _PRICING_DEFAULTS 里,默认值**真能打出来**:%s" % (absent,)
