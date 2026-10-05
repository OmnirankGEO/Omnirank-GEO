"""#130 · 快照归属三级解析 —— `defgeo_report_snapshots` 三个月零行的真因。

🔴 病灶不是「忘了写 created_by_user_id」,而是**写了、但只在一支写**:
   `server.py` 的占位 INSERT 里

       created_by_user_id = _organization_identity.actor_user_id
                            if _organization_member else None      # ← 这个 None

   而绝大多数代理**不是组织成员** ⇒ 两列恒 NULL ⇒
   `snapshot_store.persist_report_snapshot` 的守卫
   `COALESCE(responsible_user_id, created_by_user_id)` 恒 NULL ⇒ 每份报告 return None。

   这类缺陷难发现,是因为它**不产生任何问题**:报告照出、接口照 200、
   日志里那条 WARNING 长得像一条普通提示。只有去数快照行数才看得见。

修两半,本文件两半都守:
  治本 `server.py` 占位 INSERT   —— 由接线锁(数据流同一性 + 自证)守
  治标 守卫加第三兜底 `brands.owner_user_id` —— 由行为四臂(真库)守

🔴 **兜底不放宽失败条件**:三级全空仍 return None + WARN(臂 4)。
   把「查不到归属」兜成某个默认用户,等于把快照挂到错的人名下 ——
   一个具体而错误的答案会让人**停止追查**,比没有答案更坏。

⚠️ 本文件自建 `diagnosis_records` / `brands` 两张最小表(包 fixture 不含它们),
   列类型取自 `scripts/migration_organization_internal_seats_2026_07_20.sql:1078`。
   因此行为四臂证的是**守卫的解析逻辑**;它**不证**生产建表与此同构。
"""

import ast
import io

import pytest

from services.defensive_geo.monitoring import snapshot_store
from tests.defensive_geo_w4_2026_08_22.conftest import ROOT

OWNER_OF_BRAND = 113          # brands.owner_user_id(#63 生产实证里就是这个人)
CREATOR = 907                 # 点下那一下的人
RESPONSIBLE = 908             # 被指派的负责人


PROD_SCHEMA = (ROOT / "tests" / "article_self_report_2026_08_19"
               / "prod_schema_2026-08-19.sql")
NEEDED_TABLES = ("brands", "diagnosis_records", "monitoring_run_cells")


def _prod_ddl(table):
    """从生产 schema dump 里抠出这张表的 CREATE TABLE 块。

    🔴 **不手搓最小表**。第一版我手写了三张「够用」的表,`monitoring_run_cells`
       少了 `brand_id`,守卫当场 UndefinedColumn ——
       而更危险的情况是它**不报错**:手搓的表和生产不同构时,判据照样能全绿,
       却证不了生产上那条 SQL 跑得通。夹具的形状必须来自生产产物。
    """
    src = io.open(PROD_SCHEMA, encoding="utf-8").read()
    head = "CREATE TABLE public.%s (" % table
    start = src.index(head)
    end = src.index("\n);", start) + len("\n);")
    return src[start:end].replace(head, "CREATE TABLE IF NOT EXISTS " + head[13:], 1)


@pytest.fixture
def owner_tables(cur):
    """按**生产 DDL** 建守卫要读的三张表。每个用例前清空 —— 残留会污染判据。"""
    for table in NEEDED_TABLES:
        cur.execute(_prod_ddl(table))
    cur.execute("DELETE FROM public.diagnosis_records")
    cur.execute("DELETE FROM public.brands")
    cur.execute("DELETE FROM public." + snapshot_store._snap.TABLE)
    return cur


def _seed(cur, *, diagnosis_id, brand_id=629,
          created_by=None, responsible=None, brand_owner=OWNER_OF_BRAND):
    # `name` 是生产上的 NOT NULL 列 —— 手搓最小表时看不见它,
    # 换成生产 DDL 后它当场把我的 seed 判红了。这就是同构的价值。
    cur.execute(
        "INSERT INTO public.brands(id, name, owner_user_id) VALUES (%s,%s,%s)",
        (brand_id, "brand-%d" % brand_id, brand_owner))
    # 必填列一次问齐(information_schema:is_nullable='NO' AND column_default IS NULL),
    # 不一列一列试错 —— 每试一次都在赌下一条约束不存在。
    cur.execute(
        "INSERT INTO public.diagnosis_records"
        " (id, session_id, brand_name, industry, brand_id,"
        "  created_by_user_id, responsible_user_id)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (diagnosis_id, "sess-%d" % diagnosis_id, "brand-%d" % brand_id, "测试行业",
         brand_id, created_by, responsible))


def _snapshot_owner(cur, diagnosis_id):
    cur.execute(
        "SELECT tenant_owner_user_id AS o FROM public." + snapshot_store._snap.TABLE +
        " WHERE diagnosis_id = %s", (int(diagnosis_id),))
    return [int(r["o"]) for r in cur.fetchall()]


# ---------------------------------------------------------------- 行为四臂

def test_created_by_wins_when_present(owner_tables):
    """臂 1:有 created_by ⇒ 快照 owner == created_by(不是品牌主人)。"""
    cur = owner_tables
    _seed(cur, diagnosis_id=7001, created_by=CREATOR)
    assert snapshot_store.persist_report_snapshot(cur, diagnosis_id=7001) is not None
    assert _snapshot_owner(cur, 7001) == [CREATOR]


