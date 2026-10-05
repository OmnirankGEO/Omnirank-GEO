"""词表收敛锁 · WO_VOCAB_CONVERGENCE_2026-08-09

每一条"必须命中"都配了成对的"必须不命中"—— 单侧断言证明不了判别力
(本仓踩过:正反两侧同时失效 = 零判别力却全绿)。

三组:
  A. 纯函数层(归一映射本身)
  B. 🔴 接线层(真库 · 四个写入点真的把归一结果写进去了)—— 本单判据的重心
  C. 消费层(分类下拉不再冒出价格档)+ 迁移层(列/CHECK/回滚)
"""
from __future__ import annotations

import psycopg2
import pytest

from tools.media_vocab_normalize import (
    AREA_SYNONYMS,
    LISTING_SLOTS,
    canonical_area,
    detect_listing_slot,
    is_listing_slot,
    normalize_media_row,
    normalize_wemedia_row,
)


# ══════════════════════════════ A. 纯函数层 ══════════════════════════════

@pytest.mark.parametrize("raw,expect", [("综合全国", "全国"), ("海外", "全球")])
def test_area_synonyms_are_merged(raw, expect):
    """必须命中:Owner 2026-08-09 批的两组同义词。"""
    assert canonical_area(raw) == expect


@pytest.mark.parametrize("raw", ["广东", "北京", "全国", "全球", "华东", "浙江省"])
def test_area_non_synonyms_are_untouched(raw):
    """🔴 成对的必须不命中:不在同义表里的一个字都不许动。

    含目标值本身(全国/全球)—— 归一必须幂等,不能把「全国」再映射走。
    """
    assert canonical_area(raw) == raw


def test_area_normalization_is_idempotent():
    """归一两次 = 归一一次(回填脚本可能被重复执行)。"""
    for raw in list(AREA_SYNONYMS) + ["广东", ""]:
        once = canonical_area(raw)
        assert canonical_area(once) == once


def test_area_blank_and_none_are_empty_string():
    assert canonical_area(None) == ""
    assert canonical_area("   ") == ""


def test_listing_slot_vocabulary_is_exactly_these_three():
    """🔴 判据必须写成**字面量**,不能从被测常量生成。

    下面那条 parametrize 的参数取自 LISTING_SLOTS 本身 —— 词表里删掉「十元专区」时,
    对应的参数化用例会**直接消失**而不是失败(变异 M3 实测)。
    从被测对象生成的判据,对"被测对象被删小了"这类变异天然免疫 = 零判别力。
    """
    assert LISTING_SLOTS == frozenset({"套餐系列", "十元专区", "最新秒杀"})


@pytest.mark.parametrize("raw", sorted(LISTING_SLOTS))
def test_listing_slots_are_detected(raw):
    assert is_listing_slot(raw) is True
    assert detect_listing_slot(raw) == raw


@pytest.mark.parametrize("raw", ["新闻资讯", "汽车网站", "财经商业", "海外媒体", "商业资讯", "", None])
def test_real_categories_are_not_listing_slots(raw):
    """🔴 成对的必须不命中 —— 特意放了「海外媒体」「商业资讯」:

    它们长得像档位/地域,但 2026-08-09 实测是真体裁值(757 / 810 行),
    误判会把 800+ 条真媒体的分类抹掉。
    """
    assert is_listing_slot(raw) is False
    assert detect_listing_slot(raw) is None


def test_detect_listing_slot_prefers_first_hit():
    """按传入顺序取第一个命中(调用方按权威度排 resource_type_name > category)。"""
    assert detect_listing_slot("十元专区", "套餐系列") == "十元专区"
    assert detect_listing_slot("新闻资讯", "套餐系列") == "套餐系列"


def test_normalize_media_row_keeps_original_category_values():
    """🔴 Owner 批的是 (c) 叠加标记:原列值一个字不动,只多一个 listing_slot。

    这条是 (c) 与 (a)/(b) 方案的分界线 —— 改成腾空/反推,这条锁必红。
    """
    row = normalize_media_row({"area": "综合全国", "resource_type_name": "十元专区",
                               "category": "十元专区"})
    assert row["resource_type_name"] == "十元专区"   # 原值不动
    assert row["category"] == "十元专区"             # 原值不动
    assert row["listing_slot"] == "十元专区"         # 只是多了标记
    assert row["area"] == "全国"                     # 地域归一了


