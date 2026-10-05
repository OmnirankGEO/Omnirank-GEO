"""公开媒体身份投影 + 公私分离 allowlist DTO(规格 §11.5 / §15.7 / CUR-10)。

判据:MED-01 / MED-06 / MED-12 / MED-21 / POR-06 / POR-07 / POR-11。

═══════════════════════════════════════════════════════════════════════
🔴 CUR-10 的真实形态(census 2026-08-21 亲眼所见,不是规格转述)
═══════════════════════════════════════════════════════════════════════
两处 ``SELECT *`` 直出 ``mhz_media``:

  · ``db/publish_db.py:649`` ``get_media_list`` → ``api/publish_api.py:153``
    ``return {"status":"success", **result}``
  · ``api/publish_api.py:276`` ``list_media_by_engine`` 函数体内手写
    ``SELECT * FROM mhz_media`` → ``media=[dict(r) for r in cur.fetchall()]``

``mhz_media`` 同时含 ``price/price1/price2``(采购价)、``wholesale_cents``、
``platform_cost_cents``、``provider``、``provider_media_id``、``remark``、
``entrance_link`` 等**供应商私有列**。``SELECT *`` 之后再靠前端隐藏 = §11.5
明令禁止的「一个宽 DTO 再靠前端隐藏」。

本模块给出唯一正解:**服务端显式 allowlist 投影**。
列名不是抄来的 —— :data:`PRIVATE_COLUMN_PREFIXES` / :data:`PRIVATE_COLUMNS`
与 :func:`assert_no_private_leak` 让「新加一列私有字段却忘了挡」这件事
在判据里变红,而不是等它泄漏到线上。

🔴 为什么公开身份必须是 HMAC 派生而不是 media_id
------------------------------------------------
§15.7 逐字:两个 key 是「服务端安全、稳定、**不可反解供应商路由**的公开身份投影」。
直接把 ``mhz_media.id`` 当 publicMediaKey 等于把上游 media id 发出去 ——
POR-06 的毒串判据会当场命中。所以:

  publicMediaKey        = HMAC(secret, "public-media-key-v1" ‖ provider ‖ media_id)
  canonicalRootDomainKey= HMAC(secret, "public-root-domain-v1" ‖ canonical_root_domain)
  publicMediaOptionId   = HMAC(secret, "public-media-option-v1" ‖ snapshot_id ‖ media_key ‖ ordinal)

三者都**稳定**(同输入同输出,跨 preview→GET→override→confirm→status 逐值不变,MED-12)
且**不可反解**(HMAC 单向)。option id 额外绑 snapshot —— 它只是「本次候选选择句柄」,
§15.7 明确它「不能用于完成度计数」,绑 snapshot 后跨 snapshot 复用会自然解析失败。

🔴 root domain 归一化不做 PSL 查表
----------------------------------
本仓没有 publicsuffix 依赖,现造一份两级后缀表必然漏。这里的策略是**保守**:
已知多级公共后缀(``com.cn`` 这一族)显式列表化,其余取末两段。列表本身是数据,
判据拿它当分母;漏一条的后果是「两个不同 root 被算成两个」——
偏向**不合并**,不会把两家媒体错并成一家(MED-06 要防的是「同 root 多 URL 冒充多个根域」,
即错误方向是**过度合并**的反面)。真正的兜底在 :func:`canonical_root_domain_key`
只吃服务端目录里冻结的 ``source_domain``,不吃用户提交的 URL。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import struct
import unicodedata
from typing import Any, Iterable, Literal, Mapping, NamedTuple

IDENTITY_VERSION = "defgeo-public-media-identity-v1"

MediaRole = Literal[
    "authority_anchor", "strong_vertical", "broad_discovery", "official_owned",
]
#: §11.2 四角色闭集。判据拿它当分母,不手抄。
MEDIA_ROLES: tuple[MediaRole, ...] = (
    "authority_anchor", "strong_vertical", "broad_discovery", "official_owned",
)

#: §15.7 `reasonFacts.kind` 闭集。
ReasonKind = Literal[
    "question_importance", "industry_fit", "authority_fit", "audience_fit", "budget_fit",
]
REASON_KINDS: tuple[ReasonKind, ...] = (
    "question_importance", "industry_fit", "authority_fit", "audience_fit", "budget_fit",
)


# ══════════════════════════════════════════════════════════════════════════
# 私有列普查 —— CUR-10 / POR-06 / POR-07 的分母
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 **逐字**取自两份 ``CREATE TABLE mhz_media``(db/publish_db.py:54 与
#:    db/meijiehezi_db.py:108 —— 本仓对同一张表有两份建表语句,census 实证)
#:    以及 mhz_wemedia。凡是能反解供应商路由、采购成本或内部备注的列全在这里。
#:
#:    这份清单是**否定式分母**:公开投影只允许 :data:`PUBLIC_MEDIA_COLUMNS`,
#:    这里再列一遍私有列,是为了让 :func:`assert_no_private_leak` 能对
#:    「有人绕过投影直接塞了一行 dict 进响应」这种形态当场报红。
PRIVATE_COLUMNS: frozenset[str] = frozenset({
    # 采购价 / 毛利 / 加价(两份建表语句的并集)
    "price", "price1", "price2", "price_normal", "price_vip", "price_svip",
    "wholesale_cents", "wholesale_points", "platform_cost_cents",
    "cost_cents", "cost_points", "markup_ratio", "rebate_ratio",
    "video_price", "weitoutiao_price",
    # 上游路由 / 供应商身份
    "provider", "provider_media_id", "upstream_media_id", "api_host",
    "supplier", "supplier_id", "channel_account", "account_pool",
    # 内部备注 / 入口链接 / 原始响应
    "remark", "admin_remark", "entrance_link", "case_link", "raw_response",
    "internal_note", "sync_payload",
})

#: 私有列前缀。新加 ``cost_xxx`` / ``supplier_xxx`` / ``provider_xxx`` 会被自动纳管 ——
#: 靠人记得往上面那张表里补一行,是必然会漏的形态。
PRIVATE_COLUMN_PREFIXES: tuple[str, ...] = (
    "cost_", "supplier_", "provider_", "upstream_", "wholesale_", "markup_",
    "rebate_", "purchase_", "internal_",
)

#: 🔴 服务商公开面允许出现的 ``mhz_media`` 列(``provider_authenticated_public_dto``)。
#:    **只有这些列会被读**:store 层的 SELECT 直接用这个元组拼列名,
#:    所以「加一列私有字段」不会自动流到公开面 —— 那正是 SELECT * 做不到的事。
#:
#: 🔴 **逐列在真库上核过**(2026-08-21,SQL 四维核验的「列名」那一维):
#:    ``information_schema.columns WHERE table_name='mhz_media'`` 现取。
#:    第一版按记忆写了 ``industry`` —— **生产 mhz_media 没有这一列**
#:    (只有 ``special_industry``,语义也不同)。它让整个 preview 端点
#:    100% ``UndefinedColumn`` → 受控 500,即「路由在、功能不存在」。
#:    行业信息用现役真有的 ``resource_type_name``(「新闻资讯」「女性时尚」这类,
#:    ``db/publish_db.get_media_categories`` 的 P0 修复注释里写明 ``category``
#:    历史错位全 NULL、真实数据在 ``resource_type_name``)。
PUBLIC_MEDIA_COLUMNS: tuple[str, ...] = (
    "id", "media_name", "platform", "category", "media_type",
    "area", "resource_type_name", "source_domain", "is_active",
)

#: 客户 token 面允许出现的字段(``customer_token_dto``)。
#: §11.5:客户只见「公开媒体名称和平台」「公开履约」,**不出现 points**。
CUSTOMER_MEDIA_FIELDS: tuple[str, ...] = (
    "publicMediaName", "publicRootDomainLabel", "mediaRole",
)

#: 服务商公开 DTO 字段全集(含 exact points —— §11.5 表格第 4 行「服务商可见最终算力」)。
PROVIDER_MEDIA_FIELDS: tuple[str, ...] = (
    "publicMediaOptionId", "publicMediaKey", "canonicalRootDomainKey",
    "publicMediaName", "publicRootDomainLabel", "mediaRole",
)


class MediaIdentityError(ValueError):
    """身份/投影不合法。**拒绝签发**,不是就地凑一个能用的值。"""


class PrivateColumnLeak(AssertionError):
    """私有列出现在公开出口上。POR-06/07 的红。"""


# ══════════════════════════════════════════════════════════════════════════
# HMAC 派生
# ══════════════════════════════════════════════════════════════════════════
_ENV_KEY = "DEFGEO_PUBLIC_IDENTITY_SECRET"
#: 未配置时从本部署的 JWT 密钥按用途派生:每个部署稳定、互不相同、也不公开;
#: 同一部署内稳定,publicMediaKey 不随进程重启漂移(MED-12)。两者都没配 ⇒ 报错,不回落任何常量。
def _jwt_derived(purpose: bytes) -> bytes:
    from auth.jwt_utils import JWT_SECRET   # 没配 JWT_SECRET 时 jwt_utils 回落到本机生成的密钥文件
    if not JWT_SECRET:
        raise MediaIdentityError("DEFGEO_PUBLIC_IDENTITY_SECRET 与 JWT_SECRET 都没配")
    return hmac.new(JWT_SECRET.encode("utf-8"), b"defgeo-public-identity/" + purpose, hashlib.sha256).digest()


def _secret() -> bytes:
    raw = os.environ.get(_ENV_KEY) or ""
    return raw.encode("utf-8") if raw.strip() else _jwt_derived(b"public-media-key")


def _frame(*parts: bytes) -> bytes:
    """big-endian uint64 长度前缀 framing。

    与 ``presentation/commitments.py::_frame`` 同形(**刻意同形,不复用** ——
    那是 presentation 域的 commitment,本模块是身份域;共用一个函数会让
    「改了那边的 framing 顺手改了这边的身份」成为可能)。
    裸拼接下 ``("ab","c")`` 与 ``("a","bc")`` 同串,长度前缀让边界不可移动。
    """
    return b"".join(struct.pack(">Q", len(p)) + p for p in parts)


def _n(text: object, *, field: str) -> bytes:
    if not isinstance(text, str) or not text.strip():
        raise MediaIdentityError(f"{field} 必须是非空字符串,实得 {text!r}")
    return unicodedata.normalize("NFC", text).encode("utf-8")


def _derive(domain: str, *parts: bytes) -> str:
    return hmac.new(_secret(), _frame(domain.encode("ascii"), *parts), hashlib.sha256).hexdigest()


def public_media_key(*, provider: str, provider_media_id: object) -> str:
    """公开媒体身份键。``distinct_public_media`` 按它去重(MED-06)。

    同一媒体的多个库存 option 派生出**同一个** key —— 这正是「同 media 多 option
    不能冒充多家媒体」的实现形态。
    """
    return _derive(
        "public-media-key-v1",
        _n(provider, field="provider"),
        _n(str(provider_media_id), field="provider_media_id"),
    )


#: 已知多级公共后缀。**保守表**:漏一条只会让两个 root 不被合并(偏安全方向)。
_MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset({
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "co.uk", "org.uk", "gov.uk", "co.jp", "com.hk", "com.tw",
})

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def canonical_root_domain(domain: object) -> str:
    """把服务端目录里冻结的 ``source_domain`` 归一成 canonical root domain。

    只吃目录里的域名字面量,**不吃用户提交的 URL**(吃 URL 等于让客户端
    自己决定「这算不算同一个根域」,MED-06 就废了)。
    """
    if not isinstance(domain, str) or not domain.strip():
        raise MediaIdentityError(f"source_domain 必须是非空字符串,实得 {domain!r}")
    text = unicodedata.normalize("NFC", domain).strip().lower().rstrip(".")
    # 容忍目录里存了带 scheme / path 的脏值,但只取 host 部分,不做 URL 解析语义。
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@")[-1].split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    if not _DOMAIN_RE.match(text):
        raise MediaIdentityError(f"source_domain 形态不合法:{domain!r} → {text!r}")
    labels = text.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def canonical_root_domain_key(domain: object) -> str:
    """公开根域身份键。``distinct_root_domains`` 按它去重(MED-06)。"""
    return _derive("public-root-domain-v1", _n(canonical_root_domain(domain), field="root_domain"))


def public_media_option_id(*, decision_snapshot_id: str, media_key: str, ordinal: int) -> str:
    """本次候选的**选择句柄**。绑 snapshot —— 跨 snapshot 复用解析不到(§15.7)。"""
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise MediaIdentityError(f"ordinal 必须是非负整数,实得 {ordinal!r}")
    return _derive(
        "public-media-option-v1",
        _n(decision_snapshot_id, field="decisionSnapshotId"),
        _n(media_key, field="publicMediaKey"),
        struct.pack(">Q", ordinal),
    )


# ══════════════════════════════════════════════════════════════════════════
# allowlist 投影 —— CUR-10 的正解
# ══════════════════════════════════════════════════════════════════════════
class PublicMediaIdentity(NamedTuple):
    """§15.7 ``PublicMediaIdentity`` 的服务端形态。**没有任何私有字段的位置**。"""

    public_media_option_id: str
    public_media_key: str
    canonical_root_domain_key: str
    public_media_name: str
    public_root_domain_label: str
    media_role: MediaRole

    def provider_dto(self) -> dict[str, Any]:
        """服务商公开面(``provider_authenticated_public_dto``)。"""
        return {
            "publicMediaOptionId": self.public_media_option_id,
            "publicMediaKey": self.public_media_key,
            "canonicalRootDomainKey": self.canonical_root_domain_key,
            "publicMediaName": self.public_media_name,
            "publicRootDomainLabel": self.public_root_domain_label,
            "mediaRole": self.media_role,
        }

    def customer_dto(self) -> dict[str, Any]:
        """客户 token 面(``customer_token_dto``)。

        🔴 **不含 option id / media key / root key** —— 那三个是内部选择句柄与
        去重键;客户面出现它们不是泄漏成本,但会让客户 DTO 与 provider DTO
        产生可关联的 opaque ref(POR-11「客户投影无 points、内部 capability 和 canary」
        的同族要求:客户面只保留人能读的东西)。
        """
        return {
            "publicMediaName": self.public_media_name,
            "publicRootDomainLabel": self.public_root_domain_label,
            "mediaRole": self.media_role,
        }


def is_private_column(name: object) -> bool:
    """一个列名算不算私有。判据与 :func:`assert_no_private_leak` 共用同一份实现。"""
    if not isinstance(name, str):
        return False
    key = name.strip().lower()
    if key in PRIVATE_COLUMNS:
        return True
    return any(key.startswith(p) for p in PRIVATE_COLUMN_PREFIXES)


def private_columns_in(row: Mapping[str, Any] | Iterable[str]) -> list[str]:
    keys = row.keys() if isinstance(row, Mapping) else row
    return sorted(k for k in keys if is_private_column(k))


def assert_no_private_leak(payload: object, *, field: str = "payload") -> None:
    """递归检查公开出口的 payload 里没有任何私有列名。

    🔴 只查**键名**不查值 —— 值层的毒串由 DLP 双层
    (``gap_operation_labels.assert_no_internal_leak`` +
    ``xiaobang_facade_dlp.assert_facade_clean``)负责,本模块**不造第三层**(§0.5.1-6)。
    这里补的是那两层查不到的那一格:``{"provider_media_id": 8821}``
    键名是内部路由、值是纯数字,词表扫不出来,而它恰恰是最致命的一个。
    """
    stack: list[tuple[str, Any]] = [(field, payload)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, Mapping):
            bad = private_columns_in(node)
            if bad:
                raise PrivateColumnLeak(
                    f"{path} 出现私有列 {bad} —— §11.5 禁止「一个宽 DTO 再靠前端隐藏」。"
                    f"公开出口必须显式 allowlist(PUBLIC_MEDIA_COLUMNS)"
                )
            for k, v in node.items():
                stack.append((f"{path}.{k}", v))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                stack.append((f"{path}[{i}]", v))


def redact_private_columns(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """CUR-10 的**存量收口**:把已有 ``SELECT *`` 出口上的私有列剥掉。

    🔴 这不是 CUR-10 的正解 —— 正解是 :func:`project_media_row` 那条
       「私有原料进、公开身份出」的单向门,新链走那条。
       但存量 ``GET /api/publish/media`` 与 ``/media/by-engine`` 是**现役**接口,
       整形会打断存量前端;所以这里做**收口**:公开字段一个不少,
       私有列一个不留。

    与 pop 掉几个固定键的差别:判定走 :func:`is_private_column`,
    含前缀规则 —— 日后有人给 ``mhz_media`` 加一列 ``supplier_rebate_ratio``,
    这里**自动**挡住,不需要有人记得回来补一行。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({k: v for k, v in row.items() if not is_private_column(k)})
    return out


