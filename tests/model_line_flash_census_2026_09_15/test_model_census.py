# -*- coding: utf-8 -*-
"""WO_217-c1a 判据 —— 冻结普查 + 锁住签字表。

本单**不切换任何模型**,所以这里没有「回显 == 常量」那种行为锁(那是 c1b)。
这里锁的是**分母本身**:哪天分母变了,必须有人回来看一眼。
"""
import ast
import io
import os
import re

import pytest

from tests.model_line_flash_census_2026_09_15 import census as C
from tests.model_line_flash_census_2026_09_15 import classified as X

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_VENDOR_WORDS = ("qwen", "deepseek", "kimi", "moonshot", "doubao", "claude",
                 "gemini", "gpt", "glm", "siliconflow")


@pytest.fixture(scope="module")
def rows():
    return C.census(ROOT)


# ══════════════════════════════════════════════════════════════════
# 1. 分母冻结(两侧)
# ══════════════════════════════════════════════════════════════════
def test_census_totals_are_frozen(rows):
    """总数两侧都锁。

    🔴 少了也必须红。c1b 会把字面量换成常量 —— 那时 C 腿接手计数,总数应该**不变**;
       如果总数掉了,说明有发出点**消失了而不是被换掉**,那是另一回事。
    """
    prod = [r for r in rows if r["scope"] != "not_production"]
    got = {"total_all": len(rows), "total_prod": len(prod)}
    for sc in ("geo_work", "monitored_engine", "social_owner_excluded"):
        got[sc] = len([r for r in prod if r["scope"] == sc])
    assert got == X.FROZEN_TOTALS, (
        "普查读数变了。冻结=%s 实测=%s。" % (X.FROZEN_TOTALS, got) +
        "先去看差在哪几行,再决定是调冻结值还是这次改动有问题 —— 别直接改数字。")


def test_file_literal_count_is_two_sided(rows):
    """每个文件的处数两侧都锁,并且**文件集合**也锁。"""
    actual = {}
    for r in rows:
        if r["scope"] != "not_production":
            actual[r["path"]] = actual.get(r["path"], 0) + 1
    frozen = X.FILE_LITERAL_COUNT
    new_files = sorted(set(actual) - set(frozen))
    gone_files = sorted(set(frozen) - set(actual))
    over = {p: (frozen[p], actual[p]) for p in frozen if p in actual and actual[p] > frozen[p]}
    under = {p: (frozen[p], actual[p]) for p in frozen if p in actual and actual[p] < frozen[p]}
    assert not new_files, (
        "出现了没分类的文件(里面有模型名):%s —— "
        "去读那几行,判清是 emit 还是别的,再登记进 FILE_LITERAL_COUNT" % new_files)
    assert not gone_files, ("这些文件不再出现模型名:%s —— 结论可能已过期" % gone_files)
    assert not over, ("这些文件多出了模型名(冻结, 实测):%s" % over)
    assert not under, ("这些文件少了模型名(冻结, 实测):%s" % under)
    assert len(frozen) == X.FROZEN_PROD_FILES


# ══════════════════════════════════════════════════════════════════
# 2. 校准腿:锚漏掉的必须**被签字**,不许沉默
# ══════════════════════════════════════════════════════════════════
def test_every_vendor_named_calibration_entry_is_signed():
    """🔴 这条是本包的心脏。

    L 腿的锚一定会漏。本单实测漏了两次(整个 qwen3.x 族、siliconflow),
    两次都是 K 腿喊出来的。所以规矩是:K 腿列出、名字里含厂商字样、
    而 L 腿不认的每一条,都必须在 `CALIBRATION_SIGNED` 里有一句签了字的话。

    漏签的表现是**沉默**(那个名字既不在分母里,也不在例外表里),
    而沉默看起来和「已全覆盖」一模一样。
    """
    gap = C.calibration_gap(ROOT)
    need = sorted(s for s in gap
                  if any(v in s.lower() for v in _VENDOR_WORDS))
    unsigned = [s for s in need if s not in X.CALIBRATION_SIGNED]
    assert not unsigned, (
        "这些名字里含厂商字样、L 腿不认、又没签字:%s%s"
        % (unsigned, "  出处示例:%s" % {s: sorted(gap[s])[:1] for s in unsigned[:5]}))


