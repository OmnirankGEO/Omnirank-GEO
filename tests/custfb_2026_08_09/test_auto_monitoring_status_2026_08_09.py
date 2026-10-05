"""客户反馈⑥ · 服务期横幅说谎 —— 真实排班资格锁。

结论先说清楚(2026-08-09 生产快照只读取证 + 源码双证):

  自动监测有**两条互斥的排班链**,日历到期只卡其中一条 ——
    链 A(逐词订阅 `keyword_monitor_subscriptions` / `list_active_subscriptions`)
        WHERE 里没有 service_end_date,停机条件是 compliant_days >= service_days。
        实测:quote 287(雅栖)service_end_date=2026-06-10 已过期 60 天,
        3 条订阅全 active,`last_charged_at` = 2026-08-08 —— 前一天还在扣费跑。
        全库同形态(日历过期 + 有 active 订阅)共 4 张报价。
    链 B(报价级轮换 `get_monitoring_enabled_clients`)
        WHERE 里明写 `service_end_date >= CURRENT_DATE` → 日历到期确实停。

  所以前端那句写死的"(已到期 N 天 · 自动监测已暂停)"对链 A 的客户是**假的**。

锁:
  锁1-6  `resolve_auto_monitoring_status` 真值表(含雅栖那一格)
  锁7    源码锁:横幅不再写死"自动监测已暂停"(改由后端结论驱动)
  锁8    源码锁:ActionCards 不再把达标配额叫"有效期"
  锁9    接线锁:后端响应里真有 `auto_monitoring` 这个键(AST 判定)
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TODAY = date(2026, 8, 9)
MONITORING_INDEX = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "index.tsx"
ACTION_CARDS = ROOT / "frontend" / "src" / "pages" / "Monitoring" / "components" / "ActionCards.tsx"
MONITORING_API = ROOT / "api" / "monitoring_api.py"


def _code_lines(path: Path) -> list:
    """只取**代码行**,块注释整段剔掉。

    🔴 单行正则(`^\s*(//|/\*|\*)`)不够:`{/* ... */}` 多行块的**续行**既不以 //
      也不以 * 开头,本次修复自己写的解释性注释就长在那种续行上 ——
      第一版锁因此把自己打成红的(自伤,当场证伪后改成块感知)。
    """
    out = []
    in_block = False
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if line.startswith("/*") or line.startswith("{/*"):
            if "*/" not in line:
                in_block = True
            continue
        if line.startswith("//") or line.startswith("*"):
            continue
        out.append((i, raw))
    return out


def _status(**kw):
    from services.service_period import resolve_auto_monitoring_status

    kw.setdefault("today", TODAY)
    return resolve_auto_monitoring_status(**kw)


# ── 锁1 · 雅栖那一格:过期 + 有订阅 = 仍在跑 ───────────────────────────────
def test_expired_but_subscribed_is_still_running():
    got = _status(monitoring_enabled=True, service_end=date(2026, 6, 10),
                  active_subscription_count=3)
    assert got["active"] is True
    assert got["code"] == "compliance_driven"
    assert got["calendar_expired"] is True
    assert "暂停" not in got["message"], "过期但订阅 active 的品牌不许再断言'已暂停'"


# ── 锁2 · 反向对照:过期 + 无订阅 = 真的停了 ───────────────────────────────
def test_expired_without_subscription_is_paused():
    got = _status(monitoring_enabled=True, service_end=date(2026, 6, 10),
                  active_subscription_count=0)
    assert got["active"] is False
    assert got["code"] == "rotation_blocked_calendar"
    assert "暂停" in got["message"], "链 B 到期确实停 —— 这一格必须还敢说'暂停'"


# ── 锁3-6 · 其余各格 ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "enabled,end,subs,expect_active,expect_code",
    [
        (False, date(2026, 12, 1), 0, False, "monitoring_disabled"),   # 开关没开 + 无订阅 → 链 B 停
        (True, None, 0, False, "service_period_not_set"),               # 没服务期 → 排不上班
        (True, date(2026, 12, 1), 0, True, "rotation_active"),          # 链 B 未过期 → 在跑
        (True, date(2026, 12, 1), 2, True, "compliance_driven"),        # 链 A 未过期 → 在跑
    ],
)
def test_truth_table(enabled, end, subs, expect_active, expect_code):
    got = _status(monitoring_enabled=enabled, service_end=end, active_subscription_count=subs)
    assert got["active"] is expect_active
    assert got["code"] == expect_code


# ── 锁11 · [Review P1-1] 报价级开关**管不着**逐词订阅链 ─────────────────────
@pytest.mark.parametrize("end", [date(2026, 12, 1), date(2026, 6, 10), None])
def test_quote_switch_does_not_override_real_subscriptions(end):
    """`quotes.monitoring_enabled=false` + 有可排班订阅 → 必须仍判"运行中"。

    🔴 判据来自真实调度器的 WHERE,不是我的直觉:
      链 A `db.monitoring_db.list_active_subscriptions` 的 WHERE 里
      **没有 `q.monitoring_enabled` 这一列**;它只出现在链 B
      `get_monitoring_enabled_clients`(`AND q.monitoring_enabled = TRUE`)。
    🔴 咬人实证(生产快照 2026-08-09):quote 366(浙江岱林生物)
      `monitoring_enabled=false`、真实可排班订阅 **2 条** —— 旧口径会显示
      "自动监测开关没开",而 daily job 照跑照扣费。全库这种报价 1 张。
      服务期日历状态(未过期/已过期/没设)都不改变这个结论,所以三格都锁。
    """
    got = _status(monitoring_enabled=False, service_end=end, active_subscription_count=2)
    assert got["active"] is True, "报价级开关不许否定逐词订阅这条链"
    assert got["code"] == "compliance_driven"
    assert "没开" not in got["message"]


def test_quote_switch_still_governs_the_rotation_chain():
    """反向对照:**没有**可排班订阅时,报价级开关必须仍然说了算(否则上一条就是恒真)。"""
    got = _status(monitoring_enabled=False, service_end=date(2026, 12, 1),
                  active_subscription_count=0)
    assert got["active"] is False
    assert got["code"] == "monitoring_disabled"


# ── 锁7 · 横幅不再写死因果断言 ────────────────────────────────────────────
def test_banner_no_longer_hardcodes_paused():
    """源码里不许再出现"写死在 JSX 文本里的『自动监测已暂停』"。

    🔴 判据只扫**代码行**,注释行剔掉 —— 否则本次修复自己写的那段解释性注释
      会把这条锁打成恒红(这类自伤在本仓有过前科)。
    """
    offenders = [(i, ln.strip()) for i, ln in _code_lines(MONITORING_INDEX)
                 if "自动监测已暂停" in ln]
    assert not offenders, f"横幅仍写死「自动监测已暂停」:{offenders}"


def test_banner_reads_backend_conclusion():
    """正向对照:横幅确实在读后端下发的 auto_monitoring(不是把文案删了了事)。"""
    src = MONITORING_INDEX.read_text(encoding="utf-8")
    assert "setAutoMonitoring(data.auto_monitoring" in src
    assert "autoMonitoring ? ` · ${autoMonitoring.message}`" in src


# ── 锁8 · "有效期"不再用来描述达标配额 ───────────────────────────────────
def test_action_cards_no_longer_calls_quota_a_validity_period():
    offenders = [(i, ln.strip()) for i, ln in _code_lines(ACTION_CARDS)
                 if "监测/门户有效期" in ln]
    assert not offenders, f"仍把累计达标配额叫「有效期」:{offenders}"
    assert "累计达标配额" in ACTION_CARDS.read_text(encoding="utf-8")


# ── 锁12 · [Review P1-1] 过期说明(HelpHint)不许再做因果断言 ─────────────
def test_help_hint_no_longer_claims_expiry_stops_scheduling():
    """横幅改了、tooltip 没改 = 假话换了个地方住。

    旧文案:"自动监测只在服务期内排班,到期就停排班" —— 对逐词订阅那条链是假的。
    """
    offenders = [(i, ln.strip()) for i, ln in _code_lines(MONITORING_INDEX)
                 if "到期就停排班" in ln]
    assert not offenders, f"过期说明仍断言「到期就停排班」:{offenders}"
    src = MONITORING_INDEX.read_text(encoding="utf-8")
    assert "已建监测订阅的词" in src, "说明里必须点明两条链的区别,不能只把假话删掉了事"


# ── 锁9 · 接线:响应字典里真有这个键 ──────────────────────────────────────
def test_response_dict_actually_carries_auto_monitoring():
    """AST 判定:`auto_monitoring` 必须是某个 return 的字典字面量里的**键**。

    🔴 不用 grep:变量名在注释/局部赋值里出现都能骗过字符串搜索,
      而"下发给前端"只有出现在返回字典的键位上才算数
      (本仓有过"新增下发字段被前端 hook 丢掉、11 条后端锁全绿"的前科)。
    """
    tree = ast.parse(MONITORING_API.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and key.value == "auto_monitoring":
                    found = True
    assert found, "monitoring_api 的返回字典里没有 auto_monitoring 键 —— 前端拿不到就是没接线"


# ── 锁10 · 订阅计数口径必须与真实排班链同形 ──────────────────────────────
def test_subscription_count_matches_scheduler_conditions():
    """只数 `s.status='active'` 会高估 —— 那样横幅还会继续说假话,只是换了个方向。

    🔴 2026-08-09 生产快照实测:晨光富士 quote 286 按"只看订阅状态"报 5 条,
      而真实可排班是 **0** 条(5 个核心词全被代理手动关了自动监测:
      `is_monitored=false` / `monitoring_status='archived'`,KMS 行却仍 active)。
      全库这种报价 1 张 —— 一张也不能放过,它会让"仍在跑"变成新的假话。
    判据打在**这条 SQL 的四个必要条件**上,并要求服务锚走共享 SSOT 函数
    (不许在这里手抄一份 quotes 状态判断 = 第二套逻辑)。
    """
    src = MONITORING_API.read_text(encoding="utf-8")
    block_start = src.index("SELECT COUNT(*) AS cnt")
    block = src[block_start:block_start + 1400]
    for needed in (
        "JOIN confirmed_keywords",
        "ck.is_monitored = TRUE",
        "COALESCE(ck.monitoring_status, 'active') = 'active'",
        "keyword_compliance_log",
        "< q.service_days",
    ):
        assert needed in block, f"订阅计数少了必要条件:{needed}"
    assert "quote_service_anchor_condition_sql" in src, (
        "服务锚判断没走共享 SSOT —— 手抄一份就是第二套口径"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
