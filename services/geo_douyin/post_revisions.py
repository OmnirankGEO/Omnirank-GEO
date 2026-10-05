"""WP3 · 不可变成品版本 + 活动生成代际 CAS(规格 02 §3.3 / §5.6)。

## 要守的两句话

  · 「同一 post 同一时刻最多一个 active generation;迟到任务只能转 `superseded`,
     不能写 active 成品」(§2.2 不变量 7);
  · 「worker 所有写入带 `WHERE active_generation_task_id=? AND generation_epoch=?`;
     迟到任务标 `superseded`」(§5.6)。

## 为什么 epoch 与 task_id 两个都要

只比 `active_generation_task_id` 挡不住**同一个 task 被重放**(lease 过期后
reclaim,老进程醒过来继续写);只比 `generation_epoch` 挡不住**同代际内两个 task**。
两个一起比,才是"这次写入属于当前那一代的那一个任务"。

## CAS 的零行 = 冲突,不是"没事发生"

本模块所有写入都返回受影响行数。零行**必须**被调用方当作冲突处理
(迟到 → 标 superseded,而不是当成功继续往下走)。
把零行当成功是本仓记过的形态:「写库已提交只有回包炸 → 判失败必须查库」的镜像面。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Final, Mapping, Optional, Sequence

from services.article_closed_loop_contract import snapshot_hash

logger = logging.getLogger("GEO-ImgNote-Revisions")

REVISION_SCHEMA: Final = "geo-image-note-post-revision-v1"

STATUS_STAGED: Final = "staged"
STATUS_ACTIVE: Final = "active"
STATUS_SUPERSEDED: Final = "superseded"
STATUS_FAILED: Final = "failed"

OPERATION_CREATE: Final = "create"
OPERATION_EDIT: Final = "edit"
OPERATION_REGENERATE: Final = "regenerate"
OPERATION_REDRAW: Final = "redraw"
OPERATION_RESTYLE: Final = "restyle"

ALLOWED_OPERATIONS: Final[frozenset[str]] = frozenset({
    OPERATION_CREATE, OPERATION_EDIT, OPERATION_REGENERATE,
    OPERATION_REDRAW, OPERATION_RESTYLE,
})


class GenerationSuperseded(RuntimeError):
    """迟到任务试图写 active 成品。调用方必须把该 task 标 superseded,不得重试。"""


class RevisionConflict(RuntimeError):
    """`If-Match` / ETag 不匹配。调用方 409/412 + `reload_latest` / `compare_changes`。"""


def save_text_revision(cur, *, geo_post_id: int, created_by: int,
                       title: Optional[str] = None, body: Optional[str] = None,
                       hashtags: Optional[Sequence[str]] = None,
                       expected_revision_id: Optional[int] = None,
                       check_revision: bool = False) -> Optional[int]:
    """保存编辑文案及发布快照；调用方持有事务，图片及历史版本内容不改写。

    H0 对象完整性：同一 post 行锁串行化编辑和生成的指针切换。
    显式提供旧版本时返回可恢复冲突；重复提交相同文案可安全回读当前版本。
    历史无版本作品仍可保存，不在此伪造其资产/生成来源，沿用现有补版本路径。
    """
    cur.execute("SELECT * FROM geo_douyin_posts WHERE id=%s AND deleted_at IS NULL FOR UPDATE",
                (geo_post_id,))
    post = cur.fetchone()
    if not post:
        raise LookupError("作品不存在，请返回图文列表重新选择")
    if post["status"] == "published":
        raise RevisionConflict("这条图文已经发布；修改已保留在本页，请新建图文后再使用这些内容。")
    current_id = post.get("active_revision_id")
    current_copy = (post.get("title") or "", post.get("body_text") or "", post.get("hashtags") or [])
    edited = (current_copy[0] if title is None else title,
              current_copy[1] if body is None else body,
              current_copy[2] if hashtags is None else list(hashtags))
    revision = None
    if current_id is not None:
        cur.execute("SELECT * FROM geo_douyin_post_revisions WHERE post_revision_id=%s AND geo_post_id=%s",
                    (current_id, geo_post_id))
        revision = cur.fetchone()
        if not revision:
            raise RevisionConflict("作品版本暂时无法读取。请重新读取最新版本；若仍失败，请联系管理员恢复作品版本。")
    aligned = not revision or edited == (revision.get("title") or "", revision.get("body") or "", revision.get("hashtags") or [])
    if edited == current_copy and aligned:
        return current_id
    if check_revision and current_id != expected_revision_id:
        raise RevisionConflict("作品已有更新；你的输入仍保留。请先重新读取最新版本并对比，再决定保存哪些修改。")
    # 不锁 task，避免与生成链 task → post 的锁顺序相反。
    cur.execute("SELECT 1 FROM geo_douyin_post_tasks WHERE post_id=%s AND status IN ('pending','running') AND superseded_at IS NULL LIMIT 1",
                (geo_post_id,))
    if cur.fetchone() or post["status"] == "generating":
        raise RevisionConflict("图片仍在生成。你的输入已保留，请等待生成结束、重新读取最新版本后再保存。")
    next_id = current_id
    if revision:
        staged = stage_revision(cur, geo_post_id=geo_post_id, created_by=created_by,
            operation_kind=OPERATION_EDIT, title=edited[0], body=edited[1], hashtags=edited[2],
            base_revision_id=current_id, contact_enabled=revision["contact_enabled"],
            cards_snapshot=revision["cards_snapshot"], asset_manifest=revision["asset_manifest"],
            topic_snapshot_hash=revision.get("topic_snapshot_hash"),
            render_input_hash=revision.get("render_input_hash"),
            style_catalog_version=revision.get("style_catalog_version"))
        next_id = staged["post_revision_id"]
        params = {"geo_post_id": geo_post_id, "post_revision_id": next_id}
        cur.execute(_SUPERSEDE_OTHERS, params)
        cur.execute(_ACTIVATE, params)
        if not cur.fetchone():
            raise RevisionConflict("保存版本发生冲突。输入已保留，请重新读取最新版本后重试。")
    cur.execute("""UPDATE geo_douyin_posts SET title=%s, body_text=%s, hashtags=%s::jsonb,
                   active_revision_id=%s, updated_at=now() WHERE id=%s""",
                (edited[0], edited[1], json.dumps(edited[2], ensure_ascii=False), next_id, geo_post_id))
    return next_id


def compute_manifest_hash(asset_manifest: Mapping[str, Any]) -> str:
    return snapshot_hash({"schema": REVISION_SCHEMA, "asset_manifest": dict(asset_manifest)})


def compute_render_input_hash(*, topic_snapshot_hash: str, style_key: str,
                              ranking_template: Optional[str], style_catalog_version: str,
                              card_count: int, aspect_ratio: str, content_form: str,
                              contact_enabled: bool) -> str:
    """渲染输入指纹。

    🔴 `style_catalog_version` 必须进去(规格 §5.3/§6):目录版本变了,同一个
       `style_key` 解析出的 render config 可能已经不同 —— 不带版本的指纹会让
       "换了目录但指纹没变"静默通过。
    """
    return snapshot_hash({
        "schema": REVISION_SCHEMA,
        "topic_snapshot_hash": topic_snapshot_hash,
        "style_key": style_key,
        "ranking_template": ranking_template,
        "style_catalog_version": style_catalog_version,
        "card_count": int(card_count),
        "aspect_ratio": aspect_ratio,
        "content_form": content_form,
        "contact_enabled": bool(contact_enabled),
    })


def etag_for(post_id: int, revision_no: int) -> str:
    """对外 ETag。形态与规格 §5.5 示例一致:`"post-101-rev-7"`。"""
    return f'"post-{int(post_id)}-rev-{int(revision_no)}"'


def parse_etag(value: str) -> Optional[tuple[int, int]]:
    """解析 `If-Match`。认不出返回 None —— **不猜**,调用方按 412 处理。"""
    if not value:
        return None
    token = value.strip().strip('"')
    parts = token.split("-")
    if len(parts) != 4 or parts[0] != "post" or parts[2] != "rev":
        return None
    try:
        return int(parts[1]), int(parts[3])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 写入(全部在调用方事务内,本模块不开事务不 commit)
# ---------------------------------------------------------------------------

_INSERT_REVISION = """
INSERT INTO geo_douyin_post_revisions (
    geo_post_id, revision_no, base_revision_id, created_by, operation_kind,
    title, body, hashtags, contact_enabled, cards_snapshot, asset_manifest,
    manifest_hash, topic_snapshot_hash, render_input_hash, style_catalog_version,
    status, created_at
)
SELECT %(geo_post_id)s,
       COALESCE(MAX(revision_no), 0) + 1,
       %(base_revision_id)s, %(created_by)s, %(operation_kind)s,
       %(title)s, %(body)s, %(hashtags)s::jsonb, %(contact_enabled)s,
       %(cards_snapshot)s::jsonb, %(asset_manifest)s::jsonb,
       %(manifest_hash)s, %(topic_snapshot_hash)s, %(render_input_hash)s,
       %(style_catalog_version)s, 'staged', now()
  FROM geo_douyin_post_revisions
 WHERE geo_post_id = %(geo_post_id)s