def test_normalize_media_row_sets_none_for_ordinary_media():
    row = normalize_media_row({"area": "广东", "resource_type_name": "新闻资讯"})
    assert row["listing_slot"] is None
    assert row["area"] == "广东"


def test_normalize_wemedia_row_touches_province_only():
    row = normalize_wemedia_row({"province": "海外", "industry": "综合"})
    assert row["province"] == "全球"
    assert row["industry"] == "综合"          # 自媒体侧 industry 是另一层病,本单不动
    assert "listing_slot" not in row          # 该表没有这一列,不许顺手塞


# ══════════════════════ B. 接线层(真库)· 本单判据重心 ══════════════════════

def _fetch(conn, sql, params=None):
    """返回 list[tuple] —— 连接是 RealDictCursor(与生产同构),这里按列顺序转元组,
    只为让断言写起来短;被测代码拿到的仍是生产那种 dict 行。"""
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return [tuple(r.values()) for r in cur.fetchall()]


def test_wiring_meijiehezi_upsert_media_writes_canonical_area(conn, media_row):
    """🔴 接线锁:走真正的写入函数,库里落的必须是归一后的值。

    判据打在**落库结果**上,不是"归一函数被调用过"——
    本仓五次「接线没接」都是函数写好了却没接进路径。
    """
    from db import meijiehezi_db
    with conn.cursor() as cur:
        meijiehezi_db._upsert_media(cur, media_row(id=1, area="综合全国"))
        meijiehezi_db._upsert_media(cur, media_row(id=2, area="海外"))
        meijiehezi_db._upsert_media(cur, media_row(id=3, area="广东"))

    rows = dict(_fetch(conn, "SELECT id, area FROM mhz_media ORDER BY id"))
    assert rows[1] == "全国"
    assert rows[2] == "全球"
    assert rows[3] == "广东"          # 成对不命中:非同义值原样落库
    assert _fetch(conn, "SELECT count(*) FROM mhz_media WHERE area = '综合全国'")[0][0] == 0


def test_wiring_meijiehezi_upsert_media_writes_listing_slot(conn, media_row):
    """🔴 Owner 点名的验收判据:下次同步「十元专区」必须落 listing_slot。"""
    from db import meijiehezi_db
    with conn.cursor() as cur:
        meijiehezi_db._upsert_media(cur, media_row(id=1, resource_type_name="十元专区"))
        meijiehezi_db._upsert_media(cur, media_row(id=2, resource_type_name="套餐系列"))
        meijiehezi_db._upsert_media(cur, media_row(id=3, resource_type_name="新闻资讯"))

    rows = dict(_fetch(conn, "SELECT id, listing_slot FROM mhz_media ORDER BY id"))
    assert rows[1] == "十元专区"
    assert rows[2] == "套餐系列"
    assert rows[3] is None            # 成对不命中:真体裁不落档位

    # 同时:原列值一个字没动(Owner 批的 (c))
    kept = dict(_fetch(conn, "SELECT id, resource_type_name FROM mhz_media ORDER BY id"))
    assert kept[1] == "十元专区"
    assert kept[3] == "新闻资讯"


def test_wiring_upsert_media_conflict_update_also_carries_slot(conn, media_row):
    """ON CONFLICT 分支同样要更新 listing_slot —— 只写 INSERT 分支的话,

    存量行第二次同步时档位永远停在第一次的值(上游改档就漂了)。
    """
    from db import meijiehezi_db
    with conn.cursor() as cur:
        meijiehezi_db._upsert_media(cur, media_row(id=1, resource_type_name="新闻资讯"))
        assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=1")[0][0] is None
        # 上游把它挪进十元专区 → 同一 id 再同步一次
        meijiehezi_db._upsert_media(cur, media_row(id=1, resource_type_name="十元专区"))
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=1")[0][0] == "十元专区"
    # 反向:再挪回普通体裁,标记必须跟着清掉(不能只加不减)
    with conn.cursor() as cur:
        meijiehezi_db._upsert_media(cur, media_row(id=1, resource_type_name="新闻资讯"))
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=1")[0][0] is None


