"""U-9 · 「发给客户」统一面板(§0.5.5 U-9 @ spec e710be6c2)。

U-9 逐字::

    四类 token 链接(报告/报价/监测/portal)从统一面板生成,自动带人话前缀
    (【诊断报告】【报价单·请确认】…);过期/撤销时一键
    「重新签发**同对象**新链接」。

═══════════════════════════════════════════════════════════════════
🔴 为什么不去改 M3 那个六类抽屉
═══════════════════════════════════════════════════════════════════
``前端 M3 组件目录/workbench/M3 客户链接抽屉.tsx`` 已经有一个
六类链接抽屉(``/api/m3/customer-links/{brand_id}``)。两件事让它不能直接当
U-9 用:

  · **板块边界**。M3 是另一个板块的地界(CLAUDE.md「M3 板块边界」),
    而 U-9 是防御型 GEO 规格里的条款。把 defgeo 的条款塞进 M3 抽屉,
    等于让两个板块的验收互相绑架。
  · **它没有 U-9 要的两件东西**:没有人话前缀(只有"复制话术"这个另一件事),
    也没有「重新签发同对象新链接」。

所以这里是 defgeo 自己的四类面板。**不复制** M3 的实现,也不去动它。

═══════════════════════════════════════════════════════════════════
🔴 「监测快照」这一类现在拿不到链接 —— 如实说,不编
═══════════════════════════════════════════════════════════════════
现役全仓没有"监测快照"的客户侧 token 路由(``client_access_tokens`` 是门户,
``diagnosis_records.share_token`` 是报告,``keyword_selection_sessions.token``
是「报价单·请确认」那一页)。它在面板里恒 ``not_ready`` 并给出人话原因。

🔴 [工单 C-4 2026-08-25] 上面这句括号里原本写的是 ``agent_quotes.share_code``,
   而那是**错的对象**:``agent_quotes`` 是服务商白标报价**展示**页
   (``/q/:code``),按 ``user_id`` 归属、与品牌无关、客户在那一页没有
   "确认"这个动作;它连 ``brand_id`` 列都没有。真正的「报价单·请确认」
   对象是选词会话 ``/s/{token}``。详见 :func:`build_panel` 第 ② 段。

编一个假 URL 出来会让销售把一个 404 发给客户 —— 那比"这一类还没有"糟得多。
这条缺口在交付单里如实记着,由监测那一窗补路由后本模块自然亮。
"""

from __future__ import annotations

import logging
from typing import Any

from services.defensive_geo.presentation import copy_registry as _pcopy

logger = logging.getLogger("GEO-DefGeoLinks")

LINKS_VERSION = "defgeo-customer-links-v1"

#: U-9 点名的四类。**闭集**,顺序即面板顺序(销售最常发的排前面)。
LINK_KINDS: tuple[str, ...] = (
    "diagnosis_report", "quote_proposal", "monitoring_snapshot", "customer_portal",
)

LinkKind = str

#: 🔴 [工单 V4-C · C-2 · Codex fix-of-fix P2-2] **显式 scoped-out**:
#: 规格里有这一类,但我们**没有交付它**,所以它根本不出现在面板上。
#:
#: 为什么不是继续显示 ``not_ready``:上一版把它当成"还没到时候"摆在面板里,
#: 配的文案是「做完会自动出现在这里」。而事实是 —— 现役全仓既没有监测快照的
#: 客户侧 token 路由,``defgeo_report_snapshots`` 也**零生产写入方**(已 census)。
#: 也就是说"前一步"没有人在做,它永远不会自己出现。
#: 把一个永远不会亮的格子摆在销售面前、还承诺它会亮,是我们在替一条不存在的
#: 能力打包票 —— 这比"这一类还没有"糟得多。
#:
#: 为什么**不从** :data:`LINK_KINDS` 里删掉:删掉就抹去了"规格要求过这一类"
#: 这个事实,下一个人会以为四类里本来就只有三类。声明保留、投影排除,
#: 两件事分开记 —— 而且这样它是**可机读**的(``census()["scopedOutKinds"]``),
#: 不是靠读注释才知道。
#:
#: 监测那一窗把客户侧路由接上、且快照链有了写入方之后,从这里移除即可自然亮。
SCOPED_OUT_KINDS: frozenset[str] = frozenset({"monitoring_snapshot"})

