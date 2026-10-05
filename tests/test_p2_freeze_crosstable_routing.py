"""[BUG-P2] V3.5 冻结链 commit/release 跨表错路由 → sweeper 白退 · 静态守护 + 路由纯函数测试

根因:commit_freeze/release_freeze 靠 _is_v35_customer(客户【当前】是否 V3.5)判表。freeze 在
legacy point_freezes 创建后,客户运行期开通 V3.5 → _is_v35_customer 翻 True → 路由到
customer_credit_freezes 找不到 → success=False → frozen_points 永不结算 → sweeper 12h 误当
zombie release(任务成功仍退钱=白退)。
修:_route_freeze_table 按冻结【实际所在表】路由 — 先查 point_freezes(命中即 legacy),未命中再查
customer_credit_freezes;freeze_id 跨表撞号靠 user_id 消歧。
prod 实证(2026-06-10 SSH):customer_credit_freezes 0 行 → bug latent · 纯预防无回收。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_routing_uses_actual_table_not_current_status():
    src = (ROOT / "middleware" / "billing.py").read_text(encoding="utf-8")
    assert "def _route_freeze_table" in src, "须有按实际所在表路由的 helper"
    # commit_freeze / release_freeze 不再用 _is_v35_customer 判路由(改用 _route_freeze_table)
    assert src.count("_route = _route_freeze_table(cursor, freeze_id=freeze_id, task_ref=task_ref,") == 2, \
        "commit_freeze + release_freeze 两处都须按 _route_freeze_table 路由"
    assert src.count("user_id=user_id, freeze_table=freeze_table)") == 2, "[A0] 两处都须透传 freeze_table"
    assert src.count('if _route == "v35":') == 2, "两处都须按路由结果走 v35 分支"
    assert src.count('if _route == "ambiguous":') == 2, "两处都须对 ambiguous fail-closed"
    # 旧的 "user_id is not None and _is_v35_customer" 路由判定不应再出现在 commit/release
    assert "user_id is not None and _is_v35_customer(cursor, user_id):" not in src, \
        "仍残留按当前客户状态路由冻结 → 漂移白退未修"


class _FakeCursor:
    """解释 _route_freeze_table 的 SELECT · point/ccf 各为 [(id, uid, task_ref[, status]), ...]
    status 省略默认 'frozen'。"""
    def __init__(self, point, ccf):
        self.point, self.ccf = point, ccf
        self._hit = False

    @staticmethod
    def _status(r):
        return r[3] if len(r) > 3 else "frozen"

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        rows = self.point if "point_freezes" in s else self.ccf
        has_uid = ("AND user_id = %s" in s) or ("AND customer_user_id = %s" in s)
        only_frozen = "status = 'frozen'" in s
        # has_uid 时 params = (key, uid);否则 params = (key,)
        uid = params[1] if has_uid else None
        if "task_ref = %s" in s:
            self._hit = any(r[2] == params[0] and (not has_uid or r[1] == uid)
                            and (not only_frozen or self._status(r) == "frozen") for r in rows)
        else:  # id 查询
            self._hit = any(r[0] == params[0] and (not has_uid or r[1] == uid)
                            and (not only_frozen or self._status(r) == "frozen") for r in rows)

    def fetchone(self):
        return (1,) if self._hit else None


def _route(point, ccf, **kw):
    from middleware.billing import _route_freeze_table
    return _route_freeze_table(_FakeCursor(point, ccf), **kw)


def test_legacy_only():
    assert _route([(7, 100, "T7")], [], freeze_id=7, user_id=100) == "legacy"


def test_v35_only():
    assert _route([], [(7, 200, "T7")], freeze_id=7, user_id=200) == "v35"


def test_drift_legacy_freeze_customer_now_v35():
    # 客户 300 现已是 V3.5(ccf 有别的冻结),但 freeze_id=9 是 legacy → 必须路由 legacy 不被漂移误导
    assert _route([(9, 300, "T9")], [(50, 300, "OTHER")], freeze_id=9, user_id=300) == "legacy"


def test_collision_disambiguated_by_user():
    # id=5 跨两表撞号:point 属 user A=100 · ccf 属 customer B=200
    point = [(5, 100, "TA")]
    ccf = [(5, 200, "TB")]
    assert _route(point, ccf, freeze_id=5, user_id=100) == "legacy"
    assert _route(point, ccf, freeze_id=5, user_id=200) == "v35"


def test_missing_returns_none():
    assert _route([], [], freeze_id=99, user_id=100) is None


def test_task_ref_routing():
    assert _route([(1, 100, "TX")], [], task_ref="TX", user_id=100) == "legacy"
    assert _route([], [(1, 200, "TY")], task_ref="TY", user_id=200) == "v35"


def test_collision_status_frozen_preference():
    # 同 user 同 id 跨表撞号:legacy 已 committed · v35 仍 frozen
    # frozen 优先 → 必须路由到 v35(否则 committed legacy shadow 掉 frozen v35 → 永不结算白退)
    point = [(5, 100, "T5", "committed")]
    ccf = [(5, 100, "T5", "frozen")]
    assert _route(point, ccf, freeze_id=5, user_id=100) == "v35"


def test_idempotent_recall_no_frozen_falls_back_legacy():
    # 目标已 committed(幂等 re-call)· 无 frozen → 按存在性 legacy-first 定位返回 no-op
    point = [(5, 100, "T5", "committed")]
    assert _route(point, [], freeze_id=5, user_id=100) == "legacy"


def test_collision_status_frozen_legacy_side():
    # 反向:legacy frozen · v35 已 released → frozen 优先路由 legacy
    point = [(5, 100, "T5", "frozen")]
    ccf = [(5, 100, "T5", "released")]
    assert _route(point, ccf, freeze_id=5, user_id=100) == "legacy"


def test_double_frozen_returns_ambiguous():
    # [A2] 同 user 同 id 两表【双 frozen】→ 无法纯按 id 区分 → ambiguous(调用方 fail-closed 不动钱)
    point = [(5, 100, "T5", "frozen")]
    ccf = [(5, 100, "T5", "frozen")]
    assert _route(point, ccf, freeze_id=5, user_id=100) == "ambiguous"
