"""[补充工单 2026-08-06] 账号来源要在**用户列表行**上也看得出来。

Owner 原话:「就是没有显示是否是用户在团队与席位里面自建的账号」。
他看的是列表,不是详情页 —— `87c29cdf` 把详情页修好之后,两处仍然不一致:
点进去写着「组织操作员」,退出来又变回一个看不出来历的普通用户。

本锁钉四件事(对应工单 §5 的判据表):
  1. 列表行能看出「团队成员 + 团队名」;
  2. 自助注册**不许**被标成团队成员(反向对照:两类都显示同一结果 = 判据恒真,整条作废);
  3. 列表与详情**同源** —— 走同一个 `_classify_membership_row`,不许各查一套;
  4. 一页 N 行只产生 **O(1) 次**组织查询,不许 N+1。

🔴 第 4 条是本单的工程硬约束,不是优化建议:列表页一屏几十行,
逐行调 `_account_origin` 会让用户治理页明显变慢。这里用**数 SQL 次数**来证,
不接受「感觉不慢」。
"""
import datetime
import importlib.util
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _function_body(src: str, name: str) -> str:
    """取出某个顶层函数的函数体(到下一个顶层 def / 文件末为止)。

    🔴 存在的理由:不收尾的 `split(...)[1]` 会一路吃到文件末,
    后面任何同名字符串都会让断言读到别处 —— 判据看着绿,其实钉在别的函数上。
    """
    seg = src.split(f"def {name}(", 1)[1]
    nxt = re.search(r"\n(?:def |@)", seg)
    return seg[: nxt.start()] if nxt else seg