RETURNING post_revision_id, revision_no, status
"""


def stage_revision(cur, *, geo_post_id: int, created_by: int, operation_kind: str,
                   title: str = "", body: str = "", hashtags: Optional[Sequence[str]] = None,
                   contact_enabled: bool = False,
                   cards_snapshot: Optional[Sequence[Mapping[str, Any]]] = None,
                   asset_manifest: Optional[Mapping[str, Any]] = None,
                   topic_snapshot_hash: str = "", render_input_hash: str = "",
                   style_catalog_version: str = "",
                   base_revision_id: Optional[int] = None) -> dict[str, Any]:
    """建一个 `staged` 版本。**不**切 active —— 切换是 `activate_revision` 的事。

    生成失败时 staged 版本原样留着(标 failed),上一版可用成品不受影响
    (规格 §3.3 末:「生成失败保留上一版可用成品」)。
    """
    if operation_kind not in ALLOWED_OPERATIONS:
        raise ValueError(f"unknown operation_kind: {operation_kind!r}")
    manifest = dict(asset_manifest or {})
    params = {
        "geo_post_id": int(geo_post_id),
        "base_revision_id": base_revision_id,
        "created_by": int(created_by),
        "operation_kind": operation_kind,
        "title": title,
        "body": body,
        "hashtags": json.dumps(list(hashtags or []), ensure_ascii=False),
        "contact_enabled": bool(contact_enabled),
        "cards_snapshot": json.dumps(list(cards_snapshot or []), ensure_ascii=False),
        "asset_manifest": json.dumps(manifest, ensure_ascii=False),
        "manifest_hash": compute_manifest_hash(manifest),
        "topic_snapshot_hash": topic_snapshot_hash or None,
        "render_input_hash": render_input_hash or None,
        "style_catalog_version": style_catalog_version or None,
    }
    cur.execute(_INSERT_REVISION, params)
    row = cur.fetchone()
    if row is None:
        # 该 post 还没有任何 revision ⇒ 上面的 SELECT ... FROM 聚合返回空集。
        # 用 revision_no = 1 直插一次。
        cur.execute(
            _INSERT_REVISION.replace(
                "SELECT %(geo_post_id)s,\n       COALESCE(MAX(revision_no), 0) + 1,",
                "SELECT %(geo_post_id)s,\n       1,",
            ).replace(
                "  FROM geo_douyin_post_revisions\n WHERE geo_post_id = %(geo_post_id)s",
                "",
            ),
            params,
        )
        row = cur.fetchone()
    return dict(row)


_ACTIVATE = """
UPDATE geo_douyin_post_revisions
   SET status = 'active', activated_at = now()
 WHERE post_revision_id = %(post_revision_id)s
   AND geo_post_id = %(geo_post_id)s
   AND status = 'staged'
