"""套餐档位码 flagship / premium · 判据锁(WO §8 快修 2026-08-08)

缺陷:「档位 → 目标出现率」这张表在仓里有三份拷贝,其中两份只认 legacy 的 `premium`,
而档位码 SSOT(`api/selection_api.TIER_CONFIG`)的规范码是 `flagship` ——
于是 `tier='flagship'` 的报价**静默按标准版 65%(甚至报告链的 entry 50%)算**。

🔴 修法**不是**把 premium 改名成 flagship:生产实测(2026-08-08 只读取证)
`quotes.tier` 里 `premium` 还剩 1 条**且正在被监测**,改名会把那家从 75% 打回 65%。
所以是「单点 + legacy 别名,两个码都认」。

每条【必须命中】配【必须不命中】。变异见
`tests/mutation_runner_tier_target_2026_08_08.py`。
"""
from __future__ import annotations

import inspect
import re

import pytest

from services.monitoring_tier_target import (
    DEFAULT_TIER_CODE,
    LEGACY_TIER_ALIASES,
    canonical_tier_code,
    is_known_tier_code,
    tier_target,
    tier_target_rates,
)


# ══════════════════════════════════════════════════════════════════════════
# §1 单点本体
# ══════════════════════════════════════════════════════════════════════════

def test_flagship_is_the_canonical_code_and_gets_75():
    """【必须命中】这就是工单要修的那一格:flagship 不再掉进 standard。"""
    assert tier_target("flagship") == {
        "tier": "flagship", "tier_name": "旗舰版", "target_rate": 75,
    }


def test_legacy_premium_still_gets_75():
    """【必须命中 · 这条比上面那条更容易被改坏】legacy premium 不许被丢掉。

    生产实测:`quotes.tier='premium'` 还剩 1 条,而且**正在被监测**。
    把 premium 直接改名成 flagship(工单字面写法)会让这家从 75% 掉回 65%。
    """
    got = tier_target("premium")
    assert got["target_rate"] == 75
    assert got["tier_name"] == "旗舰版"
    assert got["tier"] == "flagship", "对外收敛到规范码,不再往回写 legacy 码"


@pytest.mark.parametrize("raw, rate", [
    ("entry", 50), ("standard", 65), ("flagship", 75), ("premium", 75),
    ("  Flagship ", 75), ("PREMIUM", 75),
])
def test_known_codes_resolve(raw, rate):
    """【必须命中】大小写/空白/别名都收敛得到。"""
    assert tier_target(raw)["target_rate"] == rate
    assert is_known_tier_code(raw) is True


@pytest.mark.parametrize("raw", ["", None, "bogus", "pro", "旗舰版"])
def test_unknown_codes_fall_back_to_standard_and_are_reported_unknown(raw):
    """【必须不命中】认不出的码回落 standard,**并且 `is_known_tier_code` 说不认得**。

    第二个断言是关键:`server.py` 报告链靠它保住"认不出就回落 entry"的既有语义
    (CTO-15.23 防自动升档)。只断言回落值的话,`is_known` 写成恒 True 也能满分。
    """
    assert tier_target(raw)["tier"] == DEFAULT_TIER_CODE
    assert is_known_tier_code(raw) is False


def test_rates_are_derived_from_the_ssot_not_hand_copied():
    """【必须命中】数字来自 `TIER_CONFIG.ai_probability`,不是本模块手抄的第四份。

    判据:改 SSOT 的值,本模块跟着变。不跟着变 = 又抄了一份。
    """
    from api import selection_api

    original = selection_api.TIER_CONFIG["flagship"]["ai_probability"]
    try:
        selection_api.TIER_CONFIG["flagship"]["ai_probability"] = "88%"
        assert tier_target("flagship")["target_rate"] == 88
        assert tier_target("premium")["target_rate"] == 88, "别名也必须跟着 SSOT 走"
    finally:
        selection_api.TIER_CONFIG["flagship"]["ai_probability"] = original
    assert tier_target("flagship")["target_rate"] == 75, "还原后必须回到 75"


def test_alias_table_only_maps_legacy_to_canonical():
    """【必须不命中】别名表只许老码 → 新码,不许反向(反向 = 往库里写老码)。"""
    from api.selection_api import TIER_CONFIG

    for legacy, canonical in LEGACY_TIER_ALIASES.items():
        assert canonical in TIER_CONFIG, f"{canonical} 必须是 SSOT 里的规范码"
        assert legacy not in TIER_CONFIG, f"{legacy} 是 legacy 码,不该出现在 SSOT 里"


def test_rate_table_covers_canonical_and_legacy():
    """【必须命中】整表口径:规范码 + 别名都在,数值一致。"""
    table = tier_target_rates()
    assert table["flagship"] == table["premium"] == 75
    assert table["standard"] == 65 and table["entry"] == 50


