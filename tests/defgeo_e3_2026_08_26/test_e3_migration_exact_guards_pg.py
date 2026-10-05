"""E3-3 · 迁移 051/052(以及本单新增的 054)的精确等价核验 + 同名错定义 poison。

被修的缺陷(Codex 二审 P1-F4 / P1-F5,已逐条亲证):
  · 051 的终检只做两次 ``position(... in idxdef)`` 子串探针。一条
        CREATE UNIQUE INDEX uq_defgeo_pcmd_provider_order_ref
            ON public.defgeo_publish_commands (id)
         WHERE provider_order_ref IS NOT NULL
    三个探针**全过**,而它对 provider_order_ref 零约束 ——
    同一个上游单号照样能结算两笔冻结。
  · 052 全文没有 ``pg_get_constraintdef``:同名两列弱 CHECK 直接放行。
    这一形态 2026-08-26 **真的**在本仓的判据库上发生过。

修法(不是再手写一段守卫)
--------------------------
🔴 本仓**已经有**这条机械轴:``scripts/defgeo_readiness_gen.py`` 从迁移文件
   机械导出期望对象集,期望定义从真 PG16 现读,生成 ``@readiness-begin`` 自验块,
   双向一致性由 ``tests/defensive_geo_w3_2026_08_21`` 守。
   051/052 **恰好都在这个轴外面** —— 轴建得好好的,只是 ``READINESS_FILES``
   这份**手写**名单没把它们列进去。漏登记的那一个不会让任何判据变红,
   本仓记过的同一条老教训又一形态。
   所以本单把它们(连同 054)接进轴,并顺手补上轴本身缺的两位:
   ``convalidated`` 与 ``indisvalid/indisready``。

本文件打的是**行为**:冷库预置一个同名错定义,迁移必须 RAISE。
"""

from __future__ import annotations

import psycopg2
import pytest

from tests.defgeo_e3_2026_08_26.conftest import POISON_TARGETS


# ══════════════════════════════════════════════════════════════════════
# ① 051 —— 同名但键列错的唯一索引
# ══════════════════════════════════════════════════════════════════════

_WRONG_051 = (
    "CREATE UNIQUE INDEX uq_defgeo_pcmd_provider_order_ref "
    "  ON public.defgeo_publish_commands (publish_command_id) "
    " WHERE provider_order_ref IS NOT NULL")

_RIGHT_051 = (
    "CREATE UNIQUE INDEX uq_defgeo_pcmd_provider_order_ref "
    "  ON public.defgeo_publish_commands (provider_order_ref) "
    " WHERE provider_order_ref IS NOT NULL")


def test_e3_01_051_refuses_a_same_named_index_on_the_wrong_key(cold_db):
    """键列错的同名索引必须让 051 当场 RAISE。

    这条索引能通过旧终检的**三个**探针:名字对、宿主表对、
    ``pg_get_indexdef`` 里既有 ``UNIQUE INDEX`` 也有
    ``provider_order_ref IS NOT NULL``。但它唯一的是 publish_command_id
    (本来就是主键),对 provider_order_ref 零约束 ——
    也就是 P1-5 要挡的"一个上游单号结算两笔冻结"原样可发生。
    """
    cur, apply_migration = cold_db
    cur.execute(_WRONG_051)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        apply_migration("051")
    assert "051" in str(exc.value), str(exc.value)


def test_e3_02_051_accepts_the_exact_index(cold_db):
    """判别力自证:定义**正确**的同名索引必须幂等通过。

    没有这一条,上一条可以靠"051 在任何预置索引下都炸"而假绿 ——
    那样的守卫在生产第二次部署时会把发布链整条拦下来。
    """
    cur, apply_migration = cold_db
    cur.execute(_RIGHT_051)
    apply_migration("051")            # 不抛即通过
    cur.execute(
        "SELECT pg_get_indexdef(c.oid) AS d FROM pg_class c "
        "  JOIN pg_index i ON i.indexrelid=c.oid "
        " WHERE c.relname='uq_defgeo_pcmd_provider_order_ref' "
        "   AND i.indrelid=to_regclass('public.defgeo_publish_commands')")
    assert "provider_order_ref" in cur.fetchone()["d"]


