"""五步中的 ``preview`` + ``propose`` —— 服务端选媒体、算价、给可解释理由。

规格 §11.2 / §11.3 第 1-2 步。判据:MED-01/03/05/06/09、CUR-10、POR-06/07。

═══════════════════════════════════════════════════════════════════════
🔴 CUR-10 的修法:**显式列清单**,不是 ``SELECT *`` 之后再挑
═══════════════════════════════════════════════════════════════════════
``SELECT *`` 之后在 Python 里 pop 掉几个键,和显式列清单在**行为上不等价**:
前者在「有人给 mhz_media 加了一列 supplier_rebate_ratio」时静默泄漏,
后者在同样情况下**什么都不会发生**。CUR-10 要的是后者。

所以本模块的 SQL 里没有 ``*``:列名来自
``media_identity.PUBLIC_MEDIA_COLUMNS`` + 两列**只作 HMAC 输入**的私有原料
(``provider`` / ``provider_media_id``)。那两列进得来、出不去 ——
:func:`propose` 的返回值里没有它们的位置(``PublicMediaIdentity`` 是 NamedTuple,
字段是固定的)。

═══════════════════════════════════════════════════════════════════════
🔴 理由是**冻结的事实**,不是现场生成的话术
═══════════════════════════════════════════════════════════════════════
MED-05:「小榜只转述冻结理由,不现场生成」。所以 ``reasonFacts`` 的每一条都
来自一个**可复算的判断**(角色匹配 / 行业匹配 / 预算匹配),
而不是一句 LLM 写的推荐语。每条都带 ``kind``,前端按 kind 排版。

🔴 供应商采购价、毛利、返利**不进入推荐证据**(§11.2 末句)。
   排序用的是 ``our_price_points``(对服务商的最终算力),不是采购价 ——
   采购价这一列本模块**根本不读**。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from services.defensive_geo.publish import decision_snapshot as _ds
from services.defensive_geo.publish import media_identity as _mi

logger = logging.getLogger("GEO-DefGeoMediaProposal")

PROPOSAL_VERSION = "defgeo-media-proposal-v1"

#: 一次 preview 返回多少候选。decision 1 个 + alternatives N 个。
MAX_ALTERNATIVES = 5

#: 🔴 SQL 里真正会出现的列 = 公开 allowlist + 两列 HMAC 原料 + 价格列。
#:    ``our_price_points`` 是**对服务商的最终算力**(§11.5 表格第 4 行允许),
#:    不是采购价;采购价那几列(price/price1/price2/wholesale_*)不在这里。
_QUERY_COLUMNS: tuple[str, ...] = (
    *_mi.PUBLIC_MEDIA_COLUMNS, "provider", "provider_media_id", "our_price_points",
)

#: 🔴 **仅作 HMAC 输入**的两列私有原料。它们进得来、出不去 ——
#:    :func:`propose` 的返回是 ``PublicMediaIdentity`` NamedTuple,字段固定,
#:    没有它们的位置。把它们显式登记在这里,是为了让下面那条 import 期自证
#:    能区分「故意读的两列」和「不小心多读的第三列」。
#:    (第一版没有这个白名单,自证在 import 期直接把本模块炸掉 ——
#:     而它是被端点**惰性 import** 的,所以那条路径直到跑 census 汇总才第一次真跑。
#:     「路径从没被真跑过」的又一形态。)
_HMAC_INPUT_COLUMNS: frozenset[str] = frozenset({"provider", "provider_media_id"})


class ProposalError(RuntimeError):
    """选不出媒体。**不返回空方案让她自己拼** —— MED-03 要求给原因与人工调整。"""


def _assert_no_private_column_in_query() -> None:
    """自证:查询列里除**已登记的 HMAC 原料**外,不许有任何私有列。

    这个断言在**模块导入时**跑一次,所以「有人往 ``_QUERY_COLUMNS`` 里加了
    ``supplier_cost``」会在进程启动时炸,而不是等它泄漏到某个响应里。
    """
    bad = [c for c in _mi.private_columns_in(_QUERY_COLUMNS) if c not in _HMAC_INPUT_COLUMNS]
    if bad:
        raise RuntimeError(
            f"媒体查询列里出现未登记的私有列 {bad} —— CUR-10 的病根就是让私有列进了公开面。"
            f"如果确实需要它做身份原料,请显式加进 _HMAC_INPUT_COLUMNS 并说明为什么"
        )
    # 反向:登记表本身不许放空,也不许放非私有列(那样它就成了一张没有意义的白名单)。
    if not _HMAC_INPUT_COLUMNS:
        raise RuntimeError("_HMAC_INPUT_COLUMNS 为空 —— 上面的排除就成了空操作")
    stray = [c for c in _HMAC_INPUT_COLUMNS if not _mi.is_private_column(c)]
    if stray:
        raise RuntimeError(
            f"_HMAC_INPUT_COLUMNS 里有非私有列 {stray} —— 白名单只该用来放真正的私有原料"
        )


_assert_no_private_column_in_query()


def read_candidates(
    cur, *, industry: str | None, limit: int = 40,
) -> list[dict[str, Any]]:
    """从目录读候选行。**逐列写明,零 ``SELECT *``**。"""
    cols = ", ".join(_QUERY_COLUMNS)
    params: list[Any] = []
    where = ["is_active = TRUE", "source_domain IS NOT NULL", "source_domain <> ''"]
    if industry:
        where.append("(resource_type_name ILIKE %s OR media_name ILIKE %s)")
        params.extend([f"%{industry}%", f"%{industry}%"])
    params.append(int(limit))
    cur.execute(
        f"SELECT {cols} FROM mhz_media WHERE {' AND '.join(where)} "
        f"ORDER BY our_price_points ASC NULLS LAST, id ASC LIMIT %s",
        tuple(params),
    )
    rows = cur.fetchall() or []
    if rows and isinstance(rows[0], Mapping):
        return [dict(r) for r in rows]
    cols_desc = [d[0] for d in cur.description]
    return [dict(zip(cols_desc, r)) for r in rows]


def _role_for(row: Mapping[str, Any], *, prefer: str) -> str:
    """按目录事实推角色。**可复算** —— 同一行任何时候得同一个角色。"""
    name = str(row.get("media_name") or "")
    category = str(row.get("category") or "")
    if any(k in name or k in category for k in ("日报", "新闻网", "通讯社", "人民", "新华")):
        return "authority_anchor"
    if any(k in name or k in category for k in ("行业", "垂直", "专业", "协会")):
        return "strong_vertical"
    if prefer == "official_owned":
        return "official_owned"
    return "broad_discovery"


def _reason_facts(row: Mapping[str, Any], role: str, *, fits_budget: bool) -> tuple[tuple[str, str], ...]:
    """冻结理由事实。每条都指一个**目录里真有的**属性。"""
    facts: list[tuple[str, str]] = []
    role_copy = {
        "authority_anchor": "这家媒体公信力强，适合放主体资质和关键事实",
        "strong_vertical": "这家媒体在你所在的行业里更相关",
        "broad_discovery": "这家媒体覆盖面广，容易被搜到",
        "official_owned": "这是品牌自己的稳定事实源",
    }[role]
    facts.append(("authority_fit" if role == "authority_anchor" else "industry_fit", role_copy))
    # 🔴 ``category`` 在生产里历史错位、几乎全 NULL(db/publish_db.get_media_categories
    #    的 P0 注释逐字写过);真实有数据的是 ``resource_type_name``。
    #    先读真的那一列,读不到才回落 —— 顺序反了会让理由事实大面积为空。
    category = str(row.get("resource_type_name") or row.get("category") or "").strip()
    if category:
        facts.append(("audience_fit", f"这家媒体主要覆盖「{category}」这类内容"))
    area = str(row.get("area") or "").strip()
    if area:
        facts.append(("audience_fit", f"覆盖地区：{area}"))
    if fits_budget:
        facts.append(("budget_fit", "在这一单的预算范围内"))
    return tuple(facts)


def propose(
    rows: Sequence[Mapping[str, Any]],
    *,
    decision_snapshot_id: str,
    remaining_points: int,
    prefer_role: str = "broad_discovery",
) -> tuple[_ds.Candidate, list[_ds.Candidate]]:
    """§11.3 第 2 步:生成 1 个推荐 + 有序 alternatives。

    🔴 顺序是**服务端确定的**(目录价升序 + id 升序),并且这个顺序会进
       canonicalHash —— 换序必须改 hash(MED-21)。所以这里不许用
       会随机的排序(dict 顺序、set 迭代)。
    """
    if not rows:
        raise ProposalError("媒体目录里没有可用候选")
    built: list[_ds.Candidate] = []
    for ordinal, row in enumerate(rows[: MAX_ALTERNATIVES + 1]):
        role = _role_for(row, prefer=prefer_role)
        points = int(row.get("our_price_points") or 0)
        identity = _mi.project_media_row(
            row, media_role=role,
            decision_snapshot_id=decision_snapshot_id, ordinal=ordinal,
        )
        built.append(_ds.Candidate(
            identity=identity,
            reason_facts=_reason_facts(row, role, fits_budget=points <= remaining_points),
            exact_points=points,
        ))
    # 推荐 = 第一个在预算内的;都不在预算内就取最便宜的那个,
    # 让 blocker/options 去解释差额(而不是这里静默返回空)。
    decision = next((c for c in built if c.exact_points <= remaining_points), built[0])
    alternatives = [c for c in built if c.identity.public_media_option_id
                    != decision.identity.public_media_option_id]
    return decision, alternatives


def census() -> dict[str, Any]:
    return {
        "proposalVersion": PROPOSAL_VERSION,
        "queryColumns": list(_QUERY_COLUMNS),
        "maxAlternatives": MAX_ALTERNATIVES,
        "privateColumnsInQuery": _mi.private_columns_in(_QUERY_COLUMNS),
    }
