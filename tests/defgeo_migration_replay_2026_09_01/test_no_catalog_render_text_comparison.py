# -*- coding: utf-8 -*-
"""静态锁:迁移里不许拿系统目录的**渲染文本**做全等比较。

来历:2026-09-01 P0。7 个新迁移(040/041/044/046/051/052/054)用
`pg_get_constraintdef()` 的渲染文本跟写死的字符串比,而 dump→restore 会把
`IN (...)` 形的 CHECK 重渲染 ⇒ **首次部署过、还原副本上重放必炸**。

🔴 本仓三天前就有正确先例:`db/migration_035`(08-18)诊断过同一件事并改用语义比对,
   注释写得很清楚。040–054(08-21~26)是在它之后写的,又把病带了回来。
   **注释传不出去,门才传得出去** —— 所以有了这把锁。

分母**机械取自** `db/migration_manifest.MIGRATIONS`,不手列。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

from catalog_render_comparison import find_render_text_comparisons  # noqa: E402
from db.migration_manifest import MIGRATIONS  # noqa: E402

# ── 冻结的存量例外 ───────────────────────────────────────────────────────────
# 🔴 例外只冻结**已知的那几处**:值 = 该文件当前的违规点数。
#    多一处就红 —— 「文件在例外名单里」不等于「这个文件随便写」。
#    两个都是 2026-07 的存量,机制上同病,但实测**不漂**:
#    2026-09-01 在还原副本上量过 869 条 CHECK,只有 `IN (...)`/varchar-cast 那一形会漂
#    (29 条);这两处比的是 `CHECK (x >= 0)` 这类纯数值式与布尔 default,渲染稳定。
#    ⇒ 不阻断本车,但它们仍是脆的(跨 PG 大版本渲染细节会变),记 post-train。
#: 🔴 2026-09-01 二次收口:4 处/2 文件 → **12 处/5 文件**。
#:    差的那三个文件不是新写的,是**我的检测器整族漏掉的**:索引那半的真实写法是
#:    `regexp_replace(lower(pg_get_indexdef(i.oid)),'\s','','g')` —— 最外层是包装函数,
#:    而我第一版只判「最外层就是渲染函数」,污染穿不过包装。
#:    是顺着 C 问「索引谓词要不要也加一发」才查出来的。
#:    实测这 12 处**当前都不会炸**:新库上 295 个 partial 索引里 13 个会漂,
#:    与这 5 个文件点名的 39 个对象**交集为 0**;它们比的是纯数值式/布尔 default/
#:    不漂的索引定义。⇒ 不阻断,记 post-train。
LEGACY_EXEMPT: dict[str, int] = {
    "scripts/migration_dealer_inventory_resale_2026_07_15.sql": 2,
    "scripts/migration_direct_service_refund_agreements_2026_07_15.sql": 2,
    "scripts/migration_geo_observation_v1_2026_07_17.sql": 3,
    "scripts/migration_article_generation_task_state_2026_07_21.sql": 3,
    "scripts/migration_organization_short_code_2026_07_28.sql": 2,
}
LEGACY_EXEMPT_SIZE = 5   # 例外条目数本身也锁住:新增例外必须有人做一次决定


def _read(rel: str) -> str | None:
    p = REPO / rel
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8-sig")


def test_mrx_01_manifest_has_no_new_render_text_comparison():
    """🔴 主判据:manifest 里的迁移,除冻结例外外,不许比渲染文本。"""
    assert MIGRATIONS, "分母是空的 —— 这条判据等于没跑"
    offenders: dict[str, list[str]] = {}
    scanned = 0
    for rel in MIGRATIONS:
        src = _read(rel)
        if src is None:
            continue
        scanned += 1
        hits = find_render_text_comparisons(src)
        if hits:
            offenders[rel] = hits
    assert scanned >= 100, f"只扫到 {scanned} 个迁移文件 —— 分母不对"

    unexpected = {k: v for k, v in offenders.items() if k not in LEGACY_EXEMPT}
    assert not unexpected, (
        "新增「拿渲染文本做全等比较」:\n"
        + "\n".join(f"  {k}\n    " + "\n    ".join(v) for k, v in unexpected.items())
        + "\n改法:比**语义身份**(见 db/migration_035 的先例与 defgeo_ident_* 系列),"
          "不要比 pg_get_*def() 的渲染输出。"
    )
    # 例外文件本身也不许再长:多一处就红
    for rel, n in LEGACY_EXEMPT.items():
        got = len(offenders.get(rel, []))
        assert got == n, (
            f"{rel} 的违规点从 {n} 变成 {got} —— 例外只冻结已知那几处,"
            f"不是给这个文件发的免死金牌")


def test_mrx_02_exempt_list_size_is_locked():
    """例外**条数**锁住:加一个例外要有人做一次决定,而不是顺手加。"""
    assert len(LEGACY_EXEMPT) == LEGACY_EXEMPT_SIZE, (
        f"例外条目数从 {LEGACY_EXEMPT_SIZE} 变成 {len(LEGACY_EXEMPT)}")


# ── 检测器自证:没有这几条,上面那把锁可能是恒真的 ──────────────────────────

def test_mrx_03_detector_flags_the_pre_fix_shape():
    """正样本:修前那一形**必须**被抓到。

    夹具是 `git show 3edfc0031:db/migration_040_...` 抽出来的**真代码**,
    不是我手写的 —— 手写对客样本等于判据在跟自己对话。

    🔴 摘录范围是原文件的 **235-265 行**(不是 235-266)。原第 266 行是空行,
       带上它会让 `git diff --check` 报 `new blank line at EOF` 而发车门要 0。
       修法**不是删掉那一行**(那会让夹具不再是逐字摘录),是把范围收到 265 ——
       仍与 `sed -n '235,265p'` 的输出逐字节相同,且末行是 `END LOOP;`。
       复现:`git show 3edfc0031:db/migration_040_... | sed -n '235,265p' | cmp - <本文件>`
    """
    src = _read("tests/defgeo_migration_replay_2026_09_01/fixtures/"
                "positive_pre_fix_040_excerpt.sql")
    assert src, "正样本夹具不见了"
    hits = find_render_text_comparisons(src)
    assert hits, "修前那一形没被抓到 —— 这把锁是恒真的"
    assert any("actual" in h for h in hits), hits


@pytest.mark.parametrize("rel", [
    "db/migration_040_defgeo_question_plans_2026_08_21.sql",
    "db/migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    "scripts/defgeo_readiness_gen.py",
])
def test_mrx_04_detector_does_not_flag_the_correct_shapes(rel):
    """负样本:现役正确写法(C 的语义身份 / 035 的语义包含 / 生成器本体)不许被误报。

    🔴 会误报的锁最后一定被关掉。第一版按「渲染函数后 400 字符内的 INTO」标污染,
       在 07 月那批迁移上把 `SELECT count(*) … INTO mismatch_count` 全误报了。
    """
    src = _read(rel)
    assert src, f"{rel} 不见了"
    assert not find_render_text_comparisons(src), (
        f"{rel} 被误报 —— 它用的是语义比对,不是渲染文本")


def test_mrx_05_detector_flags_the_pre_fix_files_in_history():
    """更硬的正样本:直接从 git 取修前全文,两个文件都必须被抓到。

    git 取不到时**不许 skip** —— 一个跑不了的控制组不是控制组。
    """
    got = {}
    for rel in ("db/migration_040_defgeo_question_plans_2026_08_21.sql",
                "db/migration_046_defgeo_monitoring_lineage_2026_08_22.sql"):
        r = subprocess.run(["git", "show", f"3edfc0031:{rel}"], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        assert r.returncode == 0, f"取不到修前版本 {rel}:{r.stderr[:200]}"
        got[rel] = find_render_text_comparisons(r.stdout)
    for rel, hits in got.items():
        assert hits, f"修前的 {rel} 没被抓到 —— 锁没牙"


# ── 交付面:提交成功 ≠ 东西进去了 ────────────────────────────────────────────
#
# 🔴 来历(本包自己被咬):`.gitignore:145 *.sql`(本意挡 DB dump)**静默**吞掉了
#    `fixtures/positive_pre_fix_040_excerpt.sql`。我在自己的树上 10 条全绿 ——
#    因为那个文件以 **untracked** 形式躺在盘上;换任何一棵新树(含已发车的生产尖)
#    `test_mrx_03` 当场死,而它正是「证明静态锁不是恒真」的那一条自证臂。
#    臂死了之后 mrx_01/02 是不是恒真**无人知道**,而它们还在报绿。
#
#    同一条规则两周内咬了三次:2026-08-10 orphanmon(当时只加了**单文件**豁免)、
#    2026-09-01 窗口C 的普查 SQL、2026-09-02 本包。目录级豁免只是止血;
#    真正防复发的是下面这把锁 —— 它盖住 `.sql` 之外的**所有** ignore 规则。
#
# 🔴 探针不许用 `git check-ignore` 的退出码:negation 规则也算 match,它照样退 0。
#    唯一可信的问法是 `git ls-files --error-unmatch <path>`。

def _is_tracked(rel: str) -> bool:
    r = subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                       cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0


def _disk_files() -> list[str]:
    """本包盘上的**全部**文件(机械枚举,排 __pycache__)。"""
    out = []
    for p in sorted(HERE.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out.append(p.relative_to(REPO).as_posix())
    return out


def test_mrx_06_every_file_in_this_package_is_tracked_by_git():
    """🔴 判据包里盘上的每个文件都必须被 git 跟踪。

    不按「引用到的夹具路径」扫 —— 那要我手写一套路径识别规则,漏一种写法就漏一族。
    直接拿**盘上全集**当分母:任何 ignore 规则吞掉任何一个文件,这条都会红。
    """
    files = _disk_files()
    assert len(files) >= 6, f"只枚举到 {len(files)} 个文件 —— 分母不对,这条判据等于没跑"
    missing = [f for f in files if not _is_tracked(f)]
    assert not missing, (
        "这些文件在盘上但**不在 git 里** —— 换一棵新树就没了:\n  "
        + "\n  ".join(missing)
        + "\n用 `git add -f` 加进去,并复核 `git ls-files <path>`;"
          "别看 `git commit` 的输出,它会说 'nothing to commit' 而那是告警不是好消息。")


def test_mrx_07_the_tracking_probe_can_say_no():
    """自证:上面那把锁不是恒真 —— 探针对一个不存在的路径必须返 False。"""
    assert _is_tracked("tests/defgeo_migration_replay_2026_09_01/"
                       "test_no_catalog_render_text_comparison.py"), "对真文件应返 True"
    assert not _is_tracked("tests/defgeo_migration_replay_2026_09_01/"
                           "__no_such_file_ever__.sql"), "对虚构路径应返 False"


def test_mrx_08_the_positive_fixture_specifically_survives_a_fresh_checkout():
    """🔴 点名钉住被咬的那一个:正样本夹具必须在 git 里。

    与 mrx_06 重复?不重复 —— mrx_06 的分母是「盘上有什么」,
    而这条钉的是「**这一个**必须存在」。哪天有人把夹具删了,
    mrx_06 会绿(盘上没有它,自然不缺跟踪),只有这条会红。
    """
    rel = ("tests/defgeo_migration_replay_2026_09_01/fixtures/"
           "positive_pre_fix_040_excerpt.sql")
    assert _is_tracked(rel), f"{rel} 不在 git 里 —— test_mrx_03 在新树上会死"
    assert (REPO / rel).exists(), f"{rel} 在 git 里但盘上没有"
