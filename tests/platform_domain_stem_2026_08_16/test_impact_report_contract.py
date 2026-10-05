"""[补充单 P0-3 2026-08-16] 影响面脚本的契约锁。

Review 会自己跑这个脚本对数字(±0)。所以要锁住的是**它算的是不是那个定义**,
以及**它有没有只读**——不是锁具体数字(数字随快照变,锁死就成了假绿)。
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IMPACT = os.path.join(ROOT, "scripts", "platform_domain_impact_report.py")
RECHECK = os.path.join(ROOT, "scripts", "platform_domain_stem_recheck.py")
SAMPLE = os.path.join(ROOT, "scripts", "platform_domain_stem_sample.py")
ALL = [IMPACT, RECHECK, SAMPLE]


def _load(path: str, name: str):
    """按路径 import 脚本模块 —— 下面两条是**行为锁**,不是 grep 源码。
    (第一版写成了字符串匹配,变异 M10/M11 直接存活:改 `return "非平台"` 之后
     「样本不足」这个词仍在别处出现、`random.Random()` 仍匹配 `random.Random(`。
     没判别力的守卫比没有守卫更坏,所以换成真调用。)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _src(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


@pytest.mark.parametrize("path", ALL)
def test_scripts_are_read_only(path):
    """🔴 Owner 拍板 ②:存量不回溯。这三个脚本一句写语句都不许有。"""
    src = _src(path)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "ALTER TABLE",
                 "DROP ", "TRUNCATE", "CREATE TABLE"):
        assert verb not in body.upper().replace("UPDATED_AT", "X"), f"{path} 含写类语句 {verb}"


@pytest.mark.parametrize("path", [IMPACT, RECHECK])
def test_list_consuming_scripts_import_the_list(path):
    """用到名单的脚本必须 import,不许另抄一份 —— 抄一份就会漂移。"""
    src = _src(path)
    assert "from services.media_binding_candidates import" in src
    # 影响面脚本**故意**焊死了一份「生产尖旧名单」用来算增量:
    # 那份必须显式命名且注明不参与判定,不能是偷偷抄的现名单。
    if "PROD_TIP_LIST" in src:
        assert "不参与任何判定" in src


def test_sample_script_derives_population_from_the_criterion_not_a_list():
    """抽样脚本**不该** import 名单:它要验的是判据本身,总体必须由 classify_domain 推出来。
    (若它从名单取总体,就成了「用名单验名单」的循环论证。)"""
    src = _src(SAMPLE)
    assert "classify_domain" in src
    assert "from services.media_binding_candidates import" not in src
    assert not re.search(r'SHARED_PLATFORM_DOMAINS\s*=\s*[\[{]', src), "抽样脚本里抄了一份名单"


def test_impact_report_defines_blocked_as_three_conditions():
    """🔴 行为锁 · Review 点名的定义歧义(788 vs 54 就差在这一步):
    「被拦」= 判为平台域 **且** domain_exact **且** 拿不出名称证据。三个条件缺一不算。
    (第一版写成 `assert "_has_name_evidence" in src` —— 把整段判定删掉、只留 import 也照样绿,
     变异 M8 实测存活。所以改成真调用。)"""
    mod = _load(IMPACT, "_impact_mod")
    on_platform = lambda d: d == "zhihu.com"  # noqa: E731

    base = {"entity_domain": "zhihu.com", "match_method": "domain_exact",
            "media_name": "某独立账号", "entity_canonical": "知乎", "entity_aliases": "[]"}
    assert mod.is_blocked_row(base, on_platform) is True

    # 三条「必须不命中」——每个条件各拆一次
    assert mod.is_blocked_row({**base, "entity_domain": "agri.example-news.com"}, on_platform) is False
    assert mod.is_blocked_row({**base, "match_method": "name_alias"}, on_platform) is False
    assert mod.is_blocked_row({**base, "media_name": "知乎"}, on_platform) is False, \
        "有名称证据仍判被拦 = 定义退化成「落在平台域上」"

    src = _src(IMPACT)
    assert "on_platform_domain_total" in src and "blocked_total" in src, "两个数必须并列报出"


def test_impact_report_has_no_time_window():
    """🔴 时间窗要么去掉、要么写进标题。这里是去掉:唯一过滤是 active。"""
    src = _src(IMPACT)
    sql = src[src.index("SQL_ROWS"): src.index("def _fetch")]
    assert "created_at >" not in sql and "created_at <" not in sql and "INTERVAL" not in sql.upper()
    assert "COALESCE(c.active, TRUE)" in sql
    assert "描述,不是筛选条件" in src, "必须明说 min/max created_at 只是描述"


def test_impact_report_fingerprint_excludes_timestamps():
    """🔴 快照指纹只能用计数。掺时间戳会让生产与本地快照库被判成不同快照,
    制造出「数字对不上」的假差异 —— 而这正是本轮要收口的问题。"""
    src = _src(IMPACT)
    m = re.search(r"count_keys = \[(.*?)\]", src, re.S)
    assert m, "找不到指纹输入清单"
    keys = re.findall(r'"([^"]+)"', m.group(1))
    assert keys, "指纹输入为空"
    assert not [k for k in keys if "created" in k or "updated" in k or "_at" in k], \
        f"指纹掺了时间戳:{keys}"


def test_impact_report_reports_both_absolute_and_delta():
    """交付书报的「54」是增量、Review 复算的「788」是绝对量 —— 两个都要有,才不会再对不上。"""
    src = _src(IMPACT)
    assert "baseline_blocked_total" in src and "delta_blocked_total" in src




def test_recheck_labels_insufficient_sample_as_its_own_verdict():
    """🔴 行为锁:「样本不足」是**第三类**,不是「非平台」——
    拿无信息的判定去改名单,等于用沉默当证据。"""
    from services.media_name_stem import classify_domain

    mod = _load(RECHECK, "_recheck_mod")
    few = classify_domain("tiny.example.com", ["甲媒体", "乙媒体"])
    assert mod.verdict_label(few) == "样本不足"
    # 成对反向:样本够但同一主体 → 必须是「非平台」而不是「样本不足」
    many_same = classify_domain("news.cn", ["新华网A", "新华网B", "新华网C", "新华网D", "新华网E"])
    assert mod.verdict_label(many_same) == "非平台"
    many_diff = classify_domain("x.com", ["甲媒体", "乙资讯", "丙观察", "丁快报", "戊时报"])
    assert mod.verdict_label(many_diff) == "平台"


def test_sample_is_reproducible_and_actually_seeded():
    """🔴 行为锁:同 seed 必同结果(可复跑),不同 seed 必不同结果(证明种子真的在起作用)。"""
    mod = _load(SAMPLE, "_sample_mod")
    pool = [f"d{i}.example.com" for i in range(60)]
    a = mod.pick_sample(pool, seed=20260816, n=30)
    b = mod.pick_sample(pool, seed=20260816, n=30)
    assert a == b, "同 seed 结果不一致 = 抽样不可复跑"
    c = mod.pick_sample(pool, seed=1, n=30)
    assert a != c, "换 seed 结果不变 = 种子根本没参与抽样(去掉 seed 也照样绿)"
    assert len(a) == 30 and len(set(a)) == 30
    assert mod.pick_sample(pool, seed=1, n=999) == sorted(pool), "n 超总体应退化为全取"


def test_sample_script_does_not_conclude_for_the_human():
    src = _src(SAMPLE)
    assert "本脚本不替人下结论" in src, "抽样脚本不许自己下结论,判读必须留给人"
