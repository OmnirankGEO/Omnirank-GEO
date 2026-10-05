"""
诊断 session 归属校验单测(WORKERS=4 · foundation-fix #1b · [返工3 P1] diagnosis_runs 优先归属)

注入 fake DB(get_conn)→ 无需真库即可测:
  · [返工3 P1] diagnosis_runs.owner_user_id 优先授权(无品牌诊断 brand_id=NULL 也放行 owner);
  · 只有该 session **无 run**(旧数据)才回退 diagnosis_records→brand;
  · diagnosis_runs 表缺失/查询异常 → 回退 records(不因表缺失放行);
  · fail-closed + 跨租户拒绝 + owner/分配放行。
按 SQL 关键字路由返回值(比顺序 script 更稳 · 适配两条归属链)。
"""
from auth import session_access as sa

_RAISE = object()  # 哨兵:令该 tag 的 execute 抛异常(模拟 diagnosis_runs 表缺失)


class _RouteCur:
    def __init__(self, routes):
        self.routes = routes
        self._tag = None

    def _classify(self, q):
        ql = " ".join(q.lower().split())
        if "from diagnosis_runs" in ql and "owner_user_id = %s" in ql and "select 1" in ql:
            return "run_owner"
        if "from diagnosis_runs" in ql and "brand_id is not null" in ql:
            return "run_brand"
        if "from diagnosis_runs" in ql and "select 1" in ql:
            return "run_any"
        if "from diagnosis_records" in ql:
            return "rec_brand"
        if "from brands" in ql:
            return "brand_owner"
        if "from user_clients" in ql:
            return "user_clients"
        return None

    def execute(self, q, params=None):
        self._tag = self._classify(q)
        if self.routes.get(self._tag) is _RAISE:
            raise RuntimeError("diagnosis_runs 表不存在(模拟迁移未跑)")

    def fetchone(self):
        v = self.routes.get(self._tag)
        return None if v is _RAISE else v


class _RouteConn:
    def __init__(self, routes):
        self._cur = _RouteCur(routes)
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _conn(routes):
    return lambda: _RouteConn(dict(routes))


# ── 基础 ──────────────────────────────────────────────────────────────
def test_admin_allowed():
    assert sa.authorize_session({"is_admin": True, "user_id": 9}, "s", get_conn=_conn({})) is True


def test_no_user_denied():
    assert sa.authorize_session(None, "s", get_conn=_conn({})) is False
    assert sa.authorize_session({}, "s", get_conn=_conn({})) is False          # 无 user_id


# ── [返工3 P1] diagnosis_runs 优先归属 ─────────────────────────────────
def test_run_owner_allowed():
    # 本人是 run owner → 放行(第一条查询即命中)
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn({"run_owner": {"?column?": 1}})) is True


def test_no_brand_run_owner_allowed():
    # [返工3 P1 核心]无品牌诊断(brand_id=NULL)· 本人是 run owner → 放行(原实现会因 brand_id=NULL 误拒 owner=403)
    assert sa.authorize_session({"user_id": 7}, "s", get_conn=_conn({"run_owner": {"?column?": 1}})) is True


def test_run_brand_assigned_allowed():
    # 非 owner 但 run 挂在被分配 brand 上(user_clients)→ 放行
    routes = {"run_owner": None, "run_brand": {"brand_id": 5}, "user_clients": {"?column?": 1}}
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is True


def test_run_brand_owner_not_in_user_clients_allowed():
    # [返工3 修复净增量]run 挂在 uid 拥有的 brand 上,uid 非 creator 且**不在 user_clients**(get_or_create_brand
    #   只写 brands.owner_user_id)→ 仍放行(修复:run 路径原漏 brand owner 检查 → 真 owner 自己被 403)
    routes = {"run_owner": None, "run_brand": {"brand_id": 5}, "brand_owner": {"owner_user_id": 1}, "user_clients": None}
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is True


def test_run_exists_non_owner_denied():
    # run 存在但 uid 既非 creator 也非 brand owner 也未分配 → 拒(run 是权威归属源 · **不回退 records**)
    routes = {"run_owner": None, "run_brand": {"brand_id": 5}, "brand_owner": {"owner_user_id": 99},
              "user_clients": None, "run_any": {"?column?": 1},
              "rec_brand": {"brand_id": 5}}  # 即便 records 存在也不回退(run 权威)
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is False


# ── 无 run(旧数据)→ 回退 diagnosis_records→brand ──────────────────────
def _no_run(extra):
    r = {"run_owner": None, "run_brand": None, "run_any": None}
    r.update(extra)
    return r


def test_unknown_session_denied():
    # 无 run 且 records 查不到 → 拒(防猜测枚举)
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(_no_run({"rec_brand": None}))) is False


def test_old_no_brand_no_run_denied():
    # 旧无品牌诊断且无 run → 无法授权 → fail-closed 拒
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(_no_run({"rec_brand": {"brand_id": None}}))) is False


def test_records_owner_allowed():
    routes = _no_run({"rec_brand": {"brand_id": 5}, "brand_owner": {"owner_user_id": 1}})
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is True


def test_records_cross_tenant_denied():
    routes = _no_run({"rec_brand": {"brand_id": 5}, "brand_owner": {"owner_user_id": 99}, "user_clients": None})
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is False


def test_records_assigned_allowed():
    routes = _no_run({"rec_brand": {"brand_id": 5}, "brand_owner": {"owner_user_id": 99}, "user_clients": {"?column?": 1}})
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is True


# ── fail-closed / 表缺失回退 / tuple 行 ────────────────────────────────
def test_db_error_fail_closed():
    def boom():
        raise RuntimeError("db down / pool exhausted")
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=boom) is False


def test_diagnosis_runs_missing_falls_back_records():
    # diagnosis_runs 查询抛异常(表缺失)→ **不放行** · 回滚后回退 records(records 指向 owner → 放行)
    routes = {"run_owner": _RAISE, "run_brand": _RAISE, "run_any": _RAISE,
              "rec_brand": {"brand_id": 5}, "brand_owner": {"owner_user_id": 1}}
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is True


def test_diagnosis_runs_missing_still_fail_closed_cross_tenant():
    # 表缺失回退 records · 但跨租户仍拒(表缺失绝不放行越权)
    routes = {"run_owner": _RAISE, "run_brand": _RAISE, "run_any": _RAISE,
              "rec_brand": {"brand_id": 5}, "brand_owner": {"owner_user_id": 99}, "user_clients": None}
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is False


def test_tuple_rows_supported():
    # 非 dict cursor(tuple 行)也支持:无 run + records tuple 行
    routes = _no_run({"rec_brand": (5,), "brand_owner": (1,)})
    assert sa.authorize_session({"user_id": 1}, "s", get_conn=_conn(routes)) is True