def test_e3_03_051_from_scratch_still_builds_the_index(cold_db):
    """零预置的冷库上,051 必须真的把索引建出来(轴不是只会挑刺)。"""
    cur, apply_migration = cold_db
    apply_migration("051")
    cur.execute(
        "SELECT i.indisunique AS u, i.indisvalid AS v, "
        "       ARRAY(SELECT a.attname FROM unnest(i.indkey) k(attnum) "
        "               JOIN pg_attribute a ON a.attrelid=i.indrelid "
        "                AND a.attnum=k.attnum) AS cols "
        "  FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid "
        " WHERE c.relname='uq_defgeo_pcmd_provider_order_ref' "
        "   AND i.indrelid=to_regclass('public.defgeo_publish_commands')")
    row = cur.fetchone()
    assert row and row["u"] and row["v"]
    assert list(row["cols"]) == ["provider_order_ref"], row["cols"]


# ══════════════════════════════════════════════════════════════════════
# ② 052 —— 同名但少一列的弱 CHECK
# ══════════════════════════════════════════════════════════════════════

_WEAK_052 = (
    "ALTER TABLE public.defgeo_activation_outbox "
    "  ADD COLUMN IF NOT EXISTS payer_user_id INTEGER, "
    "  ADD COLUMN IF NOT EXISTS payer_funding_policy VARCHAR(64), "
    "  ADD COLUMN IF NOT EXISTS payer_principal_kind VARCHAR(64)")

_WEAK_052_CHECK = (
    "ALTER TABLE public.defgeo_activation_outbox "
    "  ADD CONSTRAINT defgeo_activation_outbox_payer_group CHECK ("
    "      (payer_user_id IS NULL AND payer_funding_policy IS NULL) "
    "   OR (payer_user_id IS NOT NULL AND payer_funding_policy IS NOT NULL))")


def test_e3_10_052_refuses_a_same_named_two_column_check(cold_db):
    """少一列(payer_principal_kind)的同名 CHECK 必须让 052 当场 RAISE。

    🔴 这不是假想形态:2026-08-26 本仓的判据库上真的出现过它 ——
       一次撕锁把第二支收窄成两列,还原时按**数量**核对而不是按定义,
       于是弱约束留在库上,而整包 76/76 报绿。
       弱在哪里:``payer_principal_kind`` 可以单独为空 ⇒ 半冻结的付款人
       身份重新变得可表达,materializer 又得现算政策。
    """
    cur, apply_migration = cold_db
    cur.execute(_WEAK_052)
    cur.execute(_WEAK_052_CHECK)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        apply_migration("052")
    assert "052" in str(exc.value), str(exc.value)


def test_e3_11_052_accepts_the_exact_check(cold_db):
    """判别力自证:三列齐备的同名 CHECK 必须幂等通过。"""
    cur, apply_migration = cold_db
    apply_migration("052")            # 冷库首跑,建出正确定义
    apply_migration("052")            # 再跑一次 = 幂等重放(prestart 每次部署都这样)
    cur.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        " WHERE conname='defgeo_activation_outbox_payer_group' "
        "   AND conrelid='public.defgeo_activation_outbox'::regclass")
    d = cur.fetchone()["d"]
    for col in ("payer_user_id", "payer_funding_policy", "payer_principal_kind"):
        assert col in d, f"{col} 不在 CHECK 定义里:{d}"


def test_e3_12_052_refuses_a_same_named_non_check_constraint(cold_db):
    """同名但**不是 CHECK**(比如 UNIQUE)的约束同样必须被拒。

    旧守卫只比 ``conname + conrelid``,连 ``contype`` 都不看 ——
    一条同名 UNIQUE 会把 ADD 永久压住,而 prestart 的无条件重放
    永远修不回来。
    """
    cur, apply_migration = cold_db
    cur.execute(_WEAK_052)
    cur.execute("ALTER TABLE public.defgeo_activation_outbox "
                "  ADD CONSTRAINT defgeo_activation_outbox_payer_group "
                "  UNIQUE (payer_user_id, payer_funding_policy, payer_principal_kind)")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        apply_migration("052")
    assert "052" in str(exc.value), str(exc.value)


# ══════════════════════════════════════════════════════════════════════
# ③ 054 —— 本单自己的迁移,同一把牙齿
# ══════════════════════════════════════════════════════════════════════