# ══════════════════════════════════════════════════════════════════════════
# §2 接线 —— 三份拷贝必须都没了
# ══════════════════════════════════════════════════════════════════════════

def _src(path: str) -> str:
    """读源码并剥注释 —— 否则本包自己写的说明文字会让判据恒真。"""
    with open(path, encoding="utf-8") as handle:
        raw = handle.read()
    return "\n".join(l for l in raw.split("\n") if not l.strip().startswith("#"))


def test_monitoring_api_no_longer_keeps_its_own_table():
    """【必须命中 · 打在接线上】`api/monitoring_api.py` 不再有本地 TIER_TARGET 字典。"""
    code = _src("api/monitoring_api.py")
    assert "TIER_TARGET = {" not in code, "本地拷贝必须删掉,不是留着不用"
    assert "monitoring_tier_target import" in code, "必须真的接上单点"


def test_server_no_longer_keeps_its_own_table():
    """【必须命中 · 打在接线上】`server.py` 那份内联拷贝也没了。"""
    code = _src("server.py")
    assert "TIER_TARGET = {" not in code
    assert code.count("monitoring_tier_target import") >= 2, (
        "报告链与档位更新端点两处都要接"
    )


def test_no_hand_copied_rate_triplet_survives_in_the_wired_files():
    """【必须不命中】被接线的两个文件里不许再出现手抄的 50/65/75 三元组。

    元判据:这个工单的病因就是"同一张表抄了三份",所以判据要盯**形状**,
    不是盯某一个字符串。
    """
    pattern = re.compile(r'"?target_rate"?\s*[:=]\s*75')
    for path in ("api/monitoring_api.py", "server.py"):
        code = _src(path)
        assert not pattern.search(code), f"{path} 里还有手抄的 target_rate 75"


def test_update_tier_endpoint_accepts_flagship():
    """【必须命中】档位更新端点不再拒绝 SSOT 自己的规范码。

    原白名单 `("entry","standard","premium")` 会把 flagship 判成"无效套餐"。
    """
    code = _src("server.py")
    m = re.search(r'if tier not in \(([^)]*)\):', code)
    assert m, "找不到档位白名单 —— 判据锚点失效,先修判据"
    allowed = m.group(1)
    assert '"flagship"' in allowed, "必须放行规范码 flagship"
    assert '"premium"' in allowed, (
        "🔴 也必须继续放行 legacy premium —— 前端 Monitoring/index.tsx 的下拉框现在送的就是它"
    )


def test_report_chain_keeps_its_entry_fallback():
    """【必须不命中】报告链「认不出的码回落 entry」这条既有语义**一个字不许改**。

    CTO-15.23 2026-05-06 P1:fallback standard 会无脑升档、误导客户。
    我把这里改成走单点,但单点默认回落 standard —— 所以必须先问 `is_known_tier_code`。
    """
    code = _src("server.py")
    assert "is_known_tier_code" in code, "必须用'认不认得出'来保住 entry 兜底"
    assert re.search(r'_tier_known\(tier_code\)\s+else\s+"entry"', code), (
        "认不出时必须回落 entry,不是 standard"
    )


def test_monitoring_db_table_stays_consistent_with_the_ssot():
    """【必须命中 · 元判据】`db/monitoring_db._TIER_TARGET_MAP` 是第三份拷贝
    (本包**没有**改它,它本来就两个码都有)。这条锁保证它不会跟 SSOT 走散。

    谁哪天把 SSOT 的 75% 改了却忘了改它,这条转红。
    """
    from db.monitoring_db import _TIER_TARGET_MAP

    ssot = tier_target_rates()
    for code, rate in _TIER_TARGET_MAP.items():
        assert code in ssot, f"{code} 不在 SSOT 口径里(多出来的第四种码)"
        assert int(rate) == ssot[code], f"{code}: 本地 {rate} vs SSOT {ssot[code]}"


def test_agent_tier_premium_is_a_different_dimension_and_untouched():
    """【必须不命中 · 反向对照】`config/v3_3_1_flags.py` 里的 `premium` 是**代理等级**,
    与报价档位是两个维度,本包一个字都不许动它。

    没有这条,"全仓把 premium 换成 flagship"这种粗暴实现会拿满分,
    而那会把代理转换配额算错。
    """
    import config.v3_3_1_flags as flags

    src = inspect.getsource(flags.get_conversion_quota_yuan)
    assert 'tier == "premium"' in src, "代理等级那条 premium 分支必须原样还在"
    assert "flagship" not in src, "代理等级维度里不该出现 flagship"
