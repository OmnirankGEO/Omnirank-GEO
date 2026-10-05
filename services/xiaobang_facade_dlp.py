"""小榜 neutral façade 的站内 DLP-A 门(规格 §14.1 / §19.1 #11)。

## 与既有 `gap_operation_labels.assert_no_internal_leak` 的关系

**复用,不另造一套**(Refactor-not-rewrite)。既有闸已经覆盖「给人看的字符串里
不许有供应商/成本/工程术语/未翻译枚举」,而且它的术语表取自现役配置里真实
出现过的串。本模块在它之上补两件它按设计不管的事:

1. **扫描范围**。既有闸刻意跳过机读字段(``target_route`` / ``code`` / ``key`` …),
   因为那些不渲染给人。但 DLP 管的是「有没有流出去」,不是「会不会被渲染」——
   一个写在 ``status_url`` 里的上游订单号照样是泄漏。所以本模块对**全部**字符串
   (含机读键)扫供应商 / 成本数字 / 上游单号 / 内部 adapter 这四类。
2. **四类新形态**。上游订单号、内部 adapter 名与 HTTP method、原始 provider
   error/trace/redirect/header、以及裸成本数字。

## 反向对照(P0-D 的要求)

扫描器失效与「真的很干净」在结果上同形 —— 这是本仓反复付费的病。因此:

* :func:`dlp_term_inventory` 暴露术语表规模,锁可以证明它非空(空表恒绿);
* 配套测试**必须**用一组硬编码的毒串(供应商名 / 成本数字 / 上游单号)注入
  fixture 并要求判红,且那组毒串**不得**从本模块的常量里读 ——
  负样本与被测逻辑共用同一份真值 = 两边一起错还一起绿。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from services.gap_operation_labels import (
    InternalTermLeak,
    assert_no_internal_leak,
    leak_scanner_term_count,
)
from services.xiaobang_command_contract import (
    COMPUTE_ONLY_MIN_BIND_FIELDS,
    EXTERNAL_BIND_FIELDS,
)

DLP_CONTRACT_VERSION = "xiaobang-facade-dlp-a-v1"

#: 能力发现端点的 façade 名。豁免登记按 **(端点, 字段路径, 取值集合)** 三重收窄,
#: 所以每个需要豁免的端点都要有自己的 ``where``,不能共用一个大写死值。
CAPABILITIES_FACADE = "xiaobang_operations_capabilities"


# ── 字段级机器面豁免(2026-08-20 · capabilities 500 返修)────────────────────
#: 🔴 先说判定,再说做法。
#:
#: ``confirmation_policy.bind`` 的 11 个枚举(``actor_id`` / ``payload_hash`` /
#: ``expires_at`` …)在 :func:`assert_no_internal_leak` 眼里是「未翻译的内部枚举」,
#: 于是 ``GET /capabilities`` 对**任何看得见命令的身份**一律 500(fail-closed
#: 是对的,它拦住了一个真的出参)。修法二选一:补翻译表,还是登记豁免。
#:
#: **判定 = 登记豁免**,依据是消费方普查(交付单贴坐标):
#:   · 前端 grep ``confirmation_policy`` / ``capabilities`` → **零命中**;
#:   · 全仓 ``bind`` 的读取方只有两处,都不是显示面:
#:       ``services/xiaobang_command_contract.py:264`` 注册期自校验、
#:       ``tests/xiaobang_vnext_2026_08_18/test_command_contract.py`` 判据;
#:   · 五阶段 DTO 那一侧(``api/xiaobang_operations_api.py:419``)只下发
#:     ``confirmation_policy.mode``,**根本不带 bind** —— 也就是说 bind 上线至今
#:     只在 capabilities 这一个端点出现过,且没有任何人把它当文本渲染。
#: 硬塞一份人话翻译会把一份**机器契约字段清单**改成展示文案(键值语义漂移),
#: 那是砸契约,不是修 bug。
#:
#: 🔴 豁免的三重收窄,一个都不能少:
#:   ① ``where`` —— 只对 capabilities 这一个端点生效,别的端点原样 fail-closed;
#:   ② ``path_pattern`` —— 只对 ``$.commands[N].confirmation_policy.bind[i]`` 这一格生效;
#:   ③ ``values`` —— **只对合同源码里声明过的那些字段名**生效。
#:      往 bind 里塞一个没登记的枚举,照样 500(承重判据
#:      ``test_injecting_an_unregistered_bind_enum_still_trips_the_gate``;
#:      把这一条 values 收窄删掉,那条判据当场红 —— 已实拆演练过)。
#: 并且豁免只作用在「未翻译枚举」这一条规则上 —— 供应商名 / 成本术语 / 工程术语
#: 三条在这一格照旧全量生效(见 :func:`assert_no_internal_leak` 的钩子说明)。
#:
#: 🔴 ``values`` **机械取自源码常量**,不手抄 11 个字符串:手抄的那一份会在
#: 合同加字段的那天悄悄漏掉新字段,而漏掉的表现是端点 500,不是判据红。
_CONFIRMATION_BIND_VALUES: frozenset[str] = (
    frozenset(EXTERNAL_BIND_FIELDS) | frozenset(COMPUTE_ONLY_MIN_BIND_FIELDS)
)


@dataclass(frozen=True)
class MachineFaceExemption:
    """一条 (端点, 字段路径, 取值集合) 粒度的机器面豁免。"""

    where: str
    path_pattern: str
    values: frozenset[str]
    reason: str

    def matches(self, where: str, path: str, value: str) -> bool:
        if where != self.where:
            return False
        if value not in self.values:
            return False
        return re.fullmatch(self.path_pattern, path) is not None


MACHINE_FACE_EXEMPTIONS: tuple[MachineFaceExemption, ...] = (
    MachineFaceExemption(
        where=CAPABILITIES_FACADE,
        path_pattern=r"\$\.commands\[\d+\]\.confirmation_policy\.bind\[\d+\]",
        values=_CONFIRMATION_BIND_VALUES,
        reason=(
            "confirmation_policy.bind 是 §8.1 的确认绑定**字段名清单**(机器契约),"
            "全仓零显示面消费方;翻成人话会把字段清单改成展示文案。"
        ),
    ),
)


def is_machine_face_exempt(where: str, path: str, value: str) -> bool:
    """这一格 (端点, 路径, 取值) 是否已登记为机器面豁免。"""
    return any(item.matches(where, path, value) for item in MACHINE_FACE_EXEMPTIONS)


def machine_face_exemption_inventory() -> list[dict]:
    """给锁用:豁免面**逐条**可枚举,证明它没被悄悄放宽。"""
    return [
        {
            "where": item.where,
            "path_pattern": item.path_pattern,
            "value_count": len(item.values),
            "values": sorted(item.values),
            "reason": item.reason,
        }
        for item in MACHINE_FACE_EXEMPTIONS
    ]

#: 五阶段 DTO 里的机读键。它们是**标识与冻结枚举**,不渲染给人看
#: (人话在同级的 label / explanation / next_actions 里)。
#: 🔴 这份表按调用点传给既有闸,不写进全仓共享白名单 ——
#:    像 ``state`` 这样的通用键一旦进共享表,别人的出参也会跟着开洞。
FIVE_PHASE_MACHINE_KEYS: frozenset[str] = frozenset({
    # 标识
    "intent_id", "interaction_id", "execution_id", "execution_request_id",
    "prepare_request_id", "cancel_request_id", "confirmation_receipt_id",
    "approval_request_id", "channel_option_id", "assistant_request_id",
    "superseded_by_intent_id",
    # 冻结枚举(§10.5:协调态 / 领域态 / 资金态 / 外部态 四条投影各自独立)
    "intent_state", "state", "mode", "side_effect", "confirmation_mode",
    "reason_code", "error_code", "next_action_id",
    # 版本与指纹
    "registry_version", "operation_version", "contract_version",
    "request_schema_version", "response_schema_version",
    "pricing_version", "permission_version", "authority_version",
    "membership_version", "assignment_authority_version",
    "approval_policy_version", "operation_map_version",
    "payload_hash", "object_manifest_hash", "compute_quote_hash", "quote_hash",
    # [R3-P7 ③] 前端要监听的**表单字段名**。它们本来就是机读标识
    # (页面拿它对应输入框),人话在同级的 label / primary_label 里。
    "watched_fields",
    # 🔴 [R3-P8 ①] `quote_state` 是**冻结枚举**(not_applicable / unavailable /
    #    pending_domain_adapter / quoted / expired),人话在同级的 `quote_note` 里。
    #    它原来不在表里 ⇒ 真 `POST /prepare` 打过去就是 500(素树实测同形:
    #    5 个取值里 3 个被判红,只有 "quoted" 恰好因为是单词而放行 ——
    #    也就是说这个 500 只在"报不出价"的那几档触发,更难被发现)。
    "quote_state",
    # 资源与能力标识
    "resource_kind", "required_capability", "billing_feature",
    # 时间与链接
    "status_url", "expires_at", "compute_quote_expires_at",
    "approval_window_expires_at", "receipt_expires_at",
})

#: 上游订单号 / 会话 / 凭据形态。按**结构**判,不按具体供应商名判 ——
#: 换一家供应商这些形态照样存在。
#:
#: 🔴 [WO-A ③ · 2026-08-20] 下面三条 ``*_gap`` 是**实测逃逸**补的,不是想象出来的:
#:    基线上 ``{"explanation": "外部单号 KYB88123"}`` / ``{"order_sn": "MJH-88123"}`` /
#:    ``{"explanation": "order_sn=MJH-88123"}`` 三个负样本全部判绿。
#:    原因:CN 那条只认「上游/供应商/供货商」三个前缀,ASCII 那条只认
#:    ``<prefix>_order_id`` 一种拼法 —— 而现役代码里真实存在的字段名叫
#:    ``order_sn`` / ``order_sn_map`` / ``primary_sn`` / ``republished_order_sn``
#:    (见 ``api/meijiehezi_api.py``),一个都不长那个样。
_UPSTREAM_ID_PATTERNS: tuple[tuple[str, str], ...] = (
    ("upstream_order", r"(?<![a-z0-9_])(upstream|provider|vendor|supplier)_(order|task|job|request)_?id(?![a-z0-9])"),
    ("upstream_order_cn", r"(上游|供应商|供货商)(订单|单号|任务号)"),
    ("upstream_order_cn_gap", r"(外部|渠道|第三方|平台方|媒体方)(订单号|订单|单号|任务号)"),
    ("upstream_order_sn_gap",
     r"(?<![a-z0-9_])((order|task|job)_sn(_map)?|primary_sn|republished_order_sn"
     r"|out_(trade|order)_no|(provider|vendor|supplier|channel|upstream|external)_order_(no|sn|id))(?![a-z0-9])"),
    ("session_credential", r"(?<![a-z0-9_])(phpsessid|jsessionid|set-cookie|authorization:|bearer\s+[a-z0-9._-]{16,})"),
    ("sts_credential", r"(?<![a-z0-9_])(access_key_id|secret_access_key|security_token|sts_token)(?![a-z0-9])"),
    ("internal_http", r"(?<![a-z0-9_])(http|https)://(127\.0\.0\.1|localhost|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)"),
)

#: 内部 adapter 名与 HTTP method 不得进入公共 DTO(§8.2)。
#: adapter 命名规约:``<domain>_<object>_<phase>``,phase 取五阶段之一。
_ADAPTER_NAME = re.compile(
    r"(?<![a-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(query|prepare|confirm|cancel|execute|status)(?![a-z0-9])"
)
_HTTP_METHOD_FIELD = frozenset({"method", "http_method", "verb", "adapter", "adapters",
                                "internal_url", "upstream_url", "provider_route"})

#: 原始 provider 错误/追踪回显。
_RAW_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("raw_traceback", r"traceback \(most recent call last\)"),
    ("raw_exception", r"(?<![a-z0-9_])(connecterror|readtimeout|httpstatuserror|requestexception|urllib3)(?![a-z0-9])"),
    ("raw_status_echo", r"(?<![a-z0-9_])(upstream|provider) (returned|responded|status)"),
    ("stack_frame", r'file "[^"]+\.py", line \d+'),
)

#: 裸成本/单价数字(人民币口径)。用户面只允许出现「算力」。
#:
#: 🔴 [WO-A ③ · 2026-08-20] ``procurement_price`` 是**追加**的一条谓词,不是把
#:    ``cny_cost_word`` 改写成更宽的形态 —— 后者一改,原来那条守的形状就没人验了。
#:    实测逃逸:``{"explanation": "采购价 12 元"}`` 在基线判绿。原因是
#:    ``cny_cost_word`` 要求「采购」后面**紧跟数字**,而真实写法「采购价 12 元」
#:    中间隔着一个「价」字。同族的「结算价」「底价」裸词同理。
_COST_NUMBER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("cny_unit_price", r"[¥￥]\s?\d+(\.\d+)?\s*/\s*(次|条|篇|词|天|个)"),
    ("cny_cost_word", r"(成本|进货|采购|结算价|底价|毛利|返点|返利)\s*[:：]?\s*[¥￥]?\s?\d"),
    ("rmb_conversion", r"\d+\s*(元|人民币)\s*=\s*\d+\s*(积分|算力|额度)"),
    ("procurement_price", r"(采购|进货|结算|出厂|批发|上游|底)\s*(单价|价格|价|成本)"),
)

#: 渠道供应商身份。与继承来的 ``_SUPPLIER_TERMS``(模型/LLM 供应商)**互补**:
#: 那份表里一个**渠道**供应商都没有,而渠道名恰恰是最贵的一类泄漏 ——
#: 用户拿到名字就能绕过我们直采。
#: 🔴 名单取自现役代码里真实出现的渠道(``api/meijiehezi_api.py`` 的两个 provider),
#:    不是想象出来的清单;实测逃逸:``{"explanation": "这条走快易播发布"}`` 判绿。
_CHANNEL_SUPPLIER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("channel_supplier_cn", r"(快易播|媒介盒子)"),
    ("channel_supplier_code",
     r"(?<![a-z0-9_])(kuaiyibo|kyb|meijiehezi|mjhz|mhz|tikhub|metaso)(?![a-z0-9])"),
)

_COMPILED_UPSTREAM = tuple((name, re.compile(p, re.I)) for name, p in _UPSTREAM_ID_PATTERNS)
_COMPILED_RAW_ERROR = tuple((name, re.compile(p, re.I)) for name, p in _RAW_ERROR_PATTERNS)
_COMPILED_COST = tuple((name, re.compile(p, re.I)) for name, p in _COST_NUMBER_PATTERNS)
_COMPILED_CHANNEL_SUPPLIER = tuple(
    (name, re.compile(p, re.I)) for name, p in _CHANNEL_SUPPLIER_PATTERNS
)

#: 内部路由字段名出现在**值**里(不只是当键)。
#: 🔴 [WO-A ③ · 2026-08-20] 基线只比对 ``key in _HTTP_METHOD_FIELD``,于是
#:    ``{"detail": "provider_route=kyb_v2"}`` 判绿 —— 同一个字符串换个位置就流出去了。
#:    这里用**同一份**字段表编出正则,不另抄一张表(抄一张必然漂)。
_INTERNAL_ROUTE_IN_VALUE = re.compile(
    r"(?<![a-z0-9_])(" + "|".join(sorted(_HTTP_METHOD_FIELD)) + r")(?![a-z0-9])", re.I
)


class FacadeDlpLeak(RuntimeError):
    """façade 出口检出泄漏。检出即抛,不做「清洗后放行」。"""


def _walk(node: Any, path: str = "$") -> Iterable[tuple[str, str, str]]:
    """遍历**全部**字符串,连机读字段一起 —— 与既有闸的取舍刻意相反,理由见模块 docstring。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, "{0}.{1}".format(path, key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, "{0}[{1}]".format(path, index))
    elif isinstance(node, str):
        yield path, path.rsplit(".", 1)[-1].split("[", 1)[0], node