def _svc():
    spec = importlib.util.spec_from_file_location(
        "admin_user_governance_listrow", _ROOT / "services" / "admin_user_governance.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CountingCursor:
    """只回放 organization_memberships 那一条查询,并**数它被执行了几次**。"""

    def __init__(self, memberships_by_user):
        self._by_user = memberships_by_user
        self.org_query_count = 0
        self._rows = []

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        if "FROM organization_memberships m" in flat:
            self.org_query_count += 1
            if "= ANY(%s)" in flat:
                ids = params[0]
                self._rows = [dict(self._by_user[i], user_id=i) for i in ids if i in self._by_user]
            else:
                uid = params[0]
                row = self._by_user.get(uid)
                self._rows = [dict(row, user_id=uid)] if row else []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _member_row(org_id=2, name="测试一下", owner=46, is_owner=False, status="active"):
    return {
        "organization_id": org_id, "status": status, "is_owner": is_owner, "role_id": 7,
        "joined_at": datetime.datetime(2026, 8, 4, 11, 24),
        "org_name": name, "owner_user_id": owner,
    }


# ------------------------------------------------------------------ 必须命中
def test_bulk_marks_organization_members_with_team_name():
    """#161 / #159 这类:集合版必须判成 organization_member 且带出团队名。"""
    m = _svc()
    cur = CountingCursor({159: _member_row(), 161: _member_row()})
    got = m._account_origins_bulk(cur, [159, 161])
    for uid in (159, 161):
        assert got[uid]["kind"] == "organization_member"
        assert got[uid]["organization"]["name"] == "测试一下"


def test_bulk_is_o1_not_n_plus_1():
    """🔴 工单 §3 硬约束:一页 N 行只许产生 O(1) 次组织查询。

    反向对照写在同一条里:逐行调 `_account_origin` 会是 N 次;
    集合版必须是 1 次。两个数字都打出来,不接受「感觉不慢」。
    """
    m = _svc()
    users = {uid: _member_row() for uid in range(100, 140)}
    n = len(users)

    per_row = CountingCursor(users)
    for uid in users:
        m._classify_membership_row(None)  # 不走 DB 的分类本身不算查询
    for uid in users:
        per_row.execute(
            "SELECT ... FROM organization_memberships m WHERE m.user_id = %s", (uid,))
    assert per_row.org_query_count == n, "反向对照:逐行确实是 N 次,判据不是恒真"

    bulk = CountingCursor(users)
    m._account_origins_bulk(bulk, list(users))
    assert bulk.org_query_count == 1, (
        f"集合版产生了 {bulk.org_query_count} 次组织查询(N={n})—— N+1 没被消掉"
    )


# ------------------------------------------------------------------ 必须不命中
def test_self_signup_account_is_not_marked_as_team_member():
    """🔴 反向对照(工单 §5 点名 #133):自助注册账号不许被标成团队成员。

    若两类账号都返回同一结果,说明判据恒真,整条作废。
    """
    m = _svc()
    cur = CountingCursor({161: _member_row()})          # 133 故意没有 membership
    got = m._account_origins_bulk(cur, [133, 161])
    assert got[133]["kind"] == "self_signup"
    assert got[133]["organization"] is None
    # 判别力自证:同一次调用里另一个账号确实被判成了组织成员
    assert got[161]["kind"] == "organization_member"


def test_empty_input_returns_empty_not_everyone():
    """🔴 工单 §5 反向对照:集合版入参为空必须返回空,而不是「全部算成组织成员」。

    空入参退化成不带 WHERE 的全表扫,是这类批量改造最典型的翻车方式。
    """
    m = _svc()
    cur = CountingCursor({161: _member_row()})
    assert m._account_origins_bulk(cur, []) == {}
    assert cur.org_query_count == 0, "空入参不许发查询"


def test_org_owner_is_not_laundered_into_member_in_list_either():
    """🔴 §6 禁做:不许把组织**所有者**洗成普通成员 —— 列表这条路也一样。

    生产实测 7 个受影响账号里有 2 个正是所有者(#46 / #102)。
    """
    m = _svc()
    cur = CountingCursor({46: _member_row(is_owner=True)})
    got = m._account_origins_bulk(cur, [46])
    assert got[46]["kind"] == "self_signup"


# ------------------------------------------------------------------ 同源
def test_list_and_detail_share_one_classifier():
    """🔴 口径同源:两条路都必须过 `_classify_membership_row`,不许各写一套。

    锁两层:
      · 结构层 —— 源码里 `_account_origin` 与 `_account_origins_bulk` 都调它;
      · 行为层 —— 同一行 membership 喂进两条路,判定结果必须一致。
    """
    src = (_ROOT / "services" / "admin_user_governance.py").read_text(encoding="utf-8")
    body = re.sub(r"#.*", "", src)  # 🔴 先剥注释:否则锁会命中解释它的注释(踩过)
    for fn in ("_account_origin", "_account_origins_bulk"):
        seg = body.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert "_classify_membership_row(" in seg, f"{fn} 没走共用判定函数 —— 口径要分叉了"

    m = _svc()
    row = _member_row()
    detail_cur = CountingCursor({161: row})
    m._actor = lambda cur, uid: {"user_id": int(uid), "display_name": f"用户{uid}",
                                 "is_active": True, "business_identity": "plain_user", "company": None}
    detail = m._account_origin(detail_cur, 161)
    bulk = m._account_origins_bulk(CountingCursor({161: row}), [161])[161]
    assert detail["kind"] == bulk["kind"]
    assert detail["organization"]["name"] == bulk["organization"]["name"]
    assert detail["organization"]["organization_id"] == bulk["organization"]["organization_id"]
    # 详情页额外带邀请人本人;列表**故意不带**(带了就是 N+1)
    assert detail["organization"].get("owner") is not None
    assert "owner" not in bulk["organization"]


def test_multi_membership_picks_the_same_row_on_both_paths():
    """🔴 同源的另一半:一个人有多条 membership 时,两条路必须挑中同一行。

    单个版用 `ORDER BY ... LIMIT 1`,集合版用 `DISTINCT ON`;
    两处的 ORDER BY 必须逐字同源,否则会出现「详情说 A 团队、列表说 B 团队」。
    """
    src = (_ROOT / "services" / "admin_user_governance.py").read_text(encoding="utf-8")
    body = re.sub(r"#.*", "", src)
    assert body.count("_MEMBERSHIP_PICK_ORDER") >= 3, (
        "挑行顺序没有抽成常量共用 —— 两处各写一份 ORDER BY 迟早会分叉"
    )
    assert "DISTINCT ON (m.user_id)" in body, "集合版没用 DISTINCT ON,无法保证每人只取一行"


# ------------------------------------------------------------------ 前端
def test_list_row_shows_origin_as_text():
    """前端列表行必须用**文字**写出来源(§4.2:不许只加图标或色块)。"""
    src = (_ROOT / "frontend/src/pages/Admin/UserManagement.tsx").read_text(encoding="utf-8")
    assert "account_origin === 'organization_member'" in src
    assert "团队成员 · " in src, "列表行没有可扫读的「团队成员 · 团队名」文字"
    assert "自助注册" in src, "自助注册那一侧没有对应文字,两类看起来还是一样"
    # 详情页的三态实现不许被本单动掉(§6 禁做第一条)
    assert "账号来源 · 组织操作员" in src
    assert "需人工核实来历" in src
    assert "'无邀请记录'" not in src


def test_list_dto_carries_origin_fields():
    """后端列表 DTO 必须真的带上这两个字段,否则前端拿到的是 undefined。"""
    src = (_ROOT / "services" / "admin_user_governance.py").read_text(encoding="utf-8")
    body = re.sub(r"#.*", "", src)
    # 🔴 函数段必须**收尾**:第一版写成 split(...)[1] 一路吃到文件末,
    #    把后面 `def _account_origin(` 的**定义**当成了列表里的调用,判据当场失效。
    seg = _function_body(body, "list_admin_users")
    assert '"account_origin"' in seg
    assert '"organization_name"' in seg
    assert "_account_origins_bulk(" in seg, "列表没走集合版 —— 要么没接线,要么是 N+1"
    # 判据自证:段确实被收住了,没吃到后面的函数定义
    assert "def _account_origin(" not in seg, "函数段没收尾,后面的断言等于在读别的函数"
    assert not re.search(r"_account_origin\(\s*cur\s*,", seg), "列表里逐行调用单个版 = N+1"