def test_signed_is_model_entries_really_appear_in_the_census(rows):
    """签字说「它是真的模型指定」的,必须真的在分母里查得到。

    否则这张签字表自己过期了 —— 而过期的签字比没签更糟:
    它是一句**看起来已经有人核过**的话。
    """
    names = {r["model"] for r in rows}
    missing = [k for k in X.CALIBRATION_MUST_APPEAR if k not in names]
    assert not missing, ("签字表说这些是真模型指定,但普查里查不到:%s" % missing)


def test_the_anchor_still_sees_the_qwen3_family(rows):
    """🔴 回归锁:锚**曾经漏掉整单的主角**。

    第一版 `(?:qwen)[-.]` 要求厂商名后紧跟分隔符,而阿里写的是 `qwen3.7-max` ——
    于是 qwen3.x 整族一条没进表,**而表看起来是满的**。
    这条判据把那次事故钉住:少了任何一个,当场红。
    """
    names = {r["model"] for r in rows}
    must = ["qwen3.7-max", "qwen3-max", "qwen3.6-plus", "qwen3.6-flash",
            "qwen3.7-plus", "qwen3-asr-flash", "qwen3-vl-rerank"]
    missing = [m for m in must if m not in names]
    assert not missing, (
        "锚又看不见 qwen3.x 了:%s —— 八成是 _MODEL_LITERAL 被改窄了" % missing)


def test_the_anchor_still_sees_the_registry_key_scheme(rows):
    """回归锁:第三套命名(`LLM_PROVIDERS` 的 model_key)。

    `server.py:15232` 写文章的默认档就是这种写法,L 腿一条看不见。
    """
    names = {r["model"] for r in rows}
    assert "dashscope-deepseek-v4-pro" in names, (
        "M 腿(注册表键)失效 —— server.py:15232 那个写文章默认档又看不见了")
    assert len(C.model_registry_keys(ROOT)) >= 10, "LLM_PROVIDERS 读不出来了"


# ══════════════════════════════════════════════════════════════════
# 3. 例外表:必须有理由,且对象还在
# ══════════════════════════════════════════════════════════════════
def test_capability_exceptions_have_a_real_reason(rows):
    """例外必须说出「DeepSeek 缺哪项能力」,不许「其它」;且对象还在树上。"""
    names = {r["model"] for r in rows}
    for model, reason in X.CAPABILITY_EXCEPTIONS.items():
        assert reason and len(reason) >= 10, "%s 的例外理由太短或为空" % model
        assert "其它" not in reason and "其他" not in reason, (
            "%s 的例外理由写成了「其它」—— 例外必须逐条说清能力" % model)
        assert model in names, (
            "例外表里的 %s 在树上已经不存在了 —— 例外表过期" % model)


# ══════════════════════════════════════════════════════════════════
# 4. 登记在案的独立缺陷与死配置
# ══════════════════════════════════════════════════════════════════
def test_retired_name_on_monitoring_is_still_registered(rows):
    """监测线仍在用官方已退役的 `deepseek-v4-flash`(登记,不修)。

    哪天有人修了,这条会红 —— 那正是回来更新结论的时机。
    """
    have = {(r["path"], r["model"]) for r in rows
            if r["scope"] == "monitored_engine" and not r["comment"]}
    for path, model in X.RETIRED_NAME_ON_MONITORING:
        assert (path, model) in have, (
            "%s 里的 %s 不见了 —— 若已修好,把 RETIRED_NAME_ON_MONITORING 与"
            "C14_217_C1A 文档 §5 一起更新" % (path, model))


def test_dead_config_really_has_no_live_caller():
    """🔴 `GEO_MODEL_ROUTING` 是死配置 —— 用 AST 核,不用 grep。

    grep 会把**注释**算成调用点(`services/marketing/advisor_llm.py:22` 就是注释)。
    本仓 `a-wrong-comment-outlives-a-wrong-assertion`:注释不会红,
    所以判断死活必须看**语法树**,不看文本。
    """
    callers = []
    for rel in C.tracked_python_files(ROOT):
        rel = rel.replace("\\", "/")
        #: 🔴 不排除 config/model_config.py。
        #:   第一版排了它,结果注毒 N5(在该文件里加一个真调用点)**读绿** ——
        #:   不是锁没牙,是**毒够不着**:毒正好落在判据唯一跳过的那个文件里。
        #:   而「这个文件里没有调用」本来就要靠判据说,不是靠我假设。
        #:   `ast.Call` 不会匹配 `def`,所以定义处不会自己把自己判活。
        if C.scope_of(rel) == "not_production":
            continue
        try:
            tree = ast.parse(io.open(os.path.join(ROOT, rel), encoding="utf-8").read())
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                nm = f.id if isinstance(f, ast.Name) else (
                    f.attr if isinstance(f, ast.Attribute) else "")
                if nm in ("get_model_for_task", "get_agentscope_model_configs"):
                    callers.append("%s:%d" % (rel, n.lineno))
    assert not callers, (
        "死配置登记过期:%s 现在有**真的调用点** %s —— "
        "那 24 个任务键不再是死的,c1b 的范围要重算" % (sorted(X.DEAD_CONFIG), callers))