def test_wiring_wemedia_upsert_writes_canonical_province(conn, wemedia_row):
    from db import meijiehezi_db
    with conn.cursor() as cur:
        meijiehezi_db._upsert_wemedia(cur, wemedia_row(id=1, province="综合全国"))
        meijiehezi_db._upsert_wemedia(cur, wemedia_row(id=2, province="海外"))
        meijiehezi_db._upsert_wemedia(cur, wemedia_row(id=3, province="浙江"))
    rows = dict(_fetch(conn, "SELECT id, province FROM mhz_wemedia ORDER BY id"))
    assert rows[1] == "全国"
    assert rows[2] == "全球"
    assert rows[3] == "浙江"


def test_wiring_publish_db_bulk_upsert_carries_slot(conn, monkeypatch):
    """🔴 第 2/3 个写入点:publish_db 写的是 category(不是 resource_type_name)。

    2026-08-09 实测 category 列里同样有 548 行价格档 —— 只在 meijiehezi_db 那处落闸会漏。
    """
    from db import publish_db
    monkeypatch.setattr(publish_db, "get_markup_ratio", lambda: 1.0)

    class _Ctx:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(publish_db, "get_db", lambda: _Ctx())
    publish_db.bulk_upsert_media([
        {"id": 11, "media_name": "A", "category": "十元专区", "price_vip": 10},
        {"id": 12, "media_name": "B", "category": "新闻资讯", "price_vip": 10},
    ])
    rows = dict(_fetch(conn, "SELECT id, listing_slot FROM mhz_media WHERE id IN (11,12) ORDER BY id"))
    assert rows[11] == "十元专区"
    assert rows[12] is None


def test_wiring_publish_db_upsert_media_carries_slot(conn, noclose, monkeypatch):
    from db import publish_db
    monkeypatch.setattr(publish_db, "get_markup_ratio", lambda: 1.0)
    # 调用方自己 finally: conn.close(),而 psycopg2 的 close 是只读属性 patch 不掉 → 用代理挡住
    monkeypatch.setattr(publish_db, "get_connection", lambda: noclose)
    publish_db.upsert_media({
        "id": 21, "media_name": "C", "category": "套餐系列", "media_type": "media",
        "platform": "", "price_normal": 10, "price_vip": 10, "price_svip": 10,
        "inclusion_rate": "", "avg_publish_time": "", "publish_rate": "",
        "pc_weight": 0, "mobile_weight": 0, "news_source": "", "link_type": "",
        "can_geo": 0, "case_link": "", "remark": "",
    })
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=21")[0][0] == "套餐系列"


# ═══════════════ C. 消费层 + 迁移层 ═══════════════

def test_category_dropdown_excludes_listing_slots(conn, noclose, monkeypatch, media_row):
    """🔴 "不再污染体裁列"的可验出口:分类下拉里不许再出现价格档。"""
    from db import meijiehezi_db, publish_db
    with conn.cursor() as cur:
        for i, name in enumerate(["新闻资讯", "汽车网站", "十元专区", "套餐系列", "最新秒杀"], 1):
            meijiehezi_db._upsert_media(cur, media_row(id=i, resource_type_name=name))
    monkeypatch.setattr(publish_db, "get_connection", lambda: noclose)

    cats = publish_db.get_media_categories()
    assert "新闻资讯" in cats and "汽车网站" in cats     # 必须命中:真体裁照常出
    for slot in LISTING_SLOTS:                           # 成对不命中:档位一个都不许出
        assert slot not in cats


def test_category_dropdown_filter_does_not_depend_on_backfill(conn, noclose, monkeypatch, media_row):
    """过滤按**值**排除,不靠 listing_slot 列 —— 迁移/回填没跑时也得生效。

    (把正确性押在"另一步跑过了"上,是本仓 P0 的常见形态。)
    """
    from db import meijiehezi_db, publish_db
    with conn.cursor() as cur:
        meijiehezi_db._upsert_media(cur, media_row(id=1, resource_type_name="十元专区"))
        # 人为把回填结果抹掉,模拟"迁移跑了但回填没跑"
        cur.execute("UPDATE mhz_media SET listing_slot = NULL")
    monkeypatch.setattr(publish_db, "get_connection", lambda: noclose)
    assert "十元专区" not in publish_db.get_media_categories()


