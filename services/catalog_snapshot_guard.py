"""目录同步「部分快照」守卫 —— 单点实现，四条同步链共用。

分页拉取的目录同步都有同一个失效模式：上游分页中断 / session 过期 / 首页返回空，
调用方只拿到**部分快照**，若据此把「本地有、快照里没有」的记录全部 ``is_active=FALSE``，
会一次性静默下架十几万条可售媒体 —— 前台直接少一半货，且没有任何报错。

2026-08-10 实测的三处现状：

* ``services/kuaiyibo/media_sync.py::sync_catalog`` 下架分支**没有任何阈值保护**。
* ``api/meijiehezi_api.py`` 三个 admin 手动同步端点**连守卫都没有**（点一次即全量下架）。
* ``api/scheduler.py`` 三个 job 有守卫（``[GEO-R6-CAN-016]``），但**抄了三份**，
  且 ``local_ids`` 没按 provider 归口 —— 多供应商接入后 stale 恒超阈值，
  下架分支恒跳过（表面无害，实际是守卫被永久卡死，等于没有下架能力）。

本模块把判据收成一份，阈值口径**原样沿用** ``[GEO-R6-CAN-016]``，不重新发明：

1. remote 快照为空 → 一条都不下架；
2. 待下架量 > ``max(50, 本地存量 × 30%)`` → 判为疑似部分快照，一条都不下架并打 warning；
3. 其余情况正常下架。

🔴 本模块**只做取舍，不执行下架**。返回允许下架的集合，由调用方自己写库 ——
这样守卫可以在无数据库的纯函数用例里被完整验证。

🔴 ``local_ids`` 必须由调用方**按 provider 归口后**传进来。传全量会让阈值恒被撑破
（这正是媒介盒子下架分支自快易播接入起恒跳过的根因）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Set

#: 阈值下限：本地存量很小时，30% 会低到一两条，正常波动就误报。
DEFAULT_MIN_ABS: int = 50
#: 阈值比例：一轮同步掉超过三成，正常目录变动解释不了，按部分快照处理。
DEFAULT_MAX_SHARE: float = 0.3

#: verdict.reason 的三个取值（调用方落留痕串时用，别自己拼字面量）
REASON_OK = ""
REASON_EMPTY_REMOTE = "empty_remote"
REASON_PARTIAL_SNAPSHOT = "partial_snapshot"


@dataclass(frozen=True)
class SnapshotGuardVerdict:
    """守卫裁定结果。

    ``stale`` 是**允许下架**的集合：被拦下时恒为空集，调用方无需再判 ``blocked``
    就不会误下架（判据只有一处出口，少一个「忘了看 blocked」的坑）。
    """

    stale: Set[int]
    blocked: bool
    reason: str
    raw_stale_count: int
    local_count: int
    remote_count: int
    threshold: int

    def summary(self) -> str:
        if not self.blocked:
            return f"下架{len(self.stale)}"
        return f"下架0(守卫拦下:{self.reason} · 原本将下架{self.raw_stale_count}/{self.local_count})"


def screen_stale_for_deactivation(
    *,
    local_ids: Iterable[int],
    remote_ids: Iterable[int],
    label: str,
    logger: Optional[logging.Logger] = None,
    min_abs: int = DEFAULT_MIN_ABS,
    max_share: float = DEFAULT_MAX_SHARE,
) -> SnapshotGuardVerdict:
    """判「本地有、上游快照没有」的这批能不能下架。

    :param local_ids: 本地**当前 active 且属于本供应商**的 id 集合。
    :param remote_ids: 本轮上游快照里出现的 id 集合。
    :param label: 打日志用的链路名（如 ``"媒体同步"`` / ``"[kuaiyibo] mhz_wemedia"``）。
    """
    local: Set[int] = set(local_ids)
    remote: Set[int] = set(remote_ids)
    stale: Set[int] = local - remote
    log = logger or logging.getLogger("GEO-CatalogSync")

    # 阈值只在 local 非空时有意义：local 为空时 stale 必为空，走正常分支即可。
    threshold = max(int(min_abs), int(len(local) * float(max_share))) if local else 0

    # 本地无在售记录 = 无物可护（stale 恒为空集，下架分支本来就是空操作）。
    # 必须排在两个拦截分支前面：否则「首次同步」「本轮快照里没有这一类」都会被
    # 报成 empty_remote，告警变噪音，真正的十几万条风险反而被淹掉。
    if not local:
        return SnapshotGuardVerdict(set(), False, REASON_OK, 0, 0, len(remote), threshold)

    if not remote:
        log.warning(
            "[CatalogGuard] %s remote 快照为空(疑似分页中断/session 过期)· 跳过下架,保护 %d 条本地记录",
            label, len(local),
        )
        return SnapshotGuardVerdict(set(), True, REASON_EMPTY_REMOTE,
                                    len(stale), len(local), len(remote), threshold)

    if local and len(stale) > threshold:
        log.warning(
            "[CatalogGuard] %s 疑似部分快照:将下架 %d/%d 条超阈值(阈值 %d)· 跳过下架待人工核",
            label, len(stale), len(local), threshold,
        )
        return SnapshotGuardVerdict(set(), True, REASON_PARTIAL_SNAPSHOT,
                                    len(stale), len(local), len(remote), threshold)

    return SnapshotGuardVerdict(stale, False, REASON_OK,
                                len(stale), len(local), len(remote), threshold)