def scan_facade_payload(payload: Any) -> list[str]:
    """返回问题清单(空 = 干净)。不抛,方便调用方决定是抛还是记。"""
    problems: list[str] = []
    for path, key, text in _walk(payload):
        low = text.lower()
        for name, pattern in _COMPILED_UPSTREAM:
            # 🔴 **键名和值都要扫**。第一版只扫了值,负样本
            #    ``{"upstream_order_id": "MJH-88123"}`` 当场逃掉 ——
            #    泄漏在键名里,值本身长得像任何一串编号。
            #    这正是 P0-D 要求配负样本的理由:没有它,这个洞会以"全绿"上线。
            if pattern.search(low) or pattern.search(key.lower()):
                problems.append(
                    "{0}: 上游标识/凭据形态 {1} → key={2!r} value={3!r}".format(
                        path, name, key, text[:40]
                    )
                )
        for name, pattern in _COMPILED_RAW_ERROR:
            if pattern.search(low):
                problems.append("{0}: 原始 provider 错误回显 {1} → {2!r}".format(path, name, text[:60]))
        for name, pattern in _COMPILED_COST:
            if pattern.search(text):
                problems.append("{0}: 裸成本/单价数字 {1} → {2!r}".format(path, name, text[:60]))
        for name, pattern in _COMPILED_CHANNEL_SUPPLIER:
            # 键名和值一起扫,理由同上游标识那一段:泄漏在键名里同样是泄漏。
            if pattern.search(low) or pattern.search(key.lower()):
                problems.append(
                    "{0}: 渠道供应商身份 {1} → key={2!r} value={3!r}".format(
                        path, name, key, text[:40]
                    )
                )
        if key in _HTTP_METHOD_FIELD:
            problems.append("{0}: 公共 DTO 出现内部路由字段 {1!r}".format(path, key))
        elif _ADAPTER_NAME.fullmatch(low.strip()):
            problems.append("{0}: 疑似内部 adapter 名 → {1!r}".format(path, text[:60]))
        # 🔴 与上面那条是**两件事**:上面判「这个字段叫 provider_route」,
        #    这条判「某个字段的值里写着 provider_route=...」。同一串换个位置照样流出去。
        route_in_value = _INTERNAL_ROUTE_IN_VALUE.search(low)
        if route_in_value and key not in _HTTP_METHOD_FIELD:
            problems.append(
                "{0}: 值里出现内部路由字段名 {1!r} → {2!r}".format(
                    path, route_in_value.group(1), text[:60]
                )
            )
    return problems


