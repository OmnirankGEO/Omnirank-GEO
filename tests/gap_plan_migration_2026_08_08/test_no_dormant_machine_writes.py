"""命名边界锁 —— **打在包的净 diff 上**,不是打在 fixture 建出来的库上。

🔴 这条锁的第一版是重言式,Owner 当场点破:
   原版断言「跑完 029 之后库里没有 geo_article_* 表」。可 fixture 只跑 PRE_DDL + 029,
   那两样本来就一张 geo_article_* 都不建 —— **断言恒真,删掉整段实现它也绿**。
   恒真的锁比没有锁更坏:它让人以为这条边界有人守着。

   真正要守的命题是「**本包的改动**没有去写那台全黑的 closed-loop 机器」,
   而那是关于 **diff** 的命题,不是关于**运行时库**的命题。判据得打在同一个东西上。

底(净 diff 的起点)默认取生产尖;可用 GAP_PLAN_DIFF_BASE 覆盖。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# ══════════════════════════════════════════════════════════════════════
# 底 = **本包净增量**的起点(Review-CTO 2026-08-18 裁定)
#
# 🔴 原来写死 2026-08-08 的生产尖。那对 gap_plan 包自己成立,但任何**后来的**
#    分支跑这条守卫时,`BASE..HEAD` 会横跨这中间所有包的改动 ——
#    2026-08-18 实测:91 条命中里 70 条是 inventory / orphanmon 两个包带进来的
#    pg_dump 快照,与被审的包毫无关系。判据扫的不是"本包写了什么",
#    而是"这十天里全公司写了什么",于是每个后来的包都得替别人的文件背书。
#
# 🔴 改法:底取**与生产线的 merge-base**,即本分支从主线岔出去的那一点。
#    它随分支自动正确,不需要每个包来改这个常量 —— 写死的常量必然过期,
#    而过期的底会静默地把判据的作用域越拉越大。
#    `GAP_PLAN_DIFF_BASE` 保留:需要精确复现某次审计时可以钉死。
# ══════════════════════════════════════════════════════════════════════

#: 生产线候选,按优先级。第一个能解析出来的胜出。
MAINLINE_REFS = ("origin/main", "main", "origin/master", "master")


def _resolve_default_base() -> str:
    """与生产线的 merge-base。取不到就**抛**,不静默回落到某个更老的点 ——
    底越老作用域越大,而"作用域悄悄变大"正是这次要修的病。
    """
    for ref in MAINLINE_REFS:
        probe = subprocess.run(["git", "rev-parse", "--verify", "-q", ref],
                               cwd=ROOT, capture_output=True, text=True)
        if probe.returncode != 0:
            continue
        mb = subprocess.run(["git", "merge-base", "HEAD", ref],
                            cwd=ROOT, capture_output=True, text=True)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
    raise RuntimeError(
        f"解析不出生产线 merge-base(试过 {MAINLINE_REFS});"
        "请用 GAP_PLAN_DIFF_BASE 显式指定本包的底。"
    )


BASE = os.environ.get("GAP_PLAN_DIFF_BASE") or _resolve_default_base()

# 那台机器的表(scripts/migration_geo_article_closed_loop_v1_2026_07_20.sql 建的),
# 加上它在既有表上打的钉子列。
DORMANT_TABLES = (
    "geo_article_plan_outbox",
    "geo_article_plan_runs",
    "geo_article_delivery_slots",
    "geo_article_delivery_slot_events",
    "geo_article_contract_revisions",
    "geo_article_target_question_snapshots",
)

# 写类语句 + 表名。只抓**写**,不抓读 —— 读它是允许的(将来合流要读),写它才是越界。
_WRITE_RE = re.compile(
    r"\b(insert\s+into|update|delete\s+from|alter\s+table|drop\s+table|truncate)\b"
    r"[^;\n]*?\b(" + "|".join(DORMANT_TABLES) + r")\b",
    re.IGNORECASE,
)


def scan_added_lines(diff_text: str) -> list[str]:
    """只看 diff 里 **新增**的行(+ 开头),命中写类语句就返回。

    (保留原签名:下面几条自证/对照判据直接喂字符串,不需要文件归属。)
    """
    return [body for _path, body in scan_added_lines_with_path(diff_text)]


def scan_added_lines_with_path(diff_text: str) -> list[tuple[str, str]]:
    """带**文件归属**的扫描结果 `[(path, line), ...]`。

    🔴 为什么必须带归属:命题从「本包一行都不许写」搬家成了
       「只有 RFC 批准的那几个点可以写」。判「在不在批准集合里」必须知道
       这一行来自哪个文件 —— 只看语句文本会让"批准的写法"在任意文件里都放行,
       那才是真正的洞。
    """
    hits: list[tuple[str, str]] = []
    current = "<unknown>"
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            current = raw[2:] if raw.startswith("b/") else raw
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        if _WRITE_RE.search(body):
            hits.append((current, body.strip()))
    return hits


# ══════════════════════════════════════════════════════════════════════
# RFC 2026-08-17 批准集合(窄 RFC R1/R2/R3 · Review-CTO 已批)
#
# 🔴 命题搬家了,断言跟着搬,**守卫不退役**:
#    旧命题 =「本包一行都不许写那台全黑的机器」;
#    新命题 =「只有 RFC 明确批准的那几个点可以写,集合外一律仍红」。
#    直接删守卫、或写成"某个文件整体豁免",都会让边界重新变成没人守。
#
# 🔴 每一项都限定到 **(文件, 允许的表, 允许的动词)** 三元组。
#    不写成"035 这个文件随便改" —— 那样在同一个文件里偷偷 INSERT 一条
#    plan_run 也会放行,而那正是"第二个生产者"这件事本身。
# ══════════════════════════════════════════════════════════════════════

#: 槽位渠道化真正动到的两张表(RFC R1 的作用面)。另外四张仍然一个字都不许写。
_RFC_SLOT_TABLES = frozenset({
    "geo_article_delivery_slots",
    "geo_article_delivery_slot_events",
})

APPROVED_SITES: dict[str, dict[str, object]] = {
    # ① 迁移 035:只做 DDL(加列 / 加约束 / 原子替换事件 CHECK),**零 DML**。
    "db/migration_035_geo_image_note_slot_channel_2026_08_18.sql": {
        "tables": _RFC_SLOT_TABLES,
        "verbs": frozenset({"alter table"}),
        "why": "RFC R1 槽位渠道化 DDL(additive 列 + 事件 CHECK 原子替换)",
    },
    # ② 图文侧**唯一**写入者。RFC R3 的核心就是"只有一个写入方"。
    "services/geo_douyin/delivery_slots.py": {
        "tables": _RFC_SLOT_TABLES,
        "verbs": frozenset({"insert into", "update"}),
        "why": "RFC R3 图文 lane 唯一 slot writer(事件追加 + 投影 CAS)",
    },
    # ③ 本包的 PG16 行为测试:在**一次性抛弃库**上造夹具。
    #    夹具要建 contract_revision / plan_run 作为槽位的父行,所以表面比 ①② 宽;
    #    但它仍然是**逐文件登记**的,新加一个测试文件写这台机器照样红。
    "tests/geo_image_note_2026_08_17/test_wp1_slot_channel_pg16.py": {
        "tables": frozenset(DORMANT_TABLES),
        "verbs": frozenset({"insert into", "update"}),
        "why": "抛弃库夹具(CREATE DATABASE → DROP DATABASE),不触碰任何真实库",
    },
    # ④ [2026-08-18 退役] 原来还登记着两份别包带入的 pg_dump 快照。
    #    底改成"本包净增量"之后它们根本不在 diff 里了,登记项随之删除 ——
    #    留着就是恒真白名单,`test_every_approved_site_is_actually_exercised`
    #    会当场把它抓出来。**修好了根因就要把绷带拆掉。**
}

_INS = "INSERT" + " INTO"        # 拆开写,自身不构成可被扫描器命中的字面量
_UPD = "UP" + "DATE"
_SEL = "SELECT" + " * FROM"
_ALT = "ALTER" + " TABLE"
_DEL = "DELETE" + " FROM"


_VERB_RE = re.compile(
    r"\b(insert\s+into|update|delete\s+from|alter\s+table|drop\s+table|truncate)\b", re.I)


def classify_hit(path: str, line: str) -> str | None:
    """返回 None = 在批准集合内;返回字符串 = 越界原因(用于报错文案)。"""
    site = APPROVED_SITES.get(path)
    if site is None:
        return f"文件不在 RFC 批准集合内:{path}"
    verb_match = _VERB_RE.search(line)
    verb = (verb_match.group(1).lower() if verb_match else "").replace("  ", " ")
    verb = re.sub(r"\s+", " ", verb)
    if verb not in site["verbs"]:                      # type: ignore[operator]
        return f"{path} 只批准了 {sorted(site['verbs'])},出现了 {verb!r}"
    tables = {t for t in DORMANT_TABLES if re.search(rf"\b{t}\b", line, re.I)}
    outside = tables - set(site["tables"])             # type: ignore[arg-type]
    if outside:
        return f"{path} 只批准了 {sorted(site['tables'])},碰了 {sorted(outside)}"
    return None


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout


@pytest.fixture(scope="module")
def package_diff() -> str:
    # 🔴 第 0 关 · 判据可用性:底取不到时所有 diff 类检查都会「空即通过」,
    #    那比漏检更坏(preflight.sh 同一条纪律)。取不到就 fail,不 skip。
    try:
        kind = _git("cat-file", "-t", BASE).strip()
    except subprocess.CalledProcessError as exc:      # noqa: PERF203
        pytest.fail(f"底 {BASE} 在本仓不可达,判据不可用:{exc}")
    assert kind == "commit", f"底 {BASE} 不是 commit(得到 {kind})"
    return _git("diff", f"{BASE}..HEAD")


def test_package_diff_is_not_empty(package_diff):
    """反向对照:diff 是空的话,下面那条扫描什么都没扫,等于恒真。"""
    assert package_diff.strip(), (
        f"本包相对 {BASE[:12]} 的净 diff 为空 —— 扫描无对象,判据零判别力"
    )


def test_writes_into_the_dormant_machine_stay_inside_the_rfc_approved_set(package_diff):
    """🔴 写那台全黑机器的语句,必须**逐条**落在 RFC 2026-08-17 批准集合内。

    那台机器 2026-08-08 生产实测 4 表全 0 行 + ARTICLE_PLAN_* flag 全 False,
    是 outbox + 不可变 plan + append-only 投影的事件溯源设计。
    塞第二个**不受控**的写入方进去 = 把它的不变式交给两个互不知情的生产者。

    2026-08-17 窄 RFC(R1/R2/R3)批准了图文 lane 的槽位渠道化:
    035 的 DDL + 唯一 slot writer。命题因此从「一行都不许写」搬家成
    「只有批准的那几个点可以写」—— **断言跟着搬,守卫不退役**。
    """
    violations = [
        f"{path}: {line}\n      └─ {reason}"
        for path, line in scan_added_lines_with_path(package_diff)
        if (reason := classify_hit(path, line))
    ]
    assert not violations, (
        "有写 closed-loop 机器的语句落在 RFC 批准集合之外:\n  "
        + "\n  ".join(violations)
    )


def test_base_is_an_ancestor_of_head():
    """🔴 底必须是 HEAD 的祖先。不是祖先时 `BASE..HEAD` 的语义是"两边的对称差",
    扫出来的东西既不是本包写的也不是别人写的 —— 判据会指向一堆无法解释的行。
    """
    probe = subprocess.run(["git", "merge-base", "--is-ancestor", BASE, "HEAD"],
                           cwd=ROOT, capture_output=True, text=True)
    assert probe.returncode == 0, f"底 {BASE[:12]} 不是 HEAD 的祖先,净 diff 无意义"


def test_out_of_package_snapshots_are_no_longer_swept(package_diff):
    """🔴 底改成"本包净增量"之后,**别的包带进来的文件必须归零命中**。

    这是这次改底的判别信号:改之前 91 条命中里 70 条来自
    inventory / orphanmon 两个包的 pg_dump 快照;改之后它们应当根本不在 diff 里。
    如果它们还在,说明底仍然太老 —— 判据会继续替别人的文件背书。
    """
    out_of_package = (
        "tests/inventory_distribution_chain_2026_08_12/prod_schema_2026-08-12.sql",
        "tests/orphanmon_2026_08_10/prod_schema_snapshot.sql",
    )
    swept = sorted({path for path, _line in scan_added_lines_with_path(package_diff)
                    if path in out_of_package})
    assert not swept, (
        f"底 {BASE[:12]} 仍然扫到了包外文件(底太老,作用域超出本包):\n  "
        + "\n  ".join(swept)
    )


def test_every_approved_site_is_actually_exercised(package_diff):
    """🔴 反恒真:批准集合里的每一项都必须**真的被本次 diff 命中**。

    没有这一条,批准集合就可以被塞成一张"未来可能会用到"的清单 ——
    那正是 Review 明令禁止的"恒真白名单"。用不到的批准项要么删掉,
    要么说明它为什么还在(而不是默默留着当挡箭牌)。
    """
    touched = {path for path, _line in scan_added_lines_with_path(package_diff)}
    stale = set(APPROVED_SITES) - touched
    assert not stale, (
        "批准集合里这些项在本次 diff 里一次都没被命中(恒真白名单):\n  "
        + "\n  ".join(sorted(stale))
    )


def test_unauthorized_write_is_still_red():
    """🔴 成对判据 · 越界必红。三种越界形态各打一次:

      ① 批准集合外的**文件**里写批准的表;
      ② 批准文件里动了**没批准的表**(另外四张仍然一个字都不许写);
      ③ 批准文件里用了**没批准的动词**(035 只批 DDL,出现 DML 就是第二个生产者)。
    """
    approved_file = "db/migration_035_geo_image_note_slot_channel_2026_08_18.sql"
    writer = "services/geo_douyin/delivery_slots.py"

    # ① 别的文件里做同样的 ALTER
    assert classify_hit(
        "db/migration_099_someone_else.sql",
        f"{_ALT} geo_article_delivery_slots ADD COLUMN sneaky TEXT;")

    # ② 批准文件里碰没批准的表
    assert classify_hit(
        approved_file,
        f"{_ALT} geo_article_plan_runs ADD COLUMN sneaky TEXT;")

    # ③ 批准文件里出现 DML
    assert classify_hit(
        approved_file,
        _INS + " geo_article_delivery_slots (delivery_slot_key) VALUES ('x');")

    # ③' 唯一写入者里出现 DELETE(它只批了 insert/update)
    assert classify_hit(
        writer, f"{_DEL} geo_article_delivery_slot_events WHERE id = 1")


def test_approved_write_is_green():
    """反向对照:批准形态必须真的放行,否则上一条只是"什么都拒"。"""
    assert classify_hit(
        "db/migration_035_geo_image_note_slot_channel_2026_08_18.sql",
        f"{_ALT} geo_article_delivery_slots ADD COLUMN delivery_channel TEXT;") is None
    assert classify_hit(
        "services/geo_douyin/delivery_slots.py",
        '        "' + _INS + ' geo_article_delivery_slot_events(event_key, "') is None


# ══════════════════════════════════════════════════════════════════════
# 🔴🔴 自指陷阱(Review 2026-08-08 干净检出实跑抓到 · 20/21)
#
# 本文件**自己就在包的净 diff 里**(新增文件,整份都是 + 行)。
# 第一版把对照样例写成了字面量,scan_added_lines 剥掉 diff 前缀后读到的正是它
# → 判成违规写入 → 包扫描恒红(三条命中全是我自己的样例)。
#
# 🔴 更该记的是**我为什么没发现**:我在 `git add` 之前跑的套件。
#    那时文件还不在 HEAD 里,`git diff BASE..HEAD` 自然扫不到它 —— 21/21 全绿。
#    一提交它就进了净 diff,开始扫自己。**我验的状态不是我交的状态。**
#    → 带 diff 类判据的锁,必须**提交之后**再跑一次;干净检出实跑不是形式主义。
#
# 修法:样例一律**运行时拼装**,源码里不留"写语句 + 表名"同行的字面量。
#      🔴 **没有**采用"把本文件排除出扫描面"那种改法 —— 那会在扫描面上开一个洞;
#         洞一开,将来真有人往测试文件里塞一条写入就再也扫不到。
# ══════════════════════════════════════════════════════════════════════

def test_the_scanner_actually_fires_on_a_planted_write():
    """🔴 正向对照:证明这套扫描不是恒绿。

    没有这一条,上面那句 assert 在「_WRITE_RE 永远不匹配」的实现下也会绿 ——
    而那正是第一版重言式锁的同一种病。
    """
    planted = "\n".join([
        "+++ b/db/whatever.sql",
        f"+{_INS} geo_article_delivery_slots (delivery_slot_key) VALUES ('x');",
        f"""+    cur.execute("{_UPD} geo_article_plan_runs SET status='done'")""",
    ])
    hits = scan_added_lines(planted)
    assert len(hits) == 2, hits


def test_the_scanner_does_not_fire_on_reads_or_on_removed_lines():
    """反向对照的反向:读它、以及**删除**它的行,都不该报红。

    只抓写:将来合流一定要读那台机器;把读也拦了,这条锁会逼人绕开它。
    """
    benign = "\n".join([
        "+++ b/services/x.py",
        f"+    cur.execute('{_SEL} geo_article_delivery_slots WHERE quote_id=%s')",
        f"-{_INS} geo_article_plan_runs (run_key) VALUES ('x');",   # 删除行,不是新增
        "+# 注释里提到 geo_article_plan_outbox 不算写",
    ])
    assert scan_added_lines(benign) == []


def test_this_lock_file_does_not_trip_its_own_scanner():
    """🔴 自指哨兵:把本文件全文当成"全是新增行"喂给扫描器,必须零命中。

    存在的理由:上面那个陷阱**改一行字面量就会重新踩回去**。
    与其靠人记得"别写字面量",不如让它自己报错 —— 而且报出来的是
    "锁文件自己带了字面量",不是那句让人一头雾水的"包里有违规写入"。
    """
    source = Path(__file__).read_text(encoding="utf-8")
    as_added = "\n".join("+" + line for line in source.splitlines())
    hits = scan_added_lines(as_added)
    assert not hits, (
        "本锁文件自己带了会被扫描器命中的字面量(自指假阳性会让包扫描恒红):\n  "
        + "\n  ".join(hits)
    )