def project_media_row(
    row: Mapping[str, Any],
    *,
    media_role: str,
    decision_snapshot_id: str,
    ordinal: int,
) -> PublicMediaIdentity:
    """把目录行投影成公开身份。**只读 allowlist 里的列 + 两列私有身份原料**。

    ``provider`` / ``provider_media_id`` 在这里被读,但只作为 HMAC 的**输入**;
    它们不会出现在任何返回值里 —— 这是「私有原料进、公开身份出」的单向门。
    """
    if media_role not in MEDIA_ROLES:
        raise MediaIdentityError(f"未知 mediaRole {media_role!r};合法 = {list(MEDIA_ROLES)}")
    provider = row.get("provider") or "mhz"
    provider_media_id = row.get("provider_media_id")
    if provider_media_id in (None, ""):
        # 目录里 provider_media_id 允许为空(旧行);此时退回主键作为上游身份原料。
        # 退回的是**输入**,不是输出 —— HMAC 之后仍然不可反解。
        provider_media_id = row.get("id")
    media_key = public_media_key(provider=str(provider), provider_media_id=provider_media_id)
    domain = row.get("source_domain")
    root_key = canonical_root_domain_key(domain)
    return PublicMediaIdentity(
        public_media_option_id=public_media_option_id(
            decision_snapshot_id=decision_snapshot_id, media_key=media_key, ordinal=ordinal,
        ),
        public_media_key=media_key,
        canonical_root_domain_key=root_key,
        public_media_name=str(row.get("media_name") or "").strip(),
        public_root_domain_label=canonical_root_domain(domain),
        media_role=media_role,  # type: ignore[arg-type]
    )


def distinct_public_media(identities: Iterable[PublicMediaIdentity]) -> int:
    """MED-06:按冻结的 ``publicMediaKey`` 去重。**不按 option/name/URL**。"""
    return len({i.public_media_key for i in identities})


def distinct_root_domains(identities: Iterable[PublicMediaIdentity]) -> int:
    """MED-06:按 ``canonicalRootDomainKey`` 去重。"""
    return len({i.canonical_root_domain_key for i in identities})


def census() -> dict[str, Any]:
    """机械导出分母(§0.5.3 G-2)。判据从这里取,不手抄。"""
    return {
        "identityVersion": IDENTITY_VERSION,
        "mediaRoles": list(MEDIA_ROLES),
        "reasonKinds": list(REASON_KINDS),
        "publicMediaColumns": list(PUBLIC_MEDIA_COLUMNS),
        "privateColumns": sorted(PRIVATE_COLUMNS),
        "privateColumnPrefixes": list(PRIVATE_COLUMN_PREFIXES),
        "providerMediaFields": list(PROVIDER_MEDIA_FIELDS),
        "customerMediaFields": list(CUSTOMER_MEDIA_FIELDS),
        "multiLabelSuffixes": sorted(_MULTI_LABEL_SUFFIXES),
    }