def test_migration_created_column_check_and_index(conn):
    got = _fetch(conn, """
        SELECT column_name, is_nullable FROM information_schema.columns
         WHERE table_name='mhz_media' AND column_name='listing_slot'
    """)
    assert got == [("listing_slot", "YES")]
    assert _fetch(conn, "SELECT count(*) FROM pg_constraint "
                        "WHERE conname='mhz_media_listing_slot_check'")[0][0] == 1
    assert _fetch(conn, "SELECT count(*) FROM pg_indexes "
                        "WHERE indexname='idx_mhz_media_listing_slot'")[0][0] == 1


def test_migration_check_rejects_unknown_slot(conn):
    """CHECK 必须真的会拦 —— 上游新增第四个档位时要响亮报错,不是静默混进去。"""
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO mhz_media (id, listing_slot) VALUES (99, '五元专区')")


def test_migration_is_idempotent(conn):
    """重复跑 031 不报错(部署清单会重复执行)。"""
    from tests.vocab_convergence_2026_08_09.conftest import MIGRATION
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    assert _fetch(conn, "SELECT count(*) FROM pg_constraint "
                        "WHERE conname='mhz_media_listing_slot_check'")[0][0] == 1


def test_missing_migration_fails_loudly_not_silently(bare_conn, media_row):
    """🔴 迁移漏跑时必须**响亮**失败(manifest 注释里承诺的方向)。

    若这条变成"静默通过",说明写入侧没真带 listing_slot 列 —— 那时
    上面所有接线锁都会变成空转(库里恒 NULL,而没人报错)。
    """
    from db import meijiehezi_db
    with pytest.raises(psycopg2.errors.UndefinedColumn):
        with bare_conn.cursor() as cur:
            meijiehezi_db._upsert_media(cur, media_row(id=1, resource_type_name="十元专区"))


def test_rollback_script_removes_everything(conn):
    from tests.vocab_convergence_2026_08_09.conftest import ROLLBACK
    with conn.cursor() as cur:
        cur.execute(ROLLBACK.read_text(encoding="utf-8"))
    assert _fetch(conn, "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name='mhz_media' AND column_name='listing_slot'")[0][0] == 0
    assert _fetch(conn, "SELECT count(*) FROM pg_constraint "
                        "WHERE conname='mhz_media_listing_slot_check'")[0][0] == 0
    assert _fetch(conn, "SELECT count(*) FROM pg_indexes "
                        "WHERE indexname='idx_mhz_media_listing_slot'")[0][0] == 0


def test_backfill_refuses_to_run_when_gate_missing(monkeypatch):
    """🔴 回填脚本的闸自检必须真的会拒跑 —— 顺序反了(先回填后落闸)= 白干。"""
    import importlib
    bf = importlib.import_module("scripts.backfill_media_vocab_2026_08_09")
    from db import meijiehezi_db

    def _fake_source(func):
        return "def _upsert_media(cursor, media):\n    cursor.execute('...')\n"

    monkeypatch.setattr(bf.__dict__.setdefault("inspect", __import__("inspect")),
                        "getsource", _fake_source, raising=False)
    import inspect as _real_inspect
    monkeypatch.setattr(_real_inspect, "getsource", _fake_source)
    with pytest.raises(SystemExit) as exc:
        bf.assert_write_gate_installed()
    assert exc.value.code == 2


def test_backfill_gate_passes_on_real_code():
    """成对的必须命中:真代码上闸自检要通过(否则上一条是恒真的)。"""
    import importlib
    bf = importlib.import_module("scripts.backfill_media_vocab_2026_08_09")
    bf.assert_write_gate_installed()   # 不抛 SystemExit 即通过


# ═════════════ D. 收尾补的三条回归锁(2026-08-09 二次交付)═════════════