RETURNING post_revision_id, revision_no
"""

_SUPERSEDE_OTHERS = """
UPDATE geo_douyin_post_revisions
   SET status = 'superseded'
 WHERE geo_post_id = %(geo_post_id)s
   AND status = 'active'
   AND post_revision_id <> %(post_revision_id)s
"""

_POINT_POST_AT_REVISION = """
UPDATE geo_douyin_posts
   SET active_revision_id = %(post_revision_id)s,
       updated_at = now()
 WHERE id = %(geo_post_id)s
   AND active_generation_task_id = %(task_id)s
   AND generation_epoch = %(epoch)s
RETURNING id, active_revision_id, generation_epoch
"""


def activate_revision(cur, *, geo_post_id: int, post_revision_id: int,
                      task_id: int, epoch: int) -> dict[str, Any]:
    """把 staged 版本原子切成 active,并让 post 指针指向它。

    🔴 CAS 谓词同时比 `active_generation_task_id` 与 `generation_epoch`。
       零行 = 这次写入不属于当前代际的当前任务 ⇒ 抛 `GenerationSuperseded`,
       调用方把该 task 标 superseded。**不许**当成"没事发生"继续往下走。

    顺序刻意是「先把 post 指针 CAS 过去,再改 revision 状态」:
    指针那一步是竞争点,先做它,输的一方在改任何 revision 状态之前就出局了 ——
    否则迟到任务会先把别人的 active 踩成 superseded,再发现自己 CAS 输了。
    """
    cur.execute(_POINT_POST_AT_REVISION, {
        "geo_post_id": int(geo_post_id),
        "post_revision_id": int(post_revision_id),
        "task_id": int(task_id),
        "epoch": int(epoch),
    })
    pointed = cur.fetchone()
    if pointed is None:
        raise GenerationSuperseded(
            f"post={geo_post_id} 的活动生成已不是 task={task_id}/epoch={epoch};"
            "本次结果只能标 superseded,不得写入 active 成品"
        )
    cur.execute(_SUPERSEDE_OTHERS, {
        "geo_post_id": int(geo_post_id), "post_revision_id": int(post_revision_id)})
    cur.execute(_ACTIVATE, {
        "geo_post_id": int(geo_post_id), "post_revision_id": int(post_revision_id)})
    activated = cur.fetchone()
    if activated is None:
        raise RevisionConflict(
            f"revision={post_revision_id} 不处于 staged 状态,无法激活(可能已被并发激活或失败)"
        )
    return dict(activated)


_SUPERSEDE_PREVIOUS_TASKS = """
UPDATE geo_douyin_post_tasks
   SET superseded_at = now(), status = 'superseded', updated_at = now()
 WHERE post_id = %(geo_post_id)s
   AND id <> %(task_id)s
   AND superseded_at IS NULL
   AND status IN ('pending', 'running')