# ══════════════════════════════════════════════════════════════════
# 5. 仪器自检(206 的教训)
# ══════════════════════════════════════════════════════════════════
def test_no_control_chars_in_the_instrument():
    """本包自己的文件里不许有裸控制字符。

    本仓有过改写脚本把词边界 `b` 前的反斜杠吃成退格 0x08、
    于是否定断言恒真恒绿的先例(#195 全仓四处)。
    仪器坏了要**当场出声**,不是安静变绿。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    bad = []
    for fn in sorted(os.listdir(here)):
        if not fn.endswith(".py"):
            continue
        data = io.open(os.path.join(here, fn), "rb").read()
        for i, b in enumerate(data):
            if b < 0x20 and b not in (0x09, 0x0A, 0x0D):
                bad.append((fn, i, hex(b)))
    assert not bad, ("判据文件里有裸控制字符:%s" % bad[:5])


def test_the_instrument_excludes_exactly_itself_and_nothing_else():
    """🔴 [c1a'] 量尺不许把自己量进去 —— 但也不许拿排除集藏别的东西。

    c1a 交付时 total_all 冻结在 1085,是**提交本判据包之前**量的;提交后
    `git ls-files *.py` 把仪器自己数进来 ⇒ 1117,这条判据在任何干净 checkout 上都红,
    而红的理由与被测对象无关(Review 复审 @ c71331f1f 抓到)。

    修法是排除仪器自身。但**排除集是个危险的东西** —— 它天然会变成一个
    「把碍事的文件塞进去」的地方(本单 N5 那次毒够不着,就是排除项造成的盲区)。
    所以这里两侧都钉:实际被排除的文件,必须**恰好**是本判据包 + 本注毒台,
    一个不多一个不少。
    """
    import subprocess
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0
    all_py = [l for l in out.stdout.decode("utf-8").splitlines() if l.strip()]
    excluded = sorted(l.replace(chr(92), "/") for l in all_py if C.is_instrument(l))
    here = "tests/model_line_flash_census_2026_09_15/"
    expected = sorted([p for p in all_py if p.replace(chr(92), "/").startswith(here)]
                      + ["scripts/poison_217_c1a.py"])
    expected = sorted(p.replace(chr(92), "/") for p in expected)
    assert excluded == expected, (
        "排除集不是「恰好本包 + 本注毒台」。实际=%s 期望=%s —— "
        "多出来的每一个都是一片没人看的盲区" % (excluded, expected))
    assert len(excluded) >= 4, "仪器文件一个都没排除?是不是 is_instrument 失效了"


def test_models_moved_out_of_the_exception_table_are_still_tracked(rows):
    """例外表变短必须说得出**去哪了**。

    表变短有两种可能:真的不再必要(好事)/ 被谁顺手删了(坏事),
    而两者在表上长得一模一样。挪走的行要留登记,且对象还在树上 ——
    对象都没了说明这条登记本身过期。
    """
    names = {r["model"] for r in rows}
    for model, where in X.MOVED_OUT_OF_EXCEPTIONS.items():
        assert model not in X.CAPABILITY_EXCEPTIONS, (
            "%s 同时出现在例外表和挪走登记里 —— 到底算哪个" % model)
        assert where and ("WO_" in where), (
            "%s 挪走了却没写去哪个单 —— 那就是悄悄消失" % model)
        assert model in names, ("%s 已不在树上,挪走登记过期" % model)


def test_the_census_is_not_empty_and_covers_many_files(rows):
    """最低限度的活体检:普查器死了会返回空表,而空表**全绿**。"""
    assert len(rows) > 500, "普查器只找到 %d 处 —— 它多半是死了,不是仓变干净了" % len(rows)
    assert len({r["path"] for r in rows}) > 100
