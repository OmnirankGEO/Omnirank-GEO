# -*- coding: utf-8 -*-
"""WO_241 丙 · 「缺题的词」免费补救的**授权**单点。

═══════════════════════════════════════════════════════════════════════
为什么有这个模块
═══════════════════════════════════════════════════════════════════════
「生成标题」那颗按钮有**两个面孔**(`WritingHall.tsx:6435` 是个三元):

| 面孔 | 钱 | 该怎么走 |
|---|---|---|
| 新加的词出标题(`isNewKwOnly`) | **从没付过费** | 全新生产 ⇒ 批量端点 + 子集,按真出题词数计费 |
| 某批次里缺题的词补救 | **已经付过费** | 免费,但要**证明**它确实是补救 |

拆的依据是**钱有没有付过**,不是端点名字、也不是按钮文案。

🔴 为什么补救必须免费而不是"同一张卡同一价":
   退费只在**一条题都没出**时触发
   (`server.py:api_generate_titles` 的 `if not topics:` / `if not topic_ids:`);
   **部分成功一分不退** —— 出了一部分、另一部分缺题时,用户已经按 N 个词付过钱了。
   组织路径同口径(`settle_charge(actual_points=预留上限)`,不按实际出题数)。
   这时候再收一次,就是**对同一个词收两次**。

🔴 为什么补救必须带证明而不是直接放行:
   不带证明的免费路径 = 一条**免费主路** —— 任何人对任何词调一次就白拿一次生成。

═══════════════════════════════════════════════════════════════════════
证明由三件组成,缺一不可
═══════════════════════════════════════════════════════════════════════
1. **这一批真的付过**  —— `title_batch_charges` 里有这个 `generation_request_id`,
   且 `charge_tx_id` 非空、`user_id` 与调用者一致、`quote_id` 与路径一致;
2. **这个词属于这一批** —— `topics` 里存在
   `(quote_id, keyword_id, generation_request_id)` 的行;
3. **这个词还没出题**   —— 该词**没有**任何 `optimized_title IS NOT NULL` 的行。

🔴 **钱的标识不下发前端、也不由前端回传。** 前端只带它**已经有的**
   `generation_request_id` —— 它来自项目详情里 topics 行的 `t.*`
   (`db/diagnosis_db.get_writing_project_detail` 用 `SELECT t.*`,
    前端 `WritingHall.tsx:4475` 已在读它)。
   ⚠️ 它**不来自** `POST /api/writing/generate-titles` 的回包 ——
   那个回包里没有 request_id(我核过回包的全部键)。契约必须说清出处,
   否则 A 会去一个不存在的地方取。
   服务端拿这个 id 到 `title_batch_charges` 里解出 charge,**校验权全在服务端**。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("GEO-TitleBatchCharge")

#: 拒绝时对外的 code(前端按 code 分支,文案可改、code 不可)
RECOVERY_DENIED_CODE = "TITLE_RECOVERY_NOT_AUTHORIZED"


def record_batch_charge(cursor, *, generation_request_id: str, user_id: int,
                        quote_id: int, charge_tx_id: Optional[int]) -> None:
    """记下「这一批由谁付的、付的是哪一笔」。

    幂等:同一个 `generation_request_id` 重复写只保留第一次
    (`ON CONFLICT DO NOTHING`)—— 重试不该把 charge 换成另一笔。
    """
    # 🔴 [Review 09-28 · WO_317 第四笔] 个人路径现在先 `claim_batch` 落一行 charge_tx_id=NULL 的抢占行,
    #    扣完由这里**回填**;已有扣费号的行不覆盖(仍是「只保留第一次」)。
    cursor.execute(
        "INSERT INTO title_batch_charges"
        " (generation_request_id, user_id, quote_id, charge_tx_id)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (generation_request_id) DO UPDATE"
        " SET charge_tx_id = EXCLUDED.charge_tx_id"
        " WHERE title_batch_charges.charge_tx_id IS NULL"
        " AND title_batch_charges.user_id = EXCLUDED.user_id",
        (str(generation_request_id), int(user_id), int(quote_id),
         int(charge_tx_id) if charge_tx_id is not None else None),
    )


#: 抢占行被视为「被遗弃」的时限(秒)。取生成标题接口最长耗时再留余量(Review 09-28 定 30 分钟)。
TITLE_CLAIM_ABANDON_SECONDS = 30 * 60


def claim_batch(cursor, *, generation_request_id: str, user_id: int, quote_id: int) -> Optional[dict]:
    """个人路径扣费**之前**抢占这个请求编号。

    返回 None ⇒ 抢不到(调用方 409);否则 ``{"charge_tx_id": 已付那笔 | None, "taken_over": bool}``:
      · 新抢到 ⇒ charge_tx_id None、taken_over False —— 正常扣费;
      · 接管被遗弃的行 ⇒ charge_tx_id None —— 正常扣费。
    🔴 [Review 09-28 · WO_317 第六笔] **已付且未退款的行永不接管**(一律 409):
       第五笔的判据「这个编号下没存下带题选题」查的 topics.generation_request_id 会被正常流程改写
       (开写文章把它改成文章编号、整表重出删旧草稿),已交付的已付批次 30 分钟后同编号重放
       会被当成遗弃:接管后免费再出题,或带坏文体按原那一笔全额退(Review 真库复现 R1–R3)。
       扣费后、生成中被掐的已付批次由 WO_241 丙逐词免费补救兜底(受理凭据 pending、带原编号)。

    🔴 [Review 09-28 · WO_317 第五笔] 「被遗弃」= 同一用户、同一报价单的行,超过
       `TITLE_CLAIM_ABANDON_SECONDS` 且这个编号下**没有存下任何带标题的选题**
       (成功保存的选题由 save_topics_batch 盖上编号)。发车切槽掐掉在途请求时:
       ① 抢占后、扣费前被掐 ⇒ 行留着 charge_tx_id 为空;② 扣费后、生成中被掐 ⇒ 钱扣了、题没出、
       失败退费没跑。不接管的话,同内容再点(编号是指纹)永远 409。
       接管在**一条 SQL** 里做完(ON CONFLICT … DO UPDATE … WHERE … RETURNING):
       并发的两个接管者,第二个在第一个更新后重评 WHERE(created_at 已刷新 ⇒ 未超时)⇒ 拿不到 ⇒ 409。
    """
    cursor.execute(
        "INSERT INTO title_batch_charges AS b (generation_request_id, user_id, quote_id, charge_tx_id)"
        " VALUES (%s, %s, %s, NULL)"
        " ON CONFLICT (generation_request_id) DO UPDATE SET"
        "   created_at = LOCALTIMESTAMP,"
        "   charge_tx_id = CASE WHEN b.charge_tx_id IS NOT NULL AND EXISTS ("
        "       SELECT 1 FROM point_transactions p WHERE p.type = 'refund'"
        "          AND p.order_id = b.charge_tx_id::text)"
        "     THEN NULL ELSE b.charge_tx_id END"
        " WHERE b.user_id = EXCLUDED.user_id"
        "   AND b.quote_id = EXCLUDED.quote_id"
        "   AND (b.charge_tx_id IS NULL OR EXISTS ("
        "       SELECT 1 FROM point_transactions p WHERE p.type = 'refund'"
        "          AND p.order_id = b.charge_tx_id::text))"
        "   AND b.created_at < LOCALTIMESTAMP - make_interval(secs => %s)"
        "   AND NOT EXISTS (SELECT 1 FROM topics t WHERE t.quote_id = b.quote_id"
        "          AND t.generation_request_id = b.generation_request_id"
        "          AND t.optimized_title IS NOT NULL)"
        " RETURNING b.charge_tx_id, (xmax <> 0) AS taken_over",
        (str(generation_request_id), int(user_id), int(quote_id), int(TITLE_CLAIM_ABANDON_SECONDS)),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    row = dict(row) if not isinstance(row, dict) else row
    ctx = row.get("charge_tx_id")
    return {"charge_tx_id": int(ctx) if ctx is not None else None, "taken_over": bool(row.get("taken_over"))}


def release_batch_claim(cursor, *, generation_request_id: str, user_id: int) -> None:
    """整批失败并已退费后清掉抢占行,让用户用同一编号重试时能重新扣费(而不是永远 409)。

    只删本人这一行。部分成功 / 成功的批次**不许**调它 —— 那一行是免费补救的凭据。
    """
    cursor.execute(
        "DELETE FROM title_batch_charges WHERE generation_request_id = %s AND user_id = %s",
        (str(generation_request_id), int(user_id)),
    )


def authorize_recovery(cursor, *, generation_request_id: Any, user_id: Any,
                       quote_id: int, keyword_id: int) -> tuple[bool, str]:
    """能不能对这个词做**免费**补救。

    返回 `(允许?, 理由)`。理由只进日志 —— 对外一律同一个 code + 同一句文案,
    不让调用方靠"拒绝理由"去探别人的批次存不存在。
    """
    if not generation_request_id:
        return False, "no_request_id"
    rid = str(generation_request_id)

    # ① 这一批真的付过,且付的人就是你、批次就在这个 quote 下
    cursor.execute(
        "SELECT user_id, quote_id, charge_tx_id FROM title_batch_charges"
        " WHERE generation_request_id = %s",
        (rid,),
    )
    row = cursor.fetchone()
    if not row:
        return False, "unknown_batch"
    _uid = row["user_id"] if isinstance(row, dict) else row[0]
    _qid = row["quote_id"] if isinstance(row, dict) else row[1]
    _ctx = row["charge_tx_id"] if isinstance(row, dict) else row[2]
    if int(_uid) != int(user_id):
        return False, "batch_belongs_to_another_user"
    if int(_qid) != int(quote_id):
        return False, "batch_belongs_to_another_quote"
    if _ctx is None:
        # 没扣成过就没有"已经付过费"这回事 ⇒ 免费补救的前提不成立
        return False, "batch_was_never_charged"

    # ①b 这一笔**没有被退款**
    #
    # 🔴 [Review 追加 2026-09-19] 整批失败会**全额退**
    #    (`refund_points(amount=None)` = 退到原扣费全额)。退完之后,
    #    那一批的词**不再是「已付」** —— 这时候再给免费补救,就是白送一次生成。
    #    重试应当走批量、重新收费。
    #
    # 观测点与 WO_241 丙 2/3 用的是同一个:退费行是 `point_transactions` 里
    # `type='refund'` 且 `order_id = 被退那笔 consume 的 id(文本)`
    # (见 `middleware/billing.py` 的 `_legacy_consume_columns`)。
    cursor.execute(
        "SELECT 1 FROM point_transactions WHERE type = 'refund'"
        " AND order_id = %s LIMIT 1",
        (str(int(_ctx)),),
    )
    if cursor.fetchone() is not None:
        return False, "batch_charge_was_refunded"

    # ② 这个词属于这一批
    cursor.execute(
        "SELECT 1 FROM topics WHERE quote_id = %s AND keyword_id = %s"
        " AND generation_request_id = %s LIMIT 1",
        (int(quote_id), int(keyword_id), rid),
    )
    if cursor.fetchone() is None:
        return False, "keyword_not_in_batch"

    # ③ 这个词还没出题(有成品就不是"缺题",那是重复生成)
    cursor.execute(
        "SELECT 1 FROM topics WHERE quote_id = %s AND keyword_id = %s"
        " AND optimized_title IS NOT NULL LIMIT 1",
        (int(quote_id), int(keyword_id)),
    )
    if cursor.fetchone() is not None:
        return False, "keyword_already_has_a_topic"

    return True, "ok"
