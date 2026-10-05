# -*- coding: utf-8 -*-
"""媒体桶换算的**单点**:一个交付槽,按这类媒体去发要几条。

WO_225-c1 §8.1 · Owner 2026-09-15:低价媒体 5 倍、客户价不变、文案只说「约 N 条」。

═══════════════════════════════════════════════════════════════════
🔴 这是**运营工作量口径**,不是效果承诺

  裁定书 v2 §1.2 / §2.2 / §7 三处明令:不得说「两篇覆盖内容等于一篇重点媒体」、
  不得签发「某层媒体等于另一层几篇的效果系数」。那说的是**效果等价**;
  这里说的是「按这类媒体发,填满一个槽通常要打几条」。
  两件事必须分开写 —— 所以字段叫 `posts_per_slot_bps`(条/槽),
  对外文案只许「约 N 条」,**禁**「等于 / 抵 / 倍 / 效果」(§8.8 文案锁)。

  `services/citation_domain_weights.py:6-10` 记着一条硬事实:
  我方发布 185 篇仅 1 篇被引。所以「多发几十条更容易上榜」没有任何证据,
  本模块也不提供任何能被那样解读的数。

🔴 为什么整数 bps,不用小数
  10000 bps = 一槽一条。比较与求和一律整数,禁 float ——
  与 08_billing §3.3 同一条纪律。

🔴 老单不冻结:钱不读它
  快照里有 `media_mix.conversion_version` 就用快照那一版;没有(老单)= 用**当前版**。
  这样安全,因为**价不读这张表**(方案甲:价 = 槽数 × 单篇成本 × 系数,一分不动),
  它只进**展示**与**写作默认条数**。若哪天价开始读它,这条必须翻面成「老单冻结」——
  那时就是 08_billing §7.5「历史不重算」的范围了。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

logger = logging.getLogger("GEO-MediaSlotConversion")

#: 三个桶,逐字复用 035 迁移与 `services/quote_media_mix.py:300-302` 的名字。
#: 🔴 不另起一套:同一个概念两套词表,迟早有一处对不上而没人报错。
BUCKET_ANCHOR = "focus_media_anchor"
BUCKET_COVERAGE = "industry_platform_coverage"
BUCKET_DOUYIN = "douyin_doubao_only"
BUCKETS = (BUCKET_ANCHOR, BUCKET_COVERAGE, BUCKET_DOUYIN)

#: 一槽一条。**所有取不到值的路径都回落到它** —— 老单、NULL 桶、表读不出来。
ONE_SLOT_BPS = 10000

#: v1 初值(Owner 2026-09-15「低价媒体 5 倍」)。
#: 🔴 播种在这里、不在迁移里:迁移体内禁 DML(本仓红线)。
#:   `ensure_seeded()` 幂等,重放不会覆盖之后的新版本。
SEED_V1: Dict[str, int] = {
    BUCKET_ANCHOR: 10000,     # 重点媒体:一槽一条
    BUCKET_COVERAGE: 50000,   # 行业与平台覆盖:一槽约 5 条
    BUCKET_DOUYIN: 50000,     # 抖音图文:一槽约 5 条
}

#: 交付口径。`self_media` 默认 —— Owner 说的「低价媒体」是常态。
PERSPECTIVE_SELF_MEDIA = "self_media"
PERSPECTIVE_PORTAL = "portal"
PERSPECTIVE_MIXED = "mixed"
PERSPECTIVES = (PERSPECTIVE_SELF_MEDIA, PERSPECTIVE_PORTAL, PERSPECTIVE_MIXED)
DEFAULT_PERSPECTIVE = PERSPECTIVE_SELF_MEDIA


def round_half_up(value: float) -> int:
    """四舍五入到整数 —— 与 `services/quote_media_mix.round_half_up` 同法。

    🔴 前后端必须同一套取整:`frontend/src/lib/quoteMediaMix.ts` 与这里对拍,
       否则同一个组合在两边显示成不同的条数,而没有任何东西会报错。
    """
    import math
    return int(math.floor(float(value) + 0.5))


def _release(conn) -> None:
    """本模块所有出库路径的**唯一出口**:先 rollback(防御),再还池。

    🔴🔴 **订正(2026-09-15,Review 推翻 + 我做判别实验确认)**:
       这里的 rollback **不是**任何一条红的修法,它是纯防御。
       我原先写的是「不 rollback 就把脏连接还回池,下一个借到它的人全失败」——
       **错的**:`psycopg2.pool.AbstractConnectionPool._putconn` 自己就兜了,
       `len(pool) < minconn` 时非 IDLE 连接先 rollback 再回池,超出的直接 close 不回池。
       判别实验:只撤掉这里的 rollback、其余不动,同库同序跑 40 个受影响判据文件,
       **45 红 = 45 红,失败集逐条相同** ⇒ 它什么都没修。
       当时那条红(`relation "articles" does not exist`)真正的修法是
       `resolve_for_quote` 改用**调用方的游标**(读对了 schema);
       症状本来就对不上 —— 事务被判废该报 `InFailedSqlTransaction`,不是表不存在。

    🔴 那为什么还留着:`get_connection()` 不保证永远是池化连接(`db/connection.py`
       有非池化分支),那种情况下池兜不到。代价为零,留作防御;
       但**别再拿它当某条红的解释** —— 借来的游标要靠 `_read_with_cursor` 的 SAVEPOINT,
       池兜不到别人的事务,那一条才是有实证的。
    🔴 rollback 本身也可能抛(连接已经断),所以它也在 try 里 ——
       但**不能**因此跳过 close:那才是真的漏连接。
    """
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def ensure_seeded(conn=None) -> None:
    """幂等播种 v1。与 `db/social_preferences_db.ensure_schema()` 同法。

    🔴 `ON CONFLICT DO NOTHING`:重放不覆盖。Owner 之后发的新版本是**新的 version**,
       本函数只管 v1 那三行在不在。
    """
    own = conn is None
    try:
        if own:
            from db.connection import get_connection
            conn = get_connection()
        cur = conn.cursor()
        for bucket, bps in SEED_V1.items():
            cur.execute(
                "INSERT INTO media_slot_conversion_versions "
                "(version, bucket, posts_per_slot_bps, created_by, note) "
                "VALUES (1, %s, %s, 'WO_225-c1', %s) "
                "ON CONFLICT (version, bucket) DO NOTHING",
                (bucket, int(bps), "运营口径:按该类媒体发布,填满一个交付槽通常需要的条数"))
        if own:
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - 播不进去不该让读取整条挂掉
        logger.warning("[media_slot_conversion] 播种 v1 失败:%s", exc)
    finally:
        if own:
            _release(conn)


def _fetch_version(version: int) -> Dict[str, int]:
    """读一版的三个桶。**任何失败都回空 dict**,由调用方回落 10000。

    🔴 `get_connection()` 必须在 try **里面**:它本身就会抛(库没起 / DSN 空 /
       判据进程根本没有库)。放在外面等于让一张系数表把整条报价链带崩。
    """
    conn = None
    try:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT bucket, posts_per_slot_bps FROM media_slot_conversion_versions "
            "WHERE version = %s", (int(version),))
        return {str(r["bucket"]): int(r["posts_per_slot_bps"]) for r in (cur.fetchall() or [])}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[media_slot_conversion] 读第 %s 版失败:%s", version, exc)
        return {}
    finally:
        _release(conn)


def current_version() -> int:
    """最大的 version。表空 ⇒ 播种后再读;仍空 / 读不到 ⇒ 1(配合 `ONE_SLOT_BPS` 回落)。

    🔴 与 `_fetch_version` 同一条:`get_connection()` 在 try **里面**。
       返回 1 不代表「就是第 1 版」—— 它配合 `_fetch_version` 回空 ⇒
       `resolve_for_quote` 走 fallback(三桶全 10000 = 今天的行为)。
    """
    conn = None
    try:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT MAX(version) AS v FROM media_slot_conversion_versions")
        row = cur.fetchone() or {}
        v = row.get("v")
        if v:
            return int(v)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[media_slot_conversion] 读当前版失败:%s", exc)
        return 1
    finally:
        _release(conn)
    ensure_seeded()
    try:
        from db.connection import get_connection as _gc
        c2 = _gc()
        try:
            cur = c2.cursor()
            cur.execute("SELECT MAX(version) AS v FROM media_slot_conversion_versions")
            row = cur.fetchone() or {}
            return int(row.get("v") or 1)
        finally:
            c2.close()
    except Exception:  # noqa: BLE001
        return 1


_SNAPSHOT_SQL = (
    "SELECT pricing_snapshot FROM quote_pricing_snapshots "
    " WHERE quote_id = %s ORDER BY version DESC LIMIT 1")


#: SAVEPOINT 名字。固定名可以嵌套复用(同一层重复 SAVEPOINT 同名 = 覆盖),
#: 本函数不递归,所以不需要唯一名。
_SAVEPOINT = "wo225_media_mix"


def _needs_savepoint(cursor) -> bool:
    """这条游标所在的连接需要 SAVEPOINT 吗?

    🔴 先探再发,**不要「先失败一次再回落」**(Review 2026-09-15 复审指出):
       autocommit 连接上 `SAVEPOINT` 本身会报 `can only be used in transaction blocks`,
       try/except 虽然接得住,但每次调用都多**一个往返**和**一行 PG ERROR**。
       一堆良性 ERROR 会盖掉真 ERROR —— 这条本身就是本仓记过的病。

    · autocommit = True  ⇒ 不需要:每条语句自成事务,失败不会波及后续;
    · autocommit = False ⇒ 需要:失败会把调用方整条事务判成 aborted;
    · 探不到(游标没有 `connection`,或读属性抛)⇒ **按需要处理**。
      回落方向选「多发一条 SAVEPOINT」而不是「不发」:多发最坏是一行 ERROR,
      不发最坏是把调用方的事务打废。

    🔴 生产实测(2026-09-15):`db/connection.py:180-181` 每次 getconn **强制**
       `autocommit = False`(P0-5 连接污染根治),两个调用方
       (`api/selection_api.py:4528` 的 `_read`、`services/gap_operation_plan.py:228`
       的 `compute_capacity`)都走这个池且没再设 True ——
       所以**生产走的是需要 SAVEPOINT 那条腿**,探针在生产恒 True。
       探针存在的意义是给 autocommit 的调用方(判据夹具是其一)省掉那行 ERROR。
    """
    try:
        conn = getattr(cursor, "connection", None)
        if conn is None:
            return True
        return not bool(conn.autocommit)
    except Exception:  # noqa: BLE001
        return True


def _read_with_cursor(cursor, quote_id: int) -> Dict:
    """用**调用方的**游标读快照,失败不许溢出到它的事务。

    🔴 没有 SAVEPOINT 的话:这条 SELECT 一失败(表不存在 / 权限 / 迁移没跑),
       调用方整条事务就 aborted,**它自己后面每一条查询**都报
       `InFailedSqlTransaction` —— 端点全线 503,而错误看起来在别人那儿。
       实测踩过:gap_plan 判据包从 98 绿变成 35 红 12 error,
       根因是那个库的 schema 来自 `init_db()`,压根没有 `quote_pricing_snapshots`。

    🔴 autocommit 连接没有事务块,`SAVEPOINT` 自己会报错;那种模式下
       一条语句失败也不会波及后续,所以退回直接跑。两条路都要活。
    """
    has_sp = False
    if _needs_savepoint(cursor):
        try:
            cursor.execute("SAVEPOINT " + _SAVEPOINT)
            has_sp = True
        except Exception:  # noqa: BLE001 - 探针说要、实际仍建不了,就照旧不建
            has_sp = False
    try:
        cursor.execute(_SNAPSHOT_SQL, (int(quote_id),))
        row = cursor.fetchone()
        snap = (dict(row) if row else {}).get("pricing_snapshot") or {}
        mm = snap.get("media_mix") if isinstance(snap, dict) else None
        out = mm if isinstance(mm, dict) else {}
        if has_sp:
            cursor.execute("RELEASE SAVEPOINT " + _SAVEPOINT)
        return out
    except Exception:
        if has_sp:
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT " + _SAVEPOINT)
            except Exception:  # noqa: BLE001
                pass
        raise


def _read_media_mix(quote_id: int, cursor=None) -> Dict:
    """取这单最新快照里的 `media_mix` 块。读不到 ⇒ 空 dict。

    🔴 `cursor` 给了就用它 —— 它决定读的是**哪个 schema 的**表。
       自己开连接是回落,不是常态。
    """
    if cursor is not None:
        return _read_with_cursor(cursor, int(quote_id))
    conn = None
    try:
        from db.connection import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(_SNAPSHOT_SQL, (int(quote_id),))
        row = cur.fetchone()
        snap = (dict(row) if row else {}).get("pricing_snapshot") or {}
        mm = snap.get("media_mix") if isinstance(snap, dict) else None
        return mm if isinstance(mm, dict) else {}
    finally:
        _release(conn)


def _snapshot_conversion_version(quote_id: int, cursor=None):
    """快照里冻结的换算版本号;没有 ⇒ None(老单,用当前版)。"""
    mm = _read_media_mix(quote_id, cursor)
    v = mm.get("conversion_version")
    return int(v) if v else None


def resolve_for_quote(quote_id: Optional[int], cursor=None) -> Dict[str, object]:
    """这单该用哪一版换算 -> `{"version": n, "bps": {bucket: bps}}`。

    快照里有 `media_mix.conversion_version` ⇒ 用快照那一版(报价当时冻下来的);
    没有(老单)⇒ 用**当前版**。见模块抬头:老单不冻结是因为**钱不读它**。

    🔴 任何一步取不到,都回落成**三个桶全 10000** —— 即今天的行为。
       回落必须是「一槽一条」而不是「当前版」:读不出来时按 5 倍算,
       等于凭空把额度放大 5 倍,而没有任何东西会报错。

    🔴 `cursor`:调用方已经有游标时**必须**传进来。不传就另开一条池连接,
       而那条连接看的是默认 schema —— 调用方的游标若指着另一个 schema
       (判据的一次性 schema 正是如此),同一次读数就一半读 A 库一半读 B 库。
       更阴的是「读不到 ⇒ 回落 10000 ⇒ 老行为」,所以**读错了也不会红**。
    """
    fallback = {"version": 0, "bps": {b: ONE_SLOT_BPS for b in BUCKETS}}
    # 🔴 `version: 0` 是**哨兵**:它说「没读到任何一版」,不是「第 0 版」。
    #    表里 CHECK version >= 1,所以 0 永远不会与真实版本撞上。
    version = None
    if quote_id:
        try:
            version = _snapshot_conversion_version(int(quote_id), cursor)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[media_slot_conversion] 读快照失败(quote=%s):%s", quote_id, exc)
            return fallback
    if version is None:
        version = current_version()
    bps = _fetch_version(version)
    if not bps:
        return fallback
    return {"version": version,
            "bps": {b: int(bps.get(b) or ONE_SLOT_BPS) for b in BUCKETS}}


#: 口径 -> 新建 topic 的默认桶。
#: 🔴 `mixed` **不在这张表里**:它不是「一个桶」,而是按快照 mix 逐桶分,
#:    见 `plan_buckets()`。放进来会让调用方以为 mixed 也能一键取一个桶。
PERSPECTIVE_BUCKET = {
    PERSPECTIVE_PORTAL: BUCKET_ANCHOR,
    PERSPECTIVE_SELF_MEDIA: BUCKET_COVERAGE,
}


def quote_perspective(quote_id: Optional[int], cursor=None) -> Optional[str]:
    """这单冻结的发布口径。读不到 ⇒ None(**不是**默认口径)。

    🔴 读不到时回 None 而不是 `DEFAULT_PERSPECTIVE`,是因为两者后果不同:
       None ⇒ 新 topic 写 NULL 桶 ⇒ 一条一槽 ⇒ **老行为**;
       默认口径 ⇒ 写 coverage ⇒ 一条 0.2 槽 ⇒ 把没选过口径的老单额度放大 5 倍。
       回落必须落到「什么都没变」那一侧。
    """
    if not quote_id:
        return None
    try:
        # 与 resolve_for_quote 共用同一条读法 —— 两处各写一遍 SQL,
        # 迟早一处改了另一处没改,而两边都不会报错。
        per = _read_media_mix(int(quote_id), cursor).get("delivery_perspective")
        if per in PERSPECTIVES:
            return str(per)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[media_slot_conversion] 读口径失败(quote=%s):%s", quote_id, exc)
    return None


def _mixed_quota(quote_id: int, cursor=None) -> Dict[str, int]:
    """mixed 口径下每个桶**允许多少条**。取快照冻结的 `posts_estimate`;
    没有就用快照的 `mix`(槽) × 当前 bps 现算。两者都没有 ⇒ 空 dict。
    """
    try:
        mm = _read_media_mix(int(quote_id), cursor)
        est = mm.get("posts_estimate")
        if isinstance(est, dict) and est:
            return {b: int(est.get(b) or 0) for b in BUCKETS}
        mix = mm.get("mix")
        if isinstance(mix, dict) and mix:
            table = dict(resolve_for_quote(quote_id, cursor)["bps"])  # type: ignore[arg-type]
            return {b: posts_for_slots(int(mix.get(b) or 0), b, table) for b in BUCKETS}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[media_slot_conversion] 读 mixed 配额失败(quote=%s):%s", quote_id, exc)
    return {}


def mixed_remaining_posts(quote_id: Optional[int], surviving_topics, cursor=None) -> Optional[int]:
    """mixed 口径单**还能再建多少条**才不超整单配额 = Σ 桶 max(0, 配额 − 已有未删选题已占)。

    不是 mixed / 读不到配额 ⇒ None(不钳)。
    🔴 [Review 09-28 · WO_317 第三笔] 子集批次(「为新词生成标题」)出题前用它钳本批条数:
       `plan_buckets` 按「整单配额 − 已建」分桶,配额用完后多出来的写 NULL 桶(= 一条一槽),
       悄悄超出整单配额。`surviving_topics` 要传**本批保存时不会被删掉的**那些行
       (本批词的 draft/pending/regenerating 会被 save_topics_batch 删掉,不算已占),
       与 `plan_buckets` 届时数到的是同一批行。
    """
    if quote_perspective(quote_id, cursor) != PERSPECTIVE_MIXED:
        return None
    quota = _mixed_quota(int(quote_id), cursor)
    if not quota:
        return None
    used = {b: 0 for b in BUCKETS}
    for t in surviving_topics or []:
        b = (t or {}).get("media_bucket")
        if b in used:
            used[b] += 1
    return sum(max(0, int(quota.get(b) or 0) - used[b]) for b in BUCKETS)


def plan_buckets(quote_id: Optional[int], count: int, *, cursor=None) -> list:
    """给这单接下来要建的 `count` 条 topic 逐条分桶,返回长度 == count 的列表。

    · portal / self_media ⇒ 全部同一个桶(§8.3「按请求口径写桶」)。
    · mixed ⇒ **锚点先满再覆盖再抖音**(§3.3),已建的先扣掉;配额用完后剩下的写 None。
    · 读不到口径 ⇒ 全 None = 老行为(一条一槽)。

    🔴 为什么条数与桶必须同源:默认条数是 `required_articles × k`(§8.6),
       而 k 来自桶。若默认了 35 条却写 NULL 桶,35 条各占一槽 ⇒ 7 槽的单当场超 5 倍;
       若写了 coverage 却只生成 7 条 ⇒ 只算 1.4 槽,额度凭空多出来。
       两个数必须由**同一次**口径解析产出 —— 这就是那个单点。
    """
    n = max(0, int(count or 0))
    if n == 0:
        return []
    per = quote_perspective(quote_id, cursor)
    if per is None:
        return [None] * n
    if per in PERSPECTIVE_BUCKET:
        return [PERSPECTIVE_BUCKET[per]] * n
    # mixed:按已建条数把配额吃掉
    quota = _mixed_quota(int(quote_id), cursor)
    if not quota:
        return [None] * n
    used = {b: 0 for b in BUCKETS}
    try:
        own = cursor is None
        conn = None
        if own:
            from db.connection import get_connection
            conn = get_connection()
            cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT media_bucket, COUNT(*) AS cnt FROM topics "
                " WHERE quote_id = %s GROUP BY media_bucket", (int(quote_id),))
            for r in (cursor.fetchall() or []):
                r = dict(r)
                b = r.get("media_bucket")
                if b in used:
                    used[b] = int(r.get("cnt") or 0)
        finally:
            if own:
                _release(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[media_slot_conversion] 读已建分桶失败(quote=%s):%s", quote_id, exc)
        return [None] * n
    out = []
    for _ in range(n):
        placed = None
        for b in (BUCKET_ANCHOR, BUCKET_COVERAGE, BUCKET_DOUYIN):
            if used[b] < int(quota.get(b) or 0):
                used[b] += 1
                placed = b
                break
        out.append(placed)
    return out

def bps_for(bucket: Optional[str], table: Optional[Dict[str, int]] = None) -> int:
    """一个桶的 bps。桶为 None / 不认识 ⇒ `ONE_SLOT_BPS`(老行为)。

    🔴🔴 **NULL 桶 = 一条一槽,与换算表无关** —— 就是下面那两行。
       它是一条**短路**:`media_bucket IS NULL` 的行(= 迁移之前建的存量选题)
       根本不去查 `table`,所以把表里的任何一个 bps 改成任何值,
       都动不了这些行的折算结果。
       这一点有两个后果,都要知道:
         · 好的那面 —— 老单的容量读数逐字不变,这是本单的存量安全底线;
         · 麻烦的那面 —— **对 NULL 桶的行下 bps 毒是打在冗余目标上**,
           判据仍绿不代表锁没牙(Review 2026-09-15 P7 即此)。
           要打折算这条腿,夹具必须先给行盖上真桶
           (见 `test_legacy_quote_reading_equals_plain_count` 的反向对照那一段)。
    """
    if not bucket:
        return ONE_SLOT_BPS          # ← 这一行就是「NULL 桶与表无关」
    if table is None:
        table = dict(resolve_for_quote(None)["bps"])  # type: ignore[arg-type]
    return int(table.get(str(bucket)) or ONE_SLOT_BPS)


def posts_for_slots(slots: int, bucket: Optional[str],
                    table: Optional[Dict[str, int]] = None) -> int:
    """槽数 -> 条数(展示用)。`round_half_up`,与前端对拍。"""
    return round_half_up(int(slots) * bps_for(bucket, table) / float(ONE_SLOT_BPS))


def default_posts_converter(quote_id: Optional[int], cursor=None):
    """这单「槽 -> 默认条数」的换算器:`convert(slots, bucket=None) -> 条`。

    🔴 只许有这一处:写作详情每词的 `planned_posts_default`(db.diagnosis_db)与
       `POST /api/writing/generate-titles` 的 `per_keyword_plan[].slots`(server.py)都调它。
       两边各自拼「口径 -> 桶 + 这单的 bps 表」的话,哪天有一边改了,
       同一个词在详情里显示 35 条、点生成却出 7 条,而两边各自看都对。
    桶:调用方显式给的桶优先;否则按这单冻结口径取默认桶(mixed / 读不到 ⇒ None ⇒ 一条一槽,老行为)。
    0 槽 ⇒ 0 条(显式 0 合法:这个词这次不出题)。
    """
    default_bucket = PERSPECTIVE_BUCKET.get(quote_perspective(quote_id, cursor))
    table = dict(resolve_for_quote(quote_id, cursor)["bps"])

    def convert(slots: int, bucket: Optional[str] = None) -> int:
        return posts_for_slots(slots, bucket or default_bucket, table)

    return convert