#: 只有门户这一类有**现役**轮换原语(``db.monitoring_db.generate_client_token``:
#: 同一事务里失效旧 token + 签发新 token,正是「同对象新链接」)。
#: 其余三类没有 —— 写成数据,判据拿它对账,不靠读注释。
#: 🔴 [工单 V3-A · Codex 三审 P1-8] ``quote_proposal`` 进来了。
#:
#: 它以前不在这个集合里,而 ``customer_link_expired`` 的文案逐字写着
#: 「点一下就能重新签发一个新的发给客户」—— UI 只在 ``reissuable=true`` 时
#: 才显示按钮 ⇒ **文案许诺了一条不存在的路径**。那不是"少个功能",
#: 是我们对着用户说了一句做不到的话。
#:
#: 修法按工单二选一里的第一条:**接现役 ``extend_session``**
#: (``POST /api/keyword-selection/{token}/extend`` 已经上线,延 7 天 +
#: 过期时恢复 ``selecting``)。复用它那两个 db 原语,不另写一份延期逻辑。
REISSUABLE_KINDS: frozenset[str] = frozenset({"customer_portal", "quote_proposal"})


class CustomerLinkNotExtendable(RuntimeError):
    """会话已经推进到不可回退的状态(confirmed 及之后),延期必须**拒绝**。

    🔴 [工单 V4-A · Codex fix-of-fix P1-2b] 不是"延期失败"这种技术故障,
       是"这件事现在不该做":报价已经确认了,把它改回选词态是业务状态回退。
    """

    def __init__(self, token, status):
        self.token = token
        self.status = status
        super().__init__("selection session %s is %s, not extendable" % (token, status))


class CustomerLinkObjectDrifted(RuntimeError):
    """面板下发的对象与重签时的当前对象**不是同一个**。

    🔴 [工单 V3-A · Codex 三审 P1-8] 为什么必须是个**错误**而不是"按当前对象重签":
       她看着 A 的过期链接点"重新签发",期间新建了报价 B。按当前对象重签
       换掉的是 **B** 的 token —— A 仍然是坏的,而客户手上 B 那个**好好的**链接
       刚被作废。两头都错,而且没有任何提示。
    """

    def __init__(self, expected, actual):
        self.expected = expected
        self.actual = actual
        super().__init__("customer link object drifted: %r != %r" % (expected, actual))

#: 状态闭集。``not_ready`` 与 ``expired`` 分开:一个是"还没到时候",
#: 一个是"过了" —— 她的下一步完全不同(等 vs 重签)。
LINK_STATUSES: tuple[str, ...] = ("available", "expired", "revoked", "not_ready")


def reissue_supported(kind: str) -> bool:
    return kind in REISSUABLE_KINDS


def _prefix(kind: str) -> str:
    """人话前缀。名字取自 copy registry,这里只加书名号 ——
    在两个地方各写一份名字,迟早有一处漏改。"""
    return f"【{_pcopy.translate('customer_link_kind', kind)}】"


