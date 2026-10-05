"""包F ⑧ —— Z-6 签发 + 水印撤除 + MET-45 真内容。判据**成对**。

工单逐字:「customerPresentationSigned 放开,五卡终态呈现解锁,
客户版水印撤除的判据**成对**(签发前水印在/签发后终态在)」。

🔴 成对的意义
------------
只判"签发后终态在"的话,把签发闸整个删掉同样全绿 —— 而那时未签状态
也会下发终态,MET-36 就废了。所以每条都配一发反向:
把签发状态拨回未签,水印必须回来、对客闸必须重新拦住。
"""

from __future__ import annotations

import json

import pytest

from services.defensive_geo.presentation import level_policy as LP
from services.defensive_geo.presentation import projection as PROJ
from services.defensive_geo.presentation import registries as R

SETTINGS_KEY = LP.SETTINGS_KEY


# ══════════════════════════════════════════════════════════════════════
# 1. 签发状态(成对)
# ══════════════════════════════════════════════════════════════════════

def test_all_three_policies_are_signed_with_traceable_metadata():
    """三项全签,**且**每项都有可解析的签发人/日期(ACT-12)。

    🔴 只判 ``signed is True`` 不够:一个 ``signed=True`` 而
       ``signed_by=None`` 的签发是没法追责的,等于没签。
    """
    assert R.unsigned_policies() == (), (
        f"还有未签的:{R.unsigned_policies()}")
    for pid in (R.LEVEL_POLICY_VERSION, R.STATE_RULE_VERSION,
                R.SECTION_REGISTRY_VERSION):
        sig = R.signature(pid)
        assert sig.signed is True, pid
        assert sig.signed_by, f"{pid} 没有签发人 —— 无法追责的签发等于没签"
        assert sig.signed_at, f"{pid} 没有签发日期"
        assert sig.note, f"{pid} 没有签发依据说明"
    # R-2 分工:商业口径必须是 Owner 签,不能由执行方代签
    assert R.signature(R.LEVEL_POLICY_VERSION).signed_by == "owner", (
        "等级阈值是**商业口径**(对客承诺),§0.5.2 R-2 要求 Owner 签,"
        "执行方代签 = 用一个没人拍板的标准给客户发承诺")


def test_customer_gate_opens_only_because_of_the_signatures():
    """对客闸放行,**且**它真的由签发状态驱动(反向对照)。"""
    R.require_signed_for_customer("customer")       # 不抛 = 放行

    # 反向:任一项拨回未签 ⇒ 对客闸必须重新拦住
    original = dict(R._SIGNATURES)
    try:
        pid = R.LEVEL_POLICY_VERSION
        R._SIGNATURES[pid] = original[pid]._replace(signed=False)
        with pytest.raises(R.PolicyNotSigned):
            R.require_signed_for_customer("customer")
        # 服务商面**不**受此闸约束(它本来就该能看)
        R.require_signed_for_customer("service_provider")
    finally:
        R._SIGNATURES.clear()
        R._SIGNATURES.update(original)


def test_watermark_flag_is_paired_with_the_signature_state():
    """水印开关 = ``not unsigned_policies()``,两个方向都验。

    前端 ``DefensiveReportView`` 在 ``customerPresentationSigned=false`` 时
    画「测试数据·不可发客户」。所以这个布尔就是水印开关。
    """
    assert (not R.unsigned_policies()) is True, "签发后水印仍会被画出来"

    original = dict(R._SIGNATURES)
    try:
        pid = R.STATE_RULE_VERSION
        R._SIGNATURES[pid] = original[pid]._replace(signed=False)
        assert (not R.unsigned_policies()) is False, (
            "把一项拨回未签之后水印开关没跟着变 —— "
            "那说明水印不是由签发状态驱动的,撤水印这件事没有闸在守")
    finally:
        R._SIGNATURES.clear()
        R._SIGNATURES.update(original)


def test_customer_crop_now_yields_terminal_content():
    """签发后:对客裁剪真的产出内容(不再抛 PolicyNotSigned)。

    并且**只做减法** —— 业务数字一个不许被裁剪改动(§9.4)。
    """
    payload = {
        "cards": [{"key": "identity", "levelKey": "guarded",
                   "summary": {"valid": 7, "planned": 9}}],
        # 内部键:必须被剥掉
        "providerName": "内部供应商", "costPoints": 130,
        "nested": {"modelCode": "x", "keep": 1},
    }
    out = PROJ.crop_for_audience(payload, "customer")
    assert "providerName" not in out and "costPoints" not in out
    assert "modelCode" not in out["nested"], "嵌套层没剥(MET-22 要求任意层)"
    assert out["nested"]["keep"] == 1, "裁剪做了减法之外的事"
    # 业务数字逐值不变
    assert out["cards"][0]["summary"] == {"valid": 7, "planned": 9}


