"""报告快照的**持久化**(#63 · 2026-09-05 · Review §19 裁定)。

🔴 为什么单独一个模块:`snapshot.py` 是纯逻辑(零 DB 访问),它把冻结面
   该长什么样、四条不变量都写全了 —— 然后**零调用者**。
   「修复从没接线,却因为存在而让人停止检查」:`customer_links.py:64` 早就
   写着「零生产写入方(已 census)」,08-21 的验收仍把空卡片读成「诚实留白」。
   所以这个模块只做一件事:**把那份冻结面真的写进库**。

冻结面的取数口径与读点(`api/defensive_geo_report_api.py` 五卡)**同解** ——
读点在报告时重算一遍的东西,这里在冻结时算一次并落库:
  · plan_cell_ids  ← `monitoring_run_cells.plan_hash`(同一张表、同一个窗)
  · raw_result_ids ← 账本里这些格的 `monitoring_result_id`(≤ cutoff)
两处口径漂了,已发给客户的报告就会在复算时变样,而**不会有任何东西报错**。

版本列的口径(Review §19②):四个 `*_version` 在代码里**没有版本概念** ——
写伪 semver 等于伪造溯源,比空卡片坏得多(空卡片让人继续查,一个具体但
错误的溯源让人停止查并相信它)。所以写**可溯源真值** `release:<sha>`,
sha 取自 `/app/RELEASE_SHA`(生产必有);读不到 ⇒ 显式哨兵 + WARN,
**不写空串**(空串会被当成"有版本、只是空的")。
"""

from __future__ import annotations

import logging
from typing import Any

from services.defensive_geo.monitoring import snapshot as _snap

logger = logging.getLogger("GEO-DefGeo-SnapshotStore")

#: 读不到发布 sha 时的显式哨兵。**不是空串** —— 空串与"有版本"在下游同形。
UNVERSIONED = "unversioned"

#: 生产镜像里的发布标识(与 `RELEASE_SHA` 环境变量无关 —— 那个变量不存在)。
RELEASE_SHA_PATH = "/app/RELEASE_SHA"

#: 四个版本列今天共用同一个真值来源:「哪个发布的代码产出了这份快照」。
#: 它们各自的独立版本源另立 #109,不在本单。
_VERSION_COLUMNS = (
    "entity_resolver_version",
    "outcome_classifier_version",
    "evidence_extractor_version",
    "metric_definition_version",
)