RETURNING id
"""

_BEGIN_GENERATION = """
UPDATE geo_douyin_posts
   SET generation_epoch = generation_epoch + 1,
       active_generation_task_id = %(task_id)s,
       updated_at = now()
 WHERE id = %(geo_post_id)s
RETURNING generation_epoch, active_generation_task_id
"""


def supersede_active_tasks(cur, *, geo_post_id: int, except_task_id: int = -1) -> list[int]:
    """接管该 post 上全部在途任务。**必须在插入新任务之前调用**。

    🔴 顺序不是风格问题:034 的 `uq_geo_douyin_task_active_generation`
       (同 post 最多一个非 superseded 的 pending/running)会让"先插新任务再接管旧的"
       在 INSERT 那一步就 UniqueViolation。协调器的正确次序是

           supersede_active_tasks(post) → INSERT 新 task → begin_generation(post, 新 task)

       `begin_generation` 里保留同样一步作为幂等兜底(走别的路径建出来的任务也能收敛),
       但它兜不了 INSERT 本身 —— 那一步在它之前。
    """
    cur.execute(_SUPERSEDE_PREVIOUS_TASKS, {
        "geo_post_id": int(geo_post_id), "task_id": int(except_task_id)})
    return [int(dict(r)["id"]) for r in (cur.fetchall() or [])]


def begin_generation(cur, *, geo_post_id: int, task_id: int) -> dict[str, Any]:
    """开新一代生成:**同一事务内**接管上一代 + epoch +1。

    🔴 [WP3 返修 2026-08-17] 第一版只做 epoch +1,没有把上一代任务标 superseded ——
       被本包自己的并发 regenerate 判据顶出来:034 建的
       `uq_geo_douyin_task_active_generation`(同 post 最多一个非 superseded 的
       pending/running)让第二个任务**根本插不进去**,UniqueViolation。
       也就是说 DB 约束与 epoch CAS 两套机制**没接上**:
       约束以为"接管已经发生了",而代码从来没做接管。

    正确形态是两道**互补**的防线,缺一不可:
      · DB 约束:同 post 只允许一个在途任务存在 —— 挡住"新任务被创建出来"这一步;
      · epoch CAS:挡住**已经在跑**、刚好越过约束检查的那个老 worker 写结果。
    只有约束没有 CAS → 在途的老 worker 照样能写 active;
    只有 CAS 没有约束 → 会堆积一堆永远写不进去的僵尸任务。

    顺序刻意是「先接管旧的、再指向新的」:反过来的话,两者之间那一瞬
    `active_generation_task_id` 已经指向新任务、而旧任务还是 pending,
    此时崩溃会留下一个约束允许、但永远不会被接管的孤儿。
    """
    cur.execute(_SUPERSEDE_PREVIOUS_TASKS, {
        "geo_post_id": int(geo_post_id), "task_id": int(task_id)})
    superseded = [int(dict(r)["id"]) for r in (cur.fetchall() or [])]

    cur.execute(_BEGIN_GENERATION, {
        "geo_post_id": int(geo_post_id), "task_id": int(task_id)})
    row = cur.fetchone()
    if row is None:
        raise RevisionConflict(f"post={geo_post_id} 不存在")
    result = dict(row)
    result["superseded_task_ids"] = superseded
    return result


_MARK_SUPERSEDED = """
UPDATE geo_douyin_post_tasks
   SET superseded_at = now(), status = 'superseded', updated_at = now()
 WHERE id = %(task_id)s AND superseded_at IS NULL