# ══════════════════════════════════════════════════════════════════════
# 2. Z-6 阈值政策 —— 判结构,不判数字
# ══════════════════════════════════════════════════════════════════════

def test_bands_are_structurally_sound_without_pinning_the_numbers():
    """判**性质**不判数值 —— 阈值是可调系数,钉数字的判据会在
    Owner 调旋钮那天变成假红,然后逼人改判据。

    性质四条:非空 / 严格单调递减 / 最低档从 0 起 / 不含 unknown。
    """
    bands = LP.bands()
    assert bands, "阈值表为空"
    mins = [b.min_rate for b in bands]
    assert mins == sorted(mins, reverse=True) and len(set(mins)) == len(mins), (
        f"下界不是严格单调递减:{mins}")
    assert float(bands[-1].min_rate) == 0.0, "最低一档没从 0 起 —— 有段出现率无档可落"
    for b in bands:
        assert b.level_key != "unknown", "unknown 不许由阈值算出来"
        assert b.level_key in R.level_keys(), f"未知档位 {b.level_key}"


def test_rate_to_level_is_monotone_and_total():
    """全覆盖 + 单调:[0,100] 每一点都有档,且出现率越高档位不下降。

    这条同样不钉具体数字 —— 它验的是"函数是良定义的"。
    """
    order = {b.level_key: i for i, b in enumerate(LP.bands())}   # 0 = 最好
    prev = None
    for tenth in range(0, 1001):
        r = tenth / 10.0
        lvl = LP.level_for_rate(r)
        assert lvl in order, f"出现率 {r} 落到了档位表外:{lvl}"
        if prev is not None:
            assert order[lvl] <= prev, (
                f"出现率涨到 {r} 反而落进更差的档 —— 单调性破了")
        prev = order[lvl]


def test_none_is_unknown_not_the_worst_band():
    """``None`` ⇒ unknown。**不是**最低档。

    "这一项没测到"与"你一次都没被提到"在客户眼里是两件完全不同的事。
    压成最低档就是把留白讲成坏消息。
    """
    assert LP.level_for_rate(None) == "unknown"
    assert LP.level_for_rate(0.0) != "unknown", (
        "真的算出 0% 却报 unknown —— 那是把坏消息藏起来,同样不诚实")


@pytest.mark.parametrize("bad", [-0.1, 100.1, 1e9])
def test_out_of_range_rate_is_refused_not_clamped(bad):
    """越界出现率**拒绝**,不夹紧。

    夹紧会把一个上游算错的数字变成一个看起来合法的等级。
    """
    with pytest.raises(LP.LevelPolicyError):
        LP.level_for_rate(bad)


@pytest.mark.parametrize("bands", [
    [],                                                     # 空
    [LP.Band("guarded", 50.0)],                             # 最低档不从 0 起
    [LP.Band("guarded", 50.0), LP.Band("priority_fix", 80.0)],   # 非单调
    [LP.Band("guarded", 50.0), LP.Band("guarded", 50.0)],   # 重复下界
    [LP.Band("unknown", 0.0)],                              # 含 unknown
])
def test_validator_refuses_broken_band_tables(bands):
    """校验器的判别力自证 —— 每种坏形态都必须被拒。

    没有这一条,``_validate`` 写成 ``return tuple(bands)`` 时上面所有
    结构判据仍然全绿(因为出厂默认本来就是对的)。
    """
    with pytest.raises(LP.LevelPolicyError):
        LP._validate(bands)


