"""[资金 cron 幂等批] 蓝绿双跑/重跑下 3 个资金 cron 不双扣/不双发 · 真行为级 + 静态守护

预存 bug(非本批引入·Fable cron 审计):redis scheduler 锁是第一道防线,但锁失效/蓝绿切换窗口会双跑
→ 数据层须 CAS claim 做第二道防线("锁层非唯一防线")。
#1 service_fee_t7_bonus_settle:settle CAS 后无视 rowcount 直接加 bonus → 双发
#2 _try_auto_renew:扣 paid + 延 expires_at 无本期 claim → 双扣双延
#3 managed tick / pending_review:无 claim → 双扣监测/写作 + 媒体双投
"""
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ============ #1 service_fee_t7_bonus_settle ============

class _C1Cur:
    def __init__(self):
        self.sqls = []
        self._rowcount = 1
        self._rows = [{"id": 1, "referrer_id": 7, "bonus_points": 100, "recharge_order_id": None}]
    @property
    def rowcount(self):
        return self._rowcount
    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sqls.append(s)
        # 模拟:settle CAS 命中 0 行(本行已被另一实例/重跑 settle)
        self._rowcount = 0 if "SET status='settled'" in s else 1
    def fetchall(self):
        return self._rows
    def fetchone(self):
        return None
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _C1Conn:
    def __init__(self, cur): self._cur = cur
    def cursor(self): return self._cur
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_c1_double_run_settle_zero_rowcount_no_double_credit(monkeypatch):
    import services.service_fee_cron as sfc
    cur = _C1Cur()
    monkeypatch.setattr(sfc, "is_v3_3_1_enabled", lambda: True)
    monkeypatch.setattr(sfc, "get_db", lambda: _C1Conn(cur))
    res = asyncio.run(sfc.service_fee_t7_bonus_settle())
    # settle CAS 命中 0 行 → 必须在加 bonus 前 continue
    assert not any("user_wallets SET bonus_points" in s for s in cur.sqls), \
        "settle CAS 0 行仍加 bonus → 双发未修"
    assert not any("INSERT INTO point_transactions" in s for s in cur.sqls), "不应记入账流水"
    assert res["settled"] == 0


def test_c1_static_guard():
    src = (ROOT / "services" / "service_fee_cron.py").read_text(encoding="utf-8")
    assert "if cur.rowcount == 0:\n                    continue" in src, \
        "settle CAS 后须判 rowcount==0 → continue(入账前)"


# ============ #2 _try_auto_renew ============

class _C2Cur:
    def __init__(self): self.sqls = []
    def execute(self, sql, params=None): self.sqls.append(" ".join(sql.split()))
    def fetchone(self): return None  # claim CAS → 0 行(本期已被另一实例续)


class _C2Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = True
    def cursor(self): return self._cur
    def rollback(self): pass
    def commit(self): pass
    def close(self): pass


# ============ #3 managed tick / pending_review claim ============

class _C3Cur:
    def __init__(self, hit): self._hit = hit; self.sqls = []
    def execute(self, sql, params=None): self.sqls.append(" ".join(sql.split()))
    def fetchone(self): return {"id": 1} if self._hit else None


class _C3Conn:
    def __init__(self, cur): self._cur = cur
    def cursor(self): return self._cur
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_c3_claim_campaign_tick_cas(monkeypatch):
    import db.managed_campaign_db as mdb
    monkeypatch.setattr(mdb, "get_db", lambda: _C3Conn(_C3Cur(hit=True)))
    assert mdb.claim_campaign_tick(1) is True   # CAS 命中 → 占用成功
    monkeypatch.setattr(mdb, "get_db", lambda: _C3Conn(_C3Cur(hit=False)))
    assert mdb.claim_campaign_tick(1) is False  # 0 行 → 已被并发占用 → 跳过


def test_c3_claim_pending_review_cas(monkeypatch):
    import db.managed_campaign_db as mdb
    monkeypatch.setattr(mdb, "get_db", lambda: _C3Conn(_C3Cur(hit=True)))
    assert mdb.claim_pending_review_autopublish(1) is True
    monkeypatch.setattr(mdb, "get_db", lambda: _C3Conn(_C3Cur(hit=False)))
    assert mdb.claim_pending_review_autopublish(1) is False


def test_c3_tick_wires_claims():
    src = (ROOT / "tools" / "geo_managed" / "campaign_tick.py").read_text(encoding="utf-8")
    assert "if not claim_campaign_tick(cid):" in src, "主 tick 须先 claim"
    assert "if not claim_pending_review_autopublish(rid):" in src, "待审自动发布须先 claim"


def test_c3_migration_present():
    mig = ROOT / "scripts" / "migration_fund_cron_idempotency_2026_06_10.sql"
    assert mig.exists()
    txt = mig.read_text(encoding="utf-8")
    assert "last_tick_claimed_at" in txt and "autopublish_claimed_at" in txt