RETURNING id
"""


def mark_task_superseded(cur, *, task_id: int) -> bool:
    cur.execute(_MARK_SUPERSEDED, {"task_id": int(task_id)})
    return cur.fetchone() is not None


_REPLACE_CARD = """
UPDATE geo_douyin_post_revisions
   SET cards_snapshot = jsonb_set(cards_snapshot, %(path)s::text[], %(card)s::jsonb, false),
       asset_manifest = %(asset_manifest)s::jsonb,
       manifest_hash = %(manifest_hash)s
 WHERE post_revision_id = %(post_revision_id)s
   AND status = 'active'
RETURNING post_revision_id
"""


def replace_card_in_active_revision(cur, *, post_revision_id: int, card_index: int,
                                    card: Mapping[str, Any],
                                    asset_manifest: Mapping[str, Any]) -> bool:
    """单卡重绘只改目标卡(规格 §3.3 / 03 §6)。

    🔴 `AND status = 'active'` 是那条判据的物理保证:
       「新版成功后旧版 card redraw 完成 → replace CAS 拒绝」。
       旧 revision 这时已被 `_SUPERSEDE_OTHERS` 改成 superseded ⇒ 零行 ⇒ 返回 False,
       调用方据此把这笔资金收敛(release),而不是把旧图插回新版。
    """
    manifest = dict(asset_manifest or {})
    cur.execute(_REPLACE_CARD, {
        "post_revision_id": int(post_revision_id),
        "path": "{" + str(int(card_index)) + "}",
        "card": json.dumps(dict(card), ensure_ascii=False),
        "asset_manifest": json.dumps(manifest, ensure_ascii=False),
        "manifest_hash": compute_manifest_hash(manifest),
    })
    return cur.fetchone() is not None


# ══════════════════════════════════════════════════════════════════════
# [#184 d1 · 2026-09-13] 从 `contract_worker._freeze_active_revision` **搬**过来,
# 不是复制:普通生产链(`production_task`)与合同产线要用**同一份**冻结逻辑。
# 复制一份的代价是可预期的 —— 将来谁给其中一份加一步(比如多冻一个字段),
# 另一份不会跟,而且漏得无声。合同链那边保留同名别名,两个老调用点逐字不变。
# ══════════════════════════════════════════════════════════════════════
def freeze_active_revision(cur, ctx: dict) -> Optional[int]:
    """把刚生成好的成品冻成一个 **active revision**,并让 post 指针指过去。

    🔴 快照取的是**库里刚落好的那一份**(生产已经 `update_post_assets` 完),
       不是内存里的 outcome —— 发布链后面要按 revision 发,而 revision 必须
       与库里那一版逐字相同。从内存里再造一份会出现"库是 A、发的是 B"。

    🔴 CAS 输了(`GenerationSuperseded`)**不抛**:说明这一篇已经被新一代生成
       接管,本次结果按规格 §5.6 只能标 superseded。这时候硬抛会把整个
       finish_task 事务回滚掉,任务重新变回"在跑",于是被永久重领。
    """

    cur.execute(
        "SELECT title, body_text, hashtags, cards, oss_keys, cover_oss_key,"
        "       style_key, contact_enabled, aspect_ratio, generation_meta"
        "  FROM geo_douyin_posts WHERE id = %s", (int(ctx["post_id"]),))
    post = dict(cur.fetchone() or {})
    oss_keys = [str(k) for k in (post.get("oss_keys") or []) if k]
    cards = list(post.get("cards") or [])
    manifest = {
        "oss_keys": oss_keys,
        "cover_oss_key": str(post.get("cover_oss_key") or ""),
        "card_count": len(oss_keys),
    }
    topic_snapshot = ctx.get("topic_snapshot")
    # 🔴 没有选题快照就**留空**,不用 topic_ref 顶替:两者不是同一个东西
    #    (ref 是指针,snapshot 是被指的内容)。用指针冒充快照会让
    #    「这一篇当时按哪个选题做的」这个事实在库里变成一句假话。
    topic_hash = snapshot_hash(dict(topic_snapshot)) if topic_snapshot else ""
    revision = stage_revision(
        cur, geo_post_id=int(ctx["post_id"]),
        created_by=int(ctx.get("actor_user_id") or ctx.get("payer_user_id") or 0),
        operation_kind=OPERATION_CREATE,
        title=str(post.get("title") or ""), body=str(post.get("body_text") or ""),
        hashtags=list(post.get("hashtags") or []),
        contact_enabled=bool(post.get("contact_enabled")),
        cards_snapshot=cards, asset_manifest=manifest,
        topic_snapshot_hash=topic_hash,
        render_input_hash=compute_render_input_hash(
            topic_snapshot_hash=topic_hash,
            style_key=str(post.get("style_key") or ""),
            ranking_template=str(((post.get("generation_meta") or {})
                                  .get("ranking") or {}).get("template") or "") or None,
            style_catalog_version=str(((post.get("generation_meta") or {})
                                       .get("style_catalog_version") or "")),
            card_count=len(oss_keys),
            aspect_ratio=str(post.get("aspect_ratio") or ""),
            content_form=str(((post.get("generation_meta") or {})
                              .get("ranking") or {}).get("effective_form") or "cards"),
            contact_enabled=bool(post.get("contact_enabled"))),
        style_catalog_version=str(((post.get("generation_meta") or {})
                                   .get("style_catalog_version") or "")))
    try:
        activate_revision(cur, geo_post_id=int(ctx["post_id"]),
                          post_revision_id=int(revision["post_revision_id"]),
                          task_id=int(ctx["task_id"]),
                          epoch=int(ctx.get("post_epoch") or 0))
    except (GenerationSuperseded, RevisionConflict) as exc:
        logger.info("[imgnote-worker] post=%s 的活动生成已易主,本次成品标 superseded:%s",
                    ctx["post_id"], exc)
        mark_task_superseded(cur, task_id=int(ctx["task_id"]))
        return None
    return int(revision["post_revision_id"])