def test_the_dynamic_band_path_actually_works(pkgf_db, cur):
    """🔴 动态系数路径的**活性自证**:往真库插一行,bands() 必须真的变。

    这一条是那段读配置的代码存在的唯一证明。第一版写的是
    ``from db.system_settings_db import get_setting`` —— 那个模块全仓不存在,
    于是 import 永远失败、动态路径**恒空转**:后台怎么调都不生效,
    而没有任何东西会报错。本仓管这个叫「接了线但接线是坏的」。
    """
    with pkgf_db.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (
                        key VARCHAR(100) PRIMARY KEY, value TEXT,
                        value_type VARCHAR(20) DEFAULT 'string',
                        description TEXT)""")
        c.execute("DELETE FROM system_settings WHERE key=%s", (SETTINGS_KEY,))

    # 默认态:出厂默认
    assert LP.census(cur)["isDefault"] is True, "前提不成立(库里已有配置?)"
    default_boundary = LP.bands(cur)[0].min_rate

    # 后台调旋钮:把最高档下界改成一个**明显不同**的值
    moved = float(default_boundary) - 20.0
    assert 0.0 < moved < 100.0
    with pkgf_db.cursor() as c:
        c.execute(
            "INSERT INTO system_settings (key, value, value_type) VALUES (%s,%s,'json')",
            (SETTINGS_KEY, json.dumps([
                {"levelKey": "guarded", "minRate": moved},
                {"levelKey": "needs_strengthening", "minRate": moved - 20.0},
                {"levelKey": "priority_fix", "minRate": 0.0},
            ])))
    try:
        assert LP.bands(cur)[0].min_rate == moved, (
            "改了配置 bands() 没变 —— 动态路径是死的,阈值实际写死在代码里")
        assert LP.census(cur)["isDefault"] is False
        # 结果真的跟着变:落在新旧边界之间的那个出现率换了档
        between = (moved + float(default_boundary)) / 2.0
        assert LP.level_for_rate(between, cur) == "guarded", (
            f"出现率 {between} 在新阈值下应当是 guarded")
    finally:
        with pkgf_db.cursor() as c:
            c.execute("DELETE FROM system_settings WHERE key=%s", (SETTINGS_KEY,))
    # 撤回后必须回落默认(否则缓存把旧值粘住了)
    assert LP.bands(cur)[0].min_rate == default_boundary, (
        "删掉配置后没回落出厂默认 —— 有人加了缓存,动态可调就废了")


def test_broken_settings_fall_back_instead_of_breaking_the_report(pkgf_db, cur):
    """配置写坏 ⇒ 回落默认 + warning,**不**让整份报告出不来。"""
    with pkgf_db.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (
                        key VARCHAR(100) PRIMARY KEY, value TEXT,
                        value_type VARCHAR(20) DEFAULT 'string',
                        description TEXT)""")
        c.execute("DELETE FROM system_settings WHERE key=%s", (SETTINGS_KEY,))
        c.execute(
            "INSERT INTO system_settings (key, value, value_type) VALUES (%s,%s,'json')",
            (SETTINGS_KEY, '[{"levelKey": "guarded", "minRate": 50}]'))  # 最低档不从 0 起
    try:
        assert LP.census(cur)["isDefault"] is True, (
            "坏配置被当成合法阈值用了 —— 那会给客户发用坏阈值算出来的等级")
    finally:
        with pkgf_db.cursor() as c:
            c.execute("DELETE FROM system_settings WHERE key=%s", (SETTINGS_KEY,))


# ══════════════════════════════════════════════════════════════════════
# 3. MET-45 真内容(不是空签)
# ══════════════════════════════════════════════════════════════════════

def test_met45_registry_has_real_content_for_every_combination():
    """逐 mode/side/surface × 五卡 全覆盖,且 required 非空。

    分母 = ``MODE_SIDES × SURFACES × cards()`` **算出来的**,不手抄。
    """
    sections = R.professional_analysis_sections()
    expected = len(R.MODE_SIDES) * len(R.SURFACES) * len(R.cards())
    assert len(sections) == expected, (
        f"section 数 {len(sections)} != {expected} —— registry 有缺格")
    seen = set()
    for s in sections:
        key = (s.mode_side, s.surface, s.card_key)
        assert key not in seen, f"重复 section:{key}"
        seen.add(key)
        assert s.required_metrics, (
            f"{key} 的 required 是空的 —— 空签一份不存在的 registry 就是签一份谎")
        assert not (set(s.required_metrics) & set(s.optional_metrics)), (
            f"{key} 的 required 与 optional 有重叠 —— 一个指标不能既必需又可选")


def test_met45_content_is_derived_from_the_card_registry_not_handcopied():
    """内容必须与五卡→指标映射**同源**。

    手抄一份会在窗B 改卡时静默漂移 —— 本仓记过
    「手写分母漏掉的那一项不会让任何判据变红」。
    """
    for spec in R.cards():
        s = R.section_spec(mode_side="defensive", surface="web",
                           card_key=spec.key)
        assert tuple(s.required_metrics) + tuple(s.optional_metrics) \
            == tuple(spec.metric_keys), (
                f"卡 {spec.key} 的 MET-45 指标集合与 CardSpec 不同源:"
                f"{s.required_metrics + s.optional_metrics} vs {spec.metric_keys}")


def test_required_metrics_are_identical_across_surfaces():
    """同一 mode/side 下,三个 surface 的 required **逐值相同**。

    §9.4 / WP3 出口条件:同一 snapshot 的网页、内部页、PDF
    canonical business metric payload **逐值一致**,差异只在 audience 裁剪。
    required 逐 surface 不同就等于承认"网页和 PDF 算的不是同一份数"。
    """
    for card in R.cards():
        per_surface = {
            surface: R.section_spec(mode_side="defensive", surface=surface,
                                    card_key=card.key).required_metrics
            for surface in R.SURFACES
        }
        assert len(set(per_surface.values())) == 1, (
            f"卡 {card.key} 的 required 逐 surface 不同:{per_surface}")


def test_unknown_section_is_refused_not_defaulted():
    """未知组合**拒绝**,不兜底(MET-45「未知 definitionKey 拒绝投影」)。"""
    with pytest.raises(ValueError):
        R.section_spec(mode_side="nope", surface="web", card_key="identity")
    with pytest.raises(ValueError):
        R.section_spec(mode_side="defensive", surface="web", card_key="nope")
