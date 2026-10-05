"""`is_test` 打标正则与 CLAUDE.md 铁律对齐(#99 · 2026-09-05)。

误标的后果不是数据洁癖:测试客户 filter **默认 ON** ⇒ 被误标的品牌
**从服务商的客户列表里消失**,而且不报错。所以反样本和正样本一样重要。

🔴 **本轮不回溯写 `brands.is_test`**(Owner)。存量误标由规则在下一次打标时自然覆盖;
   「哪些存量行只可能由被删掉的三个词打上标」已派 Deploy 只读,取数不改数。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from utils.is_test_brand import (
    REAL_CLIENT_BRAND_IDS,
    TEST_NAME_PATTERNS,
    detect_is_test_for_existing_brand,
    detect_is_test_for_new_brand,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", [
    "XX测试", "验收专用", "压测客户01",              # CJK 三支
    "abc_test",                                    # `_test` 那一支
    "stress-J-1778426622",                         # 连字符分隔
    "breakdown_e2e_prod 的创作空间",                # 🔴 下划线分隔:`\b` 接不住这个
    "TEST客户", "e2e-回归",                         # 大小写 / CJK 相邻
])
def test_real_test_brands_are_flagged(name):
    assert detect_is_test_for_new_brand(name) is True, "%r 没被标成测试客户" % name


@pytest.mark.parametrize("name", [
    "demo 家居", "Sandbox 咖啡",                    # 删掉的裸词,不该再误标
    "latest 科技", "Protest 品牌",                  # `test` 嵌在词中间
    "e2enterprise",                                # `e2e` 嵌在词首
    "Breakdance 舞蹈", "Stressless 家具",           # 🔴 与**新加的** breakdown/stress 同前缀
    "喜茶",                                        # 基线:普通真品牌
])
def test_real_customer_brands_are_not_flagged(name):
    """🔴 `Breakdance` / `Stressless` / `e2enterprise` 三个是**新词的边界样本**。

    原规格给的三个反样本(demo / Sandbox / latest)只覆盖旧词 ——
    不加这三个,**新加的 breakdown/stress/e2e 的边界根本没被验过**。
    """
    assert detect_is_test_for_new_brand(name) is False, "%r 被误标成测试客户" % name


def test_word_boundary_is_defined_by_alnum_not_by_backslash_b():
    """🔴 `\b` 与「breakdown_e2e_prod 必标」**互斥** —— `_` 是正则里的词字符。

    实测两臂:加 `\b` 漏标 1 个正样本;去 `\b` 误标 latest / Protest / e2enterprise。
    **两边各在一个方向上坏掉**,同轴滑到另一端救不了 —— 换的是边界的**定义**。
    这条钉住那个定义,免得后人"顺手改回 `\b`"。
    """
    src = (ROOT / "utils" / "is_test_brand.py").read_text(encoding="utf-8")
    assert "(?<![A-Za-z0-9])" in src and "(?![A-Za-z0-9])" in src, "边界定义被换掉了"
    assert r"\b" not in src, "`\b` 回潮 —— 它接不住 breakdown_e2e_prod"


def test_the_redundant_underscore_test_branch_is_kept_on_purpose():
    """🔴 `_test` 现在是冗余(新边界已能接住 `abc_test`),但**故意留着**。

    留一支冗余代价是零,删错的代价是**漏标**。这条判据存在的意义就是
    让下一个"顺手清冗余"的人当场变红 —— 注释拦不住,门才拦得住。
    """
    assert "_test" in TEST_NAME_PATTERNS.pattern, "冗余但故意保留的 `_test` 支被删了"


def test_the_five_iron_rule_words_are_all_present():
    """CLAUDE.md 铁律 5 项一个都不能少 —— 分母取自铁律,不是我记得的那几个。"""
    for word in ("测试", "test", "_demo", "_test", "验收"):
        assert word in TEST_NAME_PATTERNS.pattern, "铁律里的 %r 不在正则里" % word


def test_whitelisted_real_clients_win_over_the_name():
    """白名单优先于名字 —— 真客户即使名字里有"测试"也不算测试客户。"""
    for bid in list(REAL_CLIENT_BRAND_IDS)[:3]:
        assert detect_is_test_for_existing_brand(bid, "XX测试") is False


def test_the_detector_is_alive():
    """正样本自证:两个方向都要有区分力,否则上面两族可能各自恒真/恒假。"""
    assert detect_is_test_for_new_brand("压测") is True
    assert detect_is_test_for_new_brand("喜茶") is False
    assert detect_is_test_for_new_brand(None) is False
    assert detect_is_test_for_new_brand("") is False


def _import_test_file(path):
    import importlib.util

    spec = importlib.util.spec_from_file_location('denom_probe_' + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_test_file_in_this_package_is_importable():
    """🔴 本包每个测试文件都必须**能被导入**。

    实测踩过:一个模块在收集期把 pytest 的 capture 压垮,pytest **中断收集**,
    同包 4 个文件 **50 条判据静默离开分母** —— 而剩下的**全绿**。
    「全绿」不携带「分母没缩水」;分母缩水不会让任何东西变红,只能显式去数。

    🔴 第一版这条锁读 `sys.modules`,**单跑一个文件时必红** ——
    它分不开「收集被中断」与「用户只跑了一个文件」。
    改成**上下文无关**的形式:逐个 import,导不进去才是真问题。
    """
    here = pathlib.Path(__file__).resolve().parent
    files = sorted(p for p in here.glob('test_*.py'))
    assert len(files) >= 5, '本包测试文件只剩 %d 个 —— 分母塌了' % len(files)
    broken = []
    for f in files:
        if f.name == pathlib.Path(__file__).name:
            continue        # 自己正在被跑,不用再导一次
        try:
            _import_test_file(f)
        except Exception as err:        # noqa: BLE001 —— 什么原因都算坏
            broken.append('%s: %s' % (f.name, type(err).__name__))
    assert not broken, '这些测试文件导不进去,它们的判据一条都不会跑:%s' % broken


def test_the_importability_probe_actually_catches_a_broken_file(tmp_path):
    """正样本自证:喂一个语法就坏的合成文件,探针必须报错。

    否则「零 broken」与「探针从来没真的 import 过」同形。
    """
    bad = tmp_path / 'test_synthetic_broken.py'
    bad.write_text('def f(:' + chr(10), encoding='utf-8')
    with pytest.raises(SyntaxError):
        _import_test_file(bad)
