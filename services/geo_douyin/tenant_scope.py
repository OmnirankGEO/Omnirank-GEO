"""WP3 · 列表/详情按**当前授权**过滤,而不是按 `created_by`(规格 02 §9)。

## 现状的两个方向都错

现役 `db/geo_douyin_db.list_posts` 用 `created_by = %s` 划范围。后果是**双向**的:

  · **该看不见的还看得见**:撤销客户关系后,原创建者仍能从列表读到那个客户的作品;
  · **该看见的看不见**:同一 owner 下持有客户授权的合法同事,因为不是创建者而看不到。

规格 §9 的口径:`created_by` **只记录操作者**,不能代替当前授权。
当前授权 = tenant owner + brand/quote 授权。

## 谓词形态(为什么用 owner + brand 双条件而不是只用 brand)

只按 brand 过滤,会让**跨租户**同名/共享 brand 的场景漏读;
只按 owner 过滤,会让 owner 名下**未被分配**给该员工的客户也可见。
两个都要,且都取自服务端解析的当前授权 —— 请求体里的任何 id 都不参与。

## 管理员

管理员不走这条谓词(治理走专门审计动作,规格 §9)。本模块**不**给管理员开后门:
它只负责"非管理员看得到什么",管理员路径由调用方显式分流,这样"谁能看全部"
在代码里是一处可见的分叉,而不是藏在谓词里的一个 `OR is_admin`。
"""
from __future__ import annotations

from typing import Any, Final, Optional, Sequence


class ScopeUnavailable(RuntimeError):
    """解析不出当前授权范围。**fail-closed**:调用方返回空集或 403,绝不放行全量。"""


#: 未绑定 owner 的历史行(2026-08-17 生产:`geo_douyin_posts.tenant_owner_user_id` 全 NULL,
#: 因为 034 零 DML 不回填)。它们只能被**创建者本人**看到,不进团队可见面 ——
#: 猜绑历史归属是对象身份 H0 的反面(规格 §2.2 末:「禁止按品牌、关键词和日期猜绑」)。
LEGACY_VISIBILITY_NOTE: Final = (
    "历史行 tenant_owner_user_id 为 NULL:只对创建者可见,不猜绑归属"
)


def build_post_scope(*, tenant_owner_user_id: Optional[int],
                     authorized_brand_ids: Sequence[int],
                     actor_user_id: Optional[int]) -> tuple[str, list[Any]]:
    """返回 `(SQL 片段, 参数列表)`。

    合法可见 = 以下**任一**成立:
      ① 新链行:`tenant_owner_user_id` 命中当前 owner **且** brand 在当前授权集合内;
      ② 历史行:`tenant_owner_user_id IS NULL` **且** 是本人创建的。

    🔴 ② 刻意不带 brand 条件:历史行的 owner 未知,拿 brand 去推 owner 就是猜绑。
       让它只对创建者可见,是**收紧**而不是放宽 —— 撤权后连创建者也看不到新链行,
       历史行则维持原样直到有权威归属为止。
    """
    if tenant_owner_user_id is None and actor_user_id is None:
        raise ScopeUnavailable("既无 owner 也无 actor,无法解析可见范围")

    clauses: list[str] = []
    params: list[Any] = []

    if tenant_owner_user_id is not None and authorized_brand_ids:
        placeholders = ", ".join(["%s"] * len(authorized_brand_ids))
        clauses.append(
            f"(tenant_owner_user_id = %s AND brand_id IN ({placeholders}))"
        )
        params.append(int(tenant_owner_user_id))
        params.extend(int(b) for b in authorized_brand_ids)

    if actor_user_id is not None:
        clauses.append("(tenant_owner_user_id IS NULL AND created_by = %s)")
        params.append(int(actor_user_id))

    if not clauses:
        # owner 有、但授权 brand 集合为空,且没有 actor ⇒ 什么都看不到。
        # 返回恒假谓词而不是抛异常:列表页应显示空态,不是报错。
        return "FALSE", []

    return "(" + " OR ".join(clauses) + ")", params


def assert_object_in_scope(row: Any, *, tenant_owner_user_id: Optional[int],
                           authorized_brand_ids: Sequence[int],
                           actor_user_id: Optional[int]) -> None:
    """详情/mutation 的对象级二次校验。

    🔴 校验的 target 就是**后面真正读写的那一行**,不是请求里的任何 id ——
       否则可以传一个自己有权的 brand_id 去操作别人的作品
       (本仓 `_require_post_access` 的注释里记的就是这条)。
    """
    if row is None:
        raise ScopeUnavailable("对象不存在")
    data = dict(row)
    owner = data.get("tenant_owner_user_id")
    brand_id = data.get("brand_id")
    created_by = data.get("created_by")

    if owner is None:
        if actor_user_id is not None and int(created_by or 0) == int(actor_user_id):
            return
        raise ScopeUnavailable("历史作品只对创建者可见(归属未确权,不猜绑)")

    if (tenant_owner_user_id is not None
            and int(owner) == int(tenant_owner_user_id)
            and brand_id is not None
            and int(brand_id) in {int(b) for b in authorized_brand_ids}):
        return
    raise ScopeUnavailable("你已无法操作这个客户")