def release_version(path: str = RELEASE_SHA_PATH) -> str:
    """`release:<sha>`,读不到则哨兵。

    🔴 只读文件,不读 `RELEASE_SHA` 环境变量 —— 那个变量**不存在**
       (实测 `printenv` 恒空),按它取会稳定拿到空串并把哨兵路径悄悄跳过。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            sha = (fh.read() or "").strip()
    except OSError:
        sha = ""
    if not sha:
        logger.warning(
            "[defgeo-snapshot] 读不到 %s,四个版本列写哨兵 %r。"
            "这份快照仍可溯源到「哪次诊断」,但溯不到「哪个发布的代码」。",
            path, UNVERSIONED)
        return UNVERSIONED
    return "release:" + sha


def _iso(value) -> str:
    """时间统一成 ISO 串 —— 冻结面要 JSON 安全(content_hash 拒 datetime)。"""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _frozen_face(cur, *, diagnosis_id: int, brand_id: int) -> dict[str, Any]:
    """从库里取这一刻的冻结面。与读点同解。"""
    cur.execute("SELECT NOW() AS now")
    cutoff = cur.fetchone()["now"]

    cur.execute(
        "SELECT MIN(created_at) AS lo, ARRAY_AGG(DISTINCT plan_hash) AS hashes "
        "  FROM public.monitoring_run_cells "
        " WHERE brand_id = %s AND plan_hash IS NOT NULL AND created_at <= %s",
        (int(brand_id), cutoff))
    row = cur.fetchone() or {}
    plan_hashes = [h for h in (row.get("hashes") or []) if h]
    window_start = row.get("lo") or cutoff

    raw_ids: list[int] = []
    if plan_hashes:
        cur.execute(
            "SELECT DISTINCT monitoring_result_id AS rid "
            "  FROM public." + _ledger_table() +
            " WHERE plan_cell_id = ANY(%s) AND monitoring_result_id IS NOT NULL "
            "   AND terminal_at IS NOT NULL AND terminal_at <= %s",
            (plan_hashes, cutoff))
        raw_ids = sorted(int(r["rid"]) for r in (cur.fetchall() or []))

    version = release_version()
    # plan_snapshot_id / _hash 用**真实的 plan_hash**,不新造 id(Review §19①):
    # 读点本来就按 plan_hash 取格,新造一个 id 只会多一个对不上的东西。
    plan_id = plan_hashes[0] if plan_hashes else ""
    # 🔴 冻结面必须是 **JSON 安全**的:`snapshot.content_hash` 走
    #    commitments 序列化,遇到 datetime 直接抛 CommitmentError。
    #    上一版这里塞的是原始 datetime —— 于是 `persist_report_snapshot`
    #    **从来跑不通**,而 13 条 AST/纯函数判据全绿:它们一条都没调用它。
    #    是 P6 那条「驱动真函数」的锁把它挖出来的。
    #    timestamptz 列吃 ISO 串,Postgres 自己转,不必为入库再留一份 datetime。
    face: dict[str, Any] = {
        "plan_snapshot_id": plan_id,
        "plan_snapshot_hash": plan_id,
        "sampling_window_start": _iso(window_start),
        "sampling_window_end": _iso(cutoff),
        "cutoff_at": _iso(cutoff),
        "input_watermark": _iso(cutoff),
        "raw_result_ids": raw_ids,
    }
    for col in _VERSION_COLUMNS:
        face[col] = version
    return face


def _ledger_table() -> str:
    from services.defensive_geo.monitoring import attempt_ledger as _al
    return _al.TABLE


def _next_revision(cur, *, diagnosis_id: int) -> int:
    """状态推进 = 新 revision,不是 UPDATE(snapshot.py 抬头 §13.2)。"""
    cur.execute(
        "SELECT COALESCE(MAX(revision), 0) AS r FROM public." + _snap.TABLE +
        " WHERE diagnosis_id = %s", (int(diagnosis_id),))
    return int((cur.fetchone() or {}).get("r") or 0) + 1


def persist_report_snapshot(cur, *, diagnosis_id: int, state: str = "ready") -> str | None:
    """在报告冻结的**同一个事务**里写一行快照;成功返回 report_snapshot_id。

    🔴 拿不到必需的归属信息时返回 None 并 WARN —— **不抛**:
       报告本身比快照重要,但也**不能静默** ——
       被正确捕获的失败不留痕迹,最该诊断的就最查不到。
    """
    # 🔴 [#130] 第三兜底 `brands.owner_user_id`。
    #    治本在 `server.py` 占位 INSERT(非组织用户现在也写 created_by_user_id),
    #    但**历史行不回填**(Owner 拍板),而且品牌归属本来就是比
    #    「谁点的那一下」更稳的权威源 —— 诊断可以由同事代跑,品牌只有一个主人。
    #    🔴 兜底**不放宽失败条件**:三级全空仍 return None + WARN。
    #    把「查不到归属」兜成某个默认用户,等于把快照挂到错的人名下,
    #    比没有快照更坏 —— 具体而错误的答案会让人停止追查。
    cur.execute(
        "SELECT d.brand_id, COALESCE("
        "         d.responsible_user_id, d.created_by_user_id,"
        "         (SELECT b.owner_user_id FROM brands b WHERE b.id = d.brand_id)"
        "       ) AS owner "
        "  FROM diagnosis_records d WHERE d.id = %s", (int(diagnosis_id),))
    rec = cur.fetchone() or {}
    brand_id, owner = rec.get("brand_id"), rec.get("owner")
    if not brand_id or not owner:
        logger.warning(
            "[defgeo-snapshot] 诊断 %s 缺 brand_id(%r)或归属用户(%r),不写快照 —— "
            "五卡会留白。这是数据缺口,不是「没有监测数据」。",
            diagnosis_id, brand_id, owner)
        return None

    face = _frozen_face(cur, diagnosis_id=int(diagnosis_id), brand_id=int(brand_id))
    revision = _next_revision(cur, diagnosis_id=int(diagnosis_id))
    snapshot_id = "rpt-%d-r%d" % (int(diagnosis_id), revision)
    snap = _snap.build(report_snapshot_id=snapshot_id, revision=revision,
                       state=state, frozen=face)
    # 四条不变量里这一条在**写之前**就该拦:实时面的字段不许混进冻结面。
    _snap.assert_live_not_in_frozen_surface(face)

    cur.execute(
        "INSERT INTO public." + _snap.TABLE + " ("
        " report_snapshot_id, revision, tenant_owner_user_id, brand_id, diagnosis_id,"
        " state, content_hash, plan_snapshot_id, plan_snapshot_hash,"
        " sampling_window_start, sampling_window_end, cutoff_at, input_watermark,"
        " raw_result_ids, entity_resolver_version, outcome_classifier_version,"
        " evidence_extractor_version, metric_definition_version, snapshot_version"
        ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (snap.report_snapshot_id, snap.revision, int(owner), int(brand_id),
         int(diagnosis_id), snap.state, snap.content_hash,
         face["plan_snapshot_id"], face["plan_snapshot_hash"],
         face["sampling_window_start"], face["sampling_window_end"],
         face["cutoff_at"], face["input_watermark"], face["raw_result_ids"],
         face["entity_resolver_version"], face["outcome_classifier_version"],
         face["evidence_extractor_version"], face["metric_definition_version"],
         _snap.SNAPSHOT_VERSION))
    logger.info(
        "[defgeo-snapshot] 诊断 %s 写入快照 %s(rev %d · %d 个 plan_hash · "
        "%d 个 raw result · 版本 %s)",
        diagnosis_id, snapshot_id, revision,
        1 if face["plan_snapshot_id"] else 0, len(face["raw_result_ids"]),
        face["entity_resolver_version"])
    return snapshot_id
