# -*- coding: utf-8 -*-
"""返修 R5 / R6 / R8 / R9 判据 —— Codex 2026-08-17 八条里属包②的那几条。"""
from __future__ import annotations

SRV = open("server.py", encoding="utf-8").read()
MDB = open("db/monitoring_db.py", encoding="utf-8").read()
NOTIFY = open("services/monitor_billing_notify.py", encoding="utf-8").read()
EVENTS = open("services/notification_events.py", encoding="utf-8").read()
KT = open("frontend/src/pages/Monitoring/components/KeywordTable.tsx", encoding="utf-8").read()


def _strip_py_prose(src: str) -> str:
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


# ── R5 billing_mode 全路径 ─────────────────────────────────────────────
def test_R5_reuse_branch_reads_and_writes_billing_mode():
    """🔴 复用分支必须与创建分支同闸。

    Codex 坐实:原来只修 P0-4 owner、不碰 billing_mode ⇒ 管理员在**已有订阅**上
    选"记平台账",订阅仍是 brand_owner,照旧扣服务商的钱。
    引爆条件:任何一条已存在 active/paused 订阅上再点一次开通并选平台账。
    """
    i = SRV.index('if existing and existing.get("status") in ("active", "paused_low_balance"):')
    blk = _strip_py_prose(SRV[i:SRV.index("\n@app.", i)])
    # 🔴 打**真调用**不打名字出现:这个名字在函数内的 import 块里也会出现,
    #   只查"包含"时把写入整段拆掉照样绿(变异实测过一次)。
    assert "billing_mode=_billing_mode," in blk, "复用分支没有真的把 billing_mode 写下去"
    assert blk.count("update_subscription_billing_mode") >= 2,         "复用分支只 import 没调用(import 1 次 + 调用 1 次才算接上)"
    assert 'existing.get("billing_mode")' in blk, "复用分支不读 billing_mode(无从判断要不要改)"
    assert 'if _billing_mode == "platform":' in blk, "复用分支没有 platform 主体分支"


def test_R5_batch_endpoint_is_default_only_by_design():
    """批量端点**明示**只走默认,不是忘了接。"""
    i = SRV.index('@app.post("/api/monitoring/keyword/batch-enable")')
    blk = SRV[i:SRV.index("\n@app.", i + 10)]
    assert "批量一律 brand_owner" in blk, "批量端点没有明示付款方口径"
    # 🔴 判据只打**签名与真代码**,不打 docstring —— 我在 docstring 里解释了这条口径,
    #   在全文里找 billing_mode 会命中"讲代码的话"(本轮第四次同族)。
    q = blk.index('"""')
    sig = blk[:q]
    assert "billing_mode" not in sig, \
        "批量端点签名接受了 billing_mode —— 跨品牌批量时单一付款方在语义上不成立"
    body = blk[blk.index('"""', q + 3) + 3:]
    body_code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "billing_mode" not in body_code, "批量端点内部传了 billing_mode"


def test_R5_frontend_copy_uses_daily_model():
    """K1/K2 之后两种词同一计费模型,界面不许再说「130 算力/次」。"""
    assert "算力/次" not in KT, "界面仍在说每次扣 —— 用户会按「跑一次扣一次」理解,实际每天都在扣"
    assert "算力/天" in KT


# ── R6 通知 ────────────────────────────────────────────────────────────
def test_R6_N1_has_its_own_event_type():
    """N1 不许再复用 MONITORING_COMPLETED(标题会渲染成「效果监测已完成」,语义相反)。"""
    assert "MONITORING_ENABLED_BY_OTHER" in EVENTS, "没有独立事件类型"
    assert "有人为你的账户开通了效果监测" in EVENTS, "新事件类型没有标题模板"
    i = NOTIFY.index("def notify_monitor_enabled_by_other")
    blk = _strip_py_prose(NOTIFY[i:])
    assert "MONITORING_ENABLED_BY_OTHER" in blk
    assert "MONITORING_COMPLETED" not in blk, "N1 还在用「已完成」当开通通知"


def test_R6_N3_dedup_key_allows_second_pause():
    """🔴 outbox 是 ON CONFLICT DO NOTHING:business_id 固定就等于**第二次余额不足永不通知**。"""
    i = NOTIFY.index("def notify_monitor_paused_low_balance")
    blk = _strip_py_prose(NOTIFY[i:NOTIFY.index("def notify_monitor_enabled_by_other")])
    assert "strftime" in blk and "business_id" in blk, \
        "N3 的 dedup 键没有随时间变化 —— 同一条订阅第二次停就再也不会通知了"


def test_R6_clock_is_the_real_cron_hour_everywhere():
    """🔴 时钟统一到**真注册点**:api/scheduler.py 的 CronTrigger(hour=9)。

    此前存在三个钟(文案 02:30 / 交付书 03:00 / 真注册点 09:00)。
    以代码为准,文档和文案跟着改 —— 想改成别的时间是 Owner 决策,不是文案活。
    """
    sched = open("api/scheduler.py", encoding="utf-8").read()
    assert "CronTrigger(hour=9, minute=0, timezone=BEIJING_TZ)" in sched, \
        "真注册点变了 —— 所有文案要跟着重新对齐,不能各写各的"
    for name, src in (("notify", NOTIFY), ("KeywordTable", KT)):
        assert "02:30" not in src and "03:00" not in src, f"{name} 里还留着旧时钟"


# ── R8 init DDL 与迁移同口径 ───────────────────────────────────────────
def test_R8_init_ddl_has_billing_mode():
    """🔴 建表 DDL 缺列 ⇒ 全新库 UndefinedColumn。036 那颗雷接住了,037 这颗差点没接住。"""
    i = MDB.index("CREATE TABLE IF NOT EXISTS keyword_monitor_subscriptions")
    ddl = MDB[i:MDB.index('"""', i)]
    assert "billing_mode" in ddl, "建表 DDL 没有 billing_mode —— 全新库建出来就缺列"
    assert "keyword_source" in ddl, "keyword_source 也不该丢(036 的先例)"


def test_R8_init_ddl_matches_migration():
    """init DDL 与迁移 037 **同口径**:默认值与取值白名单都要一致。"""
    i = MDB.index("CREATE TABLE IF NOT EXISTS keyword_monitor_subscriptions")
    ddl = MDB[i:MDB.index('"""', i)]
    mig = open("scripts/migration_kms_billing_mode_2026_08_16.sql", encoding="utf-8").read()
    assert "billing_mode TEXT NOT NULL DEFAULT 'brand_owner'" in ddl, "init DDL 默认值口径不对"
    assert "billing_mode TEXT NOT NULL DEFAULT 'brand_owner'" in mig, "迁移默认值口径不对"
    assert "billing_mode IN ('brand_owner', 'platform')" in mig


# ── R9 结构化字段对齐真行为 ────────────────────────────────────────────
def test_R9_structured_field_matches_real_behavior():
    """只改文案不改字段 = 下游统计照旧是错的。"""
    i = SRV.index("AUTO_MONITORING_PAUSED_BY_SERVICE_PERIOD")
    blk = _strip_py_prose(SRV[max(0, i - 1500):i + 500])
    assert 'row["auto_monitoring_paused"] = False' in blk, \
        "结构化字段仍报 true,与真行为矛盾(dispatch 从不读 service_end_date)"
    assert 'service_calendar_expired' in blk, "没有给真实语义一个直说的字段名"
    assert '"auto_monitoring_paused_count": 0' in SRV, "下游计数没同步"
    assert '"service_calendar_expired_count"' in SRV, "下游计数没有新口径"
