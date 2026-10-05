"""判据③ · 核实器**接线自证** + 出站防线自证。

两件本仓反复栽过的事,各配一条锁:

  ① **死函数**:整包做完、锁全绿、变异全杀,但那个函数**零调用方** ——
     "写了" 不等于 "接上了"。所以这里直接查:cron 注没注册、tick 调不调 sweep、
     写入方有没有真的落 pending。
  ② **出站防线**:新开一条服务端主动出站的路径,必须走既有那套逐跳 SSRF 校验,
     不能自己另拉一个 requests.get —— 否则内网就多了一个盲打面。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# ① 接线
# ---------------------------------------------------------------------------

def test_cron_registers_the_verification_job():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'id="publication-url-verification"' in src, "cron 没注册核实 job = 队列永远没人跑"
    assert "_publication_url_verification_tick" in src


def test_tick_actually_calls_the_sweep():
    """注册了 job 不代表它干活 —— tick 里必须真调 sweep。"""
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    tick = re.search(r"def _publication_url_verification_tick\(\):(.*?)\ndef ", src, re.S)
    assert tick, "找不到 tick 函数"
    assert "sweep_pending_public_urls" in tick.group(1)


def test_sweep_only_picks_pending():
    """清扫器的取件谓词必须是 pending —— 取错态就是空转(本仓栽过'新链调旧执行器')。"""
    src = (ROOT / "services" / "publication_url_verifier.py").read_text(encoding="utf-8")
    sweep = re.search(r"def sweep_pending_public_urls\(.*?\n(.*?)\Z", src, re.S).group(1)
    assert "public_url_verification_state = %s" in sweep
    assert "(STATE_PENDING, int(limit))" in sweep


# [WO_273 · 2026-09-23 肯定式退役] 原格 `test_admin_queue_endpoint_exposes_needs_action`:
#   钉住插件后端的管理员核实队列端点把 needs_action 露出来(「降级为待核实」不许悄悄丢)。
#   该端点随插件后端**整体删除**;自报的唯一写入方同时删除,不会再有**新**行进入 needs_action。
#   接替:
#     · 「不许再冒出自报写入方」→ test_publish_records_writer_census.py 的 `test_declared_writers_match_repo`;
#     · 「队列端点不许悄悄回来」→ tests/extension_retirement_2026_09_23 的路由缺席锁。
#   ⚠️ 未接替的一件(交付单已列):核实器 cron 仍在扫存量 pending 行,被扫成 needs_action 的
#      存量行从此没有管理员队列可看。


# ---------------------------------------------------------------------------
# ② 出站防线
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a",            # 非 https
    "https://127.0.0.1/a",           # 环回
    "https://10.0.0.1/a",            # 私网
    "https://169.254.169.254/latest",  # 云元数据(经典 SSRF 目标)
    "https://[::1]/a",               # v6 环回
    "https://example.com:8443/a",    # 非 443 端口
])
def test_default_fetcher_refuses_unsafe_targets(url):
    """自报的 URL 是**攻击者可控**输入 —— 内网/元数据/非默认端口一律拨不出去。

    这条不出网:所有拒绝都发生在 DNS/IP 校验阶段,拨号前就抛。
    """
    from services.publication_url_verifier import _default_fetcher

    with pytest.raises(ValueError):
        _default_fetcher(url)


def test_verifier_does_not_open_a_second_outbound_path():
    """核实器只许经 `services.safe_https_probe` 出站,不许自己拉 requests/urllib。"""
    src = (ROOT / "services" / "publication_url_verifier.py").read_text(encoding="utf-8")
    for forbidden in ("import requests", "import urllib.request", "from urllib.request",
                      "httpx", "aiohttp"):
        assert forbidden not in src, f"核实器另开了一条出站路径:{forbidden}"
    assert "from services.safe_https_probe import" in src


def test_extracted_transport_keeps_referral_probe_working():
    """SSRF 传输层是**纯搬家**:白标那边 patch 的还是 `api.referral_api` 上的同名全局。

    搬家如果搬成了「referral 里没这个名字了」,三份白标锁会红 —— 这里再顶一层,
    确保 patch 目标本身还在(名字被搬没了,monkeypatch 会静默创建新属性而不报错)。
    """
    import api.referral_api as ra

    for name in ("_assert_public_probe_host", "_assert_safe_probe_hop",
                 "_PinnedIPHTTPSConnection", "_pinned_https_roundtrip"):
        assert hasattr(ra, name), f"搬家把 {name} 从 referral_api 搬没了"


# ---------------------------------------------------------------------------
# ③ 迁移接线(prestart 只按 manifest 跑,**不 glob 目录**)
# ---------------------------------------------------------------------------

def test_migration_is_registered_in_manifest():
    """迁移不进 manifest = 上线后**永远不会跑**(本仓 2026-08-02 创作中心包实例)。

    而本包的四处闸都显式引用新列 → 漏跑不是"少个功能",是那几条 SQL 当场
    UndefinedColumn。这条锁比"迁移写得对不对"更早生效。
    """
    from db.migration_manifest import MIGRATIONS

    rel = "scripts/migration_publish_records_url_verification_2026_08_19.sql"
    assert rel in MIGRATIONS, "本包迁移没进 manifest —— 部署后不会执行"
    assert (ROOT / rel).exists(), "manifest 登记了但文件不在"


def test_migration_is_idempotent_by_construction():
    """重放安全的三处写法逐条在位(prestart 每次部署无条件重放全部迁移)。"""
    sql = (ROOT / "scripts" / "migration_publish_records_url_verification_2026_08_19.sql"
           ).read_text(encoding="utf-8")
    # 🔴 R3 改断言:原来写死"列数 == 5"。数字每加一列就要改一次,而它想守的
    #    根本不是列数,是**每一条加列都带 IF NOT EXISTS**。改成结构等式后,
    #    以后再加列不用动这条锁,而"忘了写 IF NOT EXISTS"当场红。
    assert sql.count("ADD COLUMN") == sql.count("ADD COLUMN IF NOT EXISTS")
    assert sql.count("ADD COLUMN IF NOT EXISTS") >= 5
    # 同理:每条 ADD CONSTRAINT 都得先 DROP IF EXISTS。
    # R1 已跑过的库要能被**改定义**,"不存在才加"会让它永远停在旧闭集上
    # (定义漂移且无声)。审计表那条走 pg_constraint 存在性判断,单独计。
    #
    # 🔴 R4 改判据:原来是**算数式**(ADD 的条数 - pg_constraint 出现次数 == DROP 条数)。
    #    它在本轮当场误报 —— 我在迁移里写了一行**注释**提到 "ADD CONSTRAINT",
    #    计数就多了一条(本仓的老坑:裸串计数会被散文触发)。
    #    改成**按名字比集合**:先剥行注释再取名,顺带比算数强 ——
    #    算数配不出"DROP 的名字和 ADD 的名字对不上"这种错,集合配得出。
    code = re.sub(r"--[^\n]*", "", sql)
    added = set(re.findall(r"ADD\s+CONSTRAINT\s+(\w+)", code, re.IGNORECASE))
    dropped = set(re.findall(r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+(\w+)", code,
                             re.IGNORECASE))
    # 审计表那条由 pg_constraint 存在性判断守着重放,不需要 DROP
    guarded_by_lookup = {"prve_human_attestation_needs_evidence"}
    assert added - guarded_by_lookup == dropped, (
        f"这些约束加了却没有配套的 DROP IF EXISTS(重放会 42710 或定义漂移):"
        f"{sorted(added - guarded_by_lookup - dropped)};"
        f"这些 DROP 了却没加回来:{sorted(dropped - added)}")
    assert len(dropped) >= 3
    # 分母自证:提取器确实取到了名字(而不是因为正则没命中而两边都空)
    assert "publish_records_verified_requires_authority_source" in added
    # [R3 §①] 触发器同样要重放安全:CREATE OR REPLACE + DROP IF EXISTS 再建。
    assert "CREATE OR REPLACE FUNCTION publish_records_verification_is_monotonic" in sql
    assert "DROP TRIGGER IF EXISTS trg_publish_records_verification_monotonic" in sql
    assert "FROM pg_constraint" in sql, "审计表 CHECK 没走存在性判断,重放会 42710"
    assert "CREATE TABLE IF NOT EXISTS publish_record_verification_events" in sql
    # [索引守卫加固二单 2026-08-24] 建索引已从 ``CREATE INDEX IF NOT EXISTS``
    # 换成**表绑定守卫**(按名判存不绑表 ⇒ 同名索引长在别的表上时静默跳过)。
    # 这里验的还是同一件事:建索引语句重放安全,而且现在还绑了表。
    assert "-- @index-guard" in sql, "建索引语句没有表绑定守卫锚"
    assert "i.indrelid = to_regclass(" in sql, "索引守卫没绑 indrelid"
    # 回填自限:重放不会把已经处理过的行再翻一遍
    assert "AND public_url_verification_state = 'unverified'" in sql