def assert_facade_clean(payload: Any, *, where: str = "xiaobang_facade") -> None:
    """façade 唯一出口闸。

    两层都要过:
      1. 既有 :func:`assert_no_internal_leak` —— 用户面字符串的供应商/成本/术语/枚举;
      2. 本模块 :func:`scan_facade_payload` —— 全量字符串的上游标识/原始错误/
         裸成本/内部 adapter。
    任一层报即抛。**不清洗后放行** —— 清洗会把「谁把它放进去的」变成静默问题。
    """
    assert_no_internal_leak(
        payload, where=where, extra_machine_keys=FIVE_PHASE_MACHINE_KEYS,
        machine_face_exempt=lambda path, value: is_machine_face_exempt(where, path, value),
    )
    problems = scan_facade_payload(payload)
    if problems:
        raise FacadeDlpLeak(
            "[{0}] DLP-A 检出 {1} 处泄漏:\n  - {2}".format(
                where, len(problems), "\n  - ".join(problems[:20])
            )
        )


def dlp_term_inventory() -> dict[str, int]:
    """给锁用的活性自证:每一类规则都必须非空。

    空规则表的扫描器与「真的很干净」在结果上完全一样 —— 这条判据存在的
    唯一理由就是把这两种情况分开。
    """
    return {
        "inherited_terms": leak_scanner_term_count(),
        "upstream_patterns": len(_COMPILED_UPSTREAM),
        "raw_error_patterns": len(_COMPILED_RAW_ERROR),
        "cost_patterns": len(_COMPILED_COST),
        "internal_route_fields": len(_HTTP_METHOD_FIELD),
        "channel_supplier_patterns": len(_COMPILED_CHANNEL_SUPPLIER),
    }


__all__ = [
    "CAPABILITIES_FACADE",
    "DLP_CONTRACT_VERSION",
    "FIVE_PHASE_MACHINE_KEYS",
    "MACHINE_FACE_EXEMPTIONS",
    "FacadeDlpLeak",
    "InternalTermLeak",
    "MachineFaceExemption",
    "assert_facade_clean",
    "is_machine_face_exempt",
    "machine_face_exemption_inventory",
    "scan_facade_payload",
    "dlp_term_inventory",
]