def test_responsible_outranks_created_by(owner_tables):
    """臂 2:两列都有 ⇒ responsible 优先(COALESCE 的顺序本身是口径)。

    这一臂与臂 1、臂 3 各期望**不同的人**,所以「守卫返回写死常量」
    三臂不可能同时绿 —— 这就是「守卫改常量必红」那发毒的行为面。
    """
    cur = owner_tables
    _seed(cur, diagnosis_id=7002, created_by=CREATOR, responsible=RESPONSIBLE)
    assert snapshot_store.persist_report_snapshot(cur, diagnosis_id=7002) is not None
    assert _snapshot_owner(cur, 7002) == [RESPONSIBLE]


def test_falls_back_to_brand_owner_for_historical_rows(owner_tables):
    """臂 3:两列全 NULL(历史行 · 不回填)⇒ 兜底 brands.owner_user_id,快照写成。

    这一臂就是 #63 的生产局面:diagnosis 693/695 两列全 NULL、brands 629 owner=113。
    修前它 return None(零行),修后必须写成且挂在 113 名下。
    """
    cur = owner_tables
    _seed(cur, diagnosis_id=7003, brand_owner=OWNER_OF_BRAND)
    assert snapshot_store.persist_report_snapshot(cur, diagnosis_id=7003) is not None
    assert _snapshot_owner(cur, 7003) == [OWNER_OF_BRAND]


def test_three_levels_empty_still_refuses(owner_tables, caplog):
    """臂 4(否定臂):三级全空 ⇒ 仍 return None + WARN,**不许**兜出一个默认人。

    没有这一臂,兜底可以被写成 `COALESCE(..., 0)` 之类 —— 快照有了行,
    却挂在错的人名下,而错的具体答案会让人停止追查。
    """
    cur = owner_tables
    _seed(cur, diagnosis_id=7004, brand_owner=None)
    with caplog.at_level("WARNING"):
        assert snapshot_store.persist_report_snapshot(cur, diagnosis_id=7004) is None
    assert _snapshot_owner(cur, 7004) == []
    assert any("不写快照" in str(r.msg) for r in caplog.records), (
        "降级必须留痕 —— 静默失败最查不到")


# ------------------------------------------------- 接线锁(治本那半)

PLACEHOLDER_COLUMNS = ("created_by_user_id", "responsible_user_id")
LITERAL_COLUMNS = ("result_visibility", "created_at")


def _placeholder_else_branches(src):
    """取占位 INSERT 参数里两个归属字段的 `else` 分支 AST 节点。

    🔴 按**数据流同一性**取,不用包含判定:`"creator_user_id" in src` 恒真
       (这个名字在 server.py 里出现十几次),那种断言证不了它进了这个参数。
    """
    found = {}
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        sql_node, params = node.args[0], node.args[1]
        if not isinstance(sql_node, ast.Constant) or not isinstance(sql_node.value, str):
            continue
        if "INSERT INTO diagnosis_records" not in sql_node.value:
            continue
        if not isinstance(params, ast.Tuple):
            continue
        head = sql_node.value.split("(", 1)[1].split(")", 1)[0]
        order = [c.strip() for c in head.replace("\n", " ").split(",")]
        # 参数元组里没有 result_visibility('pending')与 created_at(NOW())这两个字面量
        positional = [c for c in order if c not in LITERAL_COLUMNS]
        for col in PLACEHOLDER_COLUMNS:
            if col in positional and positional.index(col) < len(params.elts):
                found[col] = params.elts[positional.index(col)]
    return found


def test_placeholder_attributes_non_org_users_to_the_creator():
    """治本锁:两个归属字段的 else 分支必须是 `creator_user_id` 这个**名字本身**。

    修前是 `else None` ⇒ 非组织用户两列全空。这条锁盯的就是那个 None。
    """
    src = io.open(ROOT / "server.py", encoding="utf-8").read()
    branches = _placeholder_else_branches(src)
    assert set(branches) == set(PLACEHOLDER_COLUMNS), (
        "没在 server.py 里定位到占位 INSERT 的两个归属参数 —— 分母塌了,不是通过:%s"
        % sorted(branches))
    for col, node in branches.items():
        assert isinstance(node, ast.IfExp), (
            "%s 不再是三元 —— 锁的形状假设变了,请重锚" % col)
        assert not isinstance(node.orelse, ast.Constant), (
            "%s 的 else 分支是常量(%r)—— 非组织用户又会写空,#63 原样复发"
            % (col, getattr(node.orelse, "value", None)))
        assert isinstance(node.orelse, ast.Name) and node.orelse.id == "creator_user_id", (
            "%s 的 else 分支不是 creator_user_id" % col)


def test_the_placeholder_lock_would_catch_the_regression():
    """锁的自证:把 else 分支改回 `None` 的合成源码,上面那条必须红。

    喂**合成坏代码**,不改真文件 —— 自证不该污染被测树。
    """
    src = io.open(ROOT / "server.py", encoding="utf-8").read()
    poisoned = src.replace(
        "_organization_identity.actor_user_id if _organization_member else creator_user_id,",
        "_organization_identity.actor_user_id if _organization_member else None,")
    assert poisoned != src, "毒没下成 —— 「锁没牙」与「毒没下成」信号同形,必须自证"
    branches = _placeholder_else_branches(poisoned)
    assert set(branches) == set(PLACEHOLDER_COLUMNS)
    assert all(isinstance(n.orelse, ast.Constant) for n in branches.values()), (
        "毒下进去了,但探针没看出来 —— 锁的扫描面不对")
