"""GEO 抖音图文 v1 · 迁移登记锁(2026-08-02 Deploy 补)

背景:交付时 017/018 两个迁移【不在 migration_manifest 里】,而 prestart 只按清单跑
(不 glob 目录)—— 文件在仓库里,却永远不会执行。生产实测:geo_douyin 表 0 张、
feature_pricing 65 条无本条目。后果不是"功能惰性":`GET /posts` 等未过总闸的路由
会直接打不存在的表 → 500。

🔴 本文件的锁必须【两侧都会动】:
  · 正向:017/018 在清单里
  · 反向:rollback_017 **不**在清单里(登记它 = 上线即删表)
  · 反向:清单本身非空且能匹配(否则"都在/都不在"两句话同时为真 = 恒真断言)
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "db" / "migration_manifest.py"

MIGRATIONS_017 = "db/migration_017_geo_douyin_posts_2026_08_01.sql"
MIGRATIONS_018 = "db/migration_018_geo_douyin_pricing_2026_08_02.sql"
MIGRATIONS_019 = "db/migration_019_geo_douyin_pricing_390_2026_08_02.sql"
MIGRATIONS_020 = "db/migration_020_geo_douyin_detail_ui_2026_08_02.sql"
ROLLBACK_017 = "db/rollback_017_geo_douyin_posts_2026_08_01.sql"
ROLLBACK_019 = "db/rollback_019_geo_douyin_pricing_390_2026_08_02.sql"
ROLLBACK_020 = "db/migration_rollback_020_geo_douyin_detail_ui_2026_08_02.sql"

ALL_REGISTERED = [MIGRATIONS_017, MIGRATIONS_018, MIGRATIONS_019, MIGRATIONS_020]
ALL_ROLLBACKS = [ROLLBACK_017, ROLLBACK_019, ROLLBACK_020]


def _entries():
    """只取 MIGRATIONS 列表里的字符串字面量,先剥掉 # 注释。

    🔴 剥注释是必须的:2026-08-02 同日已有一例「断言命中了注释里的同样字样」
    的假绿(执行方自抓的 O4)。本文件上面那段 docstring 就写着这三个文件名 ——
    不剥注释的话,把清单条目删光本测试照样绿。

    🔴 而且剥注释必须在【切列表之前】。第一版我写反了(先 split("]") 再剥注释),
    可清单里的注释本身含 "]"(例如 "[策略/绑定包② · …]"、我自己加的
    "[GEO 抖音图文 v1 · … 补登记]")—— 列表在第一个注释的 "]" 处就被截断,
    新条目全部丢失:实测旧口径只解析出 60 条、新口径 80 条,而 grep 明明数得到。
    静态看两版都"像对的",只有实跑才暴露。
    """
    src = MANIFEST.read_text(encoding="utf-8")
    src = re.sub(r"#[^\n]*", "", src)            # ← 先剥注释(整文件)
    body = src.split("MIGRATIONS = [", 1)[1].split("]", 1)[0]
    return re.findall(r'"([^"]+\.sql)"', body)


def test_manifest_is_readable_and_nonempty():
    """反向对照:清单能读到且非空 —— 否则下面所有断言都是恒真/恒假。"""
    got = _entries()
    assert len(got) > 50, f"清单只解析出 {len(got)} 条,解析口径写废了"
    assert any(e.startswith("scripts/") for e in got), "没有 scripts/ 前缀条目,口径可疑"
    assert any(e.startswith("db/") for e in got), "没有 db/ 前缀条目 —— 而 db/ 前缀本就合法"


@pytest.mark.parametrize("path", ALL_REGISTERED)
def test_geo_douyin_migrations_are_registered(path):
    """正向:四个迁移必须在清单里,否则永远不会执行。"""
    assert path in _entries(), (
        f"{path} 不在 migration_manifest.MIGRATIONS —— prestart 不 glob 目录,"
        f"漏登记 = 上线后表/价目条永远不存在"
    )


@pytest.mark.parametrize("path", ALL_ROLLBACKS)
def test_rollback_script_is_never_registered(path):
    """反向:回滚脚本绝不能登记 —— 登记它 = 每次上线都把表删掉/把调价改回去。"""
    assert path not in _entries(), (
        f"{path} 被登记进清单了 —— 它是手工回滚脚本,自动执行会破坏上线结果"
    )


def test_017_runs_before_018():
    """顺序即依赖顺序:018 写 feature_pricing 与 017 无关,但保持声明顺序,
    避免以后有人把 018 挪到 017 之前时无人察觉。"""
    got = _entries()
    assert got.index(MIGRATIONS_017) < got.index(MIGRATIONS_018)


def test_019_runs_after_018():
    """🔴 019 UPDATE 的正是 018 建的那行 —— 顺序反了会先改后建,调价丢失。"""
    got = _entries()
    assert got.index(MIGRATIONS_018) < got.index(MIGRATIONS_019), (
        "019 必须排在 018 之后,否则 UPDATE 落空、上线后仍是 260")


def test_020_runs_after_017():
    """🔴 020 ALTER 的正是 017 建的那张表 —— 顺序反了会 UndefinedTable,详情页打不开。"""
    got = _entries()
    assert got.index(MIGRATIONS_017) < got.index(MIGRATIONS_020), (
        "020 必须排在 017 之后,否则 ALTER 落在不存在的表上")


@pytest.mark.parametrize("path", ALL_ROLLBACKS)
def test_rollback_script_actually_exists_on_disk(path):
    """反向对照:上面那条"不在清单"要有意义,文件本身得真存在 ——
    否则文件被删了测试照样绿,锁就空了。"""
    assert (REPO / path).is_file(), f"{path} 不存在,上一条断言失去意义"


@pytest.mark.parametrize("path", ALL_REGISTERED)
def test_registered_migration_actually_exists_on_disk(path):
    """🔴 2026-08-02 Deploy 补 —— 本锁原有的镜像洞。

    原来的断言组是不对称的:给【绝不该登记的】回滚脚本查了"存在于磁盘",
    却没给【必须登记的】迁移查。于是 019/020 出现了教科书式的组合:
    **清单里登记着、文件却根本不在包里**(`.gitignore` 的 `*.sql` 把 db/ 全挡了,
    白名单当时只写了 scripts/;017/018 是靠 `git add -f` 硬塞进去的,019/020 就漏了)。

    这一条在两侧都会动:
      · 登记了但文件没入库 → 本条红(就是 2026-08-02 的实况)
      · 文件在但没登记     → test_geo_douyin_migrations_are_registered 红
    两条合起来才是"登记与存在必须同时成立"。

    🔴 这个洞不是靠人更细心能堵的 —— 根治在 `.gitignore` 的 `!db/migration_*.sql`
    白名单(把"记得加 -f"这一步删掉),本条只是那道保险的可执行证据。
    """
    p = REPO / path
    assert p.is_file(), (
        f"{path} 已登记进 migration_manifest 但文件不在包里 —— "
        f"prestart.py:44 `if not path.exists(): raise` 会让整批部署 abort。"
        f"多半是被 .gitignore 的 *.sql 挡了:检查 !db/migration_*.sql 白名单是否还在"
    )
    assert p.stat().st_size > 0, f"{path} 是空文件,登记了等于没登记"