def test_e3_20_054_refuses_a_same_named_weak_check(cold_db):
    """``>= 0`` 的同名弱 CHECK 必须让 054 当场 RAISE。

    ``>= 0`` 与 ``> 0`` 只差一个字符,而那个字符正好是本单要消灭的
    "编造租户 0"的入口。
    """
    cur, apply_migration = cold_db
    cur.execute("ALTER TABLE public.monitoring_run_cells "
                "  ADD COLUMN IF NOT EXISTS tenant_owner_user_id INTEGER")
    cur.execute("ALTER TABLE public.monitoring_run_cells "
                "  ADD CONSTRAINT chk_monitoring_run_cells_tenant_owner_positive "
                "  CHECK (tenant_owner_user_id IS NULL OR tenant_owner_user_id >= 0)")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        apply_migration("054")
    assert "054" in str(exc.value), str(exc.value)


def test_e3_21_054_refuses_a_same_named_wrong_type_column(cold_db):
    """同名但**类型错**的列必须被拒 —— ``ADD COLUMN IF NOT EXISTS`` 不会修它。"""
    cur, apply_migration = cold_db
    cur.execute("ALTER TABLE public.monitoring_run_cells "
                "  ADD COLUMN IF NOT EXISTS tenant_owner_user_id TEXT")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        apply_migration("054")
    assert "054" in str(exc.value), str(exc.value)


def test_e3_22_054_from_scratch_and_replay(cold_db):
    """判别力自证:冷库首跑建对,重放幂等通过。"""
    cur, apply_migration = cold_db
    apply_migration("054")
    apply_migration("054")
    cur.execute(
        "SELECT pg_get_constraintdef(oid) AS d, convalidated AS v "
        "  FROM pg_constraint "
        " WHERE conname='chk_monitoring_run_cells_tenant_owner_positive' "
        "   AND conrelid='public.monitoring_run_cells'::regclass")
    row = cur.fetchone()
    assert row and row["v"] is True
    assert "> 0" in row["d"], row["d"]


# ══════════════════════════════════════════════════════════════════════
# ④ 轴的作用域 —— 三条迁移真的进了 readiness 轴
# ══════════════════════════════════════════════════════════════════════

def test_e3_30_the_three_migrations_are_on_the_readiness_axis():
    """051 / 052 / 054 必须在 ``READINESS_FILES`` 里。

    🔴 上面那些 poison 判据打的是**行为**;这一条打的是**作用域**。
       只有行为判据时,有人把 051 从轴上摘下去、改回手写子串探针,
       poison 判据仍然会红(好)—— 但如果他同时把 poison 判据也删了,
       就没有任何东西记得"051 本来该在轴上"。作用域锁记得。
    """
    from scripts.defgeo_readiness_gen import READINESS_FILES

    for tag, rel in POISON_TARGETS.items():
        assert READINESS_FILES.get(tag) == rel, (
            f"{tag} 不在 readiness 轴上(实得 {READINESS_FILES.get(tag)!r})—— "
            "Codex 二审 P1-F4/F5 打穿的就是「轴外面」的文件")


def test_e3_31_051_no_longer_relies_on_substring_probes():
    """051 里不许再有 ``position(... in idxdef)`` 这种子串探针。

    子串探针给的是"看起来在验"的错觉:``UNIQUE INDEX`` 与
    ``provider_order_ref IS NOT NULL`` 两个子串,一条键列完全错的索引
    照样两个都有。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sql = (root / POISON_TARGETS["051"]).read_text(encoding="utf-8")
    body = "\n".join(
        ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "position(" not in body.lower(), (
        "051 里还有子串探针 —— 它挡不住键列错的同名索引")
    # 🔴 [P0 2026-09-02] 锚随取值口更新:051 的索引核验现在走
    #    `defgeo_ident_index`(indisvalid/indisready + 列名 + indpred 身份),
    #    不再比 `pg_get_indexdef` 的渲染文本 —— 那正是本轮修掉的病
    #    (dump/restore 会重写渲染,文本全等在第二次部署必炸)。
    #    命题一个字没改:051 仍然必须**核定义**而不是只做子串探针。
    assert "defgeo_ident_index(" in body, "051 丢了索引定义核验"