# (开源版:第 5 个写入点 —— 供应商价目表导入脚本 —— 不在开源仓,它的两格回归锁随之不进。)


def test_wiring_publish_db_upsert_media_conflict_updates_slot(conn, noclose, monkeypatch):
    """🔴 `publish_db.upsert_media` 的 **ON CONFLICT 分支**要更新 listing_slot。

    原来只锁了 INSERT 路径(id 是新的),而生产同步绝大多数走的是 ON CONFLICT ——
    只测新增等于把最常走的那条路留在锁外面。
    """
    from db import publish_db
    monkeypatch.setattr(publish_db, "get_markup_ratio", lambda: 1.0)
    monkeypatch.setattr(publish_db, "get_connection", lambda: noclose)

    def _sync(category):
        publish_db.upsert_media({
            "id": 31, "media_name": "D", "category": category, "media_type": "media",
            "platform": "", "price_normal": 10, "price_vip": 10, "price_svip": 10,
            "inclusion_rate": "", "avg_publish_time": "", "publish_rate": "",
            "pc_weight": 0, "mobile_weight": 0, "news_source": "", "link_type": "",
            "can_geo": 0, "case_link": "", "remark": "",
        })

    _sync("新闻资讯")                       # 首次:普通体裁
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=31")[0][0] is None
    _sync("十元专区")                       # 上游改档 → 走 ON CONFLICT
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=31")[0][0] == "十元专区"
    _sync("新闻资讯")                       # 反向:改回体裁,标记必须清掉(不能只加不减)
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=31")[0][0] is None


def test_wiring_publish_db_bulk_upsert_conflict_updates_slot(conn, monkeypatch):
    """同上 · `bulk_upsert_media` 的 ON CONFLICT 分支。"""
    from db import publish_db
    monkeypatch.setattr(publish_db, "get_markup_ratio", lambda: 1.0)

    class _Ctx:
        def __enter__(self_inner):
            return conn

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(publish_db, "get_db", lambda: _Ctx())

    publish_db.bulk_upsert_media([{"id": 41, "media_name": "E", "category": "汽车网站", "price_vip": 10}])
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=41")[0][0] is None
    publish_db.bulk_upsert_media([{"id": 41, "media_name": "E", "category": "最新秒杀", "price_vip": 10}])
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=41")[0][0] == "最新秒杀"
    publish_db.bulk_upsert_media([{"id": 41, "media_name": "E", "category": "汽车网站", "price_vip": 10}])
    assert _fetch(conn, "SELECT listing_slot FROM mhz_media WHERE id=41")[0][0] is None


def test_init_mhz_tables_self_heals_listing_slot(bare_conn, monkeypatch):
    """🔴 运行时自愈路径 `init_mhz_tables()` 必须也能把 listing_slot 建出来。

    migration_031 走部署清单,init_mhz_tables 是**另一条**路(新库 / 旧表 / dev)。
    只有迁移那条路建列的话,新库上 _upsert_media 的 INSERT 会 UndefinedColumn
    把整批同步打挂。用**没跑迁移**的库(bare_conn)证明自愈真的补上了。
    """
    from db import meijiehezi_db
    assert _fetch(bare_conn, "SELECT count(*) FROM information_schema.columns "
                             "WHERE table_name='mhz_media' AND column_name='listing_slot'")[0][0] == 0

    class _NoClose:
        def __init__(self, c):
            self._c = c

        def close(self):
            pass

        def __getattr__(self, n):
            return getattr(self._c, n)

    monkeypatch.setattr(meijiehezi_db, "_get_conn", lambda: _NoClose(bare_conn))
    meijiehezi_db.init_mhz_tables()

    assert _fetch(bare_conn, "SELECT count(*) FROM information_schema.columns "
                             "WHERE table_name='mhz_media' AND column_name='listing_slot'")[0][0] == 1
    # 自愈补的列必须真能写(不是建了个类型不对的)
    with bare_conn.cursor() as cur:
        cur.execute("INSERT INTO mhz_media (id, listing_slot) VALUES (77, '十元专区')")
    assert _fetch(bare_conn, "SELECT listing_slot FROM mhz_media WHERE id=77")[0][0] == "十元专区"