class CustomerLinksUnavailable(RuntimeError):
    """这一次读不出客户链接面板。

    🔴 [工单 C-4 · Codex 终审 P1-11] 它存在的全部意义:**让查询失败留痕**。

    改动前 :func:`build_panel` 把整段查询包在一个 ``except Exception`` 里,
    异常一来就回一组 ``not_ready``。于是「这一步还没做到」与
    「我们的 SQL 写错了」在屏幕上**长得一模一样** —— 而实际发生的正是后者:
    ``agent_quotes`` 表根本没有 ``brand_id`` 列(生产 schema 实核:
    id/user_id/services/total_price/whitelabel/share_code/is_active/created_at),
    那条 ``WHERE brand_id = %s`` 每次都抛 ``UndefinedColumn``,
    而且因为它排在门户查询**之前**,报价与门户**两类**从上线起恒 ``not_ready``。
    没有任何东西会报错,销售只会以为"还没到时候"。

    所以现在:查询失败一律抛,由端点翻成 typed 错误 + 留 ERROR 日志。
    """


def _entry(
    *, kind: str, url: str | None, status: str,
    brand_name: str, blocked_reason_key: str | None = None,
    object_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in LINK_STATUSES:
        raise ValueError(f"未知链接状态 {status!r};合法 = {list(LINK_STATUSES)}")
    prefix = _prefix(kind)
    # 复制出去就是能直接发微信的一整段 —— 前缀 + 品牌名 + 链接。
    script = f"{prefix}{brand_name}\n{url}" if url else None
    from services.defensive_geo.copy_registry import try_user_label

    return {
        "kind": kind,
        "kindLabel": _pcopy.translate("customer_link_kind", kind),
        "prefix": prefix,
        "url": url,
        "status": status,
        "statusLabel": _pcopy.translate("customer_link_status", status),
        "reissuable": reissue_supported(kind) and status in ("expired", "revoked"),
        "wechatScript": script,
        "blockedReason": try_user_label("reason", blocked_reason_key) if blocked_reason_key else None,
        # 🔴 [工单 C-4]「面板所示对象 == 重签对象」的**可机读**形态。
        #    多 quote 的品牌上,面板与重签各自 ORDER BY 一次的话,两边可以
        #    落在不同的 quote 上 —— 于是她看着 A 的过期链接点"重新签发",
        #    实际给客户换的是 B 的门户 token。判据比的就是这一位。
        "objectRef": dict(object_ref) if object_ref else None,
        "linksVersion": LINKS_VERSION,
    }


def canonical_quote_id(cur, brand_id: int) -> int | None:
    """这个品牌**当前**那一份报价。面板与重签**共用这一个**取数口。

    🔴 [工单 C-4]「面板所示对象 == 重签对象」在结构上的落点。
       改动前面板按 ``client_access_tokens.id DESC`` 反查 quote、
       重签按 ``quotes.created_at DESC`` 正查 quote,**两条独立排序**:
       多 quote 的品牌上它们可以落在不同的 quote 上,于是"重新签发"
       换掉的不是她面板上看着的那一个。
       同一个谓词写两处 ⇒ 必有一处没人验 —— 所以只留这一处。

    🔴 ``ORDER BY created_at DESC, id DESC``:``created_at`` 可能并列
       (同一批建的),只按它排的话"最新的那一份"在两次查询之间可以变。
       补 ``id DESC`` 让顺序**确定**。
    🔴 ``deleted_at IS NULL``:已删的 quote 不该成为发给客户的对象。
       (改动前面板那条 JOIN 没有这个条件、重签有 —— 又一处不一致。)
    """
    cur.execute(
        """SELECT id FROM quotes
            WHERE brand_id = %s AND deleted_at IS NULL
            ORDER BY created_at DESC, id DESC LIMIT 1""",
        (int(brand_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return int(row[0] if isinstance(row, tuple) else row["id"])


def _selection_expired(expires_at: Any, status: str) -> bool:
    """选词/报价确认页是否已过期。

    ``keyword_selection_sessions.expires_at`` 是 **TEXT**(生产 schema 实核),
    现役 ``selection_api._check_expired`` 用 ``datetime.fromisoformat`` 解析它 ——
    这里**逐字沿用同一个解析口径**,不另写一套(两套解析在格式边角上必然分叉)。

    与现役同一条豁免:已经进入 ``confirmed`` 及之后那几档的会话**不再算过期**
    (客户已经确认了,链接仍然可以打开看)。
    """
    from datetime import datetime

    if str(status or "") in (
        "confirmed", "pending_payment", "active", "payment_overdue",
        "pricing_pending_review", "business_lines_submitted",
    ):
        return False
    if str(status or "") == "expired":
        return True
    if not expires_at:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(str(expires_at))
    except (ValueError, TypeError):
        # 解析不出来就**不判过期** —— 与现役同口径。把一个解析不了的时间
        # 当成"已过期"会让她去重签一个本来好好的链接。
        return False


def build_panel(*, brand_id: int, brand_name: str) -> list[dict[str, Any]]:
    """四类逐个解析。**只读**:一个 token 都不签发、不轮换、不延期。

    GET 顺手换 token 的后果很具体:她打开面板看一眼,客户手里那个链接就废了。

    🔴 [工单 C-4] 查询失败**抛**,不再静默回 ``not_ready``:见
       :class:`CustomerLinksUnavailable` 的说明。
    """
    from db.diagnosis_db import get_connection

    report_url: str | None = None
    report_ref: dict[str, Any] | None = None
    quote_url: str | None = None
    quote_ref: dict[str, Any] | None = None
    quote_expired = False
    portal_url: str | None = None
    portal_ref: dict[str, Any] | None = None
    portal_expired = False
    portal_revoked = False

    conn = get_connection()
    try:
        cur = conn.cursor()
        # ① 诊断报告:published-only(与 M3 同口径 —— 结算前/已退款的不算可发)
        cur.execute(
            """SELECT id, share_token FROM diagnosis_records
                WHERE brand_id = %s
                  AND (result_visibility IS NULL OR result_visibility = 'published')
                ORDER BY created_at DESC LIMIT 1""",
            (brand_id,),
        )
        row = cur.fetchone()
        if row and row.get("share_token"):
            report_url = f"/public/report/{row['id']}?st={row['share_token']}"
            report_ref = {"kind": "diagnosis_record", "id": int(row["id"])}

        # ② 报价单·请确认
        #
        # 🔴 [工单 C-4] **对象语义订正**。改动前查的是
        #    ``agent_quotes WHERE brand_id = %s`` —— 两处都错:
        #      · 列不存在(生产 ``agent_quotes`` 只有 id/user_id/services/
        #        total_price/whitelabel/share_code/is_active/created_at),
        #        每次必抛 UndefinedColumn;
        #      · 对象也不对。``agent_quotes`` 是**服务商白标报价展示页**
        #        (``/q/:code`` → PublicQuote),按 ``user_id`` 归属、
        #        与品牌无关,而且客户在那一页**没有"确认"这个动作**。
        #    而这一类的人话前缀逐字是「报价单·请确认」——
        #    客户要确认的对象是**选词会话**:``/s/{token}`` 那一页,
        #    它的 ``POST /s/{token}/confirm-quote`` 才是 accepted snapshot、
        #    activation outbox、整条防御型 GEO 商业基线的起点。
        #    发一个没有确认按钮的白标展示页给客户,前缀就是在骗人。
        cur.execute(
            """SELECT token, status, expires_at FROM keyword_selection_sessions
                WHERE brand_id = %s
                ORDER BY id DESC LIMIT 1""",
            (brand_id,),
        )
        row = cur.fetchone()
        if row and row.get("token"):
            quote_ref = {"kind": "keyword_selection_session",
                         "token": str(row["token"])}
            if _selection_expired(row.get("expires_at"), str(row.get("status") or "")):
                quote_expired = True
            else:
                quote_url = f"/s/{row['token']}"

        # ④ 周月报门户:token 绑在 quote 上(现役 client_access_tokens 形态)
        #
        # 🔴 [工单 C-4] quote 走 :func:`canonical_quote_id` ——
        #    与 :func:`reissue_link` **同一个**取数口。
        quote_id = canonical_quote_id(cur, brand_id)
        if quote_id is not None:
            portal_ref = {"kind": "quote", "id": int(quote_id)}
            cur.execute(
                """SELECT token, expires_at, is_active
                     FROM client_access_tokens
                    WHERE quote_id = %s
                    ORDER BY id DESC LIMIT 1""",
                (int(quote_id),),
            )
            row = cur.fetchone()
            if row and row.get("token"):
                # 🔴 [工单 C-4] ``expires_at`` **参与状态判定**。
                #    改动前只看 ``is_active``:一个 is_active=1 但
                #    ``expires_at`` 早已过去的 token 会显示「可以发了」,
                #    她发出去,客户点开是一个失效页。
                #    列是 **DATE**(生产 schema 实核),所以按日期比 ——
                #    当天到期的算还没过(与"当天有效"的直觉一致)。
                from datetime import date as _date

                exp = row.get("expires_at")
                if not row.get("is_active"):
                    portal_revoked = True
                elif isinstance(exp, _date) and exp < _date.today():
                    portal_expired = True
                else:
                    portal_url = f"/portal/{row['token']}"
        conn.rollback()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        # 🔴 [工单 C-4] 留痕报错,**不**静默 not_ready。
        #    exc_info 必须带:上一版那句 logger.info 连堆栈都没有,
        #    于是"哪张表哪一列"这件事在日志里也查不到。
        logger.error("[defgeo-links] 读客户链接失败 brand=%s:%s", brand_id, exc,
                     exc_info=True)
        raise CustomerLinksUnavailable(str(exc)) from exc
    finally:
        conn.close()

    # 🔴 ③「监测快照」**不在这里**:它是 scoped-out 的
    #    (见 :data:`SCOPED_OUT_KINDS`),不投影、不占位、不承诺。
    panel = [
        _entry(kind="diagnosis_report", url=report_url, brand_name=brand_name,
               status="available" if report_url else "not_ready",
               blocked_reason_key=None if report_url else "customer_link_not_ready",
               object_ref=report_ref),
        _entry(kind="quote_proposal", url=quote_url, brand_name=brand_name,
               status=("available" if quote_url
                       else "expired" if quote_expired else "not_ready"),
               blocked_reason_key=(None if quote_url
                                   else "customer_link_expired" if quote_expired
                                   else "customer_link_not_ready"),
               object_ref=quote_ref),
        _entry(kind="customer_portal", url=portal_url, brand_name=brand_name,
               status=("available" if portal_url
                       else "revoked" if portal_revoked
                       else "expired" if portal_expired else "not_ready"),
               blocked_reason_key=(None if portal_url
                                   else "customer_link_revoked" if portal_revoked
                                   else "customer_link_expired" if portal_expired
                                   else "customer_link_not_ready"),
               object_ref=portal_ref),
    ]
    # 机械兜底:哪怕上面哪天有人手滑加回来,scoped-out 的也不许流出去。
    return [e for e in panel if e["kind"] not in SCOPED_OUT_KINDS]


def _canonical_selection_token(cur, brand_id: int) -> str | None:
    """这个品牌**当前**那一份选词确认会话的 token。

    与 :func:`build_panel` 的 ②「报价单·请确认」**同一个**取数口 ——
    面板与重签各自写一条 ORDER BY 的话,两边可以落在不同的会话上,
    那正是 P1-8 在门户那一类上的同族形态。
    """
    cur.execute(
        """SELECT token FROM keyword_selection_sessions
            WHERE brand_id = %s
            ORDER BY id DESC LIMIT 1""",
        (int(brand_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return str(row[0] if isinstance(row, tuple) else row["token"])


def _assert_same_object(expected: dict | None, actual: dict | None) -> None:
    """CAS:面板下发的对象 == 此刻的当前对象。不符就抛,**什么都还没动**。"""
    if expected is None:
        raise CustomerLinkObjectDrifted(None, actual)
    want = {str(k): str(v) for k, v in dict(expected).items()}
    got = {str(k): str(v) for k, v in dict(actual or {}).items()}
    if want != got:
        raise CustomerLinkObjectDrifted(want, got)


def reissue_link(*, kind: str, brand_id: int, brand_name: str,
                 expected_object_ref: dict | None,
                 actor_user_id: int | None = None,
                 actor_username: str | None = None,
                 request_id: str | None = None) -> dict[str, Any] | None:
    """重新签发**同对象**新链接。

    🔴 [工单 V3-A · Codex 三审 P1-8] 「同对象」现在是**可执行的约束**,不是注释
    ------------------------------------------------------------------------
    改动前:GET 面板下发 ``objectRef``,POST 却只收 ``brandId+kind``,
    然后**再跑一次** ``canonical_quote_id()`` 取"最新"。两次取数之间新建一份报价,
    她看的是 A、轮换掉的是 B —— A 仍然是坏的,客户手上 B 那个好好的链接被作废。
    同一个取数口不代表同一个答案:C-4 修掉的是"两条 ORDER BY",
    这里修的是**两个时刻**。

    现在:调用方必须把面板下发的那个 ``objectRef`` 原样带回来;
    比对分两层 ——
      · 进函数先比一次(快速失败,报错清楚,**一个 token 都没动**);
      · 门户那一类再把同一个谓词作为 ``in_tx_guard`` 交给
        ``generate_client_token``,**在它失效旧 token 的那个事务里**再确认一次。
        只比第一层的话,两次之间仍然有窗口 —— 那正是本条 finding 的形状。

    🔴 [P2-8] 凭据轮换落 actor/request 审计
    --------------------------------------
    ``generate_client_token`` 早就支持 ``actor_user_id/actor_username/request_id``
    并在**同一事务**里写 audit_logs,但只有 actor 非空时才写 —— 而本调用方
    以前**一个都没传** ⇒ defgeo 这条轮换路径零审计。现在如实传。
    """
    if not reissue_supported(kind):
        return None
    from db.diagnosis_db import get_connection

    # ── quote_proposal:接现役 extend_session,不另写一份延期逻辑 ──────────
    if kind == "quote_proposal":
        conn = get_connection()
        try:
            cur = conn.cursor()
            token = _canonical_selection_token(cur, brand_id)
            conn.rollback()
        finally:
            conn.close()
        if token is None:
            return None
        _assert_same_object(expected_object_ref,
                            {"kind": "keyword_selection_session", "token": token})

        # 🔴 [工单 V4-A · Codex fix-of-fix P1-2b] 走**原子**原语。
        #    上一版是 `get_session_by_token()` → Python 里算 → `update_session()`,
        #    三条独立连接上的读→算→盲写。Codex 反例:读到 expired 之后并发推进成
        #    confirmed,延期仍把状态覆盖回 selecting —— **已确认的报价被重新打开**。
        from db.diagnosis_db import extend_selection_session_atomically

        result = extend_selection_session_atomically(token, days=7)
        if not result.get("ok"):
            if result.get("reason") == "not_found":
                return None
            # 🔴 typed 拒绝,不是「看起来成功了但什么都没变」。
            raise CustomerLinkNotExtendable(token, str(result.get("status") or ""))
        return _entry(kind=kind, url=f"/s/{token}", status="available",
                      brand_name=brand_name,
                      object_ref={"kind": "keyword_selection_session", "token": token})

    # ── customer_portal:轮换门户 token ──────────────────────────────────
    #
    # 🔴 这一支写成**显式**的 `kind ==` 而不是"剩下的都走这里":
    #    `REISSUABLE_KINDS` 里的每一类都必须在本函数里找得到自己那一段
    #    (pkgh 那条不变量判据就是这么核的)。落到 fall-through 的话,
    #    往集合里塞一个没实现的类别不会有任何判据变红 —— 又变回
    #    「看着能点、点了什么也没发生」。
    if kind != "customer_portal":
        return None

    # 🔴 [工单 V4-A · Codex fix-of-fix P1-2a] 轮换的是**她看到的那个 exact object**,
    #    不再"进来之后自己再求一次 canonical"。求出来的那个是**另一个时刻**的答案,
    #    而她点的是屏幕上那一行。对象身份由必传的 objectRef 给出,这里只做形状校验。
    if not isinstance(expected_object_ref, dict)             or str(expected_object_ref.get("kind")) != "quote"             or not expected_object_ref.get("id"):
        raise CustomerLinkObjectDrifted(expected_object_ref, {"kind": "quote", "id": None})
    # 🔴 [工单 V5-A · Codex fix-of-fix2 P2-2] ``int()`` 会对 "abc" 抛 ``ValueError``,
    #    而 ValueError 在端点那一层被兜成 **500 INTERNAL_ERROR**(「我们这边坏了」)。
    #    HTTP 入口现在有 typed model 挡在前面,但本函数是**公开可调**的域层 API:
    #    只在入口修等于假定"所有调用方都走那个入口",而这个假定没有任何东西在守。
    #    所以这里也给 typed —— 身份不合法就是身份不合法,与"对象漂移"同一类。
    try:
        quote_id = int(expected_object_ref["id"])
    except (TypeError, ValueError):
        raise CustomerLinkObjectDrifted(
            expected_object_ref, {"kind": "quote", "id": None}) from None

    def _in_tx_recheck(cursor) -> None:
        """**在轮换那个事务里**:先拿品牌级序列化点,再复核 canonical。

        🔴 顺序有意 —— 锁在前、读在后。反过来的话读到的仍是一个"拿到锁之前"的答案,
           和没锁一样。拿到锁之后,同一品牌的建报价必须排在我们后面
           (三个生产建报价站点都拿同一把锁,见 lock_brand_quote_serialization_point)。
        🔴 此刻**一个 token 都还没失效** —— 抛出去就是"一行不写"。
        """
        from db.diagnosis_db import lock_brand_quote_serialization_point

        lock_brand_quote_serialization_point(cursor, brand_id)
        _assert_same_object(expected_object_ref,
                            {"kind": "quote",
                             "id": int(canonical_quote_id(cursor, brand_id) or 0)})

    from db.monitoring_db import generate_client_token

    result = generate_client_token(
        quote_id=int(quote_id), brand_name=brand_name,
        actor_user_id=actor_user_id, actor_username=actor_username,
        request_id=request_id, reason="defgeo_customer_link_reissue",
        in_tx_guard=_in_tx_recheck)
    token = (result or {}).get("token") if isinstance(result, dict) else result
    if not token:
        return None
    return _entry(kind=kind, url=f"/portal/{token}", status="available",
                  brand_name=brand_name,
                  object_ref={"kind": "quote", "id": int(quote_id)})


def census() -> dict[str, Any]:
    return {
        "version": LINKS_VERSION,
        "kinds": list(LINK_KINDS),
        # 规格声明了、但**没有交付**的类:面板不投影它们。
        "scopedOutKinds": sorted(SCOPED_OUT_KINDS),
        "statuses": list(LINK_STATUSES),
        "reissuableKinds": sorted(REISSUABLE_KINDS),
        "prefixes": {k: _prefix(k) for k in LINK_KINDS},
        # [工单 C-4] 可机读断言:面板与重签共用同一个报价取数口;
        # 报价单那一类指向的是**选词确认会话**,不是白标展示页。
        "canonicalQuoteReader": "services.defensive_geo.customer_links.canonical_quote_id",
        "quoteProposalObject": "keyword_selection_sessions",
        "swallowsQueryErrors": False,
    }
